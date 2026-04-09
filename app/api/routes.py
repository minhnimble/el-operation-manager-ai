"""
FastAPI routes.

Endpoints:
  GET  /health
  GET  /slack/install        — Slack OAuth start
  GET  /slack/oauth_redirect — Slack OAuth callback
  GET  /auth/github/callback — GitHub OAuth callback
  POST /api/work-report      — programmatic report generation
  GET  /api/users            — list opted-in users for a team
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.config import get_settings
from app.models.user import User, UserGitHubLink
from app.github.oauth import link_github_to_user
from app.analytics.report_builder import build_work_report
from app.tasks.ingestion_tasks import trigger_github_sync, trigger_backfill

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


# ─── Health ─────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ─── GitHub OAuth ────────────────────────────────────────────────────────────

@router.get("/auth/github/callback")
async def github_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """GitHub sends user here after authorization."""
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

        # Kick off initial GitHub sync
        trigger_github_sync.delay(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            days_back=30,
        )

        return JSONResponse({
            "message": f"GitHub account @{link.github_login} linked successfully!",
            "github_login": link.github_login,
        })
    except Exception as e:
        logger.exception("GitHub OAuth callback failed")
        raise HTTPException(status_code=400, detail=str(e))


# ─── API ─────────────────────────────────────────────────────────────────────

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


@router.post("/api/backfill/{team_id}")
async def backfill(team_id: str, days_back: int = Query(30)):
    trigger_backfill.delay(team_id=team_id, requested_by="api", days_back=days_back)
    return {"queued": True, "team_id": team_id}
