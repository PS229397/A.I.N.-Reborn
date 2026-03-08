"""End-to-end tests for ain/pipeline.py.

These tests exercise the full pipeline flow using mocks for external agents.
"""
from __future__ import annotations

import json
import sys
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
        "current_stage": "idle",
        "branch": None,
        "started_at": None,
        "last_updated": None,
        "completed_stages": [],
        "fallback":   dict(pipeline._DEFAULT_FALLBACK),
        "prd_import": dict(pipeline._DEFAULT_PRD_IMPORT),
    }
    state.update(overrides)
    return state


def _make_task_graph(n: int = 2, with_parallel: bool = True) -> dict:
    tasks = [
        {"id": f"T-{i}", "description": f"Task {i}", "depends_on": [],
         "status": "pending", "files_affected": [], "completed_at": None}
        for i in range(1, n + 1)
    ]
    groups = []
    if with_parallel:
        groups = [{"group_id": f"group-{i}", "can_run_parallel": False,
                   "tasks": [f"T-{i}"], "depends_on": []} for i in range(1, n + 1)]
    return {"tasks": tasks, "parallel_groups": groups,
            "generated_at": "2026-01-01T00:00:00Z", "total": n, "completed": 0}


# ---------------------------------------------------------------------------
# T-049 — test_normal_path_with_agent_teams_and_verification
# ---------------------------------------------------------------------------

class TestNormalPathWithVerification:
    def test_full_run_with_verification(self, tmp_path, monkeypatch):
        """Implementation → Verification (VERIFIED) → Validation passes."""
        monkeypatch.setattr(pipeline, "REPO_ROOT",        tmp_path)
        monkeypatch.setattr(pipeline, "DOCS_DIR",         tmp_path / "docs")
        monkeypatch.setattr(pipeline, "PROMPTS_DIR",      tmp_path / ".ai-pipeline" / "prompts")
        monkeypatch.setattr(pipeline, "LOGS_DIR",         tmp_path / ".ai-pipeline" / "logs")
        monkeypatch.setattr(pipeline, "APPROVALS_DIR",    tmp_path / ".ai-pipeline" / "approvals")
        monkeypatch.setattr(pipeline, "STATE_DIR",        tmp_path / ".ai-pipeline" / "state")
        monkeypatch.setattr(pipeline, "TASK_GRAPH_FILE",  tmp_path / "docs" / "TASK_GRAPH.json")
        monkeypatch.setattr(pipeline, "TASKS_FILE",       tmp_path / "docs" / "TASKS.md")
        monkeypatch.setattr(pipeline, "IMPLEMENTATION_LOG_FILE", tmp_path / "docs" / "IMPL.md")
        monkeypatch.setattr(pipeline, "VERIFICATION_REPORT_FILE", tmp_path / "docs" / "VER.md")
        monkeypatch.setattr(pipeline, "VERIFICATION_FLAG", tmp_path / ".ai-pipeline" / "approvals" / "ver.flag")
        monkeypatch.setattr(pipeline, "ARCHITECTURE_FILE", tmp_path / "docs" / "arch.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE",       tmp_path / "docs" / "DESIGN.md")

        for d in [tmp_path / "docs", tmp_path / ".ai-pipeline" / "prompts",
                  tmp_path / ".ai-pipeline" / "approvals", tmp_path / ".ai-pipeline" / "logs"]:
            d.mkdir(parents=True, exist_ok=True)

        (tmp_path / ".ai-pipeline" / "prompts" / "implementation_prompt.md").write_text("impl", encoding="utf-8")
        (tmp_path / ".ai-pipeline" / "prompts" / "verification_prompt.md").write_text("verify", encoding="utf-8")
        (tmp_path / "docs" / "TASKS.md").write_text("- [ ] Task 1\n", encoding="utf-8")

        task_graph = _make_task_graph(1, with_parallel=False)
        (tmp_path / "docs" / "TASK_GRAPH.json").write_text(json.dumps(task_graph), encoding="utf-8")

        config = {
            "agents": {"implementation": {"command": "echo", "args": [], "model": None, "prompt_mode": "stdin"}},
            "agent_teams": {"enabled": True, "require_verification": True,
                            "max_teammates": 1, "lead_model": None, "teammate_model": None},
            "fallback": {"enabled": False, "trigger_on": "token_limit",
                         "notification_timeout_secs": 5, "fallback_agent": "codex",
                         "fallback_prompt_mode": "full_auto",
                         "protected_paths": ["docs/"], "stages_with_fallback": [],
                         "codex_timeout_secs": 60, "on_codex_limit": "pause"},
            "validation": {"auto_detect": False, "commands": []},
            "git": {"auto_branch": False, "auto_commit": False, "branch_prefix": "ai/feature"},
            "scan": {"ignore_dirs": [], "key_files": []},
            "prd_import": {"min_prd_chars": 500, "allowed_extensions": [".md"]},
        }

        # When verification agent runs, write the flag
        def fake_call_agent(agent_name, prompt, cfg):
            pipeline.VERIFICATION_FLAG.parent.mkdir(parents=True, exist_ok=True)
            pipeline.VERIFICATION_FLAG.write_text("status=verified\n", encoding="utf-8")
            pipeline.VERIFICATION_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            pipeline.VERIFICATION_REPORT_FILE.write_text("# Verification Report\nOverall verdict: **VERIFIED**\n",
                                                          encoding="utf-8")
            return ""

        monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)
        monkeypatch.setattr(pipeline, "build_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "set_stage", lambda stage, state=None: _base_state(current_stage=stage))
        monkeypatch.setattr(pipeline, "_mark_task_complete_in_md", lambda d: None)
        monkeypatch.setattr(pipeline, "create_git_branch", lambda s, c: None)
        monkeypatch.setattr(pipeline, "read_context_files", lambda *a: "")

        state = _base_state(current_stage="implementation")

        # Run implementation
        pipeline.run_implementation(state, config)

        # run_verification should pass (flag was written by fake agent)
        pipeline.run_verification(state, config)  # should not raise

        assert pipeline.VERIFICATION_REPORT_FILE.exists()


# ---------------------------------------------------------------------------
# T-050 — test_simulated_token_limit_in_implementation
# ---------------------------------------------------------------------------

class TestSimulatedTokenLimit:
    def test_token_limit_triggers_fallback_flow(self, tmp_path, monkeypatch):
        """Token limit error → fallback triggered → auto_switch → rollback → codex launched."""
        monkeypatch.setattr(pipeline, "REPO_ROOT",    tmp_path)
        monkeypatch.setattr(pipeline, "PROMPTS_DIR",  tmp_path / "prompts")
        monkeypatch.setattr(pipeline, "LOGS_DIR",     tmp_path / "logs")
        monkeypatch.setattr(pipeline, "STATE_DIR",    tmp_path / "state")

        (tmp_path / "prompts").mkdir(parents=True)
        (tmp_path / "prompts" / "fallback_implementation_prompt.md").write_text(
            "Fallback prompt.", encoding="utf-8"
        )
        (tmp_path / "logs").mkdir(parents=True)
        (tmp_path / "state").mkdir(parents=True)

        config = {
            "agents": {"implementation": {"command": "claude", "args": [], "model": None}},
            "fallback": {
                "enabled": True,
                "trigger_on": "token_limit",
                "notification_timeout_secs": 1,
                "fallback_agent": "codex",
                "fallback_prompt_mode": "full_auto",
                "protected_paths": ["docs/"],
                "stages_with_fallback": ["implementation"],
                "codex_timeout_secs": 10,
                "on_codex_limit": "pause",
            },
        }
        state = _base_state(current_stage="implementation")
        state["fallback"]["rollback_commit"] = "abc123"

        def fake_call_agent(name, prompt, cfg):
            raise RuntimeError("Claude usage limit reached. Your limit will reset at 5:00 PM.")

        monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)

        rollback_called: list = []

        def fake_rollback(st):
            rollback_called.append(True)
            return ["ain/pipeline.py"]

        monkeypatch.setattr(pipeline, "rollback_implementation_files", fake_rollback)
        monkeypatch.setattr(pipeline, "capture_rollback_point", lambda s: "abc123")

        def fake_notify(context, timeout_secs):
            return "auto_switch"

        monkeypatch.setattr(pipeline, "notify_fallback_and_get_decision", fake_notify)

        codex_calls: list = []

        def fake_codex(stage, prompt_path, timeout):
            codex_calls.append(stage)
            # Write completion flag
            pipeline.STATE_DIR.mkdir(parents=True, exist_ok=True)
            (pipeline.STATE_DIR / "fallback_complete.flag").write_text(
                f"stage={stage}\n", encoding="utf-8"
            )
            return True

        monkeypatch.setattr(pipeline, "invoke_codex_fallback", fake_codex)
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "_log", lambda m: None)

        result = pipeline._call_agent_with_fallback(
            "implementation", "do tasks", config, state, "implementation"
        )

        assert rollback_called, "rollback should have been called"
        assert codex_calls == ["implementation"], "codex should have been launched"
        assert (tmp_path / "state" / "fallback_complete.flag").exists()


# ---------------------------------------------------------------------------
# T-051 — test_rollback_failure_halts_safely
# ---------------------------------------------------------------------------

class TestRollbackFailureHaltsSafely:
    def test_rollback_error_raises_without_launching_codex(self, tmp_path, monkeypatch):
        """If rollback_implementation_files raises, pipeline halts and Codex is NOT launched."""
        config = {
            "fallback": {
                "enabled": True,
                "trigger_on": "token_limit",
                "notification_timeout_secs": 1,
                "fallback_agent": "codex",
                "fallback_prompt_mode": "full_auto",
                "protected_paths": [],
                "stages_with_fallback": ["implementation"],
                "codex_timeout_secs": 10,
                "on_codex_limit": "pause",
            },
        }
        state = _base_state(current_stage="implementation")
        state["fallback"]["rollback_commit"] = "abc123"

        monkeypatch.setattr(pipeline, "call_agent",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("Claude usage limit reached.")))
        monkeypatch.setattr(pipeline, "capture_rollback_point", lambda s: "abc123")
        monkeypatch.setattr(pipeline, "notify_fallback_and_get_decision",
                            lambda context, timeout_secs: "switch")

        def bad_rollback(s):
            raise RuntimeError("git checkout failed: repo is locked")

        monkeypatch.setattr(pipeline, "rollback_implementation_files", bad_rollback)
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "_log", lambda m: None)

        codex_launched = []
        monkeypatch.setattr(pipeline, "invoke_codex_fallback",
                            lambda *a, **kw: codex_launched.append(True) or True)

        with pytest.raises(RuntimeError):
            pipeline._call_agent_with_fallback(
                "implementation", "do tasks", config, state, "implementation"
            )

        assert not codex_launched, "Codex should NOT have been launched when rollback failed"


# ---------------------------------------------------------------------------
# T-052 — test_on_codex_limit_pause_preserves_resumability
# ---------------------------------------------------------------------------

class TestOnCodexLimitPause:
    def test_codex_failure_with_pause_exits_without_corrupting_state(self, tmp_path, monkeypatch):
        """When codex fallback fails and on_codex_limit=pause, pipeline exits (sys.exit) cleanly."""
        config = {
            "fallback": {
                "enabled": True,
                "trigger_on": "token_limit",
                "notification_timeout_secs": 1,
                "fallback_agent": "codex",
                "fallback_prompt_mode": "full_auto",
                "protected_paths": [],
                "stages_with_fallback": ["implementation"],
                "codex_timeout_secs": 10,
                "on_codex_limit": "pause",
            },
        }
        state = _base_state(current_stage="implementation")
        state["fallback"]["rollback_commit"] = "abc123"

        monkeypatch.setattr(pipeline, "call_agent",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("Claude usage limit reached.")))
        monkeypatch.setattr(pipeline, "capture_rollback_point", lambda s: "abc123")
        monkeypatch.setattr(pipeline, "notify_fallback_and_get_decision",
                            lambda context, timeout_secs: "auto_switch")
        monkeypatch.setattr(pipeline, "rollback_implementation_files", lambda s: [])
        monkeypatch.setattr(pipeline, "invoke_codex_fallback", lambda *a, **kw: False)
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        monkeypatch.setattr(pipeline, "_log", lambda m: None)

        with pytest.raises(SystemExit) as exc_info:
            pipeline._call_agent_with_fallback(
                "implementation", "do tasks", config, state, "implementation"
            )

        assert exc_info.value.code == 0, "Pipeline should exit 0 (clean pause) on codex limit+pause"
