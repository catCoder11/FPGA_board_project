import os

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def connect() -> psycopg.Connection:
    """Новое подключение к БД.

    Использовать так: `with connect() as conn: ...`
    При выходе из блока без ошибок изменения сохраняются (commit),
    при ошибке откатываются (rollback), и соединение закрывается.
    """
    return psycopg.connect(DATABASE_URL, connect_timeout=5)