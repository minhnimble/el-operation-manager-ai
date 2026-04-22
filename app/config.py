from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    app_secret_key: str = "change-me"
    app_base_url: str = "https://localhost:8501"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/el_ops"
    database_pool_size: int = 10

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Slack
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""

    # GitHub — Personal Access Token (PAT).
    #
    # Resolution order at sync time:
    #   1. `github_pat` env/secret (server-wide manager token, this field)
    #   2. Member's own PAT stored in UserGitHubLink (optional fallback)
    #
    # Recommended: set `GITHUB_PAT` in env/secrets. One PAT covers the team.
    # Required scopes: repo + read:org.
    github_pat: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # Google Sheets — Developer Track integration
    # google_sheets_credentials_json: full JSON key for a service account with
    # read access to the sheet. Paste as a single-line JSON string.
    # dev_track_sheet_id: the Google Sheet ID (the part between /d/ and /edit
    # in the sheet URL).
    google_sheets_credentials_json: str = ""
    dev_track_sheet_id: str = ""

    # Notion — Developer Track sync
    # notion_api_key: Internal Integration Secret from Notion → Settings →
    #   Connections → Develop or manage integrations.
    # notion_dev_track_database_id: ID of the Notion database containing one
    #   entry per developer. Copy from the database URL:
    #   notion.so/.../{DATABASE_ID}?v={VIEW_ID}
    # notion_dev_track_view_id (optional): if set, we use Notion's view-query
    #   endpoint to fetch only the pages that match the view's saved filter +
    #   sort (same rows the user sees in that Notion view). Leave blank to
    #   fetch every page in the database. This is the `v=` part of the URL.
    notion_api_key: str = ""
    notion_dev_track_database_id: str = ""
    notion_dev_track_view_id: str = ""

    # Feature flags
    enable_ai_extraction: bool = True
    enable_burnout_detection: bool = False
    enable_org_analytics: bool = False

    # Rate limits
    github_api_requests_per_hour: int = 5000
    slack_api_requests_per_minute: int = 50
    ai_requests_per_minute: int = 20

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
