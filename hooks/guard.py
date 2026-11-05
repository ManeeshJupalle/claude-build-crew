#!/usr/bin/env python3
"""guard: PreToolUse hook that blocks destructive git / gh / rm commands from the Bash and PowerShell tools.

It parses the command into shell words instead of pattern-matching raw text, so
  - quoted text such as commit messages can't trigger it ("docs: explain --no-verify"),
  - git global options can't hide it (git -C . push --force),
  - chained, grouped, nested and substituted commands are checked
    (&&, ;, |, if/then, { }, bash -c, pwsh -c, cmd /c, $(...)).
PowerShell commands get their own tokenizer, since its quoting and escapes differ from Bash.

Exit 2 = block (stderr is shown to Claude). Exit 0 = allow.
This is a seatbelt, not a sandbox: see "Limits" in the README.
"""
from __future__ import annotations

import bisect
import json
import re
import shlex
import subprocess
import sys
import time

PROTECTED = re.compile(r"^(main|master|develop|release/.+)$")
SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "|&", ";;"}
WRAPPERS = {"sudo", "command", "exec", "time", "nice", "ionice", "nohup", "env", "xargs", "timeout",
            "stdbuf", "caffeinate", "chronic"}
WRAPPER_VALUE_FLAGS = {"-u", "-g", "-n", "-s", "-k", "-c", "-p", "-o", "-e", "-i", "--signal", "--kill-after", "--user"}
# git config from the environment, in any case: Windows names ignore case, so git_config_parameters works there.
# Not HUSKY or SKIP: lowercase skip=1 is an everyday shell variable.
HOOK_SKIP_ENV = re.compile(r"^(HUSKY=0|HUSKY_SKIP_HOOKS=|HUSKY_SKIP_INSTALL=|SKIP=|PRE_COMMIT_ALLOW_NO_CONFIG=|GIT_HOOKS_PATH="
                           r"|(?i:GIT_CONFIG_PARAMETERS=|GIT_CONFIG_COUNT=|GIT_CONFIG_KEY_\d+=))")
# git's global options that take the next word as their value
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source",
                     "--shallow-file", "--super-prefix"}
CONFIG_READ_WORDS = {"--local", "--global", "--system", "--worktree", "--show-origin", "--show-scope", "-z", "--null",
                     "--get", "--get-all", "get", "--all"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "export"}  # what follows is a command
PLACEHOLDERS = {c: chr(0xE000 + n) for n, c in enumerate(";&|\n(){}#<")}  # stand-ins for quoted separators
RESTORE = {ord(v): k for k, v in PLACEHOLDERS.items()}
COMMENT_START = set(" \t\n;&|()")  # a # right after one of these starts a comment
SUBST = ""  # stands for a $(...) or `...` substitution, or a PowerShell group, checked on its own
POWERSHELLS = {"pwsh", "powershell"}
PS_VALUE_PARAMS = ("executionpolicy", "windowstyle", "inputformat", "outputformat", "version", "psconsolefile",
                   "configurationname", "configurationfile", "workingdirectory", "settingsfile", "custompipename")
PS_VALUE_ALIASES = {"ep", "ex", "w", "wd", "o", "of", "i", "if", "v"}
DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "$HOME/*",
                        ".", "./", "./*", "..", "../", "*", ".*"}
WINDOWS_ROOTS = re.compile(r"([a-z]:|\$env:(userprofile|home))(/\*?)?", re.I)  # C:\, $env:USERPROFILE\*
RM_FLAGS = set("dfiIPRrvWx")  # rm's short options, GNU and BSD: with any other letter (-Force), rm fails
PS_REMOVE = {"remove-item", "ri", "rm", "rmdir", "rd", "del", "erase"}
PS_SET_ITEM = {"set-item", "si", "set-content", "sc", "new-item", "ni"}
PS_QUOTES = {"'": "'", "‘": "'", "’": "'", "‚": "'", "‛": "'", '"': '"', "“": '"', "”": '"', "„": '"'}
PS_HERESTRING = re.compile(r"@(['\"])[ \t]*\r?\n")  # opens @'...'@ or @"..."@, which closes at a line starting '@
# $out, $script:x, ${out}, $x[0], [string[]]$x. No two parts can match the same text, so no slow backtracking
PS_VAR = r"(?:\[[\w.]+(?:\[[\w.,]*\])*\])*(?:\$\{[^}]+\}|\$[\w:.]+)(?:\[[^\]]*\])*"
# $x = , $a = $b = , $x, $y = , +=, ??= ... over a command's words joined with NUL
PS_ASSIGNMENTS = re.compile(rf"(?:{PS_VAR}(?:\0{PS_VAR})*\0?(?:[-+*/%]|\?\?)?=(?!=)\0?)+")
SEP, PIPE, OPEN, CLOSE = object(), object(), object(), object()  # PowerShell tokens: ; & newline, |, ( {, ) }
READS_STDIN = "A shell reading its commands from stdin (a pipe, a heredoc or -) can't be verified."
# Claude Code runs the command if this hook times out (60s), so a command too long or too slow to check is blocked.
# Slow ones: thousands of git commits or switches, each asking git about branches, or a long chain of pwsh -wd /c,
# each read both ways
MAX_COMMAND = 100_000  # characters
TIME_LIMIT = 10  # seconds
STARTED = time.monotonic()


def check_time() -> None:
    if time.monotonic() - STARTED > TIME_LIMIT:
        raise TimeoutError("Checking this command took too long, so it can't be verified.")


def current_branch() -> str:
    check_time()
    try:
        return subprocess.run(["git", "branch", "--show-current"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def is_protected_ref(ref: str, branch: str) -> bool:
    ref = ref.lstrip("+")
    dst = ref.split(":", 1)[1] if ":" in ref else ref
    dst = dst.removeprefix("refs/heads/")
    if dst in ("HEAD", "@"):
        dst = branch
    return bool(PROTECTED.match(dst or "x"))


def local_branch_exists(name: str) -> bool:
    check_time()  # git switch with thousands of names would ask git about each
    try:
        return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])\1")


def strip_heredocs(cmd: str) -> str:
    """Heredoc bodies are data (commit messages, PR bodies), not commands: drop them, keeping the line each starts on.
    A heredoc with no closing line is left alone. Each closing line is looked up rather than searched for, since a
    regex search per heredoc takes quadratic time on a long command, and the hook can't stop a regex at its limit."""
    closers, pos = {}, 0  # a line's text -> where lines with that text end
    for line in cmd.split("\n"):
        pos += len(line)
        closers.setdefault(line.strip(" \t"), []).append(pos)
        pos += 1
    out, i = [], 0
    for m in HEREDOC.finditer(cmd):
        body = cmd.find("\n", m.end()) + 1
        if m.start() < i or not body:
            continue
        ends = closers.get(m.group(2), [])
        k = bisect.bisect_left(ends, body)
        if k < len(ends):
            out.append(cmd[i:body - 1])
            i = ends[k]
    return "".join(out) + cmd[i:]


def ps_herestring(cmd: str, i: int, closers: dict) -> tuple[str, str, int] | None:
    """The quote, text and end of a here-string opening at i (@'...'@ or @"..."@), or None. Like strip_heredocs(),
    in linear time: closers holds where the next closing '@ and "@ are, and later here-strings reuse it."""
    m = PS_HERESTRING.match(cmd, i)
    if not m:
        return None
    q, close = m.group(1), closers.get(m.group(1))
    if close is None or 0 <= close < m.end() - 1:
        close = closers[q] = cmd.find("\n" + q + "@", m.end() - 1)
    return (q, cmd[m.end():close].removesuffix("\r"), close + 3) if close >= 0 else None


def prog_name(tok: str) -> str:
    """'/usr/bin/git' -> 'git'. Windows and macOS file systems ignore case, and Windows adds .exe: 'GIT.exe' -> 'git'."""
    name = re.split(r"[\\/]", tok)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def short_flags(tok: str) -> str:
    """'-fu' -> 'fu'; '--force' -> ''."""
    return tok[1:] if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1 else ""


def long_opt(tok: str, *names: str) -> bool:
    """git takes any unique prefix of a long option (--no-verif is --no-verify), so a prefix counts as the option."""
    tok = tok.split("=", 1)[0]
    return len(tok) > 2 and tok.startswith("--") and any(n.startswith(tok) for n in names)


def closing(cmd: str, i: int) -> int:
    """Index just past the ) or ` that closes the $( or ` at i, or -1."""
    if cmd[i] == "`":
        j = i + 1
        while j < len(cmd):
            if cmd[j] == "\\":
                j += 2
                continue
            if cmd[j] == "`":
                return j + 1
            j += 1
        return -1
    j, depth, quote = i + 2, 1, None
    while j < len(cmd):
        c = cmd[j]
        if c == "\\" and quote != "'":
            j += 2
            continue
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def prescan(cmd: str) -> tuple[str, list[str]]:
    """shlex returns a quoted ';' the same as a bare one, and splits $(...) into words. So before tokenizing, swap
    quoted or escaped separators for placeholders, and turn each $(...) and `...` into one word, returning the
    commands inside to be checked on their own."""
    out, inner, quote, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and quote != "'" and i + 1 < len(cmd):
            nxt = cmd[i + 1]
            if nxt != "\n":  # backslash-newline continues the line
                out.append("\\" + PLACEHOLDERS.get(nxt, nxt) if quote else PLACEHOLDERS.get(nxt, "\\" + nxt))
            i += 2
        elif quote != "'" and cmd.startswith("$$", i):  # the shell's PID: $$'x' is $$ and then 'x'
            out.append("$$")
            i += 2
        elif quote != "'" and (c == "`" or cmd.startswith("$(", i)):
            end = closing(cmd, i)
            if end < 0:
                raise ValueError("unbalanced substitution")
            # inside `...`, \` \$ and \\ stand for the plain characters, so `echo \`git push\`` nests
            inner.append(re.sub(r"\\([`$\\])", r"\1", cmd[i + 1:end - 1]) if c == "`" else cmd[i + 2:end - 1])
            out.append(SUBST)
            i = end
        elif quote is None and cmd.startswith("$'", i):  # $'...' quotes like '...', but \' is a quote inside it
            j, text = i + 2, []
            while j < len(cmd) and cmd[j] != "'":
                j += cmd[j] == "\\"
                text.append(PLACEHOLDERS.get(cmd[j:j + 1], cmd[j:j + 1]))
                j += 1
            if j >= len(cmd):
                raise ValueError("unbalanced quotes")
            out.append("'" + "".join(text).replace("'", "'\"'\"'") + "'")
            i = j + 1
        elif c == "#" and quote is None and (not out or out[-1] in COMMENT_START):  # a comment runs to the line's end
            j = cmd.find("\n", i)
            i = j if j >= 0 else len(cmd)
        elif c in "'\"" and quote in (None, c):
            quote = None if quote else c
            out.append(c)
            i += 1
        else:
            out.append(PLACEHOLDERS[c] if quote and c in PLACEHOLDERS else c)
            i += 1
    return "".join(out), inner


def tokenize(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|\n()")
    lex.whitespace_split = True
    lex.whitespace = " \t\r"  # keep newlines as separators
    lex.commenters = ""         # prescan() already dropped comments
    return list(lex)


def segments(tokens: list[str], braces: bool) -> list[tuple[list[str], bool]]:
    """Each command, and whether a pipe feeds it."""
    seps = set(";&|\n(){}") if braces else set(";&|\n()")
    segs, cur, piped = [], [], False
    for t in tokens:
        if t in SEPARATORS or (t and set(t) <= seps):  # an empty argument ('') is not a separator
            if cur:
                segs.append((cur, piped))
            piped = t.count("|") == 1 or (piped and not cur)  # | and |& pipe, || doesn't; x | { bash; } still pipes
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append((cur, piped))
    return segs


def strip_prefix(seg: list[str]) -> list[str]:
    """Drop env assignments and wrappers: `FOO=1 timeout 60 nice -n 5 git ...` -> `git ...`."""
    i, in_wrapper = 0, False
    while i < len(seg):
        t = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or (t in KEYWORDS and not in_wrapper):
            i += 1
        elif prog_name(t) in WRAPPERS:
            i, in_wrapper = i + 1, True
        elif in_wrapper and t in WRAPPER_VALUE_FLAGS:
            i += 2
        elif in_wrapper and (t.startswith("-") or re.match(r"^[\d.]+[smhd]?$", t)):
            i += 1
        else:
            break
    return seg[i:]


def drop_stdin_redirects(seg: list[str]) -> tuple[list[str], bool]:
    """Drop <<EOF, < file and <<< text from a command, and say whether it had any. A quoted < is a placeholder."""
    words, found, k = [], False, 0
    while k < len(seg):
        if seg[k][:1] == "<":
            found = True
            k += 2 if not seg[k].strip("<-") else 1  # a bare < or <<< takes the next word
        else:
            words.append(seg[k])
            k += 1
    return words, found


def shell_script(args: list[str]) -> str | None:
    """The script file bash, sh and the like run, or None when they read their commands from stdin (none, or -s)."""
    k = 0
    while k < len(args) and args[k][:1] in ("-", "+"):
        if "s" in short_flags(args[k]):
            return None
        k += 2 if args[k] in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file") else 1
    return args[k] if k < len(args) else None


def check_git(args: list[str], state: dict) -> str | None:
    # global options before the subcommand
    i = 0
    while i < len(args) and args[i].startswith("-"):
        opt, value = args[i].split("=", 1) if args[i].startswith("--config-env=") else (args[i], None)
        setting = (value or (args[i + 1] if i + 1 < len(args) else "")).lower() if opt in ("-c", "--config-env") else ""
        if setting.startswith("core.hookspath"):
            return "Overriding core.hooksPath skips git hooks; forbidden."
        if setting.startswith("alias."):  # git -c alias.p='push --force' p
            return "Defining a git alias can hide what a command runs; forbidden."
        if opt in GIT_VALUE_OPTIONS and value is None:
            i += 2
            continue
        i += 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sub):  # an option value taken for the subcommand
        return f"Could not tell which git command this runs ('{sub[:40]}'), so it can't be verified."

    if sub == "push":
        positionals = [a for a in rest if not a.startswith("-")]
        for a in rest:
            if a.startswith("--force") or long_opt(a, "--force", "--mirror") or "f" in short_flags(a):
                return "Force push is forbidden."
            if long_opt(a, "--no-verify"):
                return "Skipping git hooks (--no-verify) is forbidden."
            if long_opt(a, "--all", "--branches", "--delete", "--prune") or "d" in short_flags(a):
                return f"git push {a} is forbidden; push one feature branch."
        refspecs = positionals[1:]
        branch = state.get("branch") or current_branch()
        for r in refspecs:
            if r.startswith("+"):
                return "Force push (+refspec) is forbidden."
            if r.startswith(":"):
                return f"git push {r} deletes a remote branch; forbidden. Push one feature branch."
            if "*" in r:  # refs/heads/*:refs/heads/* pushes main too
                return f"git push {r} pushes many branches; forbidden. Push one feature branch."
            if is_protected_ref(r, branch):
                return "Pushing to a protected branch is forbidden. Open a PR."
        if not refspecs and PROTECTED.match(branch or "x"):
            return f"You are on protected branch '{branch}'. Create a feature branch first."
        return None

    if sub == "config":  # reading core.hooksPath or an alias is fine; setting or unsetting one isn't
        words = [a.lower() for a in rest]
        key = next((w for w in words if w == "core.hookspath" or w.startswith("alias.")), None)
        # a read names the key last: after a key, git takes even "--local" as the value to set
        if key and not (words[-1] == key and all(w in CONFIG_READ_WORDS for w in words[:-1])):
            return ("Changing core.hooksPath skips git hooks; forbidden." if key == "core.hookspath" else
                    "Defining a git alias can hide what a command runs; forbidden.")
        if "core" in words and any(w in ("remove-section", "rename-section") or long_opt(w, "--remove-section",
                                                                                         "--rename-section") for w in words):
            return "Removing or renaming git's core section drops core.hooksPath; forbidden."
        return None

    if sub == "commit":
        j = 0
        while j < len(rest):
            a = rest[j]
            if long_opt(a, "--no-verify"):
                return "Skipping git hooks (--no-verify) is forbidden."
            if a in ("-m", "-F", "-C", "-c", "-t", "--message", "--file", "--author", "--date",
                     "--template", "--fixup", "--squash", "--reuse-message", "--reedit-message"):
                j += 2
                continue
            for ch in short_flags(a):
                if ch in "mFCct":  # rest of the cluster is a value
                    break
                if ch == "n":
                    return "Skipping git hooks (-n / --no-verify) is forbidden."
            j += 1
        branch = state.get("branch") or current_branch()
        if PROTECTED.match(branch or "x"):
            return f"You are on protected branch '{branch}'. Create a feature branch first."
        return None

    if sub in ("switch", "checkout"):
        if sub == "checkout" and ("." in rest or ":/" in rest):
            return "Discarding all working-tree changes is forbidden."
        for k, a in enumerate(rest):
            if a == "--":
                break  # checkout -- <files>: not a branch change
            if a in ("-c", "-C", "-b", "-B", "--create") and k + 1 < len(rest):
                state["branch"] = rest[k + 1]
                break
            if not a.startswith("-") and (PROTECTED.match(a) or local_branch_exists(a)):
                state["branch"] = a
                break
        return None

    if sub in ("merge", "cherry-pick", "revert", "am"):
        branch = state.get("branch") or current_branch()
        if PROTECTED.match(branch or "x") and not any(a in ("--abort", "--quit") for a in rest):
            return f"git {sub} on protected branch '{branch}' is forbidden. Do it on a feature branch and open a PR."
        return None

    if sub == "reset" and any(long_opt(a, "--hard") for a in rest):
        return "git reset --hard discards work; forbidden."
    if sub == "clean" and any(long_opt(a, "--force") or "f" in short_flags(a) for a in rest):
        return "git clean -f deletes files; forbidden."
    if sub == "restore" and ("." in rest or ":/" in rest):
        staged_only = any(a in ("--staged", "-S") for a in rest) and not any(a in ("--worktree", "-W") for a in rest)
        if not staged_only:
            return "Discarding all working-tree changes is forbidden (unstaging with --staged is fine)."
    if sub == "branch" and any(a in ("-d", "-D") or long_opt(a, "--delete") for a in rest):
        if any(PROTECTED.match(a) for a in rest if not a.startswith("-")):
            return "Deleting a protected branch is forbidden."
    if sub == "add" and any(a in ("-A", ".", ":/", "-u") or long_opt(a, "--all", "--update") for a in rest):
        return "Stage files by explicit path, not 'git add -A' / 'git add .'."
    return None


def gh_api_method(args: list[str]) -> str:
    """The HTTP method gh api sends: -X/--method (also -XDELETE, -iXDELETE, --method=DELETE), else POST when there
    are fields or input (-f, -F, also -fname=x), else GET."""
    method, fields = None, False
    for k, a in enumerate(args):
        if a in ("--method", "-X") and k + 1 < len(args):
            method = args[k + 1]
        elif a.startswith("--method="):
            method = a.split("=", 1)[1]
        elif a in ("--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=", "--input=")):
            fields = True
        elif a.startswith("-") and not a.startswith("--"):
            for n, c in enumerate(a[1:], 1):  # a cluster: -i takes no value; the first flag that does ends it
                if c == "X":
                    method = a[n + 1:].lstrip("=") or (args[k + 1] if k + 1 < len(args) else "")
                elif c in "fF":
                    fields = True
                if c != "i":
                    break
    return (method or ("POST" if fields else "GET")).upper()


def check_gh(args: list[str]) -> str | None:
    if args[:2] == ["pr", "merge"]:
        return "Merging is done by code, not by an agent: run gates/merge_gate.py <PR> --merge."
    if args[:1] == ["repo"] and len(args) > 1 and args[1] in ("delete", "archive", "rename", "edit"):
        return f"gh repo {args[1]} is forbidden."
    if args[:1] == ["api"]:
        joined = " ".join(args)
        if (re.search(r"pulls/\d+/merge\b", joined) or re.search(r"/merges\b", joined)
                or re.search(r"(mergePullRequest|enablePullRequestAutoMerge|mergeBranch)", joined)):
            return "Merging through the API is forbidden: use gates/merge_gate.py."
        if re.search(r"\b(createRef|updateRefs?|deleteRef|(create|update|delete)BranchProtectionRule)\b", joined):
            return "Changing branches, refs or protection rules through the API is forbidden."
        if ("graphql" not in args and gh_api_method(args) != "GET"  # GraphQL is always POST: its mutations count
                and re.search(r"(branches|protection|rulesets|git/refs)", joined)):
            return "Changing branches, refs or protection rules through the API is forbidden."
    return None


def check_rm(args: list[str]) -> str | None:
    recursive = any(a == "--recursive" or set(short_flags(a)) <= RM_FLAGS and set("rR") & set(short_flags(a))
                    for a in args)
    if not recursive:
        return None
    targets = [a for a in args if not a.startswith("-") or a == "-"]
    for t in targets:
        if dangerous_target(t):
            return f"Recursive delete of '{t}' is forbidden."
    return None


def dangerous_target(t: str) -> bool:
    t = t.replace("\\", "/")
    return (t.rstrip("/") in {x.rstrip("/") for x in DANGEROUS_RM_TARGETS} or t in DANGEROUS_RM_TARGETS
            or bool(WINDOWS_ROOTS.fullmatch(t)))


def check_remove_item(args: list[str]) -> str | None:
    """PowerShell's Remove-Item and its aliases. Parameters ignore case and can be shortened (-r, -Rec)."""
    if not any(len(a) > 1 and a[0] == "-" and a[1] != ":" and "recurse".startswith(a[1:].split(":")[0].lower())
               for a in args):
        return None
    for t in args:
        if not t.startswith("-") and dangerous_target(t):
            return f"Recursive delete of '{t}' is forbidden."
    return None


def ps_param(arg: str) -> str:
    """What a pwsh / powershell parameter does: encoded, command, file, value (takes one) or switch."""
    name = arg.lstrip("-/").lower()
    if name in ("e", "ec") or (len(name) > 1 and "encodedcommand".startswith(name)):
        return "encoded"
    if name in ("c", "cwa") or (len(name) > 1 and "commandwithargs".startswith(name)):
        return "command"
    if name == "f" or (len(name) > 1 and "file".startswith(name)):
        return "file"
    if name in PS_VALUE_ALIASES or (len(name) > 1 and any(p.startswith(name) for p in PS_VALUE_PARAMS)):
        return "value"
    return "switch"


def check_powershell(args: list[str], state: dict, depth: int, stdin: bool = False) -> str | None:
    """PowerShell runs everything after -Command as one command, and Windows PowerShell does the same with a bare
    first argument. Parameters start with - or /, ignore case and can be shortened (-c, -co, /enc).
    -EncodedCommand is base64, so it can't be checked. After -File come the script's own arguments.
    With no command at all, it runs what's piped to it."""
    k = 0
    while k < len(args) and args[k][:1] in ("-", "/"):
        if args[k] == "-":
            return READS_STDIN
        kind = ps_param(args[k])
        if kind == "encoded":
            return "PowerShell -EncodedCommand can't be verified."
        if kind == "command":
            k += 1
            break
        if kind == "file":
            return READS_STDIN if args[k + 1:k + 2] == ["-"] else None
        nxt = args[k + 1] if kind == "value" and k + 1 < len(args) else "-"
        other = ps_param(nxt) if len(nxt) > 1 and nxt[0] in "-/" else ""
        if other in ("encoded", "command", "file"):
            # A value that looks like a parameter can be either. /c is a value (a Git Bash path: -wd /c) or -Command,
            # and -c after -i is -Command in pwsh 7, where -i is the -Interactive switch. So check both readings, and
            # keep the branch changes of the likelier one
            reason = check_powershell(args[k + 1:], state if nxt[0] == "-" else dict(state), depth, stdin)
            if reason:
                return reason
            state = dict(state) if nxt[0] == "-" else state
            k += 2
            continue
        # otherwise a value never starts with -: -NoProfile after -ep is a parameter
        k += 1 if nxt[:1] == "-" else 2
    if args[k:k + 1] == ["-"]:
        return READS_STDIN
    if k < len(args):
        return check(" ".join(args[k:]), depth + 1, state, "powershell")
    return READS_STDIN if stdin else None


def check_segment(seg: list[str], state: dict, depth: int, stdin: bool = False) -> str | None:
    check_time()
    rest = strip_prefix(seg)
    for t in seg[:len(seg) - len(rest)]:  # assignments can follow keywords and wrappers too: env HUSKY=0 git ...
        if HOOK_SKIP_ENV.match(t):
            return f"Skipping git hooks via {t.split('=')[0]} is forbidden."
    seg = rest
    if not seg:
        return None
    prog = prog_name(seg[0])
    args = seg[1:]
    if prog in SHELLS:
        for k, a in enumerate(args):
            if (a in ("-c", "--command") or "c" in short_flags(a)) and k + 1 < len(args):
                return check(args[k + 1], depth + 1, state)
        if stdin and not shell_script(args):  # echo 'git push --force' | bash, bash <<'EOF'
            return READS_STDIN
    if prog in POWERSHELLS:
        return check_powershell(args, state, depth, stdin)
    if prog == "cmd":
        for k, a in enumerate(args):
            m = re.match(r"/+[ckr]", a, re.I)  # Git Bash turns /c into a path, so it's written //c there; /cgit runs too
            if m:
                return check(" ".join([a[m.end():], *args[k + 1:]]), depth + 1, state)
    if prog == "eval":
        return check(" ".join(args), depth + 1, state)
    if prog == "git":
        return check_git(args, state)
    if prog == "gh":
        return check_gh(args)
    if prog == "rm":
        return check_rm(args)
    return None


class Quoted(str):
    """A PowerShell word that starts with a quote: first in ( ), it makes a value ('--force'), not a command."""


def ps_subexpr_end(cmd: str, i: int) -> int:
    """Index just past the ) that closes the $( at i, or -1. Quotes and backtick escapes inside are skipped."""
    j, depth = i + 2, 1
    while j < len(cmd):
        c = cmd[j]
        if c == "`":
            j += 2
            continue
        if c in PS_QUOTES:
            q, j = PS_QUOTES[c], j + 1
            while j < len(cmd) and PS_QUOTES.get(cmd[j]) != q:
                j += 2 if q == '"' and cmd[j] == "`" else 1
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def ps_subexprs(text: str) -> list[str]:
    """The commands inside each $(...) of an expandable string's text."""
    found, i = [], 0
    while i < len(text):
        if text[i] == "`":
            i += 2
        elif text.startswith("$(", i):
            end = ps_subexpr_end(text, i)
            if end < 0:
                raise ValueError("unbalanced subexpression")
            found.append(text[i + 2:end - 1])
            i = end
        else:
            i += 1
    return found


def ps_tokenize(cmd: str) -> tuple[list, list[str]]:
    """PowerShell words, with SEP, PIPE, OPEN and CLOSE for separators, pipes and brackets, and the commands inside
    $(...) in "..." and @"..."@, which run. Unlike the Bash path, quoting is tracked, so a quoted ';' is a word.
    Backtick escapes (also inside "..."), '' is a quote inside '...', a backslash is an ordinary character, a
    backtick at the end of a line continues it, and a here-string is one word."""
    out, inner, word, quoted, lead, i, closers = [], [], [], False, False, 0, {}

    def end_word():
        nonlocal word, quoted, lead
        if word or quoted:
            out.append(Quoted("".join(word)) if lead else "".join(word))
        word, quoted, lead = [], False, False

    while i < len(cmd):
        c = cmd[i]
        if c == "`":
            nxt = cmd[i + 1:i + 2]
            if not nxt:
                raise ValueError("trailing backtick")
            if nxt in "\r\n":
                end_word()
                i += 3 if cmd.startswith("\r\n", i + 1) else 2
            else:
                word.append(nxt)
                i += 2
        elif c == "@" and (h := ps_herestring(cmd, i, closers)):  # @'...'@ is literal, @"..."@ runs its $(...)
            lead = lead or not (word or quoted)
            q, text, i = h
            word.append(text)
            quoted = True
            inner += ps_subexprs(text) if q == '"' else []
        elif c in PS_QUOTES:
            lead = lead or not (word or quoted)
            q, i, quoted = PS_QUOTES[c], i + 1, True
            while True:
                if i >= len(cmd):
                    raise ValueError("unbalanced quotes")
                d = cmd[i]
                if q == '"' and cmd.startswith("$(", i):  # it runs, and may hold quotes of its own
                    end = ps_subexpr_end(cmd, i)
                    if end < 0:
                        raise ValueError("unbalanced subexpression")
                    inner.append(cmd[i + 2:end - 1])
                    word.append(cmd[i:end])
                    i = end
                    continue
                if PS_QUOTES.get(d) == q:
                    if PS_QUOTES.get(cmd[i + 1:i + 2]) == q:  # '' or "" inside the quotes is one quote
                        word.append(d)
                        i += 2
                        continue
                    i += 1
                    break
                if q == '"' and d == "`" and i + 1 < len(cmd):
                    word.append(cmd[i + 1])
                    i += 2
                    continue
                word.append(d)
                i += 1
        elif c in " \t\r,":
            end_word()
            i += 1
        elif cmd.startswith("${", i):  # ${out} is one variable, braces and all
            j = cmd.find("}", i)
            if j < 0:
                raise ValueError("unclosed variable")
            word.append(cmd[i:j + 1])
            i = j + 1
        elif c == "#" and not word and not quoted:  # a comment runs to the end of the line
            j = cmd.find("\n", i)
            i = j if j >= 0 else len(cmd)
        elif cmd.startswith("<#", i) and not word and not quoted:
            j = cmd.find("#>", i + 2)
            if j < 0:
                raise ValueError("unclosed comment")
            i = j + 2
        elif c in ";|&\n(){}":
            end_word()
            out.append(SEP if c in ";&\n" else PIPE if c == "|" else OPEN if c in "({" else CLOSE)
            i += 1
        else:
            word.append(c)
            i += 1
    end_word()
    return out, inner


def strip_ps_assignment(seg: list[str]) -> tuple[list[str], list[str]]:
    """`$out = git push --force` still runs git push: split the assigned variables from the command."""
    text = "\0".join(seg)
    m = PS_ASSIGNMENTS.match(text)
    if not m:
        return [], seg
    rest = text[m.end():]
    return re.findall(PS_VAR, m.group(0)), rest.split("\0") if rest else []


def check_set_env(args: list[str]) -> str | None:
    """Set-Item env:HUSKY 0 sets a variable, like $env:HUSKY = 0. The path and value come from -Path (or
    -LiteralPath) and -Value, also written -Value:0, and otherwise from the first two other arguments."""
    bound, positional, k = {}, [], 0
    while k < len(args):
        a, k = args[k], k + 1
        name, colon, value = a[1:].partition(":")
        name = name.lower()
        if a[:1] != "-" or not name:
            positional.append(a)
        elif any(p.startswith(name) for p in ("path", "literalpath", "value")):
            if not colon:
                value, k = (args[k] if k < len(args) else ""), k + 1
            bound["value" if "value".startswith(name) else "path"] = value
    for slot, value in zip([p for p in ("path", "value") if p not in bound], positional):
        bound[slot] = value
    m = re.fullmatch(r"env:[\\/]?(\w+)", bound.get("path", ""), re.I)
    if m and HOOK_SKIP_ENV.match(f"{m.group(1).upper()}={bound.get('value', '')}"):
        return f"Skipping git hooks via env:{m.group(1)} is forbidden."
    return None


def check_ps_segment(seg: list[str], state: dict, depth: int, stdin: bool = False) -> str | None:
    check_time()
    targets, seg = strip_ps_assignment(seg)
    for t in targets:  # $env:HUSKY = 0, also as $a = $env:HUSKY = 0
        m = re.fullmatch(r"\$\{?env:(\w+)\}?", t, re.I)
        if m and HOOK_SKIP_ENV.match(f"{m.group(1).upper()}={''.join(seg)}"):
            return f"Skipping git hooks via $env:{m.group(1)} is forbidden."
    if not seg or re.match(r"[A-Za-z_]\w*=", seg[0]):  # PowerShell has no NAME=value prefix: $x = 'HUSKY=0' is text
        return None
    prog = prog_name(seg[0])
    if prog in PS_REMOVE:
        args = [a.upper() if a[:1] == "$" else a for a in seg[1:]]  # variable names ignore case: $home is $HOME
        # on Linux and macOS, rm in pwsh is /bin/rm, so check it both ways
        return check_remove_item(args) or (check_rm(args) if prog == "rm" else None)
    if prog in PS_SET_ITEM:
        return check_set_env(seg[1:])
    if prog in ("iex", "invoke-expression"):
        args = [a for a in seg[1:] if not (len(a) > 1 and a[0] == "-" and "command".startswith(a[1:].lower()))]
        if not args:
            return "Invoke-Expression with piped input can't be verified."
        return check(" ".join(args), depth + 1, state, "powershell")
    return check_segment(seg, state, depth, stdin)


def ps_command_word(words: list) -> bool:
    """Whether a group starting with these words holds a command (Join-Path, git, .\\build.ps1) rather than a value
    ('x', $x, 1, -not $y)."""
    return bool(words) and isinstance(words[0], str) and not isinstance(words[0], Quoted) and \
        bool(re.match(r"[A-Za-z_.\\/]", words[0]))


def check_ps(cmd: str, depth: int, state: dict) -> str | None:
    try:
        tokens, inner = ps_tokenize(cmd)
    except ValueError:
        return "Could not parse this command (unbalanced quotes?), so it can't be verified."
    for sub in inner:  # $(...) runs even inside "..." and @"..."@
        reason = check(sub, depth + 1, state, "powershell")
        if reason:
            return reason
    # ( ) and { } group commands (if ($?) { git push }) but also wrap arguments (git commit -m (Get-Date) --no-verify),
    # so check both ways, each from the same starting branch. Read as an argument, a group holding a command,
    # (Join-Path $HOME x), is one word: its output. A group holding a value, ('--force') or ($HOME), is that value.
    start = dict(state)
    for grouping in (True, False):
        state.clear()
        state.update(start)
        seg, piped, skip = [], False, 0
        for n, t in enumerate(tokens + [None]):  # None ends the last command
            if skip and t is not None:
                skip += (t is OPEN) - (t is CLOSE)
            elif not grouping and t is OPEN and ps_command_word(tokens[n + 1:n + 2]):
                seg.append(SUBST)
                skip = 1
            elif not grouping and (t is OPEN or t is CLOSE):
                continue
            elif isinstance(t, str):
                seg.append(t)
            else:
                reason = check_ps_segment(seg, state, depth, piped) if seg else None
                if reason:
                    return reason
                seg, piped = [], t is PIPE or (piped and not seg)
    return None


def check(cmd: str, depth: int = 0, state: dict | None = None, shell: str = "bash") -> str | None:
    if depth > 3:
        return "Command nesting too deep to verify."
    state = {} if state is None else state
    if shell == "powershell":
        return check_ps(cmd, depth, state)
    try:
        cmd, inner = prescan(strip_heredocs(cmd))
        tokens = tokenize(cmd)
    except ValueError:
        return "Could not parse this command (unbalanced quotes?), so it can't be verified."
    for sub in inner:  # commands inside $(...) and backticks run too
        reason = check(sub, depth + 1, state)
        if reason:
            return reason
    # a standalone { } may group commands ({ git push -f; }) or be a plain argument (-m '{}'), so check both ways,
    # each from the same starting branch
    start = dict(state)
    for braces in (False, True):
        state.clear()
        state.update(start)
        for seg, piped in segments(tokens, braces):
            seg, redirected = drop_stdin_redirects(seg)
            reason = check_segment([t.translate(RESTORE) for t in seg], state, depth, piped or redirected)
            if reason:
                return reason
    return None


def main() -> int:
    cmd = ""
    try:  # Claude Code runs the command on any exit code but 2, so a crash here must block
        data = json.loads(sys.stdin.buffer.read())  # bytes: Claude Code sends UTF-8, Windows would decode as cp1252
        shell = {"Bash": "bash", "PowerShell": "powershell"}.get(data.get("tool_name"))
        if not shell:
            return 0
        cmd = (data.get("tool_input") or {}).get("command", "")
        reason = "Command too long to verify." if len(cmd) > MAX_COMMAND else check(cmd, shell=shell)
    except TimeoutError as e:
        reason = str(e)
    except Exception as e:
        reason = f"Could not read or check this command ({type(e).__name__}), so it can't be verified."
    if reason:
        print(f"BLOCKED by build-crew guard: {reason}\nCommand: {str(cmd).strip()[:300]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
