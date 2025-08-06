from app.celery_utils import celery

if __name__ == '__main__':
    celery.worker_main()