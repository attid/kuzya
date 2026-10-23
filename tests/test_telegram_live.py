"""Telegram live monitor command, callback and keyboard tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User

from kuzya.telegram.handlers import (
    MONITOR_FULL_DETAIL_LIMIT,
    cmd_live,
    format_execution_monitor,
    on_action_live,
)
from kuzya.telegram.keyboards import (
    execution_monitor_keyboard,
    task_categories_keyboard,
)
from kuzya.telegram.messenger import RICH_TEXT_LIMIT


def _user() -> User:
    return User(id=12345, is_bot=False, first_name="Test")


def _message(*, user: User) -> Message:
    message = MagicMock(spec=Message)
    message.message_id = 1
    message.from_user = user
    message.chat = Chat(id=user.id, type="private")
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    return message


def _callback(data: str, *, user: User) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = user
    callback.message = _message(user=user)
    callback.answer = AsyncMock()
    return callback


def _payload(*, tools: int) -> dict:
    return {
        "generated_at": "2026-01-01T12:34:56Z",
        "running": [
            {
                "kind": "analysis",
                "entry_type": "operation",
                "operation_kind": "analysis",
                "kind_label": "Анализ",
                "target_number": 76,
                "target_title": "Проверить деплой",
                "agent_id": "analyzer",
                "elapsed": 360,
                "session_available": True,
                "tools_used": tools,
                "tokens": {"totalTokens": 138000},
                "cost": 0.13,
                "last_activity_sec": 3,
                "current_tool": "bash",
            }
        ],
        "queues": {
            "turns": 2,
            "operations": 1,
            "subscriptions": 0,
            "tasks": 1,
        },
    }


def _running_payload(
    count: int,
    *,
    title_padding: int = 0,
    agent_padding: int = 0,
    tool_padding: int = 0,
) -> dict:
    """A monitor snapshot with many running tasks and optional long fields."""
    running = []
    for number in range(1, count + 1):
        running.append(
            {
                "kind": "task",
                "entry_type": "task",
                "number": number,
                "title": f"Задача {number}" + ("Ж" * title_padding),
                "agent_id": f"agent-{number}" + ("A" * agent_padding),
                "elapsed": 60,
                "session_available": True,
                "tools_used": number,
                "tokens": {"totalTokens": 1000},
                "cost": 0.01,
                "last_activity_sec": 2,
                "current_tool": "bash" + ("T" * tool_padding),
            }
        )
    return {
        "generated_at": "2026-01-01T12:34:56Z",
        "running": running,
        "queues": {"turns": 0, "operations": 0, "subscriptions": 0, "tasks": 0},
    }


def _rich_sender_rejecting_over_limit(**kwargs):
    """Transport mock that rejects a payload above the rich capacity."""
    markdown = kwargs["rich_message"].markdown
    if len(markdown) > RICH_TEXT_LIMIT:
        raise RuntimeError("MESSAGE_TOO_LONG")
    return MagicMock(message_id=20)


def test_category_keyboard_has_live_button() -> None:
    keyboard = task_categories_keyboard()
    live_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data == "action:live"
    ]
    assert [button.text for button in live_buttons] == ["⚡ Исполнители"]


def test_refresh_keyboard_reuses_the_live_callback() -> None:
    keyboard = execution_monitor_keyboard()
    [row] = keyboard.inline_keyboard
    [button] = row
    assert button.text == "🔄 Обновить"
    assert button.callback_data == "action:live"


def test_monitor_line_contains_live_session_stats() -> None:
    text = format_execution_monitor(_payload(tools=11))
    assert (
        "#76 · Анализ · агент analyzer · 6 мин · 11 инструментов · "
        "138k токенов ($0.13) · активность 3с назад · сейчас: bash"
    ) in text
    assert "завис" not in text.lower()
    assert "В очереди" in text


def test_callback_redraws_the_same_message_with_fresh_numbers() -> None:
    user = _user()
    first = _callback("action:live", user=user)
    second = _callback("action:live", user=user)
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(
        side_effect=[_payload(tools=11), _payload(tools=12)]
    )

    with patch(
        "kuzya.telegram.handlers.rich_edit_message", new_callable=AsyncMock
    ) as edit:
        asyncio.run(on_action_live(first, client=client))
        asyncio.run(on_action_live(second, client=client))

    assert edit.await_count == 2
    first_call, second_call = edit.await_args_list
    assert "11 инструментов" in first_call.args[1]
    assert "12 инструментов" in second_call.args[1]
    for call in (first_call, second_call):
        keyboard = call.kwargs["reply_markup"]
        [row] = keyboard.inline_keyboard
        assert row[0].callback_data == "action:live"
    first.answer.assert_awaited_with("Обновлено")
    second.answer.assert_awaited_with("Обновлено")


def test_live_command_sends_the_monitor_with_refresh_button() -> None:
    user = _user()
    message = _message(user=user)
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(return_value=_payload(tools=11))

    with patch(
        "kuzya.telegram.handlers.send_rich_or_plain", new_callable=AsyncMock
    ) as send:
        asyncio.run(cmd_live(message, client=client))

    send.assert_awaited_once()
    bot, chat_id, text = send.await_args.args
    assert bot is message.bot
    assert chat_id == message.chat.id
    assert "11 инструментов" in text
    keyboard = send.await_args.kwargs["reply_markup"]
    [row] = keyboard.inline_keyboard
    assert row[0].callback_data == "action:live"
    message.answer.assert_not_awaited()


def test_monitor_collapses_details_after_the_threshold() -> None:
    text = format_execution_monitor(_running_payload(40))

    # Every worker stays visible: 40 entries is below the hard row ceiling.
    assert all(f"#{number} ·" in text for number in range(1, 41))
    full_line = next(line for line in text.splitlines() if line.startswith("#1 ·"))
    collapsed_line = next(
        line
        for line in text.splitlines()
        if line.startswith(f"#{MONITOR_FULL_DETAIL_LIMIT + 1} ·")
    )
    assert "инструмент" in full_line
    assert "инструмент" not in collapsed_line
    assert f"агент agent-{MONITOR_FULL_DETAIL_LIMIT + 1}" in collapsed_line
    assert "…и ещё" not in text


def test_monitor_summarizes_entries_above_the_length_budget() -> None:
    total = 1000
    text = format_execution_monitor(_running_payload(total))

    assert len(text) <= RICH_TEXT_LIMIT
    assert "…и ещё" in text
    assert f"(всего {total})" in text
    assert f"#{total} ·" not in text
    assert "В очереди" in text
    assert "Обновлено" in text


def test_monitor_clips_verbose_fields_and_keeps_all_40_workers() -> None:
    """The reviewer case: 40 workers with ~400-character titles."""
    text = format_execution_monitor(
        _running_payload(
            40, title_padding=400, agent_padding=200, tool_padding=400
        )
    )

    assert len(text) <= RICH_TEXT_LIMIT
    assert "…и ещё" not in text
    assert all(f"#{number} ·" in text for number in range(1, 41))
    assert all(f"агент agent-{number}" in text for number in range(1, 41))
    assert "…" in text  # long labels/tools are visibly clipped
    assert "В очереди" in text
    assert "Обновлено" in text


def test_live_command_sends_a_40_worker_monitor_through_rich_path() -> None:
    user = _user()
    message = _message(user=user)
    message.bot.send_rich_message.side_effect = _rich_sender_rejecting_over_limit
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(
        return_value=_running_payload(
            40, title_padding=400, agent_padding=200, tool_padding=400
        )
    )

    asyncio.run(cmd_live(message, client=client))

    message.bot.send_rich_message.assert_awaited_once()
    markdown = message.bot.send_rich_message.await_args.kwargs[
        "rich_message"
    ].markdown
    assert len(markdown) <= RICH_TEXT_LIMIT
    # The rich transport shrinks every leading ``#N`` into an inline code
    # span so Telegram cannot render monitor rows as huge headings.
    assert all(f"`#{number}` ·" in markdown for number in range(1, 41))
    assert all(f"агент agent-{number}" in markdown for number in range(1, 41))
    message.bot.send_message.assert_not_awaited()


def test_live_command_truncates_the_plain_fallback_for_a_long_monitor() -> None:
    user = _user()
    message = _message(user=user)
    message.bot.send_rich_message.side_effect = RuntimeError("MESSAGE_TOO_LONG")
    message.bot.send_message.return_value = MagicMock(message_id=11)
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(
        return_value=_running_payload(40, title_padding=140)
    )

    asyncio.run(cmd_live(message, client=client))

    message.bot.send_rich_message.assert_awaited_once()
    message.bot.send_message.assert_awaited_once()
    plain = message.bot.send_message.await_args.kwargs["text"]
    assert len(plain) < 4096
    assert plain.endswith("… (текст сокращён для отправки)")


def test_callback_redraws_a_40_worker_monitor_in_place() -> None:
    callback = _callback("action:live", user=_user())
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(
        return_value=_running_payload(40, title_padding=140)
    )

    with patch(
        "kuzya.telegram.handlers.rich_edit_message", new_callable=AsyncMock
    ) as edit:
        asyncio.run(on_action_live(callback, client=client))

    edit.assert_awaited_once()
    text = edit.await_args.args[1]
    assert len(text) > 4096
    assert all(f"#{number} ·" in text for number in range(1, 41))
    callback.answer.assert_awaited_with("Обновлено")


def test_callback_redraws_verbose_workers_under_the_rich_limit() -> None:
    callback = _callback("action:live", user=_user())
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(
        return_value=_running_payload(
            40, title_padding=400, agent_padding=200, tool_padding=400
        )
    )

    def edit_message_text(**kwargs):
        rich = kwargs.get("rich_message")
        if rich is None:
            raise RuntimeError("plain transport used")
        if len(rich.markdown) > RICH_TEXT_LIMIT:
            raise RuntimeError("MESSAGE_TOO_LONG")
        return MagicMock(message_id=21)

    callback.message.bot.edit_message_text.side_effect = edit_message_text

    asyncio.run(on_action_live(callback, client=client))

    callback.message.bot.edit_message_text.assert_awaited_once()
    markdown = callback.message.bot.edit_message_text.await_args.kwargs[
        "rich_message"
    ].markdown
    assert len(markdown) <= RICH_TEXT_LIMIT
    # Leading ``#N`` is escaped into an inline code span in the rich edit.
    assert all(f"`#{number}` ·" in markdown for number in range(1, 41))
    assert all(f"агент agent-{number}" in markdown for number in range(1, 41))
    callback.answer.assert_awaited_with("Обновлено")
