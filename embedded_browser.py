"""A raw WebView2 child confined to Forge's independently inspected browser pane."""
from __future__ import annotations

import json
import math
import threading
import time
from urllib.parse import urlsplit

from native_browser import BrowserGuard, WebView2Page

PANEL_ID = 'forge-browser-panel'


def validate_bounds(request, snapshot, client):
    """CSS requests are claims; bounds always come from the host's DOM snapshot."""
    if not isinstance(request, dict) or request.get('panel_id', PANEL_ID) != PANEL_ID:
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

    def __init__(self, owner, home, ui_dispatch, changed):
        from pathlib import Path
        from webview.platforms.edgechromium import WebView2, CoreWebView2CreationProperties
        from System.Drawing import Color
        from System.Windows.Forms import Panel, TextBox, DockStyle
        self.ui, self.changed, self.owner = ui_dispatch, changed, owner
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
        props.UserDataFolder = str(Path(home) / 'state/native-browser-profile')
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
         const panel=document.getElementById('forge-browser-panel');
         if(!panel||panel.getAttribute('data-forge-browser-panel')!=='forge-browser-panel')return {visible:false};
         const r=panel.getBoundingClientRect(),s=getComputedStyle(panel);
         return {visible:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden',
           hud:!!document.querySelector('.app.hud'),modal:!!document.querySelector('.modal-backdrop, dialog[open]'),
           viewport:{width:innerWidth,height:innerHeight},rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
        })()''')

    def bind(self, request=None):
        snapshot = self._layout()
        def position(owner):
            from System.Drawing import Rectangle
            main = owner.webview
            client = (main.Left, main.Top, main.ClientSize.Width, main.ClientSize.Height)
            bounds = validate_bounds(request or {'panel_id':PANEL_ID}, snapshot, client)
            self.container.Bounds = Rectangle(*bounds)
            self.container.BringToFront()
            self.container.Visible = bool(owner.Visible)
            self.bound, self.visible = bounds, bool(owner.Visible)
            return bounds
        try: return self.ui(position)
        except BrowserGuard:
            self.hide()
            raise

    def preflight(self):
        if self.disposed: raise BrowserGuard('Browser view has been disposed.')
        if not self.visible: return
        snapshot = self._layout()
        def check(owner):
            main = owner.webview
            bounds = validate_bounds({'panel_id':PANEL_ID}, snapshot,
                (main.Left, main.Top, main.ClientSize.Width, main.ClientSize.Height))
            if not owner.Visible or bounds != self.bound:
                self._hide_ui()
                raise BrowserGuard('Browser panel changed. Inspect again after reopening it.')
        try: self.ui(check)
        except BrowserGuard:
            self.hide()
            raise

    def show(self, activate=True):
        if not self._layout().get('visible'):
            self._main_evaluate("window.dispatchEvent(new CustomEvent('forge:browser-open'));true")
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                if self._layout().get('visible'): break
                time.sleep(.05)
        self.bind()
        if activate:
            self.ui(lambda owner: (owner.Show(), owner.Activate(), self.control.Focus()))

    def focused(self):
        return self.ui(lambda owner: bool(self.visible and owner.Visible and self.control.ContainsFocus))

    def _hide_ui(self):
        self.container.Visible = False
        self.visible = False
        self.bound = None

    def hide(self):
        if not self.disposed: self.ui(lambda owner: self._hide_ui())

    def close(self):
        self.hide()  # Tab/HUD transitions preserve document and browser history.

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
