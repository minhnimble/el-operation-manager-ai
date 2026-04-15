"""
Slack workspace user listing.

Uses the EM's user token (channels:read + users:read scopes) to enumerate
all non-bot, non-deleted workspace members so the EM can pick team members
from a list instead of typing Slack user IDs manually.
"""

import logging
from app.ingestion.slack_ingester import SlackIngester

logger = logging.getLogger(__name__)


async def list_workspace_users(ingester: SlackIngester) -> list[dict]:
    """Return all active, non-bot workspace members.

    Each item is a normalized dict with keys:
        slack_user_id, display_name, real_name, avatar_url
    """
    members: list[dict] = []
    cursor = None

    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor

        data = await ingester._get("users.list", params)

        for member in data.get("members", []):
            # Skip bots, deleted accounts, and the Slack bot itself
            if member.get("deleted") or member.get("is_bot"):
                continue
            if member.get("id") == "USLACKBOT":
                continue

            profile = member.get("profile", {})
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or member.get("name", "")
            ).strip()

            members.append({
                "slack_user_id": member["id"],
                "display_name": display_name or member.get("name", member["id"]),
                "real_name": profile.get("real_name", ""),
                "email": profile.get("email", ""),
                "avatar_url": profile.get("image_48", ""),
            })

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Sort alphabetically by display name
    members.sort(key=lambda m: m["display_name"].lower())
    logger.info("Listed %d workspace users", len(members))
    return members
