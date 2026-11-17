import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import delete
from app.database import AsyncSessionLocal
from app.models.contact_message import ContactMessage
from app.config import settings

async def purge_old_messages() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CONTACT_RETENTION_DAYS)
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            delete(ContactMessage).where(ContactMessage.created_at < cutoff)
        )
        await db.commit()
        return res.rowcount

def main():
    count = asyncio.run(purge_old_messages())
    print(f"Deleted {count} contact message(s) older than {settings.CONTACT_RETENTION_DAYS} days.")

if __name__ == "__main__":
    main()
