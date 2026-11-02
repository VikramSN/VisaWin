"""Session state extractor for the US visa scheduling portal.

Extracts authenticated session cookies, user-agent, dynamic primaryId, and dynamic
appd from the Playwright browser page after login, bootstrapping the session state
for the browserless API client.
"""

import logging
import re
from typing import Dict, Optional, Tuple

from patchright.async_api import BrowserContext, Page

from src.api.models import AccountSessionContext
from src.common.constants import BASE_URL

log = logging.getLogger("api_extractor")

UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class SessionExtractionError(Exception):
    """Raised when essential session state (cookies, primaryId, appd) cannot be extracted."""
    pass


async def extract_ids_from_page(page: Page) -> Tuple[Optional[str], Optional[str]]:
    """Extract dynamic primaryId and appd from the active page DOM/scripts.

    Returns (primaryId, appd) if found, or (None, None) / partial.
    """
    js_extraction_script = r"""
    () => {
        let primaryId = "";
        let appd = "";

        const uuidPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
        const allUuids = [];
        function addUuid(u) {
            if (u && typeof u === 'string') {
                const clean = u.trim().toLowerCase();
                if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(clean)) {
                    if (!allUuids.includes(clean)) allUuids.push(clean);
                }
            }
        }

        const primaryIdPatterns = [
            /['"]?(?:primaryId|applicantUuid|ApplicationID|primary_applicant_id|primaryApplicantId|applicant_id)['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /primaryId['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /ApplicationID['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /applicantUuid['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi
        ];

        const appdPatterns = [
            /['"]?(?:contactId|appd|scheduleGroupId|familyId|group_id|groupId|family_id)['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /appd['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /contactId['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi,
            /scheduleGroupId['"]?\s*[:=]\s*['"]([0-9a-f-]{36})['"]/gi
        ];

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

        // 2. Search in anchor links (homepage links often carry ?appd=... or ?id=...)
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

        // 4. Search in inline and document scripts
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

        // 5. Search window object properties
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

        // 6. Search full document HTML for any UUIDs as final collection
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

        return { primaryId: primaryId || null, appd: appd || null };
    }
    """
    import asyncio
    for attempt in range(2):
        try:
            res = await page.evaluate(js_extraction_script)
            if isinstance(res, dict):
                return res.get("primaryId"), res.get("appd")
        except Exception as exc:
            if "context was destroyed" in str(exc).lower() or "navigation" in str(exc).lower():
                await asyncio.sleep(1.5)
                continue
            log.warning(f"Error evaluating ID extraction in page: {exc}")
            break
    return None, None


async def extract_session_context(
    page: Page,
    username: str,
    proxy_url: Optional[str] = None,
) -> AccountSessionContext:
    """Extract authenticated cookies, user agent, and dynamic IDs from the page.

    Raises SessionExtractionError if the page is missing or required cookies/IDs
    are unavailable.
    """
    if page is None or page.is_closed():
        raise SessionExtractionError("Cannot extract session: page is None or closed.")

    url = (page.url or "").lower()
    if "usvisascheduling.com" not in url and "b2clogin.com" not in url:
        raise SessionExtractionError(
            f"Cannot extract session: not on visa portal (current URL: {page.url})"
        )

    # 1. Extract Cookies from Context
    context: BrowserContext = page.context
    try:
        cookie_records = await context.cookies([
            "https://www.usvisascheduling.com",
            "https://usvisascheduling.com",
        ])
        if not cookie_records:
            cookie_records = await context.cookies()
    except Exception:
        cookie_records = await context.cookies()
    cookies: Dict[str, str] = {c["name"]: c["value"] for c in cookie_records}

    if not cookies:
        raise SessionExtractionError("No session cookies found in browser context.")

    # 2. Extract User Agent
    user_agent = ""
    try:
        user_agent = await page.evaluate("() => navigator.userAgent")
    except Exception as exc:
        log.warning(f"Could not read navigator.userAgent: {exc}")

    # 3. Resolve primaryId and appd (static configuration first, dynamic DOM fallback)
    from src.common.static_identifiers import get_account_identifiers, save_account_identifiers
    static_primary_id, static_appd = get_account_identifiers(username)

    if static_primary_id and static_appd:
        primary_id, appd = static_primary_id, static_appd
    else:
        primary_id, appd = await extract_ids_from_page(page)
        if primary_id and appd:
            save_account_identifiers(username, primary_id, appd)
        else:
            primary_id = primary_id or static_primary_id
            appd = appd or static_appd

    if not primary_id or not appd:
        # Check if page is currently on schedule page; if not, caller can navigate or retry
        raise SessionExtractionError(
            f"Could not locate static or dynamic session identifiers: primaryId={primary_id}, appd={appd}"
        )

    is_reschedule = "reschedule" in url

    # 4. Resolve proxy URL for account if not passed explicitly
    if not proxy_url:
        try:
            from src.common.proxy import get_account_proxy_url
            proxy_url = get_account_proxy_url(username)
        except Exception:
            pass

    return AccountSessionContext(
        username=username,
        primary_id=primary_id,
        appd=appd,
        cookies=cookies,
        user_agent=user_agent,
        is_reschedule=is_reschedule,
        proxy_url=proxy_url,
    )
