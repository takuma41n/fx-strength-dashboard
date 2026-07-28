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
    "us2y": "DGS2",              # 米2年金利
    "hy_oas": "BAMLH0A0HYM2",    # 米ハイイールド・スプレッド
    "breakeven5y": "T5YIE",      # 期待インフレ5年
    "effr": "EFFR",              # 実効FF金利
    "ecb_depo": "ECBDFR",        # ECB預金ファシリティ金利
}

# ---------------------------------------------------------------------------
# 金利ソース: 通貨ごとの「政策期待を代表する金利」
#
# CHF だけは 2年国債の無料日次ソースが全滅していた（SNB rendoblid/rendoblim は
# 2025年7月で更新停止）ため、SARON 6ヶ月コンパウンド（マネーマーケット金利）で
# 代用する。デュレーションが短いぶん感応度が低いことを UI 側で明示する。
# ---------------------------------------------------------------------------
RATE_SOURCES = {
    "USD": {"key": "us2y", "label": "米2年国債", "tenor": "2Y", "proxy": False},
    "EUR": {"key": "eur2y", "label": "ユーロ圏2年(AAA)", "tenor": "2Y", "proxy": False},
    "JPY": {"key": "jpy2y", "label": "日本2年国債", "tenor": "2Y", "proxy": False},
    "GBP": {"key": "gbp2y", "label": "英2年スポット", "tenor": "2Y", "proxy": False},
    "AUD": {"key": "aud2y", "label": "豪2年国債", "tenor": "2Y", "proxy": False},
    "CHF": {"key": "chf6m", "label": "SARON 6ヶ月", "tenor": "6M", "proxy": True},
}

# ---------------------------------------------------------------------------
# スコア設計
# ---------------------------------------------------------------------------

# 全体設計でのウェイト（Phase 2 完成時）
PILLAR_WEIGHTS_FULL = {
    "rates": 0.40,      # 金利・政策期待          （日次・実装済み）
    "risk": 0.25,       # リスクレジーム          （日次・実装済み）
    "growth": 0.15,     # 景気・雇用              （月次・Phase 2）
    "inflation": 0.12,  # インフレ                （月次・Phase 2）
    "idio": 0.08,       # 固有要因                （日次・実装済み）
}

# Phase 1 で実装済みの柱。合計 0.73 を 1.0 に正規化して ±100 スケールを保つ。
PHASE1_PILLARS = ["rates", "risk", "idio"]

PILLAR_LABELS = {
    "rates": "金利・政策期待",
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
