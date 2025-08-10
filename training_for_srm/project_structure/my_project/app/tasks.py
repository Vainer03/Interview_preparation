from celery import Celery
from .database import SessionLocal
from .models import User

celery_app = Celery(
    "worker",
    broker="amqp://guest:guest@rabbitmq:5672//",
    backend="redis://redis:6379/0"
)

@celery_app.task
def create_user_task(name: str):
    db = SessionLocal()
    user = User(name=name)
    db.add(user)
    db.commit()
    db.close()
    return f"User '{name}' created"
