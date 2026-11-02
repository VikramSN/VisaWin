"""Browserless HTTP API client for the US Visa Scheduling portal.

Performs slot-monitoring and schedule API calls directly over HTTP using an
authenticated session context transferred from the browser login bootstrap.

Features:
- Robust error & challenge classification (200 JSON, 429 Rate Limited, Cloudflare Challenge, Non-JSON, Session Expired).
- Controlled signal to switch to in-browser execution when challenged without aggressive looping.
- Independent OFC and Consular polling routines.
- Production-grade structured event logging with strict sanitization of cookies and tokens.
- Per-account concurrency locks preventing simultaneous duplicate queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.api.models import (
    AccountSessionContext,
    ApiResponse,
    EarlyMatchResult,
    PollResult,
    SlotMatch,
)
from src.common.constants import (
    BASE_URL,
    CONSULAR_LOCATION_MAP,
    CUSTOM_ACTIONS_ROUTE_PREFIX,
    DEFAULT_API_HEADERS,
    DEFAULT_POLL_DELAY_SECONDS,
    DEFAULT_PREVENT_IMMEDIATE_DAYS,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_VISA_CLASS,
    OFC_LOCATION_MAP,
    ROUTE_GET_CONSULAR_SCHEDULE_DAYS,
    ROUTE_GET_CONSULAR_SCHEDULE_ENTRIES,
    ROUTE_GET_OFC_SCHEDULE_DAYS,
    ROUTE_GET_OFC_SCHEDULE_ENTRIES,
    ROUTE_PAGE_VIEW,
    ROUTE_QUERY_FAMILY_MEMBERS,
    ROUTE_SCHEDULE_OFC_APPOINTMENTS,
    ApiErrorType,
    LogEvent,
    normalize_city,
)
from src.common.structured_logger import get_structured_logger

log = logging.getLogger("visa_api_client")


class SessionExpiredError(Exception):
    """Raised when the remote portal session has expired (401, login HTML, etc.)."""
    pass


class VisaScheduleApiClient:
    """Async HTTP client for official US Visa Scheduling custom action endpoints."""

    def __init__(
        self,
        session_ctx: AccountSessionContext,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ):
        self.session_ctx = session_ctx
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self.slog = get_structured_logger(self.session_ctx.username)

    async def __aenter__(self) -> "VisaScheduleApiClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def connect(self) -> None:
        """Initialize the shared httpx.AsyncClient session with cookies and headers."""
        if self._client is not None and not self._client.is_closed:
            return

        headers = dict(DEFAULT_API_HEADERS)
        if self.session_ctx.user_agent:
            headers["User-Agent"] = self.session_ctx.user_agent
        headers["Referer"] = f"{self.session_ctx.base_url}/en-US/ofc-schedule"
        headers["Origin"] = self.session_ctx.base_url
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"

        # Explicitly attach all cookies to guarantee wire transmission
        if self.session_ctx.cookies:
            cookie_header = "; ".join(f"{k}={v}" for k, v in self.session_ctx.cookies.items())
            headers["Cookie"] = cookie_header

        self._client = httpx.AsyncClient(
            base_url=self.session_ctx.base_url,
            cookies=self.session_ctx.cookies,
            headers=headers,
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            proxy=self.session_ctx.proxy_url,
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _generate_cache_string(self) -> int:
        """Generate dynamic millisecond timestamp for cacheString query param."""
        return int(time.time() * 1000)

    def _classify_error(
        self,
        status_code: int,
        response_text: str,
        parsed_json: Optional[Dict[str, Any]],
    ) -> Tuple[ApiErrorType, str, bool]:
        """Classify HTTP status and payload content into a typed error category and fallback flag.

        Returns: (ApiErrorType, error_message, fallback_to_browser)
        """
        try:
            status_code = int(status_code)
        except (ValueError, TypeError):
            status_code = 0

        low_text = (response_text or "").lower()

        # 1. Rate Limiting (429)
        if status_code == 429:
            return ApiErrorType.RATE_LIMITED, "HTTP 429 Too Many Requests", False

        # 2. Block / Cloudflare Challenge / Waiting Room
        if any(kw in low_text for kw in [
            "sorry, you have been blocked",
            "attention required",
            "verify you are human",
            "just a moment",
            "turnstile",
            "challenges.cloudflare.com",
            "access denied",
        ]):
            return (
                ApiErrorType.CLOUDFLARE_CHALLENGE,
                "Cloudflare managed challenge or waiting room detected",
                True,
            )

        # 3. Daily page view limit
        if "exceeded the limit for viewing this page" in low_text:
            return ApiErrorType.PAGE_LIMIT_EXCEEDED, "Exceeded daily page limit", False

        # 4. Auth Expired (401 or login redirect HTML)
        if status_code in (401, 403) or "b2clogin.com" in low_text or "sign in" in low_text:
            return ApiErrorType.AUTH_EXPIRED, "Session expired or authentication required", False

        # 5. Server Errors (5xx)
        if status_code >= 500:
            return ApiErrorType.SERVER_ERROR, f"Server returned HTTP {status_code}", False

        # 6. Backend Portal Error inside JSON
        if parsed_json and isinstance(parsed_json, dict):
            if parsed_json.get("HasError"):
                err_str = parsed_json.get("ErrorString") or "Unknown backend error"
                return ApiErrorType.BACKEND_ERROR, f"Portal error: {err_str}", False
            err_str = str(parsed_json.get("ErrorString") or "")
            if err_str and err_str.strip() != "<br>":
                return ApiErrorType.BACKEND_ERROR, f"Portal error: {err_str}", False

        # 7. HTML response when expecting JSON
        if low_text.strip().startswith("<!doctype") or "<html" in low_text:
            return (
                ApiErrorType.NON_JSON_RESPONSE,
                "Received HTML page instead of expected JSON payload",
                True,
            )

        return ApiErrorType.BACKEND_ERROR, f"HTTP {status_code} error", False

    def _parse_json_payload(self, text: str) -> Optional[Dict[str, Any]]:
        """Safely parse JSON response, extracting the primary JSON object even if
        trailing whitespace, BOM, or HTML fragments are present.
        """
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass

        try:
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group(0))
        except Exception:
            pass

        return None

    def _extract_retry_after(self, resp: Optional[httpx.Response]) -> Optional[float]:
        if resp is None:
            return None
        ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        if ra:
            try:
                val = float(ra)
                if 0 < val < 600:
                    return val
            except (ValueError, TypeError):
                pass
        return None

    async def _post_custom_action(
        self,
        operation_name: str,
        route: str,
        payload: Dict[str, Any],
    ) -> ApiResponse:
        """Execute a custom action POST request with timing, sanitization, and structured logging."""
        if self._client is None or self._client.is_closed:
            await self.connect()

        cache_string = self._generate_cache_string()
        url = (
            f"{CUSTOM_ACTIONS_ROUTE_PREFIX}{route}"
            f"&appd={self.session_ctx.appd}"
            f"&cacheString={cache_string}"
        )

        encoded_body = f"parameters={urllib.parse.quote(json.dumps(payload))}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        self.slog.log_http_event(
            event=LogEvent.HTTP_REQUEST_SENT,
            action=operation_name,
            method="POST",
            route=route,
            details={"cacheString": cache_string},
        )

        start_time = time.perf_counter()
        status_code = 0
        raw_text = ""
        latency_ms = 0.0
        resp_obj: Optional[httpx.Response] = None

        try:
            resp_obj = await self._client.post(
                url,
                content=encoded_body.encode("utf-8"),
                headers=headers,
            )
            status_code = resp_obj.status_code
            latency_ms = (time.perf_counter() - start_time) * 1000
            raw_text = resp_obj.text
        except httpx.TimeoutException as tex:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.slog.log_http_event(
                event=LogEvent.NETWORK_ERROR,
                action=operation_name,
                method="POST",
                route=route,
                status="TIMEOUT",
                elapsed_ms=latency_ms,
                decision="Request timed out -> backoff and retry polite gap",
            )
            return ApiResponse(
                success=False,
                status_code=0,
                error_type=ApiErrorType.TIMEOUT,
                error_message=f"Request timeout: {tex}",
                latency_ms=latency_ms,
                response_type="TIMEOUT",
                fallback_to_browser=False,
            )
        except (httpx.ConnectError, httpx.NetworkError) as nex:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.slog.log_http_event(
                event=LogEvent.NETWORK_ERROR,
                action=operation_name,
                method="POST",
                route=route,
                status="CONNECT_ERROR",
                elapsed_ms=latency_ms,
                decision="Network connection failure -> apply backoff",
            )
            return ApiResponse(
                success=False,
                status_code=0,
                error_type=ApiErrorType.NETWORK_ERROR,
                error_message=f"Network error: {nex}",
                latency_ms=latency_ms,
                response_type="NETWORK_ERROR",
                fallback_to_browser=False,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.slog.log_http_event(
                event=LogEvent.NETWORK_ERROR,
                action=operation_name,
                method="POST",
                route=route,
                status=status_code,
                elapsed_ms=latency_ms,
                decision="Unexpected HTTP exception",
            )
            return ApiResponse(
                success=False,
                status_code=status_code,
                error_type=ApiErrorType.NETWORK_ERROR,
                error_message=str(exc),
                latency_ms=latency_ms,
                response_type="ERROR",
                fallback_to_browser=False,
            )

        # Parse JSON
        parsed_data = self._parse_json_payload(raw_text)
        is_json = parsed_data is not None
        resp_type = "JSON" if is_json else ("HTML" if "<html" in raw_text.lower() else "TEXT")
        retry_after = self._extract_retry_after(resp_obj)

        self.slog.log_http_event(
            event=LogEvent.HTTP_RESPONSE_RECEIVED,
            action=operation_name,
            method="POST",
            route=route,
            status=status_code,
            elapsed_ms=latency_ms,
            details={"response_type": resp_type},
        )

        # Check for errors & classify
        if status_code != 200 or not is_json:
            error_type, error_msg, fallback_browser = self._classify_error(status_code, raw_text, parsed_data)
            
            # Specific event emission
            if error_type == ApiErrorType.RATE_LIMITED:
                self.slog.log_http_event(
                    event=LogEvent.RATE_LIMITED_429,
                    action=operation_name,
                    method="POST",
                    route=route,
                    status=429,
                    elapsed_ms=latency_ms,
                    decision=f"HTTP 429 received -> easing into rate limit (retryAfter={retry_after})",
                )
            elif error_type == ApiErrorType.CLOUDFLARE_CHALLENGE:
                self.slog.log_http_event(
                    event=LogEvent.CLOUDFLARE_CHALLENGE_DETECTED,
                    action=operation_name,
                    method="POST",
                    route=route,
                    status=status_code,
                    elapsed_ms=latency_ms,
                    decision="HTTP monitor challenged -> switching to controlled browser session path",
                )
            elif error_type == ApiErrorType.NON_JSON_RESPONSE:
                self.slog.log_http_event(
                    event=LogEvent.NON_JSON_RESPONSE,
                    action=operation_name,
                    method="POST",
                    route=route,
                    status=status_code,
                    elapsed_ms=latency_ms,
                    decision="Non-JSON HTML response received -> switching to browser session path",
                )
            elif error_type == ApiErrorType.AUTH_EXPIRED:
                self.slog.log_session_event(
                    event=LogEvent.SESSION_EXPIRED,
                    action=operation_name,
                    decision="Auth expired or 401 received -> signaling session recovery",
                )

            return ApiResponse(
                success=False,
                status_code=status_code,
                data=raw_text if not is_json else parsed_data,
                error_type=error_type,
                error_message=error_msg,
                latency_ms=latency_ms,
                response_type=resp_type,
                fallback_to_browser=fallback_browser,
                retry_after_seconds=retry_after,
            )

        # Portal JSON error flags
        if parsed_data.get("HasError") or (
            parsed_data.get("ErrorString") and parsed_data.get("ErrorString").strip() != "<br>"
        ):
            error_type, error_msg, fallback_browser = self._classify_error(status_code, raw_text, parsed_data)
            return ApiResponse(
                success=False,
                status_code=status_code,
                data=parsed_data,
                error_type=error_type,
                error_message=error_msg,
                latency_ms=latency_ms,
                response_type=resp_type,
                fallback_to_browser=fallback_browser,
            )

        # Successful 200 with JSON payload
        parsed_days = len(parsed_data.get("ScheduleDays") or [])
        parsed_slots = len(parsed_data.get("ScheduleEntries") or [])

        return ApiResponse(
            success=True,
            status_code=status_code,
            data=parsed_data,
            latency_ms=latency_ms,
            response_type=resp_type,
            parsed_days_count=parsed_days,
            parsed_slots_count=parsed_slots,
        )

    # -------------------------------------------------------------------------
    # Core API Operations
    # -------------------------------------------------------------------------

    async def query_family_members(self) -> List[str]:
        """Fetch all applicant IDs for the family/group."""
        payload = {
            "primaryId": self.session_ctx.primary_id,
            "visaClass": DEFAULT_VISA_CLASS,
        }
        res = await self._post_custom_action(
            "query_family_members",
            ROUTE_QUERY_FAMILY_MEMBERS,
            payload,
        )

        app_ids: List[str] = []
        if res.success and isinstance(res.data, dict):
            members = res.data.get("Members") or []
            for m in members:
                aid = m.get("ApplicationID")
                if aid and aid not in app_ids:
                    app_ids.append(aid)

        if not app_ids:
            app_ids = [self.session_ctx.primary_id]

        self.session_ctx.application_ids = app_ids
        return app_ids

    async def get_ofc_schedule_days(
        self,
        city: str,
        post_id: str,
        is_reschedule: Optional[bool] = None,
    ) -> ApiResponse:
        """Fetch available OFC schedule days and the Phase-1 reservation token."""
        applications = self.session_ctx.application_ids or [self.session_ctx.primary_id]
        reschedule_flag = (
            is_reschedule if is_reschedule is not None else self.session_ctx.is_reschedule
        )

        payload = {
            "primaryId": self.session_ctx.primary_id,
            "applications": applications,
            "scheduleDayId": "",
            "scheduleEntryId": "",
            "postId": post_id,
            "isReschedule": "true" if reschedule_flag else "false",
        }

        return await self._post_custom_action(
            f"get_ofc_schedule_days({city})",
            ROUTE_GET_OFC_SCHEDULE_DAYS,
            payload,
        )

    async def get_ofc_schedule_entries(
        self,
        city: str,
        post_id: Optional[str],
        date_str: str,
        token: str,
        schedule_day_id: str = "",
        is_reschedule: Optional[bool] = None,
    ) -> ApiResponse:
        """Fetch time slot entries for an OFC date using the Phase-1 token."""
        reschedule_flag = (
            is_reschedule if is_reschedule is not None else self.session_ctx.is_reschedule
        )

        payload = {
            "primaryId": None,
            "applications": None,
            "scheduleDayId": schedule_day_id or None,
            "scheduleEntryId": "",
            "postId": post_id,
            "Token": token,
            "Date": f"{date_str}T00:00:00.000Z",
            "isReschedule": "true" if reschedule_flag else "false",
        }

        return await self._post_custom_action(
            f"get_ofc_schedule_entries({city}, {date_str})",
            ROUTE_GET_OFC_SCHEDULE_ENTRIES,
            payload,
        )

    async def get_consular_schedule_days(
        self,
        city: str,
        post_id: str,
        is_reschedule: Optional[bool] = None,
    ) -> ApiResponse:
        """Fetch available Consular schedule days for a city."""
        applications = self.session_ctx.application_ids or ([self.session_ctx.primary_id] if self.session_ctx.primary_id else None)
        reschedule_flag = (
            is_reschedule if is_reschedule is not None else self.session_ctx.is_reschedule
        )

        payload = {
            "primaryId": self.session_ctx.primary_id or None,
            "applications": applications,
            "scheduleDayId": None,
            "scheduleEntryId": "",
            "postId": post_id,
            "isReschedule": "true" if reschedule_flag else "false",
        }

        return await self._post_custom_action(
            f"get_consular_schedule_days({city})",
            ROUTE_GET_CONSULAR_SCHEDULE_DAYS,
            payload,
        )

    async def get_consular_schedule_entries(
        self,
        city: str,
        post_id: Optional[str],
        date_str: str,
        token: str,
        schedule_day_id: str = "",
        is_reschedule: Optional[bool] = None,
    ) -> ApiResponse:
        """Fetch Consular interview time slot entries for a date."""
        reschedule_flag = (
            is_reschedule if is_reschedule is not None else self.session_ctx.is_reschedule
        )

        payload = {
            "primaryId": None,
            "applications": None,
            "scheduleDayId": schedule_day_id or None,
            "scheduleEntryId": "",
            "postId": post_id,
            "Token": token,
            "Date": f"{date_str}T00:00:00.000Z",
            "isReschedule": "true" if reschedule_flag else "false",
        }

        return await self._post_custom_action(
            f"get_consular_schedule_entries({city}, {date_str})",
            ROUTE_GET_CONSULAR_SCHEDULE_ENTRIES,
            payload,
        )

    # -------------------------------------------------------------------------
    # Independent Polling Operations
    # -------------------------------------------------------------------------

    async def poll_ofc_dates(
        self,
        match_config: Optional[Dict[str, Any]] = None,
    ) -> PollResult:
        """Independently poll OFC availability across target cities.

        Protected by per-account request lock to prevent duplicate simultaneous polls.
        """
        async with self.session_ctx.request_lock:
            match_config = match_config or {}
            raw_cities = match_config.get("ofcCities", []) or list(OFC_LOCATION_MAP.keys())
            target_cities = set(normalize_city(c) for c in raw_cities)

            start_date = match_config.get("ofcStartDate", "")
            end_date = match_config.get("ofcEndDate", "")
            prevent_immediate = match_config.get("prevent_immediate", False)

            effective_start_date = start_date
            if prevent_immediate:
                dyn_start = datetime.now() + timedelta(days=DEFAULT_PREVENT_IMMEDIATE_DAYS)
                dyn_str = dyn_start.strftime("%Y-%m-%d")
                if not effective_start_date or effective_start_date < dyn_str:
                    effective_start_date = dyn_str

            self.slog.log_monitor_event(
                event=LogEvent.OFC_POLL_START,
                component="MONITOR_OFC",
                action="start_cycle",
                details={
                    "cities": list(target_cities),
                    "start": effective_start_date,
                    "end": end_date,
                },
            )

            # Ensure application IDs are populated
            if not self.session_ctx.application_ids:
                try:
                    await self.query_family_members()
                except Exception as e:
                    log.warning(f"Could not fetch family members: {e}")

            days_by_city: Dict[str, Any] = {}
            tokens_by_city: Dict[str, str] = {}
            matches: List[SlotMatch] = []
            early_match: Optional[EarlyMatchResult] = None
            total_days = 0
            fallback_needed = False
            html_errors = 0

            for city, post_id in OFC_LOCATION_MAP.items():
                norm_city = normalize_city(city)
                if norm_city not in target_cities:
                    continue

                res = await self.get_ofc_schedule_days(city, post_id)

                if res.success and isinstance(res.data, dict):
                    schedule_days = res.data.get("ScheduleDays") or []
                    token = res.data.get("Token") or ""
                    days_by_city[city] = schedule_days
                    total_days += len(schedule_days)
                    if token:
                        tokens_by_city[city] = token

                    # Check for matches in target range
                    for item in schedule_days:
                        date_val = str(
                            item.get("Date") if isinstance(item, dict) else item
                        )[:10]
                        if effective_start_date and end_date:
                            if effective_start_date <= date_val <= end_date:
                                match_item = SlotMatch(
                                    city=city,
                                    date=date_val,
                                    token=token,
                                    is_consular=False,
                                )
                                matches.append(match_item)
                                if not early_match:
                                    early_match = EarlyMatchResult(
                                        city=city,
                                        date=date_val,
                                        token=token,
                                        is_consular=False,
                                    )

                    self.slog.log_monitor_event(
                        event=LogEvent.OFC_RESULT,
                        component="MONITOR_OFC",
                        action="city_polled",
                        details={
                            "city": city,
                            "days_found": len(schedule_days),
                            "matches_in_range": len(matches),
                        },
                    )

                    # If qualified date found, stop remaining sweeps early to conserve quota
                    if early_match:
                        self.slog.log_monitor_event(
                            event=LogEvent.SLOT_MATCHED,
                            component="MONITOR_OFC",
                            action="early_match",
                            details={"city": early_match.city, "date": early_match.date},
                            decision="Early OFC match qualified -> stopping city sweeps",
                        )
                        break

                else:
                    err_msg = res.error_message or "Unknown error"
                    days_by_city[city] = {"error": err_msg}
                    if res.fallback_to_browser:
                        fallback_needed = True
                    if res.error_type == ApiErrorType.AUTH_EXPIRED or "HTML" in res.response_type:
                        html_errors += 1

                await asyncio.sleep(DEFAULT_POLL_DELAY_SECONDS)

            all_challenged = (html_errors >= len(target_cities)) and (len(target_cities) > 0)
            summary_str = f"OFC total_days={total_days}, matches={len(matches)}"

            if not matches and not fallback_needed and not all_challenged:
                self.slog.log_monitor_event(
                    event=LogEvent.SLOT_NOT_FOUND,
                    component="MONITOR_OFC",
                    action="cycle_complete",
                    details={"total_parsed_days": total_days},
                    decision="No qualifying OFC slots found in date range",
                )

            return PollResult(
                success=(not fallback_needed and not all_challenged),
                component="MONITOR_OFC",
                days_by_city=days_by_city,
                tokens_by_city=tokens_by_city,
                matches=matches,
                early_match=early_match,
                fallback_to_browser=fallback_needed,
                needs_recovery=all_challenged,
                total_parsed_days=total_days,
                summary=summary_str,
            )

    async def poll_consular_dates(
        self,
        match_config: Optional[Dict[str, Any]] = None,
    ) -> PollResult:
        """Independently poll Consular interview availability across target cities."""
        async with self.session_ctx.request_lock:
            match_config = match_config or {}
            raw_cities = match_config.get("consularCities", []) or list(CONSULAR_LOCATION_MAP.keys())
            target_cities = set(normalize_city(c) for c in raw_cities)

            start_date = match_config.get("consularStartDate", "")
            end_date = match_config.get("consularEndDate", "")

            self.slog.log_monitor_event(
                event=LogEvent.CONSULAR_POLL_START,
                component="MONITOR_CONSULAR",
                action="start_cycle",
                details={
                    "cities": list(target_cities),
                    "start": start_date,
                    "end": end_date,
                },
            )

            days_by_city: Dict[str, Any] = {}
            tokens_by_city: Dict[str, str] = {}
            matches: List[SlotMatch] = []
            early_match: Optional[EarlyMatchResult] = None
            total_days = 0
            fallback_needed = False
            html_errors = 0

            for city, post_id in CONSULAR_LOCATION_MAP.items():
                norm_city = normalize_city(city)
                if norm_city not in target_cities:
                    continue

                res = await self.get_consular_schedule_days(city, post_id)

                if res.success and isinstance(res.data, dict):
                    schedule_days = res.data.get("ScheduleDays") or []
                    token = res.data.get("Token") or ""
                    days_by_city[city] = schedule_days
                    total_days += len(schedule_days)
                    if token:
                        tokens_by_city[city] = token

                    for item in schedule_days:
                        date_val = str(
                            item.get("Date") if isinstance(item, dict) else item
                        )[:10]
                        if start_date and end_date:
                            if start_date <= date_val <= end_date:
                                match_item = SlotMatch(
                                    city=city,
                                    date=date_val,
                                    token=token,
                                    is_consular=True,
                                )
                                matches.append(match_item)
                                if not early_match:
                                    early_match = EarlyMatchResult(
                                        city=city,
                                        date=date_val,
                                        token=token,
                                        is_consular=True,
                                    )

                    self.slog.log_monitor_event(
                        event=LogEvent.CONSULAR_RESULT,
                        component="MONITOR_CONSULAR",
                        action="city_polled",
                        details={
                            "city": city,
                            "days_found": len(schedule_days),
                            "matches_in_range": len(matches),
                        },
                    )

                    if early_match:
                        self.slog.log_monitor_event(
                            event=LogEvent.SLOT_MATCHED,
                            component="MONITOR_CONSULAR",
                            action="early_match",
                            details={"city": early_match.city, "date": early_match.date},
                            decision="Early Consular match qualified -> stopping city sweeps",
                        )
                        break
                else:
                    err_msg = res.error_message or "Unknown error"
                    days_by_city[city] = {"error": err_msg}
                    if res.fallback_to_browser:
                        fallback_needed = True
                    if res.error_type == ApiErrorType.AUTH_EXPIRED or "HTML" in res.response_type:
                        html_errors += 1

                await asyncio.sleep(DEFAULT_POLL_DELAY_SECONDS)

            all_challenged = (html_errors >= len(target_cities)) and (len(target_cities) > 0)
            summary_str = f"Consular total_days={total_days}, matches={len(matches)}"

            if not matches and not fallback_needed and not all_challenged:
                self.slog.log_monitor_event(
                    event=LogEvent.SLOT_NOT_FOUND,
                    component="MONITOR_CONSULAR",
                    action="cycle_complete",
                    details={"total_parsed_days": total_days},
                    decision="No qualifying Consular slots found in date range",
                )

            return PollResult(
                success=(not fallback_needed and not all_challenged),
                component="MONITOR_CONSULAR",
                days_by_city=days_by_city,
                tokens_by_city=tokens_by_city,
                matches=matches,
                early_match=early_match,
                fallback_to_browser=fallback_needed,
                needs_recovery=all_challenged,
                total_parsed_days=total_days,
                summary=summary_str,
            )
