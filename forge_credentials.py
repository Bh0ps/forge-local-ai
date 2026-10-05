"""OS-backed secret storage. Configuration contains references, never passwords."""
from __future__ import annotations

import os
import json
import re
import uuid


class CredentialUnavailable(RuntimeError):
    pass


class CredentialVault:
    def __init__(self, namespace="Forge"):
        self.namespace = namespace

    def _target(self, reference):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", str(reference)):
            raise ValueError("Invalid credential reference")
        return f"{self.namespace}/{reference}"

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise CredentialUnavailable("Install keyring and configure an OS credential store") from exc
        backend = keyring.get_keyring()
        candidates = list(getattr(backend, "backends", [backend]))
        allowed = ("keyring.backends.SecretService", "keyring.backends.macOS", "keyring.backends.kwallet",
                   "keyring.backends.Windows", "keyring.backends.libsecret")
        for candidate in candidates:
            if candidate.__class__.__module__.startswith(allowed) and candidate.priority > 0:
                return candidate
        # Never accept keyrings.alt file backends, even if they advertise a
        # positive priority. Credentials belong in an OS secret service.
        raise CredentialUnavailable("Configure Secret Service, KWallet or the platform OS credential store")

    @staticmethod
    def _windows():
        import ctypes
        from ctypes import wintypes
        class Credential(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(Credential))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        return ctypes, Credential, api

    def put(self, value, reference=None):
        reference = reference or uuid.uuid4().hex
        target = self._target(reference)
        raw = str(value).encode("utf-8")
        if not raw or len(raw) > 64000:
            raise ValueError("Credential must contain between 1 and 64000 UTF-8 bytes")
        if os.name == "nt":
            old_parts = self._chunk_references(reference)
            new_parts = []
            if len(raw) > 2400:
                import base64
                # Each part is individually stored in Credential Manager; no
                # plaintext fragment or token ever goes to a local file.
                try:
                    for offset in range(0, len(raw), 1700):
                        part = uuid.uuid4().hex
                        self.put(base64.b64encode(raw[offset:offset+1700]).decode("ascii"), part)
                        new_parts.append(part)
                    raw = ("FORGE-VAULT-PARTS:" + json.dumps(new_parts)).encode("utf-8")
                except Exception:
                    for part in new_parts:
                        self.delete(part)
                    raise
            ctypes, Credential, api = self._windows()
            blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            item = Credential(Type=1, TargetName=target, CredentialBlobSize=len(raw),
                              CredentialBlob=blob, Persist=2, UserName="Forge")
            if not api.CredWriteW(ctypes.byref(item), 0):
                for part in new_parts:
                    self.delete(part)
                raise CredentialUnavailable("Windows Credential Manager refused the credential")
            for part in old_parts:
                self.delete(part)
        else:
            self._keyring().set_password(self.namespace, reference, str(value))
        return reference

    def get(self, reference):
        if not reference:
            return None
        target = self._target(reference)
        if os.name == "nt":
            ctypes, Credential, api = self._windows()
            pointer = ctypes.POINTER(Credential)()
            if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
                if ctypes.get_last_error() == 1168:
                    return None
                raise CredentialUnavailable("Windows Credential Manager could not read the credential")
            try:
                value = ctypes.string_at(pointer.contents.CredentialBlob,
                                         pointer.contents.CredentialBlobSize).decode("utf-8")
            finally:
                api.CredFree(pointer)
            if value.startswith("FORGE-VAULT-PARTS:"):
                import base64
                parts = json.loads(value.removeprefix("FORGE-VAULT-PARTS:"))
                if not isinstance(parts, list) or len(parts) > 40:
                    raise CredentialUnavailable("Invalid credential record")
                raw = []
                for part in parts:
                    fragment = self.get(part)
                    if fragment is None:
                        raise CredentialUnavailable("Credential record is incomplete")
                    raw.append(base64.b64decode(fragment, validate=True))
                return b"".join(raw).decode("utf-8")
            return value
        return self._keyring().get_password(self.namespace, reference)

    def delete(self, reference):
        target = self._target(reference)
        if os.name == "nt":
            parts = self._chunk_references(reference)
            ctypes, _, api = self._windows()
            if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
                raise CredentialUnavailable("Windows Credential Manager could not remove the credential")
            for part in parts:
                self.delete(part)
        else:
            backend = self._keyring()
            if backend.get_password(self.namespace, reference) is not None:
                backend.delete_password(self.namespace, reference)

    def _chunk_references(self, reference):
        if os.name != "nt":
            return []
        ctypes, Credential, api = self._windows()
        pointer = ctypes.POINTER(Credential)()
        if not api.CredReadW(self._target(reference), 1, 0, ctypes.byref(pointer)):
            return []
        try:
            value = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize).decode("utf-8")
        finally:
            api.CredFree(pointer)
        if not value.startswith("FORGE-VAULT-PARTS:"):
            return []
        parts = json.loads(value.removeprefix("FORGE-VAULT-PARTS:"))
        if not isinstance(parts, list) or len(parts) > 40:
            raise CredentialUnavailable("Invalid credential record")
        return parts
