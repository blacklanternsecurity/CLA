import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from provenance import git, merged_into, previous_tag, violations  # noqa: E402

DATA = ["cloud_providers_v3.json", "README.md"]


def lookup(table):
    return lambda sha: table[sha]


def test_every_commit_through_a_merged_pr_passes():
    assert violations(["a", "b"], lambda s: True, lambda s: ["src/lib.rs"], []) == []


def test_direct_push_of_data_only_passes_when_allowed():
    files = lookup({"a": ["cloud_providers_v3.json", "README.md"]})
    assert violations(["a"], lambda s: False, files, DATA) == []


def test_direct_push_touching_code_fails_even_with_data_allowed():
    files = lookup({"a": ["cloud_providers_v3.json", "src/lib.rs"]})
    assert violations(["a"], lambda s: False, files, DATA) == [("a", ["src/lib.rs"])]


def test_direct_push_fails_when_no_paths_are_allowed():
    files = lookup({"a": ["cloud_providers_v3.json"]})
    assert violations(["a"], lambda s: False, files, []) == [("a", ["cloud_providers_v3.json"])]


def test_a_workflow_change_outside_a_pr_fails():
    files = lookup({"a": [".github/workflows/daily-update.yml"]})
    assert violations(["a"], lambda s: False, files, DATA) == [("a", [".github/workflows/daily-update.yml"])]


def test_empty_direct_commit_fails():
    assert violations(["a"], lambda s: False, lambda s: [], DATA) == [("a", [])]


def test_mixed_history_reports_only_the_bad_commits():
    merged = lookup({"pr1": True, "bot1": False, "evil": False, "pr2": True})
    files = lookup({"bot1": ["cloud_providers_v3.json"], "evil": ["Cargo.toml"]})
    assert violations(["pr1", "bot1", "evil", "pr2"], merged, files, DATA) == [("evil", ["Cargo.toml"])]


def commit(root, path, text, message):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message], cwd=root, check=True)
    return git(root, "rev-parse", "HEAD").strip()


def test_history_since_the_previous_release_tag(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "stable"], cwd=tmp_path, check=True)
    first = commit(tmp_path, "src/lib.rs", "1", "initial")
    assert previous_tag(tmp_path, "v*") is None  # first release: nothing to compare against
    subprocess.run(["git", "tag", "v1.0.0", first], cwd=tmp_path, check=True)
    subprocess.run(["git", "tag", "not-a-release", first], cwd=tmp_path, check=True)
    data = commit(tmp_path, "cloud_providers_v3.json", "{}", "daily data")
    code = commit(tmp_path, "src/lib.rs", "2", "sneaky code")
    assert previous_tag(tmp_path, "v*") == "v1.0.0"
    commits = git(tmp_path, "rev-list", "v1.0.0..HEAD").split()
    assert set(commits) == {data, code}

    def changed(sha):
        return [f for f in git(tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", sha).splitlines() if f]

    assert violations(commits, lambda s: False, changed, DATA) == [(code, ["src/lib.rs"])]
    assert violations(commits, lambda s: s == code, changed, DATA) == []


def test_a_pr_merged_into_another_branch_does_not_count():
    assert not merged_into([{"merged_at": "t", "base": {"ref": "dev"}}], "stable")
    assert not merged_into([{"merged_at": None, "base": {"ref": "stable"}}], "stable")
    assert merged_into([{"merged_at": "t", "base": {"ref": "dev"}}, {"merged_at": "t", "base": {"ref": "stable"}}], "stable")


def test_no_release_tag_at_all_is_a_first_release(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "stable"], cwd=tmp_path, check=True)
    commit(tmp_path, "a", "1", "initial")
    subprocess.run(["git", "tag", "not-a-release"], cwd=tmp_path, check=True)
    assert previous_tag(tmp_path, "v*") is None


def test_a_release_tag_not_reachable_from_head_is_an_error(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "stable"], cwd=tmp_path, check=True)
    commit(tmp_path, "a", "1", "initial")
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=tmp_path, check=True)
    commit(tmp_path, "b", "1", "side")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "stable"], cwd=tmp_path, check=True)
    with pytest.raises(subprocess.CalledProcessError):
        previous_tag(tmp_path, "v*")


def test_only_the_first_parent_line_is_walked(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "stable"], cwd=tmp_path, check=True)
    base = commit(tmp_path, "a", "1", "initial")
    subprocess.run(["git", "tag", "v1.0.0", base], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "dev"], cwd=tmp_path, check=True)
    side = commit(tmp_path, "b", "1", "dev work")
    subprocess.run(["git", "checkout", "-q", "stable"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "merge dev", "dev"], cwd=tmp_path, check=True)
    merge = git(tmp_path, "rev-parse", "HEAD").strip()
    commits = git(tmp_path, "rev-list", "--first-parent", "v1.0.0..HEAD").split()
    assert commits == [merge]
    assert side not in commits
