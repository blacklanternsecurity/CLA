import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "manifest.py"


def run(root, *args):
    return subprocess.run([sys.executable, SCRIPT, "--root", root, *args], capture_output=True, text=True)


def outputs(result):
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


@pytest.fixture
def repo(tmp_path):
    def make(pyproject, files=None):
        (tmp_path / "pyproject.toml").write_text(pyproject)
        for name, body in (files or {}).items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return tmp_path

    return make


def test_matrix_follows_requires_python(repo):
    root = repo('[project]\nname = "x"\nversion = "1.0.0"\nrequires-python = ">=3.10,<3.13"\n')
    out = outputs(run(root, "matrix"))
    assert json.loads(out["versions"]) == ["3.10", "3.11", "3.12"]
    assert out["maturin"] == "false"


def test_matrix_reads_poetry_python(repo):
    root = repo('[tool.poetry]\nversion = "0.1.0"\n[tool.poetry.dependencies]\npython = "^3.11"\n')
    assert json.loads(outputs(run(root, "matrix"))["versions"])[0] == "3.11"


def test_maturin_version_comes_from_cargo(repo):
    root = repo(
        '[build-system]\nbuild-backend = "maturin"\n[project]\nname = "x"\ndynamic = ["version"]\nrequires-python = ">=3.12"\n',
        {"Cargo.toml": '[package]\nname = "x"\nversion = "2.0.0"\n'},
    )
    assert outputs(run(root, "matrix"))["maturin"] == "true"
    assert outputs(run(root, "tag", "v2.0.0"))["version"] == "2.0.0"


def test_hatch_version_path(repo):
    root = repo(
        '[build-system]\nbuild-backend = "hatchling.build"\n[project]\nname = "x"\ndynamic = ["version"]\n[tool.hatch.version]\npath = "x/__version__.py"\n',
        {"x/__version__.py": '__version__ = "1.2.3"\n'},
    )
    assert outputs(run(root, "tag", "v1.2.3"))["version"] == "1.2.3"


@pytest.mark.parametrize("declared", ["1.2.3rc4", "1.2.3-rc.4"])
def test_rc_tag_matches_either_spelling(repo, declared):
    root = repo(f'[project]\nname = "x"\nversion = "{declared}"\n')
    assert outputs(run(root, "tag", "v1.2.3-rc.4"))["prerelease"] == "true"


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3rc4", "v1.2.3-beta.1", "v1.2.4"])
def test_bad_tags_refused(repo, tag):
    root = repo('[project]\nname = "x"\nversion = "1.2.3"\n')
    assert run(root, "tag", tag).returncode != 0


def test_notes_extracts_one_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [1.1.0]\n- new\n\n## [1.0.0]\n- old\n")
    result = run(tmp_path, "notes", str(changelog), "1.1.0")
    assert result.stdout.strip() == "- new"
    assert run(tmp_path, "notes", str(changelog), "9.9.9").returncode != 0


def test_tracked_charts_must_match_tag(repo):
    root = repo('[project]\nname = "x"\nversion = "1.2.3"\n', {"helm/Chart.yaml": 'name: x\nversion: 1.2.3\nappVersion: "1.2.2"\n'})
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    result = run(root, "tag", "v1.2.3")
    assert result.returncode != 0 and "appVersion" in result.stderr
    (root / "helm/Chart.yaml").write_text('name: x\nversion: 1.2.3\nappVersion: "1.2.3"\n')
    assert outputs(run(root, "tag", "v1.2.3")) == {"version": "1.2.3", "semver": "1.2.3", "major": "1", "minor": "2", "prerelease": "false"}


def test_chart_under_nested_package_is_not_ours(repo):
    root = repo(
        '[project]\nname = "x"\nversion = "1.2.3"\n',
        {"server/pyproject.toml": '[project]\nname = "s"\nversion = "0.3.1"\n', "server/helm/Chart.yaml": "version: 0.3.1\n"},
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    assert outputs(run(root, "tag", "v1.2.3"))["version"] == "1.2.3"
