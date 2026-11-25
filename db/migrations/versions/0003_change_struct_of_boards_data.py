"""change struct of boards_data

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24 04:15:44.178925
"""
from alembic import op


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.boards DROP COLUMN IF EXISTS station_id")
    op.execute("DROP TABLE IF EXISTS public.computer_stations")


def downgrade() -> None:
    raise RuntimeError("Откат 0003 требует резервной копии данных станций")
