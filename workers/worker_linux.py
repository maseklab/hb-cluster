# ==============================================================================
# HB CLUSTER WORKER - LINUX HEADLESS CLIENT (worker_linux.py)
# ==============================================================================

import os
import sys
import time
import errno
import socket
import requests
import subprocess
import re
import json
import threading
import shutil
import signal
import copy
import zipfile
import tarfile
import hashlib
import datetime
import platform

# O1: zkušební načtení této verze před aktualizací (python worker_linux.py --selftest; nic nespouští ani nezapisuje)
SELFTEST = "--selftest" in sys.argv[1:]


def _load_app_version(fallback: str = "0.0.0") -> str:
    """Verze se čte jen ze souboru VERSION (je v obrazu). Bez něj 0.0.0 – Manager pak nabídne aktualizaci."""
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

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_BIN_DIR = os.path.join(APP_DIR, "bin")
LEGACY_APP_TEMP_DIR = os.path.join(APP_DIR, "HB_Temp")
LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "config.json")


def _resolve_worker_data_dir() -> str:
    for cand in ("/app/data", "/data"):
        try:
            if os.path.ismount(cand):
                return cand
        except Exception:
            pass
    env_d = (os.getenv("HB_WORKER_DATA_DIR") or os.getenv("HB_DATA_DIR") or "").strip()
    if env_d:
        return env_d
    if os.path.isdir("/app/data") and not os.path.islink("/app/data"):
        try:
            if os.listdir("/app/data") or not os.path.isdir("/data"):
                return "/app/data"
        except Exception:
            pass
    if os.path.isdir("/data") or os.path.exists("/.dockerenv"):
        return "/data"
    return os.path.join(APP_DIR, "data")


def _is_custom_temp_mount() -> bool:
    try:
        if not os.path.ismount("/temp"):
            return False
        if os.path.isfile("/proc/mounts"):
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    pts = ln.split()
                    if len(pts) >= 2 and pts[1] == "/temp":
                        if "/volumes/hb_temp_" in pts[0]:
                            return False
                        return True
        return True
    except Exception:
        return False


WORKER_DATA_DIR = _resolve_worker_data_dir()
os.makedirs(WORKER_DATA_DIR, exist_ok=True)
BIN_DIR = os.path.join(WORKER_DATA_DIR, "binaries")
os.makedirs(BIN_DIR, exist_ok=True)
if not SELFTEST and BIN_DIR:
    _cur_path = os.environ.get("PATH", "")
    if BIN_DIR not in [p.strip() for p in _cur_path.split(os.pathsep) if p.strip()]:
        os.environ["PATH"] = BIN_DIR + (os.pathsep + _cur_path if _cur_path else "")
DEFAULT_HB_TEMP_DIR = os.path.join(WORKER_DATA_DIR, "temp")
os.makedirs(DEFAULT_HB_TEMP_DIR, exist_ok=True)
WORKER_LOG_FILE = os.path.join(WORKER_DATA_DIR, "worker.log")
LOCAL_CONFIG_FILE = os.path.join(WORKER_DATA_DIR, "config.json")
GEN_MARKER_FILE = os.path.join(BIN_DIR, ".binary_generation")
CORE_FILE_PATH = os.path.join(APP_DIR, "worker_core.py")
_log_file_lock = threading.Lock()

try:
    INITIAL_ALLOWED_CPUS = sorted(list(os.sched_getaffinity(0)))
except Exception:
    INITIAL_ALLOWED_CPUS = []


def _bootstrap_udp_discover(port: int = UDP_DISCOVERY_PORT, timeout: float = 2.5) -> str:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        pkt = b"HB_DISCOVER_MANAGER_V1"
        targets = {"255.255.255.255"}
        try:
            sp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sp.connect(("8.8.8.8", 80))
            lip = sp.getsockname()[0]
            sp.close()
            pts = lip.split(".")
            if len(pts) == 4:
                targets.add(f"{pts[0]}.{pts[1]}.{pts[2]}.255")
        except Exception:
            pass
        for tip in targets:
            try:
                sock.sendto(pkt, (tip, int(port)))
            except Exception:
                pass
        data, addr = sock.recvfrom(1024)
        resp = json.loads((data or b"{}").decode("utf-8", errors="ignore"))
        if isinstance(resp, dict) and resp.get("magic") == "HB_MANAGER_HERE":
            m_port = int(resp.get("port") or port)
            return f"http://{addr[0]}:{m_port}"
    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return ""


def _clean_str(val: str) -> str:
    s = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff\u00a0]+", "", str(val or "")).replace("\r", "").replace("\n", "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def load_initial_worker_settings():
    cfg_d = {}
    for cfg_p in (LOCAL_CONFIG_FILE, LEGACY_CONFIG_FILE):
        if os.path.exists(cfg_p):
            try:
                with open(cfg_p, "r", encoding="utf-8-sig") as f:
                    raw_cfg = f.read().lstrip("\ufeff").strip()
                    parsed_cfg = json.loads(raw_cfg) if raw_cfg else {}
                    if isinstance(parsed_cfg, dict) and parsed_cfg:
                        cfg_d = parsed_cfg
                        break
            except (OSError, PermissionError, ValueError):
                pass
    m_url = _clean_str(os.getenv("MANAGER_URL") or cfg_d.get("manager_url") or cfg_d.get("server_url") or "http://localhost:8383")
    if m_url.upper() == "AUTO":
        disc_u = _bootstrap_udp_discover()
        m_url = _clean_str(disc_u) if disc_u else "http://localhost:8383"
    if m_url and not re.match(r"^https?://", m_url, re.IGNORECASE):
        m_url = "http://" + m_url.lstrip("/")
    w_name = _clean_str(os.getenv("WORKER_NAME") or cfg_d.get("worker_name") or socket.gethostname()) or "linux-worker"

    env_tmp = _clean_str(os.getenv("LOCAL_TEMP_DIR") or os.getenv("TEMP_DIR") or ("/temp" if _is_custom_temp_mount() else ""))
    cfg_tmp = _clean_str(cfg_d.get("local_temp_dir") or cfg_d.get("temp_dir") or "")
    if env_tmp:
        t_dir = env_tmp
    elif cfg_tmp and cfg_tmp not in ("/tmp/hb_temp", "/temp", LEGACY_APP_TEMP_DIR):
        t_dir = cfg_tmp
    else:
        t_dir = DEFAULT_HB_TEMP_DIR

    raw_lang = str(cfg_d.get("lang") or cfg_d.get("ui_lang") or "en").strip().lower()
    u_lang = raw_lang if raw_lang in ("en", "cs") else "en"
    return m_url.rstrip("/"), w_name, t_dir, u_lang


MANAGER_URL, WORKER_NAME, LOCAL_TEMP_DIR, INITIAL_LANG = load_initial_worker_settings()

# One worker per data folder. Two containers sharing /data would share the worker key, the config and the
# Temp folder – the start-up cleanup of the second one would delete the files of the job the first one encodes.
DATA_DIR_LOCK_FILE = ".hb_worker.lock"
DATA_DIR_LOCK_WAIT_S = 60


def acquire_data_dir_lock(data_dir: str, worker_name: str, wait_s: float = DATA_DIR_LOCK_WAIT_S, poll_s: float = 5.0):
    """Locks <data_dir>/.hb_worker.lock (flock, released by the kernel when the process ends).
    Returns the open file (keep it open) or None when the file system cannot lock (then the worker runs as before).
    Held by another worker: waits up to wait_s (e.g. a container that is just stopping), then ends the process
    with exit code 3 – Docker restarts it later and the message stays in `docker logs`."""
    import fcntl
    path = os.path.join(data_dir, DATA_DIR_LOCK_FILE)
    try:
        fh = open(path, "a+", encoding="utf-8")
    except OSError as e:
        print(f"[WARN] Data folder lock not available ({e}) – continuing without it.", flush=True)
        return None
    t_end = time.time() + max(0.0, wait_s)
    told = False
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                print(f"[WARN] Data folder lock not supported here ({e}) – continuing without it.", flush=True)
                return fh
            owner = {}
            try:
                with open(path, "r", encoding="utf-8") as rf:
                    owner = json.loads(rf.read() or "{}")
            except Exception:
                pass
            who = f"worker \"{owner.get('worker_name') or '?'}\", container {owner.get('host') or '?'}"
            if owner.get("since"):
                who += f", since {owner['since']}"
            if not told:
                print(f"[ERROR] The data folder {data_dir} is already used by another HB worker ({who}). "
                      f"Every worker needs its own data folder: give this container a different host folder for "
                      f"{data_dir} (e.g. /opt/hb_worker/{re.sub(r'[^a-z0-9_-]+', '-', (worker_name or 'worker').lower())}). "
                      f"Waiting {int(wait_s)} s in case the other container is just stopping...", flush=True)
                told = True
            if time.time() >= t_end:
                print(f"[ERROR] Data folder {data_dir} is still in use ({who}) – this worker does not start. "
                      f"Docker will try again later.", flush=True)
                try:
                    fh.close()
                except Exception:
                    pass
                sys.exit(3)
            time.sleep(poll_s)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"worker_name": worker_name, "host": socket.gethostname(), "pid": os.getpid(),
                             "since": time.strftime("%Y-%m-%d %H:%M:%S")}))
        fh.flush()
    except Exception:
        pass
    if told:
        print(f"[INFO] Data folder {data_dir} is free now – starting.", flush=True)
    return fh


_DATA_DIR_LOCK = None if SELFTEST else acquire_data_dir_lock(WORKER_DATA_DIR, WORKER_NAME)
log_buffer = None
_config_file_lock = threading.Lock()
_bin_sync_lock = threading.Lock()
_shutdown_event = threading.Event()
_updating_or_restarting = False


def is_http_relay_active() -> bool:
    if str(os.getenv("HB_HTTP_RELAY", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        if os.path.isfile(LOCAL_CONFIG_FILE):
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            if cfg.get("http_relay") or cfg.get("use_http_relay"):
                return True
    except Exception:
        pass
    return False


def save_local_worker_config():
    with _config_file_lock:
        try:
            lang_v = wc.get_worker_lang() if "wc" in globals() else INITIAL_LANG
            payload = {
                "server_url": MANAGER_URL,
                "manager_url": MANAGER_URL,
                "worker_name": WORKER_NAME,
                "local_temp_dir": LOCAL_TEMP_DIR,
                "lang": lang_v,
                "ui_lang": lang_v
            }
            if is_http_relay_active():
                payload["http_relay"] = True
            tmp_p = LOCAL_CONFIG_FILE + ".tmp"
            with open(tmp_p, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp_p, LOCAL_CONFIG_FILE)
        except Exception:
            pass


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{WORKER_NAME}] {msg}"
    print(line, flush=True)
    if log_buffer is not None:
        log_buffer.append(line)
    with _log_file_lock:
        try:
            if os.path.isfile(WORKER_LOG_FILE) and os.path.getsize(WORKER_LOG_FILE) > 5 * 1024 * 1024:
                with open(WORKER_LOG_FILE, "rb") as rf:
                    rf.seek(-2 * 1024 * 1024, os.SEEK_END)
                    tail_bytes = rf.read()
                nl_idx = tail_bytes.find(b"\n")
                if nl_idx != -1:
                    tail_bytes = tail_bytes[nl_idx + 1:]
                with open(WORKER_LOG_FILE, "wb") as wf:
                    wf.write(tail_bytes)
            with open(WORKER_LOG_FILE, "a", encoding="utf-8") as wf:
                wf.write(line + "\n")
        except Exception:
            pass


def _bootstrap_bound_manager_ok(resp) -> bool:
    """Před načtením worker_core: odpověď Manageru s jiným ID, než je v manager_binding.json, nepřijmout."""
    try:
        with open(os.path.join(WORKER_DATA_DIR, "manager_binding.json"), "r", encoding="utf-8") as f:
            bound = str((json.load(f) or {}).get("manager_id") or "")
    except Exception:
        return True
    return (not bound) or (resp.headers.get("X-HB-Manager-Id") or "") == bound


def _bootstrap_fetch_text(url: str, timeout: float = 8.0) -> str:
    r = requests.get(url, headers={"Connection": "close"}, timeout=timeout)
    if r.status_code != 200 or not _bootstrap_bound_manager_ok(r):
        return ""
    return r.content.decode("utf-8-sig").lstrip("\ufeff").replace("\r\n", "\n")


def _bootstrap_fetch_core() -> str:
    """worker_core.py z Manageru podle OTA manifestu (kontrola SHA-256). "" = nepovedlo se."""
    rm = requests.get(f"{MANAGER_URL}/api/update/manifest?target=linux", headers={"Connection": "close"}, timeout=10)
    if rm.status_code != 200 or not _bootstrap_bound_manager_ok(rm):
        return ""
    ent = next((e for e in ((rm.json() or {}).get("files") or []) if isinstance(e, dict) and e.get("name") == "worker_core.py"), None)
    if not ent:
        return ""
    r = requests.get(f"{MANAGER_URL}/api/update/file/worker_core.py", headers={"Connection": "close"}, timeout=20)
    if r.status_code != 200 or not _bootstrap_bound_manager_ok(r):
        return ""
    if hashlib.sha256(r.content).hexdigest() != str(ent.get("sha256") or "").strip().lower():
        return ""
    return r.content.decode("utf-8-sig").lstrip("\ufeff").replace("\r\n", "\n")


def ensure_worker_core_module():
    """worker_core.py je součástí obrazu. Jen když chybí nebo je poškozený, stáhne se z Manageru podle OTA
    manifestu (SHA-256). Dřívější porovnání literálu verze kvůli workerům <= 2.11.4ZH je zrušené (O3)."""
    global MANAGER_URL
    os.makedirs(BIN_DIR, exist_ok=True)
    loc_p = os.path.join(APP_DIR, "locales.js")
    while True:
        if os.path.isfile(CORE_FILE_PATH):
            try:
                with open(CORE_FILE_PATH, "r", encoding="utf-8-sig") as f:
                    compile(f.read().lstrip("\ufeff").replace("\r\n", "\n"), "worker_core.py", "exec")
                if not os.path.isfile(loc_p) and not SELFTEST:
                    try:
                        l_txt = _bootstrap_fetch_text(f"{MANAGER_URL}/locales.js")
                        if len(l_txt) > 500:
                            with open(loc_p, "w", encoding="utf-8", newline="\n") as wf:
                                wf.write(l_txt)
                    except Exception:
                        pass
                return
            except Exception as e:
                if SELFTEST:
                    raise
                log(f"worker_core.py is damaged ({e}) - downloading it from Manager...")
        elif SELFTEST:
            raise RuntimeError("worker_core.py is missing")
        try:
            log(f"Downloading shared core worker_core.py from Manager ({MANAGER_URL})...")
            c_txt = _bootstrap_fetch_core()
            if len(c_txt) > 1000:
                compile(c_txt, "worker_core.py", "exec")
                tmp_c = CORE_FILE_PATH + ".tmp"
                with open(tmp_c, "w", encoding="utf-8", newline="\n") as wf:
                    wf.write(c_txt)
                os.replace(tmp_c, CORE_FILE_PATH)
                try:
                    l_txt = _bootstrap_fetch_text(f"{MANAGER_URL}/locales.js")
                    if len(l_txt) > 500:
                        with open(loc_p, "w", encoding="utf-8", newline="\n") as wf:
                            wf.write(l_txt)
                except Exception:
                    pass
                log("Module worker_core.py verified (SHA-256) and saved.")
                return
            log("Manager did not provide a valid worker_core.py yet.")
        except Exception as e:
            disc = _bootstrap_udp_discover()
            if disc and disc.rstrip("/") != MANAGER_URL:
                MANAGER_URL = disc.rstrip("/")
                log(f"UDP Discovery found Manager at new address: {MANAGER_URL}")
            else:
                log(f"Waiting for Manager ({e})...")
        time.sleep(5)


ensure_worker_core_module()
import worker_core as wc

if not SELFTEST:
    # Vlastní klíč workeru (/data/worker_identity.json). Až PO importu worker_core (dev.27 to volal dřív → pád při startu).
    # Chyba tady nesmí nikdy zastavit worker – bez klíče worker běží dál jako dřív.
    try:
        wc.init_worker_identity(WORKER_DATA_DIR, WORKER_NAME, os.getenv("HB_JOIN_TOKEN", ""))
        wc.approval_log_fn = lambda m: log(m)
    except Exception as _e_ident:
        print(f"[WARN] Worker identity init failed: {_e_ident}", flush=True)
    # Navázání na Manager (/data/manager_binding.json). HB_MANAGER_REBIND=1 vazbu při startu zapomene.
    try:
        wc.manager_bind_log_fn = lambda m: log(m)
        _rebind = str(os.getenv("HB_MANAGER_REBIND", "")).strip().lower() in ("1", "true", "yes", "on")
        if wc.init_manager_binding(WORKER_DATA_DIR, rebind=_rebind):
            log(wc.T('w_mgr_bind_loaded', wc.bound_manager_id()))
        elif _rebind:
            log(wc.T('w_mgr_bind_rebind_env'))
    except Exception as _e_bind:
        print(f"[WARN] Manager binding init failed: {_e_bind}", flush=True)
    # Složka Temp: důkladně se uklízí jen složka se značkou HB; vlastní /temp s cizími soubory → podsložka HB_Temp.
    try:
        _tmp_ready = wc.prepare_worker_temp_dir(LOCAL_TEMP_DIR, (DEFAULT_HB_TEMP_DIR,), log_fn=lambda m: log(m))
        if _tmp_ready:
            LOCAL_TEMP_DIR = _tmp_ready
    except Exception as _e_tmp:
        print(f"[WARN] Temp folder check failed: {_e_tmp}", flush=True)
try:
    import docker_selfupdate as dsu
except Exception:  # běh mimo oficiální Docker obraz bez sdíleného modulu
    dsu = None
if dsu and not SELFTEST:
    try:
        os.remove(dsu.WORKER_READY_FILE)   # O1: značku „naběhl jsem“ zapíše až tento běh
    except OSError:
        pass

GHCR_WORKER_REPO = "ghcr.io/maseklab/hb-worker"


def get_own_channel() -> str:
    """Kanál (:latest / :dev), ze kterého kontejner běží – 1× za hodinu z docker inspect. Kanál se sám
    nikdy nepřepíná (O4); když se liší od Manageru, ukáže to karta workeru."""
    now = time.time()
    if _own_channel_cache["ts"] and now - _own_channel_cache["ts"] < (3600 if _own_channel_cache["value"] else 300):
        return _own_channel_cache["value"]
    env_c = os.getenv("HB_UPDATE_CHANNEL", "").strip().lower()
    ch = env_c if (dsu and env_c in dsu.KNOWN_CHANNELS) else ""
    if not ch and dsu and dsu.docker_sock_available():
        try:
            info = dsu.inspect_own_container(user_agent=f"HB-Cluster-Worker/{CURRENT_VERSION}")
            if info:
                ch = dsu.normalize_channel(dsu.split_image_ref((info.get("Config") or {}).get("Image") or "")[1])
        except Exception:
            ch = ""
    _own_channel_cache.update(value=ch, ts=now)
    return ch   # "" = nejde zjistit (bez docker.sock) – karta workeru pak kanál neporovnává


def get_worker_image() -> str:
    """Obraz workeru ve STEJNÉM kanálu (:latest / :dev), ze kterého tento kontejner běží."""
    env_img = os.getenv("HB_WORKER_IMAGE", "").strip()
    if env_img:
        return env_img
    if dsu:
        try:
            return dsu.channel_image_ref(GHCR_WORKER_REPO, get_own_channel() or "latest")
        except Exception:
            pass
    return GHCR_WORKER_REPO + ":latest"

wc.set_worker_lang(INITIAL_LANG)
if not SELFTEST:
    wc.sync_locales_from_manager(MANAGER_URL)
log_buffer = wc.WorkerLogBuffer(maxlen=250)
if not SELFTEST:
    save_local_worker_config()


def sync_worker_lang(lang_code: str):
    if not lang_code or lang_code not in ("en", "cs"):
        return
    prev = wc.get_worker_lang()
    if prev != lang_code:
        wc.set_worker_lang(lang_code)
        save_local_worker_config()


def try_auto_discover_manager() -> bool:
    global MANAGER_URL
    disc_url, resp = wc.discover_manager_udp(prefer_bound=True)
    if disc_url:
        clean_u = disc_url.rstrip("/")
        bound_id = wc.bound_manager_id()
        resp_mid = str((resp or {}).get("manager_id") or "").strip()
        if bound_id and resp_mid and resp_mid != bound_id:
            log(f"[UDP-DISCOVERY] Ignored foreign Manager at {clean_u} (id={resp_mid[:10]}...)")
            return False
        if resp and resp.get("_all_managers"):
            log(f"[UDP-DISCOVERY] Multiple Managers detected on LAN: {', '.join(resp['_all_managers'])} (selected {clean_u})")
        if resp and resp.get("ui_lang"):
            sync_worker_lang(resp.get("ui_lang"))
        if clean_u != MANAGER_URL:
            # Při existující vazbě nejdřív ověřit identitu přes HTTP, než přepíšeme MANAGER_URL
            if bound_id and not resp_mid:
                try:
                    r_chk = api_request("GET", f"{clean_u}/api/update/version", timeout=3)
                    if r_chk.status_code != 200:
                        return False
                except Exception:
                    return False
            MANAGER_URL = clean_u
            save_local_worker_config()
            log(wc.T('w_udp_discovery_found_manager', MANAGER_URL))
        return True
    if isinstance(resp, dict) and resp.get("_foreign_managers"):
        log(f"[UDP-DISCOVERY] Only foreign Manager(s) responded ({', '.join(resp['_foreign_managers'])}) – keeping bound URL {MANAGER_URL}")
    return False


def get_linux_inputs_dir() -> str:
    return wc.sanitize_path(os.getenv("INPUTS_DIR")) or "/inputs"


def get_linux_outputs_dir() -> str:
    return wc.sanitize_path(os.getenv("OUTPUTS_DIR")) or "/outputs"


CUSTOM_HB_PATH = os.path.join(BIN_DIR, "HandBrakeCLI")
CUSTOM_FFMPEG_PATH = os.path.join(BIN_DIR, "ffmpeg")
CUSTOM_FFPROBE_PATH = os.path.join(BIN_DIR, "ffprobe")
CUSTOM_MKVPROPEDIT_PATH = os.path.join(BIN_DIR, "mkvpropedit")
CUSTOM_MKVMERGE_PATH = os.path.join(BIN_DIR, "mkvmerge")
CUSTOM_7Z_PATH = os.path.join(BIN_DIR, "7zz")

MARKER_HB_MANAGER = os.path.join(BIN_DIR, ".hbcli_from_manager")
MARKER_FFMPEG_MANAGER = os.path.join(BIN_DIR, ".ffmpeg_from_manager")
MARKER_FFPROBE_MANAGER = os.path.join(BIN_DIR, ".ffprobe_from_manager")
MARKER_MKVPROPEDIT_MANAGER = os.path.join(BIN_DIR, ".mkvpropedit_from_manager")
MARKER_MKVMERGE_MANAGER = os.path.join(BIN_DIR, ".mkvmerge_from_manager")
MARKER_7Z_MANAGER = os.path.join(BIN_DIR, ".7z_from_manager")

HANDBRAKE_CLI = "HandBrakeCLI"
FFMPEG_CLI = "ffmpeg"
FFPROBE_CLI = "ffprobe"
MKVPROPEDIT_CLI = None
MKVMERGE_CLI = None
SEVENZIP_CLI = None

HB_BINARY_SOURCE = "System package (apt)"
FFMPEG_BINARY_SOURCE = "System package (apt)"

_bin_probe_cache = {}
active_jobs = {}
held_jobs = {}
jobs_lock = threading.Lock()
held_lock = threading.Lock()
handshake_lock = threading.Lock()
uploading_held_job_id = None
update_blocked_until = 0.0
update_block_reason = ""
# O4: neúspěšné pokusy o aktualizaci (1. → krátká pauza, 2. stejné verze → 6 h; „Aktualizovat teď“ vynuluje)
update_retry_state = {}
update_attempt_clear_at = 0.0
_own_channel_cache = {"value": "", "ts": 0.0}
# Hlídání spojení s Dockerem (docker.sock). Po restartu / aktualizaci Dockeru se zapnutým live-restore
# kontejner dál drží starý, mrtvý socket -> nejde se aktualizovat. Worker to pozná a až nic nedělá,
# ukončí se; Docker ho podle restart policy hned spustí znovu s funkčním socketem.
docker_sock_state = {"ever_ok": False, "restart_policy": "", "fail_since": 0.0, "last_check": 0.0, "last_note": 0.0}
DOCKER_SOCK_STALE_AFTER_S = 180

is_running = True
force_update_check = True
current_power_mode = "AUTO"
current_custom_cores = []
cached_diagnostics = {}
handshake_status = {"inputs_ok": False, "outputs_ok": False, "verified": False}
last_logged_handshake_tuple = None
manager_reachable = True
last_udp_discovery_ts = 0.0
telemetry_state = {"last_sys_total": 0, "last_sys_idle": 0, "last_proc_ticks": {}, "stats": {}}


def get_held_job_ids() -> list:
    with held_lock:
        ids = set(held_jobs.keys())
    if uploading_held_job_id is not None:
        ids.add(uploading_held_job_id)
    return sorted(ids)


def discard_held_jobs_by_ids(cleanup_ids):
    if not cleanup_ids:
        return
    cset = set()
    for x in cleanup_ids:
        try:
            cset.add(int(x))
        except (TypeError, ValueError):
            continue
    if not cset:
        return
    removed = []
    with held_lock:
        for jid in list(cset):
            if jid == uploading_held_job_id:
                continue
            meta = held_jobs.pop(jid, None)
            if meta is not None:
                removed.append((jid, meta.get("job_temp_dir") or os.path.join(LOCAL_TEMP_DIR, f"job_{jid}")))
            else:
                j_tmp = os.path.join(LOCAL_TEMP_DIR, f"job_{jid}")
                if os.path.isdir(j_tmp):
                    removed.append((jid, j_tmp))
    for jid, j_tmp in removed:
        with jobs_lock:
            if jid in active_jobs:
                continue
        shutil.rmtree(j_tmp, ignore_errors=True)


def api_request(method, url, **kwargs):
    # Sdílená requests.Session s výchozím timeoutem (viz worker_core.http_request).
    return wc.http_request(method, url, **kwargs)


def handle_sigterm(*args):
    # Signal handler nesmí brát zámky ani dělat HTTP/disk I/O (hrozil deadlock při přerušení hlavního vlákna).
    global is_running
    is_running = False
    _shutdown_event.set()


def perform_graceful_shutdown():
    """Čisté ukončení z hlavního vlákna po SIGTERM/SIGINT: zabije podprocesy, počká na úklid .tmp a odešle logy."""
    try:
        log(wc.T('w_received_sigterm_sigint_signal'))
    except Exception:
        pass
    with jobs_lock:
        snap = list(active_jobs.values())
    for jd in snap:
        jd["cancelled"] = True
        if jd.get("process"):
            wc.kill_process_tree(jd["process"], wait_s=2.0)
    # Krátce vyčkat (max 5 s), aby vlákna úloh a uploadu doběhla do finally: a smazala částečné .tmp soubory
    t_wait = time.time()
    while time.time() - t_wait < 5.0:
        with jobs_lock:
            has_act = len(active_jobs) > 0
        with held_lock:
            has_up = uploading_held_job_id is not None
        if not has_act and not has_up:
            break
        time.sleep(0.2)
    try:
        wc.flush_held_jobs_metadata(held_jobs, held_lock, LOCAL_TEMP_DIR)
    except Exception:
        pass
    try:
        final_logs = log_buffer.drain(50) if log_buffer else []
        if final_logs:
            wc.http_request(
                "POST", f"{MANAGER_URL}/api/workers/ping",
                json={"worker_name": WORKER_NAME, "version": CURRENT_VERSION, "log_lines": final_logs},
                timeout=2, retries=1
            )
    except Exception:
        pass


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


def perform_storage_handshake(force_log=False):
    global handshake_status, last_logged_handshake_tuple
    if not handshake_lock.acquire(blocking=False):
        return handshake_status.get("inputs_ok") and handshake_status.get("outputs_ok")
    try:
        in_d = get_linux_inputs_dir()
        out_d = get_linux_outputs_dir()
        r_init = api_request("POST", f"{MANAGER_URL}/api/workers/handshake", json={"worker_name": WORKER_NAME, "phase": "init"}, timeout=8)
        if r_init.status_code != 200:
            return False
        id_d = r_init.json()
        tok = id_d.get("token", "")
        m_out = id_d.get("mgr_out_probe_filename", "")
        w_out = id_d.get("wrk_out_probe_filename", "")

        saw_out = False
        if m_out and os.path.isdir(out_d):
            p_out = os.path.join(out_d, m_out)
            for _ in range(3):
                if os.path.isfile(p_out):
                    try:
                        with open(p_out, "r", encoding="utf-8") as f:
                            if f.read().strip() == tok:
                                saw_out = True
                                break
                    except Exception:
                        pass
                time.sleep(0.3)

        if w_out and os.path.isdir(out_d):
            try:
                with open(os.path.join(out_d, w_out), "w", encoding="utf-8") as f:
                    f.write(tok)
            except Exception:
                pass

        saw_in = wc.inputs_match_manager(in_d, id_d)    # jména + velikosti vzorku souborů (Manager do Inputs nic nepíše)

        r_ver = api_request(
            "POST", f"{MANAGER_URL}/api/workers/handshake",
            json={"worker_name": WORKER_NAME, "phase": "verify", "token": tok, "worker_saw_mgr_in": saw_in, "worker_saw_mgr_out": saw_out},
            timeout=8
        )
        if r_ver.status_code == 200:
            vd = r_ver.json()
            in_ok = bool(vd.get("inputs_ok"))
            out_ok = bool(vd.get("outputs_ok"))
            handshake_status = {"inputs_ok": in_ok, "outputs_ok": out_ok, "verified": True}
            tup = (in_ok, out_ok)
            if force_log or tup != last_logged_handshake_tuple:
                last_logged_handshake_tuple = tup
                log(wc.T('w_storage_handshake_manager_inputs', 'OK' if in_ok else 'HTTP RELAY', 'R/W OK' if out_ok else 'HTTP RELAY'))
            return in_ok and out_ok
    except Exception:
        handshake_status = {"inputs_ok": False, "outputs_ok": False, "verified": False}
    finally:
        handshake_lock.release()
    return False


def resolve_worker_source_path(raw_path: str, verified_rel_source: str = "") -> str:
    in_r = get_linux_inputs_dir()
    if verified_rel_source and os.path.isdir(in_r):
        c_p = os.path.normpath(os.path.join(in_r, verified_rel_source.lstrip("\\/")))
        if os.path.exists(c_p):
            return c_p
    if raw_path and os.path.exists(raw_path):
        return os.path.normpath(raw_path)
    norm = str(raw_path or "").replace("\\", "/").strip("/")
    pts = [p for p in norm.split("/") if p and not p.endswith(":")]
    if os.path.isdir(in_r):
        for i in range(len(pts)):
            rc = "/".join(pts[i:])
            fc = os.path.normpath(os.path.join(in_r, rc))
            if os.path.exists(fc):
                return fc
    fb_rel = verified_rel_source or ("/".join(pts[1:]) if len(pts) > 1 else (pts[0] if pts else ""))
    return os.path.normpath(os.path.join(in_r, fb_rel.lstrip("\\/")))


def _get_bin_cache_key(bin_path: str):
    if not bin_path:
        return None
    rp = bin_path if os.path.isabs(bin_path) else (shutil.which(bin_path) or bin_path)
    if os.path.isfile(rp):
        try:
            st = os.stat(rp)
            return (rp, st.st_mtime, st.st_size)
        except Exception:
            return None
    return None


def test_hb_binary(bin_path):
    if not bin_path:
        return False, ""
    ck = _get_bin_cache_key(bin_path)
    if ck and ck in _bin_probe_cache:
        return _bin_probe_cache[ck]
    try:
        res = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=15)
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        if res.returncode == 0 or "HandBrake" in out:
            m = re.search(r"HandBrake\s+([\d\.]+)", out)
            ver = (m.group(1) if m else "OK")
            if ck:
                _bin_probe_cache[ck] = (True, ver)
            return True, ver
    except Exception:
        pass
    if ck:
        for old_k, old_v in _bin_probe_cache.items():
            if old_k[0] == ck[0] and old_v[0]:
                return old_v
    return False, ""


def _normalize_ffmpeg_version(raw_ver: str) -> str:
    s = str(raw_ver or "").strip().lstrip("v")
    if not s:
        return ""
    if s.startswith("N-") or s.lower().startswith(("master", "git-", "autobuild")):
        return s[:14]
    m_st = re.match(r"^n?(\d+\.\d+)(?:\.\d+)?(?:[-+].*)?$", s, re.IGNORECASE)
    return m_st.group(1) if m_st else s[:14]


# Skutečné názvy assetů BtbN/FFmpeg-Builds (release tag "latest"):
#   ffmpeg-n8.0-latest-linux64-gpl-8.0.tar.xz / ffmpeg-n8.0-latest-win64-gpl-8.0.zip
#   (starší / alternativní varianty i bez přípony verze: ffmpeg-n7.1-latest-linux64-gpl.tar.xz)
BTBN_ASSET_RE = re.compile(
    r"^ffmpeg-n(?P<br>\d+\.\d+)(?:\.\d+)?-(?P<tag>latest|\d+-g[0-9a-f]+)-(?P<plat>linux64|win64)-gpl"
    r"(?:-(?P<sfx>\d+\.\d+(?:\.\d+)?))?\.(?P<ext>tar\.xz|zip)$",
    re.IGNORECASE,
)
# Repozitář BtbN/FFmpeg-Builds byl přesunut na FFmpeg-Builds/FFmpeg-Builds (staré URL GitHub přesměrovává).
BTBN_DOWNLOAD_BASE = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/"
# BtbN drží jen 2 nejnovější stabilní větve -> sondy sestupně od nejvyšší možné.
BTBN_FALLBACK_BRANCHES = tuple(f"{mj}.{mn}" for mj in (10, 9, 8, 7) for mn in (2, 1, 0))


def pick_best_btbn_stable_ffmpeg_assets(releases_list) -> dict:
    """Vybere nejvyšší stabilní větev nX.Y (linux64 tar.xz + win64 zip, GPL, bez shared/lgpl/master)."""
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
    best_ver, best_t = "", ()
    for br, slot in branches.items():
        if not (slot.get("lin_url") and slot.get("win_url")):
            continue
        try:
            vt = tuple(int(p) for p in br.split("."))
        except Exception:
            continue
        if vt > best_t:
            best_t, best_ver = vt, br
    if not best_ver:
        return {}
    return {"version": best_ver, "linux_url": branches[best_ver]["lin_url"], "win_url": branches[best_ver]["win_url"]}


def btbn_fallback_candidates():
    """Deterministické URL na /releases/download/latest/ (nejvyšší stabilní větve, nakonec master)."""
    out = []
    for br in BTBN_FALLBACK_BRANCHES:
        out.append((br, f"{BTBN_DOWNLOAD_BASE}ffmpeg-n{br}-latest-linux64-gpl-{br}.tar.xz",
                    f"{BTBN_DOWNLOAD_BASE}ffmpeg-n{br}-latest-win64-gpl-{br}.zip"))
    out.append(("", f"{BTBN_DOWNLOAD_BASE}ffmpeg-master-latest-linux64-gpl.tar.xz",
                f"{BTBN_DOWNLOAD_BASE}ffmpeg-master-latest-win64-gpl.zip"))
    return out


def _url_exists(url: str, hdrs: dict):
    """True = existuje, False = neexistuje (404 apod.), None = síťová chyba (nemá smysl dál sondovat)."""
    try:
        r = requests.head(url, headers=hdrs, allow_redirects=True, timeout=8)
        if r.status_code == 200:
            return True
        if r.status_code in (403, 405):
            r2 = requests.get(url, headers=dict(hdrs, Range="bytes=0-0"), allow_redirects=True, stream=True, timeout=8)
            ok = r2.status_code in (200, 206)
            r2.close()
            return ok
        return False
    except Exception:
        return None


def resolve_stable_ffmpeg_urls():
    """
    Vrací (verze, linux_url, win_url) pro nejvyšší stabilní FFmpeg větev z BtbN/FFmpeg-Builds.
    Nikdy nevyhazuje výjimku: při rate-limitu (403/429) nebo neobvyklé struktuře GitHub API
    použije deterministické URL na /releases/download/latest/ (n8.x / n7.1, nakonec master).
    """
    hdrs = {"User-Agent": f"HB-Cluster-Worker/{CURRENT_VERSION}", "Accept": "application/vnd.github+json"}
    rel_items = []
    for api_url in (
        "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest",
        "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest",
        "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10",
    ):
        try:
            r = requests.get(api_url, headers=hdrs, timeout=15)
            if r.status_code == 200:
                pj = r.json()
                if isinstance(pj, dict):
                    rel_items.append(pj)
                elif isinstance(pj, list):
                    rel_items.extend([x for x in pj if isinstance(x, dict)])
                best = pick_best_btbn_stable_ffmpeg_assets(rel_items)
                if best:
                    return best["version"], best["linux_url"], best["win_url"]
            else:
                log(f"[SYSTEM-BUILD] GitHub API {api_url} -> HTTP {r.status_code} (fallback ready).")
        except Exception as e:
            log(f"[SYSTEM-BUILD] GitHub API {api_url} unavailable: {e}")
    best = pick_best_btbn_stable_ffmpeg_assets(rel_items)
    if best:
        return best["version"], best["linux_url"], best["win_url"]

    cands = btbn_fallback_candidates()
    for ver, lin_u, win_u in cands:
        ex_lin = _url_exists(lin_u, hdrs)
        if ex_lin is None:
            break  # výpadek sítě -> rovnou poslední pojistka
        if ex_lin and _url_exists(win_u, hdrs):
            log(f"[SYSTEM-BUILD] FFmpeg fallback via direct release URL: {ver or 'master'}")
            return ver, lin_u, win_u
    # Poslední pojistka: archivy "master" existují na /releases/download/latest/ vždy
    # (stahování a ověření binárky provede download_file_resilient / test_ff_binary).
    ver, lin_u, win_u = cands[-1]
    log("[SYSTEM-BUILD] FFmpeg stable probes unavailable — using always-present master archives.")
    return ver, lin_u, win_u


def test_ff_binary(bin_path):
    if not bin_path:
        return False, ""
    ck = _get_bin_cache_key(bin_path)
    if ck and ck in _bin_probe_cache:
        return _bin_probe_cache[ck]
    for flg in ("-version", "--version", "i"):
        try:
            res = subprocess.run([bin_path, flg], capture_output=True, text=True, timeout=12)
            out = (res.stdout or "") + "\n" + (res.stderr or "")
            low = out.lower()
            if res.returncode == 0 and any(k in low for k in ("version", "mkvpropedit", "mkvmerge", "7-zip")):
                m_mkv = re.search(r"v(\d+\.\d+(?:\.\d+)?)", out)
                if ("mkvmerge" in low or "mkvpropedit" in low) and m_mkv:
                    ver = m_mkv.group(1)
                    if ck:
                        _bin_probe_cache[ck] = (True, ver)
                    return True, ver
                m_7z = re.search(r"7-zip\s+(?:[\(\[][^\)\]]+[\)\]]\s+)?(\d+\.\d+)", out, re.IGNORECASE)
                if "7-zip" in low and m_7z:
                    ver = m_7z.group(1)
                    if ck:
                        _bin_probe_cache[ck] = (True, ver)
                    return True, ver
                m_v = re.search(r"version\s+([^\s]+)", out, re.IGNORECASE)
                ver = _normalize_ffmpeg_version(m_v.group(1)) if (m_v and ("ffmpeg" in low or "ffprobe" in low)) else (m_v.group(1)[:14] if m_v else "OK")
                if ck:
                    _bin_probe_cache[ck] = (True, ver)
                return True, ver
        except Exception:
            pass
    if ck:
        for old_k, old_v in _bin_probe_cache.items():
            if old_k[0] == ck[0] and old_v[0]:
                return old_v
    return False, ""


def check_and_apply_binary_generation(remote_gen: int, allow_hb_update: bool):
    if not remote_gen:
        return
    with _bin_sync_lock:
        loc_gen = 0
        if os.path.isfile(GEN_MARKER_FILE):
            try:
                with open(GEN_MARKER_FILE, "r", encoding="utf-8") as f:
                    loc_gen = int(f.read().strip() or 0)
            except Exception:
                loc_gen = 0
        if loc_gen != remote_gen and allow_hb_update:
            log(wc.T('w_clean_synchronization_bin_folder_2'))
            for p in (CUSTOM_HB_PATH, CUSTOM_FFMPEG_PATH, CUSTOM_FFPROBE_PATH, CUSTOM_MKVPROPEDIT_PATH, CUSTOM_MKVMERGE_PATH, CUSTOM_7Z_PATH):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            wc.local_md5_cache.clear()
            _bin_probe_cache.clear()
            try:
                with open(GEN_MARKER_FILE, "w", encoding="utf-8") as f:
                    f.write(str(remote_gen))
            except Exception:
                pass


def sync_and_resolve_hb_binary(allow_hb_update=True):
    global HANDBRAKE_CLI, FFMPEG_CLI, FFPROBE_CLI, MKVPROPEDIT_CLI, MKVMERGE_CLI, SEVENZIP_CLI, HB_BINARY_SOURCE, FFMPEG_BINARY_SOURCE
    with _bin_sync_lock:
        os.makedirs(BIN_DIR, exist_ok=True)
        try:
            r_info = api_request("GET", f"{MANAGER_URL}/api/update/hbcli/info", timeout=5)
            if r_info.status_code == 200:
                inf = r_info.json()
                if inf.get("ui_lang"):
                    sync_worker_lang(inf.get("ui_lang"))
                r_gen = int(inf.get("binary_generation") or 1)
                # Jsme uvnitř _bin_sync_lock -> přímo bez rekurzivního zámku
                loc_gen = 0
                if os.path.isfile(GEN_MARKER_FILE):
                    try:
                        with open(GEN_MARKER_FILE, "r", encoding="utf-8") as f:
                            loc_gen = int(f.read().strip() or 0)
                    except Exception:
                        loc_gen = 0
                if r_gen and loc_gen != r_gen and allow_hb_update:
                    log(wc.T('w_clean_synchronization_bin_folder_2'))
                    for p in (CUSTOM_HB_PATH, CUSTOM_FFMPEG_PATH, CUSTOM_FFPROBE_PATH, CUSTOM_MKVPROPEDIT_PATH, CUSTOM_MKVMERGE_PATH, CUSTOM_7Z_PATH):
                        if os.path.isfile(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    wc.local_md5_cache.clear()
                    _bin_probe_cache.clear()
                    try:
                        with open(GEN_MARKER_FILE, "w", encoding="utf-8") as f:
                            f.write(str(r_gen))
                    except Exception:
                        pass

                lin_b = (inf.get("binaries") or {}).get("linux") or {}
                t_map = [
                    ("hbcli", CUSTOM_HB_PATH, MARKER_HB_MANAGER, "HandBrakeCLI"),
                    ("ffmpeg", CUSTOM_FFMPEG_PATH, MARKER_FFMPEG_MANAGER, "ffmpeg"),
                    ("ffprobe", CUSTOM_FFPROBE_PATH, MARKER_FFPROBE_MANAGER, "ffprobe"),
                    ("mkvpropedit", CUSTOM_MKVPROPEDIT_PATH, MARKER_MKVPROPEDIT_MANAGER, "mkvpropedit"),
                    ("mkvmerge", CUSTOM_MKVMERGE_PATH, MARKER_MKVMERGE_MANAGER, "mkvmerge"),
                    ("7z", CUSTOM_7Z_PATH, MARKER_7Z_MANAGER, "7zz"),
                ]
                for tk, c_path, m_path, lbl in t_map:
                    if not allow_hb_update and os.path.isfile(c_path):
                        continue
                    ti = lin_b.get(tk) or {}
                    r_md5 = ti.get("md5")
                    if ti.get("available") and r_md5:
                        if wc.compute_file_md5(c_path) != r_md5:
                            log(wc.T('w_downloading_central_binary_bin', lbl, f"{ti.get('size', 0) / (1024 * 1024):.1f}"))
                            dl = api_request("GET", f"{MANAGER_URL}/api/update/binaries/linux/{tk}", timeout=90)
                            if dl.status_code == 200 and len(dl.content) > 10000:
                                got_md5 = hashlib.md5(dl.content).hexdigest()
                                if got_md5 != r_md5:
                                    log(f"[BIN-SYNC] MD5 mismatch for {lbl}: got {got_md5[:8]}, expected {r_md5[:8]} - skipping.")
                                    continue
                                tmp_b = c_path + ".tmp"
                                with open(tmp_b, "wb") as f:
                                    f.write(dl.content)
                                os.chmod(tmp_b, 0o755)
                                os.replace(tmp_b, c_path)
                                with open(m_path, "w") as mf:
                                    mf.write(r_md5)
                                log(wc.T('w_binary_manager_saved_activated', lbl, c_path))
        except Exception:
            pass

        if os.path.isfile(CUSTOM_HB_PATH):
            os.chmod(CUSTOM_HB_PATH, 0o755)
            ok, _ = test_hb_binary(CUSTOM_HB_PATH)
            HANDBRAKE_CLI = CUSTOM_HB_PATH if ok else (shutil.which("HandBrakeCLI") or "/usr/bin/HandBrakeCLI")
            HB_BINARY_SOURCE = "Manager (bin/)" if ok else "System apt (bin/ failed)"
        else:
            HANDBRAKE_CLI = shutil.which("HandBrakeCLI") or "/usr/bin/HandBrakeCLI"
            HB_BINARY_SOURCE = "System package (apt)"

        if os.path.isfile(CUSTOM_FFMPEG_PATH):
            os.chmod(CUSTOM_FFMPEG_PATH, 0o755)
            ok_ff, _ = test_ff_binary(CUSTOM_FFMPEG_PATH)
            FFMPEG_CLI = CUSTOM_FFMPEG_PATH if ok_ff else shutil.which("ffmpeg")
            FFMPEG_BINARY_SOURCE = "Manager (bin/)" if ok_ff else "System apt"
        else:
            FFMPEG_CLI = shutil.which("ffmpeg")
            FFMPEG_BINARY_SOURCE = "System package (apt)" if FFMPEG_CLI else "MISSING!"

        FFPROBE_CLI = CUSTOM_FFPROBE_PATH if (os.path.isfile(CUSTOM_FFPROBE_PATH) and test_ff_binary(CUSTOM_FFPROBE_PATH)[0]) else shutil.which("ffprobe")
        MKVPROPEDIT_CLI = CUSTOM_MKVPROPEDIT_PATH if (os.path.isfile(CUSTOM_MKVPROPEDIT_PATH) and test_ff_binary(CUSTOM_MKVPROPEDIT_PATH)[0]) else shutil.which("mkvpropedit")
        MKVMERGE_CLI = CUSTOM_MKVMERGE_PATH if (os.path.isfile(CUSTOM_MKVMERGE_PATH) and test_ff_binary(CUSTOM_MKVMERGE_PATH)[0]) else shutil.which("mkvmerge")
        SEVENZIP_CLI = CUSTOM_7Z_PATH if (os.path.isfile(CUSTOM_7Z_PATH) and test_ff_binary(CUSTOM_7Z_PATH)[0]) else (shutil.which("7zz") or shutil.which("7z") or shutil.which("7za"))
        return HANDBRAKE_CLI


def extract_x265_version(hb_path):
    if not hb_path or not os.path.exists(hb_path):
        return "N/A"
    ck = ("x265",) + (_get_bin_cache_key(hb_path) or (hb_path,))
    if ck in _bin_probe_cache:
        return _bin_probe_cache[ck]
    try:
        with open(hb_path, "rb") as f:
            rb = f.read()
        ms = re.search(rb"HEVC encoder version (\d+\.\d+)", rb)
        if ms:
            ver = ms.group(1).decode("utf-8", errors="ignore")
            _bin_probe_cache[ck] = ver
            return ver
    except Exception:
        pass
    _bin_probe_cache[ck] = "4.1"
    return "4.1"


def _read_cgroup_cpu_quota_cores():
    """Přečte Docker/cgroup CPU limit (--cpus / cpu.max / cpu.cfs_quota_us), pokud je nastaven."""
    try:
        # cgroup v2
        if os.path.isfile("/sys/fs/cgroup/cpu.max"):
            with open("/sys/fs/cgroup/cpu.max", "r", encoding="utf-8") as f:
                parts = (f.read() or "").strip().split()
            if len(parts) >= 2 and parts[0] != "max":
                q, per = int(parts[0]), int(parts[1])
                if q > 0 and per > 0:
                    return max(1, int(round(q / float(per))))
    except Exception:
        pass
    try:
        # cgroup v1
        qf, pf = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        if os.path.isfile(qf) and os.path.isfile(pf):
            q = int(open(qf, "r", encoding="utf-8").read().strip())
            per = int(open(pf, "r", encoding="utf-8").read().strip())
            if q > 0 and per > 0:
                return max(1, int(round(q / float(per))))
    except Exception:
        pass
    return 0


def get_effective_cpu_cores():
    aff_n = len(INITIAL_ALLOWED_CPUS) if INITIAL_ALLOWED_CPUS else (os.cpu_count() or 1)
    cg_n = _read_cgroup_cpu_quota_cores()
    if cg_n > 0:
        return max(1, min(aff_n, cg_n))
    return max(1, aff_n)


def get_cpu_model_name():
    try:
        with open("/proc/cpuinfo", "r") as f:
            for ln in f:
                if "model name" in ln:
                    return ln.split(":", 1)[1].strip()
    except Exception:
        pass
    return "Linux CPU"


_host_os_cache = {"value": "", "ts": 0.0}


def get_docker_host_os() -> str:
    """Systém serveru, na kterém běží kontejner (přes docker.sock /info), např. „Debian 13“. Obnova 1× za hodinu."""
    now = time.time()
    if _host_os_cache["value"] and now - _host_os_cache["ts"] < 3600:
        return _host_os_cache["value"]
    val = ""
    try:
        if dsu and dsu.docker_sock_responding(timeout=3.0):
            st, info = dsu.docker_api_request("GET", "/info", timeout=5.0)
            if int(st) == 200 and isinstance(info, dict):
                raw = str(info.get("OperatingSystem") or "").strip()
                val = re.sub(r"\s*GNU/Linux", "", raw)
                val = re.sub(r"\s*\([^)]*\)\s*$", "", val).strip()
    except Exception:
        val = ""
    if val:
        _host_os_cache.update(value=val, ts=now)
    return val or _host_os_cache["value"]


_linux_caps_cache = {"value": None, "ts": 0.0}


def get_linux_caps() -> dict:
    """Co kontejner umí / neumí – Manager podle toho zašedne volby a vysvětlí, co nastavit (obnova 1× za minutu).
    docker_sock: ok | stale (připojený, ale neodpovídá) | missing; restart_policy: no | always | unless-stopped | on-failure | ""."""
    now = time.time()
    if _linux_caps_cache["value"] is not None and now - _linux_caps_cache["ts"] < 60:
        return dict(_linux_caps_cache["value"])
    sock = "missing"
    try:
        if dsu and dsu.docker_sock_available():
            sock = "ok" if dsu.docker_sock_responding(timeout=3.0) else "stale"
    except Exception:
        sock = "stale"
    rp = str(docker_sock_state.get("restart_policy") or "")
    if sock == "ok" and not rp:
        try:
            info = dsu.inspect_own_container(user_agent=f"HB-Cluster-Worker/{CURRENT_VERSION}") or {}
            rp = str(((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no")
            docker_sock_state["restart_policy"] = rp
        except Exception:
            pass
    host_t = int(os.cpu_count() or 0)
    allowed_t = len(INITIAL_ALLOWED_CPUS) or host_t
    caps = {
        "docker_sock": sock,
        "restart_policy": rp,
        "cpus_allowed": allowed_t,
        "cpus_host": host_t,
        "update_blocked": update_block_reason if update_blocked_until > now else "",
    }
    _linux_caps_cache.update(value=caps, ts=now)
    return dict(caps)


def collect_diagnostics():
    global cached_diagnostics
    hb_p = HANDBRAKE_CLI if os.path.isabs(HANDBRAKE_CLI) else (shutil.which(HANDBRAKE_CLI) or HANDBRAKE_CLI)
    ok, hb_v = test_hb_binary(hb_p)
    if not ok:
        hb_v = "MISSING"
    x265_v = extract_x265_version(hb_p) if ok else "N/A"

    ff_p = FFMPEG_CLI or shutil.which("ffmpeg")
    fp_p = FFPROBE_CLI or shutil.which("ffprobe")
    mp_p = MKVPROPEDIT_CLI or shutil.which("mkvpropedit")
    mm_p = MKVMERGE_CLI or shutil.which("mkvmerge")
    sz_p = SEVENZIP_CLI or shutil.which("7zz") or shutil.which("7z")

    ok_ff, ff_v = test_ff_binary(ff_p)
    ok_mm, mkv_v = test_ff_binary(mm_p)
    ok_sz, sz_v = test_ff_binary(sz_p)

    in_d = get_linux_inputs_dir()
    out_d = get_linux_outputs_dir()
    in_ok = os.path.exists(in_d)
    out_ok = os.path.exists(out_d) and os.access(out_d, os.W_OK)
    t_free = 0.0
    try:
        os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
        _, _, fb = shutil.disk_usage(LOCAL_TEMP_DIR)
        t_free = round(fb / (1024**3), 1)
    except Exception:
        pass

    topo = wc.detect_cpu_topology() or {}
    cached_diagnostics = {
        "os": "Linux (Docker)",
        "host_os": get_docker_host_os(),
        "net": wc.get_worker_network_info(MANAGER_URL),
        "cpu_model": get_cpu_model_name(),
        "is_hybrid_cpu": bool(topo.get("is_hybrid")),
        "e_core_count": int(topo.get("e_core_count") or 0),
        "cpu_topology": topo,
        "custom_cores": list(current_custom_cores),
        "hb_version": hb_v,
        "hb_source": HB_BINARY_SOURCE,
        "hb_path": hb_p,
        "hb_md5": wc.compute_file_md5(CUSTOM_HB_PATH) or wc.compute_file_md5(hb_p),
        "x265_version": x265_v,
        "ffmpeg_ok": bool(ff_p and ok_ff),
        "ffmpeg_version": ff_v,
        "ffmpeg_source": FFMPEG_BINARY_SOURCE,
        "ffmpeg_md5": wc.compute_file_md5(CUSTOM_FFMPEG_PATH) or wc.compute_file_md5(ff_p),
        "ffprobe_ok": fp_p is not None,
        "ffprobe_md5": wc.compute_file_md5(CUSTOM_FFPROBE_PATH) or wc.compute_file_md5(fp_p),
        "mkvpropedit_ok": mp_p is not None,
        "mkvpropedit_md5": wc.compute_file_md5(CUSTOM_MKVPROPEDIT_PATH) or wc.compute_file_md5(mp_p),
        "mkvmerge_ok": mm_p is not None,
        "mkvmerge_md5": wc.compute_file_md5(CUSTOM_MKVMERGE_PATH) or wc.compute_file_md5(mm_p),
        "mkv_version": mkv_v if ok_mm else "",
        "sevenzip_ok": bool(sz_p and ok_sz),
        "sevenzip_version": sz_v if ok_sz else "",
        "sevenzip_md5": wc.compute_file_md5(CUSTOM_7Z_PATH) or wc.compute_file_md5(sz_p),
        "inputs_ok": in_ok,
        "outputs_ok": out_ok,
        "nas_dl_ok": in_ok,
        "nas_out_ok": out_ok,
        "temp_free_gb": t_free
    }
    return cached_diagnostics


_telemetry_lock = threading.Lock()


def sample_telemetry():
    global telemetry_state
    with _telemetry_lock:
        now_t = time.time()
        last_ts = float(telemetry_state.get("last_sample_ts") or 0.0)
        if (now_t - last_ts) < 1.5 and telemetry_state.get("stats"):
            return telemetry_state["stats"]

        c_eff = get_effective_cpu_cores()
        c_tot = os.cpu_count() or c_eff

        s_tot, s_idl = 0, 0
        try:
            with open("/proc/stat", "r") as f:
                cols = [int(x) for x in f.readline().split()[1:]]
                s_tot = sum(cols)
                s_idl = cols[3] + (cols[4] if len(cols) > 4 else 0)
        except Exception:
            pass

        with jobs_lock:
            root_pids = [d["process"].pid for d in active_jobs.values() if d.get("process") and d["process"].poll() is None]

        all_tree_pids = set()
        for rpid in root_pids:
            for cpid in wc.get_process_tree_pids(rpid):
                all_tree_pids.add(cpid)

        p_ticks, hb_ram = {}, 0
        for pid in all_tree_pids:
            try:
                with open(f"/proc/{pid}/stat", "r") as f:
                    st = f.read()
                    flds = st[st.rfind(")") + 2:].split()
                    p_ticks[pid] = int(flds[11]) + int(flds[12])
                with open(f"/proc/{pid}/status", "r") as f:
                    for ln in f:
                        if ln.startswith("VmRSS:"):
                            hb_ram += int(ln.split()[1]) * 1024
                            break
            except Exception:
                pass

        cpu_t = float(telemetry_state.get("last_cpu_tot") or 0.0)
        cpu_hb = float(telemetry_state.get("last_cpu_hb") or 0.0)
        d_sys = s_tot - telemetry_state["last_sys_total"]
        d_idl = s_idl - telemetry_state["last_sys_idle"]

        if telemetry_state["last_sys_total"] == 0:
            cpu_t, cpu_hb = 0.0, 0.0
            telemetry_state["last_sys_total"] = s_tot
            telemetry_state["last_sys_idle"] = s_idl
            telemetry_state["last_proc_ticks"] = p_ticks
        elif d_sys > 0:
            cpu_t = max(0.0, min(100.0, (1.0 - (d_idl / float(d_sys))) * 100.0))
            d_p = sum(
                (tks - telemetry_state["last_proc_ticks"].get(pid, 0))
                for pid, tks in p_ticks.items()
                if telemetry_state["last_proc_ticks"].get(pid, 0) > 0 and tks >= telemetry_state["last_proc_ticks"].get(pid, 0)
            )
            cpu_hb = max(0.0, min(100.0, (d_p / float(d_sys)) * 100.0))
            telemetry_state["last_sys_total"] = s_tot
            telemetry_state["last_sys_idle"] = s_idl
            telemetry_state["last_proc_ticks"] = p_ticks

        telemetry_state["last_cpu_tot"] = cpu_t
        telemetry_state["last_cpu_hb"] = cpu_hb

        r_tot, r_usd = 0.0, 0.0
        try:
            mm = {}
            with open("/proc/meminfo", "r") as f:
                for ln in f:
                    ps = ln.split(":")
                    if len(ps) == 2:
                        mm[ps[0].strip()] = int(ps[1].strip().split()[0])
            tkb = mm.get("MemTotal", 0)
            akb = mm.get("MemAvailable", mm.get("MemFree", 0))
            r_tot = round(tkb / (1024**2), 1)
            r_usd = round((tkb - akb) / (1024**2), 1)
        except Exception:
            pass

        if not cached_diagnostics:
            collect_diagnostics()
        else:
            try:
                _, _, fb = shutil.disk_usage(LOCAL_TEMP_DIR)
                cached_diagnostics["temp_free_gb"] = round(fb / (1024**3), 1)
            except Exception:
                pass

        eco_t = max(1, c_eff - 2) if c_eff >= 4 else c_eff
        hyb = wc.detect_cpu_topology()
        try:
            cached_diagnostics["linux_caps"] = get_linux_caps()
        except Exception:
            pass
        try:
            # O4: stav aktualizace pro kartu workeru (kanál, proč a do kdy čeká po neúspěchu)
            cached_diagnostics["ota"] = wc.update_retry_view(update_retry_state, get_own_channel() if dsu else "")
        except Exception:
            pass
        eff_pm = resolve_linux_power_mode(current_power_mode, hyb)
        if eff_pm == "CUSTOM" and current_custom_cores:
            inner = f"CUSTOM ({len(current_custom_cores)}/{c_eff}t)"
        elif eff_pm == "HYBRID_ECO":
            inner = f"HYBRID ECO [{hyb.get('e_core_count', eco_t)}/{c_eff}t E-cores]"
        elif eff_pm == "ECO":
            inner = f"ECO ({eco_t}/{c_eff}t)"
        else:
            inner = f"FULL ({c_eff}/{c_eff}t)"
        p_label = f"NIGHT ({inner})" if current_power_mode == "NIGHT" else inner
        st_out = {
            "cpu_total": round(cpu_t, 1),
            "cpu_hb": round(cpu_hb, 1),
            "cores": c_eff,
            "cores_total": c_tot,
            "ram_used_gb": r_usd,
            "ram_total_gb": r_tot,
            "ram_hb_gb": round(hb_ram / (1024**3), 2),
            "power_mode_active": p_label,
            "diag": cached_diagnostics
        }
        telemetry_state["last_sample_ts"] = now_t
        telemetry_state["stats"] = st_out
        return st_out


def resolve_linux_power_mode(mode=None, hyb=None):
    """Skutečný režim pro procesy (stejná pravidla jako Windows worker):
    AUTO → FULL (server bez uživatele), NIGHT → 22:00–06:00 FULL, jinak ECO (na hybridních CPU jen E-cores),
    HYBRID_ECO bez E-cores → ECO. Čas = časové pásmo kontejneru (TZ)."""
    m = str(mode or current_power_mode or "AUTO").strip().upper()
    if hyb is None:
        hyb = wc.detect_cpu_topology()
    is_hyb = bool((hyb or {}).get("is_hybrid"))
    if m == "AUTO":
        return "FULL"
    if m == "NIGHT":
        hr_now = datetime.datetime.now().hour
        if hr_now >= 22 or hr_now < 6:
            return "FULL"
        return "HYBRID_ECO" if is_hyb else "ECO"
    if m == "HYBRID_ECO" and not is_hyb:
        return "ECO"
    return m if m in ("FULL", "ECO", "HYBRID_ECO", "CUSTOM") else "FULL"


def apply_linux_power_mode(mode, custom_cores=None):
    global current_power_mode, current_custom_cores
    norm_m = str(mode or "AUTO").strip().upper()
    if norm_m not in ("AUTO", "FULL", "ECO", "NIGHT", "HYBRID_ECO", "CUSTOM"):
        norm_m = "AUTO"
    current_power_mode = norm_m
    if custom_cores is not None:
        current_custom_cores = wc.normalize_custom_cores(custom_cores, get_effective_cpu_cores())
        if cached_diagnostics:
            cached_diagnostics["custom_cores"] = list(current_custom_cores)
    hyb = wc.detect_cpu_topology()
    eff_mode = resolve_linux_power_mode(norm_m, hyb)
    with jobs_lock:
        root_pids = [d["process"].pid for d in active_jobs.values() if d.get("process") and d["process"].poll() is None]
    for rpid in root_pids:
        wc.apply_eco_affinity_to_process_tree(rpid, enable_eco=eff_mode, initial_allowed_cpus=INITIAL_ALLOWED_CPUS, hybrid_info=hyb, custom_cores=current_custom_cores)


def is_job_cancelled(job_id):
    with jobs_lock:
        jd = active_jobs.get(job_id)
        return bool(not jd or jd.get("cancelled"))


def is_job_user_paused(job_id) -> bool:
    with jobs_lock:
        jd = active_jobs.get(job_id)
        return bool(jd and jd.get("user_paused"))


def check_job_status(job_id):
    if is_job_cancelled(job_id):
        return "CANCELLED"
    try:
        r = api_request("GET", f"{MANAGER_URL}/api/jobs/{job_id}/status", params={"worker_name": WORKER_NAME}, timeout=5)
        if r.status_code == 200:
            sd = r.json()
            if sd.get("ui_lang"):
                sync_worker_lang(sd.get("ui_lang"))
            st = sd.get("status")
            with jobs_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]["user_paused"] = bool(sd.get("is_paused", False))
                    if st in ["CANCELLED", "REVOKED"]:
                        active_jobs[job_id]["cancelled"] = True
            return st
    except Exception:
        pass
    return "IN_PROGRESS"


def extract_iso_playlist_for_premux(job_id: int, iso_path: str, mpls_name: str, job_temp_dir: str, expected_bytes: int = 0):
    sz_bin = SEVENZIP_CLI or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if not sz_bin or not mpls_name:
        return "", 0, []

    ext_d = os.path.join(job_temp_dir, "ISO_EXTRACT")
    shutil.rmtree(ext_d, ignore_errors=True)
    os.makedirs(ext_d, exist_ok=True)

    m_clean = mpls_name.strip().upper()
    if not m_clean.endswith(".MPLS"):
        m_clean = f"{int(m_clean):05d}.MPLS" if m_clean.isdigit() else ""
    if not m_clean:
        return "", 0, []

    m_cmd = [
        sz_bin, "x", "-y", iso_path, f"-o{ext_d}",
        "-ir!*PLAYLIST/*.mpls", "-ir!*PLAYLIST/*.MPLS",
        "-ir!*CLIPINF/*.clpi", "-ir!*CLIPINF/*.CLPI",
        "-ir!*BDMV/*.bdmv", "-ir!*BDMV/*.BDMV"
    ]
    subprocess.run(m_cmd, capture_output=True, text=True, timeout=60)

    ext_mpls_p = wc.resolve_bdmv_mpls_path(ext_d, m_clean, require_stream_dir=False)

    clips = []
    if ext_mpls_p and os.path.isfile(ext_mpls_p):
        try:
            with open(ext_mpls_p, "rb") as mf:
                md = mf.read()
            wc.save_premux_mpls_metadata(job_temp_dir, md, m_clean)
            clips, _ = wc.parse_mpls_header_info(md)
        except Exception:
            pass

    pl_exact_b, u_clips, mb_iso = wc.parse_bluray_iso_mpls_details(iso_path, m_clean, min_valid_bytes=1024 * 1024, sevenzip_bin=sz_bin)
    if mb_iso and not (ext_mpls_p and os.path.isfile(ext_mpls_p)):
        wc.save_premux_mpls_metadata(job_temp_dir, mb_iso, m_clean)
    if not clips:
        clips = u_clips

    if not clips:
        shutil.rmtree(ext_d, ignore_errors=True)
        return "", 0, []

    unique_clips = sorted(set(clips))
    inc_args = []
    for cid in unique_clips:
        inc_args.extend([f"-ir!*STREAM/{cid}.m2ts", f"-ir!*STREAM/{cid}.M2TS", f"-ir!*{cid}.m2ts", f"-ir!*{cid}.M2TS"])

    target_b = pl_exact_b if pl_exact_b > 0 else expected_bytes
    log(wc.T('w_id_system_pre_mux', job_id, os.path.basename(iso_path), m_clean, len(clips)))
    s_cmd = [sz_bin, "x", "-y", iso_path, f"-o{ext_d}"] + inc_args
    proc = subprocess.Popen(s_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    with jobs_lock:
        if job_id in active_jobs:
            active_jobs[job_id]["process"] = proc
            active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_1_2')
            if target_b > 0:
                active_jobs[job_id]["src_size_bytes"] = target_b

    last_7z_log = [0.0, -1]

    def _is_canc(rem_chk: bool) -> bool:
        if not is_running or is_job_cancelled(job_id):
            return True
        if rem_chk and check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
            return True
        return False

    def _on_7z_prog(pct, spd, cur_b, tot_b, elap_s, now_t):
        with jobs_lock:
            if job_id in active_jobs:
                active_jobs[job_id]["progress"] = round(pct, 1)
                active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                active_jobs[job_id]["transferred_bytes"] = cur_b
                if tot_b > 0:
                    active_jobs[job_id]["src_size_bytes"] = tot_b
                active_jobs[job_id]["elapsed_sec"] = elap_s
                active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_1_2')
        if int(pct) >= last_7z_log[1] + 10 and now_t - last_7z_log[0] > 8.0:
            log(wc.T('w_pre_mux_1_2_2', job_id, f'{pct:.1f}', f'{spd:.1f}', f'{cur_b / 1024 ** 3:.1f}'))
            last_7z_log[0] = now_t
            last_7z_log[1] = int(pct)

    wc.run_7z_extract_with_disk_progress(proc, ext_d, unique_clips, target_b, _is_canc, _on_7z_prog)

    clip_file_map = {}
    for rd, _, fns in os.walk(ext_d):
        for f in fns:
            fu = f.upper()
            if fu.endswith(".M2TS") and fu[:5] in unique_clips:
                fp = os.path.join(rd, f)
                sz = os.path.getsize(fp)
                if sz > 65536:
                    clip_file_map[fu[:5]] = fp

    ordered_clip_files = [clip_file_map[cid] for cid in clips if cid in clip_file_map]
    ext_b = sum(os.path.getsize(fp) for fp in ordered_clip_files if os.path.isfile(fp))

    ref_pl_bytes = pl_exact_b if pl_exact_b > 0 else 0
    if len(ordered_clip_files) < len(clips) or (ref_pl_bytes > 0 and ext_b < ref_pl_bytes * 0.90):
        log(wc.T('w_id_iso_extraction_integrity', job_id, len(ordered_clip_files), len(clips), f'{ext_b / 1024 ** 3:.2f}'))
        shutil.rmtree(ext_d, ignore_errors=True)
        return "", 0, []

    verified_mpls_p = wc.resolve_bdmv_mpls_path(ext_d, m_clean, require_stream_dir=True) or ext_mpls_p
    return (verified_mpls_p if (verified_mpls_p and os.path.isfile(verified_mpls_p)) else ""), ext_b, ordered_clip_files


def _run_mkvmerge_process_with_progress(job_id: int, mm_cmd: list, p_out: str, expected_bytes: int) -> int:
    mm_p = subprocess.Popen(mm_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8", errors="replace")
    with jobs_lock:
        if job_id in active_jobs:
            active_jobs[job_id]["process"] = mm_p
            active_jobs[job_id]["progress"] = 0.0
            active_jobs[job_id]["transferred_bytes"] = 0
            active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_2_2')

    last_mm_log = [0.0, -1]

    def _is_canc(rem_chk: bool) -> bool:
        if not is_running or is_job_cancelled(job_id):
            return True
        if rem_chk and check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
            return True
        return False

    def _on_mm_prog(pct, spd, cur_b, tot_b, elap_s, now_t):
        with jobs_lock:
            if job_id in active_jobs:
                active_jobs[job_id]["progress"] = round(pct, 1)
                active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                active_jobs[job_id]["transferred_bytes"] = cur_b
                if tot_b > 0:
                    active_jobs[job_id]["src_size_bytes"] = tot_b
                active_jobs[job_id]["elapsed_sec"] = elap_s
                active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_2_2')
        if int(pct) >= last_mm_log[1] + 10 and now_t - last_mm_log[0] > 8.0:
            log(wc.T('w_pre_mux_2_2_2', job_id, f'{pct:.1f}', f'{spd:.1f}', f'{cur_b / 1024 ** 3:.1f}'))
            last_mm_log[0] = now_t
            last_mm_log[1] = int(pct)

    return wc.run_mkvmerge_with_disk_progress(mm_p, p_out, expected_bytes, _is_canc, _on_mm_prog)


def run_local_mkvmerge_premux(job_id: int, orig_source: str, mpls_name: str, job_temp_dir: str, expected_src_bytes: int = 0, job_dur_s: float = 0.0, allow_raw_m2ts_fallback: bool = True) -> str:
    mm_b = MKVMERGE_CLI or shutil.which("mkvmerge")
    if not mm_b:
        return ""

    m_title = os.path.basename(orig_source.rstrip("\\/"))
    try:
        api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/premux", timeout=5)
    except Exception:
        pass

    t_in = None
    playlist_clips_bytes = 0
    extracted_clips = []
    iso_clean_d = os.path.join(job_temp_dir, "ISO_EXTRACT")

    if os.path.isfile(orig_source) and orig_source.lower().endswith(".iso") and mpls_name:
        t_in, playlist_clips_bytes, extracted_clips = extract_iso_playlist_for_premux(job_id, orig_source, mpls_name, job_temp_dir, expected_bytes=expected_src_bytes)
    elif os.path.isdir(orig_source):
        if not mpls_name:
            det_m, _, _, _ = wc.find_matching_mpls_in_bdmv(orig_source, target_duration_sec=job_dur_s)
            if det_m:
                mpls_name = det_m
                log(wc.T('w_id_automatically_resolved_matching', job_id, mpls_name))
        if mpls_name:
            t_in = wc.resolve_bdmv_mpls_path(orig_source, mpls_name, require_stream_dir=True) or wc.resolve_bdmv_mpls_path(orig_source, mpls_name, require_stream_dir=False)
            if t_in and os.path.isfile(t_in):
                try:
                    with open(t_in, "rb") as mf:
                        mb = mf.read()
                    wc.save_premux_mpls_metadata(job_temp_dir, mb, mpls_name.strip().upper())
                    clips, _ = wc.parse_mpls_header_info(mb)
                    sd = os.path.join(os.path.dirname(os.path.dirname(t_in)), "STREAM")
                    if clips and os.path.isdir(sd):
                        for cid in clips:
                            for ec in (".m2ts", ".M2TS"):
                                cfp = os.path.join(sd, cid + ec)
                                if os.path.isfile(cfp):
                                    playlist_clips_bytes += os.path.getsize(cfp)
                                    extracted_clips.append(cfp)
                                    break
                except Exception:
                    pass

    if extracted_clips:
        try:
            largest_clip = max(extracted_clips, key=lambda p: os.path.getsize(p) if os.path.isfile(p) else 0)
            vp_m2ts, ap_m2ts = wc.probe_media_bitrates(largest_clip, ffprobe_bin=FFPROBE_CLI)
            if len(extracted_clips) > 1 and job_dur_s > 60.0 and playlist_clips_bytes > 100 * 1024 * 1024:
                vp_m2ts = wc.reconcile_disc_video_bitrate(playlist_clips_bytes, job_dur_s, vp_m2ts, ap_m2ts)
            with jobs_lock:
                if job_id in active_jobs:
                    if playlist_clips_bytes > 0:
                        active_jobs[job_id]["src_size_bytes"] = playlist_clips_bytes
                    if vp_m2ts > 0 and vp_m2ts != 2500:
                        active_jobs[job_id]["v_bitrate_in_kbps"] = vp_m2ts
                    if ap_m2ts > 0:
                        active_jobs[job_id]["a_bitrate_in_kbps"] = ap_m2ts
        except Exception:
            pass

    if not t_in or not os.path.isfile(t_in):
        shutil.rmtree(iso_clean_d, ignore_errors=True)
        return ""

    ref_b = playlist_clips_bytes if playlist_clips_bytes > 0 else expected_src_bytes
    p_out = os.path.join(job_temp_dir, "premux_source.mkv")
    log(wc.T('w_id_system_pre_mux_2', job_id, m_title, os.path.basename(t_in), len(extracted_clips) or 1))
    rc = _run_mkvmerge_process_with_progress(job_id, [mm_b, "--gui-mode", "-o", p_out, t_in], p_out, ref_b)

    if is_job_cancelled(job_id):
        shutil.rmtree(iso_clean_d, ignore_errors=True)
        raise InterruptedError("Cancelled")

    if rc not in (0, 1) and extracted_clips and allow_raw_m2ts_fallback:
        try:
            if os.path.isfile(p_out):
                os.remove(p_out)
        except Exception:
            pass
        free_gb_pre = wc.get_disk_free_gb(os.path.dirname(p_out))
        if free_gb_pre >= 0 and free_gb_pre < 2.0:
            log(f"ID {job_id}: Pre-Mux raw M2TS fallback skipped (low temp disk space: {free_gb_pre:.1f} GB)")
        else:
            log(wc.T('w_id_system_pre_mux_3', job_id, m_title, rc, len(extracted_clips)))
            direct_cmd = [mm_b, "--gui-mode", "-o", p_out, extracted_clips[0]]
            for extra_c in extracted_clips[1:]:
                direct_cmd.extend(["+", extra_c])
            rc = _run_mkvmerge_process_with_progress(job_id, direct_cmd, p_out, ref_b)

    shutil.rmtree(iso_clean_d, ignore_errors=True)

    if is_job_cancelled(job_id):
        raise InterruptedError("Cancelled")

    if rc in (0, 1) and os.path.isfile(p_out):
        osz = os.path.getsize(p_out)
        if osz > 5 * 1024 * 1024 and (ref_b <= 0 or osz >= ref_b * 0.70):
            log(wc.T('w_id_system_pre_mux_4', job_id, m_title, f'{osz / 1024 ** 3:.2f}'))
            return p_out
        log(wc.T('w_id_pre_mux_output', job_id, f'{osz / 1024 ** 3:.2f}', f'{ref_b / 1024 ** 3:.2f}'))
        try:
            os.remove(p_out)
        except Exception:
            pass
        return ""

    try:
        if os.path.isfile(p_out):
            os.remove(p_out)
    except Exception:
        pass
    log(wc.T('w_id_mkvmerge_pre_mux', job_id, rc))
    return ""


def heartbeat_loop():
    global is_running, force_update_check
    last_hs_retry = 0.0
    while is_running:
        time.sleep(5)
        if not is_running:
            break
        st_data = sample_telemetry()
        new_logs = log_buffer.drain(50) if log_buffer else []
        with jobs_lock:
            snap = list(active_jobs.items())
        if not snap:
            # Rychlé opakování handshake (každých 20 s) při nedostupném úložišti (parita s Windows)
            if not (handshake_status.get("inputs_ok") and handshake_status.get("outputs_ok")) and (time.time() - last_hs_retry) >= 20.0:
                last_hs_retry = time.time()
                perform_storage_handshake(force_log=False)
            st_ok = bool(handshake_status.get("inputs_ok") and handshake_status.get("outputs_ok")) or is_http_relay_active()
            try:
                rp = api_request(
                    "POST", f"{MANAGER_URL}/api/workers/ping",
                    json={"worker_name": WORKER_NAME, "version": CURRENT_VERSION, "telemetry": st_data, "log_lines": new_logs, "held_job_ids": get_held_job_ids(), "storage_ok": st_ok},
                    timeout=5
                )
                if rp.status_code == 200:
                    rd = rp.json()
                    if rd.get("cleanup_held_job_ids"):
                        discard_held_jobs_by_ids(rd.get("cleanup_held_job_ids"))
                    if rd.get("ui_lang"):
                        sync_worker_lang(rd.get("ui_lang"))
                    if rd.get("update_now"):
                        request_update_now()
                    if rd.get("update_required") and time.time() >= update_blocked_until:
                        force_update_check = True
                    pm = rd.get("power_mode")
                    cc = rd.get("custom_cores")
                    if (pm and pm != current_power_mode) or (cc is not None and wc.normalize_custom_cores(cc, get_effective_cpu_cores()) != current_custom_cores):
                        apply_linux_power_mode(pm or current_power_mode, cc)
                    rg = int(rd.get("binary_generation") or 0)
                    if rg:
                        check_and_apply_binary_generation(rg, True)
            except Exception:
                pass
            continue

        for idx_s, (jid, jd) in enumerate(snap):
            try:
                r = api_request(
                    "POST", f"{MANAGER_URL}/api/jobs/{jid}/progress",
                    json={
                        "progress": jd["progress"],
                        "fps_current": jd.get("fps_current", 0.0),
                        "fps_avg": jd.get("fps_avg", 0.0),
                        "speed_mbps": jd.get("speed_mbps", 0.0),
                        "elapsed_sec": jd.get("elapsed_sec", 0),
                        "eta_sec": jd.get("eta_sec", 0),
                        "transferred_bytes": jd.get("transferred_bytes", 0),
                        "src_size_bytes": jd.get("src_size_bytes", 0),
                        "is_net_fallback": jd.get("is_net_fallback", False),
                        "net_fallback_reason": jd.get("net_fallback_reason", ""),
                        "v_bitrate_in_kbps": jd.get("v_bitrate_in_kbps", 0),
                        "a_bitrate_in_kbps": jd.get("a_bitrate_in_kbps", 0),
                        "premux_applied": jd.get("premux_applied", False),
                        "premux_status": jd.get("premux_status", ""),
                        "step_message": jd.get("step_message"),
                        "updating_tools": jd.get("updating_tools"),
                        "worker_name": WORKER_NAME,
                        "version": CURRENT_VERSION,
                        "telemetry": st_data,
                        "log_lines": new_logs if idx_s == 0 else []
                    }, timeout=5
                )
                if r.status_code == 200:
                    rd = r.json()
                    if rd.get("ui_lang"):
                        sync_worker_lang(rd.get("ui_lang"))
                    if rd.get("update_now"):
                        request_update_now()
                    if rd.get("update_required") and time.time() >= update_blocked_until:
                        force_update_check = True
                    if (rd.get("power_mode") and rd.get("power_mode") != current_power_mode) or (rd.get("custom_cores") is not None and wc.normalize_custom_cores(rd.get("custom_cores"), get_effective_cpu_cores()) != current_custom_cores):
                        apply_linux_power_mode(rd.get("power_mode") or current_power_mode, rd.get("custom_cores"))
                    jd["user_paused"] = bool(rd.get("is_paused", False))
                    st = rd.get("status")
                    if st in ["CANCELLED", "REVOKED"]:
                        log(wc.T('w_id_received_cancellation_command', jid, st))
                        if st == "REVOKED" and rd.get("reason"):
                            log(wc.T('w_id_job_taken_over', jid, rd.get("reason")))
                        jd["cancelled"] = True
                        force_update_check = True
                        if jd.get("process"):
                            wc.kill_process_tree(jd["process"])
            except Exception:
                pass


def cleanup_temp_dir(on_startup: bool = False):
    with jobs_lock:
        act_ids = set(active_jobs.keys())
    with held_lock:
        h_ids = set(held_jobs.keys())
        up_id = uploading_held_job_id

    def _is_still_live(jid, is_sb):
        with jobs_lock:
            if jid in active_jobs:
                return True
        if not is_sb:
            with held_lock:
                if jid in held_jobs or jid == uploading_held_job_id:
                    return True
        return False

    wc.cleanup_orphaned_job_temp_dirs(
        LOCAL_TEMP_DIR, act_ids, h_ids, up_id,
        live_check_fn=_is_still_live,
        min_age_sec=0.0 if on_startup else 30.0
    )


def held_jobs_upload_loop():
    global uploading_held_job_id
    last_slot_try = {}

    while is_running:
        time.sleep(5)
        if not is_running or _updating_or_restarting:
            continue
        with held_lock:
            h_items = list(held_jobs.items())
        if not h_items:
            continue

        for jid, meta in h_items:
            if not is_running or _updating_or_restarting:
                break
            j_tmp = meta.get("job_temp_dir") or os.path.join(LOCAL_TEMP_DIR, f"job_{jid}")
            w_dest = meta.get("w_dest") or ""
            orig_d = meta.get("orig_d") or ""
            tmp_orig_d = orig_d + ".tmp"
            o_sz = int(meta.get("out_size_bytes") or wc.get_file_size(w_dest))
            use_relay_out = bool(meta.get("use_relay_output", False))
            relay_sub = str(meta.get("relay_subfolder") or "")

            if not w_dest or not os.path.isfile(w_dest):
                log(wc.T('w_held_file_missing_error', jid, w_dest or j_tmp))
                try:
                    api_request(
                        "POST", f"{MANAGER_URL}/api/jobs/{jid}/error",
                        json={"error_type": "GENERIC", "error_code": "ERR_HELD_OUTPUT_LOST", "error_detail": str(w_dest or j_tmp)[-200:], "version": CURRENT_VERSION},
                        timeout=5
                    )
                except Exception:
                    pass
                with held_lock:
                    held_jobs.pop(jid, None)
                shutil.rmtree(j_tmp, ignore_errors=True)
                continue

            force_up = False
            try:
                rs = api_request("GET", f"{MANAGER_URL}/api/jobs/{jid}/status", params={"worker_name": WORKER_NAME, "held_check": "true"}, timeout=5)
                if rs.status_code == 200:
                    sd = rs.json()
                    if sd.get("ui_lang"):
                        sync_worker_lang(sd.get("ui_lang"))
                    st = sd.get("status")
                    w_own = sd.get("worker_name")
                    force_up = bool(sd.get("force_upload"))
                    if st in ("WAITING", "ERROR", "IN_PROGRESS", "STAGING", "PREMUX") and (not w_own or w_own == WORKER_NAME):
                        # WAITING/ERROR: úloha spadla zpět do fronty po restartu Manageru; IN_PROGRESS/STAGING/PREMUX: hlášení
                        # „hotovo, čeká na místo“ se k Manageru nedostalo (výpadek sítě / restart) -> obnovit WAITING_DISK,
                        # hotové video nikdy nemazat. Pokud worker úlohu právě znovu převádí, nedělat nic.
                        with jobs_lock:
                            busy_here = jid in active_jobs
                        if busy_here:
                            continue
                        try:
                            api_request(
                                "POST", f"{MANAGER_URL}/api/jobs/{jid}/waiting_disk",
                                json={"worker_name": WORKER_NAME, "out_size_bytes": o_sz, "reason": "Re-registered held finished video after Manager reconnect"},
                                timeout=5
                            )
                        except Exception:
                            pass
                        continue
                    if st not in ("WAITING_DISK", "COPYING") or (w_own and w_own != WORKER_NAME):
                        log(wc.T('w_id_waiting_disk_job', jid, st))
                        with held_lock:
                            held_jobs.pop(jid, None)
                        shutil.rmtree(j_tmp, ignore_errors=True)
                        continue
            except Exception:
                continue

            now_t = time.time()
            if not force_up and (now_t - last_slot_try.get(jid, 0)) < 25.0:
                continue
            last_slot_try[jid] = now_t

            try:
                ru = api_request(
                    "POST", f"{MANAGER_URL}/api/jobs/{jid}/request_upload",
                    json={"worker_name": WORKER_NAME, "out_size_bytes": o_sz, "force": force_up},
                    timeout=8
                )
                if ru.status_code != 200 or not ru.json().get("allowed"):
                    continue
            except Exception:
                continue

            with held_lock:
                if _updating_or_restarting or not is_running:
                    break
                uploading_held_job_id = jid
            log(wc.T('w_id_storage_guard_target', jid, os.path.basename(orig_d), f'{o_sz / 1024 ** 3:.2f}'))
            remote_cancelled = [False]
            try:
                last_p_log = [0.0]

                def _chk_c(rem_chk):
                    if not is_running:
                        return True
                    if rem_chk:
                        try:
                            rc = api_request("GET", f"{MANAGER_URL}/api/jobs/{jid}/status", params={"worker_name": WORKER_NAME, "held_check": "true"}, timeout=5)
                            if rc.status_code == 200:
                                rj = rc.json()
                                r_own = rj.get("worker_name")
                                if rj.get("status") not in ("WAITING_DISK", "COPYING") or (r_own and r_own != WORKER_NAME):
                                    remote_cancelled[0] = True
                                    return True
                        except Exception:
                            pass
                    return False

                def _on_p(prog, spd, copied_b, elap_s, nt):
                    if nt - last_p_log[0] > 4.0:
                        try:
                            api_request(
                                "POST", f"{MANAGER_URL}/api/jobs/{jid}/progress",
                                json={
                                    "progress": prog, "fps_current": 0.0, "fps_avg": 0.0,
                                    "speed_mbps": round(spd, 2), "transferred_bytes": copied_b,
                                    "src_size_bytes": o_sz, "elapsed_sec": elap_s,
                                    "worker_name": WORKER_NAME, "version": CURRENT_VERSION
                                }, timeout=5
                            )
                        except Exception:
                            pass
                        last_p_log[0] = nt

                out_r_dir = get_linux_outputs_dir()
                if use_relay_out or not (os.path.exists(out_r_dir) and os.access(out_r_dir, os.W_OK)):
                    if not meta.get("relay_uploaded"):
                        rel_res = wc.relay_upload_output_to_manager(
                            MANAGER_URL, jid, WORKER_NAME, w_dest,
                            os.path.basename(orig_d), relay_sub, _chk_c, _on_p
                        )
                        if rel_res.get("final_dest_path"):
                            meta["complete_payload"]["final_dest_path"] = rel_res["final_dest_path"]
                        meta["relay_uploaded"] = True
                        wc.save_held_job_meta(j_tmp, meta)
                else:
                    o_refuse = wc.output_target_refusal(orig_d, "", get_linux_inputs_dir())
                    if o_refuse:
                        raise RuntimeError(o_refuse)
                    own_final = str(meta.get("final_d") or "")
                    if own_final and os.path.isfile(own_final) and wc.get_file_size(own_final) == o_sz:
                        final_d = own_final            # doručeno už při minulém pokusu (pád před potvrzením)
                    else:
                        os.makedirs(os.path.dirname(orig_d), exist_ok=True)
                        wc.copy_path_with_progress(w_dest, tmp_orig_d, o_sz, _chk_c, _on_p)
                        final_d = wc.place_output_file(tmp_orig_d, own_final or orig_d, jid, own=bool(own_final))
                    if final_d != orig_d:
                        log(wc.T('w_out_saved_new_name', jid, os.path.basename(orig_d), os.path.basename(final_d)))
                        meta["complete_payload"]["final_dest_path"] = final_d
                    meta["final_d"] = final_d
                    wc.save_held_job_meta(j_tmp, meta)

                rc = api_request("POST", f"{MANAGER_URL}/api/jobs/{jid}/complete", json=meta["complete_payload"], timeout=10)
                if rc.status_code == 200:
                    log(wc.T('w_id_storage_guard_held', jid))
                    wc.remove_held_job_meta(j_tmp)
                    with held_lock:
                        held_jobs.pop(jid, None)
                    shutil.rmtree(j_tmp, ignore_errors=True)
                elif wc.is_own_manager_refusal(rc):
                    log(f"ID {jid}: Manager rejected held /complete (HTTP {rc.status_code}) - dropping held job.")
                    wc.remove_held_job_meta(j_tmp)
                    with held_lock:
                        held_jobs.pop(jid, None)
                    shutil.rmtree(j_tmp, ignore_errors=True)
                else:
                    log(f"ID {jid}: Held /complete returned HTTP {rc.status_code} - keeping held for retry.")
            except InterruptedError:
                try:
                    if os.path.exists(tmp_orig_d):
                        os.remove(tmp_orig_d)
                except Exception:
                    pass
                # NIKDY nemazat hotové video v j_tmp při místním vypnutí/SIGTERM – smazat JEN při zrušení Managerem!
                if remote_cancelled[0]:
                    with held_lock:
                        held_jobs.pop(jid, None)
                    shutil.rmtree(j_tmp, ignore_errors=True)
            except Exception as e:
                log(wc.T('w_id_storage_guard_target_3', jid, e))
                try:
                    if os.path.exists(tmp_orig_d):
                        os.remove(tmp_orig_d)
                except Exception:
                    pass
                try:
                    api_request(
                        "POST", f"{MANAGER_URL}/api/jobs/{jid}/waiting_disk",
                        json={"worker_name": WORKER_NAME, "out_size_bytes": o_sz, "reason": f"Upload error: {e}"},
                        timeout=5
                    )
                except Exception:
                    pass
            finally:
                with held_lock:
                    uploading_held_job_id = None
                with jobs_lock:
                    idle_now = (len(active_jobs) == 0)
                if idle_now and is_running:
                    cleanup_temp_dir()
def _filter_missing_apt_packages(packages: list) -> list:
    """Vrátí pouze balíčky z `packages`, které ještě nejsou nainstalované (`install ok installed`)."""
    missing = []
    seen = set()
    for pkg in packages:
        p = str(pkg or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            chk = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", p],
                capture_output=True, text=True, timeout=5
            )
            if chk.returncode == 0 and "install ok installed" in (chk.stdout or ""):
                continue
        except Exception:
            pass
        missing.append(p)
    return missing


def run_apt_nonblocking(packages: list, timeout_sec: int = 300, force_upgrade: bool = False):
    if force_upgrade:
        to_install = list(packages or [])
        log(f"[SYSTEM-BUILD] Upgrading/installing apt packages ({len(to_install)}): {' '.join(to_install)}")
    else:
        to_install = _filter_missing_apt_packages(packages)
        if not to_install:
            log(f"[SYSTEM-BUILD] All {len(packages)} required packages already installed - skipping apt.")
            return
        log(f"[SYSTEM-BUILD] Installing missing apt packages ({len(to_install)}): {' '.join(to_install)}")
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    opts = [
        "-y", "--no-install-recommends",
        "-o", "Dpkg::Options::=--force-confdef",
        "-o", "Dpkg::Options::=--force-confold",
        "-o", "Acquire::ForceIPv4=true",
        "-o", "Acquire::http::Timeout=15",
        "-o", "Acquire::https::Timeout=15",
        "-o", "Acquire::Retries=2"
    ]
    r_upd = subprocess.run(["apt-get", "update"] + opts, env=env, capture_output=True, text=True, timeout=120)
    if r_upd.returncode != 0:
        log(f"[SYSTEM-BUILD] apt-get update warning (rc={r_upd.returncode}): {(r_upd.stderr or '').strip()[:240]}")
    r_ins = subprocess.run(["apt-get", "install"] + opts + to_install, env=env, capture_output=True, text=True, timeout=timeout_sec)
    if r_ins.returncode != 0:
        log(f"[SYSTEM-BUILD] apt-get install warning (rc={r_ins.returncode}): {(r_ins.stderr or '').strip()[:240]}")


def download_file_resilient(url: str, dest_path: str, timeout_sec: int = 180):
    c_bin = shutil.which("curl")
    if c_bin:
        cmd = [
            c_bin, "-4", "-L", "--fail", "--silent", "--show-error",
            "--connect-timeout", "15", "--max-time", str(timeout_sec),
            "--retry", "3", "--retry-delay", "2",
            "-A", f"HB-Cluster-Worker/{CURRENT_VERSION}", "-o", dest_path, url
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 15)
        if res.returncode == 0 and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 1000:
            return
    with requests.get(url, headers={"User-Agent": f"HB-Cluster-Worker/{CURRENT_VERSION}"}, stream=True, timeout=(15, timeout_sec)) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for blk in r.iter_content(chunk_size=1024 * 1024):
                if blk:
                    f.write(blk)


def upload_built_binary_to_manager(os_type: str, tool_key: str, file_path: str, version_str: str = "") -> bool:
    if not file_path or not os.path.isfile(file_path) or os.path.getsize(file_path) < 10000:
        log(wc.T('w_system_build_error_file', file_path, os_type, tool_key))
        return False
    mb = os.path.getsize(file_path) / (1024 * 1024)
    log(wc.T('w_system_build_uploading_v', os_type, tool_key, version_str or '?', f'{mb:.1f}'))
    try:
        with open(file_path, "rb") as f:
            r = wc.http_request(
                "POST",
                f"{MANAGER_URL}/api/v2/binaries/upload/{os_type}/{tool_key}",
                retries=1,
                data={"version": version_str or "", "is_custom": "false"},
                files={"file": (os.path.basename(file_path), f, "application/octet-stream")},
                headers={"Connection": "close"},
                timeout=(10, 300)
            )
        return r.status_code == 200
    except Exception as e:
        log(f"[SYSTEM-BUILD] Upload error {os_type}/{tool_key}: {e}")
        return False


def setup_official_mkvtoolnix_apt_repo():
    try:
        d_id, cname = "debian", "bookworm"
        if os.path.isfile("/etc/os-release"):
            with open("/etc/os-release", "r", encoding="utf-8") as f:
                for ln in f:
                    if ln.startswith("ID="):
                        d_id = ln.split("=", 1)[1].strip().strip('"').lower()
                    elif ln.startswith("VERSION_CODENAME="):
                        cname = ln.split("=", 1)[1].strip().strip('"').lower()
        if d_id not in ("debian", "ubuntu"):
            d_id, cname = "debian", "bookworm"

        os.makedirs("/etc/apt/keyrings", exist_ok=True)
        gpg_p = "/etc/apt/keyrings/gpg-pub-moritzbunkus.gpg"
        download_file_resilient("https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg", gpg_p, timeout_sec=30)
        os.chmod(gpg_p, 0o644)

        arch_m = (platform.machine() or "").lower()
        deb_arch = "arm64" if arch_m in ("aarch64", "arm64") else "amd64"
        with open("/etc/apt/sources.list.d/mkvtoolnix.download.list", "w", encoding="utf-8") as rf:
            rf.write(f"deb [arch={deb_arch} signed-by={gpg_p}] https://mkvtoolnix.download/{d_id}/ {cname} main\n")
        run_apt_nonblocking(["mkvtoolnix"], timeout_sec=180, force_upgrade=True)
    except Exception as e:
        log(f"[SYSTEM-BUILD] MKVToolNix repo notice: {e}")
        run_apt_nonblocking(["mkvtoolnix"], timeout_sec=180, force_upgrade=True)


def update_system_job_step(job_id: int, start_time: float, progress: float, step_msg: str, updating_tools: list = None, console_line: str = None):
    if is_job_cancelled(job_id):
        raise InterruptedError("Cancelled")
    log(f"[SYSTEM-BUILD] ({progress:.0f}%) {step_msg}")
    elap = int(time.time() - start_time)
    with jobs_lock:
        if job_id in active_jobs:
            active_jobs[job_id]["progress"] = progress
            active_jobs[job_id]["elapsed_sec"] = elap
            active_jobs[job_id]["step_message"] = step_msg
            if updating_tools is not None:
                active_jobs[job_id]["updating_tools"] = updating_tools
    try:
        api_request(
            "POST", f"{MANAGER_URL}/api/v2/binaries/build_progress",
            json={
                "worker_name": WORKER_NAME,
                "job_id": job_id,
                "status": "BUILDING",
                "progress": progress,
                "step_message": step_msg,
                "updating_tools": updating_tools,
                "console_line": console_line
            },
            timeout=8
        )
    except Exception:
        pass


# Podepisování HBWorker.exe probíhá výhradně na Manageru (klíč nikdy neopouští Manager).
# Linux builder nahrává nepodepsaný build a Manager ho při uložení podepíše.


def fetch_hbw_icon(dst_path: str) -> bool:
    """Ikona HBW z Manageru (/static/hbw.ico) – použije se jako ikona HBWorker.exe (resource ICON)."""
    try:
        r = api_request("GET", f"{MANAGER_URL}/static/hbw.ico", timeout=15)
        if r.status_code == 200 and len(r.content) > 1000 and r.content[:4] == b"\x00\x00\x01\x00":
            with open(dst_path, "wb") as f:
                f.write(r.content)
            return True
    except Exception:
        pass
    return False


def process_system_build_job_thread(job_id: int, payload: dict):
    global force_update_check
    t0 = time.time()
    b_dir = os.path.join(LOCAL_TEMP_DIR, f"job_{job_id}_sysbuild")
    raw_tgts = payload.get("target_tools") or payload.get("targets") or ["all"]
    tgts = set(raw_tgts)
    force_all = bool(payload.get("force_all") or (payload.get("force_rebuild") and "all" in tgts))
    uptodate_set = set(payload.get("uptodate_tools") or [])

    def _should_build_tool(t_key: str, disp_name: str) -> bool:
        if force_all:
            return True
        ok = (t_key not in uptodate_set) if ("all" in tgts) else (t_key in tgts)
        if not ok:
            log(wc.T("w_sysbuild_tool_skip", disp_name))
        return ok

    do_7z = _should_build_tool("7z", "7-Zip")
    do_mkv = _should_build_tool("mkvtoolnix", "MKVToolNix")
    do_ff = _should_build_tool("ffmpeg", "FFmpeg & FFprobe")
    do_hb = _should_build_tool("hbcli", "HandBrakeCLI")
    do_launcher = _should_build_tool("launcher", "HBWorker.exe")

    _ver_re = re.compile(r"^\d+(?:\.\d+){1,3}$")
    def _clean_build_ver(raw, fallback, allow_latest=False):
        s = str(raw or "").strip().lstrip("v")
        if allow_latest and s in ("", "latest"):
            return "latest"
        if not s:
            return fallback
        if not _ver_re.match(s):
            raise ValueError(f"Unsafe tool version string rejected: {s[:40]!r}")
        return s

    try:
        hb_v = _clean_build_ver(payload.get("hb_version"), "1.11.2")
        mkv_v = _clean_build_ver(payload.get("mkvtoolnix_version"), "103.0")
        ff_v = _clean_build_ver(payload.get("ffmpeg_version"), "latest", allow_latest=True)
        sz_v = _clean_build_ver(payload.get("sevenzip_version"), "26.06")
    except ValueError as ve:
        log(f"[SECURITY] {ve}")
        try:
            wc.http_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/error", json={"error_type": "BUILD_INVALID_VERSION", "error_detail": str(ve), "worker_name": WORKER_NAME, "version": CURRENT_VERSION}, timeout=10)
        except Exception:
            pass
        return

    with jobs_lock:
        prev_sb = active_jobs.get(job_id) or {}
        active_jobs[job_id] = {
            "progress": 1.0, "fps_current": 0.0, "fps_avg": 0.0, "speed_mbps": 0.0,
            "elapsed_sec": 0, "eta_sec": 0, "transferred_bytes": 0, "src_size_bytes": 0,
            "is_net_fallback": False, "net_fallback_reason": "", "v_bitrate_in_kbps": 0, "a_bitrate_in_kbps": 0,
            "premux_applied": False, "premux_status": "",
            "is_system_build": True,
            "step_message": wc.T('w_initializing_build_environment'),
            "updating_tools": None, "process": None,
            "cancelled": bool(prev_sb.get("cancelled", False)),
            "user_paused": bool(prev_sb.get("user_paused", False)),
        }
    if is_job_cancelled(job_id):
        with jobs_lock:
            active_jobs.pop(job_id, None)
        return

    try:
        shutil.rmtree(b_dir, ignore_errors=True)
        os.makedirs(b_dir, exist_ok=True)
        build_errors = []

        def _upload_or_note(os_t: str, tk_k: str, fp: str, ver_s: str) -> bool:
            if upload_built_binary_to_manager(os_t, tk_k, fp, ver_s):
                return True
            build_errors.append(f"{os_t}/{tk_k} upload failed")
            return False

        req_pkgs = []
        if do_ff or do_mkv or do_hb:
            req_pkgs.extend(["ca-certificates", "curl", "wget", "p7zip-full", "unzip", "xz-utils"])
        if do_mkv or do_hb:
            req_pkgs.extend([
                "gnupg", "patchelf", "binutils", "build-essential",
                "python3-pip", "python3-venv", "scons"
            ])
        if do_launcher:
            launcher_bins = ("x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-windres", "osslsigncode", "openssl")
            if not all(shutil.which(b) for b in launcher_bins):
                req_pkgs.extend(["gcc-mingw-w64-x86-64", "osslsigncode", "openssl"])
            else:
                log("[SYSTEM-BUILD] Preinstalled MinGW/signing tools detected for HBWorker.exe - skipping apt.")

        if req_pkgs:
            update_system_job_step(
                job_id, t0, 4.0,
                wc.T('w_step_1_5_installing'),
                None
            )
            run_apt_nonblocking(req_pkgs, timeout_sec=300)

        v_dir = "/tmp/hb_staticx_venv"
        sx_bin = os.path.join(v_dir, "bin", "staticx")
        if (do_mkv or do_hb) and not os.path.isfile(sx_bin):
            subprocess.run(["python3", "-m", "venv", v_dir], capture_output=True, timeout=60)
            subprocess.run([os.path.join(v_dir, "bin", "pip"), "install", "--upgrade", "pip", "wheel", "scons", "staticx"], capture_output=True, timeout=180)

        if do_7z:
            update_system_job_step(
                job_id, t0, 12.0,
                wc.T('w_step_2_5_downloading', sz_v),
                None
            )
            sz_nd = sz_v.replace(".", "")
            sz_arch = "arm64" if (platform.machine() or "").lower() in ("aarch64", "arm64") else "x64"
            l_tar = os.path.join(b_dir, "7z_lin.tar.xz")
            try:
                download_file_resilient(f"https://github.com/ip7z/7zip/releases/download/{sz_v}/7z{sz_nd}-linux-{sz_arch}.tar.xz", l_tar, timeout_sec=90)
                found_7zz = False
                with tarfile.open(l_tar, "r:xz") as tf:
                    for m in tf.getmembers():
                        if os.path.basename(m.name) == "7zz" and m.isfile():
                            o_7zz = os.path.join(b_dir, "7zz")
                            with tf.extractfile(m) as sf, open(o_7zz, "wb") as df:
                                shutil.copyfileobj(sf, df)
                            os.chmod(o_7zz, 0o755)
                            _upload_or_note("linux", "7z", o_7zz, sz_v)
                            found_7zz = True
                            break
                if not found_7zz:
                    build_errors.append("linux/7z missing in archive")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Linux 7-Zip warning: {e}")
                build_errors.append(f"linux/7z: {e}")

            w_sfx = os.path.join(b_dir, f"7z{sz_nd}-x64.exe")
            e_dir = os.path.join(b_dir, "win_7z_ext")
            os.makedirs(e_dir, exist_ok=True)
            sz_t = os.path.join(b_dir, "7zz") if os.path.isfile(os.path.join(b_dir, "7zz")) else "7z"
            try:
                download_file_resilient(f"https://github.com/ip7z/7zip/releases/download/{sz_v}/7z{sz_nd}-x64.exe", w_sfx, timeout_sec=90)
                subprocess.run([sz_t, "e", "-y", w_sfx, f"-o{e_dir}", "7z.exe", "7z.dll"], capture_output=True, timeout=60)
                c_exe = os.path.join(e_dir, "7z.exe")
                c_dll = os.path.join(e_dir, "7z.dll")
                if os.path.isfile(c_exe) and os.path.isfile(c_dll):
                    _upload_or_note("windows", "7z", c_exe, sz_v)
                    _upload_or_note("windows", "7z_dll", c_dll, sz_v)
                else:
                    build_errors.append("windows/7z extract missing 7z.exe or 7z.dll")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Windows 7z.exe + 7z.dll warning: {e}")
                build_errors.append(f"windows/7z: {e}")

        if do_mkv:
            update_system_job_step(
                job_id, t0, 24.0,
                wc.T('w_step_3_5_installing'),
                None
            )
            setup_official_mkvtoolnix_apt_repo()
            act_mkv = mkv_v
            for tk, sp in [("mkvmerge", "/usr/bin/mkvmerge"), ("mkvpropedit", "/usr/bin/mkvpropedit")]:
                if os.path.isfile(sp) and os.path.isfile(sx_bin):
                    _, dv = test_ff_binary(sp)
                    if dv and dv != "OK":
                        act_mkv = dv
                    o_sx = os.path.join(b_dir, f"{tk}_static")
                    subprocess.run([sx_bin, "--strip", sp, o_sx], capture_output=True, timeout=120)
                    if os.path.isfile(o_sx):
                        os.chmod(o_sx, 0o755)
                        _upload_or_note("linux", tk, o_sx, act_mkv)
                    else:
                        build_errors.append(f"linux/{tk} staticx failed")
                else:
                    build_errors.append(f"linux/{tk} binary or staticx missing")

            win_mkv = mkv_v or act_mkv
            update_system_job_step(
                job_id, t0, 34.0,
                wc.T('w_step_3_5_downloading', win_mkv),
                None
            )
            w_m7z = os.path.join(b_dir, "mkv_win.7z")
            try:
                download_file_resilient(f"https://mkvtoolnix.download/windows/releases/{win_mkv}/mkvtoolnix-64-bit-{win_mkv}.7z", w_m7z, timeout_sec=180)
                ed = os.path.join(b_dir, "mkv_win_ext")
                os.makedirs(ed, exist_ok=True)
                sz_t = os.path.join(b_dir, "7zz") if os.path.isfile(os.path.join(b_dir, "7zz")) else "7z"
                subprocess.run([sz_t, "e", "-y", w_m7z, f"-o{ed}", "*/mkvmerge.exe", "*/mkvpropedit.exe", "mkvmerge.exe", "mkvpropedit.exe"], capture_output=True, timeout=90)
                for en, tk in [("mkvmerge.exe", "mkvmerge"), ("mkvpropedit.exe", "mkvpropedit")]:
                    cp = os.path.join(ed, en)
                    if os.path.isfile(cp):
                        _upload_or_note("windows", tk, cp, win_mkv)
                    else:
                        build_errors.append(f"windows/{tk} missing in archive")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Windows MKVToolNix v{win_mkv} warning: {e}")
                build_errors.append(f"windows/mkvtoolnix: {e}")

        if do_ff:
            update_system_job_step(
                job_id, t0, 45.0,
                wc.T('w_step_4_5_downloading'),
                None
            )
            ff_rel_ver, ff_lin_url, ff_win_url = resolve_stable_ffmpeg_urls()
            log(f"[SYSTEM-BUILD] FFmpeg source: v{ff_rel_ver or 'master'} -> {ff_lin_url}")
            det_ff = ff_rel_ver or ff_v
            l_ftar = os.path.join(b_dir, "ffmpeg_linux.tar.xz")
            try:
                download_file_resilient(ff_lin_url, l_ftar, timeout_sec=180)
                lin_ff_seen = set()
                with tarfile.open(l_ftar, "r:xz") as tf:
                    for m in tf.getmembers():
                        m_br = re.search(r"ffmpeg-n(\d+\.\d+)", m.name)
                        if m_br and not ff_rel_ver:
                            det_ff = m_br.group(1)
                        bn = os.path.basename(m.name)
                        if bn in ("ffmpeg", "ffprobe") and m.isfile():
                            ep = os.path.join(b_dir, f"{bn}_lin")
                            with tf.extractfile(m) as sf, open(ep, "wb") as df:
                                shutil.copyfileobj(sf, df)
                            os.chmod(ep, 0o755)
                            _, dfv = test_ff_binary(ep)
                            if dfv and dfv != "OK" and not ff_rel_ver:
                                det_ff = _normalize_ffmpeg_version(dfv)
                            _upload_or_note("linux", bn, ep, det_ff)
                            lin_ff_seen.add(bn)
                for req_b in ("ffmpeg", "ffprobe"):
                    if req_b not in lin_ff_seen:
                        build_errors.append(f"linux/{req_b} missing in archive")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Linux FFmpeg stable v{det_ff} warning: {e}")
                build_errors.append(f"linux/ffmpeg: {e}")

            update_system_job_step(
                job_id, t0, 55.0,
                wc.T('w_step_4_5_downloading_2'),
                None
            )
            w_fzip = os.path.join(b_dir, "ff_win.zip")
            try:
                download_file_resilient(ff_win_url, w_fzip, timeout_sec=180)
                win_ff_seen = set()
                with zipfile.ZipFile(w_fzip, "r") as zf:
                    for nm in zf.namelist():
                        m_br = re.search(r"ffmpeg-n(\d+\.\d+)", nm)
                        if m_br and not ff_rel_ver and not det_ff:
                            det_ff = m_br.group(1)
                        bn = os.path.basename(nm).lower()
                        if bn in ("ffmpeg.exe", "ffprobe.exe"):
                            tk = "ffmpeg" if bn == "ffmpeg.exe" else "ffprobe"
                            oe = os.path.join(b_dir, bn)
                            with zf.open(nm) as zin, open(oe, "wb") as fout:
                                shutil.copyfileobj(zin, fout)
                            _upload_or_note("windows", tk, oe, det_ff)
                            win_ff_seen.add(tk)
                for req_b in ("ffmpeg", "ffprobe"):
                    if req_b not in win_ff_seen:
                        build_errors.append(f"windows/{req_b} missing in archive")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Windows FFmpeg stable v{det_ff} warning: {e}")
                build_errors.append(f"windows/ffmpeg: {e}")

        if do_hb:
            update_system_job_step(
                job_id, t0, 62.0,
                wc.T('w_step_5_5_downloading', hb_v),
                None
            )
            w_hzip = os.path.join(b_dir, "hb_win.zip")
            try:
                download_file_resilient(f"https://github.com/HandBrake/HandBrake/releases/download/{hb_v}/HandBrakeCLI-{hb_v}-win-x86_64.zip", w_hzip, timeout_sec=180)
                found_whb = False
                with zipfile.ZipFile(w_hzip, "r") as zf:
                    for nm in zf.namelist():
                        if nm.lower().endswith("handbrakecli.exe"):
                            oe = os.path.join(b_dir, "HandBrakeCLI.exe")
                            with zf.open(nm) as zin, open(oe, "wb") as fout:
                                shutil.copyfileobj(zin, fout)
                            _upload_or_note("windows", "hbcli", oe, hb_v)
                            found_whb = True
                            break
                if not found_whb:
                    build_errors.append("windows/hbcli missing in zip")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Windows HandBrakeCLI warning: {e}")
                build_errors.append(f"windows/hbcli: {e}")

            update_system_job_step(
                job_id, t0, 68.0,
                wc.T('w_step_5_5_installing', hb_v),
                None
            )
            run_apt_nonblocking([
                "autoconf", "automake", "cmake", "git", "libass-dev", "libbz2-dev",
                "libfontconfig-dev", "libfreetype-dev", "libfribidi-dev", "libharfbuzz-dev",
                "libjansson-dev", "liblzma-dev", "libmp3lame-dev", "libnuma-dev",
                "libogg-dev", "libopus-dev", "libsamplerate0-dev", "libspeex-dev",
                "libtheora-dev", "libtool", "libtool-bin", "libturbojpeg0-dev",
                "libvorbis-dev", "libx264-dev", "libxml2-dev", "libvpx-dev",
                "m4", "make", "meson", "nasm", "ninja-build", "patch", "pkg-config", "zlib1g-dev"
            ], timeout_sec=600)

            hb_sdir = os.path.join(b_dir, "HandBrake")
            update_system_job_step(
                job_id, t0, 73.0,
                wc.T('w_step_5_5_cloning', hb_v),
                None
            )
            cl_res = subprocess.run(["git", "clone", "--depth", "1", "--branch", hb_v, "https://github.com/HandBrake/HandBrake.git", hb_sdir], capture_output=True, text=True, timeout=300)
            if cl_res.returncode != 0 or not os.path.isdir(hb_sdir):
                raise RuntimeError(f"git clone HandBrake {hb_v} failed (rc={cl_res.returncode}): {(cl_res.stderr or '')[-160:]}")

            c_cnt = get_effective_cpu_cores()
            update_system_job_step(
                job_id, t0, 76.0,
                wc.T('w_step_5_5_compiling', hb_v, c_cnt),
                None
            )
            c_cmd = ["./configure", "--disable-gtk", "--enable-x265", "--enable-numa", f"--launch-jobs={c_cnt}", "--launch"]
            p_hb = subprocess.Popen(c_cmd, cwd=hb_sdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            with jobs_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]["process"] = p_hb

            ls_t = time.time()
            c_prog = 76.0
            last_ln = ""
            for raw_ln in p_hb.stdout:
                if not is_running or is_job_cancelled(job_id):
                    wc.kill_process_tree(p_hb)
                    raise InterruptedError("Cancelled")
                cl = raw_ln.strip()
                if cl:
                    last_ln = cl[:140]
                if time.time() - ls_t > 5.0:
                    if check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
                        wc.kill_process_tree(p_hb)
                        raise InterruptedError("Cancelled")
                    c_prog = min(94.0, c_prog + 1.5)
                    update_system_job_step(
                        job_id, t0, c_prog,
                        wc.T('w_step_5_5_compiling_2', hb_v, f'{c_prog:.0f}'),
                        None,
                        console_line=last_ln
                    )
                    ls_t = time.time()

            p_hb.wait()
            b_hb = os.path.join(hb_sdir, "build", "HandBrakeCLI")
            if not os.path.isfile(b_hb):
                raise RuntimeError(f"Compilation of HandBrakeCLI v{hb_v} failed.")

            update_system_job_step(
                job_id, t0, 96.0,
                wc.T('w_step_5_5_packing', hb_v),
                None
            )
            o_shb = os.path.join(b_dir, "HandBrakeCLI_static")
            subprocess.run([sx_bin, "--strip", b_hb, o_shb], capture_output=True, timeout=180)
            f_hb = o_shb if os.path.isfile(o_shb) else b_hb
            os.chmod(f_hb, 0o755)
            _upload_or_note("linux", "hbcli", f_hb, hb_v)

        if do_launcher:
            try:
                import launcher_builder as lb
                ico_src = os.path.join(b_dir, "hbw.ico")
                has_ico = fetch_hbw_icon(ico_src)
                final_exe = os.path.join(b_dir, "HBWorker_final.exe")
                if lb.compile_hbworker_exe(
                    final_exe,
                    icon_src_path=ico_src if has_ico else "",
                    build_dir=os.path.join(b_dir, "launcher_build"),
                    product_version=CURRENT_VERSION,
                    log_fn=log,
                ):
                    _upload_or_note("windows", "launcher", final_exe, CURRENT_VERSION)
                else:
                    build_errors.append("windows/launcher compile failed")
            except Exception as e:
                log(f"[SYSTEM-BUILD] Windows HBWorker.exe launcher warning: {e}")
                build_errors.append(f"windows/launcher: {e}")

        if build_errors:
            raise RuntimeError("System build incomplete: " + "; ".join(build_errors[:6]))

        update_system_job_step(
            job_id, t0, 99.0,
            wc.T('w_synchronizing_new_binaries_bin'),
            []
        )
        with jobs_lock:
            has_compute = any(not v.get("is_system_build") for v in active_jobs.values())
        sync_and_resolve_hb_binary(allow_hb_update=not has_compute)
        collect_diagnostics()

        dur = int(round(time.time() - t0))
        api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/complete", json={"version": CURRENT_VERSION, "duration_sec": dur}, timeout=10)
        log(wc.T('w_system_build_system_job', job_id, dur))

    except InterruptedError:
        log(wc.T('w_system_build_system_job_2', job_id))
    except Exception as e:
        log(wc.T('w_system_build_critical_error', job_id, e))
        try:
            api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/error", json={"error_type": "GENERIC", "error_code": "ERR_HB_FAILED", "error_detail": str(e)[-250:], "version": CURRENT_VERSION}, timeout=10)
        except Exception:
            pass
    finally:
        shutil.rmtree(b_dir, ignore_errors=True)
        shutil.rmtree("/tmp/hb_staticx_venv", ignore_errors=True)
        try:
            subprocess.run(["apt-get", "clean"], capture_output=True, timeout=30)
            shutil.rmtree("/var/lib/apt/lists", ignore_errors=True)
            os.makedirs("/var/lib/apt/lists/partial", exist_ok=True)
        except Exception:
            pass
        with jobs_lock:
            active_jobs.pop(job_id, None)
        force_update_check = True


def process_job_thread(
    job_id, payload, verified_rel_source, use_local_write, staging_mode, convert_ass,
    auto_clean_empty_subs, auto_extract_forced_subs, write_mkv_stats,
    lpcm_passthru, smart_premux, ineffective_action="both",
    ineffective_prefix="[HIGHER_BITRATE]_", ineffective_subfolder="_HIGHER_BITRATE_THAN_SOURCE",
    burn_in_mode="separate", dts_hd_mode="keep_both"
):
    global is_running, force_update_check
    hb_p = None
    t0 = time.time()
    keep_temp_for_disk_hold = False
    encode_completed_ok = False
    saved_meta_obj = None
    w_dest = ""
    orig_d = ""
    tmp_orig_d = ""
    src_sz = 0
    v_in = 0
    a_in = 0

    with jobs_lock:
        prev_jd = active_jobs.get(job_id) or {}
        active_jobs[job_id] = {
            "progress": 0.0, "fps_current": 0.0, "fps_avg": 0.0, "speed_mbps": 0.0,
            "elapsed_sec": 0, "eta_sec": 0, "transferred_bytes": 0, "src_size_bytes": 0,
            "is_net_fallback": False, "net_fallback_reason": "", "v_bitrate_in_kbps": 0, "a_bitrate_in_kbps": 0,
            "premux_applied": False, "premux_status": "", "step_message": None,
            "updating_tools": None, "process": None,
            "cancelled": bool(prev_jd.get("cancelled", False)),
            "user_paused": bool(prev_jd.get("user_paused", False)),
        }
    if is_job_cancelled(job_id):
        with jobs_lock:
            active_jobs.pop(job_id, None)
        return

    j_tmp = os.path.join(LOCAL_TEMP_DIR, f"job_{job_id}")
    os.makedirs(j_tmp, exist_ok=True)
    fb_net = False
    fb_reason = ""
    pm_done = False
    pm_stat = ""
    use_relay_mode = False

    def _check_cancel(remote_check: bool) -> bool:
        if not is_running or is_job_cancelled(job_id):
            return True
        if remote_check and check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
            return True
        return False

    try:
        it = payload[0] if (isinstance(payload, list) and len(payload) > 0) else payload
        if not isinstance(it, dict):
            it = {}
        job_node = copy.deepcopy(it.get("Task") or it.get("Job") or it)
        if not isinstance(job_node, dict):
            job_node = {}

        sv = job_node.get("Source", "")
        r_src = str(sv.get("Path", "")) if isinstance(sv, dict) else str(sv)
        if not r_src and isinstance(it.get("Title"), dict) and it["Title"].get("Path"):
            r_src = str(it["Title"]["Path"])
        if not r_src and isinstance(it.get("uiSettings"), dict):
            r_src = str(it["uiSettings"].get("source") or "")
        orig_s = resolve_worker_source_path(r_src, verified_rel_source)

        if not os.path.exists(orig_s):
            log(wc.T('w_id_source_not_locally', job_id, orig_s))
            try:
                api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/staging", timeout=5)
            except Exception:
                pass
            relay_s_dir = os.path.join(j_tmp, "SOURCE")
            last_rel_log = [0.0, -1]

            def _on_rel_dl(prog, spd, copied_b, elap_s, now_t):
                with jobs_lock:
                    if job_id in active_jobs:
                        active_jobs[job_id]["progress"] = prog
                        active_jobs[job_id]["fps_current"] = 0.0
                        active_jobs[job_id]["fps_avg"] = 0.0
                        active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                        active_jobs[job_id]["transferred_bytes"] = copied_b
                        active_jobs[job_id]["elapsed_sec"] = elap_s
                if int(prog) >= last_rel_log[1] + 5 and now_t - last_rel_log[0] > 5.0:
                    log(wc.T('w_http_relay_download_job', job_id, f'{prog:.1f}', f'{spd:.1f}'))
                    last_rel_log[0] = now_t
                    last_rel_log[1] = int(prog)

            try:
                orig_s = wc.relay_download_source_to_temp(
                    MANAGER_URL, job_id, WORKER_NAME, relay_s_dir, _check_cancel, _on_rel_dl
                )
                use_relay_mode = True
                use_local_write = True
            except InterruptedError:
                raise
            except Exception as rel_err:
                is_enospc = isinstance(rel_err, OSError) and (getattr(rel_err, "errno", 0) == errno.ENOSPC or "No space left" in str(rel_err))
                log(wc.T('w_id_source_missing_http', job_id, orig_s, rel_err))
                try:
                    if is_enospc:
                        api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/error", json={"error_type": "GENERIC", "error_code": "ERR_WORKER_DISK_FULL", "error_detail": str(rel_err)[-250:], "version": CURRENT_VERSION}, timeout=5)
                    else:
                        api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/error", json={"error_type": "FILE", "error_code": "ERR_SOURCE_MISSING", "error_detail": orig_s, "version": CURRENT_VERSION}, timeout=5)
                except Exception:
                    pass
                return

        src_sz, v_in, a_in, mpls_n = wc.probe_source_size_and_bitrates(
            orig_s, job_node, HANDBRAKE_CLI, root_item=it, ffprobe_bin=FFPROBE_CLI, sevenzip_bin=SEVENZIP_CLI
        )
        phys_sz = wc.get_file_size(orig_s)
        job_dur_s = wc.extract_job_duration_seconds(job_node, it)

        with jobs_lock:
            if job_id in active_jobs:
                active_jobs[job_id]["src_size_bytes"] = src_sz
                active_jobs[job_id]["v_bitrate_in_kbps"] = v_in
                active_jobs[job_id]["a_bitrate_in_kbps"] = a_in

        dv = job_node.get("Destination", "")
        g_dest = str(dv.get("File", "")) if isinstance(dv, dict) else str(dv)
        if not g_dest and isinstance(it.get("uiSettings"), dict):
            g_dest = str(it["uiSettings"].get("destination") or "")
        d_base = os.path.basename(g_dest.replace("\\", "/")) or (os.path.splitext(os.path.basename(orig_s))[0] + ".mkv")

        bn_only, o_ext = os.path.splitext(d_base)
        if o_ext.lower() not in ['.mkv', '.mp4', '.m4v', '.webm', '.mov']:
            d_base = bn_only + ".mkv"

        out_r = get_linux_outputs_dir()
        if not (os.path.exists(out_r) and os.access(out_r, os.W_OK)):
            use_relay_mode = True
            use_local_write = True

        orig_d = os.path.normpath(os.path.join(out_r, d_base))
        tmp_orig_d = orig_d + ".tmp"
        if not use_relay_mode:
            o_refuse = wc.output_target_refusal(orig_d, orig_s, get_linux_inputs_dir())
            if o_refuse:
                tmp_orig_d = ""                       # nic v cílové složce nemazat ani nevytvářet
                raise RuntimeError(o_refuse)

        base_orig_s = orig_s
        base_src_sz = src_sz
        base_phys_sz = phys_sz
        base_v_in = v_in
        base_a_in = a_in
        base_use_local_write = use_local_write
        base_job_node = copy.deepcopy(job_node)
        is_partial_job, is_chapter_rng_job = wc.is_job_partial_range(job_node, it)
        is_nonzero_start_job = wc.is_job_nonzero_start(job_node, it)
        premux_blocked_partial = bool(
            is_nonzero_start_job and wc.job_needs_premux_audio_postproc(job_node, it, lpcm_passthru, dts_hd_mode)
        )
        s_rem, s_stat, lpcm_c = 0, "OK", 0

        def _cleanup_premux_retry_artifacts():
            for _fp in (
                w_dest,
                os.path.join(j_tmp, "premux_source.mkv"),
                os.path.join(j_tmp, wc.PREMUX_MPLS_META_FILENAME),
            ):
                if _fp and os.path.isfile(_fp):
                    try:
                        os.remove(_fp)
                    except Exception:
                        pass
            shutil.rmtree(os.path.join(j_tmp, "ISO_EXTRACT"), ignore_errors=True)
            if not use_relay_mode:
                shutil.rmtree(os.path.join(j_tmp, "SOURCE"), ignore_errors=True)

        for _pm_try in (0, 1):
            if _pm_try == 1:
                _cleanup_premux_retry_artifacts()
                orig_s = base_orig_s
                src_sz = base_src_sz
                phys_sz = base_phys_sz
                v_in = base_v_in
                a_in = base_a_in
                use_local_write = base_use_local_write
                job_node = copy.deepcopy(base_job_node)
                pm_done = False
                pm_stat = "FALLBACK"
                encode_completed_ok = False
                hb_p = None
                s_rem, s_stat, lpcm_c = 0, "OK", 0

            eff_try_premux = bool(smart_premux and _pm_try == 0 and not premux_blocked_partial)

            do_stg = False
            s_low = orig_s.lower()
            if not use_relay_mode:
                if staging_mode == "Všechny formáty":
                    do_stg = True
                elif staging_mode == "Jen ISO" and s_low.endswith(".iso"):
                    do_stg = True
                elif staging_mode == "ISO a BDMV" and (s_low.endswith(".iso") or s_low.endswith(".m2ts") or "bdmv" in s_low or "video_ts" in s_low or os.path.isdir(orig_s)):
                    do_stg = True

            is_disc = os.path.isdir(orig_s) or "bdmv" in s_low or "video_ts" in s_low or s_low.endswith(".iso")
            if _pm_try == 0 and is_disc and smart_premux and premux_blocked_partial:
                log(wc.T('w_id_premux_skip_partial_audio', job_id))

            is_4k_job = wc.is_job_4k_target(job_node, it, orig_s)
            sp_plan = wc.evaluate_disk_space_strategy(
                LOCAL_TEMP_DIR, out_r, phys_sz, src_sz, is_4k_job,
                use_local_write, do_stg, eff_try_premux, is_disc, s_low.endswith(".iso"), use_relay_mode
            )

            if sp_plan["premux_skip_reason"]:
                log(f"ID {job_id}: {sp_plan['premux_skip_reason']}")
            if sp_plan["staging_skip_reason"]:
                log(f"ID {job_id}: {sp_plan['staging_skip_reason']}")
            if sp_plan["direct_outputs_reason"]:
                log(f"ID {job_id}: {sp_plan['direct_outputs_reason']}")

            do_stg = sp_plan["allow_staging"]
            use_local_write = sp_plan["allow_local_write"]
            fb_net = sp_plan["is_net_fallback"]
            fb_reason = sp_plan.get("net_fallback_reason", "")
            with jobs_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]["is_net_fallback"] = bool(fb_net)
                    active_jobs[job_id]["net_fallback_reason"] = fb_reason

            if is_disc:
                if eff_try_premux and use_local_write:
                    pm_stat = "FALLBACK"
                    if sp_plan["allow_premux"]:
                        try:
                            pm_p = run_local_mkvmerge_premux(
                                job_id, orig_s, mpls_n, j_tmp, expected_src_bytes=src_sz,
                                job_dur_s=job_dur_s, allow_raw_m2ts_fallback=(not is_chapter_rng_job)
                            )
                        except InterruptedError:
                            raise
                        except Exception as pm_exc:
                            log(f"ID {job_id}: Pre-Mux failed ({pm_exc}), falling back to direct encode.")
                            pm_p = None
                        with jobs_lock:
                            if job_id in active_jobs:
                                if active_jobs[job_id].get("v_bitrate_in_kbps", 0) > 0:
                                    v_in = active_jobs[job_id]["v_bitrate_in_kbps"]
                                if active_jobs[job_id].get("a_bitrate_in_kbps", 0) > 0:
                                    a_in = active_jobs[job_id]["a_bitrate_in_kbps"]
                                if active_jobs[job_id].get("src_size_bytes", 0) > 0:
                                    src_sz = active_jobs[job_id]["src_size_bytes"]
                        if pm_p and os.path.isfile(pm_p):
                            orig_s = pm_p
                            src_sz = wc.get_file_size(orig_s)
                            phys_sz = src_sz
                            vp, ap = wc.probe_media_bitrates(orig_s, ffprobe_bin=FFPROBE_CLI)
                            if vp > 0:
                                v_in = vp
                            if ap > 0:
                                a_in = ap
                            pm_done = True
                            pm_stat = "OK"
                            if pm_p.startswith(j_tmp):
                                do_stg = False
                            job_node["Title"] = 1
                            if isinstance(job_node.get("Source"), dict):
                                job_node["Source"]["Title"] = 1
                else:
                    pm_stat = "FALLBACK" if smart_premux else "OFF"

                if job_dur_s > 60.0 and src_sz > 100 * 1024 * 1024:
                    v_in = wc.reconcile_disc_video_bitrate(src_sz, job_dur_s, v_in, a_in)

                with jobs_lock:
                    if job_id in active_jobs:
                        active_jobs[job_id]["src_size_bytes"] = src_sz
                        active_jobs[job_id]["v_bitrate_in_kbps"] = v_in
                        active_jobs[job_id]["a_bitrate_in_kbps"] = a_in
                        active_jobs[job_id]["premux_applied"] = pm_done
                        active_jobs[job_id]["premux_status"] = pm_stat
                        active_jobs[job_id]["step_message"] = ""
                        active_jobs[job_id]["speed_mbps"] = 0.0
                        active_jobs[job_id]["transferred_bytes"] = 0

            w_dest = os.path.normpath(os.path.join(j_tmp, d_base) if use_local_write else tmp_orig_d)
            os.makedirs(os.path.dirname(w_dest), exist_ok=True)

            if do_stg:
                s_dir = os.path.join(j_tmp, "SOURCE")
                os.makedirs(s_dir, exist_ok=True)
                s_base = os.path.basename(orig_s.rstrip("\\/")) or "SOURCE"
                s_src = os.path.join(s_dir, s_base)
                try:
                    api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/staging", timeout=5)
                except Exception:
                    pass

                last_stg_log = [0.0, -1]

                def _on_stg_prog(prog, spd, copied_b, elap_s, now_t):
                    with jobs_lock:
                        if job_id in active_jobs:
                            active_jobs[job_id]["progress"] = prog
                            active_jobs[job_id]["fps_current"] = 0.0
                            active_jobs[job_id]["fps_avg"] = 0.0
                            active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                            active_jobs[job_id]["transferred_bytes"] = copied_b
                            active_jobs[job_id]["src_size_bytes"] = phys_sz
                            active_jobs[job_id]["elapsed_sec"] = elap_s
                    if int(prog) >= last_stg_log[1] + 5 and now_t - last_stg_log[0] > 5.0:
                        log(wc.T('w_staging_job_mb_s_2', job_id, f'{prog:.1f}', f'{spd:.1f}'))
                        last_stg_log[0] = now_t
                        last_stg_log[1] = int(prog)

                wc.copy_path_with_progress(orig_s, s_src, phys_sz, _check_cancel, _on_stg_prog)
                orig_s = s_src
                if orig_s.lower().endswith(".iso"):
                    nv_b = wc.inspect_bluray_iso_playlist_bytes(orig_s, mpls_n, min_valid_bytes=5 * 1024 * 1024, sevenzip_bin=SEVENZIP_CLI)
                    if nv_b >= 5 * 1024 * 1024:
                        src_sz = nv_b
                    _, v_p2, a_p2, _ = wc.probe_source_size_and_bitrates(orig_s, job_node, HANDBRAKE_CLI, root_item=it, ffprobe_bin=FFPROBE_CLI, sevenzip_bin=SEVENZIP_CLI)
                    if v_p2 > 0:
                        v_in = v_p2
                    if a_p2 > 0:
                        a_in = a_p2
                if is_disc and job_dur_s > 60.0 and src_sz > 100 * 1024 * 1024:
                    v_in = wc.reconcile_disc_video_bitrate(src_sz, job_dur_s, v_in, a_in)
                with jobs_lock:
                    if job_id in active_jobs:
                        active_jobs[job_id]["src_size_bytes"] = src_sz
                        active_jobs[job_id]["v_bitrate_in_kbps"] = v_in
                        active_jobs[job_id]["a_bitrate_in_kbps"] = a_in

            if _check_cancel(False):
                raise InterruptedError("Cancelled")

            with jobs_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]["progress"] = 0.0
                    active_jobs[job_id]["speed_mbps"] = 0.0
                    active_jobs[job_id]["transferred_bytes"] = 0
                    active_jobs[job_id]["step_message"] = ""

            try:
                api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/encoding", timeout=5)
            except Exception:
                pass

            lpcm_tr = wc.detect_lpcm_audio_tracks_in_source(
                orig_s, job_node, ffprobe_bin=FFPROBE_CLI, root_item=it,
                lpcm_passthru=(lpcm_passthru and w_dest.lower().endswith(".mkv")), dts_hd_mode=dts_hd_mode, is_partial_range=is_nonzero_start_job
            )
            skip_a = [x["hb_audio_idx"] for x in lpcm_tr]
            if lpcm_tr:
                log(wc.T('w_id_detected_lpcm_pcm_2', job_id, len(lpcm_tr)))

            eff_cores = get_effective_cpu_cores()
            hyb = wc.detect_cpu_topology()
            eff_pm = resolve_linux_power_mode(current_power_mode, hyb)
            if eff_pm == "CUSTOM" and current_custom_cores:
                is_eco_start = len(current_custom_cores) < eff_cores
                eco_t_start = max(1, len(current_custom_cores))
            elif eff_pm == "HYBRID_ECO":
                is_eco_start = True
                eco_t_start = max(1, int(hyb.get("e_core_count") or (eff_cores - 2)))
            else:
                is_eco_start = (eff_pm == "ECO")
                eco_t_start = max(1, eff_cores - 2)
            cmd = wc.build_hb_cmd_from_windows_json(
                HANDBRAKE_CLI, job_node, orig_s, w_dest,
                auto_extract_forced_subs, skip_a, root_item=it,
                eco_mode=is_eco_start, eco_threads=eco_t_start,
                burn_in_mode=burn_in_mode, dts_hd_mode=dts_hd_mode
            )
            log(wc.T('w_id_full_assembled_cli', job_id, HB_BINARY_SOURCE, ' '.join(cmd)))

            hb_p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8', errors='replace')
            with jobs_lock:
                if job_id not in active_jobs or active_jobs[job_id].get("cancelled"):
                    wc.kill_process_tree(hb_p)
                    raise InterruptedError("Cancelled")
                active_jobs[job_id]["process"] = hb_p

            apply_linux_power_mode(current_power_mode)

            p_re = re.compile(r"Encoding: task \d+ of \d+, (\d+\.\d+) % \((.*?)\)")
            l_chk, l_log, l_pwr = 0.0, 0.0, time.time()
            l_pct = -1
            hb_tail = []
            e_t0 = time.time()
            pause_guard_state = {"suspended": False, "reason": ""}
            write_dir_to_watch = os.path.dirname(os.path.abspath(w_dest))

            def _set_step_msg(msg_txt: str):
                with jobs_lock:
                    if job_id in active_jobs:
                        active_jobs[job_id]["step_message"] = msg_txt

            for ln in hb_p.stdout:
                if _check_cancel(False):
                    wc.kill_process_tree(hb_p)
                    break
                cln = ln.strip()
                if cln:
                    hb_tail.append(cln)
                    if len(hb_tail) > 15:
                        hb_tail.pop(0)

                m = p_re.search(ln)
                ct = time.time()
                if ct - l_pwr >= 5.0:
                    apply_linux_power_mode(current_power_mode)
                    l_pwr = ct

                if m:
                    prog = float(m.group(1))
                    st_s = m.group(2)
                    fm = re.search(r"([\d\.]+)\s*fps(?:,\s*avg\s*([\d\.]+)\s*fps)?", st_s)
                    em = re.search(r"ETA ([\w\d]+)", st_s)
                    f_cur = float(fm.group(1)) if fm else 0.0
                    f_avg = float(fm.group(2)) if (fm and fm.group(2)) else f_cur
                    eta_s = wc.parse_hb_eta_to_seconds(em.group(1) if em else "")
                    elap = ct - e_t0

                    with jobs_lock:
                        if job_id in active_jobs:
                            active_jobs[job_id]["progress"] = prog
                            active_jobs[job_id]["fps_current"] = f_cur
                            active_jobs[job_id]["fps_avg"] = f_avg
                            active_jobs[job_id]["speed_mbps"] = 0.0
                            active_jobs[job_id]["elapsed_sec"] = int(elap)
                            active_jobs[job_id]["eta_sec"] = eta_s
                            active_jobs[job_id]["transferred_bytes"] = 0
                            active_jobs[job_id]["is_net_fallback"] = bool(fb_net)
                            active_jobs[job_id]["net_fallback_reason"] = fb_reason

                    cur_int_pct = int(prog)
                    if (cur_int_pct > l_pct and ct - l_log >= 20.0) or (ct - l_log >= 300.0):
                        log(wc.T('w_job_progress_fps_avg', job_id, f'{prog:.1f}', f'{f_cur:.1f}', f'{f_avg:.1f}'))
                        l_log = ct
                        l_pct = cur_int_pct

                if is_job_user_paused(job_id) or (ct - l_chk > 3.0):
                    if _check_cancel(True):
                        wc.kill_process_tree(hb_p)
                        break
                    wc.check_live_pause_and_disk_guard(
                        hb_p, write_dir_to_watch, pause_guard_state,
                        _check_cancel, lambda: is_job_user_paused(job_id),
                        log_fn=log, status_cb=_set_step_msg
                    )
                    l_chk = time.time()

            hb_p.wait()

            if _check_cancel(True):
                raise InterruptedError("Cancelled")

            if hb_p.returncode != 0 and is_running:
                hb_err_msg = f"HandBrake failed (code {hb_p.returncode}). Log: {' | '.join(hb_tail[-6:])}"
                if pm_done and _pm_try == 0:
                    log(wc.T('w_id_premux_audit_fallback', job_id, hb_err_msg))
                    continue
                raise RuntimeError(hb_err_msg)

            if not is_running:
                break

            if not os.path.exists(w_dest):
                if pm_done and _pm_try == 0:
                    log(wc.T('w_id_premux_audit_fallback', job_id, "Output video file was not created!"))
                    continue
                raise RuntimeError("Output video file was not created!")
            encode_completed_ok = True

            if not lpcm_tr:
                if not use_relay_mode:
                    shutil.rmtree(os.path.join(j_tmp, "SOURCE"), ignore_errors=True)
                pm_tmp_f = os.path.join(j_tmp, "premux_source.mkv")
                if os.path.isfile(pm_tmp_f) and os.path.abspath(pm_tmp_f) != os.path.abspath(w_dest):
                    try:
                        os.remove(pm_tmp_f)
                    except Exception:
                        pass

            sync_and_resolve_hb_binary(allow_hb_update=False)

            s_rem, s_stat, lpcm_c = 0, "OK", 0
            ff_p = FFMPEG_CLI or shutil.which("ffmpeg")
            fp_p = FFPROBE_CLI or shutil.which("ffprobe")

            if ff_p and fp_p and w_dest.lower().endswith(".mkv"):
                try:
                    do_cln = auto_clean_empty_subs or auto_extract_forced_subs
                    m_list, to_rem, n_ass = wc.analyze_subtitle_tracks_packets(ff_p, fp_p, w_dest, do_clean=do_cln, convert_ass=convert_ass)

                    if to_rem or n_ass or (lpcm_tr and os.path.exists(orig_s)):
                        log(wc.T('w_id_post_processing_removing', job_id, len(to_rem), len(lpcm_tr)))
                        t_mkv = wc.choose_safe_postproc_temp_mkv(w_dest, orig_d if not use_relay_mode else "")
                        f_cmd = wc.build_postproc_ffmpeg_cmd(ff_p, fp_p, w_dest, orig_s, t_mkv, lpcm_tr, m_list, to_rem, convert_ass)
                        def _reg_pp(p, _jid=job_id):
                            with jobs_lock:
                                if _jid in active_jobs:
                                    active_jobs[_jid]["process"] = p

                        fp_rc = wc.run_tracked_subprocess(f_cmd, register_fn=_reg_pp)

                        if fp_rc == 0 and os.path.exists(t_mkv):
                            if os.path.dirname(os.path.abspath(t_mkv)) == os.path.dirname(os.path.abspath(w_dest)):
                                os.remove(w_dest)
                                os.rename(t_mkv, w_dest)
                            else:
                                try:
                                    os.remove(w_dest)
                                except Exception:
                                    pass
                                wc.rekey_pending_audio_titles(w_dest, t_mkv)
                                w_dest = t_mkv
                                use_local_write = False
                            s_rem, s_stat = wc.compute_subtitle_postproc_status(m_list, to_rem, job_node=job_node, auto_extract_forced_subs=auto_extract_forced_subs, burn_in_respected=(burn_in_mode == "respect"))
                            lpcm_c = len(lpcm_tr)
                        else:
                            try:
                                if os.path.exists(t_mkv):
                                    os.remove(t_mkv)
                            except Exception:
                                pass
                            s_stat = "ERROR"
                    else:
                        s_rem, s_stat = wc.compute_subtitle_postproc_status(m_list, set(), job_node=job_node, auto_extract_forced_subs=auto_extract_forced_subs, burn_in_respected=(burn_in_mode == "respect"))
                except Exception:
                    s_stat = "ANALYZE_ERR"

            pm_tmp_f = os.path.join(j_tmp, "premux_source.mkv")
            if os.path.isfile(pm_tmp_f) and os.path.abspath(pm_tmp_f) != os.path.abspath(w_dest):
                try:
                    os.remove(pm_tmp_f)
                except Exception:
                    pass

            wc.sync_mkv_audio_titles(w_dest, mkvpropedit_bin=MKVPROPEDIT_CLI)

            audit_ok, audit_detail = wc.verify_output_stream_contract(
                w_dest, job_node, root_item=it, ffprobe_bin=FFPROBE_CLI, auto_clean_empty_subs=auto_clean_empty_subs,
                burn_in_respected=(burn_in_mode == "respect"), dts_hd_mode=dts_hd_mode, premux_used=pm_done
            )
            if not audit_ok:
                encode_completed_ok = False
                err_audit_msg = wc.T('w_stream_audit_failed_output', audit_detail)
                log(f"ID {job_id}: {err_audit_msg}")
                if pm_done and _pm_try == 0 and not _check_cancel(False):
                    log(wc.T('w_id_premux_audit_fallback', job_id, audit_detail))
                    continue
                raise RuntimeError(err_audit_msg)
            elif audit_detail == wc.STREAM_AUDIT_SKIPPED:
                log(wc.T('w_id_stream_audit_skipped', job_id))
            else:
                log(wc.T('w_id_output_stream_contract', job_id, audit_detail))
            break

        if encode_completed_ok and is_running:
            shutil.rmtree(os.path.join(j_tmp, "SOURCE"), ignore_errors=True)

            m_stats_ok = False
            if write_mkv_stats:
                m_stats_ok = wc.write_mkv_statistics_tags(w_dest, mkvpropedit_bin=MKVPROPEDIT_CLI)
                if m_stats_ok:
                    log(wc.T('w_id_mkv_track_statistics', job_id))

            v_out, a_out = wc.probe_media_bitrates(w_dest, ffprobe_bin=FFPROBE_CLI)
            o_sz = wc.get_file_size(w_dest)
            dur = time.time() - t0

            is_ineff = (v_in > 0 and v_in != 2500 and v_out >= v_in)
            f_dest_rep = None
            relay_sub = ""
            if is_ineff and ineffective_action and ineffective_action != "none":
                f_fn = d_base
                if ineffective_action in ("prefix", "both") and ineffective_prefix and not f_fn.startswith(ineffective_prefix):
                    f_fn = f"{ineffective_prefix}{f_fn}"
                t_odir = out_r
                if ineffective_action in ("subfolder", "both") and ineffective_subfolder:
                    relay_sub = ineffective_subfolder
                    t_odir = os.path.join(out_r, ineffective_subfolder)
                    if not use_relay_mode:
                        os.makedirs(t_odir, exist_ok=True)
                d_base = f_fn
                orig_d = os.path.normpath(os.path.join(t_odir, f_fn))
                tmp_orig_d = orig_d + ".tmp"
                f_dest_rep = orig_d
                log(wc.T('w_id_output_bitrate_source_2', job_id, v_in, v_out, orig_d))

            complete_pl = {
                "version": CURRENT_VERSION,
                "src_size_bytes": int(src_sz),
                "out_size_bytes": int(o_sz),
                "duration_sec": int(round(dur)),
                "subs_removed": int(s_rem),
                "subs_status": s_stat,
                "v_bitrate_in_kbps": int(v_in),
                "a_bitrate_in_kbps": int(a_in),
                "v_bitrate_out_kbps": int(v_out),
                "a_bitrate_out_kbps": int(a_out),
                "mkv_stats_written": bool(m_stats_ok),
                "lpcm_tracks_muxed": int(lpcm_c),
                "premux_applied": bool(pm_done),
                "premux_status": pm_stat,
                "final_dest_path": f_dest_rep
            }

            if use_local_write:
                slot_allowed = True
                hold_reason = ""
                try:
                    ru = api_request(
                        "POST", f"{MANAGER_URL}/api/jobs/{job_id}/request_upload",
                        json={"worker_name": WORKER_NAME, "out_size_bytes": int(o_sz), "force": False},
                        timeout=8
                    )
                    if ru.status_code == 200:
                        rd = ru.json()
                        slot_allowed = bool(rd.get("allowed", True))
                        hold_reason = rd.get("reason") or ""
                except Exception:
                    pass

                meta_obj = {
                    "job_id": job_id,
                    "job_temp_dir": j_tmp,
                    "w_dest": w_dest,
                    "orig_d": orig_d,
                    "out_size_bytes": int(o_sz),
                    "use_relay_output": use_relay_mode,
                    "relay_subfolder": relay_sub,
                    "complete_payload": complete_pl
                }
                if not slot_allowed:
                    log(wc.T('w_id_storage_guard_target_2', job_id, hold_reason))
                    wc.save_held_job_meta(j_tmp, meta_obj)
                    with held_lock:
                        held_jobs[job_id] = meta_obj
                    keep_temp_for_disk_hold = True
                    return

                # Před zahájením kopírování uložíme held_job_meta.json jako pojistku proti pádu/restartu uprostřed přenosu
                saved_meta_obj = meta_obj
                wc.save_held_job_meta(j_tmp, meta_obj)

                try:
                    api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/copying", timeout=5)
                except Exception:
                    pass

                last_cpy_log = [0.0, -1]

                def _on_cpy_prog(prog, spd, copied_b, elap_s, now_t):
                    with jobs_lock:
                        if job_id in active_jobs:
                            active_jobs[job_id]["progress"] = prog
                            active_jobs[job_id]["fps_current"] = 0.0
                            active_jobs[job_id]["fps_avg"] = 0.0
                            active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                            active_jobs[job_id]["transferred_bytes"] = copied_b
                            active_jobs[job_id]["src_size_bytes"] = o_sz
                            active_jobs[job_id]["elapsed_sec"] = elap_s
                    if int(prog) >= last_cpy_log[1] + 5 and now_t - last_cpy_log[0] > 5.0:
                        log(wc.T('w_copying_outputs_mb_s_2', f'{prog:.1f}', f'{spd:.1f}'))
                        last_cpy_log[0] = now_t
                        last_cpy_log[1] = int(prog)

                try:
                    if use_relay_mode:
                        log(wc.T('w_job_uploading_finished_video', job_id))
                        rel_res = wc.relay_upload_output_to_manager(
                            MANAGER_URL, job_id, WORKER_NAME, w_dest, d_base, relay_sub, _check_cancel, _on_cpy_prog
                        )
                        if rel_res.get("final_dest_path"):
                            complete_pl["final_dest_path"] = rel_res["final_dest_path"]
                        log(wc.T('w_job_http_relay_upload', job_id))
                    else:
                        log(wc.T('w_job_encoding_finished_moving', job_id))
                        wc.copy_path_with_progress(w_dest, tmp_orig_d, o_sz, _check_cancel, _on_cpy_prog)
                        final_d = wc.place_output_file(tmp_orig_d, orig_d, job_id)
                        if final_d != orig_d:
                            log(wc.T('w_out_saved_new_name', job_id, os.path.basename(orig_d), os.path.basename(final_d)))
                            complete_pl["final_dest_path"] = final_d
                        meta_obj["final_d"] = final_d          # pád před potvrzením → held smyčka soubor znovu nekopíruje
                        wc.save_held_job_meta(j_tmp, meta_obj)
                        log(wc.T('w_job_finished_saved_as', job_id))
                except OSError as ose:
                    try:
                        if os.path.exists(tmp_orig_d):
                            os.remove(tmp_orig_d)
                    except Exception:
                        pass
                    if ose.errno == errno.ENOSPC or "No space left" in str(ose):
                        h_msg = wc.T('w_out_disk_space_while', ose)
                        log(f"ID {job_id} [Storage Guard]: {h_msg}")
                        meta_obj = {
                            "job_id": job_id,
                            "job_temp_dir": j_tmp,
                            "w_dest": w_dest,
                            "orig_d": orig_d,
                            "out_size_bytes": int(o_sz),
                            "use_relay_output": use_relay_mode,
                            "relay_subfolder": relay_sub,
                            "complete_payload": complete_pl
                        }
                        wc.save_held_job_meta(j_tmp, meta_obj)
                        with held_lock:
                            held_jobs[job_id] = meta_obj
                        try:
                            api_request(
                                "POST", f"{MANAGER_URL}/api/jobs/{job_id}/waiting_disk",
                                json={
                                    "worker_name": WORKER_NAME, "out_size_bytes": int(o_sz),
                                    "src_size_bytes": int(src_sz), "v_bitrate_in_kbps": int(v_in),
                                    "a_bitrate_in_kbps": int(a_in), "v_bitrate_out_kbps": int(v_out),
                                    "a_bitrate_out_kbps": int(a_out), "reason": h_msg
                                }, timeout=5
                            )
                        except Exception:
                            pass
                        keep_temp_for_disk_hold = True
                        return
                    raise
            else:
                os.makedirs(os.path.dirname(orig_d), exist_ok=True)
                if os.path.abspath(w_dest) != os.path.abspath(orig_d):
                    final_d = wc.place_output_file(w_dest, orig_d, job_id)
                    if final_d != orig_d:
                        log(wc.T('w_out_saved_new_name', job_id, os.path.basename(orig_d), os.path.basename(final_d)))
                        complete_pl["final_dest_path"] = final_d
                log(wc.T('w_job_finished_renamed_clean', job_id))

            while is_running:
                try:
                    r = api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/complete", json=complete_pl, timeout=5)
                    if r.status_code == 200 or wc.is_own_manager_refusal(r):
                        break
                    time.sleep(5)
                except Exception:
                    time.sleep(5)

    except InterruptedError:
        log(wc.T('w_job_cleanly_cancelled_temp', job_id))
    except Exception as e:
        if not is_job_cancelled(job_id):
            if encode_completed_ok and w_dest and os.path.isfile(w_dest) and os.path.getsize(w_dest) > 256 * 1024 and "[AUDIT STOP" not in str(e) and "[STREAM AUDIT" not in str(e):
                o_sz = os.path.getsize(w_dest)
                h_msg = wc.T('w_encoding_finished_but_post', e, f'{o_sz / 1024 ** 3:.1f}')
                log(f"ID {job_id} [RESCUE]: {h_msg}")
                if isinstance(saved_meta_obj, dict) and saved_meta_obj.get("complete_payload"):
                    meta_obj = dict(saved_meta_obj)
                    meta_obj["out_size_bytes"] = int(o_sz)
                else:
                    meta_obj = {
                        "job_id": job_id,
                        "job_temp_dir": j_tmp,
                        "w_dest": w_dest,
                        "orig_d": orig_d or os.path.join(get_linux_outputs_dir(), os.path.basename(w_dest)),
                        "out_size_bytes": int(o_sz),
                        "use_relay_output": use_relay_mode,
                        "relay_subfolder": "",
                        "complete_payload": {
                            "version": CURRENT_VERSION,
                            "src_size_bytes": int(src_sz),
                            "out_size_bytes": int(o_sz),
                            "duration_sec": int(round(time.time() - t0)),
                            "subs_removed": 0,
                            "subs_status": "OK",
                            "v_bitrate_in_kbps": int(v_in),
                            "a_bitrate_in_kbps": int(a_in),
                            "v_bitrate_out_kbps": 0,
                            "a_bitrate_out_kbps": 0,
                            "mkv_stats_written": False,
                            "lpcm_tracks_muxed": 0,
                            "premux_applied": bool(pm_done),
                            "premux_status": pm_stat,
                            "final_dest_path": None
                        }
                    }
                wc.save_held_job_meta(j_tmp, meta_obj)
                with held_lock:
                    held_jobs[job_id] = meta_obj
                try:
                    api_request(
                        "POST", f"{MANAGER_URL}/api/jobs/{job_id}/waiting_disk",
                        json={"worker_name": WORKER_NAME, "out_size_bytes": int(o_sz), "src_size_bytes": int(src_sz), "reason": h_msg},
                        timeout=5
                    )
                except Exception:
                    pass
                keep_temp_for_disk_hold = True
            else:
                log(wc.T('w_critical_error_job', job_id, e))
                try:
                    api_request("POST", f"{MANAGER_URL}/api/jobs/{job_id}/error", json={"error_type": "GENERIC", "error_code": "ERR_HB_FAILED", "error_detail": str(e)[-250:], "version": CURRENT_VERSION}, timeout=5)
                except Exception:
                    pass
    finally:
        try:
            wc.clear_pending_audio_titles(w_dest)
            if not keep_temp_for_disk_hold:
                try:
                    if os.path.exists(j_tmp):
                        shutil.rmtree(j_tmp, ignore_errors=True)
                except Exception:
                    pass
                for _lo in (tmp_orig_d, (orig_d + ".clean.tmp.mkv") if orig_d else ""):
                    try:
                        if _lo and os.path.exists(_lo):
                            os.remove(_lo)
                    except Exception:
                        pass
        finally:
            with jobs_lock:
                active_jobs.pop(job_id, None)
            force_update_check = True


_held_restored_once = False
_storage_migrated_once = False


def migrate_legacy_worker_storage():
    """Jednorázová migrace starých úložišť (/app/workers/bin, /temp, /app/workers/HB_Temp) do /data a jejich vyčištění."""
    global _storage_migrated_once
    if _storage_migrated_once:
        return
    _storage_migrated_once = True

    # 1. Migrace binárek a markerů z /app/workers/bin -> /data/binaries
    try:
        if os.path.isdir(LEGACY_BIN_DIR) and os.path.abspath(LEGACY_BIN_DIR) != os.path.abspath(BIN_DIR):
            os.makedirs(BIN_DIR, exist_ok=True)
            for fn in os.listdir(LEGACY_BIN_DIR):
                src_p = os.path.join(LEGACY_BIN_DIR, fn)
                dst_p = os.path.join(BIN_DIR, fn)
                try:
                    if os.path.isfile(src_p):
                        if not os.path.isfile(dst_p) or os.path.getsize(dst_p) == 0:
                            shutil.move(src_p, dst_p)
                        else:
                            os.remove(src_p)
                    elif os.path.isdir(src_p):
                        shutil.rmtree(src_p, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

    # 2. Migrace platných držených úloh ze starého /temp a /app/workers/HB_Temp -> LOCAL_TEMP_DIR (/data/temp) a kompletní úklid
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    act_tmp_abs = os.path.abspath(LOCAL_TEMP_DIR)
    for leg_dir in ("/temp", LEGACY_APP_TEMP_DIR):
        if not leg_dir or not os.path.isdir(leg_dir):
            continue
        if os.path.abspath(leg_dir) == act_tmp_abs or wc.path_is_within(leg_dir, LOCAL_TEMP_DIR):
            continue
        if leg_dir == "/temp" and _is_custom_temp_mount():
            continue          # vlastní připojená složka /temp (může v ní mít uživatel cokoli) se nikdy nemaže
        try:
            for fn in os.listdir(leg_dir):
                src_p = os.path.join(leg_dir, fn)
                try:
                    if os.path.isdir(src_p) and fn.startswith("job_"):
                        meta_p = os.path.join(src_p, "held_job_meta.json")
                        migrated = False
                        if os.path.isfile(meta_p):
                            with open(meta_p, "r", encoding="utf-8") as f:
                                m = json.load(f)
                            old_w_dest = m.get("w_dest", "")
                            cand_w_dest = old_w_dest if (old_w_dest and os.path.isfile(old_w_dest)) else os.path.join(src_p, os.path.basename(old_w_dest or ""))
                            if m.get("job_id") and cand_w_dest and os.path.isfile(cand_w_dest):
                                dst_job_dir = os.path.join(LOCAL_TEMP_DIR, fn)
                                if not os.path.exists(dst_job_dir):
                                    shutil.move(src_p, dst_job_dir)
                                    new_w_dest = os.path.join(dst_job_dir, os.path.basename(cand_w_dest))
                                    m["job_temp_dir"] = dst_job_dir
                                    m["w_dest"] = new_w_dest
                                    wc.save_held_job_meta(dst_job_dir, m)
                                    log(wc.T("w_storage_migration_moved", fn, leg_dir, LOCAL_TEMP_DIR))
                                    migrated = True
                        if not migrated and os.path.exists(src_p):
                            shutil.rmtree(src_p, ignore_errors=True)
                    elif os.path.isdir(src_p):
                        shutil.rmtree(src_p, ignore_errors=True)
                    else:
                        os.remove(src_p)
                except Exception:
                    pass
        except Exception:
            pass


def restore_held_jobs_on_startup():
    global _held_restored_once
    if _held_restored_once:
        return
    _held_restored_once = True
    migrate_legacy_worker_storage()
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    loaded = wc.load_persisted_held_jobs(LOCAL_TEMP_DIR)
    if loaded:
        with held_lock:
            held_jobs.update(loaded)
        for jid, meta in loaded.items():
            cp = meta.get("complete_payload") or {}
            log(wc.T('w_restoring_held_completed_job', jid, meta.get('w_dest')))
            try:
                rw = api_request(
                    "POST", f"{MANAGER_URL}/api/jobs/{jid}/waiting_disk",
                    json={
                        "worker_name": WORKER_NAME,
                        "out_size_bytes": int(meta.get("out_size_bytes") or 0),
                        "src_size_bytes": int(cp.get("src_size_bytes") or 0),
                        "v_bitrate_in_kbps": int(cp.get("v_bitrate_in_kbps") or 0),
                        "a_bitrate_in_kbps": int(cp.get("a_bitrate_in_kbps") or 0),
                        "v_bitrate_out_kbps": int(cp.get("v_bitrate_out_kbps") or 0),
                        "a_bitrate_out_kbps": int(cp.get("a_bitrate_out_kbps") or 0),
                        "reason": wc.T('w_restored_worker_local_hold')
                    }, timeout=5
                )
                if rw.status_code == 200 and rw.json().get("obsolete"):
                    discard_held_jobs_by_ids([jid])
            except Exception:
                pass
    cleanup_temp_dir(on_startup=True)


def check_docker_sock_and_self_heal(act_c: int):
    """
    Když spojení s Dockerem dříve fungovalo a teď je přes 3 minuty mrtvé, worker počká, až nebude
    nic převádět ani nahrávat, a ukončí se. Docker ho podle restart policy (unless-stopped / always /
    on-failure) spustí znovu s novým docker.sock. Bez restart policy jen zapíše návod do logu.
    """
    st = docker_sock_state
    now = time.time()
    if not dsu.docker_sock_available():
        return
    if dsu.docker_sock_responding():
        if not st["ever_ok"] or not st["restart_policy"]:
            try:
                info = dsu.inspect_own_container(user_agent=f"HB-Cluster-Worker/{CURRENT_VERSION}") or {}
                st["restart_policy"] = str(((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no")
            except Exception:
                pass
        if st["fail_since"]:
            log(wc.T('w_docker_sock_recovered'))
        st["ever_ok"] = True
        st["fail_since"] = 0.0
        return
    if not st["ever_ok"]:
        return
    if not st["fail_since"]:
        st["fail_since"] = now
        return
    if now - st["fail_since"] < DOCKER_SOCK_STALE_AFTER_S:
        return
    can_restart = st["restart_policy"] in ("always", "unless-stopped", "on-failure")
    if not can_restart:
        if now - st["last_note"] > 3600:
            st["last_note"] = now
            log(wc.T('w_docker_sock_stale_manual'))
        return
    global _updating_or_restarting
    with held_lock:
        busy = act_c > 0 or uploading_held_job_id is not None
        if not busy:
            _updating_or_restarting = True
    if busy:
        if now - st["last_note"] > 3600:
            st["last_note"] = now
            log(wc.T('w_docker_sock_stale_wait'))
        return
    log(wc.T('w_docker_sock_stale_restart'))
    try:
        wc.flush_held_jobs_metadata(held_jobs, held_lock, LOCAL_TEMP_DIR)
    except Exception:
        pass
    time.sleep(8)  # heartbeat ještě stihne poslat poslední řádky logu do Manageru
    os._exit(75)


def request_update_now():
    """O4: „Aktualizovat teď“ z Manageru – zruší čekání po neúspěchu a hned zkusí aktualizaci."""
    global update_blocked_until, update_block_reason, force_update_check
    if update_blocked_until > time.time() or update_retry_state:
        log(wc.T('w_update_now_requested'))
    wc.update_retry_reset(update_retry_state)
    update_blocked_until = 0.0
    update_block_reason = ""
    force_update_check = True


def evaluate_update_attempt_on_start():
    """O1: jak dopadla poslední aktualizace (záznam v /data/update_attempt.json)."""
    global update_blocked_until, update_block_reason, update_attempt_clear_at
    if not dsu:
        return
    cur_img = ""
    if dsu.docker_sock_available():
        try:
            cur_img = str(dsu.get_own_image_info(user_agent=f"HB-Cluster-Worker/{CURRENT_VERSION}").get("id") or "")
        except Exception:
            cur_img = ""
    state, rec, res = dsu.evaluate_update_attempt(WORKER_DATA_DIR, CURRENT_VERSION, cur_img)
    if state == "updated":
        log(wc.T('w_update_done', rec.get("from") or "?", CURRENT_VERSION))
        update_attempt_clear_at = time.time() + dsu.UPDATE_ATTEMPT_KEEP_S
    elif state == "failed":
        until = float(rec.get("ts") or 0) + dsu.UPDATE_RETRY_AFTER_ROLLBACK_S
        if until <= time.time():
            dsu.clear_update_attempt(WORKER_DATA_DIR)
            return
        reason = str(res.get("reason") or "") or wc.T('w_update_reason_unknown')
        update_retry_state.update({"target": str(rec.get("to") or ""), "fails": 2, "reason": "rollback",
                                   "detail": reason[:300], "until": until})
        update_blocked_until, update_block_reason = until, "rollback"
        log(wc.T('w_update_rolled_back', rec.get("to") or "?", reason, CURRENT_VERSION, wc.fmt_retry_time(until)))
        for ln in list(res.get("log_tail") or [])[-12:]:
            log(f"  | {ln}")
    elif state == "stale":
        dsu.clear_update_attempt(WORKER_DATA_DIR)


def note_update_failure(target: str, reason: str, short_s: float, detail: str = "") -> float:
    """Neúspěch aktualizace: 1. → short_s, 2. stejné verze → 6 h (O4). Vrací čas dalšího pokusu."""
    global update_blocked_until, update_block_reason
    update_blocked_until = wc.update_retry_note(update_retry_state, target, reason, short_s, detail=detail)
    update_block_reason = reason
    return update_blocked_until


def write_ready_marker():
    """O1: „naběhl jsem“ pro pomocníka aktualizace (ověřuje nový kontejner 60 s)."""
    if not dsu:
        return
    try:
        with open(dsu.WORKER_READY_FILE, "w", encoding="utf-8") as f:
            f.write(f"{CURRENT_VERSION} {int(time.time())}\n")
    except Exception:
        pass


def worker_loop():
    global is_running, force_update_check, manager_reachable, last_udp_discovery_ts, update_blocked_until, update_block_reason
    global update_attempt_clear_at
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    evaluate_update_attempt_on_start()
    migrate_legacy_worker_storage()
    sync_and_resolve_hb_binary(allow_hb_update=True)
    collect_diagnostics()
    sample_telemetry()
    perform_storage_handshake(force_log=True)
    restore_held_jobs_on_startup()
    log(wc.T('w_hb_cluster_worker_linux', CURRENT_VERSION, wc.CURRENT_VERSION, cached_diagnostics.get('hb_version'), HB_BINARY_SOURCE, cached_diagnostics.get('x265_version'), get_effective_cpu_cores(), LOCAL_TEMP_DIR))
    l_clean = time.time()
    l_upd = 0
    l_diag = time.time()
    upd_pend = False
    upd_target = ""
    write_ready_marker()

    while is_running:
        ct = time.time()
        if ct - l_diag > 60:
            collect_diagnostics()
            l_diag = ct
        if update_attempt_clear_at and ct >= update_attempt_clear_at:
            update_attempt_clear_at = 0.0
            dsu.clear_update_attempt(WORKER_DATA_DIR)   # nová verze běží 5 min – záznam o pokusu už netřeba

        with jobs_lock:
            act_c = len(active_jobs)
            compute_act_c = sum(1 for v in active_jobs.values() if not v.get("is_system_build"))
        held_ids = get_held_job_ids()

        if ct - l_clean > 300:
            if act_c == 0 and uploading_held_job_id is None:
                cleanup_temp_dir()
            perform_storage_handshake(force_log=False)
            l_clean = ct

        if dsu and ct - docker_sock_state["last_check"] > 60:
            docker_sock_state["last_check"] = ct
            check_docker_sock_and_self_heal(act_c)

        if force_update_check or (ct - l_upd > 30):
            force_update_check = False
            sync_and_resolve_hb_binary(allow_hb_update=(compute_act_c == 0))
            collect_diagnostics()
            try:
                rv = api_request("GET", f"{MANAGER_URL}/api/update/version", timeout=5)
                if rv.status_code == 200:
                    vd = rv.json()
                    if vd.get("ui_lang"):
                        sync_worker_lang(vd.get("ui_lang"))
                    lv = vd.get("version")
                    if lv and lv != CURRENT_VERSION:
                        upd_pend = True
                        upd_target = str(lv)
            except Exception:
                pass
            l_upd = ct

        if upd_pend and time.time() < update_blocked_until:
            upd_pend = False

        if upd_pend:
            global _updating_or_restarting
            with held_lock:
                can_start_upd = (act_c == 0 and uploading_held_job_id is None)
                if can_start_upd:
                    _updating_or_restarting = True
            if can_start_upd:
                try:
                    log(wc.T('w_new_version_detected_server_2'))
                    wc.flush_held_jobs_metadata(held_jobs, held_lock, LOCAL_TEMP_DIR)
                    # v2.11.2: Žádné stahování .py skriptů ani os.execv. Linux Worker se
                    # aktualizuje výhradně výměnou Docker obrazu z GHCR přes docker.sock.
                    # O1: nový obraz se nejdřív zkusmo načte (--selftest), pomocník nový kontejner 60 s ověří
                    # a když nenaběhne, vrátí tento; o pokusu ví záznam v /data/update_attempt.json.
                    worker_image = get_worker_image()
                    res = dsu.trigger_self_recreate(
                        worker_image, log_fn=log,
                        user_agent=f"HB-Cluster-Worker/{CURRENT_VERSION}", stop_timeout=20,
                        selftest_cmd=["python", os.path.abspath(__file__), "--selftest"],
                        attempt_dir=WORKER_DATA_DIR, current_version=CURRENT_VERSION, verify_role="worker",
                    ) if dsu else {"ok": False, "reason": "no_docker_sock"}
                    if res.get("ok"):
                        log(wc.T('w_docker_recreate_started', worker_image, res.get("version") or "?"))
                        # Pomocník tento kontejner do pár sekund zastaví. Když se do 2 min nic nestane, nepovedlo se.
                        t_wait = time.time()
                        while is_running and time.time() - t_wait < 120:
                            time.sleep(2)
                        if not is_running:
                            continue
                        dsu.clear_update_attempt(WORKER_DATA_DIR)
                        nxt = note_update_failure(res.get("version") or upd_target, "helper_failed", 900)
                        log(wc.T('w_update_recreate_failed', "helper", wc.T('w_update_helper_no_effect'), wc.fmt_retry_time(nxt)))
                        upd_pend = False
                        continue
                    rsn = str(res.get("reason") or "error")
                    if rsn == "no_docker_sock":
                        update_retry_state.update({"target": upd_target, "fails": 1, "reason": rsn, "detail": "", "until": time.time() + 6 * 3600})
                        update_blocked_until = update_retry_state["until"]
                        update_block_reason = "no_docker_sock"
                        log(wc.T('w_update_needs_docker_sock', worker_image))
                    elif rsn == "image_unchanged":
                        nxt = note_update_failure(upd_target, rsn, 600)
                        log(wc.T('w_update_image_not_published', worker_image, wc.fmt_retry_time(nxt)))
                    elif rsn == "selftest_failed":
                        nxt = note_update_failure(res.get("version") or upd_target, rsn, 900, detail=res.get("detail") or "")
                        log(wc.T('w_update_selftest_failed', res.get("version") or "?", res.get("detail") or "?", wc.fmt_retry_time(nxt)))
                        for ln in list(res.get("log_tail") or [])[-12:]:
                            log(f"  | {ln}")
                    else:
                        nxt = note_update_failure(upd_target, rsn, 900, detail=res.get("detail") or "")
                        log(wc.T('w_update_recreate_failed', rsn, res.get("detail") or "", wc.fmt_retry_time(nxt)))
                    upd_pend = False
                    continue
                finally:
                    if is_running:
                        _updating_or_restarting = False
            else:
                time.sleep(5)
                continue

        try:
            st_data = sample_telemetry()
            with jobs_lock:
                r_ids = list(active_jobs.keys())
            st_ok = bool(handshake_status.get("inputs_ok") and handshake_status.get("outputs_ok")) or is_http_relay_active()
            prms = {
                "worker_name": WORKER_NAME,
                "version": CURRENT_VERSION,
                "running": json.dumps(r_ids),
                "held": json.dumps(held_ids),
                "telemetry": json.dumps(st_data),
                "storage_ok": "true" if st_ok else "false"
            }
            if time.time() < update_blocked_until:
                prms["update_blocked"] = "true"

            resp = api_request("GET", f"{MANAGER_URL}/api/jobs/next", params=prms, timeout=20)
            if resp.status_code == 200:
                if not manager_reachable:
                    manager_reachable = True
                    wc.sync_locales_from_manager(MANAGER_URL)
                    log(wc.T('w_connection_hb_cluster_manager', MANAGER_URL))
                    perform_storage_handshake(force_log=False)
                d = resp.json()
                if d.get("cleanup_held_job_ids"):
                    discard_held_jobs_by_ids(d.get("cleanup_held_job_ids"))
                if d.get("ui_lang"):
                    sync_worker_lang(d.get("ui_lang"))
                if d.get("update_now"):
                    request_update_now()
                if d.get("update_required") and time.time() >= update_blocked_until:
                    upd_pend = True
                    upd_target = str(d.get("target_version") or upd_target)
                    continue
                rg = int(d.get("binary_generation") or 0)
                if rg and compute_act_c == 0:
                    check_and_apply_binary_generation(rg, True)
                if (d.get("power_mode") and d.get("power_mode") != current_power_mode) or (d.get("custom_cores") is not None and wc.normalize_custom_cores(d.get("custom_cores"), get_effective_cpu_cores()) != current_custom_cores):
                    apply_linux_power_mode(d.get("power_mode") or current_power_mode, d.get("custom_cores"))
                if d.get("job_id"):
                    jid = d["job_id"]
                    j_pl = d.get("payload") or {}
                    is_sb = bool(d.get("is_system_build") or (isinstance(j_pl, dict) and j_pl.get("SystemTask") == "BUILD_BINARIES"))
                    if not st_ok and not is_sb:
                        try:
                            api_request("POST", f"{MANAGER_URL}/api/jobs/{jid}/cancel", json={"worker_name": WORKER_NAME, "reason": "Storage unreachable"}, timeout=5)
                        except Exception:
                            pass
                        continue

                    with jobs_lock:
                        active_jobs[jid] = {
                            "progress": 0.0, "fps_current": 0.0, "fps_avg": 0.0, "speed_mbps": 0.0,
                            "elapsed_sec": 0, "eta_sec": 0, "transferred_bytes": 0, "src_size_bytes": 0,
                            "is_net_fallback": False, "net_fallback_reason": "", "v_bitrate_in_kbps": 0, "a_bitrate_in_kbps": 0,
                            "premux_applied": False, "premux_status": "", "is_system_build": is_sb, "step_message": None,
                            "updating_tools": None, "process": None, "cancelled": False, "user_paused": False
                        }

                    if is_sb:
                        t = threading.Thread(target=process_system_build_job_thread, args=(jid, j_pl), daemon=True)
                    else:
                        t = threading.Thread(
                            target=process_job_thread,
                            args=(
                                jid, j_pl, d.get("verified_rel_source", ""), d.get("use_local_temp", True),
                                d.get("staging_mode", "Nikdy"), wc.resolve_ass_srt_mode(d),
                                d.get("auto_clean_empty_subs", True), d.get("auto_extract_forced_subs", False),
                                d.get("write_mkv_stats", True), d.get("lpcm_passthru", True),
                                d.get("smart_premux", False), d.get("ineffective_action", "both"),
                                d.get("ineffective_prefix", "[HIGHER_BITRATE]_"), d.get("ineffective_subfolder", "_HIGHER_BITRATE_THAN_SOURCE"),
                                str(d.get("burn_in_mode") or "separate"),
                                str(d.get("dts_hd_mode") or "keep_both")
                            ),
                            daemon=True
                        )
                    t.start()
                    time.sleep(1)
                    continue
        except Exception:
            if manager_reachable:
                manager_reachable = False
                log(wc.T('w_hb_cluster_manager_temporarily', MANAGER_URL))
            if (ct - last_udp_discovery_ts) > 60.0:
                last_udp_discovery_ts = ct
                try_auto_discover_manager()
        time.sleep(5)


def run_selftest() -> int:
    """O1: zkušební načtení této verze. Spouští ho STARÝ worker před aktualizací v dočasném kontejneru
    z nového obrazu (bez sítě a bez svazků). Modul je už celý načtený; ověří se sdílené jádro, překlady
    a to, že se tato verze umí dál aktualizovat. Nic se nespouští."""
    problems = []
    if str(getattr(wc, "CURRENT_VERSION", "")) != CURRENT_VERSION:
        problems.append(f"worker_core {getattr(wc, 'CURRENT_VERSION', '?')} != {CURRENT_VERSION}")
    for fn in ("build_hb_cmd_from_windows_json", "update_retry_note", "update_retry_view", "init_worker_identity",
               "init_manager_binding", "prepare_worker_temp_dir", "T"):
        if not callable(getattr(wc, fn, None)):
            problems.append(f"worker_core.{fn} missing")
    if dsu is None:
        problems.append("docker_selfupdate missing")
    else:
        for fn in ("trigger_self_recreate", "run_image_selftest", "evaluate_update_attempt", "write_update_attempt"):
            if not callable(getattr(dsu, fn, None)):
                problems.append(f"docker_selfupdate.{fn} missing")
    for fn in ("worker_loop", "heartbeat_loop", "held_jobs_upload_loop", "process_job_thread", "evaluate_update_attempt_on_start"):
        if not callable(globals().get(fn)):
            problems.append(f"{fn} missing")
    try:
        with open(os.path.join(APP_DIR, "locales.js"), "r", encoding="utf-8-sig") as f:
            if not wc.parse_locales_js_text(f.read()):
                problems.append("locales.js empty")
    except Exception as e:
        problems.append(f"locales.js: {e}")
    if problems:
        print("HB-SELFTEST-FAIL " + "; ".join(problems)[:1500], flush=True)
        return 1
    print(f"{dsu.SELFTEST_OK_MARK} worker {CURRENT_VERSION}", flush=True)
    return 0


if __name__ == "__main__":
    if SELFTEST:
        os._exit(run_selftest())
    restore_held_jobs_on_startup()
    t_hb = threading.Thread(target=heartbeat_loop, daemon=True)
    t_hb.start()
    t_up = threading.Thread(target=held_jobs_upload_loop, daemon=True)
    t_up.start()
    try:
        worker_loop()
    finally:
        perform_graceful_shutdown()