"""長期履歴の取得 — 較正専用.

既存の data.json は画面用で config.KEEP_DAYS=400 に切り詰められている。
こちらは **trim せず**、取れるだけ遡って macro_history.json に貯める。
ドル・ユーロ・ゴールド・シルバー・WTI の「今どのドライバーが効いているか」を
測るには、関係が変わった過去の局面が必要になるため。

    python macro_history.py            # 全ジョブを取得してマージ
    python macro_history.py --only cot # キーの前方一致で絞る

2回目以降も差分マージなので、そのまま再実行してよい。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

import sources
from fetch_market import dict_to_series, load_previous, series_to_dict

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "macro_history.json")

# Yahoo は range=max を指定すると日次ではなく月次を返す（interval=1d を付けても
# 長期レンジでは勝手に粗くなる）。25y と明示すれば日次で約6,300点取れる。
YAHOO_RANGE = "25y"

# ---------------------------------------------------------------------------
# CFTC 建玉明細（COT） — 無料・認証不要・週次
#
# legacy futures-only は金・銀・WTI・ユーロ先物を同じスキーマで返す唯一の系列。
# disaggregated(6dca-aqww) は商品しか無く、通貨は TFF 側にあるため使わない。
# ---------------------------------------------------------------------------

COT_ENDPOINT = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"

COT_MARKETS = {
    "gold": "088691",
    "silver": "084691",
    "wti": "067651",
    "eur": "099741",
}


def fetch_cot(code: str, limit: int = 5000) -> tuple[dict, dict]:
    """投機筋（noncommercial）のネット建玉を返す.

    戻り値は (ネット枚数, 建玉全体に対するネットの比率%) の2系列。
    枚数は市場規模が時代とともに変わるので、比率のほうが長期比較に向く。
    """
    query = urllib.parse.urlencode({
        "cftc_contract_market_code": code,
        "$limit": limit,
        "$select": ("report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                    "noncomm_positions_short_all,open_interest_all"),
        "$order": "report_date_as_yyyy_mm_dd DESC",
    })
    rows = json.loads(sources.http_get(f"{COT_ENDPOINT}?{query}", timeout=90))
    net, pct = {}, {}
    for row in rows:
        date = (row.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not date:
            continue
        try:
            long_ = float(row["noncomm_positions_long_all"])
            short = float(row["noncomm_positions_short_all"])
            oi = float(row["open_interest_all"])
        except (KeyError, TypeError, ValueError):
            continue
        net[date] = long_ - short
        if oi > 0:
            pct[date] = round((long_ - short) / oi * 100, 3)
    if not net:
        raise sources.FetchError(f"CFTC {code}: 有効なレコードが0件")
    return net, pct


def cot_job(name: str, code: str):
    """COT は1リクエストで2系列（枚数と比率）を返すので専用の展開をする。"""
    def run():
        net, pct = fetch_cot(code)
        return {f"cot_{name}_net": net, f"cot_{name}_pct": pct}
    return run


# ---------------------------------------------------------------------------
# 取得ジョブ
#
# 各ジョブは {系列キー: {日付: 値}} を返す。1ジョブが複数系列を返してよい。
# ---------------------------------------------------------------------------

def fred(key: str, series_id: str):
    return lambda: {key: sources.fetch_fred(series_id)}


def yahoo(key: str, ticker: str):
    return lambda: {key: sources.fetch_yahoo(ticker, YAHOO_RANGE)}


JOBS = [
    # --- 価格（測る対象そのもの） ---
    ("eurusd", "EURUSD（1999-01〜・ユーロ誕生から）", fred("eurusd", "DEXUSEU")),
    ("wti", "WTI原油スポット（1986-01〜）", fred("wti", "DCOILWTICO")),
    ("brent", "ブレント原油（1987-05〜）", fred("brent", "DCOILBRENTEU")),
    ("gold", "ゴールド先物", yahoo("gold", "GC=F")),
    ("silver", "シルバー先物", yahoo("silver", "SI=F")),

    # --- ドルの水準 ---
    ("dxy_broad", "ドル指数・広義（2006-01〜）", fred("dxy_broad", "DTWEXBGS")),
    ("dxy_major", "ドル指数・主要通貨（1973-01〜2019-12で終了）",
     fred("dxy_major", "DTWEXM")),

    # --- 金利・インフレ（ドルと金の主要ドライバー候補） ---
    ("us_real10y", "米10年実質金利 TIPS（2003-01〜）", fred("us_real10y", "DFII10")),
    ("us_be10y", "米期待インフレ10年", fred("us_be10y", "T10YIE")),
    ("us_be5y", "米期待インフレ5年", fred("us_be5y", "T5YIE")),
    ("us10y", "米10年名目（1962-01〜）", fred("us10y", "DGS10")),
    ("us2y", "米2年名目", fred("us2y", "DGS2")),
    ("ff", "FF実効金利（1954-07〜）", fred("ff", "DFF")),
    ("ez10y", "ユーロ圏10年AAA（2004-09〜）",
     lambda: {"ez10y": sources.fetch_ecb_yield("10Y", 6000)}),
    ("ecb_depo", "ECB預金ファシリティ金利", fred("ecb_depo", "ECBDFR")),
    ("us_cpi", "米CPI（月次・1947-01〜）", fred("us_cpi", "CPIAUCSL")),
    ("ez_cpi", "ユーロ圏CPI（月次・1996-12〜）",
     fred("ez_cpi", "CP0000EZ19M086NEST")),

    # --- リスク環境・流動性 ---
    ("vix", "VIX（FRED版・1990-01〜）", fred("vix", "VIXCLS")),
    # HYスプレッド BAMLH0A0HYM2 は cosd を付けても2023-08以降しか返らない
    # （ICEのライセンス制限と思われる）。長期の信用スプレッドは Baa−10年で代替する。
    # 重複期間での相関は 水準0.544 / 20日変化0.735。
    ("baa10y", "Baa−米10年スプレッド（1986-01〜・HY代替）", fred("baa10y", "BAA10Y")),
    ("hy_oas", "米HYスプレッド（2023-08〜のみ・参考）", fred("hy_oas", "BAMLH0A0HYM2")),
    # AI インフラ投資を賄う社債は投資適格。信用不安の入口を見るならここが直接的。
    ("ig_oas", "米投資適格OAS（2023-08〜）", fred("ig_oas", "BAMLC0A0CM")),
    ("fed_assets", "FRB総資産（週次・2002-12〜）", fred("fed_assets", "WALCL")),

    # --- 工業需要・ドル代替 ---
    ("copper", "銅先物（シルバーの工業需要の代理）", yahoo("copper", "HG=F")),
    ("btc", "ビットコイン（ドル代替の温度計）", yahoo("btc", "BTC-USD")),

    # --- 原油の需給 ---
    # ターム構造（期近と2番限の差）は長期に遡れない。Yahoo は満期済み限月を配信して
    # おらず CLZ18.NYM 等は全て404を返すため、sources.fetch_wti_m2_history は
    # 2年以内しか拾えない。代わりにブレント−WTIスプレッド（brent と wti の差、
    # どちらも1986/1987年から取得済み）を現物需給の代理として macro.py 側で作る。
    ("eia_crude_stocks", "米原油在庫（EIA・週次・1982-08〜）",
     lambda: {"eia_crude_stocks": sources.fetch_eia_crude_stocks()}),

    # --- 建玉（価格履歴では見えない「今の持ち高」） ---
    ("cot_gold", "COT ゴールド 投機筋ネット建玉", cot_job("gold", COT_MARKETS["gold"])),
    ("cot_silver", "COT シルバー 投機筋ネット建玉", cot_job("silver", COT_MARKETS["silver"])),
    ("cot_wti", "COT WTI 投機筋ネット建玉", cot_job("wti", COT_MARKETS["wti"])),
    ("cot_eur", "COT ユーロ先物 投機筋ネット建玉", cot_job("eur", COT_MARKETS["eur"])),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="",
                        help="ジョブ名の前方一致で絞る（例: --only cot）")
    args = parser.parse_args()

    jobs = [j for j in JOBS if j[0].startswith(args.only)] if args.only else JOBS
    if not jobs:
        print(f"'{args.only}' に一致するジョブがありません。", file=sys.stderr)
        return 1

    previous = load_previous(HISTORY_PATH) or {}
    series = previous.setdefault("series", {})
    ok = failed = 0

    for name, label, fetcher in jobs:
        try:
            fetched = fetcher()
        except Exception as exc:  # noqa: BLE001
            print(f"  [NG] {label:<44} {str(exc)[:70]}", file=sys.stderr)
            failed += 1
            continue

        for key, values in fetched.items():
            before = series_to_dict(series.get(key))
            # trim_series は呼ばない。履歴を捨てる関数なのでこの経路では使わない。
            merged = sources.merge_series(before, values)
            series[key] = dict_to_series(merged)
            dates = sorted(merged)
            print(f"  [OK] {key:<20} {len(merged):>6}点  "
                  f"{dates[0]} 〜 {dates[-1]}")
        ok += 1

    previous["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                             time.gmtime())
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(previous, fh, ensure_ascii=False, separators=(",", ":"))

    size_mb = os.path.getsize(HISTORY_PATH) / 1024 / 1024
    print(f"\n{ok}ジョブ成功 / {failed}ジョブ失敗 ・ "
          f"{len(series)}系列 ・ macro_history.json {size_mb:.1f}MB")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
