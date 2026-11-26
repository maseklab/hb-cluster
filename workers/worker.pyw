# ==============================================================================
# HB CLUSTER WORKER - WINDOWS GUI CLIENT (worker.pyw)
# ==============================================================================
# FEATURE MANIFEST (DO NOT REMOVE ANY OF THESE FEATURES IN FUTURE EDITS):
# 1. Immortal Bootstrap Guard (`ensure_worker_core_module`): Strips UTF-8 BOM (`utf-8-sig`)
#    and normalizes CRLF before `compile()` verification + fallback UDP discovery.
# 2. Verified OTA Auto-Update with rollback (`wc.stage_windows_update` + --selftest + update_worker.bat).
# 3. Dynamic 32-bit RGBA Win32 System Tray & Window Status Icon (`render_tray_status_icon`):
#    - Replaces generic Python icon with live color-coded status badge (Green + active job count,
#      Amber + Pause/Disk warning, Sky Blue + Checkmark when idle, Red + '!' when offline).
# 4. Compact Auto-Hiding Settings Drawer (60s Inactivity Timer) & Permanent Worker Name Badge:
#    - Displays `[Worker_Name]` permanently as a non-editable badge in the top bar.
#    - Hides URL/folder paths and tray checkboxes by default on startup; clicking `Options`
#      opens the drawer and automatically collapses it after 60 seconds of inactivity.
# 5. Direct Live Job Pause Button (`⏸/▶`) in GUI & System Tray + Automatic Temp Disk Guard:
#    - Freezes `HandBrakeCLI.exe` (`NtSuspendProcess`) directly from the Worker GUI, Manager UI,
#      or when `HB_Temp` drops below 3.5 GB, preventing `code 4` crashes at the end of 4K encodes.
# 6. Strict Multi-Thread ECO Affinity Enforcement + Laptop Wake-Lock & Instant Standby Reconnect
#    (`WM_POWERBROADCAST` + `PowerSetRequest` Display/System/AwayMode/Execution).
# 7. Dual-Row Live CPU & RAM Mini-Graph Bars + Explicit Phase Tags (`[STAGING]`, `[ENCODING]`,
#    `[PAUSED]`, `[SAVING]`) in the job table.
# 8. Real-Time Non-Blocking Pre-Mux Progress + Exact `.MPLS` Playlist Enforcement +
#    Output Stream Contract Audit Gate (`wc.verify_output_stream_contract`) + Tail Error Reporting.
# ==============================================================================

import os
import sys
import math
import time
import zlib
import errno
import socket
import struct
import subprocess
import re
import json
import base64
import threading
import shutil
import traceback
import importlib
import importlib.util
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import copy
import ctypes
from ctypes import wintypes
import winreg
import webbrowser
import datetime
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
# O1: zkušební načtení nové verze před aktualizací (worker.pyw --selftest v app/ota_stage; nic nespouští)
SELFTEST = "--selftest" in sys.argv[1:]

try:
    import worker_core as wc
except Exception:
    wc = None

if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def _load_app_version(fallback: str = "0.0.0") -> str:
    """Verze se čte jen ze souboru VERSION (app/VERSION). Bez něj 0.0.0 – Manager nabídne aktualizaci,
    která VERSION obnoví. (Literály verze kvůli workerům <= 2.11.4ZH zrušeny, O3.)"""
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


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> "_GUID":
    import uuid as _uuid
    b = _uuid.UUID(s).bytes_le
    g = _GUID()
    ctypes.memmove(ctypes.byref(g), b, 16)
    return g


_GUID_SLEEP_SUBGROUP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
_GUID_ALLOW_RTC_WAKE = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"


def read_wake_timers_setting() -> str:
    """„Povolit časovače probuzení“ v aktivním plánu napájení (napájení ze sítě):
    enabled / disabled / important_only / unknown. Jen čtení, bez práv správce."""
    try:
        pp = ctypes.windll.powrprof
        k32 = ctypes.windll.kernel32
        pp.PowerGetActiveScheme.argtypes = [wintypes.HKEY, ctypes.POINTER(ctypes.POINTER(_GUID))]
        pp.PowerGetActiveScheme.restype = wintypes.DWORD
        pp.PowerReadACValueIndex.argtypes = [wintypes.HKEY, ctypes.POINTER(_GUID), ctypes.POINTER(_GUID), ctypes.POINTER(_GUID), ctypes.POINTER(wintypes.DWORD)]
        pp.PowerReadACValueIndex.restype = wintypes.DWORD
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p
        scheme_ptr = ctypes.POINTER(_GUID)()
        if pp.PowerGetActiveScheme(None, ctypes.byref(scheme_ptr)) != 0 or not scheme_ptr:
            return "unknown"
        try:
            val = wintypes.DWORD(0)
            sub = _guid(_GUID_SLEEP_SUBGROUP)
            rtc = _guid(_GUID_ALLOW_RTC_WAKE)
            rc = pp.PowerReadACValueIndex(None, scheme_ptr, ctypes.byref(sub), ctypes.byref(rtc), ctypes.byref(val))
        finally:
            try:
                k32.LocalFree(scheme_ptr)
            except Exception:
                pass
        if rc != 0:
            return "unknown"
        return {0: "disabled", 1: "enabled", 2: "important_only"}.get(int(val.value), "unknown")
    except Exception:
        return "unknown"


def _resolve_and_persist_python_exe() -> tuple:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(base_dir, "config.json")
    cfg_data = {}
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg_data = loaded
        except Exception:
            cfg_data = {}

    configured_py = str(cfg_data.get("python_path") or "").strip()
    if configured_py and "windowsapps" not in configured_py.lower() and os.path.isfile(configured_py):
        return configured_py, cfg_path, cfg_data

    cand = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.isfile(cand):
        cand = sys.executable

    if cand and "windowsapps" not in cand.lower() and os.path.isfile(cand):
        if configured_py != cand:
            try:
                cfg_data["python_path"] = cand
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, indent=4, ensure_ascii=False)
            except Exception:
                pass
    return cand, cfg_path, cfg_data


def _extract_unc_host_from_paths(*paths) -> str:
    for raw_p in paths:
        p = str(raw_p or "").strip().replace("/", "\\")
        if p.startswith("\\\\"):
            pts = [x for x in p.lstrip("\\").split("\\") if x]
            if pts:
                return pts[0]
    return ""


def _resolve_launcher_target_and_workdir(base_dir: str) -> tuple:
    parent_dir = os.path.dirname(base_dir)
    p_exe = os.path.join(parent_dir, "HBWorker.exe")
    if os.path.isfile(p_exe):
        return p_exe, parent_dir
    c_exe = os.path.join(base_dir, "HBWorker.exe")
    if os.path.isfile(c_exe):
        return c_exe, base_dir
    p_bat = os.path.join(parent_dir, "Start_Worker.bat")
    if os.path.isfile(p_bat):
        return p_bat, parent_dir
    c_bat = os.path.join(base_dir, "Start_Worker.bat")
    if os.path.isfile(c_bat):
        return c_bat, base_dir
    return (p_exe if os.path.basename(base_dir).lower() == "app" else c_exe), (parent_dir if os.path.basename(base_dir).lower() == "app" else base_dir)


def _create_or_remove_windows_shortcuts(base_dir: str, desktop_enabled: bool = True, start_menu_enabled: bool = True):
    if os.name != "nt":
        return
    target_path, work_dir = _resolve_launcher_target_and_workdir(base_dir)
    if not os.path.isfile(target_path):
        return
    ico_loc = f"{target_path},0" if target_path.lower().endswith(".exe") and os.path.isfile(target_path) else os.path.join(base_dir, "hbw.ico")
    t_esc = target_path.replace("'", "''")
    w_esc = work_dir.replace("'", "''")
    i_esc = ico_loc.replace("'", "''")
    r_esc = os.path.normpath(work_dir).rstrip("\\").replace("'", "''")
    d_flag = "$true" if desktop_enabled else "$false"
    s_flag = "$true" if start_menu_enabled else "$false"
    ps_cmd = (
        "$WshShell = New-Object -ComObject WScript.Shell; "
        "$deskLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'HB Worker.lnk'; "
        "$smDir = [Environment]::GetFolderPath('Programs'); "
        "$smLnk = Join-Path $smDir 'HB Worker.lnk'; "
        f"$root = '{r_esc}'; "
        "$mine = { param($l) $t = [string]$WshShell.CreateShortcut($l).TargetPath; "
        "  (-not $t) -or $t.ToLower().StartsWith($root.ToLower() + '\\') -or (-not (Test-Path -LiteralPath $t)) }; "
        f"if ({d_flag}) {{ "
        f"  $s1 = $WshShell.CreateShortcut($deskLnk); $s1.TargetPath = '{t_esc}'; $s1.WorkingDirectory = '{w_esc}'; "
        f"  $s1.Description = 'HandBrake Cluster Worker'; $s1.IconLocation = '{i_esc}'; $s1.Save() "
        "} else { if ((Test-Path $deskLnk) -and (& $mine $deskLnk)) { Remove-Item $deskLnk -Force -ErrorAction SilentlyContinue } }; "
        f"if ({s_flag}) {{ "
        f"  $s2 = $WshShell.CreateShortcut($smLnk); $s2.TargetPath = '{t_esc}'; $s2.WorkingDirectory = '{w_esc}'; "
        f"  $s2.Description = 'HandBrake Cluster Worker'; $s2.IconLocation = '{i_esc}'; $s2.Save() "
        "} else { if ((Test-Path $smLnk) -and (& $mine $smLnk)) { Remove-Item $smLnk -Force -ErrorAction SilentlyContinue } }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
        )
    except Exception:
        pass


AUTOSTART_TASK_NAME = "HBClusterWorker"


def _get_windows_session_id() -> int:
    if os.name != "nt":
        return 1
    try:
        sess_id = wintypes.DWORD(0)
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sess_id)):
            return int(sess_id.value)
    except Exception:
        pass
    return 1


def _is_windows_pid_alive(pid: int) -> bool:
    if os.name != "nt" or not pid or int(pid) <= 0:
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        hp = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not hp:
            return False
        try:
            code = wintypes.DWORD(0)
            if k32.GetExitCodeProcess(hp, ctypes.byref(code)):
                return int(code.value) == 259   # STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(hp)
    except Exception:
        return False


def _is_windows_process_elevated() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _get_current_windows_user_sid() -> str:
    if os.name != "nt":
        return ""
    try:
        a32 = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        a32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a32.OpenProcessToken.restype = wintypes.BOOL
        a32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        a32.GetTokenInformation.restype = wintypes.BOOL
        a32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        a32.ConvertSidToStringSidW.restype = wintypes.BOOL
        htok = wintypes.HANDLE()
        if not a32.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(htok)):   # TOKEN_QUERY
            return ""
        try:
            sz = wintypes.DWORD(0)
            a32.GetTokenInformation(htok, 1, None, 0, ctypes.byref(sz))   # TokenUser = 1
            if not sz.value:
                return ""
            buf = ctypes.create_string_buffer(sz.value)
            if not a32.GetTokenInformation(htok, 1, buf, sz.value, ctypes.byref(sz)):
                return ""
            sid_ptr = ctypes.c_void_p.from_buffer(buf)
            sptr = wintypes.LPWSTR()
            if not a32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sptr)):
                return ""
            try:
                return str(sptr.value or "").strip()
            finally:
                k32.LocalFree(sptr)
        finally:
            k32.CloseHandle(htok)
    except Exception:
        return ""


def _get_current_windows_user_account() -> str:
    dom = str(os.environ.get("USERDOMAIN") or "").strip()
    usr = str(os.environ.get("USERNAME") or "").strip()
    if dom and usr:
        return f"{dom}\\{usr}"
    return usr


def _resolve_autostart_task_exec(base_dir: str, py_exe: str = "") -> tuple:
    """Vrací (command, arguments, work_dir) pro naplánovanou úlohu před přihlášením."""
    target_path, work_dir = _resolve_launcher_target_and_workdir(base_dir)
    if os.path.isfile(target_path) and target_path.lower().endswith(".exe"):
        return target_path, "--bg-boot", work_dir
    cand_py = str(py_exe or sys.executable or "").strip()
    if cand_py and os.path.isfile(cand_py):
        pyw_p = os.path.join(os.path.dirname(cand_py), "pythonw.exe")
        run_py = pyw_p if os.path.isfile(pyw_p) else cand_py
    else:
        run_py = sys.executable
    script_p = os.path.join(base_dir, "worker.pyw")
    return run_py, f'"{script_p}" --bg-boot', base_dir


def _build_before_login_task_xml(user_id: str, command: str, arguments: str, work_dir: str) -> str:
    """XML definice úlohy Plánovače úloh (C+): pod účtem uživatele bez hesla (S4U), běžná práva (LeastPrivilege)."""
    cmd_val = f'"{command}"' if (" " in command and not command.startswith('"')) else command
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        '  <RegistrationInfo>\r\n'
        '    <Description>HB Cluster Worker (before sign-in)</Description>\r\n'
        '  </RegistrationInfo>\r\n'
        '  <Triggers>\r\n'
        '    <BootTrigger>\r\n'
        '      <Enabled>true</Enabled>\r\n'
        '      <Delay>PT10S</Delay>\r\n'
        '    </BootTrigger>\r\n'
        '  </Triggers>\r\n'
        '  <Principals>\r\n'
        '    <Principal id="Author">\r\n'
        f'      <UserId>{_xml_escape(str(user_id or ""))}</UserId>\r\n'
        '      <LogonType>S4U</LogonType>\r\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\r\n'
        '    </Principal>\r\n'
        '  </Principals>\r\n'
        '  <Settings>\r\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\r\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\r\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n'
        '    <IdleSettings>\r\n'
        '      <StopOnIdleEnd>false</StopOnIdleEnd>\r\n'
        '      <RestartOnIdle>false</RestartOnIdle>\r\n'
        '    </IdleSettings>\r\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n'
        '    <Enabled>true</Enabled>\r\n'
        '    <Hidden>false</Hidden>\r\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\r\n'
        '    <WakeToRun>false</WakeToRun>\r\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\n'
        '    <Priority>7</Priority>\r\n'
        '  </Settings>\r\n'
        '  <Actions Context="Author">\r\n'
        '    <Exec>\r\n'
        f'      <Command>{_xml_escape(cmd_val)}</Command>\r\n'
        f'      <Arguments>{_xml_escape(str(arguments or ""))}</Arguments>\r\n'
        f'      <WorkingDirectory>{_xml_escape(str(work_dir or ""))}</WorkingDirectory>\r\n'
        '    </Exec>\r\n'
        '  </Actions>\r\n'
        '</Task>\r\n'
    )


def _classify_autostart_task_xml(xml_text: str, user_sid: str = "", user_account: str = "", expected_cmd: str = "") -> str:
    """Vyhodnotí XML úlohy HBClusterWorker: 'ok' (S4U, běžná práva, náš účet, BootTrigger, cesta) nebo 'legacy'."""
    if not xml_text or not xml_text.strip():
        return "unknown"
    try:
        cleaned = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", xml_text.lstrip("\ufeff"), count=1)
        root = ET.fromstring(cleaned)
    except Exception:
        return "legacy"

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def _child_text(node, name: str) -> str:
        for ch in node:
            if _local(ch.tag) == name:
                return (ch.text or "").strip()
        return ""

    principal = None
    has_boot = False
    exec_cmd = ""
    for el in root.iter():
        lt = _local(el.tag)
        if lt == "Principal" and principal is None:
            principal = el
        elif lt == "BootTrigger":
            en = _child_text(el, "Enabled").lower()
            if en != "false":
                has_boot = True
        elif lt == "Exec" and not exec_cmd:
            exec_cmd = _child_text(el, "Command")

    if principal is None or not has_boot:
        return "legacy"
    logon_type = _child_text(principal, "LogonType").lower()
    run_level = _child_text(principal, "RunLevel").lower()
    task_uid = _child_text(principal, "UserId").lower()
    # Windows při výchozím LeastPrivilege značku <RunLevel> v exportovaném XML vynechává
    if logon_type != "s4u" or run_level not in ("", "leastprivilege"):
        return "legacy"

    allowed_ids = set()
    if user_sid:
        allowed_ids.add(user_sid.strip().lower())
    if user_account:
        ua = user_account.strip().lower()
        allowed_ids.add(ua)
        if "\\" in ua:
            allowed_ids.add(ua.split("\\", 1)[1])
    if allowed_ids and task_uid not in allowed_ids:
        return "legacy"

    if expected_cmd:
        norm_got = os.path.normcase(os.path.normpath(exec_cmd.strip().strip('"'))) if exec_cmd else ""
        norm_exp = os.path.normcase(os.path.normpath(expected_cmd.strip().strip('"')))
        if norm_got != norm_exp:
            return "legacy"
    return "ok"


def inspect_autostart_task(base_dir: str = "", py_exe: str = "", expected_cmd: str = "") -> str:
    """Stav naplánované úlohy HBClusterWorker: 'ok' | 'legacy' | 'none' | 'unknown'."""
    if os.name != "nt":
        return "none"
    if not expected_cmd and base_dir:
        try:
            expected_cmd, _, _ = _resolve_autostart_task_exec(base_dir, py_exe)
        except Exception:
            expected_cmd = ""
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", AUTOSTART_TASK_NAME, "/XML"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=0x08000000
        )
        if r.returncode != 0:
            msg = ((r.stderr or "") + "\n" + (r.stdout or "")).lower()
            if any(k in msg for k in ("access is denied", "přístup byl odepřen", "zugriff verweigert", "0x80070005", "denied", "odepřen")):
                return "unknown"
            if any(k in msg for k in ("cannot find", "nemůže nalézt", "not exist", "neexistuje", "file specified", "uvedený soubor", "angegebene datei")):
                return "none"
            return "unknown"
        return _classify_autostart_task_xml(
            r.stdout or "",
            user_sid=_get_current_windows_user_sid(),
            user_account=_get_current_windows_user_account(),
            expected_cmd=expected_cmd,
        )
    except Exception:
        return "unknown"


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _run_schtasks_elevated(args_list: list, params_str: str, allow_uac: bool = True, hwnd=None) -> tuple:
    """Spustí schtasks.exe přímo (jako správce) nebo přes UAC (runas). Vrací (status, detail)."""
    if os.name != "nt":
        return "failed", "not windows"
    schtasks_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")
    if not os.path.isfile(schtasks_exe):
        schtasks_exe = "schtasks.exe"

    if _is_windows_process_elevated():
        try:
            res = subprocess.run(
                [schtasks_exe] + list(args_list),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20, creationflags=0x08000000
            )
            if res.returncode == 0:
                return "ok", ""
            return "failed", ((res.stderr or res.stdout or "").strip() or f"schtasks exit code {res.returncode}")[:240]
        except Exception as e:
            return "failed", str(e)[:240]

    if not allow_uac:
        return "no_admin", ""
    if _get_windows_session_id() == 0:
        return "uac_no_desktop", ""

    try:
        s32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        s32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
        s32.ShellExecuteExW.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        sei = _SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
        sei.fMask = 0x00000040 | 0x00000100   # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
        sei.hwnd = hwnd
        sei.lpVerb = "runas"
        sei.lpFile = schtasks_exe
        sei.lpParameters = params_str
        sei.nShow = 0   # SW_HIDE

        ctypes.set_last_error(0)
        ok = bool(s32.ShellExecuteExW(ctypes.byref(sei)))
        if not ok:
            err = ctypes.get_last_error()
            if err == 1223:   # ERROR_CANCELLED (uživatel klikl Ne nebo vypršel čas UAC)
                return "uac_cancelled", ""
            return "failed", f"ShellExecuteExW error {err}"
        if not sei.hProcess:
            return "failed", "no process handle"
        try:
            k32.WaitForSingleObject(sei.hProcess, 30000)
            code = wintypes.DWORD(1)
            k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
            if code.value == 0:
                return "ok", ""
            return "failed", f"schtasks exit code {code.value}"
        finally:
            k32.CloseHandle(sei.hProcess)
    except Exception as e:
        return "failed", str(e)[:240]


def _create_before_login_task(base_dir: str, py_exe: str = "", allow_uac: bool = True, on_uac_start=None, hwnd=None) -> tuple:
    user_id = _get_current_windows_user_sid() or _get_current_windows_user_account()
    if not user_id:
        return "failed", "user identity unknown"
    cmd, args, work_dir = _resolve_autostart_task_exec(base_dir, py_exe)
    xml_body = _build_before_login_task_xml(user_id, cmd, args, work_dir)
    xml_path = ""
    try:
        fd, xml_path = tempfile.mkstemp(prefix="hb_task_", suffix=".xml")
        os.close(fd)
        with open(xml_path, "w", encoding="utf-16") as xf:
            xf.write(xml_body)
        if on_uac_start and allow_uac and not _is_windows_process_elevated() and _get_windows_session_id() != 0:
            try:
                on_uac_start()
            except Exception:
                pass
        return _run_schtasks_elevated(
            ["/Create", "/TN", AUTOSTART_TASK_NAME, "/XML", xml_path, "/F"],
            f'/Create /TN "{AUTOSTART_TASK_NAME}" /XML "{xml_path}" /F',
            allow_uac=allow_uac,
            hwnd=hwnd,
        )
    except Exception as e:
        return "failed", str(e)[:240]
    finally:
        if xml_path and os.path.exists(xml_path):
            try:
                os.remove(xml_path)
            except Exception:
                pass


def _delete_before_login_task(allow_uac: bool = True, on_uac_start=None, hwnd=None) -> tuple:
    if inspect_autostart_task() == "none":
        return "ok", ""
    if on_uac_start and allow_uac and not _is_windows_process_elevated() and _get_windows_session_id() != 0:
        try:
            on_uac_start()
        except Exception:
            pass
    return _run_schtasks_elevated(
        ["/Delete", "/TN", AUTOSTART_TASK_NAME, "/F"],
        f'/Delete /TN "{AUTOSTART_TASK_NAME}" /F',
        allow_uac=allow_uac,
        hwnd=hwnd,
    )


# --- One installation per PC (2.12.0) ------------------------------------------------------------------------------
# HKCU\Software\MasekLab\HB Cluster Worker: InstallDir = folder of the installation that ran last, AppDir = its app
# folder (IPC files of the running worker are there). A second copy in another folder uses it to show the running
# worker's window and, on its first start, to offer the existing installation instead of a duplicate.
HBW_REG_PATH = r"Software\MasekLab\HB Cluster Worker"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _hbw_reg_get(name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HBW_REG_PATH, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v or "")
    except Exception:
        return ""


def _hbw_register_install(app_dir: str) -> None:
    if os.name != "nt" or wc is None:
        return
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, HBW_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "InstallDir", 0, winreg.REG_SZ, wc.win_install_root(app_dir))
            winreg.SetValueEx(k, "AppDir", 0, winreg.REG_SZ, app_dir)
            winreg.SetValueEx(k, "Version", 0, winreg.REG_SZ, str(CURRENT_VERSION))
    except Exception:
        pass


def _hbw_unregister_install(app_dir: str) -> None:
    """Uninstall: forget the record only when it belongs to this installation."""
    if os.name != "nt" or wc is None:
        return
    if not wc.win_same_dir(_hbw_reg_get("InstallDir"), wc.win_install_root(app_dir)):
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, HBW_REG_PATH)
    except Exception:
        pass


def _read_run_key_command() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, "HBClusterWorker")
            return str(v or "")
    except Exception:
        return ""


def _read_autostart_task_command() -> str:
    """Command + arguments of the scheduled task HBClusterWorker ('' = none or not readable)."""
    if os.name != "nt":
        return ""
    try:
        import html as _html
        r = subprocess.run(["schtasks", "/Query", "/TN", AUTOSTART_TASK_NAME, "/XML"], capture_output=True,
                           timeout=10, creationflags=0x08000000)
        if r.returncode != 0:
            return ""
        # schtasks prints through the console code page – decode it so that folder names with diacritics stay readable
        xml = wc.decode_console_output(r.stdout or b"") if wc else (r.stdout or b"").decode("utf-8", "replace")
        cmd = re.search(r"<Command>(.*?)</Command>", xml, re.DOTALL)
        args = re.search(r"<Arguments>(.*?)</Arguments>", xml, re.DOTALL)
        c = _html.unescape(cmd.group(1).strip()) if cmd else ""
        a = _html.unescape(args.group(1).strip()) if args else ""
        if c and not c.startswith('"'):
            c = f'"{c}"'
        return (c + " " + a).strip()
    except Exception:
        return ""


def _is_worker_install_dir(root: str) -> bool:
    return bool(root) and any(os.path.isfile(os.path.join(root, *p)) for p in
                              (("HBWorker.exe",), ("app", "worker.pyw"), ("worker.pyw",)))


def _find_other_install_root(app_dir: str) -> str:
    """Another existing installation of the worker on this PC ('' = none). Older versions did not write the registry
    record, so autostart entries pointing to another folder count as well."""
    if os.name != "nt" or wc is None:
        return ""
    my_root = wc.win_install_root(app_dir)
    cands = [_hbw_reg_get("InstallDir")]
    for cmd in (_read_run_key_command(), _read_autostart_task_command()):
        t = wc.win_cmd_worker_target(cmd)
        if t:
            cands.append(wc.win_root_from_worker_target(t))
    for c in cands:
        if c and not wc.win_same_dir(c, my_root) and _is_worker_install_dir(c):
            return c
    return ""


def _launch_install(root: str) -> bool:
    """Starts the worker of another installation (HBWorker.exe, otherwise app\\Start_Worker.bat)."""
    for p in (os.path.join(root, "HBWorker.exe"), os.path.join(root, "app", "Start_Worker.bat"),
              os.path.join(root, "Start_Worker.bat")):
        if os.path.isfile(p):
            try:
                if p.lower().endswith(".exe"):
                    subprocess.Popen([p], cwd=root, creationflags=0x00000008)          # DETACHED_PROCESS
                else:
                    subprocess.Popen(["cmd.exe", "/c", p], cwd=os.path.dirname(p), creationflags=0x08000000)
                return True
            except Exception:
                pass
    return False


def _ask_existing_install(other_root: str, t_fn) -> str:
    """First start of a second copy: 'open' (existing one) | 'here' (install here anyway) | 'cancel'."""
    res = ["cancel"]
    try:
        dlg = tk.Tk()
        dlg.title(t_fn("w_dup_install_title"))
        dlg.configure(bg="#0f172a")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        tk.Label(dlg, text=t_fn("w_dup_install_msg", other_root), bg="#0f172a", fg="#f1f5f9", font=("Segoe UI", 10),
                 justify=tk.LEFT, wraplength=520).pack(padx=20, pady=(18, 14), anchor=tk.W)
        row = tk.Frame(dlg, bg="#0f172a")
        row.pack(fill=tk.X, padx=20, pady=(0, 18))

        def _pick(v):
            res[0] = v
            dlg.destroy()

        def _btn(txt, v, bg, fg="#ffffff"):
            return tk.Button(row, text=txt, command=lambda: _pick(v), bg=bg, fg=fg, activebackground=bg,
                             activeforeground=fg, relief=tk.FLAT, font=("Segoe UI", 10, "bold"), padx=14, pady=6,
                             cursor="hand2")

        _btn(t_fn("w_dup_install_open"), "open", "#0284c7").pack(side=tk.LEFT)
        _btn(t_fn("w_dup_install_cancel"), "cancel", "#334155").pack(side=tk.RIGHT)
        _btn(t_fn("w_dup_install_here"), "here", "#1e293b", "#cbd5e1").pack(side=tk.RIGHT, padx=(0, 8))
        dlg.protocol("WM_DELETE_WINDOW", lambda: _pick("cancel"))
        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        dlg.geometry(f"+{max(0, (dlg.winfo_screenwidth() - w) // 2)}+{max(0, (dlg.winfo_screenheight() - h) // 3)}")
        dlg.mainloop()
    except Exception:
        return "here"     # no dialog possible → behave as before (install here)
    return res[0]


class _NETRESOURCEW(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
    ]


def _connect_unc_share_win32(share_root: str, username: str, password: str) -> int:
    """Připojí síťovou SMB složku přes Win32 API (heslo není v příkazovém řádku ani v seznamu procesů)."""
    if os.name != "nt" or not share_root:
        return 0
    try:
        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(_NETRESOURCEW), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        mpr.WNetAddConnection2W.restype = wintypes.DWORD
        mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
        mpr.WNetCancelConnection2W.restype = wintypes.DWORD
        nr = _NETRESOURCEW()
        nr.dwType = 1   # RESOURCETYPE_DISK
        nr.lpRemoteName = share_root
        rc = int(mpr.WNetAddConnection2W(ctypes.byref(nr), password, username, 0))
        if rc == 1219 and not os.path.isdir(share_root):
            # Zastaralé nebo kolidující spojení na stejný server (včetně IPC$) → uvolnit a připojit znovu
            try:
                mpr.WNetCancelConnection2W(share_root, 0, True)
            except Exception:
                pass
            try:
                pts = [x for x in str(share_root).replace("/", "\\").lstrip("\\").split("\\") if x]
                if pts:
                    mpr.WNetCancelConnection2W(f"\\\\{pts[0]}\\IPC$", 0, True)
            except Exception:
                pass
            rc = int(mpr.WNetAddConnection2W(ctypes.byref(nr), password, username, 0))
        return rc
    except Exception:
        return -1


def _apply_autostart_in_windows_early(base_dir: str, py_exe: str, cfg_data: dict):
    if os.name != "nt":
        return
    mode = str(cfg_data.get("autostart_mode") or "off").strip().lower()
    if mode not in ("off", "after_login", "before_login"):
        mode = "off"
    target_path, work_dir = _resolve_launcher_target_and_workdir(base_dir)
    if os.path.isfile(target_path) and target_path.lower().endswith(".exe"):
        base_cmd = f'"{target_path}"'
    else:
        pyw_p = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
        run_py = pyw_p if os.path.isfile(pyw_p) else py_exe
        script_p = os.path.join(base_dir, "worker.pyw")
        base_cmd = f'"{run_py}" "{script_p}"'
    try:
        my_root = wc.win_install_root(base_dir) if wc else ""
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if mode == "after_login":
            winreg.SetValueEx(key, "HBClusterWorker", 0, winreg.REG_SZ, f"{base_cmd} --autostart-login")
        elif mode == "before_login":
            winreg.SetValueEx(key, "HBClusterWorker", 0, winreg.REG_SZ, f"{base_cmd} --desktop-takeover")
        else:
            run_ours = (not wc or not my_root) or wc.win_ref_is_ours_or_stale(_read_run_key_command(), my_root)
            if run_ours:
                try:
                    winreg.DeleteValue(key, "HBClusterWorker")
                except FileNotFoundError:
                    pass
        winreg.CloseKey(key)
    except Exception:
        pass

    try:
        if mode == "before_login":
            _create_before_login_task(base_dir, py_exe=py_exe, allow_uac=True)
        else:
            my_root = wc.win_install_root(base_dir) if wc else ""
            task_cmd = _read_autostart_task_command()
            task_ours = (wc.win_ref_is_ours_or_stale(task_cmd, my_root) if (task_cmd and wc and my_root) else (not _find_other_install_root(base_dir)))
            if task_ours:
                _delete_before_login_task(allow_uac=False)
    except Exception:
        pass


def _probe_manager_url_stdlib(raw_url: str, timeout: float = 2.0) -> tuple:
    import urllib.request
    u = str(raw_url or "").strip().rstrip("/")
    if not u or u.lower() in ("auto", "http://auto", "https://auto", "none", "null"):
        return False, ""
    if not re.match(r"^https?://", u, re.IGNORECASE):
        u = "http://" + u.lstrip("/")
    try:
        req = urllib.request.Request(f"{u}/api/update/version", headers={"User-Agent": f"HBWorker-FirstRun/{CURRENT_VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
                ver = data.get("version") or data.get("worker_version") or "OK"
                return True, f"v{ver}" if not str(ver).startswith("v") else str(ver)
    except Exception:
        pass
    return False, ""


def _ensure_first_run_python_and_packages():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    py_exe, cfg_path, cfg_data = _resolve_and_persist_python_exe()
    missing = [
        mod for mod in ("requests", "psutil", "PIL")
        if importlib.util.find_spec(mod) is None
    ]
    is_silent_boot = _get_windows_session_id() == 0 or any(
        str(a).strip().startswith(("--bg-boot", "--autostart-login", "--desktop-takeover", "--restore-state="))
        for a in sys.argv[1:]
    )
    first_run_done = bool(cfg_data.get("first_run_completed"))

    if first_run_done and not missing:
        if not cfg_data.get("desktop_shortcut_created"):
            threading.Thread(
                target=_create_or_remove_windows_shortcuts,
                args=(base_dir, bool(cfg_data.get("desktop_shortcut", True)), bool(cfg_data.get("start_menu_shortcut", True))),
                daemon=True
            ).start()
            cfg_data["desktop_shortcut_created"] = True
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, indent=4, ensure_ascii=False)
            except Exception:
                pass
        return

    cflags = 0x08000000 if os.name == "nt" else 0
    if is_silent_boot and missing:
        try:
            subprocess.run([py_exe, "-m", "ensurepip", "--upgrade"], creationflags=cflags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            subprocess.run([py_exe, "-m", "pip", "install", "--disable-pip-version-check", "requests", "psutil", "pillow"], creationflags=cflags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        except Exception:
            pass
        importlib.invalidate_caches()
        return

    _lang_default = wc.default_ui_lang() if wc else "en"     # no language in config.json yet → language of Windows
    cur_lang = [str(cfg_data.get("lang") or cfg_data.get("ui_lang") or _lang_default).strip().lower()]
    if cur_lang[0] not in ("cs", "en"):
        cur_lang[0] = _lang_default
    if wc:
        wc.set_worker_lang(cur_lang[0])

    is_app_subfolder = os.path.basename(base_dir).lower() == "app"
    current_package_root = os.path.dirname(base_dir) if is_app_subfolder else base_dir

    def _t(k: str, *args, **kwargs) -> str:
        if wc:
            return wc.T(k, *args, **kwargs)
        return k

    other_root = ""
    if not first_run_done and not is_silent_boot:
        # Second copy on this PC? Offer the existing installation instead of a duplicate (one per PC is enough).
        try:
            other_root = _find_other_install_root(base_dir)
        except Exception:
            other_root = ""
        if other_root:
            choice = _ask_existing_install(other_root, _t)
            if choice == "open":
                if not _launch_install(other_root):
                    try:
                        _r = tk.Tk()
                        _r.withdraw()
                        messagebox.showerror(_t("w_dup_install_title"), _t("w_dup_open_failed", other_root), parent=_r)
                        _r.destroy()
                    except Exception:
                        pass
                sys.exit(0)
            if choice == "cancel":
                sys.exit(0)

    wiz = None
    try:
        wiz = tk.Tk()
        wiz.title(_t("wiz_title"))
        wiz.configure(bg="#0b0f19")
        ww, wh = 640, 640
        wiz.geometry(f"{ww}x{wh}")
        wiz.resizable(False, False)
        wiz.attributes("-topmost", True)
        wiz.after(150, lambda: (wiz.attributes("-topmost", False) if wiz and wiz.winfo_exists() else None))

        wizard_completed = [False]

        def _on_window_close():
            if not wizard_completed[0]:
                if messagebox.askyesno(_t("wiz_close_title"), _t("wiz_close_msg"), parent=wiz):
                    try:
                        wiz.destroy()
                    except Exception:
                        pass
                    sys.exit(0)
            else:
                try:
                    wiz.destroy()
                except Exception:
                    pass

        wiz.protocol("WM_DELETE_WINDOW", _on_window_close)

        try:
            ico_cands = [
                os.path.join(base_dir, "hbw.ico"),
                os.path.join(base_dir, "app", "hbw.ico"),
                os.path.join(current_package_root, "hbw.ico"),
                os.path.join(current_package_root, "app", "hbw.ico"),
                os.path.join(os.path.dirname(base_dir), "static", "hbw.ico"),
                os.path.join(current_package_root, "static", "hbw.ico"),
            ]
            for ic in ico_cands:
                if os.path.isfile(ic):
                    wiz.iconbitmap(ic)
                    break
        except Exception:
            pass
        try:
            sw = wiz.winfo_screenwidth()
            sh = wiz.winfo_screenheight()
            wiz.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2)}")
        except Exception:
            pass
        try:
            wiz.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(wiz.winfo_id()) or wiz.winfo_id()
            v = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), ctypes.sizeof(v))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:
            pass

        outer = tk.Frame(wiz, bg="#1e293b", padx=1, pady=1)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)
        card = tk.Frame(outer, bg="#111827", padx=16, pady=12)
        card.pack(fill=tk.BOTH, expand=True)

        # Header row with title and language toggle dropdown
        hdr_frame = tk.Frame(card, bg="#111827")
        hdr_frame.pack(fill=tk.X, pady=(0, 6))

        # Kompaktní rozbalovací roletka jazyka napojená na locales.js
        lang_mbtn = tk.Menubutton(
            hdr_frame,
            text=f"{cur_lang[0].upper()} ▾",
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#334155",
            activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2"
        )
        lang_menu = tk.Menu(
            lang_mbtn,
            tearoff=0,
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            font=("Segoe UI", 9)
        )
        lang_mbtn["menu"] = lang_menu

        def _populate_wiz_lang_menu():
            lang_menu.delete(0, tk.END)
            available = wc.get_available_langs() if hasattr(wc, "get_available_langs") else [
                {"code": "cs", "label": "Čeština (CZ)"},
                {"code": "en", "label": "English (EN)"}
            ]
            curr = cur_lang[0].lower()
            for l_ent in available:
                c_code = str(l_ent.get("code") or "").lower()
                c_label = str(l_ent.get("label") or c_code.upper())
                prefix = "✓  " if c_code == curr else "    "
                lang_menu.add_command(
                    label=f"{prefix}{c_label}",
                    command=lambda c=c_code: _set_language(c)
                )

        # 1. Zabalit roletku jako první prvek s pack(side="right", padx=(10, 0))
        lang_mbtn.pack(side=tk.RIGHT, padx=(10, 0))

        # 2. Teprve poté zabalit nadpis s pack(side="left", fill="x", expand=True)
        lbl_hdr_title = tk.Label(
            hdr_frame,
            text=_t("wiz_hdr_title"),
            bg="#111827", fg="#38bdf8", font=("Segoe UI", 11, "bold"), anchor="w"
        )
        lbl_hdr_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        _populate_wiz_lang_menu()

        # 1. Target installation folder
        inst_box = tk.Frame(card, bg="#0b0f19", highlightbackground="#1e293b", highlightthickness=1, padx=10, pady=6)
        inst_box.pack(fill=tk.X, pady=(0, 8))
        lbl_inst = tk.Label(
            inst_box, text=_t("wiz_inst_hdr"), bg="#0b0f19", fg="#38bdf8", font=("Segoe UI", 8, "bold"), anchor="w"
        )
        lbl_inst.pack(fill=tk.X, pady=(0, 3))

        inst_row = tk.Frame(inst_box, bg="#0b0f19")
        inst_row.pack(fill=tk.X)

        sys_drive = os.environ.get("SystemDrive") or "C:"
        if not sys_drive.endswith("\\"):
            sys_root = f"{sys_drive}\\"
        else:
            sys_root = sys_drive
        default_inst_path = os.path.join(sys_root, "HBWorker") + "\\"

        init_inst_dir = str(cfg_data.get("install_dir") or "").strip()
        if not init_inst_dir:
            init_inst_dir = default_inst_path
        if other_root and wc and (wc.win_same_dir(init_inst_dir, other_root) or wc.win_path_inside(init_inst_dir, other_root)):
            # "Install here anyway": not into the existing installation – into the folder of this package
            init_inst_dir = current_package_root if not wc.win_path_inside(current_package_root, other_root) else ""

        inst_dir_var = tk.StringVar(value=init_inst_dir)
        ent_inst = tk.Entry(
            inst_row, textvariable=inst_dir_var, bg="#090d16", fg="#f8fafc",
            insertbackground="#38bdf8", font=("Consolas", 9), relief="flat",
            highlightthickness=1, highlightbackground="#334155"
        )
        ent_inst.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3, padx=(0, 8))

        def _on_browse_inst():
            cur_p = inst_dir_var.get().strip() or default_inst_path
            browse_dir = cur_p if os.path.isdir(cur_p) else (os.path.dirname(cur_p.rstrip("\\/")) or sys_root)
            sel = filedialog.askdirectory(initialdir=browse_dir, parent=wiz)
            if sel:
                sel_p = os.path.abspath(sel)
                if not sel_p.endswith("\\"):
                    sel_p += "\\"
                inst_dir_var.set(sel_p)

        btn_browse = tk.Button(
            inst_row, text=_t("wiz_browse"), command=_on_browse_inst,
            bg="#1e293b", fg="#e2e8f0", activebackground="#334155", activeforeground="#ffffff",
            font=("Segoe UI", 8), relief="flat", padx=8, pady=2, cursor="hand2"
        )
        btn_browse.pack(side=tk.RIGHT)

        # 2. Python packages status & progress bar
        pkg_box = tk.Frame(card, bg="#0b0f19", highlightbackground="#1e293b", highlightthickness=1, padx=10, pady=6)
        pkg_box.pack(fill=tk.X, pady=(0, 8))
        lbl_pkg = tk.Label(
            pkg_box,
            text=(_t("wiz_pkg_installing") if missing else _t("wiz_pkg_ready")),
            bg="#0b0f19", fg=("#f8fafc" if missing else "#34d399"), font=("Segoe UI", 8, "bold"), anchor="w"
        )
        lbl_pkg.pack(fill=tk.X, pady=(0, 4))
        prog_canvas = tk.Canvas(pkg_box, width=560, height=8, bg="#090d16", highlightthickness=1, highlightbackground="#334155")
        prog_canvas.pack(fill=tk.X)
        prog_rect = prog_canvas.create_rectangle(0, 0, 30 if missing else 640, 8, fill="#0ea5e9" if missing else "#10b981", outline="")
        prog_canvas.bind("<Configure>", lambda e: prog_canvas.coords(prog_rect, 0, 0, e.width, 8) if not missing or pkgs_ready[0] else None)
        lbl_pkg_detail = tk.Label(
            pkg_box,
            text="",
            bg="#0b0f19", fg="#94a3b8", font=("Consolas", 8), anchor="w"
        )
        if missing:
            lbl_pkg_detail.pack(fill=tk.X, pady=(3, 0))

        # 3. Autostart radio buttons + pre-filled SMB credentials
        auto_box = tk.Frame(card, bg="#0b0f19", highlightbackground="#1e293b", highlightthickness=1, padx=10, pady=6)
        auto_box.pack(fill=tk.X, pady=(0, 8))
        lbl_auto = tk.Label(
            auto_box, text=_t("wiz_auto_hdr"), bg="#0b0f19", fg="#38bdf8", font=("Segoe UI", 8, "bold"), anchor="w"
        )
        lbl_auto.pack(fill=tk.X, pady=(0, 2))

        init_auto = str(cfg_data.get("autostart_mode") or "after_login").strip().lower()
        if init_auto not in ("off", "after_login", "before_login"):
            init_auto = "after_login"
        auto_var = tk.StringVar(value=init_auto)

        smb_frame = tk.Frame(auto_box, bg="#111827", padx=8, pady=4)
        init_smb_srv = str(cfg_data.get("smb_server") or _extract_unc_host_from_paths(cfg_data.get("inputs_dir"), cfg_data.get("outputs_dir")) or "").strip()
        init_smb_usr = str(cfg_data.get("smb_user") or cfg_data.get("smb_username") or "").strip()
        init_smb_pwd = str(cfg_data.get("smb_pass") or cfg_data.get("smb_password") or "")
        smb_srv_var = tk.StringVar(value=init_smb_srv)
        smb_usr_var = tk.StringVar(value=init_smb_usr)
        smb_pwd_var = tk.StringVar(value=init_smb_pwd)

        def _adjust_wiz_geometry():
            try:
                if not wiz or not wiz.winfo_exists():
                    return
                wiz.update_idletasks()
                req_w = max(650, wiz.winfo_reqwidth() + 8)
                req_h = max(580, wiz.winfo_reqheight() + 8)
                cur_geom = wiz.geometry()
                if "+" in cur_geom:
                    pos_part = "+" + cur_geom.split("+", 1)[1]
                    wiz.geometry(f"{req_w}x{req_h}{pos_part}")
                else:
                    sw = wiz.winfo_screenwidth()
                    sh = wiz.winfo_screenheight()
                    wiz.geometry(f"{req_w}x{req_h}+{max(0, (sw - req_w) // 2)}+{max(0, (sh - req_h) // 2)}")
            except Exception:
                pass

        def _toggle_smb_visibility():
            if auto_var.get() == "before_login":
                smb_frame.pack(fill=tk.X, pady=(4, 2))
            else:
                smb_frame.pack_forget()
            _adjust_wiz_geometry()

        rb_off = tk.Radiobutton(
            auto_box, text=_t("wiz_auto_off"), value="off", variable=auto_var, command=_toggle_smb_visibility,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        rb_off.pack(fill=tk.X, pady=1)

        rb_after = tk.Radiobutton(
            auto_box, text=_t("wiz_auto_after"), value="after_login", variable=auto_var, command=_toggle_smb_visibility,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        rb_after.pack(fill=tk.X, pady=1)

        rb_before = tk.Radiobutton(
            auto_box, text=_t("wiz_auto_before"), value="before_login", variable=auto_var, command=_toggle_smb_visibility,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        rb_before.pack(fill=tk.X, pady=1)

        lbl_smb_srv = tk.Label(smb_frame, text=_t("wiz_smb_srv"), bg="#111827", fg="#94a3b8", font=("Segoe UI", 8))
        lbl_smb_srv.grid(row=0, column=0, sticky="w")
        tk.Entry(smb_frame, textvariable=smb_srv_var, width=15, bg="#090d16", fg="#f8fafc", insertbackground="#38bdf8", relief="flat", highlightthickness=1, highlightbackground="#334155").grid(row=0, column=1, padx=(4, 8))
        lbl_smb_usr = tk.Label(smb_frame, text=_t("wiz_smb_usr"), bg="#111827", fg="#94a3b8", font=("Segoe UI", 8))
        lbl_smb_usr.grid(row=0, column=2, sticky="w")
        tk.Entry(smb_frame, textvariable=smb_usr_var, width=14, bg="#090d16", fg="#f8fafc", insertbackground="#38bdf8", relief="flat", highlightthickness=1, highlightbackground="#334155").grid(row=0, column=3, padx=(4, 8))
        lbl_smb_pwd = tk.Label(smb_frame, text=_t("wiz_smb_pwd"), bg="#111827", fg="#94a3b8", font=("Segoe UI", 8))
        lbl_smb_pwd.grid(row=0, column=4, sticky="w")
        tk.Entry(smb_frame, textvariable=smb_pwd_var, show="•", width=13, bg="#090d16", fg="#f8fafc", insertbackground="#38bdf8", relief="flat", highlightthickness=1, highlightbackground="#334155").grid(row=0, column=5, padx=(4, 0))
        _toggle_smb_visibility()

        # 4. Windows Integration Checkboxes (default enabled)
        int_box = tk.Frame(card, bg="#0b0f19", highlightbackground="#1e293b", highlightthickness=1, padx=10, pady=6)
        int_box.pack(fill=tk.X, pady=(0, 8))
        lbl_int = tk.Label(
            int_box, text=_t("wiz_int_hdr"), bg="#0b0f19", fg="#38bdf8", font=("Segoe UI", 8, "bold"), anchor="w"
        )
        lbl_int.pack(fill=tk.X, pady=(0, 2))

        desk_var = tk.BooleanVar(value=bool(cfg_data.get("desktop_shortcut", True)))
        smenu_var = tk.BooleanVar(value=bool(cfg_data.get("start_menu_shortcut", True)))
        tray_var = tk.BooleanVar(value=bool(cfg_data.get("tray_minimize", cfg_data.get("start_in_tray", True))))

        cb_desk = tk.Checkbutton(
            int_box, text=_t("wiz_desk_sc"), variable=desk_var,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        cb_desk.pack(fill=tk.X, pady=1)

        cb_smenu = tk.Checkbutton(
            int_box, text=_t("wiz_smenu_sc"), variable=smenu_var,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        cb_smenu.pack(fill=tk.X, pady=1)

        cb_tray = tk.Checkbutton(
            int_box, text=_t("wiz_tray_sc"), variable=tray_var,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        cb_tray.pack(fill=tk.X, pady=1)

        sleep_var = tk.StringVar(value=str(cfg_data.get("sleep_prevention_mode") or "encoding") if str(cfg_data.get("sleep_prevention_mode") or "encoding") in ("encoding", "always", "off") else "encoding")
        lbl_sleep = tk.Label(int_box, text=_t("w_sleep_mode_lbl"), bg="#0b0f19", fg="#94a3b8", font=("Segoe UI", 8), anchor="w")
        lbl_sleep.pack(fill=tk.X, pady=(4, 0))
        sleep_row = tk.Frame(int_box, bg="#0b0f19")
        sleep_row.pack(fill=tk.X)
        rb_sleep = []
        for _val, _key in (("encoding", "w_sleep_encoding"), ("always", "w_sleep_always"), ("off", "w_sleep_off")):
            _rb = tk.Radiobutton(
                sleep_row, text=_t(_key), value=_val, variable=sleep_var,
                bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
                activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
            )
            _rb.pack(side=tk.LEFT, padx=(0, 10))
            rb_sleep.append((_rb, _key))
        wake_var = tk.BooleanVar(value=bool(cfg_data.get("wake_check", True)))
        cb_wake = tk.Checkbutton(
            int_box, text=_t("w_wake_check"), variable=wake_var,
            bg="#0b0f19", fg="#e2e8f0", selectcolor="#0f172a", activebackground="#0b0f19",
            activeforeground="#38bdf8", font=("Segoe UI", 8), anchor="w", highlightthickness=0, bd=0
        )
        cb_wake.pack(fill=tk.X, pady=1)
        lbl_wake_block = None
        try:
            if read_wake_timers_setting() in ("disabled", "important_only"):
                # Windows nepovoluje časovače probuzení -> volba by nefungovala; vysvětlit, kde se zapíná
                wake_var.set(False)
                cb_wake.config(state="disabled")
                lbl_wake_block = tk.Label(int_box, text=_t("wrk_cap_wake_disabled"), bg="#0b0f19", fg="#fbbf24",
                                          font=("Segoe UI", 7), anchor="w", justify=tk.LEFT, wraplength=560)
                lbl_wake_block.pack(fill=tk.X, pady=(0, 2))
        except Exception:
            pass

        # 5. Manager URL with live probe indicator
        url_box = tk.Frame(card, bg="#0b0f19", highlightbackground="#1e293b", highlightthickness=1, padx=10, pady=6)
        url_box.pack(fill=tk.X, pady=(0, 10))
        url_hdr = tk.Frame(url_box, bg="#0b0f19")
        url_hdr.pack(fill=tk.X, pady=(0, 3))
        lbl_url = tk.Label(
            url_hdr, text=_t("wiz_url_hdr"), bg="#0b0f19", fg="#38bdf8", font=("Segoe UI", 8, "bold"), anchor="w"
        )
        lbl_url.pack(side=tk.LEFT, fill=tk.X, expand=True)

        init_url = str(cfg_data.get("server_url") or cfg_data.get("manager_url") or "").strip().rstrip("/")
        if init_url.lower() in ("auto", "http://auto", "https://auto", "none", "null"):
            init_url = ""
        url_var = tk.StringVar(value=init_url)

        url_row = tk.Frame(url_box, bg="#0b0f19")
        url_row.pack(fill=tk.X)
        url_ent = tk.Entry(
            url_row, textvariable=url_var, bg="#090d16", fg="#f8fafc",
            insertbackground="#38bdf8", font=("Consolas", 9), relief="flat",
            highlightthickness=1, highlightbackground="#334155"
        )
        url_ent.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3, padx=(0, 8))

        probe_state = {"status": "checking", "ver": ""}
        lbl_conn_badge = tk.Label(
            url_box,
            text=_t("wiz_probe_checking"),
            bg="#0b0f19",
            fg="#fbbf24",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
            justify="left",
            wraplength=540
        )
        lbl_conn_badge.pack(fill=tk.X, pady=(4, 0), anchor="w")

        def _trigger_url_probe(*_args):
            u_cand = url_var.get().strip()
            probe_state["status"] = "checking"
            lbl_conn_badge.config(text=_t("wiz_probe_checking"), fg="#fbbf24")

            def _bg_probe():
                target_u = u_cand
                if not target_u:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        s.settimeout(2.0)
                        s.sendto(b"HB_DISCOVER_MANAGER_V1", ("255.255.255.255", 8383))
                        data, addr = s.recvfrom(1024)
                        s.close()
                        resp = json.loads(data.decode("utf-8", errors="ignore") or "{}")
                        if resp.get("magic") == "HB_MANAGER_HERE":
                            target_u = f"http://{addr[0]}:{int(resp.get('port') or 8383)}"
                    except Exception:
                        pass
                ok, ver_s = _probe_manager_url_stdlib(target_u, timeout=2.0)

                def _apply_probe(_ok=ok, _ver=ver_s, _tu=target_u):
                    try:
                        if wiz and wiz.winfo_exists():
                            if _ok:
                                if not url_var.get().strip() and _tu:
                                    url_var.set(_tu)
                                probe_state["status"] = "ok"
                                probe_state["ver"] = _ver
                                lbl_conn_badge.config(text=_t("wiz_probe_ok", _ver), fg="#10b981")
                            else:
                                probe_state["status"] = "fail"
                                lbl_conn_badge.config(text=_t("wiz_probe_fail"), fg="#f59e0b")
                    except Exception:
                        pass

                try:
                    if wiz:
                        wiz.after(0, _apply_probe)
                except Exception:
                    pass

            threading.Thread(target=_bg_probe, daemon=True).start()

        btn_verify = tk.Button(
            url_row, text=_t("wiz_verify_btn"), command=_trigger_url_probe,
            bg="#1e293b", fg="#38bdf8", activebackground="#334155", activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2, cursor="hand2"
        )
        btn_verify.pack(side=tk.RIGHT)

        def _refresh_i18n():
            wiz.title(_t("wiz_title"))
            lbl_hdr_title.config(text=_t("wiz_hdr_title"))
            lbl_inst.config(text=_t("wiz_inst_hdr"))
            btn_browse.config(text=_t("wiz_browse"))
            if missing and not pkgs_ready[0]:
                lbl_pkg.config(text=_t("wiz_pkg_installing"))
            else:
                lbl_pkg.config(text=_t("wiz_pkg_ready"))
            lbl_auto.config(text=_t("wiz_auto_hdr"))
            rb_off.config(text=_t("wiz_auto_off"))
            rb_after.config(text=_t("wiz_auto_after"))
            rb_before.config(text=_t("wiz_auto_before"))
            lbl_smb_srv.config(text=_t("wiz_smb_srv"))
            lbl_smb_usr.config(text=_t("wiz_smb_usr"))
            lbl_smb_pwd.config(text=_t("wiz_smb_pwd"))
            lbl_int.config(text=_t("wiz_int_hdr"))
            cb_desk.config(text=_t("wiz_desk_sc"))
            cb_smenu.config(text=_t("wiz_smenu_sc"))
            cb_tray.config(text=_t("wiz_tray_sc"))
            lbl_sleep.config(text=_t("w_sleep_mode_lbl"))
            for _rb, _key in rb_sleep:
                _rb.config(text=_t(_key))
            cb_wake.config(text=_t("w_wake_check"))
            if lbl_wake_block is not None:
                lbl_wake_block.config(text=_t("wrk_cap_wake_disabled"))
            lbl_url.config(text=_t("wiz_url_hdr"))
            btn_verify.config(text=_t("wiz_verify_btn"))
            if pkgs_ready[0]:
                btn_start.config(text=_t("wiz_btn_ready"))
            else:
                btn_start.config(text=_t("wiz_btn_waiting"))
            st = probe_state.get("status")
            if st == "ok":
                lbl_conn_badge.config(text=_t("wiz_probe_ok", probe_state.get("ver", "")))
            elif st == "fail":
                lbl_conn_badge.config(text=_t("wiz_probe_fail"))
            elif st == "checking":
                lbl_conn_badge.config(text=_t("wiz_probe_checking"))
            _adjust_wiz_geometry()

        def _set_language(lang_code: str):
            cur_lang[0] = lang_code
            if wc:
                wc.set_worker_lang(lang_code)
            cfg_data["ui_lang"] = lang_code
            cfg_data["lang"] = lang_code
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, indent=4, ensure_ascii=False)
            except Exception:
                pass
            lang_mbtn.config(text=f"{lang_code.upper()} ▾")
            _populate_wiz_lang_menu()
            _refresh_i18n()

        # 6. Save & Launch button
        pkgs_ready = [not bool(missing)]

        def _on_save_and_start():
            if not pkgs_ready[0]:
                return
            raw_inst = inst_dir_var.get().strip() or default_inst_path
            chosen_dir = os.path.abspath(raw_inst)
            if other_root and wc and (wc.win_same_dir(chosen_dir, other_root) or wc.win_path_inside(chosen_dir, other_root)):
                # the existing installation lives there: installing over it would mix two copies (or fail on a locked exe)
                try:
                    messagebox.showerror(_t("wiz_title"), _t("w_dup_same_folder", other_root), parent=wiz)
                except Exception:
                    pass
                return
            moved_folder = False
            active_base_dir = base_dir
            active_cfg_path = cfg_path

            if os.path.normcase(chosen_dir) != os.path.normcase(current_package_root):
                try:
                    os.makedirs(chosen_dir, exist_ok=True)
                    if is_app_subfolder:
                        target_app = os.path.join(chosen_dir, "app")
                        os.makedirs(target_app, exist_ok=True)
                        for item in os.listdir(current_package_root):
                            if item.lower() == "app":
                                continue
                            s = os.path.join(current_package_root, item)
                            d = os.path.join(chosen_dir, item)
                            if os.path.isfile(s):
                                shutil.copy2(s, d)
                            elif os.path.isdir(s):
                                shutil.copytree(s, d, dirs_exist_ok=True)
                        for item in os.listdir(base_dir):
                            s = os.path.join(base_dir, item)
                            d = os.path.join(target_app, item)
                            if os.path.isdir(s):
                                shutil.copytree(s, d, dirs_exist_ok=True)
                            else:
                                shutil.copy2(s, d)
                        # Zkopírovat Uninstall_Worker.bat i přímo do kořene instalace (pro snadnou odinstalaci)
                        for uninst_src in (os.path.join(current_package_root, "Uninstall_Worker.bat"), os.path.join(base_dir, "Uninstall_Worker.bat")):
                            if os.path.isfile(uninst_src):
                                try:
                                    shutil.copy2(uninst_src, os.path.join(chosen_dir, "Uninstall_Worker.bat"))
                                    shutil.copy2(uninst_src, os.path.join(target_app, "Uninstall_Worker.bat"))
                                except Exception:
                                    pass
                                break
                        active_base_dir = target_app
                        active_cfg_path = os.path.join(active_base_dir, "config.json")
                    else:
                        for item in os.listdir(base_dir):
                            s = os.path.join(base_dir, item)
                            d = os.path.join(chosen_dir, item)
                            if os.path.isdir(s):
                                shutil.copytree(s, d, dirs_exist_ok=True)
                            else:
                                shutil.copy2(s, d)
                        for uninst_src in (os.path.join(current_package_root, "Uninstall_Worker.bat"), os.path.join(base_dir, "Uninstall_Worker.bat")):
                            if os.path.isfile(uninst_src):
                                try:
                                    shutil.copy2(uninst_src, os.path.join(chosen_dir, "Uninstall_Worker.bat"))
                                except Exception:
                                    pass
                                break
                        active_base_dir = chosen_dir
                        active_cfg_path = os.path.join(active_base_dir, "config.json")
                    moved_folder = True
                except Exception as err:
                    err_msg = _t("wiz_inst_err", chosen_dir, err)
                    try:
                        messagebox.showerror(_t("wiz_title"), err_msg, parent=wiz)
                    except Exception:
                        pass
                    return

            sel_auto = auto_var.get()
            if sel_auto not in ("off", "after_login", "before_login"):
                sel_auto = "after_login"
            raw_u = url_var.get().strip().rstrip("/")
            if raw_u and not re.match(r"^https?://", raw_u, re.IGNORECASE):
                raw_u = "http://" + raw_u.lstrip("/")
            if raw_u:
                cfg_data["server_url"] = raw_u
                cfg_data["manager_url"] = raw_u
            cfg_data["install_dir"] = chosen_dir
            cfg_data["autostart_mode"] = sel_auto
            cfg_data["run_on_startup"] = (sel_auto == "after_login")
            cfg_data["run_before_login"] = (sel_auto == "before_login")
            cfg_data["smb_server"] = smb_srv_var.get().strip()
            cfg_data["smb_user"] = smb_usr_var.get().strip()
            cfg_data["smb_pass"] = smb_pwd_var.get()
            cfg_data["smb_username"] = cfg_data["smb_user"]
            cfg_data["smb_password"] = cfg_data["smb_pass"]
            cfg_data["desktop_shortcut"] = bool(desk_var.get())
            cfg_data["start_menu_shortcut"] = bool(smenu_var.get())
            cfg_data["tray_minimize"] = bool(tray_var.get())
            cfg_data["start_in_tray"] = False
            cfg_data["sleep_prevention_mode"] = sleep_var.get() if sleep_var.get() in ("encoding", "always", "off") else "encoding"
            cfg_data["wake_check"] = bool(wake_var.get())
            cfg_data["desktop_shortcut_created"] = True
            cfg_data["first_run_completed"] = True
            cfg_data["first_run_just_completed"] = True
            cfg_data["lang"] = cur_lang[0]
            cfg_data["ui_lang"] = cur_lang[0]

            try:
                with open(active_cfg_path, "w", encoding="utf-8") as wf:
                    json.dump(cfg_data, wf, indent=4, ensure_ascii=False)
            except Exception:
                pass

            _create_or_remove_windows_shortcuts(active_base_dir, cfg_data["desktop_shortcut"], cfg_data["start_menu_shortcut"])
            _apply_autostart_in_windows_early(active_base_dir, py_exe, dict(cfg_data))

            wizard_completed[0] = True

            if moved_folder:
                try:
                    target_exe, target_workdir = _resolve_launcher_target_and_workdir(active_base_dir)
                    cflags = 0x08000000 if os.name == "nt" else 0
                    if os.path.isfile(target_exe) and target_exe.lower().endswith(".exe"):
                        subprocess.Popen([target_exe], cwd=target_workdir, creationflags=cflags)
                    else:
                        target_worker_script = os.path.join(active_base_dir, "worker.pyw")
                        pyw_cand = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
                        spawn_exe = pyw_cand if os.path.isfile(pyw_cand) else py_exe
                        subprocess.Popen([spawn_exe, target_worker_script], cwd=active_base_dir, creationflags=cflags)
                    try:
                        wiz.destroy()
                    except Exception:
                        pass
                    sys.exit(0)
                except Exception:
                    pass

            try:
                wiz.destroy()
            except Exception:
                pass

        btn_start = tk.Button(
            card,
            text=(_t("wiz_btn_waiting") if missing else _t("wiz_btn_ready")),
            command=_on_save_and_start,
            state=(tk.DISABLED if missing else tk.NORMAL),
            bg=("#334155" if missing else "#10b981"),
            fg="#ffffff",
            activebackground="#059669",
            activeforeground="#ffffff",
            disabledforeground="#94a3b8",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            pady=8,
            cursor="hand2"
        )
        btn_start.pack(fill=tk.X, side=tk.BOTTOM)

        _set_language(cur_lang[0])
        _trigger_url_probe()

        if missing:
            done_evt = threading.Event()
            step_state = {"pct": 12, "msg": ""}

            def _install_worker():
                try:
                    step_state["pct"] = 20
                    step_state["msg"] = "ensurepip --upgrade..."
                    subprocess.run(
                        [py_exe, "-m", "ensurepip", "--upgrade"],
                        creationflags=cflags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
                    )
                except Exception:
                    pass
                try:
                    step_state["pct"] = 35
                    step_state["msg"] = "pip install requests psutil pillow..."
                    pip_cmd = [py_exe, "-m", "pip", "install", "--disable-pip-version-check", "requests", "psutil", "pillow"]
                    proc = subprocess.Popen(
                        pip_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8",
                        errors="replace",
                        creationflags=cflags
                    )
                    for raw_line in proc.stdout:
                        line = raw_line.strip()
                        if line:
                            if len(line) > 75:
                                line = line[:72] + "..."
                            step_state["msg"] = line
                            if "Collecting" in line:
                                step_state["pct"] = min(80, step_state.get("pct", 35) + 10)
                            elif "Downloading" in line:
                                step_state["pct"] = min(85, step_state.get("pct", 35) + 5)
                            elif "Installing" in line:
                                step_state["pct"] = 90
                    proc.wait(timeout=300)
                    if proc.returncode != 0:
                        proc2 = subprocess.Popen(
                            [py_exe, "-m", "pip", "install", "--user", "--disable-pip-version-check", "requests", "psutil", "pillow"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8",
                            errors="replace",
                            creationflags=cflags
                        )
                        for raw_line in proc2.stdout:
                            line = raw_line.strip()
                            if line:
                                if len(line) > 75:
                                    line = line[:72] + "..."
                                step_state["msg"] = line
                        proc2.wait(timeout=300)
                except Exception as ex:
                    step_state["msg"] = str(ex)
                step_state["pct"] = 100
                done_evt.set()

            threading.Thread(target=_install_worker, daemon=True).start()
            anim_pct = 10.0

            def _tick_pkg_install():
                nonlocal anim_pct
                if not wiz or not wiz.winfo_exists():
                    return
                if not done_evt.is_set():
                    target_p = float(step_state.get("pct", 12))
                    if anim_pct < target_p:
                        anim_pct = min(target_p, anim_pct + 2.0)
                    elif anim_pct < 92.0:
                        anim_pct += 0.35
                    full_w = max(510.0, float(prog_canvas.winfo_width() or 560.0))
                    w_px = int(max(20.0, min(full_w, (anim_pct / 100.0) * full_w)))
                    prog_canvas.coords(prog_rect, 0, 0, w_px, 8)
                    cur_msg = step_state.get("msg", "")
                    if cur_msg:
                        try:
                            lbl_pkg_detail.config(text=cur_msg)
                        except Exception:
                            pass
                    wiz.after(50, _tick_pkg_install)
                else:
                    importlib.invalidate_caches()
                    pkgs_ready[0] = True
                    full_w = max(510, int(prog_canvas.winfo_width() or 560))
                    prog_canvas.coords(prog_rect, 0, 0, full_w, 8)
                    prog_canvas.itemconfig(prog_rect, fill="#10b981")
                    lbl_pkg.config(
                        text=_t("wiz_pkg_ready"),
                        fg="#34d399"
                    )
                    try:
                        lbl_pkg_detail.pack_forget()
                    except Exception:
                        pass
                    btn_start.config(
                        text=_t("wiz_btn_ready"),
                        state=tk.NORMAL,
                        bg="#10b981"
                    )
                    _adjust_wiz_geometry()
                    if first_run_done:
                        wizard_completed[0] = True
                        wiz.after(300, wiz.destroy)

            wiz.after(50, _tick_pkg_install)

        wiz.mainloop()
    except Exception:
        if wiz is not None:
            try:
                wiz.destroy()
            except Exception:
                pass
    importlib.invalidate_caches()


def _selftest_ensure_packages():
    """--selftest: chybějící balíčky doinstalovat potichu (stejně jako start po aktualizaci), bez průvodce."""
    missing = [mod for mod in ("requests", "psutil", "PIL") if importlib.util.find_spec(mod) is None]
    if missing:
        py_exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if not os.path.isfile(py_exe):
            py_exe = sys.executable
        try:
            subprocess.run([py_exe, "-m", "pip", "install", "--disable-pip-version-check", "requests", "psutil", "pillow"],
                           creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        except Exception:
            pass
        importlib.invalidate_caches()


if SELFTEST:
    _selftest_ensure_packages()
else:
    _ensure_first_run_python_and_packages()

import requests
try:
    import psutil
except Exception:
    psutil = None

try:
    from PIL import Image, ImageDraw, ImageTk
    _HAS_PIL_TK = True
except Exception:
    Image = ImageDraw = ImageTk = None
    _HAS_PIL_TK = False

UDP_DISCOVERY_PORT = 8383
KOFI_URL = "https://ko-fi.com/maseklab"

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MasekLab.HBCluster.Worker")
except Exception:
    pass

if getattr(sys, 'frozen', False):
    APP_ROOT_PATH = os.path.dirname(sys.executable)
else:
    APP_ROOT_PATH = os.path.dirname(os.path.abspath(__file__))

BIN_DIR = os.path.join(APP_ROOT_PATH, "bin")
if not SELFTEST and BIN_DIR:
    _cur_path = os.environ.get("PATH", "")
    if BIN_DIR.lower() not in [p.strip().lower() for p in _cur_path.split(os.pathsep) if p.strip()]:
        os.environ["PATH"] = BIN_DIR + (os.pathsep + _cur_path if _cur_path else "")
GEN_MARKER_FILE = os.path.join(BIN_DIR, ".binary_generation")
CORE_FILE_PATH = os.path.join(APP_ROOT_PATH, "worker_core.py")

CONFIG_FILE = os.path.join(APP_ROOT_PATH, "config.json")
FALLBACK_CONFIG_FILE = os.path.join(APP_ROOT_PATH, "worker_config.json")
IPC_STATE_FILE = os.path.join(APP_ROOT_PATH, ".worker_instance.json")
IPC_CMD_FILE = os.path.join(APP_ROOT_PATH, ".worker_ipc_cmd.json")


def get_current_windows_session_id() -> int:
    return _get_windows_session_id()


def acquire_worker_single_instance_mutex():
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        for m_name in ("Global\\HBClusterWorkerSingleInstance", "Local\\HBClusterWorkerSingleInstance"):
            ctypes.set_last_error(0)
            h = k32.CreateMutexW(None, False, m_name)
            if h:
                already = (ctypes.get_last_error() == 183)
                return h, already
    except Exception:
        pass
    return None, False


def _running_worker_app_dir(own_app_dir: str) -> str:
    """App folder of the worker that holds the single-instance mutex: the registry AppDir when a worker from that
    folder is alive (its .worker_instance.json names a running PID), otherwise this copy's folder."""
    reg_app = _hbw_reg_get("AppDir")
    if not reg_app or wc is None or wc.win_same_dir(reg_app, own_app_dir):
        return own_app_dir
    try:
        with open(os.path.join(reg_app, os.path.basename(IPC_STATE_FILE)), "r", encoding="utf-8") as rf:
            pid = int((json.load(rf) or {}).get("pid") or 0)
        if pid and _is_windows_pid_alive(pid):
            return reg_app
    except Exception:
        pass
    return own_app_dir


def _show_other_copy_running(other_root: str) -> None:
    try:
        if wc is not None:
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as cf:
                    lang = str((json.load(cf) or {}).get("lang") or "").strip().lower()
                if lang in ("cs", "en"):
                    wc.set_worker_lang(lang)
            except Exception:
                pass
        t_fn = wc.T if wc is not None else (lambda k, *a: k)
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(t_fn("w_dup_install_title"), t_fn("w_dup_running_msg", other_root), parent=r)
        r.destroy()
    except Exception:
        pass


def unblock_smartscreen_files():
    """Odstraní NTFS ADS stream :Zone.Identifier ze všech souborů workeru a binárek."""
    try:
        del_fn = ctypes.windll.kernel32.DeleteFileW
        del_fn.argtypes = [wintypes.LPCWSTR]
        del_fn.restype = wintypes.BOOL
        for scan_dir in (APP_ROOT_PATH, BIN_DIR):
            if os.path.isdir(scan_dir):
                for fn in os.listdir(scan_dir):
                    fp = os.path.join(scan_dir, fn)
                    if os.path.isfile(fp):
                        try:
                            del_fn(f"{fp}:Zone.Identifier")
                        except Exception:
                            pass
    except Exception:
        pass


if not SELFTEST:
    unblock_smartscreen_files()

_LEGACY_HB_TEMP = os.path.join(APP_ROOT_PATH, "HB_Temp")
LOCAL_TEMP_DIR = _LEGACY_HB_TEMP if os.path.isdir(_LEGACY_HB_TEMP) else os.path.join(APP_ROOT_PATH, "temp")
for _cfg_candidate in (CONFIG_FILE, FALLBACK_CONFIG_FILE):
    if os.path.isfile(_cfg_candidate):
        try:
            with open(_cfg_candidate, "r", encoding="utf-8-sig") as _cf:
                _cd = json.load(_cf)
                if isinstance(_cd, dict):
                    _t_val = str(_cd.get("temp_dir") or _cd.get("local_temp_dir") or "").strip()
                    if _t_val:
                        LOCAL_TEMP_DIR = _t_val
            break
        except Exception:
            pass
try:
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
except Exception:
    pass
LOG_FILE = os.path.join(APP_ROOT_PATH, "cluster_debug.log")
MAX_LOG_BYTES = 2 * 1024 * 1024
KEEP_LOG_LINES = 5000

LOCAL_HB_EXE = os.path.join(BIN_DIR, "HandBrakeCLI.exe")
LOCAL_FFMPEG_EXE = os.path.join(BIN_DIR, "ffmpeg.exe")
LOCAL_FFPROBE_EXE = os.path.join(BIN_DIR, "ffprobe.exe")
LOCAL_MKVPROPEDIT_EXE = os.path.join(BIN_DIR, "mkvpropedit.exe")
LOCAL_MKVMERGE_EXE = os.path.join(BIN_DIR, "mkvmerge.exe")
LOCAL_7Z_EXE = os.path.join(BIN_DIR, "7z.exe")
LOCAL_7Z_DLL = os.path.join(BIN_DIR, "7z.dll")
LEGACY_7ZA_EXE = os.path.join(BIN_DIR, "7za.exe")
LOCAL_LAUNCHER_EXE = (
    os.path.join(os.path.dirname(APP_ROOT_PATH), "HBWorker.exe")
    if os.path.basename(APP_ROOT_PATH).lower() == "app"
    else os.path.join(APP_ROOT_PATH, "HBWorker.exe")
)

MARKER_HB_MANAGER = os.path.join(BIN_DIR, ".hbcli_from_manager")
MARKER_FFMPEG_MANAGER = os.path.join(BIN_DIR, ".ffmpeg_from_manager")
MARKER_FFPROBE_MANAGER = os.path.join(BIN_DIR, ".ffprobe_from_manager")
MARKER_MKVPROPEDIT_MANAGER = os.path.join(BIN_DIR, ".mkvpropedit_from_manager")
MARKER_MKVMERGE_MANAGER = os.path.join(BIN_DIR, ".mkvmerge_from_manager")
MARKER_7Z_MANAGER = os.path.join(BIN_DIR, ".7z_from_manager")
MARKER_7Z_DLL_MANAGER = os.path.join(BIN_DIR, ".7z_dll_from_manager")
MARKER_LAUNCHER_MANAGER = os.path.join(BIN_DIR, ".launcher_from_manager")

HANDBRAKE_CLI = "HandBrakeCLI.exe"
FFMPEG_CLI = "ffmpeg.exe"
FFPROBE_CLI = "ffprobe.exe"
MKVPROPEDIT_CLI = None
MKVMERGE_CLI = None
SEVENZIP_CLI = None

HB_BINARY_SOURCE = "Manager (bin/)"
FFMPEG_BINARY_SOURCE = "Manager (bin/)"

_bin_probe_cache = {}
log_file_lock = threading.Lock()
global_log_buffer = None

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

IS_AMD_CPU = "authenticamd" in os.environ.get("PROCESSOR_IDENTIFIER", "").lower()

SLEEP_GRACE_SEC = 1800   # režim spánku „Během převodu“: PC zůstane vzhůru ještě 30 min po poslední úloze


def render_tray_status_icon(bg_hex: str, border_hex: str, symbol_str: str, size: int = 32):
    """Deleguje vykreslení plnoplošné ikony (bez nožiček, 4x supersampling) na sdílené jádro worker_core."""
    return wc.render_tray_status_icon(bg_hex, border_hex, symbol_str, size)


def write_file_log(msg):
    ts_line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if global_log_buffer is not None:
        global_log_buffer.append(ts_line)
    with log_file_lock:
        try:
            if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
                with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as rf:
                    lns = rf.readlines()
                if len(lns) > KEEP_LOG_LINES:
                    with open(LOG_FILE, "w", encoding="utf-8") as wf:
                        wf.writelines(lns[-KEEP_LOG_LINES:])
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(ts_line + "\n")
        except Exception:
            pass


def _bootstrap_udp_discover(port: int = UDP_DISCOVERY_PORT, timeout: float = 3.0) -> str:
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


def _get_bootstrap_server_url() -> str:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
                srv = str(d.get("server_url") or d.get("manager_url") or "").strip().rstrip("/")
                if srv and srv.lower() not in ("auto", "http://auto", "https://auto", "none", "null"):
                    return srv
        except Exception:
            pass
    return os.getenv("MANAGER_URL", "http://localhost:8383").rstrip("/")


def _bootstrap_bound_manager_ok(resp) -> bool:
    """Před načtením worker_core: odpověď Manageru s jiným ID, než je v manager_binding.json, nepřijmout."""
    try:
        with open(os.path.join(APP_ROOT_PATH, "manager_binding.json"), "r", encoding="utf-8") as f:
            bound = str((json.load(f) or {}).get("manager_id") or "")
    except Exception:
        return True
    return (not bound) or (resp.headers.get("X-HB-Manager-Id") or "") == bound


def _bootstrap_fetch_core(srv: str) -> str:
    """worker_core.py z Manageru podle OTA manifestu (kontrola SHA-256). "" = nepovedlo se."""
    import hashlib
    rm = requests.get(f"{srv}/api/update/manifest?target=windows", headers={"Connection": "close"}, timeout=10)
    if rm.status_code != 200 or not _bootstrap_bound_manager_ok(rm):
        return ""
    ent = next((e for e in ((rm.json() or {}).get("files") or []) if isinstance(e, dict) and e.get("name") == "worker_core.py"), None)
    if not ent:
        return ""
    r = requests.get(f"{srv}/api/update/file/worker_core.py", headers={"Connection": "close"}, timeout=20)
    if r.status_code != 200 or not _bootstrap_bound_manager_ok(r):
        return ""
    if hashlib.sha256(r.content).hexdigest() != str(ent.get("sha256") or "").strip().lower():
        return ""
    return r.content.decode("utf-8-sig").lstrip("\ufeff").replace("\r\n", "\n")


def _bootstrap_fetch_locales(srv: str, loc_p: str):
    try:
        rl = requests.get(f"{srv}/locales.js", headers={"Connection": "close"}, timeout=8)
        if rl.status_code == 200 and len(rl.content) > 500 and _bootstrap_bound_manager_ok(rl):
            with open(loc_p, "w", encoding="utf-8", newline="\n") as wf:
                wf.write(rl.content.decode("utf-8-sig").lstrip("\ufeff").replace("\r\n", "\n"))
    except Exception:
        pass


def _ota_recover_interrupted_update() -> bool:
    """O1: výpadek proudu (nebo zamčený soubor) během prohazování v update_worker.bat nechá směs verzí.
    Podle otisků v update_attempt.json se to pozná: vrátí se celá předchozí verze ze záloh *.prev
    a worker se spustí znovu. Vrací True, když se obnovovalo (proces se pak sám ukončí)."""
    import hashlib
    try:
        with open(os.path.join(APP_ROOT_PATH, "update_attempt.json"), "r", encoding="utf-8") as f:
            rec = json.load(f)
        files = rec.get("files") if isinstance(rec, dict) else None
    except Exception:
        return False
    if not isinstance(files, dict) or not files or rec.get("settled"):
        return False      # některá verze už po pokusu normálně naběhla – ruční změny souborů nevracet

    def _sha(p):
        try:
            with open(p, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return ""

    cur = {n: _sha(os.path.join(APP_ROOT_PATH, n)) for n in files}
    if all(cur[n] == str((files[n] or {}).get("new") or "") for n in files):
        return False      # celá nová verze – v pořádku
    if all(cur[n] == str((files[n] or {}).get("prev") or "") for n in files):
        return False      # celá předchozí verze (aktualizace neproběhla / byla vrácena)
    for n, ent in files.items():
        prev_sha = str((ent or {}).get("prev") or "")
        if cur[n] != prev_sha and (not prev_sha or _sha(os.path.join(APP_ROOT_PATH, n + ".prev")) != prev_sha):
            write_file_log(f"[OTA] Mixed versions after an interrupted update, but backup {n}.prev is missing - leaving as is.")
            return False
    for n, ent in files.items():
        dst = os.path.join(APP_ROOT_PATH, n)
        try:
            if cur[n] != str((ent or {}).get("prev") or ""):
                shutil.copyfile(dst + ".prev", dst)
            if os.path.exists(dst + ".tmp"):
                os.remove(dst + ".tmp")
        except Exception as e:
            write_file_log(f"[OTA] Restoring {n} failed: {e}")
            return False
    if os.path.basename(APP_ROOT_PATH).lower() == "app":
        try:
            shutil.copyfile(os.path.join(APP_ROOT_PATH, "VERSION"), os.path.join(os.path.dirname(APP_ROOT_PATH), "VERSION"))
        except Exception:
            pass
    write_file_log(f"[OTA] Update to {rec.get('to')} was interrupted (mixed files) - previous version {rec.get('from')} restored, restarting.")
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:] if a != "--selftest"],
                         cwd=APP_ROOT_PATH, creationflags=0x08000000)
    except Exception:
        return False
    return True


def ensure_worker_core_module():
    """worker_core.py je součástí balíčku (app/). Jen když chybí nebo je poškozený, stáhne se z Manageru podle
    OTA manifestu (SHA-256). Dřívější porovnání literálu verze kvůli workerům <= 2.11.4ZH je zrušené (O3)."""
    os.makedirs(BIN_DIR, exist_ok=True)
    loc_p = os.path.join(APP_ROOT_PATH, "locales.js")
    while True:
        if os.path.isfile(CORE_FILE_PATH):
            try:
                with open(CORE_FILE_PATH, "r", encoding="utf-8-sig") as f:
                    compile(f.read().lstrip("\ufeff").replace("\r\n", "\n"), "worker_core.py", "exec")
                if not os.path.isfile(loc_p) and not SELFTEST:
                    _bootstrap_fetch_locales(_get_bootstrap_server_url(), loc_p)
                return
            except Exception as e:
                if SELFTEST:
                    raise
                write_file_log(f"worker_core.py is damaged ({e}) - downloading it from Manager...")
        elif SELFTEST:
            raise RuntimeError("worker_core.py is missing")
        srv = _get_bootstrap_server_url()
        try:
            write_file_log(f"Downloading shared core worker_core.py from {srv} ...")
            c_txt = _bootstrap_fetch_core(srv)
            if len(c_txt) > 1000:
                compile(c_txt, "worker_core.py", "exec")
                tmp_c = CORE_FILE_PATH + ".tmp"
                with open(tmp_c, "w", encoding="utf-8", newline="\n") as wf:
                    wf.write(c_txt)
                os.replace(tmp_c, CORE_FILE_PATH)
                _bootstrap_fetch_locales(srv, loc_p)
                write_file_log("Module worker_core.py verified (SHA-256) and saved.")
                return
            write_file_log("Manager did not provide a valid worker_core.py yet.")
        except Exception as e:
            disc = _bootstrap_udp_discover()
            if disc and disc != srv:
                try:
                    cfg = {}
                    if os.path.isfile(CONFIG_FILE):
                        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as rf:
                            cfg = json.load(rf)
                    cfg["server_url"] = disc
                    with open(CONFIG_FILE, "w", encoding="utf-8") as wf:
                        json.dump(cfg, wf, indent=4)
                    write_file_log(f"UDP Discovery found Manager at {disc}")
                    continue
                except Exception:
                    pass
            write_file_log(f"Waiting for Manager ({e})...")
        time.sleep(5)


if not SELFTEST and _ota_recover_interrupted_update():
    os._exit(0)
ensure_worker_core_module()
if "worker_core" in sys.modules:
    wc = importlib.reload(sys.modules["worker_core"])
else:
    import worker_core as wc

global_log_buffer = wc.WorkerLogBuffer(maxlen=250)

# O1: jak dopadla poslední aktualizace (app/update_attempt.json). Nová verze hned zapíše PID (kdyby do 2 min
# nenaběhla, update_worker.bat ji ukončí) a po startu okna ota_ok.txt („naběhl jsem“).
OTA_START_STATE, OTA_START_REC = ("none", {}) if SELFTEST else wc.evaluate_ota_attempt(APP_ROOT_PATH, CURRENT_VERSION)
if OTA_START_STATE in ("updated", "failed") and not OTA_START_REC.get("settled"):
    wc.mark_ota_attempt_settled(APP_ROOT_PATH, OTA_START_REC)   # obnova po přerušení už se pak nespustí
if OTA_START_STATE == "updated":
    try:
        with open(os.path.join(APP_ROOT_PATH, wc.OTA_PID_FILE), "w", encoding="utf-8") as _pf:
            _pf.write(f"{os.getpid()} {os.path.basename(sys.executable)}\n")
    except Exception:
        pass
elif OTA_START_STATE == "stale":
    wc.clear_ota_attempt(APP_ROOT_PATH)

# Složka Temp: důkladně se uklízí jen složka se značkou HB; vlastní Temp s cizími soubory → podsložka HB_Temp
try:
    _prep_tmp = None if SELFTEST else getattr(wc, "prepare_worker_temp_dir", None)
    if _prep_tmp:
        _tmp_ready = _prep_tmp(LOCAL_TEMP_DIR, (os.path.join(APP_ROOT_PATH, "temp"), _LEGACY_HB_TEMP), log_fn=write_file_log)
        if _tmp_ready:
            LOCAL_TEMP_DIR = _tmp_ready
except Exception:
    pass


def apply_windows_dark_titlebar(tk_win):
    try:
        tk_win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(tk_win.winfo_id()) or tk_win.winfo_id()
        dark_v = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_v), ctypes.sizeof(dark_v))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(dark_v), ctypes.sizeof(dark_v))
        cap_col = ctypes.c_int(0x00160D09)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap_col), ctypes.sizeof(cap_col))
        txt_col = ctypes.c_int(0x00F9F5F1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt_col), ctypes.sizeof(txt_col))
    except Exception:
        pass


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", wintypes.c_size_t if hasattr(wintypes, 'c_size_t') else ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


class REASON_CONTEXT(ctypes.Structure):
    # Reason union má ve Win32 velikost max(LPWSTR, Detailed{HMODULE, ULONG, ULONG, LPWSTR*}) = 32 B na x64.
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("SimpleReasonString", wintypes.LPWSTR),
        ("_reserved_union_pad", ctypes.c_byte * 24),
    ]


_power_request_handle = None
_power_request_active = False


def set_keep_awake(enable=True):
    """
    Zabrání uspání systému (System / Execution Required), ale NIKDY nepoužívá
    ES_DISPLAY_REQUIRED (0x00000002) ani PowerRequestDisplayRequired (0),
    aby se monitor mohl normálně vypnout podle nastavení napájení ve Windows.
    """
    global _power_request_handle, _power_request_active
    try:
        k32 = ctypes.windll.kernel32
        if enable:
            k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if _power_request_handle is None:
                rc = REASON_CONTEXT(0, 0x00000001, "HB Cluster Worker - active node")
                k32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
                k32.PowerCreateRequest.restype = wintypes.HANDLE
                h_req = k32.PowerCreateRequest(ctypes.byref(rc))
                # Při chybě vrací INVALID_HANDLE_VALUE (-1), ne NULL -> takový handle nepoužívat.
                invalid_handle = ctypes.c_void_p(-1).value
                _power_request_handle = h_req if (h_req and h_req != invalid_handle) else None
            if _power_request_handle and not _power_request_active:
                k32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
                # Pouze PowerRequestSystemRequired (1) a PowerRequestExecutionRequired (3);
                # NIKDY PowerRequestDisplayRequired (0)!
                k32.PowerSetRequest(_power_request_handle, 1)
                k32.PowerSetRequest(_power_request_handle, 3)
                _power_request_active = True
        else:
            k32.SetThreadExecutionState(ES_CONTINUOUS)
            if _power_request_handle and _power_request_active:
                k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
                for p_type in (0, 1, 2, 3):
                    try:
                        k32.PowerClearRequest(_power_request_handle, p_type)
                    except Exception:
                        pass
                _power_request_active = False
    except Exception:
        pass


_wake_timer_handle = None
_wake_timer_armed = False
WAKE_CHECK_MINUTES = 30    # „kontrolní buzení“: uspané PC se po této době samo probudí a zeptá se Manageru na úlohy


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def is_on_ac_power() -> bool:
    """True = napájení ze sítě (nebo stolní PC bez baterie). Na baterii PC kontrolně nebudíme."""
    try:
        st = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return st.ACLineStatus != 0   # 0 = baterie, 1 = síť, 255 = neznámo (stolní PC)
    except Exception:
        pass
    return True


def arm_wake_timer(minutes: int = WAKE_CHECK_MINUTES) -> bool:
    """Nastaví (posune) časovač, který uspané PC za `minutes` minut probudí. Dokud PC běží, posouvá se dál,
    takže probouzí jen skutečně uspaný počítač. Windows ho poslechne, jen když jsou v napájení povolené časovače probuzení."""
    global _wake_timer_handle, _wake_timer_armed
    try:
        k32 = ctypes.windll.kernel32
        if _wake_timer_handle is None:
            k32.CreateWaitableTimerW.restype = wintypes.HANDLE
            k32.CreateWaitableTimerW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            _wake_timer_handle = k32.CreateWaitableTimerW(None, True, None) or None
        if not _wake_timer_handle:
            return False
        due = ctypes.c_longlong(-int(max(1, int(minutes)) * 60 * 10_000_000))   # relativně, jednotky 100 ns
        k32.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        ok = bool(k32.SetWaitableTimer(_wake_timer_handle, ctypes.byref(due), 0, None, None, True))
        _wake_timer_armed = ok
        return ok
    except Exception:
        return False


def cancel_wake_timer():
    global _wake_timer_armed
    try:
        if _wake_timer_handle and _wake_timer_armed:
            k32 = ctypes.windll.kernel32
            k32.CancelWaitableTimer.argtypes = [wintypes.HANDLE]
            k32.CancelWaitableTimer.restype = wintypes.BOOL
            k32.CancelWaitableTimer(_wake_timer_handle)
    except Exception:
        pass
    _wake_timer_armed = False


# _GUID / _guid / read_wake_timers_setting jsou definovány v horní části souboru (využívá je i průvodce prvním spuštěním).


def read_power_capabilities() -> dict:
    """Moderní úsporný režim (Modern Standby / S0) a přítomnost baterie – z SYSTEM_POWER_CAPABILITIES."""
    out = {"modern_standby": False, "s3": True, "has_battery": False}
    try:
        buf = (ctypes.c_ubyte * 256)()
        rc = ctypes.windll.powrprof.CallNtPowerInformation(4, None, 0, ctypes.byref(buf), ctypes.sizeof(buf))   # 4 = SystemPowerCapabilities
        if rc == 0:
            out["s3"] = bool(buf[5])
            out["modern_standby"] = bool(buf[20])     # AoAc
    except Exception:
        pass
    try:
        st = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            out["has_battery"] = not bool(st.BatteryFlag & 128)   # 128 = bez systémové baterie
    except Exception:
        pass
    return out


def windows_user_can_elevate() -> bool:
    """Je přihlášený uživatel ve skupině Administrators (i když proces neběží jako správce)?"""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(["whoami", "/groups"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=8, creationflags=CREATE_NO_WINDOW)
        return "S-1-5-32-544" in (r.stdout or "")
    except Exception:
        return False


def run_windows_privilege_selftest(base_dir: str = "", py_exe: str = "") -> dict:
    """W3: kontrola práv při startu – co worker s aktuálními právy opravdu umí. Nic trvale nemění: priorita
    a afinita vlastního procesu se nastaví a hned vrátí, budicí časovač a požadavek proti spánku se hned zruší."""
    out = {"admin": False, "priority": "fail", "affinity": "fail", "wake_timer": "fail", "keep_awake": "fail",
           "task": "unknown", "ts": int(time.time())}
    try:
        out["admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        hp = k32.GetCurrentProcess()
        try:
            k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
            k32.GetPriorityClass.restype = wintypes.DWORD
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.SetPriorityClass.restype = wintypes.BOOL
            orig = k32.GetPriorityClass(hp) or 0x00000020
            ok = bool(k32.SetPriorityClass(hp, 0x00004000))          # BELOW_NORMAL (jako HandBrakeCLI)
            k32.SetPriorityClass(hp, orig)
            out["priority"] = "ok" if ok else "fail"
        except Exception:
            pass
        try:
            pm, sm = ctypes.c_size_t(0), ctypes.c_size_t(0)
            k32.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
            k32.GetProcessAffinityMask.restype = wintypes.BOOL
            k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
            k32.SetProcessAffinityMask.restype = wintypes.BOOL
            if k32.GetProcessAffinityMask(hp, ctypes.byref(pm), ctypes.byref(sm)) and pm.value:
                out["affinity"] = "ok" if k32.SetProcessAffinityMask(hp, pm.value) else "fail"
        except Exception:
            pass
        try:
            k32.CreateWaitableTimerW.restype = wintypes.HANDLE
            k32.CreateWaitableTimerW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            k32.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                                             ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
            k32.SetWaitableTimer.restype = wintypes.BOOL
            k32.CancelWaitableTimer.argtypes = [wintypes.HANDLE]
            ht = k32.CreateWaitableTimerW(None, True, None)
            if ht:
                due = ctypes.c_longlong(-24 * 3600 * 10_000_000)          # za 24 h, hned se zruší
                ctypes.set_last_error(0)
                ok = bool(k32.SetWaitableTimer(ht, ctypes.byref(due), 0, None, None, True))
                err = ctypes.get_last_error()
                out["wake_timer"] = "unsupported" if (ok and err == 50) else ("ok" if ok else "fail")
                k32.CancelWaitableTimer(ht)
                k32.CloseHandle(ht)
        except Exception:
            pass
        try:
            rc = REASON_CONTEXT(0, 0x00000001, "HB Cluster Worker - rights check")
            k32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
            k32.PowerCreateRequest.restype = wintypes.HANDLE
            k32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
            k32.PowerSetRequest.restype = wintypes.BOOL
            k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
            hr = k32.PowerCreateRequest(ctypes.byref(rc))
            if hr and hr != ctypes.c_void_p(-1).value:
                out["keep_awake"] = "ok" if k32.PowerSetRequest(hr, 1) else "fail"     # PowerRequestSystemRequired
                k32.PowerClearRequest(hr, 1)
                k32.CloseHandle(hr)
        except Exception:
            pass
    except Exception:
        pass
    out["task"] = inspect_autostart_task(base_dir=base_dir or APP_ROOT_PATH, py_exe=py_exe)
    return out


def format_privilege_selftest(res: dict) -> str:
    """Řádek do logu: Kontrola práv: správce ano · priorita OK · …"""
    def _v(x):
        x = str(x or "")
        return "OK" if x == "ok" else (wc.T('w_priv_unsupported') if x == "unsupported" else wc.T('w_priv_fail'))
    task_k = {"none": 'w_priv_task_none', "ok": 'w_priv_task_ok', "legacy": 'w_priv_task_legacy'}.get(str(res.get("task") or ""), 'w_priv_task_unknown')
    return wc.T('w_priv_selftest', wc.T('w_priv_yes') if res.get("admin") else wc.T('w_priv_no'), _v(res.get("priority")),
                _v(res.get("affinity")), _v(res.get("wake_timer")), _v(res.get("keep_awake")), wc.T(task_k))


def ft_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def get_user_idle_seconds():
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            tck = ctypes.windll.kernel32.GetTickCount()
            return max(0.0, ((tck - lii.dwTime) & 0xFFFFFFFF) / 1000.0)
    except Exception:
        pass
    return 9999.0


def apply_process_power_and_affinity(pid, mode_or_eco=False, hybrid_info=None, custom_cores=None):
    """
    Aplikuje afinitu a prioritu procesu a vláknový throttling:
      - FULL: 100% vláken, PRIORITA BELOW_NORMAL, EcoQoS OFF.
      - ECO: (cores - 2) vláken, PRIORITA BELOW_NORMAL, EcoQoS VYPNUTO (plný takt P-cores i Ryzen).
      - NIGHT / HYBRID_ECO: pouze E-cores (pokud jsou, jinak polovina vláken), priorita IDLE + Windows EcoQoS ON.
      - CUSTOM: přesně vybraná vlákna (custom_cores), PRIORITA BELOW_NORMAL, EcoQoS VYPNUTO.
    """
    try:
        if isinstance(mode_or_eco, str):
            mode = mode_or_eco.strip().upper()
            if mode not in ("FULL", "ECO", "NIGHT", "HYBRID_ECO", "CUSTOM"):
                mode = "ECO" if mode else "FULL"
        else:
            mode = "ECO" if mode_or_eco else "FULL"

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        if pid is None:
            hp = k32.GetCurrentProcess()
            if hp:
                st = PROCESS_POWER_THROTTLING_STATE(1, 0x1, 0)
                k32.SetProcessInformation(hp, 4, ctypes.byref(st), ctypes.sizeof(st))
            return

        wc.apply_eco_affinity_to_process_tree(int(pid), enable_eco=mode, hybrid_info=hybrid_info, custom_cores=custom_cores)

        # Windows EcoQoS pouze v režimech HYBRID_ECO / NIGHT na Intel CPU; v režimech ECO a CUSTOM musí být VYPNUTO!
        enable_ecoqos = bool(mode in ("HYBRID_ECO", "NIGHT") and not IS_AMD_CPU)
        hp = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if hp:
            st = PROCESS_POWER_THROTTLING_STATE(1, 0x1, 0x1 if enable_ecoqos else 0)
            k32.SetProcessInformation(hp, 4, ctypes.byref(st), ctypes.sizeof(st))
            k32.CloseHandle(hp)
    except Exception:
        pass


WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_COMMAND = 0x0111
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
TPM_BOTTOMALIGN = 0x0020
TPM_LEFTALIGN = 0x0000
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800

WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class Win32TrayIcon:
    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.nid = None
        self._current_hicon = None
        self._last_icon_sig = None
        self._icon_lock = threading.Lock()
        self._wndproc_ref = WNDPROCTYPE(self._wnd_proc)
        threading.Thread(target=self._message_loop, daemon=True).start()

    def _create_win32_hicon(self, bgra_bytes: bytes, size: int = 32):
        try:
            u32 = ctypes.windll.user32
            u32.CreateIcon.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_byte, ctypes.c_byte, ctypes.c_char_p, ctypes.c_char_p]
            u32.CreateIcon.restype = wintypes.HICON
            and_mask = b"\x00" * ((size * size) // 8)
            return u32.CreateIcon(None, size, size, 1, 32, and_mask, bgra_bytes)
        except Exception:
            return None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        u32 = ctypes.windll.user32
        if msg == WM_TRAYICON:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.app.run_in_main_thread(self.app.restore_from_tray)
            elif lparam == WM_RBUTTONUP:
                self._show_context_menu(hwnd)
            return 0
        elif msg == WM_POWERBROADCAST:
            if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self.app.run_in_main_thread(self.app.on_system_wake_up)
            return 0
        elif msg == WM_COMMAND:
            cid = wparam & 0xFFFF
            if cid == 1001:
                self.app.run_in_main_thread(self.app.restore_from_tray)
            elif cid == 1002:
                self.app.run_in_main_thread(self.app.cycle_power_mode)
            elif cid == 1003:
                self.app.run_in_main_thread(self.app.toggle_accept_jobs)
            elif cid == 1004:
                self.app.run_in_main_thread(self.app.request_stop)
            elif cid == 1005:
                self.app.run_in_main_thread(self.app.toggle_ui_language)
            elif cid == 1006:
                self.app.run_in_main_thread(self.app.toggle_pause_running_jobs)
            elif cid == 1007:
                self.app.run_in_main_thread(self.app.prompt_uninstall_worker)
            return 0
        u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.DefWindowProcW.restype = ctypes.c_longlong
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_context_menu(self, hwnd):
        u32 = ctypes.windll.user32
        u32.CreatePopupMenu.restype = wintypes.HMENU
        u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u32.AppendMenuW.restype = wintypes.BOOL
        u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        u32.TrackPopupMenu.restype = wintypes.BOOL
        u32.DestroyMenu.argtypes = [wintypes.HMENU]
        u32.DestroyMenu.restype = wintypes.BOOL
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, MF_STRING, 1001, wc.T('w_open_hb_cluster_worker'))
        u32.AppendMenuW(m, MF_STRING, 1006, wc.T('w_resume_running_encode') if self.app.any_running_job_paused() else wc.T('w_pause_running_encode'))
        u32.AppendMenuW(m, MF_STRING, 1002, wc.T('w_switch_power_mode_current', self.app.power_mode))
        u32.AppendMenuW(m, MF_STRING, 1003, wc.T('w_pause_job_intake') if self.app.accept_jobs else wc.T('w_resume_job_intake'))
        u32.AppendMenuW(m, MF_STRING, 1005, wc.T('w_switch_language_e_tina'))
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u32.AppendMenuW(m, MF_STRING, 1007, wc.T('w_uninstall_worker'))
        u32.AppendMenuW(m, MF_STRING, 1004, wc.T('w_quit_hb_cluster_worker'))
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(hwnd)
        u32.TrackPopupMenu(m, TPM_BOTTOMALIGN | TPM_LEFTALIGN, pt.x, pt.y, 0, hwnd, None)
        u32.DestroyMenu(m)

    def _message_loop(self):
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        s32 = ctypes.windll.shell32

        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        u32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p
        ]
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        u32.LoadIconW.restype = wintypes.HICON
        u32.DestroyIcon.argtypes = [wintypes.HICON]
        u32.DestroyIcon.restype = wintypes.BOOL

        hi = k32.GetModuleHandleW(None)
        cn = f"HBClusterWorkerTray_{os.getpid()}"
        wc_cls = WNDCLASSW()
        wc_cls.lpfnWndProc = self._wndproc_ref
        wc_cls.hInstance = hi
        wc_cls.lpszClassName = cn
        u32.RegisterClassW(ctypes.byref(wc_cls))

        self.hwnd = u32.CreateWindowExW(0, cn, "HBClusterWorkerTray", 0, 0, 0, 0, 0, None, None, hi, None)
        bgra_init, _ = render_tray_status_icon("#0b1626", "#22d3ee", "HBW", 32)
        hico = self._create_win32_hicon(bgra_init, 32)
        if not hico:
            hico = u32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))
        with self._icon_lock:
            self._current_hicon = hico

            self.nid = NOTIFYICONDATAW()
            self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self.nid.uCallbackMessage = WM_TRAYICON
            self.nid.hIcon = hico
            self.nid.szTip = f"HB Cluster Worker [{self.app.config.get('worker_name')}] v{CURRENT_VERSION}"
            s32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))

        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def update_status_visual(self, bg_hex: str, border_hex: str, symbol_str: str, tooltip_text: str):
        sig = (bg_hex, border_hex, symbol_str)
        with self._icon_lock:
            icon_changed = (sig != self._last_icon_sig)
            self._last_icon_sig = sig

        new_hicon = None
        png_b64 = None
        if icon_changed:
            bgra_b, _ = render_tray_status_icon(bg_hex, border_hex, symbol_str, 32)
            _, png128_b64 = render_tray_status_icon(bg_hex, border_hex, symbol_str, 128)
            _, png48_b64 = render_tray_status_icon(bg_hex, border_hex, symbol_str, 48)
            _, png32_b64 = render_tray_status_icon(bg_hex, border_hex, symbol_str, 32)
            png_b64 = (png128_b64, png48_b64, png32_b64)
            new_hicon = self._create_win32_hicon(bgra_b, 32)

        u32 = ctypes.windll.user32
        u32.DestroyIcon.argtypes = [wintypes.HICON]
        u32.DestroyIcon.restype = wintypes.BOOL
        with self._icon_lock:
            if self.nid and self.hwnd:
                old_ico = None
                try:
                    if new_hicon:
                        old_ico = self._current_hicon
                        self._current_hicon = new_hicon
                        self.nid.hIcon = new_hicon
                    if tooltip_text:
                        self.nid.szTip = str(tooltip_text)[:127]
                    ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
                except Exception:
                    pass
                finally:
                    if old_ico and old_ico != self._current_hicon:
                        try:
                            u32.DestroyIcon(old_ico)
                        except Exception:
                            pass
            elif new_hicon:
                try:
                    u32.DestroyIcon(new_hicon)
                except Exception:
                    pass
        return png_b64

    def remove(self):
        if self.nid and self.hwnd:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            except Exception:
                pass


def _detect_windows_version_label() -> str:
    """Windows 10 / 11 podle čísla sestavení (Windows 11 = build 22000 a vyšší)."""
    try:
        build = int(sys.getwindowsversion().build)
        if build >= 22000:
            return "Windows 11"
        if build >= 10240:
            return "Windows 10"
        return f"Windows (build {build})"
    except Exception:
        return "Windows"


def _detect_windows_cpu_name() -> str:
    """Skutečný název procesoru z registru (např. „AMD Ryzen 9 5900X 12-Core Processor“)."""
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        try:
            val, _ = winreg.QueryValueEx(k, "ProcessorNameString")
        finally:
            winreg.CloseKey(k)
        val = " ".join(str(val or "").split())
        if val:
            return val
    except Exception:
        pass
    return os.environ.get("PROCESSOR_IDENTIFIER", "Windows CPU")


WINDOWS_VERSION_LABEL = _detect_windows_version_label()
WINDOWS_CPU_NAME = _detect_windows_cpu_name()


def api_request(method, url, **kwargs):
    # Sdílená requests.Session s výchozím timeoutem (viz worker_core.http_request).
    return wc.http_request(method, url, **kwargs)


def _policy_saved_ok(resp) -> bool:
    """Politika je uložená jen při HTTP 200 a zároveň bez {"ok": false} v těle odpovědi."""
    try:
        if resp is None or resp.status_code != 200:
            return False
        body = resp.json()
        return not (isinstance(body, dict) and body.get("ok") is False)
    except Exception:
        return False


def _get_win_bin_cache_key(bin_path: str):
    if not bin_path or not os.path.isfile(bin_path):
        return None
    try:
        st = os.stat(bin_path)
        return (bin_path, st.st_mtime, st.st_size)
    except Exception:
        return None


def probe_win_binary_version(bin_path: str, flag: str, regex_pat: str, default_val: str = "OK"):
    if not bin_path or not os.path.isfile(bin_path):
        return ""
    ck = _get_win_bin_cache_key(bin_path)
    if ck and ck in _bin_probe_cache:
        return _bin_probe_cache[ck]
    try:
        res = subprocess.run([bin_path, flag], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6, creationflags=CREATE_NO_WINDOW)
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        m = re.search(regex_pat, out, re.IGNORECASE)
        ver = m.group(1) if m else default_val
        if ck:
            _bin_probe_cache[ck] = ver
        return ver
    except Exception:
        if ck:
            for ok_k, ok_v in _bin_probe_cache.items():
                if ok_k[0] == ck[0] and ok_v:
                    return ok_v
        return default_val


_bin_sync_lock = threading.Lock()


def check_and_apply_binary_generation(remote_gen: int, allow_hb_update: bool, log_fn=None):
    if not remote_gen:
        return
    with _bin_sync_lock:
        os.makedirs(BIN_DIR, exist_ok=True)
        l_gen = 0
        if os.path.isfile(GEN_MARKER_FILE):
            try:
                with open(GEN_MARKER_FILE, "r", encoding="utf-8") as f:
                    l_gen = int(f.read().strip() or 0)
            except Exception:
                l_gen = 0
        if l_gen != remote_gen and allow_hb_update:
            if log_fn:
                log_fn(wc.T('w_clean_synchronization_bin_folder'))
            for p in (LOCAL_HB_EXE, LOCAL_FFMPEG_EXE, LOCAL_FFPROBE_EXE, LOCAL_MKVPROPEDIT_EXE, LOCAL_MKVMERGE_EXE, LOCAL_7Z_EXE, LOCAL_7Z_DLL, LEGACY_7ZA_EXE):
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


def sync_and_resolve_binaries(server_url=None, allow_download=False, allow_hb_update=True, win_manifest=None, remote_gen=None, log_fn=None):
    global HANDBRAKE_CLI, FFMPEG_CLI, FFPROBE_CLI, MKVPROPEDIT_CLI, MKVMERGE_CLI, SEVENZIP_CLI, HB_BINARY_SOURCE, FFMPEG_BINARY_SOURCE
    if remote_gen:
        check_and_apply_binary_generation(int(remote_gen), allow_hb_update, log_fn=log_fn)

    with _bin_sync_lock:
        os.makedirs(BIN_DIR, exist_ok=True)

        if allow_download and server_url and isinstance(win_manifest, dict):
            t_map = [
                ("hbcli", LOCAL_HB_EXE, MARKER_HB_MANAGER, "HandBrakeCLI.exe"),
                ("ffmpeg", LOCAL_FFMPEG_EXE, MARKER_FFMPEG_MANAGER, "ffmpeg.exe"),
                ("ffprobe", LOCAL_FFPROBE_EXE, MARKER_FFPROBE_MANAGER, "ffprobe.exe"),
                ("mkvpropedit", LOCAL_MKVPROPEDIT_EXE, MARKER_MKVPROPEDIT_MANAGER, "mkvpropedit.exe"),
                ("mkvmerge", LOCAL_MKVMERGE_EXE, MARKER_MKVMERGE_MANAGER, "mkvmerge.exe"),
                ("7z", LOCAL_7Z_EXE, MARKER_7Z_MANAGER, "7z.exe"),
                ("7z_dll", LOCAL_7Z_DLL, MARKER_7Z_DLL_MANAGER, "7z.dll"),
                ("launcher", LOCAL_LAUNCHER_EXE, MARKER_LAUNCHER_MANAGER, "HBWorker.exe"),
            ]
            for tk_k, lp, mp, lbl in t_map:
                if not allow_hb_update and os.path.isfile(lp):
                    continue
                ti = win_manifest.get(tk_k) or {}
                r_md5 = str(ti.get("md5") or "").strip().lower()
                if ti.get("available") and r_md5:
                    if wc.compute_file_md5(lp) != r_md5:
                        if log_fn:
                            log_fn(wc.T('w_downloading_central_bin_folder', lbl, r_md5[:8]))
                        te = lp + ".tmp"
                        try:
                            dl = api_request("GET", f"{server_url}/api/update/binaries/windows/{tk_k}", timeout=90)
                            if dl.status_code == 200 and len(dl.content) > 10000:
                                with open(te, "wb") as f:
                                    f.write(dl.content)
                                got_md5 = wc.compute_file_md5(te)
                                if got_md5 != r_md5:
                                    try:
                                        os.remove(te)
                                    except OSError:
                                        pass
                                    if log_fn:
                                        log_fn(wc.T('w_cannot_download_manager', lbl, f"MD5 mismatch ({got_md5[:8]} != {r_md5[:8]})"))
                                    continue
                                try:
                                    os.replace(te, lp)
                                except OSError:
                                    old_p = lp + ".old"
                                    try:
                                        if os.path.isfile(old_p):
                                            os.remove(old_p)
                                    except Exception:
                                        pass
                                    os.replace(lp, old_p)
                                    os.replace(te, lp)
                                wc.local_md5_cache.pop(lp, None)
                                for ck in [k for k in list(_bin_probe_cache.keys()) if k[0] == lp]:
                                    _bin_probe_cache.pop(ck, None)
                                with open(mp, "w", encoding="utf-8") as mf:
                                    mf.write(r_md5)
                                if log_fn:
                                    log_fn(wc.T('w_central_saved_bin_activated', lbl))
                        except Exception as e:
                            try:
                                if os.path.isfile(te):
                                    os.remove(te)
                            except OSError:
                                pass
                            if log_fn:
                                log_fn(wc.T('w_cannot_download_manager', lbl, e))

        if os.path.isfile(LOCAL_HB_EXE):
            HANDBRAKE_CLI = LOCAL_HB_EXE
            HB_BINARY_SOURCE = "Manager (bin/)"
        else:
            shb = shutil.which("HandBrakeCLI.exe") or shutil.which("HandBrakeCLI")
            HANDBRAKE_CLI = shb if shb else "HandBrakeCLI.exe"
            HB_BINARY_SOURCE = "System (PATH)" if shb else "MISSING!"

        if os.path.isfile(LOCAL_FFMPEG_EXE):
            FFMPEG_CLI = LOCAL_FFMPEG_EXE
            FFMPEG_BINARY_SOURCE = "Manager (bin/)"
        else:
            sff = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
            FFMPEG_CLI = sff if sff else None
            FFMPEG_BINARY_SOURCE = "System (PATH)" if sff else "MISSING!"

        FFPROBE_CLI = LOCAL_FFPROBE_EXE if os.path.isfile(LOCAL_FFPROBE_EXE) else (shutil.which("ffprobe.exe") or shutil.which("ffprobe"))

        if os.path.isfile(LOCAL_MKVPROPEDIT_EXE):
            MKVPROPEDIT_CLI = LOCAL_MKVPROPEDIT_EXE
        else:
            smp = shutil.which("mkvpropedit.exe") or shutil.which("mkvpropedit")
            dmp = r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
            MKVPROPEDIT_CLI = smp if smp else (dmp if os.path.isfile(dmp) else None)

        if os.path.isfile(LOCAL_MKVMERGE_EXE):
            MKVMERGE_CLI = LOCAL_MKVMERGE_EXE
        else:
            smm = shutil.which("mkvmerge.exe") or shutil.which("mkvmerge")
            dmm = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
            MKVMERGE_CLI = smm if smm else (dmm if os.path.isfile(dmm) else None)

        d7z = r"C:\Program Files\7-Zip\7z.exe"
        if os.path.isfile(LOCAL_7Z_EXE) and os.path.isfile(LOCAL_7Z_DLL):
            SEVENZIP_CLI = LOCAL_7Z_EXE
        elif os.path.isfile(d7z):
            SEVENZIP_CLI = d7z
        elif os.path.isfile(LOCAL_7Z_EXE):
            SEVENZIP_CLI = LOCAL_7Z_EXE
        elif os.path.isfile(LEGACY_7ZA_EXE):
            SEVENZIP_CLI = LEGACY_7ZA_EXE
        else:
            SEVENZIP_CLI = shutil.which("7z.exe") or shutil.which("7za.exe") or shutil.which("7z")

        return HANDBRAKE_CLI

def _kofi_bezier_pts(p0, p1, p2, p3, steps: int = 20):
    """Sample a cubic Bezier curve into (x, y) tuples on the 24x24 grid."""
    pts = []
    for i in range(steps + 1):
        t = i / float(steps)
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * p1[0] + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * p1[1] + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        pts.append((x, y))
    return pts


def _kofi_arc_pts(cx: float, cy: float, rx: float, ry: float, deg_start: float, deg_end: float, steps: int = 20):
    """Sample an elliptical arc into (x, y) tuples on the 24x24 grid."""
    pts = []
    for i in range(steps + 1):
        ang = math.radians(deg_start + (deg_end - deg_start) * (i / float(steps)))
        pts.append((cx + rx * math.cos(ang), cy + ry * math.sin(ang)))
    return pts


def _kofi_vector_strokes(is_evening: bool):
    """Return list of (polyline_24x24, width_at_96x96) matching the Web UI icons."""
    if is_evening:
        mug = (
            [(5.0, 9.2), (16.0, 9.2), (16.0, 19.0)]
            + _kofi_arc_pts(14.0, 19.0, 2.0, 2.0, 0, 90, 14)
            + [(7.0, 21.0)]
            + _kofi_arc_pts(7.0, 19.0, 2.0, 2.0, 90, 180, 14)
            + [(5.0, 19.0), (5.0, 9.2)]
        )
        handle = (
            [(16.0, 11.2), (18.5, 11.2)]
            + _kofi_arc_pts(18.5, 13.2, 2.0, 2.0, -90, 0, 12)
            + [(20.5, 16.0)]
            + _kofi_arc_pts(18.5, 16.0, 2.0, 2.0, 0, 90, 12)
            + [(16.0, 18.0)]
        )
        foam = (
            [(5.0, 9.2)]
            + _kofi_arc_pts(5.4, 7.2, 2.1, 2.0, 105, 315, 18)
            + _kofi_arc_pts(10.5, 5.2, 3.2, 2.5, 190, 350, 22)
            + _kofi_arc_pts(15.6, 7.2, 2.1, 2.0, 225, 435, 18)
            + [(16.0, 9.2)]
        )
        return [
            (mug, 8),
            (handle, 8),
            ([(8.5, 12.6), (8.5, 18.0)], 8),
            ([(12.5, 12.6), (12.5, 18.0)], 8),
            (foam, 8),
        ]

    cup = (
        [(3.0, 9.0), (16.5, 9.0), (16.5, 14.0)]
        + _kofi_arc_pts(11.5, 14.0, 5.0, 5.0, 0, 90, 20)
        + [(8.0, 19.0)]
        + _kofi_arc_pts(8.0, 14.0, 5.0, 5.0, 90, 180, 20)
        + [(3.0, 14.0), (3.0, 9.0)]
    )
    handle = (
        [(16.5, 9.6), (18.0, 9.6)]
        + _kofi_arc_pts(18.0, 12.6, 3.0, 3.0, -90, 90, 22)
        + [(18.0, 15.6), (16.5, 15.6)]
    )
    return [
        (cup, 8),
        (handle, 8),
        ([(5.2, 21.8), (14.3, 21.8)], 7),
        (_kofi_bezier_pts((7.2, 6.2), (6.0, 4.9), (8.9, 3.8), (7.6, 2.3), 22), 7),
        (_kofi_bezier_pts((11.9, 6.2), (10.7, 4.9), (13.6, 3.8), (12.3, 2.3), 22), 7),
    ]


def _render_kofi_photo_pil(is_evening: bool, color_hex: str):
    """Draw 4x supersampled vector icon (96x96 RGBA, width=8) and downsample via LANCZOS to 24x24."""
    hx = color_hex.lstrip("#")
    rgba = (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16), 255)
    canvas_hi = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas_hi)
    for pts_24, stroke_w in _kofi_vector_strokes(is_evening):
        pts_hi = [(p[0] * 4.0, p[1] * 4.0) for p in pts_24]
        draw.line(pts_hi, fill=rgba, width=stroke_w, joint="curve")
        r = (stroke_w - 1.0) / 2.0
        for end_pt in (pts_hi[0], pts_hi[-1]):
            draw.ellipse((end_pt[0] - r, end_pt[1] - r, end_pt[0] + r, end_pt[1] + r), fill=rgba)
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    icon_24 = canvas_hi.resize((24, 24), resample)
    return ImageTk.PhotoImage(icon_24)


def _render_kofi_photo_fallback(is_evening: bool, color_hex: str):
    """Pure-Python 4x supersampled capsule rasterizer fallback when Pillow is not installed."""
    hx = color_hex.lstrip("#")
    cr, cg, cb = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    segments = []
    for pts_24, stroke_w in _kofi_vector_strokes(is_evening):
        rad2 = (stroke_w / 8.0) ** 2
        for idx in range(len(pts_24) - 1):
            ax, ay = pts_24[idx]
            bx, by = pts_24[idx + 1]
            dx, dy = bx - ax, by - ay
            segments.append((ax, ay, dx, dy, dx * dx + dy * dy, rad2))

    raw = bytearray()
    sub_offsets = (0.125, 0.375, 0.625, 0.875)
    for y in range(24):
        raw.append(0)
        for x in range(24):
            hits = 0
            for sy in sub_offsets:
                py = y + sy
                for sx in sub_offsets:
                    px = x + sx
                    for ax, ay, dx, dy, seg_len2, rad2 in segments:
                        if seg_len2 > 1e-9:
                            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
                            cx, cy = ax + t * dx, ay + t * dy
                        else:
                            cx, cy = ax, ay
                        if (px - cx) ** 2 + (py - cy) ** 2 <= rad2:
                            hits += 1
                            break
            if hits:
                raw.extend((cr, cg, cb, int(round((hits / 16.0) * 255.0))))
            else:
                raw.extend((0, 0, 0, 0))

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 24, 24, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")
    return tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))


class HandBrakeWorkerApp:
    def __init__(self, root, cli_restore_state: str = "", is_bg_boot: bool = False, is_desktop_takeover: bool = False, is_autostart_login: bool = False, wait_session0_handover: bool = False):
        self.root = root
        self.is_bg_boot = bool(is_bg_boot or get_current_windows_session_id() == 0)
        self.is_desktop_takeover = bool(is_desktop_takeover)
        self.is_autostart_login = bool(is_autostart_login)
        self.wait_session0_handover = bool(wait_session0_handover)
        self.handover_requested = False
        self.update_blocked_until = 0.0
        self.update_retry_state = {}      # O4: neúspěšné pokusy o aktualizaci (1. → 5 min, 2. → 6 h)
        self._ota_clear_at = 0.0
        self.is_in_tray = False
        self._autostart_tip_win = None
        self._smb_connect_lock = threading.Lock()
        self._smb_last_err = {}
        self._autostart_op_lock = threading.Lock()
        self._config_file_lock = threading.Lock()
        self._cached_win_state = "normal"
        self.update_in_progress = False
        self._autostart_status = ""
        self._autostart_last_error = ""
        self._autostart_task_state = "unknown"
        self.config = {
            "server_url": os.getenv("MANAGER_URL", "http://localhost:8383"),
            "worker_name": os.getenv("WORKER_NAME", socket.gethostname()),
            "inputs_dir": os.getenv("INPUTS_DIR", ""),
            "outputs_dir": os.getenv("OUTPUTS_DIR", ""),
            "smb_username": "",
            "smb_password": "",
            "ui_lang": "",
            "start_in_tray": False,
            "skip_tray_prompt": False,
            "show_log": False,
            "show_options_bar": False,
            "autostart_mode": "off",
            "run_on_startup": False,
            "run_before_login": False,
            "sleep_prevention_mode": "encoding",
            "custom_cores": []
        }
        self.needs_manager_url_setup = False
        self.url_setup_banner_visible = False
        self.load_config()
        self._start_tray_val = bool(self.config.get("start_in_tray", self.config.get("tray_minimize", False)))
        self._skip_prompt_val = bool(self.config.get("skip_tray_prompt", False))
        self._autostart_mode_val = self.get_autostart_mode()
        try:
            wc.init_worker_identity(APP_ROOT_PATH, self.config.get("worker_name", ""), self.config.get("join_token", ""))   # vlastní klíč workeru (app/worker_identity.json) + předschválení z balíčku
            wc.approval_log_fn = lambda m: self.log(m)
        except Exception:
            pass   # bez klíče worker běží dál jako dřív
        try:
            # navázání na Manager (app/manager_binding.json): od Manageru s jiným ID worker nic nepřijme
            wc.manager_bind_log_fn = lambda m: self.log(m)
            wc.init_manager_binding(APP_ROOT_PATH)
        except Exception:
            pass
        self.auto_sync_zero_config_from_manager()
        _lang_default = wc.default_ui_lang()                    # no language in config.json yet → language of Windows
        cfg_lang = str(self.config.get("ui_lang") or self.config.get("lang") or _lang_default).strip().lower()
        if cfg_lang not in ("cs", "en"):
            cfg_lang = _lang_default
        self.config["ui_lang"] = cfg_lang
        wc.set_worker_lang(cfg_lang)
        self.pending_lang_override = cfg_lang
        wc.sync_locales_from_manager(self.config.get("server_url"))

        is_first_run_boot = bool(self.config.pop("first_run_just_completed", False))
        if is_first_run_boot:
            try:
                self.save_tray_prefs()
            except Exception:
                pass

        ota_restore_state = (cli_restore_state or str(self.config.get("ota_restore_window_state") or "")).strip().lower()
        if "ota_restore_window_state" in self.config:
            self.config.pop("ota_restore_window_state", None)
            self._atomic_save_config()

        self.root.title(f"HB Cluster Worker [{self.config['worker_name']}] v{CURRENT_VERSION}")
        self.root.configure(bg="#090d16")
        self.root.resizable(True, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_button_clicked)
        self.root.bind("<Map>", self._on_root_state_event)
        self.root.bind("<Unmap>", self._on_root_state_event)
        self.root.bind("<Configure>", self._on_root_state_event)

        self.active_jobs = {}
        self.held_jobs = {}
        self.uploading_held_job_id = None
        self.jobs_lock = threading.Lock()
        self.held_lock = threading.Lock()
        self.handshake_lock = threading.Lock()
        self.is_running = True
        self.accept_jobs = bool(self.config.get("accept_jobs", True))
        self.pending_accept_override = None
        self.force_update_check = True
        cfg_pm = str(self.config.get("power_mode") or "AUTO").strip().upper()
        self.power_mode = cfg_pm if cfg_pm in wc.VALID_POWER_MODES else "AUTO"
        self.hybrid_cpu = wc.detect_cpu_topology()
        self.custom_cores = wc.normalize_custom_cores(self.config.get("custom_cores"), self.hybrid_cpu.get("total_threads"))
        self.telemetry_source = self.config.get("telemetry_source", "both") if self.config.get("telemetry_source") in ("system", "hb", "both") else "both"
        self.ram_format = self.config.get("ram_format", "gb_ratio") if self.config.get("ram_format") in ("gb_ratio", "gb_pct", "pct_gb", "pct_only") else "gb_ratio"
        self.pending_power_override = None
        self.pending_custom_cores_override = None
        self.pending_lang_override = None
        self.pending_pause_override = None
        self.active_eco_state = False
        self.cached_diag = {}
        self.remote_win_binaries = None
        self.remote_binary_gen = None
        self.handshake_state = {"verified": False, "inputs_ok": False, "outputs_ok": False}
        self.last_logged_handshake = None
        self.manager_reachable = True
        self.is_online_state = False
        self._initial_conn_checked = False
        self.telem_state = {"sys_idle": 0, "sys_kernel": 0, "sys_user": 0, "proc_times": {}}
        self._job_ui_rows = {}
        self._job_ui_order = []
        self._job_ui_start_ts = {}
        self._kofi_hover = False
        self._kofi_images = {}
        self._kofi_active_img = None
        self._job_table_headings = {
            "id": "ID", "file": "Processed File", "prog": "Progress",
            "fps": "Speed / FPS", "elapsed": "Elapsed", "eta": "Remaining (ETA)"
        }
        self.log_visible = bool(self.config.get("show_log", False))
        _sm = str(self.config.get("sleep_prevention_mode") or "encoding").strip().lower()
        self.sleep_prevention_mode = _sm if _sm in ("encoding", "always", "off") else "encoding"
        self._last_job_activity_ts = 0.0     # chytrý režim spánku: konec poslední úlohy
        self.manager_waiting_jobs = 0        # chytrý režim spánku: čekající úlohy ve frontě Manageru (z odpovědi na ping)
        self.wake_check = bool(self.config.get("wake_check", True))   # kontrolní buzení uspaného PC každých 30 min
        self._sleep_tick_ts = 0.0
        self._wake_check_until = 0.0
        self._wake_arm_ts = 0.0
        has_basic_cfg = bool(self.config.get("server_url") and (self.config.get("inputs_dir") or self.config.get("outputs_dir"))) and not self.needs_manager_url_setup
        self.options_visible = not has_basic_cfg
        self.options_autohide_remaining = 0
        self._options_autohide_after_id = None
        self._options_last_reset_ts = 0.0
        self._tk_win_icon_ref = None
        self._win_hicon_small = None
        self._win_hicon_big = None
        self._last_win_icon_sig = None

        apply_process_power_and_affinity(None, "FULL", hybrid_info=self.hybrid_cpu, custom_cores=self.custom_cores)
        self.update_sleep_prevention_state()

        self.init_aa_icons()
        self.setup_dark_theme()
        self.create_widgets()
        try:
            _hbw_ico = os.path.join(APP_ROOT_PATH, "hbw.ico")
            if os.path.isfile(_hbw_ico):
                self.root.iconbitmap(_hbw_ico)
        except Exception:
            pass
        self.refresh_ui_language()
        self.apply_window_geometry()
        self.root.title(f"HB Worker [{self.config['worker_name']}] v{CURRENT_VERSION}")
        self.tray_icon = Win32TrayIcon(self)
        self.refresh_tray_and_window_icon(force=True)
        self.root.after(250, lambda: self.refresh_tray_and_window_icon(force=True))
        self.root.after(1200, lambda: self.refresh_tray_and_window_icon(force=True))
        self.root.after(60000, self._tick_kofi_icon)

        # Restart after an update (update_worker.bat passes --restore-state=tray|minimized|normal): the window comes back
        # exactly as it was. Without this check every update counted as a manual start and opened the window (2.11.4S).
        is_ota_restart = bool(str(cli_restore_state or "").strip())
        is_interactive_manual_boot = (not self.is_bg_boot and not self.is_desktop_takeover and not self.is_autostart_login
                                      and not self.wait_session0_handover and not is_ota_restart)
        if is_first_run_boot or is_interactive_manual_boot:
            # První spuštění po instalaci nebo manuální interaktivní spuštění -> VŽDY otevřít okno!
            self.is_in_tray = False
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.after(80, lambda: apply_windows_dark_titlebar(self.root))
            if self.needs_manager_url_setup:
                try:
                    self.quick_url_ent.focus_set()
                except Exception:
                    pass
        elif ota_restore_state == "tray":
            self.hide_to_tray()
        elif ota_restore_state == "minimized":
            self.is_in_tray = False
            self._cached_win_state = "iconic"
            self.root.deiconify()        # a never-mapped Tk window has to be mapped first, otherwise iconify() only
            self.root.iconify()          # remembers the state and the window would not appear on the taskbar at all
            self.root.after(80, lambda: apply_windows_dark_titlebar(self.root))
        elif ota_restore_state == "normal":
            # window was open before the update → open again (start_in_tray applies to starts with Windows only)
            self.is_in_tray = False
            self.root.deiconify()
            self.root.after(80, lambda: apply_windows_dark_titlebar(self.root))
        else:
            if self.is_bg_boot or self.is_desktop_takeover or self.wait_session0_handover or (self.is_autostart_login and self.config.get("start_in_tray")):
                self.hide_to_tray()
            else:
                self.is_in_tray = False
                self.root.deiconify()
                self.root.after(80, lambda: apply_windows_dark_titlebar(self.root))

        cur_auto = self.get_autostart_mode()
        if cur_auto in ("after_login", "before_login"):
            threading.Thread(target=self.apply_autostart_mode_in_os, args=(cur_auto, False, False), daemon=True).start()

        # Při předávání ze Session 0 nesmíme přepsat .worker_instance.json ani číst IPC příkazy dřív,
        # než stará instance na pozadí bezpečně dokončí předání.
        if not self.wait_session0_handover:
            self.write_ipc_instance_state()
            threading.Thread(target=self.ipc_control_loop, daemon=True).start()
        threading.Thread(target=self._startup_background_workers, daemon=True).start()
        self._ota_after_start()

    def _ota_after_start(self):
        """O1: nová verze potvrdí update_worker.bat „naběhl jsem“ (ota_ok.txt, až běží okno);
        stará verze po vrácení zapíše důvod a další pokus až za 6 h."""
        try:
            if OTA_START_STATE == "updated":
                self.log(wc.T('w_update_done', OTA_START_REC.get("from") or "?", CURRENT_VERSION))
                self.root.after(5000, self._ota_mark_started)
                self._ota_clear_at = time.time() + wc.OTA_ATTEMPT_KEEP_S
            elif OTA_START_STATE == "failed":
                until = float(OTA_START_REC.get("ts") or 0) + wc.UPDATE_RETRY_LONG_S
                if until <= time.time():
                    wc.clear_ota_attempt(APP_ROOT_PATH)
                    return
                why = wc.T('w_update_win_not_started')
                self.update_retry_state.update({"target": str(OTA_START_REC.get("to") or ""), "fails": 2,
                                                "reason": "rollback", "detail": why, "until": until})
                self.update_blocked_until = until
                self.log(wc.T('w_update_rolled_back', OTA_START_REC.get("to") or "?", why, CURRENT_VERSION, wc.fmt_retry_time(until)))
        except Exception:
            pass

    def _ota_mark_started(self):
        try:
            with open(os.path.join(APP_ROOT_PATH, wc.OTA_OK_FILE), "w", encoding="utf-8") as f:
                f.write(f"{CURRENT_VERSION} {int(time.time())}\n")
        except Exception:
            pass

    def request_update_now(self):
        """O4: „Aktualizovat teď“ z Manageru – zruší čekání po neúspěchu a hned zkusí aktualizaci."""
        if self.update_blocked_until > time.time() or self.update_retry_state:
            self.log(wc.T('w_update_now_requested'))
        wc.update_retry_reset(self.update_retry_state)
        self.update_blocked_until = 0.0
        self.force_update_check = True
        try:
            self.cached_diag["ota"] = wc.update_retry_view(self.update_retry_state)
        except Exception:
            pass

    def note_update_failure(self, target: str, reason: str, short_s: float, detail: str = "") -> float:
        """Neúspěch aktualizace: 1. → short_s, 2. stejné verze → 6 h (O4). Vrací čas dalšího pokusu."""
        self.update_blocked_until = wc.update_retry_note(self.update_retry_state, target, reason, short_s, detail=detail)
        try:
            self.cached_diag["ota"] = wc.update_retry_view(self.update_retry_state)
        except Exception:
            pass
        return self.update_blocked_until

    def _startup_background_workers(self):
        if self.wait_session0_handover:
            self.log(wc.T("w_autostart_wait_session0"))
            t_wait0 = time.time()
            while self.is_running and (time.time() - t_wait0) < 7200:
                if not os.path.isfile(IPC_STATE_FILE):
                    break
                try:
                    with open(IPC_STATE_FILE, "r", encoding="utf-8") as rf:
                        st = json.load(rf)
                    st_pid = int(st.get("pid") or 0)
                    if st_pid == os.getpid() or int(st.get("session_id", 1)) != 0 or not _is_windows_pid_alive(st_pid):
                        break
                except Exception:
                    break
                time.sleep(0.5)
            self.wait_session0_handover = False
            self.write_ipc_instance_state()
            threading.Thread(target=self.ipc_control_loop, daemon=True).start()

        sync_and_resolve_binaries(self.config.get("server_url"), allow_download=False)
        try:
            self._priv_test = run_windows_privilege_selftest(base_dir=APP_ROOT_PATH, py_exe=str(self.config.get("python_path") or ""))
            self._autostart_task_state = str(self._priv_test.get("task") or "unknown")
            self._caps_cache = None
            self.log(format_privilege_selftest(self._priv_test))
            self.run_in_main_thread(self.update_autostart_status_ui)
        except Exception:
            pass
        self.collect_diagnostics()
        self.restore_held_jobs_on_startup()
        self.log(wc.T('w_hb_cluster_worker_started', self.config['worker_name'], CURRENT_VERSION, wc.CURRENT_VERSION, self.cached_diag.get('hb_version', '?'), self.cached_diag.get('x265_version', '?')))
        threading.Thread(target=self.perform_storage_handshake, kwargs={"force_log": True}, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.power_manager_loop, daemon=True).start()
        threading.Thread(target=self.held_jobs_upload_loop, daemon=True).start()
        threading.Thread(target=self.worker_loop, daemon=True).start()

    def _get_kofi_image(self, is_evening: bool, hover: bool):
        key = (bool(is_evening), bool(hover))
        if key not in self._kofi_images:
            col_hex = "#fbbf24" if hover else "#94a3b8"
            photo = None
            if _HAS_PIL_TK:
                try:
                    photo = _render_kofi_photo_pil(bool(is_evening), col_hex)
                except Exception:
                    photo = None
            if photo is None:
                try:
                    photo = _render_kofi_photo_fallback(bool(is_evening), col_hex)
                except Exception:
                    photo = None
            if photo is not None:
                self._kofi_images[key] = photo
        return self._kofi_images.get(key)

    def init_aa_icons(self):
        self.icons = {
            "save": tk.PhotoImage(data=wc.render_rgba_png_bytes("save", "#ffffff", 18)),
            "play": tk.PhotoImage(data=wc.render_rgba_png_bytes("play", "#ffffff", 18)),
            "pause": tk.PhotoImage(data=wc.render_rgba_png_bytes("pause", "#ffffff", 18)),
            "bolt_hdr": tk.PhotoImage(data=wc.render_rgba_png_bytes("bolt", "#38bdf8", 18)),
            "bolt_btn": tk.PhotoImage(data=wc.render_rgba_png_bytes("bolt", "#38bdf8", 18)),
            "cog": tk.PhotoImage(data=wc.render_rgba_png_bytes("cog", "#e2e8f0", 18)),
            "stop": tk.PhotoImage(data=wc.render_rgba_png_bytes("stop", "#ffffff", 18)),
            "search": tk.PhotoImage(data=wc.render_rgba_png_bytes("search", "#38bdf8", 16)),
        }
        for eve in (False, True):
            for hov in (False, True):
                self._get_kofi_image(eve, hov)

    def setup_dark_theme(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg_main = "#090d16"
        bg_card = "#0f172a"
        bg_input = "#1e293b"
        fg_text = "#f1f5f9"
        border_col = "#334155"

        style.configure(".", background=bg_main, foreground=fg_text, font=("Segoe UI", 9))
        style.configure("TFrame", background=bg_main)
        style.configure("Card.TFrame", background=bg_card)
        style.configure("TLabel", background=bg_main, foreground=fg_text)
        style.configure("Card.TLabel", background=bg_card, foreground=fg_text, font=("Segoe UI", 9, "bold"))
        style.configure("Header.TLabel", background=bg_main, foreground="#ffffff", font=("Segoe UI", 12, "bold"))
        style.configure("Badge.TLabel", background="#0c4a6e", foreground="#7dd3fc", font=("Consolas", 9, "bold"))
        style.configure("WorkerBadge.TLabel", background="#1e293b", foreground="#38bdf8", font=("Consolas", 10, "bold"))
        style.configure("Power.TLabel", background=bg_main, foreground="#38bdf8", font=("Segoe UI", 9, "bold"))

        style.configure("TEntry", fieldbackground=bg_input, foreground="#ffffff", insertcolor="#ffffff", bordercolor=border_col, lightcolor=border_col, darkcolor=border_col)
        style.configure("TCheckbutton", background=bg_card, foreground=fg_text, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", bg_card)], foreground=[("active", "#38bdf8")])
        style.configure("TRadiobutton", background=bg_card, foreground=fg_text, font=("Segoe UI", 9, "bold"))
        style.map("TRadiobutton", background=[("active", bg_card)], foreground=[("active", "#38bdf8"), ("selected", "#38bdf8")])

        style.configure("TButton", background=bg_input, foreground="#ffffff", bordercolor=border_col, lightcolor=bg_input, darkcolor=bg_input, relief="flat", focusthickness=0, padding=(10, 5), font=("Segoe UI", 9, "bold"))
        style.map("TButton", background=[("active", "#334155"), ("pressed", "#0284c7")])

        style.configure("Primary.TButton", background="#0284c7", foreground="#ffffff", bordercolor="#0369a1", lightcolor="#0284c7", darkcolor="#0284c7", relief="flat")
        style.map("Primary.TButton", background=[("active", "#0ea5e9")])

        style.configure("Success.TButton", background="#059669", foreground="#ffffff", bordercolor="#047857", lightcolor="#059669", darkcolor="#059669", relief="flat")
        style.map("Success.TButton", background=[("active", "#10b981")])

        style.configure("Warn.TButton", background="#b45309", foreground="#ffffff", bordercolor="#92400e", lightcolor="#b45309", darkcolor="#b45309", relief="flat")
        style.map("Warn.TButton", background=[("active", "#d97706")])

        style.configure("Danger.TButton", background="#be123c", foreground="#ffffff", bordercolor="#9f1239", lightcolor="#be123c", darkcolor="#be123c", relief="flat")
        style.map("Danger.TButton", background=[("active", "#e11d48")])

        style.configure("Kofi.TButton", background="#1e293b", foreground="#fbbf24", bordercolor="#92400e", lightcolor="#1e293b", darkcolor="#1e293b", relief="flat", padding=(6, 4), font=("Segoe UI Emoji", 10))
        style.map("Kofi.TButton", background=[("active", "#451a03")], foreground=[("active", "#fde68a")])
        style.map("Danger.TButton", background=[("active", "#e11d48")])

        style.configure("Treeview", background="#090d16", fieldbackground="#090d16", foreground="#e2e8f0", rowheight=26, bordercolor=border_col, font=("Consolas", 9))
        style.configure("Treeview.Heading", background="#1e293b", foreground="#94a3b8", font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#0369a1")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#334155")])

    def apply_window_geometry(self):
        h = 286
        if getattr(self, "url_setup_banner_visible", False):
            h += 58
        if self.options_visible:
            h += 318
        if self.log_visible:
            h += 130
        base_w = 1180
        try:
            self.root.minsize(1180, h)
            cur_w = max(base_w, int(self.root.winfo_width() or 0))
            if hasattr(self, "top_frame") and self.top_frame.winfo_exists():
                self.root.update_idletasks()
                req_w = int(self.top_frame.winfo_reqwidth() or 0) + 48
                cur_w = max(cur_w, req_w)
            self.root.geometry(f"{cur_w}x{h}")
        except Exception:
            self.root.geometry(f"1180x{h}")

    def _ensure_header_width(self):
        try:
            if not self.root.winfo_exists() or not hasattr(self, "top_frame"):
                return
            self.root.update_idletasks()
            req_w = int(self.top_frame.winfo_reqwidth() or 0) + 48
            cur_w = int(self.root.winfo_width() or 0)
            cur_h = int(self.root.winfo_height() or 286)
            target_w = max(1180, req_w)
            if cur_w < target_w:
                self.root.geometry(f"{target_w}x{cur_h}")
        except Exception:
            pass

    def _set_header_logo_b64(self, b64_png):
        if not b64_png:
            return
        try:
            if self.root.winfo_exists() and hasattr(self, "hdr_logo_lbl"):
                self._hdr_logo_img = tk.PhotoImage(data=b64_png)
                self.hdr_logo_lbl.config(image=self._hdr_logo_img)
        except Exception:
            pass

    def _load_header_logo(self):
        """Logo E1 (uzly s čipem, varianta Worker) 42×42 px; starší worker_core bez nové funkce = původní čip HBW."""
        fn = getattr(wc, "render_e1_logo_png_b64", None)
        b64 = fn("W", 42) if fn else None
        self._set_header_logo_b64(b64 or wc.render_procedural_chip_logo_png_b64("HBW", 42))

    def show_url_setup_banner(self, show: bool = True):
        if not hasattr(self, "url_setup_banner") or not self.root.winfo_exists():
            return
        if show and not self.url_setup_banner_visible:
            self.url_setup_banner_visible = True
            self.url_setup_banner.pack(fill=tk.X, padx=14, pady=(2, 4), before=self.main_card_outer)
            self.apply_window_geometry()
        elif not show and self.url_setup_banner_visible:
            self.url_setup_banner_visible = False
            self.url_setup_banner.pack_forget()
            self.apply_window_geometry()

    def save_quick_manager_url(self, event=None):
        raw_u = str(self.url_var.get() or "").strip()
        clean_u = wc.sanitize_manager_url(raw_u)
        if not clean_u:
            return
        self.url_var.set(clean_u)
        self.config["server_url"] = clean_u
        self.needs_manager_url_setup = False
        self.show_url_setup_banner(False)
        self.save_config()
        threading.Thread(target=self.auto_sync_zero_config_from_manager, daemon=True).start()

    def create_widgets(self):
        self.top_frame = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        self.top_frame.pack(fill=tk.X)

        title_box = ttk.Frame(self.top_frame)
        title_box.pack(side=tk.LEFT)
        # Ostré 12-pinové logo čipu HBW (42×42 px) před nápisem "HB Worker".
        self._hdr_logo_img = None
        self.hdr_logo_lbl = tk.Label(title_box, bg="#090d16", bd=0, highlightthickness=0, cursor="question_arrow")
        self.hdr_logo_lbl.pack(side=tk.LEFT, padx=(0, 8))
        self.hdr_logo_lbl.bind("<Enter>", self._show_title_tooltip)
        self.hdr_logo_lbl.bind("<Leave>", self._hide_kofi_tooltip)
        self._load_header_logo()
        self.hdr_title_lbl = ttk.Label(title_box, text="HB Worker", style="Header.TLabel", cursor="question_arrow")
        self.hdr_title_lbl.pack(side=tk.LEFT)
        self.hdr_title_lbl.bind("<Enter>", self._show_title_tooltip)
        self.hdr_title_lbl.bind("<Leave>", self._hide_kofi_tooltip)
        ttk.Label(title_box, text=f" v{CURRENT_VERSION} ", style="Badge.TLabel").pack(side=tk.LEFT, padx=(8, 6))
        self.hdr_worker_badge = ttk.Label(title_box, text=f" [{self.config['worker_name']}] ", style="WorkerBadge.TLabel")
        self.hdr_worker_badge.pack(side=tk.LEFT, padx=(0, 10))

        curr_l = (wc.get_worker_lang() or "cs").upper()
        self.lang_mbtn = tk.Menubutton(
            self.top_frame,
            text=f"{curr_l} ▾",
            relief="flat",
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
            padx=6,
            pady=2
        )
        self.lang_menu = tk.Menu(
            self.lang_mbtn,
            tearoff=0,
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            font=("Segoe UI", 9)
        )
        self.lang_mbtn["menu"] = self.lang_menu
        self.lang_btn = self.lang_mbtn
        self.lang_mbtn.pack(side=tk.RIGHT, padx=(4, 0))
        self._populate_main_lang_menu()

        self.kofi_btn = tk.Canvas(
            self.top_frame, width=28, height=26, bg="#090d16",
            bd=0, highlightthickness=0, cursor="hand2"
        )
        self.kofi_btn.pack(side=tk.RIGHT, padx=(8, 2))
        self.kofi_btn.bind("<Button-1>", lambda e: self.open_kofi_support())
        self.kofi_btn.bind("<Enter>", self._on_kofi_enter)
        self.kofi_btn.bind("<Leave>", self._on_kofi_leave)
        self._draw_kofi_canvas(False)

        self.status_indicator = tk.Canvas(self.top_frame, width=14, height=14, bg="#090d16", highlightthickness=0)
        self.status_indicator.pack(side=tk.RIGHT, padx=(6, 0))
        self.status_circle = self.status_indicator.create_oval(2, 2, 12, 12, fill="#38bdf8", outline="")
        self.status_label = ttk.Label(self.top_frame, text=wc.T('w_ready_awaiting_manager'), font=("Segoe UI", 9, "bold"), foreground="#38bdf8")
        self.status_label.pack(side=tk.RIGHT, padx=4)

        self.power_label = ttk.Label(self.top_frame, text="  Mode: AUTO", image=self.icons["bolt_hdr"], compound=tk.LEFT, style="Power.TLabel")
        self.power_label.pack(side=tk.LEFT, padx=4)

        self.url_var = tk.StringVar(value=self.config["server_url"])

        self.url_setup_banner = tk.Frame(self.root, bg="#0ea5e9", padx=1, pady=1)
        banner_inner = tk.Frame(self.url_setup_banner, bg="#0c243c", padx=12, pady=8)
        banner_inner.pack(fill=tk.X)
        self.lbl_quick_url_prompt = tk.Label(
            banner_inner,
            text=wc.T('w_quick_url_prompt'),
            bg="#0c243c", fg="#38bdf8", font=("Segoe UI", 9, "bold")
        )
        self.lbl_quick_url_prompt.pack(side=tk.LEFT, padx=(0, 10))
        self.quick_url_ent = ttk.Entry(banner_inner, textvariable=self.url_var, width=38)
        self.quick_url_ent.pack(side=tk.LEFT, padx=(0, 8))
        self.quick_url_ent.bind("<Return>", self.save_quick_manager_url)
        self.btn_quick_url_save = ttk.Button(
            banner_inner, text=wc.T('w_quick_url_save'), image=self.icons["save"],
            compound=tk.LEFT, style="Primary.TButton", command=self.save_quick_manager_url
        )
        self.btn_quick_url_save.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_quick_url_find = ttk.Button(
            banner_inner, text=wc.T('w_find'), image=self.icons["search"],
            compound=tk.LEFT, command=self.discover_manager_clicked
        )
        self.btn_quick_url_find.pack(side=tk.LEFT)

        self.main_card_outer = tk.Frame(self.root, bg="#1e293b", padx=1, pady=1)
        self.main_card_outer.pack(fill=tk.X, padx=14, pady=2)
        if self.needs_manager_url_setup:
            self.show_url_setup_banner(True)
        self.settings_frame = ttk.Frame(self.main_card_outer, style="Card.TFrame", padding=(10, 8, 10, 8))
        self.settings_frame.pack(fill=tk.X)

        btn_frame = ttk.Frame(self.settings_frame, style="Card.TFrame")
        btn_frame.pack(fill=tk.X)

        self.accept_btn = ttk.Button(btn_frame, text=wc.T('w_intake'), image=self.icons["play"], compound=tk.LEFT, style="Success.TButton", command=self.toggle_accept_jobs)
        self.accept_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.update_accept_btn_ui()
        self.pause_encode_btn = ttk.Button(btn_frame, text=wc.T('w_pause_encode_2'), image=self.icons["pause"], compound=tk.LEFT, command=self.toggle_pause_running_jobs)
        self.pause_encode_btn.pack(side=tk.LEFT, padx=4)
        self.pwr_btn = ttk.Button(btn_frame, text=wc.T('w_power'), image=self.icons["bolt_btn"], compound=tk.LEFT, command=self.cycle_power_mode)
        self.pwr_btn.pack(side=tk.LEFT, padx=4)
        self.opts_btn = ttk.Button(btn_frame, text=wc.T('w_options'), image=self.icons["cog"], compound=tk.LEFT, command=self.toggle_options_bar)
        self.opts_btn.pack(side=tk.LEFT, padx=4)
        self.quit_btn = ttk.Button(btn_frame, text=wc.T('w_quit'), image=self.icons["stop"], compound=tk.LEFT, style="Danger.TButton", command=self.request_stop)
        self.quit_btn.pack(side=tk.RIGHT)

        self.settings_drawer = ttk.Frame(self.settings_frame, style="Card.TFrame")
        self.settings_drawer.bind("<Motion>", self.reset_options_autohide_timer, add="+")
        self.settings_drawer.bind("<Button-1>", self.reset_options_autohide_timer, add="+")
        self.settings_drawer.columnconfigure(1, weight=1)
        self.settings_drawer.columnconfigure(3, weight=1)

        self.lbl_url = ttk.Label(self.settings_drawer, text="Manager URL:", style="Card.TLabel")
        self.lbl_url.grid(row=0, column=0, sticky=tk.W, pady=(8, 2))

        url_box = ttk.Frame(self.settings_drawer, style="Card.TFrame")
        url_box.grid(row=0, column=1, sticky=tk.EW, padx=(8, 16), pady=(8, 2))
        url_box.columnconfigure(0, weight=1)
        url_ent = ttk.Entry(url_box, textvariable=self.url_var)
        url_ent.grid(row=0, column=0, sticky=tk.EW, padx=(0, 4))
        url_ent.bind("<Key>", self.reset_options_autohide_timer)
        url_ent.bind("<Button-1>", self.reset_options_autohide_timer)
        self.find_mgr_btn = ttk.Button(url_box, text=wc.T('w_find'), image=self.icons["search"], compound=tk.LEFT, command=self.discover_manager_clicked)
        self.find_mgr_btn.grid(row=0, column=1, sticky=tk.E)

        self.lbl_name = ttk.Label(self.settings_drawer, text=wc.T('w_worker_name'), style="Card.TLabel")
        self.lbl_name.grid(row=0, column=2, sticky=tk.W, pady=(8, 2))
        self.name_var = tk.StringVar(value=self.config["worker_name"])
        name_ent = ttk.Entry(self.settings_drawer, textvariable=self.name_var)
        name_ent.grid(row=0, column=3, columnspan=2, sticky=tk.EW, padx=(8, 0), pady=(8, 2))
        name_ent.bind("<Key>", self.reset_options_autohide_timer)
        name_ent.bind("<Button-1>", self.reset_options_autohide_timer)

        self.lbl_in = ttk.Label(self.settings_drawer, text=wc.T('w_inputs_folder'), style="Card.TLabel")
        self.lbl_in.grid(row=1, column=0, sticky=tk.W, pady=2)
        self.in_var = tk.StringVar(value=self.get_inputs_dir())
        in_ent = ttk.Entry(self.settings_drawer, textvariable=self.in_var)
        in_ent.grid(row=1, column=1, columnspan=3, sticky=tk.EW, padx=(8, 6), pady=2)
        in_ent.bind("<Key>", self.reset_options_autohide_timer)
        in_ent.bind("<Button-1>", self.reset_options_autohide_timer)
        self.btn_browse_in = ttk.Button(self.settings_drawer, text=wc.T('w_browse'), width=9, command=self.browse_in)
        self.btn_browse_in.grid(row=1, column=4, sticky=tk.E, pady=2)

        self.lbl_out = ttk.Label(self.settings_drawer, text=wc.T('w_outputs_folder'), style="Card.TLabel")
        self.lbl_out.grid(row=2, column=0, sticky=tk.W, pady=2)
        self.out_var = tk.StringVar(value=self.get_outputs_dir())
        out_ent = ttk.Entry(self.settings_drawer, textvariable=self.out_var)
        out_ent.grid(row=2, column=1, columnspan=3, sticky=tk.EW, padx=(8, 6), pady=2)
        out_ent.bind("<Key>", self.reset_options_autohide_timer)
        out_ent.bind("<Button-1>", self.reset_options_autohide_timer)
        self.btn_browse_out = ttk.Button(self.settings_drawer, text=wc.T('w_browse'), width=9, command=self.browse_out)
        self.btn_browse_out.grid(row=2, column=4, sticky=tk.E, pady=2)

        drawer_bottom = ttk.Frame(self.settings_drawer, style="Card.TFrame")
        drawer_bottom.grid(row=3, column=0, columnspan=5, sticky=tk.EW, pady=(8, 2))

        self.save_btn = ttk.Button(drawer_bottom, text=wc.T('w_save_test'), image=self.icons["save"], compound=tk.LEFT, style="Primary.TButton", command=self.save_config)
        self.save_btn.pack(side=tk.LEFT, padx=(0, 14))
        # „Navázat na tento Manager“ – viditelné jen, když na nastavené adrese odpovídá jiný Manager
        self.rebind_btn = ttk.Button(drawer_bottom, text=wc.T('w_mgr_bind_btn'), style="Warn.TButton", command=self.rebind_manager_clicked)
        self._rebind_btn_shown = False

        self.start_tray_var = tk.BooleanVar(value=bool(self._start_tray_val))
        self.chk_start_tray = ttk.Checkbutton(drawer_bottom, text=wc.T('w_start_minimized_system_tray'), variable=self.start_tray_var, command=self.on_tray_checkbox_changed)
        self.chk_start_tray.pack(side=tk.LEFT, padx=(0, 12))

        self.skip_prompt_var = tk.BooleanVar(value=bool(self._skip_prompt_val))
        self.chk_skip_prompt = ttk.Checkbutton(drawer_bottom, text=wc.T('w_hide_tray_x_without'), variable=self.skip_prompt_var, command=self.on_tray_checkbox_changed)
        self.chk_skip_prompt.pack(side=tk.LEFT, padx=(0, 12))

        self.show_log_var = tk.BooleanVar(value=self.log_visible)
        self.chk_show_log = ttk.Checkbutton(drawer_bottom, text=wc.T('w_show_log_console'), variable=self.show_log_var, command=self.toggle_log_panel)
        self.chk_show_log.pack(side=tk.LEFT)

        drawer_boot = ttk.Frame(self.settings_drawer, style="Card.TFrame")
        drawer_boot.grid(row=4, column=0, columnspan=5, sticky=tk.EW, pady=(6, 1))

        self.lbl_autostart = ttk.Label(drawer_boot, text=wc.T('w_autostart_lbl'), style="Card.TLabel")
        self.lbl_autostart.pack(side=tk.LEFT, padx=(0, 10))

        self.autostart_mode_var = tk.StringVar(value=self.get_autostart_mode())
        self.rb_auto_off = ttk.Radiobutton(drawer_boot, text=wc.T('w_autostart_off_short'), value="off", variable=self.autostart_mode_var, command=self.on_autostart_mode_changed)
        self.rb_auto_off.pack(side=tk.LEFT, padx=(0, 14))
        self.rb_auto_after = ttk.Radiobutton(drawer_boot, text=wc.T('w_autostart_after_short'), value="after_login", variable=self.autostart_mode_var, command=self.on_autostart_mode_changed)
        self.rb_auto_after.pack(side=tk.LEFT, padx=(0, 14))
        self.rb_auto_before = ttk.Radiobutton(drawer_boot, text=wc.T('w_autostart_before_short'), value="before_login", variable=self.autostart_mode_var, command=self.on_autostart_mode_changed)
        self.rb_auto_before.pack(side=tk.LEFT, padx=(0, 8))

        self.autostart_help_btn = tk.Label(
            drawer_boot, text=" [?] ", font=("Consolas", 8, "bold"),
            bg="#0c4a6e", fg="#7dd3fc", cursor="hand2", padx=3, pady=1
        )
        self.autostart_help_btn.pack(side=tk.LEFT)
        self.autostart_help_btn.bind("<Enter>", self._show_autostart_tooltip)
        self.autostart_help_btn.bind("<Leave>", self._hide_autostart_tooltip)
        self.autostart_help_btn.bind("<Button-1>", self.show_autostart_help_dialog)
        for rb_w in (self.rb_auto_off, self.rb_auto_after, self.rb_auto_before):
            rb_w.bind("<Enter>", self._show_autostart_tooltip)
            rb_w.bind("<Leave>", self._hide_autostart_tooltip)

        drawer_boot_info = tk.Frame(self.settings_drawer, bg="#0f172a")
        drawer_boot_info.grid(row=5, column=0, columnspan=5, sticky=tk.EW, pady=(1, 2))

        self.lbl_autostart_desc = tk.Label(
            drawer_boot_info, text="", font=("Segoe UI", 8),
            bg="#0f172a", fg="#94a3b8", anchor=tk.W, justify=tk.LEFT, wraplength=810
        )
        self.lbl_autostart_desc.pack(fill=tk.X, anchor=tk.W)

        self.autostart_warn_frame = tk.Frame(
            drawer_boot_info, bg="#2b1d0e", highlightbackground="#b45309", highlightthickness=1, padx=8, pady=4
        )
        self.lbl_autostart_warn = tk.Label(
            self.autostart_warn_frame, text="", font=("Segoe UI", 8, "bold"),
            bg="#2b1d0e", fg="#fcd34d", anchor=tk.W, justify=tk.LEFT, wraplength=640
        )
        self.lbl_autostart_warn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_autostart_repair = tk.Button(
            self.autostart_warn_frame, text=wc.T('w_autostart_repair_btn'),
            bg="#d97706", fg="#ffffff", activebackground="#b45309", activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"), relief="flat", padx=10, pady=2, cursor="hand2",
            command=self.repair_autostart_task_clicked
        )
        self.btn_autostart_repair.pack(side=tk.RIGHT, padx=(8, 0))
        self.update_autostart_desc_label()

        drawer_sleep = ttk.Frame(self.settings_drawer, style="Card.TFrame")
        drawer_sleep.grid(row=6, column=0, columnspan=5, sticky=tk.EW, pady=(4, 2))
        self.lbl_sleep_mode = ttk.Label(drawer_sleep, text=wc.T('w_sleep_mode_lbl'), style="Card.TLabel")
        self.lbl_sleep_mode.pack(side=tk.LEFT, padx=(0, 10))
        self.sleep_mode_var = tk.StringVar(value=getattr(self, "sleep_prevention_mode", "encoding"))
        self.rb_sleep_encoding = ttk.Radiobutton(drawer_sleep, text=wc.T('w_sleep_encoding'), value="encoding", variable=self.sleep_mode_var, command=self.on_sleep_mode_changed)
        self.rb_sleep_encoding.pack(side=tk.LEFT, padx=(0, 14))
        self.rb_sleep_always = ttk.Radiobutton(drawer_sleep, text=wc.T('w_sleep_always'), value="always", variable=self.sleep_mode_var, command=self.on_sleep_mode_changed)
        self.rb_sleep_always.pack(side=tk.LEFT, padx=(0, 14))
        self.rb_sleep_off = ttk.Radiobutton(drawer_sleep, text=wc.T('w_sleep_off'), value="off", variable=self.sleep_mode_var, command=self.on_sleep_mode_changed)
        self.rb_sleep_off.pack(side=tk.LEFT, padx=(0, 8))
        self.wake_check_var = tk.BooleanVar(value=bool(getattr(self, "wake_check", True)))
        self.chk_wake_check = ttk.Checkbutton(drawer_sleep, text=wc.T('w_wake_check'), variable=self.wake_check_var, command=self.on_wake_check_changed)
        self.chk_wake_check.pack(side=tk.LEFT, padx=(6, 0))

        drawer_custom_cpu = ttk.Frame(self.settings_drawer, style="Card.TFrame")
        drawer_custom_cpu.grid(row=7, column=0, columnspan=5, sticky=tk.EW, pady=(4, 2))
        self.lbl_custom_cpu = ttk.Label(drawer_custom_cpu, text=wc.T('w_custom_cpu_lbl'), style="Card.TLabel")
        self.lbl_custom_cpu.pack(side=tk.LEFT, padx=(0, 8))
        self.lbl_cpu_topo_badge = tk.Label(
            drawer_custom_cpu, text=self._format_cpu_topology_badge(),
            font=("Consolas", 8, "bold"), bg="#0c4a6e", fg="#7dd3fc", padx=6, pady=1
        )
        self.lbl_cpu_topo_badge.pack(side=tk.LEFT, padx=(0, 8))

        hyb = self.hybrid_cpu or {}
        if hyb.get("is_hybrid"):
            self.btn_cpu_p_only = tk.Button(
                drawer_custom_cpu, text=wc.T('w_cpu_preset_p'), bg="#1e293b", fg="#38bdf8",
                activebackground="#0284c7", activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
                relief="flat", padx=6, pady=1, cursor="hand2",
                command=lambda: self.apply_custom_cpu_preset("p_only")
            )
            self.btn_cpu_p_only.pack(side=tk.LEFT, padx=(0, 4))
            self.btn_cpu_e_only = tk.Button(
                drawer_custom_cpu, text=wc.T('w_cpu_preset_e'), bg="#1e293b", fg="#34d399",
                activebackground="#059669", activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
                relief="flat", padx=6, pady=1, cursor="hand2",
                command=lambda: self.apply_custom_cpu_preset("e_only")
            )
            self.btn_cpu_e_only.pack(side=tk.LEFT, padx=(0, 4))
        if hyb.get("has_smt"):
            self.btn_cpu_no_smt = tk.Button(
                drawer_custom_cpu, text=wc.T('w_cpu_preset_no_smt'), bg="#1e293b", fg="#fbbf24",
                activebackground="#d97706", activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
                relief="flat", padx=6, pady=1, cursor="hand2",
                command=lambda: self.apply_custom_cpu_preset("no_smt")
            )
            self.btn_cpu_no_smt.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_cpu_all = tk.Button(
            drawer_custom_cpu, text=wc.T('w_cpu_preset_all'), bg="#1e293b", fg="#e2e8f0",
            activebackground="#475569", activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
            relief="flat", padx=6, pady=1, cursor="hand2",
            command=lambda: self.apply_custom_cpu_preset("all")
        )
        self.btn_cpu_all.pack(side=tk.LEFT, padx=(0, 8))

        self.cpu_core_btns_frame = tk.Frame(drawer_custom_cpu, bg="#0f172a")
        self.cpu_core_btns_frame.pack(side=tk.LEFT, fill=tk.X)
        self._cpu_core_buttons = {}
        self._build_custom_cpu_core_buttons()

        drawer_footer = ttk.Frame(self.settings_drawer, style="Card.TFrame")
        drawer_footer.grid(row=8, column=0, columnspan=5, sticky=tk.EW, pady=(4, 2))

        self.lbl_shortcuts = ttk.Label(drawer_footer, text=wc.T('w_win_shortcuts_lbl'), style="Card.TLabel")
        self.lbl_shortcuts.pack(side=tk.LEFT, padx=(0, 8))

        self.desk_sc_var = tk.BooleanVar(value=bool(self.config.get("desktop_shortcut", True)))
        self.chk_desk_sc = ttk.Checkbutton(drawer_footer, text=wc.T('w_desk_shortcut_chk'), variable=self.desk_sc_var, command=self.on_shortcuts_pref_changed)
        self.chk_desk_sc.pack(side=tk.LEFT, padx=(0, 12))

        self.smenu_sc_var = tk.BooleanVar(value=bool(self.config.get("start_menu_shortcut", True)))
        self.chk_smenu_sc = ttk.Checkbutton(drawer_footer, text=wc.T('w_smenu_shortcut_chk'), variable=self.smenu_sc_var, command=self.on_shortcuts_pref_changed)
        self.chk_smenu_sc.pack(side=tk.LEFT, padx=(0, 12))

        self.uninstall_btn = tk.Button(
            drawer_footer,
            text=wc.T('w_uninstall_worker'),
            bg="#1e293b",
            fg="#f43f5e",
            activebackground="#7f1d1d",
            activeforeground="#ffffff",
            font=("Segoe UI", 8),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=self.prompt_uninstall_worker
        )
        self.uninstall_btn.pack(side=tk.RIGHT)

        self.open_folder_btn = tk.Button(
            drawer_footer,
            text=wc.T('w_open_worker_folder'),
            bg="#1e293b",
            fg="#38bdf8",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=self.open_worker_folder
        )
        self.open_folder_btn.pack(side=tk.RIGHT, padx=(0, 8))

        if self.options_visible:
            self.settings_drawer.pack(fill=tk.X, pady=(6, 0))

        tele_outer = tk.Frame(self.root, bg="#1e293b", padx=1, pady=1)
        tele_outer.pack(fill=tk.X, padx=14, pady=2)
        tele_frame = ttk.Frame(tele_outer, style="Card.TFrame", padding=(10, 6, 10, 6))
        tele_frame.pack(fill=tk.X)
        tele_frame.columnconfigure(0, weight=1, uniform="tele_half")
        tele_frame.columnconfigure(1, weight=1, uniform="tele_half")

        cpu_pane = ttk.Frame(tele_frame, style="Card.TFrame")
        cpu_pane.grid(row=0, column=0, sticky=tk.EW, padx=(0, 8))
        cpu_pane.columnconfigure(1, weight=1)

        ttk.Label(cpu_pane, text="CPU:", style="Card.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.cpu_bar_canvas = tk.Canvas(cpu_pane, width=190, height=16, bg="#090d16", highlightthickness=1, highlightbackground="#334155")
        self.cpu_bar_canvas.grid(row=0, column=1, sticky=tk.EW, padx=(6, 8), pady=2)
        self.cpu_hb_rect = self.cpu_bar_canvas.create_rectangle(0, 0, 0, 8, fill="#0ea5e9", outline="")
        self.cpu_sys_rect = self.cpu_bar_canvas.create_rectangle(0, 10, 0, 16, fill="#cbd5e1", outline="")
        self.cpu_txt_lbl = tk.Label(cpu_pane, text="HB: 0.0% | Sys: 0.0%", width=24, anchor=tk.E, font=("Consolas", 8, "bold"), bg="#0f172a", fg="#38bdf8")
        self.cpu_txt_lbl.grid(row=0, column=2, sticky=tk.E)

        ram_pane = ttk.Frame(tele_frame, style="Card.TFrame")
        ram_pane.grid(row=0, column=1, sticky=tk.EW, padx=(8, 0))
        ram_pane.columnconfigure(1, weight=1)

        ttk.Label(ram_pane, text="RAM:", style="Card.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.ram_bar_canvas = tk.Canvas(ram_pane, width=190, height=16, bg="#090d16", highlightthickness=1, highlightbackground="#334155")
        self.ram_bar_canvas.grid(row=0, column=1, sticky=tk.EW, padx=(6, 8), pady=2)
        self.ram_hb_rect = self.ram_bar_canvas.create_rectangle(0, 0, 0, 8, fill="#0ea5e9", outline="")
        self.ram_sys_rect = self.ram_bar_canvas.create_rectangle(0, 10, 0, 16, fill="#cbd5e1", outline="")
        self.ram_txt_lbl = tk.Label(ram_pane, text="HB: 0.00 GB | 0.0 / 0.0 GB", width=29, anchor=tk.E, font=("Consolas", 8, "bold"), bg="#0f172a", fg="#34d399")
        self.ram_txt_lbl.grid(row=0, column=2, sticky=tk.E)

        job_outer = tk.Frame(self.root, bg="#1e293b", padx=1, pady=1)
        job_outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)
        job_frame = ttk.Frame(job_outer, style="Card.TFrame", padding=4)
        job_frame.pack(fill=tk.BOTH, expand=True)

        self.job_table_canvas = tk.Canvas(job_frame, bg="#090d16", height=112, bd=0, highlightthickness=0)
        self.job_table_canvas.pack(fill=tk.BOTH, expand=True)
        self.job_table_canvas.bind("<Configure>", lambda e: self._redraw_job_table())
        self.root.after(20, self._redraw_job_table)

        self.log_container = tk.Frame(self.root, bg="#090d16")
        self.log_text = tk.Text(self.log_container, height=6, state=tk.DISABLED, bg="#05080f", fg="#38bdf8", insertbackground="#ffffff", relief="flat", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))
        if self.log_visible:
            self.log_container.pack(fill=tk.BOTH, expand=False)
    def any_running_job_paused(self) -> bool:
        with self.jobs_lock:
            return any(bool(d.get("user_paused")) for d in self.active_jobs.values())

    def update_pause_encode_btn_ui(self):
        def _upd():
            if not self.root.winfo_exists():
                return
            with self.jobs_lock:
                act_cnt = len(self.active_jobs)
                is_p = any(bool(d.get("user_paused")) for d in self.active_jobs.values())
            if is_p:
                self.pause_encode_btn.config(
                    text=wc.T('w_resume_encode'),
                    image=self.icons["play"],
                    style="Warn.TButton"
                )
            else:
                lbl = wc.T('w_pause_encode', act_cnt) if act_cnt > 0 else wc.T('w_pause_encode_2')
                self.pause_encode_btn.config(
                    text=lbl,
                    image=self.icons["pause"],
                    style="TButton"
                )
        self.run_in_main_thread(_upd)

    def _sync_job_suspend_state(self, job_id):
        """Idempotentně sladí stav pozastavení (NtSuspendProcess/NtResumeProcess) procesu úlohy s user_paused."""
        with self.jobs_lock:
            jd = self.active_jobs.get(job_id)
            if not jd:
                return
            proc = jd.get("process")
            want_paused = bool(jd.get("user_paused"))
            p_state = jd.get("pause_guard_state")
            if p_state is None:
                p_state = {"suspended": False, "reason": "", "lock": threading.Lock()}
                jd["pause_guard_state"] = p_state
        if not proc or proc.poll() is not None:
            return
        st_lock = p_state.get("lock")
        if st_lock is not None:
            st_lock.acquire()
        try:
            if want_paused:
                if not p_state.get("suspended"):
                    wc.suspend_or_resume_process_tree(proc, suspend=True)
                    p_state["suspended"] = True
                    p_state["reason"] = "USER"
            else:
                if p_state.get("suspended") and p_state.get("reason") != "DISK":
                    wc.suspend_or_resume_process_tree(proc, suspend=False)
                    p_state["suspended"] = False
                    p_state["reason"] = ""
                    with self.jobs_lock:
                        if job_id in self.active_jobs:
                            self.active_jobs[job_id]["step_message"] = ""
                            self.active_jobs[job_id]["disk_paused"] = False
        finally:
            if st_lock is not None:
                st_lock.release()

    def toggle_pause_running_jobs(self):
        with self.jobs_lock:
            j_ids = list(self.active_jobs.keys())
            if not j_ids:
                self.log(wc.T('w_no_active_job_running'))
                return
            cur_p = any(bool(d.get("user_paused")) for d in self.active_jobs.values())
            target_p = not cur_p
            self.pending_pause_override = target_p
            for jid, jd in self.active_jobs.items():
                jd["user_paused"] = target_p

        for jid in j_ids:
            self._sync_job_suspend_state(jid)

        self.update_pause_encode_btn_ui()
        self.refresh_tray_and_window_icon()
        if target_p:
            self.log(wc.T('w_running_encode_paused_frozen', len(j_ids)))
        else:
            self.log(wc.T('w_running_encode_resumed', len(j_ids)))

        def _sync_pause(ids_list, pause_val):
            try:
                srv = self.config.get("server_url", "").rstrip("/")
                act_s = "freeze" if pause_val else "unfreeze"
                r = api_request("POST", f"{srv}/api/v2/jobs/action", json={"job_ids": ids_list, "action": act_s}, timeout=5)
                if r.status_code == 200 and self.pending_pause_override == pause_val:
                    self.pending_pause_override = None
            except Exception:
                pass

        threading.Thread(target=_sync_pause, args=(j_ids, target_p), daemon=True).start()

    def update_sleep_prevention_state(self):
        mode = str(getattr(self, "sleep_prevention_mode", "encoding") or "encoding").strip().lower()
        if mode == "always":
            keep = True
        elif mode == "off":
            keep = False
        else:
            # „Během převodu“ (chytře): vzhůru při převodu, ještě SLEEP_GRACE_SEC po poslední úloze
            # a dokud ve frontě Manageru čekají úlohy, které může tento worker vzít (příjem zapnutý, online).
            with self.jobs_lock:
                act_cnt = len(self.active_jobs)
            busy = (act_cnt > 0 or self.uploading_held_job_id is not None)
            now_t = time.time()
            if busy:
                self._last_job_activity_ts = now_t
            recent = (now_t - float(getattr(self, "_last_job_activity_ts", 0) or 0)) < SLEEP_GRACE_SEC
            queued = bool(getattr(self, "accept_jobs", True) and getattr(self, "is_online_state", False)
                          and int(getattr(self, "manager_waiting_jobs", 0) or 0) > 0)
            keep = busy or recent or queued
        now_t = time.time()
        # Probuzení ze spánku (mezera v pravidelném 3s tiku): pár minut vzhůru, ať se worker stihne spojit
        # s Managerem a vzít čekající úlohu; když nic nečeká, Windows PC zase uspí.
        last_tick = float(getattr(self, "_sleep_tick_ts", 0) or 0)
        if last_tick and now_t - last_tick > 90:
            self._wake_check_until = now_t + 60      # nejvýš minuta; po první odpovědi Manageru se zkrátí
            self._wake_check_pending = True
        self._sleep_tick_ts = now_t
        if mode == "encoding" and now_t < float(getattr(self, "_wake_check_until", 0) or 0):
            keep = True
        # Kontrolní buzení: jen v režimu „Během převodu“, se zapnutým příjmem úloh a na napájení ze sítě.
        want_timer = (mode == "encoding" and bool(getattr(self, "wake_check", True))
                      and bool(getattr(self, "accept_jobs", True)) and is_on_ac_power())
        if want_timer:
            if now_t - float(getattr(self, "_wake_arm_ts", 0) or 0) > 60:
                self._wake_arm_ts = now_t
                arm_wake_timer(WAKE_CHECK_MINUTES)
        elif _wake_timer_armed:
            cancel_wake_timer()
            self._wake_arm_ts = 0.0
        set_keep_awake(keep)

    def on_wake_check_changed(self):
        self.wake_check = bool(self.wake_check_var.get())
        self.config["wake_check"] = self.wake_check
        self.save_tray_prefs()
        self.update_sleep_prevention_state()

    def on_sleep_mode_changed(self):
        mode = str(self.sleep_mode_var.get() or "encoding").strip().lower()
        if mode not in ("encoding", "always", "off"):
            mode = "encoding"
        self.sleep_prevention_mode = mode
        self.config["sleep_prevention_mode"] = mode
        self.save_tray_prefs()
        self.update_sleep_prevention_state()

    def _format_cpu_topology_badge(self) -> str:
        hyb = self.hybrid_cpu or {}
        tot = int(hyb.get("total_threads") or (os.cpu_count() or 1))
        phys = int(hyb.get("physical_cores") or tot)
        if hyb.get("is_hybrid"):
            p_cnt = int(hyb.get("p_core_count") or 0)
            e_cnt = int(hyb.get("e_core_count") or 0)
            return f"{phys}C/{tot}T (P:{p_cnt}t | E:{e_cnt}t)"
        if hyb.get("has_smt"):
            return f"{phys}C/{tot}T (HT/SMT)"
        return f"{tot}C/{tot}T"

    def _build_custom_cpu_core_buttons(self):
        if not hasattr(self, "cpu_core_btns_frame") or not self.cpu_core_btns_frame.winfo_exists():
            return
        for child in self.cpu_core_btns_frame.winfo_children():
            child.destroy()
        self._cpu_core_buttons = {}
        hyb = self.hybrid_cpu or {}
        tot = int(hyb.get("total_threads") or (os.cpu_count() or 1))
        p_set = set(hyb.get("p_core_indices") or [])
        e_set = set(hyb.get("e_core_indices") or [])
        is_hyb = bool(hyb.get("is_hybrid"))
        max_shown = min(tot, 32)
        for idx in range(max_shown):
            tag = "E" if (is_hyb and idx in e_set) else ("P" if (is_hyb and idx in p_set) else "")
            lbl = f"{idx}{tag}" if (tag and tot <= 20) else f"{idx}"
            btn = tk.Button(
                self.cpu_core_btns_frame,
                text=lbl,
                font=("Consolas", 7, "bold"),
                relief="flat",
                padx=3 if tot <= 16 else 2,
                pady=1,
                bd=0,
                cursor="hand2",
                command=lambda c_idx=idx: self.toggle_custom_core_idx(c_idx)
            )
            btn.pack(side=tk.LEFT, padx=1)
            self._cpu_core_buttons[idx] = btn
        self._refresh_custom_cpu_core_buttons_ui()

    def _refresh_custom_cpu_core_buttons_ui(self):
        if not hasattr(self, "_cpu_core_buttons"):
            return
        hyb = self.hybrid_cpu or {}
        tot = int(hyb.get("total_threads") or (os.cpu_count() or 1))
        e_set = set(hyb.get("e_core_indices") or [])
        is_hyb = bool(hyb.get("is_hybrid"))
        sel = set(self.custom_cores) if self.custom_cores else set(range(tot))
        for idx, btn in self._cpu_core_buttons.items():
            if not btn.winfo_exists():
                continue
            active = (idx in sel)
            if active:
                if is_hyb and idx in e_set:
                    btn.config(bg="#059669", fg="#ffffff", activebackground="#10b981", activeforeground="#ffffff")
                else:
                    btn.config(bg="#0284c7", fg="#ffffff", activebackground="#0ea5e9", activeforeground="#ffffff")
            else:
                btn.config(bg="#1e293b", fg="#64748b", activebackground="#334155", activeforeground="#cbd5e1")

    def apply_custom_cpu_preset(self, preset: str):
        hyb = self.hybrid_cpu or {}
        tot = int(hyb.get("total_threads") or (os.cpu_count() or 1))
        if preset == "p_only" and hyb.get("p_core_indices"):
            cores = list(hyb["p_core_indices"])
        elif preset == "e_only" and hyb.get("e_core_indices"):
            cores = list(hyb["e_core_indices"])
        elif preset == "no_smt" and hyb.get("no_smt_indices"):
            cores = list(hyb["no_smt_indices"])
        else:
            cores = list(range(tot))
        self.custom_cores = wc.normalize_custom_cores(cores, tot) or list(range(tot))
        self.power_mode = "CUSTOM"
        self.pending_power_override = "CUSTOM"
        self.pending_custom_cores_override = list(self.custom_cores)
        self.config["custom_cores"] = list(self.custom_cores)
        self.save_tray_prefs()
        self._refresh_custom_cpu_core_buttons_ui()
        self.apply_current_power_state_now()
        self.save_custom_cores_to_manager()

    def toggle_custom_core_idx(self, idx: int):
        hyb = self.hybrid_cpu or {}
        tot = int(hyb.get("total_threads") or (os.cpu_count() or 1))
        cur = set(self.custom_cores) if self.custom_cores else set(range(tot))
        if idx in cur:
            if len(cur) <= 1:
                return
            cur.remove(idx)
        else:
            cur.add(idx)
        self.custom_cores = sorted(cur)
        self.power_mode = "CUSTOM"
        self.pending_power_override = "CUSTOM"
        self.pending_custom_cores_override = list(self.custom_cores)
        self.config["custom_cores"] = list(self.custom_cores)
        self.save_tray_prefs()
        self._refresh_custom_cpu_core_buttons_ui()
        self.apply_current_power_state_now()
        self.save_custom_cores_to_manager()

    def save_custom_cores_to_manager(self):
        cores_copy = list(self.custom_cores)
        pm_copy = self.power_mode

        def _sync():
            try:
                wn = self.config.get("worker_name", "")
                srv = self.config.get("server_url", "").rstrip("/")
                if not srv or not wn:
                    return
                r = api_request(
                    "POST", f"{srv}/api/v2/workers/{wn}/policy",
                    json={"power_mode": pm_copy, "custom_cores": cores_copy},
                    timeout=5
                )
                if _policy_saved_ok(r):
                    if self.pending_power_override == pm_copy:
                        self.pending_power_override = None
                    if self.pending_custom_cores_override == cores_copy:
                        self.pending_custom_cores_override = None
            except Exception:
                pass

        threading.Thread(target=_sync, daemon=True).start()

    def open_worker_folder(self):
        target_dir = APP_ROOT_PATH if os.path.isdir(APP_ROOT_PATH) else os.path.dirname(os.path.abspath(__file__))
        try:
            if os.name == "nt":
                os.startfile(target_dir)
            else:
                subprocess.Popen(["xdg-open", target_dir])
        except Exception as e:
            self.log(f"Open folder error: {e}")

    def on_system_wake_up(self):
        # Po WM_POWERBROADCAST hned nastavit okno kontroly (60 s), aby Windows neuspal PC zpět dřív než Manager odpoví
        self._wake_check_until = time.time() + 60
        self._wake_check_pending = True
        self.update_sleep_prevention_state()
        self.force_update_check = True
        self.log(wc.T('w_system_resumed_standby_reconnectin'))
        threading.Thread(target=self.perform_storage_handshake, kwargs={"force_log": False}, daemon=True).start()

    def _cancel_options_autohide(self):
        aid = getattr(self, "_options_autohide_after_id", None)
        if aid is not None:
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
            self._options_autohide_after_id = None
        self.options_autohide_remaining = 0

    def reset_options_autohide_timer(self, event=None):
        if not getattr(self, "options_visible", False) or getattr(self, "needs_manager_url_setup", False):
            self._cancel_options_autohide()
            return
        now = time.time()
        if event is not None and getattr(event, "type", None) == tk.EventType.Motion:
            if (now - getattr(self, "_options_last_reset_ts", 0.0)) < 1.0 and self.options_autohide_remaining > 10:
                return
        self._options_last_reset_ts = now
        was_counting = 0 < getattr(self, "options_autohide_remaining", 0) <= 10
        self.options_autohide_remaining = 60
        if getattr(self, "_options_autohide_after_id", None) is None:
            try:
                self._options_autohide_after_id = self.root.after(1000, self._tick_options_autohide)
            except Exception:
                self._options_autohide_after_id = None
        if was_counting:
            self.update_opts_btn_label()

    def _tick_options_autohide(self):
        self._options_autohide_after_id = None
        if not self.root.winfo_exists():
            return
        if not self.options_visible or getattr(self, "needs_manager_url_setup", False):
            self.options_autohide_remaining = 0
            self.update_opts_btn_label()
            return
        self.options_autohide_remaining = max(0, int(self.options_autohide_remaining or 0) - 1)
        if self.options_autohide_remaining <= 0:
            self.options_visible = False
            self.settings_drawer.pack_forget()
            self.update_opts_btn_label()
            self.apply_window_geometry()
            return
        if self.options_autohide_remaining <= 10:
            self.update_opts_btn_label()
        try:
            self._options_autohide_after_id = self.root.after(1000, self._tick_options_autohide)
        except Exception:
            self._options_autohide_after_id = None

    def update_opts_btn_label(self):
        if not self.root.winfo_exists():
            return
        if self.options_visible:
            rem = int(getattr(self, "options_autohide_remaining", 0) or 0)
            if 1 <= rem <= 10:
                self.opts_btn.config(text=f"{wc.T('w_hide_options')} ({rem}s)")
            else:
                self.opts_btn.config(text=wc.T('w_hide_options'))
        else:
            self.opts_btn.config(text=wc.T('w_options'))

    def refresh_tray_and_window_icon(self, force: bool = False):
        with self.jobs_lock:
            act_list = list(self.active_jobs.values())
        with self.held_lock:
            held_cnt = len(self.held_jobs)

        act_cnt = len(act_list)
        any_paused = any(d.get("user_paused") or d.get("disk_paused") for d in act_list)
        any_io = any((d.get("speed_mbps") or 0) > 0 and (d.get("fps_current") or 0) == 0 for d in act_list)

        init_checked = bool(getattr(self, "_initial_conn_checked", False))
        hs = getattr(self, "handshake_state", {}) or {}
        storage_failed = bool(hs.get("verified") and not (self.is_storage_ok() or self.is_http_relay_active()))

        if not init_checked and not self.is_online_state:
            # Při startu aplikace (dokud ještě neproběhl první test spojení s Managerem)
            # nikdy nenastavujeme červenou chybovou ikonu s vykřičníkem [!], ale výchozí ikonu aplikace.
            bg_h, brd_h, sym = "#0b1626", "#22d3ee", "HBW"
            dot_h = wc.TITLE_DOT_COLORS["ok"]
            st_tip = wc.T('w_ready_awaiting_manager')
        elif not self.is_online_state or storage_failed:
            bg_h, brd_h, sym = "#e11d48", "#fda4af", "!"
            dot_h = wc.TITLE_DOT_COLORS["offline"]
            st_tip = wc.T('w_offline') if not self.is_online_state else wc.T('w_manager_online_storage_unreachable')
        elif any_paused or held_cnt > 0 or not self.accept_jobs:
            bg_h, brd_h = "#d97706", "#fde68a"
            sym = wc.tray_symbol_for_count(act_cnt) or "I"
            dot_h = wc.TITLE_DOT_COLORS["paused"]
            st_tip = wc.T('w_paused_waiting_disk')
        elif act_cnt > 0:
            bg_h, brd_h = "#059669", "#6ee7b7"
            sym = "↓" if (any_io and act_cnt == 1) else wc.tray_symbol_for_count(act_cnt)
            dot_h = wc.TITLE_DOT_COLORS["ok"]
            st_tip = wc.T('w_active', act_cnt)
        else:
            bg_h, brd_h, sym = "#0b1626", "#22d3ee", "HBW"
            dot_h = wc.TITLE_DOT_COLORS["ok"]
            st_tip = wc.T('w_online_idle')

        w_name = self.config.get("worker_name", "Worker")
        tip_full = f"HB Worker [{w_name}] v{CURRENT_VERSION} — {st_tip}"
        sig = (bg_h, brd_h, sym, dot_h)
        sig_changed = force or (sig != getattr(self, "_last_win_icon_sig", None))

        if hasattr(self, "tray_icon") and self.tray_icon:
            png_b64 = self.tray_icon.update_status_visual(bg_h, brd_h, sym, tip_full)
            if png_b64 or sig_changed:
                self._last_win_icon_sig = sig
                if not png_b64:
                    _, b64_32 = render_tray_status_icon(bg_h, brd_h, sym, 32)
                    _, b64_48 = render_tray_status_icon(bg_h, brd_h, sym, 48)
                    png_b64 = (b64_48, b64_32)
                dot_pngs = []
                for d_sz in (24, 20, 16):
                    try:
                        dot_pngs.append(wc.render_title_dot_icon(dot_h, d_sz)[1])
                    except Exception:
                        pass
                if isinstance(png_b64, tuple):
                    png_b64 = tuple(list(png_b64) + dot_pngs)

                def _set_win_ico(b64_pair, _bg=bg_h, _brd=brd_h, _sym=sym, _dot=dot_h):
                    try:
                        if not self.root.winfo_exists():
                            return
                        if isinstance(b64_pair, tuple):
                            imgs = [tk.PhotoImage(data=b) for b in b64_pair if b]
                            if imgs:
                                self._tk_win_icon_ref = imgs
                                self.root.wm_iconphoto(True, *imgs)
                        else:
                            self._tk_win_icon_ref = tk.PhotoImage(data=b64_pair)
                            self.root.wm_iconphoto(True, self._tk_win_icon_ref)

                        if os.name == "nt" and hasattr(self, "tray_icon") and self.tray_icon:
                            bgra_sm, _ = wc.render_title_dot_icon(_dot, 16)
                            bgra_lg, _ = render_tray_status_icon(_bg, _brd, _sym, 48)
                            new_sm = self.tray_icon._create_win32_hicon(bgra_sm, 16)
                            new_lg = self.tray_icon._create_win32_hicon(bgra_lg, 48)
                            u32 = ctypes.windll.user32
                            u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                            u32.SendMessageW.restype = wintypes.LPARAM
                            u32.GetParent.argtypes = [wintypes.HWND]
                            u32.GetParent.restype = wintypes.HWND
                            hwnd_self = self.root.winfo_id()
                            hwnd_parent = u32.GetParent(hwnd_self)
                            for hw in (hwnd_self, hwnd_parent):
                                if hw:
                                    if new_sm:
                                        u32.SendMessageW(hw, 0x0080, 0, new_sm)  # WM_SETICON, ICON_SMALL
                                    if new_lg:
                                        u32.SendMessageW(hw, 0x0080, 1, new_lg)  # WM_SETICON, ICON_BIG
                            old_sm = getattr(self, "_win_hicon_small", None)
                            old_lg = getattr(self, "_win_hicon_big", None)
                            self._win_hicon_small = new_sm
                            self._win_hicon_big = new_lg
                            u32.DestroyIcon.argtypes = [wintypes.HICON]
                            u32.DestroyIcon.restype = wintypes.BOOL
                            if old_sm and old_sm != new_sm:
                                u32.DestroyIcon(old_sm)
                            if old_lg and old_lg != new_lg:
                                u32.DestroyIcon(old_lg)
                    except Exception:
                        pass
                self.run_in_main_thread(_set_win_ico, png_b64)

    def _populate_main_lang_menu(self):
        if not hasattr(self, "lang_menu") or not self.lang_menu:
            return
        try:
            self.lang_menu.delete(0, tk.END)
            available = wc.get_available_langs() if hasattr(wc, "get_available_langs") else [
                {"code": "cs", "label": "Čeština (CZ)"},
                {"code": "en", "label": "English (EN)"}
            ]
            curr = (wc.get_worker_lang() or "cs").lower()
            for l_ent in available:
                c_code = str(l_ent.get("code") or "").lower()
                c_label = str(l_ent.get("label") or c_code.upper())
                prefix = "✓  " if c_code == curr else "    "
                self.lang_menu.add_command(
                    label=f"{prefix}{c_label}",
                    command=lambda c=c_code: self.set_ui_language(c)
                )
        except Exception:
            pass

    def set_ui_language(self, lang_code: str):
        if not lang_code:
            return
        wc.set_worker_lang(lang_code)
        self.config["ui_lang"] = lang_code
        self.config["lang"] = lang_code
        self.pending_lang_override = lang_code
        self.save_tray_prefs()
        self.refresh_ui_language()
        self.log(wc.T('w_interface_language_switched_englis'))

        def _push_lang(lg):
            try:
                srv = self.config.get("server_url", "").rstrip("/")
                r = api_request("POST", f"{srv}/api/v2/cluster/action", json={"action": "update_settings", "settings": {"ui_lang": lg}}, timeout=5)
                if r.status_code == 200 and self.pending_lang_override == lg:
                    self.pending_lang_override = None
            except Exception:
                pass

        threading.Thread(target=_push_lang, args=(lang_code,), daemon=True).start()

    def refresh_ui_language(self):
        def _upd():
            if not self.root.winfo_exists():
                return
            lang = wc.get_worker_lang() or "cs"
            curr_l = lang.upper()
            if hasattr(self, "lang_mbtn") and self.lang_mbtn:
                self.lang_mbtn.config(text=f"{curr_l} ▾")
            self._populate_main_lang_menu()
            self.refresh_kofi_button()
            self.lbl_url.config(text="Manager URL:")
            self.find_mgr_btn.config(text=wc.T('w_find'))
            if hasattr(self, "lbl_quick_url_prompt"):
                self.lbl_quick_url_prompt.config(text=wc.T('w_quick_url_prompt'))
                self.btn_quick_url_save.config(text=wc.T('w_quick_url_save'))
                self.btn_quick_url_find.config(text=wc.T('w_find'))
            self.lbl_name.config(text=wc.T('w_worker_name'))
            self.lbl_in.config(text=wc.T('w_inputs_folder'))
            self.lbl_out.config(text=wc.T('w_outputs_folder'))
            self.btn_browse_in.config(text=wc.T('w_browse'))
            self.btn_browse_out.config(text=wc.T('w_browse'))
            self.save_btn.config(text=wc.T('w_save_test'))
            self.pwr_btn.config(text=wc.T('w_power'))
            self.update_opts_btn_label()
            self.quit_btn.config(text=wc.T('w_quit'))
            self.chk_start_tray.config(text=wc.T('w_start_minimized_system_tray'))
            self.chk_skip_prompt.config(text=wc.T('w_hide_tray_x_without'))
            self.chk_show_log.config(text=wc.T('w_show_log_console'))
            if hasattr(self, "uninstall_btn"):
                self.uninstall_btn.config(text=wc.T('w_uninstall_worker'))
            if hasattr(self, "open_folder_btn"):
                self.open_folder_btn.config(text=wc.T('w_open_worker_folder'))
            if hasattr(self, "lbl_sleep_mode"):
                self.lbl_sleep_mode.config(text=wc.T('w_sleep_mode_lbl'))
                self.rb_sleep_encoding.config(text=wc.T('w_sleep_encoding'))
                self.rb_sleep_always.config(text=wc.T('w_sleep_always'))
                self.rb_sleep_off.config(text=wc.T('w_sleep_off'))
                if hasattr(self, "chk_wake_check"):
                    self.chk_wake_check.config(text=wc.T('w_wake_check'))
            if hasattr(self, "lbl_custom_cpu"):
                self.lbl_custom_cpu.config(text=wc.T('w_custom_cpu_lbl'))
            if hasattr(self, "btn_cpu_p_only"):
                self.btn_cpu_p_only.config(text=wc.T('w_cpu_preset_p'))
            if hasattr(self, "btn_cpu_e_only"):
                self.btn_cpu_e_only.config(text=wc.T('w_cpu_preset_e'))
            if hasattr(self, "btn_cpu_no_smt"):
                self.btn_cpu_no_smt.config(text=wc.T('w_cpu_preset_no_smt'))
            if hasattr(self, "btn_cpu_all"):
                self.btn_cpu_all.config(text=wc.T('w_cpu_preset_all'))
            if hasattr(self, "lbl_shortcuts"):
                self.lbl_shortcuts.config(text=wc.T('w_win_shortcuts_lbl'))
            if hasattr(self, "chk_desk_sc"):
                self.chk_desk_sc.config(text=wc.T('w_desk_shortcut_chk'))
            if hasattr(self, "chk_smenu_sc"):
                self.chk_smenu_sc.config(text=wc.T('w_smenu_shortcut_chk'))
            if hasattr(self, "lbl_autostart"):
                self.lbl_autostart.config(text=wc.T('w_autostart_lbl'))
                self.rb_auto_off.config(text=wc.T('w_autostart_off_short'))
                self.rb_auto_after.config(text=wc.T('w_autostart_after_short'))
                self.rb_auto_before.config(text=wc.T('w_autostart_before_short'))
                self.update_autostart_desc_label()
                self.update_autostart_status_ui()
            self._job_table_headings["file"] = wc.T('w_processed_file')
            self._job_table_headings["prog"] = wc.T('w_progress')
            self._job_table_headings["fps"] = wc.T('w_speed_fps')
            self._job_table_headings["elapsed"] = wc.T('w_elapsed')
            self._job_table_headings["eta"] = wc.T('w_remaining_eta')
            self._redraw_job_table()
            self.update_accept_btn_ui()
            self.update_pause_encode_btn_ui()
            self.set_connection_status(self.is_online_state)
            self.apply_current_power_state_now()
            self.root.after(40, self._ensure_header_width)

        self.run_in_main_thread(_upd)

    def sync_worker_lang(self, lang_code: str):
        if not lang_code:
            return
        if self.pending_lang_override and lang_code != self.pending_lang_override:
            return
        if self.pending_lang_override == lang_code:
            self.pending_lang_override = None
        if wc.get_worker_lang() != lang_code:
            wc.set_worker_lang(lang_code)
            self.config["ui_lang"] = lang_code
            self.config["lang"] = lang_code
            self.save_tray_prefs()
            self.refresh_ui_language()

    def toggle_ui_language(self):
        next_lang = "cs" if wc.get_worker_lang() == "en" else "en"
        self.set_ui_language(next_lang)

    def discover_manager_clicked(self):
        self.reset_options_autohide_timer()
        self.log(wc.T('w_searching_hb_cluster_manager'))

        def _run_disc():
            disc_url, resp = wc.discover_manager_udp()
            if disc_url:
                def _apply():
                    self.url_var.set(disc_url)
                    self.config["server_url"] = disc_url
                    if resp and resp.get("ui_lang"):
                        self.sync_worker_lang(resp.get("ui_lang"))
                    self.save_config()
                    self.log(wc.T('w_manager_found_at_v', disc_url, resp.get('version', '?')))
                self.run_in_main_thread(_apply)
            else:
                self.log(wc.T('w_no_manager_responded_udp'))

        threading.Thread(target=_run_disc, daemon=True).start()

    def toggle_options_bar(self):
        self.options_visible = not self.options_visible
        if self.options_visible:
            self.settings_drawer.pack(fill=tk.X, pady=(6, 0))
            self.reset_options_autohide_timer()
        else:
            self._cancel_options_autohide()
            self.settings_drawer.pack_forget()
        self.update_opts_btn_label()
        self.apply_window_geometry()

    def toggle_log_panel(self):
        self.reset_options_autohide_timer()
        self.log_visible = bool(self.show_log_var.get())
        self.config["show_log"] = self.log_visible
        self.save_tray_prefs()
        if self.log_visible:
            self.log_container.pack(fill=tk.BOTH, expand=False)
        else:
            self.log_container.pack_forget()
        self.apply_window_geometry()

    def _get_autostart_command_str(self, extra_arg: str = "") -> str:
        if os.path.isfile(LOCAL_LAUNCHER_EXE):
            base = f'"{LOCAL_LAUNCHER_EXE}"'
        else:
            py_exec = sys.executable
            cfg_py = str(self.config.get("python_path") or "").strip() if hasattr(self, "config") and isinstance(self.config, dict) else ""
            if cfg_py and "windowsapps" not in cfg_py.lower() and os.path.isfile(cfg_py):
                cand_pyw = os.path.join(os.path.dirname(cfg_py), "pythonw.exe")
                py_exec = cand_pyw if os.path.isfile(cand_pyw) else cfg_py
            base = f'"{py_exec}" "{os.path.abspath(__file__)}"'
        return f"{base} {extra_arg}".strip()

    def get_autostart_mode(self) -> str:
        m = str(self.config.get("autostart_mode") or "").strip().lower()
        if m in ("off", "after_login", "before_login"):
            return m
        if self.config.get("run_before_login"):
            return "before_login"
        if self.config.get("run_on_startup"):
            return "after_login"
        return "off"

    def update_autostart_desc_label(self):
        if not hasattr(self, "lbl_autostart_desc"):
            return
        m = self.autostart_mode_var.get() if hasattr(self, "autostart_mode_var") else self.get_autostart_mode()
        if m == "after_login":
            txt = f"ℹ {wc.T('autostart_after_login_title')}: {wc.T('autostart_after_login_desc')}"
            col = "#38bdf8"
        elif m == "before_login":
            txt = f"ℹ {wc.T('autostart_before_login_title')}: {wc.T('autostart_before_login_desc')}"
            col = "#34d399"
        else:
            txt = f"ℹ {wc.T('autostart_off_title')}: {wc.T('autostart_off_desc')}"
            col = "#94a3b8"
        self.lbl_autostart_desc.config(text=txt, fg=col)

    def _build_autostart_help_text(self) -> str:
        return (
            f"• {wc.T('autostart_off_title')}\n"
            f"  {wc.T('autostart_off_desc')}\n\n"
            f"• {wc.T('autostart_after_login_title')}\n"
            f"  {wc.T('autostart_after_login_desc')}\n\n"
            f"• {wc.T('autostart_before_login_title')}\n"
            f"  {wc.T('autostart_before_login_desc')}"
        )

    def _show_autostart_tooltip(self, event=None):
        self._hide_autostart_tooltip()
        try:
            tw = tk.Toplevel(self.root)
            tw.wm_overrideredirect(True)
            tw.attributes("-topmost", True)
            tw.configure(bg="#38bdf8", padx=1, pady=1)
            x = (event.x_root + 12) if event else (self.root.winfo_rootx() + 80)
            y = (event.y_root + 18) if event else (self.root.winfo_rooty() + 180)
            tw.wm_geometry(f"+{x}+{y}")
            inner = tk.Frame(tw, bg="#0f172a", padx=12, pady=10)
            inner.pack(fill=tk.BOTH, expand=True)
            tk.Label(
                inner, text=wc.T('add_wrk_autostart_hdr'),
                bg="#0f172a", fg="#38bdf8", font=("Segoe UI", 9, "bold"), anchor=tk.W
            ).pack(fill=tk.X, pady=(0, 6))
            tk.Label(
                inner, text=self._build_autostart_help_text(),
                bg="#0f172a", fg="#f1f5f9", font=("Segoe UI", 8), justify=tk.LEFT, wraplength=540
            ).pack(fill=tk.X)
            self._autostart_tip_win = tw
        except Exception:
            pass

    def _hide_autostart_tooltip(self, event=None):
        if getattr(self, "_autostart_tip_win", None) is not None:
            try:
                self._autostart_tip_win.destroy()
            except Exception:
                pass
            self._autostart_tip_win = None

    def show_autostart_help_dialog(self, event=None):
        self._hide_autostart_tooltip()
        self.reset_options_autohide_timer()
        messagebox.showinfo(wc.T('add_wrk_autostart_hdr'), self._build_autostart_help_text())

    def _is_evening_kofi(self) -> bool:
        try:
            hr = datetime.datetime.now().hour
            return hr >= 18 or hr < 5
        except Exception:
            return False

    def _kofi_tooltip_text(self) -> str:
        return wc.T('support_tip_beer') if self._is_evening_kofi() else wc.T('support_tip_coffee')

    def _draw_kofi_canvas(self, hover: bool = False):
        if not hasattr(self, "kofi_btn") or not self.kofi_btn.winfo_exists():
            return
        self._kofi_hover = bool(hover)
        c = self.kofi_btn
        c.delete("all")
        img = self._get_kofi_image(self._is_evening_kofi(), self._kofi_hover)
        if img is not None:
            self._kofi_active_img = img
            c.create_image(14, 13, image=img, anchor=tk.CENTER)

    def _on_kofi_enter(self, event=None):
        self._draw_kofi_canvas(True)
        self._show_kofi_tooltip(event)

    def _on_kofi_leave(self, event=None):
        self._draw_kofi_canvas(False)
        self._hide_kofi_tooltip(event)

    def refresh_kofi_button(self):
        try:
            if hasattr(self, "kofi_btn") and self.kofi_btn.winfo_exists():
                self._draw_kofi_canvas(getattr(self, "_kofi_hover", False))
        except Exception:
            pass

    def _tick_kofi_icon(self):
        try:
            if self.root.winfo_exists():
                self.refresh_kofi_button()
                self.root.after(60000, self._tick_kofi_icon)
        except Exception:
            pass

    def open_kofi_support(self):
        self._hide_kofi_tooltip()
        try:
            webbrowser.open(KOFI_URL)
        except Exception as e:
            self.log(f"Ko-fi open error: {e}")

    def _show_title_tooltip(self, event=None):
        self._show_generic_tooltip(wc.T('w_title_ecosystem_tip'), event, border_col="#38bdf8", fg_col="#bae6fd")

    def _show_kofi_tooltip(self, event=None):
        self._show_generic_tooltip(self._kofi_tooltip_text(), event, border_col="#fbbf24", fg_col="#fde68a")

    def _show_generic_tooltip(self, text_val: str, event=None, border_col: str = "#fbbf24", fg_col: str = "#fde68a"):
        self._hide_kofi_tooltip()
        try:
            tw = tk.Toplevel(self.root)
            tw.wm_overrideredirect(True)
            tw.attributes("-topmost", True)
            tw.configure(bg=border_col, padx=1, pady=1)
            x = (event.x_root - 120) if event else (self.root.winfo_rootx() + 560)
            y = (event.y_root + 18) if event else (self.root.winfo_rooty() + 44)
            tw.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
            inner = tk.Frame(tw, bg="#0f172a", padx=10, pady=6)
            inner.pack(fill=tk.BOTH, expand=True)
            tk.Label(
                inner, text=text_val,
                bg="#0f172a", fg=fg_col, font=("Segoe UI", 8, "bold"), justify=tk.LEFT
            ).pack(fill=tk.X)
            self._kofi_tip_win = tw
        except Exception:
            pass

    def _hide_kofi_tooltip(self, event=None):
        if getattr(self, "_kofi_tip_win", None) is not None:
            try:
                self._kofi_tip_win.destroy()
            except Exception:
                pass
            self._kofi_tip_win = None

    def update_autostart_status_ui(self):
        """Ukáže pod volbou spouštění varování a tlačítko opravy, když úloha chybí, je stará nebo čeká UAC."""
        if not hasattr(self, "autostart_warn_frame") or not self.root.winfo_exists():
            return
        mode = self.get_autostart_mode()
        task_st = str(getattr(self, "_autostart_task_state", "unknown") or "unknown").lower()
        op_st = str(getattr(self, "_autostart_status", "") or "").lower()
        op_err = str(getattr(self, "_autostart_last_error", "") or "")
        can_elev = getattr(self, "_can_elevate", None)

        msg = ""
        btn_txt = ""
        btn_enabled = True
        tone = "warn"

        if op_st == "uac_pending":
            msg = wc.T("w_autostart_uac_pending_local")
            btn_txt = wc.T("w_autostart_repair_btn" if mode == "before_login" else "w_autostart_remove_btn")
            btn_enabled = False
            tone = "info"
        elif op_st == "uac_cancelled":
            msg = wc.T("w_autostart_uac_cancelled_local")
            btn_txt = wc.T("w_autostart_repair_btn" if mode == "before_login" else "w_autostart_remove_btn")
        elif op_st == "uac_no_desktop":
            msg = wc.T("wrk_autostart_uac_no_desktop", self.config.get("worker_name", ""))
        elif op_st == "no_admin" or (mode == "before_login" and task_st != "ok" and can_elev is False):
            msg = wc.T("wrk_cap_before_login_no_admin")
            tone = "err"
        elif op_st == "failed":
            msg = wc.T("wrk_cap_autostart_failed", op_err or "?")
            btn_txt = wc.T("w_autostart_repair_btn" if mode == "before_login" else "w_autostart_remove_btn")
            tone = "err"
        elif mode == "before_login" and task_st == "none":
            msg = wc.T("w_autostart_task_missing_local")
            btn_txt = wc.T("w_autostart_repair_btn")
        elif mode == "before_login" and task_st == "legacy":
            msg = wc.T("w_autostart_task_legacy_local")
            btn_txt = wc.T("w_autostart_repair_btn")
        elif mode != "before_login" and task_st in ("ok", "legacy"):
            msg = wc.T("w_autostart_task_leftover_local")
            btn_txt = wc.T("w_autostart_remove_btn")

        was_shown = bool(self.autostart_warn_frame.winfo_ismapped())
        if msg:
            pal = {
                "info": ("#0c2238", "#0284c7", "#bae6fd"),
                "warn": ("#2b1d0e", "#b45309", "#fcd34d"),
                "err": ("#2b1116", "#be123c", "#fda4af"),
            }[tone]
            self.autostart_warn_frame.config(bg=pal[0], highlightbackground=pal[1])
            self.lbl_autostart_warn.config(text=msg, bg=pal[0], fg=pal[2])
            if btn_txt and can_elev is not False:
                self.btn_autostart_repair.config(
                    text=btn_txt,
                    state=(tk.NORMAL if btn_enabled else tk.DISABLED),
                    bg=("#d97706" if btn_enabled else "#334155"),
                )
                if not self.btn_autostart_repair.winfo_ismapped():
                    self.btn_autostart_repair.pack(side=tk.RIGHT, padx=(8, 0))
            else:
                self.btn_autostart_repair.pack_forget()
            if not was_shown:
                self.autostart_warn_frame.pack(fill=tk.X, pady=(4, 1))
                if getattr(self, "options_visible", False):
                    self.apply_window_geometry()
        else:
            if was_shown:
                self.autostart_warn_frame.pack_forget()
                if getattr(self, "options_visible", False):
                    self.apply_window_geometry()

    def repair_autostart_task_clicked(self):
        self.reset_options_autohide_timer()
        mode = self.get_autostart_mode()
        threading.Thread(target=self.apply_autostart_mode_in_os, args=(mode, True, True, True), daemon=True).start()

    def on_autostart_mode_changed(self):
        self.reset_options_autohide_timer()
        mode = self.autostart_mode_var.get()
        if mode not in ("off", "after_login", "before_login"):
            mode = "off"
        self._autostart_mode_val = mode
        self.config["autostart_mode"] = mode
        self.config["run_on_startup"] = (mode == "after_login")
        self.config["run_before_login"] = (mode == "before_login")
        self.update_autostart_desc_label()
        self.save_tray_prefs()
        threading.Thread(target=self.apply_autostart_mode_in_os, args=(mode, True, True, False), daemon=True).start()

    def apply_autostart_mode_in_os(self, mode: str, log_change: bool = True, allow_uac: bool = False, force_task: bool = False):
        with self._autostart_op_lock:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                if mode == "after_login":
                    winreg.SetValueEx(key, "HBClusterWorker", 0, winreg.REG_SZ, self._get_autostart_command_str("--autostart-login"))
                elif mode == "before_login":
                    winreg.SetValueEx(key, "HBClusterWorker", 0, winreg.REG_SZ, self._get_autostart_command_str("--desktop-takeover"))
                else:
                    my_root = wc.win_install_root(APP_ROOT_PATH) if wc else ""
                    run_ours = (not wc or not my_root) or wc.win_ref_is_ours_or_stale(_read_run_key_command(), my_root)
                    if run_ours:
                        try:
                            winreg.DeleteValue(key, "HBClusterWorker")
                        except FileNotFoundError:
                            pass
                winreg.CloseKey(key)
            except Exception as e:
                if log_change:
                    self.log(wc.T("w_autostart_reg_warn", str(e)))

            py_cfg = str(self.config.get("python_path") or "").strip()
            try:
                cur_task = inspect_autostart_task(base_dir=APP_ROOT_PATH, py_exe=py_cfg)
                if mode == "before_login":
                    if cur_task == "ok" and not force_task:
                        self._autostart_status = ""
                        self._autostart_last_error = ""
                        self._autostart_task_state = "ok"
                        if log_change:
                            self.log(wc.T("w_autostart_before_login_active"))
                    elif not allow_uac and not _is_windows_process_elevated():
                        # Tichá kontrola při startu: UAC neotvíráme, jen ukážeme upozornění + tlačítko opravy
                        self._autostart_task_state = cur_task
                        self._autostart_status = ""
                        self._autostart_last_error = ""
                    else:
                        if getattr(self, "_can_elevate", None) is None:
                            self._can_elevate = windows_user_can_elevate()
                        if not _is_windows_process_elevated() and not self._can_elevate:
                            self._autostart_status = "no_admin"
                            self._autostart_last_error = ""
                            self._autostart_task_state = cur_task
                            if log_change:
                                self.log(wc.T("wrk_cap_before_login_no_admin"))
                        else:
                            def _mark_uac():
                                self._autostart_status = "uac_pending"
                                self._autostart_last_error = ""
                                self._caps_cache = None
                                self.run_in_main_thread(self.update_autostart_status_ui)

                            st, det = _create_before_login_task(
                                APP_ROOT_PATH, py_exe=py_cfg, allow_uac=allow_uac, on_uac_start=_mark_uac
                            )
                            new_task = inspect_autostart_task(base_dir=APP_ROOT_PATH, py_exe=py_cfg)
                            self._autostart_task_state = new_task
                            if st == "ok":
                                self._autostart_status = "" if new_task in ("ok", "unknown") else "failed"
                                self._autostart_last_error = "" if self._autostart_status == "" else f"task state={new_task}"
                                if log_change:
                                    self.log(wc.T("w_autostart_before_login_active"))
                            elif st == "uac_cancelled":
                                self._autostart_status = "uac_cancelled"
                                self._autostart_last_error = ""
                                if log_change:
                                    self.log(wc.T("w_autostart_uac_cancelled_log"))
                            elif st == "uac_no_desktop":
                                self._autostart_status = "uac_no_desktop"
                                self._autostart_last_error = ""
                                if log_change:
                                    self.log(wc.T("w_autostart_uac_no_desktop_log"))
                            elif st == "no_admin":
                                self._autostart_status = "no_admin"
                                self._autostart_last_error = ""
                            else:
                                self._autostart_status = "failed"
                                self._autostart_last_error = str(det or "schtasks failed")[:300]
                                if log_change:
                                    self.log(wc.T("w_autostart_task_warn", self._autostart_last_error))
                else:
                    if cur_task == "none":
                        self._autostart_status = ""
                        self._autostart_last_error = ""
                        self._autostart_task_state = "none"
                        if log_change:
                            self.log(wc.T("w_autostart_after_login_active" if mode == "after_login" else "w_autostart_off"))
                    elif not allow_uac and not _is_windows_process_elevated():
                        self._autostart_task_state = cur_task
                        self._autostart_status = ""
                        self._autostart_last_error = ""
                    else:
                        my_root = wc.win_install_root(APP_ROOT_PATH) if wc else ""
                        task_cmd = _read_autostart_task_command()
                        task_ours = (wc.win_ref_is_ours_or_stale(task_cmd, my_root) if (task_cmd and wc and my_root) else (not _find_other_install_root(APP_ROOT_PATH)))
                        if not task_ours:
                            self._autostart_status = ""
                            self._autostart_last_error = ""
                            self._autostart_task_state = "none"
                            if log_change:
                                self.log(wc.T("w_autostart_after_login_active" if mode == "after_login" else "w_autostart_off"))
                        else:
                            def _mark_uac_del():
                                self._autostart_status = "uac_pending"
                                self._autostart_last_error = ""
                                self._caps_cache = None
                                self.run_in_main_thread(self.update_autostart_status_ui)

                            st, det = _delete_before_login_task(allow_uac=allow_uac, on_uac_start=_mark_uac_del)
                            new_task = inspect_autostart_task(base_dir=APP_ROOT_PATH, py_exe=py_cfg)
                            self._autostart_task_state = new_task
                            if st == "ok":
                                self._autostart_status = ""
                                self._autostart_last_error = ""
                                if log_change:
                                    self.log(wc.T("w_autostart_after_login_active" if mode == "after_login" else "w_autostart_off"))
                            elif st == "uac_cancelled":
                                self._autostart_status = "uac_cancelled"
                                self._autostart_last_error = ""
                                if log_change:
                                    self.log(wc.T("w_autostart_uac_cancelled_log"))
                            elif st == "uac_no_desktop":
                                self._autostart_status = "uac_no_desktop"
                                self._autostart_last_error = ""
                                if log_change:
                                    self.log(wc.T("w_autostart_uac_no_desktop_log"))
                            else:
                                self._autostart_status = "failed"
                                self._autostart_last_error = str(det or "schtasks failed")[:300]
                                if log_change:
                                    self.log(wc.T("w_autostart_task_warn", self._autostart_last_error))
            except Exception as e:
                self._autostart_status = "failed"
                self._autostart_last_error = str(e)[:300]
                if log_change:
                    self.log(wc.T("w_autostart_task_warn", str(e)))
            if isinstance(getattr(self, "_priv_test", None), dict):
                self._priv_test["task"] = self._autostart_task_state
            self._caps_cache = None
            self.run_in_main_thread(self.update_autostart_status_ui)

    def get_windows_settings_state(self) -> dict:
        return {
            "autostart_mode": self.get_autostart_mode(),
            "smb_server": str(self.config.get("smb_server") or _extract_unc_host_from_paths(self.get_inputs_dir(), self.get_outputs_dir()) or "").strip(),
            "smb_user": str(self.config.get("smb_user") or self.config.get("smb_username") or "").strip(),
            "smb_pass": str(self.config.get("smb_pass") or self.config.get("smb_password") or ""),
            "desktop_shortcut": bool(self.config.get("desktop_shortcut", True)),
            "start_menu_shortcut": bool(self.config.get("start_menu_shortcut", True)),
            "tray_minimize": bool(self.config.get("start_in_tray", self.config.get("tray_minimize", False))),
            "sleep_prevention_mode": str(getattr(self, "sleep_prevention_mode", "encoding") or "encoding"),
            "skip_tray_prompt": bool(self.config.get("skip_tray_prompt", False)),
            "show_log": bool(getattr(self, "log_visible", False)),
            "wake_check": bool(getattr(self, "wake_check", True)),
            "caps": self.get_platform_caps(),
            "ack_ts": int(getattr(self, "last_windows_settings_ack_ts", 0) or 0),
        }

    def get_platform_caps(self) -> dict:
        """Co tento počítač umí – Manager podle toho šedne volby a vysvětlí proč (obnovuje se po 60 s)."""
        now_t = time.time()
        cache = getattr(self, "_caps_cache", None)
        if cache and now_t - cache[0] < 60:
            caps = dict(cache[1])
        else:
            caps = read_power_capabilities()
            caps["wake_timers"] = read_wake_timers_setting()
            if getattr(self, "_can_elevate", None) is None:
                self._can_elevate = windows_user_can_elevate()
            caps["can_elevate"] = bool(self._can_elevate)
            try:
                caps["is_elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                caps["is_elevated"] = False
            self._caps_cache = (now_t, dict(caps))
        caps["on_ac"] = is_on_ac_power()
        caps["autostart_status"] = str(getattr(self, "_autostart_status", "") or "")[:40]
        caps["autostart_task"] = str(getattr(self, "_autostart_task_state", "") or (self._priv_test.get("task") if isinstance(getattr(self, "_priv_test", None), dict) else "") or "unknown")
        caps["autostart_error"] = str(getattr(self, "_autostart_last_error", "") or "")[:300]
        if isinstance(getattr(self, "_priv_test", None), dict):
            caps["priv"] = dict(self._priv_test)      # W3: výsledek kontroly práv při startu
        return caps

    def apply_remote_windows_settings(self, pend: dict):
        if not isinstance(pend, dict):
            return
        req_ts = int(pend.get("req_ts") or pend.get("updated_at") or int(time.time()))
        if req_ts <= int(getattr(self, "_last_applied_win_settings_ts", 0) or 0):
            return
        self._last_applied_win_settings_ts = req_ts
        self.last_windows_settings_ack_ts = req_ts

        prev_mode = self.get_autostart_mode()
        mode = str(pend.get("autostart_mode") or prev_mode or "off").strip().lower()
        if mode not in ("off", "after_login", "before_login"):
            mode = "off"
        force_repair = bool(pend.get("force_task_repair"))
        desk_lnk = bool(pend.get("desktop_shortcut", self.config.get("desktop_shortcut", True)))
        smenu_lnk = bool(pend.get("start_menu_shortcut", self.config.get("start_menu_shortcut", True)))
        tray_min = bool(pend.get("tray_minimize", self.config.get("start_in_tray", False)))
        sleep_m = str(pend.get("sleep_prevention_mode") or getattr(self, "sleep_prevention_mode", "encoding") or "encoding").strip().lower()
        if sleep_m not in ("encoding", "always", "off"):
            sleep_m = "encoding"
        skip_prompt = bool(pend.get("skip_tray_prompt", self.config.get("skip_tray_prompt", False)))
        show_log = bool(pend.get("show_log", getattr(self, "log_visible", False)))
        wake_chk = bool(pend.get("wake_check", getattr(self, "wake_check", True)))

        self._autostart_mode_val = mode
        self._start_tray_val = tray_min
        self._skip_prompt_val = skip_prompt
        self.config["autostart_mode"] = mode
        self.config["run_on_startup"] = (mode == "after_login")
        self.config["run_before_login"] = (mode == "before_login")
        self.config["desktop_shortcut"] = desk_lnk
        self.config["start_menu_shortcut"] = smenu_lnk
        self.config["tray_minimize"] = tray_min
        self.config["start_in_tray"] = tray_min
        self.config["sleep_prevention_mode"] = sleep_m
        self.config["skip_tray_prompt"] = skip_prompt
        self.config["show_log"] = show_log
        self.config["wake_check"] = wake_chk
        self.sleep_prevention_mode = sleep_m
        self.wake_check = wake_chk
        if pend.get("smb_server") is not None:
            self.config["smb_server"] = str(pend.get("smb_server") or "").strip()
        if pend.get("smb_user") is not None:
            u_val = str(pend.get("smb_user") or "").strip()
            if u_val:
                self.config["smb_user"] = u_val
                self.config["smb_username"] = u_val
        if pend.get("smb_pass") is not None:
            p_val = str(pend.get("smb_pass") or "")
            if p_val:
                self.config["smb_pass"] = p_val
                self.config["smb_password"] = p_val

        self._atomic_save_config()

        def _sync_local_ui():
            if not self.root.winfo_exists():
                return
            if hasattr(self, "autostart_mode_var"):
                self.autostart_mode_var.set(mode)
                self.update_autostart_desc_label()
            if hasattr(self, "start_tray_var"):
                self.start_tray_var.set(tray_min)
            if hasattr(self, "desk_sc_var"):
                self.desk_sc_var.set(desk_lnk)
            if hasattr(self, "smenu_sc_var"):
                self.smenu_sc_var.set(smenu_lnk)
            if hasattr(self, "sleep_mode_var"):
                self.sleep_mode_var.set(sleep_m)
            if hasattr(self, "skip_prompt_var"):
                self.skip_prompt_var.set(skip_prompt)
            if hasattr(self, "wake_check_var"):
                self.wake_check_var.set(wake_chk)
            if hasattr(self, "show_log_var") and bool(self.show_log_var.get()) != show_log:
                self.show_log_var.set(show_log)
                try:
                    self.toggle_log_panel()
                except Exception:
                    pass
            try:
                self.update_sleep_prevention_state()
            except Exception:
                pass
            self.update_autostart_status_ui()

        self.run_in_main_thread(_sync_local_ui)
        self.log(f"[WINDOWS-SETTINGS] Applied remote startup configuration from Manager (mode={mode}, repair={force_repair}, desktop={desk_lnk}, start_menu={smenu_lnk}, tray={tray_min}, sleep={sleep_m}, wake_check={wake_chk}, no_close_prompt={skip_prompt}, log={show_log}).")

        allow_uac_remote = bool(force_repair or (mode != prev_mode))

        def _apply_os_bg():
            self.apply_autostart_mode_in_os(mode, log_change=True, allow_uac=allow_uac_remote, force_task=force_repair)
            _create_or_remove_windows_shortcuts(APP_ROOT_PATH, desk_lnk, smenu_lnk)
            self.ensure_smb_shares_connected()

        threading.Thread(target=_apply_os_bg, daemon=True).start()

    def _atomic_save_config(self):
        """Atomický a thread-safe zápis config.json (.tmp + fsync + os.replace)."""
        with self._config_file_lock:
            tmp_cfg = CONFIG_FILE + ".tmp"
            try:
                snap = dict(self.config)
                with open(tmp_cfg, "w", encoding="utf-8") as wf:
                    json.dump(snap, wf, indent=4)
                    wf.flush()
                    os.fsync(wf.fileno())
                os.replace(tmp_cfg, CONFIG_FILE)
            except Exception:
                try:
                    if os.path.isfile(tmp_cfg):
                        os.remove(tmp_cfg)
                except OSError:
                    pass

    def on_shortcuts_pref_changed(self):
        self.reset_options_autohide_timer()
        d_val = bool(self.desk_sc_var.get())
        s_val = bool(self.smenu_sc_var.get())
        self.config["desktop_shortcut"] = d_val
        self.config["start_menu_shortcut"] = s_val
        self._atomic_save_config()
        threading.Thread(target=_create_or_remove_windows_shortcuts, args=(APP_ROOT_PATH, d_val, s_val), daemon=True).start()

    def on_tray_checkbox_changed(self):
        """Voláno z hlavního Tk vlákna při přepnutí checkboxů tray/skip_prompt."""
        if hasattr(self, "start_tray_var"):
            self._start_tray_val = bool(self.start_tray_var.get())
        if hasattr(self, "skip_prompt_var"):
            self._skip_prompt_val = bool(self.skip_prompt_var.get())
        self.save_tray_prefs()

    def save_tray_prefs(self):
        # Thread-safe: čte pouze čistě Pythonové atributy, nikdy Tk proměnné (lze volat z libovolného vlákna i z __init__).
        self.reset_options_autohide_timer()
        st_val = bool(getattr(self, "_start_tray_val", self.config.get("start_in_tray", False)))
        sp_val = bool(getattr(self, "_skip_prompt_val", self.config.get("skip_tray_prompt", False)))
        am_val = str(getattr(self, "_autostart_mode_val", self.get_autostart_mode()) or "off")
        if am_val not in ("off", "after_login", "before_login"):
            am_val = "off"
        self.config["start_in_tray"] = st_val
        self.config["tray_minimize"] = st_val
        self.config["skip_tray_prompt"] = sp_val
        self.config["autostart_mode"] = am_val
        self.config["run_on_startup"] = (am_val == "after_login")
        self.config["run_before_login"] = (am_val == "before_login")
        self.config["show_log"] = bool(getattr(self, "log_visible", self.config.get("show_log", False)))
        self.config["show_options_bar"] = False
        self.config["ui_lang"] = wc.get_worker_lang()
        self.config["accept_jobs"] = bool(getattr(self, "accept_jobs", self.config.get("accept_jobs", True)))
        self.config.pop("nas_downloads", None)
        self.config.pop("nas_staging", None)
        self._atomic_save_config()

    def _on_root_state_event(self, _event=None):
        """Aktualizuje cache stavu okna výhradně z hlavního Tk vlákna."""
        try:
            st = str(self.root.state() or "").lower()
            if st:
                self._cached_win_state = st
        except Exception:
            pass

    def hide_to_tray(self):
        self.is_in_tray = True
        self._cached_win_state = "withdrawn"
        self.root.withdraw()

    def restore_from_tray(self):
        self.is_in_tray = False
        self._cached_win_state = "normal"
        self.root.deiconify()
        try:
            self.root.state("normal")
        except Exception:
            pass
        self.root.lift()
        self.root.focus_force()
        apply_windows_dark_titlebar(self.root)

    def get_current_window_state_for_ota(self) -> str:
        # Thread-safe: nevolá self.root.state() z worker_loop vlákna.
        st = str(getattr(self, "_cached_win_state", "normal") or "").lower()
        if getattr(self, "is_in_tray", False) or st == "withdrawn":
            return "tray"
        if st == "iconic":
            return "minimized"
        return "normal"

    def _remove_own_ipc_state_file(self):
        """Odstraní .worker_instance.json jen pokud patří tomuto procesu (ochrana před smazáním souboru nové instance)."""
        try:
            if not os.path.isfile(IPC_STATE_FILE):
                return
            with open(IPC_STATE_FILE, "r", encoding="utf-8") as rf:
                st = json.load(rf) or {}
            if int(st.get("pid") or 0) == os.getpid():
                os.remove(IPC_STATE_FILE)
        except Exception:
            pass

    def write_ipc_instance_state(self):
        try:
            payload = {
                "pid": os.getpid(),
                "session_id": 0 if self.is_bg_boot else get_current_windows_session_id(),
                "bg_boot": bool(self.is_bg_boot),
                "ts": time.time(),
            }
            tmp_ipc = IPC_STATE_FILE + ".tmp"
            with open(tmp_ipc, "w", encoding="utf-8") as wf:
                json.dump(payload, wf)
            os.replace(tmp_ipc, IPC_STATE_FILE)
        except Exception:
            pass

    def ipc_control_loop(self):
        while self.is_running:
            time.sleep(0.5)
            try:
                if os.path.isfile(IPC_CMD_FILE):
                    cmd_data = {}
                    try:
                        with open(IPC_CMD_FILE, "r", encoding="utf-8") as rf:
                            cmd_data = json.load(rf) or {}
                    except Exception:
                        cmd_data = {}
                    cmd_type = str(cmd_data.get("cmd") or "").upper()
                    cmd_age = time.time() - float(cmd_data.get("ts") or 0.0)
                    # Příkaz konzumuje jen ta instance, pro kterou je určený (DESKTOP_TAKEOVER jen Session 0, SHOW_WINDOW jen GUI).
                    handled = False
                    if cmd_type == "SHOW_WINDOW" and not self.is_bg_boot:
                        handled = True
                        self.run_in_main_thread(self.restore_from_tray)
                    elif cmd_type == "DESKTOP_TAKEOVER" and self.is_bg_boot:
                        handled = True
                        if not self.handover_requested:
                            self.handover_requested = True
                            self.accept_jobs = False
                            self.log("[AUTOSTART] User logged into desktop — handing over background Session 0 worker to System Tray GUI...")
                    elif cmd_age > 60.0 or not cmd_type:
                        handled = True
                    if handled:
                        try:
                            os.remove(IPC_CMD_FILE)
                        except Exception:
                            pass
                if self.is_bg_boot and self.handover_requested:
                    with self.jobs_lock:
                        act_cnt = len(self.active_jobs)
                    with self.held_lock:
                        up_id = self.uploading_held_job_id
                    if act_cnt == 0 and up_id is None:
                        self._remove_own_ipc_state_file()
                        if hasattr(self, "tray_icon"):
                            self.tray_icon.remove()
                        self.is_running = False
                        os._exit(0)
            except Exception:
                pass

    def on_close_button_clicked(self):
        if self.skip_prompt_var.get():
            self.hide_to_tray()
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(wc.T('w_minimize_system_tray'))
        dlg.configure(bg="#0f172a")
        dlg.geometry("460x185")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.after(50, lambda: apply_windows_dark_titlebar(dlg))

        tk.Label(
            dlg,
            text=wc.T('w_hb_cluster_worker_will'),
            bg="#0f172a", fg="#f1f5f9", font=("Segoe UI", 9, "bold"), justify=tk.LEFT
        ).pack(padx=16, pady=(16, 10), anchor=tk.W)

        dont_ask_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dlg,
            text=wc.T('w_do_not_show_again'),
            variable=dont_ask_var
        ).pack(padx=16, pady=4, anchor=tk.W)

        btn_row = ttk.Frame(dlg, style="Card.TFrame")
        btn_row.pack(fill=tk.X, padx=16, pady=(14, 10))

        def _confirm_hide():
            if dont_ask_var.get():
                self.skip_prompt_var.set(True)
                self._skip_prompt_val = True
                self.save_tray_prefs()
            dlg.destroy()
            self.hide_to_tray()

        def _quit_now():
            dlg.destroy()
            self.request_stop()

        ttk.Button(btn_row, text=wc.T('w_hide_system_tray'), style="Primary.TButton", command=_confirm_hide).pack(side=tk.LEFT)
        ttk.Button(btn_row, text=wc.T('w_quit_worker'), image=self.icons["stop"], compound=tk.LEFT, style="Danger.TButton", command=_quit_now).pack(side=tk.RIGHT)

    def get_inputs_dir(self) -> str:
        return (self.config.get("inputs_dir") or "").rstrip("\\/")

    def get_outputs_dir(self) -> str:
        return (self.config.get("outputs_dir") or "").rstrip("\\/")

    def _probe_manager_reachable(self, url_str: str, timeout: float = 1.5) -> bool:
        u = wc.sanitize_manager_url(url_str)
        if not u:
            return False
        try:
            r = api_request("GET", f"{u}/api/update/version", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def auto_sync_zero_config_from_manager(self):
        srv = wc.sanitize_manager_url(self.config.get("server_url") or self.config.get("manager_url") or "")
        is_local_default = (not srv or "localhost" in srv.lower() or "127.0.0.1" in srv.lower())
        if is_local_default:
            if srv and self._probe_manager_reachable(srv, timeout=1.2):
                self.needs_manager_url_setup = False
            else:
                disc = _bootstrap_udp_discover(timeout=3.0)
                if disc:
                    srv = disc.rstrip("/")
                    self.config["server_url"] = srv
                    self.needs_manager_url_setup = False
                else:
                    srv = ""
                    self.config["server_url"] = ""
                    self.needs_manager_url_setup = True
        else:
            if self._probe_manager_reachable(srv, timeout=2.0):
                self.needs_manager_url_setup = False
            else:
                disc_url, disc_resp = wc.discover_manager_udp(port=UDP_DISCOVERY_PORT, timeout=3.0, prefer_bound=True)
                bound_id = wc.bound_manager_id()
                resp_mid = str((disc_resp or {}).get("manager_id") or "").strip()
                can_adopt = bool(disc_url) and (not bound_id or (resp_mid == bound_id) or (not resp_mid and self._probe_manager_reachable(disc_url, timeout=2.0)))
                if can_adopt:
                    srv = disc_url.rstrip("/")
                    self.config["server_url"] = srv
                    self.needs_manager_url_setup = False
                else:
                    self.needs_manager_url_setup = not bool(bound_id)
        if not srv:
            return
        if not self.config.get("inputs_dir") or not self.config.get("outputs_dir") or not self.config.get("smb_username"):
            try:
                wn = self.config.get("worker_name") or socket.gethostname()
                r = api_request("GET", f"{srv}/api/workers/{wn}/config", timeout=5)
                if r.status_code == 200:
                    cd = r.json() or {}
                    self.apply_telemetry_prefs_from_server(cd)
                    changed = False
                    if not self.config.get("inputs_dir") and cd.get("win_default_inputs_dir"):
                        self.config["inputs_dir"] = wc.sanitize_path(cd["win_default_inputs_dir"])
                        changed = True
                    if not self.config.get("outputs_dir") and cd.get("win_default_outputs_dir"):
                        self.config["outputs_dir"] = wc.sanitize_path(cd["win_default_outputs_dir"])
                        changed = True
                    if cd.get("smb_username") is not None and not (self.config.get("smb_user") or self.config.get("smb_username")):
                        su = str(cd.get("smb_username") or "").strip()
                        self.config["smb_username"] = su
                        self.config["smb_user"] = su
                        changed = True
                    if cd.get("smb_password") is not None and not (self.config.get("smb_pass") or self.config.get("smb_password")):
                        spw = str(cd.get("smb_password") or "")
                        self.config["smb_password"] = spw
                        self.config["smb_pass"] = spw
                        changed = True
                    if cd.get("ui_lang") in ("en", "cs"):
                        self.config["ui_lang"] = cd["ui_lang"]
                        changed = True
                    if changed:
                        self._atomic_save_config()
            except Exception:
                pass
        self.ensure_smb_shares_connected(log_events=False)

    def ensure_smb_shares_connected(self, log_events: bool = True):
        smb_u = str(self.config.get("smb_user") or self.config.get("smb_username") or "").strip()
        smb_p = str(self.config.get("smb_pass") or self.config.get("smb_password") or "")
        if not smb_u or not smb_p:
            return
        roots = set()
        for raw_p in (self.get_inputs_dir(), self.get_outputs_dir()):
            p = str(raw_p or "").strip().replace("/", "\\")
            if p.startswith("\\\\"):
                pts = [x for x in p.lstrip("\\").split("\\") if x]
                if len(pts) >= 2:
                    roots.add(f"\\\\{pts[0]}\\{pts[1]}")
        if not roots:
            return
        with self._smb_connect_lock:
            for share_root in sorted(roots):
                if os.path.isdir(share_root):
                    self._smb_last_err.pop(share_root, None)
                    continue
                rc = _connect_unc_share_win32(share_root, smb_u, smb_p)
                if rc in (0, 85) or os.path.isdir(share_root):
                    prev_err = self._smb_last_err.pop(share_root, None)
                    if log_events and (prev_err is not None or self.is_bg_boot):
                        try:
                            self.log(wc.T("w_smb_connected", share_root))
                        except Exception:
                            pass
                else:
                    if log_events and self._smb_last_err.get(share_root) != rc:
                        self._smb_last_err[share_root] = rc
                        try:
                            self.log(wc.T("w_smb_connect_failed", share_root, rc))
                        except Exception:
                            pass

    def is_http_relay_active(self) -> bool:
        cfg = getattr(self, "config", {}) or {}
        return bool(cfg.get("http_relay_enabled") or cfg.get("use_http_relay") or cfg.get("relay_mode"))

    def is_storage_ok(self) -> bool:
        hs = getattr(self, "handshake_state", {}) or {}
        if not hs.get("verified"):
            return False
        return bool(hs.get("inputs_ok") and hs.get("outputs_ok"))

    def perform_storage_handshake(self, force_log=False):
        if not self.handshake_lock.acquire(blocking=False):
            return
        try:
            self.ensure_smb_shares_connected()
            in_dir = self.get_inputs_dir()
            out_dir = self.get_outputs_dir()
            srv = self.config.get("server_url", "").rstrip("/")
            w_name = self.config.get("worker_name", "")
            if not srv or not w_name:
                return

            st_check = wc.check_worker_storage_accessible(in_dir, out_dir)
            local_in_ok = bool(st_check.get("inputs_ok"))
            local_out_ok = bool(st_check.get("outputs_ok"))

            if not local_in_ok or not local_out_ok:
                self.handshake_state = {
                    "verified": True,
                    "inputs_ok": local_in_ok,
                    "outputs_ok": local_out_ok,
                    "error": st_check.get("reason", "Storage path inaccessible")
                }
                self.set_connection_status(self.is_online_state)
                tup = (local_in_ok, local_out_ok)
                if force_log or tup != self.last_logged_handshake:
                    self.last_logged_handshake = tup
                    reason = st_check.get("reason") or "Inputs/Outputs path inaccessible"
                    self.log(f"{wc.T('w_manager_online_storage_unreachable')}: {reason}")
                return

            r_init = api_request("POST", f"{srv}/api/workers/handshake", json={"worker_name": w_name, "phase": "init"}, timeout=8)
            if r_init.status_code != 200:
                self.handshake_state = {"verified": True, "inputs_ok": False, "outputs_ok": False, "error": f"Manager handshake HTTP {r_init.status_code}"}
                self.set_connection_status(self.is_online_state)
                return
            idat = r_init.json()
            token = idat.get("token", "")
            m_out = idat.get("mgr_out_probe_filename", "")
            w_out = idat.get("wrk_out_probe_filename", "")

            saw_out = False
            if m_out and out_dir and os.path.isdir(out_dir):
                p_out = os.path.join(out_dir, m_out)
                for _ in range(3):
                    if os.path.isfile(p_out):
                        try:
                            with open(p_out, "r", encoding="utf-8") as f:
                                if f.read().strip() == token:
                                    saw_out = True
                                    break
                        except Exception:
                            pass
                    time.sleep(0.35)

            if w_out and out_dir and os.path.isdir(out_dir):
                try:
                    with open(os.path.join(out_dir, w_out), "w", encoding="utf-8") as f:
                        f.write(token)
                except Exception:
                    pass

            saw_in = wc.inputs_match_manager(in_dir, idat)    # jména + velikosti vzorku souborů (Manager do Inputs nic nepíše)

            r_ver = api_request(
                "POST", f"{srv}/api/workers/handshake",
                json={"worker_name": w_name, "phase": "verify", "token": token, "worker_saw_mgr_in": saw_in, "worker_saw_mgr_out": saw_out},
                timeout=8
            )
            if r_ver.status_code == 200:
                vd = r_ver.json()
                in_ok = bool(vd.get("inputs_ok"))
                out_ok = bool(vd.get("outputs_ok"))
                self.handshake_state = {"verified": True, "inputs_ok": in_ok, "outputs_ok": out_ok}
                self.set_connection_status(self.is_online_state)
                tup = (in_ok, out_ok)
                if force_log or tup != self.last_logged_handshake:
                    self.last_logged_handshake = tup
                    if in_ok and out_ok:
                        self.log(wc.T('w_storage_handshake_ok_inputs'))
                    else:
                        self.log(wc.T('w_storage_handshake_inputs_outputs', 'OK' if in_ok else 'HTTP RELAY', 'R/W OK' if out_ok else 'HTTP RELAY'))
            else:
                self.handshake_state = {"verified": True, "inputs_ok": False, "outputs_ok": False, "error": f"Verify HTTP {r_ver.status_code}"}
                self.set_connection_status(self.is_online_state)
        except Exception as e:
            self.handshake_state = {"verified": True, "inputs_ok": False, "outputs_ok": False, "error": str(e)}
            self.set_connection_status(self.is_online_state)
            if force_log:
                self.log(wc.T('w_storage_handshake_manager_temporar'))
        finally:
            self.handshake_lock.release()

    def resolve_worker_source_path(self, raw_path: str, verified_rel_source: str = "") -> str:
        in_r = self.get_inputs_dir()
        if verified_rel_source and in_r and os.path.isdir(in_r):
            cand = os.path.normpath(os.path.join(in_r, verified_rel_source.lstrip("\\/")))
            if os.path.exists(cand):
                return cand
        if raw_path and os.path.exists(raw_path):
            return os.path.normpath(raw_path)
        norm = str(raw_path or "").replace("\\", "/").strip("/")
        pts = [p for p in norm.split("/") if p and not p.endswith(":")]
        if in_r and os.path.isdir(in_r):
            for i in range(len(pts)):
                rc = "/".join(pts[i:])
                fc = os.path.normpath(os.path.join(in_r, rc))
                if os.path.exists(fc):
                    return fc
        fb_rel = verified_rel_source or ("/".join(pts[1:]) if len(pts) > 1 else (pts[0] if pts else ""))
        return os.path.normpath(os.path.join(in_r, fb_rel.lstrip("\\/")))

    def collect_diagnostics(self):
        hb_ver = "MISSING"
        x265_ver = "N/A"
        hb_bin = sync_and_resolve_binaries(self.config.get("server_url"), allow_download=False)
        hb_res = hb_bin if (hb_bin and os.path.isfile(hb_bin)) else shutil.which(hb_bin or "HandBrakeCLI.exe")
        if hb_res:
            hb_ver = probe_win_binary_version(hb_res, "--version", r"HandBrake\s+([\d\.]+)", "OK")
            x265_ver = wc.extract_x265_version(hb_res) or ("4.1" if hb_ver.startswith(("1.9", "1.10", "1.11", "1.12")) else ("4.0" if hb_ver.startswith("1.8") else "3.5"))

        ff_res = FFMPEG_CLI if (FFMPEG_CLI and os.path.isfile(FFMPEG_CLI)) else shutil.which(FFMPEG_CLI or "ffmpeg.exe")
        fp_res = FFPROBE_CLI if (FFPROBE_CLI and os.path.isfile(FFPROBE_CLI)) else shutil.which(FFPROBE_CLI or "ffprobe.exe")
        mp_res = MKVPROPEDIT_CLI if (MKVPROPEDIT_CLI and os.path.isfile(MKVPROPEDIT_CLI)) else shutil.which(MKVPROPEDIT_CLI or "mkvpropedit.exe")
        mm_res = MKVMERGE_CLI if (MKVMERGE_CLI and os.path.isfile(MKVMERGE_CLI)) else shutil.which(MKVMERGE_CLI or "mkvmerge.exe")
        sz_res = SEVENZIP_CLI if (SEVENZIP_CLI and os.path.isfile(SEVENZIP_CLI)) else shutil.which(SEVENZIP_CLI or "7z.exe")

        ff_ok = bool(ff_res)
        fp_ok = bool(fp_res)
        mp_ok = bool(mp_res)
        mm_ok = bool(mm_res)
        sz_ok = bool(sz_res)
        ff_ver = probe_win_binary_version(ff_res, "-version", r"ffmpeg version\s+([^\s]+)", "OK")[:14] if ff_ok else ""
        mkv_ver = probe_win_binary_version(mm_res, "--version", r"v(\d+\.\d+(?:\.\d+)?)", "OK") if mm_ok else ""
        sz_ver = probe_win_binary_version(sz_res, "i", r"7-Zip\s+(?:[\(\[][^\)\]]+[\)\]]\s+)?(\d+\.\d+)", "OK") if sz_ok else ""

        in_dir = self.get_inputs_dir()
        out_dir = self.get_outputs_dir()
        in_ok = bool(in_dir and os.path.exists(in_dir))
        out_ok = bool(out_dir and os.path.exists(out_dir))
        t_free = 0.0
        try:
            os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
            _, _, free = shutil.disk_usage(LOCAL_TEMP_DIR)
            t_free = round(free / (1024**3), 1)
        except Exception:
            pass

        self.cached_diag = {
            "ota": wc.update_retry_view(self.update_retry_state),   # O4: proč a do kdy čeká aktualizace
            "os": WINDOWS_VERSION_LABEL,
            "cpu_model": WINDOWS_CPU_NAME,
            "net": wc.get_worker_network_info(self.config.get('server_url', '')),
            "is_hybrid_cpu": bool((self.hybrid_cpu or {}).get("is_hybrid")),
            "e_core_count": int((self.hybrid_cpu or {}).get("e_core_count") or 0),
            "p_core_count": int((self.hybrid_cpu or {}).get("p_core_count") or 0),
            "physical_cores": int((self.hybrid_cpu or {}).get("physical_cores") or 0),
            "has_smt": bool((self.hybrid_cpu or {}).get("has_smt")),
            "cpu_topology": self.hybrid_cpu or {},
            "custom_cores": list(self.custom_cores or []),
            "hb_version": hb_ver,
            "hb_source": HB_BINARY_SOURCE,
            "hb_path": hb_res or "",
            "hb_md5": wc.compute_file_md5(LOCAL_HB_EXE) or wc.compute_file_md5(hb_res),
            "x265_version": x265_ver,
            "ffmpeg_ok": ff_ok,
            "ffmpeg_version": ff_ver,
            "ffmpeg_source": FFMPEG_BINARY_SOURCE,
            "ffmpeg_md5": wc.compute_file_md5(LOCAL_FFMPEG_EXE) or wc.compute_file_md5(ff_res),
            "ffprobe_ok": fp_ok,
            "ffprobe_md5": wc.compute_file_md5(LOCAL_FFPROBE_EXE) or wc.compute_file_md5(fp_res),
            "mkvpropedit_ok": mp_ok,
            "mkvpropedit_md5": wc.compute_file_md5(LOCAL_MKVPROPEDIT_EXE) or wc.compute_file_md5(mp_res),
            "mkvmerge_ok": mm_ok,
            "mkvmerge_md5": wc.compute_file_md5(LOCAL_MKVMERGE_EXE) or wc.compute_file_md5(mm_res),
            "mkv_version": mkv_ver,
            "sevenzip_ok": sz_ok,
            "sevenzip_version": sz_ver,
            "sevenzip_md5": wc.compute_file_md5(LOCAL_7Z_EXE) or wc.compute_file_md5(sz_res),
            "inputs_ok": in_ok,
            "outputs_ok": out_ok,
            "nas_dl_ok": in_ok,
            "nas_out_ok": out_ok,
            "temp_free_gb": t_free
        }
        return self.cached_diag

    def sample_telemetry(self):
        if not hasattr(self, "_telem_lock"):
            self._telem_lock = threading.Lock()
        with self._telem_lock:
            now_t = time.time()
            last_ts = float(self.telem_state.get("last_sample_ts") or 0.0)
            if (now_t - last_ts) < 1.5 and self.telem_state.get("last_sample"):
                return self.telem_state["last_sample"]

            cores = os.cpu_count() or 1
            idle_ft, kernel_ft, user_ft = FILETIME(), FILETIME(), FILETIME()
            cpu_tot_pct = float(self.telem_state.get("last_cpu_tot") or 0.0)
            cpu_hb_pct = float(self.telem_state.get("last_cpu_hb") or 0.0)

            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft)):
                idl = ft_to_int(idle_ft)
                krn = ft_to_int(kernel_ft)
                usr = ft_to_int(user_ft)
                d_idl = idl - self.telem_state["sys_idle"]
                d_sys = (krn - self.telem_state["sys_kernel"]) + (usr - self.telem_state["sys_user"])

                with self.jobs_lock:
                    root_pids = [d["process"].pid for d in self.active_jobs.values() if d.get("process") and d["process"].poll() is None]

                all_pids = set()
                for rpid in root_pids:
                    for cpid in wc.get_process_tree_pids(rpid):
                        all_pids.add(cpid)

                c_times = {}
                hb_ram = 0
                k32 = ctypes.windll.kernel32
                k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                k32.OpenProcess.restype = wintypes.HANDLE
                k32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
                k32.GetProcessTimes.restype = wintypes.BOOL
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                psapi = ctypes.windll.psapi
                psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
                psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
                for pid in all_pids:
                    hp = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid))
                    if hp:
                        c_ft, e_ft, k_ft, u_ft = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                        if k32.GetProcessTimes(hp, ctypes.byref(c_ft), ctypes.byref(e_ft), ctypes.byref(k_ft), ctypes.byref(u_ft)):
                            c_times[pid] = ft_to_int(k_ft) + ft_to_int(u_ft)
                        pmc = PROCESS_MEMORY_COUNTERS()
                        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                        if psapi.GetProcessMemoryInfo(hp, ctypes.byref(pmc), pmc.cb):
                            hb_ram += pmc.WorkingSetSize
                        k32.CloseHandle(hp)

                if self.telem_state["sys_kernel"] == 0:
                    cpu_tot_pct, cpu_hb_pct = 0.0, 0.0
                    self.telem_state["sys_idle"] = idl
                    self.telem_state["sys_kernel"] = krn
                    self.telem_state["sys_user"] = usr
                    self.telem_state["proc_times"] = c_times
                elif d_sys > 0:
                    cpu_tot_pct = max(0.0, min(100.0, ((d_sys - d_idl) / float(d_sys)) * 100.0))
                    d_proc = sum((pt - self.telem_state["proc_times"].get(pid, 0)) for pid, pt in c_times.items() if self.telem_state["proc_times"].get(pid, 0) > 0 and pt >= self.telem_state["proc_times"].get(pid, 0))
                    cpu_hb_pct = max(0.0, min(100.0, (d_proc / float(d_sys)) * 100.0))
                    self.telem_state["sys_idle"] = idl
                    self.telem_state["sys_kernel"] = krn
                    self.telem_state["sys_user"] = usr
                    self.telem_state["proc_times"] = c_times
            else:
                hb_ram = 0

            self.telem_state["last_cpu_tot"] = cpu_tot_pct
            self.telem_state["last_cpu_hb"] = cpu_hb_pct

            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            ram_tot_gb = round(mem.ullTotalPhys / (1024**3), 1)
            ram_usd_gb = round((mem.ullTotalPhys - mem.ullAvailPhys) / (1024**3), 1)
            ram_hb_gb = round(hb_ram / (1024**3), 2)

            if not self.cached_diag:
                self.collect_diagnostics()
            else:
                try:
                    _, _, free = shutil.disk_usage(LOCAL_TEMP_DIR)
                    self.cached_diag["temp_free_gb"] = round(free / (1024**3), 1)
                except Exception:
                    pass

            eco_t = max(1, cores - 2) if cores >= 4 else cores
            hyb = self.hybrid_cpu or {}
            cust_cnt = len(self.custom_cores) if self.custom_cores else cores
            eff_mode = self.resolve_windows_power_mode()
            if eff_mode == "CUSTOM":
                inner_lbl = f"CUSTOM ({cust_cnt}/{cores}t)"
            elif eff_mode == "HYBRID_ECO" and hyb.get("is_hybrid"):
                inner_lbl = f"HYBRID ECO [{int(hyb.get('e_core_count') or eco_t)}/{cores}t E-cores]"
            elif eff_mode in ("ECO", "HYBRID_ECO"):
                inner_lbl = f"ECO ({eco_t}/{cores}t)"
            else:
                inner_lbl = f"FULL ({cores}/{cores}t)"
            if self.power_mode in ("AUTO", "NIGHT"):
                eff_m = f"{self.power_mode} ({inner_lbl})"
            else:
                eff_m = inner_lbl

            st_out = {
                "cpu_total": round(cpu_tot_pct, 1),
                "cpu_hb": round(cpu_hb_pct, 1),
                "cores": cores,
                "cores_total": cores,
                "ram_used_gb": ram_usd_gb,
                "ram_total_gb": ram_tot_gb,
                "ram_hb_gb": ram_hb_gb,
                "power_mode_active": eff_m,
                "custom_cores": list(self.custom_cores or []),
                "diag": self.cached_diag
            }
            self.telem_state["last_sample_ts"] = now_t
            self.telem_state["last_sample"] = st_out
            self.update_telemetry_canvas_ui(cpu_tot_pct, cpu_hb_pct, ram_usd_gb, ram_tot_gb, ram_hb_gb)
            self.refresh_tray_and_window_icon()
            return st_out

    def apply_telemetry_prefs_from_server(self, payload: dict):
        if not isinstance(payload, dict):
            return
        t_src = payload.get("telemetry_source")
        r_fmt = payload.get("ram_format")
        changed = False
        if t_src in ("system", "hb", "both") and t_src != getattr(self, "telemetry_source", "both"):
            self.telemetry_source = t_src
            self.config["telemetry_source"] = t_src
            changed = True
        if r_fmt in ("gb_ratio", "gb_pct", "pct_gb", "pct_only") and r_fmt != getattr(self, "ram_format", "gb_ratio"):
            self.ram_format = r_fmt
            self.config["ram_format"] = r_fmt
            changed = True
        if changed:
            self.save_tray_prefs()

    def update_telemetry_canvas_ui(self, cpu_tot, cpu_hb, ram_usd, ram_tot, ram_hb):
        def _draw():
            if not self.root.winfo_exists():
                return
            t_src = getattr(self, "telemetry_source", "both")
            r_fmt = getattr(self, "ram_format", "gb_ratio")
            cpu_txt, ram_txt = wc.format_telemetry_labels(cpu_tot, cpu_hb, ram_usd, ram_tot, ram_hb, t_src, r_fmt)

            w_cpu = max(160, self.cpu_bar_canvas.winfo_width())
            hb_w_cpu = 0 if t_src == "system" else int((min(100.0, cpu_hb) / 100.0) * w_cpu)
            sys_w_cpu = 0 if t_src == "hb" else int((min(100.0, cpu_tot) / 100.0) * w_cpu)
            self.cpu_bar_canvas.coords(self.cpu_hb_rect, 0, 0, hb_w_cpu, 8)
            self.cpu_bar_canvas.coords(self.cpu_sys_rect, 0, 10, sys_w_cpu, 16)
            col_sys_cpu = "#cbd5e1"  # plné CPU při převodu je normální stav -> vždy neutrální barva
            self.cpu_bar_canvas.itemconfig(self.cpu_sys_rect, fill=col_sys_cpu)
            self.cpu_txt_lbl.config(text=cpu_txt)

            w_ram = max(160, self.ram_bar_canvas.winfo_width())
            hb_ram_pct = (ram_hb / ram_tot) if ram_tot > 0 else 0.0
            sys_ram_pct = (ram_usd / ram_tot) if ram_tot > 0 else 0.0
            hb_w_ram = 0 if t_src == "system" else int(min(1.0, hb_ram_pct) * w_ram)
            sys_w_ram = 0 if t_src == "hb" else int(min(1.0, sys_ram_pct) * w_ram)
            self.ram_bar_canvas.coords(self.ram_hb_rect, 0, 0, hb_w_ram, 8)
            self.ram_bar_canvas.coords(self.ram_sys_rect, 0, 10, sys_w_ram, 16)
            col_sys_ram = "#ef4444" if sys_ram_pct > 0.97 else ("#f59e0b" if sys_ram_pct > 0.90 else "#cbd5e1")
            self.ram_bar_canvas.itemconfig(self.ram_sys_rect, fill=col_sys_ram)
            self.ram_txt_lbl.config(text=ram_txt)

        self.run_in_main_thread(_draw)

    def resolve_windows_power_mode(self, idle_s=None) -> str:
        """Jednotný výpočet aktivního režimu výkonu (FULL / ECO / HYBRID_ECO / CUSTOM) pro telemetrii, afinitu i HandBrake pools."""
        hyb = self.hybrid_cpu or {}
        if self.power_mode == "AUTO":
            if idle_s is None:
                idle_s = get_user_idle_seconds()
            return "ECO" if (idle_s < 180.0) else "FULL"
        if self.power_mode == "CUSTOM":
            return "CUSTOM"
        if self.power_mode == "NIGHT":
            hr_now = datetime.datetime.now().hour
            if hr_now >= 22 or hr_now < 6:
                return "FULL"
            return "HYBRID_ECO" if hyb.get("is_hybrid") else "ECO"
        if self.power_mode == "HYBRID_ECO":
            return "HYBRID_ECO" if hyb.get("is_hybrid") else "ECO"
        if self.power_mode == "ECO":
            return "ECO"
        return "FULL"

    def apply_current_power_state_now(self):
        hyb = self.hybrid_cpu or {}
        eff_mode = self.resolve_windows_power_mode()
        self.active_eco_state = (eff_mode in ("ECO", "HYBRID_ECO", "CUSTOM"))

        with self.jobs_lock:
            pids = [d["process"].pid for d in self.active_jobs.values() if d.get("process") and d["process"].poll() is None]
        for pid in pids:
            apply_process_power_and_affinity(pid, eff_mode, hybrid_info=hyb, custom_cores=self.custom_cores)

        cores = os.cpu_count() or 1
        eco_t = max(1, cores - 2) if cores >= 4 else cores
        e_cnt = int(hyb.get("e_core_count") or eco_t)
        cust_cnt = len(self.custom_cores) if self.custom_cores else cores
        if self.power_mode == "AUTO":
            if self.active_eco_state:
                mode_txt = wc.T('w_mode_auto_eco_t', eco_t, cores)
            else:
                mode_txt = wc.T('w_mode_auto_full_t', cores, cores)
        elif self.power_mode == "CUSTOM":
            mode_txt = wc.T('w_mode_custom_t', cust_cnt, cores)
        elif self.power_mode == "NIGHT":
            night_t = e_cnt if eff_mode == "HYBRID_ECO" else (eco_t if self.active_eco_state else cores)
            mode_txt = wc.T('w_mode_night_t', night_t, cores)
        elif self.power_mode == "HYBRID_ECO" and hyb.get("is_hybrid"):
            mode_txt = wc.T('w_mode_hybrid_eco_t', e_cnt, cores)
        elif self.power_mode in ("ECO", "HYBRID_ECO"):
            mode_txt = wc.T('w_mode_eco_forced_t', eco_t, cores)
        else:
            mode_txt = wc.T('w_mode_full_100_t', cores, cores)
        self.run_in_main_thread(lambda: self.power_label.config(text=mode_txt))

    def power_manager_loop(self):
        while self.is_running:
            time.sleep(3)
            try:
                self.run_in_main_thread(self.update_sleep_prevention_state)
                self.apply_current_power_state_now()
            except Exception:
                pass

    def cleanup_temp_dir(self, on_startup: bool = False):
        with self.jobs_lock:
            act_ids = set(self.active_jobs.keys())
        with self.held_lock:
            h_ids = set(self.held_jobs.keys())
            up_id = self.uploading_held_job_id

        def _is_still_live(jid, is_sb):
            with self.jobs_lock:
                if jid in self.active_jobs:
                    return True
            if not is_sb:
                with self.held_lock:
                    if jid in self.held_jobs or jid == self.uploading_held_job_id:
                        return True
            return False

        wc.cleanup_orphaned_job_temp_dirs(
            LOCAL_TEMP_DIR, act_ids, h_ids, up_id,
            live_check_fn=_is_still_live,
            min_age_sec=0.0 if on_startup else 30.0
        )

    def get_held_job_ids(self) -> list:
        with self.held_lock:
            ids = set(self.held_jobs.keys())
            if self.uploading_held_job_id is not None:
                ids.add(self.uploading_held_job_id)
        return sorted(ids)

    def discard_held_jobs_by_ids(self, cleanup_ids):
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
        with self.held_lock:
            for jid in list(cset):
                if jid == self.uploading_held_job_id:
                    continue
                meta = self.held_jobs.pop(jid, None)
                if meta is not None:
                    removed.append((jid, meta.get("job_temp_dir") or os.path.join(LOCAL_TEMP_DIR, f"job_{jid}")))
                else:
                    j_tmp = os.path.join(LOCAL_TEMP_DIR, f"job_{jid}")
                    if os.path.isdir(j_tmp):
                        removed.append((jid, j_tmp))
        for jid, j_tmp in removed:
            with self.jobs_lock:
                if jid in self.active_jobs:
                    continue
            self.remove_job_ui(jid)
            shutil.rmtree(j_tmp, ignore_errors=True)

    def restore_held_jobs_on_startup(self):
        loaded = wc.load_persisted_held_jobs(LOCAL_TEMP_DIR)
        if loaded:
            with self.held_lock:
                self.held_jobs.update(loaded)
            for jid, meta in loaded.items():
                cp = meta.get("complete_payload") or {}
                orig_d = meta.get("orig_d") or ""
                out_sz = int(meta.get("out_size_bytes") or 0)
                self.log(wc.T('w_restoring_held_completed_job', jid, os.path.basename(orig_d)))
                self.update_job_ui(jid, f"[WAITING DISK] {os.path.basename(orig_d)}", 100.0, f"{out_sz/(1024**3):.1f} GB", wc.T('w_waiting_space_outputs'))
                try:
                    rw = api_request(
                        "POST", f"{self.config['server_url']}/api/jobs/{jid}/waiting_disk",
                        json={
                            "worker_name": self.config["worker_name"],
                            "out_size_bytes": out_sz,
                            "src_size_bytes": int(cp.get("src_size_bytes") or 0),
                            "v_bitrate_in_kbps": int(cp.get("v_bitrate_in_kbps") or 0),
                            "a_bitrate_in_kbps": int(cp.get("a_bitrate_in_kbps") or 0),
                            "v_bitrate_out_kbps": int(cp.get("v_bitrate_out_kbps") or 0),
                            "a_bitrate_out_kbps": int(cp.get("a_bitrate_out_kbps") or 0),
                            "reason": wc.T('w_restored_worker_local_hold')
                        }, timeout=5
                    )
                    if rw.status_code == 200 and rw.json().get("obsolete"):
                        self.discard_held_jobs_by_ids([jid])
                except Exception:
                    pass
        self.cleanup_temp_dir(on_startup=True)

    def held_jobs_upload_loop(self):
        last_slot_try = {}
        while self.is_running:
            time.sleep(5)
            if not self.is_running or self.update_in_progress:
                continue
            with self.held_lock:
                h_items = list(self.held_jobs.items())
            if not h_items:
                continue

            for jid, meta in h_items:
                if not self.is_running or self.update_in_progress:
                    break
                j_tmp = meta.get("job_temp_dir") or os.path.join(LOCAL_TEMP_DIR, f"job_{jid}")
                w_dest = meta.get("w_dest") or ""
                orig_d = meta.get("orig_d") or ""
                tmp_orig_d = orig_d + ".tmp"
                o_sz = int(meta.get("out_size_bytes") or wc.get_file_size(w_dest))
                disp_fn = os.path.basename(orig_d) or f"Job #{jid}"
                use_relay_out = bool(meta.get("use_relay_output", False))
                relay_sub = str(meta.get("relay_subfolder") or "")

                if not w_dest or not os.path.isfile(w_dest):
                    self.log(wc.T('w_held_file_missing_error', jid, w_dest or j_tmp))
                    try:
                        api_request(
                            "POST", f"{self.config['server_url']}/api/jobs/{jid}/error",
                            json={"error_type": "GENERIC", "error_code": "ERR_HELD_OUTPUT_LOST", "error_detail": str(w_dest or j_tmp)[-200:], "version": CURRENT_VERSION},
                            timeout=5
                        )
                    except Exception:
                        pass
                    with self.held_lock:
                        self.held_jobs.pop(jid, None)
                    self.remove_job_ui(jid)
                    shutil.rmtree(j_tmp, ignore_errors=True)
                    continue

                force_up = False
                try:
                    rs = api_request("GET", f"{self.config['server_url']}/api/jobs/{jid}/status", params={"worker_name": self.config["worker_name"], "held_check": "true"}, timeout=5)
                    if rs.status_code == 200:
                        sd = rs.json()
                        if sd.get("ui_lang"):
                            self.sync_worker_lang(sd.get("ui_lang"))
                        st = sd.get("status")
                        w_own = sd.get("worker_name")
                        force_up = bool(sd.get("force_upload"))
                        if st in ("WAITING", "ERROR", "IN_PROGRESS", "STAGING", "PREMUX") and (not w_own or w_own == self.config["worker_name"]):
                            # WAITING/ERROR: úloha spadla zpět do fronty po restartu Manageru; IN_PROGRESS/STAGING/PREMUX: hlášení
                            # „hotovo, čeká na místo“ se k Manageru nedostalo (výpadek sítě / restart) -> obnovit WAITING_DISK,
                            # hotové video nikdy nemazat. Pokud worker úlohu právě znovu převádí, nedělat nic.
                            with self.jobs_lock:
                                busy_here = jid in self.active_jobs
                            if busy_here:
                                continue
                            try:
                                api_request(
                                    "POST", f"{self.config['server_url']}/api/jobs/{jid}/waiting_disk",
                                    json={"worker_name": self.config["worker_name"], "out_size_bytes": o_sz, "reason": "Re-registered held finished video after Manager reconnect"},
                                    timeout=5
                                )
                            except Exception:
                                pass
                            continue
                        if st not in ("WAITING_DISK", "COPYING") or (w_own and w_own != self.config["worker_name"]):
                            self.log(wc.T('w_id_waiting_disk_job', jid, st))
                            with self.held_lock:
                                self.held_jobs.pop(jid, None)
                            self.remove_job_ui(jid)
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
                        "POST", f"{self.config['server_url']}/api/jobs/{jid}/request_upload",
                        json={"worker_name": self.config["worker_name"], "out_size_bytes": o_sz, "force": force_up},
                        timeout=8
                    )
                    if ru.status_code != 200 or not ru.json().get("allowed"):
                        continue
                except Exception:
                    continue

                with self.held_lock:
                    if self.update_in_progress or not self.is_running:
                        break
                    self.uploading_held_job_id = jid
                self.log(wc.T('w_id_storage_guard_target', jid, disp_fn, f'{o_sz / 1024 ** 3:.2f}'))
                remote_cancelled = [False]
                try:
                    last_p_ui = [0.0, 0.0]

                    def _chk_c(rem_chk):
                        if not self.is_running:
                            return True
                        if rem_chk:
                            try:
                                rc = api_request("GET", f"{self.config['server_url']}/api/jobs/{jid}/status", params={"worker_name": self.config["worker_name"], "held_check": "true"}, timeout=5)
                                if rc.status_code == 200:
                                    rj = rc.json()
                                    r_own = rj.get("worker_name")
                                    if rj.get("status") not in ("WAITING_DISK", "COPYING") or (r_own and r_own != self.config["worker_name"]):
                                        remote_cancelled[0] = True
                                        return True
                            except Exception:
                                pass
                        return False

                    def _on_p(prog, spd, copied_b, elap_s, nt):
                        if nt - last_p_ui[0] > 0.6:
                            self.update_job_ui(jid, f"[{wc.T('w_saving')}] {disp_fn}", prog, f"{spd:.1f} MB/s", f"{copied_b/(1024**3):.1f} / {o_sz/(1024**3):.1f} GB")
                            last_p_ui[0] = nt
                        if nt - last_p_ui[1] > 4.0:
                            try:
                                api_request(
                                    "POST", f"{self.config['server_url']}/api/jobs/{jid}/progress",
                                    json={
                                        "progress": prog, "fps_current": 0.0, "fps_avg": 0.0,
                                        "speed_mbps": round(spd, 2), "transferred_bytes": copied_b,
                                        "src_size_bytes": o_sz, "elapsed_sec": elap_s,
                                        "worker_name": self.config["worker_name"], "version": CURRENT_VERSION
                                    }, timeout=5
                                )
                            except Exception:
                                pass
                            last_p_ui[1] = nt

                    st_dir = self.get_outputs_dir()
                    if not use_relay_out and st_dir and st_dir.startswith("\\\\") and not os.path.isdir(st_dir):
                        self.ensure_smb_shares_connected()
                    if use_relay_out or not (st_dir and os.path.isdir(st_dir)):
                        if not meta.get("relay_uploaded"):
                            rel_res = wc.relay_upload_output_to_manager(
                                self.config["server_url"].rstrip("/"), jid, self.config["worker_name"],
                                w_dest, disp_fn, relay_sub, _chk_c, _on_p
                            )
                            if rel_res.get("final_dest_path"):
                                meta["complete_payload"]["final_dest_path"] = rel_res["final_dest_path"]
                            meta["relay_uploaded"] = True
                            wc.save_held_job_meta(j_tmp, meta)
                    else:
                        o_refuse = wc.output_target_refusal(orig_d, "", self.get_inputs_dir())
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
                            self.log(wc.T('w_out_saved_new_name', jid, os.path.basename(orig_d), os.path.basename(final_d)))
                            meta["complete_payload"]["final_dest_path"] = final_d
                        meta["final_d"] = final_d
                        wc.save_held_job_meta(j_tmp, meta)

                    rc = api_request("POST", f"{self.config['server_url']}/api/jobs/{jid}/complete", json=meta["complete_payload"], timeout=10)
                    if rc.status_code == 200:
                        self.log(wc.T('w_id_storage_guard_held', jid))
                        wc.remove_held_job_meta(j_tmp)
                        with self.held_lock:
                            self.held_jobs.pop(jid, None)
                        self.remove_job_ui(jid)
                        shutil.rmtree(j_tmp, ignore_errors=True)
                    elif wc.is_own_manager_refusal(rc):
                        self.log(f"ID {jid}: Manager rejected held /complete (HTTP {rc.status_code}) - dropping held job.")
                        wc.remove_held_job_meta(j_tmp)
                        with self.held_lock:
                            self.held_jobs.pop(jid, None)
                        self.remove_job_ui(jid)
                        shutil.rmtree(j_tmp, ignore_errors=True)
                    else:
                        self.log(f"ID {jid}: Held /complete returned HTTP {rc.status_code} - keeping held for retry.")
                except InterruptedError:
                    try:
                        if os.path.exists(tmp_orig_d):
                            os.remove(tmp_orig_d)
                    except Exception:
                        pass
                    # NIKDY nemazat hotové video v j_tmp při místním vypnutí aplikace – smazat JEN při zrušení Managerem!
                    if remote_cancelled[0]:
                        with self.held_lock:
                            self.held_jobs.pop(jid, None)
                        self.remove_job_ui(jid)
                        shutil.rmtree(j_tmp, ignore_errors=True)
                except Exception as e:
                    self.log(wc.T('w_id_storage_guard_upload', jid, e))
                    self.update_job_ui(jid, f"[WAITING DISK] {disp_fn}", 100.0, f"{o_sz/(1024**3):.1f} GB", wc.T('w_waiting_space_outputs'))
                    try:
                        if os.path.exists(tmp_orig_d):
                            os.remove(tmp_orig_d)
                    except Exception:
                        pass
                    try:
                        api_request(
                            "POST", f"{self.config['server_url']}/api/jobs/{jid}/waiting_disk",
                            json={"worker_name": self.config["worker_name"], "out_size_bytes": o_sz, "reason": f"Upload error: {e}"},
                            timeout=5
                        )
                    except Exception:
                        pass
                finally:
                    with self.held_lock:
                        self.uploading_held_job_id = None
                    with self.jobs_lock:
                        idle_now = (len(self.active_jobs) == 0)
                    if idle_now and self.is_running:
                        self.cleanup_temp_dir()

    def run_in_main_thread(self, func, *args, **kwargs):
        try:
            if self.root and self.root.winfo_exists():
                self.root.after(0, lambda: func(*args, **kwargs))
        except Exception:
            pass

    @staticmethod
    def format_elapsed_hms(sec_val) -> str:
        s = max(0, int(sec_val or 0))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}h{m:02d}m{sec:02d}s"

    def _redraw_job_table(self):
        if not hasattr(self, "job_table_canvas") or not self.job_table_canvas.winfo_exists():
            return
        c = self.job_table_canvas
        c.delete("all")
        w = max(800, int(c.winfo_width() or 1120))
        hdr_h = 26
        row_h = 28

        # Hlavička tabulky (ID, Soubor, Průběh, Rychlost / FPS, Uplynulo, Zbývá (ETA))
        w_id = 58
        w_prog = 96
        w_fps = 138
        w_elap = 112
        w_eta = 132
        w_file = max(240, w - (w_id + w_prog + w_fps + w_elap + w_eta))
        cols = [
            ("id", w_id, "center"),
            ("file", w_file, "w"),
            ("prog", w_prog, "center"),
            ("fps", w_fps, "e"),
            ("elapsed", w_elap, "e"),
            ("eta", w_eta, "e"),
        ]
        c.create_rectangle(0, 0, w, hdr_h, fill="#1e293b", outline="")
        c.create_line(0, hdr_h - 1, w, hdr_h - 1, fill="#334155")
        x = 0
        for key, cw, anch in cols:
            txt = str(self._job_table_headings.get(key, key))
            tx = (x + cw // 2) if anch == "center" else ((x + 10) if anch == "w" else (x + cw - 10))
            c.create_text(tx, hdr_h // 2, text=txt, fill="#94a3b8", font=("Segoe UI", 9, "bold"), anchor=anch)
            x += cw
            if key != "eta":
                c.create_line(x, 4, x, hdr_h - 4, fill="#334155")

        y = hdr_h
        if not self._job_ui_order:
            tot_h = int(c.winfo_height() or 112)
            y_mid = hdr_h + max(35, (tot_h - hdr_h) // 2)
            if getattr(self, "is_online_state", False):
                badge_txt = wc.T('w_all_operational_waiting')
                col_txt = "#10b981"
            else:
                badge_txt = wc.T('w_ready_awaiting_manager')
                col_txt = "#94a3b8"
            c.create_rectangle(12, y_mid - 16, w - 12, y_mid + 16, fill="#0b1120", outline="#1e293b")
            c.create_text(w // 2, y_mid, text=badge_txt, fill=col_txt, font=("Segoe UI", 9, "bold"), anchor="center")
            return

        for idx, sid in enumerate(self._job_ui_order):
            row = self._job_ui_rows.get(sid)
            if not row:
                continue
            prog_v = max(0.0, min(100.0, float(row.get("prog", 0.0))))
            bg_row = "#090d16" if idx % 2 == 0 else "#0b1120"
            c.create_rectangle(0, y, w, y + row_h, fill=bg_row, outline="")

            # Plynulý horizontální progress bar v pozadí celého řádku
            fill_w = int((prog_v / 100.0) * w)
            if fill_w > 0:
                is_paused_r = "[PAUSED" in row.get("file", "").upper() or "[POZASTAV" in row.get("file", "").upper() or "[WAITING" in row.get("file", "").upper()
                fill_col = "#78350f" if is_paused_r else "#0c4a6e"
                edge_col = "#f59e0b" if is_paused_r else "#0ea5e9"
                c.create_rectangle(0, y + 1, fill_w, y + row_h - 1, fill=fill_col, outline="")
                c.create_rectangle(0, y + row_h - 3, fill_w, y + row_h - 1, fill=edge_col, outline="")

            c.create_line(0, y + row_h - 1, w, y + row_h - 1, fill="#1e293b")
            vals = {
                "id": sid,
                "file": row.get("file", ""),
                "prog": f"{prog_v:.1f}%",
                "fps": row.get("fps", ""),
                "elapsed": row.get("elapsed", "00h00m00s"),
                "eta": row.get("eta", ""),
            }
            max_file_ch = max(24, int(w_file / 7.4))
            if len(vals["file"]) > max_file_ch:
                vals["file"] = vals["file"][:max_file_ch - 1] + "…"
            x = 0
            for key, cw, anch in cols:
                tx = (x + cw // 2) if anch == "center" else ((x + 10) if anch == "w" else (x + cw - 10))
                fg_c = "#38bdf8" if key == "prog" else ("#cbd5e1" if key in ("elapsed", "eta") else "#f1f5f9")
                c.create_text(tx, y + row_h // 2, text=vals[key], fill=fg_c, font=("Consolas", 9, "bold" if key in ("id", "prog") else "normal"), anchor=anch)
                x += cw
            y += row_h

    def update_job_ui(self, job_id, filename, prog, fps_txt, eta_txt, elapsed_sec=None):
        sid = str(job_id)
        sfn = str(filename)
        prog_f = wc.clamp_num(prog, 0.0, 100.0, 0.0)
        if elapsed_sec is None:
            with self.jobs_lock:
                aj_el = (self.active_jobs.get(job_id) or {}).get("elapsed_sec")
            if aj_el is not None and int(aj_el or 0) > 0:
                el_s = int(aj_el)
            else:
                t0 = self._job_ui_start_ts.setdefault(sid, time.time())
                el_s = max(0, int(time.time() - t0))
        else:
            el_s = max(0, int(elapsed_sec or 0))
        el_str = self.format_elapsed_hms(el_s)

        def _upd():
            if sid not in self._job_ui_order:
                self._job_ui_order.append(sid)
            self._job_ui_rows[sid] = {
                "file": sfn,
                "prog": prog_f,
                "fps": str(fps_txt or ""),
                "elapsed": el_str,
                "eta": str(eta_txt or ""),
            }
            self._redraw_job_table()

        self.run_in_main_thread(_upd)

    def remove_job_ui(self, job_id):
        sid = str(job_id)
        self._job_ui_start_ts.pop(sid, None)

        def _upd():
            if sid in self._job_ui_rows:
                self._job_ui_rows.pop(sid, None)
            if sid in self._job_ui_order:
                self._job_ui_order.remove(sid)
            self._redraw_job_table()

        self.run_in_main_thread(_upd)

    def log(self, message):
        write_file_log(message)

        def _upd():
            if self.root.winfo_exists():
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, str(message) + "\n")
                try:
                    line_cnt = int(str(self.log_text.index("end-1c")).split(".")[0])
                    if line_cnt > 2000:
                        self.log_text.delete("1.0", f"{line_cnt - 1500}.0")
                except Exception:
                    pass
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)

        self.run_in_main_thread(_upd)

    def _sync_rebind_btn(self):
        """Tlačítko „Navázat na tento Manager“ jen při odpovědi jiného Manageru."""
        btn = getattr(self, "rebind_btn", None)
        if btn is None:
            return
        want = bool(wc.foreign_manager_id())
        try:
            if want and not self._rebind_btn_shown:
                btn.pack(side=tk.LEFT, padx=(0, 14), after=self.save_btn)
                self._rebind_btn_shown = True
            elif not want and self._rebind_btn_shown:
                btn.pack_forget()
                self._rebind_btn_shown = False
        except Exception:
            pass

    def rebind_manager_clicked(self):
        """Uživatel potvrdí, že Manager na nastavené adrese je ten správný → worker se na něj naváže."""
        f_id = wc.foreign_manager_id()
        if not f_id:
            self._sync_rebind_btn()
            return
        url = str(self.config.get("server_url") or "")
        if f_id == "-":
            # odpověď bez ID (starší Manager) – jde jen zapomenout vazbu; ID se uloží z první odpovědi, která ho má
            if not messagebox.askyesno(wc.T('w_mgr_bind_title'), wc.T('w_mgr_bind_confirm_noid', url, wc.bound_manager_id()), parent=self.root):
                return
            ok = wc.rebind_to_manager("")
        else:
            if not messagebox.askyesno(wc.T('w_mgr_bind_title'), wc.T('w_mgr_bind_confirm', url, f_id, wc.bound_manager_id()), parent=self.root):
                return
            ok = wc.rebind_to_manager(f_id)
        if not ok:
            messagebox.showerror(wc.T('w_mgr_bind_title'), wc.T('w_mgr_bind_failed'), parent=self.root)
        self._sync_rebind_btn()
        self.force_update_check = True

    def set_connection_status(self, online, mark_checked: bool = False):
        if mark_checked:
            self._initial_conn_checked = True
        self.is_online_state = bool(online)
        if online and not getattr(self, "_ever_connected_logged", False):
            self._ever_connected_logged = True
            self.log(wc.T('w_init_connected_log'))

        def _upd():
            self._sync_rebind_btn()
            if not online and wc.foreign_manager_id():
                self.status_indicator.itemconfig(self.status_circle, fill="#f59e0b")
                self.status_label.config(text=wc.T('w_mgr_bind_status_foreign'), foreground="#f59e0b")
            elif not online:
                if not getattr(self, "_initial_conn_checked", False):
                    self.status_indicator.itemconfig(self.status_circle, fill="#38bdf8")
                    self.status_label.config(text=wc.T('w_ready_awaiting_manager'), foreground="#38bdf8")
                else:
                    self.status_indicator.itemconfig(self.status_circle, fill="#ef4444")
                    self.status_label.config(text=wc.T('w_offline'), foreground="#ef4444")
            else:
                if getattr(self, "needs_manager_url_setup", False) or getattr(self, "url_setup_banner_visible", False):
                    self.needs_manager_url_setup = False
                    self.show_url_setup_banner(False)
                hs = getattr(self, "handshake_state", {}) or {}
                storage_ok = self.is_storage_ok()
                relay_active = self.is_http_relay_active()

                if hs.get("verified") and not storage_ok and relay_active:
                    self.status_indicator.itemconfig(self.status_circle, fill="#38bdf8")
                    self.status_label.config(text=wc.T('w_online_relay'), foreground="#38bdf8")
                elif hs.get("verified") and not storage_ok and not relay_active:
                    self.status_indicator.itemconfig(self.status_circle, fill="#f59e0b")
                    self.status_label.config(text=wc.T('w_manager_online_storage_unreachable'), foreground="#f59e0b")
                elif hs.get("verified") and storage_ok:
                    self.status_indicator.itemconfig(self.status_circle, fill="#10b981")
                    self.status_label.config(text=wc.T('w_online_handshake_rw_ok'), foreground="#10b981")
                else:
                    self.status_indicator.itemconfig(self.status_circle, fill="#f59e0b")
                    self.status_label.config(text=wc.T('w_manager_online_storage_unreachable'), foreground="#f59e0b")
            self.refresh_tray_and_window_icon()
            if not self._job_ui_order:
                self._redraw_job_table()

        self.run_in_main_thread(_upd)

    def update_accept_btn_ui(self):
        def _upd():
            if self.accept_jobs:
                self.accept_btn.config(text=wc.T('w_intake'), image=self.icons["play"], style="Success.TButton")
            else:
                self.accept_btn.config(text=wc.T('w_intake_paused'), image=self.icons["pause"], style="Warn.TButton")
            self.refresh_tray_and_window_icon()

        self.run_in_main_thread(_upd)

    def request_stop(self):
        with self.jobs_lock:
            act_cnt = len(self.active_jobs)
        if act_cnt == 0:
            self.is_running = False
            set_keep_awake(False)
            self._remove_own_ipc_state_file()
            if hasattr(self, "tray_icon"):
                self.tray_icon.remove()
            self.root.after(0, self.root.destroy)
            return
        self.restore_from_tray()
        if messagebox.askyesno(
            wc.T('w_quit_hb_cluster_worker_2'),
            wc.T('w_encoding_progress_jobs_really', act_cnt)
        ):
            self.is_running = False
            set_keep_awake(False)
            self._remove_own_ipc_state_file()
            if hasattr(self, "tray_icon"):
                self.tray_icon.remove()
            with self.jobs_lock:
                for _, jd in self.active_jobs.items():
                    jd["cancelled"] = True
                    if jd.get("process"):
                        try:
                            wc.kill_process_tree(jd["process"], wait_s=2.0)
                        except Exception:
                            pass
            self.root.after(0, self.root.destroy)

    def prompt_uninstall_worker(self):
        title = wc.T('w_uninstall_title')
        msg = wc.T('w_uninstall_confirm_msg')
        if not messagebox.askyesno(title, msg, parent=self.root):
            return

        self.is_running = False
        set_keep_awake(False)
        with self.jobs_lock:
            for _, jd in self.active_jobs.items():
                jd["cancelled"] = True
                if jd.get("process"):
                    try:
                        wc.kill_process_tree(jd["process"], wait_s=2.0)
                    except Exception:
                        pass
        self._remove_own_ipc_state_file()

        base_d = APP_ROOT_PATH
        target_path, work_d = _resolve_launcher_target_and_workdir(base_d)

        try:
            _create_or_remove_windows_shortcuts(base_d, desktop_enabled=False, start_menu_enabled=False)
        except Exception:
            pass

        if os.name == "nt":
            # Autostart has a fixed name: remove it only when it starts THIS installation (or a deleted one) –
            # a newer copy of the worker in another folder keeps its autostart.
            try:
                my_root = wc.win_install_root(base_d)
                run_ours = wc.win_ref_is_ours_or_stale(_read_run_key_command(), my_root)
                _task_cmd = _read_autostart_task_command()
                task_ours = wc.win_ref_is_ours_or_stale(_task_cmd, my_root) if _task_cmd else (not _find_other_install_root(base_d))
            except Exception:
                run_ours = task_ours = True       # helper unavailable → behaviour before 2.12.0
            if run_ours:
                try:
                    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                    try:
                        winreg.DeleteValue(k, "HBClusterWorker")
                    except Exception:
                        pass
                    winreg.CloseKey(k)
                except Exception:
                    pass
            if task_ours:
                try:
                    _delete_before_login_task(allow_uac=True)
                except Exception:
                    pass
            try:
                _hbw_unregister_install(base_d)
            except Exception:
                pass

        cands = [
            os.path.join(work_d, "Uninstall_Worker.bat"),
            os.path.join(base_d, "Uninstall_Worker.bat"),
            os.path.join(base_d, "app", "Uninstall_Worker.bat"),
            os.path.join(os.path.dirname(base_d), "Uninstall_Worker.bat"),
        ]
        uninst_bat = None
        for cb in cands:
            if os.path.isfile(cb):
                uninst_bat = cb
                break

        if hasattr(self, "tray_icon") and self.tray_icon:
            try:
                self.tray_icon.remove()
            except Exception:
                pass

        # Job Object: vypnout KILL_ON_JOB_CLOSE před spuštěním Uninstall_Worker.bat, jinak by ho Windows zabil spolu s tímto procesem!
        try:
            wc.release_windows_kill_on_close_job()
        except Exception:
            pass

        if uninst_bat:
            try:
                subprocess.Popen(["cmd.exe", "/c", uninst_bat, "--silent"], creationflags=0x08000000)
            except Exception:
                pass
        else:
            try:
                del_cmd = f'ping 127.0.0.1 -n 2 >nul & rmdir /s /q "{work_d}"'
                subprocess.Popen(["cmd.exe", "/c", del_cmd], creationflags=0x08000000)
            except Exception:
                pass

        try:
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)

    def load_config(self):
        target_cfg = CONFIG_FILE if os.path.exists(CONFIG_FILE) else (FALLBACK_CONFIG_FILE if os.path.exists(FALLBACK_CONFIG_FILE) else None)
        if target_cfg:
            try:
                with open(target_cfg, 'r', encoding='utf-8-sig') as f:
                    ld = wc.safe_json_loads(f.read(), default=None)
                if not isinstance(ld, dict):
                    return
                migrated = (target_cfg != CONFIG_FILE)
                if not ld.get("server_url") and ld.get("manager_url"):
                    ld["server_url"] = wc.sanitize_manager_url(ld["manager_url"])
                    migrated = True
                if not ld.get("inputs_dir") and ld.get("inputs_path"):
                    ld["inputs_dir"] = wc.sanitize_path(ld["inputs_path"])
                    migrated = True
                if not ld.get("outputs_dir") and ld.get("outputs_path"):
                    ld["outputs_dir"] = wc.sanitize_path(ld["outputs_path"])
                    migrated = True
                if "nas_downloads" in ld:
                    if not ld.get("inputs_dir") and ld["nas_downloads"]:
                        ld["inputs_dir"] = ld["nas_downloads"]
                    ld.pop("nas_downloads", None)
                    migrated = True
                if "nas_staging" in ld:
                    if not ld.get("outputs_dir") and ld["nas_staging"]:
                        ld["outputs_dir"] = ld["nas_staging"]
                    ld.pop("nas_staging", None)
                    migrated = True
                if "manager_url" in ld and not ld.get("server_url"):
                    ld["server_url"] = ld.get("manager_url")
                for k, v in ld.items():
                    if k in ("inputs_dir", "outputs_dir") and not str(v or "").strip() and self.config.get(k):
                        continue
                    if k in ("inputs_dir", "outputs_dir", "temp_dir", "local_temp_dir", "inputs_path", "outputs_path"):
                        self.config[k] = wc.sanitize_path(v)
                    elif k in ("server_url", "manager_url"):
                        self.config["server_url"] = wc.sanitize_manager_url(v)
                    elif k == "worker_name":
                        self.config[k] = wc.sanitize_display_name(v, self.config.get("worker_name", "Win-Worker"))
                    else:
                        self.config[k] = v
                if self.config.get("autostart_mode") not in ("off", "after_login", "before_login"):
                    if self.config.get("run_before_login"):
                        self.config["autostart_mode"] = "before_login"
                    elif self.config.get("run_on_startup"):
                        self.config["autostart_mode"] = "after_login"
                    else:
                        self.config["autostart_mode"] = "off"
                self.config["run_on_startup"] = (self.config["autostart_mode"] == "after_login")
                self.config["run_before_login"] = (self.config["autostart_mode"] == "before_login")
                _su = str(self.config.get("smb_user") or self.config.get("smb_username") or "").strip()
                _sp = str(self.config.get("smb_pass") or self.config.get("smb_password") or "")
                if _su:
                    self.config["smb_user"] = _su
                    self.config["smb_username"] = _su
                if _sp:
                    self.config["smb_pass"] = _sp
                    self.config["smb_password"] = _sp
                self.config.pop("nas_downloads", None)
                self.config.pop("nas_staging", None)
                if migrated:
                    self._atomic_save_config()
            except (OSError, PermissionError, ValueError):
                pass

    def save_config(self):
        self.reset_options_autohide_timer()
        # název ověřit dřív, než se cokoli změní: stávající název (i starší s diakritikou) zůstává,
        # nový se upraví (é->e, ř->r, mezera->'-'); prázdný výsledek = varování a nic se neuloží
        cur_wname = str(self.config.get("worker_name") or "")
        raw_wname = str(self.name_var.get() or "").strip()
        if raw_wname == cur_wname:
            new_wname = cur_wname
        else:
            new_wname = wc.normalize_worker_name(raw_wname)
            if not new_wname:
                try:
                    messagebox.showwarning(wc.T('w_worker_name').rstrip(': '), wc.T('add_wrk_name_invalid'), parent=self.root)
                except Exception:
                    pass
                self.name_var.set(cur_wname)
                return
            if new_wname != raw_wname:
                try:
                    messagebox.showinfo(wc.T('w_worker_name').rstrip(': '), wc.T('w_worker_name_adjusted', new_wname), parent=self.root)
                except Exception:
                    pass
        mgr_u = wc.sanitize_manager_url(self.url_var.get())
        self.config["server_url"] = mgr_u
        self.config["manager_url"] = mgr_u
        if mgr_u:
            self.needs_manager_url_setup = False
            self.show_url_setup_banner(False)
        self.config["worker_name"] = new_wname
        try:
            wc.set_worker_identity(name=self.config["worker_name"])
        except Exception:
            pass
        self.config["inputs_dir"] = wc.sanitize_path(self.in_var.get())
        self.config["outputs_dir"] = wc.sanitize_path(self.out_var.get())
        self.url_var.set(mgr_u)
        self.name_var.set(self.config["worker_name"])
        self.in_var.set(self.config["inputs_dir"])
        self.out_var.set(self.config["outputs_dir"])
        self.config["inputs_path"] = self.config["inputs_dir"]
        self.config["outputs_path"] = self.config["outputs_dir"]
        self.config.pop("nas_downloads", None)
        self.config.pop("nas_staging", None)
        w_lang = wc.get_worker_lang()
        self.config["lang"] = w_lang
        self.config["ui_lang"] = w_lang
        self._start_tray_val = bool(self.start_tray_var.get())
        self._skip_prompt_val = bool(self.skip_prompt_var.get())
        self.config["start_in_tray"] = self._start_tray_val
        self.config["tray_minimize"] = self._start_tray_val
        self.config["skip_tray_prompt"] = self._skip_prompt_val
        if hasattr(self, "autostart_mode_var"):
            m = self.autostart_mode_var.get()
            self._autostart_mode_val = m if m in ("off", "after_login", "before_login") else "off"
            self.config["autostart_mode"] = self._autostart_mode_val
            self.config["run_on_startup"] = (self.config["autostart_mode"] == "after_login")
            self.config["run_before_login"] = (self.config["autostart_mode"] == "before_login")
        self.config["show_log"] = bool(self.log_visible)
        self.config["show_options_bar"] = False
        self.config["accept_jobs"] = bool(self.accept_jobs)
        self.root.title(f"HB Worker [{self.config['worker_name']}] v{CURRENT_VERSION}")
        self.hdr_worker_badge.config(text=f" [{self.config['worker_name']}] ")
        self._atomic_save_config()
        self.collect_diagnostics()
        self.refresh_tray_and_window_icon()
        self.log(wc.T('w_settings_saved_running_bidirection'))
        threading.Thread(target=self.perform_storage_handshake, kwargs={"force_log": True}, daemon=True).start()

    def toggle_accept_jobs(self):
        self.accept_jobs = not self.accept_jobs
        self.pending_accept_override = self.accept_jobs
        self.save_tray_prefs()
        self.update_accept_btn_ui()
        self.update_sleep_prevention_state()
        self.log(wc.T('w_job_intake_state_changed', self.accept_jobs))

        def _sync_en(en_val):
            try:
                wn = self.config.get("worker_name", "")
                srv = self.config.get("server_url", "").rstrip("/")
                r = api_request("POST", f"{srv}/api/v2/workers/{wn}/policy", json={"is_enabled": en_val}, timeout=5)
                if _policy_saved_ok(r) and self.pending_accept_override == en_val:
                    self.pending_accept_override = None
            except Exception:
                pass

        threading.Thread(target=_sync_en, args=(self.accept_jobs,), daemon=True).start()

    def cycle_power_mode(self):
        order = ["AUTO", "FULL", "ECO", "NIGHT", "HYBRID_ECO", "CUSTOM"] if (self.hybrid_cpu or {}).get("is_hybrid") else ["AUTO", "FULL", "ECO", "NIGHT", "CUSTOM"]
        idx = (order.index(self.power_mode) + 1) % len(order) if self.power_mode in order else 0
        self.power_mode = order[idx]
        self.pending_power_override = self.power_mode
        self.apply_current_power_state_now()
        self.log(wc.T('w_switched_power_mode_sending', self.power_mode))

        def _sync(tm):
            try:
                wn = self.config.get("worker_name", "")
                srv = self.config.get("server_url", "").rstrip("/")
                r = api_request("POST", f"{srv}/api/v2/workers/{wn}/policy", json={"power_mode": tm, "custom_cores": list(self.custom_cores)}, timeout=5)
                if _policy_saved_ok(r) and self.pending_power_override == tm:
                    self.pending_power_override = None
            except Exception:
                pass

        threading.Thread(target=_sync, args=(self.power_mode,), daemon=True).start()

    def browse_in(self):
        self.reset_options_autohide_timer()
        p = filedialog.askdirectory(title=wc.T('w_select_inputs_folder'), initialdir=self.in_var.get())
        if p:
            self.in_var.set(os.path.normpath(p))
            self.reset_options_autohide_timer()

    def browse_out(self):
        self.reset_options_autohide_timer()
        p = filedialog.askdirectory(title=wc.T('w_select_outputs_folder'), initialdir=self.out_var.get())
        if p:
            self.out_var.set(os.path.normpath(p))
            self.reset_options_autohide_timer()
    def heartbeat_loop(self):
        while self.is_running:
            time.sleep(5)
            st_data = self.sample_telemetry()
            new_logs = global_log_buffer.drain(50) if global_log_buffer else []
            with self.jobs_lock:
                snap = list(self.active_jobs.items())
            if not snap:
                try:
                    if not self.is_storage_ok() and not self.is_http_relay_active():
                        now_t = time.time()
                        if now_t - getattr(self, "_last_storage_retry", 0) > 20:
                            self._last_storage_retry = now_t
                            threading.Thread(target=self.perform_storage_handshake, kwargs={"force_log": False}, daemon=True).start()

                    rp = api_request(
                        "POST", f"{self.config['server_url']}/api/workers/ping",
                        json={
                            "worker_name": self.config["worker_name"],
                            "version": CURRENT_VERSION,
                            "telemetry": st_data,
                            "storage_ok": bool(self.is_storage_ok() or self.is_http_relay_active()),
                            "power_mode_override": self.pending_power_override,
                            "custom_cores_override": self.pending_custom_cores_override,
                            "ui_lang_override": self.pending_lang_override,
                            "is_enabled_override": self.pending_accept_override,
                            "log_lines": new_logs,
                            "held_job_ids": self.get_held_job_ids(),
                            "windows_settings_state": self.get_windows_settings_state()
                        },
                        timeout=5
                    )
                    if rp.status_code == 200:
                        self.set_connection_status(True, mark_checked=True)
                        rd = rp.json()
                        if self.config.get("join_token"):
                            # Jednorázový schvalovací kód už splnil účel – smazat z paměti i config.json
                            self.config["join_token"] = ""
                            wc.set_worker_identity(join="")
                            try:
                                self.save_tray_prefs()
                            except Exception:
                                pass
                        if rd.get("pending_windows_settings"):
                            self.apply_remote_windows_settings(rd.get("pending_windows_settings"))
                        try:
                            self.manager_waiting_jobs = int(rd.get("queue_waiting") or 0)
                        except (TypeError, ValueError):
                            self.manager_waiting_jobs = 0
                        if getattr(self, "_wake_check_pending", False):
                            # po probuzení: Manager odpověděl – když nic nečeká a není aktualizace, hned pustit spánek
                            self._wake_check_pending = False
                            self._wake_check_until = time.time() + (90 if rd.get("update_required") else 8)
                        if rd.get("cleanup_held_job_ids"):
                            self.discard_held_jobs_by_ids(rd.get("cleanup_held_job_ids"))
                        if rd.get("ui_lang"):
                            self.sync_worker_lang(rd.get("ui_lang"))
                        if rd.get("update_now"):
                            self.request_update_now()
                        if rd.get("update_required") and time.time() >= self.update_blocked_until:
                            self.force_update_check = True
                        if rd.get("is_enabled") is not None:
                            srv_en = bool(rd.get("is_enabled"))
                            if self.pending_accept_override is not None:
                                if srv_en == self.pending_accept_override:
                                    self.pending_accept_override = None
                            elif srv_en != self.accept_jobs:
                                self.accept_jobs = srv_en
                                self.save_tray_prefs()
                                self.update_accept_btn_ui()
                        pm = rd.get("power_mode")
                        self.apply_telemetry_prefs_from_server(rd)
                        if self.pending_power_override and pm == self.pending_power_override:
                            self.pending_power_override = None
                        elif not self.pending_power_override and pm and pm != self.power_mode:
                            self.power_mode = pm
                            self.apply_current_power_state_now()
                            self.log(wc.T('w_power_mode_changed_manager', self.power_mode))
                        if rd.get("custom_cores") is not None:
                            tot_t = int((self.hybrid_cpu or {}).get("total_threads") or (os.cpu_count() or 1))
                            srv_cc = wc.normalize_custom_cores(rd.get("custom_cores"), tot_t)
                            if self.pending_custom_cores_override is not None:
                                if srv_cc == self.pending_custom_cores_override:
                                    self.pending_custom_cores_override = None
                            elif srv_cc and srv_cc != self.custom_cores:
                                self.custom_cores = srv_cc
                                self.config["custom_cores"] = list(srv_cc)
                                self.save_tray_prefs()
                                self.run_in_main_thread(self._refresh_custom_cpu_core_buttons_ui)
                                self.apply_current_power_state_now()
                        if rd.get("smb_username") is not None:
                            new_u = str(rd.get("smb_username") or "").strip()
                            new_p = str(rd.get("smb_password") or "")
                            cur_u = str(self.config.get("smb_user") or self.config.get("smb_username") or "").strip()
                            cur_p = str(self.config.get("smb_pass") or self.config.get("smb_password") or "")
                            if new_u and (new_u != cur_u or new_p != cur_p or self.config.get("smb_user") != new_u or self.config.get("smb_pass") != new_p):
                                self.config["smb_username"] = new_u
                                self.config["smb_user"] = new_u
                                self.config["smb_password"] = new_p
                                self.config["smb_pass"] = new_p
                                self.save_tray_prefs()
                                self.ensure_smb_shares_connected()
                        if not self.config.get("inputs_dir") and rd.get("win_default_inputs_dir"):
                            self.config["inputs_dir"] = str(rd["win_default_inputs_dir"]).strip()
                            self.run_in_main_thread(lambda: self.in_var.set(self.config["inputs_dir"]))
                            self.save_tray_prefs()
                        if not self.config.get("outputs_dir") and rd.get("win_default_outputs_dir"):
                            self.config["outputs_dir"] = str(rd["win_default_outputs_dir"]).strip()
                            self.run_in_main_thread(lambda: self.out_var.set(self.config["outputs_dir"]))
                            self.save_tray_prefs()
                        rg = rd.get("binary_generation")
                        if rg:
                            check_and_apply_binary_generation(int(rg), True, log_fn=self.log)
                except Exception:
                    pass
                continue

            for idx_s, (jid, jd) in enumerate(snap):
                try:
                    r = api_request(
                        "POST", f"{self.config['server_url']}/api/jobs/{jid}/progress",
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
                            "worker_name": self.config["worker_name"],
                            "version": CURRENT_VERSION,
                            "telemetry": st_data,
                            "power_mode_override": self.pending_power_override,
                            "custom_cores_override": self.pending_custom_cores_override,
                            "ui_lang_override": self.pending_lang_override,
                            "is_paused_override": self.pending_pause_override,
                            "is_enabled_override": self.pending_accept_override,
                            "log_lines": new_logs if idx_s == 0 else [],
                            "windows_settings_state": self.get_windows_settings_state() if idx_s == 0 else None
                        },
                        timeout=5
                    )
                    if r.status_code == 200:
                        rd = r.json()
                        if rd.get("pending_windows_settings"):
                            self.apply_remote_windows_settings(rd.get("pending_windows_settings"))
                        if rd.get("ui_lang"):
                            self.sync_worker_lang(rd.get("ui_lang"))
                        if rd.get("update_now"):
                            self.request_update_now()
                        if rd.get("update_required") and time.time() >= self.update_blocked_until:
                            self.force_update_check = True
                        if rd.get("is_enabled") is not None:
                            srv_en = bool(rd.get("is_enabled"))
                            if self.pending_accept_override is not None:
                                if srv_en == self.pending_accept_override:
                                    self.pending_accept_override = None
                            elif srv_en != self.accept_jobs:
                                self.accept_jobs = srv_en
                                self.save_tray_prefs()
                                self.update_accept_btn_ui()
                        pm = rd.get("power_mode")
                        self.apply_telemetry_prefs_from_server(rd)
                        if self.pending_power_override and pm == self.pending_power_override:
                            self.pending_power_override = None
                        elif not self.pending_power_override and pm and pm != self.power_mode:
                            self.power_mode = pm
                            self.apply_current_power_state_now()
                            self.log(wc.T('w_power_mode_changed_manager', self.power_mode))
                        if rd.get("custom_cores") is not None:
                            tot_t = int((self.hybrid_cpu or {}).get("total_threads") or (os.cpu_count() or 1))
                            srv_cc = wc.normalize_custom_cores(rd.get("custom_cores"), tot_t)
                            if self.pending_custom_cores_override is not None:
                                if srv_cc == self.pending_custom_cores_override:
                                    self.pending_custom_cores_override = None
                            elif srv_cc and srv_cc != self.custom_cores:
                                self.custom_cores = srv_cc
                                self.config["custom_cores"] = list(srv_cc)
                                self.save_tray_prefs()
                                self.run_in_main_thread(self._refresh_custom_cpu_core_buttons_ui)
                                self.apply_current_power_state_now()
                        srv_paused = bool(rd.get("is_paused", False))
                        if self.pending_pause_override is not None:
                            if srv_paused == self.pending_pause_override:
                                self.pending_pause_override = None
                        else:
                            if jd.get("user_paused") != srv_paused:
                                jd["user_paused"] = srv_paused
                                self._sync_job_suspend_state(jid)
                                self.update_pause_encode_btn_ui()
                                self.refresh_tray_and_window_icon()
                        st = rd.get("status")
                        if st in ["CANCELLED", "REVOKED"]:
                            self.log(wc.T('w_id_received_cancellation_command', jid, st))
                            if st == "REVOKED" and rd.get("reason"):
                                self.log(wc.T('w_id_job_taken_over', jid, rd.get("reason")))
                            jd["cancelled"] = True
                            self.force_update_check = True
                            if jd.get("process"):
                                try:
                                    wc.kill_process_tree(jd["process"], wait_s=2.0)
                                except Exception:
                                    pass
                except Exception:
                    pass

    def is_job_cancelled(self, job_id):
        with self.jobs_lock:
            jd = self.active_jobs.get(job_id)
            return bool(not jd or jd.get("cancelled"))

    def is_job_user_paused(self, job_id) -> bool:
        with self.jobs_lock:
            jd = self.active_jobs.get(job_id)
            return bool(jd and jd.get("user_paused"))

    def extract_iso_playlist_for_premux(self, job_id: int, iso_path: str, mpls_name: str, job_temp_dir: str, expected_bytes: int = 0, orig_display_name: str = ""):
        sz_b = SEVENZIP_CLI or shutil.which("7z.exe") or shutil.which("7za.exe") or shutil.which("7z")
        if not sz_b or not mpls_name:
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
            sz_b, "x", "-y", iso_path, f"-o{ext_d}",
            "-ir!*PLAYLIST/*.mpls", "-ir!*PLAYLIST/*.MPLS",
            "-ir!*CLIPINF/*.clpi", "-ir!*CLIPINF/*.CLPI",
            "-ir!*BDMV/*.bdmv", "-ir!*BDMV/*.BDMV"
        ]
        subprocess.run(m_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=CREATE_NO_WINDOW)

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

        pl_exact_b, u_clips, mb_iso = wc.parse_bluray_iso_mpls_details(iso_path, m_clean, min_valid_bytes=1024 * 1024, sevenzip_bin=sz_b)
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
        d_title = f"[PRE-MUX 1/2] {orig_display_name or os.path.basename(iso_path)}"
        self.update_job_ui(job_id, d_title, 0.0, "7-Zip", wc.T('w_1_2_extracting_iso'))
        self.log(wc.T('w_id_extracting_playlist_clips', job_id, d_title, m_clean, len(clips)))
        s_cmd = [sz_b, "x", "-y", iso_path, f"-o{ext_d}"] + inc_args
        proc = subprocess.Popen(s_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
        with self.jobs_lock:
            if job_id in self.active_jobs:
                self.active_jobs[job_id]["process"] = proc
                self.active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_1_2')
                if target_b > 0:
                    self.active_jobs[job_id]["src_size_bytes"] = target_b

        last_7z_ui = [0.0, -1, 0.0]

        def _is_canc(rem_chk: bool) -> bool:
            if not self.is_running or self.is_job_cancelled(job_id):
                return True
            if rem_chk and self.check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
                return True
            return False

        def _on_7z_prog(pct, spd, cur_b, tot_b, elap_s, now_t):
            with self.jobs_lock:
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["progress"] = round(pct, 1)
                    self.active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                    self.active_jobs[job_id]["transferred_bytes"] = cur_b
                    if tot_b > 0:
                        self.active_jobs[job_id]["src_size_bytes"] = tot_b
                    self.active_jobs[job_id]["elapsed_sec"] = elap_s
                    self.active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_1_2')
            if now_t - last_7z_ui[0] > 0.8:
                eta_lbl = f"1/2: {cur_b/(1024**3):.1f}/{tot_b/(1024**3):.1f} GB" if tot_b > 0 else f"1/2: {cur_b/(1024**3):.1f} GB"
                self.update_job_ui(job_id, d_title, pct, f"{spd:.1f} MB/s", eta_lbl)
                last_7z_ui[0] = now_t
            if int(pct) >= last_7z_ui[1] + 10 and now_t - last_7z_ui[2] > 8.0:
                self.log(wc.T('w_pre_mux_1_2_2', job_id, f'{pct:.1f}', f'{spd:.1f}', f'{cur_b / 1024 ** 3:.1f}'))
                last_7z_ui[1] = int(pct)
                last_7z_ui[2] = now_t

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
            self.log(wc.T('w_id_iso_extraction_integrity', job_id, len(ordered_clip_files), len(clips), f'{ext_b / 1024 ** 3:.2f}'))
            shutil.rmtree(ext_d, ignore_errors=True)
            return "", 0, []

        verified_mpls_p = wc.resolve_bdmv_mpls_path(ext_d, m_clean, require_stream_dir=True) or ext_mpls_p
        return (verified_mpls_p if (verified_mpls_p and os.path.isfile(verified_mpls_p)) else ""), ext_b, ordered_clip_files

    def _run_mkvmerge_process_with_progress(self, job_id: int, mm_cmd: list, p_out: str, expected_bytes: int, d_title: str) -> int:
        mm_p = subprocess.Popen(
            mm_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
        )

        with self.jobs_lock:
            if job_id in self.active_jobs:
                self.active_jobs[job_id]["process"] = mm_p
                self.active_jobs[job_id]["progress"] = 0.0
                self.active_jobs[job_id]["transferred_bytes"] = 0
                self.active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_2_2')

        last_mm_ui = [0.0, -1, 0.0]

        def _is_canc(rem_chk: bool) -> bool:
            if not self.is_running or self.is_job_cancelled(job_id):
                return True
            if rem_chk and self.check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
                return True
            return False

        def _on_mm_prog(pct, spd, cur_b, tot_b, elap_s, now_t):
            with self.jobs_lock:
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["progress"] = round(pct, 1)
                    self.active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                    self.active_jobs[job_id]["transferred_bytes"] = cur_b
                    if tot_b > 0:
                        self.active_jobs[job_id]["src_size_bytes"] = tot_b
                    self.active_jobs[job_id]["elapsed_sec"] = elap_s
                    self.active_jobs[job_id]["step_message"] = wc.T('w_pre_mux_2_2')
            if now_t - last_mm_ui[0] > 0.8:
                eta_lbl = f"2/2: {cur_b/(1024**3):.1f}/{tot_b/(1024**3):.1f} GB" if tot_b > 0 else f"2/2: {cur_b/(1024**3):.1f} GB"
                self.update_job_ui(job_id, d_title, pct, f"{spd:.1f} MB/s", eta_lbl)
                last_mm_ui[0] = now_t
            if int(pct) >= last_mm_ui[1] + 10 and now_t - last_mm_ui[2] > 8.0:
                self.log(wc.T('w_pre_mux_2_2_2', job_id, f'{pct:.1f}', f'{spd:.1f}', f'{cur_b / 1024 ** 3:.1f}'))
                last_mm_ui[1] = int(pct)
                last_mm_ui[2] = now_t

        return wc.run_mkvmerge_with_disk_progress(mm_p, p_out, expected_bytes, _is_canc, _on_mm_prog)

    def run_local_mkvmerge_premux(self, job_id: int, orig_source: str, mpls_name: str, job_temp_dir: str, expected_src_bytes: int = 0, orig_display_name: str = "", job_dur_s: float = 0.0, allow_raw_m2ts_fallback: bool = True) -> str:
        mm_b = MKVMERGE_CLI or shutil.which("mkvmerge.exe") or shutil.which("mkvmerge")
        if not mm_b:
            return ""

        m_lbl = orig_display_name or os.path.basename(orig_source.rstrip("\\/"))
        d_title = f"[PRE-MUX 2/2] {m_lbl}"

        try:
            api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/premux", timeout=5)
        except Exception:
            pass

        t_in = None
        playlist_clips_bytes = 0
        extracted_clips = []
        iso_clean_d = os.path.join(job_temp_dir, "ISO_EXTRACT")

        if os.path.isfile(orig_source) and orig_source.lower().endswith(".iso") and mpls_name:
            t_in, playlist_clips_bytes, extracted_clips = self.extract_iso_playlist_for_premux(job_id, orig_source, mpls_name, job_temp_dir, expected_bytes=expected_src_bytes, orig_display_name=m_lbl)
        elif os.path.isdir(orig_source):
            if not mpls_name:
                det_m, _, _, _ = wc.find_matching_mpls_in_bdmv(orig_source, target_duration_sec=job_dur_s)
                if det_m:
                    mpls_name = det_m
                    self.log(wc.T('w_id_automatically_resolved_matching', job_id, mpls_name))
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
                with self.jobs_lock:
                    if job_id in self.active_jobs:
                        if playlist_clips_bytes > 0:
                            self.active_jobs[job_id]["src_size_bytes"] = playlist_clips_bytes
                        if vp_m2ts > 0 and vp_m2ts != 2500:
                            self.active_jobs[job_id]["v_bitrate_in_kbps"] = vp_m2ts
                        if ap_m2ts > 0:
                            self.active_jobs[job_id]["a_bitrate_in_kbps"] = ap_m2ts
            except Exception:
                pass

        if not t_in or not os.path.isfile(t_in):
            shutil.rmtree(iso_clean_d, ignore_errors=True)
            return ""

        ref_b = playlist_clips_bytes if playlist_clips_bytes > 0 else expected_src_bytes
        p_out = os.path.join(job_temp_dir, "premux_source.mkv")
        self.update_job_ui(job_id, d_title, 0.0, "mkvmerge", wc.T('w_2_2_muxing_mkv'))
        self.log(wc.T('w_id_starting_mkvmerge_premux', job_id, d_title, os.path.basename(t_in), len(extracted_clips) or 1))
        rc = self._run_mkvmerge_process_with_progress(job_id, [mm_b, "--gui-mode", "-o", p_out, t_in], p_out, ref_b, d_title)

        if self.is_job_cancelled(job_id):
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
                self.log(f"ID {job_id}: Pre-Mux raw M2TS fallback skipped (low temp disk space: {free_gb_pre:.1f} GB)")
            else:
                self.log(wc.T('w_id_mkvmerge_mpls_returned', job_id, d_title, rc, len(extracted_clips)))
                direct_cmd = [mm_b, "--gui-mode", "-o", p_out, extracted_clips[0]]
                for extra_c in extracted_clips[1:]:
                    direct_cmd.extend(["+", extra_c])
                rc = self._run_mkvmerge_process_with_progress(job_id, direct_cmd, p_out, ref_b, d_title)

        shutil.rmtree(iso_clean_d, ignore_errors=True)

        if self.is_job_cancelled(job_id):
            raise InterruptedError("Cancelled")

        if rc in (0, 1) and os.path.isfile(p_out):
            osz = os.path.getsize(p_out)
            if osz > 5 * 1024 * 1024 and (ref_b <= 0 or osz >= ref_b * 0.70):
                self.log(wc.T('w_id_completed_verified_gb', job_id, d_title, f'{osz / 1024 ** 3:.2f}'))
                return p_out
            self.log(wc.T('w_id_pre_mux_output', job_id, f'{osz / 1024 ** 3:.2f}', f'{ref_b / 1024 ** 3:.2f}'))
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
        self.log(wc.T('w_id_mkvmerge_pre_mux', job_id, rc))
        return ""
    def worker_loop(self):
        l_clean = time.time()
        l_upd = 0
        l_diag = time.time()
        upd_pend = False
        upd_target = ""

        while self.is_running:
            try:
                ct = time.time()
                if ct - l_diag > 60:
                    self.collect_diagnostics()
                    l_diag = ct
                if self._ota_clear_at and ct >= self._ota_clear_at:
                    self._ota_clear_at = 0.0
                    wc.clear_ota_attempt(APP_ROOT_PATH)   # nová verze běží 5 min – záznam o pokusu už netřeba

                with self.jobs_lock:
                    act_cnt = len(self.active_jobs)
                held_ids = self.get_held_job_ids()

                if ct - l_clean > 300:
                    if act_cnt == 0 and self.uploading_held_job_id is None:
                        self.cleanup_temp_dir()
                    self.perform_storage_handshake(force_log=False)
                    l_clean = ct

                if self.force_update_check or (ct - l_upd > 30):
                    self.force_update_check = False
                    try:
                        rv = api_request("GET", f"{self.config['server_url']}/api/update/version", timeout=5)
                        if rv.status_code == 200:
                            vd = rv.json()
                            if vd.get("ui_lang"):
                                self.sync_worker_lang(vd.get("ui_lang"))
                            lv = vd.get("version")
                            self.remote_binary_gen = vd.get("binary_generation")
                            self.remote_win_binaries = (vd.get("binaries") or {}).get("windows")
                            if lv and lv != CURRENT_VERSION:
                                upd_pend = True
                                upd_target = str(lv)
                    except Exception:
                        pass
                    if self.remote_win_binaries:
                        sync_and_resolve_binaries(
                            self.config["server_url"], allow_download=True,
                            allow_hb_update=(act_cnt == 0), win_manifest=self.remote_win_binaries,
                            remote_gen=self.remote_binary_gen, log_fn=self.log
                        )
                        self.collect_diagnostics()
                    if ct - getattr(self, "_last_pkg_sync", 0) > 600:
                        # Ostatní soubory balíčku (ikona, .bat, README, certifikát) – podle otisku, kdykoliv.
                        self._last_pkg_sync = ct
                        try:
                            wc.sync_windows_package_files(self.config["server_url"], os.path.dirname(os.path.abspath(__file__)), api_request, log_fn=self.log)
                        except Exception:
                            pass
                    l_upd = ct

                if upd_pend and time.time() < self.update_blocked_until:
                    upd_pend = False

                if upd_pend:
                    with self.held_lock:
                        can_start_upd = (act_cnt == 0 and self.uploading_held_job_id is None)
                        if can_start_upd:
                            self.update_in_progress = True
                    if can_start_upd:
                        try:
                            self.log(wc.T('w_new_version_detected_server'))
                            sp = os.path.abspath(__file__)
                            sd = os.path.dirname(sp)
                            # O1: nová verze se stáhne podle manifestu, zkusmo načte (--selftest) a připraví se
                            # *.tmp + zálohy *.prev; update_worker.bat ji spustí a když do 2 min nenaběhne, vrátí starou.
                            res = wc.stage_windows_update(self.config['server_url'], sd, sp, api_request, CURRENT_VERSION,
                                                          selftest_fn=run_ota_selftest, log_fn=self.log)
                            if res.get("ok"):
                                try:
                                    wc.flush_held_jobs_metadata(self.held_jobs, self.held_lock, LOCAL_TEMP_DIR)
                                    ota_win_state = self.get_current_window_state_for_ota()
                                    self.config["ota_restore_window_state"] = ota_win_state
                                    self._atomic_save_config()
                                    self._remove_own_ipc_state_file()
                                    bp = os.path.join(sd, "update_worker.bat")
                                    with open(bp, "w", encoding="utf-8", newline="") as b:
                                        b.write(wc.build_windows_update_bat(sd, sp, sys.executable, ota_win_state))

                                    if hasattr(self, "tray_icon"):
                                        self.tray_icon.remove()
                                    # Job Object: vypnout KILL_ON_JOB_CLOSE, jinak by Windows update skript
                                    # ukončil spolu s tímto procesem.
                                    wc.release_windows_kill_on_close_job()
                                    subprocess.Popen(["cmd.exe", "/c", bp], cwd=sd, creationflags=CREATE_NO_WINDOW)
                                    self.log(wc.T('w_both_files_worker_core'))
                                    self.is_running = False
                                    os._exit(0)
                                except Exception as e:
                                    self.log(f"Error starting update_worker.bat: {e}")
                                    wc.clear_ota_attempt(sd)
                                    for _n in wc.OTA_FILES_WINDOWS:
                                        try:
                                            os.remove(os.path.join(sd, _n + ".tmp"))
                                        except OSError:
                                            pass
                                    nxt = self.note_update_failure(res.get("target") or upd_target, "error", 300, str(e))
                                    self.log(wc.T('w_update_win_failed', "bat", str(e)[:120], wc.fmt_retry_time(nxt)))
                                    upd_pend = False
                                    time.sleep(10)
                                    continue
                            else:
                                rsn = str(res.get("reason") or "error")
                                upd_pend = False
                                if rsn == "same":
                                    self.update_blocked_until = time.time() + 60
                                else:
                                    nxt = self.note_update_failure(res.get("target") or upd_target, rsn, 300, res.get("detail") or "")
                                    if rsn == "selftest_failed":
                                        self.log(wc.T('w_update_selftest_failed', res.get("target") or "?", res.get("detail") or "?", wc.fmt_retry_time(nxt)))
                                    else:
                                        self.log(wc.T('w_update_win_failed', rsn, res.get("detail") or "", wc.fmt_retry_time(nxt)))
                                time.sleep(10)
                                continue
                        finally:
                            if self.is_running:
                                self.update_in_progress = False
                    else:
                        time.sleep(5)
                        continue

                if not self.accept_jobs:
                    time.sleep(5)
                    continue

                st_data = self.sample_telemetry()
                with self.jobs_lock:
                    r_ids = list(self.active_jobs.keys())
                prms = {
                    "worker_name": self.config["worker_name"],
                    "version": CURRENT_VERSION,
                    "running": json.dumps(r_ids),
                    "held": json.dumps(held_ids),
                    "telemetry": json.dumps(st_data),
                    "storage_ok": "true" if (self.is_storage_ok() or self.is_http_relay_active()) else "false"
                }
                if time.time() < self.update_blocked_until:
                    prms["update_blocked"] = "true"
                if self.pending_power_override:
                    prms["power_mode_override"] = self.pending_power_override
                if self.pending_custom_cores_override is not None:
                    prms["custom_cores_override"] = json.dumps(self.pending_custom_cores_override)
                if self.pending_lang_override:
                    prms["ui_lang_override"] = self.pending_lang_override
                if self.pending_accept_override is not None:
                    prms["is_enabled_override"] = self.pending_accept_override

                resp = api_request("GET", f"{self.config['server_url']}/api/jobs/next", params=prms, timeout=20)
                if resp.status_code == 200:
                    if not self.manager_reachable:
                        self.manager_reachable = True
                        wc.sync_locales_from_manager(self.config.get("server_url"))
                        self.log(wc.T('w_connection_hb_cluster_manager', self.config['server_url']))
                        threading.Thread(target=self.perform_storage_handshake, kwargs={"force_log": False}, daemon=True).start()
                    self.set_connection_status(True, mark_checked=True)
                    d = resp.json()
                    if d.get("pending_windows_settings"):
                        self.apply_remote_windows_settings(d.get("pending_windows_settings"))
                    if d.get("cleanup_held_job_ids"):
                        self.discard_held_jobs_by_ids(d.get("cleanup_held_job_ids"))
                    if d.get("ui_lang"):
                        self.sync_worker_lang(d.get("ui_lang"))
                    if d.get("update_now"):
                        self.request_update_now()
                    if d.get("update_required") and time.time() >= self.update_blocked_until:
                        upd_pend = True
                        upd_target = str(d.get("target_version") or upd_target)
                        continue
                    if d.get("is_enabled") is not None:
                        srv_en = bool(d.get("is_enabled"))
                        if self.pending_accept_override is not None:
                            if srv_en == self.pending_accept_override:
                                self.pending_accept_override = None
                        elif srv_en != self.accept_jobs:
                            self.accept_jobs = srv_en
                            self.save_tray_prefs()
                            self.update_accept_btn_ui()
                    rg = d.get("binary_generation")
                    if rg and act_cnt == 0:
                        check_and_apply_binary_generation(int(rg), True, log_fn=self.log)
                    pm = d.get("power_mode")
                    self.apply_telemetry_prefs_from_server(d)
                    if self.pending_power_override and pm == self.pending_power_override:
                        self.pending_power_override = None
                    elif not self.pending_power_override and pm and pm != self.power_mode:
                        self.power_mode = pm
                        self.apply_current_power_state_now()
                        self.log(wc.T('w_power_mode_changed_manager', self.power_mode))
                    if d.get("custom_cores") is not None:
                        tot_t = int((self.hybrid_cpu or {}).get("total_threads") or (os.cpu_count() or 1))
                        srv_cc = wc.normalize_custom_cores(d.get("custom_cores"), tot_t)
                        if self.pending_custom_cores_override is not None:
                            if srv_cc == self.pending_custom_cores_override:
                                self.pending_custom_cores_override = None
                        elif srv_cc and srv_cc != self.custom_cores:
                            self.custom_cores = srv_cc
                            self.config["custom_cores"] = list(srv_cc)
                            self.save_tray_prefs()
                            self.run_in_main_thread(self._refresh_custom_cpu_core_buttons_ui)
                            self.apply_current_power_state_now()
                    if d.get("job_id"):
                        if not self.is_storage_ok() and not self.is_http_relay_active():
                            # Úložiště není dostupné a není zapnut HTTP relay -> novou úlohu nepřijímat
                            jid_rej = d["job_id"]
                            try:
                                api_request("POST", f"{self.config['server_url']}/api/jobs/{jid_rej}/cancel", json={"worker_name": self.config["worker_name"], "reason": "Storage unreachable"}, timeout=5)
                            except Exception:
                                pass
                            continue
                        jid = d["job_id"]
                        self.log(wc.T('w_acquired_job_id_preparing', jid))

                        with self.jobs_lock:
                            self.active_jobs[jid] = {
                                "progress": 0.0, "fps_current": 0.0, "fps_avg": 0.0, "speed_mbps": 0.0,
                                "elapsed_sec": 0, "eta_sec": 0, "transferred_bytes": 0, "src_size_bytes": 0,
                                "is_net_fallback": False, "net_fallback_reason": "",
                                "v_bitrate_in_kbps": 0, "a_bitrate_in_kbps": 0,
                                "premux_applied": False, "premux_status": "", "step_message": None,
                                "process": None, "cancelled": False, "user_paused": False, "disk_paused": False,
                                "pause_guard_state": {"suspended": False, "reason": "", "lock": threading.Lock()}
                            }
                        self.update_pause_encode_btn_ui()

                        t = threading.Thread(
                            target=self.process_job_thread,
                            args=(
                                jid, d["payload"], d.get("verified_rel_source", ""), d.get("use_local_temp", True),
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
                else:
                    self.set_connection_status(False, mark_checked=True)

            except Exception:
                if self.manager_reachable:
                    self.manager_reachable = False
                    self.log(wc.T('w_hb_cluster_manager_temporarily', self.config.get('server_url')))
                self.set_connection_status(False, mark_checked=True)
                # Parita s Linuxem: při delším výpadku zkusit každých 60 s UDP objev Manageru (chráněno manager_binding.json)
                now_d = time.time()
                if (now_d - float(getattr(self, "_last_udp_discovery_ts", 0.0) or 0.0)) > 60.0:
                    self._last_udp_discovery_ts = now_d
                    try:
                        disc_url, disc_resp = wc.discover_manager_udp(port=UDP_DISCOVERY_PORT, timeout=2.5, prefer_bound=True)
                        bound_id = wc.bound_manager_id()
                        resp_mid = str((disc_resp or {}).get("manager_id") or "").strip()
                        if disc_url and disc_url != self.config.get("server_url"):
                            if (not bound_id or not resp_mid or resp_mid == bound_id) and self._probe_manager_reachable(disc_url, timeout=3.0):
                                self.config["server_url"] = disc_url
                                self.config["manager_url"] = disc_url
                                self._atomic_save_config()
                                self.run_in_main_thread(lambda _u=disc_url: self.url_var.set(_u))
                                self.log(f"[UDP-DISCOVERY] Manager relocated to {disc_url}")
                    except Exception:
                        pass
            time.sleep(5)

    def check_job_status(self, job_id):
        if self.is_job_cancelled(job_id):
            return "CANCELLED"
        try:
            r = api_request("GET", f"{self.config['server_url']}/api/jobs/{job_id}/status", params={"worker_name": self.config["worker_name"]}, timeout=5)
            if r.status_code == 200:
                sd = r.json()
                if sd.get("ui_lang"):
                    self.sync_worker_lang(sd.get("ui_lang"))
                st = sd.get("status")
                srv_p = bool(sd.get("is_paused", False))
                pause_changed = False
                with self.jobs_lock:
                    if job_id in self.active_jobs:
                        if self.pending_pause_override is not None:
                            if srv_p == self.pending_pause_override:
                                self.pending_pause_override = None
                        else:
                            if self.active_jobs[job_id].get("user_paused") != srv_p:
                                self.active_jobs[job_id]["user_paused"] = srv_p
                                pause_changed = True
                        if st in ["CANCELLED", "REVOKED"]:
                            self.active_jobs[job_id]["cancelled"] = True
                if pause_changed:
                    self._sync_job_suspend_state(job_id)
                    self.update_pause_encode_btn_ui()
                    self.refresh_tray_and_window_icon()
                return st
        except Exception:
            pass
        return "IN_PROGRESS"

    def process_job_thread(
        self, job_id, payload, verified_rel_source, use_local_write, staging_mode, convert_ass,
        auto_clean_empty_subs, auto_extract_forced_subs, write_mkv_stats,
        lpcm_passthru, smart_premux, ineffective_action="both",
        ineffective_prefix="[HIGHER_BITRATE]_", ineffective_subfolder="_HIGHER_BITRATE_THAN_SOURCE",
        burn_in_mode="separate", dts_hd_mode="keep_both"
    ):
        hb_p = None
        t0 = time.time()
        j_tmp = os.path.join(LOCAL_TEMP_DIR, f"job_{job_id}")
        pm_done = False
        pm_stat = ""
        keep_temp_for_disk_hold = False
        use_relay_mode = False
        encode_completed_ok = False
        saved_meta_obj = None
        w_dest = ""
        orig_d = ""
        tmp_orig_d = ""
        src_sz = 0
        v_in = 0
        a_in = 0

        with self.jobs_lock:
            prev_jd = self.active_jobs.get(job_id) or {}
            pause_guard_state = prev_jd.get("pause_guard_state") or {"suspended": False, "reason": "", "lock": threading.Lock()}
            self.active_jobs[job_id] = {
                "progress": 0.0, "fps_current": 0.0, "fps_avg": 0.0, "speed_mbps": 0.0,
                "elapsed_sec": 0, "eta_sec": 0, "transferred_bytes": 0, "src_size_bytes": 0,
                "is_net_fallback": False, "net_fallback_reason": "",
                "v_bitrate_in_kbps": 0, "a_bitrate_in_kbps": 0,
                "premux_applied": False, "premux_status": "", "step_message": None,
                "process": None,
                "cancelled": bool(prev_jd.get("cancelled", False)),
                "user_paused": bool(prev_jd.get("user_paused", False)),
                "disk_paused": False,
                "pause_guard_state": pause_guard_state
            }
        if self.is_job_cancelled(job_id):
            with self.jobs_lock:
                self.active_jobs.pop(job_id, None)
            self.update_pause_encode_btn_ui()
            self.refresh_tray_and_window_icon()
            return
        self.update_pause_encode_btn_ui()
        self.update_job_ui(job_id, wc.T('w_loading_job'), 0.0, "", wc.T('w_preparing'))
        self.refresh_tray_and_window_icon()

        def _check_cancel(remote_check: bool) -> bool:
            if not self.is_running or self.is_job_cancelled(job_id):
                return True
            if remote_check and self.check_job_status(job_id) in ["CANCELLED", "REVOKED"]:
                return True
            return False

        try:
            os.makedirs(j_tmp, exist_ok=True)
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
            orig_s = self.resolve_worker_source_path(r_src, verified_rel_source)
            orig_disp = os.path.basename((orig_s or r_src).replace("\\", "/").rstrip("/")) or f"Job_{job_id}"

            if orig_s and orig_s.startswith("\\\\") and not os.path.exists(orig_s):
                self.ensure_smb_shares_connected()

            if not orig_s or not os.path.exists(orig_s):
                self.log(wc.T('w_id_source_not_locally', job_id, orig_s))
                self.update_job_ui(job_id, f"[HTTP RELAY] {orig_disp}", 0.0, "HTTP Relay", wc.T('w_downloading'))
                try:
                    api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/staging", timeout=5)
                except Exception:
                    pass
                relay_s_dir = os.path.join(j_tmp, "SOURCE")
                last_rel_ui = [0.0, -1, 0.0]

                def _on_rel_dl(prog, spd, copied_b, elap_s, now_t):
                    with self.jobs_lock:
                        if job_id in self.active_jobs:
                            self.active_jobs[job_id]["progress"] = prog
                            self.active_jobs[job_id]["fps_current"] = 0.0
                            self.active_jobs[job_id]["fps_avg"] = 0.0
                            self.active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                            self.active_jobs[job_id]["transferred_bytes"] = copied_b
                            self.active_jobs[job_id]["elapsed_sec"] = elap_s
                    if now_t - last_rel_ui[0] > 0.6:
                        self.update_job_ui(job_id, f"[HTTP RELAY] {orig_disp}", prog, f"{spd:.1f} MB/s", f"Relay: {copied_b/(1024**3):.1f} GB")
                        last_rel_ui[0] = now_t
                    if int(prog) >= last_rel_ui[1] + 5 and now_t - last_rel_ui[2] > 5.0:
                        self.log(wc.T('w_http_relay_download_job', job_id, f'{prog:.1f}', f'{spd:.1f}'))
                        last_rel_ui[1] = int(prog)
                        last_rel_ui[2] = now_t

                try:
                    orig_s = wc.relay_download_source_to_temp(
                        self.config["server_url"].rstrip("/"), job_id, self.config["worker_name"],
                        relay_s_dir, _check_cancel, _on_rel_dl
                    )
                    orig_disp = os.path.basename(orig_s.rstrip("\\/"))
                    use_relay_mode = True
                    use_local_write = True
                except InterruptedError:
                    raise
                except Exception as rel_err:
                    is_enospc = isinstance(rel_err, OSError) and (getattr(rel_err, "errno", 0) == errno.ENOSPC or getattr(rel_err, "winerror", 0) == 112 or "space" in str(rel_err).lower())
                    self.log(wc.T('w_id_source_missing_http', job_id, orig_s, rel_err))
                    try:
                        if is_enospc:
                            api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/error", json={"error_type": "GENERIC", "error_code": "ERR_WORKER_DISK_FULL", "error_detail": str(rel_err)[-250:], "version": CURRENT_VERSION}, timeout=5)
                        else:
                            api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/error", json={"error_type": "FILE", "error_code": "ERR_SOURCE_MISSING", "error_detail": orig_s, "version": CURRENT_VERSION}, timeout=5)
                    except Exception:
                        pass
                    return

            self.update_job_ui(job_id, orig_disp, 0.0, "", wc.T('w_probing_source'))
            hb_bin = sync_and_resolve_binaries(self.config.get("server_url"), allow_download=False)
            src_sz, v_in, a_in, mpls_n = wc.probe_source_size_and_bitrates(
                orig_s, job_node, hb_bin, root_item=it, ffprobe_bin=FFPROBE_CLI, sevenzip_bin=SEVENZIP_CLI
            )
            phys_sz = wc.get_file_size(orig_s)
            job_dur_s = wc.extract_job_duration_seconds(job_node, it)

            with self.jobs_lock:
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["src_size_bytes"] = src_sz
                    self.active_jobs[job_id]["v_bitrate_in_kbps"] = v_in
                    self.active_jobs[job_id]["a_bitrate_in_kbps"] = a_in

            dv = job_node.get("Destination", "")
            g_dest = str(dv.get("File", "")) if isinstance(dv, dict) else str(dv)
            if not g_dest and isinstance(it.get("uiSettings"), dict):
                g_dest = str(it["uiSettings"].get("destination") or "")
            d_base = os.path.basename(g_dest.replace("\\", "/")) or (os.path.splitext(orig_disp)[0] + ".mkv")

            bn_only, o_ext = os.path.splitext(d_base)
            if o_ext.lower() not in ['.mkv', '.mp4', '.m4v', '.webm', '.mov']:
                d_base = bn_only + ".mkv"

            st_p = self.get_outputs_dir()
            if not st_p or not os.path.isdir(st_p):
                use_relay_mode = True
                use_local_write = True
                st_p = j_tmp

            orig_d = os.path.normpath(os.path.join(st_p, d_base))
            tmp_orig_d = orig_d + ".tmp"
            if not use_relay_mode:
                o_refuse = wc.output_target_refusal(orig_d, orig_s, self.get_inputs_dir())
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
                    self.log(wc.T('w_id_premux_skip_partial_audio', job_id))

                is_4k_job = wc.is_job_4k_target(job_node, it, orig_s)
                sp_plan = wc.evaluate_disk_space_strategy(
                    LOCAL_TEMP_DIR, st_p, phys_sz, src_sz, is_4k_job,
                    use_local_write, do_stg, eff_try_premux, is_disc, s_low.endswith(".iso"), use_relay_mode
                )

                if sp_plan["premux_skip_reason"]:
                    self.log(f"ID {job_id}: {sp_plan['premux_skip_reason']}")
                if sp_plan["staging_skip_reason"]:
                    self.log(f"ID {job_id}: {sp_plan['staging_skip_reason']}")
                if sp_plan["direct_outputs_reason"]:
                    self.log(f"ID {job_id}: {sp_plan['direct_outputs_reason']}")

                do_stg = sp_plan["allow_staging"]
                use_local_write = sp_plan["allow_local_write"]
                fb_net = sp_plan["is_net_fallback"]
                fb_reason = sp_plan.get("net_fallback_reason", "")
                with self.jobs_lock:
                    if job_id in self.active_jobs:
                        self.active_jobs[job_id]["is_net_fallback"] = bool(fb_net)
                        self.active_jobs[job_id]["net_fallback_reason"] = fb_reason

                if is_disc:
                    if eff_try_premux and use_local_write:
                        pm_stat = "FALLBACK"
                        if sp_plan["allow_premux"]:
                            try:
                                pm_p = self.run_local_mkvmerge_premux(
                                    job_id, orig_s, mpls_n, j_tmp, expected_src_bytes=src_sz,
                                    orig_display_name=orig_disp, job_dur_s=job_dur_s,
                                    allow_raw_m2ts_fallback=(not is_chapter_rng_job)
                                )
                            except InterruptedError:
                                raise
                            except Exception as pm_err:
                                self.log(f"ID {job_id}: Pre-Mux failed ({pm_err}) - falling back to direct encode.")
                                pm_p = ""
                            with self.jobs_lock:
                                if job_id in self.active_jobs:
                                    if self.active_jobs[job_id].get("v_bitrate_in_kbps", 0) > 0:
                                        v_in = self.active_jobs[job_id]["v_bitrate_in_kbps"]
                                    if self.active_jobs[job_id].get("a_bitrate_in_kbps", 0) > 0:
                                        a_in = self.active_jobs[job_id]["a_bitrate_in_kbps"]
                                    if self.active_jobs[job_id].get("src_size_bytes", 0) > 0:
                                        src_sz = self.active_jobs[job_id]["src_size_bytes"]
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

                    with self.jobs_lock:
                        if job_id in self.active_jobs:
                            self.active_jobs[job_id]["src_size_bytes"] = src_sz
                            self.active_jobs[job_id]["v_bitrate_in_kbps"] = v_in
                            self.active_jobs[job_id]["a_bitrate_in_kbps"] = a_in
                            self.active_jobs[job_id]["premux_applied"] = pm_done
                            self.active_jobs[job_id]["premux_status"] = pm_stat
                            self.active_jobs[job_id]["step_message"] = ""
                            self.active_jobs[job_id]["speed_mbps"] = 0.0
                            self.active_jobs[job_id]["transferred_bytes"] = 0

                ui_m_lbl = f"{orig_disp} [Pre-Mux]" if pm_done else orig_disp
                w_dest = os.path.normpath(os.path.join(j_tmp, d_base) if use_local_write else tmp_orig_d)
                os.makedirs(os.path.dirname(w_dest), exist_ok=True)

                if do_stg:
                    s_dir = os.path.join(j_tmp, "SOURCE")
                    os.makedirs(s_dir, exist_ok=True)
                    s_base = os.path.basename(orig_s.rstrip("\\/")) or "SOURCE"
                    s_src = os.path.join(s_dir, s_base)
                    stg_lbl = f"[{wc.T('w_staging_2')}] {ui_m_lbl}"

                    self.update_job_ui(job_id, stg_lbl, 0.0, "", wc.T('w_staging'))
                    self.log(wc.T('w_id_starting_local_source', job_id, f'{phys_sz / 1024 ** 3:.2f}'))
                    try:
                        api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/staging", timeout=5)
                    except Exception:
                        pass

                    last_stg_ui = [0.0, -1, 0.0]

                    def _on_stg_prog(prog, spd, copied_b, elap_s, now_t):
                        with self.jobs_lock:
                            if job_id in self.active_jobs:
                                self.active_jobs[job_id]["progress"] = prog
                                self.active_jobs[job_id]["fps_current"] = 0.0
                                self.active_jobs[job_id]["fps_avg"] = 0.0
                                self.active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                                self.active_jobs[job_id]["transferred_bytes"] = copied_b
                                self.active_jobs[job_id]["src_size_bytes"] = phys_sz
                                self.active_jobs[job_id]["elapsed_sec"] = elap_s
                        if now_t - last_stg_ui[0] > 0.6:
                            self.update_job_ui(job_id, stg_lbl, prog, f"{spd:.1f} MB/s", f"{copied_b/(1024**3):.1f} / {phys_sz/(1024**3):.1f} GB")
                            last_stg_ui[0] = now_t
                        if int(prog) >= last_stg_ui[1] + 5 and now_t - last_stg_ui[2] > 5.0:
                            self.log(wc.T('w_staging_job_mb_s', job_id, f'{prog:.1f}', f'{spd:.1f}', f'{copied_b / 1024 ** 3:.1f}', f'{phys_sz / 1024 ** 3:.1f}'))
                            last_stg_ui[1] = int(prog)
                            last_stg_ui[2] = now_t

                    wc.copy_path_with_progress(orig_s, s_src, phys_sz, _check_cancel, _on_stg_prog)
                    orig_s = s_src
                    if orig_s.lower().endswith(".iso"):
                        nv_b = wc.inspect_bluray_iso_playlist_bytes(orig_s, mpls_n, min_valid_bytes=5 * 1024 * 1024, sevenzip_bin=SEVENZIP_CLI)
                        if nv_b >= 5 * 1024 * 1024:
                            src_sz = nv_b
                        _, v_p2, a_p2, _ = wc.probe_source_size_and_bitrates(orig_s, job_node, hb_bin, root_item=it, ffprobe_bin=FFPROBE_CLI, sevenzip_bin=SEVENZIP_CLI)
                        if v_p2 > 0:
                            v_in = v_p2
                        if a_p2 > 0:
                            a_in = a_p2
                    if is_disc and job_dur_s > 60.0 and src_sz > 100 * 1024 * 1024:
                        v_in = wc.reconcile_disc_video_bitrate(src_sz, job_dur_s, v_in, a_in)
                    with self.jobs_lock:
                        if job_id in self.active_jobs:
                            self.active_jobs[job_id]["src_size_bytes"] = src_sz
                            self.active_jobs[job_id]["v_bitrate_in_kbps"] = v_in
                            self.active_jobs[job_id]["a_bitrate_in_kbps"] = a_in

                if _check_cancel(False):
                    raise InterruptedError("Cancelled")

                with self.jobs_lock:
                    if job_id in self.active_jobs:
                        self.active_jobs[job_id]["progress"] = 0.0
                        self.active_jobs[job_id]["speed_mbps"] = 0.0
                        self.active_jobs[job_id]["transferred_bytes"] = 0
                        self.active_jobs[job_id]["step_message"] = ""

                enc_lbl = f"[{wc.T('w_encoding')}] {ui_m_lbl}"
                self.update_job_ui(job_id, enc_lbl, 0.0, "", wc.T('w_starting_hb'))
                try:
                    api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/encoding", timeout=5)
                except Exception:
                    pass

                lpcm_tr = wc.detect_lpcm_audio_tracks_in_source(
                    orig_s, job_node, ffprobe_bin=FFPROBE_CLI, root_item=it,
                    lpcm_passthru=(lpcm_passthru and w_dest.lower().endswith(".mkv")), dts_hd_mode=dts_hd_mode, is_partial_range=is_nonzero_start_job
                )
                skip_a = [x["hb_audio_idx"] for x in lpcm_tr]
                if lpcm_tr:
                    self.log(wc.T('w_id_detected_lpcm_pcm', job_id, len(lpcm_tr)))

                cores_tot = os.cpu_count() or 4
                hyb = self.hybrid_cpu or {}
                eff_pm = self.resolve_windows_power_mode()
                if eff_pm == "CUSTOM" and self.custom_cores:
                    eco_t_cmd = max(1, len(self.custom_cores))
                elif eff_pm == "HYBRID_ECO" and hyb.get("is_hybrid"):
                    eco_t_cmd = max(1, int(hyb.get("e_core_count") or (cores_tot - 2)))
                else:
                    eco_t_cmd = max(1, cores_tot - 2)
                cmd = wc.build_hb_cmd_from_windows_json(
                    hb_bin, job_node, orig_s, w_dest,
                    auto_extract_forced_subs, skip_a, root_item=it,
                    eco_mode=(eff_pm in ("ECO", "HYBRID_ECO", "CUSTOM")), eco_threads=eco_t_cmd,
                    burn_in_mode=burn_in_mode, dts_hd_mode=dts_hd_mode
                )
                self.log(wc.T('w_id_full_assembled_cli', job_id, HB_BINARY_SOURCE, ' '.join(cmd)))
                hb_p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True, encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
                )

                with self.jobs_lock:
                    if job_id not in self.active_jobs or self.active_jobs[job_id].get("cancelled"):
                        wc.kill_process_tree(hb_p, wait_s=2.0)
                        raise InterruptedError("Cancelled")
                    self.active_jobs[job_id]["process"] = hb_p
                    pause_guard_state = self.active_jobs[job_id].get("pause_guard_state") or {"suspended": False, "reason": "", "lock": threading.Lock()}
                    pause_guard_state["suspended"] = False
                    pause_guard_state["reason"] = ""
                    self.active_jobs[job_id]["pause_guard_state"] = pause_guard_state

                self.apply_current_power_state_now()

                p_re = re.compile(r"Encoding: task \d+ of \d+, (\d+\.\d+) % \((.*?)\)")
                l_chk, l_ui, l_log, l_pwr = 0.0, 0.0, 0.0, time.time()
                l_pct = -1
                hb_tail = []
                e_t0 = time.time()
                cur_prog = [0.0]
                write_dir_to_watch = os.path.dirname(os.path.abspath(w_dest))

                def _on_pause_state_cb(status_txt: str):
                    with self.jobs_lock:
                        if job_id in self.active_jobs:
                            self.active_jobs[job_id]["step_message"] = status_txt
                            self.active_jobs[job_id]["disk_paused"] = bool(status_txt)
                    if status_txt:
                        self.update_job_ui(job_id, f"[{wc.T('w_paused')}] {ui_m_lbl}", cur_prog[0], "PAUSED", status_txt)
                    else:
                        self.update_job_ui(job_id, enc_lbl, cur_prog[0], "", wc.T('w_resuming'))
                    self.update_pause_encode_btn_ui()
                    self.refresh_tray_and_window_icon()

                for ln in hb_p.stdout:
                    if _check_cancel(False):
                        try:
                            wc.kill_process_tree(hb_p, wait_s=2.0)
                        except Exception:
                            pass
                        break

                    cln = ln.strip()
                    if cln:
                        hb_tail.append(cln)
                        if len(hb_tail) > 15:
                            hb_tail.pop(0)

                    m = p_re.search(ln)
                    ct = time.time()
                    if ct - l_pwr >= 5.0:
                        self.apply_current_power_state_now()
                        l_pwr = ct

                    if m:
                        prog = float(m.group(1))
                        cur_prog[0] = prog
                        st_s = m.group(2)
                        fm = re.search(r"([\d\.]+)\s*fps(?:,\s*avg\s*([\d\.]+)\s*fps)?", st_s)
                        em = re.search(r"ETA ([\w\d]+)", st_s)

                        f_cur = float(fm.group(1)) if fm else 0.0
                        f_avg = float(fm.group(2)) if (fm and fm.group(2)) else f_cur
                        eta_r = em.group(1) if em else ""
                        eta_s = wc.parse_hb_eta_to_seconds(eta_r)
                        elap = ct - e_t0

                        with self.jobs_lock:
                            if job_id in self.active_jobs:
                                self.active_jobs[job_id]["progress"] = prog
                                self.active_jobs[job_id]["fps_current"] = f_cur
                                self.active_jobs[job_id]["fps_avg"] = f_avg
                                self.active_jobs[job_id]["speed_mbps"] = 0.0
                                self.active_jobs[job_id]["elapsed_sec"] = int(elap)
                                self.active_jobs[job_id]["eta_sec"] = eta_s
                                self.active_jobs[job_id]["transferred_bytes"] = 0
                                self.active_jobs[job_id]["is_net_fallback"] = bool(fb_net)
                                self.active_jobs[job_id]["net_fallback_reason"] = fb_reason
                        if ct - l_ui > 1.0:
                            self.update_job_ui(job_id, enc_lbl, prog, f"{f_cur:.1f} fps", eta_r, elapsed_sec=int(elap))
                            l_ui = ct

                        cur_int_pct = int(prog)
                        if (cur_int_pct > l_pct and ct - l_log >= 20.0) or (ct - l_log >= 300.0):
                            self.log(wc.T('w_job_progress_fps_avg', job_id, f'{prog:.1f}', f'{f_cur:.1f}', f'{f_avg:.1f}'))
                            l_log = ct
                            l_pct = cur_int_pct

                    if self.is_job_user_paused(job_id) or (ct - l_chk > 3.0):
                        if _check_cancel(True):
                            try:
                                wc.kill_process_tree(hb_p, wait_s=2.0)
                            except Exception:
                                pass
                            break
                        wc.check_live_pause_and_disk_guard(
                            hb_p, write_dir_to_watch, pause_guard_state,
                            _check_cancel, lambda: self.is_job_user_paused(job_id),
                            log_fn=self.log, status_cb=_on_pause_state_cb
                        )
                        l_chk = time.time()

                hb_p.wait()

                if _check_cancel(True):
                    raise InterruptedError("Cancelled")

                if hb_p.returncode != 0 and self.is_running:
                    hb_err_msg = f"HandBrake failed (code {hb_p.returncode}). Log: {' | '.join(hb_tail[-6:])}"
                    if pm_done and _pm_try == 0:
                        self.log(wc.T('w_id_premux_audit_fallback', job_id, hb_err_msg))
                        continue
                    raise RuntimeError(hb_err_msg)

                if not self.is_running:
                    break

                if not os.path.exists(w_dest):
                    if pm_done and _pm_try == 0:
                        self.log(wc.T('w_id_premux_audit_fallback', job_id, "Output video file was not created!"))
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

                if self.remote_win_binaries:
                    sync_and_resolve_binaries(
                        self.config["server_url"], allow_download=True,
                        allow_hb_update=False, win_manifest=self.remote_win_binaries,
                        remote_gen=self.remote_binary_gen, log_fn=self.log
                    )

                s_rem, s_stat, lpcm_c = 0, "OK", 0
                ff_p = FFMPEG_CLI or shutil.which("ffmpeg")
                fp_p = FFPROBE_CLI or shutil.which("ffprobe")
                is_mkv_output = w_dest.lower().endswith(".mkv")

                if is_mkv_output and ff_p and fp_p:
                    try:
                        do_cln = auto_clean_empty_subs or auto_extract_forced_subs
                        m_list, to_rem, n_ass = wc.analyze_subtitle_tracks_packets(ff_p, fp_p, w_dest, do_clean=do_cln, convert_ass=convert_ass)

                        if to_rem or n_ass or (lpcm_tr and os.path.exists(orig_s)):
                            self.log(wc.T('w_id_post_processing_removing', job_id, len(to_rem), len(lpcm_tr)))
                            t_mkv = wc.choose_safe_postproc_temp_mkv(w_dest, orig_d if not use_relay_mode else "")
                            f_cmd = wc.build_postproc_ffmpeg_cmd(ff_p, fp_p, w_dest, orig_s, t_mkv, lpcm_tr, m_list, to_rem, convert_ass)
                            def _reg_pp(p, _jid=job_id):
                                with self.jobs_lock:
                                    if _jid in self.active_jobs:
                                        self.active_jobs[_jid]["process"] = p

                            fp_rc = wc.run_tracked_subprocess(f_cmd, register_fn=_reg_pp, creationflags=CREATE_NO_WINDOW)

                            if fp_rc == 0 and os.path.exists(t_mkv):
                                if os.path.dirname(os.path.abspath(t_mkv)) == os.path.dirname(os.path.abspath(w_dest)):
                                    os.remove(w_dest)
                                    os.rename(t_mkv, w_dest)
                                else:
                                    old_w_dest = w_dest
                                    try:
                                        os.remove(w_dest)
                                    except Exception:
                                        pass
                                    w_dest = t_mkv
                                    wc.rekey_pending_audio_titles(old_w_dest, w_dest)
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

                if is_mkv_output:
                    wc.sync_mkv_audio_titles(w_dest, mkvpropedit_bin=MKVPROPEDIT_CLI)
                # Kontrola výstupních stop před nahráním na cílové úložiště
                audit_ok, audit_detail = wc.verify_output_stream_contract(
                    w_dest, job_node, root_item=it, ffprobe_bin=FFPROBE_CLI, auto_clean_empty_subs=auto_clean_empty_subs,
                    burn_in_respected=(burn_in_mode == "respect"), dts_hd_mode=dts_hd_mode, premux_used=pm_done
                )
                if not audit_ok:
                    encode_completed_ok = False
                    err_audit_msg = wc.T('w_stream_audit_failed_output', audit_detail)
                    self.log(f"ID {job_id}: {err_audit_msg}")
                    if pm_done and _pm_try == 0 and not _check_cancel(False):
                        self.log(wc.T('w_id_premux_audit_fallback', job_id, audit_detail))
                        continue
                    raise RuntimeError(err_audit_msg)
                elif audit_detail == wc.STREAM_AUDIT_SKIPPED:
                    self.log(wc.T('w_id_stream_audit_skipped', job_id))
                else:
                    self.log(wc.T('w_id_output_stream_contract', job_id, audit_detail))
                break

            if encode_completed_ok and self.is_running:
                shutil.rmtree(os.path.join(j_tmp, "SOURCE"), ignore_errors=True)

                # Zápis MKV statistik a měření výsledného bitratu (pouze pro .mkv)
                m_stats_ok = False
                if write_mkv_stats and w_dest.lower().endswith(".mkv"):
                    m_stats_ok = wc.write_mkv_statistics_tags(w_dest, mkvpropedit_bin=MKVPROPEDIT_CLI)
                    if m_stats_ok:
                        self.log(wc.T('w_id_mkv_track_statistics', job_id))

                v_out, a_out = wc.probe_media_bitrates(w_dest, ffprobe_bin=FFPROBE_CLI)
                o_sz = wc.get_file_size(w_dest)
                dur = time.time() - t0

                # Detekce neefektivní komprese (vyšší bitrate než zdroj)
                is_ineff = (v_in > 0 and v_in != 2500 and v_out >= v_in)
                f_dest_rep = None
                relay_sub = ""
                if is_ineff and ineffective_action and ineffective_action != "none":
                    f_fn = d_base
                    if ineffective_action in ("prefix", "both") and ineffective_prefix and not f_fn.startswith(ineffective_prefix):
                        f_fn = f"{ineffective_prefix}{f_fn}"
                    t_odir = st_p
                    if ineffective_action in ("subfolder", "both") and ineffective_subfolder:
                        relay_sub = ineffective_subfolder
                        t_odir = os.path.join(st_p, ineffective_subfolder)
                        if not use_relay_mode:
                            os.makedirs(t_odir, exist_ok=True)
                    d_base = f_fn
                    orig_d = os.path.normpath(os.path.join(t_odir, f_fn))
                    tmp_orig_d = orig_d + ".tmp"
                    f_dest_rep = orig_d
                    self.log(wc.T('w_id_output_bitrate_source', job_id, v_in, v_out, orig_d))

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

                # Koordinace volného místa na výstupu (Storage Guard) a přesun do Outputs
                if use_local_write:
                    slot_allowed = True
                    hold_reason = ""
                    try:
                        ru = api_request(
                            "POST", f"{self.config['server_url']}/api/jobs/{job_id}/request_upload",
                            json={"worker_name": self.config["worker_name"], "out_size_bytes": int(o_sz), "force": False},
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
                        self.log(wc.T('w_id_storage_guard_target_2', job_id, hold_reason))
                        wc.save_held_job_meta(j_tmp, meta_obj)
                        with self.held_lock:
                            self.held_jobs[job_id] = meta_obj
                        self.update_job_ui(job_id, f"[WAITING DISK] {ui_m_lbl}", 100.0, f"{o_sz/(1024**3):.1f} GB", wc.T('w_waiting_space_outputs'))
                        keep_temp_for_disk_hold = True
                        return

                    # Před zahájením kopírování uložíme held_job_meta.json jako pojistku proti pádu/restartu uprostřed přenosu
                    saved_meta_obj = meta_obj
                    wc.save_held_job_meta(j_tmp, meta_obj)

                    try:
                        api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/copying", timeout=5)
                    except Exception:
                        pass

                    sav_lbl = f"[{wc.T('w_saving')}] {ui_m_lbl}"
                    last_cpy_ui = [0.0, -1, 0.0]

                    def _on_cpy_prog(prog, spd, copied_b, elap_s, now_t):
                        with self.jobs_lock:
                            if job_id in self.active_jobs:
                                self.active_jobs[job_id]["progress"] = prog
                                self.active_jobs[job_id]["fps_current"] = 0.0
                                self.active_jobs[job_id]["fps_avg"] = 0.0
                                self.active_jobs[job_id]["speed_mbps"] = round(spd, 2)
                                self.active_jobs[job_id]["transferred_bytes"] = copied_b
                                self.active_jobs[job_id]["src_size_bytes"] = o_sz
                                self.active_jobs[job_id]["elapsed_sec"] = elap_s
                        if now_t - last_cpy_ui[0] > 0.6:
                            self.update_job_ui(job_id, sav_lbl, prog, f"{spd:.1f} MB/s", f"{copied_b/(1024**3):.1f} / {o_sz/(1024**3):.1f} GB")
                            last_cpy_ui[0] = now_t
                        if int(prog) >= last_cpy_ui[1] + 5 and now_t - last_cpy_ui[2] > 5.0:
                            self.log(wc.T('w_copying_outputs_mb_s', f'{prog:.1f}', f'{spd:.1f}'))
                            last_cpy_ui[1] = int(prog)
                            last_cpy_ui[2] = now_t

                    try:
                        if use_relay_mode:
                            self.log(wc.T('w_id_uploading_finished_video', job_id))
                            rel_res = wc.relay_upload_output_to_manager(
                                self.config["server_url"].rstrip("/"), job_id, self.config["worker_name"],
                                w_dest, d_base, relay_sub, _check_cancel, _on_cpy_prog
                            )
                            if rel_res.get("final_dest_path"):
                                complete_pl["final_dest_path"] = rel_res["final_dest_path"]
                            self.log(wc.T('w_id_http_relay_upload', job_id))
                        else:
                            if orig_d.startswith("\\\\") and not os.path.isdir(os.path.dirname(orig_d)):
                                self.ensure_smb_shares_connected()
                            self.log(wc.T('w_id_encoding_finished_moving', job_id))
                            wc.copy_path_with_progress(w_dest, tmp_orig_d, o_sz, _check_cancel, _on_cpy_prog)
                            final_d = wc.place_output_file(tmp_orig_d, orig_d, job_id)
                            if final_d != orig_d:
                                self.log(wc.T('w_out_saved_new_name', job_id, os.path.basename(orig_d), os.path.basename(final_d)))
                                complete_pl["final_dest_path"] = final_d
                            meta_obj["final_d"] = final_d          # pád před potvrzením → held smyčka soubor znovu nekopíruje
                            wc.save_held_job_meta(j_tmp, meta_obj)
                            self.log(wc.T('w_id_finished_saved_as', job_id))
                    except OSError as ose:
                        try:
                            if os.path.exists(tmp_orig_d):
                                os.remove(tmp_orig_d)
                        except Exception:
                            pass
                        win_err = getattr(ose, "winerror", 0)
                        if ose.errno == errno.ENOSPC or win_err == 112 or "space" in str(ose).lower():
                            h_msg = wc.T('w_out_disk_space_while', ose)
                            self.log(f"ID {job_id} [Storage Guard]: {h_msg}")
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
                            with self.held_lock:
                                self.held_jobs[job_id] = meta_obj
                            self.update_job_ui(job_id, f"[WAITING DISK] {ui_m_lbl}", 100.0, f"{o_sz/(1024**3):.1f} GB", wc.T('w_waiting_space_outputs'))
                            try:
                                api_request(
                                    "POST", f"{self.config['server_url']}/api/jobs/{job_id}/waiting_disk",
                                    json={
                                        "worker_name": self.config["worker_name"], "out_size_bytes": int(o_sz),
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
                    if orig_d.startswith("\\\\") and not os.path.isdir(os.path.dirname(orig_d)):
                        self.ensure_smb_shares_connected()
                    os.makedirs(os.path.dirname(orig_d), exist_ok=True)
                    if os.path.abspath(w_dest) != os.path.abspath(orig_d):
                        final_d = wc.place_output_file(w_dest, orig_d, job_id)
                        if final_d != orig_d:
                            self.log(wc.T('w_out_saved_new_name', job_id, os.path.basename(orig_d), os.path.basename(final_d)))
                            complete_pl["final_dest_path"] = final_d
                    self.log(wc.T('w_id_finished_renamed_clean', job_id))

                # Potvrzení dokončení úlohy Manageru
                while self.is_running:
                    try:
                        r = api_request(
                            "POST", f"{self.config['server_url']}/api/jobs/{job_id}/complete",
                            json=complete_pl,
                            timeout=5
                        )
                        if r.status_code == 200:
                            break
                        if wc.is_own_manager_refusal(r):
                            self.log(f"ID {job_id}: Manager rejected /complete (HTTP {r.status_code}) - stopping retry.")
                            break
                        time.sleep(5)
                    except Exception:
                        time.sleep(5)

        except InterruptedError:
            self.log(wc.T('w_job_cleanly_cancelled_temp', job_id))
        except Exception as e:
            if not self.is_job_cancelled(job_id):
                # Záchrana hotového videa při chybě uložení (mimo selhání auditu stop)
                if encode_completed_ok and w_dest and os.path.isfile(w_dest) and os.path.getsize(w_dest) > 256 * 1024 and "[AUDIT STOP" not in str(e) and "[STREAM AUDIT" not in str(e):
                    o_sz = os.path.getsize(w_dest)
                    h_msg = wc.T('w_encoding_finished_but_post', e, f'{o_sz / 1024 ** 3:.1f}')
                    self.log(f"ID {job_id} [RESCUE]: {h_msg}")
                    if isinstance(saved_meta_obj, dict) and saved_meta_obj.get("complete_payload"):
                        meta_obj = dict(saved_meta_obj)
                        meta_obj["out_size_bytes"] = int(o_sz)
                    else:
                        meta_obj = {
                            "job_id": job_id,
                            "job_temp_dir": j_tmp,
                            "w_dest": w_dest,
                            "orig_d": orig_d or os.path.join(self.get_outputs_dir() or j_tmp, os.path.basename(w_dest)),
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
                    with self.held_lock:
                        self.held_jobs[job_id] = meta_obj
                    self.update_job_ui(job_id, f"[WAITING DISK] {os.path.basename(w_dest)}", 100.0, f"{o_sz/(1024**3):.1f} GB", wc.T('w_waiting_space_outputs'))
                    try:
                        api_request(
                            "POST", f"{self.config['server_url']}/api/jobs/{job_id}/waiting_disk",
                            json={"worker_name": self.config["worker_name"], "out_size_bytes": int(o_sz), "src_size_bytes": int(src_sz), "reason": h_msg},
                            timeout=5
                        )
                    except Exception:
                        pass
                    keep_temp_for_disk_hold = True
                else:
                    self.log(wc.T('w_job_error', job_id, traceback.format_exc()))
                    try:
                        api_request("POST", f"{self.config['server_url']}/api/jobs/{job_id}/error", json={"error_type": "GENERIC", "error_code": "ERR_HB_FAILED", "error_detail": str(e)[-250:], "version": CURRENT_VERSION}, timeout=5)
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
                    for _lo in (tmp_orig_d, (orig_d + ".clean.tmp.mkv") if orig_d else "", (tmp_orig_d + ".clean.tmp.mkv") if tmp_orig_d else ""):
                        try:
                            if _lo and os.path.exists(_lo):
                                os.remove(_lo)
                        except Exception:
                            pass
                    self.remove_job_ui(job_id)
            finally:
                with self.jobs_lock:
                    self.active_jobs.pop(job_id, None)
                self.update_pause_encode_btn_ui()
                self.refresh_tray_and_window_icon()
                self.force_update_check = True


def run_ota_selftest(stage_dir: str, target_ver: str):
    """O1: novou verzi z app/ota_stage zkusmo načte v samostatném procesu (worker.pyw --selftest).
    Vrací (ok, detail). Projde jen s kódem 0 a výsledkem selftest_result.json pro cílovou verzi."""
    py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.isfile(py):
        py = sys.executable
    res_p = os.path.join(stage_dir, "selftest_result.json")
    try:
        cp = subprocess.run([py, os.path.join(stage_dir, "worker.pyw"), "--selftest"], cwd=stage_dir, timeout=120,
                            creationflags=CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return False, "timeout 120 s"
    except Exception as e:
        return False, str(e)[:300]
    try:
        with open(res_p, "r", encoding="utf-8") as f:
            res = json.load(f)
    except Exception:
        res = {}
    if cp.returncode == 0 and res.get("ok") and str(res.get("version") or "") == str(target_ver):
        return True, ""
    tail = [ln for ln in (cp.stdout or b"").decode("utf-8", errors="replace").splitlines() if ln.strip()][-4:]
    return False, (str(res.get("error") or "") or " | ".join(tail) or f"exit code {cp.returncode}")[:400]


def run_selftest() -> int:
    """--selftest: celý modul je načtený z app/ota_stage (importy, sdílené jádro); ověří se jádro, překlady,
    okno Tk a to, že se tato verze umí dál aktualizovat. Výsledek do selftest_result.json. Nic se nespouští."""
    problems = []
    try:
        if str(getattr(wc, "CURRENT_VERSION", "")) != CURRENT_VERSION:
            problems.append(f"worker_core {getattr(wc, 'CURRENT_VERSION', '?')} != {CURRENT_VERSION}")
        for fn in ("stage_windows_update", "build_windows_update_bat", "evaluate_ota_attempt", "update_retry_note",
                   "update_retry_view", "build_hb_cmd_from_windows_json", "init_worker_identity", "init_manager_binding",
                   "win_install_root", "win_cmd_worker_target", "win_ref_is_ours_or_stale", "win_same_dir",
                   "win_path_inside", "win_path_inside_garbled", "decode_console_output", "default_ui_lang"):
            if not callable(getattr(wc, fn, None)):
                problems.append(f"worker_core.{fn} missing")
        for m in ("worker_loop", "heartbeat_loop", "collect_diagnostics", "request_update_now", "_ota_after_start"):
            if not callable(getattr(HandBrakeWorkerApp, m, None)):
                problems.append(f"HandBrakeWorkerApp.{m} missing")
        with open(os.path.join(_this_dir, "locales.js"), "r", encoding="utf-8-sig") as f:
            if not wc.parse_locales_js_text(f.read()):
                problems.append("locales.js empty")
        _r = tk.Tk()
        _r.withdraw()
        _r.update_idletasks()
        _r.destroy()
    except Exception as e:
        problems.append(f"{type(e).__name__}: {e}")
    res = {"ok": not problems, "version": CURRENT_VERSION, "error": "; ".join(problems)[:1500]}
    try:
        with open(os.path.join(_this_dir, "selftest_result.json"), "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        return 1
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    if SELFTEST:
        os._exit(run_selftest())
    _cli_restore_state = ""
    _is_bg_boot = False
    _is_desktop_takeover = False
    _is_autostart_login = False
    for _arg in sys.argv[1:]:
        _a = str(_arg or "").strip()
        if _a.startswith("--restore-state="):
            _cli_restore_state = _a.split("=", 1)[1].strip().lower()
        elif _a == "--bg-boot":
            _is_bg_boot = True
        elif _a == "--desktop-takeover":
            _is_desktop_takeover = True
        elif _a == "--autostart-login":
            _is_autostart_login = True

    _my_sess = 0 if _is_bg_boot else get_current_windows_session_id()
    _wait_session0_handover = False
    _SINGLE_INSTANCE_MUTEX, _already_exists = acquire_worker_single_instance_mutex()
    if _already_exists:
        # The running worker reads its commands from ITS app folder – with a second copy of the worker in another
        # folder (registry AppDir), the command has to go there, not into this copy's folder.
        _ipc_dir = _running_worker_app_dir(APP_ROOT_PATH)
        _other_copy = _ipc_dir != APP_ROOT_PATH
        _ipc_state = os.path.join(_ipc_dir, os.path.basename(IPC_STATE_FILE))
        _ipc_cmd = os.path.join(_ipc_dir, os.path.basename(IPC_CMD_FILE))
        _running_sess = 1
        _running_bg = False
        _running_pid = 0
        try:
            if os.path.isfile(_ipc_state):
                with open(_ipc_state, "r", encoding="utf-8") as _rf:
                    _st = json.load(_rf) or {}
                _running_sess = int(_st.get("session_id", 1))
                _running_bg = bool(_st.get("bg_boot", False))
                _running_pid = int(_st.get("pid") or 0)
        except Exception:
            pass
        if not _other_copy and (_running_sess == 0 or _running_bg) and _my_sess >= 1 and (_running_pid == 0 or _is_windows_pid_alive(_running_pid)):
            try:
                with open(_ipc_cmd, "w", encoding="utf-8") as _wf:
                    json.dump({"cmd": "DESKTOP_TAKEOVER", "requester_pid": os.getpid(), "ts": time.time()}, _wf)
            except Exception:
                pass
            _wait_session0_handover = True
        else:
            if not _is_bg_boot and not _is_desktop_takeover and not _is_autostart_login:
                try:
                    with open(_ipc_cmd, "w", encoding="utf-8") as _wf:
                        json.dump({"cmd": "SHOW_WINDOW", "ts": time.time()}, _wf)
                except Exception:
                    pass
                if _other_copy:
                    _show_other_copy_running(wc.win_install_root(_ipc_dir) if wc else _ipc_dir)
            sys.exit(0)
    elif _my_sess >= 1 and os.path.isfile(IPC_STATE_FILE):
        # Záloha pro případ, že Session 0 a Session 1 nesdílely Global mutex: ověřit živý PID ze souboru
        try:
            with open(IPC_STATE_FILE, "r", encoding="utf-8") as _rf:
                _st = json.load(_rf) or {}
            _st_pid = int(_st.get("pid") or 0)
            if (_st.get("bg_boot") or int(_st.get("session_id", 1)) == 0) and _st_pid != os.getpid() and _is_windows_pid_alive(_st_pid):
                with open(IPC_CMD_FILE, "w", encoding="utf-8") as _wf:
                    json.dump({"cmd": "DESKTOP_TAKEOVER", "requester_pid": os.getpid(), "ts": time.time()}, _wf)
                _wait_session0_handover = True
        except Exception:
            pass

    _hbw_register_install(APP_ROOT_PATH)     # this copy runs → it is the installation other copies refer to
    root = tk.Tk()
    root.withdraw()
    # Kill-on-close Job Object: HandBrakeCLI / ffmpeg / mkvmerge / 7z se ukončí i při
    # pádu nebo násilném ukončení workeru (žádné osiřelé procesy držící soubory v Temp).
    wc.install_windows_kill_on_close_job()
    app = HandBrakeWorkerApp(
        root,
        cli_restore_state=_cli_restore_state,
        is_bg_boot=_is_bg_boot,
        is_desktop_takeover=_is_desktop_takeover,
        is_autostart_login=_is_autostart_login,
        wait_session0_handover=_wait_session0_handover,
    )
    root.mainloop()