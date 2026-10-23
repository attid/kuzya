"""Regression tests for recursive /outbox artifact collection."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.storage import AgentProfileRepository, Database, TaskRepository
from kuzya.task_worker import (
    TaskExecutionWorker,
    _allocate_artifact_names,
    _collect_outbox_artifacts,
    _filesystem_name_max,
)


def _run_worker_with_outbox(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes | str],
    *,
    before_run=None,
) -> tuple[Database, object, Path]:
    """Run one real worker step while a fake child writes ``files`` to /outbox."""
    database = Database.from_path(migrated_db)
    AgentProfileRepository(database).create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Collect outbox files.",
        runner_config={"mode": "success", "summary_text": "Execution finished."},
    )
    task = TaskRepository(database).create(
        title="Outbox artifact collection",
        status="in_progress",
        metadata={"task_type": "general"},
    )
    summary_path = tmp_path / "worker-summary.md"
    summary_path.write_text("worker summary", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        role_path = Path(cmd[cmd.index("--agent") + 1])
        role = json.loads(role_path.read_text(encoding="utf-8"))
        outbox = Path(role["outbox_dir"])
        if before_run is not None:
            before_run(outbox)
        for relative_path, content in files.items():
            destination = outbox / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                destination.write_bytes(content)
            else:
                destination.write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "status": "completed",
                    "summary_path": str(summary_path),
                    "changed_files": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "kuzya.task_worker.TaskProcessRegistry.run_process",
        lambda self, task_id, cmd, *, cwd, env=None: (fake_run(cmd, cwd=cwd, env=env), False),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    worker = TaskExecutionWorker(
        database=database,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        runner_argv_prefix=["true"],
        default_workspace=str(workspace),
    )
    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "completed", outcome.error
    return database, task, tmp_path / "artifacts"


def _artifact_contents_via_api(
    migrated_db: Path,
    artifact_root: Path,
    task_id: str,
) -> tuple[list[str], dict[str, bytes]]:
    """List a task's artifacts and fetch every listed artifact through the API."""
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)
    with TestClient(app) as client:
        task_response = client.get(f"/tasks/{task_id}")
        assert task_response.status_code == 200, task_response.text
        response = client.get(f"/tasks/{task_id}/artifacts")
        assert response.status_code == 200, response.text
        names = [artifact["name"] for artifact in response.json()["artifacts"]]
        assert len(names) == len(set(names)), f"duplicate artifact names: {names}"
        contents: dict[str, bytes] = {}
        for name in names:
            fetched = client.get(
                f"/tasks/{task_id}/artifacts/{quote(name, safe='')}"
            )
            assert fetched.status_code == 200, (name, fetched.text)
            contents[name] = fetched.content
        return names, contents


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {"alpha.txt": b"A", "beta.log": "B"},
            {"alpha.txt": b"A", "beta.log": b"B"},
        ),
        (
            {"reports/errors.log": b"one level"},
            {"reports_errors.log": b"one level"},
        ),
        (
            {"reports/errors/2025/log.txt": b"many levels"},
            {"reports_errors_2025_log.txt": b"many levels"},
        ),
        (
            {
                "reports/a.txt": b"A",
                "reports/b.txt": b"B",
                "reports/c.txt": b"C",
            },
            {
                "reports_a.txt": b"A",
                "reports_b.txt": b"B",
                "reports_c.txt": b"C",
            },
        ),
        pytest.param(
            {os.fsdecode(b"report-\xff.txt"): b"raw bytes"},
            {"report-~FF.txt": b"raw bytes"},
            id="non-utf8-filename-bytes",
        ),
        pytest.param(
            {
                "percent %20 name.txt": b"literal %20",
                "literal %FF.txt": b"literal %FF",
            },
            {
                "percent %20 name.txt": b"literal %20",
                "literal %FF.txt": b"literal %FF",
            },
            id="literal-percent-escapes",
        ),
        pytest.param(
            {
                "line\nbreak.txt": b"newline",
                "dir/tab\tname.txt": b"tab",
                "delete\x7f.txt": b"del",
            },
            {
                "line~0Abreak.txt": b"newline",
                "dir_tab~09name.txt": b"tab",
                "delete~7F.txt": b"del",
            },
            id="control-characters",
        ),
    ],
)
def test_worker_collects_flat_and_nested_files_through_api(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes | str],
    expected: dict[str, bytes],
) -> None:
    database, task, artifact_root = _run_worker_with_outbox(
        migrated_db, tmp_path, monkeypatch, files
    )

    names, contents = _artifact_contents_via_api(
        migrated_db, artifact_root, task.id
    )
    for name, payload in expected.items():
        assert name in names
        assert contents[name] == payload
    assert contents["summary.md"] == b"worker summary"

    stored = TaskRepository(database).get(task.id)
    assert stored is not None
    recorded = {
        artifact["name"]: Path(artifact["path"]).read_bytes()
        for artifact in stored.meta["provenance"]["artifacts"]
    }
    for name, payload in expected.items():
        assert recorded[name] == payload
    assert not stored.meta["provenance"].get("artifact_errors")


def test_worker_resolves_collisions_without_losing_root_or_nested_files(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        "a_b.txt": b"root file",
        "a/b.txt": b"nested file one",
        "a_b/c.txt": b"nested file two",
        "a/b_c.txt": b"nested file three",
        "summary.md": b"outbox summary",
    }
    database, task, artifact_root = _run_worker_with_outbox(
        migrated_db, tmp_path, monkeypatch, payloads
    )

    names, contents = _artifact_contents_via_api(
        migrated_db, artifact_root, task.id
    )
    # A root file keeps its original name even when a nested path maps to it.
    assert "a_b.txt" in names
    assert contents["a_b.txt"] == b"root file"
    # The generated worker summary is never overwritten by an outbox file.
    assert contents["summary.md"] == b"worker summary"
    assert sorted(contents.values()) == sorted(
        [*payloads.values(), b"worker summary"]
    )
    assert len(names) == len(payloads) + 1

    stored = TaskRepository(database).get(task.id)
    artifact_errors = stored.meta["provenance"].get("artifact_errors") or []
    assert artifact_errors == []


def test_worker_preserves_unicode_spaces_and_special_characters(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        "отчёт 2025.txt": b"unicode root",
        "папка с пробелом/резюме #1.log": b"nested unicode",
        "dir/special $&;@!.txt": b"special characters",
        "100%.txt": b"percent",
    }
    _, task, artifact_root = _run_worker_with_outbox(
        migrated_db, tmp_path, monkeypatch, payloads
    )

    names, contents = _artifact_contents_via_api(
        migrated_db, artifact_root, task.id
    )
    assert "отчёт 2025.txt" in names
    assert "папка с пробелом_резюме #1.log" in names
    assert "dir_special $&;@!.txt" in names
    assert "100%.txt" in names
    assert contents["отчёт 2025.txt"] == b"unicode root"
    assert contents["папка с пробелом_резюме #1.log"] == b"nested unicode"
    assert contents["dir_special $&;@!.txt"] == b"special characters"
    assert contents["100%.txt"] == b"percent"


def test_worker_shortens_overlong_nested_names_and_preserves_contents(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each component is valid, but the joined artifact name exceeds NAME_MAX.
    first = f"{'a' * 80}/{'b' * 80}/{'c' * 80}/{'d' * 80}/first.log"
    second = f"{'e' * 80}/{'f' * 80}/{'g' * 80}/{'h' * 80}/second.log"
    long_root = f"{'r' * 250}.txt"
    payloads = {
        long_root: b"root at the limit",
        first: b"first long path",
        second: b"second long path",
    }
    database, task, artifact_root = _run_worker_with_outbox(
        migrated_db, tmp_path, monkeypatch, payloads
    )

    names, contents = _artifact_contents_via_api(
        migrated_db, artifact_root, task.id
    )
    assert long_root in names
    assert contents[long_root] == b"root at the limit"

    stored = TaskRepository(database).get(task.id)
    artifact_names = [
        artifact["name"]
        for artifact in stored.meta["provenance"]["artifacts"]
    ]
    shortened = [
        name
        for name in artifact_names
        if name not in {long_root, "summary.md"}
    ]
    assert len(shortened) == 2
    artifact_dir = artifact_root / task.id / "artifacts"
    assert os.pathconf(artifact_dir, "PC_NAME_MAX") == 255
    assert _filesystem_name_max(artifact_dir) == 255
    for name in shortened:
        assert len(name.encode("utf-8")) <= 255
        assert name.endswith(".log")
    assert len(set(shortened)) == 2
    assert {contents[name] for name in shortened} == {
        b"first long path",
        b"second long path",
    }


def test_collect_ignores_empty_directories(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    (outbox / "empty" / "nested").mkdir(parents=True)
    (outbox / "kept.txt").write_bytes(b"kept")

    collected, errors = _collect_outbox_artifacts(outbox, tmp_path / "artifacts")

    assert [artifact["name"] for artifact in collected] == ["kept.txt"]
    assert errors == []
    assert sorted(path.name for path in (tmp_path / "artifacts").iterdir()) == [
        "kept.txt"
    ]


def test_collect_skips_symlinks_and_never_escapes_outbox(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outbox = tmp_path / "outbox"
    nested = outbox / "nested"
    nested.mkdir(parents=True)
    (nested / "kept.txt").write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    os.symlink(outside / "secret.txt", outbox / "leak.txt")
    os.symlink(outside, outbox / "linked")
    os.symlink(nested / "kept.txt", outbox / "inside-link.txt")

    with caplog.at_level(logging.WARNING, logger="kuzya.task_worker"):
        collected, errors = _collect_outbox_artifacts(
            outbox, tmp_path / "artifacts"
        )

    assert [artifact["name"] for artifact in collected] == ["nested_kept.txt"]
    assert (tmp_path / "artifacts" / "nested_kept.txt").read_bytes() == b"safe"
    assert not (tmp_path / "artifacts" / "leak.txt").exists()
    assert not (tmp_path / "artifacts" / "linked_secret.txt").exists()
    assert not (tmp_path / "artifacts" / "inside-link.txt").exists()
    error_sources = {error["source"] for error in errors}
    assert {"leak.txt", "linked", "inside-link.txt"} <= error_sources
    assert "symlink" in caplog.text


def test_non_utf8_error_is_sanitized_and_does_not_break_task_api(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    non_utf8_name = os.fsdecode(b"lien-\xff.txt")

    def create_symlink(outbox: Path) -> None:
        os.symlink(outside, outbox / non_utf8_name)

    database, task, artifact_root = _run_worker_with_outbox(
        migrated_db,
        tmp_path,
        monkeypatch,
        {"ok.txt": b"good"},
        before_run=create_symlink,
    )

    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)
    with TestClient(app) as client:
        listed = client.get("/tasks")
        assert listed.status_code == 200, listed.text
        filtered = client.get("/tasks", params={"status": "done"})
        assert filtered.status_code == 200, filtered.text
        fetched = client.get(f"/tasks/{task.id}")
        assert fetched.status_code == 200, fetched.text
        provenance = fetched.json()["metadata"]["provenance"]
        errors = provenance["artifact_errors"]
        assert any(
            error["source"] == "lien-~FF.txt"
            and "symlink" in error["error"]
            for error in errors
        )
        artifacts = client.get(f"/tasks/{task.id}/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        assert any(
            artifact["name"] == "ok.txt"
            for artifact in artifacts.json()["artifacts"]
        )

    stored = TaskRepository(database).get(task.id)
    assert stored is not None
    assert any(
        error["source"] == "lien-~FF.txt"
        for error in stored.meta["provenance"]["artifact_errors"]
    )


def test_artifact_download_rejects_path_traversal(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Traversal target"}).json()
        task_dir = artifact_root / str(task["id"]) / "artifacts"
        task_dir.mkdir(parents=True)
        (task_dir / "safe.txt").write_text("safe", encoding="utf-8")
        relative = os.path.relpath(secret, task_dir).replace(os.sep, "/")
        traversal = client.get(
            f"/tasks/{task['id']}/artifacts/{quote(relative, safe='')}"
        )
        safe_artifact = client.get(
            f"/tasks/{task['id']}/artifacts/safe.txt"
        )

    assert traversal.status_code == 404
    assert b"SECRET" not in traversal.content
    assert safe_artifact.status_code == 200
    assert safe_artifact.content == b"safe"


def test_collect_reports_unreadable_file_without_false_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "ok.txt").write_bytes(b"good")
    (outbox / "blocked.txt").write_bytes(b"secret")

    real_copy = shutil.copy2

    def flaky_copy(source, destination, *args, **kwargs):
        if Path(source).name == "blocked.txt":
            raise PermissionError("permission denied")
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr("kuzya.task_worker.shutil.copy2", flaky_copy)
    with caplog.at_level(logging.WARNING, logger="kuzya.task_worker"):
        collected, errors = _collect_outbox_artifacts(
            outbox, tmp_path / "artifacts"
        )

    assert [artifact["name"] for artifact in collected] == ["ok.txt"]
    assert (tmp_path / "artifacts" / "ok.txt").read_bytes() == b"good"
    assert not (tmp_path / "artifacts" / "blocked.txt").exists()
    assert any(
        error["source"] == "blocked.txt"
        and "permission denied" in error["error"]
        for error in errors
    )
    assert "blocked.txt" in caplog.text
    assert not list((tmp_path / "artifacts").glob(".collect-*.tmp"))


def test_worker_does_not_publish_failed_outbox_copy(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_copy = shutil.copy2

    def flaky_copy(source, destination, *args, **kwargs):
        if Path(source).name == "blocked.txt":
            raise PermissionError("permission denied")
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr("kuzya.task_worker.shutil.copy2", flaky_copy)
    database, task, artifact_root = _run_worker_with_outbox(
        migrated_db,
        tmp_path,
        monkeypatch,
        {"ok.txt": b"good", "blocked.txt": b"secret"},
    )

    names, contents = _artifact_contents_via_api(
        migrated_db, artifact_root, task.id
    )
    assert contents["ok.txt"] == b"good"
    assert "blocked.txt" not in names
    assert not any(payload == b"secret" for payload in contents.values())

    stored = TaskRepository(database).get(task.id)
    errors = stored.meta["provenance"]["artifact_errors"]
    assert any(
        error["source"] == "blocked.txt"
        and "permission denied" in error["error"]
        for error in errors
    )


def test_reserved_name_does_not_displace_a_real_file() -> None:
    reserved = "summary.md"
    digest = hashlib.sha256(reserved.encode("utf-8")).hexdigest()[:12]
    real_name = f"summary_{digest}.md"

    names = _allocate_artifact_names(
        [reserved, real_name],
        max_bytes=255,
        reserved=[reserved],
    )

    assert names[real_name] == real_name
    assert names[reserved] != real_name


def test_name_allocation_is_deterministic_under_tight_limits() -> None:
    relative_paths = [
        "/".join(["目录" * 20, "résumé one.txt"]),
        "/".join(["目录" * 20, "résumé two.txt"]),
        "/".join(["目录" * 20, "résumé one.txt", "child.log"]),
    ]

    first = _allocate_artifact_names(relative_paths, max_bytes=64)
    second = _allocate_artifact_names(relative_paths, max_bytes=64)

    assert first == second
    assert len(set(first.values())) == len(relative_paths)
    for relative_path, name in first.items():
        assert len(name.encode("utf-8")) <= 64
        expected_extension = Path(relative_path).suffix
        assert name.endswith(expected_extension)
