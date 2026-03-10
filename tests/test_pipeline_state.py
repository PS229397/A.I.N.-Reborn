from __future__ import annotations

import json
from pathlib import Path

import pytest

from ain import pipeline
from ain.models.state import PipelineState, PlannedFileChange
from ain.services import state_service
from ain.services.state_service import STATE_SCHEMA_VERSION, StateWriteError


def _configure_state_paths(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / ".ai-pipeline"
    state_file = pipeline_dir / "state.json"
    monkeypatch.setattr(pipeline, "PIPELINE_DIR", pipeline_dir)
    monkeypatch.setattr(pipeline, "STATE_FILE", state_file)
    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    return state_file


def test_load_state_with_backfill_defaults_and_preserves_existing_values():
    legacy_state = {
        "current_stage": "planning_generation",
        "completed_stages": ["scanning", "architecture"],
        "pause_reason": "agent_unavailable",
        "fallback_mode": {"task_creation": "codex"},
    }

    result = pipeline.load_state_with_backfill(legacy_state)

    assert result["current_stage"] == "planning_generation"
    assert result["completed_stages"] == ["scanning", "architecture"]
    assert result["pause_reason"] == "agent_unavailable"
    assert result["fallback_mode"] == {"task_creation": "codex"}
    assert result["last_safe_stage"] == "idle"
    assert result["last_attempted_stage"] is None
    assert result["pause_details"] is None
    assert result["resume_hint"] is None
    assert result["checkpoint_version"] == 1
    assert result["notification_channel"] == {}


def test_checkpoint_before_stage_persists_last_attempted_stage(monkeypatch, tmp_path):
    state_file = _configure_state_paths(monkeypatch, tmp_path)
    state = {
        "current_stage": "planning_questions",
        "completed_stages": [],
        "started_at": None,
        "last_updated": None,
    }

    result = pipeline.checkpoint_before_stage("planning_generation", state=state)

    assert result["last_attempted_stage"] == "planning_generation"
    assert result["current_stage"] == "planning_questions"
    assert state_file.exists()

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["last_attempted_stage"] == "planning_generation"
    assert persisted["current_stage"] == "planning_questions"


def test_checkpoint_after_stage_success_persists_safe_stage_and_next_stage(monkeypatch, tmp_path):
    state_file = _configure_state_paths(monkeypatch, tmp_path)
    state = {
        "current_stage": "planning_generation",
        "completed_stages": [],
        "started_at": None,
        "last_updated": None,
    }

    result = pipeline.checkpoint_after_stage_success(
        "planning_generation", "task_creation", state=state
    )

    assert result["last_safe_stage"] == "planning_generation"
    assert result["current_stage"] == "task_creation"
    assert "planning_generation" in result["completed_stages"]
    assert state_file.exists()

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["last_safe_stage"] == "planning_generation"
    assert persisted["current_stage"] == "task_creation"
    assert "planning_generation" in persisted["completed_stages"]


def test_load_state_creates_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(state_service, "_now_iso", lambda: "2026-01-01T00:00:00+00:00")
    state_path = tmp_path / "state.json"

    result = state_service.load_state(state_path=state_path)

    assert state_path.exists()
    assert result.version == STATE_SCHEMA_VERSION
    assert result.current_stage == "idle"
    assert result.status == "idle"
    assert result.created_at == "2026-01-01T00:00:00+00:00"
    assert result.updated_at == "2026-01-01T00:00:00+00:00"

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["created_at"] == "2026-01-01T00:00:00+00:00"
    assert persisted["current_stage"] == "idle"
    assert persisted["status"] == "idle"
    assert persisted["version"] == STATE_SCHEMA_VERSION


def test_load_state_repairs_corrupt_json_and_keeps_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(state_service, "_now_iso", lambda: "2026-02-02T12:00:00+00:00")
    state_path = tmp_path / "state.json"
    state_path.write_text("{not json", encoding="utf-8")

    result = state_service.load_state(state_path=state_path)

    backup_files = list(state_path.parent.glob("state.json.bak-*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_bytes() == b"{not json"

    assert result.last_error is not None
    assert result.last_error["code"] == "STATE_CORRUPT"
    assert "backup_path" in result.last_error["details"]
    assert Path(result.last_error["details"]["backup_path"]).exists()

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["version"] == STATE_SCHEMA_VERSION
    assert persisted["last_error"]["code"] == "STATE_CORRUPT"
    assert persisted["created_at"] == "2026-02-02T12:00:00+00:00"


def test_save_state_updates_timestamp_and_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(state_service, "_now_iso", lambda: "2026-03-03T03:03:03+00:00")
    state_path = tmp_path / "state.json"
    state = PipelineState(
        version=STATE_SCHEMA_VERSION,
        current_stage="scanning",
        status="running",
        last_error=None,
        artifacts={"repo": "ok"},
        created_at="2026-03-03T00:00:00+00:00",
        updated_at="2026-03-03T00:00:00+00:00",
    )

    state_service.save_state(state, state_path=state_path)

    assert state.updated_at == "2026-03-03T03:03:03+00:00"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["updated_at"] == "2026-03-03T03:03:03+00:00"
    assert persisted["status"] == "running"
    assert persisted["artifacts"] == {"repo": "ok"}


def test_save_state_rejects_wrong_version(tmp_path):
    state_path = tmp_path / "state.json"
    state = PipelineState(
        version=1,
        current_stage="idle",
        status="idle",
        last_error=None,
        artifacts={},
        created_at="2026-03-01T00:00:00+00:00",
        updated_at="2026-03-01T00:00:00+00:00",
    )

    with pytest.raises(StateWriteError):
        state_service.save_state(state, state_path=state_path)

    assert not state_path.exists()


def test_planned_file_changes_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(state_service, "_now_iso", lambda: "2026-04-04T04:04:04+00:00")
    state_path = tmp_path / "state.json"
    planned_change = PlannedFileChange(
        path="docs/test.md",
        content="# hello world",
        operation="create",
        allow_overwrite=False,
        ensure_parent_dir=True,
    )

    state = PipelineState(
        version=STATE_SCHEMA_VERSION,
        current_stage="implementation",
        status="running",
        last_error=None,
        artifacts={"sample": True},
        planned_file_changes=[planned_change],
        created_at="2026-04-04T00:00:00+00:00",
        updated_at="2026-04-04T00:00:00+00:00",
    )

    state_service.save_state(state, state_path=state_path)
    loaded = state_service.load_state(state_path=state_path)

    assert len(loaded.planned_file_changes) == 1
    assert loaded.planned_file_changes[0] == planned_change

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["planned_file_changes"][0]["path"] == "docs/test.md"
    assert persisted["planned_file_changes"][0]["content"] == "# hello world"
