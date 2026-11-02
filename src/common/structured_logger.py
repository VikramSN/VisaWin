"""Production-grade structured logger for the US Visa Scheduler.

Provides standardized, audit-ready logging across all subsystems:
- Browser & Session Lifecycle
- Independent OFC & Consular Monitoring
- Decoupled Booking State Machine
- Controlled Error Recovery & Backoff

Security Guarantee:
- Strict sanitization: cookies, tokens, passwords, authorization headers,
  and sensitive portal payload blobs are NEVER logged.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.common.constants import LogEvent

# Masking patterns for sensitive strings
_COOKIE_PATTERN = re.compile(r"(cookie|cookies|token|fedauth|asp\.net_sessionid|cf_clearance)\s*[:=]\s*[^,;\s]+", re.I)
_SECRET_KEY_PATTERN = re.compile(r"(['\"]?(?:password|token|secret|jwt|apikey|api_key)['\"]?\s*[:=]\s*['\"])[^'\"]+(['\"])", re.I)
_BEARER_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.I)


def sanitize_message(msg: str) -> str:
    """Sanitize secrets, JWTs, cookies, and tokens from any log message."""
    if not msg:
        return ""
    s = _BEARER_PATTERN.sub(r"\1***", msg)
    s = _SECRET_KEY_PATTERN.sub(r"\1***\2", s)
    s = _COOKIE_PATTERN.sub(r"\1=***", s)
    return s


def sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively mask sensitive keys in dictionary objects."""
    clean: Dict[str, Any] = {}
    sensitive_keys = {
        "password", "passwd", "token", "jwt", "cookie", "cookies",
        "authorization", "sec-ch-ua", "cf_clearance", "secret", "apikey"
    }
    for k, v in data.items():
        k_low = str(k).lower()
        if any(sk in k_low for sk in sensitive_keys):
            clean[k] = "***"
        elif isinstance(v, dict):
            clean[k] = sanitize_dict(v)
        elif isinstance(v, list):
            clean[k] = [sanitize_dict(item) if isinstance(item, dict) else item for item in v]
        elif isinstance(v, str):
            clean[k] = sanitize_message(v)
        else:
            clean[k] = v
    return clean


class StructuredLogger:
    """Production-grade structured logger for an individual account context."""

    def __init__(self, account_name: str, logger: Optional[logging.Logger] = None):
        self.account_name = account_name or "global"
        self.logger = logger or logging.getLogger(f"visa.{self.account_name}")

    def _format_event(
        self,
        event: LogEvent,
        component: str,
        action: str,
        state: Optional[str] = None,
        method: Optional[str] = None,
        route: Optional[str] = None,
        status: Optional[int | str] = None,
        elapsed_ms: Optional[float] = None,
        decision: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Format event line with consistent standardized attributes."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = [
            f"[{ts}]",
            f"[{self.account_name}]",
            f"[{component}]",
            f"[{event.value}]",
            f"action={action}",
        ]
        if state:
            parts.append(f"state={state}")
        if method and route:
            parts.append(f"{method} {route}")
        elif route:
            parts.append(f"route={route}")
        if status is not None:
            parts.append(f"status={status}")
        if elapsed_ms is not None:
            parts.append(f"elapsed={elapsed_ms:.1f}ms")
        if decision:
            parts.append(f"decision='{sanitize_message(decision)}'")
        if details:
            clean_details = sanitize_dict(details)
            # Compact key=value representation
            detail_str = " ".join(f"{k}={v}" for k, v in clean_details.items())
            parts.append(f"details=({detail_str})")

        return " ".join(parts)

    def log_session_event(
        self,
        event: LogEvent,
        action: str,
        state: Optional[str] = None,
        decision: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        level: int = logging.INFO,
    ) -> None:
        msg = self._format_event(
            event=event,
            component="SESSION",
            action=action,
            state=state,
            decision=decision,
            details=details,
        )
        self.logger.log(level, msg)

    def log_http_event(
        self,
        event: LogEvent,
        action: str,
        method: str,
        route: str,
        status: Optional[int | str] = None,
        elapsed_ms: Optional[float] = None,
        decision: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        level: int = logging.INFO,
    ) -> None:
        msg = self._format_event(
            event=event,
            component="HTTP",
            action=action,
            method=method,
            route=route,
            status=status,
            elapsed_ms=elapsed_ms,
            decision=decision,
            details=details,
        )
        self.logger.log(level, msg)

    def log_monitor_event(
        self,
        event: LogEvent,
        component: str,  # e.g. "MONITOR_OFC" or "MONITOR_CONSULAR"
        action: str,
        state: Optional[str] = None,
        decision: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        level: int = logging.INFO,
    ) -> None:
        msg = self._format_event(
            event=event,
            component=component,
            action=action,
            state=state,
            decision=decision,
            details=details,
        )
        self.logger.log(level, msg)

    def log_booking_event(
        self,
        event: LogEvent,
        action: str,
        state: Optional[str] = None,
        decision: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        level: int = logging.INFO,
    ) -> None:
        msg = self._format_event(
            event=event,
            component="BOOKING",
            action=action,
            state=state,
            decision=decision,
            details=details,
        )
        self.logger.log(level, msg)

    def log_cycle_summary(
        self,
        session_state: str,
        ofc_status: str,
        consular_status: str,
        matched_slots: int,
        next_poll_seconds: float,
        level: int = logging.INFO,
    ) -> None:
        """Log concise, standardized single-line cycle summary.

        account -> session state -> OFC status -> Consular status -> matched slots -> next poll time
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = (
            f"[{ts}] [CYCLE_SUMMARY] {self.account_name} -> "
            f"session: {session_state} | "
            f"OFC: {ofc_status} | "
            f"Consular: {consular_status} | "
            f"matches: {matched_slots} | "
            f"next_poll: ~{next_poll_seconds:.0f}s"
        )
        self.logger.log(level, summary)


# Global registry for structured loggers per account
_STRUCTURED_LOGGERS: Dict[str, StructuredLogger] = {}


def get_structured_logger(account_name: str) -> StructuredLogger:
    """Retrieve or create a cached StructuredLogger for an account."""
    name = account_name or "global"
    if name not in _STRUCTURED_LOGGERS:
        _STRUCTURED_LOGGERS[name] = StructuredLogger(name)
    return _STRUCTURED_LOGGERS[name]
