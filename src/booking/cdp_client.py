import logging
import asyncio
from patchright.async_api import Page

PORTAL_HOST = "www.usvisascheduling.com"
PORTAL_URL  = "https://www.usvisascheduling.com/en-US/"


async def ensure_on_portal(page: Page, log: logging.Logger, timeout_seconds: int = 30) -> bool:
    """Ensure the browser is on the authenticated portal homepage (https://www.usvisascheduling.com/en-US/).
    
    If the browser landed on /profile/?ReturnUrl= after B2C login, directly navigates
    to the portal homepage immediately rather than waiting idly.
    """
    def _is_valid(url: str) -> bool:
        if not url: return False
        lower = url.lower()
        if not lower.startswith("https://www.usvisascheduling.com"): return False
        if "/profile" in lower or "/account" in lower or "b2clogin.com" in lower: return False
        return True

    # 1. Quick check: already on portal homepage
    try:
        cur_url = page.url or ""
        if _is_valid(cur_url):
            log.info(f"On stable portal: {cur_url}")
            return True

        # If on usvisascheduling.com profile or redirect landing, navigate directly to portal home
        if cur_url.startswith("https://www.usvisascheduling.com"):
            log.info(f"On portal landing page ({cur_url}) — navigating directly to {PORTAL_URL} …")
            await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(1)
            if _is_valid(page.url):
                log.info(f"On stable portal: {page.url}")
                return True
    except Exception as e:
        log.warning(f"Initial portal check note: {e}")

    # 2. Wait briefly (up to 10s) if B2C redirect chain is still settling
    deadline = asyncio.get_event_loop().time() + min(timeout_seconds, 10)
    while asyncio.get_event_loop().time() < deadline:
        try:
            cur_url = page.url or ""
            if _is_valid(cur_url):
                log.info(f"On stable portal: {cur_url}")
                return True
            if cur_url.startswith("https://www.usvisascheduling.com") and "/profile" in cur_url.lower():
                await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(1)
                if _is_valid(page.url):
                    log.info(f"On stable portal: {page.url}")
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)

    # 3. Direct navigation fallback
    log.info(f"Navigating directly to {PORTAL_URL} …")
    try:
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=35000)
        await asyncio.sleep(2)
        if _is_valid(page.url):
            log.info(f"On stable portal: {page.url}")
            return True
    except Exception as e:
        log.error(f"Failed to navigate to portal: {e}")

    log.error(f"Timed out waiting for portal. Last URL: {page.url}")
    return False

