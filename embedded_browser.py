"""A raw WebView2 child confined to Forge's independently inspected browser pane."""
from __future__ import annotations

import json
import math
import threading
import time
from urllib.parse import urlsplit

from native_browser import BrowserGuard, WebView2Page, CLEAR_TARGET_SCRIPT

PANEL_ID = 'forge-browser-panel'


def validate_bounds(request, snapshot, client, panel_id=PANEL_ID):
    """CSS requests are claims; bounds always come from the host's DOM snapshot."""
    if panel_id not in (PANEL_ID, 'forge-preview-panel') or not isinstance(request, dict) or request.get('panel_id', panel_id) != panel_id:
        raise BrowserGuard('Unknown native browser panel.')
    if not isinstance(snapshot, dict) or not snapshot.get('visible') or snapshot.get('hud') or snapshot.get('modal'):
        raise BrowserGuard('Open the Browser panel in the full Forge workspace.')
    actual, viewport = snapshot.get('rect') or {}, snapshot.get('viewport') or {}
    claimed, claimed_viewport = request.get('rect', actual), request.get('viewport', viewport)
    def numbers(value, names):
        if not isinstance(value, dict) or any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k]) for k in names):
            raise BrowserGuard('Browser panel bounds must be finite numbers.')
        return [value[k] for k in names]
    x, y, width, height = numbers(actual, ('x','y','width','height'))
    vw, vh = numbers(viewport, ('width','height'))
    cx, cy, cw, ch = numbers(claimed, ('x','y','width','height'))
    cvw, cvh = numbers(claimed_viewport, ('width','height'))
    if min(vw, vh, width, height) <= 0 or x < 0 or y < 0 or x+width > vw+.5 or y+height > vh+.5:
        raise BrowserGuard('Browser panel falls outside the workspace viewport.')
    if any(abs(a-b) > 2 for a,b in zip((x,y,width,height),(cx,cy,cw,ch))) or abs(vw-cvw)>1 or abs(vh-cvh)>1:
        raise BrowserGuard('Browser layout changed. Refresh its panel bounds.')
    if not isinstance(client, (tuple, list)) or len(client) != 4 or min(client[2:]) <= 0:
        raise BrowserGuard('Native workspace viewport is unavailable.')
    left, top, native_width, native_height = client
    sx, sy = native_width/vw, native_height/vh
    if not .5 <= sx <= 5 or not .5 <= sy <= 5 or abs(sx-sy) > .08:
        raise BrowserGuard('DPI or browser zoom changed. Refresh the Browser panel.')
    bounds = (left+round(x*sx), top+round(y*sy), round(width*sx), round(height*sy))
    if bounds[0]<left or bounds[1]<top or bounds[0]+bounds[2]>left+native_width+1 or bounds[1]+bounds[3]>top+native_height+1:
        raise BrowserGuard('Native browser bounds exceed their owner.')
    return bounds


class EmbeddedWebView2Page(WebView2Page):
    """Shares the reviewed DOM tools, with no remote Python bridge or host objects."""
    embedded = True

    def __init__(self, owner, home, ui_dispatch, changed, panel_id=PANEL_ID,
                 profile_name='native-browser-profile', navigation_guard=None, diagnostics=False):
        from pathlib import Path
        from webview.platforms.edgechromium import WebView2, CoreWebView2CreationProperties
        from System.Drawing import Color
        from System.Windows.Forms import Panel, TextBox, DockStyle
        self.ui, self.changed, self.owner = ui_dispatch, changed, owner
        if panel_id not in (PANEL_ID, 'forge-preview-panel') or profile_name not in ('native-browser-profile', 'native-preview-profile'):
            raise BrowserGuard('Unknown embedded browser profile.')
        self.panel_id, self.navigation_guard, self.capture_diagnostics = panel_id, navigation_guard, diagnostics
        self.event = threading.Event()
        self.error = None
        self.visible = False
        self.bound = None
        self.disposed = False
        self.container = Panel()
        self.container.Visible = False
        self.container.BackColor = Color.FromArgb(25, 25, 27)
        self.container.Dock = getattr(DockStyle, 'None')
        self.control = WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = str(Path(home) / 'state' / profile_name)
        self.control.CreationProperties = props
        self.control.Dock = DockStyle.Fill
        self.container.Controls.Add(self.control)
        owner.Controls.Add(self.container)
        self.address = TextBox()  # Legacy navigation handlers; frontend owns the toolbar.
        self.control.CoreWebView2InitializationCompleted += self._initialized
        self._owner_resize = lambda sender, args: self._hide_ui()
        self._owner_visibility = lambda sender, args: self._hide_ui() if not owner.Visible else None
        owner.Resize += self._owner_resize
        owner.VisibleChanged += self._owner_visibility
        self.container.CreateControl()
        self.control.CreateControl()
        self.control.EnsureCoreWebView2Async(None)

    def _main_evaluate(self, script):
        from System import Action, String
        from System.Threading.Tasks import Task
        done, result = threading.Event(), {}
        def finish(task):
            try: result['value'] = json.loads(str(task.Result))
            except Exception as exc: result['error'] = exc
            finally: done.set()
        def execute(owner):
            source = urlsplit(str(owner.webview.CoreWebView2.Source))
            if source.scheme != 'http' or source.hostname not in ('127.0.0.1','localhost','::1'):
                raise BrowserGuard('The browser panel requires the local Forge workspace.')
            owner.webview.CoreWebView2.ExecuteScriptAsync(script).ContinueWith(Action[Task[String]](finish))
        self.ui(execute)
        if not done.wait(4): raise BrowserGuard('Forge panel layout did not respond. Open it again.')
        if 'error' in result: raise BrowserGuard('Forge panel layout could not be inspected.')
        return result.get('value')

    def _layout(self):
        return self._main_evaluate(r'''(() => {
         const id=__PANEL_ID__,panel=document.getElementById(id);
         if(!panel||panel.getAttribute('data-forge-browser-panel')!==id)return {visible:false};
         const r=panel.getBoundingClientRect(),s=getComputedStyle(panel);
         const clip=id==='forge-preview-panel'?panel.closest('.builder-page')?.getBoundingClientRect():null;
         const contained=!clip||(r.top>=clip.top-.5&&r.bottom<=clip.bottom+.5&&r.left>=clip.left-.5&&r.right<=clip.right+.5);
         return {visible:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&contained,
           preview_id:panel.getAttribute('data-forge-preview-id'),pane_token:panel.getAttribute('data-forge-pane-token'),
           hud:!!document.querySelector('.app.hud'),modal:!!document.querySelector('.modal-backdrop, dialog[open]'),
           viewport:{width:innerWidth,height:innerHeight},rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
        })()'''.replace('__PANEL_ID__', json.dumps(self.panel_id)))

    def bind(self, request=None, check=None):
        snapshot = self._layout()
        try: self._preview_identity(snapshot, request)
        except BrowserGuard:
            self.hide()
            raise
        def position(owner):
            from System.Drawing import Rectangle
            main = owner.webview
            client = (main.Left, main.Top, main.ClientSize.Width, main.ClientSize.Height)
            bounds = validate_bounds(request or {'panel_id':self.panel_id}, snapshot, client, self.panel_id)
            if check: check()
            if not owner.Visible:
                raise BrowserGuard('Open the Browser panel in the visible Forge workspace.')
            self.container.Bounds = Rectangle(*bounds)
            self.container.BringToFront()
            self.container.Visible = bool(owner.Visible)
            self.bound, self.visible = bounds, bool(owner.Visible)
            self.bound_pane_token = snapshot.get('pane_token')
            return bounds
        try: return self.ui(position)
        except BrowserGuard:
            self.hide()
            raise

    def preflight(self, check=None):
        if self.disposed: raise BrowserGuard('Browser view has been disposed.')
        if check: check()
        if not self.visible: return
        snapshot = self._layout()
        try: self._preview_identity(snapshot, {'pane_token': getattr(self, 'bound_pane_token', None)})
        except BrowserGuard:
            self.hide()
            raise
        def validate(owner):
            main = owner.webview
            bounds = validate_bounds({'panel_id':self.panel_id}, snapshot,
                (main.Left, main.Top, main.ClientSize.Width, main.ClientSize.Height), self.panel_id)
            if not owner.Visible or bounds != self.bound:
                self._hide_ui()
                raise BrowserGuard('Browser panel changed. Inspect again after reopening it.')
            if check: check()
        try: self.ui(validate)
        except BrowserGuard:
            self.hide()
            raise

    def _contained_layout(self):
        snapshot = self._layout()
        viewport = snapshot.get('viewport') or {}
        try:
            self._preview_identity(snapshot)
            # This only decides whether to request the frontend's open/scroll
            # action. bind still validates against the real native client/DPI.
            validate_bounds({'panel_id': self.panel_id}, snapshot,
                (0, 0, viewport.get('width', 0), viewport.get('height', 0)), self.panel_id)
            return True
        except (BrowserGuard, TypeError):
            return False

    def _preview_identity(self, snapshot, request=None):
        if self.panel_id != 'forge-preview-panel':
            return
        expected = getattr(self, 'preview_id', None)
        token = (request or {}).get('pane_token')
        if ((expected is not None and snapshot.get('preview_id') != expected)
                or (token is not None and snapshot.get('pane_token') != token)):
            raise BrowserGuard('Browser panel changed. Inspect again after reopening it.')

    def open_pane(self, check=None):
        self.show(check=check, request_open=True)

    def show(self, activate=True, check=None, request_open=False):
        if check: check()
        if request_open or not self._contained_layout():
            event = 'forge:preview-open' if self.panel_id == 'forge-preview-panel' else 'forge:browser-open'
            if check: check()
            self._main_evaluate('window.dispatchEvent(new CustomEvent('+json.dumps(event)+'));true')
            deadline = time.monotonic()+(10 if request_open else 3)
            while time.monotonic()<deadline:
                if check: check()
                if self._contained_layout(): break
                time.sleep(.05)
        if check:self.bind(check=check)
        else:self.bind()
        if activate:
            def reveal(owner):
                if check: check()
                owner.Show(); owner.Activate(); self.control.Focus()
            self.ui(reveal)

    def focused(self):
        return self.ui(lambda owner: bool(self.visible and owner.Visible and self.control.ContainsFocus))

    def set_viewport(self, width=None, height=None):
        self.preflight()
        def resize(owner):
            from System.Drawing import Rectangle
            from System.Windows.Forms import DockStyle
            if width is None:
                self.control.Dock = DockStyle.Fill
                return {'width': None, 'height': None, 'mode': 'fit'}
            scale = float(getattr(owner, 'DeviceDpi', 96)) / 96
            w, h = round(width * scale), round(height * scale)
            if w > self.container.ClientSize.Width or h > self.container.ClientSize.Height:
                raise BrowserGuard('This viewport needs a larger preview panel.')
            self.control.Dock = getattr(DockStyle, 'None')
            self.control.Bounds = Rectangle((self.container.ClientSize.Width-w)//2, 0, w, h)
            return {'width': width, 'height': height, 'mode': 'fixed'}
        return self.ui(resize)

    def _hide_ui(self):
        self.container.Visible = False
        self.visible = False
        self.bound = None
        try:self.control.CoreWebView2.ExecuteScriptAsync(CLEAR_TARGET_SCRIPT)
        except Exception:pass
        self.changed('hidden')

    def hide(self, check=None):
        def hide(owner):
            if check: check()
            self._hide_ui()
        if not self.disposed: self.ui(hide)

    def close(self, check=None):
        self.hide(check=check)  # Tab/HUD transitions preserve document and browser history.

    def dispose(self):
        if self.disposed: return
        def release(owner):
            self._hide_ui()
            owner.Resize -= self._owner_resize
            owner.VisibleChanged -= self._owner_visibility
            owner.Controls.Remove(self.container)
            self.control.Dispose()
            self.container.Dispose()
            self.address.Dispose()
            self.disposed = True
            self.changed('closed')
        self.ui(release)
