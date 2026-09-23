from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.background.snapshot_store import ArtifactMismatch, read_latest_manifest
from core.technical_v2.contracts import json_safe, sha256_json


@dataclass(frozen=True)
class SnapshotExport:
    csv_text: str
    json_text: str


@dataclass(frozen=True)
class TechnicalV2Snapshot:
    status: str
    manifest: dict[str, Any]
    rows: pd.DataFrame
    features: pd.DataFrame
    evaluations: dict[str, pd.DataFrame] = field(default_factory=dict)
    reason: str = ""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactMismatch(f"cannot read Technical V2 artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactMismatch(f"Technical V2 artifact is not an object: {path}")
    return payload


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            try:
                missing = bool(pd.isna(value))
            except (TypeError, ValueError):
                missing = False
            clean[str(key)] = None if missing else json_safe(value)
        records.append(clean)
    return records


def load_technical_v2_snapshot(artifact_root: str | Path) -> TechnicalV2Snapshot:
    root = Path(artifact_root)
    manifest = read_latest_manifest(root)
    if manifest is None:
        return TechnicalV2Snapshot("NO_PUBLICATION", {}, pd.DataFrame(), pd.DataFrame())
    run_id = str(manifest["run_id"])
    as_of = str(manifest["as_of_trade_date"])
    coverage_hash = str(manifest.get("coverage_hash") or "")
    coverage_path = root / run_id / "coverage" / f"{coverage_hash}.json"
    coverage = _read_object(coverage_path)
    if coverage.get("run_id") != run_id or coverage.get("as_of_trade_date") != as_of:
        raise ArtifactMismatch("coverage artifact binding does not match publication")
    rows = coverage.get("rows")
    if not isinstance(rows, list) or sha256_json(rows) != coverage_hash:
        raise ArtifactMismatch("coverage artifact hash does not match publication")
    prediction_rows = pd.DataFrame(rows)

    feature_path = root / run_id / "features" / "latest.json"
    features = pd.DataFrame()
    if feature_path.is_file():
        feature_payload = _read_object(feature_path)
        if (
            feature_payload.get("run_id") != run_id
            or feature_payload.get("as_of_trade_date") != as_of
            or not isinstance(feature_payload.get("rows"), list)
        ):
            raise ArtifactMismatch("feature artifact binding does not match publication")
        features = pd.DataFrame(feature_payload["rows"])
        if not features.empty and "code" in features.columns:
            metadata = features.rename(columns={"code": "entity_id"})
            overlap = [
                column
                for column in metadata.columns
                if column in prediction_rows.columns and column != "entity_id"
            ]
            metadata = metadata.drop(columns=overlap)
            prediction_rows = prediction_rows.merge(
                metadata,
                on="entity_id",
                how="left",
                validate="many_to_one",
            )

    evaluations: dict[str, pd.DataFrame] = {}
    for name in ("summary_metrics", "coverage_summary", "candidate_comparison", "probability_metrics"):
        path = root / run_id / f"{name}.csv"
        if path.is_file():
            try:
                evaluations[name] = pd.read_csv(path)
            except (OSError, pd.errors.ParserError) as exc:
                raise ArtifactMismatch(f"cannot read evaluation artifact {path}: {exc}") from exc
    status = "DEMO_ONLY" if manifest.get("data_source_mode") == "demo" else "READY"
    return TechnicalV2Snapshot(status, manifest, prediction_rows, features, evaluations)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def format_ratio(value: Any) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number:.2%}"


def _format_score(value: Any) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number:.1f} 分"


def build_display_row(row: dict[str, Any]) -> dict[str, Any]:
    display = dict(row)
    if "formula_score" in display:
        display["formula_score"] = _format_score(display["formula_score"])
    for column in (
        "p_raw_up",
        "p_raw_flat",
        "p_raw_down",
        "p_cal_up",
        "p_cal_flat",
        "p_cal_down",
        "Q01",
        "Q02",
        "Q05",
        "Q06",
    ):
        if column in display:
            display[column] = format_ratio(display[column])
    reasons = display.get("reason_codes")
    if isinstance(reasons, (list, tuple)):
        display["reason_codes"] = ", ".join(str(value) for value in reasons)
    blockers = display.get("trade_blockers")
    if isinstance(blockers, (list, tuple)):
        display["trade_blockers"] = ", ".join(str(value) for value in blockers)
    return display


def build_snapshot_export(rows: pd.DataFrame) -> SnapshotExport:
    safe = rows.copy()
    csv_buffer = StringIO()
    safe.to_csv(csv_buffer, index=False)
    json_text = json.dumps(
        _json_records(safe),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return SnapshotExport(csv_buffer.getvalue(), json_text + "\n")


def _view_columns(frame: pd.DataFrame, preferred: tuple[str, ...]) -> pd.DataFrame:
    columns = [column for column in preferred if column in frame.columns]
    if not columns:
        return pd.DataFrame(index=frame.index)
    return pd.DataFrame.from_records(
        [build_display_row(row) for row in frame[columns].to_dict(orient="records")]
    )


def _normalized_sector(frame: pd.DataFrame) -> pd.Series:
    namespace = frame.get("namespace", pd.Series(index=frame.index, dtype=object)).fillna("UNASSIGNED")
    sector = frame.get("sector_id", pd.Series(index=frame.index, dtype=object)).fillna("UNASSIGNED")
    return namespace.astype(str) + ":" + sector.astype(str)


def render_technical_v2(
    manifest: TechnicalV2Snapshot | dict[str, Any],
    enqueue_refresh: Callable[[], None],
) -> None:
    import streamlit as st

    snapshot = manifest if isinstance(manifest, TechnicalV2Snapshot) else TechnicalV2Snapshot(
        status=str(manifest.get("status") or "UNKNOWN"),
        manifest=dict(manifest.get("manifest") or manifest),
        rows=pd.DataFrame(manifest.get("rows") or []),
        features=pd.DataFrame(manifest.get("features") or []),
    )
    st.markdown(
        """
        <style>
        .stApp { background: #f7f8fa; color: #17202a; }
        h1, h2, h3, p, label, button { letter-spacing: 0; }
        h1 { font-size: 1.75rem; margin-bottom: 0.15rem; }
        h2 { font-size: 1.15rem; }
        [data-testid="stMetric"] { border-left: 3px solid #237a57; padding-left: 0.75rem; }
        [data-testid="stDataFrame"] { border: 1px solid #dfe3e8; border-radius: 4px; }
        .status-line { color: #52606d; font-size: 0.86rem; margin-bottom: 0.8rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    title_col, refresh_col = st.columns([8, 1])
    with title_col:
        st.title("Technical V2")
    with refresh_col:
        if st.button("刷新", icon=":material/refresh:", width="stretch"):
            enqueue_refresh()
            st.toast("刷新请求已提交")

    if snapshot.status == "NO_PUBLICATION":
        st.warning("NO_PUBLICATION")
        return
    if snapshot.rows.empty:
        st.error("PUBLICATION_WITHOUT_COVERAGE_ROWS")
        return

    meta = snapshot.manifest
    st.markdown(
        '<div class="status-line">'
        f"{snapshot.status} · {meta.get('as_of_trade_date', '-')} · "
        f"{meta.get('mode', '-')} · {meta.get('data_source_mode', '-')} · "
        f"run {meta.get('run_id', '-')}"
        "</div>",
        unsafe_allow_html=True,
    )
    routes = meta.get("routes") if isinstance(meta.get("routes"), dict) else {}
    formula_route = routes.get("formula") if isinstance(routes.get("formula"), dict) else {}
    jev_route = routes.get("jev") if isinstance(routes.get("jev"), dict) else {}
    metrics = st.columns(5)
    metrics[0].metric("名册股票", snapshot.rows["entity_id"].nunique())
    metrics[1].metric("覆盖状态行", len(snapshot.rows))
    metrics[2].metric("有效预测", int(snapshot.rows["prediction_status"].astype(str).eq("OK").sum()))
    metrics[3].metric("公式路线", str(formula_route.get("status") or "UNKNOWN"))
    metrics[4].metric("Jev 路线", str(jev_route.get("status") or "UNKNOWN"))

    rows = snapshot.rows.copy()
    rows["sector_key"] = _normalized_sector(rows)
    h5 = rows.loc[pd.to_numeric(rows["horizon"], errors="coerce").eq(5)].copy()
    tabs = st.tabs(["行业概览", "行业个股", "A/B 对比", "操作与纸面账户", "评估与下载"])

    with tabs[0]:
        sector_summary = (
            h5.groupby("sector_key", dropna=False)
            .agg(
                股票数=("entity_id", "nunique"),
                状态行=("entity_id", "size"),
                有效预测=("prediction_status", lambda values: int(values.astype(str).eq("OK").sum())),
            )
            .reset_index()
            .rename(columns={"sector_key": "行业"})
        )
        st.dataframe(sector_summary, hide_index=True, width="stretch")

    with tabs[1]:
        sector_options = sorted(h5["sector_key"].astype(str).unique())
        selected_sector = st.selectbox("行业", sector_options, index=0)
        query = st.text_input("股票代码或名称", value="").strip().lower()
        sector_rows = h5.loc[h5["sector_key"].eq(selected_sector)].copy()
        if query:
            names = sector_rows.get("name", pd.Series("", index=sector_rows.index)).fillna("").astype(str)
            mask = sector_rows["entity_id"].astype(str).str.lower().str.contains(query, regex=False)
            mask |= names.str.lower().str.contains(query, regex=False)
            sector_rows = sector_rows.loc[mask]
        page_size = st.select_slider("每页", options=(25, 50, 100, 200), value=50)
        max_page = max(1, (len(sector_rows) + page_size - 1) // page_size)
        page = st.number_input("页码", min_value=1, max_value=max_page, value=1, step=1)
        start = (int(page) - 1) * page_size
        stock_view = _view_columns(
            sector_rows.iloc[start : start + page_size],
            (
                "entity_id",
                "name",
                "method",
                "prediction_status",
                "forecast_class",
                "observed_trend",
                "formula_score",
                "p_cal_up",
                "p_raw_up",
                "factor_valid_count",
                "Q01",
                "Q02",
                "trade_eligible",
                "reason_codes",
            ),
        )
        st.dataframe(stock_view, hide_index=True, width="stretch")
        with st.expander("1/3 日诊断"):
            diagnostics = rows.loc[
                rows["sector_key"].eq(selected_sector)
                & pd.to_numeric(rows["horizon"], errors="coerce").isin((1, 3))
            ]
            st.dataframe(
                _view_columns(
                    diagnostics,
                    (
                        "entity_id",
                        "name",
                        "horizon",
                        "method",
                        "prediction_status",
                        "forecast_class",
                        "formula_score",
                        "p_raw_up",
                        "reason_codes",
                    ),
                ),
                hide_index=True,
                width="stretch",
            )

    with tabs[2]:
        compare_fields = [
            column
            for column in (
                "entity_id",
                "name",
                "method",
                "prediction_status",
                "forecast_class",
                "formula_score",
                "p_raw_up",
                "p_cal_up",
                "probability_validation_status",
            )
            if column in h5.columns
        ]
        st.dataframe(
            _view_columns(h5[compare_fields], tuple(compare_fields)),
            hide_index=True,
            width="stretch",
        )

    with tabs[3]:
        action_rows = _view_columns(
            h5,
            (
                "entity_id",
                "name",
                "method",
                "research_intent",
                "account_action",
                "order_quantity",
                "trade_eligible",
                "trade_blockers",
                "intent_reason_codes",
                "reason_codes",
            ),
        )
        st.dataframe(action_rows, hide_index=True, width="stretch")
        st.caption("account_type=paper / readonly_user；live_trading=NOT_CONNECTED")

    with tabs[4]:
        if snapshot.evaluations:
            evaluation_name = st.selectbox("评估表", sorted(snapshot.evaluations))
            st.dataframe(snapshot.evaluations[evaluation_name], hide_index=True, width="stretch")
        else:
            st.info("EVALUATION_NOT_AVAILABLE")
        exported = build_snapshot_export(snapshot.rows)
        download_columns = st.columns(2)
        download_columns[0].download_button(
            "下载完整 CSV",
            data=exported.csv_text,
            file_name=f"technical-v2-{meta.get('run_id', 'snapshot')}.csv",
            mime="text/csv",
            width="stretch",
        )
        download_columns[1].download_button(
            "下载完整 JSON",
            data=exported.json_text,
            file_name=f"technical-v2-{meta.get('run_id', 'snapshot')}.json",
            mime="application/json",
            width="stretch",
        )
