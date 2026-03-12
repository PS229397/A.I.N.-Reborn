"""Compatibility state helpers used by the legacy pipeline and tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ain.models.state import (
    MultilineInputMode,
    MultilineInputState,
    PipelineState,
    PlannedFileChange,
    StageTiming,
)

STATE_SCHEMA_VERSION = 3


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


def _coerce_multiline_input(item: dict | MultilineInputState | None) -> MultilineInputState | None:
    if item is None:
        return None
    if isinstance(item, MultilineInputState):
        return item
    if not isinstance(item, dict):
        raise ValueError("multiline_input must be an object or null")

    input_id = item.get("id")
    title = item.get("title")
    prompt = item.get("prompt")
    if not isinstance(input_id, str) or not input_id:
        raise ValueError("multiline_input.id must be a non-empty string")
    if not isinstance(title, str) or not title:
        raise ValueError("multiline_input.title must be a non-empty string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("multiline_input.prompt must be a non-empty string")

    initial_text = item.get("initial_text")
    if initial_text is not None and not isinstance(initial_text, str):
        raise ValueError("multiline_input.initial_text must be a string or null")

    value = item.get("value", initial_text or "")
    if not isinstance(value, str):
        raise ValueError("multiline_input.value must be a string")

    mode_raw = item.get("mode", MultilineInputMode.FEATURE_DESCRIPTION.value)
    try:
        mode = MultilineInputMode(mode_raw)
    except ValueError as exc:
        raise ValueError("multiline_input.mode must be a valid MultilineInputMode value") from exc

    is_active = item.get("is_active", False)
    if not isinstance(is_active, bool):
        raise ValueError("multiline_input.is_active must be a boolean")

    source_stage = item.get("source_stage", "")
    if not isinstance(source_stage, str):
        raise ValueError("multiline_input.source_stage must be a string")

    return MultilineInputState(
        id=input_id,
        title=title,
        prompt=prompt,
        initial_text=initial_text,
        value=value,
        mode=mode,
        is_active=is_active,
        source_stage=source_stage,
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

    multiline_input = _coerce_multiline_input(payload.get("multiline_input"))

    feature_description = payload.get("feature_description", "")
    if feature_description is None:
        feature_description = ""
    if not isinstance(feature_description, str):
        raise ValueError("feature_description must be a string")

    task_denial_feedback_payload = payload.get("task_denial_feedback_by_task_id", {})
    if task_denial_feedback_payload is None:
        task_denial_feedback_payload = {}
    if not isinstance(task_denial_feedback_payload, dict):
        raise ValueError("task_denial_feedback_by_task_id must be an object")

    task_denial_feedback_by_task_id: dict[str, str] = {}
    for task_id, feedback in task_denial_feedback_payload.items():
        if not isinstance(task_id, str):
            raise ValueError("task_denial_feedback_by_task_id keys must be strings")
        if not isinstance(feedback, str):
            raise ValueError("task_denial_feedback_by_task_id values must be strings")
        task_denial_feedback_by_task_id[task_id] = feedback

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
        multiline_input=multiline_input,
        feature_description=feature_description,
        task_denial_feedback_by_task_id=task_denial_feedback_by_task_id,
        created_at=created_at,
        updated_at=updated_at,
    )


def _backup_corrupt_state(state_path: Path) -> Path:
    # Store backups in the pipeline logs dir to avoid cluttering .ai-pipeline root.
    state_logs_dir = state_path.parent / "state_logs"
    state_logs_dir.mkdir(parents=True, exist_ok=True)
    backup_path = state_logs_dir / f"{state_path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
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
        raw_version = payload.get("version", STATE_SCHEMA_VERSION)
        if not isinstance(raw_version, int):
            raise ValueError("version must be an integer")
        if raw_version > STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported version: {raw_version}")

        migration_needed = raw_version < STATE_SCHEMA_VERSION
        if "multiline_input" not in payload:
            migration_needed = True
        if "feature_description" not in payload:
            migration_needed = True
        if "task_denial_feedback_by_task_id" not in payload:
            migration_needed = True

        state = _coerce_pipeline_state(payload)
        if migration_needed:
            state.version = STATE_SCHEMA_VERSION
            save_state(state, state_path=state_path)
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


def start_multiline_input(state: PipelineState, ctx: MultilineInputState, *, state_path: Path) -> None:
    """
    Activate a multiline input session and persist the updated pipeline state.

    Idempotent if invoked multiple times with the same context id; preserves any
    in-progress buffer already stored in state.
    """

    buffer_value = ctx.value or ctx.initial_text or ""
    existing = state.multiline_input

    if existing and existing.id == ctx.id:
        existing.title = ctx.title
        existing.prompt = ctx.prompt
        existing.initial_text = ctx.initial_text
        existing.mode = ctx.mode
        existing.source_stage = ctx.source_stage
        existing.is_active = True
        if not existing.value:
            existing.value = buffer_value
        save_state(state, state_path=state_path)
        return

    ctx.value = buffer_value
    ctx.is_active = True
    state.multiline_input = ctx
    save_state(state, state_path=state_path)


def complete_multiline_input(
    state: PipelineState,
    value: str,
    mode: MultilineInputMode,
    context_id: str,
    *,
    state_path: Path,
) -> None:
    """
    Persist the submitted multiline input and clear the active session.

    - FEATURE_DESCRIPTION writes to `state.feature_description`.
    - TASK_DENIAL_FEEDBACK stores the value in `task_denial_feedback_by_task_id`
      keyed by the provided context id (expected to reference the task).
    """

    active = state.multiline_input
    target_mode = mode

    if active and active.id == context_id:
        target_mode = active.mode
        state.multiline_input = None

    if target_mode == MultilineInputMode.FEATURE_DESCRIPTION:
        state.feature_description = value
    elif target_mode == MultilineInputMode.TASK_DENIAL_FEEDBACK:
        state.task_denial_feedback_by_task_id[context_id] = value

    save_state(state, state_path=state_path)


def cancel_multiline_input(state: PipelineState, context_id: str, *, state_path: Path) -> None:
    """
    Cancel the active multiline input session and persist state without altering
    any stored descriptions or feedback values.
    """

    active = state.multiline_input
    if not active or active.id != context_id:
        return

    state.multiline_input = None
    save_state(state, state_path=state_path)
