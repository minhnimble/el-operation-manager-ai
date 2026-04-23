"""
Notion Dev Track Sync page.

Reads per-developer track pages from a Notion database and writes skill
statuses, objectives, and evidence notes into the matching Google Sheet tab
(the same "Developer Track" sheet the Work Report already reads from).

Data flow
---------
1. **Preview** — click "Fetch from Notion" to pull all database entries + the
   current sheet state. The app parses each Notion page, fuzzy-matches the
   developer name to a sheet tab, derives skill statuses from objective
   phrasing, and computes the minimal cell diff.
2. **Diff view** — per-developer, see which cells would change (value, colour,
   note) and which Focus Areas bullets would be added/removed in Notion.
3. **Sync** — apply one member or all at once. Writes go to Google Sheets via
   ``batchUpdate`` (only changed cells) and to Notion via
   ``blocks.children.append`` / ``blocks.delete`` on Focus Areas bullets.

Notion is the source of truth. Skills that exist in the sheet but not in
Notion are left untouched — we never delete.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import streamlit as st

from app.streamlit_env import load_streamlit_secrets_into_env

load_streamlit_secrets_into_env()

from sqlalchemy import select

from app.analytics.dev_track import STATUS_LABELS, match_tab_to_member
from app.analytics.notion_sync import (
    MemberSyncPlan,
    MemberSyncResult,
    apply_all,
    apply_sync_plan,
    collect_sync_plan,
)
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.team_member import TeamMember
from app.ui.page_utils import inject_page_load_bar
from app.ui.session_cookie import restore_session_from_cookie


st.set_page_config(
    page_title="Notion Dev Track Sync", page_icon="📋", layout="wide"
)
inject_page_load_bar()
restore_session_from_cookie()


# ── Setup status ──────────────────────────────────────────────────────────────


st.title("📋 Notion Dev Track Sync")
st.caption(
    "Sync developer track data from a Notion database to the Google Sheet. "
    "Notion is the source of truth; the sheet is the snapshot."
)

settings = get_settings()

# NOTION_DEV_TRACK_VIEW_ID is optional — when set, we only pull pages that
# match the view's saved filter + sort. Shown for visibility but not required.
_config_rows = [
    ("NOTION_API_KEY",               bool(settings.notion_api_key), True),
    ("NOTION_DEV_TRACK_DATABASE_ID", bool(settings.notion_dev_track_database_id), True),
    ("NOTION_DEV_TRACK_VIEW_ID",     bool(settings.notion_dev_track_view_id), False),
    ("GOOGLE_SHEETS_CREDENTIALS_JSON", bool(settings.google_sheets_credentials_json), True),
    ("DEV_TRACK_SHEET_ID",           bool(settings.dev_track_sheet_id), True),
]
cols = st.columns(len(_config_rows))
for col, (label, ok, required) in zip(cols, _config_rows):
    if ok:
        icon = "✅"
    else:
        icon = "❌" if required else "⚪"
    suffix = "" if required else "  _(optional)_"
    col.markdown(f"{icon} `{label}`{suffix}")

if settings.notion_dev_track_view_id:
    st.caption(
        f"Filtering Notion database by view `{settings.notion_dev_track_view_id}` "
        "— only pages matching that view's saved filter + sort will be synced."
    )

if not all(ok for _, ok, required in _config_rows if required):
    st.warning(
        "Some configuration is missing. See **README → Notion Dev Track "
        "Sync** for setup instructions. The service account also needs "
        "**Editor** access on the sheet (Viewer isn't enough for writes)."
    )
    st.stop()


# ── Member selector (reports only — exclude self) ────────────────────────────


def _run_async(coro):
    """Run an async coroutine in a Streamlit callback."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


slack_user_id = st.session_state.get("slack_user_id")
slack_team_id = st.session_state.get("slack_team_id")

if not slack_user_id:
    st.warning("Please connect your Slack account first on **Connect Accounts**.")
    st.page_link("pages/1_Connect.py", label="Go to Connect Accounts")
    st.stop()


async def _load_reports(manager_user_id: str, manager_team_id: str) -> list[str]:
    """Display names of the manager's direct reports (no self)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(
                TeamMember.manager_slack_user_id == manager_user_id,
                TeamMember.manager_slack_team_id == manager_team_id,
            ).order_by(TeamMember.member_display_name)
        )
        return [m.display() for m in result.scalars().all()]


_REPORTS_CACHE_KEY = "_notion_reports_cache"


def _load_reports_cached(manager_user_id: str, manager_team_id: str) -> list[str]:
    """DB-lookup the team once per (user, team) and reuse across reruns.

    Without this, every multiselect tick / button click re-hits the DB just
    to redraw the "Sync for" widget. The only caller is this page; entries
    are keyed on both ids so account-switches still refresh the list.
    """
    cache = st.session_state.get(_REPORTS_CACHE_KEY)
    if (
        cache
        and cache["user_id"] == manager_user_id
        and cache["team_id"] == manager_team_id
    ):
        return cache["reports"]
    reports = _run_async(_load_reports(manager_user_id, manager_team_id))
    st.session_state[_REPORTS_CACHE_KEY] = {
        "user_id": manager_user_id,
        "team_id": manager_team_id,
        "reports": reports,
    }
    return reports


_reports = _load_reports_cached(slack_user_id, slack_team_id)

if not _reports:
    st.warning("No team members found. Add reports on **Team Overview** first.")
    st.stop()

if "notion_member_select" not in st.session_state:
    st.session_state["notion_member_select"] = _reports

_sel_col1, _sel_col2, _sel_col3 = st.columns([5, 1, 1])
with _sel_col2:
    if st.button("All", use_container_width=True, key="_notion_all"):
        st.session_state["notion_member_select"] = _reports
        st.rerun()
with _sel_col3:
    if st.button("Clear", use_container_width=True, key="_notion_clear"):
        st.session_state["notion_member_select"] = []
        st.rerun()
with _sel_col1:
    selected_members: list[str] = st.multiselect(
        "Sync for",
        options=_reports,
        key="notion_member_select",
        help="Pick which reports to sync. Notion entries are matched to the "
             "selected members the same way Slack maps to Google Sheet tabs "
             "(fuzzy name match).",
    )

if not selected_members:
    st.info("Select at least one report above to sync.")
    st.stop()


# ── Session state plumbing ───────────────────────────────────────────────────


_FETCH_KEY = "notion_sync_plans"
_RESULTS_KEY = "notion_sync_results"
_LAST_FETCH_KEY = "notion_sync_last_fetch"
# Snapshot of the member multiselect taken at the moment of fetch. The
# preview/diff below filters against this snapshot, not the live widget
# value, so ticking members on/off doesn't redraw the preview until the
# user explicitly clicks "Fetch from Notion" again.
_SNAPSHOT_KEY = "notion_sync_members_snapshot"


def _col_letter(idx: int) -> str:
    """Zero-based column index → spreadsheet letter (0→A, 1→B, 26→AA, …)."""
    s = ""
    n = idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _skill_type_label(col_idx: int) -> str:
    """Map the sheet column holding a skill to its dev-track category.

    The dev-track sheet puts technical skills in column C (``col_idx=2``)
    and soft skills in column D (``col_idx=3``). Any other column is an
    unexpected layout; fall back to the literal column letter so the diff
    still reads sensibly if the sheet is ever extended.
    """
    if col_idx == 2:
        return "Technical"
    if col_idx == 3:
        return "Soft"
    return f"Col {_col_letter(col_idx)}"


def _fetch_plans() -> None:
    """Fetch Notion + sheet data, build sync plans, cache in session state.

    Also snapshots the *current* member multiselect so the preview/diff
    stays pinned to what the user asked for at fetch time. Toggling the
    multiselect afterwards is a no-op on the preview until the next fetch.
    """
    snapshot = list(st.session_state.get("notion_member_select") or [])
    tabs, plans = _run_async(
        collect_sync_plan(
            spreadsheet_id=settings.dev_track_sheet_id,
            database_id=settings.notion_dev_track_database_id,
            view_id=settings.notion_dev_track_view_id or None,
        )
    )
    st.session_state[_FETCH_KEY] = plans
    st.session_state["notion_sync_all_tabs"] = tabs
    st.session_state[_LAST_FETCH_KEY] = datetime.now(timezone.utc)
    st.session_state[_SNAPSHOT_KEY] = snapshot
    # New fetch invalidates old results.
    st.session_state.pop(_RESULTS_KEY, None)


# ── Fetch button ─────────────────────────────────────────────────────────────


fetch_col, last_col = st.columns([1, 3])
with fetch_col:
    if st.button("🔄 Fetch from Notion", type="primary", use_container_width=True):
        try:
            with st.spinner("Fetching Notion database + sheet…"):
                _fetch_plans()
        except Exception as e:
            st.error(f"Fetch failed: {type(e).__name__}: {e}")
with last_col:
    last_fetch = st.session_state.get(_LAST_FETCH_KEY)
    if last_fetch:
        st.caption(
            f"Last fetched: {last_fetch.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

plans: list[MemberSyncPlan] | None = st.session_state.get(_FETCH_KEY)

if not plans:
    st.info("Click **Fetch from Notion** to preview what would be synced.")
    st.stop()

# Filter plans → keep only Notion entries that match a member captured in
# the snapshot taken when the user last clicked "Fetch from Notion". The
# live multiselect value is deliberately ignored here so ticking a name on
# or off doesn't redraw the preview/diff. A separate nudge below tells the
# user to re-fetch when the live selection has drifted from the snapshot.
# Uses `match_tab_to_member` (same fuzzy matcher as Slack→sheet-tab mapping)
# so a Notion page like "Don <> Mike" matches the member "Don Pham".
def _plan_matches_any(plan: MemberSyncPlan, names: list[str]) -> bool:
    candidate = plan.dev_name or plan.notion_page_title or ""
    return any(match_tab_to_member(candidate, n) for n in names)

# Fall back to the live selection only if we somehow have plans without a
# snapshot (shouldn't happen in normal flow — _fetch_plans always writes
# both — but keeps the page defensive across session migrations).
snapshot_members: list[str] = st.session_state.get(_SNAPSHOT_KEY) or selected_members

if set(snapshot_members) != set(selected_members):
    st.info(
        "ℹ️ Member selection has changed since the last fetch — click "
        "**🔄 Fetch from Notion** above to refresh the preview."
    )

_unfiltered_count = len(plans)
plans = [p for p in plans if _plan_matches_any(p, snapshot_members)]

if not plans:
    st.warning(
        f"None of the {_unfiltered_count} Notion entries matched the "
        f"member(s) captured at fetch time: {', '.join(snapshot_members) or '—'}. "
        "Check that the Notion page titles include the member's name."
    )
    st.stop()
else:
    st.caption(
        f"Showing {len(plans)} of {_unfiltered_count} Notion entries "
        f"matching {len(snapshot_members)} member(s) from the last fetch."
    )


# ── Preview table ────────────────────────────────────────────────────────────


st.markdown("## Preview")

preview_rows = []
for p in plans:
    levels_count = len(p.notion_track.levels)
    skills_count = sum(len(lv.skills) for lv in p.notion_track.levels)
    if p.error:
        match_status = f"⚠️ {p.error}"
    elif p.sheet_tab is None:
        match_status = "❌ no sheet tab matched"
    elif p.is_actionable:
        match_status = "🔵 changes pending"
    else:
        match_status = "✅ in sync"
    preview_rows.append({
        "Developer":         p.dev_name,
        "Notion page":       p.notion_page_title,
        "Sheet tab":         p.matched_tab_title or "—",
        "Levels":            levels_count,
        "Skills":            skills_count,
        "Cell updates":      len(p.updates),
        "Focus + / −":       f"{len(p.focus_areas_to_add)} / {len(p.focus_areas_to_remove)}",
        "Status":            match_status,
    })

st.dataframe(preview_rows, use_container_width=True, hide_index=True)

_actionable = [p for p in plans if p.is_actionable]
_summary_cols = st.columns(4)
_summary_cols[0].metric("Members",           len(plans))
_summary_cols[1].metric("Actionable",        len(_actionable))
_summary_cols[2].metric("Total cell updates", sum(len(p.updates) for p in _actionable))
_summary_cols[3].metric(
    "Focus Areas changes",
    sum(len(p.focus_areas_to_add) + len(p.focus_areas_to_remove) for p in _actionable),
)


# ── Diff view ────────────────────────────────────────────────────────────────


st.markdown("## Diff view")

_dev_options = [p.dev_name for p in plans]
if _dev_options:
    selected_name = st.selectbox("Developer", _dev_options, index=0)
    selected_plan = next(p for p in plans if p.dev_name == selected_name)

    if selected_plan.error:
        st.error(selected_plan.error)
    elif selected_plan.sheet_tab is None:
        st.warning(
            f"No sheet tab matched `{selected_plan.dev_name}`. "
            "Rename a tab to include the developer's name (the part before "
            "` <> ` in the Notion page title)."
        )
    else:
        st.caption(
            f"Notion: `{selected_plan.notion_page_title}`  →  "
            f"Sheet tab: `{selected_plan.sheet_tab.title}`"
        )

        if not selected_plan.updates:
            st.success("No cell changes needed — sheet matches Notion.")
        else:
            diff_rows = []
            for upd in selected_plan.updates:
                # `level` was added to CellUpdate after initial release.
                # Use getattr so sessions that still hold pre-upgrade
                # plan objects in session state don't crash — they just
                # render a blank level until the next Fetch from Notion.
                level_num = getattr(upd, "level", 0) or ""
                diff_rows.append({
                    "Level":  level_num,
                    "Type":   _skill_type_label(upd.col_idx),
                    "Skill":  upd.value,
                    "Status": STATUS_LABELS.get(upd.status, upd.status),
                    "Change": upd.reason,
                    "Note":   (upd.note or "").replace("\n\n", " ⏎ ").replace("\n", " │ "),
                })
            st.dataframe(diff_rows, use_container_width=True, hide_index=True)

        if selected_plan.focus_areas_to_add or selected_plan.focus_areas_to_remove:
            st.markdown("**Notion Focus Areas changes**")
            fa_cols = st.columns(2)
            with fa_cols[0]:
                st.caption("➕ To add")
                if selected_plan.focus_areas_to_add:
                    for s in selected_plan.focus_areas_to_add:
                        st.markdown(f"- {s}")
                else:
                    st.caption("_(none)_")
            with fa_cols[1]:
                st.caption("➖ To remove")
                if selected_plan.focus_areas_to_remove:
                    for s in selected_plan.focus_areas_to_remove:
                        st.markdown(f"- {s}")
                else:
                    st.caption("_(none)_")


# ── Sync controls ────────────────────────────────────────────────────────────


st.markdown("## Sync")

sync_cols = st.columns([1, 1, 2])
with sync_cols[0]:
    sync_one = st.button(
        f"🚀 Sync only `{selected_plan.dev_name}`"
        if _dev_options else "🚀 Sync selected",
        disabled=not _actionable or not selected_plan.is_actionable,
        use_container_width=True,
    )
with sync_cols[1]:
    sync_all = st.button(
        f"🚀 Sync all ({len(_actionable)})",
        type="primary",
        disabled=not _actionable,
        use_container_width=True,
    )

if sync_one:
    # `st.status` gives a live, phase-aware indicator: write → refresh →
    # done. Without it, the sync write shows a spinner but the subsequent
    # _fetch_plans() re-hits Notion + Sheets silently, leaving the user
    # staring at an unchanging page.
    try:
        with st.status(
            f"Syncing {selected_plan.dev_name}…", expanded=True
        ) as status:
            status.write("📝 Writing changes to Google Sheet…")
            result = _run_async(
                apply_sync_plan(settings.dev_track_sheet_id, selected_plan)
            )
            if result.error:
                status.write(f"⚠️ {result.error}")
            else:
                status.write(
                    f"✅ Wrote {result.cells_updated} cell(s) · "
                    f"Focus Areas +{result.focus_areas_added} "
                    f"−{result.focus_areas_removed}"
                )
            status.update(label="🔄 Refreshing preview from Notion + Sheet…")
            _fetch_plans()
            status.update(
                label=f"Sync complete for {selected_plan.dev_name}",
                state="complete",
            )
        st.session_state[_RESULTS_KEY] = [result]
        st.rerun()
    except Exception as e:
        st.error(f"Sync failed: {type(e).__name__}: {e}")

if sync_all:
    try:
        with st.status(
            f"Syncing {len(_actionable)} member(s)…", expanded=True
        ) as status:
            progress = st.progress(0.0, text="Starting…")

            def _on_progress(done: int, total: int) -> None:
                pct = done / total if total else 1.0
                progress.progress(pct, text=f"Synced {done}/{total}")

            results = _run_async(
                apply_all(settings.dev_track_sheet_id, plans, _on_progress)
            )
            ok = sum(1 for r in results if not r.error)
            failed = len(results) - ok
            cells_total = sum(r.cells_updated for r in results)
            fa_add_total = sum(r.focus_areas_added for r in results)
            fa_rm_total = sum(r.focus_areas_removed for r in results)
            status.write(
                f"✅ {ok}/{len(results)} member(s) synced · "
                f"{cells_total} cell(s) · Focus Areas "
                f"+{fa_add_total} −{fa_rm_total}"
                + (f" · ⚠️ {failed} failed" if failed else "")
            )
            status.update(label="🔄 Refreshing preview from Notion + Sheet…")
            _fetch_plans()
            status.update(
                label=f"Sync complete ({ok}/{len(results)})",
                state="complete" if failed == 0 else "error",
            )
        st.session_state[_RESULTS_KEY] = results
        st.rerun()
    except Exception as e:
        st.error(f"Sync failed: {type(e).__name__}: {e}")


# ── Results ──────────────────────────────────────────────────────────────────


results: list[MemberSyncResult] | None = st.session_state.get(_RESULTS_KEY)
if results:
    st.markdown("## Last sync results")
    result_rows = []
    total_cells = 0
    total_fa_add = 0
    total_fa_remove = 0
    for r in results:
        total_cells += r.cells_updated
        total_fa_add += r.focus_areas_added
        total_fa_remove += r.focus_areas_removed
        result_rows.append({
            "Developer":          r.dev_name,
            "Cells updated":      r.cells_updated,
            "Focus Areas added":  r.focus_areas_added,
            "Focus Areas removed": r.focus_areas_removed,
            "Status":             "❌ " + r.error if r.error else "✅ ok",
            "At":                 r.timestamp.strftime("%H:%M:%S UTC"),
        })
    st.dataframe(result_rows, use_container_width=True, hide_index=True)

    summary_cols = st.columns(3)
    summary_cols[0].metric("Total cells written",     total_cells)
    summary_cols[1].metric("Focus Areas added",       total_fa_add)
    summary_cols[2].metric("Focus Areas removed",     total_fa_remove)
