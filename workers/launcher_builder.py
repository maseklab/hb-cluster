import os
import re
import sys
import shutil
import struct
import tempfile
import subprocess

def _read_version_file() -> str:
    """Verze se čte výhradně ze souboru VERSION (vedle skriptu, o úroveň výš nebo v aktuální složce)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "VERSION"), os.path.join(os.path.dirname(here), "VERSION"), os.path.join(os.getcwd(), "VERSION")):
        try:
            if os.path.isfile(cand):
                with open(cand, "r", encoding="utf-8-sig") as f:
                    v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return "0.0.0-dev.0"


def version_to_file_tuple(ver: str) -> tuple:
    """
    Převod semver na 4 čísla Windows FILEVERSION (každé 0..65535):
      2.12.0-dev.8 -> 2,12,0,8 | -alpha.N -> 200+N | -beta.N -> 400+N | -rc.N -> 600+N | 2.12.0 -> 2,12,0,1000
    """
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?", str(ver or ""))
    if not m:
        return (0, 0, 0, 0)
    major, minor, patch = (min(int(x), 65535) for x in m.group(1, 2, 3))
    pre = m.group(4) or ""
    if not pre:
        build = 1000
    else:
        mp = re.match(r"^(dev|alpha|beta|rc)[.\-]?(\d+)", pre, re.IGNORECASE)
        base = {"dev": 0, "alpha": 200, "beta": 400, "rc": 600}
        build = (base[mp.group(1).lower()] + min(int(mp.group(2)), 199)) if mp else 0
    return (major, minor, patch, build)


VERSION = _read_version_file()
_FILE_VER = version_to_file_tuple(VERSION)
LAUNCHER_FILE_VERSION_COMMA = ",".join(str(x) for x in _FILE_VER)
LAUNCHER_FILE_VERSION_DOT = ".".join(str(x) for x in _FILE_VER)

RESOURCE_RC_TEMPLATE = r"""#include <windows.h>

1 ICON "__ICON_RES__"
1 24 "manifest.xml"

VS_VERSION_INFO VERSIONINFO
FILEVERSION     __FILEVER_COMMA__
PRODUCTVERSION  __FILEVER_COMMA__
FILEFLAGSMASK   VS_FFI_FILEFLAGSMASK
FILEFLAGS       0x0L
FILEOS          VOS_NT_WINDOWS32
FILETYPE        VFT_APP
FILESUBTYPE     VFT2_UNKNOWN
BEGIN
    BLOCK "StringFileInfo"
    BEGIN
        BLOCK "040904B0"
        BEGIN
            VALUE "CompanyName",      "MasekLab\0"
            VALUE "FileDescription",  "HB Worker - HandBrake Cluster Worker\0"
            VALUE "FileVersion",      "__FILEVER_DOT__\0"
            VALUE "InternalName",     "HBWorker.exe\0"
            VALUE "LegalCopyright",   "Copyright (C) 2026 Petr Masek (MasekLab)\0"
            VALUE "OriginalFilename", "HBWorker.exe\0"
            VALUE "ProductName",      "HandBrake Distributed Cluster Worker\0"
            VALUE "ProductVersion",   "__PRODVER__\0"
        END
    END
    BLOCK "VarFileInfo"
    BEGIN
        VALUE "Translation", 0x409, 1200
    END
END
"""

MANIFEST_XML_TEMPLATE = r"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="__FILEVER_DOT__" processorArchitecture="*" name="HandBrakeCluster.Worker" type="win32"/>
  <description>HB Worker Launcher</description>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">system</dpiAwareness>
      <gdiScaling xmlns="http://schemas.microsoft.com/SMI/2017/WindowsSettings">true</gdiScaling>
    </windowsSettings>
  </application>
</assembly>
"""

LAUNCHER_C_SRC = r"""#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <wchar.h>

#define HB_PBUF 1024

static int file_exists(const wchar_t *p) {
    if (!p || !p[0]) return 0;
    DWORD a = GetFileAttributesW(p);
    return (a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY));
}

static int is_windows_apps_stub(const wchar_t *p) {
    if (!p || !p[0]) return 1;
    wchar_t low[HB_PBUF];
    wcsncpy(low, p, HB_PBUF - 1);
    low[HB_PBUF - 1] = 0;
    _wcslwr(low);
    return (wcsstr(low, L"windowsapps") != NULL) ? 1 : 0;
}

static void strip_zone_id(const wchar_t *path) {
    if (!path || !path[0]) return;
    wchar_t ads[HB_PBUF + 64];
    _snwprintf(ads, (sizeof(ads) / sizeof(ads[0])) - 1, L"%s:Zone.Identifier", path);
    ads[(sizeof(ads) / sizeof(ads[0])) - 1] = 0;
    DeleteFileW(ads);
}

static int run_wait(wchar_t *cmd, const wchar_t *cwd, DWORD flags) {
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, flags, NULL, cwd, &si, &pi)) {
        return -1;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}

static void safe_wcopy(wchar_t *dst, size_t cap, const wchar_t *src) {
    if (!dst || cap == 0) return;
    if (!src) { dst[0] = 0; return; }
    _snwprintf(dst, cap - 1, L"%s", src);
    dst[cap - 1] = 0;
}

static int verify_python_candidate(const wchar_t *py_exe, const wchar_t *cwd, wchar_t *out_pyw, size_t out_cap) {
    if (!py_exe || !py_exe[0] || is_windows_apps_stub(py_exe) || !file_exists(py_exe)) {
        return 0;
    }
    wchar_t chk[HB_PBUF + 256];
    _snwprintf(chk, (sizeof(chk) / sizeof(chk[0])) - 1, L"\"%s\" -c \"import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)\"", py_exe);
    chk[(sizeof(chk) / sizeof(chk[0])) - 1] = 0;
    if (run_wait(chk, cwd, CREATE_NO_WINDOW) != 0) {
        return 0;
    }
    safe_wcopy(out_pyw, out_cap, py_exe);
    wchar_t *slash = wcsrchr(out_pyw, L'\\');
    if (slash) {
        size_t prefix_len = (size_t)(slash - out_pyw) + 1;
        if (prefix_len < out_cap) {
            safe_wcopy(slash + 1, out_cap - prefix_len, L"pythonw.exe");
            if (!file_exists(out_pyw)) {
                safe_wcopy(out_pyw, out_cap, py_exe);
            }
        }
    }
    return 1;
}

static int read_python_path_from_config(const wchar_t *dir, wchar_t *out_cand, size_t out_cap) {
    wchar_t cfg_p[HB_PBUF];
    _snwprintf(cfg_p, HB_PBUF - 1, L"%s\\config.json", dir);
    cfg_p[HB_PBUF - 1] = 0;
    FILE *f = _wfopen(cfg_p, L"rb");
    if (!f) return 0;
    char raw[8192] = {0};
    size_t n = fread(raw, 1, sizeof(raw) - 1, f);
    fclose(f);
    if (n == 0) return 0;
    const char *k = strstr(raw, "\"python_path\"");
    if (!k) k = strstr(raw, "\"python_exe\"");
    if (!k) return 0;
    const char *colon = strchr(k + 12, ':');
    if (!colon) return 0;
    const char *q1 = strchr(colon + 1, '"');
    if (!q1) return 0;
    q1++;
    char val[HB_PBUF * 2] = {0};
    int vi = 0;
    while (*q1 && *q1 != '"' && vi < (int)sizeof(val) - 2) {
        if (*q1 == '\\' && *(q1 + 1)) {
            q1++;
        }
        val[vi++] = *q1++;
    }
    val[vi] = 0;
    if (vi < 3) return 0;
    int wlen = MultiByteToWideChar(CP_UTF8, 0, val, -1, out_cand, (int)out_cap);
    if (wlen <= 0) {
        out_cand[0] = 0;
        return 0;
    }
    out_cand[out_cap - 1] = 0;
    return out_cand[0] ? 1 : 0;
}

static int find_python(const wchar_t *script_dir, const wchar_t *exe_dir, wchar_t *out_pyw, size_t out_cap) {
    wchar_t cand[HB_PBUF] = {0};
    if (read_python_path_from_config(script_dir, cand, HB_PBUF) || read_python_path_from_config(exe_dir, cand, HB_PBUF)) {
        if (verify_python_candidate(cand, script_dir, out_pyw, out_cap)) {
            return 1;
        }
    }
    if (SearchPathW(NULL, L"python.exe", NULL, HB_PBUF, cand, NULL)) {
        if (verify_python_candidate(cand, script_dir, out_pyw, out_cap)) {
            return 1;
        }
    }
    wchar_t local_app[HB_PBUF] = {0};
    if (GetEnvironmentVariableW(L"LOCALAPPDATA", local_app, HB_PBUF) > 0) {
        for (int v = 14; v >= 10; v--) {
            _snwprintf(cand, HB_PBUF - 1, L"%s\\Programs\\Python\\Python3%d\\python.exe", local_app, v);
            cand[HB_PBUF - 1] = 0;
            if (verify_python_candidate(cand, script_dir, out_pyw, out_cap)) return 1;
        }
    }
    wchar_t prog_files[HB_PBUF] = {0};
    if (GetEnvironmentVariableW(L"ProgramFiles", prog_files, HB_PBUF) > 0) {
        for (int v = 14; v >= 10; v--) {
            _snwprintf(cand, HB_PBUF - 1, L"%s\\Python3%d\\python.exe", prog_files, v);
            cand[HB_PBUF - 1] = 0;
            if (verify_python_candidate(cand, script_dir, out_pyw, out_cap)) return 1;
            _snwprintf(cand, HB_PBUF - 1, L"%s\\Python\\Python3%d\\python.exe", prog_files, v);
            cand[HB_PBUF - 1] = 0;
            if (verify_python_candidate(cand, script_dir, out_pyw, out_cap)) return 1;
        }
    }
    for (int v = 14; v >= 10; v--) {
        _snwprintf(cand, HB_PBUF - 1, L"C:\\Python3%d\\python.exe", v);
        cand[HB_PBUF - 1] = 0;
        if (verify_python_candidate(cand, script_dir, out_pyw, out_cap)) return 1;
    }
    return 0;
}

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, LPWSTR lpCmdLine, int nShowCmd) {
    (void)hInst; (void)hPrev; (void)nShowCmd;

    wchar_t exe_path[HB_PBUF] = {0};
    DWORD n_exe = GetModuleFileNameW(NULL, exe_path, HB_PBUF - 1);
    if (n_exe == 0 || n_exe >= HB_PBUF - 1) return 1;
    exe_path[HB_PBUF - 1] = 0;
    wchar_t exe_dir[HB_PBUF] = {0};
    safe_wcopy(exe_dir, HB_PBUF, exe_path);
    wchar_t *last_slash = wcsrchr(exe_dir, L'\\');
    if (last_slash) *last_slash = 0;

    /* 1. Zjistit složku skriptu: 'app\' pokud existuje app\worker.pyw, jinak '.' */
    wchar_t script_dir[HB_PBUF] = {0};
    wchar_t test_app_pyw[HB_PBUF] = {0};
    _snwprintf(test_app_pyw, HB_PBUF - 1, L"%s\\app\\worker.pyw", exe_dir);
    test_app_pyw[HB_PBUF - 1] = 0;
    if (file_exists(test_app_pyw)) {
        _snwprintf(script_dir, HB_PBUF - 1, L"%s\\app", exe_dir);
        script_dir[HB_PBUF - 1] = 0;
    } else {
        safe_wcopy(script_dir, HB_PBUF, exe_dir);
    }

    SetCurrentDirectoryW(script_dir);
    SetCurrentProcessExplicitAppUserModelID(L"HandBrakeCluster.Worker");

    /* Odstranit Zone.Identifier (Mark of the Web) */
    const wchar_t *files[] = {
        L"HBWorker.exe", L"README.txt", L"Start_Worker.bat",
        L"worker.pyw", L"worker_core.py", L"locales.js", L"hbw.ico", L"config.json", NULL
    };
    for (int i = 0; files[i] != NULL; i++) {
        wchar_t fp1[HB_PBUF];
        _snwprintf(fp1, HB_PBUF - 1, L"%s\\%s", exe_dir, files[i]);
        fp1[HB_PBUF - 1] = 0;
        strip_zone_id(fp1);
        wchar_t fp2[HB_PBUF];
        _snwprintf(fp2, HB_PBUF - 1, L"%s\\%s", script_dir, files[i]);
        fp2[HB_PBUF - 1] = 0;
        strip_zone_id(fp2);
    }

    /* 2. Najít pythonw.exe */
    wchar_t found_pyw[HB_PBUF] = {0};
    if (!find_python(script_dir, exe_dir, found_pyw, HB_PBUF)) {
        /* Pokud Python v systému vůbec není, spustit Start_Worker.bat --install-python */
        wchar_t bat_p[HB_PBUF] = {0};
        _snwprintf(bat_p, HB_PBUF - 1, L"%s\\Start_Worker.bat", script_dir);
        bat_p[HB_PBUF - 1] = 0;
        if (!file_exists(bat_p)) {
            _snwprintf(bat_p, HB_PBUF - 1, L"%s\\Start_Worker.bat", exe_dir);
            bat_p[HB_PBUF - 1] = 0;
        }
        if (file_exists(bat_p)) {
            wchar_t inst_cmd[HB_PBUF + 64];
            _snwprintf(inst_cmd, (sizeof(inst_cmd) / sizeof(inst_cmd[0])) - 1, L"cmd.exe /c \"\"%s\" --install-python\"", bat_p);
            inst_cmd[(sizeof(inst_cmd) / sizeof(inst_cmd[0])) - 1] = 0;
            run_wait(inst_cmd, script_dir, 0);
        }
        if (!find_python(script_dir, exe_dir, found_pyw, HB_PBUF)) {
            return 1;
        }
    }

    /* 3. Spustit "pythonw.exe" "worker.pyw" s pracovním adresářem ve složce skriptu (předat i parametry příkazové řádky) */
    SetEnvironmentVariableW(L"HB_HOSTED_BY_EXE", L"1");
    const wchar_t *extra_args = lpCmdLine;
    while (extra_args && (*extra_args == L' ' || *extra_args == L'\t')) extra_args++;
    wchar_t cmd[MAX_PATH * 4];
    if (extra_args && extra_args[0]) {
        _snwprintf(cmd, (sizeof(cmd) / sizeof(cmd[0])) - 1, L"\"%s\" \"%s\\worker.pyw\" %s", found_pyw, script_dir, extra_args);
    } else {
        _snwprintf(cmd, (sizeof(cmd) / sizeof(cmd[0])) - 1, L"\"%s\" \"%s\\worker.pyw\"", found_pyw, script_dir);
    }
    cmd[(sizeof(cmd) / sizeof(cmd[0])) - 1] = 0;

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, script_dir, &si, &pi)) {
        return 1;
    }

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exit_code = 0;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)exit_code;
}
"""


def _write_fallback_ico(ico_path: str) -> None:
    """Generates a fallback 64x64 ICO if static/hbw.ico is not provided."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (15, 23, 42, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([(2, 2), (61, 61)], radius=12, fill=(15, 23, 42, 255), outline=(56, 189, 248, 255), width=3)
        draw.text((10, 22), "HBW", fill=(255, 255, 255, 255))
        img.save(ico_path, format="ICO", sizes=[(64, 64), (32, 32), (16, 16)])
        return
    except Exception:
        pass
    bmp_hdr = struct.pack("<IIIHHIIIIII", 40, 1, 2, 1, 32, 0, 8, 0, 0, 0, 0)
    bmp_data = bmp_hdr + b"\x2a\x17\x0f\xff" + b"\x00\x00\x00\x00"
    ico_dir = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 1, 1, 0, 0, 1, 32, len(bmp_data), 22)
    with open(ico_path, "wb") as f:
        f.write(ico_dir + bmp_data)


def compile_hbworker_exe(
    out_exe_path: str,
    icon_src_path: str = "",
    build_dir: str = "",
    product_version: str = VERSION,
    log_fn=None,
    sign_fn=None,
) -> bool:
    """Compiles Windows HBWorker.exe via mingw-w64 with High-DPI manifest and optionally signs it."""
    if not shutil.which("x86_64-w64-mingw32-gcc") or not shutil.which("x86_64-w64-mingw32-windres"):
        if log_fn:
            log_fn("[LAUNCHER-BUILD] mingw-w64 toolchain missing - skipping HBWorker.exe compilation.")
        return False

    own_tmp = False
    b_dir = build_dir
    if not b_dir:
        b_dir = tempfile.mkdtemp(prefix="hbworker_build_")
        own_tmp = True

    try:
        os.makedirs(b_dir, exist_ok=True)
        ico_local = os.path.join(b_dir, "hbw.ico")
        if icon_src_path and os.path.isfile(icon_src_path) and os.path.getsize(icon_src_path) > 500:
            shutil.copy2(icon_src_path, ico_local)
        else:
            _write_fallback_ico(ico_local)

        # High-DPI Manifest XML
        manifest_path = os.path.join(b_dir, "manifest.xml")
        manifest_text = MANIFEST_XML_TEMPLATE.replace("__FILEVER_DOT__", LAUNCHER_FILE_VERSION_DOT)
        with open(manifest_path, "w", encoding="utf-8") as f_man:
            f_man.write(manifest_text)

        rc_path = os.path.join(b_dir, "resource.rc")
        rc_text = (
            RESOURCE_RC_TEMPLATE
            .replace("__ICON_RES__", "hbw.ico")
            .replace("__FILEVER_COMMA__", LAUNCHER_FILE_VERSION_COMMA)
            .replace("__FILEVER_DOT__", LAUNCHER_FILE_VERSION_DOT)
            .replace("__PRODVER__", str(product_version or VERSION))
        )
        with open(rc_path, "w", encoding="utf-8") as f_rc:
            f_rc.write(rc_text)

        c_path = os.path.join(b_dir, "launcher.c")
        with open(c_path, "w", encoding="utf-8") as f_c:
            f_c.write(LAUNCHER_C_SRC)

        res_o = os.path.join(b_dir, "resource.o")
        raw_exe = os.path.join(b_dir, "HBWorker_raw.exe")
        r1 = subprocess.run(
            ["x86_64-w64-mingw32-windres", rc_path, "-O", "coff", "-o", res_o],
            cwd=b_dir, capture_output=True, text=True, timeout=60
        )
        if r1.returncode != 0 or not os.path.isfile(res_o):
            if log_fn:
                log_fn(f"[LAUNCHER-BUILD] windres failed: {(r1.stderr or r1.stdout)[-250:]}")
            return False

        r2 = subprocess.run(
            [
                "x86_64-w64-mingw32-gcc", c_path, res_o, "-o", raw_exe,
                "-mwindows", "-municode", "-O2", "-s", "-lshell32", "-lole32"
            ],
            cwd=b_dir, capture_output=True, text=True, timeout=60
        )
        if r2.returncode != 0 or not os.path.isfile(raw_exe) or os.path.getsize(raw_exe) <= 5000:
            if log_fn:
                log_fn(f"[LAUNCHER-BUILD] gcc failed: {(r2.stderr or r2.stdout)[-250:]}")
            return False

        final_src = raw_exe
        if callable(sign_fn):
            signed_exe = os.path.join(b_dir, "HBWorker_signed.exe")
            try:
                if sign_fn(raw_exe, signed_exe) and os.path.isfile(signed_exe) and os.path.getsize(signed_exe) > 5000:
                    final_src = signed_exe
            except Exception as exc:
                if log_fn:
                    log_fn(f"[LAUNCHER-BUILD] signing fallback to unsigned: {exc}")

        out_parent = os.path.dirname(os.path.abspath(out_exe_path))
        os.makedirs(out_parent, exist_ok=True)
        tmp_dst = out_exe_path + ".tmp"
        shutil.copy2(final_src, tmp_dst)
        os.chmod(tmp_dst, 0o755)
        os.replace(tmp_dst, out_exe_path)
        return os.path.isfile(out_exe_path) and os.path.getsize(out_exe_path) > 5000
    finally:
        if own_tmp:
            shutil.rmtree(b_dir, ignore_errors=True)


def build_windows_launcher_exe(out_exe_path: str, icon_src_path: str = "", version_str: str = VERSION) -> bool:
    return compile_hbworker_exe(out_exe_path, icon_src_path=icon_src_path, product_version=version_str)


if __name__ == "__main__":
    out_p = sys.argv[1] if len(sys.argv) > 1 else "/app/prebuilt/HBWorker.exe"
    ico_p = sys.argv[2] if len(sys.argv) > 2 else "/app/static/hbw.ico"
    ok = compile_hbworker_exe(out_p, icon_src_path=ico_p, log_fn=print)
    if not ok:
        sys.exit(1)
    print(f"[LAUNCHER-BUILD] Built {out_p} ({os.path.getsize(out_p)} bytes)")
