from pydantic import BaseModel, validator
from pydantic_settings import BaseSettings


class LoggingSettings(BaseModel):
    """Logging configuration settings."""
    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class Settings(BaseSettings):
    """Application settings."""
    # Database settings
    database_url: str | None = None
    jwt_secret: str | None = None

    # RabbitMQ settings - with environment variable support
    rabbitmq_url: str  # Required parameter from environment variables
    rabbitmq_notifications_queue: str = "notifications"
    rabbitmq_notifications_exchange: str = "notifications"

    # RabbitMQ connection timeouts - with environment variable support
    rabbitmq_connection_timeout: int = 60  # seconds
    rabbitmq_heartbeat_interval: int = 600  # seconds
    rabbitmq_blocked_connection_timeout: int = 30  # seconds
    rabbitmq_channel_timeout: int = 300  # seconds - increased for quorum operations

    # Quorum queue settings - with environment variable support
    use_quorum_queues: bool = True  # ENABLE quorum queues
    rabbitmq_quorum_initial_group_size: int = 1  # For single node 1 is enough
    rabbitmq_quorum_members: int = 1  # For single node 1 is enough
    rabbitmq_message_ttl: int = 86400000  # 24 hours in milliseconds
    rabbitmq_retry_ttl: int = 60000  # 1 minute in milliseconds
    rabbitmq_dlq_ttl: int = 604800000  # 7 days in milliseconds

    # Logging settings
    logging: LoggingSettings = LoggingSettings()
    logging_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    @validator("rabbitmq_url")
    def validate_rabbitmq_url(cls, v: str) -> str:
        """Validate RabbitMQ URL."""
        if not v.startswith("amqp://"):
            raise ValueError("RabbitMQ URL must start with amqp://")
        return v

    class Config:
        """Pydantic configuration."""
        case_sensitive = False
        env_file = ".env"  # Read from .env file
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
