"""
FastAPI application entry point.

No Slack bot required — uses Sign in with Slack (user OAuth) to
query Slack data on behalf of authenticated users.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import create_tables
from app.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="Engineering Operations Manager",
    description="Slack + GitHub activity intelligence for engineering leaders",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
async def on_startup():
    logger.info("Starting Engineering Operations Manager...")
    await create_tables()
    logger.info("Database tables ready.")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutting down...")
