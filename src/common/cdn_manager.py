"""
CDN & Edge Clearance Management for US Visa Portal.
Handles quiet in-place renewals of Cloudflare edge tokens (__cf_bm, cf_clearance)
and Dynamics 365 session affinity every 60-90 minutes without triggering hard blocks.
"""

import time
import logging
from typing import Any, Dict

# Proactive CDN refresh window: 25 minutes (1500s)
# Cloudflare cf_clearance cookies expire in ~30-45 minutes.
# Refreshing every 25m guarantees tokens are ALWAYS active before expiration.
CDN_REFRESH_INTERVAL_SECONDS = 1500  # 25 minutes

_LAST_CDN_REFRESH: Dict[str, float] = {}


def is_cdn_refresh_due(account_id: str) -> bool:
    """Check if the account's CDN clearance token is older than ~25 minutes."""
    last_ts = _LAST_CDN_REFRESH.get(str(account_id).lower(), 0.0)
    if last_ts == 0.0:
        _LAST_CDN_REFRESH[str(account_id).lower()] = time.time()
        return False
    return (time.time() - last_ts) >= CDN_REFRESH_INTERVAL_SECONDS


async def quiet_cdn_refresh(page: Any, account_id: str, log: logging.Logger) -> bool:
    """Quietly refreshes Cloudflare edge cookies (__cf_bm, cf_clearance)
    and Dynamics 365 application gateway affinity without page reload."""
    clean_id = str(account_id).lower()
    try:
        log.info(f"🔄 [CDN-REFRESH] '{account_id}': Quietly renewing Cloudflare edge clearance & session tokens (~25m mark)...")

        # 1. In-page lightweight touch to valid portal route (/en-US/)
        # Fetching /en-US/ returns HTTP 200 organically and refreshes cookies
        res = await page.evaluate("""
            async () => {
                try {
                    const cacheBust = Date.now();
                    const resp = await fetch('/en-US/?_touch=' + cacheBust, {
                        method: 'GET',
                        credentials: 'include',
                        headers: {
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'X-Requested-With': 'XMLHttpRequest'
                        }
                    });
                    return { ok: resp.ok, status: resp.status };
                } catch (e) {
                    return { ok: false, error: String(e) };
                }
            }
        """)

        if isinstance(res, dict) and (res.get("ok") or res.get("status") == 200):
            _LAST_CDN_REFRESH[clean_id] = time.time()
            log.info(
                f"✅ [CDN-REFRESH] '{account_id}': Edge clearance & tokens renewed successfully (HTTP 200). "
                f"Next refresh in ~25m."
            )
            return True
        else:
            status = res.get("status") if isinstance(res, dict) else "unknown"
            log.warning(f"⚠️ [CDN-REFRESH] '{account_id}': Touch returned {status}. Executing harmless page reload to renew Turnstile clearance...")
            # If touch failed, escalate to a harmless page reload on sticky IP to force Cloudflare clearance renewal
            try:
                await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(1500)
                cur_u = (page.url or "").lower()
                if "/en-us" in cur_u and not any(k in cur_u for k in ["b2clogin", "signin"]):
                    _LAST_CDN_REFRESH[clean_id] = time.time()
                    log.info(f"✅ [CDN-REFRESH] '{account_id}': Cloudflare cf_clearance renewed via in-place reload!")
                    return True
            except Exception as reload_ex:
                log.warning(f"⚠️ [CDN-REFRESH] '{account_id}': Reload notice: {reload_ex}")

            _LAST_CDN_REFRESH[clean_id] = time.time() - (CDN_REFRESH_INTERVAL_SECONDS - 600)  # retry in 10m
            return False

    except Exception as exc:
        log.warning(f"⚠️ [CDN-REFRESH] '{account_id}': Notice during quiet renewal: {exc}")
        _LAST_CDN_REFRESH[clean_id] = time.time() - (CDN_REFRESH_INTERVAL_SECONDS - 300)
        return False
