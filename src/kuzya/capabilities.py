"""Machine-readable platform capability inventory, parity audit and discovery.

This module is the executable companion to ``docs/handbook``. It declares the
complete advertised platform surface -- the Pi JS tools in
``src/pi_runner/policy.mjs``, the REST routes in ``src/kuzya/api/app.py``
and transport adapters such as MCP -- and audits that declaration against
the live OpenAPI document, the mounted application routes, the real
registered JS tools and the execution permission registry.

Two audiences:

* :func:`inventory_document` returns the machine-readable inventory consumed
  by tooling and coverage tests.
* :func:`render_handbook_discovery` returns the short, stable instruction
  block injected into secretary and system agents. It always exposes the full
  handbook index, but lists only the capabilities actually enabled by the
  profile's configured tools.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .permissions import (
    ACTIONS_FOR_TOOLS,
    action_for_operation_kind,
    resolve_requirement,
)

__all__ = [
    "ADAPTER_CAPABILITIES",
    "CAPABILITIES",
    "Capability",
    "CapabilityAudit",
    "CapabilityAuditError",
    "Finding",
    "HANDBOOK",
    "HANDBOOK_DIR",
    "HandbookEntry",
    "audit_capabilities",
    "executable_tool_names",
    "inventory_document",
    "is_platform_agent",
    "render_handbook_discovery",
    "tool_capability",
    "write_handbook_discovery",
]


#: Repository-relative handbook directory. The same files are copied into the
#: knowledge base and mounted read-only at ``/knowledge/handbook`` for agents.
HANDBOOK_DIR = "docs/handbook"


@dataclasses.dataclass(frozen=True)
class HandbookEntry:
    """One stable handbook document referenced by capability discovery."""

    filename: str
    title: str
    summary: str

    @property
    def repo_path(self) -> str:
        """Canonical repository-relative path."""
        return f"{HANDBOOK_DIR}/{self.filename}"

    @property
    def sandbox_path(self) -> str:
        """Path a child execution sees when the knowledge base is mounted."""
        return f"/knowledge/handbook/{self.filename}"


#: Ordered handbook index. Every file is a stable, read-only reference.
HANDBOOK: tuple[HandbookEntry, ...] = (
    HandbookEntry(
        "README.md",
        "Handbook index",
        "scope, discovery order and verification",
    ),
    HandbookEntry(
        "objects.md",
        "Objects and links",
        "Task, Conversation, Message, Turn, AgentProfile, Operation",
    ),
    HandbookEntry(
        "workspaces-and-routing.md",
        "Workspaces and routing",
        "workspace resolution, agent_id routing, sandbox paths",
    ),
    HandbookEntry(
        "profiles-runtime-skills.md",
        "Profiles, runtime and skills",
        "immutable revisions, runtime defaults, frozen execution",
    ),
    HandbookEntry(
        "task-lifecycle.md",
        "Task lifecycle",
        "statuses, execution worker, turns, resume and recovery",
    ),
    HandbookEntry(
        "tools-and-api.md",
        "Tools and API discovery",
        "REST surface, JS tools, auth, idempotency, discovery",
    ),
    HandbookEntry(
        "mcp.md",
        "MCP transport",
        "OpenAPI-generated tools, inherited auth and scope, verification",
    ),
    HandbookEntry(
        "review.md",
        "Review",
        "independent review operation and verdict contract",
    ),
    HandbookEntry(
        "analysis-and-recommendations.md",
        "Analysis and recommendations",
        "evidence, recommendations, approval and apply",
    ),
    HandbookEntry(
        "approvals-and-revisions.md",
        "Approvals and revisions",
        "optimistic revision checks and explicit approval",
    ),
    HandbookEntry(
        "knowledge-and-integrations.md",
        "Knowledge and integrations",
        "knowledge base, OpenViking, skills, files, Telegram",
    ),
    HandbookEntry(
        "capability-inventory.md",
        "Capability inventory",
        "tool to API to executor mapping",
    ),
    HandbookEntry(
        "errors-security-isolation.md",
        "Errors, security and isolation",
        "error contract, auth, sandbox and instance isolation",
    ),
    HandbookEntry(
        "verification.md",
        "Verification",
        "bounded commands and completion checklist",
    ),
)

HANDBOOK_BY_FILENAME: Mapping[str, HandbookEntry] = {
    entry.filename: entry for entry in HANDBOOK
}


@dataclasses.dataclass(frozen=True)
class Capability:
    """One advertised tool, REST endpoint or transport adapter.

    ``kind`` is ``"tool"`` for a registered Pi JS tool, ``"endpoint"`` for a
    REST route in the OpenAPI document, and ``"adapter"`` for a transport
    such as MCP that forwards to those routes. Tool entries carry
    ``result_statuses`` (the values in the tool's structured result);
    endpoint entries carry ``success_statuses``. ``action`` is the
    execution-permission action the capability needs; ``None`` means the
    capability never reaches the HTTP backend (local) or that the action is
    resolved from the request body (deferred operations). Adapter entries
    also carry ``resource``; they describe inherited authority, not a new
    permission.
    """

    name: str
    kind: str
    method: str | None = None
    path: str | None = None
    action: str | None = None
    resource: str | None = None
    request_schema: str | None = None
    response_schema: str | None = None
    response_many: bool = False
    result_statuses: tuple[str, ...] = ()
    success_statuses: tuple[int, ...] = ()
    operation_kind: str | None = None
    doc: str = ""
    summary: str = ""
    local: bool = False
    deferred: bool = False

    def to_document(self) -> dict[str, Any]:
        """Return the JSON-compatible machine-readable representation."""
        return {
            "name": self.name,
            "kind": self.kind,
            "method": self.method,
            "path": self.path,
            "action": self.action,
            "resource": self.resource,
            "request_schema": self.request_schema,
            "response_schema": self.response_schema,
            "response_many": self.response_many,
            "result_statuses": list(self.result_statuses),
            "success_statuses": list(self.success_statuses),
            "operation_kind": self.operation_kind,
            "doc": self.doc,
            "summary": self.summary,
            "local": self.local,
            "deferred": self.deferred,
        }


def _tool(
    name: str,
    *,
    method: str | None = None,
    path: str | None = None,
    action: str | None = None,
    request_schema: str | None = None,
    result_statuses: tuple[str, ...] = ("ok",),
    operation_kind: str | None = None,
    doc: str,
    summary: str,
    local: bool = False,
    deferred: bool = False,
) -> Capability:
    return Capability(
        name=name,
        kind="tool",
        method=method,
        path=path,
        action=action,
        request_schema=request_schema,
        result_statuses=result_statuses,
        operation_kind=operation_kind,
        doc=doc,
        summary=summary,
        local=local,
        deferred=deferred,
    )


def _adapter(
    method: str,
    path: str,
    *,
    action: str,
    resource: str,
    doc: str,
    summary: str,
) -> Capability:
    return Capability(
        name=f"mcp_streamable_http {method} {path}",
        kind="adapter",
        method=method,
        path=path,
        action=action,
        resource=resource,
        doc=doc,
        summary=summary,
    )


def _endpoint(
    method: str,
    path: str,
    *,
    action: str | None = None,
    request_schema: str | None = None,
    response_schema: str | None = None,
    response_many: bool = False,
    success_statuses: tuple[int, ...] = (200,),
    doc: str,
    summary: str,
    deferred: bool = False,
) -> Capability:
    return Capability(
        name=f"{method} {path}",
        kind="endpoint",
        method=method,
        path=path,
        action=action,
        request_schema=request_schema,
        response_schema=response_schema,
        response_many=response_many,
        success_statuses=success_statuses,
        doc=doc,
        summary=summary,
        deferred=deferred,
    )


_TOOL_CAPABILITIES: tuple[Capability, ...] = (
    *(_tool(
        name, method=method, path="/tasks/{task_id}/subscriptions",
        action="tasks.write", request_schema="TaskSubscriptionCreate",
        result_statuses=("applied", "failed", "unknown"), deferred=True,
        doc="task-lifecycle.md", summary="subscribe the originating conversation to a task outcome or cancel before queuing",
    ) for name, method in (("subscribe_task", "POST"), ("unsubscribe_task", "DELETE"))),
    _tool(
        "call_api",
        # The universal tool carries universal authority: its execution
        # grant is admin-equivalent by design ("*", the same marker the
        # permission model uses for unrestricted principals).
        action="*",
        result_statuses=("ok", "applied", "failed", "unknown"),
        doc="tools-and-api.md",
        summary="direct REST API call to any endpoint for cases the dedicated tools do not cover (task metadata, conversations, turns)",
    ),
    # -- API-backed reads -------------------------------------------------
    _tool(
        "list_tasks",
        method="GET",
        path="/tasks",
        action="tasks.read",
        result_statuses=("ok", "failed"),
        doc="task-lifecycle.md",
        summary="read tasks with status/workspace filters and status_not_in exclusions",
    ),
    _tool(
        "get_task",
        method="GET",
        path="/tasks/{task_id}",
        action="tasks.read",
        result_statuses=("ok", "failed"),
        doc="task-lifecycle.md",
        summary="read one task with execution context",
    ),
    _tool(
        "get_task_logs",
        method="GET",
        path="/tasks/{task_id}",
        action="tasks.read",
        result_statuses=("ok", "failed"),
        doc="task-lifecycle.md",
        summary="read a task's execution summary, errors and review feedback",
    ),
    _tool(
        "resolve_workspace",
        method="GET",
        path="/workspaces/resolve",
        action="workspaces.read",
        result_statuses=("ok", "failed"),
        doc="workspaces-and-routing.md",
        summary="read-only workspace/executor preflight",
    ),
    _tool(
        "list_agents",
        method="GET",
        path="/agents",
        action="agents.read",
        result_statuses=("ok", "failed"),
        doc="profiles-runtime-skills.md",
        summary="list current agent profiles",
    ),
    _tool(
        "get_agent",
        method="GET",
        path="/agents/{agent_id}",
        action="agents.read",
        result_statuses=("ok", "failed"),
        doc="profiles-runtime-skills.md",
        summary="read the stored profile, revision and runner policy",
    ),
    # -- API-backed writes ------------------------------------------------
    _tool(
        "create_task",
        method="POST",
        path="/tasks",
        action="tasks.write",
        request_schema="TaskCreate",
        result_statuses=("applied", "failed"),
        doc="task-lifecycle.md",
        summary="preflight then create a task (incoming does not run)",
    ),
    _tool(
        "update_task",
        method="PATCH",
        path="/tasks/{task_id}",
        action="tasks.write",
        request_schema="TaskPatch",
        result_statuses=("applied", "failed"),
        doc="task-lifecycle.md",
        summary="preflight then patch a task; in_progress starts execution",
    ),
    _tool(
        "resume_task_session",
        method="PATCH",
        path="/tasks/{task_id}",
        action="tasks.write",
        request_schema="TaskPatch",
        result_statuses=("applied", "failed"),
        doc="task-lifecycle.md",
        summary="append instructions and return a task to in_progress",
    ),
    _tool(
        "create_agent",
        method="POST",
        path="/agents",
        action="agents.write",
        request_schema="AgentCreate",
        result_statuses=("applied", "failed"),
        doc="profiles-runtime-skills.md",
        summary="create revision 1 of a new profile (never an upsert)",
    ),
    _tool(
        "update_agent",
        method="PATCH",
        path="/agents/{agent_id}",
        action="agents.write",
        request_schema="AgentPatch",
        result_statuses=("applied", "failed"),
        doc="profiles-runtime-skills.md",
        summary="append a revision after an optimistic revision check",
    ),
    _tool(
        "delete_knowledge",
        method="DELETE",
        path="/knowledge/{path}",
        action="knowledge.write",
        result_statuses=("applied", "failed", "unknown"),
        doc="knowledge-and-integrations.md",
        summary="delete one explicitly confirmed Markdown document (not agents/)",
    ),
    # -- Durable operation submissions -----------------------------------
    _tool(
        "trigger_review",
        method="POST",
        path="/operations",
        action="review.trigger",
        request_schema="OperationSubmit",
        result_statuses=("applied", "failed"),
        operation_kind="review",
        doc="review.md",
        summary="submit a durable review operation and return its id",
    ),
    _tool(
        "trigger_analysis",
        method="POST",
        path="/operations",
        action="analysis.trigger",
        request_schema="OperationSubmit",
        result_statuses=("applied", "failed"),
        operation_kind="analysis",
        doc="analysis-and-recommendations.md",
        summary="submit a durable analysis operation and return its id",
    ),
    _tool(
        "trigger_commit_push",
        method="POST",
        path="/operations",
        action="git.actions",
        request_schema="OperationSubmit",
        result_statuses=("applied", "failed"),
        operation_kind="git.commit_push",
        doc="tools-and-api.md",
        summary="submit a durable commit/push operation",
    ),
    _tool(
        "trigger_rollback",
        method="POST",
        path="/operations",
        action="git.actions",
        request_schema="OperationSubmit",
        result_statuses=("applied", "failed"),
        operation_kind="git.rollback",
        doc="tools-and-api.md",
        summary="submit a durable rollback operation",
    ),
    _tool(
        "list_analysis_reports",
        method="GET",
        path="/tasks/{task_id}/analysis",
        action="analysis.read",
        result_statuses=("ok", "failed"),
        doc="analysis-and-recommendations.md",
        summary="list a task's persisted analysis reports",
    ),
    _tool(
        "get_analysis_report",
        method="GET",
        path="/analysis/{report_id}",
        action="analysis.read",
        result_statuses=("ok", "failed"),
        doc="analysis-and-recommendations.md",
        summary="read one analysis report with its evidence package",
    ),
    _tool(
        "list_recommendations",
        method="GET",
        path="/recommendations",
        action="analysis.read",
        result_statuses=("ok", "failed"),
        doc="analysis-and-recommendations.md",
        summary="list evidence-linked recommendations",
    ),
    _tool(
        "preview_recommendation",
        method="GET",
        path="/recommendations/{recommendation_id}",
        action="analysis.read",
        result_statuses=("ok", "failed"),
        doc="analysis-and-recommendations.md",
        summary="preview a recommendation and its conflict state",
    ),
    _tool(
        "approve_recommendation",
        method="POST",
        path="/recommendations/{recommendation_id}/approve",
        action="analysis.approve",
        result_statuses=("applied", "failed"),
        doc="approvals-and-revisions.md",
        summary="explicitly approve a proposed recommendation",
    ),
    _tool(
        "apply_recommendation",
        method="POST",
        path="/recommendations/{recommendation_id}/apply",
        action="analysis.approve",
        result_statuses=("applied", "failed"),
        doc="approvals-and-revisions.md",
        summary="apply an approved recommendation after a revision recheck",
    ),
    # -- Local session-deferred writes ------------------------------------
    _tool(
        "attach_document",
        result_statuses=("pending", "failed"),
        doc="knowledge-and-integrations.md",
        summary="queue a document attachment applied after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "archive_agent",
        result_statuses=("pending", "failed"),
        doc="profiles-runtime-skills.md",
        summary="queue a profile archive applied in-process after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "save_project_summary",
        result_statuses=("pending", "failed"),
        doc="knowledge-and-integrations.md",
        summary="queue a project summary write applied after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "write_knowledge",
        result_statuses=("pending", "failed"),
        doc="knowledge-and-integrations.md",
        summary="queue a knowledge document write applied after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "update_component_rules",
        result_statuses=("pending", "failed"),
        doc="knowledge-and-integrations.md",
        summary="queue a component rules write applied after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "install_skill",
        result_statuses=("pending", "failed"),
        doc="knowledge-and-integrations.md",
        summary="queue a skill installation applied after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "create_skill",
        result_statuses=("pending", "failed"),
        doc="knowledge-and-integrations.md",
        summary="queue a skill creation applied after the turn",
        local=True,
        deferred=True,
    ),
    _tool(
        "ask_clarification",
        result_statuses=("awaiting_user", "failed"),
        doc="task-lifecycle.md",
        summary="record a blocking clarification and await the user",
        local=True,
        deferred=True,
    ),
    # -- Local reads -------------------------------------------------------
    _tool(
        "read_knowledge",
        result_statuses=("ok", "failed"),
        doc="knowledge-and-integrations.md",
        summary="read a knowledge document or profile",
        local=True,
    ),
    _tool(
        "list_knowledge",
        result_statuses=("ok", "failed"),
        doc="knowledge-and-integrations.md",
        summary="list knowledge documents by category",
        local=True,
    ),
    _tool(
        "list_skills",
        result_statuses=("ok", "failed"),
        doc="knowledge-and-integrations.md",
        summary="list installed skills and descriptions",
        local=True,
    ),
    # -- Direct integrations ----------------------------------------------
    _tool(
        "openviking_search",
        result_statuses=("ok", "failed"),
        doc="knowledge-and-integrations.md",
        summary="search OpenViking when the instance configures it",
        local=True,
    ),
    _tool(
        "openviking_read",
        result_statuses=("ok", "failed"),
        doc="knowledge-and-integrations.md",
        summary="read a viking:// URI when the instance configures it",
        local=True,
    ),
)

_ENDPOINT_CAPABILITIES: tuple[Capability, ...] = (
    _endpoint(
        "GET",
        "/health",
        doc="errors-security-isolation.md",
        summary="public liveness probe",
    ),
    _endpoint(
        "GET",
        "/workspaces/resolve",
        action="workspaces.read",
        doc="workspaces-and-routing.md",
        summary="resolve workspace, executor and access policy",
    ),
    _endpoint(
        "POST",
        "/tasks",
        action="tasks.write",
        request_schema="TaskCreate",
        response_schema="TaskRead",
        success_statuses=(201,),
        doc="task-lifecycle.md",
        summary="create a task",
    ),
    _endpoint(
        "GET",
        "/tasks",
        action="tasks.read",
        response_schema="TaskRead",
        response_many=True,
        doc="task-lifecycle.md",
        summary="list tasks",
    ),
    _endpoint(
        "GET",
        "/tasks/counts",
        action="tasks.read",
        doc="task-lifecycle.md",
        summary="task counts keyed by status",
    ),
    _endpoint(
        "GET",
        "/monitor/executions",
        action="tasks.read",
        doc="tools-and-api.md",
        summary=(
            "list running tasks and operations with live session counters; "
            "verbose=true adds operation input and changed-file names"
        ),
    ),
    _endpoint(
        "GET",
        "/tasks/{task_id}",
        action="tasks.read",
        response_schema="TaskRead",
        doc="task-lifecycle.md",
        summary="read one task",
    ),
    _endpoint(
        "GET",
        "/tasks/{task_id}/artifacts",
        action="tasks.read",
        response_schema="TaskArtifactListResponse",
        doc="task-lifecycle.md",
        summary="list a task's artifacts",
    ),
    _endpoint(
        "GET",
        "/tasks/{task_id}/artifacts/{artifact_name}",
        action="tasks.read",
        doc="task-lifecycle.md",
        summary="download a task artifact file",
    ),
    _endpoint(
        "GET",
        "/tasks/{task_id}/session",
        action="tasks.read",
        doc="task-lifecycle.md",
        summary="read task execution session transcript",
    ),
    _endpoint(
        "POST",
        "/tasks/{task_id}/interrupt",
        action="tasks.write",
        doc="task-lifecycle.md",
        summary=(
            "request a manual stop of a running execution: signals the "
            "runner process group and lands the task in waiting_for_input "
            "with a stopped-manually clarification"
        ),
    ),
    _endpoint(
        "PATCH",
        "/tasks/{task_id}",
        action="tasks.write",
        request_schema="TaskPatch",
        response_schema="TaskRead",
        doc="task-lifecycle.md",
        summary="patch a task",
    ),
    _endpoint(
        "POST",
        "/tasks/{task_id}/attachments",
        action="tasks.write",
        response_schema="TaskArtifactRead",
        success_statuses=(201,),
        doc="task-lifecycle.md",
        summary="upload a task attachment file",
    ),
    _endpoint(
        "POST",
        "/imports/tasks",
        action="tasks.write",
        request_schema="TaskImportRequest",
        response_schema="TaskImportResponse",
        doc="task-lifecycle.md",
        summary="idempotent historical task import",
    ),
    _endpoint(
        "POST",
        "/ask",
        action="conversations.write",
        request_schema="AskRequest",
        response_schema="AskResponse",
        doc="objects.md",
        summary="ask an agent and wait for response",
    ),
    _endpoint(
        "POST",
        "/conversations/ask",
        action="conversations.write",
        request_schema="AskRequest",
        response_schema="AskResponse",
        doc="objects.md",
        summary="ask an agent in a conversation and wait for response",
    ),
    _endpoint(
        "POST",
        "/conversations",
        action="conversations.write",
        request_schema="ConversationCreate",
        response_schema="ConversationRead",
        success_statuses=(201,),
        doc="objects.md",
        summary="create a conversation",
    ),
    _endpoint(
        "GET",
        "/conversations",
        action="conversations.read",
        response_schema="ConversationRead",
        response_many=True,
        doc="objects.md",
        summary="list conversations with optional agent_id filter",
    ),
    _endpoint(
        "GET",
        "/conversations/{conversation_id}",
        action="conversations.read",
        response_schema="ConversationRead",
        doc="objects.md",
        summary="read one conversation",
    ),
    _endpoint(
        "PATCH",
        "/conversations/{conversation_id}",
        action="conversations.write",
        request_schema="ConversationPatch",
        response_schema="ConversationRead",
        doc="objects.md",
        summary="patch conversation title or shallow-merged metadata",
    ),
    _endpoint(
        "POST",
        "/conversations/{conversation_id}/talk",
        action="conversations.write",
        request_schema="ConversationTalkRequest",
        response_schema="ConversationRead",
        doc="task-lifecycle.md",
        summary="start, close or finalize a direct-agent workspace talk",
    ),
    _endpoint(
        "POST",
        "/conversations/{conversation_id}/messages",
        action="conversations.write",
        request_schema="MessageCreate",
        response_schema="MessageAppendResponse",
        success_statuses=(201, 200),
        doc="objects.md",
        summary="append a message (idempotent by external_id)",
    ),
    _endpoint(
        "GET",
        "/conversations/{conversation_id}/messages",
        action="conversations.read",
        response_schema="MessageRead",
        response_many=True,
        doc="objects.md",
        summary="list messages",
    ),
    _endpoint(
        "POST",
        "/conversations/{conversation_id}/turns",
        action="turns.write",
        request_schema="TurnCreate",
        response_schema="TurnRead",
        success_statuses=(201,),
        doc="objects.md",
        summary="enqueue a secretary turn",
    ),
    _endpoint(
        "GET",
        "/telegram/links/{chat_id}/{telegram_message_id}",
        action="conversations.read",
        response_schema="TelegramMessageLinkRead",
        doc="knowledge-and-integrations.md",
        summary="resolve a Telegram message to its conversation link",
    ),
    _endpoint(
        "GET",
        "/turns/{turn_id}",
        action="turns.read",
        response_schema="TurnRead",
        doc="objects.md",
        summary="read turn status and reply reference",
    ),
    _endpoint(
        "GET",
        "/tasks/{task_id}/analysis",
        action="analysis.read",
        response_schema="AnalysisReportRead",
        response_many=True,
        doc="analysis-and-recommendations.md",
        summary="list a task's analysis reports",
    ),
    _endpoint(
        "GET",
        "/tasks/{task_id}/review",
        action="tasks.read",
        response_schema="ReviewRoundRead",
        response_many=True,
        doc="review.md",
        summary="list persisted review rounds and findings for a task",
    ),
    _endpoint(
        "GET",
        "/analysis/{report_id}",
        action="analysis.read",
        response_schema="AnalysisReportRead",
        doc="analysis-and-recommendations.md",
        summary="read one analysis report with evidence",
    ),
    _endpoint(
        "GET",
        "/analysis/{report_id}/recommendations",
        action="analysis.read",
        response_schema="RecommendationRead",
        response_many=True,
        doc="analysis-and-recommendations.md",
        summary="list a report's recommendations",
    ),
    _endpoint(
        "GET",
        "/recommendations",
        action="analysis.read",
        response_schema="RecommendationRead",
        response_many=True,
        doc="analysis-and-recommendations.md",
        summary="list recommendations",
    ),
    _endpoint(
        "GET",
        "/recommendations/{recommendation_id}",
        action="analysis.read",
        response_schema="RecommendationPreviewRead",
        doc="analysis-and-recommendations.md",
        summary="preview a recommendation",
    ),
    _endpoint(
        "POST",
        "/recommendations/{recommendation_id}/approve",
        action="analysis.approve",
        response_schema="RecommendationRead",
        doc="approvals-and-revisions.md",
        summary="explicitly approve a recommendation",
    ),
    _endpoint(
        "POST",
        "/recommendations/{recommendation_id}/apply",
        action="analysis.approve",
        response_schema="RecommendationRead",
        doc="approvals-and-revisions.md",
        summary="apply an approved recommendation after a recheck",
    ),
    _endpoint(
        "POST",
        "/operations",
        request_schema="OperationSubmit",
        response_schema="OperationRead",
        success_statuses=(202, 200),
        doc="tools-and-api.md",
        summary="submit a deferred operation (kind/target scoped)",
        deferred=True,
    ),
    _endpoint(
        "GET",
        "/operations",
        response_schema="OperationRead",
        response_many=True,
        doc="tools-and-api.md",
        summary=(
            "list operations with status/kind/target_id filters "
            "(administrative only for scoped callers)"
        ),
        deferred=True,
    ),
    _endpoint(
        "GET",
        "/operations/{operation_id}",
        response_schema="OperationRead",
        doc="tools-and-api.md",
        summary="read one operation (owner-scoped for scoped callers)",
        deferred=True,
    ),
    _endpoint(
        "POST",
        "/agents",
        action="agents.write",
        request_schema="AgentCreate",
        response_schema="AgentRead",
        success_statuses=(201,),
        doc="profiles-runtime-skills.md",
        summary="create revision 1 of a profile",
    ),
    _endpoint(
        "GET",
        "/agents",
        action="agents.read",
        response_schema="AgentRead",
        response_many=True,
        doc="profiles-runtime-skills.md",
        summary="list current profiles",
    ),
    _endpoint(
        "GET",
        "/agents/{agent_id}",
        action="agents.read",
        response_schema="AgentRead",
        doc="profiles-runtime-skills.md",
        summary="read the current profile snapshot",
    ),
    _endpoint(
        "GET",
        "/agents/{agent_id}/sessions",
        action="agents.read",
        response_schema="AgentSessionRead",
        response_many=True,
        doc="profiles-runtime-skills.md",
        summary=(
            "list native Runner session ids, attempt counts and timestamps "
            "without reading session contents"
        ),
    ),
    _endpoint(
        "GET",
        "/agents/{agent_id}/revisions",
        action="agents.read",
        response_schema="AgentRead",
        response_many=True,
        doc="approvals-and-revisions.md",
        summary="list profile revisions",
    ),
    _endpoint(
        "GET",
        "/agents/{agent_id}/revisions/{revision}",
        action="agents.read",
        response_schema="AgentRead",
        doc="approvals-and-revisions.md",
        summary="read one profile revision",
    ),
    _endpoint(
        "PATCH",
        "/agents/{agent_id}",
        action="agents.write",
        request_schema="AgentPatch",
        response_schema="AgentRead",
        doc="approvals-and-revisions.md",
        summary="append a revision after an optimistic check",
    ),
    _endpoint(
        "DELETE",
        "/agents/{agent_id}",
        action="agents.write",
        response_schema="AgentRead",
        doc="approvals-and-revisions.md",
        summary="archive a profile (core system agents rejected)",
    ),
    _endpoint(
        "GET",
        "/knowledge",
        action="knowledge.read",
        response_schema="KnowledgeListResponse",
        doc="knowledge-and-integrations.md",
        summary="list knowledge documents",
    ),
    _endpoint(
        "GET",
        "/knowledge/{path}",
        action="knowledge.read",
        response_schema="KnowledgeDocumentRead",
        doc="knowledge-and-integrations.md",
        summary="read one knowledge document",
    ),
    _endpoint(
        "PUT",
        "/knowledge/{path}",
        action="knowledge.write",
        request_schema="KnowledgeDocumentWrite",
        response_schema="KnowledgeDocumentRead",
        doc="knowledge-and-integrations.md",
        summary="write or update a knowledge document",
    ),
    _endpoint(
        "DELETE",
        "/knowledge/{path}",
        action="knowledge.write",
        response_schema="KnowledgeDocumentDeleted",
        doc="knowledge-and-integrations.md",
        summary="delete one explicitly named Markdown document (not agents/)",
    ),
)

#: MCP is one transport adapter, represented by its supported method/path
#: pairs. It deliberately does not repeat the 45 generated MCP tools: those
#: are derived from the REST endpoints at application construction.
_ADAPTER_CAPABILITIES: tuple[Capability, ...] = tuple(
    _adapter(
        method,
        path,
        action="*",
        resource="*",
        doc="mcp.md",
        summary=(
            "MCP Streamable HTTP transport adapter; forwards to the real "
            "REST route and inherits its action/resource"
        ),
    )
    for path in ("/mcp", "/mcp/")
    for method in ("GET", "POST")
)

#: The complete advertised surface: 38 tools, every REST endpoint and the
#: explicit method/path records of the MCP transport adapter.
CAPABILITIES: tuple[Capability, ...] = (
    *(_endpoint(
        method, "/tasks/{task_id}/subscriptions", deferred=True,
        request_schema="TaskSubscriptionCreate", doc="task-lifecycle.md",
        summary="persist or cancel a completion subscription for the originating conversation",
    ) for method in ("POST", "DELETE")),
    *_TOOL_CAPABILITIES,
    *_ENDPOINT_CAPABILITIES,
    *_ADAPTER_CAPABILITIES,
)

TOOL_CAPABILITIES: tuple[Capability, ...] = tuple(
    cap for cap in CAPABILITIES if cap.kind == "tool"
)
#: Every HTTP route the inventory knows about: REST operations and adapters.
ENDPOINT_CAPABILITIES: tuple[Capability, ...] = tuple(
    cap for cap in CAPABILITIES if cap.kind in {"endpoint", "adapter"}
)
ADAPTER_CAPABILITIES: tuple[Capability, ...] = tuple(
    cap for cap in CAPABILITIES if cap.kind == "adapter"
)
TOOL_BY_NAME: Mapping[str, Capability] = {
    cap.name: cap for cap in TOOL_CAPABILITIES
}
ENDPOINT_BY_KEY: Mapping[tuple[str, str], Capability] = {
    (cap.method or "", cap.path or ""): cap for cap in ENDPOINT_CAPABILITIES
}


def inventory_document() -> dict[str, Any]:
    """Return the machine-readable inventory as a JSON-compatible mapping."""
    return {
        "handbook_dir": HANDBOOK_DIR,
        "handbook": [
            {
                "path": entry.repo_path,
                "sandbox_path": entry.sandbox_path,
                "title": entry.title,
                "summary": entry.summary,
            }
            for entry in HANDBOOK
        ],
        "tools": [cap.to_document() for cap in TOOL_CAPABILITIES],
        "endpoints": [cap.to_document() for cap in ENDPOINT_CAPABILITIES],
    }


# ---------------------------------------------------------------------------
# Parity audit
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Finding:
    """One parity defect with a stable machine-readable code."""

    code: str
    message: str


class CapabilityAuditError(AssertionError):
    """Raised when a capability audit finds one or more defects."""

    def __init__(self, findings: Iterable[Finding]) -> None:
        self.findings = tuple(findings)
        detail = "; ".join(f"{f.code}: {f.message}" for f in self.findings)
        super().__init__(detail)


@dataclasses.dataclass(frozen=True)
class CapabilityAudit:
    """Result of comparing the declared inventory with the live platform."""

    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no parity defect was found."""
        return not self.findings

    def raise_for_findings(self) -> None:
        """Raise :class:`CapabilityAuditError` when any defect was found."""
        if self.findings:
            raise CapabilityAuditError(self.findings)


def _basename(ref: str | None) -> str | None:
    if not ref:
        return None
    return ref.rsplit("/", 1)[-1]


def _schema_ref(operation: Mapping[str, Any], code: int) -> str | None:
    responses = operation.get("responses") or {}
    response = responses.get(str(code))
    if not isinstance(response, Mapping):
        return None
    schema = (
        (response.get("content") or {})
        .get("application/json", {})
        .get("schema", {})
    )
    if not isinstance(schema, Mapping):
        return None
    if "$ref" in schema:
        return _basename(schema.get("$ref"))
    items = schema.get("items")
    if isinstance(items, Mapping):
        return _basename(items.get("$ref"))
    return None


def _request_ref(operation: Mapping[str, Any]) -> str | None:
    body = operation.get("requestBody") or {}
    schema = (
        (body.get("content") or {})
        .get("application/json", {})
        .get("schema", {})
    )
    if not isinstance(schema, Mapping):
        return None
    return _basename(schema.get("$ref"))


def audit_capabilities(
    *,
    openapi_paths: Mapping[str, Mapping[str, Any]] | None = None,
    registered_tool_names: Iterable[str] | None = None,
    mounted_route_paths: Iterable[str] | None = None,
) -> CapabilityAudit:
    """Compare the declared inventory with the live platform surface.

    Args:
        openapi_paths: The ``paths`` mapping from the FastAPI-generated
            OpenAPI document. When supplied, every declared REST endpoint
            must exist and every live route must be declared. Adapter
            records are excluded from this comparison because they are
            deliberately not OpenAPI operations.
        registered_tool_names: The real tool names registered by
            ``src/pi_runner/policy.mjs``. When supplied, the declared tools
            and the registered tools must match exactly.
        mounted_route_paths: Paths actually mounted on the application
            router. When supplied, every declared adapter path must be
            present; this prevents the adapter from silently disappearing
            from the mounted app while remaining in the inventory.

    The audit is executable and non-vacuous: it fails when a tool, route,
    schema, permission mapping or operation-kind mapping drifts from source.
    """
    findings: list[Finding] = []

    declared_tools = {cap.name for cap in TOOL_CAPABILITIES}
    if registered_tool_names is not None:
        registered = set(registered_tool_names)
        for name in sorted(registered - declared_tools):
            findings.append(
                Finding(
                    "undeclared-tool",
                    f"registered JS tool {name!r} is not in the inventory",
                )
            )
        for name in sorted(declared_tools - registered):
            findings.append(
                Finding(
                    "missing-tool",
                    f"inventory declares tool {name!r} that policy.mjs does "
                    "not register",
                )
            )

    if openapi_paths is not None:
        live: set[tuple[str, str]] = {
            (method.upper(), path)
            for path, methods in openapi_paths.items()
            for method in methods
        }
        declared_endpoints = {
            (cap.method or "", cap.path or "")
            for cap in ENDPOINT_CAPABILITIES
            if cap.kind == "endpoint"
        }
        for method, path in sorted(declared_endpoints - live):
            findings.append(
                Finding(
                    "unsupported-route",
                    f"inventory declares {method} {path} but OpenAPI has no "
                    "such route",
                )
            )
        for method, path in sorted(live - declared_endpoints):
            findings.append(
                Finding(
                    "undocumented-route",
                    f"OpenAPI exposes {method} {path} that the inventory does "
                    "not document",
                )
            )

        mounted_paths = (
            set(mounted_route_paths)
            if mounted_route_paths is not None
            else None
        )
        for cap in ADAPTER_CAPABILITIES:
            key = (cap.method or "", cap.path or "")
            adapter_path = cap.path or ""
            if adapter_path in openapi_paths:
                findings.append(
                    Finding(
                        "adapter-openapi-overlap",
                        f"adapter declares {key[0]} {adapter_path} but the "
                        "path is an OpenAPI operation",
                    )
                )
            requirement = resolve_requirement(key[0], adapter_path)
            if requirement is not None:
                findings.append(
                    Finding(
                        "adapter-permission-drift",
                        f"adapter {key[0]} {adapter_path} resolves to "
                        f"{getattr(requirement, 'action', None)!r}; it must "
                        "remain outside permission route rules so execution "
                        "tokens are denied",
                    )
                )
            if mounted_paths is not None and adapter_path not in mounted_paths:
                findings.append(
                    Finding(
                        "missing-adapter-route",
                        f"adapter declares {key[0]} {adapter_path} but the "
                        "application router does not mount that path",
                    )
                )

        for cap in ENDPOINT_CAPABILITIES:
            if cap.kind != "endpoint":
                continue
            key = (cap.method or "", cap.path or "")
            operation = openapi_paths.get(cap.path or {}, {}).get(
                (cap.method or "").lower()
            )
            if operation is None:
                continue
            if cap.request_schema is not None:
                declared_ref = _request_ref(operation)
                if declared_ref != cap.request_schema:
                    findings.append(
                        Finding(
                            "request-schema-drift",
                            f"{key[0]} {key[1]} request schema is "
                            f"{declared_ref!r}, inventory says "
                            f"{cap.request_schema!r}",
                        )
                    )
            if cap.response_schema is not None:
                matching = [
                    _schema_ref(operation, code)
                    for code in cap.success_statuses
                ]
                if cap.response_schema not in matching:
                    findings.append(
                        Finding(
                            "response-schema-drift",
                            f"{key[0]} {key[1]} success schemas are "
                            f"{matching!r}, inventory says "
                            f"{cap.response_schema!r}",
                        )
                    )
                elif cap.response_many:
                    schema = (
                        (operation.get("responses") or {})
                        .get(str(cap.success_statuses[0]), {})
                        .get("content", {})
                        .get("application/json", {})
                        .get("schema", {})
                    )
                    if isinstance(schema, Mapping) and schema.get("type") != "array":
                        findings.append(
                            Finding(
                                "response-shape-drift",
                                f"{key[0]} {key[1]} is not a JSON array in "
                                "OpenAPI",
                            )
                        )

            requirement = resolve_requirement(key[0], key[1])
            if cap.deferred:
                if requirement is None or not requirement.deferred:
                    findings.append(
                        Finding(
                            "deferred-route-drift",
                            f"{key[0]} {key[1]} should resolve to a deferred "
                            "requirement",
                        )
                    )
            elif cap.action is not None:
                if requirement is None or requirement.action != cap.action:
                    findings.append(
                        Finding(
                            "route-action-drift",
                            f"{key[0]} {key[1]} resolves to "
                            f"{getattr(requirement, 'action', None)!r}, "
                            f"inventory says {cap.action!r}",
                        )
                    )

    # Permission mapping: every declared backend action must match the
    # registry, and no registry entry may silently grant authority to a tool
    # the inventory does not declare with that action.
    declared_actions = {
        cap.name: cap.action for cap in TOOL_CAPABILITIES if cap.action is not None
    }
    for name, action in sorted(declared_actions.items()):
        mapped = ACTIONS_FOR_TOOLS.get(name)
        if mapped != action:
            findings.append(
                Finding(
                    "tool-action-drift",
                    f"tool {name!r} declares action {action!r} but "
                    f"ACTIONS_FOR_TOOLS maps {mapped!r}",
                )
            )
    for name in sorted(set(ACTIONS_FOR_TOOLS) - set(declared_actions)):
        findings.append(
            Finding(
                "widening-tool-action",
                f"ACTIONS_FOR_TOOLS grants {ACTIONS_FOR_TOOLS[name]!r} to "
                f"undeclared or local tool {name!r}",
            )
        )

    # Operation kinds: a trigger tool's declared kind must resolve to the
    # tool's action through the same registry used by the API handler.
    for cap in TOOL_CAPABILITIES:
        if cap.operation_kind is None:
            continue
        resolved = action_for_operation_kind(cap.operation_kind)
        if resolved != cap.action:
            findings.append(
                Finding(
                    "operation-kind-action-drift",
                    f"tool {cap.name!r} kind {cap.operation_kind!r} resolves "
                    f"to {resolved!r}, inventory says {cap.action!r}",
                )
            )

    return CapabilityAudit(findings=tuple(findings))


# ---------------------------------------------------------------------------
# Discovery injection
# ---------------------------------------------------------------------------


def executable_tool_names(tools: Iterable[str] | None) -> tuple[str, ...]:
    """Return the inventory tools enabled by ``tools``, preserving order.

    Unknown names (filesystem tools such as ``read`` or ``bash``) are
    ignored: the executable capability list is derived from the platform
    inventory, never from an arbitrary profile string.
    """
    seen: set[str] = set()
    enabled: list[str] = []
    for name in tools or ():
        if name in TOOL_BY_NAME and name not in seen:
            seen.add(name)
            enabled.append(name)
    return tuple(enabled)


def is_platform_agent(agent_id: str | None, tools: Iterable[str] | None) -> bool:
    """Return whether an execution should receive platform discovery.

    The secretary (identified by the platform tool it needs) and every
    ``system-*`` agent receive the discovery block; customized identity
    prompts are never replaced. Project developer profiles do not.
    """
    if agent_id and agent_id.startswith("system-"):
        return True
    return "create_task" in set(tools or ())


def render_handbook_discovery(tools: Iterable[str] | None = None) -> str:
    """Return the short, stable handbook discovery instruction block.

    Full documentation stays discoverable for every agent. The executable
    list is filtered by the profile's configured tools so a model is not told
    it can call a tool that its revision does not expose.
    """
    lines = [
        "## Platform handbook and capability discovery",
        "",
        "The platform handbook is the read-only operating contract. Read the",
        "relevant file before a non-trivial platform action; prose never",
        "overrides source. Stable paths (also mounted at /knowledge/handbook):",
        "",
    ]
    for entry in HANDBOOK:
        lines.append(
            f"- `{entry.repo_path}` (agent: `{entry.sandbox_path}`) — "
            f"{entry.title}: {entry.summary}"
        )
    lines.append("")
    if tools is not None:
        executable = executable_tool_names(tools)
        if executable:
            rendered = ", ".join(f"`{name}`" for name in executable)
            lines.append(
                "Executable platform tools for this profile: " + rendered + "."
            )
        else:
            lines.append(
                "Executable platform tools for this profile: none. This "
                "execution exposes no platform tool."
            )
        lines.append("")
    lines.append(
        "Tool result status: `ok` (read), `applied` (write accepted), "
        "`pending` (deferred, not yet applied), `awaiting_user`, `failed`, "
        "`unknown` (read state before retrying). Never report `applied` or "
        "`pending` as completed."
    )
    return "\n".join(lines)


def write_handbook_discovery(
    directory: Path | str,
    *,
    agent_id: str | None,
    tools: Iterable[str] | None,
) -> Path | None:
    """Write the discovery block for a platform agent into ``directory``.

    Returns the written path, or ``None`` when the agent is not a platform
    agent and the caller should not append an instruction file.
    """
    if not is_platform_agent(agent_id, tools):
        return None
    target = Path(directory) / "platform_discovery.md"
    target.write_text(
        render_handbook_discovery(tools).strip() + "\n", encoding="utf-8"
    )
    return target
