import os
from files import verify_session
import uuid
import psycopg
from fastapi import APIRouter
from fastapi import FastAPI, File, Form, Header, Path, UploadFile, HTTPException, status, Request, Depends
from fastapi.concurrency import run_in_threadpool
import json
import os
from functools import lru_cache
from pathlib import Path
import httpx
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile   # именно starlette: form() отдаёт его, а не fastapi.UploadFile
from db import connect
"""POST /jobs: отправка .sof на комп с платой.

Порядок важен: сначала проверки, потом приём файла.
  1. get_board_session  (Depends): id сессии подключения -> плата в БД
  2. get_target (Depends): плата есть в hosts.json, включена (is_enabled) и доступна (is_available)
  3. create_job: только после 1 и 2 читает тело запроса, проверяет файл и отправляет его на комп

Файл намеренно не объявлен как параметр File(...): иначе FastAPI разбирает всё тело
запроса (и пишет его на диск) ДО выполнения Depends. Без него Depends отрабатывают
первыми, а файл читается вручную уже в create_job.
"""

router = APIRouter(prefix="/board")

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
        await change_job_status(status_id=0, board_session_id=target["board_session_id"])
        return await send_to_board(target, file)
    except HTTPException as e:
        await change_job_status(status_id=3, board_session_id=target["board_session_id"], error_message=e.detail)
    finally:
        await form.close()   # удаляет временные файлы загрузки

async def change_job_status(
    board_session_id: str,
    status_id: int,
    error_message: str | None = None,
) -> None:
    async with connect() as conn:
        await conn.execute(
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