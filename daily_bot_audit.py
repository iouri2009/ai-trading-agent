#!/usr/bin/env python3
"""
TTM369 Daily Bot Audit

Read-only audit script.
Reads journal/log files and creates a daily report.
Does NOT change trading logic.
Does NOT place orders.
Does NOT modify journal files.
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPORT_DIR = BASE / "data" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

JOURNAL = BASE / "journal.json"
TRADES = BASE / "journal_trades.json"
FILLS = BASE / "journal_fills.json"
BOT_LOG = BASE / "bot.log"

AEST = timezone(timedelta(hours=10))


def load_json(path, default):
    try:
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text())
    except Exception as e:
        return {"_error": str(e)}
    return default


def parse_ts(value):
    if value is None:
        return None
    try:
        v = float(value)
        if v > 10_000_000_000:
            v = v / 1000.0
        return datetime.fromtimestamp(v, tz=AEST)
    except Exception:
        return None


def in_last_24h(ts):
    if not ts:
        return False
    now = datetime.now(AEST)
    return ts >= now - timedelta(hours=24)


def read_tail(path, max_bytes=2_000_000):
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def main():
    now = datetime.now(AEST)
    stamp = now.strftime("%Y%m%d_%H%M%S")

    journal = load_json(JOURNAL, [])
    trades = load_json(TRADES, [])
    fills = load_json(FILLS, [])
    log_tail = read_tail(BOT_LOG)

    if isinstance(journal, dict) and "_error" in journal:
        journal_error = journal["_error"]
        journal = []
    else:
        journal_error = None

    # Journal blocked stats
    blocked_24h = []
    modes = Counter()
    blockers = Counter()
    symbols_blocked = Counter()
    conf_counter = Counter()

    if isinstance(journal, list):
        for row in journal:
            ts = parse_ts(row.get("timestamp_open") or row.get("timestamp") or row.get("time"))
            if row.get("status") == "BLOCKED" and in_last_24h(ts):
                blocked_24h.append(row)
                blockers[row.get("block_category") or row.get("reason") or "UNKNOWN"] += 1
                symbols_blocked[row.get("symbol") or "UNKNOWN"] += 1
                modes[row.get("user_mode") or row.get("filter_mode") or "UNKNOWN"] += 1
                conf_counter[row.get("confidence") or "UNKNOWN"] += 1

    # Trade stats
    closed_24h = []
    pnl_total = 0.0
    wins = 0
    losses = 0

    if isinstance(trades, list):
        for row in trades:
            ts = parse_ts(row.get("timestamp_close") or row.get("closed_at") or row.get("updatedTime") or row.get("timestamp"))
            if in_last_24h(ts):
                closed_24h.append(row)
                pnl = float(row.get("pnl_usdt") or row.get("pnl") or 0.0)
                pnl_total += pnl
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1

    # Fills stats
    fills_24h = []
    if isinstance(fills, list):
        for row in fills:
            ts = parse_ts(row.get("timestamp") or row.get("execTime") or row.get("updatedTime"))
            if in_last_24h(ts):
                fills_24h.append(row)

    # Log stats from tail
    scan_starts = re.findall(r"\[SCAN START\]", log_tail)
    scan_complete = re.findall(r"Scan complete", log_tail)
    checked_vals = [int(x) for x in re.findall(r"Checked:\s*(\d+)", log_tail)]
    valid_vals = [int(x) for x in re.findall(r"Valid:\s*(\d+)", log_tail)]
    leverage_errors = re.findall(r"\[LEVERAGE ERROR\]\s*([A-Z0-9]+USDT)", log_tail)
    lifecycle_errors = re.findall(r"\[LIFECYCLE ERROR\]", log_tail)
    fill_sync_errors = re.findall(r"\[FILL SYNC ERROR\]", log_tail)
    invalid_tp1 = re.findall(r"\[INVALID_TP1_R\]\s*([A-Z0-9]+USDT).*?rr=([0-9.]+)", log_tail)
    sl_too_tight = re.findall(r"\[BLOCK\] SL too tight:\s*([A-Z0-9]+USDT).*?sl_dist=([0-9.]+)%", log_tail)

    avg_checked = round(sum(checked_vals) / len(checked_vals), 1) if checked_vals else 0
    total_valid = sum(valid_vals) if valid_vals else 0

    report = {
        "generated_at_aest": now.isoformat(),
        "period": "last_24h_from_available_journal_and_recent_log_tail",
        "files": {
            "journal_exists": JOURNAL.exists(),
            "trades_exists": TRADES.exists(),
            "fills_exists": FILLS.exists(),
            "bot_log_exists": BOT_LOG.exists(),
            "journal_error": journal_error,
        },
        "scan_stats_from_log_tail": {
            "scan_starts_in_tail": len(scan_starts),
            "scan_complete_in_tail": len(scan_complete),
            "avg_checked_symbols": avg_checked,
            "total_valid_signals_in_tail": total_valid,
        },
        "journal_blocked_24h": {
            "count": len(blocked_24h),
            "by_mode": dict(modes.most_common()),
            "top_blockers": dict(blockers.most_common(10)),
            "top_symbols": dict(symbols_blocked.most_common(10)),
            "by_confidence": dict(conf_counter.most_common()),
        },
        "closed_trades_24h": {
            "count": len(closed_24h),
            "pnl_usdt": round(pnl_total, 4),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / max(1, wins + losses) * 100, 1),
        },
        "fills_24h": {
            "count": len(fills_24h),
        },
        "errors_from_log_tail": {
            "leverage_errors": Counter(leverage_errors).most_common(10),
            "lifecycle_errors": len(lifecycle_errors),
            "fill_sync_errors": len(fill_sync_errors),
            "invalid_tp1_r": invalid_tp1[-10:],
            "sl_too_tight": sl_too_tight[-10:],
        },
    }

    lines = []
    lines.append("📊 TTM369 DAILY BOT AUDIT")
    lines.append("────────────────────────")
    lines.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S AEST')}")
    lines.append("")
    lines.append("SYSTEM / SCAN")
    lines.append(f"- Scan starts in log tail: {len(scan_starts)}")
    lines.append(f"- Scan complete in log tail: {len(scan_complete)}")
    lines.append(f"- Avg checked symbols: {avg_checked}")
    lines.append(f"- Total valid signals in log tail: {total_valid}")
    lines.append("")
    lines.append("BLOCKED SIGNALS — LAST 24H")
    lines.append(f"- Blocked entries: {len(blocked_24h)}")
    lines.append(f"- By mode: {dict(modes.most_common())}")
    lines.append("- Top blockers:")
    if blockers:
        for k, v in blockers.most_common(10):
            lines.append(f"  {v} × {k}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append("TRADES — LAST 24H")
    lines.append(f"- Closed trades: {len(closed_24h)}")
    lines.append(f"- PnL: {round(pnl_total, 4)} USDT")
    lines.append(f"- Wins/Losses: {wins}/{losses}")
    lines.append("")
    lines.append("ERRORS / EXECUTION")
    lines.append(f"- Lifecycle errors in log tail: {len(lifecycle_errors)}")
    lines.append(f"- Fill sync errors in log tail: {len(fill_sync_errors)}")
    lines.append(f"- Leverage error symbols: {Counter(leverage_errors).most_common(10)}")
    lines.append(f"- Recent INVALID_TP1_R: {invalid_tp1[-5:]}")
    lines.append(f"- Recent SL too tight: {sl_too_tight[-5:]}")
    lines.append("")
    lines.append("AI TAKEAWAY")
    if len(blocked_24h) == 0 and len(scan_starts) > 0:
        lines.append("- Bot appears to be scanning, but journal has limited recent blocked data.")
    elif blockers:
        top = blockers.most_common(1)[0][0]
        lines.append(f"- Main blocker currently appears to be: {top}")
    else:
        lines.append("- Not enough data for a reliable recommendation.")
    lines.append("- No automatic strategy changes recommended from this audit alone.")
    lines.append("- Use this report for controlled experiments only.")

    txt_path = REPORT_DIR / f"daily_audit_{stamp}.txt"
    json_path = REPORT_DIR / f"daily_audit_{stamp}.json"

    txt_path.write_text("\n".join(lines))
    json_path.write_text(json.dumps(report, indent=2))

    print("OK — audit created")
    print(txt_path)
    print(json_path)


if __name__ == "__main__":
    main()
