"""Durable operation queue, worker seam and HTTP contract.

Every test runs against a real migrated temporary file database, the real
:class:`OperationRepository`, the real :class:`OperationWorker` and a bounded
in-process handler. No model, network or external service is involved.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.operation_worker import (
    OPERATION_KIND_GIT_COMMIT_PUSH,
    OPERATION_KIND_REVIEW,
    OperationExecutionError,
    OperationWorker,
)
from kuzya.storage import (
    Database,
    OperationConflictError,
    OperationRepository,
    OperationStaleClaimError,
)


def _repository(migrated_db: pathlib.Path) -> tuple[Database, OperationRepository]:
    database = Database.from_path(migrated_db)
    return database, OperationRepository(database)


def _worker(
    database: Database, tmp_path: pathlib.Path, **kwargs: object
) -> OperationWorker:
    return OperationWorker(
        database, artifact_root=tmp_path / "operations", **kwargs
    )


# -- repository protocol -------------------------------------------------


def test_submit_is_idempotent_and_conflicts_on_changed_payload(
    migrated_db: pathlib.Path,
) -> None:
    database, repository = _repository(migrated_db)
    try:
        first = repository.submit(
            kind=OPERATION_KIND_REVIEW,
            target_id="task-1",
            input={"instructions": "check"},
            idempotency_key="key-1",
            metadata={"source": "test"},
        )
        assert first.created is True
        assert first.operation.status == "pending"
        assert first.operation.attempt_count == 0

        replay = repository.submit(
            kind=OPERATION_KIND_REVIEW,
            target_id="task-1",
            input={"instructions": "check"},
            idempotency_key="key-1",
            metadata={"source": "test"},
        )
        assert replay.created is False
        assert replay.operation.id == first.operation.id

        with pytest.raises(OperationConflictError) as conflict:
            repository.submit(
                kind=OPERATION_KIND_REVIEW,
                target_id="task-1",
                input={"instructions": "different"},
                idempotency_key="key-1",
            )
        assert conflict.value.existing.id == first.operation.id

        # No key means every submit is a distinct operation.
        second = repository.submit(kind=OPERATION_KIND_REVIEW, target_id="task-1")
        assert second.created is True
        assert second.operation.id != first.operation.id
    finally:
        database.dispose()


def test_claim_complete_and_stale_token_are_enforced(
    migrated_db: pathlib.Path,
) -> None:
    database, repository = _repository(migrated_db)
    try:
        operation = repository.submit(kind="review", target_id="task-1").operation
        claimed = repository.claim_next(worker_id="worker-1")
        assert claimed is not None
        assert claimed.id == operation.id
        assert claimed.status == "running"
        assert claimed.attempt_count == 1
        assert claimed.claim_token
        assert claimed.claimed_at is not None

        # A different token cannot finish the claimed operation.
        with pytest.raises(OperationStaleClaimError):
            repository.complete(claimed.id, "not-the-token", result={"ok": True})

        stored = repository.complete(
            claimed.id, claimed.claim_token or "", result={"ok": True}
        )
        assert stored.status == "succeeded"
        assert stored.result == {"ok": True}
        assert stored.finished_at is not None

        # Repeating completion with the same token returns the stored result.
        repeated = repository.complete(
            claimed.id, claimed.claim_token or "", result={"changed": True}
        )
        assert repeated.result == {"ok": True}

        assert repository.claim_next() is None
    finally:
        database.dispose()


def test_duplicate_submission_and_restart_do_not_repeat_effects(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    seen: list[str] = []
    try:
        worker = _worker(database, tmp_path)
        worker.register(
            OPERATION_KIND_REVIEW,
            lambda context: seen.append(context.operation_id) or {"ok": True},
        )
        first = repository.submit(
            kind=OPERATION_KIND_REVIEW,
            target_id="task-1",
            input={"n": 1},
            idempotency_key="dup-key",
        )
        outcome = worker.step()
        assert outcome is not None and outcome.status == "succeeded"
        assert seen == [first.operation.id]

        # An exact retry after a lost response resolves to the same operation.
        replay = repository.submit(
            kind=OPERATION_KIND_REVIEW,
            target_id="task-1",
            input={"n": 1},
            idempotency_key="dup-key",
        )
        assert replay.operation.id == first.operation.id

        # A restart must not execute the already-completed effect again.
        restarted = _worker(database, tmp_path)
        restarted.register(
            OPERATION_KIND_REVIEW,
            lambda context: seen.append("restart") or {"ok": True},
        )
        assert restarted.step() is None
        assert seen == [first.operation.id]
    finally:
        database.dispose()


# -- worker truthfulness -------------------------------------------------


def test_unknown_kind_is_visibly_failed(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    try:
        operation = repository.submit(
            kind=OPERATION_KIND_REVIEW, target_id="task-1"
        ).operation
        worker = _worker(database, tmp_path)
        outcome = worker.step()
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.result is None
        stored = repository.get(operation.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.result is None
        assert stored.error is not None
        assert "no executor registered" in stored.error
    finally:
        database.dispose()


def test_handler_failure_is_recorded_and_never_reports_success(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    try:
        operation = repository.submit(kind="analysis", target_id="task-9").operation

        def boom(context: object) -> dict[str, object]:
            raise RuntimeError("analyzer exploded")

        worker = _worker(database, tmp_path)
        worker.register("analysis", boom)
        outcome = worker.step()
        assert outcome is not None
        assert outcome.status == "failed"
        stored = repository.get(operation.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.result is None
        assert "RuntimeError: analyzer exploded" in (stored.error or "")
        assert stored.finished_at is not None
    finally:
        database.dispose()


def test_operation_execution_error_is_reported(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    try:
        submitted = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH, target_id="missing"
        ).operation

        def missing(context: object) -> dict[str, object]:
            raise OperationExecutionError("task not found: missing")

        worker = _worker(database, tmp_path)
        worker.register(OPERATION_KIND_GIT_COMMIT_PUSH, missing)
        worker.step()
        stored = repository.get(submitted.id)
        assert stored is not None
        assert stored.status == "failed"
        assert "task not found: missing" in (stored.error or "")
    finally:
        database.dispose()


def test_registered_handler_outcome_is_stored(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    try:
        operation = repository.submit(
            kind=OPERATION_KIND_REVIEW,
            target_id="task-7",
            input={"instructions": "focus"},
        ).operation
        captured: dict[str, object] = {}

        def handler(context: object) -> dict[str, object]:
            captured["operation_id"] = context.operation_id  # type: ignore[attr-defined]
            captured["target_id"] = context.target_id  # type: ignore[attr-defined]
            captured["input"] = dict(context.input)  # type: ignore[attr-defined]
            return {"verdict": "approved", "findings": []}

        worker = _worker(database, tmp_path)
        worker.register(OPERATION_KIND_REVIEW, handler)
        assert worker.registered_kinds() == (OPERATION_KIND_REVIEW,)
        outcome = worker.step()
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert captured == {
            "operation_id": operation.id,
            "target_id": "task-7",
            "input": {"instructions": "focus"},
        }
        stored = repository.get(operation.id)
        assert stored is not None
        assert stored.result == {"verdict": "approved", "findings": []}
        # A domain verdict is stored inside the result, never as the status.
        assert stored.status == "succeeded"
    finally:
        database.dispose()


# -- orphan recovery -----------------------------------------------------


def test_orphan_recovery_interrupts_and_never_replays(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    try:
        operation = repository.submit(kind="review", target_id="task-1").operation
        claimed = repository.claim_next(worker_id="worker-1")
        assert claimed is not None and claimed.status == "running"

        interrupted = repository.recover_orphans(reason="process restart")
        assert interrupted == [operation.id]
        stored = repository.get(operation.id)
        assert stored is not None
        assert stored.status == "interrupted"
        assert stored.error == "process restart"
        assert stored.claim_token is None
        # Idempotent and never automatic.
        assert repository.recover_orphans() == []

        worker = _worker(database, tmp_path)
        worker.register("review", lambda context: {"ok": True})
        assert worker.step() is None

        # Recovery is explicit: the caller opts in to a retry.
        repository.requeue_interrupted(operation.id)
        outcome = worker.step()
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        database.dispose()


def test_worker_start_reconciles_orphans(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database, repository = _repository(migrated_db)
    try:
        operation = repository.submit(kind="review", target_id="task-1").operation
        repository.claim_next(worker_id="dead-worker")
        worker = _worker(database, tmp_path)
        worker.register("review", lambda context: {"ok": True})
        # Reconciliation runs synchronously before the loop thread starts.
        worker.reconcile_startup_orphans(reason="crash")
        stored = repository.get(operation.id)
        assert stored is not None and stored.status == "interrupted"

        worker.start()
        try:
            assert repository.get(operation.id).status == "interrupted"
        finally:
            worker.stop(timeout=5.0)
    finally:
        database.dispose()


# -- HTTP contract -------------------------------------------------------


def test_api_submit_inspect_and_conflict(migrated_db: pathlib.Path) -> None:
    with TestClient(create_app(f"sqlite:///{migrated_db}")) as client:
        submitted = client.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": "task-1",
                "input": {"instructions": "check"},
                "idempotency_key": "api-key",
            },
        )
        assert submitted.status_code == 202, submitted.text
        body = submitted.json()
        assert body["status"] == "pending"
        assert body["kind"] == "review"
        assert body["result"] is None
        assert body["error"] is None
        assert body["attempt_count"] == 0
        operation_id = body["id"]

        replayed = client.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": "task-1",
                "input": {"instructions": "check"},
                "idempotency_key": "api-key",
            },
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["id"] == operation_id

        conflict = client.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": "task-1",
                "input": {"instructions": "changed"},
                "idempotency_key": "api-key",
            },
        )
        assert conflict.status_code == 409

        inspected = client.get(f"/operations/{operation_id}")
        assert inspected.status_code == 200
        assert inspected.json()["id"] == operation_id

        listed = client.get("/operations", params={"status": "pending"})
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [operation_id]

        missing = client.get("/operations/does-not-exist")
        assert missing.status_code == 404


def test_api_reports_failed_operation_and_not_success(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        with TestClient(create_app(f"sqlite:///{migrated_db}")) as client:
            submitted = client.post(
                "/operations",
                json={"kind": "analysis", "target_id": "task-2"},
            )
            assert submitted.status_code == 202
            operation_id = submitted.json()["id"]

            worker = _worker(database, tmp_path)
            worker.register("analysis", _raise_analysis_error)
            outcome = worker.step()
            assert outcome is not None and outcome.status == "failed"

            inspected = client.get(f"/operations/{operation_id}")
            assert inspected.status_code == 200
            body = inspected.json()
            assert body["status"] == "failed"
            assert body["result"] is None
            assert "RuntimeError: analysis failed" in body["error"]
    finally:
        database.dispose()


def _raise_analysis_error(context: object) -> dict[str, object]:
    raise RuntimeError("analysis failed")


# -- workspace serialization and the worker pool -------------------------


def _task(database: Database, workspace: str | None) -> str:
    from kuzya.storage import TaskRepository

    return TaskRepository(database).create(
        title="t", workspace=workspace
    ).id


def test_claim_serializes_mutating_operations_per_workspace(
    migrated_db: pathlib.Path,
) -> None:
    database, repository = _repository(migrated_db)
    try:
        first = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/shared"),
        )
        second = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/shared"),
        )

        claimed = repository.claim_next(worker_id="w1")
        assert claimed is not None and claimed.id == first.operation.id

        # Same workspace is busy: nothing else is claimable.
        assert repository.claim_next(worker_id="w2") is None

        repository.complete(claimed.id, claimed.claim_token or "")
        after = repository.claim_next(worker_id="w2")
        assert after is not None and after.id == second.operation.id
    finally:
        database.dispose()


def test_claim_keeps_read_only_analyses_parallel(
    migrated_db: pathlib.Path,
) -> None:
    database, repository = _repository(migrated_db)
    try:
        task_id = _task(database, "/ws/analyzer-target")
        repository.submit(kind="analysis", target_id=task_id)
        repository.submit(kind="analysis", target_id=task_id)

        first = repository.claim_next(worker_id="w1")
        second = repository.claim_next(worker_id="w2")
        assert first is not None and second is not None
        assert first.id != second.id
    finally:
        database.dispose()


def test_claim_skips_a_busy_workspace_without_blocking_the_queue(
    migrated_db: pathlib.Path,
) -> None:
    database, repository = _repository(migrated_db)
    try:
        running = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/a"),
        )
        blocked = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/a"),
        )
        free = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/b"),
        )

        claimed = repository.claim_next(worker_id="w1")
        assert claimed is not None and claimed.id == running.operation.id

        # The blocked operation is older, but the free workspace wins.
        skipped_to = repository.claim_next(worker_id="w2")
        assert skipped_to is not None and skipped_to.id == free.operation.id
        assert blocked.operation.id not in {
            claimed.id,
            skipped_to.id,
        }
    finally:
        database.dispose()


def test_pool_runs_two_analyses_concurrently(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    import threading

    from kuzya.operation_worker import OperationWorkerPool

    database = Database.from_path(migrated_db)
    try:
        pool = OperationWorkerPool(
            database, concurrency=2, artifact_root=tmp_path / "operations"
        )
        barrier = threading.Barrier(2, timeout=10)

        def handler(context: object) -> dict[str, object]:
            # Only passes when both pool workers execute at once; a
            # serialized claim would leave one side waiting forever.
            barrier.wait()
            return {"ok": True}

        pool.register("analysis", handler)
        task_id = _task(database, "/ws/analyzer-target")
        repository = OperationRepository(database)
        submitted = [
            repository.submit(kind="analysis", target_id=task_id)
            for _ in range(2)
        ]

        outcomes: list[str] = []

        def run_step(worker: object) -> None:
            outcome = worker.step()
            outcomes.append("none" if outcome is None else outcome.status)

        threads = [
            threading.Thread(target=run_step, args=(worker,))
            for worker in pool.workers
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert sorted(outcomes) == ["succeeded", "succeeded"]
        statuses = {
            repository.get(submitted[i].operation.id).status for i in range(2)
        }
        assert statuses == {"succeeded"}
    finally:
        database.dispose()


def test_pool_reconciles_orphans_once_and_not_per_worker(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    from kuzya.operation_worker import OperationWorkerPool

    database = Database.from_path(migrated_db)
    try:
        repository = OperationRepository(database)
        submitted = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/a"),
        )
        orphan = repository.claim_next(worker_id="dead-process")
        assert orphan is not None

        pool = OperationWorkerPool(
            database, concurrency=2, artifact_root=tmp_path / "operations"
        )
        try:
            # The pool-level reconcile marks the orphan interrupted...
            pool.reconcile_startup_orphans()
            assert (
                repository.get(submitted.operation.id).status
                == "interrupted"
            )
            # ...and a sibling worker starting later must not re-run the
            # reconciliation: a freshly claimed operation would be marked
            # interrupted by its own pool. The pool loops stay stopped so
            # this is deterministic — the worker start itself is the act
            # under test, and a wrong per-worker reconcile would fire
            # synchronously before start() returns.
            claimed = repository.submit(
                kind="analysis", target_id=_task(database, "/ws/any")
            )
            running = repository.claim_next(worker_id="pool-worker")
            assert running is not None
            pool.workers[1].start()
            try:
                assert repository.get(running.id).status == "running"
                assert claimed.created is True
            finally:
                pool.workers[1].stop(timeout=5.0)
        finally:
            pool.stop(timeout=5.0)
        assert not pool.is_running
    finally:
        database.dispose()


def test_pool_start_reconciles_orphans(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    from kuzya.operation_worker import OperationWorkerPool

    database = Database.from_path(migrated_db)
    try:
        repository = OperationRepository(database)
        submitted = repository.submit(
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=_task(database, "/ws/a"),
        )
        assert repository.claim_next(worker_id="dead-process") is not None

        pool = OperationWorkerPool(
            database, concurrency=2, artifact_root=tmp_path / "operations"
        )
        pool.start()
        try:
            # No queued operations exist, so the live loops cannot race
            # this assertion: the claim happened before start().
            assert (
                repository.get(submitted.operation.id).status
                == "interrupted"
            )
        finally:
            pool.stop(timeout=5.0)
        assert not pool.is_running
    finally:
        database.dispose()


def test_pool_rejects_out_of_range_concurrency(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    from kuzya.operation_worker import MAX_OPERATION_CONCURRENCY, OperationWorkerPool

    database = Database.from_path(migrated_db)
    try:
        for bad in (0, -1, MAX_OPERATION_CONCURRENCY + 1, True):
            with pytest.raises(ValueError):
                OperationWorkerPool(
                    database,
                    concurrency=bad,
                    artifact_root=tmp_path / "operations",
                )
    finally:
        database.dispose()


# -- commit gate: requires_review must be honored ------------------------


def _commit_context(
    database: Database, tmp_path: pathlib.Path, task_id: str, payload: dict
) -> object:
    from kuzya.operation_worker import OperationContext, OperationInstanceContext

    return OperationContext(
        operation_id="op-gate",
        kind=OPERATION_KIND_GIT_COMMIT_PUSH,
        target_id=task_id,
        input=payload,
        attempt_count=1,
        metadata={},
        instance=OperationInstanceContext(
            database=database, artifact_root=tmp_path / "operations"
        ),
    )


def test_commit_operation_blocked_without_approved_review(
    migrated_db: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from kuzya.operation_handlers import git_commit_push_handler
    from kuzya.operation_worker import OperationExecutionError
    from kuzya.storage import TaskRepository

    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        task_id = tasks.create(
            title="gated",
            workspace="/ws/gate",
            metadata={"requires_review": True, "changed_files": []},
        ).id

        # No approved verdict: the operation fails before any git work runs
        # and the task is not done.
        with pytest.raises(OperationExecutionError, match="requires review"):
            git_commit_push_handler(
                _commit_context(database, tmp_path, task_id, {})
            )
        assert tasks.get(task_id).status != "done"

        engine = MagicMock()
        engine.commit_and_push.return_value = MagicMock(
            success=True,
            action="commit_and_push",
            message="ok",
            commit_hash="abc123",
            branch="main",
            ci_status=None,
        )
        monkeypatch.setattr(
            "kuzya.operation_handlers.GitEngine",
            MagicMock(return_value=engine),
        )

        # force=true is the explicit deliberate override.
        outcome = git_commit_push_handler(
            _commit_context(database, tmp_path, task_id, {"force": True})
        )
        assert outcome["commit_hash"] == "abc123"

        # An approved verdict opens the gate on a fresh task.
        approved_id = tasks.create(
            title="approved",
            workspace="/ws/gate",
            metadata={"requires_review": True},
        ).id
        tasks.update(
            approved_id,
            metadata={"review": {"approved": True, "round": 1}},
        )
        git_commit_push_handler(
            _commit_context(database, tmp_path, approved_id, {})
        )
        assert tasks.get(approved_id).status == "done"
    finally:
        database.dispose()


def test_git_and_review_operations_inherit_execution_workspace(
    migrated_db: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task without an explicit workspace runs Git in the directory its
    execution actually used — never in the instance default repository."""
    from unittest.mock import MagicMock

    from kuzya.operation_handlers import git_commit_push_handler
    from kuzya.storage import TaskRepository

    database = Database.from_path(migrated_db)
    try:
        task_id = TaskRepository(database).create(
            title="no explicit workspace",
            workspace=None,
            metadata={
                "changed_files": ["bot/a.py"],
                "execution_context": {
                    "host_path": "/ws/agent-project",
                },
            },
        ).id

        engine = MagicMock()
        engine.commit_local.return_value = MagicMock(
            success=True,
            action="commit_local",
            message="ok",
            commit_hash="abc",
            branch="main",
            ci_status=None,
        )
        monkeypatch.setattr(
            "kuzya.operation_handlers.GitEngine",
            MagicMock(return_value=engine),
        )

        from kuzya.operation_worker import (
            OperationContext,
            OperationInstanceContext,
        )

        context = OperationContext(
            operation_id="op-ws",
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=task_id,
            input={"push": False},
            attempt_count=1,
            metadata={},
            instance=OperationInstanceContext(
                database=database,
                artifact_root=tmp_path / "operations",
                default_workspace=pathlib.Path("/ws/instance-default"),
            ),
        )
        outcome = git_commit_push_handler(context)
        assert outcome["commit_hash"] == "abc"
        stored_meta = TaskRepository(database).get(task_id).meta
        git_outcome = stored_meta.get("git_outcome")
        assert git_outcome["commit_hash"] == "abc"
        assert git_outcome["source"] == "operation"
        assert git_outcome["pushed"] is False
        # The engine ran in the execution directory, not the default.
        engine.commit_local.assert_called_once()
        assert (
            engine.commit_local.call_args.kwargs.get("workspace")
            == "/ws/agent-project"
        )
    finally:
        database.dispose()
