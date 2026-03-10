"""Minimal agent gateway compatibility layer used by legacy tests."""

from __future__ import annotations

import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ain.services import config_service


@dataclass
class AgentRunResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    error: dict[str, Any] | None = None


def _retry_settings() -> dict[str, Any]:
    runtime = config_service.get_effective_config().get("runtime", {})
    retries = runtime.get("retries", {})
    return {
        "max_attempts": int(retries.get("max_attempts", 1)),
        "backoff_seconds": float(retries.get("backoff_seconds", 0)),
        "backoff_multiplier": float(retries.get("backoff_multiplier", 1)),
        "jitter": bool(retries.get("jitter", False)),
        "fail_fast": retries.get(
            "fail_fast",
            {"on_missing_binary": True, "on_timeout": True},
        ),
    }


def _sleep_backoff(attempt: int, settings: dict[str, Any]) -> None:
    delay = settings["backoff_seconds"] * (settings["backoff_multiplier"] ** max(0, attempt - 1))
    if settings["jitter"]:
        delay *= random.uniform(0.8, 1.2)
    if delay > 0:
        time.sleep(delay)


def run_agent(command: list[str], prompt_path: Path, *, timeout_s: int = 600) -> AgentRunResult:
    settings = _retry_settings()
    max_attempts = max(1, settings["max_attempts"])

    prompt = prompt_path.read_text(encoding="utf-8")
    last_result: AgentRunResult | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
            )
            if completed.returncode == 0:
                return AgentRunResult(
                    exit_code=0,
                    stdout=completed.stdout or "",
                    stderr=completed.stderr or "",
                )

            last_result = AgentRunResult(
                exit_code=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                error={
                    "code": "AGENT_NON_ZERO_EXIT",
                    "message": "Agent exited with a non-zero status.",
                    "details": {"attempt": attempt},
                },
            )
        except FileNotFoundError as exc:
            last_result = AgentRunResult(
                exit_code=127,
                error={
                    "code": "AGENT_MISSING",
                    "message": str(exc),
                    "details": {"attempt": attempt},
                },
            )
            if settings["fail_fast"].get("on_missing_binary", True):
                return last_result
        except subprocess.TimeoutExpired as exc:
            last_result = AgentRunResult(
                exit_code=124,
                stdout=(exc.output or "") if isinstance(exc.output, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                error={
                    "code": "AGENT_TIMEOUT",
                    "message": f"Agent timed out after {timeout_s}s.",
                    "details": {"attempt": attempt},
                },
            )
            if settings["fail_fast"].get("on_timeout", True):
                return last_result

        if attempt < max_attempts:
            _sleep_backoff(attempt, settings)

    assert last_result is not None
    return last_result
