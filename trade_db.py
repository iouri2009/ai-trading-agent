"""
trade_db.py
SQLite state management for live execution.
Tracks every trade lifecycle: signal → order → fill → TP → close.
"""
import sqlite3
import time
import logging
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "trades.db")
log = logging.getLogger("trade_db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       TEXT UNIQUE NOT NULL,
            symbol          TEXT NOT NULL,
            side            TEXT NOT NULL,
            setup_class     TEXT,
            confidence      TEXT,
            order_link_id   TEXT UNIQUE,
            order_id        TEXT,
            intended_entry  REAL,
            actual_fill     REAL,
            sl              REAL,
            tp1             REAL,
            tp2             REAL,
            tp3             REAL,
            qty             REAL,
            remaining_qty   REAL,
            tp1_hit         INTEGER DEFAULT 0,
            tp2_hit         INTEGER DEFAULT 0,
            tp3_hit         INTEGER DEFAULT 0,
            breakeven_moved INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'PENDING',
            created_at      REAL,
            updated_at      REAL,
            notes           TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info("DB initialized at %s", DB_PATH)


def insert_trade(signal_id, symbol, side, setup_class, confidence,
                 order_link_id, intended_entry, sl, tp1, tp2, tp3, qty,
                 path=None, t4h=None, t1h=None, hour_utc=None,
                 session=None, signal_score=None, fill_ts=None,
                 regime=None, macro_state=None,
                 equity_at_entry=None, actual_leverage=None, volume_ratio=None):
    conn = get_conn()
    now = time.time()
    try:
        conn.execute("""
            INSERT INTO trades
            (signal_id, symbol, side, setup_class, confidence,
             order_link_id, intended_entry, sl, tp1, tp2, tp3,
             qty, remaining_qty, status, created_at, updated_at,
             path, t4h, t1h, hour_utc, session, signal_score, fill_ts,
             regime, macro_state, equity_at_entry, actual_leverage, volume_ratio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (signal_id, symbol, side, setup_class, confidence,
              order_link_id, intended_entry, sl, tp1, tp2, tp3,
              qty, qty, "PENDING", now, now,
              path, t4h, t1h, hour_utc, session, signal_score, fill_ts,
              regime, macro_state, equity_at_entry, actual_leverage, volume_ratio))
        conn.commit()
        log.info("Inserted trade %s %s %s", signal_id, symbol, side)
    except sqlite3.IntegrityError:
        log.warning("Duplicate signal_id or order_link_id: %s", signal_id)
    finally:
        conn.close()


def update_fill(order_link_id, order_id, actual_fill, status="OPEN"):
    conn = get_conn()
    conn.execute("""
        UPDATE trades SET order_id=?, actual_fill=?, status=?, updated_at=?
        WHERE order_link_id=?
    """, (order_id, actual_fill, status, time.time(), order_link_id))
    conn.commit()
    conn.close()


def update_tp_hit(order_link_id, tp_num, remaining_qty, breakeven_moved=False):
    conn = get_conn()
    field = f"tp{tp_num}_hit"
    conn.execute(f"""
        UPDATE trades SET {field}=1, remaining_qty=?,
        breakeven_moved=CASE WHEN ? THEN 1 ELSE breakeven_moved END,
        updated_at=? WHERE order_link_id=?
    """, (remaining_qty, int(breakeven_moved), time.time(), order_link_id))
    conn.commit()
    conn.close()


def update_status(order_link_id, status, notes=None):
    conn = get_conn()
    conn.execute("""
        UPDATE trades SET status=?, notes=?, updated_at=?
        WHERE order_link_id=?
    """, (status, notes, time.time(), order_link_id))
    conn.commit()
    conn.close()


def get_open_trades():
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM trades WHERE status IN ('PENDING','OPEN')
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trade_by_link_id(order_link_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM trades WHERE order_link_id=?
    """, (order_link_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_trade_by_symbol(symbol):
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM trades WHERE symbol=? AND status IN ('PENDING','OPEN')
        ORDER BY created_at DESC LIMIT 1
    """, (symbol,)).fetchone()
    conn.close()
    return dict(row) if row else None


def signal_already_processed(signal_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT id FROM trades WHERE signal_id=? AND status NOT IN ('REJECTED','ERROR','CANCELLED')
    """, (signal_id,)).fetchone()
    conn.close()
    return row is not None






def get_drawdown_state(initial_equity=320.0):
    """Compute drawdown metrics from journal equity curve."""
    import json, os as _os, time as _t
    _jpath = _os.path.join(_os.path.dirname(__file__), 'journal.json')
    try:
        with open(_jpath) as _f:
            trades = json.load(_f)
    except Exception:
        return None
    closed = sorted(
        [t for t in trades if t.get('status')=='CLOSED'
         and t.get('is_valid_for_stats', True)
         and t.get('net_pnl_usdt') is not None],
        key=lambda x: float(x.get('timestamp_close') or x.get('timestamp_open') or 0)
    )
    if not closed:
        return {
            'current_equity': initial_equity,
            'peak_equity': initial_equity,
            'current_dd_usdt': 0.0,
            'current_dd_pct': 0.0,
            'max_dd_usdt': 0.0,
            'max_dd_pct': 0.0,
            'drawdown_duration_trades': 0,
            'drawdown_phase': 'PEAK',
            'recovery_factor': 0.0,
            'system_state': 'NORMAL',
        }
    equity = initial_equity
    peak = initial_equity
    max_dd_usdt = 0.0
    max_dd_pct = 0.0
    dd_duration = 0
    dd_start = None
    for t in closed:
        pnl = float(t.get('net_pnl_usdt') or t.get('pnl_usdt') or 0)
        equity += pnl
        if equity >= peak:
            peak = equity
            dd_duration = 0
            dd_start = None
        else:
            dd = peak - equity
            dd_pct = dd / peak if peak > 0 else 0
            dd_duration += 1
            if dd > max_dd_usdt:
                max_dd_usdt = dd
                max_dd_pct = dd_pct
    current_dd_usdt = max(0.0, peak - equity)
    current_dd_pct = current_dd_usdt / peak if peak > 0 else 0.0
    total_pnl = equity - initial_equity
    recovery_factor = round(total_pnl / max_dd_usdt, 3) if max_dd_usdt > 0 else 0.0
    if current_dd_pct < 0.001:
        phase = 'PEAK'
    elif equity < peak:
        phase = 'DRAWDOWN'
    else:
        phase = 'RECOVERY'
    if current_dd_pct >= 0.10:
        state = 'CRITICAL'
    elif current_dd_pct >= 0.05:
        state = 'WARNING'
    else:
        state = 'NORMAL'
    return {
        'current_equity':          round(equity, 2),
        'peak_equity':             round(peak, 2),
        'current_dd_usdt':         round(current_dd_usdt, 2),
        'current_dd_pct':          round(current_dd_pct * 100, 2),
        'max_dd_usdt':             round(max_dd_usdt, 2),
        'max_dd_pct':              round(max_dd_pct * 100, 2),
        'drawdown_duration_trades':dd_duration,
        'drawdown_phase':          phase,
        'recovery_factor':         recovery_factor,
        'system_state':            state,
    }

def get_edge_stats(last_n=50):
    """Calculate edge metrics for last N closed trades."""
    import statistics
    conn = get_conn()
    conn.close()
    import json as _jj, os as _oj
    _jpath = _oj.path.join(_oj.path.dirname(__file__), 'journal.json')
    try:
        with open(_jpath) as _f: trades = _jj.load(_f)
    except Exception:
        return None
    closed = sorted(
        [t for t in trades if t.get('status')=='CLOSED'
         and t.get('r_multiple') is not None
         and abs(float(t.get('r_multiple') or 0)) <= 20
         and t.get('is_valid_for_stats', True)
         and float(t.get('sl_pct') or 0) >= 0.004],
        key=lambda x: float(x.get('timestamp_close') or x.get('timestamp_open') or 0),
        reverse=True
    )[:last_n]
    if not closed:
        return None
    pnls  = [float(t.get('pnl_usdt') or 0) for t in closed]
    rs    = [float(t.get('r_multiple') or 0) for t in closed]
    fees  = [float(t.get('fee_usdt') or 0) for t in closed]
    exps  = [float(t.get('expectancy_contribution') or 0) for t in closed]
    wins  = [p for p in pnls if p > 0]
    total = len(pnls)
    return {
        'total_trades':          total,
        'win_rate':              round(len(wins)/total*100, 1) if total else 0,
        'avg_r':                 round(sum(rs)/total, 3) if total else 0,
        'median_r':              round(statistics.median(rs), 3) if rs else 0,
        'avg_expectancy_usdt':   round(sum(exps)/total, 4) if total else 0,
        'median_expectancy_usdt':round(statistics.median(exps), 4) if exps else 0,
        'total_fees':            round(sum(fees), 4),
        'expectancy_after_fees': round((sum(exps)-sum(fees))/total, 4) if total else 0,
    }

def count_open_trades():
    conn = get_conn()
    n = conn.execute("""
        SELECT COUNT(*) FROM trades WHERE status IN ('PENDING','OPEN')
    """).fetchone()[0]
    conn.close()
    return n
