import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.contact_message import ContactMessage

async def list_recent_messages():
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(ContactMessage).order_by(ContactMessage.created_at.desc()).limit(50)
        )
        messages = res.scalars().all()
        if not messages:
            print("No contact messages found.")
            return

        print(f"--- Showing newest {len(messages)} contact message(s) ---")
        for m in messages:
            snippet = (m.message or "")[:200]
            print(f"ID: {m.id} | Created: {m.created_at} | Name: {m.name} | Email: {m.email}")
            print(f"Message: {snippet}")
            print("-" * 60)

def main():
    asyncio.run(list_recent_messages())

if __name__ == "__main__":
    main()
