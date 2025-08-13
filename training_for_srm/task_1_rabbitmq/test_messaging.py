import sys
import pytest
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any, Optional
from pydantic import ValidationError

from src.core.messaging import (
    MessagingAdapter,
    RabbitMQConfig,
    NotificationMessage,
    NotificationType,
    MessagePriorityLevel,
    MessageHandler,
    AMQPError
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
    with patch('aio_pika.connect_robust', side_effect=AMQPError("Connection failed")) as mock_connect:
        adapter = MessagingAdapter(mock_config)
        
        with pytest.raises(ConnectionError, match="Failed to connect to RabbitMQ"):
            await adapter.connect()
        
        assert adapter._is_connected is False
        mock_connect.assert_called_once_with(
            adapter._get_connection_url(),
            timeout=adapter.config.connection_timeout
        )

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
        
        mock_exchange = AsyncMock()
        adapter._exchanges["notifications_exchange"] = mock_exchange
        
        with patch.object(adapter, 'publish', return_value=True) as mock_publish:
            result = await adapter.publish_notification(sample_notification)
            
            mock_publish.assert_called_once_with(
                exchange_name="notifications_exchange",
                routing_key="notification.email.high",
                message=sample_notification.model_dump(),
                priority=3
            )
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
    test_message = {
        "title": "Test",
        "message": "Hello",
        "recipient_id": "user1"
    }
    
    class TestHandler(MessageHandler):
        def __init__(self):
            self.processed = False
            self.received_message = None
            
        async def handle(self, message: Dict[str, Any]) -> None:
            self.processed = True
            self.received_message = message

    # Create test handler instance
    handler = TestHandler()

    # Setup the complete mock chain
    mock_connection = AsyncMock()
    mock_channel = AsyncMock()
    mock_queue = AsyncMock()
    mock_message = AsyncMock()
    
    # Configure the mock message
    mock_message.body = json.dumps(test_message).encode()
    mock_message.priority = 2
    
    # Create a proper async context manager
    class MockProcessContext:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *args):
            return None
    
    mock_message.process.return_value = MockProcessContext()
    
    # Configure the mock queue and channel
    mock_channel.declare_queue.return_value = mock_queue
    mock_connection.channel.return_value = mock_channel

    with patch('aio_pika.connect_robust', return_value=mock_connection):
        adapter = MessagingAdapter(mock_config)
        
        # Directly set up the connection state we need
        adapter.connection = mock_connection
        adapter.channel = mock_channel
        adapter._is_connected = True
        
        # Register our handler
        adapter.register_handler("test_queue", handler)
        
        # Manually add the queue to bypass declare_queue
        adapter._queues["test_queue"] = mock_queue
        
        # Start message consumption
        await adapter.start_consuming("test_queue")
        
        # Verify the consumer was set up
        mock_queue.consume.assert_called_once()
        
        # Get the actual callback function that was registered
        args, _ = mock_queue.consume.call_args
        process_message_callback = args[0]
        
        # Execute the callback with our test message
        await process_message_callback(mock_message)
        
        # Verify the results
        assert handler.processed is True, (
            f"Handler was not called. Queue state: {adapter._queues}\n"
            f"Handler state: processed={handler.processed}, message={handler.received_message}"
        )
        assert handler.received_message["title"] == "Test"
        assert handler.received_message.get("priority") == 2

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
    # Simulate aio_pika not being installed
    monkeypatch.setattr(sys.modules['src.core.messaging'], 'aio_pika', None)
    
    adapter = MessagingAdapter(mock_config)
    
    # Match either version of the error message
    with pytest.raises(ConnectionError) as exc_info:
        await adapter.connect()
    
    # Check that either error message variant is present
    assert "aio_pika is required" in str(exc_info.value)
    assert not adapter._is_connected

# Тесты для connection context manager

@pytest.mark.asyncio
async def test_connection_context(mock_config):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        
        async with adapter.connection_context() as conn:
            assert conn._is_connected is True
            assert conn is adapter
        
        assert adapter._is_connected is False