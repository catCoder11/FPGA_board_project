"""add board_id unique

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24 03:36:11.146418
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE jobs ADD CONSTRAINT jobs_board_session_id_key UNIQUE (board_session_id);')


def downgrade() -> None:
    pass
