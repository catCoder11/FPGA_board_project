import hmac
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
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
proc = None               # идущая прошивка (Popen), нужна для /abort
abort_requested = False   # abort запрошен для текущей прошивки


def reply(status, ok, output, **extra):
    return JSONResponse(status_code=status, content={"ok": ok, "output": output, **extra})


def require_token(authorization: str | None = Header(None)):
    expected = f"Bearer {AGENT_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "Неверный токен")


# def is_local(request: Request):
#     return request.client is not None and request.client.host in ("127.0.0.1", "::1")


def run(args):
    try:
        r = subprocess.run([QUARTUS, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "Превышено время ожидания quartus_pgm"
    except Exception as e:
        return -1, str(e)


def run_flash(args, workdir):
    """Прошивка через Popen: процесс доступен для /abort. Возвращает (код, вывод, прервана_ли).

    Вывод пишется в файл, а не в PIPE: wait() не зависнет, если у quartus_pgm
    остались дочерние процессы, и буфер канала не переполнится.
    """
    global proc, abort_requested
    abort_requested = False
    log_path = Path(workdir) / "out.log"
    timed_out = False
    with open(log_path, "wb") as log_file:
        try:
            p = subprocess.Popen([QUARTUS, *args], stdout=log_file, stderr=subprocess.STDOUT)
        except Exception as e:
            return -1, str(e), False
        proc = p
        try:
            p.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            timed_out = True
        finally:
            proc = None
    out = log_path.read_text(encoding="utf-8", errors="replace")
    if timed_out:
        out += "\nПревышено время ожидания quartus_pgm"
    # прервана только если процесс реально не завершился успешно
    return p.returncode, out, abort_requested and p.returncode != 0


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
            code, out, aborted = run_flash(["-c", cable, "-m", "JTAG", "-o", f"p;{path}"], d)
            seconds = round(time.time() - t0, 1)
    finally:
        lock.release()

    if aborted:
        return reply(200, False, out, aborted=True, board_id=board_id, filename=name, seconds=seconds)
    ok = code == 0
    return reply(200 if ok else 500, ok, out, board_id=board_id, filename=name, seconds=seconds)


@app.get("/status", dependencies=[Depends(require_token)])
def status():
    return {"busy": lock.locked()}


@app.get("/check", dependencies=[Depends(require_token)])
def check(request: Request):
    # if not is_local(request):
    #     require_token(authorization)
    cables, out = get_cables()
    return {"ok": bool(cables), "cables": cables, "output": out}


@app.post("/upload", dependencies=[Depends(require_token)])
def upload(file: UploadFile = File(...), board_id: str = Form("")):
    return flash_file(file, board_id)


@app.post("/abort", dependencies=[Depends(require_token)])
def abort():
    # Не берёт lock: иначе ждал бы окончания той самой прошивки, которую надо прервать.
    # Работает, потому что def-эндпоинты идут в разных потоках пула.
    global abort_requested
    p = proc
    if p is None or p.poll() is not None:
        return reply(409, False, "Прошивка не идёт")
    abort_requested = True
    p.terminate()   # на Windows это сразу жёсткое завершение
    return {"ok": True}


# @app.post("/flash")
# def flash(request: Request, file: UploadFile = File(...)):
#     if not is_local(request):
#         return reply(403, False, "Только с локального компьютера, для остальных есть /upload")
#     return flash_file(file)


# @app.get("/")
# def index():
#     return FileResponse(HTML)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
