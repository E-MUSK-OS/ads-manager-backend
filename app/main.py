from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
import logging

logging.basicConfig(level=logging.INFO)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Amazon Ads Management Platform API", lifespan=lifespan)

import os

origins = [o.strip() for o in os.getenv("FRONTEND_URL", "http://localhost:3000").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi import Request
from fastapi.responses import JSONResponse
import traceback
from app.core.exceptions import ServiceValidationError

@app.exception_handler(ServiceValidationError)
async def service_validation_exception_handler(request: Request, exc: ServiceValidationError):
    headers = {}
    origin = request.headers.get("origin")
    if origin in origins:
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors},
        headers=headers
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.error("Unhandled error on %s %s\n%s", request.method, request.url, traceback.format_exc())
    headers = {}
    origin = request.headers.get("origin")
    if origin in origins:
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=headers
    )

from app.routers import auth, ads_accounts, campaigns, metrics, ai, billing, ad_groups, keywords, search_terms, automation_rules, products

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
app.include_router(products.router)
# Startup event removed

@app.get("/health")
async def health_check():
    return {"status": "ok"}
