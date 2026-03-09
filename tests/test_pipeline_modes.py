from __future__ import annotations

import json

import pytest

from ain import pipeline


def _configure_paths(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / ".ai-pipeline"
    docs_dir = tmp_path / "docs"

    monkeypatch.setattr(pipeline, "PIPELINE_DIR", pipeline_dir)
    monkeypatch.setattr(pipeline, "STATE_FILE", pipeline_dir / "state.json")
    monkeypatch.setattr(pipeline, "CONFIG_FILE", pipeline_dir / "config.json")
    monkeypatch.setattr(pipeline, "PROMPTS_DIR", pipeline_dir / "prompts")
    monkeypatch.setattr(pipeline, "LOGS_DIR", pipeline_dir / "logs")
    monkeypatch.setattr(pipeline, "APPROVALS_DIR", pipeline_dir / "approvals")
    monkeypatch.setattr(pipeline, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(pipeline, "TASKS_FILE", docs_dir / "TASKS.md")
    monkeypatch.setattr(pipeline, "TASK_GRAPH_FILE", docs_dir / "TASK_GRAPH.json")
    monkeypatch.setattr(pipeline, "PRD_FILE", docs_dir / "PRD.md")
    monkeypatch.setattr(pipeline, "DESIGN_FILE", docs_dir / "DESIGN.md")
    monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", docs_dir / "FEATURE_SPEC.md")
    monkeypatch.setattr(pipeline, "ARCHITECTURE_FILE", docs_dir / "architecture.md")
    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "error", lambda *_args, **_kwargs: None)

    pipeline_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    pipeline.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def test_load_state_backfills_selected_mode(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)

    config = pipeline.load_config()
    result = pipeline.load_state_with_backfill({"current_stage": "idle"}, config)

    assert result["selected_mode"] == "default"
    assert result["mode_changed_at"] is None


def test_load_config_backfills_pipeline_mode_and_agent_keys(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    pipeline.CONFIG_FILE.write_text(json.dumps({"agents": {"planning": {"command": "codex"}}}), encoding="utf-8")

    config = pipeline.load_config()

    assert config["pipeline_mode"]["default"] == "default"
    assert config["agents"]["planning_codex"]["command"] == "codex"
    assert config["agents"]["planning_chief"]["command"] == "chief"
    assert config["agents"]["task_creation_codex"]["command"] == "codex"
    assert config["agents"]["implementation_codex"]["command"] == "codex"


@pytest.mark.parametrize(
    ("mode", "planning", "task_creation", "implementation"),
    [
        ("default", "planning", "task_creation", "implementation"),
        ("codex_only", "planning_codex", "task_creation_codex", "implementation_codex"),
        ("claude_chief_only", "planning_chief", "task_creation", "implementation"),
    ],
)
def test_resolve_stage_agent_key_for_each_mode(
    monkeypatch, tmp_path, mode, planning, task_creation, implementation
):
    _configure_paths(monkeypatch, tmp_path)
    config = pipeline.load_config()
    state = pipeline.load_state_with_backfill({"selected_mode": mode}, config)

    assert pipeline.resolve_stage_agent_key("planning_generation", state, config) == planning
    assert pipeline.resolve_stage_agent_key("task_creation", state, config) == task_creation
    assert pipeline.resolve_stage_agent_key("implementation", state, config) == implementation


def test_set_pipeline_mode_persists_state_and_config(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    config = pipeline.load_config()
    state = pipeline.load_state(config)

    pipeline.set_pipeline_mode("codex_only", state, config)

    persisted_state = json.loads(pipeline.STATE_FILE.read_text(encoding="utf-8"))
    persisted_config = json.loads(pipeline.CONFIG_FILE.read_text(encoding="utf-8"))
    assert persisted_state["selected_mode"] == "codex_only"
    assert persisted_state["mode_changed_at"] is not None
    assert persisted_config["pipeline_mode"]["default"] == "codex_only"


def test_mode_switch_during_active_stage_is_persisted_for_next_stage(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    config = pipeline.load_config()
    state = pipeline.load_state_with_backfill({"current_stage": "implementation"}, config)

    pipeline.set_pipeline_mode("codex_only", state, config)

    persisted_state = json.loads(pipeline.STATE_FILE.read_text(encoding="utf-8"))
    assert persisted_state["selected_mode"] == "codex_only"
    assert persisted_state["current_stage"] == "implementation"


def test_prompt_for_pipeline_mode_accepts_numeric_choice(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    config = pipeline.load_config()
    state = pipeline.load_state(config)

    monkeypatch.setattr("builtins.input", lambda _prompt: "2")

    assert pipeline.prompt_for_pipeline_mode(state, config) == "codex_only"


def test_codex_only_task_creation_uses_codex_without_chief(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    config = pipeline.load_config()
    state = pipeline.load_state_with_backfill(
        {"current_stage": "task_creation", "selected_mode": "codex_only"},
        config,
    )

    (pipeline.PROMPTS_DIR / "task_creation_prompt.md").write_text("Create task artifacts", encoding="utf-8")
    pipeline.PRD_FILE.write_text("# Problem\n", encoding="utf-8")
    pipeline.DESIGN_FILE.write_text("# Architecture Changes\n", encoding="utf-8")
    pipeline.FEATURE_SPEC_FILE.write_text("# Feature Specification\n", encoding="utf-8")
    pipeline.ARCHITECTURE_FILE.write_text("# System Overview\n", encoding="utf-8")

    calls = []

    def fake_call_agent(agent_name, _prompt, _config):
        calls.append(agent_name)
        return (
            "<!-- FILE: docs/TASKS.md -->\n# Tasks\n\n- [ ] add mode coverage\n<!-- END: docs/TASKS.md -->\n"
            "<!-- FILE: docs/TASK_GRAPH.json -->\n"
            '{"tasks":[{"id":1,"description":"add mode coverage","depends_on":[],"status":"pending"}],'
            '"generated_at":"2026-03-09T00:00:00+00:00","total":1,"completed":0}\n'
            "<!-- END: docs/TASK_GRAPH.json -->"
        )

    monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)

    pipeline.run_task_creation(state, config)

    assert calls == ["task_creation_codex"]
    assert pipeline.TASKS_FILE.exists()
    assert pipeline.TASK_GRAPH_FILE.exists()


def test_claude_chief_only_implementation_does_not_use_codex_fallback(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    config = pipeline.load_config()
    state = pipeline.load_state_with_backfill(
        {"current_stage": "implementation", "selected_mode": "claude_chief_only", "branch": None},
        config,
    )

    (pipeline.PROMPTS_DIR / "implementation_prompt.md").write_text("Implement the task", encoding="utf-8")
    pipeline.TASKS_FILE.write_text("# Tasks\n\n- [ ] mode-safe implementation\n", encoding="utf-8")
    pipeline.TASK_GRAPH_FILE.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": 1,
                        "description": "mode-safe implementation",
                        "depends_on": [],
                        "status": "pending",
                        "completed_at": None,
                    }
                ],
                "generated_at": "2026-03-09T00:00:00+00:00",
                "total": 1,
                "completed": 0,
            }
        ),
        encoding="utf-8",
    )

    calls = []

    def fake_call_agent(agent_name, _prompt, _config):
        calls.append(agent_name)
        raise RuntimeError("claude failed")

    monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)
    monkeypatch.setattr(pipeline, "create_git_branch", lambda *_args, **_kwargs: None)

    pipeline.run_implementation(state, config)

    assert calls == ["implementation"]
    assert "implementation" not in state.get("fallback_mode", {})
