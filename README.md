# Engineering Operations Manager

An engineering leadership intelligence layer that turns Slack + GitHub activity into structured work analytics.

Built for Engineering Managers, Tech Leads, and CTOs who want to reduce manual status tracking and surface collaboration patterns automatically.

---

## What It Does

- Captures standup messages and channel activity from Slack
- Pulls commits, PRs, reviews, and issues from GitHub
- Normalizes everything into a unified `WorkUnit` model
- Generates structured work reports via `/work-report` slash command
- Uses Claude AI to classify work items and produce leadership insights

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Database | PostgreSQL (SQLAlchemy async) |
| Cache / Queue | Redis + Celery |
| Slack integration | Slack Bolt for Python |
| GitHub integration | GitHub REST API v3 |
| AI | Anthropic Claude API |
| Migrations | Alembic |

---

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Node.js 18+ (for Claude Code hooks only)
- A Slack app with the required scopes (see below)
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
# Slack — from api.slack.com/apps
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...
SLACK_SIGNING_SECRET=...

# GitHub — from github.com/settings/developers
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...

# Anthropic — from console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Base URL (use ngrok URL when developing locally)
APP_BASE_URL=https://your-ngrok-url.ngrok.io
```

### 3. Start infrastructure

```bash
make up
```

This starts PostgreSQL (port 5432) and Redis (port 6379) via Docker Compose.

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

### Start everything (3 terminals)

**Terminal 1 — API server**
```bash
make dev
```

**Terminal 2 — Celery worker** (background job processing)
```bash
make worker
```

**Terminal 3 — Celery beat** (scheduled tasks: nightly GitHub sync, normalization)
```bash
make beat
```

Or run all services together with Docker Compose:

```bash
docker-compose up
```

The API will be available at `http://localhost:8000`.
Interactive API docs at `http://localhost:8000/docs`.

---

## Slack App Setup

### Required Bot Token Scopes

Go to **api.slack.com/apps** → your app → **OAuth & Permissions** and add:

```
channels:history
channels:read
users:read
users:read.email
commands
chat:write
```

### Event Subscriptions

Enable Events and set the Request URL to:
```
https://your-app-url/slack/events
```

Subscribe to these bot events:
- `message.channels`
- `app_home_opened`

### Slash Commands

Create these slash commands pointing to `https://your-app-url/slack/events`:

| Command | Description |
|---|---|
| `/work-report` | Generate a work report for a user |
| `/link-github` | Link your GitHub account |
| `/backfill` | Backfill channel history (admin) |

### OAuth Redirect URL

Set the redirect URL to:
```
https://your-app-url/slack/oauth_redirect
```

### Install the App

Visit `https://your-app-url/slack/install` in your browser to install the app to your workspace.

---

## GitHub OAuth App Setup

Go to **github.com/settings/developers** → **OAuth Apps** → **New OAuth App**:

- **Homepage URL**: `https://your-app-url`
- **Authorization callback URL**: `https://your-app-url/auth/github/callback`

Copy the Client ID and Client Secret into `.env`.

---

## Local Development with ngrok

Slack requires a public HTTPS URL to deliver events. Use ngrok to expose your local server:

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok.io` URL and set it as `APP_BASE_URL` in `.env`, and update all Slack app URLs in the developer portal.

---

## Usage

### Link GitHub

In Slack, run:
```
/link-github
```
Click the link, authorize GitHub access. Your commits and PRs will sync automatically.

### Generate a Work Report

```
/work-report                          # your own report, last 7 days
/work-report last-month               # your own report, last 30 days
/work-report @alice                   # another user, last 7 days
/work-report @alice 2024-01-01:2024-01-31   # specific date range
```

### Backfill Channel History

To import historical Slack messages (up to 30 days back):
```
/backfill
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/api/work-report` | Generate report (JSON) |
| `GET` | `/api/users?team_id=...` | List opted-in users |
| `POST` | `/api/sync/github/{user_id}` | Trigger GitHub sync |
| `POST` | `/api/backfill/{team_id}` | Trigger channel backfill |
| `GET` | `/auth/github/callback` | GitHub OAuth callback |
| `GET` | `/slack/install` | Slack OAuth install |
| `GET` | `/slack/oauth_redirect` | Slack OAuth callback |

---

## Running Tests

```bash
# Requires a running PostgreSQL instance at localhost:5432
make test
```

Tests use a separate `el_ops_test` database. Make sure it exists:
```bash
psql -U postgres -c "CREATE DATABASE el_ops_test;"
```

---

## Project Structure

```
app/
├── main.py                  # FastAPI app entry point
├── config.py                # Settings (pydantic-settings)
├── database.py              # SQLAlchemy async engine + session
├── models/                  # Database models
│   ├── user.py              # User + UserGitHubLink
│   ├── installation.py      # SlackInstallation (OAuth tokens)
│   ├── work_unit.py         # WorkUnit — core normalized abstraction
│   └── raw_data.py          # SlackMessage, GitHubActivity (raw)
├── ingestion/               # Layer 1: raw data collection
│   ├── slack_ingester.py    # Slack conversations.history backfill
│   └── github_ingester.py   # GitHub REST API pulls
├── normalization/           # Layer 2: raw → WorkUnit
│   └── normalizer.py
├── analytics/               # Layer 3: aggregation + reporting
│   └── report_builder.py
├── ai/                      # AI pipeline (Claude)
│   ├── schemas.py           # Pydantic output schemas
│   ├── work_extractor.py    # Standup → structured work items
│   └── insight_generator.py # WorkReport → leadership insights
├── slack/                   # Slack Bolt handlers
│   ├── app.py               # Bolt app factory + OAuth settings
│   ├── events.py            # Event handlers (message, app_home)
│   ├── commands.py          # Slash commands
│   └── installation_store.py # DB-backed installation store
├── github/
│   └── oauth.py             # GitHub OAuth exchange + user linking
├── api/
│   └── routes.py            # FastAPI route definitions
└── tasks/                   # Celery background tasks
    ├── celery_app.py        # Celery app + beat schedule
    ├── ingestion_tasks.py   # Backfill + GitHub sync tasks
    └── normalization_tasks.py
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

- Only public channels are tracked by default
- Users must opt in via `/link-github`
- Private DMs are never captured
- Data is scoped to your workspace only
