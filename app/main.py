"""
FastAPI application entry point.

Mounts:
  - Slack Bolt ASGI handler at /slack/events + /slack/install + /slack/oauth_redirect
  - FastAPI router for all other routes
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slack_bolt.adapter.fastapi import SlackRequestHandler

from app.config import get_settings
from app.database import create_tables
from app.api.routes import router
from app.slack.app import bolt_app

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

# Mount Slack Bolt
slack_handler = SlackRequestHandler(bolt_app)


@app.post("/slack/events")
async def slack_events(req):
    return await slack_handler.handle(req)


@app.get("/slack/install")
async def slack_install(req):
    return await slack_handler.handle(req)


@app.get("/slack/oauth_redirect")
async def slack_oauth_redirect(req):
    return await slack_handler.handle(req)


# Mount API routes
app.include_router(router)


@app.on_event("startup")
async def on_startup():
    logger.info("Starting Engineering Operations Manager...")
    await create_tables()
    logger.info("Database tables ready.")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutting down...")
