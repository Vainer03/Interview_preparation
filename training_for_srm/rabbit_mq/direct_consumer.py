# direct_consumer.py

import pika

def callback(ch, method, properties, body):
    print(f" [x] Received {body}")

connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()

channel.exchange_declare(exchange='logs_direct', exchange_type='direct')
queue = channel.queue_declare('', exclusive=True).method.queue
channel.queue_bind(exchange='logs_direct', queue=queue, routing_key='info')

print(' [*] Waiting for messages. To exit press CTRL+C')
channel.start_consuming()

