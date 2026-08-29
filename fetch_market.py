"""市場データを取得して data.json の series を更新する（Phase 1）.

BOE と財務省は「当月分」しか返さないソースがあるため、data.json 自体を
履歴キャッシュとして扱い、取得できた分を毎日マージしていく。
取得に失敗したソースは前回値を残したまま stale としてマークする。

    python fetch_market.py            # 取得して data.json を更新
    python fetch_market.py --dry-run  # 取得可否と鮮度を一覧表示（書き込まない）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import config
import sources
from sources import FetchError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data.json")


def _spec(key, label, fetcher, cadence="daily", required=False):
    return {"key": key, "label": label, "fetch": fetcher, "cadence": cadence, "required": required}


def build_specs(previous_series: dict) -> list:
    """取得対象の一覧。MOF の全履歴のように重いものは、蓄積が足りない
    ときだけ取りに行く。"""

    def jgb(tenor: str, key: str):
        have = len(previous_series.get(key, {}).get("values", []))
        return sources.fetch_mof_jgb(tenor, include_history=have < config.KEEP_DAYS)

    def chf10y():
        # 日次JSONは252営業日のローリング窓しか返さない。蓄積が足りないうちだけ、
        # 停止済みキューブ(5.5MB)から2025-07-31までの過去分を継ぎ足す。
        have = len(previous_series.get("chf10y", {}).get("values", []))
        daily = sources.fetch_snb_public_rate("r10")
        if have < config.KEEP_DAYS:
            daily = {**sources.fetch_snb_bond_history("10J0"), **daily}
        return daily

    specs = [
        # --- 金利（柱①の素材）: 全通貨10年で統一 ---
        _spec("us10y", "米10年国債 (FRED DGS10)",
              lambda: sources.fetch_fred("DGS10"), required=True),
        _spec("eur10y", "ユーロ圏10年AAA (ECB)",
              lambda: sources.fetch_ecb_yield("10Y"), required=True),
        _spec("jpy10y", "日本10年国債 (財務省)", lambda: jgb("10年", "jpy10y"), required=True),
        _spec("gbp10y", "英10年スポット (BOE)",
              lambda: sources.fetch_boe_current(10.0), required=True),
        _spec("aud10y", "豪10年国債 (RBA)",
              lambda: sources.fetch_rba_bond("10 year"), required=True),
        _spec("chf10y", "スイス連邦債10年 (SNB)", chf10y, required=True),
        # --- 旧2年系列（柱には入れない参考値）。蓄積を捨てず、後から
        #     「2年と10年のどちらが効いたか」を測れる状態を保つため残す。 ---
        _spec("us2y", "米2年国債 (FRED DGS2)", lambda: sources.fetch_fred("DGS2")),
        _spec("eur2y", "ユーロ圏2年AAA (ECB)", lambda: sources.fetch_ecb_yield("2Y")),
        _spec("jpy2y", "日本2年国債 (財務省)", lambda: jgb("2年", "jpy2y")),
        _spec("gbp2y", "英2年スポット (BOE)", lambda: sources.fetch_boe_current(2.0)),
        _spec("aud2y", "豪2年国債 (RBA)", lambda: sources.fetch_rba_bond("2 year")),
        _spec("chf6m", "SARON 6ヶ月 (SNB)", sources.fetch_snb_saron),
        # --- 政策織り込みの参考値（スコアには入れない） ---
        _spec("gbp_ois1y", "英OIS 1年 (BOE)", sources.fetch_boe_ois_1y_current),
        _spec("ff_futures", "FF金利先物 (Yahoo)", lambda: sources.fetch_yahoo("ZQ=F")),
        _spec("effr", "実効FF金利 (FRED)", lambda: sources.fetch_fred("EFFR")),
        _spec("ecb_depo", "ECB預金ファシリティ (FRED)", lambda: sources.fetch_fred("ECBDFR")),
        # --- リスクレジーム（柱④の素材） ---
        _spec("vix", "VIX (Yahoo)", lambda: sources.fetch_yahoo("^VIX"), required=True),
        _spec("hy_oas", "米HYスプレッド (FRED)", lambda: sources.fetch_fred("BAMLH0A0HYM2")),
        _spec("gold", "金 (Yahoo)", lambda: sources.fetch_yahoo("GC=F")),
        _spec("copper", "銅 (Yahoo)", lambda: sources.fetch_yahoo("HG=F"), required=True),
        # --- 固有要因（柱⑤の素材） ---
        _spec("oil", "WTI原油 (Yahoo)", lambda: sources.fetch_yahoo("CL=F")),
        _spec("china", "上海総合 (Yahoo)", lambda: sources.fetch_yahoo("000001.SS")),
        # --- 参考 ---
        _spec("breakeven5y", "期待インフレ5年 (FRED)", lambda: sources.fetch_fred("T5YIE")),
        _spec("dxy", "ドル指数 (Yahoo)", lambda: sources.fetch_yahoo("DX-Y.NYB")),
        # --- コモディティ（commodities.html の素材。FXスコアには一切入らない） ---
        _spec("silver", "銀 (Yahoo)", lambda: sources.fetch_yahoo("SI=F")),
        _spec("real10y", "米10年実質金利 (FRED DFII10)", lambda: sources.fetch_fred("DFII10")),
        _spec("brent", "ブレント原油 (Yahoo)", lambda: sources.fetch_yahoo("BZ=F")),
        _spec("wti_m2", "WTI 2番限 (Yahoo)", sources.fetch_wti_second_month),
        _spec("eia_crude_stocks", "米商業原油在庫 (EIA週次)",
              sources.fetch_eia_crude_stocks, cadence="weekly"),
    ]

    for pair, ticker in config.FX_TICKERS.items():
        specs.append(
            _spec(f"fx_{pair}", f"{pair} (Yahoo)",
                  lambda t=ticker: sources.fetch_yahoo(t), required=True)
        )
    for ccy, ticker in config.EQUITY_TICKERS.items():
        specs.append(
            _spec(f"eq_{ccy}", f"{ccy}株価指数 {ticker} (Yahoo)",
                  lambda t=ticker: sources.fetch_yahoo(t))
        )
    return specs


def series_to_dict(entry) -> dict:
    """data.json の {"dates": [...], "values": [...]} を {date: value} に戻す。"""
    if not entry:
        return {}
    return dict(zip(entry.get("dates", []), entry.get("values", [])))


def dict_to_series(series: dict) -> dict:
    keys = sorted(series)
    return {"dates": keys, "values": [series[k] for k in keys]}


def load_previous(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! 既存の data.json を読めなかった: {exc}", file=sys.stderr)
        return {}


def run(dry_run: bool = False) -> dict:
    previous = load_previous(DATA_PATH)
    prev_series = previous.get("series", {})

    series_out: dict = {}
    health: dict = {}
    today = dt.date.today()

    specs = build_specs(prev_series)
    print(f"[{today}] {len(specs)} ソースを取得します\n")

    for spec in specs:
        key, label = spec["key"], spec["label"]
        old = series_to_dict(prev_series.get(key))
        status = {"label": label, "cadence": spec["cadence"], "required": spec["required"]}
        try:
            fetched = spec["fetch"]()
            merged = sources.merge_series(old, fetched)
            status.update(ok=True, error=None, fetched=len(fetched), fallback=False)
        except FetchError as exc:
            merged = old
            status.update(ok=False, error=str(exc)[:300], fetched=0, fallback=bool(old))
        except Exception as exc:  # noqa: BLE001 - 1ソースの想定外エラーで全体を落とさない
            merged = old
            status.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300],
                          fetched=0, fallback=bool(old))

        merged = sources.trim_series(merged, config.KEEP_DAYS)
        series_out[key] = dict_to_series(merged)

        latest = sources.last_date(merged)
        age = sources.stale_days(merged, today)
        limit = config.STALE_THRESHOLD_DAYS.get(spec["cadence"], 5)
        status.update(
            last_date=latest,
            stale_days=age,
            points=len(merged),
            stale=(age is None or age > limit),
        )
        health[key] = status

        mark = "OK  " if status["ok"] else ("WARN" if merged else "FAIL")
        detail = f"{latest}  {len(merged):>4}点"
        if not status["ok"]:
            detail += f"  ← {status['error'][:80]}"
        elif status["stale"]:
            detail += f"  (stale: {age}日前)"
        print(f"  [{mark}] {label:<34} {detail}")

    failures = [k for k, v in health.items() if not v["ok"] and v["required"]]
    hard_fail = [k for k, v in health.items() if not v["ok"] and v["required"] and not v["fallback"]]

    print()
    ok_count = sum(1 for v in health.values() if v["ok"])
    print(f"取得成功 {ok_count}/{len(specs)}")
    if failures:
        print(f"必須ソースの取得失敗（前回値で継続）: {', '.join(failures)}")
    if hard_fail:
        print(f"致命的（前回値もなし）: {', '.join(hard_fail)}")

    payload = dict(previous)
    payload.update(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        phase=1,
        series=series_out,
        sources=health,
    )

    if dry_run:
        print("\n--dry-run のため data.json は書き換えません")
        return payload

    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(DATA_PATH) / 1024
    print(f"\ndata.json を書き出しました ({size_kb:.0f} KB)")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="市場データを取得して data.json を更新する")
    parser.add_argument("--dry-run", action="store_true",
                        help="取得可否と鮮度を確認するだけで書き込まない")
    args = parser.parse_args()

    payload = run(dry_run=args.dry_run)
    hard_fail = [k for k, v in payload["sources"].items()
                 if not v["ok"] and v["required"] and not v["fallback"]]
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
