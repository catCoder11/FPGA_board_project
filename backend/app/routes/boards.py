import logging
import os
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from psycopg import errors
from pydantic import BaseModel

from app.db import connect
from app.deps import ActiveBoardSession, CurrentUser, get_board_session, get_current_user
from app.routes.board_dialog import load_hosts

router = APIRouter(prefix="/boards", tags=["boards"])
log = logging.getLogger(__name__)

# На сколько минут плата закрепляется за пользователем после подключения
BOARD_SESSION_MINUTES = int(os.environ.get("BOARD_SESSION_MINUTES", "60"))

# Имена ограничений уникальности из миграции 0001: по ним видно, что именно нарушено
USER_ALREADY_CONNECTED = "board_sessions_unique_user"  # у пользователя уже есть плата

# Больше этого число не поместится в integer в PostgreSQL и вызвало бы ошибку БД
MAX_DB_INT = 2_147_483_647
BoardId = Annotated[int, Path(ge=1, le=MAX_DB_INT, description="ID платы")]


class BoardOut(BaseModel):
    board_id: int
    board_code: str
    enabled: bool
    is_available: bool
    status: Literal["free", "busy", "unavailable"]
    busy_by_me: bool
    seconds_left: int | None = None  # у занятой платы: через сколько секунд освободится


class BoardSessionOut(BaseModel):
    board_id: int
    board_code: str
    expires_at: datetime
    seconds_left: int


def _db_unavailable(where: str) -> HTTPException:
    log.exception("Ошибка БД: %s", where)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")


def configured_boards() -> dict[str, dict]:
    try:
        return load_hosts()
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("Ошибка чтения конфигурации плат")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Конфигурация плат недоступна")


@router.get("", response_model=list[BoardOut])
def list_boards(
    offset: Annotated[int, Query(ge=0, le=MAX_DB_INT)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    user: CurrentUser = Depends(get_current_user),
) -> list[BoardOut]:
    """Все платы и их состояние: свободна, занята или недоступна."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT b.board_id, b.board_code, b.is_available, (b.is_enabled AND s.is_enabled), "
                "bs.user_id, GREATEST(0, EXTRACT(EPOCH FROM (bs.expires_at - now()))::int) "
                "FROM boards b JOIN computer_stations s ON s.station_id = b.station_id "
                # просроченная сессия платой уже не владеет, даже если строка ещё лежит в таблице
                "LEFT JOIN board_sessions bs ON bs.board_id = b.board_id AND bs.expires_at > now() "
                "ORDER BY b.board_id OFFSET %s LIMIT %s",
                (offset, limit),
            ).fetchall()
    except psycopg.Error:
        raise _db_unavailable("список плат")

    hosts = configured_boards()
    result = []
    for board_id, board_code, available, can_connect, holder_id, seconds_left in rows:
        enabled = hosts.get(board_code, {}).get("is_enabled") is True
        if not (enabled and available and can_connect):
            state = "unavailable"
        elif holder_id is None:
            state = "free"
        else:
            state = "busy"
        result.append(
            BoardOut(
                board_id=board_id,
                board_code=board_code,
                enabled=enabled,
                is_available=available,
                status=state,
                # Чужие id наружу не отдаём: только «занята мной» или нет
                busy_by_me=state == "busy" and holder_id == user.user_id,
                seconds_left=seconds_left if state == "busy" else None,
            )
        )
    return result


@router.get("/current", response_model=BoardSessionOut)
def current_board(session: ActiveBoardSession = Depends(get_board_session)) -> BoardSessionOut:
    """К какой плате я подключён и сколько осталось. Нужно фронтенду после обновления страницы."""
    return BoardSessionOut(
        board_id=session.board_id,
        board_code=session.board_code,
        expires_at=session.expires_at,
        seconds_left=session.seconds_left,
    )


@router.post("/{board_id}/connect", response_model=BoardSessionOut)
def connect_board(
    board_id: BoardId, user: CurrentUser = Depends(get_current_user)
) -> BoardSessionOut:
    """Занять плату на BOARD_SESSION_MINUTES минут. Повторный вызов вернёт текущую сессию."""
    try:
        with connect() as conn:  # всё внутри — одна транзакция
            board = conn.execute(
                "SELECT b.board_code, (b.is_enabled AND s.is_enabled AND b.is_available) "
                "FROM boards b JOIN computer_stations s ON s.station_id = b.station_id "
                "WHERE b.board_id = %s FOR UPDATE OF b",
                (board_id,),
            ).fetchone()
            if board is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Плата не найдена")
            board_code, available = board
            if not available or configured_boards().get(board_code, {}).get("is_enabled") is not True:
                raise HTTPException(status.HTTP_409_CONFLICT, "Плата сейчас недоступна")

            # Уже подключён к этой плате (двойной клик, обновление страницы) — отдаём ту же сессию
            existing = conn.execute(
                "SELECT expires_at, EXTRACT(EPOCH FROM (expires_at - now()))::int "
                "FROM board_sessions WHERE board_id = %s AND user_id = %s AND expires_at > now()",
                (board_id, user.user_id),
            ).fetchone()

            if existing is not None:
                expires_at, seconds_left = existing
            else:
                # Просроченные строки мешают уникальным ограничениям (одна сессия на плату и
                # одна на пользователя), поэтому сначала убираем их
                conn.execute(
                    "DELETE FROM board_sessions "
                    "WHERE (board_id = %s OR user_id = %s) AND expires_at <= now()",
                    (board_id, user.user_id),
                )
                # Кто успел первым, тот и занял: второго остановит ограничение в самой БД
                expires_at, seconds_left = conn.execute(
                    "INSERT INTO board_sessions (board_id, user_id, expires_at) "
                    "VALUES (%s, %s, now() + make_interval(mins => %s::int)) "
                    "RETURNING expires_at, EXTRACT(EPOCH FROM (expires_at - now()))::int",
                    (board_id, user.user_id, BOARD_SESSION_MINUTES),
                ).fetchone()
    except errors.UniqueViolation as exc:
        if exc.diag.constraint_name == USER_ALREADY_CONNECTED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Вы уже подключены к другой плате. Сначала отключитесь от неё",
            )
        raise HTTPException(status.HTTP_409_CONFLICT, "Плата уже занята")
    except psycopg.Error:
        raise _db_unavailable("подключение к плате")

    return BoardSessionOut(
        board_id=board_id, board_code=board_code, expires_at=expires_at, seconds_left=seconds_left
    )


@router.post("/{board_id}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_board(board_id: BoardId, user: CurrentUser = Depends(get_current_user)) -> None:
    """Кнопка «выход»: освободить плату. Удаляется только своя сессия, поэтому чужую не тронуть.

    Повторный вызов тоже даёт 204: результат («вы не подключены») уже достигнут.
    """
    try:
        with connect() as conn:
            conn.execute(
                "DELETE FROM board_sessions WHERE board_id = %s AND user_id = %s",
                (board_id, user.user_id),
            )
    except psycopg.Error:
        raise _db_unavailable("отключение от платы")
