"""Compare hooks/guard.py with another version of it, to review a guard change:

    python tests/guard_diff.py [ref]        # ref defaults to origin/main

Both versions read every case in test_guard.py, and commands built from dangerous ones: wrapped, piped, substituted,
grouped, quoted, on another line, and twice over. It lists each command the other version blocks and this one allows,
and each this one crashes on, and exits 1 if there's any. Each allowed one is a bypass, or a command the other version
wrongly blocked, which the PR says. A crash blocks, so it doesn't show as a bypass, but it refuses harmless commands.
Nothing is run: the guard only reads the commands."""
from __future__ import annotations

import importlib.util
import itertools
import os
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
DANGER = ["git push --force", "git push origin main", "git commit --no-verify -m x", "HUSKY=0 git commit -m x",
          "git reset --hard", "git clean -fd", "git add -A", "git checkout .", "git branch -D main", "rm -rf ~",
          "gh pr merge 1", "gh api -X PUT repos/o/r/pulls/1/merge", "git config core.hooksPath /dev/null",
          "git -c alias.p='push --force' p", "echo 'git push --force' | bash"]
WRAPS = ["@@", "x; @@", "x && @@", "x || @@", "x | @@", "@@ &", "(@@)", "{ @@; }", "echo $(@@)", "echo `@@`",
         'echo "$(@@)"', "diff <(@@) f", "bash -c '@@'", 'sh -c "@@"', "eval '@@'", "env @@", "sudo -u x @@",
         "command @@", "nice -n 5 @@", "timeout 60 @@", "if x; then @@; fi", "for i in 1; do @@; done", "a=1 @@",
         "@@ 2>/dev/null", "@@ >/dev/null 2>&1", "cat <<EOF\nbody\nEOF\n@@", "cat <<'EOF'\n$(x)\nEOF\n@@",
         "# x\n@@", "x\n@@", ": $((1 << 2)) ${a[1]}\n@@", "((x<<y)); @@", "x | tee >(@@)", "env -i @@",
         "x | xargs -n1 @@", "x | xargs -iI @@", "sudo --login @@", "timeout 1e3 @@"]
PS_DANGER = ["git push --force", "git push origin ('--force')", "git commit -n -m x", "Remove-Item -Recurse C:\\",
             "$env:HUSKY = 0", "Set-Item env:HUSKY 0", "gh pr merge 1", "git reset --hard", "rm -r -fo $HOME"]
PS_WRAPS = ["@@", "x; @@", "x | @@", "$(@@)", "@(@@)", "& { @@ }", "if ($x) { @@ }", "try { @@ } catch { }",
            "foreach ($i in 1) { @@ }", "Write-Output (@@)", "$y = @@", "@@ 2>$null", "pwsh -c '@@'", "x\n@@",
            "$(if ($x) {'a'; } @@)"]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.current_branch = lambda: "feat/x"  # as test_guard.py's feature_repo, without asking git each time
    guard.local_branch_exists = lambda name: False
    return guard


def decide(guard, cmd: str, shell: str) -> str | None:
    """What the hook does with cmd: a reason to block it, "ask: " and a reason to ask the user, or None. A crash
    blocks, as in main()."""
    guard.STARTED = time.monotonic()
    try:
        if len(cmd) > guard.MAX_COMMAND:
            return "Command too long to verify."
        reason = guard.check(cmd, shell=shell)
        if reason and hasattr(guard, "PROTECT"):  # a version that asks before a protected branch changes
            guard.PROTECT = False
            try:
                reason = guard.check(cmd, shell=shell) or f"ask: {reason}"
            finally:
                guard.PROTECT = True
        return reason
    except Exception as e:
        return f"crash: {type(e).__name__}: {e}"


def corpus() -> list[tuple[str, str]]:
    sys.path.insert(0, TESTS)
    import test_guard as t
    cases = [("bash", c) for c in t.BLOCKED + t.ASKED + t.ALLOWED + t.UNPARSEABLE + list(t.OWN_PLACEHOLDERS)]
    cases += [("powershell", c) for c in t.PS_BLOCKED + t.PS_ASKED + t.PS_ALLOWED + t.PS_UNPARSEABLE]
    cases += [("bash", c) for c in wrapped(WRAPS, DANGER)]
    cases += [("powershell", c) for c in wrapped(PS_WRAPS, PS_DANGER)]
    return list(dict.fromkeys(cases))


def wrapped(wraps: list[str], danger: list[str]) -> list[str]:
    """Each dangerous command in each wrap, inside each wrap. @@ marks where it goes."""
    return [a.replace("@@", b.replace("@@", d)) for a, b, d in itertools.product(wraps, wraps, danger)]


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    source = subprocess.run(["git", "show", f"{ref}:hooks/guard.py"], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8")
    if source.returncode != 0:
        print(source.stderr.strip())
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "guard.py")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(source.stdout)
        base = load("guard_base", path)
    head = load("guard_head", os.path.join(ROOT, "hooks", "guard.py"))
    cases = corpus()
    allowed, asked, crashed, blocked = [], [], [], 0
    for shell, cmd in cases:
        was, now = decide(base, cmd, shell), decide(head, cmd, shell)
        if was and not now:
            allowed.append((shell, cmd, f"{ref} blocks it: {was}"))
        elif (now or "").startswith("ask:") and was and not was.startswith("ask:"):
            asked.append((shell, cmd, f"{ref} blocks it: {was}"))
        elif (now or "").startswith("crash:") and not (was or "").startswith("crash:"):
            crashed.append((shell, cmd, now))
        blocked += bool(now and not was)
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    for shell, cmd, why in allowed + [(s, c, "asked about now; " + w) for s, c, w in asked] + crashed:
        print(f"[{shell}] {cmd!r}\n    {why}")
    print(f"{len(cases)} commands: {len(allowed)} that {ref} blocks are allowed now, {len(asked)} are asked about now, "
          f"{blocked} that it allows are refused now, {len(crashed)} that crash the guard now.")
    return 1 if allowed or asked or crashed else 0


if __name__ == "__main__":
    sys.exit(main())
