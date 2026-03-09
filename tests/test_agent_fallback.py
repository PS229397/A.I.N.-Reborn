from __future__ import annotations

import json

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
    monkeypatch.setattr(pipeline, "TASKS_FILE", docs_dir / "TASKS.md")
    monkeypatch.setattr(pipeline, "TASK_GRAPH_FILE", docs_dir / "TASK_GRAPH.json")
    monkeypatch.setattr(pipeline, "PRD_FILE", docs_dir / "PRD.md")
    monkeypatch.setattr(pipeline, "DESIGN_FILE", docs_dir / "DESIGN.md")
    monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", docs_dir / "FEATURE_SPEC.md")
    monkeypatch.setattr(pipeline, "ARCHITECTURE_FILE", docs_dir / "architecture.md")
    monkeypatch.setattr(pipeline, "IMPLEMENTATION_LOG_FILE", docs_dir / "IMPLEMENTATION_LOG.md")

    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "error", lambda *_args, **_kwargs: None)


def test_task_creation_fallback_to_codex_persists_fallback_mode(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    pipeline.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    pipeline.DOCS_DIR.mkdir(parents=True, exist_ok=True)

    (pipeline.PROMPTS_DIR / "task_creation_prompt.md").write_text(
        "Create docs/TASKS.md and docs/TASK_GRAPH.json", encoding="utf-8"
    )
    pipeline.PRD_FILE.write_text("# Problem\n", encoding="utf-8")
    pipeline.DESIGN_FILE.write_text("# Architecture Changes\n", encoding="utf-8")
    pipeline.FEATURE_SPEC_FILE.write_text("# Feature Specification\n", encoding="utf-8")
    pipeline.ARCHITECTURE_FILE.write_text("# System Overview\n", encoding="utf-8")

    config = {
        "agents": {
            "task_creation": {
                "command": "claude",
                "args": [],
                "prompt_mode": "stdin",
            }
        }
    }
    state = {
        "current_stage": "task_creation",
        "completed_stages": [],
        "started_at": None,
        "last_updated": None,
        "fallback_mode": {},
    }

    calls: list[str] = []

    def fake_call_agent(_agent_name, _prompt, current_config):
        calls.append(current_config["agents"]["task_creation"]["command"])
        if len(calls) == 1:
            raise RuntimeError("maximum context length exceeded by tokens")
        return (
            "<!-- FILE: docs/TASKS.md -->\n"
            "# Tasks\n\n"
            "- [ ] add fallback coverage\n"
            "<!-- END: docs/TASKS.md -->\n\n"
            "<!-- FILE: docs/TASK_GRAPH.json -->\n"
            "{\n"
            '  "tasks": [{"id": 1, "description": "add fallback coverage", "depends_on": [], "status": "pending"}],\n'
            '  "generated_at": "2026-03-06T00:00:00+00:00",\n'
            '  "total": 1,\n'
            '  "completed": 0\n'
            "}\n"
            "<!-- END: docs/TASK_GRAPH.json -->"
        )

    monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)

    pipeline.run_task_creation(state, config)

    assert calls == ["claude", "codex"]
    assert state["fallback_mode"]["task_creation"] == "codex"
    assert config["agents"]["task_creation"]["command"] == "codex"
    assert config["agents"]["task_creation"]["args"] == ["exec"]
    assert config["agents"]["task_creation"]["prompt_mode"] == "stdin"

    persisted = json.loads(pipeline.STATE_FILE.read_text(encoding="utf-8"))
    assert persisted["fallback_mode"]["task_creation"] == "codex"
    assert persisted["current_stage"] == "waiting_approval"


def test_implementation_fallback_to_codex_persists_fallback_mode(monkeypatch, tmp_path):
    _configure_runtime_paths(monkeypatch, tmp_path)
    pipeline.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    pipeline.DOCS_DIR.mkdir(parents=True, exist_ok=True)

    (pipeline.PROMPTS_DIR / "implementation_prompt.md").write_text(
        "Implement the task", encoding="utf-8"
    )
    pipeline.TASKS_FILE.write_text(
        "# Tasks\n\n- [ ] add fallback coverage\n",
        encoding="utf-8",
    )
    pipeline.TASK_GRAPH_FILE.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": 1,
                        "description": "add fallback coverage",
                        "depends_on": [],
                        "status": "pending",
                        "completed_at": None,
                    }
                ],
                "generated_at": "2026-03-06T00:00:00+00:00",
                "total": 1,
                "completed": 0,
            }
        ),
        encoding="utf-8",
    )

    config = {
        "agents": {
            "implementation": {
                "command": "chief",
                "args": [],
                "prompt_mode": "stdin",
            }
        },
        "git": {"auto_branch": False, "auto_commit": False},
    }
    state = {
        "current_stage": "implementation",
        "completed_stages": [],
        "started_at": None,
        "last_updated": None,
        "fallback_mode": {},
        "branch": None,
    }

    calls: list[str] = []

    def fake_call_agent(_agent_name, _prompt, current_config):
        calls.append(current_config["agents"]["implementation"]["command"])
        if len(calls) == 1:
            raise RuntimeError("agent timed out and returned no response")
        return "ok"

    monkeypatch.setattr(pipeline, "call_agent", fake_call_agent)
    monkeypatch.setattr(pipeline, "create_git_branch", lambda *_args, **_kwargs: None)

    pipeline.run_implementation(state, config)

    assert calls == ["chief", "codex"]
    assert state["fallback_mode"]["implementation"] == "codex"
    assert config["agents"]["implementation"]["command"] == "codex"
    assert config["agents"]["implementation"]["args"] == ["exec"]
    assert config["agents"]["implementation"]["prompt_mode"] == "stdin"

    persisted = json.loads(pipeline.STATE_FILE.read_text(encoding="utf-8"))
    assert persisted["fallback_mode"]["implementation"] == "codex"
    assert persisted["current_stage"] == "validation"

    graph = json.loads(pipeline.TASK_GRAPH_FILE.read_text(encoding="utf-8"))
    assert graph["tasks"][0]["status"] == "completed"
    assert graph["completed"] == 1
    assert "- [x] add fallback coverage" in pipeline.TASKS_FILE.read_text(encoding="utf-8")
