"""
menu.py
Persistent Telegram menu for trading agent.
One message, edited in place. No chat flooding.
"""
import asyncio
_SCAN_RUNNING = False
import logging
import time

log = logging.getLogger("menu")

# ── State ─────────────────────────────────────────────────────────
_menu_message_ids = {}   # {chat_id: message_id}
_at_selecting     = {}   # {chat_id: bool} — mid-selection state


def _get_status_line():
    """Build live status line for menu footer.

    User-facing AutoTrade modes:
    OFF    = execution disabled
    MEDIUM = execution enabled + MEDIUM filter
    PRO    = execution enabled + strict PROD/PRO filter
    """
    try:
        from executor import get_auto_trade_mode
        from app import SCANNER_RUNNING
        import agent as _ag

        exec_mode = get_auto_trade_mode()
        filter_mode = getattr(_ag, "_TRADE_MODE", "PROD")

        if exec_mode == "OFF":
            at_label = "🔴 OFF"
        elif filter_mode == "MEDIUM":
            at_label = "🔵 MEDIUM"
        else:
            at_label = "🟢 PRO"

        loop_str = "10m ▶" if SCANNER_RUNNING else "OFF"
        return f"AT: {at_label} | Loop: {loop_str}"
    except Exception:
        return "AT: — | Loop: —"


def _get_trade_summary():
    """One-line trade summary for menu."""
    try:
        from trade_db import get_open_trades
        trades = get_open_trades()
        if not trades:
            return "Trades: none"
        # Try to show simple count
        return f"Trades: {len(trades)} active"
    except Exception:
        return "Trades: —"


def _build_main_menu_text():
    return (
        "🤖 *TRADING AGENT*\n"
        "─────────────────────\n"
        f"{_get_status_line()}\n"
        f"{_get_trade_summary()}\n"
        "─────────────────────\n"
        "Select action:"
    )


def _build_main_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        from app import SCANNER_RUNNING
        loop_label = "⏹ Stop" if SCANNER_RUNNING else "▶ Loop"
    except Exception:
        SCANNER_RUNNING = False
        loop_label = "▶ Loop"

    try:
        from executor import get_auto_trade_mode
        import agent as _ag

        exec_mode = get_auto_trade_mode()
        filter_mode = getattr(_ag, "_TRADE_MODE", "PROD")

        if exec_mode == "OFF":
            at_label = "💼 AT: 🔴 OFF"
        elif filter_mode == "MEDIUM":
            at_label = "💼 AT: 🔵 MEDIUM"
        else:
            at_label = "💼 AT: 🟢 PRO"
    except Exception:
        at_label = "💼 AT: —"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📡 Scan Now",  callback_data="menu_scan"),
            InlineKeyboardButton(loop_label,     callback_data="menu_stop" if SCANNER_RUNNING else "menu_loop15"),
        ],
        [
            InlineKeyboardButton(at_label,        callback_data="menu_at"),
        ],
        [
            InlineKeyboardButton("💰 Trades",    callback_data="menu_trades"),
            InlineKeyboardButton("📊 Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("📰 News",      callback_data="menu_news"),
        ],
    ])


def _build_at_keyboard():
    """User-facing AutoTrade mode selection: OFF / MEDIUM / PRO."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        from executor import get_auto_trade_mode
        import agent as _ag

        exec_mode = get_auto_trade_mode()
        filter_mode = getattr(_ag, "_TRADE_MODE", "PROD")

        if exec_mode == "OFF":
            cur = "OFF"
        elif filter_mode == "MEDIUM":
            cur = "MEDIUM"
        else:
            cur = "PRO"
    except Exception:
        cur = "OFF"

    def lbl(m):
        icons = {"OFF": "🔴", "MEDIUM": "🔵", "PRO": "🟢"}
        return f"{'✓ ' if m == cur else ''}{icons[m]} {m}"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(lbl("OFF"),    callback_data="menu_at_OFF"),
            InlineKeyboardButton(lbl("MEDIUM"), callback_data="menu_at_MEDIUM"),
            InlineKeyboardButton(lbl("PRO"),    callback_data="menu_at_PRO"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="menu_back")]
    ])


async def send_main_menu(bot, chat_id, edit_message_id=None):
    """Send or edit the persistent main menu."""
    text     = _build_main_menu_text()
    keyboard = _build_main_keyboard()
    try:
        if edit_message_id:
            try:
                msg = await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception:
                # Message deleted or expired — send new one
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                _menu_message_ids[chat_id] = msg.message_id
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        _menu_message_ids[chat_id] = msg.message_id
        return msg.message_id
    except Exception as e:
        log.error("Menu send/edit error: %s", e)
        return None


async def refresh_menu(bot, chat_id):
    """Refresh existing menu in place."""
    mid = _menu_message_ids.get(chat_id)
    if mid:
        await send_main_menu(bot, chat_id, edit_message_id=mid)


async def handle_menu_callback(update, context):
    """Route all menu_ callbacks."""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    mid     = query.message.message_id
    data    = query.data
    bot     = context.bot

    # ── AT mode selection: user-facing OFF / MEDIUM / PRO ─────────
    if data.startswith("menu_at_"):
        user_mode = data.replace("menu_at_", "").upper()

        if user_mode in ("OFF", "MEDIUM", "PRO"):
            from executor import set_auto_trade_mode

            if user_mode == "OFF":
                set_auto_trade_mode("OFF")
                msg = "🔴 *AutoTrade: OFF*\nTrading disabled."

            else:
                # Execution ON uses executor PRO internally.
                # Filter aggressiveness is controlled by agent._TRADE_MODE + mode.txt.
                set_auto_trade_mode("PRO")

                import agent as _ag
                import app as _app
                import os

                if user_mode == "MEDIUM":
                    filter_mode = "MEDIUM"
                    msg = "🔵 *AutoTrade: MEDIUM*\nTrading enabled with MEDIUM filters."
                else:
                    filter_mode = "PROD"
                    msg = "🟢 *AutoTrade: PRO*\nTrading enabled with strict PRO filters."

                _ag._TRADE_MODE = filter_mode
                try:
                    _app.TRADE_MODE = filter_mode
                except Exception:
                    pass

                mf = os.path.join(os.path.dirname(__file__), "mode.txt")
                with open(mf, "w") as f:
                    f.write(filter_mode + "\n")

            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=mid,
                text=msg,
                parse_mode="Markdown",
                reply_markup=_build_at_keyboard()
            )
        return

    # ── Mode selection ────────────────────────────────────────────
    if data.startswith("menu_mode_"):
        new_mode = data.replace("menu_mode_", "")
        if new_mode in ("PROD", "MEDIUM"):
            import agent as _ag
            import app as _app
            _ag._TRADE_MODE  = new_mode
            _app.TRADE_MODE  = new_mode
            import os
            mf = os.path.join(os.path.dirname(__file__), "mode.txt")
            with open(mf, 'w') as f: f.write(new_mode)
        await send_main_menu(bot, chat_id, edit_message_id=mid)
        return

    # ── Back to main menu ─────────────────────────────────────────
    if data == "menu_back":
        await send_main_menu(bot, chat_id, edit_message_id=mid)
        return

    # ── AT mode submenu ──────────────────────────────────────────
    if data == "menu_at":
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=mid,
            text="💼 *Select AutoTrade Mode:*\n\nOFF = no live trades\nMEDIUM = live trading with softer filters\nPRO = live trading with strict filters",
            parse_mode="Markdown",
            reply_markup=_build_at_keyboard()
        )
        return

    # ── Mode button ───────────────────────────────────────────────
    if data == "menu_mode":
        await bot.edit_message_text(
            chat_id=chat_id, message_id=mid,
            text="🎯 *Select Trade Mode:*\n\nPROD = strict filters\nMEDIUM = relaxed filters",
            parse_mode="Markdown",
            reply_markup=_build_mode_keyboard()
        )
        return

    # ── Scan now ──────────────────────────────────────────────────
    if data == "menu_scan":
        await send_main_menu(bot, chat_id, edit_message_id=mid)
        global _SCAN_RUNNING
        # Auto-reset if stuck (safety valve)
        if _SCAN_RUNNING:
            await bot.send_message(chat_id=chat_id, text="⚠️ Scan already running. Please wait.")
            return
        _SCAN_RUNNING = True
        try:
            from app import scan_market
            await bot.send_message(chat_id=chat_id, text="🔍 Scan started...")
            print(f"[SCAN TASK CREATED] for chat={chat_id}")
            async def _run_scan():
                global _SCAN_RUNNING
                try:
                    print("[SCAN TASK RUNNING]")
                    await asyncio.wait_for(
                        scan_market(context, chat_id, None),
                        timeout=180
                    )
                    print("[SCAN TASK DONE]")
                except asyncio.TimeoutError:
                    print("[SCAN TIMEOUT]")
                    try:
                        await bot.send_message(chat_id=chat_id, text="⚠️ Scan timeout (120s) — market data slow")
                    except Exception:
                        pass
                except Exception as _te:
                    import traceback as _tr
                    print(f"[SCAN TASK ERROR] {_te}")
                    _tr.print_exc()
                    try:
                        await bot.send_message(chat_id=chat_id, text=f"❌ Scan error: {_te}")
                    except Exception:
                        pass
                finally:
                    _SCAN_RUNNING = False
            asyncio.create_task(_run_scan())
        except Exception as _se:
            _SCAN_RUNNING = False
            await bot.send_message(chat_id=chat_id, text=f"❌ Scan error: {_se}")
        return


    # ── Loop 15m ──────────────────────────────────────────────────
    if data == "menu_loop15":
        from app import cmd_loop15_internal
        await cmd_loop15_internal(context, chat_id)
        await send_main_menu(bot, chat_id, edit_message_id=mid)
        return

    # ── Stop loop ─────────────────────────────────────────────────
    if data == "menu_stop":
        from app import loop_task, SCANNER_RUNNING
        import app as _app
        if _app.loop_task and not _app.loop_task.done():
            _app.loop_task.cancel()
        _app.SCANNER_RUNNING = False
        await send_main_menu(bot, chat_id, edit_message_id=mid)
        return

    # ── Status ────────────────────────────────────────────────────
    if data == "menu_status":
        from app import cmd_status_internal
        await cmd_status_internal(bot, chat_id)
        return

    # ── Trades view ───────────────────────────────────────────────
    if data == "menu_trades":
        from trades_view import format_active_trades
        text = await format_active_trades()
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="menu_trades"),
            InlineKeyboardButton("← Back",     callback_data="menu_back"),
        ]])
        await bot.edit_message_text(
            chat_id=chat_id, message_id=mid,
            text=text, parse_mode="Markdown", reply_markup=kb
        )
        return

    # ── Signals history ───────────────────────────────────────────
    if data == "menu_dashboard":
        import app as _dapp
        try:
            from executor import get_auto_trade_mode as _gatm
            at_mode = _gatm()
        except Exception: at_mode = "?"
        try:
            from level_cache import count as _lcc
            cache_count = _lcc()
        except Exception: cache_count = 0
        try:
            from agent import _get_btc_regime
            btc = _get_btc_regime()
        except Exception: btc = "?"
        from trades_view import format_active_trades
        loop_st = "10m ▶" if _dapp.SCANNER_RUNNING else "OFF"
        header = (
            f"📊 *Dashboard*\n"
            f"─────────────────────\n"
            f"Loop:   {loop_st}\n"
            f"BTC:    {btc}\n"
            
            f"Levels: {cache_count} cached\n"
            f"─────────────────────\n"
        )
        trades_text = await format_active_trades()
        from telegram import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
        kb = _IKM([[_IKB("🔄 Refresh", callback_data="menu_dashboard"), _IKB("← Back", callback_data="menu_back")]])
        await bot.edit_message_text(chat_id=chat_id, message_id=mid,
            text=(header+trades_text)[:4000], parse_mode="Markdown", reply_markup=kb)
        return

    if data == "menu_signals":
        from app import ACTIVE_SIGNALS
        import time as _t
        now = _t.time()
        sigs = [v for v in ACTIVE_SIGNALS.values() if now - v.get("timestamp",0) < 1800]
        if not sigs:
            txt = "📋 *Recent Signals*\n\nNo signals in last 30 minutes."
        else:
            lines = ["📋 *Recent Signals*\n"]
            for s in sigs[-5:]:
                lines.append(s.get("text","")[:120] + "...")
            txt = "\n".join(lines)
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="menu_back")]])
        await bot.edit_message_text(
            chat_id=chat_id, message_id=mid,
            text=txt[:4000], parse_mode="Markdown", reply_markup=kb
        )
        return

    # ── News ──────────────────────────────────────────────────────
    if data == "menu_news":
        try:
            from agent import get_market_context
            ctx = get_market_context()
        except Exception as _e:
            ctx = f"Market context unavailable: {_e}"
        try:
            from agent import _get_btc_regime
            btc = _get_btc_regime()
        except Exception:
            btc = "?"
        from telegram import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
        kb = _IKM([[_IKB("🔄 Refresh", callback_data="menu_news"), _IKB("← Back", callback_data="menu_back")]])
        news_text = (
            f"📰 *Market Context*\n"
            f"─────────────────────\n"
            f"BTC Regime: *{btc}*\n"
            f"─────────────────────\n"
            f"{ctx}"
        )
        await bot.edit_message_text(
            chat_id=chat_id, message_id=mid,
            text=news_text[:4000], parse_mode="Markdown", reply_markup=kb
        )
        return

        from app import run_analysis_telegram
        await send_main_menu(bot, chat_id, edit_message_id=mid)
        asyncio.create_task(
            bot.send_message(chat_id=chat_id, text="📰 Fetching market context...")
        )
        return
