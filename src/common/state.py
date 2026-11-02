"""
Thread-safe state file I/O with Windows-compatible file locking.

State files (src/state_<uid>.json) are shared between the monitor runner
and the booking runner. This module provides locked read/write to prevent
race conditions.
"""

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from src.common.config import ACCOUNTS_FILE
from src.common.utils import safe_id


def _acquire_lock(lock_file: Path, timeout: float = 5.0) -> bool:
    """Acquire a cross-process lock using a dedicated .lock file.

    A lock file older than STALE_LOCK_AFTER seconds is treated as orphaned
    (left behind by a crashed/killed process, since _release_lock only runs
    in a finally block) and is force-removed so booking is never blocked
    indefinitely by a zombie lock.
    """
    start = time.time()
    stale_after = 15.0  # seconds

    while time.time() - start < timeout:
        try:
            # os.O_EXCL ensures this fails if the file already exists.
            fd = os.open(
                str(lock_file),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.close(fd)
            return True

        except FileExistsError:
            # Take over a stale/orphaned lock so a dead process can never
            # block booking forever.
            try:
                if time.time() - os.path.getmtime(lock_file) > stale_after:
                    try:
                        os.remove(str(lock_file))
                    except OSError:
                        pass
            except OSError:
                pass
            time.sleep(0.02)

        except OSError:
            time.sleep(0.02)

    return False


def _release_lock(lock_file: Path) -> None:
    """Release the cross-process lock by deleting the .lock file."""
    try:
        lock_file.unlink()
    except OSError:
        pass


def read_state(state_file: Path) -> dict:
    """Read and parse a JSON state file. Returns {} on any error."""
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state_file: Path, state: dict) -> None:
    """
    Write the state dictionary using an atomic write pattern.

    Writes to a temporary file first, then replaces the target file so
    another process does not read partially written JSON.
    """
    tmp_path = state_file.with_suffix(".json.tmp")

    try:
        tmp_path.write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )

        # Atomic when source and destination are on the same filesystem.
        os.replace(str(tmp_path), str(state_file))

    except Exception:
        # Fallback if atomic replacement fails.
        state_file.write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )

        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _is_reserved_booking_state(state_file: Path) -> bool:
    """
    Return True only if the state file belongs to an account whose role
    is RESERVED_BOOKING in accounts.json.
    """
    try:
        if not ACCOUNTS_FILE.exists():
            print(
                f"[STATE] ⚠️ Cannot validate booking role because "
                f"accounts.json was not found at: {ACCOUNTS_FILE}"
            )
            return False

        state_uid = state_file.stem.replace("state_", "")

        accounts = json.loads(
            ACCOUNTS_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(accounts, list):
            print("[STATE] ⚠️ accounts.json must contain a JSON list.")
            return False

        for account in accounts:
            username = str(account.get("username", "")).strip()
            role = str(account.get("role", "")).strip().upper()

            if safe_id(username) == state_uid:
                return role == "RESERVED_BOOKING"

        print(
            f"[STATE] ⚠️ No account matched state file "
            f"'{state_file.name}'."
        )
        return False

    except Exception as e:
        print(f"[STATE] ⚠️ Could not validate account role: {e}")
        return False


def _send_remote_trigger(
    remote_url: str,
    state_file: Path,
    updates: dict,
) -> None:
    """Send a booking trigger to the remote booking PC or Central Server Hub."""
    try:
        username = state_file.stem.replace("state_", "")

        payload = json.dumps(
            {
                "username": username,
                "updates": updates,
            }
        ).encode("utf-8")

        api_key = (
            os.environ.get("SERVER_API_KEY", "")
            or os.environ.get("API_KEY", "")
            or os.environ.get("REMOTE_TRIGGER_API_KEY", "")
            or "visabot-secure-api-key-2026"
        )

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        }

        request = urllib.request.Request(
            remote_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            print(
                f"[STATE] ✅ Remote trigger sent for '{username}' "
                f"(HTTP {response.status})"
            )

    except Exception as e:
        print(
            f"[STATE] ❌ Failed to send remote trigger "
            f"to {remote_url}: {e}"
        )

def update_state(state_file: Path, updates: dict) -> None:
    """
    Update the local state.

    When REMOTE_TRIGGER_URL is configured, send triggers to Server Hub.
    In ALL_IN_ONE mode, keep pending=True so local booking runner fires immediately.
    In POLLING mode, clear pending=True since a separate booking PC will handle execution.
    """
    updates_to_write = dict(updates)
    remote_url = ""
    should_send_remote = False

    if updates.get("pending") is True:
        remote_url = os.environ.get(
            "REMOTE_TRIGGER_URL",
            "",
        ).strip()
        laptop_role = os.environ.get("LAPTOP_ROLE", "").strip().upper()

        if remote_url and laptop_role != "BOOKING":
            if _is_reserved_booking_state(state_file):
                should_send_remote = True

                if laptop_role == "POLLING":
                    # Dedicated Polling-only laptop: clear pending so it doesn't try booking locally
                    updates_to_write["pending"] = False
                    updates_to_write["remote_trigger_sent_at"] = time.time()
                    updates_to_write["remote_trigger_status"] = "sent_to_booking_pc"
                else:
                    # ALL_IN_ONE mode: Keep pending=True so local sniper account executes booking immediately!
                    updates_to_write["pending"] = True
                    updates_to_write["remote_trigger_sent_at"] = time.time()
                    updates_to_write["remote_trigger_status"] = "broadcast_and_local_booking"

            else:
                username = state_file.stem.replace("state_", "")
                print(
                    f"[STATE] ⏭️ Remote trigger blocked for "
                    f"'{username}': account is not RESERVED_BOOKING."
                )

    lock_file = state_file.with_suffix(".lock")

    if _acquire_lock(lock_file):
        try:
            state = read_state(state_file)
            state.update(updates_to_write)
            write_state(state_file, state)
        finally:
            _release_lock(lock_file)
    else:
        state = read_state(state_file)
        state.update(updates_to_write)
        write_state(state_file, state)

    # Start remote sending only after local state has been written.
    if should_send_remote:
        threading.Thread(
            target=_send_remote_trigger,
            args=(remote_url, state_file, updates),
            daemon=True,
        ).start()
def try_queue_local_trigger(
    state_file: Path,
    updates: dict,
) -> tuple[bool, str]:
    """
    Atomically queue a local booking trigger.

    Blocks booking when the account:
    - is completed;
    - is resting;
    - is already booking;
    - already has a pending trigger.
    """
    if updates.get("pending") is not True:
        return False, "invalid_trigger"

    lock_file = state_file.with_suffix(".lock")

    # Do not use an unlocked fallback for trigger creation.
    # That could allow CVS and self-polling to queue simultaneously.
    if not _acquire_lock(lock_file):
        return False, "lock_timeout"

    try:
        state = read_state(state_file)
        now = time.time()

        if state.get("completed"):
            return False, "completed"

        try:
            rest_until = float(state.get("rest_until", 0) or 0)
        except Exception:
            rest_until = 0

        if rest_until > now:
            remaining = int(rest_until - now)
            return False, f"resting:{remaining}"

        if state.get("booking_running"):
            return False, "running"

        if state.get("pending"):
            return False, "pending"

        safe_updates = dict(updates)

        # A trigger producer must never overwrite the runner's busy flag.
        safe_updates.pop("booking_running", None)

        safe_updates["pending"] = True
        safe_updates["trigger_timestamp"] = (
            safe_updates.get("trigger_timestamp") or now
        )

        state.update(safe_updates)
        write_state(state_file, state)

        return True, "queued"

    finally:
        _release_lock(lock_file)
def get_state_file(username: str) -> Path:
    """Return the state-file path for a username."""
    uid = safe_id(username)

    return (
        Path(__file__).resolve().parent.parent
        / f"state_{uid}.json"
    )