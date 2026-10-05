"""Collect installed license files without embedding filesystem/user metadata."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import re
import sys

from packaging.requirements import Requirement


def is_notice(item):
    """Copy notices, never installed source or bytecode inside a licenses package."""
    path = Path(item)
    if path.suffix.lower() in {'.py', '.pyi', '.pyc', '.pyo', '.exe', '.dll', '.pyd', '.so', '.a', '.lib'}:
        return False
    if path.name.lower().startswith(('license', 'copying', 'notice', 'copyright', 'authors')):
        return True
    return (any(part.lower().startswith('license') for part in path.parts[:-1])
            and path.suffix.lower() in {'.txt', '.md', '.rst', '.html', '.htm', '.xml', '.rtf', '.json', ''})


def requirements(path, seen=None):
    seen = seen or set()
    path = Path(path).resolve()
    if path in seen:
        return []
    seen.add(path)
    result = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.extend(requirements(path.parent / line[3:].strip(), seen))
        else:
            result.append(Requirement(line))
    return result


def collect(output, root):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    queue = []
    for filename in ("requirements-dev.txt", "requirements-integrations.txt"):
        queue.extend(requirements(root / filename))
    visited = set()
    records = []
    while queue:
        requirement = queue.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        key = requirement.name.lower().replace("_", "-")
        if key in visited:
            continue
        visited.add(key)
        try:
            dist = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            records.append({"name": requirement.name, "status": "not installed"})
            continue
        directory = output / re.sub(r"[^A-Za-z0-9_.-]", "_", dist.metadata["Name"])
        directory.mkdir(exist_ok=True)
        copied = []
        for item in dist.files or []:
            if is_notice(item):
                source = Path(dist.locate_file(item))
                if source.is_file() and source.stat().st_size <= 2 * 1024 * 1024:
                    name = re.sub(r"[^A-Za-z0-9_.-]", "_", "__".join(item.parts[-3:]))
                    (directory / name).write_bytes(source.read_bytes())
                    copied.append(name)
        metadata_license = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See upstream license"
        if not copied:
            (directory / "LICENSE-METADATA.txt").write_text(metadata_license, encoding="utf-8")
            copied.append("LICENSE-METADATA.txt")
        records.append({"name": dist.metadata["Name"], "version": dist.version, "license": metadata_license, "files": copied})
        for raw in dist.requires or []:
            try:
                queue.append(Requirement(raw))
            except ValueError:
                continue
    modules = root / "frontend" / "node_modules"
    if modules.is_dir():
        candidates = list(modules.glob("*/package.json")) + list(modules.glob("@*/*/package.json"))
        for metadata in candidates:
            package = json.loads(metadata.read_text(encoding="utf-8"))
            directory = output / ("npm-" + re.sub(r"[^A-Za-z0-9_.-]", "_", package.get("name", metadata.parent.name)))
            copied = []
            for source in metadata.parent.iterdir():
                if source.is_file() and source.name.lower().startswith(("license", "copying", "notice")):
                    directory.mkdir(exist_ok=True)
                    (directory / source.name).write_bytes(source.read_bytes())
                    copied.append(source.name)
            if copied:
                records.append({"name": package.get("name"), "version": package.get("version"), "license": package.get("license"), "files": copied})
    # The interpreter's own license is already shipped with binary distributions.
    interpreter_license = Path(sys.base_prefix) / "LICENSE.txt"
    if interpreter_license.is_file():
        (output / "PYTHON-LICENSE.txt").write_bytes(interpreter_license.read_bytes())
    (output / "manifest.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dist/third-party-licenses")
    options = parser.parse_args()
    count = collect(Path(options.output), Path(__file__).resolve().parents[1])
    print(f"Collected notices for {count} distributions")
