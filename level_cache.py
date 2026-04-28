"""
level_cache.py
Short-lived cache for 15M sweep levels detected by scan engine.
5M trigger engine reads from here — no independent signal generation.
"""
import time
import threading
import logging

log = logging.getLogger("level_cache")

# TTL = 3 x 15M candles = 45 minutes
LEVEL_TTL_SECONDS = 45 * 60

_cache = {}          # {symbol: level_data}
_lock  = threading.Lock()


def store_level(symbol: str, level: float, direction: str, atr: float):
    """Store a sweep level for 5M reclaim monitoring."""
    with _lock:
        _cache[symbol] = {
            "symbol":    symbol,
            "level":     level,
            "direction": direction,
            "atr":       atr,
            "stored_at": time.time(),
            "expiry":    time.time() + LEVEL_TTL_SECONDS,
        }
    log.debug("Level cached: %s %s @ %.6f (TTL 45m)", symbol, direction, level)


def get_level(symbol: str):
    """Get active level for symbol, or None if expired/missing."""
    with _lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        if time.time() > entry["expiry"]:
            del _cache[symbol]
            log.debug("Level expired: %s", symbol)
            return None
        return entry


def clear_level(symbol: str):
    """Remove level after successful reclaim or manual clear."""
    with _lock:
        _cache.pop(symbol, None)


def get_all_active():
    """Return all non-expired levels. Used by 5M trigger engine."""
    now = time.time()
    with _lock:
        expired = [s for s, v in _cache.items() if now > v["expiry"]]
        for s in expired:
            del _cache[s]
        return dict(_cache)


def clear_all():
    """Clear all levels. Called on loop stop."""
    with _lock:
        _cache.clear()
    log.info("Level cache cleared")


def count():
    """Active level count for status display."""
    now = time.time()
    with _lock:
        return sum(1 for v in _cache.values() if now <= v["expiry"])
