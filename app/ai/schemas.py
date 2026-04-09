"""Pydantic schemas for AI-structured output."""

from pydantic import BaseModel, Field
from typing import Literal


class ExtractedWorkItem(BaseModel):
    title: str = Field(description="Short title for the work item")
    category: Literal[
        "feature", "bug_fix", "architecture", "mentorship",
        "incident", "review", "documentation", "planning",
        "operational", "unknown"
    ]
    description: str = Field(description="1-2 sentence description")
    confidence: float = Field(ge=0.0, le=1.0)


class StandupExtraction(BaseModel):
    work_items: list[ExtractedWorkItem]
    blockers: list[str] = Field(default_factory=list)
    raw_standup_text: str


class WorkReport(BaseModel):
    user_display_name: str
    date_range: str
    # GitHub metrics
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    pr_reviews: int = 0
    issues_opened: int = 0
    # Slack metrics
    standup_count: int = 0
    discussion_messages: int = 0
    thread_replies: int = 0
    # AI-extracted
    feature_work: int = 0
    bug_fixes: int = 0
    architecture_work: int = 0
    mentorship: int = 0
    incidents: int = 0
    # Summaries
    standup_summary: str = ""
    ai_insights: str = ""
    # Raw standups for display
    recent_standups: list[str] = Field(default_factory=list)


class InsightResult(BaseModel):
    summary: str = Field(description="2-3 sentence leadership summary")
    highlights: list[str] = Field(description="Top 3 positive signals")
    watch_items: list[str] = Field(description="Items worth an EM's attention")
    standup_vs_github_alignment: str = Field(
        description="Brief note on whether declared work matches GitHub activity"
    )
