from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
import logging

logging.basicConfig(level=logging.INFO)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title="Amazon Ads Management Platform API", lifespan=lifespan)

import os

frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi import Request
from fastapi.responses import JSONResponse
import traceback

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.error(f"Unhandled error on {request.method} {request.url}:\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )

from app.routers import auth, ads_accounts, campaigns, metrics, ai, billing, ad_groups, keywords, search_terms, automation_rules

app.include_router(auth.router)
app.include_router(ads_accounts.router)
app.include_router(campaigns.router)
app.include_router(metrics.router)
app.include_router(ai.router)
app.include_router(billing.router)
app.include_router(ad_groups.router)
app.include_router(keywords.router)
app.include_router(search_terms.router)
app.include_router(automation_rules.router)
# Startup event removed

@app.get("/health")
async def health_check():
    return {"status": "ok"}
