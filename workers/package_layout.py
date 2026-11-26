# ==============================================================================
# HB CLUSTER - JEDINÝ SEZNAM SOUBORŮ WINDOWS BALÍČKU (workers/package_layout.py)
# ==============================================================================
"""
Podle tohoto seznamu se skládá:
  - ZIP balíček z Manageru (main.py, /api/v2/workers/download_windows_package),
  - ZIP v GitHub Release (.github/workflows/docker-publish.yml),
  - OTA manifest Windows workeru (/api/update/manifest?target=windows).

PŘIDÁNÍ NOVÉHO SOUBORU: přidej řádek do WINDOWS_PACKAGE. Soubor ve workers/, který se balit
nemá, patří do NOT_PACKAGED. `python scripts/bump_version.py --check` i CI selžou, pokud soubor
ve workers/ není ani v jednom seznamu.
ODEBRÁNÍ SOUBORU: smaž řádek a jeho cestu v balíčku přidej do WINDOWS_PACKAGE_REMOVED –
Windows workery ho pak při aktualizaci samy smažou (jen tuto jednu cestu, nic jiného).

Pole záznamu:
  path : cesta v balíčku (kořen instalace, např. C:\\HBWorker), vždy s lomítky "/".
  src  : zdroj v repozitáři, nebo zvláštní zdroj na Manageru:
         @version  – obsah souboru VERSION,
         @readme   – obecný README.txt (bez předvyplněné konfigurace),
         @codesign – veřejný certifikát Manageru (.cer),
         @launcher – podepsaný HBWorker.exe (aktualizuje se přes manifest binárek),
         @config   – config.json s nastavením workeru (NIKDY se nepřepisuje).
  ota  : code   – program; mění se jen při nové verzi (ověření kompilace, výměna při restartu),
         file   – ostatní soubory; worker je porovná otiskem a vymění kdykoliv,
         binary – spravuje manifest binárek (HBWorker.exe),
         never  – jen v ZIPu, OTA ho nikdy nepřepíše.
  crlf : True = textový soubor pro Windows (konce řádků CRLF).
  optional : True = na Manageru nemusí existovat (např. certifikát ještě nevznikl).
"""
import os

WINDOWS_PACKAGE = [
    {"path": "app/worker.pyw", "src": "workers/worker.pyw", "ota": "code"},
    {"path": "app/worker_core.py", "src": "workers/worker_core.py", "ota": "code"},
    {"path": "app/locales.js", "src": "locales.js", "ota": "code"},
    {"path": "app/VERSION", "src": "@version", "ota": "code"},
    {"path": "VERSION", "src": "@version", "ota": "file", "crlf": True},
    {"path": "README.txt", "src": "@readme", "ota": "file", "crlf": True},
    {"path": "LICENSE", "src": "LICENSE", "ota": "never", "crlf": True},
    {"path": "THIRD-PARTY-NOTICES.md", "src": "THIRD-PARTY-NOTICES.md", "ota": "never", "crlf": True},
    {"path": "Uninstall_Worker.bat", "src": "workers/Uninstall_Worker.bat", "ota": "file", "crlf": True},
    {"path": "app/Uninstall_Worker.bat", "src": "workers/Uninstall_Worker.bat", "ota": "file", "crlf": True},
    {"path": "app/Start_Worker.bat", "src": "workers/Start_Worker.bat", "ota": "file", "crlf": True},
    {"path": "app/hbw.ico", "src": "static/hbw.ico", "ota": "file"},
    {"path": "app/hbw-gradient.svg", "src": "static/hbw-gradient.svg", "ota": "file"},
    {"path": "app/maseklab_codesign.cer", "src": "@codesign", "ota": "file", "optional": True},
    {"path": "HBWorker.exe", "src": "@launcher", "ota": "binary"},
    {"path": "app/config.json", "src": "@config", "ota": "never"},
]

# Cesty, které obsahovaly starší verze balíčku a worker je má při aktualizaci smazat.
WINDOWS_PACKAGE_REMOVED = []

# Soubory ve workers/, které do Windows balíčku nepatří (a proč).
NOT_PACKAGED = {
    "workers/worker_linux.py": "Linux worker – součást Docker obrazu",
    "workers/docker_selfupdate.py": "Manager a Linux worker – součást Docker obrazu",
    "workers/launcher_builder.py": "sestavení HBWorker.exe (Manager / CI)",
    "workers/package_layout.py": "tento seznam (Manager / CI)",
    "workers/locales.js": "kopie kořenového locales.js",
    "workers/VERSION": "kopie kořenového VERSION",
    "workers/config.json": "lokální vývojová konfigurace (není v gitu)",
    "workers/hbw.ico": "kopie static/hbw.ico pro spuštění workeru přímo z repozitáře",
}

_IGNORED_DIRS = {"__pycache__", "bin", "HB_Temp", "temp", "local_hold", "ota_cache", "ota_stage"}
_IGNORED_SUFFIXES = (".pyc", ".log", ".tmp", ".bak", ".prev")
# Soubory, které vznikají za běhu workeru spuštěného přímo z repozitáře (nejsou součástí balíčku)
_IGNORED_FILES = {"update_attempt.json", "update_result.json", "ota_ok.txt", "ota_pid.txt", "selftest_result.json",
                  "worker_identity.json", "manager_binding.json"}


def ota_file_entries():
    """Záznamy, které OTA vyměňuje kdykoliv podle otisku (ota == "file")."""
    return [e for e in WINDOWS_PACKAGE if e["ota"] == "file"]


def is_safe_package_path(p: str) -> bool:
    """Cesta v balíčku: relativní, bez '..', bez dvojtečky a zpětných lomítek."""
    if not p or p.startswith("/") or "\\" in p or ":" in p:
        return False
    return all(part not in ("", ".", "..") for part in p.split("/"))


def check_repo(repo_root: str) -> list:
    """Vrací seznam problémů (prázdný = v pořádku)."""
    problems = []
    packaged_src = {e["src"] for e in WINDOWS_PACKAGE if not e["src"].startswith("@")}
    for e in WINDOWS_PACKAGE:
        if not is_safe_package_path(e["path"]):
            problems.append(f"neplatná cesta v balíčku: {e['path']}")
        if not e["src"].startswith("@") and not os.path.isfile(os.path.join(repo_root, e["src"])):
            problems.append(f"zdroj neexistuje: {e['src']} (pro {e['path']})")
    for p in WINDOWS_PACKAGE_REMOVED:
        if not is_safe_package_path(p):
            problems.append(f"neplatná cesta ke smazání: {p}")
    wdir = os.path.join(repo_root, "workers")
    for fn in sorted(os.listdir(wdir)):
        full = os.path.join(wdir, fn)
        if os.path.isdir(full) or fn in _IGNORED_DIRS or fn in _IGNORED_FILES or fn.endswith(_IGNORED_SUFFIXES):
            continue
        rel = f"workers/{fn}"
        if rel not in packaged_src and rel not in NOT_PACKAGED:
            problems.append(f"{rel} není ve WINDOWS_PACKAGE ani v NOT_PACKAGED (workers/package_layout.py)")
    return problems


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    issues = check_repo(root)
    for i in issues:
        print("[!]", i)
    print("[✓] Seznam Windows balíčku je v pořádku." if not issues else f"[!] Problémů: {len(issues)}")
    sys.exit(1 if issues else 0)
