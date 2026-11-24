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

from app.config import settings

origins = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]

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

from fastapi.exceptions import RequestValidationError

from fastapi import HTTPException
from app.routers.amazon_ads import AmazonAdsException

@app.exception_handler(AmazonAdsException)
async def amazon_ads_exception_handler(request: Request, exc: AmazonAdsException):
    headers = {}
    origin = request.headers.get("origin")
    if origin in origins:
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.message, "code": exc.code},
        headers=headers,
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    headers = {}
    origin = request.headers.get("origin")
    if origin in origins:
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    path = request.url.path
    if path.startswith("/amazon-ads") or path.startswith("/api/v1/amazon-ads"):
        code = "UNAUTHORIZED" if exc.status_code in (401, 403) else (
            "VALIDATION_ERROR" if exc.status_code in (400, 422) else (
                "NOT_CONNECTED" if exc.status_code == 409 else "INTERNAL_ERROR"
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "message": str(exc.detail), "code": code},
            headers=headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers,
    )

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        loc = err.get("loc", [])
        field = ".".join(str(x) for x in loc[1:]) if len(loc) > 1 else str(loc[0]) if loc else ""
        errors.append({"field": field, "message": str(err.get("msg", "Invalid value"))})
    headers = {}
    origin = request.headers.get("origin")
    if origin in origins:
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    path = request.url.path
    if path.startswith("/amazon-ads") or path.startswith("/api/v1/amazon-ads"):
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid request parameters", "code": "VALIDATION_ERROR"},
            headers=headers,
        )
    return JSONResponse(
        status_code=422,
        content={"detail": errors},
        headers=headers
    )

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

from app.routers import auth, ads_accounts, hourly, campaigns, ad_groups, keywords, search_terms, products, recommendation_rules, recommendations, contact, amazon_ads

app.include_router(auth.router)
app.include_router(ads_accounts.router)
app.include_router(hourly.router)
app.include_router(campaigns.router)
app.include_router(ad_groups.router)
app.include_router(keywords.router)
app.include_router(search_terms.router)
app.include_router(products.router)
app.include_router(recommendation_rules.router)
app.include_router(recommendations.router)
app.include_router(contact.router)
app.include_router(amazon_ads.router, prefix="/amazon-ads")
app.include_router(amazon_ads.router, prefix="/api/v1/amazon-ads")
# Startup event removed

@app.get("/health")
async def health_check():
    return {"status": "ok"}
