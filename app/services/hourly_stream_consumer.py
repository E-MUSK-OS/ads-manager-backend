import os
import json
import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_hourly_metric import CampaignHourlyMetric
from app.models.hourly_stream_status import HourlyStreamStatus

logger = logging.getLogger(__name__)

# Env var names only (7.2 rule)
AMS_CONSUMER_ENABLED_ENV = "AMS_CONSUMER_ENABLED"
AWS_REGION_ENV = "AWS_REGION"
AMS_SQS_QUEUE_URL_ENV = "AMS_SQS_QUEUE_URL"

IST_TZ = ZoneInfo("Asia/Kolkata")


def is_consumer_enabled() -> bool:
    """Return whether the hourly AMS background consumer is enabled."""
    val = os.getenv(AMS_CONSUMER_ENABLED_ENV, "false").strip().lower()
    return val in ("1", "true", "yes", "on")


def parse_ist_date_and_hour(time_window_str: str) -> tuple[datetime.date, int]:
    """
    Parse an ISO 8601 UTC time window string (e.g. '2026-11-18T05:00:00Z')
    and convert to Asia/Kolkata (IST) date and hour (0-23).
    """
    cleaned = time_window_str.replace("Z", "+00:00")
    utc_dt = datetime.fromisoformat(cleaned)
    ist_dt = utc_dt.astimezone(IST_TZ)
    return ist_dt.date(), ist_dt.hour


async def process_stream_record(
    db: AsyncSession,
    record: dict[str, Any],
    account_id: Optional[int] = None,
) -> Optional[CampaignHourlyMetric]:
    """
    Process a single AMS record (sp-traffic or sp-conversion).
    Converts time_window_start to IST date and hour, matches the campaign,
    and upserts into campaign_hourly_metrics.
    """
    dataset_id = record.get("dataset_id") or record.get("dataSetId") or ""
    time_window_start = record.get("time_window_start") or record.get("timeWindowStart")
    if not time_window_start:
        logger.warning("AMS record missing time_window_start: %s", record)
        return None

    metric_date, metric_hour = parse_ist_date_and_hour(time_window_start)

    # Resolve Amazon Campaign ID
    amzn_camp_id = str(record.get("campaign_id") or record.get("campaignId") or "")

    # Look up Campaign in local database
    c_query = select(Campaign).where(
        (Campaign.amazon_campaign_id == amzn_camp_id) | (Campaign.id == amzn_camp_id if amzn_camp_id.isdigit() else False)
    )
    if account_id:
        c_query = c_query.where(Campaign.ads_account_id == account_id)

    campaign = (await db.execute(c_query)).scalars().first()
    if not campaign:
        logger.debug("Campaign %s not found in database; skipping AMS record", amzn_camp_id)
        return None

    actual_account_id = campaign.ads_account_id

    # Find existing row for (account, campaign, date, hour)
    m_query = select(CampaignHourlyMetric).where(
        CampaignHourlyMetric.ads_account_id == actual_account_id,
        CampaignHourlyMetric.campaign_id == campaign.id,
        CampaignHourlyMetric.date == metric_date,
        CampaignHourlyMetric.hour == metric_hour,
    )
    hourly_row = (await db.execute(m_query)).scalars().first()

    if not hourly_row:
        hourly_row = CampaignHourlyMetric(
            ads_account_id=actual_account_id,
            campaign_id=campaign.id,
            date=metric_date,
            hour=metric_hour,
            impressions=0,
            clicks=0,
            cost=0.0,
            purchases=0,
            sales=0.0,
        )
        db.add(hourly_row)

    # Update metrics based on dataset type
    if dataset_id == "sp-traffic":
        hourly_row.impressions += int(record.get("impressions", 0))
        hourly_row.clicks += int(record.get("clicks", 0))
        cost_val = float(record.get("cost", 0.0) or record.get("spend", 0.0))
        hourly_row.cost = round(hourly_row.cost + cost_val, 2)
    elif dataset_id == "sp-conversion":
        # Conversions are attributed back to the click hour
        purchases_val = int(record.get("purchases_7d", 0) or record.get("purchases", 0) or record.get("orders", 0))
        sales_val = float(record.get("sales_7d", 0.0) or record.get("sales", 0.0))
        hourly_row.purchases += purchases_val
        hourly_row.sales = round(hourly_row.sales + sales_val, 2)
    else:
        # Generic or unified ads-performance payload
        hourly_row.impressions += int(record.get("impressions", 0))
        hourly_row.clicks += int(record.get("clicks", 0))
        cost_val = float(record.get("cost", 0.0) or record.get("spend", 0.0))
        hourly_row.cost = round(hourly_row.cost + cost_val, 2)
        purchases_val = int(record.get("purchases_7d", 0) or record.get("purchases", 0) or record.get("orders", 0))
        sales_val = float(record.get("sales_7d", 0.0) or record.get("sales", 0.0))
        hourly_row.purchases += purchases_val
        hourly_row.sales = round(hourly_row.sales + sales_val, 2)

    # Update hourly stream status
    status_row = (await db.execute(
        select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == actual_account_id)
    )).scalars().first()

    now = datetime.now(ZoneInfo("UTC"))
    if not status_row:
        status_row = HourlyStreamStatus(
            ads_account_id=actual_account_id,
            status="ACTIVE",
            last_message_at=now,
            message_count=1,
        )
        db.add(status_row)
    else:
        status_row.status = "ACTIVE"
        status_row.last_message_at = now
        status_row.message_count += 1

    await db.commit()
    await db.refresh(hourly_row)
    return hourly_row


async def process_sqs_message_body(
    db: AsyncSession,
    body_str: str,
    account_id: Optional[int] = None,
) -> list[CampaignHourlyMetric]:
    """
    Parse and process an SQS message body (supports single record, array, or SQS SNS notification wrapper).
    """
    try:
        data = json.loads(body_str)
    except Exception as e:
        logger.error("Failed to parse SQS message JSON: %s", e)
        return []

    # Handle SNS envelope if present
    if isinstance(data, dict) and "Message" in data and isinstance(data["Message"], str):
        try:
            data = json.loads(data["Message"])
        except Exception:
            pass

    records: list[dict[str, Any]] = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        # Handle subscription confirmation notification
        if data.get("Type") == "SubscriptionConfirmation" or data.get("type") == "SubscriptionConfirmation":
            logger.info("Received AMS subscription confirmation handshake: %s", data)
            if account_id:
                status_row = (await db.execute(
                    select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == account_id)
                )).scalars().first()
                if status_row:
                    status_row.status = "ACTIVE"
                    await db.commit()
            return []

        # Check records / events array inside payload
        if "records" in data and isinstance(data["records"], list):
            records = data["records"]
        elif "events" in data and isinstance(data["events"], list):
            records = data["events"]
        else:
            records = [data]

    results: list[CampaignHourlyMetric] = []
    for r in records:
        if isinstance(r, dict):
            res = await process_stream_record(db, r, account_id=account_id)
            if res:
                results.append(res)

    return results
