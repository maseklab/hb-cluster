# ==============================================================================
# HB CLUSTER WORKER - SHARED CORE ENGINE (worker_core.py)
# ==============================================================================

import os
import re
import ast
import time
import errno
import zlib
import json
import copy
import math
import base64
import socket
import struct
import shutil
import signal
import ctypes
from ctypes import wintypes
import hashlib
import threading
import subprocess
import urllib.parse
try:
    import requests
except Exception:
    requests = None
from collections import deque

def _load_app_version(fallback: str = "0.0.0") -> str:
    """Verze se čte jen ze souboru VERSION (Windows app/VERSION, Linux obraz). Bez něj 0.0.0 – Manager pak
    nabídne aktualizaci, která VERSION obnoví. (Literály verze kvůli workerům <= 2.11.4ZH zrušeny, O3.)"""
    this_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(this_dir, "VERSION"),
        os.path.join(os.path.dirname(this_dir), "VERSION"),
        os.path.join(os.getcwd(), "VERSION"),
        os.path.join(this_dir, "app", "VERSION"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                    if v:
                        return v
            except Exception:
                pass
    return fallback


CURRENT_VERSION = _load_app_version()
VERSION = CURRENT_VERSION
UDP_DISCOVERY_PORT = 8383

VALID_POWER_MODES = ("AUTO", "FULL", "ECO", "NIGHT", "HYBRID_ECO", "CUSTOM")
_INVISIBLE_WS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\u00a0]+")


def sanitize_path(val) -> str:
    """Odstraní neviditelné znaky, okrajové mezery a okolní uvozovky z cesty nebo složky."""
    s = _INVISIBLE_WS_RE.sub("", str(val or "")).replace("\r", "").replace("\n", "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def sanitize_manager_url(val) -> str:
    """Očistí URL Managera, odebere koncové lomítko a doplní http:// pokud chybí schéma."""
    s = sanitize_path(val)
    if not s or s.lower() in ("auto", "http://auto", "https://auto", "none", "null"):
        return ""
    if not re.match(r"^https?://", s, re.IGNORECASE):
        s = "http://" + s.lstrip("/")
    return s.rstrip("/")


def sanitize_display_name(val, default: str = "") -> str:
    s = _INVISIBLE_WS_RE.sub("", str(val or "")).replace("\r", "").replace("\n", "").strip()
    return s if s else default


_WORKER_NAME_EXTRA = {"ß": "ss", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "æ": "ae", "Æ": "AE",
                      "œ": "oe", "Œ": "OE", "þ": "th", "Þ": "Th"}


def normalize_worker_name(val) -> str:
    """Název workeru jen z A–Z, a–z, 0–9 a - _ . (max 48): diakritika -> bez ní (é->e, ř->r), mezery -> '-',
    ostatní znaky pryč. Stejné pravidlo jako normalizeWorkerName() v app.js. Prázdný výsledek = neplatný název."""
    import unicodedata
    s = unicodedata.normalize("NFKD", sanitize_display_name(val, ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = "".join(_WORKER_NAME_EXTRA.get(ch, ch) for ch in s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    return s[:48]


def slugify_worker_name(val, default: str = "worker") -> str:
    s = sanitize_display_name(val, default)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", s).strip("-")
    return slug or default


def clamp_num(val, min_v, max_v, default=0, as_int: bool = False):
    try:
        if val is None or isinstance(val, bool) or (isinstance(val, str) and not val.strip()):
            num = float(default)
        else:
            num = float(val)
        if not math.isfinite(num):
            num = float(default)
    except (TypeError, ValueError):
        num = float(default)
    num = max(float(min_v), min(float(max_v), num))
    return int(round(num)) if as_int else num


def safe_json_loads(raw, default=None):
    if raw is None:
        return default
    try:
        txt = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        txt = txt.lstrip("\ufeff").strip()
        if not txt:
            return default
        return json.loads(txt)
    except (ValueError, TypeError, OverflowError):
        return default


def format_sys_ram_value(ram_used_gb: float, ram_total_gb: float, ram_format: str = "gb_ratio") -> str:
    """Naformátuje systémovou RAM podle nastavení z Managera (gb_ratio, gb_pct, pct_gb, pct_only)."""
    ru = clamp_num(ram_used_gb, 0.0, 1e6, 0.0)
    rt = clamp_num(ram_total_gb, 0.0, 1e6, 0.0)
    pct = int(round((ru / rt) * 100.0)) if rt > 0 else 0
    fmt = ram_format if ram_format in ("gb_ratio", "gb_pct", "pct_gb", "pct_only") else "gb_ratio"
    ratio_s = f"{ru:.1f}/{rt:.1f} GB"
    if fmt == "gb_pct":
        return f"{ratio_s} ({pct}%)"
    if fmt == "pct_gb":
        return f"{pct}% ({ratio_s})"
    if fmt == "pct_only":
        return f"{pct}%"
    return ratio_s


def format_telemetry_labels(cpu_tot: float, cpu_hb: float, ram_used_gb: float, ram_total_gb: float, ram_hb_gb: float,
                            telemetry_source: str = "both", ram_format: str = "gb_ratio"):
    """Vrátí dvojici textů `(cpu_text, ram_text)` synchronizovanou s Managerem."""
    src = telemetry_source if telemetry_source in ("system", "hb", "both") else "both"
    sys_ram = format_sys_ram_value(ram_used_gb, ram_total_gb, ram_format)
    hb_ram = f"{clamp_num(ram_hb_gb, 0.0, 1e6, 0.0):.2f} GB"
    ct = clamp_num(cpu_tot, 0.0, 100.0, 0.0)
    ch = clamp_num(cpu_hb, 0.0, 100.0, 0.0)
    if src == "hb":
        return f"HB: {ch:.1f}%", f"HB: {hb_ram}"
    if src == "system":
        return f"Sys: {ct:.1f}%", f"Sys: {sys_ram}"
    return f"HB: {ch:.1f}% | Sys: {ct:.1f}%", f"HB: {hb_ram} | {sys_ram}"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000 if os.name == "nt" else 0
IDLE_PRIORITY_CLASS = 0x00000040 if os.name == "nt" else 0
HELD_META_FILENAME = "held_job_meta.json"
PREMUX_MPLS_META_FILENAME = "premux_mpls_streams.json"

WORKER_LANG = "cs"
WORKER_AVAILABLE_LANGS = [
    {"code": "cs", "label": "Čeština (CZ)"},
    {"code": "en", "label": "English (EN)"}
]
WORKER_LOCALES = {"en": {}, "cs": {}}


def resolve_locales_js_path() -> str:
    global LOCALES_JS_PATH
    this_d = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(this_d, "locales.js"),
        os.path.join(this_d, "app", "locales.js"),
        os.path.join(os.path.dirname(this_d), "locales.js"),
        os.path.join(os.getcwd(), "locales.js"),
        os.path.join(os.getcwd(), "app", "locales.js"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            LOCALES_JS_PATH = p
            return p
    return os.path.join(this_d, "locales.js")


LOCALES_JS_PATH = resolve_locales_js_path()
_worker_locales_mtime = 0.0
_worker_locales_checked_at = 0.0
_worker_locales_lock = threading.RLock()
local_md5_cache = {}
_pending_audio_titles = {}
_pending_audio_titles_lock = threading.Lock()
_x265_ver_cache = {}


def clear_pending_audio_titles(working_dest: str):
    if not working_dest:
        return
    with _pending_audio_titles_lock:
        _pending_audio_titles.pop(os.path.abspath(working_dest), None)


def rekey_pending_audio_titles(old_dest: str, new_dest: str):
    if not old_dest or not new_dest:
        return
    with _pending_audio_titles_lock:
        val = _pending_audio_titles.pop(os.path.abspath(old_dest), None)
        if val is not None:
            _pending_audio_titles[os.path.abspath(new_dest)] = val


def extract_x265_version(hb_bin: str) -> str:
    """Zjistí verzi vestavěného x265 přímo z binárky HandBrakeCLI (s keší podle velikosti a času změny)."""
    if not hb_bin or not os.path.isfile(hb_bin):
        return ""
    try:
        st = os.stat(hb_bin)
        ck = (os.path.abspath(hb_bin), st.st_mtime_ns, st.st_size)
        if ck in _x265_ver_cache:
            return _x265_ver_cache[ck]
        with open(hb_bin, "rb") as f:
            raw = f.read()
        m = re.search(rb"HEVC encoder version (\d+\.\d+(?:\+\d+)?)", raw)
        ver = m.group(1).decode("ascii", errors="ignore").split("+")[0] if m else ""
        if len(_x265_ver_cache) > 16:
            _x265_ver_cache.clear()
        _x265_ver_cache[ck] = ver
        return ver
    except Exception:
        return ""

# ------------------------------------------------------------------------------
# SDÍLENÁ HTTP SESSION (Worker <-> Manager)
# ------------------------------------------------------------------------------
# Jedna requests.Session s omezeným poolem a VÝCHOZÍM timeoutem pro všechna volání
# Manageru: žádné zamrznutí vlákna při výpadku sítě a žádné vyčerpání socketů.
# Hlavička "Connection: close" zůstává (Manager ji vynucuje) – po restartu Managera
# tak nikdy nezůstanou viset mrtvá keep-alive spojení.
DEFAULT_HTTP_TIMEOUT = (6, 30)
_http_session = None
_http_session_lock = threading.Lock()
_auth_hdr_lock = threading.Lock()


def get_http_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        with _http_session_lock:
            if _http_session is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                s.headers.update({"User-Agent": f"HB-Cluster-Worker/{CURRENT_VERSION}"})
                _http_session = s
    return _http_session


# ------------------------------------------------------------------------------
# IDENTITA WORKERU (Zabezpečení, krok 2): vlastní náhodný klíč workeru.
# Worker si ho vytvoří sám při prvním startu a uloží do své složky (Linux /data, Windows app/).
# Posílá ho s každým požadavkem na Manager (hlavička X-HB-Worker-Key); Manager si drží jen jeho otisk.
# Nic se ručně nekopíruje. Když Manager schvalování nepoužívá, hlavičku prostě ignoruje.
# ------------------------------------------------------------------------------
WORKER_IDENTITY_FILE = "worker_identity.json"
approval_state = ""          # "" | worker_pending | worker_rejected | worker_ignored | worker_no_key
approval_log_fn = None       # worker nastaví svou funkci pro log (jednou při změně stavu)


def _note_approval_state(resp):
    """Manager se zapnutým schvalováním vrací 403 {"detail": "worker_pending"} apod. Zapsat do logu jen při změně."""
    global approval_state
    try:
        new_st = ""
        if resp is not None and resp.status_code == 403:
            det = str((resp.json() or {}).get("detail") or "")
            if det.startswith("worker_"):
                new_st = det
            else:
                return
        elif resp is None or resp.status_code != 200:
            return
        elif not any(p in str(getattr(resp, "url", "") or "") for p in ("/api/workers/ping", "/api/jobs/next")):
            return      # 200 z volné cesty (aktualizace, texty) o schválení nic neříká
        if new_st == approval_state:
            return
        prev, approval_state = approval_state, new_st
        fn = approval_log_fn
        if fn:
            if new_st:
                fn(T("w_approval_" + new_st[len("worker_"):]))
            elif prev:
                fn(T("w_approval_ok"))
    except Exception:
        pass
_worker_identity = {"key": "", "name": "", "join": ""}

# ------------------------------------------------------------------------------
# NAVÁZÁNÍ NA MANAGER: Manager posílá v každé odpovědi své náhodné ID (hlavička X-HB-Manager-Id).
# Worker si ho při prvním spojení uloží (manager_binding.json vedle worker_identity.json) a od Manageru
# s jiným ID nic nepřijme – ochrana proti falešnému Manageru (např. podvržená odpověď na „Najít“)
# i proti omylem zadané adrese jiného Manageru. Znovu navázat: Windows – Možnosti → „Navázat na tento
# Manager“; Linux – smazat /data/manager_binding.json nebo spustit s HB_MANAGER_REBIND=1.
# ------------------------------------------------------------------------------
MANAGER_BINDING_FILE = "manager_binding.json"
_MGR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_mgr_bind = {"path": "", "id": "", "foreign": "", "seen": ""}
_mgr_bind_lock = threading.Lock()
manager_bind_log_fn = None    # worker nastaví svou funkci pro log (zapisuje se jen při změně stavu)


class ManagerMismatchError(RuntimeError):
    """Odpověď nepřišla od Manageru, na který je worker navázaný → nic z ní nepřijmout."""


def worker_auth_headers() -> dict:
    with _auth_hdr_lock:
        h = {}
        if _mgr_bind.get("id"):
            h["X-HB-Bound-Manager"] = _mgr_bind["id"]          # Manager pozná worker navázaný na jiný Manager
        if _worker_identity.get("key"):
            h["X-HB-Worker-Key"] = _worker_identity["key"]
        if _worker_identity.get("name"):
            h["X-HB-Worker-Name"] = urllib.parse.quote(str(_worker_identity["name"]), safe="")
        if _worker_identity.get("join"):
            h["X-HB-Join-Token"] = _worker_identity["join"]   # předschválení z průvodce (platí 1 h)
        return h


def set_worker_identity(name: str = None, key: str = None, join: str = None):
    with _auth_hdr_lock:
        if join is not None:
            _worker_identity["join"] = str(join or "")[:100]
        if name is not None:
            _worker_identity["name"] = str(name or "")
        if key is not None:
            _worker_identity["key"] = str(key or "")


def _refresh_session_auth_headers():
    # Hlavičky se dnes skládají per-request v http_request() ze zamčeného snímku (bez souběžné mutace s.headers).
    return


def init_worker_identity(store_dir: str, name: str = "", join: str = "") -> str:
    """Načte (nebo poprvé vytvoří) klíč workeru. Při chybě zápisu klíč platí aspoň do restartu."""
    import secrets as _secrets
    path = os.path.join(store_dir or ".", WORKER_IDENTITY_FILE)
    key = ""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            k = str((d or {}).get("key") or "")
            if len(k) >= 32:
                key = k
    except Exception:
        key = ""
    if not key:
        key = _secrets.token_urlsafe(32)
        try:
            os.makedirs(store_dir or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"key": key, "created": int(time.time())}, f)
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, path)
        except Exception:
            pass
    set_worker_identity(name=name, key=key, join=join)
    return key


def init_manager_binding(store_dir: str, rebind: bool = False) -> str:
    """Načte uložené ID Manageru. rebind=True (HB_MANAGER_REBIND=1) vazbu zapomene – další odpověď se zapamatuje."""
    path = os.path.join(store_dir or ".", MANAGER_BINDING_FILE)
    with _mgr_bind_lock:
        _mgr_bind.update({"path": path, "id": "", "foreign": ""})
        if rebind:
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    mid = str((json.load(f) or {}).get("manager_id") or "")
                if _MGR_ID_RE.match(mid):
                    _mgr_bind["id"] = mid
            except Exception:
                pass
    _refresh_session_auth_headers()
    return _mgr_bind["id"]


def bound_manager_id() -> str:
    return _mgr_bind.get("id") or ""


def foreign_manager_id() -> str:
    """ID Manageru, který odpovídá, ale není ten navázaný ("" = vše v pořádku)."""
    return _mgr_bind.get("foreign") or ""


def last_seen_manager_id() -> str:
    return _mgr_bind.get("seen") or ""


def _bind_log(msg: str):
    fn = manager_bind_log_fn
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def rebind_to_manager(manager_id: str = "") -> bool:
    """„Navázat na tento Manager“: uloží zadané ID (nebo vazbu zapomene, když je prázdné – pak se
    zapamatuje ID z příští odpovědi). Volá jen uživatel ve workeru, nikdy Manager."""
    mid = str(manager_id or "").strip()
    if mid and not _MGR_ID_RE.match(mid):
        return False
    with _mgr_bind_lock:
        path = _mgr_bind.get("path") or ""
        try:
            if mid:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"manager_id": mid, "bound_at": int(time.time())}, f)
                os.replace(tmp, path)
            elif path:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        except Exception:
            return False
        _mgr_bind["id"] = mid
        _mgr_bind["foreign"] = ""
    _refresh_session_auth_headers()
    _bind_log(T('w_mgr_bind_rebound', mid) if mid else T('w_mgr_bind_forgot'))
    return True


def _is_gateway_html_response(resp) -> bool:
    """True = odpověď bez X-HB-Manager-Id je HTML stránka přihlašovací brány (Cloudflare Access/Zero Trust, captive portál, proxy)."""
    try:
        ctype = str(resp.headers.get("Content-Type") or "").lower()
        final_url = str(getattr(resp, "url", "") or "").lower()
    except Exception:
        return False
    if "text/html" in ctype or "application/xhtml" in ctype:
        return True
    if "cloudflareaccess.com" in final_url or "/cdn-cgi/access/" in final_url:
        return True
    return False


# --- Windows: one installation per PC -----------------------------------------------------------------------------
# Autostart (Run key, scheduled task) and shortcuts have fixed names, so a second copy of the worker in another folder
# takes them over. Uninstalling the OLD copy must then leave them alone: it removes only what points into its own
# folder, or what points to a worker that no longer exists. Pure path logic (ntpath) – testable on Linux too.
WIN_WORKER_TARGETS = ("hbworker.exe", "worker.pyw", "start_worker.bat")
_WIN_CMD_PATH_RE = re.compile(r'"([^"]+)"|(\S+)')


def win_install_root(app_dir: str) -> str:
    """Folder of the installation (with HBWorker.exe) from the folder of worker.pyw (…\\app)."""
    import ntpath
    d = ntpath.normpath(str(app_dir or "")).rstrip("\\")
    return ntpath.dirname(d) if ntpath.basename(d).lower() == "app" else d


def win_cmd_worker_target(cmd: str) -> str:
    """Path of HBWorker.exe / worker.pyw / Start_Worker.bat in an autostart command or shortcut target ('' = none)."""
    import ntpath
    for m in _WIN_CMD_PATH_RE.finditer(str(cmd or "")):
        p = (m.group(1) or m.group(2) or "").strip()
        if p and ntpath.basename(p).lower() in WIN_WORKER_TARGETS:
            return p
    return ""


def win_path_inside(path: str, root: str) -> bool:
    import ntpath
    if not path or not root:
        return False
    p = ntpath.normcase(ntpath.normpath(str(path)))
    r = ntpath.normcase(ntpath.normpath(str(root))).rstrip("\\")
    return p == r or p.startswith(r + "\\")


def win_same_dir(a: str, b: str) -> bool:
    import ntpath
    if not a or not b:
        return False
    return ntpath.normcase(ntpath.normpath(str(a))).rstrip("\\") == ntpath.normcase(ntpath.normpath(str(b))).rstrip("\\")


def win_path_inside_garbled(path: str, root: str) -> bool:
    """win_path_inside for a path read through the console code page: '?' / U+FFFD stand for a character
    the code page could not show (e.g. "Ma?ek" for "Mašek") and match any single character of `root`."""
    import ntpath
    if not path or not root:
        return False
    p = ntpath.normcase(ntpath.normpath(str(path)))
    r = ntpath.normcase(ntpath.normpath(str(root))).rstrip("\\")
    if len(p) < len(r) or (len(p) > len(r) and p[len(r)] != "\\"):
        return False
    return all(pc == rc or pc in "?\ufffd" for pc, rc in zip(p[:len(r)], r))


def win_ref_is_ours_or_stale(cmd: str, root: str, exists_fn=os.path.exists) -> bool:
    """May the uninstaller of the worker in `root` remove this autostart entry / shortcut?
    Yes when it points into `root`, or to a worker that no longer exists, or when no worker path can be read from it
    (behaviour before 2.12.0). No when it points to another, still existing installation."""
    t = win_cmd_worker_target(cmd)
    if not t:
        return True
    if win_path_inside(t, root):
        return True
    if "?" in t or "\ufffd" in t:
        # garbled path (read through the console code page): ours only when it matches this folder – a path that
        # cannot be checked must not make another installation's autostart look deleted
        return win_path_inside_garbled(t, root)
    try:
        return not exists_fn(t)
    except Exception:
        return False


def decode_console_output(raw: bytes) -> str:
    """Text printed by a Windows console tool (schtasks, …) into a pipe: UTF-16, UTF-8, or the console / OEM /
    ANSI code page – non-ASCII folder names (e.g. a user name with diacritics) must not come out garbled."""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw.count(b"\x00") > len(raw) // 4:
        try:
            return raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-le")
        except Exception:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    cps = []
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            cps = [int(k32.GetConsoleOutputCP() or 0), int(k32.GetOEMCP() or 0), int(k32.GetACP() or 0)]
        except Exception:
            cps = []
    for cp in [c for c in cps if c and c != 65001]:
        try:
            return raw.decode(f"cp{cp}")
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def win_root_from_worker_target(target: str) -> str:
    """Installation folder from the path of HBWorker.exe / worker.pyw / Start_Worker.bat."""
    import ntpath
    return win_install_root(ntpath.dirname(ntpath.normpath(str(target or ""))))


def is_own_manager_refusal(resp, codes=(404, 409, 410)) -> bool:
    """True = odpověď s kódem z `codes` opravdu poslal náš (navázaný) Manager – ne proxy, Cloudflare ani výpadek.
    Jen podle takové odpovědi smí worker něco definitivně vzdát (např. přestat hlásit dokončení úlohy)."""
    try:
        if int(getattr(resp, "status_code", 0) or 0) not in codes:
            return False
        mid = str(resp.headers.get("X-HB-Manager-Id") or "").strip()
    except Exception:
        return False
    if not mid or not _MGR_ID_RE.match(mid):
        return False
    bound = bound_manager_id()
    return (not bound) or mid == bound


def check_manager_identity(resp) -> bool:
    """True = odpověď je od navázaného Manageru (nebo se worker právě navázal). Bez vazby a od staršího
    Manageru bez ID se nic nevynucuje. Chybová odpověď bez ID (např. 502 z proxy nebo HTML přihlašovací
    stránka Cloudflare Access bez VPN/WARP) projde jako nedostupný Manager, ne jako jiný Manager."""
    if resp is None:
        return True
    try:
        mid = str(resp.headers.get("X-HB-Manager-Id") or "").strip()
        code = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        return True
    if mid and not _MGR_ID_RE.match(mid):
        mid = ""
    if not mid and _is_gateway_html_response(resp):
        return True
    new_bind = False
    msg = ""
    with _mgr_bind_lock:
        if mid:
            _mgr_bind["seen"] = mid
        bound = _mgr_bind["id"]
        if not bound:
            if not mid:
                return True
            _mgr_bind["id"] = mid
            new_bind = True
            path = _mgr_bind.get("path") or ""
            if path:
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump({"manager_id": mid, "bound_at": int(time.time())}, f)
                    os.replace(tmp, path)
                except Exception:
                    pass
        elif mid == bound:
            if _mgr_bind["foreign"]:
                _mgr_bind["foreign"] = ""
                msg = T('w_mgr_bind_ok_again', bound)
        elif mid or 200 <= code < 300:
            f_id = mid or "-"
            if _mgr_bind["foreign"] != f_id:
                _mgr_bind["foreign"] = f_id
                msg = T('w_mgr_bind_mismatch', f_id, bound) if mid else T('w_mgr_bind_mismatch_noid', bound)
        else:
            return True                       # chybová odpověď bez ID (proxy, výpadek) – volající ji vyhodnotí jako chybu
    if new_bind:
        _refresh_session_auth_headers()
        _bind_log(T('w_mgr_bind_new', mid))
        return True
    if msg:
        _bind_log(msg)
    return not (bound and mid != bound)


def http_request(method: str, url: str, retries: int = 2, **kwargs):
    extra_hdrs = dict(kwargs.pop("headers", None) or {})
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = DEFAULT_HTTP_TIMEOUT
    last_exc = None
    for attempt in range(max(1, int(retries))):
        hdrs = worker_auth_headers()
        hdrs.update(extra_hdrs)
        hdrs.setdefault("Connection", "close")
        try:
            resp = get_http_session().request(method, url, headers=hdrs, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(0.5)
            continue
        if not check_manager_identity(resp):
            # cizí Manager: odpověď zahodit a neopakovat (volající to bere jako nedostupný Manager)
            raise ManagerMismatchError(T('w_mgr_bind_refused', foreign_manager_id() or "-"))
        if not str(resp.headers.get("X-HB-Manager-Id") or "").strip() and "/api/" in str(url or "") and _is_gateway_html_response(resp):
            # Cloudflare Access / Zero Trust login nebo captive portál místo API Manageru → brát jako nedostupný Manager
            raise requests.exceptions.ConnectionError("Gateway/login HTML response instead of HB Manager API")
        _note_approval_state(resp)
        return resp
    raise last_exc

HB_AUTO_AUDIO_NAMES = {
    "stereo", "surround", "mono", "dolby surround", "dolby pro logic",
    "dolby pro logic ii", "quadraphonic", "upmix", "downmix",
    "2.0 channels", "3.0 channels", "4.0 channels", "5.1 channels",
    "6.1 channels", "7.1 channels", "5.1", "7.1", "2.0"
}

ISO_LANG_GROUPS = {
    "ces": {"ces", "cze", "cs", "czech", "čeština", "cestina"},
    "slk": {"slk", "slo", "sk", "slovak", "slovenština"},
    "eng": {"eng", "en", "english", "angličtina"},
    "deu": {"deu", "ger", "de", "german", "deutsch", "němčina"},
    "fra": {"fra", "fre", "fr", "french", "francouzština", "français", "francais"},
    "ita": {"ita", "it", "italian", "italiano", "italština"},
    "spa": {"spa", "es", "spanish", "español", "espanol", "španělština"},
    "rus": {"rus", "ru", "russian", "ruština"},
    "jpn": {"jpn", "ja", "japanese", "japonština"},
    "pol": {"pol", "pl", "polish", "polština"},
    "hun": {"hun", "hu", "hungarian", "maďarština"},
    "nld": {"nld", "dut", "nl", "dutch", "ned", "nizozemština", "holandština"},
    "nor": {"nor", "nob", "nno", "no", "norwegian"},
    "swe": {"swe", "sv", "swedish"},
    "dan": {"dan", "da", "danish"},
    "fin": {"fin", "fi", "finnish"},
    "por": {"por", "pt", "portuguese"},
    "zho": {"zho", "chi", "zh", "chinese"},
    "kor": {"ko", "kor", "korean"},
    "ron": {"ron", "rum", "ro", "romanian"},
    "ell": {"ell", "gre", "el", "greek"},
}


def set_worker_lang(lang_code: str) -> str:
    global WORKER_LANG
    cl = str(lang_code or "").lower().strip()
    if cl in ("cs", "cz"):
        WORKER_LANG = "cs"
    elif cl == "en":
        WORKER_LANG = "en"
    return WORKER_LANG


def get_worker_lang() -> str:
    return WORKER_LANG


def default_ui_lang() -> str:
    """Language of a worker whose config.json has none yet (e.g. the package from GitHub Releases):
    Czech or Slovak Windows → cs, anything else → en. The worker then follows the language of its Manager."""
    try:
        if os.name == "nt":
            lid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            return "cs" if (lid & 0x3FF) in (0x05, 0x1B) else "en"
    except Exception:
        pass
    try:
        import locale
        loc = str(locale.getlocale()[0] or os.environ.get("LANG") or "").lower()
        return "cs" if loc.startswith(("cs", "sk", "czech", "slovak")) else "en"
    except Exception:
        return "en"


def L(cs_txt: str, en_txt: str) -> str:
    return cs_txt if WORKER_LANG == "cs" else en_txt


def parse_locales_js_text(txt: str) -> dict:
    if not txt:
        return {}
    try:
        idx = txt.find("window.HB_LOCALES")
        if idx != -1:
            b_start = txt.find("{", idx)
            b_end = txt.rfind("}")
            if b_start != -1 and b_end != -1:
                raw_block = re.sub(r'(?m)^\s*//.*$', '', txt[b_start:b_end + 1])
                py_obj = re.sub(r'(?m)^(\s*)([a-zA-Z0-9_]+)\s*:', r'\1"\2":', raw_block)
                parsed = ast.literal_eval(py_obj)
                if isinstance(parsed, dict) and ("en" in parsed or "cs" in parsed):
                    return parsed
    except Exception:
        pass
    return {}


def load_local_locales_file(force: bool = False) -> dict:
    global _worker_locales_mtime, _worker_locales_checked_at, LOCALES_JS_PATH
    now_t = time.time()
    if not force and WORKER_LOCALES.get("en") and WORKER_LOCALES.get("cs") and (now_t - _worker_locales_checked_at) < 30.0:
        return WORKER_LOCALES
    with _worker_locales_lock:
        if not force and WORKER_LOCALES.get("en") and WORKER_LOCALES.get("cs") and (now_t - _worker_locales_checked_at) < 30.0:
            return WORKER_LOCALES
        _worker_locales_checked_at = now_t
        loc_path = LOCALES_JS_PATH if (LOCALES_JS_PATH and os.path.isfile(LOCALES_JS_PATH)) else resolve_locales_js_path()
        if not os.path.isfile(loc_path):
            return WORKER_LOCALES
        try:
            mt = os.path.getmtime(loc_path)
            if not force and mt == _worker_locales_mtime and WORKER_LOCALES.get("en") and WORKER_LOCALES.get("cs"):
                return WORKER_LOCALES
            with open(loc_path, "r", encoding="utf-8-sig") as f:
                txt = f.read()
            parsed = parse_locales_js_text(txt)
            if parsed:
                WORKER_LOCALES.update(parsed)
                _worker_locales_mtime = mt
            idx_l = txt.find("window.HB_AVAILABLE_LANGS")
            if idx_l != -1:
                l_start = txt.find("[", idx_l)
                l_end = txt.find("]", l_start)
                if l_start != -1 and l_end != -1:
                    py_arr = re.sub(r'([a-zA-Z0-9_]+)\s*:', r'"\1":', txt[l_start:l_end + 1])
                    arr = ast.literal_eval(py_arr)
                    if isinstance(arr, list) and arr:
                        WORKER_AVAILABLE_LANGS[:] = arr
        except Exception:
            pass
        return WORKER_LOCALES


load_local_locales_file()


def get_available_langs() -> list:
    """Vrátí dynamicky načtený seznam dostupných jazyků z locales.js."""
    load_local_locales_file()
    if WORKER_AVAILABLE_LANGS:
        return list(WORKER_AVAILABLE_LANGS)
    return [
        {"code": "cs", "label": "Čeština (CZ)"},
        {"code": "en", "label": "English (EN)"}
    ]


STORAGE_PROBE_PREFIX = ".hb_storage_test_"
STORAGE_PROBE_SWEEP_EVERY_S = 6 * 3600
STORAGE_PROBE_MAX_AGE_S = 3600
_storage_probe_last_sweep = {}


def storage_probe_name() -> str:
    """Name of the write test file in Outputs – the same on every check of this computer / container."""
    host = re.sub(r"[^A-Za-z0-9_-]+", "-", socket.gethostname() or "worker").strip("-")[:40] or "worker"
    return f"{STORAGE_PROBE_PREFIX}{host}.tmp"


def remove_file_with_retry(path: str, attempts: int = 3, delay_s: float = 0.3) -> bool:
    for i in range(max(1, attempts)):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            if i + 1 < attempts:
                time.sleep(delay_s * (i + 1))
    return False


def sweep_old_storage_probes(out_d: str, now: float | None = None) -> int:
    """Removes forgotten write test files (also those of older versions: .hb_storage_test_<pid>_<time>.tmp) older
    than an hour. At most once every 6 hours per folder – Outputs on a NAS can hold many files."""
    now = time.time() if now is None else now
    if now - _storage_probe_last_sweep.get(out_d, 0.0) < STORAGE_PROBE_SWEEP_EVERY_S:
        return 0
    _storage_probe_last_sweep[out_d] = now
    removed = 0
    try:
        with os.scandir(out_d) as it:
            for e in it:
                if not (e.name.startswith(STORAGE_PROBE_PREFIX) and e.name.endswith(".tmp")):
                    continue
                try:
                    if e.is_file(follow_symlinks=False) and now - e.stat(follow_symlinks=False).st_mtime > STORAGE_PROBE_MAX_AGE_S:
                        if remove_file_with_retry(e.path, attempts=1):
                            removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def check_worker_storage_accessible(inputs_dir: str, outputs_dir: str) -> dict:
    """
    Ověří lokální / SMB dostupnost složek Inputs a Outputs na straně workera.
    Vrací dict: {'inputs_ok': bool, 'outputs_ok': bool, 'all_ok': bool, 'reason': str}
    """
    res = {
        "inputs_ok": False,
        "outputs_ok": False,
        "all_ok": False,
        "reason": ""
    }
    in_d = sanitize_path(inputs_dir)
    out_d = sanitize_path(outputs_dir)

    # 1. Kontrola Inputs
    if not in_d:
        res["reason"] = "Inputs dir not configured"
    else:
        try:
            if os.path.isdir(in_d):
                res["inputs_ok"] = True
            else:
                res["reason"] = f"Inputs path is not an accessible directory: {in_d}"
        except Exception as e:
            res["reason"] = f"Inputs path error: {e}"

    # 2. Kontrola Outputs (čtení i zápis)
    if not out_d:
        res["reason"] = (res["reason"] + "; " if res["reason"] else "") + "Outputs dir not configured"
    else:
        try:
            if os.path.isdir(out_d):
                # Fixed name per computer: if deleting the probe ever fails (file briefly held by an antivirus or
                # the NAS indexer, connection lost), the next check overwrites the same file instead of adding one.
                probe_p = os.path.join(out_d, storage_probe_name())
                try:
                    with open(probe_p, "w", encoding="utf-8") as f:
                        f.write("HB_STORAGE_OK")
                    if os.path.isfile(probe_p):
                        remove_file_with_retry(probe_p)
                        res["outputs_ok"] = True
                    else:
                        res["reason"] = (res["reason"] + "; " if res["reason"] else "") + f"Outputs probe file was not created: {out_d}"
                except Exception as we:
                    res["reason"] = (res["reason"] + "; " if res["reason"] else "") + f"Outputs write error: {we}"
                    remove_file_with_retry(probe_p, attempts=1)
                sweep_old_storage_probes(out_d)
            else:
                res["reason"] = (res["reason"] + "; " if res["reason"] else "") + f"Outputs path is not an accessible directory: {out_d}"
        except Exception as e:
            res["reason"] = (res["reason"] + "; " if res["reason"] else "") + f"Outputs path error: {e}"

    res["all_ok"] = bool(res["inputs_ok"] and res["outputs_ok"])
    return res


def sync_locales_from_manager(manager_url: str):
    load_local_locales_file()
    if not manager_url:
        return
    base_u = manager_url.rstrip("/")
    try:
        need_dl = not os.path.isfile(LOCALES_JS_PATH) or not WORKER_LOCALES.get("en")
        if not need_dl:
            with open(LOCALES_JS_PATH, "r", encoding="utf-8-sig") as f:
                hdr = f.read(256)
            if f"(locales.js v{CURRENT_VERSION})" not in hdr:
                need_dl = True
        if need_dl:
            rl = http_request("GET", f"{base_u}/locales.js", timeout=8, retries=1)
            if rl.status_code == 200 and len(rl.content) > 500:
                l_txt = rl.content.decode("utf-8-sig").lstrip("\ufeff").replace("\r\n", "\n")
                parsed = parse_locales_js_text(l_txt)
                if parsed:
                    tmp_l = LOCALES_JS_PATH + ".tmp"
                    with open(tmp_l, "w", encoding="utf-8", newline="\n") as wf:
                        wf.write(l_txt)
                    os.replace(tmp_l, LOCALES_JS_PATH)
                    load_local_locales_file(force=True)
                    return
        r = http_request("GET", f"{base_u}/api/locales", timeout=5, retries=1)
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, dict) and ("en" in d or "cs" in d):
                WORKER_LOCALES.update(d)
    except Exception:
        pass


def T(key: str, *args, **kwargs) -> str:
    load_local_locales_file()
    d = WORKER_LOCALES.get(WORKER_LANG) or {}
    en_d = WORKER_LOCALES.get("en") or {}
    tmpl = d.get(key) or en_d.get(key) or kwargs.get("default") or key
    if args or kwargs:
        try:
            return str(tmpl).format(*args, **kwargs)
        except Exception:
            return str(tmpl)
    return str(tmpl)


def SubprocFlags(low_prio: bool = False) -> int:
    if os.name != "nt":
        return 0
    return CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_prio else 0)


def _norm_lang_group(lang_code: str, text_hint: str = "") -> str:
    clean_c = re.sub(r"[^a-z]", "", str(lang_code or "").lower())
    for canon, grp in ISO_LANG_GROUPS.items():
        if clean_c in grp:
            return canon
    combined = f"{lang_code or ''} {text_hint or ''}".lower()
    for canon, grp in ISO_LANG_GROUPS.items():
        for w in grp:
            if len(w) > 2 and re.search(rf"\b{re.escape(w)}\b", combined):
                return canon
    return clean_c if (clean_c in ISO_LANG_GROUPS) else "und"


def _norm_codec_family(codec_s: str, profile_s: str = "") -> str:
    s = f"{codec_s or ''} {profile_s or ''}".lower()
    if "truehd" in s or "mlp" in s:
        return "truehd"
    if "dts-hd ma" in s or "dts-hd master" in s or "dtshd" in s or "dts-x" in s or "dts:x" in s or "dts-hd hra" in s:
        return "dtshd"
    if "dts" in s:
        return "dts"
    if "eac3" in s or "e-ac-3" in s or "dd+" in s or "ec-3" in s:
        return "eac3"
    if "ac3" in s or "ac-3" in s or "dolby digital" in s:
        return "ac3"
    if "pcm" in s or "lpcm" in s:
        return "pcm"
    if "flac" in s:
        return "flac"
    if "aac" in s:
        return "aac"
    if "mp3" in s:
        return "mp3"
    if "opus" in s:
        return "opus"
    if "pgs" in s or "hdmv_pgs" in s:
        return "pgs"
    if "vobsub" in s or "dvd_subtitle" in s:
        return "vobsub"
    if "ass" in s or "ssa" in s:
        return "ass"
    if "subrip" in s or "srt" in s or "text" in s:
        return "srt"
    return s.strip()


def clean_hb_auto_audio_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    s = str(raw_name).strip()
    low = s.lower()
    if low in HB_AUTO_AUDIO_NAMES:
        return ""
    if re.match(r"^\d+\.\d+(\s*channels?|\s*ch)?$", low):
        return ""
    return s


# ==============================================================================
# 1. BINÁRNÍ PARSER BLU-RAY .MPLS (STN_table) A VÝBĚR PLAYLISTU
# ==============================================================================
def parse_mpls_stn_table_streams(mpls_bytes: bytes) -> dict:
    res = {"audio": [], "pg": [], "pid_lang": {}}
    if not mpls_bytes or len(mpls_bytes) < 64 or b"MPLS" not in mpls_bytes[:8]:
        return res
    try:
        pl_offset = struct.unpack_from(">I", mpls_bytes, 8)[0]
        if pl_offset + 10 > len(mpls_bytes):
            return res
        num_items = struct.unpack_from(">H", mpls_bytes, pl_offset + 6)[0]
        if num_items <= 0:
            return res

        pos = pl_offset + 10
        item_len = struct.unpack_from(">H", mpls_bytes, pos)[0]
        item_end = pos + 2 + item_len
        if item_len < 34 or item_end > len(mpls_bytes):
            return res

        flags = struct.unpack_from(">H", mpls_bytes, pos + 11)[0]
        is_multi_angle = bool((flags >> 4) & 1)
        cur = pos + 34
        if is_multi_angle and cur + 2 <= item_end:
            num_angles = mpls_bytes[cur]
            cur += 2 + max(0, num_angles - 1) * 10

        if cur + 16 > item_end:
            return res
        stn_len = struct.unpack_from(">H", mpls_bytes, cur)[0]
        stn_end = min(item_end, cur + 2 + stn_len)
        num_v = mpls_bytes[cur + 4]
        num_a = mpls_bytes[cur + 5]
        num_pg = mpls_bytes[cur + 6]
        num_ig = mpls_bytes[cur + 7]
        num_sec_a = mpls_bytes[cur + 8]
        num_sec_v = mpls_bytes[cur + 9]
        num_pip = mpls_bytes[cur + 10]

        p = cur + 16

        def _read_one_stn_entry(ptr):
            if ptr >= stn_end:
                return ptr, None
            se_len = mpls_bytes[ptr]
            se_start = ptr + 1
            se_next = se_start + se_len
            if se_next >= stn_end:
                return stn_end, None
            s_type = mpls_bytes[se_start] if se_len >= 1 else 0
            pid = 0
            if s_type == 1 and se_len >= 3:
                pid = struct.unpack_from(">H", mpls_bytes, se_start + 1)[0]
            elif s_type in (2, 4) and se_len >= 5:
                pid = struct.unpack_from(">H", mpls_bytes, se_start + 3)[0]
            elif s_type == 3 and se_len >= 4:
                pid = struct.unpack_from(">H", mpls_bytes, se_start + 2)[0]

            sa_len = mpls_bytes[se_next]
            sa_start = se_next + 1
            sa_next = sa_start + sa_len
            if sa_next > stn_end:
                return stn_end, None

            coding = mpls_bytes[sa_start] if sa_len >= 1 else 0
            lang_raw = ""
            ch_hint = 0
            if coding in (0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2) and sa_len >= 5:
                fmt_byte = mpls_bytes[sa_start + 1]
                ch_nibble = (fmt_byte >> 4) & 0x0F
                ch_hint = 2 if ch_nibble in (1, 3) else (6 if ch_nibble == 6 else (8 if ch_nibble == 12 else 0))
                lang_raw = mpls_bytes[sa_start + 2:sa_start + 5].decode("ascii", errors="ignore")
            elif coding in (0x90, 0x91) and sa_len >= 4:
                lang_raw = mpls_bytes[sa_start + 1:sa_start + 4].decode("ascii", errors="ignore")
            elif coding == 0x92 and sa_len >= 5:
                lang_raw = mpls_bytes[sa_start + 2:sa_start + 5].decode("ascii", errors="ignore")

            codec_fam = {
                0x80: "pcm", 0x81: "ac3", 0x82: "dts", 0x83: "truehd",
                0x84: "eac3", 0x85: "dtshd", 0x86: "dtshd", 0xA1: "eac3", 0xA2: "dtshd",
                0x90: "pgs", 0x92: "srt"
            }.get(coding, "")
            norm_l = _norm_lang_group(lang_raw)
            info = {
                "pid": pid,
                "coding": coding,
                "family": codec_fam,
                "channels": ch_hint,
                "raw_lang": lang_raw.lower() if lang_raw else "und",
                "lang_grp": norm_l,
            }
            if pid > 0 and norm_l != "und":
                res["pid_lang"][pid] = norm_l
            return sa_next, info

        for _ in range(num_v):
            p, _ = _read_one_stn_entry(p)
        for _ in range(num_a):
            p, inf = _read_one_stn_entry(p)
            if inf:
                res["audio"].append(inf)
        for _ in range(num_pg):
            p, inf = _read_one_stn_entry(p)
            if inf:
                res["pg"].append(inf)
        for _ in range(num_ig + num_sec_a + num_sec_v + num_pip):
            p, _ = _read_one_stn_entry(p)
    except Exception:
        pass
    return res


def save_premux_mpls_metadata(job_temp_dir: str, mpls_bytes: bytes, mpls_name: str = ""):
    if not job_temp_dir or not mpls_bytes:
        return
    try:
        os.makedirs(job_temp_dir, exist_ok=True)
        stn_info = parse_mpls_stn_table_streams(mpls_bytes)
        clips, dur_s = parse_mpls_header_info(mpls_bytes)
        stn_info["mpls_name"] = mpls_name or ""
        stn_info["clips"] = clips
        stn_info["duration_sec"] = dur_s
        meta_p = os.path.join(job_temp_dir, PREMUX_MPLS_META_FILENAME)
        with open(meta_p, "w", encoding="utf-8") as wf:
            json.dump(stn_info, wf, indent=2)
    except Exception:
        pass


def load_premux_mpls_metadata(premux_mkv_path: str) -> dict:
    if not premux_mkv_path:
        return {}
    try:
        j_dir = os.path.dirname(os.path.abspath(premux_mkv_path))
        meta_p = os.path.join(j_dir, PREMUX_MPLS_META_FILENAME)
        if os.path.isfile(meta_p):
            with open(meta_p, "r", encoding="utf-8-sig") as rf:
                return json.load(rf)
    except Exception:
        pass
    return {}


def resolve_bdmv_mpls_path(root_dir: str, mpls_name: str, require_stream_dir: bool = False) -> str:
    """Najde soubor .MPLS v rozbaleném ISO nebo BDMV složce a vyhne se záložní složce BDMV/BACKUP/PLAYLIST."""
    if not root_dir or not os.path.isdir(root_dir) or not mpls_name:
        return ""
    m_clean = str(mpls_name).strip().upper()
    if not m_clean.endswith(".MPLS"):
        m_clean = f"{int(m_clean):05d}.MPLS" if m_clean.isdigit() else ""
    if not m_clean:
        return ""

    best_path = ""
    best_rank = -1
    root_abs = os.path.abspath(root_dir)
    for rd, _, fns in os.walk(root_dir):
        for f in fns:
            if f.upper() != m_clean:
                continue
            cand = os.path.join(rd, f)
            rel_parts = [p.upper() for p in os.path.relpath(rd, root_abs).replace("\\", "/").split("/") if p and p != "."]
            in_backup = "BACKUP" in rel_parts
            in_playlist = bool(os.path.basename(rd).upper() == "PLAYLIST")
            has_stream = os.path.isdir(os.path.join(os.path.dirname(rd), "STREAM"))

            if require_stream_dir and (in_backup or not has_stream):
                continue

            if not in_backup and in_playlist and has_stream:
                rank = 4
            elif not in_backup and in_playlist:
                rank = 3
            elif not in_backup and has_stream:
                rank = 2
            elif not in_backup:
                rank = 1
            else:
                rank = 0

            if rank > best_rank:
                best_rank = rank
                best_path = cand
    return best_path


def find_matching_mpls_in_bdmv(bdmv_dir: str, target_duration_sec: float = 0.0):
    if not bdmv_dir or not os.path.isdir(bdmv_dir):
        return "", "", [], 0
    best_cand = None
    best_score = -1.0

    for rd, _, fns in os.walk(bdmv_dir):
        if os.path.basename(rd).upper() != "PLAYLIST":
            continue
        stream_d = os.path.join(os.path.dirname(rd), "STREAM")
        if not os.path.isdir(stream_d):
            continue
        for f in sorted(fns):
            if not f.upper().endswith(".MPLS"):
                continue
            mp_full = os.path.join(rd, f)
            try:
                with open(mp_full, "rb") as mf:
                    mb = mf.read()
                clips, dur_s = parse_mpls_header_info(mb)
                if not clips or len(clips) > 120:
                    continue
                if len(set(clips)) == 1 and len(clips) > 3:
                    continue
                pl_bytes = 0
                valid_clips = []
                for cid in clips:
                    for ec in (".m2ts", ".M2TS"):
                        cfp = os.path.join(stream_d, cid + ec)
                        if os.path.isfile(cfp):
                            pl_bytes += os.path.getsize(cfp)
                            valid_clips.append(cfp)
                            break
                if not valid_clips or pl_bytes < 50 * 1024 * 1024:
                    continue

                if target_duration_sec > 60.0 and dur_s > 60.0:
                    diff_s = abs(dur_s - target_duration_sec)
                    if diff_s <= 5.0:
                        score = 1e15 - diff_s * 1e9 + pl_bytes
                    elif diff_s <= 60.0:
                        score = 1e13 - diff_s * 1e8 + pl_bytes
                    else:
                        score = pl_bytes * 0.1
                else:
                    score = float(pl_bytes)

                if score > best_score:
                    best_score = score
                    best_cand = (f.upper(), mp_full, valid_clips, pl_bytes)
            except Exception:
                pass

    return best_cand if best_cand else ("", "", [], 0)


def _resolve_sibling_tool(ref_bin: str, tool_names: tuple) -> str:
    """Najde doplňkový nástroj (ffprobe / mkvpropedit) nejdřív vedle ref_bin (např. HandBrakeCLI), až pak v PATH."""
    if ref_bin:
        ref_dir = os.path.dirname(os.path.abspath(str(ref_bin)))
        if ref_dir and os.path.isdir(ref_dir):
            for tn in tool_names:
                cand = os.path.join(ref_dir, tn)
                if os.path.isfile(cand):
                    return cand
    for tn in tool_names:
        hit = shutil.which(tn)
        if hit:
            return hit
    return ""


def _passthru_target_family(encoder_short: str, src_fam: str) -> str:
    """Pro obecné Auto Passthru ('copy' / 'copy:auto' / 'passthru') vrátí kodek zdrojové stopy."""
    s = str(encoder_short or "").lower().strip()
    if s.startswith("copy:"):
        s = s[5:].strip()
    elif s == "copy":
        s = ""
    s = s.replace("passthru", "").strip(": ").strip()
    if not s or s in ("copy", "auto", "none"):
        return src_fam
    return _norm_codec_family(s, "") or src_fam


def probe_source_audio_titles(source_path: str, ffprobe_bin: str = None):
    fp_b = ffprobe_bin or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not fp_b or not source_path or not os.path.exists(source_path):
        return {}, False
    tgt = find_largest_media_file_in_dir(source_path) if os.path.isdir(source_path) else source_path
    if not tgt or not os.path.isfile(tgt) or tgt.lower().endswith(".iso"):
        return {}, False
    try:
        cmd = [
            fp_b, "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index:stream_tags=title",
            "-of", "json", tgt
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12, creationflags=SubprocFlags())
        if res.returncode != 0:
            return {}, False
        sts = (json.loads(res.stdout or "{}")).get("streams") or []
        out = {}
        for a_idx, st in enumerate(sts):
            tgs = st.get("tags") or {}
            raw_t = ""
            for k, v in tgs.items():
                if str(k).lower() == "title" and v is not None:
                    raw_t = str(v).strip()
                    break
            out[a_idx] = raw_t
        return out, True
    except Exception:
        return {}, False


def is_job_nonzero_start(job_node: dict, root_item: dict = None) -> bool:
    """True, pokud úloha nezačíná od začátku souboru (kapitola > 1, čas > 0 s, snímek > 0)."""
    if not isinstance(job_node, dict):
        return False
    r_it = root_item if isinstance(root_item, dict) else {}
    ui_cfg = r_it.get("uiSettings") if isinstance(r_it.get("uiSettings"), dict) else {}
    src_d = job_node.get("Source") if isinstance(job_node.get("Source"), dict) else {}
    rng = src_d.get("Range") if isinstance(src_d.get("Range"), dict) else {}

    pt_m = job_node.get("PointToPointMode")
    if pt_m is None:
        rt = str(rng.get("Type") or ui_cfg.get("PtoPType") or "chapter").lower()
        pt_m = 0 if "chap" in rt else (1 if ("time" in rt or "sec" in rt) else (2 if "frame" in rt else 0))

    p_start = job_node.get("StartPoint") if job_node.get("StartPoint") is not None else (
        job_node.get("ChapterStart") if job_node.get("ChapterStart") is not None else rng.get("Start")
    )
    if p_start is None:
        return False
    try:
        si = int(float(p_start))
    except Exception:
        return False
    if pt_m == 0:
        return si > 1
    return si > 0


def is_job_partial_range(job_node: dict, root_item: dict = None):
    """Vrátí (is_partial, is_chapter_range): True jen tehdy, když úloha skutečně kóduje výřez (ne celý film 1..N)."""
    if not isinstance(job_node, dict):
        return False, False
    r_it = root_item if isinstance(root_item, dict) else {}
    ui_cfg = r_it.get("uiSettings") if isinstance(r_it.get("uiSettings"), dict) else {}
    t_meta = r_it.get("Title") if isinstance(r_it.get("Title"), dict) else {}
    s_info = r_it.get("SourceTitleInfo") if isinstance(r_it.get("SourceTitleInfo"), dict) else {}
    src_d = job_node.get("Source") if isinstance(job_node.get("Source"), dict) else {}
    rng = src_d.get("Range") if isinstance(src_d.get("Range"), dict) else {}

    pt_m = job_node.get("PointToPointMode")
    if pt_m is None:
        rt = str(rng.get("Type") or ui_cfg.get("PtoPType") or "chapter").lower()
        pt_m = 0 if "chap" in rt else (1 if ("time" in rt or "sec" in rt) else (2 if "frame" in rt else 0))

    p_start = job_node.get("StartPoint") if job_node.get("StartPoint") is not None else (
        job_node.get("ChapterStart") if job_node.get("ChapterStart") is not None else rng.get("Start")
    )
    p_end = job_node.get("EndPoint") if job_node.get("EndPoint") is not None else (
        job_node.get("ChapterEnd") if job_node.get("ChapterEnd") is not None else rng.get("End")
    )
    if p_start is None and p_end is None:
        return False, False

    try:
        si = int(float(p_start)) if p_start is not None else 1
    except Exception:
        si = 1
    try:
        ei = int(float(p_end)) if p_end is not None else 0
    except Exception:
        ei = 0

    if pt_m == 0:
        ch_list = t_meta.get("ChapterList") or s_info.get("Chapters") or []
        total_ch = len(ch_list) if isinstance(ch_list, list) else 0
        partial_ch = (si > 1) or (total_ch > 0 and ei > 0 and ei < total_ch)
        return partial_ch, partial_ch
    if pt_m == 1:
        rt_low = str((src_d.get("Range") or {}).get("Type") or "").lower()
        if rt_low == "time" or ei > 500000:
            si = int(round(si / 90000.0))
            ei = int(round(ei / 90000.0))
        exp_dur = extract_job_duration_seconds(job_node, r_it)
        partial_t = (si > 0) or (exp_dur > 0 and ei > 0 and ei < exp_dur - 5.0)
        return partial_t, False
    if pt_m == 2:
        return True, False
    return False, False


def _extract_job_audio_spec(t: dict, idx: int, is_libhb_a: bool, t_alist: list) -> dict:
    sc = t.get("ScannedTrack") or t.get("SourceTrack") or {}
    tr = t.get("Track")
    if tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t:
        s_tr = int(tr) + 1
        if 0 <= int(tr) < len(t_alist) and isinstance(t_alist[int(tr)], dict):
            sc = t_alist[int(tr)]
    else:
        s_tr = int(tr or sc.get("TrackNumber") or (idx + 1))

    desc = str(sc.get("Description") or sc.get("Name") or t.get("Name") or "")
    lg_code = str(sc.get("LanguageCode") or sc.get("Language") or "")
    lg_grp = _norm_lang_group(lg_code, desc)
    src_fam = _norm_codec_family(sc.get("Codec") or sc.get("Format") or "", desc)

    eo = t.get("Encoder")
    es_raw = str(eo.get("ShortName") if isinstance(eo, dict) else (eo or "copy")).strip()
    es_low = es_raw.lower()
    is_p = ("copy" in es_low or "pass" in es_low)
    out_fam = _passthru_target_family(es_low, src_fam) if is_p else _norm_codec_family(es_low, "")

    mx = t.get("MixDown") or t.get("Mixdown")
    mx_s = mx.get("ShortName", "auto") if isinstance(mx, dict) else str(mx or "auto").lower()
    ch = 6 if ("5.1" in desc or (not is_p and ("5point1" in mx_s or mx == 7))) else (
        8 if ("7.1" in desc or (not is_p and ("7point1" in mx_s or mx in (9, 10)))) else (
            2 if ("2.0" in desc or "stereo" in desc.lower() or (not is_p and ("stereo" in mx_s or mx == 4))) else (
                5 if "5.0" in desc else 0
            )
        )
    )
    m_kb = re.search(r"(\d+)\s*kbps", desc, re.IGNORECASE)
    kbps = int(m_kb.group(1)) if m_kb else 0
    return {
        "sc": sc,
        "s_tr": s_tr,
        "desc": desc,
        "lang_grp": lg_grp,
        "src_fam": src_fam,
        "out_fam": out_fam,
        "es_raw": es_raw,
        "is_passthru": is_p,
        "channels": ch,
        "kbps": kbps,
    }


def find_redundant_dts_core_indices(job_node: dict, root_item: dict = None) -> set:
    """V režimu dts_hd_mode='merge' najde indexy nadbytečných stop copy:dts (jádro), pro které úloha zároveň žádá copy:dtshd stejného jazyka."""
    if not isinstance(job_node, dict):
        return set()
    r_it = root_item if isinstance(root_item, dict) else {}
    t_meta = r_it.get("Title") if isinstance(r_it.get("Title"), dict) else {}
    s_info = r_it.get("SourceTitleInfo") if isinstance(r_it.get("SourceTitleInfo"), dict) else {}
    t_alist = t_meta.get("AudioList") or s_info.get("AudioTracks") or []

    raw_a = job_node.get("AudioTracks")
    is_libhb_a = not isinstance(raw_a, list)
    if is_libhb_a:
        raw_a = (job_node.get("Audio") or {}).get("AudioList") if isinstance(job_node.get("Audio"), dict) else []

    specs = []
    for idx, t in enumerate(raw_a or []):
        if isinstance(t, dict):
            sp = _extract_job_audio_spec(t, idx, is_libhb_a, t_alist)
            sp["idx"] = idx
            specs.append(sp)

    # Spárujeme každý požadovaný copy:dtshd nejvýše s jedním kompatibilním copy:dts jádrem stejného jazyka
    avail_hd = {}
    for sp in specs:
        if sp["is_passthru"] and sp["out_fam"] == "dtshd":
            avail_hd.setdefault(sp["lang_grp"], []).append(sp["channels"])

    redundant = set()
    for sp in specs:
        if not (sp["is_passthru"] and sp["out_fam"] == "dts"):
            continue
        hd_chs = avail_hd.get(sp["lang_grp"])
        if not hd_chs:
            continue
        c_dts = sp["channels"]
        match_pos = -1
        for hi, c_hd in enumerate(hd_chs):
            # 5.1 DTS jádro je kompatibilní s 5.1 i 7.1 DTS-HD MA; 2.0 DTS komentář ale neslučujeme do 5.1/7.1 DTS-HD MA
            if c_dts == 0 or c_hd == 0 or c_dts == c_hd or (c_dts >= 5 and c_hd >= 5):
                match_pos = hi
                break
        if match_pos >= 0:
            hd_chs.pop(match_pos)
            redundant.add(sp["idx"])
    return redundant


def job_needs_premux_audio_postproc(job_node: dict, root_item: dict = None, lpcm_passthru: bool = True, dts_hd_mode: str = "keep_both") -> bool:
    """Zjistí předem, zda by Pre-Mux vyžadoval FFmpeg injekci audia (LPCM nebo dca_core), která není vhodná pro ořezaný výřez."""
    if not isinstance(job_node, dict):
        return False
    r_it = root_item if isinstance(root_item, dict) else {}
    t_meta = r_it.get("Title") if isinstance(r_it.get("Title"), dict) else {}
    s_info = r_it.get("SourceTitleInfo") if isinstance(r_it.get("SourceTitleInfo"), dict) else {}
    t_alist = t_meta.get("AudioList") or s_info.get("AudioTracks") or []

    raw_a = job_node.get("AudioTracks")
    is_libhb_a = not isinstance(raw_a, list)
    if is_libhb_a:
        raw_a = (job_node.get("Audio") or {}).get("AudioList") if isinstance(job_node.get("Audio"), dict) else []

    merge_dts = str(dts_hd_mode or "keep_both").strip().lower() == "merge"
    redundant_dts = find_redundant_dts_core_indices(job_node, r_it) if merge_dts else set()

    # Zjistíme, pro které jazyky má zdrojový disk DTS-HD MA (zda copy:dts bude potřebovat dca_core)
    src_dtshd_langs = set()
    for ta in (t_alist or []):
        if isinstance(ta, dict):
            d_s = str(ta.get("Description") or ta.get("Name") or "")
            f_s = _norm_codec_family(ta.get("Codec") or ta.get("Format") or "", d_s)
            if f_s == "dtshd":
                src_dtshd_langs.add(_norm_lang_group(str(ta.get("LanguageCode") or ta.get("Language") or ""), d_s))

    for idx, t in enumerate(raw_a or []):
        if not isinstance(t, dict) or idx in redundant_dts:
            continue
        sp = _extract_job_audio_spec(t, idx, is_libhb_a, t_alist)
        if not sp["is_passthru"]:
            continue
        if lpcm_passthru and sp["src_fam"] == "pcm":
            return True
        if sp["out_fam"] == "dts" and (sp["lang_grp"] in src_dtshd_langs or not src_dtshd_langs):
            return True
    return False


def _assign_stn_audio_languages(a_list: list, stn_a_list: list):
    """Doplní jazyky z MPLS STN tabulky u stop bez jazyka ('und') s ohledem na rozdělení TrueHD+AC3 / E-AC3+AC3 v surovém .m2ts."""
    if not a_list or not stn_a_list:
        return
    if not any(st.get("lang_grp") == "und" for st in a_list):
        return

    si = 0
    i = 0
    n_a = len(a_list)
    n_s = len(stn_a_list)
    while i < n_a and si < n_s:
        stn_item = stn_a_list[si]
        stn_fam = str(stn_item.get("family") or "")
        stn_lg = stn_item.get("lang_grp") or "und"
        stn_raw = stn_item.get("raw_lang") or stn_lg

        # V surovém .m2ts mkvmerge rozbalí jednu Blu-ray stopu TrueHD+AC3 (0x83) nebo E-AC3+AC3 na dvě po sobě jdoucí MKV stopy
        is_split_pair = False
        if (n_a - i) > (n_s - si) and (i + 1) < n_a:
            f0 = a_list[i].get("family", "")
            f1 = a_list[i + 1].get("family", "")
            if stn_fam == "truehd" and {f0, f1} == {"truehd", "ac3"}:
                is_split_pair = True
            elif stn_fam == "eac3" and {f0, f1} == {"eac3", "ac3"}:
                is_split_pair = True

        if is_split_pair:
            for k in (i, i + 1):
                if a_list[k].get("lang_grp") == "und" and stn_lg != "und":
                    a_list[k]["lang_grp"] = stn_lg
                    a_list[k]["raw_lang"] = stn_raw
            i += 2
            si += 1
        else:
            if a_list[i].get("lang_grp") == "und" and stn_lg != "und":
                a_list[i]["lang_grp"] = stn_lg
                a_list[i]["raw_lang"] = stn_raw
            i += 1
            si += 1


def probe_premux_streams_info(mkv_path: str, ffprobe_bin: str = None):
    fp_b = ffprobe_bin or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not fp_b or not mkv_path or not os.path.isfile(mkv_path) or mkv_path.lower().endswith(".iso"):
        return [], []
    mpls_meta = load_premux_mpls_metadata(mkv_path)
    stn_a_list = mpls_meta.get("audio") or []
    stn_s_list = mpls_meta.get("pg") or []
    try:
        cmd = [
            fp_b, "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name,profile,channels,bit_rate:stream_disposition=default,forced:stream_tags=language,title,BPS,BPS-eng",
            "-of", "json", mkv_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=SubprocFlags())
        if res.returncode != 0:
            return [], []
        streams = (json.loads(res.stdout or "{}")).get("streams") or []
        a_list, s_list = [], []
        a_pos, s_pos = 1, 1
        for st in streams:
            ct = st.get("codec_type")
            tgs = st.get("tags") or {}
            disp = st.get("disposition") or {}
            lg = str(tgs.get("language") or "und")
            title = str(tgs.get("title") or "")
            cn = str(st.get("codec_name") or "")
            prof = str(st.get("profile") or "")
            fam = _norm_codec_family(cn, prof)
            ch = int(st.get("channels") or 0)
            bps = st.get("bit_rate") or tgs.get("BPS") or tgs.get("BPS-eng") or 0
            try:
                kbps = int(round(int(bps) / 1000.0))
            except Exception:
                kbps = 0

            if ct == "audio":
                lang_g = _norm_lang_group(lg, title)
                a_list.append({
                    "hb_track": a_pos,
                    "zero_idx": a_pos - 1,
                    "lang_grp": lang_g,
                    "raw_lang": lg,
                    "family": fam,
                    "codec_name": cn,
                    "channels": ch,
                    "kbps": kbps,
                    "title": title,
                })
                a_pos += 1
            elif ct == "subtitle":
                lang_g = _norm_lang_group(lg, title)
                if lang_g == "und" and (s_pos - 1) < len(stn_s_list):
                    stn_item = stn_s_list[s_pos - 1]
                    if stn_item.get("lang_grp") and stn_item["lang_grp"] != "und":
                        lang_g = stn_item["lang_grp"]
                        lg = stn_item.get("raw_lang") or lang_g
                s_list.append({
                    "hb_track": s_pos,
                    "zero_idx": s_pos - 1,
                    "lang_grp": lang_g,
                    "raw_lang": lg,
                    "family": fam,
                    "codec_name": cn,
                    "title": title,
                    "forced": bool(disp.get("forced")) or ("forced" in title.lower()),
                    "default": bool(disp.get("default")),
                })
                s_pos += 1

        _assign_stn_audio_languages(a_list, stn_a_list)

        # Doplníme pořadí v rámci jazyka/kodeku pro spolehlivé párování při více stejnojazyčných stopách
        a_ord_map = {}
        for st in a_list:
            ak = (st["lang_grp"], st["family"])
            st["spec_ord"] = a_ord_map.get(ak, 0)
            a_ord_map[ak] = st["spec_ord"] + 1

        s_ord_map = {}
        for st in s_list:
            sk = st["lang_grp"]
            st["lang_ord"] = s_ord_map.get(sk, 0)
            s_ord_map[sk] = st["lang_ord"] + 1

        return a_list, s_list
    except Exception:
        return [], []


def match_premux_audio_stream(
    exp_lang: str, exp_src_fam: str, exp_out_fam: str, is_passthru: bool,
    exp_ch: int, exp_kbps: int, orig_1based: int, premux_audio: list,
    used_audio_modes: set = None, exp_spec_ord: int = -1
):
    if not premux_audio:
        return None, False
    best_st = None
    best_score = -9999.0
    mode_key = (exp_out_fam or exp_src_fam or ("passthru" if is_passthru else "encode"))

    for st in premux_audio:
        score = 0.0
        if exp_lang != "und" and st["lang_grp"] == exp_lang:
            score += 60
        elif exp_lang != "und" and st["lang_grp"] != "und":
            score -= 60

        if exp_ch >= 5:
            if st["channels"] >= 5:
                score += 45
                if st["channels"] == exp_ch:
                    score += 8
            elif 0 < st["channels"] <= 2:
                score -= 65
        elif exp_ch == 2:
            if st["channels"] == 2:
                score += 40
            elif st["channels"] >= 5:
                score -= 30

        if exp_src_fam and st["family"] == exp_src_fam:
            score += 40
        elif exp_src_fam in ("dtshd", "dts") and st["family"] in ("dtshd", "dts"):
            score += 25
        elif exp_src_fam == "ac3" and st["family"] in ("truehd", "dtshd", "pcm"):
            score += 15
        elif is_passthru and exp_out_fam and st["family"] == exp_out_fam:
            score += 30

        if exp_kbps > 0 and st["kbps"] > 0 and abs(exp_kbps - st["kbps"]) <= 64:
            score += 15

        if exp_spec_ord >= 0 and "spec_ord" in st:
            if st["spec_ord"] == exp_spec_ord:
                score += 25
            elif abs(st["spec_ord"] - exp_spec_ord) == 1:
                score += 6

        if orig_1based and orig_1based > 0:
            score += max(0, 10 - abs(int(st["hb_track"]) - int(orig_1based)))
        score += max(0, 4 - int(st.get("zero_idx", st["hb_track"] - 1))) * 0.1

        if used_audio_modes and (st["hb_track"], mode_key) in used_audio_modes:
            score -= 120

        if score > best_score:
            best_score = score
            best_st = st

    if not best_st:
        return None, False

    needs_ac3_encode = bool(
        is_passthru
        and exp_out_fam in ("ac3", "eac3")
        and best_st["family"] != exp_out_fam
    )
    return best_st, needs_ac3_encode


def match_premux_sub_stream(
    exp_lang: str, exp_fam: str, orig_1based: int, premux_subs: list, used_tracks: set,
    exp_lang_ord: int = -1, exp_forced: bool = False
):
    if not premux_subs:
        return None
    best_st = None
    best_score = -9999.0

    for st in premux_subs:
        score = 0.0
        if exp_lang != "und" and st["lang_grp"] == exp_lang:
            score += 60
        elif exp_lang != "und" and st["lang_grp"] != "und":
            score -= 50
        if exp_fam and st["family"] == exp_fam:
            score += 15

        if exp_lang_ord >= 0 and "lang_ord" in st:
            if st["lang_ord"] == exp_lang_ord:
                score += 35
            elif abs(st["lang_ord"] - exp_lang_ord) == 1:
                score += 10

        if exp_forced and st.get("forced"):
            score += 12
        elif (not exp_forced) and (not st.get("forced")):
            score += 4

        if orig_1based and orig_1based > 0:
            score += max(0, 10 - abs(int(st["hb_track"]) - int(orig_1based)))

        if st["hb_track"] in used_tracks:
            score -= 120

        if score > best_score:
            best_score = score
            best_st = st
    return best_st


# ==============================================================================
# 2. VÝSTUPNÍ KONTROLNÍ BRÁNA STOP (verify_output_stream_contract)
# ==============================================================================
STREAM_AUDIT_SKIPPED = "SKIPPED"     # kontrola stop neproběhla (chybí ffprobe / soubor) – v logu „přeskočeno“, ne „OK“


def verify_output_stream_contract(mkv_path: str, job_node: dict, root_item: dict = None, ffprobe_bin: str = None, auto_clean_empty_subs: bool = True,
                                  burn_in_respected: bool = False, dts_hd_mode: str = "keep_both", premux_used: bool = False):
    """burn_in_respected (R1): vypálené titulky ve výstupu jako stopa nejsou – nepočítají se do očekávaných.
    dts_hd_mode: 'merge' vynechává nadbytečné DTS jádro, pokud úloha současně obsahuje DTS-HD MA téhož jazyka.
    premux_used: výstup vznikl z Pre-Mux zdroje – nepodporovaný passthru tam řeší Pre-Mux (např. DTS jádro), ne záložní enkodér."""
    fp_b = ffprobe_bin or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not fp_b or not mkv_path or not os.path.isfile(mkv_path):
        return True, STREAM_AUDIT_SKIPPED

    r_it = root_item if isinstance(root_item, dict) else {}
    t_meta = r_it.get("Title") if isinstance(r_it.get("Title"), dict) else {}
    s_info = r_it.get("SourceTitleInfo") if isinstance(r_it.get("SourceTitleInfo"), dict) else {}
    t_alist = t_meta.get("AudioList") or s_info.get("AudioTracks") or []
    t_slist = t_meta.get("SubtitleList") or s_info.get("Subtitles") or []

    raw_a = job_node.get("AudioTracks")
    is_libhb_a = not isinstance(raw_a, list)
    if is_libhb_a:
        raw_a = (job_node.get("Audio") or {}).get("AudioList") if isinstance(job_node.get("Audio"), dict) else []

    merge_dts = str(dts_hd_mode or "keep_both").strip().lower() == "merge"
    redundant_dts_idxs = find_redundant_dts_core_indices(job_node, r_it) if merge_dts else set()
    gui_fb_enc = gui_audio_fallback_encoder(job_node)

    exp_audio = []
    for idx, t in enumerate(raw_a or []):
        if not isinstance(t, dict) or idx in redundant_dts_idxs:
            continue
        sc = t.get("ScannedTrack") or t.get("SourceTrack") or {}
        tr = t.get("Track")
        if tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t:
            if 0 <= int(tr) < len(t_alist) and isinstance(t_alist[int(tr)], dict):
                sc = t_alist[int(tr)]

        desc = str(sc.get("Description") or sc.get("Name") or t.get("Name") or "")
        lg_code = str(sc.get("LanguageCode") or sc.get("Language") or "")
        lg_grp = _norm_lang_group(lg_code, desc)
        src_fam = _norm_codec_family(sc.get("Codec") or sc.get("Format") or "", desc)

        eo = t.get("Encoder")
        es = str(eo.get("ShortName") if isinstance(eo, dict) else (eo or "copy")).lower().strip()
        is_p = ("copy" in es or "pass" in es)
        if is_p and not is_libhb_a and not premux_used and gui_fb_enc and _gui_passthru_needs_fallback(t, sc):
            es = gui_fb_enc.lower()          # stejně jako překladač (build_hb_cmd_from_windows_json) a Windows GUI
            is_p = ("copy" in es or "pass" in es)
        if is_p:
            out_fam = _passthru_target_family(es, src_fam)
        else:
            out_fam = _norm_codec_family(es, "")

        mx = t.get("MixDown") or t.get("Mixdown")
        mx_s = mx.get("ShortName", "auto") if isinstance(mx, dict) else str(mx or "auto").lower()
        ch_exp = 6 if ("5.1" in desc or "5point1" in mx_s or mx == 7) else (8 if ("7.1" in desc or "7point1" in mx_s or mx in (9, 10)) else (2 if ("2.0" in desc or "stereo" in mx_s or mx == 4) else 0))

        exp_audio.append({
            "lang_grp": lg_grp,
            "out_family": out_fam,
            "channels": ch_exp,
            "desc": desc,
            "is_passthru": is_p
        })

    raw_s = job_node.get("SubtitleTracks")
    is_libhb_s = not isinstance(raw_s, list)
    if is_libhb_s:
        raw_s = (job_node.get("Subtitle") or {}).get("SubtitleList") if isinstance(job_node.get("Subtitle"), dict) else []

    exp_subs = []
    for t in (raw_s or []):
        if not isinstance(t, dict) or (burn_in_respected and subtitle_is_burned(t)):
            continue
        sc = t.get("SourceTrack") or t.get("ScannedTrack") or {}
        tr = t.get("Track")
        # Foreign Audio Scan nemusí najít žádnou cizojazyčnou stopu; chybějící externí SRT/SSA nelze přibalit
        if sc.get("IsFakeForeignAudioScanTrack") or (not is_libhb_s and (tr == 0 or (tr is None and sc.get("TrackNumber") == 0))):
            continue
        imp = t.get("Import") if isinstance(t.get("Import"), dict) else None
        if t.get("SubtitleType") in (7, 8) or t.get("SrtPath") or imp:
            ext_p = str(t.get("SrtPath") or (imp.get("Filename") if imp else "") or "").strip()
            if not (ext_p and os.path.isfile(ext_p)):
                continue
        if tr is not None and is_libhb_s and "SourceTrack" not in t and "ScannedTrack" not in t:
            if 0 <= int(tr) < len(t_slist) and isinstance(t_slist[int(tr)], dict):
                sc = t_slist[int(tr)]
        desc = str(sc.get("Name") or sc.get("Language") or t.get("Name") or "")
        lg_code = str(sc.get("LanguageCode") or sc.get("Language") or "")
        exp_subs.append({
            "lang_grp": _norm_lang_group(lg_code, desc),
            "forced": bool(t.get("Forced"))
        })

    try:
        cmd = [
            fp_b, "-v", "error",
            "-show_entries", "format=duration:stream=index,codec_type,codec_name,profile,channels:stream_tags=language,title",
            "-of", "json", mkv_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, creationflags=SubprocFlags())
        if res.returncode != 0:
            return False, f"ffprobe failed on output (rc={res.returncode})"
        d = json.loads(res.stdout or "{}")
        streams = d.get("streams") or []
        out_dur = float((d.get("format") or {}).get("duration") or 0.0)
        exp_dur = extract_job_duration_seconds(job_node, r_it)

        is_partial_range, _ = is_job_partial_range(job_node, r_it)

        if not is_partial_range and exp_dur > 600.0 and out_dur > 0.0 and out_dur < exp_dur * 0.85:
            return False, f"Output duration ({out_dur:.0f}s) is much shorter than expected ({exp_dur:.0f}s)!"

        act_audio, act_subs = [], []
        for st in streams:
            ct = st.get("codec_type")
            tgs = st.get("tags") or {}
            lg = str(tgs.get("language") or "und")
            title = str(tgs.get("title") or "")
            cn = str(st.get("codec_name") or "")
            prof = str(st.get("profile") or "")
            fam = _norm_codec_family(cn, prof)
            ch = int(st.get("channels") or 0)
            if ct == "audio":
                act_audio.append({"lang_grp": _norm_lang_group(lg, title), "raw_lang": lg, "family": fam, "channels": ch})
            elif ct == "subtitle":
                act_subs.append({"lang_grp": _norm_lang_group(lg, title), "raw_lang": lg, "family": fam})

        if len(act_audio) < len(exp_audio):
            rem_dtshd = {}
            rem_truehd = {}
            for x in exp_audio:
                if x["out_family"] == "dtshd":
                    rem_dtshd[x["lang_grp"]] = rem_dtshd.get(x["lang_grp"], 0) + 1
                elif x["out_family"] == "truehd":
                    rem_truehd[x["lang_grp"]] = rem_truehd.get(x["lang_grp"], 0) + 1
            max_skips = len(exp_audio) - len(act_audio)
            skipped = 0
            dedup_exp = []
            for ea in exp_audio:
                lg_e = ea["lang_grp"]
                cur_pos = len(dedup_exp)
                cur_act = act_audio[cur_pos] if cur_pos < len(act_audio) else None
                same_core_present = bool(
                    cur_act
                    and cur_act["family"] == ea["out_family"]
                    and (lg_e == "und" or cur_act["lang_grp"] == "und" or cur_act["lang_grp"] == lg_e)
                    and (ea["channels"] == 0 or cur_act["channels"] == 0 or cur_act["channels"] == ea["channels"])
                )
                if (
                    skipped < max_skips
                    and not same_core_present
                    and ea["out_family"] == "dts"
                    and ea.get("is_passthru")
                    and rem_dtshd.get(lg_e, 0) > 0
                ):
                    rem_dtshd[lg_e] -= 1
                    skipped += 1
                    continue
                if (
                    skipped < max_skips
                    and not same_core_present
                    and ea["out_family"] == "ac3"
                    and ea.get("is_passthru")
                    and rem_truehd.get(lg_e, 0) > 0
                    and ("448 kbps" not in str(ea.get("desc") or "").lower())
                ):
                    rem_truehd[lg_e] -= 1
                    skipped += 1
                    continue
                dedup_exp.append(ea)
            if len(dedup_exp) == len(act_audio):
                exp_audio = dedup_exp

        errors = []
        if len(act_audio) != len(exp_audio):
            errors.append(f"Audio track count {len(act_audio)} != expected {len(exp_audio)}")
        else:
            dtshd_exp = sum(1 for x in exp_audio if x["out_family"] == "dtshd")
            dtshd_act = sum(1 for x in act_audio if x["family"] == "dtshd")
            if dtshd_act > dtshd_exp:
                errors.append(f"Duplicate DTS-HD MA tracks ({dtshd_act} != expected {dtshd_exp})")

            for i, (ea, aa) in enumerate(zip(exp_audio, act_audio), start=1):
                if ea["lang_grp"] != "und" and aa["lang_grp"] != "und" and ea["lang_grp"] != aa["lang_grp"]:
                    errors.append(f"Audio #{i} lang '{aa['lang_grp']}' != expected '{ea['lang_grp']}'")
                if ea["out_family"] and aa["family"] and ea["out_family"] != aa["family"]:
                    if not (ea["out_family"] in ("dtshd", "dts") and aa["family"] in ("dtshd", "dts")):
                        errors.append(f"Audio #{i} codec '{aa['family']}' != expected '{ea['out_family']}'")
                if ea["channels"] >= 5 and 0 < aa["channels"] < 5:
                    errors.append(f"Audio #{i} channels {aa['channels']}ch != expected {ea['channels']}ch")

        non_forced_subs = [s for s in exp_subs if not s["forced"]]
        if len(non_forced_subs) > 0 and len(act_subs) == 0 and not auto_clean_empty_subs:
            errors.append(f"All subtitles missing (0 != expected {len(exp_subs)})")
        elif act_subs and exp_subs:
            exp_s_langs = {s["lang_grp"] for s in exp_subs if s["lang_grp"] != "und"}
            act_s_langs = {s["lang_grp"] for s in act_subs if s["lang_grp"] != "und"}
            if exp_s_langs and act_s_langs and not act_s_langs.issubset(exp_s_langs):
                errors.append(f"Subtitle languages {act_s_langs} != expected {exp_s_langs}")

        if errors:
            return False, "; ".join(errors)
        return True, f"Audio:{len(act_audio)}/{len(exp_audio)} OK, Sub:{len(act_subs)} OK"
    except Exception:
        return True, STREAM_AUDIT_SKIPPED


def sync_mkv_audio_titles(mkv_path: str, mkvpropedit_bin: str = None):
    if not mkv_path or not os.path.isfile(mkv_path) or not mkv_path.lower().endswith(".mkv"):
        return
    abs_p = os.path.abspath(mkv_path)
    with _pending_audio_titles_lock:
        desired_list = _pending_audio_titles.get(abs_p)
    if desired_list is None:
        return
    mp_b = mkvpropedit_bin or shutil.which("mkvpropedit") or shutil.which("mkvpropedit.exe")
    if not mp_b:
        return
    try:
        cmd = [mp_b, mkv_path]
        for idx, title_val in enumerate(desired_list, start=1):
            clean_t = str(title_val or "").strip()
            cmd.extend(["--edit", f"track:a{idx}", "--set", f"name={clean_t}"])
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=SubprocFlags())
        with _pending_audio_titles_lock:
            _pending_audio_titles.pop(abs_p, None)
    except Exception:
        pass


# ==============================================================================
# 3. NEBLOKUJÍCÍ PRŮBĚH PRE-MUXU A STRATEGIE DISKOVÉHO PROSTORU
# ==============================================================================
def _measure_extracted_clips_bytes(extract_dir: str, clips_set: set) -> int:
    tot = 0
    if not extract_dir or not os.path.isdir(extract_dir):
        return 0
    try:
        for rd, _, fns in os.walk(extract_dir):
            for f in fns:
                fu = f.upper()
                if fu.endswith(".M2TS") and (not clips_set or fu[:5] in clips_set):
                    try:
                        tot += os.path.getsize(os.path.join(rd, f))
                    except Exception:
                        pass
    except Exception:
        pass
    return tot


def run_7z_extract_with_disk_progress(proc, extract_dir: str, clips: list, expected_bytes: int, is_cancelled_fn, on_progress_fn):
    clips_set = set(clips or [])
    t0 = time.time()
    last_prog_ts = 0.0

    def _drain_stdout():
        try:
            if proc.stdout:
                for _ in proc.stdout:
                    pass
        except Exception:
            pass

    threading.Thread(target=_drain_stdout, daemon=True).start()

    last_remote_chk = t0
    while proc.poll() is None:
        now_t = time.time()
        chk_remote = (now_t - last_remote_chk) >= 12.0
        if chk_remote:
            last_remote_chk = now_t
        if is_cancelled_fn(chk_remote):
            kill_process_tree(proc)
            raise InterruptedError("Cancelled")

        if now_t - last_prog_ts >= 1.0:
            cur_b = _measure_extracted_clips_bytes(extract_dir, clips_set)
            elap = max(0.001, now_t - t0)
            spd = (cur_b / elap) / (1024 * 1024)
            pct = min(99.5, (cur_b / float(expected_bytes)) * 100.0) if expected_bytes > 0 else 0.0
            on_progress_fn(pct, spd, cur_b, expected_bytes, int(elap), now_t)
            last_prog_ts = now_t

        time.sleep(0.4)

    return proc.returncode


def run_mkvmerge_with_disk_progress(proc, out_mkv_path: str, expected_bytes: int, is_cancelled_fn, on_progress_fn) -> int:
    t0 = time.time()
    last_prog_ts = 0.0
    stdout_pct = [0.0]
    p_re = re.compile(r"#GUI#progress\s+(\d+)%|Progress:\s*(\d+)%", re.IGNORECASE)

    def _read_stdout():
        try:
            if proc.stdout:
                for ln in proc.stdout:
                    mp = p_re.search(ln)
                    if mp:
                        val = float(mp.group(1) or mp.group(2) or 0.0)
                        if val > stdout_pct[0]:
                            stdout_pct[0] = val
        except Exception:
            pass

    threading.Thread(target=_read_stdout, daemon=True).start()

    last_remote_chk = t0
    while proc.poll() is None:
        now_t = time.time()
        chk_remote = (now_t - last_remote_chk) >= 12.0
        if chk_remote:
            last_remote_chk = now_t
        if is_cancelled_fn(chk_remote):
            kill_process_tree(proc)
            raise InterruptedError("Cancelled")

        if now_t - last_prog_ts >= 1.0:
            cur_b = 0
            try:
                if out_mkv_path and os.path.isfile(out_mkv_path):
                    cur_b = os.path.getsize(out_mkv_path)
            except Exception:
                cur_b = 0
            elap = max(0.001, now_t - t0)
            spd = (cur_b / elap) / (1024 * 1024)
            disk_pct = min(99.5, (cur_b / float(expected_bytes)) * 100.0) if expected_bytes > 0 else 0.0
            # Se známou velikostí platí jen zapsaná data: mkvmerge u playlistu (.MPLS) hlásí průběh podle
            # malého souboru playlistu a ukáže 100 % hned na začátku. Jeho vlastní % jen když velikost neznáme.
            eff_pct = disk_pct if expected_bytes > 0 else min(99.5, stdout_pct[0])
            on_progress_fn(eff_pct, spd, cur_b, expected_bytes, int(elap), now_t)
            last_prog_ts = now_t

        time.sleep(0.4)

    proc.wait()
    return proc.returncode


def extract_job_duration_seconds(job_node: dict, root_item: dict = None) -> float:
    r_it = root_item if isinstance(root_item, dict) else {}
    t_meta = r_it.get("Title") if isinstance(r_it.get("Title"), dict) else {}
    s_info = r_it.get("SourceTitleInfo") if isinstance(r_it.get("SourceTitleInfo"), dict) else (job_node.get("SourceTitleInfo") if isinstance(job_node, dict) and isinstance(job_node.get("SourceTitleInfo"), dict) else {})
    st_node = r_it.get("Statistics") if isinstance(r_it.get("Statistics"), dict) else {}

    d_obj = t_meta.get("Duration") or s_info.get("Duration")
    if isinstance(d_obj, dict) and (d_obj.get("Hours") is not None or d_obj.get("Minutes") is not None):
        try:
            return int(d_obj.get("Hours", 0)) * 3600 + int(d_obj.get("Minutes", 0)) * 60 + float(d_obj.get("Seconds", 0))
        except Exception:
            pass

    d_str = (d_obj if isinstance(d_obj, str) else "") or st_node.get("SourceLength") or st_node.get("ContentLength") or ""
    if d_str and isinstance(d_str, str) and ":" in d_str:
        pts = d_str.split(":")
        if len(pts) >= 3:
            try:
                return int(pts[0]) * 3600 + int(pts[1]) * 60 + float(pts[2])
            except Exception:
                pass
    return 0.0


def is_job_4k_target(job_node: dict, root_item: dict = None, source_path: str = "") -> bool:
    if not isinstance(job_node, dict):
        return False
    pic_d = job_node.get("Picture") if isinstance(job_node.get("Picture"), dict) else {}
    ui_s = (root_item.get("uiSettings") if isinstance(root_item, dict) and isinstance(root_item.get("uiSettings"), dict) else {})
    mw = int(float(job_node.get("MaxWidth") or pic_d.get("MaxWidth") or ui_s.get("PictureWidth") or job_node.get("Width") or pic_d.get("Width") or 0))
    mh = int(float(job_node.get("MaxHeight") or pic_d.get("MaxHeight") or ui_s.get("PictureHeight") or job_node.get("Height") or pic_d.get("Height") or 0))
    if mw >= 2500 or mh >= 1400:
        return True
    if mw == 0 and mh == 0 and re.search(r'(?:2160p|\buhd\b|\b4k\b)', str(source_path or ""), re.IGNORECASE):
        return True
    return False


def reconcile_disc_video_bitrate(src_size_bytes: int, duration_sec: float, current_v_kbps: int, current_a_kbps: int) -> int:
    v_cur = int(current_v_kbps or 0)
    if v_cur == 2500:
        v_cur = 0
    if not src_size_bytes or src_size_bytes < 100 * 1024 * 1024 or not duration_sec or duration_sec <= 60.0:
        return v_cur

    a_cur = int(current_a_kbps or 0)
    if a_cur <= 0:
        a_cur = 2500

    stream_payload_b = int(src_size_bytes * 0.94)
    expected_total_kbps = int(round((stream_payload_b * 8.0) / (float(duration_sec) * 1000.0)))
    eff_a = min(a_cur, int(expected_total_kbps * 0.35))
    expected_v_kbps = max(0, expected_total_kbps - eff_a) if expected_total_kbps > eff_a + 1000 else 0

    if expected_v_kbps <= 0:
        return v_cur
    if v_cur <= 0 or (expected_v_kbps >= 8000 and v_cur < expected_v_kbps * 0.55) or (v_cur > expected_v_kbps * 1.65):
        return expected_v_kbps
    return v_cur


def evaluate_disk_space_strategy(
    local_temp_dir: str,
    outputs_dir: str,
    phys_src_bytes: int,
    est_stream_bytes: int,
    is_4k: bool,
    use_local_write: bool,
    do_staging: bool,
    smart_premux: bool,
    is_disc: bool,
    is_iso: bool,
    use_relay_mode: bool = False
) -> dict:
    free_temp_b = 0
    try:
        os.makedirs(local_temp_dir, exist_ok=True)
        _, _, free_temp_b = shutil.disk_usage(local_temp_dir)
    except Exception:
        free_temp_b = 0

    ref_stream_b = est_stream_bytes if est_stream_bytes > 0 else phys_src_bytes
    min_encode_reserve_b = max(
        (85 if is_4k else 30) * (1024**3),
        int(ref_stream_b * (0.85 if is_4k else 0.50))
    )

    res = {
        "free_temp_gb": round(free_temp_b / (1024**3), 1),
        "allow_premux": False,
        "premux_skip_reason": "",
        "allow_staging": bool(do_staging and not use_relay_mode),
        "staging_skip_reason": "",
        "allow_local_write": bool(use_local_write),
        "direct_to_outputs": False,
        "direct_outputs_reason": "",
        "is_net_fallback": False,
        "net_fallback_reason": "",
    }

    if is_disc and smart_premux and use_local_write:
        req_premux_b = int(ref_stream_b * (2.2 if is_iso else 1.4)) + int(min_encode_reserve_b * 0.5)
        if free_temp_b >= req_premux_b:
            res["allow_premux"] = True
        else:
            res["allow_premux"] = False
            res["premux_skip_reason"] = T("wc_pre_mux_skipped_direct", f'{free_temp_b / 1024 ** 3:.1f}', f'{req_premux_b / 1024 ** 3:.1f}')

    if res["allow_staging"] and not use_relay_mode:
        req_staging_b = int(phys_src_bytes) + int(min_encode_reserve_b)
        if free_temp_b < req_staging_b:
            res["allow_staging"] = False
            res["is_net_fallback"] = True
            res["staging_skip_reason"] = T("wc_local_source_staging_gb", f'{phys_src_bytes / 1024 ** 3:.1f}', f'{free_temp_b / 1024 ** 3:.1f}', f'{min_encode_reserve_b / 1024 ** 3:.1f}')
            res["net_fallback_reason"] = json.dumps({
                "kind": "staging_space",
                "free_gb": round(free_temp_b / (1024**3), 1),
                "src_gb": round(phys_src_bytes / (1024**3), 1),
                "req_gb": round(req_staging_b / (1024**3), 1),
            })

    if res["allow_local_write"] and not use_relay_mode:
        outputs_writable = bool(outputs_dir and os.path.isdir(outputs_dir))
        if free_temp_b < min_encode_reserve_b and outputs_writable:
            res["allow_local_write"] = False
            res["allow_staging"] = False
            res["allow_premux"] = False
            res["direct_to_outputs"] = True
            res["is_net_fallback"] = True
            res["direct_outputs_reason"] = T("wc_low_space_hb_temp", f'{free_temp_b / 1024 ** 3:.1f}', f'{min_encode_reserve_b / 1024 ** 3:.1f}')
            res["net_fallback_reason"] = json.dumps({
                "kind": "direct_outputs_space",
                "free_gb": round(free_temp_b / (1024**3), 1),
                "src_gb": round(phys_src_bytes / (1024**3), 1),
                "req_gb": round(min_encode_reserve_b / (1024**3), 1),
            })

    return res


def choose_safe_postproc_temp_mkv(w_dest: str, orig_d: str) -> str:
    try:
        f_sz = os.path.getsize(w_dest) if os.path.isfile(w_dest) else 0
        w_dir = os.path.dirname(os.path.abspath(w_dest))
        _, _, free_w = shutil.disk_usage(w_dir)
        if free_w < int(f_sz * 1.15) + (2 * 1024**3) and orig_d:
            out_dir = os.path.dirname(os.path.abspath(orig_d))
            if os.path.isdir(out_dir):
                _, _, free_o = shutil.disk_usage(out_dir)
                if free_o > free_w:
                    return orig_d + ".clean.tmp.mkv"
    except Exception:
        pass
    return w_dest + ".clean.mkv"


# ==============================================================================
# 4. UDP DISCOVERY, HTTP RELAY, PROCESY A STRIKTNÍ MULTI-THREAD ECO AFINITA
# ==============================================================================
_net_info_cache = {"ts": 0.0, "url": "", "data": {}}


def get_worker_network_info(server_url: str) -> dict:
    """Skutečné IPv4 adresy tohoto stroje pro zobrazení v Manageru.
    route_ip = adresa rozhraní, přes které vede cesta k Manageru (kabel / Wi-Fi / VPN);
    ips = ostatní adresy (bez 127.x a 169.254.x), max 8. Cache 60 s. V Dockeru (bridge) to bývá 172.x –
    takovou adresu web nezobrazí a použije adresu, kterou vidí Manager."""
    now = time.time()
    if _net_info_cache["data"] and _net_info_cache["url"] == server_url and now - _net_info_cache["ts"] < 60:
        return _net_info_cache["data"]
    route_ip = ""
    try:
        pu = urllib.parse.urlparse(server_url or "")
        host = pu.hostname or ""
        port = pu.port or (443 if pu.scheme == "https" else 80)
        if host:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(2.0)
                s.connect((socket.gethostbyname(host), int(port)))  # UDP connect nic neposílá, jen vybere trasu
                route_ip = s.getsockname()[0]
            finally:
                s.close()
    except Exception:
        route_ip = ""
    ips = []
    try:
        import psutil  # noqa: WPS433 – volitelné, ve workerech je
        for _nic, addrs in (psutil.net_if_addrs() or {}).items():
            for a in addrs:
                if getattr(a, "family", None) == socket.AF_INET:
                    ip = str(a.address or "")
                    if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
                        ips.append(ip)
    except Exception:
        try:
            for inf in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = inf[4][0]
                if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
    if route_ip.startswith("127."):
        route_ip = ""
    data = {"route_ip": route_ip, "ips": ips[:8]}
    _net_info_cache.update(ts=now, url=server_url, data=data)
    return data


def discover_manager_udp(port: int = UDP_DISCOVERY_PORT, timeout: float = 2.5, prefer_bound: bool = True):
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        pkt = b"HB_DISCOVER_MANAGER_V1"
        targets = {"255.255.255.255"}
        try:
            s_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s_probe.connect(("8.8.8.8", 80))
            local_ip = s_probe.getsockname()[0]
            s_probe.close()
            pts = local_ip.split(".")
            if len(pts) == 4:
                targets.add(f"{pts[0]}.{pts[1]}.{pts[2]}.255")
        except Exception:
            pass

        for t_ip in targets:
            try:
                sock.sendto(pkt, (t_ip, int(port)))
            except Exception:
                pass

        bound_id = bound_manager_id() if prefer_bound else ""
        found = {}   # url -> resp
        deadline = time.time() + max(0.3, float(timeout))
        first_seen_ts = 0.0
        while True:
            rem = deadline - time.time()
            if first_seen_ts > 0.0:
                # Po první odpovědi krátce počkat (~0.6s) na případný druhý Manager v síti
                rem = min(rem, (first_seen_ts + 0.6) - time.time())
            if rem <= 0.0:
                break
            sock.settimeout(max(0.05, rem))
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            try:
                resp = json.loads((data or b"{}").decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if isinstance(resp, dict) and resp.get("magic") == "HB_MANAGER_HERE":
                m_port = int(resp.get("port") or port)
                m_url = f"http://{addr[0]}:{m_port}"
                if m_url not in found:
                    found[m_url] = resp
                    if first_seen_ts == 0.0:
                        first_seen_ts = time.time()
                # Pokud odpověděl přesně náš navázaný Manager, můžeme skončit hned
                if bound_id and str(resp.get("manager_id") or "").strip() == bound_id:
                    break

        if found:
            chosen_url = None
            chosen_resp = None
            if bound_id:
                for u, r in found.items():
                    if str(r.get("manager_id") or "").strip() == bound_id:
                        chosen_url, chosen_resp = u, r
                        break
                if chosen_url is None:
                    # Pokud známe manager_id všech odpovídajících a žádný není náš navázaný, cizí Manager nevnucovat
                    all_have_id = all(bool(str(r.get("manager_id") or "").strip()) for r in found.values())
                    if all_have_id:
                        return None, {"_foreign_managers": list(found.keys())}
            if chosen_url is None:
                chosen_url = sorted(found.keys())[0]
                chosen_resp = found[chosen_url]
            out_resp = dict(chosen_resp)
            if len(found) > 1:
                out_resp["_all_managers"] = sorted(found.keys())
            if out_resp.get("ui_lang") in ("en", "cs"):
                set_worker_lang(out_resp["ui_lang"])
            return chosen_url, out_resp
    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return None, None


def relay_download_source_to_temp(manager_url: str, job_id: int, worker_name: str, dst_dir: str, is_cancelled_fn, on_progress_fn) -> str:
    sess = requests.Session()
    sess.headers.update(worker_auth_headers())
    try:
        r_info = sess.get(
            f"{manager_url}/api/jobs/{job_id}/relay_source_info",
            params={"worker_name": worker_name},
            timeout=20
        )
        if not check_manager_identity(r_info):
            raise ManagerMismatchError(T('w_mgr_bind_refused', foreign_manager_id() or "-"))
        if r_info.status_code != 200:
            raise RuntimeError(f"HTTP Relay source_info failed ({r_info.status_code})")

        info = r_info.json()
        is_dir = bool(info.get("is_dir"))
        # jména z odpovědi nikdy nesmí vést mimo dočasnou složku úlohy
        base_name = os.path.basename(str(info.get("base_name") or "").replace("\\", "/")).strip(" .") or f"job_{job_id}_src"
        tot_bytes = int(info.get("total_bytes") or 0)
        files_list = info.get("files") or []
        if not files_list:
            raise RuntimeError("HTTP Relay: empty source file list")

        os.makedirs(dst_dir, exist_ok=True)
        if tot_bytes > 0:
            try:
                free_b = shutil.disk_usage(dst_dir).free
                if free_b < tot_bytes + 512 * 1024 * 1024:
                    raise OSError(
                        errno.ENOSPC,
                        f"Not enough local temp space for HTTP Relay download ({free_b / (1024**3):.1f} GB free, need {tot_bytes / (1024**3):.1f} GB)"
                    )
            except OSError as _derr:
                if _derr.errno == errno.ENOSPC:
                    raise
        copied = 0
        t0 = time.time()

        def _dl_one(rel_p: str, local_dst: str):
            nonlocal copied
            os.makedirs(os.path.dirname(local_dst), exist_ok=True)
            prms = {"worker_name": worker_name}
            if is_dir:
                prms["rel_path"] = rel_p
            with sess.get(
                f"{manager_url}/api/jobs/{job_id}/relay_download",
                params=prms,
                stream=True,
                timeout=(15, 300)
            ) as resp:
                if not check_manager_identity(resp):
                    raise ManagerMismatchError(T('w_mgr_bind_refused', foreign_manager_id() or "-"))
                resp.raise_for_status()
                last_remote_chk = time.time()
                with open(local_dst, "wb", buffering=16 * 1024 * 1024) as wf:
                    for chunk in resp.iter_content(chunk_size=16 * 1024 * 1024):
                        now_c = time.time()
                        chk_r = (now_c - last_remote_chk) >= 12.0
                        if chk_r:
                            last_remote_chk = now_c
                        if is_cancelled_fn(chk_r):
                            raise InterruptedError("Cancelled")
                        if not chunk:
                            continue
                        wf.write(chunk)
                        copied += len(chunk)
                        now_t = time.time()
                        elap = max(0.001, now_t - t0)
                        spd = (copied / elap) / (1024 * 1024)
                        prog = (copied / tot_bytes) * 100.0 if tot_bytes > 0 else 0.0
                        on_progress_fn(prog, spd, copied, int(elap), now_t)

        if not is_dir:
            target_f = os.path.join(dst_dir, base_name)
            _dl_one(files_list[0]["rel_path"], target_f)
            return target_f

        target_root = os.path.join(dst_dir, base_name)
        os.makedirs(target_root, exist_ok=True)
        for f_entry in files_list:
            rp = str(f_entry.get("rel_path") or "").lstrip("\\/")
            if rp:
                loc_p = os.path.normpath(os.path.join(target_root, rp))
                if not path_is_within(target_root, loc_p):
                    raise RuntimeError(f"HTTP Relay: invalid file path in source list: {rp[:120]}")
                _dl_one(rp, loc_p)
        return target_root
    finally:
        sess.close()


def relay_upload_output_to_manager(manager_url: str, job_id: int, worker_name: str, local_file: str, dest_filename: str, subfolder: str, is_cancelled_fn, on_progress_fn) -> dict:
    if not local_file or not os.path.isfile(local_file):
        raise RuntimeError(f"Relay upload source missing: {local_file}")

    tot_bytes = os.path.getsize(local_file)
    sent = 0
    t0 = time.time()

    def _stream_gen():
        nonlocal sent
        last_remote_chk = time.time()
        with open(local_file, "rb", buffering=16 * 1024 * 1024) as rf:
            while True:
                now_c = time.time()
                chk_r = (now_c - last_remote_chk) >= 12.0
                if chk_r:
                    last_remote_chk = now_c
                if is_cancelled_fn(chk_r):
                    raise InterruptedError("Cancelled")
                buf = rf.read(16 * 1024 * 1024)
                if not buf:
                    break
                sent += len(buf)
                now_t = time.time()
                elap = max(0.001, now_t - t0)
                spd = (sent / elap) / (1024 * 1024)
                prog = (sent / tot_bytes) * 100.0 if tot_bytes > 0 else 100.0
                on_progress_fn(prog, spd, sent, int(elap), now_t)
                yield buf

    r = requests.put(
        f"{manager_url}/api/jobs/{job_id}/relay_upload",
        params={"worker_name": worker_name, "dest_filename": dest_filename, "subfolder": subfolder or "", "expected_size": tot_bytes},
        data=_stream_gen(),
        headers={"Content-Type": "application/octet-stream", **worker_auth_headers()},
        timeout=(15, 600)
    )
    if not check_manager_identity(r):
        raise ManagerMismatchError(T('w_mgr_bind_refused', foreign_manager_id() or "-"))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP Relay upload failed ({r.status_code}): {r.text[:160]}")
    return r.json()


def inputs_match_manager(in_dir: str, init_data: dict) -> bool:
    """Handshake: vidí worker ve své složce Inputs totéž co Manager? Manager do Inputs nic nezapisuje –
    pošle vzorek souborů (cesta + velikost) a worker spočítá shody (aspoň 3, nebo všechny při menším vzorku).
    Prázdné Inputs Manageru = shoda jen s prázdnými Inputs workeru. Starší Manager: zkušební soubor / jména."""
    if not in_dir or not os.path.isdir(in_dir):
        return False
    d = init_data if isinstance(init_data, dict) else {}
    files = d.get("sample_inputs_files")
    if isinstance(files, list):
        if not files:
            try:
                return not any(not n.startswith(".") for n in os.listdir(in_dir))
            except Exception:
                return False
        tested = hits = 0
        for it in files[:24]:
            if not isinstance(it, dict):
                continue
            parts = [x for x in str(it.get("path") or "").replace("\\", "/").split("/") if x]
            if not parts or any(x in (".", "..") for x in parts):
                continue
            tested += 1
            try:
                fp = os.path.join(in_dir, *parts)
                if os.path.isfile(fp) and os.path.getsize(fp) == int(it.get("size", -1)):
                    hits += 1
            except Exception:
                pass
        return tested > 0 and hits >= min(3, tested)
    m_in = os.path.basename(str(d.get("mgr_in_probe_filename") or ""))
    if m_in and os.path.isfile(os.path.join(in_dir, m_in)):
        return True
    s_items = d.get("sample_inputs_entries") or []
    if s_items:
        return any(os.path.exists(os.path.join(in_dir, os.path.basename(str(x)))) for x in s_items)
    return True


# ------------------------------------------------------------------------------
# VÝSTUP: NIKDY NEPŘEPSAT EXISTUJÍCÍ SOUBOR V OUTPUTS A NIKDY NEZAPSAT DO INPUTS
# Existuje-li cílový soubor, nový dostane jméno „Název [ID 1234].mkv“ (pak „[ID 1234-2]“ …).
# Skutečné jméno jde do logu a jako final_dest_path do Manageru. Stejná pravidla má relay v Manageru.
# ------------------------------------------------------------------------------
_ID_SUFFIX_RE = re.compile(r" \[ID \d+(?:-\d+)?\]$")


def get_disk_free_gb(path: str) -> float:
    """Volné místo ve složce v GB; -1.0, když ho nejde zjistit (volající pak místo nehlídá)."""
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:
        return -1.0


def free_output_path(dest_path: str, job_id) -> str:
    """Cesta, která nic nepřepíše: dest_path, nebo (když už existuje) varianta s „[ID <úloha>]“."""
    if not dest_path or not os.path.lexists(dest_path):
        return dest_path
    stem, ext = os.path.splitext(dest_path)
    stem = _ID_SUFFIX_RE.sub("", stem)
    for n in range(1, 1000):
        cand = f"{stem} [ID {job_id}]{ext}" if n == 1 else f"{stem} [ID {job_id}-{n}]{ext}"
        if not os.path.lexists(cand):
            return cand
    raise RuntimeError(f"No free output file name for {dest_path}")


def place_output_file(tmp_path: str, dest_path: str, job_id, own: bool = False) -> str:
    """Přesune hotový dočasný soubor na místo bez přepsání cizího souboru a vrátí skutečnou cestu.
    own=True: dest_path si tato úloha vybrala už při předchozím pokusu → svůj soubor smí atomicky nahradit."""
    if own:
        os.replace(tmp_path, dest_path)
        return dest_path
    final_p = dest_path
    for _ in range(25):
        if os.path.lexists(final_p):
            final_p = free_output_path(final_p, job_id)
        if os.name == "nt":
            try:
                os.rename(tmp_path, final_p)
                return final_p
            except FileExistsError:
                final_p = free_output_path(final_p, job_id)
                continue
        else:
            # POSIX: os.link + os.unlink atomicky odmítne přepis existujícího cíle (FileExistsError)
            try:
                os.link(tmp_path, final_p)
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return final_p
            except FileExistsError:
                final_p = free_output_path(final_p, job_id)
                continue
            except OSError:
                # SMB/CIFS/FAT bez podpory hardlinků → pád na rename po nové kontrole
                if os.path.lexists(final_p):
                    final_p = free_output_path(final_p, job_id)
                os.rename(tmp_path, final_p)
                return final_p
    raise RuntimeError(f"Could not atomically place output file without overwrite: {dest_path}")


def _norm_real(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def path_is_within(root: str, path: str) -> bool:
    if not root or not path:
        return False
    try:
        r, q = _norm_real(root), _norm_real(path)
        return os.path.commonpath([r, q]) == r
    except Exception:
        return False


def output_target_refusal(dest_path: str, source_path: str, inputs_dir: str) -> str:
    """Pojistka před zápisem výstupu: cíl nesmí být zdroj úlohy ani ležet v Inputs (zdroje jsou jen pro čtení).
    Vrací přeloženou zprávu o odmítnutí, nebo "" = v pořádku."""
    if not dest_path:
        return ""
    try:
        if source_path and _norm_real(dest_path) == _norm_real(source_path):
            return T('w_out_target_is_source', dest_path)
    except Exception:
        pass
    if inputs_dir and path_is_within(inputs_dir, dest_path):
        return T('w_out_target_in_inputs', dest_path, inputs_dir)
    return ""


class WorkerLogBuffer:
    def __init__(self, maxlen: int = 200):
        self._lock = threading.Lock()
        self._pending = deque(maxlen=maxlen)

    def append(self, formatted_line: str):
        if not formatted_line:
            return
        with self._lock:
            self._pending.append(str(formatted_line).rstrip("\r\n"))

    def drain(self, max_items: int = 50) -> list:
        out = []
        with self._lock:
            while self._pending and len(out) < max_items:
                out.append(self._pending.popleft())
        return out


if os.name == "nt":
    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]


def get_process_tree_pids(root_pid: int) -> list:
    if not root_pid or root_pid <= 0:
        return []
    if os.name == "nt":
        visited = [int(root_pid)]
        seen = {int(root_pid)}
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            h_snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
            if h_snap and h_snap != wintypes.HANDLE(-1).value:
                pe = _PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                parent_map = {}
                if k32.Process32FirstW(h_snap, ctypes.byref(pe)):
                    while True:
                        parent_map.setdefault(int(pe.th32ParentProcessID), []).append(int(pe.th32ProcessID))
                        if not k32.Process32NextW(h_snap, ctypes.byref(pe)):
                            break
                k32.CloseHandle(h_snap)
                q = [int(root_pid)]
                while q:
                    curr = q.pop(0)
                    for ch in parent_map.get(curr, []):
                        if ch not in seen:
                            seen.add(ch)
                            visited.append(ch)
                            q.append(ch)
        except Exception:
            pass
        return visited

    if not os.path.isdir("/proc"):
        return [int(root_pid)]

    visited = []
    queue_pids = [int(root_pid)]
    seen = {int(root_pid)}

    while queue_pids:
        curr = queue_pids.pop(0)
        visited.append(curr)
        task_dir = f"/proc/{curr}/task"
        if os.path.isdir(task_dir):
            try:
                for tid in os.listdir(task_dir):
                    ch_file = os.path.join(task_dir, tid, "children")
                    if os.path.isfile(ch_file):
                        with open(ch_file, "r", encoding="utf-8", errors="ignore") as f:
                            for tok in f.read().split():
                                if tok.isdigit():
                                    cpid = int(tok)
                                    if cpid not in seen:
                                        seen.add(cpid)
                                        queue_pids.append(cpid)
            except Exception:
                pass

    if len(visited) == 1:
        try:
            parent_map = {}
            for entry in os.listdir("/proc"):
                if entry.isdigit():
                    pid_i = int(entry)
                    try:
                        with open(f"/proc/{pid_i}/stat", "r", encoding="utf-8", errors="ignore") as sf:
                            st = sf.read()
                        rpar = st.rfind(")")
                        if rpar != -1:
                            flds = st[rpar + 2:].split()
                            if len(flds) >= 2 and flds[1].isdigit():
                                ppid_i = int(flds[1])
                                parent_map.setdefault(ppid_i, []).append(pid_i)
                    except Exception:
                        pass
            q2 = [int(root_pid)]
            while q2:
                curr = q2.pop(0)
                for ch_pid in parent_map.get(curr, []):
                    if ch_pid not in seen:
                        seen.add(ch_pid)
                        visited.append(ch_pid)
                        q2.append(ch_pid)
        except Exception:
            pass

    return visited


_HYBRID_CPU_CACHE = None


def _parse_linux_cpulist(spec: str) -> list:
    out = []
    for part in str(spec or "").split(","):
        p = part.strip()
        if not p:
            continue
        if "-" in p:
            a, _, b = p.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                s_i, e_i = int(a.strip()), int(b.strip())
                if e_i >= s_i and (e_i - s_i) <= 1024:
                    out.extend(range(s_i, e_i + 1))
        elif p.isdigit():
            out.append(int(p))
    return sorted(set(out))


def normalize_custom_cores(raw_cores, total_threads: int = 0) -> list:
    max_t = int(total_threads) if (total_threads and int(total_threads) > 0) else max(1, int(os.cpu_count() or 1))
    if raw_cores is None:
        return []
    if isinstance(raw_cores, str):
        s = raw_cores.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                raw_cores = json.loads(s)
            except Exception:
                raw_cores = _parse_linux_cpulist(s.strip("[]"))
        else:
            raw_cores = _parse_linux_cpulist(s)
    if not isinstance(raw_cores, (list, tuple, set)):
        return []
    out = []
    for item in raw_cores:
        try:
            idx = int(item)
            if 0 <= idx < max_t:
                out.append(idx)
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def detect_cpu_topology(force_refresh: bool = False) -> dict:
    """
    Detekuje kompletní topologii CPU (fyzická jádra, HT/SMT vlákna, P-Cores a E-Cores).
    Na homogenním CPU (např. klasický Ryzen / Xeon) vrací is_hybrid=False, ale přesně
    rozliší fyzická jádra (no_smt_indices) a SMT vlákna (smt_sibling_indices).
    """
    global _HYBRID_CPU_CACHE
    if _HYBRID_CPU_CACHE is not None and not force_refresh:
        return dict(_HYBRID_CPU_CACHE)

    total = max(1, int(os.cpu_count() or 1))
    e_indices = []
    p_indices = []
    no_smt_indices = []
    smt_sibling_indices = []
    physical_cores = total
    p_physical_cores = 0
    e_physical_cores = 0
    is_hybrid = False

    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            buf_len = wintypes.DWORD(0)
            k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(buf_len))
            if buf_len.value > 0:
                buf = (ctypes.c_ubyte * buf_len.value)()
                if k32.GetLogicalProcessorInformationEx(0, ctypes.byref(buf), ctypes.byref(buf_len)):
                    raw = bytes(buf[:buf_len.value])
                    off = 0
                    ptr_sz = ctypes.sizeof(ctypes.c_size_t)
                    rec_sz = ptr_sz + 8
                    entries = []
                    while off + 8 <= len(raw):
                        rel = int.from_bytes(raw[off:off + 4], "little")
                        sz = int.from_bytes(raw[off + 4:off + 8], "little")
                        if sz <= 0 or off + sz > len(raw):
                            break
                        if rel == 0 and off + 32 <= off + sz:
                            eff_cls = raw[off + 9]
                            grp_cnt = int.from_bytes(raw[off + 30:off + 32], "little")
                            g_off = off + 32
                            cpus_here = []
                            for _ in range(grp_cnt):
                                if g_off + rec_sz <= off + sz:
                                    mask_v = int.from_bytes(raw[g_off:g_off + ptr_sz], "little")
                                    grp = int.from_bytes(raw[g_off + ptr_sz:g_off + ptr_sz + 2], "little")
                                    if grp == 0:
                                        for bit in range(64):
                                            if mask_v & (1 << bit):
                                                cpus_here.append(bit)
                                g_off += rec_sz
                            if cpus_here:
                                cpus_sorted = sorted(set(cpus_here))
                                entries.append((eff_cls, cpus_sorted))
                                no_smt_indices.append(cpus_sorted[0])
                                if len(cpus_sorted) > 1:
                                    smt_sibling_indices.extend(cpus_sorted[1:])
                        off += sz
                    if entries:
                        physical_cores = len(entries)
                    classes = sorted({e[0] for e in entries})
                    if len(classes) >= 2:
                        min_cls = classes[0]
                        for eff_cls, cpus_here in entries:
                            if eff_cls == min_cls:
                                e_indices.extend(cpus_here)
                                e_physical_cores += 1
                            else:
                                p_indices.extend(cpus_here)
                                p_physical_cores += 1
                        e_indices = sorted(set(e_indices))
                        p_indices = sorted(set(p_indices))
                        is_hybrid = bool(e_indices and p_indices)
                    else:
                        p_physical_cores = physical_cores
        except Exception:
            is_hybrid = False
    else:
        base_sys = "/sys/devices/system/cpu"
        try:
            if os.path.isdir(base_sys):
                core_groups = {}
                for dn in sorted(os.listdir(base_sys)):
                    if dn.startswith("cpu") and dn[3:].isdigit():
                        cid = int(dn[3:])
                        sib_p = os.path.join(base_sys, dn, "topology", "thread_siblings_list")
                        if not os.path.isfile(sib_p):
                            sib_p = os.path.join(base_sys, dn, "topology", "core_cpus_list")
                        if os.path.isfile(sib_p):
                            with open(sib_p, "r", encoding="utf-8", errors="ignore") as f:
                                sibs = tuple(_parse_linux_cpulist(f.read()))
                            if sibs:
                                core_groups[sibs] = sibs
                            else:
                                core_groups[(cid,)] = (cid,)
                        else:
                            core_groups[(cid,)] = (cid,)
                if core_groups:
                    physical_cores = len(core_groups)
                    for grp in sorted(core_groups.keys()):
                        no_smt_indices.append(grp[0])
                        if len(grp) > 1:
                            smt_sibling_indices.extend(grp[1:])
        except Exception:
            pass

        try:
            atom_p = "/sys/devices/cpu_atom/cpus"
            if os.path.isfile(atom_p):
                with open(atom_p, "r", encoding="utf-8", errors="ignore") as f:
                    e_indices = _parse_linux_cpulist(f.read())
                if e_indices and len(e_indices) < total:
                    p_indices = [i for i in range(total) if i not in set(e_indices)]
                    is_hybrid = bool(p_indices)
        except Exception:
            pass
        if not is_hybrid:
            try:
                caps = {}
                if os.path.isdir(base_sys):
                    for dn in os.listdir(base_sys):
                        if dn.startswith("cpu") and dn[3:].isdigit():
                            cap_f = os.path.join(base_sys, dn, "cpu_capacity")
                            if os.path.isfile(cap_f):
                                with open(cap_f, "r", encoding="utf-8", errors="ignore") as f:
                                    v = f.read().strip()
                                if v.isdigit():
                                    caps[int(dn[3:])] = int(v)
                    if caps:
                        vals = sorted(set(caps.values()))
                        if len(vals) >= 2:
                            min_v = vals[0]
                            e_indices = sorted(i for i, c in caps.items() if c == min_v)
                            p_indices = sorted(i for i, c in caps.items() if c > min_v)
                            is_hybrid = bool(e_indices and p_indices)
            except Exception:
                pass
        if is_hybrid:
            e_set = set(e_indices)
            e_physical_cores = sum(1 for c in no_smt_indices if c in e_set) or len(e_indices)
            p_physical_cores = max(1, physical_cores - e_physical_cores)
        else:
            p_physical_cores = physical_cores

    no_smt_indices = sorted(set(no_smt_indices)) if no_smt_indices else list(range(total))
    smt_sibling_indices = sorted(set(smt_sibling_indices))
    has_smt = bool(len(smt_sibling_indices) > 0 or physical_cores < total)

    res = {
        "is_hybrid": bool(is_hybrid),
        "total_threads": total,
        "physical_cores": int(physical_cores),
        "has_smt": bool(has_smt),
        "e_core_indices": e_indices if is_hybrid else [],
        "p_core_indices": p_indices if is_hybrid else list(range(total)),
        "no_smt_indices": no_smt_indices,
        "smt_sibling_indices": smt_sibling_indices,
        "p_core_count": len(p_indices) if is_hybrid else total,
        "e_core_count": len(e_indices) if is_hybrid else 0,
        "p_physical_cores": int(p_physical_cores or physical_cores),
        "e_physical_cores": int(e_physical_cores if is_hybrid else 0),
    }
    _HYBRID_CPU_CACHE = dict(res)
    return res


def apply_eco_affinity_to_process_tree(root_pid: int, enable_eco=False, initial_allowed_cpus: list = None, hybrid_info: dict = None, custom_cores: list = None):
    """
    Nastaví CPU afinitu a prioritu stromu procesů podle režimu:
      - FULL (enable_eco=False nebo 'FULL'): 100% vláken, PRIORITA BELOW_NORMAL / nice 0.
      - ECO (enable_eco=True nebo 'ECO'): cores - 2 vláken, PRIORITA BELOW_NORMAL / nice 10 (BEZ Windows EcoQoS!).
      - NIGHT / HYBRID_ECO: pouze E-cores (pokud jsou dostupná, jinak polovina / cores-2 jader), priorita IDLE / nice 15.
      - CUSTOM: přesně vybraná vlákna z custom_cores, PRIORITA BELOW_NORMAL / nice 5.
    """
    if not root_pid or root_pid <= 0:
        return
    if isinstance(enable_eco, str):
        mode = enable_eco.strip().upper()
        if mode not in ("FULL", "ECO", "NIGHT", "HYBRID_ECO", "CUSTOM"):
            mode = "ECO" if mode else "FULL"
    else:
        mode = "ECO" if enable_eco else "FULL"

    hyb = hybrid_info if isinstance(hybrid_info, dict) else (detect_cpu_topology() if mode in ("HYBRID_ECO", "NIGHT", "CUSTOM") else {})
    e_cpus = list(hyb.get("e_core_indices") or []) if hyb.get("is_hybrid") else []
    tot_t = int(hyb.get("total_threads") or os.cpu_count() or 1)
    cust_list = normalize_custom_cores(custom_cores, tot_t) if mode == "CUSTOM" else []
    if mode == "CUSTOM" and not cust_list:
        mode = "FULL"
    if mode == "HYBRID_ECO" and not e_cpus:
        mode = "ECO"

    all_pids = get_process_tree_pids(root_pid)

    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
            k32.SetProcessAffinityMask.restype = wintypes.BOOL
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.SetPriorityClass.restype = wintypes.BOOL
            k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenThread.restype = wintypes.HANDLE
            k32.SetThreadAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
            k32.SetThreadAffinityMask.restype = ctypes.c_size_t
            k32.CloseHandle.argtypes = [wintypes.HANDLE]

            cores = min(os.cpu_count() or 1, 64)
            f_mask = (1 << cores) - 1
            if mode == "CUSTOM" and cust_list:
                mask_int = 0
                for ci in cust_list:
                    if 0 <= ci < 64:
                        mask_int |= (1 << ci)
                if mask_int <= 0:
                    mask_int = f_mask
                prio_cls = BELOW_NORMAL_PRIORITY_CLASS
            elif mode in ("HYBRID_ECO", "NIGHT") and e_cpus:
                mask_int = 0
                for ci in e_cpus:
                    if 0 <= ci < 64:
                        mask_int |= (1 << ci)
                if mask_int <= 0:
                    mask_int = ((1 << max(1, cores - 2)) - 1) if cores >= 4 else f_mask
                prio_cls = IDLE_PRIORITY_CLASS
            elif mode == "NIGHT":
                night_c = max(1, cores // 2) if cores >= 4 else max(1, cores - 1)
                mask_int = (1 << night_c) - 1
                prio_cls = IDLE_PRIORITY_CLASS
            elif mode == "ECO":
                mask_int = ((1 << max(1, cores - 2)) - 1) if cores >= 4 else f_mask
                prio_cls = BELOW_NORMAL_PRIORITY_CLASS
            else:
                mask_int = f_mask
                prio_cls = BELOW_NORMAL_PRIORITY_CLASS
            target_mask = ctypes.c_size_t(mask_int)

            for cpid in all_pids:
                hp = k32.OpenProcess(0x0200 | 0x1000, False, int(cpid))
                if hp:
                    k32.SetProcessAffinityMask(hp, target_mask)
                    k32.SetPriorityClass(hp, prio_cls)
                    k32.CloseHandle(hp)

            pid_set = set(all_pids)
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            h_snap = k32.CreateToolhelp32Snapshot(0x00000004, 0)
            if h_snap and h_snap != wintypes.HANDLE(-1).value:
                te = _THREADENTRY32()
                te.dwSize = ctypes.sizeof(_THREADENTRY32)
                if k32.Thread32First(h_snap, ctypes.byref(te)):
                    while True:
                        if int(te.th32OwnerProcessID) in pid_set:
                            ht = k32.OpenThread(0x0060, False, int(te.th32ThreadID))
                            if ht:
                                k32.SetThreadAffinityMask(ht, target_mask)
                                k32.CloseHandle(ht)
                        if not k32.Thread32Next(h_snap, ctypes.byref(te)):
                            break
                k32.CloseHandle(h_snap)
        except Exception:
            pass
    else:
        base_cpus = sorted(initial_allowed_cpus) if initial_allowed_cpus else list(range(os.cpu_count() or 1))
        base_set = set(base_cpus)
        if mode == "CUSTOM" and cust_list:
            target_cpus = {c for c in cust_list if c in base_set} or base_set
            prio_val = 5
        elif mode in ("HYBRID_ECO", "NIGHT") and e_cpus:
            target_cpus = {c for c in e_cpus if c in base_set} or base_set
            prio_val = 15
        elif mode == "NIGHT":
            night_cnt = max(1, len(base_cpus) // 2) if len(base_cpus) >= 4 else max(1, len(base_cpus) - 1)
            target_cpus = set(base_cpus[:night_cnt])
            prio_val = 15
        elif mode == "ECO":
            target_cpus = set(base_cpus[:-2] if len(base_cpus) >= 4 else base_cpus)
            prio_val = 10
        else:
            target_cpus = set(base_cpus)
            prio_val = 0
        for cpid in all_pids:
            try:
                os.setpriority(os.PRIO_PROCESS, int(cpid), prio_val)
            except Exception:
                pass
            try:
                os.sched_setaffinity(int(cpid), target_cpus)
            except Exception:
                pass
            task_dir = f"/proc/{cpid}/task"
            if os.path.isdir(task_dir):
                try:
                    for tid_s in os.listdir(task_dir):
                        if tid_s.isdigit():
                            tid_i = int(tid_s)
                            try:
                                os.sched_setaffinity(tid_i, target_cpus)
                            except Exception:
                                pass
                            try:
                                os.setpriority(os.PRIO_PROCESS, tid_i, prio_val)
                            except Exception:
                                pass
                except Exception:
                    pass


def kill_process_tree(proc, wait_s: float = 5.0):
    """
    Ukončí celý strom procesů (HandBrakeCLI, ffmpeg, mkvmerge, 7zz vč. staticx potomků)
    a proces sklidí (wait), takže nevzniknou zombie procesy držící soubory v /temp.
    """
    if not proc:
        return
    try:
        pid = proc.pid
        if pid:
            all_pids = get_process_tree_pids(pid)
            if os.name == "nt":
                try:
                    k32 = ctypes.windll.kernel32
                    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                    k32.OpenProcess.restype = wintypes.HANDLE
                    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
                    k32.CloseHandle.argtypes = [wintypes.HANDLE]
                    for cpid in reversed(all_pids):
                        hp = k32.OpenProcess(0x0001, False, int(cpid))  # PROCESS_TERMINATE
                        if hp:
                            k32.TerminateProcess(hp, 1)
                            k32.CloseHandle(hp)
                except Exception:
                    pass
            else:
                for cpid in reversed(all_pids):
                    try:
                        os.kill(cpid, signal.SIGKILL)
                    except Exception:
                        pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=wait_s)
        except Exception:
            pass
    except Exception:
        pass


# ------------------------------------------------------------------------------
# WINDOWS JOB OBJECT (KILL_ON_JOB_CLOSE)
# ------------------------------------------------------------------------------
# Všechny dceřiné procesy Windows Workeru (HandBrakeCLI.exe, ffmpeg.exe, mkvmerge.exe,
# 7z.exe) se automaticky ukončí i při pádu / zabití worker.pyw – žádné osiřelé procesy.
_win_job_handle = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _set_win_job_kill_on_close(enable: bool) -> bool:
    if os.name != "nt" or not _win_job_handle:
        return False
    try:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # 0x2000 = KILL_ON_JOB_CLOSE, 0x800 = BREAKAWAY_OK (umožní odpojit OTA restart skript)
        info.BasicLimitInformation.LimitFlags = (0x2000 | 0x800) if enable else 0x800
        k32 = ctypes.windll.kernel32
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        return bool(k32.SetInformationJobObject(_win_job_handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
    except Exception:
        return False


def install_windows_kill_on_close_job() -> bool:
    global _win_job_handle
    if os.name != "nt" or _win_job_handle:
        return bool(_win_job_handle)
    h_job = None
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h_job = k32.CreateJobObjectW(None, None)
        if not h_job:
            return False
        _win_job_handle = h_job
        if not _set_win_job_kill_on_close(True) or not k32.AssignProcessToJobObject(h_job, k32.GetCurrentProcess()):
            _win_job_handle = None
            k32.CloseHandle(h_job)
            return False
        return True
    except Exception:
        if h_job:
            try:
                ctypes.windll.kernel32.CloseHandle(h_job)
            except Exception:
                pass
        _win_job_handle = None
        return False


def release_windows_kill_on_close_job():
    """Před OTA restartem: vypne KILL_ON_JOB_CLOSE, aby update skript přežil ukončení workeru."""
    _set_win_job_kill_on_close(False)


def run_tracked_subprocess(cmd: list, register_fn=None, creationflags: int = 0) -> int:
    """
    Spustí dlouhý podproces (post-processing ffmpeg) jako Popen, zaregistruje ho do
    active_jobs (register_fn), aby šel při Cancel/Stop korektně ukončit celý strom,
    a výstup zahazuje (žádné hromadění MB logu v RAM jako u subprocess.run(PIPE)).
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    if register_fn:
        try:
            register_fn(proc)
        except Exception:
            pass
    try:
        return proc.wait()
    finally:
        if proc.poll() is None:
            kill_process_tree(proc)


def suspend_or_resume_process_tree(proc, suspend: bool = True):
    if not proc or proc.poll() is not None:
        return
    try:
        pid = proc.pid
        if os.name == "nt":
            ntdll = ctypes.windll.ntdll
            ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtSuspendProcess.restype = ctypes.c_long
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            for cpid in get_process_tree_pids(pid):
                h_proc = k32.OpenProcess(0x0800, False, int(cpid))  # PROCESS_SUSPEND_RESUME
                if h_proc:
                    if suspend:
                        ntdll.NtSuspendProcess(h_proc)
                    else:
                        ntdll.NtResumeProcess(h_proc)
                    k32.CloseHandle(h_proc)
        else:
            sig = signal.SIGSTOP if suspend else signal.SIGCONT
            all_pids = get_process_tree_pids(pid)
            for cpid in (all_pids if suspend else reversed(all_pids)):
                try:
                    os.kill(cpid, sig)
                except Exception:
                    pass
    except Exception:
        pass


def check_live_pause_and_disk_guard(
    proc,
    target_write_dir: str,
    state_ref: dict,
    is_cancelled_fn,
    is_user_paused_fn,
    log_fn=None,
    status_cb=None
):
    if not proc or proc.poll() is not None:
        return

    def _get_free_gb():
        if not target_write_dir:
            return 999.0
        try:
            _, _, fb = shutil.disk_usage(target_write_dir)
            return fb / (1024**3)
        except Exception:
            return 999.0

    user_paused = bool(is_user_paused_fn())
    free_gb = _get_free_gb()
    disk_low = (free_gb < 3.5)

    if not user_paused and not disk_low:
        return

    st_lock = state_ref.get("lock")

    def _enter_suspend(r_kind: str) -> bool:
        if st_lock is not None:
            st_lock.acquire()
        try:
            if state_ref.get("suspended"):
                return False
            suspend_or_resume_process_tree(proc, suspend=True)
            state_ref["suspended"] = True
            state_ref["reason"] = r_kind
            return True
        finally:
            if st_lock is not None:
                st_lock.release()

    def _exit_suspend():
        if st_lock is not None:
            st_lock.acquire()
        try:
            if not state_ref.get("suspended"):
                return None
            suspend_or_resume_process_tree(proc, suspend=False)
            prev_r = state_ref.get("reason")
            state_ref["suspended"] = False
            state_ref["reason"] = ""
            return prev_r
        finally:
            if st_lock is not None:
                st_lock.release()

    reason_kind = "USER" if user_paused else "DISK"
    if _enter_suspend(reason_kind):
        if reason_kind == "DISK":
            msg = T("wc_disk_guard_critically_low", f'{free_gb:.2f}', target_write_dir)
            if log_fn:
                log_fn(msg)
            if status_cb:
                status_cb(T("wc_paused_disk_full_gb", f'{free_gb:.1f}'))
        else:
            msg = T("wc_paused_job_paused_frozen")
            if log_fn:
                log_fn(msg)
            if status_cb:
                status_cb(T("wc_paused_frozen"))

    while state_ref.get("suspended") and proc.poll() is None:
        if is_cancelled_fn(True):
            _exit_suspend()
            return
        time.sleep(1.5)
        user_paused = bool(is_user_paused_fn())
        free_gb = _get_free_gb()
        disk_ok = (free_gb >= 5.0) if state_ref.get("reason") == "DISK" else (free_gb >= 3.5)
        if not user_paused and disk_ok:
            prev_r = _exit_suspend()
            if prev_r is not None:
                if log_fn:
                    if prev_r == "DISK":
                        log_fn(T("wc_disk_guard_disk_space", f'{free_gb:.1f}'))
                    else:
                        log_fn(T("wc_resumed_job_unfrozen_resuming"))
                if status_cb:
                    status_cb("")
            break


def save_held_job_meta(job_temp_dir: str, meta_dict: dict) -> bool:
    tmp_p = ""
    try:
        os.makedirs(job_temp_dir, exist_ok=True)
        meta_p = os.path.join(job_temp_dir, HELD_META_FILENAME)
        tmp_p = meta_p + ".tmp"
        with open(tmp_p, "w", encoding="utf-8") as f:
            json.dump(meta_dict, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_p, meta_p)
        return True
    except Exception:
        if tmp_p and os.path.exists(tmp_p):
            try:
                os.remove(tmp_p)
            except Exception:
                pass
        return False


def remove_held_job_meta(job_temp_dir: str):
    try:
        meta_p = os.path.join(job_temp_dir, HELD_META_FILENAME)
        if os.path.isfile(meta_p):
            os.remove(meta_p)
    except Exception:
        pass


def load_persisted_held_jobs(local_temp_dir: str) -> dict:
    held = {}
    if not local_temp_dir or not os.path.isdir(local_temp_dir):
        return held
    try:
        for item in os.listdir(local_temp_dir):
            if not item.startswith("job_"):
                continue
            j_dir = os.path.join(local_temp_dir, item)
            meta_p = os.path.join(j_dir, HELD_META_FILENAME)
            if os.path.isfile(meta_p):
                try:
                    with open(meta_p, "r", encoding="utf-8-sig") as f:
                        data = json.load(f)
                    jid = int(data.get("job_id") or item.split("_")[1])
                    w_dest = data.get("w_dest") or ""
                    if w_dest and os.path.isfile(w_dest):
                        data["job_id"] = jid
                        data["job_temp_dir"] = j_dir
                        held[jid] = data
                    else:
                        shutil.rmtree(j_dir, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass
    return held


def flush_held_jobs_metadata(held_jobs: dict, held_lock, local_temp_dir: str):
    """Zajistí okamžité zapsání held_job_meta.json pro všechny aktuálně podržené úlohy před restartem/aktualizací."""
    try:
        with held_lock:
            snap = [(jid, dict(meta)) for jid, meta in held_jobs.items()]
        for jid, meta in snap:
            j_tmp = meta.get("job_temp_dir") or os.path.join(local_temp_dir, f"job_{jid}")
            if j_tmp and os.path.isdir(j_tmp):
                save_held_job_meta(j_tmp, meta)
    except Exception:
        pass


# ------------------------------------------------------------------------------
# SLOŽKA TEMP: důkladně se uklízí JEN složka se značkou HB (skrytý soubor .hb_cluster_temp).
# Výchozí Temp workeru (Windows app/temp, Linux /data/temp) značku dostane hned. Vlastní složka Temp:
# prázdná nebo se značkou → použít přímo; s cizími soubory → worker pracuje v podsložce HB_Temp
# (se značkou), cizí soubory nechá být a rozpracované úlohy job_* do ní přesune.
# ------------------------------------------------------------------------------
HB_TEMP_MARKER = ".hb_cluster_temp"
HB_TEMP_SUBDIR = "HB_Temp"
_JOB_DIR_RE = re.compile(r"^job_(\d+)(?:_sysbuild)?$")


def is_marked_temp_dir(d: str) -> bool:
    return bool(d) and os.path.isfile(os.path.join(d, HB_TEMP_MARKER))


def _mark_temp_dir(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        mp = os.path.join(d, HB_TEMP_MARKER)
        if not os.path.isfile(mp):
            with open(mp, "w", encoding="utf-8") as f:
                f.write("HB Cluster worker temp folder - its contents are deleted automatically.\n")
            if os.name == "nt":
                try:
                    ctypes.windll.kernel32.SetFileAttributesW(mp, 0x02)   # FILE_ATTRIBUTE_HIDDEN
                except Exception:
                    pass
        return True
    except Exception:
        return False


def _has_foreign_entries(d: str) -> bool:
    """Je ve složce něco jiného než značka, podsložka HB_Temp a složky úloh job_*?"""
    try:
        for item in os.listdir(d):
            if item in (HB_TEMP_MARKER, HB_TEMP_SUBDIR):
                continue
            if _JOB_DIR_RE.match(item) and os.path.isdir(os.path.join(d, item)):
                continue
            return True
    except Exception:
        return True          # nejde přečíst → raději považovat za cizí
    return False


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def relocate_job_dirs(src_dir: str, dst_dir: str) -> int:
    """Přesune složky job_* ze src_dir do dst_dir a opraví cesty v held_job_meta.json (podržené hotové převody)."""
    moved = 0
    try:
        items = os.listdir(src_dir)
    except Exception:
        return 0
    for item in items:
        sp = os.path.join(src_dir, item)
        if not (_JOB_DIR_RE.match(item) and os.path.isdir(sp) and not os.path.islink(sp)):
            continue
        dp = os.path.join(dst_dir, item)
        if os.path.exists(dp):
            continue
        try:
            shutil.move(sp, dp)
            moved += 1
        except Exception:
            continue
        meta_p = os.path.join(dp, HELD_META_FILENAME)
        if os.path.isfile(meta_p):
            try:
                with open(meta_p, "r", encoding="utf-8-sig") as f:
                    m = json.load(f)
                sp_abs = os.path.normcase(os.path.abspath(sp))
                w_dest = str(m.get("w_dest") or "")
                if w_dest:
                    wd_abs = os.path.abspath(w_dest)
                    try:
                        inside = os.path.commonpath([sp_abs, os.path.normcase(wd_abs)]) == sp_abs
                    except Exception:
                        inside = False
                    if inside:
                        m["w_dest"] = os.path.join(dp, os.path.relpath(wd_abs, os.path.abspath(sp)))
                m["job_temp_dir"] = dp
                save_held_job_meta(dp, m)
            except Exception:
                pass
    return moved


def prepare_worker_temp_dir(chosen_dir: str, default_dirs=(), log_fn=None) -> str:
    """Vrátí složku, ve které worker pracuje a smí uklízet (vždy se značkou HB). Viz popis nad HB_TEMP_MARKER."""
    d = os.path.normpath(chosen_dir) if chosen_dir else ""
    if not d:
        return d
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return d
    sub = os.path.join(d, HB_TEMP_SUBDIR)
    if any(x and _same_dir(d, x) for x in (default_dirs or ())) or is_marked_temp_dir(d):
        _mark_temp_dir(d)
        return d
    if is_marked_temp_dir(sub):
        relocate_job_dirs(d, sub)
        return sub
    if not _has_foreign_entries(d):
        _mark_temp_dir(d)
        return d
    if not _mark_temp_dir(sub):
        return d             # podsložku nejde vytvořit → úklid v d smaže jen osiřelé job_* (bez značky)
    n = relocate_job_dirs(d, sub)
    if log_fn:
        try:
            log_fn(T('w_temp_foreign_subdir', d, sub, n))
        except Exception:
            pass
    return sub


def cleanup_orphaned_job_temp_dirs(local_temp_dir: str, active_ids, held_ids, uploading_id=None, live_check_fn=None, min_age_sec: float = 0.0):
    """Uklidí složku Temp workeru: osiřelé složky úloh job_* vždy, ostatní zbytky (spadlé převody, starší verze)
    jen ve složce se značkou HB – cizí soubory ve složce bez značky zůstanou netknuté.
    live_check_fn(jid, is_sysbuild) -> bool (True = úloha je právě aktivní/držená, nemazat!) předchází TOCTOU závodu."""
    try:
        if not os.path.exists(local_temp_dir):
            return
        act_set = set(active_ids or ())
        held_set = set(held_ids or ())
        keep_ids = act_set | held_set
        if uploading_id is not None:
            keep_ids.add(uploading_id)
        marked = is_marked_temp_dir(local_temp_dir)
        now_t = time.time()

        for item in os.listdir(local_temp_dir):
            if item == HB_TEMP_MARKER:
                continue
            ip = os.path.join(local_temp_dir, item)
            mj = _JOB_DIR_RE.match(item)
            if mj and os.path.isdir(ip):
                jid = int(mj.group(1))
                is_sb = item.endswith("_sysbuild")
                keep = (jid in act_set) if is_sb else (jid in keep_ids)
                if not keep and live_check_fn is not None:
                    try:
                        if live_check_fn(jid, is_sb):
                            keep = True
                    except Exception:
                        keep = True
                if not keep and min_age_sec > 0.0:
                    try:
                        if (now_t - os.path.getmtime(ip)) < min_age_sec:
                            keep = True
                    except Exception:
                        pass
                should_delete = not keep
            else:
                should_delete = marked
                if should_delete and min_age_sec > 0.0:
                    try:
                        if (now_t - os.path.getmtime(ip)) < min_age_sec:
                            should_delete = False
                    except Exception:
                        pass
            if should_delete:
                try:
                    if os.path.isdir(ip) and not os.path.islink(ip):
                        shutil.rmtree(ip, ignore_errors=True)
                    else:
                        os.remove(ip)
                except Exception:
                    pass
    except Exception:
        pass


# ==============================================================================
# 5. GENERÁTOR 32-BITOVÝCH RGBA IKON A TRANSAKČNÍ AUTO-UPDATER
# ==============================================================================
def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _point_in_poly(x: float, y: float, poly):
    inside = False
    n = len(poly)
    px1, py1 = poly[0]
    for i in range(1, n + 1):
        px2, py2 = poly[i % n]
        if ((py1 > y) != (py2 > y)) and (x < (px2 - px1) * (y - py1) / (py2 - py1 + 1e-9) + px1):
            inside = not inside
        px1, py1 = px2, py2
    return inside


def _icon_shape_hit(kind: str, u: float, v: float) -> bool:
    au, av = abs(u), abs(v)
    if kind == "cog":
        r = math.hypot(u, v)
        if r < 0.30 or r > 0.88:
            return False
        ang = math.atan2(v, u)
        outer = 0.68 + 0.17 * math.cos(8.0 * ang)
        return r <= outer
    elif kind == "stop":
        if max(au, av, (au + av) * 0.7071) > 0.84:
            return False
        if au < 0.44 and av < 0.44 and abs(au - av) < 0.15:
            return False
        return True
    elif kind == "play":
        if u < -0.52 or u > 0.68:
            return False
        half_h = 0.68 * (0.68 - u) / 1.20
        return av <= half_h
    elif kind == "pause":
        return (0.18 <= au <= 0.54) and (av <= 0.66)
    elif kind == "bolt":
        bolt_poly = [(0.15, -0.85), (-0.55, 0.08), (-0.02, 0.08), (-0.22, 0.85), (0.55, -0.08), (0.04, -0.08)]
        return _point_in_poly(u, v, bolt_poly)
    elif kind == "save":
        if au > 0.74 or av > 0.74 or (u - v) > 1.08:
            return False
        if -0.38 <= u <= 0.28 and -0.74 <= v <= -0.22:
            if not (0.04 <= u <= 0.18 and -0.64 <= v <= -0.30):
                return False
        if au <= 0.46 and 0.14 <= v <= 0.62:
            return False
        return True
    elif kind == "search":
        r_ring = math.hypot(u + 0.14, v + 0.14)
        if 0.34 <= r_ring <= 0.56:
            return True
        if u > 0.12 and v > 0.12 and abs(u - v) < 0.16 and math.hypot(u, v) <= 0.86:
            return True
        return False
    return False


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _find_tray_bold_font_path() -> str:
    win_dir = os.environ.get("WINDIR", r"C:\Windows")
    for fp in (
        os.path.join(win_dir, "Fonts", "arialbd.ttf"),
        os.path.join(win_dir, "Fonts", "segoeuib.ttf"),
        os.path.join(win_dir, "Fonts", "calibrib.ttf"),
        os.path.join(win_dir, "Fonts", "verdanab.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.isfile(fp):
            return fp
    return ""


def _find_chip_black_font_path() -> str:
    """Najde nejostřejší Black/Bold TrueType font (Segoe UI Black / Arial Black) pro 1:1 rasterizaci loga."""
    win_dir = os.environ.get("WINDIR", r"C:\Windows")
    for fp in (
        os.path.join(win_dir, "Fonts", "seguibl.ttf"),
        os.path.join(win_dir, "Fonts", "ariblk.ttf"),
    ):
        if os.path.isfile(fp):
            return fp
    return _find_tray_bold_font_path()


# ------------------------------------------------------------------------------
# IKONY WINDOWS WORKERU (v2.11.4H)
#  A) Tray / Taskbar 32–256 px: tmavý čtverec #091322 s tyrkysovým rámečkem #22d3ee + "HBW",
#     při práci plný stavový čtverec s vystředěným číslem (1–9 velké, 10–99 střední, >99 "∞").
#  B) Titulek okna 16–24 px: čtvereček s tyrkysovým rámečkem a stavovou diodou (BEZ textu).
#  C) Logo 12-pinového čipu HBW v hlavičce okna (42×42 px): celočíselná geometrie + ostrý font až po zmenšení.
# ------------------------------------------------------------------------------
TRAY_IDLE_BODY_HEX = "#091322"
TRAY_IDLE_RIM_HEX = "#22d3ee"
TRAY_IDLE_TEXT_HEX = "#38f29b"
_BRAND_BG_HEXES = ("#0b1626", "#0e223d", "#0f213a", "#091322")


def tray_symbol_for_count(count: int) -> str:
    """Počet úloh -> symbol ikony: 1–99 číslo, > 99 symbol nekonečna ∞."""
    try:
        n = int(count)
    except Exception:
        n = 0
    if n <= 0:
        return ""
    return str(n) if n <= 99 else "∞"


def lemniscate_points(cx: float, cy: float, half_w: float, half_h: float, n: int = 120) -> list:
    """Symetrická ležatá osmička (Bernoulliho lemniskáta) vystředěná přesně v (cx, cy)."""
    pts = []
    for i in range(n + 1):
        t = 2.0 * math.pi * i / n
        s, c = math.sin(t), math.cos(t)
        den = 1.0 + s * s
        x = c / den                    # <-1, 1>
        y = (s * c / den) / 0.353553   # <-1, 1>
        pts.append((cx + x * half_w, cy + y * half_h))
    return pts


def _draw_vector_tray_symbol_pil(draw, sym: str, w: int, color_hex: str) -> bool:
    """Draws special status glyphs (pause bars, checkmark, arrow, exclamation, infinity) crisply at 4x."""
    if sym == "∞":
        sw = max(4, int(round(w * 0.105)))
        pts = lemniscate_points(w * 0.5, w * 0.5, w * 0.30, w * 0.155)
        # Uzavřená křivka: přesah o pár bodů + kulaté zakončení, aby v místě švu nevznikl zářez.
        draw.line(pts + pts[1:4], fill=color_hex, width=sw, joint="curve")
        r = sw * 0.5
        for px, py in (pts[0], pts[len(pts) // 2]):
            draw.ellipse([px - r, py - r, px + r, py + r], fill=color_hex)
        return True
    if sym == "I":  # Pause bars (||)
        bw = w * 0.14
        bh = w * 0.48
        gap = w * 0.13
        cy = w * 0.5
        cx = w * 0.5
        rx = max(2, int(round(w * 0.03)))
        draw.rounded_rectangle([cx - gap * 0.5 - bw, cy - bh * 0.5, cx - gap * 0.5, cy + bh * 0.5], radius=rx, fill=color_hex)
        draw.rounded_rectangle([cx + gap * 0.5, cy - bh * 0.5, cx + gap * 0.5 + bw, cy + bh * 0.5], radius=rx, fill=color_hex)
        return True
    if sym == "!":
        bw = w * 0.15
        cx = w * 0.5
        draw.rounded_rectangle([cx - bw * 0.5, w * 0.21, cx + bw * 0.5, w * 0.58], radius=max(2, int(bw * 0.45)), fill=color_hex)
        dr = w * 0.088
        draw.ellipse([cx - dr, w * 0.66, cx + dr, w * 0.66 + 2 * dr], fill=color_hex)
        return True
    if sym == "✓":
        sw = max(4, int(round(w * 0.115)))
        pts = [(w * 0.24, w * 0.53), (w * 0.43, w * 0.71), (w * 0.76, w * 0.30)]
        draw.line(pts, fill=color_hex, width=sw, joint="curve")
        r = sw * 0.5
        for px, py in pts:
            draw.ellipse([px - r, py - r, px + r, py + r], fill=color_hex)
        return True
    if sym in ("↓", "↑"):
        sw = max(4, int(round(w * 0.11)))
        cx = w * 0.5
        y_top, y_bot = w * 0.22, w * 0.76
        draw.line([(cx, y_top), (cx, y_bot)], fill=color_hex, width=sw)
        if sym == "↓":
            head = [(w * 0.26, w * 0.53), (cx, y_bot), (w * 0.74, w * 0.53)]
        else:
            head = [(w * 0.26, w * 0.45), (cx, y_top), (w * 0.74, w * 0.45)]
        draw.line(head, fill=color_hex, width=sw, joint="curve")
        r = sw * 0.48
        for px, py in (head[0], head[1], head[2], (cx, y_top if sym == "↓" else y_bot)):
            draw.ellipse([px - r, py - r, px + r, py + r], fill=color_hex)
        return True
    return False


def _is_idle_brand_icon(bg_hex: str, border_hex: str, sym_u: str):
    is_brand = (bg_hex.strip().lower() in _BRAND_BG_HEXES and sym_u in ("HBW", "HBM"))
    is_idle = is_brand and border_hex.strip().lower() in ("#22d3ee", "#19e0c8", "#0b1626", "#0f213a", "#091322")
    return is_brand, is_idle


def _rgba_to_bgra(rgba: bytes) -> bytes:
    bgra = bytearray(len(rgba))
    bgra[0::4] = rgba[2::4]
    bgra[1::4] = rgba[1::4]
    bgra[2::4] = rgba[0::4]
    bgra[3::4] = rgba[3::4]
    return bytes(bgra)


def _render_tray_icon_pil(bg_hex: str, border_hex: str, sym_clean: str, size: int):
    import io
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_tray_bold_font_path()
    scale = 4
    w = size * scale
    radius = max(4, int(round(w * 0.20)))
    sym_u = sym_clean.upper()
    is_brand, is_idle_brand = _is_idle_brand_icon(bg_hex, border_hex, sym_u)

    canvas = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    if is_idle_brand:
        # Jasný tyrkysový obvodový rámeček (~8 % šířky), aby ikona nezanikala na tmavé liště Windows.
        stroke = max(3, int(round(w * 0.08)))
        d.rounded_rectangle([0, 0, w - 1, w - 1], radius=radius, fill=TRAY_IDLE_BODY_HEX,
                            outline=TRAY_IDLE_RIM_HEX, width=stroke)
        text_color = TRAY_IDLE_TEXT_HEX
    else:
        stroke = max(3, int(round(w * 0.085)))
        d.rounded_rectangle([0, 0, w - 1, w - 1], radius=radius, fill=bg_hex, outline=border_hex, width=stroke)
        text_color = "#ffffff"

    if not _draw_vector_tray_symbol_pil(d, sym_clean, w, text_color):
        if not font_path:
            return None
        label = sym_u[:3] if is_brand else sym_clean[:3]
        if is_brand:
            tw_f, th_f = 0.70, 0.46
        elif len(label) == 1:
            tw_f, th_f = 0.60, 0.62
        elif len(label) == 2:
            tw_f, th_f = 0.70, 0.54
        else:
            tw_f, th_f = 0.72, 0.42
        target_w = int(round(w * tw_f))
        target_h = int(round(w * th_f))
        lo, hi = 10, int(w * 0.95)
        best_font = ImageFont.truetype(font_path, lo)
        while lo <= hi:
            mid = (lo + hi) // 2
            tf = ImageFont.truetype(font_path, mid)
            bb = d.textbbox((0, 0), label, font=tf)
            if (bb[2] - bb[0]) <= target_w and (bb[3] - bb[1]) <= target_h:
                best_font = tf
                lo = mid + 1
            else:
                hi = mid - 1
        # Přesné geometrické vystředění podle skutečného bboxu glyfů (stejná mezera nahoře i dole).
        bbox = d.textbbox((0, 0), label, font=best_font)
        tx = (w - (bbox[2] - bbox[0])) / 2.0 - bbox[0]
        ty = (w - (bbox[3] - bbox[1])) / 2.0 - bbox[1]
        d.text((tx, ty), label, font=best_font, fill=text_color)

    out = canvas.resize((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return _rgba_to_bgra(out.tobytes("raw", "RGBA")), base64.b64encode(buf.getvalue()).decode("ascii")


def _dist_point_to_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    if denom <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _lemniscate_glyph_segments(n: int = 40) -> list:
    pts = lemniscate_points(0.5, 0.5, 0.46, 0.30, n)
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


_VECTOR_STROKE_GLYPHS = {
    "H": [((0.15, 0.15), (0.15, 0.85)), ((0.85, 0.15), (0.85, 0.85)), ((0.15, 0.50), (0.85, 0.50))],
    "B": [((0.15, 0.15), (0.15, 0.85)), ((0.15, 0.15), (0.70, 0.15)), ((0.70, 0.15), (0.82, 0.32)),
          ((0.82, 0.32), (0.68, 0.49)), ((0.15, 0.50), (0.72, 0.50)), ((0.72, 0.50), (0.86, 0.68)),
          ((0.86, 0.68), (0.72, 0.85)), ((0.15, 0.85), (0.72, 0.85))],
    "W": [((0.08, 0.15), (0.28, 0.85)), ((0.28, 0.85), (0.50, 0.35)), ((0.50, 0.35), (0.72, 0.85)), ((0.72, 0.85), (0.92, 0.15))],
    "M": [((0.12, 0.85), (0.12, 0.15)), ((0.12, 0.15), (0.50, 0.62)), ((0.50, 0.62), (0.88, 0.15)), ((0.88, 0.15), (0.88, 0.85))],
    "0": [((0.22, 0.15), (0.78, 0.15)), ((0.78, 0.15), (0.78, 0.85)), ((0.78, 0.85), (0.22, 0.85)), ((0.22, 0.85), (0.22, 0.15))],
    "1": [((0.30, 0.28), (0.55, 0.15)), ((0.55, 0.15), (0.55, 0.85)), ((0.28, 0.85), (0.80, 0.85))],
    "2": [((0.20, 0.28), (0.48, 0.15)), ((0.48, 0.15), (0.78, 0.28)), ((0.78, 0.28), (0.20, 0.85)), ((0.20, 0.85), (0.82, 0.85))],
    "3": [((0.22, 0.15), (0.76, 0.15)), ((0.76, 0.15), (0.76, 0.85)), ((0.40, 0.50), (0.76, 0.50)), ((0.76, 0.85), (0.22, 0.85))],
    "4": [((0.68, 0.85), (0.68, 0.15)), ((0.68, 0.15), (0.18, 0.60)), ((0.18, 0.60), (0.84, 0.60))],
    "5": [((0.78, 0.15), (0.22, 0.15)), ((0.22, 0.15), (0.22, 0.48)), ((0.22, 0.48), (0.78, 0.56)), ((0.78, 0.56), (0.78, 0.82)), ((0.78, 0.82), (0.20, 0.85))],
    "6": [((0.72, 0.15), (0.24, 0.45)), ((0.24, 0.45), (0.24, 0.84)), ((0.24, 0.84), (0.78, 0.84)), ((0.78, 0.84), (0.78, 0.52)), ((0.78, 0.52), (0.24, 0.52))],
    "7": [((0.18, 0.15), (0.82, 0.15)), ((0.82, 0.15), (0.38, 0.85))],
    "8": [((0.24, 0.15), (0.76, 0.15)), ((0.76, 0.15), (0.24, 0.50)), ((0.24, 0.50), (0.76, 0.85)), ((0.76, 0.85), (0.24, 0.85)), ((0.24, 0.85), (0.76, 0.50)), ((0.76, 0.50), (0.24, 0.15))],
    "9": [((0.76, 0.48), (0.24, 0.48)), ((0.24, 0.48), (0.24, 0.15)), ((0.24, 0.15), (0.76, 0.15)), ((0.76, 0.15), (0.76, 0.62)), ((0.76, 0.62), (0.30, 0.85))],
    "✓": [((0.20, 0.55), (0.42, 0.78)), ((0.42, 0.78), (0.82, 0.22))],
    "!": [((0.50, 0.16), (0.50, 0.56)), ((0.50, 0.78), (0.50, 0.80))],
    "I": [((0.32, 0.22), (0.32, 0.78)), ((0.68, 0.22), (0.68, 0.78))],
    "↓": [((0.50, 0.18), (0.50, 0.80)), ((0.24, 0.55), (0.50, 0.80)), ((0.76, 0.55), (0.50, 0.80))],
    "↑": [((0.50, 0.20), (0.50, 0.82)), ((0.24, 0.45), (0.50, 0.20)), ((0.76, 0.45), (0.50, 0.20))],
    "+": [((0.50, 0.20), (0.50, 0.80)), ((0.20, 0.50), (0.80, 0.50))],
    "∞": _lemniscate_glyph_segments(),
}


def _encode_rgba_rows_png(size: int, png_rows: bytes) -> str:
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png_bin = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
               + _png_chunk(b"IDAT", zlib.compress(bytes(png_rows), 9)) + _png_chunk(b"IEND", b""))
    return base64.b64encode(png_bin).decode("ascii")


def render_tray_status_icon(bg_hex: str, border_hex: str, symbol_str: str, size: int = 32):
    """
    Ikona tray / taskbaru: v klidu tmavý čtverec s jasným tyrkysovým rámečkem a vystředěným "HBW",
    při práci plný stavový čtverec s vystředěným číslem (1–99) nebo symbolem "∞" (> 99).
    Vrací (BGRA bytes pro Win32 HICON, PNG base64 pro Tk iconphoto). Funguje i bez Pillow.
    """
    sym_clean = str(symbol_str or "HBW").strip() or "HBW"
    if sym_clean.isdigit() and int(sym_clean) > 99:
        sym_clean = "∞"
    if sym_clean == "HBW":
        # klid / start: místo čtverce „HBW“ logo E1 (Worker); stavové ikony s číslem zůstávají beze změny
        try:
            img = _render_e1_logo_image("W", size)
            if img is not None:
                import io
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                return _rgba_to_bgra(img.tobytes("raw", "RGBA")), base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            pass
    try:
        pil_res = _render_tray_icon_pil(bg_hex, border_hex, sym_clean, size)
        if pil_res is not None:
            return pil_res
    except Exception:
        pass

    sym_u = sym_clean.upper()
    is_brand, is_idle_brand = _is_idle_brand_icon(bg_hex, border_hex, sym_u)
    if is_idle_brand:
        br, bg, bb = _hex_to_rgb(TRAY_IDLE_BODY_HEX)
        er, eg, eb = _hex_to_rgb(TRAY_IDLE_RIM_HEX)
        tr, tg, tb = _hex_to_rgb(TRAY_IDLE_TEXT_HEX)
        rim_w = max(1.0, size * 0.08)
    else:
        br, bg, bb = _hex_to_rgb(bg_hex)
        er, eg, eb = _hex_to_rgb(border_hex)
        tr, tg, tb = (56, 242, 155) if is_brand else (255, 255, 255)
        rim_w = max(1.2, size * 0.085)
    rad = max(3.0, size * 0.20)

    chars = list(sym_u[:3] if is_brand else sym_clean[:3])
    n_ch = max(1, len(chars))
    tot_w = size * (0.70 if n_ch >= 3 else (0.66 if n_ch == 2 else 0.56))
    cell_w = tot_w / n_ch
    gap = cell_w * 0.14 if n_ch > 1 else 0.0
    gw = cell_w - gap
    gh = size * (0.42 if n_ch >= 3 else (0.50 if n_ch == 2 else 0.58))
    if chars == ["∞"]:
        tot_w, gw, gh = size * 0.66, size * 0.66, size * 0.40
        cell_w = gw
    ox0 = (size - tot_w + gap) * 0.5
    oy0 = (size - gh) * 0.5
    stroke_r = max(0.85, min(gw, gh) * 0.12)

    segs_abs = []
    for idx, ch in enumerate(chars):
        gx = ox0 + idx * cell_w
        for (ax, ay), (bx, by) in _VECTOR_STROKE_GLYPHS.get(ch, _VECTOR_STROKE_GLYPHS["!"]):
            segs_abs.append((gx + ax * gw, oy0 + ay * gh, gx + bx * gw, oy0 + by * gh))

    def _in_text(fx: float, fy: float) -> bool:
        for ax, ay, bx, by in segs_abs:
            if _dist_point_to_seg(fx, fy, ax, ay, bx, by) <= stroke_r:
                return True
        return False

    sub = 4
    inv_n = 1.0 / float(sub * sub)
    inner_box = size * 0.5 - rad
    bgra_bytes = bytearray()
    png_rows = bytearray()
    for y in range(size):
        png_rows.append(0)
        for x in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(sub):
                fy = y + (sy + 0.5) / sub
                dy = max(0.0, abs(fy - size * 0.5) - inner_box)
                for sx in range(sub):
                    fx = x + (sx + 0.5) / sub
                    dx = max(0.0, abs(fx - size * 0.5) - inner_box)
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist > rad:
                        continue
                    acc_a += 1.0
                    if _in_text(fx, fy):
                        acc_r += tr; acc_g += tg; acc_b += tb
                    elif rim_w > 0.0 and dist >= rad - rim_w:
                        acc_r += er; acc_g += eg; acc_b += eb
                    else:
                        acc_r += br; acc_g += bg; acc_b += bb
            cov = acc_a * inv_n
            if cov <= 0.0:
                bgra_bytes.extend((0, 0, 0, 0))
                png_rows.extend((0, 0, 0, 0))
            else:
                r = max(0, min(255, int(round(acc_r / acc_a))))
                g = max(0, min(255, int(round(acc_g / acc_a))))
                b = max(0, min(255, int(round(acc_b / acc_a))))
                a = max(0, min(255, int(round(cov * 255.0))))
                bgra_bytes.extend((b, g, r, a))
                png_rows.extend((r, g, b, a))
    return bytes(bgra_bytes), _encode_rgba_rows_png(size, png_rows)


TITLE_DOT_COLORS = {"ok": "#10b981", "paused": "#f59e0b", "offline": "#ef4444"}


def render_title_dot_icon(dot_hex: str = "#10b981", size: int = 16):
    """
    Malá ikona titulku okna (16–24 px): tmavý zaoblený čtvereček s tyrkysovým rámečkem #22d3ee
    a jasnou stavovou diodou přesně uprostřed (s jemným prstencem) – BEZ textu, aby byla čitelná.
    Vrací (BGRA bytes, PNG base64). Čistý Python (bez Pillow), 4×4 supersampling.
    """
    size = max(8, int(size))
    body = _hex_to_rgb(TRAY_IDLE_BODY_HEX)
    rim = _hex_to_rgb(TRAY_IDLE_RIM_HEX)
    dot = _hex_to_rgb(dot_hex or "#10b981")
    glow = tuple(int(round(dot[i] * 0.42 + body[i] * 0.58)) for i in range(3))
    hl = tuple(min(255, int(round(dot[i] * 0.55 + 255 * 0.45))) for i in range(3))
    rad = size * 0.24
    rim_w = max(1.0, size * 0.115)
    dot_r = size * 0.205
    ring_r = size * 0.305
    c = size * 0.5
    inner_box = c - rad
    sub = 4
    inv_n = 1.0 / float(sub * sub)
    bgra = bytearray()
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(sub):
                fy = y + (sy + 0.5) / sub
                dy = max(0.0, abs(fy - c) - inner_box)
                for sx in range(sub):
                    fx = x + (sx + 0.5) / sub
                    dx = max(0.0, abs(fx - c) - inner_box)
                    dist = math.hypot(dx, dy)
                    if dist > rad:
                        continue
                    aa += 1.0
                    dc = math.hypot(fx - c, fy - c)
                    hc = math.hypot(fx - (c - dot_r * 0.35), fy - (c - dot_r * 0.35))
                    if dist >= rad - rim_w:
                        col = rim
                    elif dc <= dot_r:
                        col = hl if hc <= dot_r * 0.38 else dot
                    elif dc <= ring_r:
                        col = glow
                    else:
                        col = body
                    ar += col[0]; ag += col[1]; ab += col[2]
            cov = aa * inv_n
            if cov <= 0.0:
                bgra.extend((0, 0, 0, 0))
                rows.extend((0, 0, 0, 0))
            else:
                r = int(round(ar / aa)); g = int(round(ag / aa)); b = int(round(ab / aa))
                a = max(0, min(255, int(round(cov * 255.0))))
                bgra.extend((b, g, r, a))
                rows.extend((r, g, b, a))
    return bytes(bgra), _encode_rgba_rows_png(size, rows)


def parse_mlz_svg_document(svg_text: str) -> dict:
    """Parsuje jednoduché logo SVG (viewBox, lineární gradienty, <rect>, <path> jen s M/L/Z)."""
    vb = re.search(r'viewBox="([\d.\-\s]+)"', svg_text or "")
    vbox = tuple(float(v) for v in vb.group(1).split()) if vb else (0.0, 0.0, 1024.0, 1024.0)
    grads = {}
    for gm in re.finditer(r'<linearGradient\s+id="([^"]+)"[^>]*>(.*?)</linearGradient>', svg_text or "", re.S):
        stops = []
        for sm in re.finditer(r'<stop\s+offset="([\d.]+)%?"\s+stop-color="(#[0-9a-fA-F]{6})"', gm.group(2)):
            off = float(sm.group(1))
            stops.append((off / 100.0 if off > 1.0 else off, sm.group(2)))
        if stops:
            grads[gm.group(1)] = stops
    elems = []
    for em in re.finditer(r'<(rect|path)\b([^>]*)/?>', svg_text or ""):
        tag, attrs = em.group(1), em.group(2)
        fill_m = re.search(r'fill="([^"]+)"', attrs)
        fill = fill_m.group(1) if fill_m else "#000000"
        if tag == "rect":
            g = {k: float(v) for k, v in re.findall(r'\b(x|y|width|height|rx)="([\d.\-]+)"', attrs)}
            elems.append({"type": "rect", "fill": fill, **g})
        else:
            d_m = re.search(r'\bd="([^"]+)"', attrs)
            if not d_m:
                continue
            subpaths, cur = [], []
            for cmd, xs, ys in re.findall(r'([MLZ])\s*(?:([\d.\-]+)[\s,]+([\d.\-]+))?', d_m.group(1)):
                if cmd == "M":
                    if len(cur) >= 3:
                        subpaths.append(cur)
                    cur = [(float(xs), float(ys))]
                elif cmd == "L":
                    cur.append((float(xs), float(ys)))
                elif cmd == "Z":
                    if len(cur) >= 3:
                        subpaths.append(cur)
                    cur = []
            if len(cur) >= 3:
                subpaths.append(cur)
            elems.append({"type": "path", "fill": fill, "subpaths": subpaths})
    return {"viewbox": vbox, "gradients": grads, "elements": elems}


def _gradient_rgba_image(stops: list, px: int):
    from PIL import Image
    small = 64
    img = Image.new("RGBA", (small, small))
    cols = [(o, _hex_to_rgb(h)) for o, h in sorted(stops)]
    data = []
    for yy in range(small):
        for xx in range(small):
            t = ((xx + yy) / (2.0 * (small - 1)) - 0.15) / 0.70   # x1/y1 = 15 %, x2/y2 = 85 %
            t = max(0.0, min(1.0, t))
            col = cols[-1][1]
            for i in range(len(cols) - 1):
                o0, c0 = cols[i]
                o1, c1 = cols[i + 1]
                if t <= o1:
                    k = 0.0 if o1 <= o0 else (t - o0) / (o1 - o0)
                    k = max(0.0, min(1.0, k))
                    col = tuple(int(round(c0[j] + (c1[j] - c0[j]) * k)) for j in range(3))
                    break
            data.append((col[0], col[1], col[2], 255))
    img.putdata(data)
    return img.resize((px, px), Image.Resampling.BILINEAR)


def render_procedural_chip_logo_png_b64(label: str = "HBW", size: int = 42):
    """
    Ostrý 12-pinový čip HBW (3 ostré piny na každé straně = 12 pinů) v přesných celočíselných
    souřadnicích bez půlpixelového rozmazání. Text se renderuje až PO zmenšení na cílové 42×42 px
    pomocí ostrého TrueType systémového fontu (Segoe UI Black / Arial Black).
    """
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont

        s = max(24, int(size))
        scale = 4
        big = s * scale

        # Celočíselná geometrie pro cílovou velikost s×s (při 42×42: tělo 6..35 = 30×30, jádro 8..33 = 26×26)
        pad = max(3, int(round(s * 0.143)))
        bx0, by0, bx1, by1 = pad, pad, s - 1 - pad, s - 1 - pad
        rim = max(2, int(round(s * 0.05)))
        cx0, cy0, cx1, cy1 = bx0 + rim, by0 + rim, bx1 - rim, by1 - rim

        # 1) Zaoblené tělo čipu + tmavé jádro na přesných 4× hranicích -> rovné hrany bez rozmazání
        body_big = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        grad_big = _gradient_rgba_image([(0.0, "#38F29B"), (0.5, "#19E0C8"), (1.0, "#16A8E8")], big)
        mask_big = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask_big).rounded_rectangle(
            [bx0 * scale, by0 * scale, (bx1 + 1) * scale - 1, (by1 + 1) * scale - 1],
            radius=int(round(3.5 * scale)),
            fill=255,
        )
        body_big.paste(grad_big, (0, 0), mask_big)
        ImageDraw.Draw(body_big).rounded_rectangle(
            [cx0 * scale, cy0 * scale, (cx1 + 1) * scale - 1, (cy1 + 1) * scale - 1],
            radius=int(round(2.0 * scale)),
            fill="#0b1626",
        )
        out = body_big.resize((s, s), Image.Resampling.LANCZOS)

        # 2) Ostré celočíselné 12 piny (3 na každé ze 4 stran) přímo v cílovém rozlišení 1×
        grad_1x = _gradient_rgba_image([(0.0, "#38F29B"), (0.5, "#19E0C8"), (1.0, "#16A8E8")], s)
        pw = 4 if s >= 38 else max(2, int(round(s * 0.09)))
        center_p = (s - pw) // 2
        span = (bx1 - bx0 + 1) - 3 * pw
        margin = max(2, span // 4)
        gap = max(2, (span - 2 * margin) // 2)
        p_starts = [center_p - pw - gap, center_p, center_p + pw + gap]

        pins_mask = Image.new("L", (s, s), 0)
        pd = ImageDraw.Draw(pins_mask)
        for p0 in p_starts:
            p1 = p0 + pw - 1
            pd.rectangle([p0, 1, p1, bx0 - 1], fill=255)          # 3 piny nahoře
            pd.rectangle([p0, bx1 + 1, p1, s - 2], fill=255)      # 3 piny dole
            pd.rectangle([1, p0, bx0 - 1, p1], fill=255)          # 3 piny vlevo
            pd.rectangle([bx1 + 1, p0, s - 2, p1], fill=255)      # 3 piny vpravo
        out.paste(grad_1x, (0, 0), pins_mask)

        # 3) Text HBW vyrenderovaný až PO zmenšení přímo v nativních s×s (42×42) pomocí Black/Bold fontu
        fp = _find_chip_black_font_path()
        lbl = str(label or "HBW").strip().upper()[:3]
        core_w = cx1 - cx0 + 1
        core_h = cy1 - cy0 + 1
        if fp and lbl:
            txt_mask = Image.new("L", (s, s), 0)
            td = ImageDraw.Draw(txt_mask)
            lo, hi, best = 6, max(12, s), ImageFont.truetype(fp, 8)
            max_tw = core_w - 3
            max_th = core_h - 6
            while lo <= hi:
                mid = (lo + hi) // 2
                tf = ImageFont.truetype(fp, mid)
                bb = td.textbbox((0, 0), lbl, font=tf)
                if (bb[2] - bb[0]) <= max_tw and (bb[3] - bb[1]) <= max_th:
                    best, lo = tf, mid + 1
                else:
                    hi = mid - 1
            bb = td.textbbox((0, 0), lbl, font=best)
            tx = int(round((s - (bb[2] - bb[0])) * 0.5)) - bb[0]
            ty = int(round((s - (bb[3] - bb[1])) * 0.5)) - bb[1]
            td.text((tx, ty), lbl, font=best, fill=255)
            txt_grad = _gradient_rgba_image([(0.0, "#5efce0"), (0.5, "#22e3d0"), (1.0, "#38bdf8")], s)
            txt_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            txt_layer.paste(txt_grad, (0, 0), txt_mask)
            out = Image.alpha_composite(out, txt_layer)

        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def render_e1_logo_png_b64(role: str = "W", size: int = 42):
    """Logo E1 jako PNG base64 (Tk PhotoImage). Viz _render_e1_logo_image."""
    try:
        import io
        img = _render_e1_logo_image(role, size)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _render_e1_logo_image(role: str = "W", size: int = 42):
    """
    Logo E1 (uzly s čipem) vykreslené přes PIL – stejná kresba jako static/hbw-gradient.svg (viewBox 64×64).
    role: "W" = Worker (tmavý střed, svítí pravý dolní uzel), "M" = Manager (svítí střed), "C" = Cluster (svítí vše).
    Kreslí se 8× větší a pak zmenší (hladké hrany); pod 28 px bez nožiček čipu (čitelnost).
    """
    try:
        from PIL import Image, ImageDraw

        s = max(16, int(size))
        k = 8
        big = s * k
        u = big / 64.0
        role = (role or "W").upper()
        mask = Image.new("L", (big, big), 0)
        d = ImageDraw.Draw(mask)

        def P(v):
            return v * u

        def line(x1, y1, x2, y2, w):
            ww = max(1, int(round(w * u)))
            d.line([(P(x1), P(y1)), (P(x2), P(y2))], fill=255, width=ww)
            r = w * u / 2.0
            for x, y in ((x1, y1), (x2, y2)):
                d.ellipse([P(x) - r, P(y) - r, P(x) + r, P(y) + r], fill=255)

        def circle(cx, cy, r, w, filled):
            rr = r + w / 2.0
            d.ellipse([P(cx - rr), P(cy - rr), P(cx + rr), P(cy + rr)], fill=255)
            if not filled:
                ri = r - w / 2.0
                d.ellipse([P(cx - ri), P(cy - ri), P(cx + ri), P(cy + ri)], fill=0)

        corners = [(11, 11), (53, 11), (11, 53), (53, 53)]
        for x, y in corners:
            line(22 if x < 32 else 42, 22 if y < 32 else 42, x + (5 if x < 32 else -5), y + (5 if y < 32 else -5), 3.4)
        for i, (x, y) in enumerate(corners):
            hot = role == "C" or (role == "W" and i == 3)
            circle(x, y, 7 if (role == "W" and i == 3) else 6, 3.4, hot)
        if s >= 28:
            for p in (28, 36):
                line(p, 15, p, 19, 3)
                line(p, 45, p, 49, 3)
                line(15, p, 19, p, 3)
                line(45, p, 49, p, 3)
        half = 3.4 / 2.0
        if role == "W":
            d.rounded_rectangle([P(19 - half), P(19 - half), P(45 + half), P(45 + half)], radius=P(6 + half), fill=255)
            d.rounded_rectangle([P(19 + half), P(19 + half), P(45 - half), P(45 - half)], radius=P(max(0.5, 6 - half)), fill=0)
            d.rounded_rectangle([P(26), P(26), P(38), P(38)], radius=P(2.6), fill=255)
        else:
            d.rounded_rectangle([P(19 - half), P(19 - half), P(45 + half), P(45 + half)], radius=P(6 + half), fill=255)
            d.rounded_rectangle([P(26), P(26), P(38), P(38)], radius=P(2.6), fill=0)

        grad = _gradient_rgba_image([(0.0, "#38F29B"), (0.5, "#19E0C8"), (1.0, "#16A8E8")], big)
        out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        out.paste(grad, (0, 0), mask)
        return out.resize((s, s), Image.Resampling.LANCZOS)
    except Exception:
        return None


def render_rgba_png_bytes(kind: str, fg_hex: str = "#ffffff", size: int = 18) -> str:
    fr, fg, fb = _hex_to_rgb(fg_hex)
    sub = 4
    inv_sub2 = 1.0 / float(sub * sub)
    half = size / 2.0

    raw_rows = bytearray()
    for y in range(size):
        raw_rows.append(0)
        for x in range(size):
            hits = 0
            for sy in range(sub):
                v = ((y + (sy + 0.5) / sub) - half) / half
                for sx in range(sub):
                    u = ((x + (sx + 0.5) / sub) - half) / half
                    if _icon_shape_hit(kind, u, v):
                        hits += 1
            if hits == 0:
                raw_rows.extend((0, 0, 0, 0))
            else:
                alpha = int(round((hits * inv_sub2) * 255.0))
                raw_rows.extend((fr, fg, fb, alpha))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), 9)
    png_bin = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(png_bin).decode("ascii")


# v2.11.3: Docker self-recreate logika byla přesunuta do sdíleného modulu docker_selfupdate.py
# (používá ho Manager i Linux Worker). Windows Worker se aktualizuje přes API Managera.


# ------------------------------------------------------------------------------
# O4: ČEKÁNÍ PO NEÚSPĚŠNÉ AKTUALIZACI (Windows i Linux worker)
# ------------------------------------------------------------------------------
# 1. neúspěch → krátká pauza (podle druhu chyby), 2. neúspěch stejné cílové verze → 6 h,
# vrácení nové verze, která nenaběhla (O1) → hned 6 h. „Aktualizovat teď“ z Manageru čekání zruší.
UPDATE_RETRY_LONG_S = 6 * 3600


def update_retry_note(state: dict, target: str, reason: str, short_s: float, rollback: bool = False,
                      detail: str = "", now: float = None) -> float:
    """Zapíše neúspěch do state a vrátí čas (epoch), do kdy se další pokus odkládá."""
    now = time.time() if now is None else float(now)
    tgt = str(target or "")
    if state.get("target") == tgt:
        state["fails"] = int(state.get("fails") or 0) + 1
    else:
        state["target"], state["fails"] = tgt, 1
    if rollback:
        state["fails"] = max(2, int(state["fails"]))
    state["reason"] = str(reason or "")
    state["detail"] = str(detail or "")[:300]
    state["until"] = now + (UPDATE_RETRY_LONG_S if (rollback or state["fails"] >= 2) else float(short_s))
    return state["until"]


def update_retry_reset(state: dict):
    state.clear()


def update_retry_view(state: dict, channel: str = "", now: float = None) -> dict:
    """Pro Manager (telemetrie diag.ota): proč a do kdy worker s aktualizací čeká, z jakého kanálu běží."""
    now = time.time() if now is None else float(now)
    until = float(state.get("until") or 0)
    out = {"channel": str(channel or "")}
    if until > now:
        out.update({"blocked_until": int(until), "reason": str(state.get("reason") or ""),
                    "detail": str(state.get("detail") or ""), "fails": int(state.get("fails") or 0),
                    "target": str(state.get("target") or "")})
    return out


def fmt_retry_time(ts: float) -> str:
    return time.strftime("%d.%m. %H:%M", time.localtime(float(ts or 0)))


# ------------------------------------------------------------------------------
# O1: WINDOWS OTA S ZÁLOHOU A NÁVRATEM
# ------------------------------------------------------------------------------
# 1. Soubory nové verze se stáhnou podle SHA-256 manifestu (/api/update/manifest?target=windows),
#    ověří se VERSION a překlad a uloží do app/ota_stage/.
# 2. Nová verze se tam zkusmo načte v samostatném procesu (worker.pyw --selftest) – ne jen přeloží.
# 3. Teprve pak vzniknou *.tmp (nové) a *.prev (záloha současných) a záznam update_attempt.json
#    (z verze → na verzi + otisky obou sad souborů). update_worker.bat soubory prohodí, spustí novou
#    verzi a 2 min čeká na ota_ok.txt („naběhl jsem“); jinak vrátí *.prev a spustí starou verzi.
# 4. Výpadek proudu uprostřed prohazování pozná start podle otisků v záznamu a vrátí zálohy.
OTA_FILES_WINDOWS = ("worker_core.py", "worker.pyw", "locales.js", "VERSION")
OTA_STAGE_DIR = "ota_stage"
OTA_ATTEMPT_FILE = "update_attempt.json"
OTA_OK_FILE = "ota_ok.txt"
OTA_PID_FILE = "ota_pid.txt"
OTA_ATTEMPT_KEEP_S = 300


def file_sha256(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError:
        return ""


def read_ota_attempt(app_dir: str) -> dict:
    try:
        with open(os.path.join(app_dir, OTA_ATTEMPT_FILE), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def mark_ota_attempt_settled(app_dir: str, rec: dict):
    """Po prvním normálním startu (nové i vrácené verze) už záznam neslouží k obnově po přerušení."""
    try:
        d = dict(rec or {})
        d["settled"] = True
        p = os.path.join(app_dir, OTA_ATTEMPT_FILE)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(p + ".tmp", p)
    except Exception:
        pass


def clear_ota_attempt(app_dir: str):
    for fn in (OTA_ATTEMPT_FILE, OTA_OK_FILE, OTA_PID_FILE):
        try:
            os.remove(os.path.join(app_dir, fn))
        except OSError:
            pass


def evaluate_ota_attempt(app_dir: str, current_version: str):
    """Po startu Windows workeru: (stav, záznam); stav = none | updated | failed | stale."""
    rec = read_ota_attempt(app_dir)
    if not rec:
        return "none", {}
    norm = lambda v: str(v or "").strip().lstrip("vV")
    cur, frm, to = norm(current_version), norm(rec.get("from")), norm(rec.get("to"))
    if to and to == cur and frm != cur:
        return "updated", rec
    if frm and frm == cur and to != cur:
        return "failed", rec
    return "stale", rec


def build_windows_update_bat(app_dir: str, script_path: str, python_exe: str, restore_state: str,
                             wait_s: int = 120) -> str:
    """Text update_worker.bat (O1). Prohodí *.tmp, spustí novou verzi a čeká až wait_s na ota_ok.txt.
    Když nová verze nenaběhne: ukončí ji (PID a název procesu z ota_pid.txt), vrátí *.prev a spustí starou.
    Bez ! a bez závorek v cestách uvnitř bloků (cmd); cesty se čtou z proměnných."""
    sd = os.path.abspath(app_dir)
    is_app = os.path.basename(sd).lower() == "app"
    script_name = os.path.basename(script_path)
    rs = re.sub(r"[^a-z]", "", str(restore_state or "").lower()) or "normal"
    loops = max(1, int(wait_s) // 2)
    names = ["worker_core.py", script_name, "locales.js", "VERSION"]
    L = ["@echo off",
         "chcp 65001 > nul",
         "setlocal DisableDelayedExpansion",
         f'set "HBW_APP={sd}"',
         f'set "HBW_PY={python_exe}"',
         f'set "HBW_SCRIPT={os.path.abspath(script_path)}"',
         'cd /d "%HBW_APP%"',
         'ping 127.0.0.1 -n 3 > nul || powershell -NoProfile -Command "Start-Sleep -Seconds 2" > nul 2>&1',
         f'if exist "%HBW_APP%\\{OTA_OK_FILE}" del /f /q "%HBW_APP%\\{OTA_OK_FILE}" > nul 2>&1',
         f'if exist "%HBW_APP%\\{OTA_PID_FILE}" del /f /q "%HBW_APP%\\{OTA_PID_FILE}" > nul 2>&1',
         "REM --- nove soubory (*.tmp) na misto; zalohy *.prev pripravil worker ---"]
    for n in names:
        L.append(f'for /l %%r in (1,1,5) do if exist "%HBW_APP%\\{n}.tmp" (move /y "%HBW_APP%\\{n}.tmp" "%HBW_APP%\\{n}" > nul 2>&1 || ping 127.0.0.1 -n 2 > nul)')
    if is_app:
        L.append('copy /y "%HBW_APP%\\VERSION" "%HBW_APP%\\..\\VERSION" > nul 2>&1')
    L += ["call :launch",
          f"REM --- O1: nova verze ma {int(wait_s)} s na zapis {OTA_OK_FILE} (nabehl jsem) ---",
          "set /a HBW_WAIT=0",
          ":wait_ok",
          f'if exist "%HBW_APP%\\{OTA_OK_FILE}" goto ok',
          f"if %HBW_WAIT% GEQ {loops} goto rollback",
          'ping 127.0.0.1 -n 3 > nul || powershell -NoProfile -Command "Start-Sleep -Seconds 2" > nul 2>&1',
          "set /a HBW_WAIT+=1",
          "goto wait_ok",
          ":ok",
          f'del /f /q "%HBW_APP%\\{OTA_OK_FILE}" > nul 2>&1',
          f'del /f /q "%HBW_APP%\\{OTA_PID_FILE}" > nul 2>&1',
          "goto done",
          ":rollback",
          f'if exist "%HBW_APP%\\{OTA_PID_FILE}" for /f "usebackq tokens=1,2" %%a in ("%HBW_APP%\\{OTA_PID_FILE}") do taskkill /f /t /fi "PID eq %%a" /fi "IMAGENAME eq %%b" > nul 2>&1',
          'ping 127.0.0.1 -n 3 > nul || powershell -NoProfile -Command "Start-Sleep -Seconds 2" > nul 2>&1']
    for n in names:
        L.append(f'if exist "%HBW_APP%\\{n}.tmp" del /f /q "%HBW_APP%\\{n}.tmp" > nul 2>&1')
        L.append(f'if exist "%HBW_APP%\\{n}.prev" copy /y "%HBW_APP%\\{n}.prev" "%HBW_APP%\\{n}" > nul')
    if is_app:
        L.append('copy /y "%HBW_APP%\\VERSION" "%HBW_APP%\\..\\VERSION" > nul 2>&1')
    L += [f'del /f /q "%HBW_APP%\\{OTA_PID_FILE}" > nul 2>&1',
          "call :launch",
          ":done",
          "endlocal",
          '(goto) 2>nul & del "%~f0"',
          ":launch",
          'if exist "%HBW_APP%\\..\\HBWorker.exe" (',
          '    cd /d "%HBW_APP%\\.."',
          f'    start "" "HBWorker.exe" --restore-state={rs}',
          ') else if exist "%HBW_APP%\\HBWorker.exe" (',
          '    cd /d "%HBW_APP%"',
          f'    start "" "HBWorker.exe" --restore-state={rs}',
          ") else (",
          '    cd /d "%HBW_APP%"',
          f'    start "" "%HBW_PY%" "%HBW_SCRIPT%" --restore-state={rs}',
          ")",
          'cd /d "%HBW_APP%"',
          "exit /b 0"]
    return "\r\n".join(L) + "\r\n"


def stage_windows_update(manager_url: str, app_dir: str, script_path: str, api_request_fn,
                         current_version: str, selftest_fn=None, log_fn=None) -> dict:
    """Připraví Windows aktualizaci (viz výše). Vrací {"ok": bool, "reason", "detail", "target"};
    reason: same | download | integrity | selftest_failed | error. Při neúspěchu se nic nepřepíše."""
    app_dir = os.path.abspath(app_dir)
    stage = os.path.join(app_dir, OTA_STAGE_DIR)
    script_name = os.path.basename(script_path)
    names = ("worker_core.py", script_name, "locales.js", "VERSION")
    tmp_paths = [os.path.join(app_dir, n + ".tmp") for n in names]
    target_ver = ""

    def _cleanup(stage_only: bool = False):
        shutil.rmtree(stage, ignore_errors=True)
        if not stage_only:
            for p in tmp_paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    try:
        rm = api_request_fn("GET", f"{manager_url}/api/update/manifest?target=windows", timeout=10)
        if rm.status_code != 200:
            if log_fn:
                log_fn(T("wc_deferring_auto_update_server", rm.status_code, "-"))
            return {"ok": False, "reason": "download", "detail": f"manifest HTTP {rm.status_code}"}
        mf = rm.json() or {}
        target_ver = str(mf.get("version") or "").strip()
        if not target_ver or target_ver == str(current_version or "").strip():
            return {"ok": False, "reason": "same", "detail": "", "target": target_ver}
        entries = {str(e.get("name")): e for e in (mf.get("files") or []) if isinstance(e, dict)}
        for req_name in ("worker_core.py", "worker.pyw", "VERSION"):
            if req_name not in entries:
                if log_fn:
                    log_fn(T("wc_ota_integrity_failed", req_name))
                return {"ok": False, "reason": "integrity", "detail": req_name, "target": target_ver}

        blobs = {}
        for name in ("worker_core.py", "worker.pyw", "VERSION", "locales.js"):
            ent = entries.get(name)
            if not ent:
                continue
            url = str(ent.get("url") or "")
            if not url.startswith("/api/update/file/"):
                raise ValueError(f"unexpected OTA url for {name}")
            r = api_request_fn("GET", f"{manager_url}{url}", timeout=30)
            if r.status_code != 200:
                if log_fn:
                    log_fn(T("wc_deferring_auto_update_server", r.status_code, name))
                return {"ok": False, "reason": "download", "detail": f"{name} HTTP {r.status_code}", "target": target_ver}
            data = r.content or b""
            if hashlib.sha256(data).hexdigest() != str(ent.get("sha256") or "").strip().lower():
                if log_fn:
                    log_fn(T("wc_ota_integrity_failed", name))
                return {"ok": False, "reason": "integrity", "detail": name, "target": target_ver}
            blobs[name] = data

        def _txt(b: bytes) -> str:
            return b.decode("utf-8-sig").lstrip("\ufeff").replace("\r\n", "\n")

        texts = {"VERSION": f"{target_ver}\n"}
        if _txt(blobs["VERSION"]).strip() != target_ver:
            if log_fn:
                log_fn(T("wc_ota_integrity_failed", "VERSION"))
            return {"ok": False, "reason": "integrity", "detail": "VERSION", "target": target_ver}
        texts["worker_core.py"] = _txt(blobs["worker_core.py"])
        texts[script_name] = _txt(blobs["worker.pyw"])
        compile(texts["worker_core.py"], "worker_core.py", "exec")
        compile(texts[script_name], script_name, "exec")
        if "locales.js" in blobs:
            l_text = _txt(blobs["locales.js"])
            if parse_locales_js_text(l_text):
                texts["locales.js"] = l_text

        # 1) nová verze do app/ota_stage a zkušební načtení v samostatném procesu
        _cleanup(stage_only=True)
        os.makedirs(stage, exist_ok=True)
        for n, t in texts.items():
            with open(os.path.join(stage, n), "w", encoding="utf-8", newline="\n") as f:
                f.write(t)
        if selftest_fn:
            st_ok, st_detail = selftest_fn(stage, target_ver)
            if not st_ok:
                _cleanup()
                return {"ok": False, "reason": "selftest_failed", "detail": str(st_detail or "")[:400], "target": target_ver}

        # 2) *.tmp (nové) + *.prev (záloha současných) + záznam o pokusu s otisky obou sad
        files = {}
        for n in texts:
            dst = os.path.join(app_dir, n)
            with open(dst + ".tmp", "w", encoding="utf-8", newline="\n") as f:
                f.write(texts[n])
            prev_sha = ""
            if os.path.isfile(dst):
                shutil.copy2(dst, dst + ".prev")
                prev_sha = file_sha256(dst + ".prev")
            files[n] = {"new": file_sha256(dst + ".tmp"), "prev": prev_sha}
        rec = {"from": str(current_version or ""), "to": target_ver, "ts": int(time.time()), "files": files}
        rp = os.path.join(app_dir, OTA_ATTEMPT_FILE)
        with open(rp + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        os.replace(rp + ".tmp", rp)
        for fn in (OTA_OK_FILE, OTA_PID_FILE):
            try:
                os.remove(os.path.join(app_dir, fn))
            except OSError:
                pass
        _cleanup(stage_only=True)
        return {"ok": True, "reason": "", "detail": "", "target": target_ver, "files": list(texts)}
    except Exception as e:
        if log_fn:
            log_fn(T("wc_auto_update_safety_guard", e))
        _cleanup()
        return {"ok": False, "reason": "error", "detail": str(e)[:300], "target": target_ver}


def sync_windows_package_files(manager_url: str, app_dir: str, api_request_fn, log_fn=None) -> int:
    """
    Aktualizace ostatních souborů Windows balíčku (ikona, .bat skripty, README, certifikát…) podle
    manifestu Manageru. Porovná SHA-256 místních souborů, stáhne jen rozdílné, ověří otisk a vymění.
    Běží nezávisle na verzi programu; config.json a program samotný nikdy nemění.
    Vrací počet změněných souborů.
    """
    app_dir = os.path.abspath(app_dir)
    install_root = os.path.dirname(app_dir) if os.path.basename(app_dir).lower() == "app" else app_dir
    try:
        rm = api_request_fn("GET", f"{manager_url}/api/update/manifest?target=windows", timeout=10)
        if rm.status_code != 200:
            return 0
        mf = rm.json() or {}
    except Exception:
        return 0

    def _dest(rel: str):
        rel = str(rel or "")
        if not rel or rel.startswith("/") or "\\" in rel or ":" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
            return None
        parts = rel.split("/")
        base = app_dir if (parts[0] == "app" and install_root == app_dir) else install_root
        if parts[0] == "app" and install_root == app_dir:
            parts = parts[1:]
        if not parts or parts[-1].lower() == "config.json":
            return None
        dst = os.path.abspath(os.path.join(base, *parts))
        if os.path.commonpath([install_root, dst]) != install_root:
            return None
        return dst

    def _sha(path: str):
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(blk)
            return h.hexdigest()
        except Exception:
            return None

    changed = 0
    for ent in (mf.get("package") or []):
        try:
            dst = _dest(ent.get("path"))
            want = str(ent.get("sha256") or "").lower()
            url = str(ent.get("url") or "")
            if not dst or not want or not url.startswith("/api/update/pkg"):
                continue
            if os.path.isfile(dst) and _sha(dst) == want:
                continue
            r = api_request_fn("GET", f"{manager_url}{url}", timeout=30)
            data = (r.content or b"") if r.status_code == 200 else None
            if data is None or hashlib.sha256(data).hexdigest() != want:
                if log_fn:
                    log_fn(T("wc_ota_integrity_failed", ent.get("path")))
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".pkgtmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            changed += 1
            if log_fn:
                log_fn(T("wc_pkg_file_updated", ent.get("path")))
        except Exception as e:
            if log_fn:
                log_fn(T("wc_pkg_file_failed", ent.get("path"), e))
    for rel in (mf.get("remove") or []):
        try:
            dst = _dest(rel)
            if dst and os.path.isfile(dst):
                os.remove(dst)
                changed += 1
                if log_fn:
                    log_fn(T("wc_pkg_file_removed", rel))
        except Exception:
            pass
    return changed


def parse_hb_eta_to_seconds(eta_raw: str) -> int:
    if not eta_raw:
        return 0
    tot = 0
    mh = re.search(r"(\d+)h", eta_raw)
    mm = re.search(r"(\d+)m", eta_raw)
    ms = re.search(r"(\d+)s", eta_raw)
    if mh:
        tot += int(mh.group(1)) * 3600
    if mm:
        tot += int(mm.group(1)) * 60
    if ms:
        tot += int(ms.group(1))
    return tot


def compute_file_md5(filepath: str):
    if not filepath or not os.path.isfile(filepath):
        return None
    try:
        st = os.stat(filepath)
        ck = (filepath, st.st_mtime, st.st_size)
        if local_md5_cache.get("k_" + filepath) == ck:
            return local_md5_cache.get("v_" + filepath)
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                blk = f.read(1024 * 1024 * 4)
                if not blk:
                    break
                h.update(blk)
        dg = h.hexdigest()
        local_md5_cache["k_" + filepath] = ck
        local_md5_cache["v_" + filepath] = dg
        return dg
    except Exception:
        return None
# ==============================================================================
# 6. ANALÝZA ZDROJE, .ISO / .MPLS HLAVIČEK, BITRATU A TITULKŮ
# ==============================================================================
def get_file_size(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        if os.path.isdir(path):
            tot = 0
            for dp, _, fns in os.walk(path):
                for f in fns:
                    fp = os.path.join(dp, f)
                    if not os.path.islink(fp):
                        tot += os.path.getsize(fp)
            return tot
    except Exception:
        pass
    return 0


def find_largest_media_file_in_dir(dir_path: str):
    best_f = None
    best_s = -1
    exts = ('.m2ts', '.mkv', '.mp4', '.m4v', '.ts', '.vob', '.avi', '.mov')
    try:
        for rd, _, fns in os.walk(dir_path):
            for f in fns:
                fl = f.lower()
                if fl == "video_ts.vob" or fl.startswith("vts_01_0."):
                    continue
                if fl.endswith(exts):
                    fp = os.path.join(rd, f)
                    try:
                        sz = os.path.getsize(fp)
                        if sz > best_s:
                            best_s = sz
                            best_f = fp
                    except Exception:
                        pass
    except Exception:
        pass
    return best_f


def parse_mpls_header_info(mpls_bytes: bytes):
    if not mpls_bytes or len(mpls_bytes) < 64 or b"MPLS" not in mpls_bytes[:8]:
        return [], 0.0
    clips = []
    total_dur_sec = 0.0
    try:
        pl_offset = struct.unpack_from(">I", mpls_bytes, 8)[0]
        if 0 < pl_offset + 10 <= len(mpls_bytes):
            num_items = struct.unpack_from(">H", mpls_bytes, pl_offset + 6)[0]
            pos = pl_offset + 10
            for _ in range(num_items):
                if pos + 22 > len(mpls_bytes):
                    break
                item_len = struct.unpack_from(">H", mpls_bytes, pos)[0]
                if item_len < 20 or pos + 2 + item_len > len(mpls_bytes):
                    break
                clip_id = mpls_bytes[pos + 2:pos + 7].decode("ascii", errors="ignore")
                codec_id = mpls_bytes[pos + 7:pos + 11]
                if clip_id.isdigit() and codec_id == b"M2TS":
                    clips.append(clip_id)
                    in_t = struct.unpack_from(">I", mpls_bytes, pos + 14)[0]
                    out_t = struct.unpack_from(">I", mpls_bytes, pos + 18)[0]
                    if out_t > in_t:
                        seg_s = (out_t - in_t) / 45000.0
                        if 0.0 < seg_s < 43200.0:
                            total_dur_sec += seg_s
                pos += 2 + item_len
    except Exception:
        pass

    if not clips:
        for c in re.findall(rb"(\d{5})M2TS", mpls_bytes):
            cs = c.decode("ascii")
            clips.append(cs)
    return clips, round(total_dur_sec, 2)


def inspect_iso_mpls_via_7z(iso_path: str, mpls_upper: str, sevenzip_bin: str = None):
    sz_b = sevenzip_bin or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if not sz_b or not os.path.isfile(iso_path):
        return 0, [], b""
    try:
        clips = []
        mb = b""
        if mpls_upper:
            rm = subprocess.run(
                [sz_b, "e", "-so", iso_path, f"-ir!*{mpls_upper}", f"-ir!*{mpls_upper.lower()}", "-xr!*BACKUP*", "-xr!*backup*"],
                capture_output=True, timeout=25, creationflags=SubprocFlags()
            )
            mb = rm.stdout or b""
            if b"MPLS" not in mb[:8]:
                rm = subprocess.run(
                    [sz_b, "e", "-so", iso_path, f"-ir!*{mpls_upper}", f"-ir!*{mpls_upper.lower()}"],
                    capture_output=True, timeout=25, creationflags=SubprocFlags()
                )
                mb = rm.stdout or b""
            if b"MPLS" in mb[:8]:
                clips, _ = parse_mpls_header_info(mb)

        rl = subprocess.run(
            [sz_b, "l", "-slt", "-sccUTF-8", iso_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, creationflags=SubprocFlags()
        )
        c_sz = {}
        cp = ""
        for ln in (rl.stdout or "").splitlines():
            ls = ln.strip()
            if ls.startswith("Path = "):
                cp = os.path.basename(ls[7:].strip().replace("\\", "/")).upper()
            elif ls.startswith("Size = ") and cp.endswith(".M2TS"):
                cid = cp[:5]
                try:
                    sv = int(ls[7:].strip())
                    if sv > 0:
                        c_sz[cid] = max(c_sz.get(cid, 0), sv)
                except Exception:
                    pass

        if clips:
            matched_sz = sum(c_sz.get(cid, 0) for cid in clips if cid in c_sz)
            if matched_sz > 0:
                return matched_sz, clips, mb
        if c_sz:
            best_cid = max(c_sz, key=lambda k: c_sz[k])
            return c_sz[best_cid], (clips if clips else [best_cid]), mb
        return 0, clips, mb
    except Exception:
        return 0, [], b""


def parse_bluray_iso_mpls_details(iso_path: str, mpls_name: str, min_valid_bytes: int = 100 * 1024 * 1024, sevenzip_bin: str = None):
    if not iso_path or not os.path.isfile(iso_path) or not iso_path.lower().endswith(".iso"):
        return 0, [], b""
    m_up = (mpls_name or "").strip().upper()
    if m_up and not m_up.endswith(".MPLS"):
        m_up = f"{int(m_up):05d}.MPLS" if m_up.isdigit() else ""

    iso_sz = os.path.getsize(iso_path)
    eff_min = max(5 * 1024 * 1024, min_valid_bytes) if m_up else (max(min_valid_bytes, 500 * 1024 * 1024) if iso_sz > 2 * (1024**3) else min_valid_bytes)

    try:
        with open(iso_path, "rb") as f:
            buf = f.read(64 * 1024 * 1024)
        if len(buf) >= 65536:
            efe_s, efe_p = {}, {}
            bl = len(buf)
            for pos in range(0, bl - 2048, 2048):
                tid = struct.unpack_from("<H", buf, pos)[0]
                if tid in (261, 266):
                    tloc = struct.unpack_from("<I", buf, pos + 12)[0]
                    ilen = struct.unpack_from("<Q", buf, pos + 56)[0]
                    if tloc > 0 and ilen > 0:
                        efe_s[tloc] = ilen
                        efe_p[tloc] = pos

            if efe_s:
                m2ts_m = {}
                m_lbn = None
                for m in re.finditer(rb"(?:\x08([0-9]{5}\.(?:M2TS|MPLS|m2ts|mpls))|\x10((?:\x00[0-9A-Za-z\.]){10}))", buf):
                    mp = m.start()
                    fn = m.group(1).decode("ascii", errors="ignore").upper() if m.group(1) else m.group(2)[1::2].decode("ascii", errors="ignore").upper()
                    fs = mp - 38
                    if fs >= 0 and buf[fs:fs + 2] == b"\x01\x01":
                        lbn = struct.unpack_from("<I", buf, fs + 24)[0]
                        if fn.endswith(".M2TS") and lbn in efe_s:
                            cid = fn[:5]
                            m2ts_m[cid] = max(m2ts_m.get(cid, 0), efe_s[lbn])
                        elif m_up and fn == m_up:
                            m_lbn = lbn

                if m2ts_m and m_lbn and m_lbn in efe_p:
                    ep = efe_p[m_lbn]
                    tid = struct.unpack_from("<H", buf, ep)[0]
                    iflg = struct.unpack_from("<H", buf, ep + 30)[0] & 0x07
                    lea = struct.unpack_from("<I", buf, ep + (208 if tid == 266 else 168))[0]
                    ado = ep + (216 if tid == 266 else 176) + lea
                    mb = b""
                    if iflg == 3 and ado + 64 < bl:
                        mb = buf[ado:ado + min(efe_s[m_lbn], 16384)]
                    elif iflg in (0, 1) and ado + 8 <= bl:
                        albn = struct.unpack_from("<I", buf, ado + 4)[0]
                        tp = ep + ((albn - m_lbn) * 2048)
                        if 0 <= tp < bl - 2048 and buf[tp:tp + 4] == b"MPLS":
                            mb = buf[tp:tp + min(efe_s[m_lbn], 65536)]

                    if mb and b"MPLS" in mb[:8]:
                        clips, _ = parse_mpls_header_info(mb)
                        if clips and all(cid in m2ts_m for cid in clips):
                            tot_s = sum(m2ts_m.get(cid, 0) for cid in clips)
                            if tot_s >= eff_min:
                                return tot_s, clips, mb

                if not m_up and m2ts_m:
                    best_cid = max(m2ts_m, key=lambda k: m2ts_m[k])
                    if m2ts_m[best_cid] >= eff_min:
                        return m2ts_m[best_cid], [best_cid], b""
    except Exception:
        pass

    s7, c7, b7 = inspect_iso_mpls_via_7z(iso_path, m_up, sevenzip_bin=sevenzip_bin)
    if s7 >= eff_min:
        return s7, c7, b7
    return 0, c7, b7


def inspect_bluray_iso_playlist_bytes(iso_path: str, mpls_name: str, min_valid_bytes: int = 100 * 1024 * 1024, sevenzip_bin: str = None) -> int:
    sz, _, _ = parse_bluray_iso_mpls_details(iso_path, mpls_name, min_valid_bytes=min_valid_bytes, sevenzip_bin=sevenzip_bin)
    return sz


def _parse_tag_duration_sec(dur_val) -> float:
    if not dur_val:
        return 0.0
    s = str(dur_val).strip()
    m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)", s)
    if m:
        try:
            return int(m.group(1)) * 3600.0 + int(m.group(2)) * 60.0 + float(m.group(3))
        except Exception:
            pass
    try:
        return float(s)
    except Exception:
        return 0.0


def probe_media_bitrates(file_path: str, ffprobe_bin: str = None):
    if file_path and os.path.isfile(file_path) and os.path.abspath(file_path) in _pending_audio_titles:
        mp_hint = _resolve_sibling_tool(ffprobe_bin, ("mkvpropedit.exe", "mkvpropedit") if os.name == "nt" else ("mkvpropedit", "mkvpropedit.exe"))
        sync_mkv_audio_titles(file_path, mkvpropedit_bin=mp_hint or None)

    fp_b = ffprobe_bin or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not fp_b or not file_path or not os.path.exists(file_path):
        return 0, 0
    tgt = find_largest_media_file_in_dir(file_path) if os.path.isdir(file_path) else file_path
    if not tgt or not os.path.isfile(tgt) or tgt.lower().endswith(".iso"):
        return 0, 0

    try:
        cmd = [
            fp_b, "-v", "error",
            "-show_entries", "format=bit_rate,duration,size:stream=index,codec_type,codec_name,profile,bit_rate,channels,sample_rate,bits_per_raw_sample,bits_per_sample:stream_tags=BPS,BPS-eng,NUMBER_OF_BYTES,NUMBER_OF_BYTES-eng,DURATION,DURATION-eng",
            "-of", "json", tgt
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=SubprocFlags())
        d = json.loads(res.stdout or "{}")
        sts = d.get("streams") or []
        fmt = d.get("format") or {}

        fmt_dur = _parse_tag_duration_sec(fmt.get("duration"))
        v_bps = 0
        a_bps = 0
        a_est_bps = 0

        for s in sts:
            ct = s.get("codec_type")
            tgs = s.get("tags") or {}
            bv = s.get("bit_rate") or tgs.get("BPS") or tgs.get("BPS-eng")
            try:
                bi = int(float(bv)) if bv else 0
            except Exception:
                bi = 0

            if bi <= 0:
                nb_raw = tgs.get("NUMBER_OF_BYTES") or tgs.get("NUMBER_OF_BYTES-eng")
                st_dur = _parse_tag_duration_sec(tgs.get("DURATION") or tgs.get("DURATION-eng")) or fmt_dur
                if nb_raw and st_dur > 1.0:
                    try:
                        nb_i = int(str(nb_raw).strip())
                        if nb_i > 0:
                            bi = int(round((nb_i * 8.0) / st_dur))
                    except Exception:
                        pass

            if ct == "video" and v_bps == 0 and bi > 0:
                v_bps = bi
            elif ct == "audio":
                cn = str(s.get("codec_name") or "").lower()
                prof = str(s.get("profile") or "").lower()
                ch = int(s.get("channels") or 2)
                if bi == 0 and cn.startswith("pcm_"):
                    bits = int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 16)
                    bi = int(s.get("sample_rate") or 48000) * ch * max(16, bits)
                if 0 < bi < 25000000:
                    a_bps += bi
                else:
                    fam = _norm_codec_family(cn, prof)
                    if fam in ("truehd", "dtshd"):
                        a_est_bps += 3200000 if ch >= 6 else 1800000
                    elif fam == "dts":
                        a_est_bps += 1509000 if ch >= 5 else 768000
                    elif fam == "eac3":
                        a_est_bps += 640000 if ch >= 5 else 256000
                    elif fam == "ac3":
                        a_est_bps += 640000 if ch >= 5 else 192000
                    elif fam == "flac":
                        a_est_bps += 1500000 if ch >= 5 else 850000
                    elif fam in ("aac", "opus", "mp3"):
                        a_est_bps += 384000 if ch >= 5 else 160000

        eff_a_bps = a_bps if a_bps > 0 else min(a_est_bps, 12000000)
        f_bps = int(float(fmt.get("bit_rate") or 0)) if fmt.get("bit_rate") else 0
        if f_bps <= 0 and fmt_dur > 5.0:
            try:
                f_sz = int(fmt.get("size") or os.path.getsize(tgt))
                if f_sz > 0:
                    f_bps = int(round((f_sz * 8.0) / fmt_dur))
            except Exception:
                pass

        if v_bps == 0 and f_bps > eff_a_bps:
            v_bps = max(0, f_bps - (a_bps + a_est_bps))

        return int(round(v_bps / 1000.0)), int(round(eff_a_bps / 1000.0))
    except Exception:
        return 0, 0


def probe_source_size_and_bitrates(orig_source: str, job_node: dict, hb_bin: str, root_item: dict = None, ffprobe_bin: str = None, sevenzip_bin: str = None):
    if not isinstance(job_node, dict):
        job_node = {}
    tot_disk_sz = get_file_size(orig_source)
    v_br, a_br = probe_media_bitrates(orig_source, ffprobe_bin=ffprobe_bin)
    s_low = orig_source.lower()
    is_disc = s_low.endswith(".iso") or os.path.isdir(orig_source) or "bdmv" in s_low or "video_ts" in s_low

    if not is_disc and v_br > 0:
        return tot_disk_sz, v_br, a_br, ""

    root_item = root_item if isinstance(root_item, dict) else {}
    t_meta = root_item.get("Title") if isinstance(root_item.get("Title"), dict) else {}
    s_info = root_item.get("SourceTitleInfo") if isinstance(root_item.get("SourceTitleInfo"), dict) else {}
    src_d = job_node.get("Source") if isinstance(job_node.get("Source"), dict) else {}
    t_num = job_node.get("Title") if job_node.get("Title") is not None else src_d.get("Title", t_meta.get("Index", 1))

    dur_s = extract_job_duration_seconds(job_node, root_item)

    if a_br == 0:
        ja_list = t_meta.get("AudioList") or s_info.get("AudioTracks") or []
        ja_kbps = 0
        for at in ja_list:
            if isinstance(at, dict):
                br_r = int(at.get("BitRate") or at.get("Bitrate") or 0)
                dl = str(at.get("Description") or "").lower()
                if 1000 < br_r <= 8000000:
                    ja_kbps += int(round(br_r / 1000.0))
                elif "dts-hd" in dl or "truehd" in dl:
                    ja_kbps += 3200
                elif "pcm" in dl:
                    ja_kbps += 1536
                elif "ac3" in dl or "dts" in dl:
                    ja_kbps += 640
        if ja_kbps > 0:
            a_br = min(ja_kbps, 12000)

    mpls_n = ""
    pl_r = t_meta.get("Playlist") if t_meta.get("Playlist") is not None else s_info.get("Playlist")
    if isinstance(pl_r, int) and pl_r >= 0:
        mpls_n = f"{pl_r:05d}.MPLS"
    elif isinstance(pl_r, str) and ".mpls" in pl_r.lower():
        mpls_n = pl_r.strip().upper()

    if hb_bin and is_disc:
        try:
            if dur_s <= 0 or not mpls_n:
                s_res = subprocess.run(
                    [hb_bin, "-i", orig_source, "-t", str(t_num), "--previews", "1:0", "--scan"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, creationflags=SubprocFlags()
                )
                out = (s_res.stdout or "") + "\n" + (s_res.stderr or "")

                if dur_s <= 0:
                    md = re.search(r"\+\s*duration:\s*(\d+):(\d+):(\d+)", out)
                    if md:
                        dur_s = int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))

                if a_br == 0:
                    sa_kbps = 0
                    in_ab = False
                    for ln in out.splitlines():
                        if "+ audio tracks:" in ln:
                            in_ab = True
                            continue
                        if in_ab:
                            if ln.strip().startswith("+ subtitle") or (ln.strip().startswith("+ ") and not re.match(r"^\+\s*\d+,", ln.strip())):
                                break
                            mab = re.search(r"(\d+)\s*bps", ln)
                            if mab:
                                bv = int(mab.group(1))
                                if 1000 < bv <= 8000000:
                                    sa_kbps += int(round(bv / 1000.0))
                            elif "dts-hd" in ln.lower() or "truehd" in ln.lower():
                                sa_kbps += 3200
                            elif "lpcm" in ln.lower() or "pcm" in ln.lower():
                                sa_kbps += 1536
                            elif "ac3" in ln.lower() or "dts" in ln.lower():
                                sa_kbps += 640
                    if sa_kbps > 0:
                        a_br = min(sa_kbps, 12000)

                if not mpls_n:
                    mm = re.search(r"\+\s*playlist:\s*(\d+\.MPLS)", out, re.IGNORECASE)
                    if mm:
                        mpls_n = mm.group(1).upper()

            if not mpls_n and os.path.isdir(orig_source):
                det_m, _, _, det_b = find_matching_mpls_in_bdmv(orig_source, target_duration_sec=dur_s)
                if det_m:
                    mpls_n = det_m
                    if det_b >= 50 * 1024 * 1024:
                        tot_disk_sz = det_b

            min_pl = 5 * 1024 * 1024 if mpls_n else (500 * 1024 * 1024 if dur_s >= 1200 else 10 * 1024 * 1024)
            exact_pl_b = 0
            mpls_dur = 0.0
            if mpls_n and os.path.isdir(orig_source):
                for rd, _, fns in os.walk(orig_source):
                    for f in fns:
                        if f.upper() == mpls_n:
                            try:
                                with open(os.path.join(rd, f), "rb") as mf:
                                    mb = mf.read()
                                clips, mpls_dur = parse_mpls_header_info(mb)
                                if clips:
                                    sd = os.path.join(os.path.dirname(rd), "STREAM")
                                    if os.path.isdir(sd):
                                        pl_b = 0
                                        fc_fp = None
                                        for cid in clips:
                                            for ec in (".m2ts", ".M2TS"):
                                                cfp = os.path.join(sd, cid + ec)
                                                if os.path.isfile(cfp):
                                                    pl_b += os.path.getsize(cfp)
                                                    if not fc_fp:
                                                        fc_fp = cfp
                                                    break
                                        if pl_b >= min_pl:
                                            exact_pl_b = pl_b
                                            tot_disk_sz = pl_b
                                            if fc_fp and v_br == 0 and len(clips) == 1:
                                                v_br, _ = probe_media_bitrates(fc_fp, ffprobe_bin=ffprobe_bin)
                            except Exception:
                                pass
            elif s_low.endswith(".iso"):
                iso_b, _, mb_iso = parse_bluray_iso_mpls_details(orig_source, mpls_n, min_valid_bytes=min_pl, sevenzip_bin=sevenzip_bin)
                if mb_iso:
                    _, mpls_dur = parse_mpls_header_info(mb_iso)
                if iso_b >= min_pl:
                    if not mpls_n and tot_disk_sz > 20 * (1024**3) and iso_b < tot_disk_sz * 0.35:
                        exact_pl_b = tot_disk_sz
                    else:
                        exact_pl_b = iso_b
                        tot_disk_sz = iso_b

            if dur_s > 60.0 and (mpls_dur <= 60.0 or abs(mpls_dur - dur_s) > dur_s * 0.20):
                eff_dur = dur_s
            else:
                eff_dur = mpls_dur if mpls_dur > 60.0 else dur_s

            ref_sz_b = exact_pl_b if exact_pl_b >= min_pl else tot_disk_sz
            if eff_dur > 60.0 and ref_sz_b >= min_pl:
                v_br = reconcile_disc_video_bitrate(ref_sz_b, eff_dur, v_br, a_br)
            elif exact_pl_b < min_pl and s_low.endswith(".iso") and tot_disk_sz < min_pl:
                tot_disk_sz = 0
                v_br = 0
        except Exception:
            pass

    if v_br == 2500:
        v_br = 0
    return tot_disk_sz, v_br, a_br, mpls_n


def write_mkv_statistics_tags(mkv_path: str, mkvpropedit_bin: str = None) -> bool:
    sync_mkv_audio_titles(mkv_path, mkvpropedit_bin=mkvpropedit_bin)
    mp_b = mkvpropedit_bin or shutil.which("mkvpropedit")
    if not mp_b or not os.path.isfile(mkv_path) or not mkv_path.lower().endswith(".mkv"):
        return False
    try:
        res = subprocess.run([mp_b, mkv_path, "--add-track-statistics-tags"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=SubprocFlags())
        return res.returncode == 0
    except Exception:
        return False


def _build_source_audio_ord_map(t_alist: list) -> dict:
    """Namapuje 1-based TrackNumber ze zdroje (SourceTitleInfo/Title.AudioList) na pořadí v rámci (lang_grp, family)."""
    ord_by_tn = {}
    counts = {}
    for pos, item in enumerate(t_alist or [], start=1):
        if not isinstance(item, dict):
            continue
        tn = int(item.get("TrackNumber") or pos)
        desc = str(item.get("Description") or item.get("Name") or "")
        lg = _norm_lang_group(str(item.get("LanguageCode") or item.get("Language") or ""), desc)
        fam = _norm_codec_family(item.get("Codec") or item.get("Format") or "", desc)
        k = (lg, fam)
        ord_by_tn[tn] = counts.get(k, 0)
        counts[k] = ord_by_tn[tn] + 1
    return ord_by_tn


def _build_source_sub_ord_map(t_slist: list) -> dict:
    """Namapuje 1-based TrackNumber ze zdroje (SourceTitleInfo/Title.SubtitleList) na pořadí v rámci jazyka."""
    ord_by_tn = {}
    counts = {}
    for pos, item in enumerate(t_slist or [], start=1):
        if not isinstance(item, dict):
            continue
        tn = int(item.get("TrackNumber") or pos)
        desc = str(item.get("Name") or item.get("Language") or "")
        lg = _norm_lang_group(str(item.get("LanguageCode") or item.get("Language") or ""), desc)
        ord_by_tn[tn] = counts.get(lg, 0)
        counts[lg] = ord_by_tn[tn] + 1
    return ord_by_tn


def detect_lpcm_audio_tracks_in_source(
    source_path: str, job_node: dict, ffprobe_bin: str = None, root_item: dict = None,
    lpcm_passthru: bool = True, dts_hd_mode: str = "keep_both", is_partial_range: bool = None
):
    fp_b = ffprobe_bin or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not fp_b or not os.path.isfile(source_path) or source_path.lower().endswith(".iso"):
        return []
    if not isinstance(job_node, dict):
        return []
    r_it = root_item if isinstance(root_item, dict) else {}
    # Pokud úloha začíná až uprostřed filmu (kapitola > 1 / čas > 0), nelze bez posunu přimuxovat zvuk od 00:00:00
    nonzero_start = bool(is_partial_range) if is_partial_range is not None else is_job_nonzero_start(job_node, r_it)
    if nonzero_start:
        return []
    try:
        cmd = [
            fp_b, "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index,codec_name,profile,channels,bits_per_raw_sample,bits_per_sample:stream_tags=language,title",
            "-of", "json", source_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, creationflags=SubprocFlags())
        a_sts = (json.loads(res.stdout or "{}")).get("streams") or []
        if not a_sts:
            return []

        t_meta = r_it.get("Title") if isinstance(r_it.get("Title"), dict) else {}
        s_info = r_it.get("SourceTitleInfo") if isinstance(r_it.get("SourceTitleInfo"), dict) else {}
        t_alist = t_meta.get("AudioList") or s_info.get("AudioTracks") or []
        src_audio_ord_map = _build_source_audio_ord_map(t_alist)

        is_premux_src = os.path.basename(str(source_path or "")).lower().startswith("premux_source")
        premux_audio, _ = probe_premux_streams_info(source_path, ffprobe_bin=fp_b) if is_premux_src else ([], [])
        used_lpcm_modes = set()

        lpcm_out = []
        a_tr = job_node.get("AudioTracks")
        is_libhb_a = not isinstance(a_tr, list)
        if is_libhb_a:
            a_tr = (job_node.get("Audio") or {}).get("AudioList") if isinstance(job_node.get("Audio"), dict) else []

        merge_dts = str(dts_hd_mode or "keep_both").strip().lower() == "merge"
        redundant_dts_idxs = find_redundant_dts_core_indices(job_node, r_it) if merge_dts else set()

        out_slot = 0
        for idx, t in enumerate(a_tr or []):
            if not isinstance(t, dict) or idx in redundant_dts_idxs:
                continue
            cur_out_slot = out_slot
            out_slot += 1

            eo = t.get("Encoder")
            es = str(eo.get("ShortName") if isinstance(eo, dict) else (eo or "copy")).lower().strip()
            if "copy" not in es and "pass" not in es:
                continue

            sc = t.get("ScannedTrack") or t.get("SourceTrack") or {}
            tr = t.get("Track")
            if tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t:
                if 0 <= int(tr) < len(t_alist) and isinstance(t_alist[int(tr)], dict):
                    sc = t_alist[int(tr)]

            sc_desc_raw = str(sc.get("Description") or sc.get("Name") or t.get("Name") or "")
            sc_desc = str(sc.get("Description") or sc.get("Codec") or sc.get("Format") or "").lower()
            exp_src_fam = _norm_codec_family(sc.get("Codec") or sc.get("Format") or "", sc_desc_raw)
            exp_out_fam = _passthru_target_family(es, exp_src_fam)

            if is_premux_src and premux_audio and exp_out_fam == "dts":
                s_tr_orig = int(tr) + 1 if (tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t) else int(tr or sc.get("TrackNumber") or (idx + 1))
                sc_lang = str(sc.get("LanguageCode") or sc.get("Language") or "")
                exp_lang = _norm_lang_group(sc_lang, sc_desc_raw)
                m_kb = re.search(r"(\d+)\s*kbps", sc_desc_raw, re.IGNORECASE)
                exp_kbps = int(m_kb.group(1)) if m_kb else 0
                exp_ch = 6 if "5.1" in sc_desc_raw else (8 if "7.1" in sc_desc_raw else (2 if ("2.0" in sc_desc_raw or "stereo" in sc_desc_raw.lower()) else (5 if "5.0" in sc_desc_raw else 0)))
                exp_ord = src_audio_ord_map.get(s_tr_orig, -1)
                matched_dts, _ = match_premux_audio_stream(
                    exp_lang, exp_src_fam, exp_out_fam, True, exp_ch, exp_kbps, s_tr_orig, premux_audio, used_lpcm_modes, exp_spec_ord=exp_ord
                )
                # V Pre-Mux MKV je DTS-HD MA v jedné stopě -> pokud úloha žádá copy:dts (s DTS-HD MA i bez něj),
                # vyextrahujeme čisté DTS jádro přes FFmpeg -bsf:a dca_core do přesného slotu
                if matched_dts and matched_dts["family"] == "dtshd":
                    used_lpcm_modes.add((matched_dts["hb_track"], "dts"))
                    z_idx = matched_dts["zero_idx"]
                    st_i = a_sts[z_idx] if 0 <= z_idx < len(a_sts) else {}
                    tgs = st_i.get("tags") or {}
                    src_title = ""
                    src_lang = ""
                    for tk_n, tv_n in tgs.items():
                        k_low = str(tk_n).lower()
                        if k_low == "title" and tv_n is not None:
                            src_title = str(tv_n).strip()
                        elif k_low == "language" and tv_n is not None:
                            src_lang = str(tv_n).strip()
                    if not src_title:
                        src_title = clean_hb_auto_audio_name(sc.get("Name") or t.get("Name") or t.get("TrackName") or "")
                    if not src_lang or src_lang.lower() == "und":
                        src_lang = exp_lang if exp_lang != "und" else ""
                    lpcm_out.append({
                        "hb_audio_idx": idx,
                        "out_audio_idx": cur_out_slot,
                        "src_audio_zero_idx": z_idx,
                        "name": src_title,
                        "lang": src_lang,
                        "target_codec": "copy",
                        "bsf": "dca_core"
                    })
                    continue

            if not lpcm_passthru:
                continue

            if sc_desc and ("pcm" not in sc_desc and "lpcm" not in sc_desc):
                continue

            z_idx = int(tr) if (tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t) else (int(tr or sc.get("TrackNumber") or (idx + 1)) - 1)
            if is_premux_src and premux_audio and isinstance(sc, dict):
                sc_lang = str(sc.get("LanguageCode") or sc.get("Language") or "")
                exp_lang = _norm_lang_group(sc_lang, sc_desc)
                exp_ch = 6 if "5.1" in sc_desc else (8 if "7.1" in sc_desc else (2 if ("2.0" in sc_desc or "stereo" in sc_desc) else 0))
                exp_ord = src_audio_ord_map.get(z_idx + 1, -1)
                matched_a, _ = match_premux_audio_stream(
                    exp_lang, "pcm", "pcm", True, exp_ch, 0, z_idx + 1, premux_audio, used_lpcm_modes, exp_spec_ord=exp_ord
                )
                if matched_a:
                    used_lpcm_modes.add((matched_a["hb_track"], "pcm"))
                    z_idx = matched_a["zero_idx"]

            if 0 <= z_idx < len(a_sts):
                st_i = a_sts[z_idx]
                cn = str(st_i.get("codec_name") or "").lower()
                if cn.startswith("pcm_"):
                    bits = int(st_i.get("bits_per_raw_sample") or st_i.get("bits_per_sample") or 16)
                    t_c = "pcm_s24le" if bits >= 24 else "pcm_s16le"
                    tgs = st_i.get("tags") or {}
                    src_title = ""
                    src_lang = ""
                    for tk_n, tv_n in tgs.items():
                        k_low = str(tk_n).lower()
                        if k_low == "title" and tv_n is not None:
                            src_title = str(tv_n).strip()
                        elif k_low == "language" and tv_n is not None:
                            src_lang = str(tv_n).strip()
                    if not src_title:
                        src_title = clean_hb_auto_audio_name(sc.get("Name") or t.get("Name") or t.get("TrackName") or "")
                    if not src_lang or src_lang.lower() == "und":
                        raw_l = str(sc.get("LanguageCode") or sc.get("Language") or "")
                        norm_l = _norm_lang_group(raw_l, sc_desc)
                        if norm_l != "und":
                            src_lang = norm_l
                    lpcm_out.append({
                        "hb_audio_idx": idx,
                        "out_audio_idx": cur_out_slot,
                        "src_audio_zero_idx": z_idx,
                        "name": src_title,
                        "lang": src_lang,
                        "target_codec": t_c
                    })
        return lpcm_out
    except Exception:
        return []


def build_postproc_ffmpeg_cmd(
    ff_p: str,
    fp_p: str,
    w_dest: str,
    orig_s: str,
    t_mkv: str,
    lpcm_tr: list,
    m_list: list,
    to_rem: set,
    convert_ass: bool
) -> list:
    has_lpcm = bool(lpcm_tr and orig_s and os.path.exists(orig_s))
    f_cmd = [ff_p, "-y", "-i", w_dest]
    if has_lpcm:
        # Pokud je zdroj znatelně delší než výstup (např. oříznuté závěrečné titulky 1..N-1), omezíme délku vstupu #1 na délku videa
        trim_in1 = None
        try:
            d_out = float(subprocess.check_output(
                [fp_p, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", w_dest],
                text=True, encoding="utf-8", errors="replace", timeout=25, creationflags=SubprocFlags()
            ).strip() or 0.0)
            d_src = float(subprocess.check_output(
                [fp_p, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", orig_s],
                text=True, encoding="utf-8", errors="replace", timeout=25, creationflags=SubprocFlags()
            ).strip() or 0.0)
            if d_out > 0.5 and d_src > d_out + 2.0:
                trim_in1 = f"{d_out:.3f}"
        except Exception:
            trim_in1 = None
        if trim_in1:
            f_cmd.extend(["-t", trim_in1])
        f_cmd.extend(["-i", orig_s])
    f_cmd.extend(["-map_chapters", "0", "-map", "0:v", "-c:v", "copy"])

    if not has_lpcm:
        f_cmd.extend(["-map", "0:a?", "-c:a", "copy"])
    else:
        ex_a_c = 0
        try:
            pa = subprocess.check_output(
                [fp_p, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", w_dest],
                text=True, encoding="utf-8", errors="replace", timeout=25, creationflags=SubprocFlags()
            ).strip()
            ex_a_c = len([x for x in pa.splitlines() if x.strip()]) if pa else 0
        except Exception:
            ex_a_c = 0

        lpcm_by_slot = {}
        for lp in lpcm_tr:
            sk = int(lp.get("out_audio_idx") if lp.get("out_audio_idx") is not None else lp["hb_audio_idx"])
            while sk in lpcm_by_slot:
                sk += 1
            lpcm_by_slot[sk] = lp

        total_out_a = ex_a_c + len(lpcm_tr)
        hb_a_cursor = 0
        out_a_idx = 0
        emitted_slots = set()

        def _emit_lpcm_entry(lp):
            nonlocal out_a_idx
            f_cmd.extend([
                "-map", f"1:a:{lp['src_audio_zero_idx']}",
                f"-c:a:{out_a_idx}", lp["target_codec"],
            ])
            if lp.get("bsf"):
                f_cmd.extend([f"-bsf:a:{out_a_idx}", str(lp["bsf"])])
            f_cmd.extend([
                f"-metadata:s:a:{out_a_idx}", f"title={lp.get('name') or ''}"
            ])
            if lp.get("lang") and str(lp["lang"]).lower() != "und":
                f_cmd.extend([f"-metadata:s:a:{out_a_idx}", f"language={lp['lang']}"])
            out_a_idx += 1

        for slot_idx in range(total_out_a):
            if slot_idx in lpcm_by_slot:
                _emit_lpcm_entry(lpcm_by_slot[slot_idx])
                emitted_slots.add(slot_idx)
            elif hb_a_cursor < ex_a_c:
                f_cmd.extend(["-map", f"0:a:{hb_a_cursor}", f"-c:a:{out_a_idx}", "copy"])
                hb_a_cursor += 1
                out_a_idx += 1

        while hb_a_cursor < ex_a_c:
            f_cmd.extend(["-map", f"0:a:{hb_a_cursor}", f"-c:a:{out_a_idx}", "copy"])
            hb_a_cursor += 1
            out_a_idx += 1

        for sk in sorted(lpcm_by_slot.keys()):
            if sk not in emitted_slots:
                _emit_lpcm_entry(lpcm_by_slot[sk])

    # convert_ass: False = beze změny, True = ASS/SSA převést na SRT a původní nahradit,
    # "keep_both" = původní ASS/SSA ponechat a hned za něj přidat SRT kopii (viz resolve_ass_srt_mode)
    keep_both = (convert_ass == "keep_both")
    os_i = 0
    for m in (m_list or []):
        idx = m["s_idx"]
        if idx in to_rem:
            continue
        is_ass = m["codec"] in ("ass", "ssa")
        sub_flags = [f for f, on in (("default", m.get("disp_default")), ("forced", m.get("disp_forced"))) if on]
        sub_disp = "+".join(sub_flags) if sub_flags else "0"
        if convert_ass and is_ass and keep_both:
            # SRT kopie PŘED původní ASS: stejný jazyk, název s „(SRT)“, příznaky výchozí / vynucené
            # přesně podle originálu (byl-li ASS výchozí, je výchozí i SRT; jinak ne). ASS zůstává beze změny za ní.
            o_title = str(m.get("title") or "").strip()
            f_cmd.extend(["-map", f"0:s:{idx}", f"-c:s:{os_i}", "srt"])
            f_cmd.extend([f"-metadata:s:s:{os_i}", f"title={(o_title + ' (SRT)') if o_title else 'SRT'}"])
            f_cmd.extend([f"-disposition:s:{os_i}", sub_disp])
            os_i += 1
        f_cmd.extend(["-map", f"0:s:{idx}"])
        f_cmd.extend([f"-c:s:{os_i}", "srt" if (convert_ass and is_ass and not keep_both) else "copy"])
        if keep_both and convert_ass:
            # jakmile má jedna stopa příznaky zadané ručně, FFmpeg je u ostatních přestane přebírat ze zdroje
            # -> u všech titulků je nastavíme výslovně podle originálu
            f_cmd.extend([f"-disposition:s:{os_i}", sub_disp])
        os_i += 1

    if keep_both and convert_ass:
        # Matroska muxer v režimu „infer“ (výchozí ve starších FFmpeg) nechá výchozí jen první stopu daného typu;
        # „passthrough“ zapíše příznaky přesně tak, jak jsou nastavené výše (výchozí SRT i výchozí ASS)
        f_cmd.extend(["-default_mode", "passthrough"])

    f_cmd.append(t_mkv)
    return f_cmd


def is_text_sub_empty(ffmpeg_path: str, file_path: str, sub_zero_idx: int) -> bool:
    cmd = [ffmpeg_path, '-v', 'quiet', '-i', file_path, '-map', f'0:s:{sub_zero_idx}', '-f', 'srt', '-']
    res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=120, creationflags=SubprocFlags())
    if res.returncode != 0:
        # Při chybě FFmpeg NIKDY neoznačit platné titulky jako prázdné
        return False
    for ln in (res.stdout or "").splitlines():
        s = ln.strip()
        if not s or re.match(r'^\d+$', s) or re.match(r'^\d{2}:\d{2}:\d{2},\d{3}.*-->', s):
            continue
        s = re.sub(r'<[^>]+>', '', s)
        s = re.sub(r'\{\\[^}]+\}', '', s)
        s = s.replace('\u200B', '').replace('\uFEFF', '').strip()
        if s:
            return False
    return True


def subtitle_is_burned(t: dict) -> bool:
    """R1: titulek má v HandBrake GUI zapnuté vypálení do obrazu (GUI export „Burned“, libhb JSON „Burn“)."""
    return isinstance(t, dict) and bool(t.get("Burned") or t.get("Burn"))


def count_user_forced_subtitles_in_job(job_node: dict, exclude_burned: bool = False) -> int:
    if not isinstance(job_node, dict):
        return 0
    r_ssrc = job_node.get("SubtitleTracks")
    if not isinstance(r_ssrc, list):
        r_ssrc = (job_node.get("Subtitle") or {}).get("SubtitleList") if isinstance(job_node.get("Subtitle"), dict) else []
    return sum(1 for t in (r_ssrc or []) if isinstance(t, dict) and bool(t.get("Forced", False))
               and not (exclude_burned and subtitle_is_burned(t)))


def compute_subtitle_postproc_status(m_list: list, to_rem: set, job_node: dict = None, auto_extract_forced_subs: bool = False,
                                     burn_in_respected: bool = False):
    orig_forced_cnt = count_user_forced_subtitles_in_job(job_node, exclude_burned=burn_in_respected)
    removed_regular = sum(1 for m in (m_list or []) if m["s_idx"] in to_rem and not m.get("is_forced"))
    removed_forced = sum(1 for m in (m_list or []) if m["s_idx"] in to_rem and m.get("is_forced"))
    kept_forced = sum(1 for m in (m_list or []) if m["s_idx"] not in to_rem and m.get("is_forced") and not m.get("is_empty"))

    if auto_extract_forced_subs:
        newly_extracted_forced = max(0, kept_forced - orig_forced_cnt)
        if newly_extracted_forced > 0:
            return newly_extracted_forced, "FORCED_OK"
        if removed_regular > 0:
            return removed_regular, "CLEANED"
        return 0, "OK"

    total_removed = removed_regular + removed_forced
    return total_removed, ("CLEANED" if total_removed > 0 else "OK")


def resolve_ass_srt_mode(job_data: dict):
    """Z dat úlohy od Manageru určí, co dělat s titulky ASS/SSA.
    False = nechat beze změny; True = převést na SRT a ASS nahradit (chování starších verzí);
    "keep_both" = ponechat ASS a přidat SRT kopii.
    Starší Manager pole ass_srt_mode neposílá -> True (stejné chování jako dřív)."""
    if not isinstance(job_data, dict) or not job_data.get("convert_ass_to_srt", False):
        return False
    return "keep_both" if str(job_data.get("ass_srt_mode") or "").strip().lower() == "keep_both" else True


def analyze_subtitle_tracks_packets(ffmpeg_path: str, ffprobe_path: str, file_path: str, do_clean: bool = True, convert_ass: bool = False):
    p_cmd = [
        ffprobe_path, "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_disposition=default,forced:stream_tags=language,title,NUMBER_OF_BYTES,NUMBER_OF_BYTES-eng,NUMBER_OF_FRAMES",
        "-of", "json", file_path
    ]
    res = subprocess.run(p_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=SubprocFlags())
    if res.returncode != 0:
        return [], set(), False
    sts = (json.loads(res.stdout or "{}")).get("streams") or []
    if not sts:
        return [], set(), False

    # Jednoprůchodový framemd5 sken všech titulkových stop najednou (místo N× čtení celého víceGB MKV!)
    scan_indices = []
    if do_clean:
        for s_idx, st in enumerate(sts):
            tgs = st.get("tags") or {}
            t_bytes = tgs.get("NUMBER_OF_BYTES") or tgs.get("NUMBER_OF_BYTES-eng")
            if not (t_bytes is not None and str(t_bytes).strip() == "0"):
                scan_indices.append(s_idx)

    pkt_lines_by_sub = {i: [] for i in range(len(sts))}
    tot_b_by_sub = {i: 0 for i in range(len(sts))}
    scan_failed_subs = set()

    if do_clean and scan_indices:
        single_cmd = [ffmpeg_path, "-v", "error", "-i", file_path]
        for s_idx in scan_indices:
            single_cmd.extend(["-map", f"0:s:{s_idx}"])
        single_cmd.extend(["-c", "copy", "-f", "framemd5", "-"])
        f_res = subprocess.run(
            single_cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, creationflags=SubprocFlags()
        )
        if f_res.returncode == 0:
            for ln in (f_res.stdout or "").splitlines():
                ls = ln.strip()
                if not ls or ls.startswith("#"):
                    continue
                pts = [x.strip() for x in ls.split(",")]
                if len(pts) >= 6:
                    try:
                        out_s = int(pts[0])
                        sz = int(pts[4])
                    except Exception:
                        continue
                    if 0 <= out_s < len(scan_indices) and sz > 0:
                        orig_sub_idx = scan_indices[out_s]
                        tot_b_by_sub[orig_sub_idx] += sz
                        pkt_lines_by_sub[orig_sub_idx].append(",".join(pts[1:6]))
        else:
            # Záloha po jedné stopě (např. jediný problematický kodek) – při chybě stopu NIKDY nesmazat!
            for s_idx in scan_indices:
                r1 = subprocess.run(
                    [ffmpeg_path, "-v", "error", "-i", file_path, "-map", f"0:s:{s_idx}", "-c", "copy", "-f", "framemd5", "-"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, creationflags=SubprocFlags()
                )
                if r1.returncode != 0:
                    scan_failed_subs.add(s_idx)
                    continue
                for ln in (r1.stdout or "").splitlines():
                    ls = ln.strip()
                    if not ls or ls.startswith("#"):
                        continue
                    pts = [x.strip() for x in ls.split(",")]
                    if len(pts) >= 6:
                        try:
                            sz = int(pts[4])
                        except Exception:
                            sz = 0
                        if sz > 0:
                            tot_b_by_sub[s_idx] += sz
                            pkt_lines_by_sub[s_idx].append(",".join(pts[1:6]))

    m_list = []
    for s_idx, st in enumerate(sts):
        codec = str(st.get("codec_name") or "").lower()
        disp = st.get("disposition") or {}
        tgs = st.get("tags") or {}
        lang = str(tgs.get("language") or "und").lower()
        title = str(tgs.get("title") or "")
        is_f = bool(disp.get("forced")) or ("forced" in title.lower())
        p_lines = pkt_lines_by_sub[s_idx]
        tot_b = tot_b_by_sub[s_idx]

        if s_idx in scan_failed_subs:
            # Při selhání sondy stopu konzervativně ponechat a dát jí unikátní podpis (aby nebyla sloučena jako duplicita)
            c_sig = f"SCAN_ERR_{s_idx}"
            is_emp = False
        else:
            c_sig = hashlib.md5("\n".join(p_lines).encode("utf-8")).hexdigest() if p_lines else "EMPTY"
            is_bmp = any(k in codec for k in ("pgs", "dvd", "vobsub"))
            is_emp = (tot_b == 0 or len(p_lines) == 0 or (is_bmp and tot_b < 64)) if do_clean else False

        if do_clean and not is_emp and s_idx not in scan_failed_subs and codec in ("srt", "ass", "ssa", "subrip", "text"):
            if is_text_sub_empty(ffmpeg_path, file_path, s_idx):
                is_emp = True
                tot_b = 0
                c_sig = "EMPTY"

        m_list.append({
            "s_idx": s_idx, "codec": codec, "lang": lang, "title": title,
            "is_forced": is_f, "total_bytes": tot_b, "content_sig": c_sig, "is_empty": is_emp,
            "disp_default": bool(disp.get("default")), "disp_forced": bool(disp.get("forced")),
        })

    to_rem = set()
    if do_clean:
        for m in m_list:
            if m["is_empty"]:
                to_rem.add(m["s_idx"])

        max_b_lang = {}
        for m in m_list:
            if m["s_idx"] not in to_rem:
                lg = m["lang"]
                max_b_lang[lg] = max(max_b_lang.get(lg, 0), m["total_bytes"])

        s_grps = {}
        for m in m_list:
            if m["s_idx"] not in to_rem:
                s_grps.setdefault((m["lang"], m["content_sig"]), []).append(m)

        for (lg, _), grp in s_grps.items():
            if len(grp) < 2:
                continue
            is_sm_f = (max_b_lang.get(lg, 0) > grp[0]["total_bytes"] * 1.5)
            cands = [x for x in grp if x["is_forced"]] if is_sm_f else [x for x in grp if not x["is_forced"]]
            keeper = cands[0] if cands else grp[0]
            for x in grp:
                if x["s_idx"] != keeper["s_idx"]:
                    to_rem.add(x["s_idx"])

    needs_ass = any((m["s_idx"] not in to_rem and convert_ass and m["codec"] in ("ass", "ssa")) for m in m_list)
    return m_list, to_rem, needs_ass


# ==============================================================================
# 7. NATIVNÍ WIN32 CopyFileExW KOPÍROVACÍ ENGINE & HANDBRAKE CLI PŘEKLADAČ
# ==============================================================================
if os.name == "nt":
    _LPPROGRESS_ROUTINE = ctypes.WINFUNCTYPE(
        ctypes.c_uint32,
        ctypes.c_int64, ctypes.c_int64, ctypes.c_int64, ctypes.c_int64,
        ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )
    try:
        _k32_copy = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32_copy.CopyFileExW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, _LPPROGRESS_ROUTINE,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_uint32
        ]
        _k32_copy.CopyFileExW.restype = ctypes.c_int
    except Exception:
        _k32_copy = None
else:
    _k32_copy = None


def copy_path_with_progress(src_path: str, dst_path: str, total_bytes: int, is_cancelled_fn, on_progress_fn):
    copied_prior_files = 0
    t0 = time.time()
    last_cb_ts = [0.0]
    last_remote_chk = [t0]

    def _check_cancel():
        now_c = time.time()
        chk_r = (now_c - last_remote_chk[0]) >= 12.0
        if chk_r:
            last_remote_chk[0] = now_c
        return bool(is_cancelled_fn(chk_r))

    def _copy_single(sf, df):
        nonlocal copied_prior_files
        if _check_cancel():
            raise InterruptedError("Cancelled")

        if os.name == "nt" and _k32_copy is not None:
            cancel_int = ctypes.c_int(0)
            file_sz = 0
            try:
                file_sz = os.path.getsize(sf)
            except Exception:
                file_sz = 0

            def _win32_prog_cb(tot_f_sz, tot_trans, strm_sz, strm_trans, strm_num, cb_reason, h_src, h_dst, p_data):
                try:
                    if _check_cancel():
                        cancel_int.value = 1
                        return 1
                    now_t = time.time()
                    if now_t - last_cb_ts[0] >= 0.35 or tot_trans >= tot_f_sz:
                        cur_total = copied_prior_files + int(tot_trans)
                        elap = max(0.001, now_t - t0)
                        spd = (cur_total / elap) / (1024 * 1024)
                        prog = (cur_total / total_bytes) * 100.0 if total_bytes > 0 else 0.0
                        on_progress_fn(min(100.0, prog), spd, cur_total, int(elap), now_t)
                        last_cb_ts[0] = now_t
                except Exception:
                    pass
                return 0

            cb_ref = _LPPROGRESS_ROUTINE(_win32_prog_cb)
            ctypes.set_last_error(0)
            ok = _k32_copy.CopyFileExW(sf, df, cb_ref, None, ctypes.byref(cancel_int), 0)
            win_err = ctypes.get_last_error()
            if cancel_int.value or win_err == 1235 or is_cancelled_fn(False):
                raise InterruptedError("Cancelled")
            if ok:
                copied_prior_files += file_sz
                now_t = time.time()
                elap = max(0.001, now_t - t0)
                spd = (copied_prior_files / elap) / (1024 * 1024)
                prog = (copied_prior_files / total_bytes) * 100.0 if total_bytes > 0 else 100.0
                on_progress_fn(min(100.0, prog), spd, copied_prior_files, int(elap), now_t)
                return
            # Skutečné chyby disku/sítě (disk full 112, network 53/59/64, access denied 5, not found 2/3) neopakovat od nuly
            if win_err in (2, 3, 5, 39, 53, 59, 64, 67, 112, 1231, 1232):
                raise OSError(None, f"CopyFileExW failed (WinError {win_err})", sf, win_err)

        with open(sf, "rb", buffering=32 * 1024 * 1024) as fsrc, open(df, "wb", buffering=32 * 1024 * 1024) as fdst:
            while True:
                if _check_cancel():
                    raise InterruptedError("Cancelled")
                buf = fsrc.read(32 * 1024 * 1024)
                if not buf:
                    break
                fdst.write(buf)
                copied_prior_files += len(buf)
                now_t = time.time()
                if now_t - last_cb_ts[0] >= 0.35:
                    elap = max(0.001, now_t - t0)
                    spd = (copied_prior_files / elap) / (1024 * 1024)
                    prog = (copied_prior_files / total_bytes) * 100.0 if total_bytes > 0 else 0.0
                    on_progress_fn(min(100.0, prog), spd, copied_prior_files, int(elap), now_t)
                    last_cb_ts[0] = now_t

    if os.path.isfile(src_path):
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        _copy_single(src_path, dst_path)
        if total_bytes > 0 and copied_prior_files < total_bytes * 0.98:
            raise RuntimeError("File copy incomplete.")
    elif os.path.isdir(src_path):
        os.makedirs(dst_path, exist_ok=True)
        for dp, _, fns in os.walk(src_path):
            rp = os.path.relpath(dp, src_path)
            dd = os.path.join(dst_path, rp) if rp != "." else dst_path
            os.makedirs(dd, exist_ok=True)
            for f in fns:
                sf = os.path.join(dp, f)
                if not os.path.islink(sf):
                    _copy_single(sf, os.path.join(dd, f))
        if total_bytes > 0 and copied_prior_files < total_bytes * 0.98:
            raise RuntimeError("Directory copy incomplete.")


# Kodeky, které HandBrakeCLI zná v --audio-copy-mask (libhb common.c: "copy:<kodek>"). Neznámý název by CLI
# odmítlo („Invalid audio codec in autopassthru copy mask“), proto se vždy filtruje přes tento seznam.
HB_AUDIO_COPY_CODECS = ("truehd", "dtshd", "dts", "eac3", "ac3", "aac", "mp2", "mp3", "flac", "opus", "vorbis", "alac", "pcm")
HB_AUDIO_COPY_MASK_DEFAULT = "truehd,dtshd,dts,eac3,ac3,aac,mp3,flac,opus"   # beze změny od 2.11 (když úloha nic neuvádí)


def audio_copy_mask_from_job(job_node: dict) -> str:
    """Seznam kodeků pro „Auto Passthru“ (--audio-copy-mask) přesně podle úlohy HandBrake.
    Windows GUI: Task.AudioPassthruOptions[].ShortName (zaškrtnuté kodeky v záložce Audio – EncodeTaskFactory z nich
    skládá Audio.CopyMask); libhb JSON: Job.Audio.CopyMask ["copy:ac3", …]. Bez údaje dosavadní výchozí seznam."""
    raw = None
    if isinstance(job_node, dict):
        apo = job_node.get("AudioPassthruOptions")
        if isinstance(apo, list) and apo:
            raw = [(o.get("ShortName") if isinstance(o, dict) else o) for o in apo]
        else:
            a_d = job_node.get("Audio")
            cm = a_d.get("CopyMask") if isinstance(a_d, dict) else None
            if isinstance(cm, list) and cm:
                raw = cm
    out = []
    for c in raw or []:
        if not isinstance(c, str):
            continue
        n = c.strip().lower()
        if n.startswith("copy:"):
            n = n[5:]
        if n in HB_AUDIO_COPY_CODECS and n not in out:
            out.append(n)
    return ",".join(out) if out else HB_AUDIO_COPY_MASK_DEFAULT


def gui_audio_fallback_encoder(job_node: dict) -> str:
    """Záložní zvukový enkodér z Windows GUI úlohy (Task.AudioFallbackEncoder.ShortName); '' když není / je „none“."""
    fb = job_node.get("AudioFallbackEncoder") if isinstance(job_node, dict) else None
    s = str((fb.get("ShortName") if isinstance(fb, dict) else fb) or "").strip()
    return "" if s.lower() in ("none", "null") else s


def _gui_passthru_needs_fallback(track: dict, scanned: dict) -> bool:
    """Windows GUI (EncodeTaskFactory.CreateAudio): pokud zvolený passthru enkodér neumí zdrojový kodek stopy
    ((ScannedTrack.Codec & Encoder.Id) == 0), GUI stopu převede záložním enkodérem. HandBrakeCLI by takovou stopu
    zahodil („Passthru requested and input codec is not the same as output codec … dropping track“)."""
    eo = track.get("Encoder") if isinstance(track, dict) else None
    enc_id = eo.get("Id") if isinstance(eo, dict) else None
    sc_codec = scanned.get("Codec") if isinstance(scanned, dict) else None
    if isinstance(enc_id, bool) or isinstance(sc_codec, bool):
        return False
    if not isinstance(enc_id, int) or not isinstance(sc_codec, int) or enc_id <= 0 or sc_codec <= 0:
        return False
    return (sc_codec & enc_id) == 0


def build_hb_cmd_from_windows_json(
    hb_bin,
    job_node,
    orig_source,
    working_dest,
    auto_extract_forced_subs=False,
    skip_audio_indices=None,
    root_item=None,
    eco_mode: bool = False,
    eco_threads: int = 0,
    burn_in_mode: str = "separate",
    dts_hd_mode: str = "keep_both"
):
    """burn_in_mode (R1, Nastavení → Video): "respect" = titulky s Burn In z HandBrake GUI se vypálí do obrazu,
    "separate" = uloží se jako samostatná stopa (chování do dev.43; i když Manager volbu neposílá).
    dts_hd_mode: "keep_both" = podle HandBrake GUI, "merge" = vynechá nadbytečné DTS jádro, pokud je současně DTS-HD MA."""
    if not isinstance(job_node, dict):
        job_node = {}
    skip_a = set(skip_audio_indices or [])
    r_item = root_item if isinstance(root_item, dict) else {}
    ui_cfg = r_item.get("uiSettings") if isinstance(r_item.get("uiSettings"), dict) else {}
    t_meta = r_item.get("Title") if isinstance(r_item.get("Title"), dict) else {}
    s_info = r_item.get("SourceTitleInfo") if isinstance(r_item.get("SourceTitleInfo"), dict) else {}

    src_d = job_node.get("Source") if isinstance(job_node.get("Source"), dict) else {}
    dst_d = job_node.get("Destination") if isinstance(job_node.get("Destination"), dict) else {}
    vid_d = job_node.get("Video") if isinstance(job_node.get("Video"), dict) else {}
    pic_d = job_node.get("Picture") if isinstance(job_node.get("Picture"), dict) else {}
    f_arr = (job_node.get("Filters") or {}).get("FilterList") if isinstance(job_node.get("Filters"), dict) else []

    mux_v = dst_d.get("Mux") if dst_d.get("Mux") is not None else job_node.get("OutputFormat")
    mux_low = str(mux_v or "").strip().lower()
    if mux_v == 0 or mux_low in ("mp4", "av_mp4", "m4v"):
        o_fmt = "av_mp4"
    elif mux_v == 1 or mux_low in ("mov", "av_mov"):
        o_fmt = "av_mov"
    elif mux_v == 3 or "webm" in mux_low:
        o_fmt = "av_webm"
    else:
        o_fmt = "av_mkv"
    cli = [hb_bin, "-v", "--format", o_fmt, "-i", orig_source, "-o", working_dest]

    t_num = job_node.get("Title") if job_node.get("Title") is not None else src_d.get("Title", t_meta.get("Index", 1))
    cli.extend(["-t", str(t_num)])

    ang = job_node.get("Angle") or src_d.get("Angle", 1)
    if int(ang or 1) > 1:
        cli.extend(["--angle", str(ang)])

    is_premux_src = os.path.basename(str(orig_source or "")).lower().startswith("premux_source")
    is_partial_rng, _ = is_job_partial_range(job_node, r_item)

    rng = src_d.get("Range") if isinstance(src_d.get("Range"), dict) else {}
    rt_raw = str(rng.get("Type") or ui_cfg.get("PtoPType") or "chapter").lower()
    pt_m = job_node.get("PointToPointMode")
    if pt_m is None:
        pt_m = 0 if "chap" in rt_raw else (1 if "time" in rt_raw or "sec" in rt_raw else (2 if "frame" in rt_raw else 0))

    p_start = job_node.get("StartPoint") if job_node.get("StartPoint") is not None else (job_node.get("ChapterStart") if job_node.get("ChapterStart") is not None else rng.get("Start"))
    p_end = job_node.get("EndPoint") if job_node.get("EndPoint") is not None else (job_node.get("ChapterEnd") if job_node.get("ChapterEnd") is not None else rng.get("End"))
    if p_start is not None or p_end is not None:
        si = int(float(p_start)) if p_start is not None else None
        ei = int(float(p_end)) if p_end is not None else None
        if pt_m == 0:
            # U celého filmu z Pre-Muxu (1..N) nepředáváme -c, aby rozdíl v počtu kapitol mezi libbluray a mkvmerge neořízl závěr filmu
            if not (is_premux_src and not is_partial_rng):
                if si is not None and ei is not None:
                    cli.extend(["-c", f"{si}-{ei}"])
                elif si is not None:
                    cli.extend(["-c", f"{si}-"])
                elif ei is not None:
                    cli.extend(["-c", f"1-{ei}"])
        elif pt_m == 1:
            is_pts_ticks = (rt_raw == "time") or (ei is not None and ei > 500000) or (si is not None and si > 500000)
            if is_pts_ticks:
                if si is not None:
                    si = int(round(si / 90000.0))
                if ei is not None:
                    ei = int(round(ei / 90000.0))
            if si is not None:
                cli.extend(["--start-at", f"seconds:{max(0, si)}"])
            if ei is not None:
                dur_s = max(1, ei - (si or 0))
                cli.extend(["--stop-at", f"seconds:{dur_s}"])
        elif pt_m == 2:
            if si is not None:
                cli.extend(["--start-at", f"frames:{max(0, si)}"])
            if ei is not None:
                dur_f = max(1, ei - (si or 0))
                cli.extend(["--stop-at", f"frames:{dur_f}"])

    # Extrakce libhb FilterList podle ID (20/21=CROP_SCALE, 11=VFR, 17/18=ROTATE, 26/27=GRAYSCALE, 28/29=PAD)
    flt_by_id = {}
    if isinstance(f_arr, list):
        for flt in f_arr:
            if isinstance(flt, dict) and isinstance(flt.get("Settings"), dict):
                fid = flt.get("ID")
                if fid is not None:
                    flt_by_id[int(fid)] = flt["Settings"]
    flt_crop_scale = flt_by_id.get(20) or flt_by_id.get(21) or {}

    mw = job_node.get("MaxWidth") or pic_d.get("MaxWidth") or ui_cfg.get("PictureWidth") or pic_d.get("Width")
    mh = job_node.get("MaxHeight") or pic_d.get("MaxHeight") or ui_cfg.get("PictureHeight") or pic_d.get("Height")
    exp_w = job_node.get("Width") or flt_crop_scale.get("width")
    exp_h = job_node.get("Height") or flt_crop_scale.get("height")
    if mw:
        cli.extend(["--maxWidth", str(int(float(mw)))])
    elif exp_w and int(float(exp_w)) > 0:
        cli.extend(["-w", str(int(float(exp_w)))])
    if mh:
        cli.extend(["--maxHeight", str(int(float(mh)))])
    elif exp_h and int(float(exp_h)) > 0:
        cli.extend(["-l", str(int(float(exp_h)))])
    if job_node.get("AllowUpscaling") or ui_cfg.get("PictureAllowUpscaling"):
        cli.append("--allow-upscaling")

    c_val = None
    crp = job_node.get("Cropping") or job_node.get("Crop") or pic_d.get("Crop")
    if isinstance(crp, dict):
        c_val = f"{int(crp.get('Top', 0))}:{int(crp.get('Bottom', 0))}:{int(crp.get('Left', 0))}:{int(crp.get('Right', 0))}"
    elif isinstance(crp, list) and len(crp) >= 4:
        c_val = f"{int(crp[0])}:{int(crp[1])}:{int(crp[2])}:{int(crp[3])}"
    elif flt_crop_scale and any(k in flt_crop_scale for k in ("crop-top", "crop-bottom", "crop-left", "crop-right", "top", "bottom", "left", "right")):
        ct = int(flt_crop_scale.get("crop-top") if "crop-top" in flt_crop_scale else flt_crop_scale.get("top", 0))
        cb = int(flt_crop_scale.get("crop-bottom") if "crop-bottom" in flt_crop_scale else flt_crop_scale.get("bottom", 0))
        cl = int(flt_crop_scale.get("crop-left") if "crop-left" in flt_crop_scale else flt_crop_scale.get("left", 0))
        cr = int(flt_crop_scale.get("crop-right") if "crop-right" in flt_crop_scale else flt_crop_scale.get("right", 0))
        c_val = f"{ct}:{cb}:{cl}:{cr}"
    elif isinstance(f_arr, list):
        for flt in f_arr:
            if isinstance(flt, dict) and isinstance(flt.get("Settings"), dict) and flt.get("ID") is None:
                st = flt["Settings"]
                if any(k in st for k in ("crop-top", "crop-bottom", "crop-left", "crop-right")):
                    c_val = f"{int(st.get('crop-top', 0))}:{int(st.get('crop-bottom', 0))}:{int(st.get('crop-left', 0))}:{int(st.get('crop-right', 0))}"
                    break
    if not c_val and ui_cfg and any(k in ui_cfg for k in ("PictureTopCrop", "PictureBottomCrop", "PictureLeftCrop", "PictureRightCrop")):
        c_val = f"{int(float(ui_cfg.get('PictureTopCrop', 0)))}:{int(float(ui_cfg.get('PictureBottomCrop', 0)))}:{int(float(ui_cfg.get('PictureLeftCrop', 0)))}:{int(float(ui_cfg.get('PictureRightCrop', 0)))}"
    if not c_val and isinstance(t_meta.get("Crop"), list) and len(t_meta["Crop"]) >= 4:
        tc = t_meta["Crop"]
        c_val = f"{int(tc[0])}:{int(tc[1])}:{int(tc[2])}:{int(tc[3])}"
    if c_val:
        cli.extend(["--crop", c_val])

    anam = job_node.get("Anamorphic", pic_d.get("AnamorphicMode", ui_cfg.get("PicturePAR", 4)))
    if anam == 0 or str(anam).lower() in ("none", "off"):
        cli.append("--non-anamorphic")
    elif anam == 2 or str(anam).lower() == "loose":
        cli.append("--loose-anamorphic")
    elif anam == 3 or str(anam).lower() == "custom":
        cli.append("--custom-anamorphic")
        disp_w = job_node.get("DisplayWidth") or ui_cfg.get("PictureForceWidth")
        if disp_w and int(float(disp_w)) > 0:
            cli.extend(["--display-width", str(int(float(disp_w)))])
        pax, pay = job_node.get("PixelAspectX"), job_node.get("PixelAspectY")
        if pax and pay and int(pax) > 0 and int(pay) > 0:
            cli.extend(["--pixel-aspect", f"{int(pax)}:{int(pay)}"])
        if job_node.get("KeepDisplayAspect"):
            cli.append("--keep-display-aspect")
    else:
        cli.append("--auto-anamorphic")

    flt_rot = flt_by_id.get(17) or flt_by_id.get(18) or {}
    rot_a = int(job_node.get("Rotation") or ui_cfg.get("rotate") or flt_rot.get("angle") or 0)
    hf_v = 1 if (job_node.get("FlipVideo") or ui_cfg.get("hflip") or flt_rot.get("hflip")) else 0
    if rot_a != 0 or hf_v != 0:
        cli.append(f"--rotate=angle={rot_a}:hflip={hf_v}")

    pad_o = job_node.get("Padding")
    flt_pad = flt_by_id.get(28) or flt_by_id.get(29) or {}
    if isinstance(pad_o, dict) and pad_o.get("Mode", 0) != 0:
        pt, pb, pl, pr = int(pad_o.get("Y", 0)), int(pad_o.get("Bottom", 0)), int(pad_o.get("X", 0)), int(pad_o.get("Right", 0))
        pc = str(pad_o.get("Color") or "black")
        if pt or pb or pl or pr:
            cli.append(f"--pad=top={pt}:bottom={pb}:left={pl}:right={pr}:color={pc}")
    elif ui_cfg and str(ui_cfg.get("PicturePadMode") or "none").lower() != "none":
        pt, pb = int(float(ui_cfg.get("PicturePadTop", 0))), int(float(ui_cfg.get("PicturePadBottom", 0)))
        pl, pr = int(float(ui_cfg.get("PicturePadLeft", 0))), int(float(ui_cfg.get("PicturePadRight", 0)))
        pc = str(ui_cfg.get("PicturePadColor") or "black")
        if pt or pb or pl or pr:
            cli.append(f"--pad=top={pt}:bottom={pb}:left={pl}:right={pr}:color={pc}")
    elif flt_pad:
        pt, pb = int(flt_pad.get("top", 0)), int(flt_pad.get("bottom", 0))
        pl, pr = int(flt_pad.get("left", 0)), int(flt_pad.get("right", 0))
        pc = str(flt_pad.get("color") or "black")
        if pt or pb or pl or pr:
            cli.append(f"--pad=top={pt}:bottom={pb}:left={pl}:right={pr}:color={pc}")

    dst_opts = dst_d.get("Options") if isinstance(dst_d.get("Options"), dict) else {}
    if job_node.get("Optimize") or dst_opts.get("Optimize") or (dst_d.get("Mp4Options") or {}).get("Mp4Optimize"):
        cli.append("-O")
    if job_node.get("IPod5GSupport") or dst_opts.get("IpodAtom"):
        cli.append("--ipod-atom")
    if job_node.get("AlignAVStart") or dst_d.get("AlignAVStart") or dst_opts.get("AlignAVStart") or ui_cfg.get("AlignAVStart"):
        cli.append("--align-av")
    if job_node.get("InlineParameterSets") or dst_d.get("InlineParameterSets"):
        cli.append("--inline-parameter-sets")
    if job_node.get("PassthruMetadataEnabled") is False or ui_cfg.get("MetadataPassthru") is False:
        cli.append("--no-metadata")

    ve_o = job_node.get("VideoEncoder") or vid_d.get("Encoder") or ui_cfg.get("VideoEncoder")
    ve_s = ve_o.get("ShortName") if isinstance(ve_o, dict) else ve_o
    if ve_s:
        cli.extend(["-e", str(ve_s)])

    vp_o = job_node.get("VideoPreset") or vid_d.get("Preset") or ui_cfg.get("VideoPreset")
    vp_s = vp_o.get("ShortName") if isinstance(vp_o, dict) else vp_o
    if vp_s:
        cli.extend(["--encoder-preset", str(vp_s)])

    vpr_o = job_node.get("VideoProfile") or vid_d.get("Profile") or ui_cfg.get("VideoProfile")
    vpr_s = vpr_o.get("ShortName") if isinstance(vpr_o, dict) else vpr_o
    if vpr_s and str(vpr_s).lower() != "auto":
        cli.extend(["--encoder-profile", str(vpr_s)])

    vl_o = job_node.get("VideoLevel") or vid_d.get("Level") or ui_cfg.get("VideoLevel")
    vl_s = vl_o.get("ShortName") if isinstance(vl_o, dict) else vl_o
    if vl_s and str(vl_s).lower() != "auto":
        cli.extend(["--encoder-level", str(vl_s)])

    vt_arr = job_node.get("VideoTunes") or ([vid_d.get("Tune")] if vid_d.get("Tune") else [])
    if vt_arr:
        t_ns = [t.get("ShortName") if isinstance(t, dict) else str(t) for t in vt_arr if t]
        t_ns = [t for t in t_ns if t and t.lower() != "none"]
        if t_ns:
            cli.extend(["--encoder-tune", ",".join(t_ns)])

    qv = job_node.get("Quality") if job_node.get("Quality") is not None else vid_d.get("Quality")
    if qv is not None and job_node.get("VideoEncodeRateType", 2) == 2 and not vid_d.get("Bitrate"):
        qf = float(qv)
        cli.extend(["-q", str(int(qf) if qf.is_integer() else qf)])
    else:
        vbr = job_node.get("VideoBitrate") or vid_d.get("Bitrate")
        if vbr:
            cli.extend(["-b", str(vbr)])
        if job_node.get("MultiPass") or vid_d.get("MultiPass") or job_node.get("TwoPass") or vid_d.get("TwoPass"):
            cli.append("--multi-pass")
        if job_node.get("TurboAnalysisPass") or vid_d.get("Turbo"):
            cli.append("--turbo")

    flt_vfr = flt_by_id.get(11) or {}
    fm = job_node.get("FramerateMode")
    if fm is None:
        if "mode" in flt_vfr:
            # libhb VFR filter: mode 1 = CFR, mode 2 = PFR, mode 0 = VFR
            _m = int(flt_vfr.get("mode", 0))
            fm = 0 if _m == 1 else (1 if _m == 2 else 2)
        else:
            uf = str(ui_cfg.get("VideoFramerateMode") or "vfr").lower()
            fm = 0 if uf == "cfr" else (1 if uf == "pfr" else 2)
    if fm == 0 or vid_d.get("CFR"):
        cli.append("--cfr")
    elif fm == 1:
        cli.append("--pfr")
    elif fm == 2:
        cli.append("--vfr")

    fr = job_node.get("Framerate")
    if not fr and flt_vfr.get("rate"):
        try:
            _rate = float(flt_vfr["rate"])
            # libhb VFR filter ukládá rate = 27000000 / fps
            if _rate > 1000:
                fr = round(27000000.0 / _rate, 3)
            elif _rate > 0:
                fr = _rate
        except Exception:
            pass
    if not fr and ui_cfg.get("VideoFramerate") and str(ui_cfg.get("VideoFramerate")).lower() not in ("auto", "same as source", ""):
        fr = ui_cfg.get("VideoFramerate")
    if fr:
        try:
            fr_f = float(fr)
            if fr_f > 0:
                cli.extend(["-r", str(int(fr_f) if fr_f.is_integer() else fr_f)])
        except Exception:
            pass

    crng = job_node.get("VideoColourRange") or vid_d.get("ColorRange")
    if crng == 1 or str(ui_cfg.get("VideoColorRange") or "").lower() == "limited":
        cli.append("--color-range=limited")
    elif crng == 2 or str(ui_cfg.get("VideoColorRange") or "").lower() == "full":
        cli.append("--color-range=full")

    # HDR dynamická metadata (HDR10+ / Dolby Vision) – stejně jako desktopový HandBrake 1.11:
    # • Windows GUI (Task.PasshtruHDRDynamicMetadata): hodnotu z fronty do enkodéru vůbec nepředává
    #   (EncodeTaskFactory.CreateVideo ji nepoužije) -> GUI vždy zachová vše, co zdroj má. Stejně se chová CLI
    #   bez přepínače, proto nic nepřidáváme ("none" CLI nezná a bere jako "all"; 0 by naopak HDR10+/DV zahodila).
    # • libhb JSON (Job.Video.PasshtruHDRDynamicMetadata, bitová maska 0/2/4/6, -1 = výchozí) libhb respektuje -> přeložit.
    hdr_dyn = vid_d.get("PasshtruHDRDynamicMetadata")
    if isinstance(hdr_dyn, (int, float)) and not isinstance(hdr_dyn, bool) and int(hdr_dyn) >= 0:
        hdr_i = int(hdr_dyn)
        if hdr_i == 0:
            cli.append("--no-hdr-dynamic-metadata")
        elif hdr_i == 2:
            cli.append("--hdr-dynamic-metadata=hdr10plus")
        elif hdr_i == 4:
            cli.append("--hdr-dynamic-metadata=dolbyvision")
        # 6 (vše) = výchozí chování CLI

    if job_node.get("Grayscale") or vid_d.get("Grayscale") or ui_cfg.get("VideoGrayScale") or (26 in flt_by_id) or (27 in flt_by_id):
        cli.append("--grayscale")

    ex_a = job_node.get("ExtraAdvancedArguments") or job_node.get("AdvancedEncoderOptions") or vid_d.get("Options") or ui_cfg.get("VideoOptionExtra") or ""
    ex_a_str = str(ex_a).strip()
    if eco_mode and ve_s and ("x265" in str(ve_s).lower() or "hevc" in str(ve_s).lower()):
        if "pools=" not in ex_a_str.lower():
            t_cnt = int(eco_threads) if (eco_threads and int(eco_threads) > 0) else max(1, (os.cpu_count() or 4) - 2)
            ex_a_str = f"{ex_a_str}:pools={t_cnt}" if ex_a_str else f"pools={t_cnt}"
    if ex_a_str:
        cli.extend(["--encopts", ex_a_str])

    cdet = job_node.get("CombDetect", 0)
    ucmb = str(ui_cfg.get("PictureCombDetectPreset") or "off").lower()
    if cdet != 0 or ucmb not in ("off", "none", "0", ""):
        ccmb = str(job_node.get("CustomCombDetect") or ui_cfg.get("PictureCombDetectCustom") or "").strip()
        if (cdet == 1 or ucmb == "custom") and ccmb:
            cli.append(f"--comb-detect={ccmb}")
        elif cdet == 3:
            cli.append("--comb-detect=permissive")
        elif cdet == 4:
            cli.append("--comb-detect=fast")
        elif ucmb not in ("off", "none", "0", "", "default", "custom"):
            cli.append(f"--comb-detect={ucmb}")
        else:
            cli.append("--comb-detect")

    dint = job_node.get("DeinterlaceFilter", 0)
    udint = str(ui_cfg.get("PictureDeinterlaceFilter") or "off").lower()
    dint_p = job_node.get("DeinterlacePreset", {}).get("ShortName", "") if isinstance(job_node.get("DeinterlacePreset"), dict) else str(ui_cfg.get("PictureDeinterlacePreset") or "")
    dint_c = str(job_node.get("CustomDeinterlaceSettings") or ui_cfg.get("PictureDeinterlaceCustom") or "").strip()
    dint_arg = dint_c if (str(dint_p).lower() == "custom" and dint_c) else (str(dint_p).strip() if str(dint_p).strip().lower() not in ("", "default", "off", "custom") else "")
    if dint == 2 or udint == "decomb":
        cli.append(f"--decomb={dint_arg}" if dint_arg else "--decomb")
    elif dint == 3 or udint == "bwdif":
        cli.append(f"--bwdif={dint_arg}" if dint_arg else "--bwdif")
    elif dint == 1 or udint in ("yadif", "deinterlace"):
        cli.append(f"--deinterlace={dint_arg}" if dint_arg else "--deinterlace")

    dtel = job_node.get("Detelecine", 0)
    udtel = str(ui_cfg.get("PictureDetelecine") or "off").lower()
    if dtel != 0 or udtel not in ("off", "none", "0", ""):
        cdtel = str(job_node.get("CustomDetelecine") or ui_cfg.get("PictureDetelecineCustom") or "").strip()
        if (dtel == 1 or udtel == "custom") and cdtel:
            cli.append(f"--detelecine={cdtel}")
        else:
            cli.append("--detelecine")

    dns = job_node.get("Denoise", 0)
    udns = str(ui_cfg.get("PictureDenoiseFilter") or "off").lower()
    dn_p = job_node.get("DenoisePreset", {}).get("ShortName", "") if isinstance(job_node.get("DenoisePreset"), dict) else str(ui_cfg.get("PictureDenoisePreset") or "")
    dn_t = job_node.get("DenoiseTune", {}).get("ShortName", "") if isinstance(job_node.get("DenoiseTune"), dict) else str(ui_cfg.get("PictureDenoiseTune") or "")
    dn_c = str(job_node.get("CustomDenoise") or ui_cfg.get("PictureDenoiseCustom") or "").strip()
    is_dn_c = bool(str(dn_p).lower() == "custom" and dn_c)
    dn_val = dn_c if is_dn_c else ("" if str(dn_p).lower() == "custom" else str(dn_p).strip())
    if dns == 1 or udns == "hqdn3d":
        cli.append(f"--hqdn3d={dn_val}" if dn_val else "--hqdn3d")
    elif dns == 2 or udns == "nlmeans":
        cli.append(f"--nlmeans={dn_val}" if dn_val else "--nlmeans")
        if not is_dn_c and dn_t and dn_t.lower() != "none":
            cli.extend(["--nlmeans-tune", dn_t])

    csm = job_node.get("ChromaSmooth", {}).get("Key", "off") if isinstance(job_node.get("ChromaSmooth"), dict) else str(ui_cfg.get("PictureChromaSmoothPreset") or "off")
    if csm and csm.lower() not in ("off", "none", "0"):
        ccsm = str(job_node.get("CustomChromaSmooth") or ui_cfg.get("PictureChromaSmoothCustom") or "").strip()
        is_csm_c = bool(csm.lower() == "custom" and ccsm)
        csm_val = ccsm if is_csm_c else ("" if csm.lower() == "custom" else csm)
        if csm_val:
            cli.append(f"--chroma-smooth={csm_val}")
            if not is_csm_c:
                cst = job_node.get("ChromaSmoothTune", {}).get("Key", "") if isinstance(job_node.get("ChromaSmoothTune"), dict) else str(ui_cfg.get("PictureChromaSmoothTune") or "")
                if cst and cst.lower() not in ("none", ""):
                    cli.extend(["--chroma-smooth-tune", cst])

    shp = job_node.get("Sharpen", 0)
    ushp = str(ui_cfg.get("PictureSharpenFilter") or "off").lower()
    sh_p = job_node.get("SharpenPreset", {}).get("Key", "") if isinstance(job_node.get("SharpenPreset"), dict) else str(ui_cfg.get("PictureSharpenPreset") or "")
    sh_t = job_node.get("SharpenTune", {}).get("Key", "") if isinstance(job_node.get("SharpenTune"), dict) else str(ui_cfg.get("PictureSharpenTune") or "")
    sh_c = str(job_node.get("SharpenCustom") or ui_cfg.get("PictureSharpenCustom") or "").strip()
    is_sh_c = bool(str(sh_p).lower() == "custom" and sh_c)
    sh_val = sh_c if is_sh_c else ("" if str(sh_p).lower() == "custom" else str(sh_p).strip())
    if shp == 1 or ushp == "unsharp":
        cli.append(f"--unsharp={sh_val}" if sh_val else "--unsharp")
        if not is_sh_c and sh_t and sh_t.lower() != "none":
            cli.extend(["--unsharp-tune", sh_t])
    elif shp == 2 or ushp == "lapsharp":
        cli.append(f"--lapsharp={sh_val}" if sh_val else "--lapsharp")
        if not is_sh_c and sh_t and sh_t.lower() != "none":
            cli.extend(["--lapsharp-tune", sh_t])

    dbl = job_node.get("DeblockPreset", {}).get("Key", "off") if isinstance(job_node.get("DeblockPreset"), dict) else str(ui_cfg.get("PictureDeblockPreset") or "off")
    if dbl and dbl.lower() not in ("off", "none", "0"):
        cdbl = job_node.get("CustomDeblock") or ui_cfg.get("PictureDeblockCustom") or ""
        is_cdbl = bool(dbl.lower() == "custom" and cdbl)
        cli.append(f"--deblock={cdbl if is_cdbl else dbl}")
        if not is_cdbl:
            # R2: velikost bloku z GUI (Small 4x4 / Medium 8x8 / Large 16x16); u vlastního řetězce je v něm
            dbt = job_node.get("DeblockTune", {}).get("Key", "") if isinstance(job_node.get("DeblockTune"), dict) else str(ui_cfg.get("PictureDeblockTune") or "")
            if str(dbt).strip().lower() in ("small", "medium", "large"):
                cli.append(f"--deblock-tune={str(dbt).strip().lower()}")

    csp = job_node.get("Colourspace", {}).get("Key", "off") if isinstance(job_node.get("Colourspace"), dict) else str(ui_cfg.get("PictureColorspacePreset") or "off")
    if csp and csp.lower() not in ("off", "none", "0"):
        ccsp = job_node.get("CustomColourspace") or ui_cfg.get("PictureColorspaceCustom") or ""
        cli.append(f"--colorspace={ccsp if (csp.lower() == 'custom' and ccsp) else csp}")

    sibling_fp = _resolve_sibling_tool(hb_bin, ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe", "ffprobe.exe"))
    premux_audio, premux_subs = probe_premux_streams_info(orig_source, ffprobe_bin=sibling_fp or None) if is_premux_src else ([], [])

    src_audio_titles, probed_src_ok = probe_source_audio_titles(orig_source, ffprobe_bin=sibling_fp or None)
    a_tr, a_en, a_mx, a_br, a_aq, a_nm, a_sr, a_gn, a_dr = [], [], [], [], [], [], [], [], []
    ordered_audio_titles = []
    seen_premux_passthru = set()
    used_premux_audio_modes = set()
    # Přesná tabulka HB_AMIXDOWN z libhb (hb.dll): 4=stereo, 7=5point1, 9=7point1, 10=5_2_lfe
    mx_map = {1: "mono", 2: "left_only", 3: "right_only", 4: "stereo", 5: "dpl1", 6: "dpl2", 7: "5point1", 8: "6point1", 9: "7point1", 10: "5_2_lfe"}
    t_alist = t_meta.get("AudioList") or s_info.get("AudioTracks") or []
    src_audio_ord_map = _build_source_audio_ord_map(t_alist)

    r_alist = job_node.get("AudioTracks")
    is_libhb_a = not isinstance(r_alist, list)
    if is_libhb_a:
        r_alist = (job_node.get("Audio") or {}).get("AudioList") if isinstance(job_node.get("Audio"), dict) else []

    merge_dts = str(dts_hd_mode or "keep_both").strip().lower() == "merge"
    redundant_dts_idxs = find_redundant_dts_core_indices(job_node, r_item) if merge_dts else set()
    gui_fb_enc = gui_audio_fallback_encoder(job_node)

    for idx, t in enumerate(r_alist or []):
        if not isinstance(t, dict) or idx in redundant_dts_idxs:
            continue
        sc = t.get("ScannedTrack") or t.get("SourceTrack") or {}
        tr = t.get("Track")
        if tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t:
            s_tr = int(tr) + 1
            if 0 <= int(tr) < len(t_alist) and isinstance(t_alist[int(tr)], dict):
                sc = t_alist[int(tr)]
        else:
            s_tr = int(tr or sc.get("TrackNumber") or 1)

        eo = t.get("Encoder")
        es = str(eo.get("ShortName", "copy") if isinstance(eo, dict) else (eo if eo else "copy")).strip()
        is_p = ("pass" in es.lower() or "copy" in es.lower())
        forced_ac3_from_premux = False
        exp_kbps_hint = 0
        exp_ch_hint = 0

        if is_premux_src and premux_audio and isinstance(sc, dict):
            sc_desc = str(sc.get("Description") or sc.get("Name") or t.get("Name") or "")
            sc_lang = str(sc.get("LanguageCode") or sc.get("Language") or "")
            exp_lang = _norm_lang_group(sc_lang, sc_desc)
            exp_src_fam = _norm_codec_family(sc.get("Codec") or sc.get("Format") or "", sc_desc)
            exp_out_fam = _passthru_target_family(es, exp_src_fam) if is_p else _norm_codec_family(es, "")
            m_kb = re.search(r"(\d+)\s*kbps", sc_desc, re.IGNORECASE)
            exp_kbps_hint = int(m_kb.group(1)) if m_kb else 0
            exp_ch_hint = 6 if "5.1" in sc_desc else (8 if "7.1" in sc_desc else (2 if ("2.0" in sc_desc or "stereo" in sc_desc.lower()) else (5 if "5.0" in sc_desc else 0)))
            exp_spec_ord = src_audio_ord_map.get(int(s_tr), -1)

            matched_a, force_ac3 = match_premux_audio_stream(
                exp_lang, exp_src_fam, exp_out_fam, is_p, exp_ch_hint, exp_kbps_hint, int(s_tr), premux_audio, used_premux_audio_modes,
                exp_spec_ord=exp_spec_ord
            )
            if matched_a:
                s_tr = matched_a["hb_track"]
                if force_ac3:
                    es = "ac3"
                    is_p = False
                    forced_ac3_from_premux = True
                elif is_p:
                    mfam = matched_a["family"]
                    if exp_out_fam == "dts" and mfam == "dtshd":
                        es = "copy:dts"
                    elif mfam in ("truehd", "dtshd", "dts", "ac3", "eac3", "flac", "aac", "mp3", "opus"):
                        es = f"copy:{mfam}"
                    else:
                        es = "copy"
                used_premux_audio_modes.add((s_tr, exp_out_fam or exp_src_fam or ("passthru" if is_p else "encode")))

        if is_p and not is_libhb_a and not is_premux_src and gui_fb_enc and _gui_passthru_needs_fallback(t, sc):
            # stejně jako Windows GUI: nepodporovaný passthru -> záložní enkodér (CLI by stopu zahodil a posunul pořadí stop)
            es = gui_fb_enc
            is_p = ("pass" in es.lower() or "copy" in es.lower())

        z_idx = max(0, int(s_tr) - 1)
        if probed_src_ok:
            tn = src_audio_titles.get(z_idx, "")
        else:
            src_sc_name = clean_hb_auto_audio_name(sc.get("Name") or "")
            json_t_name = clean_hb_auto_audio_name(t.get("Name") or t.get("TrackName") or "")
            tn = src_sc_name if src_sc_name else json_t_name

        clean_tn = str(tn).replace(",", " ").replace('"', '').rstrip("\\").strip()

        if idx in skip_a:
            ordered_audio_titles.append(clean_tn)
            continue

        if is_premux_src and is_p:
            pm_key = (int(s_tr), es.lower())
            if pm_key in seen_premux_passthru:
                continue
            seen_premux_passthru.add(pm_key)

        a_tr.append(str(s_tr))
        a_en.append(str(es))

        mx = t.get("MixDown") or t.get("Mixdown")
        if is_p:
            mxs = "auto"
        elif forced_ac3_from_premux:
            mxs = "stereo" if exp_ch_hint == 2 else "5point1"
        elif isinstance(mx, dict):
            mxs = mx.get("ShortName", "auto")
        elif isinstance(mx, int):
            mxs = mx_map.get(mx, "auto")
        else:
            mxs = str(mx) if (mx and str(mx).lower() != "none") else "auto"
        a_mx.append(mxs)

        br = t.get("Bitrate")
        aq_v = t.get("Quality")
        ert = t.get("EncoderRateType")
        if isinstance(ert, int) and not isinstance(ert, bool):
            # Windows GUI (EncodeTaskFactory): 0 = Bitrate -> jen bitrate, 1 = Quality -> jen kvalita
            use_aq = (not is_p) and (not forced_ac3_from_premux) and ert == 1
        else:
            use_aq = (not is_p) and (not forced_ac3_from_premux) and (aq_v is not None and float(aq_v) >= 0 and (br is None or int(br) <= 0))
        if is_p:
            a_br.append("auto")
            a_aq.append("")
        elif forced_ac3_from_premux:
            a_br.append(str(exp_kbps_hint if exp_kbps_hint > 0 else 640))
            a_aq.append("")
        elif use_aq and aq_v is not None:
            aq_f = float(aq_v)
            a_br.append("auto")
            a_aq.append(str(int(aq_f) if aq_f.is_integer() else aq_f))
        else:
            a_br.append("auto" if (br is not None and int(br) <= 0) else (str(br) if br else "auto"))
            a_aq.append("")

        srv = t.get("SampleRate") or t.get("Samplerate") or 0
        if is_p or not srv or str(srv) == "0":
            a_sr.append("auto")
        else:
            try:
                sr_f = float(srv)
                # hb_audio_samplerate_get_from_name odmítá "48.0" (-1), ale bere "48" i "48000"
                a_sr.append("auto" if sr_f <= 0 else str(int(sr_f) if sr_f.is_integer() else sr_f))
            except Exception:
                a_sr.append(str(srv))
        gv = float(t.get("Gain") or 0.0)
        a_gn.append("0" if (is_p or gv == 0.0) else str(gv))
        dv = float(t.get("DRC") or 0.0)
        a_dr.append("0" if (is_p or dv == 0.0) else str(dv))

        a_nm.append(clean_tn)
        ordered_audio_titles.append(clean_tn)

    if working_dest:
        with _pending_audio_titles_lock:
            _pending_audio_titles[os.path.abspath(working_dest)] = list(ordered_audio_titles)

    if a_tr:
        cli.extend(["-a", ",".join(a_tr), "-E", ",".join(a_en)])
        if any("copy" in e.lower() or "pass" in e.lower() for e in a_en):
            cli.extend(["--audio-copy-mask", audio_copy_mask_from_job(job_node)])
        if any(m != "auto" for m in a_mx):
            cli.extend(["--mixdown", ",".join([m if m != "auto" else "" for m in a_mx])])
        if any(b != "auto" for b in a_br):
            cli.extend(["-B", ",".join([b if b != "auto" else "" for b in a_br])])
        if any(q != "" for q in a_aq):
            cli.extend(["--aq", ",".join(a_aq)])
        if any(sr != "auto" for sr in a_sr):
            cli.extend(["--arate", ",".join([sr if sr != "auto" else "auto" for sr in a_sr])])
        if any(g != "0" for g in a_gn):
            cli.extend(["--gain", ",".join(a_gn)])
        if any(d != "0" for d in a_dr):
            cli.extend(["--drc", ",".join(a_dr)])
        cli.extend(["-A", ",".join(a_nm)])
    else:
        cli.extend(["-a", "none"])

    fb_o = job_node.get("AudioFallbackEncoder") or (job_node.get("Audio") or {}).get("FallbackEncoder")
    fb_s = fb_o.get("ShortName") if isinstance(fb_o, dict) else fb_o
    if fb_s:
        cli.extend(["--audio-fallback", str(fb_s)])

    t_slist = t_meta.get("SubtitleList") or s_info.get("Subtitles") or []
    src_sub_ord_map = _build_source_sub_ord_map(t_slist)
    r_ssrc = job_node.get("SubtitleTracks")
    is_libhb_s = not isinstance(r_ssrc, list)
    if is_libhb_s:
        r_ssrc = (job_node.get("Subtitle") or {}).get("SubtitleList") if isinstance(job_node.get("Subtitle"), dict) else []

    r_subs_raw = [copy.deepcopy(t) for t in (r_ssrc or []) if isinstance(t, dict)]
    # Oddělit Foreign Audio Scan (TrackNumber==0 / IsFakeForeignAudioScanTrack / libhb Subtitle.Search)
    # a externí SRT/SSA soubory (SubtitleType 7/8, SrtPath, Import) – nikdy se nesmí přeložit jako vložená stopa -s 1!
    respect_burn = str(burn_in_mode or "").strip().lower() == "respect"
    r_subs = []
    ext_srt_tracks = []
    ext_ssa_tracks = []
    fas_entry = None
    sub_search = (job_node.get("Subtitle") or {}).get("Search") if isinstance(job_node.get("Subtitle"), dict) else None
    if isinstance(sub_search, dict) and sub_search.get("Enable"):
        fas_entry = {
            "_is_fas": True,
            "Track": "scan",
            "Forced": bool(sub_search.get("Forced")),
            "Burned": bool(respect_burn and sub_search.get("Burn")),
            "Default": bool(sub_search.get("Default")),
            "Name": "",
        }
    for t in r_subs_raw:
        so_chk = t.get("SourceTrack") or t.get("ScannedTrack") or {}
        imp = t.get("Import") if isinstance(t.get("Import"), dict) else None
        st_type = t.get("SubtitleType")
        if st_type in (7, 8) or t.get("SrtPath") or imp:
            ext_p = str(t.get("SrtPath") or (imp.get("Filename") if imp else "") or "").strip()
            ext_fmt = "ssa" if (st_type == 8 or (imp and str(imp.get("Format") or "").upper() == "SSA") or ext_p.lower().endswith((".ssa", ".ass"))) else "srt"
            if ext_p and os.path.isfile(ext_p):
                ext_rec = {
                    "path": ext_p,
                    "codeset": str(t.get("SrtCharCode") or (imp.get("Codeset") if imp else "") or "UTF-8"),
                    "lang": str(t.get("SrtLangCode") or t.get("LanguageCode") or (imp.get("Language") if imp else "") or "und"),
                    "offset": int(t.get("SrtOffset") or t.get("Offset") or 0),
                    "burn": bool(respect_burn and subtitle_is_burned(t)),
                    "default": bool(t.get("Default")),
                }
                (ext_ssa_tracks if ext_fmt == "ssa" else ext_srt_tracks).append(ext_rec)
            continue
        is_fas = bool(so_chk.get("IsFakeForeignAudioScanTrack")) or (
            not is_libhb_s and (t.get("Track") == 0 or (t.get("Track") is None and so_chk.get("TrackNumber") == 0))
        )
        if is_fas:
            if fas_entry is None:
                fas_entry = {
                    "_is_fas": True,
                    "Track": "scan",
                    "Forced": bool(t.get("Forced")),
                    "Burned": bool(respect_burn and subtitle_is_burned(t)),
                    "Default": bool(t.get("Default")),
                    "Name": "",
                }
            continue
        r_subs.append(t)

    used_premux_subs = set()
    orig_to_premux_sub = {}
    fallback_sub_lang_ord = {}
    for t in r_subs:
        if is_libhb_s and t.get("Track") is not None and "SourceTrack" not in t and "ScannedTrack" not in t:
            zi = int(t["Track"])
            t["Track"] = zi + 1
            if 0 <= zi < len(t_slist) and isinstance(t_slist[zi], dict):
                t["SourceTrack"] = copy.deepcopy(t_slist[zi])
                sf = str(t["SourceTrack"].get("Format") or t["SourceTrack"].get("SourceName") or "").lower()
                t["CanBeForced"] = ("bitmap" in sf or "pgs" in sf or "vobsub" in sf or bool(t["SourceTrack"].get("CanForce", True)))
            elif "CanBeForced" not in t:
                t["CanBeForced"] = True

        if is_premux_src and premux_subs:
            so = t.get("SourceTrack") or t.get("ScannedTrack") or {}
            orig_st_n = int(t.get("Track") or so.get("TrackNumber") or 1)
            # Pokud uživatel v GUI přidal stejnou zdrojovou stopu dvakrát (např. Forced + Full ze stopy 3), použijeme stejnou Pre-Mux stopu
            if orig_st_n in orig_to_premux_sub:
                mapped_n = orig_to_premux_sub[orig_st_n]
                t["Track"] = mapped_n
                if isinstance(so, dict):
                    so["TrackNumber"] = mapped_n
                continue
            s_desc = str(so.get("Name") or so.get("Language") or t.get("Name") or "")
            s_lang = _norm_lang_group(str(so.get("LanguageCode") or so.get("Language") or ""), s_desc)
            s_fam = _norm_codec_family(str(so.get("Format") or so.get("SourceName") or ""), "")
            if orig_st_n in src_sub_ord_map:
                exp_s_ord = src_sub_ord_map[orig_st_n]
            else:
                exp_s_ord = fallback_sub_lang_ord.get(s_lang, 0)
                fallback_sub_lang_ord[s_lang] = exp_s_ord + 1
            so_title = str(so.get("Name") or "")
            exp_s_forced = bool(so.get("Forced")) or ("forced" in so_title.lower())
            matched_s = match_premux_sub_stream(
                s_lang, s_fam, orig_st_n, premux_subs, used_premux_subs,
                exp_lang_ord=exp_s_ord, exp_forced=exp_s_forced
            )
            if matched_s:
                used_premux_subs.add(matched_s["hb_track"])
                orig_to_premux_sub[orig_st_n] = matched_s["hb_track"]
                t["Track"] = matched_s["hb_track"]
                if isinstance(so, dict):
                    so["TrackNumber"] = matched_s["hb_track"]

    ex_f_nums = set()
    for t in r_subs:
        if t.get("Forced", False):
            st_n = t.get("Track") or (t.get("SourceTrack") or {}).get("TrackNumber") or (t.get("ScannedTrack") or {}).get("TrackNumber") or 1
            ex_f_nums.add(st_n)

    exp_subs = []
    if fas_entry is not None:
        exp_subs.append(fas_entry)
    for t in r_subs:
        t["Burned"] = bool(respect_burn and subtitle_is_burned(t))   # R1: Burn In z GUI jen v režimu „respect“
        so = t.get("SourceTrack") or t.get("ScannedTrack") or {}
        st_n = t.get("Track") or so.get("TrackNumber") or 1
        cf = t.get("CanBeForced") or so.get("CanForce", False)

        if auto_extract_forced_subs and cf and not t.get("Forced", False) and st_n not in ex_f_nums:
            tf = copy.deepcopy(t)
            tf["Forced"] = True
            tf["Default"] = False
            tf["Burned"] = False        # přidaná kopie jen-forced se nikdy nevypaluje
            on = t.get("Name") or t.get("TrackName") or so.get("Name") or ""
            tf["Name"] = f"{on} (Forced)".strip() if on else "Forced"
            exp_subs.append(tf)
            ex_f_nums.add(st_n)

        exp_subs.append(t)

    srt_subs = []
    for t in exp_subs:
        if t.get("_is_fas"):
            srt_subs.insert(0, t)
            continue
        st_n = t.get("Track") or (t.get("SourceTrack") or {}).get("TrackNumber") or (t.get("ScannedTrack") or {}).get("TrackNumber") or 1
        isf = t.get("Forced", False)
        ins = False
        if isf:
            for i, ex in enumerate(srt_subs):
                if ex.get("_is_fas"):
                    continue
                ex_s = ex.get("Track") or (ex.get("SourceTrack") or {}).get("TrackNumber") or (ex.get("ScannedTrack") or {}).get("TrackNumber") or 1
                if ex_s == st_n and not ex.get("Forced", False):
                    srt_subs.insert(i, t)
                    ins = True
                    break
        if not ins:
            srt_subs.append(t)

    s_tr, s_bn, s_fc, s_nm = [], [], [], []
    def_i = None
    for i, t in enumerate(srt_subs):
        if t.get("_is_fas"):
            s_tr.append("scan")
            if t.get("Burned"):
                s_bn.append(str(i + 1))
            if t.get("Forced"):
                s_fc.append(str(i + 1))
            if t.get("Default") and def_i is None:
                def_i = i + 1
            s_nm.append("")
            continue
        so = t.get("SourceTrack") or t.get("ScannedTrack") or {}
        st_n = t.get("Track") or so.get("TrackNumber") or 1
        s_tr.append(str(st_n))
        if t.get("Burned"):
            s_bn.append(str(i + 1))
        if t.get("Forced"):
            s_fc.append(str(i + 1))
        if t.get("Default") and def_i is None:
            def_i = i + 1
        sn = t.get("Name") or t.get("TrackName") or so.get("Name") or ""
        s_nm.append(str(sn).replace(",", " ").replace('"', '').rstrip("\\").strip())

    if s_tr:
        cli.extend(["-s", ",".join(s_tr)])
        if s_bn:
            cli.append(f"--subtitle-burned={s_bn[0]}")
        if s_fc:
            cli.append(f"--subtitle-forced={','.join(s_fc)}")
        cli.append(f"--subtitle-default={def_i}" if def_i is not None else "--subtitle-default=none")
        if any(n != "" for n in s_nm):
            cli.extend(["--subname", ",".join(s_nm)])
    elif not ext_srt_tracks and not ext_ssa_tracks:
        cli.extend(["-s", "none"])

    if ext_srt_tracks:
        cli.extend(["--srt-file", ",".join(x["path"] for x in ext_srt_tracks)])
        cli.extend(["--srt-codeset", ",".join(x["codeset"] for x in ext_srt_tracks)])
        cli.extend(["--srt-lang", ",".join(x["lang"] for x in ext_srt_tracks)])
        if any(x["offset"] != 0 for x in ext_srt_tracks):
            cli.extend(["--srt-offset", ",".join(str(x["offset"]) for x in ext_srt_tracks)])
        for idx_e, x in enumerate(ext_srt_tracks, 1):
            if x["burn"]:
                cli.append(f"--srt-burn={idx_e}")
                break
        for idx_e, x in enumerate(ext_srt_tracks, 1):
            if x["default"]:
                cli.append(f"--srt-default={idx_e}")
                break

    if ext_ssa_tracks:
        cli.extend(["--ssa-file", ",".join(x["path"] for x in ext_ssa_tracks)])
        cli.extend(["--ssa-lang", ",".join(x["lang"] for x in ext_ssa_tracks)])
        if any(x["offset"] != 0 for x in ext_ssa_tracks):
            cli.extend(["--ssa-offset", ",".join(str(x["offset"]) for x in ext_ssa_tracks)])
        for idx_e, x in enumerate(ext_ssa_tracks, 1):
            if x["burn"]:
                cli.append(f"--ssa-burn={idx_e}")
                break
        for idx_e, x in enumerate(ext_ssa_tracks, 1):
            if x["default"]:
                cli.append(f"--ssa-default={idx_e}")
                break

    inc_m = job_node.get("IncludeChapterMarkers")
    if inc_m is None and isinstance(dst_d.get("ChapterMarkers"), bool):
        inc_m = dst_d.get("ChapterMarkers")
    cli.append("--markers" if (inc_m is None or inc_m) else "--no-markers")

    return cli