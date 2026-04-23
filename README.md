# Engineering Operations Manager

Turns Slack + GitHub activity into structured work analytics for Engineering Managers, Tech Leads, and CTOs.

---

## What It Does

- **Slack activity** — pulls messages from public and private channels you're in, including standup bots (Standuply-style)
- **GitHub activity** — PRs (created + reviewed) cross-org via Search API + PAT, with optional per-repo deep mode for commits/issues
- **Team management** — add engineers to your roster; they don't need to sign in
- **Batch sync** — sync yourself, your whole team, or any subset at once with per-member progress
- **Background sync** — runs in a daemon thread; switch pages freely without losing progress
- **Flexible date ranges** — "Last N days" or custom range; default 3 months
- **AI classification** — Claude surfaces feature work, bug fixes, architecture, mentorship, and incidents
- **Shareable reports** — activity feed, AI insights, and one-click copy summary per member
- **Database cleanup** — remove ignored-channel data or stale member data in bulk
- **Notion Dev Track Sync** — reads per-developer track pages from a Notion database and syncs skill status + objectives into the Google Sheet snapshot

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| Database | PostgreSQL (SQLAlchemy async + NullPool) |
| Slack | Sign in with Slack — user OAuth (no bot, no webhooks) |
| GitHub | GitHub REST + Search API — Personal Access Token (PAT) |
| AI | Anthropic Claude API |
| Notion | Notion API v1 — Internal Integration token |
| Migrations | Alembic |

> **No Redis or Celery.** Syncs run in daemon threads within the Streamlit process.

---

## Prerequisites

- Python 3.13+
- A PostgreSQL database (Supabase recommended for cloud, Docker for local)
- A Slack OAuth app
- A GitHub Personal Access Token (PAT) with `repo` + `read:org` scopes
- An Anthropic API key
- A Google Cloud service account with Sheets API access — for Developer Track
- A Notion Internal Integration token — for Notion Dev Track Sync

---

## Quick Start (Streamlit Cloud)

> **Why cloud first?** Slack OAuth requires `https://` redirect URLs — even for `localhost`. Streamlit Cloud gives you HTTPS out of the box with zero cert setup.

### 1. Set up Slack

**a. Create the OAuth app** — [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From Scratch.

**b. Add User Token Scopes** under **OAuth & Permissions → User Token Scopes**:

```
channels:history       — read public channel messages
channels:read          — list public channels
groups:history         — read private channel messages
groups:read            — list private channels
users:read             — resolve user profiles
users:read.email       — resolve user emails
usergroups:read        — resolve @subteam mentions to group handles
```

> These must be **User Token Scopes**, not Bot Token Scopes.

**c. Set the redirect URL** — **OAuth & Permissions → Redirect URLs** → add `https://yourapp.streamlit.app` (root URL, no `/callback` path).

**d. Copy credentials** — copy the **Client ID** and **Client Secret** from **Basic Information**. You'll paste them into the secrets step below.

### 2. Set up GitHub

**a. Create a Personal Access Token** — [github.com/settings/tokens](https://github.com/settings/tokens/new?description=Engineering+Operations+Manager&scopes=repo,read:org) → **Generate new token (classic)**.

| Scope | Why |
|---|---|
| `repo` | Read PRs, reviews, commits in private + public repos |
| `read:org` | Cross-org Search API visibility (needed for Overview-mode sync) |

**b. Copy the token** (`ghp_…` or `github_pat_…`) and set it as `GITHUB_PAT` in `.env` or Streamlit secrets. The PAT is never stored in the database — rotate by updating the secret and rebooting. One PAT covers the whole team.

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

# GitHub PAT — single token for the whole team. Required.
# Required scopes: repo + read:org.
GITHUB_PAT = "ghp_..."

ANTHROPIC_API_KEY = "sk-ant-..."
ANTHROPIC_MODEL   = "claude-sonnet-4-6"

APP_BASE_URL    = "https://yourapp.streamlit.app"
APP_SECRET_KEY  = "..."   # see note below

ENABLE_AI_EXTRACTION = "true"

# See "Developer Track (Google Sheets)" under Usage
GOOGLE_SHEETS_CREDENTIALS_JSON = ""
DEV_TRACK_SHEET_ID             = ""

# See "Notion Dev Track Sync" under Usage
NOTION_API_KEY                = ""
NOTION_DEV_TRACK_DATABASE_ID  = ""
NOTION_DEV_TRACK_VIEW_ID      = ""  # optional — filter by a specific view
```

> **`APP_SECRET_KEY`** signs the session cookie that keeps you logged in across page navigations and OAuth redirects. Generate one with:
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```
> Use the same key every deploy — changing it invalidates all existing sessions.

Click **Reboot app** after saving.

---

## Usage

### 1. Connect Accounts

Go to **🔗 Connect Accounts** → **Sign in with Slack**.

GitHub uses a single server-wide PAT configured via the `GITHUB_PAT` env var / Streamlit secret — no PAT is pasted in the UI and none is stored in the DB. The Connect page only validates the server PAT and links your GitHub handle so sync can query your PRs/reviews.

> One PAT covers the whole team. Required scopes: `repo` + `read:org` (for cross-org Search API access).

### 2. Build Your Team

Go to **👥 Team Overview** → **Load workspace users** → select your direct reports → **Add selected members**.

Set each member's GitHub handle (`github_login`) so the manager's PAT can query their PRs/reviews via the Search API.

> Team members do not need to sign in. They do not need their own PAT.

### 3. Sync Data

Go to **🔄 Sync Data**, pick members (**All** / **My Team** / individual), set the date range, and click **Sync Slack** or **Sync GitHub**.

The sync runs in the background — switch pages freely and come back to see progress.

**GitHub sync — two modes:**

| Mode | Toggle | What it pulls | When to use |
|---|---|---|---|
| **🔭 Overview mode** *(default)* | ON | PRs **created** + PRs **reviewed** by member, across **all organizations** the PAT can see | Default. Mirrors `github.com/<user>?tab=overview&from=YYYY-MM-DD&to=YYYY-MM-DD`. Cross-org. Fast. Single Search API query per member. |
| **Per-repo mode** | OFF | Commits + PRs + reviews + issues, repo-by-repo (only repos the token lists) | When you need commits, or the member is in a small fixed set of repos and you want the full payload. Slower. |

Overview mode uses the **GitHub Search API** with your **Personal Access Token (PAT)** to pull everything the member did in any org your PAT has visibility into — even repos you've never indexed. Skips commits (commit-search is heavily rate-limited on GitHub's side).

**Token resolution** (per member):
1. `GITHUB_PAT` env/secret + member's stored `github_login` → sync.
2. Either missing → skip.

No DB-stored PAT path. Set `GITHUB_PAT` in env/secrets and each member's handle in **Team Overview**.

**Date range** maps directly to the overview URL:
`from=sync_start.date()` `to=sync_end.date()` (or "now" if open-ended).

**Slack sync behaviour:**

| Scenario | What's captured |
|---|---|
| Syncing **yourself** | All messages from all joined channels |
| Syncing a **team member** | Only messages **sent by** or **@mentioning** that member |
| **Standup bot** (Standuply-style) | Bot's `username` matched to member's display name |

**Ignored channels** (always skipped): `nimble-*`, `*-activity`, `*-corner`, `ic-*`, and a few exact names.

**Database cleanup** (bottom of Sync page): remove ignored-channel data across all users, or clear stale data for removed members.

### 4. Generate a Work Report

Go to **📊 Work Report**, select a member, choose a date range, and click **Generate Report**.

- **🔗 Pull Request Links** — three grouped expanders (PRs Created · PRs Merged · PRs Reviewed) with direct links, deduped per PR
- **Activity Feed** — commits, PRs, reviews, standups, all browsable
- **AI Insights** — Claude-powered work classification and leadership summary
- **Developer Track** — level + skill progress from a Google Sheet (see below)
- **Share Summary** — one-click copy for Slack, email, or docs

### Developer Track (Google Sheets)

Shows each member's skill-vetting progress in the Work Report. Cell background colors drive status:

| Swatch | Color  | Status       |
|--------|--------|--------------|
| 🟢     | green  | vetted       |
| 🔵     | blue   | in progress  |
| 🟣     | purple | proposed     |
| 🟡     | yellow | focus        |
| ⚪     | white  | not started  |

Cell notes render inline.

**Sheet format** — one tab per person. The tab name must share a token (≥3 chars) with the member's Slack **display name**, **real name**, or **email local-part** (e.g. `don.vo@…` → `Don Vo`), so first name, last name, or full name all work. Column A = integer level, column B = level title, column C = Technical skills, column D = Soft skills.

| Col A | Col B                         | Col C — Technical skills | Col D — Soft skills |
|-------|-------------------------------|--------------------------|---------------------|
| `3`   | Junior Software Developer     | *skill text*             | *skill text*        |
| `4`   | Mid-senior Software Developer | *skill text*             | *skill text*        |

**Setup:**

1. In [Google Cloud Console](https://console.cloud.google.com/), enable the **Google Sheets API** and create a **Service Account** (no roles needed). Add a **JSON key** — a file downloads.
2. In the sheet, click **Share** and grant **Editor** to the key's `client_email` (Editor is required so the Notion Dev Track Sync can write back).
3. Set `GOOGLE_SHEETS_CREDENTIALS_JSON` (full JSON as a single-line string) and `DEV_TRACK_SHEET_ID` (URL segment between `/d/` and `/edit`) in secrets / `.env`. Reboot.

> **TOML tip:** wrap the JSON in single quotes so its double quotes parse; leave `\n` in `private_key` as-is.

Troubleshooting: *"No developer-track tab found"* → rename the tab to include the member's first name, last name, or full name as it appears in Slack. *"Caller does not have permission"* → share the sheet with `client_email`. *"not valid JSON"* → re-copy the full file, single-quote-wrapped.

### Notion Dev Track Sync

Reads per-developer track data from a **Notion database** and writes skill statuses,
objectives, and evidence notes into the matching Google Sheet tab. Notion is always
treated as the source of truth; the Sheet is the snapshot.

**Notion database setup:**

- Each database entry represents one developer.
- Page title format: `{developer name} <> {manager name}` — e.g. `Don <> Mike`.
- Each page body must contain a `## Skills Development` section with `### Level N`
  headings, toggle/bullet skills (bold), and `- [ ]` / `- [x]` to-do objectives.
- Optionally add a `## Focus Areas` section with bulleted skill names; those skills
  get "focus" (yellow) status in the Sheet.

**Setup:**

1. In Notion: **Settings** → **Connections** → **Develop or manage integrations**
   → **New integration**. Set type to **Internal**, copy the **Internal Integration
   Secret**.
2. Share the Notion database with the integration (open the database → ··· →
   **Connections** → add your integration).
3. Copy the database ID from its URL: `notion.so/.../{DATABASE_ID}?v={VIEW_ID}`.
4. (Optional) Copy the view ID — the `v=` segment of the same URL — if you
   want the sync to mirror a specific Notion view's filter + sort (e.g. only
   active developers). Leave blank to sync every page in the database.
5. Set `NOTION_API_KEY` (integration secret), `NOTION_DEV_TRACK_DATABASE_ID`,
   and optionally `NOTION_DEV_TRACK_VIEW_ID` in secrets / `.env`.
6. Re-share the Google Sheet with the service account as **Editor**
   (Viewer was enough for the Work Report read path; writes need Editor).
7. Navigate to **📋 Notion Dev Track Sync** in the app → **Fetch from Notion** →
   preview matches → **Sync**.

> **View-based filtering** uses Notion's Views API (`/v1/views/{view_id}/queries`,
> `Notion-Version: 2026-03-11`). Your integration must have access to the
> parent database; shared-view access is inherited from the database share.

**Sync behaviour:**

- Notion = source of truth; Sheet = snapshot.
- Skills in Notion but not in Sheet → added. Skills in Sheet but not in Notion → untouched (no deletes). Note mismatch → Notion wins.
- **Status from objective phrasing** (priority):
  1. Unchecked V-ing ("Working as…", "Actively raising…") or `In-progress:` / `In-review:` → **blue**.
  2. Unchecked `New objective:` / `To-review objective:` → **yellow**.
  3. All checked + current cell blue/yellow → **white**.
  4. Current cell green/purple → **unchanged**.
- Focus intent driven by to-do items only, never skill title.
- **Focus Areas** kept in sync: skills gaining focus intent → bullet added; losing it → removed. Bullets append inside heading when toggleable, else as siblings directly under heading (never page bottom).
- **Preview before apply** — per-member diff shown; sync only on explicit button. Per-member failures don't block others.
- Timestamps render in **GMT+7** (DB stays UTC).

---

## Standup Bot Integration

The ingester handles two patterns:

**Thread replies** — member replies to a bot's top-level post with their own account. Captured via `conversations.replies`.

**Bot-reposted summaries** (Standuply-style) — bot reposts each member's standup as a `bot_message` with `username` set to the member's full name. The ingester matches this against the team roster (case-insensitive). The member must be in **Team Overview** with a matching display name.

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

Fill in the required values:

```env
APP_SECRET_KEY=<random string — run: python -c "import secrets; print(secrets.token_hex(32))">
APP_BASE_URL=https://localhost:8501

SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...

# GitHub PAT — required. Required scopes: repo + read:org.
GITHUB_PAT=ghp_...

ANTHROPIC_API_KEY=sk-ant-...
```

`APP_SECRET_KEY` signs the session cookie that persists your Slack login across page refreshes and OAuth redirects. Any non-empty value works locally; just keep it stable (don't regenerate on every run) or your session will reset each time you restart the app.

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

Set the Slack OAuth redirect URL to `https://localhost:8501`. GitHub needs no redirect — the `GITHUB_PAT` env var is used directly.

---

## Project Structure

```
streamlit_app.py               # Main UI + OAuth callback handler
pages/
├── 1_Connect.py               # Slack OAuth + GitHub handle linking
├── 2_Work_Report.py           # Work reports — charts, feed, share summary
├── 3_Team_Overview.py         # Team management — add/remove/edit members
├── 4_Sync.py                  # Slack + GitHub sync with background progress
└── 5_Notion_Dev_Track.py      # Notion dev track preview, diff, and sync
app/
├── config.py                  # Settings (pydantic-settings + .env)
├── database.py                # SQLAlchemy async engine + session
├── models/                    # SlackMessage, GitHubActivity, WorkUnit, etc.
├── ingestion/                 # Slack + GitHub data ingestion
├── normalization/             # Raw → WorkUnit normalizer
├── analytics/                 # Report aggregation + Notion dev track parser/sync
├── ai/                        # Claude-powered classification + insights
├── integrations/              # Google Sheets + Notion async clients
├── slack/                     # OAuth flow + workspace user listing
└── github/                    # GitHub handle validation + user linking (oauth.py)
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
- GitHub access uses a single server-wide PAT set via `GITHUB_PAT` env/secret; never stored in the DB. Revoke any time at github.com/settings/tokens.
- All data stays in your own database
- Team members can be removed at any time
