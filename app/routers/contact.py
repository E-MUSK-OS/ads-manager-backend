from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from datetime import datetime, timezone, timedelta
import logging
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.contact_message import ContactMessage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contact", tags=["contact"])

class ContactRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    message: str = Field(..., min_length=10, max_length=5000)

# Simple IP-based in-memory rate limiting: max 5 requests per 15 minutes
_RATE_LIMIT_BUCKET: dict[str, list[datetime]] = defaultdict(list)
RATE_LIMIT_WINDOW = timedelta(minutes=15)
RATE_LIMIT_MAX_REQUESTS = 5

def _check_rate_limit(client_ip: str) -> None:
    now = datetime.now(timezone.utc)
    # Filter out entries older than window
    timestamps = [ts for ts in _RATE_LIMIT_BUCKET[client_ip] if now - ts < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many contact submissions from this IP. Please try again later."
        )
    timestamps.append(now)
    _RATE_LIMIT_BUCKET[client_ip] = timestamps

@router.post("")
async def submit_contact_form(
    payload: ContactRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    # Persist the message securely to database
    record = ContactMessage(
        name=payload.name.strip(),
        email=str(payload.email),
        message=payload.message.strip(),
        ip_address=client_ip,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    # Log only the id: never log name, email or message content.
    logger.info("Contact message stored id=%s", record.id)

    return {
        "status": "ok",
        "message": "Thank you for contacting us. We will get back to you shortly.",
    }

