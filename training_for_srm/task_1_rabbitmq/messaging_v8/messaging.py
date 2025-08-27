"""
Enhanced messaging interface for RabbitMQ integration with quorum queues and priority support
"""
import json
import logging
import threading
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, Tuple, Union
from pydantic import BaseModel, field_validator
from datetime import datetime
from enum import Enum, IntEnum
from contextlib import asynccontextmanager
from urllib.parse import quote

from .config import settings
from .quorum_queues import QuorumMessageHandler, QuorumQueueManager

logger = logging.getLogger(__name__)

try:
    import aio_pika
    from aio_pika import ExchangeType, Exchange, Queue, Message, connect_robust, DeliveryMode
    from aio_pika.abc import AbstractConnection, AbstractChannel, AbstractQueue
    from aio_pika.exceptions import AMQPError, ChannelClosed
except ImportError:
    logger.warning("aio_pika not installed")
    aio_pika = None


class MessagePriorityLevel(IntEnum):
    """A IntEnum class for message priority levels"""
    LOWEST = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    HIGHEST = 4


class NotificationType(str, Enum):
    """A Enum class for types of notifications"""
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


class NotificationMessage(BaseModel):
    """Base notification message schema"""
    title: str
    message: str
    recipient_id: str
    sender_id: Optional[str] = None
    notification_type: NotificationType = NotificationType.TASK
    related_object_id: Optional[str] = None
    related_object_type: Optional[str] = None
    timestamp: Optional[str] = None
    priority: MessagePriorityLevel = MessagePriorityLevel.MEDIUM

    def __init__(self, **data):
        if 'timestamp' not in data:
            data['timestamp'] = datetime.now().isoformat()
        super().__init__(**data)

    @field_validator('title', 'message', 'recipient_id')
    def validate_non_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()


class RabbitMQConfig(BaseModel):
    """Configuration model for the RabbitMQ connection"""
    host: str = "host.docker.internal"
    port: int = 5672
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    heartbeat: int = 60
    connection_timeout: int = 10
    connection_retries: int = 2
    retry_delay: int = 3
    max_priority: int = MessagePriorityLevel.HIGHEST.value


class MessagingAdapter:
    """RabbitMQ messaging adapter with quorum queues and priority support"""

    def __init__(self, config: Optional[RabbitMQConfig] = None, service_name: Optional[str] = None):
        self.config = config or RabbitMQConfig()
        self.service_name = service_name
        self.connection: Optional[AbstractConnection] = None
        self.channel: Optional[AbstractChannel] = None
        self._handlers: Dict[str, MessageHandler] = {}
        self._consumers: List[Tuple[str, Any]] = []
        self._exchanges: Dict[str, Exchange] = {}
        self._queues: Dict[str, AbstractQueue] = {}
        self._is_connected = False
        self.quorum_manager: Optional[QuorumQueueManager] = None

    def _get_connection_url(self) -> str:
        """Construct connection URL from config"""
        safe_username = quote(self.config.username)
        safe_password = quote(self.config.password)
        return (
            f"amqp://{safe_username}:{safe_password}@"
            f"{self.config.host}:{self.config.port}/{self.config.virtual_host}"
            f"?heartbeat={self.config.heartbeat}"
        )

    def _reset_connection(self) -> None:
        self.connection = None
        self.channel = None
        self._is_connected = False


    async def connect(self) -> None:
        """Establish connection with guaranteed error handling"""
        if self.is_connected():
            return

        if aio_pika is None:
            logger.warning("aio_pika not installed, using mock connection")
            self.connection = None
            self.channel = None
            return

        last_error = None
        for attempt in range(self.config.connection_retries):
            try:
                # Clean up any existing connection
                if self.connection:
                    await self.connection.close()
                self._reset_connection()

                # Attempt connection
                self.connection = await aio_pika.connect_robust(
                    self._get_connection_url(),
                    timeout=self.config.connection_timeout
                )
                self.channel = await self.connection.channel()
                await self.channel.set_qos(prefetch_count=10)
                self._is_connected = True
                
                # Initialize quorum queue manager
                self.quorum_manager = QuorumQueueManager(self)
                
                logger.info(f"Connected to RabbitMQ (attempt {attempt + 1})")
                return

            except Exception as e:
                last_error = e
                logger.warning(f"Connection attempt {attempt + 1} failed: {str(e)}")
                if attempt < self.config.connection_retries - 1:
                    await asyncio.sleep(self.config.retry_delay)
                continue

        # If we get here, all attempts failed
        self._reset_connection()
        raise ConnectionError(
            f"Failed to connect after {self.config.connection_retries} attempts. "
            f"Last error: {str(last_error)}"
        ) from last_error

    async def disconnect(self) -> None:
        """Close RabbitMQ connection"""
        if not self._is_connected:
            return

        try:
            await self.stop_consuming()
            if self.connection:
                await self.connection.close()
            logger.info("Disconnected from RabbitMQ")
        except AMQPError as e:
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

    async def declare_queue(
        self,
        queue_name: str,
        durable: bool = True,
        arguments: Optional[Dict[str, Any]] = None
    ) -> AbstractQueue:
        """Declare a queue with priority support"""
        if not self._is_connected:
            raise ConnectionError("Not connected to RabbitMQ")

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
        except AMQPError as e:
            logger.error(f"Failed to declare queue {queue_name}: {e}")
            raise

    async def declare_exchange(
        self,
        exchange_name: str,
        exchange_type: ExchangeType = ExchangeType.DIRECT,
        durable: bool = True
    ) -> Exchange:
        """Declare a RabbitMQ exchange"""
        if not self._is_connected:
            raise ConnectionError("Not connected to RabbitMQ")

        try:
            exchange = await self.channel.declare_exchange(
                exchange_name,
                exchange_type,
                durable=durable
            )
            self._exchanges[exchange_name] = exchange
            logger.info(f"Declared exchange: {exchange_name} ({exchange_type.value})")
            return exchange
        except AMQPError as e:
            logger.error(f"Failed to declare exchange {exchange_name}: {e}")
            raise

    async def bind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = ""
    ) -> None:
        """Bind queue to exchange with routing key"""
        if not self._is_connected:
            raise ConnectionError("Not connected to RabbitMQ")
       
        if queue_name not in self._queues:
            raise ValueError(f"Queue {queue_name} not declared")
        if exchange_name not in self._exchanges:
            raise ValueError(f"Exchange {exchange_name} not declared")

        try:
            await self._queues[queue_name].bind(
                self._exchanges[exchange_name],
                routing_key=routing_key
            )
            logger.info(f"Bound queue {queue_name} to exchange {exchange_name} with key '{routing_key}'")
        except AMQPError as e:
            logger.error(f"Failed to bind queue {queue_name} to exchange {exchange_name}: {e}")
            raise

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

        priority = priority if priority is not None else MessagePriorityLevel.MEDIUM.value

        try:
            message_body = json.dumps(message).encode()
            message_obj = Message(
                body=message_body,
                delivery_mode=DeliveryMode.PERSISTENT,
                priority=priority,
                expiration=str(ttl * 1000) if ttl else None
            )

            exchange = self._exchanges.get(exchange_name, self.channel.default_exchange)
            await exchange.publish(message_obj, routing_key=routing_key)
            logger.debug(f"Published message to {exchange_name} with key {routing_key}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish message: {e}")
            return False

    async def publish_message(self, message: BaseModel, queue_type: str = "main", priority: int = 0) -> bool:
        """Publish message to service-specific queue"""
        if not self._is_connected:
            logger.warning("No RabbitMQ connection, skipping message publish")
            return False

        try:
            message_body = message.model_dump_json()

            # Determine queue based on type and priority
            if priority > 0:
                queue_name = self.get_service_queue("priority")
            else:
                queue_name = self.get_service_queue(queue_type)

            message_obj = Message(
                body=message_body.encode(),
                delivery_mode=DeliveryMode.PERSISTENT,
                priority=priority if priority > 0 else None,
            )

            # Publish to default exchange with queue name as routing key
            await self.channel.default_exchange.publish(
                message_obj,
                routing_key=queue_name,
            )

            logger.info(f"Published message to {queue_name} for service {self.service_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to publish message: {e}")
            return False

    async def publish_notification(self, notification: NotificationMessage, priority: int = 0) -> bool:
        """Publish notification message to queue with priority support"""
        return await self.publish_message(notification, "main", priority)

    async def publish_notification_with_retry(self, notification: NotificationMessage, max_retries: int = 3) -> bool:
        """Publish notification with retry mechanism"""
        for attempt in range(max_retries):
            try:
                success = await self.publish_notification(notification)
                if success:
                    return True
                logger.warning(f"Attempt {attempt + 1} failed, retrying...")
            except Exception as e:
                logger.exception(f"Attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
        return False

    def register_handler(self, queue_name: str, handler: MessageHandler) -> None:
        """Register message handler for specific queue"""
        if not isinstance(handler, MessageHandler):
            raise ValueError("Handler must implement MessageHandler interface")
       
        self._handlers[queue_name] = handler
        logger.info(f"Registered handler for queue: {queue_name}")

    async def start_consuming(
        self, 
        queue_name: str,
        consumer_priority: Optional[int] = None
    ) -> None:
        """Start consuming messages from queue"""
        if not self._is_connected:
            logger.warning("No RabbitMQ connection, skipping consumer start")
            return
        
        if queue_name not in self._handlers:
            logger.warning(f"No handler registered for queue: {queue_name}")
            return

        try:
            # Get or create queue
            queue = self._queues.get(queue_name)
            if queue is None:
                queue = await self.channel.declare_queue(
                    queue_name,
                    durable=True,
                    arguments={'x-max-priority': self.config.max_priority} if self.config.max_priority else None
                )
                self._queues[queue_name] = queue

            # Message processing callback
            async def process_message(message: Message) -> None:
                try:
                    async with message.process():
                        try:
                            message_data = json.loads(message.body.decode())
                            message_data['priority'] = getattr(message, 'priority', None)
                            await self._handlers[queue_name].handle(message_data)
                        except json.JSONDecodeError as e:
                            logger.error(f"Message decode error: {e}")
                        except Exception as e:
                            logger.error(f"Message processing error: {e}")
                except Exception as e:
                    logger.error(f"Message context error: {e}")

            # Start consuming with optional priority
            consumer_args = {}
            if consumer_priority is not None:
                consumer_args['arguments'] = {'x-priority': consumer_priority}
            
            consumer_tag = await queue.consume(process_message, **consumer_args)
            self._consumers.append((queue_name, consumer_tag))
            logger.info(f"Started consuming from {queue_name}")

        except Exception as e:
            logger.error(f"Failed to start consumer: {e}")
            raise

    async def stop_consuming(self) -> None:
        """Stop all active consumers"""
        if not self._consumers:
            logger.debug("No active consumers to stop")
            return
        try:
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

    async def setup_notification_infrastructure(self) -> None:
        """Setup default notification infrastructure"""
        if not self._is_connected:
            await self.connect()
        await self.declare_exchange(
            "notifications_exchange",
            ExchangeType.TOPIC,
            durable=True
        )

    async def get_queue_info(
        self,
        queue_name: str,
        include_messages: bool = False,
        message_limit: int = 10
    ) -> Optional[Dict[str, Any]]:
        """
        Get detailed queue information including optional message sampling
        """
        if not self._is_connected:
            logger.warning("No RabbitMQ connection, cannot get queue info")
            return None

        try:
            # Declare queue in passive mode to get info without modifying it
            queue = await self.channel.declare_queue(
                queue_name,
                durable=True,
                passive=True  # Important for just getting info
            )
        
            result = {
                "name": queue.name,
                "message_count": queue.declaration_result.message_count,
                "consumer_count": queue.declaration_result.consumer_count,
                "state": "active",
            }
        
            if include_messages and queue.declaration_result.message_count > 0:
                # Get sample messages without consuming them
                messages = []

                try:
                    async with queue.iterator(no_ack=True) as queue_iter:
                        async for message in queue_iter:
                            try:
                                msg_body = json.loads(message.body.decode())
                                messages.append({
                                    "body": msg_body,
                                    "properties": {
                                        "priority": message.priority,
                                        "timestamp": message.timestamp,
                                        "expiration": message.expiration
                                    }
                                })
                                if len(messages) >= message_limit:
                                    break
                            except Exception as e:
                                logger.warning(f"Failed to decode message: {e}")
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

    async def get_all_queues_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about all queues"""
        if not self.service_name:
            queues = [
                settings.rabbitmq_notifications_queue,
                f"{settings.rabbitmq_notifications_queue}.dlq",
                f"{settings.rabbitmq_notifications_queue}.retry",
                f"{settings.rabbitmq_notifications_queue}.priority",
            ]
        else:
            queues = [
                self.get_service_queue("main"),
                self.get_service_queue("dlq"),
                self.get_service_queue("retry"),
                self.get_service_queue("priority"),
            ]
        
        result = {}
        for queue_name in queues:
            info = await self.get_queue_info(queue_name)
            if info:
                result[queue_name] = info

        return result

    async def purge_queue(self, queue_name: str) -> bool:
        """Purge all messages from queue"""
        if not self._is_connected:
            return False

        try:
            queue = await self.channel.declare_queue(queue_name, durable=True, passive=True)
            await queue.purge()
            logger.info(f"Purged queue: {queue_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to purge queue {queue_name}: {e}")
            return False

    # Quorum queue specific methods
    async def get_quorum_queue_status(self, queue_name: str) -> Dict[str, Any] | None:
        """Get status of a quorum queue"""
        if self.quorum_manager:
            return await self.quorum_manager.get_quorum_queue_status(queue_name)
        return None

    async def get_all_quorum_queues_status(self) -> Dict[str, Dict[str, Any]]:
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
_messaging_adapter_lock = threading.Lock()
_messaging_adapters: Dict[str, MessagingAdapter] = {}


async def get_messaging_adapter(
    config: Optional[RabbitMQConfig] = None,
    service_name: Optional[str] = None,
    reconnect: bool = False
) -> MessagingAdapter:
    """
    Get or create a messaging adapter instance.
    
    Args:
        config: RabbitMQ configuration (optional)
        service_name: Service name for service-specific queues. 
                     If None, creates a shared adapter.
        reconnect: Force reconnection even if adapter exists
    
    Returns:
        MessagingAdapter instance
    """
    global _messaging_adapters
    
    adapter_key = service_name or "shared"
    
    with _messaging_adapter_lock:
        if adapter_key in _messaging_adapters and not reconnect:
            adapter = _messaging_adapters[adapter_key]
            # Ensure adapter is connected
            if not adapter.is_connected():
                await adapter.connect()
            return adapter
        
        # Create new adapter
        if config is None:
            config = RabbitMQConfig()
        
        adapter = MessagingAdapter(config, adapter_key)
        await adapter.connect()
        await adapter.setup_notification_infrastructure()
        
        _messaging_adapters[adapter_key] = adapter
        return adapter


async def close_all_adapters() -> None:
    """
    Close all messaging adapter connections.
    """
    global _messaging_adapters
    
    with _messaging_adapter_lock:
        for adapter_key, adapter in list(_messaging_adapters.items()):
            try:
                await adapter.disconnect()
            except Exception as e:
                logger.error(f"Error closing adapter {adapter_key}: {e}")
        
        _messaging_adapters.clear()