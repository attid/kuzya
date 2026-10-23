"""Durable, observable operation queue backed by SQLite.

An operation is the queue record behind every deferred platform trigger. This
module owns the persistence protocol only; dispatching a claimed operation to a
real executor belongs to :mod:`kuzya.operation_worker`.

Protocol
--------

* :meth:`OperationRepository.submit` durably records a queued operation. With
  an ``idempotency_key`` an exact retry returns the stored operation
  (``created=False``) and a reused key with a different payload raises
  :class:`OperationConflictError` instead of overwriting the original.
* :meth:`OperationRepository.claim_next` atomically moves the oldest queued
  operation to ``running`` under a fresh ``claim_token``. Two workers racing
  on the same database therefore produce exactly one owner.
* :meth:`OperationRepository.complete` and :meth:`fail` require that token, so
  a stale or crashed worker cannot finish or fail a newer claim.
* :meth:`OperationRepository.recover_orphans` marks every ``running``
  operation left by a terminated process as ``interrupted``. It never
  silently replays a consequential side effect.

Statuses are execution-protocol values. A domain verdict (for example
``approved``/``changes_requested``) belongs in the JSON ``result`` and is not
an operation status.

Ownership
---------

Each row carries a nullable, non-secret ``owner``: ``admin`` for an
administrative caller or ``execution:<agent>|<label>`` for a scoped execution
grant. It lets a scoped caller read only its own operation and lets startup
recovery resolve who submitted a running operation without ever persisting a
bearer token in the operation JSON fields.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import Database
from .models import Operation, Task, utcnow

#: Kinds whose executors only read shared state. Analyzers run read-only in
#: their own sandbox and any number of them may execute concurrently, even
#: though they share one analyzer workspace. Every other kind (Git, review)
#: mutates one task workspace and serializes on it.
OPERATION_READ_ONLY_KINDS = frozenset({"analysis"})

#: Serializes claim transactions across every repository instance: pool
#: workers each build their own repository, so the lock must be shared to
#: keep ``BEGIN IMMEDIATE`` contention rare instead of constant.
_CLAIM_LOCK = threading.Lock()

#: Queued, not yet claimed.
OPERATION_STATUS_ACCEPTED = "accepted"
OPERATION_STATUS_PENDING = "pending"
#: Claimed by exactly one worker.
OPERATION_STATUS_RUNNING = "running"
#: Terminal successes and failures.
OPERATION_STATUS_SUCCEEDED = "succeeded"
OPERATION_STATUS_FAILED = "failed"
#: Terminal state written by explicit orphan recovery.
OPERATION_STATUS_INTERRUPTED = "interrupted"

#: Every public protocol status.
OPERATION_STATUSES = frozenset(
    {
        OPERATION_STATUS_ACCEPTED,
        OPERATION_STATUS_PENDING,
        OPERATION_STATUS_RUNNING,
        OPERATION_STATUS_SUCCEEDED,
        OPERATION_STATUS_FAILED,
        OPERATION_STATUS_INTERRUPTED,
    }
)

#: Queued statuses eligible to be claimed.
OPERATION_QUEUED_STATUSES = frozenset(
    {OPERATION_STATUS_ACCEPTED, OPERATION_STATUS_PENDING}
)

#: Terminal statuses that are never reclaimed.
OPERATION_TERMINAL_STATUSES = frozenset(
    {
        OPERATION_STATUS_SUCCEEDED,
        OPERATION_STATUS_FAILED,
        OPERATION_STATUS_INTERRUPTED,
    }
)

_MAX_OPERATION_ATTEMPTS = 5


class OperationError(Exception):
    """Base class for operation queue failures."""


class OperationNotFoundError(OperationError):
    """Raised when an operation id does not exist."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"operation not found: {operation_id}")
        self.operation_id = operation_id


class OperationConflictError(OperationError):
    """Raised when an idempotency key exists with a different payload.

    The stored operation is preserved and exposed as :attr:`existing`. The
    retry payload is the semantic tuple ``kind``/``target_id``/``input``/
    ``metadata``; generated ids, statuses and timestamps are excluded.
    """

    def __init__(self, existing: Operation) -> None:
        super().__init__(
            "operation idempotency conflict for "
            f"kind={existing.kind!r} "
            f"target_id={existing.target_id!r} "
            f"idempotency_key={existing.idempotency_key!r}"
        )
        self.existing = existing


class OperationStaleClaimError(OperationError):
    """Raised when a caller no longer holds the current claim token."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(
            "claim token is missing, stale or not valid for operation "
            f"{operation_id}"
        )
        self.operation_id = operation_id


@dataclasses.dataclass(frozen=True)
class OperationSubmitResult:
    """Outcome of one :meth:`OperationRepository.submit` call.

    ``created`` is true when a new row was written; false when the
    idempotency key matched a stored operation and no row was inserted.
    """

    operation: Operation
    created: bool

    @property
    def replayed(self) -> bool:
        """True when the call returned a previously stored operation."""
        return not self.created


def _is_sqlite_busy(error: OperationalError) -> bool:
    """Return whether ``error`` is a retryable SQLite busy/snapshot failure."""
    original = error.orig
    code = getattr(original, "sqlite_errorcode", None)
    busy_codes = {sqlite3.SQLITE_BUSY}
    snapshot_code = getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", None)
    if snapshot_code is not None:
        busy_codes.add(snapshot_code)
    if code is not None and code in busy_codes:
        return True
    return "database is locked" in str(original).lower()


def _run_with_retry(operation: Any) -> Any:
    """Run ``operation`` with fresh transactions during SQLite lock races."""
    last_error: OperationalError | None = None
    for attempt in range(_MAX_OPERATION_ATTEMPTS):
        try:
            return operation()
        except OperationalError as error:
            last_error = error
            if not _is_sqlite_busy(error):
                raise
            if attempt + 1 >= _MAX_OPERATION_ATTEMPTS:
                raise
    raise last_error  # pragma: no cover - loop always returns or raises


def _payload_matches(
    operation: Operation,
    kind: str,
    target_id: str | None,
    input_payload: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> bool:
    """Return whether a stored operation matches an idempotent retry."""
    return (
        operation.kind == kind
        and operation.target_id == target_id
        and (operation.input or {}) == dict(input_payload)
        and (operation.meta or {}) == dict(meta)
    )


class OperationRepository:
    """Submit, inspect and advance durable operations.

    Args:
        database: The :class:`~kuzya.storage.database.Database` that
            owns the SQLite file. Every call opens its own short transaction on
            its own connection; no Python-level lock is used.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # -- submit ----------------------------------------------------------

    def submit(
        self,
        *,
        kind: str,
        target_id: str | None = None,
        input: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        owner: str | None = None,
    ) -> OperationSubmitResult:
        """Durably record one queued operation.

        With an ``idempotency_key`` the insert is an atomic ``INSERT ... ON
        CONFLICT DO NOTHING`` followed by a fresh read, so concurrent retries
        serialize at SQLite and never raise a raw integrity failure. The
        comparison excludes generated ids and timestamps: an exact retry
        returns the stored operation, a different payload raises
        :class:`OperationConflictError`.

        ``owner`` is the stable, non-secret caller identity persisted for
        scoped status lookup and orphan recovery. It is deliberately excluded
        from the idempotency comparison so the same call retried after a
        grant re-issue still replays the original operation.
        """
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        input_payload = dict(input) if input is not None else {}
        normalized_meta = dict(metadata) if metadata is not None else {}
        operation_id = uuid.uuid4().hex
        with self._database.session() as session:
            statement = (
                sqlite_insert(Operation)
                .values(
                    id=operation_id,
                    kind=kind,
                    target_id=target_id,
                    owner=owner,
                    input=input_payload,
                    status=OPERATION_STATUS_PENDING,
                    attempt_count=0,
                    idempotency_key=idempotency_key,
                    meta=normalized_meta,
                )
                .on_conflict_do_nothing(
                    index_elements=[Operation.idempotency_key]
                )
            )
            session.execute(statement)
            inserted = session.scalars(
                select(Operation).where(Operation.id == operation_id)
            ).one_or_none()
            if inserted is not None:
                return OperationSubmitResult(operation=inserted, created=True)

            existing = session.scalars(
                select(Operation).where(
                    Operation.idempotency_key == idempotency_key
                )
            ).one()
            if not _payload_matches(
                existing, kind, target_id, input_payload, normalized_meta
            ):
                # The surrounding context rolls back when we raise; detach the
                # original first so its loaded attributes stay readable.
                session.expunge(existing)
                raise OperationConflictError(existing)
            return OperationSubmitResult(operation=existing, created=False)

    # -- reads -----------------------------------------------------------

    def get(self, operation_id: str) -> Operation | None:
        """Return the operation with ``operation_id`` or ``None``."""
        with self._database.session() as session:
            return session.scalars(
                select(Operation).where(Operation.id == operation_id)
            ).one_or_none()

    def list(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Operation]:
        """Return operations ordered by intake sequence, oldest first."""
        statement = select(Operation).order_by(Operation.sequence)
        if status is not None:
            statement = statement.where(Operation.status == status)
        if kind is not None:
            statement = statement.where(Operation.kind == kind)
        if target_id is not None:
            statement = statement.where(Operation.target_id == target_id)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._database.session() as session:
            return list(session.scalars(statement))

    # -- claim -----------------------------------------------------------

    def claim_next(self, *, worker_id: str | None = None) -> Operation | None:
        """Atomically claim the oldest claimable queued operation.

        Workspace-mutating kinds (Git, review) serialize on their target
        task's workspace: while one such operation is ``running``, the next
        operation on the same workspace is skipped and a claimable one from
        a different workspace (or a read-only analysis) is taken instead —
        a busy folder never blocks the rest of the queue. Read-only kinds
        never serialize. The claim opens an immediate write transaction, so
        concurrent pool workers observe each other's committed claims and
        two workers can never hold the same workspace.

        Returns the claimed operation carrying a fresh ``claim_token``, or
        ``None`` when nothing was claimable.
        """
        return _run_with_retry(lambda: self._claim_once(worker_id=worker_id))

    def _serialization_key(
        self, session: Session, operation: Operation
    ) -> str | None:
        """The workspace an operation serializes on; ``None`` means free.

        Read-only kinds never serialize. Mutating kinds resolve their target
        task's workspace; tasks without a workspace form one default group,
        mirroring :meth:`TaskRepository.claim_in_progress`. An operation
        with no resolvable target neither blocks nor is blocked.
        """
        if operation.kind in OPERATION_READ_ONLY_KINDS:
            return None
        if operation.target_id is None:
            return ""
        task = session.get(Task, operation.target_id)
        if task is None:
            return None
        return task.workspace or ""

    def _claim_once(self, *, worker_id: str | None) -> Operation | None:
        for _ in range(_MAX_OPERATION_ATTEMPTS):
            with _CLAIM_LOCK:
                with self._database.session() as session:
                    # Acquire the write lock before reading, exactly like
                    # the task claim: a deferred transaction could keep a
                    # stale snapshot and let two workers claim one workspace.
                    session.execute(text("BEGIN IMMEDIATE"))
                    candidates = list(
                        session.scalars(
                            select(Operation)
                            .where(
                                Operation.status.in_(OPERATION_QUEUED_STATUSES)
                            )
                            .order_by(Operation.sequence)
                        )
                    )
                    if not candidates:
                        return None
                    busy_keys = {
                        key
                        for running in session.scalars(
                            select(Operation).where(
                                Operation.status == OPERATION_STATUS_RUNNING
                            )
                        )
                        if (
                            key := self._serialization_key(session, running)
                        )
                        is not None
                    }
                    for candidate in candidates:
                        key = self._serialization_key(session, candidate)
                        if key is not None and key in busy_keys:
                            continue
                        claim_token = uuid.uuid4().hex
                        result = session.execute(
                            update(Operation)
                            .where(
                                Operation.id == candidate.id,
                                Operation.status.in_(
                                    OPERATION_QUEUED_STATUSES
                                ),
                            )
                            .values(
                                status=OPERATION_STATUS_RUNNING,
                                claim_token=claim_token,
                                worker_id=worker_id,
                                claimed_at=utcnow(),
                                updated_at=utcnow(),
                                attempt_count=Operation.attempt_count + 1,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if result.rowcount == 1:
                            session.refresh(candidate)
                            return candidate
            # Lost a race against another worker; retry on a fresh snapshot.
        return None

    # -- complete / fail -------------------------------------------------

    def complete(
        self,
        operation_id: str,
        claim_token: str,
        *,
        result: Mapping[str, Any] | None = None,
    ) -> Operation:
        """Mark a running operation succeeded and store its JSON result.

        Requires the current ``claim_token``. A repeated completion with the
        same token returns the stored operation unchanged; a stale or
        different token raises :class:`OperationStaleClaimError`.
        """
        with self._database.session() as session:
            operation = session.scalars(
                select(Operation).where(Operation.id == operation_id)
            ).one_or_none()
            if operation is None:
                raise OperationNotFoundError(operation_id)
            if operation.status == OPERATION_STATUS_SUCCEEDED:
                if operation.claim_token == claim_token:
                    return operation
                raise OperationStaleClaimError(operation_id)
            if (
                operation.status != OPERATION_STATUS_RUNNING
                or operation.claim_token != claim_token
            ):
                raise OperationStaleClaimError(operation_id)

            stored_result = (
                dict(result) if result is not None else None
            )
            update_result = session.execute(
                update(Operation)
                .where(
                    Operation.id == operation_id,
                    Operation.status == OPERATION_STATUS_RUNNING,
                    Operation.claim_token == claim_token,
                )
                .values(
                    status=OPERATION_STATUS_SUCCEEDED,
                    result=stored_result,
                    error=None,
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            if update_result.rowcount != 1:
                raise OperationStaleClaimError(operation_id)
            session.refresh(operation)
            return operation

    def fail(
        self,
        operation_id: str,
        claim_token: str,
        *,
        error: str,
    ) -> Operation:
        """Mark a running operation failed, retaining the diagnostic error.

        Requires the current ``claim_token``. A stale or different token
        raises :class:`OperationStaleClaimError` and leaves the row untouched.
        """
        with self._database.session() as session:
            result = session.execute(
                update(Operation)
                .where(
                    Operation.id == operation_id,
                    Operation.status == OPERATION_STATUS_RUNNING,
                    Operation.claim_token == claim_token,
                )
                .values(
                    status=OPERATION_STATUS_FAILED,
                    error=error,
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                exists = session.scalars(
                    select(Operation.id).where(Operation.id == operation_id)
                ).one_or_none()
                if exists is None:
                    raise OperationNotFoundError(operation_id)
                raise OperationStaleClaimError(operation_id)
            return session.scalars(
                select(Operation).where(Operation.id == operation_id)
            ).one()

    # -- recovery --------------------------------------------------------

    def recover_orphans(
        self,
        *,
        reason: str = "operation was interrupted by process restart",
    ) -> list[str]:
        """Mark every ``running`` operation ``interrupted`` and return its ids.

        This is explicit startup reconciliation, not replay: an interrupted
        operation keeps its input and history but is never automatically
        requeued, so a consequential side effect is not silently repeated.
        The call is idempotent; a second run finds nothing running.
        """
        with self._database.session() as session:
            running = list(
                session.scalars(
                    select(Operation).where(
                        Operation.status == OPERATION_STATUS_RUNNING
                    )
                )
            )
            if not running:
                return []
            for operation in running:
                operation.status = OPERATION_STATUS_INTERRUPTED
                operation.error = reason
                operation.claim_token = None
                operation.finished_at = utcnow()
                operation.updated_at = utcnow()
            session.flush()
            return [operation.id for operation in running]

    def requeue_interrupted(self, operation_id: str) -> Operation:
        """Explicitly reopen an ``interrupted`` operation as ``pending``.

        Recovery never replays automatically; a caller must opt in with this
        method after inspecting the interrupted record. Any other status
        raises :class:`OperationStaleClaimError`.
        """
        with self._database.session() as session:
            operation = session.scalars(
                select(Operation).where(Operation.id == operation_id)
            ).one_or_none()
            if operation is None:
                raise OperationNotFoundError(operation_id)
            if operation.status != OPERATION_STATUS_INTERRUPTED:
                raise OperationStaleClaimError(operation_id)
            operation.status = OPERATION_STATUS_PENDING
            operation.error = None
            operation.claim_token = None
            operation.finished_at = None
            operation.updated_at = utcnow()
            session.flush()
            return operation


__all__ = [
    "OPERATION_QUEUED_STATUSES",
    "OPERATION_STATUSES",
    "OPERATION_STATUS_ACCEPTED",
    "OPERATION_STATUS_FAILED",
    "OPERATION_STATUS_INTERRUPTED",
    "OPERATION_STATUS_PENDING",
    "OPERATION_STATUS_RUNNING",
    "OPERATION_STATUS_SUCCEEDED",
    "OPERATION_TERMINAL_STATUSES",
    "OperationConflictError",
    "OperationError",
    "OperationNotFoundError",
    "OperationRepository",
    "OperationStaleClaimError",
    "OperationSubmitResult",
]
