"""Specific starter guidance stays schema-valid, lazy and safe to upgrade offline."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import jsonschema
import pytest

import forge_library
from forge_integrations import IntegrationHub
from forge_skill_catalog import CONTENT_FIELDS, LIBRARY_VERSION, STARTER_SKILLS, WORKFLOWS, markdown
from prompt_compiler import compile_skill_guidance, static_context_allowances
from scripts import generate_starter_skills


UPGRADED = {
    "efficient-tools", "cloud-collaboration", "frontend-implementation",
    "ui-design", "preview-verification", "code-review",
}
BY_ID = {item["id"]: item for item in WORKFLOWS}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(IntegrationHub, "_refresh_catalog_source", lambda *_: (_ for _ in ()).throw(OSError("offline fixture")))
    value = IntegrationHub(tmp_path / "profile")
    yield value
    value.shutdown()
    if value.catalog_refresh_thread:
        value.catalog_refresh_thread.join(timeout=2)


def test_every_shipped_recipe_manifest_and_lazy_example_matches_canonical_source():
    for item in WORKFLOWS:
        folder = forge_library.ASSET_ROOT / "skills" / item["id"]
        for relative, expected in generate_starter_skills.rendered_assets(item).items():
            assert (folder / relative).read_bytes() == expected.encode("utf-8"), (item["id"], relative)
    assert len(WORKFLOWS) == 27
    assert {item["id"] for item in WORKFLOWS if item.get("examples")} == UPGRADED | {
        "windows-dotnet", "windows-native-cpp", "desktop-executable",
    }
    assert {item["version"] for item in WORKFLOWS if item["id"] in UPGRADED} == {"2.1.0"}
    # The previously shipped guided-task recipe is not downgraded by generation.
    assert BY_ID["long-task-checkpoints"]["version"] == "2.0.1"


def test_catalog_discovery_never_injects_instruction_bodies(hub):
    assert all(not CONTENT_FIELDS.intersection(item) for item in STARTER_SKILLS)
    discovered = hub.dispatch("catalog_discover")
    assert discovered["ok"]
    for item in discovered["entries"]:
        assert not CONTENT_FIELDS.intersection(item)
    instructions = hub.active_skill_instructions(explicit=["efficient-tools"], query="")
    recipe = next(item for item in instructions if item["skill"]["library_id"] == "efficient-tools")
    assert recipe["text"] == markdown(BY_ID["efficient-tools"])
    assert "## Result packet" in recipe["text"]
    assert BY_ID["efficient-tools"]["examples"][0]["scenario"] not in recipe["text"]
    rule = recipe["skill"]
    lazy = hub.read_skill_resource(rule["id"], "references/examples.md")
    assert lazy["ok"] and BY_ID["efficient-tools"]["examples"][0]["scenario"] in lazy["text"]


@pytest.mark.parametrize("identity", sorted(UPGRADED))
def test_complete_primary_guidance_fits_8k_context_with_two_lazy_supporting_recipes(identity):
    primary = BY_ID[identity]
    others = [item for item in WORKFLOWS if item["id"] != identity][:2]
    selected = [
        {"skill": {"id": str(index).zfill(20), "name": item["name"]}, "text": markdown(item)}
        for index, item in enumerate([primary, *others])
    ]
    budget = static_context_allowances({"context": 8192, "tokens": 2048})["skills_bytes"]
    compiled, active = compile_skill_guidance(selected, budget)
    assert selected[0]["text"] in compiled
    assert all(item["skill"]["id"] in compiled for item in selected)
    assert active == [item["skill"]["id"] for item in selected]
    assert len(compiled.encode()) <= budget
    assert len(markdown(primary).split()) < 300


def test_worked_example_calls_validate_against_current_real_schemas():
    from forge_builder import BuilderManager
    from forge_runs import WORKFLOW
    from project_tools import ProjectTools

    schemas = {item["function"]["name"]: item["function"]["parameters"]
               for item in ProjectTools.schemas() + WORKFLOW + BuilderManager.schemas(None)}
    count = 0
    for identity in UPGRADED:
        item = BY_ID[identity]
        assert len(item["examples"]) == 3
        for case in item["examples"]:
            assert case["scenario"] and case["evidence"] and case["recovery"]
            for call in case["calls"]:
                assert call["tool"] in schemas
                jsonschema.validate(call["arguments"], schemas[call["tool"]])
                count += 1
        assert set(item["recommended_tools"]) <= schemas.keys()
    assert count == 26


def test_upgrade_preserves_modified_deleted_resources_preferences_and_identity(tmp_path, monkeypatch):
    current_assets = forge_library.ASSET_ROOT
    old_assets = tmp_path / "old-bundle"
    shutil.copytree(current_assets, old_assets)
    for identity in UPGRADED:
        folder = old_assets / "skills" / identity
        (folder / "SKILL.md").write_text("---\nname: " + identity + "\n---\nPrevious reviewed recipe.\n", encoding="utf-8")
        (folder / "references" / "examples.md").write_text("Previous lazy examples.\n", encoding="utf-8")
        sidecar = json.loads((folder / "forge-skill.json").read_text(encoding="utf-8"))
        sidecar["version"] = "2.0.0"
        (folder / "forge-skill.json").write_text(json.dumps(sidecar), encoding="utf-8")
    root = tmp_path / "profile"
    root.mkdir()
    config = {}
    monkeypatch.setattr(forge_library, "ASSET_ROOT", old_assets)
    monkeypatch.setattr(forge_library, "LIBRARY_VERSION", "2.0.1")
    changed, errors = forge_library.reconcile_starters(root, config)
    assert changed and not errors and config["library"]["version"] == "2.0.1"
    folders = {identity: root / "skills" / "forge-starter" / identity for identity in UPGRADED}
    identities = {identity: forge_library.skill_identity(folder / "SKILL.md") for identity, folder in folders.items()}
    config["skills"][identities["cloud-collaboration"]] = {"enabled": False, "automatic": False}
    config["skills"][identities["frontend-implementation"]] = {"enabled": True, "automatic": False}
    edited_core = folders["frontend-implementation"] / "SKILL.md"
    edited_core.write_text("My local frontend conventions.\n", encoding="utf-8")
    deleted_core = folders["ui-design"] / "SKILL.md"
    deleted_core.unlink()
    deleted_resource = folders["preview-verification"] / "references" / "examples.md"
    deleted_resource.unlink()
    edited_resource = folders["code-review"] / "references" / "examples.md"
    edited_resource.write_text("My local review evidence.\n", encoding="utf-8")
    # Restart uses the persisted prior ownership, not the newest bundle's hashes.
    config = json.loads(json.dumps(config))
    monkeypatch.setattr(forge_library, "ASSET_ROOT", current_assets)
    monkeypatch.setattr(forge_library, "LIBRARY_VERSION", LIBRARY_VERSION)
    changed, errors = forge_library.reconcile_starters(root, config)
    assert changed and not errors and config["library"]["version"] == LIBRARY_VERSION
    for identity in ("efficient-tools", "cloud-collaboration", "preview-verification", "code-review"):
        assert (folders[identity] / "SKILL.md").read_bytes() == (current_assets / "skills" / identity / "SKILL.md").read_bytes()
        assert forge_library.skill_identity(folders[identity] / "SKILL.md") == identities[identity]
    assert edited_core.read_text(encoding="utf-8") == "My local frontend conventions.\n"
    assert edited_resource.read_text(encoding="utf-8") == "My local review evidence.\n"
    assert not deleted_core.exists() and not deleted_resource.exists()
    assert config["skills"][identities["cloud-collaboration"]] == {"enabled": False, "automatic": False}
    assert config["skills"][identities["frontend-implementation"]] == {"enabled": True, "automatic": False}
    assert json.loads((folders["efficient-tools"] / "forge-skill.json").read_text(encoding="utf-8"))["version"] == BY_ID["efficient-tools"]["version"]
    assert (folders["frontend-implementation"] / "references" / "examples.md").read_bytes() == (current_assets / "skills" / "frontend-implementation" / "references" / "examples.md").read_bytes()
    assert forge_library.reconcile_starters(root, config) == (False, [])


@pytest.mark.parametrize("query,phase,identity", [
    ("Improve tool usage and context efficiency", "discover", "efficient-tools"),
    ("Ask an OpenRouter specialist to review this", "verify", "cloud-collaboration"),
    ("Build a React component with real button behavior", "implement", "frontend-implementation"),
    ("Polish the design and layout", "plan", "ui-design"),
    ("Exercise the live app preview and verify screenshots", "verify", "preview-verification"),
    ("Review code for security regressions", "verify", "code-review"),
])
def test_updated_workflows_route_without_granting_their_recommended_tools(hub, query, phase, identity):
    result = hub.route_preview({"query": query, "phase": phase, "available_tools": []})
    assert identity in {item["library_id"] for item in result["selected"]}
    assert len(result["selected"]) <= 3
    assert not any(item.get("requires_tools") for item in result["selected"])
    assert not hub.route_preview({"query": "Good morning", "phase": phase})["selected"]


def test_generator_check_does_not_write_and_unknown_ids_cannot_escape_asset_root(tmp_path, monkeypatch):
    assets = tmp_path / "assets" / "starter-library" / "skills" / "efficient-tools"
    shutil.copytree(forge_library.ASSET_ROOT / "skills" / "efficient-tools", assets)
    monkeypatch.setattr(generate_starter_skills, "ROOT", tmp_path)
    recipe = assets / "SKILL.md"
    recipe.write_text("Stale synthetic source asset.\n", encoding="utf-8")
    assert generate_starter_skills.main(["--check", "efficient-tools"]) == 1
    assert recipe.read_text(encoding="utf-8") == "Stale synthetic source asset.\n"
    with pytest.raises(SystemExit) as caught:
        generate_starter_skills.main(["../outside"])
    assert caught.value.code == 2
    assert recipe.read_text(encoding="utf-8") == "Stale synthetic source asset.\n"
    assert generate_starter_skills.main(["efficient-tools"]) == 0
    assert generate_starter_skills.main(["--check", "efficient-tools"]) == 0
