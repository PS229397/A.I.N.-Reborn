"""Compatibility state helpers used by the legacy pipeline and tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ain.models.state import PipelineState, PlannedFileChange, StageTiming

STATE_SCHEMA_VERSION = 2


class StateWriteError(RuntimeError):
    """Raised when the state payload cannot be persisted safely."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state(*, now: str | None = None, last_error: dict | None = None) -> PipelineState:
    ts = now or _now_iso()
    return PipelineState(
        version=STATE_SCHEMA_VERSION,
        current_stage="idle",
        status="idle",
        last_error=last_error,
        artifacts={},
        planned_file_changes=[],
        created_at=ts,
        updated_at=ts,
    )


def _coerce_planned_file_change(item: dict) -> PlannedFileChange:
    if not isinstance(item, dict):
        raise ValueError("planned_file_changes items must be objects")

    path = item.get("path")
    content = item.get("content")
    operation = item.get("operation", "create")
    allow_overwrite = item.get("allow_overwrite", False)
    ensure_parent_dir = item.get("ensure_parent_dir", True)

    if not isinstance(path, str) or not path:
        raise ValueError("planned_file_change.path must be a non-empty string")
    if not isinstance(content, str):
        raise ValueError("planned_file_change.content must be a string")
    if operation not in {"create", "overwrite", "skip_if_exists"}:
        raise ValueError("planned_file_change.operation must be one of create, overwrite, skip_if_exists")
    if not isinstance(allow_overwrite, bool):
        raise ValueError("planned_file_change.allow_overwrite must be a boolean")
    if not isinstance(ensure_parent_dir, bool):
        raise ValueError("planned_file_change.ensure_parent_dir must be a boolean")

    return PlannedFileChange(
        path=path,
        content=content,
        operation=operation,
        allow_overwrite=allow_overwrite,
        ensure_parent_dir=ensure_parent_dir,
    )


def _coerce_pipeline_state(payload: dict) -> PipelineState:
    if not isinstance(payload, dict):
        raise ValueError("state payload must be an object")

    current_stage = payload.get("current_stage")
    status = payload.get("status")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")

    if not isinstance(current_stage, str) or not current_stage:
        raise ValueError("current_stage must be a non-empty string")
    if not isinstance(status, str) or not status:
        raise ValueError("status must be a non-empty string")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("created_at must be a non-empty string")
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("updated_at must be a non-empty string")

    last_error = payload.get("last_error")
    if last_error is not None and not isinstance(last_error, dict):
        raise ValueError("last_error must be an object or null")

    artifacts = payload.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object")

    planned_file_changes_payload = payload.get("planned_file_changes", [])
    if planned_file_changes_payload is None:
        planned_file_changes_payload = []
    if not isinstance(planned_file_changes_payload, list):
        raise ValueError("planned_file_changes must be a list")

    planned_file_changes: list[PlannedFileChange] = []
    for item in planned_file_changes_payload:
        if isinstance(item, PlannedFileChange):
            planned_file_changes.append(item)
        else:
            planned_file_changes.append(_coerce_planned_file_change(item))

    version = payload.get("version", STATE_SCHEMA_VERSION)
    if not isinstance(version, int):
        raise ValueError("version must be an integer")

    return PipelineState(
        version=version,
        current_stage=current_stage,
        status=status,
        last_error=last_error,
        artifacts=artifacts,
        planned_file_changes=planned_file_changes,
        created_at=created_at,
        updated_at=updated_at,
    )


def _backup_corrupt_state(state_path: Path) -> Path:
    backup_path = state_path.with_name(f"{state_path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    backup_path.write_bytes(state_path.read_bytes())
    return backup_path


def load_state(*, state_path: Path) -> PipelineState:
    if not state_path.exists():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = _default_state()
        save_state(state, state_path=state_path)
        return state

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        state = _coerce_pipeline_state(payload)
        if state.version != STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported version: {state.version}")
        return state
    except Exception:
        backup_path = _backup_corrupt_state(state_path)
        state = _default_state(
            now=_now_iso(),
            last_error={
                "code": "STATE_CORRUPT",
                "message": "State file was corrupt and has been repaired.",
                "details": {"backup_path": str(backup_path)},
            },
        )
        save_state(state, state_path=state_path)
        return state


def save_state(state: PipelineState, *, state_path: Path) -> None:
    if state.version != STATE_SCHEMA_VERSION:
        raise StateWriteError(
            f"Refusing to write state schema version {state.version}; expected {STATE_SCHEMA_VERSION}."
        )

    state.updated_at = _now_iso()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


def record_stage_timing(timing: StageTiming) -> None:
    """Accept stage timing updates without requiring the newer service layer."""
