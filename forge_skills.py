"""Lazy skill file index, portable manifests and confined resource retrieval."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import threading
from collections import OrderedDict

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__"}
PHASES = {"discover", "plan", "implement", "verify", "research"}
MANIFEST_KEYS = {"schema_version", "version", "bundle", "phases", "intents", "triggers",
                 "exclude_triggers", "requires_tools", "recommended_tools", "resources"}


def is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def signature(path):
    info = path.stat()
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1 or set(value) - MANIFEST_KEYS:
        raise ValueError("Skill manifest requires schema_version 1 and supported guidance fields.")
    result = dict(value)
    for key in ("phases", "intents", "triggers", "exclude_triggers", "requires_tools", "recommended_tools", "resources"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 64 or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in items):
            raise ValueError("Skill manifest " + key + " must contain bounded text values.")
        result[key] = list(dict.fromkeys(item.strip() for item in items))
    if set(result["phases"]) - PHASES:
        raise ValueError("Skill manifest contains an unsupported task phase.")
    for key in ("version", "bundle"):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 100):
            raise ValueError("Invalid skill manifest " + key + ".")
    for resource in result["resources"]:
        relative_path(resource)
    return result


def relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 500:
        raise ValueError("Supply a relative skill resource path.")
    windows = PureWindowsPath(value)
    parts = value.replace("\\", "/").split("/")
    if windows.drive or windows.root or any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError("Skill resources must remain inside their package.")
    if any(part in SKIP or part.startswith(".env") for part in parts):
        raise ValueError("This package resource is not available to the model.")
    return parts


def resource_path(skill_path, relative):
    base = Path(skill_path).parent
    if is_link(base) or is_link(Path(skill_path)):
        raise ValueError("Skill resources cannot traverse links.")
    target = base
    for part in relative_path(relative):
        target /= part
        if is_link(target):
            raise ValueError("Skill resources cannot traverse links.")
    if not target.resolve().is_relative_to(base.resolve()) or not target.is_file():
        raise ValueError("Skill resource was not found in this package.")
    if target.stat().st_size > 256000:
        raise ValueError("Skill resources are limited to 256 KB.")
    return target


class SkillIndex:
    """Stat known directories/files, reread only changed content; scope by root.

    Directory signatures catch added/removed nested packages without a timed
    stale window. Cached content never contains enabled/automatic preferences.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.roots = {}
        self.files = OrderedDict()
        self.descriptions = {}
        self.digests = {}
        self.cached_bytes = 0
        self.content_reads = 0
        self.directory_scans = 0

    def paths(self, root):
        root = Path(root)
        key = str(root.absolute())
        with self.lock:
            if not root.is_dir() or is_link(root):
                self.roots.pop(key, None)
                return []
            old = self.roots.get(key)
            if old:
                try:
                    if all(not is_link(Path(path)) and signature(Path(path)) == stamp for path, stamp in old["directories"]):
                        return list(old["paths"])
                except OSError:
                    pass
            paths, directories = [], []
            self.directory_scans += 1
            for current, dirs, files in os.walk(root, followlinks=False):
                folder = Path(current)
                if is_link(folder):
                    dirs[:] = []
                    continue
                dirs[:] = sorted(name for name in dirs if name not in SKIP and not is_link(folder / name))
                directories.append((str(folder), signature(folder)))
                if "SKILL.md" in files:
                    paths.append(folder / "SKILL.md")
                if len(paths) >= 500 or len(directories) >= 5000:
                    break
            self.roots[key] = {"paths": paths, "directories": directories}
            return list(paths)

    def read(self, path):
        path = Path(path)
        if is_link(path) or not path.is_file() or path.stat().st_size > 256000:
            raise ValueError("Skill file is missing, linked or too large.")
        key = str(path.absolute())
        stamp = signature(path)
        with self.lock:
            cached = self.files.get(key)
            if cached and cached["signature"] == stamp:
                self.files.move_to_end(key)
                return cached
            raw = path.read_bytes()
            if len(raw) > 256000 or is_link(path) or signature(path) != stamp:
                raise ValueError("Skill changed while being read. Try again.")
            self.content_reads += 1
            if cached:
                self.cached_bytes -= cached["byte_count"]
            cached = {"signature": stamp, "text": raw.decode("utf-8", errors="replace"), "byte_count": len(raw),
                      "digest": hashlib.sha256(raw).hexdigest()}
            self.files[key] = cached
            self.files.move_to_end(key)
            self.cached_bytes += len(raw)
            self.digests[key] = {"signature": stamp, "digest": cached["digest"]}
            # Package libraries must not retain an unbounded amount of source
            # text when many projects or large imported workflows are visited.
            while len(self.files) > 2048 or self.cached_bytes > 16 * 1024 * 1024:
                oldest = next(iter(self.files))
                if oldest == key:
                    break
                self.cached_bytes -= self.files.pop(oldest)["byte_count"]
            return cached

    def file_digest(self, path):
        path = Path(path)
        if is_link(path) or not path.is_file() or path.stat().st_size > 256000:
            raise ValueError("Skill file is missing, linked or too large.")
        key = str(path.absolute())
        with self.lock:
            stamp = signature(path)
            cached = self.digests.get(key)
            if cached and cached["signature"] == stamp:
                return cached["digest"]
            return self.read(path)["digest"]

    def describe(self, path, parser):
        """Keep tiny parsed metadata even when large unused text is evicted."""
        path = Path(path)
        if is_link(path) or not path.is_file() or path.stat().st_size > 256000:
            raise ValueError("Skill file is missing, linked or too large.")
        key = str(path.absolute()); sidecar = path.with_name("forge-skill.json")
        if is_link(sidecar): raise ValueError("Skill manifests cannot be links.")
        with self.lock:
            stamp = signature(path)
            manifest_stamp = signature(sidecar) if sidecar.is_file() else None
            cached = self.descriptions.get(key)
            if cached and cached["signature"] == stamp and cached["manifest_signature"] == manifest_stamp:
                return cached
            content = self.package(path)
            value = {key: content[key] for key in ("signature", "digest", "revision", "manifest", "manifest_error")}
            value.update(manifest_signature=manifest_stamp, frontmatter=parser(content["text"]))
            self.descriptions[key] = value
            # Retain multiple scoped project indexes without unbounded metadata.
            while len(self.descriptions) > 5000:
                self.descriptions.pop(next(iter(self.descriptions)))
            while len(self.digests) > 10000:
                self.digests.pop(next(iter(self.digests)))
            return value

    def package(self, path):
        content = self.read(path)
        sidecar = Path(path).with_name("forge-skill.json")
        manifest, error, manifest_digest = {}, None, ""
        if sidecar.exists() or is_link(sidecar):
            try:
                file = self.read(sidecar)
                manifest_digest = file["digest"]
                if "manifest" not in file:
                    file["manifest"] = validate_manifest(json.loads(file["text"]))
                manifest = file["manifest"]
            except (OSError, ValueError, TypeError) as exc:
                error = str(exc)[:300]
        revision = hashlib.sha256((content["digest"] + ":" + manifest_digest).encode()).hexdigest()
        return {**content, "manifest": dict(manifest), "manifest_error": error, "revision": revision}

    def invalidate(self, path=None):
        with self.lock:
            if path is None:
                self.roots.clear()
                self.files.clear()
                self.descriptions.clear()
                self.digests.clear()
                self.cached_bytes = 0
            else:
                key = str(Path(path).absolute())
                removed = self.files.pop(key, None)
                if removed:
                    self.cached_bytes -= removed["byte_count"]
                self.descriptions.pop(key, None)
                self.digests.pop(key, None)
                self.roots.clear()
