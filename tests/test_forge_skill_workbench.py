"""Routing, lazy package reads and reviewed edits use disposable profiles."""
import json
import re
from pathlib import Path

import pytest

from forge_integrations import IntegrationHub
from forge_skill_catalog import WORKFLOWS


@pytest.fixture
def skills(tmp_path):
    hub = IntegrationHub(tmp_path / "forge")
    yield hub
    hub.shutdown()


def starter(hub, identity):
    return next(item for item in hub.discover_skills() if item.get("library_id") == identity)


def test_24_workflows_have_compact_guidance_and_real_resources(skills):
    rows = skills.discover_skills()
    assert len(rows) == 24 and len({item["bundle"] for item in rows}) == 6
    assert len({item["description"] for item in rows}) == 24
    for row in rows:
        document = skills.read_skill(row["id"])
        assert not document["truncated"] and len(document["text"]) < 6000
        assert "## Workflow" in document["text"] and "## Verification and recovery" in document["text"]
        assert not row["manifest_error"] and row["manifest"]["schema_version"] == 1
        for relative in row["resources"]:
            result = skills.read_skill_resource(row["id"], relative)
            assert result["ok"] and result["text"]


@pytest.mark.parametrize("query,phase,expected,excluded", [
    ("Add dark mode to this app.", "implement", "frontend-implementation", "backend-apis"),
    ("Improve responsiveness and reduce local inference latency.", "discover", "performance-investigation", "data-migrations"),
    ("Fix the login button so sign-in works again.", "implement", "debug-errors", "release-packaging"),
    ("Continue the goal by verifying the implementation.", "verify", "testing", "research"),
    ("Design a polished responsive dashboard.", "plan", "ui-design", "backend-apis"),
])
def test_paraphrased_requests_select_workflow_for_the_phase(skills, query, phase, expected, excluded):
    result = skills.route_preview({"query": query, "phase": phase})
    chosen = {item["library_id"] for item in result["selected"]}
    assert expected in chosen and excluded not in chosen and len(chosen) <= 3
    assert all(item["selection_reason"] for item in result["selected"])
    assert skills.route_preview({"query": "Hello there"})["selected"] == []


def test_project_and_failed_outcomes_change_routing_without_enabling_tools(skills, tmp_path):
    folder = tmp_path / "app"; folder.mkdir(); (folder / "package.json").write_text("{}")
    current = skills.active_skill_instructions({"path": str(folder)}, query="Add account settings", context={"phase": "implement"})
    assert "frontend-implementation" in {item["skill"]["library_id"] for item in current}
    result = skills.route_preview({"query": "Investigate this", "phase": "discover", "outcomes": [{"name": "run_command", "ok": False}]})
    assert "debug-errors" in {item["library_id"] for item in result["selected"]}


def test_warm_rounds_do_not_reread_or_rewalk_unchanged_library(skills):
    skills.active_skill_instructions(query="Debug a crash and verify the fix")
    reads, scans = skills.skill_index.content_reads, skills.skill_index.directory_scans
    for _ in range(3):
        skills.active_skill_instructions(query="Debug a crash and verify the fix")
    assert skills.skill_index.content_reads == reads
    assert skills.skill_index.directory_scans == scans
    rule = starter(skills, "debug-errors")
    path = Path(rule["path"])
    path.write_text(path.read_text(encoding="utf-8") + "\nNew local rule.\n", encoding="utf-8")
    assert "New local rule" in skills.read_skill(rule["id"])["text"]
    assert skills.skill_index.content_reads == reads + 1


def test_new_nested_skill_is_seen_without_expiring_a_cache(skills):
    skills.discover_skills()
    folder = skills.root / "skills" / "team" / "nested"; folder.mkdir(parents=True)
    path = folder / "SKILL.md"; path.write_text("---\nname: team-rule\n---\nTeam guidance.")
    assert any(item["name"] == "team-rule" for item in skills.discover_skills())
    path.unlink()
    assert not any(item["name"] == "team-rule" for item in skills.discover_skills())


def test_500_skill_metadata_stays_warm_when_full_text_exceeds_cache(skills, tmp_path):
    project = tmp_path / "large-library"
    for index in range(500):
        folder = project / ".agents" / "skills" / str(index); folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: custom-" + str(index) + "\ndescription: A large optional guide.\n---\n" + "Reference material. " * 3500, encoding="utf-8")
    scope = {"path":str(project)}
    assert len(skills.discover_skills(scope)) == 500
    reads, scans = skills.skill_index.content_reads, skills.skill_index.directory_scans
    for _ in range(2):
        assert len(skills.discover_skills(scope)) == 500
    assert skills.skill_index.content_reads == reads
    assert skills.skill_index.directory_scans == scans
    assert skills.skill_index.cached_bytes <= 16 * 1024 * 1024
    assert all("text" not in item for item in skills.skill_index.descriptions.values())


def test_workbench_optimistic_edits_history_reset_restore_and_switches(skills):
    rule = starter(skills, "testing")
    skills.dispatch("skill_toggle", {"id": rule["id"], "enabled": False, "automatic": False})
    original = skills.dispatch("skill_preview", {"id": rule["id"]})
    data = {"id": rule["id"], "text": original["text"] + "\nMy focused checks.\n", "expected_revision": original["revision"]}
    edited = skills.dispatch("skill_edit", data)
    assert edited["ok"] and "My focused checks" in edited["text"]
    assert not edited["skill"]["enabled"] and not edited["skill"]["automatic"]
    assert not skills.dispatch("skill_edit", data)["ok"]
    history = skills.dispatch("skill_versions", {"id": rule["id"]})
    assert original["revision"] in {item["revision"] for item in history["versions"]}
    reset = skills.dispatch("skill_reset", {"id": rule["id"], "expected_revision": edited["revision"]})
    assert reset["ok"] and reset["text"] == original["text"]
    restored = skills.dispatch("skill_restore", {"id": rule["id"], "revision": edited["revision"], "expected_revision": reset["revision"]})
    assert restored["ok"] and restored["text"] == edited["text"]
    assert not skills.execute("skills_read", {"id": rule["id"]})["ok"]


def test_external_manifest_edit_invalidates_revision_and_prerequisites(skills):
    rule = starter(skills, "frontend-implementation")
    sidecar = Path(rule["path"]).with_name("forge-skill.json")
    manifest = json.loads(sidecar.read_text(encoding="utf-8")); manifest["requires_tools"] = ["custom_preview"]
    sidecar.write_text(json.dumps(manifest), encoding="utf-8")
    changed = skills.read_skill(rule["id"])
    assert changed["revision"] != rule["revision"]
    preview = skills.route_preview({"query": "Add a React component", "phase": "implement", "available_tools": ["read_file"]})
    assert rule["id"] not in {item["id"] for item in preview["selected"]}
    explicit = skills.route_preview({"query": "Hello", "available_tools": [], "explicit": [rule["id"]]})
    assert explicit["selected"][0]["id"] == rule["id"]  # Selection never grants its missing tool.
    assert not skills.dispatch("skill_edit", {"id": rule["id"], "expected_revision": rule["revision"], "text": "Stale edit"})["ok"]


def test_invalid_manifest_is_visible_and_does_not_auto_activate(skills):
    rule = starter(skills, "performance-investigation")
    Path(rule["path"]).with_name("forge-skill.json").write_text('{"schema_version":99}')
    current = starter(skills, "performance-investigation")
    assert current["manifest_error"]
    result = skills.route_preview({"query": "Optimize inference speed", "phase": "discover"})
    assert rule["id"] not in {item["id"] for item in result["selected"]}


@pytest.mark.parametrize("relative", ["../outside.txt", "/outside.txt", "C:\\secret.txt", "references/../../secret.txt", ".env", "references\\..\\secret.txt"])
def test_skill_resources_are_confined(skills, relative):
    rule = starter(skills, "testing")
    assert not skills.dispatch("skills_resource_read", {"id": rule["id"], "path": relative})["ok"]


def test_resources_respect_disabled_and_project_scope(skills, tmp_path):
    folder = tmp_path / "project" / ".agents" / "skills" / "project-guide"; folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: project-guide\n---\nLocal guide.")
    (folder / "reference.txt").write_text("Only this project.")
    project = {"path": str(tmp_path / "project")}
    rule = next(item for item in skills.discover_skills(project) if item["name"] == "project-guide")
    assert not skills.dispatch("skills_resource_read", {"id": rule["id"], "path": "reference.txt"})["ok"]
    assert skills.dispatch("skills_resource_read", {"id": rule["id"], "project": project, "path": "reference.txt"})["text"] == "Only this project."
    skills.dispatch("skill_toggle", {"id": rule["id"], "project": project, "enabled": False})
    assert not skills.dispatch("skills_resource_read", {"id": rule["id"], "project": project, "path": "reference.txt"})["ok"]


def test_imported_description_can_be_found_without_automatic_enable(skills, tmp_path):
    folder = tmp_path / "imported"; folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: unusual-name\ndescription: Diagnose WebSocket connection failures.\n---\nInspect the connection.")
    assert skills.dispatch("skill_install", {"source": str(folder)})["ok"]
    result = skills.execute("skills_search", {"query": "WebSocket connection", "limit": 3})
    assert any(item["name"] == "unusual-name" for item in result["skills"])
    assert not any(item["skill"]["name"] == "unusual-name" for item in skills.active_skill_instructions(query="WebSocket connection"))


def test_manifest_validation_rejects_executable_fields_without_touching_text(skills):
    rule = starter(skills, "testing")
    original = skills.read_skill(rule["id"])
    result = skills.dispatch("skill_edit", {"id": rule["id"], "text": "Should not save", "expected_revision": original["revision"],
                                            "manifest": {"schema_version": 1, "command": "arbitrary"}})
    assert not result["ok"] and skills.read_skill(rule["id"])["text"] == original["text"]


def test_browser_exclusion_and_verification_phase_do_not_trigger_unrelated_work(skills):
    result = skills.route_preview({"query": "Research primary sources without browser navigation", "phase": "research"})
    assert "research" in {item["library_id"] for item in result["selected"]}
    assert "browser-research" not in {item["library_id"] for item in result["selected"]}


def test_shipped_tool_examples_match_real_coordinator_schemas(skills):
    import jsonschema
    from agent_runtime import BUILTINS, WEB_TOOL
    from forge_runs import WORKFLOW
    from project_tools import ProjectTools
    from forge_builder import BuilderManager
    schemas = {item["function"]["name"]: item["function"]["parameters"]
               for item in ProjectTools.schemas() + BUILTINS + [WEB_TOOL] + WORKFLOW + skills.schemas() + BuilderManager.schemas(None)}
    for item in WORKFLOWS:
        match = re.fullmatch(r"(\w+)\((.*)\)", item["example"])
        assert match and match[1] in schemas, item["id"]
        schema = dict(schemas[match[1]])
        # Delegation IDs are resolved from live listed profiles, not a fixed test
        # enum. The illustrative placeholder must still obey the wire shape.
        jsonschema.validate(json.loads(match[2]), schema)
