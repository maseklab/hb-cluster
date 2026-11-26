#!/usr/bin/env python3
"""Regenerates the images used in README.md / README.cs.md (docs/screenshots).

    python scripts/readme_images.py diagram      # how-it-works.png + how-it-works.cs.png
    python scripts/readme_images.py screens      # queue / workers / monitoring / mobile .png + demo.gif

``screens`` starts a demo Manager (a copy of this folder in a temp folder, its own data, no network) with four
simulated workers, waits 5 minutes so the CPU / RAM history fills up, and takes the pictures. It needs:
  * the Manager requirements (``pip install -r requirements.txt``) in the Python given by ``--python``
  * the built web assets in static/vendor (``npm ci && node web/build-vendor.js``)
  * Playwright with Chromium (``pip install playwright pillow && playwright install chromium``) and ffmpeg for the GIF.
A system Chrome can be used instead of Playwright's Chromium: set ``HB_CHROME=/path/to/chrome``.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "screenshots"

# Colours follow the Manager UI (Tailwind slate + logo gradient).
BG = "#0b1220"
BOX = "#0f172a"
BOX_BORDER = "#1e293b"
ACCENT = "#19e0c8"
TEXT = "#ffffff"
SUB = "#94a3b8"

DIAGRAM_TEXT = {
    "en": {
        "file": "how-it-works.png",
        "hb_sub": "settings per movie",
        "export": "export queue",
        "json": "(.json)",
        "mgr_sub": "web UI · NAS / home server",
        "jobs": "jobs by slots",
        "win_ttl": "Windows workers",
        "lin_ttl": "Linux workers",
        "count": "1 … N",
        "win_sub": "background app",
        "lin_sub": "Docker container",
        "nas_sub": "/inputs · /outputs",
        "mgr_nas": "mount · HTTP relay",
        "mgr_nas2": "for workers without NAS access",
        "win_nas": "SMB",
        "lin_nas": "mount",
    },
    "cs": {
        "file": "how-it-works.cs.png",
        "hb_sub": "nastavení pro každý film",
        "export": "export fronty",
        "json": "(.json)",
        "mgr_sub": "web · NAS / domácí server",
        "jobs": "úlohy podle slotů",
        "win_ttl": "Windows workery",
        "lin_ttl": "Linux workery",
        "count": "1 … N",
        "win_sub": "aplikace na pozadí",
        "lin_sub": "Docker kontejner",
        "nas_sub": "/inputs · /outputs",
        "mgr_nas": "mount · HTTP relay",
        "mgr_nas2": "pro workery bez přístupu k NASu",
        "win_nas": "SMB",
        "lin_nas": "mount",
    },
}


def _svg_data_uri(name: str) -> str:
    raw = (ROOT / "static" / name).read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


def diagram_html(lang: str) -> str:
    t = DIAGRAM_TEXT[lang]
    hbm = _svg_data_uri("hbm-gradient.svg")
    hbw = _svg_data_uri("hbw-gradient.svg")
    # Canvas 1440 x 500 CSS px, rendered at 2x -> 2880 x 1000 px.
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:transparent}}
body{{width:1440px;height:500px;font-family:Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased}}
.card{{position:absolute;inset:0;background:{BG};border-radius:28px;overflow:hidden}}
.box{{position:absolute;box-sizing:border-box;background:{BOX};border:1.5px solid {BOX_BORDER};border-radius:18px;
     display:flex;align-items:center;gap:18px;padding:0 24px}}
.mgr{{border:3px solid transparent;border-radius:20px;
     background:linear-gradient({BOX},{BOX}) padding-box,linear-gradient(135deg,#38f29b,#19e0c8 50%,#16a8e8) border-box}}
.box img{{flex:none}}
.ttl{{color:{TEXT};font-weight:700;font-size:24px;letter-spacing:-.2px;white-space:nowrap}}
.mgr .ttl{{font-size:25px}}
.sub{{color:{SUB};font-size:17px;margin-top:8px;white-space:nowrap}}
.lbl{{position:absolute;color:{ACCENT};font-weight:600;font-size:15px;white-space:nowrap;transform:translate(-50%,-50%)}}
.lbl.sm{{font-weight:500;font-size:13.5px;color:#5eead4;opacity:.9}}
svg{{position:absolute;inset:0}}
.ghost{{position:absolute;box-sizing:border-box;background:{BOX};border:1.5px solid {BOX_BORDER};border-radius:18px}}
.cnt{{position:absolute;color:{BG};background:{ACCENT};font-weight:800;font-size:15px;line-height:1;padding:6px 10px;
     border-radius:999px;white-space:nowrap;transform:translate(-50%,-50%)}}
</style></head><body><div class="card">
<div class="ghost" style="left:858px;top:44px;width:310px;height:112px;opacity:.45"></div>
<div class="ghost" style="left:849px;top:53px;width:310px;height:112px;opacity:.75"></div>
<div class="ghost" style="left:858px;top:308px;width:310px;height:112px;opacity:.45"></div>
<div class="ghost" style="left:849px;top:317px;width:310px;height:112px;opacity:.75"></div>
<svg width="1440" height="500" viewBox="0 0 1440 500">
  <defs>
    <marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5.2" markerHeight="5.2" orient="auto-start-reverse">
      <path d="M0 0L10 5L0 10z" fill="{ACCENT}"/></marker>
  </defs>
  <g stroke="{ACCENT}" stroke-width="2.6" fill="none" stroke-linecap="round">
    <line x1="292" y1="250" x2="394" y2="250" marker-end="url(#ah)"/>
    <line x1="749" y1="198" x2="834" y2="134" marker-end="url(#ah)"/>
    <line x1="749" y1="302" x2="834" y2="366" marker-end="url(#ah)"/>
    <line x1="1176" y1="134" x2="1219" y2="180" marker-start="url(#ah)" marker-end="url(#ah)"/>
    <line x1="1176" y1="366" x2="1219" y2="320" marker-start="url(#ah)" marker-end="url(#ah)"/>
    <line x1="757" y1="250" x2="1219" y2="250" marker-start="url(#ah)" marker-end="url(#ah)"/>
  </g>
</svg>
<div class="box" style="left:30px;top:195px;width:262px;height:110px;padding-left:24px">
  <div><div class="ttl">HandBrake</div><div class="sub">{t['hb_sub']}</div></div></div>
<div class="lbl" style="left:343px;top:226px">{t['export']}</div>
<div class="lbl" style="left:343px;top:274px">{t['json']}</div>
<div class="box mgr" style="left:400px;top:170px;width:349px;height:160px">
  <img src="{hbm}" width="56" height="56"><div><div class="ttl">HB Manager</div><div class="sub">{t['mgr_sub']}</div></div></div>
<div class="lbl" style="left:752px;top:124px">{t['jobs']}</div>
<div class="box" style="left:840px;top:62px;width:310px;height:112px">
  <img src="{hbw}" width="50" height="50"><div><div class="ttl">{t['win_ttl']}</div><div class="sub">{t['win_sub']}</div></div></div>
<div class="cnt" style="left:1150px;top:62px">{t['count']}</div>
<div class="box" style="left:840px;top:326px;width:310px;height:112px">
  <img src="{hbw}" width="50" height="50"><div><div class="ttl">{t['lin_ttl']}</div><div class="sub">{t['lin_sub']}</div></div></div>
<div class="cnt" style="left:1150px;top:326px">{t['count']}</div>
<div class="lbl" style="left:990px;top:229px">{t['mgr_nas']}</div>
<div class="lbl sm" style="left:990px;top:271px">{t['mgr_nas2']}</div>
<div class="lbl" style="left:1203px;top:128px">{t['win_nas']}</div>
<div class="lbl" style="left:1207px;top:372px">{t['lin_nas']}</div>
<div class="box" style="left:1225px;top:160px;width:185px;height:180px;padding-left:26px">
  <div><div class="ttl">NAS</div><div class="sub">{t['nas_sub']}</div></div></div>
</div></body></html>"""


def _launch(p):
    exe = os.environ.get("HB_CHROME") or None
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


# ---------------------------------------------------------------------------------------------------------------------
# Demo cluster for the UI screenshots: a real Manager (copy of this repo in a temp folder, own data folder, no network)
# with four simulated workers. Movies = open movies of the Blender Studio (CC BY); worker names, CPUs and IP addresses
# are made up. Nothing here touches the real data of a Manager.
# ---------------------------------------------------------------------------------------------------------------------
GB = 1024 ** 3
TOOL_VERSIONS = {"hbcli": "1.10.2", "ffmpeg": "8.0", "ffprobe": "8.0", "mkvpropedit": "95.0", "mkvmerge": "95.0", "7z": "25.01"}

DEMO_WORKERS = [
    # name, os, cpu model, threads, RAM GB, slots, power mode, prioritize 4K, IP, CPU load %, RAM used GB
    dict(name="Studio-PC", os="win", cpu="AMD Ryzen 9 7950X", threads=32, ram=64, slots=4, power="AUTO",
         prio4k=True, ip="192.168.1.21", load=92.0, ram_used=27.6),
    dict(name="Render-Node", os="linux", cpu="AMD Ryzen 9 5950X", threads=32, ram=64, slots=4, power="FULL",
         prio4k=True, ip="192.168.1.30", load=96.5, ram_used=24.9),
    dict(name="Office-Laptop", os="win", cpu="AMD Ryzen 7 7840U", threads=16, ram=32, slots=2, power="ECO",
         prio4k=False, ip="192.168.1.42", load=88.5, ram_used=12.4),
    dict(name="Mini-Server", os="linux", cpu="Intel Core i5-12500", threads=12, ram=32, slots=2, power="AUTO",
         prio4k=False, ip="192.168.1.50", load=90.5, ram_used=10.8),
]

# file, source WxH, output WxH, length s, preset, RF
MOVIES = {
    "sintel": ("Sintel (2010) [4K].mkv", (4096, 1744), (3840, 1636), 888, "4K Film – Grain", 19),
    "tos": ("Tears of Steel (2012) [4K].mkv", (3840, 1714), (3840, 1714), 734, "4K Digital Sharp", 20),
    "cosmos": ("Cosmos Laundromat (2015) [2K].mkv", (2048, 858), (1920, 804), 730, "1080p Film", 19),
    "ed": ("Elephants Dream (2006) [1080p].mkv", (1920, 1080), (1920, 1080), 654, "1080p Animation", 18),
    "spring": ("Spring (2019) [4K].mkv", (3840, 1608), (3840, 1608), 464, "4K Animation", 18),
    "sprite": ("Sprite Fright (2021) [4K].mkv", (3840, 1608), (3840, 1608), 629, "4K Animation", 18),
    "charge": ("Charge (2022) [4K].mkv", (3840, 1608), (3840, 1608), 230, "4K Digital Sharp", 20),
    "wingit": ("Wing It! (2023) [1080p].mkv", (1920, 1080), (1920, 1080), 210, "1080p Animation", 18),
    "bbb": ("Big Buck Bunny (2008) [4K].mkv", (3840, 2160), (3840, 2160), 596, "4K Animation", 18),
    "glass": ("Glass Half (2015) [1080p].mkv", (1920, 1080), (1920, 1080), 193, "1080p Animation", 18),
    "llamigos": ("Caminandes - Llamigos (2016) [1080p].mkv", (1920, 1080), (1920, 1080), 150, "1080p Animation", 18),
    "agent": ("Agent 327 - Operation Barbershop (2017) [4K].mkv", (3840, 1608), (3840, 1608), 231, "4K Animation", 18),
    "hero": ("Hero (2018) [4K].mkv", (3840, 1608), (3840, 1608), 237, "4K Digital Sharp", 20),
    "coffee": ("Coffee Run (2020) [4K].mkv", (3840, 1608), (3840, 1608), 184, "4K Animation", 18),
    "dweebs": ("The Daily Dweebs (2017) [1080p].mkv", (1920, 1080), (1920, 1080), 60, "1080p Animation", 18),
    "dillama": ("Caminandes - Gran Dillama (2013) [1080p].mkv", (1920, 1080), (1920, 1080), 146, "1080p Animation", 18),
}

# movie, worker, progress % when the screenshots are taken, fps, video bitrate in kb/s, source GB
RUNNING = [
    ("sintel", "Render-Node", 63.7, 8.1, 61000, 21.4),
    ("tos", "Studio-PC", 42.0, 7.0, 58000, 18.9),
    ("cosmos", "Office-Laptop", 23.0, 11.5, 29000, 7.6),
    ("ed", "Mini-Server", 79.0, 14.6, 18000, 4.1),
]
# movie, source GB, fps, video bitrate in kb/s (used when a worker takes the job in the GIF)
WAITING = [("spring", 12.3, 7.4, 52000), ("sprite", 16.5, 7.2, 56000), ("charge", 6.1, 7.6, 54000),
           ("wingit", 1.6, 15.1, 16000)]
# movie, worker, source GB, output GB, encoding time s, video kb/s in, out, finished minutes ago
COMPLETED = [
    ("bbb", "Render-Node", 9.8, 2.6, 1810, 132000, 33500, 610),
    ("dillama", "Mini-Server", 0.8, 0.21, 290, 41000, 10600, 560),
    ("agent", "Studio-PC", 5.3, 1.4, 1020, 180000, 47000, 470),
    ("glass", "Mini-Server", 1.2, 0.31, 380, 49000, 12400, 400),
    ("hero", "Render-Node", 4.9, 1.6, 980, 162000, 52000, 300),
    ("llamigos", "Office-Laptop", 0.9, 0.24, 330, 47000, 12500, 210),
    ("coffee", "Studio-PC", 3.6, 0.95, 760, 153000, 40000, 120),
    ("dweebs", "Mini-Server", 0.4, 0.11, 140, 51000, 14000, 45),
]


def _hb_queue_item(key: str) -> dict:
    """One item of a HandBrake queue export (.json), as the HandBrake GUI writes it."""
    fname, (sw, sh), (ow, oh), length, preset, rf = MOVIES[key]
    crop_tb = max(0, (sh * ow // sw - oh) // 2) if sw == ow else 0
    return {
        "Job": {
            "Source": {"Path": f"/inputs/{fname}", "Title": 1, "Range": {"Type": "chapter", "Start": 1, "End": 1}},
            "Destination": {"File": f"/outputs/{fname}", "Mux": "mkv"},
            "PAR": {"Num": 1, "Den": 1},
            "Video": {"Encoder": "x265_10bit", "Quality": rf, "Preset": "slow", "Profile": "main10"},
            "Audio": {"AudioList": [{"Track": 0, "Encoder": "copy"}, {"Track": 1, "Encoder": "copy"}]},
            "Subtitle": {"SubtitleList": [{"Track": 0}, {"Track": 1}, {"Track": 2}]},
            "Filters": {"FilterList": [{"ID": 11, "Settings": {"crop-top": crop_tb, "crop-bottom": crop_tb,
                                                               "crop-left": 0, "crop-right": 0, "width": ow, "height": oh}}]},
        },
        "Title": {"Path": f"/inputs/{fname}", "Geometry": {"Width": sw, "Height": sh},
                  "Duration": {"Hours": length // 3600, "Minutes": length % 3600 // 60, "Seconds": length % 60}},
        "uiSettings": {"PresetFullName": f"Custom Presets/{preset}", "PresetName": preset},
    }


def _release_version() -> str:
    v = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    return v.split("-dev")[0]


class DemoCluster:
    def __init__(self, python: str, port: int, vendor: Path, keep: bool = False):
        import tempfile
        self.python, self.port, self.vendor, self.keep = python, port, vendor, keep
        self.base = Path(tempfile.mkdtemp(prefix="hb-demo-"))
        self.url = f"http://127.0.0.1:{port}"
        self.proc = None
        self.jobs = {}            # movie key -> job id
        self.running = []         # dicts with live state of the simulated encodes
        self.waiting = []
        self.speed = 1.0          # simulated time factor (GIF = time-lapse)
        self.md5 = {}
        self._stop = False
        self._thread = None
        self._lock = None

    # -- HTTP ----------------------------------------------------------------------------------------------------
    def api(self, method: str, path: str, payload=None, raw: bytes | None = None, ctype: str = "application/json"):
        import json
        import urllib.request
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", ctype)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=20) as r:
            body = r.read()
        return json.loads(body) if body else {}

    def import_queue(self, keys):
        import json
        body = json.dumps([_hb_queue_item(k) for k in keys]).encode()
        bnd = "----hbdemo7d1f"
        raw = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"queue.json\"\r\n"
               f"Content-Type: application/json\r\n\r\n").encode() + body + f"\r\n--{bnd}--\r\n".encode()
        self.api("POST", "/api/v2/jobs/import", raw=raw, ctype=f"multipart/form-data; boundary={bnd}")
        st = self.api("GET", "/api/v2/state")
        by_name = {}
        for j in (st.get("queue") or []) + (st.get("active_jobs") or []) + (st.get("jobs") or []):
            if isinstance(j, dict) and j.get("original_filename"):
                by_name[j["original_filename"]] = j["id"]
        for k in keys:
            jid = by_name.get(MOVIES[k][0])
            if jid is None:
                raise RuntimeError(f"imported job not found in state: {MOVIES[k][0]}")
            self.jobs[k] = jid

    # -- Manager -------------------------------------------------------------------------------------------------
    def start(self):
        import hashlib
        import json
        import shutil
        import subprocess
        import time
        app = self.base / "app"
        shutil.copytree(ROOT, app, ignore_dangling_symlinks=True, ignore=shutil.ignore_patterns(
            ".git", "node_modules", "__pycache__", "docs", "Claude outputs", "data", "binaries", "*.db",
            "manager_config.json"))
        (app / "static" / "vendor").mkdir(parents=True, exist_ok=True)
        for f in ("vue.global.prod.js", "Sortable.min.js", "hb-tailwind.css"):
            shutil.copy2(self.vendor / f, app / "static" / "vendor" / f)
        version = _release_version()
        (app / "VERSION").write_text(version + "\n", encoding="utf-8")
        self.version = version
        data, inputs, outputs = self.base / "data", self.base / "inputs", self.base / "outputs"
        for d in (data, inputs, outputs):
            d.mkdir(parents=True, exist_ok=True)
        # Central tools: placeholder files with known versions, so the UI shows "all up to date" (nothing is run).
        store = {}
        names = {"linux": {"hbcli": "HandBrakeCLI", "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "mkvpropedit": "mkvpropedit",
                           "mkvmerge": "mkvmerge", "7z": "7zz"},
                 "windows": {"hbcli": "HandBrakeCLI.exe", "ffmpeg": "ffmpeg.exe", "ffprobe": "ffprobe.exe",
                             "mkvpropedit": "mkvpropedit.exe", "mkvmerge": "mkvmerge.exe", "7z": "7z.exe", "7z_dll": "7z.dll"}}
        for os_t, tools in names.items():
            (data / "binaries" / os_t).mkdir(parents=True, exist_ok=True)
            for tool, fn in tools.items():
                blob = (f"HB demo placeholder {os_t}/{fn}\n" * 600).encode()
                (data / "binaries" / os_t / fn).write_bytes(blob)
                md5 = hashlib.md5(blob).hexdigest()
                self.md5[(os_t, tool)] = md5
                if tool in TOOL_VERSIONS:
                    store[f"{os_t}_{tool}"] = {"version": TOOL_VERSIONS[tool], "md5": md5, "is_custom": False}
        (data / "binaries" / "versions.json").write_text(json.dumps(store, indent=2), encoding="utf-8")
        cfg = {
            "ui_lang": "en", "onboarding_done": True, "auto_build_on_first_run": False, "auto_update_binaries": False,
            "auto_update_cluster": False,
            "ui_prefs": {"tips_seen": ["queue_add", "workers_stop", "history_menu", "monitoring_ip"],
                         "changelog_seen": version, "logo_motion": "on"},
        }
        (app / "manager_config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "HB_DATA_DIR": str(data), "INPUTS_DIR": str(inputs), "OUTPUTS_DIR": str(outputs), "TZ": "UTC",
            # no network: update checks fail at once instead of reaching GitHub / GHCR
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost", "HB_UPDATE_CHANNEL": "",
        })
        self.log = open(self.base / "manager.log", "wb")
        self.proc = subprocess.Popen(
            [self.python, "-m", "uvicorn", "main:fastapi_app", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"], cwd=app, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 120:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Manager exited, see {self.base / 'manager.log'}")
            try:
                if self.api("GET", "/api/v2/state").get("version"):
                    return
            except Exception:
                time.sleep(1)
        raise RuntimeError("Manager did not start in 120 s")

    def stop(self):
        import shutil
        self._stop = True
        if self._thread:
            self._thread.join(timeout=10)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except Exception:
                self.proc.kill()
        try:
            self.log.close()
        except Exception:
            pass
        if not self.keep:
            shutil.rmtree(self.base, ignore_errors=True)

    # -- simulated workers ---------------------------------------------------------------------------------------
    def telemetry(self, w: dict, busy: bool = True) -> dict:
        import random
        os_t = "linux" if w["os"] == "linux" else "windows"
        load = (w["load"] + random.uniform(-2.2, 2.2)) if busy else random.uniform(2, 6)
        load = max(1.0, min(99.6, load))
        thr = w["threads"]
        if w["power"] == "ECO":
            mode = f"ECO ({max(1, thr - 2)}/{thr}t)"
        else:
            mode = f"FULL ({thr}/{thr}t)"
        diag = {
            "os": "Linux (Docker)" if os_t == "linux" else "Windows 11 Pro 24H2",
            "host_os": "Ubuntu 24.04 LTS" if os_t == "linux" else "",
            "net": {"route_ip": w["ip"], "ips": [w["ip"]]},
            "cpu_model": w["cpu"], "is_hybrid_cpu": False, "e_core_count": 0, "cpu_topology": {}, "custom_cores": [],
            "hb_version": TOOL_VERSIONS["hbcli"], "hb_source": "manager", "hb_md5": self.md5[(os_t, "hbcli")],
            "x265_version": "4.1",
            "ffmpeg_ok": True, "ffmpeg_version": TOOL_VERSIONS["ffmpeg"], "ffmpeg_source": "manager",
            "ffmpeg_md5": self.md5[(os_t, "ffmpeg")],
            "ffprobe_ok": True, "ffprobe_md5": self.md5[(os_t, "ffprobe")],
            "mkvpropedit_ok": True, "mkvpropedit_md5": self.md5[(os_t, "mkvpropedit")],
            "mkvmerge_ok": True, "mkvmerge_md5": self.md5[(os_t, "mkvmerge")], "mkv_version": TOOL_VERSIONS["mkvmerge"],
            "sevenzip_ok": True, "sevenzip_version": TOOL_VERSIONS["7z"], "sevenzip_md5": self.md5[(os_t, "7z")],
            "inputs_ok": True, "outputs_ok": True, "nas_dl_ok": True, "nas_out_ok": True,
            "temp_free_gb": {"Studio-PC": 1612.4, "Render-Node": 812.6, "Office-Laptop": 241.7, "Mini-Server": 418.3}[w["name"]],
        }
        hb_ram = 3.4 if busy else 0.0
        return {
            "cpu_total": round(load, 1), "cpu_hb": round(max(0.0, load - random.uniform(1.5, 3.5)) if busy else 0.0, 1),
            "cores": thr, "cores_total": thr,
            "ram_used_gb": round(w["ram_used"] + random.uniform(-0.3, 0.3), 1), "ram_total_gb": float(w["ram"]),
            "ram_hb_gb": hb_ram, "power_mode_active": mode, "diag": diag,
        }

    def ping_all(self):
        for w in DEMO_WORKERS:
            busy = any(r["worker"] == w["name"] for r in self.running)
            self.api("POST", "/api/workers/ping", {"worker_name": w["name"], "version": self.version,
                                                    "telemetry": self.telemetry(w, busy)})

    def send_progress(self, r: dict):
        w = next(x for x in DEMO_WORKERS if x["name"] == r["worker"])
        length = MOVIES[r["key"]][3]
        frames = length * 24.0
        done = frames * r["progress"] / 100.0
        r["fps_avg"] = r["fps"] * 0.97
        elapsed = int(done / max(0.1, r["fps_avg"]))
        eta = int((frames - done) / max(0.1, r["fps"]))
        src = int(r["src_gb"] * GB)
        self.api("POST", f"/api/jobs/{self.jobs[r['key']]}/progress", {
            "progress": round(r["progress"], 1), "worker_name": w["name"], "version": self.version,
            "telemetry": self.telemetry(w, True), "fps_current": r["fps"], "fps_avg": round(r["fps_avg"], 1),
            "speed_mbps": 0.0, "elapsed_sec": elapsed, "eta_sec": eta, "src_size_bytes": src,
            "transferred_bytes": int(src * r["progress"] / 100.0), "v_bitrate_in_kbps": r["kbps"],
            "a_bitrate_in_kbps": 1509, "is_net_fallback": False, "premux_status": "OFF"})

    def seed(self, warmup: int = 0):
        import random
        import sqlite3
        import datetime
        random.seed(7)
        self.ping_all()
        for w in DEMO_WORKERS:
            self.api("POST", f"/api/v2/workers/{w['name']}/policy", {
                "max_slots": w["slots"], "power_mode": w["power"], "prioritize_4k": w["prio4k"],
                "allow_4k": w["slots"] >= 4, "is_enabled": True})
        # history: imported, taken by a worker and finished
        self.import_queue([c[0] for c in COMPLETED])
        for key, wname, src_gb, out_gb, enc_s, kin, kout, _ in COMPLETED:
            jid = self.jobs[key]
            self.api("POST", f"/api/jobs/{jid}/progress", {"progress": 1.0, "worker_name": wname, "version": self.version,
                                                           "fps_current": 9.0, "v_bitrate_in_kbps": kin})
            self.api("POST", f"/api/jobs/{jid}/complete", {
                "worker_name": wname, "version": self.version, "src_size_bytes": int(src_gb * GB),
                "out_size_bytes": int(out_gb * GB), "duration_sec": enc_s, "v_bitrate_in_kbps": kin,
                "a_bitrate_in_kbps": 1509, "v_bitrate_out_kbps": kout, "a_bitrate_out_kbps": 1509,
                "mkv_stats_written": True, "subs_status": "OK", "final_dest_path": f"/outputs/{MOVIES[key][0]}"})
        db = sqlite3.connect(str(self.base / "data" / "cluster.db"), timeout=30)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        for key, *_rest in COMPLETED:
            ago = _rest[-1]
            db.execute("UPDATE jobs SET completed_at=?, created_at=? WHERE id=?",
                       ((now - datetime.timedelta(minutes=ago)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        (now - datetime.timedelta(minutes=ago + 40)).strftime("%Y-%m-%d %H:%M:%S.%f"), self.jobs[key]))
        db.commit()
        db.close()
        # queue: four encodes running, four waiting
        self.import_queue([r[0] for r in RUNNING] + [w[0] for w in WAITING])
        for key, wname, prog, fps, kbps, src_gb in RUNNING:
            start = max(1.0, prog - 100.0 * fps * warmup / (MOVIES[key][3] * 24.0))   # reaches `prog` after warm-up
            self.running.append({"key": key, "worker": wname, "progress": start, "fps": fps, "base_fps": fps,
                                 "kbps": kbps, "src_gb": src_gb})
        self.waiting = list(WAITING)
        for r in self.running:
            self.send_progress(r)

    def finish(self, r: dict):
        """Encode done: report it like a worker does and let the worker take the next job it can do."""
        length = MOVIES[r["key"]][3]
        src = int(r["src_gb"] * GB)
        self.api("POST", f"/api/jobs/{self.jobs[r['key']]}/complete", {
            "worker_name": r["worker"], "version": self.version, "src_size_bytes": src, "out_size_bytes": int(src * 0.27),
            "duration_sec": int(length * 24.0 / max(0.5, r["base_fps"])), "v_bitrate_in_kbps": r["kbps"],
            "a_bitrate_in_kbps": 1509, "v_bitrate_out_kbps": int(r["kbps"] * 0.27), "a_bitrate_out_kbps": 1509,
            "mkv_stats_written": True, "subs_status": "OK", "final_dest_path": f"/outputs/{MOVIES[r['key']][0]}"})
        self.running.remove(r)
        w = next(x for x in DEMO_WORKERS if x["name"] == r["worker"])
        for item in self.waiting:
            key, src_gb, fps, kbps = item
            if MOVIES[key][2][0] > 1920 and w["slots"] < 4:
                continue
            self.waiting.remove(item)
            nr = {"key": key, "worker": w["name"], "progress": 0.3, "fps": fps, "base_fps": fps, "kbps": kbps,
                  "src_gb": src_gb}
            self.running.append(nr)
            self.send_progress(nr)
            break

    def _loop(self):
        import random
        import time
        last = time.time()
        while not self._stop:
            time.sleep(1.0)
            now = time.time()
            dt, last = now - last, now
            try:
                for r in list(self.running):
                    length = MOVIES[r["key"]][3]
                    r["fps"] = round(max(0.5, r["base_fps"] + random.uniform(-0.4, 0.4)), 1)
                    r["progress"] = r["progress"] + 100.0 * r["fps"] * dt * self.speed / (length * 24.0)
                    if r["progress"] >= 100.0:
                        self.finish(r)
                    else:
                        self.send_progress(r)
                self.ping_all()
            except Exception as e:      # a busy Manager only delays the next tick
                print("  tick failed:", e)

    def run_background(self):
        import threading
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()


def _find_vendor(arg: str = "") -> Path:
    for cand in ([Path(arg)] if arg else []) + [ROOT / "static" / "vendor"]:
        if (cand / "vue.global.prod.js").is_file() and (cand / "hb-tailwind.css").is_file():
            return cand
    raise SystemExit("static/vendor is missing – build it first: npm ci && node web/build-vendor.js "
                     "(or pass --vendor <folder>)")


def _shoot(page, url: str, out: Path, wait_ms: int = 2500):
    from PIL import Image
    page.goto(url)
    page.wait_for_timeout(wait_ms)
    page.screenshot(path=str(out))
    # 256 colours are plenty for the dark UI and make the files ~7x smaller
    im = Image.open(out).convert("RGB")
    im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(out, optimize=True)
    print("wrote", out, f"({out.stat().st_size // 1024} kB)")


def cmd_screens(args) -> int:
    import time
    from playwright.sync_api import sync_playwright

    out = Path(args.out or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    demo = DemoCluster(args.python, args.port, _find_vendor(args.vendor), keep=args.keep)
    try:
        print("starting demo Manager in", demo.base)
        demo.start()
        demo.seed(args.warmup)
        demo.run_background()
        if args.warmup:
            print(f"warming up {args.warmup} s (fills the 5-minute CPU / RAM history)")
            time.sleep(args.warmup)
        with sync_playwright() as p:
            browser = _launch(p)
            only = set(args.only or [])
            if not only or "desktop" in only:
                pg = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
                _shoot(pg, demo.url + "/#queue", out / "queue.png")
                pg.set_viewport_size({"width": 1680, "height": 1050})
                _shoot(pg, demo.url + "/#workers", out / "workers.png")
                _shoot(pg, demo.url + "/#monitoring", out / "monitoring.png")
                pg.close()
            if not only or "mobile" in only:
                ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                          is_mobile=True, has_touch=True)
                pg = ctx.new_page()
                _shoot(pg, demo.url + "/#queue", out / "mobile.png")
                ctx.close()
            if not only or "gif" in only:
                make_gif(browser, demo, out / "demo.gif", args)
            browser.close()
    finally:
        demo.stop()
        if args.keep:
            print("kept", demo.base)
    return 0


def make_gif(browser, demo: DemoCluster, target: Path, args):
    """Short tour: queue (time-lapse) → workers → monitoring → queue; frames from the browser, GIF made by ffmpeg."""
    import shutil
    import subprocess
    import tempfile
    import time
    w, h = args.gif_size
    frames_dir = Path(tempfile.mkdtemp(prefix="hb-gif-"))
    pg = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=args.gif_scale)
    pg.goto(demo.url + "/#queue")
    pg.wait_for_timeout(2500)
    fps = args.gif_fps
    n = 0

    def grab(seconds: float, scroll_px: int = 0):
        """Frames for `seconds`; with scroll_px the page scrolls that far, evenly, in the middle 60 % of the time."""
        nonlocal n
        count = max(1, round(seconds * fps))
        a, b = int(count * 0.2), int(count * 0.8)
        step = scroll_px / max(1, b - a)
        for i in range(count):
            t0 = time.time()
            if scroll_px and a <= i < b:
                pg.evaluate(f"window.scrollBy(0, {step:.1f})")
            pg.screenshot(path=str(frames_dir / f"f{n:04d}.png"))
            n += 1
            time.sleep(max(0.0, 1.0 / fps - (time.time() - t0)))

    def go(tab: str):
        pg.evaluate(f"location.hash = '#{tab}'; window.scrollTo(0, 0)")
        pg.wait_for_timeout(300)

    demo.speed = args.gif_speed
    try:
        grab(7.0, scroll_px=args.gif_scroll)
        go("workers")
        grab(3.5)
        go("monitoring")
        grab(6.0, scroll_px=args.gif_scroll)
        go("queue")
        grab(2.5)
    finally:
        demo.speed = 1.0
        pg.close()
    print(f"captured {n} frames")
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SystemExit("ffmpeg not found (needed for the GIF)")
    out_w = int(w * args.gif_scale)
    vf = (f"fps={fps},scale={out_w}:-1:flags=lanczos,split[a][b];"
          f"[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle")
    subprocess.run([ff, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames_dir / "f%04d.png"),
                    "-vf", vf, "-loop", "0", str(target)], check=True)
    shutil.rmtree(frames_dir, ignore_errors=True)
    print("wrote", target, f"({target.stat().st_size / 1e6:.1f} MB)")


def cmd_diagram(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 500}, device_scale_factor=2)
        for lang, t in DIAGRAM_TEXT.items():
            page.set_content(diagram_html(lang))
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(150)
            page.screenshot(path=str(out / t["file"]), omit_background=True)
            print("wrote", out / t["file"])
        browser.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagram", help="how-it-works diagram (EN + CS)")
    d.add_argument("--out", help="output folder (default docs/screenshots)")
    d.set_defaults(func=cmd_diagram)
    s = sub.add_parser("screens", help="UI screenshots + demo.gif from a demo cluster")
    s.add_argument("--out", help="output folder (default docs/screenshots)")
    s.add_argument("--python", default=sys.executable, help="Python with the Manager requirements (requirements.txt)")
    s.add_argument("--port", type=int, default=8399)
    s.add_argument("--vendor", default="", help="folder with the built web assets (default static/vendor)")
    s.add_argument("--warmup", type=int, default=300, help="seconds before the screenshots (CPU history), default 300")
    s.add_argument("--only", nargs="*", choices=("desktop", "mobile", "gif"), help="make only some of the images")
    s.add_argument("--gif-size", type=lambda v: tuple(int(x) for x in v.lower().split("x")), default=(1100, 860),
                   help="GIF viewport in CSS px, default 1100x860")
    s.add_argument("--gif-scale", type=float, default=1.5, help="GIF pixel ratio, default 1.5")
    s.add_argument("--gif-fps", type=int, default=5)
    s.add_argument("--gif-speed", type=float, default=40.0, help="time-lapse factor of the encodes in the GIF")
    s.add_argument("--gif-scroll", type=int, default=0,
                   help="scroll the queue and monitoring this far in the GIF (CSS px); "
                        "every scrolled frame makes the GIF much bigger")
    s.add_argument("--keep", action="store_true", help="keep the demo folder (Manager log, database)")
    s.set_defaults(func=cmd_screens)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
