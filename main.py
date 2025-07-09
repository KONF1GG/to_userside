"""RabbitMQ Consumer"""

import logging
from logging.handlers import RotatingFileHandler
import ssl
import signal
import time
import json
import pika
import requests

from pika.exceptions import AMQPError

import config


# ==================== #
#   Custom Exceptions  #
# ==================== #
class TransientError(Exception):
    """Временная ошибка, можно повторить попытку"""


class CriticalError(Exception):
    """Фатальная ошибка, требуется вмешательство"""


# ==================== #
#   Consumer Class     #
# ==================== #
class RabbitConsumer:
    """RabbitMQ Consumer"""

    def __init__(self, amqp_url, queue_name):
        self._connection = None
        self._channel = None
        self._url = amqp_url
        self._queue = queue_name
        self._reconnect_delay = 1
        self._max_reconnect_delay = 300
        self._consumer_tag = None
        self._running = False

        self._ssl_enabled = False
        if config.CA_CERT_PATH:
            self._ssl_context = ssl.create_default_context(cafile=config.CA_CERT_PATH)
            self._ssl_context.load_cert_chain(
                certfile=config.CLIENT_CERT_PATH if config.CLIENT_CERT_PATH else "",
                keyfile=config.CLIENT_KEY_PATH,
            )
            self._ssl_enabled = True

    def _connect(self):
        """Установка соединения"""
        logging.info("Connecting to RabbitMQ")
        params = pika.URLParameters(self._url)

        if self._ssl_enabled:
            params.ssl_options = pika.SSLOptions(self._ssl_context)

        params.socket_timeout = int(config.SOCKET_TIMEOUT)
        params.connection_attempts = int(config.CONNECTION_ATTEMPTS)
        params.retry_delay = int(config.RETRY_DELAY)

        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        if channel is None:
            raise RuntimeError("Failed to create channel.")

        return connection, channel

    def _reconnect(self):
        """Механизм переподключения"""
        while self._running:
            try:
                self._connection, self._channel = self._connect()
                if self._channel is None:
                    raise RuntimeError("Channel not initialized.")
                self._setup_infrastructure()
                self._start_consuming()
                self._reconnect_delay = 1
                return
            except Exception as e:
                logging.error(
                    "Reconnect failed: %s. Retry in %s seconds", e, self._reconnect_delay
                )
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    def _setup_infrastructure(self):
        """Настройка очередей"""
        if self._channel is None:
            raise RuntimeError("Channel is None in _setup_infrastructure")

        # Dead Letter Exchange
        self._channel.exchange_declare(
            exchange="dlx_to_userside", exchange_type="direct", durable=True
        )
        self._channel.queue_declare(
            queue="dlq_to_userside", durable=True, arguments={"x-queue-mode": "lazy"}
        )
        self._channel.queue_bind("dlq_to_userside", "dlx_to_userside", routing_key="dlq_to_userside")

        # Основная очередь to_userside
        self._channel.queue_declare(
            queue="to_userside",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "dlx_to_userside",
                "x-dead-letter-routing-key": "dlq_to_userside",
                "x-queue-type": "classic",
            },
        )

        self._channel.queue_bind(
            queue="to_userside",
            exchange="to_redis", 
            routing_key="to_redis",
        )

        self._channel.queue_bind(
            queue="to_userside",
            exchange="to_redis", 
            routing_key="to_redis_vector",
        )

        self._channel.basic_qos(prefetch_count=int(config.PREFETCH_COUNT))

    def _start_consuming(self):
        """Запуск потребления сообщений"""
        if self._channel is None:
            raise RuntimeError("Channel is None in _start_consuming")
        self._consumer_tag = self._channel.basic_consume(
            queue=self._queue, on_message_callback=self._on_message, auto_ack=False
        )

    def _republish_with_retry(self, channel, body, headers, attempt_count):
        """Переотправка сообщения с увеличенным счетчиком попыток"""
        new_headers = dict(headers)
        new_headers["x-attempt-count"] = attempt_count + 1

        properties = pika.BasicProperties(
            headers=new_headers,
            content_type='application/json',
            delivery_mode=2,  
        )

        channel.basic_publish(
            exchange="",
            routing_key=self._queue,
            body=body,
            properties=properties
        )

        logging.info("Message requeued with attempt %d", new_headers["x-attempt-count"])


    def _on_message(self, channel, method, properties, body):
        """Обработка сообщения"""
        try:
            message = json.loads(body)
            key = message.get("key", "")

            # Получаем число попыток
            headers = properties.headers or {}
            attempt_count = headers.get("x-attempt-count", 0)

            if key.startswith("login:"):
                try:
                    self._handle_login(message)
                    channel.basic_ack(method.delivery_tag)
                except TransientError as e:
                    logging.warning("Transient error: %s", e)
                    if attempt_count >= 2:
                        logging.error("Max retry attempts reached. Sending to DLQ.")
                        self._send_to_dlq(body, str(e))
                        channel.basic_nack(method.delivery_tag, requeue=False)
                    else:
                        self._republish_with_retry(channel, body, headers, attempt_count)
                        channel.basic_ack(method.delivery_tag)
            else:
                raise CriticalError("Неправильный ключ")
        except CriticalError as e:
            logging.error("Critical error: %s", e)
            channel.basic_nack(method.delivery_tag, requeue=False)
            self._send_to_dlq(body, str(e))
        except (AMQPError, RuntimeError, Exception) as e:
            logging.exception("Unexpected error while processing message")
            channel.basic_nack(method.delivery_tag, requeue=False)
            self._send_to_dlq(body, str(e))


    def _handle_login(self, message):
        login = message.get("key")[6:]
        value = message.get("value")
        house_id = str(value.get("houseId"))
        address = value.get("address")

        data = {
            "login": login,
            "address": address,
            "houseId": int(house_id),
        }

        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(
                f"{config.API}/login",
                json=data,
                headers=headers,
            )
            
            if response.status_code == 200:
                return
                
            elif response.status_code == 404:
                create_if_not = message.get("createIfNot", False)
                if create_if_not:
                    logging.warning("Resource not found and createIfNot is True. Will retry.")
                    raise TransientError(f"Resource not found, but createIfNot is True: {response.text}")
                else:
                    logging.info("Resource not found and createIfNot is False. Acknowledging message.")
                    return
                    
            else:
                raise TransientError(
                    f"Ошибка отправки данных: {response.status_code} - {response.text}"
                )
                
        except requests.exceptions.RequestException as e:
            raise TransientError(f"Request exception: {e}") from e

    def _send_to_dlq(self, body, error):
        """Заглушка для отправки в DLQ """
        logging.warning("Message sent to DLQ. Reason: %s | Body: %s", error, body)

    def start(self):
        self._running = True
        logging.info("Starting consumer")
        self._reconnect()
        try:
            if self._channel:
                self._channel.start_consuming()
        except (AMQPError, RuntimeError) as e:
            logging.error("Consuming error: %s", e)
            if self._running:
                self._reconnect()

    def stop(self):
        self._running = False
        if self._channel:
            self._channel.stop_consuming()
        if self._connection:
            self._connection.close()


# ==================== #
#   Main Execution     #
# ==================== #
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            RotatingFileHandler(config.LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5),
            logging.StreamHandler(),
        ],
    )

    consumer = RabbitConsumer(amqp_url=config.AMQP_URL, queue_name=config.QUEUE_NAME)

    signal.signal(signal.SIGINT, lambda s, f: consumer.stop())
    signal.signal(signal.SIGTERM, lambda s, f: consumer.stop())

    while True:
        try:
            consumer.start()
        except KeyboardInterrupt:
            break
        except (AMQPError, RuntimeError) as e:
            logging.error("Consumer crash: %s, restarting in 10s", e)
            time.sleep(10)
