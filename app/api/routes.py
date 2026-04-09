"""
FastAPI routes.

Endpoints:
  GET  /health
  GET  /auth/slack              — Slack OAuth start (Sign in with Slack)
  GET  /auth/slack/callback     — Slack OAuth callback
  GET  /auth/github             — GitHub OAuth start
  GET  /auth/github/callback    — GitHub OAuth callback
  POST /api/work-report         — generate a work report (JSON)
  POST /api/sync/slack/{user}   — trigger Slack backfill for a user
  POST /api/sync/github/{user}  — trigger GitHub sync for a user
  GET  /api/users               — list users for a team
"""

import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.config import get_settings
from app.models.user import User
from app.models.slack_token import SlackUserToken
from app.slack.oauth import build_auth_url, exchange_code, save_slack_token
from app.github.oauth import link_github_to_user
from app.analytics.report_builder import build_work_report
from app.tasks.ingestion_tasks import trigger_backfill, trigger_github_sync

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


# ─── Health ──────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ─── Slack OAuth ─────────────────────────────────────────────────────────────

@router.get("/auth/slack")
async def slack_auth_start(team_id: str = Query(default="")):
    """Redirect user to Slack authorization page."""
    state = f"{team_id}:{secrets.token_urlsafe(16)}"
    return RedirectResponse(url=build_auth_url(state=state))


@router.get("/auth/slack/callback")
async def slack_auth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Slack redirects here after user authorizes."""
    try:
        token_data = await exchange_code(code)
        token_record = await save_slack_token(db, token_data)
        return JSONResponse({
            "message": "Slack account connected successfully.",
            "slack_user_id": token_record.slack_user_id,
            "team": token_record.slack_team_name,
        })
    except Exception as e:
        logger.exception("Slack OAuth callback failed")
        raise HTTPException(status_code=400, detail=str(e))


# ─── GitHub OAuth ─────────────────────────────────────────────────────────────

@router.get("/auth/github")
async def github_auth_start(
    slack_user_id: str = Query(...),
    slack_team_id: str = Query(...),
):
    """Redirect user to GitHub authorization page."""
    state = f"{slack_team_id}:{slack_user_id}"
    github_oauth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.github_client_id}"
        f"&scope=read:user,repo"
        f"&state={state}"
        f"&redirect_uri={settings.app_base_url}/auth/github/callback"
    )
    return RedirectResponse(url=github_oauth_url)


@router.get("/auth/github/callback")
async def github_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        parts = state.split(":")
        if len(parts) != 2:
            raise ValueError("Invalid state parameter")
        slack_team_id, slack_user_id = parts

        link = await link_github_to_user(
            db=db,
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            code=code,
        )

        trigger_github_sync.delay(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            days_back=30,
        )

        return JSONResponse({
            "message": f"GitHub account @{link.github_login} linked successfully.",
            "github_login": link.github_login,
        })
    except Exception as e:
        logger.exception("GitHub OAuth callback failed")
        raise HTTPException(status_code=400, detail=str(e))


# ─── API ──────────────────────────────────────────────────────────────────────

class WorkReportRequest(BaseModel):
    slack_user_id: str
    slack_team_id: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    days_back: int = 7
    include_ai: bool = True


@router.post("/api/work-report")
async def api_work_report(
    req: WorkReportRequest,
    db: AsyncSession = Depends(get_db),
):
    end = req.end_date or datetime.utcnow()
    start = req.start_date or (end - timedelta(days=req.days_back))

    report = await build_work_report(
        db=db,
        slack_user_id=req.slack_user_id,
        slack_team_id=req.slack_team_id,
        start_date=start,
        end_date=end,
        include_ai=req.include_ai,
    )
    return report.model_dump()


@router.get("/api/users")
async def list_users(
    team_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(
            User.slack_team_id == team_id,
            User.opted_in == True,  # noqa: E712
        )
    )
    users = result.scalars().all()
    return [
        {
            "slack_user_id": u.slack_user_id,
            "display_name": u.slack_display_name or u.slack_real_name,
            "github_linked": u.github_link is not None,
        }
        for u in users
    ]


@router.post("/api/sync/slack/{slack_user_id}")
async def sync_slack(
    slack_user_id: str,
    team_id: str = Query(...),
    days_back: int = Query(30),
):
    """Trigger a Slack backfill for a single user's joined channels."""
    trigger_backfill.delay(
        slack_user_id=slack_user_id,
        team_id=team_id,
        days_back=days_back,
    )
    return {"queued": True, "user": slack_user_id}


@router.post("/api/sync/github/{slack_user_id}")
async def sync_github(
    slack_user_id: str,
    team_id: str = Query(...),
    days_back: int = Query(30),
):
    trigger_github_sync.delay(
        slack_user_id=slack_user_id,
        slack_team_id=team_id,
        days_back=days_back,
    )
    return {"queued": True, "user": slack_user_id}
