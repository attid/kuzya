# Tools and API discovery

The platform exposes one loopback REST API and one set of Pi JS tools. The JS
tools are only a client of the API (plus local knowledge/session files); they
do not implement domain logic. This file is the discovery contract.

## REST endpoints

The authoritative list is the generated OpenAPI document (`GET /openapi.json`)
and `src/kuzya/api/app.py`. The 50 REST operations below are complete: every
business route the app exposes is listed, including artifact, session,
review, agent-session and subscription routes. The app also mounts an MCP
transport on `GET`/`POST` for `/mcp` and `/mcp/`. It is not an OpenAPI
operation and has no Pi JS tool. It is represented in
`src/kuzya/capabilities.py` by explicit adapter method/path records
(`kind="adapter"`, action/resource `"*"`), not by duplicating the 48
generated MCP tools; the adapter inherits the REST action/resource and adds no
permission. See [MCP transport](mcp.md) and the
[ChatGPT plugin guide](../mcp-chatgpt.md).

`{task_id}` accepts either the internal `id` or the human `number` (`41`) on
task routes that resolve through `TaskRepository.get`; an exact internal id
wins over a numeric number. `GET /tasks/{task_id}/analysis` is the exception:
it passes the path string straight to `AnalysisRepository.list_reports`
without `TaskRepository.get`, so it requires the internal UUID. For a task
with UUID `A` and `number=41`, `/tasks/41/analysis` returns an empty list even
when reports exist for `A`. Numeric lookup is not claimed for other entities
or filters: for example the `task_id` query filter on `/recommendations` and
operation `target_id` are opaque strings. `{path}` and `{artifact_name}` are
greedy path parameters (`:path`).

| Method | Path | Success | Required action | Notes |
|---|---|---|---|---|
| `GET` | `/health` | `200` | public | Liveness; the only unauthenticated route (with `OPTIONS`). |
| `GET` | `/workspaces/resolve` | `200`, `404`, `409`, `422` | `workspaces.read` | Read-only execution preflight. |
| `POST` | `/tasks` | `201`, `409`, `422` | `tasks.write` | Create a task. |
| `GET` | `/tasks` | `200` | `tasks.read` | List tasks; filters `status`, `status_not_in` (comma-separated exclusions), `workspace`, `limit`, `offset`. |
| `GET` | `/tasks/counts` | `200` | `tasks.read` | One grouped count per stored status, including `archived`. |
| `GET` | `/monitor/executions` | `200` | `tasks.read` | Running tasks/operations, bounded live session stats and queue counts; `verbose=true` adds operation input and changed-file names. |
| `GET` | `/tasks/{task_id}` | `200`, `404` | `tasks.read` | Read one task. |
| `PATCH` | `/tasks/{task_id}` | `200`, `404`, `409`, `422` | `tasks.write` | Partial update; `409` while the task is running. |
| `POST` | `/tasks/{task_id}/interrupt` | `202`, `404`, `409` | `tasks.write` | Request a manual stop of a running execution; the task lands in `waiting_for_input` with a stopped-manually clarification. |
| `GET` | `/tasks/{task_id}/artifacts` | `200`, `404` | `tasks.read` | List artifact metadata. |
| `GET` | `/tasks/{task_id}/artifacts/{artifact_name}` | `200`, `404` | `tasks.read` | Download one artifact (raw bytes, `text/plain`). |
| `GET` | `/tasks/{task_id}/session` | `200`, `404` | `tasks.read` | Session id, summary, paths, changed files, usage. |
| `GET` | `/tasks/{task_id}/review` | `200`, `404` | `tasks.read` | Persisted review rounds from `review/round_<n>.json` artifacts, oldest round first. |
| `POST` | `/tasks/{task_id}/attachments` | `201`, `404`, `422` | `tasks.write` | Multipart upload (`file`); records the artifact. |
| `POST` | `/tasks/{task_id}/subscriptions` | `200`, `403`, `404`, `409`, `422` | deferred (`tasks.write`) | Subscribe the originating conversation. |
| `DELETE` | `/tasks/{task_id}/subscriptions` | `200`, `403`, `404`, `409`, `422` | deferred (`tasks.write`) | Cancel before the event is queued. |
| `POST` | `/imports/tasks` | `200`, `422` | `tasks.write` | Idempotent historical import by stable id. |
| `POST` | `/ask` | `200`, `404`, `422` | `conversations.write` | Ask an agent and wait for the turn. |
| `POST` | `/conversations/ask` | `200`, `404`, `422` | `conversations.write` | Alias of `/ask`. |
| `POST` | `/conversations` | `201`, `409`, `422` | `conversations.write` | Create a conversation. |
| `GET` | `/conversations` | `200` | `conversations.read` | List conversations; filters `agent_id`, `limit`, `offset`. |
| `GET` | `/conversations/{conversation_id}` | `200`, `404` | `conversations.read` | Read one conversation. |
| `PATCH` | `/conversations/{conversation_id}` | `200`, `404`, `422` | `conversations.write` | Shallow-merge `metadata`, optional `title`. |
| `POST` | `/conversations/{conversation_id}/talk` | `200`, `400`, `404`, `422` | `conversations.write` | Direct-agent workspace lifecycle: `start`, `request_end`, `finalize`. |
| `POST` | `/conversations/{conversation_id}/messages` | `201`/`200`, `404`, `409`, `422` | `conversations.write` | `201` insert, `200` idempotent replay. |
| `GET` | `/conversations/{conversation_id}/messages` | `200`, `404` | `conversations.read` | List messages. |
| `GET` | `/telegram/links/{chat_id}/{telegram_message_id}` | `200`, `404` | `conversations.read` | Resolve a Telegram reply target. |
| `POST` | `/conversations/{conversation_id}/turns` | `201`, `400`, `404`, `422` | `turns.write` | Enqueue one secretary turn. |
| `GET` | `/turns/{turn_id}` | `200`, `404` | `turns.read` | Turn status and reply reference. |
| `POST` | `/operations` | `202`/`200`, `403`, `409`, `422` | deferred (kind-scoped) | Submit a durable operation. |
| `GET` | `/operations` | `200`, `403` | deferred | List the queue; filters `status`, `kind`, `target_id`, `limit`, `offset`; scoped callers get `403`. |
| `GET` | `/operations/{operation_id}` | `200`, `404` | deferred | Read status/result/error; owner-scoped. |
| `GET` | `/tasks/{task_id}/analysis` | `200` | `analysis.read` | List a task's analysis reports; internal UUID only, no human-number lookup (empty for unknown id). |
| `GET` | `/analysis/{report_id}` | `200`, `404` | `analysis.read` | Read one report with evidence. |
| `GET` | `/analysis/{report_id}/recommendations` | `200`, `404` | `analysis.read` | List a report's recommendations. |
| `GET` | `/recommendations` | `200` | `analysis.read` | Filters: `task_id`, `status`, `target_kind`, `limit`, `offset`. |
| `GET` | `/recommendations/{recommendation_id}` | `200`, `404` | `analysis.read` | Preview and conflict check; never applies. |
| `POST` | `/recommendations/{recommendation_id}/approve` | `200`, `404`, `409` | `analysis.approve` | Record intent; never mutates the target. |
| `POST` | `/recommendations/{recommendation_id}/apply` | `200`, `404`, `409`, `422` | `analysis.approve` | Apply after a revision/hash recheck. |
| `POST` | `/agents` | `201`, `409`, `422` | `agents.write` | Create revision `1`. |
| `GET` | `/agents` | `200` | `agents.read` | Current snapshots; `include_archived` query. |
| `GET` | `/agents/{agent_id}` | `200`, `404` | `agents.read` | Current snapshot. |
| `GET` | `/agents/{agent_id}/sessions` | `200`, `404` | `agents.read` | Native Runner session ids, attempt counts and timestamps; never session contents. |
| `GET` | `/agents/{agent_id}/revisions` | `200` | `agents.read` | History; empty list for an unknown id. |
| `GET` | `/agents/{agent_id}/revisions/{revision}` | `200`, `404` | `agents.read` | One immutable snapshot. |
| `PATCH` | `/agents/{agent_id}` | `200`, `404`, `409`, `422` | `agents.write` | Append a revision after the optimistic check. |
| `DELETE` | `/agents/{agent_id}` | `200`, `400`, `404` | `agents.write` | Archive; core system agents rejected. |
| `GET` | `/knowledge` | `200`, `503` | `knowledge.read` | List Markdown documents, optional `category`. |
| `GET` | `/knowledge/{path}` | `200`, `404`, `503` | `knowledge.read` | Read one document. |
| `PUT` | `/knowledge/{path}` | `200`, `400`, `503` | `knowledge.write` | Write/update and commit when git-backed. |
| `DELETE` | `/knowledge/{path}` | `200`, `400`, `404`, `503` | `knowledge.write` | Delete one exact Markdown file. |

### Common conventions

- Every route except `/health` (and `OPTIONS`) requires
  `Authorization: Bearer <token>` when authentication is enabled. A missing or
  unknown token is `401`; a valid execution token without the route action or
  resource is `403`.
- A collection route (`resource=None` in `src/kuzya/permissions.py`) requires
  wildcard resource authority for a scoped caller. A task-scoped execution can
  read or write only `task:<id>` resources, not enumerate tasks, operations,
  agents or knowledge.
- `limit` is an optional `int | null` and is unlimited when omitted; it is
  the row-count bound. `offset` is an `int` with default `0`. The route
  signatures declare no `ge`/`le` constraints, so FastAPI accepts any integer;
  SQLAlchemy applies the values as `.limit()`/`.offset()` when `limit is not
  None`/`offset` is truthy (`limit=0` returns an empty page). This applies to
  every list route declaring these query parameters. Ordering is stated per
  section where it matters: tasks and recommendations are oldest first,
  analysis reports are newest first, and messages are ordered by `sequence`.
- Validation errors are `422` with FastAPI's standard `detail` array. Request
  models with `extra="forbid"` reject unknown fields; `TaskImportItem` is the
  documented exception that folds extras into `metadata`.
- Any unhandled failure is `500` with `{"detail": "Internal server error",
  "request_id": "..."}`. Every response carries `X-Request-ID`.
- `status` values on tasks are free-form strings; the conventional values and
  their effects are in [Task lifecycle](task-lifecycle.md#status-values).
  Protocol statuses (turn, operation, subscription) are listed per object
  below.

### Response representations

Shared shapes returned by several routes (`src/kuzya/api/schemas.py`):

`TaskRead`: `id`, `number` (int or null), `title`, `description` (nullable),
`status` (free-form string), `agent_id` (nullable), `workspace` (nullable),
`created_at`, `updated_at` (naive UTC), `metadata` (object), `attachments`
(array of `TaskAttachment`). `TaskAttachment`: `name`, `path`, `task_id`,
`size`, `content_type` (all optional, extra keys allowed).

`TaskArtifactRead`: `name`, `size` (nullable), `path` (nullable),
`content_type` (default `text/plain`). `TaskArtifactListResponse`: `task_id`,
`artifacts`.

Task session object: `task_id`, `session_id` (nullable), `status`,
`summary` (nullable), `session_path` (nullable), `events_path` (nullable),
`changed_files` (array), `usage` (nullable object). `summary` falls back from
`provenance.summary_path` to `metadata.summary` to the task description;
`changed_files` prefers `metadata.changed_files` and falls back to
`metadata.provenance.changed_files`.

Subscription object: `task_id`, `conversation_id`, `status`, `turn_id`
(nullable), `instructions`. Status: `pending`, `queued`, `cancelled`,
`delivered`.

`ConversationRead`: `id`, `title` (nullable), `created_at`, `updated_at`,
`metadata`.

`MessageRead`: `sequence`, `id`, `conversation_id`, `role`, `text`, `source`,
`external_id` (nullable), `created_at`, `metadata`.
`MessageAppendResponse` adds `replayed` (bool).

`TelegramMessageLinkRead`: `chat_id`, `telegram_message_id`,
`conversation_id`, `message_id` (nullable), `turn_id` (nullable), `task_id`
(nullable), `origin`, `created_at`.

`TurnRead`: `id`, `conversation_id`, `input_message_id`, `status`,
`claim_token` (nullable), `reply_message_id` (nullable), `error` (nullable),
`attempt_count`, `created_at`, `updated_at`, `claimed_at` (nullable),
`finished_at` (nullable), `metadata`. Status: `pending`, `running`,
`completed`, `failed`.

`OperationRead`: `id`, `kind`, `target_id` (nullable), `input`, `status`,
`result` (nullable), `error` (nullable), `idempotency_key` (nullable),
`attempt_count`, `created_at`, `updated_at`, `claimed_at` (nullable),
`finished_at` (nullable), `metadata`. Status: `accepted`/`pending`,
`running`, `succeeded`, `failed`, `interrupted`. A domain verdict lives in
`result`, never in `status`.

`AnalysisReportRead`: `id`, `task_id`, `agent_id`, `profile_revision`
(nullable), `summary` (nullable), `report`, `evidence`, `created_at`,
`updated_at`.

`ReviewRoundRead`: `round` (int), `verdict` (`approved` or
`changes_requested`), `findings` (array of finding objects), `report`
(Markdown text), `created_at` (naive UTC).

`RecommendationRead`: `id`, `report_id`, `task_id` (nullable), `target_kind`,
`target_id`, `target_revision` (nullable), `target_content_hash` (nullable),
`title`, `rationale`, `evidence`, `proposed_content`, `diff` (nullable),
`status`, `applied_revision` (nullable), `applied_content_hash` (nullable),
`apply_detail` (nullable), `created_at`, `updated_at`. Status: `proposed`,
`approved`, `applied`, `conflict`, `failed`.
`RecommendationPreviewRead`: `recommendation`, `target_exists`,
`current_revision` (nullable), `current_content_hash` (nullable),
`current_content` (nullable), `would_conflict`, `conflict_reason`
(nullable).

`AgentRead`: `agent_id`, `revision`, `name`, `role`, `system_prompt`,
`runner_config`, `skills`, `knowledge`, `created_at`.

`AgentSessionRead`: `session_id`, `created_at`, `last_activity`,
`attempts_count`. It is metadata only; session contents are never included.

`KnowledgeListResponse`: `items` (array of paths), `category` (nullable).
`KnowledgeDocumentRead`: `path`, `content`. `KnowledgeDocumentDeleted`:
`path`, `deleted`.

`AskResponse`: `conversation_id`, `turn_id`, `status`, `reply` (nullable),
`error` (nullable). Status: `completed`, `failed`, `timeout`.

### Health and workspace

#### `GET /health`

Public liveness probe. Returns `{"status": "ok"}`. No database access.

#### `GET /workspaces/resolve`

Read-only preflight; never writes.

- Query: `workspace` (optional path/alias), `agent_id` (optional, defaults to
  the configured default agent).
- `200` body: `host_path`, `sandbox_path` (always `/workspace`), `agent_id`,
  `profile_revision`, `project_read_only` (bool), `network` (bool or policy
  value), `allowed_hosts` (array).
- Errors: `404` unknown agent profile, `409` archived profile, `422` missing
  or invalid workspace.
- Rules: [Workspaces and routing](workspaces-and-routing.md).

### Tasks

#### `POST /tasks`

- Auth: `tasks.write`; collection route.
- Body (`TaskCreate`, unknown fields `422`): `title` (required string),
  `description` (optional string), `status` (string, default `incoming`),
  `agent_id` (optional), `workspace` (optional), `metadata` (object, default
  `{}`), `attachments` (optional array of `TaskAttachment`), `id` (optional
  caller-supplied id).
- `201` `TaskRead`; `409` duplicate `id`; `422` validation.
- The route writes the supplied values. Workspace/executor preflight is a
  behaviour of the `create_task` JS tool, not of this raw route; a direct
  caller must resolve the workspace itself.
- `metadata` execution keys honoured by the worker: `allowed_hosts`,
  `project_read_only`, `readonly_paths`, `allow_git_commit`, `attachments`.
  `status="incoming"` saves without executing; `status="in_progress"`
  authorizes execution.

#### `GET /tasks`

- Auth: `tasks.read`; collection route.
- Query: `status` (exact match on the free-form value), `status_not_in`
  (comma-separated statuses to exclude; empty elements are ignored),
  `workspace`, `limit`, `offset` (default `0`). The two status filters compose
  as an intersection. `status_not_in` is an exclusion filter so a caller can
  ask for "everything not shown elsewhere" without enumerating future status
  values; the Telegram "Other" category uses
  `status_not_in=incoming,in_progress,failed,done,archived`.
- `200` array of `TaskRead`, oldest first (`created_at`, then `id`).

#### `GET /tasks/counts`

- Auth: `tasks.read`.
- `200` object mapping every stored `status` to its count, for example
  `{"archived": 73, "done": 1}`. It is one grouped SQL aggregate and includes
  non-conventional statuses. Statuses with no tasks are omitted; a missing key
  means zero.

#### `GET /tasks/{task_id}`

- Auth: `tasks.read` on `task:<id>`.
- `200` `TaskRead`; `404` unknown id/number.

#### `PATCH /tasks/{task_id}`

- Auth: `tasks.write` on `task:<id>`.
- Body (`TaskPatch`, every field optional, unknown fields `422`):
  `title: str | null`, `description: str | null`, `status: str | null`,
  `workspace: str | null`, `agent_id: str | null`,
  `metadata: object | null`, `attachments: array<TaskAttachment> | null`.
  Omitted fields are unchanged (`model_fields_set`). An explicit `null` is
  not uniformly "clear":
  - `description`, `workspace` and `agent_id` store `NULL`/`null`.
  - `attachments: null` removes `metadata.attachments`; an array replaces the
    whole stored array.
  - `metadata: null` shallow-merges `{}`, so it changes nothing and does not
    clear existing keys. Send explicit keys to replace their values.
  - `title: null` and `status: null` are accepted, not rejected.
    `TaskRepository.update` applies `str(value)`, so they are stored as the
    literal string `"None"`. Omit the field when the intent is "unchanged".
- `status="in_progress"` clears a terminal `metadata.execution_status`
  (including `completed`) so the worker claims the task again.
  `attachments`/other fields do not.
- `200` `TaskRead`; `404` unknown task; `409` when the task already has an
  active `execution_status == "running"` claim and the patch would set
  `in_progress`; `422` validation.

#### `GET /tasks/{task_id}/artifacts`

- Auth: `tasks.read` on `task:<id>`.
- `200` `TaskArtifactListResponse`: declared artifacts from
  `metadata.provenance.artifacts`/`metadata.artifacts` plus files found in the
  task artifact directory. `404` unknown task.

#### `GET /tasks/{task_id}/artifacts/{artifact_name}`

- Auth: `tasks.read` on `task:<id>`.
- `200` raw file bytes with `Content-Type: text/plain; charset=utf-8`
  (the route always uses this media type, independent of the stored
  `content_type`). `404` unknown task or artifact. Disk candidates are
  resolved and must stay inside the task artifact directory; a traversal
  attempt is reported as not found.

#### `GET /tasks/{task_id}/session`

- Auth: `tasks.read` on `task:<id>`.
- `200` task session object (see above), `404` unknown task. Read-only; it
  does not start or resume anything.

#### `GET /tasks/{task_id}/review`

- Auth: `tasks.read` on `task:<id>`.
- Path `task_id` accepts the internal id or the human task number, like the
  other task-scoped reads; the resolved internal id is used for artifact
  lookup.
- `200` array of review-round objects, ordered by ascending `round` and then
  by `created_at`: `round` (int), `verdict` (domain value `approved` or
  `changes_requested`), `findings` (array of finding objects), `report`
  (Markdown text of `round_<n>.md`, or `""` when the report file is absent)
  and `created_at` (naive UTC from the artifact `timestamp`).
- Artifacts are read from the known review layouts: the in-worker
  `tasks/<task_id>/review/`, the older `<task_id>/review/` and standalone
  operation directories
  `operations/task_<task_id>/operation_<id>/review/`. A prefixed
  `task_<id>` directory is ambiguous because another task can literally be
  named `task_<id>`; every artifact from such a directory must carry a
  matching `task_id` in its JSON document, otherwise it is skipped. A
  recorded `task_id` that contradicts the requested task is skipped even in
  an exact-id directory. The old prefix-match scan of `task_<id>_*`
  directories was removed. Multiple review executions may therefore
  contribute equal round numbers; all are returned.
- A malformed `round_<n>.json` is skipped with a log warning instead of
  hiding the task's other rounds. A non-finite or out-of-range numeric
  timestamp falls back to the artifact file mtime rather than failing the
  whole request.
- `404` unknown task. A task without review artifacts returns `[]`. This is
  a read-only projection: it does not start a review, parse the database
  verdict or read a path named inside an artifact.

#### `POST /tasks/{task_id}/interrupt`

- Auth: `tasks.write` on `task:<id>`; a scoped token may stop only its own
  task.
- No body. `202` `{task_id, status: "stop_requested", process_signalled}`.
  The runner process group receives SIGTERM (escalating to SIGKILL after a
  grace window), and the worker records the task `waiting_for_input` with a
  "stopped manually" clarification. The session journal is append-only, so
  the next run resumes the session; replying to the clarification delivers
  new instructions to the same agent.
- Errors: `404` unknown task, `409` when the task is not running
  (`execution_status != "running"`). `process_signalled: false` means the
  stop marker was stored but no live process handle existed (for example the
  request raced the run's end); the recorded execution outcome governs what
  happens next.
- Workspace changes made before the stop are **not** rolled back; use the
  existing rollback operation afterwards if needed.

#### `POST /tasks/{task_id}/attachments`

- Auth: `tasks.write` on `task:<id>`. A scoped token may upload only to its
  own task and gets `403` for a foreign one.
- Content type: `multipart/form-data` with required field `file`
  (`UploadFile`). Only the basename is kept; path separators and control
  characters are stripped.
- `201` `TaskArtifactRead`. The file is written atomically into the task
  artifact directory and registered in `metadata.attachments`; an existing
  entry with the same name is replaced.
- Errors: `404` unknown task, `422` missing file, `403`/`401` auth.

#### `POST /tasks/{task_id}/subscriptions` and `DELETE .../subscriptions`

- Auth: deferred; the handler requires `tasks.write` (or `*`) on
  `task:<id>` and, for a scoped grant, the label
  `conversation:<payload.conversation_id>`. A different conversation gets
  `403`; a foreign task is reported as `404`.
- Body on both methods (`TaskSubscriptionCreate`): `conversation_id`
  (required), `instructions` (string, default `""`). DELETE also carries the
  body.
- `200` subscription object. `POST` is idempotent for an existing pending or
  queued row; re-subscribing after `cancelled` sets it back to `pending` and
  replaces `instructions`. `DELETE` cancels a `pending`/`cancelled` row and
  returns `409` once the event is `queued`/`delivered` ("event is already
  queued; it cannot be recalled").
- Other errors: `404` unknown task or conversation, `409` cancel conflict,
  `422` validation.
- Delivery contract: [Task lifecycle](task-lifecycle.md#completion-subscriptions).

#### `POST /imports/tasks`

- Auth: `tasks.write`; collection route.
- Body: `tasks` (required array). Each item is `TaskCreate` plus required
  `id`; extra top-level fields are accepted and folded into `metadata`
  (explicit `metadata` keys win).
- `200` `{"created": <int>, "skipped": <int>}`. Replaying the same ids never
  duplicates or overwrites; validation rejects the whole batch before any
  write (`422`).

### Executions monitor

#### `GET /monitor/executions`

- Auth: `tasks.read`; collection route (`resource=None`). A task-scoped
  execution token may not call it; the monitor spans all running work.
- Query: `verbose` (bool, default `false`). The base response is unchanged.
  `verbose=true` adds `input` (the exact operation input object) to each
  operation entry and `changed_files` (array of names) plus
  `changed_files_count` to each task entry. The caller opts in to the larger
  diagnostic payload; it is not included by default.
- `200` object:
  - `generated_at`: UTC ISO timestamp of the snapshot.
  - `running`: array of running executions. A task entry has `kind="task"`,
    `entry_type="task"`, `task_id`, `number`, `title`, `agent_id`,
    `workspace`, `status`, `claimed_at`, `elapsed`/`elapsed_sec`,
    `tools_used`, `last_activity_sec`, `current_tool`, `tokens`,
    `total_tokens`, `cost`, `session_available` and `events_path`. An
    operation entry has `kind` equal to its operation kind (`analysis`,
    `review`, `git.commit_push`, `git.rollback`), `entry_type="operation"`,
    `operation_id`, `operation_kind`, `kind_label`, `agent_id`, `target_id`,
    `target_number`, `target_title`, `target` (`id`/`number`/`title`), the
    same elapsed/session fields and `events_path`. `tokens` contains the
    usage counters (`input`, `output`, `cacheRead`, `totalTokens`) and
    `cost`; `total_tokens`/`tokens_total` repeat `tokens.totalTokens`.
  - `queues`: `turns` (status `pending`), `operations` (status `pending` or
    `accepted`), `subscriptions` (status `queued`) and `tasks` (`in_progress`
    without an active execution claim). Descriptive aliases
    (`turns_pending`, `operations_pending`, `subscriptions_queued`,
    `tasks_unclaimed`, `tasks_in_progress_unclaimed`) carry the same counts.
- Session fields come from the newest `events.jsonl` tail of that execution:
  `tools_used` counts `tool_execution_start`; `current_tool` is the latest
  start without a matching end; `tokens`/`cost` are the last
  `message_update`/`message_end` usage, not a sum over attempts;
  `last_activity_sec` is the file age measured in UTC. The age is not a stall
  verdict: a model can think without emitting events.
- Operation events are accepted only from artifacts linked to that exact
  `operation.id`: review/analysis directories named for the operation, or an
  analyzer's random export whose run `task.txt`/`provenance.json` carries
  `turn_id: analysis:<operation_id>`. When the export declares a Runner
  `state_dir`, that directory may be shared by many analyzer runs. A
  session's `config-path.txt`, when present, must resolve to the linked
  export's `runner_role.json`, and its `task.md`, when present, must carry the
  exact operation turn id. The newest attempt is then read only inside that
  session.
  There is no fallback to the newest file anywhere in the operations tree, so
  concurrent or completed work can never be attributed to another operation.
  Every candidate event file is considered before the newest is chosen; only
  the chosen file's contents are bounded.
- Without an event file the execution still appears with `elapsed`, but
  `session_available=false`, `tools_used=null`, `last_activity_sec=null`,
  `current_tool=null`, `tokens=null`, `cost=null`. The route never crashes or
  invents counters. The event tail is byte- and record-bounded.

### Ask

#### `POST /ask` and `POST /conversations/ask`

- Auth: `conversations.write`; collection route.
- Body (`AskRequest`): `message` (required string), `agent_id` (default
  `system-secretary`), `conversation_id` (optional; a new conversation is
  created when absent), `timeout_seconds` (float, default `60.0`, effective
  minimum one second), `metadata` (object).
- `200` `AskResponse`. `status="completed"` carries `reply`;
  `status="failed"` carries `error`; `status="timeout"` means the turn is
  still queued/running and is **not** an error — read the conversation or the
  turn instead of repeating the write.
- Errors: `404` unknown `conversation_id`, `422` validation.
- This route waits synchronously for the turn; ordinary durable turn
  processing is described in [Task lifecycle](task-lifecycle.md#turns-secretary-lifecycle).

### Conversations, messages and Telegram links

#### `POST /conversations`

- Auth: `conversations.write`; collection route.
- Body (`ConversationCreate`): `title` (optional string), `metadata`
  (object, default `{}`), `id` (optional). Unknown fields `422`.
- `201` `ConversationRead`; `409` duplicate `id`.

#### `GET /conversations`

- Auth: `conversations.read`; collection route.
- Query: `agent_id` (optional), `limit`, `offset` (default `0`). `agent_id`
  matches either the durable `conversation_runtimes.agent_id` binding or the
  `metadata.talkable_agent` hint used by the direct-agent flow; a
  conversation matching both is returned once. Unknown agents return `[]`.
- `200` array of `ConversationRead`, oldest first.

#### `GET /conversations/{conversation_id}`

- Auth: `conversations.read` on `conversation:<id>`. `200`
  `ConversationRead`; `404` unknown conversation.

#### `PATCH /conversations/{conversation_id}`

- Auth: `conversations.write` on `conversation:<id>`.
- Body (`ConversationPatch`, unknown fields `422`): optional `title`,
  optional `metadata` object. Supplied `metadata` keys are shallow-merged;
  absent keys are preserved. Sending `metadata: {}` is a no-op merge.
- `200` `ConversationRead`; `404` unknown conversation.
- Not an idempotency-keyed write: replaying the same patch leaves the same
  values, and no new conversation is created. The direct-agent workspace
  lock is managed by `POST /conversations/{id}/talk`, not by a bare patch;
  the conversation and its native Runner session are never deleted.

#### `POST /conversations/{conversation_id}/talk`

- Auth: `conversations.write` on `conversation:<id>`.
- Body (`ConversationTalkRequest`, unknown fields `422`): `action` (required
  string). Accepted values: `start`, `request_end`, `finalize`; any other
  value is `400`.
- `start` checks the task and other-talk locks under one immediate SQLite
  write transaction. If the agent's workspace is free it sets
  `metadata.talk_active=true`; if a task or another talk owns it, it sets
  `metadata.talk_pending=true` and the first turn waits.
- `request_end` sets `metadata.talk_closing=true`, so no new turn can be
  enqueued (`POST .../turns` returns `409`). Existing pending/running turns
  keep the lock and are processed.
- `finalize` sets `talk_active=false`, `talk_closing=false`,
  `talk_closed=true` only when no pending or running turn remains. It never
  deletes the conversation or its native Runner session.
- `200` `ConversationRead`; `404` missing or non-talkable conversation;
  `400` unknown action; `422` validation.

#### `POST /conversations/{conversation_id}/messages`

- Auth: `conversations.write` on `conversation:<id>`.
- Body (`MessageCreate`, unknown fields `422`): `text` (required string),
  `role` (default `user`), `source` (default `api`), `external_id`
  (optional idempotency key), `metadata` (object, default `{}`). All string
  fields are free-form (no role allowlist).
- `201` new `MessageAppendResponse`; `200` with `replayed=true` for an exact
  idempotent retry. The key is `(conversation_id, source, external_id)`; a
  reused key with a different payload is `409`; `NULL external_id` rows are
  always distinct.
- Errors: `404` unknown conversation, `409` idempotency conflict, `422`
  validation.

#### `GET /conversations/{conversation_id}/messages`

- Auth: `conversations.read` on `conversation:<id>`.
- Query: `limit`, `offset` (default `0`). `200` array of `MessageRead`;
  `404` unknown conversation.

#### `GET /telegram/links/{chat_id}/{telegram_message_id}`

- Auth: `conversations.read`.
- Path: `chat_id` (int), `telegram_message_id` (int).
- `200` `TelegramMessageLinkRead`; `404` when no durable link exists. Used to
  route a Telegram Reply to its original conversation/task; see
  [Knowledge and integrations](knowledge-and-integrations.md#dialog-context-uuid-and-telegram-delivery).

### Turns

#### `POST /conversations/{conversation_id}/turns`

- Auth: `turns.write` on `conversation:<id>`.
- Body (`TurnCreate`, unknown fields `422`): `input_message_id` (required,
  unique), `metadata` (object, default `{}`).
- `201` `TurnRead` with `status="pending"`. Replaying the same
  `input_message_id` returns the original turn.
- Errors: `404` input message not found, `400` message belongs to another
  conversation or its role is not an inbound user message, `422` validation.

#### `GET /turns/{turn_id}`

- Auth: `turns.read` on `turn:<id>`. `200` `TurnRead`; `404` unknown turn.
- Statuses: `pending`, `running`, `completed`, `failed`. A failed turn keeps
  its `error`; recovery is explicit (`TurnProcessor.retry_failed`,
  `recover_abandoned`), never automatic.

### Operations

#### `POST /operations`

- Auth: deferred and kind-scoped. A scoped caller needs the action for the
  submitted `kind` (`review.trigger`, `analysis.trigger`, `git.actions`) and
  the `task:<target_id>` resource; an unknown kind or a foreign target is
  `403`.
- Body (`OperationSubmit`, unknown fields `422`): `kind` (required string),
  `target_id` (optional string), `input` (object, default `{}`),
  `idempotency_key` (optional), `metadata` (object, default `{}`).
- `202` new `OperationRead`; `200` when the idempotency key replays the stored
  operation; `409` when the same key is reused with a different payload;
  `422` validation.
- Kinds and `input`: `review` → `{instructions?}`; `analysis` → `{}`;
  `git.commit_push` → `{push?: bool = true, message?: string,
  require_ci?: bool = false}`; `git.rollback` → `{}`. An administrative
  caller may submit an unknown kind; no executor is registered, so the
  operation ends `failed` with an explicit diagnostic. A scoped caller cannot
  submit it.
- Submission is never a result. Read `GET /operations/{id}` and inspect
  `status`, `result` and `error`. Verdicts (`approved` /
  `changes_requested`) live inside `result`.

#### `GET /operations`

- Auth: deferred; any scoped execution token receives `403` (it may not
  enumerate the queue). Administrative callers only.
- Query: `status` (exact operation status), `kind` (exact operation kind),
  `target_id` (exact target id), `limit`, `offset` (default `0`). Filters
  compose as an intersection.
- `200` array of `OperationRead`, oldest intake sequence first. Example:
  `GET /operations?status=running&kind=analysis` returns running analyses;
  `GET /operations?target_id=<task_id>` returns every operation for a task.

#### `GET /operations/{operation_id}`

- Auth: deferred; a scoped caller may read only an operation it owns. A
  foreign or legacy ownerless operation is `404` (existence is not
  disclosed).
- `200` `OperationRead`; `404` unknown or foreign operation.

### Analysis and recommendations

#### `GET /tasks/{task_id}/analysis`

- Auth: `analysis.read` on `task:<id>`.
- Path `task_id` must be the internal UUID. This route does not call
  `TaskRepository.get`; it passes the string to
  `AnalysisRepository.list_reports`, which filters the stored `task_id`
  column directly (`src/kuzya/api/app.py:1716-1724`). A human `number` is not
  resolved, so `/tasks/41/analysis` returns `[]` for a task whose UUID differs
  from `"41"`, even when reports exist.
- `200` array of `AnalysisReportRead`, newest first. Unlike the other
  task-scoped reads, an unknown task id returns an empty list, not `404`
  (`AnalysisRepository.list_reports` only filters by `task_id`).

#### `GET /analysis/{report_id}`

- Auth: `analysis.read`. `200` `AnalysisReportRead` including `evidence`;
  `404` unknown report.

#### `GET /analysis/{report_id}/recommendations`

- Auth: `analysis.read`. `200` array of `RecommendationRead`; `404` unknown
  report.

#### `GET /recommendations`

- Auth: `analysis.read`. Query: `task_id`, `status`, `target_kind`, `limit`,
  `offset` (default `0`). `200` array of `RecommendationRead`.

#### `GET /recommendations/{recommendation_id}`

- Auth: `analysis.read`. `200` `RecommendationPreviewRead` with
  `would_conflict`, `conflict_reason` and the observed current target;
  `404` unknown recommendation. This read never applies anything.

#### `POST /recommendations/{recommendation_id}/approve`

- Auth: `analysis.approve`. `200` `RecommendationRead`. Requires status
  `proposed`; an already-`approved` proposal is returned idempotently, any
  other state is `409`; unknown id is `404`. Approval records intent only and
  never mutates the target.

#### `POST /recommendations/{recommendation_id}/apply`

- Auth: `analysis.approve`. `200` `RecommendationRead`. Requires status
  `approved` and rechecks the observed revision/content hash. An
  already-`applied` recommendation is returned unchanged; a stale target or
  other non-approved state is `409` (a stale row is persisted `conflict`);
  `422` for an analysis-specific apply error. Unknown id is `404`. Newer
  content is never overwritten.

### Agents

#### `POST /agents`

- Auth: `agents.write`; collection route.
- Body (`AgentCreate`, unknown fields `422`): `agent_id`, `name`, `role`,
  `system_prompt` (all required strings), `runner_config` (object, default
  `{}`), `skills` (array of objects, default `[]`), `knowledge` (object,
  default `{}`). No role allowlist, field cap or skill-count limit.
- `201` `AgentRead` (revision `1`); `409` duplicate `agent_id` (detail names
  the current revision when readable); `422` validation.

#### `GET /agents`

- Auth: `agents.read`; collection route.
- Query: `include_archived` (bool, default `false`). `200` array of the
  highest-revision snapshot per agent.

#### `GET /agents/{agent_id}`

- Auth: `agents.read` on `agent:<id>`. `200` current `AgentRead`; `404`.

#### `GET /agents/{agent_id}/sessions`

- Auth: `agents.read` on `agent:<id>`. `404` unknown profile.
- Reads the trusted `runner_config.state_dir` of the current profile and
  lists directories under `<state_dir>/sessions/`. A missing, relative or
  symlinked state directory returns `[]`; it is never resolved against the
  server working directory.
- `200` array ordered by most recent activity first. Each item contains only
  `session_id`, `created_at` (mtime of the first/lowest-numbered attempt),
  `last_activity` (mtime of the last/highest-numbered attempt) and
  `attempts_count`. A session with no attempts uses the session directory
  mtime for both timestamps and reports `attempts_count=0`.
- Session contents (`events.jsonl`, `task.md`, prompts, replies) are never
  opened or returned.

#### `GET /agents/{agent_id}/revisions`

- Auth: `agents.read` on `agent:<id>`. `200` array of `AgentRead` ordered by
  revision; an unknown agent returns an empty list.

#### `GET /agents/{agent_id}/revisions/{revision}`

- Auth: `agents.read` on `agent:<id>`. Path `revision` is an `int` with no
  explicit numeric bounds; a non-integer is `422`.
- `200` `AgentRead`; `404` unknown agent/revision.

#### `PATCH /agents/{agent_id}`

- Auth: `agents.write` on `agent:<id>`.
- Body (`AgentPatch`, unknown fields `422`): `expected_revision` (required
  `int`), `name: str | null`, `role: str | null`,
  `system_prompt: str | null`, `runner_config: object | null`,
  `skills: array<object> | null`, `knowledge: object | null`. Every optional
  field is typed, but an explicit `null` is rejected with `422` by
  `_reject_explicit_nulls`; omit it to copy the current value. Omitted fields
  are copied from the current snapshot; supplied fields replace whole (no
  deep merge). A body with only `expected_revision` appends an unchanged
  snapshot.
- `200` new `AgentRead`; `404` no snapshots; `409` revision conflict (detail
  includes the observed revision when known); `422` validation.

#### `DELETE /agents/{agent_id}`

- Auth: `agents.write` on `agent:<id>`.
- `200` archived `AgentRead` (soft delete). `400` for
  `system-secretary`, `system-code-reviewer`, `system-log-analyzer`; `404`
  unknown agent. The archive appends the archived snapshot and removes the
  `agents/<id>.md` mirror when a knowledge base is attached.

### Knowledge

All knowledge routes return `503` when the instance has no knowledge base
configured (`knowledge base not configured`).

#### `GET /knowledge`

- Auth: `knowledge.read`; collection route. Query: `category` (optional).
- `200` `KnowledgeListResponse`; `503`.

#### `GET /knowledge/{path}`

- Auth: `knowledge.read`. `200` `KnowledgeDocumentRead`; `404` missing
  document; `503`.

#### `PUT /knowledge/{path}`

- Auth: `knowledge.write`; collection route.
- Body (`KnowledgeDocumentWrite`): `content` (required string),
  `commit_message` (optional string).
- `200` `KnowledgeDocumentRead`; `400` invalid path or write failure;
  `503`. When the tree is git-backed, the write is committed with
  `commit_message` when supplied.

#### `DELETE /knowledge/{path}`

- Auth: `knowledge.write`; collection route.
- `200` `KnowledgeDocumentDeleted`; `404` missing file; `400` invalid or
  protected path (`agents/`, absolute paths, `.`/`..`, backslashes,
  wildcards, directories, symlink components, non-`.md`); `503`. Exactly one
  file is removed; no wildcard or recursive deletion. See
  [Knowledge and integrations](knowledge-and-integrations.md#deleting-one-document).

## Authentication

`BearerAuthMiddleware` (`src/kuzya/api/app.py`) distinguishes two
credential classes:

- administrative tokens from `KUZYA_API_TOKENS` /
  `KUZYA_API_TOKEN` receive unrestricted access and are never handed to
  a child execution; and
- execution tokens issued by `ExecutionPermissions`
  (`src/kuzya/permissions.py`) are accepted only inside their granted
  action/resource scope. `resolve_requirement` maps each route to a concrete
  action and resource, so a raw HTTP call from a shell cannot bypass the tool
  exposure policy.

The authenticated/`require_auth` decision is fixed at process start: issuing
or revoking execution grants never switches the instance between anonymous and
protected. A standalone app with no administrative token and no permission
registry stays in explicit open development mode. `/health` and `OPTIONS` are
public.

`/mcp` is not a business route in `_ROUTE_RULES`, so an execution token
receives `403` at the transport endpoint; MCP currently cannot be used by a
sandbox. Each tool call forwards the caller's token into the real REST route,
where the same middleware re-checks the concrete action and resource. See
[MCP transport](mcp.md#authentication-and-permissions).

## Error contract

- 4xx/5xx JSON body: `{"detail": "<message>"}`.
- An unhandled exception returns `500` with
  `{"detail": "Internal server error", "request_id": "..."}` and logs a full
  traceback. The process stays alive and the next request is served.
- Every response carries `X-Request-ID` (incoming value preserved, otherwise a
  fresh UUID). Use it to correlate logs.

## Idempotency

- `POST /conversations/{id}/messages`: `(conversation_id, source,
  external_id)`. A reused key with a different payload returns `409`.
- `POST /operations`: `idempotency_key`. Exact retry returns the stored
  operation (`200`); reused key with a different payload returns `409`.
- `POST /imports/tasks`: stable task `id`.
- `POST /conversations/{id}/turns`: unique `input_message_id`.
- `PATCH /agents/{id}`: `expected_revision` (not idempotent; it is an
  optimistic concurrency check).

**Implemented:** the JS tools derive the operation `idempotency_key` from
`[kind, target_id, input, toolCallId]` (`operationKey` in
`src/pi_runner/policy.mjs`). An exact retry of one invocation reuses the same
operation; a later, distinct invocation with the same payload creates a new
operation. An explicit `idempotency_key` overrides this and is honored across
invocations.

## Deferred operations and Git outcomes

`POST /operations` returns a durable `OperationRead`. Read it back with
`GET /operations/{id}`: only a `succeeded` operation proves the executor ran.
`result` is kind-specific and `result.action` names the concrete engine
action.

`git.commit_push` with `input.push=false` calls `GitEngine.commit_local`;
with `push=true` (the default) it calls `GitEngine.commit_and_push`. On
success the handler marks the task `done` and returns `action`, `message`,
`commit_hash`, `branch`, `ci_status`, `ci_required`, `pushed` and
`task_status`.

Workspace resolution for Git and review operations: the task's explicit
`workspace` first; when the task has none, the directory its execution
actually used (`metadata.execution_context.host_path`, recorded by the
worker from the agent profile); the instance default workspace is the last
resort only — it is a different repository, and a task without a workspace
never executed there.

### Clean tree and no-op commit outcomes

A clean working tree does **not** by itself mean `succeeded`. `commit_local`
first builds its candidate list from `metadata.changed_files`. If that list
is empty or absent, it falls back to `git status --porcelain` (excluding
`localdoc/` and `.gemini/`) (`src/kuzya/git_engine.py:96-113`).

- If there are no candidates after that step (the common clean-tree case),
  `commit_local` returns `success=False` with
  `"Нет файлов для коммита в рабочей директории."` before running
  `git commit`. The operation handler converts any `success=False` into
  `OperationExecutionError`, so the operation ends `failed`
  (`src/kuzya/operation_handlers.py:106-109`). No commit is attempted, and
  `commit_and_push` returns before its `git push` step.
- A non-empty declared list whose entries are all ignored or match nothing is
  also `success=False` ("no stageable path"), so it is a failed operation too.
- The only successful no-commit outcome requires at least one declared path
  that git accepts for staging, followed by `git commit` reporting "nothing
  to commit" (`git_engine.py:163-170`). Then `commit_local` returns
  `success=True`, `action="commit_local"`, the message
  `"Нет новых изменений для фиксации (рабочая директория чиста)."` and
  `commit_hash=null`. An observed operation with `push=false` returned
  `status=succeeded` with:

```json
{
  "action": "commit_local",
  "message": "Нет новых изменений для фиксации (рабочая директория чиста).",
  "commit_hash": null,
  "pushed": false,
  "task_status": "done"
}
```

- This completed operation is not an error and there is nothing to retry; it
  simply created no commit. `commit_hash` is `null` because no commit object
  was created. Verify the real state with `git log -1` / `git show HEAD`, not
  by repeating the operation (see
  [Verification](verification.md#git-state-inspection-read-only)).
- The engine does not create an empty commit.
- Push does not happen when `input.push=false`; `result.pushed` reflects the
  requested flag, not proof that a remote received anything.
- With `push=true`, push only follows a successful local result. The clean
  tree/no-stageable failure above returns before `git push`, so that
  `push=true` operation performs no push. In the narrow successful no-op
  variant (declared stageable paths plus "nothing to commit"),
  `commit_and_push` can reach `git push` of the current branch, but that push
  contains no new commit; a no-change push is not evidence that a commit was
  created.
- `git.rollback` restores the declared paths, marks the task `cancelled` and
  returns success; it has no separate "nothing to roll back" error.

## Registered JS tools

Defined in `src/pi_runner/policy.mjs`. There are **38** registered tools
(earlier revisions of this page said 37 and omitted `call_api`). Grouped by
behaviour:

**API-backed reads/writes** (return `ok`/`applied`/`failed`):

`list_tasks`, `get_task`, `get_task_logs`, `resolve_workspace`, `create_task`,
`update_task`, `resume_task_session`, `subscribe_task`, `unsubscribe_task`,
`list_agents`, `get_agent`, `update_agent`, `create_agent`, `delete_knowledge`.
Note: `create_task` and `update_task` accept an optional typed `attachments`
array to stage input files into `/inbox`. `delete_knowledge` removes exactly
one explicitly confirmed `.md` file through `DELETE /knowledge/{path}` and
never accepts `agents/`, wildcards or directories.

**Universal API access**: `call_api(method, path, body?)` calls any REST
route with the execution-scoped credential. Its grant is admin-equivalent by
design (`action="*"` in `src/kuzya/capabilities.py`), and it returns
`ok`/`applied`/`failed`/`unknown`. Use it for gaps the dedicated tools do not
cover (task metadata such as `changed_files`, conversations, turns), and read
state back after a lost write response. Paths must be exact endpoints from
the tables above: an unknown path is a plain `404`, not a permission problem.
For "what is running right now" use `GET /monitor/executions` (live workers,
tools, tokens), `GET /tasks/{task_id}/session` (a finished-or-running task's
session transcript, summary and paths) and `GET /agents/{agent_id}/sessions`
(runner session ids, never contents). There is **no** `/tasks/{id}/logs`
endpoint — `get_task_logs` is a JS tool wrapping `GET /tasks/{task_id}`, not
a REST route.

**Durable operation submissions** (return the operation; inspect it to learn
the outcome): `trigger_review`, `trigger_analysis`, `trigger_commit_push`,
`trigger_rollback`.

**Analysis and recommendation reads/writes** (return `ok`/`applied`/`failed`):
`list_analysis_reports`, `get_analysis_report`, `list_recommendations`,
`preview_recommendation`, `approve_recommendation`, `apply_recommendation`.
These are exposed to the secretary profile only, not added unconditionally to
every execution.

**Session-deferred writes** (write a receipt file; applied after the turn):
`attach_document`, `archive_agent`, `save_project_summary`, `write_knowledge`,
`update_component_rules`, `install_skill`, `create_skill`,
`ask_clarification`, plus the legacy `created_agents.json` /
`archived_agents.json` / `actions.json` drains.

**Local reads**: `read_knowledge`, `list_knowledge`, `list_skills`.

**Direct integrations**: `openviking_search`, `openviking_read`.

See [Capability inventory](capability-inventory.md) for the per-tool mapping.

## Tool exposure

A model only sees the tools named in its `AgentProfile.runner_config.tools`.
The default secretary list lives in `src/kuzya/knowledge.py`
(`sec_tools`). Changing a profile's `tools` array changes exposure on the next
execution; it does not grant API permissions by itself.

## Discovery commands

```bash
# Endpoints and status codes
PYTHONPATH=src .venv/bin/python -c \
  "from kuzya.api.app import create_app; \
   app=create_app('sqlite:////tmp/probe.db'); \
   [print(m.upper(), p, sorted(app.openapi()['paths'][p][m]['responses'])) \
    for p in sorted(app.openapi()['paths']) \
    for m in sorted(app.openapi()['paths'][p])]"

# Tool names
grep -n 'name: "' src/pi_runner/policy.mjs

# Operation kinds and registrations
grep -n 'OPERATION_KIND_\|\.register(' \
  src/kuzya/operation_worker.py \
  src/kuzya/operation_handlers.py \
  src/kuzya/runner.py
```

If the repository `.venv` is absent or its interpreter is missing (as in the
2026-10-22 audit), OpenAPI generation is unavailable. Use the static
fallback instead and say so in the report:

```bash
grep -n '@app\.' src/kuzya/api/app.py
grep -n '^    _endpoint(' src/kuzya/capabilities.py
grep -n '^    _adapter(' src/kuzya/capabilities.py
```

Static analysis must still cross-check the decorators against
`_ENDPOINT_CAPABILITIES` / `CAPABILITIES`, and the mounted `/mcp` routes
against `ADAPTER_CAPABILITIES`; a route or adapter that exists in only one of
them is an inventory finding, not a documentation fact.

## Capability inventory

`src/kuzya/capabilities.py` is the machine-readable inventory of every
tool, REST route and transport adapter (method, schema, result status,
permission action, resource and handbook document). `audit_capabilities`
compares REST endpoint records with the generated OpenAPI document, adapter
records with the mounted application routes, tools with the real
`policy.mjs` registry, and actions with `ACTIONS_FOR_TOOLS`. The adapter
records are `/mcp` and `/mcp/` for `GET`/`POST` with `kind="adapter"`; they
document inherited authority and do not duplicate the generated MCP tools.
`tests/test_capabilities.py` runs the audit, so an undeclared tool,
unsupported route, missing adapter, schema drift or silent privilege widening
fails deterministically. The `doc` field links each capability to its
handbook file.

## Related

- `src/kuzya/api/app.py`, `src/kuzya/api/schemas.py`
- `src/pi_runner/policy.mjs`
- `docs/api.md`, `docs/secretary-tools.md`
