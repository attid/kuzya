"""Unit tests for GitEngine: safe commit, rollback, and CI monitoring."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kuzya.git_engine import GitEngine, GitEngineResult
from kuzya.operation_handlers import git_commit_push_handler
from kuzya.operation_worker import (
    OPERATION_KIND_GIT_COMMIT_PUSH,
    OperationContext,
    OperationInstanceContext,
)
from kuzya.storage import Database, TaskRepository


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary initialized git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=repo, check=True)

    # Initial file
    init_file = repo / "README.md"
    init_file.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_commit_local_explicit_files(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)

    # Modify an existing file and create a new file
    (temp_git_repo / "README.md").write_text("# Updated Repo\n")
    new_file = temp_git_repo / "src" / "feature.py"
    new_file.parent.mkdir(parents=True)
    new_file.write_text("print('feature')\n")

    # Untracked sensitive file that must NOT be staged
    sensitive_file = temp_git_repo / "secret.env"
    sensitive_file.write_text("SECRET=12345\n")

    res = engine.commit_local(
        workspace=temp_git_repo,
        task_id="task-42",
        title="Add new feature",
        summary="Implemented feature and updated README",
        changed_files=["README.md", "src/feature.py"],
    )

    assert res.success is True
    assert res.commit_hash is not None
    assert len(res.commit_hash) == 40
    assert res.action == "commit_local"

    # Verify commit log
    log = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=temp_git_repo, capture_output=True, text=True).stdout
    assert "feat(#task-42): Add new feature" in log
    assert "Implemented feature and updated README" in log

    # Verify sensitive file was NOT committed
    status = subprocess.run(["git", "status", "--porcelain"], cwd=temp_git_repo, capture_output=True, text=True).stdout
    assert "?? secret.env" in status


def test_rollback_discards_changes(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)

    # Modify README and add new untracked file
    (temp_git_repo / "README.md").write_text("# Corrupted Content\n")
    new_file = temp_git_repo / "temp_experiment.py"
    new_file.write_text("# Temp\n")

    res = engine.rollback(
        workspace=temp_git_repo,
        changed_files=["README.md", "temp_experiment.py"],
    )

    assert res.success is True
    assert (temp_git_repo / "README.md").read_text() == "# Test Repo\n"
    assert not new_file.exists()


def test_commit_and_push_with_ci_success(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)
    (temp_git_repo / "README.md").write_text("# Pushed\n")

    mock_run = MagicMock()
    # 1. git push
    # 2. gh run list
    # 3. gh run watch
    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout='[{"databaseId": 998877, "status": "completed", "conclusion": "success"}]', stderr="")
        elif "watch" in args and "gh" in args:
            return MagicMock(returncode=0, stdout="Run succeeded", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-99",
            title="Push test",
            changed_files=["README.md"],
            watch_ci=True,
        )

    assert res.success is True
    assert res.ci_status == "success"
    assert "GitHub Actions успешно пройдены" in res.message


def test_commit_and_push_without_workflows_succeeds(temp_git_repo: Path) -> None:
    """A successful push to a repository without Actions workflows is not a failure."""
    engine = GitEngine(default_workspace=temp_git_repo)
    (temp_git_repo / "README.md").write_text("# No CI\n")

    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-101",
            title="Push without CI",
            changed_files=["README.md"],
            watch_ci=True,
        )

    assert res.success is True
    assert res.ci_status == "not_found"
    assert "CI не требуется" in res.message
    assert res.commit_hash is not None


def test_commit_and_push_without_workflows_can_require_ci(temp_git_repo: Path) -> None:
    """The opt-in strict mode still reports missing CI as a failure."""
    engine = GitEngine(default_workspace=temp_git_repo)
    (temp_git_repo / "README.md").write_text("# Strict CI\n")

    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-102",
            title="Push with required CI",
            changed_files=["README.md"],
            watch_ci=True,
            require_ci=True,
        )

    assert res.success is False
    assert res.ci_status == "not_found"
    assert "CI обязателен" in res.message


def test_engine_level_require_ci_setting_is_applied(temp_git_repo: Path) -> None:
    """The engine-level setting makes CI strict without a per-call flag."""
    engine = GitEngine(default_workspace=temp_git_repo, require_ci=True)
    (temp_git_repo / "README.md").write_text("# Engine strict CI\n")

    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-103",
            title="Engine-level required CI",
            changed_files=["README.md"],
            watch_ci=True,
        )

    assert res.success is False
    assert res.ci_status == "not_found"


def test_commit_and_push_ci_failure(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)
    (temp_git_repo / "README.md").write_text("# Broken\n")

    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout='[{"databaseId": 12345, "status": "completed", "conclusion": "failure"}]', stderr="")
        elif "watch" in args and "gh" in args:
            return MagicMock(returncode=1, stdout="", stderr="Watch failed")
        elif "view" in args and "--log-failed" in args:
            return MagicMock(returncode=0, stdout="Error: tests failed at line 42", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-100",
            title="Push with failure",
            changed_files=["README.md"],
            watch_ci=True,
        )

    assert res.success is False
    assert res.ci_status == "failure"
    assert "tests failed at line 42" in (res.ci_logs or "")


def test_commit_push_handler_marks_task_done_without_workflows(
    migrated_db: Path,
    temp_git_repo: Path,
) -> None:
    """Missing CI must not leave the task stuck in ``ready_to_commit``."""
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        task = repository.create(
            title="Push without CI",
            status="ready_to_commit",
            workspace=str(temp_git_repo),
            metadata={"changed_files": ["README.md"]},
        )
        (temp_git_repo / "README.md").write_text("# Handler no CI\n")
        instance = OperationInstanceContext(
            database=database,
            artifact_root=temp_git_repo / "operations",
            default_workspace=temp_git_repo,
        )
        context = OperationContext(
            operation_id="op-no-ci",
            kind=OPERATION_KIND_GIT_COMMIT_PUSH,
            target_id=task.id,
            input={"push": True},
            attempt_count=1,
            metadata={},
            instance=instance,
        )

        def fake_subprocess_run(args, **kwargs):
            if "push" in args:
                return MagicMock(returncode=0, stdout="pushed", stderr="")
            elif "list" in args and "gh" in args:
                return MagicMock(returncode=0, stdout="[]", stderr="")
            kwargs.setdefault("capture_output", True)
            kwargs.setdefault("text", True)
            return subprocess.run(args, **kwargs)

        with patch.object(GitEngine, "_run_cmd", side_effect=fake_subprocess_run):
            result = git_commit_push_handler(context)

        assert result["task_status"] == "done"
        assert result["ci_status"] == "not_found"
        assert result["pushed"] is True
        assert repository.get(task.id).status == "done"
    finally:
        database.dispose()


def test_commit_local_skips_ignored_and_missing_paths(temp_git_repo: Path) -> None:
    """Git's own verdict decides: ignored or nonexistent paths are skipped,
    the rest commit; one junk entry must not abort the whole commit."""
    (temp_git_repo / ".gitignore").write_text(".coverage\n", encoding="utf-8")
    good = temp_git_repo / "src_file.py"
    good.write_text("print('hi')\n", encoding="utf-8")

    engine = GitEngine(default_workspace=temp_git_repo)
    result = engine.commit_local(
        workspace=temp_git_repo,
        task_id="41",
        title="junk-tolerant staging",
        changed_files=[".coverage", "src_file.py", "missing_file.py"],
    )
    assert result.success, result.message
    assert ".coverage" in result.message and "missing_file.py" in result.message
    committed = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=temp_git_repo, check=True, capture_output=True, text=True,
    ).stdout.split()
    assert committed == ["src_file.py"]


def test_commit_local_all_skipped_is_clear_failure_no_fallback(temp_git_repo: Path) -> None:
    """When every explicitly listed path is git-refused, fail loudly —
    never silently degrade into staging the whole porcelain status."""
    (temp_git_repo / ".gitignore").write_text("junk.bin\n", encoding="utf-8")
    (temp_git_repo / "junk.bin").write_text("x", encoding="utf-8")
    untracked_other = temp_git_repo / "other.txt"
    untracked_other.write_text("must not be committed\n", encoding="utf-8")

    engine = GitEngine(default_workspace=temp_git_repo)
    result = engine.commit_local(
        workspace=temp_git_repo,
        task_id="41",
        title="all junk",
        changed_files=["junk.bin", "nowhere.py"],
    )
    assert not result.success
    assert "junk.bin" in result.message and "nowhere.py" in result.message
    # The untracked fallback target stays untouched.
    assert untracked_other.exists()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=temp_git_repo,
        check=True, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert len(log) == 1  # only the initial commit


def _mini_repo(path, *, with_commit=True):
    import subprocess as sp

    path.mkdir(parents=True, exist_ok=True)
    sp.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    if with_commit:
        (path / "README.md").write_text("hi")
        sp.run(["git", "add", "."], cwd=path, check=True)
        sp.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def test_status_summary_reports_clean_dirty_and_untracked(tmp_path):
    from kuzya.git_engine import GitEngine

    repo = _mini_repo(tmp_path / "repo")
    engine = GitEngine(default_workspace=repo)

    clean = engine.status_summary(repo)
    assert clean["branch"] == "main"
    assert clean["clean"] is True
    assert clean["modified"] == [] and clean["untracked"] == []
    assert clean["last_commit"]["subject"] == "init"

    (repo / "README.md").write_text("changed")
    (repo / "new.txt").write_text("untracked")
    dirty = engine.status_summary(repo)
    assert dirty["clean"] is False
    assert dirty["modified"] == ["README.md"]
    assert dirty["untracked"] == ["new.txt"]
    assert dirty["last_commit"]["hash"] == clean["last_commit"]["hash"]


def test_status_summary_raises_outside_a_repository(tmp_path):
    import pytest

    from kuzya.git_engine import GitEngine

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError):
        GitEngine(default_workspace=plain).status_summary(plain)
