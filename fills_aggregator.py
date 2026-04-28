"""
fills_aggregator.py
Reconstructs trades from raw execution fills.
Input:  journal_fills.json
Output: journal_trades.json
"""
import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
FILLS_PATH  = os.path.join(BASE, "journal_fills.json")
TRADES_PATH = os.path.join(BASE, "journal_trades.json")


def load_fills():
    with open(FILLS_PATH) as f:
        fills = json.load(f)
    trade_fills = [f for f in fills if f.get("exec_type") == "Trade"]
    trade_fills.sort(key=lambda x: int(x.get("timestamp", 0)))
    return trade_fills


def weighted_avg(fills, price_key, qty_key):
    total_val = sum(f[price_key] * f[qty_key] for f in fills)
    total_qty = sum(f[qty_key] for f in fills)
    return round(total_val / total_qty, 8) if total_qty > 0 else 0.0


def run_aggregator():
    fills = load_fills()
    symbols = sorted({f["symbol"] for f in fills})

    trades = []
    fills_processed = 0
    orphans_skipped = 0
    open_lifecycles = 0

    for sym in symbols:
        sf = [f for f in fills if f["symbol"] == sym]

        net_qty    = 0.0
        lc_id      = 1
        lc_fills   = []
        lc_entry   = []
        lc_close   = []
        lc_start_ts = None

        for f in sf:
            ts       = int(f["timestamp"])
            side     = f["side"]
            qty      = f["exec_qty"]
            is_close = f.get("is_closing", False)

            # Orphan: closing fill when position is flat
            if is_close and abs(net_qty) < 0.001:
                orphans_skipped += 1
                continue

            fills_processed += 1
            lc_fills.append(f)

            if not is_close:
                lc_entry.append(f)
                if lc_start_ts is None:
                    lc_start_ts = ts
                net_qty += qty if side == "Buy" else -qty
            else:
                lc_close.append(f)
                net_qty += qty if side == "Buy" else -qty

            net_qty = round(net_qty, 8)

            # Lifecycle complete
            if abs(net_qty) < 0.001 and lc_start_ts is not None:
                # Determine position side from first entry fill
                first_entry = lc_entry[0] if lc_entry else None
                if first_entry:
                    pos_side = "LONG" if first_entry["side"] == "Buy" else "SHORT"
                else:
                    pos_side = "UNKNOWN"

                entry_price = weighted_avg(lc_entry, "exec_price", "exec_qty")
                exit_price  = weighted_avg(lc_close, "exec_price", "exec_qty")
                total_qty   = sum(f["exec_qty"] for f in lc_entry)
                total_fee   = round(sum(f.get("fee", 0) for f in lc_fills), 8)
                close_ts    = ts
                duration    = round((close_ts - lc_start_ts) / 60000, 1)

                # Calculate PnL from prices
                if pos_side == "LONG":
                    raw_pnl = (exit_price - entry_price) * total_qty
                else:
                    raw_pnl = (entry_price - exit_price) * total_qty
                pnl = round(raw_pnl, 6)
                net_pnl = round(pnl - total_fee, 6)

                trade = {
                    "trade_id":       f"lc_{sym}_{lc_start_ts}",
                    "lifecycle_id":   lc_id,
                    "symbol":         sym,
                    "side":           pos_side,
                    "entry_price":    entry_price,
                    "exit_price":     exit_price,
                    "total_qty":      total_qty,
                    "exec_ids":       [f["exec_id"] for f in lc_fills],
                    "order_link_id":  next((f["order_link_id"] for f in lc_entry if f.get("order_link_id")), ""),
                    "open_timestamp": lc_start_ts,
                    "close_timestamp": close_ts,
                    "duration_min":   duration,
                    "pnl_usdt":       pnl,
                    "net_pnl_usdt":   net_pnl,
                    "fee_usdt":       total_fee,
                    "pnl_source":     "calculated",
                    "entry_fills":    len(lc_entry),
                    "close_fills":    len(lc_close),
                    "total_fills":    len(lc_fills),
                    "is_complete":    True,
                    "grouping":       "position_lifecycle",
                }
                trades.append(trade)

                lc_id += 1
                lc_fills = []; lc_entry = []; lc_close = []
                lc_start_ts = None
                net_qty = 0.0

        # Open lifecycle remaining
        if abs(net_qty) > 0.001 and lc_start_ts:
            open_lifecycles += 1

    with open(TRADES_PATH, "w") as f:
        json.dump(trades, f, indent=2)

    print(f"=== AGGREGATOR COMPLETE ===")
    print(f"fills_processed:  {fills_processed}")
    print(f"trades_created:   {len(trades)}")
    print(f"open_lifecycles:  {open_lifecycles}")
    print(f"orphans_skipped:  {orphans_skipped}")

    if trades:
        print(f"\n=== SAMPLE TRADE ===")
        t = trades[-1]
        for k, v in t.items():
            if k != "exec_ids":
                print(f"  {k}: {v}")
        print(f"  exec_ids: [{t['exec_ids'][0][:8]}...] ({len(t['exec_ids'])} fills)")

    return trades


if __name__ == "__main__":
    run_aggregator()
