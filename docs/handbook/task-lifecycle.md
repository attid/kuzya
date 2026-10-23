# Task lifecycle

## Completion subscriptions

`subscribe_task(task_id="8", instructions="Summarize the findings and continue the agreed plan")`
subscribes the current conversation to one task outcome. The host supplies the
conversation identity; the model cannot redirect a subscription to another
conversation. Telegram users can also send `/watch 8` (with optional instructions)
or `/unwatch 8`. Cancellation is available until the event is queued.

The subscription is stored before returning success. On completion, failure or
a request for clarification, the service atomically appends an explicitly marked
`task_event` input and enqueues a normal secretary turn in the original dialogue.
It is not a new human instruction or approval. The secretary reads the result,
answers in Telegram and may continue only the previously authorized plan.
Polling recovers missed completion callbacks after a restart. Repeated subscription
calls are idempotent and do not generate another model call for the same subscription.

API: `POST /tasks/{id-or-number}/subscriptions` with `conversation_id` and optional
`instructions`; `DELETE` at the same path cancels a pending subscription.
The response exposes the subscription `status` and eventual `turn_id`.
Reply delivery retries separately from model execution and records per-part
progress; a failed Telegram send does not rerun the secretary. Transport delivery
is at-least-once across a crash in the send/acknowledge window.

Tasks expose a stable human `number` (`#1`, `#2`, …) alongside their internal
`id`. Display the number in Telegram and user conversations. `GET`/`PATCH
/tasks/5` resolve a number; exact internal IDs remain supported. Read the
returned `id` before submitting operations or accessing task-scoped resources.
Existing tasks are numbered in creation order by migration `0010_task_numbers`;
archival and status changes do not renumber them.

A task is a durable request for work. `TaskExecutionWorker` claims
`in_progress` tasks, runs the selected agent through the Pi Runner sandbox,
optionally runs an internal review cycle, and writes the result back to the
task row. Task `status` is free-form; the values below are the conventional
ones the worker and API use.

## Status values

| Status | Set by | Meaning |
|---|---|---|
| `incoming` | `POST /tasks`, `create_task` | Draft. Saved, not executed. |
| `in_progress` | `update_task` / `PATCH /tasks` | Authorizes execution; the worker claims it. |
| `waiting_for_input` | worker | The agent invoked `ask_clarification`; a human answer is required. This is the exact status spelling used by `TaskExecutionWorker` and `TaskRepository`. |
| `ready_to_commit` | worker | Code and review completed; waits for an explicit commit/rollback decision. |
| `done` | worker or Git commit operation | Finished/committed. |
| `failed` | worker | Runtime error, failed tests or an unhandled exception. |
| `cancelled` | Git rollback operation | Cancelled or reverted. |
| `archived` | `update_task` / `PATCH /tasks/{task_id}` (caller-chosen value) | Terminal operator state. Kept in storage and counted by `GET /tasks/counts`; hidden from active work by convention. The human `number` never changes, and the worker never claims it because the claim predicate requires `in_progress`. Unrelated to an archived agent profile (`runner_config.archived`). |

`incoming` does not run anything and does not schedule a future check. The
secretary must use `status="in_progress"` (or `update_task(..., status=
"in_progress")`) when the user explicitly asks to execute.

`status` is a free-form column, not an enum: `TaskPatch.status` is
`str | None` and `TaskRepository.update` stores `str(status)`. The table lists
the conventional values the worker and API actually use, not the only legal
ones. An unknown value is stored and counted; it simply matches no worker
transition. Source of truth: `src/kuzya/storage/repository.py` (`update`,
`claim_in_progress`, `get_status_counts`) and `src/kuzya/api/app.py`
(`PATCH /tasks/{task_id}`, `GET /tasks/counts`). See
[Tools and API](tools-and-api.md#deferred-operations-and-git-outcomes) for the
`done` outcome of a commit operation with no changes.

## Telegram category tabs

The `/list` keyboard is a view over statuses, not a separate state machine.
Regular tabs keep their exact-status filters; the "Other" tab is defined by
exclusion so a status added later cannot become invisible.

| Tab | `GET /tasks` filter |
|---|---|
| Новые | `status=incoming` |
| Работа | `status=in_progress` |
| Ошибки | `status=failed` |
| Прочее | `status_not_in=incoming,in_progress,failed,done,archived` |
| Готово | `status=done` |
| Архив | `status=archived` |

The "Other" counter uses the same exclusion: it sums every stored status
outside the four regular counted tabs and the archive, so the badge always
matches the list (archive remains an unlabelled tab). Rows keep the `⚠️`
marker for `waiting_for_input` and use `⏳` for `ready_to_commit`; other
statuses use the neutral `📌`.

## Direct conversations with task agents

A task agent can also be used as an interactive counterpart without creating a
task per message. Eligibility is the explicit `runner_config.talkable: true`
flag on the agent's current profile. There is one profile per agent; no
`-talk` profile copy exists.

Telegram flow:

1. `/talk` shows the secretary controls plus «👥 Выбрать агента».
2. The agent list contains only `talkable` profiles, up to ten per page.
3. Selecting an agent creates, or resumes, one conversation with
   `metadata.talkable_agent` and calls `POST /conversations/{id}/talk`
   (`start`). The Telegram user state switches to mode `agent_talk`. If the
   workspace is free the talk becomes active immediately; if a task or
   another talk owns it, the talk is marked `talk_pending` and the first turn
   waits in the dispatcher.
4. Every reply from that conversation carries «🏁 Закончить» and
   «💬 С секретарём».
5. The task-category keyboard shows «🗣 Разговоры: N». The list behind it
   names each agent, its start time and an end button.
6. «🏁 Закончить» calls `request_end` and immediately moves the user back to
   the secretary context. New turns are refused (`409` on
   `POST .../turns`), pending Telegram-queue messages are cancelled, and the
   workspace lock is released by `finalize` only after the current turn and
   every already-enqueued turn have finished. The conversation and its
   native Runner session are kept, so selecting the same agent later resumes
   the same context.

The direct conversation uses the same frozen profile, sandbox policy,
workspace and Runner session mechanics as the secretary. Starting and ending
a talk and claiming a task all take the SQLite write lock before reading
(`BEGIN IMMEDIATE`), so they share one atomic workspace decision:
`start`/`acquire_for_turn` refuse to run beside a task or another talk, and
`TaskRepository.claim_in_progress` refuses to run beside an active talk. A
busy turn is returned to `pending` by the dispatcher instead of failing and
records `metadata.workspace_deferred_until`; candidate selection skips it
until that retry deadline, so the busy conversation cannot starve independent
dialogues or make the dispatcher spin. Turn order inside the conversation is
preserved. No task, review, commit or platform summary is created for an agent
talk; the agent reports in its reply and the Runner trace remains the
execution record.

An active talk is not a task and never appears in a task category. It does
occupy the agent's workspace: `TaskRepository.claim_in_progress` skips a task
for the same agent, and a task whose resolved project directory is
`talk_workspace`, until `talk_active` is false. Queued tasks proceed after
the closing talk has drained and been finalized.

## Creating and editing

- `POST /tasks` with `id` optional; duplicate caller ids return `409`. Accepts optional `attachments` list.
- `GET /tasks/counts` returns `{status: count}` for every stored status. It is a
  single grouped SQL aggregate, so callers do not load task rows merely to
  render category counters.
- `PATCH /tasks/{task_id}` updates only supplied fields
  (`title`, `description`, `status`, `workspace`, `agent_id`, `metadata`, `attachments`);
  `metadata` is shallow-merged into the stored object, preserving keys that
  are not supplied. Setting `status="in_progress"` clears a previous
  `metadata.execution_status` (for example `completed`) so the worker claims
  the task again and a resumed session starts from the new instructions. If the
  task already has an active `execution_status == "running"` claim, that marker
  is preserved and `PATCH` returns `409 Conflict`
  (`task is already executing on worker <worker_id>: <task_id>`) instead of
  making the task claimable a second time.
- `POST /tasks/{task_id}/attachments` accepts multipart/form-data upload (`file: UploadFile`), writes the file to the task artifact store, registers it in `task.metadata["attachments"]`, and returns `TaskArtifactRead` with `201 Created`.
- `POST /imports/tasks` imports historical tasks by stable id. Replaying the
  same ids never duplicates or overwrites, and validation rejects the whole
  batch before any write.
- `create_task` / `update_task` preflight the workspace and executor first
  (see [Workspaces and routing](workspaces-and-routing.md)) and copy the
  resolved `host_path` into `tasks.workspace`. Both accept optional `attachments`.

## Worker claim and workspace lock

`TaskRepository.claim_in_progress` (`src/kuzya/storage/repository.py`)
skips a candidate when another task in the same `workspace` is
`ready_to_commit` or already `in_progress`. A candidate whose metadata already
contains `execution_status == "running"` is never claimed again: it remains
owned by the worker that set the marker. This serializes work per workspace
while allowing different workspaces to run in parallel.

## Execution

`TaskExecutionWorker.step` (`src/kuzya/task_worker.py`):

1. claims one `in_progress` task;
2. selects `task.agent_id` or the worker default, loads the current profile and
   rejects archived profiles;
3. resolves the workspace again and writes the effective context to
   `task.metadata.execution_context`;
4. builds a role configuration from `runner_config`, pointing `project` at the
   resolved host directory, extracting `skills`, and enabling
   `allow_git_commit` for `devops` tasks or `metadata.allow_git_commit`;
   stages declared task attachments from host paths or artifact references into `/inbox` with read-only permissions (`0o444`), mounted via `--ro-bind` at the root `/inbox`;
5. reuses a prior native Runner session only when both the executor and the
   workspace are unchanged; otherwise it starts a new session and keeps older
   artifacts in their original directory;
6. runs the child outside any database transaction and stores summary,
   `changed_files`, errors and provenance.

## Manual stop (interrupt)

`POST /tasks/{task_id}/interrupt` (`tasks.write` on `task:<id>`) requests a
manual stop of a **running** execution. The API marks
`metadata.execution_stop_requested_at` and signals the runner's process group
(SIGTERM, escalating to SIGKILL after a grace window) through the shared
`TaskProcessRegistry`; the Telegram task card exposes the same action as a
⏹ button while `execution_status == "running"`.

A manual stop is not a failure: the worker records the task
`waiting_for_input` with a "stopped manually" clarification and the newest
resumable session id. Replying to the clarification resumes that session with
the new instructions (see [Resume a session](#resume-a-session)). Workspace
changes made before the stop are not rolled back — use the Git rollback
operation afterwards if needed. There is no automatic timeout: stops happen
only on request.

## Internal review cycle

When the task has `changed_files` and review is required (coding tasks by
default; a profile/metadata flag can disable it), the worker runs up to three
review rounds with `system-code-reviewer` before marking the task. The
reviewer runs read-only against the project and writes reports to task
artifacts (`review_round_<n>.md`). The executor records the JSON verdict
`approved` or `changes_requested` (`src/kuzya/review_executor.py`); older text
markers such as `ВЕРДИКТ: ОДОБРЕНО` are **not** parsed. Fix rounds accumulate
`changed_files` across rounds (ordered union) instead of replacing the
previous round's list. See
[Review](review.md) for the separation between this in-worker cycle and the
pending standalone review executor.

## Resume a session

`resume_task_session(task_id, message)`:

- reads the task, appends `{text, timestamp}` to
  `metadata.additional_instructions`;
- `PATCH`es the task to `status="in_progress"` with the updated metadata.

It does not itself run the model; it feeds the next worker claim, which
resumes the prior native session when the executor and workspace are
unchanged. The `PATCH` to `in_progress` clears a terminal `metadata.execution_status`
(for example `completed`), so it cannot keep the task out of the claim
predicate. An active `running` claim is different: the repository preserves
the marker and the API rejects the request with `409 Conflict`, because the
current worker still owns the execution and must not be raced by a second
claim. The worker renders `task.md` from the original description, every
`additional_instructions` entry with its timestamp, and the current
`review_feedback`.

Resume is allowed for terminal statuses and is an execution authorization:

- The tool's only refusal is an API read failure, not a task status. It first
  calls `GET /tasks/{task_id}` through `backendRequest`, whose envelope is
  `{status: "ok"|"failed", data}` and whose `status` describes the HTTP read
  (`src/pi_runner/policy.mjs:66-85`). It returns early only when
  `current.status == "failed"`. The task status lives in
  `current.data.status` and is never inspected; the subsequent `PATCH` runs
  unconditionally (`policy.mjs:564-573`). The server route does not check the
  previous task status either.
- Resume from `failed` is therefore allowed and requeues execution exactly
  like `done`/`archived`. There is no `failed` guard and no status filter: the
  `PATCH` sets `status="in_progress"` plus the new instructions.
- Setting `in_progress` starts a **new execution cycle** through the normal
  claim predicate; it is not merely a note appended to the instructions.
  When the executor and workspace are unchanged the worker resumes the prior
  native Runner session, so its attempt counter continues (a `done` task was
  observed resuming at `attempt 7`).
- Treat it as an explicit request to execute again and confirm intent before
  resuming a `done`, `archived` or `failed` task.

## Turns (secretary lifecycle)

Secretary conversations use a separate durable protocol
(`src/kuzya/secretary.py`, `src/kuzya/dispatcher.py`):

- `POST /conversations/{id}/turns` enqueues one `pending` turn for an existing
  inbound user message. `input_message_id` is unique, so a replay returns the
  original turn.
- `QueueDispatcher` claims one eligible turn at a time per conversation
  (`claim_next`), runs the handler outside any transaction, then completes or
  fails with the claim token.
- A terminal `failed` turn retains its error and does not block later input.
  Pending and running predecessors still enforce FIFO. Failed input is never
  replayed automatically; `TurnProcessor.retry_failed` may reopen it only before
  any later turn has started.
- A terminated Runner reporting a connection failure for the pinned resumed
  session releases its reservation for new input in that same session. Unknown
  child failures and session mismatches retain the fail-closed runtime latch;
  later input receives an error rather than waiting behind a failed turn.
- `TurnProcessor.recover_abandoned(turn_id, claim_token=...)` reopens an
  abandoned `running` turn; recovery is caller-authorized, never guessed.
- `reconcile_startup_orphans` fails `running` turns left by a terminated
  process so a conversation is never permanently blocked.

The dispatcher loop never dies from one bad turn; loop-level exceptions are
logged and polling continues.

## Operation execution permissions and discovery

- Each task execution receives an execution-scoped backend credential derived
  from its configured tools; a raw HTTP call cannot exceed that scope
  (`src/kuzya/permissions.py`).
- Workers build an explicit per-instance context rather than reading a host
  path default (`src/kuzya/instance.py`).
- The `review` and `analysis` operation kinds have registered executors
  (`src/kuzya/operation_handlers.py`); a kind with no executor still
  fails visibly. See [Review](review.md) and
  [Analysis and recommendations](analysis-and-recommendations.md).
- Secretary and `system-*` executions receive the injected platform discovery
  on every run/resume without replacing the stored identity prompt (see
  [Profiles, runtime and skills](profiles-runtime-skills.md#platform-operating-instruction-injection)).

## Related

- `src/kuzya/task_worker.py`
- `src/kuzya/secretary.py`
- `src/kuzya/dispatcher.py`
- `src/kuzya/storage/repository.py`
- `src/kuzya/storage/models.py`
