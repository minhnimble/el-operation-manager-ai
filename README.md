# Engineering Operations Manager

An engineering leadership intelligence layer that turns Slack + GitHub activity into structured work analytics.

Built for Engineering Managers, Tech Leads, and CTOs who want to reduce manual status tracking and surface collaboration patterns automatically.

---

## What It Does

- Connects to Slack via **Sign in with Slack** (user OAuth) — no bot app required
- Pulls messages from **public and private channels** you are a member of
- Syncs **standup messages** from standup bots (Geekbot-style) by resolving the bot's username to a real team member
- For team member syncs, captures only messages **sent by** or **mentioning** that member — not the entire channel history
- Pulls commits, PRs, reviews, and issues from GitHub
- Normalizes everything into a unified `WorkUnit` model
- **Team management** — EM adds team members from the workspace; syncs are scoped to channels the member is actually in
- Generates structured work reports per member with activity feed and a one-click copyable summary
- Uses Claude AI to classify work items and produce leadership insights

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| Database | PostgreSQL (SQLAlchemy async + NullPool) |
| Slack integration | Sign in with Slack — user OAuth (no bot, no webhooks) |
| GitHub integration | GitHub REST API v3 — user OAuth |
| AI | Anthropic Claude API |
| Migrations | Alembic |

> **No Redis or Celery required.** Syncs run directly in the Streamlit session and work on Streamlit Cloud out of the box.

---

## Prerequisites

- Python 3.13+
- A PostgreSQL database (local Docker or Supabase for cloud)
- A Slack OAuth app (see setup below)
- A GitHub OAuth app
- An Anthropic API key

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd el-operation-manager-ai
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in:

```env
# Slack OAuth app (Sign in with Slack — no bot, no event subscriptions needed)
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...

# GitHub OAuth app — github.com/settings/developers
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...

# Anthropic — console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Your Streamlit Cloud app URL (or http://localhost:8501 for local dev)
APP_BASE_URL=https://yourapp.streamlit.app
```

### 3. Start the database

```bash
make up
```

Starts PostgreSQL on port `5432` via Docker Compose.

### 4. Run database migrations

```bash
make migrate
```

---

## Running the App

```bash
make dev
# opens at http://localhost:8501
```

No worker process needed — all syncs run inline in the Streamlit session.

---

## Local vs Streamlit Cloud Config

### Local development

- Config source: `.env` (auto-loaded by `pydantic-settings` in `app/config.py`)
- `secrets.toml` is optional locally
- Local TLS certs live in `.certs/` and are gitignored (`.certs/` should not be committed)
- Recommended `APP_BASE_URL`:

```env
APP_BASE_URL=https://localhost:8501
```

- Run Streamlit with HTTPS when testing OAuth redirects:

```bash
streamlit run streamlit_app.py \
  --server.port 8501 \
  --server.address localhost \
  --server.sslCertFile .certs/localhost.pem \
  --server.sslKeyFile .certs/localhost-key.pem
```

#### Create local HTTPS certs

Preferred (trusted locally):

```bash
brew install mkcert
mkcert -install
mkdir -p .certs
mkcert -key-file .certs/localhost-key.pem -cert-file .certs/localhost.pem localhost 127.0.0.1 ::1
```

Fallback (self-signed):

```bash
mkdir -p .certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout .certs/localhost-key.pem \
  -out .certs/localhost.pem \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1"
```

- Local OAuth redirect URL (Slack + GitHub):

```
https://localhost:8501
```

### Streamlit Cloud

- Config source: `st.secrets` (App Settings -> Secrets)
- Keep production values in secrets, not in repo `.env`
- Set `APP_BASE_URL` to your deployed app URL, for example:

```toml
APP_BASE_URL = "https://yourapp.streamlit.app"
```

- Cloud OAuth redirect URL (Slack + GitHub):

```
https://yourapp.streamlit.app
```

### Fallback behavior in this app

- If `st.secrets` exists, values are copied into env vars at runtime
- If `st.secrets` is missing (common locally), app falls back cleanly to `.env`

---

## Slack App Setup

This app uses **Sign in with Slack** — no bot, no event subscriptions, no webhooks. The EM authorizes once and their user token is used to pull channel history on demand.

### 1. Create a Slack App

Go to **[api.slack.com/apps](https://api.slack.com/apps)** → **Create New App** → **From Scratch**.

### 2. Add User Token Scopes

Go to **OAuth & Permissions** → scroll to **User Token Scopes** and add:

```
channels:history       — read messages in public channels
channels:read          — list public channels
groups:history         — read messages in private channels
groups:read            — list private channels
users:read             — resolve user profiles (for standup name matching)
users:read.email       — resolve user emails
```

> These must be **User Token Scopes**, not Bot Token Scopes.

### 3. Add Redirect URL

Under **OAuth & Permissions → Redirect URLs**, add your app's root URL:

```
https://yourapp.streamlit.app
```

> OAuth callbacks are handled at the root URL via query parameters — no `/callback` path needed.

### 4. Copy credentials to `.env`

From the **Basic Information** page:
- **Client ID** → `SLACK_CLIENT_ID`
- **Client Secret** → `SLACK_CLIENT_SECRET`

---

## GitHub OAuth App Setup

Go to **[github.com/settings/developers](https://github.com/settings/developers)** → **OAuth Apps** → **New OAuth App**:

| Field | Value |
|---|---|
| Homepage URL | `https://yourapp.streamlit.app` |
| Authorization callback URL | `https://yourapp.streamlit.app` |

Copy the **Client ID** and **Client Secret** into `.env`.

---

## Deploying to Streamlit Cloud

### 1. Push to GitHub

Make sure your latest code is pushed to a GitHub repository.

### 2. Create the app on Streamlit Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
2. Select your repository and branch
3. Set **Main file path** to `streamlit_app.py`
4. Click **Deploy**

### 3. Set up the database (Supabase)

Streamlit Cloud blocks direct TCP to port 5432 — use Supabase's **Transaction Pooler**.

1. Go to your Supabase project → **Settings** → **Database**
2. Scroll to **Connection Pooling** → Mode: **Transaction**
3. Copy the connection string and convert:
```
postgresql+asyncpg://postgres.xxxx:[password]@aws-0-us-east-1.pooler.supabase.com:6543/postgres?ssl=require
```

#### Run migrations from your local machine

```bash
DATABASE_URL="postgresql+asyncpg://postgres:[password]@db.xxxx.supabase.co:5432/postgres" \
  .venv/bin/alembic upgrade head
```

> Use the **direct connection** (port 5432) for migrations only. The app always uses the pooler URL at runtime.

| Connection type | Port | Streamlit Cloud |
|---|---|---|
| Direct (`db.xxxx.supabase.co`) | 5432 | ❌ Blocked |
| Session pooler | 5432 | ❌ Blocked |
| **Transaction pooler** | **6543** | **✅ Works** |

### 4. Add secrets

Go to **⋮ → Settings → Secrets**:

```toml
DATABASE_URL = "postgresql+asyncpg://postgres.xxxx:[password]@aws-0-us-east-1.pooler.supabase.com:6543/postgres?ssl=require"

SLACK_CLIENT_ID     = "your-slack-client-id"
SLACK_CLIENT_SECRET = "your-slack-client-secret"

GITHUB_CLIENT_ID     = "your-github-client-id"
GITHUB_CLIENT_SECRET = "your-github-client-secret"

ANTHROPIC_API_KEY = "sk-ant-..."
ANTHROPIC_MODEL   = "claude-sonnet-4-6"

APP_BASE_URL = "https://yourapp.streamlit.app"

ENABLE_AI_EXTRACTION = "true"
```

### 5. Update OAuth callback URLs

Set your Streamlit app URL as the redirect/callback URL in both:
- **Slack app** → OAuth & Permissions → Redirect URLs
- **GitHub OAuth app** → Authorization callback URL

### 6. Reboot the app

After saving secrets, click **Reboot app**.

---

## Usage

### 1. Connect Accounts

Go to **🔗 Connect Accounts** → **Sign in with Slack**. After authorizing, click **Connect GitHub**.

Use **Reconnect Slack** at any time to refresh your token or pick up new OAuth scopes (e.g. after adding `groups:read` / `groups:history` for private channels).

### 2. Build Your Team

Go to **👥 Team Overview** → **Load workspace users** → select your direct reports → **Add selected members**.

Optionally supply each member's GitHub handle. If they later connect their own GitHub via OAuth, that takes precedence.

> Team members do not need to sign in to the app.

### 3. Sync Data

Go to **🔄 Sync Data**, select a team member (or yourself), set the backfill window, and click **Sync Slack** or **Sync GitHub**.

**How Slack sync works:**

| Scenario | Behaviour |
|---|---|
| Syncing **yourself** | All messages from all joined channels (public + private) are captured |
| Syncing a **team member** | Only channels that member is in are processed; within each channel only messages **sent by** or **@mentioning** that member are saved |
| **Standup bot messages** (Geekbot-style) | The bot's `username` field is matched against the member's display/real name — only that member's own standup entry is saved, not the entire bot thread |

**Channel ignore list** — the following are always skipped regardless of membership:

- Channels with `nimble-` prefix
- Channels with `-activity` or `-corner` suffix
- Channels with `ic-` prefix
- Exact: `access-requests`, `vn-community`, `cat-place`, `hardware-and-machinery`

Progress is shown per channel and per GitHub repo with a live log and progress bar.

### 4. Generate a Work Report

Go to **📊 Work Report**, select a team member, choose a date range, and click **Generate Report**.

- **Activity Feed** — GitHub commits/PRs/reviews and Slack standups/messages, all browsable
- **AI Insights** — Claude-powered work classification and leadership summary (toggle on/off)
- **Share Summary** — formatted text block with a one-click copy button for pasting into Slack, email, or a doc

### 5. Manage Your Team

Go to **👥 Team Overview** to see all tracked members, GitHub connection source (OAuth vs manually set), edit GitHub handles, or remove members.

---

## Standup Bot Integration

The ingester handles two common standup patterns automatically:

**Pattern 1 — Thread replies** (user responds directly in-thread):
- Bot posts the question at the top level
- Team member replies with their own Slack account
- Replies are captured via `conversations.replies`

**Pattern 2 — Bot-reposted summaries** (Geekbot-style):
- Bot collects answers privately, then reposts each member's standup as a `bot_message` with `username` set to the member's full name
- The ingester matches `username` against `TeamMember.member_display_name` and `member_real_name` (case-insensitive)
- The message is stored attributed to the matched member's Slack user ID
- **Requirement:** the member must be added to your team roster in **Team Overview** with their exact display name matching what the bot uses

---

## Project Structure

```
streamlit_app.py               # Main UI + OAuth callback handler
pages/
├── 1_Connect.py               # Sign in with Slack + GitHub linking (with disconnect)
├── 2_Work_Report.py           # Work report UI — charts, activity feed, share summary
├── 3_Team_Overview.py         # Team management — add/remove/edit members
└── 4_Sync.py                  # Slack + GitHub sync with per-channel/repo progress
app/
├── config.py                  # Settings (pydantic-settings + .env)
├── database.py                # SQLAlchemy async engine + session (NullPool)
├── models/
│   ├── user.py                # User + UserGitHubLink
│   ├── slack_token.py         # SlackUserToken (per-user OAuth tokens)
│   ├── team_member.py         # TeamMember — EM's tracked team roster
│   ├── work_unit.py           # WorkUnit — core normalized abstraction
│   └── raw_data.py            # SlackMessage, GitHubActivity (raw store)
├── ingestion/
│   ├── slack_ingester.py      # Channel history, thread replies, standup name resolution
│   └── github_ingester.py     # Commits, PRs, reviews, issues per user
├── normalization/
│   └── normalizer.py          # Raw → WorkUnit (with dedup guards)
├── analytics/
│   └── report_builder.py      # Aggregation + WorkReport construction
├── ai/
│   ├── schemas.py             # Pydantic output schemas
│   ├── work_extractor.py      # Standup text → structured work items
│   └── insight_generator.py   # WorkReport → leadership insights
├── slack/
│   ├── oauth.py               # Sign in with Slack OAuth flow + token upsert
│   └── users.py               # Workspace user listing (users.list API)
└── github/
    └── oauth.py               # GitHub OAuth exchange + user linking
```

---

## Feature Flags

```env
ENABLE_AI_EXTRACTION=true      # AI work classification from standups
ENABLE_BURNOUT_DETECTION=false # Not yet implemented
ENABLE_ORG_ANALYTICS=false     # Not yet implemented
```

---

## Privacy

- Only channels the EM has joined are synced — no access to channels they're not in
- For team member syncs, only that member's messages (sent or mentioned) are stored — not the full channel history
- GitHub access requires explicit OAuth consent from each individual user
- All data is scoped to your workspace and stored in your own database
- Team members can be removed from the roster at any time
