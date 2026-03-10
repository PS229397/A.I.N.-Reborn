from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

from ain import pipeline


def _configure_runtime_paths(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / ".ai-pipeline"
    docs_dir = tmp_path / "docs"

    monkeypatch.setattr(pipeline, "PIPELINE_DIR", pipeline_dir)
    monkeypatch.setattr(pipeline, "STATE_FILE", pipeline_dir / "state.json")
    monkeypatch.setattr(pipeline, "SCAN_DIR", pipeline_dir / "scan")
    monkeypatch.setattr(pipeline, "PROMPTS_DIR", pipeline_dir / "prompts")
    monkeypatch.setattr(pipeline, "LOGS_DIR", pipeline_dir / "logs")
    monkeypatch.setattr(pipeline, "APPROVALS_DIR", pipeline_dir / "approvals")
    monkeypatch.setattr(pipeline, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(
        pipeline,
        "PLANNING_APPROVED_FLAG",
        pipeline.APPROVALS_DIR / "planning_approved.flag",
    )

    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "error", lambda *_args, **_kwargs: None)


def test_run_command_routes_to_pipeline_from_current_stage(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    called = {}

    def fake_run_pipeline(start_stage=None, single_stage=False):
        called["start_stage"] = start_stage
        called["single_stage"] = single_stage

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", ["ain", "run"])

    pipeline.main()

    assert called == {"start_stage": None, "single_stage": False}


def test_run_resume_routes_with_requested_stage(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    called = {}

    def fake_run_pipeline(start_stage=None, single_stage=False):
        called["start_stage"] = start_stage
        called["single_stage"] = single_stage

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", ["ain", "run", "--resume", "architecture"])

    pipeline.main()

    assert called == {"start_stage": "architecture", "single_stage": False}


def test_run_stage_routes_with_single_stage_enabled(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    called = {}

    def fake_run_pipeline(start_stage=None, single_stage=False):
        called["start_stage"] = start_stage
        called["single_stage"] = single_stage

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", ["ain", "run", "--stage", "architecture"])

    pipeline.main()

    assert called == {"start_stage": "architecture", "single_stage": True}


def test_status_flag_loads_and_displays_state(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    expected_state = {"current_stage": "paused", "completed_stages": ["scanning"]}
    seen = {}

    monkeypatch.setattr(pipeline, "load_state", lambda: expected_state)
    monkeypatch.setattr(
        pipeline,
        "show_status",
        lambda state: seen.setdefault("state", state),
    )
    monkeypatch.setattr(sys, "argv", ["ain", "--status"])

    pipeline.main()

    assert seen["state"] is expected_state


def test_approve_flag_writes_approval_and_advances_waiting_stage(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    transitions = {}

    state = {
        "current_stage": "waiting_approval",
        "completed_stages": ["scanning", "architecture", "planning_generation"],
        "started_at": None,
        "last_updated": None,
    }

    monkeypatch.setattr(pipeline, "load_state", lambda: state)

    def fake_set_stage(stage, incoming_state=None):
        transitions["stage"] = stage
        transitions["state"] = incoming_state
        return incoming_state or state

    monkeypatch.setattr(pipeline, "set_stage", fake_set_stage)
    monkeypatch.setattr(sys, "argv", ["ain", "--approve"])

    pipeline.main()

    assert pipeline.PLANNING_APPROVED_FLAG.exists()
    approval_text = pipeline.PLANNING_APPROVED_FLAG.read_text(encoding="utf-8")
    assert approval_text.startswith("Approved: ")
    assert transitions["stage"] == "implementation"
    assert transitions["state"] is state


def test_reset_flag_reinitializes_state_and_removes_approval(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)

    pipeline.APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    pipeline.PLANNING_APPROVED_FLAG.write_text("Approved: earlier\n", encoding="utf-8")
    pipeline.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    pipeline.STATE_FILE.write_text(
        json.dumps(
            {
                "current_stage": "implementation",
                "completed_stages": ["scanning", "architecture"],
                "started_at": "2026-03-06T10:00:00+00:00",
                "last_updated": "2026-03-06T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["ain", "--reset"])

    pipeline.main()

    assert not pipeline.PLANNING_APPROVED_FLAG.exists()

    persisted = json.loads(pipeline.STATE_FILE.read_text(encoding="utf-8"))
    assert persisted["current_stage"] == "idle"
    assert persisted["completed_stages"] == []
    assert persisted["branch"] is None
    assert persisted["started_at"] is None
    assert persisted["last_updated"] is not None


def test_status_subcommand_loads_and_displays_state(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    expected_state = {"current_stage": "paused", "completed_stages": ["scanning"]}
    seen = {}

    monkeypatch.setattr(pipeline, "load_state", lambda: expected_state)
    monkeypatch.setattr(
        pipeline,
        "show_status",
        lambda state: seen.setdefault("state", state),
    )
    monkeypatch.setattr(sys, "argv", ["ain", "status"])

    pipeline.main()

    assert seen["state"] is expected_state


def test_approve_subcommand_writes_approval_and_advances_waiting_stage(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    transitions = {}

    state = {
        "current_stage": "waiting_approval",
        "completed_stages": ["scanning", "architecture", "planning_generation"],
        "started_at": None,
        "last_updated": None,
    }

    monkeypatch.setattr(pipeline, "load_state", lambda: state)

    def fake_set_stage(stage, incoming_state=None):
        transitions["stage"] = stage
        transitions["state"] = incoming_state
        return incoming_state or state

    monkeypatch.setattr(pipeline, "set_stage", fake_set_stage)
    monkeypatch.setattr(sys, "argv", ["ain", "approve"])

    pipeline.main()

    assert pipeline.PLANNING_APPROVED_FLAG.exists()
    assert transitions["stage"] == "implementation"
    assert transitions["state"] is state


def test_version_subcommand_prints_version(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "run_command_output", lambda *_args, **_kwargs: "abc123\n")
    monkeypatch.setattr(sys, "argv", ["ain", "version", "--short"])

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        pipeline.main()

    assert stdout.getvalue().strip() == "0.1.8"


def test_clean_subcommand_routes_to_workspace_cleanup(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    seen = {}

    monkeypatch.setattr(
        pipeline,
        "clean_workspace",
        lambda silent=False: seen.setdefault("silent", silent),
    )
    monkeypatch.setattr(sys, "argv", ["ain", "clean"])

    pipeline.main()

    assert seen == {"silent": False}


def test_clean_workspace_clears_docs_contents(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    pipeline.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (pipeline.DOCS_DIR / "TASKS.md").write_text("# Tasks\n", encoding="utf-8")
    (pipeline.DOCS_DIR / "nested").mkdir()
    (pipeline.DOCS_DIR / "nested" / "artifact.txt").write_text("artifact", encoding="utf-8")

    monkeypatch.setattr(pipeline, "load_config", lambda: {})
    monkeypatch.setattr(pipeline, "save_state", lambda _state: None)

    pipeline.clean_workspace(silent=True)

    assert pipeline.DOCS_DIR.exists()
    assert list(pipeline.DOCS_DIR.iterdir()) == []
