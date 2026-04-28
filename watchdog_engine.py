"""
watchdog_engine.py
Read-only system health monitor.
Checks fills, trades, PnL, execution safety, economics.
"""
import json, os, time, asyncio, sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
FILLS_PATH  = os.path.join(BASE, "journal_fills.json")
TRADES_PATH = os.path.join(BASE, "journal_trades.json")

def ts(ms): return datetime.fromtimestamp(ms/1000).strftime('%m-%d %H:%M')

def run_checks():
    issues   = []   # (severity, check, message)
    warnings = []
    ok       = []

    # ── Load data ────────────────────────────────────────────────
    try:
        fills  = json.load(open(FILLS_PATH))
        trades = json.load(open(TRADES_PATH))
    except Exception as e:
        print(f"[ENGINE STATUS] RED\n[CRITICAL] Cannot load data files: {e}")
        return "RED"

    trade_fills = [f for f in fills if f.get("exec_type") == "Trade"]
    closed      = [t for t in trades if t.get("is_complete")]

    # ── CHECK 1: Data integrity ───────────────────────────────────
    if len(trade_fills) == 0:
        issues.append(("CRITICAL", "DATA", "No trade fills in journal_fills.json"))
    else:
        ok.append(f"fills: {len(trade_fills)} trade fills stored")

    if len(trades) == 0:
        issues.append(("CRITICAL", "DATA", "No trades in journal_trades.json"))
    else:
        ok.append(f"trades: {len(trades)} aggregated trades")

    exec_ids = [f.get("exec_id") for f in fills if f.get("exec_id")]
    dupes = len(exec_ids) - len(set(exec_ids))
    if dupes > 0:
        issues.append(("CRITICAL", "DATA", f"Duplicate exec_ids: {dupes}"))
    else:
        ok.append("exec_id: no duplicates")

    # ── CHECK 2: Lifecycle validation ─────────────────────────────
    broken = []
    for t in closed:
        eq = t.get("entry_fills", 0)
        cq = t.get("close_fills", 0)
        if eq == 0:
            broken.append(t["symbol"])
    if broken:
        issues.append(("WARNING", "LIFECYCLE", f"Trades with no entry fills: {broken}"))
    else:
        ok.append(f"lifecycle: all {len(closed)} closed trades have entry fills")

    # ── CHECK 3: PnL validation ───────────────────────────────────
    unmatched = [t for t in closed if t.get("pnl_source") == "calculated"]
    if unmatched:
        warnings.append(("WARNING", "PNL", f"Trades with calculated PnL: {[t['symbol'] for t in unmatched]}"))
    else:
        ok.append(f"pnl_source: all {len(closed)} trades enriched from Bybit")

    total_pnl = sum(t.get("pnl_usdt", 0) for t in closed)
    ok.append(f"total_pnl: ${total_pnl:.4f}")

    # ── CHECK 4: Economic check ───────────────────────────────────
    fee_losers = []
    for t in closed:
        pnl = t.get("pnl_usdt", 0)
        fee = t.get("fee_usdt", 0)
        if fee > 0 and pnl < fee and pnl > 0:
            fee_losers.append(f"{t['symbol']} pnl={pnl:.4f} fee={fee:.4f}")
    if fee_losers:
        warnings.append(("WARNING", "ECONOMICS", f"Wins eaten by fees: {fee_losers}"))
    else:
        ok.append("economics: no fee-dominated wins")

    neg_net = [t for t in closed if t.get("net_pnl_usdt",0) < -0.5 and t.get("pnl_usdt",0) > 0]
    if neg_net:
        warnings.append(("WARNING", "ECONOMICS", f"Gross wins with net loss: {[t['symbol'] for t in neg_net]}"))

    # ── CHECK 5: Execution safety ─────────────────────────────────
    no_leverage = [t for t in trades if not t.get("actual_leverage")]
    # Check journal.json for SL/liq issues
    try:
        j = json.load(open(os.path.join(BASE, "journal.json")))
        sl_liq_issues = [
            x for x in j
            if x.get("status") == "OPEN"
            and x.get("sl") and x.get("liq_price")
            and float(x.get("sl",0)) >= float(x.get("liq_price",0))
        ]
        if sl_liq_issues:
            issues.append(("CRITICAL", "SAFETY", f"SL beyond liq: {[x['symbol'] for x in sl_liq_issues]}"))
        else:
            ok.append("SL safety: no SL-beyond-liq detected in open positions")
    except Exception: pass

    # ── CHECK 6: System state ─────────────────────────────────────
    now = time.time() * 1000
    if trade_fills:
        last_fill_ms = max(int(f.get("timestamp",0)) for f in trade_fills)
        age_min = (now - last_fill_ms) / 60000
        if age_min > 60:
            warnings.append(("WARNING", "STATE", f"No fills for {age_min:.0f} min (last: {ts(last_fill_ms)})"))
        else:
            ok.append(f"last_fill: {ts(last_fill_ms)} ({age_min:.0f} min ago)")

    if trades:
        last_trade_ms = max(int(t.get("close_timestamp",0)) for t in closed) if closed else 0
        if last_trade_ms:
            ok.append(f"last_trade: {ts(last_trade_ms)}")

    # Check bot.log for scan activity
    try:
        log_path = os.path.join(BASE, "bot.log")
        with open(log_path) as lf:
            lines = lf.readlines()
        scan_lines = [l for l in lines if "[SCAN START]" in l]
        if scan_lines:
            last_scan = scan_lines[-1].strip()
            ok.append(f"last_scan: {last_scan[-20:]}")
        fill_errors = [l for l in lines[-200:] if "[FILL SYNC ERROR]" in l]
        if fill_errors:
            warnings.append(("WARNING", "PIPELINE", f"Fill sync errors in last 200 log lines: {len(fill_errors)}"))
        else:
            ok.append("fill_sync: no errors in recent log")
    except Exception: pass

    # ── Determine overall status ──────────────────────────────────
    if issues:
        status = "RED"
    elif warnings:
        status = "YELLOW"
    else:
        status = "GREEN"

    # ── Print report ──────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"[ENGINE STATUS] {status}")
    print(f"{'='*50}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if issues:
        print(f"\n--- CRITICAL ---")
        for sev, check, msg in issues:
            print(f"  [{check}] {msg}")

    if warnings:
        print(f"\n--- WARNINGS ---")
        for sev, check, msg in warnings:
            print(f"  [{check}] {msg}")

    print(f"\n--- OK ---")
    for msg in ok:
        print(f"  {msg}")

    print(f"\nFills: {len(trade_fills)} | Trades: {len(trades)} | "
          f"Complete: {len(closed)} | PnL: ${total_pnl:.2f}")

    return status


async def run_with_telegram():
    sys.path.insert(0, BASE)
    status = run_checks()
    if status == "RED":
        try:
            from executor import _notify
            await _notify(f"ENGINE RED — critical issues detected. Check watchdog_engine.py output.")
        except Exception: pass
    return status


if __name__ == "__main__":
    asyncio.run(run_with_telegram())
