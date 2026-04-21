"""Notion client — reads per-developer track pages from a Notion database.

Uses an Internal Integration token. The integration must be explicitly shared
with the target database (database → ··· → Connections → add integration).

The top-level API:

* ``fetch_database_entries(database_id)`` — query every page in a database
  (auto-paginates).
* ``fetch_page_blocks(page_id)`` — recursively fetch the full block tree of a
  page. Nested toggle/to-do/bullet children are inlined via ``NotionBlock.children``.
* ``extract_dev_name(page_title)`` — split ``"Don <> Mike"`` into ``"Don"``.
* ``add_skill_to_focus_areas`` / ``remove_skill_from_focus_areas`` — keep the
  Notion "Focus Areas" section in sync with the computed sync status.

All write paths are no-ops when the skill is already in the target state, so
callers can invoke them unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from notion_client import AsyncClient

from app.config import get_settings


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class NotionBlock:
    """A single block, with nested children inlined.

    * ``block_id`` — preserved so write paths can call ``blocks.delete()``.
    * ``type`` — Notion block type (``"heading_2"``, ``"toggle"``, ``"to_do"``,
      ``"bulleted_list_item"``, ``"paragraph"``, ``"heading_3"``, …).
    * ``text`` — concatenated plain text from the block's ``rich_text`` array.
    * ``checked`` — only meaningful for ``to_do`` blocks; ``None`` otherwise.
    * ``children`` — child blocks already fetched (same shape, recursive).
    """

    block_id: str
    type: str
    text: str
    checked: bool | None = None
    children: list["NotionBlock"] = field(default_factory=list)


# ── Client ───────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _client() -> AsyncClient:
    """Cached Notion async client. One per process — token doesn't rotate."""
    settings = get_settings()
    if not settings.notion_api_key:
        raise RuntimeError(
            "NOTION_API_KEY is not set. See README → "
            "'Notion Dev Track Sync' for setup."
        )
    return AsyncClient(auth=settings.notion_api_key)


# Views API requires API version 2026-03-11+. We keep the default client
# pinned to the stable 2022-06-28 version (that's what the rest of our
# block/page code is written against) and route view queries through a
# dedicated client so a newer version header doesn't regress other calls.
_VIEW_API_VERSION = "2026-03-11"


@lru_cache(maxsize=1)
def _view_client() -> AsyncClient:
    settings = get_settings()
    if not settings.notion_api_key:
        raise RuntimeError(
            "NOTION_API_KEY is not set. See README → "
            "'Notion Dev Track Sync' for setup."
        )
    return AsyncClient(
        auth=settings.notion_api_key,
        notion_version=_VIEW_API_VERSION,
    )


# ── Title / name helpers ─────────────────────────────────────────────────────


def extract_dev_name(page_title: str) -> str:
    """``'Don <> Mike'`` → ``'Don'``  (left side of ``<>``, stripped).

    Falls back to the original string if the delimiter isn't present so pages
    that don't follow the convention still produce a usable name.
    """
    if not page_title:
        return ""
    if "<>" in page_title:
        left = page_title.split("<>", 1)[0]
        return left.strip()
    return page_title.strip()


def _page_title(page: dict[str, Any]) -> str:
    """Extract the title from a Notion page object (database query result)."""
    props = page.get("properties") or {}
    # The title property has type "title"; its name varies ("Name", "Title", …).
    for prop in props.values():
        if prop.get("type") == "title":
            return _concat_rich_text(prop.get("title") or [])
    return ""


def _concat_rich_text(rich_text: list[dict[str, Any]]) -> str:
    """Concatenate every ``plain_text`` fragment into one string."""
    return "".join(seg.get("plain_text", "") for seg in rich_text or [])


# ── Database query ───────────────────────────────────────────────────────────


async def fetch_database_entries(
    database_id: str,
    view_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query pages in the database. Handles Notion's 100-entry pagination.

    * ``view_id`` (optional) — if set, uses Notion's view-query endpoint to
      return only the pages that match the view's saved filter + sort (same
      rows visible in the Notion UI for that view). Otherwise every page in
      the database is returned.

    Returns raw page objects (id, properties, …) — the caller is responsible
    for extracting titles and fetching block children. The view endpoint
    returns page *references* (id only), so we retrieve each page's full
    object here to keep the shape consistent for callers.
    """
    if not database_id and not view_id:
        raise RuntimeError(
            "NOTION_DEV_TRACK_DATABASE_ID is not set. Paste the database ID "
            "(the part of the URL after the workspace slug and before ``?v=``)."
        )

    if view_id:
        return await _fetch_view_entries(view_id)

    client = _client()
    pages: list[dict[str, Any]] = []
    start_cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {"database_id": database_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = await client.databases.query(**kwargs)
        pages.extend(resp.get("results") or [])
        if not resp.get("has_more"):
            break
        start_cursor = resp.get("next_cursor")
        if not start_cursor:
            break

    return pages


async def _fetch_view_entries(view_id: str) -> list[dict[str, Any]]:
    """Run a view's saved filter/sort and return full page objects.

    Flow (three-step, per Notion's Views API):

    1. ``POST /views/{view_id}/queries`` — creates a cached query, returns
       the first page of page references plus a ``query_id``.
    2. ``GET  /views/{view_id}/queries/{query_id}`` — paginates through the
       cached result set (cache expires after 15 minutes; if we hit that,
       we restart by creating a fresh query).
    3. ``pages.retrieve`` — the view endpoint returns page *references*
       (``{object: "page", id: ...}``), so we fetch the full page object
       for each reference so downstream code can read titles/properties.
    """
    view_client = _view_client()

    try:
        first = await view_client.request(
            path=f"views/{view_id}/queries",
            method="POST",
            body={"page_size": 100},
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to run Notion view query for view_id={view_id!r}: {e}. "
            "Double-check NOTION_DEV_TRACK_VIEW_ID and make sure the "
            "integration is shared on the parent database."
        ) from e

    query_id = first.get("id")
    page_refs: list[dict[str, Any]] = list(first.get("results") or [])
    has_more = bool(first.get("has_more"))
    next_cursor = first.get("next_cursor")

    while has_more and next_cursor and query_id:
        resp = await view_client.request(
            path=f"views/{view_id}/queries/{query_id}",
            method="GET",
            query={"start_cursor": next_cursor, "page_size": 100},
        )
        page_refs.extend(resp.get("results") or [])
        has_more = bool(resp.get("has_more"))
        next_cursor = resp.get("next_cursor")

    client = _client()
    pages: list[dict[str, Any]] = []
    for ref in page_refs:
        pid = ref.get("id")
        if not pid:
            continue
        page = await client.pages.retrieve(page_id=pid)
        pages.append(page)
    return pages


# ── Block tree fetch ─────────────────────────────────────────────────────────


async def _list_children(block_id: str) -> list[dict[str, Any]]:
    """List *direct* children of a block, auto-paginated."""
    client = _client()
    out: list[dict[str, Any]] = []
    start_cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"block_id": block_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = await client.blocks.children.list(**kwargs)
        out.extend(resp.get("results") or [])
        if not resp.get("has_more"):
            break
        start_cursor = resp.get("next_cursor")
        if not start_cursor:
            break
    return out


def _extract_block_text(raw: dict[str, Any]) -> str:
    """Pull ``rich_text`` out of whichever inner object matches the block type."""
    btype = raw.get("type") or ""
    inner = raw.get(btype) or {}
    return _concat_rich_text(inner.get("rich_text") or [])


def _extract_checked(raw: dict[str, Any]) -> bool | None:
    if raw.get("type") != "to_do":
        return None
    return bool((raw.get("to_do") or {}).get("checked", False))


async def _fetch_subtree(raw: dict[str, Any]) -> NotionBlock:
    """Convert one raw block (and all descendants) into a NotionBlock tree."""
    block = NotionBlock(
        block_id=raw.get("id", ""),
        type=raw.get("type", ""),
        text=_extract_block_text(raw),
        checked=_extract_checked(raw),
    )
    if raw.get("has_children"):
        raw_children = await _list_children(block.block_id)
        for rc in raw_children:
            block.children.append(await _fetch_subtree(rc))
    return block


async def fetch_page_blocks(page_id: str) -> list[NotionBlock]:
    """Recursively fetch all blocks on a page. Nested blocks are inlined."""
    top = await _list_children(page_id)
    return [await _fetch_subtree(rc) for rc in top]


# ── Focus Areas write operations ─────────────────────────────────────────────


def _find_focus_areas_children(blocks: list[NotionBlock]) -> list[NotionBlock]:
    """Return the direct children of the ``## Focus Areas`` heading.

    Notion nests children under a heading when it's in "toggleable heading"
    mode; otherwise the heading's siblings (until the next heading_2) *are*
    its logical children. We check both.
    """
    focus_block: NotionBlock | None = None
    focus_index: int | None = None
    for i, b in enumerate(blocks):
        if b.type == "heading_2" and b.text.strip().lower() == "focus areas":
            focus_block = b
            focus_index = i
            break
    if focus_block is None:
        return []

    # Case 1: toggleable heading with inline children.
    if focus_block.children:
        return focus_block.children

    # Case 2: plain heading — the "children" are siblings between this
    # heading_2 and the next heading_2 (or end of list).
    out: list[NotionBlock] = []
    for b in blocks[(focus_index or 0) + 1:]:
        if b.type == "heading_2":
            break
        out.append(b)
    return out


def _normalize_skill_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy comparison."""
    import re
    return re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()


async def _find_focus_areas_container(
    page_id: str,
    blocks: list[NotionBlock],
) -> str:
    """Return the block_id to append Focus Areas items under.

    If the heading is toggleable (has inline children), returns its block_id.
    Otherwise we need a different strategy — appending to the page itself
    would put the item at the very bottom, so we insert *after* the last
    existing Focus Areas child (or after the heading itself).

    For simplicity and to guarantee placement under the heading, we append
    to the heading block's children. This works when the heading is
    toggleable in Notion; if it's not, the API still accepts appending
    children (Notion will render it as a nested block).
    """
    for b in blocks:
        if b.type == "heading_2" and b.text.strip().lower() == "focus areas":
            return b.block_id
    # No Focus Areas heading — fall back to the page itself; the caller is
    # expected to ensure the heading exists before calling.
    return page_id


async def add_skill_to_focus_areas(
    page_id: str,
    blocks: list[NotionBlock],
    skill_text: str,
) -> bool:
    """Append a bulleted_list_item to Focus Areas if the skill isn't listed.

    Returns True if a new block was added, False if the skill was already
    present (or no Focus Areas heading exists).
    """
    if not skill_text.strip():
        return False

    existing = _find_focus_areas_children(blocks)
    target = _normalize_skill_text(skill_text)
    for child in existing:
        if _normalize_skill_text(child.text) == target:
            return False  # Already present — no-op.

    # Locate the heading; if missing, silently skip (don't invent a section).
    has_heading = any(
        b.type == "heading_2" and b.text.strip().lower() == "focus areas"
        for b in blocks
    )
    if not has_heading:
        return False

    container_id = await _find_focus_areas_container(page_id, blocks)

    client = _client()
    await client.blocks.children.append(
        block_id=container_id,
        children=[{
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [
                    {"type": "text", "text": {"content": skill_text}}
                ]
            },
        }],
    )
    return True


async def remove_skill_from_focus_areas(
    page_id: str,
    blocks: list[NotionBlock],
    skill_text: str,
) -> bool:
    """Delete the bulleted_list_item matching ``skill_text`` from Focus Areas.

    Returns True if a block was deleted, False if there was no match.
    Matching is done on normalized text so trivial whitespace/case drift
    doesn't prevent removal.
    """
    if not skill_text.strip():
        return False

    target = _normalize_skill_text(skill_text)
    client = _client()
    deleted = False
    for child in _find_focus_areas_children(blocks):
        if child.type != "bulleted_list_item":
            continue
        if _normalize_skill_text(child.text) == target:
            await client.blocks.delete(block_id=child.block_id)
            deleted = True
    return deleted
