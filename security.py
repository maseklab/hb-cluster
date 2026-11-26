# ==============================================================================
# HB CLUSTER MANAGER - ZABEZPEČENÍ WEBU (security.py)
# ------------------------------------------------------------------------------
# Krok 1 Zabezpečeného režimu: volitelné přihlášení do webového rozhraní.
#  - Ve výchozím stavu VYPNUTO. Zapíná se v Nastavení → Zabezpečení.
#  - Heslo se ukládá jen jako hash (scrypt ze standardní knihovny), nikdy v čitelné podobě.
#  - Přihlášení = náhodný token v cookie (HttpOnly). Na Manageru se drží jen jeho SHA-256.
#  - „Zapamatovat toto zařízení“ = platnost ~1 rok, jinak do zavření prohlížeče (max. 12 h nečinnosti).
#  - Omezení pokusů: po 5 chybných heslech z jedné adresy pauza 1 → 5 → 15 minut.
#  - Obnova: soubor /data/reset_admin_password → zabezpečení se vypne a heslo smaže.
#  - Domácí sítě: volitelně bez přihlášení. Požadavek přes Cloudflare (CF-Connecting-IP) nikdy není domácí.
#
# Adresy, které volají Windows a Linux workery, se heslem nezamykají (viz WORKER_PUBLIC_*). Chrání je
# krok 2 – schvalování workerů (klíč workeru). Krok 1 a 2 se zapínají a vypínají JEN SPOLU
# (main.sync_worker_approval_with_signin), jinak by šlo bez hesla číst SMB heslo nebo měnit nastavení.
# ==============================================================================
import os
import json
import time
import hmac
import secrets
import hashlib
import ipaddress
import threading
import urllib.parse

SESSION_COOKIE = "hbm_session"
REMEMBER_SEC = 365 * 24 * 3600          # „Zapamatovat toto zařízení“
SHORT_IDLE_SEC = 12 * 3600              # bez zapamatování: po 12 h nečinnosti odhlásit
MIN_PASSWORD_LEN = 8
MAX_DEVICES = 50
LOCK_STEPS_SEC = (60, 300, 900)         # 1 / 5 / 15 minut
FAILS_PER_STEP = 5

# 172.16.0.0/12 záměrně NE: je to rozsah sítí Dockeru – brána (např. 172.18.0.1) by jako „domácí“ pustila
# každého, kdo k Manageru dorazí přes Docker / NAS bez skutečné adresy (Synology ji v bridge síti maskuje).
DEFAULT_HOME_NETWORKS = ["192.168.0.0/16", "10.0.0.0/8", "127.0.0.0/8", "::1/128", "fc00::/7"]

# Adresy, které musí zůstat veřejné (načtení přihlašovací stránky, PWA, ikony, texty).
PUBLIC_EXACT = {
    "/login", "/api/auth/login", "/api/auth/logout", "/api/auth/status",
    "/api/auth/passkey/options", "/api/auth/passkey/verify",
    "/manifest.webmanifest", "/favicon.ico", "/locales.js", "/app.js",
}
PUBLIC_PREFIX = ("/static/",)

# Adresy, které volají workery (Windows i Linux). Krok 1 je NIKDY nezamyká.
WORKER_PUBLIC_EXACT = {
    "/api/workers/handshake", "/api/workers/ping", "/api/locales",
    "/api/v2/cluster/action", "/api/v2/jobs/action",
    "/api/v2/binaries/build_progress",
    "/api/v2/workers/package/windows", "/api/v2/workers/download_windows_package", "/api/worker/download_windows_zip",
}
WORKER_PUBLIC_PREFIX = (
    "/api/jobs/",            # next, progress, complete, relay_* … (web používá /api/v2/jobs/…)
    "/api/update/",          # OTA balíčky, binárky, certifikát
    "/api/v2/binaries/upload/",
)


def _is_worker_policy_path(p: str) -> bool:
    # /api/v2/workers/{jméno}/policy – volá i Windows worker (tlačítka v okně workeru)
    # /api/workers/{jméno}/config – synchronizace nastavení workeru
    parts = p.strip("/").split("/")
    if len(parts) == 5 and parts[:3] == ["api", "v2", "workers"] and parts[4] == "policy":
        return True
    if len(parts) == 4 and parts[:2] == ["api", "workers"] and parts[3] == "config":
        return True
    return False


PACKAGE_PATHS = {"/api/v2/workers/package/windows", "/api/v2/workers/download_windows_package", "/api/worker/download_windows_zip"}
SHARED_WORKER_EXACT = {"/api/v2/cluster/action", "/api/v2/jobs/action"}


def worker_guard_kind(path: str):
    """Které cesty hlídá schvalování workerů: worker | shared (volá worker i web) | package | None.
    /api/update/* a texty zůstávají vždy volné – i neschválený / starší worker se musí umět aktualizovat."""
    p = path or "/"
    if p in PACKAGE_PATHS:
        return "package"
    if p in SHARED_WORKER_EXACT:
        return "shared"
    parts = p.strip("/").split("/")
    if len(parts) == 5 and parts[:3] == ["api", "v2", "workers"] and parts[4] == "policy":
        return "shared"
    if p in ("/api/workers/handshake", "/api/workers/ping", "/api/v2/binaries/build_progress"):
        return "worker"
    if len(parts) == 4 and parts[:2] == ["api", "workers"] and parts[3] == "config":
        return "worker"
    if p.startswith("/api/v2/binaries/upload/"):
        return "shared"                       # builder (worker) i ruční nahrání z webu; builder hlídá main._builder_upload_refusal
    if p.startswith("/api/jobs/"):
        return "worker"
    return None


# OCHRANA PŘED CIZÍM WEBEM (CSRF) – platí i bez přihlášení.
# Prohlížeč u každého požadavku sám posílá hlavičku Sec-Fetch-Site (same-origin = vlastní stránka Manageru,
# none = adresa zadaná ručně / záložka). Požadavek na /api/* spuštěný jinou stránkou (cross-site, same-site)
# Manager odmítne, takže cizí web otevřený v prohlížeči nemůže Manager ovládat. Workery, skripty a curl
# hlavičku neposílají → nedotčené. Starší prohlížeč bez Sec-Fetch-Site: u měnících požadavků se porovná Origin.
# Výjimky (jen GET/HEAD): odkazy a dashboardy, které se otevírají odjinud.
CROSS_SITE_GET_OK_EXACT = {"/api/stats", "/api/locales", "/api/auth/status"}
CROSS_SITE_GET_OK_PREFIX = ("/api/update/",)


def _atomic_write_json(path: str, data, mode: int = 0o600):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        try:
            os.chmod(tmp, mode)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def cross_site_refusal(method: str, path: str, headers) -> str:
    """Vrátí důvod odmítnutí požadavku z cizího webu, nebo "" = v pořádku."""
    p = path or "/"
    if not p.startswith("/api/"):
        return ""
    m = (method or "GET").upper()
    if m in ("GET", "HEAD") and (p in CROSS_SITE_GET_OK_EXACT or p.startswith(CROSS_SITE_GET_OK_PREFIX)):
        return ""
    sfs = (headers.get("sec-fetch-site") or "").strip().lower()
    if sfs:
        return "" if sfs in ("same-origin", "none") else sfs
    if m in ("GET", "HEAD", "OPTIONS"):
        return ""
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return ""                                   # worker, skript, curl – Origin neposílají
    if origin.lower() == "null":
        return "origin-null"
    try:
        o_host = urllib.parse.urlsplit(origin).netloc.lower()
    except Exception:
        return "origin-invalid"
    hosts = {(headers.get("host") or "").strip().lower()}
    for xf in (headers.get("x-forwarded-host") or "").split(","):
        if xf.strip():
            hosts.add(xf.strip().lower())
    return "" if (o_host and o_host in hosts) else "origin-mismatch"


# ID MANAGERU: náhodné, vytvoří se jednou a uloží do /data (manager_identity.json). Manager ho posílá
# v každé odpovědi (X-HB-Manager-Id). Worker si ho při prvním spojení zapamatuje a od Manageru s jiným ID
# nic nepřijme (ochrana proti falešnému Manageru). Není tajné – je to jen poznávací značka tohoto Manageru.
MANAGER_ID_RE_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"


def load_or_create_manager_id(path: str, log=None) -> str:
    import re as _re
    try:
        with open(path, "r", encoding="utf-8") as f:
            mid = str((json.load(f) or {}).get("id") or "")
        if _re.match(MANAGER_ID_RE_PATTERN, mid):
            return mid
    except FileNotFoundError:
        pass
    except Exception as e:
        if log:
            log(f"[SECURITY] Soubor s ID Manageru nejde přečíst ({e}) – vytvářím nové ID.")
    mid = secrets.token_urlsafe(18)
    try:
        _atomic_write_json(path, {"id": mid, "created": int(time.time())}, mode=0o644)
        if log:
            log(f"[SECURITY] Vytvořeno ID tohoto Manageru: {mid} (workery se na něj navážou).")
    except Exception as e:
        if log:
            log(f"[SECURITY] CHYBA: ID Manageru nejde uložit ({e}) – po restartu se změní a workery ho odmítnou.")
    return mid


def is_public_path(path: str) -> bool:
    p = path or "/"
    if p in PUBLIC_EXACT or p in WORKER_PUBLIC_EXACT:
        return True
    if p.startswith(PUBLIC_PREFIX) or p.startswith(WORKER_PUBLIC_PREFIX):
        return True
    return _is_worker_policy_path(p)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_password(pw: str) -> dict:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return {"alg": "scrypt", "n": n, "r": r, "p": p, "salt": salt.hex(), "hash": dk.hex()}


def verify_password(pw: str, rec: dict) -> bool:
    try:
        if not rec or rec.get("alg") != "scrypt":
            return False
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(rec["salt"]),
                            n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]), dklen=32)
        return hmac.compare_digest(dk.hex(), rec["hash"])
    except Exception:
        return False


def _norm_networks(items) -> list:
    out = []
    for raw in (items or []):
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            out.append(str(ipaddress.ip_network(s, strict=False)))
        except ValueError:
            continue
    return out[:64]


# ------------------------------------------------------------------------------
# SÍŤOVÉ PROSTŘEDÍ MANAGERU: vidí Manager skutečné adresy zařízení?
# V běžné (bridge) síti Dockeru na některých systémech (typicky Synology) přichází každý požadavek
# z vnitřní brány Dockeru (např. 172.18.0.1) – Manager pak nerozliší PC, mobil ani reverse proxy.
# V režimu host (network_mode: host) vidí skutečné adresy. Domácí síť bez přihlášení a důvěryhodná
# proxy jsou proto povolené jen tehdy, když adresa NENÍ zamaskovaná branou Dockeru.
# ------------------------------------------------------------------------------
_netenv_cache = {"at": 0.0, "val": None}


def _default_gateway() -> str:
    try:
        with open("/proc/net/route", "r", encoding="ascii", errors="ignore") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw = int(parts[2], 16)
                    return ".".join(str((gw >> (8 * i)) & 0xFF) for i in range(4))
    except Exception:
        pass
    return ""


def network_env() -> dict:
    now = time.time()
    if _netenv_cache["val"] is not None and now - _netenv_cache["at"] < 300:
        return _netenv_cache["val"]
    in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    try:
        ifaces = os.listdir("/sys/class/net")
    except Exception:
        ifaces = []
    # v režimu host kontejner vidí rozhraní hostitele (docker0, br-…); v bridge jen lo + eth0
    host_mode = (not in_container) or any(n == "docker0" or n.startswith(("br-", "veth")) for n in ifaces)
    gw = "" if host_mode else _default_gateway()
    val = {"in_container": in_container, "host_mode": host_mode, "docker_gateway": gw}
    _netenv_cache.update({"at": now, "val": val})
    return val


def is_masked_peer(ip_s: str) -> bool:
    """True = adresa je jen brána Dockeru (skutečné zařízení za ní Manager nevidí)."""
    env = network_env()
    return bool(env["docker_gateway"]) and str(ip_s or "").strip() == env["docker_gateway"]


class SecurityStore:
    def __init__(self, path: str, reset_flag_path: str, log=None):
        self.path = path
        self.reset_flag_path = reset_flag_path
        self.log = log or (lambda m: None)
        self.lock = threading.RLock()
        self.fails = {}   # ip -> {"count": n, "level": k, "until": ts}
        self.data = self._defaults()
        self.load()

    @staticmethod
    def _defaults() -> dict:
        return {
            "enabled": False,
            "password": None,
            "sessions": {},            # sha(token) -> {id, created, last_seen, remember, ua, ip}
            "home_bypass": False,
            "home_networks": list(DEFAULT_HOME_NETWORKS),
            "behind_proxy": False,
            "trusted_proxies": [],
            "trust_cf_header": False,  # důvěřovat CF-Connecting-IP jen když uživatel výslovně používá Cloudflare
            "public_stats": True,      # GET /api/stats (jen počty) bez přihlášení – widgety Homepage / Homarr / Dashy
            "passkeys": [],            # krok 3: přihlášení otiskem / obličejem (WebAuthn), jen přes HTTPS
        }

    # ---------------------------------------------------------------- úložiště
    def load(self):
        with self.lock:
            try:
                if os.path.exists(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    base = self._defaults()
                    base.update({k: d[k] for k in base if k in d})
                    base["home_networks"] = _norm_networks(base.get("home_networks")) or list(DEFAULT_HOME_NETWORKS)
                    base["trusted_proxies"] = _norm_networks(base.get("trusted_proxies"))
                    if not isinstance(base.get("passkeys"), list):
                        base["passkeys"] = []
                    if not isinstance(base.get("sessions"), dict):
                        base["sessions"] = {}
                    if base["enabled"] and not base.get("password"):
                        base["enabled"] = False
                    self.data = base
            except Exception as e:
                self.log(f"[SECURITY] security.json nelze načíst ({e}) – zabezpečení zůstává vypnuté.")
                self.data = self._defaults()
            self.check_reset_flag()

    def save(self):
        with self.lock:
            try:
                _atomic_write_json(self.path, self.data, mode=0o600)
            except Exception as e:
                self.log(f"[SECURITY] security.json nelze uložit: {e}")

    def check_reset_flag(self) -> bool:
        """Soubor /data/reset_admin_password = zapomenuté heslo → vypnout zabezpečení."""
        try:
            if self.reset_flag_path and os.path.exists(self.reset_flag_path):
                with self.lock:
                    self.data["enabled"] = False
                    self.data["password"] = None
                    self.data["sessions"] = {}
                    self.data["passkeys"] = []
                    self.save()
                try:
                    os.remove(self.reset_flag_path)
                except Exception:
                    pass
                self.log("[SECURITY] Nalezen soubor reset_admin_password – přihlášení do webu bylo vypnuto a heslo smazáno.")
                return True
        except Exception:
            pass
        return False

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled") and self.data.get("password"))

    # ---------------------------------------------------------- adresa klienta
    @staticmethod
    def _parse_ip_cand(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        try:
            return str(ipaddress.ip_address(s))
        except ValueError:
            pass
        # Bezpečně podpořit [IPv6]:port nebo IPv4:port, cokoli jiného odmítnout
        if s.startswith("[") and "]:" in s:
            host_part = s[1:s.index("]:")]
            try:
                return str(ipaddress.ip_address(host_part))
            except ValueError:
                return ""
        if s.count(":") == 1:
            host_part, port_part = s.split(":", 1)
            if port_part.isdigit():
                try:
                    return str(ipaddress.ip_address(host_part))
                except ValueError:
                    return ""
        return ""

    def _trusted_proxy_client_ip(self, request) -> str:
        """Vrátí ověřenou IP klienta za důvěryhodnou proxy, nebo '' pokud hlavička chybí / není platná (fail-closed)."""
        proxies = []
        for n in self.data.get("trusted_proxies") or []:
            try:
                proxies.append(ipaddress.ip_network(n, strict=False))
            except ValueError:
                continue

        def trusted(ip_s):
            parsed = self._parse_ip_cand(ip_s)
            if not parsed:
                return False
            ip = ipaddress.ip_address(parsed)
            return any(ip in n for n in proxies)

        # CF-Connecting-IP brát JEN když uživatel explicitně zapnul „Provoz přes Cloudflare“
        if self.data.get("trust_cf_header"):
            cf_ip = self._parse_ip_cand(request.headers.get("cf-connecting-ip") or "")
            if cf_ip:
                return cf_ip
        chain = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
        for cand in reversed(chain):          # zprava: první adresa, která není naše proxy
            parsed = self._parse_ip_cand(cand)
            if not parsed:
                return ""                     # neplatná položka v řetězci → fail-closed
            if not trusted(parsed):
                return parsed
        return ""

    def client_ip(self, request) -> str:
        """Skutečná adresa klienta pro rozhodnutí o domácí síti a omezení pokusů.
        X-Forwarded-For se bere JEN když spojení přišlo přímo od důvěryhodné reverse proxy."""
        peer = (request.client.host if request.client else "") or ""
        if not self._peer_is_trusted_proxy(request):
            return peer
        resolved = self._trusted_proxy_client_ip(request)
        return resolved if resolved else peer

    def _peer_is_trusted_proxy(self, request) -> bool:
        if not self.data.get("behind_proxy"):
            return False
        peer = (request.client.host if request.client else "") or ""
        if is_masked_peer(peer):
            return False
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return False
        for n in self.data.get("trusted_proxies") or []:
            try:
                if ip in ipaddress.ip_network(n, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def is_home_request(self, request) -> bool:
        if not self.data.get("home_bypass"):
            return False
        if request.headers.get("cf-connecting-ip"):
            return False      # přes Cloudflare (tunel / WARP na veřejnou adresu) nikdy „doma“
        peer = (request.client.host if request.client else "") or ""
        if is_masked_peer(peer):
            return False      # Manager nevidí skutečné zařízení (brána Dockeru) → nikdy „doma“
        is_tproxy = self._peer_is_trusted_proxy(request)
        if is_tproxy:
            # Fail-closed: od důvěryhodné proxy vyžadujeme platnou IP klienta z X-Forwarded-For, NIKDY nespadnout na peer proxy!
            eff_ip = self._trusted_proxy_client_ip(request)
            if not eff_ip:
                return False
        else:
            # Neznámá proxy: jakákoli proxy hlavička znamená neznámý původ → raději heslo
            for ph in ("x-forwarded-for", "forwarded", "x-real-ip", "true-client-ip", "x-forwarded-host"):
                if (request.headers.get(ph) or "").strip():
                    return False
            eff_ip = peer
        try:
            ip = ipaddress.ip_address(eff_ip)
        except ValueError:
            return False
        for n in self.data.get("home_networks") or []:
            try:
                if ip in ipaddress.ip_network(n, strict=False):
                    return True
            except ValueError:
                continue
        return False

    # ----------------------------------------------------------------- relace
    def session_for(self, request):
        tok = request.cookies.get(SESSION_COOKIE) or ""
        if not tok:
            return None
        key = _sha(tok)
        now = time.time()
        with self.lock:
            s = self.data["sessions"].get(key)
            if not s:
                return None
            limit = REMEMBER_SEC if s.get("remember") else SHORT_IDLE_SEC
            if now - float(s.get("last_seen") or 0) > limit:
                self.data["sessions"].pop(key, None)
                self.save()
                return None
            if now - float(s.get("last_seen") or 0) > 300:   # neukládat při každém požadavku
                s["last_seen"] = now
                s["ip"] = self.client_ip(request)
                self.save()
            return s

    def is_authorized(self, request) -> bool:
        if not self.enabled:
            return True
        if self.session_for(request):
            return True
        return self.is_home_request(request)

    def create_session(self, request, remember: bool) -> str:
        tok = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            sess = self.data["sessions"]
            if len(sess) >= MAX_DEVICES:      # nejdéle nepoužité pryč
                for k, _ in sorted(sess.items(), key=lambda kv: kv[1].get("last_seen") or 0)[:len(sess) - MAX_DEVICES + 1]:
                    sess.pop(k, None)
            sess[_sha(tok)] = {
                "id": secrets.token_hex(6),
                "created": now,
                "last_seen": now,
                "remember": bool(remember),
                "ua": (request.headers.get("user-agent") or "")[:300],
                "ip": self.client_ip(request),
            }
            self.save()
        return tok

    def drop_session_token(self, tok: str):
        if not tok:
            return
        with self.lock:
            if self.data["sessions"].pop(_sha(tok), None) is not None:
                self.save()

    def drop_device(self, dev_id: str) -> bool:
        with self.lock:
            for k, s in list(self.data["sessions"].items()):
                if s.get("id") == dev_id:
                    self.data["sessions"].pop(k, None)
                    self.save()
                    return True
        return False

    def drop_other_devices(self, request) -> int:
        cur = _sha(request.cookies.get(SESSION_COOKIE) or "")
        with self.lock:
            before = len(self.data["sessions"])
            self.data["sessions"] = {k: v for k, v in self.data["sessions"].items() if k == cur}
            self.save()
            return before - len(self.data["sessions"])

    def devices(self, request) -> list:
        cur = _sha(request.cookies.get(SESSION_COOKIE) or "")
        with self.lock:
            out = []
            for k, s in self.data["sessions"].items():
                out.append({
                    "id": s.get("id"), "created": s.get("created"), "last_seen": s.get("last_seen"),
                    "remember": bool(s.get("remember")), "ua": s.get("ua") or "", "ip": s.get("ip") or "",
                    "current": k == cur,
                })
            out.sort(key=lambda d: (not d["current"], -(d["last_seen"] or 0)))
            return out

    # -------------------------------------------------------- omezení pokusů
    MAX_FAIL_ENTRIES = 5000

    def _prune_fails_locked(self, now: float):
        stale = [k for k, v in self.fails.items()
                 if now - float(v.get("last_at") or 0) > 3600 and float(v.get("until") or 0) <= now]
        for k in stale:
            self.fails.pop(k, None)
        if len(self.fails) > self.MAX_FAIL_ENTRIES:
            oldest = sorted(self.fails.items(), key=lambda kv: float(kv[1].get("last_at") or 0))
            for k, _ in oldest[:len(self.fails) - self.MAX_FAIL_ENTRIES]:
                self.fails.pop(k, None)

    def locked_for(self, ip: str) -> int:
        with self.lock:
            f = self.fails.get(ip)
            if not f:
                return 0
            rem = int(f.get("until", 0) - time.time())
            return rem if rem > 0 else 0

    def register_fail(self, ip: str) -> int:
        now = time.time()
        with self.lock:
            self._prune_fails_locked(now)
            f = self.fails.get(ip)
            if not f or (now - float(f.get("last_at") or 0) > 3600 and float(f.get("until") or 0) <= now):
                f = {"count": 0, "level": 0, "until": 0, "last_at": now}
                self.fails[ip] = f
            f["last_at"] = now
            f["count"] += 1
            if f["count"] >= FAILS_PER_STEP:
                step = LOCK_STEPS_SEC[min(f["level"], len(LOCK_STEPS_SEC) - 1)]
                f["until"] = now + step
                f["level"] += 1
                f["count"] = 0
                self.log(f"[SECURITY] {FAILS_PER_STEP}× chybné heslo z {ip} – další pokus až za {step // 60} min.")
                return step
            return 0

    def register_success(self, ip: str):
        with self.lock:
            self.fails.pop(ip, None)

    # ------------------------------------------------------------- nastavení
    def summary(self, request) -> dict:
        peer = (request.client.host if request.client else "") or ""
        has_xff = bool((request.headers.get("x-forwarded-for") or "").strip())
        env = network_env()
        masked = is_masked_peer(peer)
        return {
            "peer_ip": peer,
            "addresses_masked": masked,          # → domácí síť a proxy nelze nastavit, doporučit režim host
            "host_mode": bool(env["host_mode"]),
            "proxy_hint": has_xff and not masked and not self._peer_is_trusted_proxy(request),
            "enabled": self.enabled,
            "home_bypass": bool(self.data.get("home_bypass")),
            "home_networks": list(self.data.get("home_networks") or []),
            "behind_proxy": bool(self.data.get("behind_proxy")),
            "trusted_proxies": list(self.data.get("trusted_proxies") or []),
            "trust_cf_header": bool(self.data.get("trust_cf_header")),
            "client_ip": self.client_ip(request),
            "is_home_now": self.is_home_request(request) if self.data.get("home_bypass") else False,
            "via_cloudflare": bool(request.headers.get("cf-connecting-ip")),
            "device_count": len(self.data.get("sessions") or {}),
            "public_stats": bool(self.data.get("public_stats", True)),
        }

    def enable(self, pw: str):
        with self.lock:
            self.data["password"] = hash_password(pw)
            self.data["enabled"] = True
            self.data["sessions"] = {}
            self.save()

    def disable(self):
        with self.lock:
            self.data["enabled"] = False
            self.data["password"] = None
            self.data["sessions"] = {}
            self.data["passkeys"] = []
            self.save()

    def set_password(self, pw: str, request, logout_others: bool):
        with self.lock:
            self.data["password"] = hash_password(pw)
            if logout_others:
                cur = _sha(request.cookies.get(SESSION_COOKIE) or "")
                self.data["sessions"] = {k: v for k, v in self.data["sessions"].items() if k == cur}
            self.save()

    def check_password(self, pw: str) -> bool:
        return verify_password(pw or "", self.data.get("password"))

    def set_network_options(self, home_bypass, home_networks, behind_proxy, trusted_proxies, public_stats=None, trust_cf_header=None):
        with self.lock:
            if public_stats is not None:
                self.data["public_stats"] = bool(public_stats)
            if home_bypass is not None:
                self.data["home_bypass"] = bool(home_bypass)
            if home_networks is not None:
                self.data["home_networks"] = _norm_networks(home_networks) or list(DEFAULT_HOME_NETWORKS)
            if behind_proxy is not None:
                self.data["behind_proxy"] = bool(behind_proxy)
                if not self.data["behind_proxy"]:
                    self.data["trust_cf_header"] = False
            if trusted_proxies is not None:
                self.data["trusted_proxies"] = _norm_networks(trusted_proxies)
            if trust_cf_header is not None:
                self.data["trust_cf_header"] = bool(trust_cf_header) and bool(self.data.get("behind_proxy"))
            self.save()


# ==============================================================================
# REGISTR KLÍČŮ WORKERŮ (Zabezpečení, krok 2 – fáze A: jen zapisovat, nic nevynucovat)
# Worker posílá vlastní náhodný klíč v hlavičce X-HB-Worker-Key. Manager si drží jen jeho SHA-256
# a ke kterému jménu / adrese patří. Až budou klíč posílat všechny workery, půjde zapnout schvalování.
# ==============================================================================
class WorkerRegistry:
    """Klíče workerů + schvalování (Zabezpečení, krok 2).
    status: known (zapsán při vypnutém schvalování) | approved | pending | rejected (do `until`) | ignored
    test=True = zbytek dřívější simulované žádosti (funkce odstraněna) – nikdy nedostane přístup a při
    prvním přehledu se smaže."""
    SAVE_EVERY_S = 60
    REJECT_SEC = 24 * 3600
    AUTO_WINDOW_SEC = 15 * 60
    JOIN_TOKEN_SEC = 3600
    MAX_PENDING = 30
    MAX_KNOWN_KEYS = 500

    def __init__(self, path: str, log=None):
        self.path = path
        self.log = log or (lambda m: None)
        self.lock = threading.RLock()
        self.keys = {}          # sha(key) -> {id, name, bound_name, first_seen, last_seen, ip, version, status, until, test}
        self.approval = False
        self.auto_until = 0.0
        self.join_tokens = {}   # sha(token) -> {expires, join, join_used}
        self._dirty_at = 0.0
        self._saved_at = 0.0
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    if isinstance(d.get("keys"), dict):
                        self.keys = d["keys"]
                    self.approval = bool(d.get("approval"))
                    self.auto_until = float(d.get("auto_until") or 0)
                    if isinstance(d.get("join_tokens"), dict):
                        self.join_tokens = d["join_tokens"]
        except Exception as e:
            self.log(f"[SECURITY] worker_keys.json nelze načíst ({e}).")
        for kh, r in self.keys.items():
            r.setdefault("id", kh[:12])
            r.setdefault("status", "known")
            if r.get("status") == "approved" and not r.get("bound_name") and r.get("name"):
                r["bound_name"] = r["name"]

    # ---------------------------------------------------------------- úložiště
    def _save(self, force=False):
        now = time.time()
        if not force and now - self._saved_at < self.SAVE_EVERY_S:
            self._dirty_at = self._dirty_at or now
            return
        self.join_tokens = {k: v for k, v in self.join_tokens.items() if float(v.get("expires") or 0) > now}
        try:
            _atomic_write_json(
                self.path,
                {"keys": self.keys, "approval": self.approval, "auto_until": self.auto_until,
                 "join_tokens": self.join_tokens},
                mode=0o600,
            )
            self._saved_at = now
            self._dirty_at = 0.0
        except Exception as e:
            self.log(f"[SECURITY] worker_keys.json nelze uložit: {e}")

    def flush(self):
        with self.lock:
            if self._dirty_at:
                self._save(force=True)

    def _find(self, rec_id: str):
        for kh, r in self.keys.items():
            if r.get("id") == rec_id:
                return kh, r
        return None, None

    def bound_name_for(self, key: str) -> str:
        """Vrátí svázané jméno pro daný klíč workeru (pokud existuje)."""
        if not key or len(key) < 32:
            return ""
        kh = _sha(key)
        with self.lock:
            rec = self.keys.get(kh)
            if not rec:
                return ""
            return str(rec.get("bound_name") or rec.get("name") or "")

    # ------------------------------------------------------- záznam a kontrola
    def touch(self, key: str, name: str, ip: str, version: str = ""):
        """Jen zapsat (při vypnutém schvalování). Nový klíč = status known."""
        if not key or len(key) < 32:
            return
        kh = _sha(key)
        now = time.time()
        with self.lock:
            rec = self.keys.get(kh)
            if rec is None:
                if self.approval:
                    return      # se zapnutým schvalováním vzniká nový záznam JEN přes check() (jako čekající), nikdy jako „známý“
                if len(self.keys) >= self.MAX_KNOWN_KEYS:
                    known_items = [(k, r) for k, r in self.keys.items() if r.get("status") == "known"]
                    if known_items:
                        oldest_k = min(known_items, key=lambda kv: float(kv[1].get("last_seen") or 0))[0]
                        self.keys.pop(oldest_k, None)
                    else:
                        return
                if name and any(r.get("name") == name and not r.get("test") for r in self.keys.values()):
                    self.log(f"[SECURITY] Worker „{name}“ se ohlásil s NOVÝM klíčem (přeinstalace?) z {ip}.")
                self.keys[kh] = {"id": kh[:12], "name": name, "first_seen": now, "last_seen": now, "ip": ip,
                                 "version": version, "status": "known"}
                self._save(force=len(self.keys) < 100)
                return
            changed = (name and rec.get("name") != name) or (ip and rec.get("ip") != ip)
            rec["last_seen"] = now
            if name:
                rec["name"] = name
            if ip:
                rec["ip"] = ip
            if version:
                rec["version"] = version
            self._save(force=bool(changed))

    def check(self, key: str, name: str, ip: str, version: str = "", join_token: str = "") -> str:
        """Schvalování zapnuto: vrátí ok | pending | rejected | ignored | no_key (přejmenovaný schválený worker → pending)."""
        if not self.approval:
            self.touch(key, name, ip, version)
            return "ok"
        if not key or len(key) < 32:
            return "no_key"
        kh = _sha(key)
        now = time.time()
        with self.lock:
            rec = self.keys.get(kh)
            state_changed = False
            if rec is None:
                pend = sum(1 for r in self.keys.values() if r.get("status") == "pending")
                if pend >= self.MAX_PENDING:
                    return "pending"          # ochrana proti zahlcení žádostmi – další se nezapisují
                rec = {"id": kh[:12], "name": name, "first_seen": now, "last_seen": now, "ip": ip,
                       "version": version, "status": "pending"}
                self.keys[kh] = rec
                state_changed = True
                self.log(f"[SECURITY] Nový worker „{name}“ ({ip}) čeká na schválení.")
            st = rec.get("status") or "pending"
            # Schválený klíč je pevně svázán se svým jménem workeru – nemůže se vydávat za jiný worker
            if st == "approved":
                bound = rec.get("bound_name") or rec.get("name") or ""
                if not rec.get("bound_name") and bound:
                    rec["bound_name"] = bound
                    state_changed = True
                if bound and name and bound != name:
                    # Schválený klíč se ohlásil pod jiným jménem (přejmenovaný worker, nebo cizí použití klíče):
                    # pod novým jménem nic nepovolit bez správce → zpět mezi čekající s novým jménem a poznámkou,
                    # jak se worker jmenoval dřív. Schválením se nové jméno stane svázaným (bound_name).
                    rec["status"] = "pending"
                    rec["prev_name"] = bound
                    rec["name"] = name
                    rec.pop("bound_name", None)
                    st = "pending"
                    state_changed = True
                    self.log(f"[SECURITY] Schválený worker „{bound}“ se ohlásil jako „{name}“ ({ip}) – čeká na nové schválení.")
            elif st == "pending" and name and rec.get("prev_name") == name:
                # přejmenování vráceno zpět na dřív schválené jméno → původní schválení platí dál
                st = rec["status"] = "approved"
                rec["bound_name"] = name
                rec.pop("prev_name", None)
                state_changed = True
                self.log(f"[SECURITY] Worker „{name}“ ({ip}) se vrátil ke schválenému jménu – pokračuje.")
            rec["last_seen"] = now
            if st != "approved" and name:
                rec["name"] = name
            if ip:
                rec["ip"] = ip
            if version:
                rec["version"] = version
            if st == "known":
                # „známé“ záznamy schvaluje jen zapnutí schvalování (set_approval); cokoli jiného musí počkat
                st = rec["status"] = "pending"
                state_changed = True
            if st == "rejected" and now >= float(rec.get("until") or 0):
                st = rec["status"] = "pending"
                rec.pop("until", None)
                state_changed = True
            if st == "pending":
                if now < self.auto_until:
                    st = rec["status"] = "approved"
                    if rec.get("name") or name:
                        rec["bound_name"] = rec.get("name") or name
                    state_changed = True
                    rec.pop("prev_name", None)
                    self.log(f"[SECURITY] Worker „{rec.get('bound_name') or name}“ ({ip}) schválen automaticky (15min okno).")
                elif join_token and self.join_valid(join_token, need_join=True):
                    st = rec["status"] = "approved"
                    if rec.get("name") or name:
                        rec["bound_name"] = rec.get("name") or name
                    jt = self.join_tokens.get(_sha(join_token))
                    if jt is not None:
                        jt["join_used"] = True
                    state_changed = True
                    rec.pop("prev_name", None)
                    self.log(f"[SECURITY] Worker „{rec.get('bound_name') or name}“ ({ip}) schválen předem (jednorázový kód z průvodce).")
            self._save(force=state_changed)
            return "ok" if st == "approved" else st

    # ------------------------------------------------------------- nastavení
    def set_approval(self, on: bool):
        with self.lock:
            self.approval = bool(on)
            if self.approval:
                n = 0
                for r in self.keys.values():
                    if r.get("status") == "known" and not r.get("test"):
                        r["status"] = "approved"
                        if r.get("name"):
                            r["bound_name"] = r["name"]
                        n += 1
                self.log(f"[SECURITY] Schvalování workerů ZAPNUTO ({n} známých workerů schváleno).")
            else:
                self.auto_until = 0.0
                self.log("[SECURITY] Schvalování workerů VYPNUTO.")
            self._save(force=True)

    def start_auto_window(self) -> float:
        with self.lock:
            self.auto_until = time.time() + self.AUTO_WINDOW_SEC
            self._save(force=True)
            return self.auto_until

    def stop_auto_window(self):
        with self.lock:
            self.auto_until = 0.0
            self._save(force=True)

    def act(self, rec_id: str, action: str) -> dict:
        """approve | reject | ignore | remove."""
        with self.lock:
            kh, r = self._find(rec_id)
            if not r:
                return {"ok": False}
            name = r.get("name") or "?"
            if r.get("test"):
                self.keys.pop(kh, None)
                self._save(force=True)
                return {"ok": True, "action": "remove", "name": name}
            if action == "approve":
                r["status"] = "approved"
                if r.get("name"):
                    r["bound_name"] = r["name"]
                r.pop("until", None)
                r.pop("prev_name", None)
            elif action == "reject":
                r["status"] = "rejected"
                r["until"] = time.time() + self.REJECT_SEC
            elif action == "ignore":
                r["status"] = "ignored"
                r.pop("until", None)
            elif action == "remove":
                self.keys.pop(kh, None)
            else:
                return {"ok": False}
            self.log(f"[SECURITY] Worker „{name}“: {action}.")
            self._save(force=True)
            return {"ok": True, "action": action, "name": name}

    # ------------------------------------------- jednorázový kód (balíček z průvodce)
    def new_join_token(self, join: bool) -> dict:
        import secrets as _s
        tok = _s.token_urlsafe(24)
        exp = time.time() + self.JOIN_TOKEN_SEC
        with self.lock:
            self.join_tokens[_sha(tok)] = {"expires": exp, "join": bool(join), "join_used": False}
            self._save(force=True)
        return {"token": tok, "expires": exp, "join": bool(join)}

    def join_valid(self, tok: str, need_join: bool = False) -> bool:
        if not tok:
            return False
        with self.lock:
            rec = self.join_tokens.get(_sha(tok))
            if not rec or float(rec.get("expires") or 0) <= time.time():
                return False
            if need_join:
                return bool(rec.get("join")) and not bool(rec.get("join_used"))
            return True

    # ----------------------------------------------------------------- přehled
    def summary(self) -> dict:
        now = time.time()
        with self.lock:
            # zbytky dřívějších simulovaných žádostí (funkce odstraněna) hned smazat
            stale = [k for k, r in self.keys.items() if r.get("test")]
            for k in stale:
                self.keys.pop(k, None)
            if stale:
                self._save(force=True)
            out = [{"id": r.get("id"), "name": r.get("name") or "?", "first_seen": r.get("first_seen"),
                    "last_seen": r.get("last_seen"), "ip": r.get("ip") or "", "version": r.get("version") or "",
                    "status": r.get("status") or "known", "until": r.get("until") or 0, "test": bool(r.get("test")),
                    "prev_name": r.get("prev_name") or ""}
                   for r in self.keys.values()]
        out.sort(key=lambda r: (r["name"].lower(), -(r["last_seen"] or 0)))
        return {"approval": self.approval, "auto_until": self.auto_until if self.auto_until > now else 0, "entries": out}


def is_https(request, store=None) -> bool:
    if (request.url.scheme or "") == "https":
        return True
    if store is not None and not store._peer_is_trusted_proxy(request):
        return False
    if (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https":
        return True
    if store is None or store.data.get("trust_cf_header"):
        if request.headers.get("cf-visitor") and "https" in request.headers.get("cf-visitor", ""):
            return True
    return False


# ==============================================================================
# PASSKEYS (Zabezpečení, krok 3): přihlášení otiskem prstu / obličejem / klíčem
# - Jen při zapnutém přihlášení do webu a jen přes HTTPS (prohlížeče jinak WebAuthn nepovolí).
# - Passkey je vázaný na název domény (RP ID) → funguje doma i venku přes stejnou doménu (split DNS).
# - Knihovna `webauthn` se načítá až při použití: když v obrazu chybí, Manager běží dál, jen bez passkeys.
# ==============================================================================
_PK_USER_ID = b"hb-cluster-admin"
_PK_MAX_CHALLENGES = 128
_pk_challenges = {}          # challenge_id -> {"challenge": bytes, "kind": "reg"|"auth", "exp": ts, "rp": str, "origin": str}
_pk_lock = threading.Lock()


def passkeys_lib_ok() -> bool:
    try:
        import webauthn  # noqa: F401
        return True
    except Exception:
        return False


def request_rp(request, store=None):
    """(rp_id, origin) podle adresy, přes kterou uživatel Manager otevřel (X-Forwarded-Host jen od důvěryhodné proxy)."""
    xfh = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip() if (store is None or store._peer_is_trusted_proxy(request)) else ""
    host = xfh or (request.headers.get("host") or "").split(",")[0].strip()
    hostname = host
    if host.count(":") == 1:
        hostname = host.rsplit(":", 1)[0]
    return hostname.lower(), f"https://{host}"


def passkeys_usable(request, store=None) -> bool:
    return is_https(request, store) and passkeys_lib_ok()


def _pk_new_challenge(kind: str, challenge: bytes, rp: str, origin: str) -> str:
    cid = secrets.token_urlsafe(16)
    now = time.time()
    with _pk_lock:
        for k in [k for k, v in _pk_challenges.items() if v["exp"] < now]:
            _pk_challenges.pop(k, None)
        while len(_pk_challenges) >= _PK_MAX_CHALLENGES:
            oldest = min(_pk_challenges.items(), key=lambda kv: kv[1]["exp"])[0]
            _pk_challenges.pop(oldest, None)
        _pk_challenges[cid] = {"challenge": challenge, "kind": kind, "exp": now + 300, "rp": rp, "origin": origin}
    return cid


def _pk_take_challenge(cid: str, kind: str):
    with _pk_lock:
        c = _pk_challenges.pop(cid or "", None)
    if not c or c["kind"] != kind or c["exp"] < time.time():
        return None
    return c


def passkey_list(store, request) -> list:
    rp, _ = request_rp(request, store)
    out = []
    for p in store.data.get("passkeys") or []:
        out.append({"id": p.get("id", "")[:16], "name": p.get("name") or "Passkey", "created": p.get("created"),
                    "last_used": p.get("last_used"), "rp": p.get("rp") or "", "this_site": (p.get("rp") or "") == rp})
    return out


def passkey_count_for(store, request) -> int:
    rp, _ = request_rp(request, store)
    return sum(1 for p in store.data.get("passkeys") or [] if (p.get("rp") or "") == rp)


def passkey_register_options(store, request) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    rp, origin = request_rp(request, store)
    excl = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
            for p in store.data.get("passkeys") or [] if (p.get("rp") or "") == rp]
    opts = generate_registration_options(
        rp_id=rp, rp_name="HB Cluster Manager",
        user_id=_PK_USER_ID, user_name="admin", user_display_name="HB Manager",
        exclude_credentials=excl,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED),
    )
    cid = _pk_new_challenge("reg", opts.challenge, rp, origin)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def passkey_register_verify(store, request, cid: str, credential: dict, name: str) -> dict:
    from webauthn import verify_registration_response
    from webauthn.helpers import bytes_to_base64url
    c = _pk_take_challenge(cid, "reg")
    if not c:
        raise ValueError("challenge_expired")
    ver = verify_registration_response(credential=credential, expected_challenge=c["challenge"],
                                       expected_origin=c["origin"], expected_rp_id=c["rp"],
                                       require_user_verification=True)
    now = time.time()
    rec = {"id": bytes_to_base64url(ver.credential_id), "pk": bytes_to_base64url(ver.credential_public_key),
           "count": int(ver.sign_count or 0), "name": (name or "Passkey")[:60], "created": now, "last_used": 0,
           "rp": c["rp"], "ua": (request.headers.get("user-agent") or "")[:200]}
    with store.lock:
        lst = [p for p in store.data.get("passkeys") or [] if p.get("id") != rec["id"]]
        lst.append(rec)
        store.data["passkeys"] = lst[-20:]
        store.save()
    return {"ok": True}


def passkey_delete(store, short_id: str) -> bool:
    with store.lock:
        lst = store.data.get("passkeys") or []
        new = [p for p in lst if not (p.get("id") or "").startswith(short_id or "\x00")]
        if len(new) == len(lst):
            return False
        store.data["passkeys"] = new
        store.save()
        return True


def passkey_auth_options(store, request) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
    rp, origin = request_rp(request, store)
    allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
             for p in store.data.get("passkeys") or [] if (p.get("rp") or "") == rp]
    if not allow:
        raise ValueError("no_passkeys")
    opts = generate_authentication_options(rp_id=rp, allow_credentials=allow,
                                           user_verification=UserVerificationRequirement.REQUIRED)
    cid = _pk_new_challenge("auth", opts.challenge, rp, origin)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def passkey_auth_verify(store, request, cid: str, credential: dict) -> bool:
    from webauthn import verify_authentication_response
    from webauthn.helpers import base64url_to_bytes
    c = _pk_take_challenge(cid, "auth")
    if not c:
        return False
    cred_id = str((credential or {}).get("id") or "")
    with store.lock:
        rec = next((p for p in store.data.get("passkeys") or [] if p.get("id") == cred_id and p.get("rp") == c["rp"]), None)
    if not rec:
        return False
    try:
        ver = verify_authentication_response(credential=credential, expected_challenge=c["challenge"],
                                             expected_rp_id=c["rp"], expected_origin=c["origin"],
                                             credential_public_key=base64url_to_bytes(rec["pk"]),
                                             credential_current_sign_count=int(rec.get("count") or 0),
                                             require_user_verification=True)
    except Exception:
        return False
    with store.lock:
        rec["count"] = int(ver.new_sign_count or 0)
        rec["last_used"] = time.time()
        store.save()
    return True
