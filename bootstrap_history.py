"""初回のみ実行する履歴ブートストラップ.

BOE は日次CIで使う軽量zip（当月分・約400KB）しか返さないため、
z-score に必要な252営業日分が溜まるまで数か月かかってしまう。
そこで全履歴アーカイブ（約39MB）から一度だけ過去分を流し込む。

2回目以降は fetch_market.py が当月分を data.json にマージしていくので、
このスクリプトを再実行する必要はない。

    python bootstrap_history.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import config
import sources
from fetch_market import DATA_PATH, dict_to_series, load_previous, series_to_dict

JOBS = [
    ("gbp2y", "英2年スポット（全履歴）", lambda: sources.fetch_boe_2y_history()),
]


def main() -> int:
    previous = load_previous(DATA_PATH)
    if not previous:
        print("data.json がありません。先に fetch_market.py を実行してください。", file=sys.stderr)
        return 1

    series = previous.setdefault("series", {})
    changed = False

    for key, label, fetcher in JOBS:
        before = series_to_dict(series.get(key))
        print(f"{label} を取得します（数十MBのダウンロードと解析で数分かかります）...", flush=True)
        t0 = time.time()
        try:
            fetched = fetcher()
        except Exception as exc:  # noqa: BLE001
            print(f"  失敗: {str(exc)[:200]}", file=sys.stderr)
            continue

        merged = sources.merge_series(before, fetched)
        merged = sources.trim_series(merged, config.KEEP_DAYS)
        series[key] = dict_to_series(merged)
        changed = True
        print(f"  {len(before)}点 → {len(merged)}点 "
              f"({sources.last_date(merged)} まで, {time.time() - t0:.0f}秒)", flush=True)

    if not changed:
        print("更新はありませんでした。")
        return 1

    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump(previous, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"\ndata.json を更新しました ({os.path.getsize(DATA_PATH) / 1024:.0f} KB)")
    print("続けて `python score.py` を実行してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
