"""
Messaging interface for RabbitMQ integration
"""
import json
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, Tuple
from pydantic import BaseModel, field_validator
from datetime import datetime
from enum import Enum, IntEnum
from contextlib import asynccontextmanager
from urllib.parse import quote


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
    """Configuration model for RabbitMQ connection"""
    host: str = "host.docker.internal"
    port: int = 5672
    username: str = "guest"
    password: str = "guest"
    virtual_host: str = "/"
    heartbeat: int = 60
    connection_timeout: int = 10
    max_priority: int = MessagePriorityLevel.HIGHEST.value


class MessagingAdapter:
    """RabbitMQ messaging adapter"""

    _instance: Optional['MessagingAdapter'] = None
   
    def __new__(cls, config: RabbitMQConfig) -> 'MessagingAdapter':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.config = config
            cls._instance.connection = None
            cls._instance.channel = None
            cls._instance._handlers = {}
            cls._instance._consumers = []
            cls._instance._exchanges = {}
            cls._instance._queues = {}
            cls._instance._is_connected = False
        return cls._instance


    def __init__(self, config: RabbitMQConfig):
        self.config = config
        self.connection: Optional[AbstractConnection] = None
        self.channel: Optional[AbstractChannel] = None
        self._handlers: Dict[str, MessageHandler] = {}
        self._consumers: List[Tuple[str, AbstractQueue]] = []
        self._exchanges: Dict[str, Exchange] = {}
        self._queues: Dict[str, AbstractQueue] = {}
        self._is_connected = False
        self._initialized = True
        pass


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

    def is_connected(self) -> bool:
        """Check actual connection state"""
        return (self._is_connected and 
                self.connection and 
                not self.connection.is_closed and
                self.channel and
                not self.channel.is_closed)    

    async def connect(self) -> None:
        """Establish connection to RabbitMQ"""
        if aio_pika is None:
            self._reset_connection()
            logger.error("aio_pika is required but not installed")
            raise ConnectionError("aio_pika is required but not installed")

        if self._is_connected:
            logger.debug("Already connected to RabbitMQ")
            return

        try:
            # Explicitly use the module's connect_robust
            self.connection = await aio_pika.connect_robust(
                self._get_connection_url(),
                timeout=self.config.connection_timeout
            )
            self.channel = await self.connection.channel()
            await self.channel.set_qos(prefetch_count=10)
            self._is_connected = True
            logger.info("Connected to RabbitMQ")
        except AMQPError as e:
            self._reset_connection()
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            raise ConnectionError(f"Failed to connect to RabbitMQ: {e}")
        except Exception as e:
            self._reset_connection()
            logger.error(f"Unexpected error connecting to RabbitMQ: {e}")
            raise ConnectionError(f"Unexpected error connecting to RabbitMQ: {e}")

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


    async def declare_queue(
        self,
        queue_name: str,
        durable: bool = True,
        arguments: Optional[Dict[str, Any]] = None
    ) -> AbstractQueue:
       
        """Declare a priority queue"""
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
        except AMQPError as e:
            logger.error(f"Failed to declare queue {queue_name}: {e}")


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
        except AMQPError as e:
            logger.error(f"Failed to declare exchange {exchange_name}: {e}")


    async def bind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = ""
    ) -> None:
        """Bind queue to exchange with routing key"""
       
        if not self._is_connected:
            raise RuntimeError("Not connected to RabbitMQ")
       
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
            message = Message(
                body=message_body,
                delivery_mode=DeliveryMode.PERSISTENT,
                priority=priority,
                expiration=str(ttl * 1000) if ttl else None
            )


            exchange = self._exchanges.get(exchange_name, self.channel.default_exchange)
            await exchange.publish(message, routing_key=routing_key)
            logger.debug(f"Published message to {exchange_name} with key {routing_key}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish message: {e}")
            return False




    async def publish_notification(self, notification: NotificationMessage) -> bool:
        """Publish notification with proper routing"""
        exchange_name = "notifications_exchange"
        routing_key = f"notification.{notification.notification_type.value}.{notification.priority.name.lower()}"
       
        return await self.publish(
            exchange_name=exchange_name,
            routing_key=routing_key,
            message=notification.model_dump(),
            priority=notification.priority.value
        )


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


        for n_type in NotificationType:
            queue_name = f"notifications_{n_type.value}"
            await self.declare_queue(queue_name, durable=True)
            await self.bind_queue(
                queue_name,
                "notifications_exchange",
                f"notification.{n_type.value}"
            )
            logger.info(f"Configured notification queue for {n_type.value}")

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
                return


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
                # await queue.channel.set_qos(prefetch_count=message_limit)

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






# Global messaging adapter instance
_messaging_adapter_lock = threading.Lock()
_messaging_adapter: Optional[MessagingAdapter] = None

async def get_messaging_adapter(
    config: Optional[RabbitMQConfig] = None
) -> MessagingAdapter:
    """Thread-safe access to global messaging adapter"""
    global _messaging_adapter
    
    if _messaging_adapter is None:
        with _messaging_adapter_lock:
            if _messaging_adapter is None:  # Double-check pattern
                if config is None:
                    config = RabbitMQConfig()
                _messaging_adapter = MessagingAdapter(config)
                await _messaging_adapter.connect()
    
    return _messaging_adapter
