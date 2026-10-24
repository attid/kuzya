"""Execution-scoped API actions and resources.

The platform exposes an HTTP backend to child Pi executions. Administrative
user credentials must never be handed to a developer, reviewer or analyzer
sandbox, and a restricted execution must not become unrestricted by reading
its credential file and calling the backend directly with ``curl``.

This module is the reusable authorization seam:

* :class:`ExecutionPermissions` issues opaque, unpredictable tokens and keeps
  the granted actions and resources in memory for the lifetime of the
  server process.
* Action names are derived from the profile's configured tools, never from a
  role or name allowlist (see :func:`actions_for_tools`).
* Resources identify the concrete objects an execution may touch, for
  example ``task:<id>`` or ``agent:<id>``. The wildcard ``*`` is reserved for
  administrative principals.
* :func:`resolve_requirement` maps an HTTP method and path to the action and
  resource an endpoint needs, so middleware enforces the same rule for every
  client, including a raw HTTP call from a shell.

T01/T05 seam
------------

The operation endpoints and trigger dispatch added by T01 are wired here:
:data:`OPERATION_KIND_ACTIONS` maps a submitted operation kind to the exact
action it needs, :func:`action_for_operation_kind` resolves it, and the
``/operations`` routes are deliberately marked ``deferred`` so the API
handler (not the generic route middleware) can enforce the kind-specific
action and the concrete ``task:<id>`` target. The middleware still
authenticates the token and exposes the resolved grant on
``scope['state']['execution_grant']``. Callers read it through
``request.state.execution_grant``.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "ACTIONS_FOR_TOOLS",
    "ADMIN_OWNER",
    "ADMIN_RESOURCE",
    "ExecutionGrant",
    "ExecutionPermissions",
    "Requirement",
    "action_for_operation_kind",
    "actions_for_tools",
    "execution_owner",
    "owner_for_grant",
    "resolve_requirement",
]


#: Resource value granted to administrative principals only.
ADMIN_RESOURCE = "*"

# Logical actions used by the HTTP backend.
WORKSPACES_READ = "workspaces.read"
TASKS_READ = "tasks.read"
TASKS_WRITE = "tasks.write"
CONVERSATIONS_READ = "conversations.read"
CONVERSATIONS_WRITE = "conversations.write"
TURNS_READ = "turns.read"
TURNS_WRITE = "turns.write"
AGENTS_READ = "agents.read"
AGENTS_WRITE = "agents.write"
KNOWLEDGE_READ = "knowledge.read"
KNOWLEDGE_WRITE = "knowledge.write"
SKILLS_READ = "skills.read"
SKILLS_WRITE = "skills.write"
REVIEW_TRIGGER = "review.trigger"
ANALYSIS_TRIGGER = "analysis.trigger"
ANALYSIS_READ = "analysis.read"
ANALYSIS_APPROVE = "analysis.approve"
GIT_ACTIONS = "git.actions"

#: Action required to submit each durable operation kind. The generic
#: ``POST /operations`` route accepts several kinds, so the kind-specific
#: action is resolved from the request body by the handler.
OPERATION_KIND_ACTIONS: Mapping[str, str] = {
    "review": REVIEW_TRIGGER,
    "analysis": ANALYSIS_TRIGGER,
    "git.commit_push": GIT_ACTIONS,
    "git.rollback": GIT_ACTIONS,
}

#: Owner value recorded for operations submitted by an administrative caller.
ADMIN_OWNER = "admin"


def action_for_operation_kind(kind: str) -> str | None:
    """Return the backend action required to submit ``kind``, or ``None``.

    An unknown kind has no mapped action, so a scoped execution token may
    never submit it; only an administrative caller is unrestricted.
    """
    return OPERATION_KIND_ACTIONS.get(kind)


def execution_owner(agent_id: str, label: str | None = None) -> str:
    """Return a stable, non-secret owner identity for an execution.

    The value is persisted with the operation instead of the bearer token so
    a restart can resolve ownership for status lookup and orphan recovery
    without ever serializing an administrative credential into operation
    JSON. ``agent_id`` is a profile identifier, never a credential.
    """
    return f"execution:{agent_id}|{label or ''}"


def owner_for_grant(grant: "ExecutionGrant | None") -> str:
    """Return the owner identity implied by a resolved execution grant."""
    if grant is None:
        return ADMIN_OWNER
    return execution_owner(grant.agent_id, grant.label)


#: Tool name -> backend action. Tools absent from this mapping are local
#: (filesystem or session-file) tools and grant no backend action. Knowledge,
#: skill, clarification, attachment, archive, OpenViking and trigger tools
#: all operate on local files or on the parent process and therefore grant no
#: HTTP authority here. A local tool must never be mapped to an action: that
#: would silently widen a profile's raw-HTTP authority beyond the operations
#: its tools actually perform. T01/T05 can add an action for a new
#: HTTP-backed tool.
ACTIONS_FOR_TOOLS: Mapping[str, str] = {
    # The universal tool carries universal authority: an execution grant
    # holding it is admin-equivalent, exactly what the "*" marker means
    # everywhere else in this model.
    "call_api": ADMIN_RESOURCE,
    "list_tasks": TASKS_READ,
    "get_task": TASKS_READ,
    "get_task_logs": TASKS_READ,
    "create_task": TASKS_WRITE,
    "update_task": TASKS_WRITE,
    "resume_task_session": TASKS_WRITE,
    "subscribe_task": TASKS_WRITE,
    "unsubscribe_task": TASKS_WRITE,
    "resolve_workspace": WORKSPACES_READ,
    "list_agents": AGENTS_READ,
    "get_agent": AGENTS_READ,
    "create_agent": AGENTS_WRITE,
    "update_agent": AGENTS_WRITE,
    "delete_knowledge": KNOWLEDGE_WRITE,
    "trigger_review": REVIEW_TRIGGER,
    "trigger_analysis": ANALYSIS_TRIGGER,
    "trigger_commit_push": GIT_ACTIONS,
    "trigger_rollback": GIT_ACTIONS,
    "list_analysis_reports": ANALYSIS_READ,
    "get_analysis_report": ANALYSIS_READ,
    "list_recommendations": ANALYSIS_READ,
    "preview_recommendation": ANALYSIS_READ,
    "approve_recommendation": ANALYSIS_APPROVE,
    "apply_recommendation": ANALYSIS_APPROVE,
}

#: Actions that require broad (wildcard-resource) authority because they
#: create, route or mutate platform objects rather than a single task.
MANAGEMENT_ACTIONS: frozenset[str] = frozenset(
    {
        TASKS_WRITE,
        AGENTS_WRITE,
        KNOWLEDGE_WRITE,
        SKILLS_WRITE,
        GIT_ACTIONS,
        REVIEW_TRIGGER,
        ANALYSIS_TRIGGER,
        ANALYSIS_APPROVE,
    }
)


def resources_for_execution(
    actions: Iterable[str],
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> frozenset[str]:
    """Return the resource scope for an execution with ``actions``.

    A task-scoped execution stays scoped to that exact task (and its agent)
    even when its profile exposes management tools such as ``update_task`` or
    ``trigger_review``; the management action alone must never widen it to the
    wildcard resource or it could mutate a foreign task through raw HTTP. A
    conversation/turn-scoped execution with no task identity keeps the
    wildcard authority its management tools require (for example the secretary
    creating tasks).
    """
    action_set = frozenset(actions)
    resources: set[str] = set()
    if task_id:
        resources.add(f"task:{task_id}")
    if agent_id:
        resources.add(f"agent:{agent_id}")
    if task_id is None and action_set & MANAGEMENT_ACTIONS:
        return frozenset({ADMIN_RESOURCE})
    if not resources:
        # A purely read-scoped execution with no identity cannot safely be
        # constrained; refuse to issue an implied broad grant by scoping to
        # nothing. The middleware then denies every declared route.
        return frozenset()
    return frozenset(resources)


def actions_for_tools(tools: Iterable[str] | None) -> frozenset[str]:
    """Return the set of backend actions granted by ``tools``.

    Unknown tools are ignored: a profile cannot widen its own grant by naming
    an arbitrary tool. The result is derived only from configured tools, not
    from the agent's role or name.
    """
    return frozenset(
        ACTIONS_FOR_TOOLS[name]
        for name in (tools or ())
        if name in ACTIONS_FOR_TOOLS
    )


@dataclasses.dataclass(frozen=True)
class ExecutionGrant:
    """One execution-scoped credential and its exact authority."""

    token: str
    agent_id: str
    actions: frozenset[str]
    resources: frozenset[str]
    created_at: float
    label: str | None = None

    @property
    def is_admin(self) -> bool:
        """Administrative grants are never issued by this module."""
        return ADMIN_RESOURCE in self.resources and ADMIN_RESOURCE in self.actions

    def to_document(self) -> dict[str, Any]:
        """Return a credential-free description for logs and tests."""
        return {
            "agent_id": self.agent_id,
            "actions": sorted(self.actions),
            "resources": sorted(self.resources),
            "label": self.label,
        }


@dataclasses.dataclass(frozen=True)
class Requirement:
    """Action and optional resource an HTTP route needs.

    ``deferred`` routes carry no generic requirement: the token is still
authenticated, but the route handler resolves and enforces the action and
resource itself (for example ``POST /operations`` where the action depends
on the submitted kind).
    """

    action: str | None = None
    resource: str | None = None
    resource_kind: str | None = None
    deferred: bool = False


# Method, path pattern, action, resource kind. ``None`` resource kind means a
# collection route that a scoped execution may not use.
_ROUTE_RULES: tuple[tuple[str, str, str, str | None], ...] = (
    ("GET", "/workspaces/resolve", WORKSPACES_READ, None),
    ("POST", "/tasks", TASKS_WRITE, None),
    ("POST", "/imports/tasks", TASKS_WRITE, None),
    ("GET", "/tasks", TASKS_READ, None),
    ("GET", "/tasks/counts", TASKS_READ, None),
    ("GET", "/monitor/executions", TASKS_READ, None),
    ("GET", "/tasks/{task_id}", TASKS_READ, "task"),
    ("GET", "/tasks/{task_id}/git/status", TASKS_READ, "task"),
    ("PATCH", "/tasks/{task_id}", TASKS_WRITE, "task"),
    ("POST", "/tasks/{task_id}/interrupt", TASKS_WRITE, "task"),
    ("POST", "/tasks/{task_id}/attachments", TASKS_WRITE, "task"),
    ("POST", "/conversations", CONVERSATIONS_WRITE, None),
    ("GET", "/conversations", CONVERSATIONS_READ, None),
    ("GET", "/conversations/{conversation_id}", CONVERSATIONS_READ, "conversation"),
    ("PATCH", "/conversations/{conversation_id}", CONVERSATIONS_WRITE, "conversation"),
    ("POST", "/conversations/{conversation_id}/talk", CONVERSATIONS_WRITE, "conversation"),
    (
        "POST",
        "/conversations/{conversation_id}/messages",
        CONVERSATIONS_WRITE,
        "conversation",
    ),
    (
        "GET",
        "/conversations/{conversation_id}/messages",
        CONVERSATIONS_READ,
        "conversation",
    ),
    (
        "POST",
        "/conversations/{conversation_id}/turns",
        TURNS_WRITE,
        "conversation",
    ),
    (
        "GET",
        "/telegram/links/{chat_id}/{telegram_message_id}",
        CONVERSATIONS_READ,
        None,
    ),
    ("GET", "/turns/{turn_id}", TURNS_READ, "turn"),
    ("GET", "/tasks/{task_id}/analysis", ANALYSIS_READ, "task"),
    ("GET", "/tasks/{task_id}/review", TASKS_READ, "task"),
    ("GET", "/analysis/{report_id}", ANALYSIS_READ, None),
    ("GET", "/analysis/{report_id}/recommendations", ANALYSIS_READ, None),
    ("GET", "/recommendations", ANALYSIS_READ, None),
    ("GET", "/recommendations/{recommendation_id}", ANALYSIS_READ, None),
    ("POST", "/recommendations/{recommendation_id}/approve", ANALYSIS_APPROVE, None),
    ("POST", "/recommendations/{recommendation_id}/apply", ANALYSIS_APPROVE, None),
    ("POST", "/agents", AGENTS_WRITE, None),
    ("GET", "/agents", AGENTS_READ, None),
    ("GET", "/agents/{agent_id}", AGENTS_READ, "agent"),
    ("GET", "/agents/{agent_id}/sessions", AGENTS_READ, "agent"),
    ("GET", "/agents/{agent_id}/revisions", AGENTS_READ, "agent"),
    ("GET", "/agents/{agent_id}/revisions/{revision}", AGENTS_READ, "agent"),
    ("PATCH", "/agents/{agent_id}", AGENTS_WRITE, "agent"),
    ("DELETE", "/agents/{agent_id}", AGENTS_WRITE, "agent"),
    ("GET", "/knowledge", KNOWLEDGE_READ, None),
    ("GET", "/knowledge/{path}", KNOWLEDGE_READ, None),
    ("PUT", "/knowledge/{path}", KNOWLEDGE_WRITE, None),
    ("DELETE", "/knowledge/{path}", KNOWLEDGE_WRITE, None),
    ("POST", "/ask", CONVERSATIONS_WRITE, None),
    ("POST", "/conversations/ask", CONVERSATIONS_WRITE, None),
    ("GET", "/tasks/{task_id}/artifacts", TASKS_READ, "task"),
    ("GET", "/tasks/{task_id}/artifacts/{artifact_name}", TASKS_READ, "task"),
    ("GET", "/tasks/{task_id}/session", TASKS_READ, "task"),
)


def _match(pattern: str, path: str) -> str | None:
    """Return the captured value for a single-parameter pattern, else None.

    Only one ``{name}`` parameter is used per resource rule; the captured
    value is the whole remaining path segment. Patterns without a parameter
    must match the path exactly.
    """
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    captures: list[str] = []
    if pattern_parts and pattern_parts[-1] in ("{path}", "{artifact_name}"):
        prefix_len = len(pattern_parts) - 1
        if len(path_parts) <= prefix_len:
            return None
        for expected, actual in zip(pattern_parts[:prefix_len], path_parts[:prefix_len]):
            if expected.startswith("{") and expected.endswith("}"):
                captures.append(actual)
            elif expected != actual:
                return None
        captures.append("/".join(path_parts[prefix_len:]))
        return captures[0] if captures else ""
    if len(pattern_parts) != len(path_parts):
        return None
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}"):
            captures.append(actual)
        elif expected != actual:
            return None
    return captures[0] if captures else ""


def resolve_requirement(method: str, path: str) -> Requirement | None:
    """Map an HTTP request to its required action and resource.

    Returns ``None`` for paths that are public or unknown. ``/health`` is
    public by design. An unknown route is treated as ``None`` here because
    the router itself returns ``404`` before any handler runs; authenticated
    execution tokens still need a matching grant for every declared route.
    """
    method = method.upper()
    if path == "/health":
        return None
    path = path.rstrip("/") or "/"
    if method in {"POST", "DELETE"} and _match("/tasks/{task_id}/subscriptions", path) is not None:
        return Requirement(deferred=True)
    # The operations routes carry several kinds and an ownership dimension
    # the URL alone cannot express. They authenticate here and defer the
    # concrete action/resource decision to the handler.
    if path == "/operations":
        if method in ("POST", "GET"):
            return Requirement(deferred=True)
        return None
    if method == "GET" and _match("/operations/{operation_id}", path) is not None:
        return Requirement(deferred=True)
    for rule_method, pattern, action, resource_kind in _ROUTE_RULES:
        if rule_method != method:
            continue
        captured = _match(pattern, path)
        if captured is None:
            continue
        resource = (
            f"{resource_kind}:{captured}" if resource_kind is not None else None
        )
        return Requirement(
            action=action,
            resource=resource,
            resource_kind=resource_kind,
        )
    return None


class ExecutionPermissions:
    """Thread-safe in-memory registry of execution-scoped grants.

    Grants live for the lifetime of the server process. An execution and its
    server share that lifetime, so persistence is deliberately not required
    here; T01's durable operations can re-issue a grant when they resume an
    execution. Revocation supports explicit cleanup and tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: dict[str, ExecutionGrant] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)

    @property
    def enabled(self) -> bool:
        """Whether any scoped grant has been issued."""
        return len(self) > 0

    def issue(
        self,
        *,
        agent_id: str,
        actions: Iterable[str],
        resources: Iterable[str],
        label: str | None = None,
        token: str | None = None,
    ) -> ExecutionGrant:
        """Issue a new unpredictable token for one execution."""
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id must be a non-empty string")
        grant = ExecutionGrant(
            token=token or secrets.token_urlsafe(32),
            agent_id=agent_id,
            actions=frozenset(actions),
            resources=frozenset(resources),
            created_at=time.time(),
            label=label,
        )
        if grant.token in ("", ADMIN_RESOURCE):
            raise ValueError("execution tokens must be non-empty non-wildcard strings")
        with self._lock:
            if grant.token in self._grants:
                raise ValueError("execution token already exists")
            self._grants[grant.token] = grant
        return grant

    def revoke(self, token: str) -> bool:
        """Remove one grant; returns whether it existed."""
        with self._lock:
            return self._grants.pop(token, None) is not None

    def resolve(self, token: str) -> ExecutionGrant | None:
        """Return the grant for ``token``, or ``None``."""
        if not token:
            return None
        with self._lock:
            return self._grants.get(token)

    def authorize(
        self,
        token: str,
        action: str,
        resource: str | None = None,
    ) -> bool:
        """Return whether ``token`` may perform ``action`` on ``resource``.

        A collection route (``resource is None``) requires the wildcard
        resource, so a task-scoped execution can never enumerate every task.
        A concrete resource requires either that exact resource or the
        wildcard.
        """
        grant = self.resolve(token)
        if grant is None:
            return False
        if action not in grant.actions and ADMIN_RESOURCE not in grant.actions:
            return False
        if resource is None:
            return ADMIN_RESOURCE in grant.resources
        return (
            ADMIN_RESOURCE in grant.resources or resource in grant.resources
        )
