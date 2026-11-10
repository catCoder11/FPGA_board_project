"""Начальная схема: повторяет базу, созданную вручную до появления миграций

Revision ID: 0001
Revises:
Create Date: 2026-11-10
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            user_id       integer GENERATED ALWAYS AS IDENTITY,
            login         varchar NOT NULL,
            password_hash bytea NOT NULL,
            is_active     boolean DEFAULT true NOT NULL,
            created_at    timestamptz DEFAULT now() NOT NULL,
            CONSTRAINT users_pk PRIMARY KEY (user_id),
            CONSTRAINT users_unique_login UNIQUE (login)
        )
    """)

    op.execute("""
        CREATE TABLE user_sessions (
            session_id integer GENERATED ALWAYS AS IDENTITY,
            user_id    integer NOT NULL,
            token_hash bytea NOT NULL,
            created_at timestamptz DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CONSTRAINT user_sessions_pk PRIMARY KEY (session_id),
            CONSTRAINT user_sessions_unique_token UNIQUE (token_hash),
            CONSTRAINT user_sessions_user_fk FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    op.execute("""
        CREATE TABLE computer_stations (
            station_id     integer GENERATED ALWAYS AS IDENTITY,
            station_code   varchar NOT NULL,
            created_at     timestamptz DEFAULT now() NOT NULL,
            is_enabled     boolean DEFAULT false NOT NULL,
            service_ip     inet NOT NULL,
            service_port   integer NOT NULL,
            last_connected timestamptz,
            CONSTRAINT computer_stations_pk PRIMARY KEY (station_id),
            CONSTRAINT computer_stations_unique_code UNIQUE (station_code),
            CONSTRAINT computer_stations_unique_addr UNIQUE (service_ip, service_port),
            CONSTRAINT computer_stations_port_check CHECK (service_port BETWEEN 1 AND 65535)
        )
    """)

    op.execute("""
        CREATE TABLE boards (
            board_id       integer GENERATED ALWAYS AS IDENTITY,
            station_id     integer NOT NULL,
            board_code     varchar NOT NULL,
            is_enabled     boolean DEFAULT false NOT NULL,
            created_at     timestamptz DEFAULT now() NOT NULL,
            last_connected timestamptz,
            CONSTRAINT boards_pk PRIMARY KEY (board_id),
            CONSTRAINT boards_unique_code UNIQUE (board_code),
            CONSTRAINT boards_station_fk FOREIGN KEY (station_id) REFERENCES computer_stations (station_id)
        )
    """)

    op.execute("""
        CREATE TABLE board_sessions (
            board_session_id integer GENERATED ALWAYS AS IDENTITY,
            board_id   integer NOT NULL,
            user_id    integer NOT NULL,
            created_at timestamptz DEFAULT now() NOT NULL,
            expires_at timestamptz NOT NULL,
            CONSTRAINT board_sessions_pk PRIMARY KEY (board_session_id),
            CONSTRAINT board_sessions_unique UNIQUE (board_id),
            CONSTRAINT board_sessions_unique_user UNIQUE (user_id),
            CONSTRAINT board_sessions_board_fk FOREIGN KEY (board_id) REFERENCES boards (board_id),
            CONSTRAINT board_sessions_user_fk FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)

    op.execute("""
        CREATE TABLE statuses (
            status_id   integer NOT NULL,
            status_name varchar NOT NULL,
            CONSTRAINT statuses_pk PRIMARY KEY (status_id),
            CONSTRAINT statuses_unique UNIQUE (status_name)
        )
    """)
    op.execute("""
        INSERT INTO statuses (status_id, status_name) VALUES
            (0, 'queued'), (1, 'processing'), (2, 'done'), (3, 'failed')
    """)

    op.execute("""
        CREATE TABLE jobs (
            job_id           uuid NOT NULL,
            payload_json     jsonb,
            error_message    varchar,
            status_id        integer,
            board_session_id integer,
            CONSTRAINT jobs_pk PRIMARY KEY (job_id),
            CONSTRAINT jobs_status_fk FOREIGN KEY (status_id) REFERENCES statuses (status_id)
        )
    """)


def downgrade() -> None:
    for table in ("jobs", "statuses", "board_sessions", "boards",
                  "computer_stations", "user_sessions", "users"):
        op.execute(f"DROP TABLE {table}")
