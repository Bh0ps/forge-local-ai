"""Native visibility events use fake forms and never inspect personal windows."""
import json
import sys
import threading
import time
from types import SimpleNamespace

from desktop import Bridge


class Event:
    def __init__(self): self.handlers = []
    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self
    def fire(self, *args):
        for handler in self.handlers: handler(*args)


class Form:
    def __init__(self, visible=True):
        self.Visible = visible
        self._state = 'Normal'
        self.VisibleChanged, self.Resize = Event(), Event()
        self.webview = SimpleNamespace(NavigationStarting=Event())
    @property
    def WindowState(self): return self._state
    @WindowState.setter
    def WindowState(self, value):
        self._state = value
        self.Resize.fire(self, None)
    def Activate(self): pass


class Window:
    def __init__(self, form):
        self.form = form
        self.messages = []
        self.threads = []
        self.changed = threading.Event()
        self.release = None
    def hide(self):
        self.form.Visible = False
        self.form.VisibleChanged.fire(self.form, None)
    def show(self):
        self.form.Visible = True
        self.form.VisibleChanged.fire(self.form, None)
    def evaluate_js(self, script):
        self.threads.append(threading.get_ident())
        if self.release: self.release.wait(2)
        prefix = "window.dispatchEvent(new CustomEvent('forge:visibility',{detail:"
        self.messages.append(json.loads(script[len(prefix):-len('}));true')]))
        self.changed.set()


def bridge(visible=True):
    api = Bridge(SimpleNamespace())
    form = Form(visible)
    api._window = Window(form)
    api._native = lambda operation: operation(form)
    api._ensure_service = lambda: api._service
    api._server_url = 'http://127.0.0.1:12345'
    api._visible = visible
    api._tray = object()
    api.restrict_navigation()
    return api, form


def delivered(api, count):
    deadline = time.monotonic() + 2
    while len(api._window.messages) < count and time.monotonic() < deadline:
        api._window.changed.wait(.05)
        api._window.changed.clear()
    assert len(api._window.messages) >= count
    return sorted(api._window.messages, key=lambda item: item['seq'])


def test_close_hides_and_tray_show_publish_ordered_state_from_workers(monkeypatch):
    monkeypatch.setitem(sys.modules, 'System.Windows.Forms', SimpleNamespace(
        FormWindowState=SimpleNamespace(Minimized='Minimized', Maximized='Maximized', Normal='Normal')))
    api, form = bridge()
    caller = threading.get_ident()
    assert api._on_closing() is False
    assert api.call('host_status')['visible'] is False
    assert api.call('host_status')['visibility_seq'] == 1
    api.show()
    assert api.call('host_status')['visible'] is True
    assert api.call('host_status')['visibility_seq'] == 2
    assert delivered(api, 2) == [{'visible': False, 'seq': 1}, {'visible': True, 'seq': 2}]
    assert caller not in api._window.threads


def test_native_minimize_restore_and_external_show_track_actual_form_state():
    api, form = bridge()
    form.WindowState = 'Minimized'
    assert api.call('host_status')['visible'] is False
    form.WindowState = 'Normal'
    assert api.call('host_status')['visible'] is True
    api._window.hide()
    # An external native Open action need not call Bridge.show().
    api._window.show()
    assert delivered(api, 4) == [{'visible': False, 'seq': 1}, {'visible': True, 'seq': 2},
                                {'visible': False, 'seq': 3}, {'visible': True, 'seq': 4}]


def test_background_loaded_snapshot_and_stale_minimize_callback_are_safe():
    api, form = bridge(visible=False)
    api._on_loaded()
    assert delivered(api, 1) == [{'visible': False, 'seq': 0}]
    api._window.show()
    # A delayed pywebview minimized callback reads the current native state;
    # it cannot overwrite an already restored form with a fresh false sequence.
    api._on_visibility_event()
    assert api.call('host_status')['visible'] is True
    assert api.call('host_status')['visibility_seq'] == 1
    assert delivered(api, 2)[-1] == {'visible': True, 'seq': 1}
    api.restrict_navigation()
    assert len(form.VisibleChanged.handlers) == len(form.Resize.handlers) == 1


def test_closing_callback_does_not_wait_for_javascript():
    api, form = bridge()
    api._window.release = threading.Event()
    started = time.monotonic()
    try:
        assert api._on_closing() is False
        assert time.monotonic() - started < .2
        assert api.call('host_status')['visible'] is False
    finally:
        api._window.release.set()
    assert delivered(api, 1) == [{'visible': False, 'seq': 1}]


def test_missing_tray_does_not_falsely_report_hidden():
    api, form = bridge()
    api._tray = None
    assert api._on_closing() is False
    assert form.Visible and api.call('host_status')['visible'] is True
    assert api.call('host_status')['visibility_seq'] == 0
    assert not api._window.messages
