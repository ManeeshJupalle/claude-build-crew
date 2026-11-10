from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Tests never talk to real GitHub: an unusable gh makes gates fall back to local git.
os.environ.setdefault("BUILD_CREW_GH", "false")

ROOT = Path(__file__).resolve().parent.parent
GATES = ROOT / "gates"
HOOKS = ROOT / "hooks"
# Looked up on PATH: on Windows, a bare "bash" finds System32's WSL launcher before Git Bash.
BASH = shutil.which("bash")


def run_gate(name: str, *args: str, cwd: Path | None = None, env: dict | None = None) -> tuple[int, dict]:
    res = subprocess.run([sys.executable, str(GATES / name), *args], cwd=cwd, capture_output=True, text=True,
                         env={**os.environ, **(env or {})}, timeout=60)
    try:
        out = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"gate printed non-JSON:\nstdout={res.stdout}\nstderr={res.stderr}")
    return res.returncode, out


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(path: Path) -> Path:
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("# test\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "chore: init")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)
