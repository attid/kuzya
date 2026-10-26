"""Bounded, redacted diagnostic trace from a task's own Runner artifacts.

The analysis evidence package already contains the task, its selected profile,
operations and metrics. This module adds only facts observed in the target
task's ``session.jsonl`` and ``events.jsonl`` artifacts:

* tool invocation names, statuses, durations and redacted arguments;
* repeated path references and aggregate operation durations;
* error excerpts (bounded and redacted), timestamps and usage counters;
* an explicit availability status for each artifact source.

Isolation is enforced before any file is read. A provenance path must resolve
inside one of the caller-supplied artifact roots *and* inside a directory that
belongs to the target task. Symlinks are resolved first, so a link cannot
escape either boundary. Files are read with byte, record and output limits.

Nothing here returns a raw transcript: assistant text, file contents and tool
outputs are never copied into the trace. Only the bounded observations above
are retained.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SESSION_FILENAME = "session.jsonl"
EVENTS_FILENAME = "events.jsonl"
TRACE_KINDS = (SESSION_FILENAME, EVENTS_FILENAME)

STATUS_AVAILABLE = "available"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
SOURCE_AVAILABLE = "available"
SOURCE_MISSING = "missing"
SOURCE_UNREADABLE = "unreadable"
SOURCE_EMPTY = "empty"
SOURCE_MALFORMED = "malformed"
SOURCE_OUTSIDE_SCOPE = "outside_scope"
SOURCE_SKIPPED_LIMIT = "skipped_limit"

#: Hard bounds for one trace. They are deliberately small enough that evidence
#: carries diagnostic facts, not a second copy of the session.
MAX_ARTIFACT_FILES = 32
MAX_DIRECTORIES_PER_TASK = 2_000
MAX_BYTES_PER_ARTIFACT = 1_500_000
MAX_TOTAL_BYTES = 4_000_000
MAX_RECORDS_PER_ARTIFACT = 5_000
MAX_TOOL_CALLS = 100
MAX_ERRORS = 30
MAX_PATHS = 100
MAX_SESSION_IDS = 10
MAX_ARGUMENT_CHARS = 400
MAX_ERROR_CHARS = 500
MAX_PATH_CHARS = 300
MAX_STRING_CHARS = 2_000

_REDACTED = "[REDACTED]"
_REDACTED_PRIVATE_KEY = "[REDACTED PRIVATE KEY]"

_SECRET_KEY_RE = re.compile(
    r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|apikey|auth(?:orization)?|"
    r"credential|private[_-]?key|access[_-]?key|session[_-]?key|cookie|bearer|"
    r"client[_-]?secret|refresh[_-]?token|signature)",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@"
)
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(authorization|bearer|token|api[_-]?key|secret|password|passwd|"
    r"pwd|credential|private[_-]?key|client[_-]?secret|refresh[_-]?token)"
    r"\b(\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:password|passwd|token|api-key|api_key|secret|auth|"
    r"credential|private-key)\b(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk|pk|ghp|gho|ghs|ghu|xox[baprs]|AKIA)[-_A-Za-z0-9]{8,}\b"
)
_ENV_SECRET_RE = re.compile(
    r"(?i)\b([A-Z_][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL)"
    r"[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s'\";]+)"
)
_PATH_VALUE_RE = re.compile(r"(?:^|[\s='\"])((?:/|\.{1,2}/)[A-Za-z0-9_./@+~-]+)")
_CONTENT_KEY_RE = re.compile(
    r"(?:content|contents|body|data|text|old_?text|new_?text|old_?string|"
    r"new_?string|patch|diff|file_?content|code|script)",
    re.IGNORECASE,
)
_PATH_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "filepath",
        "file_path",
        "filename",
        "directory",
        "dir",
        "cwd",
        "workdir",
        "working_directory",
        "target",
        "target_path",
        "source",
        "destination",
        "dest",
    }
)


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def redact_text(value: Any) -> str:
    """Return ``value`` as bounded text with recognizable secrets removed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    text = value.replace("\x00", "")
    text = _PRIVATE_KEY_RE.sub(_REDACTED_PRIVATE_KEY, text)
    text = _URL_CREDENTIAL_RE.sub(lambda match: match.group("scheme") + _REDACTED + "@", text)
    text = _INLINE_SECRET_RE.sub(
        lambda match: match.group(1) + match.group(2) + _REDACTED, text
    )
    text = _CLI_SECRET_RE.sub(
        lambda match: match.group(1) + _REDACTED, text
    )
    text = _BEARER_RE.sub("Bearer " + _REDACTED, text)
    text = _KNOWN_TOKEN_RE.sub(_REDACTED, text)
    text = _ENV_SECRET_RE.sub(
        lambda match: match.group(1) + "=" + _REDACTED, text
    )
    return _clip(text, MAX_STRING_CHARS)


def _omitted_value(value: Any) -> str:
    """Describe an omitted content-bearing argument without copying it."""
    if isinstance(value, str):
        return f"[omitted: {len(value)} chars]"
    if isinstance(value, (list, tuple)):
        return f"[omitted: {len(value)} items]"
    if isinstance(value, Mapping):
        return f"[omitted: {len(value)} fields]"
    return "[omitted]"


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact a tool-argument value without retaining raw blobs."""
    if depth > 4:
        return "…[depth limit]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                result["…"] = "[items omitted]"
                break
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                result[name] = _REDACTED
            elif _CONTENT_KEY_RE.search(name):
                result[name] = _omitted_value(item)
            else:
                result[name] = _redact_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = list(value)
        result = [
            _redact_value(item, depth=depth + 1)
            for item in items[:25]
        ]
        if len(items) > 25:
            result.append("[items omitted]")
        return result
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def _arguments_for_trace(arguments: Any) -> Any:
    redacted = _redact_value(arguments)
    serialized = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    if len(serialized) <= MAX_ARGUMENT_CHARS:
        return redacted
    return {
        "truncated": True,
        "preview": _clip(serialized, MAX_ARGUMENT_CHARS),
    }


def _normalize_path_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().strip("'\"")
    if not value or "\n" in value or "\x00" in value or "://" in value:
        return None
    if len(value) > MAX_PATH_CHARS:
        value = value[:MAX_PATH_CHARS]
    return value


def _paths_from_arguments(arguments: Any) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        normalized = _normalize_path_value(value)
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)

    def visit(value: Any, *, key: str | None = None, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(value, Mapping):
            for name, item in value.items():
                name_text = str(name)
                if name_text.casefold() in _PATH_KEYS:
                    if isinstance(item, (list, tuple)):
                        for entry in item[:20]:
                            add(entry)
                    else:
                        add(item)
                visit(item, key=name_text, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:20]:
                visit(item, key=key, depth=depth + 1)
        elif isinstance(value, str) and key is not None:
            if key.casefold() in {"command", "cmd"}:
                for match in _PATH_VALUE_RE.finditer(value):
                    add(match.group(1))

    visit(arguments)
    return paths[:20]


def _first_value(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value[:64] if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _text_from_message(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") in {"text", "error"} and isinstance(
                    block.get("text"), str
                ):
                    parts.append(block["text"])
                elif isinstance(block.get("error"), str):
                    parts.append(block["error"])
        return "\n".join(parts)
    if isinstance(content, Mapping) and isinstance(content.get("text"), str):
        return content["text"]
    error = message.get("errorMessage") or message.get("error")
    return error if isinstance(error, str) else ""


def _message_usage(message: Mapping[str, Any], usage: dict[str, float | int]) -> None:
    raw = message.get("usage")
    if not isinstance(raw, Mapping):
        return
    for name, value in raw.items():
        number = _number(value)
        if number is not None and isinstance(name, str):
            usage[name] = usage.get(name, 0) + number


class _Call:
    """One tool invocation merged across session and event records."""

    __slots__ = (
        "key",
        "order",
        "name",
        "arguments",
        "status",
        "is_error",
        "duration_ms",
        "started_at",
        "ended_at",
        "error",
        "paths",
        "parent_id",
        "sources",
    )

    def __init__(self, key: str, order: int) -> None:
        self.key = key
        self.order = order
        self.name: str | None = None
        self.arguments: Any = None
        self.status = "unfinished"
        self.is_error: bool | None = None
        self.duration_ms: float | int | None = None
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.error: str | None = None
        self.paths: list[str] = []
        self.parent_id: str | None = None
        self.sources: set[str] = set()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name or "unknown",
            "status": self.status,
            "sources": sorted(self.sources),
        }
        if self.started_at:
            result["started_at"] = self.started_at
        if self.ended_at:
            result["ended_at"] = self.ended_at
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        if self.arguments is not None:
            result["arguments"] = self.arguments
        if self.paths:
            result["paths"] = list(self.paths)
        if self.error:
            result["error"] = self.error
        if self.parent_id:
            result["parent_tool_call_id"] = self.parent_id
        return result


class _TraceCollector:
    """Accumulate bounded observations from already validated artifacts."""

    def __init__(self) -> None:
        self.calls: dict[str, _Call] = {}
        self.call_counter = 0
        self.session_entry_types: Counter[str] = Counter()
        self.event_types: Counter[str] = Counter()
        self.message_roles: Counter[str] = Counter()
        self.usage: dict[str, float | int] = {}
        self.session_ids: list[str] = []
        self.errors: list[dict[str, Any]] = []
        self.path_counts: dict[str, dict[str, Any]] = {}
        self.timestamps: dict[str, list[str]] = {
            "session": [],
            "events": [],
        }
        self.malformed_lines = 0

    def _call(self, source: str, tool_call_id: Any) -> _Call:
        identifier = str(tool_call_id) if tool_call_id else None
        key = identifier or f"anonymous:{source}:{self.call_counter}"
        self.call_counter += 1
        call = self.calls.get(key)
        if call is None:
            call = _Call(key, len(self.calls))
            self.calls[key] = call
        call.sources.add(source)
        return call

    def _observe_timestamp(self, source: str, value: Any) -> str | None:
        observation = _timestamp(value)
        if observation is not None:
            self.timestamps[source].append(observation)
        return observation

    def _add_paths(self, call: _Call, arguments: Any, timestamp: str | None) -> None:
        for path in _paths_from_arguments(arguments):
            if path not in call.paths and len(call.paths) < 20:
                call.paths.append(path)
            entry = self.path_counts.setdefault(
                path, {"path": path, "count": 0, "tools": set(), "last_timestamp": None}
            )
            entry["count"] += 1
            if call.name:
                entry["tools"].add(call.name)
            if timestamp:
                entry["last_timestamp"] = timestamp

    def record_tool_start(
        self,
        source: str,
        payload: Mapping[str, Any],
        *,
        timestamp: str | None = None,
    ) -> None:
        call = self._call(source, _first_value(payload, "toolCallId", "id", "tool_call_id"))
        name = _first_value(payload, "toolName", "name")
        if isinstance(name, str):
            call.name = _clip(redact_text(name), 120)
        arguments = _first_value(payload, "args", "arguments", "input")
        if arguments is not None:
            redacted_arguments = _redact_value(arguments)
            call.arguments = _arguments_for_trace(redacted_arguments)
            self._add_paths(call, redacted_arguments, timestamp)
        parent = _first_value(payload, "parentToolCallId", "parent_tool_call_id")
        if parent:
            call.parent_id = _clip(redact_text(parent), 120)
        if timestamp:
            call.started_at = timestamp
        call.status = "running"

    def record_tool_end(
        self,
        source: str,
        payload: Mapping[str, Any],
        *,
        timestamp: str | None = None,
    ) -> None:
        call = self._call(source, _first_value(payload, "toolCallId", "id", "tool_call_id"))
        name = _first_value(payload, "toolName", "name")
        if isinstance(name, str):
            call.name = _clip(redact_text(name), 120)
        is_error = payload.get("isError")
        if isinstance(is_error, bool):
            call.is_error = is_error
            call.status = "error" if is_error else "ok"
        else:
            call.status = "completed"
        duration = _number(_first_value(payload, "durationMs", "duration_ms"))
        if duration is not None:
            call.duration_ms = duration
        if timestamp:
            call.ended_at = timestamp
        if call.is_error:
            error_timestamp = timestamp or call.started_at or call.ended_at
            result = _first_value(payload, "result", "error", "output")
            if isinstance(result, Mapping):
                message = _text_from_message(result)
            elif isinstance(result, list):
                message = _text_from_message({"content": result})
            elif isinstance(result, str):
                message = result
            else:
                message = ""
            if not message:
                message = _text_from_message(payload)
            if message:
                call.error = _clip(redact_text(message), MAX_ERROR_CHARS)
                self.record_error(
                    source,
                    message,
                    timestamp=error_timestamp,
                    tool=call.name,
                    tool_call_id=str(payload.get("toolCallId") or ""),
                )

    def record_error(
        self,
        source: str,
        message: Any,
        *,
        timestamp: str | None = None,
        tool: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        text = _clip(redact_text(message), MAX_ERROR_CHARS).strip()
        if not text:
            return
        self.errors.append(
            {
                "source": source,
                "message": text,
                "timestamp": timestamp,
                "tool": tool,
                "tool_call_id": tool_call_id or None,
            }
        )

    def record_session_entry(self, entry: Mapping[str, Any], source: str = "session") -> None:
        entry_type = str(entry.get("type") or "unknown")
        self.session_entry_types[entry_type] += 1
        timestamp = self._observe_timestamp(source, entry.get("timestamp"))
        if entry_type == "session":
            identifier = entry.get("id")
            if isinstance(identifier, str):
                self._add_session_id(identifier)
            return
        message = entry.get("message")
        if not isinstance(message, Mapping):
            return
        role = str(message.get("role") or "unknown")
        self.message_roles[role] += 1
        _message_usage(message, self.usage)
        message_timestamp = self._observe_timestamp(
            source, message.get("timestamp")
        ) or timestamp
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type in {"toolCall", "tool_call"}:
                    self.record_tool_start(
                        source,
                        {
                            "toolCallId": block.get("id"),
                            "toolName": block.get("name") or block.get("toolName"),
                            "args": (
                                block.get("arguments")
                                or block.get("args")
                                or block.get("input")
                            ),
                        },
                        timestamp=message_timestamp,
                    )
        if role == "toolResult":
            payload = {
                "toolCallId": message.get("toolCallId"),
                "toolName": message.get("toolName"),
                "isError": message.get("isError"),
                "result": message.get("content"),
                "error": message.get("errorMessage") or message.get("error"),
            }
            self.record_tool_end(source, payload, timestamp=message_timestamp)
        stop_reason = message.get("stopReason")
        if stop_reason in {"error", "aborted"}:
            self.record_error(
                source,
                message.get("errorMessage") or f"assistant stop reason: {stop_reason}",
                timestamp=message_timestamp,
            )

    def record_event(self, event: Mapping[str, Any], source: str = "events") -> None:
        event_type = str(event.get("type") or "unknown")
        self.event_types[event_type] += 1
        timestamp = self._observe_timestamp(source, _first_value(event, "timestamp", "time"))
        if event_type == "session":
            identifier = event.get("id")
            if isinstance(identifier, str):
                self._add_session_id(identifier)
            return
        if event_type in {
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }:
            if event_type == "tool_execution_end":
                self.record_tool_end(source, event, timestamp=timestamp)
            else:
                self.record_tool_start(source, event, timestamp=timestamp)
            return
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, Mapping):
                role = str(message.get("role") or "unknown")
                self.message_roles[role] += 1
                _message_usage(message, self.usage)
                stop_reason = message.get("stopReason")
                if stop_reason in {"error", "aborted"}:
                    self.record_error(
                        source,
                        message.get("errorMessage") or f"assistant stop reason: {stop_reason}",
                        timestamp=timestamp,
                    )
            return
        if event_type in {"error", "runner_error"} or event.get("isError") is True:
            raw_error = _first_value(event, "error", "errorMessage", "message")
            if isinstance(raw_error, Mapping):
                raw_error = _text_from_message(raw_error)
            if not isinstance(raw_error, str) or not raw_error.strip():
                raw_error = f"{event_type} event reported an error"
            self.record_error(source, raw_error, timestamp=timestamp)

    def _add_session_id(self, identifier: str) -> None:
        value = _clip(redact_text(identifier), 120)
        if value not in self.session_ids and len(self.session_ids) < MAX_SESSION_IDS:
            self.session_ids.append(value)

    def _operations(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for call in self.calls.values():
            name = call.name or "unknown"
            entry = grouped.setdefault(
                name,
                {
                    "name": name,
                    "count": 0,
                    "error_count": 0,
                    "duration_ms_total": 0,
                    "duration_ms_max": None,
                },
            )
            entry["count"] += 1
            if call.status == "error":
                entry["error_count"] += 1
            if call.duration_ms is not None:
                entry["duration_ms_total"] += call.duration_ms
                current_max = entry["duration_ms_max"]
                if current_max is None or call.duration_ms > current_max:
                    entry["duration_ms_max"] = call.duration_ms
        return [
            grouped[name]
            for name in sorted(grouped, key=lambda item: (-grouped[item]["count"], item))
        ]

    def _paths(self) -> list[dict[str, Any]]:
        entries = []
        for entry in self.path_counts.values():
            entries.append(
                {
                    "path": entry["path"],
                    "count": entry["count"],
                    "tools": sorted(entry["tools"]),
                    "last_timestamp": entry["last_timestamp"],
                    "repeated": entry["count"] > 1,
                }
            )
        entries.sort(key=lambda item: (-item["count"], item["path"]))
        return entries

    def finalize(self) -> dict[str, Any]:
        calls = sorted(self.calls.values(), key=lambda call: call.order)
        omitted_calls = max(0, len(calls) - MAX_TOOL_CALLS)
        errors = self.errors[-MAX_ERRORS:] if len(self.errors) > MAX_ERRORS else self.errors
        omitted_errors = max(0, len(self.errors) - len(errors))
        paths = self._paths()
        omitted_paths = max(0, len(paths) - MAX_PATHS)

        def observed(source: str) -> list[str]:
            return sorted(self.timestamps[source])

        session_timestamps = observed("session")
        event_timestamps = observed("events")
        all_timestamps = session_timestamps + event_timestamps
        return {
            "session_ids": list(self.session_ids),
            "session": {
                "entry_types": dict(sorted(self.session_entry_types.items())),
                "message_counts": dict(sorted(self.message_roles.items())),
                "first_timestamp": session_timestamps[0] if session_timestamps else None,
                "last_timestamp": session_timestamps[-1] if session_timestamps else None,
            },
            "events": {
                "event_types": dict(sorted(self.event_types.items())),
                "first_timestamp": event_timestamps[0] if event_timestamps else None,
                "last_timestamp": event_timestamps[-1] if event_timestamps else None,
            },
            "timestamps": {
                "first_observed": min(all_timestamps) if all_timestamps else None,
                "last_observed": max(all_timestamps) if all_timestamps else None,
            },
            "usage": dict(sorted(self.usage.items())),
            "tool_calls": [call.to_dict() for call in calls[:MAX_TOOL_CALLS]],
            "operations": self._operations(),
            "paths": paths[:MAX_PATHS],
            "errors": errors,
            "omitted": {
                "tool_calls": omitted_calls,
                "errors": omitted_errors,
                "paths": omitted_paths,
            },
        }


def _normalized_roots(roots: Iterable[Path | str]) -> list[Path]:
    normalized: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            candidate = Path(root).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_task_dir_name(name: str, task_id: str) -> bool:
    if not task_id or "/" in task_id or "\\" in task_id or task_id in {".", ".."}:
        return False
    return name == task_id or name == f"task_{task_id}" or name.startswith(
        f"task_{task_id}_"
    )


def _inside_target_task(path: Path, roots: Sequence[Path], task_id: str) -> bool:
    if not any(_within(path, root) for root in roots):
        return False
    return any(_is_task_dir_name(parent.name, task_id) for parent in path.parents)


def _relative_path(path: Path, roots: Sequence[Path]) -> str:
    for root in roots:
        if _within(path, root):
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                break
    return path.name


def _classify_provenance_path(
    raw: Any,
    roots: Sequence[Path],
    task_id: str,
) -> tuple[Path | None, str]:
    if not isinstance(raw, str) or not raw.strip():
        return None, "not_recorded"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None, "relative_path_rejected"
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None, "unresolvable_path"
    if not any(_within(resolved, root) for root in roots):
        return None, "outside_allowed_roots"
    if not _inside_target_task(resolved, roots, task_id):
        return None, "outside_target_task_scope"
    if resolved.name not in TRACE_KINDS:
        return None, "unexpected_artifact_name"
    return resolved, "candidate"


def _provenance_paths(
    meta: Mapping[str, Any], roots: Sequence[Path], task_id: str
) -> tuple[dict[str, list[tuple[Path, str]]], dict[str, list[str]]]:
    provenance = meta.get("provenance")
    source = provenance if isinstance(provenance, Mapping) else {}
    raw_paths = {
        SESSION_FILENAME: _first_value(source, "session_path")
        or _first_value(meta, "session_path"),
        EVENTS_FILENAME: _first_value(source, "events_path")
        or _first_value(meta, "events_path"),
    }
    # A historical result may only record the state directory and session id.
    state_dir = _first_value(source, "runner_state_dir") or _first_value(
        meta, "runner_state_dir"
    )
    session_id = _first_value(source, "runner_session_id") or _first_value(
        source, "session_id"
    ) or _first_value(meta, "runner_session_id")
    if state_dir and session_id:
        # Only use the derived location when it exists. A resumed session may
        # have its events in attempt 2; discovery below finds that file instead
        # of reporting a non-existent attempt 1 as a degraded source.
        derived_session = (
            Path(str(state_dir))
            / "sessions"
            / str(session_id)
            / "pi"
            / SESSION_FILENAME
        )
        derived_events = (
            Path(str(state_dir))
            / "sessions"
            / str(session_id)
            / "attempts"
            / "1"
            / EVENTS_FILENAME
        )
        if not raw_paths[SESSION_FILENAME] and derived_session.is_file():
            raw_paths[SESSION_FILENAME] = str(derived_session)
        if not raw_paths[EVENTS_FILENAME] and derived_events.is_file():
            raw_paths[EVENTS_FILENAME] = str(derived_events)
    accepted: dict[str, list[tuple[Path, str]]] = {
        SESSION_FILENAME: [],
        EVENTS_FILENAME: [],
    }
    rejected: dict[str, list[str]] = {SESSION_FILENAME: [], EVENTS_FILENAME: []}
    for kind, raw in raw_paths.items():
        if raw is None:
            continue
        resolved, status = _classify_provenance_path(raw, roots, task_id)
        if resolved is not None:
            accepted[kind].append((resolved, "provenance"))
        elif status != "not_recorded":
            rejected[kind].append(status)
    return accepted, rejected


def _discover_task_artifacts(
    roots: Sequence[Path], task_id: str
) -> tuple[dict[str, list[Path]], bool]:
    task_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    for root in roots:
        candidates: list[Path] = []
        if _is_task_dir_name(root.name, task_id):
            candidates.append(root)
        if root.is_dir():
            try:
                for entry in sorted(root.iterdir(), key=lambda item: item.name):
                    if (
                        entry.is_dir()
                        and not entry.is_symlink()
                        and _is_task_dir_name(entry.name, task_id)
                    ):
                        candidates.append(entry)
            except OSError:
                continue
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved not in seen_dirs and _inside_target_task(
                resolved / "_", roots, task_id
            ):
                seen_dirs.add(resolved)
                task_dirs.append(resolved)

    discovered: dict[str, list[Path]] = {
        SESSION_FILENAME: [],
        EVENTS_FILENAME: [],
    }
    seen_files: dict[str, set[Path]] = {
        SESSION_FILENAME: set(),
        EVENTS_FILENAME: set(),
    }
    limit_reached = False
    visited_directories = 0
    for task_dir in sorted(task_dirs):
        for dirpath, dirnames, filenames in os.walk(task_dir, followlinks=False):
            visited_directories += 1
            if visited_directories > MAX_DIRECTORIES_PER_TASK:
                limit_reached = True
                break
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not (Path(dirpath) / name).is_symlink()
            )
            for name in sorted(filenames):
                if name not in TRACE_KINDS:
                    continue
                candidate = Path(dirpath) / name
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError):
                    continue
                if not _inside_target_task(resolved, roots, task_id):
                    continue
                if resolved in seen_files[name]:
                    continue
                if not resolved.is_file():
                    continue
                seen_files[name].add(resolved)
                if len(discovered[name]) >= MAX_ARTIFACT_FILES:
                    limit_reached = True
                    continue
                discovered[name].append(resolved)
        if visited_directories > MAX_DIRECTORIES_PER_TASK:
            break
    for kind in TRACE_KINDS:
        discovered[kind].sort(key=str)
    return discovered, limit_reached


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    """Content identity of one trace source: size plus mtime.

    ``shutil.copy2`` preserves the mtime, so an artifact copy of a session
    journal and its original share the fingerprint, while a journal that
    kept growing after the copy was taken does not. ``None`` when the file
    cannot be stat'ed — such sources are never deduplicated away.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def _read_records(
    path: Path,
    *,
    max_bytes: int,
    tail: bool,
) -> tuple[list[Mapping[str, Any]], dict[str, Any], str | None]:
    """Read at most ``max_bytes`` and return parsed JSON objects plus metadata."""
    if not path.exists():
        return [], {
            "status": SOURCE_MISSING,
            "reason": "file does not exist",
            "bytes_read": 0,
            "truncated": False,
            "records": 0,
            "malformed_lines": 0,
        }, None
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            truncated = size > max_bytes
            if tail and truncated:
                stream.seek(size - max_bytes)
                data = stream.read(max_bytes)
                if b"\n" in data:
                    data = data.split(b"\n", 1)[1]
            else:
                data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    data = data[:max_bytes]
                    truncated = True
    except OSError as error:
        return [], {
            "status": SOURCE_UNREADABLE,
            "reason": type(error).__name__,
            "bytes_read": 0,
            "truncated": False,
            "records": 0,
            "malformed_lines": 0,
        }, None

    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if truncated and lines and not text.endswith(("\n", "\r")):
        # The final line is a byte-boundary fragment, not an observation.
        lines = lines[:-1]
    omitted_records = 0
    if len(lines) > MAX_RECORDS_PER_ARTIFACT:
        omitted_records = len(lines) - MAX_RECORDS_PER_ARTIFACT
        lines = (
            lines[-MAX_RECORDS_PER_ARTIFACT:]
            if tail
            else lines[:MAX_RECORDS_PER_ARTIFACT]
        )
        truncated = True
    records: list[Mapping[str, Any]] = []
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(value, Mapping):
            malformed += 1
            continue
        records.append(value)
    if not records:
        status = SOURCE_MALFORMED if malformed else SOURCE_EMPTY
    else:
        status = SOURCE_AVAILABLE
    return records, {
        "status": status,
        "reason": None,
        "bytes_read": len(data),
        "truncated": truncated,
        "records": len(records),
        "malformed_lines": malformed,
        "omitted_records": omitted_records,
        "window": "tail" if tail else "head",
    }, None


def _source_entry(
    kind: str,
    path: Path,
    roots: Sequence[Path],
    metadata: Mapping[str, Any],
    origin: str,
) -> dict[str, Any]:
    entry = {
        "kind": kind,
        "path": _relative_path(path, roots),
        "origin": origin,
    }
    entry.update(metadata)
    return entry


def _availability(
    kind: str,
    sources: Sequence[Mapping[str, Any]],
    rejected: Sequence[str],
    *,
    discovery_limit: bool,
) -> dict[str, Any]:
    available = [
        source for source in sources if source.get("status") == SOURCE_AVAILABLE
    ]
    if available:
        degraded = [
            source
            for source in sources
            if source.get("status") != SOURCE_AVAILABLE
            or source.get("truncated")
            or source.get("malformed_lines")
        ]
        result: dict[str, Any] = {
            "status": SOURCE_AVAILABLE,
            "files": len(available),
        }
        if degraded:
            result["degraded_files"] = len(degraded)
        return result
    if rejected:
        return {
            "status": SOURCE_OUTSIDE_SCOPE,
            "reason": (
                "provenance path is outside the target task's artifact roots; "
                "no file was read"
            ),
            "rejected": sorted(set(rejected)),
        }
    if sources:
        status = str(sources[0].get("status") or SOURCE_MISSING)
        result = {"status": status, "files": 0}
        if sources[0].get("reason"):
            result["reason"] = sources[0]["reason"]
        return result
    if discovery_limit:
        return {
            "status": SOURCE_SKIPPED_LIMIT,
            "reason": "artifact discovery limit reached before this kind was found",
            "files": 0,
        }
    return {
        "status": SOURCE_MISSING,
        "reason": (
            f"no {kind} artifact was found for the target task; "
            "no observations were inferred"
        ),
        "files": 0,
    }


def build_task_trace(
    task: Any,
    artifact_roots: Sequence[Path | str],
) -> dict[str, Any]:
    """Build a bounded diagnostic trace for ``task`` from its own artifacts.

    Args:
        task: A task-like object exposing ``id`` and ``meta``.
        artifact_roots: Directories that may contain task artifacts. Provenance
            paths and discoveries are accepted only when they resolve inside
            one of these roots and inside a directory belonging to this task.

    Returns:
        A JSON-serializable trace. ``status`` is ``available``, ``partial`` or
        ``unavailable``. Missing artifacts are reported as such; the function
        never invents observations.
    """
    task_id = str(getattr(task, "id", "") or "")
    meta = getattr(task, "meta", None)
    meta = dict(meta) if isinstance(meta, Mapping) else {}
    roots = _normalized_roots(artifact_roots)
    limits = {
        "max_artifact_files": MAX_ARTIFACT_FILES,
        "max_bytes_per_artifact": MAX_BYTES_PER_ARTIFACT,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_records_per_artifact": MAX_RECORDS_PER_ARTIFACT,
        "max_tool_calls": MAX_TOOL_CALLS,
        "max_errors": MAX_ERRORS,
        "max_paths": MAX_PATHS,
        "max_argument_chars": MAX_ARGUMENT_CHARS,
        "max_error_chars": MAX_ERROR_CHARS,
        "max_path_chars": MAX_PATH_CHARS,
    }
    base: dict[str, Any] = {
        "status": STATUS_UNAVAILABLE,
        "task_id": task_id,
        "availability": {},
        "sources": [],
        "session_ids": [],
        "session": {
            "entry_types": {},
            "message_counts": {},
            "first_timestamp": None,
            "last_timestamp": None,
        },
        "events": {
            "event_types": {},
            "first_timestamp": None,
            "last_timestamp": None,
        },
        "timestamps": {"first_observed": None, "last_observed": None},
        "usage": {},
        "tool_calls": [],
        "operations": [],
        "paths": [],
        "errors": [],
        "omitted": {"tool_calls": 0, "errors": 0, "paths": 0},
        "limits": limits,
        "notes": [],
    }
    if not roots:
        base["availability"] = {
            SESSION_FILENAME: {
                "status": SOURCE_MISSING,
                "reason": "no artifact roots were supplied; no file was read",
                "files": 0,
            },
            EVENTS_FILENAME: {
                "status": SOURCE_MISSING,
                "reason": "no artifact roots were supplied; no file was read",
                "files": 0,
            },
        }
        base["notes"].append(
            "Trace artifacts were not checked because no artifact root was supplied. "
            "No trace observations were inferred."
        )
        return base

    accepted, rejected = _provenance_paths(meta, roots, task_id)
    discovered, discovery_limit = _discover_task_artifacts(roots, task_id)

    all_paths: dict[str, list[tuple[Path, str]]] = {
        SESSION_FILENAME: [],
        EVENTS_FILENAME: [],
    }
    seen_paths: dict[str, set[Path]] = {
        SESSION_FILENAME: set(),
        EVENTS_FILENAME: set(),
    }
    for kind in TRACE_KINDS:
        # The same journal often exists twice: the live file under the
        # agent state directory (provenance) and the artifact copy the
        # worker takes after execution (discovered). Different paths, so
        # path dedup alone lets every record, error and token enter the
        # trace twice. A byte-identical copy (same size and mtime, what
        # shutil.copy2 produces) is dropped; a file that diverged after
        # the copy keeps both sources.
        fingerprints: set[tuple[int, int]] = set()
        for path, origin in accepted[kind]:
            if path not in seen_paths[kind]:
                seen_paths[kind].add(path)
                all_paths[kind].append((path, origin))
                fingerprint = _file_fingerprint(path)
                if fingerprint is not None:
                    fingerprints.add(fingerprint)
        for path in discovered[kind]:
            if path in seen_paths[kind]:
                continue
            fingerprint = _file_fingerprint(path)
            if fingerprint is not None and fingerprint in fingerprints:
                continue
            seen_paths[kind].add(path)
            all_paths[kind].append((path, "discovered"))
            if fingerprint is not None:
                fingerprints.add(fingerprint)

    collector = _TraceCollector()
    sources: list[dict[str, Any]] = []
    remaining = MAX_TOTAL_BYTES

    for kind in TRACE_KINDS:
        for path, origin in all_paths[kind]:
            if remaining <= 0:
                sources.append(
                    _source_entry(
                        kind,
                        path,
                        roots,
                        {
                            "status": SOURCE_SKIPPED_LIMIT,
                            "reason": "total trace byte limit reached",
                            "bytes_read": 0,
                            "truncated": False,
                            "records": 0,
                            "malformed_lines": 0,
                        },
                        origin,
                    )
                )
                continue
            budget = min(MAX_BYTES_PER_ARTIFACT, remaining)
            records, metadata, _ = _read_records(
                path,
                max_bytes=budget,
                tail=(kind == EVENTS_FILENAME),
            )
            remaining -= int(metadata.get("bytes_read") or 0)
            sources.append(_source_entry(kind, path, roots, metadata, origin))
            for record in records:
                if kind == SESSION_FILENAME:
                    collector.record_session_entry(record)
                else:
                    collector.record_event(record)

    collector_result = collector.finalize()
    base.update(collector_result)
    base["sources"] = sources

    session_sources = [
        source for source in sources if source.get("kind") == SESSION_FILENAME
    ]
    event_sources = [
        source for source in sources if source.get("kind") == EVENTS_FILENAME
    ]
    availability = {
        SESSION_FILENAME: _availability(
            SESSION_FILENAME,
            session_sources,
            rejected.get(SESSION_FILENAME, []),
            discovery_limit=discovery_limit,
        ),
        EVENTS_FILENAME: _availability(
            EVENTS_FILENAME,
            event_sources,
            rejected.get(EVENTS_FILENAME, []),
            discovery_limit=discovery_limit,
        ),
    }
    base["availability"] = availability

    session_available = availability[SESSION_FILENAME]["status"] == SOURCE_AVAILABLE
    events_available = availability[EVENTS_FILENAME]["status"] == SOURCE_AVAILABLE
    degraded = any(
        source.get("truncated")
        or source.get("malformed_lines")
        or source.get("status") not in {SOURCE_AVAILABLE}
        for source in sources
    ) or discovery_limit
    if session_available and events_available and not degraded:
        base["status"] = STATUS_AVAILABLE
    elif session_available or events_available:
        base["status"] = STATUS_PARTIAL
    else:
        base["status"] = STATUS_UNAVAILABLE

    if rejected.get(SESSION_FILENAME) or rejected.get(EVENTS_FILENAME):
        base["notes"].append(
            "One or more provenance artifact paths were rejected because they "
            "were outside the target task's artifact roots. No such file was read."
        )
    if discovery_limit:
        base["notes"].append(
            "Artifact discovery stopped at the configured directory/file limit; "
            "files beyond the limit were not inspected."
        )
    if base["status"] == STATUS_UNAVAILABLE:
        base["notes"].append(
            "No readable session.jsonl or events.jsonl observation was available "
            "for the target task. Missing trace data is not evidence of a clean run."
        )
    elif base["status"] == STATUS_PARTIAL:
        base["notes"].append(
            "Trace data is partial: at least one source is missing, truncated, "
            "malformed or unavailable. Do not infer facts for the missing source."
        )
    return base


__all__ = [
    "EVENTS_FILENAME",
    "SESSION_FILENAME",
    "STATUS_AVAILABLE",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "build_task_trace",
    "redact_text",
]
