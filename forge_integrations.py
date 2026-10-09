"""MCP, skills, portable plugin imports and browser capabilities for Forge.

Configuration actions originate from a user-facing settings page. Model calls
must enter execute() through the coordinator's permission registry. Imported
instructions never authorize actions; plugin hooks/install scripts never run.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import concurrent.futures
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
import uuid
import zipfile

from browser_tools import BrowserTools, function_schema, register_native_host
from forge_credentials import CredentialVault
from forge_skills import SkillIndex, resource_path, validate_manifest
from forge_library import (ASSET_ROOT, LIBRARY_VERSION, PRESET_CATALOGS, STARTER_BUNDLES, STARTER_SKILLS,
                           atomic_bytes, preset_catalog, reconcile_starters, select_skills, skill_identity,
                           starter_metadata, relevance_score, task_context)

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_PACKAGE_FILES = 5000
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_TOOL_CHARS = 24000
SECRET_NAMES = re.compile(r"token|password|secret|api.?key|authorization|credential|cookie|jwt|(?:^|_)pat$", re.I)
SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _is_link(path):
    path = Path(path)
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Integration configuration is too large")
    fd, name = tempfile.mkstemp(prefix=".forge-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _safe_url(value, https_only=False):
    value = str(value).strip()
    parsed = urlsplit(value)
    allowed = ("https",) if https_only else ("http", "https")
    if parsed.scheme not in allowed or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a valid URL without embedded credentials")
    if parsed.fragment:
        raise ValueError("Endpoint URLs cannot contain fragments")
    return value


def _identifier(value):
    value = str(value)
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError("Identifiers use letters, numbers, underscores and hyphens")
    return value


def _server_namespace(identity):
    return identity if len(identity) <= 12 else identity[:6] + hashlib.sha256(identity.encode()).hexdigest()[:6]


def _error(exc):
    # SDK exceptions may include request headers/URL query strings: return a safe
    # class-based failure instead of leaking secrets into a chat or event log.
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "Optional integration dependency is unavailable; install requirements-integrations.txt"
    if isinstance(exc, concurrent.futures.TimeoutError):
        return "Integration timed out. Inspect its outcome before retrying a side effect."
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return str(exc)[:500]
    return f"Integration failed ({type(exc).__name__}). Check the endpoint, authentication and server availability."


class MCPConnection:
    """Persistent official-SDK session owned by one async task on one thread."""
    def __init__(self, config, vault, oauth_flow=None):
        self.config = dict(config)
        self.vault = vault
        self.oauth_flow = oauth_flow
        self.ready = concurrent.futures.Future()
        self.pending = set()
        self.closed = False
        self.loop = self.queue = None
        self.thread = threading.Thread(target=self._thread_main, name="forge-mcp-" + config["id"], daemon=True)
        self.thread.start()

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            safe = RuntimeError(_error(exc))
            if not self.ready.done():
                self.ready.set_exception(safe)
            for future in list(self.pending):
                if not future.done():
                    future.set_exception(safe)
        finally:
            self.closed = True

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.client.sse import sse_client
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        async with AsyncExitStack() as stack:
            config = self.config
            token = self.vault.get(config.get("credential_ref"))
            headers = dict(config.get("headers", {}))
            for name, reference in config.get("header_credentials", {}).items():
                secret = self.vault.get(reference)
                if secret is None:
                    raise ValueError("A configured header credential is missing")
                if config.get("auth_type") != "oauth" or name.lower() != "authorization":
                    headers[name] = secret
            if token and config.get("auth_type") != "oauth":
                headers["Authorization"] = "Bearer " + token
            auth = None
            if config.get("auth_type") == "oauth":
                from mcp.client.auth import OAuthClientProvider
                from mcp.shared.auth import OAuthClientMetadata
                flow = self.oauth_flow
                redirect = flow.redirect_uri if flow else config.get("oauth_redirect_uri", "http://127.0.0.1:8769/callback")
                auth = OAuthClientProvider(server_url=config["url"],
                    client_metadata=OAuthClientMetadata(redirect_uris=[redirect], client_name="Forge",
                        token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"]),
                    storage=OAuthVaultStorage(self.vault, config),
                    redirect_handler=flow.redirect if flow else _oauth_unavailable,
                    callback_handler=flow.callback if flow else _oauth_unavailable)
            if config["transport"] == "stdio":
                environment = dict(config.get("env", {}))
                for name, reference in config.get("env_credentials", {}).items():
                    secret = self.vault.get(reference)
                    if secret is None:
                        raise ValueError("A configured server credential is missing")
                    environment[name] = secret
                # Child stderr may contain private data. Never relay it to model
                # context or public diagnostics. The server manages its own logs.
                errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=config["command"], args=config.get("args", []),
                    env=environment or None, cwd=config.get("cwd") or None), errlog=errlog))
            elif config["transport"] == "sse":
                streams = await stack.enter_async_context(sse_client(config["url"], headers=headers, timeout=15, auth=auth))
            else:
                import httpx2
                client = await stack.enter_async_context(httpx2.AsyncClient(headers=headers, auth=auth, timeout=30, follow_redirects=False))
                streams = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1], read_timeout_seconds=30))
            await session.initialize()
            self.ready.set_result(True)
            while True:
                item = await self.queue.get()
                if item is None:
                    return
                operation, arguments, future = item
                if future.cancelled():
                    self.pending.discard(future)
                    continue
                try:
                    if operation == "list_tools":
                        from mcp.types import PaginatedRequestParams
                        tools = []
                        cursor = None
                        seen = set()
                        for _ in range(20):
                            result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                            tools.extend(_jsonable(tool) for tool in result.tools)
                            cursor = getattr(result, "nextCursor", None)
                            if not cursor or cursor in seen:
                                break
                            seen.add(cursor)
                        value = tools[:1000]
                    elif operation == "call_tool":
                        task = asyncio.create_task(session.call_tool(arguments["name"], arguments.get("arguments", {})))
                        def abandoned(completed, task=task):
                            if completed.cancelled() and not task.done():
                                self.loop.call_soon_threadsafe(task.cancel)
                        future.add_done_callback(abandoned)
                        value = _jsonable(await task)
                    elif operation == "list_resources":
                        value = _jsonable(await session.list_resources())
                    elif operation == "read_resource":
                        value = _jsonable(await session.read_resource(arguments["uri"]))
                    else:
                        raise ValueError("Unknown MCP request")
                    if not future.done():
                        future.set_result(value)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(RuntimeError(_error(exc)))
                except asyncio.CancelledError:
                    future.cancel()
                finally:
                    self.pending.discard(future)

    def request(self, operation, arguments=None, timeout=35, cancel=None):
        if self.closed:
            raise RuntimeError("MCP connection is closed; test it to reconnect")
        self.ready.result(timeout=20)
        future = concurrent.futures.Future()
        self.pending.add(future)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (operation, arguments or {}, future))
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                future.cancel()
                raise RuntimeError("Integration invocation cancelled; inspect its outcome before retrying")
            try:
                return future.result(timeout=min(.2, max(.001, deadline - time.monotonic())))
            except concurrent.futures.TimeoutError:
                if time.monotonic() >= deadline:
                    # No automatic retry: the server may already have executed a write.
                    future.cancel()
                    raise

    def shutdown(self):
        if self.loop and self.queue and not self.closed:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
            self.thread.join(timeout=5)
        if self.oauth_flow:
            self.oauth_flow.shutdown()


async def _oauth_unavailable(*args):
    raise ValueError("This connection requires authentication. Choose Authenticate in MCP Settings.")


class OAuthVaultStorage:
    def __init__(self, vault, config):
        self.vault = vault
        self.config = config

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self.vault.get(self.config.get("oauth_tokens_ref"))
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens):
        self.vault.put(tokens.model_dump_json(), self.config["oauth_tokens_ref"])

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self.vault.get(self.config.get("oauth_client_ref"))
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info):
        self.vault.put(client_info.model_dump_json(), self.config["oauth_client_ref"])


class OAuthFlow:
    """User-driven OAuth redirect; official SDK validates state and exchanges PKCE."""
    def __init__(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs
        self.authorization_url = None
        self.result = concurrent.futures.Future()
        flow = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                values = parse_qs(parsed.query)
                if flow.result.done():
                    self.send_error(409)
                    return
                if values.get("error") or not values.get("code"):
                    flow.result.set_exception(ValueError("Authentication was declined or failed"))
                    self.send_response(400)
                else:
                    flow.result.set_result({"code": values["code"][0], "state": values.get("state", [None])[0],
                                            "iss": values.get("iss", [None])[0]})
                    self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Return to Forge to finish connecting. This page may be closed.")
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}/callback"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="forge-oauth-callback")
        self.thread.start()
        self.closed = False

    async def redirect(self, url):
        self.authorization_url = str(url)

    async def callback(self):
        from mcp.shared.auth import AuthorizationCodeResult
        result = await asyncio.to_thread(self.result.result, timeout=180)
        return AuthorizationCodeResult(**result)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        if not self.result.done():
            self.result.set_exception(ValueError("Authentication was cancelled"))
        self.server.shutdown()
        self.server.server_close()


class IntegrationHub:
    def __init__(self, data_dir, vault=None):
        self.root = Path(data_dir).resolve()
        self.config_path = self.root / "config" / "integrations.json"
        self.lock = threading.RLock()
        self.catalog_refresh_lock = threading.Lock()
        self.catalog_refresh_thread = None
        self.vault = vault or CredentialVault()
        self.connections = {}
        self.auth_flows = {}
        self.inspections = {}
        self.skill_index = SkillIndex()
        self.browser = BrowserTools(self.root)
        self.closed = False
        if self.config_path.is_file():
            if self.config_path.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("Integration configuration is too large")
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            self.config = {"version": 1, "servers": [], "plugins": [], "catalogs": [], "skills": {}}
        for key, default in (("servers", []), ("plugins", []), ("catalogs", []), ("skills", {})):
            self.config.setdefault(key, default)
        for directory in ("skills", "plugins", "artifacts/integrations"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        with self.lock:
            changed, self.library_errors = reconcile_starters(self.root, self.config)
            if changed:
                self._save()

    def _save(self):
        _atomic_json(self.config_path, self.config)

    def _server(self, identity):
        identity = _identifier(identity)
        for item in self.config["servers"]:
            if item["id"] == identity:
                return item
        raise ValueError("MCP server was not found")

    def _public_server(self, server):
        public = {key: value for key, value in server.items() if key not in {"credential_ref", "env_credentials", "header_credentials", "oauth_tokens_ref", "oauth_client_ref"}}
        public["authenticated"] = bool(server.get("credential_ref") or server.get("env_credentials") or server.get("header_credentials") or
                                         (server.get("oauth_tokens_ref") and self.vault.get(server["oauth_tokens_ref"])))
        public["connected"] = server["id"] in self.connections and not self.connections[server["id"]].closed
        public["tools"] = [{**tool, "enabled": self._tool_enabled(server, tool["name"])} for tool in server.get("tools", [])]
        return public

    def _server_enabled(self, server):
        owners = [plugin for plugin in self.config["plugins"] if server["id"] in plugin.get("server_ids", [])]
        return bool(server.get("enabled")) and all(plugin.get("enabled", True) for plugin in owners)

    def _tool_enabled(self, server, name):
        selected = server.get("enabled_tools")
        return self._server_enabled(server) and (selected is None or name in selected)

    def _connection(self, server):
        connection = self.connections.get(server["id"])
        if connection is None or connection.closed:
            connection = MCPConnection(server, self.vault)
            self.connections[server["id"]] = connection
        return connection

    def _close_connection(self, identity):
        connection = self.connections.pop(identity, None)
        if connection:
            connection.shutdown()

    def dispatch(self, action, data=None):
        # A failed import rolls back only its own transaction. Serializing the
        # install/review mutations prevents that rollback from deleting a second
        # concurrent installation or its newly configured servers.
        if action in {"plugin_install", "plugin_inspect", "plugin_discard", "plugin_toggle",
                      "skill_install", "skill_toggle", "skill_edit", "skill_reset", "skill_restore", "mcp_save", "mcp_remove"}:
            with self.lock:
                return self._dispatch(action, data)
        return self._dispatch(action, data)

    def _dispatch(self, action, data=None):
        data = data or {}
        try:
            if action in ("integrations", "mcp_servers"):
                result = {"ok": True, "servers": [self._public_server(s) for s in self.config["servers"]]}
                if action == "integrations":
                    result.update(skills=self.discover_skills(data.get("project")), plugins=list(self.config["plugins"]),
                                  catalogs=self.catalogs(), browser=self.browser.status(), capabilities={
                                      "mcp": importlib.util.find_spec("mcp") is not None,
                                      "browser": importlib.util.find_spec("playwright") is not None,
                                      "credentials": "Windows Credential Manager" if os.name == "nt" else "OS keyring",
                                      "oauth": True, "authentication": ["bearer", "oauth", "credential-backed environment"]})
                return result
            if action == "mcp_save":
                return self._mcp_save(data)
            if action == "mcp_auth_start":
                server = self._server(data["id"])
                if server["transport"] == "stdio":
                    raise ValueError("OAuth is available for remote MCP endpoints")
                self._close_connection(server["id"])
                flow = OAuthFlow()
                self.auth_flows[server["id"]] = flow
                server.update(auth_type="oauth", oauth_tokens_ref=server.get("oauth_tokens_ref") or uuid.uuid4().hex,
                              oauth_client_ref=server.get("oauth_client_ref") or uuid.uuid4().hex,
                              oauth_redirect_uri=flow.redirect_uri)
                self._save()
                self.connections[server["id"]] = MCPConnection(server, self.vault, oauth_flow=flow)
                return {"ok": True, "state": "connecting", "id": server["id"]}
            if action == "mcp_auth_status":
                server = self._server(data["id"])
                connection = self.connections.get(server["id"])
                flow = self.auth_flows.get(server["id"])
                if not connection:
                    raise ValueError("Start authentication first")
                if connection.ready.done():
                    connection.ready.result()
                    server["tools"] = self._validate_tools(connection.request("list_tools"))
                    server["status"] = "ready"
                    self._save()
                    if flow:
                        flow.shutdown()
                    return {"ok": True, "state": "ready", "server": self._public_server(server)}
                return {"ok": True, "state": "authenticate" if flow and flow.authorization_url else "connecting",
                        "authorization_url": flow.authorization_url if flow else None}
            if action == "mcp_auth_cancel":
                self._close_connection(data["id"])
                flow = self.auth_flows.pop(data["id"], None)
                if flow:
                    flow.shutdown()
                return {"ok": True}
            if action == "mcp_test":
                server = self._server(data["id"])
                self._close_connection(server["id"])
                try:
                    tools = self._connection(server).request("list_tools")
                    with self.lock:
                        server["tools"] = self._validate_tools(tools)
                        server["status"] = "ready"
                        server.pop("error", None)
                        self._save()
                    return {"ok": True, "server": self._public_server(server), "tools": server["tools"]}
                except Exception as exc:
                    self._close_connection(server["id"])
                    server["status"] = "error"
                    server["error"] = _error(exc)
                    self._save()
                    return {"ok": False, "error": server["error"], "server": self._public_server(server)}
            if action == "mcp_remove":
                server = self._server(data["id"])
                self._close_connection(server["id"])
                references = [server.get("credential_ref"), server.get("oauth_tokens_ref"), server.get("oauth_client_ref"),
                              *server.get("env_credentials", {}).values(), *server.get("header_credentials", {}).values()]
                for reference in filter(None, references):
                    self.vault.delete(reference)
                self.config["servers"].remove(server)
                self._save()
                return {"ok": True}
            if action == "mcp_resources":
                server = self._server(data["id"])
                return {"ok": True, **self._connection(server).request("list_resources")}
            if action == "skills":
                return {"ok": True, "skills": self.discover_skills(data.get("project"))}
            if action == "skills_search":
                return self.search_skills(data)
            if action == "skill_route_preview":
                return self.route_preview(data)
            if action == "skills_resource_read":
                return self.read_skill_resource(data["id"], data["path"], data.get("project"), data.get("start", 0), data.get("limit", 8000))
            if action == "skill_versions":
                return self.skill_versions(data["id"], data.get("project"))
            if action == "skill_edit":
                return self.edit_skill(data)
            if action == "skill_reset":
                return self.reset_skill(data)
            if action == "skill_restore":
                return self.restore_skill(data)
            if action == "skill_install":
                source = str(data.get("source") or data.get("path") or "").strip()
                if source.startswith("builtin:skill/"):
                    slug = source.removeprefix("builtin:skill/")
                    skill = next((item for item in self.discover_skills(data.get("project")) if item.get("library_id") == slug), None)
                    if not skill:
                        raise ValueError("This starter skill was removed locally; restore its SKILL.md from the shipped starter library")
                    return {"ok": True, "skill": skill, "skills": self.discover_skills(data.get("project"))}
                package = self._install_package(data, "skills")
                if not list(Path(package["path"]).rglob("SKILL.md")):
                    package["warning"] = "This package contains no SKILL.md"
                self.config.setdefault("skill_packages", []).append(package)
                self._save()
                return {"ok": True, "package": package, "skills": self.discover_skills(data.get("project"))}
            if action == "skill_toggle":
                identity = str(data["id"])
                if not any(skill["id"] == identity for skill in self.discover_skills(data.get("project"))):
                    raise ValueError("Skill was not found")
                previous = self.config["skills"].get(identity, {})
                self.config["skills"][identity] = {"enabled": bool(data.get("enabled", previous.get("enabled", True))),
                                                    "automatic": bool(data.get("automatic", previous.get("automatic", False)))}
                self._save()
                return {"ok": True, "skills": self.discover_skills(data.get("project"))}
            if action == "plugins":
                return {"ok": True, "plugins": list(self.config["plugins"])}
            if action == "plugin_install":
                return self._plugin_install(data)
            if action == "plugin_inspect":
                return self._plugin_inspect(data)
            if action == "plugin_discard":
                package = self.inspections.pop(str(data["inspection_id"]), None)
                if package:
                    self._discard_stage(package["path"])
                return {"ok": True}
            if action == "plugin_toggle":
                identity = _identifier(data["id"])
                plugin = next((p for p in self.config["plugins"] if p["id"] == identity), None)
                if not plugin:
                    raise ValueError("Plugin was not found")
                plugin["enabled"] = bool(data.get("enabled", True))
                self._save()
                return {"ok": True, "plugin": plugin}
            if action == "catalogs":
                return {"ok": True, "catalogs": self.catalogs()}
            if action == "catalog_discover":
                return self.catalog_discover(data)
            if action == "catalog_save":
                return self._catalog_save(data)
            if action == "catalog_test":
                catalog = next((c for c in self.config["catalogs"] if c["id"] == data["id"]), None)
                if not catalog:
                    raise ValueError("Catalog was not found")
                content = self._read_catalog(catalog["source"])
                catalog["entries"] = self._catalog_entries(content)
                self._save()
                return {"ok": True, "catalog": catalog}
            if action == "tools":
                return {"ok": True, "tools": self.schemas(data.get("project"))}
            if action == "browser_status":
                # The default native readiness check must stay lightweight.
                # Inspect optional isolated engines only when the UI requests it.
                status = self.browser.status(backend=data["backend"]) if data.get("backend") else self.browser.status()
                return {"ok": True, **status}
            if action.startswith("browser_native_"):
                return self.browser.native_action(action.removeprefix("browser_native_"), data)
            if action == 'browser_extension_info':
                return {'ok':True, **self.browser.extension_info()}
            if action == 'browser_extension_open_folder':
                return self.browser.open_extension_folder()
            if action == "browser_install":
                return self.browser.install_browser()
            if action == "browser_bridge_enable":
                return self.browser.start_bridge()
            if action == "browser_host_register":
                self.browser.start_bridge()
                return register_native_host(data["extension_id"], data.get("host_executable") or None, self.root)
            if action == "skill_read":
                return self.read_skill(data["id"], data.get("project"))
            if action == "skill_preview":
                return self._read_skill(data["id"], data.get("project"), preview=True)
            raise ValueError("Unknown integration action")
        except Exception as exc:
            return {"ok": False, "error": _error(exc)}

    def _mcp_save(self, data):
        with self.lock:
            identity = _identifier(data.get("id") or uuid.uuid4().hex[:12])
            existing = next((s for s in self.config["servers"] if s["id"] == identity), {})
            transport = data.get("transport", existing.get("transport", "http"))
            if transport in {"streamable_http", "streamable-http"}:
                transport = "http"
            if transport not in {"http", "stdio", "sse"}:
                raise ValueError("Select HTTP, stdio or legacy SSE transport")
            name = str(data.get("name") or existing.get("name") or "MCP server")[:100]
            server = {**existing, "id": identity, "name": name, "transport": transport,
                      "enabled": bool(data.get("enabled", existing.get("enabled", False)))}
            if transport == "stdio":
                command = str(data.get("command", existing.get("command", ""))).strip()
                args = data.get("args", existing.get("args", []))
                if not command or "\x00" in command or not isinstance(args, list) or not all(isinstance(a, str) and "\x00" not in a for a in args):
                    raise ValueError("Supply an executable and an array of arguments")
                server.update(command=command, args=args[:100], cwd=data.get("cwd", existing.get("cwd")))
                environment = dict(existing.get("env", {}))
                references = dict(existing.get("env_credentials", {}))
                for key, value in data.get("env", {}).items():
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", key):
                        raise ValueError("Invalid environment variable name")
                    if SECRET_NAMES.search(key):
                        references[key] = self.vault.put(str(value), references.get(key))
                        environment.pop(key, None)
                    else:
                        environment[key] = str(value)
                server.update(env=environment, env_credentials=references)
                server.pop("url", None)
            else:
                server["url"] = _safe_url(data.get("url", existing.get("url", "")))
                # Query credentials are rejected rather than serialized in config.
                if SECRET_NAMES.search(urlsplit(server["url"]).query):
                    raise ValueError("Use the authentication field instead of URL query credentials")
            if data.get("token"):
                server["credential_ref"] = self.vault.put(data["token"], existing.get("credential_ref"))
                server["auth_type"] = "bearer"
            if "auth_type" in data:
                if data["auth_type"] not in {"none", "bearer", "oauth"}:
                    raise ValueError("Select no authentication, bearer token or OAuth")
                server["auth_type"] = data["auth_type"]
            if "headers" in data:
                headers = {}
                refs = dict(existing.get("header_credentials", {}))
                for key, value in data["headers"].items():
                    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", str(key)) or "\r" in str(value) or "\n" in str(value):
                        raise ValueError("Invalid server header")
                    if SECRET_NAMES.search(str(key)):
                        refs[key] = self.vault.put(str(value), refs.get(key))
                    else:
                        headers[key] = str(value)
                server.update(headers=headers, header_credentials=refs)
            if data.get("clear_token") and server.get("credential_ref"):
                self.vault.delete(server.pop("credential_ref"))
            if "enabled_tools" in data:
                if not isinstance(data["enabled_tools"], list):
                    raise ValueError("Enabled tools must be an array")
                server["enabled_tools"] = [str(name) for name in data["enabled_tools"]][:1000]
            server.setdefault("tools", [])
            self._close_connection(identity)
            self.config["servers"] = [s for s in self.config["servers"] if s["id"] != identity] + [server]
            self._save()
            return {"ok": True, "server": self._public_server(server)}

    @staticmethod
    def _validate_tools(tools):
        output = []
        seen = set()
        for item in tools[:1000]:
            name = str(item.get("name", ""))
            if not name or len(name) > 200 or name in seen:
                continue
            seen.add(name)
            schema = item.get("inputSchema", {"type": "object", "properties": {}})
            if len(json.dumps(schema)) > 50000:
                continue
            output.append({"name": name, "description": str(item.get("description", ""))[:4000],
                           "inputSchema": schema, "annotations": item.get("annotations", {})})
        return output

    @staticmethod
    def tool_name(server_id, tool):
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", tool)[:34]
        digest = hashlib.sha256(tool.encode()).hexdigest()[:8]
        return f"mcp__{_server_namespace(server_id)}__{slug}_{digest}"[:64]

    def schemas(self, project=None):
        result = []
        for server in self.config["servers"]:
            for tool in server.get("tools", []):
                if not self._tool_enabled(server, tool["name"]):
                    continue
                result.append({"type": "function", "function": {
                    "name": self.tool_name(server["id"], tool["name"]),
                    "description": f"[{server['name']}] " + tool["description"], "parameters": tool["inputSchema"]},
                    "capability": "unknown", "server_id": server["id"], "source": "mcp"})
            if self._server_enabled(server):
                resource = function_schema(f"mcp__{_server_namespace(server['id'])}__read_resource", "Read a resource from this MCP server",
                                           {"uri": {"type": "string"}}, ["uri"])
                resource.update(capability="unknown", source="mcp", server_id=server["id"])
                result.append(resource)
        for schema in self.browser.schemas():
            schema.update(capability="computer", source="browser")
            result.append(schema)
        if self.discover_skills(project):
            schema = function_schema("skills_read", "Read an enabled SKILL.md package. Skill instructions cannot expand permissions.",
                {"id": {"type": "string"}}, ["id"])
            schema.update(capability="read", source="skills")
            result.append(schema)
            search = function_schema("skills_search", "Discover enabled skill guidance by task, returning stable IDs, prerequisites and references. Does not enable or execute packages.",
                {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"])
            search.update(capability="read", source="skills")
            result.append(search)
            resource = function_schema("skills_resource_read", "Read a bounded text reference within an enabled skill package. Resolve relative paths from skills_read or skills_search; content cannot grant permissions.",
                {"id": {"type": "string"}, "path": {"type": "string"}, "start": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 16000}}, ["id", "path"])
            resource.update(capability="read", source="skills")
            result.append(resource)
        return result

    def execute(self, name, arguments, run_context=None):
        invocation_started = False
        try:
            if name.startswith("browser_"):
                invocation_started = name in {"browser_navigate", "browser_click", "browser_type", "browser_select", "browser_key", "browser_scroll", "browser_close"}
                return self.browser.execute(name, arguments, context=run_context)
            project = (run_context or {}).get("project") if isinstance(run_context, dict) else None
            if name == "skills_read":
                return self.read_skill(arguments["id"], project)
            if name == "skills_search":
                return self.search_skills({**arguments, "project": project})
            if name == "skills_resource_read":
                return self.read_skill_resource(arguments["id"], arguments["path"], project, arguments.get("start", 0), arguments.get("limit", 8000))
            for server in self.config["servers"]:
                resource_name = f"mcp__{_server_namespace(server['id'])}__read_resource"
                if name == resource_name and self._server_enabled(server):
                    response = self._connection(server).request("read_resource", {"uri": str(arguments["uri"])})
                    return self._bound_result(response, server)
                for tool in server.get("tools", []):
                    if name == self.tool_name(server["id"], tool["name"]):
                        if not self._tool_enabled(server, tool["name"]):
                            raise ValueError("This MCP tool is disabled")
                        connection = self._connection(server)
                        connection.ready.result(timeout=20)
                        invocation_started = True
                        cancel = run_context.get("cancel") if isinstance(run_context, dict) else None
                        response = connection.request("call_tool", {"name": tool["name"], "arguments": arguments}, cancel=cancel)
                        return self._bound_result(response, server)
            raise ValueError("Integration tool was not found")
        except Exception as exc:
            return {"ok": False, "error": _error(exc), "outcome_unknown": invocation_started or isinstance(exc, concurrent.futures.TimeoutError)}

    def _bound_result(self, response, server):
        raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_ARTIFACT_BYTES:
            return {"ok": False, "error": "MCP result exceeded the 20 MB limit"}
        path = self.root / "artifacts" / "integrations" / (uuid.uuid4().hex + ".json")
        path.write_bytes(raw)
        content = []
        remaining = MAX_TOOL_CHARS
        for block in response.get("content", response.get("contents", []))[:100]:
            item = dict(block)
            if item.get("type") == "text" or "text" in item:
                text = str(item.get("text", ""))
                item["text"] = text[:remaining]
                remaining = max(0, remaining - len(item["text"]))
                if len(text) > len(item["text"]):
                    item["truncated"] = True
            for field in ("data", "blob"):
                if field in item:
                    binary = item.pop(field)
                    item["artifact"] = self._binary_artifact(binary, item.get("mimeType"), path)
                    item["message"] = "Binary content is retained as a local artifact"
            if isinstance(item.get("resource"), dict):
                resource = dict(item["resource"])
                if "blob" in resource:
                    resource["artifact"] = self._binary_artifact(resource.pop("blob"), resource.get("mimeType"), path)
                if "text" in resource:
                    resource["text"] = str(resource["text"])[:remaining]
                    remaining = max(0, remaining - len(resource["text"]))
                item["resource"] = resource
            if len(json.dumps(item, ensure_ascii=False)) > MAX_TOOL_CHARS:
                item = {"type": item.get("type", "resource"), "text": str(item.get("text", ""))[:12000],
                        "artifact": str(path), "truncated": True}
            content.append(item)
        structured = response.get("structuredContent")
        if structured is not None and len(json.dumps(structured)) > MAX_TOOL_CHARS:
            structured = {"artifact": str(path), "message": "Structured result exceeds the context preview limit"}
        return {"ok": not response.get("isError", False), "content": content, "structuredContent": structured,
                "text": "\n".join(str(c.get("text", "")) for c in content if c.get("text")),
                "artifact": str(path), "server_id": server["id"], "truncated": len(raw) > MAX_TOOL_CHARS}

    def _binary_artifact(self, encoded, mime, fallback):
        import base64
        try:
            raw = base64.b64decode(str(encoded), validate=True)
        except (ValueError, TypeError):
            return str(fallback)
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                     "audio/wav": ".wav", "audio/mpeg": ".mp3", "application/pdf": ".pdf"}.get(mime, ".bin")
        path = self.root / "artifacts" / "integrations" / (uuid.uuid4().hex + extension)
        path.write_bytes(raw)
        return str(path)

    def discover_skills(self, project=None):
        with self.lock:
            return self._discover_skills(project)

    def _discover_skills(self, project=None):
        roots = [(self.root / "skills", "global")]
        for plugin in self.config["plugins"]:
            if plugin.get("enabled", True):
                roots.append((Path(plugin["path"]), "plugin:" + plugin["id"]))
        if project:
            project_path = Path(project.get("root", project.get("path", ""))) if isinstance(project, dict) else Path(project)
            for name in (".forge/skills", ".agents/skills", ".codex/skills", ".claude/skills"):
                candidate = project_path / name
                if candidate.resolve().is_relative_to(project_path.resolve()) and not any(_is_link(project_path / part) for part in (name.split("/")[0], name)):
                    roots.append((candidate, "project"))
        output, seen = [], set()
        for root, scope in roots:
            for path in self.skill_index.paths(root):
                try:
                    if path.resolve() in seen:
                        continue
                    package_content = self.skill_index.describe(path, self._frontmatter)
                    metadata = package_content["frontmatter"]
                    builtin = starter_metadata(path, self.root, index=self.skill_index, digest=package_content["digest"])
                except (OSError, ValueError):
                    continue
                seen.add(path.resolve())
                identity = skill_identity(path)
                detail = metadata.get("metadata", {})
                detail = detail if isinstance(detail, dict) else {}
                manifest = package_content["manifest"]
                selection = self.config["skills"].get(identity, {})
                plugin = next((item for item in self.config["plugins"] if scope == "plugin:" + item["id"]), None)
                package = next((item for item in self.config.get("skill_packages", [])
                                if path.resolve().is_relative_to(Path(item["path"]).resolve())), None)
                def text_list(value, maximum=50):
                    if isinstance(value, str):
                        value = [part.strip() for part in value.split(",")]
                    return [str(part)[:200] for part in value[:maximum]] if isinstance(value, list) else []
                tags = text_list((builtin or metadata).get("tags", detail.get("tags", [])), 20)
                triggers = text_list(manifest.get("triggers", (builtin or metadata).get("triggers", tags)))
                output.append({"id": identity, "name": str((builtin or {}).get("name") or detail.get("display-name") or metadata.get("name") or path.parent.name)[:100],
                    "description": str(metadata.get("description") or (builtin or {}).get("description") or "")[:2000], "path": str(path), "scope": scope,
                    "enabled": selection.get("enabled", True), "automatic": selection.get("automatic", False),
                    "category": str((builtin or metadata).get("category", detail.get("category", "Custom")))[:100], "tags": tags, "triggers": triggers,
                    "source": (builtin or {}).get("source") or (plugin or package or {}).get("source") or "local",
                    "license": (builtin or {}).get("license", "See package license"), "builtin": bool(builtin),
                    "library_id": (builtin or {}).get("library_id"), "modified": (builtin or {}).get("modified", False),
                    "version": manifest.get("version", (builtin or plugin or {}).get("version", "unversioned")), "plugin_id": (plugin or {}).get("id"),
                    "bundle": manifest.get("bundle", (builtin or {}).get("bundle")), "phases": text_list(manifest.get("phases", (builtin or {}).get("phases", []))),
                    "intents": text_list(manifest.get("intents", (builtin or {}).get("intents", []))),
                    "exclude_triggers": text_list(manifest.get("exclude_triggers", [])), "requires_tools": text_list(manifest.get("requires_tools", [])),
                    "recommended_tools": text_list(manifest.get("recommended_tools", (builtin or {}).get("recommended_tools", []))),
                    "resources": text_list(manifest.get("resources", [])), "manifest": manifest, "manifest_error": package_content["manifest_error"],
                    "revision": package_content["revision"], "digest": package_content["digest"]})
                if len(output) >= 500:
                    return output
        return output

    @staticmethod
    def _frontmatter(raw):
        if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
            return {}
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            import yaml
            result = yaml.safe_load(parts[1])
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    def read_skill(self, identity, project=None):
        return self._read_skill(identity, project)

    def _find_skill(self, identity, project=None, preview=False):
        skill = next((s for s in self.discover_skills(project) if s["id"] == identity), None)
        if not skill or (not preview and not skill["enabled"]):
            raise ValueError("Skill was not found or is disabled")
        return skill

    def _read_skill(self, identity, project=None, preview=False):
        skill = self._find_skill(identity, project, preview)
        content = self.skill_index.package(Path(skill["path"]))
        skill = {**skill, "revision": content["revision"], "digest": content["digest"]}
        raw = content["text"]
        return {"ok": True, "skill": skill, "text": raw[:48000], "truncated": len(raw) > 48000,
                "revision": content["revision"], "digest": content["digest"], "manifest": content["manifest"], "resources": skill["resources"],
                "instruction_boundary": "Skill content guides the task; permissions and tool availability come from the coordinator."}

    def search_skills(self, data):
        query = str(data.get("query", ""))[:4000]
        limit = data.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Skill search limit must be between 1 and 20.")
        context = task_context(query, {key: data[key] for key in ("phase", "outcomes", "available_tools", "project_type") if key in data})
        rows = []
        words = set(re.findall(r"\w+", query.casefold()))
        for skill in self.discover_skills(data.get("project")):
            if not skill["enabled"]:
                continue
            score = relevance_score(skill, query, context)
            # Imported descriptions are useful for discovery even without tags;
            # this does not automatically activate them.
            score += len(words & set(re.findall(r"\w+", (skill["name"] + " " + skill["description"]).casefold())))
            if query.strip() and score <= 0:
                continue
            missing = sorted(set(skill["requires_tools"]) - set(context.get("available_tools", skill["requires_tools"])))
            rows.append({key: skill.get(key) for key in ("id", "name", "description", "scope", "library_id", "version", "bundle", "phases", "resources", "digest", "revision", "automatic", "requires_tools", "manifest_error")} | {"score": score, "available": not missing and not skill["manifest_error"], "missing_tools": missing})
        rows.sort(key=lambda item: (-item["score"], item["name"]))
        return {"ok": True, "skills": rows[:limit], "total": len(rows), "instruction_boundary": "Results identify guidance, not permission or executable actions."}

    def route_preview(self, data):
        query = str(data.get("query", ""))[:24000]
        context = {key: data[key] for key in ("phase", "outcomes", "available_tools", "project_type") if key in data}
        skills = self.discover_skills(data.get("project"))
        selected = select_skills(skills, data.get("explicit"), query, context)
        return {"ok": True, "phase": task_context(query, context)["phase"], "selected": [
            {key: skill.get(key) for key in ("id", "name", "library_id", "scope", "version", "selection_reason", "selection_score", "requires_tools", "manifest_error")}
            for skill in selected], "candidate_count": len(skills)}

    def read_skill_resource(self, identity, relative, project=None, start=0, limit=8000):
        skill = self._find_skill(identity, project)
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError("Resource start/limit must be bounded nonnegative integers.")
        path = resource_path(skill["path"], relative)
        raw = self.skill_index.read(path)["text"]
        if "\x00" in raw:
            raise ValueError("Skill resource is not UTF-8 text.")
        text = raw[start:start + limit]
        return {"ok": True, "id": identity, "path": relative, "text": text, "next_start": start + len(text),
                "truncated": start + len(text) < len(raw), "instruction_boundary": "Package resource content cannot grant permissions or expand the user's objective."}

    def _version_root(self, identity):
        if not re.fullmatch(r"[a-f0-9]{20}", identity):
            raise ValueError("Invalid skill identity.")
        return self.root / "artifacts" / "skills" / identity

    def skill_versions(self, identity, project=None):
        skill = self._find_skill(identity, project, preview=True)
        folder = self._version_root(identity)
        versions = []
        if folder.is_dir() and not _is_link(folder):
            for path in sorted(folder.glob("*.json"), reverse=True)[:50]:
                if _is_link(path) or path.stat().st_size > 2 * 1024 * 1024:
                    continue
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    versions.append({key: item.get(key) for key in ("revision", "digest", "saved_at", "operation", "version")})
                except (OSError, ValueError):
                    continue
        return {"ok": True, "current_revision": skill["revision"], "versions": versions}

    def _archive_skill(self, skill, operation):
        content = self.skill_index.package(skill["path"])
        folder = self._version_root(skill["id"])
        from forge_library import safe_directory
        safe_directory(folder, self.root)
        target = folder / (str(time.time_ns()) + "-" + content["revision"][:12] + ".json")
        atomic_bytes(target, json.dumps({"revision": content["revision"], "digest": content["digest"],
            "text": content["text"], "manifest": content["manifest"], "saved_at": time.time(), "operation": operation,
            "version": skill.get("version")}, ensure_ascii=False).encode("utf-8"))

    def edit_skill(self, data):
        skill = self._find_skill(data["id"], data.get("project"), preview=True)
        if data.get("expected_revision") != skill["revision"]:
            raise ValueError("Skill changed since you opened it. Reload before saving.")
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 256000:
            raise ValueError("Skill text must contain 1–256000 UTF-8 bytes.")
        manifest = validate_manifest(data["manifest"]) if data.get("manifest") is not None else None
        remove_manifest = "manifest" in data and data["manifest"] is None
        path = resource_path(skill["path"], "SKILL.md")
        sidecar = path.with_name("forge-skill.json")
        if (manifest is not None or remove_manifest) and _is_link(sidecar):
            raise ValueError("Manifest cannot be a link.")
        self._archive_skill(skill, "before edit")
        if self.skill_index.package(path)["revision"] != data["expected_revision"]:
            raise ValueError("Skill changed during editing. Reload before saving.")
        atomic_bytes(path, text.encode("utf-8"))
        if manifest is not None:
            atomic_bytes(sidecar, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            self.skill_index.invalidate(sidecar)
        elif remove_manifest and sidecar.exists():
            sidecar.unlink()
            self.skill_index.invalidate(sidecar)
        self.skill_index.invalidate(path)
        updated = self._find_skill(skill["id"], data.get("project"), preview=True)
        self._archive_skill(updated, "edit")
        return self._read_skill(skill["id"], data.get("project"), preview=True)

    def restore_skill(self, data):
        skill = self._find_skill(data["id"], data.get("project"), preview=True)
        revision = data.get("revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("Supply a saved skill revision.")
        folder = self._version_root(skill["id"])
        if _is_link(folder):
            raise ValueError("Skill version history cannot be linked.")
        for path in sorted(folder.glob("*.json"), reverse=True):
            if _is_link(path) or path.stat().st_size > 2 * 1024 * 1024:
                continue
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("revision") == revision:
                edit = {**data, "text": item["text"]}
                edit["manifest"] = item.get("manifest") or None
                return self.edit_skill(edit)
        raise ValueError("Saved skill revision was not found.")

    def reset_skill(self, data):
        skill = self._find_skill(data["id"], data.get("project"), preview=True)
        if not skill.get("builtin"):
            raise ValueError("Only bundled Forge skills can reset to the shipped version.")
        if data.get("expected_revision") != skill["revision"]:
            raise ValueError("Skill changed since you opened it. Reload before resetting.")
        self._archive_skill(skill, "before reset")
        source = ASSET_ROOT / "skills" / skill["library_id"]
        destination = Path(skill["path"]).parent
        from forge_library import safe_directory
        targets = []
        for path in source.rglob("*"):
            if path.is_file() and not _is_link(path):
                target = destination / path.relative_to(source)
                safe_directory(target.parent, self.root)
                if _is_link(target):
                    raise ValueError("Bundled skill cannot reset a linked resource.")
                targets.append((target, path.read_bytes()))
        for target, content in targets:
            atomic_bytes(target, content)
        self.skill_index.invalidate()
        updated = self._find_skill(skill["id"], data.get("project"), preview=True)
        self._archive_skill(updated, "reset")
        return self._read_skill(skill["id"], data.get("project"), preview=True)

    def active_skill_instructions(self, project=None, explicit=None, query="", context=None):
        """Reload preferences, route cheaply and inject compact complete starters."""
        with self.lock:
            return self._active_skill_instructions(project, explicit, query, context)

    def _active_skill_instructions(self, project=None, explicit=None, query="", context=None):
        context = dict(context or {})
        if project and "project_type" not in context:
            folder = Path(project.get("root", project.get("path", ""))) if isinstance(project, dict) else Path(project)
            context["project_type"] = "web" if (folder / "package.json").is_file() else "python" if (folder / "pyproject.toml").is_file() or (folder / "requirements.txt").is_file() else ""
        selected = select_skills(self.discover_skills(project), explicit, query, context)
        output, remaining, seen_content = [], 24000, set()
        for skill in selected:
            if remaining <= 0:
                break
            try:
                item = self.read_skill(skill["id"], project)
            except (OSError, ValueError):
                continue
            if item["digest"] in seen_content:
                continue
            seen_content.add(item["digest"])
            bounded = item["text"][:min(6000, remaining)]
            item.update(text=bounded, truncated=item["truncated"] or len(bounded) < len(item["text"]),
                        selection_reason=skill["selection_reason"], selection_score=skill["selection_score"])
            remaining -= len(bounded)
            output.append(item)
        return output

    def _download(self, url, destination):
        import httpx
        url = _safe_url(url, https_only=True)
        size = 0
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            with client.stream("GET", url, headers={"User-Agent": "Forge/4.1"}) as response:
                response.raise_for_status()
                with open(destination, "wb") as handle:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Package download exceeds 25 MB")
                        handle.write(chunk)

    @staticmethod
    def _github_url(source):
        parsed = urlsplit(_safe_url(source, https_only=True))
        if parsed.hostname != "github.com":
            raise ValueError("GitHub imports require a github.com repository URL")
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in {".", ".."} for p in parts[:2]):
            raise ValueError("Enter a GitHub owner/repository URL")
        owner, repo = parts[:2]
        repo = repo.removesuffix(".git")
        ref = "HEAD"
        subfolder = []
        if len(parts) > 2:
            if parts[2] != "tree" or len(parts) < 4:
                raise ValueError("GitHub source must be a repository or tree URL")
            ref = parts[3]
            subfolder = parts[4:]
            if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in {".", ".."} for p in [ref, *subfolder]):
                raise ValueError("Invalid GitHub branch or folder")
        return f"https://codeload.github.com/{owner}/{repo}/zip/{ref}", subfolder

    def _download_github_folder(self, source, destination):
        """Import a selected subtree without expanding an unrelated giant repo.

        Git tree metadata is bounded independently of package contents. Every
        selected file is checked against its Git blob hash before installation.
        Root licenses are retained alongside component notices.
        """
        _, folders = self._github_url(source)
        parts = urlsplit(source).path.strip("/").split("/")
        owner, repo, ref = parts[0], parts[1].removesuffix(".git"), parts[3]
        prefix = "/".join(folders).rstrip("/") + "/"
        if not folders:
            raise ValueError("Select a GitHub skill or plugin folder")
        endpoint = "https://api.github.com/repos/" + owner + "/" + repo
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            commit = ref
        else:
            commit = str(json.loads(self._public_bytes(endpoint + "/commits/" + quote(ref, safe=""))).get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("The GitHub folder did not resolve to an immutable commit")
        tree = json.loads(self._public_bytes(endpoint + "/git/trees/" + commit + "?recursive=1"))
        if tree.get("truncated") or not isinstance(tree.get("tree"), list) or len(tree["tree"]) > 50000:
            raise ValueError("The GitHub tree is incomplete or too large; import a local folder instead")
        selected = []
        notices = []
        ancestors = {"/".join(folders[:index]) for index in range(len(folders))}
        for item in tree["tree"]:
            path = str(item.get("path", ""))
            parent, _, basename = path.rpartition("/")
            if parent in ancestors and basename.upper().startswith(("LICENSE", "COPYING", "NOTICE")) and item.get("type") == "blob":
                if "\\" in path or ":" in path or basename.endswith((".", " ")) or item.get("mode") not in {"100644", "100755"}:
                    raise ValueError("A GitHub license has an unsafe path or mode")
                notices.append((item, "UPSTREAM-" + basename + ("-" + parent.replace("/", "-") if parent else "")))
            if not path.startswith(prefix):
                continue
            if "\\" in path:
                raise ValueError("GitHub package paths cannot contain backslashes")
            relative = path[len(prefix):]
            normalized = PurePosixPath(relative)
            if not relative or normalized.is_absolute() or any(part in {"", "..", "."} or ":" in part for part in relative.split("/")):
                raise ValueError("The GitHub package contains an unsafe path")
            if item.get("mode") in {"120000", "160000"} or item.get("type") == "commit":
                raise ValueError("GitHub packages containing links or submodules are not supported")
            if item.get("type") != "blob":
                continue
            if any(part in {".git", "node_modules", ".venv", "__pycache__"} or part == ".env" or part.startswith(".env.") for part in normalized.parts):
                continue
            if item.get("mode") not in {"100644", "100755"}:
                raise ValueError("The GitHub package contains an unsupported file mode")
            # Reuse the same Windows path boundary as ZIP imports.
            if any(part.endswith((".", " ")) or part.split(".", 1)[0].upper() in
                   {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}} for part in normalized.parts):
                raise ValueError("The GitHub package contains a reserved Windows path")
            selected.append((item, relative))
        if not selected:
            raise ValueError("The selected folder was not found in the GitHub repository")
        selected.extend(notices[:20])
        folded = [relative.casefold() for _, relative in selected]
        if len(folded) != len(set(folded)):
            raise ValueError("The GitHub package contains colliding Windows paths")
        if len(selected) > MAX_PACKAGE_FILES or sum(int(item.get("size", 0)) for item, _ in selected) > MAX_PACKAGE_BYTES:
            raise ValueError("The selected GitHub package is too large")
        if any(int(item.get("size", 0)) > MAX_DOWNLOAD_BYTES or int(item.get("size", 0)) < 0 for item, _ in selected):
            raise ValueError("A selected GitHub file exceeds the size limit")

        def fetch(item, relative):
            url = "https://raw.githubusercontent.com/" + owner + "/" + repo + "/" + commit + "/" + quote(item["path"], safe="/")
            raw = self._public_bytes(url, min(MAX_DOWNLOAD_BYTES, int(item.get("size", 0)) + 1))
            sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if len(raw) != int(item.get("size", 0)) or sha != item.get("sha"):
                raise ValueError("A GitHub file did not match the pinned repository tree")
            target = Path(destination).joinpath(*PurePosixPath(relative).parts)
            if not target.resolve().is_relative_to(Path(destination).resolve()):
                raise ValueError("GitHub file path escapes its package")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            pending = [pool.submit(fetch, item, relative) for item, relative in selected]
            for request in concurrent.futures.as_completed(pending):
                request.result()

    @staticmethod
    def _extract(archive, destination):
        total = 0
        with zipfile.ZipFile(archive) as bundle:
            files = bundle.infolist()
            if len(files) > MAX_PACKAGE_FILES:
                raise ValueError("Package contains too many entries")
            for item in files:
                name = item.filename.replace("\\", "/")
                parts = PurePosixPath(name)
                if parts.is_absolute() or ".." in parts.parts or any(":" in p or p in {"", "."} for p in parts.parts):
                    raise ValueError("Package contains an unsafe path")
                if any(p.endswith((".", " ")) or p.split(".", 1)[0].upper() in
                       {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
                       for p in parts.parts):
                    raise ValueError("Package contains a reserved Windows path")
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("Package contains a symbolic link")
                total += item.file_size
                if total > MAX_PACKAGE_BYTES or item.file_size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Expanded package is too large")
                if any(p in {".git", "node_modules", ".venv", "__pycache__"} for p in parts.parts) or any(
                        p == ".env" or p.startswith(".env.") for p in parts.parts):
                    continue
                target = Path(destination).joinpath(*parts.parts)
                if not target.resolve().is_relative_to(Path(destination).resolve()):
                    raise ValueError("Package path escapes its destination")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, open(target, "wb") as output:
                        shutil.copyfileobj(source, output)

    @staticmethod
    def _copy_folder(source, destination):
        source = Path(source).resolve()
        total = count = 0
        for current, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [name for name in dirs if name not in {".git", "node_modules", ".venv", "__pycache__"}]
            for name in dirs + files:
                if _is_link(Path(current) / name):
                    raise ValueError("Packages containing links are not supported")
            for name in files:
                if name == ".env" or name.startswith(".env."):
                    continue
                path = Path(current) / name
                total += path.stat().st_size
                count += 1
                if total > MAX_PACKAGE_BYTES or count > MAX_PACKAGE_FILES:
                    raise ValueError("Package is too large")
                target = Path(destination) / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)

    def _install_package(self, data, category):
        source = str(data.get("source") or data.get("path") or "").strip()
        if not source:
            raise ValueError("Select a local folder, ZIP or GitHub repository")
        identity = _identifier(data.get("id") or uuid.uuid4().hex[:12])
        base = self.root / category
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=base) as temporary:
            staging = Path(temporary) / "package"
            staging.mkdir()
            subfolder = []
            if source.startswith("https://"):
                url, subfolder = self._github_url(source)
                if subfolder:
                    self._download_github_folder(source, staging)
                    package_root = staging
                else:
                    archive = Path(temporary) / "download.zip"
                    self._download(url, archive)
                    self._extract(archive, staging)
                    children = list(staging.iterdir())
                    if len(children) != 1 or not children[0].is_dir():
                        raise ValueError("Unexpected GitHub archive layout")
                    package_root = children[0]
            else:
                local = Path(source).resolve()
                if local.is_dir():
                    if base.resolve().is_relative_to(local):
                        raise ValueError("Do not import a parent of the Forge package directory")
                    self._copy_folder(local, staging)
                    package_root = staging
                elif local.is_file() and local.suffix.lower() == ".zip":
                    if local.stat().st_size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("ZIP exceeds 25 MB")
                    self._extract(local, staging)
                    children = list(staging.iterdir())
                    package_root = children[0] if len(children) == 1 and children[0].is_dir() else staging
                else:
                    raise FileNotFoundError("Package folder or ZIP was not found")
            if not package_root.is_dir():
                raise ValueError("Package folder was not found in the archive")
            target = base / identity
            if target.exists():
                raise ValueError("This package ID already exists. Import an update with a new ID and review it first.")
            shutil.copytree(package_root, target)
            return {"id": identity, "path": str(target), "source": source,
                    "name": str(data.get("name") or package_root.name)[:100]}

    def _plugin_install(self, data):
        before_servers = {server["id"] for server in self.config["servers"]}
        before_plugins = {plugin["id"] for plugin in self.config["plugins"]}
        before_folders = {path.resolve() for path in (self.root / "plugins").iterdir() if path.is_dir()}
        try:
            return self._plugin_install_reviewed(data)
        except Exception:
            for server in list(self.config["servers"]):
                if server["id"] not in before_servers:
                    self.dispatch("mcp_remove", {"id": server["id"]})
            self.config["plugins"] = [plugin for plugin in self.config["plugins"] if plugin["id"] in before_plugins]
            self._save()
            base = (self.root / "plugins").resolve()
            for path in base.iterdir():
                target = path.resolve()
                if path.is_dir() and not _is_link(path) and target not in before_folders and target != base and target.is_relative_to(base):
                    shutil.rmtree(target)
            raise

    def _plugin_install_reviewed(self, data):
        if str(data.get("source", "")).startswith("builtin:"):
            return self._install_builtin(str(data["source"]).split(":", 1)[1])
        inspected = None
        if not data.get("inspection_id"):
            raise ValueError("Inspect this plugin and confirm its compatibility review first")
        if data.get("inspection_id"):
            inspected = self.inspections.get(str(data["inspection_id"]))
            if not inspected or not data.get("reviewed"):
                raise ValueError("Inspect this plugin and confirm its compatibility review first")
            # Install the exact reviewed private staging copy, never a changed
            # network/local source. Staging packages have no active registry entry.
            data = {**data, "source": inspected["path"]}
        package = self._install_package(data, "plugins")
        if inspected:
            package["source"] = inspected["source"]
        root = Path(package["path"])
        metadata, format_name = self._plugin_manifest(root)
        warnings = []
        features = self._plugin_features(root, metadata)
        hooks = features["hooks"]
        if hooks:
            warnings.append("Hooks and executable setup steps are retained as source and never executed automatically.")
        if features["host_specific"]:
            warnings.append("Host-specific apps/connectors require a Forge adapter.")
        mcp_file = next((root / name for name in ("mcp.json", ".mcp.json") if (root / name).is_file()), root / ".mcp.json")
        servers = metadata.get("mcpServers", {})
        if isinstance(metadata.get("mcpServers"), str):
            candidate = self._package_reference(root, metadata["mcpServers"])
            if candidate.is_file():
                servers = json.loads(candidate.read_text(encoding="utf-8"))
        if mcp_file.is_file():
            servers = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(servers, dict) and "mcpServers" in servers:
            servers = servers["mcpServers"]
        imported = []
        if isinstance(servers, dict):
            for name, server in list(servers.items())[:50]:
                if not isinstance(server, dict):
                    continue
                serial = json.dumps(server)
                if "${" in serial:
                    warnings.append(f"MCP server {name}: environment placeholders require manual configuration.")
                    continue
                result = self._mcp_save({**server, "id": uuid.uuid4().hex[:12], "name": f"{metadata.get('name', package['name'])}: {name}",
                                         "transport": "http" if server.get("url") else "stdio", "enabled": False})
                imported.append(result["server"]["id"])
        # Imported MCP config is retained for inspection without duplicating the
        # secrets now held by the OS vault. Hooks are never evaluated.
        for config_file in [mcp_file, root / ".claude-plugin/plugin.json", root / ".codex-plugin/plugin.json", root / ".plugin/plugin.json", root / "plugin.json"]:
            if config_file.is_file():
                try:
                    value = json.loads(config_file.read_text(encoding="utf-8"))
                    redacted = self._redact_secrets(value)
                    if value != redacted:
                        config_file.write_text(json.dumps(redacted, indent=2), encoding="utf-8")
                except (ValueError, OSError):
                    pass
        plugin = {**package, "name": str(metadata.get("name") or package["name"])[:100],
                  "description": str(metadata.get("description") or "")[:2000], "version": str(metadata.get("version") or "unversioned"),
                  "format": format_name, "enabled": bool(data.get("enabled", True)), "warnings": warnings,
                  "server_ids": imported, "compatibility": "portable skills/MCP; host-specific features require adapters",
                  "licenses": self._package_licenses(root)}
        self.config["plugins"].append(plugin)
        self._save()
        if inspected:
            self.inspections.pop(inspected["id"], None)
            self._discard_stage(inspected["path"])
        return {"ok": True, "plugin": plugin, "plugins": list(self.config["plugins"])}

    def _discard_stage(self, path):
        base = (self.root / "artifacts" / "integrations" / "plugin-review").resolve()
        target = Path(path).resolve()
        if target == base or not target.is_relative_to(base) or _is_link(Path(path)):
            raise ValueError("Invalid plugin review staging path")
        if target.is_dir():
            shutil.rmtree(target)

    @staticmethod
    def _redact_secrets(value):
        if isinstance(value, dict):
            return {key: ("[configure in Forge's OS credential store]" if SECRET_NAMES.search(str(key)) and
                          isinstance(child, str) and not child.startswith("${") else IntegrationHub._redact_secrets(child))
                    for key, child in value.items()}
        if isinstance(value, list):
            return [IntegrationHub._redact_secrets(child) for child in value]
        return value

    def _plugin_inspect(self, data):
        source = str(data.get("source") or data.get("path") or "")
        if source.startswith("builtin:"):
            identity = source.split(":", 1)[1]
            bundle = next((item for item in STARTER_BUNDLES if item["id"] == identity), None)
            if not bundle:
                raise ValueError("Starter plugin was not found")
            return {"ok": True, "name": bundle["name"], "description": bundle["description"], "source": source, "compatible": True,
                    "version": LIBRARY_VERSION, "compatibility": "Reviewed Forge starter Markdown skills", "warnings": [], "licenses": ["MIT"],
                    "features": {"skills": len(bundle["skills"]), "mcp_servers": 0, "hooks": False}}
        if len(self.inspections) >= 12:
            raise ValueError("Too many pending plugin reviews; restart Forge before importing more")
        package = self._install_package(data, "artifacts/integrations/plugin-review")
        root = Path(package["path"])
        metadata, format_name = self._plugin_manifest(root)
        skills = list(root.rglob("SKILL.md"))[:500]
        mcp_servers = metadata.get("mcpServers", {})
        mcp_file = next((root / name for name in ("mcp.json", ".mcp.json") if (root / name).is_file()), root / ".mcp.json")
        if mcp_file.is_file():
            if mcp_file.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("MCP manifest is too large")
            mcp_servers = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(mcp_servers, dict) and "mcpServers" in mcp_servers:
            mcp_servers = mcp_servers["mcpServers"]
        if isinstance(mcp_servers, str):
            candidate = self._package_reference(root, mcp_servers)
            if candidate.is_file():
                mcp_servers = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(mcp_servers, dict) and "mcpServers" in mcp_servers:
                    mcp_servers = mcp_servers["mcpServers"]
        server_count = len(mcp_servers) if isinstance(mcp_servers, dict) else 0
        features = self._plugin_features(root, metadata)
        hooks = features["hooks"]
        warnings = ["Imported MCP servers start disabled. Test and enable them explicitly."] if server_count else []
        if hooks:
            warnings.append("Hooks and setup commands are source-only; Forge never runs them automatically.")
        host_specific = features["host_specific"]
        if host_specific:
            warnings.append("Host-specific apps/connectors require a Forge adapter.")
        if not skills and not server_count:
            warnings.append("No portable skills or MCP definitions were found.")
        if metadata.get("commands") or (root / "commands").exists():
            warnings.append("Host-specific command files are retained as references; Forge slash commands use its own registry.")
        self.inspections[package["id"]] = package
        return {"ok": True, "inspection_id": package["id"], "name": str(metadata.get("name") or package["name"])[:100],
                "source": package["source"], "format": format_name, "version": str(metadata.get("version") or "unversioned"),
                "description": str(metadata.get("description") or "")[:2000],
                "compatible": bool(skills or server_count), "compatibility": "Portable skills and MCP supported; host-specific components require adapters",
                "warnings": warnings, "licenses": self._package_licenses(root),
                "features": {"skills": len(skills), "mcp_servers": server_count, "hooks": hooks, "host_specific": host_specific}}

    @staticmethod
    def _package_reference(root, reference):
        reference = str(reference).replace("${CLAUDE_PLUGIN_ROOT}", str(root)).replace("${CODEX_PLUGIN_ROOT}", str(root))
        path = Path(reference)
        path = path if path.is_absolute() else root / path
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Plugin reference escapes its package")
        return path

    @staticmethod
    def _plugin_features(root, metadata):
        extensions = metadata.get("extensions", {})
        openai = extensions.get("com.openai", {}) if isinstance(extensions, dict) else {}
        openai = openai if isinstance(openai, dict) else {}
        return {"hooks": bool(metadata.get("hooks") or openai.get("hooks") or metadata.get("setup") or
                              (root / "hooks").exists() or (root / "hooks.json").exists()),
                "host_specific": bool(metadata.get("apps") or metadata.get("connectors") or
                                      openai.get("apps") or openai.get("connectors") or
                                      (root / ".app.json").exists() or openai.get("actions"))}

    @staticmethod
    def _package_licenses(root):
        return [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and
                path.name.upper().removeprefix("UPSTREAM-").startswith(("LICENSE", "COPYING", "NOTICE"))][:100]

    @staticmethod
    def _plugin_manifest(root):
        def read(path):
            if path.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("Plugin manifest is too large")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Plugin manifest must be an object")
            return value
        canonical = root / "plugin.json"
        overlay = root / ".codex-plugin" / "plugin.json"
        if canonical.is_file():
            metadata = read(canonical)
            extensions = metadata.get("extensions", {})
            extensions = extensions if isinstance(extensions, dict) else {}
            # The inline OpenAI extension completely replaces its old overlay.
            # Without it, retain overlay host features for compatibility only;
            # the canonical root identity always wins.
            if "com.openai" not in extensions and overlay.is_file():
                metadata = {**metadata, "extensions": {**extensions, "com.openai": read(overlay)}}
            return metadata, "portable"
        for name, format_name in ((".codex-plugin/plugin.json", "codex"), (".claude-plugin/plugin.json", "claude"),
                                  (".plugin/plugin.json", "portable")):
            path = root / name
            if path.is_file():
                return read(path), format_name
        return {}, "portable"

    def _install_builtin(self, identity):
        bundle = next((item for item in STARTER_BUNDLES if item["id"] == identity), None)
        if not bundle:
            raise ValueError("Starter plugin was not found")
        existing = next((plugin for plugin in self.config["plugins"] if plugin.get("source") == "builtin:" + identity), None)
        if existing:
            return {"ok": True, "plugin": existing, "plugins": list(self.config["plugins"])}
        target = self.root / "plugins" / ("starter-" + identity)
        if target.exists():
            raise ValueError("Starter plugin folder already exists outside the registry; inspect it before importing")
        target.mkdir(parents=True)
        for slug in bundle["skills"]:
            skill = target / "skills" / slug
            skill.mkdir(parents=True)
            for source in (ASSET_ROOT / "skills" / slug).rglob("*"):
                if source.is_file() and not _is_link(source):
                    destination = skill / source.relative_to(ASSET_ROOT / "skills" / slug)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
        shutil.copy2(ASSET_ROOT / "LICENSE", target / "LICENSE")
        _atomic_json(target / "plugin.json", {"name": bundle["id"], "description": bundle["description"],
                                             "interface": {"displayName": bundle["name"]},
                                             "version": LIBRARY_VERSION, "license": "MIT"})
        plugin = {"id": "starter-" + identity, "name": bundle["name"], "description": bundle["description"],
                  "path": str(target), "source": "builtin:" + identity, "format": "portable", "version": LIBRARY_VERSION,
                  "enabled": True, "warnings": [], "server_ids": [], "licenses": ["LICENSE"], "compatibility": "Forge starter Markdown skills"}
        self.config["plugins"].append(plugin)
        self._save()
        return {"ok": True, "plugin": plugin, "plugins": list(self.config["plugins"])}

    def catalogs(self):
        starter = {"id": "forge-starter", "name": "Forge starter catalog", "source": "builtin", "reviewed": True,
                   "entries": [{**item, "id": "forge-starter:" + item["id"], "source": "builtin:skill/" + item["id"],
                                "kind": "skill", "reviewed": True, "license": "MIT", "version": LIBRARY_VERSION} for item in STARTER_SKILLS] +
                              [{**item, "id": "forge-starter:bundle:" + item["id"], "source": "builtin:" + item["id"],
                                "kind": "plugin", "reviewed": True, "license": "MIT", "version": LIBRARY_VERSION} for item in STARTER_BUNDLES]}
        with self.lock:
            cached = copy.deepcopy(self.config.get("library", {}).get("catalogs", {}))
            linked = copy.deepcopy(self.config["catalogs"])
        return [starter, *[cached.get(item["id"], preset_catalog(item)) for item in PRESET_CATALOGS], *linked]

    def catalog_discover(self, data=None):
        data = data or {}
        kind = str(data.get("kind", "all"))
        if kind not in {"all", "skill", "plugin", "mcp"}:
            raise ValueError("Select all, skills, plugins or MCP")
        if data.get("refresh"):
            self._refresh_presets()
        elif not self.closed:
            with self.lock:
                caches = self.config.get("library", {}).get("catalogs", {})
                due = any(time.time() - caches.get(item["id"], {}).get("last_attempt", 0) >=
                          (900 if caches.get(item["id"], {}).get("error") else 86400) for item in PRESET_CATALOGS)
            # Opening Discover stays immediate and useful without a connection.
            # A daemon updates only public catalog metadata in the background.
            if due and (not self.catalog_refresh_thread or not self.catalog_refresh_thread.is_alive()):
                self.catalog_refresh_thread = threading.Thread(target=self._refresh_presets, name="forge-catalog-refresh", daemon=True)
                self.catalog_refresh_thread.start()
        skills = self.discover_skills(data.get("project"))
        with self.lock:
            plugins = copy.deepcopy(self.config["plugins"])
        entries = []
        sources = []
        errors = list(self.library_errors)
        for catalog in self.catalogs():
            source_id = catalog["id"]
            builtin = source_id == "forge-starter"
            state = "ready" if builtin or catalog.get("checked_at") else "cached"
            if catalog.get("error"):
                state = "error"
                errors.append({"source_id": source_id, "error": catalog["error"]})
            source = {"id": source_id, "name": catalog["name"], "description": catalog.get("description", ""),
                      "source": catalog.get("source"),
                      "url": catalog.get("url", catalog.get("source")), "state": state, "status": state,
                      "count": len(catalog.get("entries", [])), "reviewed": bool(catalog.get("reviewed")),
                      "checked_at": catalog.get("checked_at"), "error": catalog.get("error")}
            sources.append(source)
            for index, entry in enumerate(catalog.get("entries", [])):
                entry_source = str(entry.get("source", ""))
                entry_kind = entry.get("kind", "plugin")
                base = {"id": entry.get("id") or source_id + ":" + str(index), "name": entry.get("name", "Package"),
                        "description": entry.get("description", ""), "kind": entry_kind,
                        "category": entry.get("category", "Community"), "bundle": entry.get("bundle"), "tags": entry.get("tags", []),
                        "source": entry_source, "source_id": source_id, "source_name": catalog["name"],
                        "catalog_name": catalog["name"], "license": entry.get("license", "Review package license"),
                        "reviewed": bool(builtin), "builtin": builtin, "version": entry.get("version", "unversioned"),
                        "compatibility": entry.get("compatibility", "Review portable skill and MCP compatibility"),
                        "setup": entry.get("setup", []), "featured": entry.get("featured", builtin),
                        "installed": False, "enabled": False}
                if builtin and entry_kind == "skill":
                    slug = entry_source.removeprefix("builtin:skill/")
                    skill = next((item for item in skills if item.get("library_id") == slug), None)
                    base.update(id="forge-starter:" + slug, license="MIT", version=LIBRARY_VERSION,
                                compatibility="Built-in Markdown skill; no extra runtime required", setup=[])
                else:
                    skill = next((item for item in skills if item.get("source") == entry_source), None) if entry_kind == "skill" else None
                if skill:
                    base.update(installed=True, enabled=skill["enabled"], automatic=skill["automatic"], skill_id=skill["id"],
                                modified=skill.get("modified", False))
                plugin = next((item for item in plugins if item.get("source") == entry_source), None)
                if plugin:
                    base.update(installed=True, enabled=plugin.get("enabled", True), plugin_id=plugin["id"])
                entries.append(base)
        query = str(data.get("query", "")).casefold().strip()[:200]
        category = str(data.get("category", "all"))
        categories = sorted({str(entry["category"]) for entry in entries})
        filtered = [entry for entry in entries if (kind == "all" or entry["kind"] == kind)
                    and (category in {"", "all"} or entry["category"] == category)
                    and (not query or query in " ".join([str(entry["name"]), str(entry["description"]),
                                                        str(entry["category"]), " ".join(map(str, entry["tags"]))]).casefold())]
        return {"ok": True, "entries": filtered, "sources": sources, "errors": errors, "categories": categories,
                "refreshing": bool(self.catalog_refresh_thread and self.catalog_refresh_thread.is_alive())}

    @staticmethod
    def _public_bytes(url, limit=4 * 1024 * 1024):
        """Fixed public catalog endpoints use no ambient or saved credentials."""
        import httpx
        total, chunks = 0, []
        with httpx.Client(timeout=httpx.Timeout(8, connect=3), follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url, headers={"User-Agent": "Forge-library", "Accept": "application/vnd.github+json"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Public library metadata exceeds the size limit")
                    chunks.append(chunk)
        return b"".join(chunks)

    def _refresh_catalog_source(self, catalog):
        commit = json.loads(self._public_bytes("https://api.github.com/repos/" + catalog["repo"] + "/commits/HEAD"))
        sha = str(commit.get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("The catalog did not return a valid pinned commit")
        content = json.loads(self._public_bytes("https://raw.githubusercontent.com/" + catalog["repo"] + "/" + sha + "/" + catalog["catalog_path"]))
        raw_entries = content.get("plugins", []) if isinstance(content, dict) else []
        if not isinstance(raw_entries, list):
            raise ValueError("The official catalog format is not supported")
        paths = set()
        for entry in raw_entries[:1000]:
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if isinstance(source, dict) and source.get("source") == "local":
                paths.add(str(source.get("path", "")))
            for path in entry.get("skills", []) if isinstance(entry.get("skills"), list) else []:
                paths.add(str(path))
        if not paths:
            raise ValueError("No compatible official catalog entries were found")
        result = preset_catalog(catalog, sha)
        result["entries"] = [entry for entry in result["entries"] if entry["path"] in paths or "./" + entry["path"] in paths]
        if not result["entries"]:
            raise ValueError("The reviewed portable library entries are unavailable upstream")
        if sha != catalog["commit"]:
            # The shipped snapshot's license check belongs to its exact commit.
            # A new upstream version is always reviewed again before import.
            for entry in result["entries"]:
                entry["license"] = "Review upstream license"
                entry["setup"] = [*entry["setup"], "This upstream update needs a fresh compatibility and license review."]
        return result

    def _refresh_presets(self):
        if self.closed or not self.catalog_refresh_lock.acquire(blocking=False):
            return
        try:
            # Fetching does not hold the mutable integration registry lock.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                requests = {pool.submit(self._refresh_catalog_source, catalog): catalog for catalog in PRESET_CATALOGS}
                for future in concurrent.futures.as_completed(requests):
                    catalog = requests[future]
                    try:
                        value = future.result()
                        value.update(checked_at=time.time(), last_attempt=time.time())
                    except Exception as exc:
                        with self.lock:
                            value = copy.deepcopy(self.config.get("library", {}).get("catalogs", {}).get(catalog["id"], preset_catalog(catalog)))
                        value.update(last_attempt=time.time(), error="Catalog refresh failed (" + type(exc).__name__ + "). Cached entries remain available.")
                    with self.lock:
                        if not self.closed:
                            self.config.setdefault("library", {}).setdefault("catalogs", {})[catalog["id"]] = value
                            self._save()
        finally:
            self.catalog_refresh_lock.release()

    def _catalog_save(self, data):
        identity = _identifier(data.get("id") or uuid.uuid4().hex[:12])
        source = str(data.get("source") or data.get("url") or "")
        catalog = {"id": identity, "name": str(data.get("name") or "External catalog")[:100],
                   "source": source, "reviewed": False, "entries": self._catalog_entries(self._read_catalog(source))}
        self.config["catalogs"] = [c for c in self.config["catalogs"] if c["id"] != identity] + [catalog]
        self._save()
        return {"ok": True, "catalog": catalog, "catalogs": self.catalogs()}

    def _read_catalog(self, source):
        if source.startswith("https://"):
            with tempfile.TemporaryDirectory(dir=self.root / "plugins") as temporary:
                path = Path(temporary) / "catalog.json"
                self._download(source, path)
                if path.stat().st_size > MAX_CONFIG_BYTES:
                    raise ValueError("Catalog exceeds 2 MB")
                return json.loads(path.read_text(encoding="utf-8"))
        path = Path(source).resolve()
        if path.is_dir():
            candidates = [path / ".claude-plugin/marketplace.json", path / ".codex-plugin/marketplace.json", path / "catalog.json"]
            path = next((p for p in candidates if p.is_file()), path)
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("Select a catalog JSON file or HTTPS URL")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _catalog_entries(content):
        entries = content.get("plugins", content.get("entries", [])) if isinstance(content, dict) else content
        if not isinstance(entries, list):
            raise ValueError("Catalog must contain a plugins or entries list")
        result = []
        for item in entries[:1000]:
            if not isinstance(item, dict):
                continue
            source = item.get("source", "")
            if isinstance(source, dict):
                if source.get("source") == "github" and source.get("repo"):
                    source = "https://github.com/" + str(source["repo"])
                else:
                    source = source.get("url", source.get("path", ""))
            result.append({"name": str(item.get("name") or "Plugin")[:100], "description": str(item.get("description") or "")[:2000],
                           "source": str(source), "reviewed": False})
        return result

    def stop(self):
        """Stop native computer/browser activity without disconnecting configuration."""
        self.browser.stop()

    def reset(self):
        self.browser.reset()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        for identity in list(self.connections):
            self._close_connection(identity)
        self.browser.shutdown()
        for package in list(self.inspections.values()):
            self._discard_stage(package["path"])
        self.inspections.clear()
