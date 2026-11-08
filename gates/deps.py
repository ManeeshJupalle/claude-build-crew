#!/usr/bin/env python3
"""deps: find outdated and vulnerable dependencies with the project's own tools.

Usage:
  deps.py    print JSON

npm projects are checked with `npm outdated` and `npm audit`; Python projects
with `pip list --outdated` in the project's virtualenv and `pip-audit --path`
from outside it.
A missing tool is SKIPPED, never "up to date": if nothing could be checked the
gate fails (NOT VERIFIED), and so does a tool that errors or prints output we
can't read. A known vulnerability fails the gate too.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, git, main_guarded, run, split_command  # noqa: E402

TIMEOUT = 600
VENV_TOOLS = {"pip", "setuptools", "wheel"}  # come with the virtualenv, not the project


def version_key(v: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", v)]


def bump(current: str, latest: str) -> str:
    cur, new = ((version_key(v) + [0, 0, 0])[:3] for v in (current, latest))
    # as npm's ^: only what's right of the first non-zero part may change. ^1.2.3 is <2.0.0, ^0.2.3 <0.3.0, ^0.0.3 <0.0.4
    lead = next((i for i, n in enumerate(cur) if n), 2)
    if new[:lead + 1] != cur[:lead + 1]:
        return "major"
    return "minor" if new[1] != cur[1] else "patch"


def tool(env: str, name: str) -> list[str] | None:
    """Tests point the env var at a fake; otherwise the tool on PATH."""
    if os.environ.get(env):
        return split_command(os.environ[env])
    path = shutil.which(name)
    return [path] if path else None


def venv_python(root: Path) -> list[str] | None:
    if os.environ.get("BUILD_CREW_PYTHON"):
        return split_command(os.environ["BUILD_CREW_PYTHON"])
    scripts, exe = ("Scripts", ".exe") if os.name == "nt" else ("bin", "")
    for venv in (".venv", "venv"):
        cand = root / venv / scripts / ("python" + exe)
        if cand.exists():
            return [str(cand)]
    return None


def call(cmd: list[str], root: Path):
    res = run(cmd, timeout=TIMEOUT, cwd=str(root))
    try:
        return res, json.loads(res.stdout) if res.stdout.strip() else {}
    except json.JSONDecodeError:
        return res, None


def failed(name: str, res) -> dict:
    return {"name": name, "status": "FAIL",
            "detail": "\n".join((res.stdout + res.stderr).strip().splitlines()[-15:]) or f"exit {res.returncode}"}


def copy_at(depth: int, location) -> str | None:
    """A copy's path from npm's root, as npm audit gives it. npm outdated gives an absolute path, but masks what looks
    like a secret in it, such as a UUID in a folder's name, as ***: so take the parts below the root's depth."""
    if not location:
        return None
    parts = Path(location).parts
    return "/".join(parts[depth:] if Path(location).is_absolute() else parts)


def npm_deps(root: Path, found: dict, checks: list) -> str | None:
    """Returns npm's root, where its paths start."""
    if not (root / "package.json").exists():
        return
    npm = tool("BUILD_CREW_NPM", "npm")
    if not npm:
        checks += [{"name": n, "status": "SKIPPED", "detail": "npm not installed"} for n in ("npm outdated", "npm audit")]
        return
    # npm works from its own root, which from a workspace's folder is the workspace root above it: its paths start there
    res = run([*npm, "prefix"], timeout=60, cwd=str(root))
    if res.returncode or not res.stdout.strip():
        checks.append(failed("npm prefix", res))
        return
    depth = len(Path(res.stdout.strip().splitlines()[-1]).parts)
    # npm prints a UUID in a folder's name as ***, so take the root from the project's path as npm sees it, cut to that
    # depth. A process sees its folder with symlinks resolved, except on Windows
    here = os.path.abspath(root) if os.name == "nt" else os.path.realpath(root)
    npm_root = str(Path(*Path(here).parts[:depth]))
    res, data = call([*npm, "outdated", "--json", "--long"], root)  # --long adds the path -w takes, dependedByLocation
    # exit 1 only means something is outdated; npm reports its own errors as {"error": ...}
    if res.returncode not in (0, 1) or not isinstance(data, dict) or "error" in data or (res.returncode and not data):
        checks.append(failed("npm outdated", res))
    else:
        entries = [(n, e) for n, d in data.items() for e in (d if isinstance(d, list) else [d])]
        missing = sorted({n for n, e in entries if not e.get("current")})
        if missing:
            checks.append({"name": "npm outdated", "status": "FAIL",
                           "detail": "not installed: " + ", ".join(missing) + " (run `npm ci` first)"})
        else:
            checks.append({"name": "npm outdated", "status": "PASS"})
            for n, e in entries:  # with workspaces, a name is listed for each package that depends on it
                if e.get("latest") and e["latest"] != e["current"]:
                    at = copy_at(depth, e.get("location"))
                    d = found.setdefault(("npm", n, at), {
                        "ecosystem": "npm", "name": n, "current": e["current"], "latest": e["latest"],
                        "bump": bump(e["current"], e["latest"]), **({"location": at} if at else {})})
                    if "dependedByLocation" in e:  # a workspace's path from npm's root, or "" for the root project
                        d["dependents"] = sorted({*d.get("dependents", []), e["dependedByLocation"] or "."})
    res, data = call([*npm, "audit", "--json"], root)
    vulns = data.get("vulnerabilities") if isinstance(data, dict) else None
    if res.returncode not in (0, 1) or not isinstance(vulns, dict) or "error" in data:
        checks.append(failed("npm audit", res))
        return npm_root
    checks.append({"name": "npm audit", "status": "PASS"})
    for n, v in vulns.items():
        for node in v.get("nodes") or [None]:  # each affected copy, also one npm outdated didn't list
            at = copy_at(depth, node)
            d = found.setdefault(("npm", n, at), {"ecosystem": "npm", "name": n, **({"location": at} if at else {})})
            d.update(vulnerable=True, severity=v.get("severity"), direct=bool(v.get("isDirect")),
                     **npm_fix(n, v.get("fixAvailable")),
                     advisories=sorted({a.get("url") or str(a.get("source")) for a in v.get("via", []) if isinstance(a, dict)}))
    return npm_root


def npm_fix(name: str, fa) -> dict:
    """npm's fixAvailable: a version of this package, a version of the one that pulls it in, `npm audit fix`, or none."""
    if isinstance(fa, dict):
        if fa.get("name") == name:
            return {"fix": [fa.get("version")]}
        return {"fix": [], "fix_via": f"{fa.get('name')}@{fa.get('version')}",
                **({"fix_major": True} if fa.get("isSemVerMajor") else {})}
    return {"fix": [], **({"fix_via": "npm audit fix"} if fa else {})}


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def version_says(version: str) -> str:
    """By PEP 440, 1.0b1 and 0.1.dev3 are pre-releases, and 1.0+cu118 is a local version."""
    facts = [fact for fact, hit in (("a pre-release", re.search(r"(a|b|rc|dev)\d", version.split("+")[0])),
                                    ("a local version", "+" in version)) if hit]
    return f" ({', '.join(facts)})" if facts else ""


def venv_tools(root: Path) -> set[str]:
    """pip, setuptools and wheel come with the virtualenv, unless the project's own requirements name them."""
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in root.glob("requirements*.txt")]
    py = root / "pyproject.toml"
    if py.exists():  # leave out [build-system]: what the build needs isn't installed here
        texts.append(re.sub(r"(?ms)^\[build-system\].*?(?=^\[|\Z)", "", py.read_text(encoding="utf-8", errors="replace")))
    declared = "\n".join(texts).lower()
    return {t for t in VENV_TOOLS if not re.search(rf"(?m)(^\s*|[\"']){t}(?![\w.-])", declared)}


def python_deps(root: Path, found: dict, checks: list) -> None:
    if not ((root / "pyproject.toml").exists() or list(root.glob("requirements*.txt")) or (root / "setup.py").exists()):
        return
    py = venv_python(root)
    if not py:
        checks += [{"name": n, "status": "SKIPPED", "detail": "no project virtualenv (.venv or venv)"}
                   for n in ("pip list --outdated", "pip-audit")]
        return
    tools = venv_tools(root)
    # --not-required: what nothing else installed depends on, i.e. what the project asked for. --exclude-editable:
    # the project installed into its own virtualenv (pip install -e ., uv sync) would otherwise "require" all of it
    res, top = call([*py, "-m", "pip", "list", "--not-required", "--exclude-editable", "--format=json"], root)
    res2, data = call([*py, "-m", "pip", "list", "--outdated", "--format=json"], root)
    res3, current = call([*py, "-m", "pip", "list", "--uptodate", "--format=json"], root)
    direct = None
    bad = next((r for r, d in ((res, top), (res2, data), (res3, current)) if r.returncode or not isinstance(d, list)), None)
    if bad:
        checks.append(failed("pip list --outdated", bad))
    else:
        direct = {canon(e["name"]) for e in top} - tools
        # pip lists only what it found a release at or above the installed version of, so each must be in one list or
        # the other. Why one isn't is a guess (an index that's down answers nothing, and pip still exits 0), except for
        # what its version says
        unchecked = sorted(direct - {canon(e["name"]) for e in data + current})
        if unchecked:
            version = {canon(e["name"]): e["version"] for e in top}
            checks.append({"name": "pip list --outdated", "status": "FAIL",
                           "detail": "pip found no release at or above the installed version of " +
                                     ", ".join(f"{n} {version[n]}" + version_says(version[n]) for n in unchecked) +
                                     (", and none of any other package either: is the package index reachable?"
                                      if not (data or current) else
                                      ". Possible causes: it isn't on the package index, an index that has it is "
                                      "unreachable, or the installed version is newer than any release there (from git, "
                                      "a local version or a pre-release)")})
        else:
            checks.append({"name": "pip list --outdated", "status": "PASS"})
        for e in data:
            if canon(e["name"]) in direct:
                found[("python", canon(e["name"]))] = {"ecosystem": "python", "name": e["name"], "current": e["version"],
                                                       "latest": e["latest_version"], "direct": True,
                                                       "bump": bump(e["version"], e["latest_version"])}
    # pip-audit runs from outside the virtualenv: installed inside, it would audit (and upgrade) its own dependencies
    audit = tool("BUILD_CREW_PIP_AUDIT", "pip-audit")
    if not audit:
        checks.append({"name": "pip-audit", "status": "SKIPPED",
                       "detail": "pip-audit not installed (install it outside the project's virtualenv, e.g. with pipx)"})
        return
    site = run([*py, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], timeout=60).stdout.strip()
    if not site:  # an empty --path would audit nothing and look clean
        checks.append({"name": "pip-audit", "status": "FAIL", "detail": "couldn't find the virtualenv's site-packages"})
        return
    res, data = call([*audit, "--path", site, "-f", "json", "--progress-spinner", "off"], root)
    if res.returncode not in (0, 1) or not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        checks.append(failed("pip-audit", res))
        return
    skipped = sorted(e["name"] for e in data["dependencies"] if e.get("skip_reason"))
    if len(skipped) == len(data["dependencies"]) and top:
        checks.append({"name": "pip-audit", "status": "FAIL", "detail": f"pip-audit audited no packages in {site}"})
        return
    checks.append({"name": "pip-audit", "status": "PASS", **({"detail": "skipped " + ", ".join(skipped)} if skipped else {})})
    for e in data["dependencies"]:
        n = canon(e["name"])
        if e.get("vulns") and n not in tools:
            d = found.setdefault(("python", n), {"ecosystem": "python", "name": e["name"], "current": e.get("version")})
            d.update(vulnerable=True, advisories=sorted({v["id"] for v in e["vulns"]}),
                     fix=sorted({f for v in e["vulns"] for f in v.get("fix_versions", [])}, key=version_key),
                     **({"direct": n in direct} if direct is not None else {}))


def deps() -> None:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or git("rev-parse", "--show-toplevel") or os.getcwd())
    checks: list = []
    found: dict = {}
    npm_root = npm_deps(root, found, checks)
    python_deps(root, found, checks)
    ran = [c for c in checks if c["status"] != "SKIPPED"]
    listed = sorted(found.values(), key=lambda d: (not d.get("vulnerable"), d["ecosystem"], d["name"]))
    verdict = ("NOT VERIFIED" if not ran else
               "CHECK FAILED" if any(c["status"] == "FAIL" for c in ran) else
               "VULNERABLE" if any(d.get("vulnerable") for d in listed) else
               "OUTDATED" if listed else
               "PARTLY VERIFIED" if len(ran) < len(checks) else
               "UP TO DATE")
    out = {"gate": "deps", "root": str(root), **({"npm_root": npm_root} if npm_root else {}), "verdict": verdict,
           "checks": checks, "dependencies": listed}
    if not ran:
        out["note"] = "no dependency check ran" + (" (all tools missing)" if checks else " (no npm or Python project)")
    emit(out, verdict in ("OUTDATED", "PARTLY VERIFIED", "UP TO DATE"))


if __name__ == "__main__":
    main_guarded("deps", deps)
