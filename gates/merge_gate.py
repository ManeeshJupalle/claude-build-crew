#!/usr/bin/env python3
"""merge_gate: decides if a PR may merge, and merges it. Code, not AI.

Usage:
  merge_gate.py <PR>                check only
  merge_gate.py <PR> --merge        check, and merge if every gate passes
  merge_gate.py <PR> --require-ci   treat "no CI configured" as a failure

Gates:
  state       open, not draft, targets the default branch, mergeable, no human "changes requested"
  review      latest pr-reviewer verdict is APPROVE, was posted by the account running
              this gate (not by outside commenters), and names the CURRENT head commit
  ci          every check passed; "no checks yet" counts as pending if the repo has workflows
  up_to_date  head contains the latest base commit (computed here, not left to repo settings)

The merge is pinned with --match-head-commit: if anything is pushed after the
checks, GitHub refuses it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import GateError, default_branch, emit, gh, gh_json, git, main_guarded  # noqa: E402

REVIEW_MARKER = "<!-- build-crew:review -->"
SLEEP = float(os.environ.get("BUILD_CREW_SLEEP", "10"))
FIELDS = ("number,title,state,isDraft,baseRefName,headRefOid,mergeable,mergeStateStatus,"
          "reviewDecision,comments")


def gate(ok: bool, detail: str) -> dict:
    return {"ok": ok, "detail": detail}


def load_pr(pr: str) -> dict:
    data = gh_json("pr", "view", pr, "--json", FIELDS)
    for _ in range(3):  # GitHub computes mergeability lazily
        if data.get("mergeable") != "UNKNOWN":
            break
        time.sleep(SLEEP)
        data = gh_json("pr", "view", pr, "--json", FIELDS)
    return data


def check_state(pr: dict, base: str) -> dict:
    if pr["state"] != "OPEN":
        return gate(False, f"PR is {pr['state']}")
    if pr["isDraft"]:
        return gate(False, "PR is a draft")
    if pr["baseRefName"] != base:
        return gate(False, f"base is '{pr['baseRefName']}', expected '{base}'")
    if pr["mergeable"] == "CONFLICTING":
        return gate(False, "merge conflicts: rebase the branch (pr-creator)")
    if pr["mergeable"] != "MERGEABLE":
        return gate(False, f"mergeable={pr['mergeable']}")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        return gate(False, "a human requested changes")
    return gate(True, "open, not draft, mergeable")


def check_review(pr: dict, me: str) -> dict:
    mine = [c for c in pr.get("comments", [])
            if REVIEW_MARKER in c.get("body", "") and (c.get("author") or {}).get("login") == me]
    if not mine:
        others = any(REVIEW_MARKER in c.get("body", "") for c in pr.get("comments", []))
        return gate(False, "no pr-reviewer verdict from @" + me + (" (ignored verdicts from other accounts)" if others else ""))
    body = mine[-1]["body"]
    verdict = re.search(r"^VERDICT:\s*(APPROVE|CHANGES_REQUESTED)\s*$", body, re.M)
    sha = re.search(r"^REVIEWED_SHA:\s*([0-9a-f]{40})\s*$", body, re.M)
    if not verdict or not sha:
        return gate(False, "latest review is malformed: needs one 'VERDICT: APPROVE|CHANGES_REQUESTED' line "
                           "and a 'REVIEWED_SHA: <40 hex>' line")
    if verdict.group(1) != "APPROVE":
        return gate(False, "latest verdict is CHANGES_REQUESTED")
    if sha.group(1) != pr["headRefOid"]:
        return gate(False, f"review was for {sha.group(1)[:7]}, head is now {pr['headRefOid'][:7]}: re-review")
    return gate(True, f"approved at {sha.group(1)[:7]}")


def repo_has_pr_workflows() -> bool:
    """True if some workflow runs on pull requests (push-only or deploy workflows never report PR checks)."""
    root = git("rev-parse", "--show-toplevel")
    wf = Path(root) / ".github" / "workflows" if root else None
    if not (wf and wf.is_dir()):
        return False
    return any(re.search(r"\bpull_request(_target)?\b", f.read_text(encoding="utf-8", errors="ignore"))
               for f in wf.glob("*.y*ml"))


def check_ci(pr_num: str, require_ci: bool) -> dict:
    res = gh("pr", "checks", pr_num, "--json", "name,bucket")
    try:
        checks = json.loads(res.stdout) if res.stdout.strip() else None
    except json.JSONDecodeError:
        checks = None
    if checks is None and "no checks" not in (res.stderr or "").lower():
        raise GateError(f"gh pr checks failed: {(res.stderr or '').strip()}")
    if not checks:
        if repo_has_pr_workflows():
            return gate(False, "no checks reported yet, but a workflow runs on pull_request: CI hasn't started, wait and retry")
        return gate(not require_ci, "NO CI CONFIGURED" + (" (required)" if require_ci else " (allowed)"))
    bad = [c["name"] for c in checks if c.get("bucket") in ("fail", "cancel")]
    pending = [c["name"] for c in checks if c.get("bucket") == "pending"]
    if bad:
        return gate(False, "failing: " + ", ".join(bad))
    if pending:
        return gate(False, "pending: " + ", ".join(pending) + " (wait: gh pr checks --watch)")
    return gate(True, f"{len(checks)} checks passed")


def check_up_to_date(pr: dict, base: str) -> dict:
    res = gh("api", f"repos/{{owner}}/{{repo}}/compare/{base}...{pr['headRefOid']}", "--jq", ".behind_by")
    behind = res.stdout.strip()
    if res.returncode != 0 or not behind.isdigit():
        return gate(False, f"could not compare with {base}: {(res.stderr or '').strip() or 'no output'}")
    if int(behind):
        return gate(False, f"{behind} commit(s) behind {base}: run `gh pr update-branch`, then re-review the new head")
    status = pr.get("mergeStateStatus", "UNKNOWN")
    if status not in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        return gate(False, f"mergeStateStatus={status}")
    return gate(True, f"contains latest {base}")


def merge_gate() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pr")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--require-ci", action="store_true")
    a = ap.parse_args()

    me = gh_json("api", "user")["login"]
    pr = load_pr(a.pr)
    base = default_branch()
    out: dict = {"gate": "merge", "pr": a.pr, "head_sha": pr["headRefOid"], "trusted_reviewer": me}
    gates = {
        "state": check_state(pr, base),
        "review": check_review(pr, me),
        "ci": check_ci(a.pr, a.require_ci),
        "up_to_date": check_up_to_date(pr, base),
    }
    out["gates"] = gates
    ok = all(g["ok"] for g in gates.values())
    merge_cmd = ["pr", "merge", a.pr, "--squash", "--delete-branch",
                 "--match-head-commit", pr["headRefOid"],
                 "--subject", f"{pr['title']} (#{pr['number']})"]

    if not (ok and a.merge):
        emit(out, ok)
    res = gh(*merge_cmd)
    after = gh_json("pr", "view", a.pr, "--json", "state,mergeCommit")
    out["merged"] = after.get("state") == "MERGED"
    out["merge_commit"] = (after.get("mergeCommit") or {}).get("oid")
    if res.returncode != 0:
        # gh also exits 1 when the merge worked and only deleting the local branch after it failed
        out["warning" if out["merged"] else "error"] = res.stderr.strip()
    emit(out, out["merged"])


if __name__ == "__main__":
    main_guarded("merge", merge_gate)
