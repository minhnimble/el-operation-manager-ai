"""
Slack Bolt app factory.
Registers all event handlers and slash commands.
"""

from slack_bolt import App
from slack_bolt.oauth.oauth_settings import OAuthSettings

from app.config import get_settings
from app.slack.installation_store import DBInstallationStore

settings = get_settings()

oauth_settings = OAuthSettings(
    client_id=settings.slack_client_id,
    client_secret=settings.slack_client_secret,
    scopes=[
        "channels:history",
        "channels:read",
        "users:read",
        "users:read.email",
        "commands",
        "chat:write",
    ],
    user_scopes=[],
    installation_store=DBInstallationStore(),
)

bolt_app = App(
    signing_secret=settings.slack_signing_secret,
    oauth_settings=oauth_settings,
)

# Register handlers (imported for side effects)
from app.slack import events  # noqa: E402, F401
from app.slack import commands  # noqa: E402, F401
