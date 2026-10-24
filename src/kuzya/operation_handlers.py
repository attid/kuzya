"""Concrete executors for the built-in operation kinds.

The Git executors are the durable replacement for the previous synchronous
``actions.json`` handling. They read the task, resolve its workspace, invoke
:class:`~kuzya.git_engine.GitEngine` and update the task status only
after the Git call succeeds. A Git failure raises, so the operation is recorded
``failed`` instead of reporting success.

The ``review`` executor (T03) launches the independent, read-only
``system-code-reviewer`` profile through the shared
:mod:`kuzya.review_executor` path. It never reruns the developer and
never approves by absence of findings: a nonzero exit, a failed Runner status,
an empty/malformed report or an absent verdict fails the operation.

The ``analysis`` executor (T04) is registered here through the durable worker
seam and delegates to :mod:`kuzya.analysis`. Until an executor is
registered such operations fail visibly with an unknown-executor diagnostic
rather than pretending to succeed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from .analysis import analysis_operation_handler
from .git_engine import GitEngine
from .operation_worker import (
    OPERATION_KIND_ANALYSIS,
    OPERATION_KIND_GIT_COMMIT_PUSH,
    OPERATION_KIND_GIT_ROLLBACK,
    OPERATION_KIND_REVIEW,
    OperationContext,
    OperationExecutionError,
    OperationWorker,
    OperationWorkerPool,
)
from .review_executor import (
    REVIEWER_AGENT_ID,
    ReviewExecutionError,
    ReviewExecutor,
    ReviewRequest,
)
from .storage import AgentProfileRepository, TaskRepository
from .workspaces import resolve_workspace

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_task_context(
    context: OperationContext,
    action: str,
) -> tuple[TaskRepository, str, Any, str, list[str]]:
    """Resolve the target task, workspace and metadata for a Git action."""
    database = context.instance.database
    task_id = (context.target_id or "").strip()
    if not task_id:
        raise OperationExecutionError(f"{action} requires a target task id")
    repository = TaskRepository(database)
    task = repository.get(task_id)
    if task is None:
        raise OperationExecutionError(f"task not found: {task_id}")
    # A task created without an explicit workspace still executes in its
    # agent's project directory — the worker records that path in
    # execution_context.host_path. Git and review actions must resolve
    # through it so the commit lands next to the changes; the instance
    # default workspace is a different repository and stays the last
    # resort only.
    execution_context = (task.meta or {}).get("execution_context") or {}
    workspace = task.workspace or (
        str(execution_context["host_path"])
        if isinstance(execution_context, dict)
        and execution_context.get("host_path")
        else None
    )
    if not workspace and context.instance.default_workspace is not None:
        workspace = str(context.instance.default_workspace)
    if not workspace:
        raise OperationExecutionError(
            f"{action} has no workspace for task {task_id}"
        )
    meta = dict(task.meta or {})
    changed_files = [
        str(item) for item in (meta.get("changed_files") or [])
    ]
    return repository, task_id, task, workspace, changed_files


def git_commit_push_handler(
    context: OperationContext,
) -> dict[str, Any]:
    """Commit (and optionally push) the task's changes, then mark it done.

    A task with ``requires_review`` may only be committed with a recorded
    approved verdict (``metadata.review.approved``): the invariant is
    enforced here, at the transition to ``done``, so neither a missed
    automatic review nor a manual trigger can slip unreviewed changes
    through. ``input.force=true`` is the explicit, deliberate override.
    """
    repository, task_id, task, workspace, changed_files = _load_task_context(
        context, "git.commit_push"
    )
    meta = dict(task.meta or {})
    review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
    force = bool(context.input.get("force", False))
    if bool(meta.get("requires_review")) and not review.get("approved"):
        if not force:
            raise OperationExecutionError(
                "task requires review but no approved review is recorded "
                f"(metadata.review.approved={review.get('approved')!r}); "
                "run the review operation first, or pass input.force=true "
                "for a deliberate override"
            )
        logger.warning(
            "git.commit_push on task %s forced past an unapproved review",
            task_id,
            extra={"event": "commit_forced_past_review", "task_id": task_id},
        )
    custom_message = str(context.input.get("message") or "").strip() or None
    push = bool(context.input.get("push", True))
    require_ci = bool(context.input.get("require_ci", False))
    engine = GitEngine(default_workspace=workspace)
    common: dict[str, Any] = {
        "workspace": workspace,
        "task_id": task_id,
        "title": task.title,
        "summary": meta.get("summary"),
        "changed_files": changed_files,
        "custom_message": custom_message,
    }
    if push:
        result = engine.commit_and_push(**common, require_ci=require_ci)
    else:
        result = engine.commit_local(**common)
    if not result.success:
        raise OperationExecutionError(
            result.message or "git commit/push failed"
        )
    repository.update(task_id, status="done")
    return {
        "action": result.action,
        "message": result.message,
        "commit_hash": result.commit_hash,
        "branch": result.branch,
        "ci_status": result.ci_status,
        "ci_required": require_ci,
        "pushed": push,
        "task_status": "done",
    }


def git_rollback_handler(context: OperationContext) -> dict[str, Any]:
    """Discard the task's working-tree changes and cancel the task."""
    repository, task_id, _task, workspace, changed_files = _load_task_context(
        context, "git.rollback"
    )
    engine = GitEngine(default_workspace=workspace)
    result = engine.rollback(
        workspace=workspace, changed_files=changed_files
    )
    if not result.success:
        raise OperationExecutionError(result.message or "git rollback failed")
    repository.update(task_id, status="cancelled")
    return {
        "action": result.action,
        "message": result.message,
        "task_status": "cancelled",
    }


def review_operation_handler(context: OperationContext) -> dict[str, Any]:
    """Run one independent, read-only review round for a task.

    The target task is loaded directly, so a review runs even when the task's
    execution status is already ``completed`` and is never rerun through
    :meth:`TaskRepository.claim_in_progress`. The developer is not resumed:
    this executor only produces a validated verdict and persists the report,
    metrics and (on failure) diagnostics.
    """
    database = context.instance.database
    task_id = (context.target_id or "").strip()
    if not task_id:
        raise OperationExecutionError("review requires a target task id")

    repository = TaskRepository(database)
    task = repository.get(task_id)
    if task is None:
        raise OperationExecutionError(f"task not found: {task_id}")

    # Same resolution rule as the Git actions: a task without an explicit
    # workspace executes in its agent's project directory, recorded in
    # execution_context.host_path. Reviewing the instance default instead
    # would read a different repository than the one the agent changed.
    execution_context = (task.meta or {}).get("execution_context") or {}
    workspace = task.workspace or (
        str(execution_context["host_path"])
        if isinstance(execution_context, dict)
        and execution_context.get("host_path")
        else None
    )
    if not workspace and context.instance.default_workspace is not None:
        workspace = str(context.instance.default_workspace)
    if not workspace:
        raise OperationExecutionError(f"review has no workspace for task {task_id}")
    try:
        project_dir = resolve_workspace(
            workspace,
            default_workspace=context.instance.default_workspace,
            workspace_map=dict(context.instance.workspace_map or {}),
        )
    except ValueError as error:
        raise OperationExecutionError(
            f"review workspace is not usable: {error}"
        ) from error

    meta = dict(task.meta or {})
    changed_files = [str(item) for item in (meta.get("changed_files") or [])]
    developer_summary = str(meta.get("summary") or "")
    focus = str(context.input.get("instructions") or "").strip()

    agent_repository = AgentProfileRepository(database)
    knowledge_base = context.instance.knowledge_base
    if knowledge_base is not None and hasattr(knowledge_base, "sync_agent_from_file"):
        try:
            knowledge_base.sync_agent_from_file(REVIEWER_AGENT_ID, database)
        except Exception as error:  # noqa: BLE001 - profile sync is best effort
            logger.debug("Could not sync reviewer profile from file: %s", error)
    reviewer = agent_repository.get_current(REVIEWER_AGENT_ID)

    task_artifact_root = Path(context.instance.artifact_root) / f"task_{task_id}"
    review_exec_dir = task_artifact_root / f"operation_{context.operation_id}"
    review_exec_dir.mkdir(parents=True, exist_ok=True)

    executor = ReviewExecutor(
        runner_argv_prefix=tuple(context.instance.runner_argv_prefix),
        runtime_defaults=getattr(context.instance, "runtime_defaults", None),
        knowledge_base=knowledge_base,
    )
    request = ReviewRequest(
        task_id=task_id,
        task_title=task.title,
        task_description=task.description,
        project_dir=project_dir,
        changed_files=tuple(changed_files),
        developer_summary=developer_summary,
        reviewer_system_prompt=(reviewer.system_prompt if reviewer else ""),
        reviewer_runner_config=(
            dict(reviewer.runner_config)
            if reviewer and isinstance(reviewer.runner_config, dict)
            else {}
        ),
        focus=focus,
        round_number=1,
        state_dir=review_exec_dir / "state",
        artifact_dir=review_exec_dir,
        task_artifact_dir=review_exec_dir,
    )

    try:
        result = executor.execute(request)
    except ReviewExecutionError as error:
        detail = str(error)
        reason = error.diagnostics.get("reason")
        if reason:
            detail = f"{detail} [{reason}]"
        if error.diagnostics_path:
            detail = f"{detail} (diagnostics: {error.diagnostics_path})"
        repository.update(
            task_id,
            metadata={
                **meta,
                "review": {
                    "verdict": None,
                    "approved": False,
                    "operation_id": context.operation_id,
                    "error": str(error),
                    "diagnostics": error.diagnostics,
                    "diagnostics_path": error.diagnostics_path,
                    "reviewer_agent_id": REVIEWER_AGENT_ID,
                    "timestamp": _utcnow_iso(),
                },
            },
        )
        raise OperationExecutionError(f"review failed: {detail}") from error

    repository.update(
        task_id,
        metadata={
            **meta,
            "review": {
                "verdict": result.verdict,
                "approved": result.approved,
                "operation_id": context.operation_id,
                "round": result.round_number,
                "report_path": result.report_path,
                "metrics_path": result.metrics_path,
                "findings": [finding.to_dict() for finding in result.findings],
                "reviewer_agent_id": result.reviewer_agent_id,
                "timestamp": _utcnow_iso(),
            },
        },
    )
    return result.to_dict()


def register_default_operation_handlers(
    worker: OperationWorker | OperationWorkerPool,
) -> None:
    """Register the built-in Git, review and analysis executors on ``worker``.

    Called from the runner composition root with either a lone worker or a
    worker pool; both expose ``register``. A kind with no registered
    executor still fails visibly with an unknown-executor diagnostic.
    """
    worker.register(OPERATION_KIND_GIT_COMMIT_PUSH, git_commit_push_handler)
    worker.register(OPERATION_KIND_GIT_ROLLBACK, git_rollback_handler)
    worker.register(OPERATION_KIND_REVIEW, review_operation_handler)
    worker.register(OPERATION_KIND_ANALYSIS, analysis_operation_handler)


__all__ = [
    "analysis_operation_handler",
    "git_commit_push_handler",
    "git_rollback_handler",
    "register_default_operation_handlers",
    "review_operation_handler",
]
