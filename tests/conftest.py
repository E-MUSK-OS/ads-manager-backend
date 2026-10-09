import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import date, timedelta
from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.product import Product

engine = create_async_engine(
    "sqlite+aiosqlite://", 
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

@pytest_asyncio.fixture(autouse=True)
async def db_setup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def db_session():
    async with TestingSessionLocal() as session:
        yield session

@pytest_asyncio.fixture
async def user_a(db_session: AsyncSession):
    user = User(email="a@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.commit()
    return user

@pytest_asyncio.fixture
async def user_b(db_session: AsyncSession):
    user = User(email="b@test.com", hashed_password="hash")
    db_session.add(user)
    await db_session.commit()
    return user

@pytest_asyncio.fixture
async def account_a(db_session: AsyncSession, user_a: User):
    acc = AdsAccount(user_id=user_a.id, marketplace="IN", is_mock=True)
    db_session.add(acc)
    await db_session.commit()
    
    p = Product(ads_account_id=acc.id, asin="B000000000", title="Test Product", price=100.0)
    db_session.add(p)
    await db_session.commit()
    return acc

@pytest_asyncio.fixture
async def account_b(db_session: AsyncSession, user_b: User):
    acc = AdsAccount(user_id=user_b.id, marketplace="US", is_mock=True)
    db_session.add(acc)
    await db_session.commit()
    
    p = Product(ads_account_id=acc.id, asin="B000000001", title="Test Product B", price=10.0)
    db_session.add(p)
    await db_session.commit()
    return acc

@pytest_asyncio.fixture(autouse=True)
def setup_auth_override():
    from fastapi import Header
    # Make get_current_user read from header
    async def override_get_current_user(x_test_user: str = Header("1")):
        async with TestingSessionLocal() as session:
            user = await session.get(User, int(x_test_user))
            return user
            
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)

@pytest_asyncio.fixture
async def client_a(user_a: User):
    def override_get_db():
        session = TestingSessionLocal()
        try: yield session
        finally: pass
        
    app.dependency_overrides[get_db] = override_get_db
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"x-test-user": str(user_a.id)}) as c:
        yield c
        
    app.dependency_overrides.pop(get_db, None)

@pytest_asyncio.fixture
async def client_b(user_b: User):
    def override_get_db():
        session = TestingSessionLocal()
        try: yield session
        finally: pass
        
    app.dependency_overrides[get_db] = override_get_db
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"x-test-user": str(user_b.id)}) as c:
        yield c
        
    app.dependency_overrides.pop(get_db, None)
