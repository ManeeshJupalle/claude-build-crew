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
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import urllib.parse

PROTECTED = re.compile(r"^(main|master|develop|release/.+)$")
# the full command, so an agent stopped from merging doesn't have to hunt for the plugin's folder
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace("\\", "/")
MERGE_GATE = f'bash "{PLUGIN_ROOT}/run.sh" gates/merge_gate.py <PR> --merge'
SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "|&", ";;"}
WRAPPERS = {"sudo", "command", "exec", "time", "nice", "ionice", "nohup", "env", "xargs", "timeout",
            "stdbuf", "caffeinate", "chronic"}
WRAPPER_VALUE_FLAGS = {"-u", "-g", "-n", "-s", "-k", "-c", "-p", "-o", "-e", "-i", "-I", "-J", "--signal", "--kill-after",
                       "--user"}
# git config from the environment, in any case: Windows names ignore case, so git_config_parameters works there.
# Not HUSKY or SKIP: lowercase skip=1 is an everyday shell variable.
HOOK_SKIP_ENV = re.compile(r"^(HUSKY=0|HUSKY_SKIP_HOOKS=|HUSKY_SKIP_INSTALL=|SKIP=|PRE_COMMIT_ALLOW_NO_CONFIG=|GIT_HOOKS_PATH="
                           r"|(?i:GIT_CONFIG_PARAMETERS=|GIT_CONFIG_COUNT=|GIT_CONFIG_KEY_\d+=|GIT_CONFIG_GLOBAL=|GIT_CONFIG_SYSTEM=))")
# git's global options that take the next word as their value
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source",
                     "--shallow-file", "--super-prefix"}
GH_API_VALUE_OPTIONS = {"--method", "--field", "--raw-field", "--header", "--preview", "--jq", "--template", "--input",
                        "--hostname", "--cache"}
CONFIG_READ_WORDS = {"--local", "--global", "--system", "--worktree", "--show-origin", "--show-scope", "-z", "--null",
                     "--get", "--get-all", "get", "--all"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "export"}  # what follows is a command
PLACEHOLDERS = {c: chr(0xE000 + n) for n, c in enumerate(";&|\n(){}#<>")}  # stand-ins for quoted separators
RESTORE = {ord(v): k for k, v in PLACEHOLDERS.items()}
COMMENT_START = set(" \t\n;&|()")  # a word starts right after one of these, so a # there starts a comment
REDIRECT_OP = re.compile(r"&>>?|<>|<&|>>|>&|>\||[<>]")  # its & or | is part of it, not a separator: 2>&1, >|, &>
# a redirect word from prescan(), whose & and | are placeholders. What follows the operator is its file
REDIRECT = re.compile(rf"\d*(<<-|[<>]+[{PLACEHOLDERS['&']}{PLACEHOLDERS['|']}]?)|{PLACEHOLDERS['&']}>+")
STDIN_PATH = re.compile(r"(^|/)(dev/(stdin|fd/\d+)|proc/[^/]+/fd/\d+)$")
SUBST = ""  # stands for a $(...) or `...` substitution, or a PowerShell group, checked on its own
PROCSUB = ""  # stands for a <(...) or >(...): a file whose contents a command makes, checked on its own
POWERSHELLS = {"pwsh", "powershell"}
PS_VALUE_PARAMS = ("executionpolicy", "windowstyle", "inputformat", "outputformat", "version", "psconsolefile",
                   "configurationname", "configurationfile", "workingdirectory", "settingsfile", "custompipename")
PS_VALUE_ALIASES = {"ep", "ex", "w", "wd", "o", "of", "i", "if", "v"}
DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "$HOME/*",
                        ".", "./", "./*", "..", "../", "*", ".*"}
# C:\, $env:USERPROFILE\*, ${env:HOME}
WINDOWS_ROOTS = re.compile(r"([a-z]:|\$env:(userprofile|home)|\$\{env:(userprofile|home)\})(/\*?)?", re.I)
RM_FLAGS = set("dfiIPRrvWx")  # rm's short options, GNU and BSD: with any other letter (-Force), rm fails
PS_REMOVE = {"remove-item", "ri", "rm", "rmdir", "rd", "del", "erase"}
PS_SET_ITEM = {"set-item", "si", "set-content", "sc", "new-item", "ni"}
PS_SET_SWITCHES = ("force", "passthru", "nonewline", "asbytestream", "whatif", "confirm", "usetransaction", "verbose",
                   "debug")
# commands whose options the guard checks: a value PowerShell computes from '-' pieces, ('--' + 'force'), can be one
PS_DASH_CHECKED = {"git", "gh", "cmd", "iex", "invoke-expression"} | SHELLS | POWERSHELLS | PS_REMOVE | PS_SET_ITEM
# an assignment in an expression, = or $o = or $o=git: what follows runs as a command
PS_ASSIGN_WORD = re.compile(r"(?:\$[^=\s]*?)?(?:[-+*/%]|\?\?)?=(.*)", re.S)
# a group led by one of these outputs the literals in it: $(if ($x) {'--force'}), (echo '--force')
PS_VALUE_LEADS = {"if", "switch", "foreach", "for", "while", "do", "try", "echo", "write", "write-output"}
PS_QUOTES = {"'": "'", "‘": "'", "’": "'", "‚": "'", "‛": "'", '"': '"', "“": '"', "”": '"', "„": '"'}
PS_HERESTRING = re.compile(r"@(['\"])[ \t]*\r?\n")  # opens @'...'@ or @"..."@, which closes at a line starting '@
# $out, $script:x, ${out}, $x[0], $x[0].y, [string[]]$x, [string] $x (words are joined with NUL). No two parts can
# match the same text, so no slow backtracking
PS_VAR = r"(?:\[[\w.]+(?:\[[\w.,]*\])*\]\0?)*(?:\$\{[^}]+\}|\$[\w:.]+)(?:\[[^\]]*\][\w.]*)*"
# $x = , $a = $b = , $x, $y = , +=, ??= ... over a command's words joined with NUL
PS_ASSIGNMENTS = re.compile(rf"(?:{PS_VAR}(?:\0{PS_VAR})*\0?(?:[-+*/%]|\?\?)?=(?!=)\0?)+")
SEP, PIPE, OPEN, BRACE, CLOSE = object(), object(), object(), object(), object()  # PowerShell: ; & \n, |, (, {, ) }
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


def heredoc_word(cmd: str, i: int) -> tuple[tuple[bool, str, bool] | None, int]:
    """The heredoc whose << is at i, and where its delimiter ends: (<<-, the delimiter with its quotes removed, whether
    any of it was quoted, which keeps the body literal), as in <<'EOF', <<E"OF", <<EOF.x. None for a here-string (<<<),
    or for a word bash may read another way: empty, or with $ or ` in it."""
    if cmd.startswith("<<<", i):
        return None, i + 3
    dash = cmd[i + 2:i + 3] == "-"
    j, quoted = i + 2 + dash, False
    while cmd[j:j + 1] in (" ", "\t"):
        j += 1
    word = []
    while j < len(cmd) and cmd[j] not in " \t\n;&|()<>":
        if cmd[j] in "'\"":
            end = cmd.find(cmd[j], j + 1)
            if end < 0 or cmd[j] == '"' and "\\" in cmd[j + 1:end]:
                return None, i + 2
            word.append(cmd[j + 1:end])
            j, quoted = end + 1, True
        else:
            quoted = quoted or cmd[j] == "\\"
            word.append(cmd[j + 1:j + 2] if cmd[j] == "\\" else cmd[j])
            j += 2 if cmd[j] == "\\" else 1
    text = "".join(word)
    return ((dash, text, quoted), j) if text and not set(text) & set("$`") else (None, i + 2)


def heredoc_end(cmd: str, start: int, words: list[tuple[bool, str, bool]], lines: dict) -> int:
    """Where the bodies of heredocs starting at start end: at the end of the last one's closing line, or -1 if one has
    none, which leaves the rest to be read as commands. A closing line is the delimiter exactly, after leading tabs for
    <<-. lines maps a line's text to where lines with that text end, and is filled on first use: a regex search per
    heredoc takes quadratic time on a long command, and the hook can't stop a regex at its limit."""
    if not lines:
        pos = 0
        for line in cmd.split("\n"):
            pos += len(line)
            lines.setdefault((False, line), []).append(pos)
            lines.setdefault((True, line.lstrip("\t")), []).append(pos)
            pos += 1
    for dash, w, _ in words:
        ends = lines.get((dash, w), [])
        k = bisect.bisect_left(ends, start)
        if k == len(ends):
            return -1
        start = ends[k] + 1
    return start - 1


def ps_herestring(cmd: str, i: int, closers: dict) -> tuple[str, str, int] | None:
    """The quote, text and end of a here-string opening at i (@'...'@ or @"..."@), or None. Like heredoc_end(),
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


def closing(cmd: str, i: int, lines: dict) -> int:
    """Index just past the ) or ` that closes the $( or ` at i, or -1. A heredoc's body inside $(...) is skipped, so
    a ' or ) in it doesn't count. lines is heredoc_end()'s."""
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
    j, depth, quote, heredocs = i + 2, 1, None, []
    while j < len(cmd):
        c = cmd[j]
        if c == "\\" and quote != "'":
            j += 2
            continue
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif cmd.startswith("<<", j):
            word, j = heredoc_word(cmd, j)
            heredocs += [word] if word else []
            continue
        elif c == "\n" and heredocs:
            end, heredocs = heredoc_end(cmd, j + 1, heredocs, lines), []
            j = end if end >= 0 else j + 1
            continue
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def substitution(cmd: str, i: int, lines: dict, inner: list[str]) -> int:
    """Add the command in the $(...) or `...` at i to inner, and return the index just past it."""
    end = closing(cmd, i, lines)
    if end < 0:
        raise ValueError("unbalanced substitution")
    # inside `...`, \` \$ and \\ stand for the plain characters, so `echo \`git push\`` nests
    inner.append(re.sub(r"\\([`$\\])", r"\1", cmd[i + 1:end - 1]) if cmd[i] == "`" else cmd[i + 2:end - 1])
    return end


def body_substitutions(cmd: str, i: int, stop: int, lines: dict, inner: list[str]) -> None:
    """Add the commands in the $(...) and `...` of an unquoted heredoc's body, from i to stop, which bash runs.
    \\$ and \\` stay text. With several heredocs on a line, all their bodies are read, quoted or not."""
    while i < stop:
        if cmd[i] == "\\":
            i += 2
        elif cmd[i] == "`" or cmd.startswith("$(", i):
            i = substitution(cmd, i, lines, inner)
            if i > stop:
                raise ValueError("unbalanced substitution")
        else:
            i += 1


def prescan(cmd: str) -> tuple[str, list[str]]:
    """shlex returns a quoted ';' the same as a bare one, and splits $(...) into words. So before tokenizing, swap
    quoted or escaped separators for placeholders, and turn each $(...) and `...` into one word, returning the
    commands inside to be checked on their own. Each redirect becomes a word of its own (x>f is x >f), and heredoc
    bodies are data (commit messages, PR bodies), not commands: drop them, keeping the line each starts on."""
    out, inner, quote, i, heredocs, lines = [], [], None, 0, [], {}
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
            end = substitution(cmd, i, lines, inner)
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
        elif quote is None and cmd.startswith(("<(", ">("), i):  # process substitution: a word, and a command
            out.append(PROCSUB)
            i = substitution(cmd, i, lines, inner)
        elif quote is None and (c in "<>" or cmd.startswith("&>", i) and cmd[i - 1:i] != "&"):  # not &&>
            k = len(out)
            while k and out[k - 1].isdigit():
                k -= 1
            fd = c != "&" and k < len(out) and (k == 0 or out[k - 1] in COMMENT_START)  # 2>f, but not a2>f
            if out and out[-1] not in COMMENT_START and not fd:
                out.append(" ")
            if cmd.startswith("<<", i):
                word, end = heredoc_word(cmd, i)
                if word is None and end == i + 2:  # its body could hide the commands after it
                    raise ValueError("unreadable heredoc delimiter")
                heredocs += [word] if word else []
                op = end if word is None else i + 2 + word[0]
            else:
                op = end = REDIRECT_OP.match(cmd, i).end()
            # the operator keeps its & or |. Its word, if part of it, is one word whatever is in it (<< ';'), and a
            # space keeps an empty one (<<<'') from merging into the operator and taking the next word as its own
            out.append(cmd[i:op].replace("&", PLACEHOLDERS["&"]).replace("|", PLACEHOLDERS["|"]) + " "
                       + "".join(PLACEHOLDERS.get(ch, ch) for ch in cmd[op:end]))
            i = end
        elif c == "\n" and quote is None and heredocs:  # the bodies start on the next line
            out.append(c)
            end = heredoc_end(cmd, i + 1, heredocs, lines)
            if end >= 0 and not all(quoted for _, _, quoted in heredocs):
                body_substitutions(cmd, i + 1, end, lines, inner)
            heredocs = []
            i = end if end >= 0 else i + 1
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


def drop_redirects(seg: list[str]) -> tuple[list[str], bool]:
    """Drop redirects (<<EOF, < file, <<< text, 2>/dev/null, >> log, 2>&1) from a command, and say whether one reads
    stdin. prescan() made each one a word, and a quoted < or > a placeholder."""
    words, found, k = [], False, 0
    while k < len(seg):
        m = REDIRECT.match(seg[k])
        if m:
            found = found or "<" in m.group()
            k += 1 if seg[k][m.end():] else 2  # a bare > or <<< takes the next word
        else:
            words.append(seg[k])
            k += 1
    return words, found


def shell_operand(args: list[str]) -> tuple[str | None, bool]:
    """What bash, sh and the like run, and whether -c makes it a command rather than a script file: the first word
    that isn't an option (bash -c -e '…' runs '…'). None means they read their commands from stdin: no script, -s, or
    a path such as /dev/stdin. -o, -O, --rcfile and --init-file take a value, also in a cluster (-euo pipefail)."""
    k, flags = 0, ""
    while k < len(args) and args[k][:1] in ("-", "+"):
        a, k = args[k], k + 1
        if a in ("-", "--"):
            break
        if a.startswith("--command="):  # fish
            return a.split("=", 1)[1], True
        if a in ("--rcfile", "--init-file"):
            k += 1
        elif a == "--command":
            flags += "c"
        elif not a.startswith("--"):
            flags += a[1:]
            k += a.count("o") + a.count("O")
    script = args[k] if k < len(args) else None
    if "c" in flags:
        return script, True
    return (None if "s" in flags or script and STDIN_PATH.search(posixpath.normpath(script)) else script), False


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
        if setting.startswith(("include.", "includeif.")):  # the included file can define aliases or hooksPath
            return "Including another config file can change hooks or aliases; forbidden."
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
        key = next((w for w in words if w == "core.hookspath" or w.startswith(("alias.", "include.", "includeif."))),
                   None)
        # a read names the key last, with nothing after it: git takes even "--local" or a quoted '2>/dev/null' as the
        # value to set, and the guard can't tell a quoted word from a redirect, so a read can't carry one
        if key and not (words[-1] == key and all(w in CONFIG_READ_WORDS for w in words[:-1])):
            return (f"Changing {key} " + ("skips git hooks" if key == "core.hookspath" else
                                           "can hide what a command runs" if key.startswith("alias.") else
                                           "can change hooks or aliases through another file")
                    + "; forbidden. To read it: git config --get " + key + ", with nothing after it.")
        section = [w for w in words if w in ("remove-section", "rename-section")
                   or long_opt(w, "--remove-section", "--rename-section")]
        # dropping core drops core.hooksPath; renaming a section to alias makes its keys aliases
        renames = any(w == "rename-section" or long_opt(w, "--rename-section") for w in section)  # also --ren
        if section and ("core" in words or renames and any(w in ("alias", "include") or w.startswith("includeif")
                                                           for w in words)):
            return "Removing or renaming git's core section, or renaming one to alias, changes hooks or aliases; forbidden."
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


def gh_api_endpoint(args: list[str]) -> str:
    """The first word after `gh api` that isn't an option or an option's value: the path, or graphql."""
    k = 1
    while k < len(args):
        a = args[k]
        if a.startswith("--"):
            k += 2 if "=" not in a and a in GH_API_VALUE_OPTIONS else 1
        elif a.startswith("-") and len(a) > 1:
            short = next((n for n, c in enumerate(a[1:], 1) if c != "i"), None)  # -i takes no value
            k += 2 if short is not None and short == len(a) - 1 and a[short] in "XfFHpqt" else 1
        else:
            return a
    return ""


def check_gh(args: list[str]) -> str | None:
    if args[:2] == ["pr", "merge"]:
        return f"Merging is done by code, not by an agent: run {MERGE_GATE}"
    if args[:1] == ["repo"] and len(args) > 1 and args[1] in ("delete", "archive", "rename", "edit"):
        return f"gh repo {args[1]} is forbidden."
    if args[:1] == ["api"]:
        joined = " ".join(args)
        if (re.search(r"pulls/\d+/merge\b", joined) or re.search(r"/merges\b", joined)
                or re.search(r"(mergePullRequest|enablePullRequestAutoMerge|mergeBranch)", joined)):
            return f"Merging through the API is forbidden: run {MERGE_GATE}"
        if re.search(r"\b(createRef|updateRefs?|deleteRef|(create|update|delete)(BranchProtectionRule|RepositoryRuleset))\b",
                     joined):
            return "Changing branches, refs or protection rules through the API is forbidden."
        # a GraphQL query read from stdin or a file (--input, query=@-, query=@file) can't be checked for mutations
        endpoint = gh_api_endpoint(args)
        # refusing is safe, so any endpoint that may reach GraphQL counts: a graphql path segment, in any case and
        # encoding (graphql?x=1, /GraphQL, gr%61phql, full URLs, but not schema.graphql), or an endpoint from $(...) ...
        # (in PowerShell, a quoted "$(...)" stays text, and an unquoted one reads as $ and a group)
        maybe_graphql = re.search(rf"(^|/)graphql(?![\w-])|{SUBST}|\$\(|\$\Z", urllib.parse.unquote(endpoint), re.I)
        if maybe_graphql and any(a == "--input" or a.startswith("--input=") or
                                 re.match(r"(-i*[fF]|--(raw-)?field=?)?\w*=@", a) for a in args):
            return "A GraphQL query read from stdin or a file can't be verified; pass it with -f query='…'."
        fields = [m.group(3) or (args[k + 1] if k + 1 < len(args) else "") for k, a in enumerate(args)
                  if (m := re.fullmatch(r"(-i*[fF]|--(raw-)?field=?)(.*)", a, re.S))]
        # a query, or a field's name, from $(...) can hide a mutation. A variable from one can't: -f owner="$(…)".
        # PowerShell reads query=$(…) as query=$ and a group: a query can't end with $
        if maybe_graphql and any(re.match(rf"(?s)(query=.*|[^=]*)({SUBST}|\$\(|\$\Z)", f) for f in fields):
            return "A GraphQL query built from $(…) can't be verified; pass it with -f query='…'."
        # ... but skipping the REST rule isn't, so only an exact GraphQL endpoint does: not .../git/refs/heads/graphql
        graphql = re.fullmatch(r"(https?://[^/]+(/api)?)?/?graphql", endpoint) is not None
        if (not graphql and gh_api_method(args) != "GET"  # GraphQL is always POST: its mutations count
                and re.search(r"(branches|protection|rulesets|git/refs)", joined)):
            return "Changing branches, refs or protection rules through the API is forbidden."
    return None


def check_rm(args: list[str]) -> str | None:
    recursive = any(long_opt(a, "--recursive") or set(short_flags(a)) <= RM_FLAGS and set("rR") & set(short_flags(a))
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
        return check(" ".join(args[k:]), depth + 1, state, "powershell", stdin)
    return READS_STDIN if stdin else None


def with_xargs_input(opts: list[str], args: list[str]) -> list[str]:
    """A command's arguments once xargs, with options opts, adds what it reads, as -c and a command at worst: after
    them, or in place of -I's or -i's replace string (one word each), or of BSD -J's (any number of words)."""
    replace, words, k = None, ["-c", "x"], 0
    while k < len(opts):
        t, k = opts[k], k + 1
        if long_opt(t, "--replace"):
            replace, words = (t.split("=", 1)[1] if "=" in t else "{}"), ["-c"]
        for n, ch in enumerate(short_flags(t)):
            if ch in "adEIJLnPRsS":  # takes a value: the rest of the word, or the next word
                value = t[n + 2:] or (opts[k] if k < len(opts) else "")
                k += not t[n + 2:]
                if ch in "IJ":
                    replace, words = value, ["-c"] if ch == "I" else ["-c", "x"]
                break
            if ch in "eil":  # takes an optional value, in the same word
                if ch == "i":
                    replace, words = t[n + 2:] or "{}", ["-c"]
                break
    if replace is None:
        return args + words
    return [w for a in args for w in (words if a.startswith(replace) else [a])]


def check_segment(seg: list[str], state: dict, depth: int, stdin: bool = False) -> str | None:
    check_time()
    rest = strip_prefix(seg)
    prefix = seg[:len(seg) - len(rest)]
    # assignments can follow keywords and wrappers too: env HUSKY=0 git ... And export and the like set each NAME=value
    # they get, even after a word they refuse: export x HUSKY=0
    assigning = "export" in prefix or set(rest[:1]) & {"declare", "typeset", "readonly", "local"}
    for t in prefix + (rest if assigning else []):
        if HOOK_SKIP_ENV.match(t):
            return f"Skipping git hooks via {t.split('=')[0]} is forbidden."
    # xargs's options, when it runs the command: not when xargs is a value (sudo -u xargs)
    xargs = next((prefix[k + 1:] for k, t in enumerate(prefix)
                  if prog_name(t) == "xargs" and (k == 0 or prefix[k - 1] not in WRAPPER_VALUE_FLAGS)), None)
    seg = rest
    if not seg:
        return None
    prog = prog_name(seg[0])
    args = seg[1:]
    if prog in SHELLS:
        script, command = shell_operand(args)
        if PROCSUB in (script or ""):  # bash <(…) runs what the command prints
            return "A shell running a script from a process substitution can't be verified."
        if xargs is not None:  # xargs passes what it reads as arguments
            if command and (script is None or any(set(short_flags(t)) & set("IiJ") or long_opt(t, "--replace")
                                                  for t in xargs)):
                return ("A shell running a command xargs builds from its input can't be verified. Pass the input "
                        "as arguments instead: xargs -n1 sh -c '… \"$1\" …' _")
            if not command and shell_operand(with_xargs_input(xargs, args))[1]:
                return ("What xargs passes a shell could be options, such as -c and a command, so it can't be "
                        "verified. End the shell's options first: xargs -n1 bash --")
            # the shell gets a script's name, and /dev/null as stdin, unless -a reads the arguments from a file
            stdin = stdin and any("a" in short_flags(t) or long_opt(t, "--arg-file") for t in xargs)
        # bash runs -c's command from its first operand (bash -c -e '…'), fish and csh from the word after -c
        commands = [args[k + 1] for k, a in enumerate(args[:-1]) if a in ("-c", "--command") or "c" in short_flags(a)]
        for text in dict.fromkeys(commands + ([script] if command and script is not None else [])):
            reason = check(text, depth + 1, state, stdin=stdin)  # the command reads the shell's stdin too
            if reason:
                return reason
        if stdin and not script and not commands:  # echo 'git push --force' | bash, bash <<'EOF'
            return READS_STDIN
    if prog in POWERSHELLS:
        if any(PROCSUB in a for a in args):  # pwsh <(…) runs it as a script file
            return "A shell running a script from a process substitution can't be verified."
        return check_powershell(args, state, depth, stdin)
    if prog == "cmd":  # with no /c, or with /k, cmd runs what it reads from stdin
        for k, a in enumerate(args):
            m = re.match(r"/+[ckr]", a, re.I)  # Git Bash turns /c into a path, so it's written //c there; /cgit runs too
            if m:
                reason = check(" ".join([a[m.end():], *args[k + 1:]]), depth + 1, state, stdin=stdin)
                return reason or (READS_STDIN if stdin and m.group()[-1] in "kK" else None)
        if stdin:
            return READS_STDIN
    if prog in ("source", ".") and args and PROCSUB in args[0]:
        return "Sourcing a process substitution can't be verified."
    if prog == "eval":
        return check(" ".join(args), depth + 1, state, stdin=stdin)
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
        elif cmd.startswith(("#", "<#"), j) and cmd[j - 1] in " \t\r\n;(){}|&":  # a ) in a comment doesn't close it
            end = cmd.find("\n", j) if c == "#" else cmd.find("#>", j + 2) + 1
            if end <= 0:
                return -1
            j = end
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
    """PowerShell words, with SEP, PIPE, OPEN, BRACE and CLOSE for separators, pipes and brackets, and the commands
    inside $(...) in "..." and @"..."@, which run. Unlike the Bash path, quoting is tracked, so a quoted ';' is a word.
    Backtick escapes (also inside "..."), '' is a quote inside '...', a backslash is an ordinary character, a
    backtick at the end of a line continues it, and a here-string is one word."""
    out, inner, word, quoted, lead, i, closers, redirect = [], [], [], False, False, 0, {}, False

    def end_word():
        nonlocal word, quoted, lead, redirect
        if word or quoted:
            if not redirect:  # a redirect's file isn't an argument
                out.append(Quoted("".join(word)) if lead else "".join(word))
            redirect = False
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
        elif c == ">":  # a redirect (2>$null, >> log, *>&1) is dropped with its file
            if not quoted and "".join(word) in ("1", "2", "3", "4", "5", "6", "*"):
                word = []  # the stream it redirects
            end_word()
            i += 2 if cmd.startswith(">>", i) else 1
            if re.match(r"&[12]", cmd[i:i + 2]):  # 2>&1 joins two streams: no file
                i += 2
            else:
                redirect = True
        elif c in ";|&\n(){}":
            end_word()
            redirect = False
            sep = c in ";&\n" or cmd.startswith("||", i)  # PowerShell 7's || runs the next command: it isn't a pipe
            out.append(SEP if sep else PIPE if c == "|" else OPEN if c == "(" else BRACE if c == "{" else CLOSE)
            i += 2 if cmd.startswith("||", i) else 1
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
    """Set-Item env:HUSKY 0 sets a variable, like $env:HUSKY = 0, and so does New-Item -Path Env: -Name HUSKY. The path
    and value come from -Path (or -LiteralPath), -Name and -Value, also written -Value:0, and otherwise from the first
    two other arguments. Other parameters take a value too (-Encoding utf8, -Filter *), except a few switches."""
    bound, positional, k = {}, [], 0
    while k < len(args):
        a, k = args[k], k + 1
        name, colon, value = a[1:].partition(":")
        name = name.lower()
        if a[:1] != "-" or not name:
            positional.append(a)
            continue
        slot = next((p for p in ("path", "literalpath", "value", "name") if p.startswith(name)), None)
        if not slot and (any(p.startswith(name) for p in PS_SET_SWITCHES) or not re.fullmatch(r"[a-z]+", name)):
            continue
        if not colon:
            value, k = (args[k] if k < len(args) else ""), k + 1
        if slot:
            bound["path" if slot == "literalpath" else slot] = value
    for slot, value in zip([p for p in ("path", "value") if p not in bound], positional):
        bound[slot] = value
    path = bound.get("path", "")
    if "name" in bound:
        path += ("" if path.endswith((":", "/", "\\")) else "/") + bound["name"]
    m = re.fullmatch(r"env:[\\/]?(\w+)", path, re.I)
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
    if seg[0] == "." and len(seg) > 1:  # . git push dot-sources git: it runs it
        seg = seg[1:]
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
    ('x', $x, 1, -not $y, or what a keyword or echo outputs)."""
    return bool(words) and isinstance(words[0], str) and not isinstance(words[0], Quoted) and \
        bool(re.match(r"[A-Za-z_.\\/]", words[0])) and words[0].lower() not in PS_VALUE_LEADS


def check_ps(cmd: str, depth: int, state: dict, stdin: bool = False) -> str | None:
    try:
        tokens, inner = ps_tokenize(cmd)
    except ValueError:
        return "Could not parse this command (unbalanced quotes?), so it can't be verified."
    for sub in inner:  # $(...) runs even inside "..." and @"..."@
        reason = check(sub, depth + 1, state, "powershell")
        if reason:
            return reason
    # ( ) and { } group commands (if ($?) { git push }) but also wrap arguments (git commit -m (Get-Date) --no-verify),
    # so check both ways, each from the same starting branch
    start = dict(state)
    for grouping in (True, False):
        state.clear()
        state.update(start)
        reason = check_ps_tokens(tokens, grouping, state, depth, stdin)
        if reason:
            return reason
    return None


def ps_group_end(tokens: list, n: int) -> int:
    """Index of the ) or } that closes the group opening at tokens[n]."""
    depth = 0
    for k in range(n, len(tokens)):
        depth += (tokens[k] is OPEN or tokens[k] is BRACE) - (tokens[k] is CLOSE)
        if depth == 0:
            return k
    return len(tokens)


def check_ps_tokens(tokens: list, grouping: bool, state: dict, depth: int, stdin: bool, nested: int = 0) -> str | None:
    """One reading of check_ps()'s tokens. With grouping, ( ) and { } separate commands. Without, a group is an
    argument: one holding a command, (Join-Path $HOME x), is one word, its output, and is checked as a command of its
    own, whose value groups are its arguments. A group holding a value, ('--force') or ($HOME), is that value."""
    if nested > 20:
        return "Command nesting too deep to verify."
    # values: for each open value group, [whether it's a ( ) group, whose ; and newlines separate values,
    # whether its -words are operators (in an expression, ($a -join "`n"), up to its first ;), the words of
    # its statement after a ; or |, which can be a command: $(1; git push --force), checked on its own too,
    # whether that statement is piped, whether an operator computes its value, whether a literal that starts its
    # value starts with -, and whether nothing but '' has come yet]. A { } block holds statements, which end the command
    # as usual, unless it's inside a ( ) value group: $(if ($x) {'a'; '--force'}) outputs both
    seg, piped, skip, values, lead = [], False, 0, [], False

    def add(word):
        seg.append(word)
        for v in values:
            if v[2] is not None:
                v[2].append(word)

    def statement_reason(v):  # on a copy of the branch state: $(if ($x) {'HEAD'; git checkout -b y}) may not switch
        return check_ps_segment(v[2], dict(state), depth, v[3] or stdin) if v[2] else None

    for n, t in enumerate(tokens + [None]):  # None ends the last command
        opens = t is OPEN or t is BRACE
        if skip and t is not None:
            skip += opens - (t is CLOSE)
        elif not grouping and opens and ps_command_word(tokens[n + 1:n + 2]):
            # ($x -and (git push origin ('--force'))) runs git with --force. It may not run at all (if, -and, try), so
            # a branch it switches to isn't the one later commands run on
            body = tokens[n + 1:ps_group_end(tokens, n)]
            reason = check_ps_tokens(body, False, dict(state), depth, stdin, nested + 1)
            if reason:
                return reason
            add(SUBST)
            if values:  # ((Get-Date) + '-fix'): the value starts with its output, not with the '-'
                values[-1][6] = False
            # a statement in it that holds a value outputs it too, and PowerShell passes each one on as an argument:
            # git push origin $(Get-Date; '--force')
            level, value, echo = 0, False, False
            for k, b in enumerate(body):
                level += (b is OPEN or b is BRACE) - (b is CLOSE)
                if level == 0 and b is SEP:
                    value = not ps_command_word(body[k + 1:k + 2])
                    echo = str(body[k + 1:k + 2][0]).lower() in ("echo", "write", "write-output") if value else False
                # its -words are operators ($a -f $b), unless it echoes them: $(Get-Date; echo --force)
                elif value and isinstance(b, str) and (echo or not (b[:1] == "-" and not isinstance(b, Quoted))):
                    add(b)
            skip = 1
        elif not grouping and opens:
            nxt = tokens[n + 1] if n + 1 < len(tokens) else None
            lead = isinstance(nxt, str) and not isinstance(nxt, Quoted) and nxt.lower() in PS_VALUE_LEADS
            if lead:  # it outputs what it echoes: its literals, and maybe more
                add(SUBST)
            values.append([t is OPEN, t is OPEN and not lead, None, False, False, False, True])
            if len(values) > 50:  # each word joins every open group's statement: deep nesting takes quadratic time
                return "Command nesting too deep to verify."
        elif not grouping and t is CLOSE:
            if not values:
                continue
            reason = statement_reason(values[-1])
            if reason:
                return reason
            v = values.pop()
            if v[4] and v[5]:  # the command it's an argument of, past $o = and a dot: its first words are enough
                owner = strip_ps_assignment(next((u[2] for u in reversed(values) if u[2]), seg)[:64])[1]
                owner = owner[1:] if owner[:1] == ["."] else owner
                if owner and prog_name(owner[0]) in PS_DASH_CHECKED:
                    return f"PowerShell builds this {prog_name(owner[0])} argument from '-' pieces, so it can't be verified."
            if values:  # (('--') + 'force') builds on what the inner group made; ((Get-Date) + '-x') doesn't start with -
                values[-1][4] |= v[4]
                values[-1][5] |= v[5]
                values[-1][6] = False
        elif any(v[0] for v in values) and (t is SEP or t is PIPE):  # ('x'; '--force') outputs both
            reason = statement_reason(values[-1])
            if reason:
                return reason
            values[-1][1:4] = [False, [], t is PIPE]
            values[-1][6] = True
        elif isinstance(t, str):
            assign = values and values[-1][1] and not isinstance(t, Quoted) and PS_ASSIGN_WORD.fullmatch(t)
            operator = bool(values) and values[-1][1] and t[:1] == "-" and not isinstance(t, Quoted)
            if values:  # ('--' + 'force'), ('-{0}force' -f '-'), but not ($prefix + '-' + $n) or '- fixed x'
                v = values[-1]
                v[4] |= operator or "+" in t or "*" in t
                v[5] |= v[6] and isinstance(t, Quoted) and t[:1] == "-" and t[1:2] != " "
                v[6] = v[6] and (t == "+" or t == "" and isinstance(t, Quoted))  # ('' + '--' + 'force')
            if lead:  # if, echo: not output
                lead = False
            elif assign:  # ($o = git push --force) runs the command: no longer an expression, and checked too
                add(t)
                values[-1][1:4] = [False, [assign.group(1)] if assign.group(1) else [], False]
            elif not operator:
                add(t)
        else:
            reason = check_ps_segment(seg, state, depth, piped or stdin) if seg else None
            if t is None:  # a group left open never reached its ), where its statement is checked
                reason = reason or next((r for v in values if (r := statement_reason(v))), None)
            if reason:
                return reason
            seg, piped = [], t is PIPE or (piped and not seg)
    return None


def check(cmd: str, depth: int = 0, state: dict | None = None, shell: str = "bash", stdin: bool = False) -> str | None:
    """stdin: whether the command reads a pipe or a redirect, as when a shell fed by one runs it."""
    if depth > 3:
        return "Command nesting too deep to verify."
    state = {} if state is None else state
    if shell == "powershell":
        return check_ps(cmd, depth, state, stdin)
    try:
        cmd, inner = prescan(cmd)
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
            seg, redirected = drop_redirects(seg)
            reason = check_segment([t.translate(RESTORE) for t in seg], state, depth, piped or redirected or stdin)
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
