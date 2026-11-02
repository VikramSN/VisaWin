"""
Request Tracker — per-account and global request volume monitoring.

Tracks API calls, 429s, 403s, 502s, network errors, and retries so operators
can see exactly where request volume comes from and identify storms.

State file: src/request_tracker.json
"""

import json
import os
import time
import threading
from pathlib import Path
from collections import deque

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TRACKER_FILE = _PROJECT_ROOT / "request_tracker.json"
TRACKER_LOCK = threading.Lock()

# Sliding window for rate calculation (seconds)
RATE_WINDOW = 60.0
_request_timestamps = deque()  # Global timestamps of requests


def _read_tracker() -> dict:
    try:
        return json.loads(TRACKER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"global": _empty_global(), "accounts": {}}


def _write_tracker(data: dict) -> None:
    tmp = TRACKER_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(TRACKER_FILE))
    except Exception:
        try:
            TRACKER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _empty_global() -> dict:
    return {
        "total_requests": 0,
        "total_429": 0,
        "total_403": 0,
        "total_502": 0,
        "total_network_errors": 0,
        "total_retries": 0,
        "total_browser_disconnects": 0,
        "total_session_refreshes": 0,
        "total_slot_detections": 0,
        "total_booking_attempts": 0,
        "total_booking_successes": 0,
        "total_booking_failures": 0,
        "average_recovery_time_seconds": 0.0,
        "last_429_at": 0,
        "last_request_at": 0,
        "requests_per_minute": 0,
    }


def _empty_account() -> dict:
    return {
        "total_requests": 0,
        "total_429": 0,
        "total_403": 0,
        "total_502": 0,
        "total_network_errors": 0,
        "total_retries": 0,
        "total_browser_disconnects": 0,
        "total_session_refreshes": 0,
        "total_slot_detections": 0,
        "total_booking_attempts": 0,
        "total_booking_successes": 0,
        "total_booking_failures": 0,
        "last_request_at": 0,
        "last_429_at": 0,
        "current_stage": "",
    }
def record_request(uid: str = "", stage: str = "") -> dict:
    """Record an API request. Returns current rate info."""
    with TRACKER_LOCK:
        data = _read_tracker()
        now = time.time()
        g = data.get("global") or _empty_global()
        g["total_requests"] = int(g.get("total_requests", 0)) + 1
        g["last_request_at"] = now
        data["global"] = g
        if uid:
            acct = data.get("accounts", {}).get(uid) or _empty_account()
            acct["total_requests"] = int(acct.get("total_requests", 0)) + 1
            acct["last_request_at"] = now
            acct["current_stage"] = stage
            if "accounts" not in data:
                data["accounts"] = {}
            data["accounts"][uid] = acct
        _request_timestamps.append(now)
        while _request_timestamps and _request_timestamps[0] < now - RATE_WINDOW:
            _request_timestamps.popleft()
        rate = len(_request_timestamps)
        g["requests_per_minute"] = rate
        _write_tracker(data)
        return {"requests_per_minute": rate, "total": g["total_requests"]}


def record_429(uid: str = "") -> None:
    """Record a 429 rate-limit response."""
    with TRACKER_LOCK:
        data = _read_tracker()
        now = time.time()
        g = data.get("global") or _empty_global()
        g["total_429"] = int(g.get("total_429", 0)) + 1
        g["last_429_at"] = now
        data["global"] = g
        if uid:
            acct = data.get("accounts", {}).get(uid) or _empty_account()
            acct["total_429"] = int(acct.get("total_429", 0)) + 1
            acct["last_429_at"] = now
            if "accounts" not in data:
                data["accounts"] = {}
            data["accounts"][uid] = acct
        _write_tracker(data)


def record_403(uid: str = "") -> None:
    """Record a 403 Forbidden response (proxy or portal)."""
    with TRACKER_LOCK:
        data = _read_tracker()
        g = data.get("global") or _empty_global()
        g["total_403"] = int(g.get("total_403", 0)) + 1
        data["global"] = g
        if uid:
            acct = data.get("accounts", {}).get(uid) or _empty_account()
            acct["total_403"] = int(acct.get("total_403", 0)) + 1
            if "accounts" not in data:
                data["accounts"] = {}
            data["accounts"][uid] = acct
        _write_tracker(data)


def record_502(uid: str = "") -> None:
    """Record a 502 Server Error response."""
    with TRACKER_LOCK:
        data = _read_tracker()
        g = data.get("global") or _empty_global()
        g["total_502"] = int(g.get("total_502", 0)) + 1
        data["global"] = g
        if uid:
            acct = data.get("accounts", {}).get(uid) or _empty_account()
            acct["total_502"] = int(acct.get("total_502", 0)) + 1
            if "accounts" not in data:
                data["accounts"] = {}
            data["accounts"][uid] = acct
        _write_tracker(data)


def record_network_error(uid: str = "") -> None:
    """Record a network-level error (XHR failure, connection refused, etc.)."""
    with TRACKER_LOCK:
        data = _read_tracker()
        g = data.get("global") or _empty_global()
        g["total_network_errors"] = int(g.get("total_network_errors", 0)) + 1
        data["global"] = g
        if uid:
            acct = data.get("accounts", {}).get(uid) or _empty_account()
            acct["total_network_errors"] = int(acct.get("total_network_errors", 0)) + 1
            if "accounts" not in data:
                data["accounts"] = {}
            data["accounts"][uid] = acct
        _write_tracker(data)


def record_retry(uid: str = "") -> None:
    """Record a retry attempt."""
    with TRACKER_LOCK:
        data = _read_tracker()
        g = data.get("global") or _empty_global()
        g["total_retries"] = int(g.get("total_retries", 0)) + 1
        data["global"] = g
        if uid:
            acct = data.get("accounts", {}).get(uid) or _empty_account()
            acct["total_retries"] = int(acct.get("total_retries", 0)) + 1
            if "accounts" not in data:
                data["accounts"] = {}
            data["accounts"][uid] = acct
        _write_tracker(data)


def _increment_counter(data: dict, scope: str, uid: str, key: str) -> None:
    if scope == "global":
        bucket = data.setdefault("global", _empty_global())
    else:
        bucket = (data.setdefault("accounts", {}).setdefault(uid, _empty_account()))
    bucket[key] = int(bucket.get(key, 0)) + 1


def record_browser_disconnect(uid: str = "") -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        _increment_counter(data, "global", uid, "total_browser_disconnects")
        if uid:
            _increment_counter(data, "account", uid, "total_browser_disconnects")
        _write_tracker(data)


def record_session_refresh(uid: str = "") -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        _increment_counter(data, "global", uid, "total_session_refreshes")
        if uid:
            _increment_counter(data, "account", uid, "total_session_refreshes")
        _write_tracker(data)


def record_slot_detection(uid: str = "") -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        _increment_counter(data, "global", uid, "total_slot_detections")
        if uid:
            _increment_counter(data, "account", uid, "total_slot_detections")
        _write_tracker(data)


def record_booking_attempt(uid: str = "") -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        _increment_counter(data, "global", uid, "total_booking_attempts")
        if uid:
            _increment_counter(data, "account", uid, "total_booking_attempts")
        _write_tracker(data)


def record_booking_success(uid: str = "") -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        _increment_counter(data, "global", uid, "total_booking_successes")
        if uid:
            _increment_counter(data, "account", uid, "total_booking_successes")
        _write_tracker(data)


def record_booking_failure(uid: str = "") -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        _increment_counter(data, "global", uid, "total_booking_failures")
        if uid:
            _increment_counter(data, "account", uid, "total_booking_failures")
        _write_tracker(data)


def record_recovery_time(uid: str = "", seconds: float = 0.0) -> None:
    with TRACKER_LOCK:
        data = _read_tracker()
        now = time.time()
        g = data.setdefault("global", _empty_global())
        existing = float(g.get("average_recovery_time_seconds") or 0.0)
        count = int(g.get("total_session_refreshes", 0) or 0)
        g["average_recovery_time_seconds"] = ((existing * max(count - 1, 0)) + float(seconds)) / max(count, 1)
        data["global"] = g
        if uid:
            acct = data.setdefault("accounts", {}).setdefault(uid, _empty_account())
            acct["average_recovery_time_seconds"] = float(seconds)
        _write_tracker(data)


def get_stats() -> dict:
    """Get current request statistics."""
    with TRACKER_LOCK:
        data = _read_tracker()
        now = time.time()
        while _request_timestamps and _request_timestamps[0] < now - RATE_WINDOW:
            _request_timestamps.popleft()
        if "global" not in data:
            data["global"] = _empty_global()
        data["global"]["requests_per_minute"] = len(_request_timestamps)
        return data


def get_global_rate() -> int:
    """Get current global requests per minute."""
    with TRACKER_LOCK:
        now = time.time()
        while _request_timestamps and _request_timestamps[0] < now - RATE_WINDOW:
            _request_timestamps.popleft()
        return len(_request_timestamps)


def reset() -> None:
    """Reset all counters."""
    with TRACKER_LOCK:
        _request_timestamps.clear()
        _write_tracker({"global": _empty_global(), "accounts": {}})

