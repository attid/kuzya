"""Focused tests for the Telegram task-context handlers."""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from aiogram.types import CallbackQuery, Chat, Message, User

from kuzya.telegram.config import TelegramConfig
from kuzya.telegram.handlers import (
    on_category_switch,
    on_switch_conversation,
    on_switch_task,
    on_talk_task,
    on_user_message,
)
from kuzya.telegram.keyboards import OTHER_EXCLUDED_STATUSES
from kuzya.telegram.state import UserStateStorage
from kuzya.telegram.worker import QueueProcessor


def _user(user_id: int = 12345) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _message(*, text: str, user: User) -> Message:
    message = MagicMock(spec=Message)
    message.message_id = 1
    message.from_user = user
    message.chat = Chat(id=user.id, type="private")
    message.text = text
    message.voice = None
    message.photo = None
    message.document = None
    message.caption = None
    # aiogram Message is a pydantic model, so spec'd mocks cannot see field
    # names; optional fields the handlers rely on are set explicitly.
    message.reply_to_message = None
    message.answer = AsyncMock()
    return message


def _callback(data: str, *, user: User) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = user
    callback.message = _message(text="", user=user)
    callback.answer = AsyncMock()
    return callback


def test_category_switch_other_uses_status_exclusion() -> None:
    """The Other tab asks the API for everything outside the other tabs."""
    user = _user()
    callback = _callback("cat:other:1", user=user)
    callback.message.edit_reply_markup = AsyncMock()
    client = MagicMock()
    client.list_tasks = AsyncMock(return_value=[])
    client.get_task_counts = AsyncMock(return_value={"paused": 1})

    asyncio.run(on_category_switch(callback, client=client))

    client.list_tasks.assert_awaited_once()
    kwargs = client.list_tasks.await_args.kwargs
    assert "status" not in kwargs
    assert set(kwargs["status_not_in"]) == set(OTHER_EXCLUDED_STATUSES)
    markup = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    other_tab = next(
        button.text
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data == "cat:other:1"
    )
    assert other_tab == "• 🗂 Прочее 1 •"


def test_category_switch_regular_tab_still_filters_one_status() -> None:
    user = _user()
    callback = _callback("cat:incoming:2", user=user)
    callback.message.edit_reply_markup = AsyncMock()
    client = MagicMock()
    client.list_tasks = AsyncMock(return_value=[])
    client.get_task_counts = AsyncMock(return_value={})

    asyncio.run(on_category_switch(callback, client=client))

    client.list_tasks.assert_awaited_once_with(status="incoming")


def test_talk_task_binds_the_existing_task_conversation(
    tmp_path: pathlib.Path,
) -> None:
    """The discuss-with-secretary action binds the task's conversation."""
    user = _user()
    callback = _callback("talk_task:task-123", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-123",
            "number": 42,
            "title": "Проверить деплой",
            "metadata": {"conversation_id": "conv-123"},
        }
    )

    asyncio.run(
        on_talk_task(callback, storage=storage, client=client)
    )

    state = storage.get_user_state(user.id)
    assert state.mode == "task"
    assert state.active_task_id == "task-123"
    assert state.active_conversation_id == "conv-123"
    prompt = callback.message.answer.call_args.args[0]
    assert "Проверить деплой" in prompt


def test_task_mode_message_is_forwarded_with_task_id(
    tmp_path: pathlib.Path,
) -> None:
    """The handler keeps context_task_id so the worker can enrich the turn."""
    user = _user()
    message = _message(text="че там с задачей?", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(
        user.id, "task", task_id="task-123", conversation_id="conv-123"
    )
    client = MagicMock()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-123",
            "number": 42,
            "title": "Проверить деплой",
            "status": "in_progress",
            "metadata": {"conversation_id": "conv-123"},
        }
    )
    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()
    config = TelegramConfig(
        bot_token="fake", allowed_user_id=user.id, media_dir=tmp_path
    )

    asyncio.run(
        on_user_message(
            message,
            storage=storage,
            client=client,
            queue_processor=processor,
            config=config,
        )
    )

    processor.submit_message.assert_awaited_once()
    kwargs = processor.submit_message.call_args.kwargs
    assert kwargs["context_mode"] == "task"
    assert kwargs["context_task_id"] == "task-123"
    assert kwargs["context_conversation_id"] == "conv-123"
    assert kwargs["text"] == "че там с задачей?"


def _reply_message(*, text: str, user: User, bot_id: int, replied_from_bot: bool):
    """A message replying to another Telegram message."""
    message = _message(text=text, user=user)
    message.bot = MagicMock()
    message.bot.id = bot_id
    reply_to = MagicMock()
    reply_to.message_id = 555
    reply_to.from_user = User(
        id=bot_id if replied_from_bot else user.id,
        is_bot=replied_from_bot,
        first_name="Whoever",
    )
    message.reply_to_message = reply_to
    return message


def test_reply_routes_to_original_conversation_not_waiting_task(
    tmp_path: pathlib.Path,
) -> None:
    """The №41 scenario: a Reply into dialog A must not resume waiting task B."""
    user = _user()
    message = _reply_message(
        text="вот ответ по другому диалогу", user=user, bot_id=999, replied_from_bot=True
    )
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(
        user.id, "task", task_id="task-b", conversation_id="conv-b"
    )
    client = MagicMock()
    # Task B is the active one and waits for a clarification answer.
    client.get_task = AsyncMock(
        return_value={
            "id": "task-b",
            "number": 41,
            "title": "Задача B",
            "status": "waiting_for_input",
            "metadata": {
                "conversation_id": "conv-b",
                "clarification": {"question": "Что делать?"},
            },
        }
    )
    client.patch_task = AsyncMock()
    # The replied-to message belongs to an unrelated dialog A.
    client.get_telegram_link = AsyncMock(
        return_value={
            "chat_id": user.id,
            "telegram_message_id": 555,
            "conversation_id": "conv-a",
            "task_id": None,
            "origin": "bot",
        }
    )
    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()
    config = TelegramConfig(
        bot_token="fake", allowed_user_id=user.id, media_dir=tmp_path
    )

    asyncio.run(
        on_user_message(
            message,
            storage=storage,
            client=client,
            queue_processor=processor,
            config=config,
        )
    )

    # The reply went to dialog A, not to waiting task B…
    processor.submit_message.assert_awaited_once()
    kwargs = processor.submit_message.call_args.kwargs
    assert kwargs["context_conversation_id"] == "conv-a"
    assert kwargs["context_mode"] == "talk"
    assert kwargs["context_task_id"] is None
    # …task B was not resumed…
    client.patch_task.assert_not_awaited()
    # …and the active context still points at B for plain messages.
    state = storage.get_user_state(user.id)
    assert state.mode == "task"
    assert state.active_task_id == "task-b"
    assert state.active_conversation_id == "conv-b"


def test_reply_routes_to_task_bound_conversation(tmp_path: pathlib.Path) -> None:
    user = _user()
    message = _reply_message(
        text="продолжаем", user=user, bot_id=999, replied_from_bot=True
    )
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(user.id, "talk", conversation_id="conv-b")
    client = MagicMock()
    client.get_telegram_link = AsyncMock(
        return_value={
            "chat_id": user.id,
            "telegram_message_id": 555,
            "conversation_id": "conv-a",
            "task_id": "task-a",
            "origin": "bot",
        }
    )
    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()
    config = TelegramConfig(
        bot_token="fake", allowed_user_id=user.id, media_dir=tmp_path
    )

    asyncio.run(
        on_user_message(
            message,
            storage=storage,
            client=client,
            queue_processor=processor,
            config=config,
        )
    )
    kwargs = processor.submit_message.call_args.kwargs
    assert kwargs["context_conversation_id"] == "conv-a"
    assert kwargs["context_mode"] == "task"
    assert kwargs["context_task_id"] == "task-a"


def test_reply_to_bot_message_with_lost_link_explains(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    message = _reply_message(
        text="ответ", user=user, bot_id=999, replied_from_bot=True
    )
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(user.id, "talk", conversation_id="conv-b")
    client = MagicMock()
    client.get_telegram_link = AsyncMock(return_value=None)
    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()
    config = TelegramConfig(
        bot_token="fake", allowed_user_id=user.id, media_dir=tmp_path
    )

    asyncio.run(
        on_user_message(
            message,
            storage=storage,
            client=client,
            queue_processor=processor,
            config=config,
        )
    )
    # Nothing was sent anywhere silently; the user got an explanation.
    processor.submit_message.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "исходный диалог" in message.answer.call_args.args[0]


def test_reply_to_unlinked_human_message_continues_ordinary_flow(
    tmp_path: pathlib.Path,
) -> None:
    """A Reply to a human message without a link is ordinary input."""
    user = _user()
    # Replying to his own earlier message, no durable link for it.
    message = _reply_message(
        text="ответ на уточнение", user=user, bot_id=999, replied_from_bot=False
    )
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(
        user.id, "task", task_id="task-b", conversation_id="conv-b"
    )
    client = MagicMock()
    client.get_telegram_link = AsyncMock(return_value=None)
    client.get_task = AsyncMock(
        return_value={
            "id": "task-b",
            "number": 41,
            "title": "Задача B",
            "status": "waiting_for_input",
            "metadata": {
                "conversation_id": "conv-b",
                "clarification": {"question": "Что делать?"},
            },
        }
    )
    client.patch_task = AsyncMock()
    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()
    config = TelegramConfig(
        bot_token="fake", allowed_user_id=user.id, media_dir=tmp_path
    )

    asyncio.run(
        on_user_message(
            message,
            storage=storage,
            client=client,
            queue_processor=processor,
            config=config,
        )
    )
    # The ordinary waiting-for-input interception applies: task B resumes.
    client.patch_task.assert_awaited_once()
    processor.submit_message.assert_not_awaited()


def test_switch_conversation_button_sets_active_context(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback(f"switch_conversation:conv-a", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(user.id, "talk", conversation_id="conv-b")
    client = MagicMock()
    client.get_conversation = AsyncMock(return_value={"id": "conv-a"})

    asyncio.run(
        on_switch_conversation(callback, storage=storage, client=client)
    )

    state = storage.get_user_state(user.id)
    assert state.mode == "talk"
    assert state.active_conversation_id == "conv-a"
    assert storage.get_user_state(user.id).talk_conversation_id == "conv-a"
    prompt = callback.message.answer.call_args.args[0]
    assert "#conv-a" in prompt

    # A repeated press is safe and does not create a new dialog.
    callback2 = _callback("switch_conversation:conv-a", user=user)
    callback2.message = _message(text="", user=user)
    client.get_conversation = AsyncMock(return_value={"id": "conv-a"})
    asyncio.run(
        on_switch_conversation(callback2, storage=storage, client=client)
    )
    assert storage.get_user_state(user.id).active_conversation_id == "conv-a"


def test_switch_conversation_button_missing_dialog_keeps_context(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("switch_conversation:conv-gone", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(user.id, "talk", conversation_id="conv-b")
    client = MagicMock()
    client.get_conversation = AsyncMock(side_effect=RuntimeError("404"))

    asyncio.run(
        on_switch_conversation(callback, storage=storage, client=client)
    )
    state = storage.get_user_state(user.id)
    assert state.active_conversation_id == "conv-b"
    callback.answer.assert_awaited_once()


def test_switch_task_button_binds_task_context(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("switch_task:task-a", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(user.id, "talk", conversation_id="conv-b")
    client = MagicMock()
    client.patch_task = AsyncMock()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-a",
            "number": 7,
            "title": "Задача A",
            "status": "in_progress",
            "metadata": {"conversation_id": "conv-a"},
        }
    )

    asyncio.run(on_switch_task(callback, storage=storage, client=client))

    state = storage.get_user_state(user.id)
    assert state.mode == "task"
    assert state.active_task_id == "task-a"
    assert state.active_conversation_id == "conv-a"
    prompt = callback.message.answer.call_args.args[0]
    assert "#conv-a" in prompt


# -- commit buttons honour the review gate -------------------------------


def _commit_setup(task_meta: dict, data: str):
    from unittest.mock import patch

    user = _user()
    callback = _callback(data, user=user)
    callback.message.edit_text = AsyncMock()
    client = MagicMock()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-1",
            "number": 7,
            "title": "T",
            "workspace": "/ws/a",
            "metadata": task_meta,
        }
    )
    client.patch_task = AsyncMock()
    config = MagicMock()
    config.default_workspace = "/ws/default"
    return callback, client, config


def test_commit_local_blocked_without_approved_review() -> None:
    from kuzya.telegram.handlers import on_commit_local

    callback, client, config = _commit_setup(
        {"requires_review": True, "review": {"approved": False}},
        "commit_local:task-1",
    )

    asyncio.run(on_commit_local(callback, client=client, config=config))

    call = callback.message.edit_text.await_args
    assert "ревью не одобрено" in call.args[0]
    assert (
        call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == "commit_local_force:task-1"
    )
    client.patch_task.assert_not_awaited()


def test_commit_local_force_commits_past_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kuzya.telegram.handlers import on_commit_local_force

    callback, client, config = _commit_setup(
        {"requires_review": True}, "commit_local_force:task-1"
    )
    callback.message.answer = AsyncMock(return_value=_commit_status_message())
    monkeypatch.setattr(
        "kuzya.telegram.handlers._redraw_task_card", AsyncMock()
    )
    engine = MagicMock()
    engine.commit_local.return_value = MagicMock(
        success=True, message="ok", branch="main", commit_hash="abc"
    )
    monkeypatch.setattr(
        "kuzya.telegram.handlers.GitEngine", MagicMock(return_value=engine)
    )

    asyncio.run(on_commit_local_force(callback, client=client, config=config))

    engine.commit_local.assert_called_once()
    client.patch_task.assert_awaited_once_with("task-1", status="done")


def test_commit_local_without_requires_review_skips_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kuzya.telegram.handlers import on_commit_local

    callback, client, config = _commit_setup(
        {"requires_review": False}, "commit_local:task-1"
    )
    callback.message.answer = AsyncMock(return_value=_commit_status_message())
    monkeypatch.setattr(
        "kuzya.telegram.handlers._redraw_task_card", AsyncMock()
    )
    engine = MagicMock()
    engine.commit_local.return_value = MagicMock(
        success=True, message="ok", branch="main", commit_hash="abc"
    )
    monkeypatch.setattr(
        "kuzya.telegram.handlers.GitEngine", MagicMock(return_value=engine)
    )

    asyncio.run(on_commit_local(callback, client=client, config=config))

    engine.commit_local.assert_called_once()
    client.patch_task.assert_awaited_once_with("task-1", status="done")


def _commit_status_message() -> MagicMock:
    status = MagicMock()
    status.edit_text = AsyncMock()
    return status


def test_commit_local_reports_in_status_message_and_keeps_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kuzya.telegram.handlers import on_commit_local

    callback, client, config = _commit_setup(
        {"requires_review": False}, "commit_local:task-1"
    )
    status_msg = _commit_status_message()
    callback.message.answer = AsyncMock(return_value=status_msg)
    engine = MagicMock()
    engine.commit_local.return_value = MagicMock(
        success=True, message="ok", branch="main", commit_hash="abc"
    )
    monkeypatch.setattr(
        "kuzya.telegram.handlers.GitEngine", MagicMock(return_value=engine)
    )
    redraw = AsyncMock()
    monkeypatch.setattr("kuzya.telegram.handlers._redraw_task_card", redraw)

    asyncio.run(on_commit_local(callback, client=client, config=config))

    # The outcome lands in its own status message...
    callback.message.answer.assert_awaited_once()
    assert "Локальный коммит" in callback.message.answer.await_args.args[0]
    assert "закоммичена" in status_msg.edit_text.await_args.args[0]
    # ...and the task card is redrawn in place, not destroyed.
    redraw.assert_awaited_once()
    client.patch_task.assert_awaited_once_with("task-1", status="done")


def test_commit_local_failure_edits_the_same_status_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kuzya.telegram.handlers import on_commit_local

    callback, client, config = _commit_setup(
        {"requires_review": False}, "commit_local:task-1"
    )
    status_msg = _commit_status_message()
    callback.message.answer = AsyncMock(return_value=status_msg)
    engine = MagicMock()
    engine.commit_local.return_value = MagicMock(
        success=False, message="Ни один путь не годен для стейджинга"
    )
    monkeypatch.setattr(
        "kuzya.telegram.handlers.GitEngine", MagicMock(return_value=engine)
    )

    asyncio.run(on_commit_local(callback, client=client, config=config))

    assert "Ошибка" in status_msg.edit_text.await_args.args[0]
    client.patch_task.assert_not_awaited()


def test_task_workspace_prefers_execution_context_host_path() -> None:
    from kuzya.telegram.handlers import _task_workspace

    config = MagicMock()
    config.default_workspace = "/ws/default"

    explicit = _task_workspace(
        {"workspace": "/ws/explicit",
         "metadata": {"execution_context": {"host_path": "/ws/agent"}}},
        config,
    )
    assert explicit == "/ws/explicit"

    inherited = _task_workspace(
        {"workspace": None,
         "metadata": {"execution_context": {"host_path": "/ws/agent"}}},
        config,
    )
    assert inherited == "/ws/agent"

    fallback = _task_workspace({"workspace": None, "metadata": {}}, config)
    assert fallback == "/ws/default"
