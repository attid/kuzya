"""Async HTTP client for interacting with the Kuzya backend API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
import httpx


class BackendError(Exception):
    """Base exception for errors returned by the Kuzya backend."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Backend returned HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class BackendClient:
    """Async client wrapping the Kuzya FastAPI endpoints."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        api_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._external_client = client is not None
        headers = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=headers
        )

    async def close(self) -> None:
        """Close the underlying HTTP client if owned."""
        if not self._external_client:
            await self._client.aclose()

    async def __aenter__(self) -> "BackendClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        req_headers = dict(headers or {})
        if self.api_token and "Authorization" not in req_headers:
            req_headers["Authorization"] = f"Bearer {self.api_token}"

        response = await self._client.request(
            method,
            url,
            json=json,
            params=params,
            headers=req_headers if req_headers else None,
        )
        if not response.is_success:
            detail = response.text
            try:
                detail_json = response.json()
                if isinstance(detail_json, dict) and "detail" in detail_json:
                    detail = str(detail_json["detail"])
            except Exception:
                pass
            raise BackendError(response.status_code, detail)
        return response.json()

    # -- Health --

    async def get_health(self) -> dict[str, str]:
        return await self._request("GET", "/health")

    # -- Tasks --

    async def subscribe_task(self, task_id: str, conversation_id: str, *, instructions: str = "", cancel: bool = False) -> dict[str, Any]:
        return await self._request(
            "DELETE" if cancel else "POST", f"/tasks/{task_id}/subscriptions",
            json={"conversation_id": conversation_id, "instructions": instructions},
        )

    async def create_task(
        self,
        title: str,
        *,
        description: str | None = None,
        status: str = "incoming",
        workspace: str | None = None,
        metadata: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "status": status,
            "metadata": metadata or {},
        }
        if description is not None:
            payload["description"] = description
        if workspace is not None:
            payload["workspace"] = workspace
        if id is not None:
            payload["id"] = id
        return await self._request("POST", "/tasks", json=payload)

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        status_not_in: Sequence[str] | None = None,
        workspace: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"offset": offset}
        if status is not None:
            params["status"] = status
        if status_not_in is not None:
            params["status_not_in"] = ",".join(status_not_in)
        if workspace is not None:
            params["workspace"] = workspace
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/tasks", params=params)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def interrupt_task(self, task_id: str) -> dict[str, Any]:
        """Request a manual stop of a running task execution."""
        return await self._request("POST", f"/tasks/{task_id}/interrupt")

    async def get_task_counts(self) -> dict[str, int]:
        """Return task counts keyed by status."""
        return await self._request("GET", "/tasks/counts")

    async def get_execution_monitor(self) -> dict[str, Any]:
        """Return running executions and queue counts."""
        return await self._request("GET", "/monitor/executions")

    async def patch_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        workspace: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = status
        if workspace is not None:
            payload["workspace"] = workspace
        if metadata is not None:
            payload["metadata"] = metadata
        return await self._request("PATCH", f"/tasks/{task_id}", json=payload)

    # -- Conversations --

    async def create_conversation(
        self,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"metadata": metadata or {}}
        if title is not None:
            payload["title"] = title
        if id is not None:
            payload["id"] = id
        return await self._request("POST", "/conversations", json=payload)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/conversations/{conversation_id}")

    async def patch_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        return await self._request(
            "PATCH", f"/conversations/{conversation_id}", json=payload
        )

    async def _conversation_talk_action(
        self, conversation_id: str, action: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/conversations/{conversation_id}/talk",
            json={"action": action},
        )

    async def start_talk(self, conversation_id: str) -> dict[str, Any]:
        """Activate a direct-agent talk or mark it pending if busy."""
        return await self._conversation_talk_action(
            conversation_id, "start"
        )

    async def request_end_talk(
        self, conversation_id: str
    ) -> dict[str, Any]:
        """Stop accepting new turns while keeping the workspace lock."""
        return await self._conversation_talk_action(
            conversation_id, "request_end"
        )

    async def finalize_talk(self, conversation_id: str) -> dict[str, Any]:
        """Release the lock only when no pending/running turn remains."""
        return await self._conversation_talk_action(
            conversation_id, "finalize"
        )

    async def get_telegram_link(
        self, chat_id: int, telegram_message_id: int
    ) -> dict[str, Any] | None:
        """Resolve one Telegram message to its conversation link, or ``None``.

        ``None`` means no durable link exists for the chat/message pair — the
        caller decides how to explain an unroutable Reply.
        """
        try:
            return await self._request(
                "GET", f"/telegram/links/{chat_id}/{telegram_message_id}"
            )
        except BackendError as error:
            if error.status_code == 404:
                return None
            raise

    async def list_conversations(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/conversations", params=params)

    # -- Agents --

    async def list_agents(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/agents",
            params={"include_archived": str(include_archived).lower()},
        )

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/agents/{agent_id}")

    # -- Messages --

    async def append_message(
        self,
        conversation_id: str,
        text: str,
        *,
        role: str = "user",
        source: str = "telegram",
        external_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "role": role,
            "source": source,
            "metadata": metadata or {},
        }
        if external_id is not None:
            payload["external_id"] = external_id
        return await self._request(
            "POST", f"/conversations/{conversation_id}/messages", json=payload
        )

    async def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        return await self._request(
            "GET", f"/conversations/{conversation_id}/messages", params=params
        )

    # -- Turns --

    async def create_turn(
        self,
        conversation_id: str,
        input_message_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "input_message_id": input_message_id,
            "metadata": metadata or {},
        }
        return await self._request(
            "POST", f"/conversations/{conversation_id}/turns", json=payload
        )

    async def get_turn(self, turn_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/turns/{turn_id}")

    # -- Operations and analysis --

    async def submit_operation(
        self,
        kind: str,
        *,
        target_id: str | None = None,
        input: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a durable operation and return its record.

        Submission alone is not a result: the returned ``status`` may still be
        ``pending``. Callers inspect ``get_operation`` for the outcome.
        """
        payload: dict[str, Any] = {
            "kind": kind,
            "input": input or {},
            "metadata": metadata or {},
        }
        if target_id is not None:
            payload["target_id"] = target_id
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/operations", json=payload)

    async def trigger_analysis(
        self, task_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Submit the durable ``analysis`` operation for a task."""
        return await self.submit_operation(
            "analysis",
            target_id=task_id,
            input={},
            idempotency_key=idempotency_key,
        )

    async def get_operation(self, operation_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/operations/{operation_id}")

    async def list_analysis_reports(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        return await self._request("GET", f"/tasks/{task_id}/analysis")

    async def get_analysis_report(self, report_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/analysis/{report_id}")

    async def list_report_recommendations(
        self, report_id: str
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/analysis/{report_id}/recommendations"
        )

    async def list_recommendations(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        target_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if task_id is not None:
            params["task_id"] = task_id
        if status is not None:
            params["status"] = status
        if target_kind is not None:
            params["target_kind"] = target_kind
        return await self._request("GET", "/recommendations", params=params)

    async def preview_recommendation(
        self, recommendation_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"/recommendations/{recommendation_id}"
        )

    async def approve_recommendation(
        self, recommendation_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"/recommendations/{recommendation_id}/approve"
        )

    async def apply_recommendation(
        self, recommendation_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"/recommendations/{recommendation_id}/apply"
        )
