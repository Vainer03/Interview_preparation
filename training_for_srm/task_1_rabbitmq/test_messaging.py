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
    with patch('aio_pika.connect_robust', new_callable=AsyncMock) as mock_connect:
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
       
        assert adapter._is_connected is True
        mock_connect.assert_called_once()


@pytest.mark.asyncio
async def test_connect_failure(mock_config):
    with patch('aio_pika.connect_robust', side_effect=AMQPError("Connection failed")):
        adapter = MessagingAdapter(mock_config)
       
        with pytest.raises(ConnectionError):
            await adapter.connect()
       
        assert adapter._is_connected is False


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
        processed = False
       
        async def handle(self, message: Dict[str, Any]) -> None:
            self.processed = True
            assert message["title"] == "Test"
   
    handler = TestHandler()
   
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
        await adapter.connect()
       
        adapter.register_handler("test_queue", handler)
       
        # Эмулируем получение сообщения
        mock_message = AsyncMock()
        mock_message.body = json.dumps(test_message).encode()
        mock_message.priority = 2
       
        # Вызываем обработчик напрямую для тестирования
        await adapter._consumers[0].callback(mock_message)
       
        assert handler.processed is True


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
    monkeypatch.setitem(sys.modules, 'aio_pika', None)
   
    adapter = MessagingAdapter(mock_config)
    await adapter.connect()
   
    assert adapter._is_connected is False
   
    with pytest.raises(RuntimeError):
        await adapter.declare_queue("test_queue")


# Тесты для connection context manager


@pytest.mark.asyncio
async def test_connection_context(mock_config):
    with patch('aio_pika.connect_robust', new_callable=AsyncMock):
        adapter = MessagingAdapter(mock_config)
       
        async with adapter.connection_context() as conn:
            assert conn._is_connected is True
            assert conn is adapter
       
        assert adapter._is_connected is False
