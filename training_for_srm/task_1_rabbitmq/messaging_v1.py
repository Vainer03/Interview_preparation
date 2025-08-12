"""
Messaging interface for RabbitMQ integration
"""
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List
from pydantic import BaseModel
from datetime import datetime


logger = logging.getLogger(__name__)




class MessageHandler(ABC):
    """Abstract base class for message handlers"""


    @abstractmethod
    async def handle(self, message: Dict[str, Any]) -> None:
        """Handle incoming message"""
        pass


class NotificationMessage(BaseModel):
    """Base notification message schema"""
    title: str
    message: str
    recipient_id: str
    notification_type: str = "tasks"
    sender_id: Optional[str] = None
    related_object_id: Optional[str] = None
    related_object_type: Optional[str] = None
    timestamp: Optional[str] = None


    def __init__(self, **data):
        if 'timestamp' not in data:
            data['timestamp'] = datetime.now().isoformat()
        super().__init__(**data)




class MessagingAdapter:
    """RabbitMQ messaging adapter"""


    def __init__(self, connection_url: str):
        self.connection_url = connection_url
        self.connection = None
        self.channel = None
        self._handlers: Dict[str, MessageHandler] = {}
        self._consumers: List[Any] = []


    async def connect(self) -> None:
        """Establish connection to RabbitMQ"""
        try:
            import aio_pika
            self.connection = await aio_pika.connect_robust(self.connection_url)
            self.channel = await self.connection.channel()
            logger.info("Connected to RabbitMQ")
        except ImportError:
            logger.warning("aio_pika not installed, using mock connection")
            self.connection = None
            self.channel = None
        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            self.connection = None
            self.channel = None


    async def disconnect(self) -> None:
        """Close RabbitMQ connection"""
        if self.connection:
            await self.connection.close()
            logger.info("Disconnected from RabbitMQ")


    async def publish_notification(self, notification: NotificationMessage) -> bool:
        """Publish notification message to queue"""
        if not self.channel:
            logger.warning("No RabbitMQ connection, skipping message publish")
            return False


        try:
            import aio_pika
            message_body = notification.model_dump_json()
            message = aio_pika.Message(
                body=message_body.encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            )


            await self.channel.default_exchange.publish(
                message,
                routing_key="notifications"
            )
            logger.info(f"Published notification for recipient: {notification.recipient_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish notification: {e}")
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
            import aio_pika
            queue = await self.channel.declare_queue(queue_name, durable=True)


            async def process_message(message):
                async with message.process():
                    try:
                        body = message.body.decode()
                        data = json.loads(body)


                        if queue_name in self._handlers:
                            await self._handlers[queue_name].handle(data)
                        else:
                            logger.warning(f"No handler registered for queue: {queue_name}")


                    except Exception as e:
                        logger.error(f"Error processing message: {e}")


            consumer = await queue.consume(process_message)
            self._consumers.append(consumer)
            logger.info(f"Started consuming from queue: {queue_name}")


        except Exception as e:
            logger.error(f"Failed to start consuming from queue {queue_name}: {e}")


    async def stop_consuming(self) -> None:
        """Stop all consumers"""
        try:
            import aio_pika
            for consumer in self._consumers:
                await consumer.cancel()
            self._consumers.clear()
            logger.info("Stopped all consumers")
        except Exception as e:
            logger.error(f"Error stopping consumers: {e}")


    async def get_queue_info(self, queue_name: str) -> Optional[Dict[str, Any]]:
        """Get queue information"""
        if not self.channel:
            return None


        try:
            import aio_pika
            queue = await self.channel.declare_queue(queue_name, durable=True, passive=True)
            return {
                "name": queue.name,
                "message_count": queue.declaration_result.message_count,
                "consumer_count": queue.declaration_result.consumer_count,
            }
        except Exception as e:
            logger.error(f"Failed to get queue info: {e}")
            return None




# Global messaging adapter instance
messaging_adapter: Optional[MessagingAdapter] = None




def get_messaging_adapter() -> MessagingAdapter:
    """Get global messaging adapter instance"""
    global messaging_adapter
    if messaging_adapter is None:
        raise RuntimeError("Messaging adapter not initialized")
    return messaging_adapter




def init_messaging_adapter(connection_url: str) -> MessagingAdapter:
    """Initialize global messaging adapter"""
    global messaging_adapter
    messaging_adapter = MessagingAdapter(connection_url)
    return messaging_adapter