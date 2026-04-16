"""
Step 1 of AI pipeline — Work Extraction.

Converts raw standup text → structured work items with categories.
Uses a focused prompt; does NOT try to generate insights here.
"""

import json
import logging

import anthropic

from app.config import get_settings
from app.ai.schemas import StandupExtraction, ExtractedWorkItem

logger = logging.getLogger(__name__)
settings = get_settings()

EXTRACTION_SYSTEM = """You are a work extraction engine for an engineering intelligence tool.

Your job: parse a software engineer's standup message and extract structured work items.

Rules:
- Each work item should be a distinct piece of work
- Categories: feature, bug_fix, architecture, mentorship, incident, review, documentation, planning, operational, unknown
- Confidence 0.0–1.0 based on how clearly the category is indicated
- Blockers are explicit things blocking progress
- Be concise; titles ≤ 10 words
- Output ONLY valid JSON matching the schema"""

EXTRACTION_USER_TEMPLATE = """Parse this standup message into structured work items.

Standup text:
{text}

Return JSON with this exact structure:
{{
  "work_items": [
    {{"title": "...", "category": "...", "description": "...", "confidence": 0.9}}
  ],
  "blockers": ["..."],
  "raw_standup_text": "{text_escaped}"
}}"""


class WorkExtractor:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def extract_from_standup(self, standup_text: str) -> StandupExtraction:
        """Synchronous extraction — call from Celery worker."""
        if not standup_text.strip():
            return StandupExtraction(work_items=[], blockers=[], raw_standup_text="")

        text_escaped = standup_text.replace('"', '\\"').replace("\n", "\\n")
        prompt = EXTRACTION_USER_TEMPLATE.format(
            text=standup_text, text_escaped=text_escaped
        )

        try:
            response = self._client.messages.create(
                model=settings.anthropic_model,
                max_tokens=1024,
                system=EXTRACTION_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            return StandupExtraction(
                work_items=[ExtractedWorkItem(**item) for item in data.get("work_items", [])],
                blockers=data.get("blockers", []),
                raw_standup_text=standup_text,
            )
        except Exception as e:
            logger.warning("Work extraction failed: %s", e)
            _raise_if_billing(e)
            return StandupExtraction(
                work_items=[], blockers=[], raw_standup_text=standup_text
            )

    def batch_extract(self, standup_texts: list[str]) -> list[StandupExtraction]:
        return [self.extract_from_standup(t) for t in standup_texts]


def _raise_if_billing(exc: Exception) -> None:
    """Re-raise the exception as AIBillingError if it's an Anthropic billing issue."""
    msg = str(exc).lower()
    if "credit balance" in msg or "too low" in msg or "payment" in msg or "billing" in msg:
        raise AIBillingError() from exc


class AIBillingError(RuntimeError):
    """Raised when Anthropic rejects the request due to insufficient credits."""
    def __str__(self) -> str:
        return (
            "Anthropic API credits exhausted. "
            "Go to console.anthropic.com → Plans & Billing to top up, "
            "then re-generate the report."
        )
