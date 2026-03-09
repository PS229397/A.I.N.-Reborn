from __future__ import annotations

import json

from ain import pipeline


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
