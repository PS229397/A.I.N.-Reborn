from __future__ import annotations

import sys

import pytest

from ain import pipeline


def _configure_runtime_paths(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / ".ai-pipeline"
    docs_dir = tmp_path / "docs"
    monkeypatch.setattr(pipeline, "PIPELINE_DIR", pipeline_dir)
    monkeypatch.setattr(pipeline, "SCAN_DIR", pipeline_dir / "scan")
    monkeypatch.setattr(pipeline, "PROMPTS_DIR", pipeline_dir / "prompts")
    monkeypatch.setattr(pipeline, "LOGS_DIR", pipeline_dir / "logs")
    monkeypatch.setattr(pipeline, "APPROVALS_DIR", pipeline_dir / "approvals")
    monkeypatch.setattr(pipeline, "DOCS_DIR", docs_dir)


def test_resolve_continue_stage_prefers_last_attempted_when_paused():
    state = {
        "current_stage": "paused",
        "last_attempted_stage": "implementation",
        "last_safe_stage": "planning_generation",
    }

    result = pipeline.resolve_continue_stage(state)

    assert result == "implementation"


def test_resolve_continue_stage_uses_last_safe_progression_when_paused_without_attempted():
    state = {
        "current_stage": "paused",
        "last_attempted_stage": None,
        "last_safe_stage": "planning_generation",
    }

    result = pipeline.resolve_continue_stage(state)

    assert result == "task_creation"


def test_resolve_continue_stage_uses_last_safe_progression_for_recoverable_failed_state():
    state = {
        "current_stage": pipeline.FAILED,
        "pause_reason": "token_exhaustion",
        "last_safe_stage": "planning_generation",
    }

    result = pipeline.resolve_continue_stage(state)

    assert result == "task_creation"


def test_resolve_continue_stage_returns_none_when_done():
    assert pipeline.resolve_continue_stage({"current_stage": "done"}) is None


def test_resolve_continue_stage_raises_for_non_recoverable_failed_state():
    state = {
        "current_stage": pipeline.FAILED,
        "pause_reason": "unknown",
        "last_safe_stage": "planning_generation",
    }

    with pytest.raises(ValueError, match="not recoverable via continue"):
        pipeline.resolve_continue_stage(state)


def test_continue_command_routes_to_resolved_stage(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    called = {}

    monkeypatch.setattr(
        pipeline,
        "load_state",
        lambda: {"current_stage": "paused", "last_attempted_stage": "implementation"},
    )
    monkeypatch.setattr(pipeline, "resolve_continue_stage", lambda _state: "implementation")
    monkeypatch.setattr(pipeline, "show_status", lambda _state: None)
    monkeypatch.setattr(pipeline, "info", lambda _text: None)
    monkeypatch.setattr(pipeline, "success", lambda _text: None)

    def fake_run_pipeline(start_stage=None, single_stage=False):
        called["start_stage"] = start_stage
        called["single_stage"] = single_stage

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", ["ain", "continue"])

    pipeline.main()

    assert called == {"start_stage": "implementation", "single_stage": False}


def test_resume_alias_routes_like_run_resume(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    resume_called = {}
    run_resume_called = {}

    def fake_run_pipeline_for_resume(start_stage=None, single_stage=False):
        resume_called["start_stage"] = start_stage
        resume_called["single_stage"] = single_stage

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline_for_resume)
    monkeypatch.setattr(sys, "argv", ["ain", "resume", "architecture"])
    pipeline.main()

    def fake_run_pipeline_for_run_resume(start_stage=None, single_stage=False):
        run_resume_called["start_stage"] = start_stage
        run_resume_called["single_stage"] = single_stage

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline_for_run_resume)
    monkeypatch.setattr(sys, "argv", ["ain", "run", "--resume", "architecture"])
    pipeline.main()

    assert resume_called == {"start_stage": "architecture", "single_stage": False}
    assert run_resume_called == {"start_stage": "architecture", "single_stage": False}
