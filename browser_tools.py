"""Permission-gated isolated browser and explicitly connected browser tabs.

The coordinator must authorize every execute() call. No user's browser profile,
cookies or passwords are imported. Native messaging connects only tabs the user
selected through the extension button.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socketserver
import subprocess
import struct
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

MAX_MESSAGE = 4 * 1024 * 1024


def browser_url(value):
    value = str(value).strip()
    parsed = urlsplit(value)
    if value != "about:blank" and (parsed.scheme not in ("http", "https") or not parsed.hostname):
        raise ValueError("Browser navigation requires an HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("Credentials must not be embedded in browser URLs")
    return value


def function_schema(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description,
             "parameters": {"type": "object", "properties": properties or {},
                            "required": required or [], "additionalProperties": False}}}


class BrowserTools:
    """Playwright lives on one dedicated thread, avoiding cross-thread handles."""
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="forge-browser")
        self.playwright = self.context = self.page = None
        self.snapshot = None
        self.bridge = None
        self.closed = False
        self.installation = {"state": "idle", "progress": [], "error": None}
        self.install_lock = threading.RLock()
        marker = self.root / "config" / "browser-runtime.json"
        if marker.is_file():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.root / "runtimes" / "playwright")
        self.binary_available = False
        self.last_installation_check = 0
        self.native = None

    def status(self, backend=None):
        if backend not in (None, 'native', 'isolated'):
            raise ValueError('Choose native or isolated browser diagnostics.')
        installed = importlib.util.find_spec("playwright") is not None
        if installed and (self.native is None or backend == 'isolated') and time.monotonic() - self.last_installation_check > 30:
            self.last_installation_check = time.monotonic()
            try:
                self.binary_available = self.pool.submit(self._check_installation).result(timeout=3)
            except Exception:
                pass
        with self.install_lock:
            installation = {**self.installation, "progress": list(self.installation["progress"])}
        isolated = {"available": installed and self.binary_available, "dependency_available": installed, "running": self.context is not None,
                "message": "Ready" if self.binary_available else "Install Chromium using Install browser" if installed else "Install requirements-integrations.txt to enable browser tools",
                "installation": installation}
        native = self.native.status() if self.native else {"available": False, "running": False, "engine": "WebView2", "message": "Open Forge desktop to use the built-in browser"}
        return {**isolated, "available": native.get('available', False) or isolated['available'],
                "running": native.get('running', False) or isolated['running'],
                "message": native['message'] if native.get('available') else isolated['message'],
                "default_backend": "native" if native.get('available') else "isolated",
                "native": native, "isolated": isolated,
                "connected_tabs": self.bridge.tabs() if self.bridge else [],
                "extension_available": self.bridge is not None}

    def native_action(self, action, data=None):
        if not self.native:
            if action == 'status':
                return self.status()['native']
            raise RuntimeError('Open Forge desktop to use its built-in browser.')
        return self.native.dispatch(action, data)

    def describe_target(self, arguments, context=None):
        if self.native and arguments.get('tab_id') is None and arguments.get('backend', 'native') == 'native':
            return self.native.describe_target(arguments, context)
        return {'app': 'browser:connected-tab' if arguments.get('tab_id') is not None else 'browser:isolated',
                'target': arguments.get('url') or ('Connected tab ' + str(arguments['tab_id']) if arguments.get('tab_id') is not None else 'Forge isolated browser')}

    def _check_installation(self):
        if self.playwright:
            return Path(self.playwright.chromium.executable_path).is_file()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as client:
            return Path(client.chromium.executable_path).is_file()

    def install_browser(self):
        with self.install_lock:
            if self.installation["state"] == "downloading":
                return {"ok": True, "installation": self.installation}
            if self.context:
                raise ValueError("Close the isolated browser before installing its runtime")
            if importlib.util.find_spec("playwright") is None:
                raise RuntimeError("Install requirements-integrations.txt first")
            self.installation = {"state": "downloading", "progress": [], "error": None}
        def download():
            from playwright._impl._driver import compute_driver_executable, get_driver_env
            target = self.root / "runtimes" / "playwright"
            target.mkdir(parents=True, exist_ok=True)
            environment = get_driver_env()
            environment["PLAYWRIGHT_BROWSERS_PATH"] = str(target)
            process = None
            try:
                node, driver = compute_driver_executable()
                process = subprocess.Popen([str(node), str(driver), "install", "chromium"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                for line in process.stdout:
                    with self.install_lock:
                        self.installation["progress"].append(line.strip()[:1000])
                        self.installation["progress"] = self.installation["progress"][-40:]
                if process.wait() != 0:
                    raise RuntimeError("Chromium download failed; check the download log and network connection")
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(target)
                config = self.root / "config" / "browser-runtime.json"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(json.dumps({"managed": True}), encoding="utf-8")
                self.binary_available = True
                with self.install_lock:
                    self.installation["state"] = "ready"
            except Exception as exc:
                with self.install_lock:
                    self.installation.update(state="error", error=str(exc)[:500])
            finally:
                if process and process.stdout:
                    process.stdout.close()
        threading.Thread(target=download, name="forge-browser-install", daemon=True).start()
        return {"ok": True, "installation": {**self.installation}}

    @staticmethod
    def schemas():
        text = {"type": "string"}
        target = {"tab_id": {"type": "integer", "description": "Optional explicitly connected extension tab"},
                  "backend": {"type": "string", "enum": ["native", "isolated"], "description": "Default is built-in native desktop browser when available"}}
        return [
            function_schema("browser_navigate", "Navigate Forge's built-in browser, optional isolated browser or explicitly connected tab", {**target, "url": text}, ["url"]),
            function_schema("browser_inspect", "Read page text and target selectors; returns a snapshot_id for actions", target),
            function_schema("browser_click", "Click a target from the current snapshot", {**target, "selector": text, "snapshot_id": text}, ["selector", "snapshot_id"]),
            function_schema("browser_type", "Replace a form field using the current snapshot", {**target, "selector": text, "text": text, "snapshot_id": text}, ["selector", "text", "snapshot_id"]),
            function_schema("browser_screenshot", "Capture the current browser viewport", target),
            function_schema("browser_tabs", "List only user-connected extension tabs"),
            function_schema("browser_close", "Close Forge's browser", {"backend": target['backend']}),
        ]

    def execute(self, name, arguments, context=None):
        if self.closed:
            raise RuntimeError("Browser tools have stopped")
        if arguments.get("tab_id") is not None:
            if not self.bridge:
                raise RuntimeError("No browser extension is connected")
            operation = name.removeprefix("browser_")
            if operation not in {"navigate", "inspect", "click", "type", "screenshot"}:
                raise ValueError("Unsupported connected-tab operation")
            data = dict(arguments)
            if operation == "navigate":
                data["url"] = browser_url(data["url"])
            result = self.bridge.command(int(data.pop("tab_id")), operation, data)
            if not result.get("ok", True):
                raise RuntimeError(str(result.get("error", "Browser action failed")))
            return self._bounded_result(result)
        if name == "browser_tabs":
            return {"ok": True, "tabs": self.bridge.tabs() if self.bridge else [],
                    "native": self.native.status() if self.native else None}
        if name not in {schema["function"]["name"] for schema in self.schemas()}:
            raise ValueError("Unknown browser tool")
        backend = arguments.get('backend', 'native' if self.native else 'isolated')
        if backend not in ('native', 'isolated'):
            return {'ok': False, 'error': 'Unknown browser backend.', 'not_executed': True}
        if backend == 'native':
            if not self.native:
                return {'ok': False, 'error': 'Open Forge desktop to use the built-in browser.', 'not_executed': True}
            return self.native.execute(name, arguments, context)
        return self.pool.submit(self._perform, name, dict(arguments)).result(timeout=50)

    def stop(self):
        if self.native:
            self.native.stop()

    def reset(self):
        if self.native:
            self.native.reset()

    def _start(self):
        if self.context:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Browser tools require requirements-integrations.txt") from exc
        self.playwright = sync_playwright().start()
        profile = self.root / "state" / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(profile), headless=os.getenv("FORGE_BROWSER_HEADLESS", "").lower() in {"1", "true"}, viewport={"width": 1280, "height": 800},
                accept_downloads=False, chromium_sandbox=True)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(15000)
        except Exception as exc:
            self.playwright.stop()
            self.playwright = None
            raise RuntimeError("Chromium could not start. Install it with python -m playwright install chromium") from exc

    def _perform(self, name, data):
        if name == "browser_close":
            if self.context:
                self.context.close()
                self.playwright.stop()
            self.context = self.playwright = self.page = None
            self.snapshot = None
            return {"ok": True}
        self._start()
        if name == "browser_navigate":
            self.page.goto(browser_url(data["url"]), wait_until="domcontentloaded", timeout=30000)
            return self._inspect()
        if name == "browser_inspect":
            return self._inspect()
        if name == "browser_screenshot":
            raw = self.page.screenshot(type="png", full_page=False)
            path = self._artifact(raw, ".png")
            return {"ok": True, "artifact": str(path), "mimeType": "image/png", "url": self.page.url,
                    "content": [{"type": "image", "artifact": str(path), "mimeType": "image/png"}]}
        if name in {"browser_click", "browser_type"}:
            expected = data.get("snapshot_id")
            selector = str(data.get("selector", ""))
            if not self.snapshot or expected != self.snapshot["id"] or self.page.url != self.snapshot["url"]:
                raise ValueError("Stale browser snapshot; inspect the page again")
            if selector not in self.snapshot["selectors"]:
                raise ValueError("Select a target returned by browser_inspect")
            target = self.page.locator(selector)
            # Saved targets include element signatures to guard DOM replacement.
            signature = target.evaluate("el => [el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.textContent.slice(0,200)]")
            if signature != self.snapshot["signatures"][selector] or target.count() != 1:
                raise ValueError("Browser target changed; inspect the page again")
            self.snapshot = None
            if name == "browser_click":
                target.click()
            else:
                target.fill(str(data.get("text", "")))
            return self._inspect()
        raise ValueError("Unknown browser operation")

    def _inspect(self):
        snap = uuid.uuid4().hex
        targets = self.page.evaluate("""(snap) => [...document.querySelectorAll('a,button,input,textarea,select,[role=button]')]
          .filter(el => el.getBoundingClientRect().width && el.getBoundingClientRect().height).slice(0,200)
          .map((el,i) => {const id=snap+'-'+i; el.setAttribute('data-forge-target',id); return {
            selector:'[data-forge-target="'+id+'"]', tag:el.tagName.toLowerCase(),
            label:(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||'').slice(0,300),
            type:el.getAttribute('type'), signature:[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.textContent.slice(0,200)]};})""", snap)
        self.snapshot = {"id": snap, "url": self.page.url, "selectors": {x["selector"] for x in targets},
                         "signatures": {x["selector"]: x.pop("signature") for x in targets}}
        return {"ok": True, "snapshot_id": snap, "url": self.page.url, "title": self.page.title(),
                "text": self.page.locator("body").inner_text()[:24000], "targets": targets}

    def _artifact(self, raw, suffix):
        target = self.root / "artifacts" / "browser"
        target.mkdir(parents=True, exist_ok=True)
        path = target / (uuid.uuid4().hex + suffix)
        path.write_bytes(raw)
        return path

    def _bounded_result(self, result):
        # Extension capture is a data URL; persist bytes rather than polluting context.
        capture = result.pop("data_url", None)
        if capture:
            import base64
            prefix, encoded = capture.split(",", 1)
            if prefix != "data:image/png;base64,".rstrip(",") or len(encoded) > MAX_MESSAGE:
                raise ValueError("Invalid browser screenshot")
            result["artifact"] = str(self._artifact(base64.b64decode(encoded, validate=True), ".png"))
            result["mimeType"] = "image/png"
            result["content"] = [{"type": "image", "artifact": result["artifact"], "mimeType": "image/png"}]
        if "text" in result:
            result["text"] = str(result["text"])[:24000]
        return result

    def start_bridge(self):
        if not self.bridge:
            self.bridge = ExtensionBridge(self.root)
        return self.bridge.status()

    def shutdown(self):
        if self.closed:
            return
        if self.native:
            self.native.shutdown()
        try:
            self.pool.submit(self._perform, "browser_close", {}).result(timeout=10)
        except Exception:
            pass
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.bridge:
            self.bridge.shutdown()


class ExtensionBridge:
    """Authenticated loopback IPC for the native host, with bounded tab leases."""
    def __init__(self, root):
        self.root = Path(root)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.connected = {}
        self.pending = {}
        bridge = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(10)
                line = self.rfile.readline(MAX_MESSAGE + 1)
                try:
                    if len(line) > MAX_MESSAGE:
                        raise ValueError("IPC message too large")
                    message = json.loads(line)
                    if not secrets.compare_digest(str(message.get("token", "")), bridge.token):
                        raise ValueError("Unauthenticated IPC request")
                    response = bridge.poll(message)
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)[:200]}
                self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False
        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="forge-native-bridge")
        self.thread.start()
        config = self.root / "config" / "browser-bridge.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        self.config_path = config
        config.write_text(json.dumps({"port": self.server.server_address[1], "token": self.token}), encoding="utf-8")
        try:
            config.chmod(0o600)
        except OSError:
            pass

    def status(self):
        return {"ok": True, "native_host": "org.forge.browser", "port": self.server.server_address[1],
                "connected_tabs": self.tabs()}

    def tabs(self):
        with self.lock:
            cutoff = time.monotonic() - 20
            self.connected = {key: value for key, value in self.connected.items() if value["seen"] > cutoff}
            return [{"id": key, "title": item["title"], "url": item["url"]} for key, item in self.connected.items()]

    def poll(self, message):
        with self.lock:
            for item in message.get("tabs", [])[:100]:
                self.connected[int(item["id"])] = {"title": str(item.get("title", ""))[:300],
                                                   "url": str(item.get("url", ""))[:2000], "seen": time.monotonic()}
            active = {int(item["id"]) for item in message.get("tabs", [])[:100]}
            for key in list(self.connected):
                if key not in active:
                    self.connected.pop(key)
            for result in message.get("results", [])[:100]:
                item = self.pending.get(str(result.get("id")))
                if item and not item["future"].done():
                    item["future"].set_result(result.get("result", {"ok": False, "error": "Missing response"}))
            commands = []
            for key, item in self.pending.items():
                if not item["sent"] and item["tab_id"] in active:
                    item["sent"] = True
                    commands.append({"id": key, "tab_id": item["tab_id"], "operation": item["operation"], "arguments": item["arguments"]})
            return {"ok": True, "commands": commands}

    def command(self, tab_id, operation, arguments):
        if tab_id not in {item["id"] for item in self.tabs()}:
            raise ValueError("This tab has not been connected by the user")
        key = uuid.uuid4().hex
        future = concurrent.futures.Future()
        with self.lock:
            self.pending[key] = {"tab_id": tab_id, "operation": operation, "arguments": arguments,
                                 "future": future, "sent": False}
        try:
            return future.result(timeout=30)
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        try:
            # Do not remove a newer coordinator's bridge configuration.
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if config.get("token") == self.token:
                self.config_path.unlink()
        except (OSError, ValueError):
            pass
        with self.lock:
            for item in self.pending.values():
                if not item["future"].done():
                    item["future"].set_exception(RuntimeError("Browser bridge stopped"))


def register_native_host(extension_id, host_executable, data_dir=None):
    """Explicit setup action; requires the installed host executable and extension ID."""
    if os.name != "nt":
        raise RuntimeError("This setup helper currently supports Windows")
    if not re.fullmatch(r"[a-p]{32}", str(extension_id)):
        raise ValueError("Enter the 32-character extension ID from the browser Extensions page")
    executable = Path(host_executable).resolve()
    if not executable.is_file() or executable.suffix.lower() != ".exe":
        raise ValueError("Select the Forge native messaging host executable")
    root = Path(data_dir or Path.home() / ".forge") / "config" / "native-messaging"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "org.forge.browser.json"
    manifest.write_text(json.dumps({"name": "org.forge.browser", "description": "Forge selected-tab bridge",
        "path": str(executable), "type": "stdio", "allowed_origins": [f"chrome-extension://{extension_id}/"]}, indent=2), encoding="utf-8")
    import winreg
    for vendor in ("Google\\Chrome", "Microsoft\\Edge"):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"Software\\{vendor}\\NativeMessagingHosts\\org.forge.browser") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
    return {"ok": True, "manifest": str(manifest)}


def native_host_main(data_dir=None):
    """Dedicated --native-host executable entry point; stdout is protocol only."""
    import socket
    root = Path(data_dir or os.environ.get("FORGE_HOME") or Path.home() / ".forge")
    source = sys.stdin.buffer
    target = sys.stdout.buffer
    while True:
        header = source.read(4)
        if not header:
            return
        if len(header) != 4:
            return
        length = struct.unpack("<I", header)[0]
        if length > MAX_MESSAGE:
            return
        payload = source.read(length)
        if len(payload) != length:
            return
        try:
            message = json.loads(payload)
            config = json.loads((root / "config" / "browser-bridge.json").read_text(encoding="utf-8"))
            message["token"] = config["token"]
            with socket.create_connection(("127.0.0.1", int(config["port"])), timeout=10) as connection:
                connection.sendall(json.dumps(message).encode("utf-8") + b"\n")
                reply = connection.makefile("rb").readline(MAX_MESSAGE + 1)
                response = json.loads(reply)
        except Exception:
            response = {"ok": False, "error": "Open Forge and enable the browser bridge"}
        encoded = json.dumps(response).encode("utf-8")
        target.write(struct.pack("<I", len(encoded)))
        target.write(encoded)
        target.flush()


if __name__ == "__main__":
    native_host_main()
