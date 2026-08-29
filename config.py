"""FX通貨強弱・方向性ダッシュボード — 設定.

7ペアは USD/EUR/JPY/GBP/AUD/CHF の6通貨で閉じるので、
「6通貨のスコア」→「7ペアの方向性」が機械的に導出できる。
"""

CURRENCIES = ["USD", "EUR", "JPY", "GBP", "AUD", "CHF"]

# (ペア名, 基軸通貨, 決済通貨)
PAIRS = [
    ("USDJPY", "USD", "JPY"),
    ("EURJPY", "EUR", "JPY"),
    ("GBPJPY", "GBP", "JPY"),
    ("EURUSD", "EUR", "USD"),
    ("GBPUSD", "GBP", "USD"),
    ("AUDUSD", "AUD", "USD"),
    ("USDCHF", "USD", "CHF"),
]

PAIR_NAMES = [p[0] for p in PAIRS]

# Yahoo Finance のティッカー。表示用にペアを直接取得する。
FX_TICKERS = {
    "USDJPY": "JPY=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X",
    "USDCHF": "CHF=X",
}

# 通貨インデックス算出用: USD を軸に「USD 1単位 = ? 通貨X」の系列を作る。
# invert=True なら取得値の逆数を使う（EURUSD → USD/EUR）。
USD_LEGS = {
    "JPY": ("USDJPY", False),
    "EUR": ("EURUSD", True),
    "GBP": ("GBPUSD", True),
    "AUD": ("AUDUSD", True),
    "CHF": ("USDCHF", False),
}

# 各通貨圏の代表株価指数（リスクβ推定とリスクレジーム判定に使用）
EQUITY_TICKERS = {
    "USD": "^GSPC",
    "EUR": "^STOXX50E",
    "JPY": "^N225",
    "GBP": "^FTSE",
    "AUD": "^AXJO",
    "CHF": "^SSMI",
}

# リスクレジーム・固有要因で使う市場データ
MARKET_TICKERS = {
    "vix": "^VIX",
    "oil": "CL=F",       # WTI原油 — JPY(輸入国)の固有要因
    "copper": "HG=F",    # 銅 — AUDの固有要因、世界景気の代理
    "gold": "GC=F",      # 金/銅比でリスク回避度を測る
    "china": "000001.SS",  # 上海総合 — AUDの対中エクスポージャ
    "ff_futures": "ZQ=F",  # FF金利先物（USDの政策織り込み、参考表示）
    "dxy": "DX-Y.NYB",
}

# 政策金利の織り込み(参考表示用)。スコアには入れない。
FRED_SERIES = {
    "us10y": "DGS10",            # 米10年金利（金利柱の素材）
    "us2y": "DGS2",              # 米2年金利（参考値。柱には入れない）
    "hy_oas": "BAMLH0A0HYM2",    # 米ハイイールド・スプレッド
    "breakeven5y": "T5YIE",      # 期待インフレ5年
    "effr": "EFFR",              # 実効FF金利
    "ecb_depo": "ECBDFR",        # ECB預金ファシリティ金利
}

# ---------------------------------------------------------------------------
# 金利ソース: 全通貨とも10年国債利回りで統一する。
#
# もとは2年（政策期待を代表する年限）だったが、CHF だけ2年の無料日次ソースが
# 全滅していた。SNB の rendoblid/rendoblim は 2025-07-31 で停止したまま、
# 代用していた SARON 6ヶ月は SNB が週次・約7日遅れでしか公開せず、政策金利0%下では
# 5日変化の標準偏差が 0.6bp しかない（他通貨の国債は 3〜10bp）。分母が桁違いに
# 小さいので、わずかな変化が過大な z-score に化けていた。
#
# 10年に揃えた根拠（米国で400営業日を実測）:
#   2年と10年の変化の相関は 5日 +0.83 / 20日 +0.82 / 60日 +0.89、符号一致率 74〜82%。
#   変動幅もほぼ同じ（5日変化std 9.5bp 対 9.5bp）。年限を替えても方向の読みは変わらない。
# 10年なら CHF も SNB 本体サイトの日次 JSON（連邦債10年）で前営業日まで取れ、
# 5日変化std も 5.6bp と他通貨と同水準になる。全通貨が同じ年限の国債利回りに
# 揃うため proxy 扱いの通貨は無くなった。
#
# 注意: 10年は政策期待だけでなくタームプレミアムと世界的なデュレーション要因も
# 含む。柱の名前を「金利（10年）」にしているのはそのため。
# ---------------------------------------------------------------------------
RATE_SOURCES = {
    "USD": {"key": "us10y", "label": "米10年国債", "tenor": "10Y", "proxy": False},
    "EUR": {"key": "eur10y", "label": "ユーロ圏10年(AAA)", "tenor": "10Y", "proxy": False},
    "JPY": {"key": "jpy10y", "label": "日本10年国債", "tenor": "10Y", "proxy": False},
    "GBP": {"key": "gbp10y", "label": "英10年スポット", "tenor": "10Y", "proxy": False},
    "AUD": {"key": "aud10y", "label": "豪10年国債", "tenor": "10Y", "proxy": False},
    "CHF": {"key": "chf10y", "label": "スイス連邦債10年", "tenor": "10Y", "proxy": False},
}

# ---------------------------------------------------------------------------
# スコア設計
# ---------------------------------------------------------------------------

# 全体設計でのウェイト（Phase 2 完成時）
PILLAR_WEIGHTS_FULL = {
    "rates": 0.40,      # 金利（10年）            （日次・実装済み）
    "risk": 0.25,       # リスクレジーム          （日次・実装済み）
    "growth": 0.15,     # 景気・雇用              （月次・Phase 2）
    "inflation": 0.12,  # インフレ                （月次・Phase 2）
    "idio": 0.08,       # 固有要因                （日次・実装済み）
}

# Phase 1 で実装済みの柱。合計 0.73 を 1.0 に正規化して ±100 スケールを保つ。
PHASE1_PILLARS = ["rates", "risk", "idio"]

PILLAR_LABELS = {
    "rates": "金利（10年）",
    "risk": "リスクレジーム",
    "growth": "景気・雇用",
    "inflation": "インフレ",
    "idio": "固有要因",
}

# 柱①の内訳。全通貨で同一の式を使う（通貨間の比較可能性を最優先するため、
# USD の FF先物や GBP の OIS といった通貨固有の高精度データはスコアに入れず
# 参考表示に回す）。
RATE_SUB_WEIGHTS = {"d5": 0.45, "d20": 0.30, "d60": 0.25}

# リスクβの事前値。実際は120営業日のローリング回帰で推定し、
# データ不足時のみこの値にフォールバックする。
RISK_BETA_PRIOR = {
    "AUD": 1.00,
    "GBP": 0.50,
    "EUR": 0.20,
    "USD": -0.30,
    "CHF": -0.70,
    "JPY": -0.90,
}

# z-score のルックバック（営業日）
Z_LOOKBACK = 252
# リスクβ推定のルックバック（営業日）
BETA_LOOKBACK = 120

# ペアバイアスの閾値（スコア差の絶対値）。
#
# バックテストで較正した値。翌10営業日の的中率はスコアの強さと単調に上がり、
#   |スコア| >= 15 → 51.2%   >= 30 → 53.9%   >= 45 → 58.2%   >= 60 → 60.6%
# 一方で閾値なし（弱いシグナルも拾う）だと 46.2% と50%を割る。
# つまり弱いスコアは無情報どころか逆効果なので、中立ゾーンを広く取る。
BIAS_STRONG = 50
BIAS_MILD = 30

BIAS_LABELS = {
    2: "強い買い",
    1: "買い",
    0: "中立",
    -1: "売り",
    -2: "強い売り",
}

# 系列を data.json に保存する際の保持日数
KEEP_DAYS = 400

# ソースが何日古くなったら stale とみなすか（営業日ベースの目安）
STALE_THRESHOLD_DAYS = {
    "daily": 5,
    "weekly": 12,
    "monthly": 45,
}

# ---------------------------------------------------------------------------
# コモディティ（USD建て・独立スコア）
#
# FXの6通貨スコアは「閉じたバスケットの相対値」（平均が必ずゼロ）だが、
# コモディティは閉じないので絶対評価とする。CURRENCIES には決して足さないこと。
# demean の基準が歪んで6通貨のスコア自体が壊れ、バックテストで較正した
# バイアス閾値も無効になる。
# ---------------------------------------------------------------------------

COMMODITIES = ["XAUUSD", "XAGUSD", "WTI"]

# series は data.json の series キー。digits/unit は表示用。
COMMODITY_META = {
    "XAUUSD": {"label": "金", "series": "gold", "digits": 2, "unit": "USD/oz"},
    "XAGUSD": {"label": "銀", "series": "silver", "digits": 3, "unit": "USD/oz"},
    "WTI": {"label": "WTI原油", "series": "oil", "digits": 2, "unit": "USD/bbl"},
}

COMMODITY_PILLAR_LABELS = {
    "real_rate": "実質金利",
    "usd": "ドル",
    "risk": "リスク回避需要",
    "inflation": "期待インフレ",
    "momentum": "モメンタム",
    "gold_link": "金連動",
    "industrial": "工業需要",
    "gs_ratio": "金銀比",
    "inventory": "在庫",
    "term": "ターム構造",
    "demand": "世界需要",
}

# 銘柄ごとの柱とウェイト。
#   金:  実質金利+ドルで0.52と、FX側の「金利40%」に相当する支配的ブロックを作る。
#        期待インフレは real = nominal − breakeven の恒等式で実質金利と一部重複
#        するため意図的に低め。
#   銀:  金の合成スコアを丸ごと流し込むと（ドル・実質金利の）二重計上になるため、
#        観測可能な金価格のモメンタムだけを伝達項にする。リスク柱を入れないのは
#        銀のVIX感応度の符号が不安定なため（貴金属として買われる局面と
#        工業金属として売られる局面が混在する）。
#   WTI: 在庫とターム構造という現物需給の柱を主役にする。
COMMODITY_PILLAR_WEIGHTS = {
    "XAUUSD": {"real_rate": 0.32, "usd": 0.20, "risk": 0.18, "inflation": 0.12,
               "momentum": 0.18},
    "XAGUSD": {"gold_link": 0.26, "industrial": 0.20, "real_rate": 0.16, "usd": 0.14,
               "momentum": 0.16, "gs_ratio": 0.08},
    "WTI": {"inventory": 0.28, "term": 0.24, "demand": 0.20, "momentum": 0.16,
            "usd": 0.12},
}

# ウェイト合計が1でないと ±100 スケールが崩れる。設定ミスは起動時に落とす。
for _sym, _w in COMMODITY_PILLAR_WEIGHTS.items():
    assert abs(sum(_w.values()) - 1.0) < 1e-9, f"{_sym} のウェイト合計が1ではない"

# 自己モメンタムのサブウェイト。コモディティの5日騰落はノイズと短期反転が
# 支配的で、時系列モメンタムが立つのは20〜60日なので金利Δ(45/30/25)とは変える。
MOMENTUM_SUB_WEIGHTS = {"d5": 0.25, "d20": 0.40, "d60": 0.35}

# EIA在庫のサブウェイト（週次空間で計算する）。Δ52週は季節性除去を兼ねる。
INVENTORY_SUB_WEIGHTS = {"w1": 0.35, "w4": 0.35, "w52": 0.30}

# 在庫z のルックバック（週次点数 = 2年）
INVENTORY_Z_LOOKBACK = 104

# コモディティのバイアス閾値。
# FXの 50/30 は「2通貨のスコア差」（実効±200レンジ）で較正した値なので流用できない。
#
# 閾値ラダーの実測（2025-01〜2026-08、python score_commodities.py --backtest）:
#   閾値なし → 翌10日 48.4%   >=20 → 44.1%   >=30 → 44.8%
#   >=40 → 50.3%   >=50 → 59.2%（71件）
# 弱〜中程度のシグナルは50%を割る（逆効果）ため、FX側と同じ考え方で
# 中立ゾーンを広く取り、単調増加が始まる 40/50 に設定した。
# ただし履歴が400営業日しかなく件数も少ないため参考値。データが溜まったら再較正する。
COMMO_BIAS_STRONG = 50
COMMO_BIAS_MILD = 40
