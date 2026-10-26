"""Diagnostic trace evidence from a target task's own Runner artifacts."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

from kuzya.analysis import (
    AnalyzerRunResult,
    build_evidence_package,
)
from kuzya.operation_handlers import register_default_operation_handlers
from kuzya.operation_worker import (
    OPERATION_KIND_ANALYSIS,
    OperationInstanceContext,
    OperationWorker,
)
from kuzya.session_trace import (
    EVENTS_FILENAME,
    SESSION_FILENAME,
    build_task_trace,
)
from kuzya.storage import (
    AnalysisRepository,
    Database,
    OperationRepository,
    TaskRepository,
)


def _write_jsonl(path: pathlib.Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        + "\n",
        encoding="utf-8",
    )


def _task_artifacts(
    root: pathlib.Path,
    task_id: str,
    *,
    path_suffix: str = "abc123",
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    task_dir = root / f"task_{task_id}_{path_suffix}"
    session_path = (
        task_dir
        / "state"
        / "sessions"
        / "session-1"
        / "pi"
        / SESSION_FILENAME
    )
    events_path = (
        task_dir
        / "state"
        / "sessions"
        / "session-1"
        / "attempts"
        / "1"
        / EVENTS_FILENAME
    )
    return task_dir, session_path, events_path


def _successful_artifacts(
    tmp_path: pathlib.Path, task_id: str = "task-one"
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, dict[str, Any]]:
    root = tmp_path / "tasks"
    task_dir, session_path, events_path = _task_artifacts(root, task_id)
    _write_jsonl(
        session_path,
        [
            {
                "type": "session",
                "id": "session-1",
                "timestamp": "2026-02-01T10:00:00Z",
            },
            {
                "type": "message",
                "timestamp": "2026-02-01T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [
                        {
                            "type": "text",
                            "text": "FULL TRANSCRIPT MUST NOT LEAK",
                        },
                        {
                            "type": "toolCall",
                            "id": "call-read",
                            "name": "read",
                            "arguments": {"path": "/workspace/src/app.py"},
                        },
                        {
                            "type": "toolCall",
                            "id": "call-bash",
                            "name": "bash",
                            "arguments": {
                                "command": (
                                    "cat /workspace/src/app.py "
                                    "--password hunter2"
                                ),
                                "api_key": "sk-live-abcdefghijklmnop",
                            },
                        },
                    ],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-02-01T10:00:02Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-read",
                    "toolName": "read",
                    "isError": False,
                    "content": [
                        {
                            "type": "text",
                            "text": "FILE CONTENT MUST NOT LEAK",
                        }
                    ],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-02-01T10:00:03Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-bash",
                    "toolName": "bash",
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "permission denied token=super-secret-value "
                                "Authorization: Bearer bearer-secret-value"
                            )
                        }
                    ],
                },
            },
        ],
    )
    _write_jsonl(
        events_path,
        [
            {
                "type": "session",
                "id": "session-1",
                "timestamp": "2026-02-01T10:00:00Z",
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "call-read",
                "toolName": "read",
                "args": {"path": "/workspace/src/app.py"},
                "timestamp": "2026-02-01T10:00:01Z",
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-read",
                "toolName": "read",
                "isError": False,
                "durationMs": 12,
                "result": {
                    "content": [
                        {"type": "text", "text": "FILE CONTENT MUST NOT LEAK"}
                    ]
                },
                "timestamp": "2026-02-01T10:00:02Z",
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "call-bash",
                "toolName": "bash",
                "args": {
                    "command": (
                        "cat /workspace/src/app.py "
                        "--password hunter2 "
                        "https://user:pass@example.invalid/project"
                    ),
                    "authorization": "Bearer header-secret-value",
                },
                "timestamp": "2026-02-01T10:00:03Z",
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-bash",
                "toolName": "bash",
                "isError": True,
                "durationMs": 30,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "command failed: permission denied "
                                "token=super-secret-value"
                            )
                        }
                    ]
                },
                "timestamp": "2026-02-01T10:00:04Z",
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "call-long",
                "toolName": "bash",
                "args": {
                    "command": (
                        "echo " + ("x" * 2000) + " --token long-secret-token"
                    )
                },
                "timestamp": "2026-02-01T10:00:05Z",
            },
            {
                "type": "message_end",
                "timestamp": "2026-02-01T10:00:06Z",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [
                        {
                            "type": "text",
                            "text": "FINAL FULL TRANSCRIPT MUST NOT LEAK",
                        }
                    ],
                    "usage": {
                        "input": 7,
                        "output": 3,
                    },
                },
            },
        ],
    )
    provenance = {
        "session_path": str(session_path),
        "events_path": str(events_path),
        "runner_state_dir": str(task_dir / "state"),
        "runner_session_id": "session-1",
        "started_at": "2026-02-01T10:00:00Z",
        "ended_at": "2026-02-01T10:00:06Z",
    }
    return root, session_path, events_path, provenance


def test_trace_observes_tool_calls_paths_errors_and_redacts_secrets(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        root, _session_path, _events_path, provenance = _successful_artifacts(
            tmp_path
        )
        tasks = TaskRepository(database)
        task = tasks.create(
            id="task-one",
            title="Trace target",
            metadata={"provenance": provenance},
        )
        # A sibling task's artifact must never enter the target evidence.
        sibling_dir, sibling_session, sibling_events = _task_artifacts(
            root, "task-two", path_suffix="other"
        )
        _write_jsonl(
            sibling_session,
            [
                {
                    "type": "session",
                    "id": "sibling-session",
                    "timestamp": "2026-02-01T09:00:00Z",
                }
            ],
        )
        _write_jsonl(
            sibling_events,
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "sibling-call",
                    "toolName": "read",
                    "args": {"path": "/workspace/SIBLING-SECRET-PATH"},
                }
            ],
        )
        assert sibling_dir.exists()

        evidence = build_evidence_package(
            database, task, artifact_roots=[root]
        )
        trace = evidence["trace"]

        assert trace["status"] == "available", trace
        assert trace["availability"][SESSION_FILENAME]["status"] == "available"
        assert trace["availability"][EVENTS_FILENAME]["status"] == "available"
        assert trace["session_ids"] == ["session-1"]
        assert trace["session"]["message_counts"]["toolResult"] == 2
        assert trace["events"]["event_types"]["tool_execution_end"] == 2
        assert trace["usage"] == {"input": 7, "output": 3}
        assert trace["timestamps"]["first_observed"] == "2026-02-01T10:00:00Z"
        assert trace["timestamps"]["last_observed"] == "2026-02-01T10:00:06Z"

        calls = {call["name"]: call for call in trace["tool_calls"]}
        read_call = next(
            call for call in trace["tool_calls"] if call["name"] == "read"
        )
        assert read_call["status"] == "ok"
        assert read_call["duration_ms"] == 12
        assert read_call["sources"] == ["events", "session"]

        bash_call = next(
            call
            for call in trace["tool_calls"]
            if call["name"] == "bash" and call["status"] == "error"
        )
        assert bash_call["duration_ms"] == 30
        assert "permission denied" in bash_call["error"]
        assert trace["errors"]
        assert "permission denied" in trace["errors"][0]["message"]

        # The repeated path is observed from both sources and both tools.
        repeated = trace["paths"][0]
        assert repeated["path"] == "/workspace/src/app.py"
        assert repeated["repeated"] is True
        assert repeated["count"] >= 2
        assert set(repeated["tools"]) == {"bash", "read"}

        # Durations are aggregated by operation type.
        operations = {item["name"]: item for item in trace["operations"]}
        assert operations["read"]["duration_ms_total"] == 12
        assert operations["bash"]["duration_ms_total"] == 30
        assert operations["bash"]["error_count"] >= 1

        # Only bounded/redacted arguments are present, never raw transcript
        # text, file contents or tool outputs.
        serialized = json.dumps(evidence, ensure_ascii=False)
        for forbidden in (
            "hunter2",
            "super-secret-value",
            "bearer-secret-value",
            "header-secret-value",
            "sk-live-abcdefghijklmnop",
            "long-secret-token",
            "user:pass@example.invalid",
            "FILE CONTENT MUST NOT LEAK",
            "FULL TRANSCRIPT MUST NOT LEAK",
            "FINAL FULL TRANSCRIPT MUST NOT LEAK",
            "SIBLING-SECRET-PATH",
            "sibling-session",
        ):
            assert forbidden not in serialized, forbidden
        assert "[REDACTED]" in serialized
        long_call = next(
            call for call in trace["tool_calls"] if call["name"] == "bash"
            and call.get("arguments", {}).get("truncated") is True
        )
        assert len(json.dumps(long_call["arguments"])) < 700

        # The trace is also a flat evidence item so recommendations can cite it.
        assert any(
            item["id"] == "trace:task" and item["kind"] == "trace"
            for item in evidence["items"]
        )
        assert all(item.get("content") is None for item in evidence["items"])
        assert all(set(item.keys()) == {"id", "kind"} for item in evidence["items"])
        assert calls  # local sanity: the observed calls are non-empty
    finally:
        database.dispose()


def test_trace_reports_unavailable_when_artifacts_are_missing(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        root = tmp_path / "tasks"
        root.mkdir()
        task = TaskRepository(database).create(
            id="task-one", title="No artifacts"
        )

        trace = build_evidence_package(
            database, task, artifact_roots=[root]
        )["trace"]

        assert trace["status"] == "unavailable"
        assert trace["availability"][SESSION_FILENAME]["status"] == "missing"
        assert trace["availability"][EVENTS_FILENAME]["status"] == "missing"
        assert trace["tool_calls"] == []
        assert trace["operations"] == []
        assert trace["paths"] == []
        assert trace["errors"] == []
        assert any(
            "No readable session.jsonl or events.jsonl" in note
            for note in trace["notes"]
        )

        direct = build_task_trace(task, [root])
        assert direct["status"] == "unavailable"
    finally:
        database.dispose()


def test_trace_rejects_provenance_paths_outside_task_artifact_scope(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        root = tmp_path / "tasks"
        root.mkdir()
        outside = tmp_path / "outside"
        outside_session = outside / SESSION_FILENAME
        outside_events = outside / EVENTS_FILENAME
        _write_jsonl(
            outside_session,
            [
                {
                    "type": "session",
                    "id": "outside-session",
                    "timestamp": "2026-02-01T10:00:00Z",
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "outside-call",
                                "name": "read",
                                "arguments": {
                                    "path": "/outside/OUTSIDE-SECRET-PATH"
                                },
                            }
                        ],
                    },
                },
            ],
        )
        _write_jsonl(
            outside_events,
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "outside-call",
                    "toolName": "read",
                    "args": {"path": "/outside/OUTSIDE-SECRET-PATH"},
                }
            ],
        )
        task = TaskRepository(database).create(
            id="task-one",
            title="Outside provenance",
            metadata={
                "provenance": {
                    "session_path": str(outside_session),
                    "events_path": str(outside_events),
                }
            },
        )

        evidence = build_evidence_package(
            database, task, artifact_roots=[root]
        )
        trace = evidence["trace"]

        assert trace["status"] == "unavailable"
        assert (
            trace["availability"][SESSION_FILENAME]["status"]
            == "outside_scope"
        )
        assert trace["availability"][EVENTS_FILENAME]["status"] == "outside_scope"
        assert trace["tool_calls"] == []
        trace_serialized = json.dumps(trace, ensure_ascii=False)
        assert "OUTSIDE-SECRET-PATH" not in trace_serialized
        assert "outside-session" not in trace_serialized
        assert str(outside) not in trace_serialized
    finally:
        database.dispose()


def test_trace_does_not_follow_symlink_outside_task_scope(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        root = tmp_path / "tasks"
        task_dir, session_path, events_path = _task_artifacts(
            root, "task-one"
        )
        task_dir.mkdir(parents=True)
        outside = tmp_path / "outside-session.jsonl"
        _write_jsonl(
            outside,
            [
                {
                    "type": "session",
                    "id": "symlink-secret",
                    "timestamp": "2026-02-01T10:00:00Z",
                }
            ],
        )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.symlink_to(outside)
        _write_jsonl(
            events_path,
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-read",
                    "toolName": "read",
                    "args": {"path": "/workspace/src/app.py"},
                }
            ],
        )
        task = TaskRepository(database).create(
            id="task-one", title="Symlink target"
        )

        evidence = build_evidence_package(
            database, task, artifact_roots=[root]
        )
        trace = evidence["trace"]

        # The event source is still available, but the symlinked session is not
        # read and the trace is explicitly partial.
        assert trace["status"] == "partial"
        assert trace["availability"][SESSION_FILENAME]["status"] == "missing"
        serialized = json.dumps(evidence, ensure_ascii=False)
        assert "symlink-secret" not in serialized
        assert str(outside) not in serialized
    finally:
        database.dispose()


def test_trace_omits_content_bearing_tool_arguments(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "tasks"
    _task_dir, session_path, events_path = _task_artifacts(
        root, "task-one"
    )
    _write_jsonl(
        session_path,
        [
            {
                "type": "session",
                "id": "session-1",
                "timestamp": "2026-02-01T10:00:00Z",
            }
        ],
    )
    _write_jsonl(
        events_path,
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "call-write",
                "toolName": "write",
                "args": {
                    "path": "/workspace/.env",
                    "content": "TOP-SECRET-FILE-CONTENT",
                    "oldText": "OLD-SECRET-FILE-CONTENT",
                    "newText": "NEW-SECRET-FILE-CONTENT",
                },
                "timestamp": "2026-02-01T10:00:01Z",
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-write",
                "toolName": "write",
                "isError": False,
                "durationMs": 4,
                "timestamp": "2026-02-01T10:00:02Z",
            },
        ],
    )
    task = SimpleNamespace(id="task-one", meta={})

    trace = build_task_trace(task, [root])
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["status"] == "available"
    assert "TOP-SECRET-FILE-CONTENT" not in serialized
    assert "OLD-SECRET-FILE-CONTENT" not in serialized
    assert "NEW-SECRET-FILE-CONTENT" not in serialized
    write_call = next(
        call for call in trace["tool_calls"] if call["name"] == "write"
    )
    assert write_call["arguments"]["path"] == "/workspace/.env"
    assert write_call["arguments"]["content"].startswith("[omitted:")
    assert write_call["arguments"]["oldText"].startswith("[omitted:")
    assert write_call["arguments"]["newText"].startswith("[omitted:")


def test_analysis_executor_passes_task_trace_to_analyzer(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    captured: dict[str, Any] = {}

    def runner(request: Any) -> AnalyzerRunResult:
        captured["trace"] = request.evidence["trace"]
        return AnalyzerRunResult(
            text=json.dumps(
                {
                    "summary": "Observed the read tool call.",
                    "findings": [],
                    "recommendations": [],
                }
            ),
            profile_revision=1,
        )

    try:
        root, _session_path, _events_path, provenance = _successful_artifacts(
            tmp_path
        )
        task = TaskRepository(database).create(
            id="task-one",
            title="Analyze with trace",
            metadata={"provenance": provenance},
        )
        instance = OperationInstanceContext(
            database=database,
            artifact_root=root,
            analyzer_runner=runner,
        )
        worker = OperationWorker(database, instance=instance)
        register_default_operation_handlers(worker)
        operation = OperationRepository(database).submit(
            kind=OPERATION_KIND_ANALYSIS,
            target_id=task.id,
            input={},
        ).operation

        outcome = worker.step()

        assert outcome is not None and outcome.status == "succeeded", (
            outcome.error if outcome else "no outcome"
        )
        assert operation.id
        trace = captured["trace"]
        assert trace["status"] == "available"
        assert trace["session_ids"] == ["session-1"]
        assert {call["name"] for call in trace["tool_calls"]} >= {
            "read",
            "bash",
        }
        reports = AnalysisRepository(database).list_reports(task_id=task.id)
        assert len(reports) == 1
        assert reports[0].evidence["trace"]["status"] == "available"
    finally:
        database.dispose()


def test_trace_deduplicates_artifact_copy_of_session_journal(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The worker's artifact copy (same size, same mtime) must not double the trace."""
    import shutil

    database = Database.from_path(migrated_db)
    try:
        root, session_path, events_path, provenance = _successful_artifacts(
            tmp_path
        )
        task_dir = session_path.parents[3]
        tasks = TaskRepository(database)
        task = tasks.create(
            id="task-one",
            title="Copy target",
            metadata={"provenance": provenance},
        )

        # Without the copy there is exactly one session source.
        single = build_task_trace(task, [root])
        session_sources = [
            source
            for source in single["sources"]
            if source["kind"] == SESSION_FILENAME
        ]
        assert len(session_sources) == 1

        # The worker takes the artifact copy with shutil.copy2, which
        # preserves the mtime — discovery then finds the same journal
        # under two different paths.
        artifact_copy = task_dir / "artifacts" / SESSION_FILENAME
        artifact_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(session_path, artifact_copy)
        assert artifact_copy.stat().st_mtime_ns == session_path.stat().st_mtime_ns

        doubled = build_task_trace(task, [root])
        session_sources = [
            source
            for source in doubled["sources"]
            if source["kind"] == SESSION_FILENAME
        ]
        assert len(session_sources) == 1
        assert session_sources[0]["origin"] == "provenance"
        # Usage and message counts stay single, not doubled.
        assert doubled["usage"] == single["usage"]
        assert (
            doubled["session"]["message_counts"]
            == single["session"]["message_counts"]
        )

        # A journal that kept growing after the copy is genuinely two
        # different files: both sources stay.
        with session_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-02-01T10:00:07Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "more"}],
                        },
                    }
                )
                + "\n"
            )
        diverged = build_task_trace(task, [root])
        session_sources = [
            source
            for source in diverged["sources"]
            if source["kind"] == SESSION_FILENAME
        ]
        assert len(session_sources) == 2
    finally:
        database.dispose()
