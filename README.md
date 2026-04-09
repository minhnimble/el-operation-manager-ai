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
# Slack — from api.slack.com/apps
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...

# GitHub — from github.com/settings/developers
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...

# Anthropic — from console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Your app's public base URL
# For local development use your ngrok URL (see below)
APP_BASE_URL=https://your-ngrok-url.ngrok.io
```

> `SLACK_SIGNING_SECRET` is not required — this app does not receive Slack webhook events.

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

**Terminal 1 — API server**
```bash
make dev
```

**Terminal 2 — Celery worker** (processes background sync jobs)
```bash
make worker
```

**Terminal 3 — Celery beat** (nightly scheduled syncs)
```bash
make beat
```

Or run everything together with Docker Compose:

```bash
docker-compose up
```

The API will be available at `http://localhost:8000`.
Interactive API docs at `http://localhost:8000/docs`.

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

## Local Development with ngrok

OAuth callbacks require a public HTTPS URL. Use ngrok to expose your local server:

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok.io` URL and:
1. Set it as `APP_BASE_URL` in `.env`
2. Update the redirect URL in your Slack app settings
3. Update the callback URL in your GitHub OAuth app settings

---

## Usage

### 1. Connect Slack

Visit this URL in your browser to authorize Slack access:

```
http://localhost:8000/auth/slack
```

Authorize the app. Your user token is stored and used to pull your joined channel messages.

### 2. Connect GitHub

After connecting Slack, link your GitHub account by visiting:

```
http://localhost:8000/auth/github?slack_user_id=UXXXXXXX&slack_team_id=TXXXXXXX
```

Replace `UXXXXXXX` and `TXXXXXXX` with your Slack user ID and team ID (visible in the response from step 1). An initial 30-day sync will kick off automatically in the background.

### 3. Backfill Slack Channel History

Trigger a backfill of all your joined public channels (up to 30 days):

```bash
curl -X POST "http://localhost:8000/api/sync/slack/UXXXXXXX?team_id=TXXXXXXX&days_back=30"
```

### 4. Generate a Work Report

```bash
curl -X POST http://localhost:8000/api/work-report \
  -H "Content-Type: application/json" \
  -d '{
    "slack_user_id": "UXXXXXXX",
    "slack_team_id": "TXXXXXXX",
    "days_back": 7,
    "include_ai": true
  }'
```

Or use the interactive docs at `http://localhost:8000/docs`.

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
