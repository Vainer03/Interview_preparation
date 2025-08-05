# direct_producer.py
# producer.py
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()

channel.queue_declare(queue='hello')
channel.exchange_declare(exchange='logs_direct', exchange_type='direct')
channel.basic_publish(exchange='logs_direct', routing_key='info', body='Info log!')

print(" [x] Sent 'Hello RabbitMQ!'")
connection.close()



