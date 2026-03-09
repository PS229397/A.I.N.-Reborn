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


def test_classify_agent_failure_detects_token_exhaustion():
    reason = pipeline.classify_agent_failure(
        "Request failed because maximum context length was exceeded by tokens."
    )

    assert reason == "token_exhaustion"


def test_classify_agent_failure_detects_no_response():
    reason = pipeline.classify_agent_failure(
        "The agent timed out and returned no response."
    )

    assert reason == "no_response"


def test_pause_pipeline_persists_paused_state_metadata(monkeypatch, tmp_path):
    state_file = _configure_state_paths(monkeypatch, tmp_path)
    initial_state = {
        "current_stage": "implementation",
        "completed_stages": ["scanning", "architecture", "planning_generation"],
        "started_at": None,
        "last_updated": None,
        "last_safe_stage": "planning_generation",
        "last_attempted_stage": "implementation",
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(initial_state), encoding="utf-8")

    result = pipeline.pause_pipeline(
        "token_exhaustion",
        "Agent exceeded token budget while running implementation",
        "ain continue",
    )

    assert result["current_stage"] == "paused"
    assert result["pause_reason"] == "token_exhaustion"
    assert result["pause_details"] == "Agent exceeded token budget while running implementation"
    assert result["resume_hint"] == "ain continue"
    assert result["last_safe_stage"] == "planning_generation"
    assert result["last_attempted_stage"] == "implementation"

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["current_stage"] == "paused"
    assert persisted["pause_reason"] == "token_exhaustion"
    assert persisted["pause_details"] == "Agent exceeded token budget while running implementation"
    assert persisted["resume_hint"] == "ain continue"
    assert persisted["last_safe_stage"] == "planning_generation"
    assert persisted["last_attempted_stage"] == "implementation"
