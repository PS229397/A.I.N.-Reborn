from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from ain import pipeline
from ain.cli import main as cli_main
from ain.models.state import HealthSummary
from ain.services import state_service
from ain.services.state_service import STATE_SCHEMA_VERSION


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
    monkeypatch.setattr(pipeline, "TASK_GRAPH_FILE", docs_dir / "TASK_GRAPH.json")

    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "notify",
        lambda *_args, **_kwargs: {"success": True, "mode": "test", "details": "test"},
    )
    monkeypatch.setattr(pipeline, "ensure_config", lambda: None)
    monkeypatch.setattr(pipeline, "load_config", lambda: {})

    return pipeline.STATE_FILE


def _install_recovery_test_graph(monkeypatch):
    call_counts = {"architecture": 0}

    def run_scanning(state, _config):
        pipeline.checkpoint_after_stage_success("scanning", "architecture", state)

    def run_architecture(state, _config):
        call_counts["architecture"] += 1
        if call_counts["architecture"] == 1:
            raise RuntimeError("maximum context length exceeded by tokens")
        pipeline.checkpoint_after_stage_success("architecture", "done", state)

    monkeypatch.setattr(pipeline, "STAGES", ["idle", "scanning", "architecture", "done"])
    monkeypatch.setattr(
        pipeline,
        "STAGE_RUNNERS",
        {
            "scanning": run_scanning,
            "architecture": run_architecture,
        },
    )
    monkeypatch.setitem(pipeline.STAGE_LABELS, "scanning", "Repository Scan")
    monkeypatch.setitem(pipeline.STAGE_LABELS, "architecture", "Architecture Generation")
    monkeypatch.setitem(pipeline.STAGE_LABELS, "done", "Done")

    return call_counts


def test_recoverable_token_failure_moves_pipeline_to_paused_with_checkpoint(monkeypatch, tmp_path):
    state_file = _configure_runtime_paths(monkeypatch, tmp_path)
    call_counts = _install_recovery_test_graph(monkeypatch)

    pipeline.run_pipeline()

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert call_counts["architecture"] == 1
    assert persisted["current_stage"] == "paused"
    assert persisted["pause_reason"] == "token_exhaustion"
    assert persisted["last_safe_stage"] == "scanning"
    assert persisted["last_attempted_stage"] == "architecture"
    assert persisted["resume_hint"] == "Run: ain run --resume architecture"


def test_continue_recovers_from_paused_checkpoint_and_completes(monkeypatch, tmp_path):
    state_file = _configure_runtime_paths(monkeypatch, tmp_path)
    call_counts = _install_recovery_test_graph(monkeypatch)

    pipeline.run_pipeline()
    monkeypatch.setattr(sys, "argv", ["ain", "continue"])

    pipeline.main()

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert call_counts["architecture"] == 2
    assert persisted["current_stage"] == "done"
    assert persisted["last_safe_stage"] == "architecture"
    assert persisted["last_attempted_stage"] == "architecture"


def test_status_cli_repairs_corrupted_state_and_reports_error(monkeypatch, tmp_path):
    corrupted_payload = {
        "version": STATE_SCHEMA_VERSION,
        "current_stage": 123,  # invalid type to trigger schema failure
        "status": "idle",
        "created_at": "",
        "updated_at": "",
    }

    pipeline_dir = tmp_path / ".ai-pipeline"
    pipeline_dir.mkdir()
    state_path = pipeline_dir / "state.json"
    state_path.write_text(json.dumps(corrupted_payload), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(state_service, "_now_iso", lambda: "2026-03-09T12:00:00+00:00")

    healthy_summary = HealthSummary(
        external_binaries={},
        config_files={},
        state_files={"state_json": {"name": "state.json", "status": "ok", "message": "repaired", "details": {}}},
        overall_status="healthy",
    )
    monkeypatch.setattr(
        "ain.services.config_service.get_health_summary",
        lambda project_root=None: healthy_summary,
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["status", "--json"])

    assert result.exit_code == 0

    repaired = json.loads(state_path.read_text(encoding="utf-8"))
    assert repaired["version"] == STATE_SCHEMA_VERSION
    assert repaired["last_error"]["code"] == "STATE_CORRUPT"
    backup_path = Path(repaired["last_error"]["details"]["backup_path"])
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == json.dumps(corrupted_payload)
    assert repaired["created_at"] == "2026-03-09T12:00:00+00:00"

    payload = json.loads(result.output)
    assert payload["pipeline_state"]["last_error"]["code"] == "STATE_CORRUPT"
