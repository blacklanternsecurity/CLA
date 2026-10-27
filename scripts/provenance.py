"""Check that every commit being released arrived through a pull request merged into the release branch.

Judged by pull-request association, not by commit author, which anyone can set.
Only the first-parent line is walked: commits off it came in through the merge
that brought them. A pull request merged into another branch doesn't count.
A commit that didn't come through a merged pull request is allowed only if it
touches nothing but the paths the caller lists (cloudcheck's daily data files);
with no paths listed, any such commit fails the release.

Usage (in release-check.yml): provenance.py --root ROOT --repo OWNER/NAME [--base stable] [--allow PATH ...]
Needs git history (fetch-depth 0) and `gh` authenticated with contents and
pull-requests read.
"""

import argparse
import json
import subprocess
import sys


def violations(commits, merged_pr, changed_files, allowed):
    """Return (sha, files) for each commit that broke the rule.

    commits: shas to check. merged_pr(sha) -> bool. changed_files(sha) -> list of paths.
    allowed: paths a commit outside a merged pull request may touch.
    """
    found = []
    allowed = set(allowed)
    for sha in commits:
        if merged_pr(sha):
            continue
        files = changed_files(sha)
        outside = sorted(f for f in files if f not in allowed)
        if not allowed or outside or not files:
            found.append((sha, outside or files))
    return found


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def previous_tag(root, pattern):
    """The most recent release tag reachable from HEAD, or None when the repo has no release tag at all."""
    if not git(root, "tag", "--list", pattern).strip():
        return None
    return git(root, "describe", "--tags", "--abbrev=0", "--match", pattern, "HEAD").strip()


def merged_into(pulls, base):
    return any(pr.get("merged_at") and pr.get("base", {}).get("ref") == base for pr in pulls)


def gh_merged_pr(repo, base):
    def lookup(sha):
        out = subprocess.run(["gh", "api", f"repos/{repo}/commits/{sha}/pulls"], check=True, capture_output=True, text=True).stdout
        return merged_into(json.loads(out), base)

    return lookup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--repo", required=True, help="owner/name, for the pull-request lookups")
    parser.add_argument("--since-tag-pattern", default="v*")
    parser.add_argument("--base", default="stable", help="the branch a pull request must have been merged into")
    parser.add_argument("--allow", nargs="*", default=[], help="paths a commit outside a merged pull request may touch")
    args = parser.parse_args()

    tag = previous_tag(args.root, args.since_tag_pattern)
    if tag is None:
        print(f"::notice::no earlier {args.since_tag_pattern} tag: first release, provenance not checked")
        return
    commits = git(args.root, "rev-list", "--first-parent", f"{tag}..HEAD").split()
    print(f"checking {len(commits)} commit(s) since {tag}")

    def changed_files(sha):
        return [f for f in git(args.root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", sha).splitlines() if f]

    bad = violations(commits, gh_merged_pr(args.repo, args.base), changed_files, args.allow)
    for sha, files in bad:
        print(f"::error::{sha[:12]} didn't arrive through a pull request merged into {args.base} and touches {', '.join(files) or 'nothing'}")
    if bad:
        allowed = ", ".join(args.allow) or "no paths"
        sys.exit(f"{len(bad)} commit(s) since {tag} bypassed review outside the allowed paths ({allowed})")
    print(f"every commit arrived through a pull request merged into {args.base}, or touched only the allowed paths")


if __name__ == "__main__":
    main()
