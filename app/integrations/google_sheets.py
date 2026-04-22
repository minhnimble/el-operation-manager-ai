"""Google Sheets client — reads cell values, background colors, and notes.

Uses a service account for auth. The service account's `client_email` must be
granted at least Viewer access to the target sheet (Editor for the write path
used by the Notion → DevTrack sync).

All cell formatting (backgrounds, notes) comes from a single
``spreadsheets.get(includeGridData=True)`` call per sheet — batched across all
tabs — so one fetch is enough to build the full developer-track picture for
every member.

Write path (used by the Notion sync page):

* ``build_cell_position_map`` indexes skills in an existing sheet tab by
  ``(level_num, normalized_skill_text)`` so we can locate their row/column.
* ``compute_cell_updates`` compares parsed Notion levels against the current
  sheet state and emits only the cells that actually need to change.
* ``apply_cell_updates`` sends one ``batchUpdate`` request, setting value,
  background colour, and note per cell atomically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, TYPE_CHECKING

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.config import get_settings

if TYPE_CHECKING:
    from app.analytics.dev_track import SkillStatus, TrackLevel


_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@dataclass
class SheetCell:
    """A single cell as returned by the Sheets API, normalized for parsing.

    * ``value`` — displayed text (formatted value, not the raw formula).
    * ``bg_rgb`` — (r, g, b) tuple in 0-1 float space, or None if no fill.
    * ``note`` — user note attached to the cell (hover annotation), or None.
    """
    value: str
    bg_rgb: tuple[float, float, float] | None
    note: str | None


@dataclass
class SheetTab:
    title: str
    rows: list[list[SheetCell]] = field(default_factory=list)


def _load_credentials_info() -> dict[str, Any]:
    """Parse and return the raw service-account JSON info dict.

    Shared between the read-only and read+write credential builders so the
    JSON is only parsed (and validated) once per call-site.
    """
    settings = get_settings()
    raw = settings.google_sheets_credentials_json
    if not raw:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS_JSON is not set. See README → "
            "'Developer Track (Google Sheets)' for setup."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS_JSON is not valid JSON. Paste the "
            "entire service-account key file as a single-line string."
        ) from e


def _load_credentials() -> Credentials:
    return Credentials.from_service_account_info(
        _load_credentials_info(), scopes=_SCOPES
    )


def _load_write_credentials() -> Credentials:
    return Credentials.from_service_account_info(
        _load_credentials_info(), scopes=_WRITE_SCOPES
    )


@lru_cache(maxsize=1)
def _service():
    """Cached Sheets API client. One per process — credentials don't rotate."""
    creds = _load_credentials()
    # cache_discovery=False avoids the noisy file-cache warning on headless
    # environments (Streamlit Cloud, Docker) where the discovery cache dir
    # isn't writable.
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


@lru_cache(maxsize=1)
def _write_service():
    """Cached Sheets API client with read+write scope — used by the Notion sync.

    Kept separate from ``_service()`` so the existing read-only path keeps
    its narrower scope (principle of least privilege); only the sync feature
    needs write access.
    """
    creds = _load_write_credentials()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _cell_from_api(raw: dict[str, Any]) -> SheetCell:
    """Translate one ``rowData[].values[]`` entry from the API into SheetCell."""
    value = raw.get("formattedValue", "") or ""

    bg_rgb: tuple[float, float, float] | None = None
    # effectiveFormat.backgroundColorStyle.rgbColor is the modern field;
    # backgroundColor is the legacy one. Either may be present.
    fmt = raw.get("effectiveFormat", {}) or {}
    bg_style = (fmt.get("backgroundColorStyle") or {}).get("rgbColor")
    bg_legacy = fmt.get("backgroundColor")
    bg = bg_style or bg_legacy
    if bg:
        # Missing channels default to 0 per the API spec.
        bg_rgb = (
            float(bg.get("red",   0.0)),
            float(bg.get("green", 0.0)),
            float(bg.get("blue",  0.0)),
        )

    note = raw.get("note")
    return SheetCell(value=value, bg_rgb=bg_rgb, note=note)


def fetch_all_tabs(sheet_id: str) -> list[SheetTab]:
    """Fetch every tab in the sheet with cell values, backgrounds, and notes.

    One API call. Tabs are returned in the order they appear in the sheet.
    """
    if not sheet_id:
        raise RuntimeError(
            "DEV_TRACK_SHEET_ID is not set. Paste the sheet ID "
            "(the part between /d/ and /edit in the URL) into secrets."
        )

    resp = (
        _service()
        .spreadsheets()
        .get(
            spreadsheetId=sheet_id,
            includeGridData=True,
            # Only fetch what we need — keeps the payload manageable on
            # sheets with lots of tabs.
            fields=(
                "sheets(properties(title),"
                "data(rowData(values("
                "formattedValue,note,"
                "effectiveFormat(backgroundColor,backgroundColorStyle)"
                "))))"
            ),
        )
        .execute()
    )

    tabs: list[SheetTab] = []
    for sh in resp.get("sheets", []):
        title = (sh.get("properties") or {}).get("title", "")
        rows: list[list[SheetCell]] = []
        for grid in sh.get("data", []) or []:
            for row in grid.get("rowData", []) or []:
                cells = [_cell_from_api(c) for c in (row.get("values") or [])]
                rows.append(cells)
        tabs.append(SheetTab(title=title, rows=rows))
    return tabs


# ═══════════════════════════════════════════════════════════════════════════
# Write path — used by the Notion → DevTrack sync feature only.
# ═══════════════════════════════════════════════════════════════════════════


# Google Sheets' default palette colours, chosen to match the hue buckets that
# ``dev_track.classify_color`` decodes when reading. These are the RGB values
# Sheets' swatch picker produces — slight drift (±0.05 per channel) is fine
# because the reader classifies by hue, not exact RGB.
# Google Sheets preset palette — "light <hue> 3" swatches. Hex values are what
# Sheets assigns when picking those presets, so cells match the native UI.
#   light green 3            #93c47d
#   light cornflower blue 3  #6d9eeb
#   light purple 3           #8e7cc3
#   light yellow 3           #ffd966
STATUS_COLORS: dict[str, tuple[float, float, float] | None] = {
    "completed":   (0x93 / 255, 0xC4 / 255, 0x7D / 255),  # light green 3
    "in_progress": (0x6D / 255, 0x9E / 255, 0xEB / 255),  # light cornflower blue 3
    "proposed":    (0x8E / 255, 0x7C / 255, 0xC3 / 255),  # light purple 3
    "focus":       (0xFF / 255, 0xD9 / 255, 0x66 / 255),  # light yellow 3
    "todo":        None,                                   # no fill (white)
}


@dataclass
class CellUpdate:
    """One cell to write in a ``batchUpdate`` request.

    * ``row_idx`` / ``col_idx`` — 0-based (Sheets API uses 0-based indices).
    * ``value`` — the cell's displayed text (the skill name).
    * ``status`` — one of the ``SkillStatus`` strings; maps to background RGB.
    * ``note`` — the multi-line note with ``-``/``+`` prefixes; ``None`` clears.
    * ``reason`` — free-form debug tag useful for diff previews in the UI
      (``"added"`` | ``"value_changed"`` | ``"color_changed"`` | ``"note_changed"``).
    """

    row_idx: int
    col_idx: int
    value: str
    status: str
    note: str | None
    reason: str = ""


def _normalize_skill_text(text: str) -> str:
    """Lowercase + strip non-alphanumerics, matching ``dev_track._normalize``."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def build_cell_position_map(
    tab: SheetTab,
) -> dict[tuple[int, str], tuple[int, int]]:
    """Return ``{(level_num, normalized_skill_text): (row_idx, col_idx)}``.

    Walks the sheet the same way ``dev_track.parse_tab`` does — column A is
    the integer level header, column B is the level title, columns C+ are
    skills — but records *positions* instead of parsed ``Skill`` objects so
    we can write back to the exact cells.
    """
    _LEVEL_RE = re.compile(r"^\s*(\d{1,2})\s*$")

    out: dict[tuple[int, str], tuple[int, int]] = {}
    current_level: int | None = None

    for row_idx, row in enumerate(tab.rows):
        first_val = (row[0].value if len(row) > 0 else "") or ""
        m = _LEVEL_RE.match(first_val)
        if m:
            n = int(m.group(1))
            if 0 < n <= 20:
                current_level = n
            continue
        if current_level is None:
            continue
        for col_idx in range(2, len(row)):
            text = (row[col_idx].value or "").strip()
            if not text:
                continue
            key = _normalize_skill_text(text)
            if key:
                out[(current_level, key)] = (row_idx, col_idx)
    return out


def _find_level_header_row(tab: SheetTab, level_num: int) -> int | None:
    """Find the row index of the header row for the given level number."""
    _LEVEL_RE = re.compile(r"^\s*(\d{1,2})\s*$")
    for i, row in enumerate(tab.rows):
        first_val = (row[0].value if len(row) > 0 else "") or ""
        m = _LEVEL_RE.match(first_val)
        if m and int(m.group(1)) == level_num:
            return i
    return None


def _next_level_header_row(tab: SheetTab, after_row: int) -> int:
    """Row index of the next level header after ``after_row``, or len(rows)."""
    _LEVEL_RE = re.compile(r"^\s*(\d{1,2})\s*$")
    for i in range(after_row + 1, len(tab.rows)):
        first_val = (tab.rows[i][0].value if len(tab.rows[i]) > 0 else "") or ""
        if _LEVEL_RE.match(first_val):
            return i
    return len(tab.rows)


def _find_free_cell_in_level(
    tab: SheetTab,
    level_num: int,
    taken: set[tuple[int, int]],
) -> tuple[int, int] | None:
    """Locate an empty cell within the level section for appending a new skill.

    Scans rows between this level's header and the next level's header.
    Columns C onwards. ``taken`` prevents re-using a cell that another
    pending update in the same batch already targets.

    Returns ``None`` if every cell in the level section is occupied — the
    caller is expected to handle that (currently: skip with a warning;
    expanding the section is out of scope for the first release).
    """
    header = _find_level_header_row(tab, level_num)
    if header is None:
        return None
    end = _next_level_header_row(tab, header)
    # Determine the existing column span by looking at the header + skill rows.
    max_cols = 0
    for row in tab.rows[header:end]:
        max_cols = max(max_cols, len(row))
    # Fallback: if the section's rows are all short, use at least 4 columns
    # (col A, B, C, D) so we have two skill slots to work with.
    col_limit = max(max_cols, 4)

    for row_idx in range(header + 1, end):
        row = tab.rows[row_idx]
        for col_idx in range(2, col_limit):
            if (row_idx, col_idx) in taken:
                continue
            cell_val = ""
            if col_idx < len(row):
                cell_val = (row[col_idx].value or "").strip()
            if not cell_val:
                return (row_idx, col_idx)
    return None


def _status_from_bg(bg_rgb: tuple[float, float, float] | None) -> str:
    """Proxy to ``dev_track.classify_color`` without a hard import cycle."""
    from app.analytics.dev_track import classify_color
    return classify_color(bg_rgb)


def compute_cell_updates(
    notion_levels: list["TrackLevel"],
    current_tab: SheetTab,
) -> list[CellUpdate]:
    """Diff Notion-parsed levels against the current sheet; return changed cells.

    Rules (from the plan):
    * Skill exists in Notion and the sheet → update only if value/colour/note differs.
    * Skill exists in Notion but not the sheet → append to an empty cell in
      the same level section (rows between its header and the next header).
    * Skill exists in the sheet but not Notion → *left untouched*.
    * Wording mismatch on the note → always take Notion's version.
    """
    updates: list[CellUpdate] = []
    position_map = build_cell_position_map(current_tab)
    # Track cells we've claimed for "append" operations so we don't pick the
    # same free slot twice in one diff pass.
    claimed: set[tuple[int, int]] = set()

    for level in notion_levels:
        for skill in level.skills:
            text = (skill.text or "").strip()
            if not text:
                continue
            key = _normalize_skill_text(text)
            existing_pos = position_map.get((level.level, key))

            if existing_pos is not None:
                row_idx, col_idx = existing_pos
                existing = current_tab.rows[row_idx][col_idx]
                cur_status = _status_from_bg(existing.bg_rgb)
                cur_note = (existing.note or "") or None
                new_note = (skill.note or None)

                reasons: list[str] = []
                if (existing.value or "").strip() != text:
                    reasons.append("value_changed")
                if cur_status != skill.status:
                    reasons.append("color_changed")
                if (cur_note or "") != (new_note or ""):
                    reasons.append("note_changed")

                if reasons:
                    updates.append(CellUpdate(
                        row_idx=row_idx,
                        col_idx=col_idx,
                        value=text,
                        status=skill.status,
                        note=new_note,
                        reason=",".join(reasons),
                    ))
            else:
                # Skill isn't in the sheet — find a free cell in the level.
                slot = _find_free_cell_in_level(current_tab, level.level, claimed)
                if slot is None:
                    # No free cell — first release keeps the behaviour simple:
                    # skip and let the user add a row manually. The UI can
                    # surface this as a warning.
                    continue
                claimed.add(slot)
                updates.append(CellUpdate(
                    row_idx=slot[0],
                    col_idx=slot[1],
                    value=text,
                    status=skill.status,
                    note=(skill.note or None),
                    reason="added",
                ))

    return updates


def _get_sheet_id_by_title(spreadsheet_id: str, tab_title: str) -> int:
    """Resolve a tab title to its internal ``sheetId`` (needed for batchUpdate).

    One extra metadata call (no grid data). Raises if the tab isn't found.
    """
    resp = (
        _write_service()
        .spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute()
    )
    for sh in resp.get("sheets", []) or []:
        props = sh.get("properties") or {}
        if props.get("title") == tab_title:
            return int(props["sheetId"])
    raise RuntimeError(
        f"Sheet tab {tab_title!r} not found in spreadsheet {spreadsheet_id}."
    )


def _color_dict(rgb: tuple[float, float, float] | None) -> dict[str, float]:
    """Build the ``Color`` object for the Sheets API.

    ``None`` (todo / no fill) is expressed as white (1,1,1,1) — Sheets treats
    missing background as white, and explicitly writing 1,1,1 clears any
    previous colour fill deterministically.
    """
    if rgb is None:
        return {"red": 1.0, "green": 1.0, "blue": 1.0, "alpha": 1.0}
    return {"red": rgb[0], "green": rgb[1], "blue": rgb[2], "alpha": 1.0}


def apply_cell_updates(
    spreadsheet_id: str,
    tab_title: str,
    updates: list[CellUpdate],
) -> int:
    """Send one ``batchUpdate`` that writes every cell in ``updates``.

    Writes three fields per cell — ``userEnteredValue.stringValue``,
    ``userEnteredFormat.backgroundColor``, and ``note`` — so the cell's text,
    status colour, and evidence note all land atomically. Returns the count
    of cells written (== ``len(updates)`` unless the API errors).
    """
    if not updates:
        return 0

    sheet_id = _get_sheet_id_by_title(spreadsheet_id, tab_title)

    requests: list[dict[str, Any]] = []
    for upd in updates:
        bg_rgb = STATUS_COLORS.get(upd.status)
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId":          sheet_id,
                    "startRowIndex":    upd.row_idx,
                    "endRowIndex":      upd.row_idx + 1,
                    "startColumnIndex": upd.col_idx,
                    "endColumnIndex":   upd.col_idx + 1,
                },
                "rows": [{
                    "values": [{
                        "userEnteredValue": {"stringValue": upd.value},
                        "userEnteredFormat": {
                            "backgroundColor": _color_dict(bg_rgb),
                        },
                        "note": upd.note or "",
                    }],
                }],
                "fields": (
                    "userEnteredValue,"
                    "userEnteredFormat.backgroundColor,"
                    "note"
                ),
            }
        })

    (
        _write_service()
        .spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        )
        .execute()
    )
    return len(updates)
