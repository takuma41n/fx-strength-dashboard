"""narrative.json の検査.

Claude が生成した見立てが、ダッシュボードが期待する形になっているかを確認する。
特に `view` の意味（機械スコアとの差分であって、機械と同じ意見を up と書かない）は
間違えるとダッシュボードの「食い違い表示」が無意味になるため、機械的に検査する。

    python validate_narrative.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import config

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWS = {"up", "down", "neutral"}


def main() -> int:
    npath = os.path.join(HERE, "narrative.json")
    dpath = os.path.join(HERE, "data.json")

    if not os.path.exists(npath):
        print("narrative.json がありません。/fx-brief で生成してください。", file=sys.stderr)
        return 1

    with open(npath, encoding="utf-8") as fh:
        n = json.load(fh)
    with open(dpath, encoding="utf-8") as fh:
        scores = json.load(fh)["scores"]

    errors, warnings = [], []

    for key in ("as_of", "generated_at", "based_on", "summary", "currencies", "watch"):
        if key not in n:
            errors.append(f"必須キー `{key}` がない")
    if errors:
        for e in errors:
            print(f"  NG  {e}", file=sys.stderr)
        return 1

    if not str(n["summary"]).strip():
        errors.append("summary が空")

    if n["based_on"] != scores["as_of"]:
        errors.append(f"基準日が data.json と一致しない: "
                      f"narrative={n['based_on']} / data={scores['as_of']}")

    have = set(n["currencies"])
    want = set(config.CURRENCIES)
    if have != want:
        errors.append(f"通貨が揃っていない: 不足={sorted(want - have)} 余分={sorted(have - want)}")

    for ccy, info in n["currencies"].items():
        if info.get("view") not in VIEWS:
            errors.append(f"{ccy}: view が不正 ({info.get('view')!r})。{sorted(VIEWS)} のいずれか")
        if not str(info.get("note", "")).strip():
            errors.append(f"{ccy}: note が空")

    for i, w in enumerate(n["watch"]):
        for key in ("date", "ccy", "event"):
            if key not in w:
                errors.append(f"watch[{i}]: `{key}` がない")
        if w.get("ccy") not in config.CURRENCIES:
            errors.append(f"watch[{i}]: 通貨が不正 ({w.get('ccy')!r})")
        try:
            dt.date.fromisoformat(w["date"])
        except (ValueError, KeyError):
            errors.append(f"watch[{i}]: date が YYYY-MM-DD 形式でない ({w.get('date')!r})")

    dates = [w["date"] for w in n["watch"] if "date" in w]
    if dates != sorted(dates):
        warnings.append("watch が日付順に並んでいない")
    if len(n["watch"]) > 6:
        warnings.append(f"watch が {len(n['watch'])} 件。6件までに絞ると読みやすい")

    # view が全通貨 neutral なのは正常（機械スコアに補正すべき材料がない状態）。
    # 逆に全通貨に view が付いているのは、機械との差分ではなく感想を書いている疑い。
    non_neutral = [c for c, i in n["currencies"].items() if i.get("view") != "neutral"]
    if len(non_neutral) == len(want):
        warnings.append("全通貨に補正が付いている。view は機械スコアとの『差分』なので、"
                        "同意見の通貨は neutral が正しい")

    for e in errors:
        print(f"  NG  {e}", file=sys.stderr)
    for w in warnings:
        print(f"  警告 {w}")

    if errors:
        return 1

    print(f"  OK  基準日 {n['based_on']} ／ 補正あり {len(non_neutral)}通貨"
          f"（{', '.join(non_neutral) or 'なし'}）／ 注目イベント {len(n['watch'])}件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
