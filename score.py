"""スコアリング層 — 柱別スコア → 6通貨スコア → 7ペアの方向性.

設計の要点:
  * すべて「水準」ではなく「変化」で評価する（スイング時間軸のため）
  * 各柱は通貨横断で平均0に揃える。為替は相対ゲームなので、
    全通貨の金利が同時に上がっても誰も強くならない
  * スコアの大きさとは別に「柱の符号一致度」を確信度として出す
  * 時系列全体でスコアを計算するので、前日比・前週比の矢印と
    バックテストが同じコードで賄える

    python score.py             # data.json にスコアを書き込む
    python score.py --backtest  # スコア符号 vs 翌5/10営業日リターンの的中率
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import warnings

import numpy as np

import config

# 系列の立ち上がり期間は全通貨が NaN になることがある。想定内なので黙らせる。
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Mean of empty slice")
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Degrees of freedom <= 0")
warnings.filterwarnings("ignore", category=RuntimeWarning, message="All-NaN slice encountered")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data.json")

# z-score を ±100 に潰すときの緩さ。1.5 で z=1.5 → 76、z=3 → 99。
SQUASH = 1.5
# ローリング統計に必要な最低サンプル数
MIN_SAMPLES = 30


# ---------------------------------------------------------------------------
# 系列ユーティリティ
# ---------------------------------------------------------------------------

def load_data(path: str = DATA_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def as_map(entry) -> dict:
    if not entry:
        return {}
    return dict(zip(entry.get("dates", []), entry.get("values", [])))


def align(series_map: dict, calendar: list) -> np.ndarray:
    """カレンダーに前方補完で貼り付ける。日本・英国・豪州で祝日が
    違うため、素朴に結合すると欠損だらけになる。"""
    out = np.full(len(calendar), np.nan)
    if not series_map:
        return out
    keys = sorted(series_map)
    idx = 0
    last = np.nan
    for i, date in enumerate(calendar):
        while idx < len(keys) and keys[idx] <= date:
            value = series_map[keys[idx]]
            if value is not None:
                last = float(value)
            idx += 1
        out[i] = last
    return out


def diff_n(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    if len(x) > n:
        out[n:] = x[n:] - x[:-n]
    return out


def pct_change_n(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    if len(x) > n:
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n:] = np.where(x[:-n] != 0, x[n:] / x[:-n] - 1.0, np.nan)
    return out


def rolling_z(x: np.ndarray, window: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        if np.isnan(x[i]):
            continue
        w = x[max(0, i - window + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) < MIN_SAMPLES:
            continue
        sd = w.std(ddof=1)
        if sd > 1e-12:
            out[i] = (x[i] - w.mean()) / sd
    return out


def rolling_pct_rank(x: np.ndarray, window: int) -> np.ndarray:
    """直近 window 期間内での順位（0=最安値、1=最高値）。"""
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        if np.isnan(x[i]):
            continue
        w = x[max(0, i - window + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) < MIN_SAMPLES:
            continue
        out[i] = float((w <= x[i]).sum() - 1) / max(1, len(w) - 1)
    return out


def squash(z: np.ndarray, scale: float = SQUASH) -> np.ndarray:
    """z-score を ±100 に圧縮。外れ値がスコアを支配しないようにする。"""
    return 100.0 * np.tanh(np.asarray(z, dtype=float) / scale)


def demean_across(matrix: dict) -> dict:
    """通貨横断で平均0に揃える。為替は相対ゲームなので、
    全通貨が同時に強くなることはありえない。"""
    keys = list(matrix)
    stacked = np.vstack([matrix[k] for k in keys])
    mean = np.nanmean(stacked, axis=0)
    return {k: matrix[k] - mean for k in keys}


def nan_safe(value, default=0.0):
    if value is None:
        return default
    v = float(value)
    return default if (math.isnan(v) or math.isinf(v)) else v


# ---------------------------------------------------------------------------
# 通貨インデックス
# ---------------------------------------------------------------------------

def build_currency_indices(fx: dict, calendar: list) -> dict:
    """USD を軸にした5本のレートから、6通貨それぞれの実効インデックスを作る。

    u_X = log(USD 1単位 = ? 通貨X) と置くと、通貨Y のインデックスは
        idx_Y = mean_{X != Y} ( u_X - u_Y )      （u_USD = 0）
    となり、定義から6通貨の合計はゼロになる。
    """
    u = {"USD": np.zeros(len(calendar))}
    for ccy, (pair, invert) in config.USD_LEGS.items():
        arr = fx.get(pair)
        if arr is None:
            u[ccy] = np.full(len(calendar), np.nan)
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(arr > 0, (1.0 / arr) if invert else arr, np.nan)
            u[ccy] = np.log(np.where(rate > 0, rate, np.nan))

    indices = {}
    others = config.CURRENCIES
    for y in others:
        stacked = np.vstack([u[x] - u[y] for x in others if x != y])
        indices[y] = np.nanmean(stacked, axis=0)
    return indices


# ---------------------------------------------------------------------------
# 柱① 金利・政策期待
# ---------------------------------------------------------------------------

def pillar_rates(rates: dict) -> tuple[dict, dict]:
    scores, detail = {}, {}
    for ccy in config.CURRENCIES:
        arr = rates[ccy]
        z5 = rolling_z(diff_n(arr, 5), config.Z_LOOKBACK)
        z20 = rolling_z(diff_n(arr, 20), config.Z_LOOKBACK)
        z60 = rolling_z(diff_n(arr, 60), config.Z_LOOKBACK)
        w = config.RATE_SUB_WEIGHTS
        combined = (
            w["d5"] * np.nan_to_num(z5)
            + w["d20"] * np.nan_to_num(z20)
            + w["d60"] * np.nan_to_num(z60)
        )
        # 3成分すべて欠損なら NaN のまま残す
        valid = ~(np.isnan(z5) & np.isnan(z20) & np.isnan(z60))
        combined = np.where(valid, combined, np.nan)
        scores[ccy] = squash(combined)
        detail[ccy] = {"d5": diff_n(arr, 5), "d20": diff_n(arr, 20), "d60": diff_n(arr, 60)}
    return demean_across(scores), detail


# ---------------------------------------------------------------------------
# 柱④ リスクレジーム
# ---------------------------------------------------------------------------

def risk_regime(series: dict, calendar: list) -> tuple[np.ndarray, dict]:
    """+1 に近いほどリスクオン、-1 に近いほどリスクオフ。"""
    comps = {}

    vix = align(as_map(series.get("vix")), calendar)
    comps["VIX"] = -rolling_z(vix, config.Z_LOOKBACK)

    hy = align(as_map(series.get("hy_oas")), calendar)
    comps["HYスプレッド"] = -rolling_z(hy, config.Z_LOOKBACK)

    spx = align(as_map(series.get("eq_USD")), calendar)
    comps["株モメンタム"] = rolling_z(pct_change_n(spx, 20), config.Z_LOOKBACK)

    gold = align(as_map(series.get("gold")), calendar)
    copper = align(as_map(series.get("copper")), calendar)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(copper > 0, gold / copper, np.nan)
    comps["金/銅比"] = -rolling_z(diff_n(ratio, 20), config.Z_LOOKBACK)

    stacked = np.vstack([comps[k] for k in comps])
    mean = np.nanmean(stacked, axis=0)
    regime = np.tanh(np.nan_to_num(mean) / SQUASH)
    return regime, comps


def rolling_beta(y: np.ndarray, x: np.ndarray, window: int) -> np.ndarray:
    """y の x に対する感応度をローリング回帰で推定する。
    リスクβを固定値にすると、レジーム転換に追随できないため。"""
    out = np.full_like(y, np.nan)
    for i in range(len(y)):
        lo = max(0, i - window + 1)
        yy, xx = y[lo:i + 1], x[lo:i + 1]
        mask = ~(np.isnan(yy) | np.isnan(xx))
        if mask.sum() < MIN_SAMPLES:
            continue
        yv, xv = yy[mask], xx[mask]
        var = xv.var(ddof=1)
        if var > 1e-18:
            out[i] = float(np.cov(yv, xv, ddof=1)[0, 1] / var)
    return out


def pillar_risk(indices: dict, series: dict, calendar: list,
                regime: np.ndarray) -> tuple[dict, dict]:
    spx = align(as_map(series.get("eq_USD")), calendar)
    with np.errstate(divide="ignore", invalid="ignore"):
        eq_ret = np.diff(np.log(np.where(spx > 0, spx, np.nan)), prepend=np.nan)

    betas = {}
    for ccy in config.CURRENCIES:
        idx = indices[ccy]
        ccy_ret = np.diff(idx, prepend=np.nan)
        beta = rolling_beta(ccy_ret, eq_ret, config.BETA_LOOKBACK)
        # 推定不能な期間は事前値で埋める（絶対値ではなく相対順位だけ使うので、
        # あとで通貨横断の標準化を通せばスケールの差は吸収される）
        prior = config.RISK_BETA_PRIOR[ccy]
        beta = np.where(np.isnan(beta), np.nan, beta)
        betas[ccy] = (beta, prior)

    # 通貨横断で標準化（共通成分は方向性を生まないので取り除く）
    stacked = np.vstack([betas[c][0] for c in config.CURRENCIES])
    mean = np.nanmean(stacked, axis=0)
    sd = np.nanstd(stacked, axis=0, ddof=1)
    prior_vals = np.array([config.RISK_BETA_PRIOR[c] for c in config.CURRENCIES])
    prior_z = (prior_vals - prior_vals.mean()) / prior_vals.std(ddof=1)

    scores, beta_out = {}, {}
    for i, ccy in enumerate(config.CURRENCIES):
        beta = betas[ccy][0]
        with np.errstate(invalid="ignore"):
            bz = np.where(sd > 1e-9, (beta - mean) / sd, np.nan)
        bz = np.where(np.isnan(bz), prior_z[i], bz)
        scores[ccy] = squash(regime * bz)
        beta_out[ccy] = beta
    return demean_across(scores), beta_out


# ---------------------------------------------------------------------------
# 柱⑤ 固有要因
# ---------------------------------------------------------------------------

def pillar_idio(series: dict, fx: dict, calendar: list) -> tuple[dict, dict]:
    n = len(calendar)
    scores = {c: np.zeros(n) for c in config.CURRENCIES}
    notes = {c: "" for c in config.CURRENCIES}

    # AUD: 銅と中国株。資源国通貨の代表的なドライバ。
    copper = align(as_map(series.get("copper")), calendar)
    china = align(as_map(series.get("china")), calendar)
    z_copper = rolling_z(pct_change_n(copper, 20), config.Z_LOOKBACK)
    z_china = rolling_z(pct_change_n(china, 20), config.Z_LOOKBACK)
    scores["AUD"] = squash(0.6 * np.nan_to_num(z_copper) + 0.4 * np.nan_to_num(z_china))
    notes["AUD"] = "銅・中国株"

    # JPY: 原油。日本は輸入国なので原油高は円にとってマイナス。
    oil = align(as_map(series.get("oil")), calendar)
    z_oil = rolling_z(pct_change_n(oil, 20), config.Z_LOOKBACK)
    scores["JPY"] = squash(-np.nan_to_num(z_oil))
    notes["JPY"] = "原油(輸入国)"

    # CHF: EURCHF が過去1年のレンジ下限に近いほど SNB の介入警戒が高まり、
    #      フラン高が抑えられやすい。
    eurusd, usdchf = fx.get("EURUSD"), fx.get("USDCHF")
    if eurusd is not None and usdchf is not None:
        eurchf = eurusd * usdchf
        pct = rolling_pct_rank(eurchf, config.Z_LOOKBACK)
        scores["CHF"] = np.where(np.isnan(pct), 0.0, 100.0 * (2.0 * pct - 1.0))
        notes["CHF"] = "SNB介入警戒(EURCHF水準)"

    return demean_across(scores), notes


# ---------------------------------------------------------------------------
# 統合
# ---------------------------------------------------------------------------

def combine(pillars: dict) -> np.ndarray:
    weights = {p: config.PILLAR_WEIGHTS_FULL[p] for p in config.PHASE1_PILLARS}
    total = sum(weights.values())
    acc = None
    for name, w in weights.items():
        term = np.nan_to_num(pillars[name]) * (w / total)
        acc = term if acc is None else acc + term
    return acc


def bias_from_score(diff: float) -> int:
    if diff >= config.BIAS_STRONG:
        return 2
    if diff >= config.BIAS_MILD:
        return 1
    if diff <= -config.BIAS_STRONG:
        return -2
    if diff <= -config.BIAS_MILD:
        return -1
    return 0


def conviction_label(value: float) -> str:
    if value >= 0.65:
        return "高"
    if value >= 0.35:
        return "中"
    return "低"


def compute(data: dict) -> dict:
    series = data.get("series", {})

    # カレンダーは為替系列（最も欠損が少ない）から作る
    cal_set: set = set()
    for pair in config.PAIR_NAMES:
        cal_set |= set(as_map(series.get(f"fx_{pair}")))
    calendar = sorted(cal_set)
    if len(calendar) < 80:
        raise SystemExit(f"為替系列が不足しています（{len(calendar)}日分）。先に fetch_market.py を実行してください。")

    fx = {p: align(as_map(series.get(f"fx_{p}")), calendar) for p in config.PAIR_NAMES}
    rates = {c: align(as_map(series.get(config.RATE_SOURCES[c]["key"])), calendar)
             for c in config.CURRENCIES}

    indices = build_currency_indices(fx, calendar)
    regime, regime_comps = risk_regime(series, calendar)

    p_rates, rate_detail = pillar_rates(rates)
    p_risk, betas = pillar_risk(indices, series, calendar, regime)
    p_idio, idio_notes = pillar_idio(series, fx, calendar)

    pillars = {"rates": p_rates, "risk": p_risk, "idio": p_idio}
    totals = {c: combine({k: v[c] for k, v in pillars.items()}) for c in config.CURRENCIES}
    totals = demean_across(totals)

    last = len(calendar) - 1

    def at(arr, offset=0):
        i = last - offset
        return nan_safe(arr[i]) if 0 <= i < len(arr) else 0.0

    # --- 通貨ごとの結果 ---
    currencies = {}
    for ccy in config.CURRENCIES:
        meta = config.RATE_SOURCES[ccy]
        currencies[ccy] = {
            "score": round(at(totals[ccy]), 1),
            "d1": round(at(totals[ccy]) - at(totals[ccy], 1), 1),
            "d5": round(at(totals[ccy]) - at(totals[ccy], 5), 1),
            "pillars": {k: round(at(v[ccy]), 1) for k, v in pillars.items()},
            "beta": round(at(betas[ccy]), 3),
            "rate": {
                "label": meta["label"],
                "tenor": meta["tenor"],
                "proxy": meta["proxy"],
                "value": round(at(rates[ccy]), 3),
                "d5": round(at(rate_detail[ccy]["d5"]) * 100, 1),    # bp
                "d20": round(at(rate_detail[ccy]["d20"]) * 100, 1),  # bp
            },
            "idio_note": idio_notes[ccy],
        }

    # --- ペアごとの結果 ---
    pillar_w = {p: config.PILLAR_WEIGHTS_FULL[p] for p in config.PHASE1_PILLARS}
    hist_len = min(120, len(calendar))
    hist_from = len(calendar) - hist_len
    pairs = {}
    for name, base, quote in config.PAIRS:
        diff_now = at(totals[base]) - at(totals[quote])
        bias = bias_from_score(diff_now)

        contribs = {}
        for p in config.PHASE1_PILLARS:
            contribs[p] = at(pillars[p][base]) - at(pillars[p][quote])

        # 確信度 = 柱の符号一致度 × スコアの大きさ
        if abs(diff_now) < 1e-9:
            agreement = 0.0
        else:
            matched = sum(pillar_w[p] for p in contribs
                          if contribs[p] * diff_now > 0)
            agreement = matched / sum(pillar_w.values())
        conviction = agreement * min(1.0, abs(diff_now) / config.BIAS_STRONG)

        # 金利差 vs 為替の乖離
        rate_diff = rates[base] - rates[quote]
        z_rate = rolling_z(diff_n(rate_diff, 20), config.Z_LOOKBACK)
        z_fx = rolling_z(pct_change_n(fx[name], 20), config.Z_LOOKBACK)
        div_series = z_fx - z_rate
        divergence = at(z_fx) - at(z_rate)

        drivers = sorted(contribs.items(), key=lambda kv: -abs(kv[1]))
        pairs[name] = {
            "base": base,
            "quote": quote,
            "score": round(diff_now, 1),
            "bias": bias,
            "bias_label": config.BIAS_LABELS[bias],
            "conviction": round(conviction, 2),
            "conviction_label": conviction_label(conviction),
            "price": round(at(fx[name]), 5),
            "chg5": round(at(pct_change_n(fx[name], 5)) * 100, 2),
            "chg20": round(at(pct_change_n(fx[name], 20)) * 100, 2),
            "rate_diff": round(at(rate_diff), 3),
            "rate_diff_d20": round(at(diff_n(rate_diff, 20)) * 100, 1),  # bp
            "divergence": round(divergence, 2),
            "divergence_series": [round(nan_safe(div_series[i]), 2)
                                  for i in range(hist_from, len(calendar))],
            "drivers": [{"pillar": p, "label": config.PILLAR_LABELS[p], "contrib": round(v, 1)}
                        for p, v in drivers],
            "proxy_rate": config.RATE_SOURCES[base]["proxy"] or config.RATE_SOURCES[quote]["proxy"],
        }

    # --- 履歴（矢印・チャート用） ---
    tail = calendar[-120:]
    offset = len(calendar) - len(tail)
    history = {"dates": tail}
    for ccy in config.CURRENCIES:
        history[ccy] = [round(nan_safe(totals[ccy][offset + i]), 1) for i in range(len(tail))]

    # --- 改定の可視化 ---
    # 金利系列は数日遅れて届くため、carry-forward した値の上を d5/d20/d60 の窓が
    # 滑る。その結果、既に発表した日付のスコアが後から書き換わる。黙って起きると
    # 「あの日の見立て」が何に対する見立てだったのか追えなくなるので、差分を出す。
    snapshots = data.get("score_snapshots", {})
    revisions = {}
    for i, date in enumerate(tail):
        snap = snapshots.get(date)
        if not snap:
            continue
        deltas = {c: round(nan_safe(totals[c][offset + i]) - snap[c], 1)
                  for c in config.CURRENCIES if c in snap}
        if deltas and any(abs(v) >= 0.1 for v in deltas.values()):
            revisions[date] = deltas
    revised_max = max((abs(v) for d in revisions.values() for v in d.values()), default=0.0)

    regime_now = nan_safe(regime[last])
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

    implemented = sum(config.PILLAR_WEIGHTS_FULL[p] for p in config.PHASE1_PILLARS)
    return {
        "as_of": calendar[last],
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "implemented_weight": round(implemented, 2),
        "pillar_weights": {p: config.PILLAR_WEIGHTS_FULL[p] for p in config.PHASE1_PILLARS},
        "pillar_labels": config.PILLAR_LABELS,
        "regime": {
            "value": round(regime_now, 2),
            "label": regime_label,
            "components": {k: round(nan_safe(v[last]), 2) for k, v in regime_comps.items()},
        },
        "currencies": currencies,
        "pairs": pairs,
        "history": history,
        # 通貨ごとの金利系列の鮮度。金利柱は重み最大なので、ここが古い通貨の
        # スコアは「観測ゼロで窓だけ動いた」結果である可能性がある。
        "rate_freshness": {
            ccy: {
                "last_date": (data.get("sources", {})
                              .get(config.RATE_SOURCES[ccy]["key"], {}).get("last_date")),
                "stale_days": (data.get("sources", {})
                               .get(config.RATE_SOURCES[ccy]["key"], {}).get("stale_days")),
                "stale": bool((data.get("sources", {})
                               .get(config.RATE_SOURCES[ccy]["key"], {}) or {}).get("stale")),
            }
            for ccy in config.CURRENCIES
        },
        "revisions": {"by_date": revisions, "dates": len(revisions),
                      "max_abs": round(revised_max, 1)},
        "_internal": {"calendar": calendar, "totals": {c: totals[c].tolist() for c in config.CURRENCIES},
                      "fx": {p: fx[p].tolist() for p in config.PAIR_NAMES},
                      # --backtest 用。柱を個別に評価できないと、重みが直感のままになる。
                      "pillars": {name: {c: arr[c].tolist() for c in config.CURRENCIES}
                                  for name, arr in pillars.items()},
                      "indices": {c: indices[c].tolist() for c in config.CURRENCIES}},
    }


# ---------------------------------------------------------------------------
# バックテスト
# ---------------------------------------------------------------------------

def forward_return(price: np.ndarray, horizon: int) -> np.ndarray:
    """horizon 営業日先までの単純リターン。末尾 horizon 本は NaN。"""
    fwd = np.full(len(price), np.nan)
    if len(price) > horizon:
        with np.errstate(divide="ignore", invalid="ignore"):
            fwd[:-horizon] = np.where(price[:-horizon] != 0,
                                      price[horizon:] / price[:-horizon] - 1.0, np.nan)
    return fwd


def pair_hit_rate(totals: dict, fx: dict, horizon: int, threshold: float,
                  lo: int = 0, hi: int | None = None) -> tuple[int, int]:
    """全ペア合算の的中数と件数。lo/hi でカレンダーの窓を切る。"""
    hits = total = 0
    for pair, base, quote in config.PAIRS:
        score = totals[base] - totals[quote]
        fwd = forward_return(fx[pair], horizon)
        mask = ~np.isnan(fwd) & ~np.isnan(score) & (np.abs(score) >= threshold)
        mask[:lo] = False
        if hi is not None:
            mask[hi:] = False
        if mask.sum():
            hits += int((np.sign(score[mask]) == np.sign(fwd[mask])).sum())
            total += int(mask.sum())
    return hits, total


def combine_weighted(pillars: dict, weights: dict) -> dict:
    """combine() と同じ式を、任意の重みで通貨ごとに適用する。"""
    total_w = sum(weights.values())
    out = {}
    for ccy in config.CURRENCIES:
        acc = None
        for name, w in weights.items():
            term = np.nan_to_num(np.asarray(pillars[name][ccy], dtype=float)) * (w / total_w)
            acc = term if acc is None else acc + term
        out[ccy] = acc
    return demean_across(out)


def backtest_windows(totals: dict, fx: dict, calendar: list, horizons, recent: int = 120) -> None:
    """全期間の平均は、直近の劣化を覆い隠す。窓を割って出す。"""
    n = len(calendar)
    split = max(0, n - recent)
    print(f"\n窓別の的中率（|スコア差|>={config.BIAS_MILD}、全ペア合算）— 劣化の検知用")
    print(f"{'窓':<22}", end="")
    for h in horizons:
        print(f"{'翌' + str(h) + '営業日':>14}{'件数':>8}", end="")
    print()
    windows = [("全期間", 0, None),
               (f"直近{recent}営業日", split, None),
               (f"それ以前({split}営業日)", 0, split)]
    for label, lo, hi in windows:
        if lo >= (hi if hi is not None else n):
            continue
        print(f"{label:<22}", end="")
        for h in horizons:
            hits, total = pair_hit_rate(totals, fx, h, config.BIAS_MILD, lo, hi)
            pct = hits / total * 100 if total else float("nan")
            print(f"{pct:>13.1f}%{total:>8}", end="")
        print()

    block = 60
    h0 = horizons[0]
    print(f"\n{block}営業日ブロックごとの推移（翌{h0}営業日、|スコア差|>={config.BIAS_MILD}）")
    # 末尾に揃える。直近ブロックが切り落とされると、劣化の検知という目的を果たさない。
    for start in range(n % block, n - block + 1, block):
        hits, total = pair_hit_rate(totals, fx, h0, config.BIAS_MILD, start, start + block)
        if not total:
            continue
        pct = hits / total * 100
        bar = "#" * int(round(pct / 5))
        print(f"  {calendar[start]} 〜 {calendar[start + block - 1]}  "
              f"{pct:>5.1f}%  (n={total:>3})  {bar}")


def backtest_pillars(pillars: dict, indices: dict, totals: dict, horizons=(5, 10, 20)) -> None:
    """柱ごとの予測力。重みを直感で決めないための材料。"""
    print("\n柱別の予測力 — スコアと『対バスケット超過リターン』の先行相関")
    print("（6通貨をプールして計測。上位/下位20%は超過リターンの平均）")
    print(f"{'柱':<14}{'重み':>6}", end="")
    for h in horizons:
        print(f"{'先' + str(h) + '日 相関':>12}{'上位20%':>10}{'下位20%':>10}", end="")
    print()

    series = {"総合スコア": (totals, None)}
    for name in config.PHASE1_PILLARS:
        series[config.PILLAR_LABELS[name]] = (pillars[name], config.PILLAR_WEIGHTS_FULL[name])

    for label, (sig_map, weight) in series.items():
        print(f"{label:<14}{('—' if weight is None else f'{weight:.2f}'):>6}", end="")
        for h in horizons:
            xs, ys = [], []
            for ccy in config.CURRENCIES:
                sig = np.asarray(sig_map[ccy], dtype=float)
                idx = np.asarray(indices[ccy], dtype=float)
                fwd = np.full(len(idx), np.nan)
                if len(idx) > h:
                    fwd[:-h] = 100.0 * (idx[h:] - idx[:-h])
                mask = ~(np.isnan(sig) | np.isnan(fwd))
                xs.append(sig[mask])
                ys.append(fwd[mask])
            x, y = np.concatenate(xs), np.concatenate(ys)
            if len(x) < 50:
                print(f"{'-':>12}{'-':>10}{'-':>10}", end="")
                continue
            corr = float(np.corrcoef(x, y)[0, 1])
            hi = y[x >= np.percentile(x, 80)].mean()
            lo = y[x <= np.percentile(x, 20)].mean()
            print(f"{corr:>+12.3f}{hi:>+9.3f}%{lo:>+9.3f}%", end="")
        print()
    print("※ 相関の符号と重みが噛み合っていないなら、重みは測定で見直す余地があります")


def backtest_weights(pillars: dict, fx: dict, horizons) -> None:
    """重み感応度。ここでは重みを変更しない — 変更判断のための材料だけ出す。"""
    current = tuple(config.PILLAR_WEIGHTS_FULL[p] for p in ("rates", "risk", "idio"))
    candidates = [current, (0.40, 0.00, 0.08), (0.40, 0.10, 0.25),
                  (0.33, 0.33, 0.33), (1.00, 0.00, 0.00), (0.00, 0.00, 1.00)]
    seen, uniq = set(), []
    for cand in candidates:
        if cand not in seen and sum(cand) > 0:
            seen.add(cand)
            uniq.append(cand)

    print(f"\n重み感応度（|スコア差|>={config.BIAS_MILD}、全ペア合算）— 変更はしていません")
    print(f"{'rates/risk/idio':<20}", end="")
    for h in horizons:
        print(f"{'翌' + str(h) + '営業日':>14}{'件数':>8}", end="")
    print()
    for wr, wk, wi in uniq:
        weights = {"rates": wr, "risk": wk, "idio": wi}
        totals_w = combine_weighted(pillars, weights)
        mark = "  ←現行" if (wr, wk, wi) == current else ""
        print(f"{f'{wr:.2f}/{wk:.2f}/{wi:.2f}' + mark:<20}", end="")
        for h in horizons:
            hits, total = pair_hit_rate(totals_w, fx, h, config.BIAS_MILD)
            pct = hits / total * 100 if total else float("nan")
            print(f"{pct:>13.1f}%{total:>8}", end="")
        print()
    print("※ 件数が重みごとに変わるのは、閾値を超えるシグナル数が変わるためです")
    print("※ 差が数pt程度なら、この標本では優劣は判定できません。重みは据え置きが既定です")


def backtest(result: dict, horizons=(5, 10)) -> None:
    internal = result["_internal"]
    calendar = internal["calendar"]
    totals = {c: np.array(v) for c, v in internal["totals"].items()}
    fx = {p: np.array(v) for p, v in internal["fx"].items()}
    pillars = {name: {c: np.array(v) for c, v in arrs.items()}
               for name, arrs in internal.get("pillars", {}).items()}
    indices = {c: np.array(v) for c, v in internal.get("indices", {}).items()}

    print(f"バックテスト  期間: {calendar[0]} 〜 {calendar[-1]}  ({len(calendar)}営業日)\n")
    print(f"{'ペア':<10}", end="")
    for h in horizons:
        print(f"{'翌' + str(h) + '営業日 的中率':>18}{'件数':>8}", end="")
    print()
    print("-" * (10 + 26 * len(horizons)))

    overall = {h: [0, 0] for h in horizons}
    for name, base, quote in config.PAIRS:
        print(f"{name:<10}", end="")
        for h in horizons:
            score = totals[base] - totals[quote]
            fwd = np.full(len(calendar), np.nan)
            price = fx[name]
            if len(price) > h:
                with np.errstate(divide="ignore", invalid="ignore"):
                    fwd[:-h] = np.where(price[:-h] != 0, price[h:] / price[:-h] - 1.0, np.nan)
            # 中立ゾーンは判定から除く（シグナルとして出していないため）
            mask = ~np.isnan(fwd) & ~np.isnan(score) & (np.abs(score) >= config.BIAS_MILD)
            if mask.sum() == 0:
                print(f"{'-':>18}{0:>8}", end="")
                continue
            hits = int((np.sign(score[mask]) == np.sign(fwd[mask])).sum())
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
    print("-" * (10 + 26 * len(horizons)))
    print(f"\n閾値ごとの的中率（全ペア合算）— 閾値の較正根拠")
    print(f"{'閾値':<14}", end="")
    for h in horizons:
        print(f"{'翌' + str(h) + '営業日':>14}{'件数':>8}", end="")
    print()
    for threshold in (0, 15, 30, 45, 60):
        name = "閾値なし" if threshold == 0 else f"|スコア差|>={threshold}"
        mark = "  ←現行" if threshold == config.BIAS_MILD else ""
        print(f"{name + mark:<14}", end="")
        for h in horizons:
            hits = total = 0
            for pair, base, quote in config.PAIRS:
                sc = totals[base] - totals[quote]
                price = fx[pair]
                fwd = np.full(len(calendar), np.nan)
                if len(price) > h:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        fwd[:-h] = np.where(price[:-h] != 0, price[h:] / price[:-h] - 1.0, np.nan)
                mask = ~np.isnan(fwd) & ~np.isnan(sc) & (np.abs(sc) >= threshold)
                if mask.sum():
                    hits += int((np.sign(sc[mask]) == np.sign(fwd[mask])).sum())
                    total += int(mask.sum())
            pct = hits / total * 100 if total else 0.0
            print(f"{pct:>13.1f}%{total:>8}", end="")
        print()

    print(f"\n※ 上表は |スコア差| < {config.BIAS_MILD} の中立ゾーンを判定から除外しています")
    print("※ 的中率がスコアの強さと単調に上がることが、中立ゾーンを広く取る根拠です")

    backtest_windows(totals, fx, calendar, horizons)
    if pillars and indices:
        backtest_pillars(pillars, indices, totals)
        backtest_weights(pillars, fx, horizons)

    print("\n※ 日次サンプルは重複が大きく（10日保有なら実質独立は約1/10）、")
    print("   件数の少ない行は統計的に有意とまでは言えません。参考値として扱ってください")


def backfill_snapshots(existing: dict) -> dict:
    """git 履歴の data.json から『発表時スコア』を復元する。

    改定の可視化は本来この先の分から積み上がるが、履歴に発表当時の data.json が
    そのまま残っているので、過去分はそこから取れる。同じ日付が複数回出てくる場合は
    最も古いコミット（＝最初に発表した値）を採用する。
    """
    import subprocess

    try:
        out = subprocess.run(["git", "log", "--format=%H", "--", "data.json"],
                             cwd=HERE, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"git 履歴を読めませんでした: {exc}", file=sys.stderr)
        return existing

    snapshots = dict(existing)
    added = 0
    for sha in reversed(out.stdout.split()):  # 古い順。最初の発表値を優先する
        try:
            blob = subprocess.run(["git", "show", f"{sha}:data.json"],
                                  cwd=HERE, capture_output=True, text=True, check=True)
            scores = json.loads(blob.stdout).get("scores", {})
        except (subprocess.CalledProcessError, json.JSONDecodeError, MemoryError):
            continue
        as_of = scores.get("as_of")
        currencies = scores.get("currencies") or {}
        if not as_of or as_of in snapshots:
            continue
        snap = {c: currencies[c]["score"] for c in config.CURRENCIES if c in currencies}
        if len(snap) == len(config.CURRENCIES):
            snapshots[as_of] = snap
            added += 1
    print(f"発表時スコアを {added} 日分 git 履歴から復元しました（計 {len(snapshots)} 日）")
    return snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description="通貨スコアとペア方向性を計算する")
    parser.add_argument("--backtest", action="store_true",
                        help="スコア符号 vs 翌5/10営業日リターンの的中率を表示")
    parser.add_argument("--backfill-snapshots", action="store_true",
                        help="git 履歴から発表時スコアを復元して data.json に保存する")
    parser.add_argument("--quiet", action="store_true", help="サマリを表示しない")
    args = parser.parse_args()

    if not os.path.exists(DATA_PATH):
        print("data.json がありません。先に fetch_market.py を実行してください。", file=sys.stderr)
        return 1

    data = load_data()
    if args.backfill_snapshots:
        data["score_snapshots"] = backfill_snapshots(data.get("score_snapshots", {}))

    result = compute(data)

    if args.backtest:
        backtest(result)
        return 0

    if not args.quiet:
        print(f"基準日: {result['as_of']}   リスクレジーム: "
              f"{result['regime']['label']} ({result['regime']['value']:+.2f})\n")
        print("通貨強弱:")
        ranked = sorted(result["currencies"].items(), key=lambda kv: -kv[1]["score"])
        for ccy, info in ranked:
            bar = "#" * int(abs(info["score"]) / 3)
            print(f"  {ccy}  {info['score']:>6.1f}  (前日比 {info['d1']:+.1f} / 前週比 {info['d5']:+.1f})  {bar}")
        print("\nペア方向性:")
        for name, _, _ in config.PAIRS:
            p = result["pairs"][name]
            print(f"  {name:<8} {p['bias_label']:<6} スコア差 {p['score']:>6.1f}  "
                  f"確信度 {p['conviction_label']}({p['conviction']:.2f})  "
                  f"乖離 {p['divergence']:+.2f}")

    payload = dict(data)
    scores = dict(result)
    scores.pop("_internal", None)
    payload["scores"] = scores

    # 発表時スコアを残す。同じ日付は最初の1回だけ記録し、後から上書きしない。
    # これが無いと「発表後に何点動いたか」を誰も検証できない。
    snapshots = dict(data.get("score_snapshots", {}))
    snapshots.setdefault(result["as_of"],
                         {c: result["currencies"][c]["score"] for c in config.CURRENCIES})
    for old_date in sorted(snapshots)[:-200]:
        snapshots.pop(old_date)
    payload["score_snapshots"] = snapshots

    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"\ndata.json にスコアを書き込みました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
