"""
trades_view.py
Active trades display for Telegram menu.
Reads from trade_db, fetches current prices.
"""
import logging
import time

log = logging.getLogger("trades_view")


async def format_active_trades() -> str:
    """Format active trades for Telegram display."""
    try:
        # Sync with Bybit first
        try:
            from executor import sync_open_trades
            await sync_open_trades()
        except Exception as _se:
            pass
        # Fetch DIRECTLY from Bybit
        from executor import _get
        import asyncio as _aio
        _pos_data = await _get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        _bybit_positions = [p for p in _pos_data.get("result", {}).get("list", []) if float(p.get("size", 0)) > 0]
        # Convert Bybit positions to trade-like dicts
        from trade_db import get_open_trades
        _db_trades = {t["symbol"]: t for t in get_open_trades()}
        trades = []
        for p in _bybit_positions:
            sym = p.get("symbol")
            db = _db_trades.get(sym, {})
            trades.append({
                "symbol": sym,
                "side": p.get("side", "Sell"),
                "actual_fill": float(p.get("avgPrice", 0)),
                "intended_entry": float(p.get("avgPrice", 0)),
                "sl": db.get("sl", 0),
                "tp1": db.get("tp1", 0),
                "tp2": db.get("tp2", 0),
                "tp3": db.get("tp3", 0),
                "qty": float(p.get("size", 0)),
                "filled_qty": float(p.get("size", 0)),
                "status": "OPEN",
                "confidence": db.get("confidence", "—"),
                "created_at": db.get("created_at", 0),
                "tp1_hit": 0, "tp2_hit": 0, "tp3_hit": 0, "breakeven_moved": 0,
                "unrealised_pnl": float(p.get("unrealisedPnl", 0)),
            })

        if not trades:
            return (
                "💰 *Active Trades*\n"
                "─────────────────────\n"
                "No active trades.\n\n"
                "_Trades will appear here when AutoTrade executes._"
            )

        lines = ["💰 *Active Trades*\n─────────────────────"]

        for t in trades:
            symbol    = t.get("symbol", "?")
            side      = t.get("side", "?")
            entry     = t.get("actual_fill") or t.get("intended_entry", 0)
            sl        = t.get("sl", 0)
            tp1       = t.get("tp1", 0)
            tp2       = t.get("tp2", 0)
            tp3       = t.get("tp3", 0)
            tp1_hit   = t.get("tp1_hit", 0)
            tp2_hit   = t.get("tp2_hit", 0)
            tp3_hit   = t.get("tp3_hit", 0)
            be_moved  = t.get("breakeven_moved", 0)
            status    = t.get("status", "OPEN")
            created   = t.get("created_at", 0)
            confidence= t.get("confidence", "—")

            # Current price
            current_price = await _get_current_price(symbol)

            # PnL
            pnl_pct = 0.0
            if current_price and entry:
                if side == "Buy":
                    pnl_pct = (current_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - current_price) / entry * 100

            pnl_sign  = "+" if pnl_pct >= 0 else ""
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            side_label = "LONG" if side == "Buy" else "SHORT"
            side_emoji = "🟢" if side == "Buy" else "🔴"

            # Duration
            age_min = int((time.time() - created) / 60) if created else 0
            age_str = f"{age_min}m" if age_min < 60 else f"{age_min//60}h {age_min%60}m"

            # TP status
            tp_status = []
            if tp1_hit: tp_status.append("TP1 ✅")
            if tp2_hit: tp_status.append("TP2 ✅")
            if tp3_hit: tp_status.append("TP3 ✅")
            if be_moved and not tp_status: tp_status.append("BE 🔒")
            tp_str = "  ".join(tp_status) if tp_status else "Waiting..."

            lines.append(
                f"\n*{symbol}*  {side_emoji} {side_label}\n"
                f"Entry:   `{entry}`\n"
                f"Now:     `{current_price or '—'}`  {pnl_emoji} {pnl_sign}{pnl_pct:.2f}%\n"
                f"SL:      `{sl}`\n"
                f"TP1/2/3: `{tp1}` / `{tp2}` / `{tp3}`\n"
                f"Status:  {tp_str}\n"
                f"Conf:    {confidence}  ⏱ {age_str}\n"
                f"State:   {status}"
            )
            lines.append("─────────────────────")

        lines.append(f"\n_Updated: {time.strftime('%H:%M:%S')}_")
        return "\n".join(lines)

    except Exception as e:
        log.error("format_active_trades error: %s", e)
        return f"💰 *Active Trades*\n\nError loading trades: {e}"


async def _get_current_price(symbol: str):
    """Fetch latest price for symbol."""
    try:
        from agent import get_kline
        df = get_kline(symbol, "1")
        if df is not None and len(df) > 0:
            return float(df["close"].astype(float).iloc[-1])
    except Exception as e:
        log.debug("Price fetch error for %s: %s", symbol, e)
    return None
