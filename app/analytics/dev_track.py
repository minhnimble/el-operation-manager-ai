"""Developer Track parser.

Reads one Google Sheet tab (see ``app/integrations/google_sheets.py``) and
extracts per-level skills, their status (from cell background color), and any
manager notes (from cell notes).

Sheet layout (inferred from the paste the user shared):

    <num>   <level title>                                       (level header)
                <skill text>           <skill text>             (skill row)
                <skill text>           <skill text>
                ...
    <num+1> <level title>
                <skill text>           <skill text>
                ...

Column A holds the integer level (1..N), column B holds the level title, and
columns C onwards hold skills. We don't assume exactly two skill columns —
whatever is present from column C onwards is captured, so the parser survives
small schema drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.integrations.google_sheets import SheetCell, SheetTab


# Skill-status taxonomy, keyed by cell background color.
SkillStatus = Literal[
    "completed",   # green   — vetted
    "proposed",    # purple  — proposed for vetting
    "in_progress", # blue    — in progress
    "focus",       # yellow  — focusing to improve but not started yet
    "todo",        # white / no fill — not touching yet
]


STATUS_LABELS: dict[SkillStatus, str] = {
    "completed":   "✅ Completed",
    "in_progress": "🔵 In progress",
    "proposed":    "🟣 Proposed for vetting",
    "focus":       "🟡 Focus area",
    "todo":        "⬜ Not started",
}

# Order we render statuses in the UI. Focus / in-progress / not-started come
# first because those are the skills that need attention; vetting state and
# already-completed skills sink to the bottom.
STATUS_ORDER: list[SkillStatus] = [
    "focus", "in_progress", "todo", "proposed", "completed",
]


@dataclass
class Skill:
    text: str
    status: SkillStatus
    note: str | None = None


@dataclass
class TrackLevel:
    level: int
    title: str
    skills: list[Skill] = field(default_factory=list)

    @property
    def counts(self) -> dict[SkillStatus, int]:
        out: dict[SkillStatus, int] = {s: 0 for s in STATUS_ORDER}
        for sk in self.skills:
            out[sk.status] += 1
        return out


@dataclass
class DevTrack:
    """Full parsed track for one person (one sheet tab)."""
    member_name: str           # the matched member display name
    tab_title: str             # the original sheet tab title
    levels: list[TrackLevel] = field(default_factory=list)

    @property
    def current_level(self) -> int | None:
        """Heuristic: the highest level that has at least one non-todo skill.

        Matches how managers typically mark progress — a person is "at" the
        level they're actively being evaluated on (green/blue/purple/yellow),
        not the level with only blank rows below it.
        """
        active: set[SkillStatus] = {"completed", "in_progress", "proposed", "focus"}
        highest: int | None = None
        for lv in self.levels:
            if any(sk.status in active for sk in lv.skills):
                highest = lv.level
        return highest


# ── Color → status ───────────────────────────────────────────────────────────


def classify_color(rgb: tuple[float, float, float] | None) -> SkillStatus:
    """Map a cell background RGB (0–1 floats) to a SkillStatus.

    Matching is fuzzy: Google Sheets' default palette produces slightly
    different shades per user, so we classify by hue + saturation rather than
    exact-match RGB.
    """
    if rgb is None:
        return "todo"

    r, g, b = rgb
    # Convert to HSV-ish: compute max/min channels and derive hue.
    mx, mn = max(r, g, b), min(r, g, b)
    delta = mx - mn

    # Near-white (low saturation, high value) → not touched.
    if delta < 0.06 and mx > 0.92:
        return "todo"
    # Near-black / very low value: treat as todo too (shouldn't happen in
    # practice but keeps the function total).
    if mx < 0.2:
        return "todo"

    # If saturation is very low but it's not bright white, it's a grey fill —
    # also treat as todo.
    if delta < 0.08:
        return "todo"

    # Compute hue in degrees [0, 360).
    if mx == r:
        h = (60 * ((g - b) / delta)) % 360
    elif mx == g:
        h = 60 * ((b - r) / delta) + 120
    else:
        h = 60 * ((r - g) / delta) + 240

    # Hue buckets, tuned against Google Sheets' default swatches:
    #   yellow  ~ 40–70
    #   green   ~ 80–170
    #   blue    ~ 180–250
    #   purple  ~ 260–320
    if   40  <= h <  75:
        return "focus"        # yellow
    elif 75  <= h < 170:
        return "completed"    # green
    elif 170 <= h < 255:
        return "in_progress"  # blue
    elif 255 <= h < 330:
        return "proposed"     # purple
    # Red / pink / orange cells aren't part of the documented palette —
    # fall back to todo so they don't get silently classified as progress.
    return "todo"


# ── Tab parser ───────────────────────────────────────────────────────────────


def _row_cell(row: list[SheetCell], idx: int) -> SheetCell | None:
    """Safe cell accessor (rows can be short when trailing cells are empty)."""
    if 0 <= idx < len(row):
        return row[idx]
    return None


def parse_tab(tab: SheetTab) -> list[TrackLevel]:
    """Walk rows top-to-bottom, grouping skill rows under their level header."""
    levels: list[TrackLevel] = []
    current: TrackLevel | None = None

    for row in tab.rows:
        first = _row_cell(row, 0)
        second = _row_cell(row, 1)
        level_num = _parse_level_number(first.value if first else "")

        if level_num is not None:
            # Start a new level section.
            current = TrackLevel(
                level=level_num,
                title=(second.value if second else "").strip(),
            )
            levels.append(current)
            continue

        if current is None:
            # Rows before the first level header (e.g., sheet-wide title) —
            # ignore. They're not skill rows.
            continue

        # Treat every non-empty cell from column C (index 2) onward as a skill.
        for idx in range(2, len(row)):
            cell = row[idx]
            text = (cell.value or "").strip()
            if not text:
                continue
            current.skills.append(
                Skill(
                    text=text,
                    status=classify_color(cell.bg_rgb),
                    note=(cell.note or "").strip() or None,
                )
            )

    return levels


_LEVEL_RE = re.compile(r"^\s*(\d{1,2})\s*$")


def _parse_level_number(raw: str) -> int | None:
    """Return the integer level from column A, or None if this isn't a header.

    A level header row is one where column A is a bare integer (e.g., "3",
    "4", "5"). Any other content — empty, text, decimals — is a skill row
    (or filler).
    """
    if not raw:
        return None
    m = _LEVEL_RE.match(raw)
    if not m:
        return None
    n = int(m.group(1))
    # Reasonable bound so freakishly large numbers in column A don't create
    # phantom levels.
    return n if 0 < n <= 20 else None


# ── Tab ↔ member matching ────────────────────────────────────────────────────


def _normalize(name: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy name matching."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def match_tab_to_member(tab_title: str, member_display_name: str) -> bool:
    """True if the tab title and member name share a recognizable name token.

    Matches in either direction: "Don" inside "Vo Minh Don — DevTrack", or
    "Don" as a tab title for member "Don Vo". Uses the full normalized
    form as a substring check against both the full names AND each token,
    which keeps single-word tab names like "Don" matching a multi-word
    display name without requiring an exact match.
    """
    tab_norm = _normalize(tab_title)
    mem_norm = _normalize(member_display_name)
    if not tab_norm or not mem_norm:
        return False
    if tab_norm in mem_norm or mem_norm in tab_norm:
        return True
    # Token-level match: "don" vs "vo minh don"
    tab_tokens = {_normalize(t) for t in re.split(r"\s+", tab_title) if t}
    mem_tokens = {_normalize(t) for t in re.split(r"\s+", member_display_name) if t}
    tab_tokens.discard("")
    mem_tokens.discard("")
    # A token is a useful match only if it's reasonably unique — two-letter
    # tokens cause false positives (e.g., "Vo" matching "Volume").
    return any(len(t) >= 3 and t in mem_tokens for t in tab_tokens) or \
           any(len(t) >= 3 and t in tab_tokens for t in mem_tokens)


def find_member_track(
    tabs: list[SheetTab],
    *candidate_names: str,
) -> DevTrack | None:
    """Locate the sheet tab for a member and parse it.

    Accepts multiple candidate names (e.g. Slack display name, real name,
    email local-part) and returns the first tab that matches any of them.
    Email addresses are split on ``@`` so the local-part alone is compared,
    and further split on ``.``/``_``/``-`` so ``don.vo@…`` matches ``Don Vo``.
    """
    names: list[str] = []
    for raw in candidate_names:
        if not raw:
            continue
        names.append(raw)
        if "@" in raw:
            local = raw.split("@", 1)[0]
            # Expand "don.vo" / "don_vo" / "don-vo" into "don vo"
            names.append(re.sub(r"[._\-]+", " ", local))

    for tab in tabs:
        for name in names:
            if match_tab_to_member(tab.title, name):
                return DevTrack(
                    member_name=names[0],
                    tab_title=tab.title,
                    levels=parse_tab(tab),
                )
    return None
