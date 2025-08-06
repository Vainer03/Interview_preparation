# celery_worker.py
from celery import Celery

celery = Celery(
    'my_app',
    broker='redis://localhost:6379/0',  # For Windows host
    backend='redis://localhost:6379/0',
    broker_connection_retry_on_startup=True
)

@celery.task
def add(x, y):
    return x + y