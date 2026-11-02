"""SessionRadar — deterministic round-robin session maintenance and health registry for VIP Snipers.

Features:
- Strict Time Gap: Ensures NO two accounts ever perform a session touch, reload, or re-login simultaneously.
- Minimum inter-account gap (default 150s / 2.5 minutes) protects Local IP from rate limits and Cloudflare alerts.
- Tiered escalation:
    Tier 1: Lightweight Active Touch (in-page fetch to /en-US/, 0% overhead)
    Tier 2: Harmless Page Reload (/en-US/)
    Tier 3: Auto Re-Login (Cloudflare Turnstile + Security Questions)
- Real-time Session Registry: Cross-Account Trigger checks session status and gives first priority
  only to accounts confirmed LIVE!
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("session-radar")

_RADAR_FILE = Path(__file__).resolve().parent.parent / "session_radar.json"
_RADAR_LOCK = Path(__file__).resolve().parent.parent / "session_radar.lock"

# Default configuration
DEFAULT_MIN_GAP_SECONDS = 150.0       # 2.5 minutes between any two accounts
DEFAULT_MAX_IDLE_SECONDS = 1080.0      # 18 minutes maximum before maintenance is required
LEASE_TIMEOUT_SECONDS = 90.0          # Maximum time an account can hold active action token


def _acquire_radar_lock(timeout: float = 3.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            fd = os.open(str(_RADAR_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_RADAR_LOCK) > 6.0:
                    try:
                        os.remove(_RADAR_LOCK)
                        continue
                    except OSError:
                        pass
            except Exception:
                pass
            time.sleep(0.02)
        except OSError:
            time.sleep(0.02)
    return False


def _release_radar_lock() -> None:
    try:
        _RADAR_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def _read_radar() -> dict:
    try:
        if _RADAR_FILE.exists():
            return json.loads(_RADAR_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "last_global_action_at": 0.0,
        "active_account": None,
        "active_action_started_at": 0.0,
        "accounts": {},
        "updated_at": time.time(),
    }


def _write_radar(data: dict) -> None:
    data["updated_at"] = time.time()
    tmp = _RADAR_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(_RADAR_FILE))
    except Exception:
        try:
            _RADAR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


class SessionRadar:
    """Fleet-wide coordinator for round-robin session keepalive and registry."""

    @staticmethod
    def register_account(account_uid: str, customer: str, role: str) -> None:
        """Register account in session radar tracking."""
        if not _acquire_radar_lock():
            return
        try:
            data = _read_radar()
            accs = data.setdefault("accounts", {})
            if account_uid not in accs:
                accs[account_uid] = {
                    "customer": customer,
                    "role": role,
                    "status": "INITIALIZING",
                    "last_verified_at": 0.0,
                    "method": "NONE",
                    "registered_at": time.time(),
                }
            else:
                accs[account_uid]["customer"] = customer
                accs[account_uid]["role"] = role
            _write_radar(data)
        finally:
            _release_radar_lock()

    @staticmethod
    def can_perform_turn(
        account_uid: str,
        min_gap: float = DEFAULT_MIN_GAP_SECONDS,
        max_idle: float = DEFAULT_MAX_IDLE_SECONDS,
    ) -> Tuple[bool, str, float]:
        """Check if account_uid is permitted to take its Round-Robin Session Maintenance Turn.

        Returns (can_act, reason, wait_seconds).
        Enforces strict time gap and orders accounts by staleness (oldest verified first).
        """
        if not _acquire_radar_lock():
            return False, "lock_busy", 1.0

        try:
            data = _read_radar()
            now = time.time()
            last_global = float(data.get("last_global_action_at", 0.0) or 0.0)
            active_acc = data.get("active_account")
            action_started = float(data.get("active_action_started_at", 0.0) or 0.0)

            # 1. Check if another account is currently performing maintenance
            if active_acc and active_acc != account_uid:
                if now - action_started < LEASE_TIMEOUT_SECONDS:
                    return False, f"in_progress_by_{active_acc}", max(5.0, LEASE_TIMEOUT_SECONDS - (now - action_started))
                else:
                    # Stale active action expired; clear it
                    data["active_account"] = None
                    data["active_action_started_at"] = 0.0

            # 2. Strict Time Gap: Has min_gap elapsed since last global action?
            time_since_global = now - last_global
            if time_since_global < min_gap:
                wait_sec = min_gap - time_since_global
                return False, "global_cooldown", wait_sec

            # 3. Check candidate VIP accounts and see whose turn it is
            accs = data.get("accounts", {})
            vip_candidates = [
                (uid, info)
                for uid, info in accs.items()
                if info.get("role") == "RESERVED_BOOKING"
            ]

            if not vip_candidates:
                # If only this account is registered
                return True, "turn_granted", 0.0

            # Sort by staleness (oldest last_verified_at first)
            vip_candidates.sort(key=lambda item: float(item[1].get("last_verified_at", 0.0) or 0.0))

            chosen_uid, chosen_info = vip_candidates[0]
            if chosen_uid != account_uid:
                return False, f"waiting_for_{chosen_uid}", min_gap

            # Account is the oldest and min_gap has passed!
            return True, "turn_granted", 0.0
        finally:
            _release_radar_lock()

    @staticmethod
    def claim_turn(account_uid: str) -> bool:
        """Atomically claim the maintenance action turn."""
        if not _acquire_radar_lock():
            return False
        try:
            data = _read_radar()
            now = time.time()
            data["active_account"] = account_uid
            data["active_action_started_at"] = now
            accs = data.setdefault("accounts", {})
            if account_uid in accs:
                accs[account_uid]["status"] = "REFRESHING"
            _write_radar(data)
            return True
        finally:
            _release_radar_lock()

    @staticmethod
    def finish_turn(
        account_uid: str,
        status: str,
        method: str,
        error: Optional[str] = None,
    ) -> None:
        """Finish the maintenance turn, update session status, and set global cooldown."""
        if not _acquire_radar_lock():
            return
        try:
            data = _read_radar()
            now = time.time()
            data["last_global_action_at"] = now
            if data.get("active_account") == account_uid:
                data["active_account"] = None
                data["active_action_started_at"] = 0.0

            accs = data.setdefault("accounts", {})
            rec = accs.setdefault(account_uid, {})
            rec["status"] = status
            rec["method"] = method
            if status == "LIVE":
                rec["last_verified_at"] = now
                rec["error"] = None
            else:
                rec["error"] = error
            rec["updated_at"] = now
            _write_radar(data)
        finally:
            _release_radar_lock()

    @staticmethod
    def get_account_session_info(account_uid: str) -> dict:
        """Read session info for an account."""
        data = _read_radar()
        accs = data.get("accounts", {})
        info = accs.get(account_uid, {})
        now = time.time()
        last_v = float(info.get("last_verified_at", 0.0) or 0.0)
        age = (now - last_v) if last_v > 0 else 999999.0
        status = info.get("status", "UNKNOWN")
        # Session is considered LIVE if status == "LIVE" and verified within 20 mins (1200s)
        is_live = (status == "LIVE") and (age < 1200.0)
        return {
            "status": status,
            "is_live": is_live,
            "age_seconds": age,
            "age_minutes": round(age / 60.0, 1) if last_v > 0 else 999.0,
            "method": info.get("method", "NONE"),
            "last_verified_at": last_v,
        }

    @staticmethod
    def calculate_round_robin_interval(role: str = "RESERVED_BOOKING") -> float:
        """Calculate estimated minutes before this account gets its next round-robin turn."""
        data = _read_radar()
        accs = data.get("accounts", {})
        count = sum(1 for a in accs.values() if a.get("role") == role)
        count = max(count, 1)
        # Each account takes ~150s gap
        total_cycle_seconds = count * DEFAULT_MIN_GAP_SECONDS
        return round(total_cycle_seconds / 60.0, 1)


session_radar = SessionRadar()
