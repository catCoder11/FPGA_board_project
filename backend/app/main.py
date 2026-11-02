from fastapi import FastAPI
from app.routes import files
app = FastAPI()
app.include_router(files.router)