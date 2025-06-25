# RabbitMQ Consumer

## Описание

Этот проект реализует RabbitMQ Consumer, который обрабатывает сообщения из очереди, отправляет данные на указанный API и поддерживает механизм повторных попыток и Dead Letter Queue (DLQ) для обработки ошибок.

---

## Основные функции

- **Подключение к RabbitMQ** с поддержкой SSL.
- **Обработка сообщений** из очереди.
- **Отправка данных** на внешний API.
- **Механизм повторных попыток** (до 3 раз) при временных ошибках.
- **Dead Letter Queue (DLQ)** для обработки сообщений при превышении количества попыток или критических ошибках.

---

## Установка

### 1. Клонирование репозитория

```bash
git clone <repository_url>
cd to_userside
```

### 2. Настройка окружения

Создайте файл `.env` в корневой директории и добавьте переменные окружения:

```plaintext
AMQP_URL=amqp://<username>:<password>@<host>:<port>/<vhost>
QUEUE_NAME=<queue name>
API=http://<ip>:<port>
SOCKET_TIMEOUT=10
CONNECTION_ATTEMPTS=3
RETRY_DELAY=5
PREFETCH_COUNT=100
CA_CERT_PATH=/path/to/ca_cert.pem
CLIENT_CERT_PATH=/path/to/client_cert.pem
CLIENT_KEY_PATH=/path/to/client_key.pem
```

### 3. Установка зависимостей

Для запуска с использованием Docker:

```bash
docker compose up
```

---

## Использование

### Запуск локально

Для запуска локально выполните команду:

```bash
python main.py
```

---
