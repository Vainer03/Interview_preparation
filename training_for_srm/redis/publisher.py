import redis

r = redis.Redis()
r.publish("chat", "Hello from user A")