"""
reclaim_monitor.py
5M reclaim trigger engine — internal only, not user-facing.
Reads from level_cache, monitors 5M for reclaim of 15M sweep levels.
Does NOT generate independent signals. Does NOT scan market.
"""
import asyncio
import logging
import time

log = logging.getLogger("reclaim_monitor")

_running = False
_task    = None


async def _check_reclaim(symbol: str, level_data: dict, notify_fn, executor_fn):
    """Check single symbol for 5M reclaim of cached 15M level."""
    try:
        from agent import get_kline
        import pandas as _pd

        level     = level_data["level"]
        direction = level_data["direction"]
        atr       = level_data["atr"]

        df5m = get_kline(symbol, "5")
        if df5m is None or len(df5m) < 10:
            return

        closes = df5m["close"].astype(float)
        opens  = df5m["open"].astype(float)
        highs  = df5m["high"].astype(float)
        lows   = df5m["low"].astype(float)
        vols   = df5m["volume"].astype(float)

        # Use closed candle only — df5m.iloc[-2]
        c_close = float(closes.iloc[-2])
        c_open  = float(opens.iloc[-2])
        c_vol   = float(vols.iloc[-2])
        avg_vol = float(vols.iloc[-20:-2].mean())
        vol_ratio = c_vol / avg_vol if avg_vol > 0 else 0.0

        # Reclaim condition
        reclaimed_long  = direction == "LONG"  and c_close > level
        reclaimed_short = direction == "SHORT" and c_close < level

        if not reclaimed_long and not reclaimed_short:
            return

        # Volume filter
        if vol_ratio < 1.2:
            log.debug("5M reclaim blocked — low volume: %s %.2fx", symbol, vol_ratio)
            return

        # Late entry filter — entry must be within 0.8 ATR of level
        entry = c_close
        dist  = abs(entry - level)
        if dist > 0.8 * atr:
            log.debug("5M reclaim blocked — late entry: %s dist=%.6f max=%.6f",
                      symbol, dist, 0.8 * atr)
            return

        # SL anchored to 15M sweep level (not 5M candle)
        buf = atr * 0.15
        sl  = (level - buf) if direction == "LONG" else (level + buf)

        # TP structure — same R as sweep signals
        risk = abs(entry - sl)
        if risk <= 0:
            return

        tp1 = entry + 1.2 * risk if direction == "LONG" else entry - 1.2 * risk
        tp2 = entry + 2.0 * risk if direction == "LONG" else entry - 2.0 * risk
        tp3 = entry + 3.0 * risk if direction == "LONG" else entry - 3.0 * risk

        signal_dict = {
            "symbol":      symbol,
            "direction":   direction,
            "confidence":  "MEDIUM",   # 5M reclaim = MEDIUM by default
            "setup_class": "MEDIUM",
            "price":       entry,
            "sl":          sl,
            "tp1":         tp1,
            "tp2":         tp2,
            "tp3":         tp3,
            "source":      "5M_RECLAIM",
        }

        # Format signal message
        side_emoji = "🟢" if direction == "LONG" else "🔴"
        r_pct = round(risk / entry * 100, 3)
        signal_msg = (
            f"────────────────────────────\n"
            f"⚡️ WATCH ENTRY  _(5M Reclaim)_\n"
            f"COIN: *{symbol}*  |  {side_emoji} *{direction}*  |  SWEEP · RECLAIM\n"
            f"CONFIDENCE: MEDIUM\n"
            f"────────────────────────────\n"
            f"ENTRY:  `{round(entry, 6)}`\n"
            f"SL:     `{round(sl, 6)}`\n"
            f"────────────────────────────\n"
            f"TP1:  `{round(tp1, 6)}`  →  1.2R  _(\u2192 move SL to breakeven)_\n"
            f"TP2:  `{round(tp2, 6)}`  →  2.0R\n"
            f"TP3:  `{round(tp3, 6)}`  →  3.0R\n"
            f"────────────────────────────\n"
            f"R: 5M reclaim of 15M level | R={r_pct}%\n"
            f"────────────────────────────"
        )

        log.info("5M reclaim signal: %s %s @ %.6f", symbol, direction, entry)

        # Remove from cache — level consumed
        from level_cache import clear_level
        clear_level(symbol)

        # Notify + route to executor
        if notify_fn:
            await notify_fn(signal_msg)
        if executor_fn:
            await executor_fn(signal_dict, time.time())

    except Exception as e:
        log.error("Reclaim check error %s: %s", symbol, e)


async def _reclaim_loop(notify_fn, executor_fn):
    """Internal 5M reclaim loop. Runs every 5M candle close."""
    global _running
    from app import seconds_until_next_candle

    log.info("5M reclaim monitor started")
    while _running:
        try:
            # Sleep until next 5M candle close + 10s
            sleep_secs = seconds_until_next_candle(interval_minutes=5, offset_seconds=10)
            await asyncio.sleep(sleep_secs)

            if not _running:
                break

            from level_cache import get_all_active
            active_levels = get_all_active()

            if not active_levels:
                continue

            log.debug("5M trigger: checking %d cached levels", len(active_levels))

            # Check all cached levels concurrently
            tasks = [
                _check_reclaim(sym, ldata, notify_fn, executor_fn)
                for sym, ldata in active_levels.items()
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Reclaim loop error: %s", e)
            await asyncio.sleep(30)

    log.info("5M reclaim monitor stopped")


def start(notify_fn, executor_fn):
    """Start the 5M reclaim monitor. Called when Loop 15m starts."""
    global _running, _task
    if _running:
        return
    _running = True
    _task = asyncio.create_task(_reclaim_loop(notify_fn, executor_fn))
    log.info("Reclaim monitor task created")


def stop():
    """Stop the 5M reclaim monitor. Called when Loop 15m stops."""
    global _running, _task
    _running = False
    if _task and not _task.done():
        _task.cancel()
        _task = None
    from level_cache import clear_all
    clear_all()
    log.info("Reclaim monitor stopped, cache cleared")


def is_running():
    return _running
