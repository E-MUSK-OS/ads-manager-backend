import os
import sys

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import all models so relationship mappers resolve
import app.models.user
import app.models.ads_account
import app.models.campaign
import app.models.campaign_product
import app.models.negative_keyword
import app.models.target
import app.models.user_campaign_settings
import app.models.ad_group
import app.models.keyword
import app.models.search_term
import app.models.metric_daily
import app.models.placement_metric_daily
import app.models.product
import app.models.product_review
import app.models.campaign_draft
import app.models.recommendation_rule
import app.models.recommendation
import app.models.recommendation_run
import app.models.automation_rule
import app.models.ai_suggestion
import app.models.ai_chat_message
import app.models.saved_filter
import app.models.campaign_history
import app.models.sync_run
import app.models.contact_message

import asyncio
import random
from datetime import datetime, timezone, timedelta, date
from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.hourly_stream_status import HourlyStreamStatus
from app.models.campaign_hourly_metric import CampaignHourlyMetric
from app.core.security import get_password_hash
from tests.factories import TestAdsDataSeeder

# Typical Indian e-commerce hourly volume curve (IST)
# Peaks in evening 18:00 - 22:00, low in late night 01:00 - 05:00
HOURLY_CURVE = {
    0: 0.15,
    1: 0.08,
    2: 0.05,
    3: 0.04,
    4: 0.05,
    5: 0.10,
    6: 0.25,
    7: 0.45,
    8: 0.70,
    9: 0.85,
    10: 1.05,
    11: 1.15,
    12: 1.10,
    13: 0.95,
    14: 1.00,
    15: 1.10,
    16: 1.15,
    17: 1.25,
    18: 1.55,
    19: 1.85,
    20: 2.10,  # Peak hour
    21: 1.95,
    22: 1.30,
    23: 0.55,
}

async def seed_hourly_stream_for_account(db, account: AdsAccount, campaigns: list[Campaign]):
    print(f"Connecting Amazon Marketing Stream for account ID {account.id}...")
    stmt_stream = select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == account.id)
    stream_row = (await db.execute(stmt_stream)).scalars().first()

    now_utc = datetime.now(timezone.utc)
    if not stream_row:
        stream_row = HourlyStreamStatus(
            ads_account_id=account.id,
            status="ACTIVE",
            sp_traffic_subscription_id=f"sub_traffic_{account.id}_sp_india",
            sp_conversion_subscription_id=f"sub_conv_{account.id}_sp_india",
            destination_arn=f"arn:aws:sqs:eu-west-1:123456789012:ams-stream-sqs-{account.id}",
            subscribed_at=now_utc - timedelta(days=14),
            last_message_at=now_utc,
            message_count=2016,
        )
        db.add(stream_row)
    else:
        stream_row.status = "ACTIVE"
        stream_row.sp_traffic_subscription_id = f"sub_traffic_{account.id}_sp_india"
        stream_row.sp_conversion_subscription_id = f"sub_conv_{account.id}_sp_india"
        stream_row.destination_arn = f"arn:aws:sqs:eu-west-1:123456789012:ams-stream-sqs-{account.id}"
        stream_row.subscribed_at = now_utc - timedelta(days=14)
        stream_row.last_message_at = now_utc
        stream_row.message_count = 2016

    await db.commit()
    print("Hourly stream status set to ACTIVE.")

    # Refresh hourly metrics: clean old records for this account
    print(f"Generating realistic hourly intra-day metrics for {len(campaigns)} campaigns across 14 days...")
    await db.execute(delete(CampaignHourlyMetric).where(CampaignHourlyMetric.ads_account_id == account.id))
    await db.commit()

    today = now_utc.date()
    metric_records = []

    # Seed for past 14 days
    for day_offset in range(14):
        target_date = today - timedelta(days=day_offset)

        for c_idx, cmp in enumerate(campaigns):
            # Base scale per campaign (budget-weighted)
            budget = float(cmp.daily_budget or 500.0)
            base_hourly_imp = max(20, int(budget * 0.35 * (0.8 + 0.4 * (c_idx % 3))))

            for hour in range(24):
                curve = HOURLY_CURVE.get(hour, 0.5)
                # Small deterministic-feeling random jitter
                jitter = 0.85 + (hash((cmp.id, str(target_date), hour)) % 30) / 100.0
                impressions = max(5, int(base_hourly_imp * curve * jitter))

                ctr = 0.025 + (hash((cmp.id, hour)) % 20) / 1000.0  # 2.5% - 4.5% CTR
                clicks = int(impressions * ctr)
                if clicks == 0 and curve > 0.8 and impressions > 15:
                    clicks = 1

                cpc = 3.5 + (hash((cmp.id, hour)) % 30) / 10.0  # ₹3.50 - ₹6.50 CPC
                cost = round(clicks * cpc, 2)

                cvr = 0.08 + (hash((cmp.id, str(target_date), hour)) % 10) / 100.0  # 8% - 18% CVR
                purchases = int(clicks * cvr)
                if purchases == 0 and clicks >= 4 and curve >= 1.4:
                    purchases = 1

                # Average order value ₹650 - ₹1,400
                aov = 650.0 + (hash((cmp.id, hour)) % 75) * 10.0
                sales = round(purchases * aov, 2)

                metric_records.append(
                    CampaignHourlyMetric(
                        ads_account_id=account.id,
                        campaign_id=cmp.id,
                        date=target_date,
                        hour=hour,
                        impressions=impressions,
                        clicks=clicks,
                        cost=cost,
                        purchases=purchases,
                        sales=sales,
                    )
                )

    db.add_all(metric_records)
    await db.commit()
    print(f"Successfully seeded {len(metric_records)} hourly metric records for test account!")

async def main():
    async with AsyncSessionLocal() as db:
        stmt = select(User).where(User.email == "test@gmail.com")
        res = await db.execute(stmt)
        user = res.scalars().first()
        if not user:
            print("Creating user test@gmail.com...")
            user = User(
                email="test@gmail.com",
                hashed_password=get_password_hash("Test@123"),
                company_name="bruhhh",
                auth_provider="local"
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            print(f"User created with ID {user.id}")
        else:
            print(f"User test@gmail.com exists with ID {user.id}. Updating password and company_name...")
            user.hashed_password = get_password_hash("Test@123")
            user.company_name = "bruhhh"
            await db.commit()

        stmt_acc = select(AdsAccount).where(AdsAccount.user_id == user.id)
        res_acc = await db.execute(stmt_acc)
        account = res_acc.scalars().first()

        if not account:
            print("Creating dummy Amazon AdsAccount for user...")
            account = AdsAccount(
                user_id=user.id,
                profile_id=f"dummy_profile_{user.id}_in",
                marketplace="IN",
                amazon_account_name="bruhhh Amazon Ads",
                connection_status="connected",
                is_active=True,
                is_mock=False,
                sync_status="idle",
                last_synced_at=datetime.now(timezone.utc),
            )
            db.add(account)
            await db.commit()
            await db.refresh(account)
            print(f"AdsAccount created with ID {account.id}")
        else:
            print(f"AdsAccount exists with ID {account.id}. Ensuring connected status and fresh last_synced_at...")
            account.connection_status = "connected"
            account.is_active = True
            account.last_synced_at = datetime.now(timezone.utc)
            account.sync_status = "idle"
            await db.commit()

        c_res = await db.execute(select(Campaign).where(Campaign.ads_account_id == account.id))
        campaigns = list(c_res.scalars().all())
        if not campaigns:
            print(f"Seeding campaigns and metrics for account ID {account.id}...")
            seeder = TestAdsDataSeeder(db)
            await seeder.generate_for_new_connection(account.id)
            c_res = await db.execute(select(Campaign).where(Campaign.ads_account_id == account.id))
            campaigns = list(c_res.scalars().all())
            print(f"Seeded {len(campaigns)} campaigns.")
        else:
            print(f"Account already has {len(campaigns)} campaigns.")

        # Connect hourly stream and seed intra-day metrics for this test account
        await seed_hourly_stream_for_account(db, account, campaigns)

if __name__ == "__main__":
    asyncio.run(main())
