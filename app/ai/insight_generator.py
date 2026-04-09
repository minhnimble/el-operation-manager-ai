"""
Step 3 of AI pipeline — Insight Generation.

Takes a compiled WorkReport and produces leadership-level insights.
Separate from extraction to keep prompts focused and costs controlled.
"""

import json
import logging

import anthropic

from app.config import get_settings
from app.ai.schemas import WorkReport, InsightResult

logger = logging.getLogger(__name__)
settings = get_settings()

INSIGHT_SYSTEM = """You are an engineering leadership intelligence assistant.

You help Engineering Managers and Tech Leads understand their team's work patterns.
You are direct, constructive, and data-driven. You do NOT police performance —
you surface patterns to help leaders support their engineers better.

Output ONLY valid JSON. No markdown, no preamble."""

INSIGHT_USER_TEMPLATE = """Generate leadership insights from this engineer's work report.

Engineer: {name}
Period: {date_range}

GitHub Activity:
- Commits: {commits}
- PRs Opened: {prs_opened}
- PRs Merged: {prs_merged}
- PR Reviews Given: {pr_reviews}

Slack Activity:
- Standups posted: {standup_count}
- Discussion messages: {discussion_messages}

AI-Classified Work (from standups):
- Feature work: {feature_work}
- Bug fixes: {bug_fixes}
- Architecture/design: {architecture_work}
- Mentorship/support: {mentorship}
- Incidents: {incidents}

Recent standup samples:
{standup_samples}

Return JSON with this structure:
{{
  "summary": "2-3 sentence overview of this engineer's contribution this period",
  "highlights": ["positive signal 1", "positive signal 2", "positive signal 3"],
  "watch_items": ["item worth EM attention 1", "item 2"],
  "standup_vs_github_alignment": "brief note on whether declared work matches commits/PRs"
}}"""


class InsightGenerator:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def generate(self, report: WorkReport) -> InsightResult:
        standup_samples = "\n".join(
            f"- {s[:200]}" for s in report.recent_standups[:5]
        ) or "(no standups found)"

        prompt = INSIGHT_USER_TEMPLATE.format(
            name=report.user_display_name,
            date_range=report.date_range,
            commits=report.commits,
            prs_opened=report.prs_opened,
            prs_merged=report.prs_merged,
            pr_reviews=report.pr_reviews,
            standup_count=report.standup_count,
            discussion_messages=report.discussion_messages,
            feature_work=report.feature_work,
            bug_fixes=report.bug_fixes,
            architecture_work=report.architecture_work,
            mentorship=report.mentorship,
            incidents=report.incidents,
            standup_samples=standup_samples,
        )

        try:
            response = self._client.messages.create(
                model=settings.anthropic_model,
                max_tokens=1024,
                system=INSIGHT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            return InsightResult(**data)
        except Exception as e:
            logger.warning("Insight generation failed: %s", e)
            return InsightResult(
                summary="Insight generation unavailable.",
                highlights=[],
                watch_items=[],
                standup_vs_github_alignment="Analysis unavailable.",
            )
