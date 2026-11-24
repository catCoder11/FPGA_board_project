import os

import httpx
import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.db import connect

router = APIRouter()
BOARD_AGENT_URL = os.environ.get('BOARD_AGENT_URL', '').rstrip('/')
BOARD_AGENT_TOKEN = os.environ.get('BOARD_AGENT_TOKEN', '')
BOARD_AGENT_CONNECT_TIMEOUT = 5
BOARD_AGENT_READ_TIMEOUT = 150
# Идентификатор (для логов и будущей таблицы boards)
BOARD_ID=1
# Ограничение, чтобы не отправлять лишнего
UPLOAD_MAX_SIZE_MB=50
MAX_FILE_SIZE = UPLOAD_MAX_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSION = ".sof"
# Запас на multipart-заголовки и boundary поверх самого файла
MULTIPART_OVERHEAD = 64 * 1024

@router.get("/login")
async def login():
    pass

def _check_session_sync(session_hash: str) -> bool:
    # Предполагаемая схема: sessions(session_hash text, expires_at timestamptz)
    with connect() as conn:
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
    if not BOARD_AGENT_URL or not BOARD_AGENT_TOKEN:
        raise HTTPException(status_code=503, detail="Подключение к агенту ПЛИС не настроено")

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

    return {"filename": filename, "size_bytes": size, "board_id": BOARD_ID, "status": "Отправлен на плату" }
