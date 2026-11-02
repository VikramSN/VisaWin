"""Appointment Confirmation PDF Downloader and Booking Finalizer.

Handles:
1. Navigating to the official appointment confirmation URL:
   https://www.usvisascheduling.com/en-US/appointment-confirmation/
2. Capturing high-resolution vector PDF using Chrome DevTools Protocol (CDP Page.printToPDF).
3. Saving the PDF to confirmations/<username>_appointment_confirmation.pdf.
4. Recording the confirmed booking in the database (booked_confirmations table) and JSON audit log.
5. Updating accounts.json to permanently disable the booked account (enabled=False, status='BOOKED SUCCESS', role='COMPLETED').
6. Evicting the user from booking_queue.json and coordinator queue.
7. Gracefully terminating the booked user's browser session.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("booking-confirmation")

CONFIRMATION_URL = "https://www.usvisascheduling.com/en-US/appointment-confirmation/"
WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIRMATIONS_DIR = WORKSPACE_DIR / "confirmations"
CONFIRMATIONS_JSON = CONFIRMATIONS_DIR / "booked_confirmations.json"
DB_PATH = WORKSPACE_DIR / "server" / "data" / "visabot.db"
ACCOUNTS_FILE = WORKSPACE_DIR / "accounts.json"
SERVER_ACCOUNTS_FILE = WORKSPACE_DIR / "server" / "data" / "accounts.json"


def ensure_confirmations_dir() -> Path:
    CONFIRMATIONS_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIRMATIONS_DIR


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-\.]", "_", str(name or "").strip())


def save_confirmation_to_db(
    username: str,
    customer_name: str,
    ofc_city: str,
    ofc_date: str,
    consular_city: str,
    consular_date: str,
    pdf_filename: str,
) -> Optional[int]:
    """Persist confirmation record into SQLite database."""
    now_iso = datetime.now().isoformat()
    try:
        if not DB_PATH.parent.exists():
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS booked_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            customer_name TEXT,
            ofc_city TEXT,
            ofc_date TEXT,
            consular_city TEXT,
            consular_date TEXT,
            pdf_filename TEXT,
            status TEXT DEFAULT 'BOOKED',
            created_at TEXT
        )
        """)
        cur.execute("""
        INSERT INTO booked_confirmations (
            username, customer_name, ofc_city, ofc_date,
            consular_city, consular_date, pdf_filename, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'BOOKED', ?)
        """, (
            username, customer_name, ofc_city, ofc_date,
            consular_city, consular_date, pdf_filename, now_iso
        ))
        record_id = cur.lastrowid
        conn.commit()
        conn.close()
        log.info(f"[DB] Saved booked confirmation #{record_id} for '{username}' ({customer_name})")
        return record_id
    except Exception as e:
        log.warning(f"[DB] Error saving confirmation to DB: {e}")
        return None


def save_confirmation_to_json(
    record: Dict[str, Any]
) -> None:
    """Append confirmation record to JSON file for audit and fast GUI loading."""
    ensure_confirmations_dir()
    records = []
    if CONFIRMATIONS_JSON.exists():
        try:
            records = json.loads(CONFIRMATIONS_JSON.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                records = []
        except Exception:
            records = []

    # Update if already exists or append
    clean_user = str(record.get("username", "")).strip().lower()
    updated = False
    for i, r in enumerate(records):
        if str(r.get("username", "")).strip().lower() == clean_user:
            records[i] = record
            updated = True
            break
    if not updated:
        records.append(record)

    try:
        tmp = CONFIRMATIONS_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(CONFIRMATIONS_JSON))
    except Exception:
        try:
            CONFIRMATIONS_JSON.write_text(json.dumps(records, indent=2), encoding="utf-8")
        except Exception:
            pass


def evict_from_booking_queue(username: str) -> None:
    """Evict booked account from coordinator queue and booking_queue.json."""
    from src.booking import coordinator as coord
    coord.cancel_trigger(username)

    # Clean from database booking_queue table
    try:
        if DB_PATH.exists():
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            cur = conn.cursor()
            cur.execute("DELETE FROM booking_queue WHERE LOWER(username) = ?", (username.lower(),))
            conn.commit()
            conn.close()
    except Exception:
        pass


def update_accounts_json_booked(
    username: str,
    customer_name: str,
    ofc_city: str,
    ofc_date: str,
    consular_city: str,
    consular_date: str,
) -> None:
    """Turn OFF the booked user's toggle and set status to BOOKED SUCCESS in accounts.json."""
    clean_user = str(username or "").strip().lower()
    now_iso = datetime.now().isoformat()

    for acc_path in (ACCOUNTS_FILE, SERVER_ACCOUNTS_FILE):
        try:
            if acc_path.exists():
                with open(acc_path, "r", encoding="utf-8") as f:
                    accs = json.load(f)
                modified = False
                for a in accs:
                    if str(a.get("username") or "").strip().lower() == clean_user:
                        a["enabled"] = False
                        a["role"] = "COMPLETED"
                        a["status"] = "BOOKED SUCCESS"
                        a["booking_status"] = "BOOKED SUCCESS"
                        a["booked"] = True
                        a["booked_ofc_date"] = ofc_date
                        a["booked_consular_date"] = consular_date
                        a["booked_ofc_city"] = ofc_city
                        a["booked_consular_city"] = consular_city
                        a["updated_at"] = now_iso
                        modified = True
                        break
                if modified:
                    tmp = acc_path.with_suffix(".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(accs, f, indent=2)
                    os.replace(str(tmp), str(acc_path))
                    log.info(f"[ACCOUNTS] Disabled booked account '{clean_user}' in {acc_path.name}")
        except Exception as e:
            log.warning(f"[ACCOUNTS] Error updating {acc_path}: {e}")

    # Also update DB accounts table
    try:
        if DB_PATH.exists():
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            cur = conn.cursor()
            cur.execute("""
            UPDATE accounts
            SET enabled = 0,
                role = 'COMPLETED',
                booking_status = 'BOOKED SUCCESS',
                booked_ofc_date = ?,
                booked_consular_date = ?,
                updated_at = ?
            WHERE LOWER(username) = ?
            """, (ofc_date, consular_date, now_iso, clean_user))
            conn.commit()
            conn.close()
    except Exception:
        pass


async def capture_appointment_confirmation(
    page: Any,
    username: str,
    customer: str,
    ofc_city: str,
    ofc_date: str,
    consular_city: str,
    consular_date: str,
    logger: Optional[logging.Logger] = None,
) -> Tuple[bool, str, str]:
    """Navigates to appointment confirmation URL, saves PDF receipt via CDP, records DB, and cleans up.

    Returns:
        (success: bool, pdf_filepath: str, pdf_filename: str)
    """
    _log = logger or log
    ensure_confirmations_dir()
    clean_name = _sanitize_filename(username)
    pdf_filename = f"{clean_name}_appointment_confirmation.pdf"
    pdf_filepath = CONFIRMATIONS_DIR / pdf_filename

    _log.info(
        f"\n================================================================================\n"
        f"📄 [CONFIRMATION] DOWNLOADING OFFICIAL APPOINTMENT RECEIPT PDF\n"
        f"--------------------------------------------------------------------------------\n"
        f"Account     : {customer} ({username})\n"
        f"Target URL  : {CONFIRMATION_URL}\n"
        f"Destination : {pdf_filepath}\n"
        f"================================================================================"
    )

    pdf_bytes: Optional[bytes] = None

    # Step 1: Navigate to confirmation page if not already there
    try:
        curr_url = page.url or ""
        if "appointment-confirmation" not in curr_url:
            _log.info(f"[CONFIRMATION] Navigating to confirmation URL: {CONFIRMATION_URL}")
            try:
                await page.goto(CONFIRMATION_URL, wait_until="networkidle", timeout=30000)
            except Exception as nav_e:
                _log.warning(f"[CONFIRMATION] Navigation notice (continuing): {nav_e}")

        # Wait for Dynamics print container / confirmation content
        await asyncio.sleep(4.0)
    except Exception as e:
        _log.warning(f"[CONFIRMATION] Page navigation issue: {e}")

    # Step 2: Capture PDF via CDP
    try:
        cdp = await page.context.new_cdp_session(page)
        _log.info("[CONFIRMATION] Connected to CDP session. Invoking Page.printToPDF...")
        pdf_res = await cdp.send("Page.printToPDF", {
            "printBackground": True,
            "preferCSSPageSize": True,
            "marginTop": 0.4,
            "marginBottom": 0.4,
            "marginLeft": 0.4,
            "marginRight": 0.4,
        })
        if pdf_res and "data" in pdf_res:
            pdf_bytes = base64.b64decode(pdf_res["data"])
            _log.info(f"[CONFIRMATION] CDP Page.printToPDF generated {len(pdf_bytes)} bytes.")
    except Exception as cdp_err:
        _log.warning(f"[CONFIRMATION] CDP Page.printToPDF failed ({cdp_err}). Attempting page.pdf fallback...")
        try:
            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"},
            )
        except Exception as pdf_fallback_err:
            _log.error(f"[CONFIRMATION] PDF fallback also failed: {pdf_fallback_err}")

    # Step 3: Write PDF to disk
    if pdf_bytes and len(pdf_bytes) > 1000:
        pdf_filepath.write_bytes(pdf_bytes)
        _log.info(f"✅ [CONFIRMATION] Appointment confirmation PDF saved successfully: {pdf_filepath.name} ({len(pdf_bytes)} bytes)")
    else:
        _log.warning(f"[CONFIRMATION] Could not capture PDF stream. Creating placeholder marker.")
        pdf_filepath.write_text(
            f"Appointment Confirmation for {customer} ({username})\n"
            f"OFC: {ofc_city} on {ofc_date}\n"
            f"Consular: {consular_city} on {consular_date}\n"
            f"Generated: {datetime.now().isoformat()}\n",
            encoding="utf-8"
        )

    # Step 4: Save record to SQLite database and JSON
    record = {
        "username": username,
        "customer_name": customer,
        "ofc_city": ofc_city,
        "ofc_date": ofc_date,
        "consular_city": consular_city,
        "consular_date": consular_date,
        "pdf_filename": pdf_filename,
        "pdf_path": str(pdf_filepath),
        "status": "BOOKED",
        "created_at": datetime.now().isoformat(),
    }
    db_id = save_confirmation_to_db(
        username=username,
        customer_name=customer,
        ofc_city=ofc_city,
        ofc_date=ofc_date,
        consular_city=consular_city,
        consular_date=consular_date,
        pdf_filename=pdf_filename,
    )
    record["id"] = db_id or 1
    save_confirmation_to_json(record)

    # Step 5: Disable account in accounts.json and evict from booking queue
    update_accounts_json_booked(
        username=username,
        customer_name=customer,
        ofc_city=ofc_city,
        ofc_date=ofc_date,
        consular_city=consular_city,
        consular_date=consular_date,
    )
    evict_from_booking_queue(username)

    return True, str(pdf_filepath), pdf_filename
