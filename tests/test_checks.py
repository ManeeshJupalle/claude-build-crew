import os
import shutil

import pytest

from conftest import git, run_gate

needs_make = pytest.mark.skipif(not shutil.which("make"), reason="make is not installed")


def env(path):
    return {"CLAUDE_PROJECT_DIR": str(path)}


def test_nothing_ran_is_not_verified_and_fails(repo):
    git(repo, "switch", "-q", "-c", "feat/x")
    (repo / "app.py").write_text("print(1)\n")
    code, out = run_gate("checks.py", cwd=repo, env=env(repo))
    assert code == 1 and out["verdict"] == "NOT VERIFIED"


def test_docs_only_change_may_pass_without_checks(repo):
    git(repo, "switch", "-q", "-c", "docs/x")
    (repo / "GUIDE.md").write_text("# guide\n")
    code, out = run_gate("checks.py", cwd=repo, env=env(repo))
    assert code == 0 and out["verdict"] == "NOT VERIFIED" and "only changes docs" in out["note"]


@needs_make
def test_make_targets_pass_and_fail(tmp_path):
    (tmp_path / "Makefile").write_text("lint:\n\ttrue\ntest:\n\t@echo boom; false\n")
    code, out = run_gate("checks.py", "--markdown", cwd=tmp_path, env=env(tmp_path))
    status = {c["name"]: c["status"] for c in out["checks"]}
    assert code == 1 and status == {"lint": "PASS", "test": "FAIL"}
    assert "boom" in next(c for c in out["checks"] if c["name"] == "test")["output_tail"]
    assert "| test | `make test` | FAIL |" in out["markdown"]


@needs_make
def test_runs_in_project_root_not_cwd(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\ttest -f Makefile\n")
    (tmp_path / "sub").mkdir()
    code, out = run_gate("checks.py", cwd=tmp_path / "sub", env=env(tmp_path))
    assert code == 0 and out["checks"][0]["status"] == "PASS"


def test_all_skipped_is_not_a_pass(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    code, out = run_gate("checks.py", cwd=tmp_path, env={**env(tmp_path), "PATH": "/nonexistent"})
    assert code == 1 and all(c["status"] == "SKIPPED" for c in out["checks"])


def venv_pytest(root, pyproject=True):
    """A Python project whose virtualenv pytest passes."""
    scripts, exe = ("Scripts", ".exe") if os.name == "nt" else ("bin", "")
    (root / ".venv" / scripts).mkdir(parents=True)
    if pyproject:
        (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    fake = root / ".venv" / scripts / f"pytest{exe}"
    fake.write_text("#!/bin/sh\nexit 0\n")  # bash runs this stand-in on every OS, even when it's named .exe
    fake.chmod(0o755)
    return fake


def test_prefers_project_virtualenv(tmp_path):
    fake = venv_pytest(tmp_path)
    code, out = run_gate("checks.py", cwd=tmp_path, env=env(tmp_path))
    assert code == 0 and out["checks"][0]["cmd"].startswith(fake.as_posix())


def test_project_path_with_spaces(tmp_path):
    root = tmp_path / "my project"
    venv_pytest(root)
    code, out = run_gate("checks.py", cwd=root, env=env(root))
    assert code == 0, out


def test_requirements_txt_is_not_docs(repo):
    git(repo, "switch", "-q", "-c", "chore/deps")
    (repo / "requirements.txt").write_text("requests\n")
    code, out = run_gate("checks.py", cwd=repo, env=env(repo))
    assert code == 1


def detected(root):
    _, out = run_gate("checks.py", cwd=root, env={**env(root), "PATH": "/nonexistent"})  # list, don't run
    return [c["cmd"] for c in out["checks"]]


@pytest.mark.parametrize("name", ["test_x.py", "x_test.py", "conftest.py"])
def test_finds_pytest_suite_without_packaging_files(tmp_path, name):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / name).write_text("def test_x():\n    pass\n")
    assert detected(tmp_path) == ["pytest -q"]


def test_pytest_suite_prefers_project_virtualenv(tmp_path):
    fake = venv_pytest(tmp_path, pyproject=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    code, out = run_gate("checks.py", cwd=tmp_path, env=env(tmp_path))
    assert code == 0 and out["checks"][0]["cmd"].startswith(fake.as_posix())


def test_tests_folder_alone_is_not_python(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "app.test.js").write_text("test('x', () => {})\n")
    assert detected(tmp_path) == []


def test_makefile_outranks_a_bare_pytest_suite(tmp_path):
    (tmp_path / "Makefile").write_text("lint:\n\ttrue\ntest:\n\ttrue\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    assert detected(tmp_path) == ["make lint", "make test"]


def test_js_repo_with_a_python_suite_keeps_only_npm_checks(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    assert detected(tmp_path) == ["npm run test"]
