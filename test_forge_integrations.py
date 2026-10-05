from __future__ import annotations

import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile

import pytest

from browser_tools import BrowserTools, ExtensionBridge, browser_url
from forge_integrations import IntegrationHub, OAuthVaultStorage


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, value, reference=None):
        reference = reference or "credential-" + str(len(self.values))
        self.values[reference] = value
        return reference

    def get(self, reference):
        return self.values.get(reference)

    def delete(self, reference):
        self.values.pop(reference, None)


@pytest.fixture
def hub(tmp_path):
    instance = IntegrationHub(tmp_path / "forge", MemoryVault())
    yield instance
    instance.shutdown()


def test_credentials_never_serialized(hub):
    result = hub.dispatch("mcp_save", {"name": "Test", "url": "https://example.com/mcp", "token": "private-test-token"})
    assert result["ok"]
    assert "private-test-token" not in hub.config_path.read_text()
    assert "credential_ref" not in result["server"]
    assert result["server"]["authenticated"]
    result = hub.dispatch("mcp_save", {"id": result["server"]["id"], "clear_token": True})
    assert not result["server"]["authenticated"]
    assert not hub.vault.values


def test_secret_environment_uses_vault(hub):
    result = hub.dispatch("mcp_save", {"transport": "stdio", "command": "python", "args": [],
        "env": {"API_TOKEN": "private-token", "EXAMPLE_SETTING": "normal"}})
    assert result["ok"]
    assert result["server"]["env"] == {"EXAMPLE_SETTING": "normal"}
    assert "private-token" not in hub.config_path.read_text()


@pytest.mark.parametrize("url", ["file:///private", "https://name:password@example.com", "javascript:alert(1)", "https://example.com/mcp?api_key=secret"])
def test_bad_endpoint_urls_rejected(hub, url):
    assert not hub.dispatch("mcp_save", {"url": url})["ok"]


def test_namespaced_collision_resistance(hub):
    tools = [{"name": "a.b", "inputSchema": {"type": "object"}},
             {"name": "a-b", "inputSchema": {"type": "object"}},
             {"name": "a_b", "inputSchema": {"type": "object"}}]
    server = hub.dispatch("mcp_save", {"id": "one", "url": "https://example.com/mcp", "enabled": True})["server"]
    hub._server(server["id"])["tools"] = hub._validate_tools(tools)
    schemas = [s for s in hub.schemas() if s.get("source") == "mcp"]
    assert len({s["function"]["name"] for s in schemas}) == len(schemas)
    assert all(s["capability"] == "unknown" for s in schemas)


def test_tool_disable_does_not_dispatch(hub):
    server = hub.dispatch("mcp_save", {"url": "https://example.com", "enabled": False})["server"]
    item = hub._server(server["id"])
    item["tools"] = [{"name": "do", "description": "", "inputSchema": {}}]
    result = hub.execute(hub.tool_name(item["id"], "do"), {})
    assert not result["ok"]
    assert "disabled" in result["error"]
    assert not hub.connections


def test_rich_results_preserved_bounded(hub):
    result = hub._bound_result({"content": [{"type": "text", "text": "文" * 50000},
        {"type": "image", "data": "YWJj", "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "x://y", "blob": "YWJj"}}],
        "structuredContent": {"key": 42}}, {"id": "test"})
    assert len(result["text"]) <= 24000
    assert "data" not in result["content"][1]
    assert "blob" not in result["content"][2]["resource"]
    artifact = json.loads(Path(result["artifact"]).read_text())
    assert artifact["content"][1]["data"] == "YWJj"
    assert result["structuredContent"] == {"key": 42}


def test_skill_discovery_scope_and_toggle(hub, tmp_path):
    folder = tmp_path / "skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: test\ndescription: Tests the workspace\n---\n\nUse project tools.", encoding="utf-8")
    installed = hub.dispatch("skill_install", {"source": str(folder)})
    assert installed["ok"]
    skill = installed["skills"][0]
    assert skill["name"] == "test"
    assert hub.read_skill(skill["id"])["text"].endswith("Use project tools.")
    assert hub.dispatch("skill_toggle", {"id": skill["id"], "enabled": False})["ok"]
    with pytest.raises(ValueError):
        hub.read_skill(skill["id"])


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "folder/../../escape.txt", "C:/escape.txt", "folder\\..\\escape.txt"])
def test_zip_traversal_rejected(hub, tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, "bad")
    assert not hub.dispatch("plugin_inspect", {"source": str(archive)})["ok"]
    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlink_rejected(hub, tmp_path):
    archive = tmp_path / "bad.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(info, "../target")
    assert not hub.dispatch("plugin_inspect", {"source": str(archive)})["ok"]


def test_portable_plugin_no_hooks_execute(hub, tmp_path):
    package = tmp_path / "plugin"
    package.mkdir()
    (package / ".claude-plugin").mkdir()
    (package / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "Portable", "version": "1", "hooks": {"anything": "must-not-run"}}))
    (package / ".mcp.json").write_text(json.dumps({"mcpServers": {"demo": {"command": "python", "args": []}}}))
    (package / "LICENSE").write_text("Example license")
    (package / "SKILL.md").write_text("# skill")
    preview = hub.dispatch("plugin_inspect", {"source": str(package)})
    assert preview["ok"] and preview["compatible"]
    assert not hub.config["servers"] and not hub.config["plugins"]
    result = hub.dispatch("plugin_install", {"inspection_id": preview["inspection_id"], "reviewed": True})
    assert result["ok"]
    assert result["plugin"]["format"] == "claude"
    assert result["plugin"]["warnings"]
    assert result["plugin"]["licenses"] == ["LICENSE"]
    assert not hub.connections
    assert not hub.config["servers"][0]["enabled"]


def test_starter_catalog_install(hub):
    assert hub.dispatch("catalogs")["catalogs"][0]["reviewed"]
    result = hub.dispatch("plugin_install", {"source": "builtin:research"})
    assert result["ok"]
    assert hub.discover_skills()[0]["name"] == "research"
    assert not hub.dispatch("plugin_install", {"source": "builtin:research"})["ok"]


def test_review_installs_exact_staged_content(hub, tmp_path):
    package = tmp_path / "reviewed"
    package.mkdir()
    (package / "SKILL.md").write_text("# reviewed version")
    preview = hub.dispatch("plugin_inspect", {"source": str(package)})
    assert preview["ok"]
    (package / "SKILL.md").write_text("# changed after review")
    assert not hub.dispatch("plugin_install", {"source": str(package)})["ok"]
    result = hub.dispatch("plugin_install", {"inspection_id": preview["inspection_id"], "reviewed": True})
    assert result["ok"]
    skill = hub.discover_skills()[0]
    assert hub.read_skill(skill["id"])["text"] == "# reviewed version"
    assert not hub.inspections


def test_failed_import_rolls_back_disabled_servers(hub, tmp_path):
    package = tmp_path / "broken"
    package.mkdir()
    (package / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "valid": {"url": "https://example.com"}, "invalid": {"command": ""}}}))
    preview = hub.dispatch("plugin_inspect", {"source": str(package)})
    assert preview["ok"]
    result = hub.dispatch("plugin_install", {"inspection_id": preview["inspection_id"], "reviewed": True})
    assert not result["ok"]
    assert not hub.config["servers"] and not hub.config["plugins"]


def test_plugin_auth_headers_moved_to_vault(hub, tmp_path):
    package = tmp_path / "auth"
    package.mkdir()
    (package / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "remote": {"url": "https://example.com", "headers": {"Authorization": "Bearer private-test-value"}}}}))
    preview = hub.dispatch("plugin_inspect", {"source": str(package)})
    result = hub.dispatch("plugin_install", {"inspection_id": preview["inspection_id"], "reviewed": True})
    assert result["ok"]
    assert "private-test-value" not in hub.config_path.read_text()
    assert "private-test-value" not in (Path(result["plugin"]["path"]) / ".mcp.json").read_text()
    assert any("private-test-value" in value for value in hub.vault.values.values())


def test_external_catalog_not_marked_reviewed(hub, tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"plugins": [{"name": "External", "source": {"source": "github", "repo": "owner/repo"}}]}))
    result = hub.dispatch("catalog_save", {"source": str(path)})
    assert result["ok"]
    assert not result["catalog"]["reviewed"]
    assert result["catalog"]["entries"][0]["source"] == "https://github.com/owner/repo"


def test_bridge_only_selected_tabs_and_authenticated(hub):
    hub.browser.start_bridge()
    bridge = hub.browser.bridge
    with socket.create_connection(("127.0.0.1", bridge.server.server_address[1])) as client:
        client.sendall(b'{"token":"wrong","tabs":[]}\n')
        response = json.loads(client.makefile("rb").readline())
    assert not response["ok"]
    bridge.poll({"tabs": [{"id": 10, "url": "https://example.com", "title": "Example"}]})
    assert bridge.tabs()[0]["id"] == 10
    with pytest.raises(ValueError):
        bridge.command(99, "inspect", {})
    result = []
    thread = threading.Thread(target=lambda: result.append(bridge.command(10, "inspect", {})))
    thread.start()
    for _ in range(100):
        polled = bridge.poll({"tabs": [{"id": 10}]})
        if polled["commands"]:
            command = polled["commands"][0]
            break
        time.sleep(.01)
    bridge.poll({"tabs": [{"id": 10}], "results": [{"id": command["id"], "result": {"ok": True, "text": "page"}}]})
    thread.join(timeout=2)
    assert result == [{"ok": True, "text": "page"}]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com", "data:text/html,test"])
def test_browser_forbids_unsafe_urls(url):
    with pytest.raises(ValueError):
        browser_url(url)


def test_real_official_mcp_stdio_fixture(hub, tmp_path):
    pytest.importorskip("mcp")
    fixture = tmp_path / "fixture.py"
    fixture.write_text('''from mcp.server import MCPServer
server = MCPServer("Forge integration fixture")
@server.tool()
def add(a: int, b: int) -> int:
    """Add integers."""
    return a+b
@server.resource("fixture://text")
def text() -> str:
    return "resource text"
server.run(transport="stdio")
''', encoding="utf-8")
    server = hub.dispatch("mcp_save", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)], "enabled": True})["server"]
    result = hub.dispatch("mcp_test", {"id": server["id"]})
    assert result["ok"], result
    tool = next(s for s in hub.schemas() if s.get("source") == "mcp" and "Add integers" in s["function"]["description"])
    result = hub.execute(tool["function"]["name"], {"a": 2, "b": 4})
    assert result["ok"], result
    assert "6" in result["text"]
    result = hub.execute(f"mcp__{server['id']}__read_resource", {"uri": "fixture://text"})
    assert result["ok"], result
    assert "resource text" in result["text"]


def test_oauth_tokens_use_os_vault_only():
    pytest.importorskip("mcp")
    import asyncio
    from mcp.shared.auth import OAuthToken
    vault = MemoryVault()
    storage = OAuthVaultStorage(vault, {"oauth_tokens_ref": "oauth", "oauth_client_ref": "client"})
    asyncio.run(storage.set_tokens(OAuthToken(access_token="private-access", refresh_token="private-refresh")))
    assert asyncio.run(storage.get_tokens()).access_token == "private-access"
    assert set(vault.values) == {"oauth"}


@pytest.mark.parametrize("transport,server_transport,path", [("http", "streamable-http", "/mcp"), ("sse", "sse", "/sse")])
def test_real_remote_mcp_transports(hub, tmp_path, transport, server_transport, path):
    pytest.importorskip("mcp")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    fixture = tmp_path / "http_fixture.py"
    fixture.write_text('''from mcp.server import MCPServer
server = MCPServer("Forge HTTP integration fixture")
@server.tool()
def greet(name: str) -> str:
    return "Hello " + name
server.run(transport=TRANSPORT, host="127.0.0.1", port=PORT)
'''.replace("TRANSPORT", repr(server_transport)).replace("PORT", str(port)), encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(fixture)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                time.sleep(.02)
        server = hub.dispatch("mcp_save", {"transport": transport, "url": f"http://127.0.0.1:{port}{path}", "enabled": True})["server"]
        tested = hub.dispatch("mcp_test", {"id": server["id"]})
        assert tested["ok"], tested
        result = hub.execute(hub.tool_name(server["id"], "greet"), {"name": "workspace"})
        assert result["ok"], result
        assert result["text"] == "Hello workspace"
        hub._close_connection(server["id"])
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_older_protocol_stdio_compatibility(hub, tmp_path):
    pytest.importorskip("mcp")
    fixture = tmp_path / "old_fixture.py"
    fixture.write_text('''import json,sys
for line in sys.stdin:
    req=json.loads(line)
    if "id" not in req: continue
    method=req.get("method")
    if method=="initialize":
        result={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"old-fixture","version":"1"}}
    elif method=="tools/list":
        result={"tools":[{"name":"echo","description":"Older server","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]}
    elif method=="tools/call":
        result={"content":[{"type":"text","text":req["params"]["arguments"]["text"]}]}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":req["id"],"error":{"code":-32601,"message":"Unknown"}}),flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":result}),flush=True)
''', encoding="utf-8")
    server = hub.dispatch("mcp_save", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)], "enabled": True})["server"]
    assert hub.dispatch("mcp_test", {"id": server["id"]})["ok"]
    result = hub.execute(hub.tool_name(server["id"], "echo"), {"text": "works"})
    assert result["ok"] and result["text"] == "works"


def test_windows_vault_large_unicode_round_trip():
    import os
    if os.name != "nt":
        pytest.skip("Windows Credential Manager integration")
    from forge_credentials import CredentialVault
    vault = CredentialVault("ForgeIntegrationTests")
    reference = None
    try:
        value = "🛠credential" * 1500
        reference = vault.put(value)
        assert vault.get(reference) == value
        vault.put("replacement", reference)
        assert vault.get(reference) == "replacement"
    finally:
        if reference:
            vault.delete(reference)
    assert vault.get(reference) is None


def test_connection_loss_after_invocation_is_unknown(hub):
    import concurrent.futures
    server = hub.dispatch("mcp_save", {"url": "https://example.com", "enabled": True})["server"]
    item = hub._server(server["id"])
    item["tools"] = [{"name": "change", "description": "", "inputSchema": {}}]
    class LostConnection:
        closed = False
        ready = concurrent.futures.Future()
        ready.set_result(True)
        def request(self, *args, **kwargs):
            raise RuntimeError("Connection lost after sending")
        def shutdown(self):
            pass
    hub.connections[item["id"]] = LostConnection()
    result = hub.execute(hub.tool_name(item["id"], "change"), {})
    assert not result["ok"] and result["outcome_unknown"]


def test_cancel_real_mcp_call_marks_unknown(hub, tmp_path):
    pytest.importorskip("mcp")
    fixture = tmp_path / "cancel_fixture.py"
    marker = tmp_path / "effect.txt"
    fixture.write_text('''import asyncio
from pathlib import Path
from mcp.server import MCPServer
server = MCPServer("Cancel fixture")
@server.tool()
async def slow() -> str:
    Path(MARKER).write_text("started")
    await asyncio.sleep(30)
    return "done"
server.run(transport="stdio")
'''.replace("MARKER", repr(str(marker))), encoding="utf-8")
    server = hub.dispatch("mcp_save", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)], "enabled": True})["server"]
    assert hub.dispatch("mcp_test", {"id": server["id"]})["ok"]
    cancel = threading.Event()
    results = []
    thread = threading.Thread(target=lambda: results.append(hub.execute(hub.tool_name(server["id"], "slow"), {}, {"cancel": cancel})))
    thread.start()
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(.02)
    assert marker.exists()
    cancel.set()
    thread.join(timeout=3)
    assert results and results[0]["outcome_unknown"]


def test_browser_install_is_background_and_reports_progress(hub, monkeypatch):
    pytest.importorskip("playwright")
    import io
    import playwright._impl._driver as driver
    class Process:
        stdout = io.StringIO("Downloading Chromium\nDownload complete\n")
        def wait(self):
            return 0
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "unused-test-cache")
    monkeypatch.setattr(driver, "compute_driver_executable", lambda: ("node", "driver"))
    monkeypatch.setattr(driver, "get_driver_env", lambda: {})
    monkeypatch.setattr("browser_tools.subprocess.Popen", lambda *args, **kwargs: Process())
    result = hub.dispatch("browser_install")
    assert result["ok"]
    for _ in range(100):
        if hub.browser.installation["state"] != "downloading":
            break
        time.sleep(.01)
    assert hub.browser.installation["state"] == "ready"
    assert "Download complete" in hub.browser.installation["progress"]
    assert (hub.root / "config/browser-runtime.json").is_file()


def test_real_isolated_browser_snapshots(hub, monkeypatch):
    pytest.importorskip("playwright")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from playwright.sync_api import sync_playwright
    with sync_playwright() as probe:
        if not Path(probe.chromium.executable_path).is_file():
            pytest.skip("Chromium optional runtime is not installed")
    monkeypatch.setenv("FORGE_BROWSER_HEADLESS", "1")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            content = b'<!doctype html><title>Fixture</title><input placeholder="Name"><button onclick="this.textContent=\'Done\'">Save</button>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(content)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        snapshot = hub.browser.execute("browser_navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}"})
        field = next(target for target in snapshot["targets"] if target["tag"] == "input")
        changed = hub.browser.execute("browser_type", {"selector": field["selector"], "snapshot_id": snapshot["snapshot_id"], "text": "Example"})
        with pytest.raises(ValueError):
            hub.browser.execute("browser_type", {"selector": field["selector"], "snapshot_id": snapshot["snapshot_id"], "text": "Stale"})
        button = next(target for target in changed["targets"] if target["tag"] == "button")
        clicked = hub.browser.execute("browser_click", {"selector": button["selector"], "snapshot_id": changed["snapshot_id"]})
        assert "Done" in clicked["text"]
        screenshot = hub.browser.execute("browser_screenshot", {})
        assert Path(screenshot["artifact"]).read_bytes().startswith(b"\x89PNG")
    finally:
        server.shutdown()
        server.server_close()
