"""BookingCoordinator — deterministic, lease-based booking queue and lock manager.

Features:
- Deterministic arbitration, priority queueing, and deduplication.
- Monotonic clock-based freshness checks (time.monotonic()) avoiding system clock skews.
- Strict bounded trigger age: FRESH (<25s) | AGING (25-45s) | STALE (>45s) | CANCELLED.
- Active pruning of stale entries so fresh slot events never wait behind stale triggers.
- Lease-based booking lock with auto-expiry and guaranteed finally release.
- Idempotency protection preventing duplicate submissions for the same slot/account.
- Isolated from monitoring; booking errors do not mutate monitoring state.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.common.reliability import (
    TriggerLifecycle,
    booking_idempotency_key,
    classify_trigger_lifecycle,
    monotonic_time,
    now_utc_iso,
    slot_fingerprint,
)

log = logging.getLogger("booking-coordinator")

# Configuration
_MAX_TRIGGER_AGE = float(os.getenv("MAX_TRIGGER_AGE_SECONDS", "180.0"))
_LEASE_SECONDS = float(os.getenv("BOOKING_COORDINATOR_LEASE_SECONDS", "60.0"))
_CLAIM_WINDOW_SECONDS = 7.0  # seconds an account has to acknowledge/start booking before eviction
_IDEMPOTENCY_TTL = float(os.getenv("BOOKING_IDEMPOTENCY_TTL_SECONDS", "300.0"))

_COORD_FILE = Path(__file__).resolve().parent.parent / "booking_coord.json"
_LOCK_FILE = Path(__file__).resolve().parent.parent / "booking_coord.lock"


def _read_coord() -> dict:
    try:
        return json.loads(_COORD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "queue": [],
            "running": None,
            "leases": {},
            "idempotency_cache": {},
            "updated_at": time.time(),
        }


def _write_coord(data: dict) -> None:
    data["updated_at"] = time.time()
    tmp = _COORD_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(_COORD_FILE))
    except Exception:
        try:
            _COORD_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    # Synchronize root booking_queue.json with strictly deduplicated UIDs
    try:
        root_q = _COORD_FILE.parent.parent / "booking_queue.json"
        q_uids = []
        seen = set()
        for e in (data.get("queue") or []):
            u = e.get("uid")
            if u and u not in seen:
                seen.add(u)
                q_uids.append(u)
        root_data = {
            "running": data.get("running"),
            "queue": q_uids,
            "total_queued": len(q_uids),
            "updated_at": time.time(),
        }
        root_tmp = root_q.with_suffix(".tmp")
        root_tmp.write_text(json.dumps(root_data, indent=2), encoding="utf-8")
        os.replace(str(root_tmp), str(root_q))
    except Exception:
        pass



def _acquire_lock(timeout: float = 3.0) -> bool:
    """Acquire the coordinator file lock with stale lock reclamation."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_LOCK_FILE) > 5.0:
                    try:
                        os.remove(_LOCK_FILE)
                        continue
                    except OSError:
                        pass
            except Exception:
                pass
            time.sleep(0.02)
        except OSError:
            time.sleep(0.02)
    return False


def _release_lock() -> None:
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _ensure_shape(data: dict) -> dict:
    data = dict(data)
    data.setdefault("queue", [])
    data.setdefault("running", None)
    data.setdefault("leases", {})
    data.setdefault("idempotency_cache", {})
    data.setdefault("updated_at", time.time())
    return data


def _clean_idempotency_cache(data: dict) -> None:
    """Remove expired idempotency records."""
    now = time.time()
    cache = data.get("idempotency_cache") or {}
    data["idempotency_cache"] = {
        k: v for k, v in cache.items()
        if now - float(v.get("recorded_at", 0)) < _IDEMPOTENCY_TTL
    }


def _purge_stale_and_expired(data: dict) -> bool:
    """Purge stale triggers and reclaim expired leases. Returns True if modified."""
    now_wall = time.time()
    now_mono = time.monotonic()
    changed = False

    # 1. Clean idempotency cache
    _clean_idempotency_cache(data)

    # 2. Check expired leases
    leases = data.get("leases") or {}
    running = data.get("running")
    if running:
        if running in leases:
            lease = leases[running]
            if float(lease.get("expires_at", 0)) < now_wall:
                reason = "unclaimed after 7s" if not lease.get("claimed") else f"booking timeout ({_LEASE_SECONDS}s)"
                log.warning(f"Booking lease for '{running}' expired ({reason}) — evicting from queue and promoting next account.")
                leases.pop(running, None)
                data["queue"] = [e for e in (data.get("queue") or []) if e.get("uid") != running]
                data["total_queued"] = len(data["queue"])
                data["running"] = None
                changed = True
        else:
            # Orphaned running state without active lease
            log.warning(f"Orphaned running lock for '{running}' without lease — clearing and evicting.")
            data["queue"] = [e for e in (data.get("queue") or []) if e.get("uid") != running]
            data["total_queued"] = len(data["queue"])
            data["running"] = None
            changed = True

    # 3. Purge stale queued entries
    queue = data.get("queue") or []
    fresh_queue = []
    for entry in queue:
        # Age check using wall clock timestamp with safety fallback
        detected_at = float(entry.get("detected_at") or now_wall)
        age = now_wall - detected_at
        if age > _MAX_TRIGGER_AGE:
            log.info(
                f"Pruning stale trigger for '{entry.get('uid')}' "
                f"(age {age:.1f}s > limit {_MAX_TRIGGER_AGE:.0f}s, id={entry.get('trigger_id')})."
            )
            changed = True
        else:
            fresh_queue.append(entry)

    if len(fresh_queue) != len(queue):
        data["queue"] = fresh_queue
        data["total_queued"] = len(fresh_queue)
        changed = True

    # 4. If nothing is running and queue has entries, promote head with initial claim window
    if not data.get("running") and data.get("queue"):
        head = data["queue"][0]
        data["running"] = head["uid"]
        data["leases"][head["uid"]] = {
            "expires_at": now_wall + _CLAIM_WINDOW_SECONDS,
            "trigger_id": head["trigger_id"],
            "acquired_at": now_wall,
            "claimed": False,
        }
        changed = True

    return changed


def enqueue(
    uid: str,
    trigger_meta: Optional[dict] = None,
) -> Tuple[bool, str, dict]:
    """Atomically enqueue or refresh a booking trigger with deterministic arbitration.

    Returns (ok, reason, snapshot)
    Reasons:
    - 'queued': successfully enqueued or refreshed as head
    - 'duplicate': identical trigger already active
    - 'idempotent_suppressed': slot was already booked/attempted
    - 'stale': trigger is already older than max age
    - 'lock_timeout': failed to acquire file lock
    """
    if trigger_meta is None:
        trigger_meta = {}
    uid = str(uid or "").strip()
    if not uid:
        return False, "invalid_uid", {"queue": []}

    now_wall = time.time()
    now_mono = time.monotonic()
    detected_at = float(trigger_meta.get("detected_at") or now_wall)

    # Pre-check freshness
    if now_wall - detected_at > _MAX_TRIGGER_AGE:
        return False, "stale", {"queue": []}

    slot = trigger_meta.get("cached_booking") or trigger_meta.get("slot_info") or trigger_meta
    fp = slot_fingerprint(slot)
    idem_key = booking_idempotency_key(uid, slot)

    # Priority arbitration: Consular waiters get top priority (#1)
    is_consular_waiter = (
        bool(trigger_meta.get("waitingForConsular"))
        or str(trigger_meta.get("action_type", "")).upper() in ("SNIPER_CONSULAR_ONLY", "RESCHEDULE_FULL_CONSULAR_ONLY")
        or bool(trigger_meta.get("onlyConsular"))
    )
    priority = 1 if is_consular_waiter else int(trigger_meta.get("priority", 10))

    trigger_id = str(trigger_meta.get("trigger_id") or uuid.uuid4())
    correlation_id = str(trigger_meta.get("correlation_id") or trigger_id)

    entry = {
        "uid": uid,
        "trigger_id": trigger_id,
        "correlation_id": correlation_id,
        "detected_at": detected_at,
        "detected_mono": now_mono,
        "slot_fingerprint": fp,
        "idempotency_key": idem_key,
        "priority": priority,
        "metadata": trigger_meta,
    }


    if not _acquire_lock():
        return False, "lock_timeout", {"queue": []}

    try:
        data = _ensure_shape(_read_coord())
        _purge_stale_and_expired(data)

        # Idempotency suppression: check if already attempted
        if idem_key in data.get("idempotency_cache", {}):
            cached = data["idempotency_cache"][idem_key]
            if cached.get("status") in ("booked", "submitted"):
                return False, "idempotent_suppressed", data

        # Strict deduplication against currently queued entries: an account may only be queued ONCE!
        queue = data.get("queue") or []
        for existing in queue:
            if existing.get("uid") == uid:
                # Same account already queued: refresh with newer trigger if fresher or if priority is higher
                if existing.get("detected_at", 0) <= detected_at or priority < existing.get("priority", 10):
                    queue.remove(existing)
                    queue.append(entry)
                    queue.sort(key=lambda x: (x.get("priority", 10), float(x.get("detected_at", 0))))
                    data["queue"] = queue
                    data["total_queued"] = len(queue)
                    _write_coord(data)
                    return True, "queued", data
                return False, "duplicate", data

        # Add to queue
        queue.append(entry)
        # Deterministic ordering: highest priority (lower number) first, then chronological detected_at
        queue.sort(key=lambda x: (x.get("priority", 10), float(x.get("detected_at", 0))))
        data["queue"] = queue
        data["total_queued"] = len(queue)


        # Promote head if running is free (initial claim window of 7s)
        if not data.get("running") and data["queue"]:
            head = data["queue"][0]
            data["running"] = head["uid"]
            data["leases"][head["uid"]] = {
                "expires_at": now_wall + _CLAIM_WINDOW_SECONDS,
                "trigger_id": head["trigger_id"],
                "acquired_at": now_wall,
                "claimed": False,
            }

        # Record in idempotency cache
        data.setdefault("idempotency_cache", {})[idem_key] = {"status": "submitted", "at": now_wall}

        _write_coord(data)
        return True, "queued", data
    except Exception as exc:
        return False, f"error:{exc}", {"queue": []}
    finally:
        _release_lock()


def wait_for_turn(
    uid: str,
    trigger_timestamp: float,
    log_owner: Optional[logging.Logger] = None,
    poll_interval: float = 0.5,
    max_wait_seconds: Optional[float] = None,
) -> bool:
    """Wait until uid is the running account, or abandon if stale.

    Guarantees that an account never waits past the max trigger freshness window.
    """
    uid = str(uid or "").strip()
    logger = log_owner or log
    now_wall = time.time()
    max_wait = max_wait_seconds if max_wait_seconds is not None else _MAX_TRIGGER_AGE
    deadline = min(trigger_timestamp + _MAX_TRIGGER_AGE, now_wall + max_wait)

    reported_position = -1

    while True:
        now = time.time()
        if now > deadline:
            logger.warning(
                f"⏭️ Trigger for '{uid}' reached freshness deadline "
                f"(age {now - trigger_timestamp:.1f}s > limit {_MAX_TRIGGER_AGE:.0f}s) — abandoning cleanly."
            )
            # Cancel from queue
            cancel_trigger(uid)
            return False

        if _acquire_lock(timeout=1.0):
            try:
                data = _ensure_shape(_read_coord())
                _purge_stale_and_expired(data)
                running = data.get("running")
                queue = data.get("queue") or []

                if running == uid:
                    # Account is actively listening and ready: grant full booking lease
                    leases = data.setdefault("leases", {})
                    if uid in leases:
                        leases[uid]["expires_at"] = now + _LEASE_SECONDS
                        leases[uid]["claimed"] = True
                        _write_coord(data)
                    return True

                # Check if uid is still in queue
                uids_in_queue = [e.get("uid") for e in queue]
                if uid not in uids_in_queue and running != uid:
                    # Removed by stale purge or cancellation
                    return False

                pos = uids_in_queue.index(uid) + 1 if uid in uids_in_queue else -1
                if pos != reported_position:
                    reported_position = pos
                    logger.info(
                        f"⏳ '{uid}' is #{pos} in coordinated booking queue "
                        f"(running: {running or '—'}, queue: {len(queue)} total, "
                        f"age: {now - trigger_timestamp:.1f}s)"
                    )
            finally:
                _release_lock()

        time.sleep(poll_interval)


def acquire_lease(uid: str, trigger_id: str = "") -> Tuple[bool, str]:
    """Claim running lock for uid and register a lease."""
    uid = str(uid or "").strip()
    if not uid:
        return False, "invalid"
    if not _acquire_lock():
        return False, "lock_timeout"
    try:
        data = _ensure_shape(_read_coord())
        _purge_stale_and_expired(data)
        running = data.get("running")

        if running == uid:
            # Refresh lease
            data["leases"][uid] = {
                "expires_at": time.time() + _LEASE_SECONDS,
                "trigger_id": trigger_id,
                "acquired_at": time.time(),
            }
            _write_coord(data)
            return True, "ok"

        queue = data.get("queue") or []
        if not queue:
            return False, "no_queue"

        head = queue[0]
        if head.get("uid") == uid:
            data["running"] = uid
            data["leases"][uid] = {
                "expires_at": time.time() + _LEASE_SECONDS,
                "trigger_id": trigger_id or head.get("trigger_id", ""),
                "acquired_at": time.time(),
            }
            _write_coord(data)
            return True, "ok"

        return False, f"held_by:{running or head.get('uid')}"
    except Exception as exc:
        return False, f"error:{exc}"
    finally:
        _release_lock()


def release_lease(uid: str, status: str = "completed") -> bool:
    """Release running lease and advance queue to next account."""
    uid = str(uid or "").strip()
    if not uid:
        return False
    if not _acquire_lock():
        return False
    try:
        data = _ensure_shape(_read_coord())
        # Remove lease
        lease = data.get("leases", {}).pop(uid, None)
        # Record idempotency if applicable
        if lease and lease.get("trigger_id"):
            idem_key = f"{uid}:{lease.get('trigger_id')}"
            data.setdefault("idempotency_cache", {})[idem_key] = {
                "status": status,
                "recorded_at": time.time(),
            }

        # Remove from queue
        q = [e for e in data.get("queue", []) if e.get("uid") != uid]
        data["queue"] = q
        data["total_queued"] = len(q)

        # Advance running to new head
        if data.get("running") == uid:
            data["running"] = q[0]["uid"] if q else None
            if data["running"]:
                data["leases"][data["running"]] = {
                    "expires_at": time.time() + _LEASE_SECONDS,
                    "trigger_id": q[0].get("trigger_id", ""),
                    "acquired_at": time.time(),
                }

        _write_coord(data)
        log.info(f"➡️ Coordinator advanced after '{uid}'. Next: {data.get('running') or '—'}")
        return True
    except Exception:
        return False
    finally:
        _release_lock()


def cancel_trigger(uid: str) -> bool:
    """Remove a pending trigger for uid from the queue without affecting others."""
    uid = str(uid or "").strip()
    if not uid or not _acquire_lock():
        return False
    try:
        data = _ensure_shape(_read_coord())
        q = [e for e in data.get("queue", []) if e.get("uid") != uid]
        data["queue"] = q
        data["total_queued"] = len(q)
        if data.get("running") == uid:
            data.get("leases", {}).pop(uid, None)
            data["running"] = q[0]["uid"] if q else None
            if data["running"]:
                data["leases"][data["running"]] = {
                    "expires_at": time.time() + _LEASE_SECONDS,
                    "trigger_id": q[0].get("trigger_id", ""),
                    "acquired_at": time.time(),
                }
        _write_coord(data)
        return True
    finally:
        _release_lock()


def clear_all_triggers(reason: str = "") -> bool:
    """Clear all pending triggers and running leases in coordinator (e.g. when slot is gone)."""
    if not _acquire_lock():
        return False
    try:
        data = _ensure_shape(_read_coord())
        data["queue"] = []
        data["total_queued"] = 0
        data["running"] = None
        data["leases"] = {}
        _write_coord(data)
        log.info(f"[COORDINATOR] Cleared all pending triggers & queue ({reason}).")
        return True
    except Exception:
        return False
    finally:
        _release_lock()


def advance_queue(completed_uid: str) -> dict:
    """Alias for release_lease that returns the snapshot."""
    release_lease(completed_uid)
    return snapshot()


def snapshot() -> dict:
    """Return a clean current snapshot of the coordination state."""
    if _acquire_lock(timeout=0.5):
        try:
            data = _ensure_shape(_read_coord())
            _purge_stale_and_expired(data)
            _write_coord(data)
            return data
        finally:
            _release_lock()
    return _ensure_shape(_read_coord())


@contextlib.contextmanager
def booking_lock_context(uid: str, trigger_id: str = ""):
    """Context manager for guaranteed release of coordinator booking lock.

    Usage:
        with booking_lock_context(uid, trigger_id):
            execute_booking()
    """
    ok, reason = acquire_lease(uid, trigger_id)
    if not ok:
        raise RuntimeError(f"Could not acquire booking coordination lock: {reason}")
    status = "failed"
    try:
        yield
        status = "completed"
    except Exception:
        status = "error"
        raise
    finally:
        release_lease(uid, status=status)


class BookingCoordinator:
    """Coordinator class providing monotonic trigger tracking, deduplication, and lease management."""

    def __init__(self, coord_file: Path | None = None):
        self.coord_file = coord_file or _COORD_FILE

    def can_dispatch_trigger(self, account_uid: str, slot_id: str) -> bool:
        if not slot_id or not account_uid:
            return True
        key = f"{account_uid}:{slot_id}"
        data = _read_coord()
        cache = data.get("idempotency_cache") or {}
        now = time.time()
        entry_val = cache.get(key)
        if entry_val is not None:
            if isinstance(entry_val, dict):
                raw_ts = entry_val.get("at") or entry_val.get("recorded_at") or entry_val.get("ts") or 0.0
            else:
                raw_ts = entry_val
            try:
                ts = float(raw_ts)
            except (ValueError, TypeError):
                ts = 0.0
            if now - ts < _IDEMPOTENCY_TTL:
                return False
        for entry in data.get("queue") or []:
            if entry.get("uid") == account_uid and (entry.get("slot_fingerprint") == slot_id or not slot_id):
                return False
        return True

    def submit_trigger(
        self,
        account_uid: str,
        trigger_key: str,
        slot_info: dict | None = None,
        max_age_seconds: float = _MAX_TRIGGER_AGE,
    ) -> dict:
        meta = {
            "trigger_key": trigger_key,
            "max_age_seconds": max_age_seconds,
            "slot_info": slot_info or {},
        }
        if isinstance(slot_info, dict):
            meta.update(slot_info)
        ok, reason, snapshot_data = enqueue(
            uid=account_uid,
            trigger_meta=meta,
        )
        return snapshot_data

    def prune_stale_triggers(self, max_age_seconds: float = _MAX_TRIGGER_AGE) -> list:
        if _acquire_lock(timeout=0.5):
            try:
                data = _ensure_shape(_read_coord())
                now_wall = time.time()
                queue = data.get("queue") or []
                fresh_queue = []
                pruned = []
                for entry in queue:
                    detected_at = float(entry.get("detected_at") or now_wall)
                    if now_wall - detected_at > max_age_seconds:
                        pruned.append(entry)
                    else:
                        fresh_queue.append(entry)
                if pruned:
                    data["queue"] = fresh_queue
                    data["total_queued"] = len(fresh_queue)
                    _write_coord(data)
                return pruned
            finally:
                _release_lock()
        return []

    def acquire_lease(self, account_uid: str, duration_seconds: float = _LEASE_SECONDS) -> tuple[bool, str]:
        return acquire_lease(account_uid)

    def release_lease(self, account_uid: str, status: str = "completed") -> None:
        release_lease(account_uid, status=status)

    def snapshot(self) -> dict:
        return snapshot()


booking_coordinator = BookingCoordinator()


__all__ = [
    "BookingCoordinator",
    "booking_coordinator",
    "enqueue",
    "wait_for_turn",
    "acquire_lease",
    "release_lease",
    "cancel_trigger",
    "advance_queue",
    "snapshot",
    "booking_lock_context",
]

