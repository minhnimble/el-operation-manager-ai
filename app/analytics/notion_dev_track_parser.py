"""Notion → DevTrack parser.

Converts a Notion page's block tree (see ``app/integrations/notion.py``) into
the same ``TrackLevel`` / ``Skill`` dataclasses already used by the Google
Sheets reader (``app/analytics/dev_track.py``), so the downstream diff and
display code can treat both sources uniformly.

Expected Notion page layout (see README → "Notion Dev Track Sync"):

    ## Focus Areas
    - Skill name A          ← bulleted_list_item (current focus list)
    - Skill name B

    ## Skills Development
    ### Level 5             ← heading_3 (level header)
    - **Skill name**        ← toggle (or bulleted_list_item with bold text)
        - [ ] Objective     ← to_do (unchecked)
            + Evidence      ← bulleted_list_item (nested)
        - [x] Completed X   ← to_do (checked)

Status is derived by analysing the **unchecked** to-do text rather than simple
checkbox counting, because the objective phrasing itself encodes intent:

* ``"Working as a Flutter developer"``  → V-ing prefix → in_progress
* ``"New objective: Deliver ..."``      → focus (ready to start)
* ``"Completed objective: Read ..."``   → past form → todo (if downgrading)

See ``_derive_status_from_objectives`` for the full 5-rule priority chain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.analytics.dev_track import Skill, SkillStatus, TrackLevel
from app.integrations.google_sheets import SheetTab
from app.integrations.notion import NotionBlock, extract_dev_name


# ── Objective-phrasing detection ─────────────────────────────────────────────


# Ordered longest-first so "in-progress objective" wins over "in-review objective"
# etc. — the first prefix that matches is stripped.
_OBJECTIVE_PREFIXES: tuple[str, ...] = (
    "in-progress objective:",
    "in-review objective:",
    "to-review objective:",
    "completed objective:",
    "discarded objective:",
    "failed objective:",
    "new objective:",
)


def _strip_objective_prefix(text: str) -> str:
    """Remove a leading objective prefix (case-insensitive) if present."""
    stripped = text.lstrip()
    lowered = stripped.lower()
    for prefix in _OBJECTIVE_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix):].lstrip()
    return stripped


def _starts_with_prefix(text: str, *prefixes: str) -> bool:
    lowered = text.lstrip().lower()
    return any(lowered.startswith(p) for p in prefixes)


def _is_in_progress_text(text: str) -> bool:
    """True if the objective phrasing indicates active work.

    Triggers:
    * Explicit "In-progress objective:" / "In-review objective:" prefix.
    * After prefix stripping, the first word is a V-ing form (>3 chars,
      ends in "ing").
    """
    if _starts_with_prefix(text, "in-progress objective:", "in-review objective:"):
        return True
    clean = _strip_objective_prefix(text).strip()
    if not clean:
        return False
    first_word = clean.split()[0]
    # Trim trailing punctuation that could throw off the -ing check.
    first_word = re.sub(r"[^a-zA-Z]+$", "", first_word)
    return len(first_word) > 3 and first_word.lower().endswith("ing")


def _is_new_objective(text: str) -> bool:
    return _starts_with_prefix(text, "new objective:")


def _has_focus_intent(todos: list[NotionBlock]) -> bool:
    """Pure-Notion signal for "this skill belongs in Focus Areas".

    Mirrors Rules 1 + 2 of ``_derive_status_from_objectives`` but **without**
    any Google Sheet context. The Focus Areas sync is defined off this helper
    (see ``notion_sync._compute_focus_area_diff``) so the sheet's derived
    colour state cannot drag a skill into — or out of — Focus Areas behind
    the user's back.

    Returns True iff any direct unchecked to-do under the skill is phrased as:

    * an in-progress objective (V-ing first word, or an explicit
      ``In-progress objective:`` / ``In-review objective:`` prefix), **or**
    * a ``New objective:`` — a focus area the developer has committed to but
      not yet started.

    Checked to-dos, paragraph-only "TBD" skills, and past-form completed
    objectives all yield False.
    """
    for td in todos:
        if td.checked:
            continue
        if _is_in_progress_text(td.text) or _is_new_objective(td.text):
            return True
    return False


# ── Status derivation ────────────────────────────────────────────────────────


def _derive_status_from_objectives(
    todos: list[NotionBlock],
    current_sheet_status: SkillStatus | None,
) -> SkillStatus:
    """Five-rule priority chain (see README / plan for the full table).

    Parameters
    ----------
    todos
        The direct ``to_do`` children of the skill block. Deeper nested blocks
        (evidence bullets) don't count toward status derivation.
    current_sheet_status
        The status the skill currently has in the Google Sheet, if any. Used
        to honour rule #4 (never demote green/purple) and to gate rule #3
        (only downgrade blue/yellow when all objectives are past-form).
    """
    # Rule 1 (highest priority): any unchecked in-progress phrasing.
    for td in todos:
        if td.checked:
            continue
        if _is_in_progress_text(td.text):
            return "in_progress"

    # Rule 2: any unchecked "New objective:" (and no V-ing from rule 1).
    for td in todos:
        if td.checked:
            continue
        if _is_new_objective(td.text):
            return "focus"

    # Rule 4: never demote completed/proposed — the sheet wins.
    if current_sheet_status in ("completed", "proposed"):
        return current_sheet_status

    # Rule 3: all checked / past-form → downgrade to todo, but only if the
    # sheet was currently blue or yellow. Otherwise fall through to the sheet
    # value (preserve "todo" as "todo", etc.).
    if todos and all(td.checked for td in todos):
        if current_sheet_status in ("in_progress", "focus"):
            return "todo"
        return current_sheet_status or "todo"

    # Rule 5: no to-dos, or we fell through — default to the current status
    # if present, else "todo".
    return current_sheet_status or "todo"


# ── Note formatting (Notion blocks → "- / +" cell-note format) ───────────────


def _format_note(todos: list[NotionBlock]) -> str | None:
    """Render to-do blocks + their evidence children as multi-line note text.

    * Each ``to_do`` becomes a ``"- {text}"`` line (the existing sheet format;
      rendered in the Work Report as a 4-space-indented markdown bullet).
    * Direct ``bulleted_list_item`` children of a to-do become ``"+ {text}"``.
    * Deeper nesting is flattened to the same ``"+"`` prefix — the Work Report
      rendering only distinguishes two indent levels (``-`` vs ``+``).

    Returns ``None`` when there's nothing to render so callers can skip writing
    an empty cell note.
    """
    groups: list[list[str]] = []
    for td in todos:
        text = td.text.strip()
        if not text:
            continue
        group: list[str] = [f"- {text}"]
        _append_nested(td.children, group)
        groups.append(group)
    if not groups:
        return None
    # One blank line between items; sub-items stay directly under their parent.
    return "\n\n".join("\n".join(g) for g in groups)


def _append_nested(blocks: list[NotionBlock], lines: list[str]) -> None:
    """Recursively append ``+ {text}`` lines for every nested bullet."""
    for b in blocks:
        # Accept any nested list-ish block as evidence; skip empty paragraphs.
        if b.type in ("bulleted_list_item", "numbered_list_item", "paragraph"):
            text = b.text.strip()
            if text:
                lines.append(f"+ {text}")
        # to_do children can also appear (rarely) — treat them as evidence too.
        elif b.type == "to_do":
            text = b.text.strip()
            if text:
                lines.append(f"+ {text}")
        if b.children:
            _append_nested(b.children, lines)


# ── Top-level parse ──────────────────────────────────────────────────────────


@dataclass
class NotionDevTrack:
    """Parsed developer track sourced from one Notion database entry.

    Shape matches ``app.analytics.dev_track.DevTrack`` closely so downstream
    display code can render either origin uniformly.

    ``skills_with_focus_intent`` and ``all_skill_texts`` are the pure-Notion
    inputs to the Focus Areas sync — computed directly from Skills
    Development objectives without consulting the Google Sheet. Keeping them
    on the track (rather than re-deriving in the sync layer) means the sheet
    can never quietly influence which Focus Areas bullets get added or
    removed.
    """

    dev_name: str
    page_id: str
    page_title: str
    levels: list[TrackLevel] = field(default_factory=list)
    focus_skill_names: set[str] = field(default_factory=set)
    skills_with_focus_intent: set[str] = field(default_factory=set)
    all_skill_texts: set[str] = field(default_factory=set)


def _normalize_key(text: str) -> str:
    """Lowercase + strip non-alphanumerics, matching ``dev_track._normalize``."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _build_sheet_status_lookup(
    sheet_tab: SheetTab | None,
) -> dict[tuple[int, str], SkillStatus]:
    """Build ``{(level_num, normalized_skill_text): SkillStatus}`` from a tab.

    Used so ``_derive_status_from_objectives`` can honour rules #3 and #4
    (don't demote green/purple; only downgrade blue/yellow when all objectives
    are past-form). Returns an empty dict if no tab is provided — callers then
    get the pure-Notion derivation without sheet-state gating.
    """
    if sheet_tab is None:
        return {}

    from app.analytics.dev_track import parse_tab  # avoid circular-ish import

    levels = parse_tab(sheet_tab)
    out: dict[tuple[int, str], SkillStatus] = {}
    for lv in levels:
        for sk in lv.skills:
            out[(lv.level, _normalize_key(sk.text))] = sk.status
    return out


def _iter_level_sections(
    blocks: list[NotionBlock],
) -> list[tuple[int, str, list[NotionBlock]]]:
    """Walk the ``## Skills Development`` section and yield one tuple per level.

    Returns ``[(level_number, level_title, [skill_blocks])]`` ordered as they
    appear in the Notion page. The level header text is everything after the
    number (e.g. ``"Level 5 — Senior Software Developer"`` → title ``"Senior
    Software Developer"``); bare ``"Level 5"`` yields an empty title.
    """
    # 1. Find the "Skills Development" heading and everything after it (until
    #    either the end of the page or the next heading_2).
    in_section = False
    section: list[NotionBlock] = []
    for b in blocks:
        if b.type == "heading_2":
            if b.text.strip().lower() == "skills development":
                in_section = True
                # Toggleable headings nest their siblings as children.
                if b.children:
                    section.extend(b.children)
                continue
            elif in_section:
                break  # Next heading_2 ends the section.
        if in_section:
            section.append(b)

    # 2. Walk the section and split by heading_3 ("Level N …").
    level_re = re.compile(r"^\s*level\s+(\d+)\s*[:\-–—]?\s*(.*)$", re.IGNORECASE)

    results: list[tuple[int, str, list[NotionBlock]]] = []
    current_num: int | None = None
    current_title: str = ""
    current_skills: list[NotionBlock] = []

    def _flush() -> None:
        if current_num is not None:
            results.append((current_num, current_title, current_skills.copy()))

    for b in section:
        if b.type == "heading_3":
            m = level_re.match(b.text.strip())
            if m:
                _flush()
                current_num = int(m.group(1))
                current_title = m.group(2).strip()
                current_skills = []
                # Toggleable headings: absorb any inline children as skills.
                if b.children:
                    current_skills.extend(b.children)
                continue
        if current_num is not None:
            current_skills.append(b)
    _flush()

    return results


def _extract_focus_areas(blocks: list[NotionBlock]) -> set[str]:
    """Collect bulleted skill names under ``## Focus Areas``.

    Handles both toggleable (children-inlined) and plain heading variants.
    """
    focus: set[str] = set()
    in_section = False
    for b in blocks:
        if b.type == "heading_2":
            if b.text.strip().lower() == "focus areas":
                in_section = True
                if b.children:
                    for c in b.children:
                        if c.type == "bulleted_list_item" and c.text.strip():
                            focus.add(c.text.strip())
                continue
            elif in_section:
                break
        if in_section and b.type == "bulleted_list_item" and b.text.strip():
            focus.add(b.text.strip())
    return focus


def _skill_blocks(section_blocks: list[NotionBlock]) -> list[NotionBlock]:
    """Select blocks inside a level section that represent individual skills.

    In Notion a bold bullet with children can appear as either a ``toggle`` or
    a ``bulleted_list_item`` (both accept children). We accept both.
    Paragraphs (like "TBD") produce a skill with no objectives.
    """
    return [
        b for b in section_blocks
        if b.type in ("toggle", "bulleted_list_item") and b.text.strip()
    ]


def _skill_todos(skill_block: NotionBlock) -> list[NotionBlock]:
    """Return the direct to_do children of a skill block."""
    return [c for c in skill_block.children if c.type == "to_do"]


def parse_dev_track_page(
    page_title: str,
    page_id: str,
    blocks: list[NotionBlock],
    current_sheet_tab: SheetTab | None = None,
) -> NotionDevTrack:
    """Convert a Notion page (as pre-fetched blocks) into a ``NotionDevTrack``.

    ``current_sheet_tab`` is optional but strongly recommended — without it,
    status derivation can't honour rules #3/#4 (don't demote green/purple,
    only downgrade blue/yellow). Pass the matching ``SheetTab`` when known.
    """
    dev_name = extract_dev_name(page_title)
    focus_names = _extract_focus_areas(blocks)
    sheet_status = _build_sheet_status_lookup(current_sheet_tab)

    levels: list[TrackLevel] = []
    # Pure-Notion focus signal — computed alongside the entangled sheet-aware
    # status so the sync layer can pick either view without re-walking blocks.
    skills_with_focus_intent: set[str] = set()
    all_skill_texts: set[str] = set()
    for level_num, level_title, section in _iter_level_sections(blocks):
        level = TrackLevel(level=level_num, title=level_title)
        for sb in _skill_blocks(section):
            skill_text = sb.text.strip()
            todos = _skill_todos(sb)
            current_status = sheet_status.get((level_num, _normalize_key(skill_text)))
            status = _derive_status_from_objectives(todos, current_status)
            note = _format_note(todos)
            level.skills.append(
                Skill(text=skill_text, status=status, note=note)
            )
            all_skill_texts.add(skill_text)
            if _has_focus_intent(todos):
                skills_with_focus_intent.add(skill_text)
        levels.append(level)

    return NotionDevTrack(
        dev_name=dev_name,
        page_id=page_id,
        page_title=page_title,
        levels=levels,
        focus_skill_names=focus_names,
        skills_with_focus_intent=skills_with_focus_intent,
        all_skill_texts=all_skill_texts,
    )
