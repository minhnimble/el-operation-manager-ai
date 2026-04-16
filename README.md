# Engineering Operations Manager

Turns Slack + GitHub activity into structured work analytics for Engineering Managers, Tech Leads, and CTOs.

---

## What It Does

- **Slack activity** — pulls messages from public and private channels you're in, including standup bots (Geekbot-style)
- **GitHub activity** — commits, PRs, reviews, and issues via user OAuth
- **Team management** — add engineers to your roster; they don't need to sign in
- **Batch sync** — sync yourself, your whole team, or any subset at once with per-member progress
- **Background sync** — runs in a daemon thread; switch pages freely without losing progress
- **Flexible date ranges** — "Last N days" or custom range; default 3 months
- **AI classification** — Claude surfaces feature work, bug fixes, architecture, mentorship, and incidents
- **Shareable reports** — activity feed, AI insights, and one-click copy summary per member
- **Database cleanup** — remove ignored-channel data or stale member data in bulk

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| Database | PostgreSQL (SQLAlchemy async + NullPool) |
| Slack | Sign in with Slack — user OAuth (no bot, no webhooks) |
| GitHub | GitHub REST API v3 — user OAuth |
| AI | Anthropic Claude API |
| Migrations | Alembic |

> **No Redis or Celery.** Syncs run in daemon threads within the Streamlit process.

---

## Prerequisites

- Python 3.13+
- A PostgreSQL database (Supabase recommended for cloud, Docker for local)
- A Slack OAuth app
- A GitHub OAuth app
- An Anthropic API key

---

## Quick Start (Streamlit Cloud)

> **Why cloud first?** Slack OAuth requires `https://` redirect URLs — even for `localhost`. Streamlit Cloud gives you HTTPS out of the box with zero cert setup.

### 1. Create OAuth apps

**Slack** — go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From Scratch.

Under **OAuth & Permissions → User Token Scopes**, add:

```
channels:history       — read public channel messages
channels:read          — list public channels
groups:history         — read private channel messages
groups:read            — list private channels
users:read             — resolve user profiles
users:read.email       — resolve user emails
```

> These must be **User Token Scopes**, not Bot Token Scopes.

**GitHub** — go to [github.com/settings/developers](https://github.com/settings/developers) → OAuth Apps → New OAuth App.

Set both **Homepage URL** and **Authorization callback URL** to your Streamlit Cloud URL (e.g. `https://yourapp.streamlit.app`).

### 2. Set redirect URLs

Both Slack and GitHub OAuth redirect to the app root URL via query params — no `/callback` path.

| Provider | Where to set | Value |
|---|---|---|
| Slack | OAuth & Permissions → Redirect URLs | `https://yourapp.streamlit.app` |
| GitHub | Authorization callback URL | `https://yourapp.streamlit.app` |

Copy the **Client ID** and **Client Secret** from each provider — you'll need them in step 4.

### 3. Deploy to Streamlit Cloud

1. Push your code to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app** → select repo/branch → set main file to `streamlit_app.py` → **Deploy**

### 4. Set up the database (Supabase)

Streamlit Cloud blocks direct TCP on port 5432 — use Supabase's **Transaction Pooler** (port 6543).

1. Create a Supabase project → **Settings** → **Database** → **Connection Pooling** → Mode: **Transaction**
2. Copy the pooler connection string:
   ```
   postgresql+asyncpg://postgres.xxxx:[password]@aws-0-us-east-1.pooler.supabase.com:6543/postgres?ssl=require
   ```

| Connection type | Port | Streamlit Cloud |
|---|---|---|
| Direct | 5432 | ❌ Blocked |
| Session pooler | 5432 | ❌ Blocked |
| **Transaction pooler** | **6543** | **✅ Works** |

Run migrations from your local machine using the **direct** connection (port 5432):

```bash
DATABASE_URL="postgresql+asyncpg://postgres:[password]@db.xxxx.supabase.co:5432/postgres" \
  .venv/bin/alembic upgrade head
```

### 5. Add secrets

Go to your Streamlit app → **⋮ → Settings → Secrets**:

```toml
DATABASE_URL = "postgresql+asyncpg://postgres.xxxx:[password]@aws-0-us-east-1.pooler.supabase.com:6543/postgres?ssl=require"

SLACK_CLIENT_ID     = "..."
SLACK_CLIENT_SECRET = "..."

GITHUB_CLIENT_ID     = "..."
GITHUB_CLIENT_SECRET = "..."

ANTHROPIC_API_KEY = "sk-ant-..."
ANTHROPIC_MODEL   = "claude-sonnet-4-6"

APP_BASE_URL = "https://yourapp.streamlit.app"

ENABLE_AI_EXTRACTION = "true"
```

Click **Reboot app** after saving.

---

## Usage

### 1. Connect Accounts

Go to **🔗 Connect Accounts** → **Sign in with Slack**. Then click **Connect GitHub**.

Use **Reconnect** at any time to refresh tokens or pick up new OAuth scopes.

### 2. Build Your Team

Go to **👥 Team Overview** → **Load workspace users** → select your direct reports → **Add selected members**.

Optionally set each member's GitHub handle. If they later connect via OAuth, that takes precedence.

> Team members do not need to sign in.

### 3. Sync Data

Go to **🔄 Sync Data**, pick members (**All** / **My Team** / individual), set the date range, and click **Sync Slack** or **Sync GitHub**.

The sync runs in the background — switch pages freely and come back to see progress.

**Slack sync behaviour:**

| Scenario | What's captured |
|---|---|
| Syncing **yourself** | All messages from all joined channels |
| Syncing a **team member** | Only messages **sent by** or **@mentioning** that member |
| **Standup bot** (Geekbot-style) | Bot's `username` matched to member's display name |

**Ignored channels** (always skipped): `nimble-*`, `*-activity`, `*-corner`, `ic-*`, and a few exact names.

**Database cleanup** (bottom of Sync page): remove ignored-channel data across all users, or clear stale data for removed members.

### 4. Generate a Work Report

Go to **📊 Work Report**, select a member, choose a date range, and click **Generate Report**.

- **Activity Feed** — commits, PRs, reviews, standups, all browsable
- **AI Insights** — Claude-powered work classification and leadership summary
- **Share Summary** — one-click copy for Slack, email, or docs

---

## Standup Bot Integration

The ingester handles two patterns:

**Thread replies** — member replies to a bot's top-level post with their own account. Captured via `conversations.replies`.

**Bot-reposted summaries** (Geekbot-style) — bot reposts each member's standup as a `bot_message` with `username` set to the member's full name. The ingester matches this against the team roster (case-insensitive). The member must be in **Team Overview** with a matching display name.

---

## Local Development

> **Note:** Slack OAuth rejects plain `http://` redirect URLs. For local development you must run Streamlit with HTTPS using local TLS certs.

### 1. Clone and install

```bash
git clone <repo-url>
cd el-operation-manager-ai
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

Fill in `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `ANTHROPIC_API_KEY`, and set:

```env
APP_BASE_URL=https://localhost:8501
```

> Config source priority: `st.secrets` (if present) → `.env` file.

### 3. Start the database and run migrations

```bash
make up       # starts PostgreSQL via Docker Compose
make migrate  # runs Alembic migrations
```

### 4. Set up local HTTPS certs

```bash
./scripts/setup_local_https.sh
```

Uses `mkcert` if available, otherwise falls back to `openssl`. Writes to `.certs/localhost.pem` and `.certs/localhost-key.pem`.

### 5. Run with HTTPS

```bash
streamlit run streamlit_app.py \
  --server.port 8501 \
  --server.address localhost \
  --server.sslCertFile .certs/localhost.pem \
  --server.sslKeyFile .certs/localhost-key.pem
```

Set the OAuth redirect URL for both Slack and GitHub to `https://localhost:8501`.

---

## Project Structure

```
streamlit_app.py               # Main UI + OAuth callback handler
pages/
├── 1_Connect.py               # Slack + GitHub OAuth linking
├── 2_Work_Report.py           # Work reports — charts, feed, share summary
├── 3_Team_Overview.py         # Team management — add/remove/edit members
└── 4_Sync.py                  # Slack + GitHub sync with background progress
app/
├── config.py                  # Settings (pydantic-settings + .env)
├── database.py                # SQLAlchemy async engine + session
├── models/                    # SlackMessage, GitHubActivity, WorkUnit, etc.
├── ingestion/                 # Slack + GitHub data ingestion
├── normalization/             # Raw → WorkUnit normalizer
├── analytics/                 # Report aggregation
├── ai/                        # Claude-powered classification + insights
├── slack/                     # OAuth flow + workspace user listing
└── github/                    # OAuth exchange + user linking
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

- Only channels the EM has joined are synced
- For team member syncs, only that member's messages are stored
- GitHub access requires explicit OAuth consent per user
- All data stays in your own database
- Team members can be removed at any time
