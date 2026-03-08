"""Integration tests for ain/pipeline.py."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import ain.pipeline as pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_state(**overrides) -> dict:
    state = {
        "current_stage": "implementation",
        "branch": None,
        "started_at": None,
        "last_updated": None,
        "completed_stages": [],
        "fallback":   dict(pipeline._DEFAULT_FALLBACK),
        "prd_import": dict(pipeline._DEFAULT_PRD_IMPORT),
    }
    state.update(overrides)
    return state


def _minimal_config(**overrides) -> dict:
    cfg = {
        "agents": {
            "implementation": {
                "command": "echo",
                "args": [],
                "model": None,
                "prompt_mode": "stdin",
            }
        },
        "agent_teams": {
            "enabled": False,
            "max_teammates": 2,
            "lead_model": None,
            "teammate_model": None,
            "require_verification": True,
        },
        "fallback": {
            "enabled": False,
            "trigger_on": "token_limit",
            "notification_timeout_secs": 5,
            "fallback_agent": "codex",
            "fallback_prompt_mode": "full_auto",
            "protected_paths": ["docs/", ".ai-pipeline/", ".git/", "CLAUDE.md", ".claude/"],
            "stages_with_fallback": ["implementation"],
            "codex_timeout_secs": 60,
            "on_codex_limit": "pause",
        },
        "validation": {"auto_detect": False, "commands": []},
        "git": {"auto_branch": False, "auto_commit": False, "branch_prefix": "ai/feature"},
        "scan": {"ignore_dirs": [], "key_files": []},
        "prd_import": {"min_prd_chars": 500, "allowed_extensions": [".md", ".txt"]},
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# T-044 — test_parallel_group_scheduling_honors_depends_on
# ---------------------------------------------------------------------------

class TestParallelGroupScheduling:
    def test_groups_execute_in_dependency_order(self, tmp_path, monkeypatch):
        """Groups with depends_on must execute after the groups they depend on."""
        monkeypatch.setattr(pipeline, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "STATE_DIR",   tmp_path / ".ai-pipeline" / "state")
        monkeypatch.setattr(pipeline, "DOCS_DIR",    tmp_path / "docs")
        monkeypatch.setattr(pipeline, "LOGS_DIR",    tmp_path / ".ai-pipeline" / "logs")
        monkeypatch.setattr(pipeline, "PROMPTS_DIR", tmp_path / ".ai-pipeline" / "prompts")
        monkeypatch.setattr(pipeline, "TASK_GRAPH_FILE",         tmp_path / "docs" / "TASK_GRAPH.json")
        monkeypatch.setattr(pipeline, "TASKS_FILE",              tmp_path / "docs" / "TASKS.md")
        monkeypatch.setattr(pipeline, "IMPLEMENTATION_LOG_FILE", tmp_path / "docs" / "IMPLEMENTATION_LOG.md")
        monkeypatch.setattr(pipeline, "ARCHITECTURE_FILE",       tmp_path / "docs" / "architecture.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE",             tmp_path / "docs" / "DESIGN.md")

        (tmp_path / ".ai-pipeline" / "prompts").mkdir(parents=True)
        (tmp_path / ".ai-pipeline" / "prompts" / "implementation_prompt.md").write_text(
            "Implement the task.", encoding="utf-8"
        )
        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / "docs" / "TASKS.md").write_text(
            "- [ ] Task 1\n- [ ] Task 2\n", encoding="utf-8"
        )

        task_graph = {
            "tasks": [
                {"id": "T-1", "description": "Task 1", "depends_on": [], "status": "pending",
                 "files_affected": [], "completed_at": None},
                {"id": "T-2", "description": "Task 2", "depends_on": ["T-1"], "status": "pending",
                 "files_affected": [], "completed_at": None},
            ],
            "parallel_groups": [
                {"group_id": "group-1", "can_run_parallel": False, "tasks": ["T-1"], "depends_on": []},
                {"group_id": "group-2", "can_run_parallel": False, "tasks": ["T-2"], "depends_on": ["group-1"]},
            ],
            "generated_at": "2026-01-01T00:00:00Z",
            "total": 2,
            "completed": 0,
        }

        execution_order: list[str] = []

        def fake_call_agent(agent_name, prompt, config):
            if "Task 1" in prompt:
                execution_order.append("T-1")
            elif "Task 2" in prompt:
                execution_order.append("T-2")
            return ""

        monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "_mark_task_complete_in_md", lambda d: None)
        monkeypatch.setattr(pipeline, "read_context_files", lambda *a: "")

        state  = _base_state()
        config = _minimal_config()

        result = pipeline.run_parallel_groups(task_graph, config, state)

        assert result.success
        assert execution_order == ["T-1", "T-2"], f"Expected T-1 before T-2, got {execution_order}"


# ---------------------------------------------------------------------------
# T-045 — test_verification_gate_blocks_validation_on_failure
# ---------------------------------------------------------------------------

class TestVerificationGate:
    def test_missing_flag_halts_before_validation(self, tmp_path, monkeypatch):
        """If run_verification_stage returns False, run_verification raises RuntimeError."""
        monkeypatch.setattr(pipeline, "PROMPTS_DIR",        tmp_path / "prompts")
        monkeypatch.setattr(pipeline, "APPROVALS_DIR",      tmp_path / "approvals")
        monkeypatch.setattr(pipeline, "VERIFICATION_FLAG",  tmp_path / "approvals" / "verification.flag")
        monkeypatch.setattr(pipeline, "VERIFICATION_REPORT_FILE", tmp_path / "docs" / "VERIFICATION_REPORT.md")
        monkeypatch.setattr(pipeline, "DOCS_DIR",           tmp_path / "docs")
        monkeypatch.setattr(pipeline, "LOGS_DIR",           tmp_path / "logs")

        (tmp_path / "prompts").mkdir(parents=True)
        (tmp_path / "prompts" / "verification_prompt.md").write_text("Audit tasks.", encoding="utf-8")
        (tmp_path / "approvals").mkdir(parents=True)
        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / "logs").mkdir(parents=True)

        monkeypatch.setattr(pipeline, "call_agent", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline, "build_prompt", lambda *a, **kw: "prompt text")
        monkeypatch.setattr(pipeline, "set_stage", lambda stage, state=None: _base_state())
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)

        config = _minimal_config()
        config["agent_teams"]["require_verification"] = True
        state = _base_state()

        with pytest.raises(RuntimeError, match="Verification failed"):
            pipeline.run_verification(state, config)


# ---------------------------------------------------------------------------
# T-046 — test_prd_import_file_mode_creates_three_docs
# ---------------------------------------------------------------------------

class TestPrdImportFileMode:
    def test_file_import_creates_three_docs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "REPO_ROOT",        tmp_path)
        monkeypatch.setattr(pipeline, "DOCS_DIR",         tmp_path / "docs")
        monkeypatch.setattr(pipeline, "STATE_DIR",        tmp_path / ".ai-pipeline" / "state")
        monkeypatch.setattr(pipeline, "PROMPTS_DIR",      tmp_path / ".ai-pipeline" / "prompts")
        monkeypatch.setattr(pipeline, "PRD_FILE",         tmp_path / "docs" / "PRD.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE",      tmp_path / "docs" / "DESIGN.md")
        monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE",tmp_path / "docs" / "FEATURE_SPEC.md")
        monkeypatch.setattr(pipeline, "LOGS_DIR",         tmp_path / ".ai-pipeline" / "logs")

        (tmp_path / ".ai-pipeline" / "prompts").mkdir(parents=True)
        (tmp_path / "docs").mkdir(parents=True)

        prd_file = tmp_path / "my_prd.md"
        prd_file.write_text("# Problem\n\nThis is a simple PRD without design markers.\n" + "x" * 600,
                             encoding="utf-8")

        saved_states: list = []
        monkeypatch.setattr(pipeline, "save_state", lambda s: saved_states.append(s))
        monkeypatch.setattr(pipeline, "set_stage", lambda stage, state=None: None)
        monkeypatch.setattr(pipeline, "load_config", lambda: {
            "prd_import": {"min_prd_chars": 500, "allowed_extensions": [".md", ".txt"]},
            "agents": {"implementation": {"command": "echo", "args": [], "model": None}},
        })

        state = _base_state()
        pipeline.handle_prd_import(prd_file, state)

        assert (tmp_path / "docs" / "PRD.md").exists()
        assert (tmp_path / "docs" / "DESIGN.md").exists()
        assert (tmp_path / "docs" / "FEATURE_SPEC.md").exists()

        assert any(s.get("prd_import", {}).get("enabled") for s in saved_states)
        assert any("PRD.md" in str(s.get("prd_import", {}).get("files_written", []))
                   or any("PRD" in f for f in s.get("prd_import", {}).get("files_written", []))
                   for s in saved_states)

        skipped = next(
            (s["prd_import"]["skipped_stages"] for s in saved_states
             if s.get("prd_import", {}).get("skipped_stages")),
            [],
        )
        for expected_skip in ["user_context", "planning_questions", "planning_generation"]:
            assert expected_skip in skipped


# ---------------------------------------------------------------------------
# T-047 — test_prd_import_directory_mode_resolves_filenames
# ---------------------------------------------------------------------------

class TestPrdImportDirectoryMode:
    def test_directory_resolves_prd_design_spec(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "REPO_ROOT",         tmp_path)
        monkeypatch.setattr(pipeline, "DOCS_DIR",          tmp_path / "docs")
        monkeypatch.setattr(pipeline, "STATE_DIR",         tmp_path / ".ai-pipeline" / "state")
        monkeypatch.setattr(pipeline, "PROMPTS_DIR",       tmp_path / ".ai-pipeline" / "prompts")
        monkeypatch.setattr(pipeline, "PRD_FILE",          tmp_path / "docs" / "PRD.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE",       tmp_path / "docs" / "DESIGN.md")
        monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", tmp_path / "docs" / "FEATURE_SPEC.md")
        monkeypatch.setattr(pipeline, "LOGS_DIR",          tmp_path / ".ai-pipeline" / "logs")

        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / ".ai-pipeline" / "state").mkdir(parents=True)

        import_dir = tmp_path / "my_docs"
        import_dir.mkdir()
        (import_dir / "product_requirements.md").write_text("# PRD content " + "x" * 500, encoding="utf-8")
        (import_dir / "architecture_design.md").write_text("# Architecture Changes\n\ncontent", encoding="utf-8")
        (import_dir / "feature_spec.md").write_text("# Feature Specification\n\ncontent", encoding="utf-8")

        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "set_stage", lambda stage, state=None: None)
        monkeypatch.setattr(pipeline, "load_config", lambda: {
            "prd_import": {"min_prd_chars": 500, "allowed_extensions": [".md", ".txt"]},
            "agents": {},
        })

        state = _base_state()
        pipeline.handle_prd_import(import_dir, state)

        assert (tmp_path / "docs" / "PRD.md").exists()
        assert "PRD content" in (tmp_path / "docs" / "PRD.md").read_text()
        assert (tmp_path / "docs" / "DESIGN.md").exists()
        assert (tmp_path / "docs" / "FEATURE_SPEC.md").exists()


# ---------------------------------------------------------------------------
# T-048 — test_resume_after_wait_returns_to_correct_stage
# ---------------------------------------------------------------------------

class TestResumeAfterWait:
    def test_resume_implementation_sets_stage_and_runs(self, tmp_path, monkeypatch):
        """ain run --resume implementation should re-enter at implementation stage."""
        monkeypatch.setattr(pipeline, "REPO_ROOT",    tmp_path)
        monkeypatch.setattr(pipeline, "PIPELINE_DIR", tmp_path / ".ai-pipeline")
        monkeypatch.setattr(pipeline, "STATE_FILE",   tmp_path / ".ai-pipeline" / "state.json")
        monkeypatch.setattr(pipeline, "CONFIG_FILE",  tmp_path / ".ai-pipeline" / "config.json")
        monkeypatch.setattr(pipeline, "LOGS_DIR",     tmp_path / ".ai-pipeline" / "logs")

        (tmp_path / ".ai-pipeline").mkdir(parents=True)

        state = _base_state(current_stage="failed")
        (tmp_path / ".ai-pipeline" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (tmp_path / ".ai-pipeline" / "config.json").write_text(json.dumps(_minimal_config()), encoding="utf-8")

        stages_run: list[str] = []

        def fake_runner(st, cfg):
            stages_run.append(st.get("current_stage", "?"))

        monkeypatch.setattr(pipeline, "STAGE_RUNNERS", {"implementation": fake_runner})
        monkeypatch.setattr(pipeline, "STAGES", ["idle", "implementation", "done"])
        monkeypatch.setattr(pipeline, "ensure_config", lambda: None)
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)

        def fake_load_state():
            return _base_state(current_stage="implementation")

        monkeypatch.setattr(pipeline, "load_state", fake_load_state)
        monkeypatch.setattr(pipeline, "load_config", lambda: _minimal_config())

        pipeline.run_pipeline(start_stage="implementation")
        # Should have entered the implementation runner
        assert "implementation" in stages_run or len(stages_run) > 0
