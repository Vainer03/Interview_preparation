"""
Messaging interface for RabbitMQ integration
"""
import sys
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, Tuple
from pydantic import BaseModel
from datetime import datetime, timezone
from enum import Enum, IntEnum
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Make aio_pika defined even if import fails so checks later are safe
aio_pika = None
try:
    import aio_pika
    from aio_pika import ExchangeType, Exchange, Queue, Message, connect_robust, DeliveryMode
    from aio_pika.abc import AbstractConnection, AbstractChannel, AbstractQueue
    from aio_pika.exceptions import AMQPError, ChannelClosed
except ImportError:
    logger.warning("aio_pika not installed; RabbitMQ functionality will be unavailable")

# -------------------------
# Enums / Models
# -------------------------
class MessagePriorityLevel(IntEnum):
    LOWEST = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    HIGHEST = 4


class NotificationType(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    SYSTEM = "system"
    TASK = "task"


class MessageHandler(ABC):
    """Abstract base class for message handlers"""

    @abstractmethod
    async def handle(self, message: Dict[str, Any]) -> None:
        """Handle incoming message"""
        raise NotImplementedError


class NotificationMessage(BaseModel):
    """Base notification message schema"""
    title: str
    message: str
    recipient_id: str
    notification_type: NotificationType = NotificationType.TASK
    sender_id: Optional[str] = None
    related_object_id: Optional[str] = None
    related_object_type: Optional[str] = None
    timestamp: Optional[str] = None
    priority: MessagePriorityLevel = MessagePriorityLevel.MEDIUM

    def __init__(self, **data):
        if 'timestamp' not in data or data.get('timestamp') is None:
            # use UTC ISO format to be consistent
            data['timestamp'] = datetime.now(timezone.utc).isoformat()
        super().__init__(**data)


class RabbitMQConfig(BaseModel):
    """Configuration model for RabbitMQ connection"""
    host: str = "localhost"
    port: int = 5672
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    heartbeat: int = 60
    connection_timeout: int = 10
    max_priority: int = MessagePriorityLevel.HIGHEST.value


# -------------------------
# MessagingAdapter
# -------------------------
class MessagingAdapter:
    """RabbitMQ messaging adapter (singleton)"""

    _instance: Optional['MessagingAdapter'] = None

    def __new__(cls, config: RabbitMQConfig):
        # simple singleton: keep one instance per process
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: RabbitMQConfig):
        # avoid re-initialization on subsequent calls
        if getattr(self, "_initialized", False):
            return

        self.config = config
        self.connection: Optional[AbstractConnection] = None
        self.channel: Optional[AbstractChannel] = None

        # consumers: store tuples (consumer_tag, queue)
        self._consumers: List[Tuple[str, AbstractQueue]] = []
        self._handlers: Dict[str, MessageHandler] = {}
        self._exchanges: Dict[str, Exchange] = {}
        self._queues: Dict[str, AbstractQueue] = {}
        self._is_connected = False

        self._initialized = True

    def _get_connection_url(self) -> str:
        """Construct connection URL from config"""
        # Note: aio_pika.connect_robust accepts kwargs as well; keep URL readable
        vhost = self.config.virtual_host
        # ensure vhost is properly encoded if needed - simple approach:
        return (
            f"amqp://{self.config.username}:{self.config.password}@"
            f"{self.config.host}:{self.config.port}/{vhost}"
            f"?heartbeat={self.config.heartbeat}"
        )

    def _reset_connection(self) -> None:
        self.connection = None
        self.channel = None
        self._is_connected = False

    # -------------------------
    # Connection management
    # -------------------------
    async def connect(self) -> None:
        """Establish connection to RabbitMQ"""
        if aio_pika is None:
            logger.error("aio_pika is required but not installed")
            self._reset_connection()
            raise RuntimeError("aio_pika is required but not installed")

        if self._is_connected:
            logger.debug("Already connected to RabbitMQ")
            return

        try:
            self.connection = await connect_robust(
                self._get_connection_url(),
                timeout=self.config.connection_timeout
            )
            self.channel = await self.connection.channel()
            # sensible default
            await self.channel.set_qos(prefetch_count=10)
            self._is_connected = True
            logger.info("Connected to RabbitMQ")
        except Exception as e:  # broad to catch connect errors
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            self._reset_connection()
            raise ConnectionError("Failed to connect to RabbitMQ") from e

    async def disconnect(self) -> None:
        """Close RabbitMQ connection"""
        if not self._is_connected:
            logger.debug("disconnect called but not connected")
            return

        try:
            await self.stop_consuming()
            if self.connection:
                await self.connection.close()
            logger.info("Disconnected from RabbitMQ")
        except Exception as e:
            logger.error(f"Error disconnecting from RabbitMQ: {e}")
            raise
        finally:
            self._reset_connection()

    @asynccontextmanager
    async def connection_context(self) -> Any:
        """Context manager for connection handling."""
        try:
            if not self._is_connected:
                await self.connect()
            yield self
        finally:
            await self.disconnect()

    # -------------------------
    # Topology helpers
    # -------------------------
    async def declare_exchange(
        self,
        exchange_name: str,
        exchange_type: ExchangeType = ExchangeType.DIRECT,
        durable: bool = True
    ) -> Exchange:
        """Declare a RabbitMQ exchange"""
        if not self._is_connected:
            raise RuntimeError("Not connected to RabbitMQ")

        try:
            exchange = await self.channel.declare_exchange(
                exchange_name,
                exchange_type,
                durable=durable
            )
            self._exchanges[exchange_name] = exchange
            logger.info(f"Declared exchange: {exchange_name} ({exchange_type.value})")
            return exchange
        except Exception as e:
            logger.error(f"Failed to declare exchange {exchange_name}: {e}")
            raise

    async def declare_queue(
        self,
        queue_name: str,
        durable: bool = True,
        arguments: Optional[Dict[str, Any]] = None
    ) -> AbstractQueue:
        """Declare a priority-capable queue"""
        if not self._is_connected:
            raise RuntimeError("Not connected to RabbitMQ")

        args = dict(arguments or {})
        if 'x-max-priority' not in args:
            args['x-max-priority'] = self.config.max_priority

        try:
            queue = await self.channel.declare_queue(
                queue_name,
                durable=durable,
                arguments=args
            )
            self._queues[queue_name] = queue
            logger.info(f"Declared queue: {queue_name} with priority support")
            return queue
        except Exception as e:
            logger.error(f"Failed to declare queue {queue_name}: {e}")
            raise

    async def bind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = ""
    ) -> None:
        """Bind queue to exchange with routing key"""
        if not self._is_connected:
            raise RuntimeError("Not connected to RabbitMQ")

        queue = self._queues.get(queue_name)
        if queue is None:
            raise ValueError(f"Queue {queue_name} not declared")

        exchange = self._exchanges.get(exchange_name)
        if exchange is None:
            raise ValueError(f"Exchange {exchange_name} not declared")

        try:
            await queue.bind(exchange, routing_key=routing_key)
            logger.info(f"Bound queue {queue_name} to exchange {exchange_name} with key '{routing_key}'")
        except Exception as e:
            logger.error(f"Failed to bind queue {queue_name} to exchange {exchange_name}: {e}")
            raise

    # -------------------------
    # Publishing
    # -------------------------
    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        message: Dict[str, Any],
        priority: Optional[int] = None,
        ttl: Optional[int] = None
    ) -> bool:
        """Publish message to exchange with advanced options"""
        if not self._is_connected:
            logger.warning("No RabbitMQ connection, skipping message publish")
            return False

        try:
            message_body = json.dumps(message).encode()
            amqp_message = Message(
                body=message_body,
                delivery_mode=DeliveryMode.PERSISTENT,
                priority=priority,
                expiration=str(ttl * 1000) if ttl else None
            )

            exchange = self._exchanges.get(exchange_name)
            # fallback to default exchange when explicitly requested
            if exchange is None:
                # if the named exchange is not declared, try to publish to default_exchange
                try:
                    default_exchange = getattr(self.channel, "default_exchange", None)
                    if default_exchange is None:
                        raise RuntimeError(f"Exchange {exchange_name} not declared and no default exchange available")
                    exchange = default_exchange
                except Exception:
                    logger.error(f"Exchange {exchange_name} not declared and default exchange access failed")
                    raise

            await exchange.publish(amqp_message, routing_key=routing_key)
            logger.debug(f"Published message to {exchange_name} with key {routing_key}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish message: {e}")
            return False

    async def publish_notification(self, notification: NotificationMessage) -> bool:
        """Publish notification with proper routing"""
        exchange_name = "notifications_exchange"
        # use a pattern that matches bindings; e.g. include priority as last token
        routing_key = f"notification.{notification.notification_type.value}.{notification.priority.name.lower()}"
        # use pydantic's model export; different pydantic versions have different methods - try both
        try:
            payload = notification.model_dump()  # pydantic v2
        except Exception:
            payload = notification.dict()
        return await self.publish(
            exchange_name=exchange_name,
            routing_key=routing_key,
            message=payload,
            priority=notification.priority.value
        )

    # -------------------------
    # Handlers / Consumers
    # -------------------------
    def register_handler(self, queue_name: str, handler: MessageHandler) -> None:
        """Register message handler for specific queue"""
        if not isinstance(handler, MessageHandler):
            raise ValueError("Handler must implement MessageHandler interface")

        self._handlers[queue_name] = handler
        logger.info(f"Registered handler for queue: {queue_name}")

    async def start_consuming(self,
                              queue_name: str,
                              consumer_priority: Optional[int] = None) -> None:
        """Start consuming messages from queue"""
        if not self._is_connected:
            logger.warning("No RabbitMQ connection, skipping consumer start")
            return

        if queue_name not in self._handlers:
            logger.warning(f"No handler registered for queue: {queue_name}")
            return

        try:
            queue = self._queues.get(queue_name)
            if not queue:
                queue = await self.channel.declare_queue(queue_name, durable=True)
                self._queues[queue_name] = queue

            async def process_message(message: Message) -> None:
                # Using message.process() will ACK by default on exit if no errors
                async with message.process():
                    try:
                        body = message.body.decode()
                        data = json.loads(body)
                        # attach priority if present
                        try:
                            data['priority'] = getattr(message, "priority", None)
                        except Exception:
                            data['priority'] = None

                        handler = self._handlers.get(queue_name)
                        if handler:
                            await handler.handle(data)
                        else:
                            logger.warning(f"No handler registered for queue: {queue_name}")
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode message: {e}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")

            # consume returns a consumer_tag (string) for many aio libraries
            consumer_tag = await queue.consume(process_message, arguments={"x-priority": consumer_priority} if consumer_priority is not None else None)
            # store consumer tag with queue so we can cancel it later
            self._consumers.append((consumer_tag, queue))
            logger.info(f"Started consuming from queue: {queue_name} (consumer_tag={consumer_tag})")
        except Exception as e:
            logger.error(f"Failed to start consuming from queue {queue_name}: {e}")
            raise

    async def stop_consuming(self) -> None:
        """Stop all active consumers"""
        if not self._consumers:
            logger.debug("No active consumers to stop")
            return
        try:
            # _consumers holds tuples (consumer_tag, queue)
            for consumer_tag, queue in list(self._consumers):
                try:
                    # Prefer queue.cancel if available
                    cancel_coro = None
                    if hasattr(queue, "cancel"):
                        cancel_coro = queue.cancel(consumer_tag)
                    else:
                        # try cancelling via channel if present
                        chan = getattr(self, "channel", None)
                        if chan and hasattr(chan, "cancel"):
                            cancel_coro = chan.cancel(consumer_tag)
                        else:
                            # last resort: call queue._channel.basic_cancel if it exists (private)
                            if hasattr(queue, "_channel") and hasattr(queue._channel, "basic_cancel"):
                                cancel_coro = queue._channel.basic_cancel(consumer_tag)

                    if cancel_coro is not None:
                        await cancel_coro
                        logger.debug(f"Cancelled consumer {consumer_tag}")
                    else:
                        logger.warning(f"Unable to find cancel method for consumer {consumer_tag}; skipping")
                except Exception as inner:
                    logger.warning(f"Failed to cancel consumer {consumer_tag}: {inner}")
            self._consumers.clear()
            logger.info("Stopped all consumers")
        except Exception as e:
            logger.error(f"Error stopping consumers: {e}")
            raise

    # -------------------------
    # Queue inspection
    # -------------------------
    async def get_queue_info(self,
                             queue_name: str,
                             include_messages: bool = False,
                             message_limit: int = 10
                             ) -> Optional[Dict[str, Any]]:
        """
        Get detailed queue information including optional message sampling

        Args:
            queue_name: Name of the queue to inspect
            include_messages: Whether to include sample messages
            message_limit: Max number of messages to include when sampling

        Returns:
            Dictionary with queue information or None if error occurs
        """
        if not self._is_connected:
            logger.warning("No RabbitMQ connection, cannot get queue info")
            return None

        try:
            # passive declare - will raise if queue doesn't exist
            queue = await self.channel.declare_queue(
                queue_name,
                durable=True,
                passive=True
            )
            # gather basic info - note: attribute names depend on aio_pika internals
            decl = getattr(queue, "declaration_result", None)
            message_count = getattr(decl, "message_count", None) if decl else None
            consumer_count = getattr(decl, "consumer_count", None) if decl else None

            result: Dict[str, Any] = {
                "name": queue.name,
                "message_count": message_count,
                "consumer_count": consumer_count,
                "state": "active"
            }

            if include_messages and (message_count or 0) > 0:
                messages = []
                # iterate without ack (no_ack) to avoid altering queue state when possible
                # NOTE: depending on broker/settings iterator(no_ack=True) may still change server state.
                try:
                    async with queue.iterator(no_ack=True) as queue_iter:
                        async for msg in queue_iter:
                            try:
                                msg_body = json.loads(msg.body.decode())
                                messages.append({
                                    "body": msg_body,
                                    "properties": {
                                        "priority": getattr(msg, "priority", None),
                                        "timestamp": getattr(msg, "timestamp", None),
                                        "expiration": getattr(msg, "expiration", None)
                                    }
                                })
                            except Exception as e:
                                logger.warning(f"Failed to decode message: {e}")
                            if len(messages) >= message_limit:
                                break
                except Exception as e:
                    logger.warning(f"Sampling messages failed: {e}")

                result["sample_messages"] = messages

            return result

        except ChannelClosed as e:
            logger.warning(f"Queue {queue_name} doesn't exist: {e}")
            return {"name": queue_name, "state": "non_existent"}
        except Exception as e:
            logger.error(f"Failed to get queue info for {queue_name}: {e}")
            return None

    # -------------------------
    # Convenience infra setup
    # -------------------------
    async def setup_notification_infrastructure(self) -> None:
        """Setup default notification infrastructure"""
        if not self._is_connected:
            await self.connect()

        # declare topic exchange for notifications
        await self.declare_exchange(
            "notifications_exchange",
            ExchangeType.TOPIC,
            durable=True
        )

        # declare and bind queues for each notification type. Use a wildcard in binding so
        # messages that include priority tokens in routing key still match.
        for n_type in NotificationType:
            queue_name = f"notifications_{n_type.value}"
            await self.declare_queue(queue_name, durable=True)
            # binding lets any priority match: notification.<type>.*
            await self.bind_queue(
                queue_name,
                "notifications_exchange",
                f"notification.{n_type.value}.*"
            )
            logger.info(f"Configured notification queue for {n_type.value}")


# Global messaging adapter instance
messaging_adapter: Optional[MessagingAdapter] = None


async def get_messaging_adapter(
        config: Optional[RabbitMQConfig] = None
) -> MessagingAdapter:
    """Get or initialize global messaging adapter"""
    global messaging_adapter
    if messaging_adapter is None:
        if config is None:
            config = RabbitMQConfig()
        messaging_adapter = MessagingAdapter(config)
        await messaging_adapter.connect()
        await messaging_adapter.setup_notification_infrastructure()
    return messaging_adapter
