"""Cross-process portal pacing gate and rate limiter.

Provides rate budgets and process-safe minimum intervals between portal actions:
- Separate budgets for MONITORING, BROWSER_HEALTH, and BOOKING.
- Pacing gate ensures requests are spaced safely across all accounts.
- 429 escalation and circuit breaking.
- Bounded jitter to prevent lock-step requests without evasive patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

from src.common.reliability import (
    ErrorClass,
    HealthState,
    Subsystem,
    classify_error,
    compute_backoff,
    get_account_tracker,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

PACE_STATE_FILE = PROJECT_ROOT / "portal_pace.json"
PACE_LOCK_FILE = PROJECT_ROOT / "portal_pace.lock"

# Default minimum interval (seconds) between portal network actions
DEFAULT_GAP = float(os.getenv("PORTAL_PACE_GAP", "10.0"))
MONITOR_PACE_GAP = float(os.getenv("MONITOR_PACE_GAP", "15.0"))
HEALTH_PACE_GAP = float(os.getenv("HEALTH_PACE_GAP", "5.0"))
BOOKING_PACE_GAP = float(os.getenv("BOOKING_PACE_GAP", "1.5"))

POLL_JITTER_SECONDS = float(os.getenv("MONITOR_POLL_JITTER_SECONDS", "1.0"))
LOCK_MAX_AGE = 8.0
RATE_LIMIT_COOLDOWN = float(os.getenv("PORTAL_RATE_LIMIT_COOLDOWN", "60.0"))
PROXY_ERROR_COOLDOWN = float(os.getenv("PROXY_ERROR_COOLDOWN_SECONDS", "15.0"))
MAX_CONSECUTIVE_429S = int(os.getenv("MAX_CONSECUTIVE_429S", "3"))

# Global consecutive 429 counter
_consecutive_429_count = 0
_last_429_ts = 0.0


def _read_pace_state() -> dict:
    try:
        with open(PACE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last": 0.0, "subsystems": {}}


def _write_pace_state(data: dict) -> None:
    try:
        tmp = PACE_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(str(tmp), str(PACE_STATE_FILE))
    except Exception:
        try:
            with open(PACE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass


def _acquire_lock() -> bool:
    try:
        fd = os.open(str(PACE_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(PACE_LOCK_FILE) > LOCK_MAX_AGE:
                os.remove(PACE_LOCK_FILE)
                return _acquire_lock()
        except OSError:
            pass
        return False
    except OSError:
        return False


def _release_lock() -> None:
    try:
        os.remove(PACE_LOCK_FILE)
    except OSError:
        pass


async def pace(
    log: logging.Logger | None = None,
    min_gap: float | None = None,
    subsystem: Subsystem | str = Subsystem.MONITORING,
) -> None:
    """Block until the minimum pacing gap has elapsed.

    Subsystems have tailored budgets:
    - BOOKING: high priority, tighter gap
    - BROWSER_HEALTH: medium priority
    - MONITORING: standard paced gap
    """
    if isinstance(subsystem, str):
        try:
            subsystem = Subsystem(subsystem)
        except ValueError:
            subsystem = Subsystem.MONITORING

    if min_gap is not None:
        gap = float(min_gap)
    else:
        if subsystem == Subsystem.BOOKING:
            gap = BOOKING_PACE_GAP
        elif subsystem == Subsystem.BROWSER_HEALTH:
            gap = HEALTH_PACE_GAP
        else:
            gap = MONITOR_PACE_GAP

    gap = max(0.5, gap)
    jitter = min(max(POLL_JITTER_SECONDS, 0.0), 2.0)
    gap_with_jitter = gap + random.uniform(0.0, jitter)

    while True:
        if _acquire_lock():
            try:
                state = _read_pace_state()
                last_global = float(state.get("last", 0.0))
                sub_data = state.get("subsystems", {})
                last_sub = float(sub_data.get(subsystem.value, 0.0))
                now = time.time()

                remaining_global = (last_global + gap_with_jitter) - now
                remaining_sub = (last_sub + gap) - now
                remaining = max(remaining_global, remaining_sub)

                if remaining <= 0:
                    state["last"] = now
                    sub_data[subsystem.value] = now
                    state["subsystems"] = sub_data
                    _write_pace_state(state)
                    return
            finally:
                _release_lock()
            await asyncio.sleep(min(max(remaining, 0.1), 1.0))
        else:
            await asyncio.sleep(0.1)


async def ease_into_rate_limit(
    log: logging.Logger | None = None,
    failure_msg: str = "",
    retry_after: float | None = None,
    account_uid: str = "",
) -> None:
    """Cooldown after a 429 response, obeying server Retry-After."""
    global _consecutive_429_count, _last_429_ts

    _consecutive_429_count += 1
    _last_429_ts = time.time()

    if account_uid:
        tracker = get_account_tracker(account_uid)
        tracker.mark_failure(ErrorClass.RATE_LIMITED, cooldown_seconds=retry_after or RATE_LIMIT_COOLDOWN)

    if retry_after is not None and retry_after > 0:
        cooldown = float(retry_after) + 1.5
    else:
        cooldown = RATE_LIMIT_COOLDOWN

    if _consecutive_429_count > 1:
        cooldown = min(cooldown * _consecutive_429_count, 180.0)

    if log is not None:
        try:
            log.warning(
                f"Rate-limit hit ({failure_msg or '429'}). "
                f"Consecutive: {_consecutive_429_count}/{MAX_CONSECUTIVE_429S}. "
                f"Cooling down {cooldown:.0f}s."
            )
        except Exception:
            pass

    await asyncio.sleep(cooldown)
    await pace(log, min_gap=3.0, subsystem=Subsystem.BOOKING)


def reset_429_count() -> None:
    """Reset the consecutive 429 counter on successful transactions."""
    global _consecutive_429_count
    _consecutive_429_count = 0


def get_consecutive_429_count() -> int:
    return _consecutive_429_count


def is_rate_limit_escalation() -> bool:
    """True if consecutive 429s have exceeded the failure threshold."""
    return _consecutive_429_count >= MAX_CONSECUTIVE_429S


async def handle_proxy_error(
    log: logging.Logger | None = None,
    account_id: str = "",
    error_msg: str = "",
) -> None:
    """Handle proxy/network transport failures cleanly."""
    err_class = classify_error(error_msg or "proxy_error")
    plan = compute_backoff(err_class, 1)
    cooldown = max(plan.delay, 5.0)

    if account_id:
        tracker = get_account_tracker(account_id)
        tracker.mark_failure(err_class, cooldown_seconds=cooldown)

    if log is not None:
        try:
            log.warning(
                f"Transport/connectivity error for '{account_id}': {error_msg} "
                f"({err_class.value}). Cooldown {cooldown:.0f}s before retry."
            )
        except Exception:
            pass

    await asyncio.sleep(cooldown)


def safe_delay_jitter(base: float, jitter: float = 0.25) -> float:
    spread = max(0.0, float(jitter))
    if spread == 0:
        return float(base)
    return float(base) * (1.0 + random.uniform(0.0, spread))


__all__ = [
    "pace",
    "ease_into_rate_limit",
    "reset_429_count",
    "get_consecutive_429_count",
    "is_rate_limit_escalation",
    "handle_proxy_error",
    "safe_delay_jitter",
    "DEFAULT_GAP",
    "RATE_LIMIT_COOLDOWN",
    "PROXY_ERROR_COOLDOWN",
]