# Миграции БД (Alembic)

Всё, что относится к схеме базы, лежит в этой папке. С остальным проектом её связывают
две вещи в корневом `docker-compose.yaml`: `include: db/compose.yaml` и
`depends_on: migrate` у backend.

## Как это работает

`docker compose up` запускает сервис `migrate`. Он применяет недостающие миграции
(`alembic upgrade head`) и завершается, а backend стартует только после его успешного
завершения. Какие миграции уже выполнены, записано в таблице `alembic_version`.

## Новая миграция

1. Создать файл со следующим номером:
   ```bash
   docker compose run --rm migrate alembic revision -m "add jobs created_at" --rev-id 0002
   ```
2. Вписать изменения в `upgrade()` и обратные им в `downgrade()`, например
   `op.execute("ALTER TABLE jobs ADD COLUMN created_at timestamptz")`.
3. Применить: `docker compose up -d` или `docker compose run --rm migrate`.

Схему больше не меняем руками в DBeaver/psql, иначе база разойдётся с миграциями.

## Полезные команды

```bash
docker compose run --rm migrate alembic current      # текущая версия базы
docker compose run --rm migrate alembic history      # список миграций
docker compose run --rm migrate alembic downgrade -1 # откатить последнюю
docker compose run --rm migrate alembic upgrade head --sql  # показать SQL, не выполняя
```

## База, созданная до миграций

`0001_init` повторяет схему, которую делали вручную. Если таблицы уже есть,
миграцию не выполняют, а только помечают как выполненную:

```bash
docker compose run --rm migrate alembic stamp 0001
```
