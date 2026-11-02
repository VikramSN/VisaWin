"""
=============================================================
  Booking Runner  ->  OFC Appointment Trigger (browser-Delegated)
  
  Connects to an authenticated Chrome session, parks on the portal,
  and watches a shared state file for a 'pending' trigger from the
  monitor. Uses 'booking_running' flag to signal busy state.

  State file: src/state_<customer>.json
  Schema:
    {
      "booking_running": false,   <- managed by this runner
      "pending": false,             <- set by monitor, cleared here
      "ofcCities": [...],
      "ofcStartDate": "...",
      "ofcEndDate": "...",
      "consularCities": [...],
      "consularStartDate": "...",
      "consularEndDate": "...",
      "customer_name": "..."
    }
=============================================================
"""

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import logging

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
import random
import argparse
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv
from patchright.async_api import async_playwright

# Ensure project root is on the path for top-level imports (slack.py) and src.* imports
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.auth.browser import connect_to_chrome
from src.booking.cdp_client import ensure_on_portal
from src.booking.executor import (
    heal_tab_for_booking,
    trigger_booking,
    trigger_booking_ofc_only,
    trigger_booking_reschedule,
    trigger_booking_sniper_consular_only,
)
from src.common import portal_pacer
from src.common.utils import safe_id, LOG_TIME_FORMAT, log_stamp
from src.common.state import (
    read_state as _read_state,
    write_state as _write_state,
    update_state as _update_state,
    try_queue_local_trigger,
)
from src.common.config import ACCOUNTS_FILE
# Slack notifications are disabled; Telegram is the active notification channel.
# Legacy slack import intentionally left commented out for compatibility.
# from slack import send_slack
from telegram import (
    send as tg_send,
    send_booking_started,
    send_booking_success,
    send_booking_failure,
    send_partial_booking,
    send_ofc_success_triggering_consular,
    send_monitoring_status,
)


log = logging.getLogger("booking_runner")


def _notify(text: str) -> None:
    """Best-effort Telegram notification  ->  never raises into the watch loop."""
    try:
        tg_send(text)
    except Exception as exc:
        log.warning(f"Telegram notify failed: {exc}")


def _safe_send(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except Exception as exc:
        log.warning(f"Telegram {getattr(fn, '__name__', 'notification')} failed: {exc}")
from src.common.db_logger import MongoDBHandler, MongoDBLogger
import html

# Add new imports for recovery
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question
from src.polling_runner import fetch_dates_via_browser
from src.common.constants import (
    CONSULAR_LOCATION_MAP,
    OFC_LOCATION_MAP,
    normalize_city,
)
from src.api import (
    VisaScheduleApiClient,
    extract_session_context,
)

# --- One-Shot Cached Booking architecture ---
# trigger_cached_ofc_booking: books the slot the monitor ALREADY matched with
# 2-4 in-page API calls (warm-up  ->  entries revalidation  ->  BOOK post) instead of
# the legacy sniper's full city/date sweep  ->  the primary 429 defence.
# Fleet lock: guarantees ONE account books at a time, with a calm-down gap
# between consecutive attempts (FLEET_BOOKING_GAP_SECONDS, default 20 s).
from src.booking.one_shot import trigger_cached_ofc_booking
from src.common.fleet_lock import (
    fleet_lock_holder,
    release_fleet_booking_lock,
    try_acquire_fleet_booking_lock,
)
from src.common.state import (
    read_state as _read_state,
    write_state as _write_state,
    update_state as _update_state,
    try_queue_local_trigger,
)
from src.booking_queue import (
    _enqueue,
    _dequeue_and_advance,
    _remove_stale,
    _advance_queue,
    wait_for_turn,
)
from src.common.fleet_lock import (
    fleet_lock_holder,
    release_fleet_booking_lock,
    try_acquire_fleet_booking_lock,
)
from src.booking.one_shot import trigger_cached_ofc_booking

from src.booking.coordinator import booking_coordinator
from src.booking.session_radar import session_radar
from src.common.cdn_manager import is_cdn_refresh_due, quiet_cdn_refresh
from src.common.reliability import (
    BookingOutcome,
    BrowserPreflightState,
    ErrorClass,
    HealthState,
    PreflightSlotState,
    Subsystem,
    get_account_tracker,
    slot_fingerprint,
)

# Terminal ANSI Color Palettes
ANSI_DARK_BLUE   = "\033[38;5;27m"
ANSI_ORANGE      = "\033[38;5;208m"
ANSI_DARK_RED    = "\033[38;5;160m"
ANSI_DARK_GREEN  = "\033[38;5;28m"
ANSI_RESET       = "\033[0m"

# Bounded trigger freshness: max 180s to prevent premature queue eviction
CACHED_TRIGGER_MAX_AGE_SECONDS = float(
    os.getenv("CACHED_TRIGGER_MAX_AGE_SECONDS", "180.0")
)

# Enable/disable the One-Shot Cached Booking architecture.
# When True, booking triggers with a cached_booking package use the
# minimal 2-4 API-call path instead of the legacy sniper city/date sweep.
CACHED_BOOKING_ENABLED = os.getenv(
    "CACHED_BOOKING_ENABLED", "1"
).strip().lower() not in ("0", "false", "no")

load_dotenv()

POLL_INTERVAL = 0.01   # seconds between state file checks
OFC_CONSULAR_WINDOW_SECONDS = 32 * 60  # 32 minutes to book consular after OFC success

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOOKING_RUNNER] %(message)s",
    datefmt=LOG_TIME_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        MongoDBHandler()
    ],
)
log = logging.getLogger("booking_runner")
db_logger = MongoDBLogger()


def _get_booking_rest_seconds() -> float:
    """
    Read the common booking-rest duration.

    Supports the old rest_hours value for backward compatibility.
    """
    polling_state_file = (
        Path(__file__).parent / "polling_state.json"
    )
    default_minutes = 60.0

    if not polling_state_file.exists():
        return default_minutes * 60

    try:
        data = json.loads(
            polling_state_file.read_text(encoding="utf-8")
        )

        if "rest_minutes" in data:
            minutes = float(
                data.get("rest_minutes", default_minutes)
            )
        elif "rest_hours" in data:
            minutes = float(
                data.get("rest_hours", 1.0)
            ) * 60
        else:
            minutes = default_minutes

        return max(minutes, 0) * 60

    except Exception:
        return default_minutes * 60

_ACCOUNT_LAST_PAGE_RELOAD: dict[str, float] = {}

def _enter_booking_rest(
    state_file: Path,
    customer: str,
    reason: str,
    duration_seconds: float | None = None,
) -> float:
    """
    Block booking while background polling continues.
    duration_seconds overrides polling_state.json's rest_minutes  ->  used to
    resume OFC retry attempts quickly after a failed booking attempt
    (client directive: book at any cost).
    """
    rest_seconds = (
        float(duration_seconds)
        if duration_seconds is not None
        else _get_booking_rest_seconds()
    )
    now = time.time()
    rest_until = now + rest_seconds
    rest_minutes = rest_seconds / 60

    _update_state(
        state_file,
        {
            "rest_until": rest_until,
            "pending": False,
            "booking_running": False,
            "last_booking_failure_at": now,
            "last_booking_failure_reason": str(reason)[:500],
        },
    )

    log.warning(
        f" Booking failed for '{customer}'. "
        f"Booking rest active for {rest_minutes:g} minute(s), "
        f"until {datetime.fromtimestamp(rest_until).strftime('%H:%M:%S')}. "
        f"Background polling remains active."
    )

    return rest_until

#  State file helpers (wrappers around shared state module) ---


class SessionExpiredError(RuntimeError):
    """Raised when the authenticated browser session has silently expired
    (OFC date fetch returns the login HTML page instead of JSON).
    The booking runner main loop catches this and runs recover_session()."""


def record_booking_event(stage: str, username: str, customer_name: str, city: str, slot_date: str, slot_time: str = "", status: str = "ATTEMPTED", notes: str = ""):
    """Record an OFC or Consular booking attempt/outcome asynchronously.
    Updates local visabot.db directly if found and sends POST to server Hub.
    """
    def _worker():
        now_str = datetime.now().isoformat()
        clean_stage = str(stage).upper()
        clean_city = str(city).upper()
        clean_status = str(status).upper()
        # 1. Direct local SQLite recording if present
        try:
            db_file = Path(__file__).resolve().parent.parent / "server" / "data" / "visabot.db"
            if db_file.exists():
                import sqlite3
                conn = sqlite3.connect(str(db_file), timeout=5)
                cur = conn.cursor()
                cur.execute("""
                CREATE TABLE IF NOT EXISTS booking_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL,
                    username TEXT NOT NULL,
                    customer_name TEXT,
                    city TEXT NOT NULL,
                    slot_date TEXT NOT NULL,
                    slot_time TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    created_at TEXT
                )
                """)
                cur.execute("""
                INSERT INTO booking_records (stage, username, customer_name, city, slot_date, slot_time, status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (clean_stage, username, customer_name or username, clean_city, slot_date or "N/A", slot_time or "", clean_status, str(notes or ""), now_str))
                conn.commit()
                conn.close()
        except Exception:
            pass

        # 2. Remote Server Hub notification if reachable
        try:
            s_url = os.getenv("SERVER_URL", "http://localhost:5000").rstrip("/")
            s_key = os.getenv("SERVER_API_KEY", os.getenv("API_KEY", ""))
            requests.post(
                f"{s_url}/api/bookings/record",
                json={
                    "stage": clean_stage,
                    "username": username,
                    "customer_name": customer_name,
                    "city": clean_city,
                    "slot_date": slot_date or "N/A",
                    "slot_time": slot_time or "",
                    "status": clean_status,
                    "notes": str(notes or "")
                },
                headers={"X-API-Key": s_key},
                timeout=3
            )
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _persist_account_booked_success(
    username: str,
    customer: str,
    ofc_date: str = "",
    consular_date: str = "",
    ofc_city: str = "",
    consular_city: str = "",
):
    """Permanently marks the account as BOOKED SUCCESS across all storage locations:
    1. Root accounts.json
    2. server/data/accounts.json
    3. SQLite visabot.db (accounts table)
    4. Server API endpoint (if running)
    Sets enabled=False, role='COMPLETED', booking_status='BOOKED SUCCESS' so
    orchestrator and polling never touch this account again.
    """
    now_iso = datetime.now().isoformat()
    clean_user = str(username or "").strip().lower()

    # 1. Update root accounts.json
    try:
        if ACCOUNTS_FILE.exists():
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
            updated = False
            for acc in accounts:
                if str(acc.get("username") or "").strip().lower() == clean_user:
                    acc["enabled"] = False
                    acc["role"] = "COMPLETED"
                    acc["status"] = "BOOKED SUCCESS"
                    acc["booking_status"] = "BOOKED SUCCESS"
                    acc["booked"] = True
                    acc["booked_ofc_date"] = ofc_date
                    acc["booked_consular_date"] = consular_date
                    acc["booked_ofc_city"] = ofc_city
                    acc["booked_consular_city"] = consular_city
                    acc["updated_at"] = now_iso
                    updated = True
                    break
            if updated:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(accounts, f, indent=2)
                log.info(f"[PERSIST] Updated root accounts.json: '{clean_user}' -> BOOKED SUCCESS (disabled).")
    except Exception as e:
        log.warning(f"[PERSIST] Error updating root accounts.json for '{clean_user}': {e}")

    # 2. Update server/data/accounts.json
    try:
        server_acc_file = Path(__file__).resolve().parent.parent / "server" / "data" / "accounts.json"
        if server_acc_file.exists():
            with open(server_acc_file, "r", encoding="utf-8") as f:
                s_accounts = json.load(f)
            updated_s = False
            for acc in s_accounts:
                if str(acc.get("username") or "").strip().lower() == clean_user:
                    acc["enabled"] = False
                    acc["role"] = "COMPLETED"
                    acc["status"] = "BOOKED SUCCESS"
                    acc["booking_status"] = "BOOKED SUCCESS"
                    acc["booked"] = True
                    acc["booked_ofc_date"] = ofc_date
                    acc["booked_consular_date"] = consular_date
                    acc["booked_ofc_city"] = ofc_city
                    acc["booked_consular_city"] = consular_city
                    acc["updated_at"] = now_iso
                    updated_s = True
                    break
            if updated_s:
                with open(server_acc_file, "w", encoding="utf-8") as f:
                    json.dump(s_accounts, f, indent=2)
                log.info(f"[PERSIST] Updated server/data/accounts.json: '{clean_user}' -> BOOKED SUCCESS.")
    except Exception as e:
        log.warning(f"[PERSIST] Error updating server/data/accounts.json for '{clean_user}': {e}")

    # 3. Update SQLite database (server/data/visabot.db)
    try:
        db_file = Path(__file__).resolve().parent.parent / "server" / "data" / "visabot.db"
        if db_file.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_file), timeout=5)
            cur = conn.cursor()
            for col in ("booking_status", "booked_ofc_date", "booked_consular_date"):
                try:
                    cur.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
            cur.execute("""
            UPDATE accounts
            SET enabled = 0,
                role = 'COMPLETED',
                booking_status = 'BOOKED SUCCESS',
                booked_ofc_date = ?,
                booked_consular_date = ?,
                updated_at = ?
            WHERE LOWER(username) = ?
            """, (ofc_date, consular_date, now_iso, clean_user))
            conn.commit()
            conn.close()
            log.info(f"[PERSIST] Updated visabot.db: '{clean_user}' -> BOOKED SUCCESS (enabled=0).")
    except Exception as e:
        log.warning(f"[PERSIST] Error updating visabot.db: {e}")

    # 4. Notify Server API if running
    try:
        s_url = os.getenv("SERVER_URL", "http://localhost:5000").rstrip("/")
        s_key = os.getenv("SERVER_API_KEY", os.getenv("API_KEY", ""))
        requests.post(
            f"{s_url}/api/accounts/{clean_user}/mark-booked",
            json={
                "ofc_date": ofc_date,
                "consular_date": consular_date,
                "ofc_city": ofc_city,
                "consular_city": consular_city,
            },
            headers={"X-API-Key": s_key},
            timeout=3,
        )
    except Exception:
        pass


def _persist_account_ofc_hold(
    username: str,
    customer: str,
    ofc_date: str = "",
    ofc_city: str = "",
):
    """Marks the account as holding an OFC appointment and waiting for Consular."""
    now_iso = datetime.now().isoformat()
    clean_user = str(username or "").strip().lower()

    for p in (ACCOUNTS_FILE, Path(__file__).resolve().parent.parent / "server" / "data" / "accounts.json"):
        try:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    accs = json.load(f)
                for a in accs:
                    if str(a.get("username") or "").strip().lower() == clean_user:
                        a["booking_status"] = "OFC_BOOKED_PENDING_CONSULAR"
                        a["booked_ofc_date"] = ofc_date
                        a["booked_ofc_city"] = ofc_city
                        a["ofc_booked_at"] = time.time()
                        a["updated_at"] = now_iso
                        break
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(accs, f, indent=2)
        except Exception:
            pass

    try:
        db_file = Path(__file__).resolve().parent.parent / "server" / "data" / "visabot.db"
        if db_file.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_file), timeout=5)
            cur = conn.cursor()
            for col in ("booking_status", "booked_ofc_date", "booked_consular_date"):
                try:
                    cur.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
            cur.execute("""
            UPDATE accounts
            SET booking_status = 'OFC_BOOKED_PENDING_CONSULAR',
                booked_ofc_date = ?,
                updated_at = ?
            WHERE LOWER(username) = ?
            """, (ofc_date, now_iso, clean_user))
            conn.commit()
            conn.close()
    except Exception:
        pass


def _persist_account_consular_timeout(username: str, customer: str):
    """Resets the account after the 32-minute Consular hold window expires."""
    now_iso = datetime.now().isoformat()
    clean_user = str(username or "").strip().lower()

    for p in (ACCOUNTS_FILE, Path(__file__).resolve().parent.parent / "server" / "data" / "accounts.json"):
        try:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    accs = json.load(f)
                for a in accs:
                    if str(a.get("username") or "").strip().lower() == clean_user:
                        a["booking_status"] = "IDLE"
                        a["booked_ofc_date"] = ""
                        a.pop("ofc_booked_at", None)
                        a["updated_at"] = now_iso
                        break
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(accs, f, indent=2)
        except Exception:
            pass

    try:
        db_file = Path(__file__).resolve().parent.parent / "server" / "data" / "visabot.db"
        if db_file.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_file), timeout=5)
            cur = conn.cursor()
            cur.execute("""
            UPDATE accounts
            SET booking_status = 'IDLE',
                booked_ofc_date = '',
                updated_at = ?
            WHERE LOWER(username) = ?
            """, (now_iso, clean_user))
            conn.commit()
            conn.close()
    except Exception:
        pass


#  Session Recovery 

def _load_account_config(username: str) -> dict:
    """Load the account config from accounts.json."""
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        clean_user = (username or "").strip().lower()
        for c in raw:
            u = str(c.get("username") or "").strip().lower()
            cn = str(c.get("customer_name") or "").strip().lower()
            if clean_user in (u, cn):
                return c
    except Exception:
        pass
    return {}

def mid_out_order(items: list) -> list:
    """Mid-Out Sweep / Center-Right-Left scan strategy.
    Avoids the common bot rush on index-0 by picking the middle element first,
    then sweeping right (later options), then sweeping left (earlier options).
    Matches user's exact specification:
    [1, 2, 3, 4, 5, 6] -> [3, 4, 5, 6, 2, 1]
    [1, 2, 3] -> [2, 3, 1]
    [1, 2] -> [2, 1]
    """
    if not items or len(items) <= 1:
        return list(items) if items else []
    if len(items) == 2:
        return [items[1], items[0]]
    mid = (len(items) - 1) // 2
    return items[mid:] + list(reversed(items[:mid]))


def _match_polled_ofc_dates(results: dict, config: dict) -> tuple[bool, str, str]:
    """Check if any polled OFC dates match the account's criteria using Mid-Out selection."""
    ofc_cities = []
    for c in config.get("ofcCities", []):
        c_upper = c.upper()
        if c_upper == "DELHI":
            ofc_cities.append("NEW DELHI")
        else:
            ofc_cities.append(c_upper)
    start = config.get("ofcStartDate", "")
    end = config.get("ofcEndDate", "")

    # Apply prevent_immediate constraint if enabled
    if config.get("prevent_immediate"):
        dynamic_start = (datetime.today() + timedelta(days=3)).strftime("%Y-%m-%d")
        if not start or start < dynamic_start:
            start = dynamic_start

    # Phantom same-day listings can never actually be booked  ->  chasing them
    # burns API requests and triggers Cloudflare 429s. Enforce a lead floor.
    lead_floor = (datetime.today() + timedelta(days=_min_slot_lead_days())).strftime("%Y-%m-%d")
    if not start or start < lead_floor:
        start = lead_floor

    if not ofc_cities or not start or not end:
        return False, "", ""

    matched_candidates = []
    for city, dates in results.items():
        if city.upper() not in ofc_cities:
            continue
        if not isinstance(dates, list):
            continue
        for d in dates:
            date_str = str((d or {}).get("Date", ""))[:10]
            if start <= date_str <= end:
                matched_candidates.append((date_str, city))

    if not matched_candidates:
        return False, "", ""

    matched_candidates.sort()
    ordered = mid_out_order(matched_candidates)
    best_date, best_city = ordered[0]
    return True, best_city, best_date

def _min_slot_lead_days() -> int:
    """Minimum days between today and a bookable OFC date.

    Same-day ("0d") listings are phantom entries on this portal  ->  an OFC
    appointment can never be booked for today. Chasing them is the primary
    cause of trigger storms and Cloudflare 429s. Override via env.
    """
    try:
        return max(0, int(os.getenv("MIN_SLOT_LEAD_DAYS", "2")))
    except ValueError:
        return 2


_FLEET_PAUSE_LOG_TS = {"t": 0.0}
_WR_LOG_TS = {"t": 0.0}   # waiting-room log throttle
_HOME_LIVENESS = {"fails": 0}  # consecutive homepage-not-live cycles (watchdog)
_FLEET_BOOKING_LEASE_SECONDS = 1200  # 20 min  ->  crashed holder can't pause fleet forever

# Consecutive booking 429s across the runner's lifetime (rate-limit tracking).
_BOOKING_429 = {"ts": 0.0, "count": 0}
_MAX_CONSECUTIVE_429 = 3

# Track consecutive rate-limit/IP-ban signals per account so recovery (page reload / IP rotate)
# kicks in reliably after repeated failures.
_ACCOUNT_IP_FAILURES = {}   # username -> {"count": int, "last_reason": str}
_ACCOUNT_FAILURES_LOCK = threading.Lock()
_IP_FAILURE = {"count": 0, "last_reason": ""}  # backward-compat global
_ROTATE_AFTER_CONSECUTIVE_FAILURES = 3  # Rotate proxy IP after 3 consecutive rate-limits / blocks (with in-place reloads first)
# Exponential backoff for consecutive rate-limit signals: 30s, 60s, 120s, 
# (capped). During backoff NO requests are made  ->  a page reload re-fires many
# requests and deepens a Cloudflare rate limit, so reload only happens after
# the backoff path has failed repeatedly.
_BACKOFF_BASE_SECONDS = float(os.getenv("RATE_LIMIT_BACKOFF_BASE", "90"))
_BACKOFF_MAX_SECONDS = float(os.getenv("RATE_LIMIT_BACKOFF_MAX", "600"))

def _record_fleet_rate_limit(reason: str) -> float:
    """Record a rate-limit event in polling_state.json and compute fleet-wide backoff."""
    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_lock_file = Path(__file__).parent / "polling_state.lock"
    backoff = _BACKOFF_BASE_SECONDS
    with _polling_state_lock(polling_lock_file) as locked:
        if locked and polling_state_file.exists():
            try:
                with open(polling_state_file, "r") as f:
                    pstate = json.load(f)
                c429 = int(pstate.get("consecutive_429s", 0)) + 1
                pstate["consecutive_429s"] = c429
                # Progressive exponential backoff across the fleet: 90s -> 180s -> 360s -> 600s max
                backoff = min(_BACKOFF_BASE_SECONDS * (2 ** min(c429 - 1, 3)), _BACKOFF_MAX_SECONDS)
                pstate["rate_limit_until"] = time.time() + backoff
                with open(polling_state_file, "w") as f:
                    json.dump(pstate, f, indent=2)
            except Exception:
                pass
    return backoff


def _reset_rate_limit_failures(username: str = "") -> None:
    """Clear the consecutive rate-limit streak after a successful poll."""
    if _IP_FAILURE["count"]:
        log.info(f" Poll succeeded  ->  rate-limit streak cleared (was {_IP_FAILURE['count']}).")
    _IP_FAILURE["count"] = 0
    _IP_FAILURE["last_reason"] = ""
    if username:
        with _ACCOUNT_FAILURES_LOCK:
            _ACCOUNT_IP_FAILURES.pop(str(username).strip().lower(), None)
        try:
            setattr(run, f"_consecutive_429_{username}", 0)
            setattr(run, f"_rot_429_{username}", 0)
        except Exception:
            pass
    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_lock_file = Path(__file__).parent / "polling_state.lock"
    try:
        with _polling_state_lock(polling_lock_file) as locked:
            if locked and polling_state_file.exists():
                with open(polling_state_file, "r") as f:
                    pstate = json.load(f)
                if pstate.get("consecutive_429s") or pstate.get("rate_limit_until"):
                    pstate["consecutive_429s"] = 0
                    pstate["rate_limit_until"] = 0
                    with open(polling_state_file, "w") as f:
                        json.dump(pstate, f, indent=2)
    except Exception:
        pass


def _set_account_quota_cooldown(username: str, cooldown_seconds: float = 3600.0) -> None:
    """Put an account that hit origin portal quota limit (429) into extended rest so other pollers continue."""
    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_lock_file = Path(__file__).parent / "polling_state.lock"
    try:
        with _polling_state_lock(polling_lock_file) as locked:
            if locked and polling_state_file.exists():
                with open(polling_state_file, "r") as f:
                    pstate = json.load(f)
                qc = pstate.setdefault("account_quota_cooldowns", {})
                qc[username] = time.time() + cooldown_seconds
                if pstate.get("active_poller") == username:
                    pstate["active_poller"] = None
                    pstate["active_poller_since"] = 0
                with open(polling_state_file, "w") as f:
                    json.dump(pstate, f, indent=2)
    except Exception:
        pass


async def _rotate_ip_and_refresh(page, customer: str, reason: str, username: str = "") -> None:
    """Reload the current page to recover after several CONSECUTIVE failure
    signals.

    "session expired (OFC API returned login HTML)" and persistent 429s during
    slot monitoring / booking are usually Cloudflare rate limits, not a real
    logout. Instead of relaunching the browser (which loses focus for slot
    finding), the page is simply reloaded  ->  the browser stays open the whole
    time. (Direct connection  ->  there is no IP to rotate.)
    """
    canonical_uid = (username or customer).strip()
    import asyncio
    from src.common.proxy import get_proxy_mode, rotate_account_ip
    is_direct = (get_proxy_mode() == "DIRECT_ONLY")
    ip_was_rotated = False

    with _ACCOUNT_FAILURES_LOCK:
        acct_fail = _ACCOUNT_IP_FAILURES.setdefault(canonical_uid.lower(), {"count": 0, "last_reason": ""})
        acct_fail["count"] += 1
        acct_fail["last_reason"] = reason
        cur_count = acct_fail["count"]

    _IP_FAILURE["count"] = cur_count
    _IP_FAILURE["last_reason"] = reason

    log.warning(f"[ALERT] Rate-limit / IP-ban error for '{customer}' ({canonical_uid}): {reason[:80]} "
                f"(consecutive #{cur_count}/{_ROTATE_AFTER_CONSECUTIVE_FAILURES})")

    r_low = reason.lower()
    is_portal_429 = "429" in r_low or "customer self-service" in r_low or "visa application home" in r_low
    is_hard_cf_block = ("1015" in r_low or "sorry, you have been blocked" in r_low) and not is_portal_429

    # DIRECT_ONLY MODE:
    # All accounts share the same local residential IP. When Microsoft or Cloudflare throttles (HTTP 429),
    # hammering the portal every 90s keeps resetting the rate limit window.
    # Instead, record fleet rate limit and execute escalating backoff (90s -> 180s -> 360s -> 600s).
    if is_direct:
        backoff = _record_fleet_rate_limit(reason)
        log.warning(f"[RATE-LIMIT] Microsoft/Cloudflare 429 rate limit active on DIRECT IP for '{customer}'.\n"
                    f"             Fleet backing off {backoff:.0f}s (making NO requests) to let server throttle window expire.\n"
                    f"             (Tip: If running on mobile hotspot, toggling Airplane mode for 3s gives a fresh IP for $0 free).")
        await asyncio.sleep(backoff)
        try:
            from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
            if await is_cloudflare_challenge(page):
                log.info("[WAIT] Cloudflare challenge present after cooldown - solving in place.")
                await solve_cloudflare_challenge(page, log)
        except Exception:
            pass
        return

    # PROXY MODE (Webshare / Decodo):
    # CLIENT RULE: Try up to 3 in-place attempts (reload, Turnstile auto-solve, clearance refresh)
    # on the current IP before rotating. If block persists after 3 tries -> Rotate to fresh IP!
    if cur_count < 3:
        log.info(f"[RETRY-IN-PLACE] Block signal on '{customer}' ({canonical_uid}) - Attempt {cur_count}/3 on current IP (reloading & clearing challenge)...")
        try:
            await portal_pacer.pace(log)
            await page.reload(wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(2)
            from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
            if await is_cloudflare_challenge(page):
                log.info(f"🤖 [CHALLENGE] Solving Cloudflare Turnstile on in-place attempt {cur_count}/3 for '{customer}'...")
                await solve_cloudflare_challenge(page, log)
                await asyncio.sleep(2)
            log.info(f"[OK] In-place reload attempt {cur_count}/3 completed for '{customer}'. Monitoring will retry on same connection.")
        except Exception as retry_err:
            log.warning(f"[RETRY-IN-PLACE] Reload attempt {cur_count}/3 error: {retry_err}")
        return

    # Attempt >= 3: Persistent block confirmed after 3 tries!
    log.warning(f"[PERSISTENT-BLOCK] 3 consecutive block attempts reached for '{customer}' ({canonical_uid})! Rotating to a brand fresh proxy IP...")
    with _ACCOUNT_FAILURES_LOCK:
        acct_fail["count"] = 0
    _IP_FAILURE["count"] = 0

    try:
        result = rotate_account_ip(canonical_uid, f"WAF/Rate-limit block (3 attempts failed): {reason[:60]}", force=True)
        if result:
            local_port, new_ip = result
            log.info(f"[PROXY-ROTATE] [OK] IP rotated for '{customer}' ({canonical_uid}) -> {new_ip} (forwarder local:{local_port})")
            ip_was_rotated = True
            await asyncio.sleep(1.5)
        else:
            log.warning(f"[PROXY-ROTATE] No fresh IP available from rotation for '{customer}' ({canonical_uid}); continuing.")
    except Exception as rot_err:
        log.warning(f"[PROXY-ROTATE] IP rotation failed for '{customer}' ({rot_err}); continuing.")

    # Check if the browser is currently in Cloudflare challenge or waiting room
    try:
        cur_url_before_nav = (page.url or "").lower()
        from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
        if await is_cloudflare_challenge(page):
            log.info("🤖 Browser has Cloudflare verification challenge - solving automatically...")
            await solve_cloudflare_challenge(page, log)
            return

        in_cf_transit = any(marker in cur_url_before_nav for marker in (
            "waitingroom", "challenge"
        ))
        if in_cf_transit and "chrome-error" not in cur_url_before_nav and "chromewebdata" not in cur_url_before_nav:
            log.info(f"[WAIT] Browser is in CF waiting room ({cur_url_before_nav[:70]}) - waiting for auto-redirect.")
            return

        # If IP was NOT rotated and page is already healthy on portal without any CF challenge, skip reload
        if not ip_was_rotated and not any(k in reason.lower() for k in ("cf", "1015", "403", "429", "rate limit", "just a moment", "challenge", "turnstile")):
            if "usvisascheduling.com" in cur_url_before_nav and not any(k in cur_url_before_nav for k in ["b2clogin", "chrome-error", "chromewebdata"]):
                content = (await page.content() or "").lower()
                is_hard_blocked = "sorry, you have been blocked" in content
                if not is_hard_blocked:
                    log.info("  Browser already healthy and parked on portal - skipping unnecessary reload.")
                    return
    except Exception:
        pass

    # Navigate to homepage to establish the fresh connection & solve any Cloudflare Turnstile
    try:
        await portal_pacer.pace(log)
        log.info("[ROUND-ROBIN] Refreshing portal homepage to establish fresh Cloudflare clearance...")
        await page.goto("https://www.usvisascheduling.com/en-US/",
                        wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

        # Auto-solve Turnstile if presented upon homepage load
        from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
        if await is_cloudflare_challenge(page):
            log.info("🤖 Cloudflare challenge presented on refresh - solving automatically...")
            await solve_cloudflare_challenge(page, log)
            await asyncio.sleep(2)

        log.info("[OK] Portal homepage refreshed & authenticated -> resuming monitoring.")
    except Exception as nav_err:
        log.warning(f"[WARN] Home navigation failed ({nav_err}); trying page.reload")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2)
            from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
            if await is_cloudflare_challenge(page):
                await solve_cloudflare_challenge(page, log)
            log.info("[OK] Page reloaded -> resuming monitoring after cooldown.")
        except Exception as reload_err:
            log.warning(f"[WARN] Page reload also failed: {reload_err}")


async def _background_heal_failed_candidate(
    page,
    customer: str,
    username: str,
    uid: str,
    trigger: dict,
    context: dict,
    state_file: Path,
) -> None:
    """Non-blocking async background self-healing for failed booking candidate.

    Runs concurrently in background WITHOUT holding the fleet lock.
    1. Rotates proxy IP to a fresh slot immediately if 403 / 429 / proxy_error.
    2. Validates session and clears Turnstile.
    3. Clears rest penalty (rest_until = 0).
    4. If slot window is still open and active, re-enqueues for circular retry!
    """
    status = context.get("status") or context.get("error") or ""
    log.info(
        f"[ASYNC HEAL] Starting background self-healing for '{customer}' ({uid}) "
        f"[reason: {status}] -- fleet lock released; other candidates can book!"
    )
    try:
        # Rotate proxy IP ONLY on explicit Cloudflare hard block ("Sorry, you have been blocked")
        # Keep sticky IP session for all normal transient misses, challenges, or 429s
        from src.auth.challenge import is_cloudflare_hard_block
        if await is_cloudflare_hard_block(page) or "blocked" in str(status).lower():
            from src.common.proxy import rotate_account_ip
            canonical_uid = (username or customer).strip()
            rot_res = rotate_account_ip(canonical_uid, f"one-shot heal: {status}", force=True)
            if rot_res:
                log.info(f"[ASYNC HEAL] [OK] Fresh proxy IP assigned to '{customer}' ({canonical_uid}) due to hard block: {rot_res[1]}")
            await asyncio.sleep(1.0)
        else:
            log.info(f"[ASYNC HEAL] Maintaining sticky IP session for '{customer}' (no hard block detected).")

        # Clear Turnstile / verify browser state
        try:
            from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
            if await is_cloudflare_challenge(page):
                log.info(f"[ASYNC HEAL] Solving Cloudflare Turnstile for '{customer}' in background...")
                await solve_cloudflare_challenge(page, log)
        except Exception as chal_err:
            log.warning(f"[ASYNC HEAL] Turnstile check error: {chal_err}")

        # Mark account ready again!
        _update_state(state_file, {
            "rest_until": 0,
            "booking_running": False,
            "last_healed_at": time.time(),
        })
        log.info(f"[ASYNC HEAL] [OK] Self-healing complete for '{customer}'. Account is 100% READY.")

        # Account healing complete: do NOT re-enqueue circularly into the active window.
        # Account is now idle and ready for future genuine slots or monitoring resumption.
        log.info(f"[ASYNC HEAL] Healed '{customer}' is idle and ready for future genuine slots or monitoring resumption (no circular retries).")

    except Exception as e:
        log.warning(f"[ASYNC HEAL] Background healing exception for '{customer}': {e}")


#  Proxy bandwidth saver -
# The Decodo traffic snapshot shows the bulk of proxy GB is wasted on:
#   ¢ heavy assets (images / media / fonts) on every portal navigation
#   ¢ third-party telemetry (Google update/safebrowsing/password-leak,
#     Microsoft telemetry, Cloudflare analytics beacons)
# None of these are needed for slot monitoring or booking, so they are
# aborted at the network layer. Cloudflare challenges
# (challenges.cloudflare.com) are NEVER touched  ->  blocking them would break
# the waiting room / turnstile.
_TRAFFIC_SAVER_BLOCKED_HOSTS = (
    "tb.events.data.microsoft.com",
    "js.monitor.azure.com",
    "applicationinsights.azure",
    "update.googleapis.com",
    "safebrowsing.googleapis.com",
    "passwordleakcheck-pa.googleapis.com",
    "content-autofill.googleapis.com",
    "optimizationguide-pa.googleapis.com",
    "mtalk.google.com",
    "android.clients.google.com",
    "clients2.google.com",
    "connectivitycheck.gstatic.com",
    "redirector.gvt1.com",
    "static.cloudflareinsights.com",
    "csp-reporting.cloudflare.com",
    "ip.decodo.com",
)

_TRAFFIC_SAVER_BLOCKED_TYPES = {"media"}


def _traffic_saver_enabled() -> bool:
    """Env switch: TRAFFIC_SAVER=1 enables asset blocking. Default disabled (0) to preserve all images, fonts, and portal logos."""
    return os.getenv("TRAFFIC_SAVER", "0").strip().lower() in ("1", "true", "yes")


async def _install_traffic_saver(context) -> None:
    """Abort telemetry hosts to cut proxy bandwidth. Never blocks portal images, logos, or fonts."""
    if not _traffic_saver_enabled():
        log.info("[NOTICE] Traffic saver disabled (TRAFFIC_SAVER=0)  ->  full assets and portal logos allowed.")
        return

    async def _route_handler(route):
        req = route.request
        try:
            url = req.url.lower()
            # 1. Block known telemetry / beacon hosts outright
            if any(host in url for host in _TRAFFIC_SAVER_BLOCKED_HOSTS):
                await route.abort()
                return
            # 2. Block heavy media types (audio/video), but NEVER images or fonts on usvisascheduling.com
            if "usvisascheduling.com" in url and req.resource_type in _TRAFFIC_SAVER_BLOCKED_TYPES:
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await context.route("**/*", _route_handler)
        log.info("Traffic saver active  ->  telemetry blocked (portal images, icons, and fonts intact).")
    except Exception as e:
        log.warning(f" Could not install traffic saver (non-fatal): {e}")


def _fleet_pause_enabled() -> bool:
    """Fleet-wide booking priority. Disable (env FLEET_BOOKING_PAUSE=0) when
    every account runs through its own proxy  ->  then there is no shared
    rate-limit bucket and full-time monitoring is safe."""
    return os.getenv("FLEET_BOOKING_PAUSE", "1").strip().lower() not in ("0", "false", "no")


def _fleet_booking_in_progress(my_customer: str) -> str:
    """Return the customer name of ANOTHER account currently booking, else ''.

    Scans all sibling state_*.json files. 'booking_running' is treated as
    stale (and ignored) when its trigger_timestamp is older than
    _FLEET_BOOKING_LEASE_SECONDS, so a crashed booking process cannot pause
    the whole fleet indefinitely.
    """
    if not _fleet_pause_enabled():
        return ""
    # FLEET slot-window: another account just matched a slot and is starting to
    # book. Pause RIGHT NOW (not when the booking runner consumes the trigger)
    # so the shared IP stays quiet during its critical POST window.
    slot_holder = _slot_window_busy_for(my_customer)
    if slot_holder:
        return slot_holder
    import glob
    now = time.time()
    for path in glob.glob(str(Path(__file__).parent / "state_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        if not s.get("booking_running"):
            # Note: An account in OFC_BOOKED_PENDING_CONSULAR waits for consular slots
            # independently. Other accounts continue normal monitoring without cascading stalls.
            continue
        other = str(s.get("customer_name") or Path(path).stem.replace("state_", ""))
        if other == my_customer:
            continue
        ts = s.get("trigger_timestamp") or 0
        try:
            if ts and now - float(ts) > _FLEET_BOOKING_LEASE_SECONDS:
                continue  # stale  ->  holder crashed mid-booking
        except (TypeError, ValueError):
            continue
        return other
    return ""
# --- Fleet slot-window 
# When ANY account's monitor matches a slot and queues a self-booking trigger,
# it opens this file so every OTHER account pauses its own polling within one
# POLL_INTERVAL  ->  before the booking runner has even consumed the trigger. The
# gap between "slot seen" and "booking started" is therefore covered, and the
# booking browser owns the shared IP during the critical POST window (prevents
# the parallel-request 429 / Network Error (XHR) storms). Closed the moment the
# booking attempt ends (success / partial / failure / throttle) so the fleet
# resumes monitoring immediately.
_SLOT_WINDOW_FILE = Path(__file__).parent / "slot_window.json"
_SLOT_WINDOW_TTL_SECONDS = 45  # crash-safe: a dead booker can never pause the fleet longer


def _open_slot_window(owner_customer: str, city: str, book_date: str) -> None:
    """Fleet-wide marker: '{owner_customer}' matched {city} {book_date} and is
    starting to book. Sibling runners pause immediately via
    _fleet_booking_in_progress()."""
    try:
        _SLOT_WINDOW_FILE.write_text(
            json.dumps({
                "customer": owner_customer,
                "city": city,
                "date": book_date,
                "opened_at": time.time(),
                "attempts": [],
                "booked": False,
                "notified": False,
            }),
            encoding="utf-8",
        )
        log.info(f"[PARK]  Fleet slot-window OPEN  ->  '{owner_customer}' booking {city} {book_date} (others pause).")
    except Exception as e:
        log.warning(f"Could not open fleet slot window: {e}")


def _record_slot_attempt(customer: str, status: str, reason: str, booked: bool = False) -> None:
    """Record a candidate account's booking attempt for consolidated reporting."""
    try:
        if not _SLOT_WINDOW_FILE.exists():
            return
        data = json.loads(_SLOT_WINDOW_FILE.read_text(encoding="utf-8"))
        attempts = data.get("attempts", [])
        attempts.append({
            "customer": customer,
            "status": status,
            "reason": str(reason or ""),
            "booked": booked,
            "ts": time.time()
        })
        data["attempts"] = attempts
        if booked:
            data["booked"] = True
        _SLOT_WINDOW_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _close_slot_window(force: bool = False) -> None:
    """Release the fleet slot-window so ALL accounts resume monitoring only when queue is empty."""
    try:
        if not force:
            from src.booking.coordinator import _read_coord
            coord = _read_coord()
            running = coord.get("running")
            queue = coord.get("queue") or []
            if running or len(queue) > 0:
                # Active accounts still queued or booking! Keep fleet monitoring paused.
                return
    except Exception:
        pass
    try:
        if _SLOT_WINDOW_FILE.exists():
            try:
                data = json.loads(_SLOT_WINDOW_FILE.read_text(encoding="utf-8"))
                # Send single consolidated Telegram alert if not booked and not yet notified
                if not data.get("notified", False) and not data.get("booked", False):
                    attempts = data.get("attempts", [])
                    if attempts:
                        city = data.get("city", "N/A")
                        date = data.get("date", "N/A")
                        scout = data.get("customer", "N/A")
                        attempt_names = ", ".join(dict.fromkeys(a.get("customer", "?") for a in attempts))
                        reasons_list = [f"{a.get('customer')}: {a.get('reason', a.get('status'))}" for a in attempts]
                        reasons_str = "; ".join(dict.fromkeys(reasons_list))
                        msg = (
                            f"⚠️ <b>Slot Found but Could Not Be Booked</b>\n"
                            f"----------------------------------------\n"
                            f"📍 <b>City:</b> {html.escape(str(city))}\n"
                            f"📅 <b>Date:</b> {html.escape(str(date))}\n"
                            f"🔎 <b>Detected by:</b> {html.escape(str(scout))}\n"
                            f"👤 <b>Attempted by:</b> {html.escape(str(attempt_names))}\n"
                            f"❌ <b>Result:</b> {html.escape(str(reasons_str[:250]))}\n\n"
                            f"🔄 <i>Monitoring resumed for all accounts.</i>"
                        )
                        tg_send(msg, emoji="")
                        data["notified"] = True
            except Exception as notify_err:
                log.debug(f"Consolidated notification note: {notify_err}")
            _SLOT_WINDOW_FILE.unlink(missing_ok=True)
            log.info("[WINDOW-CLOSED] Fleet slot-window CLOSED  ->  all accounts resume monitoring.")
    except Exception:
        pass


def _slot_window_busy_for(my_customer: str) -> str:
    """Return the customer holding the fleet slot-window, else '' (self-excluded)."""
    try:
        if not _SLOT_WINDOW_FILE.exists():
            return ""
        data = json.loads(_SLOT_WINDOW_FILE.read_text(encoding="utf-8"))
        opened_at = float(data.get("opened_at") or 0)
        if time.time() - opened_at > _SLOT_WINDOW_TTL_SECONDS:
            _close_slot_window()  # stale marker must never pause the fleet
            return ""
        holder = str(data.get("customer") or "")
        if holder and holder != my_customer:
            return holder
    except Exception:
        pass
    return ""

def _find_any_candidate(results: dict, config: dict) -> tuple[str, str]:
    """
    Relaxed fallback matcher (book-at-any-cost policy): pick the earliest
    available date across the account's configured cities AND within the
    account's configured date range. RULE: never book outside the user's
    cities or date range  ->  this fallback only relaxes the per-city minimum
    count, nothing else. Honors prevent_immediate (+3 days) and the
    minimum lead-time floor so phantom same-day listings never fire triggers.
    Returns (city, date) or ("", "").
    """
    allowed = set()
    for c in config.get("ofcCities", []):
        allowed.add("NEW DELHI" if c.upper() == "DELHI" else c.upper())

    cfg_start = config.get("ofcStartDate", "")
    cfg_end = config.get("ofcEndDate", "")

    lead_days = _min_slot_lead_days()
    if config.get("prevent_immediate"):
        lead_days = max(lead_days, 3)
    start_floor = (datetime.today() + timedelta(days=lead_days)).strftime("%Y-%m-%d")
    if not cfg_start or cfg_start < start_floor:
        cfg_start = start_floor

    candidates = []
    for city, dates in results.items():
        if allowed and city.upper() not in allowed:
            continue
        if not isinstance(dates, list):
            continue
        for d in dates:
            date_str = str(d.get("Date", ""))[:10]
            if not date_str or date_str < cfg_start:
                continue
            if cfg_end and date_str > cfg_end:
                continue
            candidates.append((date_str, city))

    if not candidates:
        return "", ""

    candidates.sort()
    ordered = mid_out_order(candidates)
    best_date, best_city = ordered[0]
    return best_city, best_date


async def recover_session(page, customer: str, username: str):
    log.info(f" Attempting in-place session recovery for '{customer}' ({username})...")

    # 1. Get credentials
    if not ACCOUNTS_FILE.exists():
        log.error("accounts.json not found for recovery.")
        return False

    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        creds = next((c for c in raw if c.get("username") == username), None)
        if not creds:
            log.error(f"Credentials not found for username '{username}'.")
            return False
        password = creds.get("password", "")
    except Exception as e:
        log.error(f"Error reading accounts.json: {e}")
        return False

    fastcaptcha = os.getenv("FASTCAPTCHA_API_KEY", "")
    if not fastcaptcha:
        log.warning("FASTCAPTCHA_API_KEY missing for recovery. Captchas will fail.")

    # 2. Trigger redirect by navigating back to the pristine home page.
    #    Uses goto_with_retry which rotates IP immediately on tunnel/connection
    #    failures (ERR_TUNNEL_CONNECTION_FAILED, ERR_CONNECTION_CLOSED, etc.)
    #    instead of waiting 45s for a timeout.
    try:
        from src.login_runner import goto_with_retry
        log.info("Navigating to home page to trigger session validation...")
        await goto_with_retry(page, "https://www.usvisascheduling.com/en-US/", log, timeout=30000)
    except Exception as nav_err:
        log.error(f"Recovery navigation failed: {nav_err}")
        return False

    # 3. Handle waiting room  ->  retry on the SAME IP first (client rule: never
    #    rotate on the first failure while the account ran well). Only if the
    #    block persists does recovery escalate to IP rotation.
    try:
        await wait_for_waiting_room(page, log, timeout_minutes=120)
    except Exception as e:
        err_str = str(e).lower()
        if "cloudflare" in err_str or "blocked" in err_str:
            log.warning(f"Cloudflare blocked during recovery waiting room  ->  retrying once on same IP...")
            try:
                from src.login_runner import goto_with_retry
                await goto_with_retry(page, "https://www.usvisascheduling.com/en-US/", log, timeout=30000)
                await wait_for_waiting_room(page, log, timeout_minutes=120)
            except Exception as retry_err:
                err_str2 = str(retry_err).lower()
                if "cloudflare" in err_str2 or "blocked" in err_str2:
                    log.warning(f"Cloudflare block persisted after same-IP retry  ->  only NOW rotating IP and retrying...")
                    try:
                        from src.common.proxy import rotate_account_ip, get_proxy_mode
                        if get_proxy_mode() != "DIRECT_ONLY":
                            rotate_account_ip(username or customer, "Persistent Cloudflare WAF block (recovery waiting room)")
                    except Exception:
                        pass
                    try:
                        from src.login_runner import goto_with_retry
                        await goto_with_retry(page, "https://www.usvisascheduling.com/en-US/", log, timeout=30000)
                        await wait_for_waiting_room(page, log, timeout_minutes=120)
                    except Exception as retry_err2:
                        log.error(f"Recovery waiting room failed after IP rotation: {retry_err2}")
                        return False
                else:
                    log.error(f"Recovery waiting room retry failed: {retry_err}")
                    return False
        else:
            log.error(f"Error waiting for waiting room during recovery: {e}")
            return False

    # 4. Authenticated Portal Check
    # If the browser is on the portal and shows authenticated elements (or did not redirect to B2C login),
    # the session is alive and valid. We don't force login when the browser is already logged in!
    log.info("Checking if session is already authenticated on portal...")
    for redirect_check in range(5):
        cur_url = (page.url or "").lower()
        if any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
            break

        if "usvisascheduling.com" in cur_url:
            try:
                is_authenticated = await page.evaluate(r"""
                    () => {
                        const url = window.location.href.toLowerCase();
                        if (!url.includes("usvisascheduling.com") || url.includes("/signin") || url.includes("b2clogin")) {
                            return false;
                        }
                        const bodyText = (document.body ? document.body.innerText : "").toLowerCase();
                        const hasPortalKeywords = bodyText.includes("schedule appointment") || 
                                                  bodyText.includes("reschedule appointment") || 
                                                  bodyText.includes("appointment manager") || 
                                                  bodyText.includes("manage appointments") ||
                                                  bodyText.includes("cancel appointment") ||
                                                  bodyText.includes("close and start new application") ||
                                                  bodyText.includes("emergency request") ||
                                                  bodyText.includes("messages");
                        const hasSignOut = Boolean(document.querySelector("a[href*='logout' i], a[href*='signout' i], a[href*='externallogout' i], a[href*='logoff' i]"));
                        const hasSignIn = Boolean(document.querySelector("a[href*='externallogin' i], a[href*='/signin' i]"));
                        return hasSignOut || (hasPortalKeywords && !hasSignIn);
                    }
                """)
                if is_authenticated:
                    log.info(f"✅ Fast recovery: Verified browser is ALREADY authenticated on portal for '{customer}'. Resuming slot monitoring immediately.")
                    # Re-persist static identifiers if missing
                    try:
                        from src.common.static_identifiers import get_account_identifiers, save_account_identifiers
                        p, a = get_account_identifiers(username)
                        if not p or not a:
                            from src.api.extractor import extract_ids_from_page
                            dp, da = await extract_ids_from_page(page)
                            if dp and da:
                                save_account_identifiers(username, dp, da)
                    except Exception:
                        pass
                    try:
                        await page.evaluate("window.__browserSessionExpired = false")
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
        await asyncio.sleep(2)

    cur_url = (page.url or "").lower()
    if not any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
        # Expired session did not auto-redirect  ->  explicitly navigate to SignIn
        log.info("Session expired: explicitly navigating to Sign-In page to re-authenticate...")
        try:
            from src.login_runner import goto_with_retry
            await goto_with_retry(page, "https://www.usvisascheduling.com/en-US/SignIn", log, timeout=30000)
            await wait_for_waiting_room(page, log, timeout_minutes=120)
        except Exception as signin_err:
            log.warning(f"SignIn navigation warning: {signin_err}")

    # 5. Perform Login
    success = False
    for attempt in range(1, 4):
        log.info(f"Recovery login attempt {attempt}/3")
        success = await login(page, username, password, fastcaptcha, log)
        if success:
            break
        try:
            log.warning("Recovery login failed; reusing the live browser session by navigating back to the portal home page.")
            await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=30000)
        except Exception as nav_err:
            log.warning(f"Recovery re-navigation failed but browser stays alive: {nav_err}")
        await asyncio.sleep(3)

    if not success:
        log.error("Recovery login failed.")
        return False

    # 6. Security Questions
    try:
        if not await handle_security_question(page, username, log):
            log.error("Security question failed during recovery.")
            return False
    except Exception as e:
        log.error(f"Error during security questions: {e}")
        return False

    log.info(" Security questions passed. Waiting for portal redirect...")
    try:
        await page.wait_for_url("**/*usvisascheduling.com/en-US*", timeout=30_000, wait_until="commit")
        log.info(" In-place session recovery successful!")
    except Exception as e:
        log.warning(f"Timeout waiting for portal redirect after login: {e}")

    # Wait for waiting room one more time in case it pops up after redirect
    try:
        await wait_for_waiting_room(page, log, timeout_minutes=120)
    except Exception as e:
        log.error(f"Error checking waiting room after security questions: {e}")

    # Clear the browser flag
    try:
        await page.evaluate("window.__browserSessionExpired = false")
    except Exception:
        pass

    # Ensure parked on homepage for stable monitoring
    try:
        cur = (page.url or "").lower()
        if "usvisascheduling.com" in cur and not cur.rstrip("/").endswith("/en-us"):
            log.info("[RECOVERY] Parking on homepage for stable monitoring...")
            await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=30000)
        # Re-heal after navigation (clear any waiting room that popped up)
        try:
            await heal_tab_for_booking(page, log, reason="recovery post-login heal")
        except Exception:
            pass
        log.info("[RECOVERY] Browser parked on homepage -- ready for monitoring.")
    except Exception as nav_err:
        log.warning(f"[RECOVERY] Could not park on homepage: {nav_err}")

    return True


async def _recover_with_retry(page, customer: str, username: str, attempts: int = 3) -> bool:
    """
    Resilient session recovery: tries full recovery up to `attempts` times
    with backoff instead of immediately killing the process. Recovery events
    are LOG-ONLY per client request (Telegram reserved for monitoring start,
    slot found/matched and booking outcomes only).

    CLIENT RULE: NEVER rotate the IP on the FIRST failure while the account
    has been running well. First attempt retries on the SAME IP; only after
    REPEATED failures does recovery rotate the IP (the block is then likely
    IP-based).
    """
    for attempt in range(1, attempts + 1):
        log.info(f"  Session recovery attempt {attempt}/{attempts} for '{customer}'...")
        try:
            if await recover_session(page, customer, username):
                log.info(f" Session recovered for '{customer}' on attempt {attempt}. Slot monitoring resumed.")
                return True
        except Exception as exc:
            err_lower = str(exc).lower()
            # If the browser itself crashed, don't keep retrying
            if "target closed" in err_lower or "browser has been closed" in err_lower:
                log.error(f"Browser closed during recovery  ->  aborting retry loop.")
                return False
            log.error(f"Recovery attempt {attempt} error: {exc}")

        # Attempts 1 and 2: keep the SAME stable IP (retry reload, relogin, Turnstile solve).
        # Only after 3 repeated failures does recovery escalate to rotating the IP!
        if attempt >= 3:
            try:
                from src.common.proxy import rotate_account_ip, get_proxy_mode
                if get_proxy_mode() == "DIRECT_ONLY":
                    log.info("[DIRECT_ONLY] Running on direct residential network without forwarder -> continuing on host connection.")
                else:
                    result = rotate_account_ip(username or customer, "Cloudflare WAF block (session recovery 3/3)")
                    if result:
                        log.info(f" Rotated IP / Switched mode after 3 repeated recovery failures  ->  retrying on fresh connection.")
                    else:
                        log.warning(f" IP rotation returned no result  ->  retrying on same connection.")
            except Exception as rot_err:
                log.warning(f"IP rotation failed (non-fatal): {rot_err}")
            await asyncio.sleep(5)  # short pause for IP to stabilize
        else:
            cooldown = 15  # short same-IP pause  ->  transient WAF clears fast
            log.warning(f"Recovery attempt {attempt}/{attempts} failed for '{customer}'  ->  retrying on the SAME IP (attempt {attempt+1}/{attempts}) in {cooldown}s...")
            await asyncio.sleep(cooldown)
    return False


async def _broadcast_results(results: dict, customer: str):
    """Trigger every idle RESERVED_BOOKING account matching the polled dates."""
    if not ACCOUNTS_FILE.exists():
        return

    try:
        all_accounts = json.loads(
            ACCOUNTS_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(all_accounts, list):
            log.error("accounts.json must contain a JSON list.")
            return

        triggered_count = 0
        remote_mode = bool(
            os.getenv("REMOTE_TRIGGER_URL", "").strip()
        )
        remote_trigger_cooldown = int(
            os.getenv("REMOTE_TRIGGER_COOLDOWN_SECONDS", "300")
        )
        max_cross_triggers = int(
            os.getenv("MAX_CROSS_TRIGGERS", "5")
        )
        stagger_min = float(
            os.getenv("CROSS_TRIGGER_STAGGER_MIN_SECONDS", "0.2")
        )
        stagger_max = float(
            os.getenv("CROSS_TRIGGER_STAGGER_MAX_SECONDS", "0.5")
        )
        if stagger_max < stagger_min:
            stagger_max = stagger_min

        matched_candidates = []
        for acct_config in all_accounts:
            acct_customer = str(
                acct_config.get("customer_name", "")
            ).strip()
            acct_username = str(
                acct_config.get("username", "")
            ).strip()
            role = str(
                acct_config.get("role", "")
            ).strip().upper()

            if not acct_customer or not acct_username:
                continue

            # Only trigger accounts that are actually enabled in accounts.json!
            if acct_config.get("enabled", True) in (False, 0, "0", "false", "no", "off"):
                continue

            if acct_config.get("booking_status") == "BOOKED SUCCESS" or acct_config.get("role") == "COMPLETED" or acct_config.get("booked"):
                continue

            # Only reserved booking accounts may receive cross-account triggers.
            if role != "RESERVED_BOOKING":
                continue

            matched, matched_city, earliest_date = (
                _match_polled_ofc_dates(results, acct_config)
            )

            if not matched:
                continue

            acct_uid = safe_id(acct_username)
            acct_state_file = (
                Path(__file__).parent / f"state_{acct_uid}.json"
            )
            acct_state = _read_state(acct_state_file)

            # If account is already holding an OFC appointment, waiting for consular,
            # or already completed/disabled, it CANNOT be triggered for an OFC slot!
            if (
                acct_state.get("waitingForConsular")
                or acct_config.get("booking_status") in ("OFC_BOOKED_PENDING_CONSULAR", "BOOKED SUCCESS")
                or acct_config.get("role") == "COMPLETED"
                or not acct_config.get("enabled", True)
            ):
                log.info(f"[SKIP] Skipping {acct_customer}: Account holding OFC, already booked, or disabled.")
                continue

            if not remote_mode:
                hb = float(acct_state.get("booking_runner_heartbeat") or 0)
                is_alive = bool(acct_state.get("booking_runner_alive") and (time.time() - hb < 45.0))
                if not is_alive:
                    log.info(
                        f"[SKIP] Skipping {acct_customer}: booking runner is NOT active/running "
                        f"(heartbeat {int(time.time() - hb)}s ago). Only live accounts can be triggered."
                    )
                    continue

            if not remote_mode and acct_state.get("booking_running"):
                log.info(
                    f"[SKIP] Skipping {acct_customer}: booking is already running."
                )
                continue

            if not remote_mode and acct_state.get("pending"):
                log.info(
                    f"[SKIP] Skipping {acct_customer}: trigger already pending."
                )
                continue

            # Fair Round-Robin & Real-time Session Registry Check:
            # 1. LIVE accounts (rank 0) over unverified / stale accounts (rank 1)
            # 2. Least attempts first (attempts_count: 0 attempts before 1, 1 before 2...)
            # 3. Earliest last attempt timestamp (last_attempt_ts) for round-robin rotation
            # 4. Never alphabetical name sorting (prevents Dinesh & Cherkala from starving other accounts)
            sess_info = session_radar.get_account_session_info(acct_uid)
            is_live_rank = 0 if sess_info.get("is_live") else 1
            is_self = 0 if acct_customer == customer else 1
            attempts_count = int(acct_state.get("booking_attempts_count", 0))
            last_attempt_ts = float(acct_state.get("last_booking_attempt_ts", 0))
            cfg_end = str(acct_config.get("ofcEndDate", "9999-99-99"))

            matched_candidates.append({
                "sort_key": (is_live_rank, attempts_count, last_attempt_ts, is_self, earliest_date, cfg_end),
                "sess_info": sess_info,
                "acct_config": acct_config,
                "acct_customer": acct_customer,
                "acct_username": acct_username,
                "acct_uid": acct_uid,
                "acct_state_file": acct_state_file,
                "acct_state": acct_state,
                "matched_city": matched_city,
                "earliest_date": earliest_date,
                "attempts_count": attempts_count,
            })

        matched_candidates.sort(key=lambda c: c["sort_key"])


        for cand in matched_candidates:
            if max_cross_triggers > 0 and triggered_count >= max_cross_triggers:
                log.info(
                    f"[SKIP] Skipping {cand['acct_customer']}: maximum cross-account "
                    f"triggers ({max_cross_triggers}) reached."
                )
                break

            acct_config = cand["acct_config"]
            acct_customer = cand["acct_customer"]
            acct_username = cand["acct_username"]
            acct_uid = cand["acct_uid"]
            acct_state_file = cand["acct_state_file"]
            acct_state = cand["acct_state"]
            matched_city = cand["matched_city"]
            earliest_date = cand["earliest_date"]

            action_mode = str(
                acct_config.get("action_mode", "SNIPER")
            ).strip().upper()
            action_type = (
                "RESCHEDULE_FULL"
                if action_mode == "RESCHEDULE_FULL"
                else "SNIPER"
            )

            trigger_key = (
                f"background|{acct_uid}|{matched_city.upper()}|"
                f"{earliest_date}|{action_type}"
            )

            # In remote mode, suppress only repeated sends for the same slot
            # and account. A different slot may still trigger immediately.
            if remote_mode:
                last_trigger_key = str(
                    acct_state.get("trigger_key", "")
                )
                last_remote_trigger = float(
                    acct_state.get("remote_trigger_sent_at", 0)
                    or acct_state.get("trigger_timestamp", 0)
                    or 0
                )

                if (
                    last_trigger_key == trigger_key
                    and last_remote_trigger
                    and time.time() - last_remote_trigger
                    < remote_trigger_cooldown
                ):
                    remaining = int(
                        remote_trigger_cooldown
                        - (time.time() - last_remote_trigger)
                    )
                    log.info(
                        f"[SKIP] Skipping {acct_customer}: same remote trigger "
                        f"was sent recently ({max(remaining, 0)}s remaining)."
                    )
                    continue

            # Monotonic coordinator check: deduplicate duplicate triggers
            slot_id = slot_fingerprint(matched_city, earliest_date)
            if not booking_coordinator.can_dispatch_trigger(acct_uid, slot_id):
                log.info(
                    f"[SKIP] Coordinator suppressed duplicate/recent trigger for {acct_customer} on slot {slot_id}"
                )
                continue

            ts_trig = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cand_sess = cand.get("sess_info", {})
            cand_status = cand_sess.get("status", "UNKNOWN")
            cand_age = cand_sess.get("age_minutes", 0.0)
            cand_method = cand_sess.get("method", "NONE")
            cross_box = (
                f"\n{ANSI_ORANGE}"
                f"================================================================================\n"
                f"[DISPATCH] [CROSS-ACCOUNT TRIGGER] Slot Shared to Reserved Account | {ts_trig}\n"
                f"--------------------------------------------------------------------------------\n"
                f"Polled / Found by: {customer}\n"
                f"Target Booking Account: {acct_customer} ({acct_username})\n"
                f"Session Health: {cand_status} (Age: {cand_age}m | Method: {cand_method})\n"
                f"City: {matched_city}\n"
                f"Available Date: {earliest_date}\n"
                f"Priority: #{triggered_count+1}\n"
                f"Action: {action_type}\n"
                f"================================================================================{ANSI_RESET}\n"
            )
            print(cross_box, flush=True)
            log.info(
                f"[SCOUT DISPATCH] Scout '{customer}' discovered slot {matched_city} ({earliest_date}) "
                f"-> Dispatched to Target Booking Account: '{acct_customer}' (Priority #{triggered_count+1} | Session: {cand_status} {cand_age}m)"
            )

            from src.common.static_identifiers import get_account_identifiers
            p_id, a_id = get_account_identifiers(acct_username, acct_config)

            trigger_updates = {
                "booking_running": False,
                "pending": True,
                "priority": triggered_count + 1,
                "source": "cross_account",
                "is_scout_self_book": False,
                "trigger_timestamp": time.time(),
                "trigger_key": trigger_key,
                "action_type": action_type,
                "primaryId": p_id or "",
                "appd": a_id or "",
                "isReschedule": acct_config.get("isReschedule", results.get("isReschedule")),
                "ofcCities": [matched_city],
                "ofcPriorityCity": matched_city,
                "ofcStartDate": acct_config.get("ofcStartDate", ""),
                "ofcEndDate": acct_config.get("ofcEndDate", ""),
                "consularCities": acct_config.get("consularCities", []),
                "consularPriorityCity": acct_config.get("consularPriorityCity", ""),
                "consularStartDate": acct_config.get("consularStartDate", ""),
                "consularEndDate": acct_config.get("consularEndDate", ""),
                "customer_name": acct_customer,
                "prevent_immediate": acct_config.get("prevent_immediate", False),
                "multiPerson": acct_config.get("multiPerson", False),
                "cached_booking": {
                    "city": matched_city.upper(),
                    "date": earliest_date,
                    "phase1_token": (
                        (results.get("tokens") or {}).get(matched_city.upper())
                        or (results.get("tokens") or {}).get(matched_city)
                        or ""
                    ),
                    "primaryId": p_id or "",
                    "appd": a_id or "",
                    "isReschedule": acct_config.get("isReschedule", results.get("isReschedule")),
                    "candidates": [
                        {"city": matched_city, "date": earliest_date}
                    ],
                    "captured_at": time.time(),
                    "source": "cross_account",
                },
            }

            # Fair rotation: record attempt count and timestamp in account state
            next_attempts = int(cand.get("attempts_count", 0)) + 1
            trigger_updates["booking_attempts_count"] = next_attempts
            trigger_updates["last_booking_attempt_ts"] = time.time()

            # Queue the booking first.
            _update_state(acct_state_file, trigger_updates)

            booking_coordinator.submit_trigger(
                account_uid=acct_uid,
                trigger_key=trigger_key,
                slot_info={
                    "city": matched_city,
                    "date": earliest_date,
                    "priority": triggered_count + 1,
                    "source": "cross_account",
                },
                max_age_seconds=CACHED_TRIGGER_MAX_AGE_SECONDS,
            )
            triggered_count += 1

        if triggered_count:
            log.info(
                f" Triggered {triggered_count} eligible booking account(s)."
            )

    except Exception as e:
        log.error(
            f"Error cross-triggering accounts: {e}",
            exc_info=True,
        )


def _extract_actual_error(result: dict) -> str:
    """Extract real raw server/API/DOM error message."""
    if not result or not isinstance(result, dict):
        return "Unknown error"
    if result.get("error"):
        return str(result["error"])
    results = result.get("results") or {}
    for city, val in results.items():
        if isinstance(val, dict) and val.get("error"):
            return f"{city}: {val['error']}"
    return "Unknown server response"


def _looks_like_cf_api_block(result: dict) -> bool:
    """Detect a genuine Cloudflare API-level / WAF rate limit block based on actual server responses.

    Cloudflare can block at two levels:
      1. Per-fetch (proxy IP rate-limited): each /api/v1/... fetch returns HTTP 429
         or a challenge/block HTML body (e.g. Error 1015, 'you are being rate limited', 'Attention Required').
      2. Whole-page: the tab itself landed on a CF block page
         ('in_cf_block': true -> "sorry, you have been blocked").
    """
    if not result or not isinstance(result, dict):
        return False
    if result.get("in_challenge"):
        return False
    if result.get("in_cf_block"):
        return True
    top_error = str(result.get("error", "")).lower()
    cf_markers = ("1015", "rate limit", "rate limited", "rate_limited", "you are being rate limited", "http 429")
    if top_error and any(m in top_error for m in cf_markers):
        return True
    results = result.get("results") or {}
    if not results:
        return False
    cf_block = 0
    for value in results.values():
        if isinstance(value, dict):
            val_err = str(value.get("error", "")).lower()
            if any(m in val_err for m in cf_markers):
                cf_block += 1
    return cf_block > 0


def _looks_like_expired_session(result: dict) -> bool:
    """Return True only on an UNMISTAKABLE session-expiry signal.

    Only two things prove the session is actually dead:
      * a city result tagged 'session expired' or 401 Unauthorized / b2clogin, or
      * result['needs_recovery'] (the browser page itself navigated away onto a login page).
    """
    if not result or not isinstance(result, dict):
        return False
    if result.get("in_waiting_room"):
        return False
    if result.get("in_challenge"):
        return False
    if result.get("needs_recovery"):
        return True

    cf_markers = (
        "1015",
        "rate limit",
        "rate limited",
        "rate_limited",
        "challenge",
        "blocked",
        "http 429",
        "http 403",
        "just a moment",
        "turnstile",
        "cf-chl",
    )

    results = result.get("results") or {}
    if not results:
        error = str(result.get("error", ""))
        err_lower = error.lower()
        if "waiting room" in err_lower or "waitingroom" in err_lower:
            return False
        if any(m in err_lower for m in cf_markers):
            return False
        if any(k in err_lower for k in ("session expired", "401 unauthorized", "b2clogin", "signin")):
            return True
        return False
    expired = 0
    for value in results.values():
        if isinstance(value, dict):
            val_err = str(value.get("error", "")).lower()
            if "waiting room" in val_err or "waitingroom" in val_err:
                return False
            if any(m in val_err for m in cf_markers):
                return False
            if any(k in val_err for k in ("session expired", "401 unauthorized", "b2clogin")):
                expired += 1
    return expired > 0


def _min_booking_slots() -> int:
    """Minimum total EntriesAvailable required before any booking is allowed.
    Defaults to 1 so even a single available slot triggers immediate booking."""
    try:
        return max(1, int(os.getenv("MIN_BOOKING_SLOTS", "1")))
    except (TypeError, ValueError):
        return 1


def _book_anyhow_enabled() -> bool:
    """BOOK-ANYHOW policy (default ON).

    When the background monitor has already matched an in-range slot, the
    pre-booking verification must NOT veto the booking attempt: verification
    is a fresh fetch that can be skewed by an IP-rotation race or a Cloudflare
    soft-block, while the real booking attempt is the ground truth. Set
    BOOK_ANYHOW=0 to restore the old conservative behaviour.
    """
    return os.getenv("BOOK_ANYHOW", "1").strip().lower() not in ("0", "false", "no")


async def _count_available_slots(page, city: str, date_str: str, token):
    """
    Query the phase-2 official endpoint (get-family-ofc-schedule-entries) for
    the given city/date and return the summed EntriesAvailable across all
    time-level entries. Returns None when the count cannot be determined.
    Requires the Token produced by the phase-1 get-family-ofc-schedule-days call
    (captured by fetch_dates_via_browser and returned in result["tokens"]).
    Uses browserless VisaScheduleApiClient first, falling back to page.evaluate on error.
    """
    try:
        session_ctx = await extract_session_context(page, "booking_user")
        async with VisaScheduleApiClient(session_ctx) as api_client:
            cnt = await api_client.count_available_slots(city, date_str, token)
            if cnt is not None:
                return cnt
    except Exception as exc:
        log.debug(f"Browserless count_available_slots failed: {exc}. Trying in-page evaluate fallback.")

    js = r"""
        async ({city, dateStr, token}) => {
            const headers = {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "x-requested-with": "XMLHttpRequest"
            };
            // Locate appd UUID exactly like fetch_dates_via_browser does.
            let appd = null;
            const uuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
            for (const key of Object.keys(window)) {
                if (typeof window[key] === 'string' && uuidRe.test(window[key])) { appd = window[key]; break; }
            }
            if (!appd) {
                for (const script of document.querySelectorAll("script:not([src])")) {
                    const m = (script.textContent || '').match(uuidRe);
                    if (m) { appd = m[0]; break; }
                }
            }
            if (!appd && document.documentElement) {
                const pageUuids = document.documentElement.innerHTML.match(new RegExp(uuidRe.source, 'gi'));
                if (pageUuids && pageUuids.length) appd = pageUuids[0];
            }
            if (!appd) return { error: "no appd" };

            // Match phase-1 exactly: the portal scopes the Token to the
            // reschedule vs. fresh-booking context. Sending the wrong mode
            // here makes the backend return an EMPTY entry list (a false 0)
            // even when the day genuinely has slots, which used to block
            // booking outright.
            const isRescheduleUrl = window.location.href.toLowerCase().includes("reschedule");
            const url = `/en-US/custom-actions/?route=/api/v1/schedule-group/get-family-ofc-schedule-entries&appd=${appd}&cacheString=${Date.now()}`;
            const payload = {
                primaryId: null,
                applications: null,
                scheduleDayId: null,
                scheduleEntryId: "",
                postId: null,
                Token: token,
                Date: `${dateStr}T00:00:00.000Z`,
                isReschedule: isRescheduleUrl ? "true" : "false"
            };
            const res = await fetch(url, {
                method: 'POST',
                headers,
                body: `parameters=${encodeURIComponent(JSON.stringify(payload))}`
            });
            const text = await res.text();
            let data;
            try {
                data = JSON.parse(text);
            } catch (parseErr) {
                // The portal sometimes appends trailing content after the JSON
                // payload (HTML, whitespace, BOM). res.json() throws on that,
                // so extract the first complete JSON object ourselves.
                const m = text.match(/\{[\s\S]*\}/);
                if (m) {
                    try { data = JSON.parse(m[0]); } catch (e2) { data = null; }
                }
                if (!data) {
                    const snippet = text.substring(0, 150).replace(/[\r\n]+/g, " ");
                    return { error: `Not JSON. Snippet: ${snippet}` };
                }
            }
            if (data.HasError || (data.ErrorString && data.ErrorString !== "<br>")) {
                return { error: String(data.ErrorString || "backend error") };
            }
            const entries = data.ScheduleEntries || [];
            let total = 0;
            for (const e of entries) {
                const n = Number(e.EntriesAvailable);
                if (!isNaN(n)) total += n;
            }
            return { total };
        }
    """
    try:
        result = await page.evaluate(js, {"city": city.upper(), "dateStr": date_str, "token": token})
        if not isinstance(result, dict) or "error" in result:
            return None
        return int(result.get("total", 0))
    except Exception as exc:
        log.warning(f"Entries-count fetch failed for {city} {date_str}: {exc}")
        return None

async def _fetch_consular_dates(page, trigger: dict, log: logging.Logger):
    """Fetch available CONSULAR dates for each configured consular city using the
    official get-family-consular-schedule-days endpoint.

    Uses the browserless VisaScheduleApiClient first, falling back to in-page evaluation
    only if session extraction or HTTP connection cannot proceed.
    """
    # Normalize + de-duplicate requested consular cities to known map entries.
    cities = []
    for raw in (trigger.get("consularCities") or []):
        cu = normalize_city(str(raw))
        if cu in CONSULAR_LOCATION_MAP and cu not in cities:
            cities.append(cu)
    if not cities:
        return {"success": True, "result": {}}

    start = trigger.get("consularStartDate", "")
    end = trigger.get("consularEndDate", "")

    # Retrieve static identifiers to avoid dynamic DOM search failures
    username = trigger.get("username") or trigger.get("customer_name") or "unknown"
    from src.common.static_identifiers import get_account_identifiers
    static_primary_id, static_appd = get_account_identifiers(username, trigger)
    static_appd = static_appd or trigger.get("appd", "")
    static_primary_id = static_primary_id or trigger.get("primaryId", "")

    # Browser evaluation (executes inside authenticated Chromium session with Cloudflare clearance)
    js = r"""
    async ({cities, cityMap, start, end, staticAppd, staticPrimaryId}) => {
        const headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest"
        };
        const uuidFull = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
        const primaryIdRe = new RegExp("['\"]?(?:primaryId|applicantUuid|ApplicationID)['\"]?\\s*[:=]\\s*['\"](" + uuidFull + ")['\"]", "gi");
        const appdRe = new RegExp("['\"]?(?:contactId|appd|scheduleGroupId|familyId)['\"]?\\s*[:=]\\s*['\"](" + uuidFull + ")['\"]", "gi");
        let primaryId = staticPrimaryId || "";
        let appd = staticAppd || "";

        if (!primaryId || !appd) {
            for (const s of Array.from(document.querySelectorAll("script:not([src])"))) {
                const n = s.textContent || "";
                if (!n.includes("-")) continue;
                if (!primaryId) { const m = primaryIdRe.exec(n); if (m && m[1]) primaryId = m[1]; }
                if (!appd) { const m = appdRe.exec(n); if (m && m[1]) appd = m[1]; }
                if (primaryId && appd) break;
            }
        }
        const plainUuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
        if (!appd) {
            for (const key of Object.keys(window)) {
                if (typeof window[key] === 'string' && plainUuidRe.test(window[key])) { appd = window[key]; break; }
            }
        }
        if (!appd && document.documentElement) {
            const pageUuids = document.documentElement.innerHTML.match(new RegExp(plainUuidRe.source, 'gi'));
            if (pageUuids && pageUuids.length) appd = pageUuids[0];
        }
        if (!appd && !primaryId) return { success: false, error: "no appd/primaryId on page" };
        const results = {};
        const allResults = {};
        for (const city of cities) {
            try {
                const url = `/en-US/custom-actions/?route=/api/v1/schedule-group/get-family-consular-schedule-days&appd=${appd}&cacheString=${Date.now()}`;
                const payload = {
                    primaryId: primaryId || null,
                    applications: primaryId ? [primaryId] : null,
                    scheduleDayId: null,
                    scheduleEntryId: "",
                    postId: cityMap[city],
                    isReschedule: "false"
                };
                const res = await fetch(url, { method: 'POST', headers, body: `parameters=${encodeURIComponent(JSON.stringify(payload))}` });
                const text = await res.text();
                let data;
                try { data = JSON.parse(text); }
                catch (parseErr) { const m = text.match(/\{[\s\S]*\}/); if (m) { try { data = JSON.parse(m[0]); } catch (e2) { data = null; } } }
                if (!data) { results[city] = { error: `Not JSON: ${String(text).substring(0, 120)}` }; continue; }
                if (data.HasError || (data.ErrorString && data.ErrorString !== "<br>")) { results[city] = { error: String(data.ErrorString || "backend error") }; continue; }
                let dates = [];
                let allDates = [];
                for (const d of (data.ScheduleDays || [])) {
                    const ds = String(d.Date || d.date || "").slice(0, 10);
                    if (!ds) continue;
                    if (!allDates.includes(ds)) allDates.push(ds);
                    if (start && ds < start) continue;
                    if (end && ds > end) continue;
                    if (!dates.includes(ds)) dates.push(ds);
                }
                dates.sort();
                allDates.sort();
                results[city] = dates;
                allResults[city] = allDates;
            } catch (err) {
                results[city] = { error: String(err.message || err) };
            }
        }
        return { success: true, result: results, all_result: allResults };
    }
    """

    try:
        res = await page.evaluate(js, {
            "cities": cities,
            "cityMap": CONSULAR_LOCATION_MAP,
            "start": start,
            "end": end,
            "staticAppd": static_appd,
            "staticPrimaryId": static_primary_id,
        })
        if isinstance(res, dict) and res.get("success"):
            return res
        return {"success": False, "error": str(res) if res else "empty result"}
    except Exception as exc:
        log.warning(f"Consular date fetch failed: {exc}")
        return {"success": False, "error": str(exc)}

# ─── Consular Slot Coordination & Round-Robin Radar ──────────────────────
_CONSULAR_POLLING_STATE_FILE = Path(__file__).parent / "consular_polling_state.json"
_CONSULAR_POLLING_LOCK_FILE = Path(__file__).parent / "consular_polling_state.lock"

def _get_active_consular_waiters() -> list[dict]:
    """Scan all sibling state_*.json files for accounts currently holding an OFC appointment
    and actively waiting for Consular slots within their 32-minute window."""
    waiters = []
    now = time.time()
    for p in Path(__file__).parent.glob("state_*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            if s.get("waitingForConsular") and not s.get("completed") and s.get("booking_status") != "BOOKED SUCCESS":
                ofc_ts = float(s.get("ofcBookedAt") or s.get("waitStartTime") or now)
                if (now - ofc_ts) < OFC_CONSULAR_WINDOW_SECONDS:
                    waiters.append({
                        "customer": str(s.get("customer_name") or p.stem.replace("state_", "")),
                        "username": str(s.get("username") or p.stem.replace("state_", "")),
                        "state_file": p,
                        "ofc_booked_at": ofc_ts,
                        "booked_ofc_date": str(s.get("bookedOfcDate", "N/A")),
                    })
        except Exception:
            continue
    waiters.sort(key=lambda x: x["ofc_booked_at"])
    return waiters


def _get_consular_coordination_params(num_waiters: int):
    """
    Returns (fleet_gap, account_cooldown, mode_str) tailored to number of active OFC-booked accounts:
    - 1 account:  Fleet gap 45-52s, account cooldown 45s (~40 checks in 32m). 100% immune to rate-limit.
    - 2 accounts: Fleet gap 24-28s, account cooldown 48s (~75 fleet checks in 32m).
    - 3+ accounts: Fleet gap 18-22s, account cooldown 50s (~95 fleet checks in 32m).
    """
    if num_waiters <= 1:
        fleet_gap = random.uniform(45.0, 52.0)
        account_cooldown = 45.0
        mode_str = f"Single Hold Radar [Gap: {fleet_gap:.0f}s | Safe Rate: ~1.2/min]"
    elif num_waiters == 2:
        fleet_gap = random.uniform(24.0, 28.0)
        account_cooldown = 48.0
        mode_str = f"Dual Consular Round-Robin [Fleet Gap: {fleet_gap:.0f}s | Account Gap: ~50s]"
    else:
        fleet_gap = random.uniform(18.0, 22.0)
        account_cooldown = 50.0
        mode_str = f"Fleet Consular Radar [{num_waiters} Waiters | Fleet Gap: {fleet_gap:.0f}s | Account Gap: ~{num_waiters*fleet_gap:.0f}s]"
    return fleet_gap, account_cooldown, mode_str


def _can_take_consular_poll_turn(customer: str, username: str, is_first_poll: bool):
    """
    Atomic check to see if this account is permitted to execute a Consular Slot Check right now.
    Enforces round-robin turn taking across multiple OFC-booked accounts, ensuring:
    1. First check after OFC booking is IMMEDIATE.
    2. No single account exceeds safe threshold (~1-1.2 req/min).
    3. Multiple accounts interleave smoothly to maximize fleet detection speed.
    """
    waiters = _get_active_consular_waiters()
    num_waiters = max(1, len(waiters))
    fleet_gap, account_cooldown, mode_str = _get_consular_coordination_params(num_waiters)
    now = time.time()

    with _polling_state_lock(_CONSULAR_POLLING_LOCK_FILE) as locked:
        if not locked:
            if is_first_poll:
                return True, 0.0, "Immediate First Probe", num_waiters, username
            return False, 4.0, mode_str, num_waiters, ""

        pdata = {}
        if _CONSULAR_POLLING_STATE_FILE.exists():
            try:
                with open(_CONSULAR_POLLING_STATE_FILE, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
            except Exception:
                pdata = {}

        last_global_poll = float(pdata.get("last_global_poll", 0))
        last_poller = str(pdata.get("last_poller", ""))
        current_turn = str(pdata.get("current_turn", ""))
        turn_assigned_at = float(pdata.get("turn_assigned_at", 0))
        account_last_poll = pdata.setdefault("account_last_poll", {})
        my_last_poll = float(account_last_poll.get(username, 0))
        waiter_usernames = [w["username"] for w in waiters]

        if not is_first_poll:
            # 1. Personal cooldown
            if (now - my_last_poll) < account_cooldown:
                wait_remain = max(1.0, account_cooldown - (now - my_last_poll))
                return False, wait_remain, mode_str, num_waiters, current_turn

            # 2. Fleet gap
            if (now - last_global_poll) < fleet_gap:
                wait_remain = max(1.0, fleet_gap - (now - last_global_poll))
                return False, wait_remain, mode_str, num_waiters, current_turn

            # 3. Multiple accounts turn enforcement
            if num_waiters > 1 and current_turn in waiter_usernames:
                fail_safe_limit = fleet_gap + 15.0
                if current_turn != username:
                    if (now - turn_assigned_at) < fail_safe_limit:
                        return False, 3.0, mode_str, num_waiters, current_turn
                if last_poller == username and (now - last_global_poll) < fail_safe_limit:
                    other_waiters = [u for u in waiter_usernames if u != username]
                    if other_waiters:
                        return False, 3.0, mode_str, num_waiters, current_turn

        # Claim the turn
        pdata["last_global_poll"] = now
        pdata["last_poller"] = username
        account_last_poll[username] = now

        next_turn = username
        if num_waiters > 1 and waiter_usernames:
            try:
                my_idx = waiter_usernames.index(username)
                next_turn = waiter_usernames[(my_idx + 1) % len(waiter_usernames)]
            except ValueError:
                next_turn = waiter_usernames[0]
        pdata["current_turn"] = next_turn
        pdata["turn_assigned_at"] = now
        pdata["poll_count"] = int(pdata.get("poll_count", 0)) + 1

        try:
            with open(_CONSULAR_POLLING_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(pdata, f, indent=2)
        except Exception:
            pass

        return True, 0.0, ("Immediate First Probe" if is_first_poll else mode_str), num_waiters, next_turn


def _broadcast_consular_trigger_to_fleet(finder_customer: str, finder_username: str, city_dates: dict, log: logging.Logger):
    """
    When ANY account holding an OFC appointment detects available Consular slots,
    ALL other accounts holding an OFC appointment are immediately queued in a
    staggered round-robin execution queue to book Consular slots simultaneously!
    """
    waiters = _get_active_consular_waiters()
    siblings = [w for w in waiters if w["username"] != finder_username]
    if not siblings:
        return

    log.info(
        f"📢 [CONSULAR FLEET BROADCAST] '{finder_customer}' detected consular slots! "
        f"Arming {len(siblings)} sibling OFC-booked account(s) in round-robin queue..."
    )

    now = time.time()
    stagger_step = 2.0  # 2.0s gap between sibling bookings to prevent portal collision

    for idx, sib in enumerate(siblings, start=1):
        sib_cust = sib["customer"]
        sib_user = sib["username"]
        sib_file = sib["state_file"]
        stagger_delay = idx * stagger_step

        try:
            _update_state(sib_file, {
                "pending": True,
                "action_type": "SNIPER_CONSULAR_ONLY",
                "trigger_timestamp": now + stagger_delay,
                "source": f"consular_fleet_broadcast_from_{finder_customer}",
            })
            log.info(
                f"   ↳ 🎯 Queued sibling '{sib_cust}' ({sib_user}) for SNIPER_CONSULAR_ONLY "
                f"(Round-robin offset: +{stagger_delay:.1f}s)"
            )
        except Exception as e:
            log.warning(f"Could not queue sibling '{sib_cust}': {e}")

    try:
        tg_text = (
            f"🎯 <b>CONSULAR SLOTS DETECTED FOR FLEET!</b>\n"
            f"<b>Discovered by:</b> {html.escape(str(finder_customer))}\n"
            f"<b>Total OFC-Hold Accounts Armed:</b> {len(waiters)}\n"
            f"All waiting accounts queued in round-robin execution queue!"
        )
        _notify(tg_text)
    except Exception:
        pass


async def _verify_slot_availability(
    page,
    trigger: dict,
    log: logging.Logger,
) -> tuple[bool, PreflightSlotState, dict]:
    """
    Verify slot availability using the official Dynamics 365 two-phase API
    before attempting booking.

    Cleanly distinguishes:
    - MONITOR_OBSERVED_SLOT
    - CURRENT_SLOT_CONFIRMED
    - CURRENT_SLOT_VERIFICATION_UNAVAILABLE
    - CURRENT_SLOT_GONE

    Never forces a booking through an unhealthy or blocked browser state.
    """
    ofcCities = trigger.get("ofcCities", [])
    if not ofcCities:
        return True, PreflightSlotState.CURRENT_SLOT_CONFIRMED, {"reason": "no_cities_specified"}

    priorityCity = trigger.get("ofcPriorityCity", ofcCities[0])
    ofcStartDate = trigger.get("ofcStartDate", "")
    ofcEndDate = trigger.get("ofcEndDate", "")

    if not priorityCity or not ofcStartDate or not ofcEndDate:
        log.warning("Missing slot verification parameters, skipping verification.")
        return True, PreflightSlotState.CURRENT_SLOT_CONFIRMED, {"reason": "missing_params"}

    min_slots = _min_booking_slots()
    log.info(
        f"[PREFLIGHT-SLOT] Verifying {priorityCity} between {ofcStartDate} and {ofcEndDate} "
        f"(minimum {min_slots} entries) -- initial state: {PreflightSlotState.MONITOR_OBSERVED_SLOT.value}"
    )

    from src.booking.executor import verify_browser_preflight
    ready, b_state, b_reason = await verify_browser_preflight(page, log)
    if not ready:
        log.warning(f"[PREFLIGHT-SLOT] Browser not ready for verification ({b_state.value}: {b_reason})")
        return False, PreflightSlotState.CURRENT_SLOT_VERIFICATION_UNAVAILABLE, {"reason": f"browser_{b_reason}"}

    try:
        verification_config = {
            "ofcCities": ofcCities,
            "ofcStartDate": ofcStartDate,
            "ofcEndDate": ofcEndDate,
            "prevent_immediate": trigger.get("prevent_immediate", False),
        }

        await portal_pacer.pace(log, subsystem=Subsystem.BOOKING)
        result = await fetch_dates_via_browser(page, verification_config)

        if not result or not result.get("success"):
            log.warning("Slot verification initial fetch failed -- re-checking browser health...")
            ready2, _, _ = await verify_browser_preflight(page, log)
            if not ready2:
                return False, PreflightSlotState.CURRENT_SLOT_VERIFICATION_UNAVAILABLE, {"reason": "browser_unhealthy_on_fetch"}
            # One bounded retry if browser is healthy
            await asyncio.sleep(1.0)
            await portal_pacer.pace(log, subsystem=Subsystem.BOOKING)
            result = await fetch_dates_via_browser(page, verification_config)
            if not result or not result.get("success"):
                return False, PreflightSlotState.CURRENT_SLOT_VERIFICATION_UNAVAILABLE, {"reason": "transport_fetch_failed"}

        results = result.get("results", {})
        tokens = result.get("tokens") or {}

        def _extract_city(city_name: str):
            key = str(city_name).upper()
            return results.get(key, []), tokens.get(key)

        def _any_city_has_dates() -> bool:
            return any(
                isinstance(_extract_city(c)[0], list) and _extract_city(c)[0]
                for c in ofcCities
            )

        if not _any_city_has_dates():
            # Potential in-flight race: probe once more with pacing
            log.info("No slots returned on first verification fetch -- checking once more to confirm...")
            await asyncio.sleep(1.5)
            await portal_pacer.pace(log, subsystem=Subsystem.BOOKING)
            result = await fetch_dates_via_browser(page, verification_config)
            results = (result or {}).get("results", {})
            tokens = (result or {}).get("tokens") or {}

            if not _any_city_has_dates():
                log.info("[INFO] Slot verification: 0 dates found on re-verification -- confirmed CURRENT_SLOT_GONE.")
                return False, PreflightSlotState.CURRENT_SLOT_GONE, {"reason": "no_dates_found"}

        # Collect in-range candidates
        city_candidates = []
        for city_name in ofcCities:
            city_data, _tok = _extract_city(city_name)
            if not isinstance(city_data, list) or not city_data:
                continue
            cands = sorted({
                str(s.get("Date", ""))[:10]
                for s in city_data
                if ofcStartDate <= str(s.get("Date", ""))[:10] <= ofcEndDate
            })
            if cands:
                city_candidates.append((cands[0], city_name, cands))
        city_candidates.sort()

        if not city_candidates:
            log.info("[INFO] Slot verification: dates exist but none in user's configured range -- confirmed CURRENT_SLOT_GONE.")
            return False, PreflightSlotState.CURRENT_SLOT_GONE, {"reason": "no_dates_in_range"}

        for best_date, city_name, cands in city_candidates:
            city_data, city_token = _extract_city(city_name)
            if isinstance(city_data, dict) and "error" in city_data:
                continue

            if len(cands) >= min_slots:
                log.info(
                    f"[OK] Slot verification CONFIRMED: {city_name} has {len(cands)} in-range dates "
                    f"-- confirmed slot on {best_date}"
                )
                return True, PreflightSlotState.CURRENT_SLOT_CONFIRMED, {
                    "city": city_name,
                    "date": best_date,
                    "token": city_token,
                    "candidates": cands,
                }

            # Count check on candidate date
            available = await _count_available_slots(page, city_name, best_date, city_token)
            log.info(f"[SLOT-COUNT] {city_name} {best_date}: {available} entries available")
            if available >= min_slots:
                log.info(f"Slot verification passed: {city_name} {best_date} with {available} entries (>= {min_slots})")
                _notify(
                    f"<b>SLOT MATCHED</b>\n\n"
                    f"<b>City:</b> {html.escape(str(city_name))}\n"
                    f"<b>Date:</b> {html.escape(str(best_date))}\n"
                    f"<b>Entries available:</b> {available}\n\n"
                    f"Proceeding to book now!"
                )
                return True, PreflightSlotState.CURRENT_SLOT_CONFIRMED, {
                    "city": city_name,
                    "date": best_date,
                    "token": city_token,
                    "available": available,
                }
            log.warning(
                f"{city_name} {best_date} reports {available} entries (< {min_slots}) -- checking next city/date..."
            )

        log.warning(
            "All candidate dates reported insufficient slot counts (< min_slots) -- slot considered gone."
        )
        return False, PreflightSlotState.CURRENT_SLOT_GONE, {"reason": "insufficient_entries"}

    except Exception as e:
        log.warning(f"Slot verification error: {e}. Marking as verification unavailable.")
        return False, PreflightSlotState.CURRENT_SLOT_VERIFICATION_UNAVAILABLE, {"error": str(e)}

@contextlib.contextmanager
def _polling_state_lock(lock_path: Path, timeout: float = 5.0):
    """Context manager for acquiring an exclusive file lock on polling_state."""
    start = time.time()
    acquired = False
    while time.time() - start < timeout:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if lock_path.exists() and (time.time() - os.path.getmtime(lock_path)) > 10:
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
            except OSError:
                pass
            time.sleep(0.05)
        except OSError:
            time.sleep(0.05)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if lock_path.exists():
                    os.remove(lock_path)
            except OSError:
                pass


def _set_account_quota_cooldown(username: str, cooldown_sec: float):
    """Set an account-level quota cooldown timestamp in polling_state.json under lock."""
    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_lock_file = Path(__file__).parent / "polling_state.lock"
    with _polling_state_lock(polling_lock_file) as locked:
        if locked and polling_state_file.exists():
            try:
                with open(polling_state_file, "r", encoding="utf-8") as f:
                    pstate = json.load(f)
                cooldowns = pstate.setdefault("account_quota_cooldowns", {})
                cooldowns[username] = time.time() + cooldown_sec
                if pstate.get("active_poller") == username:
                    pstate["active_poller"] = None
                with open(polling_state_file, "w", encoding="utf-8") as f:
                    json.dump(pstate, f, indent=2)
            except Exception as e:
                log.warning(f"Error setting account quota cooldown: {e}")


def _swap_poller_with_standby(locked_username: str, reason: str = "Persistent 429 quota limit") -> tuple[bool, str, str]:
    """
    Auto Hot-Swap Engine:
    When an active poller hits origin portal view quota (HTTP 429), dynamically:
    1. Demotes locked poller to RESERVED_BOOKING (VIP Sniper Standby).
    2. Promotes an eligible standby account to POLLING_ONLY (Fleet Radar Poller).
    3. Persists role updates to accounts.json and server/data/accounts.json.
    4. Updates polling_state.json with a 60m rest for locked_username and transfers turn to promoted candidate.
    Zero browser restarts or process restarts required.
    """
    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_lock_file = Path(__file__).parent / "polling_state.lock"
    server_accounts_file = Path(__file__).resolve().parent.parent / "server" / "data" / "accounts.json"

    with _polling_state_lock(polling_lock_file) as locked:
        if not locked:
            log.warning("[ROLE-SWAP] Failed to acquire lock for auto hot-swap!")
            return False, "", ""

        if not ACCOUNTS_FILE.exists():
            log.warning("[ROLE-SWAP] accounts.json does not exist!")
            return False, "", ""

        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
        except Exception as e:
            log.error(f"[ROLE-SWAP] Error reading accounts.json: {e}")
            return False, "", ""

        locked_account = None
        for acc in accounts:
            if acc.get("username") == locked_username:
                locked_account = acc
                break

        if not locked_account:
            log.warning(f"[ROLE-SWAP] Account '{locked_username}' not found in accounts.json")
            return False, "", ""

        locked_customer = locked_account.get("customer_name") or locked_username

        pstate = {}
        if polling_state_file.exists():
            try:
                with open(polling_state_file, "r", encoding="utf-8") as f:
                    pstate = json.load(f)
            except Exception:
                pass

        quota_cds = pstate.setdefault("account_quota_cooldowns", {})
        now = time.time()

        standby_candidates = []
        for acc in accounts:
            uname = acc.get("username")
            if not uname or uname == locked_username:
                continue
            if acc.get("enabled", True) in (False, 0, "0", "false", "no", "off"):
                continue
            if acc.get("role") != "RESERVED_BOOKING":
                continue

            c_uid = safe_id(uname)
            c_state_file = Path(__file__).parent / f"state_{c_uid}.json"
            is_alive = False
            if c_state_file.exists():
                try:
                    c_state = json.loads(c_state_file.read_text(encoding="utf-8"))
                    hb = float(c_state.get("booking_runner_heartbeat") or 0)
                    if (now - hb) < 120.0:
                        is_alive = True
                except Exception:
                    pass

            quota_expiry = float(quota_cds.get(uname, 0))
            is_quota_free = (now >= quota_expiry)
            rem_cooldown = max(0.0, quota_expiry - now)
            standby_candidates.append({
                "account": acc,
                "is_alive": is_alive,
                "is_quota_free": is_quota_free,
                "quota_expiry": quota_expiry,
                "rem_cooldown": rem_cooldown
            })

        # Ranking Strategy:
        # 1. Accounts whose cooldown is expired or never had cooldown come first (1 > 0)
        # 2. Alive accounts come next (1 > 0)
        # 3. For expired: account with oldest cooldown / rested longest first (-quota_expiry)
        #    For cooling: account closest to finishing rest first (-rem_cooldown)
        def _sort_candidate(item):
            q_free = 1 if item["is_quota_free"] else 0
            alive = 1 if item["is_alive"] else 0
            metric = -item["quota_expiry"] if item["is_quota_free"] else -item["rem_cooldown"]
            return (q_free, alive, metric)

        standby_candidates.sort(key=_sort_candidate, reverse=True)

        if not standby_candidates:
            cooldown_sec = 3600.0
            quota_cds[locked_username] = now + cooldown_sec
            if pstate.get("active_poller") == locked_username:
                pstate["active_poller"] = None
            try:
                with open(polling_state_file, "w", encoding="utf-8") as f:
                    json.dump(pstate, f, indent=2)
            except Exception:
                pass
            log.warning(f"[ROLE-SWAP] No eligible standby sniper accounts configured to hot-swap for '{locked_customer}'. Put '{locked_customer}' on 60m rest.")
            return False, locked_customer, ""

        chosen_candidate = standby_candidates[0]["account"]
        cand_username = chosen_candidate.get("username")
        cand_customer = chosen_candidate.get("customer_name") or cand_username

        # 1. Update roles in accounts.json
        locked_account["role"] = "RESERVED_BOOKING"
        chosen_candidate["role"] = "POLLING_ONLY"

        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2)
        except Exception as e:
            log.error(f"[ROLE-SWAP] Failed to write updated accounts.json: {e}")
            return False, "", ""

        if server_accounts_file.exists():
            try:
                with open(server_accounts_file, "w", encoding="utf-8") as f:
                    json.dump(accounts, f, indent=2)
            except Exception:
                pass

        # Sync updated roles directly to SQLite database for Web UI
        try:
            db_file = Path(__file__).resolve().parent.parent / "server" / "data" / "visabot.db"
            if db_file.exists():
                import sqlite3
                with sqlite3.connect(db_file) as s_conn:
                    s_cur = s_conn.cursor()
                    s_cur.execute("UPDATE accounts SET role = ? WHERE username = ?", ("RESERVED_BOOKING", locked_username))
                    s_cur.execute("UPDATE accounts SET role = ? WHERE username = ?", ("POLLING_ONLY", cand_username))
                    s_conn.commit()
        except Exception as e_db:
            log.warning(f"[ROLE-SWAP] Could not update visabot.db: {e_db}")

        # Update in-memory runner state files for live dynamic reflection
        try:
            l_uid = safe_id(locked_username)
            l_state_file = Path(__file__).parent / f"state_{l_uid}.json"
            if l_state_file.exists():
                _update_state(l_state_file, {"role": "RESERVED_BOOKING"})

            c_uid = safe_id(cand_username)
            c_state_file = Path(__file__).parent / f"state_{c_uid}.json"
            if c_state_file.exists():
                _update_state(c_state_file, {"role": "POLLING_ONLY"})
        except Exception as e_state:
            log.warning(f"[ROLE-SWAP] Could not update runner state files: {e_state}")

        # 2. Update polling_state.json
        cooldown_sec = 3600.0
        quota_cds[locked_username] = now + cooldown_sec
        # Clear cooldown for promoted candidate so it immediately begins polling
        quota_cds.pop(cand_username, None)

        reg = pstate.setdefault("registered_monitors", {})
        reg.pop(locked_username, None)
        reg[cand_username] = now

        if pstate.get("active_poller") == locked_username:
            pstate["active_poller"] = None

        pstate["current_turn"] = cand_username
        pstate["turn_assigned_at"] = now
        pstate["poll_finished_at"] = 0
        pstate["last_swap"] = {
            "time": now,
            "demoted": {"username": locked_username, "customer": locked_customer},
            "promoted": {"username": cand_username, "customer": cand_customer},
            "reason": reason
        }

        try:
            with open(polling_state_file, "w", encoding="utf-8") as f:
                json.dump(pstate, f, indent=2)
        except Exception as e:
            log.error(f"[ROLE-SWAP] Failed to write polling_state.json: {e}")

        banner = (
            f"\n"
            f"================================================================================\n"
            f"🔄 [AUTO HOT-SWAP ENGINE] ORIGIN VIEW LIMIT (HTTP 429) DETECTED!\n"
            f"--------------------------------------------------------------------------------\n"
            f"Reason:                  {reason}\n"
            f"🛡️ Demoted to VIP Sniper:  '{locked_customer}' ({locked_username}) -> RESERVED_BOOKING\n"
            f"🚀 Promoted to Fleet Poller: '{cand_customer}' ({cand_username}) -> POLLING_ONLY\n"
            f"⏱️ Quota Cooldown:         60 minutes resting parked on homepage (sniper armed)\n"
            f"🎯 ZERO-RESTART HOT-SWAP: Live dynamic swap executed! Polling continuous.\n"
            f"================================================================================\n"
        )
        print(banner, flush=True)
        log.warning(f"[AUTO-HOT-SWAP] Demoted '{locked_customer}' to VIP Sniper. Promoted '{cand_customer}' to Fleet Poller! Continuous 24/7 monitoring maintained.")

        return True, locked_customer, cand_customer


def _get_active_monitors(pstate: dict, current_username: str) -> list[str]:
    """Return sorted list of usernames currently running in MONITOR/polling mode."""
    reg = pstate.get("registered_monitors", {})
    if not isinstance(reg, dict):
        reg = {}
    quota_cds = pstate.get("account_quota_cooldowns", {})
    if not isinstance(quota_cds, dict):
        quota_cds = {}
    now = time.time()
    active = [
        u for u, ts in reg.items()
        if isinstance(ts, (int, float))
        and (now - ts) < 90
        and now >= float(quota_cds.get(u, 0))
    ]
    if current_username and current_username not in active and now >= float(quota_cds.get(current_username, 0)):
        cfg = _load_account_config(current_username)
        if cfg.get("role") == "POLLING_ONLY":
            active.append(current_username)
    return sorted(list(set(active)))


async def _try_background_poll(page, customer: str, username: str, last_background_poll: float, last_poll_debug: float) -> tuple[float, float, bool]:
    """Execute background API polling if permitted by global limits and personal cooldowns.

    Returns (last_background_poll, last_poll_debug). Raises SessionExpiredError
    when the OFC date fetch returns login HTML on every city, so the main loop
    can run session recovery immediately instead of silently polling a dead
    session.
    """
    my_config = _load_account_config(username)
    my_role = str(my_config.get("role", "")).strip().upper()
    if my_role == "RESERVED_BOOKING":
        # Dedicated sniper mode: do NOT poll slots to preserve Cloudflare rate limits.
        # Account stays parked on homepage in standby, listening for triggers from MONITOR accounts.
        if not getattr(_try_background_poll, f"_logged_standby_{username}", False):
            setattr(_try_background_poll, f"_logged_standby_{username}", True)
            log.info(f"[TARGET] [{customer}] Role is RESERVED_BOOKING: Background slot polling disabled. Standby sniper active.")
        return time.time(), last_poll_debug, False
    else:
        setattr(_try_background_poll, f"_logged_standby_{username}", False)

    # Startup stabilization: wait at least 30s after browser readiness before any background poll
    runner_start = getattr(run, f"_start_time_{username}", 0)
    if runner_start and (time.time() - runner_start) < 30:
        return last_background_poll, last_poll_debug, False

    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_lock_file = Path(__file__).parent / "polling_state.lock"
    polling_active = True
    cooldown_seconds = 90
    gap_seconds = 45
    poll_finished_at = 0
    active_poller = None
    active_since = 0
    last_poller = None
    current_turn = None
    turn_assigned_at = 0

    # Register this monitor's presence in registered_monitors under file lock
    with _polling_state_lock(polling_lock_file) as locked:
        if locked and polling_state_file.exists():
            try:
                with open(polling_state_file, "r") as f:
                    pstate = json.load(f)
                reg = pstate.setdefault("registered_monitors", {})
                reg[username] = time.time()
                with open(polling_state_file, "w") as f:
                    json.dump(pstate, f, indent=2)
            except Exception:
                pass

    pstate = {}
    if polling_state_file.exists():
        try:
            with open(polling_state_file, "r") as f:
                pstate = json.load(f)
            cooldown_seconds = int(pstate.get("cooldown", 90))
            gap_seconds = int(pstate.get("gap", 45))
            poll_finished_at = float(pstate.get("poll_finished_at", pstate.get("global_last_poll", 0)))
            active_poller = pstate.get("active_poller")
            active_since = float(pstate.get("active_poller_since", 0))
            last_poller = pstate.get("last_poller")
            current_turn = pstate.get("current_turn")
            turn_assigned_at = float(pstate.get("turn_assigned_at", 0))
        except Exception:
            pass

    active_monitors = _get_active_monitors(pstate, username)
    # Cap active monitors at max 3
    num_pollers = min(max(len(active_monitors), 1), 3)

    # ── Dynamic Runtime Cadence based on active polling fleet size ──
    # User requirement:
    #   1 polling account -> Interval 45-75s, Fleet gap 45s
    #   2 polling accounts -> Interval 80-110s, Fleet gap 45s (fleet check every ~45-55s)
    #   3 polling accounts -> Interval 120-160s, Fleet gap 40s (fleet check every ~40-50s)
    # Max 3 pollers allowed to guarantee ZERO rate limits / bans.
    # Microsoft Dynamics 365 enforces a per-account session rate limit window of ~180s-210s.
    # To guarantee ZERO 429s 24/7:
    from src.common.proxy import get_proxy_mode
    is_webshare = (get_proxy_mode() == "WEBSHARE")

    if is_webshare:
        # Dedicated sticky Webshare IPs per account -- fast, safe, continuous monitoring
        # 1 poller: 60-80s, 2 pollers: 75-95s (fleet gap 40s), 3+ pollers: 85-110s (fleet gap 30s)
        if num_pollers == 1:
            dyn_interval_min = float(os.getenv("MONITOR_INTERVAL_MIN_1P", "60"))
            dyn_interval_max = float(os.getenv("MONITOR_INTERVAL_MAX_1P", "80"))
            dyn_fleet_gap    = float(os.getenv("FLEET_MONITOR_GAP_1P", "60"))
        elif num_pollers == 2:
            dyn_interval_min = float(os.getenv("MONITOR_INTERVAL_MIN_2P", "75"))
            dyn_interval_max = float(os.getenv("MONITOR_INTERVAL_MAX_2P", "95"))
            dyn_fleet_gap    = float(os.getenv("FLEET_MONITOR_GAP_2P", "40"))
        else:  # 3+ pollers
            dyn_interval_min = float(os.getenv("MONITOR_INTERVAL_MIN_3P", "85"))
            dyn_interval_max = float(os.getenv("MONITOR_INTERVAL_MAX_3P", "110"))
            dyn_fleet_gap    = float(os.getenv("FLEET_MONITOR_GAP_3P", "30"))
    else:
        # DIRECT_ONLY fallback (shared home IP)
        if num_pollers == 1:
            dyn_interval_min = float(os.getenv("MONITOR_INTERVAL_MIN_1P", "180"))
            dyn_interval_max = float(os.getenv("MONITOR_INTERVAL_MAX_1P", "210"))
            dyn_fleet_gap    = float(os.getenv("FLEET_MONITOR_GAP_1P", "180"))
        elif num_pollers == 2:
            dyn_interval_min = float(os.getenv("MONITOR_INTERVAL_MIN_2P", "180"))
            dyn_interval_max = float(os.getenv("MONITOR_INTERVAL_MAX_2P", "210"))
            dyn_fleet_gap    = float(os.getenv("FLEET_MONITOR_GAP_2P", "90"))
        else:  # 3 pollers
            dyn_interval_min = float(os.getenv("MONITOR_INTERVAL_MIN_3P", "190"))
            dyn_interval_max = float(os.getenv("MONITOR_INTERVAL_MAX_3P", "225"))
            dyn_fleet_gap    = float(os.getenv("FLEET_MONITOR_GAP_3P", "65"))

    # Sync user-configured Cadence from GUI (monitor_config.json) if present
    gui_cfg_file = Path(__file__).resolve().parent.parent / "monitor_config.json"
    if gui_cfg_file.exists():
        try:
            with open(gui_cfg_file, "r", encoding="utf-8") as f:
                gdata = json.load(f)
                g_gap = float(gdata.get("fleet_gap") or 0)
                if g_gap >= 25.0:
                    dyn_fleet_gap = g_gap
                g_min = float(gdata.get("poll_interval_min") or 0)
                g_max = float(gdata.get("poll_interval_max") or 0)
                if g_min >= 45.0:
                    dyn_interval_min = g_min
                    dyn_interval_max = max(g_max if g_max >= g_min else g_min + 20.0, g_min + 10.0)
        except Exception:
            pass

    interval_min = dyn_interval_min
    interval_max = dyn_interval_max
    if interval_max < interval_min:
        interval_min, interval_max = interval_max, interval_min

    clamped_cadence = getattr(_try_background_poll, "_next_cadence", None)
    if clamped_cadence is None:
        clamped_cadence = random.uniform(interval_min, interval_max)
        _try_background_poll._next_cadence = clamped_cadence
    cooldown_seconds = clamped_cadence

    # Global fleet gap: minimum gap between ANY poll across all accounts in the fleet
    fleet_gap = dyn_fleet_gap

    now = time.time()

    # Gate -1: Pause all slot polling immediately if any account is actively executing booking
    booking_holder = fleet_lock_holder()
    if booking_holder:
        if (now - last_poll_debug) > 10:
            last_poll_debug = now
            log.info(f"[POLLING] Paused: Account '{booking_holder}' is actively executing booking. Focusing all fleet network/resources on booking...")
        return last_background_poll, last_poll_debug, False

    # Gate 0: Fleet-wide rate-limit / 429 pause (Only in DIRECT_ONLY mode where all accounts share 1 IP)
    from src.common.proxy import get_proxy_mode
    if get_proxy_mode() == "DIRECT_ONLY":
        rate_limit_until = float(pstate.get("rate_limit_until", 0))
        if now < rate_limit_until:
            remaining = int(rate_limit_until - now)
            if (now - last_poll_debug) > 30:
                last_poll_debug = now
                log.info(f"[RATE-LIMIT] Fleet cooling off for another {remaining}s due to server rate limit on local IP. "
                         f"(Tip: Toggle mobile hotspot Airplane mode for 3s to clear instantly for $0).")
            return last_background_poll, last_poll_debug, False

    # Gate 0b: Individual Account Quota Cooldown (when this specific account exceeded portal daily limit)
    account_quota_until = float(pstate.get("account_quota_cooldowns", {}).get(username, 0))
    if now < account_quota_until:
        rem_min = max(1, int((account_quota_until - now) / 60))
        # Active Poller resting check: If an account currently holding the POLLING_ONLY role
        # is resting due to viewing quota, immediately hot-swap it with an eligible VIP Sniper!
        swapped, locked_c, prom_c = _swap_poller_with_standby(
            username,
            reason=f"Poller account is resting ({rem_min}m remaining) due to portal 24h viewing limit (HTTP 429)"
        )
        if swapped:
            log.info(f"[ROLE-SWAP] Resting poller '{customer}' demoted to VIP Sniper. Standby '{prom_c}' promoted to Fleet Poller!")
            return last_background_poll, last_poll_debug, False

        if (now - last_poll_debug) > 60:
            last_poll_debug = now
            log.info(f"[ACCOUNT-REST] Account '{customer}' ({username}) is resting ({rem_min}m remaining) due to portal 24h viewing limit (HTTP 429).")
        return last_background_poll, last_poll_debug, False

    # Gate 1: Check active poller lease (strict mutual exclusion)
    if active_poller and active_poller != username:
        if (now - active_since) < 90:
            if (now - last_poll_debug) > 30 and os.getenv("DEBUG_POLLING") == "1":
                last_poll_debug = now
                print(f"[POLLING-RESULT] DEBUG: '{active_poller}' is actively polling. Waiting...", flush=True)
            return last_background_poll, last_poll_debug, False

    # Gate 2: Check fleet quiet gap (measured from when previous poll FINISHED)
    if (now - poll_finished_at) < fleet_gap:
        return last_background_poll, last_poll_debug, False

    # Gate 3: Check personal cooldown
    my_cooldown_passed = (now - last_background_poll) > cooldown_seconds
    if not my_cooldown_passed:
        return last_background_poll, last_poll_debug, False

    # Gate 4: Strict Round-Robin Turn & Alternation
    active_monitors = _get_active_monitors(pstate, username)
    if len(active_monitors) > 1:
        # If current_turn is set to an account that is not currently in active_monitors, reset it immediately
        if current_turn and current_turn not in active_monitors:
            current_turn = None
        # If another account is assigned this turn:
        fail_safe_limit = max(fleet_gap + 15, 55)
        if current_turn and current_turn != username:
            if (now - turn_assigned_at) < fail_safe_limit:
                return last_background_poll, last_poll_debug, False
            else:
                log.warning(f"[WARN] [FAIL-SAFE] Monitor '{current_turn}' did not poll within {fail_safe_limit:.0f}s. '{username}' taking over turn to prevent deadlock!")
        # If I was the last account to poll, don't poll consecutively if another monitor is active and turn hasn't timed out
        if last_poller == username and (now - poll_finished_at) < fail_safe_limit:
            other_monitors = [m for m in active_monitors if m != username]
            if other_monitors:
                return last_background_poll, last_poll_debug, False

    # Gate 5: Atomically acquire the active polling lease under lock
    got_slot = False
    with _polling_state_lock(polling_lock_file) as locked:
        if locked:
            try:
                with open(polling_state_file, "r") as f:
                    pstate = json.load(f)
                cur_active = pstate.get("active_poller")
                cur_since = float(pstate.get("active_poller_since", 0))
                cur_finished = float(pstate.get("poll_finished_at", pstate.get("global_last_poll", 0)))
                now = time.time()
                if (not cur_active or cur_active == username or (now - cur_since) >= 90) and (now - cur_finished) >= fleet_gap:
                    pstate["active_poller"] = username
                    pstate["active_poller_since"] = now
                    with open(polling_state_file, "w") as f:
                        json.dump(pstate, f, indent=2)
                    got_slot = True
            except Exception as lock_err:
                log.warning(f"Error acquiring polling lease under lock: {lock_err}")

    if got_slot:
        last_background_poll = time.time()
        _try_background_poll._next_cadence = random.uniform(interval_min, interval_max)
        poll_no = getattr(_try_background_poll, "poll_count", 0) + 1
        _try_background_poll.poll_count = poll_no
        next_cd_sec = int(_try_background_poll._next_cadence)
        ts = time.strftime("%I:%M:%S %p")
        if poll_no == 1:
            log.info(f"🚀 [IMMEDIATE ACTIVATION] Polling monitor '{customer}' ({username}) started slot monitoring! ({num_pollers} active monitor(s) in fleet)")

        try:
                my_config = _load_account_config(username)
                my_config["pollAllCities"] = True  # Fleet Radar Mode: Poller accounts scan all 5 cities
                # Attach static identifiers
                from src.common.static_identifiers import get_account_identifiers, save_account_identifiers
                p_id, a_id = get_account_identifiers(username, my_config)
                if not p_id or not a_id:
                    try:
                        from src.api.extractor import extract_ids_from_page
                        dp, da = await extract_ids_from_page(page)
                        if dp and da:
                            save_account_identifiers(username, dp, da)
                            p_id, a_id = dp, da
                    except Exception as ext_err:
                        log.warning(f"Dynamic ID extraction fallback in poll: {ext_err}")

                if p_id and not my_config.get("primaryId"):
                    my_config["primaryId"] = p_id
                if a_id and not my_config.get("appd"):
                    my_config["appd"] = a_id

                try:
                    # Liveness / waiting room pre-check before making any fetch request
                    try:
                        cur_tab_url = (page.url or "").lower()
                    except Exception:
                        cur_tab_url = ""

                    from src.auth.challenge import is_cloudflare_hard_block
                    if await is_cloudflare_hard_block(page):
                        log.error(f"[HARD-BLOCK] Pre-poll check: '{customer}' tab is on Cloudflare 403 block page ('Sorry, you have been blocked')! Rotating proxy IP immediately...")
                        await _rotate_ip_and_refresh(page, customer, "Cloudflare 403 Hard Block in pre-poll", username=username)
                        return last_background_poll, last_poll_debug, False

                    if any(marker in cur_tab_url for marker in ("waitingroom", "challenge", "chrome-error", "chromewebdata")):
                        log.info(f"[WAIT] Tab is in CF waiting room / transit ({cur_tab_url[:75]}) - deferring background poll.")
                        return last_background_poll, last_poll_debug, False

                    # Stable session preservation: Do NOT reload portal homepage during monitoring.
                    # In-page fetch() executes with full cookies & TLS session intact without DOM reloads,
                    # avoiding unprovoked Cloudflare WAF Turnstile challenges.

                    # Pace the monitoring API fetch too (fleet-wide spacing on one IP).
                    await portal_pacer.pace(log)
                    # Track the monitoring request
                    from src.common.request_tracker import record_request
                    acct_uid = safe_id(username)
                    rate_info = record_request(uid=acct_uid, stage="background_poll")
                    log.info(f"MONITOR-REQ rate={rate_info['requests_per_minute']}/min total={rate_info['total']}")
                    res = await fetch_dates_via_browser(page, my_config)
                except SessionExpiredError:
                    raise
                except Exception as fetch_err:
                    print(f"\n[MONITOR] ERROR Poll #{poll_no} for '{customer}': {fetch_err}\n", flush=True)
                    res = None

                # Keep browser parked on the homepage for slot monitoring
                if isinstance(res, dict) and not res.get("success"):
                    is_cf_issue = res.get("in_challenge") or res.get("in_cf_block") or any(k in str(res.get("error", "")).lower() for k in ("challenge", "turnstile", "blocked", "cloudflare"))
                    if is_cf_issue:
                        from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
                        log.info(f"[CHALLENGE/BLOCK] Cloudflare verification / reload signal detected for '{customer}'. Executing in-place F5 reload on sticky IP...")
                        try:
                            await portal_pacer.pace(log)
                            await page.reload(wait_until="domcontentloaded", timeout=35000)
                            await asyncio.sleep(2)
                            if await is_cloudflare_challenge(page):
                                log.info(f"🤖 Solving Cloudflare Turnstile challenge on page for '{customer}'...")
                                solved_poll = await solve_cloudflare_challenge(page, log)
                                if not solved_poll:
                                    log.info(f"[CHALLENGE] Retrying second F5 reload to refresh Turnstile widget...")
                                    await page.reload(wait_until="domcontentloaded", timeout=35000)
                                    await asyncio.sleep(2)
                                    if await is_cloudflare_challenge(page):
                                        await solve_cloudflare_challenge(page, log)
                            await asyncio.sleep(2)
                            log.info(f"[OK] Cloudflare clearance renewed for '{customer}' on sticky IP via F5 reload. Resuming monitoring.")
                            _reset_rate_limit_failures(username=username)
                            # Immediate in-turn retry: poll slots immediately now that page is reloaded & clearance renewed!
                            res = await fetch_dates_via_browser(page, my_config)
                            if isinstance(res, dict) and (res.get("in_cf_block") or res.get("needs_rotate")):
                                log.error(f"[HARD-BLOCK] Slot fetch reported Cloudflare 403 hard block on '{customer}'! Rotating proxy IP immediately...")
                                await _rotate_ip_and_refresh(page, customer, "Cloudflare 403 Hard Block in fetch", username=username)
                                return last_background_poll, last_poll_debug, False
                            if isinstance(res, dict) and res.get("in_challenge"):
                                return last_background_poll, last_poll_debug, False
                        except Exception as chal_err:
                            log.warning(f"[CHALLENGE] In-place F5 reload error for '{customer}': {chal_err}")
                            return last_background_poll, last_poll_debug, False

                    if res.get("in_waiting_room"):
                        log.info("[WAIT] Browser in Cloudflare waiting room - waiting passively.")
                        return last_background_poll, last_poll_debug, False

                    if _looks_like_cf_api_block(res):
                        actual_err = _extract_actual_error(res)
                        err_low = actual_err.lower()
                        is_portal_429 = "429" in err_low or "too many requests" in err_low or "rate limit" in err_low
                        is_hard_cf_block = ("1015" in err_low or "1020" in err_low or "sorry, you have been blocked" in err_low or "unable to access" in err_low or "cf-error-details" in err_low) and not is_portal_429

                        if is_portal_429:
                            c_429 = getattr(run, f"_consecutive_429_{username}", 0) + 1
                            setattr(run, f"_consecutive_429_{username}", c_429)
                            is_direct = (get_proxy_mode() == "DIRECT_ONLY")
                            rot_429 = getattr(run, f"_rot_429_{username}", 0)

                            is_dynamics_quota = ("customer self-service" in err_low or "visa application home" in err_low)
                            should_hot_swap = (
                                (not is_direct and rot_429 >= 1 and c_429 >= 2)
                                or (is_dynamics_quota and c_429 >= 2)
                                or (is_direct and c_429 >= 2)
                            )
                            if should_hot_swap:
                                # Account-Level Portal Viewing Quota Limit reached!
                                # Trigger Auto Hot-Swap Engine to promote standby sniper and demote locked poller
                                setattr(run, f"_consecutive_429_{username}", 0)
                                setattr(run, f"_rot_429_{username}", 0)
                                swapped, locked_c, prom_c = _swap_poller_with_standby(
                                    username,
                                    reason=f"Portal viewing quota (HTTP 429 Dynamics 365 limit): {actual_err}"
                                )
                                if swapped:
                                    return time.time() + 3600.0, last_poll_debug, False
                                else:
                                    cooldown_sec = 3600.0  # 1 hour rest
                                    _set_account_quota_cooldown(username, cooldown_sec)
                                    print(f"\n[MONITOR] [ACCOUNT QUOTA LIMIT] '{customer}' ({username}) portal viewing quota reached (HTTP 429 persisted across proxy IP rotation).", flush=True)
                                    print(f"[MONITOR] Putting '{customer}' to rest for 60m so other accounts poll smoothly without delays or errors.\n", flush=True)
                                    log.warning(f"[ACCOUNT-REST] '{customer}' placed in account quota rest for 60m.")
                                    return time.time() + cooldown_sec, last_poll_debug, False
                            elif get_proxy_mode() == "MOBILE":
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': 429 on Mobile IP. Triggering fast ADB Airplane mode cycle...\n", flush=True)
                                from src.common.mobile_proxy import rotate_mobile_ip
                                rot_res = rotate_mobile_ip(reason=f"Portal 429 ({actual_err})")
                                setattr(run, f"_consecutive_429_{username}", 0)
                                return time.time() + 6.0, last_poll_debug, False
                            elif not is_direct and c_429 >= 3:
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Repeated 429 on proxy IP (strike #{c_429}). Auto-rotating to fresh Webshare proxy slot...\n", flush=True)
                                log.warning(f"[PROXY-ROTATE] '{customer}' hit consecutive 429s ({c_429}) on proxy IP -> rotating to fresh slot.")
                                setattr(run, f"_consecutive_429_{username}", 0)
                                setattr(run, f"_rot_429_{username}", rot_429 + 1)
                                await _rotate_ip_and_refresh(page, customer, actual_err, username=username)
                                return time.time() + 60.0, last_poll_debug, False
                            else:
                                backoff_sec = 60.0 if not is_direct else min(max(180.0, 60.0 * c_429), 240.0)
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Portal API rate limit 429 ({actual_err}) [strike #{c_429}/3]. Soft cooldown {backoff_sec:.0f}s on sticky IP...\n", flush=True)
                                log.info(f"[COOLDOWN] '{customer}' cooling down {backoff_sec:.0f}s on sticky IP to allow rate limit window to expire.")
                                return time.time() + backoff_sec, last_poll_debug, False
                        elif is_hard_cf_block:
                            if get_proxy_mode() == "MOBILE":
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Cloudflare Block on Mobile. Triggering ADB Airplane mode cycle...\n", flush=True)
                                from src.common.mobile_proxy import rotate_mobile_ip
                                rotate_mobile_ip(reason=f"Cloudflare 403 ({actual_err})")
                                setattr(run, f"_consecutive_block_{username}", 0)
                                return time.time() + 6.0, last_poll_debug, False
                            print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Cloudflare 403 Hard Block detected ({actual_err}). Auto-rotating proxy IP immediately...\n", flush=True)
                            log.error(f"[PROXY-ROTATE] '{customer}' hit Cloudflare 403 Hard Block -> rotating to fresh Webshare proxy slot.")
                            setattr(run, f"_consecutive_block_{username}", 0)
                            await _rotate_ip_and_refresh(page, customer, actual_err, username=username)
                            return time.time() + 60.0, last_poll_debug, False
                        else:
                            # Transient block or challenge signal: execute in-place F5 reload, do NOT rotate IP prematurely
                            c_block = getattr(run, f"_consecutive_block_{username}", 0) + 1
                            setattr(run, f"_consecutive_block_{username}", c_block)
                            if c_block >= 3:
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Persistent server block ({actual_err}) after 3 attempts. Auto-rotating to fresh Webshare proxy slot...\n", flush=True)
                                setattr(run, f"_consecutive_block_{username}", 0)
                                await _rotate_ip_and_refresh(page, customer, actual_err, username=username)
                                return time.time() + 60.0, last_poll_debug, False
                            else:
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Transient block signal ({actual_err}) [strike #{c_block}/3]. Performing in-place F5 reload...\n", flush=True)
                                try:
                                    await page.reload(wait_until="domcontentloaded", timeout=35000)
                                    await asyncio.sleep(2)
                                    from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
                                    if await is_cloudflare_challenge(page):
                                        await solve_cloudflare_challenge(page, log)
                                except Exception:
                                    pass
                                return time.time() + 30.0, last_poll_debug, False

                    err_msg = str(res.get("error", ""))
                    print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': slot fetch returned error ({err_msg}). Staying parked on homepage.\n", flush=True)

                if res and res.get("success"):
                    results = res["results"]
                    dates_found = False

                    # Check for session expiration first (returns 401/403/login HTML).
                    if _looks_like_expired_session(res):
                        print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Session expired or unauthenticated. Running automatic session recovery...\n", flush=True)
                        log.warning(f" Session expired signal for '{customer}'. Triggering automatic login recovery.")
                        success = await _recover_with_retry(page, customer, username)
                        if success:
                            log.info(f" Session successfully restored for '{customer}'. Ready for next polling turn.")
                        else:
                            log.error(f" Session recovery failed for '{customer}'. Will retry next cycle.")
                        return time.time(), last_poll_debug, False

                    # CF API-level block (proxy IP rate-limited at Cloudflare)
                    if _looks_like_cf_api_block(res):
                        actual_err = _extract_actual_error(res)
                        err_low = actual_err.lower()
                        is_portal_429 = "429" in err_low or "too many requests" in err_low or "rate limit" in err_low
                        if is_portal_429:
                            c_429 = getattr(run, f"_consecutive_429_{username}", 0) + 1
                            setattr(run, f"_consecutive_429_{username}", c_429)
                            is_direct = (get_proxy_mode() == "DIRECT_ONLY")
                            rot_429 = getattr(run, f"_rot_429_{username}", 0)

                            is_dynamics_quota = ("customer self-service" in err_low or "visa application home" in err_low)
                            should_hot_swap = (
                                (not is_direct and rot_429 >= 1 and c_429 >= 2)
                                or (is_dynamics_quota and c_429 >= 2)
                                or (is_direct and c_429 >= 2)
                            )
                            if should_hot_swap:
                                setattr(run, f"_consecutive_429_{username}", 0)
                                setattr(run, f"_rot_429_{username}", 0)
                                swapped, locked_c, prom_c = _swap_poller_with_standby(
                                    username,
                                    reason=f"Portal viewing quota (HTTP 429 Dynamics 365 limit): {actual_err}"
                                )
                                if swapped:
                                    return time.time() + 3600.0, last_poll_debug, False
                                else:
                                    cooldown_sec = 3600.0
                                    _set_account_quota_cooldown(username, cooldown_sec)
                                    print(f"\n[MONITOR] [ACCOUNT QUOTA LIMIT] '{customer}' ({username}) portal viewing quota reached (HTTP 429 persisted across proxy IP rotation).", flush=True)
                                    print(f"[MONITOR] Putting '{customer}' to rest for 60m so other accounts poll smoothly without delays or errors.\n", flush=True)
                                    log.warning(f"[ACCOUNT-REST] '{customer}' placed in account quota rest for 60m.")
                                    return time.time() + cooldown_sec, last_poll_debug, False
                            elif not is_direct and c_429 >= 3:
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Repeated 429 on proxy IP (strike #{c_429}). Auto-rotating to fresh Webshare proxy slot...\n", flush=True)
                                log.warning(f"[PROXY-ROTATE] '{customer}' hit consecutive 429s ({c_429}) on proxy IP -> rotating to fresh slot.")
                                setattr(run, f"_consecutive_429_{username}", 0)
                                setattr(run, f"_rot_429_{username}", rot_429 + 1)
                                await _rotate_ip_and_refresh(page, customer, actual_err, username=username)
                                return time.time() + 60.0, last_poll_debug, False
                            else:
                                backoff_sec = 60.0 if not is_direct else min(max(180.0, 60.0 * c_429), 240.0)
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Portal API rate limit 429 ({actual_err}) [strike #{c_429}/3]. Soft cooldown {backoff_sec:.0f}s...\n", flush=True)
                                log.info(f"[COOLDOWN] '{customer}' cooling down {backoff_sec:.0f}s on sticky IP to allow rate limit window to expire.")
                                return time.time() + backoff_sec, last_poll_debug, False
                        else:
                            c_block = getattr(run, f"_consecutive_block_{username}", 0) + 1
                            setattr(run, f"_consecutive_block_{username}", c_block)
                            if c_block >= 3:
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Persistent server block ({actual_err}) after 3 attempts. Auto-rotating to fresh Webshare proxy slot...\n", flush=True)
                                setattr(run, f"_consecutive_block_{username}", 0)
                                await _rotate_ip_and_refresh(page, customer, actual_err, username=username)
                                return time.time() + 60.0, last_poll_debug, False
                            else:
                                print(f"\n[MONITOR] Poll #{poll_no} for '{customer}': Transient block signal ({actual_err}) [strike #{c_block}/3]. Performing in-place F5 reload...\n", flush=True)
                                try:
                                    await page.reload(wait_until="domcontentloaded", timeout=35000)
                                    await asyncio.sleep(2)
                                    from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
                                    if await is_cloudflare_challenge(page):
                                        await solve_cloudflare_challenge(page, log)
                                except Exception:
                                    pass
                                return time.time() + 30.0, last_poll_debug, False

                    _reset_rate_limit_failures(username=username)

                    log_lines = [
                        f"[{log_stamp()}] Poll #{poll_no} by '{customer}':",
                        f"  Type: OFC Slot"
                    ]
                    summary_parts = []
                    city_details = []
                    for city, dates in results.items():
                        if isinstance(dates, list):
                            n = len(dates)
                            dates_found = dates_found or n > 0
                            summary_parts.append(f"{city}={n}d")
                            log_lines.append(f"  {city}: {n} date(s): {dates}")
                            status_str = f"{n} slot(s) available" if n > 0 else "0 slots available"
                            city_details.append(f"   - {city:<12} : {status_str}")
                        else:
                            summary_parts.append(f"{city}=ERR")
                            log_lines.append(f"  {city}: {dates}")
                            err_str = dates.get("error", dates) if isinstance(dates, dict) else dates
                            city_details.append(f"   - {city:<12} : ERROR ({err_str})")

                    summary_str = ", ".join(summary_parts)
                    date_range = f"{my_config.get('ofcStartDate', 'Any')} to {my_config.get('ofcEndDate', 'Any')}"
                    cities_checked = ", ".join(my_config.get("ofcCities", ["ALL"]))

                    formatted_block = (
                        f"\n"
                        f"================================================================================\n"
                        f"  [SLOT MONITOR] Polling Cycle #{poll_no} | Account: {customer} ({username}) | {ts}\n"
                        f"  ------------------------------------------------------------------------------\n"
                        f"  Mode          : Fleet Radar Mode [Scanning ALL 5 Cities | Pollers: {num_pollers}/3 | Gap: {fleet_gap:.0f}s]\n"
                        f"  Appd          : {my_config.get('appd', 'N/A')}\n"
                        f"  Target Cities : {cities_checked}\n"
                        f"  Date Range    : {date_range}\n"
                        f"  Type          : OFC Slot\n"
                        f"\n"
                        f"  [AVAILABILITY RESPONSE]\n"
                        + "\n".join(city_details) + "\n"
                        f"\n"
                        f"  Next check in : ~{next_cd_sec}s (Browser parked safely on homepage)\n"
                        f"================================================================================\n"
                    )
                    print(formatted_block, flush=True)
                    # Emit standard [MONITOR] summary line for gui.py live slot parser
                    print(f"\n[MONITOR] Poll #{poll_no} '{customer}' -> {summary_str}\n", flush=True)

                    # Only park back to homepage if NO dates found!
                    # If dates_found is True, DO NOT navigate away to homepage (preserves active scheduling session & eliminates 25s delay)!
                    if not dates_found:
                        try:
                            cur_url = (page.url or "").lower()
                            if not cur_url.rstrip("/").endswith("/en-us") and not any(k in cur_url for k in ["waitingroom", "challenge"]):
                                log.info(f"[PARK] Returning '{customer}' safely to portal homepage /en-US/...")
                                await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=25000)
                        except Exception as re_err:
                            log.debug(f"Homepage park non-fatal: {re_err}")

                    # Write to file
                    try:
                        Path("logs").mkdir(exist_ok=True)
                        with open("logs/polling.log", "a", encoding="utf-8") as f:
                            f.write("\n".join(log_lines) + "\n")
                    except Exception as e:
                        print(f"[POLLING-RESULT] Error saving log: {e}")

                    if dates_found:
                        # NOTE: No Telegram message here  ->  a slot notification is
                        # only sent later, and ONLY when the slots actually match
                        # the user's cities AND date range (see the matched branch
                        # below, which includes full details and matched dates).
                        # Out-of-range sightings are logged, never messaged.

                        # --- SCOUT-FIRST FAST PASS & SELF-BOOKING LOGIC ---
                        # If the scout account itself matches the detected slot, it claims
                        # Priority #0 immediately on its already-open, authenticated session (<100ms)
                        # BEFORE broadcasting to standby accounts. Standby accounts are then
                        # queued with Priority >= 1 behind the scout as backup.
                        uid = safe_id(username)
                        state_file = (
                            Path(__file__).parent / f"state_{uid}.json"
                        )
                        instant_booking = True

                        polling_state_file = (
                            Path(__file__).parent / "polling_state.json"
                        )
                        if polling_state_file.exists():
                            try:
                                with open(polling_state_file, "r") as f:
                                    instant_booking = json.load(f).get(
                                        "instant_booking",
                                        True,
                                    )
                            except Exception:
                                pass

                        scout_self_booked = False
                        # The atomic queue helper checks rest/busy/pending state.
                        if instant_booking and ACCOUNTS_FILE.exists():
                            all_accounts = json.loads(
                                ACCOUNTS_FILE.read_text(encoding="utf-8")
                            )
                            my_config = next(
                                (
                                    acc
                                    for acc in all_accounts
                                    if acc.get("customer_name") == customer
                                    or acc.get("username") == username
                                ),
                                None,
                            )

                            # Check if the monitoring account itself needs an appointment (has not booked yet)
                            if my_config and (
                                my_config.get("booked") is True
                                or str(my_config.get("booking_status", "")).strip().upper() == "BOOKED SUCCESS"
                            ):
                                my_config = None

                            if my_config:
                                matched, matched_city, earliest_date = (
                                    _match_polled_ofc_dates(
                                        results,
                                        my_config,
                                    )
                                )

                                # Book-at-any-cost: if the strict config match
                                # fails but slots ARE visible, take the earliest
                                # candidate across all cities so OFC is booked
                                # instantly instead of waiting for a perfect
                                # criteria match. Verification still enforces
                                # the >= MIN_BOOKING_SLOTS rule before booking.
                                relaxed = False
                                if not matched:
                                    fb_city, fb_date = _find_any_candidate(results, my_config)
                                    if fb_city:
                                        matched, matched_city, earliest_date = True, fb_city, fb_date
                                        relaxed = True

                                # RULE: slots visible but OUTSIDE the user's cities /
                                # date range  ->  log only. NO Telegram message for
                                # out-of-range sightings (client request).
                                if not matched:
                                    any_dates = [
                                        (c, str(d.get("Date", ""))[:10])
                                        for c, dl in results.items()
                                        if isinstance(dl, list) for d in dl
                                        if str(d.get("Date", ""))[:10]
                                    ]
                                    if any_dates:
                                        seen_line = ", ".join(
                                            f"{c} {d}" for c, d in any_dates[:8]
                                        ) + (" " if len(any_dates) > 8 else "")
                                        log.info(
                                            f"[MONITOR] Slots visible ({seen_line}) outside poller '{customer}' range. "
                                            f"Cross-account dispatcher actively scanning fleet VIP sniper accounts."
                                        )

                                if matched:
                                    # --- MULTI-CITY CANDIDATE COLLECTION -
                                    # Collect EVERY (city, date) inside the user's
                                    # configured date range across all their cities.
                                    # The sniper will try them all, earliest first, so
                                    # a single sell-out in one city can't waste the
                                    # trigger  ->  the next-best candidate is booked.
                                    user_cities = []
                                    for c in my_config.get("ofcCities", []):
                                        cu = "NEW DELHI" if c.upper() == "DELHI" else c.upper()
                                        if cu not in user_cities:
                                            user_cities.append(cu)

                                    cfg_start = my_config.get("ofcStartDate", "")
                                    cfg_end = my_config.get("ofcEndDate", "")
                                    if my_config.get("prevent_immediate"):
                                        dyn_start = (datetime.today() + timedelta(days=3)).strftime("%Y-%m-%d")
                                        if not cfg_start or cfg_start < dyn_start:
                                            cfg_start = dyn_start
                                    # Phantom same-day listings can't be booked  -> 
                                    # filter them out so they never fire triggers.
                                    lead_floor = (datetime.today() + timedelta(days=_min_slot_lead_days())).strftime("%Y-%m-%d")
                                    if not cfg_start or cfg_start < lead_floor:
                                        cfg_start = lead_floor

                                    all_candidates = []  # [(date, city)] sorted by date
                                    for city_l, dates_l in results.items():
                                        if city_l.upper() not in user_cities or not isinstance(dates_l, list):
                                            continue
                                        for d_item in dates_l:
                                            ds = str((d_item or {}).get("Date", ""))[:10]
                                            if cfg_start <= ds <= cfg_end:
                                                all_candidates.append((ds, city_l.upper()))
                                    all_candidates.sort()
                                    all_candidates = mid_out_order(all_candidates)

                                    if not all_candidates:
                                        all_candidates = [(earliest_date, matched_city.upper())]

                                    target_slot_date, target_slot_city = all_candidates[0]
                                    earliest_date = target_slot_date
                                    matched_city = target_slot_city

                                    candidate_dates = [d for d, _ in all_candidates]
                                    candidate_cities = []
                                    for _d, c in all_candidates:
                                        if c not in candidate_cities:
                                            candidate_cities.append(c)
                                    trigger_start = min(candidate_dates)
                                    # End at the user's configured end (never narrower
                                    # than what we saw, so the sniper can still catch
                                    # dates that appeared a moment ago).
                                    trigger_end = cfg_end if cfg_end and cfg_end >= trigger_start else max(candidate_dates)

                                    log.info(
                                        f"[MONITOR]  {len(all_candidates)} candidate slot(s) across "
                                        f"{len(candidate_cities)} city/cities: "
                                        + ", ".join(f"{c} {d}" for d, c in all_candidates[:12])
                                        + (" " if len(all_candidates) > 12 else "")
                                    )

                                    # Mid-flight Telegram notification removed to preserve booking speed

                                    action_mode = str(
                                        my_config.get(
                                            "action_mode",
                                            "SNIPER",
                                        )
                                    ).strip().upper()

                                    action_type = (
                                        "RESCHEDULE_FULL"
                                        if action_mode == "RESCHEDULE_FULL"
                                        else "SNIPER"
                                    )

                                    trigger_key = (
                                        f"background|{safe_id(username)}|"
                                        f"{matched_city.upper()}|"
                                        f"{earliest_date}|{action_type}"
                                    )

                                    # --- MONITORING PAUSED: handing control to booking ---
                                    print(
                                        f"[MONITOR] [PARK]  PAUSING monitoring  ->  candidate slot found "
                                        f"({matched_city} {earliest_date}"
                                        f"{', relaxed match' if relaxed else ''}). "
                                        f"Queueing instant OFC booking",
                                        flush=True,
                                    )

                                    trigger_updates = {
                                        "pending": True,
                                        "priority": 0,
                                        "source": "scout_fast_pass",
                                        "is_scout_self_book": True,
                                        "trigger_timestamp": time.time(),
                                        "trigger_key": trigger_key,
                                        "action_type": action_type,
                                        # ALL candidate cities (earliest first)  ->  the
                                        # sniper iterates them and books the first
                                        # available slot; verification pre-checks them.
                                        "ofcCities": candidate_cities,
                                        "ofcPriorityCity": candidate_cities[0],
                                        # Full in-range window: earliest seen date  -> 
                                        # user's configured end date.
                                        "ofcStartDate": trigger_start,
                                        "ofcEndDate": trigger_end,
                                        "consularCities": my_config.get(
                                            "consularCities", []
                                        ),
                                        "consularPriorityCity": my_config.get(
                                            "consularPriorityCity", ""
                                        ),
                                        "consularStartDate": my_config.get(
                                            "consularStartDate", ""
                                        ),
                                        "consularEndDate": my_config.get(
                                            "consularEndDate", ""
                                        ),
                                        "customer_name": customer,
                                        "prevent_immediate": my_config.get(
                                            "prevent_immediate", False
                                        ),
                                        "multiPerson": my_config.get(
                                            "multiPerson", False
                                        ),
                                        # --- ONE-SHOT CACHED BOOKING PACKAGE ---
                                        # Everything the hot booking path needs
                                        # to book with 2-4 in-page API calls
                                        # instead of re-discovering every city
                                        # and date. The phase-1 token was JUST
                                        # returned by this poll; the booking
                                        # revalidates it in-page and obtains a
                                        # fresh token immediately before the
                                        # BOOK POST (never trusts it blindly).
                                        "cached_booking": {
                                            "city": matched_city.upper(),
                                            "date": earliest_date,
                                            "phase1_token": (
                                                (res.get("tokens") or {}).get(
                                                    matched_city.upper()
                                                )
                                                or (res.get("tokens") or {}).get(
                                                    matched_city
                                                )
                                                or ""
                                            ),
                                            "candidates": [
                                                {"city": c, "date": d}
                                                for d, c in all_candidates[:5]
                                            ],
                                            "captured_at": time.time(),
                                            "source": "monitor",
                                        },
                                    }

                                    # Retry hard  ->  a slot must NEVER be lost to
                                    # a transient lock race. Retries span ~2s
                                    # max; _acquire_lock also force-clears any
                                    # stale (>15s) orphaned lock file itself.
                                    queued, queue_reason = False, "lock_timeout"
                                    for _attempt in range(8):
                                        queued, queue_reason = (
                                            try_queue_local_trigger(
                                                state_file,
                                                trigger_updates,
                                            )
                                        )
                                        if queued or queue_reason != "lock_timeout":
                                            break
                                        time.sleep(0.25)

                                    if queued:
                                        scout_self_booked = True
                                        booking_coordinator.submit_trigger(
                                            account_uid=uid,
                                            trigger_key=trigger_key,
                                            slot_info={
                                                "city": matched_city,
                                                "date": earliest_date,
                                                "priority": 0,
                                                "source": "scout_fast_pass",
                                            },
                                            max_age_seconds=CACHED_TRIGGER_MAX_AGE_SECONDS,
                                        )
                                        _open_slot_window(customer, matched_city, earliest_date)
                                        ts_sb = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        self_box = (
                                            f"\n"
                                            f"================================================================================\n"
                                            f"[SNIPER] [SCOUT FAST-PASS: PRIORITY #0] Self-Booking Triggered | {ts_sb}\n"
                                            f"--------------------------------------------------------------------------------\n"
                                            f"Account: {customer} ({username})\n"
                                            f"Target City: {matched_city}\n"
                                            f"Earliest Available Date: {earliest_date}\n"
                                            f"Action Mode: ONE-SHOT CACHED SNIPER (Head-of-Line Fast Pass)\n"
                                            f"Status: Scout booking first (<100ms) -> Standby accounts queued as backup!\n"
                                            f"================================================================================\n"
                                        )
                                        print(self_box, flush=True)
                                        log.info(
                                            f"[SCOUT FAST-PASS] Scout '{customer}' claimed Priority #0! "
                                            f"Broadcasting to standby accounts behind scout..."
                                        )
                                        try:
                                            await _broadcast_results(results, customer)
                                        except Exception as bc_err:
                                            log.warning(f"Standby broadcast after scout pass: {bc_err}")

                                        return (
                                            last_background_poll,
                                            last_poll_debug,
                                            True,
                                        )

                                    print(
                                        f"[POLLING-RESULT] [ALERT]  "
                                        f"Self-booking not queued for "
                                        f"'{customer}': {queue_reason}. "
                                        f"[RESUME]  Resuming slot monitoring.",
                                        flush=True,
                                    )

                        # If scout did not self-book, broadcast to standby accounts immediately
                        if not scout_self_booked:
                            await _broadcast_results(results, customer)

                else:
                    print(f"[MONITOR]  Poll #{poll_no} returned invalid/failed data: {res}", flush=True)
                    # Cloudflare block / WAF rate limit -> back off & leave the session untouched
                    if isinstance(res, dict) and _looks_like_cf_api_block(res):
                        actual_err = _extract_actual_error(res) or "Cloudflare block (round-robin poll)"
                        print(f"[MONITOR]  Poll #{poll_no}: Cloudflare block / WAF rate limit ({actual_err}) -> backing off (no session change).", flush=True)
                        await _rotate_ip_and_refresh(page, customer, actual_err, username=username)
                        return time.time(), last_poll_debug, False
                    # Check if this looks like an expired session / block page
                    if isinstance(res, dict) and _looks_like_expired_session(res):
                        print(f"[MONITOR]  Poll #{poll_no}: session appears expired  ->  triggering recovery.", flush=True)
                        last_background_poll = 0
                        raise SessionExpiredError(f"Poll returned session-expired data: {res}")
                    else:
                        print(f"[MONITOR]  Poll #{poll_no}: transient failure, browser parked safely on homepage, will retry next cycle.", flush=True)
        except SessionExpiredError:
            # Propagate so the main watch loop runs recover_session().
            # Reset last_background_poll so we retry polling promptly after
            # the session is restored rather than waiting a full cooldown.
            last_background_poll = 0
            raise
        except Exception as e:
            print(f"[MONITOR]  Poll #{poll_no} unexpected error: {e}", flush=True)
        finally:
            # Guaranteed lease release and round-robin turn advancement
            with _polling_state_lock(polling_lock_file) as locked:
                if locked:
                    try:
                        with open(polling_state_file, "r") as f:
                            pstate = json.load(f)
                        now_ts = time.time()
                        pstate["active_poller"] = None
                        pstate["active_poller_since"] = 0
                        pstate["poll_finished_at"] = now_ts
                        pstate["global_last_poll"] = now_ts
                        pstate["last_poller"] = username

                        active_monitors = _get_active_monitors(pstate, username)
                        if len(active_monitors) > 1:
                            try:
                                curr_idx = active_monitors.index(username)
                                next_poller = active_monitors[(curr_idx + 1) % len(active_monitors)]
                            except ValueError:
                                next_poller = active_monitors[0]
                            pstate["current_turn"] = next_poller
                            pstate["turn_assigned_at"] = now_ts
                            log.info(f"[ROUND-ROBIN] Poll #{poll_no} finished by '{username}'. Turn passed to -> '{next_poller}' (Next poll in ~{fleet_gap:.0f}s)\n")
                        else:
                            pstate["current_turn"] = None
                            pstate["turn_assigned_at"] = now_ts

                        with open(polling_state_file, "w") as f:
                            json.dump(pstate, f, indent=2)
                    except Exception as rel_err:
                        log.warning(f"Error releasing polling lease: {rel_err}")

    return last_background_poll, last_poll_debug, False

#  Main runner loop 

async def run(cdp_port: int, customer: str, username: str):
    uid = safe_id(username)
    state_file = Path(__file__).parent / f"state_{uid}.json"

    # Custom logger formatting to show customer prefix
    log.name = f"booking:{customer}"

    # Initialise state file  ->  start completely fresh, clear stale cached bookings and triggers
    _update_state(state_file, {
        "booking_running": False,
        "customer_name": customer,
        "waitingForConsular": False,
        "bookedOfcDate": None,
        "waitStartTime": None,
        "ofcBookedAt": None,
        "pending": False,
        "completed": False,
        "cached_booking": None,
        "trigger_key": None,
        "trigger_timestamp": None,
        "last_booking_failure_reason": None,
    })

    last_polling_time = 0
    next_poll_delay = random.randint(180, 240)

    runner_start_time = time.time()
    setattr(run, f"_start_time_{username}", runner_start_time)
    last_background_poll = 0
    was_polling_active = False
    last_poll_debug = 0

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, cdp_port, log, handle_dialogs=True)

        # Cut proxy bandwidth: block telemetry hosts + heavy assets.
        await _install_traffic_saver(context)

        def handle_console(msg):
            text = msg.text
            is_match = any(k in text for k in ["Sniper", "Consular", "OFC", "Booking", "Heartbeat", "HEARTBEAT", "Session", "session"])
            is_err = msg.type == "error" and "usvisascheduling.com" in text

            if is_match or is_err:
                log_file = Path("logs/browser.log")
                log_file.parent.mkdir(parents=True, exist_ok=True)
                timestamp = log_stamp()
                prefix = "[ERROR]" if msg.type == "error" else ("[WARN]" if msg.type == "warning" else "[INFO]")
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{timestamp}] {prefix} [{customer}] {text}\n")
                # Also write to MongoDB
                db_logger.log_browser_console(timestamp, prefix, customer, text)
                # Live visibility in orchestrator log & terminal console
                log.info(f"🌐 [BROWSER EXTENSION] [{customer}] {text}")

        page.on("console", handle_console)

        # Inject listener for browser's session expiry broadcast
        listener_script = """
            // Always reset the flag on every navigation to prevent stale triggers
            window.__browserSessionExpired = false;
            if (!window.__sniperExpiryListenerAdded) {
                window.__sniperExpiryListenerAdded = true;
                window.addEventListener("message", (event) => {
                    if (event.source !== window || !event.data || !event.data.action) return;
                    if (event.data.action === "SESSION_EXPIRED") {
                        window.__browserSessionExpired = true; 
                    }
                });
            }
        """
        await page.add_init_script(listener_script)
        try:
            await page.evaluate(listener_script)
        except Exception as e:
            log.warning(f"Could not instantly bind expiry listener: {e}")

        log.info("Waiting for portal ")
        if not await ensure_on_portal(page, log):
            log.error("Could not reach portal. Exiting.")
            sys.exit(1)

        acct_cfg = _load_account_config(username)
        acct_role = str(acct_cfg.get("role", "")).strip().upper()
        log.info("=" * 60)
        log.info(f" Booking runner ready  ->  watching {state_file.name}")
        if acct_role == "RESERVED_BOOKING":
            log.info(" [TARGET] RESERVED_BOOKING STANDBY: Slot monitoring delegated to MONITOR pool. Listening for triggers.")
        else:
            log.info(f" Slot monitoring active on schedule page (cd: "
                     f"{os.getenv('MONITOR_INTERVAL_MIN', '90')}-{os.getenv('MONITOR_INTERVAL_MAX', '120')}s, "
                     f"fleet gap: {os.getenv('FLEET_MONITOR_GAP', '45')}s)")
        log.info("=" * 60)

        # --- TELEGRAM: [LAUNCH] MONITORING STARTED ---
        try:
            acct_cfg = _load_account_config(username)
            _safe_send(
                send_monitoring_status,
                customer=customer,
                user_id=username,
                mode=acct_cfg.get("action_type", "SNIPER"),
                ofc_cities=acct_cfg.get("ofcCities", []),
                ofc_range=f"{acct_cfg.get('ofcStartDate', 'N/A')}  ->  {acct_cfg.get('ofcEndDate', 'N/A')}",
                consular_cities=acct_cfg.get("consularCities", []),
                consular_range=f"{acct_cfg.get('consularStartDate', 'N/A')}  ->  {acct_cfg.get('consularEndDate', 'N/A')}",
                started=True,
            )
        except Exception as e:
            log.warning(f"Monitoring-started notification failed: {e}")

        runner_start_time = time.time()
        last_keep_alive = time.time()
        last_session_keepalive = time.time()
        now = time.time()
        # Throttles the "fleet busy, deferring" log (one line / 10 s max).
        _last_heartbeat_write = 0.0
        while True:
            try:
                # Active runner heartbeat (updated every 5s for cross-account health checks)
                _now_loop = time.time()
                if _now_loop - _last_heartbeat_write >= 5.0:
                    _last_heartbeat_write = _now_loop
                    _update_state(state_file, {
                        "booking_runner_alive": True,
                        "booking_runner_heartbeat": _now_loop,
                        "booking_runner_pid": os.getpid(),
                    })
                # --- Check Rest Period FIRST -
                state = _read_state(state_file)
                rest_until = state.get("rest_until", 0)
                is_resting = bool(rest_until and time.time() < rest_until)

                # If resting and pending, clear the pending state so we don't block polling
                if is_resting and state.get("pending"):
                    log.info(" Account is in a rest period. Ignoring and clearing pending booking trigger.")
                    _update_state(state_file, {"pending": False})
                    state["pending"] = False

                # --- Check for pending trigger -
                if state.get("pending") and not is_resting:
                    print("\n" + "=" * 60)

                    # --- QUEUE ENTRY: join the sequential booking queue -
                    now_ts = time.time()
                    prio = int(state.get("priority", 0 if state.get("is_scout_self_book") else 10))
                    src = str(state.get("source", "scout_fast_pass" if state.get("is_scout_self_book") else "cross_account"))
                    _enqueue(uid, {
                        "priority": prio,
                        "source": src,
                        "detected_at": float(state.get("trigger_timestamp") or now_ts),
                    })
                    log.info(
                        f" Enqueued '{customer}' (uid={uid}, Priority=#{prio}, Source={src}) into the "
                        f"booking queue. Waiting for turn..."
                    )

                    # Wait until this account reaches the front of the queue.
                    turn_ok = await wait_for_turn(
                        uid,
                        trigger_timestamp=now_ts,
                        log_owner=log,
                    )
                    if not turn_ok:
                        log.warning(
                            f"[ALERT]  Trigger for '{customer}' went stale while "
                            f"waiting in queue  ->  abandoning."
                        )
                        _close_slot_window()
                        release_fleet_booking_lock(uid)
                        _advance_queue(uid)  # allow next account to proceed
                        _update_state(state_file, {"pending": False})
                        await asyncio.sleep(0.1)
                        continue

                    # --- Re-read state after queue wait ---
                    state = _read_state(state_file)
                    if not state.get("pending"):
                        log.info(
                            f"[ALERT]  Trigger for '{customer}' no longer pending "
                            f"(cleared while waiting)  ->  skipping."
                        )
                        _close_slot_window()
                        release_fleet_booking_lock(uid)
                        _advance_queue(uid)  # allow next account to proceed
                        await asyncio.sleep(0.1)
                        continue

                    # --- FLEET SEQUENCING (One-Shot architecture) -
                    # Only ONE account may execute a booking attempt at a time.
                    _holder = fleet_lock_holder()
                    if _holder and _holder != uid:
                        _now_d_1 = time.time()
                        if _now_d_1 - _last_fleet_defer_log > 10.0:
                            _last_fleet_defer_log = _now_d_1
                            log.info(
                                f"[LOCK]  [ONE-SHOT] Fleet booking lock held by "
                                f"'{_holder}'  ->  trigger for '{customer}' "
                                f"stays queued (sequential booking)."
                            )
                        await asyncio.sleep(1.0)
                        continue

                    # Check the original trigger timestamp BEFORE modifying the state file.
                    trigger_ts = state.get("trigger_timestamp")
                    trigger_delay = None

                    if trigger_ts:
                        try:
                            trigger_delay = time.time() - float(trigger_ts)
                        except (TypeError, ValueError):
                            log.warning(
                                f"Could not read trigger timestamp: {trigger_ts!r}"
                            )

                    # Never execute a trigger that is more than 5 seconds old for competitive slots.
                    # Slots disappear quickly, so stale triggers must be skipped immediately.
                    #
                    # ONE-SHOT exception: a trigger carrying a cached booking
                    # package revalidates the slot in-page with 2 cheap calls
                    # before any booking POST, so it may be executed up to
                    # CACHED_TRIGGER_MAX_AGE_SECONDS late (e.g. while the fleet
                    # was busy on another account). Beyond that it is discarded
                    # exactly like the legacy rule.
                    _cached_trigger = (
                        CACHED_BOOKING_ENABLED
                        and isinstance(state.get("cached_booking"), dict)
                    )
                    _stale_limit = (
                        CACHED_TRIGGER_MAX_AGE_SECONDS if _cached_trigger else 120.0
                    )
                    if trigger_delay is not None and trigger_delay > _stale_limit:
                        _close_slot_window()
                        release_fleet_booking_lock(uid)
                        _advance_queue(uid)  # allow next account to proceed
                        log.warning(
                            f"[ALERT]  Skipping stale trigger for '{customer}': "
                            f"it is {trigger_delay:.1f} seconds old. Slots likely gone."
                        )

                        _update_state(
                            state_file,
                            {
                                "pending": False,
                                "booking_running": False,
                            },
                        )

                        last_keep_alive = time.time()
                        await asyncio.sleep(0.1)
                        continue

                    # --- FLEET BOOKING LOCK ---
                    # Acquired AFTER the turn arrives and the trigger is confirmed
                    # fresh. The inter-account gap (FLEET_BOOKING_GAP_SECONDS)
                    # inside try_acquire keeps consecutive attempts apart.
                    lock_ok, lock_reason = try_acquire_fleet_booking_lock(uid)
                    if not lock_ok:
                        log.info(
                            f"[LOCK]  [ONE-SHOT] Fleet booking lock not acquirable "
                            f"({lock_reason})  ->  deferring trigger for '{customer}'."
                        )
                        await asyncio.sleep(1.0)
                        continue

                    # The trigger is fresh AND this account holds the lock.
                    # Mark the browser as running.
                    _update_state(
                        state_file,
                        {
                            "booking_running": True,
                            "pending": False,
                        },
                    )

                    log.info(
                        f" Pending trigger detected for '{customer}'."
                    )

                    if trigger_delay is not None:
                        if trigger_delay > 10.0:
                            log.warning(
                                f" Trigger execution delayed by "
                                f"{trigger_delay:.1f} seconds! "
                                f"Reason: Bot was busy or in Cloudflare queue."
                            )
                        else:
                            log.info(
                                f"[FAST] Trigger picked up swiftly in "
                                f"{trigger_delay:.3f} seconds."
                            )

                    action_type = state.get("action_type")

                    trigger = {k: state[k] for k in [
                        "action_type", "trigger_key", "trigger_timestamp",
                        "ofcCities", "ofcPriorityCity", "ofcStartDate", "ofcEndDate",
                        "consularCities", "consularPriorityCity", "consularStartDate", "consularEndDate",
                        "customer_name", "prevent_immediate", "multiPerson",
                        "cached_booking", "primaryId", "appd"
                    ] if k in state}
                    trigger["account_uid"] = uid
                    if not trigger.get("customer_name"):
                        trigger["customer_name"] = customer

                    # --- HARD RULE: NEVER fire the sniper into a dead browser ---
                    try:
                        tab_healthy = await heal_tab_for_booking(
                            page, log, reason="trigger pre-flight"
                        )
                    except Exception as e:
                        log.warning(
                            f"Pre-flight health check warning (will proceed anyway): {e}"
                        )
                        tab_healthy = True

                    if not tab_healthy:
                        log.error(
                            "[ALERT] Skipping booking -> this account's browser is "
                            "NOT alive. Releasing trigger + fleet lock so a LIVE "
                            "account can book."
                        )
                        _update_state(state_file, {
                            "booking_running": False,
                            "pending": False,
                            "ofcBookedAt": None,
                        })
                        _close_slot_window()
                        release_fleet_booking_lock(uid)
                        _advance_queue(uid)  # allow next account to proceed
                        last_keep_alive = time.time()
                        await asyncio.sleep(0.2)
                        continue

                    # --- Verify slot availability before attempting booking ---
                    # For SNIPER mode, the in-page entries revalidation inside the one-shot
                    # executor IS the verification (fresh token + entry check in 200ms
                    # before the booking POST). A separate 20s Python fetch is completely
                    # redundant and falsely vetoes real slots when parked on the homepage.
                    _skip_verify = (
                        action_type == "SNIPER"
                        or str(trigger.get("trigger_key", "")).startswith("background")
                        or isinstance(trigger.get("cached_booking"), dict)
                        or os.getenv("SKIP_PREBOOK_VERIFICATION", "1").strip().lower() not in ("0", "false", "no")
                    )
                    if _skip_verify:
                        log.info(
                            "[FAST] One-shot cached trigger -> in-page entries revalidation "
                            "IS verification; firing immediate in-page booking now."
                        )
                    elif action_type == "RESCHEDULE_FULL":
                        log.info("[CHECK] Pre-booking slot verification...")
                        slot_available, preflight_slot_state, verified_info = await _verify_slot_availability(page, trigger, log)
                        if not slot_available:
                            log.warning(
                                f"[FAIL] Slot verification not confirmed ({preflight_slot_state.value}): "
                                f"{verified_info}. Skipping booking attempt to preserve rate limits and session."
                            )
                            _update_state(
                                state_file,
                                {
                                    "booking_running": False,
                                    "pending": False,
                                },
                            )
                            _close_slot_window()
                            release_fleet_booking_lock(uid)
                            _advance_queue(uid)
                            last_keep_alive = time.time()
                            await asyncio.sleep(0.1)
                            continue

                    # --- Execute action ---
                    success = False
                    context = {}
                    try:
                        if action_type == "SNIPER":
                            # --- ONE-SHOT CACHED BOOKING 
                            ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            target_city = (
                                trigger.get("ofcPriorityCity")
                                or (trigger.get("cached_booking") or {}).get("city")
                                or "N/A"
                            )
                            target_date = (trigger.get("cached_booking") or {}).get("date", "N/A")
                            booking_box = (
                                f"\n{ANSI_ORANGE}"
                                f"================================================================================\n"
                                f"🎯 [OFC BOOKING HIT] EXECUTING LIVE APPOINTMENT | {ts_start}\n"
                                f"--------------------------------------------------------------------------------\n"
                                f"Account     : {customer} ({username})\n"
                                f"Target City : {target_city}\n"
                                f"Target Date : {target_date}\n"
                                f"Action Mode : {action_type}\n"
                                f"Engine      : CHROME EXTENSION SNIPER (DOM Direct / Fast-Pass Token)\n"
                                f"================================================================================{ANSI_RESET}\n"
                            )
                            print(booking_box, flush=True)
                            log.info(f"🎯 [OFC BOOKING HIT] Account '{customer}' ({username}) FIRING OFC BOOKING for {target_city} on {target_date} via Extension Sniper...")
                            ofc_success, ofc_context = await trigger_cached_ofc_booking(page, trigger, log)

                            if ofc_success:
                                bookedOfcDate = ofc_context.get("bookedOfcDate")
                                ofc_city = (
                                    ofc_context.get("bookedCity")
                                    or trigger.get("ofcPriorityCity")
                                    or "N/A"
                                )
                                ts_done = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                success_box = (
                                    f"\n{ANSI_DARK_GREEN}"
                                    f"================================================================================\n"
                                    f"[BOOKING SUCCESS] OFC APPOINTMENT SECURED! | {ts_done}\n"
                                    f"--------------------------------------------------------------------------------\n"
                                    f"Account: {customer} ({username})\n"
                                    f"City: {ofc_city}\n"
                                    f"Booked Date: {bookedOfcDate}\n"
                                    f"Status: OFC Confirmed! Searching for Consular appointment immediately...\n"
                                    f"================================================================================{ANSI_RESET}\n"
                                )
                                print(success_box, flush=True)
                                log.info(f"[SUCCESS] OFC booked successfully! Date: {bookedOfcDate}. Probing Consular now...")
                                record_booking_event(
                                    stage="OFC",
                                    username=username,
                                    customer_name=customer,
                                    city=ofc_city,
                                    slot_date=bookedOfcDate,
                                    status="BOOKED",
                                    notes="OFC appointment secured via fast one-shot"
                                )
                                _record_slot_attempt(customer, "booked", f"OFC booked: {bookedOfcDate} in {ofc_city}", booked=True)

                                # Check if Extension Sniper booked both OFC and Consular in the same pass
                                if ofc_context.get("full_success"):
                                    consular_success = True
                                    consular_context = ofc_context
                                else:
                                    # Immediate Consular Booking attempt via Extension
                                    consular_success, consular_context = await trigger_booking_sniper_consular_only(
                                        page, trigger, bookedOfcDate, log
                                    )
                                if consular_success:
                                    booked_consular_date = consular_context.get("bookedConsularDate")
                                    booked_consular_city = consular_context.get("bookedConsularCity")
                                    log.info(f"[SUCCESS] COMPLETE BOOKING! OFC: {bookedOfcDate} ({ofc_city}), Consular: {booked_consular_date} ({booked_consular_city})")
                                    success = True
                                    context = consular_context
                                    context["bookedOfcDate"] = bookedOfcDate
                                    context["bookedCity"] = ofc_city
                                    record_booking_event(
                                        stage="CONSULAR",
                                        username=username,
                                        customer_name=customer,
                                        city=booked_consular_city,
                                        slot_date=booked_consular_date,
                                        status="BOOKED",
                                        notes=f"Consular appointment secured in {booked_consular_city}"
                                    )
                                    _persist_account_booked_success(
                                        username=username,
                                        customer=customer,
                                        ofc_date=bookedOfcDate,
                                        consular_date=booked_consular_date,
                                        ofc_city=ofc_city,
                                        consular_city=booked_consular_city,
                                    )
                                    try:
                                        from telegram import send_booking_success
                                        send_booking_success(
                                            customer=customer,
                                            user_id=state.get("customer_name"),
                                            ofc_city=ofc_city,
                                            ofc_date=bookedOfcDate,
                                            consular_city=booked_consular_city,
                                            consular_date=booked_consular_date,
                                        )
                                    except Exception as notify_err:
                                        log.warning(f"Notification error: {notify_err}")
                                    _close_slot_window(force=False)
                                else:
                                    log.info(f"[WAIT] Consular not immediately available -- entering background Consular wait mode (32-minute window).")
                                    success = False
                                    context = {
                                        "waitingForConsular": True,
                                        "bookedOfcDate": bookedOfcDate,
                                        "ofcBookedAt": time.time(),
                                    }
                                    _persist_account_ofc_hold(
                                        username=username,
                                        customer=customer,
                                        ofc_date=bookedOfcDate,
                                        ofc_city=ofc_city,
                                    )
                                    record_booking_event(
                                        stage="CONSULAR",
                                        username=username,
                                        customer_name=customer,
                                        city=(trigger.get("consularCities") or ["N/A"])[0],
                                        slot_date="PENDING",
                                        status="FAILED",
                                        notes="Consular slot empty or unavailable on instant check; entering wait mode"
                                    )
                                    try:
                                        send_ofc_success_triggering_consular(
                                            customer=customer,
                                            user_id=state.get("customer_name"),
                                            ofc_city=ofc_city,
                                            ofc_date=bookedOfcDate,
                                        )
                                        log.info(f"OFC BOOKING SUCCESS telegram sent for {customer}")
                                    except Exception as telegram_error:
                                        log.warning(f"Failed to send OFC success telegram: {telegram_error}")
                                    _close_slot_window(force=False)
                            elif ofc_context.get("rate_limited"):
                                log.error(
                                    " One-shot booking hit rate limit (429) -> fast-yielding for background heal."
                                )
                                record_booking_event(
                                    stage="OFC",
                                    username=username,
                                    customer_name=customer,
                                    city=target_city,
                                    slot_date=target_date,
                                    status="FAILED",
                                    notes="Rate limit (429) hit during one-shot cached booking"
                                )
                                _record_slot_attempt(customer, "failed", "Rate limit (429)", booked=False)
                                success = False
                                context = ofc_context
                                _close_slot_window(force=False)
                            else:
                                reason_text = ofc_context.get('error') or ofc_context.get('reason') or 'unavailable'
                                ts_fail = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                fail_box = (
                                    f"\n{ANSI_DARK_RED}"
                                    f"================================================================================\n"
                                    f"❌ [OFC BOOKING FAILED] Slot Could Not Be Booked | {ts_fail}\n"
                                    f"--------------------------------------------------------------------------------\n"
                                    f"Account : {customer} ({username})\n"
                                    f"Target  : {target_city} on {target_date}\n"
                                    f"Reason  : {reason_text}\n"
                                    f"Action  : Lock released immediately (<5ms) -> Handing over to next account\n"
                                    f"================================================================================{ANSI_RESET}\n"
                                )
                                print(fail_box, flush=True)
                                log.error(f"❌ [OFC BOOKING FAILED] Account '{customer}' failed: {reason_text}")
                                record_booking_event(
                                    stage="OFC",
                                    username=username,
                                    customer_name=customer,
                                    city=target_city,
                                    slot_date=target_date,
                                    status="FAILED",
                                    notes=reason_text
                                )
                                _record_slot_attempt(customer, "failed", reason_text, booked=False)
                                is_slot_gone = (
                                    ofc_context.get("status") == "slot_gone"
                                    or "slot_gone" in str(ofc_context.get("error", "")).lower()
                                    or "day not found" in str(ofc_context.get("error", "")).lower()
                                    or "no entries" in str(ofc_context.get("error", "")).lower()
                                )
                                if is_slot_gone:
                                    log.warning(f"[SLOT GONE] The slot is no longer available on portal! Aborting remaining queued attempts.")
                                    from src.booking_queue import clear_queue
                                    clear_queue(reason="slot_gone")
                                    _close_slot_window(force=True)
                                else:
                                    _close_slot_window(force=False)
                                success = False
                                context = ofc_context
                        elif action_type == "SNIPER":
                            log.info(f" Action type: {action_type} (SEQUENTIAL: OFC FIRST, THEN CONSULAR)")
                            ofc_target_city = trigger.get("ofcPriorityCity") or (trigger.get("ofcCities") or ["N/A"])[0]
                            record_booking_event(
                                stage="OFC",
                                username=username,
                                customer_name=customer,
                                city=ofc_target_city,
                                slot_date=trigger.get("ofcStartDate", "N/A"),
                                status="ATTEMPTED",
                                notes="OFC booking sniper triggered"
                            )
                            # Step 1: Book OFC only at maximum speed
                            log.info(" STEP 1: Booking OFC at maximum speed...")
                            from src.booking.executor import trigger_booking_ofc_only
                            ofc_success, ofc_context = await trigger_booking_ofc_only(page, trigger, log)

                            if ofc_success:
                                bookedOfcDate = ofc_context.get("bookedOfcDate")
                                log.info(f" OFC booked successfully! Date: {bookedOfcDate}")
                                record_booking_event(
                                    stage="OFC",
                                    username=username,
                                    customer_name=customer,
                                    city=ofc_context.get("bookedCity") or ofc_target_city,
                                    slot_date=bookedOfcDate,
                                    status="BOOKED",
                                    notes="OFC appointment secured successfully"
                                )

                                # Step 2: Search and book consular - try ALL cities
                                log.info(" STEP 2: Immediately searching for consular slot in ALL cities...")
                                await asyncio.sleep(0.1)  # Minimal delay to let page stabilize

                                bookedOfcDate = ofc_context.get("bookedOfcDate", "")
                                consularCities = trigger.get("consularCities", [])
                                if not consularCities:
                                    log.warning("No consular cities specified, skipping consular booking.")
                                    consular_success = False
                                else:
                                    log.info(f" Will try consular booking in {len(consularCities)} cities: {consularCities}")

                                    # Try each consular city until one succeeds
                                    consular_success = False
                                    consular_context = {}
                                    for idx, consular_city in enumerate(consularCities, 1):
                                        log.info(f" Attempting consular booking in city {idx}/{len(consularCities)}: {consular_city}")

                                        # Create a modified trigger for this specific city
                                        city_trigger = trigger.copy()
                                        city_trigger["consularCities"] = [consular_city]
                                        city_trigger["consularPriorityCity"] = consular_city

                                        city_success, city_context = await trigger_booking_sniper_consular_only(
                                            page, city_trigger, bookedOfcDate, log
                                        )

                                        if city_success:
                                            log.info(f" CONSULAR booked successfully in {consular_city}! COMPLETE BOOKING!")
                                            consular_success = True
                                            consular_context = city_context
                                            c_date = city_context.get("bookedConsularDate") or city_context.get("bookedDate") or bookedOfcDate
                                            record_booking_event(
                                                stage="CONSULAR",
                                                username=username,
                                                customer_name=customer,
                                                city=consular_city,
                                                slot_date=c_date,
                                                status="BOOKED",
                                                notes=f"Consular appointment secured in {consular_city}"
                                            )
                                            break  # Stop trying other cities
                                        else:
                                            log.warning(f" Consular booking failed in {consular_city}. Trying next city...")
                                            record_booking_event(
                                                stage="CONSULAR",
                                                username=username,
                                                customer_name=customer,
                                                city=consular_city,
                                                slot_date=state.get("consularStartDate", "N/A"),
                                                status="FAILED",
                                                notes=city_context.get("error") or f"Consular slot booking failed in {consular_city}"
                                            )
                                            await asyncio.sleep(0.05)  # Minimal delay between cities

                                    if not consular_success:
                                        log.warning(f" Consular booking failed in ALL {len(consularCities)} cities. Setting wait mode.")

                                if consular_success:
                                    log.info(" CONSULAR booked successfully! COMPLETE BOOKING!")
                                    success = True
                                    context = consular_context
                                else:
                                    log.warning(" Consular booking failed, but OFC is booked. Setting wait mode.")
                                    success = False
                                    context = {"waitingForConsular": True, "bookedOfcDate": bookedOfcDate, "ofcBookedAt": time.time()}

                                    # Send OFC BOOKING SUCCESS telegram notification
                                    try:
                                        ofc_city = ofc_context.get("bookedCity") or trigger.get("ofcPriorityCity") or "N/A"
                                        send_ofc_success_triggering_consular(
                                            customer=customer,
                                            user_id=state.get("customer_name"),
                                            ofc_city=ofc_city,
                                            ofc_date=bookedOfcDate
                                        )
                                        log.info(f" OFC BOOKING SUCCESS telegram sent for {customer}")
                                    except Exception as telegram_error:
                                        log.warning(f"Failed to send OFC success telegram: {telegram_error}")
                            else:
                                # Check if OFC was actually booked despite the failure status
                                if ofc_context.get("waitingForConsular") or ofc_context.get("ofcBooked"):
                                    bookedOfcDate = ofc_context.get("bookedOfcDate")
                                    log.info(f" OFC booked successfully! Date: {bookedOfcDate}")
                                    log.warning(" Consular dates/slots were empty. Setting wait mode.")
                                    record_booking_event(
                                        stage="OFC",
                                        username=username,
                                        customer_name=customer,
                                        city=ofc_context.get("bookedCity") or ofc_target_city,
                                        slot_date=bookedOfcDate,
                                        status="BOOKED",
                                        notes="OFC booked successfully (consular slots empty)"
                                    )
                                    success = False
                                    context = {"waitingForConsular": True, "bookedOfcDate": bookedOfcDate, "ofcBookedAt": time.time()}

                                    # Send OFC BOOKING SUCCESS telegram notification
                                    try:
                                        ofc_city = ofc_context.get("bookedCity") or trigger.get("ofcPriorityCity") or "N/A"
                                        send_ofc_success_triggering_consular(
                                            customer=customer,
                                            user_id=state.get("customer_name"),
                                            ofc_city=ofc_city,
                                            ofc_date=bookedOfcDate
                                        )
                                        log.info(f" OFC BOOKING SUCCESS telegram sent for {customer}")
                                    except Exception as telegram_error:
                                        log.warning(f"Failed to send OFC success telegram: {telegram_error}")
                                else:
                                    log.error(" OFC booking failed. Cannot proceed to consular.")
                                    record_booking_event(
                                        stage="OFC",
                                        username=username,
                                        customer_name=customer,
                                        city=ofc_target_city,
                                        slot_date=trigger.get("ofcStartDate", "N/A"),
                                        status="FAILED",
                                        notes=ofc_context.get("error") or "OFC booking failed (no slots/throttled)"
                                    )
                                    success = False
                                    context = ofc_context

                        elif action_type == "RESCHEDULE_FULL":
                            log.info(f" Action type: {action_type}")
                            success, context = await trigger_booking(page, trigger, log)
                            # Handle partial success: OFC booked but consular slots empty
                            if not success and context.get("waitingForConsular"):
                                bookedOfcDate = context.get("bookedOfcDate")
                                log.info(f" OFC booked successfully in RESCHEDULE_FULL! Date: {bookedOfcDate}")
                                log.warning(" Consular dates/slots were empty. Setting wait mode.")
                                context = {"waitingForConsular": True, "bookedOfcDate": bookedOfcDate, "ofcBookedAt": time.time()}

                                # Send OFC BOOKING SUCCESS telegram notification
                                try:
                                    ofc_city = context.get("bookedCity") or trigger.get("ofcPriorityCity") or "N/A"
                                    send_ofc_success_triggering_consular(
                                        customer=customer,
                                        user_id=state.get("customer_name"),
                                        ofc_city=ofc_city,
                                        ofc_date=bookedOfcDate
                                    )
                                    log.info(f" OFC BOOKING SUCCESS telegram sent for {customer}")
                                except Exception as telegram_error:
                                    log.warning(f"Failed to send OFC success telegram: {telegram_error}")
                        elif action_type in ("SNIPER_CONSULAR_ONLY", "RESCHEDULE_FULL_CONSULAR_ONLY"):
                            log.info(f" Action type: {action_type} (Consular Sniper)")
                            bookedOfcDate = state.get("bookedOfcDate", "")
                            # CRITICAL CHECK: Consular booking is strictly for the account whose OFC succeeded!
                            if not state.get("waitingForConsular") and not bookedOfcDate:
                                log.error(
                                    f"[CONSULAR TRIGGER ABORTED] Account '{customer}' received a Consular trigger, "
                                    f"but has NO active OFC booking! Consular booking is strictly reserved "
                                    f"for the specific account that secured OFC. Aborting trigger."
                                )
                                success = False
                                context = {"error": "no_active_ofc_hold"}
                            else:
                                success, context = await trigger_booking_sniper_consular_only(page, trigger, bookedOfcDate, log)
                            c_city = context.get("bookedConsularCity") or context.get("bookedCity") or trigger.get("consularPriorityCity", "N/A")
                            c_date = context.get("bookedConsularDate") or context.get("bookedDate") or "N/A"
                            if success:
                                record_booking_event(
                                    stage="CONSULAR",
                                    username=username,
                                    customer_name=customer,
                                    city=c_city,
                                    slot_date=c_date,
                                    status="BOOKED",
                                    notes="Consular booked via standby sniper"
                                )
                            else:
                                record_booking_event(
                                    stage="CONSULAR",
                                    username=username,
                                    customer_name=customer,
                                    city=c_city,
                                    slot_date=trigger.get("consularStartDate", "N/A"),
                                    status="FAILED",
                                    notes=context.get("error") or "Consular booking attempt failed in wait mode"
                                )
                        elif action_type == "RESCHEDULE_CONSULAR":
                            log.info(" Action type: RESCHEDULE_CONSULAR")
                            success = await trigger_booking_reschedule(page, trigger, log)
                        else:
                            log.error(f" Unknown or missing action_type: {action_type!r}  ->  skipping.")
                            success = False
                    except Exception as e:
                        log.error(f"Action error: {e}", exc_info=True)
                        success = False

                        # CRITICAL ARCHITECTURAL SAFETY:
                        # Immediately release fleet booking lock and advance coordinator queue (<5ms)!
                        # This prevents lock starvation so other candidate accounts waiting in the queue
                        # (e.g. Priority #2) can grab the slot right away!
                        _close_slot_window()
                        release_fleet_booking_lock(uid)
                        _advance_queue(uid)

                        target_c = locals().get("target_city", "N/A")
                        target_d = locals().get("target_date", "N/A")
                        record_booking_event(
                            stage="OFC",
                            username=username,
                            customer_name=customer,
                            city=target_c,
                            slot_date=target_d,
                            status="FAILED",
                            notes=str(e)[:150]
                        )

                        # 429 and Session expired errors are handled with retries/recovery
                        if "429" in str(e):
                            _now429 = time.time()
                            if _now429 - _BOOKING_429["ts"] > 1800:  # 30-min reset
                                _BOOKING_429["count"] = 0
                            _BOOKING_429["count"] += 1
                            _BOOKING_429["ts"] = _now429
                            log.warning(f"[STATS] Consecutive booking 429s: {_BOOKING_429['count']}/3")

                            if _BOOKING_429["count"] >= _MAX_CONSECUTIVE_429:
                                log.error(
                                    "[ALERT] Persistent Cloudflare 429s  ->  refreshing the page "
                                    "(browser stays open)."
                                )
                                await _rotate_ip_and_refresh(page, customer, "persistent Cloudflare 429s during booking", username=username)
                                _BOOKING_429["count"] = 0

                            log.warning(
                                "429 Too Many Requests - executor will retry with backoff. "
                                "Continuing to next polling cycle."
                            )
                            if state.get("waitingForConsular"):
                                log.warning(
                                    "WAITING MODE is over (429 hit). "
                                    "Resetting flags."
                                )
                                _update_state(
                                    state_file,
                                    {
                                        "waitingForConsular": False,
                                        "bookedOfcDate": None,
                                        "waitStartTime": None,
                                        "ofcBookedAt": None,
                                    },
                                )
                            continue

                        if "Session expired" in str(e):
                            ts_exp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            fail_box = (
                                f"\n"
                                f"================================================================================\n"
                                f"🚨 [OFC BOOKING FAILED - SESSION EXPIRED] Server Rejected Token! | {ts_exp}\n"
                                f"--------------------------------------------------------------------------------\n"
                                f"Account: {customer} ({username})\n"
                                f"Error  : Portal session expired or auth token invalidated by server.\n"
                                f"Action : Queue & fleet lock yielded to next account immediately (<5ms).\n"
                                f"         Starting in-place session recovery for '{customer}' in background...\n"
                                f"================================================================================\n"
                            )
                            print(fail_box, flush=True)
                            log.error(f"🚨 [SESSION EXPIRED] OFC Booking for '{customer}' rejected: Session expired. Queue released.")

                            is_waiting = state.get(
                                "waitingForConsular",
                                False,
                            )

                            if is_waiting:
                                log.error(
                                    "[ALERT] Session expired during Consular "
                                    "WAIT MODE. Clearing the current wait "
                                    "state before recovery."
                                )
                                _update_state(
                                    state_file,
                                    {
                                        "waitingForConsular": False,
                                        "bookedOfcDate": None,
                                        "waitStartTime": None,
                                        "ofcBookedAt": None,
                                    },
                                )
                                context["waitingForConsular"] = False
                                context["bookedOfcDate"] = None
                            else:
                                log.error(
                                    "Session expired during booking action. "
                                    "Starting recovery."
                                )

                            recovered = await recover_session(
                                page,
                                customer,
                                username,
                            )

                            _enter_booking_rest(
                                state_file,
                                customer,
                                "Session expired during booking attempt",
                                duration_seconds=120,
                            )

                            if not recovered:
                                log.error(
                                    "Recovery failed after booking action. "
                                    "Exiting to trigger orchestrator restart."
                                )
                                sys.exit(1)

                            log.info(
                                f"✅ Session recovered for '{customer}'. Background monitoring will resume."
                            )
                            continue

                    if success:
                        ofc_city = state.get("ofcPriorityCity") or context.get("bookedOfcCity") or context.get("bookedCity") or state.get("bookedCity") or "N/A"
                        ofc_date = state.get("bookedOfcDate") or context.get("bookedOfcDate") or "N/A"
                        consular_city = context.get("bookedConsularCity") or context.get("bookedCity") or trigger.get("consularPriorityCity", "N/A")
                        consular_date = context.get("bookedConsularDate") or context.get("bookedDate") or "N/A"

                        # ── Step 1: Download Appointment Confirmation PDF via CDP ────────
                        pdf_path_saved = ""
                        try:
                            from src.booking.confirmation import capture_appointment_confirmation
                            ok_pdf, pdf_path_saved, pdf_fname = await capture_appointment_confirmation(
                                page=page,
                                username=username,
                                customer=customer,
                                ofc_city=ofc_city,
                                ofc_date=ofc_date,
                                consular_city=consular_city,
                                consular_date=consular_date,
                                logger=log,
                            )
                            if ok_pdf:
                                log.info(f"✅ Official Appointment Confirmation PDF saved: {pdf_path_saved}")
                        except Exception as pdf_err:
                            log.warning(f"Failed to capture confirmation PDF: {pdf_err}")

                        complete_box = (
                            f"\n{ANSI_DARK_GREEN}"
                            f"================================================================================\n"
                            f"🎉 [BOOKING SUCCESS] OFC + CONSULAR FULLY BOOKED! | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"--------------------------------------------------------------------------------\n"
                            f"Account: {customer} ({username})\n"
                            f"OFC Appointment     : {ofc_city} on {ofc_date}\n"
                            f"Consular Appointment: {consular_city} on {consular_date}\n"
                            f"Confirmation PDF    : {pdf_path_saved or 'Saved in /confirmations'}\n"
                            f"Status: BOOKED SUCCESS (Disabled in accounts.json, Evicted from queue)\n"
                            f"Action: Browser closing permanently. Account permanently retired from monitoring.\n"
                            f"Web GUI: Check 'Booked Confirmations' tab to view/print PDF receipt.\n"
                            f"================================================================================{ANSI_RESET}\n"
                        )
                        print(complete_box, flush=True)
                        log.info(complete_box)

                        _close_slot_window()
                        release_fleet_booking_lock(uid)  # release for next account
                        _advance_queue(uid)  # allow next account to book
                        _update_state(
                            state_file,
                            {
                                "waitingForConsular": False,
                                "bookedOfcDate": ofc_date,
                                "bookedConsularDate": consular_date,
                                "waitStartTime": None,
                                "ofcBookedAt": None,
                                "completed": True,
                                "booking_status": "BOOKED SUCCESS",
                                "enabled": False,
                                "rest_until": 0,
                                "pending": False,
                                "booking_running": False,
                            },
                        )

                        # Permanently disable and mark BOOKED SUCCESS on disk & database
                        _persist_account_booked_success(
                            username=username,
                            customer=customer,
                            ofc_date=ofc_date,
                            consular_date=consular_date,
                            ofc_city=ofc_city,
                            consular_city=consular_city
                        )

                        # Send booking success notification with both OFC and consular details
                        try:
                            record_booking_event(
                                stage="OFC",
                                username=username,
                                customer_name=customer,
                                city=ofc_city,
                                slot_date=ofc_date,
                                status="BOOKED",
                                notes="OFC appointment confirmed"
                            )
                            record_booking_event(
                                stage="CONSULAR",
                                username=username,
                                customer_name=customer,
                                city=consular_city,
                                slot_date=consular_date,
                                status="BOOKED",
                                notes="Consular appointment confirmed (Complete Booking)"
                            )
                            success_message = (
                                f"🎉 <b>BOTH OFC + CONSULAR BOOKED SUCCESSFULLY</b>\n\n"
                                f"<b>Customer:</b> {html.escape(str(customer))}\n"
                                f"<b>User ID:</b> {html.escape(str(state.get('customer_name', customer)))}\n\n"
                                f"<b>OFC City:</b> {html.escape(str(ofc_city))}\n"
                                f"<b>OFC Date:</b> {html.escape(str(ofc_date))}\n\n"
                                f"<b>Consular City:</b> {html.escape(str(consular_city))}\n"
                                f"<b>Consular Date:</b> {html.escape(str(consular_date))}\n\n"
                                f"📄 <b>Confirmation PDF:</b> Saved & Available in Web Hub ('Booked Confirmations')\n\n"
                                f"✅ Complete booking successful! Browser permanently closed."
                            )
                            tg_send(success_message)
                            log.info(f"✅ BOOKING SUCCESS telegram sent for {customer} with OFC and consular details")
                        except Exception as e:
                            log.warning(f"Failed to send booking success telegram: {e}")

                        # Close browser permanently and exit after complete booking success
                        log.info("🔒 Booking complete! Closing browser permanently and exiting booking_runner...")

                        try:
                            await page.close()
                            log.info(" Browser closed successfully.")
                        except Exception as e:
                            log.warning(f"Browser close warning: {e}")
                        try:
                            if hasattr(page, "context") and page.context:
                                await page.context.close()
                        except Exception:
                            pass

                        log.info(f"[EXIT] Booking_runner exiting after successful booking for '{customer}'.")
                        sys.exit(0)
                    else:
                        if context.get("waitingForConsular"):
                            log.warning("=" * 60)
                            log.warning(f"[WAIT] PARTIAL BOOKING / STILL WAITING for '{customer}'! Transitioning to WAIT MODE...")
                            log.warning("=" * 60)
                            _close_slot_window()
                            release_fleet_booking_lock(uid)  # release for next account
                            _advance_queue(uid)  # allow next account to proceed
                            b_ofc_date = context.get("bookedOfcDate") or state.get("bookedOfcDate")
                            _update_state(state_file, {
                                "waitingForConsular": True,
                                "bookedOfcDate": b_ofc_date,
                                "ofcBookedAt": context.get("ofcBookedAt") or state.get("ofcBookedAt") or time.time(),
                                "waitStartTime": state.get("waitStartTime") or time.time(),  # Preserve start time if already waiting
                                "booking_status": "OFC_BOOKED_PENDING_CONSULAR",
                                "booking_running": False,
                                "pending": False
                            })
                            _persist_account_ofc_hold(
                                username=username,
                                customer=customer,
                                ofc_date=b_ofc_date or "",
                                ofc_city=state.get("ofcPriorityCity") or "N/A"
                            )
                            # Send partial booking notification
                            try:
                                city = context.get("bookedCity") or context.get("bookedConsularCity") or state.get("ofcPriorityCity") or "N/A"
                                date = context.get("bookedConsularDate") or context.get("bookedOfcDate") or "N/A"
                                send_partial_booking(customer=customer, appointment_type=action_type, city=city, date=date, user_id=state.get("customer_name"), reason="OFC booked; waiting for consular booking")
                            except Exception:
                                pass
                            last_keep_alive = time.time()
                            await asyncio.sleep(0.5)
                            continue
                        else:
                            release_fleet_booking_lock(uid)  # release for next account immediately (<10ms)
                            _advance_queue(uid)  # allow next account to proceed immediately
                            _close_slot_window()
                            log.error(f" Action failed for '{customer}'. [{action_type}]")
                            if state.get("waitingForConsular"):
                                # OFC is ALREADY BOOKED. A failed consular attempt
                                # (slots gone / throttled) must NOT reset WAIT
                                # MODE  ->  persistent consular monitoring continues
                                # and retries on the next periodic poll.
                                log.warning(
                                    "[WAIT] Consular attempt failed but OFC is booked  ->  "
                                    "STAYING in WAIT MODE. Will retry consular on "
                                    "the next periodic poll."
                                )
                                _update_state(state_file, {
                                    "waitingForConsular": True,
                                    "booking_running": False,
                                    "pending": False
                                })

                            _record_slot_attempt(customer, "failed", f"{action_type or 'UNKNOWN'} booking attempt failed", booked=False)

                            if context.get("needs_background_heal") or context.get("fast_fail"):
                                # FAST-FAIL WATERFALL: Launch non-blocking background heal!
                                # DO NOT block the account for 120s so it can loop-back if slot remains active!
                                _update_state(state_file, {
                                    "pending": False,
                                    "booking_running": False,
                                    "rest_until": time.time() + 8.0,  # 8s temporary guard while healing runs
                                })
                                asyncio.create_task(_background_heal_failed_candidate(
                                    page, customer, username, uid, trigger, context, state_file
                                ))
                            else:
                                _enter_booking_rest(
                                    state_file,
                                    customer,
                                    f"{action_type or 'UNKNOWN'} booking attempt failed",
                                    duration_seconds=120,
                                )

                    # Mark browser as done
                    _update_state(state_file, {"booking_running": False})
                    last_keep_alive = time.time()

                    await asyncio.sleep(0.5)
                    continue

                # --- If NO trigger, do maintenance 
                await asyncio.sleep(POLL_INTERVAL)

                # --- Delayed Polling for Consular 
                state = _read_state(state_file)
                if state.get("waitingForConsular") and not state.get("pending"):
                    ofc_booked_at = state.get("ofcBookedAt") or state.get("waitStartTime") or time.time()

                    elapsed_since_ofc = time.time() - float(ofc_booked_at)

                    # --- 32-MINUTE CONSULAR WAIT TIMEOUT ---
                    # The consular window is bounded by OFC_CONSULAR_WINDOW_SECONDS
                    # (32 min). If no consular slot was found within that window,
                    # the timer expires: release the lock, exit wait mode, and
                    # resume OFC slot monitoring for this account AND all other
                    # fleet accounts (they were held back via the same
                    # waitingForConsular flag in _fleet_booking_in_progress).
                    if elapsed_since_ofc >= OFC_CONSULAR_WINDOW_SECONDS:
                        log.warning(
                            f"[TIMEOUT] Consular wait window expired for '{customer}' "
                            f"({elapsed_since_ofc:.0f}s >= {OFC_CONSULAR_WINDOW_SECONDS}s). "
                            f"Releasing hold and resuming OFC slot monitoring."
                        )
                        try:
                            _notify(
                                f"⚠️ <b>Consular wait window expired</b>\n"
                                f"<b>Customer:</b> {html.escape(str(customer))}\n"
                                f"<b>OFC booked:</b> {html.escape(str(state.get('bookedOfcDate', 'N/A')))}\n"
                                f"No consular slot found within 32 min. Hold released on portal. "
                                f"Resuming normal OFC slot monitoring."
                            )
                        except Exception:
                            pass
                        _update_state(state_file, {
                            "waitingForConsular": False,
                            "bookedOfcDate": None,
                            "waitStartTime": None,
                            "ofcBookedAt": None,
                            "booking_status": "IDLE",
                            "pending": False,
                            "booking_running": False,
                        })
                        _persist_account_consular_timeout(username, customer)
                        last_keep_alive = time.time()
                        last_polling_time = 0
                        continue

                    # --- COVENTRY / FLEET COORDINATED CONSULAR MONITORING ---
                    # OFC is booked. Consular monitoring runs within the 32-min hold window.
                    # Coordinated via atomic round-robin turn-taking so that:
                    # - 1 account: polls safely every ~45-52s (~1.2 req/min, 100% immune to rate-limit / IP ban).
                    # - 2 accounts: fleet gap ~24-28s, individual account cooldown ~48s (round-robin alternating).
                    # - 3+ accounts: fleet gap ~18-22s, individual account cooldown ~50s.
                    # - If ANY account detects consular slots, ALL OFC-booked accounts are armed in
                    #   a staggered round-robin queue (+2s offset) to book immediately without colliding!
                    _fleet_busy_now = _fleet_booking_in_progress(customer)
                    if _fleet_busy_now and time.time() - _FLEET_PAUSE_LOG_TS["t"] > 30:
                        _FLEET_PAUSE_LOG_TS["t"] = time.time()
                        log.info(f"[LOCK]  Fleet pause: '{_fleet_busy_now}' is booking  ->  deferring own consular poll.")

                    is_first_consular_poll = not bool(state.get("firstConsularPollDone"))
                    can_poll, wait_sec, mode_desc, num_waiters, next_turn = _can_take_consular_poll_turn(
                        customer, username, is_first_consular_poll
                    )

                    if not _fleet_busy_now and not can_poll:
                        # Yield turn and sleep briefly before checking again
                        sleep_time = min(3.0, max(1.0, wait_sec))
                        await asyncio.sleep(sleep_time)
                        continue

                    if not _fleet_busy_now and can_poll:
                        booked_ofc_date = state.get("bookedOfcDate", "N/A")
                        rem_sec = max(0, int(OFC_CONSULAR_WINDOW_SECONDS - elapsed_since_ofc))
                        rem_min = rem_sec // 60
                        rem_s = rem_sec % 60
                        log.info(
                            f"[CONSULAR MONITOR] Polling turn for '{customer}' [{mode_desc}] "
                            f"({elapsed_since_ofc:.0f}s elapsed, {rem_min}m {rem_s}s remaining in 32m window). OFC booked: {booked_ofc_date}."
                        )

                        try:
                            cstart = state.get("consularStartDate", "")
                            cend = state.get("consularEndDate", "")
                            target_consular_cities = [
                                str(c).strip().upper() for c in (state.get("consularCities") or state.get("target_cities") or ALL_CITIES)
                            ]
                            cresult = await _fetch_consular_dates(page, state, log)
                            city_dates = cresult.get("result") if cresult.get("success") else None

                            if is_first_consular_poll:
                                _update_state(state_file, {"firstConsularPollDone": True})

                            has_matching_dates = False
                            matching_summary = []
                            if city_dates:
                                for city, dates in city_dates.items():
                                    if isinstance(dates, list) and dates:
                                        has_matching_dates = True
                                        matching_summary.append(f"{city}: {len(dates)} date(s)")

                            if has_matching_dates:
                                log.info(
                                    f"🎯 [CONSULAR SLOTS DETECTED!] Matching dates found for '{customer}' "
                                    f"({', '.join(matching_summary)})! Arming SNIPER_CONSULAR_ONLY now!"
                                )
                                _update_state(state_file, {
                                    "pending": True,
                                    "action_type": "SNIPER_CONSULAR_ONLY",
                                    "trigger_timestamp": time.time()
                                })
                                # Broadcast to ALL sibling OFC-booked accounts in round-robin queue (+2s offset)!
                                _broadcast_consular_trigger_to_fleet(customer, username, city_dates, log)
                                last_polling_time = time.time()
                                continue
                            elif cresult.get("success") is False:
                                log.warning(f"Consular date probe error: {cresult.get('error')}")
                                last_polling_time = time.time()
                                await asyncio.sleep(2.0)
                                continue
                            else:
                                city_details = []
                                c_log_lines = [
                                    f"[{log_stamp()}] Consular Poll by '{customer}':",
                                    f"  Type: CONSULAR Slot",
                                    f"  Mode: {mode_desc}"
                                ]
                                display_cities = list(city_dates.keys()) if (city_dates and len(city_dates) > 0) else target_consular_cities
                                for c in display_cities:
                                    d = city_dates.get(c, []) if city_dates else []
                                    cnt = len(d) if isinstance(d, list) else 0
                                    status_str = f"{cnt} slot(s) available" if cnt > 0 else "0 slots available"
                                    city_details.append(f"   - {c:<12} : {status_str}")
                                    c_log_lines.append(f"  {c}: {cnt} date(s): {d}")

                                next_info = f"Round-Robin turn -> {next_turn}" if num_waiters > 1 else f"~45-50s"
                                c_block = (
                                    f"\n"
                                    f"================================================================================\n"
                                    f"  [SLOT MONITOR] Consular Poll | Account: {customer} ({username}) | {log_stamp()}\n"
                                    f"  ------------------------------------------------------------------------------\n"
                                    f"  Mode          : {mode_desc} [{rem_min}m {rem_s}s left | OFC: {booked_ofc_date}]\n"
                                    f"  Appd          : {state.get('appd', 'N/A')}\n"
                                    f"  Target Cities : {', '.join(target_consular_cities)}\n"
                                    f"  Date Range    : {cstart} to {cend}\n"
                                    f"  Type          : CONSULAR Slot\n"
                                    f"\n"
                                    f"  [AVAILABILITY RESPONSE]\n"
                                    + "\n".join(city_details) + "\n"
                                    f"\n"
                                    f"  Turn Info     : {next_info} (Holding OFC appointment)\n"
                                    f"================================================================================\n"
                                )
                                print(c_block, flush=True)

                                try:
                                    Path("logs").mkdir(exist_ok=True)
                                    with open("logs/polling.log", "a", encoding="utf-8") as pf:
                                        pf.write("\n".join(c_log_lines) + "\n")
                                except Exception:
                                    pass

                                log.info(
                                    f"⏳ [CONSULAR MONITOR] 0 consular slots available in target cities [{cstart} .. {cend}]. "
                                    f"Holding OFC appointment ({rem_min}m {rem_s}s left in window). Next turn: {next_turn}."
                                )
                                last_polling_time = time.time()
                                await asyncio.sleep(1.0)
                                continue
                        except Exception as cexc:
                            log.warning(f"Consular date fetch error: {cexc}")
                            last_polling_time = time.time()
                            await asyncio.sleep(2.0)
                            continue

                # --- Background Slot Monitoring (ALL accounts, every cycle) -
                # Any logged-in account can monitor and self-book / broadcast  -> 
                # the client's goal is booking OFC+Consular as soon as slots >=5.
                if not state.get("waitingForConsular") and not state.get("pending"):
                    # FLEET PRIORITY: when another account is actively booking,
                    # pause THIS account's monitoring so its requests don't
                    # compete for the same IP's Cloudflare rate-limit budget.
                    # The booking account gets a clear runway; keep-alive below
                    # still runs (local-only, keeps the session warm).
                    fleet_booking_by = _fleet_booking_in_progress(customer)
                    if fleet_booking_by:
                        if time.time() - _FLEET_PAUSE_LOG_TS["t"] > 30:
                            _FLEET_PAUSE_LOG_TS["t"] = time.time()
                            log.info(
                                f"[LOCK]  Fleet pause: '{fleet_booking_by}' is booking  ->  "
                                f"holding own monitoring to keep the IP quiet."
                            )
                        await asyncio.sleep(POLL_INTERVAL)
                    else:
                        try:
                            last_background_poll, last_poll_debug, self_triggered = await _try_background_poll(
                                page, customer, username, last_background_poll, last_poll_debug
                            )
                            if self_triggered:
                                continue
                        except SessionExpiredError:
                            # Background poll detected a dead session (OFC API
                            # returned login HTML). Recover now instead of waiting
                            # for the keep-alive health check to notice minutes later.
                            print("")  # visual break
                            log.warning("[ALERT] Session expired during background polling! Triggering recovery...")
                            success = await _recover_with_retry(page, customer, username)
                            if not success:
                                log.error("Recovery failed repeatedly after background-poll expiry. Rotating IP in-place")
                                await _rotate_ip_and_refresh(page, customer, "repeated recovery failure after background poll expiry (likely IP ban / rate limit)", username=username)
                            last_keep_alive = time.time()
                            continue

                now = time.time()
                # --- VIP Sniper Round-Robin Session Radar & 3-Tier Maintenance (Task 1 & 2) ---
                # Strictly for RESERVED_BOOKING accounts to keep tokens 100% fresh while parking.
                # Enforces Strict Time Gap (150s / 2.5 min) between accounts so no two accounts ever collide on Local IP.
                current_role = str((state.get("role") or acct_role or "")).strip().upper()
                is_vip_sniper = (current_role == "RESERVED_BOOKING")
                account_uid = safe_id(username)

                # Ensure account is registered in session radar
                session_radar.register_account(account_uid, customer, current_role)

                # Quiet In-Place Cloudflare CDN & Edge Token Renewal (~65m mark)
                try:
                    if is_cdn_refresh_due(username):
                        await quiet_cdn_refresh(page, username, log)
                except Exception:
                    pass

                if is_vip_sniper:
                    try:
                        can_act, reason, wait_sec = session_radar.can_perform_turn(account_uid, min_gap=150.0)
                        if can_act:
                            if session_radar.claim_turn(account_uid):
                                round_robin_mins = session_radar.calculate_round_robin_interval(current_role)
                                try:
                                    # Cloudflare clearance tokens expire in ~30-45 minutes. If >30m has elapsed
                                    # since last page reload, force Tier-2 Harmless Page Reload directly so
                                    # standby accounts NEVER sit cold with stale Cloudflare clearance!
                                    force_tier2 = (time.time() - _ACCOUNT_LAST_PAGE_RELOAD.get(account_uid, 0.0) > 1800.0)
                                    touch_res = None

                                    if not force_tier2:
                                        # Tier 1: Lightweight Active Touch (in-page fetch to /en-US/)
                                        touch_res = await page.evaluate(r"""
                                            async () => {
                                                try {
                                                    const resp = await fetch("/en-US/", {
                                                        method: "GET",
                                                        headers: { "Cache-Control": "no-cache", "X-Requested-With": "XMLHttpRequest" }
                                                    });
                                                    if (resp.redirected && (resp.url.toLowerCase().includes("b2clogin") || resp.url.toLowerCase().includes("signin"))) {
                                                        return { ok: false, reason: "redirected_to_b2clogin" };
                                                    }
                                                    if (resp.status === 200) {
                                                        const text = await resp.text();
                                                        if (text.includes("atlasauth.b2clogin.com") || (text.includes("Sign In") && !text.includes("Sign Out") && !text.includes("sign out"))) {
                                                            return { ok: false, reason: "unauthenticated_page" };
                                                        }
                                                        return { ok: true, status: 200 };
                                                    }
                                                    return { ok: false, status: resp.status, reason: `status_${resp.status}` };
                                                } catch (e) {
                                                    return { ok: false, error: String(e) };
                                                }
                                            }
                                        """)

                                    if not force_tier2 and isinstance(touch_res, dict) and touch_res.get("ok"):
                                        session_radar.finish_turn(account_uid, status="LIVE", method="LIGHTWEIGHT TOUCH")
                                        log.info(
                                            f"{ANSI_DARK_BLUE}[SESSION RADAR] '{customer}': Verified LIVE via Lightweight Touch (HTTP 200) | "
                                            f"Token refreshed +20m | Age: 0.0m | Next turn in ~{round_robin_mins}m{ANSI_RESET}"
                                        )
                                    else:
                                        # Tier 2: Harmless Page Reload (/en-US/) to refresh Cloudflare cf_clearance & Dynamics session
                                        _ACCOUNT_LAST_PAGE_RELOAD[account_uid] = time.time()
                                        reason_desc = "proactive_30m_cf_clearance_refresh" if force_tier2 else (touch_res.get("reason") if isinstance(touch_res, dict) else "stale_or_failed")
                                        log.info(
                                            f"{ANSI_DARK_BLUE}[SESSION RADAR] '{customer}': Escalating to Tier-2 Harmless Page Reload (/en-US/) "
                                            f"[Reason: {reason_desc}] -> Renewing Cloudflare Turnstile & Edge Clearance...{ANSI_RESET}"
                                        )
                                        try:
                                            await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=30000)
                                            await page.wait_for_timeout(2000)
                                        except Exception as nav_ex:
                                            log.warning(f"[SESSION RADAR] '{customer}': Reload navigation notice: {nav_ex}")

                                        cur_url = (page.url or "").lower()
                                        if "/en-us" in cur_url and not any(k in cur_url for k in ["b2clogin", "signin", "account/login"]):
                                            session_radar.finish_turn(account_uid, status="LIVE", method="PAGE RELOAD")
                                            log.info(
                                                f"{ANSI_DARK_BLUE}[SESSION RADAR] '{customer}': Verified LIVE via Page Reload (HTTP 200) | "
                                                f"Cloudflare Clearance Renewed | Age: 0.0m | Next turn in ~{round_robin_mins}m{ANSI_RESET}"
                                            )
                                        else:
                                            # Tier 3: Auto Re-Login (Cloudflare Turnstile + Security Questions)
                                            log.info(
                                                f"{ANSI_DARK_BLUE}[SESSION RADAR] '{customer}': Session expired (redirected to {cur_url[:50]}) -> "
                                                f"Initiating Tier-3 Background Auto Re-Login...{ANSI_RESET}"
                                            )
                                            session_radar.finish_turn(account_uid, status="REFRESHING", method="AUTO RE-LOGIN")
                                            relogin_ok = await _recover_with_retry(page, customer, username)
                                            if relogin_ok:
                                                session_radar.finish_turn(account_uid, status="LIVE", method="AUTO RE-LOGIN")
                                                log.info(
                                                    f"{ANSI_DARK_BLUE}[SESSION RADAR] '{customer}': Verified LIVE via Auto Re-Login | "
                                                    f"Session Restored | Age: 0.0m | Next turn in ~{round_robin_mins}m{ANSI_RESET}"
                                                )
                                            else:
                                                session_radar.finish_turn(account_uid, status="EXPIRED", method="AUTO RE-LOGIN", error="relogin_failed")
                                                log.warning(
                                                    f"[SESSION RADAR] '{customer}': Auto Re-Login failed. Will retry in next round with safe backoff gap."
                                                )
                                except Exception as radar_err:
                                    session_radar.finish_turn(account_uid, status="EXPIRED", method="ERROR", error=str(radar_err))
                                    log.warning(f"[SESSION RADAR] '{customer}' maintenance turn error: {radar_err}")
                    except Exception:
                        pass
                else:
                    # Non-VIP account (e.g. Poller actively querying availability on its turn)
                    if now - last_session_keepalive > 120.0:
                        last_session_keepalive = now
                        cur_ka_url = (page.url or "").lower()
                        if any(k in cur_ka_url for k in ["b2clogin", "signin", "sign-in", "logon"]):
                            log.warning(f"[ALERT] Poller '{customer}' drifted to login page ({cur_ka_url[:60]}). Recovering session...")
                            await _recover_with_retry(page, customer, username)

                # --- Keep-alive & Content Health Check ---
                is_waiting = state.get("waitingForConsular", False)
                if now - last_keep_alive > 10.0:
                    try:
                        # 0. Check for browser's session expiry broadcast
                        expired_flag = await page.evaluate("window.__browserSessionExpired || false")
                        if expired_flag:
                            cur_chk_url = (page.url or "").lower()
                            is_login_page = any(k in cur_chk_url for k in ["b2clogin", "signin", "sign-in", "logon"])
                            
                            if is_login_page:
                                print("") # visual break
                                log.warning(f"[ALERT] Browser redirected to login page ({cur_chk_url[:60]})! Triggering recovery on current IP...")
                                success = await _recover_with_retry(page, customer, username)
                                if not success:
                                    log.error("Recovery failed repeatedly. Restarting runner...")
                                    sys.exit(1)
                                last_keep_alive = time.time()
                                continue
                            elif is_waiting:
                                log.warning("browser heartbeat detected session expiry in WAIT MODE, ignoring as per preference.")
                                await page.evaluate("window.__browserSessionExpired = false")
                            else:
                                # Browser is still on portal (e.g. /en-US/) - verify with lightweight URL/DOM check (NO fake API pings)
                                try:
                                    is_alive_on_portal = await page.evaluate(r"""
                                        () => {
                                            const url = window.location.href.toLowerCase();
                                            if (url.includes("b2clogin") || url.includes("/signin") || url.includes("/account/login")) {
                                                return false;
                                            }
                                            const bodyText = (document.body && document.body.innerText) ? document.body.innerText.toLowerCase() : "";
                                            if (bodyText.includes("sign in") && bodyText.includes("create an account") && !bodyText.includes("continue application")) {
                                                return false;
                                            }
                                            return true;
                                        }
                                    """)
                                    if is_alive_on_portal:
                                        await page.evaluate("window.__browserSessionExpired = false")
                                    else:
                                        log.warning(f"[ALERT] Session unauthenticated on '{customer}'! Triggering recovery on current IP...")
                                        success = await _recover_with_retry(page, customer, username)
                                        if not success:
                                            log.error("Recovery failed repeatedly. Restarting runner...")
                                            sys.exit(1)
                                        last_keep_alive = time.time()
                                        continue
                                except Exception:
                                    await page.evaluate("window.__browserSessionExpired = false")

                        # 0b. Homepage liveness watchdog  ->  "stable" does NOT mean
                        # "stuck on a dead page". Every keep-alive cycle, verify
                        # the tab is actually a live portal page. If the browser
                        # is sitting on a Chrome error page (chrome-error:// /
                        # chromewebdata), about:blank, or a crashed renderer,
                        # revive it by reloading the homepage immediately. If it
                        # stays dead across repeated cycles, exit so the
                        # orchestrator restarts the bot with a fresh browser.
                        try:
                            live_url = page.url.lower()
                            on_login_flow = "b2clogin" in live_url
                            # Cloudflare waiting room is NOT a dead page - it is a live CF page that
                            # will auto-redirect. Never try to navigate away from it.
                            in_cf_waitroom = "waitingroom" in live_url
                            in_cf_challenge = "challenge" in live_url or "cf-chl" in live_url
                            dead_url = (not in_cf_waitroom and not in_cf_challenge and (
                                "chrome-error" in live_url
                                or "chromewebdata" in live_url
                                or live_url.startswith("about:blank")
                                or live_url.startswith("data:")
                                or live_url.startswith("devtools:")
                            ))
                            portal_live = False
                            if not dead_url and not on_login_flow:
                                if in_cf_waitroom or in_cf_challenge:
                                    # CF waiting room or challenge is active -- do not interrupt with reload
                                    portal_live = True
                                elif "usvisascheduling.com" in live_url:
                                    try:
                                        portal_live = await page.evaluate(
                                            "(() => { try { return location.hostname.indexOf('usvisascheduling.com') !== -1; } catch (e) { return false; } })()")
                                    except Exception:
                                        # Evaluate itself failing = renderer wedged
                                        portal_live = False
                            if not on_login_flow and not in_cf_waitroom and (dead_url or not portal_live):
                                _HOME_LIVENESS["fails"] += 1
                                if _HOME_LIVENESS["fails"] >= 3:
                                    log.error("[REVIVE] Homepage STILL not live after repeated revive attempts "
                                              f"(url={page.url[:100]})  ->  restarting runner for a fresh browser")
                                    sys.exit(1)  # orchestrator restarts bot2 automatically
                                log.warning(f"[HEALTH] Homepage NOT live (url={page.url[:100]})  ->  "
                                            f"reviving via homepage reload "
                                            f"(attempt {_HOME_LIVENESS['fails']}/2) ")
                                try:
                                    await page.goto("https://www.usvisascheduling.com/en-US/",
                                                    wait_until="domcontentloaded", timeout=45000)
                                except Exception as revive_err:
                                    log.warning(f"Revive navigation failed (will retry next cycle): {revive_err}")
                                last_keep_alive = time.time()
                                continue
                            _HOME_LIVENESS["fails"] = 0
                        except Exception as live_err:
                            log.debug(f"Homepage liveness check skipped: {live_err}")

                        # 1. Move mouse to prevent idle expiry
                        await page.mouse.move(
                            random.randint(100, 800),
                            random.randint(100, 600),
                        )
                        # 1b. Ensure page remains safely parked on homepage without drift
                        try:
                            cur_idle_url = page.url.lower()
                            if ("usvisascheduling.com" in cur_idle_url
                                    and "waitingroom" not in cur_idle_url
                                    and "b2clogin" not in cur_idle_url):
                                from urllib.parse import urlparse
                                parsed_path = urlparse(cur_idle_url).path.lower().rstrip("/")
                                # Allow /en-us and /ofc-schedule / /schedule so sniper remains pre-warmed
                                is_valid_sniper_page = any(k in cur_idle_url for k in ["/ofc-schedule", "/schedule", "/en-us"])
                                if not is_valid_sniper_page and parsed_path != "":
                                    log.info(f"[PARK] Browser drifted to '{cur_idle_url[:60]}' -- parking back on portal.")
                                    await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=25000)
                        except Exception as park_err:
                            log.warning(f"Homepage re-park failed (non-fatal): {park_err}")

                        # 1c. Cloudflare verification & block check
                        try:
                            cf_title = (await page.title() or "").lower()
                            cf_html = (await page.content() or "").lower()

                            from src.auth.challenge import is_cloudflare_challenge, is_cloudflare_hard_block, solve_cloudflare_challenge

                            # 1. Terminal WAF Hard Block check FIRST:
                            # If IP is blocked (HTTP 403 "Sorry, you have been blocked"), Turnstile clicking or
                            # relogging on the SAME IP is completely useless. The proxy IP MUST be rotated immediately!
                            if await is_cloudflare_hard_block(page) or "sorry, you have been blocked" in cf_html:
                                print("", flush=True)
                                log.error(f"[HARD-BLOCK] Cloudflare 403 WAF Block ('Sorry, you have been blocked') detected on '{customer}'!")
                                log.info(f"[PROXY-ROTATE] Immediately rotating to fresh Webshare proxy IP for '{customer}'...")
                                await _rotate_ip_and_refresh(page, customer, "Cloudflare 403 WAF Block (Sorry, you have been blocked)", username=username)
                                last_keep_alive = time.time()
                                continue

                            # 2. Auto-solve interactive challenge ("verify you are human" / Turnstile)
                            if await is_cloudflare_challenge(page):
                                log.info(f"[CHALLENGE] Cloudflare verification active on page for '{customer}' - solving in place on sticky IP...")
                                solved = await solve_cloudflare_challenge(page, log)
                                if solved:
                                    log.info(f"[OK] Cloudflare challenge cleared for '{customer}' on sticky IP.")
                                    setattr(run, f"_chal_pending_{username}", 0)
                                else:
                                    pending = getattr(run, f"_chal_pending_{username}", 0) + 1
                                    setattr(run, f"_chal_pending_{username}", pending)
                                    log.warning(f"[CHALLENGE] Turnstile challenge pending for '{customer}' (cycle #{pending}) - retrying without rotating IP.")
                                    # Give Turnstile solver time to complete without aggressive F5 thrashing
                                    if pending == 3:
                                        log.info(f"[RELOAD] Turnstile still pending cycle #{pending} for '{customer}' -- gently refreshing page to reset widget...")
                                        try:
                                            await page.reload(wait_until="domcontentloaded", timeout=30000)
                                            await asyncio.sleep(2)
                                            if await is_cloudflare_challenge(page):
                                                solved_rel = await solve_cloudflare_challenge(page, log)
                                                if solved_rel:
                                                    log.info(f"[OK] Challenge successfully cleared after page reload for '{customer}'!")
                                                    setattr(run, f"_chal_pending_{username}", 0)
                                        except Exception as rel_err:
                                            log.warning(f"Homepage reload error during challenge: {rel_err}")
                                    # Automatically relogin if still unresolved after 5 cycles
                                    elif pending >= 5:
                                        log.warning(f"[RECOVERY] Turnstile challenge unresolved after {pending} cycles for '{customer}' -- triggering automatic relogin...")
                                        setattr(run, f"_chal_pending_{username}", 0)
                                        await _recover_with_retry(page, customer, username)

                                last_keep_alive = time.time()
                                continue
                            else:
                                setattr(run, f"_chal_pending_{username}", 0)

                            # 3. WAITING ROOM = NORMAL FLOW.
                            cf_waiting_room = ("waiting room" in cf_title
                                               or "just a moment" in cf_title
                                               or "estimated wait" in cf_html
                                               or "you are in a queue" in cf_html)
                            if cf_waiting_room:
                                if time.time() - _WR_LOG_TS["t"] > 60:
                                    _WR_LOG_TS["t"] = time.time()
                                    log.info("[WAIT] In Cloudflare waiting room -> normal flow, waiting for auto-redirect.")
                                last_keep_alive = time.time()
                                await asyncio.sleep(5)
                                continue

                            # Hard block page check
                            cf_blocked = "sorry, you have been blocked" in cf_html
                            if cf_blocked:
                                print("")  # visual break
                                log.warning("[ALERT] Cloudflare hard block page detected -> triggering relogin recovery...")
                                success = await _recover_with_retry(page, customer, username)
                                if not success:
                                    log.error("Recovery failed repeatedly. Recovering via page reload")
                                    await _rotate_ip_and_refresh(page, customer, "persistent Cloudflare block (likely rate limit)", username=username)
                                last_keep_alive = time.time()
                                continue
                        except Exception as cf_err:
                            log.warning(f"Cloudflare check error (non-fatal): {cf_err}")

                        # 2. Check for silent expiry where URL didn't change
                        body_text = (await page.inner_text("body")).lower()
                        matched_phrase = next((phrase for phrase in [
                            "session has expired", "please sign in", "sign in to continue", "unauthorized"
                        ] if phrase in body_text), None)

                        if matched_phrase:
                            if is_waiting:
                                log.warning(f"Silent session expiry detected ('{matched_phrase}') from page content in WAIT MODE, ignoring as per preference.")
                            else:
                                print("") # visual break
                                log.warning(f"[ALERT] Silent session expiry detected ('{matched_phrase}') from page content! Triggering recovery...")
                                success = await _recover_with_retry(page, customer, username)
                                if not success:
                                    log.error("Recovery failed repeatedly. Rotating IP in-place")
                                    await _rotate_ip_and_refresh(page, customer, f"repeated recovery failure after silent session expiry ('{matched_phrase}')", username=username)
                                last_keep_alive = time.time()
                                continue
                    except Exception as e:
                        err_msg = str(e).lower()
                        # "Execution context was destroyed" happens during navigation  ->  not fatal
                        if "execution context was destroyed" in err_msg or "target closed" in err_msg:
                            log.debug(f"Keep-alive interrupted by navigation (non-fatal)")
                            await asyncio.sleep(1)
                            last_keep_alive = time.time()  # don't immediately retry
                            continue
                        # Timeout errors during navigation  ->  also non-fatal
                        if "timeout" in err_msg:
                            log.debug(f"Keep-alive timed out (non-fatal)")
                            last_keep_alive = time.time()
                            continue
                        log.warning(f"Keep-alive / health check failed: {e}")
                    last_keep_alive = now

                # --- Session expiry check ---
                try:
                    cur_url = page.url.lower()
                    if any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
                        if is_waiting:
                            log.warning("Session expired (URL redirect) in WAIT MODE, ignoring as per preference.")
                        else:
                            print("") # visual break
                            log.warning(" Session expired  ->  browser redirected to login page. Triggering recovery...")

                            success = await _recover_with_retry(page, customer, username)
                            if not success:
                                log.error("Recovery failed repeatedly. Rotating IP in-place")
                                await _rotate_ip_and_refresh(page, customer, "repeated recovery failure after login-redirect session expiry", username=username)
                            last_keep_alive = time.time()
                except Exception:
                    pass

            except KeyboardInterrupt:
                log.info("Stopped by user.")
                break
            except Exception as e:
                log.error(
                    f"Unexpected error in watch loop: {e}",
                    exc_info=True,
                )

                latest_state = _read_state(state_file)

                if latest_state.get("booking_running"):
                    _enter_booking_rest(
                        state_file,
                        customer,
                        f"Unexpected error during booking: {e}",
                    )
                else:
                    _update_state(
                        state_file,
                        {"booking_running": False},
                    )

                await asyncio.sleep(5)

    # Cleanup on exit
    _update_state(state_file, {"booking_running": False, "pending": False, "booking_runner_alive": False})
    _close_slot_window()
    release_fleet_booking_lock(uid)  # release for next account
    _advance_queue(uid)  # advance queue before exit
    _safe_send(send_monitoring_status, customer=customer, user_id=username,
               mode="N/A", started=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OFC Appointment Booking Runner")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--customer",  type=str, default="default")
    parser.add_argument("--username",  type=str, required=True)
    args = parser.parse_args()
    log.info(f"Starting booking runner for customer '{args.customer}' ({args.username}) on Chrome port {args.cdp_port}")
    asyncio.run(run(args.cdp_port, args.customer, args.username))
