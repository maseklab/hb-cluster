#!/usr/bin/env python3
# ==============================================================================
# HB CLUSTER - ODZNAKY README (scripts/badges.py)
# ==============================================================================
"""
Odznaky v README.md / README.cs.md jsou lokální SVG v docs/badges/ – README tak nezávisí
na shields.io (GitHub je načítá přes svou proxy, a dynamické dotazy na shields.io jí občas
nestihnou odpovědět; místo obrázku pak zbyde jen text).

Vzhled kopíruje nativní odznak GitHub Actions (odznak „Build“ v README): tmavá levá část
s ikonou, barevná pravá část, výška 20 px, písmo DejaVu Sans / Verdana 11 px.

Použití:
    python scripts/badges.py                 # přegeneruje všechny odznaky (release ponechá svou verzi)
    python scripts/badges.py --release 2.12.1  # navíc nastaví verzi v odznaku release
    python scripts/badges.py --check         # jen kontrola, nic nezapisuje

Odznak release přepisuje při vydání scripts/release.py, kontrolu volá scripts/bump_version.py --check.
Ikony Docker a Ko-fi: Simple Icons (CC0-1.0). Ikony release a License: vlastní.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BADGE_DIR = ROOT / "docs" / "badges"
RELEASE_FILE = BADGE_DIR / "release.svg"
RELEASE_VER_RE = re.compile(r'<title>release: v(\d+\.\d+\.\d+)</title>')

# Šířky znaků ASCII 32–126 pro DejaVu Sans 11 px (z hmtx fontu). Text má navíc textLength,
# takže na Windows (Verdana místo DejaVu) se jen nepatrně stáhne/roztáhne a nikdy nepřeteče.
_W = """
    3.497 4.41 5.06 9.217 6.999 10.452 8.578 3.024 4.292 4.292 5.5 9.217 3.497 3.969 3.497 3.706
    6.999 6.999 6.999 6.999 6.999 6.999 6.999 6.999 6.999 6.999 3.706 3.706 9.217 9.217 9.217 5.838
    11.0 7.525 7.546 7.681 8.47 6.95 6.327 8.524 8.271 3.244 3.244 7.213 6.128 9.491 8.229 8.658
    6.633 8.658 7.643 6.982 6.719 8.051 7.525 10.876 7.536 6.719 7.536 4.292 3.706 4.292 9.217 5.5
    5.5 6.741 6.982 6.048 6.982 6.768 3.873 6.982 6.972 3.056 3.056 6.37 3.056 10.715 6.972 6.73
    6.982 6.982 4.522 5.731 4.313 6.972 6.51 8.997 6.51 6.51 5.774 6.999 3.706 6.999 9.217
""".split()
WIDTHS = {chr(32 + i): float(v) for i, v in enumerate(_W)}

# Ikony 14 × 14 px vlevo (stejné místo jako logo GitHubu v nativním odznaku), šedé #959DA5.
_SI = 'transform="translate(4 3) scale(0.5833)"'   # Simple Icons mají viewBox 24
_OWN = 'transform="translate(4 3) scale(0.875)"'   # vlastní ikony kreslené v mřížce 16
ICONS = {
    "docker": f'<path {_SI} fill="#959DA5" d="M13.983 11.078h2.119a.186.186 0 00.186-.185V9.006a.186.186 0 00-.186-.186h-2.119a.185.185 0 00-.185.185v1.888c0 .102.083.185.185.185m-2.954-5.43h2.118a.186.186 0 00.186-.186V3.574a.186.186 0 00-.186-.185h-2.118a.185.185 0 00-.185.185v1.888c0 .102.082.185.185.185m0 2.716h2.118a.187.187 0 00.186-.186V6.29a.186.186 0 00-.186-.185h-2.118a.185.185 0 00-.185.185v1.887c0 .102.082.185.185.186m-2.93 0h2.12a.186.186 0 00.184-.186V6.29a.185.185 0 00-.185-.185H8.1a.185.185 0 00-.185.185v1.887c0 .102.083.185.185.186m-2.964 0h2.119a.186.186 0 00.185-.186V6.29a.185.185 0 00-.185-.185H5.136a.186.186 0 00-.186.185v1.887c0 .102.084.185.186.186m5.893 2.715h2.118a.186.186 0 00.186-.185V9.006a.186.186 0 00-.186-.186h-2.118a.185.185 0 00-.185.185v1.888c0 .102.082.185.185.185m-2.93 0h2.12a.185.185 0 00.184-.185V9.006a.185.185 0 00-.184-.186h-2.12a.185.185 0 00-.184.185v1.888c0 .102.083.185.185.185m-2.964 0h2.119a.185.185 0 00.185-.185V9.006a.185.185 0 00-.184-.186h-2.12a.186.186 0 00-.186.186v1.887c0 .102.084.185.186.185m-2.92 0h2.12a.185.185 0 00.184-.185V9.006a.185.185 0 00-.184-.186h-2.12a.185.185 0 00-.184.185v1.888c0 .102.082.185.185.185M23.763 9.89c-.065-.051-.672-.51-1.954-.51-.338.001-.676.03-1.01.087-.248-1.7-1.653-2.53-1.716-2.566l-.344-.199-.226.327c-.284.438-.49.922-.612 1.43-.23.97-.09 1.882.403 2.661-.595.332-1.55.413-1.744.42H.751a.751.751 0 00-.75.748 11.376 11.376 0 00.692 4.062c.545 1.428 1.355 2.48 2.41 3.124 1.18.723 3.1 1.137 5.275 1.137.983.003 1.963-.086 2.93-.266a12.248 12.248 0 003.823-1.389c.98-.567 1.86-1.288 2.61-2.136 1.252-1.418 1.998-2.997 2.553-4.4h.221c1.372 0 2.215-.549 2.68-1.009.309-.293.55-.65.707-1.046l.098-.288Z"/>',
    "kofi": f'<path {_SI} fill="#959DA5" d="M11.351 2.715c-2.7 0-4.986.025-6.83.26C2.078 3.285 0 5.154 0 8.61c0 3.506.182 6.13 1.585 8.493 1.584 2.701 4.233 4.182 7.662 4.182h.83c4.209 0 6.494-2.234 7.637-4a9.5 9.5 0 0 0 1.091-2.338C21.792 14.688 24 12.22 24 9.208v-.415c0-3.247-2.13-5.507-5.792-5.87-1.558-.156-2.65-.208-6.857-.208m0 1.947c4.208 0 5.09.052 6.571.182 2.624.311 4.13 1.584 4.13 4v.39c0 2.156-1.792 3.844-3.87 3.844h-.935l-.156.649c-.208 1.013-.597 1.818-1.039 2.546-.909 1.428-2.545 3.064-5.922 3.064h-.805c-2.571 0-4.831-.883-6.078-3.195-1.09-2-1.298-4.155-1.298-7.506 0-2.181.857-3.402 3.012-3.714 1.533-.233 3.559-.26 6.39-.26m6.547 2.287c-.416 0-.65.234-.65.546v2.935c0 .311.234.545.65.545 1.324 0 2.051-.754 2.051-2s-.727-2.026-2.052-2.026m-10.39.182c-1.818 0-3.013 1.48-3.013 3.142 0 1.533.858 2.857 1.949 3.897.727.701 1.87 1.429 2.649 1.896a1.47 1.47 0 0 0 1.507 0c.78-.467 1.922-1.195 2.623-1.896 1.117-1.039 1.974-2.364 1.974-3.897 0-1.662-1.247-3.142-3.039-3.142-1.065 0-1.792.545-2.338 1.298-.493-.753-1.246-1.298-2.312-1.298"/>',
    "tag": f'<path {_OWN} fill="#959DA5" fill-rule="evenodd" d="M1 1.75C1 1.34 1.34 1 1.75 1h5.6c.2 0 .39.08.53.22l7.4 7.4a.75.75 0 0 1 0 1.06l-5.6 5.6a.75.75 0 0 1-1.06 0l-7.4-7.4A.75.75 0 0 1 1 7.35zM4.5 6a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"/>',
    "law": f'<g {_OWN} fill="#959DA5"><path d="M7.3 1.5h1.4v12H7.3zM2.5 3h11v1.3h-11zM4.5 13h7v1.5h-7zM.8 9.3h5a2.5 2.5 0 0 1-5 0zM10.2 9.3h5a2.5 2.5 0 0 1-5 0z"/><path fill="none" stroke="#959DA5" stroke-width="1" d="M1.2 9.3 3.3 4.3l2.1 5M10.6 9.3l2.1-5 2.1 5"/></g>',
}

# (soubor, popisek vlevo, text vpravo, barva vpravo, ikona); release se doplní verzí
STATIC_BADGES = [
    ("docker-manager.svg", "Docker", "hb-manager", "#0284c7", "docker"),
    ("docker-worker.svg", "Docker", "hb-worker", "#10b981", "docker"),
    ("license.svg", "License", "MIT", "#38bdf8", "law"),
    ("kofi.svg", "Ko-fi", "Support", "#f59e0b", "kofi"),
    ("kofi-cs.svg", "Ko-fi", "Podpora", "#f59e0b", "kofi"),
]
RELEASE_COLOR = "#6366f1"


def text_width(s: str) -> float:
    return sum(WIDTHS.get(ch, WIDTHS["W"]) for ch in s)


def _lighter(hex_color: str, k: float = 0.18) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(round(c + (255 - c) * k) for c in (r, g, b))


def _f(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_badge(label: str, message: str, color: str, icon: str = "", title: str = "") -> str:
    """SVG odznak ve stylu nativního odznaku GitHub Actions (výška 20 px, zaoblení 3 px)."""
    lw_t, mw_t = text_width(label), text_width(message)
    lx = 22 if icon else 6
    lw = round(lx + lw_t + 5)
    mw = round(5 + mw_t + 5)
    total = lw + mw
    font = 'font-family="\'DejaVu Sans\',Verdana,Geneva,sans-serif" font-size="11"'

    def txt(x, s, w):
        a = f'x="{_f(x)}" textLength="{_f(w)}" lengthAdjust="spacingAndGlyphs"'
        return (f'<text fill="#010101" fill-opacity=".3" aria-hidden="true" {a} y="15">{_esc(s)}</text>'
                f'<text fill="#FFFFFF" {a} y="14">{_esc(s)}</text>')

    title = title or f"{label}: {message}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{_esc(title)}">',
        f'<title>{_esc(title)}</title>',
        '<defs>',
        '<linearGradient id="l" x1="50%" y1="0%" x2="50%" y2="100%"><stop stop-color="#444D56" offset="0%"/><stop stop-color="#24292E" offset="100%"/></linearGradient>',
        f'<linearGradient id="r" x1="50%" y1="0%" x2="50%" y2="100%"><stop stop-color="{_lighter(color)}" offset="0%"/><stop stop-color="{color.upper()}" offset="100%"/></linearGradient>',
        '</defs>',
        f'<path fill="url(#l)" d="M3 0h{lw - 3}v20H3a3 3 0 0 1-3-3V3a3 3 0 0 1 3-3z"/>',
        f'<path fill="url(#r)" d="M{lw} 0h{mw - 3}a3 3 0 0 1 3 3v14a3 3 0 0 1-3 3h-{mw - 3}z"/>',
        f'<g {font}>{txt(lx, label, lw_t)}{txt(lw + 5, message, mw_t)}</g>',
    ]
    if icon:
        parts.append(ICONS[icon])
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def make_release_badge(ver: str) -> str:
    return make_badge("release", f"v{ver}", RELEASE_COLOR, "tag", title=f"release: v{ver}")


def current_release_version() -> str:
    """Verze, kterou teď ukazuje docs/badges/release.svg ('' když soubor chybí nebo je poškozený)."""
    if not RELEASE_FILE.exists():
        return ""
    m = RELEASE_VER_RE.search(RELEASE_FILE.read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def expected_files(release_ver: str) -> dict:
    out = {name: make_badge(lab, msg, col, ic) for name, lab, msg, col, ic in STATIC_BADGES}
    out["release.svg"] = make_release_badge(release_ver)
    return out


def write_all(release_ver: str = "") -> list:
    """Zapíše odznaky, které se liší; vrací seznam změněných souborů (relativně k repozitáři)."""
    release_ver = release_ver or current_release_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", release_ver or ""):
        raise SystemExit(f"[x] Neplatná verze pro odznak release: '{release_ver}' (čekám X.Y.Z)")
    BADGE_DIR.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, svg in expected_files(release_ver).items():
        p = BADGE_DIR / name
        if not p.exists() or p.read_bytes() != svg.encode("utf-8"):
            p.write_bytes(svg.encode("utf-8"))
            changed.append(p.relative_to(ROOT).as_posix())
    return changed


def check(version_file: str = "") -> list:
    """Problémy s odznaky. version_file = obsah VERSION: u vydání (bez -dev) musí release odznak sedět."""
    problems = []
    rel = current_release_version()
    if not rel:
        return [f"{RELEASE_FILE.relative_to(ROOT).as_posix()}: chybí, nebo v něm nejde najít verzi"]
    for name, svg in expected_files(rel).items():
        p = BADGE_DIR / name
        if not p.exists() or p.read_bytes() != svg.encode("utf-8"):
            problems.append(f"docs/badges/{name}: neodpovídá scripts/badges.py (spusť: python scripts/badges.py)")
    if re.fullmatch(r"\d+\.\d+\.\d+", version_file) and rel != version_file:
        problems.append(f"docs/badges/release.svg ukazuje v{rel}, ale VERSION je {version_file}")
    return problems


def main(argv: list) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--check" in argv:
        ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        probs = check(ver)
        for p in probs:
            print("  [!]", p)
        if not probs:
            print(f"[✓] Odznaky OK (release v{current_release_version()}).")
        return 1 if probs else 0
    rel = ""
    if "--release" in argv:
        i = argv.index("--release")
        rel = argv[i + 1].strip().lstrip("vV") if i + 1 < len(argv) else ""
    changed = write_all(rel)
    for c in changed:
        print(f"  [✓] {c}")
    if not changed:
        print("  [-] Beze změny.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
