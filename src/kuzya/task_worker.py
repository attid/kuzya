"""Background task execution worker for running developer agents on in_progress tasks."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .capabilities import write_handbook_discovery
from .instance import InstanceContext
from .logging import bind_log_context
from .permissions import (
    ExecutionPermissions,
    actions_for_tools,
    resources_for_execution,
)
from .workspaces import resolve_workspace
from .runtime_defaults import RuntimeDefaultsResolver
from .runner_output import extract_runner_result
from .profile_runtime import _build_role_config
from .review_executor import (
    VERDICT_APPROVED,
    ReviewExecutionError,
    ReviewExecutor,
    ReviewRequest,
)
from .storage import (
    AgentProfileRepository,
    Database,
    Task,
    TaskRepository,
)

logger = logging.getLogger(__name__)

#: Upper bound for the task worker pool. Each worker is an OS thread and may
#: launch a sandboxed child process, so unbounded values are rejected early.
MAX_TASK_CONCURRENCY = 32

#: Interpreter file names accepted when locating CPython among the role's
#: ``runtime_paths`` (``python``, ``python3``, ``python3.14``, ...).
_PYTHON_EXE_RE = re.compile(r"^python(?:(\d+(?:\.\d+)*))?$")

#: Sandbox-writable location for a test environment when the project itself is
#: mounted read-only. The Runner mounts a writable tmpfs at ``/tmp``, so the
#: environment can be built there while the project stays untouched.
READONLY_TEST_ENV_DIR = "/tmp/kuzya-venv"


def _python_executable_rank(name: str) -> tuple[int, int]:
    """Rank interpreter names so a versioned binary wins over bare aliases."""
    match = _PYTHON_EXE_RE.match(name)
    version = (match.group(1) if match else None) or ""
    return (version.count("."), len(version))


def _resolve_python_interpreter(runtime_paths: Any) -> str | None:
    """Return the canonical CPython executable mounted for the sandbox.

    ``runner_config.runtime_paths`` entries are bind-mounted read-only at the
    same absolute path in every sandbox, so an interpreter taken from there
    remains valid across executions and sandboxes regardless of HOME. Each
    entry may be the interpreter itself or a directory holding ``bin/python*``.
    Only existing executables are returned; missing or broken entries are
    skipped so the caller never advertises a path that cannot be executed.
    """
    if not isinstance(runtime_paths, (list, tuple)):
        return None
    for raw in runtime_paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = Path(raw)
        if entry.is_file():
            candidates = [entry]
        elif entry.is_dir():
            bin_dir = entry / "bin"
            if not bin_dir.is_dir():
                continue
            candidates = list(bin_dir.iterdir())
        else:
            continue
        usable = [
            candidate
            for candidate in candidates
            if _PYTHON_EXE_RE.match(candidate.name)
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ]
        if usable:
            usable.sort(key=lambda item: _python_executable_rank(item.name), reverse=True)
            return str(usable[0])
    return None


def _testing_section_lines(
    interpreter: str | None, *, project_read_only: bool
) -> list[str]:
    """Render the canonical test-running recipe for ``workspace.md``.

    An empty list means the role exposes no CPython runtime, so no recipe is
    advertised: a missing interpreter must not turn into a stale or wrong path.

    When the project is mounted read-only, ``rm -rf .venv`` cannot work and
    would abort the whole recipe at the first ``&&``. The read-only branch
    therefore builds the environment in the writable sandbox ``/tmp`` and runs
    pytest through that environment's interpreter instead.
    """
    if interpreter is None:
        return []
    lines = [
        "## Testing",
        "- Canonical Python interpreter, mounted read-only in every sandbox at this"
        f" absolute path: {interpreter}",
        "- A repository `.venv` may have been created in another sandbox and link to"
        " a HOME that does not exist here (for example `/home/runner/...`). If"
        " `.venv/bin/python -V` fails, do not spend time diagnosing it.",
    ]
    if project_read_only:
        lines += [
            "- This workspace is mounted read-only, so a stale project `.venv` cannot"
            " be removed or recreated. Create the test environment in the writable"
            f" `/tmp` directory instead: `UV_PROJECT_ENVIRONMENT={READONLY_TEST_ENV_DIR}"
            f" UV_PYTHON={interpreter} uv sync --frozen --no-install-project`."
            " `--frozen` keeps uv from rewriting the lock file in the read-only"
            " workspace.",
            "- Without uv, create that environment directly:"
            f" `{interpreter} -m venv {READONLY_TEST_ENV_DIR}` and install the test"
            f" dependencies into it (`{READONLY_TEST_ENV_DIR}/bin/python -m pip install"
            " pytest`, plus any dependency pytest reports as missing). The project"
            " itself is imported through `PYTHONPATH=src` and needs no install.",
            "- Run the tests as"
            f" `PYTHONPATH=src {READONLY_TEST_ENV_DIR}/bin/python -m pytest`.",
        ]
    else:
        lines += [
            "- In that case recreate the environment with the canonical interpreter:"
            f" `rm -rf .venv && UV_PYTHON={interpreter} uv sync --no-install-project`,"
            f" or without uv: `rm -rf .venv && {interpreter} -m venv .venv &&"
            " .venv/bin/python -m pip install -e . pytest`.",
            "- Run the tests as `PYTHONPATH=src .venv/bin/python -m pytest`.",
        ]
    lines.append(
        "- Do not commit `.venv`; the project `.gitignore` normally already excludes it."
    )
    return lines


def _metadata_text(value: Any) -> str:
    """Render an arbitrary metadata value as readable task-prompt text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in (
            "text",
            "instruction",
            "message",
            "feedback",
            "summary",
            "comment",
            "description",
            "report",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value).strip()


def _timestamped_block(value: Any, *, heading: str) -> str:
    """Render one instruction/feedback entry, preserving its timestamp."""
    timestamp: Any = None
    if isinstance(value, Mapping):
        for key in ("timestamp", "created_at", "at", "date"):
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                timestamp = candidate
                break
    text = _metadata_text(value)
    if not text:
        return ""
    label = str(timestamp).strip() if timestamp is not None else ""
    return f"### {heading}{f' — {label}' if label else ''}\n{text}"


def _render_additional_instructions(value: Any) -> str:
    """Render every ``metadata.additional_instructions`` entry in order."""
    if value is None:
        return ""
    entries = value if isinstance(value, (list, tuple)) else [value]
    blocks = [
        block
        for entry in entries
        if (block := _timestamped_block(entry, heading="Дополнительное указание"))
    ]
    return "\n\n".join(blocks)


def _render_review_feedback(value: Any) -> str:
    """Render the current ``metadata.review_feedback`` value."""
    if value is None:
        return ""
    entries = value if isinstance(value, (list, tuple)) else [value]
    blocks = [
        block
        for entry in entries
        if (block := _timestamped_block(entry, heading="Замечания ревьювера"))
    ]
    return "\n\n".join(blocks)


def render_task_document(task: Task) -> str:
    """Build the ``task.md`` prompt for one execution.

    The original description is followed by every accumulated additional
    instruction (with its timestamp) and the current review feedback, so a
    resumed native session receives the context that prompted the resume.
    """
    sections = [f"# Task {task.id}: {task.title}"]
    if task.description:
        sections.append(task.description)
    meta = dict(task.meta or {})
    instructions = _render_additional_instructions(
        meta.get("additional_instructions")
    )
    if instructions:
        sections.append(
            "## Дополнительные указания (additional_instructions)\n\n"
            + instructions
        )
    feedback = _render_review_feedback(meta.get("review_feedback"))
    if feedback:
        sections.append(
            "## Замечания ревьювера (review_feedback)\n\n" + feedback
        )
    return "\n\n".join(sections) + "\n"


#: Fallback when the destination filesystem does not expose ``PC_NAME_MAX``.
_DEFAULT_NAME_MAX = 255
#: Deterministic digest lengths used for collision and length fallbacks.
_ARTIFACT_HASH_LENGTHS = (12, 16, 24, 32, 64)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` filesystem bytes.

    File-name limits are byte limits, not character limits. ``os.fsencode``
    accepts surrogate-escaped input, and ignoring a split multi-byte sequence
    never leaves an invalid Unicode name.
    """
    if max_bytes <= 0:
        return ""
    encoded = os.fsencode(text)
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _split_artifact_extension(name: str) -> tuple[str, str]:
    """Split a conventional final extension while leaving hidden names alone."""
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return name, ""
    return name[:dot], name[dot:]


def _filesystem_name_max(directory: Path) -> int:
    """Return the real per-component file-name limit for ``directory``."""
    try:
        value = os.pathconf(directory, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        return _DEFAULT_NAME_MAX
    return value if isinstance(value, int) and value > 0 else _DEFAULT_NAME_MAX


def _safe_text(value: str) -> str:
    """Make filesystem-derived text safe for JSON, URLs and log lines.

    Surrogate-escaped filename bytes and control characters are replaced with
    printable ``~XX`` markers. A tilde is URL-safe and the original relative
    path still drives collision hashing, so distinct files stay distinct.
    """
    chunks: list[str] = []
    for char in value:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            chunks.append(f"~{code - 0xDC00:02X}")
        elif 0xD800 <= code <= 0xDFFF:
            chunks.append(f"~u{code:04X}")
        elif code < 0x20 or code == 0x7F:
            chunks.append(f"~{code:02X}")
        else:
            chunks.append(char)
    return "".join(chunks)


def _artifact_error_entry(source: Any, error: Any) -> dict[str, str]:
    """Build a provenance-safe artifact-error record."""
    return {
        "source": _safe_text(str(source)),
        "error": _safe_text(str(error)),
    }


def _artifact_base_name(relative_path: str) -> str:
    """Join relative path components with ``_`` to form the artifact name."""
    return "_".join(
        _safe_text(part) for part in relative_path.split("/")
    )


def _hashed_artifact_name(
    base_name: str,
    relative_path: str,
    max_bytes: int,
    digest_length: int,
) -> str:
    """Shorten ``base_name`` deterministically, retaining extension and hash.

    The digest is derived from the original relative path, so equal visible
    prefixes remain distinguishable even after truncation or collision
    handling.
    """
    stem, suffix = _split_artifact_extension(base_name)
    digest = hashlib.sha256(os.fsencode(relative_path)).hexdigest()
    marker = f"_{digest[:digest_length]}"
    marker_bytes = len(os.fsencode(marker))
    if marker_bytes >= max_bytes:
        return _truncate_utf8(digest, max_bytes) or "artifact"

    # Preserve the extension whenever the name budget allows it. Pathological
    # extensions are themselves truncated rather than overflowing the name.
    suffix = _truncate_utf8(suffix, max_bytes - marker_bytes)
    budget = max_bytes - marker_bytes - len(os.fsencode(suffix))
    prefix = _truncate_utf8(stem, budget)
    name = prefix + marker + suffix
    return name or _truncate_utf8(digest, max_bytes) or "artifact"


def _allocate_artifact_names(
    relative_paths: Sequence[str],
    *,
    max_bytes: int,
    reserved: Sequence[str] = (),
) -> dict[str, str]:
    """Map every relative outbox path to a unique artifact file name.

    Root files keep their original name unless it collides with a reserved
    generated artifact (``summary.md``/``session.jsonl``). Nested paths have
    their components joined with ``_``. Collisions and over-long names get a
    short hash of the original relative path, making allocation deterministic
    and lossless. Names already reserved by the worker are never overwritten.
    """
    reserved_names = set(reserved)
    ordered = sorted(
        set(relative_paths),
        key=lambda rel: (
            "/" in rel,
            _artifact_base_name(rel) in reserved_names,
            rel,
        ),
    )
    used = set(reserved_names)
    allocated: dict[str, str] = {}
    for relative_path in ordered:
        base_name = _artifact_base_name(relative_path)
        if len(os.fsencode(base_name)) <= max_bytes:
            candidate = base_name
        else:
            candidate = _hashed_artifact_name(
                base_name,
                relative_path,
                max_bytes,
                _ARTIFACT_HASH_LENGTHS[0],
            )
        if candidate in used:
            for digest_length in _ARTIFACT_HASH_LENGTHS:
                candidate = _hashed_artifact_name(
                    base_name,
                    relative_path,
                    max_bytes,
                    digest_length,
                )
                if candidate not in used:
                    break
            else:
                counter = 2
                while True:
                    candidate = _hashed_artifact_name(
                        base_name,
                        f"{relative_path}#{counter}",
                        max_bytes,
                        _ARTIFACT_HASH_LENGTHS[-1],
                    )
                    if candidate not in used:
                        break
                    counter += 1
        used.add(candidate)
        allocated[relative_path] = candidate
    return allocated


def _outbox_relative_files(
    outbox_root: Path,
    errors: list[dict[str, str]],
) -> list[str]:
    """Discover regular files below ``outbox_root`` without following links."""

    def record(path: Path, reason: str) -> None:
        try:
            relative = path.relative_to(outbox_root).as_posix()
        except ValueError:
            relative = str(path)
        entry = _artifact_error_entry(relative, reason)
        logger.warning(
            "Skipping outbox entry %r: %s",
            entry["source"],
            entry["error"],
        )
        errors.append(entry)

    def on_scan_error(error: OSError) -> None:
        path = Path(error.filename) if error.filename else outbox_root
        record(path, f"directory scan failed: {error}")

    files: list[str] = []
    try:
        walker = os.walk(
            outbox_root,
            topdown=True,
            followlinks=False,
            onerror=on_scan_error,
        )
        for dirpath, dirnames, filenames in walker:
            dirnames.sort()
            for dirname in list(dirnames):
                candidate = Path(dirpath) / dirname
                if candidate.is_symlink():
                    record(candidate, "symlinked directories are not followed")
                    dirnames.remove(dirname)
            filenames.sort()
            for filename in filenames:
                candidate = Path(dirpath) / filename
                if candidate.is_symlink():
                    record(candidate, "symlinked files are not collected")
                    continue
                try:
                    is_regular = candidate.is_file()
                except OSError as error:
                    record(candidate, f"cannot inspect file: {error}")
                    continue
                if not is_regular:
                    record(candidate, "not a regular file")
                    continue
                files.append(candidate.relative_to(outbox_root).as_posix())
    except OSError as error:
        record(outbox_root, f"directory scan failed: {error}")
    return files


def _collect_outbox_artifacts(
    outbox_dir: str | os.PathLike[str],
    destination_dir: str | os.PathLike[str],
    *,
    reserved_names: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Copy every regular file below ``outbox_dir`` into the artifact store.

    Returns ``(collected, errors)``. Only successfully copied files appear in
    ``collected``; failures are logged and reported in ``errors`` so a caller
    can never mistake a partial collection for a complete attachment list.
    Symlinks are never followed and every resolved source must stay inside the
    outbox root.
    """
    outbox_path = Path(outbox_dir)
    destination_path = Path(destination_dir)
    errors: list[dict[str, str]] = []
    if not outbox_path.is_dir():
        entry = _artifact_error_entry(
            outbox_path,
            "outbox directory is not available",
        )
        logger.warning(
            "No outbox artifacts collected from %r: %s",
            entry["source"],
            entry["error"],
        )
        return [], [entry]
    try:
        outbox_root = outbox_path.resolve(strict=True)
    except OSError as error:
        entry = _artifact_error_entry(outbox_path, error)
        logger.warning(
            "Cannot resolve outbox directory %r: %s",
            entry["source"],
            entry["error"],
        )
        return [], [entry]
    try:
        destination_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        entry = _artifact_error_entry(destination_path, error)
        logger.error(
            "Cannot create task artifact directory %r: %s",
            entry["source"],
            entry["error"],
        )
        return [], [entry]

    relative_paths = _outbox_relative_files(outbox_root, errors)
    names = _allocate_artifact_names(
        relative_paths,
        max_bytes=_filesystem_name_max(destination_path),
        reserved=reserved_names,
    )
    collected: list[dict[str, Any]] = []
    for relative_path in relative_paths:
        source = outbox_root / relative_path
        destination = destination_path / names[relative_path]
        temp_destination = destination_path / f".collect-{uuid.uuid4().hex}.tmp"
        try:
            resolved_source = source.resolve(strict=True)
            if not resolved_source.is_relative_to(outbox_root):
                raise ValueError("resolved path escapes the outbox root")
            shutil.copy2(resolved_source, temp_destination)
            size = temp_destination.stat().st_size
            os.replace(temp_destination, destination)
        except Exception as error:  # A broken artifact must not fail the task.
            entry = _artifact_error_entry(relative_path, error)
            logger.warning(
                "Failed to collect outbox artifact %r as %r: %s",
                entry["source"],
                destination.name,
                entry["error"],
            )
            errors.append(entry)
            try:
                temp_destination.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning(
                    "Cannot remove partial outbox copy %s: %s",
                    temp_destination,
                    cleanup_error,
                )
            continue
        collected.append(
            {
                "name": destination.name,
                "path": str(destination),
                "size": size,
            }
        )
        logger.info(
            "Collected outbox artifact %r as %r",
            _safe_text(relative_path),
            destination.name,
        )
    return collected, errors


@dataclasses.dataclass(frozen=True)
class TaskOutcome:
    """Outcome of one task execution."""

    task_id: str
    status: str  # "completed" or "failed"
    summary: str | None = None
    changed_files: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0
    worker_id: str | None = None


def latest_session_id(state_dir: Path) -> str | None:
    """Best-effort discovery of the newest runner session under ``state_dir``.

    A manually interrupted runner dies before printing its result JSON, so
    the session id cannot come from stdout. The newest session directory
    that still holds a session journal is used instead — the journal is
    append-only, so a session killed mid-turn stays resumable.
    """
    sessions_root = state_dir / "sessions"
    if not sessions_root.is_dir():
        return None
    candidates = sorted(
        (
            entry
            for entry in sessions_root.iterdir()
            if entry.is_dir() and (entry / "pi" / "session.jsonl").is_file()
        ),
        key=lambda entry: (entry / "pi" / "session.jsonl").stat().st_mtime,
    )
    return candidates[-1].name if candidates else None


class TaskProcessRegistry:
    """Live handles to running task runner processes, for manual interrupt.

    Each runner is started in its own session (process group), so one signal
    reaches the pi process and every node or bwrap child it spawned. A
    request sends SIGTERM to the group and a watchdog escalates to SIGKILL
    after a grace window, so a wedged child cannot outlive the request by
    ignoring the polite signal. Thread-safe: workers register from their own
    loops while the API thread interrupts.
    """

    def __init__(self, *, term_grace_seconds: float = 5.0) -> None:
        self._term_grace_seconds = float(term_grace_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[subprocess.Popen, list]] = {}

    def run_process(
        self,
        task_id: str,
        cmd: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess, bool]:
        """Run one runner process to completion, interruptible by task id.

        Returns the completed process (stdout/stderr captured like
        ``subprocess.run``) and whether an interrupt was requested for it.
        """
        process = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
        interrupted: list = [False]
        with self._lock:
            self._entries[task_id] = (process, interrupted)
        try:
            stdout, stderr = process.communicate()
        finally:
            with self._lock:
                self._entries.pop(task_id, None)
        completed = subprocess.CompletedProcess(
            list(cmd), process.returncode, stdout, stderr
        )
        return completed, bool(interrupted[0])

    def request_interrupt(self, task_id: str) -> bool:
        """Signal the runner of ``task_id`` to stop. False when not running."""
        with self._lock:
            entry = self._entries.get(task_id)
        if entry is None:
            return False
        process, interrupted = entry
        interrupted[0] = True
        self._signal_group(process, signal.SIGTERM)

        def _escalate() -> None:
            time.sleep(self._term_grace_seconds)
            if process.poll() is None:
                self._signal_group(process, signal.SIGKILL)

        threading.Thread(
            target=_escalate,
            name=f"task-interrupt-{task_id[:8]}",
            daemon=True,
        ).start()
        return True

    def running_task_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._entries)

    def _signal_group(self, process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            # The process (group) is already gone; the interrupt still
            # counts as delivered.
            pass


class TaskExecutionWorker:
    """Continuously claims in_progress tasks and executes them via pi_runner."""

    def __init__(
        self,
        database: Database,
        artifact_root: str | os.PathLike[str],
        *,
        agent_id: str = "kuzya-developer",
        runner_argv_prefix: Sequence[str] = (sys.executable, "-m", "pi_runner"),
        workspace_map: Mapping[str, str] | None = None,
        default_workspace: str | os.PathLike[str] | None = None,
        poll_interval_seconds: float = 1.0,
        worker_id: str | None = None,
        env: Mapping[str, str] | None = None,
        on_task_finished: Callable[[Task, TaskOutcome], None] | None = None,
        knowledge_base: Any | None = None,
        runtime_defaults: RuntimeDefaultsResolver | None = None,
        instance: InstanceContext | None = None,
        permissions: ExecutionPermissions | None = None,
        process_registry: TaskProcessRegistry | None = None,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.agent_id = agent_id
        self.runner_argv_prefix = list(runner_argv_prefix)
        self.workspace_map = dict(workspace_map or {})
        self.default_workspace = (
            Path(default_workspace).expanduser().resolve() if default_workspace else None
        )
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.worker_id = worker_id or f"task-worker-{uuid.uuid4().hex[:8]}"
        self.env = dict(env) if env is not None else None
        self.on_task_finished = on_task_finished
        self.knowledge_base = knowledge_base
        self.runtime_defaults = runtime_defaults or RuntimeDefaultsResolver()
        self.instance = instance
        self.permissions = permissions
        self.process_registry = process_registry or TaskProcessRegistry()

        self._repository = TaskRepository(database)
        self._agent_repository = AgentProfileRepository(database)

        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

        logger.info(
            "TaskExecutionWorker initialized: worker_id=%s, agent_id=%s, poll_interval=%.1fs, artifact_root=%s, default_workspace=%s",
            self.worker_id,
            self.agent_id,
            self.poll_interval_seconds,
            self.artifact_root,
            self.default_workspace,
        )

    def _resolve_workspace(self, workspace_name: str | None) -> Path:
        return resolve_workspace(workspace_name, default_workspace=self.default_workspace, workspace_map=self.workspace_map)

    def step(self) -> TaskOutcome | None:
        """Claim and execute at most one in_progress task.

        Returns None when no task was available. Catches all exceptions during
        execution and marks the task failed with diagnostic error without crashing.
        """
        task = self._repository.claim_in_progress(
            worker_id=self.worker_id, default_agent_id=self.agent_id
        )
        if task is None:
            return None

        started = time.perf_counter()
        ws_name = task.workspace or "default"
        with bind_log_context(
            worker_id=self.worker_id,
            task_id=task.id,
        ):
            logger.info(
                "Task claimed for execution: task_id=%s title=%r workspace=%s",
                task.id,
                task.title,
                ws_name,
                extra={"workspace": ws_name, "event": "task_claim"},
            )
            execution_grant = None
            try:
                selected_agent_id = task.agent_id if task.agent_id is not None else self.agent_id
                # Resolve the selected profile before choosing its default workspace.
                if self.knowledge_base is not None and hasattr(self.knowledge_base, "sync_agent_from_file"):
                    self.knowledge_base.sync_agent_from_file(selected_agent_id, self.database)
                profile = self._agent_repository.get_current(selected_agent_id)
                if profile is None:
                    raise RuntimeError(f"Agent profile not found in registry: {selected_agent_id}")
                if profile.runner_config.get("archived") or profile.role == "archived":
                    raise RuntimeError(f"Agent is archived: {selected_agent_id}")
                requested_workspace = task.workspace if task.workspace is not None else profile.runner_config.get("project")
                project_dir = self._resolve_workspace(requested_workspace)
                logger.info(
                    "Task %s workspace resolved to project_dir=%s",
                    task.id,
                    project_dir,
                    extra={"workspace": ws_name},
                )

                # 2. Prepare task directory and prompt
                exec_dir = (
                    self.artifact_root / f"task_{task.id}_{uuid.uuid4().hex[:8]}"
                )
                exec_dir.mkdir(parents=True, exist_ok=True)
                task_file = exec_dir / "task.md"
                task_text = render_task_document(task)
                task_file.write_text(task_text, encoding="utf-8")

                # 3. Prepare the selected execution configuration.
                meta_data = dict(task.meta or {})
                provenance_data = dict(meta_data.get("provenance") or {})
                resume_session_id = (
                    meta_data.get("runner_session_id")
                    or provenance_data.get("runner_session_id")
                    or provenance_data.get("session_id")
                )
                resume_state_dir = (
                    meta_data.get("runner_state_dir")
                    or provenance_data.get("runner_state_dir")
                )

                previous_context = meta_data.get("execution_context") or {}
                previous_agent = previous_context.get("agent_id") or provenance_data.get("agent_id")
                previous_project = previous_context.get("host_path") or provenance_data.get("project")
                if (previous_agent is not None and previous_agent != selected_agent_id) or (previous_project is not None and previous_project != str(project_dir)):
                    # A new executor or workspace must never inherit another native session.
                    # Earlier state and artifacts are retained in their original directory.
                    resume_session_id = None
                    resume_state_dir = None
                runner_config = dict(profile.runner_config or {})
                runner_config["project"] = str(project_dir)

                # Extract skills from profile
                skills = list(runner_config.get("skills") or [])
                if not skills and hasattr(profile, "skills") and profile.skills:
                    for sk in profile.skills:
                        if isinstance(sk, dict) and "skill" in sk:
                            skills.append(str(sk["skill"]))
                        elif isinstance(sk, str):
                            skills.append(sk)
                if skills:
                    runner_config["skills"] = skills

                # Allow git commit for devops tasks or explicit flag
                if meta_data.get("allow_git_commit") or meta_data.get("task_type") == "devops":
                    runner_config["allow_git_commit"] = True

                state_dir = (
                    Path(resume_state_dir)
                    if resume_state_dir and Path(resume_state_dir).is_dir()
                    else exec_dir / "state"
                )
                state_dir.mkdir(parents=True, exist_ok=True)
                runner_config["state_dir"] = str(state_dir)

                outbox_dir = exec_dir / "outbox"
                outbox_dir.mkdir(parents=True, exist_ok=True)
                runner_config["outbox_dir"] = str(outbox_dir)

                attachments = meta_data.get("attachments") or []
                if attachments:
                    inbox_dir = exec_dir / "inbox"
                    inbox_dir.mkdir(parents=True, exist_ok=True)
                    for att in attachments:
                        if isinstance(att, dict):
                            att_name = att.get("name") or att.get("artifact_name")
                            source_task_id = att.get("task_id")
                            source_path = att.get("path")
                        else:
                            att_name = getattr(att, "name", None) or getattr(att, "artifact_name", None)
                            source_task_id = getattr(att, "task_id", None)
                            source_path = getattr(att, "path", None)

                        resolved_path: Path | None = None
                        if source_path:
                            sp = Path(source_path)
                            if sp.is_file():
                                resolved_path = sp

                        if resolved_path is None:
                            effective_task_id = source_task_id or task.id
                            name_for_candidate = att_name or (Path(source_path).name if source_path else None)
                            if effective_task_id and name_for_candidate:
                                candidate = (
                                    self.artifact_root
                                    / str(effective_task_id)
                                    / "artifacts"
                                    / name_for_candidate
                                )
                                if candidate.is_file():
                                    resolved_path = candidate

                        if resolved_path is not None:
                            raw_target_name = att_name or resolved_path.name
                            safe_name = Path(raw_target_name).name
                            if not safe_name or safe_name in {".", ".."}:
                                safe_name = resolved_path.name
                            dest_file = inbox_dir / safe_name
                            try:
                                if dest_file.exists():
                                    try:
                                        dest_file.unlink()
                                    except OSError:
                                        dest_file.chmod(0o644)
                                shutil.copy2(resolved_path, dest_file)
                                dest_file.chmod(0o444)
                                logger.info(
                                    "Staged attachment %s to %s (chmod 0o444) for task %s",
                                    resolved_path,
                                    dest_file,
                                    task.id,
                                )
                            except Exception as err:
                                logger.warning(
                                    "Failed to stage attachment %s to %s for task %s: %s",
                                    resolved_path,
                                    dest_file,
                                    task.id,
                                    err,
                                )
                        else:
                            logger.warning(
                                "Attachment source file not found for task %s: att=%s, path=%s, candidate_task_id=%s, candidate_name=%s",
                                task.id,
                                att,
                                source_path,
                                source_task_id or task.id,
                                att_name,
                            )
                    runner_config["inbox_dir"] = str(inbox_dir)

                instructions = list(runner_config.get("instructions") or [])

                # Inject project summary if present in knowledge base
                if self.knowledge_base is not None:
                    summary_candidates = []
                    if task.workspace:
                        summary_candidates.append(
                            self.knowledge_base.projects_dir / task.workspace / "summary.md"
                        )
                    summary_candidates.append(
                        self.knowledge_base.projects_dir / project_dir.name / "summary.md"
                    )
                    for s_cand in summary_candidates:
                        if s_cand.is_file():
                            instructions.append(str(s_cand))
                            logger.info("Injected project summary from %s into task %s", s_cand, task.id)
                            break

                prompt_file = exec_dir / "system_prompt.md"
                prompt_file.write_text(profile.system_prompt, encoding="utf-8")
                discovery_path = write_handbook_discovery(
                    exec_dir,
                    agent_id=selected_agent_id,
                    tools=list(runner_config.get("tools") or []),
                )
                if discovery_path is not None:
                    instructions.append(str(discovery_path))
                # Propagate allowed_hosts from runner_config and task metadata
                allowed_hosts = list(runner_config.get("allowed_hosts") or [])
                task_meta = task.meta or {}
                if task_meta.get("allowed_hosts") and isinstance(task_meta.get("allowed_hosts"), list):
                    for host in task_meta["allowed_hosts"]:
                        if isinstance(host, str) and host not in allowed_hosts:
                            allowed_hosts.append(host)
                if allowed_hosts:
                    runner_config["allowed_hosts"] = allowed_hosts

                # Per-task mount controls. An explicit task flag wins over the
                # profile default; extra read-only directories merge on top of
                # the profile's list (the same merge pattern as allowed_hosts).
                # The knowledge root is appended later, after workspace.md is
                # rendered, so the note below states the requested mounts.
                if isinstance(task_meta.get("project_read_only"), bool):
                    runner_config["project_read_only"] = task_meta["project_read_only"]
                ro_paths = list(runner_config.get("readonly_paths") or [])
                if isinstance(task_meta.get("readonly_paths"), list):
                    for path in task_meta["readonly_paths"]:
                        if isinstance(path, str) and path not in ro_paths:
                            ro_paths.append(path)
                if ro_paths:
                    runner_config["readonly_paths"] = ro_paths

                # Resolve role defaults (pi_command/runtime_paths) before
                # rendering workspace.md so the testing recipe names the
                # interpreter the child sandbox actually mounts.
                runner_config["instructions"] = instructions
                runner_config = _build_role_config(
                    runner_config, agent_id=selected_agent_id, prompt_path=prompt_file
                )

                workspace_file = exec_dir / "workspace.md"
                workspace_lines = [
                    "# Execution workspace",
                    f"Host directory: {project_dir}",
                    "Sandbox root: /workspace",
                    f"Working directory: /workspace/{runner_config.get('workdir', '.')}",
                    f"Project read-only: {bool(runner_config.get('project_read_only', False))}",
                    f"Sandbox network: {runner_config.get('network', True)}",
                    f"Allowed hosts: {runner_config.get('allowed_hosts', [])}",
                ]
                extra_ro = [str(p) for p in (runner_config.get("readonly_paths") or [])]
                if extra_ro:
                    workspace_lines.append(
                        "Extra read-only mounts (visible at the same absolute paths): "
                        + ", ".join(extra_ro)
                    )
                workspace_lines += [
                    "Outbox: /outbox (writable artifacts; write output files, logs or reports here)",
                    "Inbox: /inbox (read-only input attachments)",
                    "Use the sandbox paths above. A directory named Project is not automatically added below /workspace.",
                ]
                testing_lines = _testing_section_lines(
                    _resolve_python_interpreter(runner_config.get("runtime_paths")),
                    project_read_only=bool(
                        runner_config.get("project_read_only", False)
                    ),
                )
                if testing_lines:
                    workspace_lines += [""] + testing_lines
                workspace_file.write_text("\n".join(workspace_lines) + "\n", encoding="utf-8")
                runner_config["instructions"].append(str(workspace_file))
                runtime_selection = self.runtime_defaults.resolve(runner_config)
                runner_config = runtime_selection.role_config
                execution_context = {
                    "agent_id": selected_agent_id,
                    "profile_revision": profile.revision,
                    "host_path": str(project_dir),
                    "sandbox_path": "/workspace",
                    "project_read_only": bool(runner_config.get("project_read_only", False)),
                    "readonly_paths": list(runner_config.get("readonly_paths") or []),
                    "network": runner_config.get("network", True),
                    "allowed_hosts": list(runner_config.get("allowed_hosts") or []),
                    "provider": runtime_selection.provider,
                    "model": runtime_selection.model,
                }
                self._repository.update(task.id, metadata={**(task.meta or {}), "execution_context": execution_context})

                # Ensure knowledge directory is in readonly_paths if available
                if (
                    self.knowledge_base is not None
                    and self.knowledge_base.root_dir.is_dir()
                ):
                    ro_paths = list(runner_config.get("readonly_paths") or [])
                    kb_root_str = str(self.knowledge_base.root_dir)
                    if kb_root_str not in ro_paths:
                        ro_paths.append(kb_root_str)
                    runner_config["readonly_paths"] = ro_paths

                # Explicit per-instance mounts and execution-scoped backend
                # credentials. Flagship/admin tokens never reach the child.
                if self.instance is not None:
                    if self.instance.knowledge_root.is_dir():
                        runner_config["knowledge_dir"] = str(
                            self.instance.knowledge_root
                        )
                    if self.instance.skills_root.is_dir():
                        runner_config["skills_dir"] = str(
                            self.instance.skills_root
                        )
                    if self.permissions is not None and self.instance.backend_url:
                        exec_actions = actions_for_tools(
                            runner_config.get("tools") or []
                        )
                        resources = resources_for_execution(
                            exec_actions,
                            task_id=task.id,
                            agent_id=selected_agent_id,
                        )
                        execution_grant = self.permissions.issue(
                            agent_id=selected_agent_id,
                            actions=exec_actions,
                            resources=resources,
                            label=f"task:{task.id}",
                        )
                        runner_config["backend"] = self.instance.child_backend(
                            execution_grant.token
                        )

                role_file = exec_dir / "role.json"
                role_file.write_text(
                    json.dumps(runner_config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.debug(
                    "Prepared execution files in exec_dir=%s (role=%s, task=%s)",
                    exec_dir,
                    role_file,
                    task_file,
                )

                # 4. Invoke child runner (resume if previous session exists)
                if (
                    resume_session_id
                    and state_dir.is_dir()
                    and (state_dir / "sessions" / resume_session_id).is_dir()
                ):
                    cmd = [
                        *self.runner_argv_prefix,
                        "resume",
                        "--state-dir",
                        str(state_dir),
                        "--session",
                        resume_session_id,
                        "--agent",
                        str(role_file),
                        "--task",
                        str(task_file),
                    ]
                    logger.info("Resuming existing runner session %s for task %s", resume_session_id, task.id)
                else:
                    cmd = [
                        *self.runner_argv_prefix,
                        "run",
                        "--agent",
                        str(role_file),
                        "--task",
                        str(task_file),
                    ]

                child_env = dict(self.env or os.environ)
                logger.info(
                    "Invoking runner command for task %s: cmd=%s cwd=%s",
                    task.id,
                    cmd,
                    project_dir,
                )
                result, interrupted = self.process_registry.run_process(
                    task.id,
                    cmd,
                    cwd=str(project_dir),
                    env=child_env,
                )

                duration_ms = (time.perf_counter() - started) * 1000.0
                logger.info(
                    "Runner finished for task %s: returncode=%d duration_ms=%.1f stdout_len=%d stderr_len=%d interrupted=%s",
                    task.id,
                    result.returncode,
                    duration_ms,
                    len(result.stdout),
                    len(result.stderr),
                    interrupted,
                )
                if result.stderr:
                    logger.warning(
                        "Runner stderr for task %s:\n%s",
                        task.id,
                        result.stderr.strip()[:2000],
                    )

                if interrupted:
                    # A manual stop is not a failure: the session journal is
                    # append-only and stays resumable, so the task lands in
                    # waiting_for_input with an explicit "what next?"
                    # clarification instead of a crash diagnostic.
                    logger.info(
                        "Task %s interrupted by request; session will be resumable",
                        task.id,
                        extra={"event": "task_interrupted", "workspace": ws_name},
                    )
                    runner_result = {
                        "status": "waiting_for_input",
                        "interrupted": True,
                        "session_id": latest_session_id(state_dir),
                        "changed_files": [],
                        "clarification": {
                            "title": "Задача остановлена вручную",
                            "description": (
                                "Выполнение прервано командой «Остановить». "
                                "Изменения в рабочей папке не откатаны."
                            ),
                            "question": (
                                "Что делать дальше? Ответьте сообщением — "
                                "агент продолжит сессию с новыми указаниями."
                            ),
                        },
                    }
                    status = "waiting_for_input"
                    summary_text = "Выполнение остановлено вручную."
                    summary_path = None
                    changed_files = []
                elif result.returncode != 0:
                    err_msg = (
                        f"Runner process exited with code {result.returncode}.\n"
                        f"Stderr: {result.stderr[:2000]}\n"
                        f"Stdout: {result.stdout[:1000]}"
                    )
                    raise RuntimeError(err_msg)
                else:
                    stdout_clean = result.stdout.strip()
                    runner_result = extract_runner_result(result.stdout)
                    if runner_result is None:
                        raise RuntimeError(
                            "Failed to parse runner JSON output: no JSON object "
                            f"found. Stdout: {stdout_clean[:1000]!r}"
                        )

                    status = runner_result.get("status")
                    if status not in ("completed", "waiting_for_input"):
                        raise RuntimeError(
                            f"Runner reported status {status!r}: {runner_result.get('errors_path')}"
                        )

                    summary_path = runner_result.get("summary_path")
                    summary_text = (
                        Path(summary_path).read_text(encoding="utf-8")
                        if summary_path and Path(summary_path).exists()
                        else ("Требуется уточнение вопроса." if status == "waiting_for_input" else "Задача выполнена.")
                    )
                    changed_files = list(runner_result.get("changed_files") or [])

                # Store session details in runner_result for provenance
                session_id = runner_result.get("session_id")
                if session_id:
                    runner_result["runner_session_id"] = session_id
                    runner_result["runner_state_dir"] = str(state_dir)

                requires_review = bool(
                    meta_data.get("requires_review")
                    or (meta_data.get("task_type") == "coding" and meta_data.get("requires_review") is not False)
                )
                if status == "completed" and requires_review and changed_files:
                    review_outcome = self._run_review_cycle(
                        task=task,
                        project_dir=project_dir,
                        changed_files=changed_files,
                        developer_summary=summary_text,
                        exec_dir=exec_dir,
                        role_file=role_file,
                        state_dir=state_dir,
                        child_env=child_env,
                        session_id=session_id,
                    )
                    if review_outcome.get("needs_user_input"):
                        status = "waiting_for_input"
                        summary_text = review_outcome.get("summary", summary_text)
                        runner_result["clarification"] = review_outcome.get("clarification")
                    else:
                        summary_text = review_outcome.get("summary", summary_text)
                        changed_files = review_outcome.get("changed_files", changed_files)

                if status == "waiting_for_input":
                    task_status = "waiting_for_input"
                    outcome_status = "waiting_for_input"
                elif (requires_review and changed_files) or (meta_data.get("task_type") == "coding" and changed_files):
                    task_status = "ready_to_commit"
                    outcome_status = "ready_to_commit"
                else:
                    task_status = "done"
                    outcome_status = "completed"
                task_artifacts_dir = self.artifact_root / str(task.id) / "artifacts"
                task_artifacts_dir.mkdir(parents=True, exist_ok=True)
                collected_artifacts, artifact_errors = _collect_outbox_artifacts(
                    outbox_dir,
                    task_artifacts_dir,
                    reserved_names=("summary.md", "session.jsonl"),
                )
                if artifact_errors:
                    runner_result["artifact_errors"] = [
                        _artifact_error_entry(
                            item.get("source", ""),
                            item.get("error", ""),
                        )
                        for item in artifact_errors
                    ]
                if summary_path and Path(summary_path).is_file():
                    sum_dest = task_artifacts_dir / "summary.md"
                    shutil.copy2(summary_path, sum_dest)
                    collected_artifacts.append({
                        "name": "summary.md",
                        "path": str(sum_dest),
                        "size": sum_dest.stat().st_size,
                    })
                if runner_result.get("session_path") and Path(runner_result["session_path"]).is_file():
                    sess_path = Path(runner_result["session_path"])
                    sess_dest = task_artifacts_dir / "session.jsonl"
                    shutil.copy2(sess_path, sess_dest)
                    collected_artifacts.append({
                        "name": "session.jsonl",
                        "path": str(sess_dest),
                        "size": sess_dest.stat().st_size,
                    })
                runner_result["artifacts"] = collected_artifacts

                # Cross-execution union: a fresh execution result replaces
                # the stored list wholesale (repository contract), which let
                # a "no code changes" round wipe a previously accumulated
                # changeset. Union with the stored list instead, filtered by
                # the workspace's own .gitignore verdict so ignored entries
                # (e.g. coverage caches) never enter the bookkeeping.
                changed_files = self._merge_changed_files(
                    previous=list((task.meta or {}).get("changed_files") or []),
                    incoming=changed_files,
                    project_dir=project_dir,
                )
                updated_task = self._repository.complete_execution(
                    task.id,
                    status=task_status,
                    summary=summary_text,
                    changed_files=changed_files,
                    provenance=runner_result,
                )
                outcome = TaskOutcome(
                    task_id=task.id,
                    status=outcome_status,
                    summary=summary_text,
                    changed_files=changed_files,
                    duration_ms=duration_ms,
                    worker_id=self.worker_id,
                )
                logger.info(
                    "Task %s execution finished with status=%s duration_ms=%.2f changed_files=%s",
                    task.id,
                    outcome_status,
                    duration_ms,
                    changed_files,
                    extra={"event": "task_finished", "workspace": ws_name, "outcome": outcome_status},
                )
                if self.on_task_finished:
                    try:
                        self.on_task_finished(updated_task, outcome)
                    except Exception as cb_err:
                        logger.error(
                            "on_task_finished callback raised for task %s: %s",
                            task.id,
                            cb_err,
                            exc_info=cb_err,
                        )
                return outcome

            except Exception as error:
                duration_ms = (time.perf_counter() - started) * 1000.0
                err_str = f"{type(error).__name__}: {error}"
                logger.error(
                    "Task %s execution failed: %s",
                    task.id,
                    err_str,
                    exc_info=error,
                    extra={"event": "task_failed", "workspace": ws_name},
                )
                try:
                    updated_task = self._repository.complete_execution(
                        task.id,
                        status="failed",
                        error=err_str,
                    )
                except Exception as save_err:
                    logger.critical(
                        "Failed to update task %s to failed status in DB: %s",
                        task.id,
                        save_err,
                        exc_info=save_err,
                    )
                    updated_task = task

                outcome = TaskOutcome(
                    task_id=task.id,
                    status="failed",
                    error=err_str,
                    duration_ms=duration_ms,
                    worker_id=self.worker_id,
                )
                if self.on_task_finished:
                    try:
                        self.on_task_finished(updated_task, outcome)
                    except Exception as cb_err:
                        logger.error(
                            "on_task_finished callback raised for failed task %s: %s",
                            task.id,
                            cb_err,
                            exc_info=cb_err,
                        )
                return outcome
            finally:
                # The execution-scoped credential lives exactly as long as
                # the child execution. Revoking it must not affect the
                # service's stable authentication mode.
                if execution_grant is not None and self.permissions is not None:
                    self.permissions.revoke(execution_grant.token)

    @staticmethod
    def _merge_changed_files(
        previous: list[str], incoming: list[str], project_dir: Path
    ) -> list[str]:
        """Ordered union of stored and fresh changed files, gitignore-filtered.

        The filter asks git itself (``check-ignore``) against the task
        workspace; anything the project ignores never enters the list. On
        any git failure the union is returned unfiltered — fail-open, the
        staging step re-checks anyway.
        """
        merged = list(dict.fromkeys([*previous, *incoming]))
        if not merged:
            return []
        try:
            proc = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                cwd=project_dir,
                input="\n".join(merged),
                text=True,
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0:
                ignored = set(proc.stdout.splitlines())
                merged = [path for path in merged if path not in ignored]
        except Exception as exc:
            logger.warning(
                "check-ignore failed for changed_files merge, keeping unfiltered: %s",
                exc,
            )
        return merged

    def _run_review_cycle(
        self,
        task: Task,
        project_dir: Path,
        changed_files: list[str],
        developer_summary: str,
        exec_dir: Path,
        role_file: Path,
        state_dir: Path,
        child_env: dict[str, str],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an independent, read-only review loop (up to 3 rounds).

        Review execution and validation are shared with the standalone
        ``review`` operation executor. A round that cannot produce an
        explicit valid verdict raises, so the task fails visibly instead of
        being approved by the absence of findings.

        ``session_id`` is the native Runner session of the original developer
        execution. When it is present, the fix round resumes that exact
        session (same state directory, role/runtime and workspace) instead of
        starting a fresh one. The injected ``runner_argv_prefix`` is used for
        both the review and the resume child process.
        """
        review_dir = exec_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        task_artifacts_dir = self.artifact_root / str(task.id)
        task_artifacts_dir.mkdir(parents=True, exist_ok=True)
        (task_artifacts_dir / "review").mkdir(parents=True, exist_ok=True)

        current_summary = developer_summary
        current_changed = list(changed_files)
        review_summary = ""

        # Hot-reload reviewer profile from file in knowledge base if present
        if self.knowledge_base is not None and hasattr(self.knowledge_base, "sync_agent_from_file"):
            try:
                self.knowledge_base.sync_agent_from_file("system-code-reviewer", self.database)
            except Exception as e:
                logger.debug("Could not sync reviewer profile from file: %s", e)

        rev_profile = self._agent_repository.get_current("system-code-reviewer")
        reviewer_system_prompt = rev_profile.system_prompt if rev_profile else ""
        # Pass the stored profile configuration through unchanged: the review
        # executor reads ``engine``/``effort`` from it to select the backend.
        reviewer_runner_config: dict[str, Any] = {}
        if rev_profile is not None and isinstance(rev_profile.runner_config, Mapping):
            reviewer_runner_config = dict(rev_profile.runner_config)

        executor = ReviewExecutor(
            runner_argv_prefix=self.runner_argv_prefix,
            runtime_defaults=self.runtime_defaults,
            knowledge_base=self.knowledge_base,
            env=child_env,
        )

        for round_num in range(1, 4):
            logger.info("Starting code review round %d for task %s", round_num, task.id)
            request = ReviewRequest(
                task_id=str(task.id),
                task_title=task.title,
                task_description=task.description,
                project_dir=Path(project_dir),
                changed_files=tuple(current_changed),
                developer_summary=current_summary,
                reviewer_system_prompt=reviewer_system_prompt,
                reviewer_runner_config=reviewer_runner_config,
                round_number=round_num,
                state_dir=review_dir / f"state_r{round_num}",
                artifact_dir=review_dir,
                task_artifact_dir=task_artifacts_dir,
            )
            try:
                result = executor.execute(request)
            except ReviewExecutionError:
                logger.error(
                    "Code review round %d for task %s produced no valid verdict; failing task",
                    round_num,
                    task.id,
                    exc_info=True,
                )
                raise

            review_summary = result.report_text
            logger.info("Saved review report for task %s to %s", task.id, result.report_path)

            if result.verdict == VERDICT_APPROVED:
                logger.info("Task %s code review round %d PASSED", task.id, round_num)
                full_summary = (
                    current_summary
                    + f"\n\n### 🔍 Code Review (Раунд {round_num}): ОДОБРЕНО ✅\n"
                    + review_summary[:1500]
                )
                return {"summary": full_summary, "changed_files": current_changed, "needs_user_input": False}

            # Not approved
            logger.warning("Task %s code review round %d requested changes", task.id, round_num)
            if round_num >= 3:
                # Exceeded round limit -> escalate to user
                return {
                    "needs_user_input": True,
                    "summary": current_summary,
                    "clarification": {
                        "title": f"Code Review не согласован после {round_num} раундов",
                        "description": (
                            f"Ревьювер вернул замечания на раунде {round_num}:\n\n"
                            f"{review_summary[:2000]}"
                        ),
                        "question": "Как поступить: принять замечания, завершить задачу как есть или дать указания разработчику?",
                    },
                }

            # Resume developer to address the reviewer's findings
            logger.info("Resuming developer for task %s with reviewer feedback (round %d)", task.id, round_num)
            dev_fix_task = exec_dir / f"dev_fix_task_r{round_num}.md"
            dev_fix_task.write_text(
                f"# Доработка по замечаниям Code Review (Раунд {round_num})\n\n"
                f"Ревьювер проверил твои изменения и вернул следующие замечания:\n\n"
                f"{review_summary}\n\n"
                "Пожалуйста, исправь указанные критические замечания, проверь код тестами и предоставь обновленный отчёт.",
                encoding="utf-8",
            )
            if session_id:
                # Resume the original developer session: the CLI requires the
                # session id and rejects a resume without it. The same state
                # directory, role/runtime and workspace are reused.
                dev_cmd = [
                    *self.runner_argv_prefix,
                    "resume",
                    "--state-dir",
                    str(state_dir),
                    "--session",
                    str(session_id),
                    "--agent",
                    str(role_file),
                    "--task",
                    str(dev_fix_task),
                ]
            else:
                # No resumable native session was reported, so the fix round
                # runs as a fresh execution rather than invoking an invalid
                # ``resume`` command.
                dev_cmd = [
                    *self.runner_argv_prefix,
                    "run",
                    "--agent",
                    str(role_file),
                    "--task",
                    str(dev_fix_task),
                ]
            dev_proc = subprocess.run(
                dev_cmd,
                capture_output=True,
                text=True,
                cwd=str(project_dir),
                env=child_env,
                check=False,
            )
            if dev_proc.returncode != 0:
                raise RuntimeError(
                    "developer fix run failed with exit code "
                    f"{dev_proc.returncode}; review round {round_num} cannot be re-verified"
                )
            try:
                dev_res = extract_runner_result(dev_proc.stdout)
                if dev_res is not None:
                    s_path = dev_res.get("summary_path")
                    if s_path and Path(s_path).is_file():
                        current_summary = Path(s_path).read_text(encoding="utf-8")
                    if dev_res.get("changed_files"):
                        new_changed = list(dev_res["changed_files"])
                        current_changed = list(
                            dict.fromkeys(current_changed + new_changed)
                        )
            except Exception:
                pass

        return {
            "summary": current_summary + "\n\n### 🔍 Code Review: Завершено с замечаниями\n" + review_summary[:1000],
            "changed_files": current_changed,
            "needs_user_input": False,
        }

    def start(self) -> None:
        """Start the background execution loop."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self.run_loop,
                args=(self._stop_event,),
                name=f"task-execution-worker-{self.worker_id}",
                daemon=True,
            )
            logger.info("Starting background TaskExecutionWorker thread: %s", self._thread.name)
            self._thread.start()

    @property
    def is_running(self) -> bool:
        """Return whether the background loop thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def thread(self) -> threading.Thread | None:
        """The background loop thread, or None before :meth:`start`."""
        return self._thread

    def request_stop(self) -> None:
        """Signal the loop to stop without waiting for the active task."""
        event = self._stop_event
        if event is not None:
            event.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the loop thread to finish, up to ``timeout`` seconds."""
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)

    def stop(self, timeout: float | None = None) -> None:
        """Gracefully stop the worker after current task completes."""
        self.request_stop()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            logger.info("Stopping background TaskExecutionWorker thread: %s", thread.name)
        self.join(timeout)

    def run_loop(self, stop_event: threading.Event | None = None) -> None:
        """Poll and execute in_progress tasks until stopped."""
        event = stop_event or self._stop_event or threading.Event()
        logger.info("TaskExecutionWorker run_loop started: worker_id=%s", self.worker_id)
        while not event.is_set():
            try:
                outcome = self.step()
            except Exception as loop_error:
                logger.exception(
                    "Unexpected task worker loop error (caught and continuing): %s",
                    loop_error,
                )
                outcome = None

            if outcome is None:
                event.wait(self.poll_interval_seconds)
        logger.info("TaskExecutionWorker run_loop finished: worker_id=%s", self.worker_id)


class TaskExecutionWorkerPool:
    """Run several :class:`TaskExecutionWorker` loops concurrently.

    Each worker owns an independent loop, stop event and worker id. Tasks in
    different workspaces can therefore execute in parallel, while
    :meth:`TaskRepository.claim_in_progress` keeps tasks from the same
    workspace strictly sequential and prevents duplicate claims. Tasks without
    a workspace form one serialized group because they resolve to the same
    default/project directory.

    Args:
        concurrency: Number of worker threads, from 1 to
            :data:`MAX_TASK_CONCURRENCY`.
        worker_id: Stable pool prefix; every thread gets
            ``<worker_id>-<index>``.
        **worker_kwargs: Keyword arguments forwarded unchanged to every
            :class:`TaskExecutionWorker`.
    """

    def __init__(
        self,
        database: Database,
        artifact_root: str | os.PathLike[str],
        *,
        concurrency: int = 3,
        worker_id: str | None = None,
        **worker_kwargs: Any,
    ) -> None:
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise ValueError("concurrency must be an integer")
        if concurrency < 1 or concurrency > MAX_TASK_CONCURRENCY:
            raise ValueError(
                f"concurrency must be between 1 and {MAX_TASK_CONCURRENCY}"
            )

        self.concurrency = concurrency
        self.worker_id = worker_id or f"task-worker-{uuid.uuid4().hex[:8]}"
        self._workers = tuple(
            TaskExecutionWorker(
                database,
                artifact_root,
                worker_id=f"{self.worker_id}-{index}",
                **worker_kwargs,
            )
            for index in range(concurrency)
        )
        self._lifecycle_lock = threading.Lock()
        self._state = "stopped"

    @property
    def workers(self) -> tuple[TaskExecutionWorker, ...]:
        """Workers owned by the pool, in index order."""
        return self._workers

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        """Started worker threads, in index order."""
        return tuple(
            worker.thread
            for worker in self._workers
            if worker.thread is not None
        )

    @property
    def is_running(self) -> bool:
        """Return whether every worker loop is alive."""
        return all(worker.is_running for worker in self._workers)

    @property
    def state(self) -> str:
        """Current lifecycle state: ``stopped``, ``starting``, ``running`` or
        ``stopping``."""
        return self._state

    def start(self) -> None:
        """Start every worker loop; an already running pool is a no-op."""
        with self._lifecycle_lock:
            if self._state == "running":
                if all(worker.is_running for worker in self._workers):
                    return
                logger.warning(
                    "TaskExecutionWorkerPool %s is partially running; "
                    "start() is ignored while any worker thread is alive",
                    self.worker_id,
                )
                return
            if self._state == "starting":
                logger.warning(
                    "TaskExecutionWorkerPool %s is starting; start() is ignored",
                    self.worker_id,
                )
                return
            if self._state == "stopping":
                if any(worker.is_running for worker in self._workers):
                    logger.warning(
                        "TaskExecutionWorkerPool %s is still stopping; "
                        "start() is ignored",
                        self.worker_id,
                    )
                    return
                # A previous timed-out stop has finished; the pool may start
                # again now that every worker thread has exited.
                self._state = "stopped"
            if any(worker.is_running for worker in self._workers):
                logger.warning(
                    "TaskExecutionWorkerPool %s still has worker threads "
                    "finishing a previous stop(); start() is ignored",
                    self.worker_id,
                )
                return

            self._state = "starting"
            logger.info(
                "Starting TaskExecutionWorkerPool: worker_id=%s concurrency=%d",
                self.worker_id,
                self.concurrency,
            )
            try:
                for worker in self._workers:
                    worker.start()
            except Exception:
                for worker in self._workers:
                    worker.request_stop()
                self._state = "stopped"
                raise

            if all(worker.is_running for worker in self._workers):
                self._state = "running"
            else:
                self._state = "stopped"
                logger.warning(
                    "TaskExecutionWorkerPool %s did not start every worker",
                    self.worker_id,
                )

    def stop(self, timeout: float | None = None) -> None:
        """Stop all workers and wait for active executions up to ``timeout``.

        Stop is requested for every worker first, so idle workers exit
        immediately while busy workers finish their current task. The whole
        pool shares one timeout budget instead of waiting ``timeout`` per
        worker. When the budget expires, stop returns but the remaining worker
        threads keep their stop request and exit after the active task.
        """
        with self._lifecycle_lock:
            self._state = "stopping"
            for worker in self._workers:
                worker.request_stop()
            deadline = (
                None if timeout is None else time.monotonic() + max(0.0, timeout)
            )
            for worker in self._workers:
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                worker.join(remaining)

            alive = [
                worker for worker in self._workers if worker.is_running
            ]
            self._state = "stopping" if alive else "stopped"
            if alive:
                logger.warning(
                    "TaskExecutionWorkerPool %s stopped with %d worker "
                    "thread(s) still finishing active tasks",
                    self.worker_id,
                    len(alive),
                )
            else:
                logger.info(
                    "TaskExecutionWorkerPool stopped: worker_id=%s",
                    self.worker_id,
                )
