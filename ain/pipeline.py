#!/usr/bin/env python3
"""
A.I.N. Pipeline
===============
Multi-agent orchestrator for structured AI-assisted development.

Workflow:
    idle -> scanning -> architecture -> planning_questions -> planning_generation
    -> task_creation -> waiting_approval -> implementation -> validation -> done

Usage (installed):
    ain init                  Scaffold .ai-pipeline/ into current repo
    ain run                   Run pipeline from current stage
    ain --status              Show pipeline status
    ain --approve             Approve planning artifacts
    ain --resume <stage>      Resume from a specific stage
    ain --stage <stage>       Run only this stage
    ain --reset               Reset pipeline to idle

Usage (drop-in):
    python pipeline.py        Same commands, same behaviour
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any

from ain.models.state import StageTiming
from ain.runtime.emitter import Emitter
from ain.runtime.events import (
    ApprovedEvent,
    ApprovalReceived,
    AwaitingApproval,
    HealthCheckResult,
    LogLevel,
    LogLine,
    LogSource,
    RunCompleted,
    RunStarted,
    RunStatus,
    StageCompleted,
    StageFailed,
    StageQueued,
    StageStarted,
    StageTimingUpdated,
    TaskCompleted,
    TaskFailed,
    TaskStarted,
    AgentOutput,
    WaitingApprovalEvent,
)
from ain.services import config_service, log_service, state_service

# 
# Helpers
# 

# Strips ANSI/VT escape sequences from agent output so they don't corrupt
# Rich's Live display when embedded in Text objects.
_ANSI_ESC = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07)")

def _strip_ansi(s: str) -> str:
    return _ANSI_ESC.sub("", s)


# 
# Paths  (REPO_ROOT = cwd so the package works in any repo)
# 

REPO_ROOT     = Path.cwd()
PIPELINE_DIR      = REPO_ROOT / ".ai-pipeline"
STATE_FILE        = PIPELINE_DIR / "state.json"
CONFIG_FILE       = PIPELINE_DIR / "config.json"
SCAN_DIR          = PIPELINE_DIR / "scan"
PROMPTS_DIR       = PIPELINE_DIR / "prompts"
LOGS_DIR          = PIPELINE_DIR / "logs"
APPROVALS_DIR     = PIPELINE_DIR / "approvals"
USER_CONTEXT_FILE = PIPELINE_DIR / "user_context.md"
BRAINSTORM_CONTEXT_FILE = PIPELINE_DIR / "brainstorm_context.md"
TASK_REVIEW_FEEDBACK_FILE = PIPELINE_DIR / "task_review_feedback.md"
DOCS_DIR      = REPO_ROOT / "docs"
PIPELINE_LOG  = LOGS_DIR / "pipeline.log"

REPO_TREE_FILE     = SCAN_DIR / "repo_tree.txt"
TRACKED_FILES_FILE = SCAN_DIR / "tracked_files.txt"
REPO_SUMMARY_FILE  = SCAN_DIR / "repo_summary.md"

ARCHITECTURE_FILE       = DOCS_DIR / "architecture.md"
OPEN_QUESTIONS_FILE     = DOCS_DIR / "OPEN_QUESTIONS.md"
OPEN_ANSWERS_FILE       = DOCS_DIR / "OPEN_ANSWERS.md"
PRD_FILE                = DOCS_DIR / "PRD.md"
DESIGN_FILE             = DOCS_DIR / "DESIGN.md"
FEATURE_SPEC_FILE       = DOCS_DIR / "FEATURE_SPEC.md"
TASKS_FILE              = DOCS_DIR / "TASKS.md"
TASK_GRAPH_FILE         = DOCS_DIR / "TASK_GRAPH.json"
IMPLEMENTATION_LOG_FILE = DOCS_DIR / "IMPLEMENTATION_LOG.md"

PLANNING_APPROVED_FLAG = APPROVALS_DIR / "planning_approved.flag"

# 
# Stage definitions
# 

STAGES = [
    "idle",
    "scanning",
    "architecture",
    "user_context",
    "planning_questions",
    "planning_generation",
    "task_creation",
    "waiting_approval",
    "implementation",
    "validation",
    "done",
]

FAILED = "failed"

STAGE_LABELS = {
    "idle":                "Idle",
    "scanning":            "Repository Scan",
    "architecture":        "Architecture Generation",
    "user_context":        "Feature Context",
    "planning_questions":  "Planning - Brainstorm",
    "planning_generation": "Planning - Generation",
    "task_creation":       "Task Creation",
    "waiting_approval":    "Waiting for Approval",
    "implementation":      "Implementation",
    "validation":          "Validation",
    "done":                "Done",
    "failed":              "Failed",
}

# 
# Validation rules
# 

ARCHITECTURE_HEADINGS = [
    "# System Overview", "# Tech Stack", "# Repo Structure",
    "# Core Domains", "# Runtime Architecture", "# Data Flow",
    "# Entry Points", "# State Management", "# Testing Strategy",
    "# Risks and Unknowns",
]

PRD_HEADINGS = [
    "# Problem", "# Goals", "# Non Goals",
    "# User Stories", "# Success Criteria",
]

DESIGN_HEADINGS = [
    "# Architecture Changes", "# Data Model", "# API Changes",
    "# UI Changes", "# Risks",
]

PIPELINE_MODES: dict[str, dict[str, Any]] = {
    "default": {
        "label": "Default",
        "summary": "Gemini -> Codex -> Chief -> Claude",
        "stages": {
            "planning_generation": "planning",
            "task_creation": "task_creation",
            "implementation": "implementation",
        },
    },
    "codex_only": {
        "label": "Codex Only",
        "summary": "Gemini -> Codex -> Codex -> Codex",
        "stages": {
            "planning_generation": "planning_codex",
            "task_creation": "task_creation_codex",
            "implementation": "implementation_codex",
        },
    },
    "claude_chief_only": {
        "label": "Claude/Chief Only",
        "summary": "Gemini -> Chief -> Chief -> Claude",
        "stages": {
            "planning_generation": "planning_chief",
            "task_creation": "task_creation",
            "implementation": "implementation",
        },
    },
}

# 
# Default configuration
# 

DEFAULT_CONFIG: dict[str, Any] = {
    "agents": {
        "architecture": {
            "command": "gemini", "args": [], "model": "gemini-2.5-flash",
            "description": "Gemini for architecture analysis",
        },
        "planning": {
            "command": "codex", "args": [], "model": "gpt-5.1",
            "prompt_mode": "arg",
            "description": "Codex for planning and specification",
        },
        "planning_codex": {
            "command": "codex", "args": [], "model": "gpt-5.1",
            "prompt_mode": "arg",
            "description": "Codex for planning generation in codex_only mode",
        },
        "planning_chief": {
            "command": "chief", "args": [], "model": "claude-opus-4.1",
            "description": "Chief for planning generation in claude_chief_only mode",
        },
        "task_creation": {
            "command": "chief", "args": [], "model": "claude-sonnet-4-20250514",
            "description": "Chief task orchestration engine  auto-installed by ain init",
        },
        "task_creation_codex": {
            "command": "codex", "args": ["exec"], "model": "gpt-5-codex",
            "prompt_mode": "stdin",
            "description": "Codex for task creation in codex_only mode",
        },
        "implementation": {
            "command": "claude",
            "args": ["--allowedTools", "Edit,Write,Bash,Read,Glob,Grep"],
            "model": "claude-sonnet-4-20250514",
            "description": "Claude Code for implementation with file access",
        },
        "implementation_codex": {
            "command": "codex",
            "args": ["exec"],
            "model": "gpt-5.1-codex-max",
            "prompt_mode": "stdin",
            "description": "Codex for implementation in codex_only mode",
        },
        "implementation_fallback": {
            "command": "codex",
            "args": ["--approval-mode", "full-auto"],
            "model": "gpt-5.1-codex-max",
            "prompt_mode": "arg",
            "description": "Codex fallback when default implementation hits token limits",
        },
    },
    "pipeline_mode": {
        "default": "default",
        "available": ["default", "codex_only", "claude_chief_only"],
    },
    "validation": {"auto_detect": True, "commands": []},
    "git": {"auto_branch": True, "auto_commit": False, "branch_prefix": "ai/feature"},
    "scan": {
        "ignore_dirs": [
            ".git", "node_modules", "vendor", ".venv", "venv",
            "__pycache__", ".ai-pipeline", "dist", "build",
            ".next", "coverage", ".turbo",
        ],
        "key_files": [
            "package.json", "composer.json", "requirements.txt",
            "Pipfile", "pyproject.toml", "Gemfile", "go.mod",
            "Cargo.toml", "pom.xml", "Dockerfile",
            "docker-compose.yml", "docker-compose.yaml",
            ".env.example", "Makefile", "README.md",
        ],
    },
}

# 
# UTF-8 output (Windows cp1252 can't render box-drawing chars)
# 

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 
# Terminal colors
# 

try:
    import colorama
    colorama.init(autoreset=True)
    _HAS_COLOR = True
except ImportError:
    _HAS_COLOR = False

_USE_COLOR = _HAS_COLOR or platform.system() != "Windows"


class C:
    RESET   = "\033[0m"  if _USE_COLOR else ""
    BOLD    = "\033[1m"  if _USE_COLOR else ""
    DIM     = "\033[2m"  if _USE_COLOR else ""
    RED     = "\033[91m" if _USE_COLOR else ""
    GREEN   = "\033[92m" if _USE_COLOR else ""
    YELLOW  = "\033[93m" if _USE_COLOR else ""
    BLUE    = "\033[94m" if _USE_COLOR else ""
    MAGENTA = "\033[95m" if _USE_COLOR else ""
    CYAN    = "\033[38;2;0;229;255m" if _USE_COLOR else ""


def _emit_log(message: str, level: LogLevel) -> None:
    """Emit a LogLine event if an emitter is active (non-blocking)."""
    if _EMITTER is not None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _emit(LogLine(ts=ts, level=level, source=LogSource.PIPELINE, stage_id=None, message=message))


def banner(text: str) -> None:
    if _EMITTER is None:   # plain mode: print to terminal
        w = 62
        print(f"\n{C.BOLD}{C.CYAN}{'-' * w}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'-' * w}{C.RESET}\n")
    _emit_log(f"===  {text}  ===", LogLevel.INFO)

def info(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.BLUE}  >{C.RESET} {text}")
    _emit_log(f"> {text}", LogLevel.INFO)

def success(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.GREEN}  OK{C.RESET} {text}")
    _emit_log(f"OK {text}", LogLevel.INFO)

def warn(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.YELLOW}  !{C.RESET} {text}")
    _emit_log(f"! {text}", LogLevel.WARN)

def error(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.RED}  X{C.RESET} {text}", file=sys.stderr)
    _emit_log(f"X {text}", LogLevel.ERROR)

def step(n: int, total: int, text: str) -> None:
    if _EMITTER is None:
        print(f"{C.BOLD}{C.CYAN}  [{n}/{total}]{C.RESET} {text}")
    _emit_log(f"[{n}/{total}] {text}", LogLevel.INFO)

# 
# Event bus state
# 

_EMITTER: Emitter | None = None
_RUN_ID: str = ""

# Optional TUI renderer with suspend/resume capability.
# Set by run_pipeline() when a Rich renderer is active so that interactive
# input prompts can temporarily hand the terminal back to cooked mode.
_RENDERER: Any = None

# Protects log-file writes and task-graph updates when tasks run in parallel.
_LOG_LOCK    = threading.Lock()
_GRAPH_LOCK  = threading.Lock()
# Unblocks the waiting_approval gate when approval is granted in-process.
_APPROVAL_EVENT = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(event: Any) -> None:
    if _EMITTER is not None:
        _EMITTER.emit(event)


def _display_path(path: Path) -> str:
    """Best-effort repo-relative display path for logs/UI."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _tui_suspend() -> None:
    """Pause the TUI (if active) before a blocking input() prompt."""
    if _RENDERER is not None and hasattr(_RENDERER, "suspend"):
        try:
            _RENDERER.suspend()
        except Exception:
            pass


def _tui_resume() -> None:
    """Resume the TUI (if active) after a blocking input() prompt."""
    if _RENDERER is not None and hasattr(_RENDERER, "resume"):
        try:
            _RENDERER.resume()
        except Exception:
            pass


# 
# Logging
# 

def _log(message: str, *, stage_id: str | None = None) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOG_LOCK:
        with open(PIPELINE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    _emit(LogLine(ts=ts, level=LogLevel.INFO, source=LogSource.PIPELINE, stage_id=stage_id, message=message))

# 
# State management
# 

def _default_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    selected_mode = (
        ((config or {}).get("pipeline_mode") or {}).get("default")
        or "default"
    )
    return {
        "current_stage": "idle", "branch": None,
        "started_at": None, "last_updated": None, "completed_stages": [],
        "fallback_mode": {},
        "last_safe_stage": "idle",
        "last_attempted_stage": None,
        "pause_reason": None,
        "pause_details": None,
        "resume_hint": None,
        "checkpoint_version": 1,
        "notification_channel": {},
        "selected_mode": selected_mode,
        "mode_changed_at": None,
        "status": "idle",
        "run_id": None,
        "last_approval_time": None,
    }


def load_state_with_backfill(
    state: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = _default_state(config)
    merged.update(state)
    if merged.get("completed_stages") is None:
        merged["completed_stages"] = []
    if merged.get("fallback_mode") is None:
        merged["fallback_mode"] = {}
    if merged.get("notification_channel") is None:
        merged["notification_channel"] = {}
    selected_mode = get_selected_mode(merged, config or DEFAULT_CONFIG, emit_warning=False)
    merged["selected_mode"] = selected_mode
    if "mode_changed_at" not in merged:
        merged["mode_changed_at"] = None
    if not merged.get("status"):
        merged["status"] = merged.get("current_stage", "idle")
    if "run_id" not in merged:
        merged["run_id"] = None
    if "last_approval_time" not in merged:
        merged["last_approval_time"] = None
    return merged


def load_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    current_config = config or load_config()
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        state = load_state_with_backfill(raw, current_config)
        if state != raw:
            save_state(state)
        return state
    return _default_state(current_config)


def save_state(state: dict[str, Any]) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    _log(f"State saved: {state['current_stage']}")


def set_stage(stage: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        state = load_state()
    prev = state.get("current_stage")
    if prev and prev not in ("idle", "failed", "done") and prev != stage:
        completed = state.get("completed_stages", [])
        if prev not in completed:
            completed.append(prev)
        state["completed_stages"] = completed
    state["current_stage"] = stage
    state["status"] = stage
    if stage not in ("idle",) and not state.get("started_at"):
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    _log(f"Stage: {prev}  {stage}")
    return state


def fail_pipeline(state: dict[str, Any], reason: str) -> None:
    error(f"Pipeline failed: {reason}")
    _log(f"FAILED: {reason}")
    state["current_stage"] = FAILED
    state["status"] = FAILED
    state["failure_reason"] = reason
    save_state(state)
    sys.exit(1)

# 
# Config management
# 

def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def save_config(config: dict[str, Any]) -> None:
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        config = _deep_merge(DEFAULT_CONFIG, raw)
        if config != raw:
            save_config(config)
        return config
    return copy.deepcopy(DEFAULT_CONFIG)


def ensure_config() -> None:
    if not CONFIG_FILE.exists():
        PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        info(f"Created default config: {CONFIG_FILE.relative_to(REPO_ROOT)}")


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key in sorted(data):
        value = data[key]
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.extend(_flatten_dict(value, path))
        else:
            items.append((path, value))
    return items


def _config_lookup(config: dict[str, Any], dotted_key: str) -> tuple[dict[str, Any], str]:
    parts = dotted_key.split(".")
    current: dict[str, Any] = config
    for part in parts[:-1]:
        value = current.get(part)
        if not isinstance(value, dict):
            raise KeyError(dotted_key)
        current = value
    return current, parts[-1]


def _config_value(config: dict[str, Any], dotted_key: str) -> Any:
    parent, leaf = _config_lookup(config, dotted_key)
    if leaf not in parent:
        raise KeyError(dotted_key)
    return parent[leaf]


def _parse_config_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _reset_config_key(config: dict[str, Any], dotted_key: str) -> bool:
    try:
        default_value = _config_value(DEFAULT_CONFIG, dotted_key)
    except KeyError:
        parent, leaf = _config_lookup(config, dotted_key)
        if leaf not in parent:
            return False
        del parent[leaf]
        return True

    parent, leaf = _config_lookup(config, dotted_key)
    parent[leaf] = copy.deepcopy(default_value)
    return True


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2))


def _try_parse_log_line(text: str) -> tuple[str | None, str]:
    match = re.match(r"^\[(?P<ts>[^\]]+)\]\s*(?P<message>.*)$", text.rstrip())
    if match:
        return match.group("ts"), match.group("message")
    return None, text.rstrip()


def _detect_log_level(message: str) -> str:
    lowered = message.lower()
    if "error" in lowered or "failed" in lowered:
        return "error"
    if "warn" in lowered:
        return "warn"
    if "debug" in lowered:
        return "debug"
    return "info"


def _load_log_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    files: list[tuple[Path, str]] = [
        (LOGS_DIR / "pipeline.log", "pipeline"),
        (LOGS_DIR / "validation.log", "validation"),
    ]
    agents_dir = LOGS_DIR / "agents"
    if agents_dir.exists():
        for path in sorted(agents_dir.glob("*.log")):
            files.append((path, "agent"))

    for path, source in files:
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                timestamp, message = _try_parse_log_line(raw_line)
                entries.append(
                    {
                        "timestamp": timestamp or "",
                        "level": _detect_log_level(message),
                        "source": source,
                        "message": message,
                        "path": str(path),
                    }
                )

    entries.sort(key=lambda entry: (entry["timestamp"], entry["path"], entry["message"]))
    return entries


def _show_logs(
    *,
    follow: bool = False,
    tail: int = 50,
    level: str | None = None,
    source: str | None = None,
    as_json: bool = False,
) -> None:
    while True:
        entries = _load_log_entries()
        if source:
            entries = [entry for entry in entries if entry["source"] == source]
        if level:
            entries = [entry for entry in entries if entry["level"] == level]
        visible = entries[-tail:] if tail > 0 else entries
        if as_json:
            _print_json(visible)
        else:
            for entry in visible:
                ts = f"[{entry['timestamp']}] " if entry["timestamp"] else ""
                print(f"{ts}{entry['source']} {entry['level']}: {entry['message']}")
        if not follow:
            return
        time.sleep(1)


def _show_config_list() -> None:
    config = load_config()
    for key, value in _flatten_dict(config):
        rendered = json.dumps(value) if isinstance(value, (dict, list, bool)) or value is None else value
        print(f"{key} = {rendered}")


def _show_config_get(key: str) -> None:
    value = _config_value(load_config(), key)
    if isinstance(value, (dict, list)):
        _print_json(value)
    else:
        print(json.dumps(value) if isinstance(value, bool) or value is None else value)


def _show_version(short: bool = False) -> None:
    from ain import __version__

    version = __version__
    commit = None
    try:
        commit = run_command_output(["git", "rev-parse", "--short", "HEAD"]).strip()
    except Exception:
        commit = None

    if short:
        print(version)
        return

    if commit:
        print(f"ain {version} ({commit})")
        return

    print(f"ain {version}")


def get_available_pipeline_modes(config: dict[str, Any]) -> list[str]:
    configured = (
        ((config.get("pipeline_mode") or {}).get("available"))
        or list(PIPELINE_MODES.keys())
    )
    available = [mode for mode in configured if mode in PIPELINE_MODES]
    return available or ["default"]


def get_selected_mode(
    state: dict[str, Any],
    config: dict[str, Any],
    *,
    emit_warning: bool = True,
) -> str:
    available = get_available_pipeline_modes(config)
    selected = (
        state.get("selected_mode")
        or ((config.get("pipeline_mode") or {}).get("default"))
        or "default"
    )
    if selected not in PIPELINE_MODES or selected not in available:
        if emit_warning:
            warn(f"Invalid stored pipeline mode '{selected}'. Falling back to default.")
        return "default"
    return selected


def get_mode_details(mode: str) -> dict[str, str]:
    meta = PIPELINE_MODES[mode]
    return {
        "key": mode,
        "label": meta["label"],
        "summary": meta["summary"],
    }


def resolve_stage_agent_key(stage_name: str, state: dict[str, Any], config: dict[str, Any]) -> str:
    mode = get_selected_mode(state, config)
    agent_key = PIPELINE_MODES[mode]["stages"].get(stage_name)
    if not agent_key:
        raise RuntimeError(f"No agent mapping defined for stage '{stage_name}' in mode '{mode}'.")
    if agent_key not in config.get("agents", {}):
        raise RuntimeError(
            f"Resolved agent '{agent_key}' for stage '{stage_name}' is missing from .ai-pipeline/config.json."
        )
    return agent_key


def resolve_agent_config(stage_name: str, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return config.get("agents", {}).get(resolve_stage_agent_key(stage_name, state, config), {})


def set_pipeline_mode(mode: str, state: dict[str, Any], config: dict[str, Any]) -> None:
    available = get_available_pipeline_modes(config)
    if mode not in PIPELINE_MODES or mode not in available:
        raise RuntimeError(f"Unsupported pipeline mode '{mode}'.")
    current_stage = state.get("current_stage", "idle")
    if state.get("selected_mode") == mode:
        return
    now = datetime.now(timezone.utc).isoformat()
    state["selected_mode"] = mode
    state["mode_changed_at"] = now
    config.setdefault("pipeline_mode", {})
    config["pipeline_mode"]["default"] = mode
    save_state(state)
    save_config(config)
    details = get_mode_details(mode)
    if current_stage in {"scanning", "architecture", "planning_generation", "task_creation", "implementation", "validation"}:
        info(
            f"Pipeline mode changed to {details['label']} ({details['summary']}). "
            "The new mode will be used starting with the next stage."
        )
    else:
        info(f"Pipeline mode changed to {details['label']} ({details['summary']}).")


def cycle_pipeline_mode(state: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    available = get_available_pipeline_modes(config)
    current = get_selected_mode(state, config)
    idx = available.index(current)
    next_mode = available[(idx + 1) % len(available)]
    try:
        set_pipeline_mode(next_mode, state, config)
        return get_mode_details(next_mode)
    except RuntimeError as exc:
        warn(str(exc))
        return get_mode_details(current)


def prompt_for_pipeline_mode(state: dict[str, Any], config: dict[str, Any]) -> str:
    available = get_available_pipeline_modes(config)
    current = get_selected_mode(state, config)
    # Non-interactive fallback (CI, pipes): keep old numeric input behavior.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print()
        print("  Select pipeline mode:")
        for index, mode in enumerate(available, start=1):
            details = get_mode_details(mode)
            marker = " (current)" if mode == current else ""
            print(f"    {index}. {details['label']} [{mode}] - {details['summary']}{marker}")
        print(f"  Press Enter to keep {current}.")
        try:
            choice = input("  Mode: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return current
        if not choice:
            return current
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(available):
                return available[idx]
        if choice in available:
            return choice
        warn(f"Unknown mode '{choice}'. Keeping {current}.")
        return current

    from rich.console import Console as RichConsole
    from rich.panel import Panel
    from rich.text import Text

    console = RichConsole(legacy_windows=False)
    rich_cyan = "#00e5ff"
    rich_pink = "#ff2d78"
    rich_dim = "dim #00e5ff"
    selected = available.index(current) if current in available else 0

    while True:
        body = Text()
        body.append("Select Pipeline Mode\n\n", style=f"bold {rich_cyan}")
        for idx, mode in enumerate(available):
            details = get_mode_details(mode)
            cursor = "► " if idx == selected else "  "
            cursor_style = f"bold {rich_pink}" if idx == selected else rich_dim
            line_style = f"bold {rich_pink}" if idx == selected else rich_cyan
            current_marker = " (current)" if mode == current else ""
            body.append(cursor, style=cursor_style)
            body.append(f"{details['label']} [{mode}]{current_marker}\n", style=line_style)
            body.append(f"   {details['summary']}\n", style=rich_dim)
        body.append("\n↑/↓ navigate • Enter select • Q keep current", style=rich_dim)

        panel = Panel(
            body,
            title="A.I.N. MODE SELECTOR",
            title_align="left",
            border_style=rich_pink,
            padding=(1, 2),
        )
        console.clear()
        console.print()
        console.print(panel)

        key = _read_keypress()
        if key == "up":
            selected = (selected - 1) % len(available)
        elif key == "down":
            selected = (selected + 1) % len(available)
        elif key == "enter":
            console.clear()
            return available[selected]
        elif key == "quit":
            console.clear()
            return current

# 
# Command runner
# 

def run_command(
    cmd: list[str] | str,
    cwd: Path | None = None,
    capture: bool = False,
    input_text: str | None = None,
    timeout: int = 300,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    _log(f"RUN: {cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)}")
    kwargs: dict[str, Any] = {
        "cwd": str(cwd or REPO_ROOT),
        "timeout": timeout,
        "env": {**os.environ, **(env or {})},
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    if input_text is not None:
        kwargs["input"] = input_text
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    if isinstance(cmd, str):
        kwargs["shell"] = True
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0 and capture:
        _log(f"STDERR: {(result.stderr or '').strip()[:500]}")
    return result


def run_command_output(cmd: list[str] | str, cwd: Path | None = None) -> str:
    result = run_command(cmd, cwd=cwd, capture=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd}\n{result.stderr}"
        )
    return result.stdout.strip()

# 
# AI agent caller
# 

def call_agent(agent_name: str, prompt: str, config: dict) -> str:
    agent_cfg   = config["agents"].get(agent_name, {})
    command     = agent_cfg.get("command", agent_name)
    extra_args  = agent_cfg.get("args", [])
    model       = agent_cfg.get("model")
    prompt_mode = agent_cfg.get("prompt_mode", "stdin")  # "stdin" | "arg"

    # Resolve full path so subprocess finds .cmd wrappers on Windows
    resolved = shutil.which(command)
    if resolved:
        command = resolved

    cmd = [command] + extra_args
    if model:
        cmd += ["--model", model]

    info(f"Invoking {agent_cfg.get('command', agent_name)} ({agent_name}) ...")
    _log(f"AGENT CALL: {agent_name} via {command}\n\t(prompt_mode={prompt_mode})")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{agent_name}_last_prompt.txt").write_text(prompt, encoding="utf-8")

    try:
        if prompt_mode == "arg":
            # Pass prompt as a positional argument (e.g. codex "prompt text")
            result = run_command(cmd + [prompt], capture=True, timeout=600)
        else:
            # Default: pipe prompt via stdin
            result = run_command(cmd, capture=True, input_text=prompt, timeout=600)
        output = result.stdout or ""
        if result.returncode != 0:
            warn(f"Agent {agent_name} exited {result.returncode}")
            _log(f"AGENT STDERR: {(result.stderr or '')[:500]}")
        (LOGS_DIR / f"{agent_name}_last_output.txt").write_text(output, encoding="utf-8")
        return output
    except FileNotFoundError:
        raise RuntimeError(
            f"Agent command not found: '{command}'. "
            f"Edit .ai-pipeline/config.json to configure the '{agent_name}' agent."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agent '{agent_name}' timed out after 600 seconds.")


def _resolve_agent_command(agent_name: str, config: dict[str, Any]) -> list[str]:
    agent_cfg = config.get("agents", {}).get(agent_name, {})
    command = agent_cfg.get("command", agent_name)
    resolved = shutil.which(command)
    if resolved:
        command = resolved
    cmd = [command] + agent_cfg.get("args", [])
    if agent_cfg.get("model"):
        cmd += ["--model", agent_cfg["model"]]
    return cmd


def _agent_display_name(agent_name: str, config: dict[str, Any]) -> str:
    command = (config.get("agents", {}).get(agent_name) or {}).get("command", agent_name)
    return str(command).capitalize()


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill a process and all its children (Windows-aware)."""
    if p.poll() is not None:
        return
    if platform.system() == "Windows":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(p.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        import signal as _signal, os as _os
        try:
            _os.killpg(_os.getpgid(p.pid), _signal.SIGTERM)
        except Exception:
            p.terminate()


def _run_agent_background(
    cmd: list,
    agent_name: str,
    log_slug: str = "",
    input_text: str | None = None,
) -> tuple[int, str]:
    """Run an agent subprocess in the background, streaming its output to the TUI
    agent panel via AgentOutput events. The TUI stays live  no suspend/resume.

    If *input_text* is provided it is written to stdin then stdin is closed,
    so the agent receives the prompt without blocking on the TUI keyboard thread.
    If omitted, stdin is DEVNULL.

    Returns (exit_code, captured_output).
    """
    slug = log_slug or agent_name.lower().replace(" ", "_")
    mode = "stdin" if input_text is not None else "devnull"
    _log(f"AGENT CALL: {agent_name} via {cmd[0]}\n\t(prompt_mode=background/{mode})")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise RuntimeError(f"Agent command not found: '{cmd[0]}'")

    output_lines: list[str] = []

    if input_text is not None:
        # Write stdin in a background thread so it doesn't block stdout reading.
        # This mirrors what communicate() does internally but lets us stream each
        # line to the TUI as it arrives instead of waiting for the process to finish.
        def _writer() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except OSError:
                pass

        writer = threading.Thread(target=_writer, daemon=True)
        writer.start()

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = _strip_ansi(raw.rstrip("\n\r"))
            output_lines.append(line)
            _emit(AgentOutput(ts=_now_iso(), line=line, agent=agent_name))

        writer.join(timeout=5)
        proc.wait()
    else:
        # No stdin  stream stdout live line by line.
        def _reader() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = _strip_ansi(raw.rstrip("\n\r"))
                output_lines.append(line)
                _emit(AgentOutput(ts=_now_iso(), line=line, agent=agent_name))

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        proc.wait()
        reader.join(timeout=5)

    captured = "\n".join(output_lines)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{slug}_last_output.txt").write_text(captured, encoding="utf-8")
    return proc.returncode, captured


def read_context_files(*files: Path) -> str:
    parts = []
    for f in files:
        if f.exists():
            content = f.read_text(encoding="utf-8")
            parts.append(f"<!-- FILE: {f.name} -->\n{content}\n<!-- END: {f.name} -->")
        else:
            parts.append(f"<!-- FILE: {f.name}  NOT FOUND -->")
    return "\n\n".join(parts)


def build_prompt(prompt_file: Path, *context_files: Path) -> str:
    if not prompt_file.exists():
        raise RuntimeError(f"Prompt file not found: {prompt_file}")
    prompt = prompt_file.read_text(encoding="utf-8")
    if context_files:
        ctx = read_context_files(*context_files)
        prompt = f"{prompt}\n\n---\n## Context\n\n{ctx}"
    return prompt

# 
# Validators
# 

def validate_headings(file: Path, required: list[str]) -> list[str]:
    if not file.exists():
        return required[:]
    content = file.read_text(encoding="utf-8")
    return [h for h in required if not re.search(r"^" + re.escape(h) + r"(\s|$)", content, re.MULTILINE)]


def validate_tasks_file(tasks_file: Path) -> bool:
    if not tasks_file.exists():
        return False
    return bool(re.search(r"- \[[ x]\]", tasks_file.read_text(encoding="utf-8")))


def validate_task_graph(graph_file: Path) -> bool:
    if not graph_file.exists():
        return False
    try:
        content = _strip_fences(graph_file.read_text(encoding="utf-8"))
        data = json.loads(content)
        # If valid JSON with tasks, also rewrite without fences
        if isinstance(data.get("tasks"), list) and len(data["tasks"]) > 0:
            graph_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        return False
    except (json.JSONDecodeError, KeyError):
        return False

# 
# Stage 1: Repository Scan
# 

def _build_tree(root: Path, ignore: set[str], prefix: str = "", depth: int = 0, max_depth: int = 6) -> list[str]:
    if depth > max_depth:
        return ["..."]
    lines = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return []
    visible = [e for e in entries if e.name not in ignore]
    for i, entry in enumerate(visible):
        connector = " " if i == len(visible) - 1 else " "
        lines.append(f"{prefix}{connector}{entry.name}")
        if entry.is_dir():
            ext = "    " if i == len(visible) - 1 else "   "
            lines.extend(_build_tree(entry, ignore, prefix + ext, depth + 1, max_depth))
    return lines


def scan_repo_tree(config: dict) -> str:
    ignore = set(config["scan"]["ignore_dirs"])
    return "\n".join([REPO_ROOT.name + "/"] + _build_tree(REPO_ROOT, ignore))


def scan_git_files() -> list[str]:
    try:
        out = run_command_output(["git", "ls-files"], cwd=REPO_ROOT)
        return [l for l in out.splitlines() if l.strip()]
    except RuntimeError:
        return []


def detect_stack(tracked_files: list[str]) -> dict[str, Any]:
    files_set = set(tracked_files)
    stack: dict[str, Any] = {
        "languages": [], "frameworks": [], "package_managers": [], "devops": [],
    }
    ext_counts: dict[str, int] = {}
    for f in tracked_files:
        ext = Path(f).suffix.lower()
        if ext:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    lang_map = {
        ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".js": "JavaScript", ".jsx": "JavaScript", ".php": "PHP",
        ".rb": "Ruby", ".go": "Go", ".rs": "Rust", ".java": "Java", ".cs": "C#",
    }
    seen: set[str] = set()
    for ext, lang in lang_map.items():
        if ext in ext_counts and lang not in seen:
            stack["languages"].append(lang)
            seen.add(lang)

    pm_map = {
        "package.json": "npm/yarn/bun", "composer.json": "Composer",
        "requirements.txt": "pip", "pyproject.toml": "poetry/uv",
        "Gemfile": "Bundler", "go.mod": "Go modules", "Cargo.toml": "Cargo",
    }
    for fname, pm in pm_map.items():
        if fname in files_set:
            stack["package_managers"].append(pm)

    if "artisan" in files_set or any("app/Http" in f for f in tracked_files):
        stack["frameworks"].append("Laravel")
    if any("next.config" in f for f in tracked_files):
        stack["frameworks"].append("Next.js")
    if any("nuxt.config" in f for f in tracked_files):
        stack["frameworks"].append("Nuxt.js")
    if any("manage.py" in f for f in tracked_files):
        stack["frameworks"].append("Django")
    if "Dockerfile" in files_set:
        stack["devops"].append("Docker")
    if any("docker-compose" in f for f in tracked_files):
        stack["devops"].append("Docker Compose")
    if any(".github/workflows" in f for f in tracked_files):
        stack["devops"].append("GitHub Actions")

    migrations = [f for f in tracked_files if "migration" in f.lower()]
    if migrations:
        stack["migrations"] = migrations[:10]

    return stack


def _extract_key_file_content(config: dict) -> dict[str, str]:
    result = {}
    for fname in config["scan"]["key_files"]:
        path = REPO_ROOT / fname
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if len(content) > 3000:
                content = content[:3000] + "\n... [truncated]"
            result[fname] = content
    return result


def generate_repo_summary(tree: str, tracked_files: list[str], config: dict) -> str:
    stack     = detect_stack(tracked_files)
    key_files = _extract_key_file_content(config)

    lines = [
        "# Repository Summary",
        f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "\n## Technology Stack",
    ]
    for cat, items in stack.items():
        if items and cat != "migrations":
            lines.append(f"\n**{cat.title()}:** {', '.join(str(i) for i in items)}")
    if "migrations" in stack:
        lines.append(f"\n**Migrations:** {len(stack['migrations'])} files detected")

    lines.append(f"\n## File Count\n\nTracked files: {len(tracked_files)}")
    lines.append("\n## Key Configuration Files\n")
    for fname, content in key_files.items():
        lines.append(f"### {fname}\n```\n{content}\n```\n")
    if not key_files:
        lines.append("No standard configuration files detected.")

    lines.append("\n## Entry Points\n")
    entries = [f for f in tracked_files
               if any(n in Path(f).name.lower() for n in
                      ["main.", "index.", "app.", "server.", "manage.py", "artisan"])
               and not any(s in f for s in ["node_modules", "vendor", "test", "spec"])]
    for ep in entries[:15]:
        lines.append(f"- `{ep}`")

    lines.append("\n## Routes / Controllers\n")
    routes = [f for f in tracked_files
              if any(kw in f.lower() for kw in ["route", "controller", "handler", "endpoint"])
              and "node_modules" not in f and "vendor" not in f]
    for rf in routes[:20]:
        lines.append(f"- `{rf}`")

    lines.append(f"\n## Repository Tree\n\n```\n{tree}\n```")
    return "\n".join(lines)


def run_scan(state: dict, config: dict) -> None:
    banner("Stage: Repository Scan")
    SCAN_DIR.mkdir(parents=True, exist_ok=True)

    step(1, 3, "Building repository tree ...")
    tree = scan_repo_tree(config)
    REPO_TREE_FILE.write_text(tree, encoding="utf-8")
    success(f"Tree  {REPO_TREE_FILE.relative_to(REPO_ROOT)}")

    step(2, 3, "Scanning tracked files ...")
    tracked = scan_git_files()
    if not tracked:
        warn("No git-tracked files. Falling back to filesystem scan.")
        ignore = set(config["scan"]["ignore_dirs"])
        tracked = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
                   for p in REPO_ROOT.rglob("*")
                   if p.is_file() and not any(ig in p.parts for ig in ignore)]
    TRACKED_FILES_FILE.write_text("\n".join(tracked), encoding="utf-8")
    success(f"{len(tracked)} files  {TRACKED_FILES_FILE.relative_to(REPO_ROOT)}")

    step(3, 3, "Generating repository summary ...")
    summary = generate_repo_summary(tree, tracked, config)
    REPO_SUMMARY_FILE.write_text(summary, encoding="utf-8")
    success(f"Summary  {REPO_SUMMARY_FILE.relative_to(REPO_ROOT)}")

    set_stage("architecture", state)
    success("Scan complete.")

# 
# Stage 2: Architecture Generation (Gemini)
# 

def run_architecture(state: dict, config: dict) -> None:
    banner("Stage: Architecture Generation")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    step(1, 3, "Building prompt ...")
    prompt_instructions = (PROMPTS_DIR / "architecture_prompt.md").read_text(encoding="utf-8")
    # Full prompt with embedded context  used by Codex fallback.
    full_prompt = build_prompt(
        PROMPTS_DIR / "architecture_prompt.md",
        REPO_TREE_FILE, REPO_SUMMARY_FILE, TRACKED_FILES_FILE,
    )
    (LOGS_DIR / "architecture_last_prompt.txt").write_text(prompt_instructions, encoding="utf-8")

    agent_cfg  = config["agents"].get("architecture", {})
    command    = agent_cfg.get("command", "gemini")
    resolved   = shutil.which(command) or command
    extra_args = agent_cfg.get("args", [])

    def _gemini_ok() -> bool:
        """True if architecture.md exists and passes heading validation."""
        return (
            ARCHITECTURE_FILE.exists()
            and ARCHITECTURE_FILE.stat().st_size > 0
            and not validate_headings(ARCHITECTURE_FILE, ARCHITECTURE_HEADINGS)
        )

    step(2, 3, "Calling Gemini architecture agent ...")
    if ARCHITECTURE_FILE.exists():
        ARCHITECTURE_FILE.unlink()

    rc, gemini_output = _run_agent_background(
        [resolved] + extra_args,
        agent_name="Gemini",
        log_slug="architecture",
        input_text=full_prompt,  # embed context directly  .ai-pipeline/ may be gitignored
    )

    # Gemini outputs to stdout (no write_file tool available via stdin mode).
    # Strip executor noise lines that Gemini emits when tool calls are blocked.
    if not (ARCHITECTURE_FILE.exists() and ARCHITECTURE_FILE.stat().st_size > 0):
        clean_output = "\n".join(
            l for l in gemini_output.splitlines()
            if not l.startswith("[LocalAgentExecutor]")
            and not l.startswith("Attempt ")
            and not l.startswith("Error executing tool")
        ).strip()
        if clean_output:
            ARCHITECTURE_FILE.write_text(clean_output, encoding="utf-8")
            success(f"Written  {ARCHITECTURE_FILE.relative_to(REPO_ROOT)}")

    if rc != 0 or not _gemini_ok():
        warn(f"Gemini {'exited with code ' + str(rc) if rc != 0 else 'did not produce a valid architecture.md'}.")
        warn("Falling back to Codex for architecture generation ...")

        step(3, 3, "Calling Codex fallback agent ...")
        if ARCHITECTURE_FILE.exists():
            ARCHITECTURE_FILE.unlink()

        codex_cmd = shutil.which("codex") or "codex"
        _, codex_output = _run_agent_background(
            [codex_cmd],
            agent_name="Codex (arch fallback)",
            log_slug="architecture_codex",
            input_text=full_prompt,
        )

        # Codex may write the file directly or output it to stdout.
        if not (ARCHITECTURE_FILE.exists() and ARCHITECTURE_FILE.stat().st_size > 0):
            if codex_output.strip():
                ARCHITECTURE_FILE.write_text(codex_output, encoding="utf-8")
            else:
                raise RuntimeError(
                    "Both Gemini and Codex failed to produce docs/architecture.md."
                )

    missing = validate_headings(ARCHITECTURE_FILE, ARCHITECTURE_HEADINGS)
    if missing:
        for h in missing:
            warn(f"  Missing heading: {h}")
        raise RuntimeError("Architecture validation failed. Fix docs/architecture.md then re-run.")

    success("Architecture validation passed.")
    set_stage("user_context", state)

# 
# 
# Popup helpers
# 

USER_CONTEXT_TEMPLATE = """\
# Feature / Bug Context

Describe the feature you want to implement or the bug you want to fix.
Be as specific as possible  this will guide the entire planning phase.

## What do you want to build or fix#

(Replace this text with your description)

## Additional context (optional)

- Relevant files or areas of the codebase:
- Related issues or tickets:
- Constraints or requirements:
- Tech preferences:
"""


def _open_in_editor(path: Path) -> None:
    """Open a file in the system default text editor."""
    if platform.system() == "Windows":
        subprocess.Popen(["notepad", str(path)])
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", "-t", str(path)])
    else:
        editor = os.environ.get("EDITOR", "nano")
        subprocess.run([editor, str(path)])


def _open_popup_terminal(title: str, command: str) -> None:
    """Open a new terminal window running the given shell command."""
    if platform.system() == "Windows":
        # Write the command to a temp batch file to avoid multi-layer quoting
        # issues with cmd's `start "title" cmd /k <command>` parsing.
        # list2cmdline double-escapes pre-quoted title strings, causing Windows
        # to interpret words in the title as the executable name.
        import tempfile
        bat = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bat", delete=False, encoding="utf-8"
        )
        bat.write(f"@echo off\r\n{command}\r\n")
        bat.close()
        safe_title = title.replace('"', "'")
        subprocess.Popen(
            ["cmd", "/c", "start", safe_title, "cmd", "/k", bat.name],
            shell=False,
        )
    elif platform.system() == "Darwin":
        # Escape embedded double-quotes before embedding in AppleScript.
        safe_cmd = command.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Terminal" to do script "{safe_cmd}"'
        subprocess.Popen(["osascript", "-e", script])
    else:
        # Use list-form exec  no shell=True, no injection.
        for term_prefix in [
            ["gnome-terminal", "--"],
            ["xterm", "-e"],
            ["konsole", "-e"],
        ]:
            exe = term_prefix[0]
            if shutil.which(exe):
                subprocess.Popen(term_prefix + ["bash", "-c", f"{command}; exec bash"])
                break


def _run_interactive_in_tui(cmd: list[str]) -> None:
    """Run an interactive subprocess with I/O routed through the TUI.

    stdout/stderr lines are emitted as LogLine events (visible in the stream
    panel).  User responses are collected via the TUI input panel
    (``request_input``) or plain ``input()`` in non-TUI mode.

    Idle detection: after ``_IDLE_SECS`` of silence the process is assumed to
    be waiting for user input.  The user can type ``done`` / ``exit`` /
    ``quit`` to terminate early.
    """
    _IDLE_SECS = 0.4   # silence threshold before requesting input
    _POLL_MS   = 0.05  # output-reader poll interval

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    line_q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line_q.put(raw.rstrip("\n\r"))
        finally:
            line_q.put(None)  # sentinel: subprocess finished

    threading.Thread(target=_reader, daemon=True).start()

    last_output = time.monotonic()

    while True:
        # Drain all available output with a short poll.
        try:
            line = line_q.get(timeout=_POLL_MS)
            if line is None:          # sentinel  process exited
                return
            if line:
                _emit_log(line, LogLevel.INFO)
                last_output = time.monotonic()
            continue                  # keep draining
        except queue.Empty:
            pass

        if proc.poll() is not None:   # exited between polls
            return

        idle = time.monotonic() - last_output
        if idle < _IDLE_SECS:
            continue                  # not idle yet

        #  Request input from the user 
        if _RENDERER is not None and hasattr(_RENDERER, "request_input"):
            user_input = _RENDERER.request_input(
                "Your response  (type 'done' to end session)"
            )
        else:
            try:
                user_input = input("> ")
            except (EOFError, KeyboardInterrupt):
                proc.terminate()
                return

        if user_input.strip().lower() in ("done", "exit", "quit", "q"):
            proc.terminate()
            proc.wait()
            return

        try:
            assert proc.stdin is not None
            proc.stdin.write(user_input + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            return

        last_output = time.monotonic()  # reset idle clock after sending input


def _run_suspended(cmd: list, title: str = "") -> int:
    """Suspend the TUI, run *cmd* raw in the terminal, then resume the TUI.

    The subprocess inherits stdin/stdout/stderr so interactive CLI tools
    (codex, editors, etc.) work exactly as if launched normally.
    Returns the subprocess exit code.
    """
    if _RENDERER is not None:
        _RENDERER.suspend()
    try:
        if title:
            print(f"\n\033[1;96m   {title} \033[0m\n")
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        return result.returncode
    finally:
        if _RENDERER is not None:
            _RENDERER.resume()


def _collect_multiline_input(prompt_header: str) -> str:
    """Suspend TUI, collect multi-line input from the user, resume TUI.

    The user types their text and submits by entering '---' on a blank line
    or pressing Ctrl+D / Ctrl+Z.
    Returns the collected text (stripped).
    """
    if _RENDERER is not None:
        _RENDERER.suspend()
    try:
        width = 60
        print(f"\n\033[1;91m{'-' * width}\033[0m")
        print(f"\033[1;91m  {prompt_header}\033[0m")
        print(f"\033[1;91m{'-' * width}\033[0m")
        print(f"\033[96m  Type your description below.\033[0m")
        print(f"\033[96m  Enter \033[1m---\033[0m\033[96m on a new line to finish.\033[0m\n")
        lines: list[str] = []
        try:
            while True:
                line = input()
                if line.strip() == "---":
                    break
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            pass
        print(f"\n\033[1;91m{'-' * width}\033[0m\n")
        return "\n".join(lines).strip()
    finally:
        if _RENDERER is not None:
            _RENDERER.resume()


def _wait_for_user(prompt: str) -> None:
    """Block until the user acknowledges.  In TUI mode the input panel is used;
    in plain mode a regular input() call is made."""
    if _RENDERER is not None and hasattr(_RENDERER, "request_input"):
        _RENDERER.request_input(prompt)
        return
    # Plain/fallback mode: standard terminal input.
    print()
    try:
        input(f"  {prompt}  ")
    except (EOFError, KeyboardInterrupt):
        warn("Interrupted.")
        sys.exit(0)


def _extract_tasks_for_review() -> list[str]:
    """Load task descriptions from TASK_GRAPH.json, with TASKS.md fallback."""
    if TASK_GRAPH_FILE.exists():
        try:
            data = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
            tasks = [
                str(t.get("description", "")).strip()
                for t in data.get("tasks", [])
                if str(t.get("description", "")).strip()
            ]
            if tasks:
                return tasks
        except Exception:
            pass

    if TASKS_FILE.exists():
        tasks: list[str] = []
        for line in TASKS_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- [ ] ") or s.startswith("- [x] "):
                tasks.append(s[6:].strip())
        if tasks:
            return tasks

    return []


def _read_keypress() -> str:
    """Read a single keypress and normalize to up/down/left/right/enter/quit/other."""
    try:
        import msvcrt  # type: ignore

        while True:
            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                ch2 = msvcrt.getch()
                if ch2 == b"H":
                    return "up"
                if ch2 == b"P":
                    return "down"
                if ch2 == b"K":
                    return "left"
                if ch2 == b"M":
                    return "right"
                return "other"
            if ch in (b"\r", b"\n"):
                return "enter"
            if ch in (b"q", b"Q", b"\x03"):
                return "quit"
            return "other"
    except ImportError:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                if not select.select([sys.stdin], [], [], 0.1)[0]:
                    continue
                ch = sys.stdin.buffer.read(1)
                if ch in (b"\r", b"\n"):
                    return "enter"
                if ch == b"\x1b":
                    seq = sys.stdin.buffer.read(2)
                    if seq == b"[A":
                        return "up"
                    if seq == b"[B":
                        return "down"
                    if seq == b"[D":
                        return "left"
                    if seq == b"[C":
                        return "right"
                    return "other"
                if ch in (b"q", b"Q", b"\x03"):
                    return "quit"
                return "other"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render_task_review_popup(tasks: list[str], selected_row: int, decisions: list[bool]) -> None:
    from rich.console import Console as RichConsole
    from rich.panel import Panel
    from rich.text import Text

    rich_cyan = "#00e5ff"
    rich_pink = "#ff2d78"
    rich_dim = "dim #00e5ff"

    console = RichConsole(legacy_windows=False)
    body = Text()
    body.append("Review task list before implementation\n\n", style=f"bold {rich_cyan}")

    for idx, task in enumerate(tasks):
        is_selected = idx == selected_row
        cursor = "► " if is_selected else "  "
        cursor_style = f"bold {rich_pink}" if is_selected else rich_dim
        row_style = f"bold {rich_pink}" if is_selected else rich_cyan
        status = "ACCEPT" if decisions[idx] else "DENY"
        status_style = rich_cyan if decisions[idx] else "yellow"
        body.append(cursor, style=cursor_style)
        body.append(f"{idx+1:02d}. {task}\n", style=row_style)
        body.append("   [", style=rich_dim)
        body.append(status, style=status_style)
        body.append("]\n", style=rich_dim)

    denied = sum(1 for d in decisions if not d)
    body.append("\n", style=rich_dim)
    if denied == 0:
        body.append("Enter approve and continue to implementation\n", style="bold green")
    else:
        body.append(
            f"Enter deny ({denied} denied), provide feedback, and re-run planning/task creation\n",
            style="bold yellow",
        )
    body.append("↑/↓ navigate • ←/→ toggle • Enter submit • Q keep current run state", style=rich_dim)

    panel = Panel(
        body,
        title="A.I.N. TASK REVIEW",
        title_align="left",
        border_style=rich_pink,
        padding=(1, 2),
    )
    console.clear()
    console.print()
    console.print(panel)


def _review_tasks_with_popup(tasks: list[str]) -> tuple[bool, str]:
    """Interactive task review. Returns (approved, feedback)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print()
        choice = input("  Approve generated tasks? [y/n]: ").strip().lower()
        if choice in ("y", "yes"):
            return True, ""
        feedback = _collect_multiline_input("Task list denied - what should be different?")
        return False, feedback

    decisions = [True] * len(tasks)
    selected_row = 0

    while True:
        _render_task_review_popup(tasks, selected_row, decisions)
        key = _read_keypress()
        if key == "up":
            selected_row = max(0, selected_row - 1)
        elif key == "down":
            selected_row = min(len(tasks) - 1, selected_row + 1)
        elif key in ("left", "right"):
            decisions[selected_row] = not decisions[selected_row]
        elif key == "enter":
            approved = all(decisions)
            print("\033[2J\033[H", end="")
            if approved:
                return True, ""
            feedback = _collect_multiline_input("Task list denied  what should be different#")
            return False, feedback
        elif key == "quit":
            raise KeyboardInterrupt


def _rerun_planning_and_task_creation(state: dict[str, Any], config: dict[str, Any], feedback: str) -> dict[str, Any]:
    TASK_REVIEW_FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASK_REVIEW_FEEDBACK_FILE.write_text(feedback.strip(), encoding="utf-8")
    info("Task list denied. Re-running planning generation and task creation with your feedback.")

    state = set_stage("planning_generation", state)
    run_planning_generation(state, config)
    state = load_state(config)

    run_task_creation(state, config)
    return load_state(config)


# 
# Stage: Feature Context
# 

def run_user_context(state: dict, config: dict) -> None:
    banner("Stage: Feature Context")

    content = _collect_multiline_input("A.I.N. - Describe the feature or bug")
    if not content:
        warn("No description provided. Please re-run and describe your feature.")
        sys.exit(0)

    USER_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_CONTEXT_FILE.write_text(content, encoding="utf-8")
    success("Feature context saved.")
    set_stage("planning_questions", state)


# Stage 3: Planning Questions (Codex)
# 

def run_planning_questions(state: dict, config: dict) -> None:
    banner("Stage: Planning  Brainstorm (Codex)")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # Brief pause so the TUI can fully settle after the user_context
    # suspend/resume cycle before we suspend again for Codex.
    time.sleep(1.5)

    user_ctx = USER_CONTEXT_FILE.read_text(encoding="utf-8") if USER_CONTEXT_FILE.exists() else ""
    arch_ctx = ARCHITECTURE_FILE.read_text(encoding="utf-8") if ARCHITECTURE_FILE.exists() else ""

    BRAINSTORM_CONTEXT_FILE.write_text(
        f"# Brainstorm Context\n\n## Feature Request\n\n{user_ctx}\n\n"
        f"## Architecture Overview\n\n{arch_ctx[:4000]}\n",
        encoding="utf-8",
    )

    codex_cmd = shutil.which("codex") or "codex"
    ctx_path  = str(BRAINSTORM_CONTEXT_FILE).replace("\\", "/")
    out_path  = str(OPEN_QUESTIONS_FILE).replace("\\", "/")
    brainstorm_prompt = (
        f"Read {ctx_path} to understand the feature request and the existing codebase. "
        f"Ask me clarifying questions to remove all ambiguity. "
        f"When we reach full clarity, write a clean Q&A summary to {out_path}."
    )

    info("Suspending TUI  starting Codex brainstorm session ...")
    info(f"Context: {BRAINSTORM_CONTEXT_FILE.relative_to(REPO_ROOT)}")
    info(f"Codex will auto-close once it writes {OPEN_QUESTIONS_FILE.name}.")

    if _RENDERER is not None:
        _RENDERER.suspend()
    try:
        # Fully reset terminal state so Codex's TUI starts clean.
        sys.stdout.write("\033[#25h\033[0m\033[2J\033[H")
        sys.stdout.flush()
        print(f"\033[1;96m   Codex Brainstorm Session \033[0m")
        print(f"\033[96m  Codex will ask clarifying questions.")
        print(f"  Press \033[1mEnter\033[0m\033[96m in Codex to confirm the prompt and start.\033[0m\n")
        proc = subprocess.Popen(
            [codex_cmd, brainstorm_prompt],
            cwd=str(REPO_ROOT),
        )

        def _watch_and_close() -> None:
            """Kill Codex (entire process tree) once OPEN_QUESTIONS.md appears."""
            while proc.poll() is None:
                if OPEN_QUESTIONS_FILE.exists() and OPEN_QUESTIONS_FILE.stat().st_size > 0:
                    time.sleep(2)   # let Codex finish any in-progress writes
                    _kill_tree(proc)
                    return
                time.sleep(1)

        watcher = threading.Thread(target=_watch_and_close, daemon=True)
        watcher.start()
        proc.wait()
    finally:
        sys.stdout.write("\033[#1049l\033[#25h\033[0m\r\n")
        sys.stdout.flush()
        time.sleep(0.5)
        if _RENDERER is not None:
            _RENDERER.resume()

    if not OPEN_QUESTIONS_FILE.exists():
        warn("OPEN_QUESTIONS.md not found. Creating placeholder.")
        OPEN_QUESTIONS_FILE.write_text("# Open Questions\n\nNo clarification needed.\n", encoding="utf-8")
    else:
        success(f"Questions loaded: {OPEN_QUESTIONS_FILE.relative_to(REPO_ROOT)}")

    set_stage("planning_generation", state)


# 
# Stage 4: Planning Generation (Codex)
# 

def _planning_direct_write_prompt() -> str:
    return (
        "Read .ai-pipeline/user_context.md, docs/OPEN_QUESTIONS.md, and docs/architecture.md "
        "to understand the feature request. Write three planning documents directly to disk  "
        "do NOT print them, write the files: docs/PRD.md (headings: # Problem, # Goals, "
        "# Non Goals, # User Stories, # Success Criteria), docs/DESIGN.md (headings: "
        "# Architecture Changes, # Data Model, # API Changes, # UI Changes, # Risks), "
        "docs/FEATURE_SPEC.md (detailed technical spec). Do not ask questions  generate all "
        "three files now."
    )


def _write_chief_planning_package(prompt: str) -> None:
    CHIEF_PRDS_DIR.mkdir(parents=True, exist_ok=True)
    prd_md = (
        "# Planning Generation Context\n\n"
        "Create the planning package for this feature.\n\n"
        "- Write `docs/PRD.md`\n"
        "- Write `docs/DESIGN.md`\n"
        "- Write `docs/FEATURE_SPEC.md`\n\n"
        "Write the files directly to disk. Do NOT output file markers or print the file contents.\n\n"
        "---\n\n"
        f"{prompt}\n\n---\n\n{_planning_direct_write_prompt()}\n"
    )
    CHIEF_PRD_MD.write_text(prd_md, encoding="utf-8")
    CHIEF_PRD_FILE.write_text(
        json.dumps(
            {
                "project": REPO_ROOT.name,
                "description": "Generate PRD, DESIGN, and FEATURE_SPEC planning docs.",
                "userStories": [
                    {"id": "PLAN-001", "title": "Create docs/PRD.md", "passes": False, "inProgress": False},
                    {"id": "PLAN-002", "title": "Create docs/DESIGN.md", "passes": False, "inProgress": False},
                    {"id": "PLAN-003", "title": "Create docs/FEATURE_SPEC.md", "passes": False, "inProgress": False},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _clear_plan_docs() -> list[Path]:
    plan_docs = [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]
    for doc in plan_docs:
        if doc.exists():
            doc.unlink()
    return plan_docs


def _watch_for_files(proc: subprocess.Popen, files: list[Path], *, interval: int = 2) -> None:
    while proc.poll() is None:
        if all(f.exists() and f.stat().st_size > 0 for f in files):
            time.sleep(interval)
            _kill_tree(proc)
            return
        time.sleep(interval)


def _run_suspended_agent(cmd: list[str], title: str, watch_files: list[Path]) -> bool:
    if _RENDERER is not None:
        _RENDERER.suspend()
    try:
        sys.stdout.write("\033[#25h\033[0m\033[2J\033[H")
        sys.stdout.flush()
        print(f"\033[1;96m   {title} \033[0m\n")
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
        threading.Thread(target=_watch_for_files, args=(proc, watch_files), daemon=True).start()
        proc.wait()
    finally:
        sys.stdout.write("\033[#1049l\033[#25h\033[0m\r\n")
        sys.stdout.flush()
        time.sleep(0.5)
        if _RENDERER is not None:
            _RENDERER.resume()
    return all(f.exists() and f.stat().st_size > 0 for f in watch_files)


def _run_planning_in_background(agent_key: str, prompt: str, config: dict[str, Any]) -> bool:
    info(f"Running {_agent_display_name(agent_key, config)} for planning generation in background ...")
    cmd = _resolve_agent_command(agent_key, config)
    agent_cfg = (config.get("agents", {}).get(agent_key) or {})
    command_name = str(agent_cfg.get("command", agent_key)).lower()
    prompt_mode = agent_cfg.get("prompt_mode", "arg")

    # `codex` without `exec` opens its own interactive UI, which steals the terminal.
    # For planning we always want a non-interactive background run that streams into AIN.
    if "codex" in command_name and "exec" not in cmd[1:]:
        cmd = [cmd[0], "exec", *cmd[1:]]
        prompt_mode = "stdin"

    if prompt_mode == "arg":
        cmd = cmd + [prompt]
        rc, output = _run_agent_background(
            cmd,
            agent_name=f"{_agent_display_name(agent_key, config)} (planning)",
            log_slug="planning_generation",
        )
    else:
        rc, output = _run_agent_background(
            cmd,
            agent_name=f"{_agent_display_name(agent_key, config)} (planning)",
            log_slug="planning_generation",
            input_text=prompt,
        )
    if output.strip():
        _parse_and_write_planning_docs(output)
    return rc == 0 and all(
        doc.exists() and doc.stat().st_size > 0
        for doc in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]
    )


def _run_planning_with_chief(prompt: str, config: dict[str, Any]) -> bool:
    _write_chief_planning_package(prompt)
    plan_docs = _clear_plan_docs()
    cmd = _resolve_agent_command("planning_chief", config) + ["--no-retry", "main"]
    info("Suspending TUI  running Chief for planning generation ...")
    return _run_suspended_agent(cmd, "Planning Generation (Chief)", plan_docs)


def _run_planning_fallback_claude(prompt: str) -> None:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        warn("claude not found  no further planning fallback available.")
        return
    _, output = _run_agent_background(
        [claude_bin, "--print"],
        agent_name="Claude (planning)",
        log_slug="planning_generation_claude",
        input_text=prompt,
    )
    if output.strip():
        _parse_and_write_planning_docs(output)


def run_planning_generation(state: dict, config: dict) -> None:
    mode = get_selected_mode(state, config)
    agent_key = resolve_stage_agent_key("planning_generation", state, config)
    banner(f"Stage: Planning  Generation [mode={mode} agent={agent_key}]")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = PROMPTS_DIR / "planning_generation_prompt.md"
    ctx_files = [
        f for f in [
            OPEN_QUESTIONS_FILE, OPEN_ANSWERS_FILE,
            ARCHITECTURE_FILE, USER_CONTEXT_FILE,
            TASK_REVIEW_FEEDBACK_FILE,
        ] if f.exists()
    ]
    prompt = build_prompt(prompt_file, *ctx_files)
    planning_prompt = f"{prompt}\n\n---\n\n{_planning_direct_write_prompt()}"
    _clear_plan_docs()

    planning_ok = False
    if mode in ("default", "codex_only"):
        planning_ok = _run_planning_in_background(agent_key, planning_prompt, config)
        if not planning_ok:
            warn("Primary planning agent did not write all planning docs. Falling back to claude --print ...")
            _run_planning_fallback_claude(prompt)
    elif mode == "claude_chief_only":
        planning_ok = _run_planning_with_chief(prompt, config)
        if not planning_ok:
            warn("Chief did not write all planning docs. Falling back to claude --print ...")
            _run_planning_fallback_claude(prompt)
    else:
        raise RuntimeError(f"Unsupported planning mode '{mode}'.")

    #  Write stubs for any still-missing files 
    for doc, headings, name in [
        (PRD_FILE,          PRD_HEADINGS,    "PRD.md"),
        (DESIGN_FILE,       DESIGN_HEADINGS, "DESIGN.md"),
        (FEATURE_SPEC_FILE, [],              "FEATURE_SPEC.md"),
    ]:
        if not doc.exists():
            warn(f"{name} not found  creating stub. Edit it before continuing.")
            stub = "\n\n".join(f"{h}\n\n(Fill in)" for h in headings) if headings else "# Feature Specification\n\n(Fill in)"
            doc.write_text(stub, encoding="utf-8")

    missing_prd = validate_headings(PRD_FILE, PRD_HEADINGS)
    if missing_prd:
        warn(f"PRD.md is missing headings: {missing_prd}")
        warn("Edit docs/PRD.md and re-run to continue.")
        sys.exit(0)

    missing_design = validate_headings(DESIGN_FILE, DESIGN_HEADINGS)
    if missing_design:
        warn(f"DESIGN.md is missing headings: {missing_design}")
        warn("Edit docs/DESIGN.md and re-run to continue.")
        sys.exit(0)

    success("Planning documents validated.")
    set_stage("task_creation", state)


def _strip_fences(content: str) -> str:
    """Strip markdown code fences from file content written by agents."""
    lines = content.strip().splitlines()
    if lines and lines[0].startswith(chr(96)*3):
        lines = lines[1:]
    if lines and lines[-1].strip() == chr(96)*3:
        lines = lines[:-1]
    return chr(10).join(lines).strip()


def _safe_doc_path(filename: str) -> Path:
    """Resolve a docs-relative filename and ensure it stays within DOCS_DIR."""
    target = (DOCS_DIR / filename.strip()).resolve()
    try:
        target.relative_to(DOCS_DIR.resolve())
    except ValueError:
        raise RuntimeError(
            f"Path traversal blocked: '{filename}' would escape the docs directory."
        )
    return target


def _parse_and_write_planning_docs(output: str) -> None:
    pattern = re.compile(
        r"<!--\s*FILE:\s*(?:docs/)?(\S+?)\s*-->(.*?)<!--\s*END:\s*(?:docs/)?\S+?\s*-->",
        re.DOTALL,
    )
    matches = list(pattern.finditer(output))
    if matches:
        for m in matches:
            try:
                target = _safe_doc_path(m.group(1))
            except RuntimeError as e:
                warn(str(e))
                continue
            target.write_text(_strip_fences(m.group(2)), encoding="utf-8")
            success(f"Written  {_display_path(target)}")
    else:
        warn("Could not parse separate files. Writing raw output to PRD.md.")
        PRD_FILE.write_text(output, encoding="utf-8")
        if not DESIGN_FILE.exists():
            DESIGN_FILE.write_text(
                "# Architecture Changes\n\n# Data Model\n\n# API Changes\n\n# UI Changes\n\n# Risks\n",
                encoding="utf-8",
            )
        if not FEATURE_SPEC_FILE.exists():
            FEATURE_SPEC_FILE.write_text("# Feature Specification\n\n", encoding="utf-8")

# 
# Stage 5: Task Creation (Chief)
# 

CHIEF_DIR      = REPO_ROOT / ".chief"
CHIEF_PRDS_DIR = CHIEF_DIR / "prds" / "main"
CHIEF_PRD_FILE = CHIEF_PRDS_DIR / "prd.json"
CHIEF_PRD_MD   = CHIEF_PRDS_DIR / "prd.md"


def _write_chief_prd(prompt: str) -> None:
    """Write .chief/prds/main/prd.json and prd.md from the task-creation prompt."""
    CHIEF_PRDS_DIR.mkdir(parents=True, exist_ok=True)

    prd_md = (
        "# Task Creation Context\n\n"
        "Read the planning documents below and produce two files:\n\n"
        "- Write `docs/TASKS.md`  a dependency-ordered markdown checkbox task list\n"
        "- Write `docs/TASK_GRAPH.json`  a JSON dependency graph\n\n"
        "Write the files directly to disk using your file-editing tools.\n"
        "Do NOT output file markers or print the content to stdout.\n\n"
        "---\n\n"
        + prompt
    )
    CHIEF_PRD_MD.write_text(prd_md, encoding="utf-8")

    prd = {
        "project": REPO_ROOT.name,
        "description": (
            "Analyse the planning documents in prd.md and produce "
            "docs/TASKS.md and docs/TASK_GRAPH.json."
        ),
        "userStories": [
            {
                "id": "US-001",
                "title": "Create docs/TASKS.md",
                "description": (
                    "As a developer, I need docs/TASKS.md containing a "
                    "dependency-ordered markdown checkbox task list."
                ),
                "acceptanceCriteria": [
                    "File docs/TASKS.md exists",
                    "File contains at least one checkbox task in '- [ ] ...' format",
                ],
                "priority": 1,
                "passes": False,
                "inProgress": False,
            },
            {
                "id": "US-002",
                "title": "Create docs/TASK_GRAPH.json",
                "description": (
                    "As a developer, I need docs/TASK_GRAPH.json containing "
                    "valid JSON with id, description, depends_on, status, "
                    "files_affected, and completed_at for every task."
                ),
                "acceptanceCriteria": [
                    "File docs/TASK_GRAPH.json exists",
                    "File contains valid JSON with a 'tasks' array",
                ],
                "priority": 2,
                "passes": False,
                "inProgress": False,
            },
        ],
    }
    CHIEF_PRD_FILE.write_text(json.dumps(prd, indent=2), encoding="utf-8")
    success(f"Written  {CHIEF_PRD_FILE.relative_to(REPO_ROOT)}")
    success(f"Written  {CHIEF_PRD_MD.relative_to(REPO_ROOT)}")


def _build_task_graph_from_tasks_md() -> None:
    if not TASKS_FILE.exists():
        return
    content = TASKS_FILE.read_text(encoding="utf-8")
    tasks = []
    for i, m in enumerate(re.finditer(r"- \[( |x)\] (.+)", content), start=1):
        tasks.append({
            "id": i,
            "description": m.group(2).strip(),
            "depends_on": [i - 1] if i > 1 else [],
            "status": "completed" if m.group(1) == "x" else "pending",
            "files_affected": [],
            "completed_at": None,
        })
    graph = {
        "tasks": tasks,
        "parallel_groups": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(tasks),
        "completed": sum(1 for t in tasks if t["status"] == "completed"),
    }
    TASK_GRAPH_FILE.write_text(json.dumps(graph, indent=2), encoding="utf-8")


def _run_chief_background(config: dict) -> tuple[bool, str]:
    """Run chief non-interactively in the background, streaming output to AGENT.OUTPUT.

    Returns (success, captured_output).
    """
    agent_cfg  = config.get("agents", {}).get("task_creation", {})
    command    = agent_cfg.get("command", "chief")
    extra_args = agent_cfg.get("args", [])

    resolved = shutil.which(command)
    if not resolved:
        warn(f"chief not found on PATH. Falling back to Codex.")
        return False, ""

    cmd = [resolved] + extra_args + ["--no-retry", "main"]
    info("Running chief in background (output in AGENT.OUTPUT panel) ...")
    rc, output = _run_agent_background(cmd, agent_name="Chief", log_slug="task_creation")
    if rc != 0:
        warn(f"chief exited with code {rc}.")
        return False, output
    return True, output


def _call_agent_live(agent_name: str, prompt: str, config: dict, *, log_slug: str) -> str:
    """Run an agent while streaming output into AGENT.OUTPUT, then return captured text."""
    agent_cfg = config.get("agents", {}).get(agent_name, {})
    command = agent_cfg.get("command", agent_name)
    extra_args = agent_cfg.get("args", [])
    model = agent_cfg.get("model")
    prompt_mode = agent_cfg.get("prompt_mode", "stdin")

    resolved = shutil.which(command)
    if resolved:
        command = resolved

    cmd = [command] + extra_args
    if model:
        cmd += ["--model", model]

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{agent_name}_last_prompt.txt").write_text(prompt, encoding="utf-8")

    display = _agent_display_name(agent_name, config)
    if prompt_mode == "arg":
        rc, output = _run_agent_background(
            cmd + [prompt],
            agent_name=display,
            log_slug=log_slug,
        )
    else:
        rc, output = _run_agent_background(
            cmd,
            agent_name=display,
            log_slug=log_slug,
            input_text=prompt,
        )

    if rc != 0:
        warn(f"Agent {agent_name} exited {rc}")
    return output


def _persist_stage_fallback(
    stage_name: str,
    state: dict[str, Any],
    config: dict[str, Any],
    *,
    command: str = "codex",
    args: list[str] | None = None,
    prompt_mode: str = "stdin",
) -> None:
    state.setdefault("fallback_mode", {})
    state["fallback_mode"][stage_name] = command
    config.setdefault("agents", {}).setdefault(stage_name, {})
    config["agents"][stage_name]["command"] = command
    config["agents"][stage_name]["args"] = args or ["exec"]
    config["agents"][stage_name]["prompt_mode"] = prompt_mode
    save_state(state)
    save_config(config)


def run_task_creation(state: dict, config: dict) -> None:
    mode = get_selected_mode(state, config)
    agent_key = resolve_stage_agent_key("task_creation", state, config)
    banner(f"Stage: Task Creation [mode={mode} agent={agent_key}]")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = PROMPTS_DIR / "task_creation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

    ctx_files = [
        f
        for f in [
            PRD_FILE,
            DESIGN_FILE,
            FEATURE_SPEC_FILE,
            ARCHITECTURE_FILE,
            TASK_REVIEW_FEEDBACK_FILE,
        ]
        if f.exists()
    ]
    prompt = build_prompt(prompt_file, *ctx_files)
    live_task_output = _EMITTER is not None

    if mode == "codex_only":
        output = (
            _call_agent_live(agent_key, prompt, config, log_slug="task_creation")
            if live_task_output
            else call_agent(agent_key, prompt, config)
        )
        if not output.strip():
            raise RuntimeError("Codex task creation returned empty output.")
        _parse_and_write_task_artifacts(output)
        if not TASKS_FILE.exists():
            TASKS_FILE.write_text(output, encoding="utf-8")
    else:
        chief_cfg = config.get("agents", {}).get(agent_key, {})
        chief_cmd_name = chief_cfg.get("command", "chief")
        chief_cmd = shutil.which(chief_cmd_name) if str(chief_cmd_name).lower() == "chief" else None
        primary_error: RuntimeError | None = None

        if chief_cmd:
            step(1, 2, "Writing chief PRD ...")
            _write_chief_prd(prompt)
            for _stale in [TASKS_FILE, TASK_GRAPH_FILE]:
                if _stale.exists():
                    _stale.unlink()

            if live_task_output:
                step(2, 2, "Running chief in background (streaming to AGENT.OUTPUT) ...")
                _run_chief_background(config)
            else:
                step(2, 2, "Running chief (suspending TUI) - will auto-start and auto-close ...")
                if _RENDERER is not None:
                    _RENDERER.suspend()
                try:
                    sys.stdout.write("\033[#25h\033[0m\033[2J\033[H")
                    sys.stdout.flush()
                    print("\033[1;96m  Chief Task Creation\033[0m")
                    print("\033[96m  Chief is starting - auto-pressing 's' in a moment.\033[0m\n")
                    proc = subprocess.Popen(
                        [chief_cmd] + chief_cfg.get("args", []) + ["--no-retry", "main"],
                        cwd=str(REPO_ROOT),
                    )

                    def _send_s() -> None:
                        time.sleep(3)
                        if proc.poll() is None:
                            subprocess.run(
                                [
                                    "powershell",
                                    "-NoProfile",
                                    "-Command",
                                    "$s = New-Object -ComObject WScript.Shell; $s.SendKeys('s')",
                                ],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )

                    threading.Thread(target=_send_s, daemon=True).start()

                    def _watch_chief() -> None:
                        while proc.poll() is None:
                            if validate_tasks_file(TASKS_FILE):
                                time.sleep(2)
                                _kill_tree(proc)
                                return
                            time.sleep(2)

                    threading.Thread(target=_watch_chief, daemon=True).start()
                    proc.wait()
                finally:
                    sys.stdout.write("\033[#1049l\033[#25h\033[0m\r\n")
                    sys.stdout.flush()
                    time.sleep(0.5)
                    if _RENDERER is not None:
                        _RENDERER.resume()

                if validate_tasks_file(TASKS_FILE):
                    for line in TASKS_FILE.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            _emit(AgentOutput(ts=_now_iso(), line=line, agent="Chief"))
        else:
            try:
                output = (
                    _call_agent_live(agent_key, prompt, config, log_slug="task_creation")
                    if live_task_output
                    else call_agent(agent_key, prompt, config)
                )
                if output.strip():
                    _parse_and_write_task_artifacts(output)
                    if not TASKS_FILE.exists():
                        TASKS_FILE.write_text(output, encoding="utf-8")
            except RuntimeError as e:
                primary_error = e

        if not validate_tasks_file(TASKS_FILE):
            if mode == "claude_chief_only":
                raise RuntimeError("Chief did not produce a valid TASKS.md in claude_chief_only mode.")
            if primary_error is not None:
                warn(f"Primary task creation agent failed ({primary_error}). Falling back to Codex ...")
            if chief_cmd:
                warn("Chief did not produce a valid TASKS.md. Falling back to Codex ...")
            else:
                info("Chief not found. Running Codex for task creation ...")
            _persist_stage_fallback("task_creation", state, config)
            output = (
                _call_agent_live("task_creation_codex", prompt, config, log_slug="task_creation_fallback")
                if live_task_output
                else call_agent("task_creation_codex", prompt, config)
            )
            if not output.strip():
                raise RuntimeError("Codex task creation returned empty output.")
            _parse_and_write_task_artifacts(output)
            if not TASKS_FILE.exists():
                TASKS_FILE.write_text(output, encoding="utf-8")

    if TASKS_FILE.exists() and not TASK_GRAPH_FILE.exists():
        _build_task_graph_from_tasks_md()

    if not validate_tasks_file(TASKS_FILE):
        raise RuntimeError("TASKS.md does not contain valid checkbox tasks.")
    if not validate_task_graph(TASK_GRAPH_FILE):
        raise RuntimeError("TASK_GRAPH.json is invalid or empty.")

    data = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
    count = len(data.get("tasks", []))
    success(f"Task graph: {count} tasks created.")
    set_stage("waiting_approval", state)
def _parse_and_write_task_artifacts(output: str) -> None:
    pattern = re.compile(
        r"<!--\s*FILE:\s*(?:docs/)?(\S+?)\s*-->(.*?)<!--\s*END:\s*(?:docs/)?\S+?\s*-->",
        re.DOTALL,
    )
    matches = list(pattern.finditer(output))
    if matches:
        for m in matches:
            try:
                target = _safe_doc_path(m.group(1))
            except RuntimeError as e:
                warn(str(e))
                continue
            content = _strip_fences(m.group(2))
            # For JSON files, validate and pretty-print
            if target.suffix == ".json":
                try:
                    content = json.dumps(json.loads(content), indent=2)
                except json.JSONDecodeError:
                    pass
            target.write_text(content, encoding="utf-8")
            success(f"Written  {_display_path(target)}")
    else:
        json_match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                TASK_GRAPH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except json.JSONDecodeError:
                pass
        md_match = re.search(r"```markdown\s*(.*?)\s*```", output, re.DOTALL)
        if md_match:
            TASKS_FILE.write_text(md_match.group(1), encoding="utf-8")
        else:
            TASKS_FILE.write_text(output, encoding="utf-8")

    if not TASK_GRAPH_FILE.exists():
        _build_task_graph_from_tasks_md()


# 
# Stage 6: Approval Gate
# 

def run_waiting_approval(state: dict, config: dict) -> None:
    banner("Stage: Waiting for Approval")

    if PLANNING_APPROVED_FLAG.exists():
        success("Planning approved. Advancing to implementation.")
        set_stage("implementation", state)
        return

    while True:
        _emit(AwaitingApproval(run_id=_RUN_ID, stage_id="waiting_approval"))
        info("Task creation completed. Review tasks before implementation continues.")

        tasks = _extract_tasks_for_review()
        if not tasks:
            warn("No tasks found to review. Falling back to manual approval flow.")
            print(f"\n{C.BOLD}{C.YELLOW}  APPROVAL REQUIRED{C.RESET}")
            print()
            print("  Review these artifacts before implementation begins:")
            print()
            for doc in [ARCHITECTURE_FILE, PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE, TASKS_FILE, TASK_GRAPH_FILE]:
                if doc.exists():
                    print(f"    {C.CYAN}{doc.relative_to(REPO_ROOT)}{C.RESET}")
            print()
            print(f"  Approve with:  {C.GREEN}ain --approve{C.RESET}")
            print()
            sys.exit(0)

        if _RENDERER is not None:
            _RENDERER.suspend()
        try:
            approved, feedback = _review_tasks_with_popup(tasks)
        finally:
            if _RENDERER is not None:
                _RENDERER.resume()

        if approved:
            APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
            approved_at = datetime.now(timezone.utc).isoformat()
            PLANNING_APPROVED_FLAG.write_text(f"Approved: {approved_at}\n", encoding="utf-8")
            _emit(ApprovalReceived(run_id=_RUN_ID, actor="user", at=approved_at))
            success("Tasks approved. Advancing to implementation.")
            set_stage("implementation", state)
            if TASK_REVIEW_FEEDBACK_FILE.exists():
                TASK_REVIEW_FEEDBACK_FILE.unlink()
            return

        state = _rerun_planning_and_task_creation(state, config, feedback)

# 
# Git integration
# 

def create_git_branch(state: dict, config: dict) -> str | None:
    if not config["git"]["auto_branch"]:
        return None
    ts     = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = config["git"]["branch_prefix"]
    branch = f"{prefix}-{ts}"
    try:
        r = run_command(["git", "rev-parse", "--git-dir"], capture=True)
        if r.returncode != 0:
            warn("Not a git repo. Skipping branch creation.")
            return None
        run_command(["git", "checkout", "-b", branch])
        success(f"Branch: {branch}")
        state["branch"] = branch
        save_state(state)
        return branch
    except Exception as e:
        warn(f"Could not create git branch: {e}")
        return None


def commit_implementation(state: dict, config: dict) -> None:
    if not config["git"]["auto_commit"]:
        info("Auto-commit is disabled. Skipping commit.")
        return
    try:
        status = run_command_output(["git", "status", "--porcelain"])
        if not status:
            info("No changes to commit.")
            return
        run_command(["git", "add", "."])
        # Respect .gitignore for generated docs even if they were tracked earlier.
        task_graph_rel = str(TASK_GRAPH_FILE.relative_to(REPO_ROOT)).replace("\\", "/")
        ignored_probe = run_command(
            ["git", "check-ignore", "--no-index", task_graph_rel],
            capture=True,
        )
        if ignored_probe.returncode == 0:
            run_command(["git", "reset", "HEAD", "--", task_graph_rel], capture=True)
        msg = (
            f"feat: AI pipeline implementation\n\n"
            f"Generated by A.I.N. Pipeline\n"
            f"Branch: {state.get('branch', 'unknown')}"
        )
        run_command(["git", "commit", "-m", msg])
        success("Changes committed.")
    except Exception as e:
        warn(f"Git commit failed: {e}")


# 
# Workspace cleanup
# 

def _clean_files() -> list[Path]:
    return [
        DOCS_DIR / "architecture.md",
        DOCS_DIR / "PRD.md",
        DOCS_DIR / "DESIGN.md",
        DOCS_DIR / "FEATURE_SPEC.md",
        DOCS_DIR / "OPEN_QUESTIONS.md",
        DOCS_DIR / "OPEN_ANSWERS.md",
        DOCS_DIR / "TASKS.md",
        DOCS_DIR / "TASK_GRAPH.json",
        DOCS_DIR / "IMPLEMENTATION_LOG.md",
        DOCS_DIR / "VERIFICATION_REPORT.md",
        USER_CONTEXT_FILE,
        BRAINSTORM_CONTEXT_FILE,
    ]


def _clean_dirs() -> list[Path]:
    return [
        SCAN_DIR,
        LOGS_DIR,
        APPROVALS_DIR,
        PIPELINE_DIR / "state",
        REPO_ROOT / ".chief" / "prds",
    ]


def clean_workspace(silent: bool = False) -> None:
    """Delete all per-run generated files and reset pipeline state to idle.

    Preserves: config.json, prompts/, CLAUDE.md, and all source code.
    Called automatically after a successful auto-commit, or manually via --clean.
    """
    removed: list[str] = []

    for f in _clean_files():
        if f.exists():
            f.unlink()
            removed.append(str(f.relative_to(REPO_ROOT)))

    for d in _clean_dirs():
        if d.exists():
            shutil.rmtree(d)
            removed.append(str(d.relative_to(REPO_ROOT)) + "/")

    # Reset state to idle (keeps config intact)
    save_state(_default_state(load_config()))

    if not silent:
        if removed:
            for item in removed:
                info(f"Removed: {item}")
        else:
            info("Nothing to clean.")
        success("Workspace cleaned. Ready for next implementation.")

# -------------------------------------------------------------
# Token-limit fallback helpers
# -------------------------------------------------------------

_TOKEN_LIMIT_PHRASES = [
    "context window", "token limit", "maximum context", "too long",
    "prompt is too", "input too long", "context length", "max_tokens",
    "context_length_exceeded", "rate limit", "overloaded",
    "reduce the length",
]


def is_token_limit_error(output: str, returncode: int) -> bool:
    """Return True if the agent output/exit looks like a context or token-limit error."""
    if returncode == 0:
        return False
    combined = output.lower()
    return any(phrase in combined for phrase in _TOKEN_LIMIT_PHRASES)


def rollback_implementation_files() -> list[str]:
    """Roll back unstaged changes introduced by a failed task via git checkout."""
    rolled_back: list[str] = []
    try:
        status_out = run_command_output(["git", "status", "--porcelain"])
        for line in status_out.splitlines():
            if len(line) > 3 and line[:2] in (" M", "M ", "A ", " A"):
                fpath = line[3:].strip().strip('"')
                result = run_command(["git", "checkout", "--", fpath], capture=True)
                if result.returncode == 0:
                    rolled_back.append(fpath)
    except RuntimeError as e:
        warn(f"Rollback failed: {e}")
    return rolled_back


def invoke_codex_fallback(task_prompt: str, config: dict) -> str:
    """Invoke codex in full-auto implementation mode as a fallback for a failed task."""
    fallback_cfg = config.get("agents", {}).get("implementation_fallback", {})
    cmd = fallback_cfg.get("command", "codex")
    if not shutil.which(cmd):
        raise RuntimeError(
            "Codex fallback requested but 'codex' is not available. "
            "Install codex and check 'implementation_fallback' in .ai-pipeline/config.json."
        )
    info("Invoking codex (implementation fallback) ...")
    if _EMITTER is not None:
        return _call_agent_live("implementation_fallback", task_prompt, config, log_slug="implementation_fallback")
    return call_agent("implementation_fallback", task_prompt, config)


def notify_fallback_and_get_decision(context: str, timeout_secs: int = 30) -> bool:
    """Inform the user of a token-limit event and ask whether to switch to codex.

    Returns True to use the fallback, False to skip the task.
    """
    warn("Token/context limit detected for this task.")
    print(f"\n{C.YELLOW}  The implementation agent hit a context or token limit.{C.RESET}")
    print(f"  {context}")
    print()
    print(f"  {C.GREEN}[f]{C.RESET}  Use codex fallback agent for this task")
    print(f"  {C.YELLOW}[s]{C.RESET}  Skip this task and continue")
    print()
    try:
        choice = input("  Choice [f/s] (default: f): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return choice != "s"


def _call_agent_with_fallback(
    agent_name: str,
    prompt: str,
    state: dict[str, Any],
    config: dict,
) -> str:
    """Call an implementation agent; on token-limit failure, offer codex fallback.

    Wraps ``call_agent()`` with:
    - stderr / exit-code inspection for token-limit signals
    - user prompt to roll back and switch to the codex fallback agent
    """
    agent_cfg   = config.get("agents", {}).get(agent_name, {})
    command     = agent_cfg.get("command", agent_name)
    extra_args  = agent_cfg.get("args", [])
    model       = agent_cfg.get("model")
    prompt_mode = agent_cfg.get("prompt_mode", "stdin")

    resolved = shutil.which(command)
    if resolved:
        command = resolved

    cmd = [command] + extra_args
    if model:
        cmd += ["--model", model]

    info(f"Invoking {agent_cfg.get('command', agent_name)} ({agent_name}) ...")
    _log(f"AGENT CALL: {agent_name} via {command}\n\t(prompt_mode={prompt_mode})")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{agent_name}_last_prompt.txt").write_text(prompt, encoding="utf-8")

    try:
        if _EMITTER is not None:
            if prompt_mode == "arg":
                rc, output = _run_agent_background(
                    cmd + [prompt],
                    agent_name=_agent_display_name(agent_name, config),
                    log_slug="implementation",
                )
            else:
                rc, output = _run_agent_background(
                    cmd,
                    agent_name=_agent_display_name(agent_name, config),
                    log_slug="implementation",
                    input_text=prompt,
                )
            stderr = ""
            returncode = rc
        else:
            if prompt_mode == "arg":
                result = run_command(cmd + [prompt], capture=True, timeout=600)
            else:
                result = run_command(cmd, capture=True, input_text=prompt, timeout=600)
            output = result.stdout or ""
            stderr = (result.stderr or "").strip()
            returncode = result.returncode
    except FileNotFoundError:
        raise RuntimeError(
            f"Agent command not found: '{command}'. "
            f"Edit .ai-pipeline/config.json to configure the '{agent_name}' agent."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agent '{agent_name}' timed out after 600 seconds.")

    (LOGS_DIR / f"{agent_name}_last_output.txt").write_text(output, encoding="utf-8")

    if returncode != 0:
        warn(f"Agent {agent_name} exited {returncode}")
        _log(f"AGENT STDERR: {stderr[:500]}")

        # Exit code 1 (token exhaustion or any error)  auto-trigger codex fallback
        info("Auto-switching to codex fallback ...")
        rolled = rollback_implementation_files()
        if rolled:
            for f in rolled:
                info(f"  Rolled back: {f}")
        _persist_stage_fallback("implementation", state, config)
        return invoke_codex_fallback(prompt, config)

    return output


# 
# Parallel task execution helpers
# 

def _build_task_prompt(task: dict, prompt_file: Path) -> str:
    """Construct the full prompt for a single task."""
    base_prompt = prompt_file.read_text(encoding="utf-8")
    context     = read_context_files(ARCHITECTURE_FILE, DESIGN_FILE, TASKS_FILE)
    return (
        f"{base_prompt}\n\n---\n## Current Task\n\n"
        f"**Task {task['id']}:** {task['description']}\n\n"
        f"**Dependencies:** {task.get('depends_on') or 'none'}\n\n"
        f"---\n## Reference Documents\n\n{context}"
    )


def _run_one_task(
    task: dict,
    prompt_file: Path,
    state: dict,
    config: dict,
    task_data: dict,
    log_lines: list,
) -> bool:
    """Execute a single task and update shared state.  Thread-safe.  Returns True on success."""
    task_id     = task["id"]
    description = task["description"]
    mode = get_selected_mode(state, config)
    agent_key = resolve_stage_agent_key("implementation", state, config)
    agent_cfg = config.get("agents", {}).get(agent_key, {})
    agent_name = agent_cfg.get("command", agent_key)

    _emit(TaskStarted(
        task_id=str(task_id),
        description=description,
        agent=agent_name,
        started_at=_now_iso(),
    ))
    info(f"   Task {task_id}: {description}")

    t0 = datetime.now(timezone.utc)
    try:
        task_prompt = _build_task_prompt(task, prompt_file)
        if mode == "default" and agent_key == "implementation":
            _call_agent_with_fallback(agent_key, task_prompt, state, config)
        else:
            if _EMITTER is not None:
                _call_agent_live(agent_key, task_prompt, config, log_slug=f"implementation_task_{task_id}")
            else:
                call_agent(agent_key, task_prompt, config)

        duration_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        _emit(TaskCompleted(
            task_id=str(task_id),
            description=description,
            duration_ms=duration_ms,
            ended_at=_now_iso(),
        ))
        success(f"   Task {task_id} complete ({duration_ms // 1000}s)")

        with _GRAPH_LOCK:
            for t in task_data["tasks"]:
                if t["id"] == task_id:
                    t["status"]       = "completed"
                    t["completed_at"] = _now_iso()
            task_data["completed"] = sum(
                1 for t in task_data["tasks"] if t.get("status") == "completed"
            )
            TASK_GRAPH_FILE.write_text(json.dumps(task_data, indent=2), encoding="utf-8")

        _mark_task_complete_in_md(description)
        log_lines.append(f"## Task {task_id}: {description}")
        log_lines.append(f"Status: completed")
        log_lines.append(f"Completed: {_now_iso()}")
        log_lines += ["", "---", ""]
        return True

    except (RuntimeError, subprocess.CalledProcessError) as e:
        duration_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        _emit(TaskFailed(
            task_id=str(task_id),
            description=description,
            error=str(e),
            ended_at=_now_iso(),
        ))
        error(f"   Task {task_id} failed: {e}")
        _log(f"Task {task_id} failed: {e}")
        log_lines.append(f"## Task {task_id}: {description}")
        log_lines.append(f"Status: FAILED")
        log_lines.append(f"Error: {e}")
        log_lines += ["", "---", ""]
        return False


def _execute_parallel_groups(
    task_data: dict,
    prompt_file: Path,
    state: dict,
    config: dict,
    log_lines: list,
) -> None:
    """Run tasks grouped by parallel_groups, executing each group concurrently."""
    tasks_by_id = {t["id"]: t for t in task_data["tasks"]}
    parallel_groups: list[list] = task_data.get("parallel_groups", [])

    # Flatten the groups to a set for fast lookup; tasks not in any group run sequentially.
    in_group: set = {tid for group in parallel_groups for tid in group}
    dep_statuses: dict = {t["id"]: t["status"] for t in task_data["tasks"]}

    # Run grouped tasks in parallel, group by group.
    for group in parallel_groups:
        runnable = [
            tasks_by_id[tid]
            for tid in group
            if tid in tasks_by_id
            and tasks_by_id[tid].get("status") == "pending"
            and not any(dep_statuses.get(d) != "completed"
                        for d in tasks_by_id[tid].get("depends_on", []))
        ]
        if not runnable:
            continue

        max_workers = min(len(runnable), 4)  # cap at 4 concurrent agents
        info(f"  Running {len(runnable)} tasks in parallel (max {max_workers} workers) ...")

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_one_task, task, prompt_file, state, config, task_data, log_lines): task
                for task in runnable
            }
            for future in as_completed(futures):
                task = futures[future]
                succeeded = future.result()
                dep_statuses[task["id"]] = "completed" if succeeded else "failed"

    # Run any tasks that were not covered by parallel_groups sequentially.
    for task in task_data["tasks"]:
        if task["id"] in in_group or task.get("status") != "pending":
            continue
        blocked = [d for d in task.get("depends_on", []) if dep_statuses.get(d) != "completed"]
        if blocked:
            warn(f"    Task {task['id']} blocked by {blocked}. Skipping.")
            continue
        dep_statuses[task["id"]] = (
            "completed" if _run_one_task(task, prompt_file, state, config, task_data, log_lines)
            else "failed"
        )


# 
# Stage 7: Implementation (Claude)
# 

def run_implementation(state: dict, config: dict) -> None:
    mode = get_selected_mode(state, config)
    agent_key = resolve_stage_agent_key("implementation", state, config)
    banner(f"Stage: Implementation [mode={mode} agent={agent_key}]")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    create_git_branch(state, config)

    if not TASK_GRAPH_FILE.exists():
        raise RuntimeError("TASK_GRAPH.json not found. Run task_creation stage first.")

    task_data = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
    tasks     = task_data.get("tasks", [])
    pending   = [t for t in tasks if t.get("status") == "pending"]

    if not pending:
        success("All tasks already completed.")
        set_stage("validation", state)
        return

    info(f"Tasks: {len(tasks)} total | {len(pending)} pending")
    print()

    prompt_file = PROMPTS_DIR / "implementation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

    log_lines: list[str] = [
        "# Implementation Log",
        f"\nStarted: {datetime.now(timezone.utc).isoformat()}",
        f"Branch: {state.get('branch', 'unknown')}",
        "",
    ]

    parallel_groups = task_data.get("parallel_groups", [])
    if parallel_groups:
        info(f"Parallel groups detected ({len(parallel_groups)} groups)  running concurrently.")
        _execute_parallel_groups(task_data, prompt_file, state, config, log_lines)
    else:
        # No parallel groups  run sequentially via the shared helper.
        dep_statuses = {t["id"]: t["status"] for t in tasks}
        for task in pending:
            blocked = [d for d in task.get("depends_on", [])
                       if dep_statuses.get(d) != "completed"]
            if blocked:
                warn(f"    Task {task['id']} blocked by {blocked}. Skipping.")
                continue
            succeeded = _run_one_task(task, prompt_file, state, config, task_data, log_lines)
            dep_statuses[task["id"]] = "completed" if succeeded else "failed"

    IMPLEMENTATION_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    success(f"Log  {IMPLEMENTATION_LOG_FILE.relative_to(REPO_ROOT)}")
    set_stage("validation", state)


def _mark_task_complete_in_md(description: str) -> None:
    with _GRAPH_LOCK:
        if not TASKS_FILE.exists():
            return
        content = TASKS_FILE.read_text(encoding="utf-8")
        snippet = re.escape(description[:60])
        new = re.sub(r"- \[ \] " + snippet, "- [x] " + description[:60], content, count=1)
        TASKS_FILE.write_text(new, encoding="utf-8")

# 
# Stage 8: Validation
# 

def detect_validation_commands(tracked_files: list[str]) -> list[list[str]]:
    files_set = set(tracked_files)
    cmds: list[list[str]] = []

    if "artisan" in files_set or any(f.endswith(".php") for f in tracked_files):
        cmds.append(["php", "artisan", "test"])
        if Path(REPO_ROOT / "phpstan.neon").exists():
            cmds.append(["./vendor/bin/phpstan", "analyse"])

    if "package.json" in files_set:
        try:
            pkg     = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                cmds.append(["npm", "test", "--", "--passWithNoTests"])
            if "lint" in scripts:
                cmds.append(["npm", "run", "lint"])
            if "build" in scripts:
                cmds.append(["npm", "run", "build"])
        except Exception:
            pass

    if "pyproject.toml" in files_set or "requirements.txt" in files_set:
        if any(Path(REPO_ROOT / f).exists() for f in ["pytest.ini", "pyproject.toml", "setup.cfg"]):
            pytest_cmd = _resolve_pytest_command()
            if pytest_cmd is not None:
                cmds.append(pytest_cmd)

    if "go.mod" in files_set:
        cmds.append(["go", "test", "./..."])
        cmds.append(["go", "vet", "./..."])

    if "Cargo.toml" in files_set:
        cmds.append(["cargo", "test"])

    return cmds


def _resolve_pytest_command() -> list[str] | None:
    candidates: list[list[str]] = []
    local_venv = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if local_venv.exists():
        candidates.append([str(local_venv)])

    if sys.executable:
        candidates.append([sys.executable])

    py_launcher = shutil.which("py")
    if py_launcher:
        candidates.append([py_launcher, "-3"])

    seen: set[tuple[str, ...]] = set()
    for base_cmd in candidates:
        key = tuple(base_cmd)
        if key in seen:
            continue
        seen.add(key)
        try:
            probe = run_command(base_cmd + ["-m", "pytest", "--version"], capture=True, timeout=10)
        except Exception:
            continue
        if probe.returncode == 0:
            return base_cmd + ["-m", "pytest", "--tb=short", "-q"]

    return None


def _summarize_validation_failure(result: subprocess.CompletedProcess, cmd_str: str) -> str:
    stderr_lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    stdout_lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    detail = stderr_lines[0] if stderr_lines else (stdout_lines[-1] if stdout_lines else f"exit {result.returncode}")
    detail = _strip_ansi(detail)
    if len(detail) > 120:
        detail = detail[:117] + "..."
    return f"{Path(cmd_str.split()[0]).name}: {detail}"


def run_validation(state: dict, config: dict) -> None:
    banner("Stage: Validation")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    cmds = config["validation"]["commands"] or []
    if not cmds and config["validation"]["auto_detect"]:
        tracked = (TRACKED_FILES_FILE.read_text(encoding="utf-8").splitlines()
                   if TRACKED_FILES_FILE.exists() else [])
        cmds = detect_validation_commands(tracked)

    if not cmds:
        warn("No validation commands configured or detected. Skipping.")
        set_stage("done", state)
        return

    log_lines = ["# Validation Log", f"Run: {datetime.now(timezone.utc).isoformat()}", ""]
    all_passed = True

    for i, cmd in enumerate(cmds, start=1):
        cmd_list = cmd if isinstance(cmd, list) else cmd.split()
        # Resolve executable path (handles .cmd/.exe wrappers on Windows)
        resolved = shutil.which(cmd_list[0])
        if resolved:
            cmd_list = [resolved] + cmd_list[1:]
        elif not Path(cmd_list[0]).is_absolute():
            warn(f"Skipping: {cmd_list[0]} not found on PATH")
            log_lines += [f"## {' '.join(str(c) for c in cmd_list)}", "Exit: SKIPPED (command not found)", ""]
            continue
        cmd_str  = " ".join(str(c) for c in cmd_list)
        step(i, len(cmds), f"Running: {' '.join(cmd if isinstance(cmd, list) else cmd.split())}")

        result = run_command(cmd_list, capture=True)
        passed = result.returncode == 0
        icon   = f"{C.GREEN}PASS{C.RESET}" if passed else f"{C.RED}FAIL{C.RESET}"
        print(f"       [{icon}] {cmd_str}")

        log_lines += [
            f"## {cmd_str}", f"Exit: {result.returncode}", "```",
            (result.stdout or "").strip(), (result.stderr or "").strip(), "```", "",
        ]
        if not passed:
            all_passed = False
            error(f"Validation failed: {cmd_str}")
            _emit(
                AgentOutput(
                    ts=_now_iso(),
                    agent="validation",
                    line=f"WARNING X failed ({_summarize_validation_failure(result, cmd_str)})",
                )
            )

    val_log = LOGS_DIR / "validation.log"
    val_log.write_text("\n".join(log_lines), encoding="utf-8")
    success(f"Log  {val_log.relative_to(REPO_ROOT)}")

    if not all_passed:
        raise RuntimeError("Validation failed. See .ai-pipeline/logs/validation.log")

    commit_implementation(state, config)
    success("All validation checks passed.")
    set_stage("done", state)

    # Clean generated artifacts after a successful auto-commit
    if config["git"]["auto_commit"]:
        banner("Cleaning workspace for next run")
        clean_workspace()

# 
# Agent CLI installation
# 

# Maps CLI command name  npm package to install if missing
AGENT_NPM_PACKAGES: dict[str, str] = {
    "gemini": "@google/gemini-cli",
    "codex":  "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}

# Maps CLI command name  curl install script URL
AGENT_CURL_INSTALLS: dict[str, str] = {
    "chief": "https://raw.githubusercontent.com/minicodemonkey/chief/main/install.sh",
}


def _install_via_npm(command: str, pkg: str) -> bool:
    """Install an npm package globally. Returns True on success."""
    info(f"{command}  not found, installing {pkg} ...")
    result = run_command(["npm", "install", "-g", pkg], capture=True, timeout=120)
    if result.returncode == 0:
        success(f"{command}  installed")
        return True
    error(f"{command}  installation failed")
    warn(f"  Manual install: npm install -g {pkg}")
    if result.stderr:
        warn(f"  {result.stderr.strip()[:200]}")
    return False


def _install_via_curl(command: str, url: str) -> bool:
    """Install via a remote shell script. Returns True on success.

    Downloads the script first, then pipes it to bash  avoids ``shell=True``
    with a raw URL interpolated into a command string.
    """
    info(f"{command}  not found, installing via install script ...")
    if not shutil.which("curl"):
        error(f"{command}  curl not found, cannot run install script")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    if not shutil.which("bash"):
        error(f"{command}  bash not found, cannot run install script")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    try:
        fetch = run_command(["curl", "-fsSL", url], capture=True, timeout=30)
        if fetch.returncode != 0:
            error(f"{command}  download failed (exit {fetch.returncode})")
            warn(f"  Manual install: curl -fsSL {url} | bash")
            return False
        install = subprocess.run(
            ["bash", "-s"],
            input=fetch.stdout,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            cwd=str(REPO_ROOT),
            env={**os.environ},
        )
        if install.returncode == 0:
            success(f"{command}  installed")
            return True
        error(f"{command}  installation failed (exit {install.returncode})")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    except subprocess.TimeoutExpired:
        error(f"{command}  installation timed out")
        return False


def install_agents(config: dict) -> None:
    """Check each configured agent CLI and install any that are missing."""
    print()
    info("Checking agent CLIs ...")

    has_npm = bool(shutil.which("npm"))
    if not has_npm:
        warn("npm not found  npm-based agents cannot be auto-installed.")
        warn("Install Node.js from https://nodejs.org then re-run ain init")

    agents   = config.get("agents", {})
    seen     = set()
    any_missing = False

    for stage, agent_cfg in agents.items():
        command = agent_cfg.get("command", "")
        if not command or command in seen:
            continue
        seen.add(command)

        if shutil.which(command):
            success(f"{command} ({stage})  already installed")
        elif command in AGENT_CURL_INSTALLS:
            any_missing = True
            _install_via_curl(command, AGENT_CURL_INSTALLS[command])
        elif command in AGENT_NPM_PACKAGES:
            any_missing = True
            if has_npm:
                _install_via_npm(command, AGENT_NPM_PACKAGES[command])
            else:
                warn(f"{command}  skipped (npm not available)")
        else:
            warn(f"{command} ({stage})  not found and no auto-install configured")
            warn(f"  Install it manually and ensure it is on your PATH")

    if not any_missing:
        success("All agents available.")


# 
# ain init  scaffold pipeline into current repo
# 

def run_init() -> None:
    from importlib.resources import files as res_files

    banner("A.I.N. Pipeline - Init")

    for d in [PIPELINE_DIR, SCAN_DIR, PROMPTS_DIR, LOGS_DIR, APPROVALS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        initial_config = load_config() if CONFIG_FILE.exists() else DEFAULT_CONFIG
        save_state(_default_state(initial_config))
        success(f"Created {STATE_FILE.relative_to(REPO_ROOT)}")
    else:
        info(f"Skipped {STATE_FILE.relative_to(REPO_ROOT)} (already exists)")

    if not CONFIG_FILE.exists():
        src = res_files("ain").joinpath("data/config.json").read_text(encoding="utf-8")
        CONFIG_FILE.write_text(src, encoding="utf-8")
        success(f"Created {CONFIG_FILE.relative_to(REPO_ROOT)}")
    else:
        info(f"Skipped {CONFIG_FILE.relative_to(REPO_ROOT)} (already exists)")

    prompt_names = [
        "architecture_prompt.md",
        "planning_questions_prompt.md",
        "planning_generation_prompt.md",
        "task_creation_prompt.md",
        "implementation_prompt.md",
    ]
    for name in prompt_names:
        target = PROMPTS_DIR / name
        if not target.exists():
            content = res_files("ain").joinpath(f"data/prompts/{name}").read_text(encoding="utf-8")
            target.write_text(content, encoding="utf-8")
            success(f"Created {target.relative_to(REPO_ROOT)}")
        else:
            info(f"Skipped {target.relative_to(REPO_ROOT)} (already exists)")

    install_agents(load_config())

    print()
    success("Pipeline initialized.")
    info(f"Edit {CONFIG_FILE.relative_to(REPO_ROOT)} to configure your agents.")
    info("Then run: ain run")

# 
# Status display
# 

def show_status(state: dict) -> None:
    banner("A.I.N. Pipeline - Status")
    current   = state.get("current_stage", "unknown")
    completed = state.get("completed_stages", [])
    mode = get_selected_mode(state, load_config())
    details = get_mode_details(mode)

    print(f"  Stage:   {C.BOLD}{C.CYAN}{STAGE_LABELS.get(current, current)}{C.RESET}")
    print(f"  Mode:    {C.DIM}{details['label']} | {details['summary']}{C.RESET}")
    if state.get("branch"):
        print(f"  Branch:  {C.DIM}{state['branch']}{C.RESET}")
    if state.get("started_at"):
        print(f"  Started: {C.DIM}{state['started_at']}{C.RESET}")
    if state.get("failure_reason"):
        print(f"  Reason:  {C.RED}{state['failure_reason']}{C.RESET}")
    print()

    for stage in STAGES:
        if stage == "idle":
            continue
        if stage in completed:
            icon = f"{C.GREEN}{C.RESET}"
        elif stage == current:
            icon = f"{C.YELLOW}{C.RESET}"
        else:
            icon = f"{C.DIM}{C.RESET}"
        print(f"    {icon}  {STAGE_LABELS.get(stage, stage)}")

    if TASK_GRAPH_FILE.exists():
        try:
            data = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
            print(f"\n  Tasks: {C.GREEN}{data.get('completed', 0)}{C.RESET}/{data.get('total', 0)} completed")
        except Exception:
            pass
    print()

# 
# Orchestrator
# 

STAGE_RUNNERS = {
    "scanning":            run_scan,
    "architecture":        run_architecture,
    "user_context":        run_user_context,
    "planning_questions":  run_planning_questions,
    "planning_generation": run_planning_generation,
    "task_creation":       run_task_creation,
    "waiting_approval":    run_waiting_approval,
    "implementation":      run_implementation,
    "validation":          run_validation,
}


def run_pipeline(
    start_stage: str | None = None,
    single_stage: bool = False,
    emitter: Emitter | None = None,
    mode: str = "plain",
    renderer: Any = None,
) -> None:
    global _EMITTER, _RUN_ID, _RENDERER
    _EMITTER = emitter
    _RENDERER = renderer
    _RUN_ID = str(uuid.uuid4())

    ensure_config()
    config = load_config()
    state  = load_state(config)
    _APPROVAL_EVENT.clear()
    state["run_id"] = _RUN_ID
    if not state.get("status") or state.get("status") == "idle":
        state["status"] = "running"
    save_state(state)

    def _record_stage_timing(stage_id: str, started_at: str, ended_at: str, duration_ms: int, status: str) -> None:
        """Persist timing metrics and emit related events without crashing the run."""
        timing = StageTiming(
            stage_name=stage_id,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            status=status,
        )
        try:
            state_service.record_stage_timing(timing)
        except Exception as exc:  # noqa: BLE001 - defensive logging
            warn(f"Unable to record timing for {stage_id}: {exc}")
        else:
            _emit(StageTimingUpdated(stage_id=stage_id, timing=timing))
            try:
                log_service.log_stage_timing(stage_id, timing)
            except Exception:
                # Logging failures must not crash the pipeline
                pass

    # Run a health check before executing any stages so we can fail fast with guidance.
    try:
        health_summary = config_service.get_health_summary(REPO_ROOT)
        _emit(HealthCheckResult(summary=health_summary, checked_at=_now_iso()))
    except Exception as exc:  # noqa: BLE001 - defensive
        warn(f"Health check failed: {exc}")
        health_summary = None

    if health_summary and getattr(health_summary, "overall_status", "") == "unhealthy":
        error("Environment health check failed. See health summary for details.")
        state["last_error"] = {
            "code": "HEALTH_CHECK_FAILED",
            "message": "Health check reported an unhealthy environment.",
            "details": {"overall_status": health_summary.overall_status},
            "stage": None,
            "recoverable": False,
        }
        state["status"] = FAILED
        state["current_stage"] = FAILED
        save_state(state)
        _emit(RunCompleted(run_id=_RUN_ID, ended_at=_now_iso(), status=RunStatus.FAILED))
        return

    if renderer is not None and hasattr(renderer, "configure_mode_controls"):
        def _cycle_mode_from_tui() -> dict[str, str]:
            fresh_config = load_config()
            return cycle_pipeline_mode(load_state(fresh_config), fresh_config)

        renderer.configure_mode_controls(
            get_mode_details(get_selected_mode(state, config)),
            _cycle_mode_from_tui,
        )

    if state["current_stage"] == FAILED:
        warn("Pipeline is in a failed state. Use --reset or --resume <stage>.")
        show_status(state)
        return

    if state["current_stage"] == "done" and not start_stage:
        success("Pipeline is complete.")
        show_status(state)
        return

    if start_stage:
        if start_stage not in STAGES:
            error(f"Unknown stage: {start_stage}")
            error(f"Valid stages: {', '.join(STAGES)}")
            sys.exit(1)
        state = set_stage(start_stage, state)

    current = state.get("current_stage") or "idle"
    if current == "idle":
        state   = set_stage("scanning", state)
        current = "scanning"

    try:
        idx = STAGES.index(current)
    except ValueError:
        error(f"Unknown stage in state: {current}")
        sys.exit(1)

    to_run = [current] if single_stage else STAGES[idx:]

    _emit(RunStarted(run_id=_RUN_ID, started_at=_now_iso(), mode=mode))  # type: ignore[arg-type]

    runnable = [s for s in to_run if s not in ("idle", "done") and STAGE_RUNNERS.get(s)]
    stage_indices = {stage: idx for idx, stage in enumerate(runnable)}
    for i, stage in enumerate(runnable):
        _emit(StageQueued(stage_id=stage, stage_name=STAGE_LABELS.get(stage, stage), index=i))

    for stage in to_run:
        if stage in ("idle", "done"):
            continue
        runner = STAGE_RUNNERS.get(stage)
        if not runner:
            continue
        stage_label = STAGE_LABELS.get(stage, stage)
        started_at = _now_iso()
        t0 = time.perf_counter()
        _emit(
            StageStarted(
                stage_id=stage,
                started_at=started_at,
                stage_name=stage_label,
                index=stage_indices.get(stage),
            )
        )
        try:
            runner(state, config)
            state = load_state(config)
            ended_at = _now_iso()
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _record_stage_timing(stage, started_at, ended_at, duration_ms, "success")
            _emit(
                StageCompleted(
                    stage_id=stage,
                    stage_name=stage_label,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                    status="success",
                )
            )
        except KeyboardInterrupt:
            ended_at = _now_iso()
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _record_stage_timing(stage, started_at, ended_at, duration_ms, "failed")
            _emit(
                StageFailed(
                    stage_id=stage,
                    stage_name=stage_label,
                    ended_at=ended_at,
                    error="Interrupted by user.",
                    error_code="INTERRUPTED",
                )
            )
            _emit(RunCompleted(run_id=_RUN_ID, ended_at=ended_at, status=RunStatus.INTERRUPTED))
            warn("\nInterrupted by user.")
            sys.exit(0)
        except Exception as e:  # noqa: BLE001 - error fencing
            ended_at = _now_iso()
            duration_ms = int((time.perf_counter() - t0) * 1000)
            err_msg = str(e)
            err_code = getattr(e, "code", None)
            _record_stage_timing(stage, started_at, ended_at, duration_ms, "failed")
            _emit(
                StageFailed(
                    stage_id=stage,
                    stage_name=stage_label,
                    ended_at=ended_at,
                    error=err_msg,
                    error_code=err_code,
                )
            )
            _emit(RunCompleted(run_id=_RUN_ID, ended_at=ended_at, status=RunStatus.FAILED))
            try:
                log_service.log_error_record(
                    err_code or "PIPELINE_STAGE_ERROR",
                    err_msg,
                    stage=stage,
                    details={"stage": stage},
                    recoverable=True,
                )
            except Exception:
                pass
            state = load_state(config)
            state["last_error"] = {
                "code": err_code or "PIPELINE_STAGE_ERROR",
                "message": err_msg,
                "details": {"stage": stage},
                "stage": stage,
                "recoverable": True,
            }
            state["status"] = FAILED
            state["current_stage"] = stage
            save_state(state)
            fail_pipeline(state, err_msg)

    state = load_state(config)
    if state["current_stage"] == "done":
        banner("Pipeline Complete")
        show_status(state)
        _emit(RunCompleted(run_id=_RUN_ID, ended_at=_now_iso(), status=RunStatus.DONE))

# 
# CLI
# 

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ain",
        description="A.I.N. Pipeline - multi-agent AI development orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent("""\
            Examples:
              ain init                       Scaffold .ai-pipeline/ into current repo
              ain run                        Run pipeline from current stage
              ain run --resume scanning      Resume from scanning stage
              ain run --stage architecture   Run one stage only
              ain --status                   Show pipeline status
              ain --approve                  Approve planning artifacts
              ain --reset                    Reset to idle
              ain --clean                    Remove all generated files, reset to idle
        """),
    )

    subparsers = parser.add_subparsers(dest="command")

    # ain init
    subparsers.add_parser("init", help="Scaffold .ai-pipeline/ into the current repo")

    # ain run
    run_parser = subparsers.add_parser("run", help="Run pipeline from current stage")
    run_parser.add_argument("--resume", metavar="STAGE", help="Resume from a specific stage")
    run_parser.add_argument("--stage",  metavar="STAGE", help="Run only this stage")
    run_parser.add_argument("--plain",  action="store_true", help="Disable TUI; print plain output")

    resume_parser = subparsers.add_parser("resume", help="Resume pipeline from a specific stage")
    resume_parser.add_argument("stage", metavar="STAGE", help="Stage to resume from")
    resume_parser.add_argument("--plain", action="store_true", help="Disable TUI; print plain output")

    subparsers.add_parser("continue", help="Continue from paused/failed/interrupted state")
    approve_parser = subparsers.add_parser("approve", help="Approve planning artifacts")
    approve_parser.add_argument("--run-id", metavar="ID", help="Reserved for compatibility", default=None)

    status_parser = subparsers.add_parser("status", help="Show pipeline status")
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable status JSON")

    reset_parser = subparsers.add_parser("reset", help="Reset pipeline state")
    reset_parser.add_argument("--hard", action="store_true", help="Remove generated files and logs")
    reset_parser.add_argument("--yes", action="store_true", help="Confirm hard reset without prompting")
    subparsers.add_parser("clean", help="Remove all generated files and reset to idle")

    logs_parser = subparsers.add_parser("logs", help="View merged logs")
    logs_parser.add_argument("--follow", action="store_true", help="Follow logs continuously")
    logs_parser.add_argument("--tail", type=int, default=50, metavar="N", help="Show the last N lines")
    logs_parser.add_argument(
        "--level",
        choices=["debug", "info", "warn", "error"],
        help="Filter by log level",
    )
    logs_parser.add_argument(
        "--source",
        choices=["pipeline", "validation", "agent"],
        help="Filter by log source",
    )
    logs_parser.add_argument("--json", action="store_true", help="Emit logs as JSON")

    config_parser = subparsers.add_parser("config", help="Manage project configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("list", help="List current config keys and values")
    config_get_parser = config_subparsers.add_parser("get", help="Show a config value")
    config_get_parser.add_argument("key", metavar="KEY")
    config_set_parser = config_subparsers.add_parser("set", help="Set a config value")
    config_set_parser.add_argument("key", metavar="KEY")
    config_set_parser.add_argument("value", metavar="VALUE")
    config_reset_parser = config_subparsers.add_parser("reset", help="Reset one key or the whole config")
    config_reset_parser.add_argument("key", metavar="KEY", nargs="?")

    version_parser = subparsers.add_parser("version", help="Show CLI version")
    version_parser.add_argument("--short", action="store_true", help="Only print the semantic version")

    # Global flags (no subcommand)
    parser.add_argument("--status",  action="store_true", help="Show pipeline status")
    parser.add_argument("--approve", action="store_true", help="Approve planning artifacts")
    parser.add_argument("--reset",   action="store_true", help="Reset pipeline to idle")
    parser.add_argument("--clean",   action="store_true", help="Remove all generated files and reset to idle")

    args = parser.parse_args()

    for d in [PIPELINE_DIR, SCAN_DIR, PROMPTS_DIR, LOGS_DIR, APPROVALS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if args.command == "init":
        run_init()
        return

    if args.command == "reset" and getattr(args, "hard", False):
        if not getattr(args, "yes", False):
            error("Hard reset requires --yes.")
            sys.exit(2)
        banner("A.I.N. Pipeline - Reset")
        clean_workspace()
        return

    if args.reset or args.command == "reset":
        save_state(_default_state(load_config()))
        if PLANNING_APPROVED_FLAG.exists():
            PLANNING_APPROVED_FLAG.unlink()
        success("Pipeline reset to idle.")
        return

    if args.clean or args.command == "clean":
        banner("A.I.N. Pipeline - Clean")
        clean_workspace()
        return

    if args.approve or args.command == "approve":
        APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
        approved_at = datetime.now(timezone.utc).isoformat()
        PLANNING_APPROVED_FLAG.write_text(f"Approved: {approved_at}\n", encoding="utf-8")
        success("Planning approved.")
        _emit(ApprovalReceived(run_id=_RUN_ID, actor="user", at=approved_at))
        state = load_state()
        if state["current_stage"] == "waiting_approval":
            set_stage("implementation", state)
            success("Advanced to implementation. Run: ain run")
        return

    if args.status or args.command == "status":
        state = load_state()
        if getattr(args, "json", False):
            payload = {
                "pipeline_state": state,
                "health": config_service.get_health_summary(REPO_ROOT).__dict__,
            }
            _print_json(payload)
        else:
            show_status(state)
        return

    if args.command == "logs":
        _show_logs(
            follow=getattr(args, "follow", False),
            tail=getattr(args, "tail", 50),
            level=getattr(args, "level", None),
            source=getattr(args, "source", None),
            as_json=getattr(args, "json", False),
        )
        return

    if args.command == "config":
        ensure_config()
        if args.config_command == "list":
            _show_config_list()
            return
        if args.config_command == "get":
            _show_config_get(args.key)
            return
        if args.config_command == "set":
            config = load_config()
            parent, leaf = _config_lookup(config, args.key)
            parent[leaf] = _parse_config_value(args.value)
            save_config(config)
            success(f"Updated {args.key}.")
            return
        if args.config_command == "reset":
            if args.key is None:
                save_config(copy.deepcopy(DEFAULT_CONFIG))
                success("Config reset to defaults.")
                return
            config = load_config()
            if not _reset_config_key(config, args.key):
                error(f"Unknown config key: {args.key}")
                sys.exit(2)
            save_config(config)
            success(f"Reset {args.key}.")
            return
        parser.error("config requires a subcommand")

    if args.command == "version":
        _show_version(short=getattr(args, "short", False))
        return

    if args.command == "run":
        config = load_config()
        state = load_state(config)
        selected_mode = prompt_for_pipeline_mode(state, config)
        if selected_mode != get_selected_mode(state, config):
            set_pipeline_mode(selected_mode, state, config)
        single = bool(getattr(args, "stage", None))
        plain  = getattr(args, "plain", False)
        _run_with_tui(
            start_stage=getattr(args, "resume", None) or getattr(args, "stage", None),
            single_stage=single,
            plain=plain,
        )
        return

    if args.command == "resume":
        plain = getattr(args, "plain", False)
        _run_with_tui(start_stage=args.stage, plain=plain)
        return

    if args.command == "continue":
        state = load_state()
        current = state.get("current_stage", "idle")
        if current == "done":
            success("Pipeline is already complete.")
            show_status(state)
            return
        if current in ("paused", "failed"):
            stage = state.get("last_attempted_stage") or state.get("last_safe_stage", "scanning")
        elif current in STAGES and current not in ("idle",):
            stage = current
        else:
            stage = "scanning"
        info(f"Continuing from: {STAGE_LABELS.get(stage, stage)}")
        _run_with_tui(start_stage=stage)
        return

    # No subcommand and no flag  show help
    parser.print_help()


def _run_with_tui(
    start_stage: str | None = None,
    single_stage: bool = False,
    plain: bool = False,
) -> None:
    """Launch run_pipeline, wrapping in the Rich TUI unless --plain is set."""
    if plain:
        run_pipeline(start_stage=start_stage, single_stage=single_stage, mode="plain")
        return

    exit_code: int | None = None
    persistent_error: str | None = None
    try:
        from ain.tui import RichRenderer
        from ain.runtime.emitter import Emitter
        from rich.console import Console as _Console
        from ain import __version__ as _ver

        # Use Rich's own terminal detection  more reliable than sys.stdout.isatty()
        if not _Console().is_terminal:
            run_pipeline(start_stage=start_stage, single_stage=single_stage, mode="plain")
            return

        emitter  = Emitter()
        renderer = RichRenderer(version=_ver)
        renderer.subscribe(emitter)
        renderer.start()
        try:
            try:
                next_start_stage = start_stage
                next_single_stage = single_stage
                while True:
                    run_pipeline(
                        start_stage=next_start_stage,
                        single_stage=next_single_stage,
                        emitter=emitter,
                        renderer=renderer,
                        mode="rich",
                    )

                    state = load_state(load_config())
                    if state.get("current_stage") != "done":
                        break

                    choice = renderer.request_input(
                        "SUCCESS: pipeline completed. [N] new AIN session, [Q] quit"
                    ).strip().lower()
                    if choice == "n":
                        save_state(_default_state(load_config()))
                        if PLANNING_APPROVED_FLAG.exists():
                            PLANNING_APPROVED_FLAG.unlink()
                        next_start_stage = None
                        next_single_stage = False
                        continue
                    break
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
                if exit_code not in (0, None):
                    try:
                        failed_state = load_state(load_config())
                        last_error = failed_state.get("last_error") or {}
                        persistent_error = str(last_error.get("message") or "Pipeline failed.")
                    except Exception:
                        persistent_error = "Pipeline failed."
        finally:
            renderer.stop()

        if persistent_error:
            print(f"{C.RED}  X{C.RESET} {persistent_error}", file=sys.stderr)
        if exit_code is not None:
            raise SystemExit(exit_code)

    except Exception:
        # Any TUI failure  fall back to plain output
        run_pipeline(start_stage=start_stage, single_stage=single_stage, mode="plain")


if __name__ == "__main__":
    main()

