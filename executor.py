"""
executor.py
Bybit V5 live execution engine.
Receives structured signal dict from run_signal_only.
Non-blocking, async-safe. Never modifies agent.py logic.

Config via environment variables (.env):
  BYBIT_API_KEY
  BYBIT_API_SECRET
  BYBIT_ENV            mainnet | testnet  (default: mainnet)
  AUTO_TRADE_ENABLED   true | false       (default: false)
  FIXED_NOTIONAL_USDT  10
  MAX_CONCURRENT_TRADES 1
  SIGNAL_MAX_AGE_SEC   30
  AUTO_TRADE_CLASSES   CORE              (comma-separated)
"""

_EXECUTING_SYMBOLS: set = set()  # in-flight symbols race guard
_TP1_HIT_TIMES: dict = {}  # link_id -> timestamp when TP1 hit
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
import aiohttp

from trade_db import (
    init_db, insert_trade, update_fill, update_tp_hit,
    update_status, get_open_trades, get_trade_by_link_id,
    get_trade_by_symbol, signal_already_processed, count_open_trades
)

log = logging.getLogger("executor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)

# ── Config ────────────────────────────────────────────────────────
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)

def _env(key, default=""):
    return os.environ.get(key, default)

API_KEY    = _env("BYBIT_API_KEY")
API_SECRET = _env("BYBIT_API_SECRET")
ENV        = _env("BYBIT_ENV", "mainnet")

BASE_URL = (
    "https://api.bybit.com"
    if ENV == "mainnet"
    else "https://api-testnet.bybit.com"
)

FIXED_NOTIONAL    = float(_env("FIXED_NOTIONAL_USDT", "10"))
MAX_CONCURRENT    = int(_env("MAX_CONCURRENT_TRADES", "1"))
SIGNAL_MAX_AGE    = int(_env("SIGNAL_MAX_AGE_SEC", "30"))
AUTO_TRADE_CLASSES = [
    c.strip().upper()
    for c in _env("AUTO_TRADE_CLASSES", "CORE").split(",")
]

# Runtime kill switch — toggled by /autotrade_on / /autotrade_off
_AUTO_TRADE_ENABLED = _env("AUTO_TRADE_ENABLED", "false").lower() == "true"

# Telegram send callback — injected by app.py
_telegram_notify = None


def set_telegram_notify(fn):
    """Inject async Telegram send function from app.py."""
    global _telegram_notify
    _telegram_notify = fn


async def _notify(msg: str):
    if _telegram_notify:
        try:
            await _telegram_notify(msg)
        except Exception as e:
            log.error("Telegram notify failed: %s", e)


# ── Kill switch ───────────────────────────────────────────────────
def enable_auto_trade():
    global _AUTO_TRADE_ENABLED
    _AUTO_TRADE_ENABLED = True
    log.info("AUTO_TRADE ENABLED")


def disable_auto_trade():
    global _AUTO_TRADE_ENABLED
    _AUTO_TRADE_ENABLED = False
    log.info("AUTO_TRADE DISABLED")


def is_auto_trade_enabled():
    return _AUTO_TRADE_ENABLED


# ── Bybit REST helpers ────────────────────────────────────────────
def _sign(params: dict, secret: str, timestamp: int, recv_window: int = 5000) -> str:
    param_str = f"{timestamp}{API_KEY}{recv_window}{json.dumps(params, separators=(',', ':'))}"
    return hmac.new(secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()


async def _post(endpoint: str, payload: dict) -> dict:
    ts = int(time.time() * 1000)
    rw = 5000
    body = json.dumps(payload, separators=(',', ':'))
    param_str = f"{ts}{API_KEY}{rw}{body}"
    sig = hmac.new(API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY":     API_KEY,
        "X-BAPI-TIMESTAMP":   str(ts),
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": str(rw),
        "Content-Type":       "application/json",
    }
    url = BASE_URL + endpoint
    log.info("POST %s payload=%s", endpoint, body)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            log.info("RESP %s → %s", endpoint, json.dumps(data))
            return data


async def _get(endpoint: str, params: dict) -> dict:
    ts = int(time.time() * 1000)
    rw = 5000
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    param_str = f"{ts}{API_KEY}{rw}{qs}"
    sig = hmac.new(API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY":     API_KEY,
        "X-BAPI-TIMESTAMP":   str(ts),
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": str(rw),
    }
    url = BASE_URL + endpoint
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return await resp.json()


# ── Instrument info ───────────────────────────────────────────────
_instrument_cache = {}

async def get_instrument_info(symbol: str) -> dict:
    if symbol in _instrument_cache:
        print(f"[INSTRUMENT] {symbol} from cache")
        return _instrument_cache[symbol]
    try:
        print(f"[INSTRUMENT] calling Bybit API for {symbol}")
        data = await asyncio.wait_for(
            _get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol}),
            timeout=8.0
        )
        print(f"[INSTRUMENT] response received for {symbol} retCode={data.get('retCode')}")
        if data.get("retCode") == 0:
            info = data["result"]["list"][0]
            _instrument_cache[symbol] = info
            print(f"[INSTRUMENT] {symbol} parsed OK")
            return info
        print(f"[INSTRUMENT ERROR] {symbol} bad retCode: {data}")
        return {}
    except asyncio.TimeoutError:
        print(f"[INSTRUMENT TIMEOUT] {symbol} — API call exceeded 8s")
        return {}
    except Exception as _ie:
        print(f"[INSTRUMENT ERROR] {symbol} -> {repr(_ie)}")
        import traceback; traceback.print_exc()
        return {}


def _round_qty(qty: float, step: str) -> float:
    step_f = float(step)
    return round(round(qty / step_f) * step_f, 8)


def _round_price(price: float, tick: str) -> float:
    tick_f = float(tick)
    return round(round(price / tick_f) * tick_f, 8)


# ── Main entry point ──────────────────────────────────────────────
async def handle_signal(signal: dict, signal_timestamp: float):
    """
    Called by app.py when a valid signal is generated.
    signal dict keys: symbol, direction, confidence, setup_class,
                      price, sl, tp1, tp2, tp3
    signal_timestamp: unix time when signal was generated
    """
    if not _AUTO_TRADE_ENABLED:
        log.debug("Auto trade disabled — skipping %s", signal.get("symbol"))
        return

    symbol     = signal.get("symbol", "")
    direction  = signal.get("direction", "")
    confidence = signal.get("confidence", "")
    setup_class = signal.get("setup_class", "")
    price      = signal.get("price", 0)
    sl         = signal.get("sl", 0)
    tp1        = signal.get("tp1", 0)
    # RANGE regime + sweep → TP1 shortened to 0.75× (mean-reversion logic)
    _sig_regime = signal.get("_regime", "")
    _sig_is_sweep = signal.get("_is_sweep", False)
    if _sig_regime == "RANGE" and _sig_is_sweep and tp1 and price:
        _tp1_orig = tp1
        if signal.get("direction","") == "LONG":
            tp1 = price + (tp1 - price) * 0.75
        else:
            tp1 = price - (price - tp1) * 0.75
        tp1 = round(tp1, 8)
        print(f"[REGIME] RANGE sweep TP1 adjusted: {_tp1_orig} → {tp1} (0.75x)")
    tp2        = signal.get("tp2", 0)
    tp3        = signal.get("tp3", 0)

    # ── Safety checks (if uncertain → abort) ─────────────────────

    # 1. Signal age
    age = time.time() - signal_timestamp
    print(f"SIGNAL RECEIVED: {symbol} dir={direction} conf={confidence} AT={get_auto_trade_mode()}")
    if age > SIGNAL_MAX_AGE:
        log.warning("Signal too old (%.1fs) — skipping %s", age, symbol)
        return
    print(f"GATE PASSED: {symbol} conf={confidence} AT={get_auto_trade_mode()}")
    print(f"SIGNAL AGE: {symbol} age={age:.1f}s max={SIGNAL_MAX_AGE}")

    # Price deviation check — fetch live mark price
    try:
        _ticker = await _get("/v5/market/tickers", {"category":"linear","symbol":symbol})
        _mark = float(_ticker.get("result",{}).get("list",[{}])[0].get("lastPrice",0) or price)
    except Exception:
        _mark = price
    _dev_pct = abs(_mark - price) / price * 100 if price > 0 else 0
    # Dynamic max deviation based on confidence and R
    _R_pct = abs(price - sl) / price * 100 if price > 0 else 1.0
    _conf_limits = {"HIGH": 1.2, "MEDIUM": 1.0, "LOW": 0.8}
    _max_dev = _conf_limits.get(str(confidence).upper(), 1.0)
    print(f"PRICE CHECK: {symbol} entry={price} mark={_mark:.6f} dev={_dev_pct:.3f}%")
    print(f"[RUNTIME] AUTO_TRADE_CLASSES={AUTO_TRADE_CLASSES} AUTO_TRADE_ENABLED={_AUTO_TRADE_ENABLED}")
    print(f"[DEBUG POST-PRICE] symbol={symbol} setup_class={setup_class!r} classes={AUTO_TRADE_CLASSES!r} auto_trade_enabled={_AUTO_TRADE_ENABLED!r}")
    try:
        print("[STEP] checking setup_class gate")
        if setup_class not in AUTO_TRADE_CLASSES:
            print(f"[BLOCK] setup_class {setup_class!r} not in {AUTO_TRADE_CLASSES}")
            return
        print("[STEP] checking max concurrent")
        open_count = count_open_trades()
        if open_count >= MAX_CONCURRENT:
            print(f"[BLOCK] max concurrent {open_count}/{MAX_CONCURRENT}")
            return
        print("[STEP] checking executing lock")
        if symbol in _EXECUTING_SYMBOLS:
            print(f"[BLOCK] already executing {symbol}")
            return
        print("[STEP] checking existing trade")
        existing = get_trade_by_symbol(symbol)
        if existing:
            print(f"[BLOCK] trade already exists for {symbol}")
            return
        _EXECUTING_SYMBOLS.add(symbol)
        print("[STEP] passed all gates — proceeding to order")

        # 5. Required fields
        print("[STEP] checking required fields")
        if not all([symbol, direction, price, sl, tp1]):
            print(f"[BLOCK] missing required fields symbol={symbol} price={price} sl={sl} tp1={tp1}")
            _EXECUTING_SYMBOLS.discard(symbol)
            return

        # 6. SL/TP sanity
        print("[STEP] SL/TP sanity check")
        if direction == "LONG":
            if not (sl < price < tp1 < tp2 < tp3):
                print(f"[BLOCK] invalid LONG levels sl={sl} price={price} tp1={tp1} tp2={tp2} tp3={tp3}")
                _EXECUTING_SYMBOLS.discard(symbol)
                return
        else:
            if not (sl > price > tp1 > tp2 > tp3):
                print(f"[BLOCK] invalid SHORT levels sl={sl} price={price} tp1={tp1} tp2={tp2} tp3={tp3}")
                _EXECUTING_SYMBOLS.discard(symbol)
                return

        # 6b. Minimum SL distance check
        _sl_dist_pct = abs(price - sl) / price if price > 0 else 0
        _min_sl = float(_env("MIN_SL_PCT", "0.005"))
        _max_sl = float(_env("MAX_SL_PCT", "0.08"))  # 8% max SL distance
        if _sl_dist_pct > _max_sl:
            print(f"[BLOCK] SL too wide: {symbol} sl_dist={_sl_dist_pct:.4%} max={_max_sl:.4%} — rejecting trade")
            return None
        if _sl_dist_pct < _min_sl:
            print(f"[BLOCK] SL too tight: {symbol} sl_dist={_sl_dist_pct:.4%} min={_min_sl:.4%} — rejecting trade")
            _EXECUTING_SYMBOLS.discard(symbol)
            return
        print(f"[STEP] SL distance OK: {_sl_dist_pct:.4%} >= {_min_sl:.4%}")
    except Exception as _gate_e:
        print(f"[EXECUTION ERROR] {symbol} gate -> {repr(_gate_e)}")
        import traceback; traceback.print_exc()
        _EXECUTING_SYMBOLS.discard(symbol)
        return

    # ── Dynamic leverage control (MANDATORY) ─────────────────────
    _sl_dist = abs(price - sl) / price if price > 0 else 0.01
    _raw_lev = (1.0 / _sl_dist) * 0.65 if _sl_dist > 0 else 2
    _leverage = max(2, min(10, int(_raw_lev)))
    print(f"[LEVERAGE] {symbol} sl_pct={_sl_dist:.4%} raw={_raw_lev:.2f} final={_leverage}x")
    try:
        _lev_resp = await _post("/v5/position/set-leverage", {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(_leverage),
            "sellLeverage": str(_leverage),
        })
        _lev_code = _lev_resp.get("retCode", -1)
        _lev_msg  = _lev_resp.get("retMsg", "")
        _lev_ok   = _lev_code == 0 or "not modified" in _lev_msg.lower() or "same" in _lev_msg.lower()
        if _lev_ok:
            print(f"[LEVERAGE SET] {symbol} {_leverage}x OK (code={_lev_code})")
        else:
            print(f"[LEVERAGE SET FAILED] {symbol} code={_lev_code} msg={_lev_msg} — BLOCKING")
            await _notify("\u26a0\ufe0f LEVERAGE SET FAILED\n" + symbol + ": " + _lev_msg + "\nTrade blocked.")
            update_status(link_id, "BLOCKED_LEVERAGE", _lev_msg)
            _EXECUTING_SYMBOLS.discard(symbol)
            return
    except Exception as _le:
        print(f"[LEVERAGE ERROR] {symbol} — {_le} — BLOCKING")
        await _notify("\u26a0\ufe0f LEVERAGE ERROR\n" + symbol + ": " + str(_le) + "\nTrade blocked.")
        _EXECUTING_SYMBOLS.discard(symbol)
        return
    try:
        _pos_pre = await _get("/v5/position/list", {"category":"linear","symbol":symbol})
        _pos_pre_list = _pos_pre.get("result",{}).get("list",[])
        _actual_lev = float(_pos_pre_list[0].get("leverage", 0)) if _pos_pre_list else 0
        if _actual_lev > 0 and abs(_actual_lev - _leverage) > 0.5:
            print(f"[LEVERAGE MISMATCH] {symbol} expected={_leverage}x actual={_actual_lev}x — BLOCKING")
            await _notify("\u26a0\ufe0f LEVERAGE MISMATCH\n" + symbol + ": expected=" + str(_leverage) + "x actual=" + str(_actual_lev) + "x\nTrade blocked.")
            _EXECUTING_SYMBOLS.discard(symbol)
            return
        print(f"[LEVERAGE VERIFY] {symbol} actual={_actual_lev}x expected={_leverage}x OK")
    except Exception as _lve:
        print(f"[LEVERAGE VERIFY WARN] {symbol} could not verify: {_lve} — proceeding")

    # ── Get instrument precision ──────────────────────────────────
    print(f"[STEP] fetching instrument info for {symbol}")
    info = await get_instrument_info(symbol)
    print(f"[STEP] instrument info result: {bool(info)}")
    if not info:
        log.error("No instrument info for %s — aborting", symbol)
        _EXECUTING_SYMBOLS.discard(symbol)
        return

    lot_filter   = next((f for f in info.get("lotSizeFilter", {}).items()), None)
    lot_step     = info.get("lotSizeFilter", {}).get("qtyStep", "0.01")
    price_filter = info.get("priceFilter", {}).get("tickSize", "0.0001")
    min_qty      = float(info.get("lotSizeFilter", {}).get("minOrderQty", "0.01"))

    # ── Calculate quantity ────────────────────────────────────────
    _notional = float(signal.get('notional', FIXED_NOTIONAL))
    # MEDIUM size reduction handled in app.py sizing engine — not duplicated here
    raw_qty = _notional / price
    qty     = _round_qty(raw_qty, lot_step)
    print(f"[STEP] qty={qty} min_qty={min_qty} raw={raw_qty:.6f} notional={_notional} price={price}")
    if qty < min_qty:
        print(f"[BLOCK] qty={qty} below min_qty={min_qty} for {symbol} — increase FIXED_NOTIONAL")
        log.warning("Qty %.6f below minOrderQty %.6f for %s — aborting", qty, min_qty, symbol)
        _EXECUTING_SYMBOLS.discard(symbol)
        return

    # ── Round levels ──────────────────────────────────────────────
    sl_r  = _round_price(sl,  price_filter)
    tp1_r = _round_price(tp1, price_filter)
    tp2_r = _round_price(tp2, price_filter)
    tp3_r = _round_price(tp3, price_filter)

    side     = "Buy" if direction == "LONG" else "Sell"
    link_id  = f"bot_{symbol}_{int(time.time())}"
    signal_id = hashlib.md5(f"{symbol}{direction}{price}{sl}{int(signal_timestamp)}".encode()).hexdigest()

    # 7. Duplicate signal check
    if signal_already_processed(signal_id):
        print(f"[BLOCK] signal already processed for {symbol}")
        _EXECUTING_SYMBOLS.discard(symbol)
        return

    # ── Place order ───────────────────────────────────────────────
    order_payload = {
        "category":     "linear",
        "symbol":       symbol,
        "side":         side,
        "orderType":    "Market",
        "qty":          str(qty),
        "timeInForce":  "IOC",
        "orderLinkId":  link_id,
        "reduceOnly":   False,
        "closeOnTrigger": False,
    }

    log.info("=== PLACING ORDER === %s", json.dumps(order_payload))
    await _notify(
        f"🤖 AUTO-TRADE SENT\n"
        f"Symbol: {symbol}  Side: {side}\n"
        f"Qty: {qty}  Entry: ~{price}\n"
        f"SL: {sl_r}  TP1: {tp1_r}  TP2: {tp2_r}  TP3: {tp3_r}"
    )

    # Record in DB before placing (idempotency)
    # Derive context fields from signal
    def _get_session(h):
        if 0 <= h < 8:   return "ASIA"
        if 8 <= h < 12:  return "LONDON"
        if 12 <= h < 16: return "OVERLAP"
        if 16 <= h < 22: return "NY"
        return "ASIA"
    import datetime as _dt
    _fill_hour = _dt.datetime.utcnow().hour
    _ctx_path   = str(signal.get("_path") or "").replace("B/C","B").strip() or None
    _ctx_t4h    = str(signal.get("trend4h") or signal.get("t4h") or "").upper() or None
    _ctx_t1h    = str(signal.get("trend1h") or signal.get("t1h") or "").upper() or None
    _ctx_score  = signal.get("score") or signal.get("signal_score") or None
    _ctx_sess   = _get_session(_fill_hour)
    _ctx_regime   = str(signal.get("market_regime") or signal.get("_regime") or "")
    _ctx_macro    = str(signal.get("macro_state") or "")
    _ctx_vol_ratio = float(signal.get("vol_ratio") or signal.get("volume_ratio") or 0)
    print(f"[EXECUTOR TRACE] {symbol} conf={confidence} score={_ctx_score} path={_ctx_path} regime={_ctx_regime} macro={_ctx_macro}")
    # Capture equity at entry for drawdown tracking
    _equity_at_entry = 0.0
    try:
        import trade_db as _tdb_dd
        _dd = _tdb_dd.get_drawdown_state()
        if _dd: _equity_at_entry = _dd.get("current_equity", 0.0)
    except Exception: pass

    insert_trade(
        signal_id=signal_id, symbol=symbol, side=side,
        setup_class=setup_class, confidence=confidence,
        order_link_id=link_id, intended_entry=price,
        sl=sl_r, tp1=tp1_r, tp2=tp2_r, tp3=tp3_r, qty=qty,
        path=_ctx_path, t4h=_ctx_t4h, t1h=_ctx_t1h,
        hour_utc=_fill_hour, session=_ctx_sess,
        signal_score=_ctx_score,
        regime=_ctx_regime, macro_state=_ctx_macro,
        equity_at_entry=_equity_at_entry,
        actual_leverage=float(_leverage) if "_leverage" in dir() else None,
        volume_ratio=_ctx_vol_ratio
    )

    try:
        resp = await _post("/v5/order/create", order_payload)
    except Exception as e:
        log.error("Order placement failed: %s", e)
        update_status(link_id, "ERROR", str(e))
        await _notify(f"❌ AUTO-TRADE ERROR\n{symbol}: {e}")
        return

    if resp.get("retCode") != 0:
        msg = resp.get("retMsg", "unknown error")
        log.error("Order rejected: %s", msg)
        update_status(link_id, "REJECTED", msg)
        _EXECUTING_SYMBOLS.discard(symbol)
        await _notify(f"❌ AUTO-TRADE REJECTED\n{symbol}: {msg}")
        return

    order_id = resp.get("result", {}).get("orderId", "")
    update_fill(link_id, order_id, price, "OPEN")
    _EXECUTING_SYMBOLS.discard(symbol)  # release lock after confirmed
    log.info("Order accepted: orderId=%s linkId=%s", order_id, link_id)
    print(f"ORDER CONFIRMED: {symbol} {side} qty={qty} orderId={order_id}")
    # Post-order position verification (read-only, no retry)
    try:
        await asyncio.sleep(3)
        _verify_resp = await _get("/v5/position/list", {"category":"linear","symbol":symbol})
        _pos_list = _verify_resp.get("result",{}).get("list",[])
        _pos_size = float(_pos_list[0].get("size",0)) if _pos_list else 0
        if _pos_size > 0:
            print(f"[ORDER VERIFIED via position] {symbol} size={_pos_size}")
        else:
            print(f"[ORDER FAILED] {symbol} — order accepted but no position found")
    except asyncio.TimeoutError:
        print(f"[ORDER VERIFY UNKNOWN] {symbol} — position check timed out")
    except Exception as _ve:
        print(f"[ORDER VERIFY UNKNOWN] {symbol} — {_ve}")

    async def _place_protection(symbol, side, sl, tp1, qty, link_id, lot_step="0.01", min_qty=0.01):
        """Place SL and first TP after fill."""
        stop_side = "Sell" if side == "Buy" else "Buy"
        sl_payload = {
            "category":  "linear",
            "symbol":    symbol,
            "stopLoss":  str(sl),
            "slTriggerBy": "MarkPrice",
        }
        resp = await _post("/v5/position/trading-stop", sl_payload)
        if resp.get("retCode") != 0:
            log.error("SL placement failed: %s — closing position", resp.get("retMsg"))
            await _notify(f"⚠️ SL FAILED on {symbol} — closing position")
            await _close_position(symbol, side, qty, link_id, reason="SL placement failed")
            return

        log.info("SL placed at %s for %s", sl, symbol)

        # Place TP1 limit order (50% qty, reduce-only)
        if tp1 and float(tp1) > 0:
            tp1_qty = _round_qty(qty * 0.5, lot_step)
            if tp1_qty < min_qty:
                _min_viable = _round_qty(min_qty * 2, lot_step)
                _min_risk = (_min_viable * price) * (abs(price - sl) / price if price > 0 else 0.01)
                _max_risk = float(_env("MAX_RISK_PER_TRADE", "1.0"))
                if _min_risk <= _max_risk:
                    qty = _min_viable
                    tp1_qty = _round_qty(qty * 0.5, lot_step)
                    print(f"[TP1 SCALE] {symbol}: qty scaled to {qty} tp1_qty={tp1_qty}")
                else:
                    print(f"[TP1 SKIP] {symbol}: tp1_qty below min, risk {_min_risk:.3f} > max {_max_risk} — skipping")
            if tp1_qty >= min_qty:
                tp1_payload = {
                    "category":     "linear",
                    "symbol":       symbol,
                    "side":         stop_side,
                    "orderType":    "Limit",
                    "qty":          str(tp1_qty),
                    "price":        str(tp1),
                    "timeInForce":  "GTC",
                    "reduceOnly":   True,
                    "closeOnTrigger": False,
                    "orderLinkId":  f"{link_id}_tp1",
                }
                tp1_resp = await _post("/v5/order/create", tp1_payload)
                if tp1_resp.get("retCode") == 0:
                    log.info("TP1 placed at %s qty=%s for %s", tp1, tp1_qty, symbol)
                    print(f"[TP1 SET] {symbol} TP1={tp1} qty={tp1_qty}")
                else:
                    log.warning("TP1 placement failed for %s: %s", symbol, tp1_resp.get("retMsg"))
                    print(f"[TP1 FAILED] {symbol}: {tp1_resp.get('retMsg')}")

        _dir_emoji = "🟢" if side == "Buy" else "🔴"
        _dir_label = "LONG" if side == "Buy" else "SHORT"
        _conf_display = signal.get("confidence", "—")
        await _notify(
            f"✅ *TRADE OPENED*\n"
            f"────────────────────\n"
            f"{symbol}  {_dir_emoji} *{_dir_label}* | {_conf_display}\n"
            f"Entry:    `{price}`\n"
            f"SL:       `{sl_r}`\n"
            f"TP1:      `{tp1_r}`\n"
            f"Notional: `${_notional:.1f}` USDT\n"
            f"────────────────────"
        )

    async def _close_position(symbol, side, qty, link_id, reason=""):
        """Emergency close — market order reduce-only."""
        close_side = "Sell" if side == "Buy" else "Buy"
        payload = {
            "category":     "linear",
            "symbol":       symbol,
            "side":         close_side,
            "orderType":    "Market",
            "qty":          str(qty),
            "timeInForce":  "IOC",
            "reduceOnly":   True,
            "closeOnTrigger": True,
        }
        log.warning("Emergency close %s qty=%s reason=%s", symbol, qty, reason)
        try:
            _close_resp = await _post("/v5/order/create", payload)
            if _close_resp.get("retCode") == 0:
                print(f"[EMERGENCY CLOSE CONFIRMED] {symbol} qty={qty} reason={reason}")
                update_status(link_id, "EMERGENCY_CLOSED", reason)
                await _notify(f"\U0001F6A8 EMERGENCY CLOSE CONFIRMED\n{symbol}: {reason}")
            else:
                _err = _close_resp.get("retMsg", "unknown")
                print(f"[EMERGENCY CLOSE FAILED] {symbol} — {_err}")
                update_status(link_id, "EMERGENCY_CLOSE_FAILED", _err)
                await _notify(f"\U0001F198 CRITICAL: EMERGENCY CLOSE FAILED\n{symbol}\nReason: {_err}\nManual intervention required")
        except Exception as _ce:
            print(f"[EMERGENCY CLOSE ERROR] {symbol} — {_ce}")
            update_status(link_id, "EMERGENCY_CLOSE_ERROR", str(_ce))
            await _notify(f"\U0001F198 CRITICAL: EMERGENCY CLOSE ERROR\n{symbol}\n{_ce}\nManual intervention required")

    # ── Place protection immediately ──────────────────────────────
    await asyncio.sleep(1)   # brief wait for fill
    try:
        await _place_protection(symbol, side, sl_r, tp1_r, qty, link_id, lot_step=lot_step, min_qty=min_qty)
        print(f"[PROTECTION COMPLETE] {symbol} SL/TP placed")
    except Exception as _pe:
        print(f"[PROTECTION ERROR] {symbol} — {_pe}")
        await _notify(f"\U0001F198 CRITICAL: PROTECTION FAILED\n{symbol}: {_pe}\nManual SL required")
    # Post-order verification + liquidation safety check
    try:
        await asyncio.sleep(3)
        _verify_resp = await _get("/v5/position/list", {"category":"linear","symbol":symbol})
        _pos_list = _verify_resp.get("result",{}).get("list",[])
        _pos_size = float(_pos_list[0].get("size",0)) if _pos_list else 0
        if _pos_size > 0:
            print(f"[ORDER VERIFIED via position] {symbol} size={_pos_size}")
            _liq_price = float(_pos_list[0].get("liqPrice",0) or 0)
            _actual_lev_pos = float(_pos_list[0].get("leverage",0) or 0)
            try:
                import json as _jj2, os as _oj2
                _jp2 = _oj2.path.join(_oj2.path.dirname(__file__), "journal.json")
                with open(_jp2) as _f2: _je2 = _jj2.load(_f2)
                for _t2 in _je2:
                    if _t2.get("order_link_id") == link_id:
                        _t2["actual_leverage"] = _actual_lev_pos
                        break
                with open(_jp2,"w") as _f2: _jj2.dump(_je2, _f2, indent=2)
                print(f"[LEVERAGE WRITTEN] {symbol} actual={_actual_lev_pos}x")
            except Exception as _lw2: print(f"[LEVERAGE WRITE ERROR] {_lw2}")
            if _liq_price > 0:
                _is_long_p = direction == "LONG"
                _sl_beyond = (_is_long_p and sl_r <= _liq_price) or (not _is_long_p and sl_r >= _liq_price)
                _sl_d = abs(price - sl_r)
                _liq_d = abs(price - _liq_price)
                _ratio = _liq_d / _sl_d if _sl_d > 0 else 99
                print(f"[LIQ CHECK] {symbol} sl={sl_r} liq={_liq_price} ratio={_ratio:.2f} unsafe={_sl_beyond}")
                if _sl_beyond or _ratio < 1.0:
                    print(f"[LIQ CRITICAL] {symbol} SL={sl_r} beyond liqPrice={_liq_price} EMERGENCY CLOSE")
                    await _notify("\U0001F198 CRITICAL: SL BEYOND LIQ\n" + symbol + " SL=" + str(sl_r) + " Liq=" + str(_liq_price))
                    try:
                        _cs = "Sell" if direction == "LONG" else "Buy"
                        await _post("/v5/order/create", {"category":"linear","symbol":symbol,"side":_cs,"orderType":"Market","qty":str(_pos_size),"timeInForce":"IOC","reduceOnly":True,"closeOnTrigger":True})
                        print(f"[EMERGENCY CLOSE SENT] {symbol}")
                    except Exception as _ec2: print(f"[EMERGENCY CLOSE FAILED] {symbol} {_ec2}")
                elif _ratio < 1.2:
                    print(f"[LIQ WARNING] {symbol} ratio={_ratio:.2f} borderline")
                    await _notify("\u26a0\ufe0f LIQ WARNING " + symbol + " ratio=" + str(round(_ratio,2)))
            else:
                print(f"[LIQ CHECK] {symbol} liqPrice=0 cannot validate")
        else:
            print(f"[ORDER FAILED] {symbol} no position found")
    except Exception as _ve:
        print(f"[ORDER VERIFY UNKNOWN] {symbol} {_ve}")




# ── TP hit handler (called from websocket) ────────────────────────
async def on_tp_hit(order_link_id: str, tp_num: int, fill_price: float):
    """Called when a TP order fills."""
    trade = get_trade_by_link_id(order_link_id)
    if not trade:
        log.warning("No trade found for link_id %s", order_link_id)
        return

    symbol = trade["symbol"]
    qty    = trade["remaining_qty"]
    sl     = trade["sl"]
    entry  = trade["actual_fill"] or trade["intended_entry"]

    if tp_num == 1:
        close_qty = round(qty * 0.5, 8)
        new_remaining = round(qty - close_qty, 8)
        update_tp_hit(order_link_id, 1, new_remaining, breakeven_moved=True)
        _TP1_HIT_TIMES[order_link_id] = time.time()  # record for trailing
        log.info("TP1 hit recorded for trailing: %s", order_link_id)
        # Move SL to breakeven
        await _move_sl_to_breakeven(symbol, entry, order_link_id)
        await _notify(
            f"✅ TP1 HIT — {symbol}\n"
            f"Fill: {fill_price}  Closed 50%\n"
            f"SL → BREAKEVEN ({entry})"
        )
    elif tp_num == 2:
        close_qty = round(qty * 0.5, 8)   # 50% of remaining = 25% total
        new_remaining = round(qty - close_qty, 8)
        update_tp_hit(order_link_id, 2, new_remaining)
        await _notify(f"⚡ TP2 HIT — {symbol}\nFill: {fill_price}  Closed 25%")
    elif tp_num == 3:
        update_tp_hit(order_link_id, 3, 0)
        update_status(order_link_id, "CLOSED")
        await _notify(f"🏁 TP3 HIT — {symbol} CLOSED\nFill: {fill_price}")


async def _move_sl_to_breakeven(symbol: str, entry: float, link_id: str):
    info = await get_instrument_info(symbol)
    tick = info.get("priceFilter", {}).get("tickSize", "0.0001")
    be   = _round_price(entry, tick)
    payload = {
        "category":    "linear",
        "symbol":      symbol,
        "stopLoss":    str(be),
        "slTriggerBy": "MarkPrice",
    }
    resp = await _post("/v5/position/trading-stop", payload)
    if resp.get("retCode") == 0:
        log.info("SL moved to breakeven %s for %s", be, symbol)
    else:
        log.error("Failed to move SL to BE: %s", resp.get("retMsg"))


# ── Startup reconciliation ────────────────────────────────────────
async def reconcile_on_startup():
    """
    On startup: check DB for PENDING/OPEN trades and verify
    they still exist on Bybit. Mark stale ones as UNKNOWN.
    """
    open_trades = get_open_trades()
    if not open_trades:
        log.info("No open trades to reconcile")
        return

    log.info("Reconciling %d open trades...", len(open_trades))
    for trade in open_trades:
        symbol = trade["symbol"]
        try:
            data = await _get("/v5/position/list", {
                "category": "linear",
                "symbol":   symbol
            })
            positions = data.get("result", {}).get("list", [])
            has_position = any(
                float(p.get("size", 0)) > 0
                for p in positions
            )
            if not has_position:
                log.warning("No position found for %s — marking UNKNOWN", symbol)
                update_status(trade["order_link_id"], "UNKNOWN",
                              "No position on Bybit at startup")
        except Exception as e:
            log.error("Reconcile error for %s: %s", symbol, e)


# ── Init ──────────────────────────────────────────────────────────
def init_executor():
    """Call once at startup."""
    init_db()
    log.info("Executor initialized. AUTO_TRADE=%s ENV=%s", _AUTO_TRADE_ENABLED, ENV)

# ── Auto trade mode (OFF / SOFT / PRO) ───────────────────────────
_AUTO_TRADE_MODE = "OFF"

def set_auto_trade_mode(mode: str, notify_fn=None):
    global _AUTO_TRADE_MODE, _AUTO_TRADE_ENABLED
    mode = mode.upper()
    # Normalize aliases
    _alias = {"ON":"PRO","HIGH":"PRO","MEDIUM":"SOFT","LOW":"OFF"}
    mode = _alias.get(mode, mode)
    if mode not in ("OFF", "SOFT", "PRO"):
        return
    _AUTO_TRADE_MODE = mode
    _AUTO_TRADE_ENABLED = mode != "OFF"

def get_auto_trade_mode() -> str:
    return _AUTO_TRADE_MODE

# Sync mode with AUTO_TRADE_ENABLED on startup
if _AUTO_TRADE_ENABLED:
    _env_mode = _env("AUTO_TRADE_MODE","SOFT").upper()
    _alias2 = {"ON":"PRO","HIGH":"PRO","MEDIUM":"SOFT","LOW":"OFF"}
    _AUTO_TRADE_MODE = _alias2.get(_env_mode, _env_mode if _env_mode in ("OFF","SOFT","PRO") else "SOFT")
