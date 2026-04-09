"""
Sync Data page.

Manually trigger Slack backfill and GitHub sync for the current user.
Background jobs are processed by the Celery worker.
"""

import streamlit as st

from app.tasks.ingestion_tasks import trigger_backfill, trigger_github_sync

st.set_page_config(page_title="Sync Data", page_icon="🔄", layout="wide")

st.title("🔄 Sync Data")
st.caption("Pull your latest Slack messages and GitHub activity into the database.")
st.markdown("---")

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if not slack_user_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

st.info(
    "Syncs run in the background via Celery. Make sure the **worker** is running. "
    "Large backfills (30 days) may take a few minutes."
)

st.markdown("---")

# ─── Slack Sync ───────────────────────────────────────────────────────────────

st.subheader("Slack")
st.caption("Pulls messages from all public channels you are a member of.")

days_slack = st.slider("Days to backfill", min_value=1, max_value=90, value=30, key="slack_days")

if st.button("Sync Slack", type="primary"):
    try:
        trigger_backfill.delay(
            slack_user_id=slack_user_id,
            team_id=slack_team_id,
            days_back=days_slack,
        )
        st.success(f"Slack backfill queued for the last {days_slack} days. Check back shortly.")
    except Exception as e:
        st.error(f"Failed to queue sync: {e}")

st.markdown("---")

# ─── GitHub Sync ──────────────────────────────────────────────────────────────

st.subheader("GitHub")
st.caption("Pulls commits, PRs, reviews, and issues from all your repositories.")

days_github = st.slider("Days to backfill", min_value=1, max_value=90, value=30, key="github_days")

if st.button("Sync GitHub", type="primary"):
    try:
        trigger_github_sync.delay(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            days_back=days_github,
        )
        st.success(f"GitHub sync queued for the last {days_github} days. Check back shortly.")
    except Exception as e:
        st.error(f"Failed to queue sync: {e}")

st.markdown("---")

st.caption(
    "Syncs are also scheduled automatically: Slack at 1am UTC, GitHub at 2am UTC (daily)."
)
