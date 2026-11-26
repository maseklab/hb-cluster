# ==============================================================================
# HB CLUSTER MANAGER - FASTAPI SERVER & ENDPOINTS (main.py)
# ==============================================================================

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from contextlib import contextmanager, asynccontextmanager
from collections import OrderedDict
import os
import re
import sys
import time
import json
import copy
import hmac
import hashlib
import secrets
import zipfile
import tempfile
import datetime
import threading
import urllib.parse
import ipaddress
from typing import Optional, List
import anyio
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import defer

import models
from database import SessionLocal, DATA_DIR, DATA_DIR_PERSISTENT, set_tx_immediate, SELFTEST
import manager_core as mc
import package_layout as pkg  # jediný seznam souborů Windows balíčku (workers/package_layout.py)
import security as sec       # volitelné přihlášení do webu (Nastavení → Zabezpečení)

VERSION = getattr(mc, "APP_VERSION", mc.FALLBACK_VERSION)
_enable_docs = str(os.getenv("HB_ENABLE_DOCS", "")).strip().lower() in ("1", "true", "yes")


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    await anyio.to_thread.run_sync(startup_event)
    try:
        yield
    finally:
        await anyio.to_thread.run_sync(shutdown_event)


fastapi_app = FastAPI(
    title="HB Cluster Manager",
    version=VERSION,
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    openapi_url="/openapi.json" if _enable_docs else None,
    lifespan=_app_lifespan,
)

# /static/* = loga a ikony (hbc-gradient.svg, hbm-gradient.svg, hbm.ico, hbw.ico, favicon.ico)
if os.path.isdir(mc.STATIC_DIR):
    fastapi_app.mount("/static", StaticFiles(directory=mc.STATIC_DIR), name="static")


# D1: komprese (gzip) jen pro web a JSON API – stav fronty má u tisíce úloh přes 1 MB. Nikdy ne pro relay (filmy),
# binárky, balíčky (.zip) ani OTA soubory: komprimovat 30GB proud za běhu by zbytečně zatěžovalo CPU.
from starlette.middleware.gzip import GZipMiddleware

GZIP_EXACT = {"/", "/app.js", "/locales.js", "/login", "/api/locales"}
GZIP_PREFIX = ("/api/v2/", "/static/vendor/")
GZIP_SKIP_PREFIX = ("/api/v2/workers/package/", "/api/v2/workers/download_windows_package", "/api/v2/binaries/upload/",
                    "/api/v2/backups/")


def gzip_allowed(path: str) -> bool:
    p = path or "/"
    if p.startswith(GZIP_SKIP_PREFIX):
        return False
    return p in GZIP_EXACT or p.startswith(GZIP_PREFIX)


class SelectiveGZipMiddleware:
    def __init__(self, app):
        self.app = app
        self.gz = GZipMiddleware(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and gzip_allowed(scope.get("path") or "/"):
            await self.gz(scope, receive, send)
        else:
            await self.app(scope, receive, send)


@fastapi_app.middleware("http")
async def add_nocache_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    p = request.url.path or ""
    if p in ("/", "/app.js", "/locales.js", "/favicon.ico") or p.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    # Bezpečnostní HTTP hlavičky (ochrana proti clickjackingu, MIME sniffingu, úniku Referer a XSS)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if p == "/login":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
    elif not p.startswith("/api/"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
    try:
        if sec.is_https(request, SEC):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")   # bez includeSubDomains: jiné služby na doméně nemusí mít HTTPS
    except Exception:
        pass
    return response


def _is_sqlite_lock_error(exc: Exception) -> bool:
    s = str(exc or "").lower()
    return "database is locked" in s or "database table is locked" in s or "sqlite_busy" in s


@contextmanager
def db_scope(commit: bool = False, immediate: Optional[bool] = None):
    use_imm = bool(commit) if immediate is None else bool(immediate)
    prev_imm = set_tx_immediate(use_imm)
    db = SessionLocal()
    try:
        yield db
        if commit:
            for attempt in range(4):
                try:
                    db.commit()
                    break
                except OperationalError as oe:
                    if _is_sqlite_lock_error(oe) and attempt < 3:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                    raise
    except HTTPException:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        mc.logger.error(f"DB scope error: {e}")
        raise
    finally:
        db.close()
        set_tx_immediate(prev_imm)


# ==============================================================================
# ZABEZPEČENÍ WEBU (krok 1): přihlášení do webového rozhraní, workerů se NETÝKÁ
# ==============================================================================
SEC = sec.SecurityStore(
    os.path.join(DATA_DIR, "security.json"),
    os.path.join(DATA_DIR, "reset_admin_password"),
    log=lambda m: mc.append_manager_log(m),
)
_sec_reset_checked_at = [0.0]
WREG = sec.WorkerRegistry(os.path.join(DATA_DIR, "worker_keys.json"), log=lambda m: mc.append_manager_log(m))
# ID tohoto Manageru (posílá se v každé odpovědi; workery se na něj navážou – viz worker_core.check_manager_identity)
# Při --selftest nic nezapisujeme na disk; reálné ID se načte / vytvoří v startup_event().
MANAGER_ID = (
    "selftest"
    if SELFTEST
    else sec.load_or_create_manager_id(os.path.join(DATA_DIR, "manager_identity.json"), log=lambda m: mc.append_manager_log(m))
)
mc.MANAGER_ID = MANAGER_ID   # odpověď UDP discovery (manager_core) posílá stejné ID
# Workery navázané na JINÝ Manager (hlásí se, ale odpovědi tohoto Manageru nepřijmou): jméno → údaje pro web
_bound_elsewhere = {}
_bound_elsewhere_lock = threading.Lock()


def _note_bound_elsewhere(name: str, ip: str, version: str, their_id: str) -> None:
    now = time.time()
    k = name or ip or "?"
    with _bound_elsewhere_lock:
        prev = _bound_elsewhere.get(k)
        if len(_bound_elsewhere) > 200 and not prev:
            return
        _bound_elsewhere[k] = {"name": name or "?", "ip": ip or "", "version": version or "",
                               "first_seen": (prev or {}).get("first_seen") or now, "last_seen": now}
    if not prev:
        mc.append_manager_log(mc.T("api_worker_bound_elsewhere", name or "?", ip or "?"))


def bound_elsewhere_list(max_age_s: float = 600.0) -> list:
    """Workery navázané na jiný Manager, které se ozvaly za posledních 10 minut (pro záložku Workery)."""
    now = time.time()
    with _bound_elsewhere_lock:
        for k in [k for k, v in _bound_elsewhere.items() if now - v["last_seen"] > 3600]:
            _bound_elsewhere.pop(k, None)
        return sorted((dict(v) for v in _bound_elsewhere.values() if now - v["last_seen"] <= max_age_s),
                      key=lambda v: v["name"].lower())


# Dvě instalace workeru pod STEJNÝM jménem (dva PC se stejným názvem, nebo přeinstalace jinam, zatímco stará kopie
# běží dál): každá má vlastní klíč (X-HB-Worker-Key). Přeinstalace = klíč se změní jednou; dvě živé kopie = klíče se
# střídají (A → B → A). Manager by jinak míchal jejich údaje i úlohy pod jedním jménem bez varování.
NAME_CLASH_WINDOW_S = 180.0
NAME_CLASH_MAX_NAMES = 50     # strop varování (ochrana paměti a logu)
NAME_CLASH_MAX_KEYS = 4       # na jedno jméno si pamatovat jen nejnovější klíče
_name_keys = {}           # jméno → {"last": otisk klíče, "seen": {otisk: (čas, ip, verze)}}
_name_clash = {}          # jméno → {"name", "since", "last_seen"}
_name_clash_lock = threading.Lock()


def _note_worker_key(name: str, key: str, ip: str, version: str = "", now: float | None = None) -> None:
    if not name or not key:
        return
    now = time.time() if now is None else now
    kh = hashlib.sha256(key.encode("utf-8", "ignore")).hexdigest()[:16]
    new_clash = ""
    with _name_clash_lock:
        ent = _name_keys.get(name)
        if ent is None:
            if len(_name_keys) >= 500:
                oldest = min(_name_keys, key=lambda n: max((v[0] for v in _name_keys[n]["seen"].values()), default=0))
                _name_keys.pop(oldest, None)
            ent = _name_keys[name] = {"last": "", "seen": {}}
        seen = ent["seen"]
        for k in [k for k, v in seen.items() if now - v[0] > NAME_CLASH_WINDOW_S]:
            seen.pop(k, None)
        if ent["last"] and ent["last"] != kh and kh in seen:
            c = _name_clash.get(name)
            if c is None:
                for n in [n for n, v in _name_clash.items() if now - v["last_seen"] > NAME_CLASH_WINDOW_S]:
                    _name_clash.pop(n, None)
                if len(_name_clash) < NAME_CLASH_MAX_NAMES:
                    c = _name_clash[name] = {"name": name, "since": now}
                    new_clash = ", ".join(sorted({v[1] or "?" for v in seen.values()} | {ip or "?"}))
            if c is not None:
                c["last_seen"] = now
        seen[kh] = (now, ip or "", version or "")
        if len(seen) > NAME_CLASH_MAX_KEYS:
            for k in sorted(seen, key=lambda k: seen[k][0])[:len(seen) - NAME_CLASH_MAX_KEYS]:
                seen.pop(k, None)
        ent["last"] = kh
    if new_clash:
        mc.append_manager_log(mc.T("api_worker_name_clash", name, new_clash))


def worker_name_clashes(now: float | None = None) -> list:
    """Jména, pod kterými se v posledních 3 minutách střídají dvě různé instalace (pro záložku Workery)."""
    now = time.time() if now is None else now
    out = []
    with _name_clash_lock:
        for n in [n for n, c in _name_clash.items() if now - c["last_seen"] > NAME_CLASH_WINDOW_S]:
            _name_clash.pop(n, None)
        for n, c in _name_clash.items():
            seen = (_name_keys.get(n) or {}).get("seen") or {}
            live = sorted((v for v in seen.values() if now - v[0] <= NAME_CLASH_WINDOW_S), key=lambda v: v[1])
            out.append({"name": n, "since": c["since"], "last_seen": c["last_seen"],
                        "installs": [{"ip": v[1], "version": v[2], "last_seen": v[0]} for v in live]})
    return sorted(out, key=lambda v: v["name"].lower())


def sync_worker_approval_with_signin(reason: str = "") -> None:
    """Přihlášení do webu a schvalování workerů platí jen SPOLU (rozhodnutí autora 2026-11-19).
    Zapnuté přihlášení = zapnuté schvalování (známé workery se schválí samy), vypnuté = vypnuté.
    Volá se při startu (sjednotí starší nastavení), po zapnutí / vypnutí přihlášení a po resetu hesla souborem."""
    try:
        if SEC.enabled and not WREG.approval:
            WREG.set_approval(True)
            mc.append_manager_log(f"[SECURITY] Schvalování workerů zapnuto spolu s přihlášením do webu ({reason or 'start'}).")
        elif not SEC.enabled and WREG.approval:
            WREG.set_approval(False)
            mc.append_manager_log(f"[SECURITY] Schvalování workerů vypnuto spolu s přihlášením do webu ({reason or 'start'}).")
    except Exception as e:
        mc.logger.warning(f"sync_worker_approval_with_signin failed: {e}")


_xs_log_last = OrderedDict()                         # (ip, cesta) → čas posledního zápisu do logu
_xs_log_lock = threading.Lock()


def _log_cross_site_refusal(why: str, method: str, path: str, ip: str) -> None:
    """Odmítnutý požadavek z cizího webu zapsat do logu Manageru – stejný zdroj a cesta nejvýš 1× za minutu."""
    k = (ip, path)
    now = time.time()
    with _xs_log_lock:
        if now - _xs_log_last.get(k, 0) < 60:
            return
        mc._lru_set(_xs_log_last, k, now, 500)
    try:
        mc.append_manager_log(f"[SECURITY] Odmítnut požadavek z cizího webu ({why}): {method} {path} z {ip}")
    except Exception:
        pass


@fastapi_app.middleware("http")
async def web_login_middleware(request: Request, call_next):
    now = time.time()
    if now - _sec_reset_checked_at[0] > 10:          # soubor pro obnovu hesla stačí hlídat jednou za 10 s
        _sec_reset_checked_at[0] = now
        if SEC.check_reset_flag():
            sync_worker_approval_with_signin("reset_admin_password")
    path = request.url.path or "/"
    xs_why = sec.cross_site_refusal(request.method, path, request.headers)
    if xs_why:
        _log_cross_site_refusal(xs_why, request.method, path, SEC.client_ip(request))
        return JSONResponse({"detail": "cross_site_blocked"}, status_code=403)
    w_key = request.headers.get("x-hb-worker-key") or ""
    w_name = urllib.parse.unquote(request.headers.get("x-hb-worker-name") or "")[:80]
    ua = request.headers.get("user-agent") or ""
    w_ver = ua.split("HB-Cluster-Worker/", 1)[1].split(" ")[0][:40] if "HB-Cluster-Worker/" in ua else ""
    bound_mid = (request.headers.get("x-hb-bound-manager") or "").strip()
    if bound_mid and bound_mid != MANAGER_ID:
        # worker navázaný na jiný Manager: odpověď by stejně nepřijal → nic nezpracovat (žádná úloha, žádná změna)
        _note_bound_elsewhere(w_name or (request.query_params.get("worker_name") or "")[:80], SEC.client_ip(request), w_ver, bound_mid)
        return JSONResponse({"detail": "worker_bound_elsewhere"}, status_code=409)
    if bound_mid and _bound_elsewhere:
        with _bound_elsewhere_lock:
            _bound_elsewhere.pop(w_name or "?", None)       # worker se mezitím navázal na tento Manager
    kind = sec.worker_guard_kind(path)
    request.state.is_worker_auth = False
    request.state.worker_bound_name = ""
    if WREG.approval and kind:
        # ZABEZPEČENÍ KROK 2: schvalování workerů je zapnuté
        web_ok = (not SEC.enabled) or SEC.is_authorized(request)
        if kind == "package":
            dl = request.query_params.get("dl") or ""
            if not ((SEC.enabled and SEC.is_authorized(request)) or WREG.join_valid(dl)):
                return JSONResponse({"detail": "download_link_expired"}, status_code=403)
        elif kind == "shared" and not w_key:
            if not web_ok:                            # bez klíče workeru = webové rozhraní → nepřihlášený na přihlášení
                return JSONResponse({"detail": "auth_required"}, status_code=401)
            # akce z webového rozhraní (pauza, příjem, politika workeru, ruční nahrání nástroje)
        else:
            try:
                st = WREG.check(w_key, w_name, SEC.client_ip(request), w_ver, request.headers.get("x-hb-join-token") or "")
            except Exception:
                st = "pending"
            if st != "ok":
                return JSONResponse({"detail": "worker_" + st}, status_code=403)
            request.state.is_worker_auth = True
            request.state.worker_bound_name = WREG.bound_name_for(w_key) or w_name
            try:
                _note_worker_key(request.state.worker_bound_name, w_key, SEC.client_ip(request), w_ver)
            except Exception:
                pass
    else:
        if w_key:                                     # klíče workerů zapisovat (schvalování vypnuté = nic se nevynucuje)
            try:
                WREG.touch(w_key, w_name, SEC.client_ip(request), w_ver)
            except Exception:
                pass
            request.state.is_worker_auth = True
            request.state.worker_bound_name = WREG.bound_name_for(w_key) or w_name
            if kind and len(w_key) >= 32:             # jen provoz workeru se skutečným klíčem (ne web, ne cizí pokusy)
                try:
                    _note_worker_key(request.state.worker_bound_name, w_key, SEC.client_ip(request), w_ver)
                except Exception:
                    pass
        if kind == "package" and SEC.enabled and not SEC.is_authorized(request) and not WREG.join_valid(request.query_params.get("dl") or ""):
            # se zapnutým přihlášením do webu: balíček (obsahuje SMB údaje) jen přihlášeným nebo přes odkaz z průvodce
            return JSONResponse({"detail": "download_link_expired"}, status_code=403)
    public_stats = (path == "/api/stats" and request.method == "GET" and SEC.data.get("public_stats", True))
    if SEC.enabled and not public_stats and not sec.is_public_path(path) and not SEC.is_authorized(request):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + urllib.parse.quote(nxt, safe=""), status_code=302)
    return await call_next(request)


def _set_session_cookie(resp, request: Request, token: str, remember: bool):
    resp.set_cookie(
        sec.SESSION_COOKIE, token,
        max_age=sec.REMEMBER_SEC if remember else None,
        httponly=True, samesite="lax", secure=sec.is_https(request, SEC), path="/",
    )


@fastapi_app.get("/login", response_class=HTMLResponse)
def serve_login_page(request: Request):
    if not SEC.enabled or SEC.is_authorized(request):
        return RedirectResponse("/", status_code=302)
    fp = os.path.join(mc.APP_BASE_DIR, "login.html")
    try:
        with open(fp, "r", encoding="utf-8-sig") as f:
            html = f.read()
    except Exception:
        html = "<html><body><h2>login.html not found</h2></body></html>"
    lang = "cs" if str(mc.AppState.ui_lang or "en").lower().startswith("cs") else "en"
    return HTMLResponse(html.replace("__LANG__", lang), headers={"Cache-Control": "no-store"})


class AuthLoginData(BaseModel):
    password: str = ""
    remember: bool = True


@fastapi_app.post("/api/auth/login")
def api_auth_login(data: AuthLoginData, request: Request):
    ip = SEC.client_ip(request)
    if not SEC.enabled:
        return {"ok": True, "enabled": False}
    wait = SEC.locked_for(ip)
    if wait > 0:
        return JSONResponse({"detail": "too_many_attempts", "retry_after": wait}, status_code=429)
    if not SEC.check_password(data.password):
        step = SEC.register_fail(ip)
        if step:
            return JSONResponse({"detail": "too_many_attempts", "retry_after": step}, status_code=429)
        return JSONResponse({"detail": "bad_password"}, status_code=401)
    SEC.register_success(ip)
    tok = SEC.create_session(request, data.remember)
    mc.append_manager_log(f"[SECURITY] Přihlášení do webu z {ip}" + (" (zapamatované zařízení)" if data.remember else ""))
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, request, tok, data.remember)
    return resp


@fastapi_app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    SEC.drop_session_token(request.cookies.get(sec.SESSION_COOKIE) or "")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sec.SESSION_COOKIE, path="/")
    return resp


@fastapi_app.get("/api/auth/status")
def api_auth_status(request: Request):
    return {
        "passkeys": sec.passkey_count_for(SEC, request) if (SEC.enabled and sec.passkeys_usable(request, SEC)) else 0,
        "enabled": SEC.enabled,
        "authorized": SEC.is_authorized(request),
        "session": bool(SEC.session_for(request)) if SEC.enabled else False,
        "home": SEC.is_home_request(request) if SEC.enabled else False,
    }


class PasskeyVerifyData(BaseModel):
    challenge_id: str = ""
    credential: dict = {}
    name: str = ""
    remember: bool = True


_pk_opt_hits = {}
_pk_opt_lock = threading.Lock()


@fastapi_app.post("/api/auth/passkey/options")
def api_auth_passkey_options(request: Request):
    if not (SEC.enabled and sec.passkeys_usable(request, SEC)):
        raise HTTPException(status_code=404, detail="passkeys_unavailable")
    ip = SEC.client_ip(request)
    wait = SEC.locked_for(ip)
    if wait > 0:
        return JSONResponse({"detail": "too_many_attempts", "retry_after": wait}, status_code=429)
    now_t = time.time()
    with _pk_opt_lock:
        if len(_pk_opt_hits) > 1024:
            _pk_opt_hits.clear()
        arr = [t for t in _pk_opt_hits.get(ip, []) if now_t - t < 60.0]
        if len(arr) >= 20:
            return JSONResponse({"detail": "too_many_attempts", "retry_after": 60}, status_code=429)
        arr.append(now_t)
        _pk_opt_hits[ip] = arr
    try:
        return sec.passkey_auth_options(SEC, request)
    except ValueError:
        raise HTTPException(status_code=404, detail="no_passkeys")


@fastapi_app.post("/api/auth/passkey/verify")
def api_auth_passkey_verify(data: PasskeyVerifyData, request: Request):
    if not (SEC.enabled and sec.passkeys_usable(request, SEC)):
        raise HTTPException(status_code=404, detail="passkeys_unavailable")
    ip = SEC.client_ip(request)
    wait = SEC.locked_for(ip)
    if wait > 0:
        return JSONResponse({"detail": "too_many_attempts", "retry_after": wait}, status_code=429)
    if not sec.passkey_auth_verify(SEC, request, data.challenge_id, data.credential):
        step = SEC.register_fail(ip)
        if step:
            return JSONResponse({"detail": "too_many_attempts", "retry_after": step}, status_code=429)
        return JSONResponse({"detail": "passkey_failed"}, status_code=401)
    SEC.register_success(ip)
    tok = SEC.create_session(request, data.remember)
    mc.append_manager_log(f"[SECURITY] Přihlášení do webu passkey z {ip}")
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, request, tok, data.remember)
    return resp


@fastapi_app.get("/api/v2/security/passkeys")
def api_security_passkeys(request: Request):
    rp, _ = sec.request_rp(request, SEC)
    return {"ok": True, "usable": bool(SEC.enabled and sec.passkeys_usable(request, SEC)), "https": sec.is_https(request, SEC),
            "lib": sec.passkeys_lib_ok(), "rp_id": rp, "items": sec.passkey_list(SEC, request)}


@fastapi_app.post("/api/v2/security/passkeys/register/options")
def api_security_passkey_reg_options(request: Request):
    if not (SEC.enabled and sec.passkeys_usable(request, SEC)):
        raise HTTPException(status_code=409, detail="passkeys_unavailable")
    return sec.passkey_register_options(SEC, request)


@fastapi_app.post("/api/v2/security/passkeys/register/verify")
def api_security_passkey_reg_verify(data: PasskeyVerifyData, request: Request):
    if not (SEC.enabled and sec.passkeys_usable(request, SEC)):
        raise HTTPException(status_code=409, detail="passkeys_unavailable")
    try:
        sec.passkey_register_verify(SEC, request, data.challenge_id, data.credential, data.name)
    except Exception as e:
        mc.append_manager_log(f"[SECURITY] Passkey se nepodařilo uložit: {str(e)[:160]}")
        raise HTTPException(status_code=400, detail="passkey_register_failed")
    mc.append_manager_log(f"[SECURITY] Přidán passkey „{(data.name or 'Passkey')[:60]}“ z {SEC.client_ip(request)}.")
    return {"ok": True, "items": sec.passkey_list(SEC, request)}


@fastapi_app.delete("/api/v2/security/passkeys/{pid}")
def api_security_passkey_delete(pid: str, request: Request):
    ok = sec.passkey_delete(SEC, pid)
    return {"ok": ok, "items": sec.passkey_list(SEC, request)}


class SecurityEnableData(BaseModel):
    password: str = ""
    remember: bool = True


class SecurityPasswordData(BaseModel):
    current: str = ""
    new: str = ""
    logout_others: bool = True


class SecurityDisableData(BaseModel):
    password: str = ""


class SecurityNetworkData(BaseModel):
    home_bypass: Optional[bool] = None
    home_networks: Optional[List[str]] = None
    behind_proxy: Optional[bool] = None
    trusted_proxies: Optional[List[str]] = None
    trust_cf_header: Optional[bool] = None
    public_stats: Optional[bool] = None


@fastapi_app.post("/api/v2/security/enable")
def api_security_enable(data: SecurityEnableData, request: Request):
    if SEC.enabled:
        raise HTTPException(status_code=409, detail="already_enabled")
    if len(data.password or "") < sec.MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail="password_too_short")
    SEC.enable(data.password)
    tok = SEC.create_session(request, data.remember)   # kdo zapíná, zůstane přihlášený
    mc.append_manager_log(f"[SECURITY] Přihlášení do webu ZAPNUTO (z {SEC.client_ip(request)}).")
    # s přihlášením se zapne i schvalování workerů; 15 minut se nové workery schvalují samy,
    # aby se stihly ohlásit ty, které se právě aktualizují nebo ještě neposlaly svůj klíč
    sync_worker_approval_with_signin("enable")
    WREG.start_auto_window()
    mc.append_manager_log("[SECURITY] Automatické schvalování nových workerů na 15 minut (zapnutí přihlášení).")
    resp = JSONResponse({"ok": True, "security": {**SEC.summary(request), "workers": WREG.summary()}})
    _set_session_cookie(resp, request, tok, data.remember)
    return resp


@fastapi_app.post("/api/v2/security/disable")
def api_security_disable(data: SecurityDisableData, request: Request):
    ip = SEC.client_ip(request)
    if SEC.enabled:
        wait = SEC.locked_for(ip)
        if wait > 0:
            return JSONResponse({"detail": "too_many_attempts", "retry_after": wait}, status_code=429)
        if not SEC.check_password(data.password):
            step = SEC.register_fail(ip)
            if step:
                return JSONResponse({"detail": "too_many_attempts", "retry_after": step}, status_code=429)
            raise HTTPException(status_code=403, detail="bad_password")
        SEC.register_success(ip)
    SEC.disable()
    mc.append_manager_log(f"[SECURITY] Přihlášení do webu VYPNUTO (z {ip}).")
    sync_worker_approval_with_signin("disable")
    resp = JSONResponse({"ok": True, "security": {**SEC.summary(request), "workers": WREG.summary()}})
    resp.delete_cookie(sec.SESSION_COOKIE, path="/")
    return resp


@fastapi_app.post("/api/v2/security/password")
def api_security_password(data: SecurityPasswordData, request: Request):
    if not SEC.enabled:
        raise HTTPException(status_code=409, detail="not_enabled")
    ip = SEC.client_ip(request)
    wait = SEC.locked_for(ip)
    if wait > 0:
        return JSONResponse({"detail": "too_many_attempts", "retry_after": wait}, status_code=429)
    if not SEC.check_password(data.current):
        SEC.register_fail(ip)
        raise HTTPException(status_code=403, detail="bad_password")
    if len(data.new or "") < sec.MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail="password_too_short")
    SEC.set_password(data.new, request, data.logout_others)
    mc.append_manager_log(f"[SECURITY] Heslo do webu změněno (z {ip})" + (", ostatní zařízení odhlášena." if data.logout_others else "."))
    return {"ok": True, "security": SEC.summary(request)}


@fastapi_app.post("/api/v2/security/network")
def api_security_network(data: SecurityNetworkData, request: Request):
    SEC.set_network_options(
        data.home_bypass, data.home_networks, data.behind_proxy,
        data.trusted_proxies, data.public_stats, data.trust_cf_header,
    )
    return {"ok": True, "security": SEC.summary(request)}


@fastapi_app.get("/api/v2/security/devices")
def api_security_devices(request: Request):
    return {"ok": True, "devices": SEC.devices(request)}


@fastapi_app.delete("/api/v2/security/devices/{dev_id}")
def api_security_device_delete(dev_id: str):
    return {"ok": SEC.drop_device(dev_id)}


class WorkerApprovalData(BaseModel):
    enabled: bool = False


class JoinTokenData(BaseModel):
    join: bool = True


@fastapi_app.post("/api/v2/security/workers/approval")
def api_security_worker_approval(data: WorkerApprovalData):
    # Schvalování se samostatně nezapíná ani nevypíná – řídí se přihlášením do webu (sync_worker_approval_with_signin).
    if bool(data.enabled) != bool(SEC.enabled):
        raise HTTPException(status_code=409, detail="approval_follows_signin")
    sync_worker_approval_with_signin("approval")
    return {"ok": True, "workers": WREG.summary()}


@fastapi_app.post("/api/v2/security/workers/auto")
def api_security_worker_auto(data: WorkerApprovalData):
    if data.enabled:
        WREG.start_auto_window()
        mc.append_manager_log("[SECURITY] Automatické schvalování nových workerů na 15 minut.")
    else:
        WREG.stop_auto_window()
    return {"ok": True, "workers": WREG.summary()}


@fastapi_app.post("/api/v2/security/workers/{rec_id}/{action}")
def api_security_worker_action(rec_id: str, action: str):
    if action not in ("approve", "reject", "ignore", "remove"):
        raise HTTPException(status_code=400, detail="bad_action")
    res = WREG.act(rec_id, action)
    return {**res, "workers": WREG.summary()}


@fastapi_app.post("/api/v2/security/join_token")
def api_security_join_token(data: JoinTokenData):
    return {"ok": True, **WREG.new_join_token(data.join)}


@fastapi_app.post("/api/v2/security/devices/logout_others")
def api_security_logout_others(request: Request):
    return {"ok": True, "removed": SEC.drop_other_devices(request)}


@fastapi_app.middleware("http")
async def disable_api_keepalive(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-HB-Manager-Id"] = MANAGER_ID       # workery podle něj poznají „svůj“ Manager
    path = request.url.path
    # Webové rozhraní (/api/v2/*, /api/auth/*, /api/stats) smí používat HTTP keep-alive (méně TCP/TLS handshaků).
    # Krátké worker endpointy mimo relay drží Connection: close kvůli rychlému uvolnění socketů po restartu.
    if path.startswith(("/api/jobs/", "/api/workers/", "/api/update/")) and not path.endswith(("/relay_download", "/relay_upload", "/relay_source_info")):
        resp.headers["Connection"] = "close"
    return resp


# ==============================================================================
# PYDANTIC SCHÉMATA POŽADAVKŮ
# ==============================================================================
class ProgressData(BaseModel):
    progress: float
    worker_name: str = "unknown"
    version: str = VERSION
    telemetry: Optional[dict] = None
    fps_current: float = 0.0
    fps_avg: float = 0.0
    speed_mbps: float = 0.0
    elapsed_sec: int = 0
    eta_sec: int = 0
    transferred_bytes: Optional[int] = None
    src_size_bytes: Optional[int] = None
    is_net_fallback: Optional[bool] = None
    net_fallback_reason: Optional[str] = None
    v_bitrate_in_kbps: Optional[int] = None
    a_bitrate_in_kbps: Optional[int] = None
    premux_applied: Optional[bool] = None
    premux_status: Optional[str] = None
    step_message: Optional[str] = None
    updating_tools: Optional[List[str]] = None
    power_mode_override: Optional[str] = None
    custom_cores_override: Optional[List[int]] = None
    ui_lang_override: Optional[str] = None
    is_paused_override: Optional[bool] = None
    is_enabled_override: Optional[bool] = None
    log_lines: Optional[List[str]] = None
    windows_settings_state: Optional[dict] = None


class PingData(BaseModel):
    worker_name: str
    version: str = VERSION
    telemetry: Optional[dict] = None
    power_mode_override: Optional[str] = None
    custom_cores_override: Optional[List[int]] = None
    ui_lang_override: Optional[str] = None
    is_enabled_override: Optional[bool] = None
    log_lines: Optional[List[str]] = None
    held_job_ids: Optional[List[int]] = None
    windows_settings_state: Optional[dict] = None


class HandshakeRequest(BaseModel):
    worker_name: str
    phase: str = "init"
    token: str = ""
    worker_saw_mgr_in: bool = False
    worker_saw_mgr_out: bool = False


class UploadSlotRequest(BaseModel):
    worker_name: str
    out_size_bytes: int = 0
    force: bool = False


class WaitingDiskReport(BaseModel):
    worker_name: str
    out_size_bytes: int = 0
    src_size_bytes: Optional[int] = None
    v_bitrate_in_kbps: Optional[int] = None
    a_bitrate_in_kbps: Optional[int] = None
    v_bitrate_out_kbps: Optional[int] = None
    a_bitrate_out_kbps: Optional[int] = None
    reason: str = ""


class CompleteData(BaseModel):
    version: Optional[str] = VERSION
    worker_name: Optional[str] = None
    src_size_bytes: int = 0
    out_size_bytes: int = 0
    duration_sec: int = 0
    subs_removed: int = 0
    subs_status: str = "OK"
    v_bitrate_in_kbps: Optional[int] = 0
    a_bitrate_in_kbps: Optional[int] = 0
    v_bitrate_out_kbps: Optional[int] = 0
    a_bitrate_out_kbps: Optional[int] = 0
    mkv_stats_written: Optional[bool] = False
    lpcm_tracks_muxed: Optional[int] = 0
    premux_applied: Optional[bool] = False
    premux_status: Optional[str] = None
    final_dest_path: Optional[str] = None


class ErrorData(BaseModel):
    error_type: str = "GENERIC"
    version: Optional[str] = VERSION
    worker_name: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None


class JobCancelRequest(BaseModel):
    worker_name: Optional[str] = ""
    reason: Optional[str] = ""


class UiPrefsRequest(BaseModel):
    prefs: dict = {}


class V2JobActionRequest(BaseModel):
    job_ids: List[int]
    action: str


class DeleteWorkerStatsRequest(BaseModel):
    worker_name: str


class V2ReorderRequest(BaseModel):
    ordered_ids: List[int]


class V2WorkerPolicyRequest(BaseModel):
    max_slots: Optional[int] = None
    capability: Optional[str] = None
    allow_4k: Optional[bool] = None
    staging_mode: Optional[str] = None
    power_mode: Optional[str] = None
    custom_cores: Optional[List[int]] = None
    prioritize_4k: Optional[bool] = None
    use_local_temp: Optional[bool] = None
    is_enabled: Optional[bool] = None


class V2WorkerWindowsSettingsRequest(BaseModel):
    autostart_mode: Optional[str] = "after_login"
    smb_server: Optional[str] = ""
    smb_user: Optional[str] = ""
    smb_pass: Optional[str] = ""
    desktop_shortcut: Optional[bool] = True
    start_menu_shortcut: Optional[bool] = True
    tray_minimize: Optional[bool] = True
    sleep_prevention_mode: Optional[str] = None   # None = ponechat (starší webové UI)
    skip_tray_prompt: Optional[bool] = None
    show_log: Optional[bool] = None
    wake_check: Optional[bool] = None
    force_task_repair: Optional[bool] = False   # C+: vynutit vytvoření / opravu / odebrání naplánované úlohy ve Windows


class V2WorkerStopRequest(BaseModel):
    disable_worker: bool = True


class V2ClusterActionRequest(BaseModel):
    action: str
    settings: Optional[dict] = None


class V2DuplicatesResolveRequest(BaseModel):
    action: str
    keep_mode: str = "newer"
    include_diff: bool = False


class V2StartBuildRequest(BaseModel):
    targets: List[str] = ["all"]
    target_tools: Optional[List[str]] = None
    preferred_worker: Optional[str] = None
    force_rebuild: bool = False
    force_all: Optional[bool] = None


class V2BuildProgressReport(BaseModel):
    worker_name: str
    job_id: Optional[int] = None
    status: str
    progress: float = 0.0
    step_message: str = ""
    updating_tools: Optional[List[str]] = None
    console_line: Optional[str] = None


class V2ClearLogsRequest(BaseModel):
    target: str = "all"


class V2UpdateCheckRequest(BaseModel):
    force: bool = True


# ==============================================================================
# STARTUP & SHUTDOWN UDÁLOSTI (SIGTERM / SIGINT -> čisté uzavření SQLite WAL)
# ==============================================================================
def purge_stale_system_build_jobs(db_session, keep_job_id: Optional[int] = None) -> int:
    """
    Odstraní staré selhané / zrušené SYSTEM_BUILD úlohy (ERROR / CANCELLED), aby po úspěšném
    buildu nebo restartu Managera zbytečně neblokovaly frontu a nebarvily faviconu do červena.
    """
    removed = 0
    try:
        for oj in db_session.query(models.Job).filter(models.Job.status.in_(["ERROR", "CANCELLED"])).all():
            if keep_job_id is not None and oj.id == keep_job_id:
                continue
            if mc.is_system_build_job(oj):
                mc.job_heartbeats.pop(oj.id, None)
                mc.job_assigned_time.pop(oj.id, None)
                db_session.delete(oj)
                removed += 1
    except Exception as e:
        mc.logger.warning(f"purge_stale_system_build_jobs warning: {e}")
    return removed


def startup_event():
    global MANAGER_ID
    mc.bootstrap_manager_runtime()
    if not MANAGER_ID or MANAGER_ID == "selftest":
        MANAGER_ID = sec.load_or_create_manager_id(
            os.path.join(DATA_DIR, "manager_identity.json"), log=lambda m: mc.append_manager_log(m)
        )
    mc.MANAGER_ID = MANAGER_ID
    sync_worker_approval_with_signin("start")
    mc.ensure_workers_subdir_and_migrate()
    try:
        with db_scope(commit=True) as db:
            db.query(models.Job).filter(models.Job.status == "DELETED").delete()
            purged_sys = purge_stale_system_build_jobs(db)
            if purged_sys:
                mc.logger.info(f"Startup: removed {purged_sys} stale failed/cancelled SYSTEM_BUILD job(s).")
    except Exception:
        pass

    mc.initialize_structured_metadata()
    mc.AUTO_BUILD_HOOK = auto_build_tick
    if VERSION != mc.MANAGER_VERSION:
        mc.logger.warning(f"VERSION in main.py ({VERSION}) != MANAGER_VERSION in manager_core.py ({mc.MANAGER_VERSION})")
    mc.ensure_shared_dir(mc.WATCH_DIR)
    for target_fn in (mc.poll_directory, mc.ffprobe_worker_loop, mc.upstream_version_watcher_loop, mc.udp_discovery_responder_loop):
        threading.Thread(target=target_fn, daemon=True).start()
    threading.Thread(target=maintenance_loop, name="hb-maintenance", daemon=True).start()
    mc.append_manager_log(
        mc.T("api_hb_cluster_manager_v", mc.MANAGER_VERSION)
    )
    if not DATA_DIR_PERSISTENT:
        # Docker bez svazku pro /data: databáze, nastavení i nástroje by s další aktualizací zmizely
        mc.logger.warning(mc.T("api_data_dir_not_volume", DATA_DIR))
    try:
        mc.evaluate_manager_update_attempt()     # O1: jak dopadla poslední aktualizace Manageru
    except Exception as e:
        mc.logger.warning(f"update attempt check: {e}")
    # Po startu chvíli počkat na registraci workerů a pak okamžitě zkontrolovat nástroje.
    threading.Thread(target=lambda: (time.sleep(40), auto_build_tick("startup", True)), daemon=True).start()


_shutdown_done = threading.Event()
MAINTENANCE_INTERVAL_S = 60


def maintenance_loop():
    """Údržba na pozadí 1× za minutu: zaseknuté úlohy (worker přestal odpovídat) zpět do fronty a automatický
    úklid historie. Dřív běžela jen při načtení stavu webem – bez otevřeného webu se nic nedělo."""
    while not _shutdown_done.is_set():
        try:
            mc.check_zombie_jobs()
        except Exception as e:
            mc.logger.warning(f"maintenance_loop: {e}")
        try:
            mc.maybe_daily_backup()                  # D2: denní záloha databáze (posledních 7)
        except Exception as e:
            mc.logger.warning(f"daily backup: {e}")
        try:
            mc.maintain_manager_update_attempt()     # O1: úklid záznamu o aktualizaci
        except Exception as e:
            mc.logger.warning(f"update attempt maintenance: {e}")
        _shutdown_done.wait(MAINTENANCE_INTERVAL_S)


def shutdown_event():
    # Uvicorn převádí SIGTERM (docker stop / Portainer recreate) i SIGINT na tento lifespan hook.
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    try:
        WREG.flush()
    except Exception:
        pass
    try:
        mc.append_manager_log(mc.T("api_manager_shutting_down"))
    except Exception:
        pass
    mc.shutdown_database_cleanly()


# ==============================================================================
# STATICKÉ SOUBORY FRONTENDU, SLOVNÍKU & WORKERŮ (/app/workers/)
# ==============================================================================
@fastapi_app.get("/", response_class=HTMLResponse)
def serve_spa_frontend():
    fp = os.path.join(mc.APP_BASE_DIR, "index.html")
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8-sig") as f:
            html = f.read()
        ver_q = urllib.parse.quote(VERSION, safe="")
        html = re.sub(
            r'((?:src|href)=["\'])(/(?:static/[^"\'?]+|(?:app|locales)\.js|favicon\.ico))(["\'])',
            rf'\1\2?v={ver_q}\3',
            html,
        )
        return HTMLResponse(
            content=html,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return HTMLResponse("<html><body><h2>index.html not found</h2></body></html>", status_code=200)


@fastapi_app.get("/manifest.webmanifest")
def serve_pwa_manifest():
    """PWA manifest: „Přidat na plochu“ otevře Manager přes celou obrazovku jako aplikaci.
    Bez service workeru záměrně – žádná mezipaměť, která by po OTA aktualizaci držela starou verzi webu.
    Instalace jako aplikace funguje jen přes HTTPS (např. Cloudflare / reverzní proxy); v LAN přes http je to jen zástupce."""
    manifest = {
        "name": "HB Cluster Manager",
        "short_name": "HB Manager",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#020617",
        "theme_color": "#020617",
        "icons": [
            {"src": "/static/pwa-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/pwa-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(content=json.dumps(manifest), media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@fastapi_app.get("/favicon.ico")
def serve_favicon():
    for fn in ("favicon.ico", "hbm.ico"):
        ico = os.path.join(mc.STATIC_DIR, fn)
        if os.path.isfile(ico):
            return FileResponse(ico, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=204)


@fastapi_app.get("/locales.js")
def get_locales_script():
    lp = os.path.join(mc.APP_BASE_DIR, "locales.js")
    if os.path.isfile(lp):
        return FileResponse(lp, media_type="application/javascript", headers={"Cache-Control": "no-store"})
    return HTMLResponse("window.HB_LOCALES={};", media_type="application/javascript")


@fastapi_app.get("/api/locales")
def get_locales_json():
    return mc.load_unified_locales_dict()


@fastapi_app.get("/app.js")
def get_frontend_app_script():
    ap = os.path.join(mc.APP_BASE_DIR, "app.js")
    if os.path.isfile(ap):
        return FileResponse(ap, media_type="application/javascript", headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="app.js not found")


# ==============================================================================
# HANDSHAKE, PING, BINÁRKY A STORAGE GUARD ENDPOINTY
# ==============================================================================
def inputs_sample_files(in_root: str, max_files: int = 12, max_dirs: int = 30, max_depth: int = 3) -> list:
    """Vzorek souborů z Inputs (cesta relativně k Inputs + velikost). Worker podle nich ověří, že vidí stejné
    místo jako Manager. Do Inputs se NIC nezapisuje (zdroje jsou jen pro čtení, stack může mít /inputs:ro).
    Když v kořeni nejsou soubory (jen složky Filmy / Seriály …), podívá se o úroveň (max. 3) níž."""
    out = []
    queue = [("", 0)]
    seen = 0
    while queue and len(out) < max_files and seen < max_dirs:
        rel, depth = queue.pop(0)
        seen += 1
        subs = []
        try:
            with os.scandir(os.path.join(in_root, rel) if rel else in_root) as it:
                for n, e in enumerate(it):
                    if n > 500 or len(out) >= max_files:
                        break
                    if e.name.startswith("."):
                        continue
                    try:
                        r_p = f"{rel}/{e.name}" if rel else e.name
                        if e.is_file(follow_symlinks=False):
                            out.append({"path": r_p, "size": int(e.stat(follow_symlinks=False).st_size)})
                        elif e.is_dir(follow_symlinks=False) and depth < max_depth and len(subs) < 8:
                            subs.append(r_p)
                    except Exception:
                        continue
        except Exception:
            continue
        queue.extend((sp, depth + 1) for sp in subs)
    return out


@fastapi_app.post("/api/workers/handshake")
def worker_storage_handshake(req: HandshakeRequest):
    in_root = mc.get_manager_inputs_dir()
    out_root = mc.get_manager_outputs_dir()
    w_safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", req.worker_name)

    p_mgr_out = os.path.join(out_root, f".hb_probe_mgr_{w_safe}.tmp")
    p_wrk_out = os.path.join(out_root, f".hb_probe_wrk_{w_safe}.tmp")
    now_t = time.time()

    if req.phase == "init":
        with mc.handshake_lock:
            cur_tok = mc.active_handshake_tokens.get(req.worker_name)
            reuse_tok = cur_tok["token"] if (cur_tok and (now_t - cur_tok["ts"]) < 8.0) else ""
        if reuse_tok and os.path.isfile(p_mgr_out):
            tok = reuse_tok
        else:
            tok = mc.hashlib.md5(f"{req.worker_name}_{now_t}_{secrets.token_hex(4)}".encode()).hexdigest()[:10]
            with mc.handshake_lock:
                mc.active_handshake_tokens[req.worker_name] = {"token": tok, "ts": now_t}

        wrote_out = False
        samples, files = [], []
        try:
            if os.path.exists(out_root):
                with open(p_mgr_out, "w", encoding="utf-8") as f:
                    f.write(tok)
                wrote_out = True
        except Exception:
            pass

        try:
            if os.path.exists(in_root):
                samples = [n for n in os.listdir(in_root)[:15] if not n.startswith(".")]   # pro workery ≤ dev.38
                files = inputs_sample_files(in_root)
        except Exception:
            pass

        return {
            "ok": True,
            "token": tok,
            "mgr_out_probe_filename": os.path.basename(p_mgr_out) if wrote_out else "",
            "wrk_out_probe_filename": os.path.basename(p_wrk_out),
            "mgr_in_probe_filename": "",                 # do Inputs se už nezapisuje (kontrola podle vzorků)
            "sample_inputs_entries": samples,
            "sample_inputs_files": files
        }

    with mc.handshake_lock:
        cur_tok = dict(mc.active_handshake_tokens.get(req.worker_name) or {}) or None

    tok = req.token or (cur_tok["token"] if cur_tok else "")
    saw_wrk_out = False
    for _ in range(3):
        try:
            if os.path.isfile(p_wrk_out):
                with open(p_wrk_out, "r", encoding="utf-8") as f:
                    val = f.read().strip()
                    if val == tok or (cur_tok and val == cur_tok.get("token")):
                        saw_wrk_out = True
                        break
        except Exception:
            pass
        time.sleep(0.25)

    for fp in (p_mgr_out, p_wrk_out):                  # jen Outputs – v Inputs se nic nemaže
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass

    in_ok = bool(req.worker_saw_mgr_in)
    out_ok = bool(req.worker_saw_mgr_out and saw_wrk_out)
    with mc.handshake_lock:
        mc.active_handshake_tokens.pop(req.worker_name, None)
        mc.worker_handshakes[req.worker_name] = {
            "inputs_ok": in_ok,
            "outputs_ok": out_ok,
            "verified_at": int(time.time())
        }
    return {"ok": True, "inputs_ok": in_ok, "outputs_ok": out_ok}


@fastapi_app.get("/api/stats")
def get_cluster_stats():
    with db_scope() as db:
        act_list = (
            db.query(models.Job)
            .options(defer(models.Job.hb_json_payload))
            .filter(models.Job.status.in_(["WAITING"] + mc.ACTIVE_JOB_STATUSES))
            .all()
        )
        run_cnt = sum(1 for j in act_list if j.status in mc.COMPUTE_ACTIVE_STATUSES)
        wait_cnt = len(act_list) - run_cnt
        c_4k = sum(1 for j in act_list if mc.get_job_height(j) >= 1200)
        c_1080 = len(act_list) - c_4k
        tot_act = len(act_list)
    now_t = time.time()
    on_cnt = sum(1 for _, t in mc.active_workers.items() if now_t - t <= 45)
    return {
        "online_workers": on_cnt,
        "running_jobs": run_cnt,
        "waiting_jobs": wait_cnt,
        "total_active": tot_act,
        "jobs_4k": c_4k,
        "jobs_1080p": c_1080
    }


def _delete_worker_stats_response(worker_name: str) -> dict:
    w_req = urllib.parse.unquote(str(worker_name or "")).strip()
    if not w_req:
        raise HTTPException(status_code=400, detail="Missing worker_name")
    with db_scope(commit=True) as db:
        res = mc.delete_worker_stats(db, w_req)
        mc.append_manager_log(mc.T("api_worker_stats_deleted", res["worker_name"], res["deleted_jobs"]))
        analytics_snap = mc.get_cached_analytics(db)
    return {"ok": True, **res, "analytics": analytics_snap}


@fastapi_app.delete("/api/stats/worker/{worker_name}")
def api_delete_worker_stats(worker_name: str):
    return _delete_worker_stats_response(worker_name)


@fastapi_app.post("/api/stats/delete-worker")
def api_post_delete_worker_stats(req: DeleteWorkerStatsRequest):
    return _delete_worker_stats_response(req.worker_name)


@fastapi_app.get("/api/update/version")
def get_version():
    mf = mc.get_all_binaries_manifest()
    return {
        "version": mc.WORKER_VERSION,
        "ui_lang": mc.AppState.ui_lang,
        "binary_generation": mc.AppState.binary_generation,
        "hbcli_win_md5": mf["windows"]["hbcli"]["md5"],
        "hbcli_linux_md5": mf["linux"]["hbcli"]["md5"],
        "binaries": mf
    }


# ------------------------------------------------------------------------------
# OTA MANIFEST (SHA-256): worker stáhne soubory podle manifestu a ověří jejich otisky.
# Staré adresy pro workery <= 2.11.4ZH (/api/update/core|windows|linux|locales, /hbcli/linux|windows,
# /binaries/info) jsou od v2.12.0 pryč (O3); /api/update/version zůstává.
# ------------------------------------------------------------------------------
_OTA_SCRIPT_FILES = {"worker_core.py", "worker.pyw", "worker_linux.py", "locales.js"}
_OTA_MANIFEST_TARGETS = {
    "windows": ("worker_core.py", "worker.pyw", "locales.js", "VERSION"),
    "linux": ("worker_core.py", "worker_linux.py", "locales.js", "VERSION"),
}
_ota_sha_cache = {}


def _ota_file_path(name: str):
    if name not in _OTA_SCRIPT_FILES:
        return None
    p = os.path.join(mc.APP_BASE_DIR, "locales.js") if name == "locales.js" else mc.resolve_worker_script_file(name)
    return p if (p and os.path.isfile(p)) else None


def _ota_file_bytes(name: str):
    """Vrací (bytes, cesta|None) pro soubor OTA balíčku; None, pokud neexistuje."""
    if name == "VERSION":
        return (f"{mc.WORKER_VERSION}\n").encode("utf-8"), None
    p = _ota_file_path(name)
    if not p:
        return None, None
    with open(p, "rb") as f:
        return f.read(), p


def _ota_file_sha256(name: str):
    if name == "VERSION":
        data = (f"{mc.WORKER_VERSION}\n").encode("utf-8")
        return hashlib.sha256(data).hexdigest(), len(data)
    path = _ota_file_path(name)
    if not path:
        return None, 0
    try:
        st = os.stat(path)
    except OSError:
        return None, 0
    ck = (path, st.st_mtime_ns, st.st_size)
    cached = _ota_sha_cache.get(name)
    if cached and cached[0] == ck:
        return cached[1], st.st_size
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    _ota_sha_cache[name] = (ck, digest)
    return digest, st.st_size


def _windows_readme_txt(cfg_lines=None) -> str:
    """README.txt Windows balíčku. Bez cfg_lines obecná verze (pro OTA), s nimi verze do ZIPu."""
    lines = [
        "==================================================================",
        f" HB CLUSTER - WINDOWS WORKER - v{mc.WORKER_VERSION}",
        "==================================================================",
        "",
        "[CZ] RYCHLÝ NÁVOD K INSTALACI:",
        "1. Rozbalte celý tento ZIP archiv do libovolné složky (např. C:\\HBWorker).",
        "2. Poklepejte na 'HBWorker.exe' (případně app\\Start_Worker.bat).",
        "3. V otevřeném okně potvrďte nastavení prvního spuštění a klikněte na",
        "   'Uložit nastavení a spustit HB Worker'.",
        "4. Pro čistou odinstalaci spusťte 'Uninstall_Worker.bat' (nebo zvolte Odinstalovat v menu workeru).",
        "   Smažou se jen soubory HB Workeru; složka zmizí, jen pokud v ní nic dalšího nezůstane.",
        "",
        "[EN] QUICK SETUP GUIDE:",
        "1. Extract this entire ZIP archive into any folder (e.g. C:\\HBWorker).",
        "2. Double-click 'HBWorker.exe' (or app\\Start_Worker.bat).",
        "3. In the setup window, confirm first-run settings and click",
        "   'Save Settings & Launch HB Worker'.",
        "4. For a clean uninstall, run 'Uninstall_Worker.bat' (or choose Uninstall from worker menu).",
        "   Only HB Worker files are removed; the folder disappears only if nothing else is left in it.",
        "",
        "Aktualizace probíhají automaticky z HB Manageru. / Updates are installed automatically from HB Manager.",
    ]
    if cfg_lines:
        lines += ["", "------------------------------------------------------------------",
                  "Předvyplněná konfigurace / Pre-filled configuration:"] + list(cfg_lines)
    return "\n".join(lines) + "\n"


def _windows_pkg_bytes(ent: dict, readme_cfg=None):
    """Obsah jednoho souboru Windows balíčku (bytes) nebo None, pokud na Manageru není."""
    src = ent["src"]
    data = None
    if src == "@version":
        data = (str(mc.WORKER_VERSION).strip() + "\n").encode("utf-8")
    elif src == "@readme":
        data = _windows_readme_txt(readme_cfg).encode("utf-8")
    elif src == "@codesign":
        try:
            mc.ensure_codesign_cert_on_manager()
        except Exception:
            pass
        if os.path.isfile(mc.CODESIGN_CER_FILE):
            with open(mc.CODESIGN_CER_FILE, "rb") as f:
                data = f.read()
    elif not src.startswith("@"):
        if src.startswith("workers/"):
            p = mc.resolve_worker_script_file(src.split("/", 1)[1])
        elif src.startswith("static/"):
            p = os.path.join(mc.STATIC_DIR, src.split("/", 1)[1])
        else:
            p = os.path.join(mc.APP_BASE_DIR, src)
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                data = f.read()
    if data is not None and ent.get("crlf"):
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


@fastapi_app.get("/api/update/pkg")
def get_update_pkg_file(path: str = ""):
    ent = next((e for e in pkg.ota_file_entries() if e["path"] == path), None)
    if not ent:
        raise HTTPException(status_code=404, detail="Unknown package file")
    data = _windows_pkg_bytes(ent)
    if data is None:
        raise HTTPException(status_code=404, detail=f"{path} not available on Manager")
    return Response(content=data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@fastapi_app.get("/api/update/manifest")
def get_update_manifest(target: str = "windows"):
    t = str(target or "").strip().lower()
    if t not in _OTA_MANIFEST_TARGETS:
        raise HTTPException(status_code=400, detail="target must be 'windows' or 'linux'")
    files = []
    for name in _OTA_MANIFEST_TARGETS[t]:
        digest, size = _ota_file_sha256(name)
        if not digest:
            raise HTTPException(status_code=404, detail=f"{name} not found on Manager")
        files.append({"name": name, "url": f"/api/update/file/{name}", "sha256": digest, "size": size})
    out = {"manifest_version": 2, "version": mc.WORKER_VERSION, "target": t, "files": files}
    if t == "windows":
        # Ostatní soubory balíčku (ikona, .bat, README, certifikát…) – worker je porovná otiskem a vymění kdykoliv.
        package = []
        for ent in pkg.ota_file_entries():
            data = _windows_pkg_bytes(ent)
            if data is None:
                continue
            package.append({"path": ent["path"], "url": "/api/update/pkg?path=" + urllib.parse.quote(ent["path"], safe=""),
                            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        out["package"] = package
        out["remove"] = list(pkg.WINDOWS_PACKAGE_REMOVED)
    return out


@fastapi_app.get("/api/update/file/{name}")
def get_update_file(name: str):
    data, path = _ota_file_bytes(name)
    if data is None:
        raise HTTPException(status_code=404, detail=f"{name} not found")
    media = "application/javascript" if name.endswith(".js") else ("text/plain" if name == "VERSION" else "text/x-python")
    return Response(content=data, media_type=media, headers={"Cache-Control": "no-store"})


@fastapi_app.get("/api/update/hbcli/info")
def get_binaries_info():
    mf = mc.get_all_binaries_manifest()
    l_hb = mf["linux"]["hbcli"]
    w_hb = mf["windows"]["hbcli"]
    return {
        "binary_generation": mc.AppState.binary_generation,
        "ui_lang": mc.AppState.ui_lang,
        "linux_available": l_hb["available"],
        "linux_md5": l_hb["md5"],
        "linux_size": l_hb["size"],
        "linux_filename": l_hb["filename"],
        "windows_available": w_hb["available"],
        "windows_md5": w_hb["md5"],
        "windows_size": w_hb["size"],
        "windows_filename": w_hb["filename"],
        "binaries": mf
    }


@fastapi_app.get("/api/update/binaries/{os_type}/{tool}")
def download_universal_binary(os_type: str, tool: str):
    bp = mc.resolve_manager_binary_path(os_type, tool)
    if not bp:
        raise HTTPException(status_code=404, detail=f"Binary {tool} for {os_type} not uploaded.")
    return FileResponse(bp, media_type="application/octet-stream", filename=os.path.basename(bp))


def client_ip_of(request: Request) -> str:
    """Adresa klienta, jak ji vidí Manager. Za proxy / Cloudflare tunelem (spojení přichází z privátní
    adresy proxy) se vezme skutečná adresa z CF-Connecting-IP / X-Forwarded-For. Slouží jen k zobrazení."""
    peer = (request.client.host if request.client else "") or ""
    try:
        p_ip = ipaddress.ip_address(peer)
        if p_ip.is_private or p_ip.is_loopback:
            fwd = (request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0]).strip()
            if fwd:
                ipaddress.ip_address(fwd)
                return fwd
    except ValueError:
        pass
    return peer


def _eligible_waiting_count(db, pol, worker_name: str) -> int:
    """Počet čekajících úloh, které by si worker mohl vzít (stejná pravidla jako /api/jobs/next, bez ohledu na volné sloty):
    rozlišení / 4K podle nastavení workeru, Pre-Mux disků jen u schopného workeru, bez systémových sestavení a pozastavených."""
    try:
        if not pol.is_enabled or mc.AppState.maintenance_mode:
            return 0
        can_4k = mc.worker_allows_4k(pol)
        cap_low = str(getattr(pol, "capability", "all") or "all").strip().lower()
        can_premux = mc.is_worker_premux_capable(worker_name, bool(pol.use_local_temp))
        n = 0
        for j in (
            db.query(models.Job)
            .options(defer(models.Job.hb_json_payload))
            .filter(models.Job.status == "WAITING")
            .all()
        ):
            if mc.is_system_build_job(j) or getattr(j, "is_paused", False):
                continue
            if mc.AppState.smart_premux and not can_premux and mc.is_job_disc_or_iso(j):
                continue
            jw = mc.get_job_weight(j)
            is_4k = jw >= 4
            if (not can_4k or cap_low in ("1080p", "sd", "720p")) and is_4k:
                continue
            if cap_low in ("sd", "720p") and jw > 1:
                continue
            if cap_low == "4k" and not is_4k:
                continue
            n += 1
        return n
    except Exception:
        return 0


@fastapi_app.post("/api/workers/ping")
def worker_ping(data: PingData, request: Request):
    now_t = time.time()
    mc.active_workers[data.worker_name] = now_t
    _seen = client_ip_of(request)
    if not sec.is_masked_peer(_seen):            # brána Dockeru (Manager v bridge síti na NAS) není adresa workeru
        mc.record_worker_seen_ip(data.worker_name, _seen)
    mc.worker_versions[data.worker_name] = data.version or mc.WORKER_VERSION
    if data.telemetry:
        mc.record_telemetry_sample(data.worker_name, data.telemetry)
        if mc.is_linux_worker(data.worker_name):
            auto_build_tick_async("ping")
    if data.log_lines:
        mc.append_worker_logs(data.worker_name, data.log_lines)
    if data.windows_settings_state and isinstance(data.windows_settings_state, dict):
        mc.record_worker_windows_settings_state(data.worker_name, data.windows_settings_state)
    if data.ui_lang_override and data.ui_lang_override in ("en", "cs") and data.ui_lang_override != mc.AppState.ui_lang:
        mc.AppState.ui_lang = data.ui_lang_override
        mc.save_manager_config()
    p_mode, is_en, c_cores = "AUTO", True, []
    q_wait = 0
    cleanup_held_ids = []
    will_write = bool(
        data.power_mode_override
        or data.custom_cores_override is not None
        or data.is_enabled_override is not None
        or data.held_job_ids is not None
    )
    try:
        with db_scope(immediate=will_write) as db:
            pol = mc.get_or_create_worker_policy(db, data.worker_name)
            pol_dirty = False
            if data.power_mode_override and data.power_mode_override in mc.VALID_POWER_MODES:
                pol.power_mode = data.power_mode_override
                pol_dirty = True
            if data.custom_cores_override is not None:
                pol.custom_cores = mc.serialize_custom_cores_list(data.custom_cores_override)
                pol_dirty = True
            if data.is_enabled_override is not None:
                pol.is_enabled = bool(data.is_enabled_override)
                if pol.is_enabled:
                    mc.AppState.maintenance_mode = False
                    mc.clear_worker_intake_off_reason(data.worker_name)
                pol_dirty = True
            if pol_dirty:
                db.commit()
                mc.save_manager_config(db)
            if data.held_job_ids is not None:
                if mc.reconcile_worker_held_disk_jobs(db, data.worker_name, data.held_job_ids):
                    db.commit()
                cleanup_held_ids = mc.compute_cleanup_held_job_ids(db, data.worker_name, data.held_job_ids)
            p_mode = getattr(pol, 'power_mode', 'AUTO') or "AUTO"
            c_cores = mc.parse_custom_cores_list(getattr(pol, 'custom_cores', ''))
            is_en = bool(pol.is_enabled and not mc.AppState.maintenance_mode)
            if is_en:
                # chytrý režim spánku Windows workeru: dokud ve frontě čeká úloha, kterou TENTO worker může vzít, PC neusne
                q_wait = _eligible_waiting_count(db, pol, data.worker_name)
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in worker_ping ({data.worker_name}): {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    is_lin_w = mc.is_linux_worker(data.worker_name)
    return {
        "ok": True,
        "queue_waiting": int(q_wait),
        "power_mode": p_mode,
        "custom_cores": c_cores,
        "is_enabled": is_en,
        "ui_lang": mc.AppState.ui_lang,
        "telemetry_source": mc.AppState.telemetry_source,
        "ram_format": mc.AppState.ram_format,
        "binary_generation": mc.AppState.binary_generation,
        "update_required": bool(data.version and data.version != mc.WORKER_VERSION),
        "update_now": mc.take_worker_update_now(data.worker_name, data.version),
        "target_version": mc.WORKER_VERSION,
        "cleanup_held_job_ids": cleanup_held_ids,
        "win_default_inputs_dir": "" if is_lin_w else mc.AppState.win_default_inputs_dir,
        "win_default_outputs_dir": "" if is_lin_w else mc.AppState.win_default_outputs_dir,
        "smb_username": "" if is_lin_w else mc.AppState.smb_username,
        "smb_password": "" if is_lin_w else mc.AppState.smb_password,
        "pending_windows_settings": None if is_lin_w else mc.get_pending_worker_windows_settings(data.worker_name),
    }


@fastapi_app.get("/api/workers/{worker_name}/config")
def get_worker_config(worker_name: str, request: Request):
    if WREG.approval and getattr(request.state, "is_worker_auth", False):
        bname = getattr(request.state, "worker_bound_name", "") or ""
        if bname and bname != worker_name:
            raise HTTPException(status_code=403, detail="worker_name_mismatch")
    is_lin_w = mc.is_linux_worker(worker_name)
    with db_scope() as db:
        pol = mc.get_or_create_worker_policy(db, worker_name)
        return {
            "max_slots": pol.max_slots,
            "capability": pol.capability or "all",
            "allow_4k": mc.worker_allows_4k(pol),
            "use_local_temp": pol.use_local_temp,
            "is_enabled": bool(pol.is_enabled and not mc.AppState.maintenance_mode),
            "staging_mode": getattr(pol, 'staging_mode', mc.AppState.default_staging_mode) or mc.AppState.default_staging_mode,
            "power_mode": getattr(pol, 'power_mode', mc.AppState.default_power_mode) or mc.AppState.default_power_mode,
            "custom_cores": mc.parse_custom_cores_list(getattr(pol, 'custom_cores', '')),
            "ui_lang": mc.AppState.ui_lang,
            "telemetry_source": mc.AppState.telemetry_source,
            "ram_format": mc.AppState.ram_format,
            "win_default_inputs_dir": "" if is_lin_w else mc.AppState.win_default_inputs_dir,
            "win_default_outputs_dir": "" if is_lin_w else mc.AppState.win_default_outputs_dir,
            "smb_username": "" if is_lin_w else mc.AppState.smb_username,
            "smb_password": "" if is_lin_w else mc.AppState.smb_password,
        }


class WindowsPackageRequest(BaseModel):
    worker_name: str = ""
    manager_url: str = ""
    inputs_dir: str = ""
    outputs_dir: str = ""
    temp_dir: str = ""
    smb_server: str = ""
    smb_username: str = ""
    smb_password: str = ""
    autostart_mode: str = "after_login"
    desktop_shortcut: bool = True
    start_menu_shortcut: bool = True
    tray_minimize: bool = True
    sleep_mode: str = ""
    wake_check: bool = True
    include_binaries: bool = False
    lang: str = ""
    dl: str = ""


def _build_windows_package_response(
    request: Request,
    eff_name: str,
    manager_url: str,
    eff_in: str,
    eff_out: str,
    eff_tmp: str,
    smb_server: str,
    smb_username: str,
    smb_password: str,
    eff_auto: str,
    desktop_shortcut: bool,
    start_menu_shortcut: bool,
    tray_minimize: bool,
    sleep_mode: str,
    wake_check: bool,
    eff_bins: bool,
    lang: str,
    dl: str,
    allow_side_effects: bool = False,
):
    eff_lang = str(lang or mc.AppState.ui_lang or "en").strip().lower()
    if eff_lang not in ("cs", "en"):
        eff_lang = "en"
    # Jazyk se týká jen tohoto balíčku (config.json, README.txt) – globální jazyk Manageru se nemění.

    safe_w = re.sub(r"[^a-zA-Z0-9_\-]", "_", mc.sanitize_display_name(eff_name, "Win-Worker")) or "Win-Worker"
    manager_url = mc.sanitize_manager_url(manager_url)
    if not manager_url:
        host_hdr = request.headers.get("host") or f"{mc.get_local_lan_ip()}:8383"
        scheme = "https" if sec.is_https(request, SEC) else (request.url.scheme or "http")
        manager_url = f"{scheme}://{host_hdr}"
    manager_url = manager_url.rstrip("/")

    in_d = mc.sanitize_path(eff_in if eff_in != "" else mc.AppState.win_default_inputs_dir)
    out_d = mc.sanitize_path(eff_out if eff_out != "" else mc.AppState.win_default_outputs_dir)
    tmp_d = mc.sanitize_path(eff_tmp)
    smb_u = (smb_username if smb_username != "" else mc.AppState.smb_username) or ""
    smb_p = (smb_password if smb_password not in ("", mc.SECRET_MASK) else mc.AppState.smb_password) or ""
    smb_srv = mc.sanitize_path(smb_server) or mc.extract_unc_server_host(in_d, out_d) or mc.get_default_smb_server()
    auto_m = eff_auto if eff_auto in ("off", "after_login", "before_login") else "after_login"

    cfg_payload = {
        "worker_name": safe_w,
        "server_url": manager_url,
        "manager_url": manager_url,
        "python_path": "",
        "inputs_dir": in_d,
        "outputs_dir": out_d,
        "inputs_path": in_d,
        "outputs_path": out_d,
        "temp_dir": tmp_d,
        "local_temp_dir": tmp_d,
        "smb_server": smb_srv,
        "smb_user": smb_u,
        "smb_pass": smb_p,
        "smb_username": smb_u,
        "smb_password": smb_p,
        "autostart_mode": auto_m,
        "run_on_startup": auto_m == "after_login",
        "run_before_login": auto_m == "before_login",
        "desktop_shortcut": bool(desktop_shortcut),
        "start_menu_shortcut": bool(start_menu_shortcut),
        "tray_minimize": bool(tray_minimize),
        "start_in_tray": bool(tray_minimize),
        "sleep_prevention_mode": mc._norm_sleep_mode(sleep_mode),
        "wake_check": bool(wake_check),
        "first_run_completed": False,
        "lang": eff_lang,
        "ui_lang": eff_lang,
        "power_mode": mc.AppState.default_power_mode or "AUTO",
        "use_local_temp": bool(mc.AppState.default_use_local_temp),
        # předschválení (Zabezpečení): worker spuštěný do hodiny se schválí sám (jednorázově)
        "join_token": dl if (dl and WREG.join_valid(dl, need_join=True)) else "",
    }
    if allow_side_effects:
        mc.record_worker_windows_settings_state(safe_w, cfg_payload)

    start_bat_file = mc.resolve_worker_script_file("Start_Worker.bat")
    if os.path.isfile(start_bat_file):
        with open(start_bat_file, "r", encoding="utf-8") as f_bat:
            bat_content = f_bat.read().replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        bat_content = (
            "@echo off\r\n"
            "setlocal EnableDelayedExpansion\r\n"
            "chcp 65001 >nul 2>&1\r\n"
            "cd /d \"%~dp0\"\r\n"
            "if exist \"HBWorker.exe\" (\r\n"
            "    start \"\" \"HBWorker.exe\"\r\n"
            "    exit /b 0\r\n"
            ")\r\n"
            "if exist \"..\\HBWorker.exe\" (\r\n"
            "    start \"\" \"..\\HBWorker.exe\"\r\n"
            "    exit /b 0\r\n"
            ")\r\n"
            "where pythonw >nul 2>nul\r\n"
            "if %ERRORLEVEL% EQU 0 (\r\n"
            "    start \"\" pythonw \"worker.pyw\"\r\n"
            ") else (\r\n"
            "    start \"\" python \"worker.pyw\"\r\n"
            ")\r\n"
        )

    readme_cfg = [
        f" - Worker Name:  {safe_w}",
        f" - Manager URL:  {manager_url}",
        f" - Language:     {eff_lang.upper()}",
        f" - Inputs Dir:   {in_d or '(ze serveru / from server)'}",
        f" - Outputs Dir:  {out_d or '(ze serveru / from server)'}",
    ]

    buf = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Obsah balíčku podle jediného seznamu workers/package_layout.py (stejný seznam používá OTA i GitHub Release)
            for ent in pkg.WINDOWS_PACKAGE:
                src = ent["src"]
                if src == "@config":
                    zf.writestr(ent["path"], json.dumps(cfg_payload, indent=4, ensure_ascii=False))
                elif src == "@launcher":
                    mc.ensure_signed_windows_launcher_on_manager()
                    launcher_p = mc.resolve_manager_binary_path("windows", "launcher")
                    if launcher_p and os.path.isfile(launcher_p):
                        zf.write(launcher_p, arcname=ent["path"])
                elif src == "@readme":
                    zf.writestr(ent["path"], _windows_pkg_bytes(ent, readme_cfg))
                else:
                    data = _windows_pkg_bytes(ent)
                    if data is None and ent["path"] == "app/Start_Worker.bat":
                        data = bat_content.encode("utf-8")
                    if data is not None:
                        zf.writestr(ent["path"], data)

            if eff_bins:
                missing_win = False
                for tool_k in (*mc.ALL_TOOLS, "7z_dll"):
                    bp = mc.resolve_manager_binary_path("windows", tool_k)
                    if bp and os.path.isfile(bp):
                        zf.write(bp, arcname=f"app/bin/{os.path.basename(bp)}")
                    else:
                        missing_win = True
                if allow_side_effects and missing_win and not mc.has_any_linux_worker():
                    mc.start_manager_windows_binaries_download(["all"], force_rebuild=False)

        buf.seek(0)
    except Exception:
        buf.close()
        raise

    def _stream_spooled_zip():
        try:
            while True:
                chunk = buf.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                buf.close()
            except Exception:
                pass

    zip_fn = f"HBWorker_{safe_w}_v{mc.WORKER_VERSION}.zip"
    return StreamingResponse(
        _stream_spooled_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_fn}"', "Cache-Control": "no-store"},
    )


@fastapi_app.post("/api/v2/workers/package/windows")
@fastapi_app.post("/api/v2/workers/download_windows_package")
def api_v2_post_windows_package(req: WindowsPackageRequest, request: Request):
    return _build_windows_package_response(
        request=request,
        eff_name=req.worker_name or "Win-Worker",
        manager_url=req.manager_url,
        eff_in=req.inputs_dir,
        eff_out=req.outputs_dir,
        eff_tmp=req.temp_dir,
        smb_server=req.smb_server,
        smb_username=req.smb_username,
        smb_password=req.smb_password,
        eff_auto=req.autostart_mode or "after_login",
        desktop_shortcut=req.desktop_shortcut,
        start_menu_shortcut=req.start_menu_shortcut,
        tray_minimize=req.tray_minimize,
        sleep_mode=req.sleep_mode,
        wake_check=req.wake_check,
        eff_bins=req.include_binaries,
        lang=req.lang,
        dl=req.dl,
        allow_side_effects=True,
    )


@fastapi_app.get("/api/v2/workers/package/windows")
@fastapi_app.get("/api/v2/workers/download_windows_package")
@fastapi_app.get("/api/worker/download_windows_zip")
def api_v2_download_windows_package(
    request: Request,
    worker_name: str = "",
    name: str = "",
    manager_url: str = "",
    inputs_dir: str = "",
    inputs: str = "",
    outputs_dir: str = "",
    outputs: str = "",
    temp_dir: str = "",
    temp: str = "",
    smb_server: str = "",
    smb_username: str = "",
    smb_password: str = "",
    autostart_mode: str = "",
    autostart: str = "",
    desktop_shortcut: bool = True,
    start_menu_shortcut: bool = True,
    tray_minimize: bool = True,
    sleep_mode: str = "",
    wake_check: bool = True,
    include_binaries: bool = False,
    binaries: bool = False,
    lang: str = "",
    dl: str = "",
):
    return _build_windows_package_response(
        request=request,
        eff_name=worker_name or name or "Win-Worker",
        manager_url=manager_url,
        eff_in=inputs_dir if inputs_dir != "" else inputs,
        eff_out=outputs_dir if outputs_dir != "" else outputs,
        eff_tmp=temp_dir if temp_dir != "" else temp,
        smb_server=smb_server,
        smb_username=smb_username,
        smb_password=smb_password,
        eff_auto=autostart_mode or autostart or "after_login",
        desktop_shortcut=desktop_shortcut,
        start_menu_shortcut=start_menu_shortcut,
        tray_minimize=tray_minimize,
        sleep_mode=sleep_mode,
        wake_check=wake_check,
        eff_bins=bool(include_binaries or binaries),
        lang=lang,
        dl=dl,
        allow_side_effects=False,
    )


@fastapi_app.get("/api/update/codesign/{kind}")
def get_codesign_file(kind: str):
    # Ven jde jen veřejný certifikát (.cer). Soukromý klíč (.pfx) Manager nikdy neposílá – HBWorker.exe podepisuje sám.
    if kind != "cer":
        raise HTTPException(status_code=404, detail="Only the public certificate (.cer) is available.")
    mc.ensure_codesign_cert_on_manager()
    if not os.path.isfile(mc.CODESIGN_CER_FILE):
        raise HTTPException(status_code=404, detail="Certificate not generated yet")
    return FileResponse(mc.CODESIGN_CER_FILE, media_type="application/octet-stream", filename="maseklab_codesign.cer",
                        headers={"Cache-Control": "no-store"})


@fastapi_app.post("/api/v2/codesign/upload")
def upload_codesign_material():
    # Zrušeno: certifikát generuje a drží výhradně Manager. Starší Linux workery dostanou 410 a nahrají nepodepsaný build.
    raise HTTPException(status_code=410, detail="Signing material is generated on the Manager only.")


class V2SmbTestSaveRequest(BaseModel):
    win_default_inputs_dir: Optional[str] = None
    win_default_outputs_dir: Optional[str] = None
    linux_default_inputs_dir: Optional[str] = None
    linux_default_outputs_dir: Optional[str] = None
    linux_default_temp_dir: Optional[str] = None
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None


@fastapi_app.post("/api/v2/settings/test_smb_and_save")
def api_v2_test_smb_and_save(req: V2SmbTestSaveRequest):
    for fld in (
        "win_default_inputs_dir",
        "win_default_outputs_dir",
        "linux_default_inputs_dir",
        "linux_default_outputs_dir",
        "linux_default_temp_dir",
        "smb_username",
        "smb_password",
    ):
        val = getattr(req, fld, None)
        if fld == "smb_password" and val == mc.SECRET_MASK:
            continue   # UI poslalo masku -> heslo se nemění
        if val is not None:
            cleaned = mc.sanitize_path(val) if fld.endswith("_dir") else str(val).strip()
            setattr(mc.AppState, fld, cleaned)
    mc.save_manager_config()

    smb_u = mc.AppState.smb_username or ""
    smb_p = mc.AppState.smb_password or ""
    win_in_res = mc.test_smb_unc_path(mc.AppState.win_default_inputs_dir or "", smb_u, smb_p, need_write=False)
    win_out_res = mc.test_smb_unc_path(mc.AppState.win_default_outputs_dir or "", smb_u, smb_p, need_write=True)
    tmpl_note = mc.T("smb_test_linux_template_only")
    lin_in_res = {"path": mc.AppState.linux_default_inputs_dir or "", "ok": True, "skipped": True, "detail": tmpl_note}
    lin_out_res = {"path": mc.AppState.linux_default_outputs_dir or "", "ok": True, "skipped": True, "detail": tmpl_note}

    mgr_in = mc.get_manager_inputs_dir()
    mgr_out = mc.get_manager_outputs_dir()
    in_ok = os.path.exists(mgr_in)
    out_ok = os.path.exists(mgr_out) and os.access(mgr_out, os.W_OK)
    all_ok = bool(win_in_res.get("ok") and win_out_res.get("ok"))
    mc.append_manager_log(mc.T(
        "api_smb_test_result",
        "OK" if all_ok else "FAIL",
        win_in_res.get("detail", ""),
        win_out_res.get("detail", "")
    ))

    return {
        "ok": all_ok,
        "saved": True,
        "win_in": win_in_res,
        "win_out": win_out_res,
        "lin_in": lin_in_res,
        "lin_out": lin_out_res,
        "manager_inputs_ok": in_ok,
        "manager_outputs_ok": out_ok,
        "settings": {
            "win_default_inputs_dir": mc.AppState.win_default_inputs_dir,
            "win_default_outputs_dir": mc.AppState.win_default_outputs_dir,
            "linux_default_inputs_dir": mc.AppState.linux_default_inputs_dir,
            "linux_default_outputs_dir": mc.AppState.linux_default_outputs_dir,
            "linux_default_temp_dir": mc.AppState.linux_default_temp_dir,
            "smb_username": mc.AppState.smb_username,
            "smb_password": mc.mask_secret(mc.AppState.smb_password),
        },
    }


@fastapi_app.get("/api/v2/fs/browse")
def api_v2_browse_filesystem(path: str = ""):
    roots = []
    for cand in (mc.get_manager_inputs_dir(), mc.get_manager_outputs_dir(), "/mnt", "/media", "/data", "/"):
        if cand and os.path.isdir(cand) and cand not in roots:
            roots.append(cand)
    if os.name == "nt":
        for drv in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            dp = f"{drv}:\\"
            if os.path.isdir(dp) and dp not in roots:
                roots.append(dp)

    cur = (path or "").strip()
    if not cur or not os.path.isdir(cur):
        cur = roots[0] if roots else "/"

    _BLOCKED_FS_PREFIXES = ("/proc", "/sys", "/etc", "/root", "/dev", "/data/certs")
    cur_abs = os.path.abspath(cur)
    if os.name != "nt" and any(cur_abs == bp or cur_abs.startswith(bp + "/") for bp in _BLOCKED_FS_PREFIXES):
        cur_abs = os.path.abspath(roots[0] if roots else "/")
    parent_p = os.path.dirname(cur_abs.rstrip("\\/"))
    if parent_p == cur_abs:
        parent_p = ""

    dirs = []
    try:
        for entry in sorted(os.scandir(cur_abs), key=lambda e: e.name.lower()):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                ep = entry.path.replace("\\", "/") if not os.name == "nt" else entry.path
                if os.name != "nt" and any(ep == bp or ep.startswith(bp + "/") for bp in _BLOCKED_FS_PREFIXES):
                    continue
                dirs.append({
                    "name": entry.name,
                    "path": ep,
                })
    except Exception:
        pass

    return {
        "ok": True,
        "current_path": cur_abs.replace("\\", "/") if not os.name == "nt" else cur_abs,
        "parent_path": parent_p.replace("\\", "/") if (parent_p and not os.name == "nt") else parent_p,
        "roots": roots,
        "directories": dirs[:200],
    }


# ==============================================================================
# HTTP RELAY ENDPOINTY PRO WORKERY MIMO LOKÁLNÍ NAS (16MB STREAMING + SIZE AUDIT)
# ==============================================================================
RELAY_DOWNLOAD_STATUSES = ("IN_PROGRESS", "STAGING", "PREMUX")


def _safe_content_disposition(filename: str) -> str:
    raw = os.path.basename(str(filename or "file.bin").replace("\\", "/"))
    ascii_fn = re.sub(r'[^A-Za-z0-9._ -]', '_', raw).strip() or "file.bin"
    utf8_fn = urllib.parse.quote(raw, safe="")
    return f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{utf8_fn}"


def _verify_relay_source_owner(job, request: Request, worker_name: str):
    hdr_name = urllib.parse.unquote(request.headers.get("x-hb-worker-name") or "")[:80]
    bname = getattr(request.state, "worker_bound_name", "") or ""
    eff_w = bname or hdr_name or worker_name
    if (
        job.status not in RELAY_DOWNLOAD_STATUSES
        or not job.worker_name
        or (eff_w and job.worker_name != eff_w)
        or (worker_name and job.worker_name != worker_name)
    ):
        mc.append_manager_log(
            f"[SECURITY] Odmítnut přístup ke zdroji úlohy {job.id} přes HTTP relay od {eff_w or '?'} "
            f"(stav {job.status}, worker úlohy {job.worker_name or '-'})."
        )
        raise HTTPException(status_code=403, detail="relay_download_not_allowed")


@fastapi_app.get("/api/jobs/{job_id}/relay_source_info")
def relay_get_source_info(job_id: int, request: Request, worker_name: str = ""):
    with db_scope() as db:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        _verify_relay_source_owner(job, request, worker_name)
        if worker_name:
            mc.active_workers[worker_name] = time.time()
        mc.job_heartbeats[job_id] = time.time()
        orig_s = job.source_path or mc.extract_orig_source_from_payload(job.hb_json_payload)
        full_p, _, found = mc.resolve_input_path_smart(orig_s, mc.get_manager_inputs_dir())
        if not found or not os.path.exists(full_p):
            raise HTTPException(status_code=404, detail="Source not found on Manager")

        if os.path.isfile(full_p):
            sz = os.path.getsize(full_p)
            bn = os.path.basename(full_p)
            return {"ok": True, "is_dir": False, "base_name": bn, "total_bytes": sz, "files": [{"rel_path": bn, "size": sz}]}

        files_out, tot_b = [], 0
        for root_d, _, fns in os.walk(full_p):
            for f in fns:
                fp = os.path.join(root_d, f)
                if not os.path.islink(fp) and os.path.isfile(fp):
                    fsz = os.path.getsize(fp)
                    rel_f = os.path.relpath(fp, full_p).replace("\\", "/")
                    files_out.append({"rel_path": rel_f, "size": fsz})
                    tot_b += fsz
        return {
            "ok": True,
            "is_dir": True,
            "base_name": os.path.basename(full_p.rstrip("\\/")) or "DISC_SOURCE",
            "total_bytes": tot_b,
            "files": files_out
        }


def _iter_large_file_16mb(filepath: str, job_id: int, worker_name: str = ""):
    with open(filepath, "rb", buffering=16 * 1024 * 1024) as rf:
        while True:
            chunk = rf.read(16 * 1024 * 1024)
            if not chunk:
                break
            now_t = time.time()
            mc.job_heartbeats[job_id] = now_t
            if worker_name:
                mc.active_workers[worker_name] = now_t
            yield chunk


@fastapi_app.get("/api/jobs/{job_id}/relay_download")
def relay_download_source_file(job_id: int, request: Request, rel_path: Optional[str] = None, worker_name: str = ""):
    with db_scope() as db:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        _verify_relay_source_owner(job, request, worker_name)
        if worker_name:
            mc.active_workers[worker_name] = time.time()
        mc.job_heartbeats[job_id] = time.time()
        orig_s = job.source_path or mc.extract_orig_source_from_payload(job.hb_json_payload)
        full_p, _, found = mc.resolve_input_path_smart(orig_s, mc.get_manager_inputs_dir())
        if not found or not os.path.exists(full_p):
            raise HTTPException(status_code=404, detail="Source not found on Manager")

        if os.path.isfile(full_p):
            target_p = full_p
        else:
            if not rel_path:
                raise HTTPException(status_code=400, detail="rel_path required for directory source")
            clean_rel = os.path.normpath(rel_path.lstrip("\\/"))
            target_p = os.path.abspath(os.path.join(full_p, clean_rel))
            # Soubor musí ležet uvnitř složky zdroje (commonpath, ne startswith: /inputs/Film ≠ /inputs/Film2)
            # a nesmí to být symlink ven – stejně jako ho nevypisuje relay_source_info.
            if (os.path.islink(target_p) or not mc.is_path_within(full_p, target_p)
                    or not os.path.isfile(target_p)):
                raise HTTPException(status_code=404, detail="Requested relative file not found")

        f_sz = os.path.getsize(target_p)
        headers = {
            "Content-Length": str(f_sz),
            "Content-Disposition": _safe_content_disposition(os.path.basename(target_p))
        }
        return StreamingResponse(
            _iter_large_file_16mb(target_p, job_id, worker_name),
            media_type="application/octet-stream",
            headers=headers
        )


RELAY_UPLOAD_STATUSES = ("IN_PROGRESS", "COPYING", "WAITING_DISK")
_relay_final_paths = OrderedDict()   # job_id → (požadovaná cesta, uložená cesta) – LRU (nikdy .clear())
_relay_final_lock = threading.Lock()
_RELAY_FINAL_PATHS_MAX = 1000


@fastapi_app.put("/api/jobs/{job_id}/relay_upload")
async def relay_upload_output_file(
    job_id: int,
    request: Request,
    worker_name: str = "",
    dest_filename: str = "",
    subfolder: str = "",
    expected_size: int = 0
):
    now_t = time.time()
    if worker_name:
        mc.active_workers[worker_name] = now_t

    hdr_name = urllib.parse.unquote(request.headers.get("x-hb-worker-name") or "")[:80]

    def _prepare_relay_target():
        with db_scope() as db:
            job = db.query(models.Job).filter(models.Job.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            # Výstup přijmout jen pro úlohu, která se právě převádí / ukládá, a jen od workeru, který ji má.
            if (
                job.status not in RELAY_UPLOAD_STATUSES
                or not job.worker_name
                or job.worker_name != worker_name
                or (hdr_name and hdr_name != worker_name)
            ):
                mc.append_manager_log(
                    f"[SECURITY] Odmítnut výstup úlohy {job_id} přes HTTP relay od {hdr_name or worker_name or '?'} "
                    f"(stav {job.status}, worker úlohy {job.worker_name or '-'})."
                )
                raise HTTPException(status_code=409, detail="relay_upload_not_allowed")
            mc.job_heartbeats[job_id] = time.time()
            d_fn_l = os.path.basename((dest_filename or job.dest_path or f"job_{job_id}.mkv").replace("\\", "/"))
            src_orig_l = job.source_path or mc.extract_orig_source_from_payload(job.hb_json_payload)

        out_root = mc.get_manager_outputs_dir()
        target_dir = out_root
        if subfolder:
            # Jediná úroveň podsložky; tečky na krajích pryč -> "..", "." ani "..\\" nevyjdou z Outputs.
            safe_sub = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', subfolder.strip()).strip(" .")
            if safe_sub:
                target_dir = os.path.join(out_root, safe_sub)
        if d_fn_l in ("", ".", ".."):
            d_fn_l = f"job_{job_id}.mkv"
        out_real = os.path.realpath(out_root)
        want_p_l = os.path.normpath(os.path.join(target_dir, d_fn_l))
        if os.path.commonpath([out_real, os.path.realpath(want_p_l)]) != out_real:
            raise HTTPException(status_code=400, detail="Invalid destination path.")
        # Pojistka: do Inputs (zdroje jsou jen pro čtení) ani přes zdroj úlohy se nikdy nezapisuje.
        in_root = mc.get_manager_inputs_dir()
        src_full, _, src_found = mc.resolve_input_path_smart(src_orig_l, in_root)
        if mc.is_path_within(in_root, want_p_l) or (src_found and os.path.realpath(src_full) == os.path.realpath(want_p_l)):
            mc.append_manager_log(mc.T("api_relay_target_in_inputs", job_id, want_p_l))
            raise HTTPException(status_code=409, detail="output_target_in_inputs")

        mc.ensure_shared_dir(target_dir)   # podsložka „ke kontrole“ v Outputs: otevřená práva jako dřív
        # Existující soubor v Outputs se nikdy nepřepíše → „Název [ID 1234].mkv“. Opakovaný pokus téže úlohy
        # (ztracená odpověď, nový pokus workeru) použije stejné jméno – je to její vlastní soubor.
        with _relay_final_lock:
            rec = _relay_final_paths.get(job_id)
            own_l = bool(rec and rec[0] == want_p_l)
            final_p_l = rec[1] if own_l else mc.free_output_path(want_p_l, job_id)
        tmp_p_l = f"{final_p_l}.{job_id}.{secrets.token_hex(4)}.relay.tmp"
        return d_fn_l, want_p_l, own_l, final_p_l, tmp_p_l

    d_fn, want_p, own, final_p, tmp_p = await anyio.to_thread.run_sync(_prepare_relay_target)

    def _finalize_relay_file():
        # Před finálním přejmenováním ověřit, že úloha nebyla mezitím zrušena ani předána jinému workeru
        with db_scope() as db:
            j_chk = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id == job_id)
                .first()
            )
            if not j_chk or j_chk.status not in RELAY_UPLOAD_STATUSES or j_chk.worker_name != worker_name:
                raise HTTPException(status_code=409, detail="relay_upload_aborted_stale")
        with _relay_final_lock:
            eff_final = final_p
            if not own and os.path.lexists(eff_final):  # mezitím vznikl jiný soubor stejného jména → další volné jméno
                eff_final = mc.free_output_path(eff_final, job_id)
            os.replace(tmp_p, eff_final)
            mc._lru_set(_relay_final_paths, job_id, (want_p, eff_final), _RELAY_FINAL_PATHS_MAX)
        try:
            os.chmod(eff_final, 0o666)
        except Exception:
            pass
        return eff_final

    written = 0
    wf = None
    try:
        wf = await anyio.to_thread.run_sync(lambda: open(tmp_p, "wb", buffering=16 * 1024 * 1024))
        pending = bytearray()
        flush_threshold = 4 * 1024 * 1024
        try:
            async for chunk in request.stream():
                if chunk:
                    pending.extend(chunk)
                    written += len(chunk)
                    if len(pending) >= flush_threshold:
                        buf_out = bytes(pending)
                        pending.clear()
                        await anyio.to_thread.run_sync(wf.write, buf_out)
                    now_u = time.time()
                    mc.job_heartbeats[job_id] = now_u
                    if worker_name:
                        mc.active_workers[worker_name] = now_u
            if pending:
                await anyio.to_thread.run_sync(wf.write, bytes(pending))
                pending.clear()
        finally:
            await anyio.to_thread.run_sync(wf.close)
            wf = None

        if expected_size > 0 and written != expected_size:
            raise RuntimeError(f"Incomplete relay upload ({written} != expected {expected_size} bytes)")

        eff_final_p = await anyio.to_thread.run_sync(_finalize_relay_file)
        f_name = os.path.basename(eff_final_p)
        if f_name != d_fn:
            mc.append_manager_log(mc.T("api_relay_saved_new_name", job_id, d_fn, f_name))
        mc.append_manager_log(
            mc.T("api_http_relay_received_verified", job_id, f_name, f'{written / 1024 ** 3:.2f}', worker_name)
        )
        return {"ok": True, "final_dest_path": eff_final_p, "size": written}
    except HTTPException:
        try:
            if wf is not None:
                await anyio.to_thread.run_sync(wf.close)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_p):
                await anyio.to_thread.run_sync(os.remove, tmp_p)
        except Exception:
            pass
        raise
    except BaseException as e:
        try:
            if wf is not None:
                await anyio.to_thread.run_sync(wf.close)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_p):
                await anyio.to_thread.run_sync(os.remove, tmp_p)
        except Exception:
            pass
        if isinstance(e, Exception):
            mc.logger.error(f"Relay upload failed for job {job_id}: {e}")
            raise HTTPException(status_code=500, detail="relay_upload_failed")
        raise


# ==============================================================================
# PŘIŘAZOVÁNÍ ÚLOH, PRŮBĚH, REZERVACE DISKU A DOKONČENÍ
# ==============================================================================
auto_first_build_triggered = False

# ------------------------------------------------------------------------------
# AUTOMATICKÝ SYSTEM_BUILD (přepínač auto_update_binaries)
# ------------------------------------------------------------------------------
# Spouští se z: startu, periodické smyčky, heartbeatu Linux workeru (/api/jobs/next, /api/workers/ping),
# načtení stavu (/api/v2/state), kontroly verzí (/api/v2/binaries/check_upstream) a uložení nastavení.
auto_build_lock = threading.Lock()
auto_build_state = {"last_tick": 0.0, "last_sig": "", "last_enqueue": 0.0}
AUTO_BUILD_THROTTLE_S = 20.0
AUTO_BUILD_RETRY_SAME_S = 1800.0   # stejná sada nástrojů se po (neúspěšném) buildu zkusí znovu nejdřív za 30 min


def auto_build_tick(source: str = "", force: bool = False):
    global auto_first_build_triggered
    now_t = time.time()
    if not force and (now_t - auto_build_state["last_tick"]) < AUTO_BUILD_THROTTLE_S:
        return None
    if not auto_build_lock.acquire(blocking=False):
        return None
    try:
        auto_build_state["last_tick"] = now_t
        if not mc.has_any_connected_or_registered_worker():
            return None
        first_run = bool(mc.AppState.auto_build_on_first_run and not auto_first_build_triggered)
        if not (mc.AppState.auto_update_binaries or first_run):
            return None
        if mc.binary_build_state.get("status") in ("QUEUED", "BUILDING"):
            return None

        # Síťový dotaz na upstream verze jen z pomalých zdrojů (ne z heartbeatu workeru).
        if mc.AppState.auto_update_binaries and source in ("watcher", "startup", "check", "settings"):
            try:
                mc.get_cached_upstream_versions(mc.UPSTREAM_CHECK_MIN_INTERVAL_S)
            except Exception:
                pass

        mf = mc.get_all_binaries_manifest()
        missing_any = bool(mc.evaluate_binaries_health(mf)[0])
        if mc.AppState.auto_update_binaries:
            targets = mc.get_outdated_or_missing_build_targets(mf, auto=True)
        else:  # jen první spuštění: chybí nástroje a je zapnuto auto_build_on_first_run
            targets = ["all"] if missing_any else []
        if not targets:
            return None

        mode_prefix = "lin" if mc.has_any_linux_worker() else "win"
        sig = f"{mode_prefix}:{'|'.join(sorted(targets))}"
        if sig == auto_build_state["last_sig"] and (now_t - auto_build_state["last_enqueue"]) < AUTO_BUILD_RETRY_SAME_S:
            return None

        with db_scope() as db:
            sel_linux = mc.select_best_linux_builder_worker(db)[0]
            if not sel_linux:
                if mc.has_any_windows_worker():
                    win_targets = mc.get_outdated_or_missing_build_targets(mf, auto=True, windows_only=True) if mc.AppState.auto_update_binaries else (["all"] if missing_any else [])
                    if not win_targets:
                        return None
                    res_dl = mc.start_manager_windows_binaries_download(win_targets, force_rebuild=False)
                    if res_dl.get("ok") and not res_dl.get("already_up_to_date"):
                        auto_build_state["last_sig"] = sig
                        auto_build_state["last_enqueue"] = time.time()
                        if first_run:
                            auto_first_build_triggered = True
                        return win_targets
                return None  # žádný online Linux worker -> zkusí se při příštím heartbeatu
            has_sys = db.query(models.Job).filter(
                models.Job.preset_name == "SYSTEM_BUILD",
                models.Job.status.in_(["WAITING"] + mc.ACTIVE_JOB_STATUSES)
            ).count()
            if has_sys:
                return None
            enqueue_system_build_job_internal(db, targets, False)

        auto_build_state["last_sig"] = sig
        auto_build_state["last_enqueue"] = time.time()
        if first_run:
            auto_first_build_triggered = True
        mc.append_manager_log(mc.T("api_auto_build_enqueued", source or "auto", ", ".join(targets)))
        return targets
    except HTTPException:
        return None
    except Exception as e:
        mc.logger.warning(f"auto_build_tick({source}) failed: {e}")
        return None
    finally:
        auto_build_lock.release()


def auto_build_tick_async(source: str = "", force: bool = False):
    """Spustí kontrolu na pozadí (neblokuje heartbeat / odpověď API), s throttlingem."""
    if not force and (time.time() - auto_build_state["last_tick"]) < AUTO_BUILD_THROTTLE_S:
        return
    if auto_build_lock.locked():
        return
    threading.Thread(target=auto_build_tick, args=(source, force), name="hb-auto-build", daemon=True).start()
# Globální zámek přidělování úloh: výběr čekající úlohy + přepnutí na IN_PROGRESS
# proběhne atomicky, takže dva workery nikdy nedostanou stejný film (ani duplicitní
# SYSTEM_BUILD), i když se jejich /api/jobs/next potkají ve stejné milisekundě.
job_assignment_lock = threading.Lock()


def _claim_waiting_job(db, job_id: int) -> bool:
    """Atomický podmíněný UPDATE (WHERE status='WAITING') jako druhá pojistka proti souběhu."""
    claimed = db.query(models.Job).filter(
        models.Job.id == job_id,
        models.Job.status == "WAITING"
    ).update({models.Job.status: "IN_PROGRESS"}, synchronize_session=False)
    return claimed == 1


@fastapi_app.get("/api/jobs/next")
def get_next_job(
    request: Request,
    worker_name: str = "unknown",
    version: str = VERSION,
    running: str = None,
    telemetry: Optional[str] = None,
    power_mode_override: Optional[str] = None,
    custom_cores_override: Optional[str] = None,
    ui_lang_override: Optional[str] = None,
    is_enabled_override: Optional[bool] = None,
    update_blocked: Optional[bool] = None,
    held: Optional[str] = None,
    storage_ok: Optional[str] = None
):
    w_key = request.headers.get("x-hb-worker-key") or ""
    key_sha = hashlib.sha256(w_key.encode("utf-8")).hexdigest() if w_key else ""
    with job_assignment_lock:
        return _get_next_job_locked(
            worker_name, version, running, telemetry,
            power_mode_override, custom_cores_override, ui_lang_override, is_enabled_override, bool(update_blocked), held, storage_ok,
            worker_key_sha=key_sha
        )


def _get_next_job_locked(
    worker_name: str,
    version: str,
    running: Optional[str],
    telemetry: Optional[str],
    power_mode_override: Optional[str],
    custom_cores_override: Optional[str],
    ui_lang_override: Optional[str],
    is_enabled_override: Optional[bool],
    update_blocked: bool,
    held: Optional[str] = None,
    storage_ok: Optional[str] = None,
    worker_key_sha: str = ""
):
    now_t = time.time()
    mc.active_workers[worker_name] = now_t
    mc.worker_versions[worker_name] = version
    if telemetry:
        t_obj = mc.safe_json_loads(telemetry, default=None)
        if isinstance(t_obj, dict):
            mc.record_telemetry_sample(worker_name, t_obj)
    if ui_lang_override and ui_lang_override in ("en", "cs") and ui_lang_override != mc.AppState.ui_lang:
        mc.AppState.ui_lang = ui_lang_override
        mc.save_manager_config()

    try:
        with db_scope(immediate=True) as db:
            rep_run = []
            if running is not None:
                try:
                    rep_run = [int(x) for x in json.loads(running)]
                    for r_id in rep_run:
                        rj = (
                            db.query(models.Job)
                            .options(defer(models.Job.hb_json_payload))
                            .filter(models.Job.id == r_id)
                            .first()
                        )
                        if rj and rj.status in mc.ACTIVE_JOB_STATUSES and rj.worker_name == worker_name:
                            mc.job_heartbeats[r_id] = now_t
                        elif rj and rj.status == "WAITING" and (rj.worker_name is None or rj.worker_name == worker_name):
                            if _claim_waiting_job(db, rj.id):
                                rj.status = "IN_PROGRESS"
                                rj.worker_name = worker_name
                                mc.job_heartbeats[r_id] = now_t

                    rep_held_pre = set()
                    if held is not None:
                        try:
                            rep_held_pre = {int(x) for x in json.loads(held)}
                        except Exception:
                            rep_held_pre = set()

                    db_running = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(
                            models.Job.status.in_(mc.COMPUTE_ACTIVE_STATUSES),
                            models.Job.worker_name == worker_name
                        )
                        .all()
                    )
                    for dj in db_running:
                        if dj.id in rep_held_pre:
                            # Worker už dokončil převod a drží hotový soubor lokálně (např. Manager byl při /waiting_disk nedostupný)
                            dj.status = "WAITING_DISK"
                            dj.progress = 100.0
                            dj.is_paused = False
                            mc.job_heartbeats[dj.id] = now_t
                            with mc.held_sync_lock:
                                mc.disk_hold_entered_ts[dj.id] = now_t
                                mc.orphaned_disk_missing_since.pop(dj.id, None)
                            continue
                        if dj.id not in rep_run:
                            if now_t - mc.job_assigned_time.get(dj.id, 0) < 30.0:
                                continue
                            mc.reset_job_for_requeue(dj, priority=mc.next_top_priority(db), clear_errors=False)
                            mc.append_manager_log(
                                mc.T("api_job_returned_top_queue", dj.id, dj.original_filename, worker_name)
                            )
                    db.commit()
                except Exception as e:
                    db.rollback()
                    mc.logger.error(f"Error syncing running jobs: {e}")

            pol = mc.get_or_create_worker_policy(db, worker_name)
            pol_dirty = False
            if power_mode_override and power_mode_override in mc.VALID_POWER_MODES:
                pol.power_mode = power_mode_override
                pol_dirty = True
            if custom_cores_override is not None:
                pol.custom_cores = mc.serialize_custom_cores_list(custom_cores_override)
                pol_dirty = True
            if is_enabled_override is not None:
                pol.is_enabled = bool(is_enabled_override)
                if pol.is_enabled:
                    mc.AppState.maintenance_mode = False
                    mc.clear_worker_intake_off_reason(worker_name)
                pol_dirty = True
            if pol_dirty:
                db.commit()
                mc.save_manager_config(db)
            cleanup_held_ids = []
            if held is not None:
                try:
                    rep_held = [int(x) for x in json.loads(held)]
                except Exception:
                    rep_held = None
                if rep_held is not None:
                    if mc.reconcile_worker_held_disk_jobs(db, worker_name, rep_held, active_running_ids=rep_run):
                        db.commit()
                    cleanup_held_ids = mc.compute_cleanup_held_job_ids(db, worker_name, rep_held)
            p_mode = getattr(pol, 'power_mode', 'AUTO') or "AUTO"
            c_cores = mc.parse_custom_cores_list(getattr(pol, 'custom_cores', ''))
            is_en = bool(pol.is_enabled and not mc.AppState.maintenance_mode)
            upd_now = mc.take_worker_update_now(worker_name, version)
            if upd_now:
                update_blocked = False        # O4: „Aktualizovat teď“ – čekání po neúspěchu neplatí
            upd_req = bool(version and version != mc.WORKER_VERSION and not update_blocked)
            idle_resp = {
                "job_id": None,
                "power_mode": p_mode,
                "custom_cores": c_cores,
                "is_enabled": is_en,
                "ui_lang": mc.AppState.ui_lang,
                "telemetry_source": mc.AppState.telemetry_source,
                "ram_format": mc.AppState.ram_format,
                "binary_generation": mc.AppState.binary_generation,
                "update_required": upd_req,
                "update_now": upd_now,
                "target_version": mc.WORKER_VERSION,
                "cleanup_held_job_ids": cleanup_held_ids,
                "pending_windows_settings": mc.get_pending_worker_windows_settings(worker_name),
            }

            if upd_req and len(rep_run) == 0 and not update_blocked:
                return idle_resp

            auto_build_tick_async("heartbeat")

            wait_all = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.status == "WAITING")
                .order_by(models.Job.priority.asc(), models.Job.created_at.asc(), models.Job.id.asc())
                .all()
            )
            if rep_run:
                wait_all = [j for j in wait_all if j.id not in rep_run]

            # SYSTEM_BUILD (FFmpeg / HBCLI / MKVToolNix / 7-Zip / LAUNCHER) má vždy Pri:MAX:
            # přiděluje se PŘED kontrolou úložiště, video slotů i pozastaveného příjmu video úloh,
            # Tier 1 = nejsilnější volný Linux worker, Tier 2 = nejsilnější vytížený (souběžně mimo sloty).
            sys_j = next((j for j in wait_all if mc.is_system_build_job(j)), None)
            if sys_j and mc.is_linux_worker(worker_name):
                t_builder, fb_b, _ = mc.select_best_linux_builder_worker(db)
                wait_age = now_t - (mc.binary_build_state.get("started_at") or now_t)
                if t_builder == worker_name or wait_age > 12.0:
                    if not _claim_waiting_job(db, sys_j.id):
                        db.rollback()
                        return idle_resp
                    sys_j.status = "IN_PROGRESS"
                    setattr(sys_j, "is_paused", False)
                    sys_j.progress = 1.0
                    sys_j.worker_name = worker_name
                    sys_j.worker_version = version
                    db.commit()
                    db.refresh(sys_j)
                    jid = sys_j.id
                    j_pl = copy.deepcopy(sys_j.hb_json_payload)
                    mc.job_heartbeats[jid] = now_t
                    mc.job_assigned_time[jid] = now_t
                    with mc.build_lock:
                        mc.binary_build_state["status"] = "BUILDING"
                        mc.binary_build_state["job_id"] = jid
                        mc.binary_build_state["assigned_worker"] = worker_name
                        mc.binary_build_state["assigned_key_sha"] = worker_key_sha
                        mc.binary_build_state["fallback_used"] = fb_b or (t_builder != worker_name)
                    mc.append_build_console_line(
                        mc.T("api_worker_v_picked_up", worker_name, version, jid)
                    )
                    return {
                        "job_id": jid,
                        "payload": j_pl,
                        "is_system_build": True,
                        "power_mode": p_mode,
                        "is_enabled": is_en,
                        "ui_lang": mc.AppState.ui_lang,
                        "telemetry_source": mc.AppState.telemetry_source,
                        "ram_format": mc.AppState.ram_format,
                        "binary_generation": mc.AppState.binary_generation
                    }

            # Pokud worker hlásí nedostupné úložiště (SMB odpojeno, vypnutá VPN a není relay), nepřidělovat video úlohy
            if storage_ok is not None and str(storage_ok).strip().lower() in ("false", "0", "no"):
                return idle_resp

            if not pol.is_enabled or mc.AppState.maintenance_mode:
                return idle_resp
            if mc.worker_cooldown_left(worker_name) > 0:     # pauza po chybě přístupu ke zdroji (S1)
                return idle_resp

            compute_jobs = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(
                    models.Job.status.in_(mc.COMPUTE_ACTIVE_STATUSES),
                    models.Job.worker_name == worker_name
                )
                .all()
            )
            held_disk_jobs = db.query(models.Job.id).filter(
                models.Job.status == "WAITING_DISK",
                models.Job.worker_name == worker_name
            ).count()

            used_slots = sum(mc.get_job_weight(j) for j in compute_jobs)
            can_4k = mc.worker_allows_4k(pol)
            def_ms = mc.default_slots_for_worker(pol)
            max_slots = mc.clamp_num(getattr(pol, "max_slots", def_ms), 1, 16, def_ms, as_int=True)

            if held_disk_jobs > 0:
                if not mc.AppState.continue_encoding_on_disk_full or not pol.use_local_temp:
                    return idle_resp
                w_free_temp = float(((mc.worker_telemetry.get(worker_name) or {}).get("diag") or {}).get("temp_free_gb") or 0.0)
                if w_free_temp < 25.0:
                    return idle_resp

            if used_slots >= max_slots:
                return idle_resp

            wait_list = [j for j in wait_all if not mc.is_system_build_job(j)]
            can_premux = mc.is_worker_premux_capable(worker_name, bool(pol.use_local_temp))
            if mc.AppState.smart_premux and not can_premux:
                wait_list = [j for j in wait_list if not mc.is_job_disc_or_iso(j)]

            chosen = None
            cap_low = str(getattr(pol, "capability", "all") or "all").strip().lower()
            # Upřednostnit 4K jen u workeru se 4+ sloty – jinak by 4K nikdy nevzal a čekal by donekonečna bez práce (P2)
            if pol.prioritize_4k and can_4k and max_slots >= 4 and cap_low not in ("1080p", "sd", "720p") and wait_list:
                f_4k = next((j for j in wait_list if mc.get_job_weight(j) >= 4), None)
                if f_4k:
                    if used_slots + 4 <= max_slots:
                        chosen = f_4k
                    else:
                        return idle_resp

            if not chosen:
                for j_cand in wait_list:
                    jw = mc.get_job_weight(j_cand)
                    is_4k = (jw >= 4)
                    if (not can_4k or cap_low in ("1080p", "sd", "720p")) and is_4k:
                        continue
                    if cap_low in ("sd", "720p") and jw > 1:
                        continue
                    if cap_low == "4k" and not is_4k:
                        continue
                    if used_slots + jw > max_slots:
                        continue
                    chosen = j_cand
                    break

            if chosen:
                if not _claim_waiting_job(db, chosen.id):
                    db.rollback()
                    return idle_resp
                chosen.status = "IN_PROGRESS"
                setattr(chosen, "is_paused", False)
                chosen.progress = 0.0
                chosen.worker_name = worker_name
                chosen.worker_version = version
                if (chosen.attempt_count or 0) == 0:
                    chosen.error_code = None
                    chosen.error_detail = None
                setattr(chosen, "disk_hold_reason", "")
                db.commit()
                db.refresh(chosen)

                jid = chosen.id
                j_pl = copy.deepcopy(chosen.hb_json_payload)
                orig_s = chosen.source_path or mc.extract_orig_source_from_payload(j_pl)
                _, ver_rel_s, _ = mc.resolve_input_path_smart(orig_s, mc.get_manager_inputs_dir())

                mc.job_heartbeats[jid] = now_t
                mc.job_assigned_time[jid] = now_t
                mc.append_manager_log(
                    mc.T("api_job_assigned_worker", jid, chosen.original_filename, worker_name)
                )

                stg_m = getattr(pol, 'staging_mode', mc.AppState.default_staging_mode) or mc.AppState.default_staging_mode
                use_loc = bool(pol.use_local_temp)
                eff_premux = bool(mc.AppState.smart_premux and can_premux)

                return {
                    "job_id": jid,
                    "payload": j_pl,
                    "verified_rel_source": ver_rel_s,
                    "use_local_temp": use_loc,
                    "staging_mode": stg_m,
                    "convert_ass_to_srt": mc.AppState.convert_ass_to_srt,
                    "ass_srt_mode": mc.AppState.ass_srt_mode,
                    "burn_in_mode": mc.AppState.burn_in_subs,
                    "dts_hd_mode": mc.AppState.dts_hd_mode,
                    "auto_clean_empty_subs": mc.AppState.auto_clean_empty_subs,
                    "auto_extract_forced_subs": mc.AppState.auto_extract_forced_subs,
                    "write_mkv_stats": mc.AppState.write_mkv_stats,
                    "lpcm_passthru": mc.AppState.lpcm_passthru,
                    "smart_premux": eff_premux,
                    "ineffective_action": mc.AppState.ineffective_action,
                    "ineffective_prefix": mc.AppState.ineffective_prefix,
                    "ineffective_subfolder": mc.AppState.ineffective_subfolder,
                    "min_free_output_gb": mc.AppState.min_free_output_gb,
                    "power_mode": p_mode,
                    "custom_cores": c_cores,
                    "is_enabled": is_en,
                    "ui_lang": mc.AppState.ui_lang,
                    "telemetry_source": mc.AppState.telemetry_source,
                    "ram_format": mc.AppState.ram_format,
                    "binary_generation": mc.AppState.binary_generation,
                    "cleanup_held_job_ids": cleanup_held_ids,
                    "pending_windows_settings": mc.get_pending_worker_windows_settings(worker_name),
                }

            return idle_resp
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in get_next_job: {e}")
        raise HTTPException(status_code=503, detail="database_busy")


# ------------------------------------------------------------------------------
# P3: PO VÝPADKU HLÁSÍ STEJNOU ÚLOHU DVA WORKERY (úloha se mezitím vrátila do fronty a vzal ji jiný).
# Vyhrává hotový převod (upload / WAITING_DISK / dokončení), jinak ten, kdo je dál ve fázi, pak kratší ETA
# (rozdíl ≥ 5 min), bez ETA procenta (rozdíl ≥ 10 %). Při malém rozdílu zůstává současný worker (žádné přeskakování).
# Poražený dostane REVOKED s důvodem → zastaví, uklidí Temp, zapíše důvod do logu.
# ------------------------------------------------------------------------------
PHASE_RANK = {"STAGING": 1, "PREMUX": 2, "IN_PROGRESS": 3, "COPYING": 4, "WAITING_DISK": 5}
TAKEOVER_ETA_MARGIN_S = 300
TAKEOVER_PROGRESS_MARGIN = 10.0
OWNER_GONE_S = 120
_revoke_notes = OrderedDict()     # (job_id, worker) → důvod pro REVOKED (LRU, nikdy .clear())
_revoke_logged = OrderedDict()    # (job_id, worker) → True (LRU, nikdy .clear())
_revoke_lock = threading.Lock()
_REVOKE_CACHE_MAX = 500


def _get_revoke_note(job_id: int, worker: str) -> str:
    with _revoke_lock:
        return _revoke_notes.get((job_id, worker), "")


def _has_revoke_note(job_id: int, worker: str) -> bool:
    with _revoke_lock:
        return (job_id, worker) in _revoke_notes


def _pop_revoke_note(job_id: int, worker: str):
    with _revoke_lock:
        _revoke_notes.pop((job_id, worker), None)


def _hdr_worker_name(request) -> str:
    try:
        return urllib.parse.unquote(request.headers.get("x-hb-worker-name") or "")[:80].strip()
    except Exception:
        return ""


def _owner_gone(owner: str) -> bool:
    return (time.time() - mc.active_workers.get(owner or "", 0)) > OWNER_GONE_S


def _contender_wins(job, c_rank: int, c_progress: float = 0.0, c_eta: int = 0):
    """Vrací (vyhrává vyzyvatel?, důvod). Vlastník = job.worker_name (jeho poslední hlášení je v řádku úlohy)."""
    owner = job.worker_name or ""
    o_rank = PHASE_RANK.get(job.status, 3)
    if _owner_gone(owner):
        return True, mc.T("api_takeover_r_gone", owner)
    if o_rank >= 4 and c_rank >= 4:
        return False, mc.T("api_takeover_r_owner_finished", owner)
    if c_rank >= 4:
        return True, mc.T("api_takeover_r_finished")
    if o_rank >= 4:
        return False, mc.T("api_takeover_r_owner_finished", owner)
    if c_rank != o_rank:
        return (c_rank > o_rank), mc.T("api_takeover_r_phase")
    o_eta, c_eta = int(job.eta_sec or 0), int(c_eta or 0)
    if o_eta > 0 and c_eta > 0:
        if o_eta - c_eta >= TAKEOVER_ETA_MARGIN_S:
            return True, mc.T("api_takeover_r_eta", int(round((o_eta - c_eta) / 60)))
        return False, mc.T("api_takeover_r_close")
    diff = float(c_progress or 0.0) - float(job.progress or 0.0)
    if diff >= TAKEOVER_PROGRESS_MARGIN:
        return True, mc.T("api_takeover_r_progress", int(diff))
    return False, mc.T("api_takeover_r_close")


def _hand_over_job(job, new_owner: str, reason: str):
    """Úloha přechází na jiného workera; starý dostane při příštím hlášení REVOKED s důvodem."""
    old = job.worker_name or ""
    if old and old != new_owner:
        with _revoke_lock:
            mc._lru_set(_revoke_notes, (job.id, old), mc.T("api_revoked_note", new_owner, reason), _REVOKE_CACHE_MAX)
        mc.append_manager_log(mc.T("api_job_takeover", job.id, new_owner, old, reason))
    job.worker_name = new_owner


def _revoked_resp(job_id: int, worker: str, owner: str, reason: str = "", **extra) -> dict:
    note = _get_revoke_note(job_id, worker) or (mc.T("api_revoked_note", owner, reason) if owner else "")
    k = (job_id, worker)
    should_log = False
    if reason:
        with _revoke_lock:
            if k not in _revoke_logged:
                mc._lru_set(_revoke_logged, k, True, _REVOKE_CACHE_MAX)
                should_log = True
    if should_log:
        mc.append_manager_log(mc.T("api_takeover_kept", job_id, worker, owner, reason))
    return {"success": True, "status": "REVOKED", "reason": note, "is_paused": False, **extra}


@fastapi_app.post("/api/jobs/{job_id}/progress")
def update_progress(job_id: int, data: ProgressData):
    now_t = time.time()
    w_clean = re.sub(r"\s*\(v[\d\.]+\)$", "", str(data.worker_name or "")).strip()
    valid_w = bool(w_clean) and w_clean.lower() != "unknown"
    if valid_w:
        mc.active_workers[data.worker_name] = now_t
        mc.worker_versions[data.worker_name] = data.version or mc.WORKER_VERSION
        if data.telemetry:
            mc.record_telemetry_sample(data.worker_name, data.telemetry)
        if data.log_lines:
            mc.append_worker_logs(data.worker_name, data.log_lines)
        if data.windows_settings_state and isinstance(data.windows_settings_state, dict):
            mc.record_worker_windows_settings_state(data.worker_name, data.windows_settings_state)
    if data.ui_lang_override and data.ui_lang_override in ("en", "cs") and data.ui_lang_override != mc.AppState.ui_lang:
        mc.AppState.ui_lang = data.ui_lang_override
        mc.save_manager_config()

    try:
        with db_scope(immediate=True) as db:
            pol = db.query(models.WorkerPolicy).filter(models.WorkerPolicy.worker_name == data.worker_name).first() if valid_w else None
            pol_dirty = False
            if pol and data.power_mode_override and data.power_mode_override in mc.VALID_POWER_MODES:
                pol.power_mode = data.power_mode_override
                pol_dirty = True
            if pol and data.custom_cores_override is not None:
                pol.custom_cores = mc.serialize_custom_cores_list(data.custom_cores_override)
                pol_dirty = True
            if pol and data.is_enabled_override is not None:
                pol.is_enabled = bool(data.is_enabled_override)
                if pol.is_enabled:
                    mc.AppState.maintenance_mode = False
                    mc.clear_worker_intake_off_reason(data.worker_name)
                pol_dirty = True
            if pol_dirty:
                db.commit()
                mc.save_manager_config(db)
            p_mode = (getattr(pol, 'power_mode', 'AUTO') or "AUTO") if pol else "AUTO"
            c_cores = mc.parse_custom_cores_list(getattr(pol, 'custom_cores', '')) if pol else []
            is_en = bool(pol.is_enabled and not mc.AppState.maintenance_mode) if pol else True

            job = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id == job_id)
                .first()
            )
            if not job:
                return {"success": True, "status": "CANCELLED", "is_paused": False, "power_mode": p_mode, "is_enabled": is_en, "ui_lang": mc.AppState.ui_lang, "telemetry_source": mc.AppState.telemetry_source, "ram_format": mc.AppState.ram_format}

            base_r = {"power_mode": p_mode, "is_enabled": is_en, "ui_lang": mc.AppState.ui_lang, "telemetry_source": mc.AppState.telemetry_source, "ram_format": mc.AppState.ram_format}
            if job.status == "COMPLETED" and valid_w and job.worker_name and job.worker_name != data.worker_name:
                # úlohu už dokončil jiný worker → tenhle ať zastaví (dřív dostal „COMPLETED“ a převáděl dál)
                return _revoked_resp(job_id, data.worker_name, job.worker_name, **base_r)
            if job.status in ["COMPLETED", "CANCELLED", "ERROR"]:
                return {"success": True, "status": job.status, "is_paused": False, **base_r}
            if job.status == "PAUSED":
                # Pozastavenou úlohu uživatel schválně pozastavil – update_progress ji nikdy samovolně neoživuje.
                return {"success": True, "status": "PAUSED", "is_paused": True, **base_r}

            if job.status == "WAITING" and valid_w and (job.worker_name is None or job.worker_name == data.worker_name):
                if _claim_waiting_job(db, job.id):
                    job.status = "IN_PROGRESS"
                    job.worker_name = data.worker_name
                else:
                    db.refresh(job)

            if valid_w and job.worker_name and job.worker_name != data.worker_name:
                if _has_revoke_note(job_id, data.worker_name):      # už jednou prohrál – žádné přeskakování zpět
                    return _revoked_resp(job_id, data.worker_name, job.worker_name, **base_r)
                c_rank = 3 if (float(data.fps_current or 0) > 0 or float(data.fps_avg or 0) > 0) else 1
                wins, why = _contender_wins(job, c_rank, data.progress, data.eta_sec)
                if not wins:
                    return _revoked_resp(job_id, data.worker_name, job.worker_name, why, **base_r)
                _hand_over_job(job, data.worker_name, why)
                job.status = "IN_PROGRESS"
                _pop_revoke_note(job_id, data.worker_name)

            mc.job_heartbeats[job_id] = now_t

            if data.is_paused_override is not None:
                setattr(job, "is_paused", bool(data.is_paused_override))

            job.progress = data.progress
            job.worker_version = data.version
            job.fps_current = data.fps_current
            job.fps_avg = data.fps_avg or data.fps_current
            job.speed_mbps = data.speed_mbps
            job.elapsed_sec = data.elapsed_sec
            job.eta_sec = data.eta_sec
            if data.transferred_bytes is not None:
                job.transferred_bytes = data.transferred_bytes
            if data.src_size_bytes is not None and data.src_size_bytes > 0:
                job.src_size_bytes = data.src_size_bytes
            if data.is_net_fallback is not None:
                job.is_net_fallback = data.is_net_fallback
            if data.net_fallback_reason:
                job.net_fallback_reason = str(data.net_fallback_reason)[:500]
            elif data.is_net_fallback is False:
                job.net_fallback_reason = None
            if data.v_bitrate_in_kbps is not None and data.v_bitrate_in_kbps > 0:
                job.v_bitrate_in_kbps = 0 if data.v_bitrate_in_kbps == 2500 else data.v_bitrate_in_kbps
            if data.a_bitrate_in_kbps is not None and data.a_bitrate_in_kbps > 0:
                job.a_bitrate_in_kbps = data.a_bitrate_in_kbps
            if data.premux_applied is not None:
                job.premux_applied = data.premux_applied
            if data.premux_status:
                setattr(job, "premux_status", data.premux_status)

            if mc.is_system_build_job(job):
                if data.step_message:
                    job.encoder_summary = data.step_message
                    job.dest_path = data.step_message
                with mc.build_lock:
                    mc.binary_build_state["status"] = "BUILDING"
                    mc.binary_build_state["progress"] = round(float(data.progress or 0.0), 1)
                    if data.step_message:
                        mc.binary_build_state["step_message"] = data.step_message
                    if data.updating_tools is not None:
                        mc.binary_build_state["updating_tools"] = list(data.updating_tools)
                if data.step_message:
                    mc.update_checklist_from_step_message(data.step_message)
                    mc.append_build_console_line(data.step_message)
            elif job.status in ("PREMUX", "IN_PROGRESS") and data.step_message is not None:
                setattr(job, "disk_hold_reason", data.step_message)

            st = job.status
            paused_flag = bool(getattr(job, "is_paused", False))
            force_up = (job_id in mc.manual_upload_release_ids)
            db.commit()
            return {
                "success": True,
                "status": st,
                "is_paused": paused_flag,
                "power_mode": p_mode,
                "custom_cores": c_cores,
                "is_enabled": is_en,
                "ui_lang": mc.AppState.ui_lang,
                "telemetry_source": mc.AppState.telemetry_source,
                "ram_format": mc.AppState.ram_format,
                "force_upload": force_up,
                "pending_windows_settings": mc.get_pending_worker_windows_settings(data.worker_name) if valid_w else None,
            }
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in update_progress #{job_id}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")


@fastapi_app.post("/api/jobs/{job_id}/request_upload")
def job_request_upload_slot(job_id: int, req: UploadSlotRequest):
    mc.active_workers[req.worker_name] = time.time()
    slot_reserved = False
    try:
        with db_scope(commit=True, immediate=True) as db:
            job = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id == job_id)
                .first()
            )
            if not job or job.status in ("CANCELLED", "COMPLETED"):
                return {"allowed": False, "obsolete": True, "reason": mc.T("api_upload_obsolete_job")}
            if job.worker_name and job.worker_name != req.worker_name:
                wins, why = _contender_wins(job, 4)
                if not wins:
                    return {"allowed": False, "obsolete": True, "reason": mc.T("api_revoked_note", job.worker_name, why)}
                _hand_over_job(job, req.worker_name, why)
                _pop_revoke_note(job_id, req.worker_name)

            mc.job_heartbeats[job_id] = time.time()
            res = mc.request_output_upload_slot(job_id, req.worker_name, req.out_size_bytes, force_override=req.force)
            slot_reserved = bool(res.get("allowed"))

            # i úloha ve stavu ERROR / WAITING (worker po výpadku vypadl z fronty) se hotovým převodem „oživí“
            if job.status in ("WAITING", "ERROR"):
                mc.append_manager_log(mc.T("api_job_finished_back", job_id, req.worker_name))
            setattr(job, "is_paused", False)
            job.worker_name = req.worker_name
            if req.out_size_bytes > 0:
                job.out_size_bytes = req.out_size_bytes
            if res["allowed"]:
                job.status = "COPYING"
                setattr(job, "disk_hold_reason", "")
            else:
                was_waiting = (job.status == "WAITING_DISK")
                job.status = "WAITING_DISK"
                job.progress = 100.0
                job.fps_current = 0.0
                job.speed_mbps = 0.0
                setattr(job, "disk_hold_reason", res["reason"])
                mc.record_disk_hold_entered(job_id)
                if not was_waiting:
                    mc.append_manager_log(
                        mc.T("api_job_held_waiting_disk", job_id, job.original_filename, req.worker_name, res['reason'])
                    )
        return res
    except HTTPException:
        if slot_reserved:
            mc.release_output_upload_slot(job_id)
        raise
    except Exception as e:
        if slot_reserved:
            mc.release_output_upload_slot(job_id)
        mc.logger.error(f"Error in job_request_upload_slot #{job_id}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")


@fastapi_app.post("/api/jobs/{job_id}/waiting_disk")
def job_report_waiting_disk(job_id: int, req: WaitingDiskReport):
    mc.active_workers[req.worker_name] = time.time()

    obsolete = False
    try:
        with db_scope(commit=True, immediate=True) as db:
            job = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id == job_id)
                .first()
            )
            if job and job.worker_name and job.worker_name != req.worker_name \
                    and job.status in ("IN_PROGRESS", "STAGING", "PREMUX", "COPYING", "WAITING_DISK"):
                wins, why = _contender_wins(job, 5)           # hotový převod čeká na místo → vyhrává nad nedokončeným
                if wins:
                    _hand_over_job(job, req.worker_name, why)
                    _pop_revoke_note(job_id, req.worker_name)
            if (
                job
                and job.status in ("IN_PROGRESS", "STAGING", "PREMUX", "COPYING", "WAITING_DISK", "WAITING", "ERROR")
                and (job.worker_name is None or job.worker_name == req.worker_name)
            ):
                mc.release_output_upload_slot(job_id)
                mc.job_heartbeats[job_id] = time.time()
                if job.status in ("WAITING", "ERROR"):
                    mc.append_manager_log(mc.T("api_job_finished_back", job_id, req.worker_name))
                job.status = "WAITING_DISK"
                setattr(job, "is_paused", False)
                job.worker_name = req.worker_name
                job.progress = 100.0
                job.fps_current = 0.0
                job.speed_mbps = 0.0
                if req.out_size_bytes > 0:
                    job.out_size_bytes = req.out_size_bytes
                if req.src_size_bytes and req.src_size_bytes > 0:
                    job.src_size_bytes = req.src_size_bytes
                if req.v_bitrate_in_kbps and req.v_bitrate_in_kbps != 2500:
                    job.v_bitrate_in_kbps = req.v_bitrate_in_kbps
                if req.a_bitrate_in_kbps:
                    job.a_bitrate_in_kbps = req.a_bitrate_in_kbps
                if req.v_bitrate_out_kbps:
                    job.v_bitrate_out_kbps = req.v_bitrate_out_kbps
                if req.a_bitrate_out_kbps:
                    job.a_bitrate_out_kbps = req.a_bitrate_out_kbps
                setattr(job, "disk_hold_reason", req.reason or mc.T("api_waiting_free_space_outputs"))
                mc.record_disk_hold_entered(job_id)
            else:
                obsolete = True
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in job_report_waiting_disk #{job_id}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    return {"success": not obsolete, "obsolete": obsolete}


def _update_job_phase(job_id: int, new_status: str, reset_transfer: bool = False, premux_trying: bool = False, w_name: str = ""):
    try:
        with db_scope(commit=True, immediate=True) as db:
            job = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id == job_id)
                .first()
            )
            if job and w_name and job.worker_name and job.worker_name != w_name:
                return {"success": False, "status": "REVOKED"}      # fázi mění jen worker, který úlohu má
            if job and job.status not in ["CANCELLED", "COMPLETED", "ERROR"]:
                mc.job_heartbeats[job_id] = time.time()
                job.status = new_status
                if new_status != "IN_PROGRESS":
                    setattr(job, "is_paused", False)
                if reset_transfer:
                    job.transferred_bytes = 0
                    job.speed_mbps = 0.0
                if premux_trying:
                    setattr(job, "premux_status", "TRYING")
                else:
                    setattr(job, "disk_hold_reason", "")
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in _update_job_phase #{job_id} -> {new_status}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    return {"success": True}


@fastapi_app.post("/api/jobs/{job_id}/staging")
def job_staging(job_id: int, request: Request):
    return _update_job_phase(job_id, "STAGING", w_name=_hdr_worker_name(request))


@fastapi_app.post("/api/jobs/{job_id}/premux")
def job_premux(job_id: int, request: Request):
    return _update_job_phase(job_id, "PREMUX", premux_trying=True, w_name=_hdr_worker_name(request))


@fastapi_app.post("/api/jobs/{job_id}/encoding")
def job_encoding(job_id: int, request: Request):
    return _update_job_phase(job_id, "IN_PROGRESS", reset_transfer=True, w_name=_hdr_worker_name(request))


@fastapi_app.post("/api/jobs/{job_id}/copying")
def job_copying(job_id: int, request: Request):
    return _update_job_phase(job_id, "COPYING", w_name=_hdr_worker_name(request))


@fastapi_app.post("/api/jobs/{job_id}/complete")
def complete_job(request: Request, job_id: int, data: CompleteData = None):
    c_name = _hdr_worker_name(request) or re.sub(r"\s*\(v[\d\.]+\)$", "", str(getattr(data, "worker_name", "") or "")).strip()
    try:
        with db_scope(commit=True, immediate=True) as db:
            job = db.query(models.Job).filter(models.Job.id == job_id).first()
            if not job:
                mc.release_output_upload_slot(job_id)
                return {"success": True}
            if job.status == "CANCELLED" and not mc.is_system_build_job(job):
                mc.release_output_upload_slot(job_id)
                # zrušená úloha zůstane zrušená; 200, aby worker dokončení neopakoval donekonečna
                out_p = (data.final_dest_path if data and data.final_dest_path else "") or (job.dest_path or "")
                mc.append_manager_log(mc.T("api_complete_rejected_cancelled", job_id, c_name or "?", out_p))
                return {"success": False, "rejected": "cancelled"}
            if job.status == "COMPLETED" and not mc.is_system_build_job(job):
                # Duplicitní / opožděné complete_job nesmí přepsat metadata ani cestu vítězného workeru!
                mc.release_output_upload_slot(job_id)
                return {"success": True, "already_completed": True}
            if c_name and job.worker_name and job.worker_name != c_name and not mc.is_system_build_job(job):
                _hand_over_job(job, c_name, mc.T("api_takeover_r_finished"))   # dokončený převod vyhrává

            mc.release_output_upload_slot(job_id)

            if mc.is_system_build_job(job):
                pl = job.hb_json_payload if isinstance(job.hb_json_payload, dict) else {}
                if pl.get("force_rebuild"):
                    mc.AppState.binary_generation += 1
                    mc.save_manager_config()
                with mc.build_lock:
                    mc.binary_build_state["status"] = "COMPLETED"
                    mc.binary_build_state["progress"] = 100.0
                    mc.binary_build_state["step_message"] = mc.T("api_all_binaries_successfully_compil")
                    mc.binary_build_state["updating_tools"] = []
                    mc.binary_build_state["finished_at"] = int(time.time())
                    mc.binary_build_state["job_id"] = None
                    for it in (mc.binary_build_state.get("checklist") or []):
                        it["status"] = "done"
                mc.append_build_console_line(
                    mc.T("api_done_all_tools_compiled")
                )
                mc.append_manager_log(
                    mc.T("api_system_binary_build_completed", job_id)
                )
                mc.job_heartbeats.pop(job_id, None)
                mc.job_assigned_time.pop(job_id, None)
                db.delete(job)
                purge_stale_system_build_jobs(db, keep_job_id=job_id)
                return {"success": True}

            job.status = "COMPLETED"
            setattr(job, "is_paused", False)
            job.progress = 100.0
            job.completed_at = datetime.datetime.utcnow()
            job.error_code = None
            job.error_detail = None
            setattr(job, "disk_hold_reason", "")
            if data:
                cand_w = re.sub(r"\s*\(v[\d\.]+\)$", "", str(getattr(data, "worker_name", "") or "")).strip()
                cur_w = str(job.worker_name or "").strip()
                # Nikdy nepřepisovat existujícího workera prázdnou hodnotou nebo 'Unknown'
                if cand_w and cand_w.lower() != "unknown" and (not cur_w or cur_w.lower() == "unknown"):
                    job.worker_name = cand_w
                if data.version:
                    job.worker_version = data.version
                job.src_size_bytes = data.src_size_bytes
                job.out_size_bytes = data.out_size_bytes
                job.duration_sec = data.duration_sec
                job.subs_removed = data.subs_removed
                job.subs_status = data.subs_status
                if data.v_bitrate_in_kbps is not None and data.v_bitrate_in_kbps > 0:
                    job.v_bitrate_in_kbps = 0 if data.v_bitrate_in_kbps == 2500 else data.v_bitrate_in_kbps
                if data.a_bitrate_in_kbps:
                    job.a_bitrate_in_kbps = data.a_bitrate_in_kbps
                if data.v_bitrate_out_kbps:
                    job.v_bitrate_out_kbps = data.v_bitrate_out_kbps
                if data.a_bitrate_out_kbps:
                    job.a_bitrate_out_kbps = data.a_bitrate_out_kbps
                if data.mkv_stats_written is not None:
                    job.mkv_stats_written = data.mkv_stats_written
                if data.lpcm_tracks_muxed is not None:
                    setattr(job, "lpcm_Tracks_muxed", data.lpcm_tracks_muxed)
                if data.premux_applied is not None:
                    job.premux_applied = data.premux_applied
                cur_pm = getattr(job, "premux_status", "") or ""
                if data.premux_status:
                    setattr(job, "premux_status", data.premux_status)
                elif data.premux_applied:
                    setattr(job, "premux_status", "OK")
                elif cur_pm == "TRYING":
                    setattr(job, "premux_status", "FALLBACK")
                elif mc.is_job_disc_or_iso(job):
                    setattr(job, "premux_status", "FALLBACK" if mc.AppState.smart_premux else "OFF")
                if data.final_dest_path:
                    job.dest_path = data.final_dest_path
            mc.populate_job_static_metadata(job, allow_disk_probe=False)
            mc.record_job_to_encoding_stats(db, job)
            mc.note_worker_job_success(job.worker_name or c_name)
            mc.append_manager_log(
                mc.T("api_job_completed", job_id, job.original_filename, job.worker_name)
            )
            mc.invalidate_analytics_cache()
            mc.job_heartbeats.pop(job_id, None)
            mc.job_assigned_time.pop(job_id, None)
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in complete_job #{job_id}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    return {"success": True}


@fastapi_app.post("/api/jobs/{job_id}/cancel")
def worker_cancel_job(job_id: int, data: JobCancelRequest = None):
    """
    Worker odmítl úlohu hned po přidělení (např. nedostupné úložiště bez HTTP relay).
    Úloha se bez penalizace (attempt_count se nemění) vrátí na začátek fronty.
    """
    w_req = mc.sanitize_display_name((data.worker_name if data else "") or "")
    reason = str((data.reason if data else "") or "")[:200]
    with db_scope(commit=True, immediate=True) as db:
        job = (
            db.query(models.Job)
            .options(defer(models.Job.hb_json_payload))
            .filter(models.Job.id == job_id)
            .first()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if mc.is_system_build_job(job) or job.status not in mc.ACTIVE_JOB_STATUSES:
            return {"ok": False, "detail": f"Job is {job.status}, nothing to cancel."}
        if w_req and job.worker_name and job.worker_name != w_req:
            return {"ok": False, "detail": "Job is assigned to another worker."}
        w_name = job.worker_name or w_req or "?"
        mc.reset_job_for_requeue(job, priority=mc.next_top_priority(db), clear_errors=False)
        mc.append_manager_log(mc.T("api_job_rejected_by_worker", job.id, job.original_filename, w_name, reason or "-"))
    return {"ok": True}


@fastapi_app.post("/api/jobs/{job_id}/error")
def worker_reported_error(request: Request, job_id: int, data: ErrorData = None):
    caller_w = _hdr_worker_name(request) or re.sub(r"\s*\(v[\d\.]+\)$", "", str(getattr(data, "worker_name", "") or "")).strip()
    try:
        with db_scope(commit=True, immediate=True) as db:
            job = db.query(models.Job).filter(models.Job.id == job_id).first()
            if not job:
                mc.release_output_upload_slot(job_id)
                return {"success": True}

            if job.status in ("COMPLETED", "CANCELLED") and not mc.is_system_build_job(job):
                # Opožděná chyba nesmí nikdy vrátit už hotový nebo zrušený film zpět do fronty!
                return {"success": False, "ignored": job.status.lower()}

            if caller_w and job.worker_name and job.worker_name != caller_w and not mc.is_system_build_job(job):
                # Opožděná chyba od starého workeru nesmí poškodit práci nového vlastníka ani mu přičíst FILE chybu!
                mc.logger.info(
                    f"Ignoring stale error report for job #{job_id} from '{caller_w}' (owned by '{job.worker_name}', status={job.status})."
                )
                return {"success": False, "ignored": "not_owner"}

            mc.release_output_upload_slot(job_id)
            setattr(job, "is_paused", False)
            if mc.is_system_build_job(job):
                err_d = (data.error_detail if data and data.error_detail else mc.T("api_error_compiling_downloading_bina"))
                job.status = "ERROR"
                job.error_code = "ERR_HB_FAILED"
                job.error_detail = err_d
                job.encoder_summary = f"Error: {err_d}"
                with mc.build_lock:
                    mc.binary_build_state["status"] = "ERROR"
                    mc.binary_build_state["step_message"] = err_d
                    mc.binary_build_state["updating_tools"] = []
                    mc.binary_build_state["finished_at"] = int(time.time())
                mc.append_build_console_line(f"ERROR: {err_d}")
                mc.append_manager_log(mc.T("api_error_system_job", job_id, err_d))
                mc.job_heartbeats.pop(job.id, None)
                mc.job_assigned_time.pop(job.id, None)
                return {"success": True}

            e_type = data.error_type if data else "GENERIC"
            if data and data.version:
                job.worker_version = data.version
            w_name = job.worker_name or caller_w

            if e_type in ("FILE", "NETWORK"):          # NETWORK dnes žádný worker neposílá – stejné chování jako FILE
                orig_s = job.source_path or mc.extract_orig_source_from_payload(job.hb_json_payload)
                _, _, found_mgr = mc.resolve_input_path_smart(orig_s, mc.get_manager_inputs_dir())
                if found_mgr:
                    # zdroj na Manageru je → chyba je na straně workeru: úloha zpět, worker pauza / po 3× vypnout příjem
                    mc.note_worker_file_error(db, w_name)
                    mc.reset_job_for_requeue(job, priority=mc.next_top_priority(db), clear_errors=False)
                    job.error_code = "ERR_NAS_UNREACHABLE"
                    job.error_detail = w_name or ""
                else:
                    job.attempt_count += 1
                    job.status = "ERROR"
                    job.error_code = "ERR_SOURCE_MISSING"
                    mc.job_heartbeats.pop(job.id, None)
                    mc.job_assigned_time.pop(job.id, None)
            elif data and data.error_code == "ERR_HELD_OUTPUT_LOST":
                mc.mark_held_disk_job_lost(db, job, w_name or "")
                return {"success": True}
            else:
                job.attempt_count += 1
                err_code = data.error_code if (data and data.error_code) else "ERR_HB_FAILED"
                raw_det = data.error_detail if (data and data.error_detail) else ""
                err_det = f"[{w_name}] {raw_det}".strip() if w_name else raw_det
                if job.attempt_count >= 3:
                    job.status = "ERROR"
                    job.error_code = err_code
                    job.error_detail = err_det
                    mc.job_heartbeats.pop(job.id, None)
                    mc.job_assigned_time.pop(job.id, None)
                else:
                    mc.reset_job_for_requeue(job, priority=mc.next_top_priority(db), clear_errors=False)
                    job.error_code = err_code
                    job.error_detail = err_det

            mc.append_manager_log(
                mc.T("api_job_error", job_id, job.original_filename, w_name, job.error_code, job.error_detail or '')
            )
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in worker_reported_error #{job_id}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    return {"success": True}


@fastapi_app.get("/api/jobs/{job_id}/status")
def get_job_status(job_id: int, worker_name: str = "", held_check: bool = False):
    """Čistě čtecí (read-only) kontrola stavu úlohy – nikdy nemění stav v databázi."""
    now_t = time.time()
    if worker_name:
        mc.active_workers[worker_name] = now_t
    try:
        with db_scope() as db:
            job = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id == job_id)
                .first()
            )
            if not job:
                return {"status": "CANCELLED", "worker_name": None, "is_paused": False, "ui_lang": mc.AppState.ui_lang}
            if worker_name and job.worker_name == worker_name and job.status in mc.ACTIVE_JOB_STATUSES:
                mc.job_heartbeats[job_id] = now_t
            if worker_name and job.worker_name and job.worker_name != worker_name and \
                    (job.status in mc.ACTIVE_JOB_STATUSES or job.status == "COMPLETED"):
                return {"status": "REVOKED", "worker_name": job.worker_name, "is_paused": False, "ui_lang": mc.AppState.ui_lang,
                        "reason": _get_revoke_note(job_id, worker_name)}
            return {
                "status": job.status,
                "worker_name": job.worker_name,
                "is_paused": bool(getattr(job, "is_paused", False)),
                "force_upload": (job_id in mc.manual_upload_release_ids),
                "ui_lang": mc.AppState.ui_lang
            }
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in get_job_status #{job_id}: {e}")
        raise HTTPException(status_code=503, detail="database_busy")


# ==============================================================================
# V2 API ENDPOINTY PRO WEBOVÉ ROZHRANÍ (SPA) & GHCR KONTEJNEROVÝ UPDATER
# ==============================================================================
@fastapi_app.post("/api/v2/system/update_check")
@fastapi_app.post("/api/v2/system/github_check")
def api_v2_check_ghcr_update(req: V2UpdateCheckRequest = None):
    force = True if req is None else bool(req.force)
    return {"ok": True, "manager_version": mc.MANAGER_VERSION, "github_updater": mc.check_ghcr_manager_update(force=force)}


@fastapi_app.post("/api/v2/system/update_apply")
@fastapi_app.post("/api/v2/system/github_update")
def api_v2_apply_ghcr_update():
    res = mc.perform_cluster_self_update(reason="manual")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("detail") or "GHCR update failed")
    return res


@fastapi_app.get("/api/v2/system/update_status")
@fastapi_app.get("/api/selfupdate/status")
def api_v2_update_status():
    return {"ok": True, "manager_version": mc.MANAGER_VERSION, "github_updater": mc.get_github_update_state_snapshot()}


system_build_enqueue_lock = threading.RLock()


def enqueue_system_build_job_internal(db_session, targets_clean: List[str], force_rebuild: bool, force_all: bool = False):
    # Zámek + kontrola existující úlohy: souběžné požadavky (start_build, auto-update
    # z /api/jobs/next, update_settings, check_upstream) nikdy nevytvoří duplicitní SYSTEM_BUILD.
    with system_build_enqueue_lock:
        return _enqueue_system_build_job_locked(db_session, targets_clean, force_rebuild, force_all=force_all)


def _enqueue_system_build_job_locked(db_session, targets_clean: List[str], force_rebuild: bool, force_all: bool = False):
    running_sys = db_session.query(models.Job).filter(
        models.Job.preset_name == "SYSTEM_BUILD",
        models.Job.status.in_(mc.ACTIVE_JOB_STATUSES)
    ).first()
    if running_sys:
        raise HTTPException(status_code=409, detail=mc.T("api_system_build_already_running", running_sys.id))

    sel_w, fb_used, scored = mc.select_best_linux_builder_worker(db_session)
    if not sel_w:
        res_mgr = mc.start_manager_windows_binaries_download(
            targets_clean, force_rebuild=bool(force_rebuild or force_all)
        )
        if res_mgr.get("already_running"):
            raise HTTPException(status_code=409, detail=mc.T("api_system_build_already_running", "HB Manager"))
        if res_mgr.get("already_up_to_date"):
            return {"ok": False, "skipped": True, "detail": mc.T("set_build_uptodate")}
        return res_mgr

    up_vers = mc.get_cached_upstream_versions()
    all_tool_keys = ("7z", "mkvtoolnix", "ffmpeg", "hbcli", "launcher")

    is_force_all = bool(force_all)
    if not is_force_all:
        needed = mc.get_outdated_or_missing_build_targets(mc.get_all_binaries_manifest(), auto=False)
        if "all" in targets_clean:
            targets_clean = list(needed)
        else:
            targets_clean = [t for t in targets_clean if t in all_tool_keys]
        if not targets_clean:
            return {"ok": False, "skipped": True, "detail": mc.T("set_build_uptodate")}
    elif not targets_clean or "all" in targets_clean:
        targets_clean = ["all"]

    uptodate_tools = [] if (is_force_all or "all" in targets_clean) else [t for t in all_tool_keys if t not in set(targets_clean)]

    upd_tools = mc.compute_updating_tools_list(targets_clean)
    checklist_init = mc.create_initial_build_checklist(targets_clean)

    for oj in db_session.query(models.Job).filter(models.Job.preset_name == "SYSTEM_BUILD").all():
        if oj.status not in mc.ACTIVE_JOB_STATUSES:
            db_session.delete(oj)
    db_session.commit()

    def _safe_ver(v, fallback):
        s = str(v or "").strip().lstrip("v")
        return s if mc.is_safe_version_string(s) else fallback

    task_pl = {
        "SystemTask": "BUILD_BINARIES",
        "task_id": int(time.time()),
        "targets": targets_clean,
        "target_tools": targets_clean,
        "uptodate_tools": uptodate_tools,
        "force_rebuild": bool(force_rebuild or is_force_all),
        "force_all": is_force_all,
        "binary_generation": mc.AppState.binary_generation + (1 if (force_rebuild or is_force_all) else 0),
        "hb_version": _safe_ver(up_vers.get("hb_latest"), "1.11.2"),
        "mkvtoolnix_version": _safe_ver(up_vers.get("mkvtoolnix_latest"), "103.0"),
        "ffmpeg_version": _safe_ver(up_vers.get("ffmpeg_latest"), ""),
        "sevenzip_version": _safe_ver(up_vers.get("sevenzip_latest"), "26.06"),
    }

    t_lbl = mc.T("api_all_tools_complete_reinstall") if (is_force_all or (force_rebuild and "all" in targets_clean)) else (
        mc.T("api_all_tools_hbcli_mkvtoolnix") if "all" in targets_clean else ", ".join(targets_clean).upper()
    )
    init_msg = mc.T("api_waiting_worker", sel_w, t_lbl)

    sys_j = models.Job(
        job_hash=f"sys_build_{int(time.time())}",
        original_filename=mc.T("api_system_task_build_update", t_lbl),
        source_path="Official GitHub & MKVToolNix repositories (Full compile + staticx)",
        dest_path=init_msg,
        hb_json_payload=task_pl,
        status="WAITING",
        priority=0,
        res_in="SYSTÉM",
        res_out="CLUSTER",
        preset_name="SYSTEM_BUILD",
        encoder_summary=init_msg,
        video_encoder="staticx",
        is_disc_source=False,
        burned_subs=0,
        dup_key="",
    )
    db_session.add(sys_j)
    db_session.commit()
    db_session.refresh(sys_j)

    with mc.build_lock:
        mc.binary_build_state["status"] = "QUEUED"
        mc.binary_build_state["job_id"] = sys_j.id
        mc.binary_build_state["assigned_worker"] = sel_w
        mc.binary_build_state["assigned_key_sha"] = ""
        mc.binary_build_state["preferred_worker"] = mc.AppState.preferred_builder_worker
        mc.binary_build_state["fallback_used"] = fb_used
        mc.binary_build_state["progress"] = 1.0
        mc.binary_build_state["step_message"] = init_msg
        mc.binary_build_state["updating_tools"] = upd_tools
        mc.binary_build_state["checklist"] = checklist_init
        mc.binary_build_state["console_lines"].clear()
        mc.binary_build_state["started_at"] = int(time.time())
        mc.binary_build_state["finished_at"] = 0

    mc.append_build_console_line(mc.T("api_queued_system_build_builder", sys_j.id, t_lbl, sel_w))
    mc.append_manager_log(mc.T("api_queued_system_build_worker", sys_j.id, t_lbl, sel_w))
    return {
        "ok": True,
        "job_id": sys_j.id,
        "assigned_worker": sel_w,
        "fallback_used": fb_used,
        "candidates": scored,
        "task": task_pl
    }


@fastapi_app.get("/api/binaries/status")
def api_binaries_status():
    mf = mc.get_all_binaries_manifest()
    started = auto_build_tick("binaries_status", False)
    with mc.build_lock:
        bstate = copy.deepcopy(mc.binary_build_state)
    bstate["ffmpeg_recent"] = mc.is_ffmpeg_recent_enough_to_skip_auto(mf)
    return {
        "ok": True,
        "binary_generation": mc.AppState.binary_generation,
        "binaries": mf,
        "binary_builder": bstate,
        "auto_build_enqueued": started or [],
    }


@fastapi_app.post("/api/binaries/check")
@fastapi_app.post("/api/v2/binaries/check_upstream")
def api_v2_check_upstream_versions():
    up_vers = mc.check_upstream_tool_versions()
    started = auto_build_tick("check", True)
    return {"ok": True, "upstream_versions": up_vers, "auto_build_enqueued": started or []}


@fastapi_app.post("/api/v2/binaries/start_build")
def api_v2_start_binary_build(req: V2StartBuildRequest):
    with db_scope() as db:
        if req.preferred_worker is not None:
            mc.AppState.preferred_builder_worker = req.preferred_worker
            mc.save_manager_config()
        raw_tools = req.target_tools if req.target_tools is not None else req.targets
        req_tools = [t for t in (raw_tools or []) if t] or ["all"]
        is_force_all = bool(req.force_all) if req.force_all is not None else bool(req.force_rebuild and "all" in req_tools)
        return enqueue_system_build_job_internal(
            db, req_tools, bool(req.force_rebuild or is_force_all), force_all=is_force_all
        )


def _verify_active_builder_caller(request: Request, req_job_id: Optional[int] = None) -> int:
    """Ověří, že POST /api/v2/binaries/build_progress posílá skutečný aktivní builder (SYSTEM_BUILD)."""
    w_key = request.headers.get("x-hb-worker-key") or ""
    w_name = getattr(request.state, "worker_bound_name", "") or urllib.parse.unquote(request.headers.get("x-hb-worker-name") or "")[:80]
    with db_scope() as db:
        sys_j = db.query(models.Job).filter(
            models.Job.preset_name == "SYSTEM_BUILD",
            models.Job.status.in_(mc.ACTIVE_JOB_STATUSES)
        ).first()
        if not sys_j:
            raise HTTPException(status_code=403, detail="no_active_build")
        if w_name and (sys_j.worker_name or "") != w_name:
            raise HTTPException(status_code=403, detail="builder_mismatch")
        if req_job_id and int(req_job_id) != int(sys_j.id):
            raise HTTPException(status_code=403, detail="build_job_mismatch")
        with mc.build_lock:
            exp_sha = (mc.binary_build_state.get("assigned_key_sha") or "") if mc.binary_build_state.get("job_id") == sys_j.id else ""
        if exp_sha and w_key and not hmac.compare_digest(exp_sha, hashlib.sha256(w_key.encode("utf-8")).hexdigest()):
            raise HTTPException(status_code=403, detail="builder_key_mismatch")
        return int(sys_j.id)


@fastapi_app.post("/api/v2/binaries/build_progress")
def api_v2_report_build_progress(req: V2BuildProgressReport, request: Request):
    act_jid = _verify_active_builder_caller(request, req.job_id)
    with mc.build_lock:
        mc.binary_build_state["status"] = req.status
        mc.binary_build_state["progress"] = round(float(req.progress or 0.0), 1)
        if req.step_message:
            mc.binary_build_state["step_message"] = req.step_message
        if req.updating_tools is not None:
            mc.binary_build_state["updating_tools"] = list(req.updating_tools)
        if req.status in ("COMPLETED", "ERROR"):
            mc.binary_build_state["finished_at"] = int(time.time())
            mc.binary_build_state["updating_tools"] = []

    if req.step_message:
        mc.update_checklist_from_step_message(req.step_message)
        mc.append_build_console_line(req.step_message)
    if req.console_line:
        mc.append_build_console_line(req.console_line)

    if act_jid:
        try:
            with db_scope(commit=True) as db:
                j = db.query(models.Job).filter(models.Job.id == act_jid).first()
                if j and mc.is_system_build_job(j):
                    j.progress = round(float(req.progress or 0.0), 1)
                    if req.step_message:
                        j.encoder_summary = req.step_message
                        j.dest_path = req.step_message
                    mc.job_heartbeats[j.id] = time.time()
        except Exception:
            pass
    return {"ok": True}


def _builder_upload_refusal(w_key: str, w_name: str, os_k: str, tool_k: str) -> str:
    """Nahrání nástroje od WORKERU smí jen builder, který právě sestavuje (SYSTEM_BUILD),
    a jen nástroje z jeho zadání. Vrací důvod odmítnutí, nebo "" = v pořádku.
    Ruční nahrání z webu (bez klíče workeru) sem nepatří – to hlídá přihlášení do webu."""
    with db_scope() as db:
        sys_j = db.query(models.Job).filter(
            models.Job.preset_name == "SYSTEM_BUILD",
            models.Job.status.in_(mc.ACTIVE_JOB_STATUSES)
        ).first()
        if not sys_j:
            return "žádné sestavení neběží"
        if (sys_j.worker_name or "") != (w_name or ""):
            return f"sestavení běží na workeru {sys_j.worker_name or '?'}"
        with mc.build_lock:
            exp_sha = (mc.binary_build_state.get("assigned_key_sha") or "") if mc.binary_build_state.get("job_id") == sys_j.id else ""
        # Po restartu Manageru uprostřed sestavení se otisk ztratí → stačí jméno (klíč k jménu ověřuje schvalování workerů).
        if exp_sha and not hmac.compare_digest(exp_sha, hashlib.sha256(w_key.encode("utf-8")).hexdigest()):
            return "klíč workeru neodpovídá builderu"
        if (os_k, tool_k) not in mc.allowed_build_uploads(sys_j.hb_json_payload):
            return "nástroj není v zadání sestavení"
    return ""


MAX_BINARY_UPLOAD_BYTES = 256 * 1024 * 1024


@fastapi_app.post("/api/v2/binaries/upload/{os_type}/{tool}")
def api_v2_upload_binary(
    request: Request,
    os_type: str,
    tool: str,
    version: Optional[str] = Form(None),
    is_custom: Optional[str] = Form("false"),
    file: UploadFile = File(...)
):
    os_k = os_type.lower().strip()
    tool_k = tool.lower().strip()
    dst_p = mc.get_canonical_binary_path(os_k, tool_k)
    if not dst_p:
        raise HTTPException(status_code=400, detail=f"Invalid combination {os_k}/{tool_k}")

    w_key = request.headers.get("x-hb-worker-key") or ""
    if w_key:
        w_name = getattr(request.state, "worker_bound_name", "") or urllib.parse.unquote(request.headers.get("x-hb-worker-name") or "")[:80]
        why = _builder_upload_refusal(w_key, w_name, os_k, tool_k)
        if why:
            mc.append_manager_log(f"[SECURITY] Odmítnuto nahrání nástroje {os_k}/{tool_k} od workeru {w_name or '?'} ({SEC.client_ip(request)}): {why}.")
            raise HTTPException(status_code=403, detail="upload_not_allowed")

    mc.ensure_binary_dirs()
    tmp_p = f"{dst_p}.{os.getpid()}.tmp"
    total_written = 0
    try:
        os.makedirs(os.path.dirname(dst_p), exist_ok=True)
        with open(tmp_p, "wb") as f:
            while True:
                blk = file.file.read(1024 * 1024)
                if not blk:
                    break
                total_written += len(blk)
                if total_written > MAX_BINARY_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Binary file too large (> 256 MB).")
                f.write(blk)
        if total_written < 10000:
            raise HTTPException(status_code=400, detail="File too small (< 10 KB).")
        os.chmod(tmp_p, 0o755)
        if os_k == "windows" and tool_k == "launcher":
            # HBWorker.exe z Linux builderu se podepisuje až tady – klíč nikdy neopouští Manager.
            if not mc.sign_windows_exe_on_manager(tmp_p, tmp_p):
                mc.append_build_console_line("[SIGN] HBWorker.exe could not be signed on Manager - storing unsigned build.")
        os.replace(tmp_p, dst_p)
        os.chmod(dst_p, 0o755)
        mc.binary_hash_cache.pop("key_" + dst_p, None)
        mc.binary_hash_cache.pop("md5_" + dst_p, None)
    except HTTPException:
        try:
            if os.path.exists(tmp_p):
                os.remove(tmp_p)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            if os.path.exists(tmp_p):
                os.remove(tmp_p)
        except Exception:
            pass
        mc.logger.error(f"Error saving binary {dst_p}: {e}")
        raise HTTPException(status_code=500, detail="Cannot save binary to disk.")

    md5_v, sz_v = mc.get_file_md5_cached(dst_p)
    s_key = f"{os_k}_{tool_k}"
    c_flag = str(is_custom or "").lower() in ("true", "1", "yes")

    clean_v = (version or "").lstrip("v").strip()
    if tool_k in ("ffmpeg", "ffprobe") and clean_v:
        clean_v = mc.normalize_ffmpeg_stable_branch(clean_v)
    if clean_v:
        mc.binary_versions_store[s_key] = {"version": clean_v, "md5": md5_v, "is_custom": c_flag}
    elif w_key:
        # Nikdy nespouštět neověřenou binárku nahranou workerem jako root na Manageru
        clean_v = "custom"
        mc.binary_versions_store[s_key] = {"version": clean_v, "md5": md5_v, "is_custom": c_flag}
    else:
        det_v, _ = mc.probe_binary_version_string(os_k, tool_k, dst_p, md5_v)
        clean_v = det_v or "custom"
        mc.binary_versions_store[s_key] = {"version": clean_v, "md5": md5_v, "is_custom": c_flag}
    mc.save_binary_versions_store()

    with mc.build_lock:
        if s_key in mc.binary_build_state["updating_tools"]:
            mc.binary_build_state["updating_tools"].remove(s_key)
        if tool_k in ("ffmpeg", "ffprobe") and clean_v and not mc.is_ffmpeg_nightly_version(clean_v):
            mc.binary_build_state["upstream_versions"]["ffmpeg_latest"] = clean_v

    mc.mark_checklist_binary_uploaded(os_k, tool_k, clean_v, sz_v)
    mc.append_build_console_line(
        mc.T("api_stored_binary_v_mb", os_k, tool_k, clean_v, f'{sz_v / (1024 * 1024):.1f}', md5_v[:8])
    )
    return {"ok": True, "os_type": os_k, "tool": tool_k, "path": dst_p, "md5": md5_v, "size": sz_v, "version": clean_v, "is_custom": c_flag}


@fastapi_app.post("/api/v2/ui_prefs")
def api_v2_save_ui_prefs(req: UiPrefsRequest):
    """Uloží nastavení webového rozhraní na Manager (sdílená pro všechny prohlížeče)."""
    clean = mc.sanitize_ui_prefs(req.prefs or {})
    if clean:
        cur = dict(getattr(mc.AppState, "ui_prefs", {}) or {})
        cur.update(clean)
        mc.AppState.ui_prefs = cur
        mc.save_manager_config()
    return {"ok": True, "ui_prefs": mc.get_effective_ui_prefs()}


@fastapi_app.get("/api/v2/state")
def api_v2_get_state(request: Request):
    auto_build_tick_async("state")
    now_t = time.time()
    with db_scope() as db:
        raw_q = (
            db.query(models.Job)
            .options(defer(models.Job.hb_json_payload))
            .filter(models.Job.status.in_(["WAITING", "PAUSED", "ERROR", "CANCELLED"] + mc.ACTIVE_JOB_STATUSES))
            .all()
        )
        sorted_q = mc.sort_jobs_logic(raw_q)
        q_ser = [mc.serialize_job(j) for j in sorted_q]
        # Vizuální pořadí #1, #2, #3... se počítá v RAM bez přepisování celé fronty v SQLite
        disp_pos = 1
        for j_obj, d_obj in zip(sorted_q, q_ser):
            if not d_obj.get("is_system_job") and j_obj.status in ("WAITING", "PAUSED"):
                d_obj["priority"] = disp_pos
                disp_pos += 1
        mc.annotate_queue_duplicates(db, sorted_q, q_ser)

        act_by_w, held_by_w, used_slots_by_w = {}, {}, {}
        for j in raw_q:
            if j.status in mc.COMPUTE_ACTIVE_STATUSES and j.worker_name:
                act_by_w[j.worker_name] = act_by_w.get(j.worker_name, 0) + 1
                used_slots_by_w[j.worker_name] = used_slots_by_w.get(j.worker_name, 0) + mc.get_job_weight(j)
            elif j.status == "WAITING_DISK" and j.worker_name:
                held_by_w[j.worker_name] = held_by_w.get(j.worker_name, 0) + 1

        pol_map = {p.worker_name: p for p in db.query(models.WorkerPolicy).all()}
        all_names = sorted(set(list(pol_map.keys()) + list(mc.active_workers.keys())))

        w_list = []
        for w_name in all_names:
            l_seen = mc.active_workers.get(w_name, 0)
            on_flag = (now_t - l_seen) <= 45 if l_seen else False
            pol = pol_map.get(w_name)
            t_data = mc.worker_telemetry.get(w_name, {})
            hs_data = mc.worker_handshakes.get(w_name, {})
            u_loc = bool(pol.use_local_temp) if pol else mc.AppState.default_use_local_temp
            pm_ok = mc.is_worker_premux_capable(w_name, u_loc)
            cap_v = (pol.capability if pol else "all") or "all"
            a4k_v = mc.worker_allows_4k(pol) if pol else True
            def_ms = mc.default_slots_for_worker(pol) if pol else 4
            w_list.append({
                "worker_name": w_name,
                "is_online": on_flag,
                "last_seen_ts": int(l_seen) if l_seen else 0,
                "is_enabled": pol.is_enabled if pol else True,
                "cooldown_left_s": mc.worker_cooldown_left(w_name),
                "cooldown_reason": (mc.worker_cooldown.get(w_name) or {}).get("reason", ""),
                "intake_off_reason": "" if (not pol or pol.is_enabled) else mc.worker_intake_off_reason.get(w_name, ""),
                "active_jobs_count": act_by_w.get(w_name, 0),
                "used_slots": used_slots_by_w.get(w_name, 0),
                "held_disk_jobs_count": held_by_w.get(w_name, 0),
                "max_slots": int(pol.max_slots if pol else def_ms),
                "capability": cap_v,
                "allow_4k": a4k_v,
                "staging_mode": (getattr(pol, "staging_mode", mc.AppState.default_staging_mode) if pol else mc.AppState.default_staging_mode) or mc.AppState.default_staging_mode,
                "power_mode": (getattr(pol, "power_mode", mc.AppState.default_power_mode) if pol else mc.AppState.default_power_mode) or mc.AppState.default_power_mode,
                "custom_cores": mc.parse_custom_cores_list(getattr(pol, "custom_cores", "")) if pol else [],
                "prioritize_4k": bool(pol.prioritize_4k) if pol else mc.AppState.default_prioritize_4k,
                "use_local_temp": u_loc,
                "premux_capable": pm_ok,
                "version": mc.worker_versions.get(w_name, "?"),
                "seen_ip": "" if sec.is_masked_peer(mc.worker_seen_ip.get(w_name, "")) else mc.worker_seen_ip.get(w_name, ""),
                "target_version": mc.WORKER_VERSION,
                "is_windows": not mc.is_linux_worker(w_name),
                "windows_settings": mc.get_worker_windows_settings_view(w_name) if not mc.is_linux_worker(w_name) else None,
                "telemetry": t_data,
                "handshake": hs_data,
                "history": list(mc.worker_telemetry_history.get(w_name, [])),
            })

        mc.enforce_premux_local_disk_guard(db)

        mf = mc.get_all_binaries_manifest()
        miss_b, upd_av = mc.evaluate_binaries_health(mf)
        ff_recent = mc.is_ffmpeg_recent_enough_to_skip_auto(mf)
        mgr_eng = mc.get_manager_engine_status(mf)
        out_telem = mc.get_output_storage_telemetry()

        act_b, b_fb, b_cands = mc.select_best_linux_builder_worker(db)
        with mc.build_lock:
            b_snap = {
                "status": mc.binary_build_state["status"],
                "job_id": mc.binary_build_state["job_id"],
                "assigned_worker": mc.binary_build_state["assigned_worker"] or (act_b or ""),
                "preferred_worker": mc.AppState.preferred_builder_worker,
                "active_auto_worker": act_b or "",
                "fallback_used": b_fb,
                "candidates": b_cands,
                "progress": mc.binary_build_state["progress"],
                "step_message": mc.binary_build_state["step_message"],
                "updating_tools": list(mc.binary_build_state["updating_tools"]),
                "checklist": copy.deepcopy(mc.binary_build_state["checklist"]),
                "console_lines": list(mc.binary_build_state["console_lines"]),
                "started_at": mc.binary_build_state["started_at"],
                "finished_at": mc.binary_build_state["finished_at"],
                "upstream_versions": dict(mc.binary_build_state["upstream_versions"]),
                "missing_binaries": miss_b,
                "updates_available": upd_av,
                "ffmpeg_recent": ff_recent,
                "binary_generation": mc.AppState.binary_generation,
            }

        return {
            "server_time": int(now_t),
            "version": mc.MANAGER_VERSION,
            "manager_version": mc.MANAGER_VERSION,
            "worker_target_version": mc.WORKER_VERSION,
            "maintenance_mode": mc.AppState.maintenance_mode,
            "output_storage": out_telem,
            "github_updater": mc.get_github_update_state_snapshot(),
            "ghcr_images": {"manager": mc.get_manager_image(), "worker": mc.get_worker_image(), "channel": mc.get_update_channel(), "project_url": mc.GITHUB_PROJECT_URL},
            "kofi_url": mc.KOFI_URL,
            "storage_mounts": {
                "inputs_dir": mc.get_manager_inputs_dir(),
                "outputs_dir": mc.get_manager_outputs_dir(),
                "inputs_ok": os.path.exists(mc.get_manager_inputs_dir()),
                "outputs_ok": os.path.exists(mc.get_manager_outputs_dir()) and os.access(mc.get_manager_outputs_dir(), os.W_OK),
                "manager_engine": mgr_eng,
                "output_storage": out_telem,
            },
            "settings": {
                "ui_lang": mc.AppState.ui_lang,
                "onboarding_done": bool(getattr(mc.AppState, "onboarding_done", False)),
                "auto_clean_history": mc.AppState.auto_clean_history,
                "convert_ass_to_srt": mc.AppState.convert_ass_to_srt,
                "ass_srt_mode": mc.AppState.ass_srt_mode,
                "burn_in_subs": mc.AppState.burn_in_subs,
                "dts_hd_mode": mc.AppState.dts_hd_mode,
                "auto_clean_empty_subs": mc.AppState.auto_clean_empty_subs,
                "auto_extract_forced_subs": mc.AppState.auto_extract_forced_subs,
                "write_mkv_stats": mc.AppState.write_mkv_stats,
                "lpcm_passthru": mc.AppState.lpcm_passthru,
                "smart_premux": mc.AppState.smart_premux,
                "ineffective_action": mc.AppState.ineffective_action,
                "ineffective_prefix": mc.AppState.ineffective_prefix,
                "ineffective_subfolder": mc.AppState.ineffective_subfolder,
                "preferred_builder_worker": mc.AppState.preferred_builder_worker,
                "auto_build_on_first_run": mc.AppState.auto_build_on_first_run,
                "auto_update_binaries": getattr(mc.AppState, "auto_update_binaries", True),
                "auto_update_cluster": getattr(mc.AppState, "auto_update_cluster", False),
                "default_staging_mode": mc.AppState.default_staging_mode,
                "default_power_mode": mc.AppState.default_power_mode,
                "default_use_local_temp": mc.AppState.default_use_local_temp,
                "default_prioritize_4k": mc.AppState.default_prioritize_4k,
                "min_free_output_gb": mc.AppState.min_free_output_gb,
                "continue_encoding_on_disk_full": mc.AppState.continue_encoding_on_disk_full,
                "telemetry_source": mc.AppState.telemetry_source,
                "worker_cpu_source": mc.AppState.worker_cpu_source,
                "worker_ram_source": mc.AppState.worker_ram_source,
                "ram_format": mc.AppState.ram_format,
                "support_url": mc.KOFI_URL,
                "win_default_inputs_dir": mc.AppState.win_default_inputs_dir,
                "win_default_outputs_dir": mc.AppState.win_default_outputs_dir,
                "smb_username": mc.AppState.smb_username,
                "smb_password": mc.mask_secret(mc.AppState.smb_password),
                "ui_prefs": mc.get_effective_ui_prefs(),
                "linux_default_inputs_dir": mc.AppState.linux_default_inputs_dir,
                "linux_default_outputs_dir": mc.AppState.linux_default_outputs_dir,
                "linux_default_temp_dir": mc.AppState.linux_default_temp_dir,
            },
            "binaries": mf,
            "binary_builder": b_snap,
            "security": {**SEC.summary(request), "workers": WREG.summary()},
            "workers_bound_elsewhere": bound_elsewhere_list(),
            "workers_name_clash": worker_name_clashes(),
            "queue_4k_unservable": bool(w_list) and not any(
                bool(w.get("is_enabled")) and bool(w.get("allow_4k")) and int(w.get("max_slots") or 0) >= 4 for w in w_list),
            "workers": w_list,
            "queue": q_ser,
            "analytics": mc.get_cached_analytics(db),
        }


@fastapi_app.get("/api/v2/logs")
def api_v2_get_logs(worker_name: str = "ALL"):
    return mc.get_worker_logs_snapshot(worker_name)


@fastapi_app.post("/api/v2/logs/clear")
def api_v2_clear_logs(req: V2ClearLogsRequest):
    mc.clear_logs_buffer(req.target)
    return {"ok": True}


@fastapi_app.get("/api/v2/history")
def api_v2_get_history(
    limit: int = 0,
    offset: int = 0,
    q: str = "",
    worker: str = "",
    preset: str = "",
    sort: str = "completed_at",
    dir: str = "desc",
):
    with db_scope() as db:
        q_base = (
            db.query(models.Job)
            .options(defer(models.Job.hb_json_payload))
            .filter(
                models.Job.status == "COMPLETED",
                or_(models.Job.preset_name.is_(None), models.Job.preset_name != "SYSTEM_BUILD"),
            )
        )
        q_txt = (q or "").strip()
        if q_txt:
            like_pat = f"%{q_txt}%"
            q_base = q_base.filter(
                or_(
                    models.Job.original_filename.ilike(like_pat),
                    models.Job.dest_path.ilike(like_pat),
                    models.Job.worker_name.ilike(like_pat),
                    models.Job.preset_name.ilike(like_pat),
                )
            )
        w_flt = (worker or "").strip()
        if w_flt:
            q_base = q_base.filter(models.Job.worker_name == w_flt)
        p_flt = (preset or "").strip()
        if p_flt:
            q_base = q_base.filter(models.Job.preset_name == p_flt)

        total = q_base.count()

        sort_map = {
            "completed_at": models.Job.completed_at,
            "id": models.Job.id,
            "original_filename": models.Job.original_filename,
            "duration_sec": models.Job.duration_sec,
            "out_size_bytes": models.Job.out_size_bytes,
            "src_size_bytes": models.Job.src_size_bytes,
        }
        sort_col = sort_map.get((sort or "completed_at").strip().lower(), models.Job.completed_at)
        is_asc = str(dir or "desc").strip().lower() == "asc"
        if is_asc:
            q_ordered = q_base.order_by(sort_col.asc(), models.Job.id.asc())
        else:
            q_ordered = q_base.order_by(sort_col.desc(), models.Job.id.desc())

        eff_offset = max(0, int(offset or 0))
        eff_limit = max(0, min(5000, int(limit or 0)))
        if eff_offset > 0:
            q_ordered = q_ordered.offset(eff_offset)
        if eff_limit > 0:
            q_ordered = q_ordered.limit(eff_limit)

        rows = [mc.serialize_job(j) for j in q_ordered.all()]
        return {"history": rows, "total": total, "limit": eff_limit, "offset": eff_offset}


@fastapi_app.get("/api/v2/jobs/{job_id}/inspect")
def api_v2_inspect_job(job_id: int):
    with db_scope() as db:
        j = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not j:
            raise HTTPException(status_code=404, detail="Job not found")
        if mc.is_system_build_job(j):
            with mc.build_lock:
                cl = copy.deepcopy(mc.binary_build_state["checklist"])
                c_lines = list(mc.binary_build_state["console_lines"])
            return {
                "job": mc.serialize_job(j),
                "is_system_build": True,
                "checklist": cl,
                "console_lines": c_lines,
                "video": {},
                "audio_tracks": [],
                "subtitle_tracks": [],
            }
        it, jn = mc.unwrap_job_payload_nodes(j.hb_json_payload)
        vid_d = jn.get("Video") if isinstance(jn.get("Video"), dict) else {}
        t_node = it.get("Title") if isinstance(it.get("Title"), dict) else {}
        s_info = it.get("SourceTitleInfo") if isinstance(it.get("SourceTitleInfo"), dict) else {}
        t_alist = t_node.get("AudioList") or s_info.get("AudioTracks") or []
        t_slist = t_node.get("SubtitleList") or s_info.get("Subtitles") or []

        raw_a = jn.get("AudioTracks")
        is_libhb_a = not isinstance(raw_a, list)
        if is_libhb_a:
            raw_a = (jn.get("Audio") or {}).get("AudioList") if isinstance(jn.get("Audio"), dict) else []

        a_out = []
        for idx, t in enumerate(raw_a or []):
            if isinstance(t, dict):
                sc = t.get("ScannedTrack") or t.get("SourceTrack") or {}
                enc = t.get("Encoder")
                enc_s = enc.get("ShortName") if isinstance(enc, dict) else str(enc or "copy")
                tr = t.get("Track")
                if tr is not None and is_libhb_a and "ScannedTrack" not in t and "SourceTrack" not in t:
                    t_disp = int(tr) + 1
                    if 0 <= int(tr) < len(t_alist) and isinstance(t_alist[int(tr)], dict):
                        sc = t_alist[int(tr)]
                else:
                    t_disp = tr or sc.get("TrackNumber") or (idx + 1)

                a_n = sc.get("Description") or t.get("Name") or t.get("TrackName") or sc.get("Name") or sc.get("Language") or ""
                is_p = ("copy" in enc_s.lower() or "pass" in enc_s.lower())
                a_out.append({
                    "index": idx + 1,
                    "track": t_disp,
                    "name": a_n,
                    "encoder": enc_s,
                    "bitrate": "passthru" if is_p else (t.get("Bitrate") or "auto"),
                })

        raw_s = jn.get("SubtitleTracks")
        is_libhb_s = not isinstance(raw_s, list)
        if is_libhb_s:
            raw_s = (jn.get("Subtitle") or {}).get("SubtitleList") if isinstance(jn.get("Subtitle"), dict) else []

        s_out = []
        for idx, t in enumerate(raw_s or []):
            if isinstance(t, dict):
                sc = t.get("SourceTrack") or t.get("ScannedTrack") or {}
                tr = t.get("Track")
                if tr is not None and is_libhb_s and "SourceTrack" not in t and "ScannedTrack" not in t:
                    t_disp = int(tr) + 1
                    if 0 <= int(tr) < len(t_slist) and isinstance(t_slist[int(tr)], dict):
                        sc = t_slist[int(tr)]
                else:
                    t_disp = tr or sc.get("TrackNumber") or (idx + 1)

                s_n = t.get("Name") or t.get("TrackName") or sc.get("Name") or sc.get("Language") or ""
                s_out.append({
                    "index": idx + 1,
                    "track": t_disp,
                    "name": s_n,
                    "forced": bool(t.get("Forced")),
                    "default": bool(t.get("Default")),
                })

        ct, cb, cl, cr, tw, th, mw, mh = mc.extract_crop_and_dims_from_payload(it, jn)
        v_enc = jn.get("VideoEncoder") or vid_d.get("Encoder")
        v_enc_s = v_enc.get("ShortName") if isinstance(v_enc, dict) else str(v_enc or "")
        v_pre = jn.get("VideoPreset") or vid_d.get("Preset")
        v_pre_s = v_pre.get("ShortName") if isinstance(v_pre, dict) else str(v_pre or "")
        q_rf = jn.get("Quality") if jn.get("Quality") is not None else vid_d.get("Quality")
        ex_opts = jn.get("ExtraAdvancedArguments") or jn.get("AdvancedEncoderOptions") or vid_d.get("Options") or ""

        return {
            "job": mc.serialize_job(j),
            "is_system_build": False,
            "video": {
                "encoder": v_enc_s,
                "preset": v_pre_s,
                "quality_rf": q_rf,
                "extra_args": ex_opts,
                "crop": {"Top": ct, "Bottom": cb, "Left": cl, "Right": cr},
                "width": tw,
                "height": th,
                "max_width": mw,
                "max_height": mh,
            },
            "audio_tracks": a_out,
            "subtitle_tracks": s_out,
        }


@fastapi_app.post("/api/v2/jobs/action")
def api_v2_jobs_action(req: V2JobActionRequest, request: Request):
    if not req.job_ids:
        return {"ok": True, "affected": 0}
    act = req.action
    is_w_auth = bool(getattr(request.state, "is_worker_auth", False))
    w_bound = getattr(request.state, "worker_bound_name", "") or ""
    if is_w_auth and act not in ("freeze", "unfreeze"):
        mc.append_manager_log(f"[SECURITY] Worker „{w_bound or '?'}“ se pokusil o zakázanou akci nad úlohami: {act}.")
        raise HTTPException(status_code=403, detail="worker_action_not_allowed")
    if act == "release_disk":
        with db_scope(commit=True, immediate=True) as db:
            target_jobs = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(models.Job.id.in_(req.job_ids))
                .all()
            )
            released_ids, lost_ids = mc.release_or_fail_held_disk_jobs(db, target_jobs)
            missing_ids = [int(jid) for jid in req.job_ids if jid not in released_ids and jid not in lost_ids]
            if missing_ids:
                with mc.upload_reservation_lock:
                    for jid in missing_ids:
                        mc.manual_upload_release_ids.add(int(jid))
        return {
            "ok": True,
            "affected": len(released_ids) + len(missing_ids),
            "lost_ids": lost_ids,
            "lost_count": len(lost_ids)
        }

    aff = 0
    try:
        with db_scope(commit=True, immediate=True) as db:
            if act in ("freeze", "unfreeze"):
                target_pause = (act == "freeze")
                for jid in req.job_ids:
                    j = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.id == jid)
                        .first()
                    )
                    if j and j.status in mc.COMPUTE_ACTIVE_STATUSES:
                        if is_w_auth and w_bound and (j.worker_name or "") != w_bound:
                            continue
                        setattr(j, "is_paused", target_pause)
                        mc.job_heartbeats[j.id] = time.time()
                        aff += 1
                return {"ok": True, "affected": aff}
            elif act in ("up", "down") and len(req.job_ids) == 1:
                tid = req.job_ids[0]
                all_q = [
                    j for j in (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.status.in_(["WAITING", "PAUSED"]))
                        .order_by(models.Job.priority.asc(), models.Job.created_at.asc(), models.Job.id.asc())
                        .all()
                    )
                    if not mc.is_system_build_job(j)
                ]
                idx = next((i for i, j in enumerate(all_q) if j.id == tid), -1)
                swap_idx = (idx - 1) if (act == "up" and idx > 0) else ((idx + 1) if (act == "down" and 0 <= idx < len(all_q) - 1) else -1)
                if swap_idx != -1:
                    a, b = all_q[idx], all_q[swap_idx]
                    if a.priority == b.priority:
                        # Raritní shoda priorit → srovnat frontu a pak prohodit jen tyto 2 sousedy
                        mc.renumber_waiting_queue(db)
                    a.priority, b.priority = b.priority, a.priority
                    aff = 1
            elif act == "to_top":
                bp = mc.next_top_priority(db) - len(req.job_ids) + 1
                for idx, jid in enumerate(req.job_ids):
                    j = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.id == jid)
                        .first()
                    )
                    if j and not mc.is_system_build_job(j):
                        j.priority = bp + idx
                        aff += 1
            elif act == "to_bottom":
                bp = mc.next_bottom_priority(db)
                for idx, jid in enumerate(req.job_ids):
                    j = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.id == jid)
                        .first()
                    )
                    if j and not mc.is_system_build_job(j):
                        j.priority = bp + idx
                        aff += 1
            elif act == "cancel":
                for jid in req.job_ids:
                    j = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.id == jid)
                        .first()
                    )
                    if j:
                        if mc.is_system_build_job(j):
                            with mc.build_lock:
                                mc.binary_build_state["status"] = "IDLE"
                                mc.binary_build_state["updating_tools"] = []
                                mc.binary_build_state["step_message"] = mc.T("api_cancelled_by_user")
                        j.status = "CANCELLED"
                        setattr(j, "is_paused", False)
                        mc.job_heartbeats.pop(j.id, None)
                        mc.job_assigned_time.pop(j.id, None)
                        mc.release_output_upload_slot(j.id)
                        aff += 1
            elif act in ("requeue", "resume"):
                bp = mc.next_top_priority(db) - len(req.job_ids) + 1
                for idx, jid in enumerate(req.job_ids):
                    j = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.id == jid)
                        .first()
                    )
                    if j:
                        if j.status in mc.COMPUTE_ACTIVE_STATUSES:
                            setattr(j, "is_paused", False)
                            mc.job_heartbeats[j.id] = time.time()
                            aff += 1
                            continue
                        is_sys = mc.is_system_build_job(j)
                        mc.reset_job_for_requeue(
                            j,
                            priority=(0 if is_sys else bp + idx),
                            reset_attempts=True,
                            clear_errors=True,
                        )
                        if is_sys:
                            # Opakování selhané systémové úlohy jedním klikem: okamžitě Pri:MAX pro nejsilnější Linux worker.
                            j.encoder_summary = mc.T("api_waiting_worker", "AUTO", "SYSTEM_BUILD")
                            with mc.build_lock:
                                mc.binary_build_state["status"] = "QUEUED"
                                mc.binary_build_state["job_id"] = j.id
                                mc.binary_build_state["progress"] = 1.0
                                mc.binary_build_state["started_at"] = int(time.time())
                                mc.binary_build_state["finished_at"] = 0
                        aff += 1
            elif act == "pause":
                for jid in req.job_ids:
                    j = (
                        db.query(models.Job)
                        .options(defer(models.Job.hb_json_payload))
                        .filter(models.Job.id == jid)
                        .first()
                    )
                    if j:
                        if j.status in mc.COMPUTE_ACTIVE_STATUSES:
                            setattr(j, "is_paused", True)
                            mc.job_heartbeats[j.id] = time.time()
                            aff += 1
                        elif j.status == "WAITING" and not mc.is_system_build_job(j):
                            j.status = "PAUSED"
                            aff += 1
            elif act == "delete":
                sys_rows = (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.id.in_(req.job_ids), models.Job.preset_name == "SYSTEM_BUILD")
                    .all()
                )
                if sys_rows:
                    with mc.build_lock:
                        mc.binary_build_state["status"] = "IDLE"
                        mc.binary_build_state["updating_tools"] = []
                with mc.media_cache_lock:
                    for jid in req.job_ids:
                        mc.job_info_cache.pop(jid, None)
                for jid in req.job_ids:
                    mc.job_heartbeats.pop(jid, None)
                    mc.job_assigned_time.pop(jid, None)
                    mc.release_output_upload_slot(jid)
                aff = db.query(models.Job).filter(models.Job.id.in_(req.job_ids)).delete(synchronize_session=False)
                mc.invalidate_analytics_cache()
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in api_v2_jobs_action ({act}): {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    return {"ok": True, "affected": aff}


@fastapi_app.post("/api/v2/jobs/reorder")
def api_v2_reorder_jobs(req: V2ReorderRequest):
    if not req.ordered_ids:
        return {"ok": True}
    try:
        with db_scope(commit=True, immediate=True) as db:
            j_map = {
                j.id: j
                for j in (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.id.in_(req.ordered_ids))
                    .all()
                )
            }
            ordered_jobs = [
                j_map[jid]
                for jid in req.ordered_ids
                if jid in j_map and not mc.is_system_build_job(j_map[jid]) and j_map[jid].status not in (mc.ACTIVE_JOB_STATUSES + ["COMPLETED"])
            ]
            if ordered_jobs:
                prios = sorted(int(j.priority) for j in ordered_jobs)
                if len(set(prios)) == len(prios):
                    # Přesouváme jen mezi stávajícími sloty – O(k) zápis bez přepisování celé fronty
                    for j, new_p in zip(ordered_jobs, prios):
                        if j.priority != new_p:
                            j.priority = new_p
                else:
                    mc.renumber_waiting_queue(db)
                    prios2 = sorted(int(j.priority) for j in ordered_jobs)
                    for j, new_p in zip(ordered_jobs, prios2):
                        if j.priority != new_p:
                            j.priority = new_p
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error in api_v2_reorder_jobs: {e}")
        raise HTTPException(status_code=503, detail="database_busy")
    return {"ok": True}


@fastapi_app.get("/api/v2/jobs/duplicates")
def api_v2_scan_duplicates():
    with db_scope() as db:
        q_jobs = [
            j for j in db.query(models.Job).filter(models.Job.status.in_(mc.ACTIVE_JOB_STATUSES + ["WAITING", "PAUSED"])).order_by(models.Job.id.asc()).all()
            if not mc.is_system_build_job(j)
        ]

        meta, by_dest = {}, {}
        for j in q_jobs:
            fp_h, sum_s, rf_v = mc.get_job_encoding_fingerprint(j)
            s_id = mc.get_job_source_identity(j)
            d_norm = mc.normalize_movie_name(j.dest_path)
            meta[j.id] = {"fp": fp_h, "summary": sum_s, "rf": rf_v, "src_id": s_id, "dest_norm": d_norm}
            k = d_norm or f"src_{s_id}"
            by_dest.setdefault(k, []).append(j)

        ex_g, df_g = [], []
        for _, grp in by_dest.items():
            if len(grp) < 2:
                continue
            u_fps = {meta[j.id]["fp"] for j in grp}
            ser = [
                {
                    "id": j.id,
                    "original_filename": j.original_filename,
                    "status": j.status,
                    "summary": meta[j.id]["summary"],
                    "rf": meta[j.id]["rf"],
                    "dest_path": j.dest_path,
                }
                for j in grp
            ]
            (ex_g if len(u_fps) == 1 else df_g).append(ser)

        return {"exact_groups": ex_g, "diff_groups": df_g}


@fastapi_app.post("/api/v2/jobs/duplicates/resolve")
def api_v2_resolve_duplicates(req: V2DuplicatesResolveRequest):
    sc = api_v2_scan_duplicates()
    ex_g, df_g = sc["exact_groups"], sc["diff_groups"]

    try:
        with db_scope(commit=True) as db:
            if req.action == "rename_diff":
                ren_c = 0
                for g in df_g:
                    for it in g:
                        dj = db.query(models.Job).filter(models.Job.id == it["id"]).first()
                        if dj and dj.status in ["WAITING", "PAUSED"]:
                            rf_v = it["rf"]
                            tag = f"RF{rf_v}_ID{dj.id}" if rf_v is not None else f"ID{dj.id}"
                            mc.rename_job_destination(db, dj, tag)
                            ren_c += 1
                return {"ok": True, "renamed": ren_c}

            del_ids = []
            t_grps = ex_g + (df_g if req.include_diff else [])
            for g in t_grps:
                run_g = [x for x in g if x["status"] in mc.ACTIVE_JOB_STATUSES]
                keeper = run_g[0] if run_g else (max(g, key=lambda x: x["id"]) if req.keep_mode == "newer" else min(g, key=lambda x: x["id"]))
                for x in g:
                    if x["id"] != keeper["id"] and x["status"] not in mc.ACTIVE_JOB_STATUSES:
                        del_ids.append(x["id"])

            del_c = 0
            if del_ids:
                with mc.media_cache_lock:
                    for did in del_ids:
                        mc.job_info_cache.pop(did, None)
                del_c = db.query(models.Job).filter(models.Job.id.in_(del_ids)).delete(synchronize_session=False)
            return {"ok": True, "deleted": del_c}
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error resolving duplicates: {e}")
        raise HTTPException(status_code=503, detail="database_busy")


MAX_QUEUE_IMPORT_BYTES = 20 * 1024 * 1024


@fastapi_app.post("/api/v2/jobs/import")
def api_v2_import_queue(file: UploadFile = File(...)):
    raw = file.file.read(MAX_QUEUE_IMPORT_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_QUEUE_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Queue JSON file too large (> 20 MB)")
    try:
        q_data = json.loads(raw.decode("utf-8-sig"), parse_constant=lambda x: None)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(q_data, list):
        q_data = [q_data]
    added, skipped = mc.import_queue_payloads_to_db(q_data)
    return {"ok": True, "added": added, "skipped": skipped}


def _reconcile_4k_slots(pol, old_state) -> str:
    """4K převod zabere 4 sloty → nastavení nesmí být v rozporu (P2). old_state = (sloty, 4K, Upřednostnit 4K) před změnou.
    Zapnutí 4K / Upřednostnit 4K zvedne sloty na 4; snížení slotů pod 4 vypne 4K i Upřednostnit 4K; vypnutí 4K vypne
    i Upřednostnit 4K. Vrací "slots_up_4" / "slots_4k_off" / "" (pro text v rozhraní)."""
    o_slots, o_4k, o_prio = old_state
    n_4k = mc.worker_allows_4k(pol)
    if o_4k and not n_4k:                                   # uživatel vypnul 4K
        pol.prioritize_4k = False
        return ""
    if pol.prioritize_4k and not n_4k:                      # Upřednostnit 4K bez 4K nedává smysl → 4K zapnout
        pol.allow_4k = True
        if (pol.capability or "all") in ("1080p", "sd", "720p"):
            pol.capability = "all"
        n_4k = True
    turned_on = (n_4k and not o_4k) or (bool(pol.prioritize_4k) and not o_prio)
    slots = int(pol.max_slots or 0)
    if turned_on and slots < 4:
        pol.max_slots = 4
        return "slots_up_4"
    if slots < 4 and slots < int(o_slots or 0) and (n_4k or pol.prioritize_4k):
        pol.allow_4k = False
        if (pol.capability or "all") in ("all", "4k"):
            pol.capability = "1080p"
        pol.prioritize_4k = False
        return "slots_4k_off"
    return ""


@fastapi_app.post("/api/v2/workers/bulk_policy")
def api_v2_bulk_update_worker_policy(req: V2WorkerPolicyRequest):
    try:
        with db_scope(commit=True) as db:
            if req.staging_mode is not None:
                mc.AppState.default_staging_mode = req.staging_mode
            if req.power_mode is not None and req.power_mode in mc.VALID_POWER_MODES:
                mc.AppState.default_power_mode = req.power_mode
            if req.use_local_temp is not None:
                mc.AppState.default_use_local_temp = bool(req.use_local_temp)
            if req.prioritize_4k is not None:
                mc.AppState.default_prioritize_4k = bool(req.prioritize_4k)
            if req.is_enabled is True:
                mc.AppState.maintenance_mode = False
            mc.save_manager_config()

            pol_map = {p.worker_name: p for p in db.query(models.WorkerPolicy).all()}
            all_names = sorted(set(list(pol_map.keys()) + list(mc.active_workers.keys())))
            aff = n_fixed = 0
            for w_name in all_names:
                pol = pol_map.get(w_name) or models.WorkerPolicy(
                    worker_name=w_name, max_slots=4, capability="all", allow_4k=True,
                    is_enabled=True, prioritize_4k=mc.AppState.default_prioritize_4k,
                    use_local_temp=mc.AppState.default_use_local_temp,
                    staging_mode=mc.AppState.default_staging_mode,
                    power_mode=mc.AppState.default_power_mode
                )
                if w_name not in pol_map:
                    db.add(pol)
                old_def_ms = mc.default_slots_for_worker(pol)
                old_state = (int(pol.max_slots or 0), mc.worker_allows_4k(pol), bool(pol.prioritize_4k))
                if req.capability is not None:
                    pol.capability = req.capability
                    pol.allow_4k = req.capability.strip().lower() not in ("1080p", "sd", "720p")
                if req.allow_4k is not None:
                    pol.allow_4k = bool(req.allow_4k)
                    if not req.allow_4k and pol.capability in ("all", "4k"):
                        pol.capability = "1080p"
                    elif req.allow_4k and pol.capability in ("1080p", "sd", "720p"):
                        pol.capability = "all"
                if req.max_slots is not None:
                    pol.max_slots = mc.clamp_num(req.max_slots, 1, 16, mc.default_slots_for_worker(pol), as_int=True)
                elif (req.capability is not None or req.allow_4k is not None) and pol.max_slots == old_def_ms:
                    pol.max_slots = mc.default_slots_for_worker(pol)
                if req.staging_mode is not None:
                    pol.staging_mode = req.staging_mode
                if req.power_mode is not None and req.power_mode in mc.VALID_POWER_MODES:
                    pol.power_mode = req.power_mode
                if req.prioritize_4k is not None:
                    pol.prioritize_4k = bool(req.prioritize_4k)
                if req.use_local_temp is not None:
                    pol.use_local_temp = bool(req.use_local_temp)
                if req.is_enabled is not None:
                    pol.is_enabled = bool(req.is_enabled)
                    if pol.is_enabled:
                        mc.clear_worker_intake_off_reason(w_name)
                if _reconcile_4k_slots(pol, old_state):
                    n_fixed += 1
                aff += 1
            db.flush()
            pm_ok = mc.enforce_premux_local_disk_guard(db)
            mc.save_manager_config(db)
            return {"ok": True, "affected": aff, "slots_4k_fixed": n_fixed, "smart_premux": mc.AppState.smart_premux, "premux_allowed": pm_ok}
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Bulk worker policy update failed: {e}")
        raise HTTPException(status_code=503, detail="database_busy")


@fastapi_app.post("/api/v2/workers/{worker_name}/policy")
def api_v2_update_worker_policy(worker_name: str, req: V2WorkerPolicyRequest, request: Request):
    is_w_auth = bool(getattr(request.state, "is_worker_auth", False))
    w_bound = getattr(request.state, "worker_bound_name", "") or ""
    if is_w_auth:
        if w_bound and w_bound != worker_name:
            mc.append_manager_log(f"[SECURITY] Worker „{w_bound}“ se pokusil změnit politiku cizího workeru „{worker_name}“.")
            raise HTTPException(status_code=403, detail="worker_policy_not_allowed")
        if any(getattr(req, f) is not None for f in ("capability", "allow_4k", "max_slots", "staging_mode", "prioritize_4k", "use_local_temp")):
            mc.append_manager_log(f"[SECURITY] Worker „{w_bound or worker_name}“ se pokusil změnit vyhrazené pole politiky.")
            raise HTTPException(status_code=403, detail="worker_policy_field_not_allowed")
    try:
        with db_scope(commit=True, immediate=True) as db:
            pol = mc.get_or_create_worker_policy(db, worker_name)
            old_def_ms = mc.default_slots_for_worker(pol)
            old_state = (int(pol.max_slots or 0), mc.worker_allows_4k(pol), bool(pol.prioritize_4k))
            if req.capability is not None:
                pol.capability = req.capability
                pol.allow_4k = req.capability.strip().lower() not in ("1080p", "sd", "720p")
            if req.allow_4k is not None:
                pol.allow_4k = bool(req.allow_4k)
                if not req.allow_4k and pol.capability in ("all", "4k"):
                    pol.capability = "1080p"
                elif req.allow_4k and pol.capability in ("1080p", "sd", "720p"):
                    pol.capability = "all"
            if req.max_slots is not None:
                pol.max_slots = mc.clamp_num(req.max_slots, 1, 16, mc.default_slots_for_worker(pol), as_int=True)
            elif (req.capability is not None or req.allow_4k is not None) and pol.max_slots == old_def_ms:
                pol.max_slots = mc.default_slots_for_worker(pol)
            if req.staging_mode is not None:
                pol.staging_mode = req.staging_mode
            if req.power_mode is not None and req.power_mode in mc.VALID_POWER_MODES:
                pol.power_mode = req.power_mode
            if req.custom_cores is not None:
                pol.custom_cores = mc.serialize_custom_cores_list(req.custom_cores)
            if req.prioritize_4k is not None:
                pol.prioritize_4k = bool(req.prioritize_4k)
            if req.use_local_temp is not None:
                pol.use_local_temp = bool(req.use_local_temp)
            if req.is_enabled is not None:
                pol.is_enabled = bool(req.is_enabled)
            if req.is_enabled is True:
                mc.AppState.maintenance_mode = False
                mc.clear_worker_intake_off_reason(worker_name)
            note = _reconcile_4k_slots(pol, old_state)
            db.flush()
            pm_ok = mc.enforce_premux_local_disk_guard(db)
            mc.save_manager_config(db)
            return {"ok": True, "smart_premux": mc.AppState.smart_premux, "premux_allowed": pm_ok, "note": note,
                    "policy": {"max_slots": int(pol.max_slots or 0), "capability": pol.capability or "all",
                               "allow_4k": mc.worker_allows_4k(pol), "prioritize_4k": bool(pol.prioritize_4k)}}
    except HTTPException:
        raise
    except Exception as e:
        # Chyba musí být vidět i podle HTTP kódu – worker jinak považoval 200 {"ok": false} za úspěch a zahodil změnu.
        mc.logger.error(f"Worker policy update for '{worker_name}' failed: {e}")
        raise HTTPException(status_code=503, detail="policy_update_failed")


@fastapi_app.post("/api/v2/workers/{worker_id}/windows_settings")
def api_v2_update_worker_windows_settings(worker_id: str, req: V2WorkerWindowsSettingsRequest):
    w_name = mc.sanitize_display_name(worker_id, "")
    if not w_name:
        raise HTTPException(status_code=400, detail="Invalid worker_id")
    view = mc.queue_worker_windows_settings(w_name, req.dict())
    mc.append_manager_log(
        mc.L(
            f"[WINDOWS-SETTINGS] Naplánována vzdálená změna spouštění pro worker '{w_name}' (režim: {view['autostart_mode']}, plocha: {view['desktop_shortcut']}, start: {view['start_menu_shortcut']}, tray: {view['tray_minimize']}).",
            f"[WINDOWS-SETTINGS] Queued remote startup settings for worker '{w_name}' (mode: {view['autostart_mode']}, desktop: {view['desktop_shortcut']}, start_menu: {view['start_menu_shortcut']}, tray: {view['tray_minimize']})."
        )
    )
    return {"ok": True, "worker_name": w_name, "windows_settings": view}


@fastapi_app.post("/api/v2/workers/{worker_name}/stop")
def api_v2_stop_worker_and_jobs(worker_name: str, req: V2WorkerStopRequest = None):
    stopped = 0
    try:
        with db_scope(commit=True, immediate=True) as db:
            if req is None or req.disable_worker:
                pol = mc.get_or_create_worker_policy(db, worker_name)
                pol.is_enabled = False
                db.flush()
                mc.save_manager_config(db)

            r_jobs = (
                db.query(models.Job)
                .options(defer(models.Job.hb_json_payload))
                .filter(
                    models.Job.worker_name == worker_name,
                    models.Job.status.in_(mc.ACTIVE_JOB_STATUSES)
                )
                .all()
            )
            for j in r_jobs:
                if mc.is_system_build_job(j):
                    with mc.build_lock:
                        mc.binary_build_state["status"] = "IDLE"
                        mc.binary_build_state["updating_tools"] = []
                        mc.binary_build_state["step_message"] = mc.T("api_stopped_by_user")
                j.status = "CANCELLED"
                setattr(j, "is_paused", False)
                mc.job_heartbeats.pop(j.id, None)
                mc.job_assigned_time.pop(j.id, None)
                mc.release_output_upload_slot(j.id)
                stopped += 1
        mc.append_manager_log(
            mc.T("api_worker_stopped_by_user", worker_name, stopped)
        )
        return {"ok": True, "stopped_jobs": stopped}
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Error stopping worker '{worker_name}': {e}")
        raise HTTPException(status_code=503, detail="database_busy")


@fastapi_app.delete("/api/v2/workers/{worker_name}")
def api_v2_delete_worker(worker_name: str):
    with db_scope(commit=True, immediate=True) as db:
        r_cnt = db.query(models.Job.id).filter(
            models.Job.worker_name == worker_name,
            models.Job.status.in_(mc.ACTIVE_JOB_STATUSES)
        ).count()
        if r_cnt > 0:
            raise HTTPException(
                status_code=400,
                detail=mc.T("api_cannot_delete_worker_while", worker_name, r_cnt)
            )
        db.query(models.WorkerPolicy).filter(models.WorkerPolicy.worker_name == worker_name).delete()
        db.flush()
        mc.enforce_premux_local_disk_guard(db)
        mc.cleanup_deleted_worker_state(worker_name)
        mc.save_manager_config(db)
    return {"ok": True}


@fastapi_app.post("/api/v2/cluster/action")
def api_v2_cluster_action(req: V2ClusterActionRequest, request: Request):
    act = req.action
    if getattr(request.state, "is_worker_auth", False):
        # Worker smí přes /api/v2/cluster/action synchronizovat výhradně jazyk UI (ui_lang)
        w_bound = getattr(request.state, "worker_bound_name", "") or "?"
        allowed_lang_only = (
            act == "update_settings"
            and isinstance(req.settings, dict)
            and bool(req.settings)
            and set(req.settings.keys()) <= {"ui_lang"}
        )
        if not allowed_lang_only:
            mc.append_manager_log(f"[SECURITY] Worker „{w_bound}“ se pokusil o zakázanou akci clusteru: {act}.")
            raise HTTPException(status_code=403, detail="worker_action_not_allowed")
    action_extra = {}
    try:
        with db_scope(commit=True, immediate=True) as db:
            if act in ("toggle_maintenance", "toggle_all_workers_intake"):
                pol_map = {p.worker_name: p for p in db.query(models.WorkerPolicy).all()}
                all_names = sorted(set(list(pol_map.keys()) + list(mc.active_workers.keys())))
                any_enabled = any((pol_map[wn].is_enabled if wn in pol_map else True) for wn in all_names) and not mc.AppState.maintenance_mode
                target_enabled = not any_enabled
                mc.AppState.maintenance_mode = not target_enabled
                for wn in all_names:
                    pol = pol_map.get(wn) or mc.get_or_create_worker_policy(db, wn)
                    pol.is_enabled = target_enabled
                    if target_enabled:
                        mc.clear_worker_intake_off_reason(wn)
                if target_enabled:
                    db.query(models.Job).filter(models.Job.status == "PAUSED").update(
                        {models.Job.status: "WAITING"}, synchronize_session=False
                    )
                db.flush()
                mc.save_manager_config(db)
                if target_enabled:
                    mc.append_manager_log(mc.T("api_job_intake_across_all_2"))
                else:
                    mc.append_manager_log(mc.T("api_job_intake_across_all"))
            elif act in ("freeze_all_active", "unfreeze_all_active"):
                target_freeze = (act == "freeze_all_active")
                for j in (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.status.in_(mc.COMPUTE_ACTIVE_STATUSES))
                    .all()
                ):
                    setattr(j, "is_paused", target_freeze)
                    mc.job_heartbeats[j.id] = time.time()
                if target_freeze:
                    mc.append_manager_log(mc.T("api_all_running_jobs_frozen"))
                else:
                    mc.append_manager_log(mc.T("api_all_running_jobs_unfrozen"))
            elif act == "release_all_disk":
                held_jobs = (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.status == "WAITING_DISK")
                    .all()
                )
                released_ids, lost_ids = mc.release_or_fail_held_disk_jobs(db, held_jobs)
                action_extra = {"released_count": len(released_ids), "lost_count": len(lost_ids), "lost_ids": lost_ids}
            elif act == "stop_all_active":
                for pol in db.query(models.WorkerPolicy).all():
                    pol.is_enabled = False
                for j in (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.status.in_(mc.ACTIVE_JOB_STATUSES))
                    .all()
                ):
                    if mc.is_system_build_job(j):
                        with mc.build_lock:
                            mc.binary_build_state["status"] = "IDLE"
                            mc.binary_build_state["updating_tools"] = []
                            mc.binary_build_state["step_message"] = mc.T("api_stopped_by_user")
                    j.status = "CANCELLED"
                    setattr(j, "is_paused", False)
                    mc.job_heartbeats.pop(j.id, None)
                    mc.job_assigned_time.pop(j.id, None)
                    mc.release_output_upload_slot(j.id)
                db.flush()
                mc.save_manager_config(db)
                mc.append_manager_log(
                    mc.T("api_bulk_stop_all_workers")
                )
            elif act in ("pause_all", "resume_all"):
                if act == "pause_all":
                    db.query(models.Job).filter(
                        models.Job.status == "WAITING",
                        or_(models.Job.preset_name.is_(None), models.Job.preset_name != "SYSTEM_BUILD"),
                    ).update({models.Job.status: "PAUSED"}, synchronize_session=False)
                else:
                    db.query(models.Job).filter(models.Job.status == "PAUSED").update(
                        {models.Job.status: "WAITING"}, synchronize_session=False
                    )
            elif act == "requeue_errors":
                e_jobs = (
                    db.query(models.Job)
                    .options(defer(models.Job.hb_json_payload))
                    .filter(models.Job.status == "ERROR")
                    .order_by(models.Job.id.asc())
                    .all()
                )
                bp = mc.next_top_priority(db) - len(e_jobs) + 1
                for idx, j in enumerate(e_jobs):
                    mc.reset_job_for_requeue(
                        j,
                        priority=(0 if mc.is_system_build_job(j) else bp + idx),
                        reset_attempts=True,
                        clear_errors=True,
                    )
            elif act in ("clear_history_14", "clear_history_all"):
                if act == "clear_history_14":
                    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=14)
                    db.query(models.Job).filter(models.Job.status.in_(["COMPLETED", "CANCELLED"]), models.Job.created_at < cutoff).delete()
                else:
                    db.query(models.Job).filter(models.Job.status.in_(["COMPLETED", "CANCELLED"])).delete()
                mc.invalidate_analytics_cache()
            elif act == "reset_analytics":
                db.query(models.EncodingStats).delete()
                mc.AppState.analytics_seeded = True
                mc.save_manager_config()
                mc.invalidate_analytics_cache()
            elif act == "update_settings" and isinstance(req.settings, dict):
                pm_ok = mc.enforce_premux_local_disk_guard(db)
                if "ui_lang" in req.settings and req.settings["ui_lang"] in ("en", "cs"):
                    mc.AppState.ui_lang = req.settings["ui_lang"]

                for bk in (
                    "onboarding_done",
                    "auto_clean_history", "convert_ass_to_srt", "auto_clean_empty_subs",
                    "auto_extract_forced_subs", "write_mkv_stats", "lpcm_passthru",
                    "smart_premux", "auto_build_on_first_run", "auto_update_binaries", "auto_update_cluster",
                    "default_use_local_temp", "default_prioritize_4k", "continue_encoding_on_disk_full"
                ):
                    if bk in req.settings:
                        setattr(mc.AppState, bk, False if (bk == "smart_premux" and not pm_ok) else bool(req.settings[bk]))
                if str(req.settings.get("ass_srt_mode") or "") in mc.VALID_ASS_SRT_MODES:
                    mc.AppState.ass_srt_mode = str(req.settings["ass_srt_mode"])
                if str(req.settings.get("burn_in_subs") or "") in mc.VALID_BURN_IN_MODES:
                    mc.AppState.burn_in_subs = str(req.settings["burn_in_subs"])
                if str(req.settings.get("dts_hd_mode") or "") in mc.VALID_DTS_HD_MODES:
                    mc.AppState.dts_hd_mode = str(req.settings["dts_hd_mode"])
                if "min_free_output_gb" in req.settings and req.settings["min_free_output_gb"] is not None:
                    mc.AppState.min_free_output_gb = mc.clamp_num(req.settings["min_free_output_gb"], 1.0, 100000.0, mc.AppState.min_free_output_gb)
                if "worker_cpu_source" in req.settings or "worker_ram_source" in req.settings:
                    if str(req.settings.get("worker_cpu_source")) in mc.VALID_TELEMETRY_SOURCES:
                        mc.AppState.worker_cpu_source = str(req.settings["worker_cpu_source"])
                    if str(req.settings.get("worker_ram_source")) in mc.VALID_TELEMETRY_SOURCES:
                        mc.AppState.worker_ram_source = str(req.settings["worker_ram_source"])
                    mc.AppState.telemetry_source = (
                        mc.AppState.worker_cpu_source
                        if mc.AppState.worker_cpu_source == mc.AppState.worker_ram_source
                        else "both"
                    )
                elif "telemetry_source" in req.settings and str(req.settings["telemetry_source"]) in mc.VALID_TELEMETRY_SOURCES:
                    mc.AppState.telemetry_source = str(req.settings["telemetry_source"])
                    mc.AppState.worker_cpu_source = mc.AppState.telemetry_source
                    mc.AppState.worker_ram_source = mc.AppState.telemetry_source
                if "ram_format" in req.settings and str(req.settings["ram_format"]) in mc.VALID_RAM_FORMATS:
                    mc.AppState.ram_format = str(req.settings["ram_format"])
                for sk in (
                    "ineffective_action", "ineffective_prefix", "ineffective_subfolder",
                    "preferred_builder_worker", "default_staging_mode", "default_power_mode"
                ):
                    if sk in req.settings and req.settings[sk] is not None:
                        vs = mc.sanitize_display_name(req.settings[sk])
                        if sk == "default_power_mode" and vs not in mc.VALID_POWER_MODES:
                            continue
                        if sk not in ("preferred_builder_worker", "default_staging_mode", "default_power_mode"):
                            vs = re.sub(r'[<>:"/\\|?*]', '_', vs)
                        if vs:
                            setattr(mc.AppState, sk, vs)
                for pk in (
                    "win_default_inputs_dir", "win_default_outputs_dir",
                    "smb_username", "smb_password",
                    "linux_default_inputs_dir", "linux_default_outputs_dir", "linux_default_temp_dir"
                ):
                    if pk == "smb_password" and req.settings.get(pk) == mc.SECRET_MASK:
                        continue   # maska z UI -> heslo zůstává
                    if pk in req.settings and req.settings[pk] is not None:
                        val_p = mc.sanitize_path(req.settings[pk]) if pk.endswith("_dir") else str(req.settings[pk]).strip()
                        setattr(mc.AppState, pk, val_p)
                mc.save_manager_config()

                auto_build_tick_async("settings", True)
    except HTTPException:
        raise
    except Exception as e:
        mc.logger.error(f"Cluster action '{act}' failed: {e}")
        raise HTTPException(status_code=503, detail="cluster_action_failed")
    return {
        "ok": True,
        "maintenance_mode": mc.AppState.maintenance_mode,
        "smart_premux": mc.AppState.smart_premux,
        "ui_lang": mc.AppState.ui_lang,
        **action_extra
    }


@fastapi_app.get("/api/v2/changelog")
def api_v2_get_changelog():
    cp = os.path.join(mc.APP_BASE_DIR, "CHANGELOG.md")
    if os.path.exists(cp):
        with open(cp, "r", encoding="utf-8-sig") as f:
            return {"markdown": f.read()}
    return {"markdown": "# Changelog\nCHANGELOG.md not found."}


@fastapi_app.get("/api/v2/backups")
def api_v2_list_backups():
    return {"ok": True, "backups": mc.list_db_backups(), "dir": mc.BACKUP_DIR}


@fastapi_app.get("/api/v2/backups/{name}")
def api_v2_download_backup(name: str):
    fn = os.path.basename(name or "")
    if not mc.BACKUP_NAME_RE.match(fn):
        raise HTTPException(status_code=400, detail="Invalid backup name")
    fp = os.path.join(mc.BACKUP_DIR, fn)
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(fp, media_type="application/octet-stream", filename=fn)


@fastapi_app.post("/api/v2/workers/{worker_name}/update_now")
def api_v2_worker_update_now(worker_name: str):
    """O4: „Aktualizovat teď“ na kartě workeru – worker zkusí aktualizaci hned (mimo čekání po neúspěchu),
    jakmile dokončí rozpracované převody. Doručí se v nejbližším ping / jobs/next."""
    ver = str(mc.worker_versions.get(worker_name) or "")
    if not ver or ver == mc.WORKER_VERSION:
        return {"ok": True, "queued": False, "detail": "up_to_date"}
    mc.request_worker_update_now(worker_name)
    mc.append_manager_log(mc.T("api_worker_update_now_requested", worker_name, ver, mc.WORKER_VERSION))
    return {"ok": True, "queued": True}


# Komprese (D1) jako poslední = nejvnější middleware: komprimuje už hotové odpovědi.
fastapi_app.add_middleware(SelectiveGZipMiddleware)


def run_selftest() -> int:
    """O1: zkušební načtení nové verze. Spouští ho STARÝ Manager před aktualizací v dočasném kontejneru
    z nového obrazu (bez sítě a bez svazků). V tu chvíli je už načtený celý main.py (importy, trasy,
    middleware); nic se nespouští, jen se ověří, že je verze úplná a umí se dál aktualizovat."""
    problems = []
    paths = {getattr(r, "path", "") for r in fastapi_app.routes}
    for p in ("/api/jobs/next", "/api/workers/ping", "/api/update/version", "/api/update/manifest",
              "/api/update/file/{name}", "/api/v2/state", "/api/v2/system/update_apply"):
        if p not in paths:
            problems.append(f"route {p} missing")
    for fn in ("trigger_self_recreate", "run_image_selftest", "evaluate_update_attempt", "write_update_attempt"):
        if not callable(getattr(mc.dsu, fn, None)):
            problems.append(f"docker_selfupdate.{fn} missing")
    for name in ("worker_core.py", "worker.pyw", "worker_linux.py", "locales.js"):
        try:
            data, _ = _ota_file_bytes(name)
            if not data:
                problems.append(f"{name} missing")
            elif name.endswith((".py", ".pyw")):
                compile(data.decode("utf-8-sig"), name, "exec")
        except Exception as e:
            problems.append(f"{name}: {e}")
    if not mc.load_unified_locales_dict():
        problems.append("locales.js unreadable")
    if problems:
        print("HB-SELFTEST-FAIL " + "; ".join(problems)[:1500], flush=True)
        return 1
    print(f"{mc.dsu.SELFTEST_OK_MARK} manager {mc.MANAGER_VERSION}", flush=True)
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    if "--selftest" in sys.argv[1:]:
        os._exit(run_selftest())
    import uvicorn
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8383)