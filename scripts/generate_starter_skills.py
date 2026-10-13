"""Render reviewed starter files from the canonical catalog, or check for drift.

Run from any directory: python scripts/generate_starter_skills.py --check
Optional positional IDs restrict generation/checking to those existing skills.
This script touches source assets only; it never reconciles installed libraries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge_skill_catalog import WORKFLOWS, example_markdown, manifest, markdown


def rendered_assets(item):
    return {
        "SKILL.md": markdown(item),
        "forge-skill.json": json.dumps(manifest(item), indent=2, ensure_ascii=False) + "\n",
        "references/examples.md": example_markdown(item),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report stale files without writing.")
    parser.add_argument("ids", nargs="*", help="Existing workflow IDs; omit to include the whole catalog.")
    arguments = parser.parse_args(argv)
    by_id = {item["id"]: item for item in WORKFLOWS}
    unknown = sorted(set(arguments.ids) - by_id.keys())
    if unknown:
        parser.error("Unknown workflow IDs: " + ", ".join(unknown))
    selected = list(dict.fromkeys(arguments.ids)) or list(by_id)
    stale = []
    for identity in selected:
        folder = ROOT / "assets" / "starter-library" / "skills" / identity
        for relative, content in rendered_assets(by_id[identity]).items():
            path = folder / relative
            expected = content.encode("utf-8")
            if path.is_file() and path.read_bytes() == expected:
                continue
            stale.append(identity + "/" + relative)
            if not arguments.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(expected)
    if arguments.check and stale:
        print("Stale starter assets:\n" + "\n".join(stale))
        return 1
    print(("Checked " if arguments.check else "Rendered ") + str(len(selected)) + " workflows; " + str(len(stale)) + " changed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
