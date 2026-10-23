"""Unified runtime orchestrator for Kuzya.

Runs the FastAPI backend, background QueueDispatcher (secretary turns),
TaskExecutionWorkerPool (developer background tasks), and Telegram Secretary bot.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path
import secrets
import shlex
import signal
import sys
from typing import Any, Sequence

from alembic import command
from alembic.config import Config
import uvicorn

from kuzya.api import create_app
from kuzya.dispatcher import QueueDispatcher
from kuzya.instance import InstanceContext
from kuzya.knowledge import KnowledgeBase
from kuzya.logging import configure_logging
from kuzya.operation_handlers import (
    register_default_operation_handlers,
)
from kuzya.operation_worker import (
    DEFAULT_OPERATION_CONCURRENCY,
    MAX_OPERATION_CONCURRENCY,
    OperationInstanceContext,
    OperationWorkerPool,
)
from kuzya.permissions import ExecutionPermissions
from kuzya.persistent_conversation import PersistentConversationHandler
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.storage import Database, TaskRepository
from kuzya.task_worker import (
    MAX_TASK_CONCURRENCY,
    TaskExecutionWorkerPool,
    TaskProcessRegistry,
)
from kuzya.task_subscriptions import TaskSubscriptions
from kuzya.telegram.bot import create_bot, setup_bot_commands
from kuzya.telegram.config import TelegramConfig
from kuzya.telegram.messenger import TelegramDeliveryService
from kuzya.telegram.notifier import TelegramNotifier

logger = logging.getLogger("kuzya.runner")


@dataclasses.dataclass(frozen=True)
class RunnerConfig:
    """Configuration for the unified runner."""

    database_url: str
    host: str = "127.0.0.1"
    port: int = 18767
    artifact_root: Path = Path("~/.kuzya/data/artifacts").expanduser()
    knowledge_root: Path = Path("~/.kuzya/data/knowledge").expanduser()
    state_root: Path = Path("~/.kuzya/data/state").expanduser()
    workspaces_root: Path = Path("~/.kuzya/data/workspaces").expanduser()
    skills_root: Path = Path("~/.kuzya/data/skills").expanduser()
    default_workspace: Path = Path.cwd()
    backend_url: str = ""
    openviking_url: str = ""
    openviking_token: str = ""
    runner_argv_prefix: tuple[str, ...] = (sys.executable, "-m", "pi_runner")
    secretary_agent_id: str = "system-secretary"
    developer_agent_id: str = "kuzya-developer"
    task_concurrency: int = 3
    operation_concurrency: int = DEFAULT_OPERATION_CONCURRENCY
    auto_migrate: bool = True
    telegram_config: TelegramConfig | None = None
    api_tokens: tuple[str, ...] = ()
    log_level: str = "INFO"
    json_logging: bool = False

    def instance_context(self) -> InstanceContext:
        """Build the typed per-instance context passed to every subsystem."""
        return InstanceContext(
            state_root=self.state_root,
            workspaces_root=self.workspaces_root,
            skills_root=self.skills_root,
            knowledge_root=self.knowledge_root,
            artifact_root=self.artifact_root,
            default_workspace=self.default_workspace,
            backend_url=self.backend_url,
            openviking_url=self.openviking_url,
            openviking_token=self.openviking_token,
        )

    @classmethod
    def from_env(cls) -> RunnerConfig:
        db_url = os.environ.get(
            "KUZYA_DB_URL",
            "sqlite:///" + str(Path("~/.kuzya/data/tasks.db").expanduser()),
        )
        host = os.environ.get("KUZYA_HOST", "127.0.0.1")
        port = int(os.environ.get("KUZYA_PORT", "18767"))

        artifact_root = Path(
            os.environ.get(
                "KUZYA_ARTIFACT_ROOT",
                "~/.kuzya/data/artifacts",
            )
        ).expanduser()

        knowledge_root = Path(
            os.environ.get(
                "KUZYA_KNOWLEDGE_ROOT",
                "~/.kuzya/data/knowledge",
            )
        ).expanduser()

        state_root = Path(
            os.environ.get(
                "KUZYA_STATE_ROOT",
                "~/.kuzya/data/state",
            )
        ).expanduser()

        workspaces_root = Path(
            os.environ.get(
                "KUZYA_WORKSPACES_ROOT",
                "~/.kuzya/data/workspaces",
            )
        ).expanduser()

        skills_root = Path(
            os.environ.get(
                "KUZYA_SKILLS_ROOT",
                "~/.kuzya/data/skills",
            )
        ).expanduser()

        backend_url = os.environ.get("KUZYA_BACKEND_URL") or f"http://127.0.0.1:{port}"
        openviking_url = os.environ.get("OPENVIKING_URL", "")
        openviking_token = os.environ.get("OPENVIKING_API_KEY", "")

        default_ws_str = os.environ.get("KUZYA_DEFAULT_WORKSPACE")
        default_workspace = (
            Path(default_ws_str).expanduser().resolve()
            if default_ws_str
            else Path.cwd().resolve()
        )

        # Ensure Node 20+ and ~/.local/bin are present in PATH for Pi execution
        home = Path.home()
        extra_paths = [
            home / ".nvm/versions/node/v24.19.0/bin",
            home / ".local/bin",
            home / ".pi/agent/bin",
        ]
        current_path = os.environ.get("PATH", "")
        to_prepend = [
            str(p.resolve())
            for p in extra_paths
            if p.is_dir() and str(p.resolve()) not in current_path.split(":")
        ]
        if to_prepend:
            os.environ["PATH"] = ":".join(to_prepend) + ":" + current_path

        runner_cmd = os.environ.get("KUZYA_RUNNER_PREFIX")
        if runner_cmd:
            runner_argv = tuple(shlex.split(runner_cmd))
        else:
            runner_argv = (sys.executable, "-m", "pi_runner")

        auto_migrate = os.environ.get("KUZYA_AUTO_MIGRATE", "true").lower() in (
            "1",
            "true",
            "yes",
        )

        try:
            task_concurrency = int(
                os.environ.get("KUZYA_TASK_CONCURRENCY", "3")
            )
        except ValueError as error:
            raise ValueError(
                "KUZYA_TASK_CONCURRENCY must be an integer between "
                f"1 and {MAX_TASK_CONCURRENCY}"
            ) from error
        if task_concurrency < 1 or task_concurrency > MAX_TASK_CONCURRENCY:
            raise ValueError(
                "KUZYA_TASK_CONCURRENCY must be an integer between "
                f"1 and {MAX_TASK_CONCURRENCY}"
            )

        try:
            operation_concurrency = int(
                os.environ.get(
                    "KUZYA_OPERATION_CONCURRENCY",
                    str(DEFAULT_OPERATION_CONCURRENCY),
                )
            )
        except ValueError as error:
            raise ValueError(
                "KUZYA_OPERATION_CONCURRENCY must be an integer between "
                f"1 and {MAX_OPERATION_CONCURRENCY}"
            ) from error
        if (
            operation_concurrency < 1
            or operation_concurrency > MAX_OPERATION_CONCURRENCY
        ):
            raise ValueError(
                "KUZYA_OPERATION_CONCURRENCY must be an integer between "
                f"1 and {MAX_OPERATION_CONCURRENCY}"
            )

        api_tokens_raw = os.environ.get("KUZYA_API_TOKENS")
        if not api_tokens_raw:
            api_tokens_raw = os.environ.get("KUZYA_API_TOKEN", "")
        api_tokens = tuple(
            t.strip() for t in api_tokens_raw.split(",") if t.strip()
        )

        log_level = os.environ.get("LOG_LEVEL", "INFO")
        json_logging = os.environ.get("LOG_FORMAT", "").lower() == "json"

        telegram_cfg: TelegramConfig | None = None
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            try:
                telegram_cfg = TelegramConfig.from_env()
            except Exception as e:
                logger.warning("Could not initialize TelegramConfig: %s", e)

        return cls(
            database_url=db_url,
            host=host,
            port=port,
            artifact_root=artifact_root,
            knowledge_root=knowledge_root,
            state_root=state_root,
            workspaces_root=workspaces_root,
            skills_root=skills_root,
            default_workspace=default_workspace,
            backend_url=backend_url,
            openviking_url=openviking_url,
            openviking_token=openviking_token,
            runner_argv_prefix=runner_argv,
            secretary_agent_id=os.environ.get(
                "KUZYA_SECRETARY_AGENT_ID", "system-secretary"
            ),
            developer_agent_id=os.environ.get(
                "KUZYA_DEVELOPER_AGENT_ID", "kuzya-developer"
            ),
            task_concurrency=task_concurrency,
            operation_concurrency=operation_concurrency,
            auto_migrate=auto_migrate,
            telegram_config=telegram_cfg,
            api_tokens=api_tokens,
            log_level=log_level,
            json_logging=json_logging,
        )


def run_database_migrations(database_url: str) -> None:
    """Apply Alembic migrations up to head."""
    repo_root = Path(__file__).resolve().parents[2]
    ini_path = repo_root / "alembic.ini"
    migrations_dir = repo_root / "migrations"

    if not ini_path.exists() or not migrations_dir.exists():
        logger.info("Alembic files not found at %s, skipping auto-migration", ini_path)
        return

    logger.info("Applying Alembic migrations to %s", database_url)
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    logger.info("Alembic migrations completed successfully.")


class KuzyaServices:
    """Manages the full lifecycle of all background and server components."""

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.instance: InstanceContext = config.instance_context()
        self.permissions = ExecutionPermissions()
        self.database: Database | None = None
        self.queue_dispatcher: QueueDispatcher | None = None
        self.task_worker: TaskExecutionWorkerPool | None = None
        self.operation_worker: OperationWorkerPool | None = None
        self.task_subscriptions: TaskSubscriptions | None = None
        self.bot = None
        self.dp = None
        self.queue_processor = None
        self.telegram_notifier: TelegramNotifier | None = None
        self.telegram_delivery: TelegramDeliveryService | None = None
        self.uvicorn_server: uvicorn.Server | None = None
        # Ephemeral in-memory administrative principal for the host Telegram
        # client. Never written to a file, child configuration or log.
        self.internal_token: str | None = None
        self._stop_event = asyncio.Event()

    def resolve_host_auth(
        self,
    ) -> tuple[tuple[str, ...], TelegramConfig | None]:
        """Resolve administrative credentials and the host Telegram client.

        The Telegram bot calls this same process over HTTP and is a host
        principal, not a child execution. When a bot is configured the
        service mints an ephemeral in-memory internal token and registers it
        as an administrative credential, so the internal client keeps
        working before, during and after execution-scoped grants. The token
        is never written to a file, child configuration or log.
        Caller-supplied administrative tokens remain valid alongside it.
        """
        admin_tokens = list(self.config.api_tokens)
        telegram_config = self.config.telegram_config
        if telegram_config is not None and telegram_config.bot_token:
            self.internal_token = secrets.token_urlsafe(32)
            admin_tokens.append(self.internal_token)
            telegram_config = dataclasses.replace(
                telegram_config,
                backend_url=(
                    self.config.backend_url or telegram_config.backend_url
                ),
                api_token=self.internal_token,
            )
        return tuple(admin_tokens), telegram_config

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize all subsystems and wire callbacks."""
        logger.info("Initializing KuzyaServices...")

        # Administrative credentials for this instance. The Telegram client
        # gets an ephemeral host-only token; children never do.
        admin_tokens, telegram_config = self.resolve_host_auth()

        # 1. Run migrations if enabled
        if self.config.auto_migrate:
            try:
                run_database_migrations(self.config.database_url)
            except Exception as e:
                logger.error("Failed to run database migrations: %s", e, exc_info=e)

        # 2. Storage
        self.database = Database(self.config.database_url)

        # 3. Knowledge Base (explicit per-instance paths)
        self.knowledge_base = KnowledgeBase(
            self.config.knowledge_root,
            instance=self.instance,
        )
        try:
            self.knowledge_base.initialize(database=self.database)
            logger.info("KnowledgeBase initialized at %s", self.config.knowledge_root)
        except Exception as e:
            logger.warning("Error initializing KnowledgeBase: %s", e)

        # 4. Telegram & Notifier
        if telegram_config and telegram_config.bot_token:
            try:
                bot, dp, queue_processor = create_bot(
                    telegram_config,
                    knowledge_base=self.knowledge_base,
                )
                self.bot = bot
                self.dp = dp
                self.queue_processor = queue_processor
                self.telegram_notifier = TelegramNotifier(
                    bot=self.bot,
                    loop=loop,
                    default_chat_id=telegram_config.allowed_user_id,
                )
                # Unified Telegram dialog delivery: the durable outbox for
                # API-question copies, replies and errors, shared with the
                # synchronous queue worker to guarantee exactly one sender
                # per delivery row.
                state_storage = queue_processor.storage
                probe_user_id = telegram_config.allowed_user_id
                probe_tasks = TaskRepository(self.database)

                def active_context_probe():
                    try:
                        state = state_storage.get_user_state(probe_user_id)
                    except Exception:
                        return None
                    active = {
                        "mode": state.mode,
                        "conversation_id": state.active_conversation_id,
                    }
                    if state.mode == "task" and state.active_task_id:
                        task = probe_tasks.get(state.active_task_id)
                        if task is not None:
                            active["task_number"] = task.number
                    return active

                self.telegram_delivery = TelegramDeliveryService(
                    bot=self.bot,
                    database=self.database,
                    default_chat_id=telegram_config.allowed_user_id,
                    knowledge_base=self.knowledge_base,
                    active_context_probe=active_context_probe,
                )
                queue_processor.delivery_service = self.telegram_delivery
                logger.info(
                    "Telegram Bot initialized for user %s",
                    telegram_config.allowed_user_id,
                )
            except Exception as e:
                logger.error("Failed to setup Telegram Bot: %s", e, exc_info=e)

        # 4. Secretary Turn Handler & Queue Dispatcher
        secretary_artifacts = self.config.artifact_root / "conversations"
        secretary_artifacts.mkdir(parents=True, exist_ok=True)
        secretary_handler = PersistentConversationHandler(
            self.database,
            agent_id=self.config.secretary_agent_id,
            runner_argv_prefix=list(self.config.runner_argv_prefix),
            artifact_root=secretary_artifacts,
            knowledge_base=self.knowledge_base,
            instance=self.instance,
            permissions=self.permissions,
        )
        self.queue_dispatcher = QueueDispatcher(
            self.database,
            handler=secretary_handler,
            poll_interval_seconds=1.0,
        )

        # 5. Task Execution Worker
        task_artifacts = self.config.artifact_root / "tasks"
        task_artifacts.mkdir(parents=True, exist_ok=True)

        # One live process registry shared by the task pool (which registers
        # each runner) and the API (whose interrupt endpoint signals them).
        self.task_process_registry = TaskProcessRegistry()

        self.task_subscriptions = TaskSubscriptions(
            self.database, knowledge_base=self.knowledge_base,
            default_chat_id=(self.config.telegram_config.allowed_user_id if self.config.telegram_config else None),
        )

        def on_task_finished(task, outcome):
            try:
                self.task_subscriptions.enqueue_ready(task.id)
            finally:
                if self.telegram_notifier is not None:
                    self.telegram_notifier.notify_sync(task, outcome)

        self.task_worker = TaskExecutionWorkerPool(
            database=self.database,
            artifact_root=task_artifacts,
            concurrency=self.config.task_concurrency,
            agent_id=self.config.developer_agent_id,
            runner_argv_prefix=list(self.config.runner_argv_prefix),
            default_workspace=str(self.config.default_workspace),
            on_task_finished=on_task_finished,
            knowledge_base=self.knowledge_base,
            poll_interval_seconds=1.0,
            instance=self.instance,
            permissions=self.permissions,
            process_registry=self.task_process_registry,
        )

        # 6. Durable Operation Worker (deferred triggers: review, analysis, Git)
        operation_artifacts = self.config.artifact_root / "operations"
        operation_artifacts.mkdir(parents=True, exist_ok=True)
        operation_instance = OperationInstanceContext(
            database=self.database,
            artifact_root=operation_artifacts,
            runner_argv_prefix=tuple(self.config.runner_argv_prefix),
            default_workspace=self.config.default_workspace,
            knowledge_base=self.knowledge_base,
            instance=self.instance,
            permissions=self.permissions,
            runtime_defaults=RuntimeDefaultsResolver(),
        )
        self.operation_worker = OperationWorkerPool(
            self.database,
            concurrency=self.config.operation_concurrency,
            instance=operation_instance,
            poll_interval_seconds=1.0,
        )
        register_default_operation_handlers(self.operation_worker)

        # 7. FastAPI Uvicorn Server
        app = create_app(
            self.config.database_url,
            api_tokens=admin_tokens,
            default_workspace=self.config.default_workspace,
            default_agent_id=self.config.developer_agent_id,
            knowledge_base=self.knowledge_base,
            permissions=self.permissions,
            artifact_root=self.config.artifact_root,
            task_process_registry=self.task_process_registry,
        )
        uvicorn_cfg = uvicorn.Config(
            app=app,
            host=self.config.host,
            port=self.config.port,
            log_level=self.config.log_level.lower(),
            access_log=False,
        )
        self.uvicorn_server = uvicorn.Server(config=uvicorn_cfg)

    def start_background_threads(self) -> None:
        """Start QueueDispatcher and TaskExecutionWorkerPool threads."""
        if self.queue_dispatcher:
            logger.info("Starting QueueDispatcher background thread...")
            self.queue_dispatcher.start()

        if self.task_worker:
            logger.info("Starting TaskExecutionWorkerPool background threads...")
            self.task_worker.start()

        if self.operation_worker:
            logger.info("Starting OperationWorkerPool background threads...")
            self.operation_worker.start()

    def stop_background_threads(self) -> None:
        """Stop background worker threads gracefully.

        The task pool receives one five-second budget per configured worker so
        that several active executions are not squeezed into a single
        five-second shutdown window.
        """
        if self.operation_worker:
            logger.info("Stopping OperationWorkerPool...")
            try:
                self.operation_worker.stop(
                    timeout=5.0 * max(1, self.config.operation_concurrency)
                )
            except Exception as e:
                logger.warning("Error stopping OperationWorkerPool: %s", e)

        if self.task_worker:
            logger.info("Stopping TaskExecutionWorkerPool...")
            try:
                self.task_worker.stop(
                    timeout=5.0 * max(1, self.config.task_concurrency)
                )
            except Exception as e:
                logger.warning("Error stopping TaskExecutionWorkerPool: %s", e)

        if self.queue_dispatcher:
            logger.info("Stopping QueueDispatcher...")
            try:
                self.queue_dispatcher.stop(timeout=5.0)
            except Exception as e:
                logger.warning("Error stopping QueueDispatcher: %s", e)

        if self.database:
            try:
                self.database.dispose()
            except Exception:
                pass

    async def _run_telegram_delivery(self) -> None:
        """Deliver durable Telegram outbox rows until cancelled.

        Startup recovery requeues rows a previous process left in ``sending``;
        the loop then keeps draining pending rows. Delivery is at-least-once:
        a network failure with an unknown outcome releases the row for retry.
        """
        assert self.telegram_delivery is not None
        await asyncio.to_thread(self.telegram_delivery.startup_recovery)
        while True:
            try:
                delivered = await self.telegram_delivery.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram delivery loop failed; will retry")
                delivered = 0
            await asyncio.sleep(0.2 if delivered else 1.0)

    async def run(self) -> None:
        """Run async loops: uvicorn server and Telegram bot polling."""
        tasks: list[asyncio.Task[Any]] = []

        if self.task_subscriptions is not None:
            tasks.append(asyncio.create_task(self.task_subscriptions.run(self.bot), name="task-subscriptions"))

        if self.telegram_delivery is not None:
            tasks.append(asyncio.create_task(self._run_telegram_delivery(), name="telegram-delivery"))

        if self.uvicorn_server:
            tasks.append(asyncio.create_task(self.uvicorn_server.serve(), name="uvicorn"))

        if self.bot and self.dp:
            async def run_telegram_polling() -> None:
                try:
                    await setup_bot_commands(self.bot)
                    logger.info("Starting Telegram polling...")
                    await self.dp.start_polling(self.bot)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.error("Telegram polling crashed: %s", exc, exc_info=exc)
                finally:
                    if self.bot.session:
                        await self.bot.session.close()

            tasks.append(asyncio.create_task(run_telegram_polling(), name="telegram_bot"))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            if self.uvicorn_server:
                self.uvicorn_server.should_exit = True
            if self.bot and self.dp:
                await self.dp.stop_polling()


async def main_async(config: RunnerConfig) -> None:
    configure_logging(
        level=config.log_level,
        json_format=config.json_logging,
    )
    logger.info("Kuzya starting up...")

    loop = asyncio.get_running_loop()
    services = KuzyaServices(config)
    services.setup(loop)
    services.start_background_threads()

    def on_signal() -> None:
        logger.info("Shutdown signal received, terminating services...")
        services.stop_background_threads()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            pass

    try:
        await services.run()
    finally:
        services.stop_background_threads()
        logger.info("Kuzya shutdown complete.")


def main() -> None:
    config = RunnerConfig.from_env()
    try:
        asyncio.run(main_async(config))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
