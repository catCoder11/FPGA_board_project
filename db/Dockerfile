ARG PYTHON_VERSION

FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /migrations

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations

RUN useradd --create-home migrator
USER migrator

CMD ["alembic", "upgrade", "head"]
