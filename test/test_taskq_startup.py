"""Task-store startup stays off-loop; requests fail closed until attachment."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable


@pytest.mark.asyncio
async def test_manager_open_is_off_loop_and_pending_spawn_is_refused(monkeypatch):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = TaskStore.open
    observed = []

    def parked_open(store):
        observed.append(TaskStore._on_running_loop_thread())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(store)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "strict_loop_guard", True)
    monkeypatch.setattr(TaskStore, "open", parked_open)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert manager._taskq is None
        for spawn in (manager.spawn, manager.spawn_async):
            result = spawn("before attachment", parent_session_key="web-1")
            info = await result if asyncio.iscoroutine(result) else result
            assert info is not None and info.done
            assert info.error_code == "task_store_unavailable"
        assert manager._running_count == 0
        assert not manager._agents
        release.set()
        await manager.wait_taskq_ready()
        assert manager._taskq is not None
        assert manager._taskq_unavailable is None
        assert manager._taskq.loop_thread_calls == 0
        assert observed == [False]
        record = manager.prepare_spawn("after attachment", parent_session_key="web-1")
        assert record is not None and hasattr(record, "record")
    finally:
        release.set()
        await manager.cancel_all()
        if manager._taskq is not None:
            await asyncio.to_thread(manager._taskq.close)


@pytest.mark.asyncio
async def test_failed_open_keeps_typed_refusal_after_startup(monkeypatch):
    def fail_open(store):
        raise TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(TaskStore, "open", fail_open)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    await manager.wait_taskq_ready()
    info = await manager.spawn_async("not accepted")
    assert manager._taskq is None
    assert info.error_code == "task_store_unavailable"
    assert "locked" in info.error
    await manager.cancel_all()


def test_manager_without_a_loop_opens_synchronously():
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert manager._taskq_init_task is None
    assert manager._taskq is not None
    assert manager._taskq.loop_thread_calls == 0
    manager._taskq.close()


def test_feature_map_covers_all_overload_routes():
    root = Path(__file__).resolve().parents[1]
    feature_map = (root / "docs/feature-map/README.md").read_text(encoding="utf-8")
    for route in (
        "GET /api/tasks",
        "GET /api/tasks/summary",
        "GET /api/tasks/{task_id}",
        "POST /api/tasks/{task_id}",
        "POST /api/tasks/{task_id}/cancel",
        "GET /api/spawn/lanes",
        "GET /api/spawn/{agent_id}/resume",
        "GET /api/sessions/health",
    ):
        assert f"`{route}`" in feature_map
    assert "`answer_input`" in feature_map and "`cancel_wait`" in feature_map


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_dependency_tick_keeps_store_off_loop_and_callbacks_on_loop(
    monkeypatch, live, expired
):
    from kiro_crew.subagent import SubagentInfo
    from kiro_crew.taskq import dependency, model

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "strict_loop_guard", True)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    await manager.wait_taskq_ready()
    store = manager._taskq
    assert store is not None
    await manager._admission.ensure_coordinator_async()
    coordinator = manager.dependency_coordinator()
    now = [coordinator.now()]
    started = now[0]
    coordinator._clock = lambda: now[0]
    seen = []
    loop_thread = threading.get_ident()
    coordinator.subscribe(
        on_wake=lambda task_id: seen.append(("wake", task_id, threading.get_ident())),
        on_fail=lambda task_id, reason: seen.append(("fail", task_id, threading.get_ident())),
    )
    monkeypatch.setattr(manager, "_drain_queue", MagicMock())
    resumes = []
    original_resume = SpawnAdmissionCoordinator.request_resume

    def request_resume(admission, info, **kwargs):
        resumes.append(threading.get_ident())
        return original_resume(admission, info, **kwargs)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "request_resume", request_resume)

    def seed():
        store.accept([model.TaskRecord(id="tick-row", kind=model.KIND_SUBAGENT)])
        claimed = store.claim("tick-row")
        assert claimed is not None
        store.transition("tick-row", model.STARTING, generation=claimed.generation)
        if live:
            store.transition("tick-row", model.RUNNING, generation=claimed.generation)
        coordinator.report(
            "tick-row",
            dependency.DependencySignal(
                dependency.KIND_RATE_LIMITED, "provider:test", "test", retry_at=started + 1.0
            ),
            generation=claimed.generation,
        )
        return claimed.generation

    try:
        generation = await store.run(seed)
        if live:
            info = SubagentInfo(id="tick-row", task="resume")
            info._slot_released = True
            info._taskq_generation = generation
            info._resume_event = asyncio.Event()
            manager._agents[info.id] = info
            assert not manager._monitor.taskq_wake_through(info.id, generation + 1)
        now[0] = started + coordinator.wait_deadline_secs + 1 if expired else started + 2.0
        manager._taskq_pump()
        tick_task = manager._taskq_tick_task
        manager._taskq_pump()
        assert manager._taskq_tick_task is tick_task
        await tick_task
        assert seen == [("fail" if expired else "wake", "tick-row", loop_thread)]
        assert resumes == ([loop_thread] if live and not expired else [])
        row = await store.run(store.get, "tick-row")
        assert row.state == (
            model.FAILED if expired else model.WAITING_DEPENDENCY if live else model.QUEUED
        )
        if not expired:
            assert row.generation == generation  # The admission pump owns the next claim.
        assert store.loop_thread_calls == 0
    finally:
        manager._agents.clear()
        timer = getattr(manager, "_taskq_pump_timer", None)
        if timer is not None:
            timer.cancel()
        await manager.cancel_all()
        await asyncio.to_thread(store.close)
