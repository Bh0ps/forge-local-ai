"""Independent migration, long-horizon work and scheduling acceptance scenarios."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from core import COMPACTION_SYSTEM_PROMPT
from forge_service import ForgeService
from forge_store import ForgeStore
from storage import Store


class ScriptedEngine:
    base = "http://127.0.0.1:11434/api"
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []
    def dispatch(self, action, data):
        if action == "show":
            return {"capabilities": ["tools", "vision"], "model_info": {"fixture.context_length": 262144}}
        if action == "models":
            return {"models": [{"name": "fixture"}]}
        raise ValueError(action)
    def stream_agent(self, data, cancel):
        self.requests.append(data)
        if any(message["role"] == "system" and COMPACTION_SYSTEM_PROMPT in message["content"] for message in data["messages"]):
            yield {"done": True, "done_reason": "stop", "eval_count": 10, "prompt_eval_count": 100,
                   "message": {"content": "Saved completed actions and artifact references. Continue the goal checklist."}}
            return
        response = self.responses.pop(0) if self.responses else {"message": {"content": "Done"}}
        yield {"done": True, "done_reason": "stop", "eval_count": 10, "prompt_eval_count": 100, **response}


def tool(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def wait_finished(service, run):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = service.jobs.poll(run["id"])
        if result["finished"]:
            return result
        time.sleep(.01)
    raise AssertionError("Disposable fixture run did not finish")


@pytest.fixture
def coordinator(tmp_path):
    service = ForgeService(core=ScriptedEngine(), data_dir=tmp_path / "forge")
    service.store.update_settings({"model": "fixture", "permission_profile": "full_access", "browser_tools": False})
    yield service
    service.shutdown()


def git_project(service, folder):
    folder.mkdir()
    service._git(folder, ["init", "-b", "main"])
    (folder / "shared.txt").write_text("base\n", encoding="utf-8")
    service._git(folder, ["add", "-A"])
    service._git(folder, ["commit", "-m", "Disposable fixture base"])
    return service.create_project({"name": "Disposable project", "path": str(folder)})


def test_sidekick_migration_preserves_ids_paths_history_and_backups(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    old = local / "Sidekick"
    project_folder = tmp_path / "existing-project"
    project_folder.mkdir()
    legacy = Store(old)
    project = legacy.create_project("Existing project", str(project_folder))
    chat = legacy.create_chat(project["id"], "Existing chat", "fixture")
    message = legacy.add_message(chat["id"], "user", "Preserve this message 🌍")
    task = legacy.create_task(project["id"], "Existing task")
    legacy.update_task(project["id"], task["id"], "completed")
    legacy.update_settings({"context": 65536})
    legacy.set_summary(chat["id"], "Existing summary", message["id"])
    (old / "attachments").mkdir()
    (old / "attachments/fixture.bin").write_bytes(b"attachment bytes")
    (old / "backups/nested").mkdir(parents=True)
    backup_manifest = {"project_id": project["id"], "path": str(project_folder / "shared.txt"), "sha256": "a" * 64}
    (old / "backups/nested/manifest.json").write_text(json.dumps(backup_manifest))
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.delenv("FORGE_DATA_DIR", raising=False)
    monkeypatch.delenv("SIDEKICK_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    migrated = ForgeStore()
    assert migrated.get_project(project["id"])["path"] == str(project_folder)
    restored_chat = migrated.get_chat(chat["id"])
    assert restored_chat["messages"][0]["id"] == message["id"]
    assert restored_chat["messages"][0]["content"] == "Preserve this message 🌍"
    assert restored_chat["summary"] == "Existing summary"
    assert migrated.list_tasks(project["id"])[0]["id"] == task["id"]
    assert migrated.list_tasks(project["id"])[0]["status"] == "completed"
    assert migrated.get_settings()["context"] == 65536
    assert (migrated.home / "attachments/fixture.bin").read_bytes() == b"attachment bytes"
    assert json.loads((migrated.home / "backups/nested/manifest.json").read_text()) == backup_manifest
    snapshots = list((migrated.home / "backups").glob("pre-upgrade-*"))
    assert len(snapshots) == 1
    assert (snapshots[0] / "sidekick.sqlite3").is_file()
    assert (snapshots[0] / "attachments/fixture.bin").read_bytes() == b"attachment bytes"
    assert json.loads((snapshots[0] / "backups/nested/manifest.json").read_text()) == backup_manifest
    with sqlite3.connect(snapshots[0] / "sidekick.sqlite3") as database:
        assert database.execute("SELECT content FROM messages WHERE id=?", (message["id"],)).fetchone()[0] == message["content"]
        assert database.execute("SELECT name FROM sqlite_master WHERE name='forge_migrations'").fetchone() is None
    again = ForgeStore()
    assert again.list_chats()[0]["id"] == chat["id"]
    assert len(list((again.home / "backups").glob("pre-upgrade-*"))) == 1
    assert legacy.get_chat(chat["id"])["summary"] == "Existing summary"


def test_parallel_worktrees_preserve_dirty_parent(coordinator, tmp_path):
    folder = tmp_path / "repo"
    project = git_project(coordinator, folder)
    (folder / "shared.txt").write_text("unsaved user edit\n")
    (folder / "untracked.txt").write_text("untracked user file\n")
    results = []
    threads = [threading.Thread(target=lambda: results.append(coordinator.worktree_create({"project_id": project["id"]}))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(results) == 2
    assert len({result["worktree"]["branch"] for result in results}) == 2
    assert len({result["project"]["id"] for result in results}) == 2
    assert (folder / "shared.txt").read_text() == "unsaved user edit\n"
    assert (folder / "untracked.txt").read_text() == "untracked user file\n"
    for result in results:
        path = Path(result["worktree"]["path"])
        assert path.is_relative_to(coordinator.store.home / "worktrees")
        assert (path / "shared.txt").read_text() == "base\n"
        (path / "agent.txt").write_text(result["worktree"]["id"])
        with pytest.raises(ValueError, match="uncommitted"):
            coordinator.worktree_integrate({"id": result["worktree"]["id"]})


def test_worktree_integration_conflict_is_visible_and_recoverable(coordinator, tmp_path):
    folder = tmp_path / "repo"
    project = git_project(coordinator, folder)
    result = coordinator.worktree_create({"project_id": project["id"]})
    worktree = result["worktree"]
    child = Path(worktree["path"])
    (child / "shared.txt").write_text("agent edit\n")
    (folder / "shared.txt").write_text("parent edit\n")
    coordinator._git(folder, ["add", "-A"])
    coordinator._git(folder, ["commit", "-m", "Disposable parent edit"])
    merged = coordinator.worktree_integrate({"id": worktree["id"]})
    assert not merged["ok"] and merged["conflicts"] == ["shared.txt"]
    assert coordinator.store.entity("worktrees", worktree["id"])["status"] == "conflict"
    assert (child / "shared.txt").read_text() == "agent edit\n"
    assert "<<<<<<<" in (folder / "shared.txt").read_text()
    coordinator._git(folder, ["merge", "--abort"])
    assert (folder / "shared.txt").read_text() == "parent edit\n"
    assert child.is_dir()


def test_goal_continues_across_compactions_and_restart_without_repeating_writes(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    # Bounded tool-result excerpts now keep four reads within 16K. Eight large
    # reads force actual history pressure before and after restart.
    for number in range(8):
        (folder / f"large-{number}.txt").write_text("漢字🌍" * 14000, encoding="utf-8")
    service = ForgeService(core=ScriptedEngine(), data_dir=tmp_path / "forge")
    restored = None
    try:
        # This regression resumes the legacy conversational execution protocol;
        # new guided runs require typed acceptance rather than narrative evidence.
        service.store.update_settings({"model": "fixture", "context": 16384, "permission_profile": "full_access", "browser_tools": False,"guided_execution":False})
        project = service.create_project({"name": "Long task", "path": str(folder)})
        goal = service.goal_create({"text": "Create first.txt then second.txt 🌍", "project_id": project["id"],
            "tasks": [{"text": "Create first.txt", "status": "pending", "evidence": []}, {"text": "Create second.txt", "status": "pending", "evidence": []}]})
        tasks = goal["tasks"]
        first_tasks = [{**tasks[0], "status": "completed", "evidence": ["first.txt contains first"]}, tasks[1]]
        batch = [tool("read_file", {"path": f"large-{number}.txt"}) for number in range(8)]
        service.core.responses = [
            {"message": {"tool_calls": [tool("write_file", {"path": "first.txt", "content": "first"}),
                tool("goal_update", {"tasks": first_tasks, "checkpoint": "First file verified", "next_action": "Create second.txt"})]}},
            {"message": {"tool_calls": batch}},
            {"done_reason": "length", "message": {"content": "Output limit; continue from saved checklist"}},
        ]
        run = service.goal_resume({"id": goal["id"]})
        accepted_request = service.store.run(run["id"])["request"]
        assert wait_finished(service, run)["status"] == "paused"
        assert (folder / "first.txt").read_text() == "first"
        assert not (folder / "second.txt").exists()
        checkpoint = service.store.goal(goal["id"])
        assert checkpoint["tasks"][0]["status"] == "completed"
        assert checkpoint["tasks"][1]["status"] == "pending"
        assert not checkpoint["external_edits"]
        assert any(event["type"] == "compacted" for event in service.store.events(run["id"]))
        service.shutdown()
        completed_tasks = [{**first_tasks[0]}, {**tasks[1], "status": "completed", "evidence": ["second.txt contains second"]}]
        engine = ScriptedEngine([
            {"message": {"tool_calls": batch}},
            {"message": {"tool_calls": [tool("write_file", {"path": "second.txt", "content": "second"}),
                tool("goal_update", {"tasks": completed_tasks, "checkpoint": "Both files verified", "next_action": "Review evidence"})]}},
            {"message": {"content": "Both files are complete"}},
        ])
        restored = ForgeService(core=engine, data_dir=tmp_path / "forge")
        resumed = restored.jobs.resume(run["id"])
        assert wait_finished(restored, resumed)["status"] == "completed"
        final_goal = restored.store.goal(goal["id"])
        assert final_goal["status"] == "completed"
        assert [task["id"] for task in final_goal["tasks"]] == [task["id"] for task in tasks]
        assert [task["status"] for task in final_goal["tasks"]] == ["completed", "completed"]
        assert all(task["evidence"] for task in final_goal["tasks"])
        assert "1. [x] Create first.txt" in final_goal["markdown"]
        assert "2. [x] Create second.txt" in final_goal["markdown"]
        assert len([event for event in restored.store.events(run["id"]) if event["type"] == "compacted"]) >= 2
        with restored.store._connection() as database:
            writes = database.execute("SELECT arguments FROM invocations WHERE run_id=? AND name='write_file'", (run["id"],)).fetchall()
        assert [json.loads(row[0])["path"] for row in writes] == ["first.txt", "second.txt"]
        assert (folder / "first.txt").read_text() == "first" and (folder / "second.txt").read_text() == "second"
        main_requests = [request for request in engine.requests if request["tools"]]
        assert any(message["content"] == accepted_request for message in main_requests[0]["messages"])
        assert accepted_request.endswith(goal["request"])
        if restored.store.run(run['id']).get('execution_mode')=='guided':
            assert any(tasks[0]['id'] in message['content'] and tasks[1]['id'] in message['content'] for message in main_requests[0]['messages'])
        else:
            assert any("1. [x] Create first.txt" in message["content"] for message in main_requests[0]["messages"])
    finally:
        if restored:
            restored.shutdown()
        service.shutdown()


@pytest.mark.parametrize("recurrence,now,expected", [
    ("daily", datetime(2026, 3, 29, 0, 40, tzinfo=timezone.utc), datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)),
    ("hourly", datetime(2026, 10, 25, 0, 40, tzinfo=timezone.utc), datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)),
    ("daily", datetime(2026, 10, 25, 0, 40, tzinfo=timezone.utc), datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc)),
])
def test_schedule_dst_next_occurrence_is_future(recurrence, now, expected):
    schedule = {"timezone": "Europe/Dublin", "time": "01:30", "recurrence": recurrence}
    result = ForgeService.next_occurrence(schedule, now)
    assert result == expected
    assert result > now


def test_schedule_coalesces_downtime_catchup_and_prevents_overlap(coordinator):
    coordinator.jobs._launch = lambda run: None
    schedule = coordinator.schedule_save({"name": "Catchup", "prompt": "Run one check", "recurrence": "hourly",
        "time": "09:00", "timezone": "UTC", "next_run": "2026-10-01T09:00:00+00:00"})
    now = datetime(2026, 10, 5, 15, 10, tzinfo=timezone.utc)
    coordinator.tick_schedules(now)
    runs = [run for run in coordinator.store.runs() if run.get("schedule_id") == schedule["id"]]
    assert len(runs) == 1
    updated = coordinator.store.entity("schedules", schedule["id"])
    assert datetime.fromisoformat(updated["next_run"]) > now
    coordinator.tick_schedules(now)
    coordinator.schedule_run(updated)
    assert len(coordinator.store.runs()) == 1
    with coordinator.store._connection() as database:
        occurrences = database.execute("SELECT * FROM occurrences WHERE schedule_id=?", (schedule["id"],)).fetchall()
    assert len(occurrences) == 1 and occurrences[0]["run_id"] == runs[0]["id"]


def test_concurrent_schedule_launch_has_one_active_occurrence(coordinator):
    schedule = coordinator.schedule_save({"name": "Concurrent", "prompt": "Check once", "recurrence": "daily"})
    entered = threading.Event()
    release = threading.Event()
    calls = []
    actual_start = coordinator.jobs.start
    coordinator.jobs._launch = lambda run: None
    def slow_start(data):
        calls.append(data)
        entered.set()
        release.wait(3)
        return actual_start(data)
    coordinator.jobs.start = slow_start
    results = []
    first = threading.Thread(target=lambda: results.append(coordinator.schedule_run(schedule)))
    second = threading.Thread(target=lambda: results.append(coordinator.schedule_run(schedule)))
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(.1)
    release.set()
    first.join(5)
    second.join(5)
    assert len(calls) == 1
    assert len(results) == 2 and results[0]["id"] == results[1]["id"]
    assert len(coordinator.store.runs()) == 1


def test_schedule_policy_cannot_expand_global_permissions(coordinator):
    coordinator.jobs._launch = lambda run: None
    coordinator.store.update_settings({"permission_profile": "always_ask"})
    schedule = coordinator.schedule_save({"prompt": "Inspect project", "permission_profile": "full_access"})
    run = coordinator.schedule_run(schedule)
    assert coordinator.store.run(run["id"])["settings"]["permission_profile"] == "always_ask"
