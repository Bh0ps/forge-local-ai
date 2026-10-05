"""Offline setup, durable preferences, discovery and selected import contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from forge_integrations import IntegrationHub
from forge_library import ASSET_ROOT, PRESET_CATALOGS, STARTER_SKILLS, select_skills
REAL_REFRESH_CATALOG = IntegrationHub._refresh_catalog_source


@pytest.fixture
def hub(tmp_path, monkeypatch):
    # Ordinary discovery tests stay offline; refresh failures and imports are
    # exercised separately with real response shapes and pinned blob hashes.
    monkeypatch.setattr(IntegrationHub, "_refresh_catalog_source", lambda *_: (_ for _ in ()).throw(OSError("offline")))
    value = IntegrationHub(tmp_path / "forge")
    yield value
    value.shutdown()
    if value.catalog_refresh_thread:
        value.catalog_refresh_thread.join(timeout=2)


def test_new_profile_has_ten_real_reviewed_toggleable_skills(hub):
    skills = hub.discover_skills()
    assert len(skills) == 10
    assert {item["library_id"] for item in skills} == {item["id"] for item in STARTER_SKILLS}
    assert all(item["enabled"] and item["automatic"] and item["builtin"] and item["license"] == "MIT" for item in skills)
    for item in skills:
        assert Path(item["path"]).is_file()
        assert (Path(item["path"]).parent / "LICENSE").read_bytes() == (ASSET_ROOT / "LICENSE").read_bytes()
        assert "permission" in hub.read_skill(item["id"])["text"].lower()


def test_restart_preserves_edits_disabled_and_automatic_choices(hub):
    item = next(skill for skill in hub.discover_skills() if skill["library_id"] == "debug-errors")
    path = Path(item["path"])
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\nMy local debugging rule.\n", encoding="utf-8")
    assert hub.dispatch("skill_toggle", {"id": item["id"], "enabled": False, "automatic": False})["ok"]
    root = hub.root
    hub.shutdown()
    reopened = IntegrationHub(root)
    try:
        changed = next(skill for skill in reopened.discover_skills() if skill["id"] == item["id"])
        assert not changed["enabled"] and not changed["automatic"] and changed["modified"]
        assert path.read_text(encoding="utf-8").endswith("My local debugging rule.\n")
        assert len(reopened.discover_skills()) == 10
    finally:
        reopened.shutdown()


def test_deleted_starter_is_not_resurrected_on_restart(hub):
    skill = hub.discover_skills()[0]
    Path(skill["path"]).unlink()
    hub.shutdown()
    reopened = IntegrationHub(hub.root)
    try:
        assert not Path(skill["path"]).exists()
        assert len(reopened.discover_skills()) == 9
    finally:
        reopened.shutdown()


def test_upgrade_refreshes_only_pristine_starter_content(hub, tmp_path, monkeypatch):
    assets = tmp_path / "updated-assets"
    shutil.copytree(ASSET_ROOT, assets)
    edited = next(item for item in hub.discover_skills() if item["library_id"] == "research")
    pristine = next(item for item in hub.discover_skills() if item["library_id"] == "testing")
    Path(edited["path"]).write_text("My research customization", encoding="utf-8")
    for slug in ("research", "testing"):
        source = assets / "skills" / slug / "SKILL.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nUpdated bundled guidance.\n", encoding="utf-8")
    monkeypatch.setattr("forge_library.ASSET_ROOT", assets)
    hub.shutdown()
    reopened = IntegrationHub(hub.root)
    try:
        assert Path(edited["path"]).read_text(encoding="utf-8") == "My research customization"
        assert Path(pristine["path"]).read_text(encoding="utf-8").endswith("Updated bundled guidance.\n")
    finally:
        reopened.shutdown()


def test_preview_disabled_does_not_grant_model_access(hub):
    skill = hub.discover_skills()[0]
    assert hub.dispatch("skill_toggle", {"id": skill["id"], "enabled": False})["ok"]
    disabled = next(item for item in hub.discover_skills() if item["id"] == skill["id"])
    assert disabled["automatic"]  # Omitting the second toggle preserves it.
    assert not hub.dispatch("skill_read", {"id": skill["id"]})["ok"]
    assert hub.dispatch("skill_preview", {"id": skill["id"]})["ok"]
    assert not hub.execute("skills_read", {"id": skill["id"]})["ok"]
    assert not hub.dispatch("skill_toggle", {"id": "unknown", "enabled": True})["ok"]


def test_automatic_skills_are_relevant_bounded_and_disabled_respected(hub):
    assert hub.active_skill_instructions(query="Hello there") == []
    selected = hub.active_skill_instructions(query="Debug a crash and test the fix")
    assert 1 <= len(selected) <= 3
    assert "debug-errors" in {item["skill"]["library_id"] for item in selected}
    assert len(selected) < len(hub.discover_skills())
    debug = next(skill for skill in hub.discover_skills() if skill["library_id"] == "debug-errors")
    hub.dispatch("skill_toggle", {"id": debug["id"], "enabled": False})
    assert debug["id"] not in {item["skill"]["id"] for item in hub.active_skill_instructions(explicit=[debug["id"]], query="debug crash")}
    explicit = hub.active_skill_instructions(explicit=["Research with sources"], query="hello")
    assert [item["skill"]["library_id"] for item in explicit] == ["research"]
    assert sum(len(item["text"]) for item in hub.active_skill_instructions(explicit=[skill["id"] for skill in hub.discover_skills()])) <= 24000


def test_disabled_plugin_is_not_automatically_or_explicitly_activated(hub):
    plugin = hub.dispatch("plugin_install", {"source": "builtin:research"})["plugin"]
    skill = next(item for item in hub.discover_skills() if item.get("plugin_id") == plugin["id"])
    hub.dispatch("skill_toggle", {"id": skill["id"], "enabled": True, "automatic": True})
    hub.dispatch("plugin_toggle", {"id": plugin["id"], "enabled": False})
    assert skill["id"] not in {item["skill"]["id"] for item in hub.active_skill_instructions(explicit=[skill["id"]], query="research")}
    assert not hub.dispatch("skill_preview", {"id": skill["id"]})["ok"]


def test_removed_skill_during_activation_does_not_interrupt_run(hub, monkeypatch):
    original = hub.read_skill
    def read(identity, project=None):
        item = next(skill for skill in hub.discover_skills() if skill["id"] == identity)
        if item["library_id"] == "debug-errors":
            Path(item["path"]).unlink()
        return original(identity, project)
    monkeypatch.setattr(hub, "read_skill", read)
    result = hub.active_skill_instructions(query="debug a crash and test")
    assert not any(item["skill"]["library_id"] == "debug-errors" for item in result)
    assert any(item["skill"]["library_id"] == "testing" for item in result)


def test_identical_starter_and_plugin_content_is_not_injected_twice(hub):
    hub.dispatch("plugin_install", {"source": "builtin:research"})
    selected = hub.active_skill_instructions(explicit=["research", "Research with sources"])
    assert len(selected) == 1


def test_discover_offline_is_populated_searchable_and_has_no_installs(hub, monkeypatch):
    monkeypatch.setattr(hub, "_refresh_presets", lambda: None)
    result = hub.dispatch("catalog_discover")
    assert result["ok"]
    assert len(result["entries"]) == 21
    assert len(result["sources"]) == 3
    assert len([item for item in result["entries"] if item["installed"]]) == 10
    assert not hub.config["plugins"] and not hub.config["servers"]
    builtin = hub.dispatch("catalog_discover", {"query": "debug", "kind": "skill", "category": "Coding"})["entries"]
    assert any(item["source"] == "builtin:skill/debug-errors" for item in builtin)
    assert all(item["kind"] == "skill" and item["category"] == "Coding" for item in builtin)
    assert hub.dispatch("catalog_discover", {"query": "there-is-no-such-package"})["entries"] == []
    assert not hub.dispatch("catalog_discover", {"kind": "executable"})["ok"]
    assert all("/tree/" in item["source"] for item in result["entries"] if not item["builtin"])
    assert not any(name in " ".join(item["source"] for item in result["entries"]) for name in ("skills/docx", "skills/pdf", "skills/pptx", "skills/xlsx"))


def test_refresh_failure_keeps_the_offline_supported_entries(hub):
    before = hub.dispatch("catalog_discover")["entries"]
    if hub.catalog_refresh_thread:
        hub.catalog_refresh_thread.join(timeout=2)
    result = hub.dispatch("catalog_discover", {"refresh": True})
    assert len(result["entries"]) == len(before)
    assert len(result["errors"]) == 2
    assert all("Cached entries remain" in item["error"] for item in result["errors"])
    assert all(item["state"] == "error" for item in result["sources"] if item["id"] != "forge-starter")


def test_official_refresh_only_uses_selected_paths_and_pinned_commit(hub, monkeypatch):
    sha = "a" * 40
    payloads = [json.dumps({"sha": sha}).encode(), json.dumps({"plugins": [
        {"name": "example-skills", "source": "./", "skills": ["./skills/frontend-design", "./skills/pdf", "./skills/xlsx"]},
    ]}).encode()]
    urls = []
    def fetch(url, *_):
        urls.append(url)
        return payloads.pop(0)
    monkeypatch.setattr(hub, "_public_bytes", fetch)
    catalog = REAL_REFRESH_CATALOG(hub, PRESET_CATALOGS[1])
    assert [entry["path"] for entry in catalog["entries"]] == ["skills/frontend-design"]
    assert "/" + sha + "/" in urls[1]
    assert sha in catalog["entries"][0]["source"]
    assert catalog["entries"][0]["license"] == "Review upstream license"
    assert not catalog["entries"][0]["reviewed"]


def test_starter_bundle_is_idempotent_and_preserves_edits_and_toggles(hub):
    first = hub.dispatch("plugin_install", {"source": "builtin:review"})["plugin"]
    assert (Path(first["path"]) / "LICENSE").is_file()
    assert len(list(Path(first["path"]).rglob("SKILL.md"))) == 3
    skill = next(Path(first["path"]).rglob("SKILL.md"))
    skill.write_text("Local plugin edit", encoding="utf-8")
    hub.dispatch("plugin_toggle", {"id": first["id"], "enabled": False})
    second = hub.dispatch("plugin_install", {"source": "builtin:review"})["plugin"]
    assert second["id"] == first["id"] and not second["enabled"]
    assert skill.read_text(encoding="utf-8") == "Local plugin edit"
    assert len(hub.config["plugins"]) == 1


def _git_item(path, raw, mode="100644"):
    return {"path": path, "type": "blob", "mode": mode, "size": len(raw),
            "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}


def test_selected_github_folder_skips_huge_repo_and_preserves_licenses(hub, monkeypatch):
    commit = "b" * 40
    skill_raw = b"---\nname: Imported skill\n---\nContent."
    license_raw = b"Apache License 2.0"
    files = {"skills/example/SKILL.md": skill_raw, "LICENSE": license_raw}
    tree = [_git_item(path, raw) for path, raw in files.items()]
    tree.extend(_git_item("unrelated/" + str(index), b"outside") for index in range(6000))
    urls = []
    def fetch(url, *_):
        urls.append(url)
        if "/git/trees/" in url:
            return json.dumps({"tree": tree, "truncated": False}).encode()
        key = url.split("/" + commit + "/", 1)[1]
        return files[key]
    monkeypatch.setattr(hub, "_public_bytes", fetch)
    result = hub.dispatch("skill_install", {"source": "https://github.com/example/repo/tree/" + commit + "/skills/example"})
    assert result["ok"], result
    package = Path(result["package"]["path"])
    assert (package / "SKILL.md").read_bytes() == skill_raw
    assert (package / "UPSTREAM-LICENSE").read_bytes() == license_raw
    assert len(urls) == 3 and not any("codeload" in url for url in urls)
    installed = next(item for item in hub.discover_skills() if item["name"] == "Imported skill")
    assert installed["source"] == result["package"]["source"]


def test_selected_folder_retains_ancestor_licenses_and_rejects_case_collisions(hub, monkeypatch):
    sha = "d" * 40
    files = {"package/skills/example/SKILL.md": b"# skill", "package/LICENSE": b"MIT"}
    def fetch(url, *_):
        if "/git/trees/" in url:
            return json.dumps({"tree": [_git_item(path, raw) for path, raw in files.items()]}).encode()
        return files[url.split("/" + sha + "/", 1)[1]]
    monkeypatch.setattr(hub, "_public_bytes", fetch)
    source = "https://github.com/example/repo/tree/" + sha + "/package/skills/example"
    result = hub.dispatch("skill_install", {"source": source})
    assert result["ok"]
    root = Path(result["package"]["path"])
    assert "UPSTREAM-LICENSE-package" in hub._package_licenses(root)
    files["package/skills/example/skill.md"] = b"Windows collision"
    assert not hub.dispatch("skill_install", {"source": source})["ok"]


@pytest.mark.parametrize("issue", ["truncated", "hash", "symlink", "size"])
def test_selected_github_folder_rejects_incomplete_or_unsafe_content(hub, monkeypatch, issue):
    commit = "c" * 40
    raw = b"# skill"
    item = _git_item("skills/example/SKILL.md", raw)
    if issue == "symlink": item["mode"] = "120000"
    if issue == "size": item["size"] = 100_000_000
    if issue == "hash": item["sha"] = "0" * 40
    def fetch(url, *_):
        if "/git/trees/" in url:
            return json.dumps({"tree": [item], "truncated": issue == "truncated"}).encode()
        return raw
    monkeypatch.setattr(hub, "_public_bytes", fetch)
    result = hub.dispatch("skill_install", {"source": "https://github.com/example/repo/tree/" + commit + "/skills/example"})
    assert not result["ok"]
    assert not hub.config.get("skill_packages")
    assert len(hub.discover_skills()) == 10


def test_current_plugin_format_mcp_and_host_components_are_reviewed(hub, tmp_path):
    package = tmp_path / "plugin"
    package.mkdir()
    (package / "plugin.json").write_text(json.dumps({"name": "Portable workflow", "extensions": {"com.openai": {"apps": {"host": "required"}, "hooks": {"install": "never-run"}}}}))
    (package / "mcp.json").write_text(json.dumps({"mcpServers": {"test": {"url": "https://example.com/mcp"}}}))
    (package / "SKILL.md").write_text("# Instructions")
    (package / "LICENSE").write_text("MIT")
    inspection = hub.dispatch("plugin_inspect", {"source": str(package)})
    assert inspection["ok"] and inspection["features"]["mcp_servers"] == 1
    assert inspection["features"]["hooks"] and inspection["features"]["host_specific"]
    assert any("adapter" in message for message in inspection["warnings"])
    assert not hub.config["servers"]
    result = hub.dispatch("plugin_install", {"inspection_id": inspection["inspection_id"], "reviewed": True})
    assert result["ok"] and result["plugin"]["licenses"] == ["LICENSE"]
    assert len(hub.config["servers"]) == 1 and not hub.config["servers"][0]["enabled"]


def test_canonical_manifest_and_inline_extension_replace_legacy_overlay(hub, tmp_path):
    package = tmp_path / "canonical"
    package.mkdir()
    (package / ".codex-plugin").mkdir()
    (package / "plugin.json").write_text(json.dumps({"name": "canonical", "extensions": {"com.openai": {}}}))
    (package / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "legacy", "apps": {"ignored": True}, "hooks": {"ignored": True}}))
    (package / "mcp.json").write_text(json.dumps({"mcpServers": {"canonical": {"url": "https://example.com/mcp"}}}))
    (package / ".mcp.json").write_text(json.dumps({"mcpServers": {"legacy": {"command": "ignored"}}}))
    (package / "SKILL.md").write_text("# skill")
    preview = hub.dispatch("plugin_inspect", {"source": str(package)})
    assert preview["ok"] and preview["name"] == "canonical"
    assert not preview["features"]["host_specific"] and not preview["features"]["hooks"]
    result = hub.dispatch("plugin_install", {"inspection_id": preview["inspection_id"], "reviewed": True})
    assert result["ok"] and result["plugin"]["name"] == "canonical"
    assert "canonical" in hub.config["servers"][0]["name"] and "url" in hub.config["servers"][0]


def test_disabling_plugin_gates_its_mcp_tools_and_resources(hub):
    plugin = hub.dispatch("plugin_install", {"source": "builtin:review"})["plugin"]
    server = hub.dispatch("mcp_save", {"url": "https://example.com/mcp", "enabled": True})["server"]
    configured = hub._server(server["id"])
    configured["tools"] = [{"name": "run", "description": "test", "inputSchema": {}}]
    plugin["server_ids"] = [server["id"]]
    tool_name = hub.tool_name(server["id"], "run")
    resource_name = "mcp__" + server["id"] + "__read_resource"
    assert any(item["function"]["name"] == tool_name for item in hub.schemas())
    hub.dispatch("plugin_toggle", {"id": plugin["id"], "enabled": False})
    assert configured["enabled"]  # Parent switch does not erase per-server selection.
    assert not any(item["function"]["name"] in {tool_name, resource_name} for item in hub.schemas())
    assert not hub.execute(tool_name, {})["ok"]
    assert not hub.execute(resource_name, {"uri": "test://read"})["ok"]
    assert not hub.connections
    hub.dispatch("plugin_toggle", {"id": plugin["id"], "enabled": True})
    assert any(item["function"]["name"] == tool_name for item in hub.schemas())


def test_plugin_mutations_serialize_review_and_rollback(hub, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = hub._plugin_install_reviewed
    def install(data):
        if data.get("source") == "broken":
            entered.set()
            assert release.wait(2)
            raise ValueError("fixture failure")
        return original(data)
    monkeypatch.setattr(hub, "_plugin_install_reviewed", install)
    results = []
    first = threading.Thread(target=lambda: results.append(hub.dispatch("plugin_install", {"source": "broken"})))
    second = threading.Thread(target=lambda: results.append(hub.dispatch("plugin_install", {"source": "builtin:review"})))
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(.02)
    assert not hub.config["plugins"]
    release.set()
    first.join(2); second.join(2)
    assert len(results) == 2 and sum(bool(result["ok"]) for result in results) == 1
    assert [plugin["id"] for plugin in hub.config["plugins"]] == ["starter-review"]
    assert Path(hub.config["plugins"][0]["path"]).is_dir()
