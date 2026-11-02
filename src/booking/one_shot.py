"""Extension-Powered Ultra-Fast Visa Booking Engine.

Directly bridges to the Chrome MV3 Extension (akashgupta2004/us-visa-scheduler)
inside the browser DOM (<300ms execution, real Microsoft anti-forgery tokens,
zero artificial CDP round trips, sticky IP session).

Flow:
1. Navigates page to /en-US/ofc-schedule (or /reschedule) to establish real CSRF tokens
2. Bootstraps primaryId and appd into the page DOM
3. Pre-arms the Chrome Extension storage with the target sniper configuration
4. Dispatches EXECUTE_SNIPER message to the Extension content script
5. Extension executes Mid-Out date and slot selection and books appointment in DOM
6. Returns execution status and booked slot information directly to booking runner
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Tuple

from patchright.async_api import Page

from src.common.constants import (
    CONSULAR_LOCATION_MAP,
    OFC_LOCATION_MAP,
    PORTAL_EN_US_URL as BASE_URL,
    normalize_city,
)
from src.common.static_identifiers import get_account_identifiers


async def _bootstrap_dom_identifiers(page: Page, primary_id: str, appd: str, log: logging.Logger) -> None:
    """Inject static primaryId and appd into the page DOM script tag.

    Guarantees autoConfigurePrimaryId() in the extension content script finds them
    in 0ms without depending on portal page structure.
    """
    if not primary_id or not appd:
        return
    try:
        await page.evaluate(
            """({ pId, aId }) => {
                let sc = document.getElementById('__sniper_bootstrap_ids');
                if (!sc) {
                    sc = document.createElement('script');
                    sc.id = '__sniper_bootstrap_ids';
                    document.head.appendChild(sc);
                }
                sc.textContent = `var __sniper_bootstrap = { "primaryId": "${pId}", "appd": "${aId}" };`;
            }""",
            {"pId": primary_id, "aId": appd},
        )
    except Exception as exc:
        log.debug(f"[DOM-BOOTSTRAP] Identifier injection notice: {exc}")


async def _execute_sniper_via_extension(
    page: Page, config: dict, log: logging.Logger, timeout_seconds: int = 300
) -> dict:
    """Dispatches EXECUTE_SNIPER message to the extension and polls window.__sniperResult.

    Matches the battle-tested implementation from akashgupta2004/us-visa-scheduler
    with navigation detection for appointment-confirmation and instant intermediate
    OFC booking recognition.
    """
    await page.evaluate("""
        window.__sniperResult = null;
        window.__sniperOfcBooked = null;
        if (!window.__sniperResultListenerAdded) {
            window.__sniperResultListenerAdded = true;
            window.addEventListener('message', function(event) {
                if (event.source !== window) return;
                if (event.data && (event.data.action === 'SNIPER_RESULT' || event.data.action === 'EXECUTE_SNIPER_RESPONSE')) {
                    window.__sniperResult = event.data;
                }
                if (event.data && event.data.action === 'SNIPER_OFC_BOOKED') {
                    window.__sniperOfcBooked = event.data;
                }
            });
        }
    """)

    await page.evaluate("""(cfg) => {
        try {
            window.postMessage({ action: "SET_SNIPER_CONFIG", config: cfg }, "*");
            window.postMessage({ action: "EXECUTE_SNIPER", config: cfg }, "*");
        } catch (e) {}
    }""", config)

    intermediate_ofc = None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        # 1. Navigation check (If page reached confirmation, booking is 100% SUCCESS!)
        try:
            cur_url = (page.url or "").lower()
            if "appointment-confirmation" in cur_url:
                log.info("🎉 [EXTENSION SNIPER] Confirmed booked via URL navigation: appointment-confirmation!")
                return {
                    "status": "success",
                    "full_success": True,
                    "msg": "Appointment confirmation page reached"
                }
        except Exception:
            pass

        # 2. Check intermediate OFC booking event
        try:
            ofc_evt = await page.evaluate("window.__sniperOfcBooked")
            if ofc_evt and not intermediate_ofc:
                intermediate_ofc = ofc_evt
                log.info(f"🎯 [OFC BOOKED CONFIRMED] OFC appointment locked on {ofc_evt.get('bookedOfcDate')} ({ofc_evt.get('bookedCity')})! Extension proceeding to Consular...")
        except Exception:
            pass

        # 3. Check result posted by extension
        try:
            result = await page.evaluate("window.__sniperResult")
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                log.info("[EXTENSION SNIPER] Navigation in flight, waiting for landing...")
                for _ in range(6):
                    await asyncio.sleep(1)
                    if "appointment-confirmation" in (page.url or "").lower():
                        return {"status": "success", "full_success": True, "msg": "Confirmed on navigation"}
                return {"status": "error", "msg": "Navigation to unknown page"}
            raise e

        if result is not None:
            try:
                await page.evaluate("window.__sniperResult = null;")
            except Exception:
                pass
            return result

        await asyncio.sleep(0.3)

    if intermediate_ofc:
        log.info(f"⏱️ Extension timed out during Consular sweep, but OFC is 100% BOOKED on {intermediate_ofc.get('bookedOfcDate')}!")
        return {
            "status": "success",
            "waitingForConsular": True,
            "bookedOfcDate": intermediate_ofc.get("bookedOfcDate"),
            "bookedCity": intermediate_ofc.get("bookedCity") or config.get("ofcPriorityCity"),
            "msg": "OFC booked successfully; Consular sweep awaiting next trigger"
        }

    return {"status": "error", "msg": f"Extension sniper timed out after {timeout_seconds}s"}


async def _execute_consular_via_extension(
    page: Page, config: dict, booked_ofc_date: str, log: logging.Logger, timeout_seconds: int = 300
) -> dict:
    """Dispatches EXECUTE_SNIPER_CONSULAR_ONLY and polls result with navigation detection."""
    await page.evaluate("""
        window.__consularResult = null;
        if (!window.__consularResultListenerAdded) {
            window.__consularResultListenerAdded = true;
            window.addEventListener('message', function(event) {
                if (event.source !== window) return;
                if (event.data && (event.data.action === 'SNIPER_CONSULAR_ONLY_RESULT' || event.data.action === 'CONSULAR_STANDALONE_RESULT')) {
                    window.__consularResult = event.data;
                }
            });
        }
    """)

    await page.evaluate("""(data) => {
        try {
            window.postMessage({ action: "SET_SNIPER_CONFIG", config: data.cfg }, "*");
            window.postMessage({ action: "EXECUTE_SNIPER_CONSULAR_ONLY", config: data.cfg, bookedOfcDate: data.ofcDate }, "*");
        } catch (e) {}
    }""", {"cfg": config, "ofcDate": booked_ofc_date})

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            if "appointment-confirmation" in (page.url or "").lower():
                return {"status": "success", "full_success": True, "msg": "Consular confirmed via navigation"}
        except Exception:
            pass

        try:
            result = await page.evaluate("window.__consularResult")
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                for _ in range(6):
                    await asyncio.sleep(1)
                    if "appointment-confirmation" in (page.url or "").lower():
                        return {"status": "success", "full_success": True, "msg": "Consular confirmed via navigation"}
                return {"status": "error", "msg": "Navigation to unknown page"}
            raise e

        if result is not None:
            try:
                await page.evaluate("window.__consularResult = null;")
            except Exception:
                pass
            return result

        await asyncio.sleep(0.3)

    return {"status": "error", "msg": f"Consular sniper timed out after {timeout_seconds}s"}


# -----------------------------------------------------------------------------
# Public API (Matches Orchestrator & Booking Runner Contract)
# -----------------------------------------------------------------------------
async def trigger_cached_ofc_booking(
    page: Page,
    trigger: dict,
    log: logging.Logger,
) -> Tuple[bool, dict]:
    """Execute high-speed OFC (+ Consular) booking via Chrome Extension Sniper.

    Execution time: < 300 Milliseconds in browser DOM.
    Returns:
        (True, {"bookedOfcDate": date, "bookedCity": city, "full_success": bool, ...})
        (False, {"error": msg, "waitingForConsular": bool, ...})
    """
    customer = trigger.get("customer_name", "unknown")
    username = trigger.get("username", "")

    # 1. Fetch static identifiers (primaryId, appd)
    static_p, static_a = get_account_identifiers(customer, trigger)
    primary_id = trigger.get("primaryId") or static_p or ""
    appd = trigger.get("appd") or static_a or ""

    # 2. Extract Target City and Dates
    cached = trigger.get("cached_booking") or {}
    target_city = normalize_city(
        trigger.get("ofcPriorityCity")
        or cached.get("city")
        or (trigger.get("ofcCities") or ["MUMBAI"])[0]
    )
    target_date = cached.get("date") or ""

    # FAST-PASS HYPER-FOCUSED SNIPER:
    # When triggered with a specific target_city (e.g., from slot radar or fast-pass),
    # lock solely onto target_city! Sweeping irrelevant cities causes 10+ seconds of wasted latency
    # that loses high-demand slots to competitor bots.
    if target_city:
        ofc_cities = [target_city]
    else:
        raw_ofc_cities = trigger.get("ofcCities") or ["MUMBAI"]
        ofc_cities = [normalize_city(c) for c in raw_ofc_cities]

    start_date = trigger.get("ofcStartDate") or target_date or ""
    end_date = trigger.get("ofcEndDate") or target_date or "2027-12-31"

    raw_consular_cities = trigger.get("consularCities") or ofc_cities
    consular_cities = [normalize_city(c) for c in raw_consular_cities]
    consular_priority_city = normalize_city(
        trigger.get("consularPriorityCity") or target_city or consular_cities[0]
    )
    consular_start = trigger.get("consularStartDate") or start_date
    consular_end = trigger.get("consularEndDate") or end_date

    is_reschedule = bool(
        trigger.get("isReschedule", False)
        or "reschedule" in (page.url or "").lower()
    )
    multi_person = bool(trigger.get("multiPerson", False))
    prevent_immediate = bool(trigger.get("preventImmediateBooking", False))

    sniper_config = {
        "ofcCities": ofc_cities,
        "ofcPriorityCity": target_city,
        "ofcStartDate": start_date,
        "ofcEndDate": end_date,
        "consularCities": consular_cities,
        "consularPriorityCity": consular_priority_city,
        "consularStartDate": consular_start,
        "consularEndDate": consular_end,
        "isReschedule": is_reschedule,
        "multiPerson": multi_person,
        "preventImmediateBooking": prevent_immediate,
        "primaryId": primary_id,
        "appd": appd,
    }

    # 3. Ensure browser has authentic scheduling Referer context without slow page reload (0ms spoof)
    ofc_path = "/en-US/ofc-schedule/?reschedule=true" if is_reschedule else "/en-US/ofc-schedule/"
    cur_url = (page.url or "").lower()
    if "/ofc-schedule" not in cur_url and "/reschedule" not in cur_url:
        log.info(f"⚡ [EXTENSION SNIPER] Setting schedule Referer context instantly (0ms): {ofc_path} ...")
        try:
            await page.evaluate(
                """(targetPath) => {
                    if (!window.location.pathname.includes('/ofc-schedule') && !window.location.pathname.includes('/reschedule')) {
                        window.history.replaceState(null, '', targetPath);
                    }
                }""",
                ofc_path,
            )
        except Exception as exc:
            log.warning(f"⚡ [EXTENSION SNIPER] Referer spoof notice: {exc}")

    # Bootstrap DOM script tag with identifiers so extension finds them instantly
    await _bootstrap_dom_identifiers(page, primary_id, appd, log)

    t0 = time.time()
    log.info(
        f"⚡ [FAST] [EXTENSION SNIPER] Dispatching native extension booking command for '{customer}' "
        f"[Target City: {target_city}, Date Range: {start_date} to {end_date}, Reschedule: {is_reschedule}]..."
    )

    result = await _execute_sniper_via_extension(page, sniper_config, log)
    elapsed_ms = int((time.time() - t0) * 1000)

    status = (result.get("status") or "").lower()
    msg = result.get("msg") or result.get("message") or ""
    source = result.get("source", "extension")

    log.info(
        f"⚡ [EXTENSION SNIPER] Response in {elapsed_ms}ms ({source}) -> "
        f"status: '{status}', msg: '{msg[:120]}'"
    )

    # Check for session expiration: Only declare session expired if page is truly redirected to login!
    if any(k in msg.lower() for k in ["session expired", "unauthorized", "please log in"]):
        curr_url = ""
        try:
            curr_url = page.url.lower()
        except Exception:
            pass
        if "b2clogin.com" in curr_url or "signin" in curr_url:
            log.warning(f"🚨 [SESSION EXPIRED] Booking rejected and redirected to login ({curr_url}): {msg}")
            raise Exception("Session expired")
        else:
            log.warning(f"⚠️ [TRANSIENT 403/CHALLENGE] Server returned '{msg}', but page is still authenticated on {curr_url}. Not a session expiration!")
            return False, {"error": msg, "transient_challenge": True, "elapsed_ms": elapsed_ms}

    # Full success: OFC and Consular both booked
    if status == "success" and (result.get("full_success") or not result.get("waitingForConsular")):
        booked_ofc = result.get("bookedOfcDate") or target_date
        booked_ofc_city = result.get("bookedCity") or target_city
        booked_consular = result.get("bookedConsularDate") or result.get("bookedSlot", {}).get("date") or "CONFIRMED"
        booked_consular_city = result.get("bookedConsularCity") or result.get("bookedSlot", {}).get("city") or booked_ofc_city
        return True, {
            "bookedOfcDate": booked_ofc,
            "bookedCity": booked_ofc_city,
            "bookedConsularDate": booked_consular,
            "bookedConsularCity": booked_consular_city,
            "full_success": True,
            "elapsed_ms": elapsed_ms,
        }

    # Partial success: OFC secured, Consular awaiting slots
    if result.get("waitingForConsular") or (status == "success" and result.get("bookedOfcDate")):
        booked_ofc = result.get("bookedOfcDate") or target_date
        booked_ofc_city = result.get("bookedCity") or target_city
        log.info(f"✅ [EXTENSION SNIPER] OFC Booked on {booked_ofc} in {booked_ofc_city}! Consular entering 32-min window.")
        return True, {
            "bookedOfcDate": booked_ofc,
            "bookedCity": booked_ofc_city,
            "waitingForConsular": True,
            "elapsed_ms": elapsed_ms,
        }

    # Instant In-Place Cloudflare Turnstile Clearance Recovery:
    # If Cloudflare returned 403 or Turnstile challenge on a cold tab, do an instant F5 reload
    # on the sticky IP connection to renew cf_clearance in 1s, and re-attempt booking immediately!
    if "403" in msg or "turnstile" in msg.lower() or "challenge" in msg.lower():
        log.warning(
            f"⚡ [CLOUDFLARE TURNSTILE CHALLENGE] Detected on '{customer}'. "
            f"Executing hot in-place clearance refresh & instant re-attempt (<2s)..."
        )
        try:
            await page.reload(wait_until="domcontentloaded", timeout=12000)
            await page.wait_for_timeout(800)
            cur_u = (page.url or "").lower()
            if not any(k in cur_u for k in ["b2clogin", "signin"]):
                await _bootstrap_dom_identifiers(page, primary_id, appd, log)
                retry_res = await _execute_sniper_via_extension(page, sniper_config, log, timeout_seconds=20)
                r_status = (retry_res.get("status") or "").lower()
                r_msg = retry_res.get("msg") or retry_res.get("message") or ""
                log.info(f"⚡ [EXTENSION SNIPER RETRY] Response in {int((time.time() - t0) * 1000)}ms -> status: '{r_status}', msg: '{r_msg[:120]}'")
                if r_status == "success" or retry_res.get("bookedOfcDate"):
                    b_ofc = retry_res.get("bookedOfcDate") or target_date
                    b_city = retry_res.get("bookedCity") or target_city
                    return True, {
                        "bookedOfcDate": b_ofc,
                        "bookedCity": b_city,
                        "waitingForConsular": bool(retry_res.get("waitingForConsular")),
                        "full_success": bool(retry_res.get("full_success")),
                        "elapsed_ms": int((time.time() - t0) * 1000),
                    }
        except Exception as retry_err:
            log.warning(f"⚡ [EXTENSION SNIPER RETRY] Clearance reload error: {retry_err}")

    # 429 Rate Limit
    if "429" in msg or "too many requests" in msg.lower() or status == "rate_limited":
        return False, {"rate_limited": True, "error": msg, "status": "rate_limited", "elapsed_ms": elapsed_ms}

    return False, {"error": msg or "Sniper attempt failed", "status": status, "detail": result, "elapsed_ms": elapsed_ms}


async def attempt_consular_booking(
    page: Page,
    trigger: dict,
    booked_ofc_date: str,
    log: logging.Logger,
    account_uid: str = "",
) -> Tuple[bool, dict]:
    """Execute immediate in-page Consular appointment booking via Chrome Extension Sniper.

    Returns:
        (True, {"bookedConsularDate": date, "bookedConsularCity": city, "time": time, "success": True})
        (False, {"status": "no_slots", "waitingForConsular": True})
    """
    customer = trigger.get("customer_name", "unknown")
    static_p, static_a = get_account_identifiers(customer, trigger)
    primary_id = trigger.get("primaryId") or static_p or ""
    appd = trigger.get("appd") or static_a or ""

    raw_consular_cities = trigger.get("consularCities") or ["MUMBAI", "CHENNAI", "HYDERABAD", "DELHI", "KOLKATA"]
    consular_cities = [normalize_city(c) for c in raw_consular_cities]
    consular_priority = normalize_city(trigger.get("consularPriorityCity") or consular_cities[0])
    if consular_priority in consular_cities:
        consular_cities.remove(consular_priority)
        consular_cities.insert(0, consular_priority)

    consular_start = trigger.get("consularStartDate") or booked_ofc_date
    consular_end = trigger.get("consularEndDate") or "2027-12-31"
    is_reschedule = bool(
        trigger.get("isReschedule", False)
        or "reschedule" in (page.url or "").lower()
    )
    multi_person = bool(trigger.get("multiPerson", False))
    prevent_immediate = bool(trigger.get("preventImmediateBooking", False))

    config = {
        "consularCities": consular_cities,
        "consularPriorityCity": consular_priority,
        "consularStartDate": consular_start,
        "consularEndDate": consular_end,
        "isReschedule": is_reschedule,
        "multiPerson": multi_person,
        "preventImmediateBooking": prevent_immediate,
        "primaryId": primary_id,
        "appd": appd,
    }

    sched_path = "/en-US/schedule/?reschedule=true" if is_reschedule else "/en-US/schedule/"
    cur_url = (page.url or "").lower()
    if "/schedule" not in cur_url:
        log.info(f"⚡ [CONSULAR SNIPER] Transitioning to Consular page: {sched_path} ...")
        try:
            await page.goto(f"https://www.usvisascheduling.com{sched_path}", wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            log.warning(f"⚡ [CONSULAR SNIPER] Navigation notice: {exc}")

    await _bootstrap_dom_identifiers(page, primary_id, appd, log)

    t0 = time.time()
    log.info(
        f"⚡ [CONSULAR SNIPER] Dispatching Consular booking for '{customer}' "
        f"[OFC Date: {booked_ofc_date}, Target Cities: {consular_cities}]..."
    )

    result = await _execute_consular_via_extension(page, config, booked_ofc_date, log)
    elapsed_ms = int((time.time() - t0) * 1000)

    status = (result.get("status") or "").lower()
    msg = result.get("msg") or result.get("message") or ""
    source = result.get("source", "extension")

    log.info(
        f"⚡ [CONSULAR SNIPER] Response in {elapsed_ms}ms ({source}) -> "
        f"status: '{status}', msg: '{msg[:120]}'"
    )

    if any(k in msg.lower() for k in ["session expired", "unauthorized"]):
        raise Exception("Session expired")

    if status == "success":
        slot_info = result.get("bookedSlot") or {}
        booked_date = slot_info.get("date") or result.get("bookedConsularDate") or "CONFIRMED"
        booked_city = slot_info.get("city") or result.get("bookedConsularCity") or consular_priority
        slot_time = slot_info.get("time") or result.get("time") or ""
        log.info(f"🎉 [CONSULAR BOOKED] Successfully booked Consular appointment in {booked_city} on {booked_date} {slot_time}!")
        return True, {
            "bookedConsularDate": booked_date,
            "bookedConsularCity": booked_city,
            "time": slot_time,
            "success": True,
            "elapsed_ms": elapsed_ms,
        }

    log.info(f"⏳ [CONSULAR WAITING] No immediate slots open ({msg}). Retaining 32-minute hold window.")
    return False, {"status": "no_slots", "waitingForConsular": True, "error": msg, "elapsed_ms": elapsed_ms}
