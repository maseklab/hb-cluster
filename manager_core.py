# ==============================================================================
# HB CLUSTER MANAGER - CORE LOGIC, STORAGE GUARD & LOG HUB (manager_core.py)
# ==============================================================================

import os
import re
import ast
import sys
import math
import time
import json
import copy
import queue
import shutil
import socket
import struct
import hashlib
import logging
import datetime
import threading
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
import requests
from collections import deque, OrderedDict
from sqlalchemy import text, func, Column, Integer, BigInteger, Float, String, Boolean, DateTime
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.exc import IntegrityError

try:
    os.umask(0o027)
except Exception:
    pass

SAFE_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")


def is_safe_version_string(val) -> bool:
    s = str(val or "").strip().lstrip("v")
    return bool(s and SAFE_VERSION_RE.match(s))

import models
from database import engine, SessionLocal, DATA_DIR, set_tx_immediate, run_sqlite_pragma

_WORKERS_DIR_FOR_IMPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workers")
if _WORKERS_DIR_FOR_IMPORT not in sys.path:
    sys.path.insert(0, _WORKERS_DIR_FOR_IMPORT)
import docker_selfupdate as dsu  # noqa: E402  (sdílený GHCR / docker.sock modul ve workers/)

FALLBACK_VERSION = "2.12.0"  # záloha pro chybějící VERSION, udržuje scripts/bump_version.py


def _load_app_version(fallback: str = FALLBACK_VERSION) -> str:
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


APP_VERSION = _load_app_version()
MANAGER_VERSION = APP_VERSION
WORKER_VERSION = APP_VERSION
VERSION = APP_VERSION
UDP_DISCOVERY_PORT = 8383
MANAGER_ID = ""   # ID tohoto Manageru pro odpověď UDP discovery – nastavuje main.py (mc.MANAGER_ID)

VALID_POWER_MODES = ("AUTO", "FULL", "ECO", "NIGHT", "HYBRID_ECO", "CUSTOM")
VALID_TELEMETRY_SOURCES = ("system", "hb", "both")
VALID_RAM_FORMATS = ("gb_ratio", "gb_pct", "pct_gb", "pct_only")


def parse_custom_cores_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                val = json.loads(s)
            except Exception:
                val = [x.strip() for x in s.strip("[]").split(",") if x.strip()]
        else:
            out_s = []
            for part in s.split(","):
                p = part.strip()
                if not p:
                    continue
                if "-" in p:
                    a, _, b = p.partition("-")
                    if a.strip().isdigit() and b.strip().isdigit():
                        s_i, e_i = int(a.strip()), int(b.strip())
                        if 0 <= s_i <= e_i <= 1024:
                            out_s.extend(range(s_i, e_i + 1))
                elif p.isdigit():
                    out_s.append(int(p))
            return sorted(set(x for x in out_s if 0 <= x < 1024))
    if not isinstance(val, (list, tuple, set)):
        return []
    out = []
    for item in val:
        try:
            idx = int(item)
            if 0 <= idx < 1024:
                out.append(idx)
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def serialize_custom_cores_list(val) -> str:
    lst = parse_custom_cores_list(val)
    return json.dumps(lst) if lst else ""

_INVISIBLE_WS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\u00a0]+")


def sanitize_path(val) -> str:
    """Odstraní neviditelné znaky, mezery a okolní uvozovky z cesty nebo složky."""
    s = _INVISIBLE_WS_RE.sub("", str(val or "")).replace("\r", "").replace("\n", "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def sanitize_manager_url(val) -> str:
    """Očistí URL Managera, odstraní koncové lomítko a případně doplní http://."""
    s = sanitize_path(val)
    if not s:
        return ""
    if not re.match(r"^https?://", s, re.IGNORECASE):
        s = "http://" + s.lstrip("/")
    return s.rstrip("/")


def sanitize_display_name(val, default: str = "") -> str:
    """Očistí zobrazované jméno od řídicích/neviditelných znaků a okolních mezer."""
    s = _INVISIBLE_WS_RE.sub("", str(val or "")).replace("\r", "").replace("\n", "").strip()
    return s if s else default


def slugify_worker_name(val, default: str = "worker") -> str:
    """Převede název workeru na bezpečný systémový identifikátor [a-zA-Z0-9_-]."""
    s = sanitize_display_name(val, default)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", s).strip("-")
    return slug or default


def clamp_num(val, min_v, max_v, default=0, as_int: bool = False):
    """Bezpečně převede číslo (hlídá None, prázdný string, NaN, inf) a omezí na [min_v, max_v]."""
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
    """Bezpečné parsování JSON řetězce / bytes bez pádu při prázdném či poškozeném vstupu."""
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

COMPUTE_ACTIVE_STATUSES = ["IN_PROGRESS", "STAGING", "PREMUX", "COPYING"]
ACTIVE_JOB_STATUSES = ["IN_PROGRESS", "STAGING", "PREMUX", "COPYING", "WAITING_DISK"]
ALL_TOOLS = ("hbcli", "ffmpeg", "ffprobe", "mkvpropedit", "mkvmerge", "7z")

APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKERS_DIR = os.path.join(APP_BASE_DIR, "workers")
CONFIG_FILE = os.path.join(APP_BASE_DIR, "manager_config.json")
LOCALES_JS_FILE = os.path.join(APP_BASE_DIR, "locales.js")
# D3: vždy v datové složce Manageru (HB_DATA_DIR / /data) – dřív mimo Docker v /app (zmizelo při nové verzi)
MEDIA_CACHE_FILE = os.path.join(DATA_DIR, "media_cache.json")
MANAGER_LOG_FILE = os.path.join(DATA_DIR, "cluster_manager.log")
WATCH_DIR = "/watch_dir"


def atomic_write_json(path_str: str, payload, mode: int = 0o600, indent=2):
    """Atomický zápis JSON (unikátní .tmp + flush + fsync + chmod + os.replace)."""
    parent = os.path.dirname(os.path.abspath(path_str))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_p = f"{path_str}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        fd = os.open(tmp_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        try:
            os.chmod(tmp_p, mode)
        except Exception:
            pass
        os.replace(tmp_p, path_str)
    except Exception:
        try:
            if os.path.exists(tmp_p):
                os.remove(tmp_p)
        except Exception:
            pass
        raise


def _lru_set(cache: OrderedDict, key, val, maxlen: int):
    cache[key] = val
    cache.move_to_end(key)
    while len(cache) > maxlen:
        cache.popitem(last=False)


# ==============================================================================
# 0. OCHRANA SQLITE PROTI ZAMKNUTÍ (WAL REŽIM + EXPLICITNÍ QueuePool v database.py)
# ==============================================================================
# v2.11.2: PRAGMA (WAL, synchronous, busy_timeout) nastavuje jediný connect-listener
# v database.py. Duplicitní listener a přepisování privátních atributů poolu
# (engine.pool._max_overflow / _timeout) byly odstraněny.
def configure_database_pool_and_wal():
    try:
        run_sqlite_pragma("PRAGMA journal_mode=WAL")
    except Exception:
        pass


def safe_chmod_777(path_str: str):
    if path_str and os.path.exists(path_str):
        try:
            os.chmod(path_str, 0o777)
        except Exception:
            pass


def ensure_shared_dir(path_str: str) -> str:
    """Složka pro uživatele, Plex a SMB mimo /data (podsložka „ke kontrole“ v Outputs, /watch_dir).
    Přísná umask 027 má chránit jen soubory Manageru v /data – složku, kterou Manager nově založí tady,
    otevře jako dřív (0777), aby do ní viděli i jiní uživatelé. Existující složky uživatele nemění."""
    if not path_str:
        return path_str
    existed = os.path.isdir(path_str)
    os.makedirs(path_str, exist_ok=True)
    if not existed:
        safe_chmod_777(path_str)
    return path_str


def enforce_open_permissions():
    # v2.11.2: /app je neměnný obraz (bez chmod) a soubory s citlivými údaji
    # (manager_config.json se SMB heslem, SQLite DB) už nedostávají 0o777.
    # v2.12.0: tajné soubory a složky (security.json, worker_keys.json, manager_config.json,
    # cluster.db*, certs, backup) se zamykají na 0600 / 0700; binaries jen 0755 (nikdy 0777).
    private_files = ("security.json", "worker_keys.json", "manager_config.json", "cluster.db")
    private_dirs = ("certs", "backup")
    ro_exec_dirs = ("binaries",)
    for folder in (DATA_DIR,):
        if os.path.isdir(folder):
            try:
                os.chmod(folder, 0o755)
            except Exception:
                pass
            try:
                for fn in os.listdir(folder):
                    fp = os.path.join(folder, fn)
                    if fn.startswith(private_files) or fn in private_dirs:
                        try:
                            os.chmod(fp, 0o700 if os.path.isdir(fp) else 0o600)
                        except Exception:
                            pass
                        continue
                    if fn in ro_exec_dirs and os.path.isdir(fp):
                        try:
                            os.chmod(fp, 0o755)
                        except Exception:
                            pass
                        continue
                    try:
                        os.chmod(fp, 0o755 if os.path.isdir(fp) else 0o644)
                    except Exception:
                        pass
            except Exception:
                pass


GHCR_MANAGER_REPO = "ghcr.io/maseklab/hb-manager"
GHCR_WORKER_REPO = "ghcr.io/maseklab/hb-worker"
GITHUB_PROJECT_URL = "https://github.com/maseklab/hb-cluster"
KOFI_URL = "https://ko-fi.com/maseklab"          # pevná adresa podpory (není editovatelná v UI)
STATIC_DIR = os.path.join(APP_BASE_DIR, "static")  # loga a ikony (HBC / HBM / HBW)

_update_channel_cache = {"value": None}


def get_update_channel() -> str:
    """'latest' (větev main) nebo 'dev' (větev dev) – podle tagu obrazu, ze kterého Manager běží."""
    if _update_channel_cache["value"] is None:
        ch = dsu.detect_own_channel(user_agent=f"HB-Cluster-Manager/{MANAGER_VERSION}")
        # Cachuje se jen jistý výsledek (env override, nebo úspěšný docker inspect).
        if os.getenv("HB_UPDATE_CHANNEL", "").strip() or dsu.docker_sock_available():
            _update_channel_cache["value"] = ch
        return ch
    return _update_channel_cache["value"]


def get_manager_image() -> str:
    return dsu.channel_image_ref(GHCR_MANAGER_REPO, get_update_channel())


def get_worker_image() -> str:
    return dsu.channel_image_ref(GHCR_WORKER_REPO, get_update_channel())


VALID_ASS_SRT_MODES = ("replace", "keep_both")
VALID_BURN_IN_MODES = ("respect", "separate")
VALID_DTS_HD_MODES = ("keep_both", "merge")


class AppState:
    ui_lang = "en"
    onboarding_done = False
    maintenance_mode = False
    auto_clean_history = False
    convert_ass_to_srt = False
    ass_srt_mode = "keep_both"   # při zapnutém převodu: "replace" = ASS nahradit SRT, "keep_both" = ponechat ASS a přidat SRT
    burn_in_subs = "respect"     # R1: titulky s Burn In v HandBrake GUI: "respect" = vypálit do obrazu, "separate" = samostatná stopa
    dts_hd_mode = "keep_both"    # DTS-HD MA a DTS jádro: "keep_both" = dle HandBrake GUI, "merge" = sloučit do 1 stopy DTS-HD MA
    auto_clean_empty_subs = True
    auto_extract_forced_subs = True
    write_mkv_stats = True
    lpcm_passthru = True
    smart_premux = False  # beta – po instalaci vypnuto
    ineffective_action = "both"
    ineffective_prefix = "[HIGHER_BITRATE]_"
    ineffective_subfolder = "_HIGHER_BITRATE_THAN_SOURCE"
    preferred_builder_worker = "AUTO"
    auto_build_on_first_run = True
    auto_update_binaries = True
    auto_update_cluster = True
    default_staging_mode = "Všechny formáty"
    default_power_mode = "AUTO"
    default_use_local_temp = True
    default_prioritize_4k = False
    min_free_output_gb = 10.0
    continue_encoding_on_disk_full = True
    telemetry_source = "both"
    worker_cpu_source = "both"
    worker_ram_source = "both"
    ram_format = "gb_ratio"
    support_url = "https://ko-fi.com/maseklab"  # konstanta, v Nastavení se needituje
    binary_generation = 1
    worker_policies_backup = {}
    worker_windows_settings = {}
    pending_worker_windows_settings = {}
    ui_prefs = {}   # nastavení webového rozhraní (sdílená pro všechny prohlížeče), viz UI_PREF_DEFAULTS
    win_default_inputs_dir = ""
    win_default_outputs_dir = ""
    smb_username = ""
    smb_password = ""
    linux_default_inputs_dir = "/mnt/nas/inputs"
    linux_default_outputs_dir = "/mnt/nas/outputs"
    linux_default_temp_dir = "/mnt/nvme/hb_temp"


_locales_cache = {"mtime": 0.0, "checked_at": 0.0, "data": {"en": {}, "cs": {}}}
_locales_lock = threading.RLock()


def load_unified_locales_dict(force: bool = False) -> dict:
    now_t = time.time()
    if not force and _locales_cache["data"].get("en") and (now_t - _locales_cache["checked_at"]) < 30.0:
        return _locales_cache["data"]
    with _locales_lock:
        now_t = time.time()
        if not force and _locales_cache["data"].get("en") and (now_t - _locales_cache["checked_at"]) < 30.0:
            return _locales_cache["data"]
        _locales_cache["checked_at"] = now_t
        if not os.path.isfile(LOCALES_JS_FILE):
            return _locales_cache["data"]
        try:
            mt = os.path.getmtime(LOCALES_JS_FILE)
            if not force and mt == _locales_cache["mtime"] and _locales_cache["data"].get("en"):
                return _locales_cache["data"]
            with open(LOCALES_JS_FILE, "r", encoding="utf-8-sig") as f:
                txt = f.read()
            idx = txt.find("window.HB_LOCALES")
            if idx != -1:
                b_start = txt.find("{", idx)
                b_end = txt.rfind("}")
                if b_start != -1 and b_end != -1:
                    raw_block = re.sub(r'(?m)^\s*//.*$', '', txt[b_start:b_end + 1])
                    py_obj = re.sub(r'(?m)^(\s*)([a-zA-Z0-9_]+)\s*:', r'\1"\2":', raw_block)
                    parsed = ast.literal_eval(py_obj)
                    if isinstance(parsed, dict):
                        _locales_cache["mtime"] = mt
                        _locales_cache["data"] = parsed
        except Exception:
            pass
        return _locales_cache["data"]


def T(key: str, *args, **kwargs) -> str:
    d = load_unified_locales_dict()
    lang = getattr(AppState, "ui_lang", "en")
    tmpl = (d.get(lang) or {}).get(key) or (d.get("en") or {}).get(key) or kwargs.get("default") or key
    if args or kwargs:
        try:
            return str(tmpl).format(*args, **kwargs)
        except Exception:
            return str(tmpl)
    return str(tmpl)


def L(cs_txt: str, en_txt: str) -> str:
    return cs_txt if getattr(AppState, "ui_lang", "en") == "cs" else en_txt


# ==============================================================================
# 1. CENTRÁLNÍ LOGOVACÍ HUB (MANAGER + SPOJENÝ LOG VŠECH WORKERŮ) & UDP DISCOVERY
# ==============================================================================
manager_log_buffer = deque(maxlen=500)
worker_logs_store = {}
combined_worker_logs = deque(maxlen=800)
logs_lock = threading.Lock()


class RingBufferLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            with logs_lock:
                manager_log_buffer.append(msg)
            try:
                if os.path.isfile(MANAGER_LOG_FILE) and os.path.getsize(MANAGER_LOG_FILE) > 5 * 1024 * 1024:
                    with open(MANAGER_LOG_FILE, "rb") as rf:
                        rf.seek(-2 * 1024 * 1024, os.SEEK_END)
                        tail_bytes = rf.read()
                    nl_idx = tail_bytes.find(b"\n")
                    if nl_idx != -1:
                        tail_bytes = tail_bytes[nl_idx + 1:]
                    with open(MANAGER_LOG_FILE, "wb") as wf:
                        wf.write(tail_bytes)
                with open(MANAGER_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
                safe_chmod_777(MANAGER_LOG_FILE)
            except Exception:
                pass
        except Exception:
            pass


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HBClusterManager")

_ring_handler = RingBufferLogHandler()
_ring_handler.setFormatter(logging.Formatter("[%(asctime)s] [MANAGER] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
if not any(isinstance(h, RingBufferLogHandler) for h in logger.handlers):
    logger.addHandler(_ring_handler)


def append_manager_log(msg: str):
    logger.info(msg)


def _format_worker_log_line_with_name(worker_name: str, raw_line: str) -> str:
    s = str(raw_line or "").strip()
    if not s:
        return ""
    tag = f"[{worker_name}]"
    if tag in s:
        return s
    if s.startswith("[") and "]" in s:
        idx = s.find("]")
        return f"{s[:idx + 1]} {tag} {s[idx + 1:].lstrip()}"
    return f"{tag} {s}"


def append_worker_logs(worker_name: str, lines: list):
    if not worker_name or not lines or not isinstance(lines, list):
        return
    with logs_lock:
        buf = worker_logs_store.setdefault(worker_name, deque(maxlen=500))
        for ln in lines:
            formatted = _format_worker_log_line_with_name(worker_name, ln)
            if formatted and (not buf or buf[-1] != formatted):
                buf.append(formatted)
                if not combined_worker_logs or combined_worker_logs[-1] != formatted:
                    combined_worker_logs.append(formatted)


def get_worker_logs_snapshot(worker_name: str = "ALL") -> dict:
    sel = (worker_name or "ALL").strip()
    with logs_lock:
        mgr_lines = list(manager_log_buffer)
        w_names = sorted(worker_logs_store.keys())
        if sel in ("", "ALL"):
            w_lines = list(combined_worker_logs)
            if not w_lines and worker_logs_store:
                merged = [ln for wn in w_names for ln in worker_logs_store.get(wn, [])]
                merged.sort()
                w_lines = merged[-800:]
        else:
            w_lines = list(worker_logs_store.get(sel, []))
    return {
        "manager_logs": mgr_lines,
        "workers_with_logs": w_names,
        "selected_worker": sel or "ALL",
        "worker_logs": w_lines,
    }


def clear_logs_buffer(target: str = "all"):
    with logs_lock:
        if target in ("all", "manager"):
            manager_log_buffer.clear()
            try:
                if os.path.isfile(MANAGER_LOG_FILE):
                    open(MANAGER_LOG_FILE, "w", encoding="utf-8").close()
                    safe_chmod_777(MANAGER_LOG_FILE)
            except Exception:
                pass
        if target in ("all", "ALL", "workers"):
            worker_logs_store.clear()
            combined_worker_logs.clear()
        elif target in worker_logs_store:
            worker_logs_store[target].clear()
            tag = f"[{target}]"
            kept = [ln for ln in combined_worker_logs if tag not in ln]
            combined_worker_logs.clear()
            combined_worker_logs.extend(kept)


def udp_discovery_responder_loop():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", UDP_DISCOVERY_PORT))
    except Exception as e:
        logger.warning(f"UDP Discovery responder error on port {UDP_DISCOVERY_PORT}: {e}")
        return

    last_reply = {}   # ip -> ts (max 1 odpověď / s na IP)
    last_log = {}     # ip -> ts (max 1 zápis do logu / min na IP)
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            msg = (data or b"").decode("utf-8", errors="ignore").strip()
            if msg.startswith("HB_DISCOVER_MANAGER_V1"):
                ip = str(addr[0] if addr else "")
                now_t = time.time()
                if now_t - last_reply.get(ip, 0.0) < 1.0:
                    continue
                if len(last_reply) > 1024:
                    last_reply.clear()
                    last_log.clear()
                last_reply[ip] = now_t
                reply = json.dumps({
                    "magic": "HB_MANAGER_HERE",
                    "port": UDP_DISCOVERY_PORT,
                    "version": MANAGER_VERSION,
                    "ui_lang": AppState.ui_lang,
                    "manager_id": MANAGER_ID
                }).encode("utf-8")
                sock.sendto(reply, addr)
                if now_t - last_log.get(ip, 0.0) >= 60.0:
                    last_log[ip] = now_t
                    append_manager_log(
                        T("mgr_udp_discovery_answered_discovery", ip)
                    )
        except Exception:
            time.sleep(1)


# ==============================================================================
# 2. AUTOMATICKÁ MIGRACE DATABÁZE A ORM MODELŮ
# ==============================================================================
DB_MIGRATION_COLS = [
    ("worker_policies", "power_mode", "VARCHAR DEFAULT 'AUTO'"),
    ("worker_policies", "custom_cores", "VARCHAR DEFAULT ''"),
    ("worker_policies", "allow_4k", "BOOLEAN DEFAULT 1"),
    ("jobs", "worker_version", "VARCHAR"),
    ("jobs", "fps_current", "FLOAT DEFAULT 0.0"),
    ("jobs", "fps_avg", "FLOAT DEFAULT 0.0"),
    ("jobs", "speed_mbps", "FLOAT DEFAULT 0.0"),
    ("jobs", "elapsed_sec", "INTEGER DEFAULT 0"),
    ("jobs", "eta_sec", "INTEGER DEFAULT 0"),
    ("jobs", "transferred_bytes", "BIGINT DEFAULT 0"),
    ("jobs", "is_net_fallback", "BOOLEAN DEFAULT 0"),
    ("jobs", "net_fallback_reason", "VARCHAR"),
    ("jobs", "src_size_bytes", "BIGINT DEFAULT 0"),
    ("jobs", "out_size_bytes", "BIGINT DEFAULT 0"),
    ("jobs", "duration_sec", "INTEGER DEFAULT 0"),
    ("jobs", "completed_at", "DATETIME"),
    ("jobs", "subs_removed", "INTEGER DEFAULT 0"),
    ("jobs", "subs_status", "VARCHAR"),
    ("jobs", "error_code", "VARCHAR"),
    ("jobs", "error_detail", "VARCHAR"),
    ("jobs", "res_in", "VARCHAR"),
    ("jobs", "res_out", "VARCHAR"),
    ("jobs", "video_duration_sec", "FLOAT DEFAULT 0.0"),
    ("jobs", "preset_name", "VARCHAR"),
    ("jobs", "encoder_summary", "VARCHAR"),
    ("jobs", "video_encoder", "VARCHAR"),
    ("jobs", "video_preset", "VARCHAR"),
    ("jobs", "video_rf", "FLOAT"),
    ("jobs", "audio_count", "INTEGER DEFAULT 0"),
    ("jobs", "subs_count", "INTEGER DEFAULT 0"),
    ("jobs", "v_bitrate_in_kbps", "INTEGER DEFAULT 0"),
    ("jobs", "a_bitrate_in_kbps", "INTEGER DEFAULT 0"),
    ("jobs", "v_bitrate_out_kbps", "INTEGER DEFAULT 0"),
    ("jobs", "a_bitrate_out_kbps", "INTEGER DEFAULT 0"),
    ("jobs", "mkv_stats_written", "BOOLEAN DEFAULT 0"),
    ("jobs", "lpcm_Tracks_muxed", "INTEGER DEFAULT 0"),
    ("jobs", "premux_applied", "BOOLEAN DEFAULT 0"),
    ("jobs", "premux_status", "VARCHAR DEFAULT ''"),
    ("jobs", "disk_hold_reason", "VARCHAR DEFAULT ''"),
    ("jobs", "is_paused", "BOOLEAN DEFAULT 0"),
    ("jobs", "dup_key", "VARCHAR"),
    ("jobs", "is_disc_source", "BOOLEAN DEFAULT 0"),
    ("jobs", "burned_subs", "INTEGER DEFAULT -1"),
    ("encoding_stats", "avg_v_bitrate_in_kbps", "INTEGER DEFAULT 0"),
    ("encoding_stats", "avg_v_bitrate_out_kbps", "INTEGER DEFAULT 0"),
]

DB_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_priority ON jobs (priority)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_worker_name ON jobs (worker_name)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_completed_at ON jobs (completed_at)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_preset_name ON jobs (preset_name)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_dup_key ON jobs (dup_key)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_status_priority ON jobs (status, priority, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_status_completed ON jobs (status, completed_at)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_status_worker ON jobs (status, worker_name)",
]


def init_database_and_migrations():
    models.Base.metadata.create_all(bind=engine)
    added_cols = set()
    with engine.connect() as conn:
        for tbl_name, col_n, col_spec in DB_MIGRATION_COLS:
            try:
                conn.execute(text(f"ALTER TABLE {tbl_name} ADD COLUMN {col_n} {col_spec}"))
                conn.commit()
                added_cols.add((tbl_name, col_n))
            except Exception:
                pass
        for idx_sql in DB_MIGRATION_INDEXES:
            try:
                conn.execute(text(idx_sql))
                conn.commit()
            except Exception:
                pass
        if ("worker_policies", "allow_4k") in added_cols:
            try:
                conn.execute(text("UPDATE worker_policies SET allow_4k = CASE WHEN capability IN ('1080p', 'sd', '720p') THEN 0 ELSE 1 END"))
                conn.commit()
            except Exception:
                pass
        try:
            conn.execute(text("CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, val VARCHAR)"))
            row_m = conn.execute(text("SELECT val FROM schema_meta WHERE key = 'weighted_slots_3tier_v1'")).fetchone()
            if not row_m:
                conn.execute(text(
                    "UPDATE worker_policies SET max_slots = 4 "
                    "WHERE COALESCE(max_slots, 1) IN (1, 2) "
                    "AND COALESCE(allow_4k, 1) = 1 "
                    "AND LOWER(COALESCE(capability, 'all')) NOT IN ('1080p', 'sd', '720p')"
                ))
                conn.execute(text(
                    "UPDATE worker_policies SET max_slots = 2 "
                    "WHERE COALESCE(max_slots, 1) = 1 "
                    "AND (LOWER(COALESCE(capability, 'all')) = '1080p' "
                    "OR (COALESCE(allow_4k, 1) = 0 AND LOWER(COALESCE(capability, 'all')) NOT IN ('sd', '720p')))"
                ))
                conn.execute(text("INSERT OR REPLACE INTO schema_meta (key, val) VALUES ('weighted_slots_3tier_v1', '1')"))
                conn.commit()
        except Exception:
            pass
        for legacy_col in ("fps", "eta"):
            try:
                conn.execute(text(f"ALTER TABLE jobs DROP COLUMN {legacy_col}"))
                conn.commit()
            except Exception:
                pass

    col_types = {
        "VARCHAR": (String, ""),
        "FLOAT": (Float, 0.0),
        "INTEGER": (Integer, 0),
        "BIGINT": (BigInteger, 0),
        "BOOLEAN": (Boolean, False),
        "DATETIME": (DateTime, None),
    }
    model_map = {
        "worker_policies": getattr(models, "WorkerPolicy", None),
        "jobs": getattr(models, "Job", None),
        "encoding_stats": getattr(models, "EncodingStats", None),
    }
    for tbl_name, col_n, col_spec in DB_MIGRATION_COLS:
        cls = model_map.get(tbl_name)
        if cls is None:
            continue
        attr = getattr(cls, col_n, None)
        if not isinstance(attr, InstrumentedAttribute):
            if attr is not None:
                try:
                    delattr(cls, col_n)
                except Exception:
                    pass
            base_t = col_spec.split()[0].upper()
            sa_type, def_val = col_types.get(base_t, (String, ""))
            try:
                if col_n not in cls.__table__.c:
                    c_obj = Column(col_n, sa_type, default=def_val)
                    cls.__table__.append_column(c_obj)
                    cls.__mapper__.add_property(col_n, c_obj)
                else:
                    cls.__mapper__.add_property(col_n, cls.__table__.c[col_n])
            except Exception:
                setattr(cls, col_n, def_val)


# ==============================================================================
# 3. PODSLOŽKA /app/workers/ A KONFIGURACE MANAGERA
# ==============================================================================
# v2.11.2: Zdrojové soubory v /app jsou neměnné (immutable image). Manager je
# NIKDY nepřepisuje za běhu — aktualizace probíhá výhradně výměnou Docker obrazu
# z GHCR (docker pull + recreate). Odstraněno: /data/ota_cache, raw.githubusercontent
# OTA a přepisování verzí v index.html / app.js / *.py při startu.
def ensure_workers_subdir_and_migrate():
    try:
        os.makedirs(WORKERS_DIR, exist_ok=True)
    except Exception:
        pass

    for fn in ("worker_core.py", "worker_linux.py", "worker.pyw", "worker.py"):
        root_p = os.path.join(APP_BASE_DIR, fn)
        sub_p = os.path.join(WORKERS_DIR, fn)
        if os.path.isfile(root_p):
            try:
                if not os.path.isfile(sub_p) or os.path.getmtime(root_p) >= os.path.getmtime(sub_p):
                    shutil.copy2(root_p, sub_p)
                os.remove(root_p)
            except Exception:
                pass

    if os.path.isfile(LOCALES_JS_FILE):
        try:
            w_loc = os.path.join(WORKERS_DIR, "locales.js")
            need_copy = True
            if os.path.isfile(w_loc):
                with open(LOCALES_JS_FILE, "rb") as a, open(w_loc, "rb") as b:
                    need_copy = a.read() != b.read()
            if need_copy:
                shutil.copy2(LOCALES_JS_FILE, w_loc)
        except Exception:
            pass


ensure_workers_subdir_and_migrate()


def resolve_worker_script_file(filename: str) -> str:
    sub_p = os.path.join(WORKERS_DIR, filename)
    root_p = os.path.join(APP_BASE_DIR, filename)
    if os.path.isfile(sub_p) and os.path.isfile(root_p):
        return sub_p if os.path.getmtime(sub_p) >= os.path.getmtime(root_p) else root_p
    return sub_p if os.path.isfile(sub_p) else (root_p if os.path.isfile(root_p) else sub_p)


def get_manager_inputs_dir() -> str:
    return sanitize_path(os.getenv("INPUTS_DIR")) or "/inputs"


def get_manager_outputs_dir() -> str:
    return sanitize_path(os.getenv("OUTPUTS_DIR")) or "/outputs"


def get_local_lan_ip() -> str:
    """LAN IP Managera (UDP 'connect' neodesílá žádná data). Dříve volané z main.py, ale chybějící."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


# Nastavení webového rozhraní – ukládají se na Manager, aby byla stejná ve všech prohlížečích a na všech PC.
# Hodnota zde = výchozí stav po první instalaci.
UI_PREF_DEFAULTS = {
    "workers_placement": "auto",   # bubliny workerů: auto / header / sidebar / compact
    "sidebar_side": "right",
    "card_dest": True,             # karta úlohy: řádek s výstupním souborem
    "card_preset": True,           # karta úlohy: štítek presetu
    "card_tech": True,             # karta úlohy: technické parametry
    "enable_pagination": False,    # stránkování čekající fronty
    "show_eta": True,              # odhad času celé fronty
    "two_click": True,             # dvojklik pro Stop / Smazat
    "changelog_seen": "",          # poslední verze, jejíž historii změn uživatel otevřel
    "log_sources": [],             # Živé logy: vybrané zdroje (prázdné = vše)
    "queue_per_page": 50,          # Fronta: úloh na stránku (jen se zapnutým stránkováním)
    "history_per_page": 50,        # Dokončené + Hledání: položek na stránku
    "tips_seen": [],               # jednorázové tipy v záložkách, které už uživatel zavřel
    "logo_motion": "on",           # pohyb loga v hlavičce: on / events (jen při událostech) / off
}
UI_PER_PAGE_CHOICES = (25, 50, 100, 250, 10000)   # 10000 = „Vše“


def sanitize_ui_prefs(raw) -> dict:
    """Ponechá jen známé klíče se správným typem (bool / krátký text / seznam krátkých textů)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, dv in UI_PREF_DEFAULTS.items():
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(dv, bool):
            out[k] = bool(v)
        elif isinstance(dv, int):
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv in UI_PER_PAGE_CHOICES:
                out[k] = iv
        elif isinstance(dv, list):
            if isinstance(v, list):
                out[k] = [str(x)[:120] for x in v[:64] if isinstance(x, (str, int, float))]
        else:
            out[k] = str(v if v is not None else "")[:200]
    return out


def get_effective_ui_prefs() -> dict:
    eff = copy.deepcopy(UI_PREF_DEFAULTS)
    eff.update(sanitize_ui_prefs(getattr(AppState, "ui_prefs", {}) or {}))
    return eff


CONFIG_KEYS = (
    "ui_lang", "onboarding_done", "maintenance_mode", "auto_clean_history", "convert_ass_to_srt", "ass_srt_mode", "burn_in_subs", "dts_hd_mode", "auto_clean_empty_subs",
    "auto_extract_forced_subs", "write_mkv_stats", "lpcm_passthru", "smart_premux",
    "ineffective_action", "ineffective_prefix", "ineffective_subfolder",
    "preferred_builder_worker", "auto_build_on_first_run", "auto_update_binaries", "auto_update_cluster",
    "default_staging_mode", "default_power_mode", "default_use_local_temp",
    "default_prioritize_4k", "min_free_output_gb", "continue_encoding_on_disk_full",
    "telemetry_source", "worker_cpu_source", "worker_ram_source", "ram_format",
    "binary_generation",
    "win_default_inputs_dir", "win_default_outputs_dir", "smb_username", "smb_password",
    "linux_default_inputs_dir", "linux_default_outputs_dir", "linux_default_temp_dir"
)

PERSISTENT_CONFIG_FILE = os.path.join(DATA_DIR, "manager_config.json")
config_file_lock = threading.Lock()


def load_manager_config():
    loaded_from = None
    for cand in (PERSISTENT_CONFIG_FILE, "/data/manager_config.json", CONFIG_FILE):
        if os.path.isfile(cand):
            try:
                with open(cand, 'r', encoding='utf-8-sig') as f:
                    cfg = safe_json_loads(f.read(), default=None)
                if not isinstance(cfg, dict):
                    continue
                for k in CONFIG_KEYS:
                    if k in cfg and cfg[k] is not None:
                        def_v = getattr(AppState, k)
                        if isinstance(def_v, bool):
                            setattr(AppState, k, bool(cfg[k]))
                        elif isinstance(def_v, float):
                            setattr(AppState, k, clamp_num(cfg[k], 0.0, 1e9, def_v))
                        elif isinstance(def_v, int):
                            setattr(AppState, k, clamp_num(cfg[k], 0, 10**9, def_v, as_int=True))
                        elif k.endswith("_dir"):
                            setattr(AppState, k, sanitize_path(cfg[k]))
                        else:
                            setattr(AppState, k, sanitize_display_name(cfg[k], str(def_v)))
                if isinstance(cfg.get("worker_policies"), dict):
                    wp_b = dict(cfg["worker_policies"])
                    if not cfg.get("weighted_slots_3tier_v1"):
                        for wn, b_ent in wp_b.items():
                            if isinstance(b_ent, dict):
                                cap_l = str(b_ent.get("capability") or "all").strip().lower()
                                a4k_l = bool(b_ent.get("allow_4k", cap_l not in ("1080p", "sd", "720p"))) and cap_l not in ("1080p", "sd", "720p")
                                ms_old = b_ent.get("max_slots")
                                if a4k_l and ms_old in (None, 1, 2):
                                    b_ent["max_slots"] = 4
                                elif cap_l == "1080p" and ms_old in (None, 1):
                                    b_ent["max_slots"] = 2
                    AppState.worker_policies_backup = wp_b
                if isinstance(cfg.get("ui_prefs"), dict):
                    AppState.ui_prefs = sanitize_ui_prefs(cfg["ui_prefs"])
                if isinstance(cfg.get("worker_windows_settings"), dict):
                    AppState.worker_windows_settings = dict(cfg["worker_windows_settings"])
                if isinstance(cfg.get("pending_worker_windows_settings"), dict):
                    AppState.pending_worker_windows_settings = dict(cfg["pending_worker_windows_settings"])
                if "onboarding_done" not in cfg and bool(cfg.get("worker_policies")):
                    AppState.onboarding_done = True
                AppState.analytics_seeded = bool(cfg.get("analytics_seeded", False))
                if AppState.ui_lang not in ("en", "cs"):
                    AppState.ui_lang = "en"
                if AppState.ass_srt_mode not in VALID_ASS_SRT_MODES:
                    AppState.ass_srt_mode = "keep_both"
                if AppState.burn_in_subs not in VALID_BURN_IN_MODES:
                    AppState.burn_in_subs = "respect"
                if AppState.dts_hd_mode not in VALID_DTS_HD_MODES:
                    AppState.dts_hd_mode = "keep_both"
                if AppState.telemetry_source not in VALID_TELEMETRY_SOURCES:
                    AppState.telemetry_source = "both"
                if "worker_cpu_source" not in cfg:
                    AppState.worker_cpu_source = AppState.telemetry_source
                if "worker_ram_source" not in cfg:
                    AppState.worker_ram_source = AppState.telemetry_source
                if AppState.worker_cpu_source not in VALID_TELEMETRY_SOURCES:
                    AppState.worker_cpu_source = "both"
                if AppState.worker_ram_source not in VALID_TELEMETRY_SOURCES:
                    AppState.worker_ram_source = "both"
                if AppState.ram_format not in VALID_RAM_FORMATS:
                    AppState.ram_format = "gb_ratio"
                if AppState.default_power_mode not in VALID_POWER_MODES:
                    AppState.default_power_mode = "AUTO"
                loaded_from = cand
                break
            except Exception as e:
                logger.error(f"Config load error ({cand}): {e}")
    if loaded_from and os.path.abspath(loaded_from) != os.path.abspath(PERSISTENT_CONFIG_FILE):
        save_manager_config()


def save_manager_config(db_session=None):
    if db_session is not None:
        try:
            wp_snap = {}
            for p in db_session.query(models.WorkerPolicy).all():
                if p.worker_name:
                    cap_s = str(p.capability or "all")
                    allow_4k_v = bool(getattr(p, "allow_4k", cap_s not in ("1080p", "sd", "720p"))) and (cap_s not in ("1080p", "sd", "720p"))
                    def_ms = default_slots_for_worker(p)
                    wp_snap[p.worker_name] = {
                        "is_enabled": bool(p.is_enabled),
                        "max_slots": clamp_num(p.max_slots, 1, 16, def_ms, as_int=True),
                        "capability": cap_s,
                        "allow_4k": allow_4k_v,
                        "staging_mode": str(getattr(p, "staging_mode", AppState.default_staging_mode) or AppState.default_staging_mode),
                        "power_mode": str(getattr(p, "power_mode", AppState.default_power_mode) or AppState.default_power_mode),
                        "custom_cores": parse_custom_cores_list(getattr(p, "custom_cores", "")),
                        "prioritize_4k": bool(p.prioritize_4k),
                        "use_local_temp": bool(p.use_local_temp),
                    }
            AppState.worker_policies_backup = wp_snap
        except Exception:
            pass
    # Atomický zápis (tmp + fsync + os.replace) pod zámkem s právy 0600: payload se sestavuje
    # AŽ uvnitř config_file_lock, takže pomalejší vlákno nikdy nepřepíše novější nastavení.
    with config_file_lock:
        try:
            payload = {k: getattr(AppState, k) for k in CONFIG_KEYS}
            payload["weighted_slots_3tier_v1"] = True
            payload["analytics_seeded"] = bool(getattr(AppState, "analytics_seeded", False))
            payload["worker_policies"] = dict(getattr(AppState, "worker_policies_backup", {}) or {})
            payload["ui_prefs"] = sanitize_ui_prefs(getattr(AppState, "ui_prefs", {}) or {})
            payload["worker_windows_settings"] = dict(getattr(AppState, "worker_windows_settings", {}) or {})
            payload["pending_worker_windows_settings"] = dict(getattr(AppState, "pending_worker_windows_settings", {}) or {})
            atomic_write_json(PERSISTENT_CONFIG_FILE, payload, mode=0o600, indent=2)
        except Exception as e:
            logger.error(f"Config save error: {e}")


# ==============================================================================
# 3B. ČISTÁ GHCR KONTEJNEROVÁ AKTUALIZACE (WATCHTOWER STYL, BEZ os.execv)
# ==============================================================================
# Fáze 1: Manager zjistí novou verzi přímo z veřejného GHCR (OCI label verze +
#         SHA256 digest), přes /var/run/docker.sock stáhne
#         ghcr.io/maseklab/hb-manager:latest a přetvoří vlastní kontejner.
# Fáze 2: Linux Workery (ghcr.io/maseklab/hb-worker:latest) se samy přetvoří ve
#         stavu Idle; Windows Workery si stáhnou klientské soubory z API Managera.
DOCKER_SOCK_PATH = dsu.DOCKER_SOCK_PATH
GHCR_CHECK_MIN_INTERVAL_S = 300       # lehký manifest dotaz na ghcr.io každých 5 minut
UPSTREAM_CHECK_MIN_INTERVAL_S = 3600  # kontrola externích nástrojů přes GitHub API každých 60 minut

github_update_state = {
    "image": GHCR_MANAGER_REPO + ":latest",
    "channel": "latest",
    "remote_version": "",
    "remote_digest": "",
    "local_digest": "",
    "local_image_version": "",
    "update_available": False,
    "digest_update": False,
    "checked_at": 0,
    "last_error": "",
    "updating": False,
    "phase": "idle",
    # O1/O4: poslední neúspěšný pokus {to, kind, reason, until, count, log_tail}; automatika do „until“ čeká
    "failure": {},
    "docker_sock_available": dsu.docker_sock_available(),
    # Živý strukturovaný postup OTA (pro Web UI): krok 1/4–4/4, procenta, detail a uběhlý čas.
    "progress": {
        "phase": "", "step": 0, "total_steps": 4, "percent": 0.0, "detail": "",
        "elapsed_sec": 0, "started_at": 0, "layers_done": 0, "layers_total": 0,
        "mb_done": 0.0, "mb_total": 0.0, "target_version": "",
    },
}
ghcr_update_lock = threading.Lock()
OTA_TOTAL_STEPS = 4


def _set_ota_progress(step: int, percent: float, info: dict = None):
    """Uloží strukturovaný postup OTA aktualizace (volá se z vlákna self-update)."""
    info = info or {}
    pr = github_update_state.setdefault("progress", {})
    step = max(0, min(OTA_TOTAL_STEPS, int(step)))
    prev_step = int(pr.get("step") or 0)
    prev_pct = float(pr.get("percent") or 0.0)
    pr["step"] = max(step, prev_step) if step else 0
    pr["total_steps"] = OTA_TOTAL_STEPS
    # Procenta nikdy necouvají (monotónní progress bar), jen při resetu (step 0).
    pr["percent"] = round(max(prev_pct, float(percent)) if step else 0.0, 1)
    step = pr["step"]
    pr["phase"] = f"{step}/{OTA_TOTAL_STEPS} {T('ota_prog_step_' + str(step))}" if step else ""
    for k in ("layers_done", "layers_total", "mb_done", "mb_total"):
        if k in info:
            pr[k] = info[k]
    if step == 2 and int(pr.get("layers_total") or 0) > 0:
        pr["detail"] = T("ota_prog_layers_fmt", pr.get("layers_done", 0), pr.get("layers_total", 0),
                         f"{float(pr.get('mb_done') or 0):.0f}", f"{float(pr.get('mb_total') or 0):.0f}")
    elif step:
        pr["detail"] = T("ota_prog_detail_" + str(step))
    started = int(pr.get("started_at") or 0)
    pr["elapsed_sec"] = int(time.time() - started) if started else 0


def get_github_update_state_snapshot() -> dict:
    """Kopie stavu GHCR updateru s živě přepočítaným uběhlým časem OTA."""
    snap = dict(github_update_state)
    pr = dict(snap.get("progress") or {})
    started = int(pr.get("started_at") or 0)
    if snap.get("updating") and started:
        pr["elapsed_sec"] = int(time.time() - started)
    snap["progress"] = pr
    return snap


def _version_tuple(v_str: str):
    return dsu.version_tuple(v_str)


def check_ghcr_manager_update(force: bool = False) -> dict:
    st = github_update_state
    st["docker_sock_available"] = dsu.docker_sock_available()
    now_t = int(time.time())
    if not force and st.get("checked_at") and (now_t - int(st.get("checked_at") or 0)) < GHCR_CHECK_MIN_INTERVAL_S:
        return dict(st)
    ua = f"HB-Cluster-Manager/{MANAGER_VERSION}"
    try:
        mgr_image = get_manager_image()
        st["image"] = mgr_image
        st["channel"] = get_update_channel()
        loc_digests = []
        if st["docker_sock_available"]:
            try:
                loc = dsu.get_own_image_info(user_agent=ua)
                loc_digests = list(loc.get("repo_digests") or [])
                st["local_image_version"] = str(loc.get("version") or "")
            except Exception:
                loc_digests = []
        # HEAD na manifest (jen digest); GET až při skutečně novém buildu.
        remote = dsu.ghcr_fetch_remote_image_info(
            mgr_image, user_agent=ua,
            known_local={"digests": loc_digests, "version": st.get("local_image_version") or MANAGER_VERSION}
        )
        r_ver = str(remote.get("version") or "")
        r_dig = str(remote.get("digest") or "")
        newer = dsu.is_version_newer(r_ver, MANAGER_VERSION)
        same_ver = bool(r_ver) and _version_tuple(r_ver) == _version_tuple(MANAGER_VERSION)
        digest_upd = bool(same_ver and r_dig and loc_digests and r_dig not in loc_digests)
        # Pojistka vydání: novou verzi Manageru nabídnout (i automaticky spustit) až ve chvíli, kdy je na GHCR
        # i obraz Linux workeru stejné verze. Jinak by se Manager aktualizoval a Linux workery by čekaly
        # na obraz, který se třeba zasekl při sestavení (Windows workery berou soubory přímo z Manageru).
        worker_pending = False
        worker_remote_ver = ""
        if newer:
            try:
                w_remote = dsu.ghcr_fetch_remote_image_info(get_worker_image(), user_agent=ua)
                worker_remote_ver = str(w_remote.get("version") or "")
                worker_pending = not worker_remote_ver or _version_tuple(worker_remote_ver) < _version_tuple(r_ver)
            except Exception:
                worker_pending = True
        st.update({
            "remote_version": r_ver,
            "remote_digest": r_dig,
            "local_digest": loc_digests[0] if loc_digests else "",
            "update_available": bool((newer and not worker_pending) or digest_upd),
            "worker_image_pending": bool(worker_pending),
            "worker_remote_version": worker_remote_ver,
            "digest_update": digest_upd,
            "checked_at": now_t,
            "last_error": "",
        })
    except Exception as e:
        st["checked_at"] = now_t
        st["last_error"] = str(e)[:180]
    return dict(st)


# ------------------------------------------------------------------------------
# D2: ZÁLOHY DATABÁZE (SQLite backup API – bezpečná kopie i za běhu): denně, posledních 7, a před každou
# aktualizací Manageru, poslední 3. Složka data/backup; seznam a stažení v Nastavení → O aplikaci.
# Obnova jen ručně podle nápovědy (zastavit Manager, nahradit cluster.db).
# ------------------------------------------------------------------------------
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
BACKUP_KEEP = {"daily": 7, "update": 3}
BACKUP_NAME_RE = re.compile(r"^cluster-(daily|update)-\d{8}-\d{6}\.db$")
DAILY_BACKUP_EVERY_S = 24 * 3600


def list_db_backups() -> list:
    out = []
    try:
        for fn in os.listdir(BACKUP_DIR):
            if BACKUP_NAME_RE.match(fn):
                st = os.stat(os.path.join(BACKUP_DIR, fn))
                out.append({"name": fn, "kind": fn.split("-")[1], "size": int(st.st_size), "created": int(st.st_mtime)})
    except Exception:
        pass
    out.sort(key=lambda b: b["created"], reverse=True)
    return out


def backup_database(kind: str = "daily") -> str:
    """Vytvoří zálohu databáze a smaže nejstarší nad limit. Vrací název souboru ("" = nepovedlo se)."""
    import sqlite3
    if kind not in BACKUP_KEEP:
        kind = "daily"
    try:
        from database import DB_PATH
        os.makedirs(BACKUP_DIR, exist_ok=True)
        try:
            os.chmod(BACKUP_DIR, 0o700)
        except Exception:
            pass
        fn = f"cluster-{kind}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        dst = os.path.join(BACKUP_DIR, fn)
        src_c = sqlite3.connect(DB_PATH, timeout=30)
        try:
            dst_c = sqlite3.connect(dst + ".tmp")
            try:
                src_c.backup(dst_c)
            finally:
                dst_c.close()
        finally:
            src_c.close()
        os.replace(dst + ".tmp", dst)
        try:
            os.chmod(dst, 0o600)
        except Exception:
            pass
        same = [b for b in list_db_backups() if b["kind"] == kind]
        for old_b in same[BACKUP_KEEP[kind]:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old_b["name"]))
            except Exception:
                pass
        append_manager_log(T("mgr_db_backup_done", fn))
        return fn
    except Exception as e:
        logger.error(f"Database backup failed: {e}")
        append_manager_log(T("mgr_db_backup_failed", str(e)[:200]))
        return ""


def maybe_daily_backup():
    daily = [b for b in list_db_backups() if b["kind"] == "daily"]
    if not daily or time.time() - daily[0]["created"] >= DAILY_BACKUP_EVERY_S:
        backup_database("daily")


# ------------------------------------------------------------------------------
# O1 + O4: OVĚŘENÁ AKTUALIZACE MANAGERU A ČEKÁNÍ PO NEÚSPĚCHU
# ------------------------------------------------------------------------------
# Neúspěch (zkušební spuštění, stažení, pomocník): automatika zkusí další kolo kontroly (5 min),
# po druhém neúspěchu stejné verze až za 6 h. Vrácení nové verze pomocníkem (nenaběhla) = hned 6 h.
# Ruční „Aktualizovat“ v Nastavení čekání nerespektuje.
UPDATE_LONG_RETRY_S = dsu.UPDATE_RETRY_AFTER_ROLLBACK_S
_update_attempt_clear_at = {"ts": 0.0}


def _fmt_retry_time(ts: float) -> str:
    return time.strftime("%d.%m. %H:%M", time.localtime(float(ts or 0)))


def manager_update_blocked(remote_version: str = "") -> bool:
    """True, když automatická aktualizace na tuto verzi čeká po neúspěchu (O4)."""
    f = github_update_state.get("failure") or {}
    until = float(f.get("until") or 0)
    if until <= time.time():
        return False
    tgt = str(f.get("to") or "")
    return not tgt or not remote_version or dsu._same_version(tgt, remote_version)


def note_manager_update_failure(kind: str, target: str, reason: str, log_tail=None, rollback: bool = False) -> dict:
    st = github_update_state
    prev = st.get("failure") or {}
    tgt = str(target or st.get("remote_version") or "")
    count = int(prev.get("count") or 0) + 1 if dsu._same_version(prev.get("to"), tgt) else 1
    long_wait = rollback or count >= 2
    until = time.time() + (UPDATE_LONG_RETRY_S if long_wait else GHCR_CHECK_MIN_INTERVAL_S)
    st["failure"] = {"to": tgt, "kind": str(kind or "error"), "reason": str(reason or "")[:300], "count": count,
                     "until": int(until), "long": bool(long_wait), "log_tail": [str(x)[:300] for x in (log_tail or [])][-15:]}
    return st["failure"]


def evaluate_manager_update_attempt():
    """Po startu (O1): nová verze záznam o pokusu smaže až po 5 min běhu; stará verze po vrácení
    zapíše důvod a automatika další 6 h nic nezkouší."""
    cur_img = ""
    if dsu.docker_sock_available():
        try:
            cur_img = str(dsu.get_own_image_info(user_agent=f"HB-Cluster-Manager/{MANAGER_VERSION}").get("id") or "")
        except Exception:
            cur_img = ""
    state, rec, res = dsu.evaluate_update_attempt(DATA_DIR, MANAGER_VERSION, cur_img)
    if state == "updated":
        append_manager_log(T("mgr_update_done", rec.get("from") or "?", MANAGER_VERSION))
        _update_attempt_clear_at["ts"] = time.time() + dsu.UPDATE_ATTEMPT_KEEP_S
    elif state == "failed":
        until = int(rec.get("ts") or 0) + UPDATE_LONG_RETRY_S
        if until <= time.time():
            dsu.clear_update_attempt(DATA_DIR)
            return
        reason = str(res.get("reason") or "") or T("mgr_update_reason_unknown")
        st = github_update_state
        st["failure"] = {"to": str(rec.get("to") or ""), "kind": "rollback", "reason": reason[:300], "count": 2,
                         "until": until, "long": True, "log_tail": [str(x)[:300] for x in (res.get("log_tail") or [])][-15:]}
        append_manager_log(T("mgr_update_rolled_back", rec.get("to") or "?", reason, MANAGER_VERSION, _fmt_retry_time(until)))
        for ln in st["failure"]["log_tail"][-12:]:
            append_manager_log(f"[SELF-UPDATE]   | {ln}")
    elif state == "stale":
        dsu.clear_update_attempt(DATA_DIR)


def maintain_manager_update_attempt():
    """Volá maintenance_loop 1× za minutu."""
    ts = _update_attempt_clear_at["ts"]
    if ts and time.time() >= ts:
        _update_attempt_clear_at["ts"] = 0.0
        dsu.clear_update_attempt(DATA_DIR)
    f = github_update_state.get("failure") or {}
    if f.get("kind") == "rollback" and float(f.get("until") or 0) <= time.time():
        dsu.clear_update_attempt(DATA_DIR)


def perform_cluster_self_update(reason: str = "manual") -> dict:
    """Fáze 1: docker pull hb-manager:latest + recreate vlastního kontejneru (na pozadí)."""
    st = github_update_state
    st["docker_sock_available"] = dsu.docker_sock_available()
    if not st["docker_sock_available"]:
        return {"ok": False, "detail": T("mgr_update_requires_docker_sock")}
    with ghcr_update_lock:
        if st.get("updating"):
            return {"ok": True, "started": False, "already_running": True, "phase": st.get("phase")}
        if reason == "auto" and manager_update_blocked(st.get("remote_version") or ""):
            return {"ok": False, "detail": "blocked"}
        backup_database("update")          # D2: záloha DB před každou aktualizací Manageru
        st["updating"] = True
        st["phase"] = "pulling"
        st["last_error"] = ""
        st["progress"] = {
            "phase": "", "step": 0, "total_steps": OTA_TOTAL_STEPS, "percent": 0.0, "detail": "",
            "elapsed_sec": 0, "started_at": int(time.time()), "layers_done": 0, "layers_total": 0,
            "mb_done": 0.0, "mb_total": 0.0, "target_version": str(st.get("remote_version") or ""),
        }
        _set_ota_progress(1, 1.0)

    def _run():
        ua = f"HB-Cluster-Manager/{MANAGER_VERSION}"
        target = str(st.get("remote_version") or "")
        try:
            mgr_image = get_manager_image()
            append_manager_log(T("mgr_ghcr_pull_started", mgr_image, reason))
            res = dsu.trigger_self_recreate(
                mgr_image, log_fn=append_manager_log, user_agent=ua, stop_timeout=30,
                progress_fn=_set_ota_progress,
                selftest_cmd=["python", os.path.join(APP_BASE_DIR, "main.py"), "--selftest"],
                attempt_dir=DATA_DIR, current_version=MANAGER_VERSION, verify_role="manager",
            )
            if res.get("ok"):
                st["phase"] = "recreating"
                _set_ota_progress(4, 95.0)
                append_manager_log(T("mgr_ghcr_recreating", res.get("version") or "?"))
                # Pomocník Manager do pár sekund zastaví. Když se do 3 min nic nestane, pokus se nepovedl.
                time.sleep(180)
                dsu.clear_update_attempt(DATA_DIR)
                f = note_manager_update_failure("helper", res.get("version") or target, T("mgr_update_helper_no_effect"))
                st["phase"] = "error"
                st["last_error"] = T("mgr_update_helper_no_effect")
                append_manager_log(f"[SELF-UPDATE] {st['last_error']} {T('mgr_update_next_try', _fmt_retry_time(f['until']))}")
            else:
                rsn = str(res.get("reason") or "error")
                st["phase"] = "unchanged" if rsn == "image_unchanged" else "error"
                if rsn == "image_unchanged":
                    st["last_error"] = T("mgr_ghcr_image_unchanged")
                    note_manager_update_failure(rsn, target, st["last_error"])
                    append_manager_log(f"[SELF-UPDATE] {st['last_error']}")
                elif rsn == "selftest_failed":
                    st["last_error"] = T("mgr_update_selftest_failed", res.get("version") or target, res.get("detail") or "?")
                    f = note_manager_update_failure(rsn, res.get("version") or target, res.get("detail") or "", res.get("log_tail"))
                    append_manager_log(f"[SELF-UPDATE] {st['last_error']} {T('mgr_update_next_try', _fmt_retry_time(f['until']))}")
                    for ln in f["log_tail"][-12:]:
                        append_manager_log(f"[SELF-UPDATE]   | {ln}")
                else:
                    st["last_error"] = f"{rsn}: {res.get('detail') or ''}".strip(": ")
                    note_manager_update_failure(rsn, target, st["last_error"])
                    append_manager_log(f"[SELF-UPDATE] {st['last_error']}")
        except Exception as e:
            st["phase"] = "error"
            st["last_error"] = str(e)[:180]
            note_manager_update_failure("error", target, st["last_error"])
            append_manager_log(f"[SELF-UPDATE] Error: {e}")
        st["updating"] = False
        st["progress"] = dict(st.get("progress") or {}, step=0, percent=0.0, phase="", detail="")

    threading.Thread(target=_run, name="hb-ghcr-self-update", daemon=True).start()
    return {"ok": True, "started": True, "phase": "pulling"}


# ==============================================================================
# 4. ATOMICKÝ KOORDINÁTOR MÍSTA NA VÝSTUPNÍM DISKU (STORAGE GUARD)
# ==============================================================================
upload_reservation_lock = threading.Lock()
active_upload_reservations = {}
manual_upload_release_ids = set()
held_sync_lock = threading.Lock()
worker_reported_held_jobs = {}   # worker_name -> (ts, set[int])
orphaned_disk_missing_since = {} # job_id -> first_missing_ts
disk_hold_entered_ts = {}        # job_id -> ts when WAITING_DISK was entered


def get_output_storage_telemetry() -> dict:
    out_dir = get_manager_outputs_dir()
    free_b, total_b = 0, 0
    try:
        if os.path.exists(out_dir):
            total_b, _, free_b = shutil.disk_usage(out_dir)
    except Exception:
        pass

    now_t = time.time()
    with upload_reservation_lock:
        for jid in [jid for jid, r in active_upload_reservations.items() if (now_t - r["ts"]) > 180.0]:
            active_upload_reservations.pop(jid, None)
        reserved_b = sum(r["bytes"] for r in active_upload_reservations.values())
        active_uploads_cnt = len(active_upload_reservations)

    min_res_b = int(AppState.min_free_output_gb * (1024**3))
    eff_free_b = max(0, free_b - reserved_b)
    return {
        "free_gb": round(free_b / (1024**3), 1),
        "total_gb": round(total_b / (1024**3), 1),
        "reserved_gb": round(reserved_b / (1024**3), 2),
        "effective_free_gb": round(eff_free_b / (1024**3), 1),
        "min_free_gb": round(AppState.min_free_output_gb, 1),
        "active_uploads_count": active_uploads_cnt,
        "is_low_space": (free_b > 0 and eff_free_b < min_res_b),
    }


def request_output_upload_slot(job_id: int, worker_name: str, file_size_bytes: int, force_override: bool = False) -> dict:
    out_dir = get_manager_outputs_dir()
    try:
        _, _, free_b = shutil.disk_usage(out_dir)
    except Exception:
        return {"allowed": True, "free_gb": 999.0, "reason": ""}

    now_t = time.time()
    with upload_reservation_lock:
        for jid in [jid for jid, r in active_upload_reservations.items() if (now_t - r["ts"]) > 180.0]:
            active_upload_reservations.pop(jid, None)

        manual_forced = force_override or (job_id in manual_upload_release_ids)
        other_reserved = sum(r["bytes"] for jid, r in active_upload_reservations.items() if jid != job_id)
        avail_b = max(0, free_b - other_reserved)
        min_res_b = int(512 * 1024 * 1024) if manual_forced else int(AppState.min_free_output_gb * (1024**3))
        needed_b = int(file_size_bytes or 0) + min_res_b

        if avail_b >= needed_b:
            active_upload_reservations[job_id] = {
                "worker_name": worker_name,
                "bytes": int(file_size_bytes or 0),
                "ts": now_t,
            }
            manual_upload_release_ids.discard(job_id)
            return {
                "allowed": True,
                "free_gb": round(free_b / (1024**3), 1),
                "effective_free_gb": round((avail_b - file_size_bytes) / (1024**3), 1),
                "reason": "",
            }

        missing_gb = max(0.1, (needed_b - avail_b) / (1024**3))
        reason_str = T("mgr_insufficient_space_outputs_gb", f'{free_b / 1024 ** 3:.1f}', f'{other_reserved / 1024 ** 3:.1f}', f'{file_size_bytes / 1024 ** 3:.1f}', f'{AppState.min_free_output_gb:.0f}', f'{missing_gb:.1f}')
        return {
            "allowed": False,
            "free_gb": round(free_b / (1024**3), 1),
            "missing_gb": round(missing_gb, 1),
            "reason": reason_str,
        }


def release_output_upload_slot(job_id: int):
    with upload_reservation_lock:
        active_upload_reservations.pop(job_id, None)
        manual_upload_release_ids.discard(job_id)
    with held_sync_lock:
        disk_hold_entered_ts.pop(job_id, None)
        orphaned_disk_missing_since.pop(job_id, None)


# ==============================================================================
# 5. STAV WORKERŮ, BINÁREK A TELEMETRIE
# ==============================================================================
active_workers = {}
worker_versions = {}
worker_seen_ip = {}   # adresa, ze které worker naposledy přišel (jak ji vidí Manager); jen pro zobrazení
worker_update_now = {}   # O4: „Aktualizovat teď“ – worker_name -> čas požadavku (doručí se 1× v ping / jobs/next)
WORKER_UPDATE_NOW_TTL_S = 900


def request_worker_update_now(w_name: str):
    if w_name:
        worker_update_now[w_name] = time.time()


def take_worker_update_now(w_name: str, version: str) -> bool:
    """Jednorázový příkaz „Aktualizovat teď“ pro worker; jen když opravdu potřebuje jinou verzi."""
    ts = worker_update_now.get(w_name)
    if not ts:
        return False
    if time.time() - ts > WORKER_UPDATE_NOW_TTL_S or not version or version == WORKER_VERSION:
        worker_update_now.pop(w_name, None)
        return False
    worker_update_now.pop(w_name, None)
    return True


worker_seen_ip_recent = {}   # worker → {adresa: kdy naposledy} – jen kvůli tichému střídání adres v logu
WORKER_IP_NOTE_QUIET_S = 1800


def record_worker_seen_ip(w_name: str, ip: str, now: float | None = None):
    """Uloží adresu workeru; při změně zapíše krátkou poznámku do logu Manageru (nic neblokuje).
    Poznámka jen u adresy, kterou worker posledních 30 min neměl: přes tunel (Cloudflare / WARP) Manager vidí
    adresu toho, kdo požadavek předal, a ta se může střídat každých pár sekund – to není změna workeru."""
    if not w_name or not ip:
        return
    now = time.time() if now is None else now
    old = worker_seen_ip.get(w_name)
    worker_seen_ip[w_name] = ip
    recent = worker_seen_ip_recent.setdefault(w_name, {})
    seen_before = recent.get(ip)
    recent[ip] = now
    if len(recent) > 8:
        for k in sorted(recent, key=recent.get)[:len(recent) - 8]:
            recent.pop(k, None)
    if old and old != ip and (seen_before is None or now - seen_before > WORKER_IP_NOTE_QUIET_S):
        try:
            logger.info(T('mgr_worker_ip_changed', w_name, ip, old))
        except Exception:
            pass
worker_telemetry = {}
worker_telemetry_history = {}
worker_handshakes = {}
active_handshake_tokens = {}
handshake_lock = threading.Lock()
job_heartbeats = {}
job_assigned_time = {}
windows_settings_lock = threading.Lock()


def extract_unc_server_host(*paths) -> str:
    for raw_p in paths:
        p = str(raw_p or "").strip().replace("/", "\\")
        if p.startswith("\\\\"):
            pts = [x for x in p.lstrip("\\").split("\\") if x]
            if pts:
                return pts[0]
    return ""


def get_default_smb_server() -> str:
    return extract_unc_server_host(AppState.win_default_inputs_dir, AppState.win_default_outputs_dir)


SECRET_MASK = "********"   # zástupná hodnota hesla v odpovědích pro webové UI


def mask_secret(value) -> str:
    return SECRET_MASK if str(value or "") else ""


WIN_SLEEP_MODES = ("encoding", "always", "off")


def _clean_caps(raw: dict) -> dict:
    """Schopnosti PC hlášené workerem – jen známé klíče a krátké hodnoty."""
    out = {}
    for k in ("modern_standby", "s3", "has_battery", "on_ac", "can_elevate", "is_elevated"):
        if k in raw:
            out[k] = bool(raw.get(k))
    wt = str(raw.get("wake_timers") or "").strip().lower()
    if wt in ("enabled", "disabled", "important_only", "unknown"):
        out["wake_timers"] = wt
    err = str(raw.get("autostart_error") or "").strip()
    if err:
        out["autostart_error"] = err[:300]
    a_st = str(raw.get("autostart_status") or "").strip().lower()
    if a_st in ("uac_pending", "uac_cancelled", "uac_no_desktop", "no_admin", "failed"):
        out["autostart_status"] = a_st
    a_tk = str(raw.get("autostart_task") or "").strip().lower()
    if a_tk in ("ok", "legacy", "none", "unknown"):
        out["autostart_task"] = a_tk
    pv = raw.get("priv")
    if isinstance(pv, dict):   # W3: kontrola práv Windows workeru při startu
        p_out = {"admin": bool(pv.get("admin"))}
        for k in ("priority", "affinity", "wake_timer", "keep_awake"):
            v = str(pv.get(k) or "").strip().lower()
            p_out[k] = v if v in ("ok", "fail", "unsupported") else "fail"
        t = str(pv.get("task") or "").strip().lower()
        p_out["task"] = t if t in ("ok", "legacy", "none", "highest", "limited", "unknown") else "unknown"
        try:
            p_out["ts"] = int(pv.get("ts") or 0)
        except (TypeError, ValueError):
            p_out["ts"] = 0
        out["priv"] = p_out
    return out


def _norm_sleep_mode(v, fallback="encoding") -> str:
    m = str(v or "").strip().lower()
    return m if m in WIN_SLEEP_MODES else fallback


def get_worker_windows_settings_view(worker_name: str) -> dict:
    """Pohled pro webové UI – heslo SMB je zamaskované."""
    view = _get_worker_windows_settings_effective(worker_name)
    view["smb_pass"] = mask_secret(view.get("smb_pass"))
    return view


def _get_worker_windows_settings_effective(worker_name: str) -> dict:
    with windows_settings_lock:
        cur = dict((AppState.worker_windows_settings or {}).get(worker_name) or {})
        pend = dict((AppState.pending_worker_windows_settings or {}).get(worker_name) or {})
    eff = dict(cur)
    if pend:
        eff.update(pend)
    mode = str(eff.get("autostart_mode") or "after_login").strip().lower()
    if mode not in ("off", "after_login", "before_login"):
        mode = "after_login"
    smb_srv = str(eff.get("smb_server") or get_default_smb_server() or "").strip()
    smb_u = str(eff.get("smb_user") if eff.get("smb_user") is not None else (eff.get("smb_username") or AppState.smb_username or "")).strip()
    smb_p = str(eff.get("smb_pass") if eff.get("smb_pass") is not None else (eff.get("smb_password") or AppState.smb_password or ""))
    return {
        "autostart_mode": mode,
        "smb_server": smb_srv,
        "smb_user": smb_u,
        "smb_pass": smb_p,
        "desktop_shortcut": bool(eff.get("desktop_shortcut", True)),
        "start_menu_shortcut": bool(eff.get("start_menu_shortcut", True)),
        "tray_minimize": bool(eff.get("tray_minimize", eff.get("start_in_tray", True))),
        "sleep_prevention_mode": _norm_sleep_mode(eff.get("sleep_prevention_mode")),
        "skip_tray_prompt": bool(eff.get("skip_tray_prompt", False)),
        "show_log": bool(eff.get("show_log", False)),
        "wake_check": bool(eff.get("wake_check", True)),
        "caps": dict(cur.get("caps") or {}) if isinstance(cur.get("caps"), dict) else {},   # co PC umí (šednutí voleb v UI)
        "reported": bool(cur.get("reported")),   # worker už sám poslal svůj stav (verze s těmito volbami)
        "pending_sync": bool(pend),
        "updated_at": int(eff.get("updated_at") or cur.get("updated_at") or 0),
    }


def queue_worker_windows_settings(worker_name: str, req_data: dict) -> dict:
    now_ts = int(time.time())
    mode = str(req_data.get("autostart_mode") or "after_login").strip().lower()
    if mode not in ("off", "after_login", "before_login"):
        mode = "after_login"
    smb_srv = sanitize_path(req_data.get("smb_server") or "") or get_default_smb_server()
    smb_u = str(req_data.get("smb_user") if req_data.get("smb_user") is not None else (req_data.get("smb_username") or AppState.smb_username or "")).strip()
    smb_p = str(req_data.get("smb_pass") if req_data.get("smb_pass") is not None else (req_data.get("smb_password") or AppState.smb_password or ""))
    cur_eff = _get_worker_windows_settings_effective(worker_name)
    if smb_p == SECRET_MASK:
        smb_p = str(cur_eff.get("smb_pass") or AppState.smb_password or "")

    def _opt_bool(key):
        v = req_data.get(key)
        return bool(cur_eff.get(key)) if v is None else bool(v)

    item = {
        "autostart_mode": mode,
        "smb_server": smb_srv,
        "smb_user": smb_u,
        "smb_pass": smb_p,
        "desktop_shortcut": bool(req_data.get("desktop_shortcut", True)),
        "start_menu_shortcut": bool(req_data.get("start_menu_shortcut", True)),
        "tray_minimize": bool(req_data.get("tray_minimize", True)),
        "sleep_prevention_mode": _norm_sleep_mode(req_data.get("sleep_prevention_mode"), cur_eff.get("sleep_prevention_mode") or "encoding"),
        "skip_tray_prompt": _opt_bool("skip_tray_prompt"),
        "show_log": _opt_bool("show_log"),
        "wake_check": _opt_bool("wake_check"),
        "req_ts": now_ts,
        "updated_at": now_ts,
    }
    force_repair = bool(req_data.get("force_task_repair"))
    with windows_settings_lock:
        if not isinstance(AppState.worker_windows_settings, dict):
            AppState.worker_windows_settings = {}
        if not isinstance(AppState.pending_worker_windows_settings, dict):
            AppState.pending_worker_windows_settings = {}
        prev_cur = AppState.worker_windows_settings.get(worker_name) or {}
        prev_pend = AppState.pending_worker_windows_settings.get(worker_name) or {}
        req_ts = max(
            now_ts,
            int(prev_pend.get("req_ts") or 0) + 1,
            int(prev_cur.get("req_ts") or 0) + 1,
            int(prev_cur.get("last_ack_ts") or 0) + 1,
        )
        item["req_ts"] = req_ts
        item["updated_at"] = req_ts
        prev_caps = dict(prev_cur.get("caps") or {}) if isinstance(prev_cur.get("caps"), dict) else {}
        AppState.worker_windows_settings[worker_name] = dict(
            item,
            reported=bool(prev_cur.get("reported")),
            last_ack_ts=int(prev_cur.get("last_ack_ts") or 0),
            caps=prev_caps,
        )
        pend_item = dict(item)
        if force_repair:
            pend_item["force_task_repair"] = True
        AppState.pending_worker_windows_settings[worker_name] = pend_item
    save_manager_config()
    return get_worker_windows_settings_view(worker_name)


def record_worker_windows_settings_state(worker_name: str, state_dict: dict):
    if not worker_name or not isinstance(state_dict, dict):
        return
    dirty = False
    with windows_settings_lock:
        if not isinstance(AppState.worker_windows_settings, dict):
            AppState.worker_windows_settings = {}
        if not isinstance(AppState.pending_worker_windows_settings, dict):
            AppState.pending_worker_windows_settings = {}
        prev = AppState.worker_windows_settings.get(worker_name) or {}
        mode = str(state_dict.get("autostart_mode") or prev.get("autostart_mode") or "off").strip().lower()
        if mode not in ("off", "after_login", "before_login"):
            mode = "off"
        norm = {
            "autostart_mode": mode,
            "smb_server": str(state_dict.get("smb_server") or prev.get("smb_server") or get_default_smb_server() or "").strip(),
            "smb_user": str(state_dict.get("smb_user") if state_dict.get("smb_user") is not None else (state_dict.get("smb_username") or prev.get("smb_user") or AppState.smb_username or "")).strip(),
            "smb_pass": str(state_dict.get("smb_pass") if state_dict.get("smb_pass") is not None else (state_dict.get("smb_password") or prev.get("smb_pass") or AppState.smb_password or "")),
            "desktop_shortcut": bool(state_dict.get("desktop_shortcut", prev.get("desktop_shortcut", True))),
            "start_menu_shortcut": bool(state_dict.get("start_menu_shortcut", prev.get("start_menu_shortcut", True))),
            "tray_minimize": bool(state_dict.get("tray_minimize", state_dict.get("start_in_tray", prev.get("tray_minimize", False)))),
            "sleep_prevention_mode": _norm_sleep_mode(state_dict.get("sleep_prevention_mode"), _norm_sleep_mode(prev.get("sleep_prevention_mode"))),
            "skip_tray_prompt": bool(state_dict.get("skip_tray_prompt", prev.get("skip_tray_prompt", False))),
            "show_log": bool(state_dict.get("show_log", prev.get("show_log", False))),
            "wake_check": bool(state_dict.get("wake_check", prev.get("wake_check", True))),
            "caps": _clean_caps(state_dict.get("caps")) if isinstance(state_dict.get("caps"), dict) else dict(prev.get("caps") or {}),
            "reported": bool(prev.get("reported")) or ("sleep_prevention_mode" in state_dict),
            "updated_at": int(time.time()),
        }
        ack_ts = int(state_dict.get("ack_ts") or 0)
        norm["last_ack_ts"] = max(ack_ts, int(prev.get("last_ack_ts") or 0))
        norm["req_ts"] = max(int(prev.get("req_ts") or 0), norm["last_ack_ts"])
        pend = AppState.pending_worker_windows_settings.get(worker_name)
        if pend:
            req_ts = int(pend.get("req_ts") or 0)
            if ack_ts >= req_ts or (
                not pend.get("force_task_repair")
                and not norm.get("reported")
                and norm["autostart_mode"] == pend.get("autostart_mode")
                and norm["desktop_shortcut"] == bool(pend.get("desktop_shortcut", True))
                and norm["start_menu_shortcut"] == bool(pend.get("start_menu_shortcut", True))
                and norm["tray_minimize"] == bool(pend.get("tray_minimize", True))
            ):
                AppState.pending_worker_windows_settings.pop(worker_name, None)
                dirty = True
        if any(prev.get(k) != norm[k] for k in ("autostart_mode", "desktop_shortcut", "start_menu_shortcut", "tray_minimize",
                                                 "sleep_prevention_mode", "skip_tray_prompt", "show_log", "wake_check", "reported")):
            dirty = True
        AppState.worker_windows_settings[worker_name] = norm
    if dirty:
        save_manager_config()


def get_pending_worker_windows_settings(worker_name: str):
    if not worker_name:
        return None
    with windows_settings_lock:
        pend = (AppState.pending_worker_windows_settings or {}).get(worker_name)
        return dict(pend) if isinstance(pend, dict) else None

analytics_cache = {"dirty": True, "data": None}
analytics_lock = threading.Lock()


def invalidate_analytics_cache():
    with analytics_lock:
        analytics_cache["dirty"] = True


BINARIES_ROOT_DIR = os.path.join(DATA_DIR, "binaries")
LEGACY_APP_BINARIES_DIR = os.path.join(APP_BASE_DIR, "binaries")
BINARIES_LINUX_DIR = os.path.join(BINARIES_ROOT_DIR, "linux")
BINARIES_WIN_DIR = os.path.join(BINARIES_ROOT_DIR, "windows")
CERTS_DIR = os.path.join(BINARIES_ROOT_DIR, "certs")
ROOT_CERTS_DIR = os.path.join(DATA_DIR, "certs")
CODESIGN_CER_FILE = os.path.join(CERTS_DIR, "maseklab_codesign.cer")   # veřejný certifikát (DER) -> do .zip workera
# Soukromý klíč a heslo leží jen v /data/certs (mimo /data/binaries, které má práva 777) a Manager je nikdy neposílá ven.
CODESIGN_PFX_FILE = os.path.join(ROOT_CERTS_DIR, "maseklab_codesign.pfx")
CODESIGN_PASS_FILE = os.path.join(ROOT_CERTS_DIR, "maseklab_codesign.pass")
LEGACY_CODESIGN_PFX_FILE = os.path.join(CERTS_DIR, "maseklab_codesign.pfx")
ROOT_CODESIGN_CER_FILE = os.path.join(ROOT_CERTS_DIR, "maseklab_codesign.cer")
PREBUILT_WIN_LAUNCHER = os.path.join(APP_BASE_DIR, "prebuilt", "HBWorker.exe")
VERSIONS_JSON_FILE = os.path.join(BINARIES_ROOT_DIR, "versions.json")

CANONICAL_BINARY_FILENAMES = {
    ("linux", "hbcli"): "HandBrakeCLI",
    ("linux", "ffmpeg"): "ffmpeg",
    ("linux", "ffprobe"): "ffprobe",
    ("linux", "mkvpropedit"): "mkvpropedit",
    ("linux", "mkvmerge"): "mkvmerge",
    ("linux", "7z"): "7zz",
    ("windows", "hbcli"): "HandBrakeCLI.exe",
    ("windows", "ffmpeg"): "ffmpeg.exe",
    ("windows", "ffprobe"): "ffprobe.exe",
    ("windows", "mkvpropedit"): "mkvpropedit.exe",
    ("windows", "mkvmerge"): "mkvmerge.exe",
    ("windows", "7z"): "7z.exe",
    ("windows", "7z_dll"): "7z.dll",
    ("windows", "launcher"): "HBWorker.exe",
}

binary_hash_cache = {}
binary_versions_store = {}


def migrate_legacy_binaries_dir():
    """
    v2.11.2: Centrální binárky žijí trvale v /data/binaries (přežijí recreate kontejneru,
    takže se SYSTEM_BUILD nespouští znovu). Stávající soubory z /app/binaries se přesunou
    a /app/binaries se nahradí symlinkem -> /data/binaries (zpětná kompatibilita).
    Pokud je /app/binaries starý bind-mount, soubory se zkopírují a mount zůstane netknutý.
    """
    legacy = LEGACY_APP_BINARIES_DIR
    try:
        os.makedirs(BINARIES_ROOT_DIR, exist_ok=True)
        if os.path.lexists(legacy) and os.path.realpath(legacy) == os.path.realpath(BINARIES_ROOT_DIR):
            return
        if os.path.islink(legacy):
            os.remove(legacy)
        elif os.path.isdir(legacy):
            moved = 0
            for root_d, _, files in os.walk(legacy):
                rel_d = os.path.relpath(root_d, legacy)
                dst_d = BINARIES_ROOT_DIR if rel_d == "." else os.path.join(BINARIES_ROOT_DIR, rel_d)
                os.makedirs(dst_d, exist_ok=True)
                for fn in files:
                    src_f = os.path.join(root_d, fn)
                    dst_f = os.path.join(dst_d, fn)
                    try:
                        if not os.path.exists(dst_f) or os.path.getmtime(src_f) > os.path.getmtime(dst_f):
                            shutil.copy2(src_f, dst_f)
                            moved += 1
                    except Exception:
                        pass
            if moved:
                logger.info(f"Migrated {moved} binaries from {legacy} to {BINARIES_ROOT_DIR}")
            if os.path.ismount(legacy):
                return
            shutil.rmtree(legacy, ignore_errors=True)
        if os.name != "nt" and not os.path.lexists(legacy):
            os.symlink(BINARIES_ROOT_DIR, legacy)
    except Exception as e:
        logger.warning(f"Binaries dir migration warning: {e}")


def ensure_binary_dirs():
    for folder in (BINARIES_ROOT_DIR, BINARIES_LINUX_DIR, BINARIES_WIN_DIR, CERTS_DIR):
        try:
            os.makedirs(folder, exist_ok=True)
            os.chmod(folder, 0o755)
        except Exception:
            pass
    try:
        os.makedirs(ROOT_CERTS_DIR, exist_ok=True)
        os.chmod(ROOT_CERTS_DIR, 0o700)   # soukromý klíč – jen pro proces Manageru
    except Exception:
        pass


def _archive_legacy_codesign_files():
    """Staré certifikáty (CA:TRUE, pevné heslo) se nemažou: přesunou se do /data/certs jako *.legacy-<čas> s právy 0600."""
    ts = int(time.time())
    os.makedirs(ROOT_CERTS_DIR, exist_ok=True)
    for p in (LEGACY_CODESIGN_PFX_FILE, CODESIGN_CER_FILE, CODESIGN_PFX_FILE, ROOT_CODESIGN_CER_FILE):
        try:
            if os.path.isfile(p):
                src_tag = "binaries" if os.path.dirname(os.path.abspath(p)) != os.path.abspath(ROOT_CERTS_DIR) else "data"
                dst = os.path.join(ROOT_CERTS_DIR, f"{os.path.basename(p)}.{src_tag}.legacy-{ts}")
                shutil.move(p, dst)
                os.chmod(dst, 0o600)
        except Exception:
            pass


codesign_cert_lock = threading.RLock()
binary_meta_lock = threading.RLock()


def ensure_codesign_cert_on_manager() -> bool:
    """
    Zajistí lokální podpisový certifikát MasekLab přímo na Manageru:
      - koncový certifikát CA:FALSE, keyUsage=digitalSignature, extendedKeyUsage=codeSigning (nemůže vydávat další certifikáty),
      - náhodné heslo k .pfx v /data/certs/maseklab_codesign.pass (0600),
      - veřejný .cer se kopíruje do /data/binaries/certs (do .zip workeru), .pfx nikdy neopouští /data/certs.
    Staré certifikáty bez souboru s heslem (CA:TRUE / heslo 'maseklab' či 'hbcluster') se archivují a vygeneruje se nový.
    """
    with codesign_cert_lock:
        ensure_binary_dirs()
        try:
            if not (os.path.isfile(CODESIGN_PFX_FILE) and os.path.isfile(CODESIGN_PASS_FILE) and os.path.isfile(ROOT_CODESIGN_CER_FILE)):
                openssl_bin = shutil.which("openssl")
                if not openssl_bin:
                    return False
                _archive_legacy_codesign_files()
                import secrets
                tmp_dir = tempfile.mkdtemp(prefix="hb_cert_", dir=ROOT_CERTS_DIR)
                try:
                    key_p = os.path.join(tmp_dir, "cs.key")
                    pem_p = os.path.join(tmp_dir, "cs.pem")
                    cnf_p = os.path.join(tmp_dir, "openssl.cnf")
                    pass_tmp = os.path.join(tmp_dir, "cs.pass")
                    pfx_tmp = os.path.join(tmp_dir, "cs.pfx")
                    cer_tmp = os.path.join(tmp_dir, "cs.cer")
                    fd = os.open(pass_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as pf:
                        pf.write(secrets.token_urlsafe(32))
                        pf.flush()
                        try:
                            os.fsync(pf.fileno())
                        except Exception:
                            pass
                    with open(cnf_p, "w", encoding="utf-8") as cf:
                        cf.write(
                            "[req]\n"
                            "distinguished_name = dn\n"
                            "x509_extensions = v3_cs\n"
                            "prompt = no\n"
                            "[dn]\n"
                            "C = CZ\n"
                            "O = MasekLab\n"
                            "CN = MasekLab HB Cluster Code Signing\n"
                            "[v3_cs]\n"
                            "basicConstraints = critical,CA:FALSE\n"
                            "keyUsage = critical,digitalSignature\n"
                            "extendedKeyUsage = codeSigning\n"
                            "subjectKeyIdentifier = hash\n"
                        )
                    subprocess.run(
                        [openssl_bin, "req", "-x509", "-newkey", "rsa:3072", "-nodes",
                         "-keyout", key_p, "-out", pem_p, "-days", "3650", "-sha256", "-config", cnf_p],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
                    )
                    subprocess.run(
                        [openssl_bin, "x509", "-in", pem_p, "-outform", "DER", "-out", cer_tmp],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
                    )
                    subprocess.run(
                        [openssl_bin, "pkcs12", "-export", "-out", pfx_tmp,
                         "-inkey", key_p, "-in", pem_p, "-name", "MasekLab HB Cluster Code Signing",
                         "-passout", f"file:{pass_tmp}"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
                    )
                    os.chmod(pfx_tmp, 0o600)
                    os.replace(pfx_tmp, CODESIGN_PFX_FILE)
                    os.replace(pass_tmp, CODESIGN_PASS_FILE)
                    os.replace(cer_tmp, ROOT_CODESIGN_CER_FILE)
                    logger.info("Generated local MasekLab code-signing certificate (CA:FALSE, random password) on Manager.")
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            # Veřejný certifikát (.cer) do /data/binaries/certs pro .zip balíček workeru
            try:
                with open(ROOT_CODESIGN_CER_FILE, "rb") as f_src:
                    cer_b = f_src.read()
                cur_b = b""
                if os.path.isfile(CODESIGN_CER_FILE):
                    with open(CODESIGN_CER_FILE, "rb") as f_cur:
                        cur_b = f_cur.read()
                if cur_b != cer_b:
                    tmp_c = f"{CODESIGN_CER_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
                    with open(tmp_c, "wb") as f_dst:
                        f_dst.write(cer_b)
                        f_dst.flush()
                        try:
                            os.fsync(f_dst.fileno())
                        except Exception:
                            pass
                    os.replace(tmp_c, CODESIGN_CER_FILE)
            except Exception:
                pass
            return os.path.isfile(CODESIGN_PFX_FILE) and os.path.isfile(CODESIGN_PASS_FILE) and os.path.isfile(CODESIGN_CER_FILE)
        except Exception as e:
            logger.warning(f"ensure_codesign_cert_on_manager warning: {e}")
            return False


def sign_windows_exe_on_manager(src_exe: str, dst_exe: str) -> bool:
    """
    Podepíše .exe lokálním certifikátem Manageru (osslsigncode). Případný cizí podpis (např. ze staršího
    Linux workeru) se nejdřív odstraní. Vrací True jen při úspěšném podpisu; dst_exe se zapisuje atomicky.
    """
    ossl_bin = shutil.which("osslsigncode")
    if not ossl_bin or not ensure_codesign_cert_on_manager():
        return False
    work_dir = tempfile.mkdtemp(prefix="hb_sign_")
    try:
        unsigned_p = os.path.join(work_dir, "unsigned.exe")
        signed_p = os.path.join(work_dir, "signed.exe")
        r0 = subprocess.run([ossl_bin, "remove-signature", "-in", src_exe, "-out", unsigned_p],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if r0.returncode != 0 or not os.path.isfile(unsigned_p):
            shutil.copy2(src_exe, unsigned_p)   # soubor nebyl podepsaný
        res = subprocess.run(
            [ossl_bin, "sign", "-pkcs12", CODESIGN_PFX_FILE, "-readpass", CODESIGN_PASS_FILE,
             "-h", "sha256", "-n", "HB Cluster Windows Worker",
             "-i", "https://github.com/maseklab/hb-cluster",
             "-in", unsigned_p, "-out", signed_p],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
        )
        if res.returncode != 0 or not os.path.isfile(signed_p) or os.path.getsize(signed_p) <= 10000:
            return False
        tmp_dst = dst_exe + ".signtmp"
        shutil.copy2(signed_p, tmp_dst)
        os.replace(tmp_dst, dst_exe)
        return True
    except Exception as e:
        logger.warning(f"sign_windows_exe_on_manager error: {e}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def ensure_signed_windows_launcher_on_manager(force: bool = False) -> bool:
    """
    Pokud v /data/binaries/windows/HBWorker.exe chybí platný podepsaný spouštěč
    aktuální verze, vezme předkompilovaný /app/prebuilt/HBWorker.exe, okamžitě jej
    podepíše lokálním certifikátem přes osslsigncode (< 0.1 s) a uloží do /data/binaries/windows/HBWorker.exe.
    """
    with codesign_cert_lock:
        ensure_codesign_cert_on_manager()
        dst_exe = get_canonical_binary_path("windows", "launcher")
        if not dst_exe:
            return False
        with binary_meta_lock:
            rec = dict(binary_versions_store.get("windows_launcher") or {})
        cur_ver = str(rec.get("version") or "").lstrip("v")
        if not force and os.path.isfile(dst_exe) and os.path.getsize(dst_exe) > 10000 and cur_ver == WORKER_VERSION:
            return True

        src_unsigned = PREBUILT_WIN_LAUNCHER if (os.path.isfile(PREBUILT_WIN_LAUNCHER) and os.path.getsize(PREBUILT_WIN_LAUNCHER) > 10000) else None
        tmp_compiled = None
        if not src_unsigned and shutil.which("x86_64-w64-mingw32-gcc"):
            try:
                from workers import launcher_builder as lb
                tmp_compiled = f"{dst_exe}.{os.getpid()}.unsigned.tmp"
                ico_p = os.path.join(STATIC_DIR, "hbw.ico")
                if lb.build_windows_launcher_exe(tmp_compiled, ico_p, version_str=WORKER_VERSION):
                    src_unsigned = tmp_compiled
            except Exception as e:
                logger.warning(f"Fallback mingw compile on Manager failed: {e}")

        if not src_unsigned:
            return os.path.isfile(dst_exe) and os.path.getsize(dst_exe) > 10000

        tmp_signed = f"{dst_exe}.{os.getpid()}.signed.tmp"
        try:
            if os.path.exists(tmp_signed):
                os.remove(tmp_signed)
            signed_ok = sign_windows_exe_on_manager(src_unsigned, tmp_signed)
            if not signed_ok:
                shutil.copy2(src_unsigned, tmp_signed)

            os.makedirs(os.path.dirname(dst_exe), exist_ok=True)
            os.replace(tmp_signed, dst_exe)
            try:
                os.chmod(dst_exe, 0o755)
            except Exception:
                pass
            binary_hash_cache.pop("key_" + dst_exe, None)
            binary_hash_cache.pop("md5_" + dst_exe, None)
            md5, _ = get_file_md5_cached(dst_exe)
            with binary_meta_lock:
                binary_versions_store["windows_launcher"] = {
                    "version": WORKER_VERSION,
                    "md5": md5,
                    "is_custom": False,
                    "updated_at": int(time.time()),
                }
            save_binary_versions_store()
            logger.info(f"Prepared signed Windows launcher HBWorker.exe v{WORKER_VERSION} on Manager (signed={signed_ok}).")
            return True
        except Exception as e:
            logger.warning(f"ensure_signed_windows_launcher_on_manager error: {e}")
            return False
        finally:
            for tp in (tmp_signed, tmp_compiled):
                if tp and os.path.exists(tp):
                    try:
                        os.remove(tp)
                    except Exception:
                        pass


def load_binary_versions_store():
    with binary_meta_lock:
        if os.path.isfile(VERSIONS_JSON_FILE):
            try:
                with open(VERSIONS_JSON_FILE, "r", encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        binary_versions_store.update(loaded)
            except Exception:
                pass


def save_binary_versions_store():
    with binary_meta_lock:
        try:
            os.makedirs(BINARIES_ROOT_DIR, exist_ok=True)
            atomic_write_json(VERSIONS_JSON_FILE, dict(binary_versions_store), mode=0o644, indent=2)
        except Exception:
            pass


def get_canonical_binary_path(os_type: str, tool: str) -> str:
    fn = CANONICAL_BINARY_FILENAMES.get((os_type.lower(), tool.lower()))
    return os.path.join(BINARIES_ROOT_DIR, os_type.lower(), fn) if fn else ""


def resolve_manager_binary_path(os_type: str, tool: str):
    p = get_canonical_binary_path(os_type, tool)
    return p if (p and os.path.isfile(p) and os.path.getsize(p) > 10000) else None


def get_file_md5_cached(filepath):
    if not filepath or not os.path.isfile(filepath):
        return None, 0
    try:
        st = os.stat(filepath)
        ck = (filepath, st.st_mtime, st.st_size)
        if binary_hash_cache.get("key_" + filepath) == ck:
            return binary_hash_cache.get("md5_" + filepath), st.st_size
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                blk = f.read(1024 * 1024)
                if not blk:
                    break
                h.update(blk)
        dg = h.hexdigest()
        binary_hash_cache["key_" + filepath] = ck
        binary_hash_cache["md5_" + filepath] = dg
        return dg, st.st_size
    except Exception:
        return None, 0


def is_ffmpeg_nightly_version(ver_str: str) -> bool:
    s = str(ver_str or "").strip().lstrip("v")
    if not s:
        return False
    low = s.lower()
    if s.startswith("N-") or low.startswith(("master", "git-", "autobuild")):
        return True
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s))


def normalize_ffmpeg_stable_branch(raw_str: str) -> str:
    """Normalizes FFmpeg release strings (e.g. 'n8.0.1-12-gabc' -> '8.0') while preserving nightly markers."""
    s = str(raw_str or "").strip().lstrip("v")
    if not s:
        return ""
    if is_ffmpeg_nightly_version(s):
        return s[:14]
    m_st = re.match(r"^n?(\d+\.\d+)(?:\.\d+)?(?:[-+].*)?$", s, re.IGNORECASE)
    if m_st:
        return m_st.group(1)
    return s[:14]


# Skutečné názvy assetů BtbN/FFmpeg-Builds (release tag "latest"):
#   ffmpeg-n8.0-latest-linux64-gpl-8.0.tar.xz / ffmpeg-n8.0-latest-win64-gpl-8.0.zip
#   (alternativně i bez přípony verze: ffmpeg-n7.1-latest-linux64-gpl.tar.xz)
BTBN_ASSET_RE = re.compile(
    r"^ffmpeg-n(?P<br>\d+\.\d+)(?:\.\d+)?-(?P<tag>latest|\d+-g[0-9a-f]+)-(?P<plat>linux64|win64)-gpl"
    r"(?:-(?P<sfx>\d+\.\d+(?:\.\d+)?))?\.(?P<ext>tar\.xz|zip)$",
    re.IGNORECASE,
)
# Repozitář BtbN/FFmpeg-Builds byl přesunut na FFmpeg-Builds/FFmpeg-Builds (staré URL GitHub přesměrovává).
BTBN_DOWNLOAD_BASE = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/"
# BtbN drží jen 2 nejnovější stabilní větve -> sondy sestupně od nejvyšší možné.
BTBN_FALLBACK_BRANCHES = tuple(f"{mj}.{mn}" for mj in (10, 9, 8, 7) for mn in (2, 1, 0))


def pick_best_btbn_stable_ffmpeg_assets(releases_list: list) -> dict:
    """Najde nejvyšší stabilní větev nX.Y (linux64 tar.xz + win64 zip, GPL; ignoruje master/shared/lgpl)."""
    branches = {}
    for rel in (releases_list or []):
        if not isinstance(rel, dict):
            continue
        for asset in (rel.get("assets") or []):
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            url = str(asset.get("browser_download_url") or "").strip()
            if not name or not url:
                continue
            m = BTBN_ASSET_RE.match(name)
            if not m:
                continue
            plat = m.group("plat").lower()
            ext = m.group("ext").lower()
            if (plat == "linux64" and ext != "tar.xz") or (plat == "win64" and ext != "zip"):
                continue
            key = "lin_url" if plat == "linux64" else "win_url"
            slot = branches.setdefault(m.group("br"), {})
            if key not in slot or m.group("tag").lower() == "latest":
                slot[key] = url
    best_ver = None
    best_tuple = ()
    for br, slot in branches.items():
        if not (slot.get("lin_url") and slot.get("win_url")):
            continue
        try:
            ver_t = tuple(int(p) for p in br.split("."))
        except Exception:
            continue
        if ver_t > best_tuple:
            best_tuple = ver_t
            best_ver = br
    if not best_ver:
        return {}
    return {
        "version": best_ver,
        "linux_url": branches[best_ver]["lin_url"],
        "win_url": branches[best_ver]["win_url"],
    }


def probe_btbn_stable_branch_direct(hdrs: dict) -> str:
    """Fallback bez GitHub API (rate-limit 403/429): HEAD na deterministické /releases/download/latest/ URL."""
    for br in BTBN_FALLBACK_BRANCHES:
        url = f"{BTBN_DOWNLOAD_BASE}ffmpeg-n{br}-latest-linux64-gpl-{br}.tar.xz"
        try:
            r = requests.head(url, headers=hdrs, allow_redirects=True, timeout=6)
            if r.status_code == 200:
                return br
        except Exception:
            return ""  # výpadek sítě: nezdržovat dalšími sondami
    return ""


def probe_binary_version_string(os_type: str, tool: str, filepath: str, md5_hash: str):
    s_key = f"{os_type.lower()}_{tool.lower()}"
    rec = binary_versions_store.get(s_key)
    is_cust = False
    if isinstance(rec, dict):
        is_cust = bool(rec.get("is_custom", False))
        if rec.get("md5") == md5_hash and rec.get("version"):
            v_str = str(rec["version"]).lstrip("v")
            if tool in ("ffmpeg", "ffprobe"):
                v_str = normalize_ffmpeg_stable_branch(v_str)
            return v_str, is_cust

    if os_type.lower() == "linux" and filepath and os.path.isfile(filepath):
        try:
            os.chmod(filepath, 0o777)
            args = ["--version"] if tool in ("hbcli", "mkvpropedit", "mkvmerge") else (["-version"] if tool in ("ffmpeg", "ffprobe") else ["i"])
            res = subprocess.run([filepath] + args, capture_output=True, text=True, timeout=6)
            out = (res.stdout or "") + "\n" + (res.stderr or "")
            ver_s = ""
            if tool == "hbcli":
                m = re.search(r"HandBrake\s+([\d\.]+)", out)
                if m:
                    ver_s = m.group(1)
            elif tool in ("mkvpropedit", "mkvmerge"):
                m = re.search(r"v(\d+\.\d+(?:\.\d+)?)", out)
                if m:
                    ver_s = m.group(1)
            elif tool in ("ffmpeg", "ffprobe"):
                m = re.search(r"version\s+([^\s]+)", out)
                if m:
                    ver_s = normalize_ffmpeg_stable_branch(m.group(1))
            elif tool == "7z":
                m = re.search(r"7-Zip\s+(?:[\(\[][^\)\]]+[\)\]]\s+)?(\d+\.\d+)", out, re.IGNORECASE)
                if m:
                    ver_s = m.group(1)
            if ver_s:
                binary_versions_store[s_key] = {"version": ver_s, "md5": md5_hash, "is_custom": is_cust}
                save_binary_versions_store()
                return ver_s, is_cust
        except Exception:
            pass

    for _, telem in worker_telemetry.items():
        diag = (telem or {}).get("diag") or {}
        w_os = "linux" if "linux" in str(diag.get("os") or "").lower() else "windows"
        if w_os == os_type.lower():
            if tool == "hbcli" and diag.get("hb_md5") == md5_hash and diag.get("hb_version") not in (None, "", "CHYBÍ", "MISSING", "OK"):
                return str(diag.get("hb_version")).lstrip("v"), is_cust
            if tool in ("ffmpeg", "ffprobe") and diag.get("ffmpeg_md5") == md5_hash and diag.get("ffmpeg_version"):
                return normalize_ffmpeg_stable_branch(str(diag.get("ffmpeg_version"))), is_cust
            if tool in ("mkvmerge", "mkvpropedit") and diag.get("mkvmerge_md5") == md5_hash and diag.get("mkv_version"):
                return str(diag.get("mkv_version")).lstrip("v"), is_cust
            if tool == "7z" and diag.get("sevenzip_md5") == md5_hash and diag.get("sevenzip_version"):
                return str(diag.get("sevenzip_version")).lstrip("v"), is_cust

    if isinstance(rec, dict) and rec.get("version"):
        v_str = str(rec["version"]).lstrip("v")
        if tool in ("ffmpeg", "ffprobe"):
            v_str = normalize_ffmpeg_stable_branch(v_str)
        return v_str, is_cust
    return "", is_cust


def has_any_linux_worker() -> bool:
    """Vrátí True, pokud je v klastru zaregistrován nebo online alespoň jeden Linux worker."""
    for _, telem in list(worker_telemetry.items()):
        os_s = str(((telem or {}).get("diag") or {}).get("os") or "").lower()
        if "linux" in os_s:
            return True
    return False


def has_any_windows_worker() -> bool:
    """Vrátí True, pokud je v klastru zaregistrován nebo online alespoň jeden Windows worker."""
    for _, telem in list(worker_telemetry.items()):
        os_s = str(((telem or {}).get("diag") or {}).get("os") or "").lower()
        if "win" in os_s:
            return True
    return False


def has_any_connected_or_registered_worker() -> bool:
    """Vrátí True, pokud se k Manageru už připojil (nebo je v telemetrii/politikách) alespoň jeden worker."""
    if active_workers or worker_telemetry:
        return True
    if getattr(AppState, "worker_policies_backup", None):
        return True
    return False


def create_initial_build_checklist(targets: list, windows_only: bool = False):
    t_set = set(targets or ["all"])
    do_all = "all" in t_set
    items = []
    if not windows_only:
        items.append({"id": "env", "label": T("mgr_preparing_build_environment_gcc"), "status": "pending", "detail": ""})
    if do_all or "7z" in t_set:
        if not windows_only:
            items.append({"id": "lin_7z", "label": "Linux 7-Zip (7zz)", "status": "pending", "detail": ""})
        items.append({"id": "win_7z", "label": "Windows 7-Zip (7z.exe + 7z.dll)", "status": "pending", "detail": ""})
    if do_all or "mkvtoolnix" in t_set:
        if not windows_only:
            items.append({"id": "lin_mkv", "label": "Linux MKVToolNix (mkvmerge + mkvpropedit via staticx)", "status": "pending", "detail": ""})
        items.append({"id": "win_mkv", "label": "Windows MKVToolNix (mkvmerge.exe + mkvpropedit.exe)", "status": "pending", "detail": ""})
    if do_all or "ffmpeg" in t_set:
        if not windows_only:
            items.append({"id": "lin_ff", "label": "Linux FFmpeg & FFprobe (static GPL build)", "status": "pending", "detail": ""})
        items.append({"id": "win_ff", "label": "Windows FFmpeg.exe & FFprobe.exe", "status": "pending", "detail": ""})
    if do_all or "hbcli" in t_set:
        items.append({"id": "win_hb", "label": "Windows HandBrakeCLI.exe (official release)", "status": "pending", "detail": ""})
        if not windows_only:
            items.append({"id": "lin_hb", "label": T("mgr_linux_handbrakecli_source_compil"), "status": "pending", "detail": ""})
    if do_all or "launcher" in t_set:
        items.append({"id": "win_launcher", "label": "Windows Native Launcher (HBWorker.exe)", "status": "pending", "detail": ""})
    return items


binary_build_state = {
    "status": "IDLE",
    "job_id": None,
    "assigned_worker": "",
    "assigned_key_sha": "",      # otisk klíče workeru, který SYSTEM_BUILD převzal (nahrávat nástroje smí jen on)
    "preferred_worker": "AUTO",
    "fallback_used": False,
    "progress": 0.0,
    "step_message": "",
    "updating_tools": [],
    "checklist": [],
    "console_lines": deque(maxlen=80),
    "started_at": 0,
    "finished_at": 0,
    "upstream_versions": {
        "hb_latest": "",
        "mkvtoolnix_latest": "",
        "ffmpeg_latest": "",
        "sevenzip_latest": "",
        "checked_at": 0
    }
}
build_lock = threading.Lock()


def append_build_console_line(msg: str):
    if not msg:
        return
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with build_lock:
        if not binary_build_state["console_lines"] or binary_build_state["console_lines"][-1] != line:
            binary_build_state["console_lines"].append(line)


def update_checklist_from_step_message(step_msg: str):
    if not step_msg:
        return
    low = step_msg.lower()
    with build_lock:
        cl = binary_build_state.get("checklist") or []
        active_id = None
        if "1/5" in low or "překladače" in low or "compilers" in low:
            active_id = "env"
        elif "2/5" in low or "7-zip" in low:
            active_id = "lin_7z"
        elif "3/5" in low and "windows" in low:
            active_id = "win_mkv"
        elif "3/5" in low:
            active_id = "lin_mkv"
        elif "4/5" in low and "windows" in low:
            active_id = "win_ff"
        elif "4/5" in low:
            active_id = "lin_ff"
        elif "5/5" in low and "windows" in low:
            active_id = "win_hb"
        elif "5/5" in low:
            active_id = "lin_hb"

        if active_id:
            for it in cl:
                if it["id"] == "env" and active_id != "env" and it["status"] == "running":
                    it["status"] = "done"
                if it["id"] == active_id and it["status"] == "pending":
                    it["status"] = "running"


def mark_checklist_binary_uploaded(os_k: str, tool_k: str, ver_s: str, size_b: int):
    mb_s = f"{size_b / (1024 * 1024):.1f} MB"
    det = f"v{ver_s} ({mb_s})" if ver_s else mb_s
    map_id = {
        ("linux", "7z"): "lin_7z",
        ("windows", "7z"): "win_7z",
        ("windows", "7z_dll"): "win_7z",
        ("linux", "mkvmerge"): "lin_mkv",
        ("linux", "mkvpropedit"): "lin_mkv",
        ("windows", "mkvmerge"): "win_mkv",
        ("windows", "mkvpropedit"): "win_mkv",
        ("linux", "ffmpeg"): "lin_ff",
        ("linux", "ffprobe"): "lin_ff",
        ("windows", "ffmpeg"): "win_ff",
        ("windows", "ffprobe"): "win_ff",
        ("windows", "hbcli"): "win_hb",
        ("linux", "hbcli"): "lin_hb",
        ("windows", "launcher"): "win_launcher",
    }.get((os_k, tool_k))

    with build_lock:
        for it in (binary_build_state.get("checklist") or []):
            if it["id"] == "env" and it["status"] != "done":
                it["status"] = "done"
            if it["id"] == map_id:
                it["status"] = "done"
                it["detail"] = det


def check_upstream_tool_versions():
    hdrs = {"User-Agent": f"HB-Cluster-Manager/{MANAGER_VERSION}"}
    hb_v, mkv_v, ff_v, sz_v = "", "", "", ""
    try:
        r_hb = requests.get("https://api.github.com/repos/HandBrake/HandBrake/releases/latest", headers=hdrs, timeout=6)
        if r_hb.status_code == 200:
            hb_v = str(r_hb.json().get("tag_name") or "").lstrip("v").strip()
    except Exception:
        pass
    try:
        r_news = requests.get("https://mkvtoolnix.download/doc/NEWS.md", headers=hdrs, timeout=6)
        if r_news.status_code == 200:
            m_rel = re.search(r"#\s*Version\s+(\d+\.\d+(?:\.\d+)?)", r_news.text, re.IGNORECASE)
            if m_rel:
                mkv_v = m_rel.group(1).strip()
    except Exception:
        pass
    rel_items = []
    for api_url in (
        "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest",
        "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10",
    ):
        try:
            r_ff = requests.get(api_url, headers=hdrs, timeout=8)
            if r_ff.status_code == 200:
                payload_ff = r_ff.json()
                if isinstance(payload_ff, dict):
                    rel_items.append(payload_ff)
                elif isinstance(payload_ff, list):
                    rel_items.extend([x for x in payload_ff if isinstance(x, dict)])
                if pick_best_btbn_stable_ffmpeg_assets(rel_items):
                    break
        except Exception:
            continue
    best_ff = pick_best_btbn_stable_ffmpeg_assets(rel_items)
    ff_v = best_ff.get("version") or probe_btbn_stable_branch_direct(hdrs)
    try:
        r_sz = requests.get("https://api.github.com/repos/ip7z/7zip/releases/latest", headers=hdrs, timeout=6)
        if r_sz.status_code == 200:
            sz_v = str(r_sz.json().get("tag_name") or "").lstrip("v").strip()
    except Exception:
        pass

    with build_lock:
        if hb_v:
            binary_build_state["upstream_versions"]["hb_latest"] = hb_v
        if mkv_v:
            binary_build_state["upstream_versions"]["mkvtoolnix_latest"] = mkv_v
        if ff_v:
            binary_build_state["upstream_versions"]["ffmpeg_latest"] = ff_v
        if sz_v:
            binary_build_state["upstream_versions"]["sevenzip_latest"] = sz_v
        binary_build_state["upstream_versions"]["checked_at"] = int(time.time())
    return binary_build_state["upstream_versions"]


def get_all_binaries_manifest():
    manifest = {"linux": {}, "windows": {}, "binary_generation": AppState.binary_generation}
    with build_lock:
        upd_set = set(binary_build_state.get("updating_tools") or [])
    for os_type in ("linux", "windows"):
        for tool in ALL_TOOLS:
            p = resolve_manager_binary_path(os_type, tool)
            md5, sz = get_file_md5_cached(p) if p else (None, 0)
            ver, is_c = probe_binary_version_string(os_type, tool, p, md5) if (p and md5) else ("", False)
            manifest[os_type][tool] = {
                "available": bool(p and md5),
                "md5": md5,
                "size": sz,
                "version": ver,
                "is_custom": is_c,
                "updating": f"{os_type}_{tool}" in upd_set,
                "filename": p or ""
            }
    dll_p = resolve_manager_binary_path("windows", "7z_dll")
    dll_md5, dll_sz = get_file_md5_cached(dll_p) if dll_p else (None, 0)
    manifest["windows"]["7z_dll"] = {
        "available": bool(dll_p and dll_md5),
        "md5": dll_md5,
        "size": dll_sz,
        "filename": dll_p or ""
    }
    lnch_p = resolve_manager_binary_path("windows", "launcher")
    lnch_md5, lnch_sz = get_file_md5_cached(lnch_p) if lnch_p else (None, 0)
    lnch_ver, _ = probe_binary_version_string("windows", "launcher", lnch_p, lnch_md5) if (lnch_p and lnch_md5) else ("", False)
    manifest["windows"]["launcher"] = {
        "available": bool(lnch_p and lnch_md5),
        "md5": lnch_md5,
        "size": lnch_sz,
        "version": lnch_ver,
        "filename": lnch_p or ""
    }
    return manifest


def get_manager_engine_status(manifest: dict):
    lin = manifest.get("linux") or {}
    hb_m, fp_m, sz_m = lin.get("hbcli") or {}, lin.get("ffprobe") or {}, lin.get("7z") or {}

    hb_p = resolve_manager_binary_path("linux", "hbcli") or shutil.which("HandBrakeCLI")
    fp_p = resolve_manager_binary_path("linux", "ffprobe") or shutil.which("ffprobe")
    sz_p = resolve_manager_binary_path("linux", "7z") or shutil.which("7zz") or shutil.which("7z")

    return {
        "all_ok": bool(hb_p and fp_p and sz_p),
        "hbcli_ok": bool(hb_p),
        "hbcli_version": hb_m.get("version") or ("system" if hb_p else "MISSING"),
        "hbcli_central": bool(hb_m.get("available")),
        "ffprobe_ok": bool(fp_p),
        "ffprobe_version": fp_m.get("version") or ("system" if fp_p else "MISSING"),
        "ffprobe_central": bool(fp_m.get("available")),
        "sevenzip_ok": bool(sz_p),
        "sevenzip_version": sz_m.get("version") or ("system" if sz_p else "MISSING"),
        "sevenzip_central": bool(sz_m.get("available")),
    }


def evaluate_binaries_health(manifest: dict):
    missing = []
    check_lin = has_any_linux_worker()
    check_any = has_any_connected_or_registered_worker()
    os_types_to_check = ("linux", "windows") if check_lin else (("windows",) if check_any else ())
    for os_type in os_types_to_check:
        for tool in ALL_TOOLS:
            t_obj = (manifest.get(os_type) or {}).get(tool) or {}
            if not t_obj.get("available"):
                missing.append(f"{os_type}_{tool}")
        if os_type == "windows":
            dll_obj = (manifest.get("windows") or {}).get("7z_dll") or {}
            if not dll_obj.get("available") and "windows_7z" not in missing:
                missing.append("windows_7z")

    with build_lock:
        up = dict(binary_build_state["upstream_versions"])

    updates = []
    hb_up = up.get("hb_latest")
    mkv_up = up.get("mkvtoolnix_latest")
    ff_up = up.get("ffmpeg_latest")
    sz_up = up.get("sevenzip_latest")

    for os_type in (("linux", "windows") if check_lin else ("windows",)):
        hb_cur = ((manifest.get(os_type) or {}).get("hbcli") or {}).get("version") or ""
        if hb_up and hb_cur and hb_cur != hb_up:
            updates.append(f"{os_type}_hbcli ({hb_cur} -> {hb_up})")
        mkv_cur = ((manifest.get(os_type) or {}).get("mkvmerge") or {}).get("version") or ""
        if mkv_up and mkv_cur and mkv_cur != mkv_up:
            updates.append(f"{os_type}_mkvtoolnix ({mkv_cur} -> {mkv_up})")
        ff_cur = ((manifest.get(os_type) or {}).get("ffmpeg") or {}).get("version") or ""
        if ff_cur and (is_ffmpeg_nightly_version(ff_cur) or (ff_up and ff_up != "latest" and ff_cur != ff_up)):
            updates.append(f"{os_type}_ffmpeg ({ff_cur} -> {ff_up or 'stable'})")
        sz_cur = ((manifest.get(os_type) or {}).get("7z") or {}).get("version") or ""
        if sz_up and sz_cur and sz_cur != sz_up:
            updates.append(f"{os_type}_7z ({sz_cur} -> {sz_up})")

    return missing, updates


FFMPEG_AUTO_MIN_AGE_DAYS = 30


def is_ffmpeg_recent_enough_to_skip_auto(manifest: dict) -> bool:
    """
    FFmpeg nyní sleduje výhradně stabilní větev (např. 8.0 / 7.1 místo denního masteru).
    Starší noční buildy (N-... / vN-...) se nikdy nepřeskakují, aby okamžitě přešly na stabilní větev.
    """
    with build_lock:
        ff_up = str(binary_build_state["upstream_versions"].get("ffmpeg_latest") or "").strip()
    os_types = ("linux", "windows") if has_any_linux_worker() else ("windows",)
    for os_type in os_types:
        for tool in ("ffmpeg", "ffprobe"):
            t_obj = (manifest.get(os_type) or {}).get(tool) or {}
            if not t_obj.get("available"):
                return False
            ver = str(t_obj.get("version") or "").strip()
            if not ver or is_ffmpeg_nightly_version(ver):
                return False
            if ff_up and ff_up != "latest" and not is_ffmpeg_nightly_version(ff_up) and ver != ff_up:
                return False
    return True


def get_outdated_or_missing_build_targets(manifest: dict = None, auto: bool = False, windows_only: bool = False) -> list:
    if manifest is None:
        manifest = get_all_binaries_manifest()
    with build_lock:
        up = dict(binary_build_state["upstream_versions"])

    hb_up = up.get("hb_latest") or ""
    mkv_up = up.get("mkvtoolnix_latest") or ""
    ff_up = up.get("ffmpeg_latest") or ""
    sz_up = up.get("sevenzip_latest") or ""

    check_lin = (not windows_only) and has_any_linux_worker()
    lin = manifest.get("linux") or {}
    win = manifest.get("windows") or {}

    targets = []
    if (check_lin and not (lin.get("7z") or {}).get("available")) or not (win.get("7z") or {}).get("available") or not (win.get("7z_dll") or {}).get("available"):
        targets.append("7z")
    elif sz_up and ((check_lin and ((lin.get("7z") or {}).get("version") or "") != sz_up) or ((win.get("7z") or {}).get("version") or "") != sz_up):
        targets.append("7z")

    if any((check_lin and not (lin.get(k) or {}).get("available")) or not (win.get(k) or {}).get("available") for k in ("mkvmerge", "mkvpropedit")):
        targets.append("mkvtoolnix")
    elif mkv_up and ((check_lin and ((lin.get("mkvmerge") or {}).get("version") or "") != mkv_up) or ((win.get("mkvmerge") or {}).get("version") or "") != mkv_up):
        targets.append("mkvtoolnix")

    lin_ff_v = (lin.get("ffmpeg") or {}).get("version") or ""
    win_ff_v = (win.get("ffmpeg") or {}).get("version") or ""
    if any((check_lin and not (lin.get(k) or {}).get("available")) or not (win.get(k) or {}).get("available") for k in ("ffmpeg", "ffprobe")):
        targets.append("ffmpeg")
    elif (check_lin and is_ffmpeg_nightly_version(lin_ff_v)) or is_ffmpeg_nightly_version(win_ff_v):
        targets.append("ffmpeg")
    elif ff_up and ff_up != "latest" and not is_ffmpeg_nightly_version(ff_up) and ((check_lin and lin_ff_v != ff_up) or win_ff_v != ff_up):
        targets.append("ffmpeg")

    if (check_lin and not (lin.get("hbcli") or {}).get("available")) or not (win.get("hbcli") or {}).get("available"):
        targets.append("hbcli")
    elif hb_up and ((check_lin and ((lin.get("hbcli") or {}).get("version") or "") != hb_up) or ((win.get("hbcli") or {}).get("version") or "") != hb_up):
        targets.append("hbcli")

    lnch = win.get("launcher") or {}
    if not lnch.get("available"):
        targets.append("launcher")
    elif str(lnch.get("version") or "").lstrip("v") != WORKER_VERSION:
        targets.append("launcher")

    return targets


def _store_downloaded_windows_binary(tool_key: str, src_file: str, version_str: str) -> bool:
    dst_p = get_canonical_binary_path("windows", tool_key)
    if not dst_p or not os.path.isfile(src_file) or os.path.getsize(src_file) <= 10000:
        return False
    os.makedirs(os.path.dirname(dst_p), exist_ok=True)
    tmp_dst = dst_p + ".mgr.tmp"
    shutil.copy2(src_file, tmp_dst)
    os.replace(tmp_dst, dst_p)
    safe_chmod_777(dst_p)
    binary_hash_cache.pop("key_" + dst_p, None)
    binary_hash_cache.pop("md5_" + dst_p, None)
    md5, sz = get_file_md5_cached(dst_p)
    clean_v = str(version_str or "").strip().lstrip("v")
    if tool_key in ("ffmpeg", "ffprobe"):
        clean_v = normalize_ffmpeg_stable_branch(clean_v)
    binary_versions_store[f"windows_{tool_key}"] = {
        "version": clean_v,
        "md5": md5,
        "is_custom": False,
        "updated_at": int(time.time()),
    }
    save_binary_versions_store()
    mark_checklist_binary_uploaded("windows", tool_key, clean_v, sz)
    return True


def _set_checklist_item_status(item_id: str, status: str, detail: str = None):
    with build_lock:
        for it in (binary_build_state.get("checklist") or []):
            if it.get("id") == item_id:
                it["status"] = status
                if detail is not None:
                    it["detail"] = detail


def start_manager_windows_binaries_download(targets: list = None, force_rebuild: bool = False) -> dict:
    """
    Režim pro klastr bez Linux workeru (NAS Manager + pouze Windows workery):
    Manager ve vlastním vlákně na pozadí stáhne a rozbalí Windows binárky
    (7z.exe, 7z.dll, mkvmerge.exe, mkvpropedit.exe, ffmpeg.exe, ffprobe.exe, HandBrakeCLI.exe)
    a podepíše HBWorker.exe přímo do /data/binaries/windows/.
    """
    ensure_signed_windows_launcher_on_manager()
    t_list = [str(x).lower().strip() for x in (targets or ["all"]) if str(x).strip()]
    if not t_list:
        t_list = ["all"]
    if not force_rebuild and t_list == ["all"]:
        needed = get_outdated_or_missing_build_targets(auto=False, windows_only=True)
        if not needed:
            return {"ok": True, "already_up_to_date": True, "targets": []}
        t_list = needed

    t_set = set(t_list)
    do_all = "all" in t_set
    upd_tools = []
    if do_all or "hbcli" in t_set:
        upd_tools.append("windows_hbcli")
    if do_all or "ffmpeg" in t_set:
        upd_tools.extend(["windows_ffmpeg", "windows_ffprobe"])
    if do_all or "mkvtoolnix" in t_set:
        upd_tools.extend(["windows_mkvmerge", "windows_mkvpropedit"])
    if do_all or "7z" in t_set:
        upd_tools.append("windows_7z")

    with build_lock:
        if binary_build_state.get("status") in ("QUEUED", "BUILDING"):
            return {"ok": False, "already_running": True, "assigned_worker": binary_build_state.get("assigned_worker") or "HB Manager"}
        binary_build_state["status"] = "BUILDING"
        binary_build_state["job_id"] = None
        binary_build_state["assigned_worker"] = "HB Manager"
        binary_build_state["fallback_used"] = False
        binary_build_state["progress"] = 2.0
        binary_build_state["step_message"] = T("mgr_win_prep")
        binary_build_state["updating_tools"] = upd_tools
        binary_build_state["checklist"] = create_initial_build_checklist(t_list, windows_only=True)
        binary_build_state["console_lines"].clear()
        binary_build_state["started_at"] = int(time.time())
        binary_build_state["finished_at"] = 0

    def _worker_fn():
        work_dir = tempfile.mkdtemp(prefix="hb_mgr_win_dl_")
        sz_bin = shutil.which("7zz") or shutil.which("7z") or resolve_manager_binary_path("linux", "7z")
        headers = {"User-Agent": f"HB-Cluster-Manager/{MANAGER_VERSION}"}

        def _set_step(prog: float, msg: str):
            with build_lock:
                binary_build_state["progress"] = round(float(prog), 1)
                binary_build_state["step_message"] = msg
            append_build_console_line(msg)

        def _download_url(url: str, dst_path: str):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as r, open(dst_path, "wb") as f:
                shutil.copyfileobj(r, f)

        try:
            _set_step(5.0, T("mgr_win_query_versions"))
            up = check_upstream_tool_versions()
            hb_ver = str(up.get("hb_latest") or "1.9.2").lstrip("v")
            mkv_ver = str(up.get("mkvtoolnix_latest") or "89.0").lstrip("v")
            ff_ver = str(up.get("ffmpeg_latest") or "7.1").lstrip("v")
            sz_ver = str(up.get("sevenzip_latest") or "24.09").lstrip("v")

            # 1. Windows 7-Zip (7z.exe + 7z.dll)
            if do_all or "7z" in t_set:
                _set_checklist_item_status("win_7z", "running")
                _set_step(15.0, T("mgr_win_dl_7z", sz_ver))
                sz_nodot = sz_ver.replace(".", "")
                sz_exe_dl = os.path.join(work_dir, "7z_win_setup.exe")
                sz_ext_dir = os.path.join(work_dir, "7z_ext")
                os.makedirs(sz_ext_dir, exist_ok=True)
                try:
                    _download_url(f"https://www.7-zip.org/a/7z{sz_nodot}-x64.exe", sz_exe_dl)
                except Exception:
                    sz_ver = "24.09"
                    _download_url("https://www.7-zip.org/a/7z2409-x64.exe", sz_exe_dl)
                if not sz_bin:
                    raise RuntimeError(T("mgr_win_err_7z_missing"))
                subprocess.run([sz_bin, "e", "-y", sz_exe_dl, f"-o{sz_ext_dir}", "7z.exe", "7z.dll", "-r"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                _store_downloaded_windows_binary("7z", os.path.join(sz_ext_dir, "7z.exe"), sz_ver)
                _store_downloaded_windows_binary("7z_dll", os.path.join(sz_ext_dir, "7z.dll"), sz_ver)
                _set_checklist_item_status("win_7z", "done")

            # 2. Windows MKVToolNix (mkvmerge.exe + mkvpropedit.exe)
            if do_all or "mkvtoolnix" in t_set:
                _set_checklist_item_status("win_mkv", "running")
                _set_step(35.0, T("mgr_win_dl_mkv", mkv_ver))
                mkv_7z_dl = os.path.join(work_dir, "mkv_win.7z")
                mkv_ext_dir = os.path.join(work_dir, "mkv_ext")
                os.makedirs(mkv_ext_dir, exist_ok=True)
                _download_url(f"https://mkvtoolnix.download/windows/releases/{mkv_ver}/mkvtoolnix-64-bit-{mkv_ver}.7z", mkv_7z_dl)
                if not sz_bin:
                    raise RuntimeError(T("mgr_win_err_mkv_7z_missing"))
                subprocess.run([sz_bin, "e", "-y", mkv_7z_dl, f"-o{mkv_ext_dir}", "mkvmerge.exe", "mkvpropedit.exe", "-r"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                _store_downloaded_windows_binary("mkvmerge", os.path.join(mkv_ext_dir, "mkvmerge.exe"), mkv_ver)
                _store_downloaded_windows_binary("mkvpropedit", os.path.join(mkv_ext_dir, "mkvpropedit.exe"), mkv_ver)
                _set_checklist_item_status("win_mkv", "done")

            # 3. Windows FFmpeg & FFprobe
            if do_all or "ffmpeg" in t_set:
                _set_checklist_item_status("win_ff", "running")
                _set_step(60.0, T("mgr_win_dl_ff", ff_ver))
                ff_zip_dl = os.path.join(work_dir, "ffmpeg_win.zip")
                ff_ext_dir = os.path.join(work_dir, "ff_ext")
                os.makedirs(ff_ext_dir, exist_ok=True)
                ff_urls = []
                if ff_ver and ff_ver != "latest":
                    ff_urls.append(f"https://github.com/GyanD/codexffmpeg/releases/download/{ff_ver}/ffmpeg-{ff_ver}-full_build.zip")
                ff_urls.append("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")
                dl_ok = False
                for u in ff_urls:
                    try:
                        _download_url(u, ff_zip_dl)
                        if os.path.isfile(ff_zip_dl) and os.path.getsize(ff_zip_dl) > 10_000_000:
                            dl_ok = True
                            break
                    except Exception:
                        pass
                if not dl_ok:
                    raise RuntimeError(T("mgr_win_err_ff_dl"))
                with zipfile.ZipFile(ff_zip_dl, "r") as zf:
                    for m in zf.namelist():
                        bn = os.path.basename(m).lower()
                        if bn in ("ffmpeg.exe", "ffprobe.exe"):
                            with zf.open(m) as src_s, open(os.path.join(ff_ext_dir, bn), "wb") as dst_s:
                                shutil.copyfileobj(src_s, dst_s)
                _store_downloaded_windows_binary("ffmpeg", os.path.join(ff_ext_dir, "ffmpeg.exe"), ff_ver)
                _store_downloaded_windows_binary("ffprobe", os.path.join(ff_ext_dir, "ffprobe.exe"), ff_ver)
                _set_checklist_item_status("win_ff", "done")

            # 4. Windows HandBrakeCLI.exe
            if do_all or "hbcli" in t_set:
                _set_checklist_item_status("win_hb", "running")
                _set_step(85.0, T("mgr_win_dl_hb", hb_ver))
                hb_zip_dl = os.path.join(work_dir, "hb_win.zip")
                hb_ext_dir = os.path.join(work_dir, "hb_ext")
                os.makedirs(hb_ext_dir, exist_ok=True)
                _download_url(f"https://github.com/HandBrake/HandBrake/releases/download/{hb_ver}/HandBrakeCLI-{hb_ver}-win-x86_64.zip", hb_zip_dl)
                with zipfile.ZipFile(hb_zip_dl, "r") as zf:
                    for m in zf.namelist():
                        if os.path.basename(m).lower() == "handbrakecli.exe":
                            with zf.open(m) as src_s, open(os.path.join(hb_ext_dir, "HandBrakeCLI.exe"), "wb") as dst_s:
                                shutil.copyfileobj(src_s, dst_s)
                _store_downloaded_windows_binary("hbcli", os.path.join(hb_ext_dir, "HandBrakeCLI.exe"), hb_ver)
                _set_checklist_item_status("win_hb", "done")

            # 5. Windows Native Launcher (HBWorker.exe)
            if do_all or "launcher" in t_set:
                _set_checklist_item_status("win_launcher", "running")
                _set_step(95.0, T("mgr_win_sign_launcher", WORKER_VERSION))
                if ensure_signed_windows_launcher_on_manager(force=True):
                    lp = resolve_manager_binary_path("windows", "launcher")
                    _, l_sz = get_file_md5_cached(lp) if lp else (None, 0)
                    mark_checklist_binary_uploaded("windows", "launcher", WORKER_VERSION, l_sz)
                    _set_checklist_item_status("win_launcher", "done")

            AppState.binary_generation = int(getattr(AppState, "binary_generation", 1) or 1) + 1
            save_manager_config()
            save_binary_versions_store()
            with build_lock:
                binary_build_state["status"] = "COMPLETED"
                binary_build_state["progress"] = 100.0
                binary_build_state["updating_tools"] = []
                binary_build_state["step_message"] = T("mgr_win_completed_step")
                binary_build_state["finished_at"] = int(time.time())
            append_build_console_line(T("mgr_win_completed_console"))
            append_manager_log(T("mgr_win_completed_log"))
        except Exception as e:
            err_msg = T("mgr_win_error_log", e)
            logger.error(err_msg)
            append_build_console_line(f"ERROR: {err_msg}")
            append_manager_log(f"[Binaries] {err_msg}")
            with build_lock:
                for it in (binary_build_state.get("checklist") or []):
                    if it.get("status") == "running":
                        it["status"] = "error"
                        it["detail"] = str(e)[:120]
                binary_build_state["status"] = "ERROR"
                binary_build_state["updating_tools"] = []
                binary_build_state["step_message"] = err_msg
                binary_build_state["finished_at"] = int(time.time())
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _guarded_worker_fn():
        # Pojistka: chyba mimo vnitřní try (např. při přípravě work_dir) nesmí nechat stav viset v "BUILDING".
        try:
            _worker_fn()
        except Exception as e:
            logger.exception("[Binaries] Manager Windows download thread crashed")
            with build_lock:
                if binary_build_state.get("status") == "BUILDING":
                    binary_build_state["status"] = "ERROR"
                    binary_build_state["updating_tools"] = []
                    binary_build_state["step_message"] = T("mgr_win_error_log", e)
                    binary_build_state["finished_at"] = int(time.time())

    threading.Thread(target=_guarded_worker_fn, daemon=True).start()
    return {"ok": True, "manager_direct": True, "assigned_worker": "HB Manager", "targets": t_list}
# ==============================================================================
# 6. PARSOVÁNÍ MÉDIÍ, UDF .ISO ČTEČKA, STATISTIKY A POMOCNÉ FUNKCE FRONTY
# ==============================================================================
media_cache = OrderedDict()
media_cache_lock = threading.Lock()
media_cache_version = 1
job_info_cache = OrderedDict()
iso_playlist_size_cache = OrderedDict()
ffprobe_queue = queue.Queue()
ffprobe_queued_paths = OrderedDict()
MEDIA_CACHE_MAX = 5000
JOB_INFO_CACHE_MAX = 5000
ISO_SIZE_CACHE_MAX = 2000


def load_media_cache():
    if os.path.exists(MEDIA_CACHE_FILE):
        try:
            with open(MEDIA_CACHE_FILE, 'r', encoding='utf-8-sig') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    with media_cache_lock:
                        media_cache.clear()
                        for k, v in loaded.items():
                            if isinstance(v, dict) and (v.get("w") or v.get("h")):
                                _lru_set(media_cache, k, v, MEDIA_CACHE_MAX)
        except Exception:
            pass


def save_media_cache():
    try:
        with media_cache_lock:
            valid = {k: v for k, v in media_cache.items() if isinstance(v, dict) and (v.get("w") or v.get("h"))}
        atomic_write_json(MEDIA_CACHE_FILE, valid, mode=0o644, indent=None)
    except Exception:
        pass


_runtime_bootstrapped = False
_runtime_bootstrap_lock = threading.Lock()


def bootstrap_manager_runtime():
    """Jednorázová inicializace disku/DB/konfigurace/binárek – volá se z FastAPI lifespan, NIKDY při --selftest."""
    global _runtime_bootstrapped
    if _runtime_bootstrapped:
        return
    with _runtime_bootstrap_lock:
        if _runtime_bootstrapped:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        configure_database_pool_and_wal()
        init_database_and_migrations()
        load_manager_config()
        migrate_legacy_binaries_dir()
        load_binary_versions_store()
        ensure_binary_dirs()
        ensure_signed_windows_launcher_on_manager()
        load_media_cache()
        _runtime_bootstrapped = True


def unwrap_job_payload_nodes(payload):
    """Sjednocený rozbalovač JSON úlohy (vrací dvojici `(root_dict, job_node_dict)`)."""
    pl = payload or {}
    if isinstance(pl, str):
        try:
            pl = json.loads(pl)
        except Exception:
            pl = {}
    if isinstance(pl, list) and pl:
        pl = pl[0]
    if not isinstance(pl, dict):
        return {}, {}
    jn = pl.get("Task") or pl.get("Job") or pl
    return pl, (jn if isinstance(jn, dict) else {})


def _to_int(v):
    try:
        return int(round(float(v))) if v is not None else 0
    except Exception:
        return 0


def is_system_build_job(job: models.Job) -> bool:
    if not job:
        return False
    # Systémovou build úlohu zakládá výhradně Manager (preset_name == "SYSTEM_BUILD");
    # importovaný JSON z fronty se za SYSTEM_BUILD nikdy nesmí vydávat.
    return getattr(job, "preset_name", None) == "SYSTEM_BUILD"


def renumber_waiting_queue(db_session):
    waiting = (
        db_session.query(models.Job)
        .options(defer(models.Job.hb_json_payload))
        .filter(models.Job.status.in_(["WAITING", "PAUSED"]))
        .order_by(models.Job.priority.asc(), models.Job.created_at.asc(), models.Job.id.asc())
        .all()
    )

    pos = 1
    for j in waiting:
        if is_system_build_job(j):
            if j.priority != 0:
                j.priority = 0
        else:
            if j.priority != pos:
                j.priority = pos
            pos += 1


def next_top_priority(db_session) -> int:
    """Vrátí prioritu pro zařazení běžné úlohy na začátek fronty bez přepisování celé fronty (O(1) zápis)."""
    row = (
        db_session.query(func.min(models.Job.priority))
        .filter(
            models.Job.status.in_(["WAITING", "PAUSED"]),
            models.Job.preset_name != "SYSTEM_BUILD",
        )
        .first()
    )
    min_p = row[0] if (row and row[0] is not None) else None
    if min_p is None:
        return 1
    return int(min_p) - 1


def next_bottom_priority(db_session) -> int:
    """Vrátí prioritu pro zařazení běžné úlohy na konec fronty bez přepisování celé fronty (O(1) zápis)."""
    row = (
        db_session.query(func.max(models.Job.priority))
        .filter(
            models.Job.status.in_(["WAITING", "PAUSED"]),
            models.Job.preset_name != "SYSTEM_BUILD",
        )
        .first()
    )
    max_p = row[0] if (row and row[0] is not None) else 0
    return max(1, int(max_p) + 1)


def is_linux_worker(w_name: str) -> bool:
    diag = (worker_telemetry.get(w_name) or {}).get("diag") or {}
    return "linux" in str(diag.get("os") or "").lower()


def select_best_linux_builder_worker(db_session):
    """
    2-tier Linux builder selection:
    Tier 1: Strongest IDLE online Linux worker (0 active compute jobs).
    Tier 2: Strongest BUSY online Linux worker (runs SYSTEM_BUILD concurrently, bypassing slot limits).
    """
    now_ts = time.time()
    online_lin = [w for w, t in active_workers.items() if (now_ts - t) <= 45 and is_linux_worker(w)]
    if not online_lin:
        return None, False, []

    busy_workers = set()
    if db_session is not None:
        try:
            active_rows = (
                db_session.query(models.Job.worker_name)
                .filter(
                    models.Job.status.in_(COMPUTE_ACTIVE_STATUSES),
                    models.Job.preset_name != "SYSTEM_BUILD",
                    models.Job.worker_name.isnot(None),
                )
                .all()
            )
            busy_workers = {str(r[0]) for r in active_rows if r and r[0]}
        except Exception:
            busy_workers = set()

    scored = []
    for w_name in online_lin:
        telem = worker_telemetry.get(w_name) or {}
        diag = telem.get("diag") or {}
        cores = float(telem.get("cores") or 1.0)
        free_gb = float(diag.get("temp_free_gb") or 0.0)
        cpu_m = str(diag.get("cpu_model") or "").lower()
        ver_bonus = 25.0 if worker_versions.get(w_name, "") == WORKER_VERSION else 0.0
        r9_bonus = 15.0 if any(k in cpu_m for k in ("9900x", "7950x", "9950x", "ryzen 9")) else 0.0
        sp_pen = -50.0 if free_gb < 6.0 else 0.0
        is_idle = w_name not in busy_workers
        hw_score = round((cores * 2.0) + ver_bonus + r9_bonus + sp_pen, 1)
        scored.append({
            "worker_name": w_name,
            "score": hw_score,
            "tier": 1 if is_idle else 2,
            "is_idle": is_idle,
            "cores": telem.get("cores", 0),
            "cpu_model": diag.get("cpu_model", "Linux CPU"),
            "temp_free_gb": diag.get("temp_free_gb", 0.0),
        })
    scored.sort(key=lambda x: (1 if x["is_idle"] else 0, x["score"]), reverse=True)

    pref = AppState.preferred_builder_worker or "AUTO"
    if pref != "AUTO":
        if pref in online_lin:
            return pref, False, scored
        return scored[0]["worker_name"], True, scored
    return scored[0]["worker_name"], False, scored


def is_worker_premux_capable(w_name: str, use_local_temp: bool) -> bool:
    if not use_local_temp:
        return False
    diag = (worker_telemetry.get(w_name) or {}).get("diag")
    return True if not diag else bool(diag.get("mkvmerge_ok") and diag.get("sevenzip_ok", True))


def extract_basename(path_str):
    if not path_str:
        return "Unknown_source"
    pts = [p for p in str(path_str).replace('\\', '/').rstrip('/').split('/') if p and ':' not in p]
    return pts[-1] if pts else "Unknown_source"


def extract_orig_source_from_payload(payload):
    pl, j_node = unwrap_job_payload_nodes(payload)
    if not pl:
        return ""
    src = j_node.get("Source")
    if isinstance(src, dict) and src.get("Path"):
        return str(src.get("Path"))
    if isinstance(src, str) and src:
        return src
    if pl.get("ScannedSourcePath"):
        return str(pl.get("ScannedSourcePath"))
    t_node = pl.get("Title")
    if isinstance(t_node, dict) and t_node.get("Path"):
        return str(t_node.get("Path"))
    ui_s = pl.get("uiSettings")
    if isinstance(ui_s, dict) and ui_s.get("source"):
        return str(ui_s.get("source"))
    return ""


def is_path_within(root: str, path: str) -> bool:
    """True, když skutečná cesta (po rozbalení symlinků) leží uvnitř root (nebo je to root sám)."""
    if not root or not path:
        return False
    try:
        r = os.path.realpath(root)
        p = os.path.realpath(path)
        return os.path.commonpath([r, p]) == r
    except Exception:
        return False


_ID_SUFFIX_RE = re.compile(r" \[ID \d+(?:-\d+)?\]$")


def free_output_path(dest_path: str, job_id) -> str:
    """Výstup nikdy nepřepíše existující soubor: když dest_path existuje, vrátí „Název [ID 1234].mkv“
    (pak „[ID 1234-2]“ …). Stejné pravidlo jako worker_core.free_output_path ve workerech."""
    if not dest_path or not os.path.lexists(dest_path):
        return dest_path
    stem, ext = os.path.splitext(dest_path)
    stem = _ID_SUFFIX_RE.sub("", stem)
    for n in range(1, 1000):
        cand = f"{stem} [ID {job_id}]{ext}" if n == 1 else f"{stem} [ID {job_id}-{n}]{ext}"
        if not os.path.lexists(cand):
            return cand
    raise RuntimeError(f"No free output file name for {dest_path}")


def resolve_input_path_smart(raw_path: str, input_root: str = None):
    """Najde zdroj úlohy v Inputs Manageru. Vrací (plná cesta, cesta relativně k Inputs, nalezeno).
    Zdroj smí ležet JEN uvnitř Inputs: absolutní cesty mimo, „..“ ani symlink ven se nikdy nepoužijí
    (jinak by relay / ffprobe mohly sáhnout na libovolný soubor Manageru, třeba /data)."""
    if not raw_path:
        return "", "", False
    if input_root is None:
        input_root = get_manager_inputs_dir()
    root = input_root or "/inputs"
    if os.path.isabs(str(raw_path)) and os.path.exists(raw_path) and is_path_within(root, raw_path):
        try:
            rel_a = os.path.relpath(os.path.realpath(raw_path), os.path.realpath(root)).replace("\\", "/")
        except Exception:
            rel_a = os.path.basename(raw_path)
        return raw_path, ("" if rel_a == "." else rel_a), True
    pts = [p for p in str(raw_path).replace('\\', '/').strip('/').split('/') if p and p not in (".", "..") and not p.endswith(':')]
    if os.path.exists(root):
        for i in range(len(pts)):
            rel_c = "/".join(pts[i:])
            full_c = os.path.normpath(os.path.join(root, rel_c))
            if os.path.exists(full_c) and is_path_within(root, full_c):
                return full_c, rel_c, True
    fb_rel = "/".join(pts[1:]) if len(pts) > 1 else (pts[0] if pts else "")
    return os.path.normpath(os.path.join(root, fb_rel)), fb_rel, False


def is_job_disc_or_iso(job: models.Job, allow_disk_check: bool = False) -> bool:
    persisted = getattr(job, "is_disc_source", None)
    if persisted is True:
        return True
    src_raw = getattr(job, "source_path", None)
    if not src_raw and allow_disk_check:
        src_raw = extract_orig_source_from_payload(getattr(job, "hb_json_payload", None))
    orig_src = (src_raw or "").lower()
    if orig_src.endswith(".iso") or "bdmv" in orig_src or "video_ts" in orig_src:
        return True
    if persisted is False and not allow_disk_check:
        return False
    if allow_disk_check and orig_src:
        full_p, _, found = resolve_input_path_smart(orig_src, get_manager_inputs_dir())
        return bool(found and os.path.isdir(full_p))
    return False


def enforce_premux_local_disk_guard(db_session):
    policies = {p.worker_name: p for p in db_session.query(models.WorkerPolicy).all()}
    if policies and not any(bool(p.use_local_temp) for p in policies.values()):
        if AppState.smart_premux:
            AppState.smart_premux = False
            save_manager_config()
        return False
    return True


def record_telemetry_sample(w_name: str, telem: dict):
    if not w_name or not isinstance(telem, dict):
        return
    diag = telem.get("diag")
    if isinstance(diag, dict):
        if "inputs_ok" not in diag and "nas_dl_ok" in diag:
            diag["inputs_ok"] = bool(diag["nas_dl_ok"])
        if "outputs_ok" not in diag and "nas_out_ok" in diag:
            diag["outputs_ok"] = bool(diag["nas_out_ok"])
        if "nas_dl_ok" not in diag and "inputs_ok" in diag:
            diag["nas_dl_ok"] = bool(diag["inputs_ok"])
        if "nas_out_ok" not in diag and "outputs_ok" in diag:
            diag["nas_out_ok"] = bool(diag["outputs_ok"])

    worker_telemetry[w_name] = telem
    hist = worker_telemetry_history.setdefault(w_name, deque(maxlen=60))
    now_ts = int(time.time())
    if not hist or (now_ts - hist[-1]["ts"]) >= 5:
        hist.append({
            "ts": now_ts,
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "cpu_total": telem.get("cpu_total", 0.0),
            "cpu_hb": telem.get("cpu_hb", 0.0),
            "ram_used_gb": telem.get("ram_used_gb", 0.0),
            "ram_hb_gb": telem.get("ram_hb_gb", 0.0),
        })


def find_probeable_file_in_dir(dir_path):
    best_f, best_sz = None, -1
    exts = ('.mkv', '.mp4', '.m4v', '.m2ts', '.ts', '.vob', '.avi', '.mov', '.wmv')
    try:
        for root_d, _, files in os.walk(dir_path):
            for f in files:
                fl = f.lower()
                if fl == "video_ts.vob" or fl.startswith("vts_01_0."):
                    continue
                if fl.endswith(exts):
                    fp = os.path.join(root_d, f)
                    try:
                        sz = os.path.getsize(fp)
                        if sz > best_sz:
                            best_sz, best_f = sz, fp
                    except Exception:
                        pass
    except Exception:
        pass
    return best_f


def parse_mpls_playitem_clips_only(mpls_bytes: bytes) -> list:
    if not mpls_bytes or len(mpls_bytes) < 64 or b"MPLS" not in mpls_bytes[:8]:
        return []
    clips = []
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
                if clip_id.isdigit() and codec_id == b"M2TS" and clip_id not in clips:
                    clips.append(clip_id)
                pos += 2 + item_len
    except Exception:
        pass
    if not clips:
        for c in re.findall(rb"(\d{5})M2TS", mpls_bytes):
            cs = c.decode("ascii")
            if cs not in clips:
                clips.append(cs)
    return clips


def inspect_bluray_iso_via_7z(iso_path: str, mpls_upper: str = "") -> int:
    sz_bin = resolve_manager_binary_path("linux", "7z") or shutil.which("7zz") or shutil.which("7z")
    if not sz_bin or not os.path.isfile(iso_path):
        return 0
    try:
        clips = []
        if mpls_upper:
            res_mpls = subprocess.run(
                [sz_bin, "e", "-so", iso_path, f"-ir!*{mpls_upper}", f"-ir!*{mpls_upper.lower()}"],
                capture_output=True, timeout=20
            )
            mpls_bytes = res_mpls.stdout or b""
            if b"MPLS" in mpls_bytes[:8]:
                clips = parse_mpls_playitem_clips_only(mpls_bytes)

        res_list = subprocess.run([sz_bin, "l", "-slt", iso_path], capture_output=True, text=True, errors="ignore", timeout=25)
        clip_sizes, curr_p = {}, ""
        for line in (res_list.stdout or "").splitlines():
            ls = line.strip()
            if ls.startswith("Path = "):
                curr_p = os.path.basename(ls[7:].strip().replace("\\", "/")).upper()
            elif ls.startswith("Size = ") and curr_p.endswith(".M2TS"):
                c_id = curr_p[:5]
                try:
                    sv = int(ls[7:].strip())
                    if sv > 0:
                        clip_sizes[c_id] = max(clip_sizes.get(c_id, 0), sv)
                except Exception:
                    pass
        if clips:
            matched_sz = sum(clip_sizes.get(c_id, 0) for c_id in clips if c_id in clip_sizes)
            if matched_sz > 0:
                return matched_sz
        if clip_sizes:
            return max(clip_sizes.values())
    except Exception:
        pass
    return 0


def inspect_bluray_playlist_bytes(source_path: str, mpls_name: str = "", min_valid_bytes: int = 100 * 1024 * 1024) -> int:
    if not source_path or not os.path.exists(source_path):
        return 0
    mpls_up = (mpls_name or "").strip().upper()
    if mpls_up and not mpls_up.endswith(".MPLS"):
        mpls_up = f"{int(mpls_up):05d}.MPLS" if mpls_up.isdigit() else ""

    eff_min_pl = 5 * 1024 * 1024 if mpls_up else min_valid_bytes
    ck = (source_path, mpls_up or "__AUTO__")
    with media_cache_lock:
        cached_sz = iso_playlist_size_cache.get(ck)
    if cached_sz is not None and cached_sz >= eff_min_pl:
        return cached_sz

    if os.path.isdir(source_path):
        try:
            if mpls_up:
                for root_d, _, files in os.walk(source_path):
                    for f in files:
                        if f.upper() == mpls_up:
                            with open(os.path.join(root_d, f), "rb") as mf:
                                clips = parse_mpls_playitem_clips_only(mf.read())
                            if clips:
                                stream_d = os.path.join(os.path.dirname(root_d), "STREAM")
                                if os.path.isdir(stream_d):
                                    pl_b = 0
                                    for c_id in clips:
                                        for ext_c in (".m2ts", ".M2TS"):
                                            cfp = os.path.join(stream_d, c_id + ext_c)
                                            if os.path.isfile(cfp):
                                                pl_b += os.path.getsize(cfp)
                                                break
                                    if pl_b >= eff_min_pl:
                                        with media_cache_lock:
                                            _lru_set(iso_playlist_size_cache, ck, pl_b, ISO_SIZE_CACHE_MAX)
                                        return pl_b
            largest_f = find_probeable_file_in_dir(source_path)
            if largest_f and os.path.isfile(largest_f):
                lsz = os.path.getsize(largest_f)
                if lsz >= eff_min_pl:
                    with media_cache_lock:
                        _lru_set(iso_playlist_size_cache, ck, lsz, ISO_SIZE_CACHE_MAX)
                    return lsz
        except Exception:
            pass
        return 0

    if os.path.isfile(source_path) and source_path.lower().endswith(".iso"):
        iso_sz = os.path.getsize(source_path)
        eff_min = eff_min_pl if mpls_up else (max(min_valid_bytes, 500 * 1024 * 1024) if iso_sz > 2 * (1024**3) else min_valid_bytes)
        try:
            with open(source_path, "rb") as f:
                buf = f.read(64 * 1024 * 1024)
            if len(buf) >= 65536:
                efe_sz, efe_pos = {}, {}
                b_len = len(buf)
                for pos in range(0, b_len - 2048, 2048):
                    tid = struct.unpack_from("<H", buf, pos)[0]
                    if tid in (261, 266):
                        tloc = struct.unpack_from("<I", buf, pos + 12)[0]
                        ilen = struct.unpack_from("<Q", buf, pos + 56)[0]
                        if tloc > 0 and ilen > 0:
                            efe_sz[tloc], efe_pos[tloc] = ilen, pos

                if efe_sz:
                    m2ts_map, mpls_lbn = {}, None
                    for m in re.finditer(rb"(?:\x08([0-9]{5}\.(?:M2TS|MPLS|m2ts|mpls))|\x10((?:\x00[0-9A-Za-z\.]){10}))", buf):
                        mpos = m.start()
                        fn = m.group(1).decode("ascii", errors="ignore").upper() if m.group(1) else m.group(2)[1::2].decode("ascii", errors="ignore").upper()
                        fstart = mpos - 38
                        if fstart >= 0 and buf[fstart:fstart + 2] == b"\x01\x01":
                            lbn = struct.unpack_from("<I", buf, fstart + 24)[0]
                            if fn.endswith(".M2TS") and lbn in efe_sz:
                                cid = fn[:5]
                                m2ts_map[cid] = max(m2ts_map.get(cid, 0), efe_sz[lbn])
                            elif mpls_up and fn == mpls_up:
                                mpls_lbn = lbn

                    if m2ts_map and mpls_lbn and mpls_lbn in efe_pos:
                        epos = efe_pos[mpls_lbn]
                        tid = struct.unpack_from("<H", buf, epos)[0]
                        iflags = struct.unpack_from("<H", buf, epos + 30)[0] & 0x07
                        lea = struct.unpack_from("<I", buf, epos + (208 if tid == 266 else 168))[0]
                        adoff = epos + (216 if tid == 266 else 176) + lea
                        mbuf = b""
                        if iflags == 3 and adoff + 64 < b_len:
                            mbuf = buf[adoff:adoff + min(efe_sz[mpls_lbn], 16384)]
                        elif iflags in (0, 1) and adoff + 8 <= b_len:
                            adlbn = struct.unpack_from("<I", buf, adoff + 4)[0]
                            tpos = epos + ((adlbn - mpls_lbn) * 2048)
                            if 0 <= tpos < b_len - 2048 and buf[tpos:tpos + 4] == b"MPLS":
                                mbuf = buf[tpos:tpos + min(efe_sz[mpls_lbn], 65536)]

                        if mbuf and b"MPLS" in mbuf[:8]:
                            clips = parse_mpls_playitem_clips_only(mbuf)
                            if clips and all(cid in m2ts_map for cid in clips):
                                tot_b = sum(m2ts_map.get(cid, 0) for cid in clips)
                                if tot_b >= eff_min:
                                    with media_cache_lock:
                                        _lru_set(iso_playlist_size_cache, ck, tot_b, ISO_SIZE_CACHE_MAX)
                                    return tot_b

                    if not mpls_up and m2ts_map:
                        max_m2ts = max(m2ts_map.values())
                        if max_m2ts >= eff_min:
                            res_sz = iso_sz if (iso_sz > 20 * (1024**3) and max_m2ts < iso_sz * 0.35) else max_m2ts
                            with media_cache_lock:
                                _lru_set(iso_playlist_size_cache, ck, res_sz, ISO_SIZE_CACHE_MAX)
                            return res_sz
        except Exception:
            pass

        sz_fb = inspect_bluray_iso_via_7z(source_path, mpls_up)
        if sz_fb >= eff_min:
            res_sz = iso_sz if (not mpls_up and iso_sz > 20 * (1024**3) and sz_fb < iso_sz * 0.35) else sz_fb
            with media_cache_lock:
                _lru_set(iso_playlist_size_cache, ck, res_sz, ISO_SIZE_CACHE_MAX)
            return res_sz
    return 0


def estimate_disc_bitrates_from_size_and_duration(src_size_bytes: int, duration_sec: float, payload: dict = None, existing_a_kbps: int = 0):
    if not src_size_bytes or src_size_bytes < 50 * 1024 * 1024 or not duration_sec or duration_sec <= 30.0:
        return 0, existing_a_kbps or 0

    a_kbps = int(existing_a_kbps or 0)
    if a_kbps <= 0 and payload:
        it, j_node = unwrap_job_payload_nodes(payload)
        if it:
            t_meta = it.get("Title") if isinstance(it.get("Title"), dict) else {}
            s_info = it.get("SourceTitleInfo") if isinstance(it.get("SourceTitleInfo"), dict) else {}
            for at in (t_meta.get("AudioList") or s_info.get("AudioTracks") or []):
                if isinstance(at, dict):
                    br_r = int(at.get("BitRate") or at.get("Bitrate") or 0)
                    dl = str(at.get("Description") or "").lower()
                    if 1000 < br_r <= 8000000:
                        a_kbps += int(round(br_r / 1000.0))
                    elif any(k in dl for k in ("dts-hd", "truehd", "dts-x", "atmos")):
                        a_kbps += 3200
                    elif "pcm" in dl:
                        a_kbps += 1536
                    elif "ac3" in dl or "dts" in dl:
                        a_kbps += 640
            if a_kbps <= 0 and j_node:
                a_tr = j_node.get("AudioTracks") or ((j_node.get("Audio") or {}).get("AudioList") if isinstance(j_node.get("Audio"), dict) else []) or []
                for t in a_tr:
                    if isinstance(t, dict):
                        enc = t.get("Encoder")
                        es = str(enc.get("ShortName") if isinstance(enc, dict) else (enc or "")).lower()
                        if "dtshd" in es or "truehd" in es:
                            a_kbps += 3200
                        elif any(k in es for k in ("dts", "ac3", "eac3")):
                            a_kbps += 640
        a_kbps = min(a_kbps, 12000)

    if a_kbps <= 0:
        a_kbps = 2500

    stream_payload_bytes = int(src_size_bytes * 0.94)
    total_kbps = int(round((stream_payload_bytes * 8.0) / (float(duration_sec) * 1000.0)))
    eff_a = min(a_kbps, int(total_kbps * 0.35))
    v_kbps = max(0, total_kbps - eff_a) if total_kbps > eff_a + 1000 else 0
    return (0 if v_kbps == 2500 else v_kbps), eff_a


def extract_title_and_range_from_job_node(job_node: dict, root_payload: dict = None):
    if not isinstance(job_node, dict):
        return 1, "", ""
    src_d = job_node.get("Source") if isinstance(job_node.get("Source"), dict) else {}
    r_title = (root_payload.get("Title") if isinstance(root_payload, dict) and isinstance(root_payload.get("Title"), dict) else {})
    title = job_node.get("Title")
    if title is None:
        title = src_d.get("Title") if src_d.get("Title") is not None else r_title.get("Index", 1)
    rng = src_d.get("Range") if isinstance(src_d.get("Range"), dict) else {}
    c_start = job_node.get("StartPoint")
    if c_start is None:
        c_start = job_node.get("ChapterStart") if job_node.get("ChapterStart") is not None else rng.get("Start", "")
    c_end = job_node.get("EndPoint")
    if c_end is None:
        c_end = job_node.get("ChapterEnd") if job_node.get("ChapterEnd") is not None else rng.get("End", "")
    return title, c_start, c_end


def extract_crop_and_dims_from_payload(payload: dict, job_node: dict):
    pic_n = job_node.get("Picture") if isinstance(job_node.get("Picture"), dict) else {}
    ui_s = payload.get("uiSettings") if isinstance(payload.get("uiSettings"), dict) else {}
    t_node = payload.get("Title") if isinstance(payload.get("Title"), dict) else {}
    s_info = payload.get("SourceTitleInfo") if isinstance(payload.get("SourceTitleInfo"), dict) else {}

    ct, cb, cl, cr = 0, 0, 0, 0
    has_crop = False

    cd = job_node.get("Cropping") or job_node.get("Crop") or pic_n.get("Crop")
    if isinstance(cd, dict):
        ct, cb, cl, cr = _to_int(cd.get("Top")), _to_int(cd.get("Bottom")), _to_int(cd.get("Left")), _to_int(cd.get("Right"))
        has_crop = True
    elif isinstance(cd, list) and len(cd) >= 4:
        ct, cb, cl, cr = _to_int(cd[0]), _to_int(cd[1]), _to_int(cd[2]), _to_int(cd[3])
        has_crop = True

    flist = (job_node.get("Filters") or {}).get("FilterList") if isinstance(job_node.get("Filters"), dict) else []
    fw, fh = 0, 0
    if isinstance(flist, list):
        for flt in flist:
            if isinstance(flt, dict) and isinstance(flt.get("Settings"), dict):
                fid = flt.get("ID")
                st = flt["Settings"]
                # Filtr CROP_SCALE ma ID 20/21; PAD ma ID 28/29 a nesmi byt zamenen za orez
                is_crop_flt = (fid in (20, 21)) or (fid is None and any(k in st for k in ("crop-top", "crop-bottom", "crop-left", "crop-right")))
                if is_crop_flt:
                    if not has_crop and any(k in st for k in ("crop-top", "crop-bottom", "crop-left", "crop-right", "top", "bottom", "left", "right")):
                        ct = _to_int(st.get("crop-top") if "crop-top" in st else st.get("top"))
                        cb = _to_int(st.get("crop-bottom") if "crop-bottom" in st else st.get("bottom"))
                        cl = _to_int(st.get("crop-left") if "crop-left" in st else st.get("left"))
                        cr = _to_int(st.get("crop-right") if "crop-right" in st else st.get("right"))
                        has_crop = True
                    if st.get("width") or st.get("height"):
                        fw, fh = _to_int(st.get("width")), _to_int(st.get("height"))

    if not has_crop and ui_s and any(k in ui_s for k in ("PictureTopCrop", "PictureBottomCrop", "PictureLeftCrop", "PictureRightCrop")):
        ct, cb, cl, cr = _to_int(ui_s.get("PictureTopCrop")), _to_int(ui_s.get("PictureBottomCrop")), _to_int(ui_s.get("PictureLeftCrop")), _to_int(ui_s.get("PictureRightCrop"))
        has_crop = True

    if not has_crop and isinstance(t_node.get("Crop"), list) and len(t_node["Crop"]) >= 4:
        tc = t_node["Crop"]
        ct, cb, cl, cr = _to_int(tc[0]), _to_int(tc[1]), _to_int(tc[2]), _to_int(tc[3])
        has_crop = True

    if not has_crop and isinstance(s_info.get("AutoCropDimensions"), dict):
        ac = s_info["AutoCropDimensions"]
        ct, cb, cl, cr = _to_int(ac.get("Top")), _to_int(ac.get("Bottom")), _to_int(ac.get("Left")), _to_int(ac.get("Right"))

    tw = _to_int(job_node.get("Width") or pic_n.get("Width") or fw or ui_s.get("scale_width") or ui_s.get("PictureWidth"))
    th = _to_int(job_node.get("Height") or pic_n.get("Height") or fh or ui_s.get("scale_height") or ui_s.get("PictureHeight"))
    mw = _to_int(job_node.get("MaxWidth") or pic_n.get("MaxWidth") or ui_s.get("PictureWidth") or ui_s.get("maxWidth"))
    mh = _to_int(job_node.get("MaxHeight") or pic_n.get("MaxHeight") or ui_s.get("PictureHeight") or ui_s.get("maxHeight"))

    return ct, cb, cl, cr, tw, th, mw, mh


def translate_path_for_manager(path_str):
    full_p, _, found = resolve_input_path_smart(path_str, get_manager_inputs_dir())
    return full_p if found else ""      # nenalezený zdroj se nesondáží (a nikdy ne mimo Inputs)


def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "N/A"
    h, m = int(seconds // 3600), int((seconds % 3600) // 60)
    return f"{h:02d}h {m:02d}m" if h > 0 else f"{m:02d}m"


def classify_pixels(width, height):
    w, h = _to_int(width), _to_int(height)
    if w <= 0 and h <= 0:
        return None
    if w >= 2500 or h >= 1400:
        return "4K"
    if w >= 1600 or h >= 900:
        return "1080p"
    if w >= 1000 or h >= 650:
        return "720p"
    return "576p" if h >= 520 else "480p"


def ffprobe_worker_loop():
    global media_cache_version
    dirty = 0
    while True:
        ck = None
        try:
            item = ffprobe_queue.get()
            ck, raw_p, t_num = item[0], item[1], item[2]
            mpls_hint = item[3] if len(item) > 3 else ""
            w, h, dur_s, det_mpls, src_bytes = 0, 0, 0.0, str(mpls_hint or ""), 0
            is_disc_p = False

            trans_p = (
                raw_p
                if (raw_p and os.path.isabs(raw_p) and os.path.exists(raw_p) and is_path_within(get_manager_inputs_dir(), raw_p))
                else translate_path_for_manager(raw_p)
            )
            if trans_p and os.path.exists(trans_p):
                if trans_p.lower().endswith('.iso') or os.path.isdir(trans_p):
                    is_disc_p = True
                    hb_bin = resolve_manager_binary_path("linux", "hbcli") or shutil.which("HandBrakeCLI")
                    if hb_bin:
                        try:
                            s_res = subprocess.run([hb_bin, "-i", trans_p, "-t", str(t_num or 1), "--previews", "1:0", "--scan"], capture_output=True, text=True, errors="ignore", timeout=60)
                            s_out = (s_res.stdout or "") + "\n" + (s_res.stderr or "")
                            m_sz = re.search(r"\+\s*size:\s*(\d+)x(\d+)", s_out)
                            if m_sz:
                                w, h = int(m_sz.group(1)), int(m_sz.group(2))
                            m_dur = re.search(r"\+\s*duration:\s*(\d+):(\d+):(\d+)", s_out)
                            if m_dur:
                                dur_s = int(m_dur.group(1)) * 3600 + int(m_dur.group(2)) * 60 + float(m_dur.group(3))
                            m_pl = re.search(r"\+\s*playlist:\s*(\d+\.MPLS)", s_out, re.IGNORECASE)
                            if m_pl:
                                det_mpls = m_pl.group(1).upper()
                        except Exception:
                            pass
                    try:
                        min_sz = 100 * 1024 * 1024 if dur_s >= 1200 else 1024 * 1024
                        pl_b = inspect_bluray_playlist_bytes(trans_p, det_mpls, min_valid_bytes=min_sz)
                        if pl_b >= min_sz:
                            src_bytes = int(pl_b)
                    except Exception:
                        pass

                if w == 0 and h == 0:
                    t_file = find_probeable_file_in_dir(trans_p) if os.path.isdir(trans_p) else trans_p
                    if t_file and os.path.isfile(t_file) and not t_file.lower().endswith('.iso'):
                        fp_bin = resolve_manager_binary_path("linux", "ffprobe") or shutil.which("ffprobe") or "ffprobe"
                        try:
                            cmd = [fp_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", t_file]
                            d = json.loads(subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=12))
                            st = d.get("streams") or []
                            if st:
                                w, h = int(st[0].get("width") or 0), int(st[0].get("height") or 0)
                            fmt = d.get("format") or {}
                            if fmt.get("duration"):
                                dur_s = float(fmt["duration"])
                        except Exception:
                            pass

            if w > 0 or h > 0 or src_bytes > 0:
                c_obj = {"w": w, "h": h, "dur": dur_s}
                if det_mpls:
                    c_obj["mpls"] = det_mpls
                if src_bytes > 0:
                    c_obj["src_bytes"] = src_bytes
                if is_disc_p:
                    c_obj["is_disc"] = True
                with media_cache_lock:
                    _lru_set(media_cache, ck, c_obj, MEDIA_CACHE_MAX)
                    media_cache_version += 1
                dirty += 1
                if dirty >= 5 or ffprobe_queue.empty():
                    save_media_cache()
                    dirty = 0
            else:
                with media_cache_lock:
                    _lru_set(media_cache, ck, {"w": 0, "h": 0, "dur": 0.0}, MEDIA_CACHE_MAX)
        except Exception as e:
            logger.error(f"Error in ffprobe_worker_loop: {e}")
        finally:
            if ck is not None:
                with media_cache_lock:
                    ffprobe_queued_paths.pop(ck, None)
            try:
                ffprobe_queue.task_done()
            except Exception:
                pass


def get_job_media_info_ext(job):
    if is_system_build_job(job):
        return T("job_system"), "CLUSTER", T("job_compilation"), 0.0, "SYSTEM_BUILD"

    if job.id is not None:
        with media_cache_lock:
            ce = job_info_cache.get(job.id)
            cur_ver = media_cache_version
        if ce and ce[0] == cur_ver and ce[1][0] not in ("Neznámé", "Unknown") and ce[1][3] > 0:
            return ce[1]

    try:
        pl, j_node = unwrap_job_payload_nodes(job.hb_json_payload)
        if not pl:
            return T("job_unknown"), T("job_unknown"), T("job_unavailable"), 0.0, T("job_unknown_preset")

        s_info = pl.get("SourceTitleInfo") or j_node.get("SourceTitleInfo") or pl.get("Title") or {}
        st_node = pl.get("Statistics") if isinstance(pl.get("Statistics"), dict) else {}
        ui_s = pl.get("uiSettings") if isinstance(pl.get("uiSettings"), dict) else {}

        t_num, _, _ = extract_title_and_range_from_job_node(j_node, pl)

        preset_n = T("job_unknown_preset")
        if pl.get("SelectedPresetKey"):
            preset_n = str(pl.get("SelectedPresetKey"))
        elif ui_s:
            pf, ps = str(ui_s.get("PresetFullName") or ""), str(ui_s.get("PresetName") or "")
            if pf and "/" in pf:
                preset_n = pf.rsplit("/", 1)[-1].strip() or ps or T("job_custom_preset")
            elif ps:
                preset_n = (ps + ")") if ("(" in ps and ")" not in ps) else ps
        elif j_node.get("PresetName"):
            preset_n = str(j_node.get("PresetName"))

        ct, cb, cl, cr, tw, th, mw, mh = extract_crop_and_dims_from_payload(pl, j_node)
        disp_w = _to_int(j_node.get("DisplayWidth"))
        par_x = _to_int(j_node.get("PixelAspectX") or (j_node.get("PAR") or {}).get("Num"))
        par_y = _to_int(j_node.get("PixelAspectY") or (j_node.get("PAR") or {}).get("Den"))

        in_w, in_h = 0, 0
        r_obj = s_info.get("Resolution") if isinstance(s_info, dict) else {}
        g_obj = s_info.get("Geometry") if isinstance(s_info, dict) else {}
        if isinstance(r_obj, dict) and (r_obj.get("Width") or r_obj.get("Height")):
            in_w, in_h = _to_int(r_obj.get("Width")), _to_int(r_obj.get("Height"))
        elif isinstance(g_obj, dict) and (g_obj.get("Width") or g_obj.get("Height")):
            in_w, in_h = _to_int(g_obj.get("Width")), _to_int(g_obj.get("Height"))
        elif isinstance(s_info, dict) and (s_info.get("Width") or s_info.get("Height")):
            in_w, in_h = _to_int(s_info.get("Width")), _to_int(s_info.get("Height"))
        else:
            src_o = j_node.get("Source")
            if isinstance(src_o, dict):
                sr = src_o.get("Resolution") if isinstance(src_o.get("Resolution"), dict) else {}
                in_w, in_h = _to_int(sr.get("Width") or src_o.get("Width")), _to_int(sr.get("Height") or src_o.get("Height"))
            if in_w == 0 and in_h == 0 and ui_s:
                in_w, in_h = _to_int(ui_s.get("source_width") or ui_s.get("SourceWidth")), _to_int(ui_s.get("source_height") or ui_s.get("SourceHeight"))

        dur_s = 0.0
        d_obj = s_info.get("Duration") if isinstance(s_info, dict) else None
        if isinstance(d_obj, dict) and (d_obj.get("Hours") is not None or d_obj.get("Minutes") is not None):
            dur_s = _to_int(d_obj.get("Hours")) * 3600 + _to_int(d_obj.get("Minutes")) * 60 + _to_int(d_obj.get("Seconds"))
        else:
            d_str = (d_obj if isinstance(d_obj, str) else "") or st_node.get("SourceLength") or st_node.get("ContentLength") or ""
            if d_str and isinstance(d_str, str) and d_str != "Not Available":
                pts = d_str.split(':')
                if len(pts) >= 3:
                    try:
                        dur_s = int(pts[0]) * 3600 + int(pts[1]) * 60 + float(pts[2])
                    except Exception:
                        pass

        src_p = extract_orig_source_from_payload(pl)
        t_ck = f"{src_p}::T{t_num}" if src_p else ""
        queue_probe = False
        with media_cache_lock:
            c_item = media_cache.get(t_ck) or media_cache.get(src_p)
            if t_ck and t_ck not in media_cache and t_ck not in ffprobe_queued_paths and (in_w == 0 or dur_s <= 0):
                _lru_set(ffprobe_queued_paths, t_ck, True, 5000)
                queue_probe = True
        if queue_probe:
            ffprobe_queue.put((t_ck, src_p, t_num))

        if in_w == 0 and in_h == 0:
            if c_item and (c_item.get("w") or c_item.get("h")):
                in_w, in_h = c_item.get("w", 0), c_item.get("h", 0)
                if dur_s <= 0:
                    dur_s = c_item.get("dur", 0.0)
            else:
                name_hint = f"{src_p or ''} {job.original_filename or ''}"
                is_down_4k = (
                    bool(re.search(r'(?:2160p|\buhd\b|\b4k\b)', name_hint, re.IGNORECASE))
                    or (th == 1080 and (ct + cb) > 0 and mw == 0)
                    or (tw == 1920 and disp_w > 1930)
                    or (tw == 1920 and par_x > par_y and par_y > 0)
                    or (th + ct + cb) > 1150
                    or (tw + cl + cr) > 2000
                )
                if is_down_4k:
                    in_w, in_h = 3840, 2160
                elif tw > 0 or th > 0:
                    in_w, in_h = tw + cl + cr, th + ct + cb
                elif mw > 0 or mh > 0:
                    in_w, in_h = mw, mh

        if dur_s <= 0 and c_item:
            dur_s = c_item.get("dur", 0.0)

        eff_w = tw or mw or in_w
        if th > 0:
            sc = (eff_w / in_w) if (in_w > 0 and eff_w > 0 and eff_w < in_w) else 1.0
            eff_h = int(round(th + (ct + cb) * sc))
        elif mh > 0:
            eff_h = min(in_h, mh) if in_h > 0 else mh
        else:
            eff_h = in_h or 1080

        if mw > 0 and eff_w > mw:
            eff_w = mw
        if mh > 0 and eff_h > mh:
            eff_h = mh

        in_lbl = classify_pixels(in_w, in_h) or T("job_unknown")
        out_lbl = classify_pixels(eff_w, eff_h) or "1080p"

        ranks = {"480p": 1, "576p": 2, "720p": 3, "1080p": 4, "4K": 5}
        if in_lbl in ranks and out_lbl in ranks and ranks[in_lbl] < ranks[out_lbl]:
            out_lbl = in_lbl

        res = (in_lbl, out_lbl, format_duration(dur_s), float(dur_s), preset_n)
        if job.id is not None and in_lbl not in ("Neznámé", "Unknown") and dur_s > 0:
            with media_cache_lock:
                _lru_set(job_info_cache, job.id, (media_cache_version, res), JOB_INFO_CACHE_MAX)
        return res
    except Exception:
        return T("job_unknown"), "1080p", T("job_unavailable"), 0.0, T("job_unknown_preset")


def get_job_media_info(job):
    in_l, out_l, dur_f, _, _ = get_job_media_info_ext(job)
    return in_l, out_l, dur_f


def get_job_height(job):
    if is_system_build_job(job):
        return 720
    explicit_h = _to_int(getattr(job, "height", 0))
    if explicit_h > 0:
        return explicit_h
    res_attr = str(getattr(job, "res_out", "") or "").strip()
    res_low = res_attr.lower()
    if res_low == "4k":
        return 2160
    if res_low == "1080p":
        return 1080
    if res_low == "720p":
        return 720
    if res_low in ("576p", "480p"):
        return 480
    # Fallback jen když res_out ještě nebylo vyplněno v DB
    out_h = get_job_media_info(job)[1]
    if str(out_h or "").strip().lower() == "4k":
        return 2160
    if out_h == "1080p":
        return 1080
    if out_h == "720p":
        return 720
    return 480


def get_job_weight(job) -> int:
    """3-úrovňová váha úlohy ve slotech: 4K (height >= 1400 / 4K příznak) = 4, 1080p (900..1399) = 2, SD/720p (< 900) = 1."""
    if is_system_build_job(job):
        return 1
    res_attr = str(getattr(job, "res_out", "") or "").strip().lower()
    if res_attr == "4k":
        return 4
    if res_attr == "1080p":
        return 2
    if res_attr in ("720p", "576p", "480p"):
        return 1
    h = get_job_height(job)
    if h >= 1400:
        return 4
    if h >= 900:
        return 2
    return 1


def worker_allows_4k(pol_or_cap) -> bool:
    if isinstance(pol_or_cap, str):
        return pol_or_cap.strip().lower() not in ("1080p", "sd", "720p")
    if pol_or_cap is None:
        return True
    cap = str(getattr(pol_or_cap, "capability", "all") or "all").strip().lower()
    if cap in ("1080p", "sd", "720p"):
        return False
    a4 = getattr(pol_or_cap, "allow_4k", None)
    return True if a4 is None else bool(a4)


def default_slots_for_worker(pol_or_cap) -> int:
    """Výchozí kapacita slotů podle nejvyšší povolené kvality: 4K = 4, 1080p = 2, SD/720p = 1."""
    if isinstance(pol_or_cap, str):
        cap = pol_or_cap.strip().lower()
        return 1 if cap in ("sd", "720p") else (2 if cap == "1080p" else 4)
    if pol_or_cap is None:
        return 4
    cap = str(getattr(pol_or_cap, "capability", "all") or "all").strip().lower()
    if cap in ("sd", "720p"):
        return 1
    if cap == "1080p":
        return 2
    a4 = getattr(pol_or_cap, "allow_4k", None)
    if a4 is False:
        return 2
    return 4
def sort_jobs_logic(jobs):
    def _key(j):
        if is_system_build_job(j) and j.status in (["WAITING"] + ACTIVE_JOB_STATUSES):
            return (-1, 0, j.id)
        cat = 0 if j.status in COMPUTE_ACTIVE_STATUSES else (1 if j.status == "WAITING_DISK" else (2 if j.status == "WAITING" else (3 if j.status == "PAUSED" else 4)))
        return (cat, j.priority, j.created_at, j.id)
    return sorted(jobs, key=_key)


def normalize_movie_name(name_str):
    if not name_str:
        return ""
    base, _ = os.path.splitext(extract_basename(name_str).lower())
    return re.sub(r'[\.\_\-\s]+', ' ', base).strip()


def extract_structured_encoding_params(job):
    if is_system_build_job(job):
        return "system_build", job.encoder_summary or "Official sources + staticx", "staticx", "official", None, 0, 0

    pl, job_node = unwrap_job_payload_nodes(job.hb_json_payload)
    if not pl:
        return "unknown", "Unknown parameters", "default", "", None, 0, 0

    vid_d = job_node.get("Video") if isinstance(job_node.get("Video"), dict) else {}
    ui_s = pl.get("uiSettings") if isinstance(pl.get("uiSettings"), dict) else {}

    v_enc = job_node.get("VideoEncoder") or vid_d.get("Encoder") or ui_s.get("VideoEncoder")
    if isinstance(v_enc, dict):
        v_enc = v_enc.get("ShortName") or v_enc.get("Name")
    v_enc = str(v_enc or "default")

    is_cq_mode = (job_node.get("VideoEncodeRateType", 2) == 2) and not vid_d.get("Bitrate")
    rf_v = None
    v_abr = 0
    if is_cq_mode:
        rf_v = job_node.get("Quality")
        if rf_v is None:
            rf_v = vid_d.get("Quality") if vid_d.get("Quality") is not None else ui_s.get("VideoQualitySlider")
    else:
        try:
            v_abr = int(float(job_node.get("VideoBitrate") or vid_d.get("Bitrate") or 0))
        except Exception:
            v_abr = 0
    try:
        rf_fl = float(rf_v) if rf_v is not None else None
    except Exception:
        rf_fl = None

    vp_obj = job_node.get("VideoPreset") or vid_d.get("Preset") or ui_s.get("VideoPreset")
    v_preset = vp_obj.get("ShortName") if isinstance(vp_obj, dict) else str(vp_obj or "")
    v_extra = str(job_node.get("ExtraAdvancedArguments") or job_node.get("AdvancedEncoderOptions") or vid_d.get("Options") or ui_s.get("VideoOptionExtra") or "")

    ct, cb, cl, cr, tw, th, mw, mh = extract_crop_and_dims_from_payload(pl, job_node)
    w, h = tw or mw or 0, th or mh or 0
    crop = {"Top": ct, "Bottom": cb, "Left": cl, "Right": cr}

    a_tracks = job_node.get("AudioTracks")
    is_libhb_a = not isinstance(a_tracks, list)
    if is_libhb_a:
        a_tracks = (job_node.get("Audio") or {}).get("AudioList") if isinstance(job_node.get("Audio"), dict) else []
    a_sig = []
    for idx_a, t in enumerate(a_tracks or []):
        if isinstance(t, dict):
            sc = t.get("ScannedTrack") or t.get("SourceTrack") or {}
            tr = t.get("Track")
            t_num = (int(tr) + 1) if (tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t) else (sc.get("TrackNumber") or tr or (idx_a + 1))
            enc = t.get("Encoder")
            enc_s = str(enc.get("ShortName") if isinstance(enc, dict) else (enc or ""))
            br_v = 0 if ("copy" in enc_s.lower() or "pass" in enc_s.lower()) else int(t.get("Bitrate") or 0)
            a_sig.append((int(t_num), enc_s, br_v))

    s_tracks = job_node.get("SubtitleTracks")
    is_libhb_s = not isinstance(s_tracks, list)
    if is_libhb_s:
        s_tracks = (job_node.get("Subtitle") or {}).get("SubtitleList") if isinstance(job_node.get("Subtitle"), dict) else []
    s_sig = []
    for idx_s, t in enumerate(s_tracks or []):
        if isinstance(t, dict):
            st = t.get("SourceTrack") or t.get("ScannedTrack") or {}
            tr = t.get("Track")
            t_num = (int(tr) + 1) if (tr is not None and is_libhb_s and "SourceTrack" not in t and "ScannedTrack" not in t) else (st.get("TrackNumber") or tr or (idx_s + 1))
            s_sig.append((int(t_num), bool(t.get("Forced")), bool(t.get("Burned") or t.get("Burn"))))

    t_num, ch_s, ch_e = extract_title_and_range_from_job_node(job_node, pl)
    sig_d = {
        "title": int(t_num or 1), "ch_start": ch_s, "ch_end": ch_e,
        "v_enc": v_enc, "rf": rf_fl, "v_preset": v_preset, "v_extra": v_extra,
        "w": w, "h": h, "crop": crop, "audio": a_sig, "subs": s_sig
    }
    if v_abr > 0:
        sig_d["v_abr"] = v_abr
    fp_h = hashlib.md5(json.dumps(sig_d, sort_keys=True).encode('utf-8')).hexdigest()

    rf_s = f"RF:{int(rf_fl) if rf_fl is not None and rf_fl.is_integer() else rf_fl}" if rf_fl is not None else (f"ABR:{v_abr}k" if v_abr > 0 else "ABR")
    sum_s = f"{v_enc} | {rf_s}" + (f" | {v_preset}" if v_preset else "") + f" | Audio:{len(a_sig)}x | Sub:{len(s_sig)}x"
    return fp_h, sum_s, v_enc, v_preset, rf_fl, len(a_sig), len(s_sig)


def get_job_encoding_fingerprint(job):
    fp_h, sum_s, _, _, rf_v, _, _ = extract_structured_encoding_params(job)
    return fp_h, sum_s, rf_v


def get_job_source_identity(job):
    pl, job_node = unwrap_job_payload_nodes(job.hb_json_payload)
    title, cs, ce = extract_title_and_range_from_job_node(job_node, pl)
    orig_s = extract_orig_source_from_payload(pl)
    _, rel_s, _ = resolve_input_path_smart(orig_s, get_manager_inputs_dir())
    rel_s = rel_s.lower().strip('/') or normalize_movie_name(job.original_filename)
    return (rel_s, str(title), str(cs), str(ce))


# ------------------------------------------------------------------------------
# Podezření na duplicitu: stejný zdrojový soubor (+ titul / kapitoly) a stejné parametry převodu
# (get_job_encoding_fingerprint – kodek, RF, preset, rozlišení, ořez, stopy) jako úloha, která už
# byla dokončena, nebo jako jiná úloha ve frontě. Jen upozornění v UI, nic se neblokuje.
# Klíč zdroje se počítá jen z textu cesty (bez přístupu na disk / SMB), výsledky se cachují.
# ------------------------------------------------------------------------------
_dup_key_cache = OrderedDict()     # job_id -> (ts, key)
_dup_cache_lock = threading.Lock()
_dup_hist_cache = {"sig": None, "index": {}}
DUP_KEY_TTL_S = 300
DUP_KEY_CACHE_MAX = 10000


def compute_job_duplicate_key_str(job) -> str:
    """Spočítá stabilní textový klíč duplicity (zdroj|titul|kapitoly|otisk) bez přístupu na disk."""
    try:
        if is_system_build_job(job):
            return ""
        pl, job_node = unwrap_job_payload_nodes(job.hb_json_payload)
        src = str(extract_orig_source_from_payload(pl) or job.source_path or "").replace("\\", "/").strip().rstrip("/").lower()
        if not src:
            return ""
        title, cs, ce = extract_title_and_range_from_job_node(job_node, pl)
        fp_h = get_job_encoding_fingerprint(job)[0]
        if not fp_h or fp_h == "unknown":
            return ""
        return f"{src}|{title}|{cs}|{ce}|{fp_h}"
    except Exception:
        return ""


def get_job_duplicate_key(job):
    """Textový klíč duplicity nebo None. Čte primárně perzistovaný sloupec `dup_key` (bez lazy loadu JSONu)."""
    if is_system_build_job(job):
        return None
    dk = getattr(job, "dup_key", None)
    if isinstance(dk, str):
        return dk if dk else None
    now = time.time()
    if job.id:
        with _dup_cache_lock:
            hit = _dup_key_cache.get(job.id)
        if hit and now - hit[0] < DUP_KEY_TTL_S:
            return hit[1]
    key = compute_job_duplicate_key_str(job) or None
    if job.id:
        with _dup_cache_lock:
            _lru_set(_dup_key_cache, job.id, (now, key), DUP_KEY_CACHE_MAX)
    return key


def get_completed_duplicate_index(db_session) -> dict:
    """klíč duplicity -> poslední dokončená úloha se stejným klíčem. Přepočítá se jen při změně historie a bez načítání JSONů."""
    try:
        cnt, mx_id, mx_ts = db_session.query(
            func.count(models.Job.id), func.max(models.Job.id), func.max(models.Job.completed_at)
        ).filter(models.Job.status == "COMPLETED").one()
    except Exception:
        with _dup_cache_lock:
            return _dup_hist_cache["index"]
    sig = (cnt, mx_id, str(mx_ts))
    with _dup_cache_lock:
        if _dup_hist_cache["sig"] == sig:
            return _dup_hist_cache["index"]
    idx = {}
    rows = (
        db_session.query(
            models.Job.id,
            models.Job.dup_key,
            models.Job.completed_at,
            models.Job.worker_name,
            models.Job.dest_path,
        )
        .filter(
            models.Job.status == "COMPLETED",
            models.Job.dup_key.isnot(None),
            models.Job.dup_key != "",
        )
        .all()
    )
    for jid, dk, comp_at, w_name, dest_p in rows:
        if not dk:
            continue
        prev = idx.get(dk)
        if prev and prev["id"] > jid:
            continue
        idx[dk] = {
            "id": jid,
            "completed_at": comp_at.isoformat() if comp_at else None,
            "worker": w_name or "",
            "dest": dest_p or "",
        }
    with _dup_cache_lock:
        _dup_hist_cache.update(sig=sig, index=idx)
    return idx


def annotate_queue_duplicates(db_session, jobs, serialized):
    """Doplní do serializovaných úloh fronty `dup_done` (dokončená shoda) a `dup_queue` (ID shod ve frontě)."""
    try:
        hist = get_completed_duplicate_index(db_session)
        keys, by_key = {}, {}
        for j in jobs:
            if j.status in ("ERROR", "CANCELLED"):
                continue
            k = get_job_duplicate_key(j)
            if k:
                keys[j.id] = k
                by_key.setdefault(k, []).append(j.id)
        for d in serialized:
            k = keys.get(d.get("id"))
            if not k:
                continue
            h = hist.get(k)
            if h and h["id"] != d.get("id"):
                d["dup_done"] = h
            others = [x for x in by_key.get(k, []) if x != d.get("id")]
            if others:
                d["dup_queue"] = sorted(others)
    except Exception as e:
        logger.debug(f"duplicate annotate failed: {e}")


def rename_job_destination(db_session, job, suffix_tag):
    b_dest, e_dest = os.path.splitext(job.dest_path)
    e_dest = e_dest or ".mkv"
    if suffix_tag not in b_dest:
        job.dest_path = f"{b_dest}_{suffix_tag}{e_dest}"

    pl = copy.deepcopy(job.hb_json_payload)
    _, jn = unwrap_job_payload_nodes(pl)
    if jn:
        dst = jn.get("Destination")
        if isinstance(dst, dict) and dst.get("File"):
            bf, ef = os.path.splitext(str(dst["File"]))
            if suffix_tag not in bf:
                dst["File"] = f"{bf}_{suffix_tag}{ef or '.mkv'}"
        elif isinstance(dst, str) and dst:
            bf, ef = os.path.splitext(dst)
            if suffix_tag not in bf:
                jn["Destination"] = f"{bf}_{suffix_tag}{ef or '.mkv'}"
    job.hb_json_payload = pl


def populate_job_static_metadata(job: models.Job, allow_disk_probe: bool = True):
    if is_system_build_job(job):
        job.res_in, job.res_out, job.preset_name = "SYSTÉM", "CLUSTER", "SYSTEM_BUILD"
        job.is_disc_source = False
        job.burned_subs = 0
        job.dup_key = ""
        return
    try:
        in_l, out_l, _, dur_s, preset_n = get_job_media_info_ext(job)
        _, enc_sum, v_enc, v_preset, rf_val, a_cnt, s_cnt = extract_structured_encoding_params(job)
        job.res_in = in_l
        job.res_out = out_l
        job.video_duration_sec = dur_s if dur_s > 0 else 0.0
        job.preset_name = "SYSTEM_BUILD (user)" if str(preset_n or "").strip() == "SYSTEM_BUILD" else preset_n
        job.encoder_summary = enc_sum
        job.video_encoder = v_enc
        job.video_preset = v_preset
        job.video_rf = rf_val
        job.audio_count = a_cnt
        job.subs_count = s_cnt
        job.burned_subs = count_burned_subtitles(job.hb_json_payload)
        job.dup_key = compute_job_duplicate_key_str(job)

        if job.v_bitrate_in_kbps == 2500:
            job.v_bitrate_in_kbps = 0

        pl, j_node = unwrap_job_payload_nodes(job.hb_json_payload)
        is_disc = is_job_disc_or_iso(job, allow_disk_check=allow_disk_probe)
        job.is_disc_source = bool(is_disc)

        min_valid_sz = 100 * 1024 * 1024 if dur_s >= 1200 else 1024 * 1024
        if not job.src_size_bytes or job.src_size_bytes < min_valid_sz:
            if pl:
                t_num, _, _ = extract_title_and_range_from_job_node(j_node, pl)
                t_meta = pl.get("Title") if isinstance(pl.get("Title"), dict) else {}
                s_info = pl.get("SourceTitleInfo") if isinstance(pl.get("SourceTitleInfo"), dict) else {}
                pl_raw = t_meta.get("Playlist") if t_meta.get("Playlist") is not None else s_info.get("Playlist")
                orig_s = extract_orig_source_from_payload(pl)
                t_ck = f"{orig_s}::T{t_num}" if orig_s else ""
                with media_cache_lock:
                    c_item = media_cache.get(t_ck) or media_cache.get(orig_s) or {}
                if not pl_raw and c_item.get("mpls"):
                    pl_raw = c_item.get("mpls")
                if c_item.get("is_disc"):
                    is_disc = True
                    job.is_disc_source = True
                if int(c_item.get("src_bytes") or 0) >= min_valid_sz:
                    job.src_size_bytes = int(c_item["src_bytes"])

                if (not job.src_size_bytes or job.src_size_bytes < min_valid_sz) and allow_disk_probe:
                    full_p, _, found_p = resolve_input_path_smart(orig_s, get_manager_inputs_dir())
                    if found_p and (pl_raw is not None or full_p.lower().endswith(".iso") or os.path.isdir(full_p)):
                        if full_p.lower().endswith(".iso") or os.path.isdir(full_p):
                            is_disc = True
                            job.is_disc_source = True
                        pl_b = inspect_bluray_playlist_bytes(full_p, str(pl_raw or ""), min_valid_bytes=min_valid_sz)
                        if pl_b >= min_valid_sz:
                            job.src_size_bytes = pl_b
                elif (not job.src_size_bytes or job.src_size_bytes < min_valid_sz) and not allow_disk_probe and is_disc and t_ck:
                    # Odložit pomalé čtení 64MB ISO / 7z na vlákno ffprobe_worker_loop
                    queue_disc_probe = False
                    with media_cache_lock:
                        if t_ck not in ffprobe_queued_paths and not int((media_cache.get(t_ck) or {}).get("src_bytes") or 0):
                            _lru_set(ffprobe_queued_paths, t_ck, True, 5000)
                            queue_disc_probe = True
                    if queue_disc_probe:
                        ffprobe_queue.put((t_ck, orig_s, t_num, str(pl_raw or "")))

                if not job.src_size_bytes or job.src_size_bytes < min_valid_sz:
                    st_n = pl.get("Statistics")
                    if isinstance(st_n, dict) and st_n.get("SourceFileSizeInBytes"):
                        sv = _to_int(st_n.get("SourceFileSizeInBytes"))
                        if sv >= min_valid_sz:
                            job.src_size_bytes = sv

                if job.src_size_bytes and job.src_size_bytes < min_valid_sz:
                    job.src_size_bytes = 0

        if (job.src_size_bytes or 0) >= min_valid_sz and dur_s > 60.0 and is_disc:
            est_v, est_a = estimate_disc_bitrates_from_size_and_duration(
                int(job.src_size_bytes), float(dur_s), pl, int(job.a_bitrate_in_kbps or 0)
            )
            cur_v = int(job.v_bitrate_in_kbps or 0)
            if est_v > 0 and (cur_v <= 0 or (est_v >= 8000 and cur_v < est_v * 0.55) or (cur_v > est_v * 1.65)):
                job.v_bitrate_in_kbps = est_v
            if est_a > 0 and (job.a_bitrate_in_kbps or 0) <= 0:
                job.a_bitrate_in_kbps = est_a
    except Exception:
        pass


def record_job_to_encoding_stats(db_session, job: models.Job):
    if is_system_build_job(job) or (job.v_bitrate_in_kbps or 0) <= 0 or job.v_bitrate_in_kbps == 2500 or (job.src_size_bytes or 0) <= 0:
        return
    try:
        raw_w = str(getattr(job, "worker_name", "") or "").strip()
        w_clean = re.sub(r"\s*\(v[\d\.]+\)$", "", raw_w).strip() if raw_w else ""
        if not w_clean or w_clean.lower() == "unknown":
            w_clean = "Unknown"
        # Nepřepisovat již přiřazeného reálného workera na prázdno / Unknown
        elif raw_w != w_clean:
            job.worker_name = w_clean
        res_in, res_out = job.res_in or "1080p", job.res_out or "1080p"
        v_enc, v_preset, v_rf = job.video_encoder or "x265_10bit", job.video_preset or "slow", job.video_rf

        stat_row = db_session.query(models.EncodingStats).filter(
            models.EncodingStats.worker_name == w_clean,
            models.EncodingStats.res_in == res_in,
            models.EncodingStats.res_out == res_out,
            models.EncodingStats.video_encoder == v_enc,
            models.EncodingStats.video_preset == v_preset,
            models.EncodingStats.video_rf == v_rf
        ).first()

        if not stat_row:
            stat_row = models.EncodingStats(
                worker_name=w_clean, res_in=res_in, res_out=res_out,
                video_encoder=v_enc, video_preset=v_preset, video_rf=v_rf,
                jobs_count=0, total_src_bytes=0, total_out_bytes=0,
                total_encoding_sec=0, total_video_duration_sec=0.0,
                avg_fps=0.0, avg_v_bitrate_in_kbps=0, avg_v_bitrate_out_kbps=0
            )
            db_session.add(stat_row)

        prev_cnt = stat_row.jobs_count or 0
        new_cnt = prev_cnt + 1
        stat_row.jobs_count = new_cnt
        stat_row.total_src_bytes = (stat_row.total_src_bytes or 0) + int(job.src_size_bytes or 0)
        stat_row.total_out_bytes = (stat_row.total_out_bytes or 0) + int(job.out_size_bytes or 0)
        stat_row.total_encoding_sec = (stat_row.total_encoding_sec or 0) + int(job.duration_sec or 0)
        stat_row.total_video_duration_sec = float(stat_row.total_video_duration_sec or 0.0) + float(job.video_duration_sec or 0.0)
        if stat_row.total_encoding_sec > 0 and stat_row.total_video_duration_sec > 0:
            stat_row.avg_fps = round((stat_row.total_video_duration_sec * 24.0) / stat_row.total_encoding_sec, 2)

        if (job.v_bitrate_in_kbps or 0) > 0:
            old_in = stat_row.avg_v_bitrate_in_kbps or job.v_bitrate_in_kbps
            stat_row.avg_v_bitrate_in_kbps = int(round((old_in * prev_cnt + job.v_bitrate_in_kbps) / new_cnt))
        if (job.v_bitrate_out_kbps or 0) > 0:
            old_out = stat_row.avg_v_bitrate_out_kbps or job.v_bitrate_out_kbps
            stat_row.avg_v_bitrate_out_kbps = int(round((old_out * prev_cnt + job.v_bitrate_out_kbps) / new_cnt))

        stat_row.updated_at = datetime.datetime.utcnow()
        invalidate_analytics_cache()
    except Exception as e:
        logger.error(f"Error recording EncodingStats: {e}")


def get_cached_analytics(db_session):
    with analytics_lock:
        if not analytics_cache["dirty"] and analytics_cache["data"] is not None:
            return analytics_cache["data"]

    t_cnt, t_src, t_out, t_dur = 0, 0, 0, 0
    pw_stats, prof_list = {}, []

    for sr in db_session.query(models.EncodingStats).all():
        c, sb, ob, ds = sr.jobs_count or 0, sr.total_src_bytes or 0, sr.total_out_bytes or 0, sr.total_encoding_sec or 0
        t_cnt += c
        t_src += sb
        t_out += ob
        t_dur += ds
        wc_raw = re.sub(r"\s*\(v[\d\.]+\)$", "", str(sr.worker_name or "")).strip()
        wc = "Unknown" if (not wc_raw or wc_raw.lower() in ("unknown", "neznámý")) else wc_raw
        pw = pw_stats.setdefault(wc, {"count": 0, "src_bytes": 0, "out_bytes": 0})
        pw["count"] += c
        pw["src_bytes"] += sb
        pw["out_bytes"] += ob
        prof_list.append({
            "worker_name": wc, "res_in": sr.res_in, "res_out": sr.res_out,
            "video_encoder": sr.video_encoder, "video_preset": sr.video_preset,
            "video_rf": sr.video_rf, "jobs_count": c, "avg_fps": sr.avg_fps,
            "avg_v_bitrate_in_kbps": sr.avg_v_bitrate_in_kbps or 0,
            "avg_v_bitrate_out_kbps": sr.avg_v_bitrate_out_kbps or 0,
            "total_src_bytes": sb, "total_out_bytes": ob, "total_encoding_sec": ds,
        })

    sw_list = [
        {
            "worker_name": wn,
            "count": int(vd.get("count") or 0),
            "src_bytes": int(vd.get("src_bytes") or 0),
            "out_bytes": int(vd.get("out_bytes") or 0),
        }
        for wn, vd in pw_stats.items()
    ]
    sw_list.sort(key=lambda x: (0 if x["worker_name"] == "Unknown" else 1, -x["count"], x["worker_name"].lower()))

    h_cnt = db_session.query(models.Job.id).filter(models.Job.status == "COMPLETED").count()
    res = {
        "completed_count": max(t_cnt, h_cnt),
        "history_rows_count": h_cnt,
        "total_src_bytes": t_src,
        "total_out_bytes": t_out,
        "total_saved_bytes": max(0, t_src - t_out),
        "total_encoding_sec": t_dur,
        "per_worker": pw_stats,
        "stats_workers": sw_list,
        "profiles": prof_list,
    }
    with analytics_lock:
        analytics_cache["data"] = res
        analytics_cache["dirty"] = False
    return res


def delete_worker_stats(db_session, worker_name: str) -> dict:
    """Smaže statistiky (EncodingStats) pro vybraný worker (vč. Unknown = NULL / '' / Unknown) bez dopadu na ostatní."""
    raw = re.sub(r"\s*\(v[\d\.]+\)$", "", str(worker_name or "")).strip()
    is_unknown = (raw == "" or raw.lower() in ("unknown", "neznámý"))
    target_lbl = "Unknown" if is_unknown else raw
    deleted_rows = 0
    deleted_jobs = 0
    for sr in db_session.query(models.EncodingStats).all():
        sw = re.sub(r"\s*\(v[\d\.]+\)$", "", str(sr.worker_name or "")).strip()
        if is_unknown:
            matched = (sw == "" or sw.lower() in ("unknown", "neznámý"))
        else:
            matched = (sw == raw or str(sr.worker_name or "").strip() == str(worker_name or "").strip())
        if matched:
            deleted_jobs += int(sr.jobs_count or 0)
            db_session.delete(sr)
            deleted_rows += 1
    AppState.analytics_seeded = True
    db_session.commit()
    save_manager_config()
    invalidate_analytics_cache()
    return {"deleted_rows": deleted_rows, "deleted_jobs": deleted_jobs, "worker_name": target_lbl}


def initialize_structured_metadata():
    prev_imm = set_tx_immediate(True)
    db = SessionLocal()
    try:
        with media_cache_lock:
            job_info_cache.clear()
        all_jobs = db.query(models.Job).all()
        for j in all_jobs:
            if j.v_bitrate_in_kbps == 2500:
                j.v_bitrate_in_kbps = 0
            if (j.video_duration_sec or 0) >= 1200 and (j.src_size_bytes or 0) < 100 * 1024 * 1024:
                j.src_size_bytes = 0
            needs_backfill = (
                getattr(j, "dup_key", None) is None
                or getattr(j, "burned_subs", None) is None
                or int(getattr(j, "burned_subs", -1) if getattr(j, "burned_subs", None) is not None else -1) < 0
                or getattr(j, "is_disc_source", None) is None
            )
            if (
                j.status != "COMPLETED"
                or needs_backfill
                or not j.res_in
                or j.res_in in ("Neznámé", "Unknown")
                or not j.preset_name
                or not j.video_encoder
                or (j.src_size_bytes or 0) == 0
                or (j.v_bitrate_in_kbps or 0) == 0
            ):
                populate_job_static_metadata(j, allow_disk_probe=(j.status != "COMPLETED"))
            if j.worker_name and "(" in j.worker_name:
                m_v = re.search(r"\(v([\d\.]+)\)", j.worker_name)
                if m_v and not j.worker_version:
                    j.worker_version = m_v.group(1)
                j.worker_name = re.sub(r"\s*\(v[\d\.]+\)$", "", j.worker_name).strip()

        if db.query(models.EncodingStats).count() == 0 and not getattr(AppState, "analytics_seeded", False):
            for cj in [j for j in all_jobs if j.status == "COMPLETED" and not is_system_build_job(j)]:
                record_job_to_encoding_stats(db, cj)
            AppState.analytics_seeded = True
        elif db.query(models.EncodingStats).count() > 0:
            AppState.analytics_seeded = True

        renumber_waiting_queue(db)
        restore_worker_policies_from_backup(db)
        enforce_premux_local_disk_guard(db)
        db.commit()
        save_manager_config(db)
        invalidate_analytics_cache()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error initializing metadata: {e}")
    finally:
        db.close()
        set_tx_immediate(prev_imm)
        enforce_open_permissions()


def import_queue_payloads_to_db(jobs_list: list):
    prepared_items = []
    for item in (jobs_list or []):
        if not isinstance(item, dict):
            continue
        # Přeskočit zarážky (Breakpoint) z exportované fronty Windows HandBrake GUI (TaskType==1 nebo Task: null)
        if item.get("TaskType") == 1 or ("Task" in item and not isinstance(item.get("Task"), dict)):
            continue
        # Bezpečnost: importovaná fronta nesmí obsahovat interní SystemTask (BUILD_BINARIES)
        _, j_node = unwrap_job_payload_nodes(item)
        if "SystemTask" in item or (isinstance(j_node, dict) and "SystemTask" in j_node):
            append_manager_log("[SECURITY] Odmítnuta položka fronty obsahující zakázaný klíč SystemTask.")
            continue
        orig_s = extract_orig_source_from_payload(item)
        raw_b = extract_basename(orig_s)
        j_hash = hashlib.md5(json.dumps(item, sort_keys=True).encode('utf-8')).hexdigest()

        dst_v = j_node.get("Destination", {})
        orig_d = dst_v.get("File", "") if isinstance(dst_v, dict) else str(dst_v)
        if not orig_d and isinstance(item.get("uiSettings"), dict):
            orig_d = str(item["uiSettings"].get("destination") or "")

        dest_b = extract_basename(orig_d if orig_d else orig_s)
        base, ext = os.path.splitext(dest_b)
        if ext.lower() == '.iso' or ext.lower() not in ['.mkv', '.mp4', '.m4v', '.webm', '.mov']:
            dest_b = base + '.mkv'

        if orig_d and ("\\" in orig_d or "/" in orig_d):
            sep = "\\" if "\\" in orig_d else "/"
            disp_dest = orig_d.rsplit(sep, 1)[0] + sep + dest_b
        else:
            disp_dest = f"/outputs/{dest_b}"

        temp_j = models.Job(
            job_hash=j_hash, original_filename=raw_b, source_path=orig_s,
            dest_path=disp_dest, hb_json_payload=item, status="WAITING", priority=1
        )
        # Nepřistupovat na disk / 7z při HTTP importu (ISO/BDMV sondování proběhne asynchronně ve ffprobe_worker_loop)
        populate_job_static_metadata(temp_j, allow_disk_probe=False)
        prepared_items.append(temp_j)

    prev_imm = set_tx_immediate(True)
    db = SessionLocal()
    added_cnt, skipped_cnt = 0, 0
    try:
        existing_hashes = {
            r[0] for r in db.query(models.Job.job_hash).filter(
                models.Job.status.in_(["WAITING", "IN_PROGRESS", "STAGING", "PREMUX", "COPYING", "WAITING_DISK", "PAUSED"])
            ).all()
        }
        next_p = next_bottom_priority(db)

        # job_hash je v DB unikátní i pro dokončené / zrušené / chybové úlohy. Když se znovu načte úloha,
        # která už jednou proběhla, starý záznam (historie) dostane příponu a nová úloha se normálně zařadí.
        # Duplicita v aktivní frontě se dál přeskakuje.
        new_hashes = {nj.job_hash for nj in prepared_items if nj.job_hash not in existing_hashes}
        if new_hashes:
            nh_list = list(new_hashes)
            old_rows = []
            for i in range(0, len(nh_list), 500):
                old_rows += (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.job_hash.in_(nh_list[i:i + 500]))
                    .all()
                )
            for orow in old_rows:
                orow.job_hash = f"{orow.job_hash}~{orow.id}"
            if old_rows:
                db.flush()

        for nj in prepared_items:
            if nj.job_hash in existing_hashes:
                skipped_cnt += 1
                continue
            existing_hashes.add(nj.job_hash)
            nj.priority = next_p
            next_p += 1
            db.add(nj)
            added_cnt += 1

        if added_cnt > 0:
            db.commit()
            append_manager_log(
                T("mgr_imported_new_jobs_into", added_cnt, skipped_cnt)
            )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"Error importing queue to DB: {e}")
        raise
    finally:
        db.close()
        set_tx_immediate(prev_imm)
    return added_cnt, skipped_cnt


def process_watchdog_file(filepath):
    try:
        # Počkat na dokončení SMB/lokálního zápisu (2 shodné po sobě jdoucí kontroly velikosti a mtime, max. 5 s)
        prev_stat = None
        stable = 0
        for _ in range(10):
            try:
                st = os.stat(filepath)
            except FileNotFoundError:
                return
            cur_stat = (st.st_size, st.st_mtime_ns)
            if cur_stat == prev_stat and st.st_size > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
                prev_stat = cur_stat
            time.sleep(0.5)
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            pl = json.load(f, parse_constant=lambda x: None)
        if pl:
            import_queue_payloads_to_db(pl if isinstance(pl, list) else [pl])
        try:
            os.remove(filepath)
        except Exception:
            try:
                os.replace(filepath, filepath + ".imported")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error processing watchdog file ({filepath}): {e}")
        try:
            os.replace(filepath, filepath + ".error")
        except Exception:
            pass


def poll_directory():
    while True:
        try:
            if os.path.exists(WATCH_DIR):
                for fn in os.listdir(WATCH_DIR):
                    if fn.startswith("queue") and fn.endswith(".json"):
                        fp = os.path.join(WATCH_DIR, fn)
                        if os.path.isfile(fp):
                            process_watchdog_file(fp)
        except Exception:
            pass
        time.sleep(3)


def parse_unc_path(p: str):
    s = str(p or "").strip().replace("/", "\\")
    if not s.startswith("\\\\"):
        return None
    parts = [x for x in s.lstrip("\\").split("\\") if x]
    if len(parts) < 2:
        return None
    return parts[0], parts[1], "\\".join(parts[2:])


def test_smb_unc_path(unc_path: str, username: str, password: str, need_write: bool = False, timeout_s: int = 8) -> dict:
    """
    Reálný test síťové cesty z Managera: (1) TCP 445, (2) SMB přihlášení,
    (3) výpis složky (čtení), (4) volitelně zápis + smazání testovacího souboru.
    Používá čistě Python knihovnu smbprotocol (bez mount/CAP_SYS_ADMIN v kontejneru).
    """
    res = {"path": unc_path or "", "ok": False, "skipped": False, "stage": "", "detail": "",
           "reachable": False, "auth_ok": False, "read_ok": False, "write_ok": None}
    if not str(unc_path or "").strip():
        res.update(ok=True, skipped=True, stage="empty", detail=T("smb_test_empty"))
        return res
    parsed = parse_unc_path(unc_path)
    if not parsed:
        res.update(ok=True, skipped=True, stage="not_unc", detail=T("smb_test_not_unc"))
        return res
    host, share, sub = parsed
    try:
        socket.create_connection((host, 445), timeout=3.0).close()
        res["reachable"] = True
    except Exception as e:
        res.update(stage="tcp", detail=T("smb_test_unreachable", host, str(e)[:80]))
        return res

    try:
        import smbclient  # z balíčku smbprotocol
    except Exception:
        res.update(ok=True, stage="tcp_only", detail=T("smb_test_tcp_only", host))
        return res

    stage = "auth"
    try:
        smbclient.register_session(host, username=(username or None), password=(password or None), connection_timeout=int(timeout_s))
        res["auth_ok"] = True
        stage = "read"
        full = f"\\\\{host}\\{share}" + (f"\\{sub}" if sub else "")
        entries = smbclient.listdir(full)
        res["read_ok"] = True
        if need_write:
            stage = "write"
            probe = f"{full}\\.hb_smb_probe_manager.tmp"     # fixed name: a failed delete leaves one file, not one per test
            try:
                with smbclient.open_file(probe, mode="w") as fh:
                    fh.write("hb-cluster-smb-probe")
            finally:
                for _i in range(3):
                    try:
                        smbclient.remove(probe)
                        break
                    except Exception:
                        time.sleep(0.3 * (_i + 1))
            res["write_ok"] = True
        res.update(ok=True, stage="ok", detail=T("smb_test_ok_rw" if need_write else "smb_test_ok_read", len(entries)))
    except Exception as e:
        if stage == "write":
            res["write_ok"] = False
        key = {"auth": "smb_test_auth_failed", "read": "smb_test_read_failed", "write": "smb_test_write_failed"}[stage]
        res.update(stage=stage, detail=T(key, str(e)[:140]))
    finally:
        try:
            smbclient.delete_session(host)
        except Exception:
            pass
    return res


def get_cached_upstream_versions(max_age_s: int = UPSTREAM_CHECK_MIN_INTERVAL_S) -> dict:
    up = binary_build_state.get("upstream_versions") or {}
    if up.get("checked_at") and (time.time() - float(up.get("checked_at") or 0)) < max_age_s:
        return dict(up)
    return check_upstream_tool_versions()


def _cluster_has_active_transfers() -> bool:
    db = SessionLocal()
    try:
        return db.query(models.Job.id).filter(models.Job.status.in_(["COPYING", "STAGING", "PREMUX"])).count() > 0
    except Exception:
        return True
    finally:
        db.close()


AUTO_BUILD_HOOK = None  # nastavuje main.py (auto_build_tick) – manager_core nesmí importovat main


def upstream_version_watcher_loop():
    time.sleep(15)  # po startu nechat workery zaregistrovat (heartbeat)
    while True:
        try:
            # Externí nástroje přes GitHub API cachovat na 60 min (UPSTREAM_CHECK_MIN_INTERVAL_S = 3600)
            get_cached_upstream_versions(max_age_s=UPSTREAM_CHECK_MIN_INTERVAL_S)
        except Exception:
            pass
        try:
            if AUTO_BUILD_HOOK:
                AUTO_BUILD_HOOK("watcher", True)
        except Exception as e:
            logger.warning(f"Auto SYSTEM_BUILD hook failed: {e}")
        try:
            # Lehká manifest kontrola GHCR každých 5 min (GHCR_CHECK_MIN_INTERVAL_S = 300)
            st = check_ghcr_manager_update(force=True)
            if (
                getattr(AppState, "auto_update_cluster", False)
                and st.get("update_available")
                and st.get("docker_sock_available")
                and not st.get("updating")
                and not manager_update_blocked(st.get("remote_version") or "")
                and binary_build_state.get("status") not in ("QUEUED", "BUILDING")
                and not _cluster_has_active_transfers()
            ):
                perform_cluster_self_update(reason="auto")
        except Exception:
            pass
        time.sleep(GHCR_CHECK_MIN_INTERVAL_S)


def shutdown_database_cleanly():
    """SIGTERM/SIGINT (docker stop): WAL checkpoint + uzavření všech SQLite spojení,
    aby po zastavení kontejneru nezůstaly neuzavřené soubory cluster.db-wal / -shm."""
    try:
        run_sqlite_pragma("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.warning(f"WAL checkpoint on shutdown failed: {e}")
    try:
        engine.dispose()
    except Exception:
        pass


def restore_worker_policies_from_backup(db_session):
    backup = getattr(AppState, "worker_policies_backup", None) or {}
    if not isinstance(backup, dict) or not backup:
        return
    existing = {p.worker_name: p for p in db_session.query(models.WorkerPolicy).all()}
    for w_name, b_cfg in backup.items():
        if not w_name or not isinstance(b_cfg, dict):
            continue
        pol = existing.get(w_name)
        if not pol:
            cap_b = str(b_cfg.get("capability") or "all")
            allow_4k_b = bool(b_cfg.get("allow_4k", cap_b not in ("1080p", "sd", "720p"))) and (cap_b not in ("1080p", "sd", "720p"))
            def_b = default_slots_for_worker(cap_b if not allow_4k_b or cap_b in ("sd", "720p", "1080p") else "all")
            pol = models.WorkerPolicy(
                worker_name=w_name,
                max_slots=clamp_num(b_cfg.get("max_slots"), 1, 16, def_b, as_int=True),
                capability=cap_b,
                allow_4k=allow_4k_b,
                is_enabled=bool(b_cfg.get("is_enabled", True)),
                prioritize_4k=bool(b_cfg.get("prioritize_4k", AppState.default_prioritize_4k)),
                use_local_temp=bool(b_cfg.get("use_local_temp", AppState.default_use_local_temp)),
                staging_mode=str(b_cfg.get("staging_mode") or AppState.default_staging_mode),
                power_mode=str(b_cfg.get("power_mode") or AppState.default_power_mode),
                custom_cores=serialize_custom_cores_list(b_cfg.get("custom_cores")),
            )
            db_session.add(pol)
        else:
            if "is_enabled" in b_cfg and b_cfg["is_enabled"] is not None:
                pol.is_enabled = bool(b_cfg["is_enabled"])
            if "custom_cores" in b_cfg and not getattr(pol, "custom_cores", ""):
                pol.custom_cores = serialize_custom_cores_list(b_cfg.get("custom_cores"))
    db_session.flush()


def get_or_create_worker_policy(db_session, w_name: str):
    pol = db_session.query(models.WorkerPolicy).filter(models.WorkerPolicy.worker_name == w_name).first()
    if not pol:
        b_cfg = (getattr(AppState, "worker_policies_backup", None) or {}).get(w_name) or {}
        cap_s = str(b_cfg.get("capability") or "all")
        allow_4k_v = bool(b_cfg.get("allow_4k", cap_s not in ("1080p", "sd", "720p"))) and (cap_s not in ("1080p", "sd", "720p"))
        def_slots = default_slots_for_worker(cap_s) if not (cap_s == "all" and not allow_4k_v) else 2
        pol = models.WorkerPolicy(
            worker_name=w_name,
            max_slots=clamp_num(b_cfg.get("max_slots"), 1, 16, def_slots, as_int=True),
            capability=cap_s,
            allow_4k=allow_4k_v,
            is_enabled=bool(b_cfg.get("is_enabled", True)),
            prioritize_4k=bool(b_cfg.get("prioritize_4k", AppState.default_prioritize_4k)),
            use_local_temp=bool(b_cfg.get("use_local_temp", AppState.default_use_local_temp)),
            staging_mode=str(b_cfg.get("staging_mode") or AppState.default_staging_mode),
            power_mode=str(b_cfg.get("power_mode") or AppState.default_power_mode),
            custom_cores=serialize_custom_cores_list(b_cfg.get("custom_cores")),
        )
        try:
            db_session.add(pol)
            db_session.commit()
            db_session.refresh(pol)
            save_manager_config(db_session)
        except IntegrityError:
            db_session.rollback()
            pol = db_session.query(models.WorkerPolicy).filter(models.WorkerPolicy.worker_name == w_name).first()
            if not pol:
                raise
    return pol


# S1: chyba přístupu ke zdroji (FILE – worker zdroj nevidí a nestáhl ho ani přes relay, Manager ho přitom má):
# úloha se vrátí do fronty, worker dostane pauzu 10 min (vidět na kartě), 3× za sebou → vypnout příjem s důvodem.
# Úspěšně dokončená úloha počítadlo nuluje.
FILE_ERROR_COOLDOWN_S = 600
FILE_ERROR_DISABLE_AFTER = 3
worker_file_errors = {}         # jméno → počet chyb FILE za sebou
worker_cooldown = {}            # jméno → {"until": čas, "reason": text}
worker_intake_off_reason = {}   # jméno → proč Manager sám vypnul příjem (zmizí po zapnutí příjmu)


def note_worker_file_error(db_session, w_name: str) -> str:
    """Zaznamená chybu FILE workeru. Vrací 'cooldown' (pauza 10 min) nebo 'disabled' (vypnutý příjem)."""
    if not w_name:
        return ""
    n = worker_file_errors.get(w_name, 0) + 1
    if n >= FILE_ERROR_DISABLE_AFTER:
        worker_file_errors.pop(w_name, None)
        worker_cooldown.pop(w_name, None)
        disable_worker_in_db(db_session, w_name)
        worker_intake_off_reason[w_name] = T("wrk_reason_file_errors", FILE_ERROR_DISABLE_AFTER)
        append_manager_log(T("api_worker_file_errors_disabled", w_name, FILE_ERROR_DISABLE_AFTER))
        return "disabled"
    worker_file_errors[w_name] = n
    worker_cooldown[w_name] = {"until": time.time() + FILE_ERROR_COOLDOWN_S, "reason": T("wrk_reason_file_error_pause")}
    append_manager_log(T("api_worker_file_error_pause", w_name, FILE_ERROR_COOLDOWN_S // 60, n, FILE_ERROR_DISABLE_AFTER))
    return "cooldown"


def worker_cooldown_left(w_name: str) -> int:
    """Kolik sekund má worker ještě pauzu po chybě FILE (0 = žádná)."""
    c = worker_cooldown.get(w_name or "")
    if not c:
        return 0
    left = int(c.get("until", 0) - time.time())
    if left <= 0:
        worker_cooldown.pop(w_name, None)
        return 0
    return left


def note_worker_job_success(w_name: str):
    if not w_name:
        return
    worker_file_errors.pop(w_name, None)
    worker_cooldown.pop(w_name, None)


def clear_worker_intake_off_reason(w_name: str):
    """Volá se při zapnutí příjmu (is_enabled=True): vymaže důvod předchozího auto-vypnutí i pauzu."""
    if not w_name:
        return
    worker_intake_off_reason.pop(w_name, None)
    worker_cooldown.pop(w_name, None)
    worker_file_errors.pop(w_name, None)


def cleanup_deleted_worker_state(w_name: str):
    """Uklidí veškerý in-memory stav smazaného workeru ze všech slovníků (ochrana proti úniku RAM)."""
    if not w_name:
        return
    for d in (
        active_workers, worker_versions, worker_seen_ip, worker_seen_ip_recent, worker_update_now,
        worker_telemetry, worker_telemetry_history,
        worker_file_errors, worker_cooldown, worker_intake_off_reason,
    ):
        d.pop(w_name, None)
    with handshake_lock:
        worker_handshakes.pop(w_name, None)
        active_handshake_tokens.pop(w_name, None)
    with held_sync_lock:
        worker_reported_held_jobs.pop(w_name, None)
    with logs_lock:
        worker_logs_store.pop(w_name, None)
    with windows_settings_lock:
        if isinstance(AppState.worker_windows_settings, dict):
            AppState.worker_windows_settings.pop(w_name, None)
        if isinstance(AppState.pending_worker_windows_settings, dict):
            AppState.pending_worker_windows_settings.pop(w_name, None)
    with config_file_lock:
        if isinstance(AppState.worker_policies_backup, dict):
            AppState.worker_policies_backup.pop(w_name, None)


def reset_job_for_requeue(job, priority=None, reset_attempts: bool = False, clear_errors: bool = True):
    """Jednotný reset všech běhových polí úlohy při vrácení zpět do fronty (WAITING)."""
    job.status = "WAITING"
    setattr(job, "is_paused", False)
    if priority is not None:
        job.priority = int(priority)
    job.worker_name = None
    job.progress = 0.0
    job.fps_current = 0.0
    job.fps_avg = 0.0
    job.speed_mbps = 0.0
    job.elapsed_sec = 0
    job.eta_sec = 0
    job.transferred_bytes = 0
    job.is_net_fallback = False
    setattr(job, "net_fallback_reason", None)
    setattr(job, "disk_hold_reason", "")
    if reset_attempts:
        job.attempt_count = 0
    if clear_errors:
        job.error_code = None
        job.error_detail = None
    if getattr(job, "id", None) is not None:
        clear_disk_hold_tracking(job.id)
        job_heartbeats.pop(job.id, None)
        job_assigned_time.pop(job.id, None)
        release_output_upload_slot(job.id)


def disable_worker_in_db(db_session, w_name: str):
    if w_name:
        pol = db_session.query(models.WorkerPolicy).filter(models.WorkerPolicy.worker_name == w_name).first()
        if pol:
            pol.is_enabled = False
            db_session.flush()
            save_manager_config(db_session)


def clean_old_jobs(days=30):
    prev_imm = set_tx_immediate(True)
    db = SessionLocal()
    try:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        deleted = db.query(models.Job).filter(models.Job.status.in_(["COMPLETED", "CANCELLED"]), models.Job.created_at < cutoff).delete()
        db.commit()
        if deleted:
            invalidate_analytics_cache()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
        set_tx_immediate(prev_imm)


def record_disk_hold_entered(job_id: int):
    now_t = time.time()
    with held_sync_lock:
        disk_hold_entered_ts[job_id] = now_t
        orphaned_disk_missing_since.pop(job_id, None)


def clear_disk_hold_tracking(job_id: int):
    with held_sync_lock:
        disk_hold_entered_ts.pop(job_id, None)
        orphaned_disk_missing_since.pop(job_id, None)


def mark_held_disk_job_lost(db, job, worker_name: str = ""):
    """Vrátí osiřelou úlohu z WAITING_DISK zpět na začátek fronty (WAITING), pokud její výstup v /temp zmizel."""
    w_lbl = sanitize_display_name(worker_name or getattr(job, "worker_name", "") or "worker", "worker")
    reset_job_for_requeue(job, priority=next_top_priority(db), clear_errors=False)
    job.error_code = "ERR_HELD_OUTPUT_LOST"
    job.error_detail = w_lbl
    append_manager_log(T("api_job_held_output_lost", job.id, job.original_filename, w_lbl))


def reconcile_worker_held_disk_jobs(db, worker_name: str, reported_held_ids, active_running_ids=None, grace_sec: float = 45.0) -> list:
    """Porovná workerem hlášené podržené úlohy s DB; dlouhodobě chybějící vrátí zpět na začátek fronty (WAITING)."""
    if not worker_name or reported_held_ids is None:
        return []
    now_t = time.time()
    held_set = set()
    for x in reported_held_ids:
        try:
            held_set.add(int(x))
        except (TypeError, ValueError):
            pass
    present_ids = set(held_set)
    for x in (active_running_ids or ()):
        try:
            present_ids.add(int(x))
        except (TypeError, ValueError):
            pass

    with held_sync_lock:
        worker_reported_held_jobs[worker_name] = (now_t, held_set)

    disk_jobs = (
        db.query(models.Job)
        .options(defer(models.Job.hb_json_payload))
        .filter(models.Job.status == "WAITING_DISK", models.Job.worker_name == worker_name)
        .all()
    )
    if not disk_jobs:
        return []

    lost_ids = []
    for dj in disk_jobs:
        if dj.id in present_ids:
            with held_sync_lock:
                orphaned_disk_missing_since.pop(dj.id, None)
            job_heartbeats[dj.id] = now_t
            continue
        with held_sync_lock:
            entered_ts = disk_hold_entered_ts.get(dj.id, 0.0)
        if (now_t - entered_ts) < 12.0 or (now_t - job_assigned_time.get(dj.id, 0.0)) < grace_sec:
            continue
        with held_sync_lock:
            first_ts = orphaned_disk_missing_since.setdefault(dj.id, now_t)
        if (now_t - first_ts) >= grace_sec:
            mark_held_disk_job_lost(db, dj, worker_name)
            lost_ids.append(dj.id)
    return lost_ids


def is_job_known_missing_on_worker(job_id: int, worker_name: str, max_age_sec: float = 30.0) -> bool:
    """Vrátí True, pokud online worker čerstvě nahlásil seznam podržených úloh a toto ID v něm není."""
    if not worker_name:
        return False
    now_t = time.time()
    l_seen = active_workers.get(worker_name, 0)
    if not l_seen or (now_t - l_seen) > max_age_sec:
        return False
    with held_sync_lock:
        rep = worker_reported_held_jobs.get(worker_name)
        entered_ts = disk_hold_entered_ts.get(job_id, 0.0)
    if not rep:
        return False
    rep_ts, held_set = rep
    if (now_t - rep_ts) > max_age_sec:
        return False
    if entered_ts > 0.0 and rep_ts < (entered_ts + 5.0):
        return False
    return int(job_id) not in held_set


def release_or_fail_held_disk_jobs(db, jobs_list) -> tuple:
    """Při ručním pokusu o nahrání buď uvolní slot, nebo rovnou vrátí ztracené úlohy zpět do fronty."""
    released_ids, lost_ids = [], []
    for j in jobs_list:
        if j.status == "WAITING_DISK" and is_job_known_missing_on_worker(j.id, j.worker_name):
            mark_held_disk_job_lost(db, j, j.worker_name)
            lost_ids.append(j.id)
        else:
            with upload_reservation_lock:
                manual_upload_release_ids.add(j.id)
            released_ids.append(j.id)
    return released_ids, lost_ids


def compute_cleanup_held_job_ids(db, worker_name: str, reported_held_ids) -> list:
    """Vrátí ID podržených úloh hlášených workerem, které v DB neexistují, jsou hotové/zrušené nebo patří jinému workeru.
    Pokud úloha mezitím spadla do WAITING/ERROR bez cizího vlastníka (např. při restartu Manageru během výpadku NAS),
    obnoví ji zpět do WAITING_DISK pro tento worker místo zničení hotového videa."""
    if not worker_name or not reported_held_ids:
        return []
    req_ids = []
    seen = set()
    for x in reported_held_ids:
        try:
            jid = int(x)
        except (TypeError, ValueError):
            continue
        if jid not in seen:
            seen.add(jid)
            req_ids.append(jid)
    if not req_ids:
        return []
    jobs = db.query(models.Job).options(defer(models.Job.hb_json_payload)).filter(
        models.Job.id.in_(req_ids)
    ).all()
    by_id = {j.id: j for j in jobs}
    cleanup_ids = []
    revived = False
    now_t = time.time()
    for jid in req_ids:
        j = by_id.get(jid)
        if j is None:
            cleanup_ids.append(jid)
            continue
        st, w_name = j.status, j.worker_name
        if st in ("WAITING_DISK", "COPYING") and w_name == worker_name:
            job_heartbeats[jid] = now_t
            continue
        if st in ("WAITING", "ERROR") and (not w_name or w_name == worker_name):
            # Worker má hotové video v local temp – nikdy ho neničit, obnovit WAITING_DISK
            j.status = "WAITING_DISK"
            j.worker_name = worker_name
            j.progress = 100.0
            j.is_paused = False
            job_heartbeats[jid] = now_t
            with held_sync_lock:
                disk_hold_entered_ts[jid] = now_t
                orphaned_disk_missing_since.pop(jid, None)
            revived = True
            continue
        cleanup_ids.append(jid)
    if revived:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return cleanup_ids


def check_zombie_jobs():
    now_t = time.time()
    prev_imm = set_tx_immediate(True)
    db = SessionLocal()
    try:
        running_list = (
            db.query(models.Job)
            .options(defer(models.Job.hb_json_payload))
            .filter(models.Job.status.in_(ACTIVE_JOB_STATUSES))
            .all()
        )
        dirty = False

        for j in running_list:
            if j.id not in job_heartbeats:
                job_heartbeats[j.id] = now_t
                continue
            if j.status == "WAITING_DISK" or bool(getattr(j, "is_paused", False)):
                w_last = active_workers.get(j.worker_name or "", 0)
                if (now_t - w_last) <= 120:
                    job_heartbeats[j.id] = now_t
                    continue

            limit_s = 900 if is_system_build_job(j) else 300
            if now_t - job_heartbeats[j.id] > limit_s:
                if j.status == "WAITING_DISK":
                    continue
                j.attempt_count += 1
                j.error_code = "ERR_ZOMBIE_TIMEOUT"
                j.error_detail = f"Timeout on {j.worker_name or 'worker'}"
                append_manager_log(
                    T("mgr_job_worker_stopped_responding", j.id, j.original_filename, j.worker_name)
                )
                if j.attempt_count >= 3 or is_system_build_job(j):
                    j.status = "ERROR"
                    setattr(j, "is_paused", False)
                    if is_system_build_job(j):
                        j.encoder_summary = f"Error: {j.error_detail}"
                        with build_lock:
                            binary_build_state["status"] = "ERROR"
                            binary_build_state["updating_tools"] = []
                            binary_build_state["step_message"] = j.error_detail
                            binary_build_state["finished_at"] = int(time.time())
                    job_heartbeats.pop(j.id, None)
                    job_assigned_time.pop(j.id, None)
                    release_output_upload_slot(j.id)
                else:
                    reset_job_for_requeue(j, priority=next_top_priority(db), clear_errors=False)
                dirty = True

        if dirty:
            db.commit()

        # Úklid neaktivních klíčů z in-memory heartbeat map (prevence pomalého úniku RAM)
        active_ids = {j.id for j in running_list if j.status in ACTIVE_JOB_STATUSES}
        for stale_id in list(job_heartbeats.keys()):
            if stale_id not in active_ids and (now_t - float(job_heartbeats.get(stale_id) or 0)) > 60:
                job_heartbeats.pop(stale_id, None)
        for stale_id in list(job_assigned_time.keys()):
            if stale_id not in active_ids and (now_t - float(job_assigned_time.get(stale_id) or 0)) > 60:
                job_assigned_time.pop(stale_id, None)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
        set_tx_immediate(prev_imm)

    if AppState.auto_clean_history:
        clean_old_jobs(30)


# Které soubory smí builder nahrát pro jednotlivé cíle SYSTEM_BUILD (stejná logika jako
# _should_build_tool v worker_linux.py). 7z.dll a HBWorker.exe v compute_updating_tools_list nejsou.
BUILD_TARGET_UPLOADS = {
    "7z": (("linux", "7z"), ("windows", "7z"), ("windows", "7z_dll")),
    "mkvtoolnix": (("linux", "mkvmerge"), ("linux", "mkvpropedit"), ("windows", "mkvmerge"), ("windows", "mkvpropedit")),
    "ffmpeg": (("linux", "ffmpeg"), ("linux", "ffprobe"), ("windows", "ffmpeg"), ("windows", "ffprobe")),
    "hbcli": (("linux", "hbcli"), ("windows", "hbcli")),
    "launcher": (("windows", "launcher"),),
}


def allowed_build_uploads(payload) -> set:
    """Množina (os, nástroj), které smí builder nahrát během SYSTEM_BUILD s tímto zadáním."""
    pl = payload if isinstance(payload, dict) else {}
    tgts = set(pl.get("target_tools") or pl.get("targets") or ["all"])
    force_all = bool(pl.get("force_all") or (pl.get("force_rebuild") and "all" in tgts))
    uptodate = set(pl.get("uptodate_tools") or [])
    out = set()
    for t_key, pairs in BUILD_TARGET_UPLOADS.items():
        if force_all or ((t_key not in uptodate) if ("all" in tgts) else (t_key in tgts)):
            out.update(pairs)
    return out


def compute_updating_tools_list(targets: list) -> list:
    t_set = set(targets or ["all"])
    if "all" in t_set:
        return [f"{os_k}_{tk}" for os_k in ("linux", "windows") for tk in ALL_TOOLS]
    out = []
    if "hbcli" in t_set:
        out.extend(["linux_hbcli", "windows_hbcli"])
    if "mkvtoolnix" in t_set:
        out.extend(["linux_mkvmerge", "linux_mkvpropedit", "windows_mkvmerge", "windows_mkvpropedit"])
    if "ffmpeg" in t_set:
        out.extend(["linux_ffmpeg", "linux_ffprobe", "windows_ffmpeg", "windows_ffprobe"])
    if "7z" in t_set:
        out.extend(["linux_7z", "windows_7z"])
    if "launcher" in t_set:
        out.append("windows_launcher")
    return out


def count_burned_subtitles(payload) -> int:
    """R1: kolik titulků má v HandBrake GUI zapnuté vypálení do obrazu (štítek Burn-in u úlohy)."""
    try:
        _, job_node = unwrap_job_payload_nodes(payload)
        st = job_node.get("SubtitleTracks") if isinstance(job_node, dict) else None
        if not isinstance(st, list):
            st = (job_node.get("Subtitle") or {}).get("SubtitleList") if isinstance(job_node, dict) and isinstance(job_node.get("Subtitle"), dict) else []
        return sum(1 for t in (st or []) if isinstance(t, dict) and (t.get("Burned") or t.get("Burn")))
    except Exception:
        return 0


def serialize_job(j: models.Job) -> dict:
    sys_f = is_system_build_job(j)
    has_meta = bool(
        sys_f
        or (
            j.res_in
            and j.res_in not in ("Neznámé", "Unknown")
            and j.res_out
            and j.preset_name
            and j.video_encoder
            and ((j.video_duration_sec or 0) > 0 or j.status == "COMPLETED")
        )
    )
    if sys_f:
        in_l, out_l, dur_s, preset_n = T("job_system"), "CLUSTER", 0.0, "SYSTEM_BUILD"
    elif has_meta:
        in_l = j.res_in
        out_l = j.res_out
        dur_s = float(j.video_duration_sec or 0.0)
        preset_n = j.preset_name
    else:
        in_l, out_l, _, dur_s, preset_n = get_job_media_info_ext(j)

    full_s = j.source_path or ("" if sys_f else extract_orig_source_from_payload(j.hb_json_payload)) or j.original_filename
    dest_fn = j.dest_path if sys_f else (j.dest_path.split('\\')[-1] if '\\' in (j.dest_path or "") else (j.dest_path or "").split('/')[-1])

    if not j.video_encoder and not sys_f:
        populate_job_static_metadata(j, allow_disk_probe=False)

    eff_res_in = in_l if (in_l and in_l not in ("Neznámé", "Unknown")) else (j.res_in if (j.res_in and j.res_in not in ("Neznámé", "Unknown")) else T("job_unknown"))
    eff_res_out = out_l if out_l else (j.res_out or "1080p")
    eff_vid_dur = int(dur_s if dur_s > 0 else (j.video_duration_sec or 0))

    rf_clean = None
    if j.video_rf is not None:
        rf_clean = int(j.video_rf) if float(j.video_rf).is_integer() else round(float(j.video_rf), 1)

    lpcm_c = getattr(j, "lpcm_Tracks_muxed", getattr(j, "lpcm_tracks_muxed", 0)) or 0
    v_in = 0 if int(j.v_bitrate_in_kbps or 0) == 2500 else int(j.v_bitrate_in_kbps or 0)
    a_in = int(j.a_bitrate_in_kbps or 0)
    v_out = int(j.v_bitrate_out_kbps or 0)
    src_b = int(j.src_size_bytes or 0)
    out_b = int(j.out_size_bytes or 0)

    s_low = (full_s or "").lower()
    persisted_disc = getattr(j, "is_disc_source", None)
    is_disc = bool(
        not sys_f
        and (
            persisted_disc is True
            or s_low.endswith(".iso")
            or "bdmv" in s_low
            or "video_ts" in s_low
        )
    )

    if not sys_f and src_b <= 0 and is_disc and full_s:
        with media_cache_lock:
            c_hit = media_cache.get(f"{full_s}::T1") or media_cache.get(full_s)
        if isinstance(c_hit, dict) and int(c_hit.get("src_bytes") or 0) > 0:
            src_b = int(c_hit["src_bytes"])

    if not sys_f and src_b >= 100 * 1024 * 1024 and eff_vid_dur > 60 and is_disc and (v_in <= 0 or a_in <= 0):
        est_v, est_a = estimate_disc_bitrates_from_size_and_duration(
            src_b, float(eff_vid_dur), None, a_in
        )
        if est_v > 0 and (v_in <= 0 or (est_v >= 8000 and v_in < est_v * 0.55) or (v_in > est_v * 1.65)):
            v_in = est_v
        if est_a > 0 and a_in <= 0:
            a_in = est_a

    is_ineff = bool(not sys_f and j.status == "COMPLETED" and v_in > 0 and v_out >= v_in)

    pm_stat = str(getattr(j, "premux_status", "") or "") if isinstance(getattr(j, "premux_status", ""), str) else ""
    if not pm_stat and not sys_f:
        if j.premux_applied:
            pm_stat = "OK"
        elif is_disc and j.status in ("IN_PROGRESS", "COPYING", "WAITING_DISK", "COMPLETED"):
            pm_stat = "FALLBACK" if AppState.smart_premux else "OFF"

    hold_reason = str(getattr(j, "disk_hold_reason", "") or "") if isinstance(getattr(j, "disk_hold_reason", ""), str) else ""

    b_subs_col = getattr(j, "burned_subs", None)
    if sys_f:
        burned_val = 0
    elif b_subs_col is not None and int(b_subs_col) >= 0:
        burned_val = int(b_subs_col)
    else:
        burned_val = count_burned_subtitles(j.hb_json_payload)

    return {
        "id": j.id,
        "is_system_job": sys_f,
        "is_disc_source": is_disc,
        "burned_subs": burned_val,
        "original_filename": j.original_filename,
        "dest_filename": dest_fn,
        "source_path": full_s,
        "dest_path": j.dest_path,
        "status": j.status,
        "is_paused": bool(getattr(j, "is_paused", False)),
        "priority": j.priority,
        "attempt_count": j.attempt_count,
        "progress": round(float(j.progress or 0.0), 1),
        "worker_name": j.worker_name or "",
        "worker_version": j.worker_version or "",
        "res_in": eff_res_in,
        "res_out": eff_res_out,
        "video_duration_sec": eff_vid_dur,
        "preset_name": j.preset_name or preset_n,
        "encoder_summary": j.encoder_summary or "",
        "video_encoder": j.video_encoder or "",
        "video_preset": j.video_preset or "",
        "video_rf": rf_clean,
        "audio_count": int(j.audio_count or 0),
        "subs_count": int(j.subs_count or 0),
        "fps_current": round(float(j.fps_current or 0.0), 1),
        "fps_avg": round(float(j.fps_avg or 0.0), 1),
        "speed_mbps": round(float(j.speed_mbps or 0.0), 1),
        "elapsed_sec": int(j.elapsed_sec or 0),
        "eta_sec": int(j.eta_sec or 0),
        "transferred_bytes": int(j.transferred_bytes or 0),
        "src_size_bytes": src_b,
        "out_size_bytes": out_b,
        "duration_sec": int(j.duration_sec or 0),
        "completed_at": j.completed_at.isoformat() if j.completed_at else (j.created_at.isoformat() if j.created_at else None),
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "subs_removed": int(j.subs_removed or 0),
        "subs_status": j.subs_status or "",
        "error_code": j.error_code or "",
        "error_detail": j.error_detail or "",
        "disk_hold_reason": hold_reason,
        "is_net_fallback": bool(j.is_net_fallback),
        "net_fallback_reason": getattr(j, "net_fallback_reason", None) or "",
        "v_bitrate_in_kbps": v_in,
        "a_bitrate_in_kbps": a_in,
        "v_bitrate_out_kbps": v_out,
        "a_bitrate_out_kbps": int(j.a_bitrate_out_kbps or 0),
        "mkv_stats_written": bool(j.mkv_stats_written),
        "lpcm_tracks_muxed": int(lpcm_c),
        "premux_applied": bool(j.premux_applied),
        "premux_status": pm_stat,
        "is_ineffective": is_ineff,
        "job_weight": get_job_weight(j),
    }