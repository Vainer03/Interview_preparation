# tasks.py
from celery import Celery

# Создаём приложение Celery
app = Celery('tasks', broker='redis://localhost:6379/0')

# Простая задача
@app.task
def add(x, y):
    return x + y