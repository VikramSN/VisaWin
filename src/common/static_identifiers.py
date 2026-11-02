"""Static Account Identifiers Registry and Persistent Cache.

Manages static and semi-static account identifiers (appd, primaryId, applicationIds)
so that slot monitoring and schedule API calls never need to scrape the browser DOM
or fail when the browser is parked on the homepage.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("static_identifiers")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
IDENTIFIERS_FILE = PROJECT_ROOT / "account_identifiers.json"
ACCOUNTS_FILE = PROJECT_ROOT / "accounts.json"

# Verified static identifiers for known accounts
STATIC_ACCOUNT_IDENTIFIERS: Dict[str, Dict[str, str]] = {
    "dineshkumar.usa": {
        "appd": "80fcba11-cf7a-f111-ab0f-001dd80c4770",
        "primaryId": "82ae3d4a-44ac-f111-a3d0-001dd81190ea",
    },
    "RAHULBHARDWAJ.USA": {
        "appd": "3ea5dfdf-ce7a-f111-ab0e-001dd80edd16",
        "primaryId": "3ea5dfdf-ce7a-f111-ab0e-001dd80edd16",
    },
    "fnu vinod": {
        "appd": "7df5a5e7-4a9d-f111-b8dc-001dd8064036",
        "primaryId": "7df5a5e7-4a9d-f111-b8dc-001dd8064036",
    },
    "cherkala00000.usa": {
        "appd": "194c51ce-6662-f111-a823-001dd80138a7",
        "primaryId": "e551d652-0f6b-f111-9b47-001dd80a7720",
    },
    "Bhavishya001": {
        "appd": "021c0f37-f466-f111-ab0b-001dd80eb685",
        "primaryId": "32228c13-9da7-f111-a3d0-001dd8305aaa",
    },
    "manishongc": {
        "appd": "e4abe07c-b146-f111-bec6-001dd80db40a",
        "primaryId": "023873f5-9da7-f111-a3d0-001dd811f380",
    },
    "agarw245": {
        "appd": "bb9f999b-49c6-f011-bbd3-001dd80ff094",
        "primaryId": "0454833c-9da7-f111-a3d0-001dd811f380",
    },
    "Agarwal": {
        "appd": "bb9f999b-49c6-f011-bbd3-001dd80ff094",
        "primaryId": "0454833c-9da7-f111-a3d0-001dd811f380",
    },
    "Shefali1982": {
        "appd": "e5c21135-4f2b-ee11-9965-001dd80634b1",
        "primaryId": "b176aafd-b69b-f111-a3d0-001dd801647e",
    },
    "nitin.official.143": {
        "appd": "1dd02907-2886-f011-b4cc-001dd80beba1",
        "primaryId": "69536487-b65c-f111-9a90-001dd808d4cf",
    },
    "sharad.USA": {
        "appd": "d6991d44-8bae-f111-aaac-001dd80f766c",
        "primaryId": "d6991d44-8bae-f111-aaac-001dd80f766c",
    },
    "ajaybalaji.USA": {
        "appd": "84b71af9-7eab-f111-aaac-001dd80db2d0",
        "primaryId": "402e9791-81ab-f111-a3d0-001dd80042fd",
    },
}


def _load_persistent_identifiers() -> Dict[str, Dict[str, str]]:
    """Load identifiers from disk cache."""
    if not IDENTIFIERS_FILE.exists():
        return {}
    try:
        with open(IDENTIFIERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.warning(f"Error loading {IDENTIFIERS_FILE}: {e}")
    return {}


def get_account_identifiers(
    username: str,
    config: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve (primaryId, appd) for an account from static registries and caches.

    Resolution order:
    1. Explicit values in passed config dict ('appd', 'primaryId', 'primary_id')
    2. Persistent identifiers cache (account_identifiers.json)
    3. Built-in STATIC_ACCOUNT_IDENTIFIERS mapping
    4. accounts.json entries
    """
    clean_user = (username or "").strip()
    config = config or {}

    # Canonicalize username: if a customer_name was passed (e.g. 'Bhavishya'),
    # map it to the actual username (e.g. 'Bhavishya001') via accounts.json
    candidates = [clean_user]
    if ACCOUNTS_FILE.exists():
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
                if isinstance(accts, list):
                    for a in accts:
                        u = str(a.get("username") or "").strip()
                        c = str(a.get("customer_name") or "").strip()
                        if clean_user.lower() in (u.lower(), c.lower()):
                            if u and u not in candidates:
                                candidates.insert(0, u)
                            if c and c not in candidates:
                                candidates.append(c)
                            break
        except Exception:
            pass

    # 1. Config dict
    appd = config.get("appd") or config.get("scheduleGroupId")
    primary_id = config.get("primaryId") or config.get("primary_id") or config.get("applicantId")
    if appd and primary_id:
        return str(primary_id).strip(), str(appd).strip()

    # 2. Persistent identifiers cache (check all candidate names)
    disk_data = _load_persistent_identifiers()
    for name in candidates:
        if name in disk_data:
            entry = disk_data[name]
            appd = appd or entry.get("appd")
            primary_id = primary_id or entry.get("primaryId")
            if appd and primary_id:
                return str(primary_id).strip(), str(appd).strip()
        for k, v in disk_data.items():
            if k.lower() == name.lower():
                appd = appd or v.get("appd")
                primary_id = primary_id or v.get("primaryId")
                if appd and primary_id:
                    return str(primary_id).strip(), str(appd).strip()

    # 3. Static known accounts dictionary
    for name in candidates:
        if name in STATIC_ACCOUNT_IDENTIFIERS:
            entry = STATIC_ACCOUNT_IDENTIFIERS[name]
            appd = appd or entry.get("appd")
            primary_id = primary_id or entry.get("primaryId")
            if appd and primary_id:
                return str(primary_id).strip(), str(appd).strip()
        for k, v in STATIC_ACCOUNT_IDENTIFIERS.items():
            if k.lower() == name.lower():
                appd = appd or v.get("appd")
                primary_id = primary_id or v.get("primaryId")
                if appd and primary_id:
                    return str(primary_id).strip(), str(appd).strip()

    # 4. Check accounts.json
    if ACCOUNTS_FILE.exists():
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
                if isinstance(accts, list):
                    for a in accts:
                        u = str(a.get("username") or "").strip().lower()
                        c = str(a.get("customer_name") or "").strip().lower()
                        if clean_user.lower() in (u, c):
                            appd = appd or a.get("appd")
                            primary_id = primary_id or a.get("primaryId") or a.get("primary_id")
                            if appd and primary_id:
                                return str(primary_id).strip(), str(appd).strip()
        except Exception:
            pass

    # Symmetrical fallback if one is found
    if appd and not primary_id:
        primary_id = appd
    elif primary_id and not appd:
        appd = primary_id

    return primary_id, appd


def save_account_identifiers(username: str, primary_id: str, appd: str) -> None:
    """Save newly discovered identifiers for an account persistently so they become static."""
    clean_user = (username or "").strip()
    if not clean_user or not primary_id or not appd:
        return
    if clean_user in STATIC_ACCOUNT_IDENTIFIERS:
        existing = STATIC_ACCOUNT_IDENTIFIERS[clean_user]
        # If existing has identical dummy primaryId == appd, or discovered has different primaryId/appd, update it
        if existing.get("primaryId") == existing.get("appd") and primary_id != appd:
            STATIC_ACCOUNT_IDENTIFIERS[clean_user]["primaryId"] = primary_id
            STATIC_ACCOUNT_IDENTIFIERS[clean_user]["appd"] = appd
        else:
            return
    data = _load_persistent_identifiers()
    existing = data.get(clean_user, {})
    if existing.get("primaryId") == primary_id and existing.get("appd") == appd:
        return

    data[clean_user] = {
        "primaryId": primary_id,
        "appd": appd,
    }

    try:
        with open(IDENTIFIERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(f"Saved static identifiers for {clean_user} (appd={appd}, primaryId={primary_id})")
    except Exception as e:
        log.warning(f"Failed to persist static identifiers to {IDENTIFIERS_FILE}: {e}")

    # Also update accounts.json if possible
    if ACCOUNTS_FILE.exists():
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
            updated = False
            for a in accts:
                u = str(a.get("username") or "").strip().lower()
                c = str(a.get("customer_name") or "").strip().lower()
                if clean_user.lower() in (u, c):
                    if a.get("appd") != appd or a.get("primaryId") != primary_id:
                        a["appd"] = appd
                        a["primaryId"] = primary_id
                        updated = True
            if updated:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(accts, f, indent=2)
        except Exception as e:
            log.warning(f"Could not update accounts.json with static identifiers: {e}")
