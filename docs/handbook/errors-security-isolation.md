# Errors, security and isolation

## Error contract

- 4xx/5xx JSON body: `{"detail": "<message>"}`.
- `500` body: `{"detail": "Internal server error", "request_id": "..."}`.
  The unhandled-exception middleware logs the traceback and keeps the process
  alive.
- Every response carries `X-Request-ID`; reuse an incoming value or read the
  generated one from the response/logs.
- A tool maps transport failure or a `>= 500` write response to
  `status="failed"`, `outcome="unknown"` and an instruction to read state
  before retrying. A lost response never proves a write failed.

### Operation failures

- Unknown executor kind: the operation is recorded `failed` with
  `OperationExecutionError: no executor registered ...`; it is never reported
  as succeeded.
- Executor exception: recorded `failed`, error carries the exception type and
  message.
- Process restart while running: `reconcile_startup_orphans` marks the
  operation `interrupted`; it is not silently replayed.
- Explicit reopen: `OperationRepository.requeue_interrupted(id)` moves an
  `interrupted` operation back to `pending` after inspection.

### Turn / conversation failures

- A bad model call on a secretary turn fails closed: the conversation runtime
  is blocked with a diagnostic and there is no automatic replay or fresh
  fallback.
- A pre-launch failure (bad runtime-default file or malformed pinned config)
  releases the reservation so an operator can repair and explicitly retry.
- Startup reconciliation fails orphaned `running` turns so a conversation is
  not permanently blocked.

## Authentication

- Loopback only: the API default host is `127.0.0.1`.
- `BearerAuthMiddleware` fixes the authenticated/`require_auth` decision when
  the app is built. Administrative tokens from `KUZYA_API_TOKENS`
  receive unrestricted access and are never handed to a child execution.
- Execution tokens issued by `ExecutionPermissions`
  (`src/kuzya/permissions.py`) are accepted only for the granted
  action and resource. Scoped tokens with `tasks.write` can upload attachments
  only to their assigned task (`POST /tasks/{task_id}/attachments`), receiving
  `403 Forbidden` on foreign tasks. `resolve_requirement` maps every declared route, so a
  shell inside a sandbox cannot bypass the tool-exposure policy with `curl`.
  A path no application route matches is not a scope question: execution
  credentials receive the router's plain `404` (or `405`) exactly like an
  administrative caller, never a misleading scope denial. Deliberately
  unexposed routes that do exist (`/mcp`, `/docs`) stay `403`.
  Operations additionally check the submitted `kind` and `target_id`, and a
  scoped caller can read only its own operations.
- Issuing the first execution grant never switches a running service from
  anonymous to protected, and revoking the last grant never reopens anonymous
  access. A standalone app with no administrative token and no permission
  registry stays in explicit open development mode.

## Sandbox isolation

`pi_runner` (`src/pi_runner/core.py`, `src/pi_runner/cli.py`) enforces:

- the project must be an existing directory other than `/`;
- the real HOME and its parents cannot be the project;
- `state_dir` must be outside the project;
- dependency/runtime paths cannot be the project, the state dir, HOME, system
  data directories, or credential/Git directories (`.ssh`, `.aws`, `.gnupg`,
  `.git`) and must exist;
- tool installs are mounted read-only; only the target project is writable
  (and only when `project_read_only` is false);
- `network=false` or `"restricted"` isolates network via `--unshare-net` and
  bridges connections through the host-side UDS egress forward proxy.
  Authorized LLM providers and declared `allowed_hosts` are tunneled;
  unauthorized destinations are rejected with HTTP 403 Forbidden.

The workflow guard in `policy.mjs` blocks `git commit` and `git push` from
`bash` unless explicitly allowed (`/session/allow_git_commit` or
`RUNNER_ALLOW_GIT_COMMIT=1`), and even then still blocks `push`. It is an
accidental-action guard, not a shell security parser; variable expansion,
aliases and interpreted programs are outside it.

## Isolated instances

- Tests and demos use temporary file-backed SQLite databases and temporary
  artifact/knowledge roots. WAL tests use a real file database, not SQLite
  `:memory:`.
- No production endpoint, production database, knowledge base or Telegram
  account is part of the development contract.
- `RunnerConfig.from_env` reads database URL, host, port, artifact/knowledge
  roots, default workspace, runner prefix, API tokens, Telegram config and log
  settings from the environment. Execution instances should inject explicit
  values rather than rely on defaults.

## Truthfulness rules for agents

- `applied` = write reached the API without failure; it is not proof that a
  later executor finished.
- `pending` = deferred, not yet applied.
- `unknown` = read state before retrying.
- A submitted operation is not a verdict; read `status`/`result`/`error`.
- Do not report a deferred or submitted request as a completed result, and
  never substitute hardcoded prose for an analyzer, reviewer or executor
  result. `cmd_analyze` reads the real operation outcome.
- Do not claim a skill, knowledge binding or integration works when the
  execution factory reports it disconnected.

## Related

- `src/kuzya/api/app.py`
- `src/kuzya/operation_worker.py`
- `src/kuzya/persistent_conversation.py`
- `src/pi_runner/core.py`, `src/pi_runner/cli.py`, `src/pi_runner/policy.mjs`
- `docs/sandbox-security.md`, `docs/deployment.md`
