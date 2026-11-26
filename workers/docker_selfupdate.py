# ==============================================================================
# HB CLUSTER - SHARED DOCKER SELF-UPDATER & GHCR REGISTRY CLIENT (docker_selfupdate.py)
# ==============================================================================
# Sdílený modul pro Manager (ghcr.io/maseklab/hb-manager) i Linux Worker
# (ghcr.io/maseklab/hb-worker). Implementuje čistou kontejnerovou aktualizaci
# ve stylu Watchtower:
#   1. Kontrola nové verze přes veřejné GHCR API (token -> manifest -> config blob
#      -> OCI label `org.opencontainers.image.version` + SHA256 digest obrazu).
#   2. `docker pull` + přetvoření vlastního kontejneru přes /var/run/docker.sock
#      pomocí krátkodobého pomocného kontejneru (rename -> create -> start,
#      s automatickým rollbackem původního kontejneru při chybě).
# Žádné stahování zdrojových .py souborů a žádné os.execv.
# ==============================================================================

import os
import re
import json
import shutil
import copy
import time
import socket
import http.client
import urllib.parse

import requests

DOCKER_SOCK_PATH = "/var/run/docker.sock"
GHCR_HOST = "ghcr.io"
OCI_VERSION_LABEL = "org.opencontainers.image.version"
SELF_UPDATER_LABEL = "hb.cluster.role"
_MODULE_START_EPOCH = time.time()


def safe_read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except (OSError, PermissionError, ValueError):
        return default


def _get_own_process_start_epoch() -> float:
    """Odhadne epoch startu PID 1 (nebo procesu) v Linux kontejneru pro párování s Docker Created."""
    try:
        stat_txt = safe_read_text("/proc/stat")
        btime_m = re.search(r"^btime\s+(\d+)", stat_txt, re.MULTILINE)
        p_txt = safe_read_text("/proc/1/stat")
        if btime_m and p_txt:
            btime = float(btime_m.group(1))
            rparen = p_txt.rfind(")")
            if rparen != -1:
                fields = p_txt[rparen + 2:].split()
                if len(fields) > 19 and fields[19].isdigit():
                    hz = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0
                    if hz > 0:
                        return btime + (float(fields[19]) / hz)
    except (OSError, PermissionError, ValueError):
        pass
    try:
        return float(os.stat("/proc/1").st_ctime)
    except (OSError, PermissionError):
        return _MODULE_START_EPOCH

MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def docker_sock_available() -> bool:
    return hasattr(socket, "AF_UNIX") and os.path.exists(DOCKER_SOCK_PATH)


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, unix_socket_path: str, timeout: float = 120.0):
        super().__init__("localhost", timeout=timeout)
        self.unix_socket_path = unix_socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_socket_path)
        self.sock = sock


def docker_sock_responding(timeout: float = 5.0) -> bool:
    """True, když Docker přes docker.sock opravdu odpovídá (GET /_ping). Soubor socketu může existovat,
    ale být „mrtvý“ – typicky po restartu / aktualizaci Dockeru se zapnutým live-restore."""
    if not docker_sock_available():
        return False
    try:
        st, _ = docker_api_request("GET", "/_ping", timeout=timeout)
        return int(st) == 200
    except Exception:
        return False


def docker_api_request(method: str, path: str, body=None, timeout: float = 120.0, user_agent: str = "HB-Cluster",
                       raw: bool = False):
    """Dotaz na Docker API přes docker.sock. Vrací (status, JSON | text); s raw=True (status, bytes)."""
    if not docker_sock_available():
        raise RuntimeError("Docker socket /var/run/docker.sock is not available")
    conn = _UnixSocketHTTPConnection(DOCKER_SOCK_PATH, timeout=timeout)
    try:
        headers = {"Host": "localhost", "User-Agent": user_agent}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8") if not isinstance(body, (bytes, bytearray)) else body
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw_body = resp.read()
        status = resp.status
        if raw:
            return status, raw_body
        text_body = raw_body.decode("utf-8", errors="replace") if raw_body else ""
        parsed = None
        if text_body:
            try:
                parsed = json.loads(text_body)
            except Exception:
                parsed = text_body
        return status, parsed
    finally:
        try:
            conn.close()
        except Exception:
            pass


_PULL_DONE_STATES = ("pull complete", "already exists")
_PULL_LAYER_STATES = (
    "pulling fs layer", "waiting", "downloading", "verifying checksum",
    "download complete", "extracting", "pull complete", "already exists",
)


def docker_pull_with_progress(repo: str, tag: str, progress_fn=None, timeout: float = 900.0,
                              user_agent: str = "HB-Cluster"):
    """
    `docker pull` přes Docker API se streamovaným čtením JSON řádků (chunked) a průběžným
    hlášením postupu: progress_fn(fraction 0..1, info dict{layers_done, layers_total, mb_done, mb_total}).
    Vrací (http_status, error_text). error_text == "" znamená úspěch.
    """
    if not docker_sock_available():
        raise RuntimeError("Docker socket /var/run/docker.sock is not available")
    path = f"/images/create?fromImage={urllib.parse.quote(repo, safe='')}&tag={urllib.parse.quote(tag, safe='')}"
    conn = _UnixSocketHTTPConnection(DOCKER_SOCK_PATH, timeout=timeout)
    layers = {}
    err_txt = ""
    last_emit = 0.0
    try:
        conn.request("POST", path, body=None, headers={"Host": "localhost", "User-Agent": user_agent})
        resp = conn.getresponse()
        status = resp.status
        while True:
            line = resp.readline()
            if not line:
                break
            try:
                ev = json.loads(line.decode("utf-8", errors="replace").strip() or "{}")
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("error"):
                err_txt = str(ev.get("error"))[:300]
                continue
            lid = str(ev.get("id") or "")
            st_low = str(ev.get("status") or "").strip().lower()
            if not lid or not any(st_low.startswith(s) for s in _PULL_LAYER_STATES):
                continue
            lay = layers.setdefault(lid, {"cur": 0, "tot": 0, "done": False, "extract": 0.0})
            pd = ev.get("progressDetail") or {}
            if st_low.startswith("downloading"):
                lay["cur"] = int(pd.get("current") or lay["cur"] or 0)
                lay["tot"] = int(pd.get("total") or lay["tot"] or 0)
            elif st_low.startswith(("verifying checksum", "download complete")):
                if lay["tot"]:
                    lay["cur"] = lay["tot"]
            elif st_low.startswith("extracting"):
                if lay["tot"]:
                    lay["cur"] = lay["tot"]
                tot_e = float(pd.get("total") or 0)
                if tot_e > 0:
                    lay["extract"] = min(1.0, float(pd.get("current") or 0) / tot_e)
            if any(st_low.startswith(s) for s in _PULL_DONE_STATES):
                lay["done"] = True
                lay["extract"] = 1.0
                if lay["tot"]:
                    lay["cur"] = lay["tot"]
            now = time.time()
            if progress_fn and (now - last_emit) >= 0.5:
                last_emit = now
                _emit_pull_progress(layers, progress_fn)
        if progress_fn:
            _emit_pull_progress(layers, progress_fn, final=not err_txt)
        if status not in (200, 201) and not err_txt:
            err_txt = f"HTTP {status}"
        return status, err_txt
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _emit_pull_progress(layers: dict, progress_fn, final: bool = False):
    try:
        n = len(layers)
        done = sum(1 for v in layers.values() if v["done"])
        b_tot = sum(v["tot"] for v in layers.values())
        b_cur = sum(min(v["cur"], v["tot"]) if v["tot"] else 0 for v in layers.values())
        if final:
            frac = 1.0
        elif n == 0:
            frac = 0.0
        else:
            dl_frac = (b_cur / b_tot) if b_tot > 0 else (done / n)
            ex_frac = sum(v["extract"] for v in layers.values()) / n
            frac = max(0.0, min(0.99, 0.75 * dl_frac + 0.25 * ex_frac))
        progress_fn(frac, {
            "layers_done": done, "layers_total": n,
            "mb_done": round(b_cur / 1048576.0, 1), "mb_total": round(b_tot / 1048576.0, 1),
        })
    except Exception:
        pass


def detect_own_container_candidates() -> list:
    """
    Vrátí uspořádaný seznam možných ID/názvů vlastního kontejneru:
      1) 64znakové hex ID z /proc/self/mountinfo (/docker/containers/<id>/ nebo /containers/<id>/)
      2) 64znakové hex ID z /proc/self/cgroup (Cgroup v1/v2)
      3) env HOSTNAME a socket.gethostname()
    """
    cands = []
    seen = set()

    def _add(val: str):
        v = str(val or "").strip()
        if v and v not in seen:
            seen.add(v)
            cands.append(v)

    if os.path.isfile("/proc/self/mountinfo"):
        txt = safe_read_text("/proc/self/mountinfo")
        if txt:
            for pat in (
                r"([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)",
                r"/docker/containers/([0-9a-f]{64})/",
                r"/containers/([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)",
            ):
                for m in re.finditer(pat, txt):
                    _add(m.group(1))

    if os.path.isfile("/proc/self/cgroup"):
        txt = safe_read_text("/proc/self/cgroup")
        if txt:
            for pat in (
                r"/docker/([0-9a-f]{64})",
                r"docker-([0-9a-f]{64})\.scope",
                r"\b([0-9a-f]{64})\b",
            ):
                for m in re.finditer(pat, txt):
                    _add(m.group(1))

    _add(os.environ.get("HOSTNAME"))
    try:
        _add(socket.gethostname())
    except Exception:
        pass
    return cands


def split_image_ref(image_ref: str):
    """'ghcr.io/maseklab/hb-worker:latest' -> ('ghcr.io/maseklab/hb-worker', 'latest')."""
    ref = str(image_ref or "").strip()
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag or "latest"
    return ref, "latest"


def registry_path_from_ref(image_ref: str) -> str:
    """'ghcr.io/maseklab/hb-manager:latest' -> 'maseklab/hb-manager'."""
    repo, _ = split_image_ref(image_ref)
    if repo.startswith(GHCR_HOST + "/"):
        repo = repo[len(GHCR_HOST) + 1:]
    return repo.lower()


# ------------------------------------------------------------------------------
# KANÁLY :latest (větev main) a :dev (větev dev)
# ------------------------------------------------------------------------------
KNOWN_CHANNELS = ("latest", "dev")


def normalize_channel(tag: str) -> str:
    t = str(tag or "").strip().lower()
    return t if t in KNOWN_CHANNELS else "latest"


def detect_own_channel(user_agent: str = "HB-Cluster") -> str:
    """
    Zjistí, z jakého kanálu tento kontejner běží: proměnná HB_UPDATE_CHANNEL (override),
    jinak tag obrazu z `docker inspect` vlastního kontejneru (:dev -> 'dev', ostatní 'latest').
    Kontrola i stahování aktualizací pak probíhají vždy ze STEJNÉHO kanálu.
    """
    env_c = os.getenv("HB_UPDATE_CHANNEL", "").strip().lower()
    if env_c in KNOWN_CHANNELS:
        return env_c
    if docker_sock_available():
        try:
            info = inspect_own_container(user_agent=user_agent)
            if info:
                _, tag = split_image_ref((info.get("Config") or {}).get("Image") or "")
                return normalize_channel(tag)
        except Exception:
            pass
    return "latest"


def channel_image_ref(base_repo: str, channel: str) -> str:
    """('ghcr.io/maseklab/hb-worker', 'dev') -> 'ghcr.io/maseklab/hb-worker:dev'."""
    repo, _ = split_image_ref(base_repo)
    return f"{repo}:{normalize_channel(channel)}"


def version_tuple(v_str: str):
    try:
        s = str(v_str or "").strip().lstrip("vV")
        base, _, suf = s.partition("-")
        m_alpha = re.search(r"([A-Za-z]+)$", base)
        alpha = m_alpha.group(1).upper() if m_alpha else ""
        nums = tuple(int(x) for x in re.findall(r"\d+", base))
        # Semver pre-release: číselné části se porovnávají číselně (dev.9 < dev.10), text abecedně.
        pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                        for p in re.split(r"[.\-]", suf.lower()) if p)
        return (nums, 0 if suf else 1, alpha, pre_key)
    except Exception:
        return ((), 0, "", ())


def is_version_newer(remote: str, local: str) -> bool:
    r_t = version_tuple(remote)
    return bool(r_t[0]) and r_t > version_tuple(local)


# ------------------------------------------------------------------------------
# GHCR REGISTRY CLIENT (anonymní pull token pro veřejné obrazy)
# ------------------------------------------------------------------------------
# Šetření počítadla stažení na GitHubu: běžná kontrola používá jen `HEAD` na manifest
# (vrací hlavičku Docker-Content-Digest, nic se "nestahuje"). Plný `GET` manifestu a
# config blobu se provede jen tehdy, když se digest liší od posledního známého a zároveň
# neodpovídá lokálnímu obrazu (= opravdu vyšel nový build).
_REMOTE_INFO_CACHE = {}


def _detect_docker_arch() -> str:
    import platform as _pf
    m = (_pf.machine() or "").lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("armv7l", "armv7"):
        return "arm"
    return "amd64"


def ghcr_fetch_remote_image_info(image_ref: str, timeout: float = 12.0, user_agent: str = "HB-Cluster",
                                 platform_os: str = "linux", platform_arch: str = "",
                                 known_local: dict = None) -> dict:
    if not platform_arch:
        platform_arch = _detect_docker_arch()
    img_path = registry_path_from_ref(image_ref)
    _, tag = split_image_ref(image_ref)
    cache_key = f"{img_path}:{tag}"
    sess = requests.Session()
    sess.headers.update({"User-Agent": user_agent})
    try:
        tr = sess.get(
            f"https://{GHCR_HOST}/token",
            params={"service": GHCR_HOST, "scope": f"repository:{img_path}:pull"},
            timeout=timeout,
        )
        tr.raise_for_status()
        token = (tr.json() or {}).get("token") or (tr.json() or {}).get("access_token") or ""
        if not token:
            raise RuntimeError("GHCR did not return a pull token")
        auth = {"Authorization": f"Bearer {token}"}
        manifest_url = f"https://{GHCR_HOST}/v2/{img_path}/manifests/{tag}"

        # 1) HEAD – pouze digest
        head_digest = ""
        try:
            hr = sess.head(manifest_url, headers={**auth, "Accept": MANIFEST_ACCEPT}, timeout=timeout, allow_redirects=True)
            if hr.status_code == 404:
                raise RuntimeError(f"Image {img_path}:{tag} not found on GHCR")
            if hr.status_code == 200:
                head_digest = str(hr.headers.get("Docker-Content-Digest") or "").strip()
        except RuntimeError:
            raise
        except Exception:
            head_digest = ""

        if head_digest:
            cached = _REMOTE_INFO_CACHE.get(cache_key)
            if cached and cached.get("digest") == head_digest and cached.get("version"):
                return dict(cached)
            kl = known_local or {}
            if head_digest in (kl.get("digests") or []) and kl.get("version"):
                info = {"image": f"{GHCR_HOST}/{img_path}:{tag}", "version": str(kl["version"]), "digest": head_digest,
                        "config_digest": "", "created": "", "via": "head+local"}
                _REMOTE_INFO_CACHE[cache_key] = info
                return dict(info)

        # 2) Nový / neznámý digest -> plný GET manifestu + config blobu (načtení OCI labelu verze)
        mr = sess.get(
            manifest_url,
            headers={**auth, "Accept": MANIFEST_ACCEPT},
            timeout=timeout,
        )
        if mr.status_code == 404:
            raise RuntimeError(f"Image {img_path}:{tag} not found on GHCR")
        mr.raise_for_status()
        top_digest = str(mr.headers.get("Docker-Content-Digest") or head_digest or "").strip()
        manifest = mr.json() or {}

        if isinstance(manifest.get("manifests"), list):
            chosen = None
            for m in manifest["manifests"]:
                plat = m.get("platform") or {}
                if plat.get("os") == platform_os and plat.get("architecture") == platform_arch:
                    chosen = m
                    break
            if chosen is None:
                chosen = next((m for m in manifest["manifests"] if (m.get("platform") or {}).get("os") not in ("unknown", None)), None)
            if chosen is None:
                raise RuntimeError("No matching platform manifest in GHCR image index")
            mr2 = sess.get(
                f"https://{GHCR_HOST}/v2/{img_path}/manifests/{chosen.get('digest')}",
                headers={**auth, "Accept": MANIFEST_ACCEPT},
                timeout=timeout,
            )
            mr2.raise_for_status()
            manifest = mr2.json() or {}

        cfg_digest = str((manifest.get("config") or {}).get("digest") or "")
        version = ""
        created = ""
        if cfg_digest:
            br = sess.get(f"https://{GHCR_HOST}/v2/{img_path}/blobs/{cfg_digest}", headers=auth, timeout=timeout, allow_redirects=True)
            br.raise_for_status()
            cfg = br.json() or {}
            labels = ((cfg.get("config") or {}).get("Labels") or {})
            version = str(labels.get(OCI_VERSION_LABEL) or "").strip()
            created = str(cfg.get("created") or "")
        info = {
            "image": f"{GHCR_HOST}/{img_path}:{tag}",
            "version": version,
            "digest": top_digest,
            "config_digest": cfg_digest,
            "created": created,
            "via": "get",
        }
        if top_digest and version:
            _REMOTE_INFO_CACHE[cache_key] = info
        return dict(info)
    finally:
        sess.close()


# ------------------------------------------------------------------------------
# LOKÁLNÍ DOCKER INSPEKCE
# ------------------------------------------------------------------------------
def _format_docker_error(status: int, body) -> str:
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("error") or "").strip()
    elif isinstance(body, str):
        msg = body.strip()
    msg = re.sub(r"\s+", " ", msg)[:180]
    return f"HTTP {status}: {msg}" if msg else f"HTTP {status}"


def _detect_self_role_and_hints(target_image: str = "", user_agent: str = ""):
    ti = str(target_image or "").lower()
    ua = str(user_agent or "").lower()
    w_name = str(os.environ.get("WORKER_NAME") or "").strip()
    mgr_url = str(os.environ.get("MANAGER_URL") or "").strip()
    if "hb-worker" in ti or "worker" in ua or w_name or mgr_url:
        return "worker", w_name
    if "hb-manager" in ti or "manager" in ua:
        return "manager", ""
    return "manager", ""


def inspect_own_container_with_error(user_agent: str = "HB-Cluster", target_image: str = ""):
    """
    Spolehlivě najde a načte `docker inspect` vlastního kontejneru i na Synology NAS,
    Cgroup v2 nebo při `network_mode: host`.
    Vrací `(info_dict, "")` nebo `(None, detailní_popis_chyby)`.
    """
    if not docker_sock_available():
        return None, "Docker socket /var/run/docker.sock is not mounted or accessible"

    attempts = []
    for cid in detect_own_container_candidates():
        try:
            st, info = docker_api_request(
                "GET", f"/containers/{urllib.parse.quote(cid, safe='')}/json",
                timeout=20.0, user_agent=user_agent,
            )
            if st == 200 and isinstance(info, dict) and info.get("Id"):
                return info, ""
            attempts.append(f"direct({cid[:12]})={_format_docker_error(st, info)}")
        except Exception as e:
            attempts.append(f"direct({cid[:12]})={e}")

    # Spolehlivý fallback přes seznam běžících kontejnerů: GET /containers/json
    role, w_name = _detect_self_role_and_hints(target_image=target_image, user_agent=user_agent)
    try:
        l_st, l_body = docker_api_request("GET", "/containers/json", timeout=20.0, user_agent=user_agent)
    except Exception as e:
        attempts.append(f"list={e}")
        return None, "; ".join(attempts)[:240]

    if l_st != 200 or not isinstance(l_body, list):
        attempts.append(f"list={_format_docker_error(l_st, l_body)}")
        return None, "; ".join(attempts)[:240]

    own_start_ep = _get_own_process_start_epoch()
    scored = []
    for c in l_body:
        if not isinstance(c, dict):
            continue
        labels = c.get("Labels") or {}
        if labels.get(SELF_UPDATER_LABEL) == "self-updater":
            continue
        names_l = [str(n or "").lstrip("/").lower() for n in (c.get("Names") or [])]
        img_l = str(c.get("Image") or "").lower()
        svc_l = str(labels.get("com.docker.compose.service") or "").lower()
        mounts_dest = {str(m.get("Destination") or "") for m in (c.get("Mounts") or []) if isinstance(m, dict)}

        score = 0
        if role == "manager":
            if any("hb-manager" in n for n in names_l):
                score += 60
            if "hb-manager" in img_l:
                score += 45
            if svc_l == "hb-manager":
                score += 40
            if "/data" in mounts_dest and "hb-worker" not in img_l and not any("hb-worker" in n for n in names_l):
                score += 25
        else:
            if w_name and any(w_name.lower() in n for n in names_l):
                score += 45
            if any("hb-worker" in n for n in names_l):
                score += 35
            if "hb-worker" in img_l:
                score += 30
            if "hb-worker" in svc_l:
                score += 25
            if "/inputs" in mounts_dest or "/outputs" in mounts_dest or "/data" in mounts_dest:
                score += 10

        try:
            c_created = float(c.get("Created") or 0)
            if score > 0 and c_created > 0 and own_start_ep > 0:
                diff_s = abs(own_start_ep - c_created)
                if diff_s <= 20.0:
                    score += 30
                elif diff_s <= 120.0:
                    score += 15
        except (TypeError, ValueError):
            pass

        if score > 0 and c.get("Id"):
            scored.append((score, str(c["Id"]), names_l))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        attempts.append(f"list_match=0/{len(l_body)} containers matched role={role}")
        return None, "; ".join(attempts)[:240]

    best_info = None
    best_err = ""
    for _, cand_id, _ in scored[:5]:
        try:
            st, info = docker_api_request(
                "GET", f"/containers/{urllib.parse.quote(cand_id, safe='')}/json",
                timeout=20.0, user_agent=user_agent,
            )
            if st == 200 and isinstance(info, dict) and info.get("Id"):
                if role == "worker" and w_name:
                    env_list = (info.get("Config") or {}).get("Env") or []
                    if f"WORKER_NAME={w_name}" in env_list:
                        return info, ""
                    if best_info is None:
                        best_info = info
                else:
                    return info, ""
            else:
                best_err = _format_docker_error(st, info)
        except Exception as e:
            best_err = str(e)

    if best_info is not None:
        return best_info, ""
    if best_err:
        attempts.append(f"fallback_inspect={best_err}")
    return None, "; ".join(attempts)[:240]


def inspect_own_container(user_agent: str = "HB-Cluster", target_image: str = ""):
    info, _ = inspect_own_container_with_error(user_agent=user_agent, target_image=target_image)
    return info


def get_own_image_info(user_agent: str = "HB-Cluster") -> dict:
    """Vrátí ID, RepoDigests a OCI verzi obrazu, ze kterého běží tento kontejner."""
    info = inspect_own_container(user_agent=user_agent)
    if not info:
        return {}
    img_id = str(info.get("Image") or "")
    st, im = docker_api_request("GET", f"/images/{urllib.parse.quote(img_id, safe=':')}/json", timeout=20.0, user_agent=user_agent)
    if st != 200 or not isinstance(im, dict):
        return {"id": img_id, "repo_digests": [], "version": ""}
    labels = ((im.get("Config") or {}).get("Labels") or {})
    digests = [str(d).split("@", 1)[-1] for d in (im.get("RepoDigests") or []) if "@" in str(d)]
    return {"id": img_id, "repo_digests": digests, "version": str(labels.get(OCI_VERSION_LABEL) or "")}


def _strip_old_image_defaults(container_cfg: dict, old_image_cfg: dict, short_id: str) -> dict:
    """
    Watchtower-style: z konfigurace kontejneru odstraní hodnoty zděděné ze STARÉHO
    obrazu (labely, ENV, CMD, ENTRYPOINT, WORKDIR...), aby nový obraz uplatnil své
    vlastní výchozí hodnoty (např. nový label org.opencontainers.image.version).
    Uživatelské hodnoty (WORKER_NAME, MANAGER_URL, compose labely...) zůstanou.
    """
    cfg = copy.deepcopy(container_cfg or {})
    img = old_image_cfg or {}

    img_labels = img.get("Labels") or {}
    if isinstance(cfg.get("Labels"), dict):
        cfg["Labels"] = {k: v for k, v in cfg["Labels"].items() if img_labels.get(k) != v}

    img_env = set(img.get("Env") or [])
    if isinstance(cfg.get("Env"), list):
        cfg["Env"] = [e for e in cfg["Env"] if e not in img_env]

    for key in ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "StopSignal", "ExposedPorts", "Volumes", "OnBuild", "Shell"):
        if key in cfg and cfg.get(key) == img.get(key):
            cfg.pop(key, None)

    if str(cfg.get("Hostname") or "") == short_id:
        cfg.pop("Hostname", None)
    cfg.pop("Image", None)
    return cfg


COMPOSE_IMAGE_LABEL = "com.docker.compose.image"


def _refresh_compose_image_label(create_spec: dict, new_image_id: str) -> bool:
    """Docker Compose stores the image ID of a container in the label com.docker.compose.image.
    The recreated container gets the labels of the old one, so the label has to point to the NEW image:
    otherwise `docker compose up -d` recreates the container again and tools like Arcane or Portainer
    look for the old (already removed) image and show a bare sha256 instead of the version."""
    labels = create_spec.get("Labels")
    if not new_image_id or not isinstance(labels, dict) or COMPOSE_IMAGE_LABEL not in labels:
        return False
    labels[COMPOSE_IMAGE_LABEL] = new_image_id
    return True


# ------------------------------------------------------------------------------
# O1: OVĚŘENÁ AKTUALIZACE S NÁVRATEM
# ------------------------------------------------------------------------------
# 1. Před přetvořením se nový obraz zkusmo načte v dočasném kontejneru bez sítě a svazků (--selftest).
# 2. Starý proces zapíše do své datové složky záznam o pokusu (update_attempt.json: z verze → na verzi).
#    Složku sdílí starý i nový kontejner, takže po startu každá verze pozná, jak pokus dopadl:
#      - nová verze (to == moje)  → aktualizace proběhla; záznam smaže až po 5 min běhu,
#      - stará verze (from == moje) → pomocník novou verzi vrátil → další pokus až za 6 h.
# 3. Pomocník po startu nového kontejneru do 60 s ověří, že běží (Manager: odpovídá HTTP :8383,
#    worker: značka /tmp/hb_worker_ready nebo běh bez pádu po celou dobu). Jinak nový smaže,
#    starý vrátí pod původním jménem a do jeho datové složky zapíše důvod (update_result.json).
# 4. Po ověření smaže předchozí obraz, pokud zůstal bez názvu (tagu) a nepoužívá ho jiný kontejner (O2).
UPDATE_ATTEMPT_FILE = "update_attempt.json"
UPDATE_RESULT_FILE = "update_result.json"
UPDATE_RETRY_AFTER_ROLLBACK_S = 6 * 3600
UPDATE_ATTEMPT_KEEP_S = 300          # nová verze smaže záznam až po 5 min běhu (pomocník ověřuje 60 s)
UPDATE_VERIFY_TIMEOUT_S = 60
SELFTEST_OK_MARK = "HB-SELFTEST-OK"
SELFTEST_TIMEOUT_S = 90
WORKER_READY_FILE = "/tmp/hb_worker_ready"


def _same_version(a: str, b: str) -> bool:
    na, nb = str(a or "").strip().lstrip("vV"), str(b or "").strip().lstrip("vV")
    return bool(na) and na == nb


def write_update_attempt(dir_path: str, from_version: str, to_version: str, **extra) -> bool:
    """Zapíše záznam o pokusu o aktualizaci (atomicky). Starý výsledek pomocníka smaže."""
    if not dir_path:
        return False
    try:
        os.makedirs(dir_path, exist_ok=True)
        rec = {"from": str(from_version or ""), "to": str(to_version or ""), "ts": int(time.time())}
        rec.update({k: v for k, v in extra.items() if v is not None})
        p = os.path.join(dir_path, UPDATE_ATTEMPT_FILE)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        os.replace(p + ".tmp", p)
        try:
            os.remove(os.path.join(dir_path, UPDATE_RESULT_FILE))
        except OSError:
            pass
        return True
    except Exception:
        return False


def _read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def read_update_attempt(dir_path: str) -> dict:
    return _read_json_file(os.path.join(dir_path, UPDATE_ATTEMPT_FILE)) if dir_path else {}


def clear_update_attempt(dir_path: str):
    for fn in (UPDATE_ATTEMPT_FILE, UPDATE_RESULT_FILE):
        try:
            os.remove(os.path.join(dir_path, fn))
        except OSError:
            pass


def evaluate_update_attempt(dir_path: str, current_version: str, current_image_id: str = ""):
    """Po startu: jak dopadl poslední pokus o aktualizaci.
    Vrací (stav, záznam, výsledek pomocníka); stav = none | updated | failed | stale.
    Přednost má ID obrazu (pozná i nový build stejné verze), jinak se porovnají verze."""
    rec = read_update_attempt(dir_path)
    if not rec:
        return "none", {}, {}
    cur_img = str(current_image_id or "")
    if cur_img and rec.get("to_image") and rec.get("from_image"):
        if cur_img == rec.get("to_image"):
            return "updated", rec, {}
        if cur_img == rec.get("from_image"):
            return "failed", rec, _read_json_file(os.path.join(dir_path, UPDATE_RESULT_FILE))
        return "stale", rec, {}
    frm_same = _same_version(rec.get("from"), current_version)
    to_same = _same_version(rec.get("to"), current_version)
    if to_same and not frm_same:
        return "updated", rec, {}
    if frm_same and not to_same:
        return "failed", rec, _read_json_file(os.path.join(dir_path, UPDATE_RESULT_FILE))
    return "stale", rec, {}   # stejná verze (nový build) bez ID obrazu – nedá se poznat, nic neblokovat


def demux_docker_log(raw: bytes) -> list:
    """Výstup GET /containers/{id}/logs (bez TTY je proložený 8bajtovými hlavičkami) → seznam řádků."""
    data = bytes(raw or b"")
    out = b""
    if len(data) >= 8 and data[0] in (0, 1, 2) and data[1:4] == b"\x00\x00\x00":
        i = 0
        while i + 8 <= len(data):
            size = int.from_bytes(data[i + 4:i + 8], "big")
            out += data[i + 8:i + 8 + size]
            i += 8 + size
    else:
        out = data
    return [ln.rstrip("\r") for ln in out.decode("utf-8", errors="replace").split("\n") if ln.strip()]


def container_log_tail(container_id: str, lines: int = 40, user_agent: str = "HB-Cluster") -> list:
    try:
        st, raw = docker_api_request("GET", f"/containers/{urllib.parse.quote(container_id, safe='')}/logs?stdout=1&stderr=1&tail={int(lines)}",
                                     timeout=20.0, user_agent=user_agent, raw=True)
        return demux_docker_log(raw) if st == 200 else []
    except Exception:
        return []


def run_image_selftest(image_ref: str, cmd: list, env: list = None, timeout: float = SELFTEST_TIMEOUT_S,
                       user_agent: str = "HB-Cluster") -> dict:
    """O1: nová verze se zkusmo načte v samostatném dočasném kontejneru (bez sítě a bez svazků, nic nezapíše
    mimo sebe). Projde jen s návratovým kódem 0 a značkou HB-SELFTEST-OK ve výstupu.
    Vrací {"ok": bool, "detail": str, "log_tail": [řádky]}."""
    name = f"hb-selftest-{int(time.time() * 1000) % 1000000000}"
    spec = {
        "Image": image_ref,
        "Entrypoint": [str(c) for c in cmd],
        "Cmd": [],
        "Env": [str(e) for e in (env or []) if not str(e).startswith("HB_SELFTEST=")] + ["HB_SELFTEST=1"],
        "Labels": {SELF_UPDATER_LABEL: "self-updater", "com.centurylinklabs.watchtower.enable": "false"},
        "HostConfig": {"NetworkMode": "none", "AutoRemove": False},
    }
    st, body = docker_api_request("POST", f"/containers/create?name={name}", body=spec, timeout=30.0, user_agent=user_agent)
    if st not in (200, 201) or not isinstance(body, dict) or not body.get("Id"):
        return {"ok": False, "detail": "create " + _format_docker_error(st, body), "log_tail": []}
    cid = str(body["Id"])
    try:
        st, body = docker_api_request("POST", f"/containers/{cid}/start", timeout=30.0, user_agent=user_agent)
        if st not in (204, 304):
            return {"ok": False, "detail": "start " + _format_docker_error(st, body), "log_tail": []}
        code = None
        try:
            st, body = docker_api_request("POST", f"/containers/{cid}/wait?condition=not-running", timeout=float(timeout), user_agent=user_agent)
            if st == 200 and isinstance(body, dict) and body.get("StatusCode") is not None:
                code = int(body.get("StatusCode"))
        except Exception:
            code = None
        tail = container_log_tail(cid, 40, user_agent=user_agent)
        if code is None:
            return {"ok": False, "detail": f"timeout {int(timeout)} s", "log_tail": tail}
        if code != 0:
            return {"ok": False, "detail": f"exit code {code}", "log_tail": tail}
        if not any(SELFTEST_OK_MARK in ln for ln in tail):
            return {"ok": False, "detail": "no " + SELFTEST_OK_MARK, "log_tail": tail}
        return {"ok": True, "detail": "", "log_tail": tail}
    finally:
        try:
            docker_api_request("DELETE", f"/containers/{cid}?force=true&v=true", timeout=30.0, user_agent=user_agent)
        except Exception:
            pass


_HELPER_SCRIPT = r'''
import io, os, json, time, shutil, socket, tarfile, http.client, urllib.parse

class UConn(http.client.HTTPConnection):
    def __init__(self, timeout=180):
        super().__init__("localhost", timeout=timeout)
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect("/var/run/docker.sock")
        self.sock = s

def req(m, p, b=None):
    c = UConn()
    try:
        h = {"Host": "localhost"}
        pl = None
        if b is not None:
            pl = json.dumps(b).encode("utf-8")
            h["Content-Type"] = "application/json"
            h["Content-Length"] = str(len(pl))
        c.request(m, p, body=pl, headers=h)
        r = c.getresponse()
        d = r.read().decode("utf-8", errors="replace")
        return r.status, d
    except Exception as e:
        return 0, str(e)
    finally:
        c.close()

def migrate_job_temp(old_cid, src_path, dst_cnt_temp="/data/temp"):
    c = UConn(timeout=1800)
    try:
        c.request("GET", f"/containers/{old_cid}/archive?path={q(src_path)}", headers={"Host": "localhost"})
        r = c.getresponse()
        if r.status != 200:
            print(f"[hb-self-updater] temp archive HTTP {r.status}, skipping migration", flush=True)
            return True
        copied = 0
        touched_meta = []
        with tarfile.open(fileobj=r, mode="r|") as tf:
            for mem in tf:
                rel_raw = str(mem.name or "").lstrip("./")
                if "/" not in rel_raw:
                    continue
                _, sub = rel_raw.split("/", 1)
                sub = sub.lstrip("/")
                if not sub or not sub.startswith("job_"):
                    continue
                parts = [p for p in sub.split("/") if p and p != "."]
                if not parts or any(p == ".." for p in parts):
                    continue
                dst_root = "/mnt_hb_data/temp" if os.path.isdir("/mnt_hb_data") else "/mnt_hb_temp"
                dst_p = os.path.join(dst_root, *parts)
                if mem.isdir():
                    os.makedirs(dst_p, exist_ok=True)
                elif mem.isreg():
                    os.makedirs(os.path.dirname(dst_p), exist_ok=True)
                    sf = tf.extractfile(mem)
                    if sf is not None:
                        with sf, open(dst_p, "wb") as wf:
                            shutil.copyfileobj(sf, wf, length=1024 * 1024)
                        copied += 1
                        if parts[-1] == "held_job_meta.json":
                            touched_meta.append((parts[0], dst_p))
        for j_folder, mp in touched_meta:
            try:
                with open(mp, "r", encoding="utf-8") as rf:
                    md = json.load(rf)
                if isinstance(md, dict):
                    new_jdir = dst_cnt_temp.rstrip("/") + "/" + j_folder
                    md["job_temp_dir"] = new_jdir
                    if md.get("w_dest"):
                        md["w_dest"] = new_jdir + "/" + os.path.basename(str(md["w_dest"]))
                    with open(mp, "w", encoding="utf-8") as wf:
                        json.dump(md, wf, indent=2)
            except Exception:
                pass
        print(f"[hb-self-updater] migrated {copied} file(s) from {src_path} to {dst_cnt_temp}", flush=True)
        return True
    except Exception as e:
        print(f"[hb-self-updater] temp migration failed: {e}", flush=True)
        return False
    finally:
        c.close()

def jload(s):
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def demux(raw):
    out = b""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        i = 0
        while i + 8 <= len(raw):
            n = int.from_bytes(raw[i + 4:i + 8], "big")
            out += raw[i + 8:i + 8 + n]
            i += 8 + n
    else:
        out = raw
    return [ln.rstrip("\r") for ln in out.decode("utf-8", errors="replace").split("\n") if ln.strip()][-30:]

def log_tail(cid):
    c = UConn(timeout=20)
    try:
        c.request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail=30", headers={"Host": "localhost"})
        return demux(c.getresponse().read())
    except Exception:
        return []
    finally:
        c.close()

def exec_ok(cid, cmd):
    st, body = req("POST", f"/containers/{cid}/exec", {"Cmd": cmd, "AttachStdout": False, "AttachStderr": False})
    eid = jload(body).get("Id") if st in (200, 201) else None
    if not eid:
        return False
    st, _ = req("POST", f"/exec/{eid}/start", {"Detach": True, "Tty": False})
    if st not in (200, 204):
        return False
    for _ in range(25):
        st, body = req("GET", f"/exec/{eid}/json")
        d = jload(body)
        if st == 200 and not d.get("Running"):
            return d.get("ExitCode") == 0
        time.sleep(0.3)
    return False

def verify(cid):
    # O1: nová verze musí do HB_VERIFY_TIMEOUT s běžet (bez restartů); Manager navíc odpovídat na HTTP :8383
    t_end = time.time() + verify_s
    while True:
        st, body = req("GET", f"/containers/{cid}/json")
        d = jload(body) if st == 200 else {}
        s = d.get("State") or {}
        if not s:
            return False, f"inspect HTTP {st}"
        if s.get("Restarting") or int(d.get("RestartCount") or 0) > 0:
            return False, f"restarting (exit code {s.get('ExitCode')})"
        if not s.get("Running"):
            return False, f"exited (exit code {s.get('ExitCode')})"
        if role == "manager":
            if exec_ok(cid, ["curl", "-fsS", "-o", "/dev/null", "--max-time", "4", check_url]) or \
               exec_ok(cid, ["python3", "-c", "import urllib.request as u; u.urlopen('" + check_url + "', timeout=4)"]):
                return True, "http"
        else:
            hs, _ = req("HEAD", f"/containers/{cid}/archive?path={q(ready_file)}")
            if hs == 200:
                return True, "ready"
        if time.time() >= t_end:
            if role == "manager":
                return False, f"no HTTP answer on :8383 within {verify_s} s"
            return True, "running"   # starší verze bez značky: běžela celou dobu bez pádu
        time.sleep(2)

def put_result(cid, res):
    # důvod návratu do datové složky STARÉHO kontejneru (jeho proces ho po startu načte a zapíše do logu)
    if not res_dir:
        return 0
    data = json.dumps(res, ensure_ascii=False).encode("utf-8")
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as tf:
        ti = tarfile.TarInfo("update_result.json")
        ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o600
        tf.addfile(ti, io.BytesIO(data))
    pl = bio.getvalue()
    c = UConn(timeout=30)
    try:
        c.request("PUT", f"/containers/{cid}/archive?path={q(res_dir)}", body=pl,
                  headers={"Host": "localhost", "Content-Type": "application/x-tar", "Content-Length": str(len(pl))})
        r = c.getresponse()
        r.read()
        return r.status
    except Exception:
        return 0
    finally:
        c.close()

def remove_old_image(new_cid):
    # O2: předchozí obraz smazat, jen když po stažení nového zůstal bez názvu (tagu); Docker ho navíc
    # sám nesmaže, dokud ho používá jakýkoli jiný kontejner (409) – pak zůstane.
    if not old_img:
        return
    new_img = jload(req("GET", f"/containers/{new_cid}/json")[1]).get("Image") or ""
    if not new_img or new_img == old_img:
        return
    ist, ibody = req("GET", f"/images/{qi(old_img)}/json")
    if ist != 200:
        return
    tags = [t for t in (jload(ibody).get("RepoTags") or []) if t and t != "<none>:<none>"]
    if tags:
        print(f"[hb-self-updater] previous image {old_img[:19]} still tagged {tags[0]} - kept", flush=True)
        return
    dst, dbody = req("DELETE", f"/images/{qi(old_img)}")
    print(f"[hb-self-updater] previous image {old_img[:19]} removed (HTTP {dst})" if dst == 200 else
          f"[hb-self-updater] previous image {old_img[:19]} kept (HTTP {dst}: {str(dbody)[:120]})", flush=True)

q = lambda s: urllib.parse.quote(s, safe="")
qi = lambda s: urllib.parse.quote(s, safe=":")
time.sleep(2)
old_id = os.environ["HB_OLD_ID"]
name = os.environ.get("HB_CONTAINER_NAME", "")
spec = json.loads(os.environ["HB_CREATE_SPEC"])
extra = json.loads(os.environ.get("HB_EXTRA_NETWORKS") or "{}")
stop_t = int(os.environ.get("HB_STOP_TIMEOUT") or "30")
mig_src = os.environ.get("HB_MIGRATE_TEMP_SRC", "").strip()
mig_dst = os.environ.get("HB_MIGRATE_TEMP_DST", "/data/temp").strip() or "/data/temp"
cleanup_vols = [v.strip() for v in (os.environ.get("HB_CLEANUP_VOLUMES") or "").split(",") if v.strip()]
role = os.environ.get("HB_ROLE", "worker")
verify_s = int(os.environ.get("HB_VERIFY_TIMEOUT") or "60")
old_img = os.environ.get("HB_OLD_IMAGE_ID", "")
res_dir = os.environ.get("HB_RESULT_DIR", "")
target_v = os.environ.get("HB_TARGET_VERSION", "")
ready_file = os.environ.get("HB_READY_FILE") or "/tmp/hb_worker_ready"
check_url = os.environ.get("HB_CHECK_URL") or "http://127.0.0.1:8383/api/update/version"
print(f"[hb-self-updater] stopping {name or old_id[:12]} ...", flush=True)
req("POST", f"/containers/{old_id}/stop?t={stop_t}")
mig_ok = migrate_job_temp(old_id, mig_src, mig_dst) if mig_src else True
renamed = False
st, body, new_id, ok = 0, "", None, False
fail = None
if mig_ok:
    if name:
        st, _ = req("POST", f"/containers/{old_id}/rename?name={q(name + '-hbold-' + str(int(time.time())))}")
        renamed = st in (200, 204)
    if name and not renamed:
        fail = {"ok": False, "stage": "rename", "reason": f"Could not rename old container '{name}' (HTTP {st})", "log_tail": []}
        print(f"[hb-self-updater] rename failed (HTTP {st}) -> rollback", flush=True)
    else:
        qn = f"?name={q(name)}" if (name and renamed) else ""
        st, body = req("POST", f"/containers/create{qn}", spec)
        if st in (200, 201):
            new_id = jload(body).get("Id")
        if new_id:
            for net_name, ep in extra.items():
                req("POST", f"/networks/{q(net_name)}/connect", {"Container": new_id, "EndpointConfig": ep or {}})
            st2, body2 = req("POST", f"/containers/{new_id}/start")
            ok = st2 in (204, 304)
            if not ok:
                st, body = st2, body2
if ok:
    print(f"[hb-self-updater] new container {new_id[:12]} started, verifying (max {verify_s} s) ...", flush=True)
    good, why = verify(new_id)
    if good:
        print(f"[hb-self-updater] new container verified ({why}), removing old {old_id[:12]}", flush=True)
        req("DELETE", f"/containers/{old_id}?force=true")
        for vn in cleanup_vols:
            vst, _ = req("DELETE", f"/volumes/{q(vn)}")
            print(f"[hb-self-updater] removed legacy volume {vn} (HTTP {vst})", flush=True)
        remove_old_image(new_id)
    else:
        tail = log_tail(new_id)
        print(f"[hb-self-updater] new container failed verification: {why} -> rollback", flush=True)
        for ln in tail[-10:]:
            print(f"[hb-self-updater]   | {ln}", flush=True)
        fail = {"ok": False, "stage": "verify", "reason": why, "log_tail": tail}
        req("POST", f"/containers/{new_id}/stop?t=10")
        ok = False
if not ok:
    if fail is None:
        fail = {"ok": False, "stage": "create" if mig_ok else "migrate", "reason": f"HTTP {st}: {str(body)[:300]}", "log_tail": []}
        print(f"[hb-self-updater] create/start/migration failed (HTTP {st}: {str(body)[:300]}) -> rollback", flush=True)
    if new_id:
        req("DELETE", f"/containers/{new_id}?force=true")
    if renamed:
        req("POST", f"/containers/{old_id}/rename?name={q(name)}")
    fail.update({"to": target_v, "ts": int(time.time())})
    put_result(old_id, fail)
    req("POST", f"/containers/{old_id}/start")
'''


def trigger_self_recreate(target_image: str, log_fn=None, user_agent: str = "HB-Cluster",
                          stop_timeout: int = 30, require_change: bool = True, progress_fn=None,
                          selftest_cmd: list = None, attempt_dir: str = "", current_version: str = "",
                          verify_role: str = "", verify_timeout: int = UPDATE_VERIFY_TIMEOUT_S) -> dict:
    """
    Stáhne `target_image` a přetvoří vlastní kontejner (stejný název, sítě, volume,
    porty, ENV, compose labely). Vrací dict {ok, reason, ...}.
    reason: no_docker_sock | inspect_failed | pull_failed | image_unchanged | selftest_failed | helper_failed
    progress_fn(step:int 1..4, percent:float 0..100, info:dict) – volitelné hlášení postupu:
      1 = kontrola obrazu, 2 = docker pull (vrstvy), 3 = příprava přetvoření, 4 = restart kontejneru.
    O1: selftest_cmd = příkaz, který novou verzi zkusmo načte (např. ["python", "/app/main.py", "--selftest"]);
    attempt_dir = datová složka pro záznam o pokusu (update_attempt.json) a výsledek pomocníka;
    verify_role = "manager" (ověří HTTP :8383) | "worker" (značka /tmp/hb_worker_ready nebo běh bez pádu).
    """
    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    def _prog(step: int, pct: float, info: dict = None):
        if progress_fn:
            try:
                progress_fn(int(step), float(max(0.0, min(100.0, pct))), dict(info or {}))
            except Exception:
                pass

    if not docker_sock_available():
        return {"ok": False, "reason": "no_docker_sock"}

    _prog(1, 2.0)
    info, inspect_err = inspect_own_container_with_error(user_agent=user_agent, target_image=target_image)
    if not info:
        return {"ok": False, "reason": "inspect_failed", "detail": inspect_err or "container not found"}

    full_id = str(info.get("Id") or "")
    short_id = full_id[:12]
    c_name = str(info.get("Name") or "").lstrip("/")
    orig_cfg = info.get("Config") or {}
    host_cfg = copy.deepcopy(info.get("HostConfig") or {})
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    cur_image_id = str(info.get("Image") or "")

    repo, tag = split_image_ref(target_image or orig_cfg.get("Image") or "")
    target_ref = f"{repo}:{tag}"

    _log(f"[SELF-UPDATE] docker pull {target_ref} ...")
    _prog(2, 5.0, {"layers_done": 0, "layers_total": 0, "mb_done": 0.0, "mb_total": 0.0})
    p_code, pull_err = docker_pull_with_progress(
        repo, tag,
        progress_fn=lambda frac, inf: _prog(2, 5.0 + 75.0 * frac, inf),
        timeout=900.0, user_agent=user_agent,
    )
    if pull_err or p_code not in (200, 201):
        return {"ok": False, "reason": "pull_failed", "detail": pull_err or f"HTTP {p_code}"}
    _prog(3, 82.0)

    im_code, im_info = docker_api_request("GET", f"/images/{urllib.parse.quote(target_ref, safe=':/')}/json", timeout=20.0, user_agent=user_agent)
    new_image_id = str((im_info or {}).get("Id") or "") if isinstance(im_info, dict) else ""
    new_version = str((((im_info or {}).get("Config") or {}).get("Labels") or {}).get(OCI_VERSION_LABEL) or "") if isinstance(im_info, dict) else ""
    if require_change and new_image_id and new_image_id == cur_image_id:
        return {"ok": False, "reason": "image_unchanged", "version": new_version}

    old_img_cfg = {}
    if cur_image_id:
        oi_code, oi_info = docker_api_request("GET", f"/images/{urllib.parse.quote(cur_image_id, safe=':')}/json", timeout=20.0, user_agent=user_agent)
        if oi_code == 200 and isinstance(oi_info, dict):
            old_img_cfg = oi_info.get("Config") or {}

    create_spec = _strip_old_image_defaults(orig_cfg, old_img_cfg, short_id)
    create_spec["Image"] = target_ref
    _refresh_compose_image_label(create_spec, new_image_id)

    orig_env = [str(e) for e in (orig_cfg.get("Env") or [])]
    orig_role = str(((orig_cfg.get("Labels") or {}).get(SELF_UPDATER_LABEL)) or "").lower()
    is_worker_container = (
        orig_role == "worker"
        or "hb-worker" in target_ref.lower()
        or any(e.startswith("WORKER_NAME=") for e in orig_env)
    )
    migrate_temp_src = ""
    migrate_temp_dst = "/data/temp"
    helper_binds = []
    cleanup_volumes = []

    if is_worker_container:
        safe_cname = re.sub(r"[^a-zA-Z0-9_.-]+", "_", c_name or short_id).strip("._-") or short_id
        legacy_vol_Set = {f"hb_temp_{safe_cname}"}

        def _extract_bind_src_dst(bind_str: str):
            pts = str(bind_str or "").split(":")
            if len(pts) < 2:
                return "", ""
            last_seg = pts[-1].strip()
            dst = pts[-2] if (len(pts) >= 3 and not last_seg.startswith("/")) else last_seg
            src = ":".join(pts[:-2]) if (len(pts) >= 3 and not last_seg.startswith("/")) else ":".join(pts[:-1])
            return src.strip(), (dst.strip().rstrip("/") or "/")

        # 1. Odebrat staré hb_temp_* svazky z HostConfig.Binds a HostConfig.Mounts nového kontejneru
        orig_binds = list(host_cfg.get("Binds") or [])
        cleaned_binds = []
        for b in orig_binds:
            b_src, b_dst = _extract_bind_src_dst(b)
            if b_src.startswith("hb_temp_"):
                legacy_vol_Set.add(b_src)
                continue
            cleaned_binds.append(b)
        if orig_binds or cleaned_binds:
            host_cfg["Binds"] = cleaned_binds

        if isinstance(host_cfg.get("Mounts"), list):
            cleaned_mounts = []
            for m in host_cfg["Mounts"]:
                if isinstance(m, dict) and str(m.get("Source") or "").startswith("hb_temp_"):
                    legacy_vol_Set.add(str(m.get("Source")))
                    continue
                cleaned_mounts.append(m)
            host_cfg["Mounts"] = cleaned_mounts

        data_host_src = ""
        data_cnt_dst = "/data"
        for m in (info.get("Mounts") or []):
            if not isinstance(m, dict):
                continue
            m_dst = str(m.get("Destination") or "").rstrip("/")
            m_name = str(m.get("Name") or "")
            m_src = str(m.get("Source") or "")
            if m_name.startswith("hb_temp_"):
                legacy_vol_Set.add(m_name)
            if m_dst in ("/data", "/app/data"):
                data_cnt_dst = m_dst
                data_host_src = m_name if (str(m.get("Type") or "") == "volume" and m_name) else m_src

        if not data_host_src:
            for b in cleaned_binds:
                b_src, b_dst = _extract_bind_src_dst(b)
                if b_dst in ("/data", "/app/data") and b_src:
                    data_cnt_dst = b_dst
                    data_host_src = b_src
                    break

        cleanup_volumes = sorted(legacy_vol_Set)
        migrate_temp_dst = f"{data_cnt_dst}/temp"

        # 2. Pokud jsou ve starém /temp držené úlohy job_*, migrovat je do /data/temp
        try:
            if os.path.isdir("/temp") and os.path.abspath("/temp") != os.path.abspath(migrate_temp_dst):
                has_jobs_in_temp = any(
                    n.startswith("job_") and os.path.isdir(os.path.join("/temp", n))
                    for n in os.listdir("/temp")
                )
                if has_jobs_in_temp:
                    copied_locally = False
                    if os.path.isdir(data_cnt_dst):
                        os.makedirs(migrate_temp_dst, exist_ok=True)
                        for n in os.listdir("/temp"):
                            s_p = os.path.join("/temp", n)
                            d_p = os.path.join(migrate_temp_dst, n)
                            if n.startswith("job_") and os.path.isdir(s_p) and not os.path.exists(d_p):
                                shutil.copytree(s_p, d_p)
                                mp = os.path.join(d_p, "held_job_meta.json")
                                if os.path.isfile(mp):
                                    try:
                                        with open(mp, "r", encoding="utf-8") as rf:
                                            md = json.load(rf)
                                        if isinstance(md, dict):
                                            md["job_temp_dir"] = d_p
                                            if md.get("w_dest"):
                                                md["w_dest"] = os.path.join(d_p, os.path.basename(str(md["w_dest"])))
                                            with open(mp, "w", encoding="utf-8") as wf:
                                                json.dump(md, wf, indent=2)
                                    except Exception:
                                        pass
                        copied_locally = True
                    if not copied_locally and data_host_src:
                        migrate_temp_src = "/temp"
                        if data_host_src.startswith("/"):
                            helper_binds.append(f"{data_host_src.rstrip('/')}/temp:/mnt_hb_temp")
                        else:
                            # Docker named volume: připojit do /mnt_hb_data a rozbalit do /mnt_hb_data/temp
                            helper_binds.append(f"{data_host_src}:/mnt_hb_data")
                    if not migrate_temp_src and not copied_locally:
                        # Držené úlohy nemáme kam bezpečně přesunout -> starý svazek NESMAZAT.
                        cleanup_volumes = []
                        _log("[SELF-UPDATE] Held job_* folders in /temp could not be migrated (no /data mount) - legacy volumes are kept.")
                    else:
                        _log(f"[SELF-UPDATE] Migrating held job_* folders from /temp to {migrate_temp_dst} and scheduling cleanup of {', '.join(cleanup_volumes)}.")
        except Exception as mig_e:
            # Migrace selhala -> žádné mazání starých svazků, jinak by se ztratily hotové převody.
            cleanup_volumes = []
            migrate_temp_src = ""
            _log(f"[SELF-UPDATE] Migration of held jobs from /temp failed ({mig_e}) - legacy volumes are kept.")

    create_spec["HostConfig"] = host_cfg

    # O1: novou verzi nejdřív zkusmo načíst v dočasném kontejneru – když se nenačte, nic se nezastaví ani nepřetvoří.
    if selftest_cmd:
        _prog(3, 84.0)
        _log(f"[SELF-UPDATE] Self-test of v{new_version or '?'} in a temporary container ...")
        stt = run_image_selftest(target_ref, selftest_cmd, env=create_spec.get("Env") or [], user_agent=user_agent)
        if not stt.get("ok"):
            return {"ok": False, "reason": "selftest_failed", "detail": stt.get("detail") or "",
                    "log_tail": list(stt.get("log_tail") or [])[-15:], "version": new_version}
        _log(f"[SELF-UPDATE] Self-test of v{new_version or '?'} passed.")

    extra_networks = {}
    net_mode = str(host_cfg.get("NetworkMode") or "")
    if networks and net_mode not in ("host", "none") and not net_mode.startswith("container:"):
        def _ep(nv: dict) -> dict:
            aliases = [a for a in (nv.get("Aliases") or []) if a and a not in (short_id, full_id)]
            ep = {}
            if aliases:
                ep["Aliases"] = aliases
            if nv.get("IPAMConfig"):
                ep["IPAMConfig"] = nv.get("IPAMConfig")
            if nv.get("MacAddress"):
                ep["MacAddress"] = nv.get("MacAddress")
            if nv.get("Links"):
                ep["Links"] = nv.get("Links")
            return ep

        names = list(networks.keys())
        primary = net_mode if net_mode in networks else names[0]
        create_spec["NetworkingConfig"] = {"EndpointsConfig": {primary: _ep(networks[primary] or {})}}
        for n in names:
            if n != primary:
                extra_networks[n] = _ep(networks[n] or {})

    host_sock_src = DOCKER_SOCK_PATH
    for m in (info.get("Mounts") or []):
        if isinstance(m, dict):
            dst = str(m.get("Destination") or "").rstrip("/")
            if dst == DOCKER_SOCK_PATH or dst.endswith("docker.sock"):
                src = str(m.get("Source") or "").strip()
                if src:
                    host_sock_src = src
                    break

    helper_name = f"hb-self-updater-{short_id}-{int(time.time()) % 100000}"
    helper_spec = {
        "Image": target_ref,
        "Entrypoint": ["python3", "-c", _HELPER_SCRIPT],
        "Cmd": [],
        "Env": [
            f"HB_OLD_ID={full_id}",
            f"HB_CONTAINER_NAME={c_name}",
            f"HB_CREATE_SPEC={json.dumps(create_spec)}",
            f"HB_EXTRA_NETWORKS={json.dumps(extra_networks)}",
            f"HB_STOP_TIMEOUT={int(stop_timeout)}",
            f"HB_MIGRATE_TEMP_SRC={migrate_temp_src}",
            f"HB_MIGRATE_TEMP_DST={migrate_temp_dst}",
            f"HB_CLEANUP_VOLUMES={','.join(cleanup_volumes)}",
            f"HB_ROLE={verify_role or ('worker' if is_worker_container else 'manager')}",
            f"HB_VERIFY_TIMEOUT={int(verify_timeout)}",
            f"HB_OLD_IMAGE_ID={cur_image_id}",
            f"HB_RESULT_DIR={attempt_dir or ''}",
            f"HB_TARGET_VERSION={new_version or ''}",
            f"HB_READY_FILE={WORKER_READY_FILE}",
        ],
        "Labels": {SELF_UPDATER_LABEL: "self-updater", "com.centurylinklabs.watchtower.enable": "false"},
        "HostConfig": {
            "AutoRemove": True,
            "NetworkMode": "none",
            "Binds": [f"{host_sock_src}:{DOCKER_SOCK_PATH}"] + helper_binds,
        },
    }
    hc_code, hc_body = docker_api_request("POST", f"/containers/create?name={helper_name}", body=helper_spec, timeout=30.0, user_agent=user_agent)
    if hc_code not in (200, 201) or not isinstance(hc_body, dict) or not hc_body.get("Id"):
        return {"ok": False, "reason": "helper_failed", "detail": f"create HTTP {hc_code}: {str(hc_body)[:200]}"}
    _prog(3, 88.0)
    # záznam o pokusu až těsně před startem pomocníka (ten starý kontejner zastaví nejdřív za 2 s)
    if attempt_dir:
        write_update_attempt(attempt_dir, current_version, new_version or "", image=target_ref,
                             from_image=cur_image_id or None, to_image=new_image_id or None)
    hs_code, hs_body = docker_api_request("POST", f"/containers/{hc_body['Id']}/start", timeout=30.0, user_agent=user_agent)
    if hs_code not in (204, 304):
        if attempt_dir:
            clear_update_attempt(attempt_dir)
        try:
            docker_api_request("DELETE", f"/containers/{hc_body['Id']}?force=true", timeout=20.0, user_agent=user_agent)
        except Exception:
            pass
        return {"ok": False, "reason": "helper_failed", "detail": f"start HTTP {hs_code}: {str(hs_body)[:200]}"}

    _prog(4, 92.0)
    _log(f"[SELF-UPDATE] Image {target_ref} (v{new_version or '?'}) pulled — recreating container '{c_name or short_id}' via helper {helper_name}.")
    return {"ok": True, "recreating": True, "container_name": c_name, "version": new_version, "image_id": new_image_id}
