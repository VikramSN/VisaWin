"""Booking executor helpers and browser preflight validation.

Implements strict browser preflight state verification:
BROWSER_CONNECTED -> PAGE_VALID -> EXPECTED_ROUTE -> AUTH_VALID ->
SESSION_IDS_PRESENT -> BOOKING_READY

Guarantees the browser is never sent into a booking transaction from an
invalid, blocked, or disconnected state.
"""

import asyncio
import logging
from typing import Any, Tuple

from patchright.async_api import Page

from src.auth.login import wait_for_waiting_room
from src.booking.cdp_client import ensure_on_portal
from src.common import portal_pacer
from src.common.reliability import (
    BrowserPreflightState,
    ErrorClass,
    Subsystem,
)

from src.common.constants import (
    PORTAL_EN_US_URL as BASE_URL,
    SCHEDULE_URL,
    normalize_city,
)

DEFAULT_OFC_CITY = "HYDERABAD"


async def safe_evaluate(
    page: Page,
    expression: str | Any,
    arg: Any = None,
    *,
    log: logging.Logger | None = None,
    timeout: float | None = None,
) -> Any:
    """Call page.evaluate with bounded error handling and no crashes on poisoned tabs."""
    try:
        if page is None:
            raise RuntimeError("page is None")
        if hasattr(page, "is_closed") and page.is_closed():
            raise RuntimeError("page is closed")
        if timeout is not None:
            try:
                return await asyncio.wait_for(page.evaluate(expression, arg), timeout)
            except TypeError:
                pass
        return await page.evaluate(expression, arg)
    except Exception as exc:
        if log is not None:
            try:
                log.warning(f"safe_evaluate failed: {exc}")
            except Exception:
                pass
        return None


async def check_for_page_limit(page: Page, customerName: str, log: logging.Logger) -> bool:
    try:
        limit_hit = await safe_evaluate(
            page,
            """() => {
                if (!document.body) return false;
                const txt = document.body.innerText.toLowerCase();
                return txt.includes('exceeded the limit for viewing this page')
                    || txt.includes('access limitation')
                    || txt.includes('prohibited conduct')
                    || txt.includes('attempt to view the calendar section was blocked');
            }""",
        )
        if limit_hit:
            msg = f"[FAIL] *Page View / Calendar Limit Exceeded*: `{customerName}` has exceeded viewing limits or hit Access Limitation."
            log.error(msg)
            return True
    except Exception:
        pass
    return False


async def verify_browser_preflight(
    page: Page,
    log: logging.Logger,
    *,
    expected_route: str = "",
    require_session_ids: bool = False,
) -> Tuple[bool, BrowserPreflightState, str]:
    """Strict browser preflight validation:

    BROWSER_CONNECTED -> PAGE_VALID -> EXPECTED_ROUTE -> AUTH_VALID ->
    SESSION_IDS_PRESENT -> BOOKING_READY

    Returns (is_ready, reached_state, failure_reason).
    """
    # 1. BROWSER_CONNECTED
    if page is None:
        return False, BrowserPreflightState.BROWSER_CONNECTED, "page_is_none"
    try:
        if hasattr(page, "is_closed") and page.is_closed():
            return False, BrowserPreflightState.BROWSER_CONNECTED, "page_is_closed"
        test_val = await safe_evaluate(page, "1 + 1", timeout=5.0)
        if test_val != 2:
            return False, BrowserPreflightState.BROWSER_CONNECTED, "evaluate_failed"
    except Exception as exc:
        return False, BrowserPreflightState.BROWSER_CONNECTED, f"browser_disconnected:{exc}"

    # 2. PAGE_VALID
    try:
        url = (page.url or "").strip().lower()
        if not url or url == "about:blank":
            return False, BrowserPreflightState.PAGE_VALID, "blank_page"
        if "chrome-error:" in url or "chromewebdata" in url:
            return False, BrowserPreflightState.PAGE_VALID, "chrome_error_page"
    except Exception as exc:
        return False, BrowserPreflightState.PAGE_VALID, f"invalid_page_url:{exc}"

    # 3. EXPECTED_ROUTE
    try:
        if "usvisascheduling.com" not in url:
            return False, BrowserPreflightState.EXPECTED_ROUTE, f"unexpected_domain:{url}"
        if expected_route and expected_route.lower() not in url:
            return False, BrowserPreflightState.EXPECTED_ROUTE, f"route_mismatch:{url}"
    except Exception as exc:
        return False, BrowserPreflightState.EXPECTED_ROUTE, f"route_check_failed:{exc}"

    # 4. AUTH_VALID
    try:
        title = (await page.title() or "").lower()
        if any(kw in title for kw in ("waiting room", "moment", "queue", "verify you are human", "attention required")):
            return False, BrowserPreflightState.AUTH_VALID, "in_waiting_room_or_challenge"

        # Check block page
        content = await safe_evaluate(page, "() => document.body ? document.body.innerText.toLowerCase() : ''", timeout=5.0) or ""
        if "sorry, you have been blocked" in content:
            return False, BrowserPreflightState.AUTH_VALID, "portal_block_page"

        if "sign in" in title or "b2clogin.com" in url:
            return False, BrowserPreflightState.AUTH_VALID, "auth_expired_redirect"
    except Exception as exc:
        return False, BrowserPreflightState.AUTH_VALID, f"auth_check_failed:{exc}"

    # 5. SESSION_IDS_PRESENT
    if require_session_ids:
        try:
            has_session = await safe_evaluate(
                page,
                """() => {
                    const storage = window.sessionStorage || {};
                    const applicantId = storage.getItem('ApplicantId') || '';
                    const appointmentId = storage.getItem('AppointmentId') || '';
                    const html = document.body ? document.body.innerHTML : '';
                    return Boolean(applicantId || appointmentId || html.includes('ApplicantId'));
                }""",
                timeout=5.0,
            )
            if not has_session:
                return False, BrowserPreflightState.SESSION_IDS_PRESENT, "missing_session_identifiers"
        except Exception as exc:
            return False, BrowserPreflightState.SESSION_IDS_PRESENT, f"session_ids_check_failed:{exc}"

    # 6. BOOKING_READY
    return True, BrowserPreflightState.BOOKING_READY, "ready"


async def heal_tab_for_booking(page: Page, log: logging.Logger, reason: str = "") -> bool:
    """Pre-flight check and bounded tab recovery.

    Never forces booking into a dead or blocked tab.
    """
    tag = f" ({reason})" if reason else ""

    # Run strict preflight check
    ok, state, fail_reason = await verify_browser_preflight(page, log)
    if ok:
        return True

    log.warning(f"Browser preflight state {state.value} failed{tag}: {fail_reason}")

    # If browser disconnected or blank, attempt one clean recovery
    if state in (BrowserPreflightState.BROWSER_CONNECTED, BrowserPreflightState.PAGE_VALID):
        try:
            log.info(f"Attempting controlled portal re-park{tag}...")
            await portal_pacer.pace(log, subsystem=Subsystem.BROWSER_HEALTH)
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.0)
        except Exception as exc:
            log.error(f"Browser connection could not be restored{tag}: {exc}")
            return False

    # Check for waiting room (bounded, passive wait)
    try:
        title = (await page.title() or "").lower()
        if any(k in title for k in ("waiting room", "moment", "queue", "verify you are human", "attention required")):
            log.warning(f"Cloudflare waiting room detected{tag} - waiting passively (<=45s)...")
            try:
                await wait_for_waiting_room(page, log, timeout_minutes=0.75)
            except Exception as wr_err:
                log.warning(f"Waiting room did not clear cleanly: {wr_err}")
                return False
    except Exception:
        pass

    # Final preflight verification
    ok2, state2, reason2 = await verify_browser_preflight(page, log)
    if not ok2:
        log.error(f"Browser still not ready for booking after recovery ({state2.value}: {reason2}). Aborting attempt.")
        return False

    return True


async def trigger_booking_ofc_only(page: Page, trigger: dict, log: logging.Logger) -> tuple[bool, dict]:
    customer_name = trigger.get("customer_name", "unknown")
    log.info(f"[DISPATCH] Triggering extension OFC booking for '{customer_name}'")
    from src.booking.one_shot import trigger_cached_ofc_booking
    return await trigger_cached_ofc_booking(page, trigger, log)


async def trigger_booking(page: Page, trigger: dict, log: logging.Logger) -> tuple[bool, dict]:
    customer_name = trigger.get("customer_name", "unknown")
    log.info(f"[DISPATCH] Triggering extension full booking workflow for '{customer_name}'")
    from src.booking.one_shot import trigger_cached_ofc_booking
    return await trigger_cached_ofc_booking(page, trigger, log)


async def trigger_booking_reschedule(page: Page, trigger: dict, log: logging.Logger) -> bool:
    customer_name = trigger.get("customer_name", "unknown")
    log.info(f"[DISPATCH] Triggering extension reschedule for '{customer_name}'")
    trigger["isReschedule"] = True
    from src.booking.one_shot import trigger_cached_ofc_booking
    success, _ = await trigger_cached_ofc_booking(page, trigger, log)
    return success


async def trigger_booking_sniper_consular_only(
    page: Page, trigger: dict, booked_ofc_date: str, log: logging.Logger
) -> tuple[bool, dict]:
    customer_name = trigger.get("customer_name", "unknown")
    log.info(f"[ACTION] Triggering in-page consular booking for '{customer_name}' (OFC booked on {booked_ofc_date})")
    if not await heal_tab_for_booking(page, log, reason="consular booking"):
        return False, {"status": "browser_unhealthy", "customer_name": customer_name}
    
    try:
        from src.booking.one_shot import attempt_consular_booking
        return await attempt_consular_booking(
            page=page,
            trigger=trigger,
            booked_ofc_date=booked_ofc_date,
            log=log,
            account_uid=trigger.get("account_uid", ""),
        )
    except Exception as e:
        log.error(f"[CONSULAR] Booking execution exception: {e}")
        return False, {"error": str(e), "waitingForConsular": True}


async def ensure_on_schedule_page(page: Page, log: logging.Logger, timeout_seconds: int = 30) -> bool:
    """Ensure the browser is situated in an active OFC schedule workflow context.
    
    If currently on the homepage, profile, or dashboard, enters the native schedule
    session in Dynamics 365, clicking 'Reschedule Appointment', 'Schedule Appointment',
    or 'Continue Application', falling back to direct navigation if buttons aren't visible.
    """
    try:
        cur_url = (page.url or "").lower()
        if "/ofc-schedule" in cur_url or "/schedule" in cur_url:
            log.info(f"[SCHEDULE-CONTEXT] Already on active schedule page: {page.url}")
            return True

        if "/profile" in cur_url:
            log.info(f"[SCHEDULE-CONTEXT] On profile page ({page.url}), navigating to portal root {BASE_URL}...")
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(2)
            except Exception as e:
                log.warning(f"[SCHEDULE-CONTEXT] Navigating from profile to portal failed: {e}")

        nav_clicked = False
        # Try dashboard buttons
        action_names = [
            "Reschedule Appointment",
            "Schedule Appointment",
            "Continue Application",
            "reschedule appointment",
            "schedule appointment",
            "continue application",
        ]
        for btn_text in action_names:
            try:
                locator = page.get_by_role("button", name=btn_text).first
                if not await locator.is_visible(timeout=800):
                    locator = page.get_by_role("link", name=btn_text).first
                if not await locator.is_visible(timeout=800):
                    locator = page.locator(f"text='{btn_text}'").first
                if await locator.is_visible(timeout=800):
                    log.info(f"[SCHEDULE-CONTEXT] Clicking dashboard action '{btn_text}' to enter schedule flow...")
                    await locator.click()
                    nav_clicked = True
                    break
            except Exception:
                pass

        if not nav_clicked:
            # Try anchor links containing ofc-schedule or schedule
            try:
                link = page.locator("a[href*='ofc-schedule'], a[href*='schedule']").first
                if await link.is_visible(timeout=1500):
                    href = await link.get_attribute("href")
                    log.info(f"[SCHEDULE-CONTEXT] Clicking schedule link '{href}'...")
                    await link.click()
                    nav_clicked = True
            except Exception:
                pass

        if not nav_clicked:
            log.info(f"[SCHEDULE-CONTEXT] No dashboard buttons found; direct navigating to {SCHEDULE_URL}...")
            try:
                await page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=25000)
            except Exception as nav_err:
                log.warning(f"[SCHEDULE-CONTEXT] Direct navigation warning: {nav_err}")

        # Wait for page to arrive on schedule context
        for _ in range(timeout_seconds):
            await asyncio.sleep(1)
            cur = (page.url or "").lower()
            if "/ofc-schedule" in cur or "/schedule" in cur:
                log.info(f"[SCHEDULE-CONTEXT] Successfully active on schedule page: {page.url}")
                await asyncio.sleep(2)
                return True
            if "waitingroom" in cur or "challenge" in cur:
                await wait_for_waiting_room(page, log, timeout_minutes=5)
                from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
                if await is_cloudflare_challenge(page):
                    await solve_cloudflare_challenge(page, log)

        log.warning(f"[SCHEDULE-CONTEXT] Could not reach schedule page within {timeout_seconds}s. Current URL: {page.url}")
        return False
    except Exception as e:
        log.warning(f"[SCHEDULE-CONTEXT] Error ensuring schedule page: {e}")
        return False


trigger_booking_reschedule_alias = trigger_booking_reschedule
trigger_booking_sniper_consular_only_alias = trigger_booking_sniper_consular_only

__all__ = [
    "verify_browser_preflight",
    "heal_tab_for_booking",
    "trigger_booking_ofc_only",
    "trigger_booking",
    "trigger_booking_reschedule",
    "trigger_booking_sniper_consular_only",
    "ensure_on_schedule_page",
    "check_for_page_limit",
    "safe_evaluate",
    "normalize_city",
    "SCHEDULE_URL",
    "BASE_URL",
]

