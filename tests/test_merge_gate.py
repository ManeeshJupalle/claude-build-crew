import copy
import json
import sys
from pathlib import Path

import pytest

from conftest import run_gate

HEAD = "a" * 40
FAKE = Path(__file__).with_name("fake_gh.py")


def review(verdict="APPROVE", sha=HEAD, author="maneesh"):
    return {"author": {"login": author},
            "body": f"<!-- build-crew:review -->\n## Review\nVERDICT: {verdict}\nREVIEWED_SHA: {sha}\n"}


BASE = {
    "me": "maneesh",
    "pr": {"number": 7, "title": "feat: thing", "state": "OPEN", "isDraft": False, "baseRefName": "main",
           "headRefOid": HEAD, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
           "reviewDecision": "", "comments": [review()]},
    "checks": [{"name": "test", "bucket": "pass"}],
    "behind_by": 0,
}


def gate(tmp_path, state, *flags, cwd=None, fake=FAKE):
    sp = tmp_path / "gh.json"
    sp.write_text(json.dumps(state))
    # quoted, so paths with spaces stay one word each
    code, out = run_gate("merge_gate.py", "7", *flags, cwd=cwd or tmp_path,
                         env={"BUILD_CREW_GH": f'"{sys.executable}" "{fake}"', "FAKE_GH_STATE": str(sp),
                              "BUILD_CREW_SLEEP": "0"})
    return code, out, json.loads(sp.read_text())


def variant(**changes):
    s = copy.deepcopy(BASE)
    for k, v in changes.items():
        if k in s["pr"]:
            s["pr"][k] = v
        else:
            s[k] = v
    return s


def merged(st):
    return any(c[:2] == ["pr", "merge"] for c in st["calls"])


def test_all_gates_pass_and_merges(tmp_path):
    code, out, st = gate(tmp_path, copy.deepcopy(BASE), "--merge")
    assert code == 0 and out["merged"] is True
    merge_call = next(c for c in st["calls"] if c[:2] == ["pr", "merge"])
    assert "--match-head-commit" in merge_call and HEAD in merge_call


def test_merge_that_worked_is_reported_even_if_cleanup_failed(tmp_path):
    # gh v2.89 merges, then exits 1 if it can't delete the local branch, e.g. one checked out in another worktree
    err = "failed to delete local branch feat/x: cannot delete branch 'feat/x' used by worktree at 'D:/wt'"
    code, out, _ = gate(tmp_path, variant(merge_error=err), "--merge")
    assert code == 0 and out["merged"] is True and out["merge_commit"] == "f" * 40 and err in out["warning"]


def test_failed_merge_is_reported(tmp_path):
    code, out, _ = gate(tmp_path, variant(merge_error="Head branch was modified", pr_after_merge={}), "--merge")
    assert code == 1 and out["merged"] is False and "Head branch was modified" in out["error"]


OTHER = "b" * 40


@pytest.mark.parametrize("merge_error", [None, "Head branch was modified"])
def test_merged_at_another_head_fails(tmp_path, merge_error):
    # pushed to and merged outside the gate: gh skips its own merge, or GitHub refuses it
    after = {"state": "MERGED", "headRefOid": OTHER, "mergeCommit": {"oid": "e" * 40}}
    code, out, _ = gate(tmp_path, variant(pr_after_merge=after, merge_error=merge_error), "--merge")
    assert code == 1 and out["merged"] is True and OTHER in out["error"] and (merge_error or "") in out["error"]


def test_merge_error_survives_a_failed_view(tmp_path):
    state = variant(merge_error="Head branch was modified", pr_after_merge={}, view_error_after_merge="HTTP 502")
    code, out, _ = gate(tmp_path, state, "--merge")
    assert code == 1 and "Head branch was modified" in out["error"] and "HTTP 502" in out["error"]
    assert out["gates"]["review"]["ok"] and out["merged"] is None


def test_check_only_does_not_merge(tmp_path):
    code, out, st = gate(tmp_path, copy.deepcopy(BASE))
    assert code == 0 and "merged" not in out and not merged(st)


def test_fake_gh_in_a_path_with_spaces(tmp_path):
    fake = tmp_path / "with space" / FAKE.name
    fake.parent.mkdir()
    fake.write_bytes(FAKE.read_bytes())
    code, out, st = gate(tmp_path, copy.deepcopy(BASE), fake=fake)
    assert code == 0 and st["calls"]


@pytest.mark.parametrize("state,failing", [
    (variant(comments=[review(sha="b" * 40)]), "review"),                         # stale review
    (variant(comments=[review(verdict="CHANGES_REQUESTED")]), "review"),
    (variant(comments=[review(), review(verdict="CHANGES_REQUESTED")]), "review"),  # latest wins
    (variant(comments=[review(author="drive-by-user")]), "review"),               # forged by outsider
    (variant(comments=[review(verdict="CHANGES_REQUESTED"), review(author="x")]), "review"),  # can't override
    (variant(comments=[{"author": {"login": "maneesh"},
                        "body": "<!-- build-crew:review -->\nVERDICT: APPROVE | CHANGES_REQUESTED\n"
                                f"REVIEWED_SHA: {HEAD}\n"}]), "review"),          # unfilled template
    (variant(comments=[{"author": {"login": "maneesh"}, "body": "LGTM!"}]), "review"),
    (variant(isDraft=True), "state"),
    (variant(mergeable="CONFLICTING"), "state"),
    (variant(baseRefName="develop"), "state"),
    (variant(behind_by=3), "up_to_date"),
    (variant(mergeStateStatus="BLOCKED"), "up_to_date"),
    (variant(checks=[{"name": "test", "bucket": "fail"}]), "ci"),
    (variant(checks=[{"name": "test", "bucket": "pending"}]), "ci"),
])
def test_refuses(tmp_path, state, failing):
    code, out, st = gate(tmp_path, state, "--merge")
    assert code == 1
    assert out["gates"][failing]["ok"] is False
    assert not merged(st), "must never merge when a gate fails"


def test_no_ci_allowed_by_default_but_can_be_required(tmp_path):
    state = variant(checks=None)
    code, out, _ = gate(tmp_path, state)
    assert code == 0 and "NO CI" in out["gates"]["ci"]["detail"]
    code, out, _ = gate(tmp_path, state, "--require-ci")
    assert code == 1


def test_no_checks_yet_is_pending_when_repo_has_workflows(tmp_path, repo):
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on:\n  pull_request:\n")
    code, out, st = gate(tmp_path, variant(checks=None), "--merge", cwd=repo)
    assert code == 1 and "hasn't started" in out["gates"]["ci"]["detail"] and not merged(st)


def test_missing_gh_fails_closed_with_json(tmp_path):
    code, out = run_gate("merge_gate.py", "7", cwd=tmp_path, env={"BUILD_CREW_GH": "/nonexistent/gh"})
    assert code == 1 and "error" in out


def test_push_only_workflows_dont_block_forever(tmp_path, repo):
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text("on:\n  workflow_dispatch:\n  push:\n    tags: ['v*']\n")
    code, out, _ = gate(tmp_path, variant(checks=None), cwd=repo)
    assert code == 0 and "NO CI" in out["gates"]["ci"]["detail"]
