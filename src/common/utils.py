"""
Shared utility functions used across multiple modules.
"""

import re
from datetime import datetime

# Timestamp format used by every log line across the project.
# Example output: "31 Aug 26, 06:33:31 PM"
LOG_TIME_FORMAT = "%d %b %y, %I:%M:%S %p"


def log_stamp(dt: datetime | None = None) -> str:
    """Format a timestamp (now by default) for log lines."""
    return (dt or datetime.now()).strftime(LOG_TIME_FORMAT)


def safe_id(username: str) -> str:
    """Generate a filesystem-safe unique identifier from a username/email.
    
    Replaces any character that is not alphanumeric with an underscore.
    Used for state file names, Chrome profile directories, etc.
    """
    return re.sub(r'[^a-zA-Z0-9]', '_', str(username))


def _fix_mojibake(s: str) -> str:
    """Attempt to repair common UTF-8 mojibake (UTF-8 bytes decoded as latin-1).

    Many garbled strings like "âŒ" come from UTF-8 being interpreted with a
    single-byte encoding. This helper tries a latin-1 -> utf-8 re-decode and
    falls back to the original on failure.
    """
    if not isinstance(s, str) or not s:
        return s
    try:
        repaired = s.encode('latin-1').decode('utf-8')
        # If decoding produced replacement characters, consider it a failure.
        if '\ufffd' in repaired:
            return s
        return repaired
    except Exception:
        return s


def sanitize_text(s: str) -> str:
    """Clean/normalize text for logs and Telegram messages.

    1. Try to repair mojibake (latin-1 -> utf-8 symptom).
    2. Collapse excessive whitespace and remove stray control chars.
    """
    if s is None:
        return ""
    try:
        text = str(s)
    except Exception:
        return ""
    text = _fix_mojibake(text)
    # Remove ASCII control characters except common whitespace (tab/newline)
    text = ''.join(ch for ch in text if (ch == '\t' or ch == '\n' or ch == '\r' or ord(ch) >= 32))
    # Preserve newlines: normalize horizontal whitespace line-by-line
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(lines)
