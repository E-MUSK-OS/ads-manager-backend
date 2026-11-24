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
FRONTEND_BASE_URL=http://localhost:3000                 # required in production with HTTPS (no localhost); used for OAuth callback redirects
SUPPORTED_MARKETPLACES=IN                               # Comma-separated allowed marketplaces (default IN)
CONTACT_RETENTION_DAYS=365                              # Retention window for contact form inquiries


# Amazon LWA Credentials
ADS_PROVIDER=amazon
AMAZON_LWA_CLIENT_ID=<your-lwa-client-id>
AMAZON_LWA_CLIENT_SECRET=<your-lwa-client-secret>
AMAZON_LWA_REDIRECT_URI=https://api.netutechno.com/api/v1/amazon-ads/callback
TOKEN_ENCRYPTION_KEY=<fernet-key-generate-with-python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
AMAZON_ADS_REGION=eu                                    # India marketplace routes to EU region (eu)
AMAZON_LWA_SCOPE=advertising::campaign_management
AMAZON_OAUTH_STATE_TTL_MINUTES=10
```
- `DATABASE_URL`: Asynchronous PostgreSQL connection string using `postgresql+asyncpg://`.
- `JWT_SECRET_KEY`: Secret key for signing JWTs. Must be set and cannot be `"changeme"` (app refuses to start).
- `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`: Access token TTL in minutes (default 30).
- `JWT_REFRESH_TOKEN_EXPIRE_DAYS`: Refresh token TTL in days (default 30).
- `FRONTEND_URL`: Frontend origin(s) used for CORS — replaces the old `ALLOWED_ORIGINS` name.
- `FRONTEND_BASE_URL`: Public HTTPS URL of the frontend application (required in production; never localhost). Used by OAuth callback handler to redirect browser back to frontend.
- `ADS_PROVIDER`: `amazon` (default; only official Amazon Ads API is supported).
- `AMAZON_LWA_CLIENT_ID` / `AMAZON_LWA_CLIENT_SECRET`: From your Login with Amazon Security Profile.
- `AMAZON_LWA_REDIRECT_URI`: Exact registered callback URL matching Amazon LWA Security Profile Allowed Return URLs.
- `TOKEN_ENCRYPTION_KEY`: Fernet key used to encrypt stored refresh tokens at rest.
- `AMAZON_ADS_REGION`: Regional endpoint group (`eu` for India / Europe, `na` for North America, `fe` for Far East).
- `AMAZON_LWA_SCOPE`: Scope granted by user (`advertising::campaign_management`).
- `AMAZON_OAUTH_STATE_TTL_MINUTES`: Lifespan of one-time OAuth state token before expiration (default 10 minutes).

## Amazon Ads (Login with Amazon, LWA) OAuth Connection

### How the Amazon Connection Works
1. **User Initiation**: The user navigates to `/connect-amazon` and clicks "Connect with Amazon Ads".
2. **State Generation (`POST /amazon-ads/connect`)**:
   - Backend generates a cryptographically secure, random 32-byte one-time `state` token.
   - The state hash (SHA-256) is persisted in `amazon_oauth_states` with a 10-minute TTL.
   - Backend constructs the regional authorization URL pointing to `https://eu.account.amazon.com/ap/oa` (for India / EU region) with parameters: `client_id`, `scope=advertising::campaign_management`, `response_type=code`, `redirect_uri`, and `state`.
3. **User Consent**: The user is redirected to Amazon's consent dialog to authorize AdsManager for Sponsored Products management.
4. **Direct Browser Callback (`GET /amazon-ads/callback`)**:
   - After approval, Amazon redirects the user's browser directly to the backend callback endpoint:
     `https://api.netutechno.com/api/v1/amazon-ads/callback?code=...&state=...`
   - The backend validates the `state` against stored active states. Once validated, the state is immediately marked as consumed (`used_at = now()`) to prevent replay attacks.
   - The backend exchanges the one-time `code` with Amazon's EU token endpoint (`https://api.amazon.co.uk/auth/o2/token`) for access and refresh tokens.
   - Refresh tokens are encrypted at rest using AES-128 Fernet via `TOKEN_ENCRYPTION_KEY` and saved in `amazon_connections`. Raw refresh tokens are never returned or logged.
   - Backend fetches available advertising profiles from `https://advertising-api-eu.amazon.com/v2/profiles` and records them in `amazon_profiles`.
   - Backend issues an HTTP 302 Found redirect back to the frontend:
     - On success: `${FRONTEND_BASE_URL}/connect-amazon?status=success`
     - On denial / error: `${FRONTEND_BASE_URL}/connect-amazon?status=error&reason=<error_code>`
5. **Profile Selection (`POST /amazon-ads/accounts/select`)**:
   - The user selects their India advertising profile (`IN` / INR).
   - Backend activates the connection and links the profile to `ads_accounts`.

### Exact Callback Address (Allowed Return URL)
The exact callback URL to register in Login with Amazon is:
```
https://api.netutechno.com/api/v1/amazon-ads/callback
```
> **IMPORTANT**: The URL registered in the Amazon Security Profile must match `AMAZON_LWA_REDIRECT_URI` character-for-character (including protocol `https://` and exact path). Any mismatch causes an Amazon `redirect_uri mismatch` error.

### Amazon Developer Console Setup Steps
1. **Sign In**: Go to the [Amazon Developer Console](https://developer.amazon.com) and navigate to **Login with Amazon** > **Security Profiles**.
2. **Configure Web Settings**:
   - Select your Security Profile and open **Web Settings**.
   - In **Allowed Return URLs**, add:
     `https://api.netutechno.com/api/v1/amazon-ads/callback`
   - In **Privacy Notice URL**, enter your live public privacy policy:
     `https://<your-frontend-domain>/privacy-policy`
3. **Link to Approved Ads API Application**:
   - Open your Amazon Ads API approval email and click the linkage URL.
   - Select the configured Login with Amazon Security Profile to bind it to your approved Amazon Advertising API access.
4. **Deploy Secrets to Server Environment**:
   - Copy Client ID and Client Secret from the Security Profile into your server's private environment variables (`AMAZON_LWA_CLIENT_ID` and `AMAZON_LWA_CLIENT_SECRET`). Never check these credentials into source control.

### Required Environment Variable Names (No Values)
- `AMAZON_LWA_CLIENT_ID`
- `AMAZON_LWA_CLIENT_SECRET`
- `AMAZON_LWA_REDIRECT_URI`
- `AMAZON_ADS_REGION`
- `AMAZON_LWA_SCOPE`
- `FRONTEND_BASE_URL`
- `TOKEN_ENCRYPTION_KEY`
- `AMAZON_OAUTH_STATE_TTL_MINUTES`

### Deprecation Notice for Legacy Routes
The legacy endpoints:
- `GET /ads-accounts/amazon/authorize-url`
- `POST /ads-accounts/amazon/callback`

are marked **DEPRECATED**. They are preserved temporarily to ensure existing connected accounts continue to operate without disruption. These legacy routes will be permanently removed in a future release following successful production verification of the new `/amazon-ads/*` flow.

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
