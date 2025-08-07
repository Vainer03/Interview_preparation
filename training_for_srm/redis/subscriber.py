import redis

r = redis.Redis()

pubsub = r.pubsub()
pubsub.subscribe("chat")

print("Waiting for messages...")
for message in pubsub.listen():
    print(message)