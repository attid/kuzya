"""Telegram message and callback query handlers for the Secretary bot."""

from __future__ import annotations

import datetime as dt
import html
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Awaitable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .client import BackendClient
from .config import TelegramConfig
from .messenger import (
    RICH_TEXT_LIMIT,
    markdown_hashtag_safe,
    rich_edit_message,
    send_rich_or_plain,
)
from .keyboards import (
    active_talks_keyboard,
    agent_conversation_keyboard,
    category_list_filters,
    execution_monitor_keyboard,
    mode_keyboard,
    task_display_number,
    talk_agent_keyboard,
    talk_keyboard,
    task_card_keyboard,
    task_categories_keyboard,
)
from ..git_engine import GitEngine
from .state import UserStateStorage
from .worker import QueueProcessor

logger = logging.getLogger(__name__)

router = Router(name="telegram_secretary")


class AuthMiddleware(BaseMiddleware):
    """Enforce single allowed Telegram user ID."""

    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None

        # If allowed_user_id is configured (> 0) and does not match, reject
        if self.allowed_user_id > 0 and user.id != self.allowed_user_id:
            if isinstance(event, Message):
                await event.answer("⛔️ Доступ запрещен.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ Доступ запрещен.", show_alert=True)
            return None

        return await handler(event, data)


# -- Commands --


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    storage: UserStateStorage,
) -> None:
    user_id = message.from_user.id
    state = storage.get_user_state(user_id)
    text = (
        "👋 Привет! Я интерфейс к системе агентов и секретарю.\n\n"
        f"Текущий режим: <b>{state.mode}</b>\n\n"
        "Выберите действие в меню ниже или используйте команды:\n"
        "• /new — поставить новую задачу\n"
        "• /talk — общий разговор с секретарём или выбор агента\n"
        "• /list — просмотр задач и карточек\n"
        "• /live — что выполняется прямо сейчас"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=mode_keyboard())


@router.message(Command("new"))
async def cmd_new(
    message: Message,
    storage: UserStateStorage,
) -> None:
    user_id = message.from_user.id
    storage.set_user_mode(user_id, "new_task")
    await message.answer(
        "➕ <b>Режим постановки новой задачи</b>\n\n"
        "Опишите задачу текстом или отправьте голосовое сообщение.\n"
        "Секретарь уточнит требования, подготовит карточку и предложит перевести во входящие или в работу.",
        parse_mode="HTML",
    )


@router.message(Command("talk"))
async def cmd_talk(
    message: Message,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    user_id = message.from_user.id
    state = storage.get_user_state(user_id)
    talk_conv_id = state.talk_conversation_id

    if not talk_conv_id:
        conv = await client.create_conversation(
            title="Общий разговор (Telegram)",
            metadata={"origin": "telegram_talk", "user_id": user_id},
        )
        talk_conv_id = conv["id"]
        storage.set_talk_conversation(user_id, talk_conv_id)

    storage.set_user_mode(user_id, "talk", conversation_id=talk_conv_id)
    active_talks = await _active_talk_conversations(client)
    suffix = (
        f"\n\n🗣 Активных разговоров с агентами: {len(active_talks)}. "
        "Их можно завершить в «📋 Список задач → 🗣 Разговоры»."
        if active_talks
        else ""
    )
    await message.answer(
        "💬 <b>Режим общего разговора</b>\n\n"
        "Задайте любой вопрос о состоянии задач, проектных планах или перемещении карточек."
        f"{suffix}",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )


@router.message(Command("reset"))
async def cmd_reset(
    message: Message,
    storage: UserStateStorage,
) -> None:
    user_id = message.from_user.id
    storage.set_talk_conversation(user_id, None)
    storage.set_user_mode(user_id, "talk", conversation_id=None)
    await message.answer(
        "🔄 <b>Диалог сброшен.</b> Следующее сообщение начнёт новый разговор со секретарём.",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )


@router.message(Command("list"))
async def cmd_list(
    message: Message,
    client: BackendClient,
) -> None:
    tasks = await client.list_tasks(status="incoming")
    counts = await client.get_task_counts()
    active_talks = await _active_talk_conversations(client)
    await message.answer(
        "📋 <b>Список задач</b>\nВыберите категорию или карточку:",
        parse_mode="HTML",
        reply_markup=task_categories_keyboard(
            "incoming",
            tasks,
            counts=counts,
            active_talks_count=len(active_talks),
        ),
    )


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    """Pick the Russian plural form for a small counter."""
    remainder = abs(value) % 100
    if 11 <= remainder <= 14:
        return many
    remainder %= 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def _is_talkable_agent(agent: dict[str, Any]) -> bool:
    """Return whether a profile explicitly allows direct conversations."""
    runner_config = agent.get("runner_config")
    return bool(
        isinstance(runner_config, dict)
        and runner_config.get("talkable") is True
    )


async def _talkable_agents(client: BackendClient) -> list[dict[str, Any]]:
    """Fetch current profiles and keep only ``runner_config.talkable`` ones."""
    try:
        agents = await client.list_agents()
    except Exception as error:
        logger.warning("talkable agent list failed: %s", error)
        return []
    return [
        agent
        for agent in agents
        if isinstance(agent, dict) and _is_talkable_agent(agent)
    ]


async def _active_talk_conversations(
    client: BackendClient,
) -> list[dict[str, Any]]:
    """Return conversations whose direct-talk workspace lock is active."""
    try:
        conversations = await client.list_conversations()
    except Exception as error:
        logger.warning("active talk conversation list failed: %s", error)
        return []
    active: list[dict[str, Any]] = []
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        metadata = conversation.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("talk_active") is True and metadata.get(
            "talkable_agent"
        ):
            active.append(conversation)
    return active


def _agent_name_map(agents: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(agent.get("agent_id")): str(
            agent.get("name") or agent.get("agent_id")
        )
        for agent in agents
        if agent.get("agent_id")
    }


def _format_started_at(value: Any) -> str:
    """Render a stored ISO timestamp as a compact local-looking label."""
    if not isinstance(value, str) or not value:
        return "время неизвестно"
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _active_talk_message(
    conversations: list[dict[str, Any]],
    agents: list[dict[str, Any]],
) -> str:
    names = _agent_name_map(agents)
    lines = ["🗣 <b>Активные разговоры с агентами</b>"]
    for conversation in conversations:
        metadata = conversation.get("metadata") or {}
        agent_id = str(metadata.get("talkable_agent") or "agent")
        name = names.get(agent_id, agent_id)
        started = _format_started_at(metadata.get("talk_started_at"))
        lines.append(
            f"• <b>{html.escape(name)}</b> "
            f"(<code>{html.escape(agent_id)}</code>) — с {html.escape(started)}"
        )
    lines.append("")
    lines.append(
        "Разговоры занимают workspace агента: задачи этого агента ждут, "
        "пока разговор не завершён."
    )
    return "\n".join(lines)


def _format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total}с"
    if total < 3600:
        return f"{total // 60} мин"
    hours, remainder = divmod(total, 3600)
    if remainder >= 60:
        return f"{hours} ч {remainder // 60} мин"
    return f"{hours} ч"


def _format_tokens(tokens: Any) -> str | None:
    if not isinstance(tokens, dict):
        return None
    total = tokens.get("totalTokens")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return None
    if total >= 1000:
        rounded = int(round(total / 1000))
        return f"{rounded}k токенов"
    return f"{int(total)} токенов"


#: Above this many running entries the monitor collapses session details.
#: The first entries keep the full picture; the rest stay one compact line
#: each so the owner can still see who is working.
MONITOR_FULL_DETAIL_LIMIT = 15

#: Per-field caps for the monitor. Titles, agent ids and tool names come from
#: task data and can be arbitrarily long; clipping them keeps one row bounded
#: while preserving the worker identity (number and agent prefix).
MONITOR_LABEL_LIMIT = 120
MONITOR_AGENT_LIMIT = 40
MONITOR_TOOL_LIMIT = 60


def _clip_monitor_text(value: Any, limit: int) -> str:
    """Clip one field to ``limit`` characters with an explicit ellipsis."""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _monitor_entry_parts(item: dict[str, Any]) -> list[str]:
    """Identity, agent and elapsed time of one running entry.

    These facts are always rendered, including for collapsed rows: the
    monitor must never hide *who* is working, only optional live counters.
    """
    is_operation = item.get("entry_type") == "operation" or bool(
        item.get("operation_id")
    )
    number = item.get("target_number") if is_operation else item.get("number")
    if is_operation:
        label = item.get("kind_label") or item.get("operation_kind") or "операция"
    else:
        label = item.get("title") or "задача"
    parts = [
        f"#{number}" if number is not None else "?",
        _clip_monitor_text(label, MONITOR_LABEL_LIMIT),
    ]
    agent = item.get("agent_id")
    if agent:
        parts.append(f"агент {_clip_monitor_text(agent, MONITOR_AGENT_LIMIT)}")
    parts.append(_format_duration(item.get("elapsed")))
    return parts


def _format_monitor_entry(item: dict[str, Any], *, detailed: bool) -> str:
    """Render one entry as a full or collapsed monitor line."""
    parts = _monitor_entry_parts(item)
    if detailed and item.get("session_available"):
        tools = item.get("tools_used")
        if tools is not None:
            parts.append(
                f"{tools} {_plural_ru(int(tools), 'инструмент', 'инструмента', 'инструментов')}"
            )
        tokens_text = _format_tokens(item.get("tokens"))
        if tokens_text:
            cost = item.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                tokens_text += f" (${cost:.2f})"
            parts.append(tokens_text)
        activity = item.get("last_activity_sec")
        if activity is not None:
            parts.append(f"активность {activity}с назад")
        current = item.get("current_tool")
        if current:
            parts.append(
                f"сейчас: {_clip_monitor_text(current, MONITOR_TOOL_LIMIT)}"
            )
    return " · ".join(parts)


def _monitor_remaining_line(remaining: int, total: int) -> str:
    """Explicit summary for entries that did not fit the transport budget."""
    noun = _plural_ru(remaining, "исполнитель", "исполнителя", "исполнителей")
    return f"…и ещё {remaining} {noun} (всего {total})"


def _render_monitor(
    prefix: list[str],
    suffix: list[str],
    rows: list[str],
    summary: str | None,
) -> str:
    """Assemble the monitor with an optional collapse summary."""
    lines = list(prefix)
    lines.extend(rows)
    if summary is not None:
        if rows:
            lines.append("")
        lines.append(summary)
    lines.extend(suffix)
    return "\n".join(lines)


def format_execution_monitor(payload: dict[str, Any]) -> str:
    """Render the monitor payload as a compact Telegram message.

    The session age is labelled as activity, never as a stall verdict: a
    model can think without emitting an event for a long time.

    The message has a hard character budget: the rich transport capacity
    (``RICH_TEXT_LIMIT``), measured on the hashtag-escaped variant the rich
    transport actually sends. The first ``MONITOR_FULL_DETAIL_LIMIT`` entries
    carry full session counters, later entries collapse to identity/agent/
    elapsed, long fields are clipped, and rows that still do not fit are
    replaced by an explicit remaining count. The queue footer is always
    rendered; it is never sacrificed to an unbounded worker list.
    """
    running = payload.get("running") or []
    prefix = ["⚡ Исполнители", ""]

    queues = payload.get("queues") or {}
    suffix = [
        "",
        "⏳ В очереди: "
        f"ходы {int(queues.get('turns', 0))} · "
        f"операции {int(queues.get('operations', 0))} · "
        f"подписки {int(queues.get('subscriptions', 0))} · "
        f"задачи без клейма {int(queues.get('tasks', 0))}",
    ]
    generated = payload.get("generated_at")
    if isinstance(generated, str) and len(generated) >= 19:
        suffix.append(f"Обновлено: {generated[11:19]} UTC")

    if not running:
        return _render_monitor(
            prefix, suffix, ["Сейчас никто не выполняется."], None
        )

    rows: list[str] = []
    summary: str | None = None
    for index, item in enumerate(running):
        preferred = _format_monitor_entry(
            item, detailed=index < MONITOR_FULL_DETAIL_LIMIT
        )
        candidates = [preferred]
        if index < MONITOR_FULL_DETAIL_LIMIT:
            compact = _format_monitor_entry(item, detailed=False)
            if compact != preferred:
                candidates.append(compact)

        chosen: str | None = None
        for candidate in candidates:
            remaining = len(running) - (index + 1)
            candidate_summary = (
                _monitor_remaining_line(remaining, len(running))
                if remaining > 0
                else None
            )
            # The budget check mirrors the transport: every ``#N`` row gains
            # two backticks in the rich Markdown escaping, so the *safe*
            # variant is what must fit RICH_TEXT_LIMIT.
            delivered = markdown_hashtag_safe(
                _render_monitor(prefix, suffix, [*rows, candidate], candidate_summary)
            )
            if len(delivered) <= RICH_TEXT_LIMIT:
                chosen = candidate
                summary = candidate_summary
                break
        if chosen is None:
            # Preserve the remaining count even when not a single extra row
            # fits: the owner still learns that more workers are running.
            summary = _monitor_remaining_line(
                len(running) - len(rows), len(running)
            )
            break
        rows.append(chosen)

    return _render_monitor(prefix, suffix, rows, summary)


@router.message(Command("live"))
async def cmd_live(
    message: Message,
    client: BackendClient,
) -> None:
    """Show running executions and allow refreshing by pressing the button."""
    try:
        payload = await client.get_execution_monitor()
    except Exception as error:
        await message.answer(f"❌ Не удалось получить состояние исполнителей: {error}")
        return
    await send_rich_or_plain(
        message.bot,
        message.chat.id,
        format_execution_monitor(payload),
        reply_markup=execution_monitor_keyboard(),
    )


@router.message(Command("watch", "unwatch"))
async def cmd_watch(message: Message, storage: UserStateStorage, client: BackendClient) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Укажите номер задачи: /watch 8 или /unwatch 8.")
        return
    cancel = parts[0].split("@", 1)[0] == "/unwatch"
    try:
        task = await client.get_task(parts[1])
        state = storage.get_user_state(message.from_user.id)
        conversation_id = state.active_conversation_id or state.talk_conversation_id
        if not conversation_id:
            conv = await client.create_conversation(
                title="Общий разговор (Telegram)",
                metadata={"chat_id": message.chat.id, "user_id": message.from_user.id},
            )
            conversation_id = conv["id"]
            storage.set_talk_conversation(message.from_user.id, conversation_id)
            storage.set_user_mode(message.from_user.id, "talk", conversation_id=conversation_id)
        result = await client.subscribe_task(
            task["id"], conversation_id, instructions=parts[2] if len(parts) > 2 else "", cancel=cancel,
        )
        number = task_display_number(task)
        if cancel:
            text = f"Ожидание результата задачи #{number} отменено."
        elif result["status"] == "delivered":
            text = f"Результат задачи #{number} уже разобран в этом диалоге."
        else:
            text = f"Подписка на задачу #{number} сохранена. Когда она завершится, секретарь разберёт результат и ответит здесь."
        await message.answer(text, parse_mode=None)
    except Exception as error:
        await message.answer(f"Не удалось изменить подписку: {error}", parse_mode=None)


async def _render_analysis_report(
    message: Message,
    client: BackendClient,
    task: dict[str, Any],
    report_id: str,
    task_id: str,
) -> None:
    """Render a persisted analysis report and its recommendations."""
    try:
        report = await client.get_analysis_report(report_id)
        recommendations = await client.list_report_recommendations(report_id)
    except Exception as error:
        await message.answer(
            f"❌ Не удалось прочитать отчёт анализа "
            f"<code>{html.escape(report_id)}</code>: "
            f"<code>{html.escape(str(error))}</code>",
            parse_mode="HTML",
        )
        return

    summary = report.get("summary") or ""
    agent_id = report.get("agent_id") or "system-log-analyzer"
    revision = report.get("profile_revision")
    lines = [
        f"📊 <b>Анализ задачи #{html.escape(task_display_number(task))}: {html.escape(task.get('title', ''))}</b>",
        "",
        f"• <b>Анализатор:</b> <code>{html.escape(str(agent_id))}</code>"
        + (f" (rev {revision})" if revision is not None else ""),
        f"• <b>Статус:</b> <code>{html.escape(str(task.get('status', '')))}</code>",
        f"• <b>Резюме:</b>\n<i>{html.escape(summary[:800])}</i>",
    ]
    if recommendations:
        lines.append("")
        lines.append(f"💡 <b>Рекомендации ({len(recommendations)}):</b>")
        for item in recommendations[:10]:
            lines.append(
                f"• <code>{html.escape(str(item.get('status', '')))}</code> "
                f"{html.escape(str(item.get('target_kind', '')))}"
                f":{html.escape(str(item.get('target_id', '')))}\n"
                f"  {html.escape(str(item.get('title', '')))}"
            )
    else:
        lines.append("")
        lines.append("Без рекомендаций.")
    lines.append("")
    lines.append(
        "Одобрение и применение — отдельные явные действия; "
        f"для повторного анализа после изменений: "
        f"<code>/analyze {html.escape(task_id)} refresh</code>. "
        f"Отчёт <code>{html.escape(report_id)}</code>."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 К карточке задачи",
                    callback_data=f"select_task:{task_id}",
                )
            ]
        ]
    )
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@router.message(Command("analyze"))
async def cmd_analyze(
    message: Message,
    client: BackendClient,
) -> None:
    """Show the existing analysis report or submit a durable analysis.

    The command never invents findings and never launches a model when a
    report is already stored. Pending, failed and completed outcomes are
    shown truthfully. A repeated call while an analysis is pending replays
    the same durable operation; ``/analyze <task_id> refresh`` explicitly
    starts a new analysis after the evidence changed.
    """
    tokens = (message.text or "").split()
    refresh_flags = {"refresh", "--refresh", "fresh"}
    force_fresh = any(token.lower() in refresh_flags for token in tokens[1:])
    positional = [
        token for token in tokens[1:] if token.lower() not in refresh_flags
    ]
    task_id = positional[0].strip() if positional else None

    if not task_id:
        tasks = await client.list_tasks(status="done")
        if not tasks:
            tasks = await client.list_tasks(status="failed")
        if not tasks:
            await message.answer("ℹ️ Нет завершённых задач для анализа. Укажите ID: <code>/analyze &lt;task_id&gt;</code>", parse_mode="HTML")
            return
        task_id = tasks[0]["id"]

    try:
        task = await client.get_task(task_id)
    except Exception:
        await message.answer(f"❌ Задача <code>{html.escape(task_id)}</code> не найдена.", parse_mode="HTML")
        return

    task_id = task["id"]

    # An existing report is a stored result: render it without a model call.
    try:
        reports = await client.list_analysis_reports(task_id)
    except Exception:
        reports = []
    if reports and not force_fresh:
        latest = reports[0]
        report_id = latest.get("id")
        if report_id:
            await _render_analysis_report(
                message, client, task, str(report_id), task_id
            )
            return

    try:
        # A repeated request for the same task evidence replays the same
        # durable operation instead of launching another model. The key is
        # bound to the task's current revision, so changed evidence (or an
        # explicit ``refresh``) can still start a fresh analysis.
        idempotency_key = None
        if not force_fresh:
            revision = str(task.get("updated_at") or "")
            idempotency_key = f"telegram-analysis:{task_id}:{revision}"
        operation = await client.trigger_analysis(
            task_id, idempotency_key=idempotency_key
        )
        operation_id = operation.get("id")
        if operation_id:
            # Submission alone is not a result; read the stored status.
            operation = await client.get_operation(operation_id)
    except Exception as error:
        await message.answer(
            f"❌ Не удалось запустить анализ задачи "
            f"<code>{html.escape(task_id)}</code>: "
            f"<code>{html.escape(str(error))}</code>",
            parse_mode="HTML",
        )
        return

    status = operation.get("status")
    result = operation.get("result") or {}
    operation_id = operation.get("id", operation_id)

    if status in {"pending", "accepted", "running"}:
        await message.answer(
            f"⏳ <b>Анализ задачи #{html.escape(task_display_number(task))} запущен</b>\n"
            f"Операция <code>{html.escape(str(operation_id))}</code> в статусе "
            f"<code>{html.escape(str(status))}</code>. Отчёт появится после "
            "завершения операции; повторный вызов читает ту же операцию и не "
            "запускает модель. Для повторного анализа после изменений: "
            f"<code>/analyze {html.escape(task_id)} refresh</code>.",
            parse_mode="HTML",
        )
        return

    if status != "succeeded" or not result.get("report_id"):
        error = operation.get("error") or "анализ не завершился успешно"
        await message.answer(
            f"❌ <b>Анализ задачи #{html.escape(task_display_number(task))} не удался</b>\n"
            f"<code>{html.escape(str(error))}</code>",
            parse_mode="HTML",
        )
        return

    await _render_analysis_report(
        message, client, task, str(result["report_id"]), task_id
    )


# -- Callbacks --


@router.callback_query(F.data.startswith("cat:"))
async def on_category_switch(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    parts = callback.data.split(":")
    category = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    tasks = await client.list_tasks(**category_list_filters(category))
    counts = await client.get_task_counts()
    active_talks = await _active_talk_conversations(client)
    await callback.message.edit_reply_markup(
        reply_markup=task_categories_keyboard(
            category,
            tasks,
            page=page,
            counts=counts,
            active_talks_count=len(active_talks),
        )
    )
    await callback.answer()


@router.callback_query(F.data.startswith("select_task:"))
async def on_select_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    task_id = callback.data.split(":")[1]
    task = await client.get_task(task_id)

    await _bind_task_conversation(task, callback, storage, client)
    meta = dict(task.get("metadata") or {})

    clar = meta.get("clarification") or dict(meta.get("provenance") or {}).get("clarification") or {}

    # Markdown card through the rich editor (~16k capacity) with a truncated
    # plain-text fallback; long descriptions are capped so the whole card
    # always fits the rich limit.
    description = task.get("description") or ""
    if len(description) > 14000:
        description = description[:14000] + "\n\n… (описание сокращено)"
    card_lines = [
        f"📌 **Задача #{task_display_number(task)}**",
        f"Название: **{task['title']}**",
        f"Статус: `{task['status']}`",
        f"Пространство: {task.get('workspace') or '—'}",
        "",
        f"Описание:\n{description or '_(без описания)'}",
    ]
    if task.get("status") == "waiting_for_input" and clar:
        card_lines += [
            "",
            "⚠️ **Вопрос от агента:**",
            f"**{clar.get('title') or ''}**",
            str(clar.get("description") or ""),
            "",
            f"❓ **Вопрос:** {clar.get('question') or ''}",
        ]
    card_lines += [
        "",
        "_Контекст задачи выбран. Сообщения направляются по этой задаче._",
    ]
    await rich_edit_message(
        callback.message,
        "\n".join(card_lines),
        reply_markup=task_card_keyboard(
            task["id"], task["status"], running=_task_is_running(task)
        ),
    )
    await callback.answer("Контекст задачи выбран")


def _task_is_running(task: Dict[str, Any]) -> bool:
    """Whether the task's execution is live (the ⏹ stop button condition)."""
    return (task.get("metadata") or {}).get("execution_status") == "running"


async def _bind_task_conversation(
    task: Dict[str, Any],
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> str:
    """Ensure the task has a bound conversation and make it the active context.

    Returns the conversation id. Used by both the task card selection and the
    dialog switch button, so either path lands in the same task context.
    """
    task_id = task["id"]
    conv_id = task.get("metadata", {}).get("conversation_id")
    meta = dict(task.get("metadata") or {})
    needs_patch = False
    if not conv_id:
        conv = await client.create_conversation(
            title=f"Контекст задачи {task_id}",
            metadata={"task_id": task_id},
        )
        conv_id = conv["id"]
        meta["conversation_id"] = conv_id
        needs_patch = True

    chat_id = callback.message.chat.id if callback.message and callback.message.chat else None
    if chat_id and "chat_id" not in meta:
        meta["chat_id"] = chat_id
        meta["user_id"] = callback.from_user.id
        needs_patch = True

    if needs_patch:
        await client.patch_task(task_id, metadata=meta)

    storage.set_user_mode(
        callback.from_user.id, "task", task_id=task_id, conversation_id=conv_id
    )
    return conv_id


@router.callback_query(F.data.startswith("switch_conversation:"))
async def on_switch_conversation(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    """Explicit switch of the active context to a conversation by its UUID."""
    conversation_id = callback.data.split(":", 1)[1]
    try:
        conversation = await client.get_conversation(conversation_id)
    except Exception as error:
        logger.warning("switch conversation lookup failed: %s", error)
        conversation = None
    if conversation is None:
        await callback.answer("Диалог недоступен", show_alert=True)
        await callback.message.answer(
            "⚠️ Диалог больше не доступен. Активный контекст не изменён."
        )
        return

    user_id = callback.from_user.id
    previous = storage.get_user_state(user_id).active_conversation_id
    metadata = conversation.get("metadata") or {}
    agent_id = (
        metadata.get("talkable_agent")
        if isinstance(metadata, dict)
        else None
    )
    if agent_id:
        try:
            conversation = await client.start_talk(conversation_id)
        except Exception as error:
            logger.warning("resume talk conversation failed: %s", error)
            await callback.answer(
                "Не удалось возобновить разговор", show_alert=True
            )
            return
        metadata = conversation.get("metadata") or {}
        closing = metadata.get("talk_closing") is True
        active = metadata.get("talk_active") is True and not closing
        storage.set_talk_agent_conversation(user_id, conversation_id)
        reply_markup = agent_conversation_keyboard(
            str(agent_id), conversation_id
        )
        if closing:
            note = (
                "Предыдущий разговор ещё завершается. Новые ходы пока не "
                "принимаются."
            )
        elif active:
            note = "Обычные сообщения теперь уходят этому агенту."
        else:
            note = (
                "Workspace занят. Первое сообщение будет ждать "
                "освобождения."
            )
        confirmation = (
            f"✅ <b>Активный разговор с агентом "
            f"{html.escape(str(agent_id))}:</b>\n"
            f"<code>#{html.escape(conversation_id)}</code>\n\n"
            f"{note}"
        )
    else:
        storage.set_talk_conversation(user_id, conversation_id)
        storage.set_user_mode(
            user_id, "talk", conversation_id=conversation_id
        )
        reply_markup = talk_keyboard()
        confirmation = (
            f"✅ <b>Активный диалог:</b>\n"
            f"<code>#{html.escape(conversation_id)}</code>\n\n"
            "Обычные сообщения теперь уходят в этот диалог."
        )
    logger.info(
        "telegram context switched to conversation",
        extra={
            "event": "telegram_context_switch",
            "basis": "switch_conversation",
            "previous_conversation_id": previous,
            "conversation_id": conversation_id,
        },
    )
    await callback.message.answer(
        confirmation,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )
    await callback.answer("Диалог выбран")


@router.callback_query(F.data.startswith("switch_task:"))
async def on_switch_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    """Explicit switch of the active context to a task's dialog."""
    task_id = callback.data.split(":", 1)[1]
    try:
        task = await client.get_task(task_id)
    except Exception as error:
        logger.warning("switch task lookup failed: %s", error)
        task = None
    if task is None:
        await callback.answer("Задача недоступна", show_alert=True)
        await callback.message.answer(
            "⚠️ Задача больше не доступна. Активный контекст не изменён."
        )
        return

    user_id = callback.from_user.id
    previous = storage.get_user_state(user_id).active_conversation_id
    conv_id = await _bind_task_conversation(task, callback, storage, client)
    logger.info(
        "telegram context switched to task",
        extra={
            "event": "telegram_context_switch",
            "basis": "switch_task",
            "previous_conversation_id": previous,
            "conversation_id": conv_id,
            "task_id": task["id"],
        },
    )
    await callback.message.answer(
        f"✅ <b>Активный контекст — задача #{task_display_number(task)}:</b>\n"
        f"Диалог: <code>#{html.escape(conv_id)}</code>\n\n"
        "Обычные сообщения теперь уходят в диалог этой задачи.",
        parse_mode="HTML",
        reply_markup=task_card_keyboard(
            task["id"], task["status"], running=_task_is_running(task)
        ),
    )
    await callback.answer("Контекст задачи выбран")


@router.callback_query(F.data.startswith("set_status:"))
async def on_set_status(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    _, task_id, new_status = callback.data.split(":")
    task = await client.patch_task(task_id, status=new_status)

    description = task.get("description") or ""
    if len(description) > 14000:
        description = description[:14000] + "\n\n… (описание сокращено)"
    card_text = (
        f"📌 **Задача #{task_display_number(task)}**\n"
        f"Название: **{task['title']}**\n"
        f"Статус: `{task['status']}`\n"
        f"Пространство: {task.get('workspace') or '—'}\n\n"
        f"Описание:\n{description or '_(без описания)'}\n\n"
        f"✅ _Статус обновлен на: {new_status}_"
    )
    await rich_edit_message(
        callback.message,
        card_text,
        reply_markup=task_card_keyboard(
            task["id"], task["status"], running=_task_is_running(task)
        ),
    )
    await callback.answer(f"Статус изменен на {new_status}")


@router.callback_query(F.data.startswith("interrupt_task:"))
async def on_interrupt_task(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    """Request a manual stop of a running task execution."""
    task_id = callback.data.split(":", 1)[1]
    try:
        await client.interrupt_task(task_id)
    except Exception as error:
        await callback.answer(
            f"Не удалось остановить: {error}", show_alert=True
        )
        return
    await callback.answer("Сигнал остановки отправлен")
    await rich_edit_message(
        callback.message,
        "⏹ **Остановка запрошена.**\n\n"
        "Процесс завершит текущий ход и выйдет, задача перейдёт в "
        "«ожидание ввода». Ответьте следующим сообщением, что делать "
        "дальше — агент продолжит сессию с новыми указаниями.",
    )


def _review_gate_blocked(meta: Dict[str, Any]) -> bool:
    """The task demands review but no approved verdict is recorded.

    Mirrors the ``git.commit_push`` operation gate: the invariant
    ``requires_review`` ⇒ approved must hold on every path to ``done``,
    including the Telegram commit buttons that bypass the operation queue.
    """
    review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
    return bool(meta.get("requires_review")) and not review.get("approved")


def _review_gate_keyboard(task_id: str, *, push: bool) -> InlineKeyboardMarkup:
    action = "commit_push_force" if push else "commit_local_force"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Закоммитить без ревью",
                    callback_data=f"{action}:{task_id}",
                )
            ]
        ]
    )


def _git_outcome_payload(result: Any, *, source: str, pushed: bool | None = None) -> Dict[str, Any]:
    """The durable git outcome for task metadata, mirroring the operation handler."""
    payload: Dict[str, Any] = {
        "action": result.action,
        "commit_hash": result.commit_hash,
        "branch": result.branch,
        "message": result.message,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source,
    }
    if pushed is not None:
        payload["pushed"] = pushed
        payload["ci_status"] = result.ci_status
    return payload


def _task_workspace(task: Dict[str, Any], config: TelegramConfig) -> str | None:
    """Where a task's Git operations run: explicit workspace first, then the
    directory the execution actually used (execution_context.host_path).

    A task created without a workspace still executes in its agent's project
    directory; falling back to the instance default would commit into a
    different repository.
    """
    if task.get("workspace"):
        return str(task["workspace"])
    host_path = (task.get("metadata") or {}).get("execution_context", {}).get(
        "host_path"
    )
    return str(host_path) if host_path else config.default_workspace


async def _redraw_task_card(
    message: Any,
    client: BackendClient,
    task_id: str,
    *,
    note: str | None = None,
) -> None:
    """Redraw the task card in place with a fresh status and buttons.

    Commit and push outcomes are reported in their own status message; the
    card must not be destroyed by them — it stays a live control surface.
    """
    task = await client.get_task(task_id)
    description = task.get("description") or ""
    if len(description) > 14000:
        description = description[:14000] + "\n\n… (описание сокращено)"
    card_text = (
        f"📌 **Задача #{task_display_number(task)}**\n"
        f"Название: **{task['title']}**\n"
        f"Статус: `{task['status']}`\n"
        f"Пространство: {task.get('workspace') or '—'}\n\n"
        f"Описание:\n{description or '_(без описания)'}"
    )
    if note:
        card_text += f"\n\n{note}"
    await rich_edit_message(
        message,
        card_text,
        reply_markup=task_card_keyboard(
            task["id"], task["status"], running=_task_is_running(task)
        ),
    )


async def _commit_local_impl(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
    *,
    force: bool = False,
) -> None:
    task_id = callback.data.split(":")[1]
    await callback.answer("⏳ Выполняется локальный коммит...")
    task = await client.get_task(task_id)
    ws = _task_workspace(task, config)
    meta = task.get("metadata") or {}
    if _review_gate_blocked(meta) and not force:
        await callback.message.edit_text(
            f"⏸ <b>Задача #{html.escape(task_display_number(task))}: ревью не одобрено</b>\n\n"
            "Для задачи включено обязательное Code Review, но подтверждённого "
            "вердикта нет — коммит заблокирован.\n\n"
            "<i>Можно закоммитить принудительно; это осознанный обход ревью "
            "и он попадёт в журнал.</i>",
            parse_mode="HTML",
            reply_markup=_review_gate_keyboard(task_id, push=False),
        )
        return
    if force:
        logger.info(
            "commit_local forced past an unapproved review: task=%s", task_id
        )
    changed_files = meta.get("changed_files") or []
    summary = meta.get("summary")

    status_msg = await callback.message.answer("⏳ Локальный коммит...")
    git_engine = GitEngine(default_workspace=ws)
    res = git_engine.commit_local(
        workspace=ws,
        task_id=task_id,
        title=task["title"],
        summary=summary,
        changed_files=changed_files,
    )
    if res.success:
        await client.patch_task(
            task_id,
            status="done",
            metadata={
                "git_outcome": _git_outcome_payload(res, source="telegram")
            },
        )
        await status_msg.edit_text(
            f"✅ <b>Задача #{html.escape(task_display_number(task))} закоммичена!</b>\n\n"
            f"Ветка: <code>{html.escape(res.branch or 'HEAD')}</code>\n"
            f"Коммит: <code>{html.escape(res.commit_hash or '—')}</code>\n\n"
            f"<i>{html.escape(res.message)}</i>",
            parse_mode="HTML",
        )
        await _redraw_task_card(
            callback.message,
            client,
            task_id,
            note="_✅ Изменения закоммичены локально_",
        )
    else:
        await status_msg.edit_text(
            f"⚠️ <b>Ошибка локального коммита по задаче #{html.escape(task_display_number(task))}:</b>\n\n{html.escape(res.message)}",
            parse_mode="HTML",
        )


async def _commit_push_impl(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
    *,
    force: bool = False,
) -> None:
    task_id = callback.data.split(":")[1]
    await callback.answer("🚀 Запуск коммита, пуша и проверки CI...")
    task = await client.get_task(task_id)
    ws = _task_workspace(task, config)
    meta = task.get("metadata") or {}
    if _review_gate_blocked(meta) and not force:
        await callback.message.edit_text(
            f"⏸ <b>Задача #{html.escape(task_display_number(task))}: ревью не одобрено</b>\n\n"
            "Для задачи включено обязательное Code Review, но подтверждённого "
            "вердикта нет — коммит и пуш заблокированы.\n\n"
            "<i>Можно закоммитить принудительно; это осознанный обход ревью "
            "и он попадёт в журнал.</i>",
            parse_mode="HTML",
            reply_markup=_review_gate_keyboard(task_id, push=True),
        )
        return
    if force:
        logger.info(
            "commit_push forced past an unapproved review: task=%s", task_id
        )
    changed_files = meta.get("changed_files") or []
    summary = meta.get("summary")

    status_msg = await callback.message.answer("⏳ Коммит и отправка в GitHub... Ожидаем проверки CI...")

    git_engine = GitEngine(default_workspace=ws)
    res = git_engine.commit_and_push(
        workspace=ws,
        task_id=task_id,
        title=task["title"],
        summary=summary,
        changed_files=changed_files,
        watch_ci=True,
    )

    if res.success:
        if res.ci_status == "success":
            outcome = "успешно закоммичена, запушена и прошла CI!"
        elif res.ci_status == "not_found":
            outcome = "успешно закоммичена и запушена (CI не настроен)."
        else:
            outcome = "успешно закоммичена и запушена."
        await client.patch_task(
            task_id,
            status="done",
            metadata={
                "git_outcome": _git_outcome_payload(
                    res, source="telegram", pushed=True
                )
            },
        )
        await status_msg.edit_text(
            f"🎉 <b>Задача #{html.escape(task_display_number(task))} {outcome}</b>\n\n"
            f"Ветка: <code>{html.escape(res.branch or 'HEAD')}</code>\n"
            f"Коммит: <code>{html.escape(res.commit_hash or '—')}</code>\n\n"
            f"<i>{html.escape(res.message)}</i>",
            parse_mode="HTML",
        )
        await _redraw_task_card(
            callback.message,
            client,
            task_id,
            note="_🚀 Изменения закоммичены и запушены_",
        )
    else:
        ci_logs_part = f"\n\n<b>Логи сбоя CI:</b>\n<pre>{html.escape(res.ci_logs[:1000])}</pre>" if res.ci_logs else ""
        fix_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Обсудить ошибку",
                        callback_data=f"select_task:{task_id}",
                    )
                ]
            ]
        )
        await status_msg.edit_text(
            f"❌ <b>Ошибка при пуше или в CI для задачи #{html.escape(task_display_number(task))}:</b>\n\n"
            f"{html.escape(res.message)}"
            f"{ci_logs_part}",
            parse_mode="HTML",
            reply_markup=fix_kb,
        )
        await _redraw_task_card(
            callback.message,
            client,
            task_id,
            note="_❌ Пуш или CI не прошли — изменения остались в рабочей папке_",
        )


@router.callback_query(F.data.startswith("commit_local:"))
async def on_commit_local(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    await _commit_local_impl(callback, client, config, force=False)


@router.callback_query(F.data.startswith("commit_local_force:"))
async def on_commit_local_force(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    await _commit_local_impl(callback, client, config, force=True)


@router.callback_query(F.data.startswith("commit_push:"))
async def on_commit_push(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    await _commit_push_impl(callback, client, config, force=False)


@router.callback_query(F.data.startswith("commit_push_force:"))
async def on_commit_push_force(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    await _commit_push_impl(callback, client, config, force=True)


@router.callback_query(F.data.startswith("rollback:"))
async def on_rollback(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    task_id = callback.data.split(":")[1]
    await callback.answer("⏳ Откат изменений...")
    task = await client.get_task(task_id)
    ws = _task_workspace(task, config)
    meta = task.get("metadata") or {}
    changed_files = meta.get("changed_files") or []

    git_engine = GitEngine(default_workspace=ws)
    res = git_engine.rollback(workspace=ws, changed_files=changed_files)

    if res.success:
        await client.patch_task(
            task_id,
            status="cancelled",
            metadata={
                "git_outcome": _git_outcome_payload(res, source="telegram")
            },
        )
        await callback.message.edit_text(
            f"❌ <b>Изменения по задаче #{html.escape(task_display_number(task))} откатаны.</b>\n\n"
            f"Задача переведена в статус <code>cancelled</code>.\n"
            f"Рабочая директория возвращена к исходному состоянию.",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            f"⚠️ <b>Ошибка при откате изменений:</b>\n\n{html.escape(res.message)}",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("talk_task:"))
async def on_talk_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    task_id = callback.data.split(":")[1]
    task = await client.get_task(task_id)
    conv_id = task.get("metadata", {}).get("conversation_id")

    storage.set_user_mode(
        callback.from_user.id, "task", task_id=task_id, conversation_id=conv_id
    )
    await callback.answer("Диалог привязан к задаче")
    await callback.message.answer(
        f"💬 Напишите сообщение секретарю по задаче <b>{html.escape(task['title'])}</b>:",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cancel_queue:"))
async def on_cancel_queue(
    callback: CallbackQuery,
    storage: UserStateStorage,
) -> None:
    queue_id = callback.data.split(":")[1]
    cancelled = storage.cancel_queued_message(queue_id)
    if cancelled:
        await callback.message.edit_text("❌ Сообщение отменено из очереди.")
        await callback.answer("Сообщение отменено")
    else:
        await callback.answer(
            "⚠️ Сообщение уже находится в обработке, отмена невозможна.",
            show_alert=True,
        )


@router.callback_query(F.data == "action:new_task")
async def on_action_new_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
) -> None:
    storage.set_user_mode(callback.from_user.id, "new_task")
    await callback.message.answer(
        "➕ <b>Режим новой задачи</b>. Опишите задачу голосом или текстом:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "action:talk")
async def on_action_talk(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    state = storage.get_user_state(callback.from_user.id)
    talk_conv_id = state.talk_conversation_id
    if not talk_conv_id:
        conv = await client.create_conversation(
            title="Общий разговор (Telegram)",
            metadata={"origin": "telegram_talk", "user_id": callback.from_user.id},
        )
        talk_conv_id = conv["id"]
        storage.set_talk_conversation(callback.from_user.id, talk_conv_id)

    storage.set_user_mode(
        callback.from_user.id, "talk", conversation_id=talk_conv_id
    )
    await callback.message.answer(
        "💬 <b>Общий разговор</b>. Задайте вопрос секретарю:",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "action:reset_talk")
async def on_action_reset_talk(
    callback: CallbackQuery,
    storage: UserStateStorage,
) -> None:
    user_id = callback.from_user.id
    storage.set_talk_conversation(user_id, None)
    storage.set_user_mode(user_id, "talk", conversation_id=None)
    await callback.message.answer(
        "🔄 <b>Диалог сброшен.</b> Следующее сообщение начнёт новый разговор со секретарём.",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "action:live")
async def on_action_live(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    """Redraw the monitor message in place with fresh numbers."""
    try:
        payload = await client.get_execution_monitor()
    except Exception as error:
        await callback.answer(
            f"Не удалось обновить: {error}", show_alert=True
        )
        return
    await rich_edit_message(
        callback.message,
        format_execution_monitor(payload),
        reply_markup=execution_monitor_keyboard(),
    )
    await callback.answer("Обновлено")


@router.callback_query(F.data == "action:list_tasks")
async def on_action_list_tasks(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    tasks = await client.list_tasks(status="incoming")
    counts = await client.get_task_counts()
    active_talks = await _active_talk_conversations(client)
    await callback.message.answer(
        "📋 <b>Список задач:</b>",
        parse_mode="HTML",
        reply_markup=task_categories_keyboard(
            "incoming",
            tasks,
            counts=counts,
            active_talks_count=len(active_talks),
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("talk_agent:"))
async def on_talk_agent_list(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    """Show the paginated list of agents that accept direct talks."""
    raw_page = callback.data.split(":", 1)[1]
    page = int(raw_page) if raw_page.isdigit() else 1
    agents = await _talkable_agents(client)
    if not agents:
        await callback.message.answer(
            "👥 Нет агентов с флагом <code>talkable: true</code>.",
            parse_mode="HTML",
            reply_markup=talk_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.answer(
        "👥 <b>Выберите агента для прямого разговора</b>\n\n"
        "Разговор сохраняет сессию: вернуться и продолжить можно из этого же списка.",
        parse_mode="HTML",
        reply_markup=talk_agent_keyboard(agents, page=page),
    )
    await callback.answer("Выберите агента")


@router.callback_query(F.data.startswith("select_talk_agent:"))
async def on_select_talk_agent(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    """Create or resume the persistent conversation with one agent."""
    agent_id = callback.data.split(":", 1)[1]
    try:
        agent = await client.get_agent(agent_id)
    except Exception as error:
        logger.warning("talk agent lookup failed: %s", error)
        agent = None
    if agent is None or not _is_talkable_agent(agent):
        await callback.answer(
            "Агент недоступен для разговора", show_alert=True
        )
        return

    user_id = callback.from_user.id
    conversations = await _active_talk_conversations(client)
    try:
        all_conversations = await client.list_conversations()
    except Exception as error:
        logger.warning("talk conversation lookup failed: %s", error)
        all_conversations = []

    def matching(conversation: dict[str, Any]) -> bool:
        metadata = conversation.get("metadata") or {}
        if metadata.get("talkable_agent") != agent_id:
            return False
        if metadata.get("origin") != "telegram_agent_talk":
            return False
        owner = metadata.get("user_id")
        return owner is None or str(owner) == str(user_id)

    existing = [c for c in all_conversations if matching(c)]
    active_existing = [
        c
        for c in existing
        if c in conversations
        and (c.get("metadata") or {}).get("talk_closing") is not True
    ]
    usable = [
        c
        for c in existing
        if (c.get("metadata") or {}).get("talk_closing") is not True
    ]
    chosen = (active_existing or usable or existing or [None])[-1]
    if chosen is None:
        metadata = {
            "origin": "telegram_agent_talk",
            "user_id": user_id,
            "chat_id": (
                callback.message.chat.id
                if callback.message and callback.message.chat
                else None
            ),
            "talkable_agent": agent_id,
        }
        runner_config = agent.get("runner_config") or {}
        project = runner_config.get("project")
        if isinstance(project, str) and project:
            metadata["talk_workspace"] = project
        conversation = await client.create_conversation(
            title=f"Разговор с агентом {agent.get('name') or agent_id}",
            metadata=metadata,
        )
    else:
        conversation = chosen

    conversation_id = conversation["id"]
    try:
        conversation = await client.start_talk(conversation_id)
    except Exception as error:
        logger.warning("talk start failed: %s", error)
        await callback.answer(
            "Не удалось начать разговор", show_alert=True
        )
        return
    metadata = dict(conversation.get("metadata") or {})
    closing = metadata.get("talk_closing") is True
    active = metadata.get("talk_active") is True and not closing
    storage.set_talk_agent_conversation(user_id, conversation_id)
    name = agent.get("name") or agent_id
    if closing:
        workspace_note = (
            "Предыдущий разговор ещё завершается. Дождитесь освобождения "
            "workspace и выберите агента снова."
        )
    elif not active:
        workspace_note = (
            "Workspace занят задачей или другим разговором. Первое сообщение "
            "будет ждать освобождения; задачи этого агента тоже ждут."
        )
    else:
        workspace_note = (
            "Сообщения уходят этому агенту. Его workspace занят, пока "
            "разговор не завершён."
        )
    await callback.message.answer(
        f"💬 <b>Разговор с агентом {html.escape(str(name))}</b>\n"
        f"ID: <code>{html.escape(agent_id)}</code>\n"
        f"Диалог: <code>#{html.escape(conversation_id)}</code>\n\n"
        f"{workspace_note}",
        parse_mode="HTML",
        reply_markup=agent_conversation_keyboard(
            agent_id, conversation_id
        ),
    )
    await callback.answer("Разговор выбран")


@router.callback_query(F.data.startswith("end_talk:"))
async def on_end_talk(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    """End one direct-agent talk and release its workspace lock."""
    raw_id = callback.data.split(":", 1)[1].strip()
    user_id = callback.from_user.id
    state = storage.get_user_state(user_id)
    conversation_id = (
        raw_id
        or state.talk_agent_conversation_id
        or state.active_conversation_id
    )
    if not conversation_id:
        await callback.answer("Нет активного разговора", show_alert=True)
        return
    try:
        conversation = await client.get_conversation(conversation_id)
    except Exception as error:
        logger.warning("end talk lookup failed: %s", error)
        conversation = None
    if conversation is None:
        await callback.answer("Разговор недоступен", show_alert=True)
        return
    metadata = dict(conversation.get("metadata") or {})
    agent_id = metadata.get("talkable_agent")
    if not agent_id:
        await callback.answer(
            "Это не разговор с агентом", show_alert=True
        )
        return

    # Stop accepting new local messages immediately. Pending local messages
    # are cancelled; one already processing keeps the workspace lock until
    # its turn finishes.
    local_busy = storage.is_conversation_busy(conversation_id)
    cancelled = storage.cancel_pending_for_conversation(conversation_id)

    try:
        conversation = await client.request_end_talk(conversation_id)
    except Exception as error:
        logger.warning("end talk request failed: %s", error)
        await callback.answer(
            "Не удалось завершить разговор", show_alert=True
        )
        return
    metadata = dict(conversation.get("metadata") or {})

    released = False
    if not local_busy:
        try:
            conversation = await client.finalize_talk(conversation_id)
        except Exception as error:
            logger.warning("end talk finalize failed: %s", error)
        else:
            metadata = dict(conversation.get("metadata") or {})
            released = metadata.get("talk_active") is False

    if state.mode == "agent_talk" and conversation_id in (
        state.talk_agent_conversation_id,
        state.active_conversation_id,
    ):
        storage.set_user_mode(
            user_id, "talk", conversation_id=state.talk_conversation_id
        )

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if released:
        body = (
            "Workspace освобождён, задачи этого агента снова могут "
            "выполняться. Runner-сессия сохранена: при следующем выборе "
            "агента контекст продолжится."
        )
    else:
        body = (
            "Новые сообщения больше не принимаются. Workspace освободится "
            "автоматически после завершения текущего и уже поставленных "
            "ходов. Runner-сессия сохранится."
        )
    if cancelled:
        body += f" Отменено сообщений в очереди: {cancelled}."
    await callback.message.answer(
        f"🏁 <b>Разговор с агентом {html.escape(str(agent_id))} "
        f"{'завершён' if released else 'закрывается'}:</b>\n\n{body}",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )
    await callback.answer("Разговор завершён")


@router.callback_query(F.data == "action:active_talks")
async def on_action_active_talks(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    """Render active direct-agent talks with a per-conversation end button."""
    conversations = await _active_talk_conversations(client)
    agents = await _talkable_agents(client)
    if not conversations:
        await callback.message.answer(
            "🗣 Активных разговоров с агентами нет.",
            reply_markup=talk_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.answer(
        _active_talk_message(conversations, agents),
        parse_mode="HTML",
        reply_markup=active_talks_keyboard(conversations),
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# -- Messages (Text, Voice, Photo, Document) --


async def _route_reply_to_original_conversation(
    message: Message,
    *,
    client: BackendClient,
    queue_processor: QueueProcessor,
    text: str,
    media_type: str,
    media_path: str | None,
    audio_bytes: bytes | None,
) -> bool:
    """Route one explicit Telegram Reply into its original conversation.

    The durable link lookup happens before any active-mode logic, so the reply
    goes to the replied-to message's conversation regardless of the user's
    current context, and it never satisfies another task's clarification
    request. The reply itself does not switch the active context: plain
    messages after it keep going to the previously selected dialog until the
    switch button is pressed.

    Returns ``True`` when the message was fully handled (submitted to the
    linked conversation, or explained because the link is lost); ``False``
    when processing must continue through the ordinary active-context flow.
    """
    reply_to = message.reply_to_message
    if reply_to is None:
        return False
    try:
        link = await client.get_telegram_link(
            message.chat.id, reply_to.message_id
        )
    except Exception as error:
        logger.warning("telegram link lookup failed: %s", error)
        link = None

    if link is not None:
        context_task_id = link.get("task_id")
        context_mode = "task" if context_task_id else "talk"
        if not context_task_id:
            try:
                linked_conversation = await client.get_conversation(
                    link["conversation_id"]
                )
            except Exception as error:
                logger.warning(
                    "reply target conversation lookup failed: %s", error
                )
                linked_conversation = None
            if linked_conversation is not None:
                metadata = linked_conversation.get("metadata") or {}
                if isinstance(metadata, dict) and metadata.get(
                    "talkable_agent"
                ):
                    if (
                        metadata.get("talk_active") is True
                        and metadata.get("talk_closing") is not True
                    ):
                        context_mode = "agent_talk"
                    else:
                        logger.info(
                            "telegram reply to inactive agent talk refused",
                            extra={
                                "event": "telegram_reply_route",
                                "basis": "agent_talk_inactive",
                                "conversation_id": link["conversation_id"],
                                "telegram_message_id": message.message_id,
                            },
                        )
                        await message.answer(
                            "⚠️ Этот разговор с агентом завершён или ещё не "
                            "активирован. Новый ход не отправлен. Выберите "
                            "агента заново через /talk → «👥 Выбрать агента» "
                            "или нажмите кнопку продолжения диалога.",
                            reply_markup=talk_keyboard(),
                        )
                        return True
        logger.info(
            "telegram reply routed to original conversation",
            extra={
                "event": "telegram_reply_route",
                "basis": "link",
                "conversation_id": link["conversation_id"],
                "task_id": context_task_id,
                "telegram_message_id": message.message_id,
                "replied_to_message_id": reply_to.message_id,
            },
        )
        await queue_processor.submit_message(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            context_mode=context_mode,
            context_task_id=context_task_id,
            context_conversation_id=link["conversation_id"],
            text=text,
            media_type=media_type,
            media_path=media_path,
            audio_bytes=audio_bytes,
        )
        return True

    from_bot = (
        reply_to.from_user is not None
        and message.bot is not None
        and reply_to.from_user.id == message.bot.id
    )
    if from_bot:
        logger.info(
            "telegram reply to a bot message with a lost link",
            extra={
                "event": "telegram_reply_route",
                "basis": "lost",
                "telegram_message_id": message.message_id,
                "replied_to_message_id": reply_to.message_id,
            },
        )
        await message.answer(
            "⚠️ Не удалось определить исходный диалог: связь сообщения потеряна "
            "или диалог удалён. Ответ не отправлен — выберите диалог или задачу:",
            reply_markup=mode_keyboard(),
        )
        return True

    # A reply to a human message without a link (foreign or predating the
    # link registry) is ordinary input; never guess a dialog from quoted text.
    logger.info(
        "telegram reply to an unlinked message continues as ordinary input",
        extra={
            "event": "telegram_reply_route",
            "basis": "foreign",
            "telegram_message_id": message.message_id,
            "replied_to_message_id": reply_to.message_id,
        },
    )
    return False


@router.message(F.text | F.voice | F.photo | F.document)
async def on_user_message(
    message: Message,
    storage: UserStateStorage,
    client: BackendClient,
    queue_processor: QueueProcessor,
    config: TelegramConfig,
) -> None:
    user_id = message.from_user.id
    state = storage.get_user_state(user_id)

    # 1. Idle mode check — an explicit Reply is itself a dialog action and is
    # routed below even from idle mode.
    if state.mode == "idle" and message.reply_to_message is None:
        await message.answer(
            "⚠️ Режим не выбран. Пожалуйста, выберите действие:",
            reply_markup=mode_keyboard(),
        )
        return

    # Extract text and media
    media_type = "text"
    text = ""
    audio_bytes: bytes | None = None
    media_path: str | None = None

    if message.voice:
        media_type = "voice"
        text = "(Голосовое сообщение)"
        # Download audio
        try:
            file_info = await message.bot.get_file(message.voice.file_id)
            if file_info.file_path:
                audio_stream = await message.bot.download_file(file_info.file_path)
                audio_bytes = audio_stream.read()
                # Persist locally
                config.media_dir.mkdir(parents=True, exist_ok=True)
                local_file = config.media_dir / f"voice_{message.voice.file_id}.ogg"
                local_file.write_bytes(audio_bytes)
                media_path = str(local_file)
        except Exception as e:
            logger.warning("Failed to download voice file: %s", e)
    elif message.photo:
        media_type = "photo"
        text = message.caption or "(Изображение)"
        try:
            photo = message.photo[-1]
            file_info = await message.bot.get_file(photo.file_id)
            if file_info.file_path:
                photo_stream = await message.bot.download_file(file_info.file_path)
                config.media_dir.mkdir(parents=True, exist_ok=True)
                local_file = config.media_dir / f"photo_{photo.file_id}.jpg"
                local_file.write_bytes(photo_stream.read())
                media_path = str(local_file)
        except Exception as e:
            logger.warning("Failed to download photo file: %s", e)
    elif message.document:
        media_type = "document"
        caption = message.caption or ""
        doc = message.document
        doc_filename = Path(doc.file_name or "document").name
        target_filename = f"doc_{doc.file_id[:8]}_{doc_filename}"
        try:
            file_info = await message.bot.get_file(doc.file_id)
            if file_info.file_path:
                doc_stream = await message.bot.download_file(file_info.file_path)
                config.media_dir.mkdir(parents=True, exist_ok=True)
                local_file = config.media_dir / target_filename
                local_file.write_bytes(doc_stream.read())
                media_path = str(local_file)
        except Exception as e:
            logger.warning("Failed to download document file: %s", e)

        # Inline text preview if it's a small text-based document
        doc_size = doc.file_size or 0
        is_text = False
        text_content: str | None = None
        if media_path and doc_size <= 150_000:
            try:
                raw_bytes = Path(media_path).read_bytes()
                text_content = raw_bytes.decode("utf-8")
                is_text = True
            except Exception:
                is_text = False

        if is_text and text_content is not None:
            ext = Path(doc_filename).suffix.lstrip(".") or "text"
            file_block = f"📄 Файл `{doc_filename}` ({doc_size} байт):\n```{ext}\n{text_content}\n```"
            text = f"{caption}\n\n{file_block}" if caption else file_block
        else:
            notice = f"📎 Прикреплен файл: `{doc_filename}` ({doc_size} байт)"
            text = f"{caption}\n\n{notice}" if caption else notice
    else:
        text = message.text or ""

    # 1.5 Reply routing: an explicit Reply targets the original conversation
    # of the replied-to message, resolved before the active-mode and
    # task-clarification branches so it can never be intercepted (in
    # particular, it must not resume an unrelated waiting task). Media is
    # already extracted above, so voice/photo/document replies keep their
    # transcription and file handling.
    if message.reply_to_message is not None:
        routed = await _route_reply_to_original_conversation(
            message,
            client=client,
            queue_processor=queue_processor,
            text=text,
            media_type=media_type,
            media_path=media_path,
            audio_bytes=audio_bytes,
        )
        if routed:
            return

    # 2. Context resolution
    context_mode = state.mode
    context_task_id = state.active_task_id
    context_conversation_id = state.active_conversation_id

    if context_mode == "new_task":
        # First message in new_task mode creates the task draft and conversation
        title_summary = (
            text[:50] + "..." if len(text) > 50 else (text or "Новая задача")
        )
        task = await client.create_task(
            title=title_summary,
            status="incoming",
            metadata={
                "created_from": "telegram_new_task",
                "chat_id": message.chat.id,
                "user_id": user_id,
            },
        )
        context_task_id = task["id"]
        conv = await client.create_conversation(
            title=f"Задача #{task_display_number(task)}: {title_summary}",
            metadata={"task_id": context_task_id},
        )
        context_conversation_id = conv["id"]
        # Link conversation into task metadata
        await client.patch_task(
            context_task_id,
            metadata={
                "created_from": "telegram_new_task",
                "conversation_id": context_conversation_id,
                "chat_id": message.chat.id,
                "user_id": user_id,
            },
        )
        # Switch mode to task context
        storage.set_user_mode(
            user_id,
            "task",
            task_id=context_task_id,
            conversation_id=context_conversation_id,
        )
        context_mode = "task"

    elif context_mode == "talk":
        if not context_conversation_id:
            conv = await client.create_conversation(
                title="Общий разговор (Telegram)",
                metadata={"origin": "telegram_talk", "user_id": user_id},
            )
            context_conversation_id = conv["id"]
            storage.set_talk_conversation(user_id, context_conversation_id)
            storage.set_user_mode(
                user_id, "talk", conversation_id=context_conversation_id
            )

    elif context_mode == "agent_talk":
        context_conversation_id = (
            context_conversation_id or state.talk_agent_conversation_id
        )
        if not context_conversation_id:
            await message.answer(
                "⚠️ Разговор с агентом не выбран. Откройте /talk и выберите агента.",
                reply_markup=mode_keyboard(),
            )
            return

    elif context_mode == "task":
        if context_task_id:
            task = await client.get_task(context_task_id)

            # If task is waiting for user clarification, user's message resumes the task!
            if task.get("status") == "waiting_for_input":
                clar_reply_text = text
                if media_type == "voice" and audio_bytes:
                    transcribed = await queue_processor.transcriber.transcribe(audio_bytes)
                    if transcribed:
                        clar_reply_text = transcribed

                meta = dict(task.get("metadata") or {})
                meta["last_clarification_response"] = clar_reply_text
                clar = meta.get("clarification") or dict(meta.get("provenance") or {}).get("clarification") or {}
                q_text = clar.get("question") or "Уточнение"

                updated_desc = (task.get("description") or "").strip()
                updated_desc += f"\n\n### Ответ на уточнение:\n- **Вопрос:** {q_text}\n- **Ответ:** {clar_reply_text}\n"

                await client.patch_task(
                    context_task_id,
                    status="in_progress",
                    description=updated_desc,
                    metadata=meta,
                )
                await message.answer(
                    f"✅ <b>Ответ принят.</b>\n\n⚙️ <i>Агент возобновляет выполнение задачи #{task_display_number(task)}...</i>",
                    parse_mode="HTML",
                    reply_markup=task_card_keyboard(context_task_id, "in_progress"),
                )
                return

            if not context_conversation_id:
                context_conversation_id = task.get("metadata", {}).get("conversation_id")
                if not context_conversation_id:
                    conv = await client.create_conversation(
                        title=f"Контекст задачи {context_task_id}",
                        metadata={"task_id": context_task_id},
                    )
                    context_conversation_id = conv["id"]
                    meta = dict(task.get("metadata") or {})
                    meta["conversation_id"] = context_conversation_id
                    await client.patch_task(context_task_id, metadata=meta)
                    storage.set_user_mode(
                        user_id,
                        "task",
                        task_id=context_task_id,
                        conversation_id=context_conversation_id,
                    )

    if not context_conversation_id:
        await message.answer(
            "⚠️ Не удалось определить контекст диалога. Пожалуйста, выберите режим:",
            reply_markup=mode_keyboard(),
        )
        return

    # 3. Submit message through queue processor
    await queue_processor.submit_message(
        user_id=user_id,
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        context_mode=context_mode,
        context_task_id=context_task_id,
        context_conversation_id=context_conversation_id,
        text=text,
        media_type=media_type,
        media_path=media_path,
        audio_bytes=audio_bytes,
    )
