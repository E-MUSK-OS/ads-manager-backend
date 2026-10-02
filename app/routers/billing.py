from fastapi import APIRouter
router = APIRouter(prefix="/billing", tags=["billing"])

@router.get("/plan")
async def get_plan():
    # TODO: real Stripe integration
    return {"plan": "Free"}
