"""
Authentic Modern User-Agent & Client Hints (UA-CH) Profile Pool.
Provides 25+ cryptographically aligned Chrome profiles (Chrome 128 - 132) on Windows 10 & 11.
Synchronizes HTTP User-Agent, Sec-CH-UA headers, and navigator.userAgentData DOM Client Hints
so Cloudflare Turnstile entropy and bot-detection checks pass cleanly.
"""

import hashlib
import logging
from typing import Dict, Any, List

log = logging.getLogger("UserAgents")

# 25 Curated, battle-tested modern profiles (Chrome 128 - 132 on Windows 10/11)
PROFILES: List[Dict[str, Any]] = [
    # --- Chrome 131 Cohort (Windows 11) ---
    {
        "id": "win11_chrome_131_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.139",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "15.0.0",  # Windows 11 in UA-CH
        "grease": "Google Chrome",
    },
    {
        "id": "win11_chrome_131_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.204",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "grease": "Chromium",
    },
    {
        "id": "win11_chrome_131_3",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.108",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "10.0.22631",
        "grease": "Google Chrome",
    },
    {
        "id": "win11_chrome_131_4",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.85",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "10.0.22621",
        "grease": "Chromium",
    },

    # --- Chrome 131 Cohort (Windows 10) ---
    {
        "id": "win10_chrome_131_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.139",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "10.0.19045",  # Windows 10 22H2
        "grease": "Google Chrome",
    },
    {
        "id": "win10_chrome_131_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.204",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "10.0.19045",
        "grease": "Chromium",
    },
    {
        "id": "win10_chrome_131_3",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "version": "131.0.6778.85",
        "major": "131",
        "platform": "Windows",
        "platformVersion": "10.0.19044",  # Windows 10 21H2
        "grease": "Google Chrome",
    },

    # --- Chrome 130 Cohort (Windows 11) ---
    {
        "id": "win11_chrome_130_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130.0.6723.116",
        "major": "130",
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "grease": "Google Chrome",
    },
    {
        "id": "win11_chrome_130_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130.0.6723.92",
        "major": "130",
        "platform": "Windows",
        "platformVersion": "10.0.22631",
        "grease": "Chromium",
    },
    {
        "id": "win11_chrome_130_3",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130.0.6723.69",
        "major": "130",
        "platform": "Windows",
        "platformVersion": "10.0.22621",
        "grease": "Google Chrome",
    },

    # --- Chrome 130 Cohort (Windows 10) ---
    {
        "id": "win10_chrome_130_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130.0.6723.116",
        "major": "130",
        "platform": "Windows",
        "platformVersion": "10.0.19045",
        "grease": "Google Chrome",
    },
    {
        "id": "win10_chrome_130_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130.0.6723.92",
        "major": "130",
        "platform": "Windows",
        "platformVersion": "10.0.19045",
        "grease": "Chromium",
    },
    {
        "id": "win10_chrome_130_3",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130.0.6723.58",
        "major": "130",
        "platform": "Windows",
        "platformVersion": "10.0.19044",
        "grease": "Google Chrome",
    },

    # --- Chrome 129 Cohort (Windows 11) ---
    {
        "id": "win11_chrome_129_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "version": "129.0.6668.100",
        "major": "129",
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "grease": "Google Chrome",
    },
    {
        "id": "win11_chrome_129_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "version": "129.0.6668.89",
        "major": "129",
        "platform": "Windows",
        "platformVersion": "10.0.22631",
        "grease": "Chromium",
    },
    {
        "id": "win11_chrome_129_3",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "version": "129.0.6668.71",
        "major": "129",
        "platform": "Windows",
        "platformVersion": "10.0.22621",
        "grease": "Google Chrome",
    },

    # --- Chrome 129 Cohort (Windows 10) ---
    {
        "id": "win10_chrome_129_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "version": "129.0.6668.100",
        "major": "129",
        "platform": "Windows",
        "platformVersion": "10.0.19045",
        "grease": "Google Chrome",
    },
    {
        "id": "win10_chrome_129_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "version": "129.0.6668.89",
        "major": "129",
        "platform": "Windows",
        "platformVersion": "10.0.19044",
        "grease": "Chromium",
    },

    # --- Chrome 128 Cohort (Windows 11) ---
    {
        "id": "win11_chrome_128_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "version": "128.0.6613.137",
        "major": "128",
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "grease": "Google Chrome",
    },
    {
        "id": "win11_chrome_128_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "version": "128.0.6613.119",
        "major": "128",
        "platform": "Windows",
        "platformVersion": "10.0.22631",
        "grease": "Chromium",
    },

    # --- Chrome 128 Cohort (Windows 10) ---
    {
        "id": "win10_chrome_128_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "version": "128.0.6613.137",
        "major": "128",
        "platform": "Windows",
        "platformVersion": "10.0.19045",
        "grease": "Google Chrome",
    },
    {
        "id": "win10_chrome_128_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "version": "128.0.6613.119",
        "major": "128",
        "platform": "Windows",
        "platformVersion": "10.0.19044",
        "grease": "Chromium",
    },

    # --- Chrome 132 Cohort (Modern Stable on Windows 11 & 10) ---
    {
        "id": "win11_chrome_132_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "version": "132.0.6834.110",
        "major": "132",
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "grease": "Google Chrome",
    },
    {
        "id": "win11_chrome_132_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "version": "132.0.6834.83",
        "major": "132",
        "platform": "Windows",
        "platformVersion": "10.0.22631",
        "grease": "Chromium",
    },
    {
        "id": "win10_chrome_132_1",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "version": "132.0.6834.110",
        "major": "132",
        "platform": "Windows",
        "platformVersion": "10.0.19045",
        "grease": "Google Chrome",
    },
    {
        "id": "win10_chrome_132_2",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "version": "132.0.6834.83",
        "major": "132",
        "platform": "Windows",
        "platformVersion": "10.0.19044",
        "grease": "Chromium",
    },
]


def get_profile_for_account(identifier: str) -> Dict[str, Any]:
    """
    Returns a deterministic, sticky profile for a given account or port.
    Guarantees the account always uses the same realistic fingerprint across runs.
    """
    clean_id = (identifier or "default").strip().lower()
    h = int(hashlib.sha256(clean_id.encode("utf-8")).hexdigest(), 16)
    idx = h % len(PROFILES)
    base = PROFILES[idx]

    major = base["major"]
    full_ver = base["version"]
    plat = base["platform"]
    plat_ver = base["platformVersion"]

    # Construct cryptographic User-Agent Client Hints (UA-CH) metadata
    user_agent_metadata = {
        "brands": [
            {"brand": "Google Chrome", "version": major},
            {"brand": "Chromium", "version": major},
            {"brand": "Not_A Brand", "version": "24"}
        ],
        "fullVersionList": [
            {"brand": "Google Chrome", "version": full_ver},
            {"brand": "Chromium", "version": full_ver},
            {"brand": "Not_A Brand", "version": "24.0.0.0"}
        ],
        "platform": plat,
        "platformVersion": plat_ver,
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False
    }

    return {
        "id": base["id"],
        "userAgent": base["userAgent"],
        "version": full_ver,
        "major": major,
        "platformVersion": plat_ver,
        "userAgentMetadata": user_agent_metadata
    }


def get_user_agent_arg(identifier: str) -> str:
    """Returns the --user-agent=... flag for Chrome CLI launch."""
    profile = get_profile_for_account(identifier)
    return f"--user-agent={profile['userAgent']}"


async def apply_user_agent_to_page(page, identifier: str) -> None:
    """
    Applies synchronized User-Agent and Client Hints via CDP to both
    Network (HTTP headers) and Emulation (DOM navigator.userAgentData).
    """
    try:
        profile = get_profile_for_account(identifier)
        cdp = await page.context.new_cdp_session(page)
        
        await cdp.send("Emulation.setUserAgentOverride", {
            "userAgent": profile["userAgent"],
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "Win32",
            "userAgentMetadata": profile["userAgentMetadata"]
        })
        await cdp.send("Network.setUserAgentOverride", {
            "userAgent": profile["userAgent"],
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "Win32",
            "userAgentMetadata": profile["userAgentMetadata"]
        })
        log.info(f"[UA] Synchronized profile '{profile['id']}' (Chrome {profile['major']} on Win {profile['platformVersion']}) for '{identifier}'")
    except Exception as e:
        log.warning(f"[UA] Could not apply CDP UserAgentOverride for '{identifier}': {e}")
