#!/usr/bin/env python3
# ==============================================================================
# HB CLUSTER - POVÝŠENÍ / KONTROLA VERZE (scripts/bump_version.py)
# ==============================================================================
"""
Jediný zdroj pravdy je soubor VERSION v kořeni repozitáře (semver, např. 2.12.0-dev.1).

Použití:
    python scripts/bump_version.py 2.12.0-dev.2   # povýší verzi v celém projektu
    python scripts/bump_version.py --check        # jen kontrola, nic nezapisuje

Skript je idempotentní: opakované spuštění se stejnou verzí nic nezmění.
Synchronizuje pouze místa, kde verze zůstat musí:
  - VERSION, workers/VERSION
  - zálohu FALLBACK_VERSION v manager_core.py
  - (workery verzi čtou jen ze souboru VERSION – literály pro workery <= 2.11.4ZH zrušeny v2.12.0, O3;
    kontrola níže hlídá, aby se do workers/*.py nevrátily)
  - OCI label org.opencontainers.image.version v obou Dockerfile (kontroluje CI)
  - (README.md už verzi neobsahuje – odznak „release“ ukazuje poslední vydání automaticky)
Build číslo HBWorker.exe se odvozuje ze semveru v workers/launcher_builder.py (nic se neukládá).
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?$")
ANY_VER = r'[0-9A-Za-z.\-]+'

WORKER_FILES = ["workers/worker_core.py", "workers/worker.pyw", "workers/worker_linux.py"]
_STALE_LITERAL_RE = re.compile(r'^(FALLBACK_VERSION|CURRENT_VERSION)\s*=\s*"', re.M)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _write_preserving_eol(rel: str, new_text_lf: str) -> bool:
    """Zapíše text se zachováním původních konců řádků (CRLF / LF). Vrací True, pokud se soubor změnil."""
    p = ROOT / rel
    raw = p.read_bytes().decode("utf-8") if p.exists() else ""
    eol = "\r\n" if "\r\n" in raw else "\n"
    out = new_text_lf.replace("\r\n", "\n").replace("\n", eol)
    if out == raw:
        return False
    p.write_bytes(out.encode("utf-8"))
    return True


def _rules(ver: str):
    rules = [("manager_core.py", re.compile(r'^(FALLBACK_VERSION\s*=\s*")' + ANY_VER + r'(")', re.M), r'\g<1>' + ver + r'\g<2>', 1)]
    for f in ("Dockerfile.manager", "Dockerfile.worker"):
        rules.append((f, re.compile(r'(org\.opencontainers\.image\.version=")' + ANY_VER + r'(")'), r'\g<1>' + ver + r'\g<2>', 1))
    return rules


def apply(ver: str, write: bool) -> list:
    problems, changed = [], []
    for rel in ("VERSION", "workers/VERSION"):
        cur = (ROOT / rel).read_text(encoding="utf-8").strip() if (ROOT / rel).exists() else ""
        if cur != ver:
            if write:
                _write_preserving_eol(rel, ver + "\n")
                changed.append(rel)
            else:
                problems.append(f"{rel}: '{cur}' != '{ver}'")
    texts = {}
    for rel, pat, repl, expected in _rules(ver):
        txt = texts.get(rel) if rel in texts else _read(rel).replace("\r\n", "\n")
        found = len(pat.findall(txt))
        if found != expected:
            problems.append(f"{rel}: vzor {pat.pattern[:50]}… nalezen {found}x (čekáno {expected}x)")
            texts[rel] = txt
            continue
        new_txt = pat.sub(repl, txt)
        if new_txt != txt and not write:
            problems.append(f"{rel}: verze neodpovídá {ver} ({pat.pattern[:40]}…)")
        texts[rel] = new_txt
    if write:
        for rel, txt in texts.items():
            if _write_preserving_eol(rel, txt):
                changed.append(rel)
    return problems if not write else (problems, changed)


def check_no_worker_literals() -> list:
    """Workery čtou verzi jen ze souboru VERSION; pevný literál verze by se po aktualizaci rozcházel."""
    out = []
    for rel in WORKER_FILES:
        if _STALE_LITERAL_RE.search(_read(rel)):
            out.append(f"{rel}: pevný literál FALLBACK_VERSION / CURRENT_VERSION (verze se čte jen z VERSION)")
    return out


def check_windows_package() -> list:
    """Každý soubor ve workers/ musí být ve Windows balíčku, nebo výslovně mimo něj (workers/package_layout.py)."""
    sys.path.insert(0, str(ROOT / "workers"))
    try:
        import package_layout
    except Exception as e:
        return [f"workers/package_layout.py nejde načíst: {e}"]
    return package_layout.check_repo(str(ROOT))


def check_locales_parity() -> list:
    a, b = (ROOT / "locales.js").read_bytes(), (ROOT / "workers/locales.js").read_bytes()
    ha, hb = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
    return [] if ha == hb else [f"locales.js ({ha[:12]}) != workers/locales.js ({hb[:12]})"]


def main(argv: list) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    check_only = argv[1] == "--check"
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if check_only else argv[1].strip().lstrip("vV")
    if not SEMVER_RE.match(ver):
        print(f"[!] '{ver}' není platný semver (např. 2.12.0-dev.1 nebo 2.12.0).")
        return 1
    if check_only:
        problems = apply(ver, write=False)
    else:
        problems, changed = apply(ver, write=True)
        for c in changed:
            print(f"  [✓] {c}")
        if not changed:
            print("  [-] Beze změny (verze už je všude nastavená).")
    problems += check_locales_parity()
    problems += check_windows_package()
    problems += check_no_worker_literals()
    if problems:
        print("[!] Nalezené problémy:")
        for p in problems:
            print("   -", p)
        return 1
    print(f"[✓] Verze {ver} je konzistentní, locales.js parita SHA-256 OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
