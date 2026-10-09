import asyncio
import os
import sys

sys.path.append(os.getcwd())

import app.models
from app.models.product_review import ProductReview

from app.database import get_db, engine
from app.routers.campaigns import report
from app.models.user import User
from app.models.ads_account import AdsAccount
from sqlalchemy import text, select
import traceback

async def main():
    async for db in get_db():
        try:
            res = await db.execute(text("SELECT id, marketplace FROM ads_accounts WHERE id=1"))
            row = res.first()
            print("Account 1:", row)
            
            u_obj = (await db.execute(select(User).where(User.id == 1))).scalars().first()
            if not u_obj:
                print("No user 1")
                break
                
            res_report = await report(
                ads_account_id=1,
                db=db,
                current_user=u_obj,
                state=None,
                targeting_type=None,
                sort="spend",
                dir="desc",
                page=1,
                page_size=50,
                export=False
            )
            print("Report success:", res_report.get("total_count"))
            
        except Exception as e:
            traceback.print_exc()
        break

if __name__ == "__main__":
    asyncio.run(main())
