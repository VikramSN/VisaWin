import asyncio
import json
import os
import sys
import time
import logging
import random
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
from patchright.async_api import async_playwright

# Ensure project root is on the path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.auth.browser import connect_to_chrome, ensure_chrome_debug_running
from src.booking.cdp_client import ensure_on_portal
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question
from src.common.config import ACCOUNTS_FILE
from src.common.state import read_state, get_state_file
from src.common.utils import LOG_TIME_FORMAT
from src.common.constants import (
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_FAILED_POLL_BACKOFF_MINUTES,
    DEFAULT_GAP_MINUTES,
    SCHEDULE_URL,
    LogEvent,
)
from src.common.structured_logger import get_structured_logger
from src.api import (
    AccountSessionContext,
    VisaScheduleApiClient,
    PollResult,
    extract_session_context,
    SessionExtractionError,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [POLLING] %(message)s",
    datefmt=LOG_TIME_FORMAT,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("polling_runner")

def load_running_accounts():
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        # Assuming enabled means action_mode is SNIPER or similar, or just active in accounts manager
        # In gui.py, accounts are usually just loaded. Let's assume all accounts in accounts.json are eligible unless marked disabled.
        return [acc for acc in accounts if acc.get("enabled", True)]
    except Exception as e:
        log.error(f"Failed to load accounts: {e}")
        return []

async def fetch_dates_via_browser_fallback(page, match_config=None):
    """
    Direct OFC schedule date fetching in browser context with static parameters.
    Bypasses DOM scraping when static identifiers (appd, primaryId) are available,
    keeping the browser safely parked on the homepage without interference.
    """
    match_config = match_config or {}
    username = match_config.get("username") or match_config.get("customer_name") or "unknown"
    from src.common.static_identifiers import get_account_identifiers, save_account_identifiers
    static_primary_id, static_appd = get_account_identifiers(username, match_config)

    if not static_primary_id or not static_appd:
        try:
            from src.api.extractor import extract_ids_from_page
            disc_p, disc_a = await extract_ids_from_page(page)
            if disc_p and disc_a:
                save_account_identifiers(username, disc_p, disc_a)
                static_primary_id = disc_p
                static_appd = disc_a
                match_config["primaryId"] = disc_p
                match_config["appd"] = disc_a
        except Exception as id_err:
            log.warning(f"Fallback dynamic ID extraction failed: {id_err}")

    is_resched = match_config.get("isReschedule")
    if is_resched is not None:
        is_resched = bool(is_resched)

    poll_all = bool(
        match_config.get("pollAllCities", False)
        or match_config.get("role") in ("POLLING_ONLY", "HYBRID")
        or os.getenv("FLEET_RADAR_MODE", "1") == "1"
    )
    criteria = {
        "username": username,
        "appd": static_appd or match_config.get("appd") or "",
        "primaryId": static_primary_id or match_config.get("primaryId") or "",
        "ofcCities": match_config.get("ofcCities", []),
        "ofcStartDate": match_config.get("ofcStartDate", ""),
        "ofcEndDate": match_config.get("ofcEndDate", ""),
        "preventImmediate": match_config.get("prevent_immediate", False),
        "isReschedule": is_resched,
        "pollAllCities": poll_all,
        "role": match_config.get("role", "POLLING_ONLY"),
    }

    js_code = r"""
    async (criteria) => {
        let primaryId = criteria.primaryId || "";
        let appd = criteria.appd || "";
        
        // Early exit: if we're not on the portal (e.g. login page, block page, error page)
        const _pageHostname = window.location.hostname.toLowerCase();
        const _pageUrl = window.location.href.toLowerCase();
        if (_pageUrl.includes("/signin") || _pageUrl.includes("/account/login") || _pageUrl.includes("b2clogin.com")) {
            return { error: "Session expired / redirected to login page: " + window.location.href, needs_recovery: true, session_expired: true };
        }
        if (_pageUrl.includes("waitingroom")) {
            return { error: "In Cloudflare waiting room -- waiting passively", in_waiting_room: true, success: false };
        }
        const _pageTitle = (document.title || "").toLowerCase();
        const _pageHtml = (document.documentElement ? document.documentElement.innerHTML : "").toLowerCase();
        if (_pageTitle.includes("verify you are human") || _pageTitle.includes("just a moment") || (_pageHtml.includes("verify you are human") && document.querySelector("#challenge-stage"))) {
            return { error: "In Cloudflare challenge -- auto-solving", in_challenge: true, success: false };
        }
        if (!_pageHostname.includes("usvisascheduling.com") && !_pageHostname.includes("b2clogin")) {
            return { error: "Not on portal hostname -- current hostname: " + _pageHostname, needs_recovery: true };
        }
        if (!_pageUrl.includes("usvisascheduling.com") && !_pageUrl.includes("b2clogin.com")) {
            return { error: "Not on portal page -- current URL: " + window.location.href, needs_recovery: true };
        }
        
        // Check for Cloudflare terminal WAF block (403 Sorry, you have been blocked -> requires immediate IP rotation)
        if (_pageHtml.includes("sorry, you have been blocked") || _pageHtml.includes("error code 1020") || _pageHtml.includes("cf-error-details")) {
            return { error: "Cloudflare 403 Hard Block -- Proxy IP Banned ('Sorry, you have been blocked')", in_challenge: false, in_cf_block: true, needs_rotate: true, success: false };
        }
        if (_pageHtml.includes("you are being rate limited")) {
            return { error: "Cloudflare rate limit signal", in_challenge: false, in_cf_block: true, needs_rotate: true, success: false };
        }
        
        // If static IDs are missing, fallback to dynamic DOM/script search
        if (!primaryId || !appd) {
            const allUuids = [];
            function addUuid(u) {
                if (u && typeof u === 'string') {
                    const clean = u.trim().toLowerCase();
                    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(clean)) {
                        if (!allUuids.includes(clean)) allUuids.push(clean);
                    }
                }
            }
        
        // Expanded patterns for primaryId
        const primaryIdPatterns = [
            /['"]?(?:primaryId|applicantUuid|ApplicationID|primary_applicant_id|primaryApplicantId|applicant_id)['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /primaryId['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /ApplicationID['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /applicantUuid['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi
        ];
        
        // Expanded patterns for appd/contactId
        const appdPatterns = [
            /['"]?(?:contactId|appd|scheduleGroupId|familyId|group_id|groupId|family_id)['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /appd['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /contactId['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /scheduleGroupId['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi
        ];

        const uuidPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

        // 1. Search in URL and query parameters
        try {
            const curUrl = window.location.href;
            const urlMatches = curUrl.match(uuidPattern) || [];
            urlMatches.forEach(addUuid);
            const uObj = new URL(curUrl);
            const pAppd = uObj.searchParams.get("appd") || uObj.searchParams.get("id") || uObj.searchParams.get("scheduleGroupId");
            if (pAppd && /^[0-9a-f-]{36}$/i.test(pAppd)) {
                appd = pAppd.toLowerCase();
            }
            const pPrim = uObj.searchParams.get("primaryId") || uObj.searchParams.get("applicantId");
            if (pPrim && /^[0-9a-f-]{36}$/i.test(pPrim)) {
                primaryId = pPrim.toLowerCase();
            }
        } catch (e) {}

        // 2. Search in anchor links
        try {
            const links = Array.from(document.querySelectorAll("a[href]"));
            for (const a of links) {
                const href = a.getAttribute("href") || "";
                const hrefMatches = href.match(uuidPattern) || [];
                hrefMatches.forEach(addUuid);
                if (!appd) {
                    const mAppd = href.match(/[?&](?:appd|id|scheduleGroupId)=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
                    if (mAppd && mAppd[1]) {
                        appd = mAppd[1].toLowerCase();
                    }
                }
                if (!primaryId) {
                    const mPrim = href.match(/[?&](?:primaryId|applicantId|ApplicationID)=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
                    if (mPrim && mPrim[1]) {
                        primaryId = mPrim[1].toLowerCase();
                    }
                }
            }
        } catch (e) {}

        // 3. Search inputs, buttons, and hidden form fields
        try {
            const inputs = Array.from(document.querySelectorAll("input, button, form"));
            for (const el of inputs) {
                const val = el.value || "";
                if (/^[0-9a-f-]{36}$/i.test(val)) {
                    addUuid(val);
                    const name = (el.name || el.id || "").toLowerCase();
                    if (!primaryId && (name.includes("primary") || name.includes("applicant"))) {
                        primaryId = val.toLowerCase();
                    }
                    if (!appd && (name.includes("contact") || name.includes("group") || name.includes("appd") || name.includes("family"))) {
                        appd = val.toLowerCase();
                    }
                }
            }
        } catch (e) {}
        
        // 4. Search in inline scripts
        try {
            for (const script of Array.from(document.querySelectorAll("script"))) {
                const content = script.textContent || "";
                if (!content.includes("-")) continue;
                for (const pattern of primaryIdPatterns) {
                    pattern.lastIndex = 0;
                    const match = pattern.exec(content);
                    if (match && match[1]) {
                        primaryId = match[1].toLowerCase();
                        addUuid(match[1]);
                        break;
                    }
                }
                for (const pattern of appdPatterns) {
                    pattern.lastIndex = 0;
                    const match = pattern.exec(content);
                    if (match && match[1]) {
                        appd = match[1].toLowerCase();
                        addUuid(match[1]);
                        break;
                    }
                }
                const matches = content.match(uuidPattern) || [];
                matches.forEach(addUuid);
                if (primaryId && appd) break;
            }
        } catch (e) {}
        
        // 5. Search in window object and global variables
        try {
            const windowKeys = Object.keys(window);
            for (const key of windowKeys) {
                const lk = key.toLowerCase();
                const value = window[key];
                if (typeof value === 'string' && /^[0-9a-f-]{36}$/i.test(value)) {
                    addUuid(value);
                    if (!primaryId && (lk.includes('primary') || lk.includes('applicant'))) {
                        primaryId = value.toLowerCase();
                    }
                    if (!appd && (lk.includes('contact') || lk.includes('group') || lk.includes('appd') || lk.includes('family'))) {
                        appd = value.toLowerCase();
                    }
                }
            }
        } catch (e) {}

        // 6. Search full document HTML for any UUIDs
        try {
            if (document.documentElement) {
                const htmlMatches = document.documentElement.innerHTML.match(uuidPattern) || [];
                htmlMatches.forEach(addUuid);
            }
        } catch (e) {}

        // 7. Resolving and fallback symmetry
        if (!primaryId && appd) {
            primaryId = appd;
        }
        if (!appd && primaryId) {
            appd = primaryId;
        }
        if (!primaryId || !appd) {
            if (allUuids.length >= 2) {
                if (!primaryId) primaryId = allUuids[0];
                if (!appd) appd = allUuids[1];
            } else if (allUuids.length === 1) {
                if (!primaryId) primaryId = allUuids[0];
                if (!appd) appd = allUuids[0];
            }
        }
        
        if (!primaryId || !appd) {
            console.log("ID extraction failed. Found primaryId:", primaryId, "Found appd:", appd);
            return { error: "transient_extraction_failure: Could not locate primaryId or appd in DOM", transient: true, success: false };
        }
        }

        const headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest"
        };

        // Fetch Family Members to get all application IDs only for multi-applicant accounts
        let applicationIds = [primaryId];
        const isMulti = Boolean(criteria.multiPerson);
        if (isMulti) {
            if (window.__cachedApplicationIds && window.__cachedApplicationIds.length > 0) {
                applicationIds = window.__cachedApplicationIds;
            } else {
                try {
                    const familyUrl = `/en-US/custom-actions/?route=/api/v1/schedule-group/query-family-members-ofc&appd=${appd}&cacheString=${Date.now()}`;
                    const bodyStr = `parameters=${encodeURIComponent(JSON.stringify({ primaryId: primaryId, visaClass: "all" }))}`;
                    const res = await fetch(familyUrl, { method: 'POST', headers, body: bodyStr });
                    const data = await res.json();
                    if (data && data.Members) {
                        applicationIds = data.Members.map(m => m.ApplicationID).filter(Boolean);
                        if (applicationIds.length > 0) {
                            window.__cachedApplicationIds = applicationIds;
                        }
                    }
                } catch (e) {
                    console.error(e);
                }
                // Pacing delay after family fetch before city loop to prevent rapid 2-POST burst
                await new Promise(r => setTimeout(r, 2500));
            }
        }
        if (applicationIds.length === 0) applicationIds = [primaryId];

        const OFC_LOCATION_MAP = {
            "CHENNAI": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
            "HYDERABAD": "436bf614-b0db-ec11-a7b4-001dd80234f6",
            "KOLKATA": "466bf614-b0db-ec11-a7b4-001dd80234f6",
            "MUMBAI": "486bf614-b0db-ec11-a7b4-001dd80234f6",
            "NEW DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6"
        };

        let isReschedule = false;
        if (window.__cachedIsReschedule !== undefined) {
            isReschedule = window.__cachedIsReschedule;
        } else if (criteria.isReschedule !== null && criteria.isReschedule !== undefined) {
            isReschedule = Boolean(criteria.isReschedule);
        } else {
            const curHref = (window.location.href || "").toLowerCase();
            const hasRescheduleInUrl = curHref.includes("reschedule");
            const hasRescheduleBtn = Boolean(
                document.querySelector("a[href*='reschedule' i], button[data-action*='reschedule' i]") ||
                Array.from(document.querySelectorAll("a, button, span")).some(el => {
                    const t = (el.innerText || "").trim().toLowerCase();
                    return t === "reschedule appointment" || t === "reschedule";
                })
            );
            isReschedule = hasRescheduleInUrl || hasRescheduleBtn;
        }
        const spoofPath = isReschedule ? "/en-US/ofc-schedule/?reschedule=true" : "/en-US/ofc-schedule/";
        try {
            if (!window.location.pathname.includes("/ofc-schedule")) {
                window.history.replaceState(null, "", spoofPath);
            }
        } catch(e) {}

        const results = {};
        const tokens = {};
        let inChallenge = false;
        let inCfBlock = false;

        const normalizeCity = (value) => {
            const city = String(value || "").trim().toUpperCase();
            return city === "DELHI" ? "NEW DELHI" : city;
        };

        const targetCities = new Set(
            (criteria.ofcCities || []).map(normalizeCity)
        );

        let effectiveStartDate = criteria.ofcStartDate || "";
        const effectiveEndDate = criteria.ofcEndDate || "";

        if (criteria.preventImmediate) {
            const dynamicStart = new Date();
            dynamicStart.setDate(dynamicStart.getDate() + 3);
            const dynamicStartDate =
                `${dynamicStart.getFullYear()}-` +
                `${String(dynamicStart.getMonth() + 1).padStart(2, "0")}-` +
                `${String(dynamicStart.getDate()).padStart(2, "0")}`;

            if (!effectiveStartDate || effectiveStartDate < dynamicStartDate) {
                effectiveStartDate = dynamicStartDate;
            }
        }

        const isFleetRadar = Boolean(criteria.pollAllCities || criteria.role === "POLLING_ONLY");
        const activeCities = isFleetRadar
            ? Object.entries(OFC_LOCATION_MAP)
            : Object.entries(OFC_LOCATION_MAP).filter(([c, _]) => {
                if (!targetCities || targetCities.size === 0) return true;
                return targetCities.has(normalizeCity(c));
            });
        const citiesToPoll = activeCities.length > 0 ? activeCities : Object.entries(OFC_LOCATION_MAP);

        const extractRawError = (response, rawBody) => {
            const titleMatch = (rawBody || "").match(/<title[^>]*>([^<]+)<\/title>/i);
            const title = titleMatch ? titleMatch[1].trim().replace(/\s+/g, " ") : "";
            const textOnly = (rawBody || "").replace(/<[^>]+>/g, " ").replace(/[\r\n\t]+/g, " ").trim();
            const snippet = textOnly.substring(0, 160);
            if (title) {
                return `HTTP ${response.status} [Title: "${title}"] ${snippet}`;
            }
            return `HTTP ${response.status}: ${snippet || "(empty response)"}`;
        };

        for (const [city, postId] of citiesToPoll) {
            try {
                const dateUrl = `/en-US/custom-actions/?route=/api/v1/schedule-group/get-family-ofc-schedule-days&appd=${appd}&cacheString=${Date.now()}`;
                let payload = {
                    primaryId: primaryId,
                    applications: applicationIds,
                    scheduleDayId: "",
                    scheduleEntryId: "",
                    postId: postId,
                    isReschedule: isReschedule ? "true" : "false"
                };
                let bodyStr = `parameters=${encodeURIComponent(JSON.stringify(payload))}`;
                let res = await fetch(dateUrl, { method: 'POST', headers, body: bodyStr });
                let text = await res.text();
                let fullLow = text.toLowerCase();
                let bodyLow = fullLow.substring(0, 4000);

                let isHtml = text.trim().startsWith("<!DOCTYPE") || text.trim().startsWith("<html") || bodyLow.includes("<html") || bodyLow.includes("<body");
                let parsedInit = null;
                try {
                    parsedInit = JSON.parse(text);
                } catch (pe) {}

                // Auto-detect / flip isReschedule if we got the homepage HTML back OR a HasError/ErrorString response without ScheduleDays (e.g. PSE0600 error when reschedule flag is mismatched)
                const hasApiError = Boolean(
                    (parsedInit && (parsedInit.HasError || parsedInit.ErrorString || parsedInit.error)) ||
                    bodyLow.includes("pse0600") ||
                    bodyLow.includes("requested change is not allowed")
                );
                const needsFlip = (isHtml && (bodyLow.includes("customer self-service") || bodyLow.includes("visa application home") || res.status === 200)) ||
                                  (hasApiError && (!parsedInit || !Array.isArray(parsedInit.ScheduleDays)));

                if (needsFlip && window.__cachedIsReschedule === undefined) {
                    const flipped = !isReschedule;
                    const testPayload = { ...payload, isReschedule: flipped ? "true" : "false" };
                    const testBody = `parameters=${encodeURIComponent(JSON.stringify(testPayload))}`;
                    try {
                        const testRes = await fetch(dateUrl, { method: 'POST', headers, body: testBody });
                        const testText = await testRes.text();
                        const testData = JSON.parse(testText);
                        if (testData && (Array.isArray(testData.ScheduleDays) || (!testData.HasError && !testData.ErrorString))) {
                            // Flipped worked! Update isReschedule and replace response
                            isReschedule = flipped;
                            window.__cachedIsReschedule = isReschedule;
                            res = testRes;
                            text = testText;
                            fullLow = text.toLowerCase();
                            bodyLow = fullLow.substring(0, 4000);
                            isHtml = false;
                        }
                    } catch (testErr) {}
                }

                // 1. Cloudflare WAF hard block vs Turnstile challenge vs Microsoft Dynamics Portal 429 rate limit
                const isCfHardBlock = (
                    bodyLow.includes("sorry, you have been blocked") ||
                    bodyLow.includes("you are unable to access") ||
                    bodyLow.includes("cf-error-details") ||
                    bodyLow.includes("error 1015") ||
                    bodyLow.includes("error code 1020") ||
                    bodyLow.includes("you are being rate limited")
                );
                const isCfChallenge = !isCfHardBlock && (res.status === 403 && (
                    bodyLow.includes("just a moment") ||
                    bodyLow.includes("cf-chl") ||
                    bodyLow.includes("challenge-running") ||
                    bodyLow.includes("verify you are human")
                ));
                const isPortal429 = (res.status === 429) ||
                                    bodyLow.includes("429 too many requests") ||
                                    bodyLow.includes("rate limit exceeded");

                if (isCfHardBlock || isCfChallenge || isPortal429) {
                    if (isCfHardBlock) inCfBlock = true;
                    else if (isCfChallenge) inChallenge = true;
                    results[city] = { error: extractRawError(res, text) };
                    break;
                }

                // 2. Session expiration / unauthenticated detection
                if (res.status === 401 || (res.status === 403 && (bodyLow.includes("b2clogin") || bodyLow.includes("signin") || bodyLow.includes("sign-in")))) {
                    results[city] = { error: `Session expired: ${extractRawError(res, text)}` };
                    break;
                }

                if (isHtml) {
                    results[city] = { error: extractRawError(res, text) };
                    continue;
                }

                try {
                    const data = JSON.parse(text);
                    if (data && Array.isArray(data.ScheduleDays)) {
                        results[city] = data.ScheduleDays;
                        if (data.Token) {
                            tokens[city] = data.Token;
                        }
                        window.__cachedIsReschedule = isReschedule;

                        const cityIsSelected = targetCities.has(
                            normalizeCity(city)
                        );

                        const qualifyingDate = cityIsSelected
                            ? data.ScheduleDays.find((item) => {
                                  const dateValue = String(
                                      item.Date || item
                                  ).slice(0, 10);

                                  return (
                                      effectiveStartDate &&
                                      effectiveEndDate &&
                                      dateValue >= effectiveStartDate &&
                                      dateValue <= effectiveEndDate
                                  );
                              })
                            : null;

                        // In Fleet Radar mode, scan all 5 cities completely so all VIP snipers across all cities get slot updates
                        if (qualifyingDate && !isFleetRadar) {
                            const matchedDate = String(
                                qualifyingDate.Date || qualifyingDate
                            ).slice(0, 10);

                            console.log(
                                `[Polling] Qualifying OFC date found: ` +
                                `${city} ${matchedDate}. ` +
                                `Stopping remaining city polling.`
                            );

                            return {
                                success: true,
                                results: results,
                                tokens: tokens,
                                earlyMatch: {
                                    city: city,
                                    date: matchedDate
                                },
                                primaryId: primaryId,
                                appd: appd,
                                isReschedule: isReschedule
                            };
                        }
                    } else if (data && (data.HasError || data.ErrorString || data.title || data.type || data.error)) {
                        const titleStr = String(data.title || "").toLowerCase();
                        if (titleStr.includes("1015") || titleStr.includes("rate limit")) {
                            results[city] = { error: `HTTP ${res.status}: Rate Limited (${data.title})` };
                            break;
                        } else {
                            const errMsg = data.ErrorString || data.title || data.error || JSON.stringify(data);
                            results[city] = { error: `HTTP ${res.status} [API Error]: ${errMsg}` };
                        }
                    } else {
                        results[city] = [];
                    }
                } catch (e) {
                    results[city] = { error: extractRawError(res, text) };
                }
            } catch (e) {
                results[city] = { error: `Browser fetch error: ${e.message || String(e)}` };
            }
            // Human Pacing delay between cities: 2.0s-3.0s with random jitter (prevents burst blocks while keeping cycle snappy)
            await new Promise(r => setTimeout(r, 2000 + Math.floor(Math.random() * 1000)));
        }

        // Always restore window.history URL safely back to homepage /en-US/
        try {
            if (!window.location.pathname.endsWith("/en-US/")) {
                window.history.replaceState(null, "", "/en-US/");
            }
        } catch(e) {}

        const hasCityError = Object.values(results).some(v => v && typeof v === 'object' && v.error);
        return {
            success: !inChallenge && !inCfBlock && !hasCityError,
            in_challenge: inChallenge,
            in_cf_block: inCfBlock,
            results: results,
            tokens: tokens,
            primaryId: primaryId,
            appd: appd,
            isReschedule: isReschedule
        };
    }
    """
    return await page.evaluate(js_code, criteria)
 
async def fetch_dates_via_browser(page, match_config=None):
    """
    Primary OFC schedule date fetching entrypoint.

    Executes direct API queries in the authenticated browser context with static
    identifiers (independent of page DOM/navigation), completely avoiding Cloudflare
    TLS 403 blocks and page reloads.
    """
    match_config = match_config or {}
    username = match_config.get("username") or match_config.get("customer_name") or "unknown"
    from src.common.static_identifiers import get_account_identifiers
    static_primary_id, static_appd = get_account_identifiers(username, match_config)

    if static_primary_id and not match_config.get("primaryId"):
        match_config["primaryId"] = static_primary_id
    if static_appd and not match_config.get("appd"):
        match_config["appd"] = static_appd


    res = await fetch_dates_via_browser_fallback(page, match_config)
    if isinstance(res, dict):
        if res.get("primaryId") and res.get("appd"):
            try:
                from src.common.static_identifiers import save_account_identifiers
                save_account_identifiers(username, str(res["primaryId"]).strip(), str(res["appd"]).strip())
            except Exception:
                pass
        if "isReschedule" in res:
            match_config["isReschedule"] = res["isReschedule"]
    return res


async def poll_account(account, p):
    username = account.get("username")
    password = account.get("password")
    customer_name = account.get("customer_name", username)

    log.info(f"[DISPATCH] Starting polling cycle for account: {customer_name} ({username})")

    cdp_port = 9500 + random.randint(1, 999)
    profile_dir = str((Path(_project_root) / f"chrome_profile_{username}_polling").resolve())
    
    login_script = Path(_project_root) / "src" / "login_runner.py"
    cmd = [
        sys.executable, str(login_script),
        "--username", username,
        "--password", password,
        "--cdp-port", str(cdp_port),
        "--customer", customer_name,
        "--profile-dir", profile_dir,
    ]
    
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    
    login_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(_project_root),
        env=env
    )
    
    ready = False
    
    def read_output():
        nonlocal ready
        for line in iter(login_proc.stdout.readline, ''):
            if line:
                log.info(f"[LOGIN] {line.rstrip()}")
                if "[READY]" in line:
                    ready = True
                    break
                    
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, read_output)
    
    if not ready:
        log.error("Login runner failed to reach [READY] state.")
        login_proc.kill()
        _kill_chrome_by_port(cdp_port)
        return False
        
    log.info(f"[OK] Login complete! Connecting to Chrome on port {cdp_port}...")
    
    browser = None
    try:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0]
        page = context.pages[0]

        log.info("Browser parked safely on portal homepage for direct API monitoring...")
        await asyncio.sleep(2)

        slog = get_structured_logger(username)
        slog.log_session_event(
            event=LogEvent.SESSION_HEALTH_CHECK,
            action="verify_session",
            state="SESSION_EXTRACTING",
            decision="Extracting authenticated context for schedule polling",
        )

        poll_result: Optional[PollResult] = None
        data: Dict[str, Any] = {}

        try:
            session_ctx = await extract_session_context(page, username)
            slog.log_session_event(
                event=LogEvent.SESSION_READY,
                action="session_extracted",
                state="READY",
                details={
                    "primaryId": session_ctx.safe_username,
                    "is_reschedule": session_ctx.is_reschedule,
                },
            )

            # 1. Primary: Direct HTTP API polling
            async with VisaScheduleApiClient(session_ctx) as api_client:
                poll_result = await api_client.poll_ofc_dates(account)

            if poll_result.fallback_to_browser:
                slog.log_monitor_event(
                    event=LogEvent.CLOUDFLARE_CHALLENGE_DETECTED,
                    component="MONITOR_OFC",
                    action="switch_transport",
                    decision="HTTP monitor challenged or returned non-JSON -> switching to controlled browser in-page fetch",
                )
                data = await fetch_dates_via_browser_fallback(page, account)
            elif poll_result.success:
                data = {
                    "success": True,
                    "results": poll_result.days_by_city,
                    "tokens": poll_result.tokens_by_city,
                    "matches": [m.date for m in poll_result.matches],
                }
            else:
                data = {
                    "success": False,
                    "error": poll_result.error_message or "Unknown polling failure",
                    "results": poll_result.days_by_city,
                }

        except Exception as extract_err:
            slog.log_monitor_event(
                event=LogEvent.NETWORK_ERROR,
                component="MONITOR_OFC",
                action="extract_fallback",
                decision=f"Session extraction error ({extract_err}) -> fallback to in-page browser fetch",
            )
            data = await fetch_dates_via_browser_fallback(page, account)

        if data.get("error"):
            slog.log_monitor_event(
                event=LogEvent.NETWORK_ERROR,
                component="MONITOR_OFC",
                action="poll_failed",
                decision=f"Polling failed: {data.get('error')}",
            )
            slog.log_cycle_summary(
                session_state="DEGRADED",
                ofc_status="ERROR",
                consular_status="SKIPPED",
                matched_slots=0,
                next_poll_seconds=60.0,
            )
            return False

        if data.get("success"):
            results = data.get("results", {}) or {}
            # Only an explicit login-page tag ('login_html') proves the session
            # is dead. Transient/CF-blocked HTML must NOT trigger re-login.
            login_errors = [
                city for city, dates in results.items()
                if isinstance(dates, dict)
                and "login_html" in str(dates.get("error", "")).lower()
            ]
            if results and len(login_errors) == len(results):
                slog.log_session_event(
                    event=LogEvent.SESSION_EXPIRED,
                    action="all_login_responses",
                    state="EXPIRED",
                    decision="Polling returned login HTML for all cities -> session expired, triggering re-login",
                )
                return False

            total_matches = len(data.get("matches") or [])
            ofc_status_str = f"OK ({len(results)} cities checked)"
            if total_matches > 0:
                slog.log_monitor_event(
                    event=LogEvent.SLOT_MATCHED,
                    component="MONITOR_OFC",
                    action="match_found",
                    details={"matched_count": total_matches},
                    decision="Qualified slot detected -> notifying booking queue",
                )
            else:
                slog.log_monitor_event(
                    event=LogEvent.SLOT_NOT_FOUND,
                    component="MONITOR_OFC",
                    action="no_matches",
                    decision="Browser healthy; no slots open in target date range",
                )

            slog.log_cycle_summary(
                session_state="HEALTHY",
                ofc_status=ofc_status_str,
                consular_status="STANDBY",
                matched_slots=total_matches,
                next_poll_seconds=float(cooldown_minutes * 60),
            )
            return True
            
    except Exception as e:
        log.error(f"Error during polling for {username}: {e}")
        return False
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        
        try:
            login_proc.kill()
        except Exception:
            pass
            
        _kill_chrome_by_port(cdp_port)
        log.info("Closed browser session and killed login runner.")

def _kill_chrome_by_port(cdp_port: int):
    import subprocess
    try:
        output = subprocess.check_output(f"netstat -ano | findstr :{cdp_port}", shell=True, text=True)
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and parts[1].endswith(f":{cdp_port}"):
                pid = parts[-1]
                if pid != "0":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
    except Exception:
        pass

async def run_polling_loop(cooldown_minutes: int, gap_minutes: int):
    cooldown_map = {}
    
    async with async_playwright() as p:
        while True:
            accounts = load_running_accounts()
            if not accounts:
                log.info("No running accounts found. Waiting...")
                await asyncio.sleep(60)
                continue
                
            now = datetime.now()
            account_polled = False
            
            for account in accounts:
                username = account.get("username")
                
                # Check cooldown
                if username in cooldown_map:
                    cooldown_end = cooldown_map[username]
                    if now < cooldown_end:
                        log.debug(f"Skipping {username}, in cooldown until {cooldown_end.strftime('%H:%M:%S')}")
                        continue
                
                # Check state guard (skip if booking is active)
                state_file = get_state_file(username)
                if state_file.exists():
                    state = read_state(state_file)
                    if state.get("booking_running") or state.get("pending"):
                        log.info(f"Skipping {username}, account is currently busy with a booking.")
                        continue

                # We have an eligible account
                success = await poll_account(account, p)
                account_polled = True

                if success:
                    # Only place a long cooldown on a successful poll.
                    cooldown_map[username] = datetime.now() + timedelta(minutes=cooldown_minutes)
                    log.info(f"Placed {username} in cooldown for {cooldown_minutes} minutes.")
                else:
                    # A failed poll (e.g. expired session, login failure) should
                    # retry soon, not wait a full cooldown. Use a short backoff
                    # so the account re-attempts login on the next cycle.
                    short_backoff = DEFAULT_FAILED_POLL_BACKOFF_MINUTES
                    cooldown_map[username] = datetime.now() + timedelta(minutes=short_backoff)
                    log.info(f"Poll failed for {username}. Short retry cooldown of {short_backoff} minutes.")
                
                # Wait for gap before next account
                log.info(f"Waiting for gap period: {gap_minutes} minutes...")
                await asyncio.sleep(gap_minutes * 60)
                break # Only process one account per loop iteration to re-check states
                
            if not account_polled:
                # All accounts are in cooldown, just sleep for a bit and re-eval
                await asyncio.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_MINUTES, help="Cooldown per account in minutes")
    parser.add_argument("--gap", type=int, default=DEFAULT_GAP_MINUTES, help="Gap between accounts in minutes")
    args = parser.parse_args()
    
    try:
        asyncio.run(run_polling_loop(args.cooldown, args.gap))
    except KeyboardInterrupt:
        log.info("Polling runner stopped.")