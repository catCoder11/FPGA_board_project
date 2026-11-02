from fastapi import APIRouter
router = APIRouter()


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

