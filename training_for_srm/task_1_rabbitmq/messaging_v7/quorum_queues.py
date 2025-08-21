"""Quorum queues implementation for RabbitMQ
"""
import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .config import settings


logger = logging.getLogger(__name__)


class QuorumQueueConfig(BaseModel):
    """Configuration for quorum queue"""
    name: str
    initial_group_size: int = 1
    max_length: int | None = None
    message_ttl: int | None = None
    max_priority: int | None = None
    dead_letter_exchange: str = ""
    dead_letter_routing_key: str | None = None
    overflow: str = "drop-head"
    queue_type: str = "quorum"  # "quorum" или "classic"


class QuorumQueueManager:
    """Manager for quorum queues operations"""

    def __init__(self, messaging_adapter):
        self.messaging_adapter = messaging_adapter

        # Используем кворумные очереди если включены в настройках
        queue_type = "quorum" if settings.use_quorum_queues else "classic"
        logger.info(f"Using queue type: {queue_type} (use_quorum_queues: {settings.use_quorum_queues})")

        self.quorum_queues = {
            settings.rabbitmq_notifications_queue: QuorumQueueConfig(
                name=settings.rabbitmq_notifications_queue,
                initial_group_size=settings.rabbitmq_quorum_initial_group_size,
                max_length=10000,
                message_ttl=settings.rabbitmq_message_ttl,
                dead_letter_routing_key=f"{settings.rabbitmq_notifications_queue}.dlq",
                queue_type=queue_type,
            ),
            f"{settings.rabbitmq_notifications_queue}.dlq": QuorumQueueConfig(
                name=f"{settings.rabbitmq_notifications_queue}.dlq",
                initial_group_size=settings.rabbitmq_quorum_initial_group_size,
                max_length=1000,
                message_ttl=settings.rabbitmq_dlq_ttl,
                queue_type=queue_type,
            ),
            f"{settings.rabbitmq_notifications_queue}.retry": QuorumQueueConfig(
                name=f"{settings.rabbitmq_notifications_queue}.retry",
                initial_group_size=settings.rabbitmq_quorum_initial_group_size,
                max_length=5000,
                message_ttl=settings.rabbitmq_retry_ttl,
                dead_letter_routing_key=settings.rabbitmq_notifications_queue,
                queue_type=queue_type,
            ),
            f"{settings.rabbitmq_notifications_queue}.priority": QuorumQueueConfig(
                name=f"{settings.rabbitmq_notifications_queue}.priority",
                initial_group_size=settings.rabbitmq_quorum_initial_group_size,
                max_length=5000,
                message_ttl=settings.rabbitmq_message_ttl,
                dead_letter_routing_key=f"{settings.rabbitmq_notifications_queue}.dlq",
                queue_type=queue_type,
            ),
        }

    async def create_quorum_queue(self, config: QuorumQueueConfig, max_retries: int = 3) -> bool:
        """Create a quorum queue with specified configuration and retry logic"""
        if not self.messaging_adapter.connection:
            logger.warning("No RabbitMQ connection, skipping queue creation")
            return False

        try:

            # Создаем новый канал для каждой операции
            channel = await self.messaging_adapter.connection.channel()

            # Сначала пытаемся получить информацию о существующей очереди
            try:
                existing_queue = await channel.declare_queue(
                    config.name,
                    durable=True,
                    passive=True,
                )
                logger.info(f"Queue {config.name} already exists, skipping creation")
                await channel.close()
                return True
            except Exception:
                # Очередь не существует, создаем новую
                logger.info(f"Queue {config.name} does not exist, creating new one")

            # Создаем новую очередь с параметрами
            arguments = {}

            # Настраиваем тип очереди
            logger.info(f"Creating queue {config.name} with type: {config.queue_type}")
            if config.queue_type == "quorum":
                arguments["x-queue-type"] = "quorum"
                arguments["x-quorum-initial-group-size"] = config.initial_group_size
                logger.info(f"Added quorum arguments: {arguments}")
            else:
                logger.info("Using classic queue (no quorum arguments)")
            # Для classic очередей не добавляем x-queue-type (по умолчанию classic)

            # Убираем сложные параметры которые могут вызывать таймауты
            # if config.max_length:
            #     arguments["x-max-length"] = config.max_length
            # if config.message_ttl:
            #     arguments["x-message-ttl"] = config.message_ttl
            # if config.max_priority and config.queue_type == "classic":
            #     # Приоритеты поддерживаются только в classic очередях
            #     arguments["x-max-priority"] = config.max_priority
            # if config.dead_letter_exchange:
            #     arguments["x-dead-letter-exchange"] = config.dead_letter_exchange
            # if config.dead_letter_routing_key:
            #     arguments["x-dead-letter-routing-key"] = config.dead_letter_routing_key
            # if config.overflow:
            #     arguments["x-overflow"] = config.overflow

            # Создаем новую очередь с увеличенным таймаутом
            await channel.declare_queue(
                config.name,
                durable=True,
                arguments=arguments,
                timeout=settings.rabbitmq_channel_timeout,  # Используем настройку из config
            )
            logger.info(f"Created quorum queue: {config.name}")

            # Закрываем канал
            await channel.close()
            return True

        except Exception as e:
            logger.error(f"Failed to create quorum queue {config.name}: {e}")
            return False

    async def create_all_quorum_queues(self) -> dict[str, bool]:
        """Create all configured quorum queues"""
        results = {}
        for queue_name, config in self.quorum_queues.items():
            results[queue_name] = await self.create_quorum_queue(config)
        return results

    async def get_quorum_queue_status(self, queue_name: str) -> dict[str, Any] | None:
        """Get detailed status of a quorum queue"""
        if not self.messaging_adapter.channel:
            return None

        try:
            queue = await self.messaging_adapter.channel.declare_queue(
                queue_name,
                durable=True,
                passive=True,
            )

            status = {
                "name": queue.name,
                "type": "quorum",
                "message_count": queue.declaration_result.message_count,
                "consumer_count": queue.declaration_result.consumer_count,
                "config": self.quorum_queues.get(queue_name, {}),
                "created_at": datetime.now().isoformat(),
            }

            return status

        except Exception:
            logger.exception(f"Failed to get quorum queue status for {queue_name}")
            return None

    async def get_all_quorum_queues_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all quorum queues."""
        status = {}
        for queue_name in self.quorum_queues.keys():
            queue_status = await self.get_quorum_queue_status(queue_name)
            if queue_status:
                status[queue_name] = queue_status
        return status

    async def purge_quorum_queue(self, queue_name: str) -> bool:
        """Purge all messages from a quorum queue."""
        if not self.messaging_adapter.channel:
            return False

        try:
            queue = await self.messaging_adapter.channel.declare_queue(
                queue_name,
                durable=True,
                passive=True,
            )
            await queue.purge()
            logger.info(f"Purged quorum queue: {queue_name}")
            return True

        except Exception:
            logger.exception(f"Failed to purge quorum queue {queue_name}")
            return False

    async def delete_quorum_queue(self, queue_name: str) -> bool:
        """Delete a quorum queue."""
        if not self.messaging_adapter.channel:
            return False

        try:
            queue = await self.messaging_adapter.channel.declare_queue(
                queue_name,
                durable=True,
                passive=True,
            )
            await queue.delete()
            logger.info(f"Deleted quorum queue: {queue_name}")
            return True

        except Exception:
            logger.exception(f"Failed to delete quorum queue {queue_name}")
            return False


class QuorumMessageHandler:
    """Enhanced message handler for quorum queues."""

    def __init__(self, base_handler):
        self.base_handler = base_handler
        self.retry_count = 0
        self.max_retries = 3

    async def handle(self, message: dict[str, Any]) -> None:
        """Handle message with retry logic for quorum queues."""
        await self.handle_with_retry(message)

    async def handle_with_retry(self, message: dict[str, Any]) -> None:
        """Handle message with retry logic for quorum queues."""
        try:
            await self.base_handler.handle(message)
            self.retry_count = 0  # Reset retry count on success

        except Exception:
            self.retry_count += 1
            logger.exception(f"Error processing message (attempt {self.retry_count})")

            if self.retry_count >= self.max_retries:
                logger.error("Max retries reached for message, moving to DLQ")
                # Message will be automatically moved to DLQ by RabbitMQ
                self.retry_count = 0
            else:
                # Re-raise to trigger retry mechanism
                raise
