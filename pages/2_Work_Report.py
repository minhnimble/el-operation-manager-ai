"""
Work Report page.

Select a user, pick a date range, generate a structured report
with GitHub metrics, Slack activity, and AI work classification.
"""

import asyncio
from datetime import datetime, timedelta, date

import streamlit as st

from app.streamlit_env import load_streamlit_secrets_into_env

load_streamlit_secrets_into_env()
import plotly.graph_objects as go
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import User
from app.models.team_member import TeamMember
from app.analytics.report_builder import build_work_report, format_report_for_slack
from app.ai.schemas import WorkReport

st.set_page_config(page_title="Work Report", page_icon="📊", layout="wide")


def run(coro):
    return asyncio.run(coro)


def _format_standup_body(text: str) -> str:
    """Format standup message text for readable display.

    Standup bot messages use Slack mrkdwn: bold question headers are wrapped
    in *...* and bullet answers follow as inline • sequences.

    Strategy:
      1. Normalise existing newlines to spaces so both stored formats are
         handled consistently.
      2. Insert a paragraph break (\\n\\n) before each *bold section* that
         follows non-whitespace content — this separates answer bullets from
         the next question header without breaking inside the header itself.
      3. Convert inline bullet sequences to one bullet per soft line break
         (markdown '  \\n') so they stay visually grouped with their header.
    """
    import re

    text = text.strip()
    if not text:
        return text

    # Step 1: normalise — collapse all newlines to single spaces
    text = re.sub(r"\s*\n\s*", " ", text).strip()

    # Step 2: paragraph break before each *bold header* that follows content
    # Matches: non-whitespace char, optional spaces, then opening *
    # Uses negative lookbehind so we don't double-insert on already-split text
    text = re.sub(r"(\S) +(\*\S)", lambda m: m.group(1) + "\n\n" + m.group(2), text)

    # Step 3: one bullet per line (soft markdown line break '  \n')
    text = re.sub(r" *• +", "  \n• ", text)

    return text.strip()


async def _get_team_options(
    manager_user_id: str,
    manager_team_id: str,
    self_name: str,
) -> dict[str, str]:
    """Return {display_name: slack_user_id} for self + all added team members."""
    options: dict[str, str] = {self_name: manager_user_id}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
            ).order_by(TeamMember.member_display_name)
        )
        members = result.scalars().all()

    for m in members:
        name = m.display()
        # Avoid key collision if display name matches self
        key = name if name != self_name else f"{name} (team)"
        options[key] = m.member_slack_user_id

    return options


async def _load_user_map(slack_team_id: str) -> dict[str, str]:
    """Return {slack_user_id: display_name} for all known users in the team.

    Merges TeamMember and User tables so both managed members and signed-in
    users are resolvable.  Used to replace <@USER_ID> mentions in message text.
    """
    from app.models.team_member import TeamMember
    id_to_name: dict[str, str] = {}
    async with AsyncSessionLocal() as db:
        tm_rows = await db.execute(
            select(TeamMember.member_slack_user_id, TeamMember.member_display_name).where(
                TeamMember.member_slack_team_id == slack_team_id,
            )
        )
        for uid, name in tm_rows.all():
            if uid and name:
                id_to_name[uid] = name

        u_rows = await db.execute(
            select(User.slack_user_id, User.slack_display_name, User.slack_real_name).where(
                User.slack_team_id == slack_team_id,
            )
        )
        for uid, display, real in u_rows.all():
            if uid and uid not in id_to_name:
                id_to_name[uid] = display or real or uid
    return id_to_name


def _collect_unknown_user_ids(items: list[dict], known: dict[str, str]) -> set[str]:
    """Scan message bodies for <@USER_ID> mentions not yet in the known map."""
    import re
    unknown: set[str] = set()
    for item in items:
        body = item.get("body") or item.get("title") or ""
        for uid in re.findall(r"<@([A-Z0-9]+)>", body):
            if uid not in known:
                unknown.add(uid)
    return unknown


def _enrich_user_map(
    user_map: dict[str, str],
    unknown_ids: set[str],
    access_token: str,
) -> dict[str, str]:
    """Fetch Slack profiles for unknown user IDs and merge into user_map.

    Uses synchronous requests (same pattern as oauth.py) so it's safe to call
    from Streamlit's sync context.  Silently skips any ID that fails to resolve.
    """
    import requests

    enriched = dict(user_map)
    for uid in unknown_ids:
        try:
            resp = requests.get(
                "https://slack.com/api/users.info",
                params={"user": uid},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                profile = data.get("user", {}).get("profile", {})
                name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or data["user"].get("name")
                    or uid
                )
                enriched[uid] = name
        except Exception:
            pass  # leave ID as-is if lookup fails
    return enriched


def _format_slack_text(text: str, user_map: dict[str, str]) -> str:
    """Convert Slack mrkdwn codes in message body to readable markdown.

    Handles:
      <@USER_ID>            → @display_name  (or @USER_ID if unknown)
      <#CHANNEL_ID|name>    → #name
      <#CHANNEL_ID>         → #CHANNEL_ID
      <URL|link text>       → [link text](URL)
      <URL>                 → URL (bare link)
    """
    import re

    # User mentions
    def _replace_user(m: re.Match) -> str:
        uid = m.group(1)
        return f"@{user_map.get(uid, uid)}"
    text = re.sub(r"<@([A-Z0-9]+)>", _replace_user, text)

    # Channel references  <#C123|name> or <#C123>
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<#([A-Z0-9]+)>", r"#\1", text)

    # Links with display text  <https://...|text>
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"[\2](\1)", text)

    # Bare links  <https://...>
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

    return text


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
    self_name = st.session_state.get("slack_display_name", slack_user_id)
    user_options = run(_get_team_options(slack_user_id, slack_team_id, self_name))

    selected_name = st.selectbox(
        "Team member",
        options=list(user_options.keys()),
        help="Add team members on the Team Overview page.",
    )
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

# Load user map: start from DB, then enrich unknown IDs via Slack API
_user_map: dict[str, str] = run(_load_user_map(slack_team_id))

_all_slack_msgs = [a for a in report.recent_activity if a.get("source") == "slack"]
_unknown_ids = _collect_unknown_user_ids(_all_slack_msgs, _user_map)
if _unknown_ids:
    try:
        from app.models.slack_token import SlackUserToken

        async def _get_slack_token_for_report() -> str | None:
            async with AsyncSessionLocal() as _db:
                _r = await _db.execute(
                    select(SlackUserToken).where(
                        SlackUserToken.slack_user_id == slack_user_id,
                        SlackUserToken.slack_team_id == slack_team_id,
                    )
                )
                _rec = _r.scalar_one_or_none()
                return _rec.access_token if _rec else None

        _slack_token = run(_get_slack_token_for_report())
        if _slack_token:
            _user_map = _enrich_user_map(_user_map, _unknown_ids, _slack_token)
    except Exception:
        pass  # enrichment is best-effort; falls back to raw ID on any error

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

# ─── Activity Feed ────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("📋 Activity Feed")
st.caption("Raw activity captured in this period.")

_activity    = getattr(report, "recent_activity", [])
github_items = [a for a in _activity if a["source"] == "github"]
slack_items  = [a for a in _activity if a["source"] == "slack"]

# ── GitHub activity ──────────────────────────────────────────────────────────

_GITHUB_ICONS = {
    "commit":       "🔨",
    "pr_opened":    "🔀",
    "pr_merged":    "✅",
    "pr_review":    "👀",
    "issue_opened": "🐛",
    "issue_closed": "✔️",
    "issue_comment":"💬",
}

with st.expander(f"GitHub Activity ({len(github_items)} items)", expanded=len(github_items) > 0):
    if not github_items:
        st.info("No GitHub activity in this period.")
    else:
        for item in github_items:
            icon  = _GITHUB_ICONS.get(item["type"], "⚙️")
            label = item["type"].replace("_", " ").title()
            repo  = f"`{item['github_repo']}`" if item["github_repo"] else ""
            title = item["title"] or (item["body"][:120] if item["body"] else "(no title)")
            ts    = item["timestamp"]

            col_icon, col_body, col_ts = st.columns([0.3, 5, 1.5])
            col_icon.markdown(icon)
            if item["url"]:
                col_body.markdown(f"**[{title}]({item['url']})** &nbsp; {repo}")
            else:
                col_body.markdown(f"**{title}** &nbsp; {repo}")
            col_ts.caption(ts)

# ── Slack messages ───────────────────────────────────────────────────────────

_SLACK_ICONS = {
    "standup":      "🗣️",
    "discussion":   "💬",
    "thread_reply": "↩️",
    "announcement": "📢",
}

standups     = [a for a in slack_items if a["type"] == "standup"]
other_slack  = [a for a in slack_items if a["type"] != "standup"]

with st.expander(f"Standups ({len(standups)})", expanded=len(standups) > 0):
    if not standups:
        st.info("No standup messages in this period.")
    else:
        for item in standups:
            ts      = item["timestamp"]
            raw     = item["body"] or item["title"] or "(empty)"
            body    = _format_standup_body(_format_slack_text(raw, _user_map))
            ch_name = item.get("channel_name") or item.get("slack_channel_id") or ""
            ch      = f"#{ch_name}" if ch_name else ""
            st.markdown(f"**{ts}** {ch}")
            st.markdown(body)
            st.divider()

with st.expander(f"Slack Messages ({len(other_slack)})", expanded=False):
    if not other_slack:
        st.info("No discussion messages in this period.")
    else:
        # ── CSV export ────────────────────────────────────────────────────────
        import csv, io
        _csv_buf = io.StringIO()
        _writer  = csv.DictWriter(
            _csv_buf,
            fieldnames=["timestamp", "channel", "type", "body", "url"],
            extrasaction="ignore",
        )
        _writer.writeheader()
        for _item in other_slack:
            _writer.writerow({
                "timestamp": _item["timestamp"],
                "channel":   _item.get("channel_name") or _item.get("slack_channel_id") or "",
                "type":      _item["type"],
                "body":      _item["body"] or _item["title"] or "",
                "url":       _item.get("url") or "",
            })
        st.download_button(
            label="⬇️ Export to CSV",
            data=_csv_buf.getvalue().encode("utf-8"),
            file_name=f"slack_messages_{selected_name.replace(' ', '_')}.csv",
            mime="text/csv",
            key="export_slack_csv",
        )

        st.markdown("---")

        # ── Group by channel ──────────────────────────────────────────────────
        from collections import defaultdict
        _by_channel: dict[str, list[dict]] = defaultdict(list)
        for _item in other_slack:
            _ch_key = _item.get("channel_name") or _item.get("slack_channel_id") or "unknown"
            _by_channel[_ch_key].append(_item)

        for _ch_name, _msgs in sorted(_by_channel.items()):
            st.markdown(f"**#{_ch_name}** &nbsp; <small>{len(_msgs)} message(s)</small>", unsafe_allow_html=True)
            for item in _msgs:
                icon = _SLACK_ICONS.get(item["type"], "💬")
                ts   = item["timestamp"]
                raw  = item["body"] or item["title"] or "(empty)"
                body = _format_slack_text(raw, _user_map)

                col_icon, col_body, col_ts = st.columns([0.3, 5, 1.5])
                col_icon.markdown(icon)
                col_body.markdown(body)
                col_ts.caption(ts)
            st.markdown("---")

# ─── Share Summary ────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("📤 Share Summary")
st.caption("Copy this text to share via Slack, email, or a doc. Click the copy icon in the top-right of the block.")


def _build_share_text(r: "WorkReport") -> str:
    from datetime import datetime as _dt
    generated = _dt.utcnow().strftime("%b %d, %Y")

    def _pad(label: str, value: int, width: int = 22) -> str:
        dots = "." * max(1, width - len(label))
        return f"  {label} {dots} {value}"

    lines = [
        f"Work Report: {r.user_display_name}",
        f"Period:      {r.date_range}",
        f"Generated:   {generated}",
        "",
        "── GITHUB ACTIVITY ──────────────────",
        _pad("Commits",      r.commits),
        _pad("PRs Opened",   r.prs_opened),
        _pad("PRs Merged",   r.prs_merged),
        _pad("PR Reviews",   r.pr_reviews),
        _pad("Issues Opened",r.issues_opened),
        "",
        "── SLACK ACTIVITY ───────────────────",
        _pad("Standups",       r.standup_count),
        _pad("Discussions",    r.discussion_messages),
        _pad("Thread Replies", r.thread_replies),
    ]

    if r.feature_work or r.bug_fixes or r.architecture_work or r.mentorship or r.incidents:
        lines += [
            "",
            "── AI WORK CLASSIFICATION ───────────",
            _pad("Feature Work",  r.feature_work),
            _pad("Bug Fixes",     r.bug_fixes),
            _pad("Architecture",  r.architecture_work),
            _pad("Mentorship",    r.mentorship),
            _pad("Incidents",     r.incidents),
        ]

    if r.ai_insights:
        lines += [
            "",
            "── AI INSIGHTS ──────────────────────",
            *[f"  {line}" for line in r.ai_insights.splitlines()],
        ]

    if r.standup_summary:
        lines += [
            "",
            "── STANDUP VS GITHUB ────────────────",
            f"  {r.standup_summary}",
        ]

    if r.recent_standups:
        lines += ["", "── RECENT STANDUPS ──────────────────"]
        for i, text in enumerate(r.recent_standups, 1):
            lines.append(f"  {i}. {text[:300]}{'…' if len(text) > 300 else ''}")

    lines += ["", "─" * 38]
    return "\n".join(lines)


st.code(_build_share_text(report), language="text")
