"""Data models and session context representations for the API layer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.common.constants import BASE_URL, ApiErrorType


@dataclass
class AccountSessionContext:
    """Encapsulates the authenticated session state extracted from the browser.

    All dynamic per-account values live here and are never made global constants.
    """
    username: str
    primary_id: str
    appd: str
    cookies: Dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    is_reschedule: bool = False
    application_ids: List[str] = field(default_factory=list)
    proxy_url: Optional[str] = None
    base_url: str = BASE_URL
    # Per-account lock ensuring non-duplicate concurrent API queries
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def safe_username(self) -> str:
        """Sanitized username safe for logs."""
        if not self.username:
            return "unknown"
        if len(self.username) <= 4:
            return "***"
        return f"{self.username[:2]}***{self.username[-2:]}"


@dataclass
class ApiResponse:
    """Standardized result of an API operation with timing, error classification, and recovery decision."""
    success: bool
    status_code: int = 0
    data: Any = None
    error_type: Optional[ApiErrorType] = None
    error_message: Optional[str] = None
    latency_ms: float = 0.0
    response_type: str = "JSON"
    fallback_to_browser: bool = False
    retry_after_seconds: Optional[float] = None
    parsed_days_count: int = 0
    parsed_slots_count: int = 0


@dataclass
class EarlyMatchResult:
    """Result when an OFC or Consular schedule date qualifies early during polling."""
    city: str
    date: str
    token: Optional[str] = None
    is_consular: bool = False


@dataclass
class SlotMatch:
    """Represents an identified matching appointment date/slot within configured criteria."""
    city: str
    date: str
    token: Optional[str] = None
    available_entries: int = 1
    is_consular: bool = False


@dataclass
class PollResult:
    """Aggregated outcome of an independent polling run across target cities."""
    success: bool
    component: str = "MONITOR_OFC"
    days_by_city: Dict[str, Any] = field(default_factory=dict)
    tokens_by_city: Dict[str, str] = field(default_factory=dict)
    matches: List[SlotMatch] = field(default_factory=list)
    early_match: Optional[EarlyMatchResult] = None
    error_type: Optional[ApiErrorType] = None
    error_message: Optional[str] = None
    fallback_to_browser: bool = False
    needs_recovery: bool = False
    total_parsed_days: int = 0
    total_parsed_slots: int = 0
    summary: str = ""
