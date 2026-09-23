from __future__ import annotations

import streamlit as st

from config import settings
from core.background.snapshot_store import request_refresh
from ui.technical_v2 import load_technical_v2_snapshot, render_technical_v2


def _enqueue_refresh() -> None:
    request_refresh(
        "data_v2_sync",
        {"profile": "technical_v2", "data_source_mode": settings.data_mode},
        force=True,
    )


def main() -> None:
    st.set_page_config(page_title="Technical V2", page_icon=":material/query_stats:", layout="wide")
    snapshot = load_technical_v2_snapshot(settings.technical_v2_artifact_root)
    render_technical_v2(snapshot, _enqueue_refresh)


if __name__ == "__main__":
    main()
