"""Unified Telegram messenger: rendering, sending and the durable outbox."""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest

from kuzya.storage import ConversationRepository, Database, TaskRepository
from kuzya.storage.telegram_delivery import (
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENT,
    INPUT_SOURCE_TELEGRAM,
    TelegramDeliveryRepository,
    reply_delivery_key,
)
from kuzya.secretary import TurnProcessor
from kuzya.telegram.keyboards import (
    agent_conversation_keyboard,
    dialog_switch_keyboard,
)
from kuzya.telegram.messenger import (
    TelegramDeliveryService,
    api_question_header,
    compose_dialog_message,
    conversation_hashtag,
    format_verified_caller,
    markdown_hashtag_safe,
    render_foreign_context_plate,
    send_markdown,
    split_for_telegram,
    with_conversation_footer,
)
from kuzya.telegram.state import UserStateStorage
from kuzya.telegram.worker import QueueProcessor


def _bot(message_id: int = 501) -> AsyncMock:
    bot = AsyncMock()
    bot.send_rich_message.return_value.message_id = message_id
    return bot


def _service(migrated_db, bot, **kwargs) -> TelegramDeliveryService:
    return TelegramDeliveryService(
        bot=bot,
        database=Database.from_path(migrated_db),
        claim_grace_seconds=0.0,
        **kwargs,
    )


def test_format_verified_caller_only_trusts_principal_shapes() -> None:
    assert format_verified_caller(None) is None
    assert format_verified_caller("admin") == "администратор"
    assert format_verified_caller("execution:agy|build") == "агент: agy"
    assert format_verified_caller("execution:solo") == "агент: solo"
    # Free-form junk never renders as a confirmed identity.
    assert format_verified_caller("random string") is None


def test_api_question_header_variants() -> None:
    assert api_question_header() == "📨 Вопрос секретарю через API"
    assert (
        api_question_header("execution:agy|build")
        == "📨 Вопрос секретарю через API (агент: agy)"
    )
    assert (
        api_question_header("admin")
        == "📨 Вопрос секретарю через API (администратор)"
    )


def test_foreign_context_plate_snapshot() -> None:
    assert render_foreign_context_plate() is None
    assert (
        render_foreign_context_plate(active_task_number=41)
        == "↩️ Другой диалог · сейчас активна задача №41"
    )
    assert (
        render_foreign_context_plate(active_conversation_id="abc")
        == "↩️ Другой диалог"
    )


def test_split_for_telegram_footers_every_part() -> None:
    conversation_id = "a" * 32
    composed = compose_dialog_message(
        header="📨 Вопрос секретарю через API",
        body="т" * 9000,
    )
    parts = split_for_telegram(composed, conversation_id)
    assert len(parts) == 3
    assert all(len(part) <= 4096 for part in parts)
    assert all(part.endswith(conversation_hashtag(conversation_id)) for part in parts)
    assert "".join(
        part[: -len(conversation_hashtag(conversation_id))].rstrip() for part in parts
    ).endswith("т")


def test_deliver_api_question_copy_with_uuid_and_button(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 77})
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Что с релизом?",
            verified_caller="execution:agy|build",
        )
        bot = _bot(501)
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1

        bot.send_rich_message.assert_awaited_once()
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["chat_id"] == 77
        assert kwargs["rich_message"].markdown == (
            "📨 Вопрос секретарю через API (агент: agy)\n\n"
            f"Что с релизом?\n\n`#{conversation.id}`"
        )
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conversation.id
        )

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_SENT
        link = repo.get_message_link(77, 501)
        assert link is not None
        assert link.conversation_id == conversation.id
        assert link.message_id == question.message.id
        assert link.origin == "bot"
    finally:
        database.dispose()


def test_task_bound_conversation_gets_switch_task_button(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 88})
        task = tasks.create(
            title="Платёжный модуль",
            status="in_progress",
            metadata={"conversation_id": conversation.id},
        )
        conversations.append_message(
            conversation_id=conversation.id, role="user", text="Как дела?"
        )
        bot = _bot(601)
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conversation.id,
            task_id=task.id,
            task_number=task.number,
        )
    finally:
        database.dispose()


def test_reply_row_header_for_api_input_and_plain_for_telegram(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        processor = TurnProcessor(database)
        service = _service(migrated_db, _bot(701))

        api_conv = conversations.create_conversation(metadata={"chat_id": 1})
        q1 = conversations.append_message(
            conversation_id=api_conv.id, role="user", text="API вопрос"
        )
        processor.enqueue(conversation_id=api_conv.id, input_message_id=q1.message.id)
        turn1 = processor.claim_next(conversation_id=api_conv.id)
        processor.complete(turn1.id, turn1.claim_token, text="API ответ")

        tg_conv = conversations.create_conversation(metadata={"chat_id": 2})
        q2 = conversations.append_message(
            conversation_id=tg_conv.id,
            role="user",
            text="TG вопрос",
            source="telegram",
        )
        processor.enqueue(conversation_id=tg_conv.id, input_message_id=q2.message.id)
        turn2 = processor.claim_next(conversation_id=tg_conv.id)
        processor.complete(turn2.id, turn2.claim_token, text="TG ответ")

        # Three delivery rows: the API question copy, its reply, and the
        # telegram-originated reply (the telegram question itself is already
        # in the chat and is never copied).
        assert asyncio.run(service.deliver_pending()) == 3
        texts = {
            call.kwargs["rich_message"].markdown
            for call in service.bot.send_rich_message.await_args_list
        }
        # The API question copy and reply keep their distinguishing headers;
        # the telegram-originated reply stays a bare answer.
        assert any(t.startswith("📨 Вопрос секретарю через API") for t in texts)
        assert (
            f"💬 Ответ секретаря (вопрос был через API)\n\nAPI ответ\n\n`#{api_conv.id}`"
            in texts
        )
        assert f"TG ответ\n\n`#{tg_conv.id}`" in texts
    finally:
        database.dispose()


def test_no_recipient_keeps_row_pending(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Куда доставлять?"
        )
        bot = _bot()
        service = _service(migrated_db, bot)
        # The row is claimed, diagnosed and released — nothing counts as
        # delivered while the recipient is unknown.
        assert asyncio.run(service.deliver_pending()) == 0
        bot.send_rich_message.assert_not_awaited()
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_PENDING
    finally:
        database.dispose()


def test_send_error_with_unknown_outcome_requeues(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 99})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Вопрос"
        )
        bot = _bot()
        for method in (bot.send_rich_message, bot.send_message, bot.send_document):
            method.side_effect = RuntimeError("network timeout")
        service = _service(migrated_db, bot)
        # Attempted with an unknown outcome: released back to pending, the
        # pass stops instead of spinning on the same row.
        assert asyncio.run(service.deliver_pending()) == 0
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_PENDING

        # The retry after an "unknown outcome" may resend the same part —
        # at-least-once by design — and then completes.
        for method in (bot.send_rich_message, bot.send_message, bot.send_document):
            method.side_effect = None
        bot.send_rich_message.return_value.message_id = 801
        assert asyncio.run(service.deliver_pending()) == 1
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_SENT
    finally:
        database.dispose()


def test_definite_telegram_rejection_marks_failed(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 99})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Вопрос"
        )
        bot = _bot()
        error = TelegramBadRequest(method="sendRichMessage", message="chat not found")
        for method in (bot.send_rich_message, bot.send_message, bot.send_document):
            method.side_effect = error
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == "failed"
        assert "chat not found" in row.error
    finally:
        database.dispose()


def test_foreign_plate_rendered_when_active_context_differs(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 55})
        conversations.append_message(
            conversation_id=conversation.id, role="user", text="Фоновый вопрос"
        )
        bot = _bot(901)
        service = _service(
            migrated_db,
            bot,
            active_context_probe=lambda: {
                "mode": "task",
                "conversation_id": "other",
                "task_number": 41,
            },
        )
        assert asyncio.run(service.deliver_pending()) == 1
        markdown = bot.send_rich_message.await_args.kwargs["rich_message"].markdown
        assert "↩️ Другой диалог · сейчас активна задача №41" in markdown
        assert markdown.endswith(f"`#{conversation.id}`")
    finally:
        database.dispose()


def test_claim_grace_defers_fresh_rows_to_synchronous_worker(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 66})
        conversations.append_message(
            conversation_id=conversation.id, role="user", text="Свежий вопрос"
        )
        bot = _bot()
        service = TelegramDeliveryService(
            bot=bot,
            database=database,
            claim_grace_seconds=60.0,
        )
        assert asyncio.run(service.deliver_pending()) == 0
        bot.send_rich_message.assert_not_awaited()
    finally:
        database.dispose()


def test_worker_and_outbox_cannot_double_send_a_reply(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 33})
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос",
            source="telegram",
        )
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id=conversation.id, input_message_id=question.message.id
        )
        turn = processor.claim_next(conversation_id=conversation.id)
        processor.complete(turn.id, turn.claim_token, text="Ответ")

        bot = _bot()
        service = _service(migrated_db, bot)
        first = service.claim_reply_delivery(turn.id)
        assert first is not None
        # The outbox (and any second worker) loses the claim and must not send.
        assert service.claim_reply_delivery(turn.id) is None
        assert asyncio.run(service.deliver_pending()) == 0
        bot.send_rich_message.assert_not_awaited()

        service.record_delivery_sent(
            first, chat_id=33, telegram_message_ids=[444, 445]
        )
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row.status == DELIVERY_STATUS_SENT
        assert row.telegram_message_ids == [444, 445]
        assert repo.get_message_link(33, 444).origin == "bot"
        assert repo.get_message_link(33, 445).origin == "bot"
    finally:
        database.dispose()


def _worker_turn(migrated_db, *, reply_text: str, source: str = INPUT_SOURCE_TELEGRAM):
    """A completed telegram-origin turn plus mocks for QueueProcessor."""
    database = Database.from_path(migrated_db)
    conversations = ConversationRepository(database)
    conversation = conversations.create_conversation(metadata={"chat_id": 55})
    question = conversations.append_message(
        conversation_id=conversation.id,
        role="user",
        text="вопрос",
        source=source,
        metadata={"chat_id": 55, "telegram_message_id": 100},
    )
    processor = TurnProcessor(database)
    processor.enqueue(
        conversation_id=conversation.id, input_message_id=question.message.id
    )
    turn = processor.claim_next(conversation_id=conversation.id)
    processor.complete(turn.id, turn.claim_token, text=reply_text)

    client = MagicMock()
    client.append_message = AsyncMock(return_value={"id": question.message.id})
    client.create_turn = AsyncMock(return_value={"id": turn.id})
    client.get_turn = AsyncMock(
        return_value={"status": "completed", "reply_message_id": "rep-1"}
    )
    client.list_messages = AsyncMock(
        return_value=[{"id": "rep-1", "text": reply_text}]
    )
    return database, conversation, turn, client


def _queue_processor(bot, client, tmp_path, conversation_id, *, knowledge_base=None):
    storage = UserStateStorage(tmp_path / "state.db")
    queued = storage.enqueue_message(
        user_id=1,
        chat_id=55,
        telegram_message_id=100,
        context_mode="talk",
        context_task_id=None,
        context_conversation_id=conversation_id,
        text="вопрос",
    )
    processor = QueueProcessor(
        bot=bot,
        backend_client=client,
        storage=storage,
        transcriber=MagicMock(),
        knowledge_base=knowledge_base,
        poll_interval_seconds=0.01,
        turn_timeout_seconds=2.0,
    )
    return storage, queued, processor


def test_worker_file_failure_releases_claim_and_outbox_resumes(
    migrated_db, tmp_path
) -> None:
    """A failed file send must not close the delivery; the outbox finishes it."""
    report = tmp_path / "report.md"
    report.write_text("данные", encoding="utf-8")
    reply_text = "Вот файл:\n[SEND_FILE: report]"
    database, conversation, turn, client = _worker_turn(
        migrated_db, reply_text=reply_text
    )
    try:
        bot = AsyncMock()
        bot.send_rich_message.return_value.message_id = 321
        bot.send_document = AsyncMock(side_effect=RuntimeError("upload failed"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=901))
        knowledge_base = MagicMock()
        knowledge_base.resolve_file = MagicMock(return_value=report)

        storage, queued, processor = _queue_processor(
            bot, client, tmp_path, conversation.id, knowledge_base=knowledge_base
        )
        service = TelegramDeliveryService(
            bot=bot, database=database, claim_grace_seconds=0.0
        )
        processor.delivery_service = service
        asyncio.run(processor.process_message(queued))

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        # Not sent: the failed file released the claim for an outbox retry,
        # and a retry notice went to the user instead of a generic error.
        assert row.status == DELIVERY_STATUS_PENDING
        assert row.parts_sent == 1  # the text part is durably accounted
        rich_texts = [
            call.kwargs["rich_message"].markdown
            for call in bot.send_rich_message.await_args_list
        ]
        assert any("Вот файл" in text for text in rich_texts)
        notices = [
            call.kwargs.get("text", "")
            for call in bot.send_message.await_args_list
        ]
        assert any("Не удалось отправить файл" in text for text in notices)
        assert not any("Не удалось обработать" in text for text in notices)

        # The outbox resumes from the shared part plan: only the file part is
        # (re)sent, the delivered text is not duplicated.
        retry_bot = AsyncMock()
        retry_bot.send_rich_message.return_value.message_id = 950
        retry_bot.send_document = AsyncMock(return_value=MagicMock(message_id=951))
        retry_service = TelegramDeliveryService(
            bot=retry_bot,
            database=database,
            knowledge_base=knowledge_base,
            claim_grace_seconds=0.0,
        )
        assert asyncio.run(retry_service.deliver_pending()) == 1
        retry_bot.send_rich_message.assert_not_awaited()
        retry_bot.send_document.assert_awaited_once()
        caption = retry_bot.send_document.await_args.kwargs["caption"]
        assert caption.endswith(f"#{conversation.id}")
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row.status == DELIVERY_STATUS_SENT
    finally:
        database.dispose()


def test_worker_unexpected_error_releases_claim(migrated_db, tmp_path) -> None:
    """An exception after the claim must hand the delivery back, not hang it."""
    database, conversation, turn, client = _worker_turn(
        migrated_db, reply_text="Обычный ответ"
    )
    try:
        bot = AsyncMock()
        # No status bubble: the text path dies at the rich send and its
        # document fallback, reaching the generic error handler.
        bot.send_rich_message = AsyncMock(side_effect=RuntimeError("rich dead"))
        bot.send_document = AsyncMock(side_effect=RuntimeError("upload dead"))

        storage, queued, processor = _queue_processor(
            bot, client, tmp_path, conversation.id
        )
        service = TelegramDeliveryService(
            bot=bot, database=database, claim_grace_seconds=0.0
        )
        processor.delivery_service = service
        asyncio.run(processor.process_message(queued))

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row.status == DELIVERY_STATUS_PENDING
        assert storage.get_queued_message(queued.id).status == "failed"
        # The generic error notice carries the dialog UUID.
        notices = [
            call.kwargs.get("text", "")
            for call in bot.send_message.await_args_list
        ]
        assert any(
            "Не удалось обработать" in text and text.endswith(f"#{conversation.id}")
            for text in notices
        )
    finally:
        database.dispose()


def test_record_part_progress_persists_progress_and_link_together(migrated_db) -> None:
    """One call commits both the part progress and its Reply link."""
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 66})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Q"
        )
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert repo.claim_delivery(row.id) is True
        repo.record_part_progress(
            row.id,
            part_index=0,
            chat_id=66,
            telegram_message_id=777,
            conversation_id=conversation.id,
            message_id=question.message.id,
            origin="bot",
        )
        stored = repo.get_delivery_by_idempotency_key(row.idempotency_key)
        assert stored.parts_sent == 1
        assert stored.telegram_message_ids == [777]
        link = repo.get_message_link(66, 777)
        assert link is not None
        assert link.conversation_id == conversation.id
        assert link.message_id == question.message.id
    finally:
        database.dispose()


def test_send_markdown_document_fallback_caption_carries_uuid() -> None:
    bot = AsyncMock()
    for method in (bot.send_rich_message, bot.send_message):
        method.side_effect = RuntimeError("no text transport")
    bot.send_document = AsyncMock(return_value=MagicMock(message_id=301))

    assert (
        asyncio.run(
            send_markdown(bot, 42, "текст части", conversation_id="b" * 32)
        )
        == 301
    )
    caption = bot.send_document.await_args.kwargs["caption"]
    assert caption == f"📄 Текст прикреплён файлом:\n\n#{'b' * 32}"


def test_retry_reuses_frozen_plan_when_context_plate_changes(
    migrated_db, tmp_path
) -> None:
    """The part plan must survive a context switch between attempts.

    Regression: the plate was re-rendered on retry. After the user switched
    into the dialog, the plate disappeared, the long text re-split into fewer
    parts, and the saved parts_sent over-counted — the retry skipped the
    undelivered file and marked the row sent.
    """
    from aiogram.types import FSInputFile

    report = tmp_path / "report.md"
    report.write_text("данные", encoding="utf-8")
    body = "т" * 4050
    reply_text = body + "\n[SEND_FILE: report]"
    database, conversation, turn, client = _worker_turn(
        migrated_db, reply_text=reply_text
    )
    try:
        bot = AsyncMock()
        bot.send_rich_message.return_value.message_id = 321
        bot.send_document = AsyncMock(side_effect=RuntimeError("upload failed"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=901))
        knowledge_base = MagicMock()
        knowledge_base.resolve_file = MagicMock(return_value=report)

        storage, queued, processor = _queue_processor(
            bot, client, tmp_path, conversation.id, knowledge_base=knowledge_base
        )
        # The user's active context is elsewhere: the reply carries the
        # "other dialog" plate, which pushes the text over the 4096 split
        # into two parts.
        storage.set_user_mode(1, "talk", conversation_id="other-conv")
        service = TelegramDeliveryService(
            bot=bot, database=database, claim_grace_seconds=0.0
        )
        processor.delivery_service = service
        asyncio.run(processor.process_message(queued))

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row.status == DELIVERY_STATUS_PENDING
        assert row.parts_sent == 2  # both plate-rendered text parts accounted
        assert row.meta["plate"] == "↩️ Другой диалог"

        # Retry with no context probe at all: a re-rendered plate would be
        # absent, the text would re-split into ONE part and the file would
        # shift to index 1 — a recomputed plan would skip it entirely.
        retry_bot = AsyncMock()
        retry_bot.send_rich_message.return_value.message_id = 950
        retry_bot.send_document = AsyncMock(return_value=MagicMock(message_id=951))
        retry_service = TelegramDeliveryService(
            bot=retry_bot,
            database=database,
            knowledge_base=knowledge_base,
            claim_grace_seconds=0.0,
        )
        assert asyncio.run(retry_service.deliver_pending()) == 1
        retry_bot.send_rich_message.assert_not_awaited()
        retry_bot.send_document.assert_awaited_once()
        sent_document = retry_bot.send_document.await_args.kwargs["document"]
        assert isinstance(sent_document, FSInputFile)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row.status == DELIVERY_STATUS_SENT
    finally:
        database.dispose()


def test_markdown_hashtag_safe_shrinks_every_leading_hash_line() -> None:
    hex32 = "a" * 32
    text = (
        f"# Ответ агента\n\n"
        f"#94 · задача · агент dev\n\n"
        f"Текст с номером #41.\n\n"
        f"#{hex32}"
    )
    safe = markdown_hashtag_safe(text)
    # A real heading becomes bold at normal size, a hashtag token (task
    # number, footer id) becomes a small inline code span...
    assert safe == (
        f"**Ответ агента**\n\n"
        f"`#94` · задача · агент dev\n\n"
        f"Текст с номером #41.\n\n"
        f"`#{hex32}`"
    )
    # ...and mid-line hashtags keep their original shape.
    assert "Текст с номером #41." in safe


def test_markdown_hashtag_safe_is_idempotent_and_wraps_short_ids() -> None:
    assert markdown_hashtag_safe("#") == "`#`"
    assert markdown_hashtag_safe("##") == "`##`"
    assert markdown_hashtag_safe("текст\n\n#conv-001") == "текст\n\n`#conv-001`"
    once = markdown_hashtag_safe("#94 · задача\n\n#abc123")
    assert markdown_hashtag_safe(once) == once


def test_markdown_hashtag_safe_leaves_code_fences_verbatim() -> None:
    text = "```python\n# комментарий\nx = 1\n```\n\n## Заголовок"
    safe = markdown_hashtag_safe(text)
    assert "# комментарий" in safe
    assert "x = 1" in safe
    assert safe.endswith("**Заголовок**")


def test_outbox_delivers_agent_talk_reply_with_agent_buttons(
    migrated_db,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(
            metadata={
                "chat_id": 90,
                "talkable_agent": "devops-docker3",
                "talk_active": True,
            }
        )
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="проверь nginx",
            source=INPUT_SOURCE_TELEGRAM,
        )
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id=conversation.id,
            input_message_id=question.message.id,
        )
        turn = processor.claim_next(conversation_id=conversation.id)
        processor.complete(turn.id, turn.claim_token, text="nginx работает")

        bot = _bot(801)
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["reply_markup"] == agent_conversation_keyboard(
            "devops-docker3", conversation.id
        )
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert f"end_talk:{conversation.id}" in callbacks
        assert "action:talk" in callbacks
    finally:
        database.dispose()
