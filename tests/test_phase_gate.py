import pytest

from conftest import git, run_gate

PLAN = """# Demo — Plan

## Progress
- [x] Phase 0 — Foundation
- [ ] Phase 1 — Data
- [ ] Phase 2 — Search

## Phase 0 — Foundation
**Goal:** scaffold
**Depends on:** none
### Done when
- `true`

## Phase 1 — Data
**Goal:** tables
**Depends on:** Phase 0
**Work in:** `src/db`
### Done when
- `test -f marker.txt`
- `true`

## Phase 2 — Search
**Goal:** search
**Depends on:** Phase 1
### Done when
- `git push --force`
"""


def setup(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_text(PLAN, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: plan")
    return {"CLAUDE_PROJECT_DIR": str(repo)}


def test_next(repo):
    code, out = run_gate("phase_gate.py", "next", cwd=repo, env=setup(repo))
    assert code == 0 and out["phase"] == "1"


def test_check_ok(repo):
    code, out = run_gate("phase_gate.py", "check", "1", cwd=repo, env=setup(repo))
    assert code == 0 and out["done_when"] == ["test -f marker.txt", "true"] and out["skills"] == []


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("line,expected", [
    ("**Skills:** `tdd`, `frontend-design:frontend-design`", ["tdd", "frontend-design:frontend-design"]),
    ("**Skills:** `apps/web:conventions`", ["apps/web:conventions"]),  # a skill scoped to a folder
    ("**Skills:** none", []),
    ("**Skills:**\n**Notes:** see `db/README.md`", []),  # an empty line doesn't borrow the next line's
])
def test_check_lists_the_phases_skills(repo, line, expected, eol):
    plan = PLAN.replace("**Work in:** `src/db`\n", f"**Work in:** `src/db`\n{line}\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_bytes(plan.replace("\n", eol).encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: plan")
    code, out = run_gate("phase_gate.py", "check", "1", cwd=repo, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert code == 0 and out["skills"] == expected and out["done_when"] == ["test -f marker.txt", "true"]


@pytest.mark.parametrize("line,error", [
    ("**Skills:** `tdd`\n**Skills:** `frontend-design:frontend-design`", "more than one **Skills:** line"),
    ("**Skills:** `tdd` (see `docs/x.md`)", "`docs/x.md`"),
    ("**Skills:** `tdd`, `npm test`", "`npm test`"),
])
def test_check_refuses_a_malformed_skills_line(repo, line, error):
    plan = PLAN.replace("**Work in:** `src/db`\n", f"**Work in:** `src/db`\n{line}\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_text(plan, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: plan")
    code, out = run_gate("phase_gate.py", "check", "1", cwd=repo, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert code == 1 and error in out["error"], out


def test_check_blocks_unmet_dependency(repo):
    code, out = run_gate("phase_gate.py", "check", "2", cwd=repo, env=setup(repo))
    assert code == 1 and "Phase 1" in out["error"]


def test_check_blocks_done_and_unknown(repo):
    env = setup(repo)
    assert run_gate("phase_gate.py", "check", "0", cwd=repo, env=env)[0] == 1
    assert run_gate("phase_gate.py", "check", "9", cwd=repo, env=env)[0] == 1


def test_check_blocks_dirty_tree(repo):
    env = setup(repo)
    (repo / "scratch.txt").write_text("x")
    code, out = run_gate("phase_gate.py", "check", "1", cwd=repo, env=env)
    assert code == 1 and "not clean" in out["error"]


def test_done_only_ticks_when_done_when_passes(repo):
    env = setup(repo)
    code, out = run_gate("phase_gate.py", "done", "1", cwd=repo, env=env)
    assert code == 1 and "- [ ] Phase 1" in (repo / "docs/PLAN.md").read_text(encoding="utf-8")
    (repo / "marker.txt").write_text("ok")
    code, out = run_gate("phase_gate.py", "done", "1", cwd=repo, env=env)
    assert code == 0 and out["marked_done"]
    assert "- [x] Phase 1 — Data" in (repo / "docs/PLAN.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_done_keeps_the_plans_line_endings(repo, eol):
    # editors and git settings differ per OS; ticking a box must stay a one-line diff either way
    env = setup(repo)
    plan = repo / "docs" / "PLAN.md"
    plan.write_bytes(PLAN.replace("\n", eol).encode("utf-8"))
    (repo / "marker.txt").write_text("ok")
    code, out = run_gate("phase_gate.py", "done", "1", cwd=repo, env=env)
    assert code == 0, out
    assert plan.read_bytes() == PLAN.replace("- [ ] Phase 1", "- [x] Phase 1").replace("\n", eol).encode("utf-8")


def test_verify_refuses_guard_blocked_commands(repo):
    code, out = run_gate("phase_gate.py", "verify", "2", cwd=repo, env=setup(repo))
    assert code == 1 and out["commands"][0]["status"] == "BLOCKED"


def test_example_plan_parses(repo):
    from conftest import ROOT
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_text((ROOT / "examples" / "PLAN.md").read_text(encoding="utf-8"), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: plan")
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    assert run_gate("phase_gate.py", "next", cwd=repo, env=env)[1]["phase"] == "0"
    code, out = run_gate("phase_gate.py", "check", "2", cwd=repo, env=env)
    assert code == 1 and "Phase 1" in out["error"]
    assert run_gate("phase_gate.py", "check", "0", cwd=repo, env=env)[1]["done_when"] == ["npm run lint", "npm test"]
    plan = repo / "docs" / "PLAN.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("- [ ] Phase 0", "- [x] Phase 0"), encoding="utf-8")
    git(repo, "commit", "-q", "-am", "docs: phase 0 done")
    assert run_gate("phase_gate.py", "check", "1", cwd=repo, env=env)[1]["skills"] == ["tdd"]


def test_missing_plan(repo):
    code, out = run_gate("phase_gate.py", "next", cwd=repo, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert code == 1 and "/kickoff" in out["error"]


def test_capital_x_and_prose_dependencies(repo):
    plan = PLAN.replace("- [x] Phase 0", "- [X] Phase 0").replace(
        "**Depends on:** Phase 0\n**Work in:**", "**Depends on:** Phase 0 (needs 3 tables)\n**Work in:**")
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_text(plan, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: plan")
    code, out = run_gate("phase_gate.py", "check", "1", cwd=repo, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert code == 0, out  # "3" in prose is not read as Phase 3; [X] counts as done


def test_slow_done_when_fails_with_json(repo, monkeypatch):
    plan = PLAN.replace("- `test -f marker.txt`\n- `true`", "- `sleep 5`")
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_text(plan, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: plan")
    code, out = run_gate("phase_gate.py", "verify", "1", cwd=repo,
                         env={"CLAUDE_PROJECT_DIR": str(repo), "BUILD_CREW_DONE_WHEN_TIMEOUT": "1"})
    assert code == 1 and out["commands"][0]["status"] == "FAIL"
