"""Tests for AI work extractor — uses mocked Anthropic client."""

import json
from unittest.mock import MagicMock, patch

from app.ai.work_extractor import WorkExtractor


MOCK_RESPONSE_JSON = json.dumps({
    "work_items": [
        {
            "title": "Ship login feature",
            "category": "feature",
            "description": "Implemented OAuth login flow with GitHub.",
            "confidence": 0.95,
        },
        {
            "title": "Fix auth token expiry bug",
            "category": "bug_fix",
            "description": "Fixed token expiry not being checked on refresh.",
            "confidence": 0.88,
        },
    ],
    "blockers": ["Waiting for design review on settings page"],
    "raw_standup_text": "Yesterday: shipped login, fixed auth bug. Today: code review. Blocked on design.",
})


def test_extract_from_standup_parses_correctly():
    mock_content = MagicMock()
    mock_content.text = MOCK_RESPONSE_JSON

    mock_response = MagicMock()
    mock_response.content = [mock_content]

    with patch("app.ai.work_extractor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.return_value = mock_client

        extractor = WorkExtractor()
        result = extractor.extract_from_standup(
            "Yesterday: shipped login, fixed auth bug. Today: code review."
        )

    assert len(result.work_items) == 2
    assert result.work_items[0].category == "feature"
    assert result.work_items[1].category == "bug_fix"
    assert len(result.blockers) == 1


def test_extract_handles_empty_input():
    extractor = WorkExtractor()
    result = extractor.extract_from_standup("")
    assert result.work_items == []
    assert result.blockers == []


def test_extract_handles_api_failure():
    with patch("app.ai.work_extractor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")
        mock_anthropic.return_value = mock_client

        extractor = WorkExtractor()
        result = extractor.extract_from_standup("Yesterday: did stuff.")

    assert result.work_items == []
