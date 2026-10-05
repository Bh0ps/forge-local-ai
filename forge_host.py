"""Per-user host lifecycle and browser pairing, without startup inference."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sys
import threading
import time


def data_directory():
    return Path(os.getenv('FORGE_DATA_DIR') or os.getenv('SIDEKICK_DATA_DIR') or
                (Path.home() / '.forge')).expanduser().resolve()


class PairingAuthority:
    """Short-lived, single-use codes displayed only by the trusted native host."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._codes = {}
        self._sessions = {}
        self._attempts = {}

    def issue(self):
        code = secrets.token_hex(5).upper()
        with self._lock:
            self._codes = {code: self.clock() + 180}
        return {'code': code, 'expires_in': 180}

    def pair(self, code, client='local'):
        now = self.clock()
        with self._lock:
            attempts, reset = self._attempts.get(client, (0, now + 60))
            if now >= reset:
                attempts, reset = 0, now + 60
            if attempts >= 8:
                raise ValueError('Too many attempts. Try again in one minute.')
            self._attempts[client] = (attempts + 1, reset)
            candidate = str(code).strip().upper()
            found = next((key for key in self._codes if hmac.compare_digest(key, candidate)), None)
            if found is None or self._codes[found] < now:
                raise ValueError('Invalid or expired pairing code. Generate a code in Forge Settings.')
            del self._codes[found]
            token = secrets.token_urlsafe(48)
            self._sessions[hashlib.sha256(token.encode()).hexdigest()] = now + 86400
            return token

    def valid(self, token):
        if not isinstance(token, str) or len(token) > 256:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            return self._sessions.get(digest, 0) > self.clock()

    def revoke(self, token):
        with self._lock:
            self._sessions.pop(hashlib.sha256(str(token).encode()).hexdigest(), None)

    def close(self):
        with self._lock:
            self._codes.clear()
            self._sessions.clear()


class SingleCoordinator:
    """Windows named mutex; a filesystem lock for Linux/Docker hosts."""
    def __init__(self, directory=None):
        self.directory = Path(directory or data_directory()).resolve()
        self.handle = None
        self._file = None
        self.primary = False

    def acquire(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(self.directory).casefold().encode()).hexdigest()[:24]
        if os.name == 'nt':
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            self.handle = kernel.CreateMutexW(None, False, 'Local\\ForgeCoordinator-' + key)
            if not self.handle:
                raise OSError(ctypes.get_last_error(), 'Cannot create coordinator mutex')
            self.primary = ctypes.get_last_error() != 183
        else:
            import fcntl
            self._file = open(self.directory / 'coordinator.lock', 'a+b')
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.primary = True
            except BlockingIOError:
                self.primary = False
        return self.primary

    def close(self):
        if self.handle:
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(self.handle)
            self.handle = None
        if self._file:
            self._file.close()
            self._file = None
        self.primary = False


class EmergencyHotkey:
    """Own a Windows message pump so a blocked UI cannot prevent Stop."""
    def __init__(self, callback, shortcut='Ctrl+Alt+Shift+S'):
        self.callback = callback
        self.shortcut = shortcut
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self.error = None

    @staticmethod
    def parse(shortcut):
        parts = str(shortcut).upper().split('+')
        flags = {'ALT': 1, 'CTRL': 2, 'CONTROL': 2, 'SHIFT': 4}
        if len(parts) < 2 or any(x not in flags for x in parts[:-1]):
            raise ValueError('Use a shortcut such as Ctrl+Alt+Shift+S.')
        key = parts[-1]
        if len(key) != 1 or not key.isascii() or not key.isalnum():
            raise ValueError('The shortcut must end in one letter or digit.')
        return sum(set(flags[x] for x in parts[:-1])) | 0x4000, ord(key)

    def start(self):
        self.parse(self.shortcut)
        if os.name != 'nt':
            return False
        def pump():
            import ctypes
            from ctypes import wintypes
            user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
            self._thread_id = kernel.GetCurrentThreadId()
            modifiers, key = self.parse(self.shortcut)
            if not user.RegisterHotKey(None, 1, modifiers, key):
                self.error = 'Emergency shortcut is already registered.'
                self._ready.set()
                return
            self._ready.set()
            message = wintypes.MSG()
            try:
                while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    if message.message == 0x0312:
                        try:
                            self.callback()
                        except Exception:
                            pass
            finally:
                user.UnregisterHotKey(None, 1)
        self._thread = threading.Thread(target=pump, name='forge-emergency-stop', daemon=True)
        self._thread.start()
        self._ready.wait(3)
        return not self.error

    def stop(self):
        if self._thread_id and os.name == 'nt':
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)


def set_startup(enabled):
    """Only an explicit native settings action changes the per-user Run entry."""
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    if os.name != 'nt':
        raise ValueError('Windows startup is unavailable on this host.')
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if enabled:
            import subprocess
            command = ([sys.executable, '--background'] if getattr(sys, 'frozen', False)
                       else [sys.executable, str(Path(__file__).with_name('desktop.py')), '--background'])
            winreg.SetValueEx(key, 'Forge', 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        else:
            try:
                winreg.DeleteValue(key, 'Forge')
            except FileNotFoundError:
                pass
    return {'enabled': enabled}


@dataclass
class HostLifecycle:
    service: object
    pairing: PairingAuthority
    server: object = None
    shutting_down: bool = False

    def emergency_stop(self):
        callback = getattr(self.service, 'emergency_stop', None)
        if callback:
            return callback()
        return self.service.dispatch('cancel', {})

    def quit(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.pairing.close()
        self.emergency_stop()
        callback = getattr(self.service, 'shutdown', None)
        if callback:
            callback()
        if self.server:
            self.server.should_exit = True
