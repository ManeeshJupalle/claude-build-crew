import json
import sys
from pathlib import Path

import pytest

from conftest import run_gate

FAKE = Path(__file__).with_name("fake_tool.py")
NPM = {"package.json": "{}"}
PY = {"requirements.txt": "requests==2.0.0\n"}

OUTDATED = {"left-pad": {"current": "1.0.0", "wanted": "1.0.2", "latest": "1.0.2"},
            "express": {"current": "4.17.0", "wanted": "4.21.0", "latest": "4.21.0"},
            "react": {"current": "17.0.2", "wanted": "17.0.2", "latest": "18.3.1"},
            "tiny": {"current": "0.1.0", "wanted": "0.1.0", "latest": "0.2.0"}}
AUDIT_CLEAN = {"auditReportVersion": 2, "vulnerabilities": {}}
AUDIT_VULN = {"auditReportVersion": 2, "vulnerabilities": {"express": {
    "name": "express", "severity": "high", "isDirect": True, "fixAvailable": True,
    "via": [{"source": 1096820, "url": "https://github.com/advisories/GHSA-qw6h-vgh9-j6wx"}, "body-parser"]}}}

# requests is what the project asked for; urllib3 comes with it; pip comes with the virtualenv
TOP = [{"name": "requests", "version": "2.0.0"}, {"name": "pip", "version": "24.0"}]
PIP_OUTDATED = [{"name": "requests", "version": "2.0.0", "latest_version": "2.32.3", "latest_filetype": "wheel"},
                {"name": "urllib3", "version": "1.23", "latest_version": "2.8.0", "latest_filetype": "wheel"},
                {"name": "pip", "version": "24.0", "latest_version": "26.3", "latest_filetype": "wheel"}]
VULN = {"id": "PYSEC-2018-28", "fix_versions": ["2.20.0"], "aliases": []}
PIP_AUDIT = {"dependencies": [
    {"name": "requests", "version": "2.0.0", "vulns": [VULN, VULN]},  # pip-audit repeats advisories
    {"name": "urllib3", "version": "1.23", "vulns": [{"id": "PYSEC-2019-133", "fix_versions": ["1.24.2", "1.3"]}]},
    {"name": "pip", "version": "24.0", "vulns": [{"id": "PYSEC-2026-196", "fix_versions": ["26.0"]}]}], "fixes": []}


def py(**changes):
    r = {"pip list --not-required": {"stdout": TOP}, "pip list --outdated": {"stdout": PIP_OUTDATED},
         "pip list --uptodate": {"stdout": [{"name": "requests", "version": "2.32.3"}]},
         "site": {"stdout": "/project/.venv/lib/site-packages\n"}, "pip-audit": {"stdout": PIP_AUDIT, "code": 1}}
    return {**r, **changes}


def calls(tmp_path):
    return json.loads((tmp_path / "fake.json").read_text(encoding="utf-8"))["calls"]


def gate(tmp_path, files, responses, fake_python=True, fake_audit=True, env=None):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    sp = tmp_path / "fake.json"
    sp.write_text(json.dumps(responses), encoding="utf-8")
    fake = f'"{sys.executable}" "{FAKE}"'
    return run_gate("deps.py", cwd=tmp_path, env={
        "CLAUDE_PROJECT_DIR": str(tmp_path), "FAKE_TOOL_STATE": str(sp), "BUILD_CREW_NPM": fake,
        **({"BUILD_CREW_PYTHON": fake} if fake_python else {}),
        **({"BUILD_CREW_PIP_AUDIT": fake} if fake_audit else {}), **(env or {})})


def by_name(out):
    return {d["name"]: d for d in out["dependencies"]}


def test_npm_outdated_and_bump_kinds(tmp_path):
    code, out = gate(tmp_path, NPM, {"outdated": {"stdout": OUTDATED, "code": 1}, "audit": {"stdout": AUDIT_CLEAN}})
    assert code == 0 and out["verdict"] == "OUTDATED"
    assert {n: d["bump"] for n, d in by_name(out).items()} == {
        "left-pad": "patch", "express": "minor", "react": "major", "tiny": "major"}  # 0.1 -> 0.2 may break


def test_npm_up_to_date(tmp_path):
    code, out = gate(tmp_path, NPM, {"outdated": {"stdout": ""}, "audit": {"stdout": AUDIT_CLEAN}})
    assert code == 0 and out["verdict"] == "UP TO DATE" and out["dependencies"] == []


def test_vulnerability_fails_and_comes_first(tmp_path):
    code, out = gate(tmp_path, NPM, {"outdated": {"stdout": OUTDATED, "code": 1}, "audit": {"stdout": AUDIT_VULN, "code": 1}})
    assert code == 1 and out["verdict"] == "VULNERABLE"
    first = out["dependencies"][0]
    assert first["name"] == "express" and first["vulnerable"] and first["bump"] == "minor"
    assert first["advisories"] == ["https://github.com/advisories/GHSA-qw6h-vgh9-j6wx"] and first["severity"] == "high"
    assert first["fix"] == [] and first["fix_via"] == "npm audit fix"


def test_npm_fix_is_a_list_like_python(tmp_path):
    vulns = {"minimist": {"isDirect": True, "fixAvailable": {"name": "minimist", "version": "1.2.8", "isSemVerMajor": True}},
             "qs": {"isDirect": False, "fixAvailable": {"name": "express", "version": "4.21.0", "isSemVerMajor": False}},
             "old": {"isDirect": True, "fixAvailable": False},
             "cookie": {"isDirect": False, "fixAvailable": {"name": "express", "version": "5.0.0", "isSemVerMajor": True}}}
    code, out = gate(tmp_path, NPM, {"outdated": {"stdout": ""}, "audit": {"stdout": {"vulnerabilities": vulns}, "code": 1}})
    deps = by_name(out)
    assert deps["minimist"]["fix"] == ["1.2.8"] and "fix_via" not in deps["minimist"]
    assert deps["qs"]["fix"] == [] and deps["qs"]["fix_via"] == "express@4.21.0" and deps["qs"]["direct"] is False
    assert "fix_major" not in deps["qs"] and deps["cookie"]["fix_major"] is True  # the fix is express 5: breaking
    assert deps["old"]["fix"] == [] and "fix_via" not in deps["old"]


def test_npm_workspaces_list_each_location(tmp_path):
    lodash = [{"current": "4.17.20", "wanted": "4.17.21", "latest": "4.17.21", "location": "node_modules/lodash"},
              {"current": "4.17.19", "wanted": "4.17.21", "latest": "4.17.21", "location": "packages/a/node_modules/lodash"}]
    code, out = gate(tmp_path, NPM, {"outdated": {"stdout": {"lodash": lodash}, "code": 1}, "audit": {"stdout": AUDIT_CLEAN}})
    assert code == 0 and out["verdict"] == "OUTDATED" and by_name(out)["lodash"]["bump"] == "patch"


def test_python_outdated_and_vulnerable(tmp_path):
    code, out = gate(tmp_path, PY, py())
    assert code == 1 and out["verdict"] == "VULNERABLE"
    deps = by_name(out)
    assert deps["requests"] == {"ecosystem": "python", "name": "requests", "current": "2.0.0", "latest": "2.32.3",
                                "direct": True, "bump": "minor", "vulnerable": True,
                                "advisories": ["PYSEC-2018-28"], "fix": ["2.20.0"]}
    # urllib3 only comes with requests: its vulnerability is listed, its plain update isn't the project's to make
    assert deps["urllib3"]["direct"] is False and "latest" not in deps["urllib3"]
    assert deps["urllib3"]["fix"] == ["1.3", "1.24.2"]  # by version, not alphabetically
    assert "pip" not in deps  # the virtualenv's own tools aren't the project's dependencies
    # the project installed into its own virtualenv would otherwise "require" every dependency
    assert ["-m", "pip", "list", "--not-required", "--exclude-editable", "--format=json"] in calls(tmp_path)


@pytest.mark.parametrize("files,reported", [
    ({"requirements.txt": "setuptools==65.5.0\n"}, True),                           # the project asks for it
    ({"pyproject.toml": '[project]\ndependencies = ["setuptools>=65"]\n'}, True),
    ({"pyproject.toml": '[build-system]\nrequires = ["setuptools>=61"]\n\n[project]\nname = "x"\n'}, False),
    ({"requirements.txt": "pip-audit==2.7\n"}, False),                              # not the same package
])
def test_venv_tools_count_when_the_project_names_them(tmp_path, files, reported):
    audit = {"dependencies": [{"name": "requests", "version": "2.32.3", "vulns": []},
                              {"name": "setuptools", "version": "65.5.0", "vulns": [{"id": "PYSEC-2022-43012"}]}]}
    code, out = gate(tmp_path, files, py(**{"pip list --outdated": {"stdout": []}, "pip-audit": {"stdout": audit, "code": 1}}))
    assert ("setuptools" in by_name(out)) is reported and (out["verdict"] == "VULNERABLE") is reported


def test_pip_audit_names_what_it_skipped(tmp_path):
    audit = {"dependencies": [{"name": "requests", "version": "2.32.3", "vulns": []},
                              {"name": "myproject", "skip_reason": "Dependency not found on PyPI"}]}
    code, out = gate(tmp_path, PY, py(**{"pip list --outdated": {"stdout": []}, "pip-audit": {"stdout": audit}}))
    assert code == 0 and out["checks"][1] == {"name": "pip-audit", "status": "PASS", "detail": "skipped myproject"}


def test_python_up_to_date(tmp_path):
    code, out = gate(tmp_path, PY, py(**{"pip list --outdated": {"stdout": []},
                                         "pip-audit": {"stdout": {"dependencies": [
                                             {"name": "requests", "version": "2.32.3", "vulns": []}]}}}))
    assert code == 0 and out["verdict"] == "UP TO DATE"


def test_pip_audit_missing_is_skipped_not_clean(tmp_path):
    code, out = gate(tmp_path, PY, py(**{"pip list --outdated": {"stdout": []}}), fake_audit=False, env={"PATH": ""})
    assert code == 0 and out["verdict"] == "PARTLY VERIFIED"
    assert {c["name"]: c["status"] for c in out["checks"]} == {"pip list --outdated": "PASS", "pip-audit": "SKIPPED"}


def test_no_virtualenv_is_not_verified(tmp_path):
    code, out = gate(tmp_path, PY, {}, fake_python=False)
    assert code == 1 and out["verdict"] == "NOT VERIFIED" and "all tools missing" in out["note"]


def test_no_project_is_not_verified(tmp_path):
    code, out = gate(tmp_path, {}, {})
    assert code == 1 and out["verdict"] == "NOT VERIFIED" and out["checks"] == []


def test_npm_missing_is_skipped(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    code, out = run_gate("deps.py", cwd=tmp_path, env={"CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": ""})
    assert code == 1 and out["verdict"] == "NOT VERIFIED"
    assert {c["status"] for c in out["checks"]} == {"SKIPPED"}


@pytest.mark.parametrize("responses", [
    {"outdated": {"stdout": "npm ERR! something broke", "code": 1}, "audit": {"stdout": AUDIT_CLEAN}},  # not JSON
    {"outdated": {"stdout": {"error": {"code": "E404"}}, "code": 1}, "audit": {"stdout": AUDIT_CLEAN}},  # npm's error
    {"outdated": {"stdout": "", "code": 1}, "audit": {"stdout": AUDIT_CLEAN}},                         # exit 1, nothing listed
    {"outdated": {"stdout": {}, "code": 2}, "audit": {"stdout": AUDIT_CLEAN}},                         # unexpected exit
    {"outdated": {"stdout": {"x": {"wanted": "1.0.1", "latest": "1.0.1"}}, "code": 1},
     "audit": {"stdout": AUDIT_CLEAN}},                                                               # not installed
    {"outdated": {"stdout": ""}, "audit": {"stdout": {"error": {"code": "ENOLOCK"}}, "code": 1}},     # no lockfile
    {"outdated": {"stdout": ""}, "audit": {"stdout": ""}},                                            # audit printed nothing
    {"outdated": {"stdout": ""}, "audit": {"stdout": AUDIT_CLEAN, "code": 2}},                        # audit's unexpected exit
    {"outdated": {"stdout": {"x": [{"current": "1.0.0", "latest": "1.0.1"}, {"latest": "1.0.1"}]}, "code": 1},
     "audit": {"stdout": AUDIT_CLEAN}},                                                               # a workspace not installed
])
def test_a_failed_npm_check_is_never_up_to_date(tmp_path, responses):
    code, out = gate(tmp_path, NPM, responses)
    assert code == 1 and out["verdict"] == "CHECK FAILED"
    assert any(c["status"] == "FAIL" and c["detail"] for c in out["checks"])


@pytest.mark.parametrize("changes", [
    {"pip list --outdated": {"code": 2, "stderr": "ERROR: network unreachable"}},
    {"pip list --not-required": {"stdout": "not json"}},
    {"pip list --uptodate": {"code": 1, "stderr": "ERROR: boom"}},
    {"pip list --uptodate": {"stdout": []}},                           # index unreachable: pip lists nothing, exits 0
    {"site": {"stdout": ""}},                                          # an empty --path would audit nothing
    {"pip-audit": {"stdout": {"dependencies": []}}},                   # saw no packages at all
    {"pip-audit": {"stdout": {"dependencies": [{"name": "requests", "skip_reason": "x"}]}}},  # skipped all of them
    {"pip-audit": {"code": 2, "stderr": "pip-audit: error: bad path"}},
    {"pip-audit": {"stdout": "Traceback (most recent call last):", "code": 1}},
])
def test_a_failed_python_check_is_never_up_to_date(tmp_path, changes):
    code, out = gate(tmp_path, PY, py(**{"pip list --outdated": {"stdout": []}, **changes}))
    assert code == 1 and out["verdict"] == "CHECK FAILED"
    assert any(c["status"] == "FAIL" and c["detail"] for c in out["checks"])
