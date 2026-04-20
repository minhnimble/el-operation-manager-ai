"""Google Sheets client — reads cell values, background colors, and notes.

Uses a service account for auth. The service account's `client_email` must be
granted at least Viewer access to the target sheet.

All cell formatting (backgrounds, notes) comes from a single
``spreadsheets.get(includeGridData=True)`` call per sheet — batched across all
tabs — so one fetch is enough to build the full developer-track picture for
every member.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.config import get_settings


_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


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


def _load_credentials() -> Credentials:
    settings = get_settings()
    raw = settings.google_sheets_credentials_json
    if not raw:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS_JSON is not set. See README → "
            "'Developer Track (Google Sheets)' for setup."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS_JSON is not valid JSON. Paste the "
            "entire service-account key file as a single-line string."
        ) from e
    return Credentials.from_service_account_info(info, scopes=_SCOPES)


@lru_cache(maxsize=1)
def _service():
    """Cached Sheets API client. One per process — credentials don't rotate."""
    creds = _load_credentials()
    # cache_discovery=False avoids the noisy file-cache warning on headless
    # environments (Streamlit Cloud, Docker) where the discovery cache dir
    # isn't writable.
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
