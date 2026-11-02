import time
import asyncio
import logging
from patchright.async_api import Page
from src.auth.utils import human_delay, human_type, human_click, human_scroll

async def wait_for_waiting_room(
    page: Page,
    log: logging.Logger,
    timeout_minutes: float = 30.0,
    account_id: str | None = None,
) -> None:
    deadline = time.time() + max(0.5, float(timeout_minutes)) * 60
    log.info("Checking for Cloudflare Waiting Room …")
    
    while time.time() < deadline:
        try:
            html = await page.content()
            title = await page.title()
        except Exception:
            # Page is mid-navigation
            await asyncio.sleep(1.5)
            continue

        html_lower = html.lower()
        title_lower = title.lower()

        # Scheduled maintenance
        if (
            "scheduled website outage" in html_lower
            or "website downtime is estimated" in html_lower
            or "website under maintenance" in title_lower
        ):
            log.error("U.S. Visa Scheduling Website is under scheduled maintenance.")
            raise Exception("Website Under Maintenance")

        # Already on authenticated portal page
        if any(kw in html_lower for kw in ["schedule appointment", "reschedule appointment", "cancel appointment"]):
            log.info("Home page keywords detected. Already logged in.")
            return

        # 1. First Priority: Check for interactive Cloudflare verification challenge (Turnstile)
        from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
        if await is_cloudflare_challenge(page):
            log.info("[CHALLENGE] Cloudflare challenge / Turnstile detected — solving...")
            solved = await solve_cloudflare_challenge(page, log, max_attempts=3)
            if not solved:
                chal_fails = getattr(wait_for_waiting_room, "_chal_fails", 0) + 1
                wait_for_waiting_room._chal_fails = chal_fails
                if chal_fails in (2, 3):
                    log.info(f"[RELOAD] Turnstile widget pending in waiting room (attempt #{chal_fails}) -- reloading page...")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                elif chal_fails >= 5:
                    wait_for_waiting_room._chal_fails = 0
            else:
                wait_for_waiting_room._chal_fails = 0
            await asyncio.sleep(2)
            continue

        # 2. Hard block page: Only if explicit "sorry, you have been blocked" AND not a challenge widget
        if "sorry, you have been blocked" in html_lower and not await is_cloudflare_challenge(page):
            log.warning("🚫 Cloudflare hard block page detected.")
            raise Exception("Cloudflare WAF Block Page")

        # Check for waiting room indicators
        in_queue = any(kw in title_lower for kw in [
            "moment", "waiting room", "queue", "verify you are human", "attention required"
        ])
        if not in_queue:
            cf_elements = [
                "#cf-please-wait",
                "[data-translate='challenge_headline']",
                ".queue-position",
                "#queuePosition",
                "#waitTime",
            ]
            for sel in cf_elements:
                try:
                    if await page.locator(sel).is_visible():
                        in_queue = True
                        break
                except Exception:
                    pass

        if not in_queue:
            current_url = page.url.lower()
            if "about:blank" in current_url:
                await asyncio.sleep(2)
                continue

            if "chrome-error" in current_url or "chromewebdata" in current_url:
                log.warning(f"Browser on error page ({page.url}) — waiting and reloading...")
                await asyncio.sleep(4)
                try:
                    await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue

            if "externalauthenticationfailed" in current_url:
                log.warning(f"Detected ExternalAuthenticationFailed ({page.url}) — re-navigating to base URL...")
                try:
                    await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue

            if "usvisascheduling.com" not in current_url and "b2clogin" not in current_url:
                log.warning(f"Page on unexpected URL: {page.url}")
            else:
                log.info(f"Waiting room cleared — URL: {page.url}")
            return

        # Check if this is an actual queued waiting room with a position/wait time counter
        try:
            loc = page.locator("[data-queue-position], .queue-position, #queuePosition, #waitTime")
            if await loc.count() > 0 and await loc.first.is_visible():
                pos = (await loc.first.inner_text()).strip()
                if pos and pos != "unknown":
                    log.info(f"[WAITING ROOM] In waiting queue — position: {pos}")
        except Exception:
            pass

        log.info("[WAITING ROOM] In waiting room — waiting for queue to advance...")
        await asyncio.sleep(3)

    raise TimeoutError("Timed out waiting for waiting room to clear.")


async def login(page: Page, username: str, password: str, api_key: str, log: logging.Logger) -> bool:
    log.info("Starting login …")

    # Fast-check for Cloudflare challenge on login page
    from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
    if await is_cloudflare_challenge(page):
        log.info("[LOGIN] Cloudflare challenge detected on login page — solving...")
        await solve_cloudflare_challenge(page, log, max_attempts=3)
        await asyncio.sleep(2)

    # Check if we are already logged in on the portal homepage
    try:
        cur_url = (page.url or "").lower()
        if "usvisascheduling.com" in cur_url and not any(k in cur_url for k in ["b2clogin", "signin", "sign-in", "login", "logon"]):
            page_text = (await page.content() or "").lower()
            if any(kw in page_text for kw in ["schedule appointment", "reschedule appointment", "appointment manager", "cancel appointment"]):
                log.info("Already on authenticated portal page! Bypassing login credentials.")
                return True
    except Exception:
        pass

    u_selectors = [
        "#signInName", "#email", "input[name='signInName']",
        "input[name='UserName']", "input[name='username']",
        "input[name='email']",    "input[type='email']",
        "input[id='UserName']",   "input[id='username']",
        "input[placeholder*='username' i]", "input[placeholder*='email' i]",
        "input[type='text']",
    ]
    p_selectors = [
        "#password", "input[name='Password']", "input[name='password']",
        "input[type='password']", "input[id='Password']",
        "input[placeholder*='password' i]",
    ]

    u_combined = ", ".join(u_selectors)
    p_combined = ", ".join(p_selectors)

    try:
        await page.wait_for_selector(u_combined, state="visible", timeout=20000)
    except Exception:
        # Check if Turnstile popped up late
        if await is_cloudflare_challenge(page):
            log.info("[LOGIN] Late Cloudflare challenge detected — solving...")
            await solve_cloudflare_challenge(page, log, max_attempts=3)
            await asyncio.sleep(2)
            try:
                await page.wait_for_selector(u_combined, state="visible", timeout=15000)
            except Exception:
                log.error("Username field did not appear within timeout.")
                return False
        else:
            log.error("Username field did not appear within 20s.")
            return False

    u_field = None
    for s in u_selectors:
        if await page.locator(s).count() > 0:
            u_field = s
            break

    p_field = None
    for s in p_selectors:
        if await page.locator(s).count() > 0:
            p_field = s
            break

    if not u_field:
        log.error("Username field not found.")
        return False
    if not p_field:
        log.error("Password field not found.")
        return False

    await human_type(page, u_field, username)
    log.debug(f"Typed username: {username}")
    await human_type(page, p_field, password)
    log.debug("Typed password.")

    from src.auth.captcha import solve_captcha_on_page
    if not await solve_captcha_on_page(page, api_key, log):
        log.error("CAPTCHA failed.")
        return False

    await human_delay(300, 600)

    signin_selectors = [
        "input[type='submit'][value*='Sign' i]",
        "input[type='submit'][value*='Log' i]",
        "button[type='submit']",
        "button:has-text('Sign In')",
        "button:has-text('Login')",
        "input[value='Login']",
        "#loginButton", ".login-btn",
    ]

    clicked = False
    for sel in signin_selectors:
        if await page.locator(sel).count() > 0:
            await human_click(page, sel)
            log.info(f"Clicked Sign In: {sel}")
            clicked = True
            break

    if not clicked:
        log.error("Sign In button not found.")
        return False

    log.info("Waiting for login redirect ...")
    try:
        for _ in range(45):
            u = page.url.lower()
            if "selfasserted" in u or not any(k in u for k in ["b2clogin", "login", "logon", "signin", "sign-in"]):
                break
            await asyncio.sleep(1)
    except Exception:
        pass

    try:
        url  = page.url
        html = await page.inner_text("body")  # MUST use inner_text
    except Exception as e:
        if page.is_closed() or "closed" in str(e).lower():
            log.error("Page was unexpectedly closed!")
            return False
        log.info(f"Page navigating, couldn't read content ({e}) — assuming successful redirect.")
        return True

    if any(p in html.lower() for p in [
        "invalid credentials", "incorrect password", "login failed",
        "the user name or password", "invalid username",
        "character", "match the image", "try again", "does not match", "captcha failed", "image captcha"
    ]):
        log.error("Login error / invalid CAPTCHA message on page. Aborting attempt and forcing refresh...")
        return False

    url_lower = url.lower()
    if "selfasserted" not in url_lower and any(k in url_lower for k in ["logon", "login", "signin", "sign-in", "b2clogin"]):
        log.error("Login still failed after initial submit. Aborting attempt.")
        return False

    try:
        log.info(f"Login successful — URL: {page.url}")
    except Exception:
        log.info("Login successful — navigating ...")
    return True
