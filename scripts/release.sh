#!/usr/bin/env bash
# Cut a release tag from the repository in the current directory. Usage: release.sh vMAJOR.MINOR.PATCH[-rc.N]
set -euo pipefail

die() { echo "release: $*" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: $0 vMAJOR.MINOR.PATCH[-rc.N]"
tag=$1
remote=${RELEASE_REMOTE:-origin}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository"
[ -z "$(git status --porcelain)" ] || die "working tree is dirty"
git remote set-head "$remote" --auto >/dev/null
trunk=$(git symbolic-ref --short "refs/remotes/$remote/HEAD")
trunk=${trunk#"$remote/"}
branch=$(git symbolic-ref --quiet --short HEAD || true)
[ "$branch" = "$trunk" ] || die "on '${branch:-detached HEAD}', not trunk '$trunk'"

git fetch --quiet --tags "$remote" "$trunk"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "$remote/$trunk")" ] || die "local $trunk differs from $remote/$trunk"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
manifest() { uv run --quiet --no-project --with packaging --with pyyaml python "$here/manifest.py" "$@"; }
caller=$(manifest caller) || die "no workflow here calls release-check.yml"
root=$(jq -r '.["working-directory"]' <<<"$caller")
check=$(jq -r '.["check-script"]' <<<"$caller")
manifest --root "$root" tag "$tag" >/dev/null || die "merge the version bump first"
if [ -n "$check" ]; then
  read -ra cmd <<<"$check"
  "./${cmd[0]}" "${cmd[@]:1}" "$tag" || die "$check refused $tag"
fi

prev=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)
echo "Repository: $(git remote get-url "$remote")"
echo "Commit:     $(git log -1 --format='%h %s')"
echo "Tag:        $tag${prev:+ (previous $prev)}"
[ -n "$prev" ] && git log --oneline "$prev..HEAD"
read -r -p "Create and push $tag? [y/N] " answer
[ "$answer" = y ] || [ "$answer" = Y ] || die "aborted"

git tag -a "$tag" -m "$tag"
git push "$remote" "refs/tags/$tag"
