import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.deps import ActiveBoardSession, CurrentUser, get_board_session, get_current_user

router = APIRouter()

# Агент прошивки на компьютере с платой. Значения можно переопределить переменными
# окружения; настоящий токен в публичный репозиторий коммитить нельзя.
BOARD_AGENT_URL = os.environ.get("BOARD_AGENT_URL", "http://192.168.1.50:9000")
BOARD_AGENT_TOKEN = os.environ.get("BOARD_AGENT_TOKEN", "change-me")
BOARD_AGENT_CONNECT_TIMEOUT = 5
BOARD_AGENT_READ_TIMEOUT = 60

# Ограничение, чтобы не отправлять лишнего
UPLOAD_MAX_SIZE_MB = 50
MAX_FILE_SIZE = UPLOAD_MAX_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSION = ".sof"
# Запас на multipart-заголовки и boundary поверх самого файла
MULTIPART_OVERHEAD = 64 * 1024


@router.get("/results")
async def get_results(
    offset: int = 0, limit: int = -1, user: CurrentUser = Depends(get_current_user)
):
    """Заглушка: результаты прошивки. Пока пусто, но уже только для вошедших."""
    pass


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
async def send_file(
    request: Request, session: ActiveBoardSession = Depends(get_board_session)
) -> dict:
    # get_board_session уже проверил, что пользователь вошёл и сейчас занимает плату,
    # поэтому прошить можно только свою плату и только пока не вышло время сессии.
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

    # Отправка на агент. Адрес /upload и поля формы нужно согласовать с тем, кто делает агента.
    timeout = httpx.Timeout(BOARD_AGENT_READ_TIMEOUT, connect=BOARD_AGENT_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{BOARD_AGENT_URL}/upload",
                headers={"Authorization": f"Bearer {BOARD_AGENT_TOKEN}"},
                data={"board_id": str(session.board_id)},
                files={"file": (filename, content, "application/octet-stream")},
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="BOARD_AGENT не ответил вовремя")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="BOARD_AGENT недоступен")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"BOARD_AGENT вернул ошибку {resp.status_code}")

    return {
        "filename": filename,
        "size_bytes": size,
        "board_id": session.board_id,
        "status": "Отправлен на плату",
    }