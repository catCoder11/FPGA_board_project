import os
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

router = APIRouter()
BOARD_AGENT_URL='http://192.168.1.50:9000'
BOARD_AGENT_TOKEN='change-me'
BOARD_AGENT_CONNECT_TIMEOUT=5
BOARD_AGENT_READ_TIMEOUT=60
# Идентификатор (для логов и будущей таблицы boards)
BOARD_ID=1
# Ограничение, чтобы не отправлять лишнего
UPLOAD_MAX_SIZE_MB=50
MAX_FILE_SIZE = UPLOAD_MAX_SIZE_MB * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSION = ".sof"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

@router.get("/login")
async def login():
    pass

@router.post("/board_available")
async def board_available(offset: int = 0, limit: int = -1):
    pass

@router.get("/results")
async def get_results(offset: int = 0, limit: int = -1):
    pass

@router.post("/connect")
async def connect():
    pass

@router.post("/disconnect")
async def disconnect():
    pass

def _check_session_sync(session_hash: str) -> bool:
    # Предполагаемая схема: sessions(session_hash text, expires_at timestamptz)
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_hash = %s AND expires_at > now()",
            (session_hash,),
        ).fetchone()
    return row is not None


async def verify_session(
        x_session_id: str | None = Header(None, alias="X-Session-Id"),
) -> str:
    """Принимает хэшированный id сессии, сверяет с БД. 401 если не найден/истёк."""
    if not x_session_id or len(x_session_id) > 128:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительная сессия")
    try:
        valid = await run_in_threadpool(_check_session_sync, x_session_id)
    except psycopg.Error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="База данных недоступна")
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительная сессия")
    return x_session_id


# Запас на multipart-заголовки и boundary поверх самого файла
MULTIPART_OVERHEAD = 64 * 1024


# Файл не объявлен как параметр File(...) намеренно: иначе FastAPI разбирает
# всё тело запроса (и пишет его на диск) ДО проверки сессии. Здесь сначала
# проверяется сессия и Content-Length, и только потом читается тело.
@router.post(
    "/send_file",
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
async def send_file(request: Request, session_hash: str = Depends(verify_session)) -> dict:
    content_length = request.headers.get("content-length")
    if not content_length or not content_length.isdigit():
        raise HTTPException(status_code=411, detail="Заголовок Content-Length обязателен")
    if int(content_length) > MAX_FILE_SIZE + MULTIPART_OVERHEAD:
        raise HTTPException(status_code=413, detail=f"Файл слишком большой. Лимит: {UPLOAD_MAX_SIZE_MB} МБ")

    try:
        form = await request.form(max_files=1, max_fields=5)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректное тело запроса, ожидается multipart/form-data")

    try:
        file = form.get("file")
        # request.form() отдаёт starlette UploadFile; fastapi.UploadFile — его подкласс,
        # поэтому isinstance с ним всегда был бы False
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=400, detail="Поле file с файлом обязательно")

        filename = os.path.basename(file.filename or "")
        if not filename.lower().endswith(ALLOWED_EXTENSION):
            raise HTTPException(status_code=400, detail="Неверный формат файла. Требуется .sof")

        # Проверяем реальный размер (Content-Length включает multipart-обвязку)
        size = file.size if file.size is not None else 0
        if size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail=f"Файл слишком большой. Лимит: {UPLOAD_MAX_SIZE_MB} МБ")
        if size == 0:
            raise HTTPException(status_code=400, detail="Пустой файл")
        content = await file.read()
    finally:
        await form.close()

    # Отправка на BOARD_AGENT (эндпоинт /upload — предположение, см. примечание)
    timeout = httpx.Timeout(BOARD_AGENT_READ_TIMEOUT, connect=BOARD_AGENT_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{BOARD_AGENT_URL}/upload",
                headers={"Authorization": f"Bearer {BOARD_AGENT_TOKEN}"},
                data={"board_id": str(BOARD_ID)},
                files={"file": (filename, content, "application/octet-stream")},
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="BOARD_AGENT не ответил вовремя")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="BOARD_AGENT недоступен")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"BOARD_AGENT вернул ошибку {resp.status_code}")

    return {"filename": filename, "size_bytes": size, "board_id": BOARD_ID, "status": "Отправлен на плату"}



"""POST /jobs: отправка .sof на комп с платой.

Порядок важен: сначала проверки, потом приём файла.
  1. get_board_session  (Depends): id сессии подключения -> плата в БД
  2. get_target (Depends): плата есть в hosts.json, включена (is_enabled) и доступна (is_available)
  3. create_job: только после 1 и 2 читает тело запроса, проверяет файл и отправляет его на комп

Файл намеренно не объявлен как параметр File(...): иначе FastAPI разбирает всё тело
запроса (и пишет его на диск) ДО выполнения Depends. Без него Depends отрабатывают
первыми, а файл читается вручную уже в create_job.
"""


router = APIRouter()

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
async def create_job(request: Request, target: dict = Depends(get_target)):
    # Сюда доходим только после get_board_session и get_target, тело запроса ещё не прочитано.

    form = await request.form()
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(400, "Нужен файл в поле file")
        validate_file(file)
        with connect() as conn:
            row = await conn.execute(
                """INSERT INTO jobs (job_id, status_id, board_session_id)
                VALUES (%(job_id)s, %(status_id)s, %(board_session_id)s)
                """,
                {
                    "job_id": uuid.uuid4(),
                    "status_id": 0,
                    "board_session_id": target["board_session_id"],
                })
        return await send_to_board(target, file)
    except HTTPException as e:
        with connect() as conn:
            row = await conn.execute(
                """INSERT INTO jobs (job_id, status_id, board_session_id)
                VALUES (%(job_id)s, %(status_id)s, %(board_session_id)s)
                """,
                {
                    "job_id": uuid.uuid4(),
                    "status_id": 3,
                    "board_session_id": target["board_session_id"],
                })
    finally:
        await form.close()   # удаляет временные файлы загрузки