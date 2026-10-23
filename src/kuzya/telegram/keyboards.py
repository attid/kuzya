"""Inline keyboards for the Telegram Secretary bot."""

from __future__ import annotations

from typing import Any
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def task_display_number(task: Any) -> str:
    """Human task number; tolerate older backends without this field."""
    if isinstance(task, dict):
        number = task.get("number")
        return str(number if number is not None else task["id"])
    number = getattr(task, "number", None)
    return str(number if number is not None else task.id)


def mode_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting global mode when idle."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Новая задача", callback_data="action:new_task"),
                InlineKeyboardButton(text="💬 Разговор", callback_data="action:talk"),
            ],
            [
                InlineKeyboardButton(text="📋 Список задач", callback_data="action:list_tasks"),
            ],
        ]
    )


def talk_keyboard() -> InlineKeyboardMarkup:
    """Actions available during general talk mode."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Новый диалог", callback_data="action:reset_talk"),
                InlineKeyboardButton(text="📋 Список задач", callback_data="action:list_tasks"),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Выбрать агента", callback_data="talk_agent:1"
                ),
            ],
        ]
    )


def talk_agent_keyboard(
    agents: list[dict[str, Any]],
    page: int = 1,
    page_size: int = 10,
) -> InlineKeyboardMarkup:
    """Paginated list of agents that allow direct conversations.

    ``agents`` is the already filtered ``talkable`` profile list. Each row
    selects one agent by its stable id; the handler verifies the flag again
    before creating or resuming a conversation.
    """
    total_pages = max(1, (len(agents) + page_size - 1) // page_size)
    current_page = max(1, min(page, total_pages))
    start = (current_page - 1) * page_size
    keyboard: list[list[InlineKeyboardButton]] = []

    for agent in agents[start : start + page_size]:
        agent_id = str(agent.get("agent_id") or "")
        if not agent_id:
            continue
        name = str(agent.get("name") or agent_id)
        if len(name) > 40:
            name = name[:37] + "..."
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {name} ({agent_id})",
                    callback_data=f"select_talk_agent:{agent_id}",
                )
            ]
        )
    if not keyboard:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="— Нет доступных агентов —", callback_data="noop"
                )
            ]
        )

    if total_pages > 1:
        prev_btn = (
            InlineKeyboardButton(
                text="⬅️", callback_data=f"talk_agent:{current_page - 1}"
            )
            if current_page > 1
            else InlineKeyboardButton(text="⏹", callback_data="noop")
        )
        page_btn = InlineKeyboardButton(
            text=f"Стр. {current_page} из {total_pages}",
            callback_data="noop",
        )
        next_btn = (
            InlineKeyboardButton(
                text="➡️", callback_data=f"talk_agent:{current_page + 1}"
            )
            if current_page < total_pages
            else InlineKeyboardButton(text="⏹", callback_data="noop")
        )
        keyboard.append([prev_btn, page_btn, next_btn])

    keyboard.append(
        [
            InlineKeyboardButton(
                text="💬 К секретарю", callback_data="action:talk"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def agent_conversation_keyboard(
    agent_id: str, conversation_id: str | None = None
) -> InlineKeyboardMarkup:
    """Buttons attached to every direct-agent reply.

    Ending releases the agent workspace lock without deleting the native
    Runner session, so a later selection resumes the same conversation.
    """
    end_callback = (
        f"end_talk:{conversation_id}" if conversation_id else "end_talk:"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏁 Закончить", callback_data=end_callback
                ),
                InlineKeyboardButton(
                    text="💬 С секретарём", callback_data="action:talk"
                ),
            ]
        ]
    )


def active_talks_keyboard(
    conversations: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    """One 'end' button per active direct-agent conversation."""
    keyboard: list[list[InlineKeyboardButton]] = []
    for conversation in conversations:
        conversation_id = str(conversation.get("id") or "")
        if not conversation_id:
            continue
        metadata = conversation.get("metadata") or {}
        agent_id = str(metadata.get("talkable_agent") or "agent")
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"🏁 Закончить ({agent_id})",
                    callback_data=f"end_talk:{conversation_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def cancel_queue_keyboard(queue_id: str) -> InlineKeyboardMarkup:
    """Inline button to cancel a queued message while waiting."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"cancel_queue:{queue_id}",
                )
            ]
        ]
    )


def dialog_switch_keyboard(
    *,
    conversation_id: str,
    task_id: str | None = None,
    task_number: Any = None,
) -> InlineKeyboardMarkup:
    """The explicit 'continue this dialog' button on dialog messages.

    Task-bound dialogs offer to resume the task (the handler reuses the task
    card flow); free dialogs offer the conversation itself. Callback data
    stays within Telegram's 64-byte budget: ``switch_task:<32 hex>`` and
    ``switch_conversation:<32 hex>``.
    """
    if task_id is not None:
        number = f" №{task_number}" if task_number is not None else ""
        button = InlineKeyboardButton(
            text=f"▶️ Продолжить задачу{number}",
            callback_data=f"switch_task:{task_id}",
        )
    else:
        button = InlineKeyboardButton(
            text="💬 Продолжить этот диалог",
            callback_data=f"switch_conversation:{conversation_id}",
        )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def task_card_keyboard(
    task_id: str, status: str, *, running: bool = False
) -> InlineKeyboardMarkup:
    """Actions available for a selected task card."""
    buttons: list[list[InlineKeyboardButton]] = []

    if running:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⏹ Остановить", callback_data=f"interrupt_task:{task_id}"
                )
            ]
        )

    action_row: list[InlineKeyboardButton] = []
    if status != "incoming":
        action_row.append(
            InlineKeyboardButton(
                text="📥 Во входящие", callback_data=f"set_status:{task_id}:incoming"
            )
        )
    if status != "in_progress":
        action_row.append(
            InlineKeyboardButton(
                text="⚙️ В работу", callback_data=f"set_status:{task_id}:in_progress"
            )
        )
    if status != "done":
        action_row.append(
            InlineKeyboardButton(
                text="✅ Завершить", callback_data=f"set_status:{task_id}:done"
            )
        )
    if status != "archived":
        action_row.append(
            InlineKeyboardButton(
                text="📦 В архив", callback_data=f"set_status:{task_id}:archived"
            )
        )
    if action_row:
        # Split action buttons into rows of max 2 buttons
        buttons.extend([action_row[i : i + 2] for i in range(0, len(action_row), 2)])

    talk_label = (
        "✍️ Ответить на уточнение"
        if status == "waiting_for_input"
        else "💬 Обсудить с секретарём"
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=talk_label, callback_data=f"talk_task:{task_id}"
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="📋 К списку задач", callback_data="action:list_tasks"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def execution_monitor_keyboard() -> InlineKeyboardMarkup:
    """Refresh button for the live execution monitor message."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data="action:live"
                )
            ]
        ]
    )


#: Category tab whose contents are every status not shown by another tab.
OTHER_CATEGORY = "other"

#: Statuses owned by the regular category tabs. ``archived`` is deliberately
#: absent: the archive tab never contributes to the "Other" list or count.
CATEGORY_STATUSES = ("incoming", "in_progress", "failed", "done")
OTHER_EXCLUDED_STATUSES = (*CATEGORY_STATUSES, "archived")

#: Distinguishable row markers for statuses that need operator attention.
TASK_STATUS_ICONS = {
    "waiting_for_input": "⚠️",
    "ready_to_commit": "⏳",
}


def category_list_filters(category: str) -> dict[str, Any]:
    """Return ``list_tasks`` keyword filters for a category tab.

    Regular tabs filter one exact status. The "Other" tab is defined by
    exclusion, so a status added later automatically appears there without
    changing this module or the handlers.
    """
    if category == OTHER_CATEGORY:
        return {"status_not_in": list(OTHER_EXCLUDED_STATUSES)}
    return {"status": category}


def category_task_count(category: str, counts: dict[str, int]) -> int:
    """Return a tab counter derived by the same rules as its task list.

    The "Other" counter sums every stored status outside the regular tabs and
    the archive, so it matches the list even when a new status appears.
    """
    if category != OTHER_CATEGORY:
        return counts.get(category, 0)
    return sum(
        count
        for status, count in counts.items()
        if status not in OTHER_EXCLUDED_STATUSES
    )


def task_categories_keyboard(
    active_category: str = "incoming",
    tasks: list[dict[str, Any]] | None = None,
    page: int = 1,
    page_size: int = 8,
    counts: dict[str, int] | None = None,
    active_talks_count: int = 0,
) -> InlineKeyboardMarkup:
    """Category tabs and paginated task list buttons.

    ``counts`` maps a task status to its task count. Every category except the
    archive shows its count (missing statuses are rendered as zero). Archive
    remains a plain label because its contents are intentionally not tracked
    in the active workflow.
    """
    categories = [
        ("incoming", "📥 Новые"),
        ("in_progress", "⚙️ Работа"),
        ("failed", "❌ Ошибки"),
        (OTHER_CATEGORY, "🗂 Прочее"),
        ("done", "✅ Готово"),
        ("archived", "📦 Архив"),
    ]

    def category_label(category: str, label: str) -> str:
        if counts is None or category == "archived":
            return label
        return f"{label} {category_task_count(category, counts)}"

    # Category tabs
    cat_buttons = []
    for category, label in categories:
        text = category_label(category, label)
        if category == active_category:
            text = f"• {text} •"
        cat_buttons.append(
            InlineKeyboardButton(
                text=text,
                callback_data=f"cat:{category}:1",
            )
        )
    # Split into 2 rows (3 and 2)
    keyboard = [cat_buttons[:3], cat_buttons[3:]]

    all_tasks = tasks or []
    total_count = len(all_tasks)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * page_size
    page_tasks = all_tasks[start_idx : start_idx + page_size]

    # Tasks on current page
    if page_tasks:
        for t in page_tasks:
            title = t.get("title") or t["id"]
            if len(title) > 30:
                title = title[:27] + "..."
            icon = TASK_STATUS_ICONS.get(t.get("status"), "📌")
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} #{task_display_number(t)} {title}",
                        callback_data=f"select_task:{t['id']}",
                    )
                ]
            )
    else:
        keyboard.append(
            [InlineKeyboardButton(text="— Нет задач в категории —", callback_data="noop")]
        )

    # Pagination navigation if more than 1 page
    if total_pages > 1:
        prev_btn = (
            InlineKeyboardButton(
                text="⬅️", callback_data=f"cat:{active_category}:{current_page - 1}"
            )
            if current_page > 1
            else InlineKeyboardButton(text="⏹", callback_data="noop")
        )
        page_btn = InlineKeyboardButton(
            text=f"Стр. {current_page} из {total_pages}", callback_data="noop"
        )
        next_btn = (
            InlineKeyboardButton(
                text="➡️", callback_data=f"cat:{active_category}:{current_page + 1}"
            )
            if current_page < total_pages
            else InlineKeyboardButton(text="⏹", callback_data="noop")
        )
        keyboard.append([prev_btn, page_btn, next_btn])

    # Active direct-agent conversations are a separate entity from tasks:
    # they occupy an agent workspace but never appear in a task category.
    keyboard.append(
        [
            InlineKeyboardButton(
                text=f"🗣 Разговоры: {active_talks_count}",
                callback_data="action:active_talks",
            )
        ]
    )
    # Live execution monitor and global actions.
    keyboard.append(
        [
            InlineKeyboardButton(
                text="⚡ Исполнители", callback_data="action:live"
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(text="➕ Новая задача", callback_data="action:new_task"),
            InlineKeyboardButton(text="💬 Общий разговор", callback_data="action:talk"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
