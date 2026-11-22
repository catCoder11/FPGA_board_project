from fastapi import FastAPI
from app.routes import files, auth
app = FastAPI()
app.include_router(files.router)
app.include_router(auth.router)