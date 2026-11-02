"""
=============================================================
  Orchestrator - Multi-Account Visa Bot Manager
  ─────────────────────────────────────────────────────────
  HOW TO USE:
      python main.py - Start the orchestrator (all accounts)

  Reads accounts.json and for each account:
    1. Assigns a unique Chrome CDP port (9222, 9223, ...)
    2. Spawns login_runner.py  - opens Chrome, logs in, stays open
    3. When login_runner.py prints [READY], spawns booking_runner.py
       -  connects to that same Chrome, parks on the portal
         and watches for trigger_<customer>.json

  Each booking_runner also runs the direct in-browser slot checker
  (fetch_dates_via_browser) for its own logged-in session, so slot
  monitoring starts automatically once login authentication succeeds.

  Press Ctrl+C to gracefully shut down all child processes.
=============================================================
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import threading
import random
from datetime import datetime
import signal
from pathlib import Path
import queue
import re

# Ensure UTF-8 output on Windows consoles/logs
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv

# Ensure project root is on the path for top-level imports (slack.py)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Slack notifications are disabled; Telegram is the active notification channel.
# Legacy slack import intentionally left commented out for compatibility.
# from slack import send as slack_send
from telegram import send as slack_send
from src.common.utils import safe_id, LOG_TIME_FORMAT
from src.common.config import load_accounts as _load_accounts, ACCOUNTS_FILE
from src.common.state import update_state as _update_bot_state, get_state_file as _get_state_file

# ─────────────────────────────────────────────────────────────
BOT_SCRIPT      = Path(__file__).parent / "login_runner.py"
BOT2_SCRIPT     = Path(__file__).parent / "booking_runner.py"

BASE_CDP_PORT   = 9222   # first account gets this port; each subsequent +1
PYTHON          = sys.executable

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def is_account_enabled(acc: dict) -> bool:
    """Check if an account is enabled (status toggle ON) in Account Manager / accounts.json."""
    if acc.get("booking_status") == "BOOKED SUCCESS" or acc.get("role") == "COMPLETED" or acc.get("booked") is True:
        return False
    val = acc.get("enabled", True)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off", "")
    return True


def load_accounts() -> list[dict]:
    """Load accounts using the shared config loader."""
    return _load_accounts()


_MOJIBAKE_MAP = {
    "â ¸ï¸": "[WAIT]",
    "âš¡": "[FAST]",
    "ðŸ…¿ï": "[PARK]",
    "ðŸ…¿": "[PARK]",
    "âœ...": "[OK]",
    "â Œ": "[FAIL]",
    "âš ï¸": "[WARN]",
    "â–¶ï¸": "[START]",
    "â ¹ï¸": "[STOP]",
    "â ³": "[WAIT]",
    "âœ¨": "[OK]",
    "â†’": "->",
    "â€“": "-",
    "dYc": "[HOME]",
    "dY\"": "[REFRESH]",
    "dY -": "[CHALLENGE]",
    "dY": "",
    "o.": "[OK]",
    "⏳": "[WAIT]",
    "⚡": "[FAST]",
    "🅿️": "[PARK]",
    "🎯": "[TARGET]",
    "🔄": "[ROUND-ROBIN]",
    "🚀": "[DISPATCH]",
    "❌": "[FAIL]",
    "⚠️": "[WARN]",
    "✅": "[OK]",
    "🔍": "[SEARCH]",
    "🤖": "[CHALLENGE]",
    "➡️": "->",
    "ℹ️": "[INFO]",
    "🛑": "[STOP]",
    "🏁": "[DONE]",
    "🎉": "[SUCCESS]",
    "🔒": "[LOCK]",
}


def clean_log_text(text: str) -> str:
    """Strip corrupted unicode mojibake and replace emojis with clean ASCII tags."""
    if not text:
        return ""
    s = text
    for k, v in _MOJIBAKE_MAP.items():
        if k in s:
            s = s.replace(k, v)
    # Remove raw emoji characters that corrupt Windows consoles and files
    s = re.sub(r'[\U00010000-\U0010ffff]', '', s)
    s = re.sub(r'[\u2300-\u27bf]', '', s)
    s = re.sub(r'[\ufe00-\ufe0f]', '', s)
    return s


def _write_log_to_file(line: str) -> None:
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    try:
        clean = clean_log_text(line)
        with open(log_dir / "orchestrator.log", "a", encoding="utf-8") as f:
            f.write(clean + "\n")
            f.flush()
    except Exception:
        pass


def log(msg: str) -> None:
    ts = time.strftime(LOG_TIME_FORMAT)
    out = clean_log_text(f"[{ts}] [ORCHESTRATOR] {msg}")
    print(out, flush=True)
    _write_log_to_file(out)


# ─────────────────────────────────────────────────────────────
# Process launchers
# ─────────────────────────────────────────────────────────────

def _get_subprocess_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def spawn_login_runner(account: dict, cdp_port: int, profile_dir: str) -> subprocess.Popen:
    """Launch login_runner.py for a single account."""
    customer = account["customer_name"]
    cmd = [
        PYTHON, str(BOT_SCRIPT),
        "--username",    account["username"],
        "--password",    account["password"],
        "--cdp-port",    str(cdp_port),
        "--customer",    customer,
        "--profile-dir", profile_dir,
    ]
    log(f"[START] Starting bot for '{customer}' on port {cdp_port} (direct connection)")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
        env=_get_subprocess_env(),
    )


def spawn_booking_runner(account: dict, cdp_port: int) -> subprocess.Popen:
    """Launch booking_runner.py for a single account once login is done."""
    customer = account.get("customer_name") or account["username"]
    cmd = [
        PYTHON, str(BOT2_SCRIPT),
        "--cdp-port", str(cdp_port),
        "--customer",  customer,
        "--username",  account["username"]
    ]
    log(f"[START] Starting bot2 for '{customer}' on port {cdp_port}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
        env=_get_subprocess_env(),
    )


def chrome_still_running(cdp_port: int) -> bool:
    """True when Chrome is STILL listening on the CDP port.

    The client requirement: once the browser is open it must not be closed and
    reopened repeatedly (slot monitoring must survive 24x7). When the runner
    crashes but Chrome is healthy, the restarted runner simply reconnects to
    the existing browser instead of killing it.
    """
    try:
        with socket.create_connection(("127.0.0.1", cdp_port), timeout=2):
            return True
    except OSError:
        return False


def kill_chrome_by_port(cdp_port: int) -> None:
    """Kill the Chrome process listening on the given CDP port."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True
        )
        pid = None
        for line in result.stdout.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            # parts[1] is the local address column, e.g. "127.0.0.1:9222"
            if len(parts) >= 5 and parts[1].endswith(f":{cdp_port}"):
                pid = parts[-1]
                break
        if pid and pid.isdigit():
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
            log(f"🖥️  Killed Chrome PID {pid} on port {cdp_port}")
        else:
            log(f"[WARN]  No Chrome process found on port {cdp_port} to kill")
    except Exception as e:
        log(f"[WARN]  Failed to kill Chrome on port {cdp_port}: {e}")


# ─────────────────────────────────────────────────────────────
# Log-relay thread (pipes subprocess stdout -> our stdout)
# ─────────────────────────────────────────────────────────────

_log_lock = threading.Lock()
_last_log_label = ""

def relay_output(proc: subprocess.Popen, label: str, ready_event: threading.Event | None = None, on_ready=None) -> None:
    """
    Read subprocess stdout line-by-line and re-print with a label prefix.
    If ready_event is provided, set it when '[READY]' is detected in a line.
    If on_ready callback is provided, invoke it immediately when '[READY]' is detected.
    """
    global _last_log_label
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line.strip():
                with _log_lock:
                    print("", flush=True)
                    _write_log_to_file("")
                continue
            
            with _log_lock:
                if label == _last_log_label:
                    prefix = " " * (len(label) + 3) # len(f"[{label}] ")
                else:
                    prefix = f"[{label}] "
                    _last_log_label = label
                
                out = clean_log_text(f"{prefix}{line}")
                print(out, flush=True)
                _write_log_to_file(out)
                
            if ready_event and "[READY]" in line:
                ready_event.set()
            if on_ready and "[READY]" in line:
                try:
                    on_ready()
                except Exception as cb_err:
                    log(f"[WARN] on_ready callback error for {label}: {cb_err}")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────



def stdin_listener(q: queue.Queue):
    for line in sys.stdin:
        if line.strip():
            q.put(line.strip())

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-monitor", action="store_true",
                        help="Accepted for GUI compatibility (slot monitoring now runs inside booking_runner).")
    parser.add_argument("--only", default="",
                        help="Comma-separated list of account usernames to run (default: all accounts).")
    args = parser.parse_args()

    # Clear runtime log files on startup so files start fresh and avoid disk/memory load
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    for lf_name in ["orchestrator.log", "browser.log", "polling.log"]:
        try:
            lf_path = log_dir / lf_name
            with open(lf_path, "w", encoding="utf-8") as f:
                f.truncate(0)
        except Exception as e:
            print(f"[STARTUP] Could not clear {lf_name}: {e}", flush=True)

    raw_accounts = load_accounts()
    log(f"Loaded {len(raw_accounts)} total account(s) from accounts.json")

    # ── Filter by Account Manager Status Toggle (Web UI) ─────────────
    # Only run accounts whose status is ON in the Web App Account Manager.
    # Accounts with toggle OFF (disabled) must never be launched.
    enabled_accounts = [a for a in raw_accounts if is_account_enabled(a)]
    disabled_accounts = [a for a in raw_accounts if not is_account_enabled(a)]
    if disabled_accounts:
        disabled_desc = ", ".join(f"'{a.get('customer_name') or a.get('username')}'" for a in disabled_accounts)
        log(f"⏸️  [ACCOUNT-MANAGER] {len(disabled_accounts)} account(s) have Status Toggle OFF — skipping bot launch: {disabled_desc}")

    accounts = enabled_accounts
    log(f"✅ [ACCOUNT-MANAGER] Launching {len(accounts)} active account(s) with Status Toggle ON: "
        + ", ".join(f"'{a.get('customer_name') or a.get('username')}'" for a in accounts))

    # ── Optional subset selection (GUI "Start Bots" passes --only user1,user2) ──
    if args.only.strip():
        wanted = {u.strip() for u in args.only.split(",") if u.strip()}
        accounts = [a for a in accounts if a.get("username") in wanted]
        missing_or_disabled = wanted - {a.get("username") for a in accounts}
        if missing_or_disabled:
            log(f"[WARN] Selected account(s) not started (either not found or status toggle OFF in Account Manager): {', '.join(sorted(missing_or_disabled))}")
        log("[START] Running " + str(len(accounts)) + " selected account(s): "
            + ", ".join(a.get("username", "?") for a in accounts))
    # ── Reset tracker and clean stale runtime states on startup ──
    try:
        from src.common.request_tracker import reset as reset_tracker
        reset_tracker()
        log("[TRACKER] Request tracker reset: total requests = 0.")
    except Exception as e:
        log(f"[TRACKER] Note on reset: {e}")

    # Clean stale polling state registered monitors and active leases
    try:
        pstate_file = Path(__file__).parent / "polling_state.json"
        if pstate_file.exists():
            with open(pstate_file, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            pdata["active_poller"] = None
            pdata["active_poller_since"] = 0
            pdata["current_turn"] = None
            pdata["turn_assigned_at"] = 0
            pdata["last_poller"] = None
            pdata["poll_finished_at"] = 0
            pdata["global_last_poll"] = 0
            pdata["registered_monitors"] = {}
            pdata["account_quota_cooldowns"] = {}
            with open(pstate_file, "w", encoding="utf-8") as f:
                json.dump(pdata, f, indent=2)
            log("[POLLING] Cleaned stale registered monitors, turns, active leases, and quota cooldowns for fresh startup.")
    except Exception as e:
        log(f"[POLLING] Note on pstate clean: {e}")

    # Clean stale booking coordinator queue and slot window on startup
    try:
        coord_file = Path(__file__).parent / "booking_coord.json"
        if coord_file.exists():
            with open(coord_file, "w", encoding="utf-8") as f:
                json.dump({
                    "queue": [],
                    "running": None,
                    "leases": {},
                    "idempotency_cache": {},
                    "updated_at": time.time(),
                }, f, indent=2)
            log("[COORDINATOR] Cleaned stale booking queue and coordinator leases.")
        slot_window_file = Path(__file__).parent / "slot_window.json"
        if slot_window_file.exists():
            slot_window_file.unlink()
            log("[COORDINATOR] Cleaned stale slot_window lock file.")
    except Exception as e:
        log(f"[COORDINATOR] Note on coordinator clean: {e}")

    # Configure / Reset account proxies
    try:
        from src.common.proxy import reset_all_proxies_to_static, get_proxy_mode
        mode = get_proxy_mode()
        if mode in ("AUTO", "LOCAL", "HYBRID"):
            log(f"[PROXY] Running in {mode} mode: Starts on host clean residential broadband directly, auto-switches to Decodo on rate-limit/ban (Smart Round-Robin).")
            reset_all_proxies_to_static()
        elif mode == "DECODO":
            log("[PROXY] Running in DECODO mode: using Decodo static residential ISP gateway.")
            reset_all_proxies_to_static()
        elif mode == "WEBSHARE":
            log("[PROXY] Running in WEBSHARE mode: routing through Webshare rotating proxy pool (p.webshare.io:80).")
            reset_all_proxies_to_static()
        else:
            log(f"[PROXY] Running in {mode} mode.")
    except Exception as e:
        log(f"[PROXY] Note on startup proxy reset: {e}")

    # Clean stale account state files (cached bookings, stale triggers) on startup
    for account in accounts:
        try:
            u_id = safe_id(account.get("username", ""))
            s_file = Path(__file__).parent / f"state_{u_id}.json"
            if s_file.exists():
                with open(s_file, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                s_data["booking_running"] = False
                s_data["pending"] = False
                s_data["completed"] = False
                s_data["cached_booking"] = None
                s_data["trigger_key"] = None
                s_data["trigger_timestamp"] = None
                s_data["last_booking_failure_reason"] = None
                with open(s_file, "w", encoding="utf-8") as f:
                    json.dump(s_data, f, indent=2)
        except Exception:
            pass

    # Clean stale booking queue and coordinator state on startup
    try:
        bqueue_file = Path(__file__).resolve().parent.parent / "booking_queue.json"
        if bqueue_file.exists():
            with open(bqueue_file, "w", encoding="utf-8") as f:
                json.dump({"running": None, "queue": [], "total_queued": 0, "updated_at": time.time()}, f, indent=2)
        bcoord_file = Path(__file__).parent / "booking_coord.json"
        if bcoord_file.exists():
            with open(bcoord_file, "w", encoding="utf-8") as f:
                json.dump({"queue": [], "running": None, "leases": {}, "idempotency_cache": {}, "updated_at": time.time(), "total_queued": 0}, f, indent=2)
        log("[QUEUE] Reset booking queue and coordinator states on startup.")
    except Exception as e:
        log(f"[QUEUE] Note on queue clean: {e}")

    log("[STATE] Cleaned stale booking tokens & triggers across account state files.")

    all_procs: list[subprocess.Popen] = []
    sessions: list[dict] = []
    procs_lock = threading.Lock()

    def shutdown(signum=None, frame=None):
        log("Shutting down all child processes ...")
        for session in sessions:
            kill_chrome_by_port(session.get("cdp_port"))

        for p in all_procs:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            except Exception:
                pass
        # Give them a moment to die
        time.sleep(2)
        for p in all_procs:
            try:
                p.kill()
            except Exception:
                pass
        log("All done. Bye!")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ─────────────────────────────────────────────────────────────
    # Inner helpers (close over sessions / all_procs / procs_lock)
    # ─────────────────────────────────────────────────────────────

    def ensure_booking_runner_running(session: dict) -> None:
        """Spawn booking_runner (bot2) if not already active."""
        with procs_lock:
            existing_proc = session.get("booking_proc")
            if existing_proc is not None and existing_proc.poll() is None:
                return  # already running
            customer = session["account"]["customer_name"]
            log(f"[OK]  '{customer}' is logged in - starting bot2")
            booking_proc = spawn_booking_runner(session["account"], session["cdp_port"])
            session["booking_proc"] = booking_proc
            all_procs.append(booking_proc)
            threading.Thread(
                target=relay_output,
                args=(booking_proc, f"booking:{customer}"),
                daemon=True,
            ).start()

    def wait_and_spawn_booking_runner(session: dict, expected_event: threading.Event, expected_proc) -> None:
        """Background thread: waits for [READY] then spawns booking_runner."""
        customer = session["account"]["customer_name"]
        log(f"[WAIT]  Waiting for '{customer}' login to complete ...")
        # Keep waiting as long as the login process is alive
        while True:
            if expected_event.wait(timeout=20):
                if session.get("ready_event") is not expected_event or session.get("login_proc") is not expected_proc:
                    return  # Phantom thread from an older run
                ensure_booking_runner_running(session)
                return
            if session.get("ready_event") is not expected_event or session.get("login_proc") is not expected_proc:
                return  # Older waiter, superseded by restart
            if expected_proc.poll() is not None:
                return  # Login process exited

    def start_bot_session(sess_dict: dict) -> None:
        """Spawn login_runner and wire up relay + waiter threads."""
        uid = safe_id(sess_dict["account"]["username"])
        c_name = sess_dict["account"].get("customer_name") or uid
        p_dir = str(Path(__file__).parent.parent / f"chrome_profile_{uid}")

        new_proc = spawn_login_runner(sess_dict["account"], sess_dict["cdp_port"], p_dir)
        with procs_lock:
            all_procs.append(new_proc)
        sess_dict["login_proc"] = new_proc

        new_ready_event = threading.Event()
        sess_dict["ready_event"] = new_ready_event

        def _on_ready():
            ensure_booking_runner_running(sess_dict)

        threading.Thread(
            target=relay_output,
            args=(new_proc, f"login:{c_name}", new_ready_event),
            kwargs={"on_ready": _on_ready},
            daemon=True,
        ).start()

        threading.Thread(
            target=wait_and_spawn_booking_runner,
            args=(sess_dict, new_ready_event, new_proc),
            daemon=True,
        ).start()

    def _rotate_proxy_before_restart(session: dict, reason: str) -> None:
        """Rotate to a fresh residential IP for an account before restarting.
        Each account has its own forwarder with its own IP.
        """
        try:
            from src.common.proxy import rotate_account_ip, proxy_credentials_configured
            if not proxy_credentials_configured():
                return
            username = session["account"].get("username", "")
            result = rotate_account_ip(username, f"restart: {reason}")
            if result:
                local_port, new_ip = result
                log(f"[ROUND-ROBIN] Rotated IP for '{username}' -> {new_ip} (local:{local_port})")
        except Exception as e:
            log(f"[WARN]  Proxy rotation before restart skipped: {e}")
    # ─────────────────────────────────────────────────────────────────────────
    # Crash handlers run in their own background threads.
    # ─────────────────────────────────────────────────────────────────────────

    def handle_login_crash(session: dict, code: int) -> None:
        """Runs in a background thread - sleeping here does NOT block the main loop."""
        customer = session["account"]["customer_name"]

        # RULE: a fully-booked account is permanently retired - never reopen.
        if session.get("fully_booked"):
            log(f"🏁 '{customer}' is fully booked - skipping login restart.")
            return

        if code == 99:
            log(f"🛑 Chrome window for '{customer}' was manually closed. Aborting auto-restart.")
            return

        log(f"[ROUND-ROBIN] Restarting login for '{customer}' after crash ...")

        now = time.time()
        history = session.setdefault("login_restart_history", [])
        history.append(now)
        history[:] = [t for t in history if now - t < 300]
        if len(history) > 3:
            log(f"[WARN]  Too many rapid login restarts for '{customer}'. Waiting 60s...")
            time.sleep(60)
            history.clear()
            
            if session.get("intended_state") == "STOPPED":
                log(f"[SKIP]  Skipping login restart for '{customer}' - was manually stopped.")
                return
            if session.get("login_proc") is not None or session.get("booking_proc") is not None:
                log(f"[SKIP]  Skipping login restart for '{customer}' - already running.")
                return

        # Keep the browser open whenever Chrome itself is healthy - only the
        # crashed runner is restarted; it reconnects to the SAME Chrome.
        if chrome_still_running(session["cdp_port"]):
            log(f"🖥️  Chrome still alive on port {session['cdp_port']} - restarting runner "
                f"WITHOUT closing the browser ...")
            time.sleep(2)
        else:
            kill_chrome_by_port(session["cdp_port"])
            time.sleep(4)

        if session.get("intended_state") == "STOPPED":
            return

        # The runner likely died because its IP got banned / rate-limited -
        # rotate to a fresh residential IP before relaunching so the new
        # runner doesn't inherit the flagged address. (Rotation is throttled
        # and hourly-capped inside proxy.py, so this can never loop wildly.)
        _rotate_proxy_before_restart(session, f"login crash (exit {code})")

        start_bot_session(session)

    def handle_booking_crash(session: dict, code: int) -> None:
        """Runs in a background thread - sleeping here does NOT block the main loop."""
        customer = session["account"]["customer_name"]

        # RULE: a fully-booked account is permanently retired - never reopen.
        if session.get("fully_booked"):
            log(f"🏁 '{customer}' is fully booked - skipping booking restart.")
            return

        # Exit code 0 = complete booking success (OFC + consular). Close the
        # browser PERMANENTLY and never restart this account. Other accounts
        # keep running untouched.
        if code == 0:
            log(f"🎉 booking:{customer} finished with SUCCESS - OFC + consular fully booked.")
            log(f"🔒 Closing browser for '{customer}' permanently - this account will not be restarted.")
            session["fully_booked"] = True
            kill_chrome_by_port(session["cdp_port"])
            return

        if code == 42 or code == 43:
            # Legacy rate-limit exit codes - rare now. Treat them like a normal
            # crash: restart shortly without any long 429 cooldown.
            log(f"[WARN]  booking:{customer} exited with code {code} (rate-limit path) - restarting shortly ...")

            def short_rotate_restart(sess_dict):
                sess_dict["rate_limited"] = False
                time.sleep(60)
                c_name = sess_dict["account"]["customer_name"]
                if sess_dict.get("login_proc") is not None or sess_dict.get("booking_proc") is not None:
                    log(f"[SKIP]  Skipping restart for '{c_name}' - already running.")
                    return
                if sess_dict.get("intended_state") == "STOPPED":
                    log(f"[SKIP]  Skipping restart for '{c_name}' - was manually stopped.")
                    return
                log(f"[ROUND-ROBIN] Restarting '{c_name}' ...")
                if chrome_still_running(sess_dict["cdp_port"]):
                    log(f"🖥️  Chrome still alive on port {sess_dict['cdp_port']} - keeping browser open.")
                    time.sleep(2)
                else:
                    kill_chrome_by_port(sess_dict["cdp_port"])
                    time.sleep(4)
                # Rate-limit exit: grab a fresh IP so the restarted runner
                # doesn't retry from the address that just got throttled.
                _rotate_proxy_before_restart(session, f"booking rate-limit exit {code}")
                start_bot_session(sess_dict)

            threading.Thread(target=short_rotate_restart, args=(session,), daemon=True).start()

        else:
            # Session expiry or unexpected booking crash - restart immediately
            log(f"[WARN]  booking:{customer} exited with code {code} - restarting bot ...")

            now = time.time()
            history = session.setdefault("booking_restart_history", [])
            history.append(now)
            history[:] = [t for t in history if now - t < 300]
            if len(history) > 3:
                log(f"[WARN]  Too many rapid booking restarts for '{customer}'. Waiting 60s...")
                time.sleep(60)
                history.clear()
                
                if session.get("intended_state") == "STOPPED":
                    log(f"[SKIP]  Skipping booking restart for '{customer}' - was manually stopped.")
                    return
                if session.get("login_proc") is not None or session.get("booking_proc") is not None:
                    log(f"[SKIP]  Skipping booking restart for '{customer}' - already running.")
                    return

            # Keep Chrome alive when it is healthy - reconnect instead of reopen.
            if chrome_still_running(session["cdp_port"]):
                log(f"🖥️  Chrome still alive on port {session['cdp_port']} - keeping browser open.")
                time.sleep(2)
            else:
                kill_chrome_by_port(session["cdp_port"])
                time.sleep(4)

            if session.get("intended_state") == "STOPPED":
                return

            # Unexpected booking crash - frequently IP-ban / rate-limit
            # related. Rotate first so the restart starts from a clean IP.
            _rotate_proxy_before_restart(session, f"booking crash (exit {code})")

            start_bot_session(session)

    # ── Role Enforcement (Split Distributed Setup) ──────────────
    load_dotenv(Path(__file__).parent.parent / ".env")
    
    remote_trigger_url = os.getenv("REMOTE_TRIGGER_URL", "").strip()
    laptop_role = os.getenv("LAPTOP_ROLE", "").strip().upper()
    
    # Fallback for old .env files before LAPTOP_ROLE was added
    if not laptop_role:
        if remote_trigger_url:
            laptop_role = "POLLING"
        else:
            laptop_role = "ALL_IN_ONE"
            
    valid_accounts = []
    polling_count = 0
    
    for account in accounts:
        role = account.get("role", "POLLING_ONLY")
        c_name = account.get("customer_name") or account.get("username", "Unknown")
        
        # Enforce Account Manager status toggle: Skip if OFF
        if not is_account_enabled(account):
            log(f"[SKIP] Account '{c_name}' status toggle is OFF in Account Manager — skipping bot launch.")
            continue
        
        if laptop_role == "POLLING":
            # Polling Laptop: Only run POLLING_ONLY
            if role == "RESERVED_BOOKING":
                log(f"[SKIP] Skipping VIP account '{c_name}' on Polling Laptop.")
                continue
        elif laptop_role == "BOOKING":
            # Booking Laptop: Only run RESERVED_BOOKING
            if role == "POLLING_ONLY":
                log(f"[SKIP] Skipping Polling account '{c_name}' on Booking Laptop.")
                continue
        elif laptop_role == "ALL_IN_ONE":
            # Run everything
            pass

        # Maximum 3 polling accounts allowed across the fleet to guarantee zero rate limits
        if role == "POLLING_ONLY":
            if polling_count >= 3:
                log(f"🛡️  [FLEET-RULE] Max 3 polling monitors reached! Re-assigning '{c_name}' to RESERVED_BOOKING standby.")
                account = dict(account)
                account["role"] = "RESERVED_BOOKING"
            else:
                polling_count += 1
            
        valid_accounts.append(account)
        
    if not valid_accounts:
        log("[FAIL] No valid accounts to run based on current role settings!")
    else:
        log(f"🚀 [FLEET-STATUS] Launching {len(valid_accounts)} accounts: {polling_count} Active Poller(s) (Max 3), {len(valid_accounts) - polling_count} Standby Booking Sniper(s).")

    # ── Spawn one session per account ─────────────────────────
    for idx, account in enumerate(valid_accounts):
        cdp_port = BASE_CDP_PORT + idx
        sess_dict = {
            "account":               account,
            "cdp_port":              cdp_port,
            "login_proc":            None,
            "booking_proc":          None,
            "ready_event":           None,
            "login_restart_history":    [],
            "booking_restart_history":  [],
            "rate_limited":             False,   # Explicit 429 flag
            "intended_state":           "RUNNING",
        }
        sessions.append(sess_dict)
        start_bot_session(sess_dict)
        
        # Add random delay between account starts to stagger booking attempts
        # This helps avoid rate limiting when multiple accounts try to book simultaneously
        if idx < len(valid_accounts) - 1:
            delay = random.uniform(2, 4)
            log(f"[WAIT] Waiting {delay:.1f}s before starting next account to stagger requests...")
            time.sleep(delay)

    # ── Webhook receiver on BOOKING laptop (remote triggers only) ──
    if laptop_role == "BOOKING":
        # Slot monitoring runs inside booking_runner via fetch_dates_via_browser;
        # no external monitor process exists anymore.
        def spawn_webhook():
            env = os.environ.copy()
            return subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).parent / "webhook_receiver.py")],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace"
            )
        wp = spawn_webhook()
        with procs_lock:
            all_procs.append(wp)
        threading.Thread(target=relay_output, args=(wp, "webhook"), daemon=True).start()

    log("="*60)
    log("All processes launched. Press Ctrl+C to stop everything.")
    log("="*60)

    # ── Keep the main thread alive ────────────────────────────
    cmd_queue = queue.Queue()
    threading.Thread(target=stdin_listener, args=(cmd_queue,), daemon=True).start()

    try:
        while True:
            # ── Process GUI/CLI commands from stdin ───────────────────
            try:
                cmd_str = cmd_queue.get(timeout=5)
                if cmd_str.startswith("STOP:"):
                    uid = cmd_str.split(":")[1]
                    for session in sessions:
                        if safe_id(session["account"]["username"]) == uid:
                            cname = session["account"].get("customer_name") or uid
                            log(f"🛑 UI requested shutdown for '{cname}'")
                            proc  = session.get("login_proc")
                            bproc = session.get("booking_proc")
                            if bproc and bproc.poll() is None:
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(bproc.pid)], capture_output=True)
                            if proc and proc.poll() is None:
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                            kill_chrome_by_port(session["cdp_port"])
                            session["login_proc"]  = None
                            session["booking_proc"] = None
                            session["ready_event"]  = None
                            session["intended_state"] = "STOPPED"
                            # Clear flag so any in-flight delayed_restart thread aborts
                            session["rate_limited"] = False
                            try:
                                _update_bot_state(_get_state_file(session["account"]["username"]), {"booking_running": False, "pending": False})
                            except Exception:
                                pass
                            break
                elif cmd_str.startswith("START:"):
                    uid = cmd_str.split(":")[1]
                    for session in sessions:
                        if safe_id(session["account"]["username"]) == uid:
                            if session.get("login_proc") is not None or session.get("booking_proc") is not None:
                                break  # already running
                            cname = session["account"].get("customer_name") or uid
                            log(f"[START] UI requested start for '{cname}'")
                            session["intended_state"] = "RUNNING"
                            start_bot_session(session)
                            break
            except queue.Empty:
                pass

            # ── Clean up dead processes from all_procs list ───────────
            with procs_lock:
                all_procs[:] = [p for p in all_procs if p.poll() is None]

            # ── Health-check each bot session ─────────────────────────
            for session in sessions:
                proc         = session.get("login_proc")
                booking_proc = session.get("booking_proc")
                customer     = session["account"]["customer_name"]

                # --- Login runner exited ---
                if proc and proc.poll() is not None:
                    code = proc.returncode
                    log(f"[WARN]  login:{customer} exited with code {code}")
                    session["login_proc"] = None
                    # Always kill booking_proc when login dies
                    if booking_proc and booking_proc.poll() is None:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(booking_proc.pid)], capture_output=True)
                    session["booking_proc"] = None
                    try:
                        _update_bot_state(_get_state_file(session["account"]["username"]), {"booking_running": False})
                    except Exception:
                        pass
                    # Hand off to background thread - main loop is never blocked
                    threading.Thread(target=handle_login_crash, args=(session, code), daemon=True).start()

                # --- Booking runner exited ---
                # Re-read from session dict since the login handler above may have already cleared it.
                current_booking_proc = session.get("booking_proc")
                if current_booking_proc and current_booking_proc.poll() is not None:
                    code = current_booking_proc.returncode
                    session["booking_proc"] = None
                    try:
                        _update_bot_state(_get_state_file(session["account"]["username"]), {"booking_running": False})
                    except Exception:
                        pass
                    # Re-fetch login_proc from session instead of using the stale
                    # local `proc` captured at the top of this loop iteration.
                    current_login_proc = session.get("login_proc")
                    if current_login_proc and current_login_proc.poll() is None:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(current_login_proc.pid)], capture_output=True)
                    session["login_proc"] = None
                    # Hand off to background thread - main loop is never blocked
                    threading.Thread(target=handle_booking_crash, args=(session, code), daemon=True).start()

    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
