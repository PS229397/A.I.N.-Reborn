from __future__ import annotations

import subprocess
from types import SimpleNamespace

from ain.services import agent_gateway


def test_run_agent_retries_on_non_zero_exit(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("hello", encoding="utf-8")

    attempts = {"count": 0}

    def fake_config(_project_root=None):
        return {
            "runtime": {
                "retries": {
                    "max_attempts": 3,
                    "backoff_seconds": 0,
                    "backoff_multiplier": 1,
                    "jitter": False,
                    "fail_fast": {
                        "on_missing_binary": True,
                        "on_timeout": True,
                    },
                }
            }
        }

    def fake_run(command, **_kwargs):
        attempts["count"] += 1
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(agent_gateway.config_service, "get_effective_config", fake_config)
    monkeypatch.setattr(agent_gateway, "_sleep_backoff", lambda *_, **__: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent_gateway.run_agent(["codex"], prompt_path, timeout_s=1)

    assert attempts["count"] == 3
    assert result.exit_code == 1
    assert result.error is not None
    assert result.error["code"] == "AGENT_NON_ZERO_EXIT"


def test_run_agent_missing_binary_retries_when_fail_fast_disabled(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("hello", encoding="utf-8")

    attempts = {"count": 0}

    def fake_config(_project_root=None):
        return {
            "runtime": {
                "retries": {
                    "max_attempts": 2,
                    "backoff_seconds": 0,
                    "backoff_multiplier": 1,
                    "jitter": False,
                    "fail_fast": {"on_missing_binary": False, "on_timeout": True},
                }
            }
        }

    def fake_run(_command, **_kwargs):
        attempts["count"] += 1
        raise FileNotFoundError("binary not found")

    monkeypatch.setattr(agent_gateway.config_service, "get_effective_config", fake_config)
    monkeypatch.setattr(agent_gateway, "_sleep_backoff", lambda *_, **__: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent_gateway.run_agent(["missing-binary"], prompt_path, timeout_s=1)

    assert attempts["count"] == 2
    assert result.exit_code == 127
    assert result.error is not None
    assert result.error["code"] == "AGENT_MISSING"


def test_run_agent_retries_timeout_then_succeeds(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("hello", encoding="utf-8")

    attempts = {"count": 0}

    def fake_config(_project_root=None):
        return {
            "runtime": {
                "retries": {
                    "max_attempts": 2,
                    "backoff_seconds": 0,
                    "backoff_multiplier": 1,
                    "jitter": False,
                    "fail_fast": {"on_missing_binary": True, "on_timeout": False},
                }
            }
        }

    def fake_run(command, timeout, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise subprocess.TimeoutExpired(command, timeout, output="partial", stderr="slow")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(agent_gateway.config_service, "get_effective_config", fake_config)
    monkeypatch.setattr(agent_gateway, "_sleep_backoff", lambda *_, **__: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent_gateway.run_agent(["codex"], prompt_path, timeout_s=1)

    assert attempts["count"] == 2
    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert result.error is None
