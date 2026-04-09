# Engineering Operations Manager

An engineering leadership intelligence layer that turns Slack + GitHub activity into structured work analytics.

Built for Engineering Managers, Tech Leads, and CTOs who want to reduce manual status tracking and surface collaboration patterns automatically.

---

## What It Does

- Connects to Slack via **Sign in with Slack** (user OAuth) — no bot app required
- Pulls standup messages and channel activity from channels you're a member of
- Pulls commits, PRs, reviews, and issues from GitHub
- Normalizes everything into a unified `WorkUnit` model
- Generates structured work reports via a REST API
- Uses Claude AI to classify work items and produce leadership insights

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Database | PostgreSQL (SQLAlchemy async) |
| Cache / Queue | Redis + Celery |
| Slack integration | Slack SDK — Sign in with Slack (user OAuth) |
| GitHub integration | GitHub REST API v3 (user OAuth) |
| AI | Anthropic Claude API |
| Migrations | Alembic |

---

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- A Slack OAuth app (see setup below — no bot or event subscriptions needed)
- A GitHub OAuth app
- An Anthropic API key

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd el-operation-manager-ai
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in:

```env
# Slack OAuth app (Sign in with Slack — no bot, no event subscriptions needed)
# Create at api.slack.com/apps → OAuth & Permissions → User Token Scopes
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...

# GitHub OAuth app — github.com/settings/developers
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...

# Anthropic — console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Your Streamlit Cloud app URL (e.g. https://yourapp.streamlit.app)
APP_BASE_URL=https://yourapp.streamlit.app
```

### 3. Start infrastructure

```bash
make up
```

Starts PostgreSQL on port `5432` and Redis on port `6379` via Docker Compose.

### 4. Run database migrations

```bash
make migrate
```

### 5. (Optional) Seed development data

Populates the database with 3 fake users and 30 days of Slack + GitHub activity for local testing.

```bash
make seed
```

---

## Running the App

Open 3 terminals:

**Terminal 1 — Streamlit UI**
```bash
make dev
# opens at http://localhost:8501
```

**Terminal 2 — Celery worker** (processes background sync jobs)
```bash
make worker
```

**Terminal 3 — Celery beat** (nightly scheduled syncs)
```bash
make beat
```

> The FastAPI backend (`make api`) is optional — only needed if you want the raw REST API alongside the UI.

---

## Slack App Setup

This app uses **Sign in with Slack** — there is no bot, no event subscriptions, and no real-time webhook handling. Users authorize the app once and their token is used to pull channel history on demand.

### 1. Create a Slack App

Go to **[api.slack.com/apps](https://api.slack.com/apps)** → **Create New App** → **From Scratch**.

### 2. Add User Token Scopes

Go to **OAuth & Permissions** → scroll to **User Token Scopes** and add:

```
channels:history
channels:read
users:read
users:read.email
identity.basic
identity.email
```

> These are **User Token Scopes**, not Bot Token Scopes.

### 3. Add Redirect URL

Under **OAuth & Permissions → Redirect URLs**, add:

```
https://your-app-url/auth/slack/callback
```

### 4. Copy credentials to `.env`

From the **Basic Information** page, copy:
- `App ID` → not needed
- `Client ID` → `SLACK_CLIENT_ID`
- `Client Secret` → `SLACK_CLIENT_SECRET`

---

## GitHub OAuth App Setup

Go to **[github.com/settings/developers](https://github.com/settings/developers)** → **OAuth Apps** → **New OAuth App**:

| Field | Value |
|---|---|
| Homepage URL | `https://your-app-url` |
| Authorization callback URL | `https://your-app-url/auth/github/callback` |

Copy the **Client ID** and **Client Secret** into `.env`.

---

## Deploying to Streamlit Cloud

Deploy to **[Streamlit Community Cloud](https://streamlit.io/cloud)** (free) to get a permanent public HTTPS URL. Use that URL as the OAuth callback in your Slack and GitHub app settings.

1. Push the repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app** → select your repo
3. Set **Main file path** to `streamlit_app.py`
4. Under **Settings → Secrets**, paste the contents of `.streamlit/secrets.toml.example` with your values filled in
5. Copy your app URL (e.g. `https://yourapp.streamlit.app`) into:
   - `APP_BASE_URL` in Streamlit secrets
   - Slack app redirect URL
   - GitHub OAuth callback URL

---

## Usage

Open the app at `http://localhost:8501` (or your Streamlit Cloud URL).

### 1. Connect Accounts

Go to **🔗 Connect Accounts** and click **Sign in with Slack**. After authorizing, click **Connect GitHub**. Both flows redirect back to the app automatically.

### 2. Sync Data

Go to **🔄 Sync Data**, set how many days to backfill, and click **Sync Slack** and **Sync GitHub**. Jobs run in the background via Celery — check back in a minute or two.

### 3. Generate a Work Report

Go to **📊 Work Report**, select a team member, choose a date range, and click **Generate Report**. Toggle **AI insights** on to get Claude-powered work classification and leadership summary.

### 4. View Team

Go to **👥 Team Overview** to see all connected users and their Slack/GitHub link status.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/auth/slack` | Start Slack OAuth (Sign in with Slack) |
| `GET` | `/auth/slack/callback` | Slack OAuth callback |
| `GET` | `/auth/github` | Start GitHub OAuth |
| `GET` | `/auth/github/callback` | GitHub OAuth callback |
| `POST` | `/api/work-report` | Generate a work report (JSON) |
| `GET` | `/api/users?team_id=` | List opted-in users for a team |
| `POST` | `/api/sync/slack/{user_id}?team_id=` | Trigger Slack backfill for a user |
| `POST` | `/api/sync/github/{user_id}?team_id=` | Trigger GitHub sync for a user |

---

## Running Tests

Requires a running PostgreSQL instance. Create the test database first:

```bash
psql -U postgres -c "CREATE DATABASE el_ops_test;"
```

Then run:

```bash
make test
```

---

## Project Structure

```
app/
├── main.py                    # FastAPI app entry point
├── config.py                  # Settings (pydantic-settings + .env)
├── database.py                # SQLAlchemy async engine + session
├── models/
│   ├── user.py                # User + UserGitHubLink
│   ├── slack_token.py         # SlackUserToken (per-user OAuth tokens)
│   ├── work_unit.py           # WorkUnit — core normalized abstraction
│   └── raw_data.py            # SlackMessage, GitHubActivity (raw store)
├── ingestion/                 # Layer 1 — raw data collection
│   ├── slack_ingester.py      # Pulls channel history via user token
│   └── github_ingester.py     # Pulls commits, PRs, reviews via user token
├── normalization/             # Layer 2 — raw → WorkUnit
│   └── normalizer.py
├── analytics/                 # Layer 3 — aggregation + reporting
│   └── report_builder.py
├── ai/                        # AI pipeline (Claude)
│   ├── schemas.py             # Pydantic output schemas
│   ├── work_extractor.py      # Standup text → structured work items
│   └── insight_generator.py   # WorkReport → leadership insights
├── slack/
│   └── oauth.py               # Sign in with Slack OAuth flow
├── github/
│   └── oauth.py               # GitHub OAuth exchange + user linking
├── api/
│   └── routes.py              # All FastAPI route definitions
└── tasks/                     # Celery background tasks
    ├── celery_app.py          # Celery app + beat schedule
    ├── ingestion_tasks.py     # Slack backfill + GitHub sync tasks
    └── normalization_tasks.py # Raw → WorkUnit normalization tasks
```

---

## Feature Flags

Control which features are active via `.env`:

```env
ENABLE_AI_EXTRACTION=true      # AI work classification from standups
ENABLE_BURNOUT_DETECTION=false # Phase 4 — not yet implemented
ENABLE_ORG_ANALYTICS=false     # Phase 5 — not yet implemented
```

---

## Privacy

This tool is designed for team transparency, not surveillance:

- Only public channels the user has joined are tracked
- Each user connects their own account — no admin can pull data without user authorization
- Private DMs are never captured
- GitHub access requires explicit user OAuth consent
- Data is scoped to your workspace only
