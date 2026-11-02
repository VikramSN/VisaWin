"""Centralized configuration and constants for the US Visa Scheduler.

Contains all static and semi-static values used across the codebase:
- Portal routes and endpoint paths
- OFC and Consular location/city mappings
- Default API headers
- City normalization rules
- Timing, retry, and cooldown values
- Error classification types

Dynamic per-account values (primaryId, appd, applicationIds, session cookies,
tokens, request timestamps) are strictly excluded and managed dynamically.
"""

from enum import Enum
from typing import Dict, Final

# -----------------------------------------------------------------------------
# Base Portal URLs & Routes
# -----------------------------------------------------------------------------
BASE_URL: Final[str] = "https://www.usvisascheduling.com"
PORTAL_EN_US_URL: Final[str] = f"{BASE_URL}/en-US/"
SCHEDULE_PATH: Final[str] = "/en-US/ofc-schedule"
SCHEDULE_URL: Final[str] = f"{BASE_URL}{SCHEDULE_PATH}"
RESCHEDULE_PATH: Final[str] = "/en-US/reschedule"

CUSTOM_ACTIONS_ROUTE_PREFIX: Final[str] = "/en-US/custom-actions/?route="

# Custom action API routes
ROUTE_PAGE_VIEW: Final[str] = (
    "/api/v1/page-views/schedule/single-family-schedule-page-views"
)
ROUTE_QUERY_FAMILY_MEMBERS: Final[str] = (
    "/api/v1/schedule-group/query-family-members-ofc"
)
ROUTE_GET_OFC_SCHEDULE_DAYS: Final[str] = (
    "/api/v1/schedule-group/get-family-ofc-schedule-days"
)
ROUTE_GET_OFC_SCHEDULE_ENTRIES: Final[str] = (
    "/api/v1/schedule-group/get-family-ofc-schedule-entries"
)
ROUTE_SCHEDULE_OFC_APPOINTMENTS: Final[str] = (
    "/api/v1/schedule-group/schedule-ofc-appointments-for-family"
)
ROUTE_GET_CONSULAR_SCHEDULE_DAYS: Final[str] = (
    "/api/v1/schedule-group/get-family-consular-schedule-days"
)
ROUTE_GET_CONSULAR_SCHEDULE_ENTRIES: Final[str] = (
    "/api/v1/schedule-group/get-family-consular-schedule-entries"
)

# -----------------------------------------------------------------------------
# Fixed Headers & Visa Defaults
# -----------------------------------------------------------------------------
DEFAULT_API_HEADERS: Final[Dict[str, str]] = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

DEFAULT_VISA_CLASS: Final[str] = "all"

# -----------------------------------------------------------------------------
# Location GUID Maps
# -----------------------------------------------------------------------------
# OFC (Biometrics) city -> PostId UUID map
OFC_LOCATION_MAP: Final[Dict[str, str]] = {
    "CHENNAI": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
    "HYDERABAD": "436bf614-b0db-ec11-a7b4-001dd80234f6",
    "KOLKATA": "466bf614-b0db-ec11-a7b4-001dd80234f6",
    "MUMBAI": "486bf614-b0db-ec11-a7b4-001dd80234f6",
    "NEW DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
}

# Consular (Interview) city -> PostId UUID map
CONSULAR_LOCATION_MAP: Final[Dict[str, str]] = {
    "CHENNAI": "c86af614-b0db-ec11-a7b4-001dd80234f6",
    "HYDERABAD": "ae6af614-b0db-ec11-a7b4-001dd80234f6",
    "KOLKATA": "816af614-b0db-ec11-a7b4-001dd80234f6",
    "MUMBAI": "716af614-b0db-ec11-a7b4-001dd80234f6",
    "NEW DELHI": "e66af614-b0db-ec11-a7b4-001dd80234f6",
}

CITY_ALIASES: Final[Dict[str, str]] = {
    "DELHI": "NEW DELHI",
    "DELHI VAC": "NEW DELHI",
    "NEW DELHI VAC": "NEW DELHI",
    "MUMBAI VAC": "MUMBAI",
    "CHENNAI VAC": "CHENNAI",
    "HYDERABAD VAC": "HYDERABAD",
    "KOLKATA VAC": "KOLKATA",
}


def normalize_city(city: str) -> str:
    """Normalize city name to canonical uppercase form (e.g. 'DELHI' -> 'NEW DELHI')."""
    raw = str(city or "").strip().upper()
    return CITY_ALIASES.get(raw, raw)


# -----------------------------------------------------------------------------
# Default Timing, Backoff & Cooldowns
# -----------------------------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT: Final[float] = 30.0
DEFAULT_POLL_DELAY_SECONDS: Final[float] = 10.0
DEFAULT_COOLDOWN_MINUTES: Final[int] = 60
DEFAULT_FAILED_POLL_BACKOFF_MINUTES: Final[int] = 5
DEFAULT_GAP_MINUTES: Final[int] = 15
DEFAULT_PREVENT_IMMEDIATE_DAYS: Final[int] = 3

BACKOFF_INITIAL_SECONDS: Final[float] = 5.0
BACKOFF_MAX_SECONDS: Final[float] = 300.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
BACKOFF_JITTER_RATIO: Final[float] = 0.2
MAX_CONSECUTIVE_FAILURES: Final[int] = 5
MAX_CONSECUTIVE_429S: Final[int] = 3


# -----------------------------------------------------------------------------
# API Error Classification
# -----------------------------------------------------------------------------
class ApiErrorType(str, Enum):
    """Categorized error reasons for API operations."""
    AUTH_EXPIRED = "auth_expired"
    RATE_LIMITED = "rate_limited"
    BLOCKED_OR_CHALLENGE = "blocked_or_challenge"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    PAGE_LIMIT_EXCEEDED = "page_limit_exceeded"
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    NON_JSON_RESPONSE = "non_json_response"
    INVALID_SESSION = "invalid_session"
    BACKEND_ERROR = "backend_error"
    BOOKING_CONFLICT = "booking_conflict"
    SLOT_UNAVAILABLE = "slot_unavailable"


# -----------------------------------------------------------------------------
# Standardized Structured Logging Events
# -----------------------------------------------------------------------------
class LogEvent(str, Enum):
    """Standardized event codes for structured logging."""
    SESSION_STARTING = "SESSION_STARTING"
    SESSION_READY = "SESSION_READY"
    SESSION_HEALTH_CHECK = "SESSION_HEALTH_CHECK"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CLOUDFLARE_CHALLENGE_DETECTED = "CLOUDFLARE_CHALLENGE_DETECTED"
    HTTP_MONITOR_START = "HTTP_MONITOR_START"
    HTTP_REQUEST_SENT = "HTTP_REQUEST_SENT"
    HTTP_RESPONSE_RECEIVED = "HTTP_RESPONSE_RECEIVED"
    NON_JSON_RESPONSE = "NON_JSON_RESPONSE"
    OFC_POLL_START = "OFC_POLL_START"
    OFC_RESULT = "OFC_RESULT"
    CONSULAR_POLL_START = "CONSULAR_POLL_START"
    CONSULAR_RESULT = "CONSULAR_RESULT"
    SLOT_MATCHED = "SLOT_MATCHED"
    SLOT_NOT_FOUND = "SLOT_NOT_FOUND"
    BACKOFF_APPLIED = "BACKOFF_APPLIED"
    RATE_LIMITED_429 = "RATE_LIMITED_429"
    NETWORK_ERROR = "NETWORK_ERROR"
    BOOKING_CANDIDATE_CREATED = "BOOKING_CANDIDATE_CREATED"
    BOOKING_STARTED = "BOOKING_STARTED"
    BOOKING_STEP = "BOOKING_STEP"
    BOOKING_SUCCESS = "BOOKING_SUCCESS"
    BOOKING_FAILED = "BOOKING_FAILED"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    RECOVERY_COMPLETED = "RECOVERY_COMPLETED"


# -----------------------------------------------------------------------------
# Static Identifiers & Cache Resolution
# -----------------------------------------------------------------------------
from src.common.static_identifiers import (  # noqa: E402
    STATIC_ACCOUNT_IDENTIFIERS,
    get_account_identifiers,
    save_account_identifiers,
)


