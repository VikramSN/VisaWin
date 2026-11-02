"""
Automated solver for Cloudflare Turnstile and 'Verify you are human' challenges.
Uses Patchright's native in-frame click automation (which natively sets isTrusted=True).
Directly targets the Turnstile checkbox inside challenge iframes without delay.
"""

import asyncio
import logging
import random
import time
from patchright.async_api import Page

CHALLENGE_TITLE_KEYWORDS = [
    "just a moment",
    "verify you are human",
    "security check",
    "challenge",
]

HARD_BLOCK_KEYWORDS = [
    "sorry, you have been blocked",
    "you are unable to access",
    "block_headline",
    "why have i been blocked",
    "error code 1020",
    "cf-error-details",
]

IFRAME_SELECTORS = [
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='turnstile']",
    "iframe[title*='Cloudflare']",
    "iframe[title*='Widget']",
    "iframe[src*='challenge']",
]


async def is_cloudflare_hard_block(page: Page) -> bool:
    """Return True if the page is a terminal Cloudflare WAF block (HTTP 403 / Error 1020).
    Hard blocks have no interactive widget and CANNOT be solved with clicks.
    They require an immediate proxy IP rotation.
    """
    try:
        if page is None or (hasattr(page, "is_closed") and page.is_closed()):
            return False
        title = (await page.title() or "").lower()
        content = (await page.content() or "").lower()
        if any(kw in content for kw in HARD_BLOCK_KEYWORDS):
            return True
        if "attention required" in title and ("blocked" in content or "cf-error-details" in content or "cf-wrapper" in content):
            return True
    except Exception:
        pass
    return False


async def is_cloudflare_challenge(page: Page) -> bool:
    """Return True ONLY if the page is currently showing an active, unsolved Cloudflare challenge.
    Never returns True when the user is authenticated and parked on the portal homepage,
    or when the page is a terminal hard WAF block."""
    try:
        if page is None or (hasattr(page, "is_closed") and page.is_closed()):
            return False

        # 0. Terminal hard block check: A 403 WAF block is NOT an interactive challenge!
        if await is_cloudflare_hard_block(page):
            return False

        url = (page.url or "").lower()
        title = (await page.title() or "").lower()

        # Cloudflare challenge pages ALWAYS have very specific titles:
        # "just a moment...", "verify you are human", "security check"
        is_cf_title = any(kw in title for kw in CHALLENGE_TITLE_KEYWORDS)

        # 1. Check if Turnstile is already solved (token populated):
        try:
            val = await page.eval_on_selector(
                "input[name='cf-turnstile-response']",
                "el => el ? el.value : ''",
            )
            if val and len(val) > 20:
                return False
        except Exception:
            pass

        # 2. If title explicitly indicates a challenge, return True:
        if is_cf_title:
            return True

        # 3. Check for ACTUALLY VISIBLE challenge containers on the page:
        for container_sel in ["#challenge-stage", "#turnstile-wrapper", "#cf-please-wait", "#challenge-form", ".challenge-form"]:
            loc = page.locator(container_sel)
            if await loc.count() > 0:
                try:
                    if await loc.first.is_visible():
                        return True
                except Exception:
                    pass

        # 4. Check if any Turnstile iframe contains an active, UNCHECKED checkbox:
        for f in page.frames:
            f_url = (f.url or "").lower()
            if any(k in f_url for k in ["challenges.cloudflare.com", "turnstile", "challenge"]):
                try:
                    el = await f.frame_element()
                    if el and await el.is_visible():
                        box = await el.bounding_box()
                        if box and box["width"] >= 40 and box["height"] >= 20:
                            chk = f.locator("input[type='checkbox'], .ctp-checkbox-label, span.mark, label.ctp-checkbox-label")
                            if await chk.count() > 0 and await chk.first.is_visible():
                                return True
                except Exception:
                    pass

        # 5. Check if user is authenticated and parked on portal homepage without any visible widget:
        if "usvisascheduling.com" in url:
            try:
                is_portal = await page.evaluate(r"""
                    () => {
                        const html = (document.documentElement ? document.documentElement.innerHTML : '').toLowerCase();
                        const nav = document.querySelector('nav, header, .navbar, #navbar, .nav, footer');
                        const hasPortalKeywords = [
                            'customer self-service', 'sign out', 'sign in', 'manage applications',
                            'schedule appointment', 'reschedule appointment', 'appointment confirmation',
                            'messages', 'profile', 'applicant', 'dashboard', 'welcome'
                        ].some(k => html.includes(k));
                        return Boolean(nav || hasPortalKeywords);
                    }
                """)
                if is_portal:
                    return False
            except Exception:
                pass

    except Exception:
        pass
    return False


async def _click_turnstile_checkbox(page: Page, log: logging.Logger) -> bool:
    """Attempt all known vectors to immediately click the Turnstile checkbox inside the iframe."""
    clicked = False

    # Vector 1: Iterate all frames and click inside matching iframe (NEVER click body!)
    try:
        for f in page.frames:
            f_url = (f.url or "").lower()
            if any(k in f_url for k in ["challenge", "turnstile", "cloudflare"]):
                for sel in ["input[type='checkbox']", ".ctp-checkbox-label", "span.mark", "label.ctp-checkbox-label"]:
                    try:
                        loc = f.locator(sel).first
                        if await loc.count() > 0:
                            await loc.click(timeout=1500)
                            log.info(f"[CHALLENGE] [OK] Clicked Turnstile element '{sel}' in challenge frame.")
                            clicked = True
                            return True
                    except Exception:
                        pass
    except Exception:
        pass

    # Vector 2: Use frame_locator across known iframe selectors
    for f_sel in IFRAME_SELECTORS:
        try:
            floc = page.frame_locator(f_sel)
            for sel in ["input[type='checkbox']", ".ctp-checkbox-label", "span.mark", "label.ctp-checkbox-label"]:
                try:
                    loc = floc.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=1500)
                        log.info(f"[CHALLENGE] [OK] Clicked '{sel}' via frame_locator({f_sel}).")
                        clicked = True
                        return True
                except Exception:
                    pass
        except Exception:
            pass

    # Vector 3: In-page mouse click at iframe bounding box (exact checkbox position inside frame)
    try:
        for f in page.frames:
            f_url = (f.url or "").lower()
            if any(k in f_url for k in ["challenge", "turnstile", "cloudflare"]):
                el = await f.frame_element()
                if el:
                    box = await el.bounding_box()
                    if box and box["width"] >= 40 and box["height"] >= 20:
                        target_x = box["x"] + min(28.0, box["width"] * 0.12)
                        target_y = box["y"] + box["height"] * 0.5
                        await page.mouse.move(target_x, target_y)
                        await asyncio.sleep(0.08)
                        await page.mouse.click(target_x, target_y)
                        log.info(f"[CHALLENGE] [OK] Mouse clicked Turnstile at coords ({target_x:.0f}, {target_y:.0f}).")
                        clicked = True
                        return True
    except Exception:
        pass

    # Vector 4: Turnstile response input wrapper click
    try:
        for wrapper_sel in ["#challenge-stage", "#turnstile-wrapper", ".cf-turnstile"]:
            wrap = page.locator(wrapper_sel).first
            if await wrap.count() > 0 and await wrap.is_visible():
                await wrap.click(position={"x": 30, "y": 30}, timeout=1500)
                log.info(f"[CHALLENGE] [OK] Clicked Turnstile wrapper '{wrapper_sel}'.")
                clicked = True
                return True
    except Exception:
        pass

    return clicked


async def solve_cloudflare_challenge(page: Page, log: logging.Logger, max_attempts: int = 5) -> bool:
    """
    Detect and immediately solve Cloudflare Turnstile / verification challenge.
    Immediately clicks the checkbox as soon as visible and waits for token/redirect.
    Gives Turnstile adequate time to compute proof-of-work without aborting it with rapid F5 reloads.
    """
    if not await is_cloudflare_challenge(page):
        return True

    log.info("[CHALLENGE] Cloudflare Turnstile detected — attempting auto-click...")

    for attempt in range(1, max_attempts + 1):
        # Check if already solved
        try:
            val = await page.eval_on_selector("input[name='cf-turnstile-response']", "el => el ? el.value : ''")
            if val and len(val) > 20:
                log.info("[CHALLENGE] [OK] Turnstile token verified!")
                return True
        except Exception:
            pass

        # Click the checkbox
        clicked = await _click_turnstile_checkbox(page, log)
        # Give Cloudflare time to compute proof-of-work without interrupting with DOM reloads
        poll_ticks = 8 if clicked else 4
        for _ in range(poll_ticks):
            await asyncio.sleep(0.5)
            try:
                val = await page.eval_on_selector("input[name='cf-turnstile-response']", "el => el ? el.value : ''")
                if val and len(val) > 20:
                    log.info("[CHALLENGE] [OK] Turnstile token verified after click!")
                    return True
            except Exception:
                pass

            if not await is_cloudflare_challenge(page):
                log.info("[CHALLENGE] [OK] Challenge successfully cleared!")
                return True

    return not await is_cloudflare_challenge(page)
