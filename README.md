# FX通貨強弱・方向性ダッシュボード

USD / EUR / JPY / GBP / AUD / CHF の6通貨をファンダメンタル面からスコア化し、
メイン7ペア（USDJPY / EURJPY / GBPJPY / EURUSD / GBPUSD / AUDUSD / USDCHF）の
方向性を示すダッシュボード。エントリーの細部ではなく、**全体のファンダの向き**を出すことが目的。

対象7ペアはこの6通貨で閉じるので、通貨スコアが決まればペアの方向は自動的に決まる。

時間軸は**スイング（数日〜2週間）**。データはすべて**無料・APIキー不要**。

## 設計の中核

### 1. 「水準」ではなく「変化」で見る
数日〜2週間で為替を動かすのは金利の絶対水準ではなく変化。全指標を「今いくつか」ではなく
「直近どれだけ動いたか」で評価し、直近1年の z-score に変換してから ±100 に圧縮する。

### 2. サプライズは2年金利の反応で代理する
無料ソースでは市場コンセンサス（予想値）が取れない。そこで**指標発表日の金利の変化そのものを
「顕示されたサプライズ」として使う**。市場が驚いた量は、予想値を推測するより金利の反応を
見るほうが速くて正確。

### 3. スコアは相対値
各柱は6通貨の平均が必ずゼロになるよう揃える。全通貨の金利が同時に上がっても誰も強くならないため。

### 4. 確信度はスコアと別軸
5本柱すべてが同方向を向いたシグナルと、1本の柱だけで押し切っているシグナルは意味が違う。
確信度 = 柱の符号一致度 × スコアの大きさ。

## スコア設計

| 柱 | ウェイト | 中身 | 状態 |
|---|---|---|---|
| ① 金利・政策期待 | 40% | 2年金利の Δ5日 / Δ20日 / Δ60日 | 実装済み（日次） |
| ④ リスクレジーム | 25% | レジーム指標 × ローリング推定したリスクβ | 実装済み（日次） |
| ⑤ 固有要因 | 8% | AUD: 銅・中国株 ／ JPY: 原油 ／ CHF: SNB介入警戒 | 実装済み（日次） |
| ③ 景気・雇用 | 15% | 失業率トレンド、雇用、生産 | **Phase 2** |
| ② インフレ | 12% | コアCPI の YoY 変化、3m年率 − YoY | **Phase 2** |

Phase 1 は実装済み73%を1.0に正規化して ±100 スケールを保つ。

### 閾値の較正根拠

バックテストで翌10営業日の的中率がスコアの強さと単調に上がることを確認した。

| 閾値 | 翌5営業日 | 翌10営業日 | 件数(10日) |
|---|---|---|---|
| 閾値なし | 45.8% | 46.2% | 2730 |
| \|スコア差\| ≧ 15 | 50.0% | 51.2% | 1464 |
| **\|スコア差\| ≧ 30（現行の中立境界）** | **50.9%** | **53.9%** | **686** |
| \|スコア差\| ≧ 45 | 52.3% | 58.2% | 251 |
| \|スコア差\| ≧ 60 | 51.4% | 60.6% | 71 |

閾値を設けずに弱いシグナルまで拾うと50%を割るため、±30未満は「シグナルなし」として中立に倒している。

> 日次サンプルは重複が大きく（10日保有なら実質独立は約1/10）、高閾値の的中率は件数が
> 少ないため統計的に有意とまでは言えない。参考値として扱うこと。

`python score.py --backtest` でいつでも再現できる。

## データソース（すべて無料・APIキー不要）

| 用途 | ソース |
|---|---|
| 為替7ペア・株価指数・VIX・商品・FF先物・ドル指数 | Yahoo Finance |
| 米2年金利・HYスプレッド・期待インフレ・ECB政策金利 | FRED（`fredgraph.csv`） |
| ユーロ圏2年AAAスポット（日次） | ECB Data Portal |
| 日本2年国債（日次・和暦・Shift-JIS） | 財務省 `jgbcm_all.csv` + `jgbcm.csv` |
| 英2年スポット・英OIS 1年（日次） | BOE イールドカーブ zip |
| 豪2年国債（日次） | RBA `f2-data.csv` |
| CHF 短期金利（SARON 6ヶ月） | SNB `zirepo` キューブ |

### ハマりどころ（実地検証で判明）

- **FRED はブラウザ風 User-Agent を付けると応答が返ってこない。** ヘッダ無しの素の urllib なら即返る
- **RBA も同様にブラウザ風 UA だと 403。** ヘッダ無しなら通る
- **RBA の `f2.1-data.csv` は月次。** 日次が必要なら `f2-data.csv`
- **stooq** は JavaScript proof-of-work の bot チェックが入り CI から使えない
- **FRED の OECD 由来の国際系列は多くが更新停止**（景気先行指数 CLI は6か国とも2024年1月、
  ユーロ圏失業率は2023年、日本CPIは2021年で停止）。米国以外のマクロは各国公式APIが必要
- **SNB の CHF 国債利回り（`rendoblim` / `rendoblid`）は2025年7月で更新停止。**
  代わりに `zirepo` の SARON を使っている。デュレーションが短いぶん金利感応度が低く出る
- **BOE と財務省の当月ファイルは当月分しか返さない。** `data.json` 自体を履歴キャッシュとして
  扱い、毎日マージして育てる設計にしている

## 使い方

```bash
pip install numpy openpyxl

python fetch_market.py            # データ取得 → data.json
python fetch_market.py --dry-run  # 取得可否と鮮度だけ確認（書き込まない）
python bootstrap_history.py       # 初回のみ: BOE の全履歴を流し込む
python score.py                   # スコア計算 → data.json
python score.py --backtest        # 的中率の検証
python serve.py                   # http://localhost:8000 で確認
python serve.py --refresh         # 取得・計算してから配信
```

初回セットアップ:

```bash
python fetch_market.py && python bootstrap_history.py && python score.py && python serve.py
```

`bootstrap_history.py` は BOE の全履歴アーカイブ（約39MB）から英2年金利の過去分を一度だけ
流し込む。これをやらないと、z-score に必要な252営業日分が溜まるまで GBP のスコアが出ない。
2回目以降は `fetch_market.py` が当月分を `data.json` にマージしていくので再実行は不要。

## 自動更新

`.github/workflows/update-data.yml` が平日 06:10 UTC（日本時間 15:10）に実行され、
`data.json` を更新してコミットする。GitHub Pages で配信すればそのまま反映される。

「Claude の見立て」（`narrative.json`）は claude.ai のクラウド routine
「FXダッシュボード narrative 毎日更新」が平日 06:40 UTC（日本時間 15:40、データ更新の
30分後）に `/fx-brief` の手順で再生成してコミットする。Anthropic API キーは不要で、
管理（停止・時刻変更・手動実行）は https://claude.ai/code/routines から。

## ファイル構成

```
config.py             通貨・ペア・ティッカー・ウェイト・閾値
sources.py            データ取得層（ホストごとのヘッダ差異を吸収）
fetch_market.py       全ソース取得 → data.json（失敗時は前回値を保持し stale 表示）
bootstrap_history.py  初回のみ: BOE 全履歴の流し込み
score.py              柱別スコア → 通貨スコア → ペア方向性 ／ バックテスト
index.html            静的ダッシュボード
serve.py              ローカル確認用サーバ
validate_narrative.py narrative.json の検査
data.json             生成物（履歴キャッシュを兼ねる）
narrative.json        Claude の見立て（/fx-brief が生成）
.claude/skills/fx-brief/SKILL.md   /fx-brief スキルの定義
```

## Claude の見立て（`/fx-brief`）

機械スコアが数値化できない材料——中銀のスタンス、要人発言、政治イベント、為替介入の
警戒感——を Claude が補うレイヤー。Claude Code で `/fx-brief` を叩くと、`data.json` を
読んだうえで各国中銀の現状を調べ、`narrative.json` を生成してコミットする。

**機械スコアは書き換えない。** 数値を手で動かすとバックテストで検証した的中率の意味が
失われ、シグナルが当たった/外れたときの原因追跡もできなくなる。Claude の見解は
別レイヤーとして重ねて表示し、**機械と食い違っている通貨にだけ ▲▼ の印が付く**。
印がなければ「Claude も機械と同意見」という意味で、食い違いが一目で分かるようにしてある。

```bash
# Claude Code 上で
/fx-brief

# 生成物の検査だけ単体で回す場合
python validate_narrative.py
```

`narrative.json` のスキーマと書き方のルールは `.claude/skills/fx-brief/SKILL.md` にある。
`view` は**機械スコアとの差分**を表すので、機械と同意見の通貨は `neutral` が正しい
（`validate_narrative.py` が全通貨に補正が付いている場合に警告を出す）。

実行は平日 15:40 JST にクラウド routine が自動で行う（「自動更新」の項を参照）。
注目イベントの日付が日々消化されていくため、材料が乏しい日でも watch の鮮度維持に意味がある。
FOMC・日銀会合の直後などは、任意のタイミングで `/fx-brief` を手で叩いて上書きしてもよい。
機械スコアとナラティブの更新日がずれた場合は、ダッシュボードに警告色で明示される。

## 今後

- **Phase 2**: インフレ（12%）と景気・雇用（15%）の柱を各国公式API
  （Eurostat / ONS / e-Stat / ABS / BFS）から追加。米国は既存の
  [us-econ-dashboard](https://takuma41n.github.io/us-econ-dashboard/) のロジックを流用
- **Phase 3**: CFTC COT のポジションオーバーレイ（投機筋が極端に傾いていたら逆張り警告）
- `/fx-brief` の自動化は、GitHub Actions（要 Anthropic API キー・従量課金）ではなく
  claude.ai のクラウド routine で実現した（2026-08-07 から平日 15:40 JST に毎日実行）

## 免責

機械的に計算した参考値であり、投資助言ではない。
