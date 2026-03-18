
import requests
import pandas as pd
import ta
import time

BASE_URL = "https://api.binance.com"


def safe_request(url, params, retries=3):
    last_error = None

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    for _ in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)

            # проверка HTTP
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code} - {r.text}"
                time.sleep(1)
                continue

            # безопасный JSON
            try:
                data = r.json()
            except Exception:
                last_error = f"Invalid JSON response: {r.text}"
                time.sleep(1)
                continue

            # проверка API ответа
            if data.get("retCode") != 0:
                last_error = data.get("retMsg", "API error")
                time.sleep(1)
                continue

            # проверка result
            if "result" not in data or data["result"] is None:
                last_error = "Missing result in response"
                time.sleep(1)
                continue

            return data

        except Exception as e:
            last_error = str(e)
            time.sleep(1)

    print("Request failed:", last_error)
    return None

# ===============================
# MARKET DATA
# ===============================

def get_kline(symbol, interval):

    interval_map = {
        "15": "15m",
        "60": "1h",
        "240": "4h",
        "D": "1d"
    }

    url = BASE_URL + "/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": interval_map[interval],
        "limit": 200
    }

    r = requests.get(url, params=params)
    data = r.json()

    df = pd.DataFrame(data, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])

    df = df[["timestamp","open","high","low","close","volume"]]

    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)

    return df


# ===============================
# OPEN INTEREST
# ===============================

def get_open_interest(symbol):
    return 0

    url = BASE_URL + "/v5/market/open-interest"

    params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "limit": 2
    }

    data = safe_request(url, params)

    if data is None:
        return 0

    rows = data["result"]["list"]

    if len(rows) < 2:
        return 0

    now = float(rows[0]["openInterest"])
    prev = float(rows[1]["openInterest"])

    if prev == 0:
        return 0

    return (now - prev) / prev * 100


# ===============================
# FUNDING RATE
# ===============================

def get_funding(symbol):
    return 0

    url = BASE_URL + "/v5/market/funding/history"

    params = {
        "category": "linear",
        "symbol": symbol,
        "limit": 1
    }

    data = safe_request(url, params)

    if data is None:
        return 0

    rows = data["result"]["list"]

    if not rows:
        return 0

    return float(rows[0]["fundingRate"])


# ===============================
# ORDERBOOK IMBALANCE
# ===============================

def get_orderbook(symbol):
    return 0

    url = BASE_URL + "/v5/market/orderbook"

    params = {
        "category": "linear",
        "symbol": symbol,
        "limit": 50
    }

    data = safe_request(url, params)

    if data is None:
        return 0

    bids = data["result"]["b"]
    asks = data["result"]["a"]

    bid_volume = sum(float(b[1]) for b in bids)
    ask_volume = sum(float(a[1]) for a in asks)

    total = bid_volume + ask_volume

    if total == 0:
        return 0

    imbalance = (bid_volume - ask_volume) / total

    return imbalance


# ===============================
# TRADE FLOW
# ===============================

def get_trade_flow(symbol):
    return 0

    url = BASE_URL + "/v5/market/recent-trade"

    params = {
        "category": "linear",
        "symbol": symbol,
        "limit": 200
    }

    data = safe_request(url, params)

    if data is None:
        return 0

    trades = data["result"]["list"]

    buys = 0
    sells = 0

    for t in trades:
        if t["side"] == "Buy":
            buys += 1
        else:
            sells += 1

    total = buys + sells

    if total == 0:
        return 0

    return (buys - sells) / total


# ===============================
# ANALYSIS
# ===============================

def analyze(symbol):

    df15 = get_kline(symbol, "15")
    df1h = get_kline(symbol, "60")
    df4h = get_kline(symbol, "240")
    df1d = get_kline(symbol, "D")

    # indicators
    df15["ATR"] = ta.volatility.AverageTrueRange(
        high=df15["high"],
        low=df15["low"],
        close=df15["close"],
        window=14
    ).average_true_range()

    df15["RSI"] = ta.momentum.RSIIndicator(df15["close"], 14).rsi()
    df15["VOL_AVG20"] = df15["volume"].rolling(20).mean()

    df1h["EMA20"] = df1h["close"].ewm(span=20).mean()
    df1h["EMA50"] = df1h["close"].ewm(span=50).mean()
    df1h["ADX"] = ta.trend.ADXIndicator(
        high=df1h["high"],
        low=df1h["low"],
        close=df1h["close"],
        window=14
    ).adx()

    df4h["EMA20"] = df4h["close"].ewm(span=20).mean()
    df4h["EMA50"] = df4h["close"].ewm(span=50).mean()
    df4h["ADX"] = ta.trend.ADXIndicator(
        high=df4h["high"],
        low=df4h["low"],
        close=df4h["close"],
        window=14
    ).adx()

    df1d["EMA20"] = df1d["close"].ewm(span=20).mean()
    df1d["EMA50"] = df1d["close"].ewm(span=50).mean()

    last15 = df15.iloc[-1]
    last1h = df1h.iloc[-1]
    last4h = df4h.iloc[-1]
    last1d = df1d.iloc[-1]

    price = float(last15["close"])
    atr = float(last15["ATR"]) if not pd.isna(last15["ATR"]) else 0.0001
    rsi = float(last15["RSI"]) if not pd.isna(last15["RSI"]) else 50

    trend1h = "BULLISH" if last1h["EMA20"] > last1h["EMA50"] else "BEARISH"
    trend4h = "BULLISH" if last4h["EMA20"] > last4h["EMA50"] else "BEARISH"
    trend1d = "BULLISH" if last1d["EMA20"] > last1d["EMA50"] else "BEARISH"

    # ===============================
    # MARKET BIAS
    # ===============================

    if trend1h == "BULLISH" and trend4h == "BULLISH" and trend1d == "BULLISH":
        market_bias = "STRONG BULLISH"
    elif trend1h == "BEARISH" and trend4h == "BEARISH" and trend1d == "BEARISH":
        market_bias = "STRONG BEARISH"
    elif trend4h == trend1d:
        market_bias = "TREND CONTINUATION"
    else:
        market_bias = "MIXED MARKET"

    # orderflow data
    oi_change = get_open_interest(symbol)
    funding = get_funding(symbol)
    orderbook = get_orderbook(symbol)
    flow = get_trade_flow(symbol)

    avg_volume_20 = last15["VOL_AVG20"]
    volume_spike = False if pd.isna(avg_volume_20) or avg_volume_20 == 0 else last15["volume"] > avg_volume_20 * 1.8

    # liquidation proxy
    liquidation_pressure = abs(oi_change) > 3

    # ===============================
    # VOLATILITY REGIME
    # ===============================

    volatility = 0 if price == 0 else atr / price

    if volatility < 0.003:
        volatility_regime = "LOW VOLATILITY"
    elif volatility < 0.007:
        volatility_regime = "NORMAL VOLATILITY"
    elif volatility < 0.015:
        volatility_regime = "HIGH VOLATILITY"
    else:
        volatility_regime = "EXTREME VOLATILITY"

    # ===============================
    # MARKET MODE / RANGE DETECTOR
    # ===============================

    adx_1h = float(last1h["ADX"]) if not pd.isna(last1h["ADX"]) else 0
    adx_4h = float(last4h["ADX"]) if not pd.isna(last4h["ADX"]) else 0

    if (adx_1h >= 22 or adx_4h >= 22) and volatility >= 0.007:
        market_mode = "TREND"
    elif (adx_1h < 22 and adx_4h < 22) and (not volume_spike) and volatility < 0.01:
        market_mode = "RANGE"
    elif volatility >= 0.015 or volume_spike:
        market_mode = "VOLATILE"
    else:
        market_mode = "BALANCED"

    # ===============================
    # AI SCORE
    # ===============================

    score = 50

    if trend1h == "BULLISH":
        score += 10
    else:
        score -= 10

    if trend4h == "BULLISH":
        score += 10
    else:
        score -= 10

    if trend1d == "BULLISH":
        score += 10
    else:
        score -= 10

    if rsi > 60:
        score += 5

    if rsi < 40:
        score -= 5

    if orderbook > 0.1:
        score += 8

    if orderbook < -0.1:
        score -= 8

    if flow > 0.1:
        score += 5

    if flow < -0.1:
        score -= 5

    if volume_spike:
        score += 5

    if oi_change > 2:
        score += 5

    if oi_change < -2:
        score -= 5

    if market_mode == "RANGE":
        score += 4
    elif market_mode == "VOLATILE":
        score -= 4

    score = max(0, min(100, score))

    if score > 65:
        direction = "LONG GRID"
    elif score < 35:
        direction = "SHORT GRID"
    else:
        direction = "WAIT"

    lower = price - atr * 10
    upper = price + atr * 10

    # ===============================
    # GRID OPTIMIZER
    # ===============================

    if volatility < 0.002:
        grids = 30
    elif volatility < 0.004:
        grids = 50
    elif volatility < 0.007:
        grids = 70
    elif volatility < 0.012:
        grids = 100
    else:
        grids = 150

    if volatility < 0.003:
        leverage = 5
    elif volatility < 0.006:
        leverage = 4
    elif volatility < 0.01:
        leverage = 3
    elif volatility < 0.02:
        leverage = 2
    else:
        leverage = 1

    if market_mode == "RANGE":
        grids = int(grids * 1.1)
    elif market_mode == "TREND":
        grids = int(grids * 0.9)
    elif market_mode == "VOLATILE":
        leverage = max(1, leverage - 1)

    grid_range = abs(upper - lower)
    grid_step = grid_range / max(grids, 1)
    expected_profit = 0 if price == 0 else (grid_step / price) * 100

    # ===============================
    # PROBABILITY MODEL
    # ===============================

    bull_prob = 50
    bear_prob = 50

    if trend1h == "BULLISH":
        bull_prob += 10
    else:
        bear_prob += 10

    if trend4h == "BULLISH":
        bull_prob += 10
    else:
        bear_prob += 10

    if trend1d == "BULLISH":
        bull_prob += 15
    else:
        bear_prob += 15

    if rsi > 55:
        bull_prob += 5

    if rsi < 45:
        bear_prob += 5

    if orderbook > 0:
        bull_prob += 5
    else:
        bear_prob += 5

    if flow > 0:
        bull_prob += 5
    else:
        bear_prob += 5

    total_prob = bull_prob + bear_prob
    bull_prob = round(bull_prob / total_prob * 100, 1)
    bear_prob = round(bear_prob / total_prob * 100, 1)

    # ===============================
    # OUTPUT
    # ===============================

    print("\n========== AI MARKET REPORT ==========")

    print("Symbol:", symbol)
    print("Price:", round(price, 5) if price < 1 else round(price, 2))

    print("\nTrend 1H:", trend1h)
    print("Trend 4H:", trend4h)
    print("Trend 1D:", trend1d)

    print("\nRSI:", round(rsi, 2))
    print("ATR:", round(atr, 5) if price < 1 else round(atr, 2))

    print("\nOpen Interest Change:", round(oi_change, 2), "%")
    print("Funding Rate:", funding)

    print("\nOrderbook Imbalance:", round(orderbook, 3))
    print("Trade Flow:", round(flow, 3))

    print("\nVolume Spike:", volume_spike)
    print("Liquidation Pressure:", liquidation_pressure)

    print("\nAI Score:", score)
    print("\nRecommendation:", direction)

    print("\nMarket Bias:", market_bias)
    print("Volatility Regime:", volatility_regime)
    print("Market Mode:", market_mode)

    print("\nMarket Probability")
    print("Bullish:", bull_prob, "%")
    print("Bearish:", bear_prob, "%")

    print("\nSuggested Grid Range:")
    if price < 1:
        print(round(lower, 5), "-", round(upper, 5))
    else:
        print(round(lower, 2), "-", round(upper, 2))

    print("\nGRID OPTIMIZATION")
    print("Volatility:", round(volatility * 100, 2), "%")
    print("Grid Count:", grids)
    print("Grid Step:", round(grid_step, 6))
    print("Suggested Leverage:", str(leverage) + "x")
    print("Estimated Profit per Grid:", round(expected_profit, 3), "%")

    print("======================================\n")


def run_analysis(symbol):
    import io
    import sys

    buffer = io.StringIO()
    sys_stdout = sys.stdout
    sys.stdout = buffer

    try:
        analyze(symbol)
    except Exception as e:
        print("Error:", e)

    sys.stdout = sys_stdout
    return buffer.getvalue()


# ===============================
# MAIN
# ===============================

def main():

    print("\nAI Trading Agent Ready\n")

    while True:

        symbol = input("Enter coin (example BTCUSDT) or 'exit': ").upper()

        if symbol == "EXIT":
            break

        if not symbol.endswith("USDT"):
            print("Use symbol like BTCUSDT or ETHUSDT\n")
            continue

        try:
            analyze(symbol)
        except Exception as e:
            print("Error analyzing", symbol, ":", e)

        time.sleep(2)


if __name__ == "__main__":
    main()