import json
import os
import re
import subprocess
import sys
import time

import pytest

from conftest import BASH, HOOKS, ROOT

BLOCKED = [
    # force push, every spelling
    "git push --force origin feat/x",
    "git push -f",
    "git push -fu origin feat/x",
    "git push --force-with-lease",
    "git push origin +feat/x",
    "git -C . push --force",
    "git --no-pager push --force",
    # protected branches, every spelling
    "git push origin main",
    "git push origin HEAD:master",
    "git push origin feat/x:main",
    "git push origin HEAD:refs/heads/main",
    "git push origin release/1.2",
    "git push --all",
    "git push origin --delete feat/old",
    "git push origin :feat/old",                       # an empty source deletes the remote branch
    "git push origin :refs/heads/feat/old",
    "git push origin feat/x :feat/old",
    "git --config-env core.hooksPath=V push --force",  # a value that isn't the subcommand
    "git --config-env=core.hooksPath=V commit -m x",
    "git --config-env CORE.HOOKSPATH=V commit -m x",
    "git --config-env user.name=V push --force",
    "git --attr-source HEAD push --force",
    "git --shallow-file x push --force",
    "git --bogus-option x/y push --force",            # x/y isn't a subcommand: an option's value
    "git -c alias.p='push --force' p",
    "git --config-env alias.p=V p",
    "git config alias.p 'push --force'",
    "git push --branches origin",
    "git push origin 'refs/heads/*:refs/heads/*'",
    "gh api -iXDELETE repos/o/r/git/refs/heads/x",
    "gh api -X DELETE repos/o/r/git/refs/heads/feat/x -t graphql",   # graphql is a value here, not the endpoint
    "gh api -X DELETE repos/o/r/git/refs/heads/feat/x --jq graphql",
    "gh api -X DELETE repos/o/r/git/refs/heads/feat/x -p graphql",
    "gh api graphql -f query='mutation { deleteRepositoryRuleset(input: {}) { clientMutationId } }'",
    "git config --rename-section foo alias",           # git config foo.p 'push --force' first, then git p
    "git config --ren foo alias",                      # an abbreviated --rename-section
    "git config --rena foo alias",
    "git config --rename-section foo include",
    "git config --ren foo includeIf.onbranch:feat/x",
    "git -c include.path=/tmp/x.gitconfig status",     # the included file can define aliases or hooksPath
    "git -c includeIf.onbranch:feat/x.path=/tmp/x.gitconfig status",
    "git --config-env include.path=V status",
    "echo '{\"query\":\"mutation { deleteRef }\"}' | gh api graphql --input -",  # the query can't be read
    "gh api graphql -F query=@-",
    "gh api graphql -F query=@query.graphql",
    "gh api graphql --field=query=@query.graphql",
    "echo '{\"query\":\"mutation { deleteRef }\"}' | gh api https://api.github.com/graphql --input -",
    "gh api /graphql --input -",
    "gh api graphql -iFquery=@-",
    "gh api 'graphql?x=1' --input -",                  # still reaches GraphQL
    "gh api 'https://api.github.com/graphql?x=1' --input -",
    "gh api /GraphQL -F query=@q.graphql",
    "gh api -X DELETE repos/o/r/git/refs/heads/graphql",  # a branch named graphql is still a ref
    "git config --rename-section foo includeIf",       # a section, not a key: main allows it
    "GIT_CONFIG_GLOBAL=/tmp/x.gitconfig git p",        # another config file, like include.path
    "git_config_system=/tmp/x.gitconfig git status",
    "git config include.path /tmp/x.gitconfig",        # an included file can set hooksPath or aliases
    "git config includeIf.onbranch:feat/x.path /tmp/x.gitconfig",
    "git config core.hooksPath '2>/dev/null'",         # the guard can't tell a quoted value from a redirect
    "gh api repos/o/r/branches/main/rename -fnew_name=old-main",
    "gh api graphql -f query='mutation { deleteRef(input: {refId: \"x\"}) { clientMutationId } }'",
    "git push --mir origin",                           # git takes any unique prefix of a long option
    "git push --del origin feat/old",
    "git push --prune origin 'refs/heads/*:refs/heads/*'",
    "git push --prun origin",
    "git reset --har HEAD~1",
    "git clean --forc",
    "git add --al",
    "git branch --del main",
    "gh api --method=DELETE repos/o/r/git/refs/heads/x",
    "gh api -XDELETE repos/o/r/git/refs/heads/x",
    "gh api repos/o/r/git/refs/heads/main -f sha=abc -F force=true",
    # skipping hooks
    "git commit --no-verif -m x",
    "git commit --no-veri -m x",
    "git config core.hooksPath get",                   # "get" is the value here
    "git config core.hooksPath --local",               # so is "--local": after the key, git takes it as the value
    "git config core.hooksPath --show-origin",
    "git config core.hooksPath -z",
    "git config core.hooksPath --get",
    "git config --comment get core.hooksPath /dev/null",
    "git config --unset-a core.hooksPath",
    "git config --remove-section core",
    "git config --rename-section core x",
    "git config remove-section core",
    "git config rename-section core x",
    "git_config_parameters=\"'core.hooksPath=x'\" git commit -m x",
    "git_config_count=1 git_config_key_0=core.hooksPath git_config_value_0=x git commit -m x",
    "git config core.hooksPath /dev/null",
    "git config --local core.hooksPath .nohooks",
    "git config --file .git/config core.hooksPath x",
    "git config set core.hooksPath x",
    "git config --unset core.hooksPath",
    "git config CORE.HOOKSPATH x",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/dev/null'\" git commit -m x",
    "export GIT_CONFIG_PARAMETERS=\"'core.hooksPath=x'\"",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=x git commit -m x",
    "git commit -m x --no-verify",
    "git commit -n -m x",
    "git commit -anm x",
    "git -c core.hooksPath=/dev/null commit -m x",
    # discarding work
    "git reset --hard HEAD~1",
    "git -C . reset --hard",
    "git clean -fd",
    "git clean --force",
    "git checkout -- .",
    "git restore .",
    "git branch -D main",
    # sloppy staging
    "git add -A",
    "git add .",
    "git add --all",
    # merging outside merge_gate
    "gh pr merge 12 --squash",
    "\"gh\" pr merge 12",
    "/usr/bin/gh pr merge 12",
    "gh api -X PUT repos/o/r/pulls/7/merge",
    "gh api repos/o/r/merges -f base=main -f head=x",
    "gh api -X DELETE repos/o/r/branches/main/protection",
    "gh repo delete me/x",
    # recursive deletes of things that matter
    "rm -rf /",
    "rm -rf ~",
    "rm -rf ./",
    "rm -rf ..",
    'rm -rf "$HOME"',
    "rm --recursive --force ~",
    "sudo rm -rf /",
    # hidden inside other commands
    "npm test && git push --force",
    "echo ok; git reset --hard",
    "bash -c 'git push --force'",
    "echo $(git push --force)",
    "FOO=1 git push -f",
    "eval git push --force",
    # second-review cases
    "timeout 60 git push --force",
    "timeout 30 gh pr merge 7",
    "nice -n 10 git push --force",
    "bash -lc 'git push --force'",
    "git status  # check\ngit push --force",
    "HUSKY=0 git commit -m x",
    "export HUSKY=0",
    "SKIP=lint git commit -m x",
    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'",
    "git checkout main && git merge feat/x && git push",
    "git checkout main && git commit -m x",
    "git checkout main\ngit commit -m x",
    # Windows and macOS spellings of the same programs
    "git.exe push --force",
    "\"/c/Program Files/Git/cmd/git.exe\" push --force",
    "GH.EXE pr merge 7",
    # an empty argument must not split the command
    "git commit -m '' --no-verify",
    "git push origin \"\" --force",
    # other shells
    "fish -c 'git push --force'",
    "ksh -c 'git reset --hard'",
    "zsh --command 'git push -f'",
    "pwsh -c 'git push --force'",
    "powershell.exe -NoProfile -Command git push --force",
    "pwsh -NoLogo -com \"gh pr merge 7\"",
    "pwsh -EncodedCommand ZwBpAHQAIABwAHUAcwBoAA==",
    "powershell -enc ZwBpAHQA",
    "powershell \"git push --force\"",
    "powershell /c \"git push --force\"",
    "powershell -co \"git push --force\"",
    "powershell /enc ZwBpAHQA",
    "powershell -ExecutionPolicy Bypass \"git push --force\"",
    "pwsh -NoProfile -ep Bypass -Command git push --force",
    "cmd /c \"git push --force\"",
    "cmd.exe /C git reset --hard",
    "cmd //c \"git push --force\"",
    "cmd /s /c \"git reset --hard\"",
    # $(...) in the middle of a command
    "git push origin $(git branch --show-current) --force",
    "git commit -m $(cat msg.txt) --no-verify",
    "git reset $(git merge-base HEAD origin/main) --hard",
    "git push origin $(echo $(whoami)) --force",
    "git push origin `git branch --show-current` --force",
    "git reset HEAD~$((1+1)) --hard",
    "git push origin $(git log -1 --format='%h (x)') --force",
    "echo '$(' && git push --force && echo ')'",
    # grouped and control-flow commands
    "(cd sub && git push --force)",
    "{ git push --force; }",
    "npm test || { git reset --hard; }",
    "if true; then git push --force; fi",
    "for b in x; do git push -f; done",
    "! git push --force",
    # ...while a { } argument doesn't hide one
    "git commit -m '{}' --no-verify",
    "git commit -m \"}\" --no-verify",
    "git push origin } --force",
    "gh api --jq '{}' -X PUT repos/o/r/pulls/7/merge",
    # PowerShell payloads are parsed as PowerShell
    "pwsh -c 'git push --f`orce'",
    "pwsh -c 'npm test; if ($?) { git push --force }'",
    "pwsh -c 'rm -rf ~'",
    "powershell -Command 'rm -rf .'",
    # assignments after a keyword or wrapper
    "if x; then HUSKY=0 git commit -m y; fi",
    "env HUSKY=0 git commit -m y",
    "sudo HUSKY=0 git commit -m y",
    "then export HUSKY=0",
    # a quoted or escaped separator is an argument, not a separator
    "git commit -m ';' --no-verify",
    "git commit -m '(' --no-verify",
    "git commit -m \\; --no-verify",
    "git commit -m '#1' --no-verify",
    "git commit -m \"a && b\" --no-verify",
    "git push \\\n  --force",
    # subshells, and a substitution as an option's value
    "(git reset --hard)",
    "x=1; (cd sub; git push --force)",
    "git -C $(git rev-parse --show-toplevel) push --force",
    "git -C `git rev-parse --show-toplevel` push --force",
    "echo \"$(git push --force)\"",
    # cmd and PowerShell switches
    "cmd //cgit push --force",
    "cmd /cgit reset --hard",
    "pwsh -i -enc ZwBpAHQA",
    "echo 'git push --force' | pwsh -Command -",
    "echo 'git push --force' | pwsh -c -",
    "echo 'git push --force' | powershell -",
    "echo 'git push --force' | pwsh -File -",
    "pwsh -WorkingDirectory /c -Command 'Remove-Item -Recurse -Force ~'",
    "pwsh -wd /f -c 'git -C D:/repo push --force'",
    "pwsh -wd /c -enc ZwBpAHQA",
    "pwsh -wd /c -c 'git switch main' && git commit -m x",
    # comments end at the line's end, and only start a word
    "# Let's reset\ngit reset --hard origin/feat/x\n# That's it",
    "git push origin a#b --force",
    # a shell running what it reads from stdin
    "echo 'git push --force' | powershell -NoProfile",
    "echo 'git push --force' | bash",
    "echo 'git push --force' | sudo sh -s",
    "echo 'git push --force' | bash -o pipefail",
    "bash <<'EOF'\ngit push --force\nEOF",
    "bash <<< 'git push --force'",
    "pwsh -NoProfile <<'EOF'\ngit push --force\nEOF",
    # nested backticks, and $'...' quoting
    "echo `echo \\`git push --force\\``",
    "git commit -m $'fix: don\\'t crash' --no-verify",
    # from the review of #35: $$ is the PID, a carriage return isn't a blank, and every /c reading counts
    "echo $$'\\'; git push --force; echo '\\'",
    "git status\r# x; git push --force",
    "pwsh -wd /c -i /c /usr/bin/git push --force",
    # a value starting with - can be the next parameter
    "pwsh -wd -f -c 'git push --force'",
    "pwsh -ep -File -c 'git push --force'",
]

# blocked because they can't be parsed, the only cases allowed to block that way
UNPARSEABLE = ["echo $(git status"]

ALLOWED = [
    "git push -u origin feat/resume-export",
    "git push origin feat/x:feat/y",
    "git config core.hooksPath",                       # reading it is fine
    "git config --get core.hooksPath",
    "git config get core.hooksPath",
    "git config user.name 'Sam Dev'",
    "git --config-env user.name=GIT_NAME commit -m x",
    "echo GIT_CONFIG_PARAMETERS",
    "git config --global core.hooksPath",
    "git config --show-origin --get core.hooksPath",
    "git config --remove-section alias",
    "git --attr-source HEAD log -1",
    "git --no-pager log --oneline -5",
    "git push --follow-tags origin feat/x",
    "git push --dry-run origin feat/x",
    "git push --atomic origin feat/x",
    "git reset --soft HEAD~1",
    "git add --patch src/app.ts",
    "git commit --no-edit --amend",
    "gh api repos/o/r/git/refs/heads/main",
    "gh api repos/o/r/pulls -f title=x -f head=feat/x -f base=main",
    "gh api -i repos/o/r/branches",
    "gh api graphql -f query='query { viewer { login } }'",
    "gh api repos/o/r/git/refs/heads/x --jq .object.sha",
    "git config --get include.path",
    "git config --rename-section foo bar",
    "gh api repos/o/r/issues --input body.json",
    "gh api https://api.github.com/graphql -f query='query { viewer { login } }'",
    "gh api graphql -f query='{ repository(owner: \"o\", name: \"r\") { rulesets(first: 5) { totalCount } } }'",
    "git config --get-regexp alias",
    "git config --get alias.st",
    "for f in *.py; do skip=1; done",
    "skip=0; echo $skip",
    "git p4 sync",
    "git push",
    "git push origin feat/x",
    "git push -u origin feat/x && gh pr create --base main --title 'feat: x'",
    "gh pr create --title 'support -f flag' --body 'no --force here'",
    "git commit -m 'docs: explain why we never use --no-verify'",
    "git commit -m 'feat: add x'",
    "git commit -am 'fix: n things'",
    "git restore --staged .",
    "git add src/app.ts tests/app.test.ts",
    "git switch main && git pull --ff-only && git switch -c feat/new",
    "rm -rf node_modules",
    "rm -rf ./dist",
    "rm -f .",
    "gh pr diff 12",
    "gh pr view 12 --json state",
    "gh api repos/o/r/pulls/7",
    "python3 gates/merge_gate.py 12 --merge",
    "git status",
    "git log --oneline -5 | head -3",
    # heredoc messages are data, not commands (Claude Code writes commits and PRs this way)
    "git commit -m \"$(cat <<'EOF'\nfix: don't crash on empty input\n\nWe don't retry. git push --force is never used.\nEOF\n)\"",
    "git commit -F - <<'EOF'\nfeat: it's faster\nEOF",
    "gh pr create --title 'feat: x' --body \"$(cat <<'EOF'\n## What\nIt's faster; don't worry.\nEOF\n)\"",
    "git status  # just looking",
    "timeout 120 npm test",
    "git checkout -- src/app.ts",
    "git commit -m 'fix: 修复 naïve — ✓'",
    "pwsh -c 'git status'",
    "pwsh -NoProfile -ExecutionPolicy Bypass -File build.ps1",
    "fish -c 'git push -u origin feat/x'",
    "pwsh -File deploy.ps1 -e staging",
    "cmd /c \"git status\"",
    "cmd //c \"npm test\"",
    "git push -u origin $(git branch --show-current)",
    "(cd docs && git status)",
    "if [ -f x ]; then git push -u origin feat/x; fi",
    "echo $((1 + 2))",
    "git commit -m 'fix(guard): handle (cd x && y)'",
    # switching to main after the work is done
    "git commit -m 'feat: x' && git switch main",
    "git push && git checkout main && git pull",
    "powershell -c 'Get-ChildItem \"D:\\\"'",
    # quoted separators, parentheses and substitutions are text
    "git commit -m 'feat: a; b (c) #1 && d'",
    "git commit -m \"fix(guard): x; y\"",
    "git commit -m 'docs: never run $(git push --force)'",
    "(cd docs && npm test)",
    "echo $(( $(date +%s) - 60 ))",
    "export NODE_ENV=test && npm test",
    "env NODE_ENV=test npm test",
    "cmd /c dir",
    "pwsh -wd /c/Users/me/repo -c 'git status'",
    "pwsh -wd /c -c 'git status'",
    "git rebase origin/main  # (then git push --force-with-lease)",
    "git status # it's fine",
    "git commit -m $'fix: don\\'t crash'",
    "cat files.txt | bash scripts/each.sh",
    "echo hi | pwsh -c 'Write-Output $input'",
    "pwsh --version",
]

# commands from Claude Code's PowerShell tool
PS_BLOCKED = [
    "git push --force",
    "git push origin main",
    "git reset --hard; Write-Host done",
    "gh pr merge 7",
    # chaining and blocks (PowerShell 5.1 has no &&, so Claude chains with if ($?))
    "npm test; if ($?) { git push --force }",
    "npm test && git push --force",
    "try { git push -f } catch { }",
    "foreach ($b in 'x') { git push -f }",
    "& { git reset --hard }",
    "& git push --force",
    "& 'C:\\Program Files\\Git\\cmd\\git.exe' push --force",
    "git status # look\ngit push --force",
    "<# note #> git push --force",
    # escapes and quoting
    "git push --f`orce",
    "git push `\n  --force",
    "git push `\r\n  --force",
    "git push \"--force\"",
    "git push “--force”",
    "git commit -m 'it''s done' --no-verify",
    "git commit -m \"say `\"hi`\"\" --no-verify",
    "git commit -m ';' --no-verify",
    "git commit -m '(' --no-verify",
    "git commit -m @'\nfix: x\n'@ --no-verify",
    # subexpressions
    "git commit -m (Get-Content msg.txt) --no-verify",
    "git push origin $(git branch --show-current) --force",
    "Write-Output \"$(git push --force)\"",
    "Write-Output @\"\n$(git reset --hard)\n\"@",
    # skipping hooks
    "$env:HUSKY=0",
    "$env:GIT_CONFIG_PARAMETERS = \"'core.hooksPath=x'\"; git commit -m x",
    "git push origin :feat/old",
    "git config core.hooksPath .nohooks",
    "$env:HUSKY = '0'; git commit -m x",
    "$Env:husky=0",
    "$env:SKIP = 'lint'",
    # recursive deletes
    "Remove-Item -Recurse -Force ~",
    "Remove-Item C:\\ -Recurse",
    "ri -r -fo $env:USERPROFILE",
    "rm -Recurse .\\",
    "Remove-Item -Path $HOME\\* -Recurse:$true",
    # nested commands
    "iex 'git push --force'",
    "Invoke-Expression -Command \"git reset --hard\"",
    "'git push --force' | iex",
    "bash -c 'git push --force'",
    "cmd /c \"git push --force\"",
    "pwsh -c { git push --force }",
    "powershell -Command \"& {git push --force}\"",
    # an assignment still runs the command
    "$out = git push --force 2>&1",
    "$null = git reset --hard HEAD~1",
    "$res = gh pr merge 26 --squash",
    "$out=git push origin main",
    "[string]$a = $b = git push -f",
    "$log += git push --force",
    "rm -rf ~",
    "'git push --force' | powershell -NoProfile",
    "Write-Output \"$(if ($x) { git push --force })\"",
    "iex @'\ngit push --force\n'@",
    "pwsh -Command @'\ngit push --force\n'@",
    # variable names ignore case
    "Remove-Item -Recurse -Force $home",
    "Remove-Item -Recurse -Force $Home\\*",
    # environment variables set as items
    "Set-Item env:HUSKY 0",
    "si env:HUSKY 0",
    "Set-Content env:HUSKY 0",
    "Set-Item -Path Env:\\HUSKY -Value 0",
    # every form of assignment
    "[string[]]$x = git push --force",
    "${out} = git push --force",
    "$x[0] = git push --force",
    "$x, $y = git push --force",
    "$x ??= git push --force",
    "$a = $env:HUSKY = 0",
    # from the review of #36: a group holding a value is that value
    "git push origin ('--force')",
    "git push $('--force')",
    "git push @('--force')",
    "git push origin ('--force', 'x')",
    "& ('git') push --force",
    "Remove-Item -Recurse -Force ('~')",
    "git push origin ('main')",
    "pwsh -c ('git push --force')",
    "$env:HUSKY = (0)",
    "Set-Item env:HUSKY -Value:0",
]

PS_UNPARSEABLE = ["git commit -m 'unbalanced", "git push --force`"]

PS_ALLOWED = [
    "git status",
    "git push -u origin feat/x",
    "Get-ChildItem \"D:\\\"",
    "git commit -m 'docs: explain why we never use --no-verify'",
    "git commit -m \"fix: it's `\"quoted`\"\"",
    "git commit -m 'it''s fine'",
    "git commit -m 'a; b' -m '(c)'",
    "npm test; if ($?) { git push -u origin feat/x }",
    "if (Test-Path x) { git push -u origin feat/x }",
    "git commit -m @'\nfix: don't use git push --force\n'@",
    "git commit -m @\"\nfeat: x; y\n\"@",
    "git push -u origin $(git branch --show-current)",
    "Remove-Item -Recurse -Force node_modules",
    "Remove-Item -Force .",
    "rm .\\dist -Recurse",
    "$env:NODE_ENV = 'test'; npm test",
    "git log --oneline -5 | Select-Object -First 3",
    "Get-Content log.txt # git push --force",
    "Write-Output 'git push --force'",
    "pwsh -NoProfile -File build.ps1",
    "git commit -m 'feat: x' && git switch main",
    "$branch = git branch --show-current",
    "$out = git push -u origin feat/x 2>&1",
    "if ($x -eq 1) { git status }",
    # a group used as an argument is one word
    "Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE '.cache\\pip')",
    "Remove-Item -Recurse -Force (Join-Path . 'build')",
    "git commit -m (Get-Content msg.txt -Encoding utf8)",
    # $(...) runs only in expandable strings
    "gh pr create --title x --body @'\ndocs: $(git push --force) is now blocked\n'@",
    "git commit -m 'docs: never run $(git push --force)'",
    "Write-Output \"a $(git log --format=\"%h (x)\") b\"",
    # PowerShell's rm parameters aren't rm flags
    "rm -Force *",
    "rm * -Force",
    "rm -Force .",
    "rm -ErrorAction SilentlyContinue *",
    # PowerShell has no NAME=value prefix
    "$x = 'HUSKY=0'",
    "$line = \"SKIP=lint\"",
    "Set-Item env:NODE_ENV test",
    "Set-Content -Path notes.txt -Value env:SKIP",
]


def payload(cmd: str, tool: str = "Bash") -> bytes:
    # raw UTF-8, as Claude Code sends it
    return json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}, ensure_ascii=False).encode("utf-8")


def run_guard(cmd: str, tool: str = "Bash", cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOKS / "guard.py")], input=payload(cmd, tool),
                          capture_output=True, cwd=cwd)


def guard(cmd: str, tool: str = "Bash", cwd=None) -> int:
    return run_guard(cmd, tool, cwd).returncode


@pytest.fixture
def feature_repo(repo):
    subprocess.run(["git", "switch", "-q", "-c", "feat/x"], cwd=repo, check=True)
    return repo


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocks(cmd, feature_repo):
    res = run_guard(cmd, cwd=feature_repo)
    assert res.returncode == 2 and b"Could not parse" not in res.stderr  # blocked for what it does


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allows(cmd, feature_repo):
    assert guard(cmd, cwd=feature_repo) == 0


@pytest.mark.parametrize("cmd", PS_BLOCKED)
def test_blocks_powershell(cmd, feature_repo):
    res = run_guard(cmd, "PowerShell", feature_repo)
    assert res.returncode == 2 and b"Could not parse" not in res.stderr


@pytest.mark.parametrize("cmd", PS_ALLOWED)
def test_allows_powershell(cmd, feature_repo):
    assert guard(cmd, tool="PowerShell", cwd=feature_repo) == 0


@pytest.mark.parametrize("tool,cmd", [("Bash", c) for c in UNPARSEABLE] + [("PowerShell", c) for c in PS_UNPARSEABLE])
def test_blocks_what_it_cannot_parse(tool, cmd, feature_repo):
    res = run_guard(cmd, tool, feature_repo)
    assert res.returncode == 2 and b"Could not parse" in res.stderr


@pytest.mark.parametrize("cmd", ["gh pr merge 12 --squash", "gh api -X PUT repos/o/r/pulls/12/merge"])
def test_merge_block_gives_the_full_gate_command(cmd, feature_repo):
    res = run_guard(cmd, cwd=feature_repo)
    m = re.search(r'run bash "([^"]+)/run\.sh" gates/merge_gate\.py <PR> --merge', res.stderr.decode())
    assert res.returncode == 2 and m and os.path.isfile(os.path.join(m.group(1), "gates", "merge_gate.py"))


def test_powershell_tracks_branch(repo):
    assert guard("git commit -m x", tool="PowerShell", cwd=repo) == 2  # on main
    assert guard("git switch -c feat/y; if ($?) { git commit -m 'feat: y' }", tool="PowerShell", cwd=repo) == 0


def test_blocks_commit_and_bare_push_on_protected_branch(repo):
    assert guard("git commit -m x", cwd=repo) == 2  # repo fixture is on main
    assert guard("git push", cwd=repo) == 2


def test_tracks_branch_switch_inside_one_command(repo):
    assert guard("git switch -c feat/y && git commit -m 'feat: y'", cwd=repo) == 0
    assert guard("git switch main && git commit -m x", cwd=repo.parent) == 2
    assert guard("{ git commit -m x; }; git switch -c feat/y", cwd=repo) == 2  # the commit runs on main


def test_push_head_resolves_to_current_branch(repo, feature_repo=None):
    assert guard("git push -u origin HEAD", cwd=repo) == 2     # on main
    assert guard("git push origin HEAD:refs/heads/main", cwd=repo) == 2


def test_push_head_allowed_on_feature_branch(feature_repo):
    assert guard("git push -u origin HEAD", cwd=feature_repo) == 0


def test_checkout_to_feature_branch_then_commit_is_allowed(repo):
    subprocess.run(["git", "branch", "feat/z"], cwd=repo, check=True)
    assert guard("git checkout feat/z && git commit -m 'feat: z'", cwd=repo) == 0


def test_ignores_other_tools():
    assert guard("git push --force", tool="Read") == 0


def test_fails_closed():
    res = subprocess.run([sys.executable, str(HOOKS / "guard.py")], input="not json", text=True, capture_output=True)
    assert res.returncode == 2
    assert guard("git commit -m 'unbalanced") == 2


def test_blocks_when_the_check_crashes():
    # a crash would exit 1, which Claude Code treats as a non-blocking error and runs the command
    bad = json.dumps({"tool_name": "Bash", "tool_input": {"command": ["git", "push", "--force"]}}).encode()
    res = subprocess.run([sys.executable, str(HOOKS / "guard.py")], input=bad, capture_output=True)
    assert res.returncode == 2 and b"Could not read or check this command" in res.stderr


def test_blocks_commands_too_long_or_slow_to_check(monkeypatch):
    # if the hook times out, Claude Code runs the command
    res = run_guard("git status && " * 10_000)
    assert res.returncode == 2 and b"too long" in res.stderr
    for cmd, tool in (("cat <<" + "A" * 99_000, "Bash"), ("cat <<A\n" * 12_000, "Bash"), ("@'\n" * 30_000, "PowerShell"),
                      ("si" + " env:A" * 16_000 + "; git push --force", "PowerShell")):
        start = time.monotonic()
        run_guard(cmd, tool)
        assert time.monotonic() - start < 10, cmd[:10]  # no regex that rescans the rest of the command
    monkeypatch.syspath_prepend(str(HOOKS))
    import guard as g
    monkeypatch.setattr(g, "STARTED", time.monotonic())
    assert g.check("git status") is None
    monkeypatch.setattr(g, "STARTED", time.monotonic() - g.TIME_LIMIT - 1)
    for slow in (lambda: g.check("git status"), lambda: g.check("rm x", shell="powershell"), g.current_branch,
                 lambda: g.local_branch_exists("x")):
        with pytest.raises(TimeoutError):
            slow()


def hook_command() -> str:
    """The guard command from hooks.json, with the plugin path filled in the way Claude Code does it."""
    hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    return hooks[0]["hooks"][0]["command"].replace("${CLAUDE_PLUGIN_ROOT}", ROOT.as_posix())


def test_hook_matches_bash_and_powershell():
    matcher = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["matcher"]
    assert all(re.fullmatch(matcher, tool) for tool in ("Bash", "PowerShell"))


@pytest.mark.parametrize("tool,cmd,code", [("Bash", "git push --force", 2), ("Bash", "git push", 0),
                                           ("PowerShell", "git push --force", 2), ("PowerShell", "git push", 0)])
def test_hook_command_finds_python_on_this_os(feature_repo, tool, cmd, code):
    res = subprocess.run([BASH, "-c", hook_command()], input=payload(cmd, tool), capture_output=True,
                         cwd=feature_repo)
    assert res.returncode == code, res.stderr


def test_hook_blocks_when_python_is_missing(feature_repo, tmp_path):
    # any exit code but 2 is a non-blocking hook error, which would let every command through
    res = subprocess.run([BASH, str(ROOT / "run.sh"), "hooks/guard.py"], input=payload("git status"),
                         capture_output=True, cwd=feature_repo, env={**os.environ, "PATH": str(tmp_path)})
    assert res.returncode == 2 and b"needs Python 3.9+" in res.stderr
