import logging
from telegram import Update, BotCommand
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CallbackQueryHandler,\
     ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from menu import send_main_menu, handle_menu_callback
import level_cache
import reclaim_monitor
load_dotenv()
from ws_manager import start_ws, stop_ws, set_callbacks as ws_set_callbacks
from executor import init_executor, handle_signal, enable_auto_trade, disable_auto_trade, is_auto_trade_enabled, set_telegram_notify, set_auto_trade_mode, get_auto_trade_mode, _get

# ====== DRAWDOWN BREAKER CONFIG ======
import os as _dd_os, json as _dd_json
from datetime import datetime
def _log_blocked_trade(symbol, side, confidence, path, reason, regime=None, sig_dict=None):
    """Log a blocked trade to journal.json for analysis. Additive only — never breaks existing fields."""
    try:
        import json as _jb, os as _ob, time as _tb
        _jpath = _ob.path.join(_ob.path.dirname(__file__), 'journal.json')
        try:
            with open(_jpath) as _f: _entries = _jb.load(_f)
        except Exception: _entries = []
        # Normalize block_category and block_detail from reason string
        _reason_str = reason or ""
        if ":" in _reason_str:
            _bcat, _bdet = _reason_str.split(":", 1)
        else:
            _bcat, _bdet = _reason_str, ""
        try:
            import agent as _ag_mode
            _filter_mode = getattr(_ag_mode, "_TRADE_MODE", "PROD")
        except Exception:
            _filter_mode = globals().get("TRADE_MODE", "PROD")
        try:
            _execution_mode = get_auto_trade_mode()
        except Exception:
            _execution_mode = "UNKNOWN"
        _user_mode = "OFF" if _execution_mode == "OFF" else ("MEDIUM" if _filter_mode == "MEDIUM" else "PRO")

        _entry = {
            "trade_id":       f"blocked_{int(_tb.time()*1000)}_{symbol}",
            "status":         "BLOCKED",
            "user_mode":      _user_mode,
            "filter_mode":    _filter_mode,
            "execution_mode": _execution_mode,
            "symbol":         symbol,
            "side":           side or "UNKNOWN",
            "confidence":     confidence or "UNKNOWN",
            "path":           path or "UNKNOWN",
            "reason":         _reason_str,
            "block_category": _bcat.strip(),
            "block_detail":   _bdet.strip(),
            "regime":         regime or globals().get("_MARKET_REGIME", "UNKNOWN"),
            "macro_state":    sig_dict.get("macro_state") if sig_dict else None,
            "timestamp_open": _tb.time(),
            "pnl_usdt":       0,
            "result":         "BLOCKED",
            "signal_score":   sig_dict.get("signal_score") or sig_dict.get("score") if sig_dict else None,
            "atr_at_signal":  sig_dict.get("atr") or sig_dict.get("atr_at_signal") if sig_dict else None,
            "tp1_dist_pct":   None,
        }
        if sig_dict:
            _ep  = float(sig_dict.get("price", sig_dict.get("entry_price", 0)) or 0)
            _sl  = float(sig_dict.get("sl", 0) or 0)
            _tp1 = float(sig_dict.get("tp1", 0) or 0)
            _entry["entry_price"]   = _ep
            _entry["sl"]            = _sl
            _entry["tp1"]           = _tp1
            _entry["setup_type"]    = sig_dict.get("setup_type", sig_dict.get("setup_class", ""))
            _entry["signal_score"]  = sig_dict.get("score", sig_dict.get("signal_score", None))
            _entry["atr_at_signal"] = sig_dict.get("atr", None)
            # Compute sl_pct and tp1_dist_pct if we have the data
            if _ep > 0 and _sl > 0:
                _entry["sl_pct"] = round(abs(_ep - _sl) / _ep * 100, 4)
            if _ep > 0 and _tp1 > 0:
                _entry["tp1_dist_pct"] = round(abs(_tp1 - _ep) / _ep * 100, 4)
        _entries.append(_entry)
        with open(_jpath, 'w') as _f: _jb.dump(_entries, _f, indent=2)
        print(f"[BLOCKED] {symbol} {side} — {_bcat}: {_bdet}")
    except Exception as _e:
        import traceback as _tbl
        print(f"[BLOCKED LOG ERROR] {_e}")
        _tbl.print_exc()


MAX_DAILY_LOSS_USDT = float(_dd_os.getenv("MAX_DAILY_LOSS_USDT", "3.0"))

# ── Market regime globals (module-level, always defined) ──────────
_MARKET_REGIME       = "RANGE"
_open_positions_cache = []
_MARKET_REGIME_ATR   = 1.0
_REGIME_PREV         = "RANGE"
_CHAOTIC_LOSS_TIMES  = []
_CHAOTIC_PAUSE_UNTIL = 0
DD_COOLDOWN_MIN     = int(_dd_os.getenv("DD_COOLDOWN_MIN", "120"))
_DD_COOLDOWN_UNTIL  = 0
_DD_DAY_STOP        = False

# ====== DYNAMIC SIZING + CORRELATION CONFIG ======
MAX_OPEN_POSITIONS     = int(_dd_os.environ.get("MAX_OPEN_POSITIONS", "6"))
ACCOUNT_BALANCE_USDT   = float(_dd_os.environ.get("ACCOUNT_BALANCE_USDT", "320"))
BASE_RISK_PCT          = float(_dd_os.environ.get("BASE_RISK_PCT", "0.015"))
HIGH_MULT              = float(_dd_os.environ.get("HIGH_MULT", "1.5"))
MEDIUM_MULT            = float(_dd_os.environ.get("MEDIUM_MULT", "1.0"))
MIN_SL_PCT             = float(_dd_os.environ.get("MIN_SL_PCT", "0.005"))
MAX_NOTIONAL_HIGH      = float(_dd_os.environ.get("MAX_NOTIONAL_HIGH", "60"))
MAX_NOTIONAL_MEDIUM    = float(_dd_os.environ.get("MAX_NOTIONAL_MEDIUM", "30"))
MAX_CONCURRENT_RISK_PCT = float(_dd_os.environ.get("MAX_CONCURRENT_RISK_PCT", "0.10"))
MAX_NOTIONAL_HIGH_MAJOR = float(_dd_os.environ.get("MAX_NOTIONAL_HIGH_MAJOR", "200"))
MAJOR_SYMBOLS           = [s.strip() for s in _dd_os.environ.get("MAJOR_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")]

# ====== STARTUP CONFIG LOG ======
print(f"[BOOT] Trading Engine v2.1 — config loading")
print(f"[CONFIG LOADED]")
print(f"  MAX_OPEN_POSITIONS     = {MAX_OPEN_POSITIONS}")
print(f"  ACCOUNT_BALANCE_USDT   = {ACCOUNT_BALANCE_USDT}")
print(f"  BASE_RISK_PCT          = {BASE_RISK_PCT}")
print(f"  MAX_CONCURRENT_RISK_PCT= {MAX_CONCURRENT_RISK_PCT}")
print(f"  MAX_NOTIONAL_HIGH      = {MAX_NOTIONAL_HIGH}")
print(f"  MAX_NOTIONAL_HIGH_MAJOR= {MAX_NOTIONAL_HIGH_MAJOR}")
print(f"  MAX_NOTIONAL_MEDIUM    = {MAX_NOTIONAL_MEDIUM}")
print(f"  MAJOR_SYMBOLS          = {MAJOR_SYMBOLS}")
if MAX_OPEN_POSITIONS < 5:
    print(f"[CONFIG WARNING] MAX_OPEN_POSITIONS too low: {MAX_OPEN_POSITIONS}")
if ACCOUNT_BALANCE_USDT < 50:
    print(f"[CONFIG WARNING] ACCOUNT_BALANCE_USDT too low: {ACCOUNT_BALANCE_USDT}")

CORR_CLUSTERS = {
    "BTC":  ["BTCUSDT", "ETHUSDT"],
    "L1":   ["SOLUSDT", "AVAXUSDT", "DOTUSDT"],
    "MEME": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT"],
}

def _get_signal_notional(confidence, entry, sl, symbol=""):
    if entry <= 0 or sl <= 0:
        return 0
    sl_pct = abs(entry - sl) / entry
    effective_sl = max(sl_pct, MIN_SL_PCT)
    is_major = symbol in MAJOR_SYMBOLS
    MAX_NOTIONAL_MEDIUM_MAJOR = float(_dd_os.environ.get("MAX_NOTIONAL_MEDIUM_MAJOR", "80"))
    if confidence == "HIGH":
        mult = HIGH_MULT
        cap = MAX_NOTIONAL_HIGH_MAJOR if is_major else MAX_NOTIONAL_HIGH
    elif confidence == "MEDIUM":
        mult = MEDIUM_MULT
        cap = MAX_NOTIONAL_MEDIUM_MAJOR if is_major else MAX_NOTIONAL_MEDIUM
    else:
        return 0
    risk_usdt = ACCOUNT_BALANCE_USDT * BASE_RISK_PCT * mult
    raw_notional = risk_usdt / effective_sl
    capped = round(min(raw_notional, cap), 4)
    floor_applied = effective_sl > sl_pct
    tier = "MAJOR" if is_major else "ALT"
    print(f"[SIZING CALC] {symbol} tier={tier} conf={confidence} sl_pct={sl_pct:.4%} eff_sl={effective_sl:.4%} risk_usdt={risk_usdt:.2f} raw={raw_notional:.2f} capped={capped}")
    return capped

def _get_portfolio_risk():
    try:
        import json as _jj, os as _oo
        _p = _oo.path.join(_oo.path.dirname(__file__), "journal.json")
        _d = _jj.load(open(_p))
        total = 0.0
        for t in _d:
            if t.get("status") != "OPEN":
                continue
            _n = float(t.get("qty", 0) or 0) * float(t.get("entry_price", 0) or 0)
            _sl = float(t.get("sl", 0) or 0)
            _ep = float(t.get("entry_price", 0) or 0)
            if _ep > 0 and _sl > 0:
                _sl_pct = abs(_ep - _sl) / _ep
                _eff_sl = max(_sl_pct, MIN_SL_PCT)
            else:
                _eff_sl = MIN_SL_PCT
            total += _n * _eff_sl
        return round(total, 4)
    except Exception:
        return 0.0

def _get_open_symbols():
    try:
        import json as _jj, os as _oo
        _p = _oo.path.join(_oo.path.dirname(__file__), "journal.json")
        _d = _jj.load(open(_p))
        return {t.get("symbol") for t in _d if t.get("status") == "OPEN"}
    except Exception:
        return set()

def _dd_aest_day_bounds():
    ts = int(time.time())
    aest_ts = ts + 10*3600
    dt = datetime.utcfromtimestamp(aest_ts)
    start = datetime(dt.year, dt.month, dt.day)
    start_utc = int(start.timestamp()) - 10*3600
    return start_utc, start_utc + 86400

def _dd_daily_pnl():
    try:
        import os as _o
        _path = _o.path.join(_o.path.dirname(__file__), "journal.json")
        with open(_path) as f:
            data = _dd_json.load(f)
    except Exception:
        return 0.0
    start, end = _dd_aest_day_bounds()
    pnl = 0.0
    for t in data:
        ts_close = t.get("timestamp_close")
        if not ts_close:
            continue
        if start <= int(ts_close) < end:
            pnl += float(t.get("pnl_usdt") or 0.0)
    return pnl
from trade_db import init_db, count_open_trades
from agent import run_analysis_telegram, run_signal_only, run_signal_raw, run_watch_only, get_market_context

import os
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing from environment/.env")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    keyboard = ReplyKeyboardMarkup(
        [["📊 Dashboard", "📡 Scan"], ["💰 Trades", "📰 News"]],
        resize_keyboard=True, is_persistent=True
    )
    await context.bot.send_message(chat_id=chat_id, text="🤖 Ready.", reply_markup=keyboard)
    await send_main_menu(context.bot, chat_id)

async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/analyse BTCUSDT`", parse_mode="Markdown")
        return
    symbol = context.args[0].upper()
    try:
        signal_msg, no_trade_msg, _sig_dict = run_signal_only(symbol) if MODE != "RAW" else (run_signal_raw(symbol))
        if no_trade_msg and not signal_msg:
            _r = no_trade_msg.split("REASON:")[-1].strip() if "REASON:" in no_trade_msg else "unknown"
            dbg_reasons[_r] = dbg_reasons.get(_r, 0) + 1
            dbg_blocked.append((symbol, _r))
        if signal_msg:
            await update.message.reply_text(signal_msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(no_trade_msg or f"COIN: {symbol}\nSTATUS: NO TRADE", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    txt = update.message.text.strip()
    print(f"BUTTON CLICKED: {txt!r} from chat={chat_id}")
    # Register chat_id for auto-scanner
    if chat_id not in ALLOWED_CHAT_IDS:
        ALLOWED_CHAT_IDS.append(chat_id)
        print(f"[CHAT REGISTERED] chat_id={chat_id}")
    from menu import refresh_menu, send_main_menu, _menu_message_ids
    from telegram import ReplyKeyboardMarkup as _RKM

    _kb = _RKM([["📊 Dashboard", "📡 Scan"], ["💰 Trades", "📰 News"]], resize_keyboard=True, is_persistent=True)

    if "Scan" in txt:
        _pmsg = await update.message.reply_text("🔄 Scanning market...", reply_markup=_kb)
        asyncio.create_task(scan_market(context, chat_id, _pmsg.message_id))
        return
    elif "Trades" in txt:
        from trades_view import format_active_trades
        trades_text = await format_active_trades()
        await update.message.reply_text(trades_text or "No active trades.", parse_mode="Markdown", reply_markup=_kb)
        return
    elif "News" in txt:
        from agent import get_market_context, _get_btc_regime
        btc = _get_btc_regime()
        ctx = get_market_context()
        await update.message.reply_text(f"📰 *Market*\nBTC: *{btc}*\n{ctx}", parse_mode="Markdown", reply_markup=_kb)
        return
    elif txt.upper().endswith("USDT"):
        await handle_symbol(update, txt.upper())
        return
    else:
        print(f"DASHBOARD CLICKED by {chat_id} txt={txt!r}")
        await send_main_menu(context.bot, chat_id)
        return
async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_market_context())

import asyncio
import time
from datetime import datetime, timezone

# ── Global scanner state ──────────────────────────────────────────
SCANNER_RUNNING = False
ALLOWED_CHAT_IDS = []  # populated on first message
LAST_SIGNALS    = {}      # anti-spam: stores signal keys already sent {key: timestamp}
MODE            = "SOFT"  

# Trading mode persistence (PROD / MEDIUM)
import os as _os2
_MODE_FILE = _os2.path.join(_os2.path.dirname(__file__), "mode.txt")
TRADE_MODE = open(_MODE_FILE).read().strip() if _os2.path.exists(_MODE_FILE) else "PROD"

loop_task       = None      # auto scan task handle
ACTIVE_SIGNALS  = {}         # signal memory cache
SIGNAL_TTL      = 1800       # 30 minutes  # STRICT or SOFT


def seconds_until_next_candle(interval_minutes=15, offset_seconds=10):
    """
    Returns seconds until next candle close for given interval + offset.
    Supports 1, 3, 5, 15, 30, 60 minute intervals.
    """
    now    = datetime.now(timezone.utc)
    minute = now.minute
    second = now.second
    next_mark = ((minute // interval_minutes) + 1) * interval_minutes
    if next_mark >= 60:
        wait = (60 - minute - 1) * 60 + (60 - second)
    else:
        wait = (next_mark - minute - 1) * 60 + (60 - second)
    return max(1, wait + offset_seconds)


async def scan_market(context, chat_id, progress_msg_id=None):
    global _MARKET_REGIME, _MARKET_REGIME_ATR, _REGIME_PREV, _CHAOTIC_PAUSE_UNTIL, _CHAOTIC_LOSS_TIMES, _DD_COOLDOWN_UNTIL, _DD_DAY_STOP
    """
    Two-stage scan:
    Stage 1: fetch 15M only, skip if no sweep (fast filter)
    Stage 2: run full run_signal_only only on sweep candidates
    """
    import datetime as _dt
    print(f"[SCAN START] {_dt.datetime.now().strftime('%H:%M:%S')}")
    _breadth_active = 0  # symbols with ATR ratio > 1.2 or strong volume
    _market_mode = "NEUTRAL"  # default until breadth computed
    # ── Regime detection (BTC 4H, every scan cycle with hysteresis) ──
    try:
        from agent import get_kline as _gk2, detect_market_regime as _dmr
        _btc4h_df = _gk2("BTCUSDT", "240")
        _new_regime, _new_atr_ratio = _dmr(_btc4h_df, "BTCUSDT")
        if _new_regime == _REGIME_PREV:
            _MARKET_REGIME = _new_regime
            _MARKET_REGIME_ATR = _new_atr_ratio
        else:
            print(f"[REGIME] Pending {_MARKET_REGIME} → {_new_regime} (need 2 cycles)")
        _REGIME_PREV = _new_regime
        print(f"[REGIME] Active={_MARKET_REGIME} atr_ratio={_MARKET_REGIME_ATR:.2f}")
    except Exception as _re:
        print(f"[REGIME ERROR] {_re}")

    global MODE
    import agent as _a
    _a.clear_kline_cache()  # reset per-scan cache


    _pending_signals = []  # collect signals for ranking — defined early for all paths
    try:
        import requests
        resp = requests.get(
            "https://api.bytick.com/v5/market/tickers",
            params={"category": "linear"}, timeout=20
        )
        tickers = resp.json().get("result", {}).get("list", [])
        filtered = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"): continue
            try:
                vol = float(t.get("turnover24h", 0))
            except Exception:
                continue
            if vol >= 10_000_000:
                filtered.append((sym, vol))
        filtered.sort(key=lambda x: x[1], reverse=True)
        _BLACKLIST = {"XAUTUSDT","TRUMPUSDT","DASHUSDT","ONGUSDT","ONDOUSDT","NEARUSDT","CLUSDT","XAGUSDT","INJUSDT","LABUSDT","SWARMSUSDT"}
        # Remove API-restricted / excluded symbols BEFORE taking top 60,
        # otherwise blacklist symbols occupy top-60 slots and reduce scan universe.
        symbols = [s[0] for s in sorted([s for s in filtered if s[0] not in _BLACKLIST], key=lambda x: x[1], reverse=True)[:60]]

        total   = len(symbols)
    except Exception as e:
        if progress_msg_id:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id,
                text=f"❌ Failed to fetch symbols: {e}"
            )
        return []

    signals_found = []
    dbg_sweep = 0; dbg_reclaim = 0; dbg_volume = 0
    dbg_reasons   = {}
    dbg_blocked    = []          # symbol → reason list
    start_time = time.time()
    _a.SIGNAL_MODE = MODE

    # ── STAGE 1: fast filter — 15M sweep check only ───────────────
    sweep_candidates = []
    for idx, symbol in enumerate(symbols, 1):
        if progress_msg_id and idx % 8 == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=progress_msg_id,
                    text=f"⏳ Stage 1: {idx} / {total}"
                )
            except Exception:
                pass
        try:
            df = _a.get_kline(symbol, "15")
            if df is None or len(df) < 25:
                await asyncio.sleep(0.03)
                continue
            ev = _a._eval_sweep(df)
            if ev.get("direction"):
                dbg_sweep += 1
                if ev.get("reclaim_strength", 0) >= 0.01: dbg_reclaim += 1
                if ev.get("vol_ratio", 0) >= 1.0: dbg_volume += 1
                sweep_candidates.append(symbol)
        except Exception:
            pass
        await asyncio.sleep(0.03)

    # ── STAGE 2: full analysis only on sweep candidates ───────────
    if sweep_candidates and progress_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id,
                text=f"⏳ Stage 2: checking {len(sweep_candidates)} candidates..."
            )
        except Exception:
            pass

    for symbol in sweep_candidates:
        try:
            signal_msg, no_trade_msg, _sig_dict = run_signal_only(symbol) if MODE != "RAW" else (list(run_signal_raw(symbol)) + [None])
            if _sig_dict:
                if not _sig_dict.get("path"): _sig_dict["path"] = str(_sig_dict.get("_path","")).replace("B/C","B") or "sweep"
                if not _sig_dict.get("signal_score"): _sig_dict["signal_score"] = float(_sig_dict.get("r_pct",0) or 0)
                _sig_dict["macro_state"] = str(_sig_dict.get("trend4h", _sig_dict.get("t4h","UNKNOWN"))).upper()
                _sig_dict["market_regime"] = _MARKET_REGIME
            if no_trade_msg and not signal_msg:
                _r = no_trade_msg.split("REASON:")[-1].strip() if "REASON:" in no_trade_msg else "unknown"
                dbg_reasons[_r] = dbg_reasons.get(_r, 0) + 1
                dbg_blocked.append((symbol, _r))
                _log_blocked_trade(symbol, _sig_dict.get("direction","") if _sig_dict else "", _sig_dict.get("confidence","") if _sig_dict else "", _sig_dict.get("path","") if _sig_dict else "", f"no_setup:{_r}", sig_dict=_sig_dict)
            if signal_msg:
                key = signal_msg[:60]
                if key not in LAST_SIGNALS or time.time() - LAST_SIGNALS.get(key,0) > SIGNAL_TTL:
                    LAST_SIGNALS[key] = time.time()
                    signals_found.append(signal_msg)
                # Save to signal memory
                ACTIVE_SIGNALS[symbol] = {
                    "text":      signal_msg,
                    "timestamp": time.time(),
                    "status":    "ACTIVE"
                }
                # Store sweep level in cache for 5M reclaim monitoring
                try:
                    from agent import _eval_sweep_level
                    _lv = _eval_sweep_level(df15m) if 'df15m' in dir() else None
                    if _lv:
                        level_cache.store_level(symbol, _lv["level"], _lv["direction"], _lv["atr"])
                except Exception:
                    pass

                # Route to executor using structured dict (preferred) or regex fallback
                if signal_msg:  # route to executor
                    if _sig_dict:
                        # ── APP-LEVEL MACRO FILTER ────────────────
                        _mf_dir  = str(_sig_dict.get("direction","")).upper()
                        _mf_conf = str(_sig_dict.get("confidence","")).upper()
                        _mf_t4h  = str(_sig_dict.get("trend4h", _sig_dict.get("t4h","NEUTRAL"))).upper()
                        _mf_t1h  = str(_sig_dict.get("trend1h", _sig_dict.get("t1h","NEUTRAL"))).upper()
                        _mf_block = False; _mf_reason = ""
                        # ── NEW MACRO FILTER LOGIC (v2) ──────────
                        # BEARISH/BULLISH = genuine directional opposition → hard block
                        # NEUTRAL = uncertain → HIGH only at 75% size, MEDIUM blocked
                        # NEUTRAL + CHAOTIC regime → hard block
                        if _mf_dir == "LONG" and _mf_t4h == "BEARISH":
                            if _mf_conf == "HIGH":
                                _sig_dict["confidence"] = "MEDIUM"
                                _sig_dict["_macro_downgrade"] = True
                                _sig_dict["_macro_conflict"] = "4H_opposite"
                                _mf_conf = "MEDIUM"
                                print(f"[MACRO DOWNGRADE] {_sig_dict.get('symbol','')} LONG 4H=BEARISH HIGH→MEDIUM")
                            elif _mf_conf == "MEDIUM":
                                _sig_dict["_macro_downgrade"] = True
                                _sig_dict["_macro_conflict"] = "4H_opposite"
                                print(f"[MACRO DOWNGRADE] {_sig_dict.get('symbol','')} LONG 4H=BEARISH MEDIUM allowed")
                        elif _mf_dir == "SHORT" and _mf_t4h == "BULLISH":
                            if _mf_conf == "HIGH":
                                _sig_dict["confidence"] = "MEDIUM"
                                _sig_dict["_macro_downgrade"] = True
                                _sig_dict["_macro_conflict"] = "4H_opposite"
                                _mf_conf = "MEDIUM"
                                print(f"[MACRO DOWNGRADE] {_sig_dict.get('symbol','')} SHORT 4H=BULLISH HIGH→MEDIUM")
                            elif _mf_conf == "MEDIUM":
                                _sig_dict["_macro_downgrade"] = True
                                _sig_dict["_macro_conflict"] = "4H_opposite"
                                print(f"[MACRO DOWNGRADE] {_sig_dict.get('symbol','')} SHORT 4H=BULLISH MEDIUM allowed")
                        elif _mf_t4h == "NEUTRAL" and _mf_conf in ("MEDIUM", "LOW"):
                            _mf_block = True; _mf_reason = f"{_mf_conf} blocked 4H=NEUTRAL"
                        elif _mf_t4h == "NEUTRAL" and globals().get("_MARKET_REGIME","") == "CHAOTIC":
                            _mf_block = True; _mf_reason = f"blocked 4H=NEUTRAL + CHAOTIC regime"
                        elif _mf_t4h == "NEUTRAL" and _mf_conf == "HIGH":
                            # HIGH in NEUTRAL: pass at 75% size (applied in sizing engine)
                            _sig_dict["_macro_neutral"] = True
                            print(f"[MACRO FILTER] {symbol} NEUTRAL — HIGH allowed at 75% size")
                        # 1H momentum gate (keep — separate from macro)
                        if not _mf_block:
                            if _mf_dir == "LONG" and _mf_t1h == "BEARISH":
                                _mf_block = True; _mf_reason = f"LONG blocked 1H=BEARISH"
                                print(f"[FILTER BLOCK] 1H bearish vs LONG {symbol}")
                            elif _mf_dir == "SHORT" and _mf_t1h == "BULLISH":
                                _mf_block = True; _mf_reason = f"SHORT blocked 1H=BULLISH"
                                print(f"[FILTER BLOCK] 1H bullish vs SHORT {symbol}")
                        if _mf_block:
                            print(f"[MACRO FILTER] {symbol} BLOCKED — {_mf_reason}")
                            signal_msg = None  # suppress Telegram too
                            _log_blocked_trade(symbol, _mf_dir, _mf_conf, "A", f"MACRO:{_mf_reason}", sig_dict=_sig_dict)
                        else:
                            print(f"[MACRO FILTER] {symbol} ALLOWED — {_mf_dir} {_mf_conf} 4H={_mf_t4h}")
                        # ─────────────────────────────────────────
                        if not _mf_block:
                            print(f"ROUTING: {symbol} sig_dict={_sig_dict is not None} conf={_sig_dict.get('confidence') if _sig_dict else None}")
                            # ── Signal quality gates ─────────────────────────
                            _gp = float(_sig_dict.get("price",0) or 0)
                            _gs = float(_sig_dict.get("sl",0) or 0)
                            _gt = float(_sig_dict.get("tp1",0) or 0)
                            if _gp > 0 and _gs > 0:
                                _gsl_pct = abs(_gp - _gs) / _gp
                                if _gsl_pct < MIN_SL_PCT:
                                    print(f"[INVALID_SL] {symbol} sl_pct={_gsl_pct:.4%} < MIN_SL_PCT — BLOCKED")
                                    _log_blocked_trade(symbol, _sig_dict.get("direction",""), _sig_dict.get("confidence",""), _sig_dict.get("_path",""), f"INVALID_SL:sl_pct {_gsl_pct:.4%}", regime=_MARKET_REGIME, sig_dict=_sig_dict)
                                    continue
                                if _gt > 0:
                                    _grr = abs(_gt - _gp) / abs(_gp - _gs)
                                    if _grr < 1.2:
                                        print(f"[INVALID_TP1_R] {symbol} rr={_grr:.3f} < 1.2 — BLOCKED")
                                        _log_blocked_trade(symbol, _sig_dict.get("direction",""), _sig_dict.get("confidence",""), _sig_dict.get("_path",""), f"INVALID_TP1_R:rr={_grr:.3f}", regime=_MARKET_REGIME, sig_dict=_sig_dict)
                                        continue
                                    # Fee-dominated check (execution-accurate)
                                    _qty_est = float(_sig_dict.get("qty",0) or 0)
                                    if _qty_est <= 0:
                                        _qty_est = float(_sig_dict.get("notional",75) or 75) / max(_gp, 0.000001)
                                    _tp1_profit_est = abs(_gt - _gp) * _qty_est
                                    _fee_est = (_gp * _qty_est) * 0.00055 * 2
                                    _fee_ratio = _tp1_profit_est / max(_fee_est, 0.000001)
                                    if _tp1_profit_est < _fee_est * 2.0:
                                        print(f"[FEE BLOCK] {symbol} profit={_tp1_profit_est:.4f} fee={_fee_est:.4f} ratio={_fee_ratio:.2f} — BLOCKED")
                                        _log_blocked_trade(symbol, _sig_dict.get("direction",""), _sig_dict.get("confidence",""), _sig_dict.get("_path",""), f"FEE_DOMINATED:ratio={_fee_ratio:.2f}", regime=_MARKET_REGIME, sig_dict=_sig_dict)
                                        continue
                            _pending_signals.append((_sig_dict, time.time()))
                    else:
                        try:
                            import re as _re
                            _conf = "HIGH" if "CORE SETUP" in signal_msg else "MEDIUM" if "WATCH ENTRY" in signal_msg else "LOW"
                            _dir  = "LONG" if "LONG" in signal_msg else "SHORT"
                            _ep   = float(_re.search(r"ENTRY:\s+`([^`]+)`", signal_msg).group(1))
                            _sl   = float(_re.search(r"SL:\s+`([^`]+)`", signal_msg).group(1))
                            _tp1  = float(_re.search(r"TP1:\s+`([^`]+)`", signal_msg).group(1))
                            _tp2  = float(_re.search(r"TP2:\s+`([^`]+)`", signal_msg).group(1))
                            _tp3  = float(_re.search(r"TP3:\s+`([^`]+)`", signal_msg).group(1))
                            _sig  = {"symbol": symbol, "direction": _dir, "confidence": _conf,
                                     "setup_class": "CORE" if _conf=="HIGH" else _conf,
                                     "price": _ep, "sl": _sl, "tp1": _tp1, "tp2": _tp2, "tp3": _tp3}
                            _pending_signals.append((_sig, time.time()))
                        except Exception as _ex:
                            print(f"Executor routing fallback error: {_ex}")
                # Stage 3: watch layer (only if no signal)
                watch_msg, _ = run_watch_only(symbol)
                if watch_msg:
                    signals_found.append(watch_msg)
        except Exception as _scan_ex:
            print(f"SCAN LOOP ERROR {symbol}: {_scan_ex}")
            import traceback as _tb2; _tb2.print_exc()
            pass
        await asyncio.sleep(0.05)


    # ── Rank and execute top signals ─────────────────────────────
    try:
        def _sig_score(s):
            d = s[0]
            conf_score = 2 if d.get("confidence","").upper() == "HIGH" else 1
            r = float(d.get("r_pct", 0))
            return (conf_score, r)
        _pending_signals.sort(key=_sig_score, reverse=True)
        # Signals will be executed sequentially after all paths collected
        # (execution happens at end of scan_market)
    except Exception as _re:
        print(f"RANKING ERROR: {_re}")

    # ── STAGE 3: Path B + C on non-sweep symbols ────────────────
    non_sweep = [s for s in symbols if s not in sweep_candidates]
    for symbol in non_sweep:
        try:
            from agent import _eval_consolidation_breakout, _eval_continuation, get_kline as _gk, _TRADE_MODE
            df = _gk(symbol, "15")
            if df is None or len(df) < 25:
                continue
            # Path B: breakout
            cb = _eval_consolidation_breakout(df, _TRADE_MODE)
            # Path C: continuation (MEDIUM+ only for execution)
            cc = _eval_continuation(df, _TRADE_MODE)
            # Pick best result
            _best = None
            if cb.get("direction"): _best = ("BREAKOUT", cb)
            elif cc.get("direction"): _best = ("CONT", cc)
            if not _best:
                continue
            _path, _result = _best
            _conf = _result.get("confidence", "LOW")
            _dir  = _result.get("direction")
            # Always log to bot.log
            import logging as _lg
            _lg.getLogger("app").info(
                "Stage3 %s %s %s conf=%s reason=%s",
                _path, symbol, _dir, _conf, _result.get("reason")
            )
            # Only surface MEDIUM+ to Telegram + executor
            if _conf == "LOW":
                continue
            # Build minimal signal via run_signal_only
            signal_msg, no_trade_msg, _sig_dict = run_signal_only(symbol)
            if signal_msg:
                key = signal_msg[:60]
                if key not in LAST_SIGNALS or time.time() - LAST_SIGNALS.get(key,0) > SIGNAL_TTL:
                    LAST_SIGNALS[key] = time.time()
                    signals_found.append(signal_msg)
                    ACTIVE_SIGNALS[symbol] = {"text": signal_msg, "timestamp": time.time(), "status": "ACTIVE"}
                    # Route to executor — add to unified pipeline
                    if _sig_dict:
                        _sig_dict["_path"] = "B/C"
                        _bc_dir  = str(_sig_dict.get("direction","")).upper()
                        _bc_conf = str(_sig_dict.get("confidence","")).upper()
                        _bc_t4h  = str(_sig_dict.get("trend4h", _sig_dict.get("t4h","NEUTRAL"))).upper()
                        _bc_t1h  = str(_sig_dict.get("trend1h", _sig_dict.get("t1h","NEUTRAL"))).upper()
                        print(f"[FILTER CHECK] {symbol} dir={_bc_dir} conf={_bc_conf} t4h={_bc_t4h} path=B/C")
                        _bc_block = False
                        if _bc_dir == "LONG" and _bc_t4h == "BEARISH":
                            if _bc_conf == "HIGH":
                                _sig_dict["confidence"] = "MEDIUM"; _bc_conf = "MEDIUM"
                                _sig_dict["_macro_downgrade"] = True; _sig_dict["_macro_conflict"] = "4H_opposite"
                                print(f"[MACRO DOWNGRADE] {symbol} LONG 4H=BEARISH HIGH→MEDIUM (B/C)")
                            else:
                                _sig_dict["_macro_downgrade"] = True; _sig_dict["_macro_conflict"] = "4H_opposite"
                                print(f"[MACRO DOWNGRADE] {symbol} LONG 4H=BEARISH MEDIUM allowed (B/C)")
                        elif _bc_dir == "SHORT" and _bc_t4h == "BULLISH":
                            if _bc_conf == "HIGH":
                                _sig_dict["confidence"] = "MEDIUM"; _bc_conf = "MEDIUM"
                                _sig_dict["_macro_downgrade"] = True; _sig_dict["_macro_conflict"] = "4H_opposite"
                                print(f"[MACRO DOWNGRADE] {symbol} SHORT 4H=BULLISH HIGH→MEDIUM (B/C)")
                            else:
                                _sig_dict["_macro_downgrade"] = True; _sig_dict["_macro_conflict"] = "4H_opposite"
                                print(f"[MACRO DOWNGRADE] {symbol} SHORT 4H=BULLISH MEDIUM allowed (B/C)")
                        elif _bc_t4h == "NEUTRAL":
                            _sig_dict["_macro_context"] = "4H_neutral"
                            print(f"[MACRO CONTEXT] {symbol} 4H=NEUTRAL — allowed with tag (B/C)")
                        elif _bc_dir == "LONG" and _bc_t1h == "BEARISH":
                            _bc_block = True; print(f"[FILTER BLOCK] 1H bearish vs LONG {symbol}")
                        elif _bc_dir == "SHORT" and _bc_t1h == "BULLISH":
                            _bc_block = True; print(f"[FILTER BLOCK] 1H bullish vs SHORT {symbol}")
                        if not _bc_block:
                            if _sig_dict.get("sl", 0) == 0:
                                print(f"[SL GUARD] {symbol} BLOCKED — sl=0 (B/C)")
                            else:
                                print(f"[FILTER PASSED] {symbol} {_bc_dir} {_bc_conf} 4H={_bc_t4h}")
                                _pending_signals.append((_sig_dict, time.time()))
            elif no_trade_msg:
                _r = no_trade_msg.split("REASON:")[-1].strip() if "REASON:" in no_trade_msg else "unknown"
                dbg_reasons[_r] = dbg_reasons.get(_r, 0) + 1
                dbg_blocked.append((symbol, _r))
        except Exception as _s3e:
            pass
        await asyncio.sleep(0.03)

    # ── STAGE 4: Path D momentum scan (all symbols) ──────────────
    _momentum_candidates = []
    for symbol in symbols:
        try:
            from agent import _eval_momentum, get_kline as _gk4, _TRADE_MODE
            df4 = _gk4(symbol, "15")
            if df4 is None or len(df4) < 20:
                continue
            md = _eval_momentum(df4, _TRADE_MODE, symbol)
            if md.get("direction"):
                _momentum_candidates.append((symbol, md))
        except Exception:
            pass
        await asyncio.sleep(0.02)

    # Pick best momentum signal (highest body ratio)
    if _momentum_candidates:
        _momentum_candidates.sort(key=lambda x: x[1].get("body_ratio", 0), reverse=True)
        _best_sym, _best_md = _momentum_candidates[0]
        # Build signal via run_signal_only to get formatted message
        _ms, _mn, _md2 = run_signal_only(_best_sym)
        if not _ms:
            # Build minimal momentum signal
            _dir  = _best_md["direction"]
            _ep   = _best_md["entry"]
            _sl   = _best_md["sl_raw"]
            _atr  = _best_md["atr"]
            _risk = abs(_ep - _sl)
            _tp1  = round(_ep + 1.2*_risk if _dir=="LONG" else _ep - 1.2*_risk, 6)
            _tp2  = round(_ep + 2.0*_risk if _dir=="LONG" else _ep - 2.0*_risk, 6)
            _tp3  = round(_ep + 3.0*_risk if _dir=="LONG" else _ep - 3.0*_risk, 6)
            _side = "🟢" if _dir=="LONG" else "🔴"
            _rpct = round(_risk / _ep * 100, 3) if _ep > 0 else 0
            _ms = (
                f"────────────────────────────\n"
                f"⚡ MOMENTUM\n"
                f"COIN: *{_best_sym}*  |  {_side} *{_dir}*  |  PATH D\n"
                f"CONFIDENCE: HIGH\n"
                f"────────────────────────────\n"
                f"ENTRY:  `{round(_ep,6)}`\n"
                f"SL:     `{round(_sl,6)}`\n"
                f"────────────────────────────\n"
                f"TP1:  `{_tp1}`  →  1.5R  _(→ move SL to breakeven)_\n"
                f"TP2:  `{_tp2}`  →  2.5R\n"
                f"TP3:  `{_tp3}`  →  3.5R\n"
                f"────────────────────────────\n"
                f"R: momentum breakout | R={_rpct}%\n"
                f"────────────────────────────"
            )
            _md2 = {
                "symbol": _best_sym, "direction": _dir,
                "confidence": "HIGH", "setup_class": "CORE",
                "price": _ep, "sl": _sl,
                "tp1": _tp1, "tp2": _tp2, "tp3": _tp3,
            }
        key = _ms[:60]
        if key not in LAST_SIGNALS or time.time() - LAST_SIGNALS.get(key,0) > SIGNAL_TTL:
            LAST_SIGNALS[key] = time.time()
            ACTIVE_SIGNALS[_best_sym] = {"text": _ms, "timestamp": time.time(), "status": "ACTIVE"}
            # Execute in MEDIUM or HIGH mode
            from executor import get_auto_trade_mode as _gatm4
            _atm4 = _gatm4()
            if _atm4 in ("MEDIUM", "HIGH", "PRO") and _md2:
                print(f"[ROUTING PATH-D] {_best_sym} conf=HIGH mode={_atm4}")
                _md2["_path"] = "D"
                _d_dir  = str(_md2.get("direction","")).upper()
                _d_conf = str(_md2.get("confidence","")).upper()
                _d_t4h  = str(_md2.get("trend4h", _md2.get("t4h","NEUTRAL"))).upper()
                _d_t1h  = str(_md2.get("trend1h", _md2.get("t1h","NEUTRAL"))).upper()
                print(f"[FILTER CHECK] {_best_sym} dir={_d_dir} conf={_d_conf} t4h={_d_t4h} path=D")
                _d_block = False
                if _d_dir == "LONG" and _d_t4h == "BEARISH":
                    if _d_conf == "HIGH":
                        _md2["confidence"] = "MEDIUM"; _d_conf = "MEDIUM"
                        _md2["_macro_downgrade"] = True; _md2["_macro_conflict"] = "4H_opposite"
                        print(f"[MACRO DOWNGRADE] {_best_sym} LONG 4H=BEARISH HIGH→MEDIUM (D)")
                    else:
                        _md2["_macro_downgrade"] = True; _md2["_macro_conflict"] = "4H_opposite"
                        print(f"[MACRO DOWNGRADE] {_best_sym} LONG 4H=BEARISH MEDIUM allowed (D)")
                elif _d_dir == "SHORT" and _d_t4h == "BULLISH":
                    if _d_conf == "HIGH":
                        _md2["confidence"] = "MEDIUM"; _d_conf = "MEDIUM"
                        _md2["_macro_downgrade"] = True; _md2["_macro_conflict"] = "4H_opposite"
                        print(f"[MACRO DOWNGRADE] {_best_sym} SHORT 4H=BULLISH HIGH→MEDIUM (D)")
                    else:
                        _md2["_macro_downgrade"] = True; _md2["_macro_conflict"] = "4H_opposite"
                        print(f"[MACRO DOWNGRADE] {_best_sym} SHORT 4H=BULLISH MEDIUM allowed (D)")
                elif _d_t4h == "NEUTRAL":
                    _md2["_macro_context"] = "4H_neutral"
                    print(f"[MACRO CONTEXT] {_best_sym} 4H=NEUTRAL — allowed with tag (D)")
                elif _d_dir == "LONG" and _d_t1h == "BEARISH":
                    _d_block = True; print(f"[FILTER BLOCK] 1H bearish vs LONG {_best_sym}")
                elif _d_dir == "SHORT" and _d_t1h == "BULLISH":
                    _d_block = True; print(f"[FILTER BLOCK] 1H bullish vs SHORT {_best_sym}")
                # LONG MEDIUM block removed — macro filter handles direction via downgrade
                if not _d_block:
                    if _md2.get("sl", 0) == 0:
                        print(f"[SL GUARD] {_best_sym} BLOCKED — sl=0 (Path D)")
                    else:
                        print(f"[FILTER PASSED] {_best_sym} {_d_dir} {_d_conf} 4H={_d_t4h}")
                        signals_found.append(_ms)
                        _pending_signals.append((_md2, time.time()))
            else:
                signals_found.append(_ms)

    elapsed = round(time.time() - start_time, 1)

    # ── SYMBOL-LEVEL CANDIDATE RESOLUTION ───────────────────────────
    print(f"[RESOLVE INPUT] {len(_pending_signals)} total candidates: {[(s[0].get('symbol'),s[0].get('_path','sweep'),s[0].get('confidence')) for s in _pending_signals]}")
    # Group all candidates by symbol, pick best per symbol, then execute
    def _score_candidate(sig):
        d = sig[0]
        _conf = str(d.get("confidence","")).upper()
        c = {"HIGH": 100, "MEDIUM": 10, "LOW": 1}.get(_conf, 1)
        r = float(d.get("r_pct", 0))
        pb = 0.5 if d.get("_path") == "D" else 0
        return c + pb + r

    # Group by symbol
    _by_symbol = {}
    for _sd, _ts in _pending_signals:
        _sym = _sd.get("symbol")
        if _sym not in _by_symbol:
            _by_symbol[_sym] = []
        _by_symbol[_sym].append((_sd, _ts))

    # Resolve winner per symbol
    _resolved = []
    for _sym, _candidates in _by_symbol.items():
        if len(_candidates) > 1:
            print(f"[SYMBOL RESOLVE] {_sym} — {len(_candidates)} candidates:")
            for _sd, _ts in _candidates:
                _c = str(_sd.get("confidence","")).upper()
                _pb = 0.5 if _sd.get("_path") == "D" else 0
                _sc = ({"HIGH":100,"MEDIUM":10,"LOW":1}.get(_c,1)) + _pb + float(_sd.get("r_pct",0))
                print(f"  path={_sd.get('_path','sweep')} side={_sd.get('direction')} conf={_c} score={_sc:.3f}")
            _winner = max(_candidates, key=_score_candidate)
            _wd = _winner[0]
            print(f"  winner: path={_wd.get('_path','sweep')} {_wd.get('direction')} {_wd.get('confidence','').upper()}")
            _resolved.append(_winner)
        else:
            _resolved.append(_candidates[0])

    # Sort resolved signals by score
    _resolved.sort(key=_score_candidate, reverse=True)

    # Safety check — no duplicates should reach executor
    _exec_syms = set()
    print(f"[EXEC QUEUE] {len(_resolved)} signals:")
    for _sd, _ts in _resolved:
        _c = str(_sd.get("confidence","")).upper()
        _sc = _score_candidate((_sd, _ts))
        _sym = _sd.get("symbol")
        print(f"  [EXEC QUEUE] {_sym} path={_sd.get('_path','sweep')} {_sd.get('direction')} {_c} score={_sc:.3f}")
        if _sym in _exec_syms:
            print(f"  [DUPLICATE ERROR] {_sym} multiple signals passed resolve — BLOCKED")
            continue
        _exec_syms.add(_sym)
        try:
            import agent as _ag_mode_exec
            _filter_mode_exec = getattr(_ag_mode_exec, "_TRADE_MODE", "PROD")
        except Exception:
            _filter_mode_exec = globals().get("TRADE_MODE", "PROD")
        try:
            _execution_mode_exec = get_auto_trade_mode()
        except Exception:
            _execution_mode_exec = "UNKNOWN"
        _user_mode_exec = "OFF" if _execution_mode_exec == "OFF" else ("MEDIUM" if _filter_mode_exec == "MEDIUM" else "PRO")

        _sd["user_mode"] = _user_mode_exec
        _sd["filter_mode"] = _filter_mode_exec
        _sd["execution_mode"] = _execution_mode_exec

        async def _run_signal(_sd=_sd, _ts=_ts):
            try:
                await handle_signal(_sd, _ts)
            except Exception as _e:
                print(f"[SIGNAL ERROR] {_sd.get('symbol')} — {_e}")
                import traceback; traceback.print_exc()
        # ====== RISK ENGINE ======
        _open_syms = _get_open_symbols()

        # Max positions check
        if len(_open_syms) >= MAX_OPEN_POSITIONS:
            print(f"[RISK] MAX_OPEN_POSITIONS {MAX_OPEN_POSITIONS} reached — blocking {_sym}")
            continue

        # Correlation filter
        _blocked_corr = False
        for _cluster_name, _cluster_syms in CORR_CLUSTERS.items():
            if _sym in _cluster_syms:
                _overlap = _open_syms & set(_cluster_syms)
                if _overlap:
                    print(f"[RISK] CORR BLOCK {_sym} — cluster={_cluster_name} open={_overlap}")
                    _blocked_corr = True
                    break
        if _blocked_corr:
            continue

        # Dynamic sizing
        _conf = str(_sd.get("confidence", "")).upper()
        _entry = float(_sd.get("price", 0) or 0)
        _sl_price = float(_sd.get("sl", 0) or 0)
        _notional = _get_signal_notional(_conf, _entry, _sl_price, symbol=_sym)
        if _notional <= 0:
            print(f"[RISK] BLOCK {_sym} — invalid sizing (conf={_conf} entry={_entry} sl={_sl_price})")
            continue
        # Portfolio risk check
        _port_risk = _get_portfolio_risk()
        _sl_pct = abs(_entry - _sl_price) / _entry if _entry > 0 else MIN_SL_PCT
        _eff_sl = max(_sl_pct, MIN_SL_PCT)
        _new_risk = _notional * _eff_sl
        _max_risk = ACCOUNT_BALANCE_USDT * MAX_CONCURRENT_RISK_PCT
        print(f"[SIZING] {_sym} conf={_conf} notional={_notional} port_risk={_port_risk:.2f} new_risk={_new_risk:.2f} max={_max_risk:.2f}")
        if _port_risk + _new_risk > _max_risk:
            print(f"[RISK] BLOCK {_sym} — portfolio risk {_port_risk+_new_risk:.2f} > max {_max_risk:.2f}")
            continue
        _sd["notional"] = _notional
        # Apply 75% size for NEUTRAL macro HIGH signals
        if _sd.get("_macro_neutral"):
            if _size_reductions < 2:
                _sd["notional"] = round(_sd.get("notional", _notional) * 0.75, 2)
                _size_reductions += 1
                print(f"[MACRO] NEUTRAL HIGH — {_sym} notional 75%: {_sd['notional']}")
            else:
                print(f"[MACRO] NEUTRAL HIGH — {_sym} skipped (max 2 reductions reached)")
        # ====== END RISK ENGINE ======

        # ====== DRAWDOWN BREAKER CHECK (TIERED DEV MODE) ======
        # Tier 1 (-$5): reduce notional 50%, HIGH only → continue trading
        # Tier 2 (-$10): reduce notional 75%, HIGH only, 2h cooldown between trades
        # Tier 3 (-$15): full stop — nuclear option only
        _DD_TIER1 = float(_dd_os.getenv("DD_TIER1_USDT", "5.0"))
        _DD_TIER2 = float(_dd_os.getenv("DD_TIER2_USDT", "10.0"))
        _DD_TIER3 = float(_dd_os.getenv("DD_TIER3_USDT", "15.0"))
        global _DD_COOLDOWN_UNTIL, _DD_DAY_STOP
        _dd_now = int(time.time())
        if _DD_DAY_STOP:
            print(f"[DD] TIER3 STOP ACTIVE — blocking {_sym}")
            _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), "DD:TIER3_full_stop", sig_dict=_sd)
            continue
        if _dd_now < _DD_COOLDOWN_UNTIL:
            _mins_left = int((_DD_COOLDOWN_UNTIL - _dd_now) / 60)
            print(f"[DD] COOLDOWN ACTIVE ({_mins_left} min left) — blocking {_sym}")
            continue
        _dd_pnl = _dd_daily_pnl()
        if _dd_pnl <= -_DD_TIER3:
            _DD_DAY_STOP = True
            print(f"[DD] TIER3 STOP TRIGGERED | pnl={_dd_pnl:.2f} — full stop")
            continue
        if _dd_pnl <= -_DD_TIER2:
            # Tier 2: HIGH only, 75% size reduction, 2h cooldown between trades
            if _sd.get("confidence","").upper() != "HIGH":
                print(f"[DD] TIER2 ({_dd_pnl:.2f}) — blocking {_sym} non-HIGH")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), f"DD:TIER2_non_HIGH pnl={_dd_pnl:.2f}", sig_dict=_sd)
                continue
            if _DD_COOLDOWN_UNTIL == 0:
                _DD_COOLDOWN_UNTIL = _dd_now + 120 * 60
                print(f"[DD] TIER2 TRIGGERED | pnl={_dd_pnl:.2f} — 2h cooldown, HIGH only, 75% size")
            _sd["notional"] = round(_sd["notional"] * 0.25, 2)
            _size_reductions += 1
            print(f"[DD] TIER2 active — {_sym} notional reduced 75% to {_sd['notional']}")
        elif _dd_pnl <= -_DD_TIER1:
            # Tier 1: HIGH only, 50% size reduction, continue trading
            if _sd.get("confidence","").upper() != "HIGH":
                print(f"[DD] TIER1 ({_dd_pnl:.2f}) — blocking {_sym} non-HIGH")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), f"DD:TIER1_non_HIGH pnl={_dd_pnl:.2f}", sig_dict=_sd)
                continue
            _sd["notional"] = round(_sd["notional"] * 0.5, 2)
            _size_reductions += 1
            print(f"[DD] TIER1 active — {_sym} notional reduced 50% to {_sd['notional']}")
        # ====== END DRAWDOWN BREAKER ======
        # ── REGIME APPLICATION ──
        _regime_now = int(time.time())
        _sig_conf = _sd.get("confidence","").upper()
        _sig_setup = _sd.get("setup_type", _sd.get("_path","")).lower()
        _is_sweep = "sweep" in _sig_setup or _sig_setup in ("a","core")
        _base_notional = _sd.get("notional", 0)
        _size_reductions = 0  # track stacked reductions
        MIN_NOTIONAL = 75.0  # floor — no trades below this

        if _MARKET_REGIME == "CHAOTIC":
            # Check 4h pause after 3 consecutive CHAOTIC losses
            if _regime_now < _CHAOTIC_PAUSE_UNTIL:
                _mins = int((_CHAOTIC_PAUSE_UNTIL - _regime_now)/60)
                print(f"[REGIME] CHAOTIC pause active ({_mins}m left) — blocking {_sym}")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), "REGIME:CHAOTIC_pause", sig_dict=_sd)
                continue
            # Sweep only, HIGH only
            if not _is_sweep:
                print(f"[REGIME] CHAOTIC — blocking {_sym}: non-sweep setup")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), "REGIME:CHAOTIC_non_sweep", sig_dict=_sd)
                continue
            if _sig_conf != "HIGH":
                print(f"[REGIME] CHAOTIC — blocking {_sym}: non-HIGH conf={_sig_conf}")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), f"REGIME:CHAOTIC_non_HIGH conf={_sig_conf}", sig_dict=_sd)
                continue
            # Count open positions
            _open_count = len([p for p in _open_positions_cache if p.get("status")=="OPEN"])
            if _open_count >= 1:
                print(f"[REGIME] CHAOTIC — max 1 position, {_open_count} open, blocking {_sym}")
                continue
            # 50% size (was 25%)
            _sd["notional"] = round(_base_notional * 0.50, 2)
            _size_reductions += 1
            print(f"[REGIME] CHAOTIC — {_sym} size 50%: {_sd['notional']}")

        elif _MARKET_REGIME == "RANGE":
            _open_count = len([p for p in _open_positions_cache if p.get("status")=="OPEN"])
            _open_sides = [p.get("side","") for p in _open_positions_cache if p.get("status")=="OPEN"]
            _sig_side = "Buy" if _sd.get("direction","") == "LONG" else "Sell"
            # Max 2 positions, not same side
            if _open_count >= 2:
                print(f"[REGIME] RANGE — max 2 positions, blocking {_sym}")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), "REGIME:RANGE_max_positions", sig_dict=_sd)
                continue
            if _open_sides.count(_sig_side) >= 1:
                print(f"[REGIME] RANGE — same side already open, blocking {_sym}")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), "REGIME:RANGE_same_side", sig_dict=_sd)
                continue
            # RANGE: no size reduction — behavior rules only (max 2 pos, no same side)
            _sd["notional"] = _base_notional
            print(f"[REGIME] RANGE — {_sym} full size, behavior rules apply")
            # Sweep TP adjustment handled in executor via regime flag
            _sd["_regime"] = "RANGE"
            _sd["_is_sweep"] = _is_sweep

        elif _MARKET_REGIME in ("TREND_UP", "TREND_DOWN"):
            # Lower score threshold handled at signal level
            # Entry extension soft penalty handled in executor
            _sd["_regime"] = _MARKET_REGIME
            print(f"[REGIME] {_MARKET_REGIME} — {_sym} full size, no restriction")

        # Tag regime and macro_state on signal for logging
        _sd["_regime"] = _sd.get("_regime", _MARKET_REGIME)
        _sd["macro_state"] = _sd.get("_macro_conflict", _sd.get("_macro_context", str(_sd.get("trend4h", _sd.get("t4h","UNKNOWN"))).upper()))
        _sd["market_regime"] = _MARKET_REGIME
        # Ensure path and signal_score are set for journal
        if not _sd.get("path"): _sd["path"] = str(_sd.get("_path","")).replace("B/C","B") or "sweep"
        if not _sd.get("signal_score"): _sd["signal_score"] = float(_sd.get("r_pct", 0) or 0)
        # ── MIN_NOTIONAL floor ────────────────────────────────────
        _final_notional = float(_sd.get("notional", 0))
        if _final_notional < MIN_NOTIONAL:
            _sl_pct = abs(float(_sd.get("price",0)) - float(_sd.get("sl",0))) / float(_sd.get("price",1)) if float(_sd.get("price",0)) > 0 else 0.01
            _rescale_risk = MIN_NOTIONAL * _sl_pct
            if _port_risk + _rescale_risk <= _max_risk:
                _sd["notional"] = MIN_NOTIONAL
                print(f"[SIZE FLOOR] {_sym} notional rescaled {_final_notional:.1f}→{MIN_NOTIONAL} within risk limits")
            else:
                print(f"[SIZE FLOOR] {_sym} notional {_final_notional:.1f} below floor but risk limit prevents rescale — skipping")
                _log_blocked_trade(_sym, _sd.get("direction",""), _sd.get("confidence",""), _sd.get("_path",""), f"SIZE:below_floor_risk_limit", sig_dict=_sd)
                continue
        asyncio.create_task(_run_signal())

    # Verify Path D candidates were not dropped
    _pathd_syms = {_sd.get("symbol") for _sd,_ts in _pending_signals if _sd.get("_path")=="D"}
    _resolved_syms = {_sd.get("symbol") for _sd,_ts in _resolved}
    for _ds in _pathd_syms:
        if _ds not in _resolved_syms:
            print(f"  [PATH-D DROPPED ERROR] {_ds} Path D candidate was dropped from resolve")
        else:
            _winner = next((_sd for _sd,_ts in _resolved if _sd.get("symbol")==_ds), None)
            if _winner and _winner.get("_path") != "D":
                print(f"  [PATH-D OVERRIDE] {_ds} Path D lost to {_winner.get('_path','sweep')} — check scores")

    # User-facing scan mode display:
    # OFF = execution disabled
    # MEDIUM = execution enabled + MEDIUM filters
    # PRO = execution enabled + strict PROD/PRO filters
    try:
        import agent as _ag_scan_mode
        _scan_filter_mode = getattr(_ag_scan_mode, "_TRADE_MODE", "PROD")
    except Exception:
        _scan_filter_mode = globals().get("TRADE_MODE", "PROD")
    try:
        _scan_exec_mode = get_auto_trade_mode()
    except Exception:
        _scan_exec_mode = "UNKNOWN"
    _scan_user_mode = "OFF" if _scan_exec_mode == "OFF" else ("MEDIUM" if _scan_filter_mode == "MEDIUM" else "PRO")

    if progress_msg_id:
        if signals_found:
            _top = sorted(dbg_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
            _cnt_str = "  |  ".join(f"{k}:{v}" for k, v in _top) if _top else "—"
            _long_cnt = sum(1 for s in signals_found if "LONG" in s)
            _short_cnt = sum(1 for s in signals_found if "SHORT" in s)
            _bias = f"L:{_long_cnt} S:{_short_cnt}"
            summary = (f"✅ Scan complete | AT: {_scan_user_mode} | {elapsed}s\n"
                      f"Checked: {total} | Valid: {len(signals_found)} | Blocked: {len(dbg_blocked)}\n"
                      f"Bias: {_bias} | Filters: {_cnt_str}\n"
                      f"Regime: {globals().get('_MARKET_REGIME','RANGE')}")
        else:
            # TTL cleanup
            _now = time.time()
            for _sym in list(ACTIVE_SIGNALS.keys()):
                if _now - ACTIVE_SIGNALS[_sym]["timestamp"] > SIGNAL_TTL:
                    del ACTIVE_SIGNALS[_sym]
            # Build summary
            if ACTIVE_SIGNALS:
                _age_lines = []
                for _sym, _sig in ACTIVE_SIGNALS.items():
                    _mins = int((_now - _sig["timestamp"]) / 60)
                    _age_lines.append(f"{_sym} — {_mins}min ago")
                _mem_str = "\n".join(_age_lines)
                summary = (f"✅ Scan complete\n"
                           f"AT: {_scan_user_mode}\n"
                           f"No new setups\n"
                           f"Time: {elapsed}s\n\n"
                           f"DEBUG:\n"
                           f"Scanned: {total} | Sweep: {dbg_sweep}\n"
                           f"Reclaim: {dbg_reclaim} | Vol OK: {dbg_volume}\n\n"
                           f"⚠️ Recent active signals:\n{_mem_str}")
            else:
                _top_reasons = sorted(dbg_reasons.items(), key=lambda x: x[1], reverse=True)[:2]
                _reason_str = " | ".join(f"{k}:{v}" for k,v in _top_reasons) if _top_reasons else "no_setup"
                summary = (f"✅ Scan complete | AT: {_scan_user_mode} | {elapsed}s\n"
                           f"Checked: {total} | Valid: 0 | Blocked: {len(dbg_blocked)}\n"
                           f"Regime: {globals().get('_MARKET_REGIME','RANGE')}\n"
                           f"No trade: {_reason_str}")
            if dbg_blocked or dbg_reasons:
                _lines = [f"{s} → {r}" for s, r in dbg_blocked[:10]]
                _sym_str = "\n".join(_lines)
                _top = sorted(dbg_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
                _cnt_str = "  |  ".join(f"{k}:{v}" for k, v in _top)
                summary += f"\nBLOCKED ({len(dbg_blocked)}):\n{_sym_str}\n\n({_cnt_str})"
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id, text=summary
            )
        except Exception:
            pass

    return signals_found


async def scanner_loop(context, chat_id):
    """Auto scan loop — syncs to 10M candle close."""
    global SCANNER_RUNNING
    while SCANNER_RUNNING:
        wait = seconds_until_next_candle()
        await asyncio.sleep(wait)
        if not SCANNER_RUNNING:
            break
        # Send progress message
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text="🔍 Auto scan starting..."
            )
            progress_msg_id = msg.message_id
        except Exception:
            progress_msg_id = None

        signals = await scan_market(context, chat_id, progress_msg_id)
        # Signal details suppressed — execution notifications sent by executor


# ── Command handlers ──────────────────────────────────────────────

async def cmd_scan(update, context):
    """/scan — manual immediate scan."""
    chat_id = update.effective_chat.id
    # Count symbols first
    try:
        import requests
        resp = requests.get(
            "https://api.bytick.com/v5/market/tickers",
            params={"category": "linear"}, timeout=20
        )
        tickers = resp.json().get("result", {}).get("list", [])
        count = sum(1 for t in tickers
                    if t.get("symbol","").endswith("USDT")
                    and float(t.get("turnover24h", 0)) >= 10_000_000)
        count = min(count, 25)
    except Exception:
        count = "?"

    msg = await update.message.reply_text(
        f"🔍 Scanning market...\nSymbols: {count}"
    )
    signals = await scan_market(context, chat_id, msg.message_id)
    # Signal details suppressed — execution notifications sent by executor


async def cmd_start_scan(update, context):
    """/start_scan — start auto scan synced to 15M candle."""
    global SCANNER_RUNNING
    if SCANNER_RUNNING:
        await update.message.reply_text("⚠️ Scanner already running.")
        return
    SCANNER_RUNNING = True
    chat_id = update.effective_chat.id
    wait = seconds_until_next_candle()
    mins = wait // 60
    secs = wait % 60
    await update.message.reply_text(
        f"✅ Auto scan started\nNext scan in: {mins}m {secs}s"
    )
    asyncio.create_task(scan_loop(context, chat_id, 10))


async def cmd_stop_scan(update, context):
    """/stop_scan — stop auto scan."""
    global SCANNER_RUNNING
    SCANNER_RUNNING = False
    await update.message.reply_text("🛑 Auto scan stopped.")



async def cmd_mode(update, context):
    """/mode strict or /mode soft"""
    global MODE
    if not context.args:
        await update.message.reply_text(f"Current mode: {MODE}\nUsage: /mode strict or /mode soft")
        return
    arg = context.args[0].upper()
    if arg in ("STRICT", "SOFT", "RAW"):
        MODE = arg
        await update.message.reply_text(f"✅ Mode set to: {MODE}")
    else:
        await update.message.reply_text("Usage: /mode strict / soft / raw")



async def set_commands(app):
    """Register command menu visible when user types /"""
    commands = []
    await app.bot.set_my_commands(commands)
    _kb = ReplyKeyboardMarkup([["\U0001f4ca Menu"]], resize_keyboard=True, is_persistent=True)
    import app as _app_ref
    for _cid in getattr(_app_ref, "ALLOWED_CHAT_IDS", []):
        try:
            await app.bot.send_message(chat_id=_cid, text="\U0001f916 Ready.", reply_markup=_kb)
            from menu import send_main_menu as _smm
            await _smm(app.bot, _cid)
        except Exception: pass


async def scan_loop(context, chat_id, minutes=15):
    global SCANNER_RUNNING
    if SCANNER_RUNNING:
        print(f"[SCAN GUARD] scan_loop already running — duplicate blocked")
        return
    SCANNER_RUNNING = True
    scan_loop._interval = minutes
    # Start internal 5M reclaim monitor
    if minutes == 15:
        async def _notify(msg): await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        from executor import handle_signal as _hs
        reclaim_monitor.start(_notify, _hs)
    """Auto scan loop — runs every N minutes. Cancellation-safe."""
    global loop_task
    while True:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔍 Auto scan ({minutes}min interval)..."
            )
            signals = await scan_market(context, chat_id, msg.message_id)
            # Signal details suppressed — execution notifications sent by executor
        except asyncio.CancelledError:
            SCANNER_RUNNING = False
            break
        except Exception as e:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Loop error: {e}")
            except Exception:
                pass
        await asyncio.sleep(seconds_until_next_candle(interval_minutes=minutes, offset_seconds=10))


async def cmd_loop15(update, context):
    """/loop15 — start auto scan every 15 minutes."""
    global loop_task
    if loop_task and not loop_task.done():
        loop_task.cancel()
    chat_id   = update.effective_chat.id
    loop_task = asyncio.create_task(scan_loop(context, chat_id, 10))
    await update.message.reply_text("✅ Auto scan started (every 15 min)\nUse /stop to cancel.")




async def cmd_loop5(update, context):
    """/loop5 — start auto scan every 5 minutes."""
    global loop_task
    if loop_task and not loop_task.done():
        loop_task.cancel()
    chat_id   = update.effective_chat.id
    loop_task = asyncio.create_task(scan_loop(context, chat_id, 5))
    await update.message.reply_text("✅ Auto scan started (every 5 min)\nUse /stop to cancel.")
async def cmd_stop(update, context):
    """/stop — stop auto scan loop."""
    global loop_task
    if loop_task and not loop_task.done():
        loop_task.cancel()
        loop_task = None
        await update.message.reply_text("🛑 Auto scan stopped.")
    else:
        await update.message.reply_text("No active loop running.")


async def cmd_mode_soft(update, context):
    """/mode_soft — switch to SOFT mode."""
    global MODE
    import agent as _a

    MODE = "SOFT"

    _a.SIGNAL_MODE = "SOFT"
    await update.message.reply_text("✅ Mode set to: SOFT")


async def cmd_mode_strict(update, context):
    """/mode_strict — switch to STRICT mode."""
    global MODE
    import agent as _a

    MODE = "STRICT"
    _a.SIGNAL_MODE = "STRICT"
    await update.message.reply_text("✅ Mode set to: STRICT")




async def cmd_market_news(update, context):
    """/market_news — market context overview."""
    ctx = get_market_context()
    await update.message.reply_text(ctx)

async def cmd_signals(update, context):
    """/signals — show active signal memory."""
    if not ACTIVE_SIGNALS:
        await update.message.reply_text("No active signals in memory.")
        return
    now = time.time()
    # TTL cleanup
    for sym in list(ACTIVE_SIGNALS.keys()):
        if now - ACTIVE_SIGNALS[sym]["timestamp"] > SIGNAL_TTL:
            del ACTIVE_SIGNALS[sym]
    if not ACTIVE_SIGNALS:
        await update.message.reply_text("No active signals in memory.")
        return
    await update.message.reply_text(
        f"⚠️ Active signals ({len(ACTIVE_SIGNALS)}):",
    )
    for sym, sig in ACTIVE_SIGNALS.items():
        mins = int((now - sig["timestamp"]) / 60)
        msg  = sig["text"] + f"\n\nAGE: {mins} min ago"
        try:
            await update.message.reply_text(msg, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(msg)


async def cmd_autotrade_on(update, context):
    """/autotrade_on — enable live execution."""
    enable_auto_trade()
    await update.message.reply_text("✅ Auto-trade ENABLED. CORE signals will execute live.")

async def cmd_autotrade_off(update, context):
    """/autotrade_off — disable live execution (kill switch)."""
    disable_auto_trade()
    await update.message.reply_text("🛑 Auto-trade DISABLED. No new orders will be placed.")

async def cmd_autotrade_status(update, context):
    """/autotrade_status — show current state."""
    state = "✅ ENABLED" if is_auto_trade_enabled() else "🛑 DISABLED"
    await update.message.reply_text(f"Auto-trade: {state}")


async def cmd_status(update, context):
    """/status — show system state."""
    from dotenv import load_dotenv
    load_dotenv('/Users/iouriilioukhine/ai-trading-agent/.env')
    import aiohttp, hmac, hashlib, time, os
    state = {"OFF":"🔴 OFF","SOFT":"🟡 SOFT","PRO":"🟢 PRO"}.get(get_auto_trade_mode(),"🔴 OFF")
    open_trades = count_open_trades()
    # Fetch balance
    usdt_bal = "n/a"
    try:
        key    = os.environ.get('BYBIT_API_KEY','')
        secret = os.environ.get('BYBIT_API_SECRET','')
        ts  = int(time.time() * 1000)
        qs  = 'accountType=UNIFIED'
        sig = hmac.new(secret.encode(), f'{ts}{key}5000{qs}'.encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': str(ts),
                   'X-BAPI-SIGN': sig, 'X-BAPI-RECV-WINDOW': '5000'}
        async with aiohttp.ClientSession() as s:
            async with s.get('https://api.bybit.com/v5/account/wallet-balance',
                             params={'accountType': 'UNIFIED'}, headers=headers) as r:
                data = await r.json()
        coins = data['result']['list'][0].get('coin', [])
        usdt  = next((c for c in coins if c['coin'] == 'USDT'), None)
        if usdt:
            usdt_bal = f"{float(usdt.get('walletBalance',0)):.2f} USDT"
    except Exception as e:
        usdt_bal = f"error: {e}"

    btn_label = "🔴 AutoTrade OFF" if not is_auto_trade_enabled() else "🟢 AutoTrade ON"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(btn_label, callback_data="toggle_autotrade")
    ]])
    import agent as _agent_st
    _fmode = getattr(_agent_st, "_TRADE_MODE", "PROD")
    _fmode_emoji = "🔵" if _fmode == "MEDIUM" else "🟢"
    text = (
        f"📊 *System Status*\n"
        f"─────────────────────\n"
        f"AutoTrade:     {state}\n"
        f"Filter mode:   {_fmode_emoji} {_fmode}\n"
        f"Balance:       {usdt_bal}\n"
        f"Active trades: {open_trades}\n"
        f"─────────────────────\n"
        f"Use /at to change execution mode\n"
        f"Use /tmode to change filter mode"
    )
    filter_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton(btn_label, callback_data="toggle_autotrade"),
        InlineKeyboardButton(f"⚙️ Filter: {_fmode_emoji} {_fmode}", callback_data="tmode_show"),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=filter_btn)


async def cb_toggle_autotrade(update, context):
    """Inline button toggle for AutoTrade."""
    query = update.callback_query
    await query.answer()
    if is_auto_trade_enabled():
        disable_auto_trade()
        state = "🔴 OFF"
        msg   = "🔴 AutoTrade *DISABLED*"
        btn   = "🔴 AutoTrade OFF"
    else:
        enable_auto_trade()
        state = "🟢 ON"
        msg   = "🟢 AutoTrade *ENABLED*"
        btn   = "🟢 AutoTrade ON"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(btn, callback_data="toggle_autotrade")
    ]])
    await query.edit_message_reply_markup(reply_markup=keyboard)
    await query.message.reply_text(msg, parse_mode="Markdown")


def _tmode_keyboard(current_mode):
    buttons = [
        InlineKeyboardButton(
            ("✅ " if current_mode == "MEDIUM" else "") + "🔵 MEDIUM",
            callback_data="tmode_MEDIUM"
        ),
        InlineKeyboardButton(
            ("✅ " if current_mode == "PROD" else "") + "🟢 PRO",
            callback_data="tmode_PROD"
        ),
    ]
    return InlineKeyboardMarkup([buttons])

async def cmd_trade_mode(update, context):
    """/tmode — show and switch signal filter mode (MEDIUM/PRO)."""
    global TRADE_MODE
    import agent as _agent
    args = context.args
    if not args:
        kb = _tmode_keyboard(TRADE_MODE)
        await update.message.reply_text(
            f"🎛 *Signal Filter Mode*\nActive: *{TRADE_MODE}*\n\nTap to switch:",
            parse_mode="Markdown", reply_markup=kb
        )
        return
    new_mode = args[0].upper()
    if new_mode not in ("PROD", "MEDIUM"):
        await update.message.reply_text("Invalid mode. Use: /tmode prod  or  /tmode medium")
        return
    TRADE_MODE = new_mode
    _agent._TRADE_MODE = new_mode
    with open(_MODE_FILE, 'w') as _f: _f.write(new_mode)
    emoji = "🔵" if new_mode == "MEDIUM" else "🟢"
    await update.message.reply_text(f"{emoji} Mode switched to *{new_mode}*", parse_mode="Markdown")

async def cb_tmode_button(update, context):
    """Handle inline button taps for signal filter mode."""
    global TRADE_MODE
    import agent as _agent
    query = update.callback_query
    await query.answer()
    new_mode = query.data.replace("tmode_", "").upper()
    if new_mode not in ("PROD", "MEDIUM"):
        return
    TRADE_MODE = new_mode
    _agent._TRADE_MODE = new_mode
    with open(_MODE_FILE, 'w') as _f: _f.write(new_mode)
    emoji = "🔵" if new_mode == "MEDIUM" else "🟢"
    kb = _tmode_keyboard(new_mode)
    await query.edit_message_text(
        f"🎛 *Signal Filter Mode*\nActive: *{new_mode}*\n\n{emoji} Switched to *{new_mode}*\nNo restart required.",
        parse_mode="Markdown", reply_markup=kb
    )


def _at_keyboard(current_mode):
    """Build inline keyboard with current mode highlighted."""
    labels = {
        "OFF":  "🔴 OFF",
        "SOFT": "🟡 SOFT",
        "PRO":  "🟢 PRO",
    }
    buttons = [
        InlineKeyboardButton(
            ("● " if m == current_mode else "○ ") + labels[m] + (" ✓" if m == current_mode else ""),
            callback_data=f"at_{m}"
        )
        for m in ("OFF", "SOFT", "PRO")
    ]
    return InlineKeyboardMarkup([buttons])

def _at_text(mode):
    descriptions = {
        "OFF":  "🔴 *AutoTrade: OFF*\nTrading disabled. No orders will be placed.",
        "SOFT": "🟡 *AutoTrade: SOFT*\nOnly HIGH confidence signals will be executed.",
        "PRO":  "🟢 *AutoTrade: PRO*\nAll valid signals will be executed.",
    }
    return descriptions.get(mode, "❓ Unknown mode")

async def cmd_at(update, context):
    """/at [off|soft|pro] — control AutoTrade mode."""
    args = context.args
    if not args:
        # Show current status with buttons
        mode = get_auto_trade_mode()
        from trade_db import count_open_trades as _cot
        import os as _osat
        max_tph = int(open(_osat.path.join(_osat.path.dirname(__file__), '.env')).read()
                      .split('MAX_TRADES_PER_HOUR=')[-1].split('\n')[0]
                      if 'MAX_TRADES_PER_HOUR=' in open(_osat.path.join(
                          _osat.path.dirname(__file__), '.env')).read() else "3")
        text = (
            f"🤖 *AutoTrade Status*\n"
            f"──────────────────\n"
            f"Mode:            {_at_text(mode).split('*')[1]}\n"
            f"Max trades/hour: {max_tph}\n"
            f"Active trades:   {_cot()}\n"
            f"──────────────────\n"
            f"Tap to change mode:"
        )
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=_at_keyboard(mode)
        )
        return

    new_mode = args[0].upper()
    if new_mode not in ("OFF", "SOFT", "PRO"):
        await update.message.reply_text(
            "❓ Usage:\n/at off — disable\n/at soft — HIGH only\n/at pro — all signals"
        )
        return

    set_auto_trade_mode(new_mode, notify_fn=lambda msg: context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown"))
    await update.message.reply_text(
        _at_text(new_mode),
        parse_mode="Markdown",
        reply_markup=_at_keyboard(new_mode)
    )

async def cb_at_button(update, context):
    """Handle inline button taps for AutoTrade mode."""
    query = update.callback_query
    await query.answer()
    new_mode = query.data.replace("at_", "").upper()
    if new_mode not in ("OFF", "SOFT", "PRO"):
        return
    set_auto_trade_mode(new_mode)
    await query.edit_message_text(
        _at_text(new_mode),
        parse_mode="Markdown",
        reply_markup=_at_keyboard(new_mode)
    )


async def cmd_loop5_internal(context, chat_id):
    """Start loop5 programmatically."""
    global loop_task, SCANNER_RUNNING
    if loop_task and not loop_task.done():
        loop_task.cancel()
    SCANNER_RUNNING = True
    loop_task = asyncio.create_task(scan_loop(context, chat_id, 5))

async def cmd_loop15_internal(context, chat_id):
    """Start loop15 programmatically."""
    global loop_task, SCANNER_RUNNING
    if loop_task and not loop_task.done():
        loop_task.cancel()
    SCANNER_RUNNING = True
    loop_task = asyncio.create_task(scan_loop(context, chat_id, 10))

async def cmd_status_internal(bot, chat_id):
    """Send status message."""
    from executor import get_auto_trade_mode, is_auto_trade_enabled
    from trade_db import count_open_trades
    try:
        import aiohttp, hmac, hashlib, time as _t, os as _os2
        from dotenv import load_dotenv as _ld; _ld()
        key = _os2.environ.get("BYBIT_API_KEY",""); secret = _os2.environ.get("BYBIT_API_SECRET","")
        ts  = int(_t.time()*1000); qs = "accountType=UNIFIED"
        sig = hmac.new(secret.encode(), f"{ts}{key}5000{qs}".encode(), hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY":key,"X-BAPI-TIMESTAMP":str(ts),"X-BAPI-SIGN":sig,"X-BAPI-RECV-WINDOW":"5000"}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.bybit.com/v5/account/wallet-balance",
                             params={"accountType":"UNIFIED"},headers=headers,
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
        coins = data["result"]["list"][0].get("coin",[])
        usdt  = next((c for c in coins if c["coin"]=="USDT"),None)
        bal   = float(usdt.get("walletBalance",0)) if usdt else 0
        usdt_bal = f"{bal:.2f} USDT"
    except Exception as e:
        usdt_bal = f"error"
    at_mode = get_auto_trade_mode()
    icons   = {"OFF":"🔴","SOFT":"🟡","PRO":"🟢"}
    text = (
        f"📊 *System Status*\n"
        f"─────────────────────\n"
        f"AutoTrade:     {icons.get(at_mode,'')} {at_mode}\n"
        
        f"Signal Mode:   {get_auto_trade_mode()}\n"
        f"Balance:       {usdt_bal}\n"
        f"Active trades: {count_open_trades()}\n"
        f"─────────────────────"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")


async def cmd_menu(update, context):
    """/menu — show main control panel."""
    """/menu — show main control panel."""
    from menu import refresh_menu, send_main_menu
    chat_id = update.effective_chat.id
    from menu import _menu_message_ids
    if chat_id in _menu_message_ids:
        await refresh_menu(context.bot, chat_id)
    else:
        await send_main_menu(context.bot, chat_id)


async def _structure_exit_monitor():
    """
    Background monitor — checks open trades every 1H for exit conditions.
    Runs independently of lifecycle loop.
    """
    import time as _t
    print("[STRUCTURE MONITOR] started")
    print("[EXIT MONITOR] Started")

    while True:
        try:
            await asyncio.sleep(3600)  # check every 1H
            from executor import _get, _post, _notify, get_instrument_info
            import json as _json

            with open('journal.json') as f:
                trades = _json.load(f)

            open_trades = [t for t in trades if t.get('status') == 'OPEN']
            if not open_trades:
                continue

            now = _t.time()
            for trade in open_trades:
                sym = trade.get('symbol')
                side = trade.get('side', 'LONG')
                is_long = side == 'LONG' or side == 'Buy'
                entry = float(trade.get('entry_price', 0))
                ts_open = float(trade.get('timestamp_open', 0))
                upnl = float(trade.get('pnl_usdt', 0))

                if not sym or not entry or not ts_open:
                    continue

                duration_min = (now - ts_open) / 60
                duration_h = duration_min / 60

                print(f"[EXIT CHECK] {sym} pnl={upnl:.4f} duration={duration_h:.1f}h")

                # ── STALL EXIT (ATR-based, trend-aware) ──────────
                try:
                    if duration_min >= 60 and upnl <= 0:
                        from agent import get_kline, detect_trend
                        df15m = get_kline(sym, "15")
                        df1h_t = get_kline(sym, "60")
                        if df15m is not None and len(df15m) >= 20:
                            # ATR from 15m
                            _hi = df15m['high'].astype(float)
                            _lo = df15m['low'].astype(float)
                            _cl = df15m['close'].astype(float)
                            _tr = (_hi - _lo).combine((_hi - _cl.shift()).abs(), max).combine((_lo - _cl.shift()).abs(), max)
                            _atr = float(_tr.rolling(14).mean().iloc[-1])
                            _atr_threshold = _atr * 0.4
                            _cur_price = float(df15m['close'].iloc[-1])
                            _price_move = abs(_cur_price - entry)
                            # Trend-aware stall time
                            _stall_min = 60
                            try:
                                _t15, _, _ = detect_trend(df15m)
                                _t1h, _, _ = detect_trend(df1h_t) if df1h_t is not None else ("NEUTRAL","","")
                                _dir_str = "BULLISH" if is_long else "BEARISH"
                                if _t1h == _dir_str: _stall_min = 120
                                elif _t15 == _dir_str: _stall_min = 90
                            except: pass
                            if duration_min >= _stall_min and _price_move < _atr_threshold:
                                print(f"[STALL EXIT] {sym} duration={duration_min:.0f}m pnl={upnl:.4f} move={_price_move:.6f} atr_thresh={_atr_threshold:.6f}")
                                await _close_trade_market(sym, is_long, trade, "stall_exit")
                                continue
                except Exception as _se:
                    print(f"[STALL EXIT ERROR] {sym}: {_se}")

                # ── MOMENTUM FAILURE EXIT ─────────────────────────
                try:
                    if duration_min >= 90 and upnl < 0:
                        sl = float(trade.get('sl', 0))
                        if sl > 0 and entry > 0:
                            _loss_r = abs(entry - float(df15m['close'].iloc[-1] if 'df15m' in dir() and df15m is not None else entry)) / abs(entry - sl)
                            try:
                                from agent import get_kline, detect_trend
                                _df15_mf = get_kline(sym, "15")
                                _t15_mf, _, _ = detect_trend(_df15_mf)
                                _dir_str_mf = "BULLISH" if is_long else "BEARISH"
                                if _loss_r > 0.6 and _t15_mf != _dir_str_mf:
                                    print(f"[MOMENTUM EXIT] {sym} loss_r={_loss_r:.2f} trend={_t15_mf} duration={duration_min:.0f}m")
                                    await _close_trade_market(sym, is_long, trade, "momentum_failure")
                                    continue
                            except: pass
                except Exception as _mfe:
                    print(f"[MOMENTUM EXIT ERROR] {sym}: {_mfe}")

                # ── HARD TIMEOUT ──────────────────────────────────
                if duration_min >= 240 and upnl < 0:
                    print(f"[HARD TIMEOUT] {sym} duration={duration_min:.0f}m pnl={upnl:.4f}")
                    await _close_trade_market(sym, is_long, trade, "hard_timeout")
                    continue

                # MIN HOLD for profit exits: 2 hours
                if duration_h < 2.0:
                    continue

                # PROFIT CONDITION for structure exits: must be in profit
                if upnl <= 0:
                    continue

                print(f"[STRUCTURE CHECK] {sym} pnl={upnl:.4f} duration={duration_h:.1f}h")

                # Fetch 1H klines
                try:
                    from agent import get_kline
                    df1h = get_kline(sym, "60")
                    if df1h is None or len(df1h) < 5:
                        continue

                    closes = df1h['close'].astype(float).tolist()
                    highs  = df1h['high'].astype(float).tolist()
                    lows   = df1h['low'].astype(float).tolist()

                    # ── RANGE EXIT (dead trade) ──────────────────────
                    last4_high = max(highs[-4:])
                    last4_low  = min(lows[-4:])
                    range_pct  = (last4_high - last4_low) / entry * 100
                    if range_pct < 0.4:
                        print(f"[EXIT TRIGGERED] {sym} reason=range_dead range={range_pct:.3f}%")
                        await _close_trade_market(sym, is_long, trade, "range_dead")
                        continue

                    # ── STRUCTURE EXIT (swing low/high break) ────────
                    # Find swing low: candle with lower lows on both sides
                    exit_triggered = False
                    for i in range(len(lows)-3, len(lows)-1):
                        if i < 1: continue
                        if is_long:
                            # Swing low: lows[i] < lows[i-1] and lows[i] < lows[i+1] (if exists)
                            is_swing = lows[i] < lows[i-1] and (i+1 >= len(lows) or lows[i] < lows[i+1])
                            if is_swing and closes[-1] < lows[i]:
                                print(f"[EXIT TRIGGERED] {sym} reason=structure_break swing_low={lows[i]:.6f} close={closes[-1]:.6f}")
                                await _close_trade_market(sym, is_long, trade, "structure_break")
                                exit_triggered = True
                                break
                        else:
                            is_swing = highs[i] > highs[i-1] and (i+1 >= len(highs) or highs[i] > highs[i+1])
                            if is_swing and closes[-1] > highs[i]:
                                print(f"[EXIT TRIGGERED] {sym} reason=structure_break swing_high={highs[i]:.6f} close={closes[-1]:.6f}")
                                await _close_trade_market(sym, is_long, trade, "structure_break")
                                exit_triggered = True
                                break

                except Exception as _ex:
                    print(f"[EXIT CHECK ERROR] {sym}: {_ex}")

        except asyncio.CancelledError:
            break
        except Exception as _me:
            print(f"[EXIT MONITOR ERROR] {_me}")
            await asyncio.sleep(60)


async def _close_trade_market(symbol: str, is_long: bool, trade: dict, reason: str):
    """Close position via market order."""
    from executor import _post, _notify, get_instrument_info, _round_qty
    try:
        # Get actual position size from Bybit
        from executor import _get
        r = await _get('/v5/position/list', {'category': 'linear', 'symbol': symbol})
        pos_list = r.get('result', {}).get('list', [])
        actual_qty = 0.0
        for p in pos_list:
            if float(p.get('size', 0)) > 0:
                actual_qty = float(p.get('size', 0))
                break
        if actual_qty <= 0:
            print(f"[EXIT SKIP] {symbol} no position found")
            return

        close_side = "Sell" if is_long else "Buy"
        payload = {
            "category": "linear",
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": str(actual_qty),
            "timeInForce": "IOC",
            "reduceOnly": True,
        }
        resp = await _post("/v5/order/create", payload)
        if resp.get("retCode") == 0:
            print(f"[ORDER CLOSED] {symbol} reason={reason} qty={actual_qty}")
            await _notify(f"🚪 EXIT — {symbol}\nReason: {reason}\nDuration: trade closed by structure monitor")
            # Update close_type in journal
            try:
                import json as _jj2, os as _oj2
                _jp = _oj2.path.join(_oj2.path.dirname(__file__), "journal.json")
                with open(_jp) as _f2: _jdata = _jj2.load(_f2)
                for _jt in _jdata:
                    if _jt.get('symbol')==symbol and _jt.get('status')=='OPEN':
                        _jt['close_type'] = reason
                        break
                with open(_jp,'w') as _f2: _jj2.dump(_jdata, _f2, indent=2)
            except Exception as _je:
                print(f"[JOURNAL UPDATE ERROR] {_je}")
        else:
            print(f"[EXIT FAILED] {symbol}: {resp.get('retMsg')}")
    except Exception as _ce:
        print(f"[EXIT CLOSE ERROR] {symbol}: {_ce}")


async def _trade_lifecycle_loop():
    """Bybit-first journal sync — module level, independent of main()."""
    import json as _jj, os as _oj, time as _tj2
    _jpath = _oj.path.join(_oj.path.dirname(__file__), "journal.json")

    def _load_j():
        try:
            with open(_jpath) as _f: return _jj.load(_f)
        except: return []

    def _save_j(e):
        with open(_jpath, "w") as _f: _jj.dump(e, _f, indent=2)

    def _det_side(ep, xp, pnl):
        if abs(pnl) < 0.0001:
            return "LONG" if xp >= ep else "SHORT"
        return "LONG" if ((xp > ep and pnl > 0) or (xp < ep and pnl < 0)) else "SHORT"

    print("[SYNC LOOP START]")
    while True:
        try:
            entries = _load_j()
            changed = False
            now_ts = _tj2.time()

            # TASK 1 — Dedup pass (runs first every cycle)
            _open_by_sym = {}
            for _di, _de in enumerate(entries):
                if _de.get("status") == "OPEN":
                    _ds = _de.get("symbol")
                    if _ds not in _open_by_sym:
                        _open_by_sym[_ds] = []
                    _open_by_sym[_ds].append((_di, _de))
            for _ds, _dlist in _open_by_sym.items():
                if len(_dlist) > 1:
                    # Keep executor-created (confidence != "—"), else latest timestamp
                    _exec = [(_i,_e) for _i,_e in _dlist if _e.get("confidence","—") != "—"]
                    if _exec:
                        _keep_i = max(_exec, key=lambda x: x[1].get("timestamp_open",0))[0]
                    else:
                        _keep_i = max(_dlist, key=lambda x: x[1].get("timestamp_open",0))[0]
                    for _di2, _de2 in _dlist:
                        if _di2 != _keep_i:
                            entries[_di2]["status"] = "CLOSED"
                            entries[_di2]["close_type"] = "dedup_cleanup"
                            entries[_di2]["timestamp_close"] = now_ts
                            changed = True
                    print(f"[DEDUP CLEANUP] {_ds} resolved {len(_dlist)} duplicate OPEN entries")

            # STEP 1: open positions
            pos_data = await _get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
            if not pos_data:
                print("[LIFECYCLE] _get returned None — skipping cycle")
                await asyncio.sleep(30)
                continue
            live = [p for p in pos_data.get("result", {}).get("list", []) if float(p.get("size", 0) or 0) > 0]
            print(f"[OPEN POSITIONS FOUND] {len(live)}")
            open_syms = {e.get("symbol") for e in entries if e.get("status") == "OPEN"}
            live_map = {p["symbol"]: p for p in live}

            # AUTO SYNC-CLOSE: require 2 consecutive misses (60s apart) before closing
            for _se in entries:
                if _se.get("status") != "OPEN":
                    continue
                _ssym = _se.get("symbol")
                if _ssym not in live_map:
                    _open_age = now_ts - float(_se.get("timestamp_open") or now_ts)
                    if _open_age < 90:
                        print(f"[SYNC CLOSE SKIP] {_ssym} too fresh ({_open_age:.0f}s) — waiting")
                        continue
                    # Two-cycle confirmation: set pending flag first, close on second miss
                    if not _se.get("sync_close_pending"):
                        _se["sync_close_pending"] = now_ts
                        changed = True
                        print(f"[SYNC CLOSE PENDING] {_ssym} — confirming next cycle")
                        continue
                    _pending_age = now_ts - float(_se.get("sync_close_pending") or now_ts)
                    if _pending_age < 25:
                        print(f"[SYNC CLOSE WAIT] {_ssym} — pending {_pending_age:.0f}s")
                        continue
                    _se["status"] = "CLOSED"
                    _se["close_type"] = "auto_sync"
                    _se["timestamp_close"] = now_ts
                    _se["sync_close_pending"] = None
                    changed = True
                    print(f"[SYNC CLOSE] {_ssym} confirmed gone after 2 cycles — auto-closed")
                else:
                    # Position found — clear any pending flag
                    if _se.get("sync_close_pending"):
                        _se["sync_close_pending"] = None
                        changed = True
            # Rebuild open_syms after sync-close
            open_syms = {e.get("symbol") for e in entries if e.get("status") == "OPEN"}

            for p in live:
                sym = p.get("symbol")
                if sym not in open_syms:
                    # Skip if a recent entry for this symbol was closed within last 120s
                    _recent_closed = any(
                        e.get("symbol") == sym and e.get("status") == "CLOSED"
                        and now_ts - float(e.get("timestamp_close") or 0) < 120
                        for e in entries
                    )
                    if _recent_closed:
                        print(f"[RECONCILE SKIP] {sym} recently closed — skipping re-create")
                        continue
                    side = "LONG" if p.get("side") == "Buy" else "SHORT"
                    ep = float(p.get("avgPrice", 0) or 0)
                    upnl = float(p.get("unrealisedPnl", 0) or 0)
                    qty = float(p.get("size", 0) or 0)
                    sl = float(p.get("stopLoss", 0) or 0)
                    tp = float(p.get("takeProfit", 0) or 0)
                    # TASK 4 — Timestamp lock: always use now_ts for reconciliation
                    created = now_ts
                    print(f"[TIMESTAMP FIX] {sym} using now_ts for reconciled entry")
                     # Fetch real SL from position, TP from limit orders
                    _real_sl = sl
                    _tp1 = 0.0; _tp2 = 0.0; _tp3 = 0.0
                    try:
                        _ord_r = await _get("/v5/order/realtime", {"category":"linear","symbol":sym})
                        _ords = _ord_r.get("result",{}).get("list",[])
                        _is_l = side == "LONG"
                        _tps = sorted(
                            [float(o.get("price",0)) for o in _ords
                             if o.get("reduceOnly") and o.get("orderType")=="Limit"
                             and float(o.get("price",0))>0
                             and ((_is_l and float(o.get("price",0))>ep) or (not _is_l and float(o.get("price",0))<ep))],
                            reverse=not _is_l
                        )
                        _order_link_id = ""
                        for _o in _ords:
                            if _o.get("reduceOnly") and "_tp1" in _o.get("orderLinkId",""):
                                _order_link_id = _o.get("orderLinkId","").replace("_tp1","")
                                break
                        if len(_tps)>=1: _tp1=_tps[0]
                        if len(_tps)>=2: _tp2=_tps[1]
                        if len(_tps)>=3: _tp3=_tps[2]
                        print(f"[SL/TP FETCHED] {sym} sl={_real_sl} tp1={_tp1} tp2={_tp2} tp3={_tp3}")
                    except Exception as _oe:
                        print(f"[SL/TP FETCH ERROR] {sym}: {_oe}")
                    # Dedup check — never create duplicate OPEN for same symbol
                    _already_open = any(e.get("symbol")==sym and e.get("status")=="OPEN" for e in entries)
                    if _already_open:
                        print(f"[JOURNAL] skip duplicate OPEN {sym}")
                    else:
                        # Look up all context fields from DB via order_link_id
                        _db_conf = None; _db_setup = None; _db_path = None
                        _db_t4h = None; _db_t1h = None
                        _db_hour = None; _db_sess = None; _db_score = None
                        if _order_link_id:
                            try:
                                from trade_db import get_trade_by_link_id as _gtbl
                                _dbt = _gtbl(_order_link_id)
                                if _dbt:
                                    _db_conf  = _dbt.get("confidence") or None
                                    _db_setup = _dbt.get("setup_class") or None
                                    _db_path  = _dbt.get("path") or None
                                    _db_t4h   = _dbt.get("t4h") or None
                                    _db_t1h   = _dbt.get("t1h") or None
                                    _db_hour  = _dbt.get("hour_utc")
                                    _db_sess  = _dbt.get("session") or None
                                    _db_score = _dbt.get("signal_score")
                            except Exception as _dbe:
                                pass
                        entries.append({
                            "trade_id": f"bybit_{sym}_{int(created)}",
                            "order_link_id": _order_link_id,
                            "symbol": sym, "side": side,
                            "entry_price": ep, "exit_price": None,
                            "sl": _real_sl, "tp1": _tp1, "tp2": _tp2, "tp3": _tp3,
                            "qty": qty, "original_qty": qty, "remaining_qty": qty,
                            "pnl_realised": 0.0, "pnl_usdt": None, "result": None,
                            "timestamp_open": created, "timestamp_close": None,
                            "duration_min": None, "status": "OPEN",
                            "confidence": _db_conf, "setup_type": _db_setup,
                            "path": _db_path, "t4h": _db_t4h, "t1h": _db_t1h,
                            "hour_utc": _db_hour, "session": _db_sess,
                            "signal_score": _db_score,
                            "reconciled": True,
                            "ghost_pending": False, "ghost_detected_at": None,
                        })
                        changed = True
                        print(f"[JOURNAL] create {sym} {side} entry={ep} conf={_db_conf} setup={_db_setup} path={_db_path} t4h={_db_t4h} t1h={_db_t1h} session={_db_sess}")

            for i, e in enumerate(entries):
                if e.get("status") == "OPEN" and e.get("symbol") in live_map:
                    _lp = live_map[e["symbol"]]
                    _sym_u = e.get("symbol")
                    _side_u = e.get("side","LONG")
                    _updated = []
                    # pnl
                    new_upnl = round(float(_lp.get("unrealisedPnl", 0) or 0), 4)
                    if entries[i].get("pnl_usdt") != new_upnl:
                        entries[i]["pnl_usdt"] = new_upnl
                        changed = True
                        _updated.append("pnl")
                    # qty
                    new_qty = float(_lp.get("size", 0) or 0)
                    if new_qty > 0 and entries[i].get("qty") != new_qty:
                        entries[i]["qty"] = new_qty
                        changed = True
                        _updated.append("qty")
                    # sl — direction-safe
                    new_sl = float(_lp.get("stopLoss", 0) or 0)
                    _cur_sl = float(entries[i].get("sl", 0) or 0)
                    _sl_ok = False
                    if new_sl > 0:
                        if _cur_sl == 0:
                            _sl_ok = True
                        elif _side_u == "LONG" and new_sl >= _cur_sl:
                            _sl_ok = True
                        elif _side_u == "SHORT" and new_sl <= _cur_sl:
                            _sl_ok = True
                    if _sl_ok and entries[i].get("sl") != new_sl:
                        entries[i]["sl"] = new_sl
                        changed = True
                        _updated.append("sl")
                    if _updated:
                        print(f"[JOURNAL UPDATE] {_sym_u} updated: {','.join(_updated)}")

            # STEP 1a: TP1 fallback detection (in case WS missed the fill)
            try:
                from executor import _TP1_HIT_TIMES, on_tp_hit as _on_tp_hit_fn
                for _te in entries:
                    if _te.get("status") != "OPEN": continue
                    if _te.get("tp1_hit"): continue
                    _tsym = _te.get("symbol")
                    if _tsym not in live_map: continue
                    _tp1_price = float(_te.get("tp1") or 0)
                    if _tp1_price <= 0: continue
                    _link = _te.get("order_link_id") or _te.get("trade_id") or ""
                    if not _link:
                        print(f"[TP1 FALLBACK SKIP] {_tsym} no order_link_id — cannot fire")
                        continue
                    if _TP1_HIT_TIMES.get(_link): continue  # already processed by WS
                    _lp = live_map[_tsym]
                    _mark = float(_lp.get("markPrice", 0) or _lp.get("lastPrice", 0) or 0)
                    _tside = _te.get("side", "LONG")
                    _is_long = _tside in ("Buy", "LONG")
                    _tp1_crossed = (_is_long and _mark >= _tp1_price) or (not _is_long and _mark <= _tp1_price)
                    _age = now_ts - float(_te.get("timestamp_open") or now_ts)
                    if _tp1_crossed and _age > 60:
                        print(f"[TP1 FALLBACK] {_tsym} mark={_mark} tp1={_tp1_price} link={_link} — firing")
                        await _on_tp_hit_fn(_link, 1, _tp1_price)
                        _te["tp1_hit"] = True
                        changed = True
            except Exception as _tp1fb_e:
                print(f"[TP1 FALLBACK ERROR] {_tp1fb_e}")

            # STEP 1b: trailing stop after TP1
            try:
                import os as _os_trail
                _trail_pct = float(_os_trail.environ.get("TRAILING_STOP_PCT", "0.6")) / 100
                from executor import _TP1_HIT_TIMES, _post as _epost
                for _te in entries:
                    if _te.get("status") != "OPEN": continue
                    _tsym = _te.get("symbol")
                    if _tsym not in live_map: continue
                    _link = _te.get("order_link_id", "") or _te.get("trade_id", "")
                    # Only activate if tp1_hit recorded and 5+ seconds passed
                    _tp1_ts = _TP1_HIT_TIMES.get(_link, 0)
                    if not _tp1_ts: continue
                    if now_ts - _tp1_ts < 5: continue
                    # Get current price and SL
                    _lp = live_map[_tsym]
                    _cur_price = float(_lp.get("markPrice", 0) or _lp.get("lastPrice", 0) or 0)
                    _cur_sl = float(_lp.get("stopLoss", 0) or 0)
                    if _cur_price <= 0: continue
                    _tside = _te.get("side", "Buy")
                    _is_long = _tside in ("Buy", "LONG")
                    # Calculate new trailing SL
                    if _is_long:
                        _new_sl = round(_cur_price * (1 - _trail_pct), 8)
                    else:
                        _new_sl = round(_cur_price * (1 + _trail_pct), 8)
                    # Only update if improvement
                    if _is_long and _new_sl <= _cur_sl: continue
                    if not _is_long and _new_sl >= _cur_sl and _cur_sl > 0: continue
                    # Update SL on Bybit
                    _tr = await _epost("/v5/position/trading-stop", {
                        "category": "linear",
                        "symbol": _tsym,
                        "stopLoss": str(_new_sl),
                        "slTriggerBy": "MarkPrice",
                    })
                    if _tr.get("retCode") == 0:
                        _te["sl"] = _new_sl
                        changed = True
                        print(f"[TRAIL] {_tsym} SL moved {_cur_sl} → {_new_sl} price={_cur_price}")
                    else:
                        print(f"[TRAIL ERROR] {_tsym}: {_tr.get('retMsg')}")
            except Exception as _trail_ex:
                print(f"[TRAIL EXCEPTION] {_trail_ex}")

            # STEP 2: closed trades
            start_ms = str(int((now_ts - 7 * 86400) * 1000))
            cl_data = await _get("/v5/position/closed-pnl", {"category": "linear", "limit": "100", "startTime": start_ms})
            bybit_closed = cl_data.get("result", {}).get("list", [])
            print(f"[CLOSED TRADES FOUND] {len(bybit_closed)}")
            closed_ids = {e.get("trade_id") for e in entries if e.get("status") == "CLOSED"}

            print(f"[BYBIT CLOSED LIST] {len(bybit_closed)} records")
            _sym_counts = {}
            for _dbct in bybit_closed:
                _dbsym = _dbct.get("symbol","?")
                _sym_counts[_dbsym] = _sym_counts.get(_dbsym, 0) + 1
                print(f"[BYBIT TRADE] sym={_dbsym} orderId={_dbct.get('orderId')} pnl={_dbct.get('closedPnl')} qty={_dbct.get('qty')} updated={_dbct.get('updatedTime')}")
            for _s, _c in _sym_counts.items():
                if _c > 1: print(f"[MULTI FILL] {_s} has {_c} records in bybit_closed")
            for ct in bybit_closed:
                oid = ct.get("orderId", "")
                if oid in closed_ids:
                    print(f"[SKIP DUPLICATE] {ct.get('symbol')} oid={oid}")
                    continue
                sym = ct.get("symbol")
                ep = float(ct.get("avgEntryPrice", 0) or 0)
                xp = float(ct.get("avgExitPrice", 0) or 0)
                pnl = float(ct.get("closedPnl", 0) or 0)
                qty = float(ct.get("qty", 0) or 0)
                created = int(ct.get("createdTime", 0) or 0) / 1000
                updated = int(ct.get("updatedTime", 0) or 0) / 1000
                dur = round((updated - created) / 60, 1)
                side = _det_side(ep, xp, pnl)
                result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"
                _matched_idx = None  # initialize before first use
                _entry_obj = entries[_matched_idx] if _matched_idx is not None else {}
                if result == "LOSS" and _entry_obj.get("tp1_hit"):
                    result = "BE"
                    print(f"[RESULT] {sym} upgraded LOSS to BE (tp1_hit=True)")
                pnl_pct = round(pnl / (ep * qty) * 100, 3) if ep > 0 and qty > 0 else 0
                # CHAOTIC consecutive loss tracking → 4h pause after 3 in a row
                global _CHAOTIC_LOSS_TIMES, _CHAOTIC_PAUSE_UNTIL, _MARKET_REGIME
                if _MARKET_REGIME == "CHAOTIC":
                    _now_cl = int(time.time())
                    if result == "LOSS":
                        _CHAOTIC_LOSS_TIMES.append(_now_cl)
                        _CHAOTIC_LOSS_TIMES = [t for t in _CHAOTIC_LOSS_TIMES if _now_cl - t < 86400]
                        if len(_CHAOTIC_LOSS_TIMES) >= 3:
                            _CHAOTIC_PAUSE_UNTIL = _now_cl + 4 * 3600
                            _CHAOTIC_LOSS_TIMES = []
                            print(f"[REGIME] CHAOTIC 3 consecutive losses — 4h pause until {_CHAOTIC_PAUSE_UNTIL}")
                    elif result == "WIN":
                        _CHAOTIC_LOSS_TIMES = []  # reset on win

                _epsilon = 0.001
                _matched_idx = None
                for i, e in enumerate(entries):
                    if (e.get("status") == "OPEN"
                            and e.get("symbol") == sym
                            and not e.get("internal")):
                        _matched_idx = i
                        break

                if _matched_idx is not None:
                    i = _matched_idx
                    _known_side = entries[i].get("side", side)
                    side = _known_side
                    _prev_realised = float(entries[i].get("pnl_realised", 0) or 0)
                    _new_realised = round(_prev_realised + pnl, 6)
                    _prev_remaining = float(entries[i].get("remaining_qty") or entries[i].get("qty") or 0)
                    _new_remaining = max(0.0, _prev_remaining - qty)
                    _is_final = (sym not in live_map) or (_new_remaining <= _epsilon)

                    if not _is_final:
                        entries[i]["pnl_realised"] = _new_realised
                        entries[i]["remaining_qty"] = _new_remaining
                        entries[i]["tp1_hit"] = True
                        entries[i]["last_partial_exit"] = xp
                        changed = True
                        print(f"[PARTIAL CLOSE] {sym} pnl_realised={_new_realised} remaining_qty={_new_remaining}")
                    else:
                        _orig_entry = float(entries[i].get("entry_price", ep) or ep)
                        _orig_qty = float(entries[i].get("original_qty") or entries[i].get("qty") or qty)
                        _partial_exit = float(entries[i].get("last_partial_exit", 0) or 0)
                        _partial_qty = _orig_qty - _prev_remaining
                        if _partial_exit > 0 and _partial_qty > 0:
                            _wavg_exit = round((_partial_exit * _partial_qty + xp * qty) / _orig_qty, 8)
                        else:
                            _wavg_exit = xp
                        _total_pnl = round(_new_realised, 4)
                        _total_pnl_pct = round(_total_pnl / (_orig_entry * _orig_qty) * 100, 3) if _orig_entry > 0 and _orig_qty > 0 else 0
                        _final_result = "WIN" if _total_pnl > 0 else "LOSS" if _total_pnl < 0 else "BE"
                        _tp1_hit = bool(entries[i].get("tp1_hit")) or bool(entries[i].get("tp1",0)>0 and ((side=="LONG" and xp>=float(entries[i].get("tp1",0))*0.999) or (side=="SHORT" and xp<=float(entries[i].get("tp1",0))*1.001)))
                        _sl_hit = bool(entries[i].get("sl",0)>0 and ((side=="LONG" and xp<=float(entries[i].get("sl",0))*1.001) or (side=="SHORT" and xp>=float(entries[i].get("sl",0))*0.999)))
                        _be_exit = bool(_orig_entry>0 and abs(xp-_orig_entry)/max(_orig_entry,0.000001)<0.003)
                        # If trade is profitable but labeled SL — was a trailing stop
                        try:
                            from executor import _TP1_HIT_TIMES as _TP1_HIT_TIMES_REF
                        except Exception: _TP1_HIT_TIMES_REF = {}
                        _was_trailing = bool(entries[i].get("tp1_hit") or _TP1_HIT_TIMES_REF.get(entries[i].get("order_link_id","")))
                        if _sl_hit and _total_pnl > 0: _close_type = "tp1+trail"
                        elif _tp1_hit or _was_trailing: _close_type = "tp1+trail"
                        elif _sl_hit: _close_type = "sl"
                        elif _be_exit: _close_type = "be"
                        else: _close_type = "market_close"
                        _open_ts = float(entries[i].get("timestamp_open") or created)
                        _dur = round((updated - _open_ts) / 60, 1)
                        entries[i].update({
                            "trade_id": oid,
                            "exit_price": _wavg_exit,
                            "pnl_usdt": _total_pnl,
                            "pnl_realised": _new_realised,
                            "pnl_pct": _total_pnl_pct,
                            "result": _final_result,
                            "side": side,
                            "remaining_qty": 0,
                            "timestamp_close": updated,
                            "duration_min": _dur,
                            "status": "CLOSED",
                            "close_type": _close_type,
                            "regime": _MARKET_REGIME,
                            "macro_state": None,
                            "signal_score": None,
                            "atr_at_signal": None,
                            "tp1_dist_pct": round(abs(float(entries[_matched_idx].get("tp1",0) or 0) - float(entries[_matched_idx].get("entry_price",0) or 0)) / float(entries[_matched_idx].get("entry_price",1) or 1) * 100, 4) if _matched_idx is not None and float(entries[_matched_idx].get("entry_price",0) or 0) > 0 else None,
                            "tp1_hit": _tp1_hit,
                            "sl_hit": _sl_hit,
                            "be_exit": _be_exit,
                        })
                        # ── Derived analytics fields (written once at close) ──
                        _e = entries[i]
                        _entry_p = float(_e.get("entry_price") or 0)
                        _sl_p    = float(_e.get("sl") or 0)
                        _tp1_p   = float(_e.get("tp1") or 0)
                        _qty_f   = float(_e.get("original_qty") or _e.get("qty") or 0)
                        _side_f  = _e.get("side","LONG")
                        # fee_usdt
                        _fee = round(_qty_f * _entry_p * 0.00055 * 2, 6) if _entry_p > 0 else 0
                        # net_pnl_usdt
                        _net_pnl = round(_total_pnl - _fee, 6)
                        # sl_pct
                        _sl_pct = round(abs(_entry_p - _sl_p) / _entry_p * 100, 4) if _entry_p > 0 and _sl_p > 0 else None
                        # r_multiple
                        _risk = abs(_entry_p - _sl_p) if _entry_p > 0 and _sl_p > 0 else 0
                        _exit_f = float(_e.get("exit_price") or 0)
                        if _risk > 0 and _exit_f > 0:
                            _r = (_exit_f - _entry_p) / _risk if _side_f == "LONG" else (_entry_p - _exit_f) / _risk
                            _r_mult = round(_r, 4)
                        else:
                            _r_mult = None
                        # tp1_rr
                        if _risk > 0 and _tp1_p > 0:
                            _tp1_r = (_tp1_p - _entry_p) / _risk if _side_f == "LONG" else (_entry_p - _tp1_p) / _risk
                            _tp1_rr = round(_tp1_r, 4)
                        else:
                            _tp1_rr = None
                        # trailing_hit
                        _trailing_hit = bool(_tp1_hit and _close_type == "tp1+trail")
                        # full_cycle_completed
                        _full_cycle = bool(_tp1_hit and _e.get("be_exit") == False and _trailing_hit)
                        entries[i].update({
                            "fee_usdt": _fee,
                            "net_pnl_usdt": _net_pnl,
                            "sl_pct": _sl_pct,
                            "r_multiple": _r_mult,
                            "tp1_rr": _tp1_rr,
                            "trailing_hit": _trailing_hit,
                            "full_cycle_completed": _full_cycle,
                            "expectancy_contribution": round(_total_pnl - _fee, 4),
                        })
                        changed = True
                        print(f"[TRADE CLOSED] {sym} {side} total_pnl={_total_pnl} net={_net_pnl} R={_r_mult} -> {_final_result}")
                        # Edge snapshot
                        try:
                            import trade_db as _tdb2
                            _es = _tdb2.get_edge_stats(last_n=20)
                            if _es and _es["total_trades"] >= 3:
                                print(f"[EDGE] trades={_es['total_trades']} WR={_es['win_rate']}% AvgR={_es['avg_r']} MedR={_es['median_r']} Exp=${_es['avg_expectancy_usdt']} MedExp=${_es['median_expectancy_usdt']} Fees=${_es['total_fees']}")
                        except Exception: pass

                else:
                    entries.append({
                        "trade_id": oid, "symbol": sym, "side": side,
                        "entry_price": ep, "exit_price": xp,
                        "sl": 0, "tp1": 0,
                        "qty": qty, "original_qty": qty, "remaining_qty": 0,
                        "pnl_realised": round(pnl, 4),
                        "pnl_usdt": round(pnl, 4), "pnl_pct": pnl_pct,
                        "result": result, "timestamp_open": created,
                        "timestamp_close": updated, "duration_min": dur,
                        "status": "CLOSED", "confidence": "—", "setup_type": "—",
                        "reconciled": True,
                    })
                    changed = True
                    print(f"[NEW CLOSED TRADE DETECTED] {sym} {side} pnl={pnl:.4f} -> {result}")

            # STEP 3: execution/list fill sync (parallel to closed-pnl, does not replace)
            try:
                import json as _jf_json, os as _jf_os
                _jf_path = _jf_os.path.join(_jf_os.path.dirname(__file__), "journal_fills.json")
                try:
                    with open(_jf_path) as _jf_f: _jf_data = _jf_json.load(_jf_f)
                except Exception: _jf_data = []
                _known_exec_ids = {f.get("exec_id") for f in _jf_data if f.get("exec_id")}
                _last_ts = max((int(f.get("timestamp",0) or 0) for f in _jf_data), default=0)
                _exec_start = str(max(_last_ts - 5000, int((now_ts - 7*86400)*1000)))
                _exec_resp = await _get("/v5/execution/list", {
                    "category": "linear",
                    "limit": "100",
                    "startTime": _exec_start,
                })
                _exec_list = _exec_resp.get("result", {}).get("list", [])
                print(f"[EXEC FETCH] {len(_exec_list)} executions from Bybit (since {_exec_start})")
                _new_fills = 0
                for _ex in _exec_list:
                    _eid = _ex.get("execId", "")
                    if not _eid or _eid in _known_exec_ids:
                        continue
                    _fill = {
                        "exec_id":       _eid,
                        "order_id":      _ex.get("orderId", ""),
                        "order_link_id": _ex.get("orderLinkId", ""),
                        "symbol":        _ex.get("symbol", ""),
                        "side":          _ex.get("side", ""),
                        "exec_price":    float(_ex.get("execPrice", 0) or 0),
                        "exec_qty":      float(_ex.get("execQty", 0) or 0),
                        "closed_size":   float(_ex.get("closedSize", 0) or 0),
                        "exec_value":    float(_ex.get("execValue", 0) or 0),
                        "fee":           float(_ex.get("execFee", 0) or 0),
                        "exec_type":     _ex.get("execType", ""),
                        "is_closing":    float(_ex.get("closedSize", 0) or 0) > 0,
                        "timestamp":     int(_ex.get("execTime", 0) or 0),
                        "is_maker":      _ex.get("isMaker", False),
                        "leaves_qty":    float(_ex.get("leavesQty", 0) or 0),
                    }
                    _jf_data.append(_fill)
                    _known_exec_ids.add(_eid)
                    _new_fills += 1
                    print(f"[FILL STORED] {_fill['symbol']} side={_fill['side']} price={_fill['exec_price']} qty={_fill['exec_qty']} closed={_fill['closed_size']} execId={_eid}")
                if _new_fills > 0:
                    with open(_jf_path, "w") as _jf_f: _jf_json.dump(_jf_data, _jf_f, indent=2)
                    print(f"[FILL SYNC] stored {_new_fills} new fills | total={len(_jf_data)}")
                print(f"[COUNT CHECK] bybit_exec={len(_exec_list)} new_stored={_new_fills} total_fills={len(_jf_data)}")
            except Exception as _jfe:
                import traceback
                print(f"[FILL SYNC ERROR] {_jfe}")
                print(f"[FILL SYNC ERROR DETAIL] {repr(_jfe)}")
                traceback.print_exc()

            # STEP 3: execution/list fill sync (parallel to closed-pnl, does not replace)
            try:
                import json as _jf_json, os as _jf_os
                _jf_path = _jf_os.path.join(_jf_os.path.dirname(__file__), "journal_fills.json")
                try:
                    with open(_jf_path) as _jf_f: _jf_data = _jf_json.load(_jf_f)
                except Exception: _jf_data = []
                _known_exec_ids = {f.get("exec_id") for f in _jf_data if f.get("exec_id")}
                _last_ts = max((int(f.get("timestamp",0) or 0) for f in _jf_data), default=0)
                _exec_start = str(max(_last_ts - 5000, int((now_ts - 7*86400)*1000)))
                _exec_resp = await _get("/v5/execution/list", {
                    "category": "linear",
                    "limit": "100",
                    "startTime": _exec_start,
                })
                _exec_list = _exec_resp.get("result", {}).get("list", [])
                print(f"[EXEC FETCH] {len(_exec_list)} executions from Bybit (since {_exec_start})")
                _new_fills = 0
                for _ex in _exec_list:
                    _eid = _ex.get("execId", "")
                    if not _eid or _eid in _known_exec_ids:
                        continue
                    _fill = {
                        "exec_id":       _eid,
                        "order_id":      _ex.get("orderId", ""),
                        "order_link_id": _ex.get("orderLinkId", ""),
                        "symbol":        _ex.get("symbol", ""),
                        "side":          _ex.get("side", ""),
                        "exec_price":    float(_ex.get("execPrice", 0) or 0),
                        "exec_qty":      float(_ex.get("execQty", 0) or 0),
                        "closed_size":   float(_ex.get("closedSize", 0) or 0),
                        "exec_value":    float(_ex.get("execValue", 0) or 0),
                        "fee":           float(_ex.get("execFee", 0) or 0),
                        "exec_type":     _ex.get("execType", ""),
                        "is_closing":    float(_ex.get("closedSize", 0) or 0) > 0,
                        "timestamp":     int(_ex.get("execTime", 0) or 0),
                        "is_maker":      _ex.get("isMaker", False),
                        "leaves_qty":    float(_ex.get("leavesQty", 0) or 0),
                    }
                    _jf_data.append(_fill)
                    _known_exec_ids.add(_eid)
                    _new_fills += 1
                    print(f"[FILL STORED] {_fill['symbol']} side={_fill['side']} price={_fill['exec_price']} qty={_fill['exec_qty']} closed={_fill['closed_size']} execId={_eid}")
                if _new_fills > 0:
                    with open(_jf_path, "w") as _jf_f: _jf_json.dump(_jf_data, _jf_f, indent=2)
                    print(f"[FILL SYNC] stored {_new_fills} new fills | total={len(_jf_data)}")
                print(f"[COUNT CHECK] bybit_exec={len(_exec_list)} new_stored={_new_fills} total_fills={len(_jf_data)}")
            except Exception as _jfe:
                import traceback
                print(f"[FILL SYNC ERROR] {_jfe}")
                print(f"[FILL SYNC ERROR DETAIL] {repr(_jfe)}")
                traceback.print_exc()

            # STEP 3: execution/list fill sync (parallel to closed-pnl, does not replace)
            try:
                import json as _jf_json, os as _jf_os
                _jf_path = _jf_os.path.join(_jf_os.path.dirname(__file__), "journal_fills.json")
                try:
                    with open(_jf_path) as _jf_f: _jf_data = _jf_json.load(_jf_f)
                except Exception: _jf_data = []
                _known_exec_ids = {f.get("exec_id") for f in _jf_data if f.get("exec_id")}
                _last_ts = max((int(f.get("timestamp",0) or 0) for f in _jf_data), default=0)
                _exec_start = str(max(_last_ts - 5000, int((now_ts - 7*86400)*1000)))
                _exec_resp = await _get("/v5/execution/list", {
                    "category": "linear",
                    "limit": "100",
                    "startTime": _exec_start,
                })
                _exec_list = _exec_resp.get("result", {}).get("list", [])
                print(f"[EXEC FETCH] {len(_exec_list)} executions from Bybit (since {_exec_start})")
                _new_fills = 0
                for _ex in _exec_list:
                    _eid = _ex.get("execId", "")
                    if not _eid or _eid in _known_exec_ids:
                        continue
                    _fill = {
                        "exec_id":       _eid,
                        "order_id":      _ex.get("orderId", ""),
                        "order_link_id": _ex.get("orderLinkId", ""),
                        "symbol":        _ex.get("symbol", ""),
                        "side":          _ex.get("side", ""),
                        "exec_price":    float(_ex.get("execPrice", 0) or 0),
                        "exec_qty":      float(_ex.get("execQty", 0) or 0),
                        "closed_size":   float(_ex.get("closedSize", 0) or 0),
                        "exec_value":    float(_ex.get("execValue", 0) or 0),
                        "fee":           float(_ex.get("execFee", 0) or 0),
                        "exec_type":     _ex.get("execType", ""),
                        "is_closing":    float(_ex.get("closedSize", 0) or 0) > 0,
                        "timestamp":     int(_ex.get("execTime", 0) or 0),
                        "is_maker":      _ex.get("isMaker", False),
                        "leaves_qty":    float(_ex.get("leavesQty", 0) or 0),
                    }
                    _jf_data.append(_fill)
                    _known_exec_ids.add(_eid)
                    _new_fills += 1
                    print(f"[FILL STORED] {_fill['symbol']} side={_fill['side']} price={_fill['exec_price']} qty={_fill['exec_qty']} closed={_fill['closed_size']} execId={_eid}")
                if _new_fills > 0:
                    with open(_jf_path, "w") as _jf_f: _jf_json.dump(_jf_data, _jf_f, indent=2)
                    print(f"[FILL SYNC] stored {_new_fills} new fills | total={len(_jf_data)}")
                print(f"[COUNT CHECK] bybit_exec={len(_exec_list)} new_stored={_new_fills} total_fills={len(_jf_data)}")
            except Exception as _jfe:
                import traceback
                print(f"[FILL SYNC ERROR] {_jfe}")
                print(f"[FILL SYNC ERROR DETAIL] {repr(_jfe)}")
                traceback.print_exc()

            # TASK 3 — Ghost cleanup (2-cycle confirmation)
            for i, e in enumerate(entries):
                if e.get("status") != "OPEN":
                    continue
                _gsym = e.get("symbol")
                if _gsym in live_map:
                    # Position is alive — clear any pending ghost flag
                    if entries[i].get("ghost_pending"):
                        entries[i]["ghost_pending"] = False
                        entries[i]["ghost_detected_at"] = None
                        changed = True
                else:
                    # Position missing from Bybit
                    if not entries[i].get("ghost_pending"):
                        entries[i]["ghost_pending"] = True
                        entries[i]["ghost_detected_at"] = now_ts
                        changed = True
                        print(f"[GHOST PENDING] {_gsym} not found on Bybit — cycle 1")
                    else:
                        _ghost_age = now_ts - float(entries[i].get("ghost_detected_at") or now_ts)
                        if _ghost_age >= 60:
                            entries[i]["status"] = "CLOSED"
                            entries[i]["close_type"] = "ghost_cleanup"
                            entries[i]["timestamp_close"] = now_ts
                            entries[i]["ghost_pending"] = False
                            changed = True
                            print(f"[GHOST CLEANUP] {_gsym} confirmed closed after {int(_ghost_age)}s")

            if changed:
                _save_j(entries)

        except Exception as _le:
            import traceback
            print(f"[LIFECYCLE ERROR] {_le}")
            traceback.print_exc()
        import asyncio as _aio2
        await _aio2.sleep(30)

def main():
    init_db()
    # ── Journal HTTP server ──────────────────────────────────────
    import http.server, threading, os as _os
    _journal_dir = _os.path.dirname(_os.path.abspath(__file__))
    class _JHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=_journal_dir, **kw)
        def log_message(self, fmt, *args): pass
        def end_headers(self):
            self.send_header('Cache-Control','no-store, no-cache, must-revalidate')
            self.send_header('Pragma','no-cache')
            super().end_headers()
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            if parsed.path == '/bots':
                try:
                    import os as _os2
                    _bp = _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)), "bots_config.json")
                    with open(_bp) as _bf: _bd = _bf.read()
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Access-Control-Allow-Origin','*')
                    self.end_headers()
                    self.wfile.write(_bd.encode())
                except Exception as _be:
                    print(f"BOTS ERROR: {_be}")
                    self.send_response(500); self.end_headers()
                return
            if parsed.path == '/journal':
                import json as _jj2, os as _oj
                try:
                    # Load legacy journal (all records)
                    _base_dir = '/Users/iouriilioukhine/ai-trading-agent'
                    with open(_oj.path.join(_base_dir,'journal.json')) as _f2:
                        _legacy = _jj2.load(_f2)
                    # Load fills-based trades
                    _trades_path = _oj.path.join(_base_dir,'journal_trades.json')
                    _fills_trades = []
                    if _oj.path.exists(_trades_path):
                        with open(_trades_path) as _ft: _fills_trades = _jj2.load(_ft)
                    # Normalize fills trades to legacy schema
                    _ft_normalized = []
                    for _t in _fills_trades:
                        _ft_normalized.append({
                            "status": "CLOSED",
                            "symbol": _t.get("symbol"),
                            "side": _t.get("side"),
                            "entry_price": _t.get("entry_price"),
                            "exit_price": _t.get("exit_price"),
                            "qty": _t.get("total_qty"),
                            "pnl_usdt": _t.get("pnl_usdt"),
                            "net_pnl_usdt": _t.get("net_pnl_usdt"),
                            "fee_usdt": _t.get("fee_usdt"),
                            "timestamp_open": _t.get("open_timestamp",0)/1000,
                            "timestamp_close": _t.get("close_timestamp",0)/1000,
                            "order_link_id": _t.get("order_link_id",""),
                            "trade_id": _t.get("trade_id",""),
                            "confidence": "HIGH",
                            "path": "D",
                            "session": None,
                            "regime": None,
                            "sl_pct": None,
                            "r_multiple": None,
                            "close_type": "tp1+trail",
                            "tp1_hit": True,
                            "result": "WIN" if (_t.get("pnl_usdt",0) or 0) > 0 else "LOSS",
                            "is_valid_for_stats": True,
                            "data_source": "fills",
                        })
                    # Build dedup key set from fills trades
                    _fills_keys = set()
                    for _t in _fills_trades:
                        _fills_keys.add((_t.get("symbol"), round(float(_t.get("open_timestamp",0))/1000)))
                    # Keep from legacy: OPEN trades + BLOCKED + CLOSED not in fills
                    _legacy_keep = []
                    for _r in _legacy:
                        if _r.get("status") != "CLOSED":
                            _r["data_source"] = "legacy"
                            _legacy_keep.append(_r)
                        else:
                            _rkey = (_r.get("symbol"), round(float(_r.get("timestamp_open",0))))
                            if _rkey not in _fills_keys:
                                _r["data_source"] = "legacy"
                                _legacy_keep.append(_r)
                    # Serve: OPEN/BLOCKED from legacy + CLOSED from fills only
                    _open_blocked = [r for r in _legacy_keep if r.get("status") != "CLOSED"]
                    _jdata = _open_blocked + _ft_normalized
                    _open = sum(1 for r in _open_blocked if r.get("status")=="OPEN")
                    _closed_fills = len(_ft_normalized)
                    print(f"[JOURNAL API] open={_open} closed_fills={_closed_fills} total={len(_jdata)}")
                    _body = _jj2.dumps(_jdata).encode()
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Access-Control-Allow-Origin','*')
                    self.send_header('Content-Length',str(len(_body)))
                    self.end_headers()
                    self.wfile.write(_body)
                except Exception as _je2:
                    import traceback
                    _tb = traceback.format_exc()
                    print(f"[JOURNAL 500 ERROR] {_tb}")
                    with open("/tmp/journal_error.txt","w") as _ef: _ef.write(_tb)
                    self.send_response(500); self.end_headers()
                return

            if parsed.path == '/klines':
                import asyncio as _al, json as _json
                qs = parse_qs(parsed.query)
                symbol = qs.get('symbol',[''])[0]
                frm = qs.get('from',[''])[0]
                to = qs.get('to',[''])[0]
                if not symbol:
                    self.send_response(400); self.end_headers(); return
                try:
                    import aiohttp as _ah, hmac as _hm, hashlib as _hs, time as _ti, os as _oe
                    API_KEY = _oe.environ.get('BYBIT_API_KEY','')
                    API_SECRET = _oe.environ.get('BYBIT_API_SECRET','')
                    BASE_URL = 'https://api.bybit.com'
                    async def fetch_klines():
                        interval_param = qs.get('interval',['5'])[0]
                        params = {'category':'linear','symbol':symbol,'interval':interval_param,'limit':'300'}
                        if frm: params['start'] = str(int(float(frm)*1000))
                        if to: params['end'] = str(int(float(to)*1000))
                        url = BASE_URL + '/v5/market/kline'
                        async with _ah.ClientSession() as sess:
                            async with sess.get(url, params=params, timeout=_ah.ClientTimeout(total=10)) as resp:
                                return await resp.json()
                    import threading as _th
                    _result = {}
                    def _run():
                        loop = _al.new_event_loop()
                        _al.set_event_loop(loop)
                        try: _result['data'] = loop.run_until_complete(fetch_klines())
                        finally: loop.close()
                    t = _th.Thread(target=_run); t.start(); t.join(timeout=15)
                    data = _result.get('data', {})
                    result = data.get('result',{})
                    klines = result.get('list',[])
                    # Convert to OHLCV: [ts, open, high, low, close, vol]
                    candles = [{'t':int(k[0]),'o':float(k[1]),'h':float(k[2]),'l':float(k[3]),'c':float(k[4]),'v':float(k[5])} for k in reversed(klines)]
                    body = _json.dumps({'candles':candles,'symbol':symbol}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Access-Control-Allow-Origin','*')
                    self.send_header('Content-Length',str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as _ke:
                    err = _json.dumps({'error':str(_ke)}).encode()
                    self.send_response(500); self.send_header('Content-Type','application/json'); self.send_header('Access-Control-Allow-Origin','*'); self.end_headers(); self.wfile.write(err)
            elif parsed.path == '/balance':
                import json as _jb, threading as _tb, asyncio as _ab2
                import aiohttp as _ahb, hmac as _hmb, hashlib as _hsb, time as _tib, os as _oeb
                _resb = {}
                async def fetch_bal():
                    ts=str(int(_tib.time()*1000));rw='5000'
                    params={'accountType':'UNIFIED'}
                    qs='&'.join(f'{k}={v}' for k,v in sorted(params.items()))
                    key=_oeb.environ.get('BYBIT_API_KEY','')
                    secret=_oeb.environ.get('BYBIT_API_SECRET','')
                    sig=_hmb.new(secret.encode(),f'{ts}{key}{rw}{qs}'.encode(),_hsb.sha256).hexdigest()
                    headers={'X-BAPI-API-KEY':key,'X-BAPI-TIMESTAMP':ts,'X-BAPI-SIGN':sig,'X-BAPI-RECV-WINDOW':rw}
                    async with _ahb.ClientSession() as sess:
                        async with sess.get('https://api.bybit.com/v5/account/wallet-balance',params=params,headers=headers,timeout=_ahb.ClientTimeout(total=10)) as resp:
                            return await resp.json()
                def _runb():
                    loop=_ab2.new_event_loop();_ab2.set_event_loop(loop)
                    try:_resb['d']=loop.run_until_complete(fetch_bal())
                    finally:loop.close()
                _tb.Thread(target=_runb).start()
                import time as _twait;_twait.sleep(3)
                raw=_resb.get('d',{})
                bal=0;upnl=0
                for coin in raw.get('result',{}).get('list',[{}])[0].get('coin',[]):
                    if coin.get('coin')=='USDT':
                        bal=float(coin.get('walletBalance',0))
                        upnl=float(coin.get('unrealisedPnl',0))
                body=_jb.dumps({'balance':bal,'unrealisedPnl':upnl}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Access-Control-Allow-Origin','*')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == '/positions':
                import json as _json, threading as _th, asyncio as _al2
                import aiohttp as _ah2, os as _oe2
                BASE_URL2 = 'https://api.bybit.com'
                _res2 = {}
                async def fetch_pos():
                    import hmac as _hm2, hashlib as _hs2, time as _ti2
                    API_KEY2 = _oe2.environ.get('BYBIT_API_KEY','')
                    API_SECRET2 = _oe2.environ.get('BYBIT_API_SECRET','')
                    ts = str(int(_ti2.time()*1000))
                    rw = '5000'
                    params = {'category':'linear','settleCoin':'USDT'}
                    qs2 = '&'.join(f'{k}={v}' for k,v in sorted(params.items()))
                    sign_str = f'{ts}{API_KEY2}{rw}{qs2}'
                    sig = _hm2.new(API_SECRET2.encode(), sign_str.encode(), _hs2.sha256).hexdigest()
                    headers = {'X-BAPI-API-KEY':API_KEY2,'X-BAPI-TIMESTAMP':ts,'X-BAPI-SIGN':sig,'X-BAPI-RECV-WINDOW':rw}
                    url = BASE_URL2 + '/v5/position/list'
                    async with _ah2.ClientSession() as sess:
                        async with sess.get(url, params=params, headers=headers, timeout=_ah2.ClientTimeout(total=10)) as resp:
                            return await resp.json()
                def _run2():
                    loop = _al2.new_event_loop()
                    _al2.set_event_loop(loop)
                    try: _res2['data'] = loop.run_until_complete(fetch_pos())
                    finally: loop.close()
                t2 = _th.Thread(target=_run2); t2.start(); t2.join(timeout=15)
                raw = _res2.get('data', {})
                positions = [p for p in raw.get('result',{}).get('list',[]) if float(p.get('size',0))>0]
                # Fetch balance + unrealized PnL
                _bal = 0.0; _upnl_total = 0.0
                try:
                    import hmac as _hm3, hashlib as _hs3, time as _ti3
                    API_KEY3 = _oe2.environ.get('BYBIT_API_KEY','')
                    API_SECRET3 = _oe2.environ.get('BYBIT_API_SECRET','')
                    async def fetch_bal3():
                        ts3 = str(int(_ti3.time()*1000)); rw3 = '5000'
                        qs3 = 'accountType=UNIFIED'
                        ss3 = f'{ts3}{API_KEY3}{rw3}{qs3}'
                        sg3 = _hm3.new(API_SECRET3.encode(), ss3.encode(), _hs3.sha256).hexdigest()
                        hh3 = {'X-BAPI-API-KEY':API_KEY3,'X-BAPI-TIMESTAMP':ts3,'X-BAPI-SIGN':sg3,'X-BAPI-RECV-WINDOW':rw3}
                        url3 = 'https://api.bybit.com/v5/account/wallet-balance'
                        async with _ah2.ClientSession() as s3:
                            async with s3.get(url3, params={'accountType':'UNIFIED'}, headers=hh3, timeout=_ah2.ClientTimeout(total=8)) as r3:
                                return await r3.json()
                    _rb3 = {}
                    def _runb3():
                        lp3 = _al2.new_event_loop(); _al2.set_event_loop(lp3)
                        try: _rb3['d'] = lp3.run_until_complete(fetch_bal3())
                        finally: lp3.close()
                    import threading as _tb3; t3 = _tb3.Thread(target=_runb3); t3.start(); t3.join(timeout=8)
                    for coin in _rb3.get('d',{}).get('result',{}).get('list',[{}])[0].get('coin',[]):
                        if coin.get('coin') == 'USDT':
                            _bal = float(coin.get('walletBalance', 0))
                            _upnl_total = float(coin.get('unrealisedPnl', 0))
                except Exception as _be3:
                    pass
                # Enrich positions with journal data (SL, TP1, confidence, path)
                try:
                    import os as _oj3, json as _jj3
                    _jpath = _oj3.path.join(_oj3.path.dirname(__file__), 'journal.json')
                    with open(_jpath) as _jf3: _jdata3 = _jj3.load(_jf3)
                    _open_j = {e.get('order_link_id'): e for e in _jdata3 if e.get('status') == 'OPEN' and e.get('order_link_id')}
                    _sym_j  = {e.get('symbol'): e for e in _jdata3 if e.get('status') == 'OPEN'}
                    for p in positions:
                        sym = p.get('symbol','')
                        # Normalize Bybit fields for dashboard
                        p['entry_price']   = float(p.get('avgPrice', 0) or 0)
                        p['pnl_usdt']      = float(p.get('unrealisedPnl', 0) or 0)
                        p['positionValue'] = float(p.get('positionValue', 0) or 0)
                        p['leverage']      = float(p.get('leverage', 0) or 0)
                        p['sl']            = float(p.get('stopLoss', 0) or 0)
                        # Match by symbol to get journal metadata
                        je = _sym_j.get(sym, {})
                        if je:
                            if not p.get('sl') or p['sl'] == 0:
                                p['sl'] = float(je.get('sl', 0) or 0)
                            p['tp1']        = je.get('tp1', 0)
                            p['confidence'] = je.get('confidence', '')
                            p['path']       = je.get('path', '')
                            p['setup_type'] = je.get('setup_type', '')
                            p['timestamp_open'] = je.get('timestamp_open', 0)
                            p['tp1_hit']    = je.get('tp1_hit', False)
                            p['order_link_id'] = je.get('order_link_id', '')
                            p['qty']        = float(je.get('qty', 0) or 0)
                except Exception as _je3:
                    pass
                body = _json.dumps({'positions': positions, 'balance': _bal, 'unrealisedPnl': _upnl_total}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Access-Control-Allow-Origin','*')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()
    def _start_journal_server():
        try:
            srv = http.server.HTTPServer(("127.0.0.1", 8765), _JHandler)
            print("✅ Journal server running on http://localhost:8765")
            srv.serve_forever()
        except Exception as _se:
            print(f"Journal server error: {_se}")
    threading.Thread(target=_start_journal_server, daemon=False).start()
    init_executor()
    # ── Trade lifecycle polling ──────────────────────────────────
    # lifecycle loop started via post_init below
    print("✅ Trade lifecycle polling started")
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(__import__("executor").reconcile_on_startup())

    print("🤖 Telegram bot starting...")
    async def _on_startup(app):
        # 1. Set bot commands
        await set_commands(app)
        # 2. Start lifecycle loop
        asyncio.create_task(_trade_lifecycle_loop())
        print("✅ Trade lifecycle polling started")
        asyncio.create_task(_structure_exit_monitor())
        print("✅ Structure exit monitor started")
        # 3. Start WebSocket for TP fill detection
        try:
            from executor import on_tp_hit as _tp_hit_fn
            from executor import get_trade_by_link_id as _get_trade_fn
            from executor import update_status as _update_status_fn
            from executor import _notify as _exec_notify
            ws_set_callbacks(
                telegram_fn=_exec_notify,
                tp_hit_fn=_tp_hit_fn,
                get_trade_fn=_get_trade_fn,
                update_status_fn=_update_status_fn
            )
            start_ws()
            print("✅ WebSocket started for TP fill detection")
        except Exception as _we:
            print(f"⚠️ WebSocket start failed: {_we}")
        # 4. Auto-start 15m scanner loop
        try:
            global SCANNER_RUNNING
            import os as _os_scan
            _auto_chat_id = int(_os_scan.environ.get("TELEGRAM_CHAT_ID", "0"))
            if not _auto_chat_id and ALLOWED_CHAT_IDS:
                _auto_chat_id = ALLOWED_CHAT_IDS[0]
            if _auto_chat_id:
                SCANNER_RUNNING = False
                if _auto_chat_id not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(_auto_chat_id)
                asyncio.create_task(scan_loop(app, _auto_chat_id, 10))
                print(f"✅ Auto scanner started for chat_id={_auto_chat_id}")
                from executor import set_auto_trade_mode as _set_mode
                _set_mode("PRO")
                print("✅ Auto trade mode forced to PRO from .env")
            elif SCANNER_RUNNING:
                print("⚠️ Scanner already running — skipping auto-start")
            else:
                print("⚠️ TELEGRAM_CHAT_ID not set — scanner not auto-started")
        except Exception as _se:
            print(f"⚠️ Auto scanner start failed: {_se}")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_on_startup).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("context", cmd_context))
    application.add_handler(CommandHandler("analyse", cmd_analyse))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("start_scan", cmd_start_scan))
    application.add_handler(CommandHandler("stop_scan", cmd_stop_scan))
    application.add_handler(CommandHandler("mode", cmd_mode))
    application.add_handler(CommandHandler("loop5",  cmd_loop5))
    application.add_handler(CommandHandler("loop15", cmd_loop15))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("mode_soft", cmd_mode_soft))
    application.add_handler(CommandHandler("mode_strict", cmd_mode_strict))
    application.add_handler(CommandHandler("market_news", cmd_market_news))
    application.add_handler(CommandHandler("signals", cmd_signals))
    application.add_handler(CommandHandler("autotrade_on", cmd_autotrade_on))
    application.add_handler(CommandHandler("autotrade_off", cmd_autotrade_off))
    application.add_handler(CommandHandler("autotrade_status", cmd_autotrade_status))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("tmode", cmd_trade_mode))
    application.add_handler(CallbackQueryHandler(cb_tmode_button, pattern="^tmode_"))

    async def _cb_tmode_show(update, context):
        query = update.callback_query
        await query.answer()
        import agent as _agent_sh
        _fm = getattr(_agent_sh, "_TRADE_MODE", "PROD")
        await query.message.reply_text(
            f"🎛 *Signal Filter Mode*\nActive: *{_fm}*\n\nTap to switch:",
            parse_mode="Markdown",
            reply_markup=_tmode_keyboard(_fm)
        )

    application.add_handler(CallbackQueryHandler(_cb_tmode_show, pattern="^tmode_show$"))
    application.add_handler(CommandHandler("at", cmd_at))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CallbackQueryHandler(handle_menu_callback, pattern="^menu_"))
    application.add_handler(CallbackQueryHandler(cb_at_button, pattern="^at_"))
    application.add_handler(CallbackQueryHandler(cb_toggle_autotrade, pattern="^toggle_autotrade$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Running. Send a symbol in Telegram.")

    application.run_polling()

if __name__ == "__main__":
    main()
