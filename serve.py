"""ローカル確認用の静的サーバ.

GitHub Pages と同じ「静的ファイルを配信するだけ」の構成で動作を確認する。
fetch() は file:// では CORS に阻まれるため、ローカルでも HTTP で配信する必要がある。

    python serve.py            # http://localhost:8000
    python serve.py --port 9000
    python serve.py --refresh  # 起動前に fetch_market.py と score.py を実行
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import subprocess
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # data.json を編集しながら確認するのでキャッシュを無効化する
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "GET / " in (fmt % args) or ".json" in (fmt % args):
            sys.stderr.write("  %s\n" % (fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description="ダッシュボードをローカル配信する")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--refresh", action="store_true",
                        help="起動前にデータ取得とスコア計算を実行する")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.refresh:
        for script in ("fetch_market.py", "score.py"):
            print(f"$ python {script}", flush=True)
            rc = subprocess.call([sys.executable, os.path.join(HERE, script)], cwd=HERE)
            if rc != 0:
                print(f"{script} が失敗しました (exit {rc})", file=sys.stderr)
                return rc

    if not os.path.exists(os.path.join(HERE, "data.json")):
        print("data.json がありません。先に `python fetch_market.py && python score.py` を実行してください。",
              file=sys.stderr)
        return 1

    # ブラウザは index.html / data.json / narrative.json を並列に取りに来るので、
    # シングルスレッドのサーバだと keep-alive 接続で待たされて固まる。
    handler = functools.partial(Handler, directory=HERE)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("", args.port), handler) as httpd:
        url = f"http://localhost:{args.port}/"
        print(f"配信中: {url}   (Ctrl+C で停止)")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - WSL 等でブラウザが開けなくても続行
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n停止しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
