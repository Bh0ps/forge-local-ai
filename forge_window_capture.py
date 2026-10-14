"""Bounded target-rendered Windows capture, without desktop-pixel fallback.

PrintWindow is synchronous and application-dependent. Only the disposable
helper calls it. PNG data and protocol files are private, temporary and bounded;
neither identifiers nor image bytes are written to diagnostics.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time

MAX_PIXELS = 20_000_000
MAX_EDGE = 16_384
MAX_OUTPUT_EDGE = 1600
MAX_PNG_BYTES = 6 * 1024 * 1024
MAX_PROTOCOL_BYTES = 4096
MAX_SECONDS = 3.0
PREFIX = 'forge-window-capture-'
REASONS = frozenset({'captured', 'unsupported', 'blank', 'changed', 'geometry_mismatch',
    'cancelled', 'access_changed', 'timeout', 'busy', 'helper_failed', 'unsafe_output', 'image_too_large'})
_GATE = threading.Lock()


@dataclass(frozen=True, repr=False)
class WindowCapture:
    available: bool
    reason: str
    png: bytes | None = field(default=None, repr=False)
    original_size: tuple[int, int] | None = None
    image_size: tuple[int, int] | None = None


class _Unavailable(Exception):
    def __init__(self, reason):
        self.reason = reason if reason in REASONS else 'helper_failed'
        super().__init__('Window capture is unavailable.')


def _dimensions(bounds):
    if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or any(type(value) is not int or not -(2**31) <= value < 2**31 for value in bounds)):
        raise _Unavailable('geometry_mismatch')
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if not 1 <= width <= MAX_EDGE or not 1 <= height <= MAX_EDGE or width * height > MAX_PIXELS:
        raise _Unavailable('image_too_large')
    return width, height


def _integer(value, maximum=2**64-1):
    return type(value) is int and 0 < value <= maximum


def _plain(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise _Unavailable('unsafe_output')
    return info


def _directory(value):
    path = Path(value)
    base = Path(tempfile.gettempdir()).absolute()
    if not path.is_absolute() or path.parent != base or not path.name.startswith(PREFIX):
        raise _Unavailable('unsafe_output')
    for parent in [path, *path.parents]:
        _plain(parent, directory=True)
    return path


def _read(path, limit):
    before = _plain(path)
    if before.st_nlink != 1 or not 0 < before.st_size <= limit:
        raise _Unavailable('unsafe_output')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        current = os.fstat(descriptor)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (current.st_dev, current.st_ino, current.st_size) != (before.st_dev, before.st_ino, before.st_size)):
            raise _Unavailable('unsafe_output')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(limit+1)
        after = _plain(path)
        if len(raw) != before.st_size or (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise _Unavailable('unsafe_output')
        return raw
    finally:
        if descriptor is not None: os.close(descriptor)


def _write(path, raw):
    _plain(path.parent, directory=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_nlink != 1:
            raise _Unavailable('unsafe_output')
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class _RECT(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ('left', 'top', 'right', 'bottom')]


class _FILETIME(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('width', ctypes.c_int32), ('height', ctypes.c_int32),
        ('planes', ctypes.c_uint16), ('bits', ctypes.c_uint16), ('compression', ctypes.c_uint32),
        ('image_bytes', ctypes.c_uint32), ('xppm', ctypes.c_int32), ('yppm', ctypes.c_int32),
        ('used', ctypes.c_uint32), ('important', ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('header', _BITMAPINFOHEADER), ('color', ctypes.c_uint32)]


class _Native:
    def __init__(self):
        if os.name != 'nt': raise _Unavailable('unsupported')
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        pointer, uint, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        signatures = (
            (self.user, 'SetThreadDpiAwarenessContext', [pointer], pointer),
            (self.user, 'IsWindow', [pointer], boolean), (self.user, 'IsWindowVisible', [pointer], boolean),
            (self.user, 'IsIconic', [pointer], boolean),
            (self.user, 'GetWindowThreadProcessId', [pointer, ctypes.POINTER(uint)], uint),
            (self.user, 'GetWindowRect', [pointer, ctypes.POINTER(_RECT)], boolean),
            (self.user, 'PrintWindow', [pointer, pointer, uint], boolean),
            (self.gdi, 'CreateCompatibleDC', [pointer], pointer),
            (self.gdi, 'CreateDIBSection', [pointer, ctypes.POINTER(_BITMAPINFO), uint,
                ctypes.POINTER(pointer), pointer, uint], pointer),
            (self.gdi, 'SelectObject', [pointer, pointer], pointer),
            (self.gdi, 'DeleteObject', [pointer], boolean), (self.gdi, 'DeleteDC', [pointer], boolean),
            (self.gdi, 'GdiFlush', [], boolean),
            (self.kernel, 'OpenProcess', [uint, boolean, uint], pointer),
            (self.kernel, 'GetProcessTimes', [pointer, *[ctypes.POINTER(_FILETIME)]*4], boolean),
            (self.kernel, 'CloseHandle', [pointer], boolean), (self.kernel, 'GetTickCount64', [], ctypes.c_uint64),
        )
        for library, name, args, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    @contextmanager
    def dpi(self):
        previous = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous: raise _Unavailable('unsupported')
        try: yield
        finally: self.user.SetThreadDpiAwarenessContext(previous)

    def created(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle: raise _Unavailable('unsupported')
        try:
            creation, exit_time, kernel_time, user_time = [_FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in (creation, exit_time, kernel_time, user_time)]):
                raise _Unavailable('unsupported')
            return creation.low | creation.high << 32
        finally: self.kernel.CloseHandle(handle)

    def ticks(self): return int(self.kernel.GetTickCount64())

    def target(self, hwnd):
        with self.dpi():
            if not self.user.IsWindow(hwnd) or not self.user.IsWindowVisible(hwnd) or self.user.IsIconic(hwnd):
                raise _Unavailable('unsupported')
            pid, rect = ctypes.c_uint32(), _RECT()
            if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not self.user.GetWindowRect(hwnd, ctypes.byref(rect)):
                raise _Unavailable('unsupported')
            bounds = (rect.left, rect.top, rect.right, rect.bottom)
            _dimensions(bounds)
            return int(pid.value), self.created(pid.value), bounds

    def render(self, hwnd, bounds):
        width, height = _dimensions(bounds)
        assert ctypes.sizeof(_BITMAPINFOHEADER) == 40
        dc = bitmap = previous = None
        with self.dpi():
            try:
                dc = self.gdi.CreateCompatibleDC(None)
                if not dc: raise _Unavailable('unsupported')
                info = _BITMAPINFO()
                info.header = _BITMAPINFOHEADER(40, width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0)
                bits = ctypes.c_void_p()
                bitmap = self.gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
                if not bitmap or not bits.value: raise _Unavailable('unsupported')
                previous = self.gdi.SelectObject(dc, bitmap)
                if not previous or previous == ctypes.c_void_p(-1).value: raise _Unavailable('unsupported')
                sentinel = b'\x13\x53\xad\x81'
                ctypes.memmove(bits.value, sentinel*(width*height), width*height*4)
                if not self.user.PrintWindow(hwnd, dc, 0) or not self.gdi.GdiFlush():
                    raise _Unavailable('unsupported')
                raw = ctypes.string_at(bits.value, width*height*4)
                if sentinel*32 in raw or raw == sentinel*(width*height): raise _Unavailable('blank')
                return raw
            finally:
                if previous and previous != ctypes.c_void_p(-1).value and dc: self.gdi.SelectObject(dc, previous)
                if bitmap: self.gdi.DeleteObject(bitmap)
                if dc: self.gdi.DeleteDC(dc)


def _request(value):
    fields = {'format','nonce','parent_pid','parent_created','hwnd','pid','created','bounds','deadline_tick'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['format']) is not int or value['format'] != 1
            or not isinstance(value['nonce'], str) or len(value['nonce']) != 64
            or any(char not in '0123456789abcdef' for char in value['nonce'])
            or any(not _integer(value[name], 2**32-1) for name in ('pid','parent_pid'))
            or any(not _integer(value[name]) for name in ('hwnd','created','parent_created','deadline_tick'))):
        raise _Unavailable('helper_failed')
    _dimensions(value['bounds'])
    return value


def _match(native, request):
    if native.target(request['hwnd']) != (request['pid'], request['created'], tuple(request['bounds'])):
        raise _Unavailable('changed')


def _encode(raw, bounds):
    from PIL import Image
    width, height = _dimensions(bounds)
    if not isinstance(raw, bytes) or len(raw) != width*height*4: raise _Unavailable('helper_failed')
    with Image.frombytes('RGB', (width,height), raw, 'raw', 'BGRX') as image:
        if all(low == high == 0 for low,high in image.getextrema()): raise _Unavailable('blank')
        image.thumbnail((MAX_OUTPUT_EDGE,MAX_OUTPUT_EDGE))
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        result, size = stream.getvalue(), image.size
    if not 0 < len(result) <= MAX_PNG_BYTES: raise _Unavailable('image_too_large')
    return result, size


def _watch(native, request, stopped):
    while not stopped.wait(.025):
        try:
            valid = native.created(request['parent_pid']) == request['parent_created']
            if not valid or native.ticks() >= request['deadline_tick']: os._exit(3)
        except Exception: os._exit(3)


def _helper(directory):
    request = _request(json.loads(_read(directory/'request.json', MAX_PROTOCOL_BYTES)))
    native = _Native()
    if (os.getppid() != request['parent_pid'] or native.created(request['parent_pid']) != request['parent_created']
            or not native.ticks() < request['deadline_tick'] <= native.ticks()+3000):
        raise _Unavailable('helper_failed')
    stopped = threading.Event()
    watcher = threading.Thread(target=_watch, args=(native,request,stopped), daemon=True)
    watcher.start()
    response = {'format':1,'nonce':request['nonce'],'pid':os.getpid(),'status':'unavailable','reason':'helper_failed'}
    try:
        _match(native,request)
        raw = native.render(request['hwnd'],request['bounds'])
        _match(native,request)
        png, size = _encode(raw,request['bounds'])
        _match(native,request)
        _write(directory/'capture.png', png)
        response.update(status='captured',reason='captured',original_size=list(_dimensions(request['bounds'])),
                        image_size=list(size),png_bytes=len(png),png_sha256=hashlib.sha256(png).hexdigest())
    except _Unavailable as error: response['reason'] = error.reason
    except Exception: response['reason'] = 'helper_failed'
    finally:
        try: _write(directory/'response.json', json.dumps(response,separators=(',',':')).encode())
        finally: stopped.set()
    return 0


def helper_entry(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == '--forge-window-capture': args.pop(0)
    if len(args) != 2 or args[0] != '--directory': return 2
    try: return _helper(_directory(args[1]))
    except Exception: return 2


def _fence(cancel, check):
    if cancel is not None and cancel.is_set(): raise _Unavailable('cancelled')
    if check is not None:
        try: check()
        except Exception: raise _Unavailable('access_changed') from None
    if cancel is not None and cancel.is_set(): raise _Unavailable('cancelled')


def _response(directory, request, process):
    value = json.loads(_read(directory/'response.json',MAX_PROTOCOL_BYTES))
    common = {'format','nonce','pid','status','reason'}
    if (not isinstance(value,dict) or value.get('format') != 1 or type(value.get('format')) is not int
            or value.get('nonce') != request['nonce'] or type(value.get('pid')) is not int or value['pid'] != process.pid
            or value.get('reason') not in REASONS): raise _Unavailable('unsafe_output')
    if value.get('status') == 'unavailable':
        if set(value) != common or value['reason'] == 'captured': raise _Unavailable('unsafe_output')
        return WindowCapture(False,value['reason'])
    if set(value) != common | {'original_size','image_size','png_bytes','png_sha256'} or value.get('status') != 'captured' or value['reason'] != 'captured':
        raise _Unavailable('unsafe_output')
    original = _dimensions(request['bounds'])
    size = value['image_size']
    original_value = value['original_size']
    if (not isinstance(original_value,list) or len(original_value) != 2
            or any(type(item) is not int for item in original_value) or original_value != list(original)
            or not isinstance(size,list) or len(size) != 2
            or any(type(item) is not int or not 1 <= item <= MAX_OUTPUT_EDGE for item in size)
            or any(size[i] > original[i] for i in range(2))
            or type(value['png_bytes']) is not int or not 0 < value['png_bytes'] <= MAX_PNG_BYTES):
        raise _Unavailable('unsafe_output')
    png = _read(directory/'capture.png',MAX_PNG_BYTES)
    if len(png) != value['png_bytes'] or hashlib.sha256(png).hexdigest() != value['png_sha256']: raise _Unavailable('unsafe_output')
    from PIL import Image
    with Image.open(io.BytesIO(png)) as image:
        if image.format != 'PNG' or image.size != tuple(size) or image.mode != 'RGB': raise _Unavailable('unsafe_output')
        image.load()
    return WindowCapture(True,'captured',png,original,tuple(size))


def _cleanup(directory):
    if directory is None: return
    try:
        _directory(directory.name)  # Absolute checked ordinary target before recursive cleanup.
        directory.cleanup()
    except Exception:
        _preserve(directory)  # Preserve an unknown replacement; never remove its contents.


def _preserve(directory):
    if directory is not None: directory._finalizer.detach()


def _running(process):
    try: return process.poll() is None
    except Exception: return True  # Unknown exit never permits another helper or output cleanup.


def _retire(process, directory):
    try:
        process.wait()
        if _running(process):
            _preserve(directory)
            return
    except Exception:
        _preserve(directory)
        return
    _cleanup(directory)
    _GATE.release()


def _launch_command():
    environment = {key:value for key,value in os.environ.items()
                   if key.upper() in {'SYSTEMROOT','WINDIR','TEMP','TMP'}}
    if getattr(sys,'frozen',False):
        return [sys.executable,'--forge-window-capture'],environment
    executable = sys.executable
    base = getattr(sys,'_base_executable',executable)
    if os.name == 'nt' and base and os.path.normcase(base) != os.path.normcase(executable):
        # CPython's Windows venv redirector creates another process. Launch the
        # actual interpreter directly, preserving only this runtime's venv path
        # as CPython's own multiprocessing launcher does. Exact PID/PPID checks
        # then hold without trusting a grandparent or accepting a different PID.
        if not isinstance(base,str) or not os.path.isabs(base): raise _Unavailable('helper_failed')
        environment['__PYVENV_LAUNCHER__'] = executable
        executable = base
    return [executable,'-m','forge_window_capture'],environment


def capture_window(hwnd, expected_pid, bounds, *, cancel=None, check=None, timeout=MAX_SECONDS):
    """Capture one exact visible target; failure never falls back to screen pixels."""
    if not _GATE.acquire(blocking=False): return WindowCapture(False,'busy')
    directory = process = None
    transferred = False
    try:
        if (not _integer(hwnd) or not _integer(expected_pid,2**32-1)
                or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_SECONDS):
            raise _Unavailable('unsupported')
        _dimensions(bounds)
        deadline = time.monotonic()+timeout
        _fence(cancel,check)
        native = _Native()
        pid,created,physical = native.target(hwnd)
        if pid != expected_pid: raise _Unavailable('changed')
        if physical != tuple(bounds): raise _Unavailable('geometry_mismatch')
        request = {'format':1,'nonce':secrets.token_hex(32),'parent_pid':os.getpid(),
            'parent_created':native.created(os.getpid()),'hwnd':hwnd,'pid':pid,'created':created,
            'bounds':list(bounds),'deadline_tick':native.ticks()+max(1,int((deadline-time.monotonic())*1000))}
        _request(request)
        directory = tempfile.TemporaryDirectory(prefix=PREFIX)
        path = _directory(directory.name)
        _write(path/'request.json',json.dumps(request,separators=(',',':')).encode())
        _fence(cancel,check)
        _match(native,request)
        if time.monotonic() >= deadline: raise _Unavailable('timeout')
        command,environment = _launch_command()
        process = subprocess.Popen([*command,'--directory',str(path)],cwd=str(Path(__file__).resolve().parent),
            env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        while process.poll() is None:
            _fence(cancel,check)
            if time.monotonic() >= deadline: raise _Unavailable('timeout')
            time.sleep(min(.01,max(0,deadline-time.monotonic())))
        _fence(cancel,check)
        if time.monotonic() >= deadline: raise _Unavailable('timeout')
        if process.returncode != 0: raise _Unavailable('helper_failed')
        result = _response(path,request,process)
        _fence(cancel,check)
        _match(native,request)
        if time.monotonic() >= deadline: raise _Unavailable('timeout')
        return result
    except _Unavailable as error: return WindowCapture(False,error.reason)
    except Exception: return WindowCapture(False,'helper_failed')
    finally:
        if process is not None and _running(process):
            try: process.terminate()
            except Exception: pass
            if _running(process):
                transferred = True
                try: threading.Thread(target=_retire,args=(process,directory),daemon=True).start()
                except Exception: _preserve(directory)
        if not transferred:
            _cleanup(directory)
            _GATE.release()


if __name__ == '__main__':
    raise SystemExit(helper_entry())
