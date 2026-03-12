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
import _thread
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
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any

from ain.models.state import MultilineInputMode, MultilineInputState, PlannedFileChange, StageTiming
from ain.runtime.emitter import Emitter
from ain.runtime.events import (
    ApprovalReceived,
    AwaitingApproval,
    CancelMultilineInputEvent,
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
    SubmitMultilineInputEvent,
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


# Ensure legacy context files are migrated to docs/ so TUI and agents share one location.
def _migrate_context_files() -> None:
    try:
        if _LEGACY_USER_CONTEXT_FILE.exists():
            USER_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
            USER_CONTEXT_FILE.write_text(_LEGACY_USER_CONTEXT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            _LEGACY_USER_CONTEXT_FILE.unlink(missing_ok=True)
        if _LEGACY_BRAINSTORM_CONTEXT_FILE.exists():
            BRAINSTORM_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
            BRAINSTORM_CONTEXT_FILE.write_text(
                _LEGACY_BRAINSTORM_CONTEXT_FILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            _LEGACY_BRAINSTORM_CONTEXT_FILE.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not migrate legacy context files: {exc}")

def _migrate_task_feedback_file() -> None:
    try:
        if _LEGACY_TASK_REVIEW_FEEDBACK_FILE.exists():
            TASK_REVIEW_FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
            TASK_REVIEW_FEEDBACK_FILE.write_text(
                _LEGACY_TASK_REVIEW_FEEDBACK_FILE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            _LEGACY_TASK_REVIEW_FEEDBACK_FILE.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not migrate legacy task feedback file: {exc}")


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
STATE_LOGS_DIR    = PIPELINE_DIR / "state_logs"
APPROVALS_DIR     = PIPELINE_DIR / "approvals"
USER_CONTEXT_FILE = REPO_ROOT / "docs" / "user_context.md"
BRAINSTORM_CONTEXT_FILE = REPO_ROOT / "docs" / "brainstorm_context.md"
_LEGACY_USER_CONTEXT_FILE = PIPELINE_DIR / "user_context.md"
_LEGACY_BRAINSTORM_CONTEXT_FILE = PIPELINE_DIR / "brainstorm_context.md"
TASK_REVIEW_FEEDBACK_FILE = REPO_ROOT / "docs" / "task_review_feedback.md"
_LEGACY_TASK_REVIEW_FEEDBACK_FILE = PIPELINE_DIR / "task_review_feedback.md"
DOCS_DIR      = REPO_ROOT / "docs"
PIPELINE_LOG  = LOGS_DIR / "pipeline.log"
NOTIFICATIONS_LOG = LOGS_DIR / "notifications.log"
NOTIFICATIONS_TAB_TITLE = "A.I.N. Notifications"

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
GITIGNORE_FILE          = REPO_ROOT / ".gitignore"

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
    "planning_answers",
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
    "planning_answers":    "Planning - Answers",
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
    "default_lite": {
        "label": "Default (Lite)",
        "summary": "Gemini scan → GPT-5.1 Codex planning/task creation → Claude Sonnet 4.5 implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "codex_lite",
            "planning_generation": "codex_lite",
            "task_creation": "codex_lite",
            "implementation": "claude_lite",
        },
    },
    "default_balanced": {
        "label": "Default (Balanced)",
        "summary": "Gemini scan → GPT-5.2 Codex planning/task creation → Claude Sonnet 4.6 implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "codex_balanced",
            "planning_generation": "codex_balanced",
            "task_creation": "codex_balanced",
            "implementation": "claude_balanced",
        },
    },
    "default_max": {
        "label": "Default (Max)",
        "summary": "Gemini scan → GPT-5.3 Codex planning/task creation → Claude Opus 4.6 implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "codex_max",
            "planning_generation": "codex_max",
            "task_creation": "codex_max",
            "implementation": "claude_max",
        },
    },
    "codex_only_lite": {
        "label": "Codex Only (Lite)",
        "summary": "Gemini scan → GPT-5.1 Codex handles planning, tasks, and implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "codex_lite",
            "planning_generation": "codex_lite",
            "task_creation": "codex_lite",
            "implementation": "codex_lite",
        },
    },
    "codex_only_balanced": {
        "label": "Codex Only (Balanced)",
        "summary": "Gemini scan → GPT-5.2 Codex handles planning, tasks, and implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "codex_balanced",
            "planning_generation": "codex_balanced",
            "task_creation": "codex_balanced",
            "implementation": "codex_balanced",
        },
    },
    "codex_only_max": {
        "label": "Codex Only (Max)",
        "summary": "Gemini scan → GPT-5.3 Codex max handles planning, tasks, and implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "codex_max",
            "planning_generation": "codex_max",
            "task_creation": "codex_max",
            "implementation": "codex_max",
        },
    },
    "claude_only_lite": {
        "label": "Claude Only (Lite)",
        "summary": "Gemini scan → Claude Sonnet 4.6 handles planning, tasks, and implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "claude_lite",
            "planning_generation": "claude_lite",
            "task_creation": "claude_lite",
            "implementation": "claude_lite",
        },
    },
    "claude_only_balanced": {
        "label": "Claude Only (Balanced)",
        "summary": "Gemini scan → Claude Opus 4.5 handles planning, tasks, and implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "claude_balanced",
            "planning_generation": "claude_balanced",
            "task_creation": "claude_balanced",
            "implementation": "claude_balanced",
        },
    },
    "claude_only_max": {
        "label": "Claude Only (Max)",
        "summary": "Gemini scan → Claude Opus 4.6 handles planning, tasks, and implementation",
        "stages": {
            "architecture": "scan_gemini",
            "planning_questions": "claude_max",
            "planning_generation": "claude_max",
            "task_creation": "claude_max",
            "implementation": "claude_max",
        },
    },
    "gemini_only": {
        "label": "Gemini Only",
        "summary": "Gemini 3.1 Flash Lite handles every stage (scan, planning, and implementation)",
        "stages": {
            "architecture": "gemini_only",
            "planning_questions": "gemini_only",
            "planning_generation": "gemini_only",
            "task_creation": "gemini_only",
            "implementation": "gemini_only",
        },
    },
}

# 
# Default configuration
# 

DEFAULT_CONFIG: dict[str, Any] = {
    "agents": {
        "scan_gemini": {
            "command": "gemini",
            "args": [],
            "model": "gemini-1.5-flash",
            "description": "Gemini for scanning and architecture analysis",
            "skills": [
                "codebase analysis",
                "system architecture reasoning",
            ],
        },
        "codex_lite": {
            "command": "codex",
            "args": [],
            "model": "gpt-5.1-codex",
            "prompt_mode": "stdin",
            "description": "GPT-5.1 Codex for planning, brainstorming, and task creation",
            "skills": ["software planning", "task decomposition", "multi-step reasoning"],
        },
        "codex_balanced": {
            "command": "codex",
            "args": [],
            "model": "gpt-5.2-codex",
            "prompt_mode": "stdin",
            "description": "GPT-5.2 Codex for planning and implementation with extra scale",
            "skills": ["software planning", "tool orchestration", "code synthesis"],
        },
        "codex_max": {
            "command": "codex",
            "args": [],
            "model": "gpt-5.3-codex-max",
            "prompt_mode": "stdin",
            "description": "GPT-5.3 Codex Max for long-context planning and code generation",
            "skills": ["autonomous planning", "deep code synthesis", "extended context reasoning"],
        },
        "claude_lite": {
            "command": "claude",
            "args": ["--allowedTools", "Edit,Write,Bash,Read,Glob,Grep"],
            "model": "claude-sonnet-4-20250514",
            "description": "Claude Sonnet 4.5 for implementation with multi-file editing",
            "skills": ["software implementation", "code refactoring", "debugging"],
        },
        "claude_balanced": {
            "command": "claude",
            "args": ["--allowedTools", "Edit,Write,Bash,Read,Glob,Grep"],
            "model": "claude-sonnet-4-20260115",
            "description": "Claude Sonnet 4.6 for adaptive implementation with broader reasoning",
            "skills": ["implementation", "architectural reasoning", "automation"],
        },
        "claude_max": {
            "command": "claude",
            "args": ["--allowedTools", "Edit,Write,Bash,Read,Glob,Grep"],
            "model": "claude-opus-4.6",
            "description": "Claude Opus 4.6 for maximum context implementation",
            "skills": ["long-context coding", "team orchestration", "execution supervision"],
        },
        "gemini_only": {
            "command": "gemini",
            "args": [],
            "model": "gemini-1.5-flash",
            "description": "Gemini for every stage (free-tier safe)",
            "skills": ["end-to-end planning", "code generation", "architecture reasoning"],
        },
        "task_creation_codex": {
            "command": "codex",
            "args": ["exec"],
            "model": "gpt-5.3-codex-max",
            "prompt_mode": "stdin",
            "description": "Codex Max fallback for task creation",
            "skills": ["task generation", "planning feedback"],
        },
        "implementation_fallback": {
            "command": "codex",
            "args": ["--approval-mode", "full-auto"],
            "model": "gpt-5.3-codex-max",
            "prompt_mode": "arg",
            "description": "Codex fallback for implementation when other agents exceed limits.",
            "skills": ["code recovery", "autonomous implementation"],
        },
    },
    "pipeline_mode": {
        "default": "default_balanced",
        "available": [
            "default_lite",
            "default_balanced",
            "default_max",
            "codex_only_lite",
            "codex_only_balanced",
            "codex_only_max",
            "claude_only_lite",
            "claude_only_balanced",
            "claude_only_max",
            "gemini_only",
        ],
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


def _is_unexpected_kwarg_typeerror(exc: TypeError) -> bool:
    return "unexpected keyword argument" in str(exc)


def _run_pipeline_compat(**kwargs: Any) -> None:
    try:
        run_pipeline(**kwargs)
    except TypeError as exc:
        if not _is_unexpected_kwarg_typeerror(exc):
            raise
        reduced = {k: v for k, v in kwargs.items() if k in {"start_stage", "single_stage"}}
        run_pipeline(**reduced)


def _workspace_status_snapshot() -> dict[str, str]:
    """Return a repo-relative git porcelain snapshot keyed by path."""
    git = shutil.which("git")
    if not git:
        return {}

    try:
        result = run_command(
            [git, "status", "--short", "--untracked-files=all"],
            capture=True,
            timeout=30,
        )
    except Exception:
        return {}

    if result.returncode != 0:
        return {}

    snapshot: dict[str, str] = {}
    for raw in (result.stdout or "").splitlines():
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:].strip()
        if not path:
            continue
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        snapshot[path.replace("\\", "/")] = status
    return snapshot


def _emit_workspace_delta(agent_name: str, before: dict[str, str]) -> None:
    """Send a concise workspace delta summary to the agent output panel."""
    after = _workspace_status_snapshot()
    if not after and not before:
        return

    changed_paths = sorted(set(before) | set(after))
    delta_lines: list[str] = []

    for path in changed_paths:
        old_status = before.get(path)
        new_status = after.get(path)
        if old_status == new_status:
            continue
        if new_status is None:
            label = "CLEAN"
        elif new_status == "??":
            label = "NEW"
        elif "D" in new_status:
            label = "DELETE"
        elif "R" in new_status or "C" in new_status:
            label = "MOVE"
        else:
            label = "WRITE"
        delta_lines.append(f"{label} {path}")

    if not delta_lines:
        return

    _emit(AgentOutput(ts=_now_iso(), agent=agent_name, line="WORKSPACE DELTA"))
    for line in delta_lines[:12]:
        _emit(AgentOutput(ts=_now_iso(), agent=agent_name, line=line))

    git = shutil.which("git")
    if not git:
        return

    diff_targets = [path for path in changed_paths if after.get(path) not in {None, "??"}]
    if not diff_targets:
        return

    try:
        result = run_command(
            [git, "diff", "--numstat", "--", *diff_targets],
            capture=True,
            timeout=30,
        )
    except Exception:
        return

    if result.returncode != 0:
        return

    stats = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not stats:
        return

    _emit(AgentOutput(ts=_now_iso(), agent=agent_name, line="DIFFSTAT"))
    for line in stats[:8]:
        parts = line.split("\t", 2)
        if len(parts) == 3:
            added, removed, path = parts
            _emit(AgentOutput(ts=_now_iso(), agent=agent_name, line=f"+{added} -{removed} {path}"))


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
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": state_service.STATE_SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
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
    if "last_approval_time" not in merged:
        merged["last_approval_time"] = None
    return merged


def load_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    current_config = config or load_config()
    if not STATE_FILE.exists():
        return _default_state(current_config)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        # Corrupt JSON — delegate to state_service for backup and reset.
        state_service.load_state(state_path=STATE_FILE)
        return _default_state(current_config)
    state = load_state_with_backfill(raw, current_config)
    if state != raw:
        save_state(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    state["last_updated"] = now
    state["updated_at"] = now
    state.setdefault("created_at", now)
    state.setdefault("version", state_service.STATE_SCHEMA_VERSION)
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    _log(f"State saved: {state['current_stage']}")


def checkpoint_before_stage(stage: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        state = load_state()
    state = load_state_with_backfill(state)
    state["last_attempted_stage"] = stage
    save_state(state)
    return state


def checkpoint_after_stage_success(
    stage: str,
    next_stage: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if state is None:
        state = load_state()
    state = load_state_with_backfill(state)
    completed = state.get("completed_stages", [])
    if stage not in completed:
        completed.append(stage)
    state["completed_stages"] = completed
    state["last_safe_stage"] = stage
    state["last_attempted_stage"] = stage
    state["current_stage"] = next_stage
    state["status"] = next_stage
    save_state(state)
    return state


def classify_agent_failure(message: str) -> str:
    text = (message or "").lower()
    if any(phrase in text for phrase in _TOKEN_LIMIT_PHRASES):
        return "token_exhaustion"
    if "timed out" in text or "no response" in text:
        return "no_response"
    return "unknown"


def pause_pipeline(reason: str, details: str, resume_hint: str) -> dict[str, Any]:
    state = load_state()
    state = load_state_with_backfill(state)
    state["current_stage"] = "paused"
    state["status"] = "paused"
    state["pause_reason"] = reason
    state["pause_details"] = details
    state["resume_hint"] = resume_hint
    save_state(state)
    return state


def _next_stage_after(stage: str) -> str:
    try:
        idx = STAGES.index(stage)
    except ValueError:
        return "scanning"
    for candidate in STAGES[idx + 1 :]:
        if candidate not in {"idle", "done"}:
            return candidate
    return "done"


def resolve_continue_stage(state: dict[str, Any]) -> str | None:
    current = state.get("current_stage", "idle")
    if current == "done":
        return None
    if current == "paused":
        return state.get("last_attempted_stage") or _next_stage_after(state.get("last_safe_stage", "idle"))
    if current == FAILED:
        if state.get("pause_reason") not in {"token_exhaustion", "no_response"}:
            raise ValueError("Current failed state is not recoverable via continue")
        return state.get("last_attempted_stage") or _next_stage_after(state.get("last_safe_stage", "idle"))
    if current in STAGES and current != "idle":
        return current
    return "scanning"


def is_warp_running() -> bool:
    try:
        result = run_command(["tasklist"], capture=True, timeout=15)
    except Exception:
        return False
    return "warp.exe" in (result.stdout or "").lower()


def open_warp_tab(title: str, command: str) -> dict[str, Any]:
    warp = shutil.which("warp")
    if not warp:
        return {"success": False, "mode": "warp", "details": "Warp CLI not found on PATH."}
    try:
        if not is_warp_running():
            subprocess.Popen([warp])
            details = "Opened new Warp launch."
        else:
            details = "Opened tab in existing Warp window."
        subprocess.Popen([warp, "new-tab", "--title", title, "--command", command])
        return {"success": True, "mode": "warp", "details": details}
    except Exception as exc:
        return {"success": False, "mode": "warp", "details": str(exc)}


def open_fallback_terminal(title: str, command: str) -> dict[str, Any]:
    try:
        subprocess.Popen(["cmd.exe", "/c", "start", title, "cmd.exe", "/k", command])
        return {
            "success": True,
            "mode": "fallback_terminal",
            "details": f"Opened fallback terminal window with title '{title}'.",
        }
    except Exception as exc:
        return {"success": False, "mode": "fallback_terminal", "details": str(exc)}


def open_preferred_terminal_tab(title: str, command: str) -> dict[str, Any]:
    warp_result = open_warp_tab(title, command)
    if warp_result.get("success"):
        return warp_result
    fallback = open_fallback_terminal(title, command)
    details = f"Warp tab launch failed: {warp_result.get('details')}"
    if fallback.get("details"):
        details = f"{details} {fallback['details']}"
    return {
        "success": fallback.get("success", False),
        "mode": fallback.get("mode", "fallback_terminal"),
        "details": details,
    }


def notify(level: str, summary: str, hint: str | None = None) -> dict[str, Any]:
    normalized = level.lower()
    state = load_state()
    state = load_state_with_backfill(state)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    NOTIFICATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)

    with open(NOTIFICATIONS_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{normalized.upper()}: {summary}\n")
        if hint:
            handle.write(f"HINT: {hint}\n")

    channel = state.get("notification_channel") or {}
    if channel.get("active") and channel.get("log_path") == str(NOTIFICATIONS_LOG):
        launch = {
            "success": True,
            "mode": channel.get("mode", "fallback_terminal"),
            "details": "Reusing existing notification channel.",
        }
    else:
        command = f"type \"{NOTIFICATIONS_LOG}\""
        launched = open_preferred_terminal_tab(NOTIFICATIONS_TAB_TITLE, command)
        launch = dict(launched)
        prefix = "Recreated notification channel." if channel else "Created notification channel."
        launch["details"] = f"{prefix} {launched.get('details', '')}".strip()
        channel = {
            "active": bool(launched.get("success")),
            "title": NOTIFICATIONS_TAB_TITLE,
            "mode": launched.get("mode"),
            "details": launched.get("details"),
            "log_path": str(NOTIFICATIONS_LOG),
            "created_at": _now_iso(),
        }

    channel["active"] = True
    channel["title"] = NOTIFICATIONS_TAB_TITLE
    channel["log_path"] = str(NOTIFICATIONS_LOG)
    channel["last_level"] = normalized
    channel["last_notified_at"] = _now_iso()
    state["notification_channel"] = channel
    save_state(state)

    return {
        "success": True,
        "level": normalized,
        "summary": summary,
        "hint": hint,
        "channel_launch": launch,
    }


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
    configured = (config.get("pipeline_mode") or {}).get("available") or []
    normalized = [_normalize_mode_key(m) for m in configured if m]
    available = [m for m in normalized if m in PIPELINE_MODES]

    # If nothing valid was configured, start with all known modes.
    if not available:
        available = list(PIPELINE_MODES.keys())

    # Append any missing modes so new additions (e.g., gemini_only) always show up.
    for mode in PIPELINE_MODES.keys():
        if mode not in available:
            available.append(mode)
    return available


def get_selected_mode(
    state: dict[str, Any],
    config: dict[str, Any],
    *,
    emit_warning: bool = True,
) -> str:
    available = get_available_pipeline_modes(config)
    default_mode = (
        (config.get("pipeline_mode") or {}).get("default")
        or (available[0] if available else "default_balanced")
    )
    selected = _normalize_mode_key(state.get("selected_mode") or default_mode)
    if selected not in PIPELINE_MODES or selected not in available:
        if emit_warning:
            warn(f"Invalid stored pipeline mode '{selected}'. Falling back to {default_mode}.")
        if default_mode in PIPELINE_MODES and default_mode in available:
            return default_mode
        return available[0] if available else default_mode
    return selected


def get_mode_details(mode: str, config: dict[str, Any]) -> dict[str, Any]:
    mode = _normalize_mode_key(mode)
    meta = PIPELINE_MODES[mode]
    return {
        "key": mode,
        "label": meta["label"],
        "summary": meta["summary"],
        "model_lines": _build_mode_model_lines(mode, config),
    }

def _normalize_mode_key(mode: str | None) -> str:
    if not mode:
        return "default_balanced"
    aliases = {
        "default": "default_balanced",
        "codex_only": "codex_only_balanced",
        "claude_chief_only": "claude_only_balanced",
        "codex": "codex_only_balanced",
        "claude": "claude_only_balanced",
        "gemini": "gemini_only",
    }
    return aliases.get(mode, mode)


def _build_mode_model_lines(mode: str, config: dict[str, Any]) -> list[str]:
    agents = config.get("agents", {})
    stage_map = PIPELINE_MODES[mode]["stages"]

    def _fmt(agent_key: str | None) -> str | None:
        if not agent_key:
            return None
        agent = agents.get(agent_key)
        if not agent:
            return None
        cmd = agent.get("command", "").lower() or agent_key
        model = agent.get("model", "unknown model")
        model_lower = model.lower()
        # If model already carries the command prefix, drop the extra command tag.
        if model_lower.startswith(f"{cmd}-") or model_lower == cmd:
            return model
        return f"{cmd}-{model}"

    scan = _fmt(stage_map.get("architecture"))
    plan = _fmt(stage_map.get("planning_generation"))
    tasks = _fmt(stage_map.get("task_creation"))
    impl = _fmt(stage_map.get("implementation"))

    lines: list[str] = []
    if scan:
        lines.append(f"// Scan: {scan}     // Scans the repository structure")
    if plan:
        lines.append(f"// Plan: {plan}     // Brainstorms with user about the bug/feature")
    if tasks:
        lines.append(f"// Task: {tasks}     // Creates a task list to use during implementation")
    if impl:
        lines.append(f"// Code: {impl}     // Implements feature/bug based on planning documents")

    # Fallback to a single unknown line if somehow empty
    return lines or ["// Model: unknown"]


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
    details = get_mode_details(mode, config)
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
        return get_mode_details(next_mode, config)
    except RuntimeError as exc:
        warn(str(exc))
        return get_mode_details(current, config)


def prompt_for_pipeline_mode(state: dict[str, Any], config: dict[str, Any]) -> str:
    available = get_available_pipeline_modes(config)
    current = get_selected_mode(state, config)
    # Non-interactive fallback (CI, pipes): keep old numeric input behavior.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print()
        print("  Select pipeline mode:")
        for index, mode in enumerate(available, start=1):
            details = get_mode_details(mode, config)
            marker = " (current)" if mode == current else ""
            print(f"    {index}. {details['label']} [{mode}] - {details['summary']}{marker}")
        print(f"  Press Enter to keep {current}.")
        try:
            choice = input("  Mode: ").strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
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
            details = get_mode_details(mode, config)
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
    cmd = _resolve_agent_command(agent_name, config)
    prompt_mode = config.get("agents", {}).get(agent_name, {}).get("prompt_mode", "stdin")
    display = _agent_display_name(agent_name, config)

    info(f"Invoking {display} ({agent_name}) ...")
    _log(f"AGENT CALL: {agent_name} via {cmd[0]}\n\t(prompt_mode={prompt_mode})")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{agent_name}_last_prompt.txt").write_text(prompt, encoding="utf-8")

    try:
        if prompt_mode == "arg":
            result = run_command(cmd + [prompt], capture=True, timeout=600)
        else:
            result = run_command(cmd, capture=True, input_text=prompt, timeout=600)
        output = result.stdout or ""
        if result.returncode != 0:
            warn(f"Agent {agent_name} exited {result.returncode}")
            _log(f"AGENT STDERR: {(result.stderr or '')[:500]}")
        (LOGS_DIR / f"{agent_name}_last_output.txt").write_text(output, encoding="utf-8")
        return output
    except FileNotFoundError:
        raise RuntimeError(
            f"Agent command not found: '{cmd[0]}'. "
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
    agent_cfg = config.get("agents", {}).get(agent_name) or {}
    command = str(agent_cfg.get("command", agent_name)).capitalize()
    model = agent_cfg.get("model")
    if model:
        return f"{command} ({model})"
    return command


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
        connector = "└ " if i == len(visible) - 1 else "├ "
        lines.append(f"{prefix}{connector}{entry.name}")
        if entry.is_dir():
            ext = "    " if i == len(visible) - 1 else "│   "
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

    agent_key  = resolve_stage_agent_key("architecture", state, config)
    agent_cfg  = config["agents"].get(agent_key, {})
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
        agent_name=_agent_display_name(agent_key, config),
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
        if get_selected_mode(state, config) == "gemini_only":
            raise RuntimeError("Gemini-only mode: architecture agent failed to produce docs/architecture.md.")
        warn(f"Gemini {'exited with code ' + str(rc) if rc != 0 else 'did not produce a valid architecture.md'}.")
        warn("Falling back to Codex for architecture generation ...")

        step(3, 3, "Calling Codex fallback agent ...")
        if ARCHITECTURE_FILE.exists():
            ARCHITECTURE_FILE.unlink()

        fallback_key = "codex_balanced"
        fallback_cmd = _resolve_agent_command(fallback_key, config)
        _, codex_output = _run_agent_background(
            fallback_cmd,
            agent_name=_agent_display_name(fallback_key, config),
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


def _collect_multiline_input(prompt_header: str, initial_text: str = "") -> str:
    """Suspend TUI, collect multi-line input from the user, resume TUI.

    The user types their text and submits by entering '---' on a blank line
    or pressing Ctrl+D / Ctrl+Z.  If *initial_text* is provided it is printed
    as a pre-filled reference before the prompt so the user can re-type/edit it.
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
        if initial_text:
            print(f"\033[2m{initial_text}\033[0m\n")
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


_TASK_DENIAL_CONTEXT_PREFIX = "approval.task_denial"
_TASK_DENIAL_TITLE = "Explain why you are denying this task"
_TASK_DENIAL_PROMPT_TEMPLATE = (
    "Explain why you are denying this task so planning can be rerun with your feedback.\n\n"
    "Task {task_id}: {description}"
)


def _truncate_for_prompt(text: str, max_len: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= max_len else f"{text[: max_len - 3]}..."


def _persist_task_denial_feedback(
    feedback: str,
    context_id: str,
    *,
    service_state: Any | None = None,
) -> None:
    """Persist denial feedback through the state service (best-effort)."""
    try:
        svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
        state_service.complete_multiline_input(
            svc_state,
            feedback,
            MultilineInputMode.TASK_DENIAL_FEEDBACK,
            context_id,
            state_path=STATE_FILE,
        )
    except Exception as exc:  # noqa: BLE001
        warn(f"Unable to persist task denial feedback: {exc}")


def _collect_task_denial_feedback(
    task_id: str,
    task_description: str,
    *,
    source_stage: str = "waiting_approval",
) -> str:
    """
    Collect multiline feedback for a denied task.

    Uses the Rich multiline input flow when an emitter/renderer is active,
    falling back to the legacy inline prompt otherwise.
    """

    context_id = f"{_TASK_DENIAL_CONTEXT_PREFIX}.{task_id}"
    normalized_description = " ".join(task_description.split())

    service_state: Any | None = None
    existing = ""
    try:
        service_state = state_service.load_state(state_path=STATE_FILE)
        existing = (
            service_state.task_denial_feedback_by_task_id.get(context_id, "")
            if getattr(service_state, "task_denial_feedback_by_task_id", None)
            else ""
        )
        active_ctx = getattr(service_state, "multiline_input", None)
        if (
            active_ctx
            and getattr(active_ctx, "id", "") == context_id
            and getattr(active_ctx, "mode", None) == MultilineInputMode.TASK_DENIAL_FEEDBACK
        ):
            existing = (getattr(active_ctx, "value", "") or getattr(active_ctx, "initial_text", "") or existing).strip()
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not load persisted denial feedback: {exc}")

    # Plain/legacy path (no emitter)
    if _EMITTER is None:
        feedback = _collect_multiline_input(
            f"Task denied - what should be different?\nTask {task_id}: {normalized_description}"
        )
        _persist_task_denial_feedback(feedback, context_id, service_state=service_state)
        return feedback.strip()

    # Prepare multiline input context for the TUI path
    ctx = MultilineInputState(
        id=context_id,
        title=_TASK_DENIAL_TITLE,
        prompt=_truncate_for_prompt(
            _TASK_DENIAL_PROMPT_TEMPLATE.format(task_id=task_id, description=normalized_description)
        ),
        initial_text=existing,
        value=existing,
        mode=MultilineInputMode.TASK_DENIAL_FEEDBACK,
        source_stage=source_stage,
    )

    renderer_resumed = False
    if _RENDERER is not None and hasattr(_RENDERER, "resume"):
        try:
            _RENDERER.resume()
            renderer_resumed = True
        except Exception:
            renderer_resumed = False

    try:
        try:
            svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
            service_state = svc_state
            state_service.start_multiline_input(svc_state, ctx, state_path=STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            warn(f"Unable to start multiline denial input UI; falling back to inline prompt: {exc}")
            feedback = _collect_multiline_input(
                f"Task denied - what should be different?\nTask {task_id}: {normalized_description}"
            )
            _persist_task_denial_feedback(feedback, context_id, service_state=service_state)
            return feedback.strip()

        submitted: dict[str, str | None] = {"value": None}
        cancelled = False
        done = threading.Event()

        def _on_event(event: Any) -> None:
            nonlocal service_state, cancelled
            if isinstance(event, SubmitMultilineInputEvent) and event.id == ctx.id:
                try:
                    svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
                    state_service.complete_multiline_input(
                        svc_state,
                        event.value,
                        event.mode,
                        event.id,
                        state_path=STATE_FILE,
                    )
                    service_state = svc_state
                except Exception as exc2:  # noqa: BLE001
                    warn(f"Unable to persist task denial submission: {exc2}")
                submitted["value"] = event.value
                done.set()
            elif isinstance(event, CancelMultilineInputEvent) and event.id == ctx.id:
                try:
                    svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
                    state_service.cancel_multiline_input(svc_state, event.id, state_path=STATE_FILE)
                    service_state = svc_state
                except Exception as exc2:  # noqa: BLE001
                    warn(f"Unable to cancel task denial input cleanly: {exc2}")
                cancelled = True
                done.set()

        _EMITTER.subscribe(_on_event)
        try:
            _EMITTER.open_multiline_input(
                id=ctx.id,
                mode=ctx.mode,
                title=ctx.title,
                prompt=ctx.prompt,
                initial_text=ctx.initial_text,
                source_stage=ctx.source_stage,
            )
            while not done.wait(0.2):
                pass
        finally:
            _EMITTER.unsubscribe(_on_event)

        if cancelled:
            warn("Task denial feedback input cancelled. Aborting run.")
            sys.exit(0)

        feedback = (submitted["value"] or existing).strip()
        _persist_task_denial_feedback(feedback, context_id, service_state=service_state)
        return feedback
    finally:
        if renderer_resumed and _RENDERER is not None and hasattr(_RENDERER, "suspend"):
            try:
                _RENDERER.suspend()
            except Exception:
                pass


_FEATURE_DESCRIPTION_CONTEXT_ID = "planning.feature_description"
_FEATURE_DESCRIPTION_TITLE = "Describe the feature or bug"
_FEATURE_DESCRIPTION_PROMPT = (
    "Provide a clear, multi-paragraph summary of the feature or bug. "
    "This description feeds the planning agents and is saved to docs/user_context.md."
)


def _persist_feature_description(
    description: str,
    state: dict[str, Any],
    *,
    service_state: Any = None,
) -> None:
    """Persist the feature description to disk and keep state in sync."""

    normalized = description.strip()
    if not normalized:
        return

    state["feature_description"] = normalized
    USER_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_CONTEXT_FILE.write_text(normalized, encoding="utf-8")
    try:
        if _LEGACY_USER_CONTEXT_FILE.exists():
            _LEGACY_USER_CONTEXT_FILE.unlink()
    except Exception:
        pass

    try:
        svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
        state_service.complete_multiline_input(
            svc_state,
            normalized,
            MultilineInputMode.FEATURE_DESCRIPTION,
            _FEATURE_DESCRIPTION_CONTEXT_ID,
            state_path=STATE_FILE,
        )
    except Exception as exc:  # noqa: BLE001 - persistence must not crash the run
        warn(f"Could not persist feature description to state service: {exc}")


def _ensure_feature_description(state: dict[str, Any], *, source_stage: str) -> str:
    """
    Ensure the feature description is available, preferring the Rich multiline input
    flow when a renderer/emitter is active, with a plain fallback otherwise.
    """

    existing = (state.get("feature_description") or "").strip()
    if not existing and USER_CONTEXT_FILE.exists():
        existing = USER_CONTEXT_FILE.read_text(encoding="utf-8").strip()
    # Migrate legacy location if present
    if not existing and _LEGACY_USER_CONTEXT_FILE.exists():
        existing = _LEGACY_USER_CONTEXT_FILE.read_text(encoding="utf-8").strip()
        USER_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_CONTEXT_FILE.write_text(existing, encoding="utf-8")
        try:
            _LEGACY_USER_CONTEXT_FILE.unlink()
        except Exception:
            pass

    service_state: Any = None
    try:
        service_state = state_service.load_state(state_path=STATE_FILE)
        if not existing and getattr(service_state, "feature_description", ""):
            existing = service_state.feature_description.strip()
        active_ctx = getattr(service_state, "multiline_input", None)
        if (
            not existing
            and active_ctx
            and getattr(active_ctx, "mode", None) == MultilineInputMode.FEATURE_DESCRIPTION
        ):
            existing = (getattr(active_ctx, "value", "") or getattr(active_ctx, "initial_text", "")).strip()
    except Exception as exc:  # noqa: BLE001 - fall back gracefully
        warn(f"Could not load persisted feature description: {exc}")

    if existing:
        _persist_feature_description(existing, state, service_state=service_state)
        return existing

    # No TUI renderer: use the legacy inline prompt.
    if _EMITTER is None:
        description = _collect_multiline_input("A.I.N. - Describe the feature or bug")
        if not description:
            warn("No description provided. Please re-run and describe your feature.")
            sys.exit(0)
        _persist_feature_description(description, state, service_state=service_state)
        return description

    if service_state is None:
        try:
            service_state = state_service.load_state(state_path=STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            warn(f"Could not prepare multiline input state. Falling back to inline prompt: {exc}")
            description = _collect_multiline_input("A.I.N. - Describe the feature or bug")
            if not description:
                warn("No description provided. Please re-run and describe your feature.")
                sys.exit(0)
            _persist_feature_description(description, state)
            return description

    ctx = MultilineInputState(
        id=_FEATURE_DESCRIPTION_CONTEXT_ID,
        title=_FEATURE_DESCRIPTION_TITLE,
        prompt=_FEATURE_DESCRIPTION_PROMPT,
        initial_text=existing,
        value=existing,
        mode=MultilineInputMode.FEATURE_DESCRIPTION,
        source_stage=source_stage,
    )

    try:
        state_service.start_multiline_input(service_state, ctx, state_path=STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        warn(f"Unable to start multiline input UI; falling back to inline prompt: {exc}")
        description = _collect_multiline_input("A.I.N. - Describe the feature or bug")
        if not description:
            warn("No description provided. Please re-run and describe your feature.")
            sys.exit(0)
        _persist_feature_description(description, state, service_state=service_state)
        return description

    submitted: dict[str, str | None] = {"value": None}
    cancelled = False
    done = threading.Event()

    def _on_event(event: Any) -> None:
        nonlocal service_state, cancelled
        if isinstance(event, SubmitMultilineInputEvent) and event.id == ctx.id:
            try:
                svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
                state_service.complete_multiline_input(
                    svc_state,
                    event.value,
                    event.mode,
                    event.id,
                    state_path=STATE_FILE,
                )
                service_state = svc_state
            except Exception as exc2:  # noqa: BLE001
                warn(f"Unable to persist submitted description: {exc2}")
            submitted["value"] = event.value
            done.set()
        elif isinstance(event, CancelMultilineInputEvent) and event.id == ctx.id:
            try:
                svc_state = service_state or state_service.load_state(state_path=STATE_FILE)
                state_service.cancel_multiline_input(svc_state, event.id, state_path=STATE_FILE)
                service_state = svc_state
            except Exception as exc2:  # noqa: BLE001
                warn(f"Unable to cancel feature description input cleanly: {exc2}")
            cancelled = True
            done.set()

    _EMITTER.subscribe(_on_event)
    try:
        _EMITTER.open_multiline_input(
            id=ctx.id,
            mode=ctx.mode,
            title=ctx.title,
            prompt=ctx.prompt,
            initial_text=ctx.initial_text,
            source_stage=ctx.source_stage,
        )
        while not done.wait(0.2):
            pass
    finally:
        _EMITTER.unsubscribe(_on_event)

    if cancelled:
        warn("Feature description input cancelled. Aborting run.")
        sys.exit(0)

    description = (submitted["value"] or "").strip()
    if not description:
        warn("Feature description cannot be empty.")
        sys.exit(0)

    _persist_feature_description(description, state, service_state=service_state)
    return description


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


def _extract_tasks_for_review() -> list[dict[str, Any]]:
    """Load tasks (id + description) from TASK_GRAPH.json, with TASKS.md fallback."""
    if TASK_GRAPH_FILE.exists():
        try:
            data = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
            tasks: list[dict[str, Any]] = []
            for t in data.get("tasks", []):
                desc = str(t.get("description", "")).strip()
                if not desc:
                    continue
                tid = t.get("id", len(tasks) + 1)
                tasks.append({"id": str(tid), "description": desc})
            if tasks:
                return tasks
        except Exception:
            pass

    if TASKS_FILE.exists():
        tasks: list[dict[str, Any]] = []
        for i, line in enumerate(TASKS_FILE.read_text(encoding="utf-8").splitlines(), start=1):
            s = line.strip()
            if s.startswith("- [ ] ") or s.startswith("- [x] "):
                tasks.append({"id": str(i), "description": s[6:].strip()})
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


def _render_task_review_popup(tasks: list[dict[str, Any]], selected_row: int, decisions: list[bool]) -> None:
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
        task_id = str(task.get("id", idx + 1))
        if task_id.isdigit() and len(task_id) < 2:
            task_label = task_id.zfill(2)
        else:
            task_label = task_id
        task_desc = task.get("description", "")
        is_selected = idx == selected_row
        cursor = "► " if is_selected else "  "
        cursor_style = f"bold {rich_pink}" if is_selected else rich_dim
        row_style = f"bold {rich_pink}" if is_selected else rich_cyan
        status = "ACCEPT" if decisions[idx] else "DENY"
        status_style = rich_cyan if decisions[idx] else "yellow"
        body.append(cursor, style=cursor_style)
        body.append(f"{task_label}. {task_desc}\n", style=row_style)
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


def _review_tasks_with_popup(tasks: list[dict[str, Any]]) -> tuple[bool, str]:
    """Interactive task review. Returns (approved, feedback)."""
    if _RENDERER is not None and hasattr(_RENDERER, "request_task_approval"):
        normalized_tasks = [
            {
                "id": str(task.get("id", idx + 1)),
                "description": str(task.get("description", "")).strip(),
            }
            for idx, task in enumerate(tasks)
        ]
        return _RENDERER.request_task_approval(normalized_tasks)

    if _RENDERER is not None and hasattr(_RENDERER, "request_input"):
        info("Review task list before implementation:")
        for task in tasks:
            task_id = str(task.get("id", ""))
            task_desc = str(task.get("description", "")).strip()
            if task_desc:
                info(f"  [{task_id}] {task_desc}")

        while True:
            choice = _RENDERER.request_input(
                "Task review: type 'approve' or 'deny' (q to cancel run)"
            ).strip().lower()
            if choice in {"approve", "a", "yes", "y"}:
                return True, ""
            if choice in {"deny", "d", "no", "n"}:
                feedback = _collect_task_denial_feedback("task_list", "Task list")
                return False, feedback
            if choice in {"q", "quit", "cancel"}:
                raise KeyboardInterrupt
            warn("Please enter approve or deny.")

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print()
        choice = input("  Approve generated tasks? [y/n]: ").strip().lower()
        if choice in ("y", "yes"):
            return True, ""
        feedback = _collect_task_denial_feedback("task_list", "Task list")
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
            denied_indices = [i for i, decision in enumerate(decisions) if not decision]
            feedback_entries: list[str] = []
            for idx in denied_indices:
                task = tasks[idx]
                task_id = str(task.get("id", idx + 1))
                task_desc = task.get("description", f"Task {task_id}")
                feedback = _collect_task_denial_feedback(task_id, task_desc)
                if feedback.strip():
                    feedback_entries.append(f"[{task_id}] {feedback.strip()}")
            combined_feedback = "\n".join(feedback_entries).strip()
            return False, combined_feedback
        elif key == "quit":
            raise KeyboardInterrupt


def _rerun_planning_and_task_creation(state: dict[str, Any], config: dict[str, Any], feedback: str) -> dict[str, Any]:
    _migrate_task_feedback_file()
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
    _migrate_context_files()

    content = _ensure_feature_description(state, source_stage="user_context")
    if not content:
        # Defensive fallback; _ensure_feature_description exits on empty input.
        warn("No description provided. Please re-run and describe your feature.")
        sys.exit(0)

    success("Feature context saved.")
    set_stage("planning_questions", state)


# Stage 3: Planning Questions (Codex)
# 

def run_planning_questions(state: dict, config: dict) -> None:
    # Track how many question rounds we've run to avoid infinite loops.
    state["planning_round"] = int(state.get("planning_round", 0)) + 1
    save_state(state)

    mode = get_selected_mode(state, config)
    agent_key = resolve_stage_agent_key("planning_questions", state, config)
    banner(f"Stage: Planning  Brainstorm [mode={mode} agent={agent_key}]")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_context_files()

    feature_description = _ensure_feature_description(state, source_stage="planning_questions")
    arch_ctx = ARCHITECTURE_FILE.read_text(encoding="utf-8") if ARCHITECTURE_FILE.exists() else ""

    BRAINSTORM_CONTEXT_FILE.write_text(
        f"# Brainstorm Context\n\n## Feature Request\n\n{feature_description}\n\n"
        f"## Architecture Overview\n\n{arch_ctx[:4000]}\n",
        encoding="utf-8",
    )
    try:
        if _LEGACY_BRAINSTORM_CONTEXT_FILE.exists():
            _LEGACY_BRAINSTORM_CONTEXT_FILE.unlink()
    except Exception:
        pass

    ctx_path = BRAINSTORM_CONTEXT_FILE.relative_to(REPO_ROOT)
    prompt = (
        "You are the planning brainstorm agent. Do NOT call any tools or attempt to read/write files. "
        "Read the context below and output the final markdown to stdout only; AIN will save it.\n\n"
        f"Context file path (for reference only): {ctx_path}\n"
        f"Print markdown with this structure:\n"
        "# Open Questions\n"
        "- Q: <question>\n  A: <answer or Needs input>\n"
        "Limit to the 10 most important uncertainties. Keep questions short and specific.\n\n"
        "---\n"
        "# Feature Request\n"
        f"{feature_description}\n\n"
        "# Architecture Overview\n"
        f"{arch_ctx[:4000]}\n"
        "---"
    )

    live = _EMITTER is not None
    call = (
        (lambda: _call_agent_live(agent_key, prompt, config, log_slug="planning_questions"))
        if live
        else (lambda: call_agent(agent_key, prompt, config))
    )
    output = call().strip()

    if output:
        OPEN_QUESTIONS_FILE.write_text(output, encoding="utf-8")

    if (not OPEN_QUESTIONS_FILE.exists() or OPEN_QUESTIONS_FILE.stat().st_size == 0) and "gemini" in agent_key:
        info("Primary brainstorm agent did not produce output; opening interactive Gemini brainstorm session ...")
        _run_agent_interactive(agent_key, prompt, config, header="Gemini Brainstorm Session")

    if not OPEN_QUESTIONS_FILE.exists() or OPEN_QUESTIONS_FILE.stat().st_size == 0:
        raise RuntimeError("Planning brainstorm agent did not produce docs/OPEN_QUESTIONS.md.")

    success(f"Questions written: {OPEN_QUESTIONS_FILE.relative_to(REPO_ROOT)}")
    set_stage("planning_answers", state)


def _build_answers_template() -> str:
    if not OPEN_QUESTIONS_FILE.exists():
        return ""
    lines = OPEN_QUESTIONS_FILE.read_text(encoding="utf-8").splitlines()
    questions: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("- Q:"):
            questions.append(stripped.split("Q:", 1)[1].strip())
    if not questions:
        return ""
    parts: list[str] = []
    for idx, q in enumerate(questions, start=1):
        parts.append(f"{idx}. {q}\nA: \n")
    return "\n".join(parts).strip() + "\n"


def run_planning_answers(state: dict, config: dict) -> None:
    banner("Stage: Planning  Answers")
    _migrate_context_files()

    if not OPEN_QUESTIONS_FILE.exists():
        raise RuntimeError("docs/OPEN_QUESTIONS.md not found. Run planning_questions first.")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    existing = OPEN_ANSWERS_FILE.read_text(encoding="utf-8") if OPEN_ANSWERS_FILE.exists() else ""
    template = _build_answers_template()
    initial_text = existing or template
    prompt_text = (
        "Answer each question below on the A: line. Keep answers concise.\n"
        "If something is still unclear, write 'Needs input'. "
        "These answers are saved to docs/OPEN_ANSWERS.md."
    )

    if _EMITTER is None:
        answers = _collect_multiline_input("Answer planning questions", initial_text=initial_text).strip()
        if not answers:
            warn("No answers provided. Re-run to continue.")
            sys.exit(0)
        OPEN_ANSWERS_FILE.write_text(answers, encoding="utf-8")
        success(f"Answers written: {OPEN_ANSWERS_FILE.relative_to(REPO_ROOT)}")
        set_stage("planning_generation", state)
        return

    ctx = MultilineInputState(
        id="planning.open_answers",
        title="Answer planning questions",
        prompt=prompt_text,
        initial_text=initial_text,
        value=initial_text,
        mode=MultilineInputMode.PLANNING_ANSWERS,
        source_stage="planning_answers",
    )

    submitted: dict[str, str | None] = {"value": None}
    cancelled = False
    done = threading.Event()

    def _on_event(event: Any) -> None:
        nonlocal cancelled
        if isinstance(event, SubmitMultilineInputEvent) and event.id == ctx.id:
            submitted["value"] = event.value
            done.set()
        elif isinstance(event, CancelMultilineInputEvent) and event.id == ctx.id:
            cancelled = True
            done.set()

    _EMITTER.subscribe(_on_event)
    try:
        _EMITTER.open_multiline_input(
            id=ctx.id,
            mode=ctx.mode,
            title=ctx.title,
            prompt=ctx.prompt,
            initial_text=ctx.initial_text,
            source_stage=ctx.source_stage,
        )
        while not done.wait(0.2):
            pass
    finally:
        _EMITTER.unsubscribe(_on_event)

    if cancelled:
        warn("Planning answers input cancelled. Aborting run.")
        sys.exit(0)

    answers = (submitted["value"] or "").strip()
    if not answers:
        warn("No answers provided. Re-run to continue.")
        sys.exit(0)

    OPEN_ANSWERS_FILE.write_text(answers, encoding="utf-8")
    success(f"Answers written: {OPEN_ANSWERS_FILE.relative_to(REPO_ROOT)}")
    # If unclear answers remain, loop once more through questions.
    needs_more = ("needs input" in answers.lower()) or re.search(r"A:\s*$", answers, re.MULTILINE)
    if needs_more and state.get("planning_round", 1) < 2:
        info("Unclear answers detected. Running a second round of brainstorming.")
        set_stage("planning_questions", state)
    else:
        set_stage("planning_generation", state)


# 
# Stage 4: Planning Generation (Codex)
# 

def _planning_direct_write_prompt() -> str:
    return (
        "Read docs/user_context.md, docs/OPEN_QUESTIONS.md, and docs/architecture.md "
        "to understand the feature request. IMPORTANT: Do NOT call tools or write files. "
        "Print the content for three documents to stdout only, using file markers so AIN can save them:\n"
        "<!-- FILE: docs/PRD.md -->\n<content>\n<!-- END: docs/PRD.md -->\n"
        "<!-- FILE: docs/DESIGN.md -->\n<content>\n<!-- END: docs/DESIGN.md -->\n"
        "<!-- FILE: docs/FEATURE_SPEC.md -->\n<content>\n<!-- END: docs/FEATURE_SPEC.md -->\n"
        "PRD headings: # Problem, # Goals, # Non Goals, # User Stories, # Success Criteria. "
        "DESIGN headings: # Architecture Changes, # Data Model, # API Changes, # UI Changes, # Risks. "
        "FEATURE_SPEC: detailed technical spec. Generate all three now without further questions."
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
            agent_name=f"{_agent_display_name(agent_key, config)} - planning",
            log_slug="planning_generation",
        )
    else:
        rc, output = _run_agent_background(
            cmd,
            agent_name=f"{_agent_display_name(agent_key, config)} - planning",
            log_slug="planning_generation",
            input_text=prompt,
        )
    if output.strip():
        _parse_and_write_planning_docs(output)
    return rc == 0 and all(
        doc.exists() and doc.stat().st_size > 0
        for doc in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]
    )


def _run_agent_interactive(agent_key: str, prompt: str, config: dict[str, Any], *, header: str) -> None:
    """Suspend the TUI and launch the agent in the foreground, piping the prompt."""
    agent_cfg = (config.get("agents", {}) or {}).get(agent_key, {})
    cmd = _resolve_agent_command(agent_key, config)
    prompt_mode = agent_cfg.get("prompt_mode", "stdin")

    info(f"Suspending TUI  launching {_agent_display_name(agent_key, config)} interactively ...")
    if _RENDERER is not None:
        _RENDERER.suspend()
    try:
        # Clear alt buffer for a clean session.
        sys.stdout.write("\033[#25h\033[0m\033[2J\033[H")
        sys.stdout.flush()
        print(f"\033[1;96m   {header} \033[0m")
        print("  The prompt has been sent. Let the agent finish, then exit to return to AIN.\n")
        if prompt_mode == "arg":
            proc = subprocess.Popen(cmd + [prompt], cwd=str(REPO_ROOT))
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
    finally:
        # Restore alt buffer and resume TUI.
        sys.stdout.write("\033[#1049l\033[#25h\033[0m\r\n")
        sys.stdout.flush()
        time.sleep(0.5)
        if _RENDERER is not None:
            _RENDERER.resume()


def _run_planning_fallback_claude(prompt: str) -> None:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        warn("claude not found  no further planning fallback available.")
        return
    _, output = _run_agent_background(
        [claude_bin, "--print"],
        agent_name="Claude - planning",
        log_slug="planning_generation_claude",
        input_text=prompt,
    )
    if output.strip():
        _parse_and_write_planning_docs(output)


def _find_answer(label: str, text: str) -> str | None:
    """Return the answer line matching **A#:** from OPEN_QUESTIONS.md."""
    match = re.search(rf"\*\*{re.escape(label)}:\*\*\s*(.+)", text)
    if match:
        return match.group(1).strip()
    return None


def _first_inline_code(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"`([^`]+)`", text)
    return match.group(1).strip() if match else None


def _upsert_planned_file_change(state: dict[str, Any], change: dict[str, Any]) -> None:
    """Insert or replace a planned file change in state.planned_file_changes."""
    changes: list[Any] = state.setdefault("planned_file_changes", [])
    normalized: list[dict[str, Any]] = []
    inserted = False

    for existing in changes:
        existing_dict = existing if isinstance(existing, dict) else {
            "path": getattr(existing, "path", None),
            "content": getattr(existing, "content", None),
            "operation": getattr(existing, "operation", None),
            "allow_overwrite": getattr(existing, "allow_overwrite", None),
            "ensure_parent_dir": getattr(existing, "ensure_parent_dir", None),
        }
        if existing_dict.get("path") == change.get("path"):
            normalized.append(change)
            inserted = True
        else:
            normalized.append(existing_dict)

    if not inserted:
        normalized.append(change)

    state["planned_file_changes"] = normalized


def apply_planned_file_change(
    change: PlannedFileChange | dict[str, Any],
    *,
    emitter: Emitter | None = None,
    repo_root: Path | None = None,
) -> str:
    """Apply a single planned file change to the local filesystem.

    Returns a status string: ``"created"``, ``"overwritten"``, or ``"skipped"``.
    """
    emitter = emitter if emitter is not None else _EMITTER
    root = (repo_root or REPO_ROOT).resolve()

    if isinstance(change, PlannedFileChange):
        path = change.path
        content = change.content
        operation = change.operation or "create"
        allow_overwrite = change.allow_overwrite
        ensure_parent_dir = change.ensure_parent_dir
    elif isinstance(change, dict):
        path = change.get("path")
        content = change.get("content", "")
        operation = change.get("operation", "create")
        allow_overwrite = bool(change.get("allow_overwrite", False))
        ensure_parent_dir = bool(change.get("ensure_parent_dir", True))
    else:
        raise TypeError("change must be a PlannedFileChange or dict")

    if not isinstance(path, str) or not path:
        raise ValueError("planned file change requires a non-empty path")
    if not isinstance(content, str):
        raise ValueError("planned file change content must be a string")

    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise RuntimeError(f"Planned file change path '{path}' escapes the repository root.")

    if ensure_parent_dir:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        raise RuntimeError(f"Parent directory does not exist for {path} and ensure_parent_dir=False")

    op = operation or "create"
    if emitter is not None:
        emitter.planned_file_change_started(path, op)

    exists = target.exists()
    if exists:
        if op == "skip_if_exists":
            info(f"Skipped {path} (already exists).")
            if emitter is not None:
                emitter.planned_file_change_completed(path, op, status="skipped")
            return "skipped"
        if not allow_overwrite:
            info(f"Skipped {path} (already exists, overwrite disabled).")
            if emitter is not None:
                emitter.planned_file_change_completed(path, op, status="skipped")
            return "skipped"

    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{target.name}.", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        Path(tmp_name).replace(target)
    except Exception as exc:  # noqa: BLE001 - propagate after emitting failure
        if emitter is not None:
            emitter.planned_file_change_completed(path, op, status="failed", error=str(exc))
        try:
            if tmp_name:
                tmp_path = Path(tmp_name)
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    status = "overwritten" if exists else "created"
    if emitter is not None:
        emitter.planned_file_change_completed(path, op, status=status)
    if exists:
        info(f"Overwrote {path} with planned content.")
    else:
        info(f"Created {path} with planned content.")
    return status


def run_planning_generation(state: dict, config: dict) -> None:
    mode = get_selected_mode(state, config)
    agent_key = resolve_stage_agent_key("planning_generation", state, config)
    banner(f"Stage: Planning  Generation [mode={mode} agent={agent_key}]")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    _ensure_feature_description(state, source_stage="planning_generation")

    prompt_file = PROMPTS_DIR / "planning_generation_prompt.md"
    ctx_files = [
        f for f in [
            OPEN_QUESTIONS_FILE, OPEN_ANSWERS_FILE,
            ARCHITECTURE_FILE, USER_CONTEXT_FILE,
            TASK_REVIEW_FEEDBACK_FILE,
        ] if f.exists()
    ]
    prompt = build_prompt(prompt_file, *ctx_files)
    planning_prompt = (
        "IMPORTANT: Do NOT call any tools or attempt filesystem writes. Output all results to stdout only.\n\n"
        + prompt
        + "\n\n---\n\n"
        + _planning_direct_write_prompt()
    )
    _clear_plan_docs()

    agent_cfg = config.get("agents", {}).get(agent_key, {})
    command_name = str(agent_cfg.get("command", agent_key)).lower()

    planning_ok = False
    if "codex" in command_name:
        planning_ok = _run_planning_in_background(agent_key, planning_prompt, config)
    else:
        resolved_cmd = _resolve_agent_command(agent_key, config)
        rc, output = _run_agent_background(
            resolved_cmd,
            agent_name=f"{_agent_display_name(agent_key, config)} - planning",
            log_slug="planning_generation",
            input_text=planning_prompt,
        )
        if rc == 0 and output.strip():
            _parse_and_write_planning_docs(output)
            planning_ok = all(
                doc.exists() and doc.stat().st_size > 0
                for doc in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]
            )
        if not planning_ok:
            warn("Primary planning agent did not write all planning docs.")

    if not planning_ok:
        if mode == "gemini_only":
            info("Gemini-only mode: opening interactive Gemini planning session ...")
            _run_agent_interactive(agent_key, planning_prompt, config, header="Gemini Planning Session")
            planning_ok = all(
                doc.exists() and doc.stat().st_size > 0
                for doc in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]
            )
            if not planning_ok:
                raise RuntimeError("Gemini-only mode: planning agent failed to produce planning docs.")
        else:
            warn("Falling back to claude --print for planning docs ...")
            _run_planning_fallback_claude(prompt)

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
# Stage 5: Task Creation
# 


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


def _call_agent_live(agent_name: str, prompt: str, config: dict, *, log_slug: str) -> str:
    """Run an agent while streaming output into AGENT.OUTPUT, then return captured text."""
    cmd = _resolve_agent_command(agent_name, config)
    prompt_mode = config.get("agents", {}).get(agent_name, {}).get("prompt_mode", "stdin")

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

    call = (
        lambda: _call_agent_live(agent_key, prompt, config, log_slug="task_creation")
        if live_task_output
        else call_agent(agent_key, prompt, config)
    )
    output = call()
    if not output.strip():
        raise RuntimeError("Task creation agent returned empty output.")

    _parse_and_write_task_artifacts(output)
    if not TASKS_FILE.exists():
        TASKS_FILE.write_text(output, encoding="utf-8")

    if not validate_tasks_file(TASKS_FILE):
        if mode == "gemini_only":
            raise RuntimeError("Gemini-only mode: task creation agent did not produce a valid TASKS.md.")
        warn("Primary task creation agent did not produce a valid TASKS.md. Falling back to Codex Max ...")
        fallback = (
            _call_agent_live("task_creation_codex", prompt, config, log_slug="task_creation_fallback")
            if live_task_output
            else call_agent("task_creation_codex", prompt, config)
        )
        if not fallback.strip():
            raise RuntimeError("Codex task creation returned empty output during fallback.")
        _parse_and_write_task_artifacts(fallback)
        if not TASKS_FILE.exists():
            TASKS_FILE.write_text(fallback, encoding="utf-8")

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
    _migrate_task_feedback_file()

    if PLANNING_APPROVED_FLAG.exists():
        success("Planning approved. Advancing to implementation.")
        set_stage("implementation", state)
        return

    while True:
        _emit(AwaitingApproval(run_id="", stage_id="waiting_approval"))
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

        using_tui_task_review = _RENDERER is not None and (
            hasattr(_RENDERER, "request_task_approval") or hasattr(_RENDERER, "request_input")
        )
        if _RENDERER is not None and not using_tui_task_review:
            _RENDERER.suspend()
        try:
            approved, feedback = _review_tasks_with_popup(tasks)
        finally:
            if _RENDERER is not None and not using_tui_task_review:
                _RENDERER.resume()

        if approved:
            APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
            approved_at = datetime.now(timezone.utc).isoformat()
            PLANNING_APPROVED_FLAG.write_text(f"Approved: {approved_at}\n", encoding="utf-8")
            _emit(ApprovalReceived(run_id="", actor="user", at=approved_at))
            success("Tasks approved. Advancing to implementation.")
            set_stage("implementation", state)
            for f in [TASK_REVIEW_FEEDBACK_FILE, _LEGACY_TASK_REVIEW_FEEDBACK_FILE]:
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass
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
        STATE_LOGS_DIR,
        APPROVALS_DIR,
        PIPELINE_DIR / "state",
    ]


def _clean_docs_dir() -> list[str]:
    removed: list[str] = []
    if not DOCS_DIR.exists():
        return removed

    for entry in DOCS_DIR.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
            removed.append(_display_path(entry) + "/")
        else:
            entry.unlink()
            removed.append(_display_path(entry))

    return removed


def clean_workspace(silent: bool = False) -> None:
    """Delete all per-run generated files and reset pipeline state to idle.

    Preserves: config.json, prompts/, CLAUDE.md, and all source code.
    Called automatically after a successful auto-commit, or manually via --clean.
    """
    removed: list[str] = _clean_docs_dir()

    for f in _clean_files():
        if f.exists():
            f.unlink()
            removed.append(_display_path(f))

    for d in _clean_dirs():
        if d.exists():
            shutil.rmtree(d)
            removed.append(_display_path(d) + "/")

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
    _tui_suspend()
    try:
        try:
            choice = input("  Choice [f/s] (default: f): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return choice != "s"
    finally:
        _tui_resume()


def _call_agent_with_fallback(
    agent_name: str,
    prompt: str,
    state: dict[str, Any],
    config: dict,
) -> str:
    """Call an implementation agent; on non-zero exit, roll back and invoke codex fallback."""
    cmd = _resolve_agent_command(agent_name, config)
    prompt_mode = config.get("agents", {}).get(agent_name, {}).get("prompt_mode", "stdin")
    display = _agent_display_name(agent_name, config)

    info(f"Invoking {display} ({agent_name}) ...")
    _log(f"AGENT CALL: {agent_name} via {cmd[0]}\n\t(prompt_mode={prompt_mode})")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{agent_name}_last_prompt.txt").write_text(prompt, encoding="utf-8")

    try:
        if prompt_mode == "arg":
            returncode, output = _run_agent_background(
                cmd + [prompt],
                agent_name=display,
                log_slug="implementation",
            )
        else:
            returncode, output = _run_agent_background(
                cmd,
                agent_name=display,
                log_slug="implementation",
                input_text=prompt,
            )
        stderr = ""
    except FileNotFoundError:
        raise RuntimeError(
            f"Agent command not found: '{cmd[0]}'. "
            f"Edit .ai-pipeline/config.json to configure the '{agent_name}' agent."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agent '{agent_name}' timed out after 600 seconds.")
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        if _EMITTER is not None:
            raise
        warn(f"Agent {agent_name} failed: {exc}")
        if get_selected_mode(state, config) == "gemini_only":
            raise RuntimeError(f"Implementation agent failed in gemini-only mode: {exc}")
        info("Auto-switching to codex fallback ...")
        rolled = rollback_implementation_files()
        if rolled:
            for f in rolled:
                info(f"  Rolled back: {f}")
        return invoke_codex_fallback(prompt, config)

    (LOGS_DIR / f"{agent_name}_last_output.txt").write_text(output, encoding="utf-8")

    if returncode != 0:
        warn(f"Agent {agent_name} exited {returncode}")
        _log(f"AGENT STDERR: {stderr[:500]}")

        # Exit code 1 (token exhaustion or any error)  auto-trigger codex fallback
        if get_selected_mode(state, config) == "gemini_only":
            raise RuntimeError("Implementation agent failed and codex fallback is disabled in gemini-only mode.")
        info("Auto-switching to codex fallback ...")
        rolled = rollback_implementation_files()
        if rolled:
            for f in rolled:
                info(f"  Rolled back: {f}")
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
    workspace_before = _workspace_status_snapshot()
    try:
        task_prompt = _build_task_prompt(task, prompt_file)
        _call_agent_with_fallback(agent_key, task_prompt, state, config)

        _emit_workspace_delta(agent_name, workspace_before)

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

    log_lines: list[str] = [
        "# Implementation Log",
        f"\nStarted: {datetime.now(timezone.utc).isoformat()}",
        f"Branch: {state.get('branch', 'unknown')}",
        "",
    ]

    planned_changes = state.get("planned_file_changes") or []
    if planned_changes:
        info("Applying planned file changes ...")
        log_lines.append("## Planned File Changes")
        for change in planned_changes:
            change_path = getattr(change, "path", None) if hasattr(change, "path") else None
            if change_path is None and isinstance(change, dict):
                change_path = change.get("path")
            try:
                status = apply_planned_file_change(change, emitter=_EMITTER, repo_root=REPO_ROOT)
                log_lines.append(f"{change_path}: {status}")
            except Exception as exc:
                log_lines.append(f"{change_path or 'unknown'}: FAILED ({exc})")
                raise
        log_lines += ["", "---", ""]
    else:
        info("No planned file changes to apply.")

    if not pending:
        success("All tasks already completed.")
        IMPLEMENTATION_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
        set_stage("validation", state)
        return

    info(f"Tasks: {len(tasks)} total | {len(pending)} pending")
    print()

    prompt_file = PROMPTS_DIR / "implementation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

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
    success(f"Log  {_display_path(IMPLEMENTATION_LOG_FILE)}")
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
    success(f"Log  {_display_path(val_log)}")

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
AGENT_CURL_INSTALLS: dict[str, str] = {}


def _install_via_npm(command: str, pkg: str, npm_cmd: str | None = None) -> bool:
    """Install an npm package globally. Returns True on success."""
    npm_exec = npm_cmd or shutil.which("npm")
    if not npm_exec:
        warn(f"{command}  npm not found, cannot install {pkg}")
        warn(f"  Manual install: npm install -g {pkg}")
        return False

    info(f"{command}  not found, installing {pkg} ...")
    try:
        result = run_command([npm_exec, "install", "-g", pkg], capture=True, timeout=120)
    except FileNotFoundError:
        warn(f"{command}  npm not found on PATH, cannot install {pkg}")
        warn(f"  Manual install: npm install -g {pkg}")
        return False
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

    npm_cmd = shutil.which("npm")
    if not npm_cmd:
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
            if npm_cmd:
                _install_via_npm(command, AGENT_NPM_PACKAGES[command], npm_cmd=npm_cmd)
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

    for src in (res_files("ain") / "data" / "prompts").iterdir():
        if not src.name.endswith(".md"):
            continue
        target = PROMPTS_DIR / src.name
        if not target.exists():
            target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            success(f"Created {_display_path(target)}")
        else:
            info(f"Skipped {_display_path(target)} (already exists)")

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
    current = state.get("current_stage", "unknown")
    completed = state.get("completed_stages", [])
    config = load_config()
    mode = get_selected_mode(state, config)
    details = get_mode_details(mode, config)
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
    "planning_answers":    run_planning_answers,
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
    global _EMITTER, _RENDERER
    _EMITTER = emitter
    _RENDERER = renderer

    ensure_config()
    config = load_config()
    state  = load_state(config)
    _APPROVAL_EVENT.clear()
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
        _emit(RunCompleted(run_id="", ended_at=_now_iso(), status=RunStatus.FAILED))
        return

    if renderer is not None and hasattr(renderer, "configure_mode_controls"):
        def _cycle_mode_from_tui() -> dict[str, str]:
            fresh_config = load_config()
            return cycle_pipeline_mode(load_state(fresh_config), fresh_config)

        renderer.configure_mode_controls(
            get_mode_details(get_selected_mode(state, config), config),
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

    _emit(RunStarted(run_id="", started_at=_now_iso(), mode=mode))

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
            _emit(RunCompleted(run_id="", ended_at=ended_at, status=RunStatus.INTERRUPTED))
            warn("\nInterrupted by user.")
            sys.exit(0)
        except Exception as e:  # noqa: BLE001 - error fencing
            ended_at = _now_iso()
            duration_ms = int((time.perf_counter() - t0) * 1000)
            err_msg = str(e)
            err_code = getattr(e, "code", None)
            failure_reason = classify_agent_failure(err_msg)
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
            _emit(RunCompleted(run_id="", ended_at=ended_at, status=RunStatus.FAILED))
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
                "recoverable": failure_reason in {"token_exhaustion", "no_response"},
            }
            state["last_attempted_stage"] = stage
            if failure_reason in {"token_exhaustion", "no_response"}:
                completed = state.get("completed_stages", [])
                eligible = [s for s in completed if s in STAGES and STAGES.index(s) < STAGES.index(stage)]
                state["last_safe_stage"] = eligible[-1] if eligible else "idle"
                save_state(state)
                pause_pipeline(failure_reason, err_msg, f"Run: ain run --resume {stage}")
                try:
                    notify("warning", f"Pipeline paused during {stage_label}", f"Run: ain run --resume {stage}")
                except Exception:
                    pass
                return
            state["status"] = FAILED
            state["current_stage"] = stage
            save_state(state)
            fail_pipeline(state, err_msg)

    state = load_state(config)
    if state["current_stage"] == "done":
        banner("Pipeline Complete")
        show_status(state)
        _emit(RunCompleted(run_id="", ended_at=_now_iso(), status=RunStatus.DONE))

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
        _emit(ApprovalReceived(run_id="", actor="user", at=approved_at))
        state = load_state()
        if state["current_stage"] == "waiting_approval":
            set_stage("implementation", state)
            success("Advanced to implementation. Run: ain run")
        return

    if args.status or args.command == "status":
        if getattr(args, "json", False):
            service_state = state_service.load_state(state_path=STATE_FILE)
            state = load_state_with_backfill(service_state.to_dict(), load_config())
            payload = {
                "pipeline_state": state,
                "health": config_service.get_health_summary(REPO_ROOT).__dict__,
            }
            _print_json(payload)
        else:
            state = load_state()
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
        plain  = getattr(args, "plain", False)
        interactive_mode_prompt = bool(
            sys.stdin is not None
            and sys.stdout is not None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        )
        if plain and interactive_mode_prompt:
            selected_mode = prompt_for_pipeline_mode(state, config)
        else:
            selected_mode = get_selected_mode(state, config)
        if selected_mode != get_selected_mode(state, config):
            set_pipeline_mode(selected_mode, state, config)
        single = bool(getattr(args, "stage", None))
        _run_with_tui(
            start_stage=getattr(args, "resume", None) or getattr(args, "stage", None),
            single_stage=single,
            plain=plain,
            prompt_mode_selection=interactive_mode_prompt and not plain,
        )
        return

    if args.command == "resume":
        plain = getattr(args, "plain", False)
        _run_with_tui(start_stage=args.stage, plain=plain)
        return

    if args.command == "continue":
        state = load_state()
        stage = resolve_continue_stage(state)
        if stage is None:
            success("Pipeline is already complete.")
            show_status(state)
            return
        info(f"Continuing from: {STAGE_LABELS.get(stage, stage)}")
        _run_with_tui(start_stage=stage)
        return

    # No subcommand and no flag  show help
    parser.print_help()


def _run_with_tui(
    start_stage: str | None = None,
    single_stage: bool = False,
    plain: bool = False,
    prompt_mode_selection: bool = False,
) -> None:
    """Launch run_pipeline, wrapping in the Rich TUI unless --plain is set."""
    if plain:
        _run_pipeline_compat(start_stage=start_stage, single_stage=single_stage, mode="plain")
        return

    exit_code: int | None = None
    persistent_error: str | None = None
    should_clean_on_exit = False
    try:
        from ain.tui import RichRenderer
        from ain.runtime.emitter import Emitter
        from rich.console import Console as _Console
        from ain import __version__ as _ver

        # Use Rich's own terminal detection  more reliable than sys.stdout.isatty()
        if not _Console().is_terminal:
            _run_pipeline_compat(start_stage=start_stage, single_stage=single_stage, mode="plain")
            return

        def _request_quit(*, clean: bool = False) -> None:
            nonlocal should_clean_on_exit
            should_clean_on_exit = should_clean_on_exit or clean
            _thread.interrupt_main()

        emitter  = Emitter()
        renderer = RichRenderer(
            version=_ver,
            on_quit=lambda: _request_quit(clean=False),
            on_quit_clean=lambda: _request_quit(clean=True),
            task_graph_file=TASK_GRAPH_FILE,
        )
        renderer.subscribe(emitter)
        renderer.start()
        try:
            try:
                if prompt_mode_selection and hasattr(renderer, "request_mode_selection"):
                    current_config = load_config()
                    current_state = load_state(current_config)
                    available_modes = get_available_pipeline_modes(current_config)
                    current_mode = get_selected_mode(current_state, current_config)
                    mode_options = [get_mode_details(mode_key, current_config) for mode_key in available_modes]
                    selected_mode = renderer.request_mode_selection(mode_options, current_mode)
                    if selected_mode != current_mode:
                        set_pipeline_mode(selected_mode, current_state, current_config)

                next_start_stage = start_stage
                next_single_stage = single_stage
                while True:
                    _run_pipeline_compat(
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
                        "SUCCESS: pipeline completed. [N] new AIN session, [Q] quit, [C] quit + clean"
                    ).strip().lower()
                    if hasattr(renderer, "reset_state"):
                        renderer.reset_state()
                    if choice == "n":
                        clean_workspace(silent=True)
                        save_state(_default_state(load_config()))
                        if PLANNING_APPROVED_FLAG.exists():
                            PLANNING_APPROVED_FLAG.unlink()
                        next_start_stage = None
                        next_single_stage = False
                        continue
                    if choice == "c":
                        should_clean_on_exit = True
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
        if should_clean_on_exit:
            clean_workspace()
        if exit_code is not None:
            raise SystemExit(exit_code)

    except Exception:
        # Any TUI failure  fall back to plain output
        _run_pipeline_compat(start_stage=start_stage, single_stage=single_stage, mode="plain")


if __name__ == "__main__":
    main()

