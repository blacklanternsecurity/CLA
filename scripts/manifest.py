"""Read release and test facts from a repository's own manifests. Run under uv with packaging and pyyaml."""

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

MANIFESTS = ("pyproject.toml", "Cargo.toml", "package.json")
RELEASE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
CARGO_TABLES = ("dependencies", "build-dependencies")


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


def unreleased(requirement):
    """A PEP 508 direct reference (`name @ url`) installs whatever the URL serves, never a registry release."""
    return "@" in requirement.split(";", 1)[0]


def git_specs(table):
    return [name for name, spec in table.items() if isinstance(spec, dict) and "git" in spec]


def git_dependencies(root):
    pyproject = load(root / "pyproject.toml")
    project = pyproject.get("project", {})
    found = [d for d in project.get("dependencies", []) if unreleased(d)]
    for group in project.get("optional-dependencies", {}).values():
        found += [d for d in group if unreleased(d)]
    tool = pyproject.get("tool", {})
    for name, source in tool.get("uv", {}).get("sources", {}).items():
        sources = source if isinstance(source, list) else [source]
        if any(isinstance(s, dict) and ("git" in s or "url" in s) for s in sources):
            found.append(name)
    for table in ("dependencies", "dev-dependencies"):
        found += git_specs(tool.get("poetry", {}).get(table, {}))
    cargo = load(root / tool.get("maturin", {}).get("manifest-path", "Cargo.toml"))
    tables = [cargo.get(t, {}) for t in CARGO_TABLES]
    tables += [target.get(t, {}) for target in cargo.get("target", {}).values() for t in CARGO_TABLES]
    tables.append(cargo.get("workspace", {}).get("dependencies", {}))
    for table in tables:
        found += git_specs(table)
    return found


def package_names(root):
    pyproject = load(root / "pyproject.toml")
    pypi = pyproject.get("project", {}).get("name") or pyproject.get("tool", {}).get("poetry", {}).get("name") or ""
    manifest = pyproject.get("tool", {}).get("maturin", {}).get("manifest-path", "Cargo.toml")
    crate = load(root / manifest).get("package", {}).get("name") or ""
    return pypi, crate


def cmd_release(args):
    declared = version(args.root)
    if not RELEASE_VERSION.fullmatch(declared):
        sys.exit(f"{declared} is not a MAJOR.MINOR.PATCH release version")
    parsed = Version(declared)
    for chart in charts(args.root):
        for field, value in chart_fields(chart).items():
            if value != declared:
                sys.exit(f"{chart} {field} {value} does not match manifest version {declared}")
    pinned = git_dependencies(args.root)
    if pinned:
        sys.exit(f"stable must pin released versions, not git or URL sources: {', '.join(pinned)}")
    pypi, crate = package_names(args.root)
    emit(version=declared, tag=f"v{declared}", major=str(parsed.major), minor=str(parsed.minor), pypi_name=pypi, crate_name=crate)


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


def cmd_images(args):
    listed = subprocess.run(["git", "ls-files", "-z", "--", "Dockerfile*"], cwd=args.root, capture_output=True, text=True, check=True)
    builds = []
    for name in sorted(n for n in listed.stdout.split("\0") if n and "/" not in n):
        variant = name.removeprefix("Dockerfile").removeprefix(".")
        suffix = f"-{variant}" if variant else ""
        builds.append({"file": name, "suffix": suffix, "ref": f"{args.image}:{args.version}{suffix}"})
    if not builds:
        sys.exit(f"no Dockerfile tracked at {args.root}")
    emit(builds=json.dumps(builds), images=json.dumps([b["ref"] for b in builds]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(required=True)
    sub.add_parser("matrix").set_defaults(func=cmd_matrix)
    sub.add_parser("release").set_defaults(func=cmd_release)
    notes = sub.add_parser("notes")
    notes.add_argument("changelog", type=Path)
    notes.add_argument("version")
    notes.set_defaults(func=cmd_notes)
    images = sub.add_parser("images")
    images.add_argument("image")
    images.add_argument("version")
    images.set_defaults(func=cmd_images)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
