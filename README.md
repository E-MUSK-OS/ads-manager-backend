# Ads Manager - Backend

This is the FastAPI backend for the AI-Powered Amazon Ads Management Platform.

## Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) or `pip` for package management

## Environment Variables
Copy `.env.example` to `.env` and fill in the necessary values:
```bash
cp .env.example .env
```
Ensure you have the `DATABASE_URL` pointing to your Neon database (using the `postgresql+asyncpg://` driver format).
`REDIS_URL` should point to your Upstash Redis instance (currently deferred until background jobs are required).

## Running Locally

1. Install dependencies:
```bash
cd ads-backend
uv sync  # or pip install -r requirements.txt
```

2. Run Alembic migrations:
```bash
alembic upgrade head
```

3. Start the server:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
The API will be available at `http://localhost:8000`.
