from fastapi import FastAPI
from app.routes import files, auth, boards
app = FastAPI()
app.include_router(files.router)
app.include_router(auth.router)
app.include_router(boards.router)