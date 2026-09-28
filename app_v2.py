from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from config import settings
from core.background.snapshot_store import request_refresh
from ui.technical_v2 import (
    TechnicalV2Snapshot,
    load_technical_v2_snapshot,
    render_technical_v2,
)


def _enqueue_refresh() -> None:
    request_refresh(
        "data_v2_sync",
        {"profile": "technical_v2", "mode": settings.data_mode},
        force=False,
    )


@st.cache_data(max_entries=2, show_spinner=False)
def _cached_snapshot(
    artifact_root: str,
    publication_pointer: bytes,
    evaluation_versions: tuple[tuple[str, int], ...],
) -> TechnicalV2Snapshot:
    return load_technical_v2_snapshot(artifact_root)


@st.fragment(run_every="10s")
def _render_snapshot() -> None:
    root = Path(settings.technical_v2_artifact_root)
    pointer = root / "latest.json"
    publication = pointer.read_bytes() if pointer.exists() else b""
    run_id = str(json.loads(publication).get("run_id") or "") if publication else ""
    evaluation_versions = tuple(
        (path.name, path.stat().st_mtime_ns)
        for path in sorted((root / run_id).glob("*.csv"))
    )
    snapshot = _cached_snapshot(str(root), publication, evaluation_versions)
    if settings.data_mode == "real" and snapshot.status == "DEMO_ONLY":
        st.warning("真实行情结果尚未发布，当前显示的是演示数据。")
    render_technical_v2(snapshot, _enqueue_refresh)


def main() -> None:
    st.set_page_config(page_title="Technical V2", page_icon=":material/query_stats:", layout="wide")
    _render_snapshot()


if __name__ == "__main__":
    main()
