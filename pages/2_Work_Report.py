"""
Work Report page.

Select a user, pick a date range, generate a structured report
with GitHub metrics, Slack activity, and AI work classification.
"""

import asyncio
from datetime import datetime, timedelta, date

import streamlit as st
import plotly.graph_objects as go
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import User
from app.analytics.report_builder import build_work_report, format_report_for_slack
from app.ai.schemas import WorkReport

st.set_page_config(page_title="Work Report", page_icon="📊", layout="wide")


def run(coro):
    return asyncio.run(coro)


async def _get_users(team_id: str) -> list[User]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(
                User.slack_team_id == team_id,
                User.opted_in == True,  # noqa: E712
            )
        )
        return result.scalars().all()


async def _get_report(slack_user_id, slack_team_id, start, end, include_ai) -> WorkReport:
    async with AsyncSessionLocal() as db:
        return await build_work_report(
            db=db,
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            start_date=start,
            end_date=end,
            include_ai=include_ai,
        )


st.title("📊 Work Report")
st.caption("Generate a structured activity report for any team member.")
st.markdown("---")

# ─── Auth check ──────────────────────────────────────────────────────────────

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if not slack_user_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

# ─── Controls ─────────────────────────────────────────────────────────────────

col1, col2, col3 = st.columns([2, 2, 1])

with col1:
    users = run(_get_users(slack_team_id))
    user_options = {
        (u.slack_display_name or u.slack_real_name or u.slack_user_id): u.slack_user_id
        for u in users
    }
    # Always include self
    self_name = st.session_state.get("slack_display_name", slack_user_id)
    if self_name not in user_options:
        user_options = {self_name: slack_user_id, **user_options}

    selected_name = st.selectbox("Team member", options=list(user_options.keys()))
    target_user_id = user_options[selected_name]

with col2:
    preset = st.selectbox(
        "Date range",
        ["Last 7 days", "Last 14 days", "Last 30 days", "Custom"],
    )

with col3:
    include_ai = st.toggle("AI insights", value=True)

# Custom date range
if preset == "Custom":
    c1, c2 = st.columns(2)
    start_date = c1.date_input("From", value=date.today() - timedelta(days=14))
    end_date = c2.date_input("To", value=date.today())
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time().replace(microsecond=0))
else:
    days = {"Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30}[preset]
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days)

st.markdown("---")

# ─── Generate ─────────────────────────────────────────────────────────────────

if st.button("Generate Report", type="primary", use_container_width=False):
    with st.spinner("Building report..."):
        try:
            report = run(_get_report(target_user_id, slack_team_id, start_dt, end_dt, include_ai))
            st.session_state["last_report"] = report
        except Exception as e:
            st.error(f"Failed to generate report: {e}")
            st.stop()

report: WorkReport | None = st.session_state.get("last_report")

if not report:
    st.info("Select a team member and click **Generate Report**.")
    st.stop()

# ─── Display ──────────────────────────────────────────────────────────────────

st.subheader(f"Report: {report.user_display_name}")
st.caption(f"Period: {report.date_range}")

# Top metrics row
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Commits",       report.commits)
m2.metric("PRs Opened",    report.prs_opened)
m3.metric("PRs Merged",    report.prs_merged)
m4.metric("PR Reviews",    report.pr_reviews)
m5.metric("Standups",      report.standup_count)
m6.metric("Discussions",   report.discussion_messages)

st.markdown("---")

col_left, col_right = st.columns(2)

# GitHub activity bar chart
with col_left:
    st.markdown("**GitHub Activity**")
    fig = go.Figure(go.Bar(
        x=["Commits", "PRs Opened", "PRs Merged", "PR Reviews", "Issues"],
        y=[report.commits, report.prs_opened, report.prs_merged,
           report.pr_reviews, report.issues_opened],
        marker_color=["#4C9BE8", "#5DBB8B", "#2E8B57", "#E8A24C", "#E86B4C"],
    ))
    fig.update_layout(margin=dict(t=10, b=10), height=260, showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

# AI work classification donut
with col_right:
    st.markdown("**AI Work Classification** _(from standups)_")
    ai_labels = ["Feature", "Bug Fix", "Architecture", "Mentorship", "Incident"]
    ai_values = [
        report.feature_work, report.bug_fixes, report.architecture_work,
        report.mentorship, report.incidents,
    ]
    if sum(ai_values) > 0:
        fig2 = go.Figure(go.Pie(
            labels=ai_labels,
            values=ai_values,
            hole=0.5,
            marker_colors=["#4C9BE8", "#E86B4C", "#9B4CE8", "#5DBB8B", "#E8A24C"],
        ))
        fig2.update_layout(margin=dict(t=10, b=10), height=260)
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No standup data available for AI classification in this period.")

# AI insights
if report.ai_insights:
    st.markdown("---")
    st.markdown("**AI Insights**")
    st.info(report.ai_insights)

if report.standup_summary:
    st.caption(f"Standup vs GitHub: {report.standup_summary}")

# Recent standups
if report.recent_standups:
    st.markdown("---")
    with st.expander(f"Recent Standups ({len(report.recent_standups)})"):
        for i, text in enumerate(report.recent_standups, 1):
            st.markdown(f"**{i}.** {text}")
