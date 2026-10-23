"""Tests for TaskExecutionWorker and TelegramNotifier."""

from __future__ import annotations

import asyncio
import functools
import json
import re
from pathlib import Path
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kuzya.storage import (
    AgentProfileRepository,
    Database,
    TaskRepository,
)
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.task_worker import (
    READONLY_TEST_ENV_DIR,
    TaskExecutionWorker,
    TaskOutcome,
    _resolve_python_interpreter,
)
from kuzya.telegram.notifier import (
    TelegramNotifier,
    build_task_outcome_keyboard,
    format_duration_ms,
    format_task_outcome_message,
)


def sync_test(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


@pytest.fixture
def fake_runner_argv() -> list[str]:
    runner_script = Path(__file__).parent / "fake_pi_runner.py"
    return [sys.executable, str(runner_script)]


@pytest.fixture
def setup_developer_agent(migrated_db: Path) -> tuple[Database, str]:
    db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(db)
    agent_id = "kuzya-developer"
    agent_repo.create(
        agent_id=agent_id,
        name="Developer",
        role="developer",
        system_prompt="You are a developer agent.",
        runner_config={"mode": "success", "summary_text": "Code updated."},
    )
    return db, agent_id


def test_task_execution_worker_success(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Fix bug in auth module",
        description="Fix the auth token validation logic.",
        status="in_progress",
        workspace="test_ws",
    )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    artifact_root = tmp_path / "artifacts"

    agent_repo = AgentProfileRepository(test_db)
    agent_repo.update(
        agent_id=agent_id,
        expected_revision=1,
        system_prompt="Developer agent prompt",
        runner_config={
            "mode": "success",
            "summary_text": "Successfully fixed auth validation in token.py",
            "changed_files": ["src/auth/token.py"],
        },
    )

    notifications: list[tuple[Any, TaskOutcome]] = []

    def on_finished(updated_task: Any, outcome: TaskOutcome) -> None:
        notifications.append((updated_task, outcome))

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        workspace_map={"test_ws": str(workspace_dir)},
        on_task_finished=on_finished,
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "completed"
    assert outcome.task_id == task.id
    assert "Successfully fixed auth" in (outcome.summary or "")
    assert outcome.changed_files == ["src/auth/token.py"]
    assert outcome.duration_ms > 0

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "done"
    assert reloaded.meta["summary"] == outcome.summary
    assert reloaded.meta["changed_files"] == ["src/auth/token.py"]
    assert reloaded.meta["execution_finished_at"] is not None

    assert len(notifications) == 1
    assert notifications[0][0].id == task.id
    assert notifications[0][1].status == "completed"


def test_task_execution_worker_no_in_progress(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task_repo.create(title="Incoming task", status="incoming")

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
    )

    outcome = worker.step()
    assert outcome is None


def test_task_execution_worker_runner_nonzero_exit(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Failing task",
        status="in_progress",
    )

    agent_repo = AgentProfileRepository(test_db)
    agent_repo.update(
        agent_id=agent_id,
        expected_revision=1,
        system_prompt="Developer agent prompt",
        runner_config={"mode": "nonzero", "stderr_text": "Runner crash detected"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert "Runner process exited with code" in (outcome.error or "")
    assert "Runner crash detected" in (outcome.error or "")

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"
    assert reloaded.meta["execution_finished_at"] is not None


def test_task_execution_worker_runner_failed_status(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task failing internally",
        status="in_progress",
    )

    agent_repo = AgentProfileRepository(test_db)
    agent_repo.update(
        agent_id=agent_id,
        expected_revision=1,
        system_prompt="Developer agent prompt",
        runner_config={"mode": "failed_status"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert "Runner reported status 'failed'" in (outcome.error or "")

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"


def test_task_worker_parses_pretty_printed_runner_result(
    migrated_db: Path, tmp_path: Path
) -> None:
    """The real Runner CLI prints an indented multi-line JSON document.

    A single-line scan used to mark a successful execution as an unparseable
    failure. The worker must parse the whole document.
    """
    test_db = Database.from_path(migrated_db)
    AgentProfileRepository(test_db).create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Coding prompt.",
        runner_config={"tools": ["read", "write"]},
    )
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Update the file",
        status="in_progress",
        metadata={"task_type": "general"},
    )
    summary_path = tmp_path / "summary.md"
    summary_path.write_text("Changed the file.", encoding="utf-8")
    runner_stdout = json.dumps(
        {
            "status": "completed",
            "changed_files": ["index.html"],
            "summary_path": str(summary_path),
        },
        ensure_ascii=False,
        indent=2,
    )
    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    mock_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout=runner_stdout, stderr="")
    )
    with patch("subprocess.run", mock_run), patch(
        "kuzya.task_worker.TaskProcessRegistry.run_process",
        side_effect=lambda task_id, cmd, **kwargs: (
            mock_run(cmd, **kwargs),
            False,
        ),
    ):
        outcome = worker.step()

    assert outcome is not None
    assert outcome.status == "completed", outcome.error
    assert outcome.changed_files == ["index.html"]
    assert "Changed the file." in (outcome.summary or "")
    assert task_repo.get(task.id).status == "done"


def test_task_execution_worker_missing_agent_profile(
    migrated_db: Path,
    tmp_path: Path,
    fake_runner_argv: list[str],
) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task with unknown agent",
        status="in_progress",
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="non-existent-agent",
        runner_argv_prefix=fake_runner_argv,
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert "Agent profile not found in registry" in (outcome.error or "")

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"


def test_task_execution_worker_callback_exception_does_not_crash(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Callback error test",
        status="in_progress",
    )

    def crashing_callback(t: Any, o: Any) -> None:
        raise ValueError("Explosion in notification callback!")

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
        on_task_finished=crashing_callback,
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "completed"


def test_task_execution_worker_lifecycle(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        poll_interval_seconds=0.05,
    )

    worker.start()
    assert worker._thread is not None
    assert worker._thread.is_alive()

    worker.stop(timeout=2.0)
    assert not worker._thread.is_alive()


# -- Telegram Notifier Tests --


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (0.0, "0 сек"),
        (999.0, "1 сек"),
        (45_000.0, "45 сек"),
        (59_999.0, "1 мин"),
        (60_000.0, "1 мин"),
        (120_000.0, "2 мин"),
        (209_800.0, "3 мин 30 сек"),
        (3_599_999.0, "1 ч"),
        (3_600_000.0, "1 ч"),
        (3_605_000.0, "1 ч 5 сек"),
        (4_335_000.0, "1 ч 12 мин 15 сек"),
        (86_400_000.0, "1 д"),
        (184_200_000.0, "2 д 3 ч 10 мин"),
        (184_215_000.0, "2 д 3 ч 10 мин 15 сек"),
    ],
)
def test_format_duration_ms(duration_ms: float, expected: str) -> None:
    assert format_duration_ms(duration_ms) == expected


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("completed", {"summary": "Done."}),
        ("ready_to_commit", {"summary": "Ready."}),
        ("failed", {"error": "Boom."}),
    ],
)
def test_format_task_outcome_message_uses_human_duration(
    migrated_db: Path, status: str, extra: dict[str, str]
) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(title="Duration formatting", workspace="backend")
    outcome = TaskOutcome(
        task_id=task.id,
        status=status,
        duration_ms=209_800.0,
        **extra,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "⏱ **Время:** 3 мин 30 сек" in msg
    assert "209.8" not in msg


def test_format_task_outcome_message_success(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Add <sanitized> HTML support",
        workspace="backend",
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="completed",
        summary="Added & tested <b>sanitized</b> output.",
        changed_files=["src/app.py", "tests/test_app.py"],
        duration_ms=4500.0,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "✅" in msg
    assert f"Задача #{task.number} завершена!" in msg
    assert task.id not in msg
    # Markdown transport: content passes verbatim; no HTML entities appear.
    assert "Add <sanitized> HTML support" in msg
    assert "&lt;" not in msg
    assert "<b>sanitized</b>" in msg
    assert "`src/app.py`" in msg
    assert "⏱ **Время:** 5 сек" in msg

    kb = build_task_outcome_keyboard(task.id)
    assert kb.inline_keyboard[0][0].callback_data == f"select_task:{task.id}"


def test_format_task_outcome_message_failure(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Deploy worker",
        workspace="ops",
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="failed",
        error="ConnectionRefusedError: Port <8080> unreachable",
        duration_ms=1200.0,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "❌" in msg
    assert f"Задача #{task.number} завершилась с ошибкой!" in msg
    assert task.id not in msg
    assert "Port <8080> unreachable" in msg
    assert "&lt;" not in msg
    assert "```" in msg


@sync_test
async def test_telegram_notifier_async_send(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Notification test task",
        metadata={"chat_id": 12345678, "user_id": 12345678},
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="completed",
        summary="Done successfully.",
    )

    bot_mock = AsyncMock()
    notifier = TelegramNotifier(bot=bot_mock)

    await notifier.notify_async(task, outcome)

    # Notifications now go through the rich sender first.
    bot_mock.send_rich_message.assert_awaited_once()
    assert bot_mock.send_rich_message.await_args.kwargs["chat_id"] == 12345678
    kwargs = bot_mock.send_rich_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345678
    assert "✅" in kwargs["rich_message"].markdown


@sync_test
async def test_telegram_notifier_fallback_to_default_chat_id(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task with no chat_id",
        metadata={},
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="failed",
        error="Some error",
    )

    bot_mock = AsyncMock()
    notifier = TelegramNotifier(bot=bot_mock, default_chat_id=999888)

    await notifier.notify_async(task, outcome)

    bot_mock.send_rich_message.assert_awaited_once()
    kwargs = bot_mock.send_rich_message.await_args.kwargs
    assert kwargs["chat_id"] == 999888
    assert "❌" in kwargs["rich_message"].markdown


@sync_test
async def test_telegram_notifier_suppresses_bot_errors(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task failing bot",
        metadata={"chat_id": 12345},
    )
    outcome = TaskOutcome(task_id=task.id, status="completed")

    bot_mock = AsyncMock()
    # Both transports fail: rich sender and its plain fallback.
    bot_mock.send_rich_message.side_effect = RuntimeError("Telegram Network Timeout")
    bot_mock.send_message.side_effect = RuntimeError("Telegram Network Timeout")

    notifier = TelegramNotifier(bot=bot_mock)
    # Should not raise exception
    await notifier.notify_async(task, outcome)
    bot_mock.send_rich_message.assert_awaited_once()
    bot_mock.send_message.assert_awaited_once()


def test_task_worker_skills_and_devops_git_commit(migrated_db: Path, tmp_path: Path) -> None:
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="devops-agent",
        name="DevOps Agent",
        role="devops",
        system_prompt="Manage devops tasks.",
        runner_config={"tools": ["read", "bash"], "skills": ["portainer-operations"]},
        skills=[{"skill": "portainer-operations"}],
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Check container logs",
        status="in_progress",
        metadata={"task_type": "devops"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="devops-agent",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout='{"status": "completed", "changed_files": []}', stderr=""))
    with patch("subprocess.run", mock_run), patch(
        "kuzya.task_worker.TaskProcessRegistry.run_process",
        side_effect=lambda task_id, cmd, **kwargs: (
            mock_run(cmd, **kwargs),
            False,
        ),
    ):
        outcome = worker.step()

    assert outcome is not None
    assert mock_run.called
    # Check that role.json was written with skills and allow_git_commit
    exec_dirs = list((tmp_path / "artifacts").glob(f"task_{task.id}_*"))
    assert len(exec_dirs) == 1
    role_content = json.loads((exec_dirs[0] / "role.json").read_text())
    assert role_content.get("skills") == ["portainer-operations"]
    assert role_content.get("allow_git_commit") is True


def test_task_worker_code_review_cycle_invoked(migrated_db: Path, tmp_path: Path) -> None:
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Coding prompt.",
        runner_config={"tools": ["read", "write"]},
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Fix pagination bug",
        status="in_progress",
        metadata={"task_type": "coding", "requires_review": True},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    dev_output = json.dumps({"status": "completed", "changed_files": ["src/bot.py"], "summary_path": str(tmp_path / "dev_sum.md")})
    (tmp_path / "dev_sum.md").write_text("Fixed pagination.")

    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=dev_output, stderr=""))
    with patch("subprocess.run", mock_run), patch(
        "kuzya.task_worker.TaskProcessRegistry.run_process",
        side_effect=lambda task_id, cmd, **kwargs: (
            mock_run(cmd, **kwargs),
            False,
        ),
    ):
            with patch.object(worker, "_run_review_cycle") as mock_review:
                mock_review.return_value = {
                    "summary": "Fixed pagination.\n\n### 🔍 Code Review: ОДОБРЕНО ✅",
                    "changed_files": ["src/bot.py"],
                    "needs_user_input": False,
                }
                outcome = worker.step()

    assert outcome is not None
    assert mock_review.called
    assert "ОДОБРЕНО" in outcome.summary
    assert outcome.status == "ready_to_commit"
    reloaded = task_repo.get(task.id)
    assert reloaded.status == "ready_to_commit"


def test_format_task_outcome_message_ready_to_commit(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Implement commit confirmation",
        workspace="project_a",
        status="ready_to_commit",
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="ready_to_commit",
        summary="Code reviewed and passed all checks.",
        changed_files=["src/main.py", "src/utils.py"],
        duration_ms=3500.0,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "🔍" in msg
    assert f"Задача #{task.number} ожидает подтверждения!" in msg
    assert "`src/main.py`" in msg
    assert "`src/utils.py`" in msg
    assert "⏱ **Время:** 4 сек" in msg
    assert "Выберите действие ниже:" in msg

    kb = build_task_outcome_keyboard(task.id, status="ready_to_commit")
    # Row 1: commit_local, commit_push
    assert kb.inline_keyboard[0][0].text == "✅ Закоммитить"
    assert kb.inline_keyboard[0][0].callback_data == f"commit_local:{task.id}"
    assert kb.inline_keyboard[0][1].text == "🚀 Закоммитить и запушить"
    assert kb.inline_keyboard[0][1].callback_data == f"commit_push:{task.id}"
    # Row 2: rollback, discuss
    assert kb.inline_keyboard[1][0].text == "❌ Откатить"
    assert kb.inline_keyboard[1][0].callback_data == f"rollback:{task.id}"
    assert kb.inline_keyboard[1][1].callback_data == f"select_task:{task.id}"


@pytest.mark.parametrize("invalid_first", [False, True])
def test_task_worker_run_review_cycle_metrics_and_reload(
    migrated_db: Path, tmp_path: Path, invalid_first: bool
) -> None:
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="system-code-reviewer",
        name="System Reviewer",
        role="reviewer",
        system_prompt="Custom Reviewer Rules: Always check edge cases.",
        runner_config={"tools": ["read", "bash"]},
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Check review loop",
        status="in_progress",
        metadata={"task_type": "coding"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    review_out_file = tmp_path / "review_out.md"
    review_out_file.write_text('{"verdict": "approved", "findings": []}')

    exec_dir = tmp_path / "exec"
    exec_dir.mkdir(parents=True)
    role_file = exec_dir / "role.json"
    role_file.write_text("{}")
    state_dir = exec_dir / "state"
    state_dir.mkdir()

    mock_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"status": "completed", "summary_path": str(review_out_file)}),
        stderr="",
    )

    invalid_file = tmp_path / "invalid_review.md"
    invalid_file.write_text("ВЕРДИКТ: ОДОБРЕНО")
    invalid_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"status": "completed", "summary_path": str(invalid_file)}),
        stderr="",
    )
    responses = [invalid_proc, mock_proc] if invalid_first else [mock_proc]
    with patch("subprocess.run", side_effect=responses) as run_mock:
        res = worker._run_review_cycle(
            task=task,
            project_dir=tmp_path,
            changed_files=["file1.py"],
            developer_summary="Implemented feature.",
            exec_dir=exec_dir,
            role_file=role_file,
            state_dir=state_dir,
            child_env={},
        )

    assert run_mock.call_count == len(responses)
    assert res["needs_user_input"] is False
    assert "ОДОБРЕНО" in res["summary"]

    # The automatic verdict must leave the same machine-readable trace the
    # standalone review operation writes: the secretary instruction and the
    # commit gate both check metadata.review.approved.
    stored = task_repo.get(task.id)
    review = (stored.meta or {}).get("review") or {}
    assert review["approved"] is True
    assert review["verdict"] == "approved"
    assert review["round"] == 1
    assert review["source"] == "in_worker"
    assert review["report_path"]

    # Verify review metrics persisted
    round_json = tmp_path / "artifacts" / str(task.id) / "review" / "round_1.json"
    assert round_json.is_file()
    data = json.loads(round_json.read_text())
    assert data["round"] == 1
    assert data["format_attempts"] == len(responses)
    assert data["verdict"] == "APPROVED"
    assert data["approved"] is True
    assert "duration_ms" in data


def test_task_worker_malformed_review_fails_task(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    """A reviewer without an explicit verdict must fail the task, not approve."""
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Coding prompt.",
        runner_config={
            "mode": "success",
            "summary_text": "Implemented the fix.",
            "changed_files": ["src/x.py"],
        },
    )
    agent_repo.create(
        agent_id="system-code-reviewer",
        name="System Reviewer",
        role="reviewer",
        system_prompt="Review prompt.",
        runner_config={"mode": "malformed"},
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Review must fail visibly",
        status="in_progress",
        metadata={"task_type": "coding", "requires_review": True},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=fake_runner_argv,
        runtime_defaults=RuntimeDefaultsResolver(
            global_settings_path=tmp_path / "absent_settings.json"
        ),
    )

    outcome = worker.step()

    assert outcome is not None
    assert outcome.status == "failed"
    assert "no parseable JSON" in (outcome.error or "")
    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"
    failure = (
        tmp_path / "artifacts" / str(task.id) / "review" / "round_1_failure.json"
    )
    assert failure.is_file()
    diagnostics = json.loads(failure.read_text())
    assert diagnostics["reason"] == "malformed_runner_output"


def test_review_changes_requested_resumes_original_developer_session(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    """A changes_requested review must resume the real native session.

    This runs the real ``TaskExecutionWorker._run_review_cycle`` with the
    deterministic child Runner over ``subprocess`` instead of mocking the
    review cycle. The fake Runner enforces the same ``resume --session``
    requirement as the production CLI, so a missing session id fails the task.
    """
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    session_id = "11111111-1111-1111-1111-111111111111"
    argv_log = tmp_path / "dev_resume_argv.json"
    agent_repo.create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Coding prompt.",
        runner_config={
            "mode": "success",
            "summary_text": "Initial implementation.",
            "changed_files": ["src/x.py"],
            "stateful_session": True,
            "session_id_value": session_id,
            "argv_log": str(argv_log),
        },
    )
    agent_repo.create(
        agent_id="system-code-reviewer",
        name="System Reviewer",
        role="reviewer",
        system_prompt="Review prompt.",
        runner_config={
            "mode": "success",
            "summary_text": (
                '{"verdict": "changes_requested", "findings": [{"severity": "P1", "file": "src/x.py", "line": 1, "message": "src/x.py:1 -- null dereference"}]}'
            ),
        },
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Review then fix",
        status="in_progress",
        metadata={"task_type": "coding", "requires_review": True},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=fake_runner_argv,
        runtime_defaults=RuntimeDefaultsResolver(
            global_settings_path=tmp_path / "absent_settings.json"
        ),
    )

    outcome = worker.step()

    assert outcome is not None
    # Three rejected rounds escalate to the user; the important part is that
    # the task did not fail on an invalid resume command.
    assert outcome.status == "waiting_for_input", outcome.error
    reloaded = task_repo.get(task.id)
    assert reloaded.status == "waiting_for_input"

    logged = json.loads(argv_log.read_text())
    resume_argv = logged["argv"]
    assert resume_argv[1] == "resume"
    assert "--session" in resume_argv
    assert resume_argv[resume_argv.index("--session") + 1] == session_id
    assert "--state-dir" in resume_argv


def test_task_prompt_contains_description_additional_instructions_and_review_feedback(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """A resumed execution must receive the instructions that triggered it."""
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    first_instruction = {
        "text": "Add a regression test for the empty result.",
        "timestamp": "2025-01-02T03:04:05Z",
    }
    second_instruction = {
        "text": "Update the user-facing documentation.",
        "timestamp": "2025-01-03T04:05:06Z",
    }
    task = task_repo.create(
        title="Fix empty search results",
        description="Original task description: handle empty search results.",
        status="in_progress",
        metadata={
            "additional_instructions": [first_instruction, second_instruction],
            "review_feedback": "P1: src/search.py:42 -- handle the empty list.",
        },
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "completed"

    exec_dirs = list((tmp_path / "artifacts").glob(f"task_{task.id}_*"))
    assert len(exec_dirs) == 1
    task_text = (exec_dirs[0] / "task.md").read_text(encoding="utf-8")

    assert "Original task description: handle empty search results." in task_text
    assert first_instruction["text"] in task_text
    assert first_instruction["timestamp"] in task_text
    assert second_instruction["text"] in task_text
    assert second_instruction["timestamp"] in task_text
    assert "P1: src/search.py:42 -- handle the empty list." in task_text
    assert task_text.index(first_instruction["timestamp"]) < task_text.index(
        second_instruction["timestamp"]
    )


def test_review_fix_round_unions_changed_files(
    migrated_db: Path, tmp_path: Path
) -> None:
    """A fix round must not discard files changed by the first round."""
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="system-code-reviewer",
        name="System Reviewer",
        role="reviewer",
        system_prompt="Review prompt.",
        runner_config={"tools": ["read"]},
    )
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Review then fix",
        status="in_progress",
        metadata={"task_type": "coding", "requires_review": True},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    exec_dir = tmp_path / "exec"
    exec_dir.mkdir(parents=True)
    role_file = exec_dir / "role.json"
    role_file.write_text("{}")
    state_dir = exec_dir / "state"
    state_dir.mkdir()

    first_review = tmp_path / "review_round_1.md"
    first_review.write_text(
        '{"verdict": "changes_requested", "findings": [{"severity": "P1", "file": "src/old.py", "line": 1, "message": "src/old.py:1 -- missing test"}]}'
    )
    developer_fix = tmp_path / "developer_fix.md"
    developer_fix.write_text("Added the missing test.")
    second_review = tmp_path / "review_round_2.md"
    second_review.write_text('{"verdict": "approved", "findings": []}')

    review_one = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {"status": "completed", "summary_path": str(first_review)}
        ),
        stderr="",
    )
    developer_proc = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "status": "completed",
                "summary_path": str(developer_fix),
                "changed_files": ["src/fix.py", "src/old.py"],
            }
        ),
        stderr="",
    )
    review_two = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {"status": "completed", "summary_path": str(second_review)}
        ),
        stderr="",
    )

    with patch("subprocess.run", side_effect=[review_one, developer_proc, review_two]):
        result = worker._run_review_cycle(
            task=task,
            project_dir=tmp_path,
            changed_files=["src/old.py", "src/first.py"],
            developer_summary="Initial implementation.",
            exec_dir=exec_dir,
            role_file=role_file,
            state_dir=state_dir,
            child_env={},
        )

    assert result["needs_user_input"] is False
    assert result["changed_files"] == ["src/old.py", "src/first.py", "src/fix.py"]


def test_review_cycle_uses_reviewer_profile_engine(
    migrated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored ``runner_config.engine`` must reach the review executor."""
    monkeypatch.setattr(
        "kuzya.review_executor.shutil.which",
        lambda name: f"/fake/bin/{name}",
    )
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="system-code-reviewer",
        name="System Reviewer",
        role="reviewer",
        system_prompt="Review prompt.",
        runner_config={"engine": "codex", "effort": "high"},
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Engine selection",
        status="in_progress",
        metadata={"task_type": "coding", "requires_review": True},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="kuzya-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    exec_dir = tmp_path / "exec"
    exec_dir.mkdir(parents=True)
    role_file = exec_dir / "role.json"
    role_file.write_text("{}")
    state_dir = exec_dir / "state"
    state_dir.mkdir()

    review_reply = '{"verdict": "approved", "findings": []}'

    def fake_review_run(cmd, **kwargs):
        # codex delivers its report via the result file named in the prompt,
        # and the prompt itself arrives on stdin (``codex exec -``).
        if len(cmd) > 1 and cmd[1] == "exec":
            prompt = kwargs.get("input") or ""
            match = re.search(
                r"Write the final JSON object to the file (\S+?) \(", prompt
            )
            if match:
                result_file = Path(match.group(1))
                result_file.parent.mkdir(parents=True, exist_ok=True)
                result_file.write_text(review_reply, encoding="utf-8")
        return MagicMock(returncode=0, stdout=review_reply, stderr="")

    with patch("subprocess.run", side_effect=fake_review_run) as run_mock:
        result = worker._run_review_cycle(
            task=task,
            project_dir=tmp_path,
            changed_files=["file1.py"],
            developer_summary="Implemented feature.",
            exec_dir=exec_dir,
            role_file=role_file,
            state_dir=state_dir,
            child_env={},
        )

    assert result["needs_user_input"] is False
    cmd = run_mock.call_args.args[0]
    assert cmd[0] == "/fake/bin/codex"
    # File-based contract: codex exec with full access writes the JSON to an
    # exact result file instead of printing it on stdout.
    assert cmd[1] == "exec"
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    # The prompt is delivered on stdin, not as an argv element.
    assert cmd[-1] == "-"
    assert run_mock.call_args.kwargs["input"]
    metrics = json.loads(
        (
            tmp_path
            / "artifacts"
            / str(task.id)
            / "review"
            / "round_1.json"
        ).read_text(encoding="utf-8")
    )
    assert metrics["runtime"]["engine"] == "codex"


def _run_one_task_with_worker(
    tmp_path: Path,
    fake_runner_argv: list[str],
    db: Database,
    agent_id: str,
    *,
    task_metadata: dict[str, Any] | None = None,
) -> Any:
    task_repo = TaskRepository(db)
    task_repo.create(
        title="Mount controls probe",
        description="Check per-task mounts.",
        status="in_progress",
        workspace="test_ws",
        metadata=task_metadata or {},
    )
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(exist_ok=True)
    worker = TaskExecutionWorker(
        database=db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        workspace_map={"test_ws": str(workspace_dir)},
    )
    outcome = worker.step()
    assert outcome is not None and outcome.status == "completed"
    return task_repo.list()[0]


def test_task_metadata_mount_controls(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """project_read_only and readonly_paths in task metadata reach the sandbox role."""
    db, agent_id = setup_developer_agent
    extra_ro = tmp_path / "reference-repo"
    extra_ro.mkdir()

    task = _run_one_task_with_worker(
        tmp_path, fake_runner_argv, db, agent_id,
        task_metadata={
            "project_read_only": True,
            "readonly_paths": [str(extra_ro)],
        },
    )
    context = task.meta["execution_context"]
    assert context["project_read_only"] is True
    assert str(extra_ro) in context["readonly_paths"]

    workspace_notes = list((tmp_path / "artifacts").rglob("workspace.md"))
    assert workspace_notes, "workspace.md not written"
    note = workspace_notes[0].read_text(encoding="utf-8")
    assert "Project read-only: True" in note
    assert "Extra read-only mounts" in note and str(extra_ro) in note


def test_task_mount_controls_merge_with_profile(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """Task values merge on top of the profile defaults; explicit flag wins."""
    db, agent_id = setup_developer_agent
    profile_ro = tmp_path / "profile-ro"
    profile_ro.mkdir()
    extra_ro = tmp_path / "task-ro"
    extra_ro.mkdir()
    AgentProfileRepository(db).update(
        agent_id=agent_id,
        expected_revision=1,
        runner_config={
            "mode": "success",
            "summary_text": "ok",
            "project_read_only": False,
            "readonly_paths": [str(profile_ro)],
        },
    )

    task = _run_one_task_with_worker(
        tmp_path, fake_runner_argv, db, agent_id,
        task_metadata={
            "project_read_only": True,
            "readonly_paths": [str(extra_ro), str(profile_ro)],
        },
    )
    context = task.meta["execution_context"]
    assert context["project_read_only"] is True
    # merged and deduplicated, profile first
    assert context["readonly_paths"] == [str(profile_ro), str(extra_ro)]


def test_task_without_mount_metadata_keeps_profile_policy(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """No task metadata: the profile mount policy applies unchanged."""
    db, agent_id = setup_developer_agent
    profile_ro = tmp_path / "profile-ro"
    profile_ro.mkdir()
    AgentProfileRepository(db).update(
        agent_id=agent_id,
        expected_revision=1,
        runner_config={
            "mode": "success",
            "summary_text": "ok",
            "project_read_only": True,
            "readonly_paths": [str(profile_ro)],
        },
    )

    task = _run_one_task_with_worker(
        tmp_path, fake_runner_argv, db, agent_id, task_metadata={},
    )
    context = task.meta["execution_context"]
    assert context["project_read_only"] is True
    assert str(profile_ro) in context["readonly_paths"]


def test_changed_files_union_survives_junk_round(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """Cross-execution union with gitignore verdict (repository replaces
    the list per execution; a junk-only round must not wipe the changeset)."""
    import subprocess

    db, agent_id = setup_developer_agent
    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(".coverage\n", encoding="utf-8")
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )

    task_repo = TaskRepository(db)
    task_repo.create(
        title="Union probe", status="in_progress", workspace="ws",
        metadata={"requires_review": False},
    )

    def run_round(changed: list[str]) -> None:
        task_repo.update(task_repo.list()[0].id, status="in_progress")
        AgentProfileRepository(db).update(
            agent_id=agent_id,
            expected_revision=AgentProfileRepository(db).get_current(agent_id).revision,
            runner_config={
                "mode": "success",
                "summary_text": "round done",
                "changed_files": changed,
                "project": str(repo),
            },
        )
        worker = TaskExecutionWorker(
            database=db,
            artifact_root=tmp_path / "artifacts",
            agent_id=agent_id,
            runner_argv_prefix=fake_runner_argv,
            workspace_map={"ws": str(repo)},
        )
        outcome = worker.step()
        assert outcome is not None and outcome.status == "completed"

    run_round(["web/db/database.py", "web/main.py"])
    run_round([".coverage"])  # junk-only round, no real changes

    stored = task_repo.list()[0].meta["changed_files"]
    assert stored == ["web/db/database.py", "web/main.py"]


def _read_workspace_note(tmp_path: Path) -> str:
    notes = list((tmp_path / "artifacts").rglob("workspace.md"))
    assert notes, "workspace.md not written"
    return notes[-1].read_text(encoding="utf-8")


def _write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_workspace_note_has_testing_recipe_with_canonical_interpreter(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """The Testing section names the interpreter from the role runtime_paths.

    The runtime directory is mounted read-only at the same absolute path in
    every sandbox, so the recipe stays valid across sandboxes and HOMEs. The
    versioned binary is preferred over the ``python3`` alias in the same dir.
    """
    db, agent_id = setup_developer_agent
    runtime_dir = tmp_path / "uv-python"
    _write_executable(runtime_dir / "bin" / "python3")
    canonical = _write_executable(runtime_dir / "bin" / "python3.14")
    AgentProfileRepository(db).update(
        agent_id=agent_id,
        expected_revision=1,
        runner_config={
            "mode": "success",
            "summary_text": "ok",
            "runtime_paths": [str(runtime_dir)],
        },
    )

    _run_one_task_with_worker(tmp_path, fake_runner_argv, db, agent_id)
    note = _read_workspace_note(tmp_path)

    assert "## Testing" in note
    assert f"absolute path: {canonical}" in note
    # The recipe recreates the environment with the canonical interpreter and
    # then runs pytest with PYTHONPATH=src.
    assert f"{canonical} -m venv .venv" in note
    assert f"UV_PYTHON={canonical} uv sync --no-install-project" in note
    assert "PYTHONPATH=src .venv/bin/python -m pytest" in note
    assert ".venv/bin/python -V" in note
    assert "Do not commit `.venv`" in note


def test_workspace_note_uses_absolute_uv_from_runtime_paths(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """Bare ``uv`` is not on the sandbox PATH; the recipe must not use it.

    When uv is among the mounted runtime paths, every uv command in the
    Testing section carries its absolute path instead of the bare name that
    would fail with "command not found" on the first task.
    """
    db, agent_id = setup_developer_agent
    runtime_dir = tmp_path / "uv-python"
    _write_executable(runtime_dir / "bin" / "python3.14")
    uv_path = _write_executable(tmp_path / "uvbin" / "uv")
    AgentProfileRepository(db).update(
        agent_id=agent_id,
        expected_revision=1,
        runner_config={
            "mode": "success",
            "summary_text": "ok",
            "runtime_paths": [str(runtime_dir), str(uv_path)],
        },
    )

    _run_one_task_with_worker(tmp_path, fake_runner_argv, db, agent_id)
    note = _read_workspace_note(tmp_path)

    assert f"{uv_path} sync --no-install-project" in note
    assert " uv sync" not in note


def test_workspace_note_read_only_recipe_builds_env_under_tmp(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """A read-only project cannot lose its broken .venv, so build it in /tmp.

    The Runner mounts the project with ``--ro-bind`` when ``project_read_only``
    is set, so ``rm -rf .venv`` fails and the ``&&`` chain stops before an
    environment exists; the next reader keeps the same broken ``.venv``. The
    recipe must therefore build the environment in the writable ``/tmp`` tmpfs
    and run pytest through that environment instead of the project one.
    """
    db, agent_id = setup_developer_agent
    runtime_dir = tmp_path / "uv-python"
    canonical = _write_executable(runtime_dir / "bin" / "python3.14")
    AgentProfileRepository(db).update(
        agent_id=agent_id,
        expected_revision=1,
        runner_config={
            "mode": "success",
            "summary_text": "ok",
            "runtime_paths": [str(runtime_dir)],
        },
    )

    # A repository .venv that points at a HOME of a previous sandbox.
    workspace_dir = tmp_path / "workspace"
    broken_bin = workspace_dir / ".venv" / "bin"
    broken_bin.mkdir(parents=True)
    (broken_bin / "python").symlink_to(
        Path("/home/runner/nonexistent/bin/python3.14")
    )

    _run_one_task_with_worker(
        tmp_path,
        fake_runner_argv,
        db,
        agent_id,
        task_metadata={"project_read_only": True},
    )
    note = _read_workspace_note(tmp_path)

    assert "Project read-only: True" in note
    assert "## Testing" in note
    assert f"absolute path: {canonical}" in note
    # uv gets a writable environment directory and must not rewrite the lock.
    assert f"UV_PROJECT_ENVIRONMENT={READONLY_TEST_ENV_DIR}" in note
    assert f"UV_PYTHON={canonical}" in note
    assert "uv sync --frozen --no-install-project" in note
    # The non-uv fallback and the pytest run use the same /tmp environment.
    assert f"{canonical} -m venv {READONLY_TEST_ENV_DIR}" in note
    assert (
        f"PYTHONPATH=src {READONLY_TEST_ENV_DIR}/bin/python -m pytest" in note
    )
    # Never suggest a command that fails on a --ro-bind mount.
    assert "rm -rf .venv" not in note
    assert "PYTHONPATH=src .venv/bin/python -m pytest" not in note
    # The stale project environment is still flagged, just not used.
    assert ".venv/bin/python -V" in note


def test_workspace_note_omits_testing_section_without_python_runtime(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """No CPython in runtime_paths: the note must not advertise a wrong path."""
    db, agent_id = setup_developer_agent
    non_python = _write_executable(tmp_path / "runtime" / "node")
    AgentProfileRepository(db).update(
        agent_id=agent_id,
        expected_revision=1,
        runner_config={
            "mode": "success",
            "summary_text": "ok",
            "runtime_paths": [str(non_python)],
        },
    )

    _run_one_task_with_worker(tmp_path, fake_runner_argv, db, agent_id)
    note = _read_workspace_note(tmp_path)

    assert "## Testing" not in note
    assert "Canonical Python interpreter" not in note


def test_resolve_python_interpreter_skips_broken_and_non_python_entries(
    tmp_path: Path,
) -> None:
    """Broken symlinks and non-CPython executables never become the recipe path."""
    runtime_dir = tmp_path / "uv-python"
    broken = runtime_dir / "bin" / "python3.14"
    broken.parent.mkdir(parents=True)
    broken.symlink_to(tmp_path / "missing-python")
    _write_executable(runtime_dir / "bin" / "node")

    assert _resolve_python_interpreter([str(runtime_dir)]) is None
    assert _resolve_python_interpreter([str(tmp_path / "absent")]) is None
    assert _resolve_python_interpreter(None) is None
    assert _resolve_python_interpreter("not-a-list") is None

    explicit = _write_executable(tmp_path / "python3.14")
    assert _resolve_python_interpreter([str(explicit)]) == str(explicit)


def test_resolve_python_interpreter_normalizes_symlinks_and_tilde(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sandbox mounts the resolved target, never the alias.

    A symlinked runtime directory or a ``~``-relative entry must resolve to
    the real mounted path before it is advertised in workspace.md, or the
    recipe names a path that does not exist inside the sandbox.
    """
    real = _write_executable(tmp_path / "real" / "bin" / "python3.14")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)

    assert _resolve_python_interpreter([str(link)]) == str(real)

    home = tmp_path / "home"
    home_python = _write_executable(home / ".local" / "py" / "bin" / "python3.14")
    monkeypatch.setenv("HOME", str(home))

    assert _resolve_python_interpreter(["~/.local/py"]) == str(home_python)


def test_resolve_runtime_executable_finds_named_entry(
    tmp_path: Path,
) -> None:
    """uv is reachable only through its mount point, not the sandbox PATH."""
    from kuzya.task_worker import _resolve_runtime_executable

    uv = _write_executable(tmp_path / "uvbin" / "uv")

    assert _resolve_runtime_executable([str(uv)], "uv") == str(uv)
    assert _resolve_runtime_executable([str(tmp_path / "uvbin")], "uv") == str(uv)
    assert _resolve_runtime_executable([str(tmp_path)], "uv") is None
    assert _resolve_runtime_executable(None, "uv") is None
