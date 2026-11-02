"""
Centralized configuration â€” paths, constants, and account loading.

All path constants are resolved relative to the project root (the directory
containing accounts.json, .env, gui.py, etc.).
"""

import json
import sys
from pathlib import Path
from datetime import datetime

# â”€â”€â”€ Project root â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The project root is two levels up from this file:
#   src/common/config.py  â†’  src/  â†’  bot/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

ACCOUNTS_FILE = PROJECT_ROOT / "accounts.json"
SLOT_ALERT_STATE_FILE = PROJECT_ROOT / "slot_alert_state.json"
LOGS_DIR = PROJECT_ROOT / "logs"

# â”€â”€â”€ Shared constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DATE_FORMATS = [
    "%d %b %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
]

# Canonical city names used internally throughout the system.
# The monitor normalizes all incoming API data to these names.
# The executor then maps them to browser-specific names (e.g. DELHI â†’ NEW DELHI)
# when communicating with the browser browser.
CITY_ALIASES = {
    "MUMBAI": "MUMBAI",
    "MUMBAI VAC": "MUMBAI",
    "CHENNAI": "CHENNAI",
    "CHENNAI VAC": "CHENNAI",
    "HYDERABAD": "HYDERABAD",
    "HYDERABAD VAC": "HYDERABAD",
    "DELHI": "DELHI",
    "DELHI VAC": "DELHI",
    "NEW DELHI": "DELHI",
    "NEW DELHI VAC": "DELHI",
    "KOLKATA": "KOLKATA",
    "KOLKATA VAC": "KOLKATA",
}

ALL_CONSULATES = ["CHENNAI", "HYDERABAD", "MUMBAI", "DELHI", "KOLKATA"]

# â”€â”€â”€ Account loading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def load_accounts() -> list[dict]:
    """Load and validate the accounts.json file.
    
    Returns the raw list of account dicts.
    Exits the process if the file is missing or malformed.
    """
    if not ACCOUNTS_FILE.exists():
        print(f"[CONFIG] âŒ  accounts.json not found at {ACCOUNTS_FILE}")
        sys.exit(1)
    with ACCOUNTS_FILE.open(encoding="utf-8") as f:
        accounts = json.load(f)
    if not isinstance(accounts, list) or not accounts:
        print("[CONFIG] âŒ  accounts.json must be a non-empty JSON array.")
        sys.exit(1)
    return accounts


def parse_date(value) -> datetime | None:
    """Parse a date string in any of the common formats.
    
    Returns a datetime object or None if parsing fails.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        if hasattr(value, "to_pydatetime"):
            return value.to_pydatetime()
    except Exception:
        pass
    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def normalize_city(value: str) -> str:
    """Normalize a city name to its canonical internal form.
    
    E.g. "NEW DELHI VAC" â†’ "DELHI", "MUMBAI VAC" â†’ "MUMBAI", "ANY" â†’ "ANY"
    """
    raw = str(value or "").strip().upper()
    if raw == "ANY":
        return "ANY"
    return CITY_ALIASES.get(raw, raw)

