"""T02 integration: kind, target and ownership authorization for operations.

These tests use the real FastAPI middleware, the real execution-permission
registry and the real migrated file database. They deliberately populate
unrelated operations so a denial cannot be explained by an empty queue, and
they exercise raw HTTP with scoped bearer tokens rather than a bypass.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.permissions import (
    ADMIN_OWNER,
    ADMIN_RESOURCE,
    GIT_ACTIONS,
    REVIEW_TRIGGER,
    ExecutionPermissions,
    actions_for_tools,
    execution_owner,
    resources_for_execution,
)
from kuzya.storage import Database, OperationRepository

ADMIN_TOKEN = "administrative-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _client(migrated_db: Path, permissions: ExecutionPermissions) -> TestClient:
    app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={ADMIN_TOKEN},
        permissions=permissions,
    )
    return TestClient(app)


def _scoped(
    permissions: ExecutionPermissions,
    tools: list[str],
    *,
    agent_id: str,
    task_id: str | None = None,
    label: str | None = None,
    resources: frozenset[str] | None = None,
) -> str:
    actions = actions_for_tools(tools)
    if resources is None:
        resources = resources_for_execution(
            actions, task_id=task_id, agent_id=agent_id
        )
    grant = permissions.issue(
        agent_id=agent_id,
        actions=actions,
        resources=resources,
        label=label,
    )
    return grant.token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_review_grant_cannot_submit_git_through_generic_route(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    review_token = _scoped(
        permissions, ["trigger_review"], agent_id="reviewer", label="task:t1"
    )
    with _client(migrated_db, permissions) as client:
        review = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1"},
            headers=_bearer(review_token),
        )
        assert review.status_code == 202, review.text

        # Same route, different kind: the review grant has no git authority.
        git = client.post(
            "/operations",
            json={"kind": "git.commit_push", "target_id": "t1"},
            headers=_bearer(review_token),
        )
        assert git.status_code == 403, git.text
        assert "scope" in git.json()["detail"]

        analysis = client.post(
            "/operations",
            json={"kind": "analysis", "target_id": "t1"},
            headers=_bearer(review_token),
        )
        assert analysis.status_code == 403, analysis.text

        # A git-scoped grant has the mirror-image restriction.
        git_token = _scoped(
            permissions, ["trigger_commit_push"], agent_id="developer", label="task:t1"
        )
        assert actions_for_tools(["trigger_commit_push"]) == frozenset({GIT_ACTIONS})
        allowed_git = client.post(
            "/operations",
            json={"kind": "git.commit_push", "target_id": "t1"},
            headers=_bearer(git_token),
        )
        assert allowed_git.status_code == 202, allowed_git.text
        denied_review = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1"},
            headers=_bearer(git_token),
        )
        assert denied_review.status_code == 403, denied_review.text

        # The administrative token is unrestricted on the same route.
        admin_git = client.post(
            "/operations",
            json={"kind": "git.commit_push", "target_id": "t1"},
            headers=ADMIN_HEADERS,
        )
        assert admin_git.status_code == 202, admin_git.text


def test_scoped_grant_enforces_target_resource(migrated_db: Path) -> None:
    permissions = ExecutionPermissions()
    # The real factory output for a task-scoped review grant is limited to
    # exactly task t1 even though review.trigger is a management action.
    actions = actions_for_tools(["trigger_review"])
    resources = resources_for_execution(
        actions, task_id="t1", agent_id="reviewer"
    )
    assert resources == frozenset({"task:t1", "agent:reviewer"})
    token = _scoped(
        permissions,
        ["trigger_review"],
        agent_id="reviewer",
        task_id="t1",
        label="task:t1",
        resources=resources,
    )
    with _client(migrated_db, permissions) as client:
        own = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1"},
            headers=_bearer(token),
        )
        assert own.status_code == 202, own.text

        foreign = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t2"},
            headers=_bearer(token),
        )
        assert foreign.status_code == 403, foreign.text

        # A collection-shaped submission without a target needs wildcard.
        targetless = client.post(
            "/operations",
            json={"kind": "review"},
            headers=_bearer(token),
        )
        assert targetless.status_code == 403, targetless.text


def test_unknown_kind_is_denied_for_scoped_and_allowed_for_admin(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    token = _scoped(
        permissions,
        ["trigger_review"],
        agent_id="reviewer",
        task_id="t1",
        label="task:t1",
    )
    with _client(migrated_db, permissions) as client:
        denied = client.post(
            "/operations",
            json={"kind": "custom.future", "target_id": "t1"},
            headers=_bearer(token),
        )
        assert denied.status_code == 403, denied.text

        allowed = client.post(
            "/operations",
            json={"kind": "custom.future", "target_id": "t1"},
            headers=ADMIN_HEADERS,
        )
        assert allowed.status_code == 202, allowed.text


def test_scoped_read_cannot_enumerate_or_read_foreign_operations(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    first_token = _scoped(
        permissions, ["trigger_review"], agent_id="first", label="task:t1"
    )
    second_token = _scoped(
        permissions, ["trigger_review"], agent_id="second", label="task:t2"
    )
    with _client(migrated_db, permissions) as client:
        # Populate operations from unrelated principals so the queue is not
        # empty when the denials are asserted.
        first = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1"},
            headers=_bearer(first_token),
        )
        assert first.status_code == 202, first.text
        second = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t2"},
            headers=_bearer(second_token),
        )
        assert second.status_code == 202, second.text
        first_id = first.json()["id"]
        second_id = second.json()["id"]

        # Scoped callers can never enumerate the queue.
        assert (
            client.get("/operations", headers=_bearer(first_token)).status_code
            == 403
        )
        # Each can read its own operation...
        owned = client.get(
            f"/operations/{first_id}", headers=_bearer(first_token)
        )
        assert owned.status_code == 200, owned.text
        assert owned.json()["id"] == first_id
        # ...but a foreign id is reported as not found, not disclosed.
        foreign = client.get(
            f"/operations/{second_id}", headers=_bearer(first_token)
        )
        assert foreign.status_code == 404, foreign.text
        # Authentication is still required at all.
        assert client.get(f"/operations/{first_id}").status_code == 401

    # Ownership is persisted on the record, never in the JSON payload.
    database = Database.from_path(migrated_db)
    try:
        repository = OperationRepository(database)
        stored_first = repository.get(first_id)
        stored_second = repository.get(second_id)
        assert stored_first is not None and stored_second is not None
        assert stored_first.owner == execution_owner("first", "task:t1")
        assert stored_second.owner == execution_owner("second", "task:t2")
    finally:
        database.dispose()

    with _client(migrated_db, permissions) as client:
        # Administrative callers see the whole queue and every record.
        listed = client.get("/operations", headers=ADMIN_HEADERS)
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()} >= {first_id, second_id}
        assert (
            client.get(f"/operations/{second_id}", headers=ADMIN_HEADERS).status_code
            == 200
        )


def test_admin_owner_and_no_token_leak_into_operation_json(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    with _client(migrated_db, permissions) as client:
        submitted = client.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": "t1",
                "input": {"instructions": "check"},
                "metadata": {"note": "reviewed"},
            },
            headers=ADMIN_HEADERS,
        )
        assert submitted.status_code == 202, submitted.text
        operation_id = submitted.json()["id"]

    database = Database.from_path(migrated_db)
    try:
        stored = OperationRepository(database).get(operation_id)
        assert stored is not None
        assert stored.owner == ADMIN_OWNER
        serialized = json.dumps(
            {
                "input": stored.input,
                "metadata": stored.meta,
                "result": stored.result,
                "owner": stored.owner,
            }
        )
        assert ADMIN_TOKEN not in serialized
        assert "Bearer" not in serialized
    finally:
        database.dispose()


def test_scoped_grant_without_operation_tools_is_denied(migrated_db: Path) -> None:
    permissions = ExecutionPermissions()
    # A filesystem-only execution carries no platform action at all.
    token = _scoped(
        permissions,
        ["read", "bash"],
        agent_id="reviewer",
        task_id="t1",
        label="task:t1",
    )
    with _client(migrated_db, permissions) as client:
        denied = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1"},
            headers=_bearer(token),
        )
        assert denied.status_code == 403, denied.text


def test_wildcard_scoped_grant_submits_all_mapped_kinds(
    migrated_db: Path,
) -> None:
    """A management grant with wildcard resources reaches every mapped kind."""
    permissions = ExecutionPermissions()
    actions = actions_for_tools(
        ["trigger_review", "trigger_analysis", "trigger_commit_push", "trigger_rollback"]
    )
    assert actions == frozenset({REVIEW_TRIGGER, "analysis.trigger", GIT_ACTIONS})
    grant = permissions.issue(
        agent_id="secretary",
        actions=actions,
        resources=frozenset({ADMIN_RESOURCE}),
        label="conversation:conv-1",
    )
    with _client(migrated_db, permissions) as client:
        for kind in ("review", "analysis", "git.commit_push", "git.rollback"):
            response = client.post(
                "/operations",
                json={"kind": kind, "target_id": "t1"},
                headers=_bearer(grant.token),
            )
            assert response.status_code == 202, (kind, response.text)
        # A later, separate call with the same kind/target still creates a
        # new operation when the idempotency key differs.
        first = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1", "idempotency_key": "k1"},
            headers=_bearer(grant.token),
        )
        second = client.post(
            "/operations",
            json={"kind": "review", "target_id": "t1", "idempotency_key": "k2"},
            headers=_bearer(grant.token),
        )
        assert first.json()["id"] != second.json()["id"]


def test_analysis_and_recommendation_routes_enforce_action_and_resource(
    migrated_db: Path,
) -> None:
    """Raw HTTP cannot widen an execution beyond its granted analysis scope."""
    permissions = ExecutionPermissions()
    # A task-scoped read grant: allowed on its own task's analysis list, but
    # denied on report/recommendation ids that need wildcard authority.
    read_token = _scoped(
        permissions,
        ["list_analysis_reports"],
        agent_id="reviewer",
        label="task:t1",
        resources=frozenset({"task:t1"}),
    )
    # Approving is a management action requiring wildcard authority and the
    # exact analysis.approve action, not the analysis.trigger action.
    approve_token = _scoped(
        permissions, ["approve_recommendation"], agent_id="secretary"
    )
    trigger_token = _scoped(
        permissions, ["trigger_analysis"], agent_id="secretary"
    )
    with _client(migrated_db, permissions) as client:
        allowed_read = client.get(
            "/tasks/t1/analysis", headers=_bearer(read_token)
        )
        assert allowed_read.status_code == 200, allowed_read.text
        # The same read grant cannot reach an unrelated report by id.
        assert (
            client.get(
                "/analysis/other", headers=_bearer(read_token)
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/recommendations", headers=_bearer(read_token)
            ).status_code
            == 403
        )
        # analysis.trigger is not analysis.approve.
        assert (
            client.post(
                "/recommendations/r1/approve", headers=_bearer(trigger_token)
            ).status_code
            == 403
        )
        # The approve grant passes authorization; the missing recommendation is
        # a domain 404, never a 403.
        assert client.post(
            "/recommendations/r1/approve", headers=_bearer(approve_token)
        ).status_code == 404
        assert client.post(
            "/recommendations/r1/apply", headers=_bearer(approve_token)
        ).status_code == 404
        # The same grant cannot smuggle a Git operation.
        assert (
            client.post(
                "/operations",
                json={"kind": "git.commit_push", "target_id": "t1"},
                headers=_bearer(approve_token),
            ).status_code
            == 403
        )


# -- unknown paths vs unexposed routes -----------------------------------


def _wildcard_call_api_token(permissions: ExecutionPermissions) -> str:
    """A secretary-shaped grant: ``call_api`` authority, wildcard resources."""
    return _scoped(
        permissions,
        ["call_api", "create_task"],
        agent_id="system-secretary",
    )


def test_unknown_path_is_a_plain_404_for_execution_tokens(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    token = _wildcard_call_api_token(permissions)
    headers = {"Authorization": f"Bearer {token}"}
    with _client(migrated_db, permissions) as client:
        # Guessed paths (there is no /tasks/{id}/logs, no bare /sessions)
        # must be the router's 404, not a misleading scope denial.
        guessed = client.get("/tasks/some-task-id/logs", headers=headers)
        assert guessed.status_code == 404
        bare = client.get("/sessions", headers=headers)
        assert bare.status_code == 404
        # The administrative caller sees the same honest 404.
        assert (
            client.get(
                "/tasks/some-task-id/logs", headers=ADMIN_HEADERS
            ).status_code
            == 404
        )
        # A declared route deliberately unexposed to executions stays 403.
        assert client.get("/mcp", headers=headers).status_code == 403


def test_tasks_counts_is_mapped_for_execution_tokens(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    token = _wildcard_call_api_token(permissions)
    headers = {"Authorization": f"Bearer {token}"}
    with _client(migrated_db, permissions) as client:
        response = client.get("/tasks/counts", headers=headers)
        assert response.status_code == 200
        assert isinstance(response.json(), dict)
