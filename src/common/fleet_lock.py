"""
Fleet booking lock - cross-process, file-based sequencing for the One-Shot
Cached Booking architecture.

GOAL
────
Only ONE account may execute a booking attempt at a time, and consecutive
attempts (across accounts) must be separated by a calm-down gap. This keeps
the total portal request rate predictable and is the primary defence against
Cloudflare 429 bursts when a hot slot matches multiple accounts.

MECHANICS
─────────
* `fleet_booking.lock`      - an O_EXCL-created marker file whose content is
                              `{"owner": <uid>, "created_at": <ts>}`. Same
                              Windows-safe pattern as src/common/state.py.
* Stale takeover            - a lock older than FLEET_LOCK_STALE_SECONDS
                              (default 180 s) is treated as orphaned (a
                              crashed booking process) and is force-taken.
                              180 s safely covers a full one-shot attempt
                              including a <=90 s Cloudflare-waiting-room heal.
* `fleet_last_release.json` - timestamp of the last deliberate release.
                              A NEW acquisition is refused while we are inside
                              the inter-account gap (default 20 s) so the next
                              account never fires immediately after the
                              previous attempt finished.

Every function is best-effort and never raises: a locking failure must never
crash a booking runner.
"""

import json
import os
import time
from pathlib import Path

_LOCK_FILE = Path(__file__).resolve().parent.parent / "fleet_booking.lock"
_GAP_FILE = Path(__file__).resolve().parent.parent / "fleet_last_release.json"

# How long two booking attempts (different accounts) must be apart.
# Set to 0.2s because accounts run on independent proxy slots/ports.
GAP_SECONDS = float(os.getenv("FLEET_BOOKING_GAP_SECONDS", "0.2"))

# A booking attempt holding the lock longer than this is considered dead.
STALE_SECONDS = float(os.getenv("FLEET_LOCK_STALE_SECONDS", "60.0"))

_ACQUIRE_TIMEOUT = 2.0  # seconds - short; callers defer and retry instead



def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def fleet_lock_holder() -> str | None:
    """Return the uid owning the fleet lock, or None when free.

    A lock whose file is older than STALE_SECONDS is considered orphaned and
    is removed here (crash recovery) so the fleet can never be wedged.
    """
    try:
        if not _LOCK_FILE.exists():
            return None
        if time.time() - _LOCK_FILE.stat().st_mtime > STALE_SECONDS:
            try:
                _LOCK_FILE.unlink()
            except OSError:
                pass
            return None
        data = _read_json(_LOCK_FILE)
        owner = str(data.get("owner") or "").strip()
        return owner or None
    except Exception:
        return None


def try_acquire_fleet_booking_lock(owner: str) -> tuple[bool, str]:
    """Try to take the fleet booking lock for `owner` (non-blocking-ish).

    Returns (True, "ok") on success, otherwise (False, reason) where reason
    is one of:
        "gap:<remaining>s"   - inside the inter-account calm-down gap
        "held:<other_owner>" - another account is currently booking
        "timeout"            - could not create the lock file in time
    """
    owner = str(owner or "").strip() or "unknown"

    # 1. Inter-account calm-down gap (only after a previous release).
    try:
        last = float(_read_json(_GAP_FILE).get("released_at") or 0)
    except Exception:
        last = 0.0
    remaining = GAP_SECONDS - (time.time() - last)
    if last > 0 and remaining > 0:
        return False, f"gap:{remaining:.1f}s"

    # 2. Fast pre-check for a friendly log message (races are resolved by O_EXCL).
    holder = fleet_lock_holder()
    if holder and holder != owner:
        return False, f"held:{holder}"

    # 3. Atomic creation (O_EXCL) with short retry + stale takeover.
    start = time.time()
    while time.time() - start < _ACQUIRE_TIMEOUT:
        # Re-check staleness inside the loop: another owner may have died
        # while we were waiting.
        if fleet_lock_holder() is None:
            try:
                fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, json.dumps({
                        "owner": owner,
                        "created_at": time.time(),
                    }).encode("utf-8"))
                finally:
                    os.close(fd)
                return True, "ok"
            except FileExistsError:
                pass
            except OSError:
                return False, "timeout"
        elif fleet_lock_holder() == owner:
            # Already ours (e.g. double pickup of the same account) - idempotent.
            return True, "ok"
        time.sleep(0.05)

    return False, "timeout"


def release_fleet_booking_lock(owner: str) -> bool:
    """Release the fleet lock if (and only if) `owner` holds it.

    Records the release timestamp so the inter-account gap starts now.
    Returns True when this call actually released a lock.
    """
    owner = str(owner or "").strip() or "unknown"
    released = False
    try:
        if _LOCK_FILE.exists():
            data = _read_json(_LOCK_FILE)
            if str(data.get("owner") or "") == owner or fleet_lock_holder() == owner:
                try:
                    _LOCK_FILE.unlink()
                    released = True
                except OSError:
                    pass
    except Exception:
        pass
    try:
        _write_json(_GAP_FILE, {"released_at": time.time(), "by": owner})
    except Exception:
        pass
    return released


import contextlib


@contextlib.contextmanager
def fleet_booking_lock_context(owner: str):
    """Context manager ensuring guaranteed release of the fleet booking lock."""
    ok, reason = try_acquire_fleet_booking_lock(owner)
    if not ok:
        raise RuntimeError(f"Could not acquire fleet booking lock: {reason}")
    try:
        yield
    finally:
        release_fleet_booking_lock(owner)

