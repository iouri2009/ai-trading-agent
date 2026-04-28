"""
ws_manager.py
Bybit V5 private websocket manager.
Handles: order fills, position updates, TP detection, manual close detection.
Non-blocking — runs as background asyncio task inside app.py process.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("ws_manager")

# ── Config ────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

API_KEY    = _env("BYBIT_API_KEY")
API_SECRET = _env("BYBIT_API_SECRET")
ENV        = _env("BYBIT_ENV", "mainnet")

WS_URL = (
    "wss://stream.bybit.com/v5/private"
    if ENV == "mainnet"
    else "wss://stream-testnet.bybit.com/v5/private"
)

_telegram_notify    = None
_executor_on_tp_hit = None
_processed_exec_ids = set()  # dedup TP1 events
_executor_get_trade = None
_executor_update_status = None

_running  = False
_ws_task  = None


def set_callbacks(telegram_fn, tp_hit_fn, get_trade_fn, update_status_fn):
    global _telegram_notify, _executor_on_tp_hit
    global _executor_get_trade, _executor_update_status
    _telegram_notify        = telegram_fn
    _executor_on_tp_hit     = tp_hit_fn
    _executor_get_trade     = get_trade_fn
    _executor_update_status = update_status_fn


async def _notify(msg: str):
    if _telegram_notify:
        try:
            await _telegram_notify(msg)
        except Exception as e:
            log.error("Telegram notify failed: %s", e)


# ── Auth ──────────────────────────────────────────────────────────
def _auth_payload() -> dict:
    expires = int((time.time() + 10) * 1000)
    sig = hmac.new(
        API_SECRET.encode(),
        f"GET/realtime{expires}".encode(),
        hashlib.sha256
    ).hexdigest()
    return {"op": "auth", "args": [API_KEY, expires, sig]}


# ── TP detection ──────────────────────────────────────────────────
def _detect_tp(trade: dict, fill_price: float) -> int:
    side = trade.get("side", "Buy")
    tp1  = trade.get("tp1", 0)
    tp2  = trade.get("tp2", 0)
    tp3  = trade.get("tp3", 0)
    t1h  = trade.get("tp1_hit", 0)
    t2h  = trade.get("tp2_hit", 0)
    t3h  = trade.get("tp3_hit", 0)

    if side == "Buy":
        if not t3h and tp3 > 0 and fill_price >= tp3: return 3
        if not t2h and tp2 > 0 and fill_price >= tp2: return 2
        if not t1h and tp1 > 0 and fill_price >= tp1: return 1
    else:
        if not t3h and tp3 > 0 and fill_price <= tp3: return 3
        if not t2h and tp2 > 0 and fill_price <= tp2: return 2
        if not t1h and tp1 > 0 and fill_price <= tp1: return 1
    return 0


# ── Execution handler ─────────────────────────────────────────────
async def _handle_execution(data: dict):
    for item in data.get("data", []):
        order_link_id = item.get("orderLinkId", "")
        exec_type     = item.get("execType", "")
        fill_price    = float(item.get("execPrice", 0) or 0)
        fill_qty      = float(item.get("execQty",   0) or 0)
        symbol        = item.get("symbol", "")

        if exec_type != "Trade":
            continue
        # Accept bot orders and TP orders
        is_bot_order = order_link_id and order_link_id.startswith("bot_")
        is_tp_order  = order_link_id and ("_tp1" in order_link_id or "_tp2" in order_link_id or "_tp3" in order_link_id)
        if not order_link_id or (not is_bot_order and not is_tp_order):
            continue

        print(f"[WS EXEC] {symbol} linkId={order_link_id} price={fill_price} qty={fill_qty}")
        log.info("Fill: %s linkId=%s price=%s qty=%s",
                 symbol, order_link_id, fill_price, fill_qty)

        # Direct TP routing via orderLinkId suffix
        if is_tp_order and _executor_on_tp_hit:
            parent_link_id = order_link_id.replace("_tp1","").replace("_tp2","").replace("_tp3","")
            tp_num = 1 if "_tp1" in order_link_id else 2 if "_tp2" in order_link_id else 3
            _exec_id = item.get("execId") or f"{item.get('orderId','')}-{item.get('execTime','')}-{fill_qty}"
            if _exec_id in _processed_exec_ids:
                print(f"[WS DUPLICATE] skipped execId={_exec_id}")
                continue
            _processed_exec_ids.add(_exec_id)
            print(f"[TP{tp_num} DETECTED] {symbol} fill={fill_price} parent={parent_link_id} execId={_exec_id}")
            log.info("TP%d hit for %s at %s", tp_num, symbol, fill_price)
            await _executor_on_tp_hit(parent_link_id, tp_num, fill_price)
            continue

        if not _executor_get_trade:
            continue

        trade = _executor_get_trade(order_link_id)
        if not trade:
            log.warning("No trade for linkId=%s", order_link_id)
            continue

        if trade.get("status") == "PENDING":
            from trade_db import update_fill
            update_fill(order_link_id, item.get("orderId", ""), fill_price, "OPEN")
            await _notify(
                f"✅ FILLED\n"
                f"{symbol}  {trade.get('side')}  "
                f"@ {fill_price}  qty={fill_qty}"
            )
            return

        tp_num = _detect_tp(trade, fill_price)
        if tp_num > 0 and _executor_on_tp_hit:
            log.info("TP%d hit for %s at %s", tp_num, symbol, fill_price)
            await _executor_on_tp_hit(order_link_id, tp_num, fill_price)


# ── Position handler ──────────────────────────────────────────────
async def _handle_position(data: dict):
    for item in data.get("data", []):
        symbol = item.get("symbol", "")
        size   = float(item.get("size", 0) or 0)

        if size > 0:
            continue

        if not _executor_get_trade:
            continue

        from trade_db import get_trade_by_symbol
        trade = get_trade_by_symbol(symbol)
        if not trade:
            continue
        if trade.get("status") not in ("PENDING", "OPEN"):
            continue

        link_id = trade.get("order_link_id", "")
        log.warning("Manual close detected: %s linkId=%s", symbol, link_id)

        if _executor_update_status:
            _executor_update_status(link_id, "MANUALLY_CLOSED",
                                    "Position closed manually on Bybit")

        await _notify(
            f"ℹ️ MANUAL CLOSE DETECTED\n"
            f"{symbol} — position closed on Bybit\n"
            f"Bot state updated."
        )


# ── WS loop ───────────────────────────────────────────────────────
async def _ws_loop():
    global _running
    reconnect_delay = 5

    while _running:
        try:
            log.info("Connecting WS: %s", WS_URL)
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    WS_URL,
                    heartbeat=20,
                    timeout=aiohttp.ClientWSTimeout(ws_close=10)
                ) as ws:
                    await ws.send_json(_auth_payload())
                    try:
                        auth_resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
                    except asyncio.TimeoutError:
                        log.error("WS auth timeout")
                        await asyncio.sleep(reconnect_delay)
                        continue

                    if not auth_resp.get("success"):
                        log.error("WS auth failed: %s", auth_resp)
                        await _notify("⚠️ WS auth failed — check API keys")
                        _running = False
                        return

                    log.info("WS authenticated")
                    await ws.send_json({
                        "op":   "subscribe",
                        "args": ["execution", "position"]
                    })
                    reconnect_delay = 5

                    async for msg in ws:
                        if not _running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                d = json.loads(msg.data)
                                topic = d.get("topic", "")
                                if topic == "execution":
                                    await _handle_execution(d)
                                elif topic == "position":
                                    await _handle_position(d)
                            except Exception as e:
                                log.error("WS msg error: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            log.warning("WS closed/error — reconnecting")
                            break

        except asyncio.CancelledError:
            log.info("WS cancelled")
            break
        except Exception as e:
            log.error("WS error: %s — retry in %ds", e, reconnect_delay)
            if _running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    log.info("WS loop exited")


# ── Public API ────────────────────────────────────────────────────
def start_ws():
    global _running, _ws_task
    if _running:
        log.warning("WS already running")
        return
    _running = True
    try:
        _ws_task = asyncio.ensure_future(_ws_loop())
    except RuntimeError:
        # Called from async context — use create_task
        _ws_task = asyncio.create_task(_ws_loop())
    log.info("WS manager started (ENV=%s)", ENV)
    print(f"✅ WS manager started ENV={ENV}")


def stop_ws():
    global _running, _ws_task
    _running = False
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
    log.info("WS manager stopped")


def is_ws_running() -> bool:
    return _running and _ws_task is not None and not _ws_task.done()
