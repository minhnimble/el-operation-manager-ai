"""
Work Report page.

Select a user, pick a date range, generate a structured report
with GitHub metrics, Slack activity, and AI work classification.
"""

import asyncio
from datetime import datetime, timedelta, date, timezone

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
from app.ui.page_utils import inject_page_load_bar
from app.ui.session_cookie import restore_session_from_cookie
inject_page_load_bar()
restore_session_from_cookie()


def run(coro):
    return asyncio.run(coro)


def _copy_button(text: str, key: str) -> None:
    """Render a copy button using st.popover.

    Clicking opens a popover with the message text in a st.code block.
    The code block has a native Streamlit copy icon in its top-right corner.
    """
    with st.popover("📋", help="Copy message"):
        st.code(text, language=None)


def _format_standup_body(text: str) -> str:
    """Format standup message text for readable display.

    Standup bot messages use Slack mrkdwn: bold question headers are wrapped
    in *...* and answer bullets follow as inline ``•`` (top-level) and
    ``◦`` (sub-bullet) sequences.  Slack stores these with real newlines in
    some clients and as a single flowed line in others, so we normalise first
    and then rebuild the structure deterministically.

    Rendering strategy — real nested markdown lists
      An earlier version tried to emulate bullets with literal ``•`` / ``◦``
      characters plus soft line breaks and a leading run of U+00A0 for the
      sub-bullet indent.  That produces correct HTML but Streamlit's theme
      (and most copy-paste paths) collapses the leading whitespace, so the
      second-level items visually flattened back against the left margin.

      Instead we emit a real Commonmark nested list.  Browsers render nested
      ``<ul>`` with disc → circle by default, which is exactly the Slack
      ``•`` → ``◦`` visual we want, and the indentation is handled by the
      list CSS rather than by fragile leading whitespace.

    Steps:
      1. Collapse existing newlines to spaces so both stored formats are
         handled consistently.
      2. Insert a paragraph break before each ``*bold header*`` that follows
         content, so the next question starts a new paragraph.
      3. Turn each ``• `` into a top-level list item (``\\n- ``).
      4. Turn each ``◦ `` into a nested list item (4-space indent + ``- ``).
      5. Ensure a blank line precedes the first list item in each paragraph,
         otherwise Commonmark treats ``text\\n- item`` as a single line and
         never opens a ``<ul>``.
    """
    import re

    text = text.strip()
    if not text:
        return text

    text = re.sub(r"\s*\n\s*", " ", text).strip()

    text = re.sub(r"(\S) +(\*\S)", lambda m: m.group(1) + "\n\n" + m.group(2), text)

    text = re.sub(r" *• +", "\n- ", text)
    text = re.sub(r" *◦ +", "\n    - ", text)

    text = re.sub(r"(\S)\n- ", r"\1\n\n- ", text)

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
    """Scan message bodies for <@USER_ID> mentions not yet in the known map.

    Also picks up ``sender_id`` values so that enriched sender names are
    resolved even when the author isn't mentioned anywhere in the text.
    """
    import re
    unknown: set[str] = set()
    for item in items:
        sid = item.get("sender_id")
        if sid and sid not in known:
            unknown.add(sid)
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


def _collect_unknown_channel_ids(items: list[dict], known: dict[str, str]) -> set[str]:
    """Return channel IDs from activity items that have no name in *known* yet."""
    unknown: set[str] = set()
    for item in items:
        cid = item.get("slack_channel_id") or ""
        if cid and cid not in known and not item.get("channel_name", "").startswith("#") and item.get("channel_name", "") == cid:
            unknown.add(cid)
        # Also catch items where channel_name is still the raw ID
        ch = item.get("channel_name") or ""
        if ch and ch == cid:
            unknown.add(cid)
    return unknown


def _enrich_channel_map(
    channel_map: dict[str, str],
    unknown_ids: set[str],
    access_token: str,
) -> dict[str, str]:
    """Fetch channel names from Slack conversations.info for unknown channel IDs."""
    import requests

    enriched = dict(channel_map)
    for cid in unknown_ids:
        try:
            resp = requests.get(
                "https://slack.com/api/conversations.info",
                params={"channel": cid},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                name = data.get("channel", {}).get("name") or cid
                enriched[cid] = name
        except Exception:
            pass
    return enriched


def _collect_subteam_ids(items: list[dict]) -> set[str]:
    """Return Slack user-group IDs referenced as ``<!subteam^S…>`` in message bodies."""
    import re
    ids: set[str] = set()
    for item in items:
        body = item.get("body") or item.get("title") or ""
        for sid in re.findall(r"<!subteam\^([A-Z0-9]+)", body):
            ids.add(sid)
    return ids


def _fetch_subteam_map(access_token: str) -> dict[str, str]:
    """Fetch a {subteam_id: handle} map via ``usergroups.list``.

    Handle (the @alias) is preferred over name for compact inline display.
    Returns an empty dict if the call fails or the token lacks the
    ``usergroups:read`` scope — callers fall back to the raw ID.

    ``include_disabled=true`` is important: archived/disabled groups still
    appear in historical messages as ``<!subteam^SID>``, and Slack excludes
    them by default, which produces ugly ``@SID`` renders for anything that
    was ever retired.
    """
    import logging
    import requests

    logger = logging.getLogger(__name__)
    try:
        resp = requests.get(
            "https://slack.com/api/usergroups.list",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"include_disabled": "true"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            # Surface the Slack error so users understand why subteam
            # mentions still show raw IDs (almost always missing scope).
            logger.warning(
                "usergroups.list failed: %s — add the 'usergroups:read' "
                "User Token Scope to your Slack app and reconnect.",
                data.get("error", "unknown"),
            )
            return {}
        out: dict[str, str] = {}
        for g in data.get("usergroups", []):
            gid = g.get("id")
            if gid:
                out[gid] = g.get("handle") or g.get("name") or gid
        return out
    except Exception as e:
        logger.warning("usergroups.list raised: %s", e)
        return {}


@st.cache_data(show_spinner=False, max_entries=200)
def _fetch_slack_image_bytes(url: str, token: str) -> bytes | None:
    """Fetch a Slack-hosted image with the user's Bearer token.

    Slack file URLs (`url_private`, `thumb_*`) are auth-gated — a plain
    `<img>` tag won't work. Cached per (url, token) so re-renders during the
    same Streamlit session don't re-download.
    """
    if not url or not token:
        return None
    try:
        import requests
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            allow_redirects=True,
        )
        if resp.status_code == 200 and resp.content:
            # Defensive: Slack sometimes returns the HTML login page (200) when
            # the token can't see the file. Skip if the body looks like HTML.
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("image/"):
                return resp.content
    except Exception:
        pass
    return None


def _render_slack_attachments(files: list[dict], slack_token: str | None) -> None:
    """Render image files inline and non-image files as compact links.

    Designed to be called inside whatever column the message body lives in
    so attachments visually belong to that message.
    """
    if not files:
        return
    images = [f for f in files if (f.get("mimetype") or "").startswith("image/")]
    others = [f for f in files if not (f.get("mimetype") or "").startswith("image/")]

    # ── Images ────────────────────────────────────────────────────────────────
    for f in images:
        # Prefer thumb (smaller, faster) over full url_private.
        url = f.get("thumb_url") or f.get("url_private")
        img_bytes = _fetch_slack_image_bytes(url, slack_token) if slack_token else None
        if img_bytes:
            st.image(img_bytes, caption=f.get("name") or None, width=420)
        elif f.get("permalink"):
            # Fallback: clickable link (will require Slack login in the browser).
            st.markdown(f"🖼️ [{f.get('name', 'image')}]({f['permalink']})")

    # ── Other files ──────────────────────────────────────────────────────────
    for f in others:
        link = f.get("permalink") or f.get("url_private")
        name = f.get("name", "file")
        if link:
            st.markdown(f"📎 [{name}]({link})")
        else:
            st.markdown(f"📎 {name}")


def _format_sender(item: dict, user_map: dict[str, str]) -> str:
    """Return a markdown-bold sender label for a Slack activity item.

    Falls back to the bot-repost username, then to "—" if neither is present
    (e.g. very old rows captured before sender enrichment landed).
    """
    sid = item.get("sender_id")
    sname = item.get("sender_name")
    if sid:
        return user_map.get(sid, sid)
    if sname:
        return sname
    return "—"


def _format_slack_text(
    text: str,
    user_map: dict[str, str],
    subteam_map: dict[str, str] | None = None,
) -> str:
    """Convert Slack mrkdwn codes in message body to readable markdown.

    Handles:
      <@USER_ID>                    → @display_name  (or @USER_ID if unknown)
      <#CHANNEL_ID|name>            → #name
      <#CHANNEL_ID>                 → #CHANNEL_ID
      <!subteam^SID|name>           → @name
      <!subteam^SID>                → @handle        (or @SID if unknown)
      <!here> / <!channel> / <!everyone> → @here / @channel / @everyone
      <URL|link text>               → [link text](URL)
      <URL>                         → URL (bare link)
    """
    import re

    # User mentions
    def _replace_user(m: re.Match) -> str:
        uid = m.group(1)
        return f"@{user_map.get(uid, uid)}"
    text = re.sub(r"<@([A-Z0-9]+)>", _replace_user, text)

    # Subteam (user-group) mentions  <!subteam^S123|name> or <!subteam^S123>
    def _replace_subteam(m: re.Match) -> str:
        sid, inline_name = m.group(1), m.group(2)
        if inline_name:
            return f"@{inline_name}"
        if subteam_map and sid in subteam_map:
            return f"@{subteam_map[sid]}"
        return f"@{sid}"
    text = re.sub(
        r"<!subteam\^([A-Z0-9]+)(?:\|([^>]+))?>", _replace_subteam, text,
    )

    # Broadcast mentions  <!here>, <!channel>, <!everyone> (with optional |label)
    text = re.sub(r"<!(here|channel|everyone)(?:\|[^>]+)?>", r"@\1", text)

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

# Pin widget state so it survives navigation away from this page. Streamlit
# garbage-collects widget keys for widgets that aren't currently rendered, so
# a fresh read-and-write keeps each key registered in session_state. Defaults
# are seeded here (not on the widgets) because Streamlit rejects widgets that
# pass both `value=`/`index=` and have a pre-set session_state value.
_wr_filter_defaults = {
    "wr_date_preset":  "Last 3 months",
    "wr_include_ai":   False,
    "wr_custom_start": date.today() - timedelta(days=90),
    "wr_custom_end":   date.today(),
}
for _k, _v in _wr_filter_defaults.items():
    st.session_state.setdefault(_k, _v)

for _persist_key in ("wr_team_member", *_wr_filter_defaults.keys()):
    if _persist_key in st.session_state:
        st.session_state[_persist_key] = st.session_state[_persist_key]

# ─── Auth check ──────────────────────────────────────────────────────────────

slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if not slack_user_id:
    st.warning("Please connect your Slack account first on the **Connect Accounts** page.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()

# ─── Controls ─────────────────────────────────────────────────────────────────

from app.ui.page_utils import loading_section

# ── Cache team options in session state ───────────────────────────────────────
# _get_team_options hits the DB every rerun — cache it so changing the date
# range, toggling AI insights, or clicking Generate doesn't re-query the team.
# Cache is keyed by (user_id, team_id) and invalidated on page refresh via the
# 🔄 button below.
def _load_report_members(user_id: str, team_id: str, self_name: str) -> dict:
    cache = st.session_state.get("_report_members_cache")
    if cache and cache["user_id"] == user_id and cache["team_id"] == team_id:
        return cache["options"]
    options = run(_get_team_options(user_id, team_id, self_name))
    st.session_state["_report_members_cache"] = {
        "user_id": user_id, "team_id": team_id, "options": options,
    }
    return options

col1, col2, col3 = st.columns([2, 2, 1])

with col1:
    self_name = st.session_state.get("slack_display_name", slack_user_id)
    _cache_hit = bool(
        st.session_state.get("_report_members_cache")
        and st.session_state["_report_members_cache"].get("user_id") == slack_user_id
    )
    if _cache_hit:
        user_options = _load_report_members(slack_user_id, slack_team_id, self_name)
    else:
        with loading_section("Loading team members…", n_skeleton_lines=1):
            user_options = _load_report_members(slack_user_id, slack_team_id, self_name)

    _rcol1, _rcol2 = st.columns([5, 1])
    with _rcol2:
        if st.button("🔄", key="refresh_report_members", help="Refresh team list"):
            st.session_state.pop("_report_members_cache", None)
            st.rerun()
    with _rcol1:
        _opts = list(user_options.keys())
        # Drop a stale persisted selection (member removed since last visit).
        if st.session_state.get("wr_team_member") not in _opts:
            st.session_state.pop("wr_team_member", None)
        selected_name = st.selectbox(
            "Team member",
            options=_opts,
            key="wr_team_member",
            help="Add team members on the Team Overview page.",
        )
    target_user_id = user_options[selected_name]

with col2:
    preset = st.selectbox(
        "Date range",
        ["Last 30 days", "Last 3 months", "Last 6 months", "Custom"],
        key="wr_date_preset",
    )

with col3:
    include_ai = st.toggle("AI insights", key="wr_include_ai")

# Custom date range
if preset == "Custom":
    c1, c2 = st.columns(2)
    start_date = c1.date_input("From", key="wr_custom_start")
    end_date = c2.date_input("To", key="wr_custom_end")
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time().replace(microsecond=0))
else:
    days = {"Last 30 days": 30, "Last 3 months": 90, "Last 6 months": 180}[preset]
    end_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    start_dt = end_dt - timedelta(days=days)

st.markdown("---")

# ─── Generate ─────────────────────────────────────────────────────────────────

if st.button("Generate Report", type="primary", use_container_width=False):
    with st.spinner("Building report..."):
        try:
            report = run(_get_report(target_user_id, slack_team_id, start_dt, end_dt, include_ai))
            st.session_state["last_report"] = report
            # New report invalidates the enrichment cache built for the prior one.
            st.session_state.pop("_report_enrichment", None)
        except Exception as e:
            st.error(f"Failed to generate report: {e}")
            st.stop()

report: WorkReport | None = st.session_state.get("last_report")

if not report:
    st.info("Select a team member and click **Generate Report**.")
    st.stop()

# ─── Display ──────────────────────────────────────────────────────────────────

# Load user map + token in one loading section — these run only when a new
# report was just generated. Re-visits reuse the cached enrichment so there's
# no spinner or redundant DB/API work on page navigation.
_enrichment = st.session_state.get("_report_enrichment")
# Bust a stale cache when we previously failed to resolve subteam mentions
# but the user has since reconnected Slack with the `usergroups:read` scope.
# Without this, the empty `subteam_map` persists for the life of the report
# and @group mentions keep rendering as raw IDs even after reconnect.
_enrichment_stale = bool(
    _enrichment
    and _enrichment.get("subteam_ids_seen")
    and not _enrichment.get("subteam_map")
)
_enrichment_matches = bool(
    _enrichment
    and _enrichment.get("report_id") == id(report)
    and not _enrichment_stale
)

if _enrichment_matches:
    _user_map    = _enrichment["user_map"]
    _channel_map = _enrichment["channel_map"]
    _subteam_map = _enrichment["subteam_map"]
    _slack_token = _enrichment["slack_token"]
    # Channel-name patching was already applied when the cache was built.
else:
  with loading_section("Preparing report display…", n_skeleton_lines=3):
    _user_map: dict[str, str] = run(_load_user_map(slack_team_id))

    _all_slack_msgs = [a for a in report.recent_activity if a.get("source") == "slack"]

    # Build initial channel map from what the DB already returned
    _channel_map: dict[str, str] = {
        a["slack_channel_id"]: a["channel_name"]
        for a in _all_slack_msgs
        if a.get("slack_channel_id") and a.get("channel_name") and a["channel_name"] != a["slack_channel_id"]
    }

    _unknown_user_ids = _collect_unknown_user_ids(_all_slack_msgs, _user_map)
    _unknown_channel_ids = {
        a["slack_channel_id"]
        for a in _all_slack_msgs
        if a.get("slack_channel_id") and a["slack_channel_id"] not in _channel_map
    }

    _subteam_map: dict[str, str] = {}
    _subteam_ids_seen: set[str] = set()

    # Always fetch the Slack token — needed for image attachment fetching
    # (Slack file URLs are bearer-auth gated). Enrichment of unknown users /
    # channels still piggybacks on the same token when present.
    _slack_token: str | None = None
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
            if _unknown_user_ids:
                _user_map = _enrich_user_map(_user_map, _unknown_user_ids, _slack_token)
            if _unknown_channel_ids:
                _channel_map = _enrich_channel_map(_channel_map, _unknown_channel_ids, _slack_token)
            # Subteam names — only fetched when at least one message references one.
            _subteam_ids_seen = _collect_subteam_ids(_all_slack_msgs)
            if _subteam_ids_seen:
                _subteam_map = _fetch_subteam_map(_slack_token)
                # Warn the user when we know which subteams are referenced
                # but couldn't resolve any — almost always the scope issue.
                _missing = _subteam_ids_seen - set(_subteam_map.keys())
                if _missing and not _subteam_map:
                    st.warning(
                        "Some @user-group mentions are showing as raw IDs "
                        "(e.g. `@S05…`). Add the **`usergroups:read`** "
                        "scope to your Slack app and click **Reconnect "
                        "Slack** on the 🔗 Connect Accounts page, then "
                        "regenerate this report."
                    )
    except Exception:
        pass  # enrichment is best-effort; falls back to raw ID on any error

    # Patch channel_name in activity items using the enriched channel map.
    # Done once, inside the enrichment branch, so cached re-reads don't
    # re-walk the list on every rerun.
    for _a in report.recent_activity:
        _cid = _a.get("slack_channel_id") or ""
        if _cid and _cid in _channel_map:
            _a["channel_name"] = _channel_map[_cid]

    st.session_state["_report_enrichment"] = {
        "report_id":        id(report),
        "user_map":         _user_map,
        "channel_map":      _channel_map,
        "subteam_map":      _subteam_map,
        "subteam_ids_seen": _subteam_ids_seen,
        "slack_token":      _slack_token,
    }

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

# ─── Developer Track ──────────────────────────────────────────────────────────
# Pulls the member's tab from the configured Google Sheet and renders each
# level with its skill statuses (color-coded) and manager notes. The section
# silently hides itself when the Sheets integration isn't configured.

from app.config import get_settings as _get_settings
from app.analytics.dev_track import (
    STATUS_LABELS as _DT_STATUS_LABELS,
    STATUS_ORDER as _DT_STATUS_ORDER,
    find_member_track as _dt_find_member_track,
)

_dt_settings = _get_settings()
if _dt_settings.google_sheets_credentials_json and _dt_settings.dev_track_sheet_id:
    # Fetch + cache the whole sheet once per session (one API call).
    _dt_cache_key = ("dev_track_tabs", _dt_settings.dev_track_sheet_id)
    _dt_tabs = st.session_state.get(_dt_cache_key)
    _dt_load_error: str | None = None
    if _dt_tabs is None:
        try:
            from app.integrations.google_sheets import fetch_all_tabs as _dt_fetch
            with loading_section("Loading developer track sheet…", n_skeleton_lines=2):
                _dt_tabs = _dt_fetch(_dt_settings.dev_track_sheet_id)
            st.session_state[_dt_cache_key] = _dt_tabs
        except Exception as e:
            _dt_load_error = str(e)

    st.markdown("## 📈 Developer Track (in progress)")
    if _dt_load_error:
        st.warning(
            f"Could not load the developer-track sheet: {_dt_load_error}"
        )
    else:
        _dt_track = _dt_find_member_track(
            _dt_tabs,
            report.user_display_name,
            report.user_real_name,
            report.user_email,
        )
        if _dt_track is None:
            st.info(
                f"No developer-track tab found for **{report.user_display_name}**. "
                "Make sure the sheet has a tab whose title contains the "
                "person's Slack display name."
            )
        else:
            _curr = _dt_track.current_level
            _hdr_cols = st.columns([2, 1])
            _hdr_cols[0].markdown(
                f"**Tab:** `{_dt_track.tab_title}`"
            )
            if _curr is not None:
                _hdr_cols[1].metric("Latest working level", _curr)

            # Render levels high → low, and hide fully-vetted levels so the
            # report focuses on what's still in progress or ahead.
            for _lv in sorted(_dt_track.levels, key=lambda l: l.level, reverse=True):
                if not _lv.skills:
                    continue
                _counts = _lv.counts
                _done = _counts["completed"]
                _total = len(_lv.skills)
                if _done == _total:
                    continue
                # Skip levels where nothing has been started yet — they're
                # aspirational rather than actionable for this report.
                if _counts["todo"] == _total:
                    continue
                _is_current = (_curr == _lv.level)
                _prefix = "⭐ " if _is_current else ""
                # Collapsible per-level card — mirrors the standups/messages
                # UX. The member's current level opens by default; the rest
                # collapse so the page stays scannable.
                _exp_label = (
                    f"{_prefix}Level {_lv.level} — "
                    f"{_lv.title or '(untitled)'}  ·  {_done}/{_total} vetted"
                )
                with st.expander(_exp_label, expanded=_is_current):
                    st.progress(
                        _done / _total if _total else 0.0,
                        text=f"{_done}/{_total} vetted",
                    )
                    # Status breakdown pills
                    _summary_parts = [
                        f"{_DT_STATUS_LABELS[s]}: **{_counts[s]}**"
                        for s in _DT_STATUS_ORDER if _counts[s]
                    ]
                    if _summary_parts:
                        st.caption(" · ".join(_summary_parts))

                    # Skills grouped by status — progress-first ordering.
                    # Skip "completed" (vetted) skills: the report focuses on
                    # what's still in progress or ahead, and the count + header
                    # already summarise how many have been vetted.
                    for _status in _DT_STATUS_ORDER:
                        if _status == "completed":
                            continue
                        _skills_here = [s for s in _lv.skills if s.status == _status]
                        if not _skills_here:
                            continue
                        st.markdown(f"**{_DT_STATUS_LABELS[_status]}**")
                        for _sk in _skills_here:
                            _lines = [f"- **{_sk.text}**"]
                            if _sk.note:
                                # Notes are bulleted lists where `-` marks a
                                # top-level item and `+` marks a sub-item under
                                # the previous `-`. We translate both into nested
                                # markdown lists: `-` → 4-space indent (child of
                                # the skill), `+` → 8-space indent (grandchild).
                                for _raw in _sk.note.splitlines():
                                    _s = _raw.strip()
                                    if not _s:
                                        continue
                                    if _s.startswith(("+ ", "+")):
                                        _s = _s[1:].lstrip()
                                        _indent = "        "  # 8 spaces
                                    elif _s.startswith(("- ", "* ")):
                                        _s = _s[2:].strip()
                                        _indent = "    "      # 4 spaces
                                    elif _s.startswith(("-", "*")):
                                        _s = _s[1:].strip()
                                        _indent = "    "
                                    else:
                                        _indent = "    "
                                    _lines.append(f"{_indent}- {_s}")
                            st.markdown("\n".join(_lines))

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
if getattr(report, "ai_error", ""):
    st.markdown("---")
    st.warning(
        f"⚠️ **AI insights unavailable** — {report.ai_error}\n\n"
        f"Turn off the **AI insights** toggle to generate the report without AI, "
        f"or top up your Anthropic credits and try again."
    )
elif report.ai_insights:
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

# ── PR links grouped by category (Created / Merged / Reviewed) ──────────────
_pr_created  = [a for a in github_items if a["type"] == "pr_opened"]
_pr_merged   = [a for a in github_items if a["type"] == "pr_merged"]
_pr_reviewed = [a for a in github_items if a["type"] == "pr_review"]

def _render_pr_links(label: str, items: list, icon: str) -> None:
    # De-dupe by URL base first — pr_review entries are per-review, collapse
    # to per-PR. Count the unique PRs so the expander header matches the
    # rendered list (otherwise "Reviewed (40)" shows 6 rows after dedupe).
    seen: set[str] = set()
    unique: list = []
    for it in sorted(items, key=lambda x: x.get("timestamp") or "", reverse=True):
        url = it.get("url") or ""
        key = url.split("#")[0] if url else (it.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    with st.expander(
        f"{icon} {label} ({len(unique)})",
        expanded=len(unique) > 0 and len(unique) <= 30,
    ):
        if not unique:
            st.caption("None in this period.")
            return
        for it in unique:
            url   = it.get("url") or ""
            repo  = f"`{it['github_repo']}`" if it.get("github_repo") else ""
            title = it.get("title") or "(no title)"
            ts    = it.get("timestamp", "")
            if url:
                st.markdown(f"- [{title}]({url}) &nbsp; {repo} &nbsp; · _{ts}_")
            else:
                st.markdown(f"- {title} &nbsp; {repo} &nbsp; · _{ts}_")

st.markdown("#### 🔗 Pull Request Links")
_render_pr_links("PRs Created (open)",  _pr_created,  "🔀")
_render_pr_links("PRs Merged",          _pr_merged,   "✅")
_render_pr_links("PRs Reviewed",        _pr_reviewed, "👀")

with st.expander(f"GitHub Activity ({len(github_items)} items)", expanded=False):
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
        for _i, item in enumerate(standups):
            ts      = item["timestamp"]
            sender  = _format_sender(item, _user_map)
            files   = item.get("files") or []
            raw_text = item["body"] or item["title"] or ""
            # Only show "(empty)" when there's also no attachment to render
            if not raw_text and not files:
                raw_text = "(empty)"
            body    = _format_standup_body(_format_slack_text(raw_text, _user_map, _subteam_map)) if raw_text else ""
            ch_name = item.get("channel_name") or item.get("slack_channel_id") or ""
            ch      = f"#{ch_name}" if ch_name else ""
            _hdr_col, _btn_col = st.columns([8, 1])
            header = f"· {ts} {ch}" if sender == "—" else f"**{sender}** · {ts} {ch}"
            _hdr_col.markdown(header)
            with _btn_col:
                # Copy button still copies just the text body (not the images)
                _copy_button(body or raw_text, key=f"copy_standup_{_i}")
            if body:
                st.markdown(body)
            _render_slack_attachments(files, _slack_token)
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
            fieldnames=["timestamp", "sender", "channel", "type", "body", "files", "url"],
            extrasaction="ignore",
        )
        _writer.writeheader()
        for _item in other_slack:
            _file_links = "; ".join(
                (f.get("permalink") or f.get("url_private") or "")
                for f in (_item.get("files") or [])
                if f.get("permalink") or f.get("url_private")
            )
            _writer.writerow({
                "timestamp": _item["timestamp"],
                "sender":    _format_sender(_item, _user_map),
                "channel":   _item.get("channel_name") or _item.get("slack_channel_id") or "",
                "type":      _item["type"],
                "body":      _item["body"] or _item["title"] or "",
                "files":     _file_links,
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

        _msg_idx = 0
        for _ch_name, _msgs in sorted(_by_channel.items()):
            st.markdown(f"**#{_ch_name}** &nbsp; <small>{len(_msgs)} message(s)</small>", unsafe_allow_html=True)
            for item in _msgs:
                icon   = _SLACK_ICONS.get(item["type"], "💬")
                ts     = item["timestamp"]
                sender = _format_sender(item, _user_map)
                files  = item.get("files") or []
                raw_text = item["body"] or item["title"] or ""
                # Don't print "(empty)" when there's at least one attachment
                if not raw_text and not files:
                    raw_text = "_(no text)_"
                body = _format_slack_text(raw_text, _user_map, _subteam_map) if raw_text else ""

                col_icon, col_body, col_ts, col_copy = st.columns([0.3, 5, 1.5, 0.7])
                col_icon.markdown(icon)
                with col_body:
                    # Sender on its own line above the body so it's always visible
                    st.markdown(f"**{sender}**")
                    if body:
                        st.markdown(body)
                    _render_slack_attachments(files, _slack_token)
                col_ts.caption(ts)
                with col_copy:
                    _copy_button(body or raw_text, key=f"copy_slack_{_msg_idx}")
                _msg_idx += 1
            st.markdown("---")

# ─── Share Summary ────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("📤 Share Summary")
st.caption("Copy this text to share via Slack, email, or a doc. Click the copy icon in the top-right of the block.")


def _build_share_text(r: "WorkReport") -> str:
    from app.ui.time_format import now_gmt7
    generated = now_gmt7().strftime("%b %d, %Y")

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

    # Standup summary: group by date, show only bullet answers (skip question headers)
    _standup_items = [
        a for a in (r.recent_activity or []) if a.get("type") == "standup"
    ]
    if _standup_items:
        import re as _re
        from collections import defaultdict as _dd

        def _bullets_only(text: str) -> list[str]:
            """Strip *question headers* and return only the bullet answer lines."""
            # Normalise newlines
            flat = _re.sub(r"\s*\n\s*", " ", text).strip()
            # Remove *bold question sections* (everything inside *...*)
            flat = _re.sub(r"\*[^*]+\*", "", flat)
            # Split on bullet markers and clean up
            parts = _re.split(r"\s*•\s*", flat)
            return [p.strip() for p in parts if p.strip()]

        # Group by calendar date (first 12 chars of "Apr 15, 2026 08:13" → "Apr 15, 2026")
        _by_date: dict[str, list[str]] = _dd(list)
        for _a in _standup_items:
            _date_key = _a["timestamp"].split(" ")[0:3]  # e.g. ["Apr", "15,", "2026"]
            _date_str = " ".join(_date_key)
            for _b in _bullets_only(_a.get("body") or _a.get("title") or ""):
                _by_date[_date_str].append(_b)

        # Deduplicate bullets within each day (same message sometimes ingested twice)
        lines += ["", "── RECENT STANDUPS ──────────────────"]
        for _date_str, _bullets in sorted(
            _by_date.items(),
            key=lambda kv: kv[0],
            reverse=True,
        ):
            seen: set[str] = set()
            unique = [b for b in _bullets if not (b in seen or seen.add(b))]  # type: ignore[func-returns-value]
            lines.append(f"  {_date_str}")
            for _b in unique:
                _truncated = _b[:200] + ("…" if len(_b) > 200 else "")
                lines.append(f"    • {_truncated}")

    lines += ["", "─" * 38]
    return "\n".join(lines)


st.code(_build_share_text(report), language="text")
