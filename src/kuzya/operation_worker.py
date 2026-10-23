"""Durable operation worker and executor registration seam.

This module is the join between the durable :class:`Operation` queue and the
code that actually performs deferred platform work. It adds no HTTP endpoint
and no domain behaviour of its own: review, analysis and Git executors are
registered by the composition root (or by a later task) through
:meth:`OperationWorker.register`.

Contract
--------

* **Handlers run outside any database transaction.** :meth:`OperationWorker.step`
  claims one queued operation, then calls the registered handler with an
  :class:`OperationContext` after the claim transaction has closed. Expensive
  model or process work therefore never holds the SQLite write lock.
* **Unknown handlers fail visibly.** An operation whose ``kind`` has no
  registered executor is recorded ``failed`` with a diagnostic error. It is
  never reported as succeeded.
* **Registration is the integration seam.** A later task (review, analysis)
  registers its executor for a kind without changing the queue, the storage
  model or this dispatch loop. Registering the same kind again replaces the
  previous handler, so a task can upgrade a placeholder to a real executor.
* **Orphan recovery is explicit.** :meth:`reconcile_startup_orphans` marks
  operations left ``running`` by a terminated process as ``interrupted`` and
  never silently replays them.

Handler signature
-----------------

.. code-block:: python

    def handler(context: OperationContext) -> Mapping[str, Any] | None:
        ...

The returned mapping (or ``None``) is stored as the operation ``result`` JSON.
Raising an exception records a visible ``failed`` operation whose ``error``
carries the exception type and message.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .instance import InstanceContext
from .logging import bind_log_context
from .permissions import ExecutionPermissions
from .storage import (
    Database,
    Operation,
    OperationRepository,
    OperationStaleClaimError,
)

logger = logging.getLogger(__name__)

#: Registry keys for the deferred platform triggers.
OPERATION_KIND_REVIEW = "review"
OPERATION_KIND_ANALYSIS = "analysis"
OPERATION_KIND_GIT_COMMIT_PUSH = "git.commit_push"
OPERATION_KIND_GIT_ROLLBACK = "git.rollback"

#: Map a legacy session action name onto its durable operation kind.
SESSION_ACTION_KINDS: dict[str, str] = {
    "trigger_review": OPERATION_KIND_REVIEW,
    "trigger_analysis": OPERATION_KIND_ANALYSIS,
    "trigger_commit_push": OPERATION_KIND_GIT_COMMIT_PUSH,
    "trigger_rollback": OPERATION_KIND_GIT_ROLLBACK,
}

#: Default reason recorded when a running operation is interrupted by restart.
DEFAULT_ORPHAN_REASON = "operation was interrupted by process restart"


@dataclasses.dataclass(frozen=True)
class OperationInstanceContext:
    """Trusted, typed services shared by every executor of one instance.

    The composition root builds this once from its configuration and passes it
    to the worker. Executors receive it through :class:`OperationContext`; it is
    the stable place to extend with per-instance paths, backend settings and
    permission grants as later tasks add them. ``instance`` and
    ``permissions`` are the same objects the rest of the runtime uses, so an
    executor that resumes durable work can re-issue a scoped backend grant
    from the persisted owner identity.
    """

    database: Database
    artifact_root: Path
    runner_argv_prefix: tuple[str, ...] = ()
    default_workspace: Path | None = None
    knowledge_base: Any | None = None
    workspace_map: Mapping[str, str] = dataclasses.field(default_factory=dict)
    instance: InstanceContext | None = None
    permissions: ExecutionPermissions | None = None
    runtime_defaults: Any | None = None
    #: Optional injected analyzer runner for the ``analysis`` executor. When
    #: ``None`` the executor builds the production Pi-backed runner from this
    #: context. The runner itself stays read-only.
    analyzer_runner: Any | None = None


@dataclasses.dataclass(frozen=True)
class OperationContext:
    """Input handed to one executor for one claimed operation."""

    operation_id: str
    kind: str
    target_id: str | None
    input: Mapping[str, Any]
    attempt_count: int
    metadata: Mapping[str, Any]
    instance: OperationInstanceContext
    owner: str | None = None


@dataclasses.dataclass(frozen=True)
class OperationOutcome:
    """Result of one :meth:`OperationWorker.step` call.

    ``status`` is ``"succeeded"`` or ``"failed"``. ``operation`` is the stored
    row after the transition; ``result`` and ``error`` mirror its columns.
    """

    status: str
    operation: Operation
    result: Mapping[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0
    worker_id: str | None = None


class OperationExecutionError(Exception):
    """Raised by an executor to fail an operation with a clear diagnostic."""


#: A registered executor receives one :class:`OperationContext` and returns an
#: optional JSON-serializable result mapping.
OperationHandler = Callable[[OperationContext], "Mapping[str, Any] | None"]


def _format_error(error: BaseException) -> str:
    """Render an executor failure as a durable diagnostic string."""
    return f"{type(error).__name__}: {error}"


#: Default and maximum size of the :class:`OperationWorkerPool`.
DEFAULT_OPERATION_CONCURRENCY = 5
MAX_OPERATION_CONCURRENCY = 16


class OperationWorker:
    """Claim and execute durable operations with registered executors.

    Args:
        database: File-backed SQLite :class:`Database` shared with the API.
        instance: Trusted per-instance context passed to each executor. When
            omitted a minimal context is built from ``database`` and
            ``artifact_root``.
        artifact_root: Parent directory recorded in the default instance
            context when ``instance`` is omitted.
        poll_interval_seconds: Idle wait between polls. Non-negative.
        worker_id: Optional stable identifier used in records and logs.
        reconcile_on_start: Run orphan reconciliation in :meth:`start`. A
            pool must pass ``False`` and reconcile once for the whole pool
            instead: a per-worker pass would mark operations claimed by
            sibling workers as interrupted.
    """

    def __init__(
        self,
        database: Database,
        *,
        instance: OperationInstanceContext | None = None,
        artifact_root: str | Path | None = None,
        poll_interval_seconds: float = 0.5,
        worker_id: str | None = None,
        reconcile_on_start: bool = True,
    ) -> None:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self._database = database
        self._repository = OperationRepository(database)
        self._handlers: dict[str, OperationHandler] = {}
        if instance is not None:
            self._instance = instance
        else:
            root = (
                Path(artifact_root).expanduser()
                if artifact_root is not None
                else Path.cwd()
            )
            self._instance = OperationInstanceContext(
                database=database,
                artifact_root=root,
            )
        self._poll_interval_seconds = float(poll_interval_seconds)
        self.worker_id = worker_id or f"operation-worker-{uuid.uuid4().hex[:8]}"
        self._reconcile_on_start = reconcile_on_start

        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._reconcile_lock = threading.Lock()
        self._reconciled = False

    # -- registration ----------------------------------------------------

    def register(self, kind: str, handler: OperationHandler) -> None:
        """Register the executor for ``kind``, replacing any previous one.

        This is the seam later tasks use to attach review and analysis
        executors without touching the queue or dispatch loop.
        """
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[kind] = handler

    def registered_kinds(self) -> tuple[str, ...]:
        """Return the registered executor kinds in sorted order."""
        return tuple(sorted(self._handlers))

    @property
    def repository(self) -> OperationRepository:
        """The underlying operation repository (read-only convenience)."""
        return self._repository

    @property
    def instance(self) -> OperationInstanceContext:
        """The trusted per-instance context handed to executors."""
        return self._instance

    @property
    def thread(self) -> threading.Thread | None:
        """The background loop thread, once started."""
        return self._thread

    @property
    def is_running(self) -> bool:
        """Whether this worker's background loop thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # -- reconciliation --------------------------------------------------

    def reconcile_startup_orphans(
        self,
        *,
        reason: str = DEFAULT_ORPHAN_REASON,
    ) -> list[str]:
        """Mark orphaned ``running`` operations ``interrupted``.

        Idempotent and safe to call from several workers; an operation is
        never silently replayed.
        """
        with self._reconcile_lock:
            interrupted = self._repository.recover_orphans(reason=reason)
            self._reconciled = True
        if interrupted:
            logger.warning(
                "startup reconciliation interrupted %d operation(s)",
                len(interrupted),
                extra={
                    "event": "operation_reconciliation",
                    "reconciled_operations": len(interrupted),
                },
            )
        return interrupted

    # -- execution -------------------------------------------------------

    def step(self) -> OperationOutcome | None:
        """Claim and execute at most one queued operation.

        Returns ``None`` when nothing was queued. Any executor exception is
        recorded as a ``failed`` operation and returned, never re-raised.
        """
        operation = self._repository.claim_next(worker_id=self.worker_id)
        if operation is None:
            return None

        started = time.perf_counter()
        with bind_log_context(
            worker_id=self.worker_id,
            task_id=operation.target_id,
        ):
            logger.info(
                "operation claimed",
                extra={
                    "event": "operation_claim",
                    "operation_id": operation.id,
                    "kind": operation.kind,
                    "attempt_count": operation.attempt_count,
                },
            )
            context = OperationContext(
                operation_id=operation.id,
                kind=operation.kind,
                target_id=operation.target_id,
                input=dict(operation.input or {}),
                attempt_count=operation.attempt_count,
                metadata=dict(operation.meta or {}),
                instance=self._instance,
                owner=operation.owner,
            )
            try:
                result = self._invoke_handler(operation, context)
            except Exception as error:  # noqa: BLE001 - recorded as failure
                return self._record_failure(
                    operation, _format_error(error), started
                )

            try:
                stored = self._repository.complete(
                    operation.id,
                    operation.claim_token or "",
                    result=result,
                )
            except Exception as error:  # noqa: BLE001 - recorded as failure
                return self._record_failure(
                    operation, _format_error(error), started
                )

            duration_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                "operation succeeded duration_ms=%.3f",
                duration_ms,
                extra={
                    "event": "operation_completed",
                    "operation_id": operation.id,
                    "kind": operation.kind,
                    "duration_ms": duration_ms,
                },
            )
            return OperationOutcome(
                status="succeeded",
                operation=stored,
                result=stored.result,
                duration_ms=duration_ms,
                worker_id=self.worker_id,
            )

    def _invoke_handler(
        self, operation: Operation, context: OperationContext
    ) -> Mapping[str, Any] | None:
        """Dispatch one claimed operation to its registered executor."""
        handler = self._handlers.get(operation.kind)
        if handler is None:
            raise OperationExecutionError(
                f"no executor registered for operation kind {operation.kind!r}"
            )
        result = handler(context)
        if result is not None and not isinstance(result, Mapping):
            raise OperationExecutionError(
                "operation handler must return a mapping or None"
            )
        return result

    def _record_failure(
        self,
        operation: Operation,
        error: str,
        started: float,
    ) -> OperationOutcome:
        """Persist a failed operation without pretending success."""
        duration_ms = (time.perf_counter() - started) * 1000.0
        logger.error(
            "operation failed duration_ms=%.3f error=%s",
            duration_ms,
            error,
            extra={
                "event": "operation_failed",
                "operation_id": operation.id,
                "kind": operation.kind,
                "duration_ms": duration_ms,
                "error": error,
            },
        )
        try:
            stored = self._repository.fail(
                operation.id,
                operation.claim_token or "",
                error=error,
            )
        except OperationStaleClaimError:
            stored = self._repository.get(operation.id) or operation
        return OperationOutcome(
            status="failed",
            operation=stored,
            error=error,
            duration_ms=duration_ms,
            worker_id=self.worker_id,
        )

    # -- background loop -------------------------------------------------

    def start(self) -> None:
        """Reconcile orphans, then start the background polling loop.

        Reconciliation runs synchronously before the loop thread starts, so an
        orphaned ``running`` operation is resolved before this returns.
        Calling :meth:`start` on a running worker is a no-op.
        """
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = threading.Event()
            self._reconciled = False
            if self._reconcile_on_start:
                self.reconcile_startup_orphans()
            self._thread = threading.Thread(
                target=self.run_loop,
                args=(self._stop_event,),
                name=f"operation-worker-{self.worker_id}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Request a graceful stop after the active operation finishes."""
        event = self._stop_event
        if event is not None:
            event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)

    def run_loop(
        self, stop_event: threading.Event | None = None
    ) -> None:
        """Poll and execute operations until ``stop_event`` is set."""
        event = stop_event or self._stop_event
        if event is None:
            event = threading.Event()
            self._stop_event = event
        logger.info(
            "OperationWorker run_loop started worker_id=%s",
            self.worker_id,
            extra={"event": "operation_worker_start", "worker_id": self.worker_id},
        )
        while not event.is_set():
            try:
                outcome = self.step()
            except Exception as loop_error:  # noqa: BLE001 - loop must survive
                logger.exception(
                    "unexpected operation worker loop error: %s", loop_error
                )
                outcome = None
            if outcome is None:
                event.wait(self._poll_interval_seconds)
        logger.info(
            "OperationWorker run_loop finished worker_id=%s",
            self.worker_id,
            extra={"event": "operation_worker_stop", "worker_id": self.worker_id},
        )


class OperationWorkerPool:
    """Run several :class:`OperationWorker` loops concurrently.

    Analyzers are read-only and any number may execute at once; Git and
    review executors mutate one task workspace, and the repository claim
    keeps same-workspace operations strictly sequential — the same rule
    task execution already follows. Extra threads therefore parallelize
    only independent work (three backlog analyses run side by side, a hung
    one no longer freezes the queue).

    Orphan reconciliation runs once for the whole pool before any worker
    thread starts: a per-worker pass could mark operations claimed by
    sibling workers as interrupted.
    """

    def __init__(
        self,
        database: Database,
        *,
        concurrency: int = DEFAULT_OPERATION_CONCURRENCY,
        worker_id: str | None = None,
        **worker_kwargs: Any,
    ) -> None:
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise ValueError("concurrency must be an integer")
        if concurrency < 1 or concurrency > MAX_OPERATION_CONCURRENCY:
            raise ValueError(
                f"concurrency must be between 1 and {MAX_OPERATION_CONCURRENCY}"
            )
        self.concurrency = concurrency
        prefix = worker_id or f"operation-worker-{uuid.uuid4().hex[:8]}"
        self._workers = tuple(
            OperationWorker(
                database,
                worker_id=f"{prefix}-{index}",
                reconcile_on_start=False,
                **worker_kwargs,
            )
            for index in range(concurrency)
        )
        self._lifecycle_lock = threading.Lock()

    @property
    def workers(self) -> tuple[OperationWorker, ...]:
        """Workers owned by the pool, in index order."""
        return self._workers

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        """Started worker threads, in index order."""
        return tuple(
            worker.thread for worker in self._workers if worker.thread is not None
        )

    @property
    def is_running(self) -> bool:
        """Whether every worker loop thread is alive."""
        return all(worker.is_running for worker in self._workers)

    # -- registration ----------------------------------------------------

    def register(self, kind: str, handler: OperationHandler) -> None:
        """Register one executor on every worker of the pool."""
        for worker in self._workers:
            worker.register(kind, handler)

    def registered_kinds(self) -> tuple[str, ...]:
        """Return the registered executor kinds in sorted order."""
        return self._workers[0].registered_kinds()

    def reconcile_startup_orphans(
        self, *, reason: str = DEFAULT_ORPHAN_REASON
    ) -> list[str]:
        """Run orphan reconciliation once for the whole pool."""
        return self._workers[0].reconcile_startup_orphans(reason=reason)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Reconcile orphans once, then start every worker loop."""
        with self._lifecycle_lock:
            if self.is_running:
                return
            self.reconcile_startup_orphans()
            for worker in self._workers:
                worker.start()

    def stop(self, timeout: float | None = None) -> None:
        """Request a graceful stop of every worker after its active operation."""
        with self._lifecycle_lock:
            for worker in self._workers:
                worker.stop(timeout)


__all__ = [
    "DEFAULT_OPERATION_CONCURRENCY",
    "DEFAULT_ORPHAN_REASON",
    "MAX_OPERATION_CONCURRENCY",
    "OPERATION_KIND_ANALYSIS",
    "OPERATION_KIND_ANALYSIS",
    "OPERATION_KIND_GIT_COMMIT_PUSH",
    "OPERATION_KIND_GIT_ROLLBACK",
    "OPERATION_KIND_REVIEW",
    "SESSION_ACTION_KINDS",
    "OperationContext",
    "OperationExecutionError",
    "OperationHandler",
    "OperationInstanceContext",
    "OperationOutcome",
    "OperationWorker",
    "OperationWorkerPool",
]
