"""Reliability-first architecture helpers for state machines, error classification,
backoff, circuit breakers, and deterministic coordination.

This module provides the core state machines and policies for:
1. Monitoring, Browser Health, and Booking subsystems.
2. Distinct error classification:
   NETWORK_UNAVAILABLE, RATE_LIMITED, AUTH_EXPIRED, PORTAL_BLOCKED,
   SLOT_GONE, BROWSER_BROKEN, BOOKING_REJECTED.
3. State models:
   HEALTHY -> DEGRADED -> COOLDOWN -> RECOVERING -> HEALTHY
   FAILED -> RESTART_SESSION -> VERIFY_HEALTH -> RESUME
4. Browser preflight states:
   BROWSER_CONNECTED -> PAGE_VALID -> EXPECTED_ROUTE -> AUTH_VALID ->
   SESSION_IDS_PRESENT -> BOOKING_READY
5. Trigger lifecycles:
   FRESH -> AGING -> STALE -> CANCELLED
6. Booking outcomes:
   FULLY_BOOKED, OFC_BOOKED_PENDING_CONSULAR, BOOKING_FAILED, SLOT_GONE, RECOVERY_REQUIRED
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Set, Tuple


# ── State Machine Enums ──────────────────────────────────────────────────


class Subsystem(str, Enum):
    MONITORING = "MONITORING"
    BROWSER_HEALTH = "BROWSER_HEALTH"
    BOOKING = "BOOKING"


class HealthState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


class SessionRecoveryState(str, Enum):
    FAILED = "FAILED"
    RESTART_SESSION = "RESTART_SESSION"
    VERIFY_HEALTH = "VERIFY_HEALTH"
    RESUME = "RESUME"


class ErrorClass(str, Enum):
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PORTAL_BLOCKED = "PORTAL_BLOCKED"
    SLOT_GONE = "SLOT_GONE"
    BROWSER_BROKEN = "BROWSER_BROKEN"
    BOOKING_REJECTED = "BOOKING_REJECTED"


class TriggerLifecycle(str, Enum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class PreflightSlotState(str, Enum):
    MONITOR_OBSERVED_SLOT = "MONITOR_OBSERVED_SLOT"
    CURRENT_SLOT_CONFIRMED = "CURRENT_SLOT_CONFIRMED"
    CURRENT_SLOT_VERIFICATION_UNAVAILABLE = "CURRENT_SLOT_VERIFICATION_UNAVAILABLE"
    CURRENT_SLOT_GONE = "CURRENT_SLOT_GONE"


class BrowserPreflightState(str, Enum):
    BROWSER_CONNECTED = "BROWSER_CONNECTED"
    PAGE_VALID = "PAGE_VALID"
    EXPECTED_ROUTE = "EXPECTED_ROUTE"
    AUTH_VALID = "AUTH_VALID"
    SESSION_IDS_PRESENT = "SESSION_IDS_PRESENT"
    BOOKING_READY = "BOOKING_READY"


class BookingOutcome(str, Enum):
    FULLY_BOOKED = "FULLY_BOOKED"
    OFC_BOOKED_PENDING_CONSULAR = "OFC_BOOKED_PENDING_CONSULAR"
    BOOKING_FAILED = "BOOKING_FAILED"
    SLOT_GONE = "SLOT_GONE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


# ── Configuration Values ─────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception:
        return default


RETRY_BASE_DELAY = _env_float("RETRY_BASE_DELAY_SECONDS", 2.0)
RETRY_MAX_DELAY = _env_float("RETRY_MAX_DELAY_SECONDS", 45.0)
RETRY_MAX_ATTEMPTS = _env_int("RETRY_MAX_ATTEMPTS", 3)

POLL_BASE_INTERVAL = _env_float("MONITOR_POLL_INTERVAL_SECONDS", 15.0)
POLL_JITTER = _env_float("MONITOR_POLL_JITTER_SECONDS", 2.0)

BOOKING_LOCK_TTL_SECONDS = _env_float("BOOKING_LOCK_TTL_SECONDS", 60.0)
REQUEST_TIMEOUT_SECONDS = _env_float("REQUEST_TIMEOUT_SECONDS", 25.0)
BROWSER_HEALTH_TIMEOUT_SECONDS = _env_float("BROWSER_HEALTH_TIMEOUT_SECONDS", 15.0)

# Bounded trigger freshness
TRIGGER_FRESH_MAX_AGE_SECONDS = _env_float("TRIGGER_FRESH_MAX_AGE_SECONDS", 25.0)
TRIGGER_STALE_MAX_AGE_SECONDS = _env_float("TRIGGER_STALE_MAX_AGE_SECONDS", 60.0)


# ── Time & Clock Utilities ───────────────────────────────────────────────


def now_utc_iso() -> str:
    """Return ISO-formatted UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def monotonic_time() -> float:
    """Monotonic clock for delay, age, and interval calculations."""
    return time.monotonic()


def calculate_trigger_age(
    detected_mono: float,
    current_mono: float | None = None,
) -> float:
    """Return elapsed seconds using the monotonic clock."""
    if current_mono is None:
        current_mono = time.monotonic()
    return max(0.0, current_mono - float(detected_mono))


def classify_trigger_lifecycle(
    age_seconds: float,
    fresh_limit: float = TRIGGER_FRESH_MAX_AGE_SECONDS,
    stale_limit: float = TRIGGER_STALE_MAX_AGE_SECONDS,
) -> TriggerLifecycle:
    """Classify trigger freshness based on age."""
    if age_seconds < fresh_limit:
        return TriggerLifecycle.FRESH
    if age_seconds <= stale_limit:
        return TriggerLifecycle.AGING
    return TriggerLifecycle.STALE


# ── Error Classification ─────────────────────────────────────────────────


def classify_error(exc: Any | None, *, default: ErrorClass = ErrorClass.NETWORK_UNAVAILABLE) -> ErrorClass:
    """Categorize exceptions, HTTP status codes, or strings into structured ErrorClass."""
    if exc is None:
        return default

    if isinstance(exc, int):
        if exc == 429:
            return ErrorClass.RATE_LIMITED
        if exc == 403:
            return ErrorClass.PORTAL_BLOCKED
        if exc in (500, 502, 503, 504, 520, 521, 522, 523, 524):
            return ErrorClass.NETWORK_UNAVAILABLE
        if 400 <= exc < 500:
            return ErrorClass.BOOKING_REJECTED
        return ErrorClass.NETWORK_UNAVAILABLE

    text = str(exc).lower()

    # Rate limiting
    if "429" in text or "too many requests" in text or "rate limit" in text or "retry-after" in text:
        return ErrorClass.RATE_LIMITED

    # Portal blocked / Cloudflare block
    if (
        "sorry, you have been blocked" in text
        or "cloudflare waf" in text
        or "waf block" in text
        or "attention required" in text
        or "access denied" in text
        or ("403" in text and "usvisascheduling" in text)
    ):
        return ErrorClass.PORTAL_BLOCKED

    # Auth expired / stale session
    if (
        "session expired" in text
        or "session_expired" in text
        or "b2clogin.com" in text
        or "login html" in text
        or "login required" in text
        or "unauthorized" in text
        or "401" in text
    ):
        return ErrorClass.AUTH_EXPIRED

    # Browser broken / disconnected
    if (
        "browser has been closed" in text
        or "target closed" in text
        or "page crashed" in text
        or "context was destroyed" in text
        or "frame was detached" in text
        or "connection closed" in text
        or "cdp" in text
        and "disconnected" in text
    ):
        return ErrorClass.BROWSER_BROKEN

    # Slot gone
    if (
        "slot gone" in text
        or "no bookable slot" in text
        or "no_package" in text
        or "no slots found" in text
        or "no dates" in text
    ):
        return ErrorClass.SLOT_GONE

    # Booking rejected by portal API
    if (
        "booking rejected" in text
        or "invalid appointment" in text
        or "already scheduled" in text
        or "limit reached" in text
    ):
        return ErrorClass.BOOKING_REJECTED

    # Network / Proxy transport unavailable
    if (
        "tunnel refused" in text
        or "522" in text
        or "connection refused" in text
        or "connection reset" in text
        or "timed out" in text
        or "timeout" in text
        or "proxy" in text
        or "socket" in text
        or "dns" in text
        or "server error" in text
        or "502" in text
        or "503" in text
        or "504" in text
    ):
        return ErrorClass.NETWORK_UNAVAILABLE

    return default


# ── Backoff & Retry Computations ─────────────────────────────────────────


@dataclass
class RetryPlan:
    delay: float
    should_retry: bool
    state: HealthState
    error_class: ErrorClass
    reason: str


def compute_backoff(
    error_class: ErrorClass | str,
    attempt: int,
    retry_after: float | None = None,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
) -> RetryPlan:
    """Compute bounded exponential backoff with Retry-After support."""
    if isinstance(error_class, str):
        try:
            error_class = ErrorClass(error_class)
        except ValueError:
            error_class = classify_error(error_class)

    # Server-directed Retry-After takes precedence
    if retry_after is not None and retry_after > 0:
        delay = min(float(retry_after) + 1.0, RETRY_MAX_DELAY * 2)
        should_retry = attempt <= max_attempts
        return RetryPlan(
            delay=delay,
            should_retry=should_retry,
            state=HealthState.COOLDOWN,
            error_class=error_class,
            reason="server_retry_after",
        )

    # Base backoff delays by error class
    if error_class == ErrorClass.RATE_LIMITED:
        base = max(RETRY_BASE_DELAY * 2.5, 10.0)
        state = HealthState.COOLDOWN
    elif error_class == ErrorClass.PORTAL_BLOCKED:
        base = max(RETRY_BASE_DELAY * 3.0, 15.0)
        state = HealthState.COOLDOWN
    elif error_class == ErrorClass.NETWORK_UNAVAILABLE:
        base = max(RETRY_BASE_DELAY, 3.0)
        state = HealthState.DEGRADED
    elif error_class == ErrorClass.BROWSER_BROKEN:
        base = 5.0
        state = HealthState.RECOVERING
    elif error_class == ErrorClass.AUTH_EXPIRED:
        base = 4.0
        state = HealthState.RECOVERING
    elif error_class in (ErrorClass.SLOT_GONE, ErrorClass.BOOKING_REJECTED):
        # Do not retry on definite business misses
        return RetryPlan(
            delay=0.0,
            should_retry=False,
            state=HealthState.HEALTHY,
            error_class=error_class,
            reason="terminal_outcome",
        )
    else:
        base = RETRY_BASE_DELAY
        state = HealthState.DEGRADED

    # Exponential backoff: base * 2^(attempt - 1) capped at RETRY_MAX_DELAY
    delay = min(base * (2.0 ** max(0, attempt - 1)), RETRY_MAX_DELAY)
    # Add modest jitter (0 - 15%) to avoid lock-step retries
    delay += random.uniform(0.0, delay * 0.15)

    should_retry = attempt <= max_attempts
    if not should_retry:
        state = HealthState.FAILED

    return RetryPlan(
        delay=delay,
        should_retry=should_retry,
        state=state,
        error_class=error_class,
        reason=error_class.value,
    )


def jittered_poll_interval(base_interval: float | None = None) -> float:
    """Return a jittered interval for legitimate polling spacing."""
    base = base_interval if base_interval is not None else POLL_BASE_INTERVAL
    if base <= 0:
        return 0.0
    jitter = random.uniform(0.0, max(0.0, POLL_JITTER))
    return max(1.0, base + jitter)


# ── Slot Identification & Deduplication ──────────────────────────────────


def slot_fingerprint(slot_or_city: dict | str | None, date_str: str | None = None) -> str:
    """Generate a deterministic fingerprint for a slot candidate.
    Supports either a slot dict: slot_fingerprint({'city': '...', 'date': '...'})
    or separate city and date arguments: slot_fingerprint('NEW DELHI', '2026-09-27').
    """
    if isinstance(slot_or_city, str):
        city = slot_or_city.strip().upper()
        d = str(date_str or "").strip()[:10]
        return f"{city}|{d}"
    if not isinstance(slot_or_city, dict):
        return ""
    city = str(slot_or_city.get("city") or slot_or_city.get("ofcCity") or slot_or_city.get("ofcPriorityCity") or "").strip().upper()
    d = str(slot_or_city.get("date") or slot_or_city.get("ofcDate") or slot_or_city.get("ofcStartDate") or "").strip()[:10]
    token = str(slot_or_city.get("phase1_token") or slot_or_city.get("token") or slot_or_city.get("phase1Token") or "").strip()
    return f"{city}|{d}|{token}" if token else f"{city}|{d}"


def booking_idempotency_key(account_uid: str, slot: dict | None) -> str:
    """Generate a deterministic idempotency key for an account and slot."""
    uid = str(account_uid or "").strip()
    fp = slot_fingerprint(slot)
    return f"{uid}:{fp}"


# ── Circuit Breaker ──────────────────────────────────────────────────────


class CircuitBreaker:
    """Thread-safe circuit breaker with exponential cooldown and failure thresholds."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        max_cooldown_seconds: float = 180.0,
        reset_after_seconds: float = 120.0,
    ):
        self.failure_threshold = max(1, failure_threshold)
        self.base_cooldown = max(0.01, cooldown_seconds)
        self.max_cooldown = max(self.base_cooldown, max_cooldown_seconds)
        self.reset_after = max(1.0, reset_after_seconds)
        self._lock = threading.RLock()
        self._failures = 0
        self._tripped_at = 0.0
        self._current_cooldown = self.base_cooldown
        self._state = HealthState.HEALTHY

    @property
    def state(self) -> HealthState:
        with self._lock:
            now = time.monotonic()
            if self._state == HealthState.COOLDOWN:
                if now - self._tripped_at >= self._current_cooldown:
                    # Transition from cooldown to recovering (probing)
                    self._state = HealthState.RECOVERING
            elif self._state in (HealthState.DEGRADED, HealthState.RECOVERING):
                if self._failures == 0 or (now - self._tripped_at >= self.reset_after):
                    self._state = HealthState.HEALTHY
                    self._failures = 0
                    self._current_cooldown = self.base_cooldown
            return self._state

    def allow_request(self) -> bool:
        """Return True if requests are allowed; False if circuit is open in COOLDOWN."""
        with self._lock:
            st = self.state
            return st != HealthState.COOLDOWN

    def record_success(self) -> None:
        """Reset failures and restore HEALTHY state."""
        with self._lock:
            self._failures = 0
            self._tripped_at = 0.0
            self._current_cooldown = self.base_cooldown
            self._state = HealthState.HEALTHY

    def record_failure(self, error_class: ErrorClass | str | None = None) -> HealthState:
        """Record a failure and potentially trip the breaker into COOLDOWN."""
        with self._lock:
            now = time.monotonic()
            self._failures += 1
            self._tripped_at = now

            if self._failures >= self.failure_threshold:
                self._state = HealthState.COOLDOWN
                # Exponential cooldown escalation
                consecutive_trips = max(1, self._failures - self.failure_threshold + 1)
                self._current_cooldown = min(self.base_cooldown * consecutive_trips, self.max_cooldown)
            else:
                self._state = HealthState.DEGRADED

            return self._state


# ── Account Health Tracker ───────────────────────────────────────────────


class AccountHealthTracker:
    """Manages an account's bounded lifecycle states without infinite loops.

    Valid transitions:
    HEALTHY -> DEGRADED -> COOLDOWN -> RECOVERING -> HEALTHY
    FAILED -> RESTART_SESSION -> VERIFY_HEALTH -> RESUME -> HEALTHY
    """

    def __init__(self, account_uid: str):
        self.account_uid = account_uid
        self._lock = threading.RLock()
        self.health_state = HealthState.HEALTHY
        self.session_recovery_state: Optional[SessionRecoveryState] = None
        self.consecutive_failures = 0
        self.recovery_attempts = 0
        self.max_recovery_attempts = 3
        self.cooldown_until = 0.0
        self.last_error_class: Optional[ErrorClass] = None
        self.last_error_message: str = ""
        self.last_transition_ts = time.time()
        self.circuit_breaker = CircuitBreaker()

    def mark_healthy(self) -> None:
        with self._lock:
            self.health_state = HealthState.HEALTHY
            self.session_recovery_state = None
            self.consecutive_failures = 0
            self.recovery_attempts = 0
            self.cooldown_until = 0.0
            self.circuit_breaker.record_success()
            self.last_transition_ts = time.time()

    def mark_failure(
        self,
        error: Any,
        cooldown_seconds: float = 0.0,
    ) -> HealthState:
        with self._lock:
            now = time.monotonic()
            err_class = classify_error(error)
            self.last_error_class = err_class
            self.last_error_message = str(error)
            self.consecutive_failures += 1
            self.last_transition_ts = time.time()

            self.circuit_breaker.record_failure(err_class)

            if cooldown_seconds > 0:
                self.cooldown_until = now + cooldown_seconds
                self.health_state = HealthState.COOLDOWN
                return self.health_state

            if self.health_state == HealthState.HEALTHY:
                self.health_state = HealthState.DEGRADED
            elif self.health_state == HealthState.DEGRADED:
                self.health_state = HealthState.COOLDOWN
                self.cooldown_until = now + 15.0
            elif self.health_state == HealthState.COOLDOWN:
                self.health_state = HealthState.RECOVERING
            elif self.health_state == HealthState.RECOVERING:
                self.health_state = HealthState.FAILED
                self.session_recovery_state = SessionRecoveryState.FAILED

            return self.health_state

    def start_session_recovery(self) -> Tuple[bool, SessionRecoveryState]:
        """Advance session recovery state machine.

        FAILED -> RESTART_SESSION -> VERIFY_HEALTH -> RESUME
        """
        with self._lock:
            if self.recovery_attempts >= self.max_recovery_attempts:
                self.session_recovery_state = SessionRecoveryState.FAILED
                self.health_state = HealthState.FAILED
                return False, SessionRecoveryState.FAILED

            self.recovery_attempts += 1
            self.health_state = HealthState.RECOVERING

            if self.session_recovery_state is None or self.session_recovery_state == SessionRecoveryState.FAILED:
                self.session_recovery_state = SessionRecoveryState.RESTART_SESSION
            elif self.session_recovery_state == SessionRecoveryState.RESTART_SESSION:
                self.session_recovery_state = SessionRecoveryState.VERIFY_HEALTH
            elif self.session_recovery_state == SessionRecoveryState.VERIFY_HEALTH:
                self.session_recovery_state = SessionRecoveryState.RESUME

            return True, self.session_recovery_state

    def can_perform_action(self) -> bool:
        """Check if account is healthy enough to perform an action."""
        with self._lock:
            now = time.monotonic()
            if self.health_state == HealthState.COOLDOWN:
                if now < self.cooldown_until:
                    return False
                self.health_state = HealthState.RECOVERING

            if self.health_state == HealthState.FAILED:
                return False

            return self.circuit_breaker.allow_request()


# ── Global Account Registry ──────────────────────────────────────────────

_account_trackers: Dict[str, AccountHealthTracker] = {}
_registry_lock = threading.Lock()


def get_account_tracker(account_uid: str) -> AccountHealthTracker:
    """Retrieve or create the singleton AccountHealthTracker for an account."""
    uid = str(account_uid or "global").strip()
    with _registry_lock:
        if uid not in _account_trackers:
            _account_trackers[uid] = AccountHealthTracker(uid)
        return _account_trackers[uid]


__all__ = [
    "Subsystem",
    "HealthState",
    "SessionRecoveryState",
    "ErrorClass",
    "TriggerLifecycle",
    "PreflightSlotState",
    "BrowserPreflightState",
    "BookingOutcome",
    "RetryPlan",
    "CircuitBreaker",
    "AccountHealthTracker",
    "classify_error",
    "compute_backoff",
    "jittered_poll_interval",
    "slot_fingerprint",
    "booking_idempotency_key",
    "now_utc_iso",
    "monotonic_time",
    "calculate_trigger_age",
    "classify_trigger_lifecycle",
    "get_account_tracker",
    "RETRY_BASE_DELAY",
    "RETRY_MAX_DELAY",
    "RETRY_MAX_ATTEMPTS",
    "POLL_BASE_INTERVAL",
    "POLL_JITTER",
    "BOOKING_LOCK_TTL_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "BROWSER_HEALTH_TIMEOUT_SECONDS",
    "TRIGGER_FRESH_MAX_AGE_SECONDS",
    "TRIGGER_STALE_MAX_AGE_SECONDS",
]
