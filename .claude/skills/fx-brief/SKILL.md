---
name: fx-brief
description: FX通貨強弱ダッシュボードに「Claude の見立て」を書き込むスキル。data.json の機械スコアを読み、中銀のスタンス・要人発言・政治イベント・今週の重要指標を調べて narrative.json を生成しコミットする。/fx-brief で起動。「見立てを更新」「ナラティブ更新」「FXの相場観をまとめて」などでも起動。
---

# fx-brief — Claude の見立てを更新する

FX通貨強弱ダッシュボード（https://takuma41n.github.io/fx-strength-dashboard/）の
「Claude の見立て」欄を更新する。

## 大前提: 機械スコアは絶対に書き換えない

`data.json` の中身は**読むだけ**。スコアを上書きしてはいけない。

理由は2つ。数値を手で動かすとバックテスト（`python3 score.py --backtest`）で検証した
的中率の意味が失われる。もうひとつは、毎回同じ入力から同じスコアが出るという再現性が
壊れると、シグナルが当たった/外れたときの原因追跡ができなくなる。

Claude の役割は**機械が数値化できないものを別レイヤーで補うこと**であって、
機械の出力を上書きすることではない。両者が食い違っているときは、
食い違っていること自体が最も有用な情報なので、隠さず並べて出す。

## 手順

### 1. 機械スコアを読む

```bash
python3 -c "
import json; d=json.load(open('data.json')); s=d['scores']
print('基準日', s['as_of'], '/ レジーム', s['regime']['label'], s['regime']['value'])
for c,i in sorted(s['currencies'].items(), key=lambda kv:-kv[1]['score']):
    print(f\"{c} {i['score']:+6.1f} (前週 {i['d5']:+.1f}) 柱={i['pillars']} {i['rate']['label']} {i['rate']['value']}% 20日{i['rate']['d20']:+.0f}bp\")
for n,p in s['pairs'].items():
    print(f\"{n} {p['bias_label']} 差{p['score']:+.1f} 確信度{p['conviction']:.2f} 乖離{p['divergence']:+.2f}\")
"
```

データが古ければ先に更新する（`python3 fetch_market.py && python3 score.py --quiet`）。

### 2. 数値に出ていないものを調べる

WebSearch / WebFetch で、**機械スコアが拾えない範囲**を調べる。
金利や株価はすでにスコアに入っているので、調べるのはその手前にあるもの。

対象6通貨: USD / EUR / JPY / GBP / AUD / CHF

- **中銀のスタンス**: 直近の会合の声明・議事要旨・総裁発言のトーン変化。
  「次回の利下げに含みを持たせた」など、まだ金利に織り込まれていない変化
- **要人発言**: 財務省・中銀高官のコメント。特に**円の為替介入への言及**
- **政治・財政イベント**: 選挙、予算、関税、地政学
- **今週〜来週の重要指標**: 各通貨のCPI・雇用統計・中銀会合の日程

調べる順番は、機械スコアで**大きく動いた通貨**と**乖離が大きいペア**を優先する。
全通貨を均等に調べる必要はない。動いていない通貨は「特筆なし」でよい。

### 3. 特に注目すべきもの

- **乖離が ±1.0σ を超えているペア**: 機械が「ファンダと価格がズレている」と
  言っている。その理由が定性面にあるなら、それこそが Claude が書くべきこと
- **確信度が高いのに柱が1本しか効いていないシグナル**: 脆いので注意喚起する
- **CHF**: SARON 6ヶ月で代用しているため金利感応度が低く出る。SNBの動きは
  数値に出にくいので定性面で補う
- **JPY**: 介入警戒は数値化していない。USDJPY の水準と当局の発言を必ず確認する

### 4. narrative.json を書く

```json
{
  "as_of": "YYYY-MM-DD",
  "generated_at": "YYYY-MM-DDTHH:MM:SS+00:00",
  "based_on": "data.json の scores.as_of の値",
  "summary": "全体の見立て。3〜5文。今週の相場を動かしている軸は何かを最初の1文で言い切る。",
  "currencies": {
    "USD": { "view": "up | down | neutral", "note": "1〜2文。機械スコアに出ていない材料だけ書く" }
  },
  "watch": [
    { "date": "YYYY-MM-DD", "ccy": "USD", "event": "FOMC", "note": "スコアが反転しうるか一言" }
  ],
  "caveat": "今回の見立てで自信のない部分。無ければ空文字"
}
```

- `view` は**機械スコアとの差分**を表す。機械が USD 強気で Claude も強気なら
  `neutral`（＝補正なし）。機械が見落としている下押し材料があるときだけ `down`。
  「機械と同じ意見」を `up` と書かない。ここを間違えるとダッシュボードの
  「Claudeが機械と食い違っている箇所」の表示が意味をなさなくなる
- `currencies` は6通貨すべてを入れる。材料がなければ `view: "neutral"`、
  `note` に「特筆なし」
- `watch` は**日付順**。今後2週間程度。多くても6件まで
- 断定を避けた曖昧な書き方はしない。根拠が弱いなら `caveat` に書く

### 5. 検証して公開する

```bash
python3 validate_narrative.py
git add narrative.json && git commit -m "chore: update narrative ($(date -u +%Y-%m-%d))" && git push
```

`validate_narrative.py` が6通貨そろっているか・`view` の値が正しいか・
基準日が `data.json` と一致しているかを検査する。
Pages は main への push で自動的に再デプロイされる。

## やってはいけないこと

- `data.json` の書き換え
- 売買の推奨（「買うべき」「利確しろ」）。ユーザーはエントリーを自分で判断する。
  出すのは判断材料であって判断そのものではない
- 調べずに書く。知識だけで中銀の現在のスタンスを書かない。必ず検索して確認する
- 全通貨に無理やり材料を作る。動いていない通貨は「特筆なし」が正しい
