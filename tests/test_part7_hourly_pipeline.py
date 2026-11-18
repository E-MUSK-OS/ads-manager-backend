import pytest
from datetime import date
from sqlalchemy import select
from app.models.campaign import Campaign
from app.models.campaign_hourly_metric import CampaignHourlyMetric
from app.models.hourly_stream_status import HourlyStreamStatus
from app.services.hourly_stream_consumer import (
    parse_ist_date_and_hour,
    process_stream_record,
    process_sqs_message_body,
)


def test_ist_conversion_timezone():
    # UTC: 2026-11-18 05:00:00 UTC -> IST is UTC+05:30 -> 10:30 (Hour 10)
    utc_time_1 = "2026-11-18T05:00:00Z"
    d1, h1 = parse_ist_date_and_hour(utc_time_1)
    assert d1 == date(2026, 11, 18)
    assert h1 == 10

    # UTC late night: 2026-11-18 20:00:00 UTC -> IST is +05:30 -> 2026-11-19 01:30 (Next day, Hour 1)
    utc_time_2 = "2026-11-18T20:00:00Z"
    d2, h2 = parse_ist_date_and_hour(utc_time_2)
    assert d2 == date(2026, 11, 19)
    assert h2 == 1


@pytest.mark.asyncio
async def test_process_sp_traffic_message(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="amzn_stream_c_1",
        name="Stream Campaign 1",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    traffic_payload = {
        "dataset_id": "sp-traffic",
        "time_window_start": "2026-11-18T08:00:00Z",  # 08:00 UTC -> 13:30 IST (Hour 13)
        "campaign_id": "amzn_stream_c_1",
        "impressions": 1500,
        "clicks": 45,
        "cost": 315.50,
    }

    result = await process_stream_record(db_session, traffic_payload, account_id=account_a.id)
    assert result is not None
    assert result.date == date(2026, 11, 18)
    assert result.hour == 13
    assert result.impressions == 1500
    assert result.clicks == 45
    assert result.cost == 315.50
    assert result.purchases == 0
    assert result.sales == 0.0

    # Verify stream status was updated to ACTIVE
    status_row = (await db_session.execute(
        select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == account_a.id)
    )).scalars().first()
    assert status_row is not None
    assert status_row.status == "ACTIVE"
    assert status_row.message_count == 1


@pytest.mark.asyncio
async def test_late_conversion_attribution(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="amzn_stream_c_2",
        name="Stream Campaign 2",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    time_window = "2026-11-18T09:00:00Z"  # 14:30 IST (Hour 14)

    # 1. First comes traffic message
    traffic_msg = {
        "dataset_id": "sp-traffic",
        "time_window_start": time_window,
        "campaign_id": "amzn_stream_c_2",
        "impressions": 2000,
        "clicks": 60,
        "cost": 420.0,
    }
    await process_stream_record(db_session, traffic_msg, account_id=account_a.id)

    # 2. Later, late conversion message arrives for the SAME click hour
    conv_msg = {
        "dataset_id": "sp-conversion",
        "time_window_start": time_window,
        "campaign_id": "amzn_stream_c_2",
        "purchases_7d": 5,
        "sales_7d": 2500.0,
    }
    updated_result = await process_stream_record(db_session, conv_msg, account_id=account_a.id)
    assert updated_result is not None
    assert updated_result.hour == 14
    # Traffic is preserved
    assert updated_result.impressions == 2000
    assert updated_result.clicks == 60
    assert updated_result.cost == 420.0
    # Conversions are updated in the click hour
    assert updated_result.purchases == 5
    assert updated_result.sales == 2500.0


@pytest.mark.asyncio
async def test_sqs_json_envelope_and_duplicate_processing(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="amzn_stream_c_3",
        name="Stream Campaign 3",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    sqs_body = """{
        "records": [
            {
                "dataset_id": "sp-traffic",
                "time_window_start": "2026-11-18T10:00:00Z",
                "campaign_id": "amzn_stream_c_3",
                "impressions": 100,
                "clicks": 5,
                "cost": 30.0
            }
        ]
    }"""

    results = await process_sqs_message_body(db_session, sqs_body, account_id=account_a.id)
    assert len(results) == 1
    assert results[0].clicks == 5
    assert results[0].hour == 15  # 10:00 UTC -> 15:30 IST
