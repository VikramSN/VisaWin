"""Booking Queue — sequential, ordered processing for one-shot cached bookings.

Delegates authoritative queueing, deterministic arbitration, and lease locking
to src/booking/coordinator.py while maintaining booking_queue.json for
operator visibility.

Rules:
- Triggers have bounded lifetimes (default <= 35s). Never wait 600s in queue.
- Stale triggers are pruned actively.
- Queue advances reliably on any completion (success, failure, cancel, timeout).
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from src.booking import coordinator as booking_coordinator
from src.common.fleet_lock import (
    fleet_lock_holder,
    release_fleet_booking_lock,
    try_acquire_fleet_booking_lock,
)
from src.common.state import read_state as _read_state, write_state as _write_state

log = logging.getLogger("booking-queue")

_QUEUE_FILE = Path(__file__).resolve().parent.parent / "booking_queue.json"
_QUEUE_POLL_INTERVAL = float(os.getenv("QUEUE_POLL_INTERVAL_SECONDS", "0.5"))
_QUEUE_STALE_LIMIT = float(os.getenv("QUEUE_STALE_LIMIT_SECONDS", "180.0"))


# ── Queue state helpers ─────────────────────────────────────────────────


def _read_queue() -> dict:
    """Read the booking queue state file (best-effort)."""
    return _read_state(_QUEUE_FILE)


def _write_queue(data: dict) -> None:
    """Atomically persist the queue state."""
    data["updated_at"] = time.time()
    _write_state(_QUEUE_FILE, data)


def _enqueue(uid: str, trigger_meta: dict | None = None) -> dict:
    """Add `uid` to the coordinated booking queue with bounded age."""
    meta = dict(trigger_meta or {})
    if "detected_at" not in meta:
        meta["detected_at"] = time.time()

    try:
        ok, reason, info = booking_coordinator.enqueue(uid, meta)
    except Exception as exc:
        log.warning(f"Coordinator enqueue error for '{uid}': {exc}")
        ok, reason, info = False, "error", {"queue": []}

    try:
        if isinstance(info, dict):
            _write_queue({
                "running": info.get("running"),
                "queue": [e.get("uid") for e in (info.get("queue") or [])],
                "total_queued": int(info.get("total_queued", 0)),
            })
    except Exception:
        pass

    return info


def _dequeue_and_advance(current_uid: str) -> dict:
    """Remove `current_uid` from the queue and promote the next account."""
    try:
        info = booking_coordinator.advance_queue(current_uid)
    except Exception:
        info = _read_queue()

    try:
        if isinstance(info, dict):
            _write_queue({
                "running": info.get("running"),
                "queue": [e.get("uid") for e in (info.get("queue") or [])],
                "total_queued": int(info.get("total_queued", 0)),
            })
    except Exception:
        pass

    return info


def _set_running(uid: str | None) -> dict:
    """Set the currently-running uid. Promote next queued if uid is None."""
    data = _read_queue()
    if uid is None:
        queue: list[str] = data.get("queue") or []
        data["running"] = queue[0] if queue else None
    else:
        data["running"] = uid
    _write_queue(data)
    return data


def _remove_stale(uid: str) -> dict:
    """Remove `uid` from queue if it is present; cancel in coordinator."""
    try:
        booking_coordinator.cancel_trigger(uid)
    except Exception:
        pass

    data = _read_queue()
    queue: list[str] = data.get("queue") or []
    if uid in queue:
        queue.remove(uid)
        data["queue"] = queue
        data["total_queued"] = len(queue)
        if data.get("running") == uid:
            data["running"] = queue[0] if queue else None
    _write_queue(data)
    return data


def _advance_queue(completed_uid: str) -> dict:
    """Advance the queue after a booking completes (success or failure)."""
    try:
        booking_coordinator.release_lease(completed_uid)
    except Exception:
        pass

    data = _read_queue()
    queue: list[str] = data.get("queue") or []

    if completed_uid in queue:
        queue.remove(completed_uid)
        data["queue"] = queue
        data["total_queued"] = len(queue)
        if data.get("running") == completed_uid:
            data["running"] = queue[0] if queue else None
        _write_queue(data)
    elif data.get("running") == completed_uid:
        data["running"] = queue[0] if queue else None
        _write_queue(data)

    log.info(f"➡️ Queue advanced after '{completed_uid}'. Next: {data.get('running') or '—'}")
    return data


# ── Turn waiting ────────────────────────────────────────────────────────


async def wait_for_turn(
    uid: str,
    trigger_timestamp: float,
    log_owner: logging.Logger | None = None,
    max_wait_seconds: float | None = None,
) -> bool:
    """Wait until `uid` is the running account OR its trigger goes stale.

    Bounded to max_wait_seconds (default _QUEUE_STALE_LIMIT).
    Never sits waiting 600s in queue.
    """
    logger = log_owner if log_owner else log
    wait_limit = max_wait_seconds if max_wait_seconds is not None else _QUEUE_STALE_LIMIT

    loop = asyncio.get_running_loop()
    # Run the bounded wait in executor or async loop
    turn_ok = await loop.run_in_executor(
        None,
        booking_coordinator.wait_for_turn,
        uid,
        trigger_timestamp,
        logger,
        _QUEUE_POLL_INTERVAL,
        wait_limit,
    )

    if not turn_ok:
        _remove_stale(uid)
        return False

    return True


def clear_queue(reason: str = "") -> dict:
    """Clear all pending triggers from queue and coordinator (e.g. when slot is gone)."""
    try:
        booking_coordinator.clear_all_triggers(reason=reason)
    except Exception:
        pass
    data = {"running": None, "queue": [], "total_queued": 0, "updated_at": time.time()}
    _write_queue(data)
    log.info(f"[QUEUE] Cleared booking queue ({reason}).")
    return data


__all__ = [
    "_read_queue",
    "_write_queue",
    "_enqueue",
    "_dequeue_and_advance",
    "_set_running",
    "_remove_stale",
    "_advance_queue",
    "wait_for_turn",
    "clear_queue",
]