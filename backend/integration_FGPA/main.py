import subprocess
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse

QUARTUS = r"D:\quartus\bin64\quartus_pgm.exe"
CABLE = "USB-Blaster [USB-0]"
HTML = Path(__file__).parent / "index.html"
app = FastAPI()
@app.post("/flash")
def flash(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".sof"):
        return {"ok": False, "output": "Нужен файл .sof"}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "design.sof"
        path.write_bytes(file.file.read())
        try:
            r = subprocess.run([QUARTUS, "-c", CABLE, "-m", "JTAG", "-o", f"p;{path}"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "output": str(e)}
    return {"ok": r.returncode == 0, "output": r.stdout + r.stderr}


@app.get("/check")
def check():
    try:
        r = subprocess.run([QUARTUS, "-l"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "output": str(e)}
    return {"ok": CABLE in r.stdout, "output": r.stdout + r.stderr}

@app.get("/")
def index():
    return FileResponse(HTML)
uvicorn.run(app, host="127.0.0.1", port=8000)