"""Native release detection and opt-in downloads. Background work never installs."""
from __future__ import annotations

import os
import sys
import threading
import time

from forge_updates import UPDATE_CHANNELS, UpdateManager


class UpdateMonitor:
    def __init__(self, manager, *, startup_delay=30, interval=6 * 60 * 60, retry_interval=5 * 60, poll_interval=30):
        self.manager = manager
        self.startup_delay, self.interval, self.poll_interval = startup_delay, interval, poll_interval
        self.retry_interval = retry_interval
        self.stop_event = threading.Event()
        self.thread = None
        self.active_check = None
        self.preference_generation = None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self._run, daemon=True, name='Forge-update-monitor')
        self.thread.start()

    def tick(self, now=None):
        if self.stop_event.is_set(): return
        settings = self.manager.store.get_settings()
        policy = self.manager.store.update_policy() if hasattr(self.manager.store, 'update_policy') else None
        generation_changed = policy is not None and policy['generation'] != self.preference_generation
        automatic = getattr(self.manager, 'automatic_downloads', None)
        if automatic is not None: automatic.tick(self.stop_event, now)
        if settings.get('update_checks', True) is not True:
            self.manager.cancel_monitor_check(self.stop_event)
            return
        channel = settings.get('update_channel', 'preview')
        if channel not in UPDATE_CHANNELS: channel = 'preview'
        status = self.manager.status()
        if self.manager.applying or status.get('inspection_required') or status.get('state') in ('checking', 'downloading', 'prepared'):
            return
        checked = status.get('checked_at', 0)
        if type(checked) is not int or checked < 0: checked = 0
        current = time.time() if now is None else now
        # Clock changes cannot suppress update checks indefinitely.
        interval = self.retry_interval if status.get('state') == 'error' and status.get('last_check_failed') is True else self.interval
        needs_check = automatic is not None and automatic.needs_check
        if not generation_changed and not needs_check and status.get('channel') == channel and 0 <= current - checked < interval:
            return
        try:
            if self.stop_event.is_set(): return
            self.manager.check_background(channel, stop_event=self.stop_event)
            self.active_check = self.manager.thread
            if policy is not None: self.preference_generation = policy['generation']
        except ValueError:
            pass  # A user operation may have acquired the updater meanwhile.

    def _run(self):
        if self.stop_event.wait(self.startup_delay): return
        while not self.stop_event.is_set():
            try: self.tick()
            except Exception: pass  # No raw local settings or network logs leave this monitor.
            if self.stop_event.wait(self.poll_interval): return

    def stop(self):
        self.manager.stop_monitor(self.stop_event)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)


def start_native_update_monitor(service):
    """Never contact public services from source launches or disposable QA hosts."""
    if not getattr(sys, 'frozen', False) or os.environ.get('FORGE_DATA_DIR') or os.environ.get('SIDEKICK_DATA_DIR') or any(
            flag in sys.argv for flag in ('--smoke-test', '--native-browser-smoke', '--dictation-smoke', '--builder-native-smoke')):
        return None
    with service.manager_lock:
        if service.update_manager is None:
            service.update_manager = UpdateManager(service)
        service.update_manager.start_monitor()
        return service.update_manager.monitor
