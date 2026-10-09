from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.providers.llm.claude_provider import ClaudeProvider

router = APIRouter(prefix="/ai", tags=["ai"])

@router.post("/chat")
async def chat(message: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from sqlalchemy import select
    from app.models.ads_account import AdsAccount
    from app.models.campaign import Campaign
    from app.models.metric_daily import MetricDaily
    
    # Fetch accounts
    acc_res = await db.execute(select(AdsAccount).where(AdsAccount.user_id == current_user.id))
    accounts = acc_res.scalars().all()
    
    context = "User has no connected Amazon Ads accounts."
    if accounts:
        account = accounts[0]
        # Fetch campaigns
        camp_res = await db.execute(select(Campaign).where(Campaign.ads_account_id == account.id))
        campaigns = camp_res.scalars().all()
        
        from app.services.metrics_service import account_totals
        totals = await account_totals(db, account.id, days=30)
        
        total_spend = totals.get("spend") or 0.0
        total_sales = totals.get("sales") or 0.0
        acos = totals.get("acos") or 0.0
        
        context = f"User has connected account ID {account.id}. They have {len(campaigns)} campaigns. Total spend: ${total_spend:.2f}, Total sales: ${total_sales:.2f}, ACOS: {acos:.1f}%."
    provider = ClaudeProvider()
    response = await provider.chat([{"role": "user", "content": message}], context)
    return {"response": response}
