FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Зависимостей нет: main.py использует только стандартную библиотеку.
COPY main.py .

# Quartus в образ не кладём: каталог с ним монтируется с хоста (см. docker-compose.yml).
# Работаем от root: так проще получить доступ к USB-устройству программатора.
CMD ["python", "-u", "main.py"]