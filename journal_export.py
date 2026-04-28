"""
journal_export.py
Trade journal — writes to journal.json after each closed trade.
"""
import json, os, time, logging

log = logging.getLogger("journal")
JOURNAL_PATH = os.path.join(os.path.dirname(__file__), "journal.json")

def _load():
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return []

def _save(data):
    with open(JOURNAL_PATH, "w") as f:
        json.dump(data, f, indent=2)
    try:
        import shutil, os
        downloads = os.path.expanduser("~/Downloads/journal.json")
        shutil.copy2(JOURNAL_PATH, downloads)
    except Exception:
        pass

def append_trade_to_journal(trade: dict):
    """Append or update a trade entry in journal.json."""
    try:
        entries = _load()
        trade_id = trade.get("trade_id") or trade.get("order_link_id", "?")
        # Update if exists
        for i, e in enumerate(entries):
            if e.get("trade_id") == trade_id:
                entries[i].update(trade)
                _save(entries)
                log.info("JOURNAL UPDATED: %s", trade.get("symbol"))
                return
        # New entry
        entries.append(trade)
        _save(entries)
        log.info("JOURNAL ENTRY CREATED: %s", trade.get("symbol"))
    except Exception as e:
        log.error("Journal write error: %s", e)

def create_journal_entry(symbol, side, entry_price, sl, tp1, tp2, tp3,
                          qty, order_link_id, confidence, setup_type="—",
                          regime="standard", stop_pct=0.0, runner_mult=1.5):
    """Create open trade entry in journal."""
    entry = {
        "trade_id":      order_link_id,
        "symbol":        symbol,
        "side":          side,
        "entry_price":   entry_price,
        "sl":            sl,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           tp3,
        "qty":           qty,
        "confidence":    confidence,
        "setup_type":    setup_type,
        "regime":        regime,
        "stop_pct":      stop_pct,
        "runner_mult":   runner_mult,
        "timestamp_open": time.time(),
        "timestamp_close": None,
        "exit_price":    None,
        "pnl_usdt":      None,
        "pnl_pct":       None,
        "result":        None,
        "duration_min":  None,
        "status":        "OPEN"
    }
    append_trade_to_journal(entry)
    return entry

def close_journal_entry(order_link_id, exit_price, result="UNKNOWN", pnl_usdt=None):
    """Mark trade as closed and calculate PnL."""
    try:
        entries = _load()
        for i, e in enumerate(entries):
            if e.get("trade_id") == order_link_id and e.get("status") == "OPEN":
                entry_price = e.get("entry_price", exit_price)
                side = e.get("side", "Sell")
                qty = e.get("qty", 0)
                # Use Bybit pnl if provided (source of truth)
                if pnl_usdt is None:
                    if side == "Buy":
                        pnl_pct = (exit_price - entry_price) / entry_price * 100
                    else:
                        pnl_pct = (entry_price - exit_price) / entry_price * 100
                    pnl_usdt = round(pnl_pct / 100 * entry_price * qty, 4)
                else:
                    pnl_usdt = round(pnl_usdt, 4)
                entry_p = entry_price if entry_price > 0 else 1
                pnl_pct = round(pnl_usdt / (entry_p * qty) * 100, 3) if qty > 0 else 0
                t_open = e.get("timestamp_open", time.time())
                duration = round((time.time() - t_open) / 60, 1)
                if result == "UNKNOWN":
                    result = "WIN" if pnl_pct > 0 else "LOSS"
                t_close = time.time()
                t_open2 = e.get("timestamp_open", t_close)
                dur_min = round((t_close - t_open2) / 60, 1)
                entries[i].update({
                    "status":         "CLOSED",
                    "exit_price":     exit_price,
                    "pnl_pct":        round(pnl_pct, 3),
                    "pnl_usdt":       pnl_usdt,
                    "result":         result,
                    "timestamp_close": t_close,
                    "duration_min":   dur_min
                })
                _save(entries)
                log.info("JOURNAL CLOSED: %s result=%s pnl=%.2f%%", 
                         e.get("symbol"), result, pnl_pct)
                return True
        return False
    except Exception as e:
        log.error("Journal close error: %s", e)
        return False
