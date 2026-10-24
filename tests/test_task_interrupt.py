"""Manual task interruption: process signalling, API contract, worker outcome.

The registry is exercised against real child processes (a ``sleep``), the API
against the real middleware and repository, and the worker against a fake
runner script that hangs until interrupted — the exact production failure
mode the interrupt exists for.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.permissions import ExecutionPermissions
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.storage import AgentProfileRepository, Database, TaskRepository
from kuzya.task_worker import TaskExecutionWorker, TaskProcessRegistry

ADMIN_TOKEN = "administrative-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before the timeout")


def test_registry_interrupts_a_running_process(tmp_path: Path) -> None:
    registry = TaskProcessRegistry(term_grace_seconds=0.2)
    done: dict[str, Any] = {}

    def _run() -> None:
        completed, interrupted = registry.run_process(
            "task-1",
            ["/bin/sh", "-c", "sleep 30"],
            cwd=str(tmp_path),
            env=None,
        )
        done["returncode"] = completed.returncode
        done["interrupted"] = interrupted

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        _wait_for(lambda: "task-1" in registry.running_task_ids())
        assert registry.request_interrupt("task-1") is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert done["interrupted"] is True
    assert done["returncode"] != 0
    # The entry is gone with the process; a second request reports miss.
    assert registry.request_interrupt("task-1") is False


def _mark_running(database: Database, title: str) -> str:
    repository = TaskRepository(database)
    task = repository.create(title=title, status="in_progress")
    repository.update(
        task.id,
        metadata={**(task.meta or {}), "execution_status": "running"},
    )
    return task.id


def test_interrupt_endpoint_contract(migrated_db: Path) -> None:
    database = Database.from_path(migrated_db)
    permissions = ExecutionPermissions()
    registry = TaskProcessRegistry(term_grace_seconds=0.2)
    try:
        app = create_app(
            f"sqlite:///{migrated_db}",
            api_tokens={ADMIN_TOKEN},
            permissions=permissions,
            task_process_registry=registry,
        )
        with TestClient(app) as client:
            unknown = client.post("/tasks/nope/interrupt", headers=ADMIN_HEADERS)
            assert unknown.status_code == 404

            idle = TaskRepository(database).create(title="idle")
            not_running = client.post(
                f"/tasks/{idle.id}/interrupt", headers=ADMIN_HEADERS
            )
            assert not_running.status_code == 409

            # execution_status is running but no live handle exists: the
            # stop marker is still recorded and reported honestly.
            task_id = _mark_running(database, "running")
            response = client.post(
                f"/tasks/{task_id}/interrupt", headers=ADMIN_HEADERS
            )
            assert response.status_code == 202
            body = response.json()
            assert body["status"] == "stop_requested"
            assert body["process_signalled"] is False

            stored = TaskRepository(database).get(task_id)
            assert stored is not None
            assert stored.meta.get("execution_stop_requested_at")
            # The running marker survives the stop request.
            assert stored.meta.get("execution_status") == "running"
    finally:
        database.dispose()


def test_interrupt_signals_a_live_registry_process(
    migrated_db: Path, tmp_path: Path
) -> None:
    database = Database.from_path(migrated_db)
    permissions = ExecutionPermissions()
    live_id = _mark_running(database, "live")
    registry = TaskProcessRegistry(term_grace_seconds=0.2)
    sleeper = threading.Thread(
        target=registry.run_process,
        args=(live_id, ["/bin/sh", "-c", "sleep 30"]),
        kwargs={"cwd": str(tmp_path), "env": None},
        daemon=True,
    )
    sleeper.start()
    try:
        _wait_for(lambda: live_id in registry.running_task_ids())
        app = create_app(
            f"sqlite:///{migrated_db}",
            api_tokens={ADMIN_TOKEN},
            permissions=permissions,
            task_process_registry=registry,
        )
        with TestClient(app) as client:
            response = client.post(
                f"/tasks/{live_id}/interrupt", headers=ADMIN_HEADERS
            )
            assert response.status_code == 202
            assert response.json()["process_signalled"] is True
        sleeper.join(timeout=10)
        assert not sleeper.is_alive()
    finally:
        database.dispose()


def test_interrupt_is_task_scoped_for_execution_tokens(
    migrated_db: Path,
) -> None:
    database = Database.from_path(migrated_db)
    permissions = ExecutionPermissions()
    mine = _mark_running(database, "mine")
    foreign = _mark_running(database, "foreign")
    assert mine != foreign
    grant = permissions.issue(
        agent_id="dev",
        actions=frozenset({"tasks.write"}),
        resources=frozenset({f"task:{mine}"}),
        label=f"task:{mine}",
    )
    headers = {"Authorization": f"Bearer {grant.token}"}
    try:
        app = create_app(
            f"sqlite:///{migrated_db}",
            api_tokens={ADMIN_TOKEN},
            permissions=permissions,
        )
        with TestClient(app) as client:
            allowed = client.post(
                f"/tasks/{mine}/interrupt", headers=headers
            )
            assert allowed.status_code == 202
            denied = client.post(
                f"/tasks/{foreign}/interrupt", headers=headers
            )
            assert denied.status_code == 403
    finally:
        database.dispose()


def test_worker_records_waiting_for_input_after_manual_interrupt(
    migrated_db: Path, tmp_path: Path
) -> None:
    database = Database.from_path(migrated_db)
    AgentProfileRepository(database).create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Developer prompt.",
        runner_config={"tools": []},
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_runner = tmp_path / "fake_runner.sh"
    fake_runner.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_runner.chmod(0o755)

    registry = TaskProcessRegistry(term_grace_seconds=0.2)
    worker = TaskExecutionWorker(
        database,
        tmp_path / "artifacts",
        agent_id="kuzya-developer",
        runner_argv_prefix=["/bin/sh", str(fake_runner)],
        workspace_map={},
        default_workspace=str(workspace),
        poll_interval_seconds=0.01,
        runtime_defaults=RuntimeDefaultsResolver(
            global_settings_path=tmp_path / "absent_settings.json"
        ),
        process_registry=registry,
    )
    task = TaskRepository(database).create(
        title="hangs forever", status="in_progress", workspace=str(workspace)
    )
    outcomes: list[Any] = []

    def _step() -> None:
        outcomes.append(worker.step())

    thread = threading.Thread(target=_step)
    thread.start()
    try:
        _wait_for(lambda: task.id in registry.running_task_ids())
        assert registry.request_interrupt(task.id) is True
    finally:
        thread.join(timeout=15)
    assert not thread.is_alive()

    assert outcomes and outcomes[0].status == "waiting_for_input"
    stored = TaskRepository(database).get(task.id)
    assert stored is not None
    assert stored.status == "waiting_for_input"
    provenance = stored.meta.get("provenance") or {}
    assert provenance.get("interrupted") is True
    clarification = provenance.get("clarification") or {}
    assert clarification.get("title") == "Задача остановлена вручную"
    # A stop is not a failure: no error is recorded.
    assert not (stored.meta.get("error"))


def test_git_status_endpoint_contract(migrated_db: Path, tmp_path: Path) -> None:
    """GET /tasks/{id}/git/status: snapshot via execution workspace, honest 409."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "f.txt").write_text("dirty")

    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        task = repository.create(title="git status target", workspace=None)
        repository.update(
            task.id,
            metadata={
                "execution_context": {"host_path": str(repo)},
            },
        )
        app = create_app(
            f"sqlite:///{migrated_db}",
            api_tokens={ADMIN_TOKEN},
            permissions=ExecutionPermissions(),
        )
        with TestClient(app) as client:
            response = client.get(
                f"/tasks/{task.id}/git/status", headers=ADMIN_HEADERS
            )
            assert response.status_code == 200
            body = response.json()
            assert body["branch"] == "main"
            assert body["clean"] is False
            assert body["modified"] == ["f.txt"]
            assert body["last_commit"]["subject"] == "init"

            # No workspace anywhere: an honest conflict, not a guess.
            bare = TaskRepository(database).create(title="no ws")
            conflict = client.get(
                f"/tasks/{bare.id}/git/status", headers=ADMIN_HEADERS
            )
            assert conflict.status_code == 409

            missing = client.get(
                "/tasks/nope/git/status", headers=ADMIN_HEADERS
            )
            assert missing.status_code == 404
    finally:
        database.dispose()
