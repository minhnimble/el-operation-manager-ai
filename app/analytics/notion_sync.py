"""Notion → DevTrack sync orchestration.

Ties the three moving parts together:

1. **Read Notion** — fetch the database entries and each page's block tree
   (``app/integrations/notion.py``).
2. **Parse + diff** — convert each page into a ``NotionDevTrack``, match it
   to the right Google Sheet tab (fuzzy name match via ``dev_track``), and
   compute the minimal set of cell updates
   (``app/integrations/google_sheets.py``).
3. **Apply** — batch-write the cell updates to the sheet AND update Notion's
   ``## Focus Areas`` section so it reflects the developer's current focus.

Two independent diffs come out of step 2, with deliberately different
sources of truth:

* **Cell updates** (Google Sheets) are driven by the full status derivation
  in ``notion_dev_track_parser`` — sheet colour + Notion objectives, rules
  1–5, so the sheet ends up showing the right colour / note / value.
* **Focus Areas diff** (Notion) is driven **only** by Notion's Skills
  Development objectives — see ``_compute_focus_area_diff``. The Google
  Sheet is never read for this decision, which keeps the Focus Areas list
  a faithful mirror of what the developer says they're working on in
  Notion rather than an echo of the sheet's colour state.

Call ``collect_sync_plan`` first to build a preview (no side effects), then
``apply_sync_plan`` to actually write. The Streamlit page uses the preview
step for the diff view and only calls apply on user confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.analytics.dev_track import (
    SkillStatus,
    match_tab_to_member,
    parse_tab,
)
from app.analytics.notion_dev_track_parser import (
    NotionDevTrack,
    parse_dev_track_page,
)
from app.integrations import notion as notion_api
from app.integrations.google_sheets import (
    CellUpdate,
    SheetTab,
    apply_cell_updates,
    compute_cell_updates,
    fetch_all_tabs,
)


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class MemberSyncPlan:
    """Everything needed to sync one developer, computed ahead of time.

    ``updates`` is what would be written to the Google Sheet; the two
    ``focus_areas_*`` sets are the skill names to add/remove in the Notion
    page's Focus Areas section. Each is independent — a member with no cell
    updates but a focus-areas drift will still produce a non-empty plan.
    """

    dev_name: str
    notion_page_id: str
    notion_page_title: str
    notion_track: NotionDevTrack
    sheet_tab: SheetTab | None
    matched_tab_title: str | None
    updates: list[CellUpdate] = field(default_factory=list)
    focus_areas_to_add: list[str] = field(default_factory=list)
    focus_areas_to_remove: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def is_actionable(self) -> bool:
        return (
            self.error is None
            and self.sheet_tab is not None
            and (
                bool(self.updates)
                or bool(self.focus_areas_to_add)
                or bool(self.focus_areas_to_remove)
            )
        )


@dataclass
class MemberSyncResult:
    """Outcome of applying one ``MemberSyncPlan``."""

    dev_name: str
    cells_updated: int = 0
    focus_areas_added: int = 0
    focus_areas_removed: int = 0
    error: str | None = None
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── Plan building ────────────────────────────────────────────────────────────


def _match_sheet_tab(
    dev_name: str,
    tabs: list[SheetTab],
) -> SheetTab | None:
    """Find the first sheet tab whose title matches the dev name.

    Reuses ``dev_track.match_tab_to_member``, the same fuzzy matcher that the
    Work Report uses. ``extract_dev_name`` has already stripped the ``<> Mike``
    suffix so we pass just ``"Don"``.
    """
    if not dev_name:
        return None
    for tab in tabs:
        if match_tab_to_member(tab.title, dev_name):
            return tab
    return None


def _compute_focus_area_diff(
    track: NotionDevTrack,
) -> tuple[list[str], list[str]]:
    """Derive which skill names need to be added/removed from Focus Areas.

    **Pure-Notion decision.** The Google Sheet is never consulted here. A
    skill belongs in Focus Areas iff one of its unchecked to-dos in the
    ``## Skills Development`` section matches the focus constraint
    (V-ing / ``In-progress objective:`` / ``New objective:``) — see
    ``notion_dev_track_parser._has_focus_intent``. The sheet's colour /
    derived status is deliberately ignored so a green cell in the sheet
    can't stop a genuinely-focused skill from appearing, and a blue cell
    can't keep a demoted skill pinned.

    Diff rules:

    * Skill with focus intent + not already in Focus Areas → **add**.
    * Skill **without** focus intent + already in Focus Areas → **remove**
      (it was presumably added back when it had an active objective).
    * A bullet in Focus Areas that doesn't correspond to any skill in
      Skills Development is left alone — it was added by hand and isn't
      ours to touch.

    Comparison keys go through ``notion_api.normalize_skill_text`` so
    trivial drift (trailing period, case, whitespace) can't produce a
    phantom "to add" that the write step would silently skip. ``to_add``
    strings are stripped of any trailing ``.!?…`` so new bullets follow
    the Focus Areas convention of short phrases without sentence
    terminators. ``to_remove`` echoes the exact Notion bullet text so the
    diff view matches what the user sees on the page.
    """
    active_by_key: dict[str, str] = {}
    for raw in track.skills_with_focus_intent:
        text = notion_api.strip_focus_terminator(raw)
        key = notion_api.normalize_skill_text(text)
        if key:
            active_by_key[key] = text

    # Notion-tracked skills that did *not* get focus intent. These are the
    # only skills we're allowed to remove from Focus Areas; anything else in
    # Focus Areas was put there by hand and stays untouched.
    passive_keys: set[str] = set()
    for raw in track.all_skill_texts:
        key = notion_api.normalize_skill_text(notion_api.strip_focus_terminator(raw))
        if key and key not in active_by_key:
            passive_keys.add(key)

    current_by_key: dict[str, str] = {}
    for raw in track.focus_skill_names:
        key = notion_api.normalize_skill_text(raw)
        if key:
            current_by_key[key] = (raw or "").strip()

    to_add = sorted(
        text for key, text in active_by_key.items() if key not in current_by_key
    )
    to_remove = sorted(
        current_by_key[key] for key in passive_keys if key in current_by_key
    )
    return to_add, to_remove


async def collect_sync_plan(
    spreadsheet_id: str,
    database_id: str,
    view_id: str | None = None,
) -> tuple[list[SheetTab], list[MemberSyncPlan]]:
    """Build one ``MemberSyncPlan`` per developer in the Notion database.

    Side-effect-free: fetches from Notion and Google Sheets, parses everything,
    computes diffs. No writes. Suitable for populating a preview UI.

    If ``view_id`` is provided, only the pages matching that Notion view's
    saved filter + sort are processed. Otherwise every page in the database
    is considered.

    Returns ``(all_sheet_tabs, plans)`` so callers can also show a list of
    "members in the sheet but not in Notion" (tabs with no matching plan).
    """
    entries = await notion_api.fetch_database_entries(
        database_id, view_id=view_id
    )
    tabs = fetch_all_tabs(spreadsheet_id)

    plans: list[MemberSyncPlan] = []
    for entry in entries:
        page_id = entry.get("id", "")
        title = notion_api._page_title(entry)  # noqa: SLF001 — tight coupling is intentional
        dev_name = notion_api.extract_dev_name(title)

        plan = MemberSyncPlan(
            dev_name=dev_name or title,
            notion_page_id=page_id,
            notion_page_title=title,
            notion_track=NotionDevTrack(  # placeholder, overwritten below on success
                dev_name=dev_name,
                page_id=page_id,
                page_title=title,
            ),
            sheet_tab=None,
            matched_tab_title=None,
        )

        try:
            blocks = await notion_api.fetch_page_blocks(page_id)
            sheet_tab = _match_sheet_tab(dev_name, tabs)
            plan.sheet_tab = sheet_tab
            plan.matched_tab_title = sheet_tab.title if sheet_tab else None

            track = parse_dev_track_page(
                page_title=title,
                page_id=page_id,
                blocks=blocks,
                current_sheet_tab=sheet_tab,
            )
            plan.notion_track = track

            if sheet_tab is not None:
                plan.updates = compute_cell_updates(track.levels, sheet_tab)
                add, remove = _compute_focus_area_diff(track)
                plan.focus_areas_to_add = add
                plan.focus_areas_to_remove = remove
            # else: leave updates empty — the UI will show "no match" status
        except Exception as e:
            plan.error = f"{type(e).__name__}: {e}"

        plans.append(plan)

    return tabs, plans


# ── Plan application ─────────────────────────────────────────────────────────


async def apply_sync_plan(
    spreadsheet_id: str,
    plan: MemberSyncPlan,
) -> MemberSyncResult:
    """Apply one member's sync plan.

    Steps (independent; partial success is possible):
    1. Write cell updates to Google Sheets via ``batchUpdate``.
    2. Append bulleted_list_items under Notion's "Focus Areas" heading for
       every new active skill.
    3. Delete existing Focus Areas bullets for demoted skills.

    The first exception stops the run and is captured on the result. Prior
    steps that did succeed keep their counts — the UI surfaces both so the
    user can retry meaningfully.
    """
    result = MemberSyncResult(dev_name=plan.dev_name)

    if plan.error:
        result.error = plan.error
        return result
    if plan.sheet_tab is None:
        result.error = "No matching Google Sheet tab"
        return result

    try:
        # 1. Sheet writes
        if plan.updates:
            result.cells_updated = apply_cell_updates(
                spreadsheet_id=spreadsheet_id,
                tab_title=plan.sheet_tab.title,
                updates=plan.updates,
            )

        # 2 + 3. Notion Focus Areas updates — need the latest block tree so
        # we don't duplicate bullets or try to delete blocks that were
        # already removed by a concurrent edit.
        if plan.focus_areas_to_add or plan.focus_areas_to_remove:
            blocks = await notion_api.fetch_page_blocks(plan.notion_page_id)

            for skill in plan.focus_areas_to_add:
                added = await notion_api.add_skill_to_focus_areas(
                    plan.notion_page_id, blocks, skill
                )
                if added:
                    result.focus_areas_added += 1

            for skill in plan.focus_areas_to_remove:
                removed = await notion_api.remove_skill_from_focus_areas(
                    plan.notion_page_id, blocks, skill
                )
                if removed:
                    result.focus_areas_removed += 1
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"

    return result


# ── Bulk helpers ─────────────────────────────────────────────────────────────


async def apply_all(
    spreadsheet_id: str,
    plans: list[MemberSyncPlan],
    progress_cb=None,
) -> list[MemberSyncResult]:
    """Apply every actionable plan sequentially; return one result per member.

    Sequential (not parallel) to keep the Google Sheets API well under its
    quota and to avoid concurrent Notion mutations on the same page — Notion's
    write API isn't strongly transactional, so serialising is the safe default.

    ``progress_cb(done, total)`` is invoked after each member if provided;
    used by the Streamlit page to drive the progress bar.
    """
    actionable = [p for p in plans if p.is_actionable]
    results: list[MemberSyncResult] = []
    total = len(actionable)
    for i, plan in enumerate(actionable, start=1):
        result = await apply_sync_plan(spreadsheet_id, plan)
        results.append(result)
        if progress_cb:
            try:
                progress_cb(i, total)
            except Exception:
                # Progress callbacks are best-effort — never let a UI glitch
                # abort a partially completed sync.
                pass
    return results
