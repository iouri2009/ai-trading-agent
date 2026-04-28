"""
pnl_enricher.py
Enriches journal_trades.json with realized PnL from Bybit /v5/position/closed-pnl
Matching: orderLinkId (primary) → timestamp proximity (secondary)
"""
import json, os, asyncio, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from executor import _get

BASE        = os.path.dirname(os.path.abspath(__file__))
TRADES_PATH = os.path.join(BASE, "journal_trades.json")

async def fetch_closed_pnl():
    resp = await _get("/v5/position/closed-pnl", {
        "category":  "linear",
        "limit":     "100",
    })
    return resp.get("result", {}).get("list", [])

async def enrich():
    trades = json.load(open(TRADES_PATH))
    bybit  = await fetch_closed_pnl()

    print(f"Trades to enrich: {len(trades)}")
    print(f"Bybit closed-pnl records: {len(bybit)}")

    # Build lookup by orderLinkId
    link_map = {}
    for b in bybit:
        lid = b.get("orderLinkId") or b.get("order_link_id") or ""
        if lid:
            link_map[lid] = b

    matched_link = 0
    matched_time = 0
    unmatched    = 0

    for t in trades:
        matched = False

        # A — primary: orderLinkId
        link = t.get("order_link_id", "")
        if link and link in link_map:
            b = link_map[link]
            t["realized_pnl"] = float(b.get("closedPnl", 0) or 0)
            t["pnl_usdt"]     = t["realized_pnl"]
            t["net_pnl_usdt"] = round(t["realized_pnl"] - t.get("fee_usdt", 0), 6)
            t["pnl_source"]   = "bybit"
            t["bybit_avg_entry"] = float(b.get("avgEntryPrice", 0) or 0)
            t["bybit_avg_exit"]  = float(b.get("avgExitPrice", 0) or 0)
            matched_link += 1
            matched = True
            print(f"[LINK MATCH] {t['symbol']} link={link[:25]} pnl={t['realized_pnl']:.4f}")

        # B — secondary: sum all Bybit records within lifecycle time window
        if not matched:
            open_ts_s  = t["open_timestamp"] / 1000
            close_ts_s = t["close_timestamp"] / 1000
            sym  = t["symbol"]
            side = "Sell" if t["side"] == "LONG" else "Buy"
            # Find all Bybit records for this symbol+side within lifecycle window (+/-120s)
            window_records = [
                b for b in bybit
                if b.get("symbol") == sym
                and b.get("side") == side
                and open_ts_s - 120 <= int(b.get("updatedTime",0))/1000 <= close_ts_s + 120
            ]
            if window_records:
                total_bybit_pnl = sum(float(b.get("closedPnl",0)) for b in window_records)
                t["realized_pnl"] = round(total_bybit_pnl, 8)
                t["pnl_usdt"]     = t["realized_pnl"]
                t["net_pnl_usdt"] = round(t["realized_pnl"] - t.get("fee_usdt", 0), 6)
                t["pnl_source"]   = "bybit_ts"
                t["bybit_records"] = len(window_records)
                t["bybit_avg_entry"] = float(window_records[0].get("avgEntryPrice", 0) or 0)
                t["bybit_avg_exit"]  = float(window_records[-1].get("avgExitPrice", 0) or 0)
                matched_time += 1
                matched = True
                print(f"[WINDOW MATCH] {sym} records={len(window_records)} sum_pnl={total_bybit_pnl:.4f}")

        if not matched:
            unmatched += 1
            print(f"[UNMATCHED] {t['symbol']} {t['side']} close={t['close_timestamp']} qty={t['total_qty']}")

    json.dump(trades, open(TRADES_PATH, "w"), indent=2)

    print(f"\n=== ENRICHMENT SUMMARY ===")
    print(f"total_trades:     {len(trades)}")
    print(f"matched_by_link:  {matched_link}")
    print(f"matched_by_time:  {matched_time}")
    print(f"unmatched:        {unmatched}")
    total = sum(t.get("pnl_usdt", 0) for t in trades)
    bybit_total = sum(t.get("pnl_usdt", 0) for t in trades if t.get("pnl_source") in ("bybit","bybit_ts"))
    print(f"total_pnl:        ${total:.4f}")
    print(f"bybit_pnl_total:  ${bybit_total:.4f}")

asyncio.run(enrich())
