"""Mobile 4G/5G Proxy Controller via Android ADB.

Controls a USB-connected Android phone to auto-rotate IP via Airplane Mode:
1. Enables Airplane Mode (disconnects carrier IP).
2. Waits 3.5 seconds.
3. Disables Airplane Mode (reconnects to carrier and receives a fresh residential IP via CGNAT).
4. Verifies mobile data is enabled.
5. Returns the new IP address.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import urllib.request
import json

log = logging.getLogger("mobile-proxy")

_last_mobile_rotation_ts = 0.0
_mobile_rotation_lock = threading.Lock()
_MIN_MOBILE_ROTATION_INTERVAL = 10.0  # Avoid rapid cycling within 10s


def get_adb_path() -> str:
    """Find adb executable path on the system."""
    custom = os.environ.get("ADB_PATH")
    if custom and os.path.isfile(custom):
        return custom
    # Check default PATH
    try:
        res = subprocess.run(["where", "adb"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0]
    except Exception:
        pass
    # Common Android SDK paths on Windows
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"),
        r"C:\platform-tools\adb.exe",
        r"C:\Program Files\Android\platform-tools\adb.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "adb"


def is_adb_connected(device_id: str | None = None) -> bool:
    """Check if at least one Android device is connected via ADB."""
    adb = get_adb_path()
    try:
        res = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return False
        lines = [line.strip() for line in res.stdout.strip().splitlines()[1:] if line.strip()]
        devices = [line.split()[0] for line in lines if "device" in line and "offline" not in line and "unauthorized" not in line]
        if device_id:
            return device_id in devices
        return len(devices) > 0
    except Exception:
        return False


def get_current_public_ip(timeout: float = 4.0) -> str:
    """Fetch current public IP address."""
    endpoints = [
        "https://api.ipify.org?format=json",
        "https://api4.ipify.org?format=json",
        "https://ifconfig.me/all.json",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ip = data.get("ip") or data.get("ip_addr")
                if ip:
                    return str(ip).strip()
        except Exception:
            continue
    return "UNKNOWN"


def rotate_mobile_ip(reason: str = "", device_id: str | None = None, wait_seconds: float | None = None) -> dict:
    """Auto-toggle Airplane mode on Android phone to acquire a brand-new carrier residential IP.
    
    Returns:
        {"success": bool, "old_ip": str, "new_ip": str, "duration_s": float, "error": str}
    """
    global _last_mobile_rotation_ts
    now = time.time()
    
    with _mobile_rotation_lock:
        if now - _last_mobile_rotation_ts < _MIN_MOBILE_ROTATION_INTERVAL:
            log.info(f"[MOBILE-PROXY] Rotation throttled ({int(_MIN_MOBILE_ROTATION_INTERVAL - (now - _last_mobile_rotation_ts))}s remaining).")
            return {"success": True, "throttled": True, "ip": get_current_public_ip()}

        _last_mobile_rotation_ts = now
        t0 = time.time()
        adb = get_adb_path()
        device_arg = ["-s", device_id] if device_id else []

        old_ip = get_current_public_ip()
        log.info(f"\n================================================================================")
        log.info(f"📱 [MOBILE 4G/5G IP ROTATION] Starting Airplane Mode Cycle | Reason: {reason or 'IP Refresh'}")
        log.info(f"   Current IP: {old_ip}")

        # 1. Verify ADB Device is online
        try:
            chk = subprocess.run([adb] + device_arg + ["devices"], capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in chk.stdout.strip().splitlines()[1:] if l.strip()]
            attached = [l.split()[0] for l in lines if "\tdevice" in l]
            if not attached:
                err_msg = "No authorized Android device attached via ADB (check USB Debugging)!"
                log.error(f"❌ [MOBILE-PROXY] {err_msg}")
                return {"success": False, "error": err_msg, "old_ip": old_ip}
            target_dev = device_id or attached[0]
            device_arg = ["-s", target_dev]
        except Exception as dev_err:
            log.error(f"❌ [MOBILE-PROXY] Failed checking adb devices: {dev_err}")
            return {"success": False, "error": str(dev_err), "old_ip": old_ip}

        cycle_wait = wait_seconds if wait_seconds is not None else float(os.environ.get("MOBILE_AIRPLANE_WAIT_SECONDS", "3.5"))

        # 2. Turn Airplane Mode ON
        log.info("   -> Turning Airplane Mode ON...")
        try:
            # Modern Android (Android 10+)
            subprocess.run([adb] + device_arg + ["shell", "cmd", "connectivity", "airplane-mode", "enable"], capture_output=True, timeout=5)
            # Legacy fallback
            subprocess.run([adb] + device_arg + ["shell", "settings", "put", "global", "airplane_mode_on", "1"], capture_output=True, timeout=5)
            subprocess.run([adb] + device_arg + ["shell", "am", "broadcast", "-a", "android.intent.action.AIRPLANE_MODE", "--ez", "state", "true"], capture_output=True, timeout=5)
        except Exception as e:
            log.warning(f"   Airplane ON non-fatal: {e}")

        time.sleep(cycle_wait)

        # 3. Turn Airplane Mode OFF
        log.info("   -> Turning Airplane Mode OFF...")
        try:
            # Modern Android (Android 10+)
            subprocess.run([adb] + device_arg + ["shell", "cmd", "connectivity", "airplane-mode", "disable"], capture_output=True, timeout=5)
            # Legacy fallback
            subprocess.run([adb] + device_arg + ["shell", "settings", "put", "global", "airplane_mode_on", "0"], capture_output=True, timeout=5)
            subprocess.run([adb] + device_arg + ["shell", "am", "broadcast", "-a", "android.intent.action.AIRPLANE_MODE", "--ez", "state", "false"], capture_output=True, timeout=5)
        except Exception as e:
            log.warning(f"   Airplane OFF non-fatal: {e}")

        # 4. Ensure Mobile Data is enabled
        try:
            subprocess.run([adb] + device_arg + ["shell", "svc", "data", "enable"], capture_output=True, timeout=5)
        except Exception:
            pass

        # 5. Wait for network link reconnection (typically 3-5 seconds for carrier handshake)
        log.info("   -> Waiting for cellular connection & IP assignment (4s)...")
        time.sleep(4.0)

        new_ip = get_current_public_ip()
        dur = round(time.time() - t0, 1)

        if new_ip != "UNKNOWN" and new_ip != old_ip:
            log.info(f"✅ [MOBILE-PROXY] SUCCESS! Clean Residential IP acquired in {dur}s: {old_ip} -> {new_ip}")
            log.info(f"================================================================================\n")
            return {"success": True, "old_ip": old_ip, "new_ip": new_ip, "duration_s": dur}
        else:
            log.info(f"ℹ️ [MOBILE-PROXY] Airplane cycle finished in {dur}s (Active IP: {new_ip}).")
            log.info(f"================================================================================\n")
            return {"success": True, "old_ip": old_ip, "new_ip": new_ip, "duration_s": dur}
