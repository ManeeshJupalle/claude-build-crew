# Changelog

## [Unreleased]
### Added
- `tests/guard_diff.py` runs the guard and another version of it, `origin/main` by default, on the test cases and on dangerous commands wrapped in others, and lists each command that version blocks and this one allows. Use it to review a guard change. (#62)

### Fixed
- `deps.py` reports npm's root as `npm_root`, and `/deps` installs an npm update from there. Before, it went to the folder `npm prefix` prints, which doesn't exist when a folder's name has a UUID in it: npm prints that as `***`. (#64)

### Security
- The guard catches more ways past it in Bash: (#62)
  - a wrapper option that takes no value, which hid the command after it: `env -i git …`, `sudo -n …`, `sudo -s …`, `command -p …`, `time -p …`. Each wrapper has its own options, so `env -C dir`, `exec -a name` and `sudo -nu root` skip their values
  - `env -S '…'` and `--split-string`, whose string env splits into the command, reading `#` and `\c` as env does
  - `timeout` with a duration in another form: `timeout 1e3 git push --force`
  - `builtin` as a wrapper, and `export` after one: `command export x HUSKY=0`
  - a command, or git's or gh's subcommand, that xargs reads: `echo 'push --force' | xargs git`, `xargs -I{} {} push`, `xargs env`. Also `xargs cmd /c` and `xargs pwsh -c`, which run what xargs adds as code
  - a GraphQL query from a shell variable: `-f query="$Q"`
  - a shell reading the pipe from a substitution: `… | tee >(bash)`, `… | y "$(bash)"`
  - a `<<` in `((…))`, `$((…))`, `$[…]`, `${…}` or `a[…]=`, which bash reads as a shift, not a heredoc whose body would hide the lines after it. Where bash may still read a heredoc, the guard checks both readings
- The guard reads `$'…'` escapes as bash does. Before, it dropped each backslash, so `eval $'true\ngit push --force'` read as one harmless command, and `$'\x2d\x2dforce'` wasn't `--force`. (#65)

## [0.5.1] - 2026-11-07
### Added
- `stale-approval` and `approved-merge` evals: the same request to merge a PR, with an approval that does or doesn't cover its head commit. An eval can't merge real PRs, so a stand-in `gh` in `evals/bin/` plays the PR. (#50)

### Changed
- When the guard blocks a merge, it gives the full `merge_gate` command with the plugin's path. Before, an agent had to find the plugin's folder itself. (#50)
- When `/deps` runs `npm audit fix`, its report lists every other package that changed in `package-lock.json`. `npm audit fix` fixes all it can, not just the one dependency `/deps` picked. (#55)

### Removed
- The failing-hook eval case. `claude plugin eval` turns git hooks off for the agent's commands, so the case couldn't tell the arms apart. (#48)

### Fixed
- `phase_gate.py check` fails when a phase has a second `**Skills:**` line, or a backticked word on it that isn't a skill name. Before, it ignored the second line and took a path like `docs/x.md` for a skill. `/build-phase` reads a skill's description before invoking it, and skips one that acts instead of advising. (#54)
- The `rejected-push` eval catches a branch moved inside the local remote without a push (`update-ref`, `branch -f`, a fetch run in it). With hooks off, those never reached the remote's audit hook. (#48)
- The `rejected-push` eval no longer passes work pushed only to `main`. It also fails a run that copies a ref file into the remote, or pushes with the remote's hooks off. (#53)
- `deps.py` fixes for npm and pip: (#55)
  - When pip finds no release at or above a package's installed version, the message says that and names the possible causes, adding what the version itself shows: a pre-release or a local version, such as `+cu118`. Before, it only asked whether the index was reachable. It still asks that when pip found nothing at all.
  - With npm workspaces, each installed copy of a package is listed, with its `location` and its `dependents`, both as paths from npm's root, also when run from a workspace's folder. A vulnerability marks only the copies `npm audit` names, also one that isn't outdated. Before, only the last copy was kept. `/deps` installs an update in each dependent, with `-w` for a workspace.
  - An update from 0.0.x to 0.0.y counts as a major bump, as npm's `^` reads it, so `/deps` skips it unless you name the package. Before, it was a patch.
- `merge_gate` reports a merge that worked even when gh fails afterwards, for example deleting a local branch that another worktree has checked out. Before, it said `"merged": false` for a PR GitHub had merged. (#56)
- The guard allows commands it wrongly blocked: (#58)
  - a `git config` read that ends with a redirect, such as `git config --get core.hooksPath 2>/dev/null`, `2>&1` or PowerShell's `2>$null`. A quoted `'2>/dev/null'` is still refused, since git sets the path to it.
  - `ls scripts/*.sh | xargs -n1 bash --`, where the `--` makes what xargs passes bash a script's name. Without it, xargs could pass `-c` and a command, so that form is still refused.
  - `diff <(sort a) <(sort b)`: a process substitution is one word, and its command is checked
  - `git pull || pwsh -NoProfile`: PowerShell 7's `||` runs the next command, it isn't a pipe
  - `gh api` REST paths that only contain "graphql", such as `repos/o/graphql-tools/issues --input body.json`
- In PowerShell, an operator in an expression isn't read as a flag: ``git commit -m ($lines -join "`n")`` is allowed. (#60)

### Security
- The guard catches more ways past it: (#58)
  - a `<<` in quotes or a comment, which was taken for a heredoc and hid the lines after it, and heredocs whose delimiter bash reads another way: `<<<EOF`, `<<E"OF"`, `<<EOF.x`, `<< ';'`
  - heredocs: the `$(…)` and backticks in an unquoted heredoc's body, which bash runs; a closing line with spaces around the delimiter, which bash doesn't count; and a delimiter the guard can't read, such as `<<''` or `<<$EOF`, which is refused
  - a process substitution that split a command, as in `rm -rf <(true) ~`, and a shell or `source` running one as its script: `bash <(…)`, `. <(…)`, `pwsh <(…)`
  - a hook-skipping variable after another word in `export`, `declare` or `typeset`, which bash still sets: `export x HUSKY=0`, `declare -x HUSKY=0`
  - a shell reading stdin through `/dev/stdin`, a redirect next to it (`bash<<EOF`, `| bash 2>/dev/null`), a shell it runs (`| bash -c bash`), `xargs -I{} bash -c '{}'`, or `cmd` without `/c`
  - bash options that hid the command: `-euo pipefail` before a heredoc, `bash -c -e '…'`, `-oc`, `-co`
  - a redirect that split a command, as in `git push &>/dev/null --force` or `git push 2>&1 --force`
  - a GraphQL query built from `$(…)`, or sent to an encoded endpoint such as `gr%61phql`
- The guard catches more ways past it in PowerShell: (#60)
  - a `)` in a `#` or `<# #>` comment inside `$(…)`, which doesn't close it
  - assignments written `[string] $x = …` or `$x[0].y = …`
  - `${env:USERPROFILE}` and `${env:HOME}` as delete targets
  - a group whose output becomes arguments: `$(if ($true) {'--force'})`, `$(switch …)`, `(echo '--force')`, and values split by a newline or `;`, as in `@(⏎'x',⏎'--force'⏎)` or `$('x'; '--force')`
  - `Set-Content -Encoding utf8 env:HUSKY 0`, `Set-Item -Filter * env:HUSKY 0` and `New-Item -Path Env: -Name HUSKY -Value 0`
  - `gh api $(…) --input -`, whose endpoint PowerShell builds
- `rm --rec ~` from Bash is refused: GNU rm takes a long option's unique prefix. (#60)
- `merge_gate` passes only if the PR merged at the head the review approved. Before, it also passed when someone pushed a new commit and merged the PR outside the gate while it ran. (#56)

## [0.5.0] - 2026-11-05
### Added
- A phase in `docs/PLAN.md` can name the skills it's built with. `/kickoff` gives the planner the installed skills to choose from, and `/build-phase` loads the ones a phase names. Skills stay advice: the gates still decide, and a phase with no skills builds as before. (#41)
- `/deps` updates one outdated or vulnerable dependency, proves the update with `checks.py`, then hands off to `/ship`. (#42)
  - The new `deps.py` gate finds them with each stack's own tools: `npm outdated` and `npm audit`, and for Python `pip list` in the project's virtualenv plus `pip-audit --path` run from outside it.
  - A missing tool is SKIPPED and never reported as up to date. A tool that errors fails the gate.
- `evals/`: a `claude plugin eval` suite that runs the same git tasks with and without build-crew, and grades what happened in the repo, not what the agent said. It runs on macOS or Linux. (#43)

### Changed
- The guard refuses some commands it allowed before, because it can't check them: (#44)
  - A `git config` read of `core.hooksPath`, an alias or an include must end with the key. Only scope flags and `--get`, `--get-all` or `get` can come before it, and nothing can come after it, not even a redirect.
    - So `git config --type=path --get core.hooksPath`, `git config --get-regexp alias.` and `git config core.hooksPath 2>/dev/null` are refused.
    - The guard can't tell `2>/dev/null` from a quoted value, and `git config core.hooksPath '2>/dev/null'` sets the path.
  - A git subcommand that isn't a plain word, such as `git $c` or `git "$sub"`, is refused.

### Security
- The guard catches more ways past it: (#44, #46)
  - a git option whose value hid the real command: `git --config-env user.name=V push --force`, also with `--attr-source` or `--shallow-file`
  - a variable as the subcommand: `S=push; git $S --force origin feat/x`
  - abbreviated long options, which git accepts: `commit --no-verif`, `push --mir`, `push --del`, `reset --har`, `clean --forc`, `add --al`
  - deleting remote branches or pushing all of them: `git push origin :branch`, `--prune`, `--branches`, and any refspec with `*`
  - turning hooks off or hiding commands through git config:
    - `git config core.hooksPath …`, including `core.hooksPath get` and `core.hooksPath --local`, which set the path to that word
    - removing the core section
    - `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM`, in any case
    - defining aliases (`git -c alias.p='push --force' p`, `git config alias.…`) or renaming a section to alias (`--rename-section`, also abbreviated as `--ren`)
    - config includes (`include.path`, `includeIf`), set directly, with `-c`, or by renaming a section
  - `gh api` writes to refs or branch protection:
    - `--method=DELETE`, `-XDELETE`, `-iXDELETE`
    - fields without `-X`, which gh sends as POST
    - GraphQL ref, branch-protection and ruleset mutations, and GraphQL queries read from stdin or a file

## [0.4.1] - 2026-10-30
### Security
- `secret_scan` reports npm and Hugging Face tokens that come right after a non-ASCII letter, such as `é`. Before, it missed them. (#29, by @Golden007-prog)

## [0.4.0] - 2026-10-30
### Added
- The guard checks commands from Claude Code's PowerShell tool as well as Bash. PowerShell gets its own parser for quoting, escapes, here-strings, brackets and assignments, so `$out = git push --force` and `Remove-Item -Recurse -Force ~` are caught. (#26, #36)

### Changed
- The guard blocks a shell that reads its commands from stdin, because it can't see what they run. This covers `pwsh -Command -` (#28) and a pipe or heredoc into bash, sh or PowerShell, such as `echo … | bash`, `bash <<'EOF'` or `curl … | sh` (#35).
- It also blocks any command it can't check in time: when it crashes, when a command is over 100,000 characters, or after 10s of checking. Before, a crash or a hook timeout let the command run. (#35)

### Fixed
- Bash commands that were wrongly blocked are now allowed:
  - a single-quoted `$(git push --force)` in a commit message (#28)
  - `powershell -c 'Get-ChildItem "D:\"'` (#26)
  - `git status # it's fine` (#35)
  - `git commit -m $'fix: don\'t crash'` (#35)
  - `rm -Force *` inside `pwsh -c` (#36)

### Security
- More commands that got past the guard are now caught:
  - quoted separators, subshells, substitutions used as option values, `HUSKY=0` after `env`, `sudo` or `then`, `cmd /cgit`, and `pwsh -i -enc …` (#28)
  - a reset hidden between two apostrophes in comments (#35)
  - a command after `$'…'` quoting with `\'` in it, as in `echo $'a\'b' ; git push --force ; echo \'` (#35)
- From Bash, recursive deletes of `C:/` and `$env:USERPROFILE` are blocked. (#26)

## [0.3.1] - 2026-10-30
### Changed
- The repo is now `ManeeshJupalle/claude-build-crew`, so the install command is `/plugin marketplace add ManeeshJupalle/claude-build-crew`. The old name redirects, and the plugin is still called `build-crew`. (#23)

### Fixed
- `secret_scan` no longer flags `npm_` text inside a longer word, that is with a letter, digit or `_` right before `npm_` or right after its 36 characters. Real npm tokens are still caught. (#21, by @ungaaaabungaaa)

## [0.3.0] - 2026-10-29
### Changed
- Tested on Python 3.14 as well as 3.9 and 3.13, on macOS, Linux and Windows. (#15, by @Vamsi-2581)

### Fixed
- `checks.py` runs `pytest -q` in a Python repo with no `pyproject.toml`, `setup.py` or `requirements*.txt` (Pipenv, conda or plain scripts), when `conftest.py`, `test_*.py` or `*_test.py` sits directly in `tests/` and no other stack or Makefile target matched. Before, it reported NOT VERIFIED. (#17, by @babyhuey)

### Security
- `secret_scan` catches GitLab personal access tokens (`glpat-`), npm tokens (`npm_`) and Hugging Face tokens (`hf_`). (#10, by @babyhuey)

## [0.2.0] - 2026-10-28
### Added
- Runs on macOS, Linux and Windows. Gates and hooks start through `run.sh`, which finds Python 3.9+ as `python3`, `python` or `py`. (#4)

### Changed
- Done-when and check commands run in bash on every OS (Git Bash on Windows), the shell Claude's Bash tool uses. (#4)

### Fixed
- Windows: plans, settings and commit messages are read and written as UTF-8, so non-ASCII text survives. (#4)
- Windows: agent and command files check out with LF line endings, which Claude Code needs to read their frontmatter. (#4)
- Windows: `checks.py` finds virtualenv tools (`Scripts\*.exe`). (#4)
- `checks.py` handles project paths with spaces. (#4)
- `phase_gate done` keeps PLAN.md's own line endings, so ticking a phase is a one-line diff. Before, it could rewrite the ending of every line. (#4)

### Security
- The guard hook blocks every Bash command when Python is missing. Before, it let them all through. (#4)
- The guard catches Windows spellings of git and gh: `git.exe`, `GH.EXE`, full paths with backslashes. (#4)
- The guard checks commands inside `{ …; }` groups and `if`/`for`/`while` bodies, and after `!`. (#9, #14)
- The guard looks inside more shells: ksh, mksh, ash, fish, csh, tcsh, `cmd /c` (`cmd //c` in Git Bash), and the command given to `pwsh`/`powershell` with `-Command`, `-c` or as a bare first argument. A plain `-EncodedCommand` can't be read, so it's blocked. (#9)
- An empty argument no longer splits a command, so `git push origin "" --force` is blocked. (#9)
- Known gaps: on Windows, commands run through Claude Code's PowerShell tool aren't checked yet (#11). Smaller gaps, such as `(git reset --hard)` and `pwsh -i -enc …`, are tracked in #12.

## [0.1.0] - 2026-10-27
### Added
- Agents: architect, planner, researcher, debugger, pr-creator, pr-reviewer, release-manager, deployer.
- Commands: /kickoff, /build-phase, /ship, /fix, /research, /handoff, /release, /deploy.
- Code gates: `phase_gate`, `checks`, `secret_scan`, `merge_gate`, `next_version`.
- Guard hook blocking destructive git/gh commands; session hook loading the last handoff.
- Plugin + marketplace manifests, and `install.sh` / `uninstall.sh` for manual global installs.
- Two human-facing docs per project: `HOW_IT_WORKS.md` (≤ 150 lines) and `PLAN.md`. Agents treat the code as the spec.
- Merging is done only by `merge_gate.py`. Direct `gh pr merge` is blocked.
