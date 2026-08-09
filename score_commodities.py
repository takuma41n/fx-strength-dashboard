"""コモディティ（金・銀・WTI）のファンダ柱スコア → 方向性.

FX側（score.py）との違い:
  * 閉じたバスケットではないので demean しない（絶対スコア）。
    「3銘柄すべて強気」がありうる。
  * 柱が欠損した時点はウェイトを再正規化して残りの柱で算出する。
    ターム構造と在庫は z-score に必要な蓄積が貯まるまで NaN になるため、
    固定ウェイトで薄めると機械的に「中立」へ誤誘導されてしまう。
  * リスクレジームは score.py の4成分から「金/銅比」を除いた3成分で
    取り直す。金/銅比を金のスコアに使うと「金上昇 → レジーム低下 →
    金のリスク柱上昇」という自己強化ループ（実質モメンタムの二重計上）になる。
  * バイアス閾値はFXと別（COMMO_BIAS_*）。FXの50/30は「2通貨のスコア差」
    に対する較正値で、単一銘柄の絶対スコアには過大。

score.py は一切変更せずユーティリティだけを import する。
出力は data.json のトップレベル "commodities" キー（scores とは兄弟）。

    python score_commodities.py             # data.json に書き込む
    python score_commodities.py --backtest  # スコア符号 vs 翌5/10営業日の的中率
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np

import config
import score
from score import (
    DATA_PATH,
    MIN_SAMPLES,
    align,
    as_map,
    conviction_label,
    diff_n,
    load_data,
    nan_safe,
    pct_change_n,
    rolling_beta,
    rolling_pct_rank,
    rolling_z,
    squash,
)


# ---------------------------------------------------------------------------
# ドライバの z 化
# ---------------------------------------------------------------------------

def z_multi(x: np.ndarray, kind: str = "diff", sub: dict | None = None) -> np.ndarray:
    """Δ5/Δ20/Δ60 の z-score を重み付き合成。pillar_rates と同じ形。"""
    f = diff_n if kind == "diff" else pct_change_n
    w = sub or config.RATE_SUB_WEIGHTS
    z5 = rolling_z(f(x, 5), config.Z_LOOKBACK)
    z20 = rolling_z(f(x, 20), config.Z_LOOKBACK)
    z60 = rolling_z(f(x, 60), config.Z_LOOKBACK)
    combined = (
        w["d5"] * np.nan_to_num(z5)
        + w["d20"] * np.nan_to_num(z20)
        + w["d60"] * np.nan_to_num(z60)
    )
    valid = ~(np.isnan(z5) & np.isnan(z20) & np.isnan(z60))
    return np.where(valid, combined, np.nan)


def inventory_z(weekly_map: dict, calendar: list) -> np.ndarray:
    """EIA在庫の z を「週次空間で」計算してから日次カレンダーへ前方補完する。

    先に日次へ補完してから diff を取ると、新プリントが載った日だけ値が立ち
    他の日は 0 になって z が壊れる。Δ52週は原油在庫の年内季節性を相殺する。
    """
    if not weekly_map:
        return np.full(len(calendar), np.nan)
    weeks = sorted(weekly_map)
    x = np.array([float(weekly_map[k]) for k in weeks])
    w = config.INVENTORY_SUB_WEIGHTS
    z1 = rolling_z(diff_n(x, 1), config.INVENTORY_Z_LOOKBACK)
    z4 = rolling_z(diff_n(x, 4), config.INVENTORY_Z_LOOKBACK)
    z52 = rolling_z(diff_n(x, 52), config.INVENTORY_Z_LOOKBACK)
    comb = (
        w["w1"] * np.nan_to_num(z1)
        + w["w4"] * np.nan_to_num(z4)
        + w["w52"] * np.nan_to_num(z52)
    )
    valid = ~(np.isnan(z1) & np.isnan(z4) & np.isnan(z52))
    comb = np.where(valid, comb, np.nan)
    zmap = {weeks[i]: float(comb[i]) for i in range(len(weeks)) if not np.isnan(comb[i])}
    return align(zmap, calendar)


def term_structure(series: dict, calendar: list) -> tuple[np.ndarray, np.ndarray]:
    """WTI 期近−2番限スプレッド（期近比%）。正 = バックワーデーション = 現物逼迫。

    「変化でなく水準」を使う例外。スプレッドの水準そのものが需給の逼迫度を
    表す価格指標なので、水準0.55 + Δ20日0.45 の二段構えにする
    （CHF固有柱が EURCHF 水準の pct_rank を使うのと同じ扱い）。
    """
    m1 = align(as_map(series.get("oil")), calendar)
    m2 = align(as_map(series.get("wti_m2")), calendar)
    with np.errstate(divide="ignore", invalid="ignore"):
        spread = np.where((m1 > 0) & (m2 > 0), (m1 - m2) / m1 * 100.0, np.nan)
    z_level = rolling_z(spread, config.Z_LOOKBACK)
    z_chg = rolling_z(diff_n(spread, 20), config.Z_LOOKBACK)
    comb = 0.55 * np.nan_to_num(z_level) + 0.45 * np.nan_to_num(z_chg)
    valid = ~(np.isnan(z_level) & np.isnan(z_chg))
    return np.where(valid, comb, np.nan), spread


def safe_risk_regime(series: dict, calendar: list) -> tuple[np.ndarray, dict]:
    """score.risk_regime の4成分から「金/銅比」を除いた3成分の平均（z空間）。"""
    _, comps = score.risk_regime(series, calendar)
    safe = {k: v for k, v in comps.items() if k != "金/銅比"}
    stacked = np.vstack([safe[k] for k in safe])
    mean = np.nanmean(stacked, axis=0)
    allnan = np.all(np.isnan(stacked), axis=0)
    return np.where(allnan, np.nan, mean), safe


# ---------------------------------------------------------------------------
# 柱の合成
# ---------------------------------------------------------------------------

def combine_commodity(pillars: dict, weights: dict) -> tuple[np.ndarray, np.ndarray]:
    """欠損柱を除いた残りでウェイトを再正規化して合成する。

    戻り値: (合成スコア, 有効ウェイト合計)。有効ウェイト合計は
    implemented_weight としてUIに出す。
    """
    n = len(next(iter(pillars.values())))
    acc = np.zeros(n)
    wsum = np.zeros(n)
    for name, w in weights.items():
        ok = ~np.isnan(pillars[name])
        acc += np.where(ok, np.nan_to_num(pillars[name]) * w, 0.0)
        wsum += np.where(ok, w, 0.0)
    total = np.where(wsum > 1e-9, acc / np.where(wsum > 1e-9, wsum, 1.0), np.nan)
    return total, wsum


def commo_bias(value: float) -> int:
    if value >= config.COMMO_BIAS_STRONG:
        return 2
    if value >= config.COMMO_BIAS_MILD:
        return 1
    if value <= -config.COMMO_BIAS_STRONG:
        return -2
    if value <= -config.COMMO_BIAS_MILD:
        return -1
    return 0


# ---------------------------------------------------------------------------
# 計算本体
# ---------------------------------------------------------------------------

def compute(data: dict) -> dict:
    series = data.get("series", {})

    cal_set: set = set()
    for sym in config.COMMODITIES:
        cal_set |= set(as_map(series.get(config.COMMODITY_META[sym]["series"])))
    calendar = sorted(cal_set)
    if len(calendar) < 80:
        raise SystemExit(
            f"コモディティ系列が不足しています（{len(calendar)}日分）。"
            "先に fetch_market.py を実行してください。")

    px = {sym: align(as_map(series.get(config.COMMODITY_META[sym]["series"])), calendar)
          for sym in config.COMMODITIES}

    # --- 共有ドライバ ---
    real = align(as_map(series.get("real10y")), calendar)
    dxy = align(as_map(series.get("dxy")), calendar)
    brk = align(as_map(series.get("breakeven5y")), calendar)
    copper = align(as_map(series.get("copper")), calendar)
    china = align(as_map(series.get("china")), calendar)
    brent = align(as_map(series.get("brent")), calendar)

    z_real = z_multi(real, "diff")
    z_dxy = z_multi(dxy, "pct")
    z_brk = z_multi(brk, "diff")

    # 工業需要 = AUD固有柱と同じ構成（0.6銅 + 0.4上海、20日変化）
    z_copper = rolling_z(pct_change_n(copper, 20), config.Z_LOOKBACK)
    z_china = rolling_z(pct_change_n(china, 20), config.Z_LOOKBACK)
    industrial = 0.6 * np.nan_to_num(z_copper) + 0.4 * np.nan_to_num(z_china)
    industrial = np.where(~(np.isnan(z_copper) & np.isnan(z_china)), industrial, np.nan)

    mom = {sym: z_multi(px[sym], "pct", config.MOMENTUM_SUB_WEIGHTS)
           for sym in config.COMMODITIES}

    with np.errstate(divide="ignore", invalid="ignore"):
        gs_ratio = np.where(px["XAGUSD"] > 0, px["XAUUSD"] / px["XAGUSD"], np.nan)
    gs_pct = rolling_pct_rank(gs_ratio, config.Z_LOOKBACK)

    inv_z = inventory_z(as_map(series.get("eia_crude_stocks")), calendar)
    term_z, term_spread = term_structure(series, calendar)
    regime_mean, regime_comps = safe_risk_regime(series, calendar)

    # --- 柱（符号はここで確定。正 = その銘柄にとって強気） ---
    pillars = {
        "XAUUSD": {
            "real_rate": squash(-z_real),
            "usd": squash(-z_dxy),
            "risk": squash(-regime_mean),  # リスクオフ = 金にとって強気
            "inflation": squash(z_brk),
            "momentum": squash(mom["XAUUSD"]),
        },
        "XAGUSD": {
            "gold_link": squash(mom["XAUUSD"]),
            "industrial": squash(industrial),
            "real_rate": squash(-z_real),
            "usd": squash(-z_dxy),
            "momentum": squash(mom["XAGUSD"]),
            # 比が高い = 銀が金対比で割安 = 平均回帰で銀強気
            "gs_ratio": np.where(np.isnan(gs_pct), np.nan, 100.0 * (2.0 * gs_pct - 1.0)),
        },
        "WTI": {
            "inventory": squash(-inv_z),
            "term": squash(term_z),
            "demand": squash(industrial),
            "momentum": squash(mom["WTI"]),
            "usd": squash(-z_dxy),
        },
    }

    totals, wsums = {}, {}
    for sym in config.COMMODITIES:
        totals[sym], wsums[sym] = combine_commodity(
            pillars[sym], config.COMMODITY_PILLAR_WEIGHTS[sym])

    last = len(calendar) - 1

    def at(arr, offset=0):
        i = last - offset
        return nan_safe(arr[i]) if 0 <= i < len(arr) else 0.0

    # --- 診断値（表示文字列まで作ってフロントを単純に保つ） ---
    def _valid_count(arr):
        return int(np.sum(~np.isnan(arr)))

    silver_ret = np.diff(np.log(np.where(px["XAGUSD"] > 0, px["XAGUSD"], np.nan)),
                         prepend=np.nan)
    gold_ret = np.diff(np.log(np.where(px["XAUUSD"] > 0, px["XAUUSD"], np.nan)),
                       prepend=np.nan)
    beta_sg = rolling_beta(silver_ret, gold_ret, config.BETA_LOOKBACK)

    inv_map = as_map(series.get("eia_crude_stocks"))
    inv_weeks = sorted(inv_map)
    inv_last = float(inv_map[inv_weeks[-1]]) if inv_weeks else None
    inv_w1 = (inv_last - float(inv_map[inv_weeks[-2]])) if len(inv_weeks) >= 2 else None

    diagnostics = {
        "XAUUSD": [
            {"label": "米10年実質金利",
             "text": f"{at(real):.2f}%（20日 {at(diff_n(real, 20)) * 100:+.0f}bp）"},
            {"label": "ドル指数",
             "text": f"{at(dxy):.1f}（20日 {at(pct_change_n(dxy, 20)) * 100:+.1f}%）"},
            {"label": "期待インフレ5年",
             "text": f"{at(brk):.2f}%（20日 {at(diff_n(brk, 20)) * 100:+.0f}bp）"},
        ],
        "XAGUSD": [
            {"label": "金銀比",
             "text": f"{at(gs_ratio):.1f}（過去1年の{at(gs_pct) * 100:.0f}%位）"},
            {"label": "銅",
             "text": f"20日 {at(pct_change_n(copper, 20)) * 100:+.1f}%"},
            {"label": "金へのβ（120日）",
             "text": f"{at(beta_sg):.2f}"},
        ],
        "WTI": [
            {"label": "EIA商業原油在庫",
             "text": (f"{inv_last / 1000:.1f}百万bbl"
                      f"（前週 {inv_w1 / 1000:+.1f}百万bbl）")
             if inv_last is not None and inv_w1 is not None else "取得待ち"},
            {"label": "ターム構造（期近−2番限）",
             "text": (f"{at(term_spread):+.2f}%"
                      f"（{'バックワーデーション' if at(term_spread) > 0 else 'コンタンゴ'}）")
             if not np.isnan(term_spread[last]) else "蓄積中"},
            {"label": "ブレント−WTI",
             "text": f"{at(brent) - at(px['WTI']):+.2f} USD"
             if not np.isnan(brent[last]) else "—"},
        ],
    }

    # --- 銘柄ごとの結果 ---
    hist_from = max(0, len(calendar) - 120)
    instruments = {}
    for sym in config.COMMODITIES:
        meta = config.COMMODITY_META[sym]
        weights = config.COMMODITY_PILLAR_WEIGHTS[sym]
        total = totals[sym]
        wsum_last = float(wsums[sym][last])

        # 時点 last で有効な柱と正規化後ウェイト
        active = {p: w for p, w in weights.items()
                  if not np.isnan(pillars[sym][p][last])}
        wn = {p: w / wsum_last for p, w in active.items()} if wsum_last > 1e-9 else {}

        sc = at(total)
        bias = commo_bias(sc)

        contribs = {p: wn[p] * at(pillars[sym][p]) for p in wn}
        if abs(sc) < 1e-9 or not wn:
            agreement = 0.0
        else:
            agreement = sum(wn[p] for p in contribs if contribs[p] * sc > 0)
        conviction = agreement * min(1.0, abs(sc) / config.COMMO_BIAS_STRONG)

        notes = []
        for p, w in weights.items():
            if p in active:
                continue
            got = 0
            if p == "term":
                got = _valid_count(term_spread)
            elif p == "inventory":
                got = _valid_count(inv_z)
            notes.append(
                f"「{config.COMMODITY_PILLAR_LABELS[p]}」は蓄積中"
                f"（有効データ {got}点、z化には約{MIN_SAMPLES}点必要）。"
                "残りの柱をウェイト再正規化して算出しています")

        drivers = sorted(contribs.items(), key=lambda kv: -abs(kv[1]))
        instruments[sym] = {
            "label": meta["label"],
            "unit": meta["unit"],
            "digits": meta["digits"],
            "score": round(sc, 1),
            "d1": round(sc - at(total, 1), 1),
            "d5": round(sc - at(total, 5), 1),
            "bias": bias,
            "bias_label": config.BIAS_LABELS[bias],
            "conviction": round(conviction, 2),
            "conviction_label": conviction_label(conviction),
            "price": round(at(px[sym]), meta["digits"]),
            "chg5": round(at(pct_change_n(px[sym], 5)) * 100, 2),
            "chg20": round(at(pct_change_n(px[sym], 20)) * 100, 2),
            "implemented_weight": round(wsum_last, 2),
            "pillars": {p: round(at(pillars[sym][p]), 1) for p in active},
            "pillar_weights": {p: round(w, 2) for p, w in weights.items()},
            "drivers": [
                {"pillar": p, "label": config.COMMODITY_PILLAR_LABELS[p],
                 "contrib": round(v, 1), "weight": round(weights[p], 2)}
                for p, v in drivers],
            "diagnostics": diagnostics[sym],
            "notes": notes,
            "history": [round(nan_safe(total[i]), 1)
                        for i in range(hist_from, len(calendar))],
            "price_history": [round(nan_safe(px[sym][i]), meta["digits"])
                              for i in range(hist_from, len(calendar))],
        }

    # --- リスク回避需要（3成分レジーム） ---
    regime_now = nan_safe(np.tanh(nan_safe(regime_mean[last]) / score.SQUASH))
    if regime_now >= 0.35:
        regime_label = "リスクオン"
    elif regime_now >= 0.1:
        regime_label = "ややリスクオン"
    elif regime_now > -0.1:
        regime_label = "中立"
    elif regime_now > -0.35:
        regime_label = "ややリスクオフ"
    else:
        regime_label = "リスクオフ"

    return {
        "as_of": calendar[last],
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bias_thresholds": {"strong": config.COMMO_BIAS_STRONG,
                            "mild": config.COMMO_BIAS_MILD},
        "pillar_labels": config.COMMODITY_PILLAR_LABELS,
        "pillar_order": list(config.COMMODITY_PILLAR_LABELS),
        "regime": {
            "value": round(regime_now, 2),
            "label": regime_label,
            "components": {k: round(nan_safe(v[last]), 2)
                           for k, v in regime_comps.items()},
            "note": "金/銅比は金スコアとの循環参照になるため除外した3成分",
        },
        "history_dates": calendar[hist_from:],
        "instruments": instruments,
        "_internal": {
            "calendar": calendar,
            "totals": {s: totals[s].tolist() for s in config.COMMODITIES},
            "price": {s: px[s].tolist() for s in config.COMMODITIES},
        },
    }


# ---------------------------------------------------------------------------
# バックテスト
# ---------------------------------------------------------------------------

def backtest(result: dict, horizons=(5, 10)) -> None:
    internal = result["_internal"]
    calendar = internal["calendar"]
    totals = {s: np.array(v) for s, v in internal["totals"].items()}
    price = {s: np.array(v) for s, v in internal["price"].items()}

    def fwd_returns(p, h):
        fwd = np.full(len(calendar), np.nan)
        if len(p) > h:
            with np.errstate(divide="ignore", invalid="ignore"):
                fwd[:-h] = np.where(p[:-h] != 0, p[h:] / p[:-h] - 1.0, np.nan)
        return fwd

    print(f"コモディティ・バックテスト  期間: {calendar[0]} 〜 {calendar[-1]}"
          f"  ({len(calendar)}営業日)\n")
    print(f"{'銘柄':<10}", end="")
    for h in horizons:
        print(f"{'翌' + str(h) + '営業日 的中率':>18}{'件数':>8}", end="")
    print()
    print("-" * (10 + 26 * len(horizons)))

    overall = {h: [0, 0] for h in horizons}
    for sym in config.COMMODITIES:
        print(f"{sym:<10}", end="")
        sc = totals[sym]
        for h in horizons:
            fwd = fwd_returns(price[sym], h)
            mask = (~np.isnan(fwd) & ~np.isnan(sc)
                    & (np.abs(sc) >= config.COMMO_BIAS_MILD))
            if mask.sum() == 0:
                print(f"{'-':>18}{0:>8}", end="")
                continue
            hits = int((np.sign(sc[mask]) == np.sign(fwd[mask])).sum())
            total = int(mask.sum())
            overall[h][0] += hits
            overall[h][1] += total
            print(f"{hits / total * 100:>17.1f}%{total:>8}", end="")
        print()

    print("-" * (10 + 26 * len(horizons)))
    print(f"{'全体':<10}", end="")
    for h in horizons:
        hits, total = overall[h]
        pct = hits / total * 100 if total else 0.0
        print(f"{pct:>17.1f}%{total:>8}", end="")
    print()

    print(f"\n閾値ごとの的中率と該当日割合（3銘柄合算）— 閾値較正用")
    print(f"{'閾値':<16}", end="")
    for h in horizons:
        print(f"{'翌' + str(h) + '営業日':>14}{'件数':>8}", end="")
    print(f"{'該当日割合':>12}")
    n_days = sum(int(np.sum(~np.isnan(totals[s]))) for s in config.COMMODITIES)
    for threshold in (0, 10, 20, 30, 40, 50):
        name = "閾値なし" if threshold == 0 else f"|スコア|>={threshold}"
        mark = "  ←現行" if threshold == config.COMMO_BIAS_MILD else ""
        print(f"{name + mark:<16}", end="")
        share_n = 0
        for h in horizons:
            hits = total = 0
            for sym in config.COMMODITIES:
                sc = totals[sym]
                fwd = fwd_returns(price[sym], h)
                mask = ~np.isnan(fwd) & ~np.isnan(sc) & (np.abs(sc) >= threshold)
                if mask.sum():
                    hits += int((np.sign(sc[mask]) == np.sign(fwd[mask])).sum())
                    total += int(mask.sum())
            pct = hits / total * 100 if total else 0.0
            print(f"{pct:>13.1f}%{total:>8}", end="")
        for sym in config.COMMODITIES:
            sc = totals[sym]
            share_n += int(np.sum(~np.isnan(sc) & (np.abs(sc) >= threshold)))
        share = share_n / n_days * 100 if n_days else 0.0
        print(f"{share:>11.1f}%")

    print(f"\n※ 銘柄別の表は |スコア| < {config.COMMO_BIAS_MILD} の中立ゾーンを判定から除外しています")
    print("※ 該当日割合がほぼ0%なら閾値の下げ、80%超なら上げを検討してください")
    print("※ ターム構造・在庫の柱はデータ蓄積前の期間 NaN のため、履歴の前半は")
    print("   実質的に少ない柱で計算されたスコアです（ウェイト再正規化）")
    print("※ 銘柄は3つしかなく金と銀の相関が高いため、FX 7ペアより実質サンプルは")
    print("   少なめです。参考値として扱ってください")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="コモディティのスコアと方向性を計算する")
    parser.add_argument("--backtest", action="store_true",
                        help="スコア符号 vs 翌5/10営業日リターンの的中率を表示")
    parser.add_argument("--quiet", action="store_true", help="サマリを表示しない")
    args = parser.parse_args()

    if not os.path.exists(DATA_PATH):
        print("data.json がありません。先に fetch_market.py を実行してください。",
              file=sys.stderr)
        return 1

    data = load_data()
    result = compute(data)

    if args.backtest:
        backtest(result)
        return 0

    if not args.quiet:
        print(f"基準日: {result['as_of']}   リスク回避需要: "
              f"{result['regime']['label']} ({result['regime']['value']:+.2f})\n")
        print("コモディティ方向性:")
        for sym in config.COMMODITIES:
            i = result["instruments"][sym]
            print(f"  {sym:<8} {i['bias_label']:<6} スコア {i['score']:>6.1f}  "
                  f"確信度 {i['conviction_label']}({i['conviction']:.2f})  "
                  f"実装ウェイト {i['implemented_weight']:.2f}")
            for d in i["drivers"][:3]:
                print(f"           {d['label']:<8} {d['contrib']:>6.1f}")
        print()

    payload = dict(data)
    commodities = dict(result)
    commodities.pop("_internal", None)
    payload["commodities"] = commodities
    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print("data.json に commodities を書き込みました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
