"""Windows workflow routing, real tool contracts and safe library upgrades."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

import jsonschema
import pytest

import forge_library
from forge_integrations import IntegrationHub
from forge_skill_catalog import CONTENT_FIELDS, LIBRARY_VERSION, STARTER_SKILLS, WORKFLOWS, markdown
from prompt_compiler import compile_skill_guidance, static_context_allowances
from scripts.generate_starter_skills import rendered_assets


NEW = {"windows-dotnet", "windows-native-cpp", "desktop-executable"}
BY_ID = {item["id"]: item for item in WORKFLOWS}


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get(self, reference):
        return self.values.get(reference)

    def put(self, value):
        reference = "synthetic-" + str(len(self.values))
        self.values[reference] = value
        return reference

    def delete(self, reference):
        self.values.pop(reference, None)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(IntegrationHub, "_refresh_catalog_source", lambda *_: (_ for _ in ()).throw(OSError("offline fixture")))
    value = IntegrationHub(tmp_path / "profile", vault=MemoryVault())
    yield value
    value.shutdown()
    if value.catalog_refresh_thread:
        value.catalog_refresh_thread.join(timeout=2)


@pytest.mark.parametrize("text,expected", [
    ("C++", "cplusplus"), ("c#", "csharp"), ("Build C# and C++.", "build csharp and cplusplus"),
    ("(C++), [C#]", "cplusplus csharp"), ("C++! C#?", "cplusplus csharp"),
    ("C", "c"), ("Plain C build", "plain c build"),
    ("XC++", "xc"), ("C#value", "c value"), ("C+++", "c"),
    ("file.c++", "file c"), ("C++.cpp", "c cpp"), ("C#.cs", "c cs"),
    ("folder/C++/file", "folder c file"), ("folder\\C#\\file", "folder c file"),
    ("name-C++-file", "name-c -file"),
])
def test_language_tokens_are_distinct_without_rewriting_paths_or_identifiers(text, expected):
    assert forge_library._words(text) == expected


def test_language_normalization_is_bounded_and_preserves_existing_word_aliases():
    query = "C# testing " + "x" * 24000 + " C++"
    result = forge_library._words(query)
    assert result.startswith("csharp test ") and "cplusplus" not in result
    assert result == forge_library._words(query)
    assert forge_library._words("Verified building features; plain C.") == "verify build feature plain c"


@pytest.mark.parametrize("query,identity", [
    ("Build a C# WPF application", "windows-dotnet"),
    ("Build a WinForms Windows Forms application", "windows-dotnet"),
    ("Fix the .csproj with its dotnet SDK", "windows-dotnet"),
    ("Build the .NET solution with its slnx", "windows-dotnet"),
    ("Build a Visual Studio C# desktop app", "windows-dotnet"),
    ("Build a dotnet app", "windows-dotnet"),
    ("Build a native C++ application", "windows-native-cpp"),
    ("Build the CMake project with its presets", "windows-native-cpp"),
    ("Build with CMake", "windows-native-cpp"),
    ("Build a cpp app", "windows-native-cpp"),
    ("Fix the Visual Studio C++ vcxproj with MSVC", "windows-native-cpp"),
    ("Package this desktop executable EXE", "desktop-executable"),
    ("Package a standalone app with PyInstaller", "desktop-executable"),
])
def test_windows_requests_select_the_specific_workflow_without_granting_tools(hub, query, identity):
    result = hub.route_preview({"query": query, "phase": "implement", "available_tools": []})
    selected = {item["library_id"] for item in result["selected"]}
    assert identity in selected
    assert len(selected) <= 3
    assert not any(item.get("requires_tools") for item in result["selected"])
    assert (NEW - {identity}).isdisjoint(selected)


@pytest.mark.parametrize("query", [
    "Good morning", "Build a React webpage", "Research primary sources", "Write a plain C library",
    "Check an unrelated SDK",
])
def test_unrelated_requests_do_not_select_windows_workflows(hub, query):
    result = hub.route_preview({"query": query, "phase": "implement"})
    assert NEW.isdisjoint(item["library_id"] for item in result["selected"])


def test_disabled_and_nonautomatic_windows_recipes_keep_explicit_selection_rules(hub):
    rows = {item["library_id"]: item for item in hub.discover_skills()}
    dotnet, native = rows["windows-dotnet"], rows["windows-native-cpp"]
    hub.dispatch("skill_toggle", {"id": dotnet["id"], "enabled": False})
    hub.dispatch("skill_toggle", {"id": native["id"], "automatic": False})
    result = hub.route_preview({"query": "Build a C# WPF and Visual Studio C++ vcxproj app", "phase": "implement"})
    assert {dotnet["id"], native["id"]}.isdisjoint(item["id"] for item in result["selected"])
    result = hub.route_preview({"query": "Hello", "available_tools": [], "explicit": [dotnet["id"], native["id"]]})
    assert [item["id"] for item in result["selected"]] == [native["id"]]
    assert not hub.execute("skills_read", {"id": dotnet["id"]})["ok"]


@pytest.mark.parametrize("identity", sorted(NEW | {'windows-computer'}))
def test_complete_windows_primary_recipe_fits_8k_context_with_lazy_support(identity):
    primary = BY_ID[identity]
    selected = [{"skill": {"id": str(index).zfill(20), "name": item["name"]}, "text": markdown(item)}
                for index, item in enumerate([primary, BY_ID["project-discovery"], BY_ID["testing"]])]
    budget = static_context_allowances({"context": 8192, "tokens": 2048})["skills_bytes"]
    compiled, active = compile_skill_guidance(selected, budget)
    assert selected[0]["text"] in compiled
    assert active == [item["skill"]["id"] for item in selected]
    assert all(item["skill"]["id"] in compiled for item in selected)
    assert len(compiled.encode("utf-8")) <= budget
    assert len(markdown(primary).split()) < 300


@pytest.mark.parametrize("identity", sorted(NEW | {'windows-computer'}))
def test_new_guidance_is_canonical_and_examples_stay_lazy(hub, identity):
    item = BY_ID[identity]
    folder = forge_library.ASSET_ROOT / "skills" / identity
    assert LIBRARY_VERSION == "2.4.1"
    assert item["version"] == ("2.3.0" if identity == 'windows-computer' else
        "2.2.1" if identity == 'desktop-executable' else "2.2.0")
    for relative, expected in rendered_assets(item).items():
        assert (folder / relative).read_bytes() == expected.encode("utf-8")
    assert all(not CONTENT_FIELDS.intersection(row) for row in STARTER_SKILLS)
    row = next(row for row in hub.discover_skills() if row["library_id"] == identity)
    document = hub.read_skill(row["id"])
    assert document["text"] == markdown(item) and not document["truncated"]
    assert item["examples"][0]["scenario"] not in document["text"]
    resource = hub.read_skill_resource(row["id"], "references/examples.md")
    assert resource["ok"] and item["examples"][0]["scenario"] in resource["text"]
    assert row["manifest"]["resources"] == ["references/examples.md"]


def test_every_windows_example_uses_actual_registered_tool_shapes():
    from forge_runs import WORKFLOW
    from project_tools import ProjectTools

    schemas = {item["function"]["name"]: item["function"]["parameters"]
               for item in ProjectTools.schemas() + WORKFLOW}
    calls = 0
    for identity in NEW:
        item = BY_ID[identity]
        example = re.fullmatch(r"(\w+)\((.*)\)", item["example"])
        assert example and example[1] in schemas
        jsonschema.validate(json.loads(example[2]), schemas[example[1]])
        assert set(item["recommended_tools"]) <= schemas.keys()
        for case in item["examples"]:
            for call in case["calls"]:
                assert call["tool"] in schemas
                jsonschema.validate(call["arguments"], schemas[call["tool"]])
                calls += 1
    assert calls == 26


@pytest.mark.parametrize('excluded,old_version,old_count', [
    (NEW | {'windows-computer'}, '2.1.0', 24), ({'windows-computer'}, '2.2.0', 27)], ids=['from24', 'from27'])
def test_upgrade_to_28_preserves_owned_edits_deletions_preferences_and_identity(tmp_path, monkeypatch, excluded, old_version, old_count):
    current_assets = forge_library.ASSET_ROOT
    old_assets = tmp_path / "old-bundle"
    shutil.copytree(current_assets, old_assets)
    for identity in excluded:
        shutil.rmtree(old_assets / "skills" / identity)
    old_skills = [item for item in STARTER_SKILLS if item["id"] not in excluded]
    root = tmp_path / "profile"
    root.mkdir()
    config = {}
    monkeypatch.setattr(forge_library, "ASSET_ROOT", old_assets)
    monkeypatch.setattr(forge_library, "STARTER_SKILLS", old_skills)
    monkeypatch.setattr(forge_library, "LIBRARY_VERSION", old_version)
    assert forge_library.reconcile_starters(root, config) == (True, [])
    assert len(config["library"]["files"]) == old_count
    base = root / "skills" / "forge-starter"
    edited = base / "frontend-implementation" / "SKILL.md"
    edited.write_text("Local desktop conventions.\n", encoding="utf-8")
    deleted = base / "testing" / "SKILL.md"
    deleted.unlink()
    resource = base / "code-review" / "references" / "examples.md"
    resource.write_text("Private reviewed examples.\n", encoding="utf-8")
    removed_resource = base / "preview-verification" / "references" / "examples.md"
    removed_resource.unlink()
    stable_id = forge_library.skill_identity(edited)
    config["skills"][stable_id] = {"enabled": False, "automatic": False}
    config = json.loads(json.dumps(config))
    monkeypatch.setattr(forge_library, "ASSET_ROOT", current_assets)
    monkeypatch.setattr(forge_library, "STARTER_SKILLS", STARTER_SKILLS)
    monkeypatch.setattr(forge_library, "LIBRARY_VERSION", LIBRARY_VERSION)
    assert forge_library.reconcile_starters(root, config) == (True, [])
    assert len(config["library"]["files"]) == 28 and config["library"]["version"] == "2.4.1"
    assert edited.read_text(encoding="utf-8") == "Local desktop conventions.\n"
    assert not deleted.exists() and not removed_resource.exists()
    assert resource.read_text(encoding="utf-8") == "Private reviewed examples.\n"
    assert forge_library.skill_identity(edited) == stable_id
    assert config["skills"][stable_id] == {"enabled": False, "automatic": False}
    for identity in NEW:
        assert (base / identity / "SKILL.md").read_bytes() == (current_assets / "skills" / identity / "SKILL.md").read_bytes()
    # A later restart also preserves new guides' independent local ownership.
    new_core = base / "windows-dotnet" / "SKILL.md"
    new_resource = base / "windows-native-cpp" / "references" / "examples.md"
    new_removed = base / "desktop-executable" / "references" / "examples.md"
    new_core.write_text("My .NET policy.\n", encoding="utf-8")
    new_resource.write_text("My native checks.\n", encoding="utf-8")
    new_removed.unlink()
    new_id = forge_library.skill_identity(new_core)
    config["skills"][new_id] = {"enabled": True, "automatic": False}
    config = json.loads(json.dumps(config))
    assert forge_library.reconcile_starters(root, config) == (False, [])
    assert new_core.read_text(encoding="utf-8") == "My .NET policy.\n"
    assert new_resource.read_text(encoding="utf-8") == "My native checks.\n" and not new_removed.exists()
    assert config["skills"][new_id] == {"enabled": True, "automatic": False}
