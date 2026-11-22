
import hmac
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "change-me")
QUARTUS = os.environ.get("QUARTUS_PGM", r"D:\quartus\bin64\quartus_pgm.exe")
CABLE = os.environ.get("CABLE", "")
PORT = int(os.environ.get("AGENT_PORT", "9000"))
MAX_SIZE = 50 * 1024 * 1024
TIMEOUT = 120
HTML = Path(__file__).parent / "index.html"

app = FastAPI()
lock = threading.Lock()


def reply(status, ok, output, **extra):
    return JSONResponse(status_code=status, content={"ok": ok, "output": output, **extra})


def require_token(authorization):
    expected = f"Bearer {AGENT_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Неверный токен")


def is_local(request: Request):
    return request.client is not None and request.client.host in ("127.0.0.1", "::1")


def run(args):
    try:
        r = subprocess.run([QUARTUS, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "Превышено время ожидания quartus_pgm"
    except Exception as e:
        return -1, str(e)


def get_cables():
    _, out = run(["-l"])
    return re.findall(r"^\s*\d+\)\s+(.+?)\s*$", out, flags=re.M), out


def flash_file(file: UploadFile, board_id: str = ""):

    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(".sof"):
        return reply(400, False, "Нужен файл .sof")
    data = file.file.read(MAX_SIZE + 1)
    if not data:
        return reply(400, False, "Пустой файл")
    if len(data) > MAX_SIZE:
        return reply(413, False, "Файл слишком большой")

    if not lock.acquire(blocking=False):
        return reply(409, False, "Плата занята, попробуйте через несколько секунд")
    try:
        cable = CABLE
        if not cable:
            cables, out = get_cables()
            if not cables:
                return reply(503, False, "Программатор не найден\n\n" + out)
            cable = cables[0]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "design.sof"
            path.write_bytes(data)
            t0 = time.time()
            code, out = run(["-c", cable, "-m", "JTAG", "-o", f"p;{path}"])
            seconds = round(time.time() - t0, 1)
    finally:
        lock.release()

    ok = code == 0
    return reply(200 if ok else 500, ok, out, board_id=board_id, filename=name, seconds=seconds)


@app.get("/status")
def status():
    return {"busy": lock.locked()}


@app.get("/check")
def check(request: Request, authorization: str | None = Header(None)):
    if not is_local(request):
        require_token(authorization)
    cables, out = get_cables()
    return {"ok": bool(cables), "cables": cables, "output": out}


@app.post("/upload")
def upload(file: UploadFile = File(...), board_id: str = Form(""),
           authorization: str | None = Header(None)):
    require_token(authorization)
    return flash_file(file, board_id)


@app.post("/flash")
def flash(request: Request, file: UploadFile = File(...)):
    if not is_local(request):
        return reply(403, False, "Только с локального компьютера, для остальных есть /upload")
    return flash_file(file)


# @app.get("/")
# def index():
#     return FileResponse(HTML)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)