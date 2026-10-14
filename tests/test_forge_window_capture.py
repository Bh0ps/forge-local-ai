"""Target-rendered capture protocol with only synthetic pixels and owned processes."""
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

import forge_window_capture as capture


BOUNDS = (20, 30, 60, 60)
TARGET = (4321, 98765, BOUNDS)


def request(bounds=BOUNDS):
    return {'format': 1, 'nonce': 'a' * 64, 'parent_pid': os.getppid(),
            'parent_created': 54321, 'hwnd': 1234, 'pid': TARGET[0], 'created': TARGET[1],
            'bounds': list(bounds), 'deadline_tick': 3000}


def png(size=(40, 30), mode='RGB', format='PNG'):
    output = io.BytesIO()
    Image.new(mode, size, (120, 90, 70) if mode == 'RGB' else 80).save(output, format)
    return output.getvalue()


def response(directory, value, pid=2468, mutate=None):
    raw = png(capture._dimensions(value['bounds']))
    data = {'format': 1, 'nonce': value['nonce'], 'pid': pid, 'status': 'captured',
            'reason': 'captured', 'original_size': list(capture._dimensions(value['bounds'])),
            'image_size': list(capture._dimensions(value['bounds'])), 'png_bytes': len(raw),
            'png_sha256': hashlib.sha256(raw).hexdigest()}
    if mutate: mutate(data)
    capture._write(directory / 'capture.png', raw)
    capture._write(directory / 'response.json', json.dumps(data).encode())


@pytest.fixture
def directory():
    with tempfile.TemporaryDirectory(prefix=capture.PREFIX) as name:
        yield capture._directory(name)


class FakeNative:
    def __init__(self):
        self.value = TARGET
        self.calls = 0

    def target(self, hwnd):
        assert hwnd == 1234
        self.calls += 1
        return self.value

    def created(self, pid): return 54321
    def ticks(self): return 1000
    def render(self, hwnd, bounds): return bytes((70, 90, 120, 0)) * (40 * 30)


class FakeProcess:
    pid = 2468
    def __init__(self, done=True):
        self.returncode = 0 if done else None
        self.terminations = 0
        self.exited = threading.Event()
        if done: self.exited.set()

    def poll(self): return self.returncode
    def terminate(self):
        self.terminations += 1
        self.returncode = -1
        self.exited.set()
    def wait(self):
        assert self.exited.wait(2), 'Owned synthetic process must be released'
        return self.returncode


@pytest.fixture
def transport(monkeypatch):
    native = FakeNative()
    processes, calls = [], []
    def launch(command, **kwargs):
        calls.append((command, kwargs))
        process = FakeProcess()
        path = Path(command[-1])
        value = capture._request(json.loads(capture._read(path / 'request.json', capture.MAX_PROTOCOL_BYTES)))
        response(path, value, process.pid)
        processes.append(process)
        return process
    monkeypatch.setattr(capture, '_Native', lambda: native)
    monkeypatch.setattr(capture.subprocess, 'Popen', launch)
    return SimpleNamespace(native=native, processes=processes, calls=calls)


def test_success_uses_owned_exact_protocol_and_no_environment_credentials(transport, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_SECRET', 'private-marker-never-forward')
    result = capture.capture_window(1234, TARGET[0], BOUNDS)
    assert result.available and result.reason == 'captured'
    assert result.original_size == result.image_size == (40, 30)
    with Image.open(io.BytesIO(result.png)) as image:
        assert image.getpixel((10, 10)) == (120, 90, 70)
    command, options = transport.calls[0]
    assert command[:3] == capture._launch_command()[0]
    assert command[-2] == '--directory' and not Path(command[-1]).exists()
    assert set(options['env']) <= {'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'windir', 'TEMP', 'TMP','__PYVENV_LAUNCHER__'}
    if '__PYVENV_LAUNCHER__' in options['env']: assert options['env']['__PYVENV_LAUNCHER__']==sys.executable
    assert 'private-marker' not in json.dumps(options)
    assert options['stdin'] == options['stdout'] == options['stderr'] == subprocess.DEVNULL
    assert transport.processes[0].terminations == 0 and transport.native.calls >= 3
    assert '120' not in repr(result) and 'PNG' not in repr(result)


def test_frozen_command_has_only_owned_capture_route(transport, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert capture.capture_window(1234, TARGET[0], BOUNDS).available
    assert transport.calls[0][0][:2] == [sys.executable, '--forge-window-capture']


@pytest.mark.parametrize('bounds', [(0, 0, 0, 2), (0, 0, 16385, 2), (0, 0, 5000, 5000),
    (False, 0, 20, 20), (0.0, 0, 20, 20), (0, 0, 20), (-2**31-1, 0, 20, 20)])
def test_unusable_geometry_never_launches_helper(transport, bounds):
    result = capture.capture_window(1234, TARGET[0], bounds)
    assert not result.available and result.png is None
    assert not transport.calls


@pytest.mark.parametrize('timeout', [False, 0, -1, 3.001, float('nan'), float('inf'), '3'])
def test_deadline_cannot_be_unbounded_or_coerced(transport, timeout):
    assert capture.capture_window(1234, TARGET[0], BOUNDS, timeout=timeout).reason == 'unsupported'
    assert not transport.calls


@pytest.mark.parametrize('target,reason', [((22, TARGET[1], BOUNDS), 'changed'),
    ((TARGET[0], TARGET[1], (20, 30, 61, 60)), 'geometry_mismatch')])
def test_wrong_owner_or_dpi_geometry_refuses_before_launch(transport, target, reason):
    transport.native.value = target
    assert capture.capture_window(1234, TARGET[0], BOUNDS).reason == reason
    assert not transport.calls


@pytest.mark.parametrize('change', ['pid', 'creation', 'bounds'])
def test_post_helper_window_identity_change_never_publishes_pixels(transport, monkeypatch, change):
    original = capture._response
    def changed(*args):
        result = original(*args)
        values = list(TARGET)
        values[{'pid': 0, 'creation': 1, 'bounds': 2}[change]] = (1, 2, 3, 4) if change == 'bounds' else 111
        transport.native.value = tuple(values)
        return result
    monkeypatch.setattr(capture, '_response', changed)
    result = capture.capture_window(1234, TARGET[0], BOUNDS)
    assert result.reason == 'changed' and result.png is None


def test_cancel_before_construction_and_after_constructor_has_no_pixel_result(transport, monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    assert capture.capture_window(1234, TARGET[0], BOUNDS, cancel=cancelled).reason == 'cancelled'
    assert not transport.calls
    cancelled.clear()
    original = capture.subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        cancelled.set()
        return process
    monkeypatch.setattr(capture.subprocess, 'Popen', launch)
    result = capture.capture_window(1234, TARGET[0], BOUNDS, cancel=cancelled)
    assert result.reason == 'cancelled' and result.png is None


def test_latest_permission_callback_refusal_is_fixed_and_has_no_marker(transport):
    calls = 0
    def guard():
        nonlocal calls
        calls += 1
        if calls >= 3: raise ValueError('synthetic-private-permission-text')
    result = capture.capture_window(1234, TARGET[0], BOUNDS, check=guard)
    assert result.reason == 'access_changed' and result.png is None
    assert 'synthetic' not in repr(result)


@pytest.mark.parametrize('stage',['response','permission','target'])
def test_deadline_checked_after_last_decode_and_identity_or_permission_read(transport,monkeypatch,stage):
    clock=[0.0]; monkeypatch.setattr(capture.time,'monotonic',lambda: clock[0])
    if stage=='response':
        original=capture._response
        def decoded(*args):
            result=original(*args); clock[0]=1.1; return result
        monkeypatch.setattr(capture,'_response',decoded)
    elif stage=='target':
        original=transport.native.target
        def target(hwnd):
            value=original(hwnd)
            if transport.native.calls==3: clock[0]=1.1
            return value
        transport.native.target=target
    guards=[0]
    def check():
        guards[0]+=1
        if stage=='permission' and guards[0]==4: clock[0]=1.1
    result=capture.capture_window(1234,TARGET[0],BOUNDS,timeout=.5,check=check)
    assert result.reason=='timeout' and result.png is None


def test_timeout_terminates_only_exact_owned_process(monkeypatch):
    native, process = FakeNative(), FakeProcess(done=False)
    monkeypatch.setattr(capture, '_Native', lambda: native)
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *a, **k: process)
    result = capture.capture_window(1234, TARGET[0], BOUNDS, timeout=.025)
    assert result.reason == 'timeout' and result.png is None
    assert process.terminations == 1
    assert capture._GATE.acquire(blocking=False)
    capture._GATE.release()


def test_unconfirmed_owned_exit_retains_gate_and_files_until_exit(monkeypatch):
    native, process = FakeNative(), FakeProcess(done=False)
    paths = []
    process.terminate = lambda: setattr(process, 'terminations', process.terminations + 1)
    def launch(command, **kwargs):
        paths.append(Path(command[-1])); return process
    monkeypatch.setattr(capture, '_Native', lambda: native)
    monkeypatch.setattr(capture.subprocess, 'Popen', launch)
    result = capture.capture_window(1234, TARGET[0], BOUNDS, timeout=.02)
    assert result.reason == 'timeout' and paths[0].exists()
    try:
        assert capture.capture_window(1234, TARGET[0], BOUNDS).reason == 'busy'
    finally:
        process.returncode = -1; process.exited.set()
    assert capture._GATE.acquire(timeout=2)
    capture._GATE.release()
    assert not paths[0].exists()


@pytest.mark.parametrize('mutation', [lambda d: d.update(nonce='b'*64), lambda d: d.update(pid=True),
    lambda d: d.update(pid=999), lambda d: d.update(format=True), lambda d: d.update(extra='private-marker'),
    lambda d: d.update(status='success'), lambda d: d.update(reason='private-marker'),
    lambda d: d.update(original_size=[True, 30]), lambda d: d.update(image_size=[True, 30]),
    lambda d: d.update(image_size=[1601, 30]), lambda d: d.update(png_bytes=True),
    lambda d: d.update(png_bytes=capture.MAX_PNG_BYTES+1), lambda d: d.update(png_sha256='a'*64)])
def test_response_is_closed_schema_and_exact_owned_identity(directory, mutation):
    value = request(bounds=(0, 0, 1, 30))
    response(directory, value, mutate=mutation)
    with pytest.raises(capture._Unavailable, match='unavailable'):
        capture._response(directory, value, SimpleNamespace(pid=2468))


@pytest.mark.parametrize('reason', ['blank', 'unsupported', 'changed'])
def test_typed_unavailable_response_has_no_image(directory, reason):
    value = request()
    capture._write(directory/'response.json', json.dumps({'format':1,'nonce':value['nonce'],
        'pid':2468,'status':'unavailable','reason':reason}).encode())
    result = capture._response(directory,value,SimpleNamespace(pid=2468))
    assert not result.available and result.reason == reason and result.png is None
    assert result.original_size is result.image_size is None


def test_protocol_exclusive_write_and_hardlink_rejection(directory):
    capture._write(directory/'request.json', b'{"synthetic":true}')
    with pytest.raises(FileExistsError): capture._write(directory/'request.json', b'overwrite')
    os.link(directory/'request.json', directory/'alias.json')
    with pytest.raises(capture._Unavailable): capture._read(directory/'request.json', 4096)
    assert (directory/'request.json').read_bytes() == b'{"synthetic":true}'


def test_output_link_replacement_never_reads_another_file(directory, tmp_path):
    outside=tmp_path/'synthetic-unrelated.json'; outside.write_bytes(b'synthetic-private-marker')
    try: os.symlink(outside,directory/'response.json')
    except OSError:
        os.link(outside,directory/'response.json')
    with pytest.raises(capture._Unavailable): capture._read(directory/'response.json',4096)
    assert outside.read_bytes()==b'synthetic-private-marker'


def test_cleanup_preserves_replaced_unknown_directory(monkeypatch):
    detached=[]; cleaned=[]
    directory=SimpleNamespace(name='untrusted-path',cleanup=lambda: cleaned.append(True),
        _finalizer=SimpleNamespace(detach=lambda: detached.append(True)))
    monkeypatch.setattr(capture,'_directory',lambda _: (_ for _ in ()).throw(capture._Unavailable('unsafe_output')))
    capture._cleanup(directory)
    assert detached==[True] and cleaned==[]


def test_unknown_owned_wait_cannot_clean_output_or_release_admission(monkeypatch):
    gate=threading.Lock(); gate.acquire(); monkeypatch.setattr(capture,'_GATE',gate)
    detached=[]; cleaned=[]
    directory=SimpleNamespace(cleanup=lambda: cleaned.append(True),
        _finalizer=SimpleNamespace(detach=lambda: detached.append(True)))
    process=SimpleNamespace(wait=lambda: (_ for _ in ()).throw(OSError('synthetic-private-wait-error')))
    capture._retire(process,directory)
    assert detached==[True] and cleaned==[] and not gate.acquire(blocking=False)
    gate.release()


def test_poll_failure_stays_fixed_and_retained_until_known_exit(monkeypatch):
    gate=threading.Lock(); monkeypatch.setattr(capture,'_GATE',gate)
    monkeypatch.setattr(capture,'_Native',FakeNative)
    released=threading.Event(); terminated=[]
    def poll():
        if released.is_set(): return -1
        raise OSError('synthetic-private-poll-error')
    process=SimpleNamespace(poll=poll,terminate=lambda: terminated.append(True),
        wait=lambda: released.wait(2),pid=2468)
    monkeypatch.setattr(capture.subprocess,'Popen',lambda *a,**k: process)
    result=capture.capture_window(1234,TARGET[0],BOUNDS)
    try:
        assert result.reason=='helper_failed' and result.png is None
        assert terminated==[True] and not gate.acquire(blocking=False)
    finally: released.set()
    assert gate.acquire(timeout=2); gate.release()


def test_private_directory_not_arbitrary_absolute_output(tmp_path):
    with pytest.raises(capture._Unavailable): capture._directory(str(tmp_path))
    with pytest.raises(capture._Unavailable): capture._directory('relative-output')


@pytest.mark.parametrize('field,value', [('nonce','a'*63), ('parent_pid',True), ('pid',0),
    ('created',False), ('format',True), ('deadline_tick',-1), ('extra','private-marker')])
def test_request_closed_fields_and_strict_bound_types(field, value):
    data = request(); data[field] = value
    with pytest.raises(capture._Unavailable): capture._request(data)


def test_png_decoding_rejects_wrong_format_mode_and_dimensions(directory):
    value = request()
    raw = png((40,30),format='JPEG')
    capture._write(directory/'capture.png',raw)
    data = {'format':1,'nonce':value['nonce'],'pid':2468,'status':'captured','reason':'captured',
        'original_size':[40,30],'image_size':[40,30],'png_bytes':len(raw),'png_sha256':hashlib.sha256(raw).hexdigest()}
    capture._write(directory/'response.json',json.dumps(data).encode())
    with pytest.raises(capture._Unavailable): capture._response(directory,value,SimpleNamespace(pid=2468))


def test_pixel_encoding_is_bounded_and_blank_refuses_without_fallback():
    raw = bytes((70,90,120,0)) * (2000*10)
    result,size = capture._encode(raw,(0,0,2000,10))
    assert size == (1600,8) and len(result) < capture.MAX_PNG_BYTES
    with pytest.raises(capture._Unavailable) as error: capture._encode(bytes(4*40*30),BOUNDS)
    assert error.value.reason == 'blank'
    with pytest.raises(capture._Unavailable): capture._encode(b'wrong-length',BOUNDS)


class GDI:
    def __init__(self, failure=None):
        self.failure=failure; self.calls=[]; self.pixels=ctypes.create_string_buffer(40*30*4)
    def CreateCompatibleDC(self, parent):
        assert parent is None; self.calls.append(('create_dc',)); return 0 if self.failure=='dc' else 101
    def CreateDIBSection(self, dc, info, usage, bits, section, offset):
        header=ctypes.cast(info,ctypes.POINTER(capture._BITMAPINFO)).contents.header
        assert (header.width,header.height,header.bits,header.planes,usage,section,offset)==(40,-30,32,1,0,None,0)
        ctypes.cast(bits,ctypes.POINTER(ctypes.c_void_p)).contents.value=ctypes.addressof(self.pixels)
        self.calls.append(('create_bitmap',)); return 0 if self.failure=='bitmap' else 202
    def SelectObject(self,dc,bitmap):
        self.calls.append(('select',dc,bitmap)); return 0 if self.failure=='select' else 303
    def DeleteObject(self,bitmap): self.calls.append(('delete_bitmap',bitmap)); return 1
    def DeleteDC(self,dc): self.calls.append(('delete_dc',dc)); return 1
    def GdiFlush(self): self.calls.append(('flush',)); return 0 if self.failure=='flush' else 1


@pytest.mark.parametrize('failure', [None,'dc','bitmap','select','print','flush','blank','exception'])
def test_memory_only_printwindow_and_every_owned_gdi_handle_cleaned(failure):
    native=capture._Native.__new__(capture._Native)
    gdi=GDI(failure); native.gdi=gdi; contexts=[]
    def print_window(hwnd,dc,flags):
        assert (hwnd,dc,flags)==(1234,101,0)
        if failure=='exception': raise RuntimeError('synthetic-private-render-error')
        if failure not in ('blank','print'):
            ctypes.memmove(gdi.pixels,bytes((70,90,120,0))*(40*30),40*30*4)
        return 0 if failure=='print' else 1
    native.user=SimpleNamespace(SetThreadDpiAwarenessContext=lambda value: contexts.append(value) or 44,
                               PrintWindow=print_window)
    if failure is None: assert native.render(1234,BOUNDS)==bytes((70,90,120,0))*(40*30)
    else:
        with pytest.raises(Exception): native.render(1234,BOUNDS)
    if failure!='dc': assert ('delete_dc',101) in gdi.calls
    if failure not in ('dc','bitmap'): assert ('delete_bitmap',202) in gdi.calls
    if failure not in ('dc','bitmap','select'): assert ('select',101,303) in gdi.calls
    assert len(contexts)==2 and contexts[-1]==44
    assert not any(name in vars(gdi) for name in ('GetDC','BitBlt','GetWindowDC'))


def test_win32_signatures_are_pointer_sized_and_only_target_rendering(monkeypatch):
    names=['SetThreadDpiAwarenessContext','IsWindow','IsWindowVisible','IsIconic',
        'GetWindowThreadProcessId','GetWindowRect','PrintWindow','CreateCompatibleDC',
        'CreateDIBSection','SelectObject','DeleteObject','DeleteDC','GdiFlush','OpenProcess',
        'GetProcessTimes','CloseHandle','GetTickCount64']
    functions={name: SimpleNamespace() for name in names}; loads=[]
    def load(name,**kwargs):
        loads.append((name,kwargs)); return SimpleNamespace(**functions)
    monkeypatch.setattr(capture,'os',SimpleNamespace(name='nt'))
    monkeypatch.setattr(capture.ctypes,'WinDLL',load,raising=False)
    capture._Native()
    assert [name for name,_ in loads]==['user32','gdi32','kernel32']
    assert functions['PrintWindow'].argtypes==[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint32]
    assert functions['CreateCompatibleDC'].restype is ctypes.c_void_p
    assert functions['CreateDIBSection'].restype is ctypes.c_void_p
    assert functions['OpenProcess'].restype is ctypes.c_void_p
    assert functions['GetTickCount64'].restype is ctypes.c_uint64
    assert ctypes.sizeof(capture._RECT)==16 and ctypes.sizeof(capture._BITMAPINFOHEADER)==40
    assert not {'GetDC','BitBlt','GetWindowDC','GetDesktopWindow'} & set(functions)


@pytest.mark.parametrize('success',[True,False])
def test_process_identity_handle_closed_even_on_metadata_failure(success):
    native=capture._Native.__new__(capture._Native); closed=[]
    def times(handle,created,*unused):
        assert handle==777
        pointer=ctypes.cast(created,ctypes.POINTER(capture._FILETIME)).contents
        pointer.low=12; pointer.high=3
        return int(success)
    def opened(access,inherit,pid):
        assert (access,inherit,pid)==(0x1000,False,4321); return 777
    native.kernel=SimpleNamespace(OpenProcess=opened,GetProcessTimes=times,CloseHandle=closed.append)
    if success: assert native.created(4321)==12|(3<<32)
    else:
        with pytest.raises(capture._Unavailable): native.created(4321)
    assert closed==[777]


def test_real_inert_helper_refuses_malformed_request_before_native_api(directory, tmp_path):
    capture._write(directory/'request.json',b'{"format":1}')
    environment=dict(os.environ,SIDEKICK_DATA_DIR=str(tmp_path/'inert-unused-state'))
    completed=subprocess.run([sys.executable,'-m','forge_window_capture','--directory',str(directory)],
        cwd=str(Path(capture.__file__).parent),env=environment,capture_output=True,timeout=3)
    assert completed.returncode==2 and completed.stdout==completed.stderr==b''
    assert set(path.name for path in directory.iterdir())=={'request.json'}
    assert not (tmp_path/'inert-unused-state').exists()


def test_real_source_launcher_has_exact_owned_pid_parent_and_current_environment():
    command,environment=capture._launch_command()
    program='import json,os,sys; from PIL import Image; print(json.dumps({"pid":os.getpid(),"parent":os.getppid(),"exe":sys.executable,"pillow":True}))'
    process=subprocess.Popen([command[0],'-c',program],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output,error=process.communicate(timeout=3)
    assert process.returncode==0 and error==b''
    result=json.loads(output)
    assert result['pid']==process.pid and result['parent']==os.getpid()
    assert os.path.normcase(result['exe'])==os.path.normcase(sys.executable) and result['pillow'] is True


def test_desktop_capture_entry_precedes_all_startup(monkeypatch):
    import desktop
    monkeypatch.setattr(sys,'argv',['Forge.exe','--forge-window-capture','--directory','synthetic-private-path'])
    monkeypatch.setattr(capture,'helper_entry',lambda: 37)
    monkeypatch.setattr(desktop,'_start_service',lambda *a,**k: pytest.fail('Capture must never start coordinator'),raising=False)
    assert desktop.main()==37


def test_helper_exact_parent_and_target_checks_before_and_after_render(directory, monkeypatch):
    native=FakeNative(); value=request(); value['parent_pid']=os.getppid()
    capture._write(directory/'request.json',json.dumps(value).encode())
    monkeypatch.setattr(capture,'_Native',lambda: native)
    monkeypatch.setattr(capture,'_watch',lambda *args: None)
    assert capture.helper_entry(['--directory',str(directory)])==0
    result=json.loads((directory/'response.json').read_text())
    assert result['status']=='captured' and result['pid']==os.getpid()
    assert native.calls==3


def test_helper_parent_reuse_refuses_before_render(directory, monkeypatch):
    value=request(); capture._write(directory/'request.json',json.dumps(value).encode())
    native=FakeNative(); native.created=lambda pid: 111
    native.render=lambda *args: pytest.fail('Wrong parent may never capture')
    monkeypatch.setattr(capture,'_Native',lambda: native)
    assert capture.helper_entry(['--directory',str(directory)])==2
    assert not (directory/'response.json').exists()


def test_helper_changed_target_after_render_writes_only_fixed_refusal(directory, monkeypatch):
    native=FakeNative(); value=request(); capture._write(directory/'request.json',json.dumps(value).encode())
    def render(*args):
        native.value=(TARGET[0],TARGET[1]+1,BOUNDS)
        return bytes((70,90,120,0))*(40*30)
    native.render=render
    monkeypatch.setattr(capture,'_Native',lambda: native)
    monkeypatch.setattr(capture,'_watch',lambda *args: None)
    assert capture.helper_entry(['--directory',str(directory)])==0
    assert json.loads((directory/'response.json').read_text())['reason']=='changed'
    assert not (directory/'capture.png').exists()


@pytest.mark.parametrize('alive,expired', [(False,False),(True,True)])
def test_helper_watchdog_exits_only_owned_helper_when_parent_or_deadline_invalid(monkeypatch, alive, expired):
    class Stop:
        calls=0
        def wait(self,interval): self.calls+=1; return self.calls>1
    exits=[]; native=FakeNative()
    native.created=lambda pid: 54321 if alive else 111
    native.ticks=lambda: 3000 if expired else 1000
    monkeypatch.setattr(capture.os,'_exit',exits.append)
    capture._watch(native,request(),Stop())
    assert exits==[3]
