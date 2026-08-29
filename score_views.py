"""narrative.json の「見立て」を事後採点する.

`view` は「機械スコアとの差分」として定義されているので、的中には2つの別の意味がある。

  価格成績   … 対バスケット超過リターンの符号一致率。**取引価値はこちらにしかない**
  スコア成績 … 機械スコアの変化の符号一致率。モデルの改定を当てただけかもしれない

この2つが食い違うコールは要注意で、たとえば「金利系列が止まっているので、
観測が届けば機械が自分で直す」という理由で付けた補正は、スコア成績だけが上がる。
相場ではなくデータ到着を予測していたことになり、取引には使えない。

見立てが機械に価値を足しているかを測る仕組みが他にないため、このスクリプトが唯一の
検証手段になる。過去分は git 履歴の narrative.json から復元する。

    python score_views.py                # 既定 (+5 / +10 営業日)
    python score_views.py --horizons 5 10 20
    python score_views.py --detail       # コード1本ずつの明細も出す
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import numpy as np

import config
from score import align, as_map, build_currency_indices

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data.json")
NARRATIVE = "narrative.json"
SIGN = {"up": 1, "down": -1, "neutral": 0}


def git_narratives() -> list[dict]:
    """git 履歴から narrative.json を全て復元する。based_on ごとに最新の1本を採用。"""
    try:
        out = subprocess.run(["git", "log", "--format=%H", "--", NARRATIVE],
                             cwd=HERE, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"git 履歴を読めませんでした: {exc}", file=sys.stderr)
        return []

    by_base: dict[str, dict] = {}
    for sha in out.stdout.split():
        try:
            blob = subprocess.run(["git", "show", f"{sha}:{NARRATIVE}"],
                                  cwd=HERE, capture_output=True, text=True, check=True)
            doc = json.loads(blob.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        base = doc.get("based_on")
        if not base or "currencies" not in doc:
            continue
        # git log は新しい順。最初に見つかったものが、その based_on の最終版。
        by_base.setdefault(base, doc)

    return [by_base[k] for k in sorted(by_base)]


def load_context() -> tuple[list, dict, dict, dict]:
    """カレンダー・対バスケット指数・機械スコア履歴を用意する。"""
    with open(DATA_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    series = data.get("series", {})

    cal_set: set = set()
    for pair in config.PAIR_NAMES:
        cal_set |= set(as_map(series.get(f"fx_{pair}")))
    calendar = sorted(cal_set)
    fx = {p: align(as_map(series.get(f"fx_{p}")), calendar) for p in config.PAIR_NAMES}
    indices = build_currency_indices(fx, calendar)

    hist = data.get("scores", {}).get("history", {})
    hist_pos = {d: i for i, d in enumerate(hist.get("dates", []))}
    return calendar, indices, hist, hist_pos


def collect(narratives: list[dict], calendar: list, indices: dict,
            hist: dict, hist_pos: dict, horizons: list[int]) -> list[dict]:
    cal_pos = {d: i for i, d in enumerate(calendar)}
    rows = []
    for doc in narratives:
        base = doc["based_on"]
        i_cal = cal_pos.get(base)
        i_hist = hist_pos.get(base)
        for ccy, info in doc["currencies"].items():
            view = info.get("view")
            if view not in SIGN or SIGN[view] == 0 or ccy not in config.CURRENCIES:
                continue
            row = {"as_of": doc.get("as_of", base), "based_on": base, "ccy": ccy,
                   "view": view, "sign": SIGN[view], "price": {}, "score": {}}
            for h in horizons:
                if i_cal is not None and i_cal + h < len(calendar):
                    idx = np.asarray(indices[ccy], dtype=float)
                    val = 100.0 * (idx[i_cal + h] - idx[i_cal])
                    if not np.isnan(val):
                        row["price"][h] = float(val)
                if i_hist is not None and ccy in hist and i_hist + h < len(hist[ccy]):
                    row["score"][h] = float(hist[ccy][i_hist + h] - hist[ccy][i_hist])
            rows.append(row)
    return rows


def summarise(rows: list[dict], horizons: list[int]) -> None:
    print(f"{'先行':<8}{'件数':>6}{'価格的中':>10}{'平均超過':>11}"
          f"{'スコア的中':>12}{'両者一致':>10}")
    for h in horizons:
        pr = [(r["sign"], r["price"][h]) for r in rows if h in r["price"]]
        sc = [(r["sign"], r["score"][h]) for r in rows if h in r["score"]]
        both = [r for r in rows if h in r["price"] and h in r["score"]]
        if not pr:
            continue
        p_hit = sum(1 for s, v in pr if s * v > 0) / len(pr)
        p_avg = float(np.mean([s * v for s, v in pr]))
        s_hit = sum(1 for s, v in sc if s * v > 0) / len(sc) if sc else float("nan")
        agree = (sum(1 for r in both
                     if (r["sign"] * r["price"][h] > 0) == (r["sign"] * r["score"][h] > 0))
                 / len(both)) if both else float("nan")
        print(f"+{h:<7}{len(pr):>6}{p_hit * 100:>9.0f}%{p_avg:>+10.3f}%"
              f"{s_hit * 100:>11.0f}%{agree * 100:>9.0f}%")


def flag_divergent(rows: list[dict], horizon: int) -> None:
    """価格とスコアで判定が割れたコール = モデルを当てて市場を外した（またはその逆）。"""
    bad = [r for r in rows
           if horizon in r["price"] and horizon in r["score"]
           and (r["sign"] * r["price"][horizon] > 0) != (r["sign"] * r["score"][horizon] > 0)]
    if not bad:
        return
    print(f"\n価格とスコアで判定が割れたコール（+{horizon}営業日）")
    print("  スコアだけ当たっている＝データ到着を予測しただけの疑い")
    for r in bad:
        p, s = r["price"][horizon], r["score"][horizon]
        verdict = "モデルに勝ち市場に負け" if r["sign"] * s > 0 else "市場に勝ちモデルに負け"
        print(f"  {r['based_on']} {r['ccy']:<4}{r['view']:<6}"
              f"価格{r['sign'] * p:>+7.2f}%  スコア{r['sign'] * s:>+7.1f}  {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="narrative.json の見立てを事後採点する")
    parser.add_argument("--horizons", type=int, nargs="+", default=[5, 10],
                        help="先行営業日数（既定 5 10）")
    parser.add_argument("--detail", action="store_true", help="コード1本ずつの明細も出す")
    args = parser.parse_args()

    if not os.path.exists(DATA_PATH):
        print("data.json がありません。先に fetch_market.py を実行してください。", file=sys.stderr)
        return 1

    narratives = git_narratives()
    if not narratives:
        print("採点できる narrative がありません。", file=sys.stderr)
        return 1

    calendar, indices, hist, hist_pos = load_context()
    horizons = sorted(set(args.horizons))
    rows = collect(narratives, calendar, indices, hist, hist_pos, horizons)

    print(f"見立ての事後採点  narrative {len(narratives)}本 "
          f"({narratives[0]['based_on']} 〜 {narratives[-1]['based_on']})")
    print(f"非中立コール {len(rows)}本\n")
    if not rows:
        print("非中立のコールがありません。")
        return 0

    summarise(rows, horizons)
    flag_divergent(rows, horizons[0])

    print("\n通貨別（価格成績、+%d営業日）" % horizons[0])
    h = horizons[0]
    for ccy in config.CURRENCIES:
        sub = [r for r in rows if r["ccy"] == ccy and h in r["price"]]
        if not sub:
            continue
        hit = sum(1 for r in sub if r["sign"] * r["price"][h] > 0) / len(sub)
        avg = float(np.mean([r["sign"] * r["price"][h] for r in sub]))
        print(f"  {ccy}  n={len(sub):<3} 的中{hit * 100:>3.0f}%  平均超過{avg:>+7.3f}%")

    if args.detail:
        print("\n明細")
        for r in rows:
            cells = "  ".join(
                f"+{h}: 価格{r['sign'] * r['price'][h]:>+6.2f}% / スコア{r['sign'] * r['score'].get(h, float('nan')):>+6.1f}"
                for h in horizons if h in r["price"])
            print(f"  {r['based_on']} {r['ccy']:<4}{r['view']:<6}{cells}")

    n = len([r for r in rows if horizons[0] in r["price"]])
    print(f"\n※ 的中率の標準誤差はおよそ {50 / max(n, 1) ** 0.5:.0f}pt（n={n}）。"
          "50%との差がこれ未満なら、判定できていません")
    print("※ 価格成績とスコア成績が乖離しているうちは、見立てが相場ではなく"
          "モデルの改定を予測している疑いがあります")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
