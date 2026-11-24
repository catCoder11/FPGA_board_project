from fastapi import Depends, FastAPI
from app.routes import auth, board_dialog, boards, files
app = FastAPI()
app.include_router(files.router)
app.include_router(auth.router)
app.include_router(boards.router)
app.include_router(board_dialog.session_router)
app.include_router(board_dialog.router, dependencies=[Depends(board_dialog.refresh_board_session)])
