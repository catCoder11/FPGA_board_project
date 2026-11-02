# Версия Python, адрес и порт uvicorn задаются в .env и приходят из docker-compose
ARG PYTHON_VERSION

# Общая часть для dev и prod
FROM python:${PYTHON_VERSION}-slim AS base

# Не создавать .pyc и сразу выводить логи в терминал контейнера
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

# Зависимости копируем отдельно: пока requirements.txt не меняется,
# Docker берёт этот шаг из кэша и не скачивает библиотеки заново
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Приложение принимает файлы от пользователей, поэтому запускаем его не от root
RUN useradd --create-home appuser


# Разработка: есть тестовые библиотеки и папка tests
FROM base AS dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

USER appuser

CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$APP_HOST\" --port \"$APP_PORT\""]


# Сайт: только код приложения, без тестов и тестовых библиотек
FROM base AS prod

COPY app ./app

USER appuser

# Запросы приходят только через nginx. Он передаёт настоящий IP пользователя и протокол
# в заголовках, а --forwarded-allow-ips разрешает им доверять
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$APP_HOST\" --port \"$APP_PORT\" --proxy-headers --forwarded-allow-ips='*'"]
