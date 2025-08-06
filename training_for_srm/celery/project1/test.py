from celery_worker import add
import time

try:
    result = add.delay(4, 6)
    print("Task ID:", result.id)
    
    # Wait for task to complete
    while not result.ready():
        print("Waiting...")
        time.sleep(1)
    
    print("Result:", result.get(timeout=10))
except Exception as e:
    print(f"Error: {e}")