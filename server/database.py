import sqlite3
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from config import Config

Config.DATA_DIR.mkdir(parents=True, exist_ok=True)

def get_db():
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables and optionally migrate from existing accounts.json."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        action_mode TEXT DEFAULT 'SNIPER',
        role TEXT DEFAULT 'RESERVED_BOOKING',
        ofc_cities TEXT DEFAULT '[]',
        ofc_start_date TEXT DEFAULT '',
        ofc_end_date TEXT DEFAULT '',
        consular_cities TEXT DEFAULT '[]',
        consular_start_date TEXT DEFAULT '',
        consular_end_date TEXT DEFAULT '',
        security_questions TEXT DEFAULT '{}',
        prevent_immediate INTEGER DEFAULT 0,
        multi_person INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        booking_status TEXT DEFAULT 'IDLE',
        booked_ofc_date TEXT DEFAULT '',
        booked_consular_date TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT
    )
    """)
    for col in ("booking_status", "booked_ofc_date", "booked_consular_date"):
        try:
            cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS triggers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city TEXT NOT NULL,
        slot_date TEXT NOT NULL,
        slot_time TEXT DEFAULT '',
        slot_type TEXT DEFAULT 'OFC',
        matched_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'PROCESSED',
        created_at TEXT
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS booking_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger_id INTEGER,
        username TEXT NOT NULL,
        customer_name TEXT,
        city TEXT NOT NULL,
        slot_date TEXT NOT NULL,
        slot_time TEXT DEFAULT '',
        status TEXT DEFAULT 'PENDING',
        notes TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (trigger_id) REFERENCES triggers (id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS slot_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city TEXT NOT NULL,
        slot_date TEXT NOT NULL,
        slot_time TEXT DEFAULT '',
        quantity INTEGER DEFAULT 1,
        slot_type TEXT DEFAULT 'OFC',
        discovered_by TEXT DEFAULT '',
        status TEXT DEFAULT 'AVAILABLE',
        created_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS booking_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage TEXT NOT NULL,
        username TEXT NOT NULL,
        customer_name TEXT,
        city TEXT NOT NULL,
        slot_date TEXT NOT NULL,
        slot_time TEXT DEFAULT '',
        status TEXT NOT NULL,
        notes TEXT DEFAULT '',
        created_at TEXT
    )
    """)

    cursor.execute("""
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
    
    conn.commit()

    
    # Auto-seed from existing accounts.json if table is empty
    cursor.execute("SELECT COUNT(*) FROM accounts")
    count = cursor.fetchone()[0]
    if count == 0:
        seed_path = Config.BASE_DIR.parent / "accounts.json"
        if not seed_path.exists():
            seed_path = Config.ACCOUNTS_BACKUP_JSON
        if seed_path.exists():
            try:
                with open(seed_path, "r", encoding="utf-8") as f:
                    seed_accounts = json.load(f)
                now = datetime.now().isoformat()
                for acc in seed_accounts:
                    cursor.execute("""
                    INSERT OR IGNORE INTO accounts (
                        customer_name, username, password, action_mode, role,
                        ofc_cities, ofc_start_date, ofc_end_date,
                        consular_cities, consular_start_date, consular_end_date,
                        security_questions, prevent_immediate, multi_person, enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        acc.get("customer_name") or acc.get("username", ""),
                        acc.get("username", ""),
                        acc.get("password", ""),
                        acc.get("action_mode", "SNIPER"),
                        acc.get("role", "RESERVED_BOOKING"),
                        json.dumps(acc.get("ofcCities", [])),
                        acc.get("ofcStartDate", ""),
                        acc.get("ofcEndDate", ""),
                        json.dumps(acc.get("consularCities", [])),
                        acc.get("consularStartDate", ""),
                        acc.get("consularEndDate", ""),
                        json.dumps(acc.get("security_questions", {})),
                        1 if acc.get("prevent_immediate") else 0,
                        1 if acc.get("multiPerson") else 0,
                        1 if acc.get("enabled", True) else 0,
                        now, now
                    ))
                conn.commit()
            except Exception as e:
                print(f"[DB] Could not seed accounts: {e}")
    conn.close()
    clean_booking_queue()
    _sync_to_backup_json()

def _account_row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    try:
        d["ofcCities"] = json.loads(d.get("ofc_cities") or "[]")
    except Exception:
        d["ofcCities"] = []
    try:
        d["consularCities"] = json.loads(d.get("consular_cities") or "[]")
    except Exception:
        d["consularCities"] = []
    try:
        d["security_questions"] = json.loads(d.get("security_questions") or "{}")
    except Exception:
        d["security_questions"] = {}
        
    d["prevent_immediate"] = bool(d.get("prevent_immediate", 0))
    d["multiPerson"] = bool(d.get("multi_person", 0))
    d["enabled"] = bool(d.get("enabled", 1))
    d["ofcStartDate"] = d.get("ofc_start_date") or ""
    d["ofcEndDate"] = d.get("ofc_end_date") or ""
    d["consularStartDate"] = d.get("consular_start_date") or ""
    d["consularEndDate"] = d.get("consular_end_date") or ""
    d["booking_status"] = d.get("booking_status") or "IDLE"
    d["booked_ofc_date"] = d.get("booked_ofc_date") or ""
    d["booked_consular_date"] = d.get("booked_consular_date") or ""
    d["booked"] = bool(d.get("booking_status") == "BOOKED SUCCESS" or d.get("role") == "COMPLETED")
    return d

def get_all_accounts(enabled_only=False, role=None):
    # Automatically sync live roles from root accounts.json if updated by Auto Hot-Swap
    try:
        root_acc = Config.BASE_DIR.parent / "accounts.json"
        if root_acc.exists():
            with open(root_acc, "r", encoding="utf-8") as rf:
                r_accs = json.load(rf)
            conn_sync = get_db()
            cur_sync = conn_sync.cursor()
            changed = False
            for ra in r_accs:
                u = ra.get("username")
                r = ra.get("role")
                if u and r:
                    cur_sync.execute("UPDATE accounts SET role = ? WHERE username = ? AND role != ?", (r, u, r))
                    if cur_sync.rowcount > 0:
                        changed = True
            if changed:
                conn_sync.commit()
            conn_sync.close()
    except Exception:
        pass

    conn = get_db()
    cursor = conn.cursor()
    query = "SELECT * FROM accounts WHERE 1=1"
    params = []
    if enabled_only:
        query += " AND enabled = 1 AND booking_status != 'BOOKED SUCCESS'"
    if role:
        query += " AND role = ?"
        params.append(role)
    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [_account_row_to_dict(r) for r in rows]

def get_account_by_username(username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return _account_row_to_dict(row)

def add_account(data):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    
    ofc_cities = json.dumps(data.get("ofcCities", []))
    consular_cities = json.dumps(data.get("consularCities", []))
    security_questions = json.dumps(data.get("security_questions", {}))
    
    cursor.execute("""
    INSERT INTO accounts (
        customer_name, username, password, action_mode, role,
        ofc_cities, ofc_start_date, ofc_end_date,
        consular_cities, consular_start_date, consular_end_date,
        security_questions, prevent_immediate, multi_person, enabled,
        created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("customer_name") or data.get("username", ""),
        data.get("username", "").strip(),
        data.get("password", ""),
        data.get("action_mode", "SNIPER"),
        data.get("role", "RESERVED_BOOKING"),
        ofc_cities,
        data.get("ofcStartDate", ""),
        data.get("ofcEndDate", ""),
        consular_cities,
        data.get("consularStartDate", ""),
        data.get("consularEndDate", ""),
        security_questions,
        1 if data.get("prevent_immediate") else 0,
        1 if data.get("multiPerson") else 0,
        1 if data.get("enabled", True) else 0,
        now, now
    ))
    conn.commit()
    conn.close()
    _sync_to_backup_json()
    return True

def update_account(username, data):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    
    fields = []
    params = []
    
    field_mappings = {
        "customer_name": "customer_name",
        "password": "password",
        "action_mode": "action_mode",
        "role": "role",
        "ofcStartDate": "ofc_start_date",
        "ofcEndDate": "ofc_end_date",
        "consularStartDate": "consular_start_date",
        "consularEndDate": "consular_end_date",
    }
    for k, db_col in field_mappings.items():
        if k in data:
            fields.append(f"{db_col} = ?")
            params.append(data[k])
            
    if "ofcCities" in data:
        fields.append("ofc_cities = ?")
        params.append(json.dumps(data["ofcCities"]))
    if "consularCities" in data:
        fields.append("consular_cities = ?")
        params.append(json.dumps(data["consularCities"]))
    if "security_questions" in data:
        fields.append("security_questions = ?")
        params.append(json.dumps(data["security_questions"]))
    if "prevent_immediate" in data:
        fields.append("prevent_immediate = ?")
        params.append(1 if data["prevent_immediate"] else 0)
    if "multiPerson" in data:
        fields.append("multi_person = ?")
        params.append(1 if data["multiPerson"] else 0)
    if "enabled" in data:
        fields.append("enabled = ?")
        params.append(1 if data["enabled"] else 0)
        
    fields.append("updated_at = ?")
    params.append(now)
    params.append(username)
    
    query = f"UPDATE accounts SET {', '.join(fields)} WHERE username = ?"
    cursor.execute(query, params)
    conn.commit()
    conn.close()
    _sync_to_backup_json()
    return True

def delete_account(username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM accounts WHERE username = ?", (username,))
    # Also remove any queue items for this deleted account
    cursor.execute("DELETE FROM booking_queue WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    _sync_to_backup_json()
    clean_booking_queue()
    return True

def toggle_account(username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE accounts SET enabled = 1 - enabled, updated_at = ? WHERE username = ?", (datetime.now().isoformat(), username))
    conn.commit()
    conn.close()
    _sync_to_backup_json()
    return True

def mark_account_booked(username, ofc_date="", consular_date="", ofc_city="", consular_city=""):
    """Permanently sets account to BOOKED SUCCESS, disabled, role=COMPLETED."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
        UPDATE accounts
        SET enabled = 0,
            role = 'COMPLETED',
            booking_status = 'BOOKED SUCCESS',
            booked_ofc_date = ?,
            booked_consular_date = ?,
            updated_at = ?
        WHERE username = ?
        """, (ofc_date, consular_date, now, username))
        conn.commit()
        conn.close()
        _sync_to_backup_json()
        return True
    except Exception as e:
        print(f"[DB] Error marking account booked: {e}")
        return False

def mark_account_ofc_booked(username, ofc_date="", ofc_city=""):
    """Sets account to OFC_BOOKED_PENDING_CONSULAR."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
        UPDATE accounts
        SET booking_status = 'OFC_BOOKED_PENDING_CONSULAR',
            booked_ofc_date = ?,
            updated_at = ?
        WHERE username = ?
        """, (ofc_date, now, username))
        conn.commit()
        conn.close()
        _sync_to_backup_json()
        return True
    except Exception as e:
        print(f"[DB] Error marking account OFC booked: {e}")
        return False

def mark_account_consular_timeout(username):
    """Resets account after 32m consular window expires."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
        UPDATE accounts
        SET booking_status = 'IDLE',
            booked_ofc_date = '',
            updated_at = ?
        WHERE username = ?
        """, (now, username))
        conn.commit()
        conn.close()
        _sync_to_backup_json()
        return True
    except Exception as e:
        print(f"[DB] Error resetting account after timeout: {e}")
        return False

def clean_booking_queue():
    """
    Clean booking queue:
    1. Delete queue entries for accounts that do not exist in accounts table.
    2. Remove orphaned triggers.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM booking_queue WHERE username NOT IN (SELECT username FROM accounts)")
        cursor.execute("DELETE FROM triggers WHERE id NOT IN (SELECT DISTINCT trigger_id FROM booking_queue WHERE trigger_id IS NOT NULL)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] Could not clean booking queue: {e}")

def clear_booking_queue():
    """
    Completely clear / purge all entries from booking queue table
    and reset root booking_queue.json so operator has a fresh queue.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM booking_queue")
        conn.commit()
        conn.close()
        
        # Also synchronize root booking_queue.json
        try:
            root_queue_file = Config.BASE_DIR.parent / "booking_queue.json"
            with open(root_queue_file, "w", encoding="utf-8") as f:
                json.dump({"running": None, "queue": [], "total_queued": 0, "updated_at": datetime.now().timestamp()}, f, indent=2)
        except Exception as ex_json:
            print(f"[DB] Could not reset root booking_queue.json: {ex_json}")
        return True
    except Exception as e:
        print(f"[DB] Could not clear booking queue: {e}")
        return False

def delete_queue_item(queue_id):
    """Delete a single task from booking queue by ID."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM booking_queue WHERE id = ?", (queue_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Could not delete queue item #{queue_id}: {e}")
        return False

def clear_triggers_history():
    """Delete all trigger history entries."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM triggers")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Could not clear triggers: {e}")
        return False

def _sync_to_backup_json():
    """Keep BOTH data/accounts.json AND root accounts.json in sync with database."""
    try:
        accounts = get_all_accounts()
        # 1. Server data backup json
        with open(Config.ACCOUNTS_BACKUP_JSON, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
        # 2. Project root accounts.json for Desktop Monitor & Orchestrator
        root_path = Config.BASE_DIR.parent / "accounts.json"
        with open(root_path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
    except Exception as e:
        print(f"[DB] Could not sync accounts.json: {e}")

def _normalize_city_name(city_str: str) -> str:
    raw = str(city_str or "").strip().upper()
    if raw in ("DELHI", "DELHI VAC", "NEW DELHI VAC"):
        return "NEW DELHI"
    if raw.endswith(" VAC"):
        return raw[:-4].strip()
    return raw

def get_monitoring_targets():
    """Extract consolidated monitoring targets across all enabled accounts."""
    accounts = get_all_accounts(enabled_only=True)
    all_ofc_cities = set()
    all_consular_cities = set()
    earliest_date = None
    latest_date = None
    
    for acc in accounts:
        for c in acc.get("ofcCities", []):
            all_ofc_cities.add(_normalize_city_name(c))
        for c in acc.get("consularCities", []):
            all_consular_cities.add(_normalize_city_name(c))
            
        s_date = acc.get("ofcStartDate")
        e_date = acc.get("ofcEndDate")
        if s_date:
            if not earliest_date or s_date < earliest_date:
                earliest_date = s_date
        if e_date:
            if not latest_date or e_date > latest_date:
                latest_date = e_date
                
    return {
        "ofcCities": sorted(list(all_ofc_cities)) if all_ofc_cities else ["CHENNAI", "MUMBAI", "HYDERABAD", "NEW DELHI", "KOLKATA"],
        "consularCities": sorted(list(all_consular_cities)),
        "minDate": earliest_date or datetime.now().strftime("%Y-%m-%d"),
        "maxDate": latest_date or (datetime.now().replace(year=datetime.now().year + 1)).strftime("%Y-%m-%d"),
        "total_active_accounts": len(accounts)
    }

def record_trigger_and_queue(city, slot_date, slot_time="", slot_type="OFC", quantity=1, discovered_by=""):
    """
    Called when a slot is detected. Finds all accounts whose criteria match this slot,
    records the trigger, saves to slot_history, and puts matching accounts into the booking queue.
    CRITICAL RULE: Consular slots can ONLY be booked by the account with an active OFC hold!
    """
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    
    city_clean = _normalize_city_name(city)
    is_consular_slot = (str(slot_type).strip().upper() == "CONSULAR")
    accounts = get_all_accounts(enabled_only=True)
    matched_accounts = []
    
    # Priority sorting: RESERVED_BOOKING first
    accounts.sort(key=lambda a: 0 if a.get("role") == "RESERVED_BOOKING" else 1)

    if is_consular_slot:
        # CRITICAL CHECK FOR CONSULAR SLOTS:
        # Consular slots are strictly reserved for accounts holding an active OFC booking within 32 min!
        ofc_holding_accounts = []
        for acc in accounts:
            uname = acc.get("username", "")
            u_id = re.sub(r'[^a-zA-Z0-9_]', '_', uname).strip('_')
            s_file = Config.BASE_DIR.parent / "src" / f"state_{u_id}.json"
            is_waiting = False
            ofc_booked_at = 0
            if s_file.exists():
                try:
                    with open(s_file, "r", encoding="utf-8") as sf:
                        s_data = json.load(sf)
                        if s_data.get("waitingForConsular"):
                            is_waiting = True
                            ofc_booked_at = float(s_data.get("ofcBookedAt") or s_data.get("waitStartTime") or 0)
                except Exception:
                    pass
            if not is_waiting and acc.get("booking_status") == "OFC_BOOKED_PENDING_CONSULAR":
                is_waiting = True
                ofc_booked_at = float(acc.get("ofc_booked_at") or 0)

            # Check 32-minute window (1920s)
            if is_waiting and (time.time() - ofc_booked_at < 1920.0):
                ofc_holding_accounts.append(acc)

        if not ofc_holding_accounts:
            print(f"[TRIGGER] Consular slot found ({city_clean} on {slot_date}), but NO account currently holds an active OFC booking within 32m window. Trigger ignored.")
            conn.close()
            return {
                "trigger_id": None,
                "matched_count": 0,
                "queue_entries": []
            }
        accounts_to_check = ofc_holding_accounts
    else:
        # For OFC slots: Skip accounts holding an OFC appointment (waiting for consular)
        accounts_to_check = []
        for acc in accounts:
            if acc.get("booking_status") == "OFC_BOOKED_PENDING_CONSULAR":
                continue
            uname = acc.get("username", "")
            u_id = re.sub(r'[^a-zA-Z0-9_]', '_', uname).strip('_')
            s_file = Config.BASE_DIR.parent / "src" / f"state_{u_id}.json"
            if s_file.exists():
                try:
                    with open(s_file, "r", encoding="utf-8") as sf:
                        s_data = json.load(sf)
                        if s_data.get("waitingForConsular"):
                            continue
                except Exception:
                    pass
            accounts_to_check.append(acc)
    
    for acc in accounts_to_check:
        if is_consular_slot:
            raw_c = acc.get("consularCities") or acc.get("ofcCities") or []
            cities = [_normalize_city_name(c) for c in raw_c]
            s_date = acc.get("consularStartDate") or acc.get("ofcStartDate", "")
            e_date = acc.get("consularEndDate") or acc.get("ofcEndDate", "")
        else:
            raw_c = acc.get("ofcCities", [])
            cities = [_normalize_city_name(c) for c in raw_c]
            s_date = acc.get("ofcStartDate", "")
            e_date = acc.get("ofcEndDate", "")
        
        # Check city match (both sides canonical)
        if cities and city_clean not in cities:
            continue
            
        # Check date range match
        if s_date and slot_date < s_date:
            continue
        if e_date and slot_date > e_date:
            continue
            
        matched_accounts.append(acc)
        
    # Record trigger
    cursor.execute("""
    INSERT INTO triggers (city, slot_date, slot_time, slot_type, matched_count, status, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (city_clean, slot_date, slot_time, slot_type, len(matched_accounts), "PROCESSED", now))
    trigger_id = cursor.lastrowid

    # Record into Slot History table
    cursor.execute("""
    INSERT INTO slot_history (city, slot_date, slot_time, quantity, slot_type, discovered_by, status, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (city_clean, slot_date, slot_time, int(quantity or 1), slot_type, discovered_by or "MONITOR", "FOUND", now))
    
    # Insert matching accounts into booking queue
    queue_entries = []
    for acc in matched_accounts:
        cursor.execute("""
        INSERT INTO booking_queue (trigger_id, username, customer_name, city, slot_date, slot_time, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
        """, (
            trigger_id,
            acc.get("username"),
            acc.get("customer_name"),
            city_clean,
            slot_date,
            slot_time,
            f"Matched {slot_type} in {city_clean} on {slot_date}",
            now, now
        ))
        queue_entries.append({
            "id": cursor.lastrowid,
            "username": acc.get("username"),
            "customer_name": acc.get("customer_name"),
            "city": city_clean,
            "slot_date": slot_date,
            "slot_time": slot_time
        })
        
    conn.commit()
    conn.close()
    
    return {
        "trigger_id": trigger_id,
        "matched_count": len(matched_accounts),
        "queue_entries": queue_entries
    }

def get_queue(limit=50, status=None):
    conn = get_db()
    cursor = conn.cursor()
    query = "SELECT * FROM booking_queue WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_queue_status(queue_id, status, notes=""):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
    UPDATE booking_queue SET status = ?, notes = ?, updated_at = ? WHERE id = ?
    """, (status, notes, now, queue_id))
    conn.commit()
    conn.close()
    return True

def get_recent_triggers(limit=20):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM triggers ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_slot_history(limit=100):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM slot_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def record_slot_history(city, slot_date, slot_time="", quantity=1, slot_type="OFC", discovered_by="", status="AVAILABLE"):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
    INSERT INTO slot_history (city, slot_date, slot_time, quantity, slot_type, discovered_by, status, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (city.strip().upper(), slot_date, slot_time, int(quantity or 1), slot_type, discovered_by or "MONITOR", status, now))
    conn.commit()
    sid = cursor.lastrowid
    conn.close()
    return sid

def clear_slot_history():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM slot_history")
    conn.commit()
    conn.close()
    return True

def get_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM accounts")
    total_accounts = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 1")
    active_accounts = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM triggers")
    total_triggers = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM booking_queue WHERE status = 'PENDING'")
    pending_queue = cursor.fetchone()[0]
    conn.close()
    return {
        "total_accounts": total_accounts,
        "active_accounts": active_accounts,
        "total_triggers": total_triggers,
        "pending_queue": pending_queue
    }

def record_booking_attempt(stage, username, customer_name="", city="", slot_date="", slot_time="", status="ATTEMPTED", notes=""):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
    INSERT INTO booking_records (stage, username, customer_name, city, slot_date, slot_time, status, notes, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(stage).strip().upper(),
        str(username).strip(),
        str(customer_name or username).strip(),
        str(city).strip().upper(),
        str(slot_date or "").strip(),
        str(slot_time or "").strip(),
        str(status).strip().upper(),
        str(notes or "").strip(),
        now
    ))
    conn.commit()
    record_id = cursor.lastrowid
    conn.close()
    return record_id

def get_booking_records(stage=None, limit=100):
    conn = get_db()
    cursor = conn.cursor()
    if stage:
        cursor.execute("SELECT * FROM booking_records WHERE stage = ? ORDER BY id DESC LIMIT ?", (str(stage).upper(), limit))
    else:
        cursor.execute("SELECT * FROM booking_records ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_booking_record(record_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM booking_records WHERE id = ?", (record_id,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def clear_booking_records(stage=None):
    conn = get_db()
    cursor = conn.cursor()
    if stage:
        cursor.execute("DELETE FROM booking_records WHERE stage = ?", (str(stage).upper(),))
    else:
        cursor.execute("DELETE FROM booking_records")
    conn.commit()
    conn.close()
    return True


# ─── Booked Confirmations Helpers ─────────────────────────────────────

def save_booked_confirmation(
    username: str,
    customer_name: str,
    ofc_city: str,
    ofc_date: str,
    consular_city: str,
    consular_date: str,
    pdf_filename: str,
) -> int:
    now_iso = datetime.now().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO booked_confirmations (
        username, customer_name, ofc_city, ofc_date,
        consular_city, consular_date, pdf_filename, status, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'BOOKED', ?)
    """, (
        username, customer_name, ofc_city, ofc_date,
        consular_city, consular_date, pdf_filename, now_iso
    ))
    record_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return record_id


def get_all_booked_confirmations() -> list:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM booked_confirmations ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


