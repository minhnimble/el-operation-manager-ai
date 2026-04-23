"""Shared datetime formatting helpers — all display times in GMT+7.

The app stores timestamps in UTC (standard Python/DB convention) but the
user reads reports in Indochina Time. Every user-facing timestamp should
route through these helpers so the UTC → GMT+7 conversion is consistent
and can be changed in one place if the target zone ever moves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Fixed +07:00 offset — Indochina Time has no DST, so a fixed offset is
# sufficient and avoids a zoneinfo dependency on Streamlit Cloud.
GMT7 = timezone(timedelta(hours=7), name="GMT+7")


def to_gmt7(dt: datetime) -> datetime:
    """Return ``dt`` converted to GMT+7.

    Naive datetimes are assumed to be UTC (matches what our code writes
    via ``datetime.now(timezone.utc).replace(tzinfo=None)`` and asyncpg
    default).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(GMT7)


def format_gmt7(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format ``dt`` in GMT+7 with a trailing ``GMT+7`` label."""
    return to_gmt7(dt).strftime(fmt) + " GMT+7"


def format_gmt7_time(dt: datetime) -> str:
    """Short ``HH:MM:SS GMT+7`` — for compact table cells."""
    return to_gmt7(dt).strftime("%H:%M:%S") + " GMT+7"


def now_gmt7() -> datetime:
    """Current time as a GMT+7-aware datetime."""
    return datetime.now(GMT7)
