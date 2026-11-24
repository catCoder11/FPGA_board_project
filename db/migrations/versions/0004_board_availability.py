"""Хранение доступности стендов и удаление заданий вместе с сессией."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE boards ADD COLUMN is_available boolean NOT NULL DEFAULT false,
                           ADD COLUMN failed_checks integer NOT NULL DEFAULT 0;
        ALTER TABLE jobs ADD CONSTRAINT jobs_board_session_fk
            FOREIGN KEY (board_session_id) REFERENCES board_sessions (board_session_id)
            ON DELETE CASCADE NOT VALID;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE jobs DROP CONSTRAINT jobs_board_session_fk;
        ALTER TABLE boards DROP COLUMN failed_checks, DROP COLUMN is_available;
    """)
