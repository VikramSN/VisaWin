"""
=============================================================
  Visa Scheduling Bot -- usvisascheduling.com/en-US/
  ─────────────────────────────────────────────────
  Entrypoint runner for spawning Chrome and logging in.
=============================================================
"""

import argparse
import asyncio
import os
import random
import sys
import time
import logging
from pathlib import Path

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

# Ensure project root is on the path for top-level imports (slack.py) and src.* packages
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Import modularized components
from src.common.db_logger import MongoDBHandler
from src.auth.browser import ensure_chrome_debug_running, connect_to_chrome
from src.auth.login import wait_for_waiting_room, login
from src.auth.security import handle_security_question
from src.auth.utils import human_delay
from src.common.utils import safe_id, LOG_TIME_FORMAT
# Slack notifications are disabled; Telegram is the active notification channel.
# Legacy slack import intentionally left commented out for compatibility.
# from slack import send_slack_error
from telegram import send_slack_error

load_dotenv()

FASTCAPTCHA_API_KEY = os.getenv("FASTCAPTCHA_API_KEY", "")

BASE_URL  = "https://www.usvisascheduling.com/en-US/"
# NOTE: The portal does NOT have an /Account/LogOn route. The real sign-in flow is
# /Account/Login/ExternalLogin?returnUrl=...&provider=https://atlasauth.b2clogin.com/tfp/...
# (verified from live traffic). We prefer clicking the "Sign in" link on the home
# page so the site builds this URL itself; the constant below is only a fallback.
LOGIN_URL = ("https://www.usvisascheduling.com/en-US/Account/Login/ExternalLogin"
             "?returnUrl=%2Fen-US%2F"
             "&provider=https%3A%2F%2Fatlasauth.b2clogin.com%2Ftfp%2F"
             "f50ebcfb-eadd-41d8-9099-a7049d073f5c%2Fb2c_1a_atoproduction_atlas_susi%2Fv2.0%2F")

_ARGS: argparse.Namespace | None = None

def _is_login_url(url: str) -> bool:
    u = url.lower()
    if "externalauthenticationfailed" in u or "authenticationfailed" in u:
        return False
    return any(k in u for k in ["logon", "login", "signin", "b2clogin"])


# ── Transient network-error retry (NO browser restart) ──────────────────────
# These Chrome net errors are usually a brief outage, not something the browser
# did wrong. They are retried in-place; the process only exits once retries are
# exhausted (orchestrator restart is the last backstop).
_TRANSIENT_NET_ERRORS = (
    "ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_COMMON_NAME_INVALID",
    "ERR_CERT_DATE_INVALID", "ERR_CERT_WEAK_SIGNATURE_ALGORITHM",
    "ERR_SSL_PROTOCOL_ERROR", "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_EMPTY_RESPONSE", "ERR_ABORTED", "ERR_NAME_NOT_RESOLVED",
    "ERR_PROXY_CONNECTION_FAILED", "ERR_HTTP2_PROTOCOL_ERROR",
    "ERR_SOCKET_NOT_CONNECTED", "ERR_NETWORK_CHANGED", "ERR_INTERNET_DISCONNECTED",
    "ERR_ADDRESS_UNREACHABLE", "ERR_CONNECTION_ABORTED",
)

MAX_NAV_ATTEMPTS = 8        # navigation retries before giving up
MAX_EMERGENCY_ROTATIONS = 3 # emergency IP rotations per goto call (bounded)
NAV_BACKOFF_SECONDS = 5     # base backoff; doubles each attempt (capped at 60s)


async def safe_navigate_or_reload(page, url: str, log, account_id: str = None, reason: str = "retry") -> bool:
    """Retry navigation without killing the live browser session.

    Direct page.reload() calls are brittle when the tab is on a Cloudflare/blocked
    page or the browser is mid-crash; they can hang or trigger a full page crash
    loop. Prefer a gated goto/retry path so the existing browser stays open and we
    recover in-place on the same session whenever possible.
    """
    try:
        if page is None:
            return False
        if hasattr(page, "is_closed") and page.is_closed():
            log.warning(f"Page already closed during {reason}; keeping browser alive instead of reloading it.")
            return False
        await goto_with_retry(page, url, log, account_id=account_id)
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("page crashed", "browser has been closed", "target closed", "page closed")):
            log.warning(f"Browser/page instability during {reason}; staying on the live browser instead of forcing a reload.")
            return False
        log.warning(f"Safe navigation failed during {reason}: {exc}. Falling back to direct goto.")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return True
        except Exception:
            return False


# Errors that mean the proxy tunnel itself is broken -- rotate IP immediately
_TUNNEL_BROKEN_ERRORS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_RESET",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
    "ERR_PROXY_CONNECTION_FAILED",
)

async def goto_with_retry(page, url: str, log, timeout: int = 120_000,
                          wait_until: str = "domcontentloaded", account_id: str = None) -> None:
    """Navigate with retries, in-place. On tunnel / connection failures,
    rotates the proxy IP immediately (emergency rotation) to escape."""
    failures = 0
    ip_rotated = False
    emergency_rotations = 0
    while True:
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:
            msg = str(e)
            if not any(k in msg for k in _TRANSIENT_NET_ERRORS):
                raise  # not a network error -- let the caller decide
            failures += 1
            # Give up only when BOTH ladders are exhausted: the current-IP
            # retry ladder AND the emergency-rotation ladder. A successful
            # rotation restarts the retry ladder (fresh IP = new situation),
            # so a dead tunnel is escaped as soon as any healthy port exists.
            if failures >= MAX_NAV_ATTEMPTS and emergency_rotations >= MAX_EMERGENCY_ROTATIONS:
                log.error(f"Giving up navigating to {url} after {failures} attempts "
                          f"({emergency_rotations} emergency rotations): "
                          f"{msg.splitlines()[0][:200]}")
                raise
            
            # Rotate IP immediately on tunnel/connection failures (no waiting
            # for 3 attempts -- a broken tunnel won't fix itself on retry).
            # This covers ERR_TUNNEL_CONNECTION_FAILED, ERR_CONNECTION_CLOSED, etc.
            is_tunnel_broken = any(k in msg for k in _TUNNEL_BROKEN_ERRORS)
            # Rotate on the first tunnel-broken error -- and again later if the
            # fresh IP also dies (rotation is NOT one-shot for the whole call).
            may_rotate = (not ip_rotated) or failures >= 4
            if (is_tunnel_broken or failures >= 3) and may_rotate \
                    and emergency_rotations < MAX_EMERGENCY_ROTATIONS:
                try:
                    from src.common.proxy import rotate_account_ip
                    result = rotate_account_ip(account_id or "", "Tunnel broken (emergency)")
                    if result:
                        ip_rotated = True
                        emergency_rotations += 1
                        # Fresh exit IP -- restart the per-IP failure ladder so
                        # the new connection gets a full set of retries instead
                        # of instantly exhausting the old ladder.
                        failures = 0
                        log.info(f"🔄 Rotated IP (emergency #{emergency_rotations}/"
                                 f"{MAX_EMERGENCY_ROTATIONS}) -- retrying on fresh IP.")
                    else:
                        log.warning(f"⚠️ IP rotation returned no result -- retrying on same IP.")
                except Exception as rot_err:
                    log.warning(f"IP rotation failed (non-fatal): {rot_err}")
            
            # Short backoff for tunnel errors (we already rotated), longer for others
            if is_tunnel_broken and ip_rotated:
                backoff = 2  # quick retry on fresh IP
            else:
                backoff = min(NAV_BACKOFF_SECONDS * (2 ** (failures - 1)), 30)
            log.warning(f"⚠️ Navigation error (attempt {failures}/{MAX_NAV_ATTEMPTS}): "
                        f"{msg.splitlines()[0][:160]} -- retrying in {backoff}s …")
            await asyncio.sleep(backoff)


def _is_portal_url(url: str) -> bool:
    u = (url or "").lower()
    if not u or "usvisascheduling.com" not in u:
        return False
    if _is_login_url(u) or "b2clogin.com" in u or "externallogin" in u:
        return False
    return any(k in u for k in ["/en-us", "en-us", "/schedule", "/ofc-schedule", "/profile", "/applicant_details", "/dashboard"])


async def wait_for_waiting_room_with_waf_retry(page, log, timeout_minutes: int = 60, account_id: str = None) -> None:
    """Wait for waiting room and Cloudflare challenges patiently without frantic IP churn."""
    await wait_for_waiting_room(page, log, timeout_minutes=timeout_minutes, account_id=account_id)




def _get_state_file(args) -> Path:
    uid = safe_id(args.username)
    return Path(__file__).parent / f"state_{uid}.json"

def _get_args() -> argparse.Namespace:
    """Parse CLI arguments once, caching the result."""
    global _ARGS
    if _ARGS is not None:
        return _ARGS
    parser = argparse.ArgumentParser(description="Visa Scheduling Bot - Login Runner")
    parser.add_argument("--username",    default="",
                        help="Username for the portal")
    parser.add_argument("--password",    default="",
                        help="Visa portal password (default: VISA_PASSWORD env var)")
    parser.add_argument("--cdp-port",   type=int, default=9222,
                        help="Chrome remote-debugging port (default: 9222)")
    parser.add_argument("--customer",    default="",
                        help="Customer label used in log lines (default: username)")
    parser.add_argument("--profile-dir", default="",
                        help="Chrome user-data-dir path (default: chrome_profile_<customer>)")
    _ARGS = parser.parse_args()
    if not _ARGS.customer:
        _ARGS.customer = _ARGS.username
    if not _ARGS.profile_dir:
        _ARGS.profile_dir = str(Path(__file__).parent.parent / f"chrome_profile_{safe_id(_ARGS.username)}")
    return _ARGS

def _make_logger(customer: str) -> logging.Logger:
    name = f"visa-bot[{customer}]"
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s [{customer}] [%(levelname)s] %(message)s",
            datefmt=LOG_TIME_FORMAT,
        ))
        logger.addHandler(handler)
        
        db_handler = MongoDBHandler()
        logger.addHandler(db_handler)
        
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger

log = logging.getLogger("visa-bot")

async def _run_once() -> None:
    """One attempt of the full login flow. Unexpected exceptions propagate to
    run() which retries WITHOUT closing the browser."""
    global log
    args = _get_args()

    # Set up a customer-tagged logger for this session
    log = _make_logger(args.customer)

    missing = []
    if not args.username:
        missing.append("--username")
    if not args.password:
        missing.append("--password")

    if missing:
        log.error(f"Missing required values: {', '.join(missing)}")
        sys.exit(1)

    if not FASTCAPTCHA_API_KEY:
        log.warning("FASTCAPTCHA_API_KEY missing; existing sessions can still be reused, but fresh CAPTCHA login will fail.")

    # Start Chrome in debug mode (or connect to existing).
    # The account dict MUST be passed so the proxy forwarder is keyed under
    # THIS username. Without it every browser shared the legacy '' forwarder:
    # one exit IP for all accounts, and rotate_account_ip(<username>) never
    # matched the real forwarder → ERR_TUNNEL_CONNECTION_FAILED was unrecoverable.
    ensure_chrome_debug_running(args.cdp_port, args.profile_dir, log,
                                account={"username": args.username})
    await asyncio.sleep(2)  # give Chrome a moment to initialise

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, args.cdp_port, log)

        disconnect_event = asyncio.Event()
        browser.on("disconnected", lambda _: disconnect_event.set())

        try:
            # ── 1. & 2. Open site & wait ──────────────
            already_logged_in = False
            cur_url = page.url.lower()
            if _is_portal_url(cur_url):
                try:
                    title = (await page.title() or "").lower()
                    is_challenge = any(bad in title for bad in ["just a moment", "attention required", "verify you are human", "sorry, you have been blocked", "access denied"])
                    if not is_challenge:
                        is_portal_healthy = await page.evaluate(r"""
                            () => {
                                const html = (document.documentElement ? document.documentElement.innerHTML : '').toLowerCase();
                                const nav = document.querySelector('nav, header, .navbar, #navbar, .nav, footer');
                                const hasKeywords = ['customer self-service', 'sign out', 'sign in', 'manage applications',
                                                     'schedule appointment', 'reschedule appointment', 'appointment confirmation',
                                                     'messages', 'profile', 'applicant', 'dashboard', 'welcome'].some(k => html.includes(k));
                                return Boolean(nav || hasKeywords);
                            }
                        """)
                        if is_portal_healthy:
                            log.info(f"Existing authenticated portal session detected on '{page.url}'. Skipping all login steps.")
                            already_logged_in = True
                        else:
                            log.info(f"Page on portal URL ({page.url}) -- waiting for full portal load...")
                    else:
                        log.warning(f"Existing page has portal URL but shows challenge ('{title}'). Handling challenge...")
                except Exception:
                    already_logged_in = True
            elif _is_login_url(cur_url):
                log.info("Already on login page -- skipping initial navigation.")
            else:
                if "usvisascheduling.com" not in cur_url:
                    log.info(f"→ Navigating to {BASE_URL}")
                    await goto_with_retry(page, BASE_URL, log, account_id=args.username)
                    await human_delay(2000, 4000)

                try:
                    await wait_for_waiting_room_with_waf_retry(page, log, timeout_minutes=480, account_id=args.username)
                except Exception as e:
                    if "Cloudflare WAF Hard Block" in str(e):
                        log.error("🛑 Persistent Cloudflare WAF block. Cooling down 30s before restart …")
                        send_slack_error(f"🛑 *Cloudflare WAF block* (persistent): `{args.customer}`. Cooling down 30s.")
                        await asyncio.sleep(30)
                    raise
                await human_delay(1000, 2000)

                # ── 3. Wait for login page or Schedule Portal ──────────────
                log.info("Waiting for login page or portal redirect (up to 30 seconds) …")
                deadline = time.time() + 30
                err_streak = 0
                proactive_signin_attempted = False

                while time.time() < deadline:
                    try:
                        cur_url = page.url.lower()
                    except Exception:
                        await asyncio.sleep(1.5)
                        continue

                    if "chrome-error" in cur_url or "chromewebdata" in cur_url:
                        err_streak += 1
                        log.warning(f"Page is on error page ({page.url}) -- "
                                    f"re-navigating (streak {err_streak}) …")
                        try:
                            await goto_with_retry(page, BASE_URL, log, account_id=args.username)
                        except Exception:
                            pass
                        await asyncio.sleep(min(2 * err_streak, 10))
                        continue

                    if "externalauthenticationfailed" in cur_url or "authenticationfailed" in cur_url:
                        err_streak += 1
                        log.warning(f"Page reached {page.url} (Session/Auth error) — resetting cookies and re-navigating to base URL …")
                        try:
                            await context.clear_cookies()
                        except Exception:
                            pass
                        try:
                            await goto_with_retry(page, BASE_URL, log, account_id=args.username)
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                        continue
                    err_streak = 0

                    # Check if Cloudflare verification challenge popped up
                    from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
                    if await is_cloudflare_challenge(page):
                        log.info("Cloudflare challenge detected while waiting for redirect — solving...")
                        await solve_cloudflare_challenge(page, log)
                        await asyncio.sleep(2)
                        continue

                    if _is_login_url(cur_url):
                        log.info("Arrived at login page.")
                        break
                        
                    if _is_portal_url(cur_url):
                        log.info("Already logged in! Reached home page directly.")
                        already_logged_in = True
                        break

                    # If after 8 seconds we are still on the homepage and haven't auto-redirected, click Sign in
                    if not proactive_signin_attempted and (deadline - time.time()) < 22:
                        proactive_signin_attempted = True
                        signin_link_selectors = [
                            "a[href*='ExternalLogin']",
                            "a[href*='signin']",
                            "a[href*='Login']",
                            "a:has-text('Sign in')",
                            "a:has-text('Sign In')",
                            "button:has-text('Sign in')",
                            "button:has-text('Sign In')",
                            "a:has-text('Log in')",
                            "a:has-text('Login')",
                        ]
                        for sel in signin_link_selectors:
                            try:
                                loc = page.locator(sel).first
                                if await loc.is_visible():
                                    await loc.click(timeout=3000)
                                    log.info(f"Proactively clicked sign-in link: {sel}")
                                    await asyncio.sleep(2)
                                    break
                            except Exception:
                                continue
                        
                    await asyncio.sleep(2)
                else:
                    log.warning("Did not auto-redirect in 30 seconds. Trying manual sign-in navigation …")
                    try:
                        await goto_with_retry(page, BASE_URL, log, account_id=args.username)
                        await human_delay(1000, 2000)
                        signin_link_selectors = [
                            "a[href*='ExternalLogin']",
                            "a[href*='signin']",
                            "a[href*='Login']",
                            "a:has-text('Sign in')",
                            "a:has-text('Sign In')",
                            "button:has-text('Sign in')",
                            "button:has-text('Sign In')",
                            "a:has-text('Log in')",
                            "a:has-text('Login')",
                        ]
                        clicked = False
                        for sel in signin_link_selectors:
                            loc = page.locator(sel).first
                            try:
                                if await loc.is_visible():
                                    await loc.click(timeout=4000)
                                    log.info(f"Clicked sign-in link: {sel}")
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if not clicked:
                            log.warning("Sign-in link not found -- navigating directly to ExternalLogin URL …")
                            await goto_with_retry(page, LOGIN_URL, log, account_id=args.username)
                    except Exception as nav_err:
                        log.warning(f"Sign-in navigation warning ({nav_err}) -- trying ExternalLogin URL …")
                        await goto_with_retry(page, LOGIN_URL, log, account_id=args.username)
                    await wait_for_waiting_room_with_waf_retry(page, log, timeout_minutes=15, account_id=args.username)
                    try:
                        cur_url = page.url.lower()
                        if _is_portal_url(cur_url):
                            log.info("Existing session redirected to portal after manual navigation.")
                            already_logged_in = True
                    except Exception:
                        pass
                    
            if already_logged_in:
                # Skip to step 5 / READY
                pass
            else:
                await human_delay(1000, 2000)

            if not already_logged_in:
                # ── 4. Login ──────────────────────────────
                if not FASTCAPTCHA_API_KEY:
                    log.error("Fresh login requires FASTCAPTCHA_API_KEY because this session was not already authenticated.")
                    return

                success = False
                for attempt in range(1, 6):
                    log.info(f"Login attempt {attempt}/5")
                    success = await login(page, args.username, args.password, FASTCAPTCHA_API_KEY, log)
                    if success:
                        break
                    
                    if attempt < 5:
                        log.info("Retrying login...")
                        await safe_navigate_or_reload(page, BASE_URL, log, account_id=args.username, reason="login retry")
                        await human_delay(3000, 5000)

                if not success:
                    log.error("Login failed after 5 attempts.")
                    try:
                        send_slack_error(f"❌ *Login Failed*: `{args.customer}` could not log in after 5 consecutive attempts. Bot is restarting.")
                    except Exception as e:
                        log.error(f"Failed to send Slack alert: {e}")
                    await page.screenshot(path=f"login_failed_{args.customer}.png")
                    sys.exit(1)

            if not already_logged_in:
                # ── 5. Security question ──────────────────
                await human_delay(1500, 3000)
                if not await handle_security_question(page, args.username, log):
                    log.error("Security question failed.")
                    await page.screenshot(path=f"security_question_failed_{args.customer}.png")
                    sys.exit(1)
                try:
                    log.info("Waiting for portal redirect after login...")
                    deadline = time.time() + 120
                    while time.time() < deadline:
                        if "/en-us" in page.url.lower():
                            if "/profile" in page.url.lower():
                                log.info(f"Arrived at profile landing ({page.url}) — navigating to portal home...")
                                try:
                                    await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=25000)
                                except Exception:
                                    pass
                            log.info(f"Arrived at portal: {page.url}")
                            break
                        
                        # Cloudflare can intercept the intermediate redirect (e.g. signin-aad-b2c_1)
                        await wait_for_waiting_room(page, log, timeout_minutes=1)
                        await asyncio.sleep(2)
                except Exception as e:
                    log.warning(f"Timeout waiting for portal redirect: {e}")
                    if "Cloudflare WAF Block Page" in str(e):
                        log.error(f"[{args.customer}] Cloudflare WAF blocked this IP during login. Rotating IP and restarting...")
                        try:
                            from src.common.proxy import rotate_account_ip
                            rotate_account_ip(args.customer, "WAF block during login redirect")
                        except Exception:
                            pass
                        sys.exit(1)

            # Ensure page actually arrived at portal and is not a Cloudflare block page
            cur_p_url = (page.url or "").lower()
            if "/en-us" not in cur_p_url:
                log.error(f"[{args.customer}] Login failed — page is at '{page.url}', not portal. Exiting for restart...")
                sys.exit(1)
            try:
                from src.auth.challenge import is_cloudflare_challenge, solve_cloudflare_challenge
                if await is_cloudflare_challenge(page):
                    log.info(f"[{args.customer}] Cloudflare challenge detected on portal landing — solving...")
                    await solve_cloudflare_challenge(page, log, max_attempts=3)
                    await asyncio.sleep(2)

                page_html = (await page.content() or "").lower()
                if "sorry, you have been blocked" in page_html and not await is_cloudflare_challenge(page):
                    log.warning(f"[{args.customer}] Block page detected on portal landing. Navigating to {BASE_URL} to refresh...")
                    try:
                        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=25000)
                        await asyncio.sleep(3)
                        await wait_for_waiting_room_with_waf_retry(page, log, timeout_minutes=5, account_id=args.username)
                        page_html = (await page.content() or "").lower()
                    except Exception as nav_e:
                        log.warning(f"Refresh navigation error: {nav_e}")
                    
                    if "sorry, you have been blocked" in page_html and not await is_cloudflare_challenge(page):
                        log.error(f"[{args.customer}] Cloudflare WAF block page persisted after reload. Cooling down 45s...")
                        await asyncio.sleep(45)
                        try:
                            from src.common.proxy import rotate_account_ip
                            rotate_account_ip(args.customer, "Persistent WAF block on portal landing")
                        except Exception:
                            pass
                        sys.exit(1)
            except Exception as e:
                log.warning(f"Landing block verification note: {e}")

            # Auto-persist static identifiers if discoverable on initial login
            try:
                from src.common.static_identifiers import save_account_identifiers
                from src.api.extractor import extract_ids_from_page
                disc_p, disc_a = await extract_ids_from_page(page)
                if disc_p and disc_a:
                    save_account_identifiers(args.username, disc_p, disc_a)
                    if getattr(args, "customer", None) and args.customer != args.username:
                        save_account_identifiers(args.customer, disc_p, disc_a)
                    log.info(f"Discovered and persisted fresh static identifiers for {args.username}: appd={disc_a}, primaryId={disc_p}")
            except Exception as ident_err:
                log.warning(f"Could not auto-persist static identifiers: {ident_err}")

            # ── Signal orchestrator: login complete ───
            print(f"[READY] {args.customer}", flush=True)
            log.info("=" * 60)
            log.info("✅  Login complete! Keeping browser open …")
            try:
                log.info(f"   URL: {page.url}")
            except Exception:
                pass
            log.info("=" * 60)

            # Keep the browser open so bot2 can use this session
            # If the user manually closes Chrome, disconnect_event will trigger and we will exit.
            await disconnect_event.wait()
            log.error("Browser was disconnected (closed). Exiting login runner...")
            sys.exit(99)

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            try:
                await page.screenshot(path=f"error_{args.customer}.png")
                log.info(f"Screenshot → error_{args.customer}.png")
            except Exception:
                pass
            # Do NOT exit here -- let run() retry the whole flow (browser stays
            # open); the orchestrator restart is only the last backstop.
            raise


# Whole-flow retries before giving up and exiting (orchestrator backstop).
MAX_RUN_RETRIES = 3


async def run() -> None:
    """Retry wrapper: on unexpected errors, retry the whole login flow IN-PLACE
    (reconnecting to the SAME Chrome) instead of crashing and forcing the
    orchestrator to kill and reopen the browser."""
    for run_attempt in range(1, MAX_RUN_RETRIES + 2):
        try:
            await _run_once()
            return
        except SystemExit:
            raise  # deliberate exits (login failed, browser closed, etc.)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return
        except Exception as e:
            if run_attempt > MAX_RUN_RETRIES:
                log.error(f"❌ Run retries exhausted after repeated errors -- exiting: {e}")
                raise
            cooldown = 10 if run_attempt == 1 else min(30 * run_attempt, 90)
            log.warning(f"⚠️ Run-level error (attempt {run_attempt}/{MAX_RUN_RETRIES + 1}): "
                        f"{e} -- retrying in {cooldown}s (browser stays open) …")
            await asyncio.sleep(cooldown)


if __name__ == "__main__":
    asyncio.run(run())
