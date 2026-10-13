"""A native handoff freezes schedule admission and durable occurrence writes."""
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from forge_service import ForgeService
from test_forge_core import Engine


@pytest.fixture
def service(tmp_path, monkeypatch):
    class FakeVault:
        def __init__(self, *args, **kwargs): pass
        def get(self, reference): pytest.fail('No live credentials are needed')
    monkeypatch.setattr('forge_credentials.CredentialVault', FakeVault)
    value = ForgeService(core=Engine(), data_dir=tmp_path / 'disposable')
    value.store.update_settings({'model': 'synthetic-model'})
    value.jobs._launch = lambda run: None
    yield value
    value.update_manager = None
    value.shutdown()


def test_due_slots_remain_identical_during_handoff_and_admit_once_after_abort(service):
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    schedule = service.schedule_save({'prompt': 'Synthetic scheduled work', 'recurrence': 'daily',
        'timezone': 'UTC', 'time': '12:00', 'next_run': now.isoformat()})
    # Saving computes timing; pin the synthetic due slot explicitly.
    schedule = service.store.save_entity('schedules', {**schedule, 'next_run': now.isoformat()})
    service.update_manager = SimpleNamespace(applying=True)
    for _ in range(2): service.tick_schedules(now)
    assert service.store.entity('schedules', schedule['id']) == schedule
    with service.store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM occurrences').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM request_keys').fetchone()[0] == 0
    assert service.store.runs() == []
    with pytest.raises(ValueError, match='preparing an update'): service.schedule_run(schedule)
    service.update_manager.applying = False
    service.tick_schedules(now); service.tick_schedules(now)
    with service.store._connection() as db:
        occurrences = db.execute('SELECT * FROM occurrences').fetchall()
        keys = db.execute('SELECT source_key FROM request_keys').fetchall()
    assert len(occurrences) == len(keys) == len(service.store.runs()) == 1
    assert keys[0]['source_key'] == 'schedule:' + schedule['id'] + ':' + now.isoformat()


def test_background_tick_never_holds_schedule_lock_while_waiting_for_admission(service):
    attempted = threading.Event()
    actual = threading.RLock()
    main = threading.get_ident()
    class ObservedLock:
        def __enter__(self):
            if threading.get_ident() != main: attempted.set()
            actual.acquire(); return self
        def __exit__(self, *args): actual.release()
    service.admission_lock = ObservedLock()
    errors = []
    def tick():
        try: service.tick_schedules()
        except Exception as exc: errors.append(type(exc).__name__)
    with service.admission_lock:
        worker = threading.Thread(target=tick)
        worker.start()
        try:
            assert attempted.wait(2)
            assert service.schedule_lock.acquire(timeout=.2)
            service.schedule_lock.release()
            service.update_manager = SimpleNamespace(applying=True)
        finally:
            # Exiting admission releases a blocked tick before the bounded join.
            pass
    worker.join(2)
    assert not worker.is_alive() and errors == []
