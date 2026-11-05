---
description: Update one dependency — code finds outdated and vulnerable ones and proves the update, Claude makes it — then hand off to /ship. Usage: /deps [name]
argument-hint: [dependency name]
---

Update a dependency. Name (may be empty): $ARGUMENTS

1. **Find (Code).** Run `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/deps.py`. If its `verdict` is NOT VERIFIED or CHECK FAILED, show the failing `checks` and STOP: don't guess what's outdated.
2. **Pick one.** The dependency `$ARGUMENTS` names, or else the first in `dependencies`: vulnerable ones come first, then take a `patch` or `minor` bump. Skip `major` bumps unless `$ARGUMENTS` names that dependency, since breaking changes need a person or a planned phase. A vulnerable one whose only fix is a new major version counts as a major bump. Nothing left: say so, list the major bumps, and END.
3. `git status --porcelain` must be empty. If not, STOP with "Commit or stash first." Then `git switch <default> && git pull --ff-only && git switch -c chore/deps-<name>`.
4. **Update.** If it has a `fix_via`, do that (`npm audit fix`, never with `--force`). Otherwise go to `latest`, or for a vulnerable one where that's a major bump, the newest version in its `fix` list that keeps its major version. A vulnerable Python one that isn't `direct` comes with another package: update that one instead. Use the package manager so the lockfile changes with it: `npm install <name>@<version>`, or for Python change the pin in `requirements*.txt` or `pyproject.toml` and install it into the project's virtualenv. Change nothing else.
5. **Prove it (Code).** Run `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/checks.py`, then `deps.py` again: the dependency must be at the new version, with no advisories left on it.
6. **If either fails,** don't change application code to make the update fit. `git restore` the files you changed, then put the old version back where it's installed too: `npm ci`, or reinstall the restored requirements into the virtualenv. Run `deps.py` again and check the dependency is back at its old version. Then `git switch <default>`, `git branch -D chore/deps-<name>`, and report what failed so a person can plan the work.
7. **Report** the dependency, old → new version, advisories fixed, the checks result, and the major bumps left. Then: "Run `/ship`."

To run this regularly, use `/loop` or `/schedule`.
