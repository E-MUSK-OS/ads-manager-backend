import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from app.models.user import User

async def main():
    engine = create_async_engine("sqlite+aiosqlite:///./ads_manager.db")
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        for u in users:
            print(u.id, u.email, u.auth_provider, u.hashed_password)

asyncio.run(main())
