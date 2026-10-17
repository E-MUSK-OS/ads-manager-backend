# Ads Manager - Backend

This is the FastAPI backend for the AI-Powered Amazon Ads Management Platform.

## Prerequisites
- Python 3.11+ (Python 3.14 compatible)
- [uv](https://github.com/astral-sh/uv) or `pip` for package management
- PostgreSQL (e.g. Neon) or SQLite for test suites

## Environment Variables
Copy `.env.example` to `.env` and fill in the necessary values:
```bash
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>/<dbname>?sslmode=require
JWT_SECRET=your_secret_key
ACCESS_TOKEN_EXPIRE_MINUTES=1440
ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```
- `DATABASE_URL`: Asynchronous PostgreSQL connection string using `postgresql+asyncpg://`.
- `JWT_SECRET`: Secret key for signing access tokens.
- `ALLOWED_ORIGINS`: Comma-separated list of allowed frontend origins for CORS.

## Running Locally

1. Install dependencies:
```bash
uv sync
```

2. Run Alembic migrations:
```bash
alembic upgrade head
```

3. Start the server:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
The API and Swagger documentation will be available at `http://localhost:8000/docs`.

4. Run automated tests:
```bash
pytest
```

## Key API Endpoints

### Campaigns & Reporting
- `GET /campaigns/report` — Filtered campaign performance report with pre-pagination health filtering and generic `min_/max_` column metrics.
- `GET /campaigns/summary` — Aggregated time-series metrics. Supports `compare=true` returning aligned `previous_series`.
- `GET /campaigns/alerts` — Breached ACOS alert campaigns.
- `GET/PUT /campaigns/settings/campaigns` — Global ACOS thresholds and customizable, data-driven `health_rules`.
- `POST /campaigns` — Transactional campaign creation supporting site (`AMAZON_AND_BEYOND` / `AMAZON_BUSINESS`), targeting, keywords, product targets, and negatives.
- `POST /campaigns/validate` — Dry-run validation of campaign configuration.
- `POST /campaigns/bulk` — Multi-campaign state and budget operations.
- `POST /campaigns/bid-suggestions` — Deterministic suggested bid ranges (low, median, high) and auto groups.
- `POST /campaigns/forecast` — Heuristic weekly click, order, and spend projections.
- `GET /campaigns/negative-suggestions` — High-spend, zero-sale search terms recommended for negative targeting.
- `GET /campaigns/brands?q=` — Brand name search for negative brand exclusions.
- `GET /campaigns/name-check` — Case-insensitive campaign name uniqueness verification.

### Negative Keywords
- `GET /campaigns/{campaign_id}/negative-keywords` — List campaign negative keywords.
- `POST /campaigns/{campaign_id}/negative-keywords` — Create negative keyword with duplicate and positive exact keyword conflict prevention.
- `DELETE /campaigns/{campaign_id}/negative-keywords/{id}` — Remove negative keyword.

### Campaign Drafts
- `GET /campaign-drafts?ads_account_id=` — List user drafts for account.
- `POST /campaign-drafts` — Create draft (up to 200KB payload).
- `PUT /campaign-drafts/{id}` — Update draft payload and step.
- `DELETE /campaign-drafts/{id}` — Discard draft.

### Products
- `GET /products/search` — Search account products by title, ASIN, or SKU with relevance sorting and active campaign counts.
- `POST /products/lookup` — Lookup up to 500 ASINs or SKUs with `found` and `not_found` reporting.

### Keywords & Ad Groups
- `POST /keywords` & `PATCH /keywords/{id}` — Add or edit keywords and bids with marketplace limit enforcement.
- `PATCH /ad-groups/{id}` — Update ad group name, state, and default bid.
- `GET /campaigns/{campaign_id}/ad-groups` — Ad groups for campaign with calculated metrics.
