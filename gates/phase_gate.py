#!/usr/bin/env python3
"""phase_gate — reads docs/PLAN.md and enforces the build order.

Usage:
  phase_gate.py next          print the first unchecked phase
  phase_gate.py check <id>    may this phase start? (exists, not done, deps done, clean tree), and its skills
  phase_gate.py verify <id>   run the phase's "Done when" commands
  phase_gate.py done <id>     verify, then tick the phase in PLAN.md

"Done" means the Done-when commands passed when code ran them.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
from _common import emit, git, main_guarded, run  # noqa: E402

try:
    from guard import check as guard_check  # same rules as the PreToolUse hook
except Exception:  # pragma: no cover
    def guard_check(cmd: str):
        return None

PROGRESS_RE = re.compile(r"^- \[( |x|X)\] Phase (\w+) [—-] (.+)$", re.M)
SKILL_RE = re.compile(r"(?:[\w./-]+:)?[a-z0-9][a-z0-9-]*")  # skill, plugin:skill or folder/path:skill


def plan_path() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())) / "docs" / "PLAN.md"


def load() -> tuple[str, dict]:
    p = plan_path()
    if not p.exists():
        emit({"gate": "phase", "error": "docs/PLAN.md not found: run /kickoff first"}, False)
    with open(p, encoding="utf-8", newline="") as f:  # newline="": keep \r\n or \n as the file has it
        text = f.read()
    phases = {m.group(2): {"done": m.group(1) in "xX", "name": m.group(3).strip()}
              for m in PROGRESS_RE.finditer(text)}
    return text, phases


def section(text: str, pid: str) -> str:
    m = re.search(rf"^## Phase {re.escape(pid)} [—-].*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0) if m else ""


def deps(sec: str) -> list[str]:
    m = re.search(r"^\*\*Depends on:\*\*\s*(.+)$", sec, re.M)
    if not m or m.group(1).strip().lower().startswith("none"):
        return []
    line = m.group(1)
    named = re.findall(r"Phase\s+(\w+)", line)
    if named:
        return named
    # bare list like "0, 1 and 2a"; anything else (prose) is ignored rather than guessed
    if re.fullmatch(r"\s*\w+(\s*(,|and|&)\s*\w+)*\s*", line):
        return re.findall(r"\b(\d+[a-z]?)\b", line)
    return []


def done_when(sec: str) -> list[str]:
    m = re.search(r"^### Done when\s*$(.*?)(?=^### |\Z)", sec, re.M | re.S)
    return re.findall(r"^\s*- (?:\[[ x]\] )?`([^`]+)`", m.group(1), re.M) if m else []


def skills(sec: str) -> list[str]:
    m = re.search(r"^\*\*Skills:\*\*[ \t]*(.*)$", sec, re.M)
    return re.findall(r"`([^`]+)`", m.group(1)) if m else []


def cmd_check(pid: str, text: str, phases: dict) -> None:
    out = {"gate": "phase", "action": "check", "phase": pid}
    if pid not in phases:
        out["error"] = f"Phase {pid} not in PLAN.md Progress list"
        emit(out, False)
    if phases[pid]["done"]:
        out["error"] = f"Phase {pid} is already done"
        emit(out, False)
    missing = [d for d in deps(section(text, pid)) if not phases.get(d, {}).get("done")]
    if missing:
        out["error"] = "dependencies not done: " + ", ".join(f"Phase {d}" for d in missing)
        emit(out, False)
    if len(re.findall(r"^\*\*Skills:\*\*", section(text, pid), re.M)) > 1:
        out["error"] = "more than one **Skills:** line: list the phase's skills on one line"
        emit(out, False)
    bad = [s for s in skills(section(text, pid)) if not SKILL_RE.fullmatch(s)]
    if bad:
        out["error"] = "not skill names on the **Skills:** line: " + ", ".join(f"`{s}`" for s in bad)
        emit(out, False)
    dirty = git("status", "--porcelain")
    if dirty:
        out["error"] = "working tree not clean: commit or stash first"
        out["dirty"] = dirty.splitlines()[:20]
        emit(out, False)
    out["name"] = phases[pid]["name"]
    out["done_when"] = done_when(section(text, pid))
    out["skills"] = skills(section(text, pid))
    emit(out, True)


def verify(pid: str, text: str) -> dict:
    cmds = done_when(section(text, pid))
    results = []
    for c in cmds:
        blocked = guard_check(c)
        if blocked:
            results.append({"cmd": c, "status": "BLOCKED", "detail": blocked})
            continue
        res = run(c, timeout=int(os.environ.get("BUILD_CREW_DONE_WHEN_TIMEOUT", "900")), cwd=str(plan_path().parent.parent))
        results.append({"cmd": c, "status": "PASS" if res.returncode == 0 else "FAIL",
                        **({"output_tail": "\n".join((res.stdout + res.stderr).splitlines()[-20:])}
                           if res.returncode else {})})
    return {"commands": results, "ok": bool(cmds) and all(r["status"] == "PASS" for r in results),
            **({} if cmds else {"error": "phase has no `Done when` commands: cannot verify"})}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("next", "check", "verify", "done"):
        print(__doc__)
        sys.exit(2)
    action = sys.argv[1]
    text, phases = load()
    if action == "next":
        nxt = next((pid for pid, p in phases.items() if not p["done"]), None)
        emit({"gate": "phase", "action": "next", "phase": nxt,
              "name": phases[nxt]["name"] if nxt else None,
              **({} if nxt else {"note": "all phases done"})}, nxt is not None)
    if len(sys.argv) < 3:
        emit({"gate": "phase", "error": f"usage: phase_gate.py {action} <id>"}, False)
    pid = sys.argv[2]
    if action == "check":
        cmd_check(pid, text, phases)
    if pid not in phases:
        emit({"gate": "phase", "error": f"Phase {pid} not in PLAN.md"}, False)
    v = verify(pid, text)
    out = {"gate": "phase", "action": action, "phase": pid, **v}
    if action == "verify" or not v["ok"]:
        emit(out, v["ok"])
    # done: tick the box
    new = re.sub(rf"^- \[ \] Phase {re.escape(pid)} ([—-])", rf"- [x] Phase {pid} \1", text, count=1, flags=re.M)
    with open(plan_path(), "w", encoding="utf-8", newline="") as f:  # ticking a box is a one-line diff
        f.write(new)
    out["marked_done"] = True
    emit(out, True)


if __name__ == "__main__":
    main_guarded("phase", main)
