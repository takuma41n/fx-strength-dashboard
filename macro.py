"""ドル・ユーロ・金・銀・WTI — 今どのドライバーが効いているか.

固定ウェイトを持たないのがこの設計の核心。設計時に決めた重みで押し通すと、
関係が変わったときに気づけない。

実例として、金と米10年実質金利の相関（教科書的に最も確実とされる逆相関）は、
2019-21年に −0.77 まで効いていたが 2024-11 に +0.05、2026-02 に +0.09 まで
消えたあと、2026-08 には −0.61 に戻っている。**壊れて終わりではなく往復する。**
23年平均の固定ウェイトは、この2年間ずっと外し続けたことになる。

そこで毎回「今の効き方」をローリング窓で測り直し、
  ・安定して効いている
  ・効かなくなった（いつからかを明示）
  ・新しく効き始めた
に仕分ける。過去データは重みを決めるためではなく、変化を検知する基準線として使う。

    python macro.py --sensitivity   # 各ドライバーの効き方と変化点
    python macro.py --split         # 測り方を変えても結論が変わらないかを確認

時間軸は2〜3ヶ月。短期の値動きは意図的に見ない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "macro_history.json")

# 変化を測る期間（週）。使う人の時間軸が2〜3ヶ月なので、日次や週次の
# 値動きではなく月単位の動きに対する感応度を測る。1週だと高頻度の同時変動を
# 拾ってしまい、13週だと重複が大きく推定が甘くなるため、その中間を既定にする。
HORIZON = 4

# ローリング窓（週）。104週=2年。4週変化と組み合わせて独立標本は約26個。
# 短いと推定が暴れ、長いと変化の検知が遅れる。--split で頑健性を見る。
WINDOW = 104

# 効いている/いないの境目。|0.25| はその1本で分散の6%強を説明する水準。
STRONG = 0.25
DEAD = 0.12
# 過去に「一貫して効いていた」と言うための、符号が揃っていた窓の割合
CONSISTENT = 0.70

# 状態が変わったと認めるのに必要な連続週数。これが無いと当週の揺れで
# 「効き始めた」と言ってしまい、排除したいはずの短期ノイズを持ち込む。
# 8週=約2ヶ月で、使う人の時間軸と揃えている。
PERSIST = 8


# ---------------------------------------------------------------------------
# 対象とドライバー候補
#
# 重みは持たない。ここにあるのは「測る対象」の一覧であって、寄与の大きさは
# 毎回データから測る。候補は事前に固定する（毎週選び直すと多重検定になるため）。
# ---------------------------------------------------------------------------

def spread(a: str, b: str):
    """2系列の差を作る（金利差やブレント−WTIなど）。"""
    return lambda s: {d: s[a][d] - s[b][d] for d in s[a] if d in s[b]}


TARGETS = {
    "usd": {
        "label": "ドル",
        "series": "dxy_broad",
        "kind": "logret",
        "drivers": [
            ("us_real10y", "実質金利", "diff"),
            ("us_be10y", "期待インフレ", "diff"),
            ("us10y", "名目金利", "diff"),
            ("ff", "政策金利", "diff"),
            ("baa10y", "信用スプレッド", "diff"),
            ("vix", "リスク回避", "logret"),
            ("cot_eur_pct", "建玉(ユーロ先物の裏)", "diff"),
            ("btc", "ドル代替(BTC)", "logret"),
        ],
    },
    "eur": {
        "label": "ユーロ",
        "series": "eurusd",
        "kind": "logret",
        "drivers": [
            ("_ez_us_10y", "独米金利差", "diff", spread("ez10y", "us10y")),
            ("ecb_depo", "ECB政策金利", "diff"),
            ("us_real10y", "米実質金利", "diff"),
            ("cot_eur_pct", "建玉", "diff"),
            ("vix", "リスク回避", "logret"),
            ("baa10y", "信用スプレッド", "diff"),
        ],
    },
    "gold": {
        "label": "ゴールド",
        "series": "gold",
        "kind": "logret",
        "drivers": [
            ("us_real10y", "実質金利", "diff"),
            ("dxy_broad", "ドル", "logret"),
            ("us_be10y", "期待インフレ", "diff"),
            ("cot_gold_pct", "建玉", "diff"),
            ("vix", "リスク回避", "logret"),
            ("baa10y", "信用スプレッド", "diff"),
            ("us10y", "名目金利", "diff"),
        ],
    },
    "silver": {
        "label": "シルバー",
        "series": "silver",
        "kind": "logret",
        "drivers": [
            ("gold", "金連動", "logret"),
            ("copper", "工業需要(銅)", "logret"),
            ("us_real10y", "実質金利", "diff"),
            ("dxy_broad", "ドル", "logret"),
            ("cot_silver_pct", "建玉", "diff"),
        ],
    },
    "wti": {
        "label": "WTI",
        "series": "wti",
        "kind": "logret",
        "drivers": [
            ("eia_crude_stocks", "米在庫", "logret"),
            ("_brent_wti", "ブレント差(現物需給)", "diff", spread("brent", "wti")),
            ("dxy_broad", "ドル", "logret"),
            ("copper", "世界需要(銅)", "logret"),
            ("cot_wti_pct", "建玉", "diff"),
            ("vix", "リスク回避", "logret"),
        ],
    },
}


# ---------------------------------------------------------------------------
# データ整形
# ---------------------------------------------------------------------------

def load_series() -> dict:
    if not os.path.exists(HISTORY_PATH):
        raise SystemExit("macro_history.json がありません。"
                         "先に python macro_history.py を実行してください。")
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)["series"]
    return {k: dict(zip(v["dates"], v["values"])) for k, v in raw.items()}


def weekly_grid(series: dict) -> list[str]:
    """全系列の日付から、ISO週ごとの最終営業日を拾って週次の軸を作る。"""
    weeks: dict[tuple, str] = {}
    for values in series.values():
        for date in values:
            iso = dt.date.fromisoformat(date).isocalendar()[:2]
            if iso not in weeks or date > weeks[iso]:
                weeks[iso] = date
    return [weeks[k] for k in sorted(weeks)]


def on_grid(values: dict, grid: list[str]) -> list[float | None]:
    """週次軸に前方補完で載せる。月次のCPIや週次の在庫もこれで揃う。"""
    dates = sorted(values)
    out, i, last = [], 0, None
    for cutoff in grid:
        while i < len(dates) and dates[i] <= cutoff:
            last = values[dates[i]]
            i += 1
        out.append(last)
    return out


def changes(level: list, kind: str, weeks: int) -> list[float | None]:
    """指定した週数ぶんの変化に直す。

    水準どうしの相関は見せかけになるので必ず変化で測る。週数を使う人の
    時間軸に合わせるのが要点で、ここを1週にすると高頻度の同時変動を拾う。
    """
    out: list[float | None] = [None] * weeks
    for i in range(weeks, len(level)):
        a, b = level[i - weeks], level[i]
        if a is None or b is None:
            out.append(None)
        elif kind == "logret":
            out.append(math.log(b / a) if a > 0 and b > 0 else None)
        else:
            out.append(b - a)
    return out


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------

def corr(xs: list, ys: list) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 20:
        return None
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    num = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    dx = sum((p[0] - mx) ** 2 for p in pairs)
    dy = sum((p[1] - my) ** 2 for p in pairs)
    return num / math.sqrt(dx * dy) if dx > 0 and dy > 0 else None


def rolling_corr(xs: list, ys: list, window: int) -> list[float | None]:
    return [None if i < window else corr(xs[i - window:i], ys[i - window:i])
            for i in range(len(xs))]


def median(vals: list) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def ols_r2(y: list, xs: list[list]) -> float | None:
    """重回帰の決定係数。説明できない残差を出すために使う。"""
    rows = [i for i in range(len(y))
            if y[i] is not None and all(x[i] is not None for x in xs)]
    k = len(xs)
    if len(rows) < k + 10:
        return None
    Y = [y[i] for i in rows]
    X = [[1.0] + [x[i] for x in xs] for i in rows]
    m = k + 1
    # 正規方程式 (X'X)b = X'Y をガウス消去で解く
    xtx = [[sum(X[r][a] * X[r][b] for r in range(len(rows))) for b in range(m)]
           for a in range(m)]
    xty = [sum(X[r][a] * Y[r] for r in range(len(rows))) for a in range(m)]
    for c in range(m):
        piv = max(range(c, m), key=lambda r: abs(xtx[r][c]))
        if abs(xtx[piv][c]) < 1e-12:
            return None
        xtx[c], xtx[piv] = xtx[piv], xtx[c]
        xty[c], xty[piv] = xty[piv], xty[c]
        for r in range(m):
            if r == c:
                continue
            f = xtx[r][c] / xtx[c][c]
            for cc in range(c, m):
                xtx[r][cc] -= f * xtx[c][cc]
            xty[r] -= f * xty[c]
    beta = [xty[r] / xtx[r][r] for r in range(m)]
    my = sum(Y) / len(Y)
    sst = sum((v - my) ** 2 for v in Y)
    sse = sum((Y[r] - sum(beta[a] * X[r][a] for a in range(m))) ** 2
              for r in range(len(rows)))
    return 1 - sse / sst if sst > 0 else None


def debounce(flags: list[bool], persist: int) -> list[bool]:
    """連続 persist 週そろって初めて状態を切り替える。

    これが無いと当週の揺れで「効き始めた」と言ってしまい、
    排除したいはずの短期ノイズをそのまま画面に持ち込むことになる。
    """
    out, state, run = [], flags[0], 0
    for flag in flags:
        if flag == state:
            run = 0
        else:
            run += 1
            if run >= persist:
                state, run = flag, 0
        out.append(state)
    return out


# ---------------------------------------------------------------------------
# 仕分け: 安定 / 壊れた / 新規 / 弱い
# ---------------------------------------------------------------------------

def classify(roll: list, grid: list[str]) -> dict:
    hist = [(i, c) for i, c in enumerate(roll) if c is not None]
    if len(hist) < 30:
        return {"state": "データ不足", "current": None}

    current = hist[-1][1]
    past = [c for _, c in hist[:-1]]
    med = median(past)
    sign = 1 if med >= 0 else -1
    share = sum(1 for c in past if (c >= 0) == (med >= 0)) / len(past)
    was_strong = abs(med) >= STRONG and share >= CONSISTENT

    # 「効いている」の生の判定を作り、持続性でならしてから状態を決める
    raw = [abs(c) >= STRONG and (c >= 0) == (sign >= 0) for _, c in hist]
    smooth = debounce(raw, PERSIST)
    now_on = smooth[-1]

    since = None
    for pos in range(len(smooth) - 1, -1, -1):
        if smooth[pos] != now_on:
            since = grid[hist[pos + 1][0]]
            break

    if was_strong and now_on:
        state = "安定して効いている"
        since = None                      # 続いているので変化点は出さない
    elif was_strong and not now_on:
        state = ("効かなくなった" if abs(current) < DEAD
                 or (current >= 0) != (sign >= 0) else "弱まっている")
    elif not was_strong and now_on:
        state = "新しく効き始めた"
    else:
        state = "もともと弱い"
        since = None

    return {"state": state, "current": current, "hist_median": med,
            "consistency": share, "since": since, "sign": sign,
            "active": now_on}


def analyse(window: int = WINDOW, horizon: int = HORIZON) -> dict:
    series = load_series()
    grid = weekly_grid(series)
    out = {}

    for name, spec in TARGETS.items():
        level = on_grid(series[spec["series"]], grid)
        y = changes(level, spec["kind"], horizon)

        drivers, active_cols = [], []
        for drv in spec["drivers"]:
            key, label, kind = drv[0], drv[1], drv[2]
            raw = drv[3](series) if len(drv) > 3 else series.get(key)
            if not raw:
                continue
            x = changes(on_grid(raw, grid), kind, horizon)
            info = classify(rolling_corr(x, y, window), grid)
            info.update({"key": key, "label": label})
            drivers.append(info)
            if info.get("active"):
                active_cols.append(x[-window:])

        r2 = ols_r2(y[-window:], active_cols) if active_cols else None
        out[name] = {"label": spec["label"], "drivers": drivers,
                     "r2": r2, "as_of": grid[-1]}
    return out


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

ORDER = {"安定して効いている": 0, "新しく効き始めた": 1, "弱まっている": 2,
         "効かなくなった": 3, "もともと弱い": 4, "データ不足": 5}


def report(result: dict) -> None:
    print(f"変化を測る期間 {HORIZON}週 ／ ローリング窓 {WINDOW}週 "
          f"／ 状態変化に必要な連続週数 {PERSIST}週")
    for r in result.values():
        print(f"\n■ {r['label']}   基準週 {r['as_of']}")
        rows = sorted(r["drivers"], key=lambda d: (ORDER.get(d["state"], 9),
                                                   -abs(d["current"] or 0)))
        print(f"  {'ドライバー':<22}{'今':>7}{'過去中央':>9}{'一貫性':>8}  状態")
        for d in rows:
            cur = f"{d['current']:+.2f}" if d["current"] is not None else "  —  "
            med = (f"{d['hist_median']:+.2f}"
                   if d.get("hist_median") is not None else "  —  ")
            con = (f"{d['consistency']*100:.0f}%"
                   if d.get("consistency") is not None else " — ")
            note = f"  ← {d['since']} から" if d.get("since") else ""
            print(f"  {d['label']:<22}{cur:>7}{med:>9}{con:>8}  "
                  f"{d['state']}{note}")
        if r["r2"] is not None:
            print(f"  効いているドライバーで説明できる割合 {r['r2']*100:.0f}%"
                  f" ／ 説明できない部分 {(1-r['r2'])*100:.0f}%")
        else:
            print("  効いているドライバーが無いため、説明できる部分はゼロ")


def split_report() -> None:
    """測り方を変えても結論が変わらないかを見る。変わるなら信用できない。"""
    combos = [(4, 104), (1, 52), (4, 52), (13, 156), (13, 104)]
    print("変化期間と窓の組み合わせを変えて、符号が入れ替わらないかを見る。")
    print("組み合わせ: " + " / ".join(f"{h}週変化×{w}週窓" for h, w in combos))
    results = {c: analyse(c[1], c[0]) for c in combos}
    base = results[combos[0]]
    for name in TARGETS:
        print(f"\n■ {base[name]['label']}")
        head = "".join(f"{h}x{w}".rjust(9) for h, w in combos)
        print(f"  {'ドライバー':<22}{head}   符号一致")
        for i, d in enumerate(base[name]["drivers"]):
            cells, signs = "", []
            for combo in combos:
                c = results[combo][name]["drivers"][i]["current"]
                cells += (f"{c:+.2f}".rjust(9) if c is not None
                          else "—".rjust(9))
                if c is not None:
                    signs.append(c >= 0)
            agree = "○" if len(set(signs)) <= 1 else "×"
            print(f"  {d['label']:<22}{cells}   {agree}")


# ---------------------------------------------------------------------------
# 予測力の検定
#
# ここまでの相関は「今の値動きが何で説明されるか」という同時の関係で、
# 「2〜3ヶ月先がどちらを向くか」とは別物。方向を断定するには、
# ドライバーの現在の状態が **先の** リターンと関係することを示す必要がある。
#
# 重複標本で t値を出すと必ず有意に見えてしまうので、13週おきの
# 非重複標本だけで判定する。さらに前半・後半で符号が一致することを求める。
# 全期間の集計値だけを見て採用したのが過去の失敗（乖離シグナルは全標本で
# シャープ0.79だったが、前半+7.36%/後半−3.54%と反転していた）。
# ---------------------------------------------------------------------------

FWD = 13          # 先行させる週数（約3ヶ月）
PCT_LOOKBACK = 156  # 水準パーセンタイルの参照期間（3年）


def pct_rank(level: list, lookback: int) -> list[float | None]:
    """その時点までの lookback 週の中での順位（0〜1）。水準の割高割安。"""
    out: list[float | None] = []
    for i, v in enumerate(level):
        if v is None or i < lookback:
            out.append(None)
            continue
        past = [x for x in level[i - lookback:i + 1] if x is not None]
        out.append(sum(1 for x in past if x <= v) / len(past) if past else None)
    return out


def t_stat(xs: list, ys: list) -> tuple:
    r = corr(xs, ys)
    n = sum(1 for x, y in zip(xs, ys) if x is not None and y is not None)
    if r is None or n < 8 or abs(r) >= 1:
        return r, None, n
    return r, r * math.sqrt(n - 2) / math.sqrt(1 - r * r), n


def forecast_report() -> None:
    series = load_series()
    grid = weekly_grid(series)
    print(f"ドライバーの現在の状態が {FWD}週先(約3ヶ月)のリターンと関係するかを検定する。")
    print(f"重複を避けて{FWD}週おきの非重複標本のみ使用。前半後半で符号が一致することを要求。\n")

    for name, spec in TARGETS.items():
        level = on_grid(series[spec["series"]], grid)
        # 先行リターン: t から t+FWD
        fwd: list[float | None] = []
        for i in range(len(level)):
            a = level[i]
            b = level[i + FWD] if i + FWD < len(level) else None
            fwd.append(math.log(b / a) if a and b and a > 0 and b > 0 else None)

        print(f"■ {spec['label']}")
        print(f"  {'ドライバー':<22}{'測り方':<12}{'相関':>7}{'t値':>7}"
              f"{'標本':>6}{'前半':>6}{'後半':>6}  判定")

        # 対象自身のモメンタムと割高割安。数ヶ月horizonで最も実証が厚い効果なので、
        # 外部ドライバーだけを試して「何も効かない」と結論するのは不当。
        mom52 = changes(level, spec["kind"], 52)
        mom4 = changes(level, spec["kind"], 4)
        candidates = [
            ("(自身)", "モメンタム26週", changes(level, spec["kind"], 26)),
            ("(自身)", "モメンタム52週", mom52),
            ("(自身)", "モメンタム52-4週",
             [None if a is None or b is None else a - b
              for a, b in zip(mom52, mom4)]),
            ("(自身)", "割高割安3年", pct_rank(level, PCT_LOOKBACK)),
        ]
        for drv in spec["drivers"]:
            key, label = drv[0], drv[1]
            raw = drv[3](series) if len(drv) > 3 else series.get(key)
            if not raw:
                continue
            dlevel = on_grid(raw, grid)
            candidates.append((label, "水準の位置",
                               pct_rank(dlevel, PCT_LOOKBACK)))
            candidates.append((label, f"{FWD}週の変化",
                               changes(dlevel, drv[2], FWD)))

        for label, vlabel, x in candidates:
            idx = [i for i in range(0, len(grid), FWD)
                   if x[i] is not None and fwd[i] is not None]
            if len(idx) < 20:
                continue
            xs = [x[i] for i in idx]
            ys = [fwd[i] for i in idx]
            r, t, n = t_stat(xs, ys)
            half = len(idx) // 2
            r1 = corr(xs[:half], ys[:half])
            r2 = corr(xs[half:], ys[half:])
            agree = (r1 is not None and r2 is not None
                     and (r1 >= 0) == (r2 >= 0) and (r1 >= 0) == (r >= 0))
            verdict = ("採用" if agree and t is not None and abs(t) >= 2.0
                       else "不採用")

            def fmt(v):
                return f"{v:+.2f}" if v is not None else "  — "

            print(f"  {label:<22}{vlabel:<12}{fmt(r):>7}"
                  f"{(f'{t:+.1f}' if t else '  — '):>7}{n:>6}"
                  f"{fmt(r1):>6}{fmt(r2):>6}  {verdict}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sensitivity", action="store_true",
                        help="各ドライバーの効き方と変化点を表示")
    parser.add_argument("--split", action="store_true",
                        help="測り方への頑健性を確認")
    parser.add_argument("--forecast", action="store_true",
                        help="2〜3ヶ月先への予測力を検定")
    args = parser.parse_args()

    if args.split:
        split_report()
    elif args.forecast:
        forecast_report()
    elif args.sensitivity:
        report(analyse())
    else:
        print("いまは --sensitivity / --split のみ。"
              "画面用の macro.json 生成は Stage 3 で追加する。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
