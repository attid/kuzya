"""Concurrency tests for the background task execution worker pool."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from kuzya.runner import RunnerConfig
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.storage import AgentProfileRepository, Database, TaskRepository
from kuzya.task_worker import (
    MAX_TASK_CONCURRENCY,
    TaskExecutionWorkerPool,
)

_DEVELOPER_AGENT_ID = "kuzya-developer"


def _create_database(migrated_db: Path) -> Database:
    database = Database.from_path(migrated_db)
    AgentProfileRepository(database).create(
        agent_id=_DEVELOPER_AGENT_ID,
        name="Developer",
        role="developer",
        system_prompt="Developer prompt.",
        runner_config={"tools": []},
    )
    return database


def _task_id_from_cmd(cmd: list[str]) -> str:
    """Recover the task id from the generated ``task_<id>_<suffix>`` directory."""
    task_file = Path(cmd[cmd.index("--task") + 1])
    exec_dir_name = task_file.parent.name
    assert exec_dir_name.startswith("task_")
    return exec_dir_name.removeprefix("task_").rsplit("_", 1)[0]


def _runner_success() -> MagicMock:
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"status": "completed", "changed_files": []}),
        stderr="",
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before the timeout")


def _build_pool(
    database: Database,
    tmp_path: Path,
    *,
    concurrency: int,
    workspace_map: dict[str, str],
    on_task_finished: Callable[[Any, Any], None],
    default_workspace: str | None = None,
) -> TaskExecutionWorkerPool:
    return TaskExecutionWorkerPool(
        database=database,
        artifact_root=tmp_path / "artifacts",
        concurrency=concurrency,
        agent_id=_DEVELOPER_AGENT_ID,
        workspace_map=workspace_map,
        default_workspace=default_workspace,
        poll_interval_seconds=0.01,
        on_task_finished=on_task_finished,
        runtime_defaults=RuntimeDefaultsResolver(
            global_settings_path=tmp_path / "absent_settings.json"
        ),
    )


def test_pool_executes_different_workspaces_in_parallel(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Two isolated workspaces must overlap instead of waiting in one FIFO."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    first = repository.create(
        title="Workspace A task",
        status="in_progress",
        workspace="workspace_a",
    )
    second = repository.create(
        title="Workspace B task",
        status="in_progress",
        workspace="workspace_b",
    )
    for name in ("workspace_a", "workspace_b"):
        (tmp_path / name).mkdir()

    barrier = threading.Barrier(2, timeout=10)
    state_lock = threading.Lock()
    started: list[str] = []
    outcomes: dict[str, Any] = {}

    def fake_run(cmd: list[str], **_kwargs: Any) -> MagicMock:
        task_id = _task_id_from_cmd(cmd)
        with state_lock:
            started.append(task_id)
        # Both tasks can leave this barrier only when the pool really runs
        # two executions at the same time.
        barrier.wait(timeout=10)
        return _runner_success()

    def on_finished(task: Any, outcome: Any) -> None:
        with state_lock:
            outcomes[task.id] = outcome

    pool = _build_pool(
        database,
        tmp_path,
        concurrency=2,
        workspace_map={
            "workspace_a": str(tmp_path / "workspace_a"),
            "workspace_b": str(tmp_path / "workspace_b"),
        },
        on_task_finished=on_finished,
    )
    assert [worker.worker_id for worker in pool.workers] == [
        f"{pool.worker_id}-0",
        f"{pool.worker_id}-1",
    ]
    try:
        with patch(
            "kuzya.task_worker.TaskProcessRegistry.run_process",
            side_effect=lambda task_id, cmd, **kwargs: (fake_run(cmd, **kwargs), False),
        ):
            pool.start()
            _wait_for(lambda: len(outcomes) == 2)
    finally:
        pool.stop(timeout=5.0)
        database.dispose()

    assert set(started) == {first.id, second.id}
    assert {outcome.status for outcome in outcomes.values()} == {"completed"}
    assert len({outcome.worker_id for outcome in outcomes.values()}) == 2
    assert repository.get(first.id).status == "done"
    assert repository.get(second.id).status == "done"


def test_pool_executes_same_workspace_tasks_sequentially(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Workspace isolation must prevent overlapping tasks in one workspace."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    first = repository.create(
        title="First task",
        status="in_progress",
        workspace="shared",
    )
    second = repository.create(
        title="Second task",
        status="in_progress",
        workspace="shared",
    )
    (tmp_path / "shared").mkdir()

    state_lock = threading.Lock()
    outcomes: dict[str, Any] = {}
    in_flight = 0
    max_in_flight = 0

    def fake_run(cmd: list[str], **_kwargs: Any) -> MagicMock:
        nonlocal in_flight, max_in_flight
        _task_id_from_cmd(cmd)
        with state_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            # A real execution takes long enough for a second worker to try
            # to claim another task in the same workspace.
            time.sleep(0.15)
        finally:
            with state_lock:
                in_flight -= 1
        return _runner_success()

    def on_finished(task: Any, outcome: Any) -> None:
        with state_lock:
            outcomes[task.id] = outcome

    pool = _build_pool(
        database,
        tmp_path,
        concurrency=2,
        workspace_map={"shared": str(tmp_path / "shared")},
        on_task_finished=on_finished,
    )
    try:
        with patch(
            "kuzya.task_worker.TaskProcessRegistry.run_process",
            side_effect=lambda task_id, cmd, **kwargs: (fake_run(cmd, **kwargs), False),
        ):
            pool.start()
            _wait_for(lambda: len(outcomes) == 2)
    finally:
        pool.stop(timeout=5.0)
        database.dispose()

    assert max_in_flight == 1
    assert set(outcomes) == {first.id, second.id}
    assert repository.get(first.id).status == "done"
    assert repository.get(second.id).status == "done"


def test_pool_executes_null_workspace_tasks_sequentially(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Tasks without workspace share the default directory, so they serialize."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    first = repository.create(title="First default task", status="in_progress")
    second = repository.create(title="Second default task", status="in_progress")
    default_workspace = tmp_path / "default_workspace"
    default_workspace.mkdir()

    state_lock = threading.Lock()
    outcomes: dict[str, Any] = {}
    in_flight = 0
    max_in_flight = 0
    observed_cwds: set[str] = set()

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        nonlocal in_flight, max_in_flight
        _task_id_from_cmd(cmd)
        with state_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            observed_cwds.add(str(kwargs.get("cwd")))
        try:
            time.sleep(0.15)
        finally:
            with state_lock:
                in_flight -= 1
        return _runner_success()

    def on_finished(task: Any, outcome: Any) -> None:
        with state_lock:
            outcomes[task.id] = outcome

    pool = _build_pool(
        database,
        tmp_path,
        concurrency=2,
        workspace_map={},
        default_workspace=str(default_workspace),
        on_task_finished=on_finished,
    )
    try:
        with patch(
            "kuzya.task_worker.TaskProcessRegistry.run_process",
            side_effect=lambda task_id, cmd, **kwargs: (fake_run(cmd, **kwargs), False),
        ):
            pool.start()
            _wait_for(lambda: len(outcomes) == 2)
    finally:
        pool.stop(timeout=5.0)
        database.dispose()

    assert max_in_flight == 1
    assert observed_cwds == {str(default_workspace.resolve())}
    assert set(outcomes) == {first.id, second.id}
    assert repository.get(first.id).status == "done"
    assert repository.get(second.id).status == "done"


def test_claim_in_progress_has_no_races_between_threads(
    migrated_db: Path,
) -> None:
    """Concurrent repositories must claim every task exactly once."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    tasks = [
        repository.create(
            title=f"Task {index}",
            status="in_progress",
            workspace=f"workspace-{index}",
        )
        for index in range(40)
    ]
    workers = 8
    start = threading.Barrier(workers)
    claims: list[str] = []
    claims_lock = threading.Lock()

    def claim_until_empty(index: int) -> None:
        thread_repository = TaskRepository(database)
        start.wait(timeout=10)
        local_claims: list[str] = []
        while True:
            claimed = thread_repository.claim_in_progress(
                worker_id=f"claim-worker-{index}"
            )
            if claimed is None:
                break
            local_claims.append(claimed.id)
        with claims_lock:
            claims.extend(local_claims)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(claim_until_empty, range(workers)))

        expected_ids = {task.id for task in tasks}
        assert len(claims) == len(tasks)
        assert set(claims) == expected_ids
        for task in tasks:
            reloaded = repository.get(task.id)
            assert reloaded.meta["execution_status"] == "running"
            assert reloaded.meta["execution_worker_id"].startswith("claim-worker-")
    finally:
        database.dispose()


def test_same_workspace_claim_is_exclusive_between_threads(
    migrated_db: Path,
) -> None:
    """Only one of two simultaneous claims may take a task from one workspace."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    repository.create(title="First", status="in_progress", workspace="shared")
    repository.create(title="Second", status="in_progress", workspace="shared")
    start = threading.Barrier(2)

    def attempt(index: int) -> Any:
        thread_repository = TaskRepository(database)
        start.wait(timeout=10)
        return thread_repository.claim_in_progress(
            worker_id=f"same-workspace-worker-{index}"
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(attempt, range(2)))
        assert sum(claim is not None for claim in claims) == 1
    finally:
        database.dispose()


def test_null_workspace_claim_is_exclusive_between_threads(
    migrated_db: Path,
) -> None:
    """NULL workspace is one serialized group, not a claim wildcard."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    repository.create(title="First default", status="in_progress")
    repository.create(title="Second default", status="in_progress")
    start = threading.Barrier(2)

    def attempt(index: int) -> Any:
        thread_repository = TaskRepository(database)
        start.wait(timeout=10)
        return thread_repository.claim_in_progress(
            worker_id=f"null-workspace-worker-{index}"
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(attempt, range(2)))
        assert sum(claim is not None for claim in claims) == 1
    finally:
        database.dispose()


def test_pool_stop_waits_for_active_execution(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Graceful shutdown must not abandon a task that is already running."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    task = repository.create(
        title="Long task",
        status="in_progress",
        workspace="workspace",
    )
    (tmp_path / "workspace").mkdir()

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def fake_run(_cmd: list[str], **_kwargs: Any) -> MagicMock:
        started.set()
        release.wait(timeout=10)
        finished.set()
        return _runner_success()

    pool = _build_pool(
        database,
        tmp_path,
        concurrency=1,
        workspace_map={"workspace": str(tmp_path / "workspace")},
        on_task_finished=lambda _task, _outcome: None,
    )
    stopper = threading.Thread(target=lambda: pool.stop(timeout=5.0), daemon=True)
    try:
        with patch(
            "kuzya.task_worker.TaskProcessRegistry.run_process",
            side_effect=lambda task_id, cmd, **kwargs: (fake_run(cmd, **kwargs), False),
        ):
            pool.start()
            assert started.wait(timeout=5.0)
            stopper.start()
            time.sleep(0.15)
            assert stopper.is_alive(), "stop() returned while a task was still active"
            release.set()
            stopper.join(timeout=5.0)
            assert not stopper.is_alive()
            assert finished.is_set()
    finally:
        release.set()
        pool.stop(timeout=5.0)
        database.dispose()

    assert repository.get(task.id).status == "done"
    assert not pool.is_running


def test_pool_stop_respects_timeout(
    migrated_db: Path, tmp_path: Path
) -> None:
    """A slow execution must not make shutdown wait forever."""
    database = _create_database(migrated_db)
    repository = TaskRepository(database)
    task = repository.create(
        title="Slow task",
        status="in_progress",
        workspace="workspace",
    )
    (tmp_path / "workspace").mkdir()

    started = threading.Event()
    release = threading.Event()

    def fake_run(_cmd: list[str], **_kwargs: Any) -> MagicMock:
        started.set()
        release.wait(timeout=10)
        return _runner_success()

    pool = _build_pool(
        database,
        tmp_path,
        concurrency=1,
        workspace_map={"workspace": str(tmp_path / "workspace")},
        on_task_finished=lambda _task, _outcome: None,
    )
    try:
        with patch(
            "kuzya.task_worker.TaskProcessRegistry.run_process",
            side_effect=lambda task_id, cmd, **kwargs: (fake_run(cmd, **kwargs), False),
        ):
            pool.start()
            assert started.wait(timeout=5.0)
            began = time.monotonic()
            pool.stop(timeout=0.05)
            elapsed = time.monotonic() - began
            assert elapsed < 2.0
            assert pool.workers[0].is_running

            release.set()
            pool.stop(timeout=5.0)
    finally:
        release.set()
        pool.stop(timeout=5.0)
        database.dispose()

    assert repository.get(task.id).status == "done"
    assert not pool.is_running


def test_pool_lifecycle_is_idempotent(migrated_db: Path, tmp_path: Path) -> None:
    """Repeated start/stop calls and a restart keep a complete worker pool."""
    database = _create_database(migrated_db)
    pool = _build_pool(
        database,
        tmp_path,
        concurrency=2,
        workspace_map={},
        on_task_finished=lambda _task, _outcome: None,
    )
    try:
        pool.start()
        first_threads = pool.threads
        assert pool.state == "running"
        assert len(first_threads) == 2
        assert all(thread.is_alive() for thread in first_threads)

        pool.start()
        assert pool.threads == first_threads

        pool.stop(timeout=2.0)
        assert pool.state == "stopped"
        assert not any(thread.is_alive() for thread in pool.threads)

        pool.stop(timeout=2.0)
        assert not pool.is_running

        pool.start()
        assert pool.state == "running"
        assert len(pool.threads) == 2
        assert all(thread.is_alive() for thread in pool.threads)
    finally:
        pool.stop(timeout=5.0)
        database.dispose()


@pytest.mark.parametrize("concurrency", [0, -1, MAX_TASK_CONCURRENCY + 1, "3", True])
def test_pool_rejects_invalid_concurrency(
    tmp_path: Path, concurrency: Any
) -> None:
    database = Database.from_path(tmp_path / "unused.db")
    try:
        with pytest.raises(ValueError, match="concurrency"):
            TaskExecutionWorkerPool(
                database=database,
                artifact_root=tmp_path / "artifacts",
                concurrency=concurrency,
            )
    finally:
        database.dispose()


def test_runner_config_reads_task_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUZYA_TASK_CONCURRENCY", " 5 ")
    assert RunnerConfig.from_env().task_concurrency == 5


@pytest.mark.parametrize("value", ["", "abc", "0", "-1", str(MAX_TASK_CONCURRENCY + 1)])
def test_runner_config_rejects_invalid_task_concurrency(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KUZYA_TASK_CONCURRENCY", value)
    with pytest.raises(ValueError, match="between 1 and 32"):
        RunnerConfig.from_env()
