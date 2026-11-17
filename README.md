# Ads Manager - Backend

This is the FastAPI backend for the AI-Powered Amazon Ads Management Platform.

## Prerequisites
- Python 3.11+ (Python 3.14 compatible)
- [uv](https://github.com/astral-sh/uv) or `pip` for package management
- PostgreSQL (e.g. Neon) or SQLite for test suites

## Environment Variables
Copy `.env.example` to `.env` and fill in the necessary values:
```bash
ENVIRONMENT=development                                  # Set ENVIRONMENT=production on any public deployment.
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>/<dbname>?sslmode=require
JWT_SECRET_KEY=your_very_long_random_secret_key_here   # min 32 chars; "changeme" rejected at startup
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=30
FRONTEND_URL=http://localhost:3000                      # used for CORS; comma-separate multiple origins (must use https in production)
SUPPORTED_MARKETPLACES=IN                               # Comma-separated allowed marketplaces (default IN)
CONTACT_RETENTION_DAYS=365                              # Retention window for contact form inquiries


# Amazon LWA (only required when ADS_PROVIDER=amazon)
ADS_PROVIDER=mock                                       # mock | amazon
AMAZON_LWA_CLIENT_ID=<your-lwa-client-id>
AMAZON_LWA_CLIENT_SECRET=<your-lwa-client-secret>
AMAZON_LWA_REDIRECT_URI=http://localhost:3000/connect-amazon/callback
TOKEN_ENCRYPTION_KEY=<fernet-key-generate-with-python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
AMAZON_ADS_REGION=eu                                    # na | eu | fe — TODO(verify correct region)
```
- `DATABASE_URL`: Asynchronous PostgreSQL connection string using `postgresql+asyncpg://`.
- `JWT_SECRET_KEY`: Secret key for signing JWTs. Must be set and cannot be `"changeme"` (app refuses to start).
- `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`: Access token TTL in minutes (default 30).
- `JWT_REFRESH_TOKEN_EXPIRE_DAYS`: Refresh token TTL in days (default 30).
- `FRONTEND_URL`: Frontend origin(s) used for CORS — replaces the old `ALLOWED_ORIGINS` name.
- `ADS_PROVIDER`: `mock` (default, no Amazon credentials needed) or `amazon` (requires LWA creds below).
- `AMAZON_LWA_CLIENT_ID` / `AMAZON_LWA_CLIENT_SECRET`: From your Login with Amazon Security Profile.
- `TOKEN_ENCRYPTION_KEY`: Fernet key used to encrypt stored refresh tokens at rest.

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
