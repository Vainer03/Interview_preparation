from celery import Celery

celery_app = Celery('tasks')

def make_celery(app):
    celery_app.conf.update(
        broker_url=app.config['CELERY_BROKER_URL'],
        result_backend=app.config['CELERY_RESULT_BACKEND'],
    )
    return celery_app