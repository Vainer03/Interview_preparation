"""Enhanced messaging interface for RabbitMQ integration with quorum queues support
"""
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .config import settings
from .quorum_queues import QuorumMessageHandler, QuorumQueueManager


logger = logging.getLogger(__name__)


class MessageHandler(ABC):
    """Abstract base class for message handlers"""

    @abstractmethod
    async def handle(self, message: dict[str, Any]) -> None:
        """Handle incoming message"""


class NotificationMessage(BaseModel):
    """Base notification message schema"""
    title: str
    message: str
    recipient_id: str
    notification_type: str = "tasks"
    sender_id: str | None = None
    related_object_id: str | None = None
    related_object_type: str | None = None
    timestamp: str | None = None

    def __init__(self, **data):
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()
        super().__init__(**data)


class MessagingAdapter:
    """RabbitMQ messaging adapter"""

    def __init__(self, connection_url: str = None, service_name: str = None):
        if connection_url is None and settings.rabbitmq_url is None:
            raise ValueError("RabbitMQ URL must be provided either as parameter or in settings")
        self.connection_url = connection_url or settings.rabbitmq_url
        self.service_name = service_name
        self.connection = None
        self.channel = None
        self._handlers: dict[str, MessageHandler] = {}
        self._consumers: list[Any] = []
        self.quorum_manager: QuorumQueueManager | None = None

    async def connect(self) -> None:
        """Establish connection to RabbitMQ"""
        try:
            import aio_pika

            # Настройки подключения с таймаутами для aio_pika 9.x
            logger.info(f"Connecting to RabbitMQ with timeout: {settings.rabbitmq_connection_timeout}s")
            self.connection = await aio_pika.connect_robust(
                self.connection_url,
                timeout=settings.rabbitmq_connection_timeout,
            )

            # Создаем канал с увеличенным таймаутом для кворумных операций
            self.channel = await self.connection.channel()
            logger.info("Connected to RabbitMQ")

            # Initialize quorum queue manager first
            self.quorum_manager = QuorumQueueManager(self)

            # Убираем автоматическое создание очередей - будем создавать их по требованию
            # await self.create_queues()

        except ImportError:
            logger.warning("aio_pika not installed, using mock connection")
            self.connection = None
            self.channel = None
        except Exception:
            logger.exception("Failed to connect to RabbitMQ")
            self.connection = None
            self.channel = None

    async def create_queues(self) -> None:
        """Create service-specific queues with quorum support"""
        if not self.channel:
            logger.warning("No RabbitMQ connection, skipping queue creation")
            return

        try:
            # Ждем немного, чтобы RabbitMQ полностью инициализировался
            import asyncio
            logger.info("Waiting for RabbitMQ to be ready...")
            await asyncio.sleep(3)

            # Создаем очереди для конкретного сервиса
            main_queue = self.get_service_queue("main")
            dlq_queue = self.get_service_queue("dlq")
            retry_queue = self.get_service_queue("retry")
            priority_queue = self.get_service_queue("priority")

            import aio_pika

            # Создаем exchange для dead letter
            dlx_exchange = await self.channel.declare_exchange(
                f"{self.service_name}.dlx",
                aio_pika.ExchangeType.DIRECT,
                durable=True,
            )
            logger.info(f"Created DLX exchange: {self.service_name}.dlx")

            # Создаем DLQ (quorum) - сначала, чтобы другие очереди могли ссылаться на него
            dlq_queue_obj = await self.channel.declare_queue(
                dlq_queue,
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-quorum-initial-group-size": 1,
                },
            )
            # Привязываем DLQ к DLX exchange
            await dlq_queue_obj.bind(dlx_exchange, routing_key=dlq_queue)
            logger.info(f"Created DLQ: {dlq_queue}")

            # Создаем основную очередь (quorum)
            main_queue_obj = await self.channel.declare_queue(
                main_queue,
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-quorum-initial-group-size": 1,
                    "x-dead-letter-exchange": f"{self.service_name}.dlx",
                    "x-dead-letter-routing-key": dlq_queue,
                },
            )
            logger.info(f"Created main queue: {main_queue}")

            # Создаем retry очередь (quorum)
            retry_queue_obj = await self.channel.declare_queue(
                retry_queue,
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-quorum-initial-group-size": 1,
                    "x-dead-letter-exchange": f"{self.service_name}.dlx",
                    "x-dead-letter-routing-key": main_queue,
                },
            )
            logger.info(f"Created retry queue: {retry_queue}")

            # Создаем priority очередь (classic для поддержки приоритетов)
            priority_queue_obj = await self.channel.declare_queue(
                priority_queue,
                durable=True,
                arguments={
                    "x-max-priority": 10,
                    "x-dead-letter-exchange": f"{self.service_name}.dlx",
                    "x-dead-letter-routing-key": dlq_queue,
                },
            )
            logger.info(f"Created priority queue: {priority_queue}")

            logger.info(f"Successfully created all queues for service: {self.service_name}")

        except Exception:
            logger.exception(f"Failed to create queues for service {self.service_name}")

    async def disconnect(self) -> None:
        """Close RabbitMQ connection"""
        if self.connection:
            await self.connection.close()
            logger.info("Disconnected from RabbitMQ")

    def set_service_name(self, service_name: str) -> None:
        """Set service name for queue naming"""
        self.service_name = service_name
        logger.info(f"Service name set to: {service_name}")

    def get_service_queue(self, queue_type: str = "main") -> str:
        """Get service-specific queue name"""
        if not self.service_name:
            raise ValueError("Service name must be set to use service-specific queues")

        if queue_type == "main":
            return f"{self.service_name}.queue"
        if queue_type == "dlq":
            return f"{self.service_name}.queue.dlq"
        if queue_type == "retry":
            return f"{self.service_name}.queue.retry"
        if queue_type == "priority":
            return f"{self.service_name}.queue.priority"
        return f"{self.service_name}.{queue_type}"

    async def publish_message(self, message: BaseModel, queue_type: str = "main", priority: int = 0) -> bool:
        """Publish message to service-specific queue"""
        if not self.channel:
            logger.warning("No RabbitMQ connection, skipping message publish")
            return False

        try:
            import aio_pika
            message_body = message.model_dump_json()

            # Определяем queue в зависимости от типа и приоритета
            if priority > 0:
                queue_name = self.get_service_queue("priority")
            else:
                queue_name = self.get_service_queue(queue_type)

            message_obj = aio_pika.Message(
                body=message_body.encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                priority=priority if priority > 0 else None,
            )

            # Публикуем в default exchange с именем очереди как routing key
            await self.channel.default_exchange.publish(
                message_obj,
                routing_key=queue_name,
            )

            logger.info(f"Published message to {queue_name} for service {self.service_name}")
            return True

        except Exception:
            logger.exception("Failed to publish message")
            return False

    async def publish_notification(self, notification: NotificationMessage, priority: int = 0) -> bool:
        """Publish notification message to queue with priority support (backward compatibility)"""
        return await self.publish_message(notification, "main", priority)

    async def publish_notification_with_retry(self, notification: NotificationMessage, max_retries: int = 3) -> bool:
        """Publish notification with retry mechanism"""
        for attempt in range(max_retries):
            try:
                success = await self.publish_notification(notification)
                if success:
                    return True
                logger.warning(f"Attempt {attempt + 1} failed, retrying...")
            except Exception:
                logger.exception(f"Attempt {attempt + 1} failed")
                if attempt == max_retries - 1:
                    raise
        return False

    def register_handler(self, queue_name: str, handler: MessageHandler) -> None:
        """Register message handler for specific queue"""
        self._handlers[queue_name] = handler
        logger.info(f"Registered handler for queue: {queue_name}")

    async def start_consuming(self, queue_name: str) -> None:
        """Start consuming messages from queue"""
        if not self.channel:
            logger.warning("No RabbitMQ connection, skipping consumer start")
            return

        try:
            # Используем passive=True для существующих очередей
            queue = await self.channel.declare_queue(queue_name, durable=True, passive=True)

            async def process_message(message):
                async with message.process():
                    try:
                        body = message.body.decode()
                        data = json.loads(body)

                        if queue_name in self._handlers:
                            await self._handlers[queue_name].handle(data)
                        else:
                            logger.warning(f"No handler registered for queue: {queue_name}")

                    except Exception:
                        logger.exception("Error processing message")

            consumer = await queue.consume(process_message)
            self._consumers.append(consumer)
            logger.info(f"Started consuming from queue: {queue_name}")

        except Exception:
            logger.exception(f"Failed to start consuming from queue {queue_name}")

    async def stop_consuming(self) -> None:
        """Stop all consumers"""
        try:
            for consumer in self._consumers:
                await consumer.cancel()
            self._consumers.clear()
            logger.info("Stopped all consumers")
        except Exception:
            logger.exception("Error stopping consumers")

    async def get_queue_info(self, queue_name: str) -> dict[str, Any] | None:
        """Get queue information"""
        if not self.channel:
            return None

        try:
            queue = await self.channel.declare_queue(queue_name, durable=True, passive=True)

            # Получаем дополнительную информацию для кворумных очередей
            queue_info = {
                "name": queue.name,
                "message_count": queue.declaration_result.message_count,
                "consumer_count": queue.declaration_result.consumer_count,
                "queue_type": "quorum" if queue_name in [
                    settings.rabbitmq_notifications_queue,
                    f"{settings.rabbitmq_notifications_queue}.dlq",
                    f"{settings.rabbitmq_notifications_queue}.retry",
                    f"{settings.rabbitmq_notifications_queue}.priority",
                ] else "classic",
            }

            # Добавляем информацию о кворумной очереди если это кворумная очередь
            if queue_info["queue_type"] == "quorum":
                queue_info.update({
                    "quorum_initial_group_size": settings.rabbitmq_quorum_initial_group_size,
                    "quorum_members": settings.rabbitmq_quorum_members,
                    "leader": "rabbit@rabbitmq",  # В кластере это будет динамически определяться
                })

            return queue_info
        except Exception:
            logger.exception("Failed to get queue info")
            return None

    async def get_all_queues_info(self) -> dict[str, dict[str, Any]]:
        """Get information about all queues"""
        queues = [
            settings.rabbitmq_notifications_queue,
            f"{settings.rabbitmq_notifications_queue}.dlq",
            f"{settings.rabbitmq_notifications_queue}.retry",
            f"{settings.rabbitmq_notifications_queue}.priority",
        ]
        result = {}

        for queue_name in queues:
            info = await self.get_queue_info(queue_name)
            if info:
                result[queue_name] = info

        return result

    async def purge_queue(self, queue_name: str) -> bool:
        """Purge all messages from queue"""
        if not self.channel:
            return False

        try:
            queue = await self.channel.declare_queue(queue_name, durable=True, passive=True)
            await queue.purge()
            logger.info(f"Purged queue: {queue_name}")
            return True
        except Exception:
            logger.exception(f"Failed to purge queue {queue_name}")
            return False

    # Quorum queue specific methods
    async def get_quorum_queue_status(self, queue_name: str) -> dict[str, Any] | None:
        """Get status of a quorum queue"""
        if self.quorum_manager:
            return await self.quorum_manager.get_quorum_queue_status(queue_name)
        return None

    async def get_all_quorum_queues_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all quorum queues"""
        if self.quorum_manager:
            return await self.quorum_manager.get_all_quorum_queues_status()
        return {}

    async def purge_quorum_queue(self, queue_name: str) -> bool:
        """Purge a quorum queue"""
        if self.quorum_manager:
            return await self.quorum_manager.purge_quorum_queue(queue_name)
        return False

    async def delete_quorum_queue(self, queue_name: str) -> bool:
        """Delete a quorum queue"""
        if self.quorum_manager:
            return await self.quorum_manager.delete_quorum_queue(queue_name)
        return False

    def register_quorum_handler(self, queue_name: str, handler: MessageHandler) -> None:
        """Register message handler with quorum queue retry logic"""
        quorum_handler = QuorumMessageHandler(handler)
        self._handlers[queue_name] = quorum_handler
        logger.info(f"Registered quorum handler for queue: {queue_name}")


# Global messaging adapter instance
messaging_adapter: MessagingAdapter | None = None


def get_messaging_adapter() -> MessagingAdapter:
    """Get global messaging adapter instance"""
    global messaging_adapter
    if messaging_adapter is None:
        raise RuntimeError("Messaging adapter not initialized")
    return messaging_adapter


def init_messaging_adapter(connection_url: str, service_name: str = None) -> MessagingAdapter:
    """Initialize global messaging adapter"""
    global messaging_adapter
    messaging_adapter = MessagingAdapter(connection_url, service_name)
    return messaging_adapter


def create_service_adapter(service_name: str, connection_url: str = None) -> MessagingAdapter:
    """Create messaging adapter for specific service"""
    adapter = MessagingAdapter(connection_url, service_name)
    return adapter


def create_shared_adapter(connection_url: str = None) -> MessagingAdapter:
    """Create messaging adapter for shared queues (no service-specific naming)"""
    adapter = MessagingAdapter(connection_url)
    return adapter
