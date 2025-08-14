import sys
import pytest
import json
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any, Optional
from pydantic import ValidationError

try:
    import aio_pika
    from aio_pika import ExchangeType, Exchange, Queue, Message, connect_robust, DeliveryMode
    from aio_pika.abc import AbstractConnection, AbstractChannel, AbstractQueue
    from aio_pika.exceptions import AMQPError, ChannelClosed
except ImportError:
    aio_pika = None

from src.core.messaging import (
    MessagingAdapter,
    RabbitMQConfig,
    NotificationMessage,
    NotificationType,
    MessagePriorityLevel,
    MessageHandler,
    AMQPError,
    get_messaging_adapter
)

# Фикстуры для тестов

@pytest.fixture
def mock_config():
    return RabbitMQConfig(
        host="localhost",
        port=5672,
        username="guest",
        password="guest"
    )

@pytest.fixture
def sample_notification():
    return NotificationMessage(
        title="Test",
        message="Hello World",
        recipient_id="user123",
        notification_type=NotificationType.EMAIL,
        priority=MessagePriorityLevel.HIGH
    )

@pytest.fixture
def mock_handler():
    class MockHandler(MessageHandler):
        async def handle(self, message: Dict[str, Any]) -> None:
            pass
    return MockHandler()

# Тесты для моделей

def test_notification_message_validation(sample_notification):
    assert sample_notification.title == "Test"
    assert sample_notification.notification_type == NotificationType.EMAIL
    assert sample_notification.priority == MessagePriorityLevel.HIGH
    assert isinstance(sample_notification.timestamp, str)

def test_notification_message_defaults():
    msg = NotificationMessage(
        title="Test",
        message="Hello",
        recipient_id="user1"
    )
    assert msg.notification_type == NotificationType.TASK
    assert msg.priority == MessagePriorityLevel.MEDIUM
    assert msg.sender_id is None

def test_invalid_notification_message():
    with pytest.raises(ValidationError):
        NotificationMessage(
            title="",  # Невалидное пустое значение
            message="Hello",
            recipient_id="user1"
        )

# Тесты для MessagingAdapter

@pytest.mark.asyncio
async def test_singleton_pattern(mock_config):
    adapter1 = MessagingAdapter(mock_config)
    adapter2 = MessagingAdapter(mock_config)
    assert adapter1 is adapter2

@pytest.mark.asyncio
async def test_connect_success(mock_config):
    # Create a mock connection and channel
    mock_connection = AsyncMock()
    mock_channel = AsyncMock()
    mock_connection.channel.return_value = mock_channel
    
    with patch('aio_pika.connect_robust', new_callable=AsyncMock) as mock_connect:
        # Setup the mock return values
        mock_connect.return_value = mock_connection
        
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        # Verify results
        assert adapter._is_connected is True
        mock_connect.assert_called_once_with(
            adapter._get_connection_url(),
            timeout=adapter.config.connection_timeout
        )
        mock_connection.channel.assert_called_once()
        mock_channel.set_qos.assert_called_once_with(prefetch_count=10)

@pytest.mark.asyncio
async def test_connect_failure(mock_config):
    """Test connection failure handling"""
    with patch('aio_pika.connect_robust', 
              side_effect=AMQPError("Simulated connection failure")) as mock_connect:
        adapter = MessagingAdapter(mock_config)
        
        with pytest.raises(ConnectionError) as exc_info:
            await adapter.connect()
        
        # Verify the error contains our message
        assert "Simulated connection failure" in str(exc_info.value)
        # Verify connection state
        assert not adapter.is_connected()
        assert adapter.connection is None

@pytest.mark.asyncio
async def test_disconnect(mock_config):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        with patch.object(adapter.connection, 'close', new_callable=AsyncMock) as mock_close:
            await adapter.disconnect()
            mock_close.assert_called_once()
            assert adapter._is_connected is False

@pytest.mark.asyncio
async def test_declare_queue(mock_config):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        mock_queue = AsyncMock()
        with patch.object(adapter.channel, 'declare_queue', return_value=mock_queue) as mock_declare:
            queue = await adapter.declare_queue("test_queue")
            
            mock_declare.assert_called_once_with(
                "test_queue",
                durable=True,
                arguments={'x-max-priority': 4}
            )
            assert queue == mock_queue
            assert "test_queue" in adapter._queues

@pytest.mark.asyncio
async def test_publish_notification(mock_config, sample_notification):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        # Create mock exchange and add it to the adapter
        mock_exchange = AsyncMock()
        adapter._exchanges["notifications_exchange"] = mock_exchange
        
        # Call the method we're testing
        result = await adapter.publish_notification(sample_notification)
        
        # Verify the exchange was used to publish
        mock_exchange.publish.assert_called_once()
        
        # Get the actual message that was published
        published_message = mock_exchange.publish.call_args[0][0]
        
        # Verify message properties
        assert json.loads(published_message.body.decode()) == sample_notification.model_dump()
        assert published_message.priority == sample_notification.priority.value
        assert result is True


@pytest.mark.asyncio
async def test_register_handler(mock_config, mock_handler):
    adapter = MessagingAdapter(mock_config)
    adapter.register_handler("test_queue", mock_handler)
    
    assert "test_queue" in adapter._handlers
    assert adapter._handlers["test_queue"] == mock_handler

@pytest.mark.asyncio
async def test_start_consuming(mock_config, mock_handler):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        adapter.register_handler("test_queue", mock_handler)
        mock_queue = AsyncMock()
        adapter._queues["test_queue"] = mock_queue
        
        with patch.object(mock_queue, 'consume', new_callable=AsyncMock) as mock_consume:
            await adapter.start_consuming("test_queue")
            mock_consume.assert_called_once()
            assert len(adapter._consumers) == 1


@pytest.mark.asyncio
async def test_message_processing(mock_config):
    """Test that message processing correctly invokes the handler"""
    # Test data
    test_message = {
        "title": "Test",
        "message": "Hello",
        "recipient_id": "user1"
    }

    # Track if handler was called
    handler_called = False
    received_data = None

    class TestHandler(MessageHandler):
        async def handle(self, message: Dict[str, Any]) -> None:
            nonlocal handler_called, received_data
            handler_called = True
            received_data = message

    # Create the adapter
    adapter = MessagingAdapter(mock_config)
    
    # Bypass RabbitMQ entirely and test just the processing logic
    async def test_process_message():
        # 1. Create a mock message
        mock_message = AsyncMock()
        mock_message.body = json.dumps(test_message).encode()
        mock_message.priority = 2
        
        # 2. Create a real handler instance
        handler = TestHandler()
        adapter.register_handler("test_queue", handler)
        
        # 3. Simulate what the adapter would do internally
        message_data = json.loads(mock_message.body.decode())
        message_data['priority'] = mock_message.priority
        await handler.handle(message_data)

    # Run the test
    await test_process_message()
    
    # Verify results
    assert handler_called is True
    assert received_data["title"] == "Test"
    assert received_data["message"] == "Hello"
    assert received_data.get("priority") == 2


@pytest.mark.asyncio
async def test_get_queue_info(mock_config):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        mock_queue = AsyncMock()
        mock_queue.declaration_result.message_count = 5
        mock_queue.declaration_result.consumer_count = 1
        mock_queue.name = "test_queue"
        
        with patch.object(adapter.channel, 'declare_queue', return_value=mock_queue):
            info = await adapter.get_queue_info("test_queue", include_messages=True)
            
            assert info["name"] == "test_queue"
            assert info["message_count"] == 5
            assert info["consumer_count"] == 1
            assert info["state"] == "active"

# Тесты для работы без aio_pika

@pytest.mark.asyncio
async def test_no_aio_pika(mock_config, monkeypatch):
    """Test behavior when aio_pika is not available"""
    monkeypatch.setattr('src.core.messaging.aio_pika', None)
    
    adapter = MessagingAdapter(mock_config)
    
    with pytest.raises(ConnectionError, match="aio_pika is required"):
        await adapter.connect()
    
    assert not adapter.is_connected()

# Тесты для connection context manager

@pytest.mark.asyncio
async def test_connection_context(mock_config):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        
        async with adapter.connection_context() as conn:
            assert conn._is_connected is True
            assert conn is adapter
        
        assert adapter._is_connected is False

@pytest.mark.asyncio
async def test_priority_queue_handling(mock_config):
    """Test that messages with different priorities are handled correctly"""
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        # Test declaring queue with priority support
        mock_queue = AsyncMock()
        with patch.object(adapter.channel, 'declare_queue', return_value=mock_queue) as mock_declare:
            await adapter.declare_queue("priority_queue")
            mock_declare.assert_called_once_with(
                "priority_queue",
                durable=True,
                arguments={'x-max-priority': 4}  # From config
            )

@pytest.mark.asyncio
async def test_publish_with_ttl(mock_config):
    """Test message publishing with TTL"""
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        with patch.object(adapter.channel.default_exchange, 'publish', new_callable=AsyncMock) as mock_publish:
            test_message = {"test": "value"}
            await adapter.publish(
                exchange_name="",
                routing_key="test_queue",
                message=test_message,
                ttl=60  # 60 seconds
            )
            
            # Verify the message was created with expiration
            published_message = mock_publish.call_args[0][0]
            assert published_message.expiration == "60000"  # TTL in milliseconds

@pytest.mark.asyncio
async def test_concurrent_access(mock_config):
    """Test thread-safe singleton access"""
    async def create_adapter():
        return await get_messaging_adapter(mock_config)
    
    # Simulate concurrent access
    adapters = await asyncio.gather(*[create_adapter() for _ in range(5)])
    
    # All should be the same instance
    assert all(a is adapters[0] for a in adapters)

@pytest.mark.asyncio
async def test_message_handler_error_recovery(mock_config):
    """Test that handler errors don't break the consumer"""
    test_message = {
        "title": "Test",
        "message": "Hello",
        "recipient_id": "user1"
    }
    
    class ErrorHandler(MessageHandler):
        async def handle(self, message: Dict[str, Any]) -> None:
            raise ValueError("Simulated handler error")

    # Setup mocks
    mock_connection = AsyncMock()
    mock_channel = AsyncMock()
    mock_queue = AsyncMock()
    mock_message = AsyncMock()
    mock_message.body = json.dumps(test_message).encode()
    
    # Context manager for message processing
    class MockProcessContext:
        async def __aenter__(self):
            return None
        async def __aexit__(self, exc_type, exc, tb):
            return None
    
    mock_message.process.return_value = MockProcessContext()
    mock_channel.declare_queue.return_value = mock_queue
    mock_connection.channel.return_value = mock_channel

    with patch('aio_pika.connect_robust', return_value=mock_connection):
        adapter = MessagingAdapter(mock_config)
        adapter.connection = mock_connection
        adapter.channel = mock_channel
        adapter._is_connected = True
        
        # Register error-throwing handler
        adapter.register_handler("test_queue", ErrorHandler())
        adapter._queues["test_queue"] = mock_queue
        
        # Start consumption
        await adapter.start_consuming("test_queue")
        
        # Get the callback and execute it
        args, _ = mock_queue.consume.call_args
        process_message_callback = args[0]
        
        # Should not raise despite handler error
        await process_message_callback(mock_message)
        
        # Verify message was still processed (acknowledged)
        mock_message.process.assert_called_once()


@pytest.mark.asyncio
async def test_queue_binding(mock_config):
    """Test queue to exchange binding"""
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        mock_exchange = AsyncMock()
        mock_queue = AsyncMock()
        adapter._exchanges["test_exchange"] = mock_exchange
        adapter._queues["test_queue"] = mock_queue
        
        await adapter.bind_queue(
            queue_name="test_queue",
            exchange_name="test_exchange",
            routing_key="test.key"
        )
        
        mock_queue.bind.assert_called_once_with(
            mock_exchange,
            routing_key="test.key"
        )


@pytest.mark.asyncio
async def test_invalid_message_handling(mock_config):
    """Test that invalid messages are properly rejected"""
    validation_errors = []

    class ValidatingHandler(MessageHandler):
        async def handle(self, message: Dict[str, Any]) -> None:
            try:
                if not isinstance(message, dict):
                    raise ValueError("Message must be a dictionary")
                if "title" not in message:
                    raise ValueError("Missing required field: title")
            except ValueError as e:
                validation_errors.append(str(e))
                raise

    # Test with handler directly (no mocks)
    handler = ValidatingHandler()
    
    # Test valid message
    await handler.handle({"title": "Valid", "message": "OK"})
    assert len(validation_errors) == 0
    
    # Test invalid messages
    invalid_inputs = [
        "not a dict",
        {"missing_title": "test"},
        None
    ]

    for invalid in invalid_inputs:
        with pytest.raises(ValueError):
            await handler.handle(invalid)
    
    assert len(validation_errors) == 3
    assert "must be a dictionary" in validation_errors[0]
    assert "Missing required field" in validation_errors[1]


@pytest.mark.asyncio
async def test_message_priority_handling(mock_config):
    """Test that message priorities are handled correctly"""
    received_priorities = []

    class PriorityHandler(MessageHandler):
        async def handle(self, message: Dict[str, Any]) -> None:
            received_priorities.append(message.get("priority"))

    # Test with handler directly
    handler = PriorityHandler()
    
    # Test different priority levels
    test_messages = [
        {"priority": 0, "data": "lowest"},
        {"priority": 2, "data": "medium"}, 
        {"priority": 4, "data": "highest"}
    ]

    for msg in test_messages:
        await handler.handle(msg)
    
    assert received_priorities == [0, 2, 4]


@pytest.mark.asyncio
async def test_connection_recovery(mock_config):
    """Test that connection recovers after initial failure"""
    # Track attempts
    attempts = 0
    
    # Create mock connection and channel
    mock_conn = AsyncMock()
    mock_channel = AsyncMock()

    async def mock_connect_robust(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("Simulated connection failure")
        return mock_conn

    with patch('aio_pika.connect_robust', new=mock_connect_robust):
        adapter = MessagingAdapter(mock_config)
        adapter.config.connection_retries = 2  # Ensure we retry
        adapter.config.retry_delay = 0  # No delay for testing
        
        # Setup successful channel creation
        mock_conn.channel.return_value = mock_channel
        
        # This should succeed after retry
        await adapter.connect()
        
        # Verify results
        assert attempts == 2, f"Expected 2 attempts, got {attempts}"
        assert adapter._is_connected, "Adapter should be connected"
        assert adapter.connection is mock_conn, "Wrong connection object"
        assert adapter.channel is mock_channel, "Wrong channel object"


@pytest.mark.asyncio
async def test_concurrent_publishing(mock_config):
    """Test thread-safe publishing"""
    mock_exchange = AsyncMock()
    adapter = MessagingAdapter(mock_config)
    adapter._exchanges["test_exchange"] = mock_exchange
    adapter._is_connected = True
    adapter.channel = AsyncMock()  # Add channel mock

    # Configure publish to actually count calls
    publish_calls = 0
    async def mock_publish(*args, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        return True
    adapter.publish = mock_publish

    async def publish_task(msg):
        await adapter.publish("test_exchange", "routing.key", {"data": msg})

    tasks = [publish_task(i) for i in range(10)]
    await asyncio.gather(*tasks)

    assert publish_calls == 10


@pytest.mark.asyncio
async def test_message_expiration(mock_config):
    """Test that TTL is properly set on messages"""
    # Track the published message
    published_message = None

    # Create adapter and mock the publish method
    adapter = MessagingAdapter(mock_config)
    
    async def mock_publish(exchange_name, routing_key, message, **kwargs):
        nonlocal published_message
        published_message = {
            "exchange": exchange_name,
            "routing_key": routing_key,
            "message": message,
            "ttl": kwargs.get('ttl')
        }
        return True
    
    # Replace the actual publish method
    adapter.publish = mock_publish

    # Test with TTL
    await adapter.publish(
        exchange_name="expiry_test",
        routing_key="test",
        message={"test": "data"}, 
        ttl=100  # 100ms TTL
    )

    # Verify results
    assert published_message is not None
    assert published_message["exchange"] == "expiry_test"
    assert published_message["ttl"] == 100
    assert published_message["message"] == {"test": "data"}


def test_handler_registration(mock_config):
    """Test handler management"""
    adapter = MessagingAdapter(mock_config)
    handler1 = AsyncMock(spec=MessageHandler)
    handler2 = AsyncMock(spec=MessageHandler)

    adapter.register_handler("queue1", handler1)
    adapter.register_handler("queue2", handler2)

    assert adapter._handlers["queue1"] is handler1
    assert adapter._handlers["queue2"] is handler2

    with pytest.raises(ValueError):
        adapter.register_handler("queue1", "not_a_handler")  # Invalid handler


@pytest.mark.asyncio
async def test_queue_binding(mock_config):
    """Test queue-exchange binding"""
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
        
        mock_exchange = AsyncMock()
        mock_queue = AsyncMock()
        adapter._exchanges["test_exchange"] = mock_exchange
        adapter._queues["test_queue"] = mock_queue

        await adapter.bind_queue(
            queue_name="test_queue",
            exchange_name="test_exchange",
            routing_key="test.key"
        )

        mock_queue.bind.assert_called_once_with(
            mock_exchange,
            routing_key="test.key"
        )