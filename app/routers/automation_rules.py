from fastapi import APIRouter
router = APIRouter(prefix="/automation-rules", tags=["automation-rules"])
@router.get("")
async def get_rules():
    return []
