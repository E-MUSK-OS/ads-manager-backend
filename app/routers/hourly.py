from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import Optional, Any
from datetime import date, timedelta
from app.database import get_db
from app.dependencies import get_current_user, assert_owned_account, load_owned_campaign
from app.models.user import User
from app.models.campaign_hourly_metric import CampaignHourlyMetric
from app.models.hourly_stream_status import HourlyStreamStatus
from app.services.metrics_service import derive

router = APIRouter(prefix="/campaigns/hourly", tags=["Hourly Performance Tracking"])


@router.get("/status")
async def get_hourly_status(
    ads_account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await assert_owned_account(db, ads_account_id, current_user)
    row = (await db.execute(
        select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == ads_account_id)
    )).scalars().first()

    if not row or row.status != "ACTIVE":
        return {
            "is_connected": False,
            "status": row.status if row else "NOT_CONNECTED",
            "subscribed_at": row.subscribed_at.isoformat() if row and row.subscribed_at else None,
            "last_message_at": row.last_message_at.isoformat() if row and row.last_message_at else None,
            "message_count": row.message_count if row else 0,
        }

    return {
        "is_connected": True,
        "status": row.status,
        "subscribed_at": row.subscribed_at.isoformat() if row.subscribed_at else None,
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
        "message_count": row.message_count,
    }


@router.get("/summary")
async def get_hourly_summary(
    ads_account_id: int,
    start: date,
    end: date,
    campaign_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    if campaign_id:
        await load_owned_campaign(db, campaign_id, current_user)

    # Check stream status
    status_row = (await db.execute(
        select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == ads_account_id)
    )).scalars().first()
    is_connected = bool(status_row and status_row.status == "ACTIVE")

    filters = [
        CampaignHourlyMetric.ads_account_id == account.id,
        CampaignHourlyMetric.date >= start,
        CampaignHourlyMetric.date <= end,
    ]
    if campaign_id:
        filters.append(CampaignHourlyMetric.campaign_id == campaign_id)

    agg = (await db.execute(
        select(
            func.coalesce(func.sum(CampaignHourlyMetric.impressions), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.clicks), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.cost), 0.0),
            func.coalesce(func.sum(CampaignHourlyMetric.purchases), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.sales), 0.0),
        ).where(and_(*filters))
    )).first()

    imp = int(agg[0]) if agg else 0
    clk = int(agg[1]) if agg else 0
    spd = round(float(agg[2]), 2) if agg else 0.0
    ords = int(agg[3]) if agg else 0
    sls = round(float(agg[4]), 2) if agg else 0.0

    has_data = bool(imp > 0 or clk > 0 or spd > 0 or ords > 0 or sls > 0)

    derived = derive(imp, clk, spd, sls, ords)

    return {
        "is_connected": is_connected,
        "has_data": has_data,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": {
            "impressions": imp if has_data else None,
            "clicks": clk if has_data else None,
            "spend": spd if has_data else None,
            "orders": ords if has_data else None,
            "sales": sls if has_data else None,
            "ctr": derived["ctr"] if has_data else None,
            "cpc": derived["cpc"] if has_data else None,
            "cvr": derived["cvr"] if has_data else None,
            "acos": derived["acos"] if has_data else None,
            "roas": derived["roas"] if has_data else None,
        },
    }


@router.get("/trends")
async def get_hourly_trends(
    ads_account_id: int,
    start: date,
    end: date,
    campaign_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    if campaign_id:
        await load_owned_campaign(db, campaign_id, current_user)

    status_row = (await db.execute(
        select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == ads_account_id)
    )).scalars().first()
    is_connected = bool(status_row and status_row.status == "ACTIVE")

    filters = [
        CampaignHourlyMetric.ads_account_id == account.id,
        CampaignHourlyMetric.date >= start,
        CampaignHourlyMetric.date <= end,
    ]
    if campaign_id:
        filters.append(CampaignHourlyMetric.campaign_id == campaign_id)

    # Group by hour (0 to 23)
    rows = (await db.execute(
        select(
            CampaignHourlyMetric.hour,
            func.coalesce(func.sum(CampaignHourlyMetric.impressions), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.clicks), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.cost), 0.0),
            func.coalesce(func.sum(CampaignHourlyMetric.purchases), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.sales), 0.0),
        )
        .where(and_(*filters))
        .group_by(CampaignHourlyMetric.hour)
    )).all()

    hour_map = {r[0]: r for r in rows}
    total_data_points = len(rows)

    hours_series = []
    for h in range(24):
        label = f"{h:02d}:00"
        if h in hour_map:
            row = hour_map[h]
            imp = int(row[1])
            clk = int(row[2])
            spd = round(float(row[3]), 2)
            ords = int(row[4])
            sls = round(float(row[5]), 2)
            derived = derive(imp, clk, spd, sls, ords)
            hours_series.append({
                "hour": h,
                "label": label,
                "impressions": imp,
                "clicks": clk,
                "spend": spd,
                "orders": ords,
                "sales": sls,
                "ctr": derived["ctr"],
                "cpc": derived["cpc"],
                "cvr": derived["cvr"],
                "acos": derived["acos"],
                "roas": derived["roas"],
            })
        else:
            hours_series.append({
                "hour": h,
                "label": label,
                "impressions": 0,
                "clicks": 0,
                "spend": 0.0,
                "orders": 0,
                "sales": 0.0,
                "ctr": None,
                "cpc": None,
                "cvr": None,
                "acos": None,
                "roas": None,
            })

    # Plain facts Peak and Low (maximum and minimum) for each key metric
    peaks_and_lows: dict[str, dict[str, Any]] = {}
    if total_data_points > 0:
        for metric_name in ("sales", "orders", "clicks", "impressions", "spend", "roas"):
            sorted_hours = sorted(hours_series, key=lambda x: (x.get(metric_name) or 0), reverse=True)
            peak_item = sorted_hours[0]
            low_item = sorted_hours[-1]
            peaks_and_lows[metric_name] = {
                "peak_hour": peak_item["hour"],
                "peak_label": peak_item["label"],
                "peak_value": peak_item.get(metric_name),
                "low_hour": low_item["hour"],
                "low_label": low_item["label"],
                "low_value": low_item.get(metric_name),
            }

    return {
        "is_connected": is_connected,
        "has_data": total_data_points > 0,
        "series": hours_series,
        "peaks_and_lows": peaks_and_lows,
    }


@router.get("/matrix")
async def get_hourly_matrix(
    ads_account_id: int,
    start: date,
    end: date,
    metric: str = "sales",
    campaign_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    if campaign_id:
        await load_owned_campaign(db, campaign_id, current_user)

    status_row = (await db.execute(
        select(HourlyStreamStatus).where(HourlyStreamStatus.ads_account_id == ads_account_id)
    )).scalars().first()
    is_connected = bool(status_row and status_row.status == "ACTIVE")

    valid_metrics = ("sales", "orders", "clicks", "impressions", "spend", "roas", "acos", "ctr", "cpc")
    norm_metric = metric.lower() if metric.lower() in valid_metrics else "sales"

    filters = [
        CampaignHourlyMetric.ads_account_id == account.id,
        CampaignHourlyMetric.date >= start,
        CampaignHourlyMetric.date <= end,
    ]
    if campaign_id:
        filters.append(CampaignHourlyMetric.campaign_id == campaign_id)

    rows = (await db.execute(
        select(
            CampaignHourlyMetric.date,
            CampaignHourlyMetric.hour,
            func.coalesce(func.sum(CampaignHourlyMetric.impressions), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.clicks), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.cost), 0.0),
            func.coalesce(func.sum(CampaignHourlyMetric.purchases), 0),
            func.coalesce(func.sum(CampaignHourlyMetric.sales), 0.0),
        )
        .where(and_(*filters))
        .group_by(CampaignHourlyMetric.date, CampaignHourlyMetric.hour)
    )).all()

    # Map (date_str, hour) -> metric value
    cell_map: dict[tuple[str, int], float] = {}
    for r in rows:
        d_str = r[0].isoformat()
        h_val = int(r[1])
        imp, clk, spd, ords, sls = int(r[2]), int(r[3]), round(float(r[4]), 2), int(r[5]), round(float(r[6]), 2)
        derived = derive(imp, clk, spd, sls, ords)

        if norm_metric == "sales":
            val = sls
        elif norm_metric == "orders":
            val = float(ords)
        elif norm_metric == "clicks":
            val = float(clk)
        elif norm_metric == "impressions":
            val = float(imp)
        elif norm_metric == "spend":
            val = spd
        elif norm_metric == "roas":
            val = derived["roas"] or 0.0
        elif norm_metric == "acos":
            val = derived["acos"] or 0.0
        elif norm_metric == "ctr":
            val = derived["ctr"] or 0.0
        elif norm_metric == "cpc":
            val = derived["cpc"] or 0.0
        else:
            val = sls

        cell_map[(d_str, h_val)] = val

    # Generate all dates from start to end
    dates_list: list[str] = []
    curr = start
    while curr <= end:
        dates_list.append(curr.isoformat())
        curr += timedelta(days=1)

    matrix_rows = []
    min_val = None
    max_val = None

    hourly_totals = {h: 0.0 for h in range(24)}
    grand_total = 0.0

    for d_str in dates_list:
        hours_dict: dict[str, float] = {}
        daily_total = 0.0
        for h in range(24):
            v = cell_map.get((d_str, h), 0.0)
            hours_dict[str(h)] = v
            daily_total += v
            hourly_totals[h] += v

            if v > 0:
                if min_val is None or v < min_val:
                    min_val = v
                if max_val is None or v > max_val:
                    max_val = v

        grand_total += daily_total
        matrix_rows.append({
            "date": d_str,
            "hours": hours_dict,
            "daily_total": round(daily_total, 2),
        })

    # Summary row
    summary_row = {
        "date": "Total",
        "hours": {str(h): round(hourly_totals[h], 2) for h in range(24)},
        "daily_total": round(grand_total, 2),
    }

    return {
        "is_connected": is_connected,
        "has_data": len(rows) > 0,
        "metric": norm_metric,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "dates": dates_list,
        "rows": matrix_rows,
        "summary_row": summary_row,
        "min_value": min_val or 0.0,
        "max_value": max_val or 0.0,
    }
