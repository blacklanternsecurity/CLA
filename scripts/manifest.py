"""Read release and test facts from a repository's own manifests. Run under uv with packaging available."""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from enum import Enum
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:-rc\.(?P<rc>\d+))?)$")
MANIFESTS = ("pyproject.toml", "Cargo.toml", "package.json")


class Backend(Enum):
    MATURIN = "maturin"
    HATCHLING = "hatchling.build"
    OTHER = None

    @classmethod
    def of(cls, pyproject):
        backend = pyproject.get("build-system", {}).get("build-backend")
        return next((b for b in cls if b.value == backend), cls.OTHER)


def load(path):
    return tomllib.loads(path.read_text()) if path.is_file() else {}


def cargo_version(path):
    data = load(path)
    version = data.get("package", {}).get("version")
    if isinstance(version, dict) or version is None:
        version = data.get("workspace", {}).get("package", {}).get("version")
    return version


def poetry_spec(spec):
    caret = re.fullmatch(r"\^(\d+)\.(\d+)", spec.strip())
    return f">={caret[1]}.{caret[2]},<{int(caret[1]) + 1}" if caret else spec


def python_spec(root):
    pyproject = load(root / "pyproject.toml")
    spec = pyproject.get("project", {}).get("requires-python")
    if spec is None:
        spec = poetry_spec(pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python", ""))
    if not spec:
        sys.exit(f"no requires-python in {root / 'pyproject.toml'}")
    return SpecifierSet(spec)


def stable_minors():
    listing = subprocess.run(
        ["uv", "python", "list", "--all-versions", "--only-downloads", "--output-format", "json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    minors = set()
    for build in json.loads(listing):
        if build["implementation"] != "cpython" or build["variant"] != "default":
            continue
        if Version(build["version"]).is_prerelease:
            continue
        parts = build["version_parts"]
        minors.add((parts["major"], parts["minor"]))
    return sorted(minors)


def python_versions(root):
    spec = python_spec(root)
    versions = [f"{major}.{minor}" for major, minor in stable_minors() if f"{major}.{minor}" in spec]
    if not versions:
        sys.exit(f"no released CPython satisfies {spec}")
    return versions


def version(root):
    pyproject = load(root / "pyproject.toml")
    project = pyproject.get("project", {})
    if "version" in project:
        return project["version"]
    tool = pyproject.get("tool", {})
    backend = Backend.of(pyproject)
    if backend is Backend.HATCHLING and "path" in tool.get("hatch", {}).get("version", {}):
        source = (root / tool["hatch"]["version"]["path"]).read_text()
        return re.search(r"""__version__\s*=\s*["']([^"']+)["']""", source)[1]
    if "version" in tool.get("poetry", {}):
        return tool["poetry"]["version"]
    cargo = root / tool.get("maturin", {}).get("manifest-path", "Cargo.toml")
    found = cargo_version(cargo)
    if found is None:
        sys.exit(f"no static version in {root}")
    return found


def emit(**outputs):
    lines = [f"{key}={value}" for key, value in outputs.items()]
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def charts(root):
    listed = subprocess.run(["git", "ls-files", "-z", "--", "*Chart.yaml"], cwd=root, capture_output=True, text=True)
    found = [root / name for name in listed.stdout.split("\0") if name]
    return [chart for chart in found if owned(root, chart)]


def owned(root, chart):
    """A chart beneath a nested package manifest versions with that package, not with root."""
    nested = [root / part for part in chart.parent.relative_to(root).parents][:-1]
    nested.insert(0, chart.parent)
    return not any((folder / name).is_file() for folder in nested if folder != root for name in MANIFESTS)


def chart_fields(path):
    found = {}
    for line in path.read_text().splitlines():
        field = re.match(r"""^(version|appVersion):\s*["']?([^"'\s#]+)""", line)
        if field:
            found[field[1]] = field[2]
    return found


def cmd_matrix(args):
    pyproject = load(args.root / "pyproject.toml")
    emit(
        versions=json.dumps(python_versions(args.root)),
        maturin=str(Backend.of(pyproject) is Backend.MATURIN).lower(),
    )


def cmd_tag(args):
    match = TAG.match(args.tag)
    if not match:
        sys.exit(f"tag {args.tag} is not vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N")
    declared = version(args.root)
    if Version(declared) != Version(match["version"]):
        sys.exit(f"tag {args.tag} does not match manifest version {declared}")
    for chart in charts(args.root):
        for field, value in chart_fields(chart).items():
            if value != match["version"]:
                sys.exit(f"tag {args.tag} does not match {chart} {field} {value}")
    major, minor, _ = match["version"].split("-")[0].split(".")
    emit(
        version=declared,
        semver=match["version"],
        major=major,
        minor=minor,
        prerelease=str(match["rc"] is not None).lower(),
    )


def cmd_notes(args):
    heading = re.compile(rf"^## \[?v?{re.escape(args.version)}\]?(\s|$)")
    section, inside = [], False
    for line in args.changelog.read_text().splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = bool(heading.match(line))
            continue
        if inside:
            section.append(line)
    if not inside:
        sys.exit(f"no '## [{args.version}]' section in {args.changelog}")
    print("\n".join(section).strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(required=True)
    sub.add_parser("matrix").set_defaults(func=cmd_matrix)
    tag = sub.add_parser("tag")
    tag.add_argument("tag")
    tag.set_defaults(func=cmd_tag)
    notes = sub.add_parser("notes")
    notes.add_argument("changelog", type=Path)
    notes.add_argument("version")
    notes.set_defaults(func=cmd_notes)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
