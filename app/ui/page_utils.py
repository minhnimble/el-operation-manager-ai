"""
Shared UI utilities for all Streamlit pages.

Usage at the top of every page (after st.set_page_config):

    from app.ui.page_utils import page_header, loading_section

    page_header("🔗 Connect Accounts", "Connect your Slack and GitHub accounts.")

    with loading_section("Fetching your team…"):
        data = run(fetch_data())   # heavy async work here

    # render UI with data below
"""

from __future__ import annotations

import contextlib
import streamlit as st


# ── Animated top-bar + skeleton CSS ──────────────────────────────────────────

_LOADING_CSS = """
<style>
/* ── Page-load progress bar ──────────────────────────────────────────────── */
/* Appears immediately when the Streamlit script starts rendering, plays a
   2-second animation, then disappears.  No JS required. */
@keyframes _page_load_bar {
    0%   { width: 0%;   opacity: 1; }
    60%  { width: 85%;  opacity: 1; }
    90%  { width: 95%;  opacity: 1; }
    100% { width: 100%; opacity: 0; }
}

.__page_load_bar {
    position:  fixed;
    top:       0;
    left:      0;
    height:    3px;
    width:     0%;
    background: linear-gradient(90deg, #4C9BE8 0%, #7EC8F0 60%, #4C9BE8 100%);
    background-size: 200% 100%;
    animation: _page_load_bar 2s ease-out forwards;
    z-index:   9999;
    pointer-events: none;
}

/* ── Skeleton shimmer (used by loading_section) ────────────────────────── */
@keyframes _shimmer {
    0%   { background-position: -700px 0; }
    100% { background-position:  700px 0; }
}

.__skeleton_line {
    border-radius: 4px;
    margin-bottom: 10px;
    background: linear-gradient(
        90deg,
        #1A1F2E 25%,
        #252B3B 50%,
        #1A1F2E 75%
    );
    background-size: 700px 100%;
    animation: _shimmer 1.4s infinite linear;
}
</style>
<div class="__page_load_bar"></div>
"""


def inject_page_load_bar() -> None:
    """Inject the CSS loading bar + shimmer styles.

    Call once per page, immediately after st.set_page_config.
    The bar is rendered into the page DOM within milliseconds — before any
    DB queries run — giving instant visual feedback on navigation.
    """
    st.markdown(_LOADING_CSS, unsafe_allow_html=True)


def page_header(title: str, caption: str | None = None) -> None:
    """Render the page title + optional caption, then inject the load bar.

    Putting title + bar together means the very first visible frame already
    has the page name, so users know immediately which page they landed on.
    """
    inject_page_load_bar()
    st.title(title)
    if caption:
        st.caption(caption)
    st.markdown("---")


@contextlib.contextmanager
def loading_section(message: str = "Loading…", n_skeleton_lines: int = 6):
    """Context manager that shows an animated skeleton while heavy work runs.

    Usage:
        with loading_section("Fetching team data…"):
            members = run(fetch_members())
        # members is available here; skeleton is gone

    The skeleton is rendered immediately, replaced by the real content once
    the ``with`` block exits.  Combined with ``inject_page_load_bar()`` this
    gives a two-layer loading experience:
      1. Top bar animates from the very first frame (CSS, <10 ms)
      2. Skeleton placeholder fills the content area while DB queries run
    """
    placeholder = st.empty()

    # ── Show skeleton ─────────────────────────────────────────────────────────
    _widths = ["80%", "60%", "90%", "50%", "75%", "40%"]
    skeleton_html_lines = "".join(
        f'<div class="__skeleton_line" '
        f'style="height:16px; width:{_widths[i % len(_widths)]};"></div>'
        for i in range(n_skeleton_lines)
    )
    with placeholder.container():
        st.markdown(
            f'<p style="color:#888; font-size:0.85rem; margin-bottom:12px;">'
            f'⏳ {message}</p>'
            f'{skeleton_html_lines}',
            unsafe_allow_html=True,
        )

    try:
        yield
    finally:
        # ── Clear skeleton — real content is rendered by the caller below ────
        placeholder.empty()
