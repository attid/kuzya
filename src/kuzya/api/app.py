"""FastAPI application factory for the durable storage boundary.

The factory never creates, replaces or drops schema. The database file must
already exist and contain the ``tasks``, ``conversations``, ``messages``,
``agent_profiles``, ``turns`` and ``conversation_runtimes`` tables produced by
the Alembic migration chain; otherwise application startup fails with a clear
error telling the operator to run the migration.

An engine is created lazily inside the application lifespan and disposed when
the application stops, so importing this module never touches the database
file. Every request opens its own short-lived session through
:class:`~kuzya.storage.repository.TaskRepository`,
:class:`~kuzya.storage.repository.ConversationRepository` or
:class:`~kuzya.storage.agents.AgentProfileRepository`; sessions are
never shared between requests.

The ``/agents`` endpoints are thin HTTP wrappers over the immutable versioned
agent-profile repository. They are administrative endpoints. Administrative
Bearer tokens receive unrestricted access; execution credentials issued by
:class:`~kuzya.permissions.ExecutionPermissions` are accepted only
for the declared route action and resource, so an agent profile's configured
tools bound what its sandbox can reach even through raw HTTP.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote_to_bytes

import sqlalchemy as sa
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging import bind_log_context
from ..monitor import build_execution_monitor
from ..agent_sessions import list_agent_sessions
from ..review_history import list_review_rounds
from ..analysis import (
    AnalysisError,
    RecommendationConflictError,
    RecommendationService,
)
from ..permissions import (
    ExecutionGrant,
    ExecutionPermissions,
    action_for_operation_kind,
    owner_for_grant,
    resolve_requirement,
)
from ..workspaces import resolve_workspace
from ..secretary import (
    TurnConversationClosedError,
    TurnConversationMismatchError,
    TurnInputMessageNotFoundError,
    TurnInputRoleError,
    TurnProcessor,
)
from ..storage import (
    CORE_SYSTEM_AGENTS,
    AgentProfileNotFoundError,
    AgentProfileRepository,
    AgentProfileRevisionConflictError,
    AnalysisRepository,
    ConversationNotFoundError,
    ConversationRepository,
    Database,
    DuplicateAgentProfileError,
    DuplicateConversationIdError,
    DuplicateTaskIdError,
    MessageConflictError,
    OperationConflictError,
    OperationRepository,
    RecommendationNotFoundError,
    RecommendationStateError,
    ConversationTalkNotFoundError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskRepository,
    WorkspaceTalkRepository,
)
from ..storage.telegram_delivery import TelegramDeliveryRepository
from .schemas import (
    AgentCreate,
    AgentPatch,
    AgentRead,
    AgentSessionRead,
    AnalysisReportRead,
    AskRequest,
    AskResponse,
    ConversationCreate,
    ConversationPatch,
    ConversationRead,
    ConversationTalkRequest,
    KnowledgeDocumentDeleted,
    KnowledgeDocumentRead,
    KnowledgeDocumentWrite,
    KnowledgeListResponse,
    MessageAppendResponse,
    MessageCreate,
    MessageRead,
    OperationRead,
    OperationSubmit,
    RecommendationPreviewRead,
    RecommendationRead,
    ReviewRoundRead,
    TaskArtifactListResponse,
    TaskArtifactRead,
    TaskCreate,
    TaskImportRequest,
    TaskImportResponse,
    TaskPatch,
    TaskRead,
    TaskSubscriptionCreate,
    TelegramMessageLinkRead,
    TurnCreate,
    TurnRead,
    agent_to_read,
    analysis_report_to_read,
    conversation_to_read,
    import_item_to_record,
    message_append_to_response,
    message_to_read,
    operation_to_read,
    recommendation_preview_to_read,
    recommendation_to_read,
    task_to_read,
    turn_to_read,
)
from ..knowledge import (
    KnowledgeBase,
    KnowledgeDocumentNotFoundError,
    KnowledgePathError,
)
from .mcp import attach_mcp_endpoint


ENV_VAR = "KUZYA_DB_URL"

REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger(__name__)


# Tables that must already exist (via the Alembic migration chain) before the
# application accepts traffic. Checking them at startup turns an unmigrated
# database into a clear, early error instead of a later request-time 500.
_REQUIRED_TABLES = (
    "tasks",
    "conversations",
    "messages",
    "agent_profiles",
    "conversation_runtimes",
    "turns",
)

# The operations table arrived in the last migration. It is checked after the
# assignment-migration guard so a database at 0005 still reports the specific
# missing ``tasks.agent_id`` column instead of the new table.
_REQUIRED_OPERATION_TABLES = ("operations",)

# Analysis reports and recommendations arrived after the operations table.
_REQUIRED_ANALYSIS_TABLES = ("analysis_reports", "recommendations")

_MIGRATION_HINT = (
    "Run the migration first: "
    "KUZYA_DB_URL={url} PYTHONPATH=src .venv/bin/alembic upgrade head"
)


def _scope_header(scope: Scope, name: str) -> str | None:
    """Return the first request header value named ``name``."""

    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _request_state(scope: Scope) -> dict[str, object]:
    """Return the mutable request-state mapping for ``scope``."""

    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    return state


def _request_id_for_scope(scope: Scope) -> str:
    """Return the request id already chosen for ``scope`` or choose one.

    An incoming ``X-Request-ID`` is preserved. Blank values and requests
    without the header receive a fresh UUID. The chosen id is stored in the
    request state so outer middleware can still use it after inner context
    bindings have been reset.

    """

    state = _request_state(scope)
    existing = state.get("request_id")
    if isinstance(existing, str) and existing:
        return existing

    header_value = _scope_header(scope, REQUEST_ID_HEADER)
    request_id = header_value.strip() if header_value else ""
    if not request_id:
        request_id = str(uuid.uuid4())
    state["request_id"] = request_id
    return request_id


def _set_response_header(
    message: Message,
    name: str,
    value: str,
) -> None:
    """Set a response header, replacing any existing occurrence."""

    raw_name = name.lower().encode("latin-1")
    headers = [
        (key, header_value)
        for key, header_value in message.get("headers", [])
        if key.lower() != raw_name
    ]
    headers.append((raw_name, value.encode("latin-1")))
    message["headers"] = headers


def _internal_error_response(request_id: str) -> JSONResponse:
    """Build the canonical 500 response for an unhandled exception."""

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


def _log_unhandled_exception(
    exc: Exception,
    scope: Scope,
    request_id: str,
) -> None:
    """Log an escaping exception with its full traceback and context."""

    method = scope.get("method", "")
    path = scope.get("path", "")
    with bind_log_context(request_id=request_id):
        logger.error(
            "unhandled exception method=%s path=%s request_id=%s",
            method,
            path,
            request_id,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "event": "unhandled_exception",
                "method": method,
                "path": path,
                "status_code": 500,
            },
        )


class UnhandledExceptionMiddleware:
    """Convert unexpected route exceptions into a logged 500 response.

    This middleware sits outside the request-correlation middleware, so an
    exception that escapes a route handler is caught before Starlette's
    outer error middleware re-raises it. The process therefore stays alive
    and the next request is served normally. A response that has already
    started cannot be replaced; in that case the exception is re-raised after
    being logged so the server can close the failed response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracking_start(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception as exc:
            request_id = _request_id_for_scope(scope)
            _log_unhandled_exception(exc, scope, request_id)
            if response_started:
                raise
            response = _internal_error_response(request_id)
            await response(scope, receive, send)


class BearerAuthMiddleware:
    """Authenticate Bearer tokens and enforce execution scopes.

    Two credential classes are supported:

    * administrative tokens from ``allowed_tokens`` receive unrestricted
      access and are never placed in a child execution environment; and
    * execution tokens issued by an
      :class:`~kuzya.permissions.ExecutionPermissions` registry are
      accepted only when the resolved route action and resource are inside
      the granted scope. The exact grant is stored in
      ``scope['state']['execution_grant']`` for downstream handlers.

    ``/health`` and OPTIONS requests are always public.

    The authenticated/``require_auth`` decision is fixed when the middleware
    is built and never changes with the number of live execution grants.
    Issuing the first execution token must not silently switch a running
    service from anonymous to protected, and revoking the last token must not
    reopen it. Callers that pass neither administrative tokens nor an
    execution registry stay in explicit open development mode.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_tokens: set[str],
        permissions: ExecutionPermissions | None = None,
        *,
        require_auth: bool | None = None,
    ) -> None:
        self.app = app
        self.allowed_tokens = set(allowed_tokens)
        self.permissions = permissions
        # A stable per-instance decision, independent of the current grant
        # count. Callers may pass it explicitly; otherwise authentication is
        # on whenever any credential source is configured.
        if require_auth is None:
            require_auth = bool(self.allowed_tokens) or permissions is not None
        self.require_auth = bool(require_auth)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Explicit open development mode: no administrative token and no
        # execution registry were configured. The decision is fixed for the
        # lifetime of the middleware, so it never depends on grant count.
        if not self.require_auth:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Exclude /health and OPTIONS (CORS preflight)
        if path == "/health" or method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        auth_header = _scope_header(scope, "authorization")
        token = ""
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

        if not token:
            await self._deny(
                scope,
                receive,
                send,
                status.HTTP_401_UNAUTHORIZED,
                "Unauthorized: invalid or missing Bearer token",
                method,
                path,
                event="unauthorized_access",
            )
            return

        # Administrative credential: unrestricted, outside the registry.
        if token in self.allowed_tokens:
            _set_execution_grant(scope, None)
            await self.app(scope, receive, send)
            return

        grant = (
            self.permissions.resolve(token)
            if self.permissions is not None
            else None
        )
        if grant is None:
            await self._deny(
                scope,
                receive,
                send,
                status.HTTP_401_UNAUTHORIZED,
                "Unauthorized: invalid or missing Bearer token",
                method,
                path,
                event="unauthorized_access",
            )
            return

        requirement = resolve_requirement(method, path)
        if requirement is None:
            if not self._route_exists(scope):
                # An unknown path is not a scope question: forward it and
                # let the router answer a plain 404/405. Reporting it as a
                # missing scope sent agents hunting for rights they already
                # had when they had merely guessed the URL.
                await self.app(scope, receive, send)
                return
            await self._deny(
                scope,
                receive,
                send,
                status.HTTP_403_FORBIDDEN,
                "Forbidden: execution credential lacks the required scope",
                method,
                path,
                event="forbidden_scope",
            )
            return
        if not requirement.deferred:
            allowed = (
                self.permissions is not None
                and requirement.action is not None
                and self.permissions.authorize(
                    token, requirement.action, requirement.resource
                )
            )
            if not allowed:
                await self._deny(
                    scope,
                    receive,
                    send,
                    status.HTTP_403_FORBIDDEN,
                    "Forbidden: execution credential lacks the required scope",
                    method,
                    path,
                    event="forbidden_scope",
                )
                return

        _set_execution_grant(scope, grant)
        await self.app(scope, receive, send)

    def _route_exists(self, scope: Scope) -> bool:
        """Whether the wrapped application declares a route for this request.

        A declared route without a scope rule (``/mcp``, ``/docs``) stays a
        deliberate 403 for execution credentials; a path no route matches is
        not an endpoint at all and becomes the router's 404/405 instead of a
        misleading scope denial. When the route table cannot be reached the
        strict historic denial is kept.
        """
        routes = getattr(scope.get("app"), "routes", None)
        if not routes:
            return True
        for route in routes:
            match, _child_scope = route.matches(scope)
            if match is Match.FULL:
                return True
        return False

    async def _deny(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
        method: str,
        path: str,
        *,
        event: str,
    ) -> None:
        request_id = _request_id_for_scope(scope)
        logger.warning(
            "API access denied method=%s path=%s status=%s request_id=%s",
            method,
            path,
            status_code,
            request_id,
            extra={
                "event": event,
                "path": path,
                "method": method,
            },
        )
        headers = {REQUEST_ID_HEADER: request_id}
        if status_code == status.HTTP_401_UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail},
            headers=headers,
        )
        await response(scope, receive, send)


def _set_execution_grant(scope: Scope, grant: ExecutionGrant | None) -> None:
    """Expose the resolved execution grant to downstream handlers."""
    scope.setdefault("state", {})["execution_grant"] = grant


def _execution_grant_for_request(request: Request) -> ExecutionGrant | None:
    """Return the caller's execution grant, or ``None`` for admin/open mode."""
    return getattr(request.state, "execution_grant", None)


def _enforce_operation_submit_scope(
    request: Request,
    kind: str,
    target_id: str | None,
) -> None:
    """Reject a scoped submission whose kind or target is out of scope.

    ``POST /operations`` is a single generic route, so the kind-specific
    action and the concrete ``task:<id>`` resource are resolved here. A grant
    for review therefore cannot smuggle a Git action through the same route,
    and a grant scoped to one task cannot target another.
    """
    grant = _execution_grant_for_request(request)
    if grant is None:
        return
    action = action_for_operation_kind(kind)
    resource = f"task:{target_id}" if target_id else None
    permissions = getattr(request.app.state, "permissions", None)
    if (
        action is None
        or permissions is None
        or not permissions.authorize(grant.token, action, resource)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Forbidden: operation kind is outside the execution "
                "credential scope"
            ),
        )


class RequestCorrelationMiddleware:
    """Bind request id context and log request start/completion."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_for_scope(scope)
        method = scope.get("method", "")
        path = scope.get("path", "")
        started_at = time.perf_counter()
        status_code: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                _set_response_header(
                    message,
                    REQUEST_ID_HEADER,
                    request_id,
                )
            await send(message)

        with bind_log_context(request_id=request_id):
            logger.info(
                "%s %s request started",
                method,
                path,
                extra={
                    "event": "request_start",
                    "method": method,
                    "path": path,
                },
            )
            try:
                await self.app(
                    scope,
                    receive,
                    send_with_request_id,
                )
            finally:
                duration_ms = (time.perf_counter() - started_at) * 1000
                logger.info(
                    "%s %s request completed status_code=%s "
                    "duration_ms=%.3f",
                    method,
                    path,
                    status_code if status_code is not None else 500,
                    duration_ms,
                    extra={
                        "event": "request_complete",
                        "method": method,
                        "path": path,
                        "status_code": (
                            status_code if status_code is not None else 500
                        ),
                        "duration_ms": duration_ms,
                    },
                )


def _resolve_database_url(database_url: str | None) -> str:
    """Resolve the database URL from the argument or the environment."""
    url = database_url or os.environ.get(ENV_VAR)
    if not url:
        raise RuntimeError(
            "No database URL configured. Pass database_url=... to create_app "
            f"or set the {ENV_VAR} environment variable."
        )
    return url


def _sqlite_file_path(url: str) -> Path | None:
    """Return the file path for a file-backed SQLite URL, else ``None``."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the engine on startup and dispose it on shutdown."""
    url: str = app.state.database_url
    path = _sqlite_file_path(url)
    if path is not None and not path.exists():
        raise RuntimeError(
            f"Database file {path} does not exist. "
            + _MIGRATION_HINT.format(url=url)
        )

    database = Database(url)
    inspector = sa.inspect(database.engine)
    existing_tables = set(inspector.get_table_names())
    missing = {
        name for name in _REQUIRED_TABLES if name not in existing_tables
    }
    if missing:
        database.dispose()
        raise RuntimeError(
            f"Database {url} is missing required tables: "
            f"{', '.join(sorted(missing))}. "
            + _MIGRATION_HINT.format(url=url)
        )
    # The assignment-migration check stays specific so a database created at
    # 0005 reports the exact missing column instead of a generic table list.
    task_columns = {
        column["name"] for column in inspector.get_columns("tasks")
    }
    if "agent_id" not in task_columns:
        database.dispose()
        raise RuntimeError(
            "Database is missing tasks.agent_id. "
            + _MIGRATION_HINT.format(url=url)
        )
    missing_operations = {
        name
        for name in _REQUIRED_OPERATION_TABLES
        if name not in existing_tables
    }
    if missing_operations:
        database.dispose()
        raise RuntimeError(
            f"Database {url} is missing required tables: "
            f"{', '.join(sorted(missing_operations))}. "
            + _MIGRATION_HINT.format(url=url)
        )
    missing_analysis = {
        name
        for name in _REQUIRED_ANALYSIS_TABLES
        if name not in existing_tables
    }
    if missing_analysis:
        database.dispose()
        raise RuntimeError(
            f"Database {url} is missing required tables: "
            f"{', '.join(sorted(missing_analysis))}. "
            + _MIGRATION_HINT.format(url=url)
        )
    app.state.database = database
    app.state.repository = TaskRepository(database)
    app.state.conversation_repository = ConversationRepository(database)
    app.state.agent_repository = AgentProfileRepository(database)
    app.state.operation_repository = OperationRepository(database)
    app.state.analysis_repository = AnalysisRepository(database)
    app.state.turn_processor = TurnProcessor(database)
    mcp_endpoint = getattr(app.state, "mcp_endpoint", None)
    async with AsyncExitStack() as stack:
        if mcp_endpoint is not None:
            await stack.enter_async_context(mcp_endpoint)
        try:
            # A killed or restarted process leaves turns ``running`` with no live
            # owner. Fail them and release their reservations before serving
            # traffic so no conversation stays permanently blocked.
            result = app.state.turn_processor.reconcile_startup_orphans()
            if result.turn_ids:
                logger.warning(
                    "startup reconciliation failed %d orphaned turn(s)",
                    len(result.turn_ids),
                    extra={
                        "event": "startup_reconciliation",
                        "reconciled_turns": len(result.turn_ids),
                        "reconciled_conversations": len(
                            result.conversation_ids
                        ),
                    },
                )
            else:
                logger.info(
                    "startup reconciliation found no orphaned turns",
                    extra={
                        "event": "startup_reconciliation",
                        "reconciled_turns": 0,
                    },
                )
            yield
        finally:
            database.dispose()


def _resolve_api_tokens(
    tokens: Sequence[str] | set[str] | None = None,
) -> set[str]:
    """Resolve allowed Bearer tokens from argument or environment."""
    if tokens is not None:
        return {t.strip() for t in tokens if t.strip()}
    raw = os.environ.get("KUZYA_API_TOKENS", "").strip()
    if not raw:
        raw = os.environ.get("KUZYA_API_TOKEN", "").strip()
    if not raw:
        return set()
    return {t.strip() for t in raw.split(",") if t.strip()}


def create_app(
    database_url: str | None = None,
    api_tokens: Sequence[str] | set[str] | None = None,
    *,
    default_workspace: str | Path | None = None,
    workspace_map: dict[str, str] | None = None,
    default_agent_id: str = "kuzya-developer",
    knowledge_base: KnowledgeBase | None = None,
    permissions: ExecutionPermissions | None = None,
    artifact_root: str | Path | None = None,
    task_process_registry: Any | None = None,
) -> FastAPI:
    """Build a FastAPI application bound to a file-backed SQLite database.

    Args:
        database_url: SQLAlchemy file URL, for example
            ``sqlite:////tmp/kuzya.db``. When omitted, the
            ``KUZYA_DB_URL`` environment variable is used. The
            database must already be migrated to head.
        api_tokens: Optional collection of administrative Bearer tokens. When
            supplied or configured via ``KUZYA_API_TOKENS``, all
            endpoints except ``/health`` require a valid Bearer token.
        permissions: Optional execution-scope registry shared with the
            runtime. Supplying a registry also protects the API from
            startup, before any execution token has been issued; when
            omitted a fresh registry is created and exposed as
            ``app.state.permissions`` but the app stays in explicit open
            development mode unless administrative tokens are configured.
            Execution tokens are accepted only inside their granted
            action/resource scope.
    """
    url = _resolve_database_url(database_url)
    resolved_tokens = _resolve_api_tokens(api_tokens)
    permissions_provided = permissions is not None
    resolved_permissions = (
        permissions if permissions is not None else ExecutionPermissions()
    )
    # Stable per-instance auth decision. An explicitly supplied execution
    # registry (the unified service always supplies one) protects the API
    # from startup through grant revocation, even before any scoped token
    # exists and after the last one is gone. Only a standalone app with no
    # registry and no administrative token remains in explicit open
    # development mode.
    require_auth = bool(resolved_tokens) or permissions_provided

    app = FastAPI(title="Kuzya", version="0.1.0", lifespan=_lifespan)
    app.state.task_process_registry = task_process_registry
    app.state.database_url = url
    app.state.api_tokens = resolved_tokens
    app.state.permissions = resolved_permissions
    app.state.require_auth = require_auth
    app.state.knowledge_base = knowledge_base
    app.state.artifact_root = Path(artifact_root) if artifact_root else None

    # Added inner-to-outer: BearerAuth is inner to RequestCorrelation
    app.add_middleware(
        BearerAuthMiddleware,
        allowed_tokens=resolved_tokens,
        permissions=resolved_permissions,
        require_auth=require_auth,
    )
    app.add_middleware(RequestCorrelationMiddleware)
    app.add_middleware(UnhandledExceptionMiddleware)

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Fallback handler for exceptions outside the resilience middleware."""

        request_id = _request_id_for_scope(request.scope)
        _log_unhandled_exception(exc, request.scope, request_id)
        return _internal_error_response(request_id)

    def get_repository(request: Request) -> TaskRepository:
        return request.app.state.repository

    def get_conversation_repository(
        request: Request,
    ) -> ConversationRepository:
        return request.app.state.conversation_repository

    def get_workspace_talk_repository(
        request: Request,
    ) -> WorkspaceTalkRepository:
        return WorkspaceTalkRepository(request.app.state.database)

    def get_agent_repository(request: Request) -> AgentProfileRepository:
        return request.app.state.agent_repository

    def get_turn_processor(request: Request) -> TurnProcessor:
        return request.app.state.turn_processor

    def get_operation_repository(request: Request) -> OperationRepository:
        return request.app.state.operation_repository

    def get_analysis_repository(request: Request) -> AnalysisRepository:
        return request.app.state.analysis_repository

    def get_recommendation_service(
        request: Request,
    ) -> RecommendationService:
        return RecommendationService(
            request.app.state.database,
            knowledge_base=getattr(request.app.state, "knowledge_base", None),
        )

    def get_knowledge_base(request: Request) -> KnowledgeBase:
        kb = getattr(request.app.state, "knowledge_base", None)
        if kb is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge base not configured",
            )
        return kb

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/knowledge", response_model=KnowledgeListResponse)
    def list_knowledge(
        category: str | None = None,
        kb: KnowledgeBase = Depends(get_knowledge_base),
    ) -> KnowledgeListResponse:
        items = kb.list_documents(category=category)
        return KnowledgeListResponse(items=items, category=category)

    @app.get("/knowledge/{path:path}", response_model=KnowledgeDocumentRead)
    def read_knowledge(
        path: str,
        kb: KnowledgeBase = Depends(get_knowledge_base),
    ) -> KnowledgeDocumentRead:
        content = kb.read_document(path)
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"knowledge document not found: {path}",
            )
        return KnowledgeDocumentRead(path=path, content=content)

    @app.put("/knowledge/{path:path}", response_model=KnowledgeDocumentRead)
    def write_knowledge(
        path: str,
        payload: KnowledgeDocumentWrite,
        kb: KnowledgeBase = Depends(get_knowledge_base),
    ) -> KnowledgeDocumentRead:
        try:
            kb.write_document(
                relative_path=path,
                content=payload.content,
                commit_message=payload.commit_message,
            )
        except Exception as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(err),
            ) from err
        content = kb.read_document(path)
        return KnowledgeDocumentRead(path=path, content=content or payload.content)

    @app.delete("/knowledge/{path:path}", response_model=KnowledgeDocumentDeleted)
    def delete_knowledge(
        path: str,
        kb: KnowledgeBase = Depends(get_knowledge_base),
    ) -> KnowledgeDocumentDeleted:
        try:
            kb.delete_document(path)
        except KnowledgeDocumentNotFoundError as err:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(err),
            ) from err
        except KnowledgePathError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(err),
            ) from err
        return KnowledgeDocumentDeleted(path=path, deleted=True)

    @app.post("/ask", response_model=AskResponse)
    @app.post("/conversations/ask", response_model=AskResponse)
    async def ask_agent(
        payload: AskRequest,
        request: Request,
        conversation_repo: ConversationRepository = Depends(get_conversation_repository),
        turn_processor: TurnProcessor = Depends(get_turn_processor),
    ) -> AskResponse:
        if payload.conversation_id:
            conv = conversation_repo.get_conversation(payload.conversation_id)
            if conv is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"conversation not found: {payload.conversation_id}",
                )
            conv_id = conv.id
        else:
            conv = conversation_repo.create_conversation(
                title=payload.message[:50],
                metadata=payload.metadata,
            )
            conv_id = conv.id

        msg_result = conversation_repo.append_message(
            conversation_id=conv_id,
            role="user",
            text=payload.message,
            metadata=payload.metadata,
            verified_caller=owner_for_grant(_execution_grant_for_request(request)),
        )

        turn_result = turn_processor.enqueue(
            conversation_id=conv_id,
            input_message_id=msg_result.message.id,
            metadata={"agent_id": payload.agent_id, **payload.metadata},
        )
        turn_id = turn_result.turn.id

        deadline = time.monotonic() + max(1.0, payload.timeout_seconds)
        while time.monotonic() < deadline:
            current_turn = turn_processor.get_turn(turn_id)
            if current_turn is None:
                break
            if current_turn.status == "completed":
                reply_text = None
                if current_turn.reply_message_id:
                    messages = conversation_repo.list_messages(conv_id)
                    for m in messages:
                        if m.id == current_turn.reply_message_id:
                            reply_text = m.text
                            break
                return AskResponse(
                    conversation_id=conv_id,
                    turn_id=turn_id,
                    status="completed",
                    reply=reply_text,
                )
            elif current_turn.status == "failed":
                return AskResponse(
                    conversation_id=conv_id,
                    turn_id=turn_id,
                    status="failed",
                    error=current_turn.error or "turn execution failed",
                )
            await asyncio.sleep(0.1)

        return AskResponse(
            conversation_id=conv_id,
            turn_id=turn_id,
            status="timeout",
            error=f"Turn execution did not finish within {payload.timeout_seconds}s",
        )

    @app.get("/workspaces/resolve")
    def resolve_task_workspace(
        workspace: str | None = None,
        agent_id: str | None = None,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> dict[str, object]:
        selected_agent = agent_id if agent_id is not None else default_agent_id
        profile = repository.get_current(selected_agent)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"agent profile not found: {selected_agent}")
        cfg = profile.runner_config
        if cfg.get("archived") or profile.role == "archived":
            raise HTTPException(status_code=409, detail=f"agent is archived: {selected_agent}")
        if workspace is None:
            workspace = cfg.get("project")
        try:
            host = resolve_workspace(workspace, default_workspace=default_workspace, workspace_map=workspace_map)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "host_path": str(host), "sandbox_path": "/workspace", "agent_id": selected_agent,
            "profile_revision": profile.revision if profile else None,
            "project_read_only": bool(cfg.get("project_read_only", False)),
            "network": cfg.get("network", True),
            "allowed_hosts": list(cfg.get("allowed_hosts") or []),
        }

    @app.post(
        "/tasks",
        response_model=TaskRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(
        payload: TaskCreate,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskRead:
        try:
            task = repository.create(
                title=payload.title,
                description=payload.description,
                status=payload.status,
                workspace=payload.workspace,
                agent_id=payload.agent_id,
                metadata=payload.metadata,
                attachments=payload.attachments,
                id=payload.id,
            )
        except DuplicateTaskIdError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task id already exists: {error.task_id}",
            ) from error
        return task_to_read(task)

    @app.post("/imports/tasks", response_model=TaskImportResponse)
    def import_tasks(
        payload: TaskImportRequest,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskImportResponse:
        """Import completed task history by stable id, idempotently.

        Replaying the same ids never duplicates or overwrites stored tasks.
        Validation rejects the whole batch before any write happens.
        """
        records = [import_item_to_record(item) for item in payload.tasks]
        result = repository.import_tasks(records)
        return TaskImportResponse(created=result.created, skipped=result.skipped)

    @app.get("/tasks", response_model=list[TaskRead])
    def list_tasks(
        status_filter: str | None = Query(default=None, alias="status"),
        status_not_in: str | None = Query(
            default=None,
            description=(
                "Comma-separated statuses to exclude; empty values are "
                "ignored"
            ),
        ),
        workspace: str | None = Query(default=None),
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: TaskRepository = Depends(get_repository),
    ) -> list[TaskRead]:
        excluded_statuses = (
            tuple(
                value.strip()
                for value in status_not_in.split(",")
                if value.strip()
            )
            if status_not_in
            else None
        )
        tasks = repository.list(
            status=status_filter,
            status_not_in=excluded_statuses,
            workspace=workspace,
            limit=limit,
            offset=offset,
        )
        return [task_to_read(task) for task in tasks]

    @app.get("/tasks/counts", response_model=dict[str, int])
    def get_task_counts(
        repository: TaskRepository = Depends(get_repository),
    ) -> dict[str, int]:
        """Return task counts keyed by status."""
        return repository.get_status_counts()

    @app.get("/monitor/executions")
    def get_execution_monitor(
        request: Request,
        verbose: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Return running tasks/operations with live session counters.

        ``verbose=true`` adds each operation's input payload and each task's
        changed-file names for operational investigation.
        """
        return build_execution_monitor(
            request.app.state.database,
            getattr(request.app.state, "artifact_root", None),
            verbose=verbose,
        )

    @app.get("/tasks/{task_id}", response_model=TaskRead)
    def get_task(
        task_id: str,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskRead:
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task_to_read(task)

    @app.get("/tasks/{task_id}/artifacts", response_model=TaskArtifactListResponse)
    def list_task_artifacts(
        task_id: str,
        request: Request,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskArtifactListResponse:
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        meta = task.meta or {}
        provenance = meta.get("provenance") or {}
        artifacts = []
        for a in provenance.get("artifacts") or meta.get("artifacts") or []:
            artifacts.append(TaskArtifactRead(
                name=a.get("name"),
                size=a.get("size"),
                path=a.get("path"),
            ))
        artifact_root = getattr(request.app.state, "artifact_root", None) or Path("~/.kuzya/data/artifacts").expanduser()
        for cand_dir in [
            Path(artifact_root) / "tasks" / str(task.id) / "artifacts",
            Path(artifact_root) / str(task.id) / "artifacts",
        ]:
            if cand_dir.is_dir():
                existing = {a.name for a in artifacts}
                for item in sorted(cand_dir.iterdir()):
                    if item.is_file() and item.name not in existing:
                        artifacts.append(TaskArtifactRead(
                            name=item.name,
                            size=item.stat().st_size,
                            path=str(item),
                        ))
        return TaskArtifactListResponse(task_id=task_id, artifacts=artifacts)

    @app.post(
        "/tasks/{task_id}/attachments",
        response_model=TaskArtifactRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_task_attachment(
        task_id: str,
        request: Request,
        file: UploadFile = File(...),
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskArtifactRead:
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="task not found",
            )

        raw_filename = file.filename or "attachment"
        safe_name = Path(raw_filename).name
        safe_name = "".join(
            c for c in safe_name if c.isprintable() and c not in "/\\\x00"
        ).strip()
        if not safe_name or safe_name in {".", ".."}:
            safe_name = "attachment"

        artifact_root = (
            getattr(request.app.state, "artifact_root", None)
            if request else None
        ) or Path("~/.kuzya/data/artifacts").expanduser()
        task_artifacts_dir = Path(artifact_root) / str(task.id) / "artifacts"
        task_artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest_path = task_artifacts_dir / safe_name

        tmp_path = dest_path.with_name(f"{dest_path.name}.tmp.{uuid.uuid4().hex[:8]}")
        total_size = 0
        try:
            with open(tmp_path, "wb") as buffer:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    buffer.write(chunk)
                    total_size += len(chunk)
                buffer.flush()
                os.fsync(buffer.fileno())
            if dest_path.exists():
                try:
                    dest_path.chmod(0o644)
                except OSError:
                    pass
            os.replace(tmp_path, dest_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        content_type = file.content_type or "application/octet-stream"

        meta = dict(task.meta or {})
        existing_attachments = list(meta.get("attachments") or [])
        attachment_record = {
            "name": safe_name,
            "path": str(dest_path),
            "size": total_size,
            "content_type": content_type,
            "task_id": str(task.id),
        }

        updated = False
        for idx, att in enumerate(existing_attachments):
            att_name = att.get("name") if isinstance(att, dict) else getattr(att, "name", None)
            if att_name == safe_name:
                existing_attachments[idx] = attachment_record
                updated = True
                break
        if not updated:
            existing_attachments.append(attachment_record)

        repository.update(task.id, attachments=existing_attachments)

        return TaskArtifactRead(
            name=safe_name,
            size=total_size,
            path=str(dest_path),
            content_type=content_type,
        )

    def _raw_artifact_name(
        request: Request,
        task_id: str,
        fallback: str,
    ) -> str:
        """Recover a literal artifact filename from the undecoded URL path.

        Starlette exposes ``scope["path"]`` after percent-decoding. A stored
        name that itself contains a valid escape such as ``%20`` or ``%FF``
        would otherwise be decoded a second time and become unreachable.
        """
        raw_path = request.scope.get("raw_path")
        if not isinstance(raw_path, (bytes, bytearray)):
            return fallback
        try:
            marker = (
                b"/tasks/"
                + quote(task_id, safe="").encode("ascii")
                + b"/artifacts/"
            )
        except UnicodeEncodeError:
            marker = b"/artifacts/"
        index = raw_path.rfind(marker)
        if index < 0:
            marker = b"/artifacts/"
            index = raw_path.rfind(marker)
        if index < 0:
            return fallback
        try:
            return unquote_to_bytes(
                raw_path[index + len(marker) :]
            ).decode("utf-8", errors="surrogateescape")
        except (UnicodeDecodeError, ValueError):
            return fallback

    @app.get("/tasks/{task_id}/artifacts/{artifact_name:path}")
    def get_task_artifact(
        task_id: str,
        artifact_name: str,
        request: Request,
        repository: TaskRepository = Depends(get_repository),
    ) -> Response:
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        artifact_name = _raw_artifact_name(request, task_id, artifact_name)
        meta = task.meta or {}
        provenance = meta.get("provenance") or {}
        artifact_file = None
        for a in provenance.get("artifacts") or meta.get("artifacts") or []:
            if a.get("name") == artifact_name and a.get("path"):
                p = Path(a["path"])
                if p.is_file():
                    artifact_file = p
                    break
        if artifact_file is None:
            artifact_root = getattr(request.app.state, "artifact_root", None) or Path("~/.kuzya/data/artifacts").expanduser()
            for cand_dir in [
                Path(artifact_root) / "tasks" / str(task.id) / "artifacts",
                Path(artifact_root) / str(task.id) / "artifacts",
            ]:
                cand = cand_dir / artifact_name
                if not cand.is_file():
                    continue
                try:
                    resolved_dir = cand_dir.resolve(strict=True)
                    resolved_cand = cand.resolve(strict=True)
                except OSError:
                    continue
                if not resolved_cand.is_relative_to(resolved_dir):
                    continue
                artifact_file = resolved_cand
                break
        if artifact_file is None or not artifact_file.is_file():
            safe_label = artifact_name.encode(
                "utf-8",
                errors="backslashreplace",
            ).decode("utf-8")
            raise HTTPException(
                status_code=404,
                detail=f"artifact not found: {safe_label}",
            )
        content = artifact_file.read_bytes()
        return Response(content=content, media_type="text/plain; charset=utf-8")

    @app.get("/tasks/{task_id}/session")
    def get_task_session(
        task_id: str,
        repository: TaskRepository = Depends(get_repository),
    ) -> dict[str, Any]:
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        meta = task.meta or {}
        provenance = meta.get("provenance") or {}
        session_path = provenance.get("session_path")
        summary_path = provenance.get("summary_path")
        summary_text = None
        if summary_path and Path(summary_path).is_file():
            summary_text = Path(summary_path).read_text(encoding="utf-8", errors="replace")
        elif meta.get("summary"):
            summary_text = meta["summary"]
        elif task.description:
            summary_text = task.description
        changed_files = meta.get("changed_files") or provenance.get("changed_files") or []
        return {
            "task_id": task_id,
            "session_id": provenance.get("session_id") or provenance.get("runner_session_id"),
            "status": task.status,
            "summary": summary_text,
            "session_path": session_path,
            "events_path": provenance.get("events_path"),
            "changed_files": changed_files,
            "usage": provenance.get("usage"),
        }

    @app.get("/tasks/{task_id}/git/status")
    def get_task_git_status(
        task_id: str,
        repository: TaskRepository = Depends(get_repository),
    ) -> dict[str, Any]:
        """One read-only git snapshot of the task's workspace.

        The workspace resolves exactly like the Git operations do: the
        task's explicit ``workspace``, then the directory its execution
        actually used, then the instance default. Lets the secretary (via
        ``call_api``) answer "what is the state of the tree" without
        launching a diagnostic task.
        """
        from ..git_engine import GitEngine

        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        execution_context = (task.meta or {}).get("execution_context") or {}
        workspace = task.workspace or (
            str(execution_context["host_path"])
            if isinstance(execution_context, dict)
            and execution_context.get("host_path")
            else None
        )
        if not workspace and default_workspace is not None:
            workspace = str(default_workspace)
        if not workspace:
            raise HTTPException(
                status_code=409,
                detail=f"no workspace for task {task_id}",
            )
        try:
            summary = GitEngine(default_workspace=workspace).status_summary(
                workspace
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"task_id": task_id, **summary}

    def change_task_subscription(task_id, payload, request, *, cancel=False):
        from ..task_subscriptions import TaskSubscriptions

        grant = getattr(request.state, "execution_grant", None)
        if grant is not None and (
            grant.label != f"conversation:{payload.conversation_id}"
            or not ({"tasks.write", "*"} & grant.actions)
        ):
            raise HTTPException(status_code=403, detail="subscription requires the originating conversation grant")
        repository = request.app.state.repository
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if grant is not None and not request.app.state.permissions.authorize(grant.token, "tasks.write", f"task:{task.id}"):
            raise HTTPException(status_code=404, detail="task not found")
        service = TaskSubscriptions(request.app.state.database)
        try:
            if cancel:
                return service.cancel(task.id, payload.conversation_id)
            return service.subscribe(task.id, payload.conversation_id, payload.instructions)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/tasks/{task_id}/subscriptions")
    def subscribe_task(task_id: str, payload: TaskSubscriptionCreate, request: Request) -> dict[str, Any]:
        return change_task_subscription(task_id, payload, request)

    @app.delete("/tasks/{task_id}/subscriptions")
    def unsubscribe_task(task_id: str, payload: TaskSubscriptionCreate, request: Request) -> dict[str, Any]:
        return change_task_subscription(task_id, payload, request, cancel=True)

    @app.patch("/tasks/{task_id}", response_model=TaskRead)
    def patch_task(
        task_id: str,
        payload: TaskPatch,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskRead:
        try:
            task = repository.update(
                task_id=task_id,
                reject_if_running=True,
                **payload.replacements(),
            )
        except TaskAlreadyRunningError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        except TaskNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task not found: {error.task_id}",
            ) from error
        return task_to_read(task)

    @app.post(
        "/tasks/{task_id}/interrupt",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def interrupt_task(
        task_id: str,
        repository: TaskRepository = Depends(get_repository),
    ) -> dict[str, object]:
        """Request a manual stop of a running task execution.

        Marks the task ``stop_requested`` and signals the live runner process
        group (SIGTERM, escalating to SIGKILL). The worker then records the
        task as ``waiting_for_input`` with an explicit "stopped manually"
        clarification, so the owner replies with new instructions and the
        session resumes. signalling the group reaches every node and bwrap
        child the runner spawned.
        """
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task not found: {task_id}",
            )
        meta = dict(task.meta or {})
        if meta.get("execution_status") != "running":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "task is not running "
                    f"(execution_status={meta.get('execution_status')!r})"
                ),
            )
        registry = getattr(app.state, "task_process_registry", None)
        signalled = bool(registry is not None and registry.request_interrupt(task_id))
        meta["execution_stop_requested_at"] = dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
        repository.update(task_id, metadata=meta)
        return {
            "task_id": task_id,
            "status": "stop_requested",
            "process_signalled": signalled,
        }

    @app.post(
        "/conversations",
        response_model=ConversationRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_conversation(
        payload: ConversationCreate,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> ConversationRead:
        try:
            conversation = repository.create_conversation(
                title=payload.title,
                metadata=payload.metadata,
                id=payload.id,
            )
        except DuplicateConversationIdError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "conversation id already exists: "
                    f"{error.conversation_id}"
                ),
            ) from error
        return conversation_to_read(conversation)

    @app.get("/conversations", response_model=list[ConversationRead])
    def list_conversations(
        agent_id: str | None = Query(default=None),
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> list[ConversationRead]:
        """List conversations, optionally scoped to one agent.

        ``agent_id`` matches the durable ConversationRuntime binding or the
        ``metadata.talkable_agent`` hint, so a caller can find both prepared
        native sessions and direct-agent conversations.
        """
        conversations = repository.list_conversations(
            agent_id=agent_id, limit=limit, offset=offset
        )
        return [conversation_to_read(item) for item in conversations]

    @app.get(
        "/conversations/{conversation_id}",
        response_model=ConversationRead,
    )
    def get_conversation(
        conversation_id: str,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> ConversationRead:
        conversation = repository.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )
        return conversation_to_read(conversation)

    @app.patch(
        "/conversations/{conversation_id}",
        response_model=ConversationRead,
    )
    def patch_conversation(
        conversation_id: str,
        payload: ConversationPatch,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> ConversationRead:
        """Shallow-merge conversation metadata and optionally replace title.

        Used by the Telegram direct-agent flow to release an active workspace
        lock without deleting the conversation or its native Runner session.
        """
        updates = payload.model_dump(exclude_unset=True)
        try:
            conversation = repository.update_conversation(
                conversation_id, **updates
            )
        except ConversationNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conversation not found: {error.conversation_id}",
            ) from error
        return conversation_to_read(conversation)

    @app.post(
        "/conversations/{conversation_id}/talk",
        response_model=ConversationRead,
    )
    def change_conversation_talk(
        conversation_id: str,
        payload: ConversationTalkRequest,
        talks: WorkspaceTalkRepository = Depends(
            get_workspace_talk_repository
        ),
        conversations: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> ConversationRead:
        """Start, close or finalize the direct-agent workspace lock.

        ``start`` activates the talk when the workspace is free, otherwise
        marks it pending so the first turn waits. ``request_end`` sets
        ``talk_closing`` and refuses new turns while the current/queued work
        drains. ``finalize`` releases ``talk_active`` only when no pending or
        running turn remains. The conversation and native Runner session are
        never deleted.
        """
        action = payload.action.strip().lower()
        try:
            if action == "start":
                talks.start(conversation_id)
            elif action == "request_end":
                talks.request_end(conversation_id)
            elif action == "finalize":
                talks.finalize_if_idle(conversation_id)
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="action must be start, request_end or finalize",
                )
        except ConversationTalkNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"talkable conversation not found: {error.conversation_id}",
            ) from error
        conversation = conversations.get_conversation(conversation_id)
        if conversation is None:  # pragma: no cover - created above
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conversation not found: {conversation_id}",
            )
        return conversation_to_read(conversation)

    @app.post(
        "/conversations/{conversation_id}/messages",
        response_model=MessageAppendResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def append_message(
        conversation_id: str,
        payload: MessageCreate,
        response: Response,
        request: Request,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> MessageAppendResponse:
        """Append a message; exact idempotent retries return the original.

        ``201 Created`` means a new row was written; ``200 OK`` means the
        payload matched a stored message for the same idempotency key and no
        row was written. A reused key with a different ``role``/``text``/
        ``metadata`` payload returns ``409 Conflict`` and leaves the original
        untouched. Appending to an absent conversation returns ``404``.
        """
        try:
            result = repository.append_message(
                conversation_id=conversation_id,
                role=payload.role,
                text=payload.text,
                source=payload.source,
                external_id=payload.external_id,
                metadata=payload.metadata,
                verified_caller=owner_for_grant(_execution_grant_for_request(request)),
            )
        except ConversationNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conversation not found: {error.conversation_id}",
            ) from error
        except MessageConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return message_append_to_response(
            result.message, replayed=result.replayed
        )

    @app.get(
        "/telegram/links/{chat_id}/{telegram_message_id}",
        response_model=TelegramMessageLinkRead,
    )
    def get_telegram_message_link(
        chat_id: int,
        telegram_message_id: int,
        request: Request,
    ) -> TelegramMessageLinkRead:
        """Resolve one Telegram message to its durable conversation link.

        The link is the single routing source for incoming Telegram Replies:
        a hit names the original conversation (and task, when bound); a 404
        means no durable link exists for that chat/message pair.
        """
        repo = TelegramDeliveryRepository(request.app.state.database)
        link = repo.get_message_link(chat_id, telegram_message_id)
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "telegram message link not found: "
                    f"{chat_id}/{telegram_message_id}"
                ),
            )
        return TelegramMessageLinkRead(
            chat_id=link.chat_id,
            telegram_message_id=link.telegram_message_id,
            conversation_id=link.conversation_id,
            message_id=link.message_id,
            turn_id=link.turn_id,
            task_id=link.task_id,
            origin=link.origin,
            created_at=link.created_at,
        )

    @app.get(
        "/conversations/{conversation_id}/messages",
        response_model=list[MessageRead],
    )
    def list_messages(
        conversation_id: str,
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> list[MessageRead]:
        if repository.get_conversation(conversation_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )
        messages = repository.list_messages(
            conversation_id, limit=limit, offset=offset
        )
        return [message_to_read(message) for message in messages]

    @app.post(
        "/conversations/{conversation_id}/turns",
        response_model=TurnRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_turn(
        conversation_id: str,
        payload: TurnCreate,
        turn_processor: TurnProcessor = Depends(get_turn_processor),
    ) -> TurnRead:
        """Enqueue one turn for an existing inbound user message."""
        try:
            result = turn_processor.enqueue(
                conversation_id=conversation_id,
                input_message_id=payload.input_message_id,
                metadata=payload.metadata,
            )
        except TurnInputMessageNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"input message not found: {error.message_id}",
            ) from error
        except TurnConversationMismatchError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        except TurnInputRoleError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        except TurnConversationClosedError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        return turn_to_read(result.turn)

    @app.get(
        "/turns/{turn_id}",
        response_model=TurnRead,
    )
    def get_turn(
        turn_id: str,
        turn_processor: TurnProcessor = Depends(get_turn_processor),
    ) -> TurnRead:
        """Return the turn status and reply reference or 404."""
        turn = turn_processor.get_turn(turn_id)
        if turn is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"turn not found: {turn_id}",
            )
        return turn_to_read(turn)

    @app.post(
        "/operations",
        response_model=OperationRead,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_operation(
        request: Request,
        payload: OperationSubmit,
        response: Response,
        repository: OperationRepository = Depends(
            get_operation_repository
        ),
    ) -> OperationRead:
        """Submit a durable operation; exact idempotent retries reuse it.

        ``202 Accepted`` means a new queued operation was written; ``200 OK``
        means the idempotency key matched a stored operation and no row was
        written. A reused key with a different payload returns ``409
        Conflict`` and leaves the original untouched. The response is the
        operation record, so a lost write response is resolved by reading the
        same operation id. A submitted operation is never itself proof of a
        completed side effect; callers inspect ``status``/``result``/``error``.
        """
        _enforce_operation_submit_scope(
            request, payload.kind, payload.target_id
        )
        owner = owner_for_grant(_execution_grant_for_request(request))
        try:
            result = repository.submit(
                kind=payload.kind,
                target_id=payload.target_id,
                input=payload.input,
                idempotency_key=payload.idempotency_key,
                metadata=payload.metadata,
                owner=owner,
            )
        except OperationConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return operation_to_read(result.operation)

    @app.get("/operations", response_model=list[OperationRead])
    def list_operations(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        kind: str | None = Query(default=None),
        target_id: str | None = Query(default=None),
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: OperationRepository = Depends(
            get_operation_repository
        ),
    ) -> list[OperationRead]:
        # A scoped execution may inspect only the operations it submitted, so
        # it may never enumerate the whole queue.
        if _execution_grant_for_request(request) is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Forbidden: scoped credential cannot enumerate "
                    "operations"
                ),
            )
        operations = repository.list(
            status=status_filter,
            kind=kind,
            target_id=target_id,
            limit=limit,
            offset=offset,
        )
        return [operation_to_read(item) for item in operations]

    @app.get("/operations/{operation_id}", response_model=OperationRead)
    def get_operation(
        request: Request,
        operation_id: str,
        repository: OperationRepository = Depends(
            get_operation_repository
        ),
    ) -> OperationRead:
        """Return one operation, including status, result and error.

        A scoped caller may read only an operation it owns. A foreign or
        legacy ownerless operation is reported as not found so the route does
        not disclose the existence of another caller's operation.
        """
        operation = repository.get(operation_id)
        grant = _execution_grant_for_request(request)
        if operation is None or (
            grant is not None and operation.owner != owner_for_grant(grant)
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"operation not found: {operation_id}",
            )
        return operation_to_read(operation)

    @app.get(
        "/tasks/{task_id}/analysis",
        response_model=list[AnalysisReportRead],
    )
    def list_task_analysis(
        task_id: str,
        repository: AnalysisRepository = Depends(get_analysis_repository),
    ) -> list[AnalysisReportRead]:
        """Return the task's analysis reports, newest first."""
        return [
            analysis_report_to_read(report)
            for report in repository.list_reports(task_id=task_id)
        ]

    @app.get("/analysis/{report_id}", response_model=AnalysisReportRead)
    def get_analysis_report(
        report_id: str,
        repository: AnalysisRepository = Depends(get_analysis_repository),
    ) -> AnalysisReportRead:
        """Return one persisted analysis report, including its evidence."""
        report = repository.get_report(report_id)
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"analysis report not found: {report_id}",
            )
        return analysis_report_to_read(report)

    @app.get(
        "/tasks/{task_id}/review",
        response_model=list[ReviewRoundRead],
    )
    def list_task_review_rounds(
        task_id: str,
        request: Request,
        repository: TaskRepository = Depends(get_repository),
    ) -> list[ReviewRoundRead]:
        """Return persisted review rounds for one task, oldest round first.

        Rounds are read from the task's ``review/round_<n>.json`` artifacts,
        never from the database. An unknown task is ``404``; a task with no
        review artifacts returns an empty list. ``report`` is the matching
        ``round_<n>.md`` text or an empty string when that file is missing.
        """
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="task not found",
            )
        artifact_root = getattr(request.app.state, "artifact_root", None)
        return [
            ReviewRoundRead(**item)
            for item in list_review_rounds(artifact_root, str(task.id))
        ]

    @app.get(
        "/analysis/{report_id}/recommendations",
        response_model=list[RecommendationRead],
    )
    def list_report_recommendations(
        report_id: str,
        repository: AnalysisRepository = Depends(get_analysis_repository),
    ) -> list[RecommendationRead]:
        """Return the evidence-linked recommendations of one report."""
        if repository.get_report(report_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"analysis report not found: {report_id}",
            )
        return [
            recommendation_to_read(item)
            for item in repository.list_recommendations(report_id=report_id)
        ]

    @app.get(
        "/recommendations", response_model=list[RecommendationRead]
    )
    def list_recommendations(
        task_id: str | None = Query(default=None),
        status_filter: str | None = Query(default=None, alias="status"),
        target_kind: str | None = Query(default=None),
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: AnalysisRepository = Depends(get_analysis_repository),
    ) -> list[RecommendationRead]:
        """List recommendations, optionally filtered by task/status/kind."""
        items = repository.list_recommendations(
            task_id=task_id,
            status=status_filter,
            target_kind=target_kind,
            limit=limit,
            offset=offset,
        )
        return [recommendation_to_read(item) for item in items]

    @app.get(
        "/recommendations/{recommendation_id}",
        response_model=RecommendationPreviewRead,
    )
    def preview_recommendation(
        recommendation_id: str,
        service: RecommendationService = Depends(get_recommendation_service),
    ) -> RecommendationPreviewRead:
        """Preview a recommendation and its current target state.

        ``would_conflict`` reports whether the observed revision/content hash
        still matches; a stale proposal is never applied by this read.
        """
        try:
            preview = service.preview(recommendation_id)
        except RecommendationNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        return recommendation_preview_to_read(preview)

    @app.post(
        "/recommendations/{recommendation_id}/approve",
        response_model=RecommendationRead,
    )
    def approve_recommendation(
        recommendation_id: str,
        service: RecommendationService = Depends(get_recommendation_service),
    ) -> RecommendationRead:
        """Explicitly approve a ``proposed`` recommendation.

        Approval records intent only; it never mutates the target. Applying is
        a separate call that re-checks the observed revision/content hash.
        """
        try:
            recommendation = service.approve(recommendation_id)
        except RecommendationNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        except RecommendationStateError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return recommendation_to_read(recommendation)

    @app.post(
        "/recommendations/{recommendation_id}/apply",
        response_model=RecommendationRead,
    )
    def apply_recommendation(
        recommendation_id: str,
        service: RecommendationService = Depends(get_recommendation_service),
    ) -> RecommendationRead:
        """Apply an approved recommendation after re-checking the target.

        A stale target returns ``409 Conflict`` and is persisted ``conflict``;
        newer content is never overwritten. A non-approved recommendation is
        also a ``409`` state error.
        """
        try:
            recommendation = service.apply(recommendation_id)
        except RecommendationNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        except (RecommendationConflictError, RecommendationStateError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        except AnalysisError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
        return recommendation_to_read(recommendation)

    @app.post(
        "/agents",
        response_model=AgentRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent(
        payload: AgentCreate,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Create revision ``1`` of an agent profile.

        A repeated ``agent_id`` returns ``409 Conflict``; when the current
        snapshot can be read its revision is included in the detail.
        """
        try:
            profile = repository.create(
                agent_id=payload.agent_id,
                name=payload.name,
                role=payload.role,
                system_prompt=payload.system_prompt,
                runner_config=payload.runner_config,
                skills=payload.skills,
                knowledge=payload.knowledge,
            )
        except DuplicateAgentProfileError as error:
            current = repository.get_current(error.agent_id)
            detail = f"agent profile already exists: {error.agent_id}"
            if current is not None:
                detail += f" (current revision {current.revision})"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from error
        if knowledge_base is not None:
            knowledge_base.save_agent_profile(profile)
        return agent_to_read(profile)

    @app.get("/agents", response_model=list[AgentRead])
    def list_agents(
        include_archived: bool = Query(default=False),
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> list[AgentRead]:
        """List the current (highest-revision) snapshot of every agent."""
        return [
            agent_to_read(profile)
            for profile in repository.list_current(include_archived=include_archived)
        ]

    @app.get("/agents/{agent_id}", response_model=AgentRead)
    def get_agent(
        agent_id: str,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Return the current snapshot of ``agent_id`` or ``404``."""
        profile = repository.get_current(agent_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile not found",
            )
        return agent_to_read(profile)

    @app.get(
        "/agents/{agent_id}/sessions",
        response_model=list[AgentSessionRead],
    )
    def list_agent_runner_sessions(
        agent_id: str,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> list[AgentSessionRead]:
        """Return metadata about the agent's native Runner sessions.

        Only session ids, attempt counts and attempt-directory times are
        exposed. Session contents (``events.jsonl``, prompts and replies)
        are never read. An unknown profile is ``404``; a profile without a
        usable ``runner_config.state_dir`` returns an empty list.
        """
        profile = repository.get_current(agent_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile not found",
            )
        runner_config = profile.runner_config or {}
        state_dir = (
            runner_config.get("state_dir")
            if isinstance(runner_config, dict)
            else None
        )
        return [
            AgentSessionRead(**item)
            for item in list_agent_sessions(state_dir or "")
        ]

    @app.get(
        "/agents/{agent_id}/revisions",
        response_model=list[AgentRead],
    )
    def list_agent_revisions(
        agent_id: str,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> list[AgentRead]:
        """Return every immutable snapshot of ``agent_id`` by revision.

        An unknown agent yields an empty list, matching the repository
        contract for listing history.
        """
        return [
            agent_to_read(profile)
            for profile in repository.list_history(agent_id)
        ]

    @app.get(
        "/agents/{agent_id}/revisions/{revision}",
        response_model=AgentRead,
    )
    def get_agent_revision(
        agent_id: str,
        revision: int,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Return one immutable snapshot or ``404`` when it is absent."""
        profile = repository.get_revision(agent_id, revision)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile revision not found",
            )
        return agent_to_read(profile)

    @app.patch("/agents/{agent_id}", response_model=AgentRead)
    def patch_agent(
        agent_id: str,
        payload: AgentPatch,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Append a new revision after an optimistic revision check.

        Omitted fields are copied from the current snapshot; supplied fields
        replace the stored value whole. ``409 Conflict`` reports the current
        revision when known and leaves all snapshots unchanged; ``404`` means
        the agent has no snapshots.
        """
        try:
            profile = repository.update(
                agent_id=agent_id,
                expected_revision=payload.expected_revision,
                **payload.replacements(),
            )
        except AgentProfileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent profile not found: {error.agent_id}",
            ) from error
        except AgentProfileRevisionConflictError as error:
            detail = (
                "agent profile revision conflict: expected revision "
                f"{error.expected_revision}"
            )
            if error.actual_revision is not None:
                detail += f", current revision {error.actual_revision}"
            else:
                detail += ", current revision unknown"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from error
        if knowledge_base is not None:
            knowledge_base.save_agent_profile(profile)
        return agent_to_read(profile)

    @app.delete("/agents/{agent_id}", response_model=AgentRead)
    def delete_agent(
        agent_id: str,
        request: Request,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Archive (soft-delete) an agent profile."""
        if agent_id in CORE_SYSTEM_AGENTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Core system agent '{agent_id}' cannot be archived",
            )
        current = repository.get_current(agent_id)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile not found",
            )
        try:
            profile = repository.archive(agent_id, expected_revision=current.revision)
            try:
                kb = getattr(request.app.state, "knowledge_base", None)
                # No knowledge base is attached to this instance: do not
                # construct one against an implicit host HOME directory.
                if kb is not None:
                    target = kb.agents_dir / f"{agent_id}.md"
                    if target.is_file():
                        target.unlink()
                        kb.git_commit(f"Archive agent profile: {agent_id}")
            except Exception as e:
                logger.warning("Could not remove agent markdown file on delete: %s", e)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        return agent_to_read(profile)

    # Mounted last, after every API route: the MCP tool registry is generated
    # from the completed OpenAPI document and refreshed on the next start.
    attach_mcp_endpoint(app)

    return app

