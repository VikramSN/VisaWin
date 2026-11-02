"""
Decodo Residential proxy - each account gets its OWN forwarder with its OWN IP.
When one IP is blocked, only that account is affected - others stay live.
"""

import os, sys, json, base64, socket, select, logging, threading, time, random, string, zlib, requests
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("proxy")
if not log.handlers and not logging.getLogger().handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%d %b %y, %I:%M:%S %p"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

DEFAULT_SERVER = "isp.decodo.com"
DEFAULT_PORT = 10001
DEFAULT_USER = "spkmd70vt8"
DEFAULT_PASS = "n7fUzdXbiq62qYq~A4"
DEFAULT_SESSION_DURATION = 60

FORWARD_PORT_BASE = int(os.environ.get("PROXY_FORWARD_PORT", "18080"))
HEALTHCHECK_URL = os.environ.get("PROXY_HEALTHCHECK_URL", "https://api.ipify.org?format=json")
HEALTHCHECK_TIMEOUT = 8  # fast failure detection during wide outages

_state_lock = threading.Lock()
_account_forwarders = {}  # {account_id: AccountForwarder}
# Self-heal throttling: only one thread scans for a fresh port at a time and
# at most once per SELF_HEAL_MIN_INTERVAL seconds. Without this, concurrent
# failing connections each ran a full 25-port scan and raced each other's
# registry writes (the 403 self-heal storm).
_self_heal_lock = threading.Lock()
# Decodo Static Residential ISP ports:
# Decodo pool provides 16,000 static ISP proxies (ports 10001 to 26000, corresponding to proxy #1 to #16000).
_RESIDENTIAL_PORTS = list(range(10001, 26001))

# Backward-compatibility empty placeholders for legacy imports:
STATIC_ACCOUNT_PORTS = {}
STATIC_WEBSHARE_SLOTS = {}
_PRIMARY_PORTS = set(range(10001, 10012))


def _get_configured_accounts(active_only: bool = False) -> list:
    """Read accounts.json dynamically and return list of canonical usernames in order."""
    try:
        from src.common.static_identifiers import ACCOUNTS_FILE
        if ACCOUNTS_FILE.exists():
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
            if isinstance(accts, list):
                result = []
                for a in accts:
                    if active_only and not a.get("enabled", True):
                        continue
                    u = str(a.get("username") or "").strip()
                    if u and u not in result:
                        result.append(u)
                return result
    except Exception:
        pass
    return []


def _normalize_account_id(account_id: str) -> str:
    """Resolve display customer_name (e.g. 'Manish') to canonical username ('manishongc')."""
    if not account_id:
        return ""
    clean = str(account_id).strip()
    try:
        from src.common.static_identifiers import ACCOUNTS_FILE
        if ACCOUNTS_FILE.exists():
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
            if isinstance(accts, list):
                for a in accts:
                    c_name = str(a.get("customer_name") or "").strip().lower()
                    u_name = str(a.get("username") or "").strip()
                    if clean.lower() in (c_name, u_name.lower()):
                        return u_name
    except Exception:
        pass
    return clean


def get_account_decodo_port(account_id: str) -> int:
    """Dynamically get primary dedicated Decodo port for an account from accounts.json (10001..10011)."""
    account_id = _normalize_account_id(account_id)
    if not account_id:
        return 10001
    accounts = _get_configured_accounts()
    for idx, u in enumerate(accounts):
        if u.lower() == str(account_id).lower():
            return 10001 + (idx % 11)
    return 10001 + (zlib.crc32(account_id.encode("utf-8")) % 11)


def get_account_role(account_id: str) -> str:
    """Read account role ('POLLING_ONLY' or 'RESERVED_BOOKING') from accounts.json."""
    account_id = _normalize_account_id(account_id)
    try:
        from src.common.static_identifiers import ACCOUNTS_FILE
        if ACCOUNTS_FILE.exists():
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
            if isinstance(accts, list):
                for a in accts:
                    u_name = str(a.get("username") or "").strip().lower()
                    c_name = str(a.get("customer_name") or "").strip().lower()
                    if str(account_id).lower() in (u_name, c_name):
                        return str(a.get("role", "RESERVED_BOOKING")).strip().upper()
    except Exception:
        pass
    return "RESERVED_BOOKING"


def get_poller_accounts() -> list:
    """Return list of active POLLING_ONLY accounts in order."""
    try:
        from src.common.static_identifiers import ACCOUNTS_FILE
        if ACCOUNTS_FILE.exists():
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accts = json.load(f)
            if isinstance(accts, list):
                return [
                    str(a.get("username") or "").strip()
                    for a in accts
                    if a.get("enabled", True) and str(a.get("role", "")).upper() == "POLLING_ONLY"
                ]
    except Exception:
        pass
    return []


def get_primary_webshare_slot(account_id: str) -> int:
    """Get primary dedicated Webshare slot.
    Poller #1 -> Slot 1 (Dedicated Pool: 1..10)
    Poller #2 -> Slot 11 (Dedicated Pool: 11..20)
    """
    account_id = _normalize_account_id(account_id)
    if not account_id:
        return 1
    pollers = get_poller_accounts()
    for idx, p in enumerate(pollers):
        if p.lower() == str(account_id).lower():
            if idx == 0:
                return 1   # Poller #1 starts on Slot 1
            elif idx == 1:
                return 11  # Poller #2 starts on Slot 11
            else:
                return 1 + (idx * 5) % 20
    accounts = _get_configured_accounts()
    for idx, u in enumerate(accounts):
        if u.lower() == str(account_id).lower():
            return 1 + (idx % 20)
    return 1 + (zlib.crc32(account_id.encode("utf-8")) % 20)


def get_account_webshare_slot(account_id: str) -> int:
    """Get the dedicated sticky Webshare proxy slot (1..20) for an account dynamically.
    Accounts configured in Account Manager (accounts.json) are assigned dedicated primary slots (1..10)
    in order of their configuration.
    Using a dedicated sticky slot guarantees the IP never shifts mid-session,
    preventing Azure AD B2C ExternalAuthenticationFailed session blocks."""
    account_id = _normalize_account_id(account_id)
    if not account_id:
        return 1
    entry = _registry_get(account_id)
    if entry and entry.get("webshare_slot"):
        try:
            return int(entry["webshare_slot"])
        except (ValueError, TypeError):
            pass

    return get_primary_webshare_slot(account_id)


# Full spare pool of 15,989 static ISP proxies available for rotation:
_SPARE_PORTS = list(range(10012, 26001))
_MAX_PORTS_TO_TRY = 20

def normalize_decodo_port(port: int) -> int:
    """Normalize Decodo port: if 1-16000 is given, map to 10001-26000."""
    p = int(port)
    if 1 <= p <= 16000:
        return 10000 + p
    return p

_tunnel_fail = {}        # port -> [count, window_start_ts]
_last_fail_log = {}      # port -> last warning ts
_tunnel_banned = {}      # port -> ban_until_ts
_banned_lock = threading.Lock()
_TUNNEL_FAIL_LOG_INTERVAL = 30.0   # max one warning per port per 30 s
_TUNNEL_FAIL_THRESHOLD = 3         # refusals before the port is banned
_TUNNEL_FAIL_WINDOW = 120.0        # refusals counted within this window
_TUNNEL_BAN_TTL = 600.0            # ban duration (10 min)

# ─── Per-account Proxy error cooldown ────────────────────────────
_PROXY_ERROR_COOLDOWN = 15.0  # seconds
_account_proxy_error_ts = {}  # account_id -> timestamp of last proxy error
_account_proxy_error_last_log = {}  # account_id -> last warning ts
_proxy_cooldown_lock = threading.Lock()

# ─── Webshare 20-slot dynamic pool & burn cooldown tracking ──────
_webshare_slot_cooldown = {}   # slot (1..20) -> timestamp when rotated away / burned
_webshare_slot_lock = threading.Lock()


def should_throttle_proxy_log(account_id: str, cooldown: float = 10.0) -> bool:
    """Return True if an error log should be output; False if throttled."""
    now = time.time()
    with _proxy_cooldown_lock:
        last = _account_proxy_error_last_log.get(account_id, 0.0)
        if now - last >= cooldown:
            _account_proxy_error_last_log[account_id] = now
            return True
        return False
# ─── Cross-process registry ─────────────────────────────────────
# Each account runs in its own Python process, so the in-memory
# _account_forwarders dict is PER-PROCESS. This file is the shared source of
# truth for {account -> local_port/decodo_port/ip} so that a rotation from ANY
# process (login_runner, booking_runner, orchestrator) takes effect everywhere
# — including the process that actually owns the listening forwarder socket.
_REGISTRY_FILE = Path(__file__).parent / "proxy_registry.json"


_registry_cache = {"mtime": None, "data": {}}
_registry_cache_lock = threading.Lock()


def _registry_read():
    """Read proxy_registry.json with an mtime cache.

    _handle_client runs once per browser connection (dozens per page load
    across the whole fleet); re-reading and re-parsing the file on every
    connection was measurable I/O churn. The mtime check keeps reads cheap
    while staying fresh across processes."""
    try:
        mtime = os.stat(_REGISTRY_FILE).st_mtime
    except OSError:
        return {}
    with _registry_cache_lock:
        if _registry_cache["mtime"] == mtime:
            return _registry_cache["data"]
    try:
        with open(_REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # Corrupt/partial read: serve the last good snapshot.
        with _registry_cache_lock:
            return _registry_cache["data"]
    with _registry_cache_lock:
        _registry_cache["mtime"] = mtime
        _registry_cache["data"] = data
    return data


def _registry_write(reg):
    try:
        tmp = str(_REGISTRY_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f)
        os.replace(tmp, _REGISTRY_FILE)
    except Exception as exc:
        log.warning(f"[PROXY] registry write failed: {exc}")


def _registry_get(account_id):
    """Get a registry entry for exactly this account. Falls back to the legacy
    unnamed ('') forwarder only. NOTE: the previous 'adopt the single entry'
    fallback (len(fw)==1) was removed — it made every account adopt whichever
    one entry existed, funneling ALL browsers through one shared exit IP."""
    reg = _registry_read()
    fw = reg.get("forwarders", {})
    entry = fw.get(account_id)
    if entry is None and not account_id and "" in fw:
        # Legacy fallback ONLY for callers that genuinely have no account id.
        # Falling back for a NAMED account funnels it through another
        # account's forwarder (shared exit IP + cross-account churn).
        entry = fw[""]
    return entry


def _registry_known_ports():
    """All Decodo ports currently registered (any process). Not thread-locked —
    callers either hold _state_lock or accept a slightly stale snapshot."""
    ports = set()
    for entry in _registry_read().get("forwarders", {}).values():
        try:
            if entry.get("decodo_port"):
                ports.add(int(entry["decodo_port"]))
        except (TypeError, ValueError):
            pass
    return ports


def _registry_persist(account_id, local_port, decodo_port, ip, session_tag=None, mode=None, last_local_ban_ts=None, webshare_slot=None):
    try:
        # Cross-process critical section: several processes rotate at once
        # during an outage; without this lock a read-modify-write race can
        # drop another account's concurrent registry update.
        lock_path = str(_REGISTRY_FILE) + ".lock"
        lock_fd = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.02)
        try:
            reg = _registry_read()
            prev = reg.setdefault("forwarders", {}).get(account_id, {})
            tag = session_tag or prev.get("session_tag") or ""
            target_mode = mode or prev.get("mode") or ("WEBSHARE" if get_proxy_mode() == "WEBSHARE" else ("LOCAL" if get_proxy_mode() in ("AUTO", "LOCAL", "HYBRID") else "DECODO"))
            ban_ts = float(last_local_ban_ts) if last_local_ban_ts is not None else float(prev.get("last_local_ban_ts", 0))
            wslot = webshare_slot or prev.get("webshare_slot")
            reg.setdefault("forwarders", {})[account_id] = {
                "local_port": int(local_port),
                "decodo_port": int(decodo_port),
                "ip": ip or "",
                "session_tag": tag,
                "mode": target_mode,
                "last_local_ban_ts": ban_ts,
            }
            if wslot is not None:
                reg["forwarders"][account_id]["webshare_slot"] = int(wslot)
            reg["updated"] = time.time()
            _registry_write(reg)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
    except Exception as exc:
        log.warning(f"[PROXY] registry persist failed: {exc}")


class AccountForwarder:
    def __init__(self, account_id, local_port, decodo_port, session_tag=None, mode=None, webshare_slot=None):
        self.account_id = account_id
        self.local_port = local_port
        self.decodo_port = decodo_port
        self.session_tag = session_tag or "".join(c for c in str(account_id or "").strip() if c.isalnum() or c in "-_")
        self.current_ip = None
        self.server = None
        self.mode = mode or ("WEBSHARE" if get_proxy_mode() == "WEBSHARE" else ("LOCAL" if get_proxy_mode() in ("AUTO", "LOCAL", "HYBRID") else "DECODO"))
        self.webshare_slot = webshare_slot


def get_proxy_mode() -> str:
    """Return configured proxy mode: 'WEBSHARE', 'DECODO', 'AUTO', 'LOCAL', 'MOBILE', or 'DIRECT_ONLY'."""
    return (os.environ.get("PROXY_MODE") or "AUTO").strip().upper()


def get_account_mode(account_id: str) -> str:
    """Get active operating mode ('LOCAL', 'MOBILE', 'DECODO', or 'WEBSHARE') for an account."""
    pmode = get_proxy_mode()
    if pmode == "WEBSHARE":
        return "WEBSHARE"
    if pmode in ("LOCAL", "MOBILE"):
        return pmode
    entry = _registry_get(account_id)
    if entry and entry.get("mode"):
        return str(entry["mode"]).upper()
    fwd = _account_forwarders.get(account_id)
    if fwd and hasattr(fwd, "mode") and fwd.mode:
        return str(fwd.mode).upper()
    if pmode in ("AUTO", "HYBRID"):
        return "LOCAL"
    return "DECODO"


def _credentials():
    server = os.environ.get("PROXY_SERVER", "") or DEFAULT_SERVER
    user = os.environ.get("PROXY_USERNAME", "") or DEFAULT_USER
    pwd = os.environ.get("PROXY_PASSWORD", "") or DEFAULT_PASS
    return server.split(":")[0], user, pwd


def proxy_credentials_configured():
    if get_proxy_mode() == "DIRECT_ONLY":
        return False
    server, user, pwd = _credentials()
    return bool(server and user)


def _session_user(account_id=None):
    server, user, _ = _credentials()
    if "decodo.com" in server.lower():
        # Normalize base username: strip 'user-' prefix and any existing session duration tags
        base_user = str(user).strip()
        if base_user.startswith("user-"):
            base_user = base_user[5:]
        if "-sessionduration-" in base_user:
            base_user = base_user.split("-sessionduration-")[0]
        if "-session-" in base_user:
            base_user = base_user.split("-session-")[0]
        return f"user-{base_user}"
    elif "webshare" in server.lower():
        # Webshare proxy: assign dedicated sticky slot (1..10) per account
        # to ensure IP never shifts during login or monitoring sessions!
        base_user = str(user).strip()
        if "-rotate" in base_user:
            base_user = base_user.replace("-rotate", "")
        import re as _re
        base_user = _re.sub(r"-\d+$", "", base_user)
        if account_id:
            slot = get_account_webshare_slot(account_id)
            return f"{base_user}-{slot}"
        return f"{base_user}-1"
    return str(user).strip()


def _parse_ip(resp, healthcheck_url=None):
    """Extract the exit IP from a healthcheck response. `healthcheck_url`
    overrides the global default so fallback endpoints parse correctly."""
    url = healthcheck_url or HEALTHCHECK_URL
    try:
        if "json" in url:
            data = resp.json()
            ip = data.get("ip") or data.get("IP") or data.get("address") or data.get("origin")
            if not ip:
                proxy_obj = data.get("proxy") or {}
                if isinstance(proxy_obj, dict):
                    ip = proxy_obj.get("ip") or proxy_obj.get("address")
            if not ip:
                for v in data.values():
                    if isinstance(v, dict):
                        ip = v.get("ip") or v.get("address")
                        if ip:
                            break
            return ip or "unknown"
        else:
            text = resp.text.strip()
            return text.split()[0] if text and "." in text else "unknown"
    except Exception:
        return "unknown"


_HEALTHCHECK_TIMEOUT = 6  # per-attempt timeout; fast failure detection
# Multiple healthcheck endpoints: if the primary is down or rate-limited, the
# others still prove the tunnel exits through Decodo. Without fallbacks a single
# dead endpoint made EVERY port look unhealthy → rotation returned None → the
# bot gave up on a dead tunnel (ERR_TUNNEL_CONNECTION_FAILED forever).
_HEALTHCHECK_FALLBACKS = (
    "https://ipinfo.io/json",
    "http://httpbin.org/ip",
    "https://ip.decodo.com/json",
)


def _raw_connect_probe(server, port, account_id=None):
    """Tunnel-level health check: issue a raw HTTP CONNECT through the Decodo
    port. Works even when every healthcheck website is unreachable — if the
    CONNECT completes, the tunnel IS alive. Returns (ok, ip_or_'unknown').
    """
    server, _, pwd = _credentials()
    token = base64.b64encode(f"{_session_user(account_id)}:{pwd}".encode()).decode()
    probe_host = os.environ.get("PROXY_PROBE_HOST", "www.google.com")
    sock = None
    try:
        sock = socket.create_connection((server, int(port)), timeout=6)
        sock.settimeout(8)
        req = (f"CONNECT {probe_host}:443 HTTP/1.1\r\n"
               f"Host: {probe_host}:443\r\n"
               f"Proxy-Authorization: Basic {token}\r\n"
               f"\r\n")
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp and len(resp) < 16384:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        first = resp.split(b"\r\n", 1)[0]
        return (b" 200 " in first), "unknown"
    except Exception:
        return False, ""
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def check_ip_health(port=None, account_id=None):
    """Check if a proxy port is healthy (Decodo reachable).

    Resilient by design:
      1. Fetch the exit IP through the port from the primary healthcheck URL.
      2. On failure, try fallback healthcheck URLs (a down/rate-limited
         healthcheck site must NOT make the port look dead).
      3. Last resort: a raw CONNECT probe — tunnel alive = port healthy even
         when no IP-echo website can be reached.
    """
    server, user, pwd = _credentials()
    if "decodo.com" not in server.lower():
        port = int(os.environ.get("PROXY_PORT", "80"))
    else:
        port = port or DEFAULT_PORT
    proxy_url = f"http://{_session_user(account_id)}:{pwd}@{server}:{port}"
    proxies = {"http": proxy_url, "https": proxy_url}

    for url in (HEALTHCHECK_URL,) + _HEALTHCHECK_FALLBACKS:
        try:
            resp = requests.get(url, proxies=proxies, timeout=_HEALTHCHECK_TIMEOUT)
            if resp.status_code == 200:
                return True, _parse_ip(resp, healthcheck_url=url)
        except Exception:
            continue

    # All IP-echo endpoints unreachable through this port — verify the tunnel
    # itself with a raw CONNECT before declaring the port dead.
    ok, ip = _raw_connect_probe(server, port, account_id=account_id)
    if ok:
        log.info(f"[PROXY] Port {port}: healthcheck sites unreachable, but raw CONNECT OK — treating as healthy")
        return True, ip
    return False, ""


def _tunnel_track_fail(port):
    """Track tunnel refusals for a port within the fail window. Returns count."""
    now = time.time()
    with _banned_lock:
        cnt, start = _tunnel_fail.get(port, [0, now])
        if now - start > _TUNNEL_FAIL_WINDOW:
            cnt, start = 0, now
        cnt += 1
        _tunnel_fail[port] = [cnt, start]
        return cnt


def _ban_port(port):
    with _banned_lock:
        _tunnel_banned[int(port)] = time.time() + _TUNNEL_BAN_TTL
        _tunnel_fail.pop(int(port), None)


def _port_banned(port):
    with _banned_lock:
        until = _tunnel_banned.get(int(port))
        if until is None:
            return False
        if time.time() > until:
            _tunnel_banned.pop(int(port), None)
            return False
        return True


def _find_healthy_port(exclude=None, pool=None):
    """Find a healthy upstream proxy port."""
    server, _, _ = _credentials()
    cfg_port = int(os.environ.get("PROXY_PORT", str(DEFAULT_PORT)))
    if "decodo.com" not in server.lower():
        # Dedicated ISP or custom proxy — use configured port directly
        ok, ip = check_ip_health(cfg_port)
        if ok:
            return cfg_port, ip
        return None, ""

    exclude = exclude or set()
    target_pool = pool if pool is not None else _RESIDENTIAL_PORTS
    sample_size = min(_MAX_PORTS_TO_TRY * 3, len(target_pool))
    sampled = random.sample(target_pool, sample_size)
    ports_to_try = [p for p in sampled if p not in exclude and not _port_banned(p)]
    for port in ports_to_try[:_MAX_PORTS_TO_TRY]:
        ok, ip = check_ip_health(port)
        if ok and ip:
            return port, ip
    return None, ""


# ─── Local forwarder (one per account) ──────────────────────────

def _pipe(a, b):
    sockets = [a, b]
    try:
        while True:
            # 120s idle window: a short window kills silent long-poll
            # connections (Cloudflare waiting room) mid-flight → spurious
            # ERR_EMPTY_RESPONSE / error pages in the browser.
            r, _, _ = select.select(sockets, [], [], 120)
            if not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except (OSError, ValueError):
        # ValueError: a socket's fd turned negative (closed under us) while
        # select was waiting — treat exactly like a broken pipe.
        pass
    finally:
        for s in sockets:
            try:
                s.close()
            except OSError:
                pass


def _is_proxy_error(exc: Exception) -> bool:
    """Determine if an exception is a proxy/connectivity error (not a portal 429).

    Proxy errors include:
    - Tunnel refused (HTTP 403/522 from Decodo)
    - Connection refused / timed out
    - DNS resolution failures
    - SSL/TLS errors

    These are SEPARATE from portal 429 rate-limit errors and should NOT
    count toward the rate-limit counter.
    """
    err_str = str(exc).lower()
    proxy_error_keywords = [
        "tunnel refused", "403", "522", "forbidden",
        "connection refused", "connection timed out", "timed out",
        "name not resolved", "dns", "ssl", "tls",
        "proxy", "tunnel"
    ]
    return any(kw in err_str for kw in proxy_error_keywords)


def _handle_client(client, account_id):
    global _last_self_heal_ts, _last_proxy_error_ts
    fwd = _account_forwarders.get(account_id)
    if not fwd and account_id and "" in _account_forwarders:
        fwd = _account_forwarders[""]  # legacy unnamed forwarder
    if not fwd:
        try:
            client.close()
        except OSError:
            pass
        return
    
    upstream = None
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(65536)
            if not chunk:
                return
            head += chunk
            if len(head) > 128 * 1024:
                return
        
        is_connect = head.partition(b"\r\n")[0].upper().startswith(b"CONNECT")
        
        # Freshest mode, Decodo port & session tag: the registry file wins over memory,
        # because rotations/failovers from other processes land there.
        current_mode = getattr(fwd, "mode", "LOCAL")
        decodo_port = fwd.decodo_port
        try:
            entry = _registry_get(account_id)
            if entry:
                if entry.get("mode"):
                    current_mode = str(entry["mode"]).upper()
                if entry.get("decodo_port"):
                    decodo_port = int(entry["decodo_port"])
                if entry.get("session_tag"):
                    fwd.session_tag = entry["session_tag"]
                if entry.get("webshare_slot"):
                    fwd.webshare_slot = int(entry["webshare_slot"])
        except Exception:
            pass
        
        if get_proxy_mode() == "WEBSHARE":
            current_mode = "WEBSHARE"
        elif get_proxy_mode() in ("LOCAL", "MOBILE"):
            current_mode = get_proxy_mode()
        elif get_proxy_mode() == "DECODO" and not (entry and entry.get("mode")):
            current_mode = "DECODO"

        # ─── LOCAL / MOBILE MODE (Direct Host Residential / Mobile Network) ─────────────────────
        if current_mode in ("LOCAL", "MOBILE"):
            first_line = head.partition(b"\r\n")[0]
            if is_connect:
                parts = first_line.split()
                target = parts[1].decode("latin1", errors="replace") if len(parts) > 1 else ""
                if ":" in target:
                    target_host, target_port_s = target.split(":", 1)
                    try:
                        target_port = int(target_port_s)
                    except ValueError:
                        target_port = 443
                else:
                    target_host = target
                    target_port = 443
                try:
                    upstream = socket.create_connection((target_host, target_port), timeout=15)
                    upstream.settimeout(30)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    _pipe(client, upstream)
                    upstream = None
                    return
                except Exception:
                    if upstream:
                        try:
                            upstream.close()
                        except OSError:
                            pass
                        upstream = None
                    return
            else:
                # Plain HTTP forwarding directly on host
                target_host = ""
                target_port = 80
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"host:"):
                        h_val = line.split(b":", 1)[1].strip().decode("latin1", errors="replace")
                        if ":" in h_val:
                            target_host, p_str = h_val.split(":", 1)
                            try:
                                target_port = int(p_str)
                            except ValueError:
                                target_port = 80
                        else:
                            target_host = h_val
                        break
                if target_host:
                    try:
                        upstream = socket.create_connection((target_host, target_port), timeout=15)
                        upstream.settimeout(30)
                        upstream.sendall(head)
                        _pipe(client, upstream)
                        upstream = None
                        return
                    except Exception:
                        if upstream:
                            try:
                                upstream.close()
                            except OSError:
                                pass
                            upstream = None
                        return
                return

        # ─── UPSTREAM PROXY MODE (Webshare / Decodo Gateway) ───────────────────
        server, user, pwd = _credentials()
        upstream_port = int(os.environ.get("PROXY_PORT", "80")) if "decodo.com" not in server.lower() else int(decodo_port)
        max_tunnel_tries = 2
        for attempt in range(1, max_tunnel_tries + 1):
            try:
                upstream = socket.create_connection((server, upstream_port), timeout=15)
                upstream.settimeout(30)
                
                token = base64.b64encode(f"{_session_user(account_id)}:{pwd}".encode()).decode()
                first_line, _, rest = head.partition(b"\r\n")
                auth_header = f"Proxy-Authorization: Basic {token}\r\n".encode()
                upstream.sendall(first_line + b"\r\n" + auth_header + rest)
                
                if is_connect:
                    resp = b""
                    try:
                        while b"\r\n\r\n" not in resp:
                            chunk = upstream.recv(65536)
                            if not chunk:
                                break
                            resp += chunk
                    except OSError:
                        pass
                    first_resp = resp.split(b"\r\n", 1)[0]
                    if b" 200 " not in first_resp:
                        bad = first_resp[:60]
                        raise OSError(f"tunnel refused: {bad}")
                    try:
                        client.sendall(resp)
                    except OSError:
                        pass
                _pipe(client, upstream)
                upstream = None  # _pipe already closed both ends
                return
            except Exception as exc:
                if upstream:
                    try:
                        upstream.close()
                    except OSError:
                        pass
                    upstream = None
                if attempt >= max_tunnel_tries or not is_connect:
                    # Clean exit -- never rotate dedicated proxy slot on random client socket drops
                    return
                time.sleep(0.3)
    except Exception:
        pass
    finally:
        for s in (client, upstream):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


def _local_port_candidates(account_id):
    """Deterministic, collision-free local listening ports for an account.

    Every Python process (login_runner / booking_runner / orchestrator) used to
    allocate from the same _next_local_port counter starting at 18080, so ALL
    accounts adopted or created 'local:18080' — one shared exit IP for the whole
    fleet and cross-account traffic through a foreign forwarder. Deriving the
    port from the account id makes every process agree on the same unique port.
    """
    if account_id:
        h = zlib.crc32(account_id.encode("utf-8"))
        base = FORWARD_PORT_BASE + (h % 1500) * 10          # 18080 .. 33070
        return [base + i for i in range(10)]
    return [FORWARD_PORT_BASE + i for i in range(10)]


def _start_forwarder_server(account_id):
    """Ensure a listening socket exists for this account in THIS process.

    Returns True if this process now owns the listener. If the port is already
    bound (another process owns it and serves this account), that is fine —
    return False WITHOUT rebinding elsewhere, so exactly one owner exists.
    """
    fwd = _account_forwarders[account_id]
    if fwd.server:
        return True

    candidates = _local_port_candidates(account_id)
    if fwd.local_port in candidates:
        candidates = [fwd.local_port] + [p for p in candidates if p != fwd.local_port]
    else:
        candidates = [fwd.local_port] + candidates

    for port in candidates:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # Windows: SO_REUSEADDR lets a second socket silently bind an
            # already-listening port. Every process then "bound" 18080 and
            # Chrome connections were served by whichever process won the
            # race — account X's traffic through account Y's (possibly dead)
            # Decodo port → fleet-wide ERR_CONNECTION_ABORTED. Exclusive
            # binding makes the second bind FAIL, so this process falls
            # through to its own account's deterministic candidate ports.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
        except OSError:
            srv.close()
            continue
        srv.listen(64)
        fwd.server = srv
        if port != fwd.local_port:
            old = fwd.local_port
            fwd.local_port = port
            _registry_persist(account_id, port, fwd.decodo_port, fwd.current_ip or "")
            log.info(f"[PROXY] Account '{account_id}' local listener moved {old} -> {port}")

        def _loop(s=srv):
            while True:
                try:
                    client, _ = s.accept()
                except OSError:
                    return
                threading.Thread(target=_handle_client, args=(client, account_id), daemon=True).start()

        threading.Thread(target=_loop, daemon=True).start()
        return True

    log.warning(f"[PROXY] Account '{account_id}': could not bind any local port "
                f"{candidates[0]}-{candidates[-1]} — relying on an external owner if one exists.")
    return False


# ─── Public API ─────────────────────────────────────────────────

def get_or_create_forwarder(account_id, preferred_local_port=None):
    """Get or create a forwarder for an account. Returns (local_port, ip) or (None, None).

    Cross-process safe: assigns dedicated ports and supports zero-restart failover between
    Direct Residential LOCAL and Decodo Residential Proxies.
    """
    account_id = _normalize_account_id(account_id)
    with _state_lock:
        if account_id in _account_forwarders:
            fwd = _account_forwarders[account_id]
            _start_forwarder_server(account_id)
            return fwd.local_port, fwd.current_ip
        
        own_ports = _local_port_candidates(account_id)
        local_port = int(preferred_local_port) if (preferred_local_port and int(preferred_local_port) in own_ports) else own_ports[0]

        server, _, _ = _credentials()
        is_decodo = "decodo.com" in server.lower()

        # Target port:
        if is_decodo:
            target_decodo_port = get_account_decodo_port(account_id)
        else:
            target_decodo_port = int(os.environ.get("PROXY_PORT", "80"))

        entry = _registry_get(account_id)
        decodo_port = target_decodo_port
        
        # Initial mode determination:
        # Initial mode determination:
        # In WEBSHARE mode, ALL accounts (both Pollers and VIP Snipers) receive their dedicated sticky Webshare slot (1..20),
        # ensuring zero IP collision so Cloudflare 429 rate limit is never triggered across accounts.
        role = get_account_role(account_id)
        pmode = get_proxy_mode()
        if pmode in ("AUTO", "LOCAL", "HYBRID"):
            default_mode = "LOCAL"
        elif pmode == "MOBILE":
            default_mode = "MOBILE"
        elif pmode == "WEBSHARE":
            default_mode = "WEBSHARE"
        else:
            default_mode = "DECODO"
        mode = default_mode
        if pmode == "WEBSHARE":
            mode = "WEBSHARE"
        elif pmode in ("LOCAL", "MOBILE"):
            mode = pmode
        elif entry and entry.get("mode"):
            mode = str(entry["mode"]).upper()

        ip = None
        slot = None
        if mode in ("LOCAL", "MOBILE"):
            ip = "MOBILE_CARRIER" if mode == "MOBILE" else "LOCAL_RESIDENTIAL"
            if entry and entry.get("decodo_port"):
                decodo_port = int(entry["decodo_port"])
        elif mode == "WEBSHARE":
            decodo_port = int(os.environ.get("PROXY_PORT", "80"))
            slot = get_account_webshare_slot(account_id)
            ok, live_ip = check_ip_health(decodo_port, account_id=account_id)
            ip = live_ip if (ok and live_ip) else f"WEBSHARE_SLOT_{slot}"
        else:
            if entry and entry.get("decodo_port"):
                existing_port = int(entry["decodo_port"])
                if existing_port in _SPARE_PORTS or existing_port == target_decodo_port:
                    decodo_port = existing_port
                    ip = entry.get("ip") or None

            # Verify Decodo port is alive
            ok, live_ip = check_ip_health(decodo_port, account_id=account_id)
            if ok and live_ip:
                ip = live_ip
            else:
                log.warning(f"[PROXY] Account '{account_id}': Decodo port {decodo_port} check failed — picking healthy spare port...")
                used = {f.decodo_port for f in _account_forwarders.values() if f.decodo_port} | _registry_known_ports()
                new_port, new_ip = _find_healthy_port(exclude=used | {decodo_port}, pool=_SPARE_PORTS)
                if new_port:
                    decodo_port = new_port
                    ip = new_ip

        fwd = AccountForwarder(account_id, local_port, decodo_port, mode=mode, webshare_slot=slot)
        fwd.current_ip = ip
        _account_forwarders[account_id] = fwd
        _start_forwarder_server(account_id)
        _registry_persist(account_id, local_port, decodo_port, ip or "", mode=mode, webshare_slot=slot)
        log.info(f"[PROXY] Account '{account_id}' -> local:{local_port} -> Mode: {mode} ({ip}) [Slot: {slot or 'N/A'}]")
        return fwd.local_port, fwd.current_ip


_last_local_ban_ts = {}  # account_id -> timestamp when LOCAL got rate-limited
_account_last_rotate_ts = {}  # account_id -> timestamp of last proxy rotation
_MIN_ROTATION_COOLDOWN = 45.0  # seconds between rotations per account


def rotate_account_ip(account_id, reason="", force=False):
    """Rotate an account to a new IP or alternate mode (LOCAL <-> DECODO <-> WEBSHARE). Returns (local_port, ip) or None."""
    account_id = _normalize_account_id(account_id)
    if get_proxy_mode() == "DIRECT_ONLY":
        log.info(f"[PROXY] Running in DIRECT_ONLY mode: proxy rotation skipped.")
        return None

    # Microsoft Dynamics 365 / PowerPages 429 rate limit is per-account/session,
    # NOT a proxy IP ban. Rotating the proxy IP causes Cloudflare Turnstile challenges
    # and breaks cf_clearance without clearing the backend session rate limit.
    reason_str = str(reason or "").lower()
    if not force and ("429" in reason_str or "portal" in reason_str or "throttle" in reason_str) and not ("1015" in reason_str or "waf" in reason_str or "blocked" in reason_str):
        log.info(f"[PROXY] Skipping proxy IP rotation for '{account_id}' ({reason}): Portal rate limit is session-level, maintaining sticky proxy IP.")
        entry = _registry_get(account_id)
        if entry:
            return int(entry.get("local_port", FORWARD_PORT_BASE)), str(entry.get("ip", "ACTIVE"))
        return None

    now = time.time()
    last_rot = _account_last_rotate_ts.get(str(account_id or "").lower(), 0.0)
    is_urgent = any(k in reason_str for k in ["waf", "cloudflare", "block", "ban", "transport", "broken", "failed"])
    if not force and not is_urgent and (now - last_rot < _MIN_ROTATION_COOLDOWN):
        rem = int(_MIN_ROTATION_COOLDOWN - (now - last_rot))
        log.info(f"[PROXY] Skipping rapid IP rotation for '{account_id}' (cooldown {rem}s remaining | {reason}).")
        entry = _registry_get(account_id)
        if entry:
            return int(entry.get("local_port", FORWARD_PORT_BASE)), str(entry.get("ip", "ACTIVE"))
        return None
    elif is_urgent and not force and (now - last_rot < 4.0):
        # Even on urgent block, debounce by 4 seconds to prevent tight loop
        log.info(f"[PROXY] Urgent rotation debounce (4s) for '{account_id}'.")
        entry = _registry_get(account_id)
        if entry:
            return int(entry.get("local_port", FORWARD_PORT_BASE)), str(entry.get("ip", "ACTIVE"))
        return None

    _account_last_rotate_ts[str(account_id or "").lower()] = now

    with _state_lock:
        fwd = _account_forwarders.get(account_id)
        if not fwd:
            for k, f in _account_forwarders.items():
                if str(k).lower() == str(account_id).lower():
                    fwd = f
                    account_id = k
                    break
        if not fwd and account_id and "" in _account_forwarders:
            fwd = _account_forwarders[""]
            account_id = ""

        entry = _registry_get(account_id)
        if not entry and not fwd:
            reg = _registry_read()
            fw = reg.get("forwarders", {})
            for k, e in fw.items():
                if str(k).lower() == str(account_id).lower():
                    entry = e
                    account_id = k
                    break

        local_port = int(fwd.local_port if fwd else (entry.get("local_port") if entry else FORWARD_PORT_BASE))
        old_port = int(fwd.decodo_port if fwd else (entry.get("decodo_port") if entry else DEFAULT_PORT))

        current_mode = "WEBSHARE" if get_proxy_mode() == "WEBSHARE" else "LOCAL"
        if entry and entry.get("mode"):
            if get_proxy_mode() == "WEBSHARE":
                current_mode = "WEBSHARE"
            else:
                current_mode = str(entry["mode"]).upper()
        elif fwd and hasattr(fwd, "mode"):
            current_mode = str(fwd.mode).upper()
        elif get_proxy_mode() == "DECODO":
            current_mode = "DECODO"

    # MOBILE mode: cycle Android phone via ADB Airplane mode
    if current_mode == "MOBILE" or get_proxy_mode() == "MOBILE":
        log.info(f"[PROXY] Mobile 4G/5G Mode active for '{account_id}': Triggering ADB Airplane mode cycle...")
        try:
            from src.common.mobile_proxy import rotate_mobile_ip
            rot_res = rotate_mobile_ip(reason=reason)
            new_ip = rot_res.get("new_ip") or "MOBILE_CARRIER"
            _registry_persist(account_id, local_port=local_port, decodo_port=DEFAULT_PORT, ip=new_ip, mode="MOBILE")
            return local_port, new_ip
        except Exception as e:
            log.error(f"[PROXY] Mobile IP rotation error: {e}")
            return local_port, "MOBILE_CARRIER"

    # VIP Snipers on LOCAL mode stay on clean direct residential broadband only if PROXY_MODE is not WEBSHARE
    if current_mode == "LOCAL" and get_proxy_mode() != "WEBSHARE":
        log.info(f"[PROXY] Account '{account_id}' is running on clean LOCAL residential broadband (VIP Sniper). Maintaining direct connection.")
        entry = _registry_get(account_id)
        if entry:
            return int(entry.get("local_port", FORWARD_PORT_BASE)), "LOCAL_RESIDENTIAL"
        return None

    server, _, _ = _credentials()
    has_decodo = bool(server and "decodo.com" in server.lower())

    if not has_decodo:
        # Webshare / Custom Proxy: dynamically rotate within poller's dedicated slot pool
        # Poller #1 -> Dedicated Pool: Slots 1..10 (10 IPs)
        # Poller #2 -> Dedicated Pool: Slots 11..20 (10 IPs)
        cfg_port = int(os.environ.get("PROXY_PORT", "80"))
        cur_slot = get_account_webshare_slot(account_id)
        now = time.time()
        with _webshare_slot_lock:
            if cur_slot:
                _webshare_slot_cooldown[cur_slot] = now

        pollers = get_poller_accounts()
        poller_idx = 0
        for idx, p in enumerate(pollers):
            if p.lower() == str(account_id).lower():
                poller_idx = idx
                break

        if poller_idx == 0:
            assigned_pool = list(range(1, 11))
        elif poller_idx == 1:
            assigned_pool = list(range(11, 21))
        else:
            assigned_pool = list(range(1, 21))

        configured = _get_configured_accounts(active_only=True)
        reg_data = _registry_read()
        reg_fw = reg_data.get("forwarders", {})

        # Only slots currently in use by OTHER active accounts are busy
        used_slots = set()
        for k, v in reg_fw.items():
            if str(k).lower() != str(account_id).lower() and k in configured and v.get("webshare_slot"):
                try:
                    used_slots.add(int(v["webshare_slot"]))
                except (ValueError, TypeError):
                    pass

        # Slots within assigned pool minus cur_slot and minus used_slots:
        free_slots = [s for s in assigned_pool if s != cur_slot and s not in used_slots]

        new_slot = None
        # PASS 1: Free slots with NO recent cooldown (< 300s) - shuffled for random fresh pick
        unburned_free = [s for s in free_slots if now - _webshare_slot_cooldown.get(s, 0.0) >= 300.0]
        if unburned_free:
            random.shuffle(unburned_free)
            new_slot = unburned_free[0]

        # PASS 2: Free slots with cooldown >= 120s
        if not new_slot:
            cooling_free = [s for s in free_slots if now - _webshare_slot_cooldown.get(s, 0.0) >= 120.0]
            if cooling_free:
                cooling_free.sort(key=lambda s: _webshare_slot_cooldown.get(s, 0.0))
                new_slot = cooling_free[0]

        # PASS 3: Any free slot in assigned pool (oldest cooldown)
        if not new_slot and free_slots:
            free_slots.sort(key=lambda s: _webshare_slot_cooldown.get(s, 0.0))
            new_slot = free_slots[0]

        # PASS 4: If all assigned slots had recent cooldowns, pick the slot that is NOT cur_slot with oldest cooldown
        if not new_slot:
            other_candidates = [s for s in assigned_pool if s != cur_slot]
            with _webshare_slot_lock:
                other_candidates.sort(key=lambda s: _webshare_slot_cooldown.get(s, 0.0))
            new_slot = other_candidates[0] if other_candidates else cur_slot

        # Update slot in forwarder and registry before health check
        _registry_persist(account_id, local_port, cfg_port, "", mode="WEBSHARE", webshare_slot=new_slot)
        with _state_lock:
            if fwd:
                fwd.webshare_slot = new_slot

        ok, live_ip = check_ip_health(cfg_port, account_id=account_id)
        if not ok or not live_ip:
            live_ip = f"WEBSHARE_SLOT_{new_slot}"
        with _state_lock:
            if fwd:
                fwd.mode = "WEBSHARE"
                fwd.decodo_port = cfg_port
                fwd.current_ip = live_ip
                fwd.webshare_slot = new_slot
        _registry_persist(account_id, local_port, cfg_port, live_ip, mode="WEBSHARE", webshare_slot=new_slot)
        log.info(f"[PROXY] Account '{account_id}' rotated on Webshare -> Slot {new_slot} (fresh IP {live_ip}) | {reason}")
        return local_port, live_ip

    # ─────────────────────────────────────────────────────────────
    # CASE 1: Currently LOCAL -> Rate-limited / Banned!
    # Failover to DECODO (Residential Proxies)
    # ─────────────────────────────────────────────────────────────
    if current_mode == "LOCAL":
        if not has_decodo:
            log.warning(f"[PROXY] Account '{account_id}' LOCAL rate-limited but Decodo is not configured.")
            return None

        _last_local_ban_ts[account_id] = time.time()

        # Pick dedicated or fresh spare Decodo port
        target_port = get_account_decodo_port(account_id) or old_port or 10001
        ok, live_ip = check_ip_health(target_port, account_id=account_id)
        if not (ok and live_ip):
            other_used = _registry_known_ports() | _PRIMARY_PORTS | {old_port}
            new_port, new_ip = _find_healthy_port(exclude=other_used, pool=_SPARE_PORTS)
            if new_port and new_ip:
                target_port = new_port
                live_ip = new_ip
            else:
                live_ip = "DECODO_RESIDENTIAL"

        now_ts = time.time()
        _last_local_ban_ts[account_id] = now_ts
        with _state_lock:
            if fwd:
                fwd.mode = "DECODO"
                fwd.decodo_port = target_port
                fwd.current_ip = live_ip

        _registry_persist(account_id, local_port, target_port, live_ip, mode="DECODO", last_local_ban_ts=now_ts)
        log.info(f"[SMART-HYBRID] [FAILOVER] Account '{account_id}': LOCAL IP rate-limited -> AUTO-SWITCHED to DECODO Residential IP: {live_ip} (Decodo Port {target_port}) | {reason}")
        return local_port, live_ip

    # ─────────────────────────────────────────────────────────────
    # CASE 2: Currently DECODO -> Rotate or Round-Robin back to LOCAL
    # ─────────────────────────────────────────────────────────────
    # If in pure DECODO mode: rotate to another Decodo port
    if get_proxy_mode() == "DECODO":
        other_used = _registry_known_ports() | _PRIMARY_PORTS | {old_port}
        new_port, new_ip = _find_healthy_port(exclude=other_used, pool=_SPARE_PORTS)
        if not new_port:
            new_port, new_ip = _find_healthy_port(exclude=_registry_known_ports() | {old_port})
        if not new_port:
            log.warning(f"[PROXY] No healthy spare port available for rotation of '{account_id}'")
            return None
        with _state_lock:
            if fwd:
                fwd.decodo_port = new_port
                fwd.current_ip = new_ip
        _registry_persist(account_id, local_port, new_port, new_ip, mode="DECODO")
        log.info(f"[PROXY] Account '{account_id}' rotated: Decodo {old_port} -> {new_port} ({new_ip}) - {reason}")
        return local_port, new_ip

    # AUTO / HYBRID / LOCAL cycling:
    # Check if LOCAL cooldown has finished (>= 600s / 10 mins since local ban)
    last_ban = float(entry.get("last_local_ban_ts", 0) if entry else 0) or _last_local_ban_ts.get(account_id, 0)
    if last_ban <= 0:
        # If entry has no timestamp yet, assume fresh switch to Decodo (start 10-min cooldown now)
        last_ban = time.time()
        _last_local_ban_ts[account_id] = last_ban
        _registry_persist(account_id, local_port, old_port, fwd.current_ip if fwd else "DECODO_RESIDENTIAL", mode="DECODO", last_local_ban_ts=last_ban)

    cooldown_window = 600.0  # 10 minutes safe window for Cloudflare WAF rate limits
    cooldown_elapsed = (time.time() - last_ban) >= cooldown_window

    if cooldown_elapsed:
        # Switch back to LOCAL!
        with _state_lock:
            if fwd:
                fwd.mode = "LOCAL"
                fwd.current_ip = "LOCAL_RESIDENTIAL"
        _registry_persist(account_id, local_port, old_port, "LOCAL_RESIDENTIAL", mode="LOCAL", last_local_ban_ts=0)
        log.info(f"[SMART-HYBRID] [ROUND-ROBIN] Account '{account_id}': Local IP cooled down (10m passed) -> AUTO-SWITCHED back to LOCAL (Direct Residential) | {reason}")
        return local_port, "LOCAL_RESIDENTIAL"
    else:
        # Local still cooling down: rotate to another Decodo port from pool
        other_used = _registry_known_ports() | _PRIMARY_PORTS | {old_port}
        new_port, new_ip = _find_healthy_port(exclude=other_used, pool=_SPARE_PORTS)
        if not new_port:
            new_port, new_ip = _find_healthy_port(exclude=_registry_known_ports() | {old_port})
        if new_port and new_ip:
            with _state_lock:
                if fwd:
                    fwd.decodo_port = new_port
                    fwd.current_ip = new_ip
            _registry_persist(account_id, local_port, new_port, new_ip, mode="DECODO", last_local_ban_ts=last_ban)
            cooldown_rem = int(max(0, cooldown_window - (time.time() - last_ban)))
            log.info(f"[SMART-HYBRID] [ROTATE] Account '{account_id}' (Local cooldown {cooldown_rem}s remaining): Rotated to next Decodo IP: {new_ip} (Port {new_port}) | {reason}")
            return local_port, new_ip
        else:
            # Fallback to LOCAL only if entire Decodo pool is exhausted
            with _state_lock:
                if fwd:
                    fwd.mode = "LOCAL"
                    fwd.current_ip = "LOCAL_RESIDENTIAL"
            _registry_persist(account_id, local_port, old_port, "LOCAL_RESIDENTIAL", mode="LOCAL", last_local_ban_ts=0)
            log.info(f"[SMART-HYBRID] [ROUND-ROBIN] Account '{account_id}': Switched back to LOCAL (Direct Residential) | {reason}")
            return local_port, "LOCAL_RESIDENTIAL"


def chrome_proxy_args(account_id, preferred_local_port=None):
    """Get Chrome proxy args for an account. Points Chrome to internal forwarder 127.0.0.1:{local_port}
    to enable zero-restart failover between direct local residential and Decodo."""
    account_id = _normalize_account_id(account_id)
    if get_proxy_mode() == "DIRECT_ONLY":
        log.info(f"[PROXY] Account '{account_id}' using DIRECT_ONLY mode (direct OS network).")
        return []
    if not proxy_credentials_configured() and get_proxy_mode() == "DECODO":
        return []

    local_port, ip = get_or_create_forwarder(account_id, preferred_local_port=preferred_local_port)
    if local_port is None:
        log.warning(f"[PROXY] Forwarder unavailable for '{account_id}' - falling back to direct connection")
        return []

    mode = get_account_mode(account_id)
    log.info(f"[PROXY] Chrome for '{account_id}' using forwarder 127.0.0.1:{local_port} [Initial Mode: {mode} ({ip})]")
    return [f"--proxy-server=http://127.0.0.1:{local_port}"]


def get_account_proxy_url(account_id: str) -> Optional[str]:
    """Get the HTTP proxy URL (http://127.0.0.1:{local_port}) for an account forwarder."""
    if get_proxy_mode() == "DIRECT_ONLY":
        return None
    entry = _registry_get(account_id)
    if entry and entry.get("local_port"):
        return f"http://127.0.0.1:{entry['local_port']}"
    return None


def reset_all_proxies_to_static():
    """Reset all accounts to their primary dedicated startup state.
    In WEBSHARE mode, sets forwarders to Webshare port 80.
    In AUTO / LOCAL modes, resets accounts to clean LOCAL direct broadband.
    In DECODO mode, resets accounts to dedicated static Decodo ports."""
    if get_proxy_mode() == "DIRECT_ONLY":
        return
    server, _, _ = _credentials()
    is_decodo = "decodo.com" in server.lower()
    pmode = get_proxy_mode()
    if pmode in ("AUTO", "LOCAL", "HYBRID"):
        initial_mode = "LOCAL"
    elif pmode == "WEBSHARE":
        initial_mode = "WEBSHARE"
    else:
        initial_mode = "DECODO"
    cfg_port = int(os.environ.get("PROXY_PORT", "80")) if not is_decodo else 10001
    try:
        reg = _registry_read()
        fw = reg.get("forwarders", {})
        active_accts = list(_get_configured_accounts(active_only=True))
        # Prune forwarders for accounts that are disabled/inactive so they do not squat on proxy ports or slots
        stale_keys = [k for k in list(fw.keys()) if k and k not in active_accts]
        for sk in stale_keys:
            del fw[sk]
            changed = True
        all_accts = active_accts

        # DYNAMIC STARTUP WEBSHARE ALLOCATION:
        # Give every active account a fresh, non-overlapping Webshare slot (1..20).
        # Randomly shuffle unburned slots so accounts don't keep starting with the same stale IP.
        allocated_slots = {}
        if not is_decodo:
            now = time.time()
            all_slots = list(range(1, 21))
            unburned = [s for s in all_slots if now - _webshare_slot_cooldown.get(s, 0.0) >= 300.0]
            burned = [s for s in all_slots if s not in unburned]
            random.shuffle(unburned)
            burned.sort(key=lambda s: _webshare_slot_cooldown.get(s, 0.0))
            available_pool = unburned + burned

            for idx, a_id in enumerate(all_accts):
                allocated_slots[a_id] = available_pool[idx % len(available_pool)]

        for account_id in all_accts:
            account_id = _normalize_account_id(account_id)
            if not account_id:
                continue
            entry = fw.setdefault(account_id, {
                "local_port": _local_port_candidates(account_id)[0] if account_id else FORWARD_PORT_BASE,
                "decodo_port": cfg_port,
                "ip": "",
                "session_tag": "",
                "mode": initial_mode,
                "last_local_ban_ts": 0.0
            })
            if entry.get("mode") != initial_mode:
                entry["mode"] = initial_mode
                changed = True
            if not is_decodo:
                slot = allocated_slots.get(account_id) or 1
                if entry.get("decodo_port") != cfg_port:
                    entry["decodo_port"] = cfg_port
                    changed = True
                if entry.get("webshare_slot") != slot:
                    entry["webshare_slot"] = slot
                    changed = True
            else:
                decodo_port = get_account_decodo_port(account_id)
                if decodo_port and entry.get("decodo_port") != decodo_port:
                    entry["decodo_port"] = decodo_port
                    changed = True
                if initial_mode == "LOCAL" and entry.get("ip") != "LOCAL_RESIDENTIAL":
                    entry["ip"] = "LOCAL_RESIDENTIAL"
                    changed = True
        if changed or len(fw) != len(reg.get("forwarders", {})):
            reg["forwarders"] = fw
            reg["updated"] = time.time()
            _registry_write(reg)
            log.info(f"[PROXY] Reset all accounts to dedicated primary startup mode: {initial_mode}")
    except Exception as e:
        log.warning(f"[PROXY] Note on proxy reset: {e}")