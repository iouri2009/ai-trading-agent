import time
import requests
import pandas as pd
import numpy as np
import ta

BASE_URL = "https://api.bytick.com"

# ═══════════════════════════════════════════════════════════════════
# SAFETY STUBS — survive TextEdit underscore mangling
# Both snake_case AND mangled camelCase covered
# ═══════════════════════════════════════════════════════════════════

def apply_macro_filter(final_state, confidence, global_ctx, is_altcoin=True):
    """Filter signals based on 4H macro trend."""
    t4h = global_ctx.get("trend4h", "NEUTRAL") if global_ctx else "NEUTRAL"
    fs = str(final_state).upper()
    direction = "LONG" if "LONG" in fs else "SHORT" if "SHORT" in fs else None
    if direction is None:
        return final_state, confidence
    if direction == "LONG" and t4h == "BEARISH":
        print(f"[MACRO FILTER] LONG blocked: 4H=BEARISH")
        return "NO TRADE", confidence
    if direction == "SHORT" and t4h == "BULLISH":
        print(f"[MACRO FILTER] SHORT blocked: 4H=BULLISH")
        return "NO TRADE", confidence
    if direction == "LONG" and str(confidence) == "MEDIUM" and t4h != "BULLISH":
        print(f"[MACRO FILTER] LONG MEDIUM blocked: 4H={t4h}")
        return "NO TRADE", confidence
    print(f"[MACRO FILTER] {direction} allowed: 4H={t4h} conf={confidence}")
    return final_state, confidence

def _stub(*a, **k): return "NONE"

# snake_case (correct names)
detect_breakdown_breakout = _stub
detect_retest             = _stub
get_continuation_signal   = _stub

# camelCase variants (what TextEdit produces after mangling)
detectbreakdownbreakout   = _stub
detectretest              = _stub
getcontinuationsignal     = _stub


# ═══════════════════════════════════════════════════════════════════
# LAYER 1 — DATA
# ═══════════════════════════════════════════════════════════════════

def safe_request(url, params, retries=3):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            if r.status_code != 200:
                time.sleep(1); continue
            data = r.json()
            if data.get("retCode") != 0:
                time.sleep(1); continue
            if "result" not in data or data["result"] is None:
                time.sleep(1); continue
            return data
        except Exception:
            time.sleep(1)
    return None


# ── Scan-level kline cache (reset each scan via clear_kline_cache()) ──
_kline_cache = {}

def clear_kline_cache():
    global _kline_cache
    _kline_cache = {}
    print(f"[CACHE] cleared")

def get_kline(symbol, interval):
    global _kline_cache
    _cache_key = (symbol, interval)
    if _cache_key in _kline_cache:
        return _kline_cache[_cache_key]
    data = safe_request(BASE_URL + "/v5/market/kline", {
        "category": "linear", "symbol": symbol,
        "interval": interval, "limit": 200
    })
    if not data:
        raise Exception(f"Kline failed [{interval}]")
    rows = data["result"]["list"]
    if not rows:
        raise Exception("No kline data")
    df = pd.DataFrame(rows).iloc[:, 0:6]
    df.columns = ["timestamp", "open", "high", "low", "close", "volume"]
    df = df.astype(float)
    _df = df[::-1].reset_index(drop=True)
    _kline_cache[_cache_key] = _df
    return _df


def get_open_interest(symbol):
    data = safe_request(BASE_URL + "/v5/market/open-interest", {
        "category": "linear", "symbol": symbol,
        "intervalTime": "5min", "limit": 10
    })
    if not data:
        return 0.0, False
    try:
        vals = [float(r["openInterest"]) for r in data["result"]["list"]]
        if len(vals) < 2:
            return 0.0, False
        pct = round((vals[0] - vals[1]) / vals[1] * 100, 4) if vals[1] else 0.0
        bleeding = all(vals[i] <= vals[i+1] for i in range(min(4, len(vals)-1)))
        return pct, bleeding
    except Exception:
        return 0.0, False


def get_funding(symbol):
    data = safe_request(BASE_URL + "/v5/market/funding/history", {
        "category": "linear", "symbol": symbol, "limit": 1
    })
    try:
        return float(data["result"]["list"][0]["fundingRate"])
    except Exception:
        return 0.0


def get_orderbook(symbol):
    data = safe_request(BASE_URL + "/v5/market/orderbook", {
        "category": "linear", "symbol": symbol, "limit": 50
    })
    try:
        bid = sum(float(b[1]) for b in data["result"]["b"])
        ask = sum(float(a[1]) for a in data["result"]["a"])
        tot = bid + ask
        return round((bid - ask) / tot, 4) if tot else 0.0
    except Exception:
        return 0.0


def get_trade_flow(symbol):
    data = safe_request(BASE_URL + "/v5/market/recent-trade", {
        "category": "linear", "symbol": symbol, "limit": 200
    })
    try:
        buys = sells = 0.0
        for t in data["result"]["list"]:
            val = float(t.get("size", 0)) * float(t.get("price", 1))
            if t.get("side") == "Buy":
                buys += val
            else:
                sells += val
        tot = buys + sells
        return round((buys - sells) / tot, 4) if tot else 0.0
    except Exception:
        return 0.0


def get_volume_data(symbol):
    data = safe_request(BASE_URL + "/v5/market/kline", {
        "category": "linear", "symbol": symbol,
        "interval": "D", "limit": 7
    })
    try:
        rows  = data["result"]["list"]
        vols  = [float(r[5]) for r in rows]
        v0, v1, v2 = vols[0], vols[1], vols[2]
        avg   = sum(vols) / len(vols)
        def pct(a, b): return round((a - b) / b * 100, 1) if b else 0.0
        vs_avg = pct(v0, avg)
        return {"vol_24h": v0, "vol_day1": v1, "vol_day2": v2,
                "vs_day1_pct": pct(v0, v1), "vs_day2_pct": pct(v0, v2),
                "vs_avg_pct": vs_avg}
    except Exception:
        return {"vol_24h": 0, "vol_day1": 0, "vol_day2": 0,
                "vs_day1_pct": 0, "vs_day2_pct": 0, "vs_avg_pct": 0}


# ═══════════════════════════════════════════════════════════════════
# LAYER 2 — INDICATORS
# ═══════════════════════════════════════════════════════════════════

def detect_trend(df):
    """
    Returns (direction, strength, state).
    direction : BULLISH | BEARISH | NEUTRAL
    strength  : STRONG | WEAK
    state     : TRENDING | RANGE | PULLBACK
    """
    if df is None or len(df) < 50:
        return "NEUTRAL", "WEAK", "RANGE"

    close = df["close"]
    ema20 = close.ewm(span=20).mean()
    ema50 = close.ewm(span=50).mean()

    price  = close.iloc[-1]
    e20    = ema20.iloc[-1]
    e50    = ema50.iloc[-1]
    e20_5  = ema20.iloc[-5]

    # Direction
    if price > e20 > e50:
        direction = "BULLISH"
    elif price < e20 < e50:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # Strength — slope of EMA20
    slope = (e20 - e20_5) / e20_5 * 100 if e20_5 else 0
    strength = "STRONG" if abs(slope) > 0.15 else "WEAK"

    # State
    high20 = df["high"].iloc[-20:].max()
    low20  = df["low"].iloc[-20:].min()
    rng    = (high20 - low20) / low20 * 100 if low20 else 0

    if direction != "NEUTRAL" and strength == "STRONG":
        state = "TRENDING"
    elif direction == "NEUTRAL" or rng < 1.5:
        state = "RANGE"
    else:
        # Pullback: direction exists but price counter to EMA
        if direction == "BEARISH" and price > e20:
            state = "PULLBACK"
        elif direction == "BULLISH" and price < e20:
            state = "PULLBACK"
        else:
            state = "TRENDING" if strength == "STRONG" else "RANGE"

    return direction, strength, state




def classify_1h_structure(df1h, trend4h, bos_1h):
    """
    Extended 1H phase: REVERSAL_ATTEMPT_UP/DOWN, PULLBACK_UP/DOWN, or base state.
    Priority: REVERSAL_ATTEMPT > PULLBACK > base state
    Returns (direction, strength, state, phase)
    """
    direction, strength, state = detect_trend(df1h)
    if df1h is None or len(df1h) < 30:
        return direction, strength, state, state
    try:
        highs  = df1h["high"].values
        lows   = df1h["low"].values
        closes = df1h["close"].values
        n      = len(closes)
        sh, sl = [], []
        for i in range(2, min(n-2, 30)):
            if highs[i]>highs[i-1] and highs[i]>highs[i-2] and highs[i]>highs[i+1] and highs[i]>highs[i+2]:
                sh.append((i, highs[i]))
            if lows[i]<lows[i-1] and lows[i]<lows[i-2] and lows[i]<lows[i+1] and lows[i]<lows[i+2]:
                sl.append((i, lows[i]))
        if len(sh) >= 2 and len(sl) >= 2:
            # REVERSAL ATTEMPT UP
            prior_dn   = sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]
            bull_candle= closes[-1] > highs[-2] and closes[-1] > closes[-2]
            bos_up     = bos_1h == "BULLISH" or closes[-1] > sh[-1][1]
            if prior_dn and bull_candle and bos_up:
                return direction, strength, "REVERSAL_ATTEMPT", "REVERSAL_ATTEMPT_UP"
            # REVERSAL ATTEMPT DOWN
            prior_up   = sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]
            bear_candle= closes[-1] < lows[-2] and closes[-1] < closes[-2]
            bos_dn     = bos_1h == "BEARISH" or closes[-1] < sl[-1][1]
            if prior_up and bear_candle and bos_dn:
                return direction, strength, "REVERSAL_ATTEMPT", "REVERSAL_ATTEMPT_DOWN"
            # PULLBACK UP (local bull in 4H downtrend, LH not broken)
            if trend4h in ("BEARISH","NEUTRAL") and direction == "BULLISH":
                if sh[-1][1] < sh[-2][1]:
                    return direction, strength, "PULLBACK", "PULLBACK_UP"
            # PULLBACK DOWN (local bear in 4H uptrend, HL not broken)
            if trend4h in ("BULLISH","NEUTRAL") and direction == "BEARISH":
                if sl[-1][1] > sl[-2][1]:
                    return direction, strength, "PULLBACK", "PULLBACK_DOWN"
        return direction, strength, state, state
    except Exception:
        return direction, strength, state, state


def detect_bos(df):
    """Break of Structure on 1H. Returns BULLISH | BEARISH | NONE."""
    if df is None or len(df) < 20:
        return "NONE"
    highs = df["high"].iloc[-20:-1]
    lows  = df["low"].iloc[-20:-1]
    close = df["close"].iloc[-1]
    if close > highs.max():
        return "BULLISH"
    if close < lows.min():
        return "BEARISH"
    return "NONE"


def detect_cvd_trend(df):
    """
    CVD absolute level + slope combined.
    Absolute level DOMINATES when extreme — prevents flat-slope misclassification.
    Returns: (direction, slope_label)
    """
    if df is None or len(df) < 30:
        return "NEUTRAL", "flat"
    try:
        delta = (df["close"] - df["open"]) * df["volume"]
        cvd   = delta.cumsum()
        level = cvd.iloc[-1]

        # Use 50-bar range for threshold
        window = min(50, len(cvd))
        recent = cvd.iloc[-window:]
        rng    = recent.max() - recent.min()
        threshold_strong = rng * 0.35  # top/bottom 35% = extreme
        threshold_weak   = rng * 0.15

        slope_val = cvd.iloc[-1] - cvd.iloc[-10]
        slope_lbl = "falling" if slope_val < -threshold_weak * 0.5 else \
                    ("rising"  if slope_val >  threshold_weak * 0.5 else "flat")

        # Absolute level is primary classifier
        if level < -threshold_strong:
            direction = "NEGATIVE"
            if slope_lbl == "rising":  slope_lbl = "bounce"
        elif level > threshold_strong:
            direction = "POSITIVE"
            if slope_lbl == "falling": slope_lbl = "fade"
        elif level < -threshold_weak:
            direction = "NEGATIVE"
        elif level > threshold_weak:
            direction = "POSITIVE"
        else:
            # Center zone — slope decides
            if slope_val < -threshold_weak * 0.3:
                direction = "NEGATIVE"
            elif slope_val > threshold_weak * 0.3:
                direction = "POSITIVE"
            else:
                direction = "NEUTRAL"
                slope_lbl = "flat"

        return direction, slope_lbl
    except Exception:
        return "NEUTRAL", "flat"


def detect_key_levels(df1h, df4h):
    """Support / resistance from recent swing highs/lows."""
    try:
        combined   = pd.concat([df4h["high"], df4h["low"]]).sort_values()
        price      = df1h["close"].iloc[-1]
        above      = combined[combined > price]
        below      = combined[combined < price]
        resistance = float(above.iloc[0])  if len(above) else price * 1.02
        support    = float(below.iloc[-1]) if len(below) else price * 0.98
        _atr_est   = float(price) * 0.012
        if resistance - price < _atr_est * 3:
            resistance = price + _atr_est * 4
        if price - support < _atr_est * 3:
            support = price - _atr_est * 4
        # Enforce minimum distance: 4x ATR from price
        _atr_est = atr if atr and atr > 0 else float(price) * 0.012
        if resistance - price < _atr_est * 3:
            resistance = price + _atr_est * 4
        if price - support < _atr_est * 3:
            support = price - _atr_est * 4
        return round(support, 6), round(resistance, 6)
    except Exception:
        price = df1h["close"].iloc[-1]
        return round(price * 0.98, 6), round(price * 1.02, 6)

def _count_touches(df, level, tolerance):
    touches = 0
    for _, row in df.iterrows():
        if abs(row["high"] - level) <= tolerance or abs(row["low"] - level) <= tolerance:
            touches += 1
    return touches

def detect_level_strength(df1h, df4h, support, resistance, atr):
    try:
        tol = atr * 0.3
        df  = df4h.iloc[-60:]
        res_t = _count_touches(df, resistance, tol)
        sup_t = _count_touches(df, support,    tol)
        rs = "WALL" if res_t>=3 else ("STRONG" if res_t>=2 else "NORMAL")
        ss = "WALL" if sup_t>=3 else ("STRONG" if sup_t>=2 else "NORMAL")
        return ss, rs
    except Exception:
        return "NORMAL", "NORMAL"

def detect_liquidity_zones(df1h, df4h, price, atr):
    try:
        tol   = atr * 0.5
        h4    = df4h.iloc[-50:]
        highs = h4["high"].values
        lows  = h4["low"].values
        buy_liq = sell_liq = None
        for i in range(len(highs)):
            c = [h for h in highs if abs(h - highs[i]) <= tol]
            if len(c) >= 2 and highs[i] > price:
                buy_liq = round(float(min(c)), 6); break
        for i in range(len(lows)):
            c = [l for l in lows if abs(l - lows[i]) <= tol]
            if len(c) >= 2 and lows[i] < price:
                sell_liq = round(float(max(c)), 6); break
        return {"buy_liquidity": buy_liq, "sell_liquidity": sell_liq}
    except Exception:
        return {"buy_liquidity": None, "sell_liquidity": None}

def classify_price_position(price, support, resistance, atr):
    rng = resistance - support
    if rng <= 0: return "MID_RANGE"
    pos = (price - support) / rng
    tol = atr / rng if rng else 0.1
    if pos >= 1 - tol:  return "AT_RESISTANCE"
    if pos <= tol:      return "AT_SUPPORT"
    if pos >= 0.70:     return "NEAR_RESISTANCE"
    if pos <= 0.30:     return "NEAR_SUPPORT"
    return "MID_RANGE"



# ═══════════════════════════════════════════════════════════════════
# ABSORPTION DETECTOR MODULE
# ═══════════════════════════════════════════════════════════════════

def detect_absorption(df1h, cvd_dir, price, support, resistance, atr, lookback=5):
    none_result = {"detected":False,"type":"NONE","strength":"NONE",
                   "flow":"NEUTRAL","pressure":"NEUTRAL","label":"BALANCED ⚖️"}
    if df1h is None or len(df1h) < lookback+2: return none_result
    try:
        closes=df1h["close"].values; highs=df1h["high"].values
        lows=df1h["low"].values;    opens=df1h["open"].values; n=len(closes)
        price_now=closes[-1]; price_prev=closes[-lookback]
        price_delta=(price_now-price_prev)/price_prev*100 if price_prev else 0
        cvd_deltas=[closes[i]-opens[i] for i in range(n-lookback,n)]
        cvd_net=sum(cvd_deltas)
        cvd_strong=abs(cvd_net)>atr*0.5
        wick_rejections=0; small_bodies=0
        for i in range(n-3,n):
            body=abs(closes[i]-opens[i]); total=highs[i]-lows[i]
            if total>0:
                br=body/total
                uw=highs[i]-max(closes[i],opens[i])
                lw=min(closes[i],opens[i])-lows[i]
                if uw>body*0.7: wick_rejections+=1
                if lw>body*0.7: wick_rejections+=1
                if br<0.35:     small_bodies+=1
        near_res=(resistance-price)<atr*1.5
        near_sup=(price-support)<atr*1.5
        cvd_bull=cvd_dir=="POSITIVE" or cvd_net>0
        cvd_bear=cvd_dir=="NEGATIVE" or cvd_net<0
        def score(cs,pw,rej,bod,nl):
            p=0
            if cs: p+=2
            if pw: p+=2
            if rej>=2: p+=2
            elif rej==1: p+=1
            if bod>=2: p+=1
            if nl: p+=1
            return "HIGH" if p>=6 else ("MEDIUM" if p>=4 else ("LOW" if p>=2 else "NONE"))
        if cvd_bull and cvd_strong:
            pw=abs(price_delta)<0.3
            st=score(cvd_strong,pw,wick_rejections,small_bodies,near_res)
            if st!="NONE":
                ic={"HIGH":"🧱🧱","MEDIUM":"🧱","LOW":"⚠️"}.get(st,"")
                return {"detected":True,"type":"SELL_INTO_BUYERS","strength":st,
                        "flow":"BUYING","pressure":"ABSORPTION",
                        "label":f"ABSORPTION {ic} — sell into buying (Strength: {st})"}
        if cvd_bear and cvd_strong:
            pw=abs(price_delta)<0.3
            st=score(cvd_strong,pw,wick_rejections,small_bodies,near_sup)
            if st!="NONE":
                ic={"HIGH":"🧱🧱","MEDIUM":"🧱","LOW":"⚠️"}.get(st,"")
                return {"detected":True,"type":"BUY_INTO_SELLERS","strength":st,
                        "flow":"SELLING","pressure":"ABSORPTION",
                        "label":f"ABSORPTION {ic} — buy into selling (Strength: {st})"}
        if cvd_bull and price_delta>0.2:
            return {**none_result,"flow":"BUYING","pressure":"TRUE BUY","label":"TRUE BUYING 🟢"}
        if cvd_bear and price_delta<-0.2:
            return {**none_result,"flow":"SELLING","pressure":"TRUE SELL","label":"TRUE SELLING 🔴"}
        return none_result
    except Exception: return none_result

def format_pressure_line(abs_ctx):
    """Absorption overrides TRUE flow. TRUE cannot coexist with absorption."""
    if abs_ctx["detected"]:
        strength = abs_ctx["strength"]
        icon = {"HIGH": "🧱🧱", "MEDIUM": "🧱", "LOW": "⚠️"}.get(strength, "⚠️")
        flow = abs_ctx["flow"]
        return f"Pressure: ABSORPTION {icon} (Strength: {strength}) | Quality: WEAK\n"
    if abs_ctx["pressure"] == "TRUE BUY":
        return "Pressure: TRUE BUYING 🟢 | Quality: STRONG\n"
    if abs_ctx["pressure"] == "TRUE SELL":
        return "Pressure: TRUE SELLING 🔴 | Quality: STRONG\n"
    return ""


def classify_flow_cvd(flow, cvd_dir, price, resistance, support, atr):
    near_res = (resistance - price) < atr * 1.5
    near_sup = (price - support)   < atr * 1.5
    cvd_bull = cvd_dir == "POSITIVE"
    cvd_bear = cvd_dir == "NEGATIVE"
    flow_bull = flow > 0.1
    flow_bear = flow < -0.1
    if cvd_bull and flow_bull and not near_res: return "TRUE BUYING 🟢"
    if cvd_bear and flow_bear and not near_sup: return "TRUE SELLING 🔴"
    if cvd_bull and near_res:  return "ABSORPTION 🧱 (buyers sold into resistance)"
    if cvd_bear and near_sup:  return "DISTRIBUTION TRAP 🪤 (sellers at support)"
    if flow_bull and cvd_bear: return "LIQUIDITY TRAP 🪤 (buy into bearish CVD)"
    if flow_bear and cvd_bull: return "LIQUIDITY TRAP 🪤 (sell into bullish CVD)"
    if cvd_bull: return "BUYING 📈"
    if cvd_bear: return "SELLING 📉"
    return "BALANCED ⚖️"

def build_liquidity_context(df1h, df4h, price, support, resistance, atr, flow, cvd_dir):
    try:
        ss, rs = detect_level_strength(df1h, df4h, support, resistance, atr)
        lz     = detect_liquidity_zones(df1h, df4h, price, atr)
        pos    = classify_price_position(price, support, resistance, atr)
        fl     = classify_flow_cvd(flow, cvd_dir, price, resistance, support, atr)
        adj = 0
        if rs in ("STRONG","WALL") and pos in ("AT_RESISTANCE","NEAR_RESISTANCE"): adj -= 10
        if ss in ("STRONG","WALL") and pos in ("AT_SUPPORT","NEAR_SUPPORT"):       adj -= 8
        if pos == "MID_RANGE":    adj -= 5
        if "ABSORPTION" in fl:    adj -= 8
        if "TRUE BUYING"  in fl and pos in ("AT_SUPPORT","NEAR_SUPPORT"):    adj += 8
        if "TRUE SELLING" in fl and pos in ("AT_RESISTANCE","NEAR_RESISTANCE"): adj += 8
        return {"support_strength":ss,"resistance_strength":rs,
                "buy_liquidity":lz["buy_liquidity"],"sell_liquidity":lz["sell_liquidity"],
                "position":pos,"flow_label":fl,"confidence_adj":adj}
    except Exception:
        return {"support_strength":"NORMAL","resistance_strength":"NORMAL",
                "buy_liquidity":None,"sell_liquidity":None,
                "position":"MID_RANGE","flow_label":"BALANCED ⚖️","confidence_adj":0}

def format_liquidity_block(liq_ctx, fmt, support, resistance):
    ss  = liq_ctx["support_strength"]
    rs  = liq_ctx["resistance_strength"]
    pos = liq_ctx["position"]
    bl  = liq_ctx["buy_liquidity"]
    sl  = liq_ctx["sell_liquidity"]
    fl  = liq_ctx["flow_label"]
    ri  = "🧱" if rs=="WALL" else ("⚠️" if rs=="STRONG" else "")
    si  = "🧱" if ss=="WALL" else ("⚠️" if ss=="STRONG" else "")
    pl  = {"AT_RESISTANCE":"🔴 AT RESISTANCE","NEAR_RESISTANCE":"🟠 NEAR RESISTANCE",
           "AT_SUPPORT":"🟢 AT SUPPORT","NEAR_SUPPORT":"🟡 NEAR SUPPORT","MID_RANGE":"⚪️ MID RANGE"}
    lines = [
        f"\n💎 *LIQUIDITY*",
        f"Resistance: `{fmt(resistance)}` {ri} `{rs}`  Support: `{fmt(support)}` {si} `{ss}`",
        f"Position: {pl.get(pos, pos)}",
        f"Flow: {fl}",
    ]
    if bl: lines.append(f"Buy-side liq: `{fmt(bl)}` (equal highs above)")
    if sl: lines.append(f"Sell-side liq: `{fmt(sl)}` (equal lows below)")
    return "\n".join(lines) + "\n"



def detect_breakdown_breakout(df, support, resistance, cvd_trend, volume_spike):
    """Detect breakdown / breakout events."""
    try:
        price = df["close"].iloc[-1]
        if price < support and cvd_trend == "NEGATIVE":
            return "BREAKDOWN"
        if price > resistance and cvd_trend == "POSITIVE":
            return "BREAKOUT"
        return "NONE"
    except Exception:
        return "NONE"


# ═══════════════════════════════════════════════════════════════════
# LAYER 3 — DIRECTION ENGINE
# ═══════════════════════════════════════════════════════════════════

# Smoothing state — persists between calls within a session
_ema_confidence = None
_ema_trend_score = None

def get_dominant_direction(trend4h, trend1h, trend1d, bos_1h="NONE",
                           cvd_dir="NEUTRAL", flow=0.0, trend_score=50):
    """
    4H is the anchor.
    BOS can only override if ALSO confirmed by CVD or flow.
    Single BOS alone cannot flip dominant direction.
    """
    # Primary: 4H direction
    if trend4h == "BEARISH":
        base = "SHORT"
    elif trend4h == "BULLISH":
        base = "LONG"
    elif trend1d == "BEARISH":
        base = "SHORT"
    elif trend1d == "BULLISH":
        base = "LONG"
    else:
        base = "WEAK"

    # BOS override ONLY if confirmed by CVD OR flow (multi-signal)
    if bos_1h == "BULLISH" and base == "SHORT":
        cvd_confirms  = cvd_dir == "POSITIVE"
        flow_confirms = flow > 0.2
        if cvd_confirms or flow_confirms:
            # Still keep SHORT — BOS in pullback, not trend change
            # Just note it as SHORT RETEST (handled in classify_state)
            pass
        # BOS alone never flips dominant

    if bos_1h == "BEARISH" and base == "LONG":
        # Same — BOS alone cannot flip LONG to SHORT
        pass

    return base


def build_trend_score(trend1h, strength1h, state1h,
                      trend4h, strength4h, state4h,
                      trend1d, strength1d):
    """
    0-100 score. Weighted: 4H=50%, 1H=30%, 1D=20%.
    Each timeframe contributes direction + strength + state.
    EMA-smoothed across calls for stability.
    """
    global _ema_trend_score

    def tf_score(direction, strength, state):
        base  = 50
        d     = 20 if direction == "BULLISH" else (-20 if direction == "BEARISH" else 0)
        s     = 10 if strength == "STRONG"   else -5
        st    = 5  if state == "TRENDING"    else (-5 if state == "RANGE" else 0)
        return max(0, min(100, base + d + s + st))

    s1h = tf_score(trend1h, strength1h, state1h)
    s4h = tf_score(trend4h, strength4h, state4h)
    s1d = tf_score(trend1d, strength1d, "TRENDING")  # 1D state not tracked

    raw = s4h * 0.5 + s1h * 0.3 + s1d * 0.2

    # EMA smoothing (period=3) — prevents score jumping
    alpha = 0.4
    if _ema_trend_score is None:
        _ema_trend_score = raw
    else:
        _ema_trend_score = alpha * raw + (1 - alpha) * _ema_trend_score

    return round(_ema_trend_score)


def build_confidence(dominant_dir, trend_score, bos_1h,
                     cvd_trend, flow, alignment, trap_detected, vol_spike):
    """
    Confidence 0-100. EMA-smoothed (period=3).
    BOS = +12 if confirms dominant, -5 if contradicts.
    LIQUIDITY TRAP = -12.
    """
    global _ema_confidence

    base  = trend_score
    bonus = 0

    # BOS confirms dominant
    if bos_1h == "BEARISH" and dominant_dir == "SHORT":
        bonus += 12
    elif bos_1h == "BULLISH" and dominant_dir == "LONG":
        bonus += 12
    elif bos_1h != "NONE":
        bonus -= 5  # contradicts

    # CVD confirms
    if dominant_dir == "SHORT" and cvd_trend == "NEGATIVE":
        bonus += 8
    elif dominant_dir == "LONG" and cvd_trend == "POSITIVE":
        bonus += 8
    elif cvd_trend != "NEUTRAL":
        bonus -= 3

    # Flow confirms
    if dominant_dir == "SHORT" and flow < -0.1:
        bonus += 6
    elif dominant_dir == "LONG" and flow > 0.1:
        bonus += 6

    # Alignment
    if alignment in ("STRONG_BEAR", "STRONG_BULL"):
        bonus += 5
    elif alignment == "MIXED":
        bonus -= 8

    # Volume spike
    if vol_spike:
        bonus += 4

    # Liquidity trap penalty
    if trap_detected:
        bonus -= 12

    raw = max(0, min(100, base + bonus))

    # EMA smoothing
    alpha = 0.4
    if _ema_confidence is None:
        _ema_confidence = raw
    else:
        _ema_confidence = alpha * raw + (1 - alpha) * _ema_confidence

    return round(_ema_confidence)


def build_alignment(trend1h, trend4h, trend1d):
    """Alignment label for display."""
    score = 0
    score += 1 if trend1h == "BULLISH" else (-1 if trend1h == "BEARISH" else 0)
    score += 2 if trend4h == "BULLISH" else (-2 if trend4h == "BEARISH" else 0)
    score += 3 if trend1d == "BULLISH" else (-3 if trend1d == "BEARISH" else 0)
    if score >= 5:   return "STRONG_BULL"
    if score >= 2:   return "BULLISH"
    if score <= -5:  return "STRONG_BEAR"
    if score <= -2:  return "BEARISH"
    return "MIXED"


def classify_state(dominant_dir, bos_1h, trend4h, trend1h,
                   state1h, state4h, confidence, breakdown):
    """
    Returns (state, sig_type).

    ALLOWED STATES:
    STRONG SHORT / SHORT / WEAK SHORT / SHORT RETEST
    STRONG LONG  / LONG  / WEAK LONG  / LONG RETEST
    RANGE

    RULES:
    - Pullback (4H bear + BOS bull) → SHORT RETEST — NEVER flips to LONG
    - State always matches dominant_dir
    - No contradictions
    """
    # Breakdown/breakout event
    if breakdown in ("BREAKDOWN", "BREAKOUT"):
        d = "SHORT" if breakdown == "BREAKDOWN" else "LONG"
        return d, "BREAKDOWN"

    # Pullback pattern — 4H bearish + 1H bouncing + BOS bullish
    if dominant_dir == "SHORT" and bos_1h == "BULLISH" and trend4h == "BEARISH":
        return "SHORT RETEST", "PULLBACK"

    if dominant_dir == "LONG" and bos_1h == "BEARISH" and trend4h == "BULLISH":
        return "LONG RETEST", "PULLBACK"

    # Mode-based type
    if state1h == "PULLBACK" or state4h == "PULLBACK":
        sig_type = "PULLBACK"
    elif state4h == "TRENDING":
        sig_type = "TREND"
    else:
        sig_type = "RANGE"

    # Full trend alignment
    if dominant_dir == "SHORT":
        if confidence >= 65:
            return "STRONG SHORT", sig_type
        if confidence >= 50:
            return "SHORT", sig_type
        return "WEAK SHORT", sig_type

    if dominant_dir == "LONG":
        if confidence >= 65:
            return "STRONG LONG", sig_type
        if confidence >= 50:
            return "LONG", sig_type
        return "WEAK LONG", sig_type

    return "RANGE", "RANGE"


def build_probability(dominant_dir, trend1h, trend4h, trend1d,
                      rsi, bos_1h, cvd_trend, flow, funding, state):
    """
    Bull/Bear probabilities.
    HARD CLAMP: must align with dominant_dir and state.
    SHORT state → bull ≤ 45
    LONG state  → bull ≥ 55
    """
    bull = 50
    bull += 12 if trend4h == "BULLISH" else (-12 if trend4h == "BEARISH" else 0)
    bull += 8  if trend1h == "BULLISH" else (-8  if trend1h == "BEARISH" else 0)
    bull += 6  if trend1d == "BULLISH" else (-6  if trend1d == "BEARISH" else 0)
    bull += 4  if rsi > 55 else (-4 if rsi < 45 else 0)
    bull += 8  if bos_1h == "BULLISH" else (-8 if bos_1h == "BEARISH" else 0)
    bull += 6  if cvd_trend == "POSITIVE" else (-6 if cvd_trend == "NEGATIVE" else 0)
    bull += 4  if flow > 0.1 else (-4 if flow < -0.1 else 0)
    bull += 2  if funding < -0.001 else (-2 if funding > 0.001 else 0)
    bull = max(0, min(100, bull))

    # HARD CLAMP — no exceptions, threshold = 50
    # Uses BOTH dominant_dir AND state for double safety
    is_short_state = "SHORT" in state
    is_long_state  = "LONG"  in state

    if dominant_dir == "SHORT" or is_short_state:
        bull = min(bull, 45)
    elif dominant_dir == "LONG" or is_long_state:
        bull = max(bull, 55)

    return round(bull, 1), round(100 - bull, 1)


# ═══════════════════════════════════════════════════════════════════
# LAYER 4 — TRADE PLAN
# ═══════════════════════════════════════════════════════════════════

def build_trade_plan(state, price, support, resistance, atr, fmt):
    """
    Direction derived STRICTLY from state.
    LONG states → LONG plan. SHORT states → SHORT plan.
    No external direction argument — eliminates state/plan contradiction.
    """
    if not atr or atr == 0 or state == "RANGE":
        return None

    is_short = "SHORT" in state
    is_long  = "LONG"  in state

    if not is_short and not is_long:
        return None

    # Derive direction from state — single source of truth
    direction = "SHORT" if is_short else "LONG"

    if is_short:
        entry_low  = price - atr * 0.2
        entry_high = price + atr * 0.3
        stop_loss  = resistance + atr * 0.5
        _sl_dist   = abs(price - stop_loss)
        tp1 = price - max(_sl_dist * 1.0, atr * 1.0)   # TP1 >= 1R
        tp2 = price - max(_sl_dist * 2.0, atr * 3.0)
        tp3 = price - max(_sl_dist * 3.0, atr * 5.0)
        rr1 = round((price - tp1) / (stop_loss - price), 2) if stop_loss != price else 0
        rr2 = round((price - tp2) / (stop_loss - price), 2) if stop_loss != price else 0
    else:
        entry_low  = price - atr * 0.3
        entry_high = price + atr * 0.2
        stop_loss  = support  - atr * 0.5
        _sl_dist   = abs(price - stop_loss)
        tp1 = price + max(_sl_dist * 1.0, atr * 1.0)   # TP1 >= 1R
        tp2 = price + max(_sl_dist * 2.0, atr * 3.0)
        tp3 = price + max(_sl_dist * 3.0, atr * 5.0)
        rr1 = round((tp1 - price) / (price - stop_loss), 2) if stop_loss != price else 0
        rr2 = round((tp2 - price) / (price - stop_loss), 2) if stop_loss != price else 0

    return {
        "direction":  direction,
        "entry_low":  fmt(round(entry_low,  6)),
        "entry_high": fmt(round(entry_high, 6)),
        "stop_loss":  fmt(round(stop_loss,  6)),
        "tp1": fmt(round(tp1, 6)),
        "tp2": fmt(round(tp2, 6)),
        "tp3": fmt(round(tp3, 6)),
        "rr1": str(rr1),
        "rr2": str(rr2),
    }


# ═══════════════════════════════════════════════════════════════════
# LAYER 5 — EXECUTION BLOCK
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# ENTRY ENGINE — BREAKOUT → PULLBACK MODEL
# ═══════════════════════════════════════════════════════════════════

def _detect_micro_structure(df5m):
    if df5m is None or len(df5m) < 10: return "NEUTRAL"
    try:
        h = df5m["high"].values[-10:]; l = df5m["low"].values[-10:]
        n = len(h); sh, sl = [], []
        for i in range(1, n-1):
            if h[i]>h[i-1] and h[i]>h[i+1]: sh.append(h[i])
            if l[i]<l[i-1] and l[i]<l[i+1]: sl.append(l[i])
        if len(sh)>=2 and len(sl)>=2:
            if sh[-1]>sh[-2] and sl[-1]>sl[-2]: return "BULL"
            if sh[-1]<sh[-2] and sl[-1]<sl[-2]: return "BEAR"
        return "NEUTRAL"
    except Exception: return "NEUTRAL"

def _check_breakout_quality(df1h, level, direction, atr):
    try:
        c1=float(df1h["close"].iloc[-1]); c2=float(df1h["close"].iloc[-2])
        b1=abs(df1h["close"].iloc[-1]-df1h["open"].iloc[-1])
        if direction=="UP":
            if c2>level and c1>c2: return "CLEAN" if b1>atr*0.3 else "WEAK"
            if c1>level: return "WEAK"
        else:
            if c2<level and c1<c2: return "CLEAN" if b1>atr*0.3 else "WEAK"
            if c1<level: return "WEAK"
        return "NONE"
    except Exception: return "NONE"

def _check_pullback_formed(df1h, level, direction, atr):
    try:
        price=float(df1h["close"].iloc[-1]); near=abs(price-level)<atr*1.0
        if direction=="UP":  return near and price>level*0.995
        else:                return near and price<level*1.005
    except Exception: return False

def build_entry_context(df1h, df5m, price, support, resistance, atr,
                        final_state, liq_ctx, cvd_dir, flow, vol_spike, bos_1h):
    disabled = {"enabled":False,"entry_type":"NONE","status":"DISABLED",
                "level":0.0,"direction":"NONE","micro_struct":"NEUTRAL",
                "confidence_adj":0,"reason":""}
    if final_state == "RANGE": return {**disabled,"reason":"State is RANGE"}
    pos = liq_ctx.get("position","MID_RANGE")
    if pos == "MID_RANGE": return {**disabled,"reason":"Price in mid-range"}
    res_str=liq_ctx.get("resistance_strength","NORMAL")
    sup_str=liq_ctx.get("support_strength","NORMAL")
    flow_lbl=liq_ctx.get("flow_label","")
    is_long="LONG" in final_state; is_short="SHORT" in final_state
    if is_long  and res_str=="WALL" and pos in ("AT_RESISTANCE","NEAR_RESISTANCE"):
        return {**disabled,"reason":"LONG blocked: resistance WALL ahead"}
    if is_short and sup_str=="WALL" and pos in ("AT_SUPPORT","NEAR_SUPPORT"):
        return {**disabled,"reason":"SHORT blocked: support WALL ahead"}
    if "ABSORPTION" in flow_lbl:
        return {**disabled,"reason":"Absorption detected"}
    direction="LONG" if is_long else ("SHORT" if is_short else "NONE")
    if direction=="NONE": return {**disabled,"reason":"No directional state"}
    micro=_detect_micro_structure(df5m)
    cvd_bull=cvd_dir=="POSITIVE"; cvd_bear=cvd_dir=="NEGATIVE"
    flow_valid=True
    if direction=="LONG"  and cvd_bear and flow<-0.1: flow_valid=False
    if direction=="SHORT" and cvd_bull and flow>0.1:  flow_valid=False
    if direction=="LONG":
        level=resistance; bo=_check_breakout_quality(df1h,level,"UP",atr)
        pb=_check_pullback_formed(df1h,level,"UP",atr)
    else:
        level=support; bo=_check_breakout_quality(df1h,level,"DOWN",atr)
        pb=_check_pullback_formed(df1h,level,"DOWN",atr)
    if bo in ("CLEAN","WEAK") and pb:
        et="PULLBACK"
        st="TRIGGERED" if micro==("BULL" if direction=="LONG" else "BEAR") and flow_valid else "WAITING"
    elif bo=="CLEAN" and vol_spike and flow_valid:
        et="BREAKOUT"
        bos_match=("BULLISH" if direction=="LONG" else "BEARISH")
        st="TRIGGERED" if bos_1h==bos_match else "WAITING"
    elif pos in (("NEAR_RESISTANCE","AT_RESISTANCE") if direction=="LONG" else ("NEAR_SUPPORT","AT_SUPPORT")):
        et="PULLBACK"; st="WAITING"
    else:
        et="NONE"; st="WAITING"
    adj=0
    if et=="PULLBACK" and st=="TRIGGERED": adj+=10
    if et=="BREAKOUT" and st=="TRIGGERED": adj+=6
    if bo=="CLEAN": adj+=5
    if bo=="WEAK":  adj-=3
    if micro==("BULL" if direction=="LONG" else "BEAR"): adj+=5
    if not flow_valid: adj-=8
    if not vol_spike:  adj-=3
    if res_str=="STRONG" and direction=="LONG":  adj-=5
    if sup_str=="STRONG" and direction=="SHORT": adj-=5
    return {"enabled":True,"entry_type":et,"status":st,"level":level,
            "direction":direction,"micro_struct":micro,"confidence_adj":adj,
            "reason":f"{et}|BO:{bo}|Micro:{micro}"}

def format_entry_block(entry_ctx, fmt, price, atr):
    if not entry_ctx.get("enabled"): return ""
    et=entry_ctx["entry_type"]; st=entry_ctx["status"]
    level=entry_ctx["level"]; d=entry_ctx["direction"]; micro=entry_ctx["micro_struct"]
    if et=="NONE": return ""
    si={"TRIGGERED":"🟢 TRIGGERED","WAITING":"🟡 WAITING","INVALIDATED":"🔴 INVALIDATED"}
    ml={"BULL":"🟢 BULL","BEAR":"🔴 BEAR","NEUTRAL":"⚪️ NEUTRAL"}.get(micro,micro)
    dl="🟢 LONG" if d=="LONG" else "🔴 SHORT"
    lines=[f"\n🎯 *ENTRY ENGINE*",
           f"Type: `{et}`  Direction: {dl}  Status: {si.get(st,st)}",
           f"Level: `{fmt(level)}`  Micro: {ml}"]
    if d=="LONG":
        lines.append(f"Wait for: Pullback to `{fmt(level)}` → bullish reaction → close above")
        lines.append(f"Invalidate: Close back below `{fmt(level)}` | absorption | LH forms")
    else:
        lines.append(f"Wait for: Pullback to `{fmt(level)}` → bearish reaction → close below")
        lines.append(f"Invalidate: Close back above `{fmt(level)}` | absorption | HL forms")
    return "\n".join(lines)+"\n"




# ═══════════════════════════════════════════════════════════════════
# ATTACK MODE — LIQUIDITY BREAK ACTIVATION ENGINE
# States: OFF → ARMED → ACTIVE → CANCELLED
# ═══════════════════════════════════════════════════════════════════

def build_attack_mode(df1h, df5m, price, support, resistance, atr,
                      final_state, liq_ctx, entry_ctx,
                      cvd_dir, flow, vol_spike, bos_1h, alignment):
    off = {"status":"OFF","direction":"NONE","level":0.0,"trigger":"","confidence_adj":0}
    if final_state == "RANGE": return {**off}
    pos = liq_ctx.get("position","MID_RANGE")
    if pos == "MID_RANGE": return {**off}
    is_long="LONG" in final_state; is_short="SHORT" in final_state
    if not (is_long or is_short): return {**off}
    direction = "LONG" if is_long else "SHORT"
    htf_ok = alignment in ("STRONG_BULL","STRONG_BEAR","BULLISH","BEARISH")
    if not htf_ok: return {**off}
    buy_liq  = liq_ctx.get("buy_liquidity")
    sell_liq = liq_ctx.get("sell_liquidity")
    res_str  = liq_ctx.get("resistance_strength","NORMAL")
    sup_str  = liq_ctx.get("support_strength","NORMAL")
    flow_lbl = liq_ctx.get("flow_label","")
    if direction=="LONG":
        watch_level = buy_liq if buy_liq and buy_liq < resistance else resistance
    else:
        watch_level = sell_liq if sell_liq and sell_liq > support else support
    if not watch_level or watch_level <= 0: return {**off}
    try:
        c1=float(df1h["close"].iloc[-1]); c2=float(df1h["close"].iloc[-2])
        h1=float(df1h["high"].iloc[-1]);  l1=float(df1h["low"].iloc[-1])
        body1=abs(c1-float(df1h["open"].iloc[-1]))
        rng=h1-l1
        wick_ratio=(h1-c1)/rng if direction=="LONG" and rng>0 else ((c1-l1)/rng if rng>0 else 0)
    except Exception:
        return {**off}
    cvd_bull=cvd_dir=="POSITIVE"; cvd_bear=cvd_dir=="NEGATIVE"
    absorption="ABSORPTION" in flow_lbl
    if direction=="LONG":
        broke_out=c2>watch_level and c1>watch_level
        held=c1>watch_level*0.998
        no_reject=wick_ratio<0.5
        cvd_ok=cvd_bull and not absorption
        pb_forming=abs(c1-watch_level)<atr*0.8
        if broke_out and held and no_reject and cvd_ok:
            adj=12 if (body1>atr*0.2 and vol_spike) else 6
            t=f"Breakout above `{c2:.4f}` confirmed — watching pullback to `{watch_level:.4f}`"
            return {"status":"ACTIVE","direction":direction,"level":watch_level,
                    "trigger":t,"confidence_adj":adj+(5 if pb_forming else 0)}
        if c2>watch_level and c1<watch_level:
            return {"status":"CANCELLED","direction":direction,"level":watch_level,
                    "trigger":"Breakout failed — price returned below level","confidence_adj":-10}
    else:
        broke_out=c2<watch_level and c1<watch_level

def format_attack_block(attack, fmt):
    status=attack.get("status","OFF")
    if status=="OFF": return ""
    d=attack["direction"]; level=attack["level"]; trigger=attack["trigger"]
    icons={"ARMED":"⚡️ ARMED","ACTIVE":"🔴 ACTIVE","CANCELLED":"❌ CANCELLED"}
    dl="🟢 LONG" if d=="LONG" else "🔴 SHORT"
    lines=[f"\n⚔️ *ATTACK MODE: {icons.get(status,status)}*",
           f"Direction: {dl}  Level: `{fmt(level)}`",f"{trigger}"]
    if status=="ACTIVE":
        if d=="LONG":
            lines.append("Entry: Pullback to level → HL on 5M → close above → ENTER")
            lines.append("Stop: Close back below level | absorption | rejection")
        else:
            lines.append("Entry: Pullback to level → LH on 5M → close below → ENTER")
            lines.append("Stop: Close back above level | absorption | rejection")
    elif status=="ARMED":
        lines.append("Waiting for liquidity break — do NOT enter before trigger")
    return "\n".join(lines)+"\n"




def fmt_level(v):
    if v < 1:   return f"{v:.5f}"
    if v < 10:  return f"{v:.4f}"
    if v < 100: return f"{v:.3f}"
    return f"{v:.2f}"

def build_trade_intent(price, support, resistance, atr,
                       final_state, liq_ctx, attack, entry_ctx,
                       in_trend=False, trade_ready=False):
    pos        = liq_ctx.get("position","MID_RANGE")
    buy_liq    = liq_ctx.get("buy_liquidity")
    sell_liq   = liq_ctx.get("sell_liquidity")
    res_str    = liq_ctx.get("resistance_strength","NORMAL")
    sup_str    = liq_ctx.get("support_strength","NORMAL")
    atk_status = attack.get("status","OFF")
    none_intent = {"direction":"NONE","level":0.0,"level_type":"",
                   "condition":"NO TRADE — no clear setup","status":"WAIT"}
    # Attack active
    # TREND INTENT
    if in_trend:
        _d = "LONG" if "LONG" in final_state else ("SHORT" if "SHORT" in final_state else "NONE")
        if _d != "NONE":
            _bl=liq_ctx.get("buy_liquidity"); _sl=liq_ctx.get("sell_liquidity")
            _lv=_bl if (_d=="LONG" and _bl) else (_sl if (_d=="SHORT" and _sl) else (resistance if _d=="LONG" else support))
            _st="LOOK_FOR_ENTRY" if trade_ready else "WAIT"
            _cond=f"Trend — {'near edge' if trade_ready else 'mid-range'} — pullback + {'HL' if _d=='LONG' else 'LH'} on 5M"
            return {"direction":_d,"level":_lv if _lv else price,"level_type":"trend zone","condition":_cond,"status":_st}

    if atk_status == "ACTIVE":
        d=attack["direction"]; l=attack["level"]
        return {"direction":d,"level":l,"level_type":"breakout level",
                "condition":(f"Pullback to {l:.4f} forming — "
                             f"wait for {'HL on 5M + close above' if d=='LONG' else 'LH on 5M + close below'}"),
                "status":"TRIGGERED"}
    if atk_status == "ARMED":
        d=attack["direction"]; l=attack["level"]
        return {"direction":d,"level":l,"level_type":"liquidity level",
                "condition":(f"Wait for close {'above' if d=='LONG' else 'below'} {l:.4f} "
                             f"with CVD {'positive' if d=='LONG' else 'negative'} then pullback"),
                "status":"READY"}
    # Entry triggered
    if entry_ctx.get("enabled") and entry_ctx.get("status")=="TRIGGERED":
        d=entry_ctx["direction"]; l=entry_ctx["level"]
        return {"direction":d,"level":l,"level_type":"entry level",
                "condition":(f"Pullback to {l:.4f} confirmed — "
                             f"enter on {'bullish reaction + close above' if d=='LONG' else 'bearish reaction + close below'}"),
                "status":"TRIGGERED"}
    # Directional state
    if final_state != "RANGE":
        if "LONG" in final_state:
            level = buy_liq if buy_liq and buy_liq < resistance else resistance
            return {"direction":"LONG","level":level,
                    "level_type":"buy-side liquidity" if buy_liq and buy_liq<resistance else "resistance",
                    "condition":f"Watch for break + close above {level:.4f} then pullback then LONG",
                    "status":"READY" if pos in ("NEAR_RESISTANCE","AT_RESISTANCE") else "WAIT"}
        if "SHORT" in final_state:
            level = sell_liq if sell_liq and sell_liq > support else support
            return {"direction":"SHORT","level":level,
                    "level_type":"sell-side liquidity" if sell_liq and sell_liq>support else "support",
                    "condition":f"Watch for break + close below {level:.4f} then pullback then SHORT",
                    "status":"READY" if pos in ("NEAR_SUPPORT","AT_SUPPORT") else "WAIT"}
    # Range state
    if pos == "MID_RANGE":
        return {"direction":"NONE","level":0.0,"level_type":"",
                "condition":"NO TRADE — price in middle of range","status":"WAIT"}
    if pos in ("AT_SUPPORT","NEAR_SUPPORT"):
        level = buy_liq if buy_liq else resistance
        return {"direction":"LONG","level":level,
                "level_type":"buy-side liquidity" if buy_liq else "resistance target",
                "condition":(f"Price near support {fmt_level(support)} — "
                             f"watch for break above {level:.4f} then pullback then LONG"),
                "status":"READY" if sup_str in ("STRONG","WALL") else "WAIT"}
    if pos in ("AT_RESISTANCE","NEAR_RESISTANCE"):
        level = sell_liq if sell_liq else support
        return {"direction":"SHORT","level":level,
                "level_type":"sell-side liquidity" if sell_liq else "support target",
                "condition":(f"Price near resistance {fmt_level(resistance)} — "
                             f"watch for break below {level:.4f} then pullback then SHORT"),
                "status":"READY" if res_str in ("STRONG","WALL") else "WAIT"}
    return none_intent

def format_trade_intent(intent, fmt):
    d=intent["direction"]; level=intent["level"]
    lt=intent["level_type"]; cond=intent["condition"]; st=intent["status"]
    si={"WAIT":"⏳ WAIT","READY":"👁 READY","TRIGGERED":"⚡️ TRIGGERED","LOOK_FOR_ENTRY":"🎯 LOOK FOR ENTRY","FOLLOW_TREND":"🚀 FOLLOW TREND","WAIT_FOR_PULLBACK":"↩️ WAIT FOR PULLBACK"}
    di={"LONG":"🟢 LONG","SHORT":"🔴 SHORT","NONE":"⚪️ NONE"}
    lines=[f"\n📌 *TRADE INTENT*"]
    if d=="NONE":
        lines.append(f"Direction: ⚪️ NONE  Status: {si.get(st,st)}")
        lines.append(f"{cond}")
    else:
        ls=f"`{fmt(level)}`" if level>0 else "—"
        lines.append(f"Direction: {di.get(d,d)}  Level: {ls}" + (f" ({lt})" if lt else ""))
        lines.append(f"Condition: {cond}")
        lines.append(f"Status: {si.get(st,st)}")
    return "\n".join(lines)+"\n"


def build_execution(state, sig_type, confidence, quality,
                    price, support, resistance, atr,
                    bos_1h, cvd_trend, rsi, flow,
                    trade_plan, fmt):
    """
    Execution guidance block.
    Only shown when confidence ≥ 45 and quality != LOW (unless confidence ≥ 60).
    Status = READY when BOS + CVD both confirm direction.
    """
    # Gate — strict
    if state == "RANGE":
        return ""
    if quality == "LOW":
        return ""
    if confidence < 45:
        return ""

    is_short = "SHORT" in state

    # Entry zone — use trade plan if available, else ATR fallback
    if trade_plan:
        entry_zone = f"{trade_plan['entry_low']} — {trade_plan['entry_high']}"
        sl  = trade_plan['stop_loss']
        tp1 = trade_plan['tp1']
        tp2 = trade_plan['tp2']
        tp3 = trade_plan['tp3']
    else:
        dp   = 5 if price < 1 else (4 if price < 10 else (3 if price < 100 else 2))
        f    = lambda v: f"{v:.{dp}f}"
        if is_short:
            entry_zone = f"{f(price - atr*0.2)} — {f(price + atr*0.3)}"
            sl  = f(resistance + atr * 0.5)
            _sld = abs(price - (resistance + atr * 0.5))
            tp1 = f(price - max(_sld * 1.0, atr * 1.0))
            tp2 = f(price - max(_sld * 2.0, atr * 3.0))
            tp3 = f(support   - atr * 0.5)
        else:
            entry_zone = f"{f(price - atr*0.3)} — {f(price + atr*0.2)}"
            sl  = f(support   - atr * 0.5)
            _sld = abs(price - (support - atr * 0.5))
            tp1 = f(price + max(_sld * 1.0, atr * 1.0))
            tp2 = f(price + max(_sld * 2.0, atr * 3.0))
            tp3 = f(resistance + atr * 0.5)

    # Conditions
    conds = []
    if is_short:
        if rsi > 55:   conds.append(f"RSI exhaustion ({round(rsi,1)})")
        if flow > 0.1: conds.append("Flow flip to sell")
        if not conds:  conds.append("Bearish momentum confirmation")
        trigger = "Bearish candle close below entry | Micro BOS down"
        inval   = f"Close above `{fmt(resistance)}` | CVD flips POSITIVE | BOS upward"
    else:
        if rsi < 45:    conds.append(f"RSI oversold ({round(rsi,1)})")
        if flow < -0.1: conds.append("Flow flip to buy")
        if not conds:   conds.append("Bullish momentum confirmation")
        trigger = "Bullish candle close above entry | Micro BOS up"
        inval   = f"Close below `{fmt(support)}` | CVD flips NEGATIVE | BOS downward"

    # Status — deterministic BOS + CVD confirmation
    short_confirmed = bos_1h == "BEARISH" and cvd_trend == "NEGATIVE"
    long_confirmed  = bos_1h == "BULLISH" and cvd_trend == "POSITIVE"

    if (is_short and short_confirmed and confidence >= 55) or \
       (not is_short and long_confirmed and confidence >= 55):
        status = "🟢 READY"
    elif confidence >= 45:
        status = "🟡 WATCH"
    else:
        status = "⚪️ WAIT"

    return (
        f"\n🎯 *EXECUTION*\n"
        f"Setup: `{state}` | Type: `{sig_type}`\n"
        f"Status: {status}\n"
        f"\n"
        f"*Entry:* `{entry_zone}`\n"
        f"*Conditions:* {' | '.join(conds)}\n"
        f"*Trigger:* {trigger}\n"
        f"*Invalidation:* {inval}\n"
        f"\n"
        f"*Targets:*\n"
        f"TP1: `{tp1}`  TP2: `{tp2}`  TP3: `{tp3}`\n"
        f"SL:  `{sl}`\n"
    )


# ═══════════════════════════════════════════════════════════════════
# LAYER 6 — INTERPRETERS (display labels)
# ═══════════════════════════════════════════════════════════════════

def interp_rsi(rsi):
    if rsi >= 70: return "OVERBOUGHT 🔴"
    if rsi >= 60: return "STRONG"
    if rsi <= 30: return "OVERSOLD 🟢"
    if rsi <= 40: return "WEAK"
    return "NEUTRAL"

def interp_cvd(cvd_dir, slope_lbl):
    labels = {
        ("NEGATIVE", "falling"): "STRONG SELLING 💀",
        ("NEGATIVE", "bounce"):  "SELLING 📉 (bounce)",
        ("POSITIVE", "rising"):  "STRONG BUYING 🚀",
        ("POSITIVE", "fade"):    "BUYING 📈 (fade)",
    }
    if (cvd_dir, slope_lbl) in labels:
        return labels[(cvd_dir, slope_lbl)]
    if cvd_dir == "NEGATIVE": return "SELLING 📉"
    if cvd_dir == "POSITIVE": return "BUYING 📈"
    return "NEUTRAL ➡️"

def interp_flow(flow, cvd_dir, dominant_dir):
    flow_bull = flow > 0.1
    flow_bear = flow < -0.1
    cvd_bull  = cvd_dir == "POSITIVE"
    cvd_bear  = cvd_dir == "NEGATIVE"

    # Trap: flow opposes CVD
    if flow_bull and cvd_bear:
        lbl = "buy into bearish" if dominant_dir == "SHORT" else "buy into selling CVD"
        return f"LIQUIDITY TRAP 🪤 ({lbl})"
    if flow_bear and cvd_bull:
        lbl = "sell into bullish" if dominant_dir == "LONG" else "sell into buying CVD"
        return f"LIQUIDITY TRAP 🪤 ({lbl})"

    if flow > 0.3:  return "STRONG BUY FLOW 🟢"
    if flow > 0.1:  return "BUY FLOW"
    if flow < -0.3: return "STRONG SELL FLOW 🔴"
    if flow < -0.1: return "SELL FLOW"
    return "BALANCED ⚖️"

def interp_orderbook(ob):
    if ob > 0.1:  return "BIDS DOMINANT 🟢"
    if ob < -0.1: return "ASKS DOMINANT 🔴"
    return "BALANCED ⚖️"

def interp_oi(pct):
    if pct > 0.5:   return "RISING ⚠️"
    if pct < -0.5:  return "FALLING 📉"
    return "STABLE"

def interp_funding(f):
    if f > 0.001:   return "BULLISH (longs pay)"
    if f < -0.001:  return "BEARISH (shorts pay) 🟢"
    return "NEUTRAL"

def interp_mode(state1h, state4h, trend4h):
    if trend4h == "BEARISH":
        if state1h == "PULLBACK" or state4h == "PULLBACK":
            return "PULLBACK (bear)"
        if state4h == "TRENDING":
            return "BEARISH TREND"
    if trend4h == "BULLISH":
        if state1h == "PULLBACK" or state4h == "PULLBACK":
            return "PULLBACK (bull)"
        if state4h == "TRENDING":
            return "BULLISH TREND"
    return "RANGE"

def fmt_vol(v):
    if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
    if v >= 1_000:     return f"{v/1_000:.2f}K"
    return f"{v:.2f}"


# ═══════════════════════════════════════════════════════════════════
# LAYER 7 — FORMAT OUTPUT
# ═══════════════════════════════════════════════════════════════════

def _trend_emoji(d):
    return "🟢" if d == "BULLISH" else ("🔴" if d == "BEARISH" else "⚪️")

def _bos_emoji(b):
    if b == "BULLISH": return "🟢 BULLISH"
    if b == "BEARISH": return "🔴 BEARISH"
    return "⚪️ NONE"

def _score_bar(score):
    filled = min(10, round(score / 10))
    block  = "🟢" if score >= 60 else ("🟡" if score >= 40 else "🔴")
    return block * filled + "⬜" * (10 - filled)

def _rec_emoji(state, wait_reason=""):
    m = {
        "STRONG SHORT": "🔴🔴 STRONG SHORT",
        "SHORT":        "🔴 SHORT",
        "WEAK SHORT":   "🟠 WEAK SHORT",
        "SHORT RETEST": "🔴 SHORT RETEST ⚠️",
        "STRONG LONG":  "🟢🟢 STRONG LONG",
        "LONG":         "🟢 LONG",
        "WEAK LONG":    "🟡 WEAK LONG",
        "LONG RETEST":  "🟢 LONG RETEST ⚠️",
    }
    if state in m:
        return m[state]
    reason = f" ({wait_reason})" if wait_reason else ""
    return f"⬜ RANGE{reason}"

def _vol_emoji(pct):
    if pct > 20:   return "🟢"
    if pct < -20:  return "🔴"
    return "🟡"


def format_report(symbol, price, fmt,
                  trend1h, strength1h, state1h,
                  trend4h, strength4h, state4h,
                  trend1d, strength1d,
                  alignment, trend_score, dominant_dir,
                  bos_1h,
                  rsi, atr, cvd_dir, cvd_slope, vol_spike,
                  oi_change, oi_bleeding, funding, orderbook, flow,
                  vol_regime, mode,
                  score, bull_prob, bear_prob,
                  final_state, sig_type, confidence, quality,
                  trade_plan, exec_block, vol_data,
                  support, resistance, breakdown,
                  trap_detected, mkt_ctx_block="", macro_block="",
                  market_phase=None, low_activity=False, phase1h="TRENDING", liq_block="", entry_block="", attack_block="", intent_block="", pressure_line="", vol_status="NORMAL",
                  master_state="RANGE", master_decision="NO TRADE",
                  master_type="WAIT", master_reason="", master_confidence=0):

    # ── helpers ─────────────────────────────────────────────────────
    dom_lbl = "🟢 LONG" if dominant_dir == "LONG" else \
              ("🔴 SHORT" if dominant_dir == "SHORT" else "⚪️ WEAK")

    eq_map  = {"HIGH": "✅ HIGH", "MEDIUM": "🟡 MEDIUM",
               "LOW": "⚠️ LOW", "NO SIGNAL": "⚪️ —"}
    eq      = eq_map.get(quality, "⚪️ —")

    # ── trade plan block ────────────────────────────────────────────
    show_plan = (trade_plan is not None) and confidence >= 45 and quality != "LOW"
    if show_plan:
        d_arrow = "🔴 SHORT" if trade_plan["direction"] == "SHORT" else "🟢 LONG"
        tp_block = (
            f"\n📋 *TRADE PLAN*\n"
            f"Direction: {d_arrow}\n"
            f"Entry: `{trade_plan['entry_low']} — {trade_plan['entry_high']}`\n"
            f"SL: `{trade_plan['stop_loss']}`\n"
            f"TP1: `{trade_plan['tp1']}`  R:R `{trade_plan['rr1']}`\n"
            f"TP2: `{trade_plan['tp2']}`  R:R `{trade_plan['rr2']}`\n"
            f"TP3: `{trade_plan['tp3']}`\n"
        )
    elif final_state != "RANGE" and confidence < 45:
        tp_block = "\n`⚠️ No valid setup — confidence too low`\n"
    else:
        tp_block = ""

    # ── flow/CVD mismatch ───────────────────────────────────────────
    flow_bull = flow > 0.1
    flow_bear = flow < -0.1
    cvd_bull  = cvd_dir == "POSITIVE"
    cvd_bear  = cvd_dir == "NEGATIVE"
    mismatch  = (flow_bull and cvd_bear) or (flow_bear and cvd_bull)
    mismatch_line = "⚠️ Flow/CVD divergence → `ABSORPTION / TRAP`\n" if mismatch else ""

    # ── breakdown line ──────────────────────────────────────────────
    bd_line = f"🔴 Breakdown detected!\n" if breakdown == "BREAKDOWN" else \
              f"🟢 Breakout detected!\n"  if breakdown == "BREAKOUT"  else ""

    # ── volume ──────────────────────────────────────────────────────
    s1   = "+" if vol_data["vs_day1_pct"] >= 0 else ""
    s2   = "+" if vol_data["vs_day2_pct"] >= 0 else ""
    sa   = "+" if vol_data["vs_avg_pct"]  >= 0 else ""
    ve   = _vol_emoji(vol_data["vs_avg_pct"])

    msg = (
        f"📊 *{symbol}* — `${fmt(price)}`\n"
        f"{'─' * 28}\n"
        f"\n"
        f"📈 *TREND*\n"
        f"{_trend_emoji(trend1h)} 1H: {trend1h} | {strength1h} | `{phase1h if phase1h not in ("TRENDING","RANGE",state1h) else state1h}`\n"
        f"{_trend_emoji(trend4h)} 4H: {trend4h} | {strength4h} | `{state4h}`\n"
        f"{_trend_emoji(trend1d)} 1D: {trend1d} | {strength1d}\n"
        f"Alignment: `{alignment}`  Score: `{trend_score}/100`\n"
        f"Dominant: {dom_lbl}  BOS: {_bos_emoji(bos_1h)}\n"
        f"\n"
        f"⚡️ *MOMENTUM*\n"
        f"RSI: `{round(rsi,1)}` — {interp_rsi(rsi)}\n"
        f"ATR: `{round(atr/price*100,2)}%`\n"
        f"CVD: {interp_cvd(cvd_dir, cvd_slope)}\n"
        f"\n"
        f"🏗 *STRUCTURE*\n"
        f"Support: `{fmt(support)}`  Resistance: `{fmt(resistance)}`\n"
        f"{bd_line}"
        f"{liq_block}"
        f"\n"
        f"💧 *FLOW & LIQUIDITY*\n"
        f"OI: `{round(oi_change,2)}%` — {interp_oi(oi_change)}\n"
        f"Funding: {interp_funding(funding)}\n"
        f"Orderbook: {interp_orderbook(orderbook)}\n"
        f"Flow: {interp_flow(flow, cvd_dir, dominant_dir)}\n"
        f"{pressure_line}"
        f"{mismatch_line}"
        f"\n"
        f"🌐 *MARKET STATE*\n"
        f"Mode: `{mode}`  Volatility: `{vol_regime}`\n"
        f"Volume: `{vol_status}`\n"
        f"{'📊 Phase: `' + market_phase + '`  ' if market_phase else ''}"
        f"{'⚠️ LOW ACTIVITY  ' if low_activity else ''}\n"
        f"{macro_block}"
        f"{mkt_ctx_block}"
        f"\n"
        f"🤖 *AI DECISION*\n"
        f"Score: `{score}/100`  {_score_bar(score)}\n"
        f"🟢 Bull: `{bull_prob}%`  🔴 Bear: `{bear_prob}%`\n"
        f"State: `{final_state}`  Type: `{sig_type}`  Confidence: `{confidence}%`\n"
        f"Quality: {eq}\n"
        f"➡️ *{_rec_emoji(final_state)}*\n"
        f"{tp_block}"
        f"{exec_block}"
        f"{entry_block}"
        f"{attack_block}"
        f"{intent_block}"
        f"\n"
        f"🔒 *MASTER GATE*\n"
        f"State: `{master_state}`  Decision: `{master_decision}`  Type: `{master_type}`\n"
        f"Confidence: `{master_confidence}%`\n"
        f"Reason: _{master_reason}_\n"
        f"\n"
        f"📦 *24H VOLUME*\n"
        f"Today:      `{fmt_vol(vol_data['vol_24h'])}`\n"
        f"Yesterday:  `{fmt_vol(vol_data['vol_day1'])}` ({s1}{vol_data['vs_day1_pct']}%)\n"
        f"2 Days ago: `{fmt_vol(vol_data['vol_day2'])}` ({s2}{vol_data['vs_day2_pct']}%)\n"
        f"{ve} vs Avg: `{sa}{vol_data['vs_avg_pct']}%`\n"
        f"{'─' * 28}"
    )
    return msg


# ═══════════════════════════════════════════════════════════════════
# LAYER 8 — MAIN ANALYSIS
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# MARKET STRUCTURE LAYER
# ═══════════════════════════════════════════════════════════════════

def detect_market_structure(df):
    """HH/HL=UP, LH/LL=DOWN, else RANGE. Close override has priority."""
    if df is None or len(df) < 20:
        return "RANGE"
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        # Close override
        if closes[-1] < lows[-2] and closes[-1] < closes[-2]:
            return "DOWN"
        if closes[-1] > highs[-2] and closes[-1] > closes[-2]:
            return "UP"
        # Anti-chop
        high20 = df["high"].iloc[-20:].max()
        low20  = df["low"].iloc[-20:].min()
        if low20 > 0 and (high20 - low20) / low20 * 100 < 1.2:
            return "RANGE"
        # Swings
        n = len(highs)
        sh, sl = [], []
        for i in range(2, n - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                sh.append(highs[i])
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                sl.append(lows[i])
        if len(sh) < 2 or len(sl) < 2:
            return "RANGE"
        hh = sh[-1] > sh[-2]; hl = sl[-1] > sl[-2]
        lh = sh[-1] < sh[-2]; ll = sl[-1] < sl[-2]
        if hh and hl: return "UP"
        if lh and ll: return "DOWN"
        if hh and ll: return "WEAK"
        if lh and hl: return "WEAK"
        return "RANGE"
    except Exception:
        return "RANGE"


def build_market_context(df1d, df4h, df1h, df15m=None):
    daily = detect_market_structure(df1d)
    h4    = detect_market_structure(df4h)
    h1    = detect_market_structure(df1h)
    m15   = detect_market_structure(df15m) if df15m is not None else "NEUTRAL"
    # 4H close validation
    try:
        lc4 = float(df4h["close"].iloc[-1])
        ph4 = float(df4h["high"].iloc[-2])
        pl4 = float(df4h["low"].iloc[-2])
        if h4 == "UP"   and lc4 < ph4: h4 = "WEAK"
        if h4 == "DOWN" and lc4 > pl4: h4 = "WEAK"
    except Exception:
        pass
    h1_norm  = h1  if h1  in ("UP","DOWN") else "NEUTRAL"
    m15_norm = m15 if m15 in ("UP","DOWN") else "NEUTRAL"
    filter_reason = None; is_pullback = False; allowed_direction = "NONE"
    # Hard block: 4H WEAK
    if h4 == "WEAK":
        return {"daily":daily,"h4":h4,"h1":h1_norm,"m15":m15_norm,
                "alignment":"WEAK","allowed_direction":"NONE",
                "filter_reason":"4H structure WEAK","is_pullback":False,"m15_bonus":0}
    # Daily RANGE — trade 4H if strong
    if daily == "RANGE":
        if h4 in ("UP","DOWN"):
            allowed_direction = "LONG" if h4 == "UP" else "SHORT"
            daily = h4
            filter_reason = "Daily range — trading 4H trend"
            alignment = "PARTIAL"
        else:
            return {"daily":"RANGE","h4":h4,"h1":h1_norm,"m15":m15_norm,
                    "alignment":"WEAK","allowed_direction":"NONE",
                    "filter_reason":"Daily + 4H both unclear","is_pullback":False,"m15_bonus":0}
    else:
        alignment = "PARTIAL"
    # Pullback
    if (daily=="DOWN" and h4=="UP") or (daily=="UP" and h4=="DOWN"):
        is_pullback = True
    # Direction
    if allowed_direction == "NONE":
        allowed_direction = "LONG" if daily=="UP" else ("SHORT" if daily=="DOWN" else "NONE")
    # Alignment
    if daily == h4 and m15_norm in (daily,"NEUTRAL") and h1_norm in (daily,"NEUTRAL"):
        alignment = "FULL"
    elif daily == h4:
        alignment = "PARTIAL"
    elif is_pullback:
        alignment = "PARTIAL"
    else:
        alignment = "WEAK"; filter_reason = "Daily/4H conflict"; allowed_direction = "NONE"
    # M15 bonus
    m15_bonus = 1 if (m15_norm != "NEUTRAL" and m15_norm == daily and m15_norm == h4) else 0
    return {"daily":daily,"h4":h4,"h1":h1_norm,"m15":m15_norm,
            "alignment":alignment,"allowed_direction":allowed_direction,
            "filter_reason":filter_reason,"is_pullback":is_pullback,"m15_bonus":m15_bonus}


def apply_market_filter(final_state, sig_type, confidence, ctx):
    allowed  = ctx["allowed_direction"]
    reason   = ctx.get("filter_reason")
    pullback = ctx["is_pullback"]
    is_long  = "LONG"  in final_state
    is_short = "SHORT" in final_state
    if allowed == "NONE":
        return "RANGE","RANGE", f"Filtered: {reason}"
    if is_long  and allowed == "SHORT":
        return "RANGE","RANGE","Filtered: LONG in downtrend"
    if is_short and allowed == "LONG":
        return "RANGE","RANGE","Filtered: SHORT in uptrend"
    if pullback and "STRONG" in final_state:
        final_state = final_state.replace("STRONG ","")
        sig_type = "PULLBACK"
    if final_state == "RANGE" and confidence >= 60:
        if ctx.get("h4") == "UP":   return "LONG","TREND",None
        if ctx.get("h4") == "DOWN": return "SHORT","TREND",None
    return final_state, sig_type, None


def format_market_context(ctx):
    icons = {"UP":"🟢","DOWN":"🔴","RANGE":"⚪️","WEAK":"🟡","NEUTRAL":"⚪️"}
    ai = {"FULL":"✅","PARTIAL":"⚠️","WEAK":"❌"}.get(ctx["alignment"],"⚪️")
    lines = [
        f"\n🗺 *MARKET STRUCTURE*",
        f"Daily: {icons.get(ctx['daily'],'⚪️')} `{ctx['daily']}`  "
        f"4H: {icons.get(ctx['h4'],'⚪️')} `{ctx['h4']}`  "
        f"1H: {icons.get(ctx['h1'],'⚪️')} `{ctx['h1']}`",
        f"Alignment: {ai} `{ctx['alignment']}`",
    ]
    if ctx["is_pullback"]:  lines.append("↩️ Pullback mode")
    if ctx.get("filter_reason"): lines.append(f"⚠️ {ctx['filter_reason']}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════
# GLOBAL MACRO CONTEXT
# ═══════════════════════════════════════════════════════════════════

def _get_macro_structure(symbol, interval="D"):
    try:
        df = get_kline(symbol, interval)
        return detect_market_structure(df) if df is not None else "RANGE"
    except Exception:
        return "RANGE"

def _analyze_telegram(symbol):
    # ── Fetch data ────────────────────────────────────────────────
    df15m = get_kline(symbol, "15")
    df1h = get_kline(symbol, "60")
    df4h = get_kline(symbol, "240")
    df1d = get_kline(symbol, "D")

    # ── Indicators ───────────────────────────────────────────────
    for df in (df1h, df4h, df1d):
        df["ATR"] = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"], window=14).average_true_range()
        df["RSI"] = ta.momentum.RSIIndicator(
            df["close"], window=14).rsi()

    price = float(df1h["close"].iloc[-1])
    atr   = float(df1h["ATR"].iloc[-1])
    rsi   = float(df1h["RSI"].iloc[-1])

    dp  = 5 if price < 1 else (4 if price < 10 else (3 if price < 100 else 2))
    fmt = lambda v: f"{v:.{dp}f}"

    # ── Trend ────────────────────────────────────────────────────
    trend1h, strength1h, state1h = detect_trend(df1h)
    bos_1h_pre = detect_bos(df1h)
    _, _, _, phase1h = classify_1h_structure(df1h, detect_trend(df4h)[0], bos_1h_pre)
    trend4h, strength4h, state4h = detect_trend(df4h)
    trend1d, strength1d, _       = detect_trend(df1d)

    # ── CVD ──────────────────────────────────────────────────────
    cvd_dir, cvd_slope = detect_cvd_trend(df1h)

    # ── Structure ────────────────────────────────────────────────
    support, resistance = detect_key_levels(df1h, df4h)
    bos_1h = detect_bos(df1h)

    # ── Market data ──────────────────────────────────────────────
    oi_change, oi_bleeding = get_open_interest(symbol)
    funding  = get_funding(symbol)
    orderbook = get_orderbook(symbol)
    flow     = get_trade_flow(symbol)
    vol_data = get_volume_data(symbol)

    # ── Volume spike ─────────────────────────────────────────────
    avg_vol   = df1h["volume"].iloc[-20:].mean()
    curr_vol  = df1h["volume"].iloc[-1]
    vol_spike = bool(curr_vol > avg_vol * 1.5)
    # ── Volume intelligence ───────────────────────────────────────
    vol_drop_pct  = vol_data.get("vs_avg_pct", 0)
    volume_ratio  = max(0.0, 1 + (vol_drop_pct / 100))
    low_activity  = volume_ratio < 0.5
    block_trades  = volume_ratio < 0.3
    vol_confidence_adj = -30 if volume_ratio < 0.3 else (-20 if volume_ratio < 0.5 else 0)
    if volume_ratio >= 0.8:
        vol_status = f"HIGH ({("+" if vol_drop_pct>=0 else "")}{round(vol_drop_pct)}%)"
    elif volume_ratio >= 0.5:
        vol_status = f"NORMAL ({round(vol_drop_pct)}%)"
    elif volume_ratio >= 0.3:
        vol_status = f"LOW ({round(vol_drop_pct)}%)"
    else:
        vol_status = f"DEAD ({round(vol_drop_pct)}%)"

    # ── Volatility regime ────────────────────────────────────────
    atr_pct   = atr / price * 100
    vol_regime = "HIGH" if atr_pct > 1.0 else ("LOW" if atr_pct < 0.3 else "NORMAL")

    # ── Trap detection ───────────────────────────────────────────
    flow_bull = flow > 0.1
    flow_bear = flow < -0.1
    cvd_bull  = cvd_dir == "POSITIVE"
    cvd_bear  = cvd_dir == "NEGATIVE"
    trap_detected = (flow_bull and cvd_bear) or (flow_bear and cvd_bull)

    # ── Breakdown ────────────────────────────────────────────────
    breakdown = detect_breakdown_breakout(df1h, support, resistance, cvd_dir, vol_spike)

    # ── Direction engine ─────────────────────────────────────────
    dominant_dir = get_dominant_direction(
        trend4h, trend1h, trend1d, bos_1h, cvd_dir, flow, 50)
    alignment    = build_alignment(trend1h, trend4h, trend1d)
    trend_score  = build_trend_score(trend1h, strength1h, state1h,
                                     trend4h, strength4h, state4h,
                                     trend1d, strength1d)
    confidence   = build_confidence(dominant_dir, trend_score, bos_1h,
                                    cvd_dir, flow, alignment, trap_detected, vol_spike)

    # ── State classification ─────────────────────────────────────
    final_state, sig_type = classify_state(
        dominant_dir, bos_1h, trend4h, trend1h,
        state1h, state4h, confidence, breakdown)

    # ── Market phase ─────────────────────────────────────────────
    market_phase = None
    if trend1d == "BULLISH" and trend4h in ("NEUTRAL","WEAK") and trend1h == "BEARISH":
        market_phase = "DISTRIBUTION"
        if "LONG" in final_state and "RETEST" not in final_state:
            final_state = "RANGE"; sig_type = "RANGE"
    elif trend1d == "BEARISH" and trend4h in ("NEUTRAL","WEAK") and trend1h == "BULLISH":
        market_phase = "ACCUMULATION"
        if "SHORT" in final_state and "RETEST" not in final_state:
            final_state = "RANGE"; sig_type = "RANGE"

    # ── Volume confidence adjustment ──────────────────────────────
    confidence = max(0, min(100, confidence + vol_confidence_adj))
    if low_activity and final_state != "RANGE":
        final_state = "RANGE"; sig_type = "RANGE"

    # ── Market context ────────────────────────────────────────────
    mkt_ctx_block = ""
    try:
        mkt_ctx = build_market_context(df1d, df4h, df1h, df15m)
        final_state, sig_type, filter_msg = apply_market_filter(
            final_state, sig_type, confidence, mkt_ctx)
        mkt_ctx_block = format_market_context(mkt_ctx)
        if filter_msg: mkt_ctx_block += f"{filter_msg}\n"
        _ma = mkt_ctx.get("alignment","WEAK")
        if _ma == "FULL":    confidence = min(100, confidence + 10)
        elif _ma == "PARTIAL": confidence = min(100, confidence + 2)
        elif _ma == "WEAK":  confidence = max(0, confidence - 5)
        if mkt_ctx.get("m15_bonus",0) == 1: confidence = min(100, confidence + 6)
    except Exception as e:
        mkt_ctx_block = ""

    # ── Macro context ─────────────────────────────────────────────
    macro_block = ""
    try:
        global_ctx  = get_global_context()
        altcoin     = _is_altcoin(symbol)
        final_state, confidence = apply_macro_filter(final_state, confidence, global_ctx, altcoin)
        macro_block = format_macro_context(global_ctx)
    except Exception:
        macro_block = ""


    # ── Quality ──────────────────────────────────────────────────
    # ── CONFIDENCE FLOOR IN TREND (Points 1, 8, 10) ─────────────
    _has_trend   = trend4h in ("BULLISH","BEARISH") and strength4h in ("STRONG","MODERATE")
    _has_1d      = trend1d in ("BULLISH","BEARISH")
    _str_aligned = alignment in ("STRONG_BULL","STRONG_BEAR","BULLISH","BEARISH")
    _has_bos     = bos_1h not in ("NONE",)
    _has_impulse = vol_spike or abs(flow) > 0.3

    # Floor confidence in trend context
    if _has_trend and _str_aligned:
        confidence = max(45, confidence)
    if _has_trend and _has_1d and (_has_bos or _has_impulse):
        confidence = max(55, confidence)

    # Point 10: Consolidation in trend = CONTINUATION not RANGE
    if _has_trend and final_state == "RANGE" and _str_aligned:
        if trend4h == "BULLISH":
            final_state = "WEAK LONG"; sig_type = "CONTINUATION"
        elif trend4h == "BEARISH":
            final_state = "WEAK SHORT"; sig_type = "CONTINUATION"

    if confidence >= 60 and alignment in ("STRONG_BEAR", "STRONG_BULL", "BEARISH", "BULLISH"):
        quality = "HIGH"
    elif confidence >= 45:
        quality = "MEDIUM"
    else:
        quality = "LOW"

    # Point 12: Upgrade WEAK to directional in strong trend
    if quality != "LOW" and _has_trend and _str_aligned:
        if final_state == "WEAK LONG"  and confidence >= 50: final_state = "LONG"
        if final_state == "WEAK SHORT" and confidence >= 50: final_state = "SHORT"

    # ── Probabilities ────────────────────────────────────────────
    bull_prob, bear_prob = build_probability(
        dominant_dir, trend1h, trend4h, trend1d,
        rsi, bos_1h, cvd_dir, flow, funding, final_state)


    # ── Mode ─────────────────────────────────────────────────────
    mode = interp_mode(state1h, state4h, trend4h)
    # ── Trade plan ───────────────────────────────────────────────
    exec_block = ""  # default
    trade_plan = build_trade_plan(final_state, price, support, resistance, atr, fmt)

    # ── FINAL VALIDATION GUARD ───────────────────────────────────
    # State → Trade plan direction must match. No exceptions.
    if trade_plan:
        state_is_short = "SHORT" in final_state
        state_is_long  = "LONG"  in final_state
        plan_is_short  = trade_plan["direction"] == "SHORT"
        plan_is_long   = trade_plan["direction"] == "LONG"
        if (state_is_short and plan_is_long) or (state_is_long and plan_is_short):
            # Mismatch — kill the plan
            trade_plan = None

    # Probabilities must align with state
    if "SHORT" in final_state and bull_prob > 45:
        bull_prob = 45.0
        bear_prob = 55.0
    elif "LONG" in final_state and bear_prob > 45:
        bear_prob = 45.0
        bull_prob = 55.0

    # ── Execution block ──────────────────────────────────────────

    # ── LIQUIDITY INTELLIGENCE ────────────────────────────────────
    liq_ctx = build_liquidity_context(
        df1h, df4h, price, support, resistance, atr, flow, cvd_dir)
    confidence = max(0, min(100, confidence + liq_ctx["confidence_adj"]))
    if liq_ctx["resistance_strength"] in ("STRONG","WALL") and \
       liq_ctx["position"] in ("AT_RESISTANCE","NEAR_RESISTANCE") and \
       final_state in ("STRONG LONG","LONG"):
        final_state = "WEAK LONG"
    if liq_ctx["support_strength"] in ("STRONG","WALL") and \
       liq_ctx["position"] in ("AT_SUPPORT","NEAR_SUPPORT") and \
       final_state in ("STRONG SHORT","SHORT"):
        final_state = "WEAK SHORT"
    liq_block = format_liquidity_block(liq_ctx, fmt, support, resistance)

    # ── ABSORPTION DETECTOR ───────────────────────────────────────
    try:
        abs_ctx = detect_absorption(df1h, cvd_dir, price, support, resistance, atr)
        pressure_line = format_pressure_line(abs_ctx)
        liq_ctx["absorption_detected"] = abs_ctx["detected"]
        liq_ctx["absorption_type"]     = abs_ctx["type"]
        liq_ctx["absorption_strength"] = abs_ctx["strength"]
        if abs_ctx["detected"]:
            flow_word = abs_ctx["flow"]
            liq_ctx["flow_label"] = f"{flow_word} (absorbed)"
            liq_block = format_liquidity_block(liq_ctx, fmt, support, resistance)
    except Exception:
        abs_ctx = {"detected":False,"type":"NONE","strength":"NONE",
                   "flow":"NEUTRAL","pressure":"NEUTRAL","label":"BALANCED ⚖️"}
        pressure_line = ""

    # ── Score penalties ──────────────────────────────────────
    score = trend_score
    if abs_ctx.get("detected"):  score = max(0, score - 10)
    if volume_ratio < 0.5:       score = max(0, score - 15)
    if final_state == "RANGE":   score = max(0, score - 5)
    if volume_ratio < 0.3:       score = min(score, 45)

    # ── ENTRY ENGINE ──────────────────────────────────────────────
    try:
        entry_ctx = build_entry_context(
            df1h, df5m, price, support, resistance, atr,
            final_state, liq_ctx, cvd_dir, flow, vol_spike, bos_1h)
        confidence = max(0, min(100, confidence + entry_ctx["confidence_adj"]))
        entry_block = format_entry_block(entry_ctx, fmt, price, atr)
    except Exception:
        entry_ctx = {"enabled":False,"entry_type":"NONE","status":"DISABLED",
                     "level":0.0,"direction":"NONE","micro_struct":"NEUTRAL",
                     "confidence_adj":0,"reason":""}
        entry_block = ""

    # ── ATTACK MODE ───────────────────────────────────────────────
    try:
        attack = build_attack_mode(
            df1h, df5m, price, support, resistance, atr,
            final_state, liq_ctx, entry_ctx,
            cvd_dir, flow, vol_spike, bos_1h, alignment)
        confidence = max(0, min(100, confidence + attack["confidence_adj"]))
        attack_block = format_attack_block(attack, fmt)
    except Exception:
        attack = {"status":"OFF","direction":"NONE","level":0.0,
                  "trigger":"","confidence_adj":0}
        attack_block = ""

    # ── TRADE INTENT ──────────────────────────────────────────────
    try:
        intent = build_trade_intent(
            price, support, resistance, atr,
            final_state, liq_ctx, attack, entry_ctx)
        intent_block = format_trade_intent(intent, fmt)
    except Exception:
        intent_block = ""

    # ── TREND PRIORITY LAYER ─────────────────────────────────────
    in_bull_trend = mode in ("BULLISH TREND",) if "mode" in dir() else False
    in_bear_trend = mode in ("BEARISH TREND",) if "mode" in dir() else False
    in_trend      = in_bull_trend or in_bear_trend
    momentum_strong = vol_spike or abs(flow) > 0.2
    breakout_up   = price > resistance and momentum_strong and cvd_dir == "POSITIVE"
    breakout_down = price < support    and momentum_strong and cvd_dir == "NEGATIVE"
    if breakout_up:   in_bull_trend = True; in_trend = True
    if breakout_down: in_bear_trend = True; in_trend = True
    abs_in_trend = in_trend and abs_ctx.get("detected", False)
    if in_bull_trend and "SHORT" in final_state and "RETEST" not in final_state:
        final_state = "RANGE"; sig_type = "RANGE"
    if in_bear_trend and "LONG"  in final_state and "RETEST" not in final_state:
        final_state = "RANGE"; sig_type = "RANGE"
    # TREND STATE PROMOTION
    if in_bull_trend and final_state == "RANGE" and volume_ratio >= 0.2:
        final_state = "WEAK LONG"; sig_type = "TREND"
    if in_bear_trend and final_state == "RANGE" and volume_ratio >= 0.2:
        final_state = "WEAK SHORT"; sig_type = "TREND"
    _lp2 = liq_ctx.get("position","MID_RANGE") if isinstance(liq_ctx,dict) else "MID_RANGE"
    trade_ready = in_trend and _lp2 not in ("MID_RANGE",) and volume_ratio >= 0.2
    block_trades_trend = (volume_ratio < 0.2) if in_trend else block_trades

    if block_trades_trend:
        final_state = "RANGE"; sig_type = "RANGE"; trade_plan = None; exec_block = ""
    elif abs_ctx.get("detected") and volume_ratio < 0.5:
        trade_plan = None; exec_block = ""
        exec_block = build_execution(
        final_state, sig_type, confidence, quality,
        price, support, resistance, atr,
        bos_1h, cvd_dir, rsi, flow,
        trade_plan, fmt)


    # ── Post-filter cleanup ───────────────────────────────────────
    if final_state == "RANGE":
        trade_plan = None
        exec_block = ""


    # ============================================================
    # FINAL OVERRIDE LAYER — post-processing only, no refactor
    # ============================================================
    _is_bull_mode  = mode == "BULLISH TREND"
    _is_bear_mode  = mode == "BEARISH TREND"
    _is_trend_mode = _is_bull_mode or _is_bear_mode
    _vblock        = volume_ratio < 0.2 if _is_trend_mode else block_trades

    if _is_trend_mode and not _vblock:
        if _is_bull_mode:
            if "SHORT" not in final_state or "RETEST" in final_state:
                final_state = "WEAK LONG"; sig_type = "TREND"
        if _is_bear_mode:
            if "LONG" not in final_state or "RETEST" in final_state:
                final_state = "WEAK SHORT"; sig_type = "TREND"
        if _is_bull_mode and "SHORT" in final_state and "RETEST" not in final_state:
            final_state = "WEAK LONG"; sig_type = "TREND"
        if _is_bear_mode and "LONG"  in final_state and "RETEST" not in final_state:
            final_state = "WEAK SHORT"; sig_type = "TREND"

    _cvd_ok  = cvd_dir in ("POSITIVE","NEGATIVE")
    _ob_ok   = orderbook in ("BIDS DOMINANT","ASKS DOMINANT")
    _trend_follow = _is_trend_mode and (trend_score >= 55 or _cvd_ok or _ob_ok) and not _vblock

    _fp = liq_ctx.get("position","MID_RANGE") if isinstance(liq_ctx,dict) else "MID_RANGE"
    _trade_ready = _is_trend_mode and _fp not in ("MID_RANGE",) and not _vblock

    if _is_trend_mode:
        _dir = "LONG" if _is_bull_mode else "SHORT"
        _bl  = liq_ctx.get("buy_liquidity")  if isinstance(liq_ctx,dict) else None
        _sl  = liq_ctx.get("sell_liquidity") if isinstance(liq_ctx,dict) else None
        _lv  = (_bl if (_dir=="LONG" and _bl) else
                (_sl if (_dir=="SHORT" and _sl) else
                 (resistance if _dir=="LONG" else support)))
        if _vblock:
            _st = "WAIT"; _cond = "Volume too low — no trade"
        elif _trend_follow and _trade_ready:
            _st = "FOLLOW_TREND"
            _cond = f"Trend {'bull' if _is_bull_mode else 'bear'} — near edge — pullback + {'HL' if _dir=='LONG' else 'LH'} on 5M"
        elif _trend_follow:
            _st = "WAIT_FOR_PULLBACK"; _cond = "Trend active — wait for pullback to edge"
        elif _trade_ready:
            _st = "LOOK_FOR_ENTRY"; _cond = "Trend — near edge — wait for 5M setup"
        else:
            _st = "WAIT"; _cond = "Trend active — no setup yet"
        intent_block = format_trade_intent(
            {"direction":_dir,"level":_lv if _lv else price,
             "level_type":"trend zone","condition":_cond,"status":_st}, fmt)
    # ============================================================


    # ============================================================
    # MASTER DECISION GATE — LOCK VERSION
    # ============================================================
    _t4h_bull = trend4h == "BULLISH"; _t4h_bear = trend4h == "BEARISH"
    _t1d_bull = trend1d == "BULLISH"; _t1d_bear = trend1d == "BEARISH"
    _t1h_bull = trend1h == "BULLISH"; _t1h_bear = trend1h == "BEARISH"
    _full_bull = _t4h_bull and _t1d_bull; _full_bear = _t4h_bear and _t1d_bear
    _part_bull = _t4h_bull and _t1h_bull; _part_bear = _t4h_bear and _t1h_bear
    _trend_valid = _full_bull or _full_bear or _part_bull or _part_bear
    _struct_valid = state4h == "TRENDING"
    _vol_ok   = volume_ratio >= 0.4
    _vol_low  = 0.2 <= volume_ratio < 0.4
    _vol_dead = volume_ratio < 0.2
    _mdir = "LONG" if (_full_bull or _part_bull) else ("SHORT" if (_full_bear or _part_bear) else "NONE")
    _rsi_ok_long  = rsi < 70
    _rsi_ok_short = rsi > 30
    _rr = float(trade_plan["rr1"]) if (trade_plan and trade_plan.get("rr1")) else 0.0
    _rr_ok = _rr >= 1.2 or _rr == 0.0
    _liq_pos = liq_ctx.get("position","MID_RANGE") if isinstance(liq_ctx,dict) else "MID_RANGE"
    _at_res = _liq_pos in ("AT_RESISTANCE","NEAR_RESISTANCE")
    _at_sup = _liq_pos in ("AT_SUPPORT","NEAR_SUPPORT")
    _pos_ok_long  = not _at_res
    _pos_ok_short = not _at_sup
    _entry_ready = entry_ctx.get("status") == "TRIGGERED" if isinstance(entry_ctx,dict) else False
    _abs_active = abs_ctx.get("detected",False) if isinstance(abs_ctx,dict) else False
    _abs_is_pb = _abs_active and _is_trend_mode

    if _trend_valid and _vol_dead:    master_state = "TREND_LOW_LIQUIDITY"
    elif _trend_valid and _vol_low:   master_state = "TREND_LOW_LIQUIDITY"
    elif _trend_valid:                master_state = "TREND"
    else:                             master_state = "RANGE"

    _blocks = []
    if not _trend_valid:                               _blocks.append("no trend alignment")
    if not _struct_valid:                              _blocks.append("no confirmed structure")
    if _vol_dead:                                      _blocks.append(f"volume DEAD ({round(vol_drop_pct)}%)")
    elif _vol_low:                                     _blocks.append(f"volume LOW ({round(vol_drop_pct)}%)")
    if _mdir == "LONG"  and not _rsi_ok_long:          _blocks.append(f"RSI overbought ({round(rsi,1)})")
    if _mdir == "SHORT" and not _rsi_ok_short:         _blocks.append(f"RSI oversold ({round(rsi,1)})")
    if _rr > 0 and not _rr_ok:                        _blocks.append(f"R:R low ({_rr})")
    if _mdir == "LONG"  and _at_res:                  _blocks.append("price at resistance")
    if _mdir == "SHORT" and _at_sup:                  _blocks.append("price at support")

    # ── FLOW SIGNAL SAFE FILTER (contextual) ────────────────
    _flow_label = liq_ctx.get("flow_label","") if isinstance(liq_ctx,dict) else ""
    _flow_is_trap = any(k in _flow_label.upper() for k in ("ABSORPTION","TRAP"))
    _strong_trend = _trend_valid and _struct_valid and _is_trend_mode
    flow_blocked = False
    if _flow_is_trap:
        if _strong_trend:
            # CONTEXTUAL OVERRIDE: in strong trend, absorption = pullback
            # Do NOT block — downgrade confidence only
            confidence = max(0, confidence - 10)
            _blocks.append("flow absorption (trend pullback, -10 conf)")
            # Intent already handled by final override layer
        else:
            # No trend context — full block
            flow_blocked = True
            confidence = max(0, confidence - 20)
            _blocks.append("flow absorption/trap detected")

    _can_trade = (
        _trend_valid and _struct_valid and _vol_ok and _rr_ok
        and (_rsi_ok_long  if _mdir=="LONG"  else True)
        and (_rsi_ok_short if _mdir=="SHORT" else True)
        and (_pos_ok_long  if _mdir=="LONG"  else True)
        and (_pos_ok_short if _mdir=="SHORT" else True)
        and master_state != "TREND_LOW_LIQUIDITY"
        and not flow_blocked
    )

    if not _trend_valid or master_state == "RANGE":
        master_decision = "NO TRADE"; master_type = "WAIT"
    elif master_state == "TREND_LOW_LIQUIDITY":
        master_decision = "NO TRADE"; master_type = "TREND_LOW_LIQUIDITY"
    elif _can_trade and _entry_ready:
        master_decision = _mdir; master_type = "CONTINUATION" if _abs_is_pb else "BREAKOUT"
    elif _can_trade:
        master_decision = _mdir; master_type = "WAIT"
    else:
        master_decision = "NO TRADE"; master_type = "WAIT"

    master_confidence = confidence
    master_reason = (", ".join(_blocks) if _blocks
        else f"trend {'bullish' if _mdir=='LONG' else 'bearish'} + structure valid + all filters clear")

    if master_decision == "NO TRADE" and not _trend_valid:
        intent_block = format_trade_intent(
            {"direction":"NONE","level":0.0,"level_type":"",
             "condition":f"BLOCKED: {master_reason}","status":"WAIT"}, fmt)
    elif master_state == "TREND_LOW_LIQUIDITY":
        intent_block = format_trade_intent(
            {"direction":_mdir,"level":support if _mdir=="SHORT" else resistance,
             "level_type":"trend target",
             "condition":f"TREND_LOW_LIQUIDITY — wait for volume ({round(vol_drop_pct)}%)",
             "status":"WAIT"}, fmt)

    _report = format_report(
        symbol, price, fmt,
        trend1h, strength1h, state1h,
        trend4h, strength4h, state4h,
        trend1d, strength1d,
        alignment, trend_score, dominant_dir,
        bos_1h,
        rsi, atr, cvd_dir, cvd_slope, vol_spike,
        oi_change, oi_bleeding, funding, orderbook, flow,
        vol_regime, mode,
        score, bull_prob, bear_prob,
        final_state, sig_type, confidence, quality,
        trade_plan, exec_block, vol_data,
        support, resistance, breakdown,
        trap_detected, mkt_ctx_block, macro_block,
        market_phase, low_activity,
        phase1h, liq_block,
        entry_block, attack_block, intent_block, pressure_line, vol_status,
        master_state, master_decision, master_type, master_reason, master_confidence
    )
    sweep_signal = build_sweep_signal(symbol, df15m, price, atr)
    return _report



def _eval_breakout(df, lookback=20):
    """Detects breakout above 20-bar high or below 20-bar low."""
    result = {"direction": None, "vol_ratio": 0.0, "level": 0.0, "reason": "no_breakout"}
    try:
        if df is None or len(df) < lookback + 2: return result
        prev      = df.iloc[-(lookback+1):-1]
        cur       = df.iloc[-1]
        r_high    = float(prev["high"].max())
        r_low     = float(prev["low"].min())
        avg_vol   = float(df["volume"].iloc[-20:].mean())
        cur_vol   = float(cur["volume"])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0.0
        cur_close = float(cur["close"])
        if cur_close > r_high and vol_ratio >= 0.7:
            return {"direction":"LONG",  "vol_ratio":vol_ratio, "level":r_high, "reason":"ok"}
        if cur_close < r_low  and vol_ratio >= 0.7:
            return {"direction":"SHORT", "vol_ratio":vol_ratio, "level":r_low,  "reason":"ok"}
        result["reason"] = "no_volume" if vol_ratio < 1.0 else "no_breakout"
        return result
    except Exception as e:
        result["reason"] = f"error:{e}"; return result

def _check_flat_market(df, lookback=20):
    """
    Returns True if market is flat/dead.
    Flat = current candle range < 30% of avg range of last 20 candles.
    Also checks volume is not dead (< 20% of avg).
    """
    try:
        if df is None or len(df) < lookback + 1: return True
        recent = df.iloc[-(lookback+1):-1]
        avg_range = (recent["high"] - recent["low"]).mean()
        cur_range = float(df.iloc[-1]["high"] - df.iloc[-1]["low"])
        if avg_range > 0 and cur_range < avg_range * 0.3: return True
        avg_vol = recent["volume"].mean()
        cur_vol = float(df.iloc[-1]["volume"])
        if avg_vol > 0 and cur_vol < avg_vol * 0.15: return True
        return False
    except Exception:
        return True


def _eval_sweep(df, lookback=20, sweep_window=3):
    """
    Multi-candle sweep detection with quality filters.
    Requires: sweep depth >= 0.2 ATR, strong reclaim candle, reclaim strength >= 0.05.
    """
    result = {"direction": None, "reclaim_strength": 0.0,
              "vol_ratio": 0.0, "reason": "no_sweep",
              "sweep_low": None, "sweep_high": None, "sweep_idx": None}
    try:
        if df is None or len(df) < lookback + sweep_window + 1:
            result["reason"] = "insufficient_data"; return result

        ref      = df.iloc[-(lookback + sweep_window + 2):-(sweep_window + 2)]
        r_high   = float(ref["high"].max())
        r_low    = float(ref["low"].min())
        confirm  = df.iloc[-2]
        avg_vol  = float(df["volume"].iloc[-21:-1].mean())
        cur_vol  = float(confirm["volume"])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0.0
        rng      = r_high - r_low + 1e-9

        # ATR for depth filter (14-period true range on closed candles)
        import pandas as pd
        highs  = df["high"].astype(float).iloc[:-1]
        lows   = df["low"].astype(float).iloc[:-1]
        closes = df["close"].astype(float).iloc[:-1]
        prev_c = closes.shift(1)
        tr     = pd.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)
        atr    = float(tr.rolling(14).mean().iloc[-1])
        if atr <= 0: atr = rng * 0.05

        sweep_candles = df.iloc[-(sweep_window + 2):-2]
        best = None

        for _, sweep_c in sweep_candles.iterrows():
            c_low   = float(sweep_c["low"])
            c_high  = float(sweep_c["high"])
            c_close = float(confirm["close"])
            c_open  = float(confirm["open"])
            c_rng   = float(confirm["high"]) - float(confirm["low"])

            # ── LONG sweep ────────────────────────────────────────
            swept_low = c_low < r_low and c_close > r_low
            if swept_low:
                # Condition 3: sweep depth >= 0.2 ATR
                depth = r_low - c_low
                _sweep_depth_min = 0.3 * atr if _TRADE_MODE == "MEDIUM" else 0.4 * atr
                if depth < _sweep_depth_min:
                    continue

                rs = (c_close - r_low) / rng

                # Reclaim strength >= 0.05
                if rs < 0.05:
                    continue

                # Reclaim candle strength (LONG)
                body = c_close - c_open
                if body <= 0:
                    continue
                if body < 0.3 * atr:
                    continue
                if c_rng > 0 and c_close < float(confirm["low"]) + 0.6 * c_rng:
                    continue

                candidate = {"direction": "LONG", "reclaim_strength": rs,
                             "vol_ratio": vol_ratio, "reason": "ok",
                             "sweep_low": c_low, "sweep_high": float(sweep_c["high"]),
                             "sweep_idx": None}
                if best is None or rs > best["reclaim_strength"]:
                    best = candidate

            # ── SHORT sweep ───────────────────────────────────────
            swept_high = c_high > r_high and c_close < r_high
            if swept_high:
                # Condition 3: sweep depth >= 0.2 ATR
                depth = c_high - r_high
                if depth < 0.4 * atr:
                    continue

                rs = (r_high - c_close) / rng

                # Reclaim strength >= 0.05
                if rs < 0.05:
                    continue

                # Reclaim candle strength (SHORT)
                body = c_open - c_close
                if body <= 0:
                    continue
                if body < 0.3 * atr:
                    continue
                if c_rng > 0 and c_close > float(confirm["high"]) - 0.6 * c_rng:
                    continue

                candidate = {"direction": "SHORT", "reclaim_strength": rs,
                             "vol_ratio": vol_ratio, "reason": "ok",
                             "sweep_low": float(sweep_c["low"]), "sweep_high": c_high,
                             "sweep_idx": None}
                if best is None or rs > best["reclaim_strength"]:
                    best = candidate

        if best is None:
            return result

        _vol_min = 0.7 if _TRADE_MODE == "MEDIUM" else 0.8
        if vol_ratio < _vol_min:
            best["reason"] = "no_volume"
            return best

        return best

    except Exception as e:
        result["reason"] = f"error:{e}"; return result


def _eval_breakout(df, lookback=20):
    """Detects breakout above 20-bar high or below 20-bar low."""
    result = {"direction": None, "vol_ratio": 0.0, "level": 0.0, "reason": "no_breakout"}
    try:
        if df is None or len(df) < lookback + 2: return result
        prev      = df.iloc[-(lookback+1):-1]
        cur       = df.iloc[-1]
        r_high    = float(prev["high"].max())
        r_low     = float(prev["low"].min())
        avg_vol   = float(df["volume"].iloc[-20:].mean())
        cur_vol   = float(cur["volume"])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0.0
        cur_close = float(cur["close"])
        if cur_close > r_high and vol_ratio >= 0.7:
            return {"direction":"LONG",  "vol_ratio":vol_ratio, "level":r_high, "reason":"ok"}
        if cur_close < r_low  and vol_ratio >= 0.7:
            return {"direction":"SHORT", "vol_ratio":vol_ratio, "level":r_low,  "reason":"ok"}
        result["reason"] = "no_volume" if vol_ratio < 1.0 else "no_breakout"
        return result
    except Exception as e:
        result["reason"] = f"error:{e}"; return result

def _check_flat_market(df, lookback=20):
    """
    Returns True if market is flat/dead.
    Flat = current candle range < 30% of avg range of last 20 candles.
    Also checks volume is not dead (< 20% of avg).
    """
    try:
        if df is None or len(df) < lookback + 1: return True
        recent = df.iloc[-(lookback+1):-1]
        avg_range = (recent["high"] - recent["low"]).mean()
        cur_range = float(df.iloc[-1]["high"] - df.iloc[-1]["low"])
        if avg_range > 0 and cur_range < avg_range * 0.3: return True
        avg_vol = recent["volume"].mean()
        cur_vol = float(df.iloc[-1]["volume"])
        if avg_vol > 0 and cur_vol < avg_vol * 0.15: return True
        return False
    except Exception:
        return True


def _eval_sweep(df, lookback=20, sweep_window=3):
    """
    Multi-candle sweep detection.
    Searches last sweep_window candles for liquidity grab.
    Confirmation: df.iloc[-1] (current closed candle).
    Volume checked on confirmation candle.
    """
    result = {"direction": None, "reclaim_strength": 0.0,
              "vol_ratio": 0.0, "reason": "no_sweep",
              "sweep_low": None, "sweep_high": None, "sweep_idx": None}
    try:
        if df is None or len(df) < lookback + sweep_window + 1:
            result["reason"] = "insufficient_data"; return result

        ref      = df.iloc[-(lookback + sweep_window + 2):-(sweep_window + 2)]
        r_high   = float(ref["high"].max())
        r_low    = float(ref["low"].min())
        confirm  = df.iloc[-2]   # closed candle only
        avg_vol  = float(df["volume"].iloc[-21:-1].mean())
        cur_vol  = float(confirm["volume"])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0.0
        rng      = r_high - r_low + 1e-9

        sweep_candles = df.iloc[-(sweep_window + 2):-2]
        best = None

        # ── ATR for adaptive depth threshold ─────────────────
        import pandas as _pd2
        _highs = df["high"].astype(float)
        _lows  = df["low"].astype(float)
        _cls   = df["close"].astype(float)
        _pc    = _cls.shift(1)
        _tr    = _pd2.concat([_highs-_lows, (_highs-_pc).abs(), (_lows-_pc).abs()], axis=1).max(axis=1)
        _atr14 = float(_tr.rolling(14).mean().iloc[-2])
        _atr50 = float(_tr.rolling(50).mean().iloc[-2]) if len(df) >= 52 else _atr14
        # Adaptive: in low-vol regime use 0.15, else mode-based baseline
        _base  = 0.4 if _TRADE_MODE == "PROD" else 0.3
        _depth_min = (0.15 * _atr14) if (_atr14 < _atr50 * 0.7) else (_base * _atr14)

        for _, sweep_c in sweep_candles.iterrows():
            # Depth filter — how far wick penetrated beyond level
            _long_depth  = r_low  - float(sweep_c["low"])
            _short_depth = float(sweep_c["high"]) - r_high
            swept_low  = float(sweep_c["low"])  < r_low  and float(confirm["close"]) > r_low
            swept_high = float(sweep_c["high"]) > r_high and float(confirm["close"]) < r_high

            # Apply adaptive depth filter
            if swept_low  and _long_depth  < _depth_min: swept_low  = False
            if swept_high and _short_depth < _depth_min: swept_high = False

            if swept_low:
                rs = (float(confirm["close"]) - r_low) / rng
                candidate = {"direction": "LONG", "reclaim_strength": rs,
                             "vol_ratio": vol_ratio, "reason": "ok",
                             "sweep_low": float(sweep_c["low"]),
                             "sweep_high": float(sweep_c["high"]),
                             "sweep_idx": None}
                if best is None or rs > best["reclaim_strength"]:
                    best = candidate

            if swept_high:
                rs = (r_high - float(confirm["close"])) / rng
                candidate = {"direction": "SHORT", "reclaim_strength": rs,
                             "vol_ratio": vol_ratio, "reason": "ok",
                             "sweep_low": float(sweep_c["low"]),
                             "sweep_high": float(sweep_c["high"]),
                             "sweep_idx": None}
                if best is None or rs > best["reclaim_strength"]:
                    best = candidate

        if best is None:
            return result

        if vol_ratio < 1.0:
            best["reason"] = "no_volume"
            return best

        return best

    except Exception as e:
        result["reason"] = f"error:{e}"; return result


def _simple_trend(df, fast=8, slow=21):
    """
    Returns trend direction using EMA fast/slow crossover on closed candles.
    Returns: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    """
    try:
        if df is None or len(df) < slow + 2:
            return "NEUTRAL"
        closes = df["close"].astype(float)
        ema_f  = closes.ewm(span=fast,  adjust=False).mean().iloc[-1]
        ema_s  = closes.ewm(span=slow, adjust=False).mean().iloc[-1]
        margin = closes.iloc[-1] * 0.002   # 0.2% dead zone
        if ema_f > ema_s + margin:   return "BULLISH"
        if ema_f < ema_s - margin:   return "BEARISH"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"



def _stable_trend_4h(df, symbol=""):
    try:
        if df is None or len(df) < 55:
            return "NEUTRAL"
        closes = df["close"].astype(float)
        ema21 = closes.ewm(span=21, adjust=False).mean()
        ema50 = closes.ewm(span=50, adjust=False).mean()
        bullish_now  = ema21.iloc[-1] > ema50.iloc[-1]
        bearish_now  = ema21.iloc[-1] < ema50.iloc[-1]
        bullish_3ago = ema21.iloc[-3] > ema50.iloc[-3]
        bearish_3ago = ema21.iloc[-3] < ema50.iloc[-3]
        ema50_slope  = float(ema50.iloc[-1] - ema50.iloc[-4])
        if bullish_now and bullish_3ago:
            raw = "BULLISH"
        elif bearish_now and bearish_3ago:
            raw = "BEARISH"
        else:
            raw = "NEUTRAL"
        if raw == "BULLISH" and ema50_slope < 0:
            raw = "NEUTRAL"
        if raw == "BEARISH" and ema50_slope > 0:
            raw = "NEUTRAL"
        print(f"[TREND4H] {symbol} = {raw} (ema21={ema21.iloc[-1]:.6f} ema50={ema50.iloc[-1]:.6f} slope={ema50_slope:.6f})")
        return raw
    except Exception as _e:
        print(f"[TREND4H ERROR] {symbol}: {_e}")
        return "NEUTRAL"



def detect_market_regime(df4h, symbol="BTCUSDT"):
    """
    Classify market regime from 4H BTC data.
    Returns: ("TREND_UP" | "TREND_DOWN" | "RANGE" | "CHAOTIC", atr_ratio)
    
    Logic:
      CHAOTIC:   ATR > 20-period ATR average * 1.5  (volatility expansion)
      TREND_UP:  EMA21 > EMA50, slope positive, ATR normal
      TREND_DOWN:EMA21 < EMA50, slope negative, ATR normal
      RANGE:     EMA21 ~ EMA50 (flat slope), ATR compressed
    """
    try:
        if df4h is None or len(df4h) < 55:
            return "RANGE", 1.0
        closes = df4h["close"].astype(float)
        highs  = df4h["high"].astype(float)
        lows   = df4h["low"].astype(float)
        # ATR calculation
        tr = (highs - lows).abs()
        tr = tr.combine(abs(closes.shift(1) - highs), max)
        tr = tr.combine(abs(closes.shift(1) - lows),  max)
        atr_cur = float(tr.rolling(14).mean().iloc[-1])
        atr_avg = float(tr.rolling(20).mean().iloc[-1])
        atr_ratio = atr_cur / atr_avg if atr_avg > 0 else 1.0
        # EMA slope
        ema21 = closes.ewm(span=21, adjust=False).mean()
        ema50 = closes.ewm(span=50, adjust=False).mean()
        slope21 = float(ema21.iloc[-1] - ema21.iloc[-5]) / float(ema21.iloc[-5]) if ema21.iloc[-5] > 0 else 0
        ema50_slope = float(ema50.iloc[-1] - ema50.iloc[-4])
        bullish = ema21.iloc[-1] > ema50.iloc[-1] and ema21.iloc[-3] > ema50.iloc[-3]
        bearish = ema21.iloc[-1] < ema50.iloc[-1] and ema21.iloc[-3] < ema50.iloc[-3]
        # Classification — CHAOTIC overrides trend
        if atr_ratio > 1.5:
            regime = "CHAOTIC"
        elif bullish and ema50_slope > 0:
            regime = "TREND_UP"
        elif bearish and ema50_slope < 0:
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"
        print(f"[REGIME_DETECT] {symbol} → {regime} (atr_ratio={atr_ratio:.2f} slope21={slope21:.4f} bull={bullish} bear={bearish})")
        return regime, atr_ratio
    except Exception as e:
        print(f"[REGIME_DETECT ERROR] {symbol}: {e}")
        return "RANGE", 1.0


def _phase_filter(df15m, df4h, direction, entry_price, symbol=""):
    try:
        if df15m is None or len(df15m) < 20: return None
        if df4h is None or len(df4h) < 10: return None
        bodies_4h = (df4h["close"].astype(float) - df4h["open"].astype(float)).abs()
        avg_body_4h = bodies_4h.rolling(20).mean().iloc[-1]
        impulse_too_recent = bool(any(bodies_4h.iloc[-3:] > avg_body_4h * 2.0))
        hi = df15m["high"].astype(float)
        lo = df15m["low"].astype(float)
        cl = df15m["close"].astype(float)
        tr = (hi - lo).combine((hi - cl.shift()).abs(), max).combine((lo - cl.shift()).abs(), max)
        atr_now  = float(tr.rolling(14).mean().iloc[-1])
        atr_peak_series = tr.rolling(14).mean().iloc[-20:-1]
        atr_peak = float(atr_peak_series.max()) if len(atr_peak_series) > 0 else atr_now
        atr_ratio = atr_now / atr_peak if atr_peak > 0 else 1.0
        atr_contracted = atr_ratio < 0.65
        fails = int(impulse_too_recent) + int(atr_contracted)
        if fails >= 2:
            print(f"[PHASE FILTER] {symbol} downgraded HIGH->MEDIUM impulse_recent={impulse_too_recent} atr_ratio={atr_ratio:.2f}")
            return "MEDIUM"
        return None
    except Exception as _pe:
        print(f"[PHASE FILTER ERROR] {symbol}: {_pe}")
        return None


def detect_htf_bias(symbol):
    """
    Fetches 1D, 4H, 1H candles and determines directional bias.
    Returns dict:
    {
      "bias":       LONG | SHORT | CONFLICT | NEUTRAL,
      "d1":         BULLISH | BEARISH | NEUTRAL,
      "h4":         BULLISH | BEARISH | NEUTRAL,
      "h1":         BULLISH | BEARISH | NEUTRAL,
      "reason":     str,
      "confidence": HIGH | MEDIUM | LOW
    }
    """
    try:
        df1d = get_kline(symbol, "D")
        df4h = get_kline(symbol, "240")
        df1h = get_kline(symbol, "60")

        t1d = _simple_trend(df1d)
        t4h = _stable_trend_4h(df4h, symbol)
        t1h = _simple_trend(df1h)

        # LONG bias: 1D bull + 4H bull/neutral + 1H not strongly bear
        long_score  = sum([
            t1d == "BULLISH",
            t4h == "BULLISH",
            t4h == "NEUTRAL",
            t1h != "BEARISH",
        ])
        # SHORT bias: 1D bear + 4H bear/neutral + 1H not strongly bull
        short_score = sum([
            t1d == "BEARISH",
            t4h == "BEARISH",
            t4h == "NEUTRAL",
            t1h != "BULLISH",
        ])

        if t1d == "BULLISH" and t4h in ("BULLISH","NEUTRAL") and t1h != "BEARISH":
            bias = "LONG"
            confidence = "HIGH" if t4h == "BULLISH" else "MEDIUM"
            reason = f"1D {t1d} + 4H {t4h} + 1H {t1h}"
        elif t1d == "BEARISH" and t4h in ("BEARISH","NEUTRAL") and t1h != "BULLISH":
            bias = "SHORT"
            confidence = "HIGH" if t4h == "BEARISH" else "MEDIUM"
            reason = f"1D {t1d} + 4H {t4h} + 1H {t1h}"
        elif t1d == "NEUTRAL" and t4h in ("BULLISH","BEARISH"):
            bias = "LONG" if t4h == "BULLISH" else "SHORT"
            confidence = "MEDIUM"
            reason = f"4H {t4h} leading (1D neutral)"
        else:
            bias = "CONFLICT"
            confidence = "LOW"
            reason = f"HTF conflict: 1D {t1d} / 4H {t4h} / 1H {t1h}"

        return {"bias": bias, "d1": t1d, "h4": t4h, "h1": t1h,
                "reason": reason, "confidence": confidence}

    except Exception as e:
        return {"bias": "NEUTRAL", "d1": "NEUTRAL", "h4": "NEUTRAL",
                "h1": "NEUTRAL", "reason": f"error:{e}", "confidence": "LOW"}


SIGNAL_MODE = "SOFT"    # set by app.py: STRICT or SOFT

# Trading mode — PROD or MEDIUM (read from mode.txt)
import os as _os_tm
_MODE_FILE_AGENT = _os_tm.path.join(_os_tm.path.dirname(__file__), "mode.txt")
_TRADE_MODE = open(_MODE_FILE_AGENT).read().strip() if _os_tm.path.exists(_MODE_FILE_AGENT) else "PROD"


def _eval_sweep_level(df):
    """
    Extract sweep level from 15M data for 5M reclaim monitoring.
    Does NOT generate a signal — only identifies level + direction.
    Returns dict or None.
    """
    try:
        ev = _eval_sweep(df)
        if not ev.get("direction"):
            return None
        direction = ev["direction"]
        # Level = the swept extreme (low for LONG, high for SHORT)
        if direction == "LONG":
            level = ev.get("sweep_low")
        else:
            level = ev.get("sweep_high")
        if level is None:
            return None
        # ATR from sweep function context
        import pandas as _pdsl
        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        closes = df["close"].astype(float)
        prev_c = closes.shift(1)
        tr     = _pdsl.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)
        atr    = float(tr.rolling(14).mean().iloc[-2])
        return {
            "level":     float(level),
            "direction": direction,
            "atr":       atr,
        }
    except Exception as e:
        return None


def _eval_consolidation_breakout(df, trade_mode="PROD"):
    """
    Consolidation breakout detection.
    Uses only closed candles — no partial candle risk.
    breakout  = df.iloc[-3]  (closed)
    follow_through = df.iloc[-2]  (closed)
    entry     = df.iloc[-2] close
    """
    result = {"direction": None, "reason": "no_breakout", "setup_type": "BREAKOUT"}
    try:
        if df is None or len(df) < 25:
            return result

        import pandas as _pd4
        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        closes = df["close"].astype(float)
        opens  = df["open"].astype(float)
        vols   = df["volume"].astype(float)
        prev_c = closes.shift(1)
        tr = _pd4.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)

        atr_recent = float(tr.rolling(14).mean().iloc[-3])
        atr_mean   = float(tr.rolling(20).mean().iloc[-3])

        # ── Condition 1: ATR compression ─────────────────────────
        if atr_recent >= atr_mean * 0.90:
            result["reason"] = "no_compression"
            return result

        # ── Range: last 15 candles before breakout candle ─────────
        lookback = 15
        ref_high = float(highs.iloc[-lookback-3:-3].max())
        ref_low  = float(lows.iloc[-lookback-3:-3].min())

        # ── Breakout candle (df.iloc[-3]) ─────────────────────────
        brk_close = float(closes.iloc[-3])
        brk_open  = float(opens.iloc[-3])
        brk_high  = float(highs.iloc[-3])
        brk_low   = float(lows.iloc[-3])
        brk_body  = abs(brk_close - brk_open)
        brk_vol   = float(vols.iloc[-3])
        avg_vol   = float(vols.iloc[-21:-3].mean())
        vol_ratio = brk_vol / avg_vol if avg_vol > 0 else 0.0

        # ── Condition 2: body size filter (skip if > 2 ATR) ───────
        if brk_body > 2.0 * atr_recent:
            result["reason"] = "candle_too_extended"
            return result

        # ── Condition 3: body CLOSES beyond range (not just wick) ─
        breakout_long  = brk_close > ref_high
        breakout_short = brk_close < ref_low

        if not breakout_long and not breakout_short:
            result["reason"] = "no_range_break"
            return result

        direction = "LONG" if breakout_long else "SHORT"

        # ── Condition 4: volume expansion ─────────────────────────
        vol_min = 1.5 if trade_mode == "PROD" else 1.2
        if vol_ratio < vol_min:
            result["reason"] = "insufficient_volume"
            return result

        # ── Follow-through candle (df.iloc[-2], closed) ───────────
        ft_close = float(closes.iloc[-2])
        ft_open  = float(opens.iloc[-2])
        _ft_conflict = False
        if direction == "LONG"  and ft_close <= ft_open:
            _ft_conflict = True
        if direction == "SHORT" and ft_close >= ft_open:
            _ft_conflict = True
        if _ft_conflict:
            if result.get("confidence") == "HIGH":
                result["confidence"] = "MEDIUM"
                result["_ft_downgrade"] = True
            else:
                result["reason"] = "no_follow_through"
                return result

        # ── SL: breakout candle low/high + ATR buffer ─────────────
        buf = min(atr_recent * 0.2, brk_body * 0.1)
        if direction == "LONG":
            sl_raw = brk_low - buf
            # Override: if body > 1.5 ATR, use entry - 1.0 ATR
            if brk_body > 1.5 * atr_recent:
                sl_raw = ft_close - atr_recent
        else:
            sl_raw = brk_high + buf
            if brk_body > 1.5 * atr_recent:
                sl_raw = ft_close + atr_recent

        # ── Entry: follow-through candle close ────────────────────
        entry = ft_close

        # ── Breakout confidence assignment ───────────────────────
        _range_duration = 15  # lookback candles

        # HTF 1H alignment
        _htf_1h = "NEUTRAL"
        try:
            from agent import get_kline as _gk
            _df1h = _gk(df.attrs.get("symbol",""), "60") if hasattr(df, "attrs") else None
            if _df1h is not None and len(_df1h) > 22:
                _hc   = _df1h["close"].astype(float)
                _e8   = _hc.ewm(span=8).mean().iloc[-2]
                _e21  = _hc.ewm(span=21).mean().iloc[-2]
                _htf_1h = "BULLISH" if _e8 > _e21 else "BEARISH" if _e8 < _e21 else "NEUTRAL"
        except Exception:
            _htf_1h = "NEUTRAL"

        _htf_aligned = ((direction=="LONG" and _htf_1h=="BULLISH") or
                        (direction=="SHORT" and _htf_1h=="BEARISH"))

        # HIGH: all 7 conditions
        _is_high = (
            atr_recent < atr_mean * 0.65 and   # tight compression
            brk_body   >= 0.8 * atr_recent and  # strong body
            vol_ratio  >= 1.5 and               # volume expansion
            _range_duration >= 8 and            # genuine coil
            _htf_aligned                        # HTF aligned
        )
        # MEDIUM: relaxed conditions
        _vol_min = 1.5 if trade_mode == "PROD" else 1.2
        _is_medium = (
            atr_recent < atr_mean * 0.75 and
            brk_body   >= 0.5 * atr_recent and
            vol_ratio  >= _vol_min and
            _range_duration >= 5
        )

        if _is_high:
            _confidence = "HIGH"
        elif _is_medium:
            _confidence = "MEDIUM"
        else:
            result["reason"] = "insufficient_quality"
            return result

        result.update({
            "direction":   direction,
            "reason":      "ok",
            "confidence":  _confidence,
            "vol_ratio":   vol_ratio,
            "atr":         atr_recent,
            "sl_raw":      sl_raw,
            "entry":       entry,
            "ref_high":    ref_high,
            "ref_low":     ref_low,
            "setup_type":  "BREAKOUT",
        })
        return result

    except Exception as e:
        result["reason"] = f"error:{e}"
        return result


def _get_btc_regime():
    """
    BTC 1H EMA8 vs EMA21 trend.
    Returns: BULLISH / BEARISH / NEUTRAL
    Cached for 15 minutes to avoid repeated API calls.
    """
    import time
    now = time.time()
    cache = getattr(_get_btc_regime, '_cache', None)
    if cache and now - cache['ts'] < 900:
        return cache['val']
    try:
        df = get_kline("BTCUSDT", "60")
        if df is None or len(df) < 22:
            return "NEUTRAL"
        cls   = df["close"].astype(float)
        ema8  = float(cls.ewm(span=8).mean().iloc[-2])
        ema21 = float(cls.ewm(span=21).mean().iloc[-2])
        val   = "BULLISH" if ema8 > ema21 else "BEARISH" if ema8 < ema21 else "NEUTRAL"
        _get_btc_regime._cache = {'ts': now, 'val': val}
        return val
    except Exception:
        return "NEUTRAL"


def _eval_continuation(df, trade_mode="PROD"):
    """
    Continuation setup (Path C): impulse → pullback → rejection → entry.
    Uses only closed candles.
    impulse_start = df.iloc[-6] to df.iloc[-4]
    pullback      = df.iloc[-3]
    entry candle  = df.iloc[-2] (closed)
    """
    result = {"direction": None, "reason": "no_continuation", "setup_type": "CONT"}
    try:
        if df is None or len(df) < 20:
            return result

        import pandas as _pd5
        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        closes = df["close"].astype(float)
        opens  = df["open"].astype(float)
        vols   = df["volume"].astype(float)
        prev_c = closes.shift(1)
        tr = _pd5.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-2])

        if atr <= 0:
            result["reason"] = "zero_atr"
            return result

        # ── Impulse: candles -6 to -4 (3 closed candles) ─────────
        imp_opens  = [float(opens.iloc[i]) for i in [-6, -5, -4]]
        imp_closes = [float(closes.iloc[i]) for i in [-6, -5, -4]]
        imp_highs  = [float(highs.iloc[i]) for i in [-6, -5, -4]]
        imp_lows   = [float(lows.iloc[i]) for i in [-6, -5, -4]]

        # Detect direction from impulse
        imp_bull = sum(1 for o, c in zip(imp_opens, imp_closes) if c > o)
        imp_bear = sum(1 for o, c in zip(imp_opens, imp_closes) if c < o)

        if imp_bull >= 2:
            direction = "LONG"
            imp_start = float(opens.iloc[-6])   # origin before impulse
            imp_end   = max(imp_closes)
            imp_move  = imp_end - imp_start
        elif imp_bear >= 2:
            direction = "SHORT"
            imp_start = float(opens.iloc[-6])
            imp_end   = min(imp_closes)
            imp_move  = imp_start - imp_end
        else:
            result["reason"] = "no_clear_impulse"
            return result

        # Impulse size filter — mode-aware
        _imp_min = 0.6 if trade_mode == "MEDIUM" else 0.75
        if imp_move < _imp_min * atr:
            result["reason"] = "impulse_too_small"
            return result

        # ── Pullback: candle -3 ────────────────────────────────────
        pb_open  = float(opens.iloc[-3])
        pb_close = float(closes.iloc[-3])
        pb_high  = float(highs.iloc[-3])
        pb_low   = float(lows.iloc[-3])

        # Pullback must retrace 30-60% of impulse
        if direction == "LONG":
            pb_retrace = imp_end - pb_close
            pb_pct = pb_retrace / imp_move if imp_move > 0 else 0
            # Must NOT close below impulse origin
            if pb_close < imp_start:
                result["reason"] = "pullback_broke_origin"
                return result
        else:
            pb_retrace = pb_close - imp_end
            pb_pct = pb_retrace / imp_move if imp_move > 0 else 0
            if pb_close > imp_start:
                result["reason"] = "pullback_broke_origin"
                return result

        if not (0.15 <= pb_pct <= 0.80):
            result["reason"] = f"pullback_pct_invalid:{round(pb_pct,2)}"
            return result

        # ── Entry candle: df.iloc[-2] (closed) ────────────────────
        en_open  = float(opens.iloc[-2])
        en_close = float(closes.iloc[-2])
        en_high  = float(highs.iloc[-2])
        en_low   = float(lows.iloc[-2])
        en_vol   = float(vols.iloc[-2])
        avg_vol  = float(vols.iloc[-20:-2].mean())
        vol_ratio = en_vol / avg_vol if avg_vol > 0 else 0.0

        # Entry candle: against direction downgrades confidence, does not block
        _candle_conflict = False
        if direction == "LONG"  and en_close <= en_open:
            _candle_conflict = True
        if direction == "SHORT" and en_close >= en_open:
            _candle_conflict = True
        if _candle_conflict:
            if result.get("confidence") == "HIGH":
                result["confidence"] = "MEDIUM"
                result["_candle_downgrade"] = True
            else:
                result["reason"] = "entry_candle_conflict"
                return result

        # Volume filter
        vol_min = 1.0 if trade_mode == "PROD" else 0.85
        if vol_ratio < vol_min:
            result["reason"] = "insufficient_volume"
            return result

        # Late entry filter — entry must be within 0.8 ATR of pullback close
        entry = en_close
        dist  = abs(entry - pb_close)
        if dist > 0.8 * atr:
            result["reason"] = "late_entry"
            return result

        # ── SL: below pullback low (LONG) / above pullback high (SHORT)
        buf = atr * 0.1
        if direction == "LONG":
            sl_raw = pb_low - buf
        else:
            sl_raw = pb_high + buf

        risk = abs(entry - sl_raw)
        if risk <= 0:
            result["reason"] = "zero_risk"
            return result

        # ── Confidence ────────────────────────────────────────────
        _htf_1h = "NEUTRAL"
        try:
            _df1h = get_kline("", "60")  # will fail gracefully
        except Exception:
            pass

        _is_high = (imp_move >= 2.0 * atr and
                    0.25 <= pb_pct <= 0.55 and
                    vol_ratio >= 1.2)
        _is_medium = (imp_move >= 1.5 * atr and
                      0.20 <= pb_pct <= 0.65 and
                      vol_ratio >= vol_min)

        if _is_high:
            confidence = "HIGH"
        elif _is_medium:
            confidence = "MEDIUM"
        else:
            result["reason"] = "insufficient_quality"
            return result

        result.update({
            "direction":  direction,
            "reason":     "ok",
            "confidence": confidence,
            "vol_ratio":  vol_ratio,
            "atr":        atr,
            "sl_raw":     sl_raw,
            "entry":      entry,
            "imp_start":  imp_start,
            "imp_end":    imp_end,
            "pb_pct":     round(pb_pct, 2),
            "setup_type": "CONT",
        })
        return result

    except Exception as e:
        result["reason"] = f"error:{e}"
        return result


def _eval_momentum(df, trade_mode="PROD", symbol=""):
    """
    Path D: momentum breakout.
    Trigger candle: df.iloc[-2] (closed, confirmed)
    Entry candle:   df.iloc[-1] close (follow-through)
    Confidence:     HIGH only — binary pass/fail
    """
    result = {"direction": None, "reason": "no_momentum", "setup_type": "MOMENTUM"}
    try:
        if df is None or len(df) < 20:
            return result

        import pandas as _pd6
        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        closes = df["close"].astype(float)
        opens  = df["open"].astype(float)
        vols   = df["volume"].astype(float)
        prev_c = closes.shift(1)
        tr = _pd6.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)
        atr    = float(tr.rolling(14).mean().iloc[-2])

        if atr <= 0:
            result["reason"] = "zero_atr"
            return result

        # ── Trigger candle (df.iloc[-2], closed) ─────────────────
        t_open  = float(opens.iloc[-2])
        t_close = float(closes.iloc[-2])
        t_high  = float(highs.iloc[-2])
        t_low   = float(lows.iloc[-2])
        t_body  = abs(t_close - t_open)
        t_range = t_high - t_low + 1e-9
        t_vol   = float(vols.iloc[-2])
        avg_vol = float(vols.iloc[-20:-2].mean())
        vol_ratio = t_vol / avg_vol if avg_vol > 0 else 0.0

        # Direction from trigger candle
        if t_close > t_open:
            direction = "LONG"
            wick_opp  = t_open - t_low    # lower wick (opposite to LONG direction)
        else:
            direction = "SHORT"
            wick_opp  = t_high - t_open   # upper wick (opposite to SHORT direction)

        # ── Condition 1: Body size ≥ 1.2 ATR ─────────────────────
        if t_body < 1.2 * atr:
            result["reason"] = f"body_too_small:{round(t_body/atr,2)}x"
            return result

        # ── Condition 2: Body ratio ≥ 0.70 ───────────────────────
        body_ratio = t_body / t_range
        if body_ratio < 0.70:
            result["reason"] = f"body_ratio_low:{round(body_ratio,2)}"
            return result

        # ── Condition 3: Opposite wick ≤ 20% of body ─────────────
        if wick_opp > 0.20 * t_body:
            result["reason"] = f"wick_too_large:{round(wick_opp/t_body,2)}"
            return result

        # ── Condition 4: Volume ≥ 2.0× average ───────────────────
        if vol_ratio < 2.0:
            result["reason"] = f"volume_low:{round(vol_ratio,2)}x"
            return result

        # ── Condition 5: Structural break beyond 10-candle range ──
        ref_highs = float(highs.iloc[-14:-4].max())
        ref_lows  = float(lows.iloc[-14:-4].min())

        if direction == "LONG" and t_close <= ref_highs:
            result["reason"] = "no_structural_break_high"
            return result
        if direction == "SHORT" and t_close >= ref_lows:
            result["reason"] = "no_structural_break_low"
            return result

        # ── Condition 6: BTC regime alignment (hard gate) ─────────
        btc = _get_btc_regime()
        if direction == "LONG"  and btc != "BULLISH":
            result["reason"] = f"btc_not_bullish:{btc}"
            return result
        if direction == "SHORT" and btc != "BEARISH":
            result["reason"] = f"btc_not_bearish:{btc}"
            return result

        # ── Condition 7: Follow-through candle (df.iloc[-1]) ──────
        ft_close = float(closes.iloc[-1])
        ft_open  = float(opens.iloc[-1])
        _ft2_conflict = False
        if direction == "LONG"  and ft_close <= ft_open:
            _ft2_conflict = True
        if direction == "SHORT" and ft_close >= ft_open:
            _ft2_conflict = True
        if _ft2_conflict:
            if result.get("confidence") == "HIGH":
                result["confidence"] = "MEDIUM"
                result["_ft2_downgrade"] = True
            else:
                result["reason"] = "no_follow_through"
            return result

        # ── Entry + SL ────────────────────────────────────────────
        entry = ft_close
        buf   = atr * 0.15
        if direction == "LONG":
            sl_raw = t_low - buf
        else:
            sl_raw = t_high + buf

        risk = abs(entry - sl_raw)
        if risk <= 0:
            result["reason"] = "zero_risk"
            return result

        result.update({
            "direction":  direction,
            "reason":     "ok",
            "confidence": "HIGH",
            "vol_ratio":  vol_ratio,
            "body_ratio": round(body_ratio, 3),
            "atr":        atr,
            "sl_raw":     sl_raw,
            "entry":      entry,
            "setup_type": "MOMENTUM",
        })
        return result

    except Exception as e:
        result["reason"] = f"error:{e}"
        return result


def run_signal_only(symbol):
    """
    Final assembly signal engine.
    HTF = confidence only, never blocks.
    Hard blocks: no sweep, no reclaim, no volume, dead market, low vol+conf.
    """
    try:
        # ── HTF confidence (never blocks) ────────────────────────
        try:
            df1h = get_kline(symbol, "60")
            df4h = get_kline(symbol, "240")
            df1d = get_kline(symbol, "D")
            t1h  = _simple_trend(df1h)
            t4h  = _stable_trend_4h(df4h, symbol)
            t1d  = _simple_trend(df1d)
        except Exception:
            t1h = t4h = t1d = "NEUTRAL"

        # ── 15M data ─────────────────────────────────────────────
        df15m = get_kline(symbol, "15")
        if df15m is None or len(df15m) < 25:
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: insufficient data", None

        price  = float(df15m["close"].iloc[-2])   # use closed candle

        # ── True ATR (14-period) ──────────────────────────────────
        highs  = df15m["high"].astype(float).iloc[:-1]
        lows   = df15m["low"].astype(float).iloc[:-1]
        closes = df15m["close"].astype(float).iloc[:-1]
        prev_c = closes.shift(1)
        tr     = pd.concat([
            highs - lows,
            (highs - prev_c).abs(),
            (lows  - prev_c).abs()
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])



        # ── Hard block: sweep + reclaim + volume ──────────────────
        ev = _eval_sweep(df15m)
        setup_type  = "SWEEP"
        is_breakout = False

        if ev["reason"] != "ok" or ev["direction"] is None:
            bv = _eval_breakout(df15m)
            if bv["direction"] is None:
                # Path B: consolidation breakout
                cb = _eval_consolidation_breakout(df15m, _TRADE_MODE)
                if cb.get("direction"):
                    bv = {
                        "direction":  cb["direction"],
                        "vol_ratio":  cb.get("vol_ratio", 1.0),
                        "level":      cb.get("ref_high") if cb["direction"]=="LONG" else cb.get("ref_low"),
                        "reason":     "ok",
                        "sl_raw":     cb.get("sl_raw"),
                        "setup_type": "BREAKOUT",
                    }
                else:
                    # Path C: continuation
                    cc = _eval_continuation(df15m, _TRADE_MODE)
                    if cc.get("direction"):
                        bv = {
                            "direction":  cc["direction"],
                            "vol_ratio":  cc.get("vol_ratio", 1.0),
                            "level":      cc.get("imp_end"),
                            "reason":     "ok",
                            "sl_raw":     cc.get("sl_raw"),
                            "atr":        cc.get("atr", 0),
                            "setup_type": "CONT",
                            "confidence": cc.get("confidence", "MEDIUM"),
                        }
                    else:
                        _reasons = f"sweep:{ev.get('reason','?')} | breakout:{cb.get('reason','?')} | cont:{cc.get('reason','?')}"
                        return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: no_setup\nDETAIL: {_reasons}", None
            direction        = bv["direction"]
            vol_ratio        = bv["vol_ratio"]
            reclaim_strength = 1.0
            setup_type       = "BREAKOUT"
            is_breakout      = True
        else:
            direction         = ev["direction"]
            vol_ratio         = ev["vol_ratio"]
            reclaim_strength  = ev.get("reclaim_strength", 0.0)

        # ── Mode-based thresholds ─────────────────────────────────
        if SIGNAL_MODE == "SOFT":
            RECLAIM_THRESHOLD = 0.05
            VOL_BLOCK         = 1.0
            VOL_REDUCE        = 1.05
        else:  # STRICT
            RECLAIM_THRESHOLD = 0.05
            VOL_BLOCK         = 1.0
            VOL_REDUCE        = 1.2

        # ── HTF confidence score ──────────────────────────────────
        def aligns(t, d):
            return (t == "BULLISH" and d == "LONG") or (t == "BEARISH" and d == "SHORT")
        score = sum([aligns(t1h, direction), aligns(t4h, direction), aligns(t1d, direction)])
        if score == 3:   confidence = "HIGH"
        elif score == 2: confidence = "MEDIUM"
        else:            confidence = "LOW"

        # ── 4H trend: confidence modifier (no hard block) ──────────
        if (direction == "LONG"  and t4h == "BEARISH") or            (direction == "SHORT" and t4h == "BULLISH"):
            if confidence == "HIGH":     confidence = "MEDIUM"
            elif confidence == "MEDIUM": confidence = "LOW"
            # LOW stays LOW
        elif t4h == "NEUTRAL":
            if confidence == "HIGH":     confidence = "MEDIUM"
            elif confidence == "MEDIUM": confidence = "LOW"

        # ── MACRO TREND HARD BLOCK (Phase 1 filter) ─────────────
        # Block trades that go against 4H trend
        # Allow NEUTRAL — only block confirmed counter-trend
        _macro_block = False
        if direction == "LONG" and t4h == "BEARISH":
            _macro_block = True
        elif direction == "SHORT" and t4h == "BULLISH":
            _macro_block = True
        if _macro_block:
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: macro_filter\nDETAIL: {direction} blocked by 4H={t4h}", None

        # ── BTC regime filter (light) ─────────────────────────────
        _btc = _get_btc_regime()
        if _btc == "BEARISH" and direction == "LONG":
            if confidence == "HIGH":     confidence = "MEDIUM"
            elif confidence == "MEDIUM": confidence = "LOW"
        elif _btc == "BULLISH" and direction == "SHORT":
            if confidence == "HIGH":     confidence = "MEDIUM"
            elif confidence == "MEDIUM": confidence = "LOW"



        # ── Momentum filter (last 3 closed candles) ──────────────
        _c3 = df15m["close"].astype(float).iloc[-4:-1]
        _o3 = df15m["open"].astype(float).iloc[-4:-1]
        _bearish_streak = all(_c3.iloc[i] < _o3.iloc[i] for i in range(3))
        _bullish_streak = all(_c3.iloc[i] > _o3.iloc[i] for i in range(3))
        if _bearish_streak and direction == "LONG":
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: bearish momentum (3 red candles)", None
        if _bullish_streak and direction == "SHORT":
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: bullish momentum (3 green candles)", None


        # ── Setup classification + entry zone filter ─────────────
        _entry_price = float(df15m["close"].astype(float).iloc[-2])
        _low20c = float(df15m["low"].astype(float).iloc[-21:-1].min())
        _high20c = float(df15m["high"].astype(float).iloc[-21:-1].max())
        _rng20c = _high20c - _low20c
        _posc   = (_entry_price - _low20c) / _rng20c if _rng20c > 0 else 0.5
        if _posc < 0.3 or _posc > 0.7:
            setup_class = "REV"
        else:
            setup_class = "CONT"

        if not is_breakout:
            _ft_open  = float(df15m["open"].astype(float).iloc[-1])
            _dist     = abs(_entry_price - _ft_open)
            if _TRADE_MODE == "MEDIUM":
                _max_dist = atr * 0.8 if setup_class == "REV" else atr * 1.2
            else:
                _max_dist = atr * 0.6 if setup_class == "REV" else atr * 0.9
            if _dist > _max_dist:
                return None, (f"COIN: {symbol}\nSTATUS: NO TRADE\n"
                              f"REASON: late entry ({setup_class}, dist={round(_dist/atr,1)}R)"), None

        if not is_breakout and setup_class == "CONT":
            _c2h = float(df15m["high"].astype(float).iloc[-4:-2].max())
            _c2l = float(df15m["low"].astype(float).iloc[-4:-2].min())
            _spd = (_c2h - _c2l) / atr if atr > 0 else 0
            if _spd > 2.0:
                return None, (f"COIN: {symbol}\nSTATUS: NO TRADE\n"
                              f"REASON: momentum extended ({round(_spd,1)}x ATR)"), None


        # ── Follow-through filter (direction only, closed candle) ──
        _ft_close = float(df15m["close"].astype(float).iloc[-1])
        _ft_open  = float(df15m["open"].astype(float).iloc[-1])
        _ft3_conflict = False
        if direction == "LONG"  and _ft_close <= _ft_open:
            _ft3_conflict = True
        if direction == "SHORT" and _ft_close >= _ft_open:
            _ft3_conflict = True
        if _ft3_conflict:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            else:
                return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: no follow-through", None

        # ── Reclaim strength filter ───────────────────────────────
        if not is_breakout:
            if reclaim_strength < RECLAIM_THRESHOLD:
                return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: weak reclaim", None

        # ── Volume filter ─────────────────────────────────────────
        # User-facing mode consistency:
        # MEDIUM = softer volume floor, PROD/PRO = stricter volume floor.
        _vol_hard_min = 0.7 if _TRADE_MODE == "MEDIUM" else 0.8
        if vol_ratio < _vol_hard_min:
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: insufficient volume", None
        elif vol_ratio < VOL_BLOCK:
            if confidence == "HIGH":     confidence = "MEDIUM"
            elif confidence == "MEDIUM": confidence = "LOW"
        elif vol_ratio < VOL_REDUCE:
            if confidence == "HIGH": confidence = "MEDIUM"

        # ── Low volatility filter ─────────────────────────────────
        if "BTC" in symbol:   VOL_THRESHOLD = 0.0015
        elif "ETH" in symbol: VOL_THRESHOLD = 0.0018
        else:                 VOL_THRESHOLD = 0.002

        low_vol = price > 0 and (atr / price) < VOL_THRESHOLD
        if low_vol:
            if confidence == "HIGH": confidence = "MEDIUM"

        # ── SL from sweep candle with buffer ──────────────────────
        MIN_BUFFER_RATIO = 0.0005
        MAX_BUFFER_RATIO = 0.002
        SL_BUFFER = min(
            max(atr * 0.2, price * MIN_BUFFER_RATIO),
            price * MAX_BUFFER_RATIO
        )

        if is_breakout:
            _brk_level = bv.get("level", price)
            if direction == "LONG":
                sl_raw = max(float(df15m.iloc[-1]["low"]),  _brk_level - atr * 0.5)
                emoji  = "🟢"
            else:
                sl_raw = min(float(df15m.iloc[-1]["high"]), _brk_level + atr * 0.5)
                emoji  = "🔴"
        else:
            sl_raw = ev.get("sweep_low") if direction == "LONG" else ev.get("sweep_high")
            emoji  = "🟢" if direction == "LONG" else "🔴"

        if sl_raw is None:
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: sl level missing", None

        sl = round(sl_raw - SL_BUFFER if direction == "LONG" else sl_raw + SL_BUFFER, 4)

        # ── SL side validation ────────────────────────────────────
        if direction == "LONG" and sl >= price:
            print(f"[SL RAW ERROR] {symbol} LONG sl_raw={sl_raw} sl={sl} >= price={price} — dropped")
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: sl above price for LONG", None
        if direction == "SHORT" and sl <= price:
            print(f"[SL RAW ERROR] {symbol} SHORT sl_raw={sl_raw} sl={sl} <= price={price} — dropped")
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: sl below price for SHORT", None

        # ── Minimum SL distance enforcement ──────────────────────
        MIN_SL_DIST = price * 0.001
        if abs(price - sl) < MIN_SL_DIST:
            sl = round(price - MIN_SL_DIST if direction == "LONG" else price + MIN_SL_DIST, 4)

        # ── Risk validation ───────────────────────────────────────
        risk = abs(price - sl)
        MIN_RISK_RATIO = 0.0007 if "BTC" in symbol else 0.001
        if risk <= 0 or (risk / price) < MIN_RISK_RATIO:
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: risk too small", None

        # ── TP calculation ────────────────────────────────────────
        # ── Regime classification ─────────────────────────────────
        _stop_pct = (risk / price) * 100 if price > 0 else 0
        _conf_str = confidence if isinstance(confidence, str) else str(confidence)
        if _stop_pct <= 0.4 and _conf_str == "HIGH":
            _regime = "tight_clean"
            _runner = 2.0
        elif _stop_pct > 1.0:
            _regime = "standard" if _conf_str == "HIGH" else "wide_volatile"
            _runner = 1.5 if _conf_str == "HIGH" else 1.0
        else:
            _regime = "standard"
            _runner = 1.5
        print(f"[REGIME] {symbol} regime={_regime} stop_pct={_stop_pct:.3f}% runner={_runner}R conf={_conf_str}")

        # ── SL sanity check ──────────────────────────────────────
        if direction == "LONG" and sl >= price:
            print(f"[INVALID SIGNAL] LONG {symbol} sl={sl} >= price={price} — dropping")
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: invalid SL for LONG", None
        if direction == "SHORT" and sl <= price:
            print(f"[INVALID SIGNAL] SHORT {symbol} sl={sl} <= price={price} — dropping")
            return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: invalid SL for SHORT", None

        if direction == "LONG":
            tp1 = round(price + risk * 1.0, 4)  # minimum 1R
            tp2 = round(price + risk * _runner, 4)
            tp3 = round(price + risk * (_runner + 1.0), 4)
            if not (tp1 > price and tp2 > tp1 and tp3 > tp2):
                return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: invalid TP levels", None
        else:
            tp1 = round(price - risk * 1.0, 4)  # minimum 1R
            tp2 = round(price - risk * _runner, 4)
            tp3 = round(price - risk * (_runner + 1.0), 4)
            if not (tp1 < price and tp2 < tp1 and tp3 < tp2):
                return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: invalid TP levels", None

        # ── Signal output ─────────────────────────────────────────
        # ── WATCH context confidence upgrade ─────────────────
        import time as _t
        _wmem = WATCH_MEMORY.get(symbol)
        if _wmem and (_t.time() - _wmem["timestamp"]) < 3600:
            _wstr  = _wmem.get("strength", 1)
            _lvls  = ["LOW", "MEDIUM", "HIGH"]
            _bump  = 2 if _wstr >= 3 else 1
            _idx   = _lvls.index(confidence) if confidence in _lvls else 0
            confidence = _lvls[min(_idx + _bump, 2)]

        r_pct      = round((risk / price) * 100, 3)
        reason_str = f"sweep + reclaim + volume | R={r_pct}%"
        sep        = "────────────────────────────"
        r1 = round(abs(tp1-price)/abs(price-sl),1) if abs(price-sl)>0 else 0
        r2 = round(abs(tp2-price)/abs(price-sl),1) if abs(price-sl)>0 else 0
        r3 = round(abs(tp3-price)/abs(price-sl),1) if abs(price-sl)>0 else 0
        label = "✅ CORE SETUP" if confidence=="HIGH" else ("⚡ WATCH ENTRY" if confidence=="MEDIUM" else "⚠️ AGGRESSIVE / OPTIONAL")
        signal_msg = (
            f"{sep}\n"
            f"{label}\n"
            f"COIN: *{symbol}*  |  {emoji} *{direction}*  |  SWEEP · {setup_class}\n"
            f"CONFIDENCE: {confidence}\n"
            f"{sep}\n"
            f"ENTRY:  `{price}`\n"
            f"SL:     `{sl}`\n"
            f"{sep}\n"
            f"TP1:  `{tp1}`  →  {r1}R  _(→ move SL to breakeven)_\n"
            f"TP2:  `{tp2}`  →  {r2}R\n"
            f"TP3:  `{tp3}`  →  {r3}R\n"
            f"{sep}\n"
            f"R: {reason_str}\n"
            f"{sep}"
        )
        # ── ENTRY QUALITY FILTER ─────────────────────────────────────────────────────────
        # Downgrade HIGH→MEDIUM for phase, late entries, exhaustion
        # Phase filter BLOCKS HIGH signals. Pre-move/exhaustion downgrades MEDIUM.
        _entry_filter_reason = None
        try:
            if confidence == "HIGH":
                # Phase filter FIRST — hard block for HIGH confidence exhaustion
                try:
                    _df4h_pf = get_kline(symbol, "240")
                    _phase = _phase_filter(df15m, _df4h_pf, direction, price, symbol)
                    if _phase:
                        print(f"[PHASE BLOCK] {symbol} HIGH signal BLOCKED — phase exhaustion")
                        return None, None, None
                except Exception as _pfe:
                    print(f"[PHASE FILTER ERROR] {symbol}: {_pfe}")
            if confidence == "HIGH" and df15m is not None and len(df15m) >= 20:
                # Filter 1: Pre-move detection (late entry)
                _recent_low = df15m['low'].rolling(48).min().iloc[-1]
                if _recent_low > 0 and price > _recent_low:
                    _pre_move_pct = (price - _recent_low) / _recent_low * 100
                    if _pre_move_pct > 8.0:
                        confidence = "MEDIUM"
                        _entry_filter_reason = f"late_entry_pre_move={_pre_move_pct:.1f}%"
                # Filter 2: Exhaustion candle detection
                if confidence == "HIGH":
                    _bodies = (df15m['close'] - df15m['open']).abs()
                    _avg_body = _bodies.rolling(20).mean().iloc[-1]
                    _entry_body = _bodies.iloc[-1]
                    if _avg_body > 0 and _entry_body > _avg_body * 2.5:
                        confidence = "MEDIUM"
                        _entry_filter_reason = "exhaustion_candle"
            if _entry_filter_reason:
                print(f"[ENTRY FILTER] {symbol} downgraded HIGH→MEDIUM reason={_entry_filter_reason}")
        except Exception as _ef_ex:
            print(f"[ENTRY FILTER ERROR] {symbol}: {_ef_ex}")
        # ─────────────────────────────────────────────────────────

        # Build structured signal dict for executor
        _signal_dict = {
            "symbol":      symbol,
            "direction":   direction,
            "confidence":  confidence,
            "setup_class": "CORE" if confidence == "HIGH" else confidence,
            "price":       price,
            "sl":          sl,
            "tp1":         tp1,
            "tp2":         tp2,
            "regime":      _regime,
            "stop_pct":    round(_stop_pct, 4),
            "runner_mult": _runner,
            "tp3":         tp3,
            "r_pct":       r_pct,
            "trend4h":     t4h,
            "trend1h":     t1h,
            "trend1d":     t1d,
        }
        print(f"[SIGNAL DEBUG] {symbol} dir={direction} conf={confidence} t4h={t4h} t1h={t1h}")
        return signal_msg, None, _signal_dict

    except Exception:
        return None, f"COIN: {symbol}\nSTATUS: NO TRADE\nREASON: error", None


def get_market_context():
    try:
        df_btc = get_kline("BTCUSDT","D")
        df_eth = get_kline("ETHUSDT","D")
        df_sol = get_kline("SOLUSDT","D")
        df_bnb = get_kline("BNBUSDT","D")
        def trend3(df):
            if df is None or len(df) < 4: return "FLAT"
            c = df["close"].astype(float)
            ch = (c.iloc[-1] - c.iloc[-3]) / c.iloc[-3] * 100
            return "UP" if ch > 1.5 else ("DOWN" if ch < -1.5 else "FLAT")
        btc_dir = trend3(df_btc)
        eth_dir = trend3(df_eth)
        sol_dir = trend3(df_sol)
        bnb_dir = trend3(df_bnb)
        alts = [eth_dir, sol_dir, bnb_dir]
        btcd = "UP" if btc_dir=="UP" and alts.count("UP")<=1 else ("DOWN" if btc_dir=="DOWN" and alts.count("UP")>=2 else "FLAT")
        total3 = "UP" if alts.count("UP")>=2 else ("DOWN" if alts.count("DOWN")>=2 else "FLAT")
        if btcd=="UP" and total3 in ("DOWN","FLAT"): mode="BTC DOMINANCE"
        elif btcd=="DOWN" and total3=="UP": mode="ALT SEASON"
        elif btc_dir=="DOWN" and total3=="DOWN": mode="RISK OFF"
        else: mode="NEUTRAL"
        return f"BTC.D: {btcd}\nTOTAL: {btc_dir}\nTOTAL3: {total3}\nMARKET MODE: {mode}"
    except Exception:
        return "MARKET CONTEXT: error"

def run_signal_raw(symbol):
    """
    RAW mode — no blocking. Shows any sweep or breakout detected.
    User decides manually. Returns (signal_msg, reason).
    """
    try:
        df15m = get_kline(symbol, "15")
        if df15m is None or len(df15m) < 25:
            return None, f"COIN: {symbol}\nSTATUS: NO DATA", None

        price  = float(df15m["close"].iloc[-1])
        highs  = df15m["high"].astype(float)
        lows   = df15m["low"].astype(float)
        closes = df15m["close"].astype(float)
        prev_c = closes.shift(1)
        import pandas as pd
        tr  = pd.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # Check sweep first, then breakout
        ev = _eval_sweep(df15m)
        bv = _eval_breakout(df15m)

        if ev.get("direction"):
            direction  = ev["direction"]
            setup_type = "SWEEP"
            vol_ratio  = ev.get("vol_ratio", 0.0)
            reclaim    = ev.get("reclaim_strength", 0.0)
        elif bv.get("direction"):
            direction  = bv["direction"]
            setup_type = "BREAKOUT"
            vol_ratio  = bv.get("vol_ratio", 0.0)
            reclaim    = 1.0
        else:
            return None, f"COIN: {symbol}\nSTATUS: NO SETUP", None

        # Notes — flag weak conditions
        notes = []
        if vol_ratio < 1.0:   notes.append("low volume")
        if reclaim < 0.02:    notes.append("weak reclaim")
        _note_vol_min = 0.7 if _TRADE_MODE == "MEDIUM" else 0.8
        if vol_ratio >= _note_vol_min and reclaim >= 0.02: notes.append("filters OK")

        # HTF confidence
        try:
            t1h = _simple_trend(get_kline(symbol, "60"))
            t4h = _simple_trend(get_kline(symbol, "240"))
            t1d = _simple_trend(get_kline(symbol, "D"))
            def aligns(t, d):
                return (t=="BULLISH" and d=="LONG") or (t=="BEARISH" and d=="SHORT")
            score = sum([aligns(t1h,direction),aligns(t4h,direction),aligns(t1d,direction)])
            confidence = "HIGH" if score==3 else ("MEDIUM" if score==2 else "LOW")
        except Exception:
            confidence = "LOW"

        # SL/TP
        MIN_BUFFER_RATIO = 0.0005
        MAX_BUFFER_RATIO = 0.002
        SL_BUFFER = min(max(atr*0.2, price*MIN_BUFFER_RATIO), price*MAX_BUFFER_RATIO)
        if setup_type == "SWEEP":
            sl_raw = ev.get("sweep_low") if direction=="LONG" else ev.get("sweep_high")
            sl_raw = sl_raw if sl_raw else (price - atr*1.5 if direction=="LONG" else price + atr*1.5)
        else:
            sl_raw = price - atr*1.5 if direction=="LONG" else price + atr*1.5

        sl = sl_raw - SL_BUFFER if direction=="LONG" else sl_raw + SL_BUFFER
        MIN_SL_DIST = price * 0.001
        if abs(price - sl) < MIN_SL_DIST:
            sl = price - MIN_SL_DIST if direction=="LONG" else price + MIN_SL_DIST
        sl = round(sl, 4)

        risk = abs(price - sl)
        if risk <= 0: risk = price * 0.001

        emoji = "🟢" if direction=="LONG" else "🔴"
        if direction=="LONG":
            tp1 = round(price + risk*1.2, 4)
            tp2 = round(price + risk*2,   4)
            tp3 = round(price + risk*3,   4)
        else:
            tp1 = round(price - risk*1.2, 4)
            tp2 = round(price - risk*2,   4)
            tp3 = round(price - risk*3,   4)

        note_str = " | ".join(notes)
        sep = "────────────────────────────"
        msg = (
            f"{sep}\n"
            f"COIN: *{symbol}*\n"
            f"TYPE: {emoji} *{direction}*\n"
            f"SETUP: {setup_type}\n"
            f"CONFIDENCE: {confidence}\n\n"
            f"ENTRY: `{price}`\n"
            f"SL: `{sl}`\n"
            f"TP1: `{tp1}`\n"
            f"TP2: `{tp2}`\n"
            f"TP3: `{tp3}`\n\n"
            f"NOTE: {note_str}\n"
            f"{sep}"
        )
        return msg, None

    except Exception as e:
        return None, f"COIN: {symbol}\nSTATUS: error", None


# ═══════════════════════════════════════════════════════════════
# WATCH LAYER — Pre-move compression detection
# Additive only. Does NOT affect run_signal_only or any filter.
# ═══════════════════════════════════════════════════════════════

WATCH_MEMORY = {}   # {symbol: {"count": int, "timestamp": float}}

def run_watch_only(symbol):
    """
    Detects pre-move compression conditions on 15M closed candles.
    Returns (watch_msg, None) or (None, reason).
    Never triggers entry. Never affects existing logic.
    """
    import time as _time
    try:
        df = get_kline(symbol, "15")
        if df is None or len(df) < 30:
            return None, "insufficient_data"

        price  = float(df["close"].astype(float).iloc[-2])
        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        closes = df["close"].astype(float)
        opens  = df["open"].astype(float)
        vols   = df["volume"].astype(float)
        prev_c = closes.shift(1)

        import pandas as pd
        tr = pd.concat([
            highs - lows,
            (highs - prev_c).abs(),
            (lows  - prev_c).abs()
        ], axis=1).max(axis=1)

        # ── 1. ATR compression ────────────────────────────────
        atr_now  = float(tr.iloc[-15:-1].mean())
        atr_prev = float(tr.iloc[-29:-15].mean())
        atr_ratio = atr_now / atr_prev if atr_prev > 0 else 1.0
        cond_atr  = atr_ratio < 0.65

        # ── 2. Body compression ───────────────────────────────
        body_10   = float((closes.iloc[-11:-1] - opens.iloc[-11:-1]).abs().mean())
        body_20   = float((closes.iloc[-21:-1] - opens.iloc[-21:-1]).abs().mean())
        body_ratio = body_10 / body_20 if body_20 > 0 else 1.0
        cond_body  = body_ratio < 0.6

        # ── 3. Range coiling ──────────────────────────────────
        h20 = float(highs.iloc[-21:-1].max())
        l20 = float(lows.iloc[-21:-1].min())
        h10 = float(highs.iloc[-11:-1].max())
        l10 = float(lows.iloc[-11:-1].min())
        rng20 = h20 - l20
        rng10 = h10 - l10
        range_ratio = rng10 / rng20 if rng20 > 0 else 1.0
        cond_range  = range_ratio < 0.7

        # ── 4. Directional pressure ───────────────────────────
        c10 = closes.iloc[-11:-1]
        o10 = opens.iloc[-11:-1]
        bull = sum(c10.iloc[i] > o10.iloc[i] for i in range(10))
        bear = 10 - bull
        pressure = abs(bull - bear) / 10
        bias = "LONG" if bull >= bear else "SHORT"
        cond_pressure = pressure >= 0.4 and cond_range

        # ── 5. Volume dryness ─────────────────────────────────
        vol_10 = float(vols.iloc[-11:-1].mean())
        vol_20 = float(vols.iloc[-21:-1].mean())
        vol_ratio = vol_10 / vol_20 if vol_20 > 0 else 1.0
        cond_vol = vol_ratio < 0.7

        # ── Level test (repeated touches) ─────────────────────
        touch_zone = atr_now * 0.2
        high_touches = sum(1 for i in range(-21, -1)
                           if abs(float(highs.iloc[i]) - h20) < touch_zone)
        low_touches  = sum(1 for i in range(-21, -1)
                           if abs(float(lows.iloc[i])  - l20) < touch_zone)
        cond_level = (high_touches >= 3 and bias == "SHORT") or                      (low_touches  >= 3 and bias == "LONG")

        # ── Count conditions ──────────────────────────────────
        required = 2 if cond_vol else 3
        conds = [cond_atr, cond_body, cond_range, cond_pressure,
                 cond_vol, cond_level]
        count = sum(conds)
        if count < required:
            return None, f"compression_score_{count}/{required}"

        # ── FILTERS ───────────────────────────────────────────
        # 1. Suppress if already trending in bias direction
        t4h = _simple_trend(get_kline(symbol, "240"))
        if t4h == "BULLISH" and bias == "LONG":  return None, "trend_aligned_skip"
        if t4h == "BEARISH" and bias == "SHORT": return None, "trend_aligned_skip"

        # 2. Suppress extreme position
        pos = (price - l20) / rng20 if rng20 > 0 else 0.5
        if bias == "LONG"  and pos > 0.75: return None, "price_too_high"
        if bias == "SHORT" and pos < 0.25: return None, "price_too_low"

        # 3. Min range size
        if rng20 < atr_now * 3: return None, "range_too_small"

        # 4. Suppress if already moving
        last_move = abs(float(closes.iloc[-2]) - float(closes.iloc[-4])) / atr_now                     if atr_now > 0 else 0
        if last_move > 1.5: return None, "move_already_started"

        # ── Build reason string ───────────────────────────────
        reasons = []
        if cond_atr:      reasons.append(f"ATR compressed ({round(atr_ratio,2)}x)")
        if cond_body:     reasons.append(f"body compressed ({round(body_ratio,2)}x)")
        if cond_range:    reasons.append(f"range coiling ({round(range_ratio,2)}x)")
        if cond_pressure: reasons.append(f"{bias} pressure ({bull if bias=='LONG' else bear}/10)")
        if cond_vol:      reasons.append(f"volume dry ({round(vol_ratio,2)}x)")
        if cond_level:    reasons.append(f"level tested {max(high_touches,low_touches)}x")

        # ── Clustering / strength ─────────────────────────────
        now = _time.time()
        mem = WATCH_MEMORY.get(symbol, {"strength": 0, "timestamp": 0})
        if now - mem["timestamp"] < 3600:   # within 60min TTL
            mem["strength"] += 1
        else:
            mem["strength"] = 1
        mem["timestamp"] = now
        WATCH_MEMORY[symbol] = mem
        label = "WATCH ⚡ STRONG" if mem["strength"] >= 3 else "WATCH"

        reason_str = "\n• ".join(reasons)
        msg = (
            f"\n⚠️ *{label}* — *{symbol}*\n"
            f"SETUP: PRE-MOVE COMPRESSION\n"
            f"BIAS: {bias}\n\n"
            f"REASON:\n• {reason_str}\n\n"
            f"ACTION: wait for sweep + reclaim confirmation"
        )
        return msg, None

    except Exception as e:
        return None, f"error:{e}"


def run_full_audit(symbol):
    """Full decision audit. Read-only — does not modify state."""
    import time as _t
    import pandas as pd
    lines = []
    def log(s=""): lines.append(s)
    try:
        log(f"\n{'='*44}")
        log(f"  AUDIT: {symbol}")
        log(f"{'='*44}")
        df15m = get_kline(symbol, "15")
        df4h  = get_kline(symbol, "240")
        if df15m is None or len(df15m) < 30:
            log("ERROR: insufficient data"); return "\n".join(lines)
        price  = float(df15m["close"].astype(float).iloc[-1])   # follow-through candle close
        highs  = df15m["high"].astype(float)
        lows   = df15m["low"].astype(float)
        closes = df15m["close"].astype(float)
        opens  = df15m["open"].astype(float)
        vols   = df15m["volume"].astype(float)
        prev_c = closes.shift(1)
        tr = pd.concat([highs-lows,(highs-prev_c).abs(),(lows-prev_c).abs()],axis=1).max(axis=1)
        atr     = float(tr.rolling(14).mean().iloc[-2])
        t4h     = _simple_trend(df4h)
        avg_vol = float(vols.iloc[-21:-1].mean())
        cur_vol = float(vols.iloc[-2])
        vol_r   = cur_vol / avg_vol if avg_vol > 0 else 0
        h20 = float(highs.iloc[-21:-1].max())
        l20 = float(lows.iloc[-21:-1].min())
        log(f"\n1. MARKET CONTEXT")
        log(f"   Price:      {price}")
        log(f"   ATR(15M):   {round(atr,6)} ({round(atr/price*100,3)}%)")
        log(f"   4H Trend:   {t4h}")
        log(f"   Vol ratio:  {round(vol_r,2)}")
        log(f"   20-bar H/L: {round(h20,4)} / {round(l20,4)}")
        log(f"\n2. SETUP DETECTION")
        ev = _eval_sweep(df15m)
        bv = _eval_breakout(df15m)
        if ev.get("direction"):
            log(f"   Sweep:      YES — {ev['direction']}")
            log(f"   Sweep low:  {ev.get('sweep_low')}  high: {ev.get('sweep_high')}")
            log(f"   Reclaim:    {round(ev.get('reclaim_strength',0),3)}")
        else:
            log(f"   Sweep:      NO ({ev.get('reason')})")
        log(f"   Breakout:   {'YES — '+bv['direction'] if bv.get('direction') else 'NO ('+bv.get('reason','')+')' }")
        if not ev.get("direction") and not bv.get("direction"):
            log(f"\nFINAL: BLOCKED — no_sweep_no_breakout"); return "\n".join(lines)
        is_breakout = not ev.get("direction") and bv.get("direction")
        direction   = ev.get("direction") or bv.get("direction")
        vol_ratio_ev= ev.get("vol_ratio") if ev.get("direction") else bv.get("vol_ratio",0)
        entry_price = price
        log(f"\n3. ENTRY")
        log(f"   Direction:  {direction}  Entry: {entry_price}")
        log(f"\n4. FILTERS")
        blocked = None
        _dbg_vol_min = 0.7 if _TRADE_MODE == "MEDIUM" else 0.8
        log(f"   Volume:     {'PASS' if vol_ratio_ev>=_dbg_vol_min else 'BLOCK'} ({round(vol_ratio_ev,2)})")
        if vol_ratio_ev < _dbg_vol_min: blocked = f"insufficient volume ({round(vol_ratio_ev,2)})"
        if not blocked:
            tf_counter = (direction=="LONG" and t4h=="BEARISH") or (direction=="SHORT" and t4h=="BULLISH")
            log(f"   4H Trend:   {'COUNTER→LOW conf' if tf_counter else 'PASS'} ({t4h}/{direction})")
            # soft modifier only — no block
        if not blocked:
            c3=closes.iloc[-4:-1]; o3=opens.iloc[-4:-1]
            b3=all(c3.iloc[i]<o3.iloc[i] for i in range(3))
            g3=all(c3.iloc[i]>o3.iloc[i] for i in range(3))
            mom_block = (b3 and direction=="LONG") or (g3 and direction=="SHORT")
            log(f"   Momentum:   {'BLOCK' if mom_block else 'PASS'} (bear={b3} bull={g3})")
            if mom_block: blocked = "momentum streak"
        if not blocked and not is_breakout:
            rs=ev.get("reclaim_strength",0)
            from agent import SIGNAL_MODE
            thresh=0.05  # synced with run_signal_only
            log(f"   Reclaim:    {'PASS' if rs>=thresh else 'BLOCK'} ({round(rs,3)} vs {thresh})")
            if rs < thresh: blocked = f"weak reclaim ({round(rs,3)})"
        setup_class = "BREAKOUT"
        if not blocked:
            rng20=(h20-l20); posc=(entry_price-l20)/rng20 if rng20>0 else 0.5
            setup_class="REV" if (posc<0.3 or posc>0.7) else "CONT"
            log(f"   Class:      {setup_class} (pos={round(posc,2)})")
            if not is_breakout:
                sr=ev.get("sweep_low") if direction=="LONG" else ev.get("sweep_high")
                if sr:
                    dist=abs(entry_price-float(sr))
                    md=atr*0.6 if setup_class=="REV" else atr*0.9
                    log(f"   Late entry: {'PASS' if dist<=md else 'BLOCK'} dist={round(dist/atr,2)}R max={round(md/atr,2)}R  entry={entry_price} sweep={round(float(sr),4)}")
                    if dist>md: blocked=f"late entry ({setup_class}, dist={round(dist/atr,1)}R)"
        if not blocked and not is_breakout and setup_class=="CONT":
            c2h=float(highs.iloc[-4:-2].max()); c2l=float(lows.iloc[-4:-2].min())
            spd=(c2h-c2l)/atr if atr>0 else 0
            log(f"   Speed:      {'BLOCK' if spd>2.0 else 'PASS'} ({round(spd,1)}x ATR)")
            if spd>2.0: blocked=f"momentum extended ({round(spd,1)}x ATR)"
        if not blocked:
            t1h=_simple_trend(get_kline(symbol,"60")); t1d=_simple_trend(get_kline(symbol,"D"))
            def al(t,d): return (t=="BULLISH" and d=="LONG") or (t=="BEARISH" and d=="SHORT")
            score=sum([al(t1h,direction),al(t4h,direction),al(t1d,direction)])
            conf="HIGH" if score==3 else ("MEDIUM" if score==2 else "LOW")
            _wmem=WATCH_MEMORY.get(symbol); now=_t.time()
            wactive=_wmem and (now-_wmem["timestamp"])<3600
            conf_final=conf
            if wactive:
                lvls=["LOW","MEDIUM","HIGH"]; bump=2 if _wmem.get("strength",1)>=3 else 1
                conf_final=lvls[min(lvls.index(conf)+bump,2)]
            log(f"\n5. CONFIDENCE: {conf} → {conf_final} (HTF={score}/3 WATCH={'YES str='+str(_wmem.get('strength',0)) if wactive else 'NO'})")
            SL_BUF=min(max(atr*0.2,price*0.0005),price*0.002)
            if is_breakout:
                bl=bv.get("level",price)
                sr=max(float(df15m["low"].iloc[-2]),bl-atr*0.5) if direction=="LONG" else min(float(df15m["high"].iloc[-2]),bl+atr*0.5)
            else:
                sr=ev.get("sweep_low") if direction=="LONG" else ev.get("sweep_high")
            if sr is None: sr=price-atr*1.5 if direction=="LONG" else price+atr*1.5
            sl=round(float(sr)-SL_BUF if direction=="LONG" else float(sr)+SL_BUF,4)
            risk=abs(price-sl); minr=0.0007 if "BTC" in symbol else 0.001
            if risk<=0 or (risk/price)<minr:
                log(f"   Risk:       BLOCK ({round(risk/price*100,4)}% < {minr*100}%)"); blocked="risk too small"
            else:
                tp1=round(price+risk*1.2,4) if direction=="LONG" else round(price-risk*1.2,4)
                tp2=round(price+risk*2,4)   if direction=="LONG" else round(price-risk*2,4)
                tp3=round(price+risk*3,4)   if direction=="LONG" else round(price-risk*3,4)
                log(f"\n{'='*44}"); log(f"  FINAL: SIGNAL")
                log(f"  Dir: {direction} ({setup_class})  Conf: {conf_final}")
                log(f"  Entry:{entry_price}  SL:{sl}  R:{round(risk/price*100,3)}%")
                log(f"  TP1:{tp1}  TP2:{tp2}  TP3:{tp3}")
                log(f"{'='*44}"); return "\n".join(lines)
        log(f"\n{'='*44}"); log(f"  FINAL: BLOCKED — {blocked}"); log(f"{'='*44}")
        return "\n".join(lines)
    except Exception as e:
        import traceback; return f"AUDIT ERROR: {e}\n{traceback.format_exc()}"

def run_analysis_telegram(symbol):
    """Public entry point. Never crashes."""
    try:
        return _analyze_telegram(symbol)
    except NameError as e:
        fn = str(e).split("'")[1] if "'" in str(e) else str(e)
        return f"⚠️ Config error: `{fn}` not found. Redeploy bot."
    except Exception as e:
        return f"⚠️ Error analysing {symbol}: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════════

def run_telegram():
    import telebot
    import os
    TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing from environment/.env")
    bot   = telebot.TeleBot(TOKEN)

    @bot.message_handler(func=lambda m: True)
    def handle(message):
        symbol = message.text.strip().upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        bot.reply_to(message, f"⏳ Analysing {symbol}...")
        result = run_analysis_telegram(symbol)
        try:
            bot.reply_to(message, result, parse_mode="Markdown")
        except Exception:
            bot.reply_to(message, result)

    print("Bot running...")
    while True:
        try:
            bot.polling(none_stop=True, timeout=30)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(5)


def main():
    run_telegram()


if __name__ == "__main__":
    main()
