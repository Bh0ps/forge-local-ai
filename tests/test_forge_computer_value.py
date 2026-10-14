"""Explicit UIA value admission, with no real windows, input, clipboard or pixels."""
import base64
import io
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from PIL import Image
import pytest

from computer_tools import ComputerBroker
from tool_routing import validate_arguments


MARKER = 'SYNTHETIC_PRIVATE_PROVIDER_DETAIL'


class ValuePattern:
    def __init__(self):
        self.readonly = False
        self.before_read = self.on_set = None
        self.values = []
        self.threads = []

    @property
    def CurrentIsReadOnly(self):
        if self.before_read:
            self.before_read()
        return self.readonly

    def SetValue(self, value):
        self.values.append(value)
        self.threads.append(threading.current_thread().name)
        if self.on_set:
            self.on_set()


class Control:
    def __init__(self, handle=101, runtime=(7, 101, 1), kind='Edit'):
        self.handle = handle
        self.element_info = SimpleNamespace(runtime_id=list(runtime), automation_id='fixture-edit',
            control_type=kind, is_password=False, element=SimpleNamespace(CurrentIsPassword=False))
        self.box = SimpleNamespace(left=30, top=40, right=280, bottom=90)
        self.name = 'Synthetic accessible control'
        self.pid = 55
        self.visible = self.enabled = True
        self.pattern = ValuePattern()
        self.pattern_reads = 0
        self.pattern_hook = None
        self.focus_calls = self.pointer_calls = 0

    @property
    def iface_value(self):
        self.pattern_reads += 1
        if self.pattern_hook:
            self.pattern_hook()
        return self.pattern

    def rectangle(self): return self.box
    def window_text(self): return self.name
    def process_id(self): return self.pid
    def is_visible(self): return self.visible
    def is_enabled(self): return self.enabled
    def set_focus(self):
        self.focus_calls += 1
        pytest.fail('The value action requested focus')
    def click_input(self):
        self.pointer_calls += 1
        pytest.fail('The value action requested pointer input')


class Window(Control):
    def __init__(self):
        super().__init__(runtime=(7, 101), kind='Window')
        self.box = SimpleNamespace(left=10, top=20, right=310, bottom=220)
        self.child = Control()
        self.captures = 0
        self.traverse_hook = None

    def wrapper_object(self): return self
    def descendants(self):
        if self.traverse_hook:
            self.traverse_hook()
        return [self.child]
    def capture_as_image(self):
        self.captures += 1
        return Image.new('RGB', (300, 200))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    window = Window()
    now = [0.0]
    foreground = [999]  # A different app owns foreground throughout the value call.
    session = [True]
    cancel = threading.Event()
    context = {'run_id': 'synthetic-owned-run', 'cancel': cancel}
    desktop = SimpleNamespace(window=lambda **kwargs: window, windows=lambda **kwargs: [window])
    def capture(*args, **kwargs):
        from forge_window_capture import WindowCapture
        picture = window.capture_as_image()  # Owned synthetic fixture, never an OS capture.
        buffer = io.BytesIO(); picture.save(buffer, 'PNG')
        return WindowCapture(True, 'captured', buffer.getvalue(), picture.size, picture.size)
    broker = ComputerBroker(tmp_path, desktop_factory=lambda: desktop, clock=lambda: now[0],
        session_check=lambda: session[0], foreground=lambda: foreground[0], capture=capture)
    # Any accidental fallback is an offline failure, never real input.
    import pywinauto.keyboard
    import pywinauto.mouse
    def forbid(*args, **kwargs): pytest.fail('ValuePattern used a keyboard/mouse fallback')
    monkeypatch.setattr(pywinauto.keyboard, 'send_keys', forbid)
    monkeypatch.setattr(pywinauto.mouse, 'click', forbid)
    try:
        yield SimpleNamespace(window=window, control=window.child, broker=broker, now=now,
            foreground=foreground, session=session, cancel=cancel, context=context)
    finally:
        broker.close()


def inspect(fixture, image=False):
    result = fixture.broker.execute('computer_inspect', {'window_id': 101, 'include_image': image}, fixture.context)
    assert 'snapshot_id' in result
    return result, {'snapshot_id': result['snapshot_id'], 'element_id': 1, 'value': 'literal {braces}+^%~ Unicode Ω'}


def refused(result, text):
    assert result['ok'] is False and result['not_executed'] is True
    assert text in result['error']


def test_background_value_uses_only_uia_and_retires_successful_snapshot(fixture):
    result, args = inspect(fixture)
    assert result['image_available'] is False
    assert not {'image', 'mime_type', 'image_size'}.intersection(result)
    assert fixture.window.captures == 0 and result['focused'] is False
    assert fixture.broker.execute('computer_set_value', args, fixture.context) == {'ok': True, 'inspect_again': True}
    assert fixture.control.pattern.values == [args['value']]
    assert fixture.control.pattern.threads[0].startswith('forge-computer')
    assert fixture.foreground == [999]
    assert fixture.control.focus_calls == fixture.control.pointer_calls == fixture.window.focus_calls == 0
    refused(fixture.broker.execute('computer_set_value', args, fixture.context), 'Unknown snapshot')
    assert fixture.control.pattern.values == [args['value']]


@pytest.mark.parametrize('value', ['', 'a' * 12000])
def test_empty_and_bounded_values_are_literal(fixture, value):
    _, args = inspect(fixture)
    assert fixture.broker.execute('computer_set_value', {**args, 'value': value}, fixture.context)['ok']
    assert fixture.control.pattern.values == [value]


@pytest.mark.parametrize('value', [None, 123, True, ['text'], 'a' * 12001])
def test_invalid_value_refuses_before_provider_input(fixture, value):
    _, args = inspect(fixture)
    refused(fixture.broker.execute('computer_set_value', {**args, 'value': value}, fixture.context), 'Value is limited')
    assert not fixture.control.pattern.values and fixture.control.pattern_reads == 0


@pytest.mark.parametrize('change,reason', [
    ('readonly', 'read-only'), ('unsupported', 'ValuePattern'), ('readonly_unknown', 'Cannot confirm'),
    ('readonly_error', 'ValuePattern'), ('missing_setter', 'Cannot confirm'), ('pattern_error', 'ValuePattern'),
    ('password', 'Password'), ('password_read_error', 'Password'), ('disabled', 'changed'), ('hidden', 'changed'),
    ('fingerprint', 'changed'), ('control_runtime', 'changed'), ('window_runtime', 'Target window changed'),
    ('pid', 'Target window changed'), ('window_hidden', 'Target window changed'), ('bounds', 'Window moved'),
    ('outside', 'bounds are outside'), ('other_process', 'different app'), ('missing_runtime', 'identity is unavailable'),
    ('missing_window_runtime', 'identity is unavailable'), ('expiry', 'expired'), ('wrong_run', 'Unknown snapshot'),
    ('cancel', 'cancelled'), ('session', 'unavailable'),
])
def test_value_requires_current_writable_identified_target(fixture, change, reason):
    if change == 'outside': fixture.control.box.right = 400
    if change == 'other_process': fixture.control.pid = 66
    if change == 'missing_runtime': fixture.control.element_info.runtime_id = None
    if change == 'missing_window_runtime': fixture.window.element_info.runtime_id = None
    _, args = inspect(fixture)
    context = fixture.context
    pattern = fixture.control.pattern
    if change == 'readonly': pattern.readonly = True
    elif change == 'unsupported': fixture.control.pattern = None
    elif change == 'readonly_unknown': pattern.readonly = None
    elif change == 'readonly_error': pattern.before_read = lambda: (_ for _ in ()).throw(RuntimeError(MARKER))
    elif change == 'missing_setter': pattern.SetValue = None
    elif change == 'pattern_error': fixture.control.pattern_hook = lambda: (_ for _ in ()).throw(RuntimeError(MARKER))
    elif change == 'password': fixture.control.element_info.is_password = True
    elif change == 'password_read_error':
        class UnknownPassword:
            @property
            def CurrentIsPassword(self): raise RuntimeError(MARKER)
        fixture.control.element_info.element = UnknownPassword()
    elif change == 'disabled': fixture.control.enabled = False
    elif change == 'hidden': fixture.control.visible = False
    elif change == 'fingerprint': fixture.control.name = 'Replacement control'
    elif change == 'control_runtime': fixture.control.element_info.runtime_id = [7, 101, 9]
    elif change == 'window_runtime': fixture.window.element_info.runtime_id = [7, 999]
    elif change == 'pid': fixture.window.pid = 77
    elif change == 'window_hidden': fixture.window.visible = False
    elif change == 'bounds': fixture.window.box.left += 10
    elif change == 'expiry': fixture.now[0] = 31
    elif change == 'wrong_run': context = {'run_id': 'unrelated'}
    elif change == 'cancel': fixture.cancel.set()
    elif change == 'session': fixture.session[0] = False
    result = fixture.broker.execute('computer_set_value', args, context)
    refused(result, reason)
    assert MARKER not in str(result)
    assert not pattern.values and fixture.control.focus_calls == fixture.control.pointer_calls == 0


@pytest.mark.parametrize('change', ['cancel', 'session', 'window', 'element', 'bounds', 'password'])
def test_slow_pattern_discovery_cannot_bless_changed_target(fixture, change):
    _, args = inspect(fixture)
    def during_lookup():
        if change == 'cancel': fixture.cancel.set()
        elif change == 'session': fixture.session[0] = False
        elif change == 'window': fixture.window.name = 'Different window'
        elif change == 'element': fixture.control.element_info.runtime_id = [7, 101, 999]
        elif change == 'bounds': fixture.window.box.left += 20
        else: fixture.control.element_info.is_password = True
    fixture.control.pattern.before_read = during_lookup
    result = fixture.broker.execute('computer_set_value', args, fixture.context)
    assert result['not_executed'] is True
    assert not fixture.control.pattern.values


def test_queued_value_request_rechecks_cancellation_at_actual_admission(fixture):
    _, args = inspect(fixture)
    entered, release = threading.Event(), threading.Event()
    def prior_work():
        entered.set()
        assert release.wait(3)
    prior = fixture.broker._executor.submit(prior_work)
    assert entered.wait(3)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(fixture.broker.execute, 'computer_set_value', args, fixture.context)
        fixture.cancel.set()
        release.set()
        refused(result.result(timeout=3), 'cancelled')
    prior.result(timeout=3)
    assert not fixture.control.pattern.values and fixture.control.pattern_reads == 0


def test_stop_during_pattern_discovery_prevents_value_delivery(fixture):
    _, args = inspect(fixture)
    entered, release = threading.Event(), threading.Event()
    def slow_read():
        entered.set()
        assert release.wait(3)
    fixture.control.pattern.before_read = slow_read
    with ThreadPoolExecutor(max_workers=2) as caller:
        action = caller.submit(fixture.broker.execute, 'computer_set_value', args, fixture.context)
        assert entered.wait(3)
        stop = caller.submit(fixture.broker.stop)
        assert fixture.broker._cancelled.wait(3)
        release.set()
        refused(action.result(timeout=3), 'cancelled')
        stop.result(timeout=3)
    assert not fixture.control.pattern.values and not fixture.broker._snapshots


def test_last_process_read_cannot_replace_the_inspected_control(fixture):
    _, args = inspect(fixture)
    def changed_process_read():
        fixture.control.element_info.runtime_id = [7, 101, 999]
        return 55
    fixture.control.process_id = changed_process_read
    refused(fixture.broker.execute('computer_set_value', args, fixture.context), 'changed')
    assert not fixture.control.pattern.values


@pytest.mark.parametrize('read', ['rectangle', 'process_id'])
def test_unreadable_native_metadata_is_a_sanitized_pre_effect_refusal(fixture, read):
    _, args = inspect(fixture)
    def failed_read(): raise RuntimeError(MARKER)
    setattr(fixture.control, read, failed_read)
    result = fixture.broker.execute('computer_set_value', args, fixture.context)
    refused(result, 'could not be safely validated')
    assert MARKER not in str(result) and not fixture.control.pattern.values


@pytest.mark.parametrize('failure', ['provider_error', 'cancel_after_delivery', 'session_after_delivery'])
def test_ambiguous_effect_is_sanitized_and_never_replayed(fixture, failure):
    _, args = inspect(fixture)
    def after_delivery():
        if failure == 'provider_error': raise TimeoutError(MARKER)
        if failure == 'cancel_after_delivery': fixture.cancel.set()
        if failure == 'session_after_delivery': fixture.session[0] = False
    fixture.control.pattern.on_set = after_delivery
    with pytest.raises(ValueError) as error:
        fixture.broker.execute('computer_set_value', args, fixture.context)
    assert error.value.not_executed is False
    assert MARKER not in ''.join(traceback.format_exception(error.value))
    assert fixture.control.pattern.values == [args['value']]
    fixture.cancel.clear(); fixture.session[0] = True
    refused(fixture.broker.execute('computer_set_value', args, fixture.context), 'Unknown snapshot')
    assert fixture.control.pattern.values == [args['value']]


@pytest.mark.parametrize('value', [None, 0, 1, 'false', [], {}])
def test_inspection_requires_an_exact_image_boolean_before_any_capture(fixture, value):
    result = fixture.broker.execute('computer_inspect', {'window_id': 101, 'include_image': value}, fixture.context)
    refused(result, 'include_image')
    assert fixture.window.captures == 0 and not fixture.broker._snapshots


def test_default_inspection_uses_acknowledged_window_render_pixels(fixture):
    result = fixture.broker.execute('computer_inspect', {'window_id': 101}, fixture.context)
    assert fixture.window.captures == 1 and result['mime_type'] == 'image/png'
    assert result['image_size'] == [300, 200]
    with Image.open(io.BytesIO(base64.b64decode(result['image']))) as image:
        assert image.format == 'PNG' and image.size == (300, 200)
    assert result['image_available'] is True and result['image_source'] == 'window_render'


@pytest.mark.parametrize('reason', ['unsupported', 'timeout', 'blank'])
def test_unavailable_window_render_keeps_accessibility_and_refuses_coordinates(fixture, reason):
    from forge_window_capture import WindowCapture
    fixture.broker.capture = lambda *args, **kwargs: WindowCapture(False, reason)
    fixture.foreground[0] = 101
    result = fixture.broker.execute('computer_inspect', {'window_id': 101}, fixture.context)
    assert result['elements'] and result['image_available'] is False
    assert result['capture_reason'] == reason and fixture.window.captures == 0
    assert not {'image', 'mime_type', 'image_size'}.intersection(result)
    refused(fixture.broker.execute('computer_coordinate_click',
        {'snapshot_id': result['snapshot_id'], 'x': 20, 'y': 20}, fixture.context), 'image')


def test_permission_revoked_during_pattern_lookup_prevents_the_effect(fixture):
    _, args = inspect(fixture)
    admission = [True]
    context = {**fixture.context, 'authority_check': lambda: admission[0]}
    fixture.control.pattern.before_read = lambda: admission.__setitem__(0, False)
    refused(fixture.broker.execute('computer_set_value', args, context), 'permission changed')
    assert not fixture.control.pattern.values


def test_permission_revoked_after_value_effect_stays_unknown_and_cannot_replay(fixture):
    _, args = inspect(fixture)
    admission = [True]
    context = {**fixture.context, 'authority_check': lambda: admission[0]}
    fixture.control.pattern.on_set = lambda: admission.__setitem__(0, False)
    with pytest.raises(ValueError, match='permission changed'):
        fixture.broker.execute('computer_set_value', args, context)
    assert fixture.control.pattern.values == [args['value']]
    admission[0] = True
    refused(fixture.broker.execute('computer_set_value', args, context), 'Unknown snapshot')
    assert fixture.control.pattern.values == [args['value']]


@pytest.mark.parametrize('answer', [False, 1, 'allow', None])
def test_native_authority_requires_literal_true_before_any_input(fixture, answer):
    _, args = inspect(fixture)
    refused(fixture.broker.execute('computer_set_value', args,
        {**fixture.context, 'authority_check': lambda: answer}), 'permission changed')
    assert not fixture.control.pattern.values


def test_revoked_capture_never_returns_pixels_or_a_snapshot(fixture):
    admission = [True]
    original = fixture.broker.capture
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        admission[0] = False
        return result
    fixture.broker.capture = capture
    result = fixture.broker.execute('computer_inspect', {'window_id':101},
        {**fixture.context, 'authority_check': lambda: admission[0]})
    refused(result, 'permission changed')
    assert 'image' not in result and not fixture.broker._snapshots


def test_window_list_rechecks_authority_before_returning_app_metadata(fixture):
    admission = [True]
    original = fixture.window.window_text
    def read_name():
        admission[0] = False
        return original()
    fixture.window.window_text = read_name
    result = fixture.broker.execute('computer_windows', {},
        {**fixture.context, 'authority_check': lambda: admission[0]})
    refused(result, 'permission changed')
    assert 'windows' not in result


@pytest.mark.parametrize('change', ['run_cancel', 'global_stop', 'locked'])
def test_delayed_authority_cannot_bless_cancelled_or_locked_value_dispatch(fixture, change):
    _, args = inspect(fixture)
    armed = [False]
    fixture.control.pattern.before_read = lambda: armed.__setitem__(0, True)
    def authority():
        if armed[0]:
            if change == 'run_cancel': fixture.cancel.set()
            elif change == 'global_stop': fixture.broker._cancelled.set()
            else: fixture.session[0] = False
        return True
    result = fixture.broker.execute('computer_set_value', args,
        {**fixture.context, 'authority_check': authority})
    refused(result, 'unavailable' if change == 'locked' else 'cancelled')
    assert not fixture.control.pattern.values


@pytest.mark.parametrize('operation', ['windows', 'clipboard_read', 'clipboard_write', 'click'])
def test_unexpected_native_errors_are_sanitized_with_honest_effect_classification(fixture, monkeypatch, operation):
    import sys
    _, args = inspect(fixture)
    fixture.foreground[0] = 101
    def failure(*args, **kwargs): raise RuntimeError(MARKER)
    if operation == 'windows':
        fixture.broker.desktop_factory = failure
        name, supplied = 'computer_windows', {}
    elif operation.startswith('clipboard'):
        monkeypatch.setitem(sys.modules, 'pyperclip', SimpleNamespace(paste=failure, copy=failure))
        name, supplied = 'computer_' + operation, {'text':'synthetic'}
    else:
        fixture.control.click_input = failure
        name, supplied = 'computer_click', args
    if operation in ('windows', 'clipboard_read'):
        result = fixture.broker.execute(name, supplied, fixture.context)
        refused(result, 'could not start')
        assert MARKER not in str(result)
    else:
        with pytest.raises(ValueError, match='outcome is unknown') as error:
            fixture.broker.execute(name, supplied, fixture.context)
        assert MARKER not in str(error.value) and error.value.not_executed is False
        assert error.value.__suppress_context__


def test_unexpected_target_lookup_error_never_exports_native_details(fixture):
    def failure(): raise RuntimeError(MARKER)
    fixture.window.window_text = failure
    result = fixture.broker.describe_target({'window_id':101}, fixture.context)
    refused(result, 'could not be identified')
    assert MARKER not in str(result)


def test_queued_value_keeps_the_exact_original_literal(fixture, monkeypatch):
    _, args = inspect(fixture)
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    def prior_work():
        entered.set()
        assert release.wait(3)
    prior = fixture.broker._executor.submit(prior_work)
    assert entered.wait(3)
    submit = fixture.broker._executor.submit
    def observed_submit(function, *values, **options):
        future = submit(function, *values, **options)
        queued.set()
        return future
    monkeypatch.setattr(fixture.broker._executor, 'submit', observed_submit)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(fixture.broker.execute, 'computer_set_value', args, fixture.context)
        assert queued.wait(3)
        original = args['value']
        args['value'] = 'Different queued literal'
        release.set()
        assert future.result(timeout=3)['ok'] is True
    prior.result(timeout=3)
    assert fixture.control.pattern.values == [original]


@pytest.mark.parametrize('operation', ['key', 'coordinate_click', 'click', 'type'])
def test_slow_final_authority_cannot_dispatch_input_to_new_foreground(fixture, monkeypatch, operation):
    result, args = inspect(fixture, image=True)
    fixture.foreground[0] = 101
    calls, delivered = [0], []
    def authority():
        calls[0] += 1
        if calls[0] == 3: fixture.foreground[0] = 999
        return True
    import pywinauto.keyboard
    import pywinauto.mouse
    monkeypatch.setattr(pywinauto.keyboard, 'send_keys', lambda *a, **k: delivered.append(fixture.foreground[0]))
    monkeypatch.setattr(pywinauto.mouse, 'click', lambda *a, **k: delivered.append(fixture.foreground[0]))
    supplied = {'snapshot_id':result['snapshot_id'], 'element_id':args['element_id'],
                'key':'enter', 'x':20, 'y':20, 'text':'Synthetic literal'}
    refused(fixture.broker.execute('computer_' + operation, supplied,
        {**fixture.context, 'authority_check':authority}), 'focused')
    assert calls[0] == 3 and not delivered
    assert fixture.control.focus_calls == fixture.control.pointer_calls == 0


def test_coordinate_input_refuses_an_accessibility_only_snapshot_before_mouse_effect(fixture):
    _, args = inspect(fixture)
    fixture.foreground[0] = 101
    refused(fixture.broker.execute('computer_coordinate_click', {'snapshot_id': args['snapshot_id'], 'x': 2, 'y': 2}, fixture.context), 'requires an image')
    assert fixture.window.captures == 0


def test_coordinate_input_still_uses_default_image_geometry(fixture, monkeypatch):
    import pywinauto.mouse
    calls = []
    monkeypatch.setattr(pywinauto.mouse, 'click', lambda **kwargs: calls.append(kwargs))
    result = fixture.broker.execute('computer_inspect', {'window_id': 101}, fixture.context)
    fixture.foreground[0] = 101
    args = {'snapshot_id': result['snapshot_id'], 'x': 25, 'y': 35}
    assert fixture.broker.execute('computer_coordinate_click', args, fixture.context)['ok']
    assert calls == [{'coords': (35, 55)}] and fixture.window.captures == 1


@pytest.mark.parametrize('change', ['title', 'bounds', 'hidden', 'cancel'])
def test_accessibility_only_inspection_revalidates_target_after_traversal(fixture, change):
    def during_traversal():
        if change == 'title': fixture.window.name = 'Replacement'
        elif change == 'bounds': fixture.window.box.left += 3
        elif change == 'hidden': fixture.window.visible = False
        else: fixture.cancel.set()
    fixture.window.traverse_hook = during_traversal
    result = fixture.broker.execute('computer_inspect', {'window_id': 101, 'include_image': False}, fixture.context)
    assert result['not_executed'] is True
    assert 'snapshot_id' not in result and not fixture.broker._snapshots and fixture.window.captures == 0


@pytest.mark.parametrize('extra', [{'desktop': 'secure'}, {'session_id': 2}, {'scope': 'all'}, {'window_id': 999}])
def test_model_cannot_choose_an_alternate_os_scope(fixture, extra):
    schema = next(tool for tool in fixture.broker.schemas() if tool['function']['name'] == 'computer_set_value')
    assert validate_arguments(schema, {'snapshot_id': 'opaque', 'element_id': 1, 'value': 'text', **extra})
    assert validate_arguments(schema, {'snapshot_id': 'opaque', 'element_id': 1, 'value': 'text'}) is None


def test_schema_advertises_the_limited_value_action_and_optional_pixels(fixture):
    schemas = {tool['function']['name']: tool['function'] for tool in fixture.broker.schemas()}
    value = schemas['computer_set_value']
    assert 'ValuePattern' in value['description'] and 'no desktop isolation' in value['description']
    assert value['parameters']['additionalProperties'] is False
    assert value['parameters']['properties']['value']['maxLength'] == 12000
    inspect_schema = schemas['computer_inspect']['parameters']
    assert inspect_schema['required'] == ['window_id']
    assert inspect_schema['properties']['include_image'] == {'type': 'boolean', 'default': True}
