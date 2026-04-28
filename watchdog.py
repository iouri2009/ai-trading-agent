"""
TTM369 Watchdog + Guard System
Architecture: parse → state machine → detect → action
"""
import asyncio, os, time, json, subprocess, re
from collections import deque
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/ai-trading-agent/.env"))

TG_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
BOT_LOG    = os.path.expanduser("~/ai-trading-agent/bot.log")
ERR_LOG    = os.path.expanduser("~/ai-trading-agent/bot.error.log")
DEPLOY_SH  = os.path.expanduser("~/ai-trading-agent/deploy.sh")
STATE_FILE = os.path.expanduser("~/ai-trading-agent/watchdog_state.json")
WD_DIR     = os.path.expanduser("~/ai-trading-agent")

WAITING_TIMEOUT  = 120
RESOLVED_TTL     = 300
ALERT_RESET      = 900
CLEAN_INTERVAL   = 1800

OUTCOMES = {
    "ORDER CONFIRMED":             "confirmed",
    "ORDER VERIFIED via position": "verified",
    "ORDER FAILED":                "failed",
    "ORDER VERIFY UNKNOWN":        "unknown",
    "[BLOCK]":                     "blocked",
    "[RISK] BLOCK":                "blocked",
}

SYM_RE = re.compile(r'\b([A-Z0-9]{2,20}USDT)\b')

def extract_symbol_from_states(line):
    matches = SYM_RE.findall(line)
    return next((m for m in matches if m in execution_states), None)

def extract_symbol_any(line):
    m = SYM_RE.search(line)
    return m.group(1) if m else None

execution_states = {}
bot_log_pos      = 0
error_file_pos   = 0
last_scan_time   = time.time()
last_log_time    = time.time()
last_order_t     = 0.0
scan_count       = 0
trade_count      = 0
recent_valid     = deque(maxlen=20)
alerts_sent      = {}
error_times      = {}
error_window     = deque()
order_failures   = deque()

def load_pstate():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {"restarts": []}

def save_pstate(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception as e:
        print(f"[WD] pstate save failed: {e}")

async def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print(f"[WD] {msg}")
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": f"WD: {msg}"}
            )
    except Exception as e:
        print(f"[WD TG FAIL] {e}")

def alert(key, msg):
    now = time.time()
    if now - alerts_sent.get(key, 0) > ALERT_RESET:
        alerts_sent[key] = now
        asyncio.create_task(tg(msg))

def clean_alerts():
    now = time.time()
    for k in list(alerts_sent):
        if now - alerts_sent[k] > ALERT_RESET * 2:
            del alerts_sent[k]

def set_state(sym, state, outcome=None):
    old = execution_states.get(sym, {}).get("state", "none")
    execution_states[sym] = {
        "state":   state,
        "t":       time.time(),
        "outcome": outcome,
        "alerted": False,
    }
    print(f"[WD STATE] symbol={sym} state={old}→{state}" +
          (f" outcome={outcome}" if outcome else ""))

def resolve_sym(sym, outcome):
    if sym and sym in execution_states:
        existing = execution_states[sym]
        execution_states[sym] = {
            "state":   "resolved",
            "t":       time.time(),
            "outcome": outcome,
            "alerted": existing.get("alerted", False),
        }
        print(f"[WD STATE] symbol={sym} state=resolved outcome={outcome}")

def cleanup_states():
    now = time.time()
    for sym in list(execution_states):
        s = execution_states[sym]
        if s["state"] == "resolved" and now - s["t"] > RESOLVED_TTL:
            del execution_states[sym]
            print(f"[WD STATE] symbol={sym} cleaned up")

def parse_new_lines():
    global bot_log_pos, last_scan_time, last_log_time
    global last_order_t, scan_count, trade_count
    try:
        size = os.path.getsize(BOT_LOG)
        if bot_log_pos == 0:
            bot_log_pos = max(0, size - 50000)
        if size <= bot_log_pos:
            return
        last_log_time = time.time()
        with open(BOT_LOG) as f:
            f.seek(bot_log_pos)
            lines = f.readlines()
        bot_log_pos = size

        scan_valid = None
        for line in lines:
            if "[SIZING]" in line:
                sym = extract_symbol_any(line)
                if sym and execution_states.get(sym, {}).get("state") != "waiting":
                    set_state(sym, "waiting")

            for marker, outcome_type in OUTCOMES.items():
                if marker in line:
                    sym = extract_symbol_from_states(line)
                    if sym:
                        resolve_sym(sym, outcome_type)
                        if outcome_type == "confirmed":
                            last_order_t = time.time()
                            trade_count += 1
                        elif outcome_type == "failed":
                            order_failures.append(time.time())
                    break

            if "[SCAN START]" in line:
                last_scan_time = time.time()
                scan_count += 1
                if scan_valid is not None:
                    recent_valid.append(scan_valid)
                scan_valid = 0

            if "Scan complete" in line:
                last_scan_time = time.time()

            if "Valid:" in line and "Checked:" in line and scan_valid is not None:
                try:
                    v = int(line.split("Valid:")[1].strip().split()[0].rstrip(",|"))
                    scan_valid = v
                except Exception:
                    pass

        if scan_valid is not None:
            recent_valid.append(scan_valid)

    except Exception as e:
        print(f"[WD] parse error: {e}")

def parse_errors():
    global error_file_pos
    try:
        size = os.path.getsize(ERR_LOG)
        if error_file_pos == 0:
            error_file_pos = size
            return
        if size <= error_file_pos:
            return
        with open(ERR_LOG) as f:
            f.seek(error_file_pos)
            new = f.read()
        error_file_pos = size
        now = time.time()
        for line in new.splitlines():
            if "ERROR" in line or "Loop error" in line:
                key = line.strip()[:100]
                if now - error_times.get(key, 0) > 60:
                    error_times[key] = now
                    error_window.append(now)
        cutoff = now - 300
        while error_window and error_window[0] < cutoff:
            error_window.popleft()
    except Exception as e:
        print(f"[WD] error parse failed: {e}")

async def action_kill_switch(reason):
    print(f"[GUARD ACTION] TYPE=KILL_SWITCH reason={reason}")
    try:
        result = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0,'/Users/iouriilioukhine/ai-trading-agent'); "
             "from executor import set_auto_trade_mode,get_auto_trade_mode; "
             "set_auto_trade_mode('OFF'); print(get_auto_trade_mode())"],
            capture_output=True, text=True, timeout=10, cwd=WD_DIR
        )
        mode = result.stdout.strip()
        if mode == "OFF":
            await tg(f"AUTO TRADE DISABLED\nReason: {reason}")
            print(f"[GUARD ACTION] KILL_SWITCH confirmed mode=OFF")
        else:
            await tg(f"KILL SWITCH FAILED — mode={mode}\nManual check required")
            print(f"[GUARD ACTION] KILL_SWITCH FAILED mode={mode}")
    except Exception as e:
        await tg(f"KILL SWITCH ERROR: {e}")
        print(f"[GUARD ACTION] KILL_SWITCH ERROR: {e}")

async def action_restart_bot(reason):
    ps = load_pstate()
    now = time.time()
    recent = [t for t in ps.get("restarts", []) if now - t < 3600]
    if len(recent) >= 2:
        await tg(f"RESTART BLOCKED — {len(recent)} restarts in last hour\nManual check required")
        print(f"[GUARD ACTION] RESTART BLOCKED")
        return
    print(f"[GUARD ACTION] TYPE=RESTART reason={reason}")
    ps["restarts"] = recent + [now]
    save_pstate(ps)
    await tg(f"BOT RESTARTING\nReason: {reason}")
    try:
        subprocess.Popen(["bash", DEPLOY_SH], cwd=WD_DIR)
        print(f"[GUARD ACTION] RESTART triggered")
    except Exception as e:
        await tg(f"RESTART FAILED: {e}")
        print(f"[GUARD ACTION] RESTART FAILED: {e}")

async def detect_waiting_timeout():
    now = time.time()
    unresolved = [
        sym for sym, s in execution_states.items()
        if s["state"] == "waiting" and now - s["t"] > WAITING_TIMEOUT
    ]
    latest_had_valid = bool(recent_valid and recent_valid[-1] > 0)
    if (len(unresolved) >= 3
            and latest_had_valid
            and len(execution_states) > 0):
        await action_kill_switch(f"3+ unresolved sizing: {unresolved[:3]}")
        for sym in unresolved:
            resolve_sym(sym, "timeout_kill")

async def detect_order_failed():
    now = time.time()
    cutoff = now - 600
    while order_failures and order_failures[0] < cutoff:
        order_failures.popleft()
    count = len(order_failures)
    if count == 1:
        alert("order_failed_single", "ORDER FAILED — single occurrence, monitoring")
    elif count >= 3:
        alert("order_failed_repeat", "ORDER FAILED 3x in 10min")
        if "order_failed_kill" not in alerts_sent:
            alerts_sent["order_failed_kill"] = now
            await action_kill_switch("ORDER FAILED 3+ times in 10 minutes")
            order_failures.clear()

async def detect_unknown_outcome():
    for sym, s in execution_states.items():
        if (s["state"] == "resolved"
                and s["outcome"] == "unknown"
                and not s.get("alerted", False)):
            execution_states[sym]["alerted"] = True
            asyncio.create_task(tg(f"WARNING: ORDER VERIFY UNKNOWN for {sym}"))
            print(f"[WD] unknown outcome alert sent for {sym}")

async def detect_scan_dead():
    now = time.time()
    if now - last_scan_time > 900 and now - last_log_time > 900:
        await action_restart_bot("No scan and no log activity for 15+ minutes")

async def detect_error_storm():
    if len(error_window) >= 5:
        await action_kill_switch(f"{len(error_window)} unique errors in 5 minutes")
        error_window.clear()

def detect_over_filter():
    if len(recent_valid) >= 20 and sum(recent_valid) == 0 and scan_count > 30:
        alert("over_filter",
              f"NO VALID SETUPS in last 20 scans ({scan_count} total)\n"
              f"Over-filtering or dead market")
    else:
        alerts_sent.pop("over_filter", None)

def heartbeat():
    now = time.time()
    ls = f"{int((now-last_scan_time)/60)}m ago"
    lo = f"{int((now-last_order_t)/60)}m ago" if last_order_t else "never"
    rv = sum(recent_valid) if recent_valid else 0
    waiting = sum(1 for s in execution_states.values() if s["state"] == "waiting")
    active  = [k for k, t in alerts_sent.items() if now - t < ALERT_RESET]
    status  = "OK" if not active else f"ALERTS:{len(active)}"
    return (f"WD | Scans:{scan_count} Trades:{trade_count} Valid(20):{rv}\n"
            f"States:{len(execution_states)} Waiting:{waiting} Errors:{len(error_window)}\n"
            f"Scan:{ls} Order:{lo} | {status}")

async def run():
    await tg("WATCHDOG STARTED")
    print("[WD] Started")
    last_hb    = time.time()
    last_clean = time.time()
    while True:
        try:
            parse_new_lines()
            parse_errors()
            cleanup_states()
            await detect_waiting_timeout()
            await detect_order_failed()
            await detect_unknown_outcome()
            await detect_scan_dead()
            await detect_error_storm()
            detect_over_filter()
            now = time.time()
            if now - last_hb > 600:
                await tg(heartbeat())
                last_hb = now
            if now - last_clean > CLEAN_INTERVAL:
                clean_alerts()
                last_clean = now
        except Exception as e:
            print(f"[WD ERR] {e}")
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(run())
