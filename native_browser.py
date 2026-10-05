"""Forge-owned WebView2 research window; remote pages receive no native bridge."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from browser_tools import browser_url
from computer_tools import session_available


class BrowserGuard(ValueError):
    """A rejected preflight, before any browser side effect was sent."""


class NativeBrowser:
    def __init__(self, home, ui_dispatch=None, view_factory=None, clock=time.monotonic, session_check=None):
        self.home = Path(home)
        self.ui_dispatch = ui_dispatch
        self.view_factory = view_factory
        self.clock = clock
        self.session_check = session_check or (lambda: bool(self.view_factory) or session_available())
        self.view = None
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='forge-native-browser')
        self.cancelled = threading.Event()
        self.snapshot = None
        self.generation = 0
        self.closed = False
        self.state = dict(running=False, loading=False, url='about:blank', title='', error=None)

    @staticmethod
    def _run(context):
        return str((context or {}).get('run_id') or 'interactive')

    def _check(self, context=None):
        cancel = (context or {}).get('cancel')
        if self.closed or self.cancelled.is_set() or (hasattr(cancel, 'is_set') and cancel.is_set()):
            raise BrowserGuard('Browser action cancelled.')
        if not self.session_check():
            raise BrowserGuard('Windows desktop session is unavailable. Unlock Windows before continuing browser work.')

    def _changed(self, kind, value=None):
        with self.lock:
            if kind == 'navigation':
                self.generation += 1
                self.snapshot = None
                self.state.update(url=str(value), loading=True, error=None)
            elif kind == 'loaded':
                self.state.update(loading=False, error=value)
            elif kind == 'title':
                self.state['title'] = str(value or '')[:500]
            elif kind == 'closed':
                self.generation += 1
                self.snapshot = None
                self.state.update(running=False, loading=False)
                self.view = None

    def status(self):
        with self.lock:
            available = not self.closed and bool(self.view_factory or os.name == 'nt' and self.ui_dispatch)
            return dict(available=available, engine='WebView2', **self.state,
                message=self.state['error'] or ('Ready — uses the Windows web runtime' if available else 'Open Forge desktop to use the built-in browser'),
                capabilities=['navigate', 'inspect', 'click', 'type', 'screenshot', 'back', 'forward', 'reload'])

    def _start(self):
        if self.view is not None:
            return
        if not self.status()['available']:
            raise BrowserGuard('The built-in browser requires Forge desktop on Windows.')
        if self.view_factory:
            view = self.view_factory(self._changed)
        else:
            view = self.ui_dispatch(lambda owner: WebView2Page(owner, self.home, self.ui_dispatch, self._changed))
        with self.lock:
            self.view = view
            self.state.update(running=True, error=None)
        try:
            view.ready()
        except Exception as exc:
            try:
                view.close()
            finally:
                with self.lock:
                    self.view = None
                    self.state.update(running=False, loading=False, error=str(exc)[:400])
            raise

    def dispatch(self, action, data=None):
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise ValueError('Expected browser controls object.')
        if action == 'status':
            return self.status()
        if action not in ('open', 'show', 'navigate', 'back', 'forward', 'reload', 'close'):
            raise ValueError('Unknown native browser control.')
        return self.pool.submit(self._manual, action, data).result(timeout=45)

    def _manual(self, action, data):
        if action == 'close':
            if self.view:
                self.view.close()
            return {'ok': True, **self.status()}
        # A new direct user command may continue after Stop; old agent requests
        # retain their own cancellation event and cannot reuse a cleared snapshot.
        self.cancelled.clear()
        self._check()
        self._start()
        self.view.show()
        if action in ('open', 'navigate') and (action == 'navigate' or data.get('url')):
            self.view.navigate(browser_url(data.get('url', 'about:blank')))
        elif action in ('back', 'forward', 'reload'):
            self.view.history(action)
        return {'ok': True, **self.status()}

    def describe_target(self, args=None, context=None):
        try:
            self._check(context)
            if (args or {}).get('snapshot_id'):
                self._snapshot(args, context, focus=False)
            status = self.status()
            return {'app': 'forge:native-browser', 'target': str((args or {}).get('url') or status['url']),
                    'backend': 'native'}
        except BrowserGuard as exc:
            return {'ok': False, 'error': str(exc), 'not_executed': True}

    def execute(self, name, args, context=None):
        try:
            return self.pool.submit(self._perform, name, dict(args), context).result(timeout=45)
        except BrowserGuard as exc:
            return {'ok': False, 'error': str(exc), 'not_executed': True}

    def _snapshot(self, args, context, focus=True):
        self._check(context)
        with self.lock:
            snapshot = self.snapshot
            if not snapshot or args.get('snapshot_id') != snapshot['id'] or snapshot['run'] != self._run(context):
                raise BrowserGuard('Stale browser snapshot. Inspect this page again.')
            if self.clock() - snapshot['time'] > 30 or snapshot['generation'] != self.generation:
                raise BrowserGuard('Browser page changed or snapshot expired. Inspect again.')
            if self.state['loading'] or snapshot['url'] != self.state['url'] or not self.view:
                raise BrowserGuard('Browser page is navigating or closed. Inspect again.')
        if focus and not self.view.focused():
            raise BrowserGuard('Forge browser is not focused. Open its window, then inspect again.')
        return snapshot

    def _perform(self, name, data, context):
        self._check(context)
        if name == 'browser_close':
            if self.view:
                self.view.close()
            return {'ok': True}
        if name == 'browser_navigate':
            try:
                url = browser_url(data.get('url', ''))
            except ValueError as exc:
                raise BrowserGuard(str(exc)) from None
            self._start()
            try:
                self._check(context)
            except BrowserGuard as exc:
                # Starting an owned window is already a completed host action.
                raise RuntimeError('Browser window opened; navigation was cancelled before dispatch.') from exc
            self.view.show()
            self.view.navigate(url)
            return {'ok': True, 'url': url, 'loading': True, 'next_action': 'Use browser_inspect after navigation finishes.'}
        if name not in ('browser_inspect', 'browser_screenshot', 'browser_click', 'browser_type'):
            raise BrowserGuard('Unsupported native browser tool.')
        if self.view is None:
            raise BrowserGuard('Open Forge Browser or use browser_navigate before inspecting this page.')
        if name == 'browser_inspect':
            return self._inspect(context)
        if name == 'browser_screenshot':
            with self.lock:
                generation = self.generation
            self._check(context)
            raw = self.view.screenshot()
            with self.lock:
                if generation != self.generation:
                    raise BrowserGuard('Browser navigated while capturing. Inspect again.')
            directory = self.home / 'artifacts/browser'
            directory.mkdir(parents=True, exist_ok=True)
            artifact = directory / (uuid4().hex + '.png')
            artifact.write_bytes(raw)
            return {'ok': True, 'artifact': str(artifact), 'mimeType': 'image/png', 'url': self.state['url'],
                'content': [{'type': 'image', 'artifact': str(artifact), 'mimeType': 'image/png'}]}
        snapshot = self._snapshot(data, context, focus=False)
        selector = data.get('selector')
        if selector not in snapshot['signatures']:
            raise BrowserGuard('Choose a selector returned by browser_inspect.')
        text = data.get('text')
        if name == 'browser_type' and (not isinstance(text, str) or len(text) > 12000):
            raise BrowserGuard('Browser text must contain at most 12000 characters.')
        expected = snapshot['signatures'][selector]
        self._check(context)
        # Validation and DOM action share one script: no host round-trip race.
        payload = json.dumps(dict(selector=selector, signature=expected, operation=name, text=text,
                                  url=snapshot['url']), ensure_ascii=True)
        check = json.dumps(dict(selector=selector, signature=expected, operation='validate', text=None,
                                url=snapshot['url']), ensure_ascii=True)
        preflight = self.view.evaluate(ACTION_SCRIPT.replace('__FORGE_DATA__', check))
        if preflight.get('not_executed'):
            return preflight
        self._snapshot(data, context, focus=False)
        focused_effect = not self.view.focused()
        if focused_effect:
            self.view.show()
        if not self.view.focused():
            raise RuntimeError('Owned browser focus changed after focusing. Inspect before retrying.')
        try:
            self._check(context)
        except BrowserGuard as exc:
            if focused_effect:
                raise RuntimeError('Browser focused, then action cancelled before dispatch.') from exc
            raise
        result = self.view.evaluate(ACTION_SCRIPT.replace('__FORGE_DATA__', payload))
        if result.get('not_executed'):
            if focused_effect:
                result = dict(result)
                result.pop('not_executed', None)
                result['performed'] = 'focused_browser'
            return result
        # Exceptions/timeout after ExecuteScriptAsync may follow a dispatched click;
        # do not turn them into a safe rejection or repeat them automatically.
        with self.lock:
            self.snapshot = None
        return {'ok': True, 'inspect_again': True, 'backend': 'native'}

    def _inspect(self, context):
        self._check(context)
        with self.lock:
            if self.state['loading']:
                raise BrowserGuard('Browser page is still loading. Inspect again when navigation completes.')
            generation = self.generation
        key = uuid4().hex
        result = self.view.evaluate(INSPECT_SCRIPT.replace('__FORGE_KEY__', json.dumps(key)))
        if not isinstance(result, dict) or not result.get('url'):
            raise BrowserGuard('Browser page could not be inspected.')
        self._check(context)
        with self.lock:
            if generation != self.generation or self.state['loading'] or result['url'] != self.state['url']:
                raise BrowserGuard('Browser navigated while inspecting. Inspect again.')
            signatures = {target['selector']: target.pop('signature') for target in result['targets']}
            self.snapshot = dict(id=key, run=self._run(context), time=self.clock(), generation=generation,
                                 url=result['url'], signatures=signatures)
        return dict(ok=True, snapshot_id=key, backend='native', **result)

    def stop(self):
        self.cancelled.set()
        with self.lock:
            self.snapshot = None
        if self.view:
            self.view.stop()

    def reset(self):
        self.cancelled.clear()

    def shutdown(self):
        if self.closed:
            return
        self.cancelled.set()
        if self.view:
            self.view.close()
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)


INSPECT_SCRIPT = r'''(() => {
 const key=__FORGE_KEY__;
 const sig=el=>[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.getAttribute('aria-label'),el.textContent.slice(0,200),el.disabled===true,el.readOnly===true,el.type!=='password'&&['INPUT','TEXTAREA'].includes(el.tagName)?el.value:null];
 const targets=[...document.querySelectorAll('a,button,input,textarea,select,[role=button]')]
 .filter(el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(el).visibility!=='hidden';}).slice(0,200)
 .map((el,i)=>{const id=key+'-'+i;el.setAttribute('data-forge-native',id);return {
 selector:'[data-forge-native="'+id+'"]',tag:el.tagName.toLowerCase(),type:el.getAttribute('type'),
 label:(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||'').slice(0,300),
 disabled:el.disabled===true,password:el.type==='password',signature:sig(el)};});
 return {url:location.href,title:document.title.slice(0,500),text:(document.body?.innerText||'').slice(0,24000),targets};
})()'''

ACTION_SCRIPT = r'''(() => {
 const d=__FORGE_DATA__;
 const fail=error=>({ok:false,error,not_executed:true});
 if(location.href!==d.url)return fail('Browser navigated. Inspect again.');
 const matches=document.querySelectorAll(d.selector);if(matches.length!==1)return fail('Browser target changed. Inspect again.');
 const el=matches[0],r=el.getBoundingClientRect();
 const signature=[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.getAttribute('aria-label'),el.textContent.slice(0,200),el.disabled===true,el.readOnly===true,el.type!=='password'&&['INPUT','TEXTAREA'].includes(el.tagName)?el.value:null];
 if(JSON.stringify(signature)!==JSON.stringify(d.signature)||!el.isConnected||!r.width||!r.height||getComputedStyle(el).visibility==='hidden')return fail('Browser target changed. Inspect again.');
 if(el.type==='password'||el.type==='file')return fail('Password and file fields require direct user interaction.');
 if(el.disabled||el.readOnly)return fail('Target is disabled or read-only.');
 if(d.operation==='validate')return {ok:true};
 if(d.operation==='browser_type'&&!['INPUT','TEXTAREA'].includes(el.tagName))return fail('Select an editable text field.');
 if(d.operation==='browser_type'&&!['text','search','email','url','tel','number',''].includes(el.type||''))return fail('Unsupported input type.');
 if(d.operation==='browser_click'){el.click();return {ok:true};}
 const prototype=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
 const setter=Object.getOwnPropertyDescriptor(prototype,'value')?.set;if(!setter)return fail('This field cannot be edited.');
 el.focus();setter.call(el,d.text);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return {ok:true};
})()'''


class WebView2Page:
    """Raw control: no pywebview JS API, host objects, downloads or extension profile."""
    def __init__(self, owner, home, ui_dispatch, changed):
        from webview.platforms.edgechromium import WebView2, CoreWebView2CreationProperties
        from System.Drawing import Color, Size, Font
        from System.Windows.Forms import Form, Panel, Button, TextBox, DockStyle, Keys, FlatStyle, Screen
        self.ui = ui_dispatch
        self.changed = changed
        self.event = threading.Event()
        self.error = None
        scale = float(getattr(owner, 'DeviceDpi', 96)) / 96
        px = lambda value: round(value * scale)
        area = Screen.FromControl(owner).WorkingArea
        self.form = Form()
        self.form.Text = 'Forge Browser'
        self.form.Size = Size(min(px(1100), area.Width), min(px(780), area.Height))
        self.form.MinimumSize = Size(min(px(620), area.Width), min(px(440), area.Height))
        self.form.BackColor = Color.FromArgb(25, 25, 27)
        self.form.Font = Font('Segoe UI', 10)
        self.form.Icon = owner.Icon
        self.form.Owner = owner
        self.control = WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = str(Path(home) / 'state/native-browser-profile')
        self.control.CreationProperties = props
        self.control.Dock = DockStyle.Fill
        self.form.Controls.Add(self.control)
        toolbar = Panel()
        toolbar.Dock = DockStyle.Top
        toolbar.Height = px(46)
        self.address = TextBox()
        self.address.Left, self.address.Top, self.address.Height = px(200), px(12), px(24)
        self.address.Width = px(730)
        self.address.BackColor = Color.FromArgb(38, 38, 40)
        self.address.ForeColor = Color.WhiteSmoke
        self.address.AccessibleName = 'Browser address'
        toolbar.Controls.Add(self.address)
        for label, left, operation in [('‹', 10, 'back'), ('›', 54, 'forward'), ('↻', 98, 'reload'), ('Go', 148, 'go')]:
            button = Button()
            button.Text, button.Left, button.Top, button.Width, button.Height = label, px(left), px(8), px(42), px(30)
            button.FlatStyle = FlatStyle.Flat
            button.FlatAppearance.BorderSize = 0
            button.BackColor = Color.FromArgb(237, 129, 59) if operation == 'go' else Color.FromArgb(38, 38, 40)
            button.ForeColor = Color.Black if operation == 'go' else Color.WhiteSmoke
            button.AccessibleName = {'back':'Back','forward':'Forward','reload':'Reload','go':'Navigate'}[operation]
            button.Click += lambda sender, args, name=operation: self._toolbar(name)
            toolbar.Controls.Add(button)
        self.address.KeyDown += lambda sender, args: self._address_key(args, Keys.Enter)
        self.form.Resize += lambda sender, args: setattr(self.address, 'Width', max(px(220), self.form.ClientSize.Width - px(210)))
        self.form.Controls.Add(toolbar)
        toolbar.BringToFront()
        self.control.CoreWebView2InitializationCompleted += self._initialized
        self.form.FormClosed += lambda sender, args: self.changed('closed')
        self.form.Show()
        self.control.EnsureCoreWebView2Async(None)

    def _address_key(self, args, enter):
        if args.KeyCode == enter:
            args.SuppressKeyPress = True
            self._toolbar('go')

    def _toolbar(self, action):
        try:
            if action == 'go':
                value = self.address.Text.strip()
                if '://' not in value:
                    value = 'https://' + value
                self.navigate(browser_url(value))
            else:
                self.history(action)
        except Exception as exc:
            self.changed('loaded', str(exc)[:400])

    def _initialized(self, sender, args):
        try:
            if not args.IsSuccess:
                raise RuntimeError('Windows WebView2 could not initialize.')
            core = self.control.CoreWebView2
            core.Settings.AreHostObjectsAllowed = False
            core.Settings.IsWebMessageEnabled = False
            core.Settings.AreDevToolsEnabled = False
            core.NavigationStarting += self._navigation
            core.NavigationCompleted += self._loaded
            core.SourceChanged += self._source_changed
            core.DocumentTitleChanged += lambda sender, args: self.changed('title', core.DocumentTitle)
            core.NewWindowRequested += self._popup
            core.DownloadStarting += lambda sender, args: setattr(args, 'Cancel', True)
            core.PermissionRequested += self._permission
            core.Navigate('about:blank')
        except Exception as exc:
            self.error = exc
        finally:
            self.event.set()

    def _navigation(self, sender, args):
        try:
            url = browser_url(str(args.Uri))
            self.address.Text = url
            self.changed('navigation', url)
        except ValueError:
            args.Cancel = True
            self.changed('loaded', 'Only HTTP and HTTPS pages can open in Forge Browser.')

    def _loaded(self, sender, args):
        self.changed('loaded', None if args.IsSuccess else 'Navigation failed: ' + str(args.WebErrorStatus))

    def _source_changed(self, sender, args):
        # SPA/history/fragment changes need the same invalidation as full loads.
        source = str(self.control.CoreWebView2.Source)
        self.address.Text = source
        self.changed('navigation', source)
        if not args.IsNewDocument:
            self.changed('loaded')

    def _popup(self, sender, args):
        args.Handled = True
        try:
            self.control.CoreWebView2.Navigate(browser_url(str(args.Uri)))
        except ValueError:
            pass

    @staticmethod
    def _permission(sender, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState
        args.State = CoreWebView2PermissionState.Deny

    def ready(self):
        if not self.event.wait(20):
            raise TimeoutError('Windows WebView2 initialization timed out.')
        if self.error:
            raise self.error

    def show(self):
        self.ui(lambda _: (self.form.Show(), self.form.Activate()))

    def focused(self):
        return self.ui(lambda _: bool(self.form.ContainsFocus and self.form.Visible))

    def navigate(self, url):
        self.ui(lambda _: self.control.CoreWebView2.Navigate(url))

    def history(self, action):
        def change(_):
            core = self.control.CoreWebView2
            if action == 'back' and core.CanGoBack:
                core.GoBack()
            elif action == 'forward' and core.CanGoForward:
                core.GoForward()
            elif action == 'reload':
                core.Reload()
        self.ui(change)

    def evaluate(self, script):
        from System import Action, String
        from System.Threading.Tasks import Task
        done, result = threading.Event(), {}
        def complete(task):
            try:
                result['value'] = json.loads(str(task.Result))
            except Exception as exc:
                result['error'] = exc
            finally:
                done.set()
        self.ui(lambda _: self.control.CoreWebView2.ExecuteScriptAsync(script).ContinueWith(Action[Task[String]](complete)))
        if not done.wait(12):
            raise TimeoutError('Browser script timed out; inspect the action outcome before retrying.')
        if 'error' in result:
            raise result['error']
        if result.get('value') is None:
            raise RuntimeError('Browser script did not return a result; inspect the page before retrying.')
        return result['value']

    def screenshot(self):
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        from System import Action
        from System.IO import MemoryStream
        from System.Threading.Tasks import Task
        done, result = threading.Event(), {}
        stream = MemoryStream()
        def complete(task):
            try:
                task.GetAwaiter().GetResult()
                result['value'] = bytes(stream.ToArray())
            except Exception as exc:
                result['error'] = exc
            finally:
                stream.Dispose()
                done.set()
        self.ui(lambda _: self.control.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream).ContinueWith(Action[Task](complete)))
        if not done.wait(12):
            raise TimeoutError('Browser screenshot timed out.')
        if 'error' in result:
            raise result['error']
        return result['value']

    def stop(self):
        self.ui(lambda _: self.control.CoreWebView2.Stop())

    def close(self):
        self.ui(lambda _: self.form.Close())
