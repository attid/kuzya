"""Asynchronous message processor for the Telegram bot queue."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile, InputRichMessage

from kuzya.knowledge import KnowledgeBase, SEND_FILE_PATTERN
from .client import BackendClient
from .keyboards import (
    agent_conversation_keyboard,
    cancel_queue_keyboard,
    dialog_switch_keyboard,
    talk_keyboard,
)
from .messenger import (
    TelegramDeliveryService,
    build_delivery_parts,
    compose_dialog_message,
    markdown_hashtag_safe,
    render_foreign_context_plate,
    strip_file_directives,
    with_conversation_footer,
)
from .state import QueuedMessage, UserStateStorage
from .transcription import DeepgramTranscriber

logger = logging.getLogger(__name__)


def secretary_error_summary(error: str) -> str:
    """Keep technical tracebacks in the journal, not in the chat bubble."""
    if "model_connection_error" in error:
        return "Не удалось связаться с провайдером модели. Попробуйте повторить сообщение."
    lines = error.strip().splitlines()
    return html.escape(lines[-1] if lines else "Неизвестная ошибка выполнения.")


def task_context_header(
    *, task_id: Any, task_number: Any, task_title: Any
) -> str:
    """Render the explicit task marker prepended to a contextual message."""
    return (
        f"[Контекст задачи #{task_number}: «{task_title}» "
        f"(id: {task_id})]"
    )


class QueueProcessor:
    """Processes messages sequentially per conversation."""

    def __init__(
        self,
        bot: Bot,
        backend_client: BackendClient,
        storage: UserStateStorage,
        transcriber: DeepgramTranscriber,
        *,
        knowledge_base: KnowledgeBase | None = None,
        poll_interval_seconds: float = 0.5,
        turn_timeout_seconds: float = 120.0,
        delivery_service: TelegramDeliveryService | None = None,
    ) -> None:
        self.bot = bot
        self.client = backend_client
        self.storage = storage
        self.transcriber = transcriber
        self.knowledge_base = knowledge_base
        self.poll_interval_seconds = poll_interval_seconds
        self.turn_timeout_seconds = turn_timeout_seconds
        self.delivery_service = delivery_service

    async def submit_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        telegram_message_id: int,
        context_mode: str,
        context_task_id: str | None,
        context_conversation_id: str,
        text: str,
        media_type: str = "text",
        media_path: str | None = None,
        audio_bytes: bytes | None = None,
    ) -> QueuedMessage:
        """Enqueue message and either dispatch immediately or show queue indicator."""
        is_busy = self.storage.is_conversation_busy(context_conversation_id)

        if is_busy:
            # Enqueue as pending with cancel button
            queued_msg = self.storage.enqueue_message(
                user_id=user_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                context_mode=context_mode,
                context_task_id=context_task_id,
                context_conversation_id=context_conversation_id,
                text=text,
                media_type=media_type,
                media_path=media_path,
            )
            try:
                status_msg = await self.bot.send_message(
                    chat_id=chat_id,
                    text="⏳ Сообщение в очереди на обработку.",
                    reply_markup=cancel_queue_keyboard(queued_msg.id),
                    reply_to_message_id=telegram_message_id,
                )
                self.storage.update_status_message_id(
                    queued_msg.id, status_msg.message_id
                )
            except Exception as e:
                logger.warning("Failed to send queue status message: %s", e)
            return queued_msg

        # Immediately claim and process
        queued_msg = self.storage.enqueue_message(
            user_id=user_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            context_mode=context_mode,
            context_task_id=context_task_id,
            context_conversation_id=context_conversation_id,
            text=text,
            media_type=media_type,
            media_path=media_path,
        )
        self.storage.set_message_status(queued_msg.id, "processing")

        initial_status_text = (
            "🎙 Расшифровываю аудио..."
            if media_type == "voice"
            else "⏳ Жду ответ секретаря..."
        )
        try:
            status_msg = await self.bot.send_message(
                chat_id=chat_id,
                text=initial_status_text,
                reply_to_message_id=telegram_message_id,
            )
            self.storage.update_status_message_id(
                queued_msg.id, status_msg.message_id
            )
        except Exception as e:
            logger.warning("Failed to send initial status message: %s", e)
            status_msg = None

        # Run processing in background task
        asyncio.create_task(
            self.process_message(
                queued_msg,
                audio_bytes=audio_bytes,
                status_message_id=(
                    status_msg.message_id if status_msg else None
                ),
            )
        )
        return queued_msg

    async def process_message(
        self,
        queued_msg: QueuedMessage,
        audio_bytes: bytes | None = None,
        status_message_id: int | None = None,
    ) -> None:
        """Process one claimed message against backend secretary turn."""
        status_msg_id = status_message_id or queued_msg.status_message_id
        chat_id = queued_msg.chat_id
        conv_id = queued_msg.context_conversation_id
        text = queued_msg.text
        agent_talk_id = await self._agent_talk_agent_id(queued_msg, conv_id)
        # The durable delivery row claimed for this turn's reply, if any. It
        # lives outside the try so an unexpected error below can hand the
        # delivery back to the outbox instead of leaving it stuck in sending.
        claimed = None

        try:
            # 1. Transcribe voice if needed
            if queued_msg.media_type == "voice":
                if status_msg_id:
                    await self._safe_edit_text(
                        chat_id, status_msg_id, "🎙 Расшифровываю аудио..."
                    )
                if audio_bytes:
                    transcribed = await self.transcriber.transcribe(audio_bytes)
                    if transcribed:
                        text = f"[Голосовое сообщение (автоматическая расшифровка речи, возможны неточности распознавания)]:\n{transcribed}"
                if status_msg_id:
                    await self._safe_edit_text(
                        chat_id, status_msg_id, "⏳ Жду ответ секретаря..."
                    )

            # 2. Resolve the task context before appending. Without this
            # header the secretary only sees the bare user question and
            # cannot know which task the conversation is bound to.
            text, task_metadata = await self._task_context_for_message(
                queued_msg, text
            )

            # 3. Append user message to backend. Inbound media stays in the
            # explicit Telegram media directory; the persistent conversation
            # handler is the single owner that stages it into the
            # conversation/turn-scoped attachment store. chat_id +
            # telegram_message_id let the backend register the durable Reply
            # link transactionally with the message insert.
            metadata = {
                "chat_id": queued_msg.chat_id,
                "telegram_message_id": queued_msg.telegram_message_id,
                "media_type": queued_msg.media_type,
                "media_path": queued_msg.media_path,
            }
            metadata.update(task_metadata)
            append_res = await self.client.append_message(
                conv_id,
                text=text,
                metadata=metadata,
            )
            input_message_id = append_res["id"]

            # 4. Enqueue turn on backend
            turn = await self.client.create_turn(conv_id, input_message_id)
            turn_id = turn["id"]

            # 5. Wait for turn to finish (polling dispatcher outcome)
            elapsed = 0.0
            reply_text: str | None = None
            turn_error: str | None = None

            while elapsed < self.turn_timeout_seconds:
                await asyncio.sleep(self.poll_interval_seconds)
                elapsed += self.poll_interval_seconds
                current_turn = await self.client.get_turn(turn_id)
                turn_status = current_turn.get("status")

                if turn_status == "completed":
                    reply_id = current_turn.get("reply_message_id")
                    if reply_id:
                        messages = await self.client.list_messages(conv_id)
                        for m in messages:
                            if m["id"] == reply_id:
                                reply_text = m.get("text")
                                break
                    if not reply_text:
                        reply_text = "Ответ получен, но текст пуст."
                    break

                if turn_status == "failed":
                    turn_error = current_turn.get(
                        "error", "Неизвестная ошибка выполнения хода"
                    )
                    break

            if reply_text is not None:
                self.storage.set_message_status(queued_msg.id, "completed")

                # Claim the durable reply row before sending: whoever holds
                # the claim sends; the other side must not send a copy.
                outbox_owned = False
                if self.delivery_service is not None:
                    claimed = self.delivery_service.claim_reply_delivery(turn_id)
                    if claimed is None:
                        outbox_owned = (
                            self.delivery_service.get_reply_delivery(turn_id)
                            is not None
                        )

                # Build the canonical part plan shared with the outbox, so
                # per-part progress recorded here resumes exactly where the
                # outbox would continue (and vice versa). The plate is pinned
                # on the delivery row: a retry must rebuild this exact plan,
                # not a fresh context snapshot.
                plate = await self._foreign_plate(queued_msg, conv_id)
                if claimed is not None and self.delivery_service is not None:
                    plate = self.delivery_service.ensure_delivery_plate(claimed, plate)
                text_parts, file_targets = build_delivery_parts(
                    reply_text, conv_id, plate=plate
                )
                text_part_count = len(text_parts)
                clean_reply_text = strip_file_directives(reply_text)[0]
                if not clean_reply_text and file_targets:
                    clean_reply_text = "📄 Запрошенный документ:"
                elif not clean_reply_text:
                    clean_reply_text = "Ответ получен."

                switch_keyboard = (
                    agent_conversation_keyboard(agent_talk_id, conv_id)
                    if agent_talk_id
                    else dialog_switch_keyboard(
                        conversation_id=conv_id,
                        task_id=task_metadata.get("task_id"),
                        task_number=task_metadata.get("task_number"),
                    )
                )

                if outbox_owned:
                    # The durable outbox already delivered (or is delivering)
                    # this reply with full formatting; point the user at it.
                    pointer = with_conversation_footer(
                        "↗️ Ответ доставлен отдельным сообщением выше.", conv_id
                    )
                    if status_msg_id:
                        await self._safe_edit_text(
                            chat_id, status_msg_id, pointer,
                            reply_markup=switch_keyboard,
                        )
                    else:
                        try:
                            await self.bot.send_message(
                                chat_id=chat_id, text=pointer,
                                reply_markup=switch_keyboard,
                            )
                        except Exception as pointer_err:
                            logger.warning("Failed to send reply pointer: %s", pointer_err)
                else:
                    rendered_reply = with_conversation_footer(
                        compose_dialog_message(
                            header=None,
                            body=clean_reply_text,
                            plate=plate,
                        ),
                        conv_id,
                    )
                    # The whole reply text (all text parts of the shared plan)
                    # reaches the chat as one message: the edited status bubble
                    # or a single send/document fallback.
                    text_delivered_message_id: int | None = None

                    if status_msg_id:
                        edit_success = False
                        if len(rendered_reply) <= 16384:
                            try:
                                await self.bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=status_msg_id,
                                    text=rendered_reply,
                                    rich_message=InputRichMessage(
                                        markdown=markdown_hashtag_safe(rendered_reply)
                                    ),
                                    reply_markup=switch_keyboard,
                                )
                                edit_success = True
                            except Exception as edit_err:
                                logger.debug("edit_message_text with rich_message failed: %s", edit_err)
                                try:
                                    await self.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=status_msg_id,
                                        text=rendered_reply,
                                        reply_markup=switch_keyboard,
                                    )
                                    edit_success = True
                                except Exception:
                                    pass

                        if edit_success:
                            text_delivered_message_id = status_msg_id
                        else:
                            doc_file = BufferedInputFile(
                                rendered_reply.encode("utf-8"), filename="ответ_секретаря.md"
                            )
                            doc = await self.bot.send_document(
                                chat_id=chat_id,
                                document=doc_file,
                                caption=with_conversation_footer(
                                    "📄 Текст ответа прикреплён файлом:", conv_id
                                ),
                                reply_to_message_id=queued_msg.telegram_message_id,
                                reply_markup=switch_keyboard,
                            )
                            text_delivered_message_id = doc.message_id
                    else:
                        if len(rendered_reply) <= 16384:
                            try:
                                sent = await self.bot.send_rich_message(
                                    chat_id=chat_id,
                                    rich_message=InputRichMessage(
                                        markdown=markdown_hashtag_safe(rendered_reply)
                                    ),
                                    reply_to_message_id=queued_msg.telegram_message_id,
                                    reply_markup=switch_keyboard,
                                )
                                text_delivered_message_id = sent.message_id
                            except Exception as send_err:
                                logger.warning("send_rich_message failed, fallback to doc: %s", send_err)
                                doc_file = BufferedInputFile(
                                    rendered_reply.encode("utf-8"), filename="ответ_секретаря.md"
                                )
                                doc = await self.bot.send_document(
                                    chat_id=chat_id,
                                    document=doc_file,
                                    caption=with_conversation_footer(
                                        "📄 Текст ответа прикреплён файлом:", conv_id
                                    ),
                                    reply_to_message_id=queued_msg.telegram_message_id,
                                    reply_markup=switch_keyboard,
                                )
                                text_delivered_message_id = doc.message_id
                        else:
                            doc_file = BufferedInputFile(
                                rendered_reply.encode("utf-8"), filename="ответ_секретаря.md"
                            )
                            doc = await self.bot.send_document(
                                chat_id=chat_id,
                                document=doc_file,
                                caption=with_conversation_footer(
                                    "📄 Ответ секретаря превышает 16k символов. "
                                    "Текст прикреплён файлом:", conv_id
                                ),
                                reply_to_message_id=queued_msg.telegram_message_id,
                                reply_markup=switch_keyboard,
                            )
                            text_delivered_message_id = doc.message_id

                    if claimed is not None and self.delivery_service is not None:
                        if text_delivered_message_id is not None:
                            self.delivery_service.record_delivery_progress(
                                claimed,
                                part_index=text_part_count - 1,
                                chat_id=chat_id,
                                telegram_message_id=text_delivered_message_id,
                            )

                    # Send attached documents if found. A failed file never
                    # marks the delivery sent: the claim is released so the
                    # outbox retries the remaining file parts from the shared
                    # plan (the already-delivered text is not resent).
                    delivery_released = False
                    for file_index, file_query in enumerate(file_targets):
                        resolved_file = (
                            self.knowledge_base.resolve_file(file_query)
                            if self.knowledge_base is not None
                            else None
                        )
                        if resolved_file and resolved_file.is_file():
                            try:
                                doc = await self.bot.send_document(
                                    chat_id=chat_id,
                                    document=FSInputFile(resolved_file),
                                    caption=with_conversation_footer(
                                        f"📄 {resolved_file.name}", conv_id
                                    ),
                                )
                                logger.info(
                                    "Sent document %s to chat_id=%s",
                                    resolved_file,
                                    chat_id,
                                )
                                file_message_id = doc.message_id
                            except Exception as doc_err:
                                logger.error(
                                    "Failed to send document %s: %s",
                                    resolved_file,
                                    doc_err,
                                    exc_info=doc_err,
                                )
                                if claimed is not None and self.delivery_service is not None:
                                    self.delivery_service.release_delivery(claimed)
                                    delivery_released = True
                                try:
                                    await self.bot.send_message(
                                        chat_id=chat_id,
                                        text=with_conversation_footer(
                                            "⚠️ Не удалось отправить файл "
                                            f"<code>{file_query}</code>. "
                                            "Попробую доставить его автоматически.",
                                            conv_id,
                                        ),
                                        parse_mode="HTML",
                                    )
                                except Exception as warn_err:
                                    logger.warning(
                                        "Failed to send file-retry notice: %s", warn_err
                                    )
                                break
                        else:
                            logger.warning(
                                "Requested document could not be resolved: %s", file_query
                            )
                            warn = await self.bot.send_message(
                                chat_id=chat_id,
                                text=with_conversation_footer(
                                    f"⚠️ Документ не найден: <code>{file_query}</code>",
                                    conv_id,
                                ),
                                parse_mode="HTML",
                            )
                            file_message_id = warn.message_id

                        if (
                            claimed is not None
                            and self.delivery_service is not None
                            and not delivery_released
                        ):
                            self.delivery_service.record_delivery_progress(
                                claimed,
                                part_index=text_part_count + file_index,
                                chat_id=chat_id,
                                telegram_message_id=file_message_id,
                            )

                    if (
                        claimed is not None
                        and self.delivery_service is not None
                        and not delivery_released
                    ):
                        self.delivery_service.mark_delivery_sent(claimed)
            elif turn_error is not None:
                self.storage.set_message_status(queued_msg.id, "failed")
                is_talk = (
                    queued_msg.context_mode == "talk"
                    and "conversation_continuation_available;" not in turn_error
                )
                if is_talk:
                    self.storage.set_talk_conversation(queued_msg.user_id, None)
                    state = self.storage.get_user_state(queued_msg.user_id)
                    if state.active_conversation_id == queued_msg.context_conversation_id:
                        self.storage.set_user_mode(queued_msg.user_id, "talk", conversation_id=None)
                summary = secretary_error_summary(turn_error)
                err_msg = with_conversation_footer(
                    (
                        f"❌ <b>Ошибка секретаря:</b>\n{summary}\n\n"
                        "<i>Контекст диалога сброшен. Следующее сообщение начнёт новый разговор.</i>"
                        if is_talk
                        else f"❌ Ошибка секретаря:\n{summary}"
                    ),
                    conv_id,
                )
                markup = (
                    agent_conversation_keyboard(agent_talk_id, conv_id)
                    if agent_talk_id
                    else talk_keyboard()
                    if is_talk
                    else dialog_switch_keyboard(
                        conversation_id=conv_id,
                        task_id=(
                            queued_msg.context_task_id
                            if queued_msg.context_mode == "task"
                            else None
                        ),
                    )
                )
                if status_msg_id:
                    await self._safe_edit_text(chat_id, status_msg_id, err_msg, reply_markup=markup)
                else:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=err_msg,
                        parse_mode="HTML",
                        reply_to_message_id=queued_msg.telegram_message_id,
                        reply_markup=markup,
                    )
            else:
                self.storage.set_message_status(queued_msg.id, "failed")
                is_talk = queued_msg.context_mode == "talk"
                if is_talk:
                    self.storage.set_talk_conversation(queued_msg.user_id, None)
                    state = self.storage.get_user_state(queued_msg.user_id)
                    if state.active_conversation_id == queued_msg.context_conversation_id:
                        self.storage.set_user_mode(queued_msg.user_id, "talk", conversation_id=None)
                timeout_msg = with_conversation_footer(
                    (
                        "⏱ <b>Время ожидания ответа секретаря истекло.</b>\n\n"
                        "<i>Контекст диалога сброшен. Следующее сообщение начнёт новый разговор.</i>"
                        if is_talk
                        else "⏱ Время ожидания ответа секретаря истекло."
                    ),
                    conv_id,
                )
                markup = (
                    agent_conversation_keyboard(agent_talk_id, conv_id)
                    if agent_talk_id
                    else talk_keyboard()
                    if is_talk
                    else dialog_switch_keyboard(
                        conversation_id=conv_id,
                        task_id=(
                            queued_msg.context_task_id
                            if queued_msg.context_mode == "task"
                            else None
                        ),
                    )
                )
                if status_msg_id:
                    await self._safe_edit_text(
                        chat_id, status_msg_id, timeout_msg, reply_markup=markup
                    )
                else:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=timeout_msg,
                        parse_mode="HTML",
                        reply_to_message_id=queued_msg.telegram_message_id,
                        reply_markup=markup,
                    )

        except Exception as error:
            logger.exception("Error processing message %s: %s", queued_msg.id, error)
            self.storage.set_message_status(queued_msg.id, "failed")
            # Hand any claimed delivery back to the outbox: without this the
            # row would hang in ``sending`` (blocking the conversation's
            # queue) until the next process restart.
            if claimed is not None and self.delivery_service is not None:
                try:
                    self.delivery_service.release_delivery(claimed)
                except Exception as release_err:
                    logger.warning(
                        "Failed to release delivery claim after error: %s", release_err
                    )
            fail_text = with_conversation_footer(
                f"❌ Не удалось обработать сообщение: {error}", conv_id
            )
            if status_msg_id:
                await self._safe_edit_text(chat_id, status_msg_id, fail_text)
            else:
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=fail_text,
                        reply_to_message_id=queued_msg.telegram_message_id,
                    )
                except Exception:
                    pass

        finally:
            # Drain next pending message in queue for this conversation
            next_msg = self.storage.claim_next_pending(conv_id)
            if next_msg is not None:
                if next_msg.status_message_id:
                    await self._safe_edit_text(
                        next_msg.chat_id,
                        next_msg.status_message_id,
                        "⏳ Обрабатываю запрос из очереди...",
                        reply_markup=None,
                    )
                asyncio.create_task(self.process_message(next_msg))
            elif agent_talk_id:
                # A closing direct talk releases its workspace only after
                # the local queue is empty; the backend re-checks pending
                # and running turns in the same transaction.
                await self._finalize_closing_talk(conv_id)

    async def _finalize_closing_talk(self, conversation_id: str) -> None:
        """Ask the backend to release a closing talk if it is idle."""
        try:
            conversation = await self.client.get_conversation(
                conversation_id
            )
        except Exception as error:
            logger.warning(
                "Failed to read closing talk %s: %s", conversation_id, error
            )
            return
        metadata = conversation.get("metadata") or {}
        if metadata.get("talk_closing") is not True:
            return
        try:
            await self.client.finalize_talk(conversation_id)
        except Exception as error:
            logger.warning(
                "Failed to finalize closing talk %s: %s",
                conversation_id,
                error,
            )

    async def _agent_talk_agent_id(
        self, queued_msg: QueuedMessage, conversation_id: str
    ) -> str | None:
        """Return the direct-talk agent for this conversation, if any.

        Only a conversation created by the agent-selection flow carries
        ``metadata.talkable_agent``; a failed lookup never blocks the message
        and falls back to the ordinary dialog keyboard.
        """
        if queued_msg.context_mode != "agent_talk":
            return None
        try:
            conversation = await self.client.get_conversation(
                conversation_id
            )
        except Exception as error:
            logger.warning(
                "Failed to read agent talk conversation %s: %s",
                conversation_id,
                error,
            )
            return None
        metadata = conversation.get("metadata") or {}
        agent_id = metadata.get("talkable_agent")
        return str(agent_id) if agent_id else None

    async def _task_context_for_message(
        self, queued_msg: QueuedMessage, text: str
    ) -> tuple[str, dict[str, Any]]:
        """Return the message text plus explicit task context metadata.

        Only task-bound conversations are enriched. A failed lookup is
        logged and leaves the message unchanged so a transient task read
        failure cannot block the user's conversation.
        """
        if (
            queued_msg.context_mode != "task"
            or not queued_msg.context_task_id
        ):
            return text, {}
        try:
            task = await self.client.get_task(queued_msg.context_task_id)
        except Exception as error:
            logger.warning(
                "Failed to load context for task %s: %s",
                queued_msg.context_task_id,
                error,
            )
            return text, {}

        task_id = task.get("id") or queued_msg.context_task_id
        task_number = task.get("number")
        if task_number is None:
            task_number = task_id
        task_title = task.get("title") or ""
        header = task_context_header(
            task_id=task_id,
            task_number=task_number,
            task_title=task_title,
        )
        if header not in text:
            text = f"{header}\n\n{text}"
        return text, {
            "task_id": task_id,
            "task_number": task_number,
            "task_title": task_title,
        }

    async def _foreign_plate(
        self, queued_msg: QueuedMessage, conversation_id: str
    ) -> str | None:
        """Snapshot plate when this reply targets a non-active dialog.

        Shown when the user's explicitly selected context differs from the
        conversation this reply belongs to (e.g. a Reply routed into another
        dialog). Reflects state at send time only.
        """
        try:
            state = self.storage.get_user_state(queued_msg.user_id)
        except Exception:
            return None
        if state.active_conversation_id == conversation_id:
            return None
        if state.mode == "task" and state.active_task_id:
            try:
                active_task = await self.client.get_task(state.active_task_id)
                plate = render_foreign_context_plate(
                    active_task_number=active_task.get("number")
                )
                if plate is not None:
                    return plate
            except Exception:
                pass
        return render_foreign_context_plate(
            active_conversation_id=state.active_conversation_id
        )

    async def _safe_edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
    ) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.debug("safe_edit_text exception (harmless): %s", e)
