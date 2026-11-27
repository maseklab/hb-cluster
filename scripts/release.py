#!/usr/bin/env python3
# ==============================================================================
# HB CLUSTER - VYDÁNÍ VERZE (scripts/release.py)
# ==============================================================================
"""
Připraví vydání LOKÁLNĚ. Nikdy nic nepushuje a nic nemaže na GitHubu – na konci vypíše
příkazy `git push`, které spustí autor. Bez --apply jen vypíše, co by udělal (nic nemění).

Běžné vydání (větev dev -> main, na main jeden squash commit na verzi):
    python scripts/release.py 2.12.1 --date 2026-12-20 --apply

Jednorázový čistý start historie (před zveřejněním repozitáře):
    python scripts/release.py 2.12.0 --date 2026-12-01 --fresh-start --apply

Kroky:
  1. kontrola: aktuální větev dev, žádné necommitnuté změny
  2. scripts/bump_version.py <verze> + odznak docs/badges/release.svg (scripts/badges.py)
  3. CHANGELOG.md: "## [<verze>] - Unreleased" -> "## [<verze>] - <datum>" (sekce verze musí existovat)
  4. commit na dev "Release v<verze>"
  5a. běžné vydání: main <- squash dev ("HB Cluster v<verze>"), tag v<verze>,
      dev <- "merge -s ours main" (zaznamená vydání, obsah dev se nemění; další squash vezme jen nové změny)
  5b. čistý start: záloha CELÉ historie do .bundle vedle složky repozitáře, nový kořenový commit
      "HB Cluster v<verze>" se stromem dev, main i dev na něj, lokální tagy smazány, tag v<verze>
  6. výpis příkazů git push pro autora
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def git(*args, capture=True, check=True) -> str:
    # autocrlf=input: do historie jdou vždy LF (stejně jako dosud), CRLF v pracovní složce Windows nevadí
    res = subprocess.run(["git", "-c", "core.autocrlf=input", "-c", "core.safecrlf=false", *args], cwd=ROOT, capture_output=capture, text=True, encoding="utf-8")
    if check and res.returncode != 0:
        msg = ((res.stderr or "") + (res.stdout or "")).strip() if capture else ""
        raise SystemExit(f"[x] git {' '.join(args)} selhal: {msg}")
    return (res.stdout or "").strip() if capture else ""


def step(apply: bool, text: str, fn=None):
    print(("[>] " if apply else "[plan] ") + text)
    if apply and fn:
        return fn()
    return None


def update_changelog(ver: str, date_s: str, apply: bool) -> None:
    p = ROOT / "CHANGELOG.md"
    raw = p.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    txt = raw.replace("\r\n", "\n")
    pat_unrel = re.compile(r"^## \[" + re.escape(ver) + r"\] - Unreleased[ \t]*$", re.M)
    pat_any = re.compile(r"^## \[" + re.escape(ver) + r"\] - .+$", re.M)
    if pat_unrel.search(txt):
        new = pat_unrel.sub(f"## [{ver}] - {date_s}", txt, count=1)
        step(apply, f"CHANGELOG.md: [{ver}] - Unreleased -> [{ver}] - {date_s}",
             lambda: p.write_bytes((new.replace("\n", "\r\n") if crlf else new).encode("utf-8")))
    elif pat_any.search(txt):
        print(f"[=] CHANGELOG.md už má sekci [{ver}] s datem – beze změny.")
    else:
        raise SystemExit(f"[x] CHANGELOG.md nemá sekci '## [{ver}] - Unreleased'. Nejdřív ji doplň (anglicky, jen pro uživatele).")


def _release_badge(ver: str) -> None:
    # odznak „release“ v README je lokální SVG (README nezávisí na shields.io)
    sys.path.insert(0, str(ROOT / "scripts"))
    import badges
    for c in badges.write_all(ver):
        print(f"  [✓] {c}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Připraví vydání HB Cluster lokálně (bez pushe).")
    ap.add_argument("version", help="verze vydání, např. 2.12.0 (bez -dev)")
    ap.add_argument("--date", default=datetime.date.today().isoformat(), help="datum vydání YYYY-MM-DD (výchozí dnes)")
    ap.add_argument("--fresh-start", action="store_true", help="jednorázově nahradit celou historii jedním commitem")
    ap.add_argument("--apply", action="store_true", help="opravdu provést (bez něj jen výpis kroků)")
    a = ap.parse_args()

    ver, date_s, apply = a.version.strip().lstrip("v"), a.date.strip(), a.apply
    if not SEMVER_RE.match(ver):
        raise SystemExit(f"[x] '{ver}' není verze vydání ve tvaru X.Y.Z")
    try:
        datetime.date.fromisoformat(date_s)
    except ValueError:
        raise SystemExit(f"[x] '{date_s}' není datum ve tvaru YYYY-MM-DD")
    tag = f"v{ver}"
    msg_release = f"HB Cluster {tag}"

    # 1. předpoklady
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "dev":
        raise SystemExit(f"[x] Vydání se dělá z větve dev (teď: {branch}).")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("[x] Ve větvi dev jsou necommitnuté změny – nejdřív je commitni.")
    if git("tag", "-l", tag):
        raise SystemExit(f"[x] Tag {tag} už existuje.")
    has_main = bool(git("branch", "--list", "main"))
    if not has_main and not a.fresh_start:
        raise SystemExit("[x] Chybí lokální větev main (git branch main origin/main).")
    print(f"HB Cluster – vydání {tag} ({date_s}){' – ČISTÝ START HISTORIE' if a.fresh_start else ''}"
          f"{'' if apply else ' – jen výpis, nic se nemění (přidej --apply)'}")

    # 2.–4. verze, CHANGELOG, commit na dev
    step(apply, f"scripts/bump_version.py {ver}",
         lambda: subprocess.run([sys.executable, str(ROOT / "scripts" / "bump_version.py"), ver], cwd=ROOT, check=True))
    step(apply, f"docs/badges/release.svg -> v{ver}", lambda: _release_badge(ver))
    update_changelog(ver, date_s, apply)

    def _commit_dev():
        if git("status", "--porcelain", "--untracked-files=no"):
            git("commit", "-a", "-m", f"Release {tag}")

    step(apply, f"commit na dev: \"Release {tag}\"", _commit_dev)

    if a.fresh_start:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bundle = ROOT.parent / f"{ROOT.name}-history-{stamp}.bundle"
        old_tags = git("tag", "-l").split()
        step(apply, f"záloha celé historie: {bundle}", lambda: git("bundle", "create", str(bundle), "--all"))

        def _fresh():
            tree = git("rev-parse", "dev^{tree}")
            root_c = git("commit-tree", tree, "-m", msg_release)
            git("branch", "-f", "main", root_c)
            git("reset", "--hard", root_c)
            for t in old_tags:
                git("tag", "-d", t)
            git("tag", "-a", tag, "-m", msg_release, root_c)
            return root_c

        step(apply, f"nový kořenový commit \"{msg_release}\" se stromem dev; main i dev na něj; "
                    f"smazat {len(old_tags)} lokálních tagů; tag {tag}", _fresh)
        # tagy a větve, které jsou jen na GitHubu (lokálně je nevidíme) – smazat je musí autor
        r_tags, r_heads = set(), set()
        for ln in git("ls-remote", "--tags", "--heads", "origin", check=False).splitlines():
            ref = ln.split("\t")[-1].strip()
            if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
                r_tags.add(ref[len("refs/tags/"):])
            elif ref.startswith("refs/heads/") and ref[len("refs/heads/"):] not in ("main", "dev"):
                r_heads.add(ref[len("refs/heads/"):])
        del_tags = sorted((set(old_tags) | r_tags) - {tag})
        print("\n" + ("Hotovo lokálně." if apply else "Nic nezměněno (bez --apply).") + " Na GitHubu pak spouští autor (repo zůstává, jen se přepíše historie):")
        print("  1) na GitHubu: zavři otevřené pull requesty (i od Dependabota), smaž stará vydání (Releases -> Delete;")
        print("     smazání vydání tag nemaže) a ideálně i staré běhy Actions – po zveřejnění jsou vidět všem;")
        print("     hromadně v PowerShellu: gh run list -R maseklab/hb-cluster -L 500 --json databaseId --jq \".[].databaseId\" |")
        print("       ForEach-Object { gh api -X DELETE \"repos/maseklab/hb-cluster/actions/runs/$_\" }")
        print("  2) git push --force origin main dev")
        if del_tags:
            print(f"  3) git push origin --delete {' '.join(del_tags)}")
        if r_heads:
            print(f"     git push origin --delete {' '.join(sorted(r_heads))}   (další větve na GitHubu)")
        if not del_tags and not r_heads:
            print("  3) (na GitHubu nejsou žádné staré tagy ani další větve – nic k mazání)")
        print(f"  4) git push origin {tag}")
        print("  5) Settings -> General -> Default branch -> main")
        print("  6) po doběhnutí buildu (Actions zelené, vydání s HB-Worker-Windows.zip): Settings -> General -> Change visibility -> Public")
        print("  Pozor: záložka Activity (force push -> Compare changes) ukáže i starou historii. Kdo ji nechce zveřejnit vůbec,")
        print("  dá novou historii do nového prázdného repozitáře (staré přejmenovat a nechat soukromé).")
        print(f"  Záloha staré historie: {bundle.name} (obnova: git clone <bundle> <složka>)")
        return 0

    # 5a. běžné vydání
    def _squash():
        git("checkout", "main")
        git("merge", "--squash", "dev")
        if git("status", "--porcelain", "--untracked-files=no"):
            git("commit", "-m", msg_release)
        git("tag", "-a", tag, "-m", msg_release)
        git("checkout", "dev")
        git("merge", "-s", "ours", "--no-edit", "-m", f"Sync dev with release {tag}", "main")

    step(apply, f"main <- squash dev (\"{msg_release}\"), tag {tag}, dev <- merge -s ours main", _squash)
    print("\n" + ("Hotovo lokálně." if apply else "Nic nezměněno (bez --apply).") + " Na GitHub pak pushuje autor:")
    print(f"  git push origin main dev && git push origin {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
