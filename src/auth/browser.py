import os
import sys
import time
import socket
import logging
import subprocess
from pathlib import Path
from patchright.async_api import Page, BrowserContext

from src.common.proxy import chrome_proxy_args

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def get_clean_extension_path() -> str:
    """Ensure the Chrome Extension files reside in a space-free path (e.g. %LOCALAPPDATA%/us_visa_extension)
    so Chromium's command-line parser reliably loads it on Windows without quote/space parsing issues."""
    src_dir = Path(__file__).resolve().parent.parent.parent / "extension-build" / "chrome-mv3-prod"
    clean_dir = Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "us_visa_extension"
    try:
        clean_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        for item in src_dir.iterdir():
            dest = clean_dir / item.name
            if item.is_file():
                if not dest.exists() or dest.stat().st_mtime < item.stat().st_mtime or dest.stat().st_size != item.stat().st_size:
                    shutil.copy2(item, dest)
            elif item.is_dir():
                if not dest.exists():
                    shutil.copytree(item, dest)
        return str(clean_dir.resolve())
    except Exception as e:
        return str(src_dir.resolve())


def ensure_chrome_debug_running(cdp_port: int, profile_dir: str, log: logging.Logger, account: dict | None = None) -> None:
    """Start Chrome with remote debugging on the given port if not already running."""
    def port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    if port_open(cdp_port):
        # Verify that the extension is actually loaded in this running instance
        has_extension = False
        try:
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                targets = json.loads(resp.read().decode())
                has_extension = any("chrome-extension://" in (t.get("url") or "") for t in targets)
        except Exception:
            pass

        if has_extension:
            log.info(f"Chrome debug port {cdp_port} already active with extension verified — connecting.")
            return
        else:
            log.warning(f"Chrome on port {cdp_port} was running WITHOUT extension! Terminating old instance...")
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Get-NetTCPConnection -LocalPort {cdp_port} -ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
                    timeout=5
                )
            except Exception:
                pass
            time.sleep(1.5)

    log.info(f"Starting Chrome with --remote-debugging-port={cdp_port} …")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    import shutil
    import json

    default_dir = Path(profile_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # Clean volatile session/cache data while preserving persistent device state
    cleanup_dirs = [
        default_dir / "Sessions",
        default_dir / "Cache",
        default_dir / "Code Cache",
        default_dir / "GPUCache",
        Path(profile_dir) / "Crashpad",
        Path(profile_dir) / "ShaderCache",
        Path(profile_dir) / "GrShaderCache",
    ]
    for cdir in cleanup_dirs:
        if cdir.exists():
            try:
                shutil.rmtree(cdir, ignore_errors=True)
            except Exception as e:
                log.warning(f"Failed to clear {cdir.name} dir: {e}")

    prefs_file = default_dir / "Preferences"
    try:
        if prefs_file.exists():
            with open(prefs_file, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        else:
            prefs = {}
        prefs["credentials_enable_service"] = False
        if "profile" not in prefs:
            prefs["profile"] = {}
        prefs["profile"]["password_manager_enabled"] = False

        # Pin the Visa Extension to the toolbar so it is prominently visible and active!
        ext_ids = ["glbjnfimcajjenihimblfaponejbkoph", "fignfifoniblkonapihmkfakmlgkbkcf"]
        if "extensions" not in prefs:
            prefs["extensions"] = {}
        if "pinned_extensions" not in prefs["extensions"]:
            prefs["extensions"]["pinned_extensions"] = []
        for eid in ext_ids:
            if eid not in prefs["extensions"]["pinned_extensions"]:
                prefs["extensions"]["pinned_extensions"].append(eid)
        if "ui" not in prefs["extensions"]:
            prefs["extensions"]["ui"] = {}
        prefs["extensions"]["ui"]["developer_mode"] = True

        if "toolbar" not in prefs:
            prefs["toolbar"] = {}
        if "pinned_actions" not in prefs["toolbar"]:
            prefs["toolbar"]["pinned_actions"] = []
        for eid in ext_ids:
            act_id = f"chrome-extension_{eid}"
            if act_id not in prefs["toolbar"]["pinned_actions"]:
                prefs["toolbar"]["pinned_actions"].append(act_id)
            if eid not in prefs["toolbar"]["pinned_actions"]:
                prefs["toolbar"]["pinned_actions"].append(eid)

        with open(prefs_file, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception as e:
        log.warning(f"Could not configure preferences: {e}")

    # Prioritize Playwright's custom Chromium binary because standard Google Chrome
    # heavily restricts side-loading unpacked extensions via --load-extension.
    chrome_exe = None
    import glob
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        pattern_patch = os.path.join(local_app_data, "patchright", "chromium-*", "chrome-win*", "chrome.exe")
        pattern_pw = os.path.join(local_app_data, "ms-playwright", "chromium-*", "chrome-win*", "chrome.exe")
        matches = glob.glob(pattern_patch) + glob.glob(pattern_pw)
        if matches:
            matches.sort(reverse=True)
            chrome_exe = matches[0]

    if not chrome_exe or not os.path.isfile(chrome_exe):
        chrome_exe = CHROME_EXE

    if not os.path.isfile(chrome_exe):
        log.error("Chromium or Google Chrome not found. Please run: playwright install chromium")
        sys.exit(1)

    proxy_account_id = str((account or {}).get("username") or "").strip()
    if not proxy_account_id:
        import re as _re
        m = _re.search(r"chrome_profile_(.+?)(?:_polling)?$", profile_dir)
        if m:
            proxy_account_id = m.group(1)
    if not proxy_account_id:
        proxy_account_id = f"port{cdp_port}"
    log.info(f"Proxy account id resolved to '{proxy_account_id}'")

    clean_extension_path = get_clean_extension_path()
    cmd = [
        chrome_exe,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--disable-blink-features=AutomationControlled",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-software-rasterizer",
        "--disable-dev-shm-usage",
        "--disk-cache-size=10485760",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
        "--force-webrtc-ip-handling-policy=default_public_interface_only",
    ]
    if os.path.isdir(clean_extension_path) and os.path.isfile(os.path.join(clean_extension_path, "manifest.json")):
        log.info(f"Loading extension from verified space-free path: {clean_extension_path}")
        cmd.extend([
            f"--disable-extensions-except={clean_extension_path}",
            f"--load-extension={clean_extension_path}",
            "--enable-extensions",
        ])
    cmd.extend(chrome_proxy_args(proxy_account_id))
    cmd.append("about:blank")

    subprocess.Popen(cmd)

    for _ in range(30):
        time.sleep(0.5)
        if port_open(cdp_port):
            log.info("Chrome debug port ready.")
            return

    log.error(f"Chrome debug port {cdp_port} did not open in time.")
    sys.exit(1)



def browser_page_is_healthy(page) -> bool:
    try:
        if page is None:
            return False
        if hasattr(page, "is_closed") and page.is_closed():
            return False
        url = (page.url or "").lower()
        if not url:
            return False
        if url != "about:blank":
            from urllib.parse import urlparse
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if "usvisascheduling.com" not in hostname and "b2clogin" not in hostname:
                return False
            if any(marker in url for marker in ["/signin", "/account/login", "chrome-error"]):
                return False
        return True
    except Exception:
        return False


async def ensure_page_live(page, log: logging.Logger, *, url: str = "about:blank") -> bool:
    if browser_page_is_healthy(page):
        return True
    try:
        if page is not None and hasattr(page, "context") and page.context is not None:
            new_page = await page.context.new_page()
            if url and url != "about:blank":
                await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            log.warning("Recovered a stale page by creating a fresh page in the same context.")
            return True
    except Exception as exc:
        log.warning(f"Page recreation failed: {exc}")
    return False


async def connect_to_chrome(playwright, cdp_port: int, log: logging.Logger, handle_dialogs: bool = False, **kwargs):
    log.info(f"Connecting to Chrome on ws://127.0.0.1:{cdp_port} …")
    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    page = None
    if context.pages:
        for p in context.pages:
            try:
                if "usvisascheduling.com" in p.url.lower():
                    page = p
                    break
            except Exception:
                pass
        if not page:
            page = context.pages[-1]

        for p in context.pages:
            try:
                if p != page and p.url == "about:blank":
                    await p.close()
            except Exception:
                pass
    else:
        page = await context.new_page()

    if not browser_page_is_healthy(page):
        page = await context.new_page()

    if handle_dialogs:
        async def handle_dialog(dialog):
            try:
                await dialog.accept()
            except Exception:
                pass
        page.on("dialog", handle_dialog)

    log.info(f"Connected — current page: {page.url}")
    return browser, context, page
