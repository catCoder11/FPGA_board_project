"""POST /jobs: отправка .sof на комп с платой.

Порядок важен: сначала проверки, потом приём файла.
  1. get_board_session  (Depends): id сессии подключения -> плата в БД
  2. get_target (Depends): плата есть в hosts.json, включена (is_enabled) и доступна (is_available)
  3. create_job: только после 1 и 2 читает тело запроса, проверяет файл и отправляет его на комп

Файл намеренно не объявлен как параметр File(...): иначе FastAPI разбирает всё тело
запроса (и пишет его на диск) ДО выполнения Depends. Без него Depends отрабатывают
первыми, а файл читается вручную уже в create_job.
"""

import asyncio
import json
import logging
import os
import uuid
from functools import lru_cache
from pathlib import Path

import httpx
from celery import Celery
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from app.db import connect
from app.routes.files import verify_session


router = APIRouter(prefix="/board")
session_router = APIRouter(prefix="/board")

HOSTS_FILE = Path(os.environ.get("HOSTS_FILE", "hosts.json"))
# Должен быть больше таймаута прошивки на компе (там 120 с), пока комп отвечает после её окончания
COMPUTER_TIMEOUT = float(os.environ.get("COMPUTER_TIMEOUT", "150"))


# ---------- hosts.json ----------

@lru_cache(maxsize=1)
def load_hosts() -> dict[str, dict]:
    """Читается один раз. Формат: список объектов
    {"board_code", "board_url", "board_token", "is_enabled"}.
    Файл с токенами не должен попадать в git."""
    items = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    return {item["board_code"]: item for item in items}


# ---------- Depends ----------

def get_board_session(b_session_id: str | None = Header(None, alias="B-Session-Id")) -> dict:
    """Id сессии подключения (не токен) -> плата из БД.

    Предполагаемая схема (подставьте свои имена):
      board_sessions(id, board_id, expires_at)
      boards(id, board_code, is_available)
    """
    if not b_session_id:
        raise HTTPException(401, "Нет id сессии подключения")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT b.board_id, b.board_code, b.is_available
            FROM board_sessions s
            JOIN boards b ON b.board_id = s.board_id
            WHERE s.board_session_id = %(session_id)s
              AND s.expires_at > now()
            """,
            {"session_id": b_session_id},
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Сессия подключения недействительна или истекла")
    return {"id": row[0], "board_code": row[1], "is_available": row[2], "board_session_id": b_session_id}


def get_target(board: dict = Depends(get_board_session)) -> dict:
    """Плата из БД -> запись в hosts.json -> url и токен для связи с компом."""
    host = load_hosts().get(board["board_code"])
    if host is None or not host.get("is_enabled", False):
        raise HTTPException(404, "Плата не найдена")
    if not board["is_available"]:
        raise HTTPException(503, "Плата сейчас недоступна")
    return {
        "board_id": board["id"],
        "board_code": board["board_code"],
        "url": host["board_url"].rstrip("/"),
        "token": host["board_token"],
    }


# ---------- Файл ----------

def validate_file(file: UploadFile) -> None:
    """Проверка полученного файла. ЗАГЛУШКА: пока ничего не проверяет.

    Сюда: расширение .sof, не пустой, размер (MAX_SIZE), и т.п.
    При проблеме бросать HTTPException(400/413, "...").
    """
    return None


# ---------- Отправка на комп ----------

async def send_to_board(target: dict, file: UploadFile) -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(COMPUTER_TIMEOUT, connect=5.0)) as client:
            resp = await client.post(
                f"{target['url']}/upload",
                headers={"Authorization": f"Bearer {target['token']}"},
                files={"file": (file.filename or "design.sof", file.file, "application/octet-stream")},
                data={"board_id": str(target["board_id"])},
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Комп не ответил вовремя")
    except httpx.RequestError:
        raise HTTPException(502, "Комп недоступен")

    if resp.status_code in (401, 403):
        # неверный токен или запрет на стороне компа: детали пользователю не нужны
        raise HTTPException(502, "Комп отклонил запрос сервера")
    try:
        content = resp.json()
    except ValueError:
        content = {"ok": False, "output": resp.text[:500]}
    return JSONResponse(status_code=resp.status_code, content=content)


# ---------- Эндпоинт ----------

@router.post(
    "/jobs",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def create_job(request: Request, session_hash: str = Depends(verify_session),
                     target: dict = Depends(get_target)):
    form = await request.form()
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(400, "Нужен файл в поле file")
        validate_file(file)
        await run_in_threadpool(change_job_status, target["board_session_id"], 0)
        return await send_to_board(target, file)
    except HTTPException as e:
        await run_in_threadpool(change_job_status, target["board_session_id"], 3, e.detail)
        raise
    finally:
        await form.close()   # удаляет временные файлы загрузки

def change_job_status(
    board_session_id: str,
    status_id: int,
    error_message: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, status_id, board_session_id, error_message)
               VALUES (%(job_id)s, %(status_id)s, %(board_session_id)s,
                       %(error_message)s)
               ON CONFLICT (board_session_id)
               DO UPDATE SET status_id = EXCLUDED.status_id,
                             error_message = EXCLUDED.error_message""",
            {
                "job_id": uuid.uuid4(),
                "status_id": status_id,
                "board_session_id": board_session_id,
                "error_message": error_message,
            },
        )


ABORT_TIMEOUT = float(os.environ.get("ABORT_TIMEOUT", "10"))   # abort должен отвечать быстро


@router.post("/abort")
async def abort_job(session_hash: str = Depends(verify_session),
                    target: dict = Depends(get_target)):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(ABORT_TIMEOUT, connect=5.0)) as client:
            resp = await client.post(
                f"{target['url']}/abort",
                headers={"Authorization": f"Bearer {target['token']}"},
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Комп не ответил вовремя")
    except httpx.RequestError:
        raise HTTPException(502, "Комп недоступен")

    if resp.status_code in (401, 403):
        # неверный токен или запрет на стороне компа: детали пользователю не нужны
        raise HTTPException(502, "Комп отклонил запрос сервера")
    try:
        content = resp.json()
    except ValueError:
        content = {"ok": False, "output": resp.text[:500]}
    return JSONResponse(status_code=resp.status_code, content=content)

@router.get("/jobs/status")
def get_job_status(session_hash: str = Depends(verify_session),
                   target: dict = Depends(get_target)):
    with connect() as conn:
        row = conn.execute(
            "SELECT j.job_id, j.status_id, s.name, j.error_message "
            "FROM jobs j JOIN job_statuses s ON s.id = j.status_id "
            "WHERE j.board_session_id = %s "
            "LIMIT 1",
            (target["board_session_id"],),
        ).fetchone()
    if row is None:
        return {"job_id": None, "status": None}   # в этой сессии ещё не было прошивки
    job_id, status_id, status, error_message = row
    return {
        "job_id": str(job_id),
        "status_id": status_id,
        "status": status,
        "error_message": error_message,
    }

SESSION_IDLE_TIMEOUT = 3600
SESSION_CLEANUP_INTERVAL = 30
BOARD_HEALTH_INTERVAL = int(os.environ.get("BOARD_HEALTH_INTERVAL", "30"))
BOARD_HEALTH_FAILURE_LIMIT = int(os.environ.get("BOARD_HEALTH_FAILURE_LIMIT", "3"))
BOARD_HEALTH_TIMEOUT = 5.0

logger = logging.getLogger(__name__)
celery_app = Celery("board_dialog", broker=os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0"))
celery_app.conf.update(
    task_ignore_result=True,
    beat_schedule={
        "check-boards": {
            "task": "board_dialog.check_boards",
            "schedule": BOARD_HEALTH_INTERVAL,
            "options": {"expires": BOARD_HEALTH_INTERVAL},
        },
        "expire-board-sessions": {
            "task": "board_dialog.expire_board_sessions",
            "schedule": SESSION_CLEANUP_INTERVAL,
            "options": {"expires": SESSION_CLEANUP_INTERVAL},
        },
    },
)


def delete_board_session(conn, session_id: int) -> None:
    """Удаляет сессию и связанные задания в транзакции вызывающей стороны."""
    conn.execute("DELETE FROM board_sessions WHERE board_session_id = %s", (session_id,))


def get_site_user_id(session_hash: str = Depends(verify_session)) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT u.user_id FROM user_sessions s
               JOIN users u ON u.user_id = s.user_id
               WHERE encode(s.token_hash, 'hex') = lower(%s)
                 AND s.expires_at > now() AND u.is_active""",
            (session_hash,),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Сессия пользователя недействительна")
    return row[0]


def refresh_board_session(
    user_id: int = Depends(get_site_user_id),
    session_id: int | None = Header(None, alias="B-Session-Id", ge=1, le=2_147_483_647),
) -> None:
    if session_id is None:
        raise HTTPException(401, "Нет id сессии подключения")
    with connect() as conn:
        row = conn.execute(
            """UPDATE board_sessions SET expires_at = now() + %s * interval '1 second'
               WHERE board_session_id = %s AND user_id = %s AND expires_at > now()
               RETURNING board_session_id""",
            (SESSION_IDLE_TIMEOUT, session_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Сессия подключения недействительна или истекла")


@session_router.post("/sessions/{board_code}", status_code=201)
def open_board_session(board_code: str, user_id: int = Depends(get_site_user_id)):
    with connect() as conn:
        board = conn.execute(
            """SELECT board_id, is_enabled, is_available FROM boards
               WHERE board_code = %s FOR UPDATE""",
            (board_code,),
        ).fetchone()
        if board is None or not board[1]:
            raise HTTPException(404, "Плата не найдена")
        if not board[2]:
            raise HTTPException(503, "Плата сейчас недоступна")
        board_id = board[0]
        expired = conn.execute(
            """SELECT board_session_id FROM board_sessions
               WHERE (board_id = %s OR user_id = %s) AND expires_at <= now()
               ORDER BY board_session_id FOR UPDATE""",
            (board_id, user_id),
        ).fetchall()
        for (session_id,) in expired:
            delete_board_session(conn, session_id)
        row = conn.execute(
            """INSERT INTO board_sessions (board_id, user_id, expires_at)
               VALUES (%s, %s, now() + %s * interval '1 second')
               ON CONFLICT DO NOTHING RETURNING board_session_id, expires_at""",
            (board_id, user_id, SESSION_IDLE_TIMEOUT),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "У платы или пользователя уже есть сессия")
    return {"board_session_id": row[0], "expires_at": row[1]}


@session_router.delete("/sessions/{board_code}", status_code=204)
def close_board_session(board_code: str, user_id: int = Depends(get_site_user_id)):
    with connect() as conn:
        row = conn.execute(
            """SELECT s.board_session_id FROM board_sessions s
               JOIN boards b USING (board_id)
               WHERE b.board_code = %s AND s.user_id = %s
               FOR UPDATE OF s""",
            (board_code, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Сессия платы не найдена")
        delete_board_session(conn, row[0])


@celery_app.task(name="board_dialog.expire_board_sessions")
def expire_board_sessions():
    with connect() as conn:
        sessions = conn.execute(
            """SELECT board_session_id FROM board_sessions
               WHERE expires_at <= now() FOR UPDATE SKIP LOCKED"""
        ).fetchall()
        for (session_id,) in sessions:
            delete_board_session(conn, session_id)


async def probe_board(client: httpx.AsyncClient, host: dict) -> tuple[str, bool]:
    board_code = host["board_code"]
    try:
        response = await client.get(
            f"{host['board_url'].rstrip('/')}/check",
            headers={"Authorization": f"Bearer {host['board_token']}"},
        )
        response.raise_for_status()
        result = response.json()
        return board_code, isinstance(result, dict) and result.get("ok") is True
    except (httpx.HTTPError, ValueError):
        return board_code, False


async def probe_boards(hosts: dict[str, dict]) -> list[tuple[str, bool]]:
    async with httpx.AsyncClient(timeout=BOARD_HEALTH_TIMEOUT) as client:
        return await asyncio.gather(*(probe_board(client, host) for host in hosts.values()))


@celery_app.task(name="board_dialog.check_boards")
def check_boards():
    try:
        hosts = load_hosts()
    except FileNotFoundError:
        logger.warning("Файл адресов плат отсутствует: %s", HOSTS_FILE)
        return
    for board_code, available in asyncio.run(probe_boards(hosts)):
        with connect() as conn:
            row = conn.execute(
                """UPDATE boards SET
                   failed_checks = CASE WHEN %s THEN 0 ELSE LEAST(failed_checks + 1, %s) END,
                   is_available = CASE
                       WHEN %s THEN true
                       WHEN failed_checks + 1 >= %s THEN false
                       ELSE is_available
                   END
                   WHERE board_code = %s
                   RETURNING board_id, failed_checks""",
                (available, BOARD_HEALTH_FAILURE_LIMIT, available, BOARD_HEALTH_FAILURE_LIMIT, board_code),
            ).fetchone()
            if row is not None and row[1] >= BOARD_HEALTH_FAILURE_LIMIT:
                session = conn.execute(
                    "SELECT board_session_id FROM board_sessions WHERE board_id = %s FOR UPDATE",
                    (row[0],),
                ).fetchone()
                if session is not None:
                    delete_board_session(conn, session[0])
