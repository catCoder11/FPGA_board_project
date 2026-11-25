"""Удаление устаревших полей плат после перехода на hosts.json."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Базы с прежним upgrade() миграции 0003 могли сохранить станцию и её внешний ключ.
    op.execute("""
        ALTER TABLE public.boards
            DROP COLUMN IF EXISTS station_id,
            DROP COLUMN IF EXISTS is_enabled;
        DROP TABLE IF EXISTS public.computer_stations;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE public.boards ADD COLUMN is_enabled boolean NOT NULL DEFAULT false")
