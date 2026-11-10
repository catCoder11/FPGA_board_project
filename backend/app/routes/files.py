import os

import httpx
import psycopg
from fastapi import APIRouter
from fastapi import FastAPI, File, Form, Header, Path, UploadFile, HTTPException, status, Request, Depends
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

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
        if not isinstance(file, StarletteUploadFile):
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


# @router.post("/templates", dependencies=[Depends(verify_global_key)])
# async def download_template(
#         request: Request,
#         file: UploadFile = File(...),
#         name: str = Form(None),
#         created_by: str = Form(None),
#         description: str = Form(None),
#         allow_pdf: bool = Form(False)
# ) -> dict:
#     if not file.filename.endswith(".docx"):
#         raise HTTPException(status_code=400, detail="Неверный формат файла. Требуется docx")
#     content_length = request.headers.get("content-length")
#     if not content_length:
#         raise HTTPException(status_code=411, detail="Заголовок Content-Length обязателен")
#     if int(content_length) > MAX_FILE_SIZE:
#         raise HTTPException(
#             status_code=413,
#             detail=f"Файл слишком большой. Лимит: {MAX_FILE_SIZE // (1024 * 1024)} МБ"
#         )
#     if file.content_type != "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
#         raise HTTPException(
#             status_code=400,
#             detail="Некорректный MIME-тип файла."
#         )
#     bytes_read = 0
#     if name is None or name == "":
#         name = file.filename[:-5]
#     file_id = add_docx_to_DB(name, created_by, description, allow_pdf)
#     file_path = UPLOAD_DIR / (str(file_id)+".docx")
#     try:
#         # Потоковая запись файла на диск по частям
#         async with aiofiles.open(file_path, "wb") as buffer:
#             while content := await file.read(CHUNK_SIZE):
#                 # Дополнительная защита: проверяем реальный размер в процессе чтения
#                 bytes_read += len(content)
#                 if bytes_read > MAX_FILE_SIZE:
#                     raise HTTPException(status_code=413, detail="Файл превысил допустимый лимит")
#
#                 await buffer.write(content)
#
#     # except HTTPException:
#     #     # Если сработал лимит во время чтения, удаляем недозаписанный файл
#     #     if file_path.exists():
#     #         file_path.unlink()
#     #     raise
#
#     except Exception as e:
#         if file_path.exists():
#             file_path.unlink()
#             with engine.connect() as conn:
#                 conn.execute(text("""DELETE FROM templates WHERE id = :id"""), {"id": file_id})
#                 conn.commit()
#         raise HTTPException(status_code=500, detail=f"Ошибка при записи файла: {e}")
#
#     return {
#         "id": file_id,
#         "filename": name,
#         "size_bytes": bytes_read,
#         "status": "Успешно загружен"
#     }