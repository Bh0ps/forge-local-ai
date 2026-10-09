"""Inference waits retain durable requests and do not repeat completed tools."""
from test_forge_core import Engine, call, finished, service


def loading_timeout(data, cancel):
    raise ValueError('Timed out waiting for the model to load. Resume from the saved checkpoint.')
    yield


def test_load_timeout_preserves_checkpoint_and_resume_does_not_replay_write(tmp_path):
    engine = Engine([
        {'tool_calls': [call('write_file', {'path': 'result.txt', 'content': 'saved once'})]},
        loading_timeout,
        {'content': 'The saved write is verified.'},
    ])
    svc = service(tmp_path, engine)
    try:
        svc.store.update_settings({'permission_profile': 'full_access', 'context': 32768})
        folder = tmp_path / 'project'
        folder.mkdir()
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        request = 'Write the marker once and verify it. Preserve this exact request 🌍.'
        run = svc.jobs.start({'text': request, 'project_id': project['id']})
        paused = finished(svc, run)
        assert paused['status'] == 'paused'
        assert svc.store.run(run['id'])['request'] == request
        assert svc.store.run(run['id'])['settings']['context'] == 32768
        assert (folder / 'result.txt').read_text() == 'saved once'
        svc.jobs.resume(run['id'])
        assert finished(svc, run)['status'] == 'completed'
        with svc.store._connection() as db:
            writes = db.execute("SELECT status FROM invocations WHERE name='write_file'").fetchall()
        assert len(writes) == 1 and writes[0][0] == 'completed'
        assert request in str(engine.requests[-1]['messages'])
    finally:
        svc.shutdown()
