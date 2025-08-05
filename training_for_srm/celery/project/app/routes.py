from flask import Blueprint, request, jsonify
from .tasks import celery_app

bp = Blueprint('main', __name__)

@celery_app.task
def long_task(name):
    return f"Привет, {name}! Задача выполнена."

@bp.route('/run-task', methods=['POST'])
def run_task():
    data = request.get_json()
    task = long_task.delay(data['name'])
    return jsonify({"task_id": task.id}), 202