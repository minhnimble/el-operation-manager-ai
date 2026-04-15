"""
Helpers for loading Streamlit secrets into environment variables.

Local development can rely on `.env` without a `.streamlit/secrets.toml`.
On Streamlit Cloud, `st.secrets` is available and should override missing envs.
"""

from __future__ import annotations

import os

import streamlit as st


def load_streamlit_secrets_into_env() -> None:
    """
    Copy string values from st.secrets into os.environ if present.

    If secrets.toml is missing (common locally), silently fall back to `.env`.
    StreamlitSecretNotFoundError was removed in newer Streamlit versions, so we
    catch the underlying exceptions directly (FileNotFoundError, KeyError).
    """
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ.setdefault(key.upper(), value)
    except (FileNotFoundError, KeyError):
        # Local run without .streamlit/secrets.toml; pydantic-settings uses .env.
        return
    except Exception:
        # Catch-all for any other Streamlit secret loading errors.
        return
