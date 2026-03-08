#!/usr/bin/env python3
"""
A.I.N. Pipeline
===============
Multi-agent orchestrator for structured AI-assisted development.

Workflow:
    idle → scanning → architecture → planning_questions → planning_generation
    → task_creation → waiting_approval → implementation → validation → done

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

from ain.runtime.emitter import Emitter
from ain.runtime.events import (
    ApprovalReceived,
    AwaitingApproval,
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
    TaskCompleted,
    TaskFailed,
    TaskStarted,
)

# ─────────────────────────────────────────────────────────────
# Paths  (REPO_ROOT = cwd so the package works in any repo)
# ─────────────────────────────────────────────────────────────

REPO_ROOT     = Path.cwd()
PIPELINE_DIR      = REPO_ROOT / ".ai-pipeline"
STATE_FILE        = PIPELINE_DIR / "state.json"
CONFIG_FILE       = PIPELINE_DIR / "config.json"
SCAN_DIR          = PIPELINE_DIR / "scan"
PROMPTS_DIR       = PIPELINE_DIR / "prompts"
LOGS_DIR          = PIPELINE_DIR / "logs"
APPROVALS_DIR     = PIPELINE_DIR / "approvals"
USER_CONTEXT_FILE = PIPELINE_DIR / "user_context.md"
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

# ─────────────────────────────────────────────────────────────
# Stage definitions
# ─────────────────────────────────────────────────────────────

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
    "planning_questions":  "Planning — Brainstorm",
    "planning_generation": "Planning — Generation",
    "task_creation":       "Task Creation",
    "waiting_approval":    "Waiting for Approval",
    "implementation":      "Implementation",
    "validation":          "Validation",
    "done":                "Done",
    "failed":              "Failed",
}

# ─────────────────────────────────────────────────────────────
# Validation rules
# ─────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────
# Default configuration
# ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "agents": {
        "architecture": {
            "command": "gemini", "args": [], "model": None,
            "description": "Gemini for architecture analysis",
        },
        "planning": {
            "command": "codex", "args": [], "model": None,
            "prompt_mode": "arg",
            "description": "Codex for planning and specification",
        },
        "task_creation": {
            "command": "chief", "args": [], "model": None,
            "description": "Chief task orchestration engine — auto-installed by ain init",
        },
        "implementation": {
            "command": "claude",
            "args": ["--allowedTools", "Edit,Write,Bash,Read,Glob,Grep"],
            "model": None,
            "description": "Claude Code for implementation with file access",
        },
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

# ─────────────────────────────────────────────────────────────
# UTF-8 output (Windows cp1252 can't render box-drawing chars)
# ─────────────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────
# Terminal colors
# ─────────────────────────────────────────────────────────────

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
    CYAN    = "\033[96m" if _USE_COLOR else ""
    WHITE   = "\033[97m" if _USE_COLOR else ""


def _emit_log(message: str, level: LogLevel) -> None:
    """Emit a LogLine event if an emitter is active (non-blocking)."""
    if _EMITTER is not None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _emit(LogLine(ts=ts, level=level, source=LogSource.PIPELINE, stage_id=None, message=message))


def banner(text: str) -> None:
    if _EMITTER is None:   # plain mode: print to terminal
        w = 62
        print(f"\n{C.BOLD}{C.CYAN}{'─' * w}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'─' * w}{C.RESET}\n")
    _emit_log(f"━━━  {text}  ━━━", LogLevel.INFO)

def info(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.BLUE}  ▸{C.RESET} {text}")
    _emit_log(f"▸ {text}", LogLevel.INFO)

def success(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.GREEN}  ✓{C.RESET} {text}")
    _emit_log(f"✓ {text}", LogLevel.INFO)

def warn(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.YELLOW}  ⚠{C.RESET} {text}")
    _emit_log(f"⚠ {text}", LogLevel.WARN)

def error(text: str) -> None:
    if _EMITTER is None:
        print(f"{C.RED}  ✗{C.RESET} {text}", file=sys.stderr)
    _emit_log(f"✗ {text}", LogLevel.ERROR)

def step(n: int, total: int, text: str) -> None:
    if _EMITTER is None:
        print(f"{C.BOLD}{C.WHITE}  [{n}/{total}]{C.RESET} {text}")
    _emit_log(f"[{n}/{total}] {text}", LogLevel.INFO)

# ─────────────────────────────────────────────────────────────
# Event bus state
# ─────────────────────────────────────────────────────────────

_EMITTER: Emitter | None = None
_RUN_ID: str = ""

# Optional TUI renderer with suspend/resume capability.
# Set by run_pipeline() when a Rich renderer is active so that interactive
# input prompts can temporarily hand the terminal back to cooked mode.
_RENDERER: Any = None

# Protects log-file writes and task-graph updates when tasks run in parallel.
_LOG_LOCK    = threading.Lock()
_GRAPH_LOCK  = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(event: Any) -> None:
    if _EMITTER is not None:
        _EMITTER.emit(event)


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


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

def _log(message: str, *, stage_id: str | None = None) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOG_LOCK:
        with open(PIPELINE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    _emit(LogLine(ts=ts, level=LogLevel.INFO, source=LogSource.PIPELINE, stage_id=stage_id, message=message))

# ─────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "current_stage": "idle", "branch": None,
        "started_at": None, "last_updated": None, "completed_stages": [],
    }


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
    if stage not in ("idle",) and not state.get("started_at"):
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    _log(f"Stage: {prev} → {stage}")
    return state


def fail_pipeline(state: dict[str, Any], reason: str) -> None:
    error(f"Pipeline failed: {reason}")
    _log(f"FAILED: {reason}")
    state["current_stage"] = FAILED
    state["failure_reason"] = reason
    save_state(state)
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# Config management
# ─────────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return _deep_merge(DEFAULT_CONFIG, json.load(f))
    return DEFAULT_CONFIG


def ensure_config() -> None:
    if not CONFIG_FILE.exists():
        PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        info(f"Created default config: {CONFIG_FILE.relative_to(REPO_ROOT)}")

# ─────────────────────────────────────────────────────────────
# Command runner
# ─────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────
# AI agent caller
# ─────────────────────────────────────────────────────────────

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
    _log(f"AGENT CALL: {agent_name} via {command} (prompt_mode={prompt_mode})")

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


def read_context_files(*files: Path) -> str:
    parts = []
    for f in files:
        if f.exists():
            content = f.read_text(encoding="utf-8")
            parts.append(f"<!-- FILE: {f.name} -->\n{content}\n<!-- END: {f.name} -->")
        else:
            parts.append(f"<!-- FILE: {f.name} — NOT FOUND -->")
    return "\n\n".join(parts)


def build_prompt(prompt_file: Path, *context_files: Path) -> str:
    if not prompt_file.exists():
        raise RuntimeError(f"Prompt file not found: {prompt_file}")
    prompt = prompt_file.read_text(encoding="utf-8")
    if context_files:
        ctx = read_context_files(*context_files)
        prompt = f"{prompt}\n\n---\n## Context\n\n{ctx}"
    return prompt

# ─────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────
# Stage 1: Repository Scan
# ─────────────────────────────────────────────────────────────

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
        connector = "└── " if i == len(visible) - 1 else "├── "
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
    success(f"Tree → {REPO_TREE_FILE.relative_to(REPO_ROOT)}")

    step(2, 3, "Scanning tracked files ...")
    tracked = scan_git_files()
    if not tracked:
        warn("No git-tracked files. Falling back to filesystem scan.")
        ignore = set(config["scan"]["ignore_dirs"])
        tracked = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
                   for p in REPO_ROOT.rglob("*")
                   if p.is_file() and not any(ig in p.parts for ig in ignore)]
    TRACKED_FILES_FILE.write_text("\n".join(tracked), encoding="utf-8")
    success(f"{len(tracked)} files → {TRACKED_FILES_FILE.relative_to(REPO_ROOT)}")

    step(3, 3, "Generating repository summary ...")
    summary = generate_repo_summary(tree, tracked, config)
    REPO_SUMMARY_FILE.write_text(summary, encoding="utf-8")
    success(f"Summary → {REPO_SUMMARY_FILE.relative_to(REPO_ROOT)}")

    set_stage("architecture", state)
    success("Scan complete.")

# ─────────────────────────────────────────────────────────────
# Stage 2: Architecture Generation (Gemini)
# ─────────────────────────────────────────────────────────────

def run_architecture(state: dict, config: dict) -> None:
    banner("Stage: Architecture Generation (Gemini)")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    step(1, 2, "Building prompt ...")
    prompt = build_prompt(
        PROMPTS_DIR / "architecture_prompt.md",
        REPO_TREE_FILE, REPO_SUMMARY_FILE, TRACKED_FILES_FILE,
    )

    step(2, 2, "Calling architecture agent ...")
    output = call_agent("architecture", prompt, config)
    if not output.strip():
        raise RuntimeError("Architecture agent returned empty output.")

    ARCHITECTURE_FILE.write_text(output, encoding="utf-8")
    success(f"Written → {ARCHITECTURE_FILE.relative_to(REPO_ROOT)}")

    missing = validate_headings(ARCHITECTURE_FILE, ARCHITECTURE_HEADINGS)
    if missing:
        for h in missing:
            warn(f"  Missing heading: {h}")
        raise RuntimeError("Architecture validation failed. Fix docs/architecture.md then re-run.")

    success("Architecture validation passed.")
    set_stage("user_context", state)

# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Popup helpers
# ─────────────────────────────────────────────────────────────

USER_CONTEXT_TEMPLATE = """\
# Feature / Bug Context

Describe the feature you want to implement or the bug you want to fix.
Be as specific as possible — this will guide the entire planning phase.

## What do you want to build or fix?

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
        # Use list-form exec — no shell=True, no injection.
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
            if line is None:          # sentinel → process exited
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

        # ── Request input from the user ──────────────────────────
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


def _wait_for_user(prompt: str) -> None:
    """Block until the user acknowledges.  In TUI mode the input panel is used;
    in plain mode a regular input() call is made."""
    if _RENDERER is not None and hasattr(_RENDERER, "request_input"):
        _RENDERER.request_input(prompt)
        return
    # Plain/fallback mode: standard terminal input.
    print()
    try:
        input(f"  {prompt} → ")
    except (EOFError, KeyboardInterrupt):
        warn("Interrupted.")
        sys.exit(0)


# ─────────────────────────────────────────────────────────────
# Stage: Feature Context
# ─────────────────────────────────────────────────────────────

def run_user_context(state: dict, config: dict) -> None:
    banner("Stage: Feature Context")

    if not USER_CONTEXT_FILE.exists():
        USER_CONTEXT_FILE.write_text(USER_CONTEXT_TEMPLATE, encoding="utf-8")

    info(f"Opening context file: {USER_CONTEXT_FILE.relative_to(REPO_ROOT)}")
    _open_in_editor(USER_CONTEXT_FILE)

    print()
    print(f"{C.BOLD}{C.YELLOW}  ACTION REQUIRED{C.RESET}")
    print(f"  Describe the feature or bug in the editor window that just opened.")
    print(f"  File: {C.CYAN}{USER_CONTEXT_FILE.relative_to(REPO_ROOT)}{C.RESET}")
    print(f"  Save and close the file when done.")
    print()
    _wait_for_user("Press Enter once you have saved your feature description")

    content = USER_CONTEXT_FILE.read_text(encoding="utf-8")
    if "(Replace this text with your description)" in content:
        warn("Context file appears unchanged. Fill in your description and re-run.")
        sys.exit(0)

    success("Feature context saved.")
    set_stage("planning_questions", state)


# Stage 3: Planning Questions (Codex)
# ─────────────────────────────────────────────────────────────

def run_planning_questions(state: dict, config: dict) -> None:
    banner("Stage: Planning — Brainstorm (Codex)")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    user_ctx = USER_CONTEXT_FILE.read_text(encoding="utf-8") if USER_CONTEXT_FILE.exists() else ""
    arch_ctx = ARCHITECTURE_FILE.read_text(encoding="utf-8") if ARCHITECTURE_FILE.exists() else ""

    brainstorm_context = PIPELINE_DIR / "brainstorm_context.md"
    brainstorm_context.write_text(
        f"# Brainstorm Context\n\n## Feature Request\n\n{user_ctx}\n\n"
        f"## Architecture Overview\n\n{arch_ctx[:4000]}\n",
        encoding="utf-8",
    )

    codex_cmd = shutil.which("codex") or "codex"
    ctx_path  = str(brainstorm_context).replace("\\", "/")
    out_path  = str(OPEN_QUESTIONS_FILE).replace("\\", "/")
    brainstorm_prompt = (
        f"Read {ctx_path} to understand the feature request and the existing codebase. "
        f"Ask me clarifying questions to remove all ambiguity. "
        f"When we reach full clarity, write a clean Q&A summary to {out_path}."
    )

    info("Starting Codex brainstorm session inside the TUI ...")
    info(f"Context: {brainstorm_context.relative_to(REPO_ROOT)}")
    info(f"When Codex finishes clarifying, it will write: {OPEN_QUESTIONS_FILE.relative_to(REPO_ROOT)}")
    info("Type 'done' in the input bar to end the session early.")
    _run_interactive_in_tui([codex_cmd, brainstorm_prompt])

    if not OPEN_QUESTIONS_FILE.exists():
        warn("OPEN_QUESTIONS.md not found. Creating placeholder.")
        OPEN_QUESTIONS_FILE.write_text("# Open Questions\n\nNo clarification needed.\n", encoding="utf-8")
    else:
        success(f"Questions loaded: {OPEN_QUESTIONS_FILE.relative_to(REPO_ROOT)}")

    set_stage("planning_generation", state)


# ─────────────────────────────────────────────────────────────
# Stage 4: Planning Generation (Codex)
# ─────────────────────────────────────────────────────────────

def run_planning_generation(state: dict, config: dict) -> None:
    banner("Stage: Planning — Generation (Codex)")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    codex_cmd = shutil.which("codex") or "codex"
    docs_rel  = str(DOCS_DIR.relative_to(REPO_ROOT)).replace("\\", "/")
    ctx_rel   = str(PIPELINE_DIR.relative_to(REPO_ROOT)).replace("\\", "/")

    plan_prompt = (
        f"Using {ctx_rel}/user_context.md, {docs_rel}/OPEN_QUESTIONS.md, "
        f"and {docs_rel}/architecture.md, write a complete feature plan. "
        f"Create three files in {docs_rel}/: "
        f"PRD.md with sections (# Problem, # Goals, # Non Goals, # User Stories, # Success Criteria), "
        f"DESIGN.md with sections (# Architecture Changes, # Data Model, # API Changes, # UI Changes, # Risks), "
        f"FEATURE_SPEC.md with a detailed implementation spec. "
        f"Do not ask questions — generate the complete documents now."
    )

    info("Opening Codex planning session in a new terminal window ...")
    _open_popup_terminal("A.I.N. Planning", f'{codex_cmd} "{plan_prompt}"')

    print()
    print(f"{C.BOLD}{C.YELLOW}  PLANNING IN PROGRESS{C.RESET}")
    print(f"  Codex is generating the plan documents in the popup window.")
    print(f"  It should create:")
    for doc in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]:
        print(f"    {C.CYAN}{doc.relative_to(REPO_ROOT)}{C.RESET}")
    print()
    _wait_for_user("Press Enter when Codex has finished generating the plan")

    for doc, headings, name in [
        (PRD_FILE,          PRD_HEADINGS,    "PRD.md"),
        (DESIGN_FILE,       DESIGN_HEADINGS, "DESIGN.md"),
        (FEATURE_SPEC_FILE, [],              "FEATURE_SPEC.md"),
    ]:
        if not doc.exists():
            warn(f"{name} not found — creating stub. Edit it before continuing.")
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
            success(f"Written → {target.relative_to(REPO_ROOT)}")
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

# ─────────────────────────────────────────────────────────────
# Stage 5: Task Creation (Chief)
# ─────────────────────────────────────────────────────────────

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
        "- Write `docs/TASKS.md` — a dependency-ordered markdown checkbox task list\n"
        "- Write `docs/TASK_GRAPH.json` — a JSON dependency graph\n\n"
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
    success(f"Written → {CHIEF_PRD_FILE.relative_to(REPO_ROOT)}")
    success(f"Written → {CHIEF_PRD_MD.relative_to(REPO_ROOT)}")


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


def _run_chief_tui(config: dict) -> None:
    """Run chief as an interactive TUI with a real TTY — no stdout capture."""
    agent_cfg  = config.get("agents", {}).get("task_creation", {})
    command    = agent_cfg.get("command", "chief")
    extra_args = agent_cfg.get("args", [])

    resolved = shutil.which(command)
    if not resolved:
        raise RuntimeError(
            f"Agent command not found: '{command}'. "
            "Ensure chief is installed and on your PATH."
        )

    cmd = [resolved] + extra_args + ["--no-retry", "main"]
    _log(f"RUN (TUI): {' '.join(str(c) for c in cmd)}")
    info("Launching chief — work through the stories, then exit chief when done.")

    result = subprocess.run(cmd, cwd=str(REPO_ROOT), env={**os.environ})
    if result.returncode != 0:
        warn(f"chief exited with code {result.returncode}")


def run_task_creation(state: dict, config: dict) -> None:
    banner("Stage: Task Creation (Chief)")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = PROMPTS_DIR / "task_creation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

    agent_cmd = config.get("agents", {}).get("task_creation", {}).get("command", "")
    ctx_files = [f for f in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE, ARCHITECTURE_FILE] if f.exists()]

    if agent_cmd == "chief":
        step(1, 3, "Writing chief PRD ...")
        _write_chief_prd(build_prompt(prompt_file, *ctx_files))

        step(2, 3, "Running chief TUI ...")
        _run_chief_tui(config)

        step(3, 3, "Validating chief output ...")
        if not TASK_GRAPH_FILE.exists() and not TASKS_FILE.exists():
            _build_task_graph_from_tasks_md()
    else:
        step(1, 2, "Building prompt ...")
        prompt = build_prompt(prompt_file, *ctx_files)

        step(2, 2, "Calling task creation agent ...")
        output = call_agent("task_creation", prompt, config)
        if not output.strip():
            raise RuntimeError("Task creation agent returned empty output.")
        _parse_and_write_task_artifacts(output)

    if not validate_tasks_file(TASKS_FILE):
        raise RuntimeError("TASKS.md does not contain valid checkbox tasks.")
    if not validate_task_graph(TASK_GRAPH_FILE):
        raise RuntimeError("TASK_GRAPH.json is invalid or empty.")

    data  = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
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
            success(f"Written → {target.relative_to(REPO_ROOT)}")
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


# ─────────────────────────────────────────────────────────────
# Stage 6: Approval Gate
# ─────────────────────────────────────────────────────────────

def run_waiting_approval(state: dict, config: dict) -> None:
    banner("Stage: Waiting for Approval")

    if PLANNING_APPROVED_FLAG.exists():
        success("Planning approved. Advancing to implementation.")
        set_stage("implementation", state)
        return

    _emit(AwaitingApproval(run_id=_RUN_ID, stage_id="waiting_approval"))

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

# ─────────────────────────────────────────────────────────────
# Git integration
# ─────────────────────────────────────────────────────────────

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
        msg = (
            f"feat: AI pipeline implementation\n\n"
            f"Generated by A.I.N. Pipeline\n"
            f"Branch: {state.get('branch', 'unknown')}"
        )
        run_command(["git", "commit", "-m", msg])
        success("Changes committed.")
    except Exception as e:
        warn(f"Git commit failed: {e}")


# ─────────────────────────────────────────────────────────────
# Workspace cleanup
# ─────────────────────────────────────────────────────────────

# Generated files that belong to a single pipeline run, not the tool itself.
_CLEAN_FILES = [
    # Planning & architecture docs
    DOCS_DIR / "architecture.md",
    DOCS_DIR / "PRD.md",
    DOCS_DIR / "DESIGN.md",
    DOCS_DIR / "FEATURE_SPEC.md",
    DOCS_DIR / "OPEN_QUESTIONS.md",
    DOCS_DIR / "OPEN_ANSWERS.md",
    # Task artifacts
    DOCS_DIR / "TASKS.md",
    DOCS_DIR / "TASK_GRAPH.json",
    # Run logs & reports
    DOCS_DIR / "IMPLEMENTATION_LOG.md",
    DOCS_DIR / "VERIFICATION_REPORT.md",
    # Pipeline session files
    PIPELINE_DIR / "user_context.md",
    PIPELINE_DIR / "brainstorm_context.md",
]

_CLEAN_DIRS = [
    PIPELINE_DIR / "scan",
    PIPELINE_DIR / "logs",
    PIPELINE_DIR / "approvals",
    PIPELINE_DIR / "state",
    # Chief PRD inputs — regenerated each run, clearing prevents stale state
    REPO_ROOT / ".chief" / "prds",
]


def clean_workspace(silent: bool = False) -> None:
    """Delete all per-run generated files and reset pipeline state to idle.

    Preserves: config.json, prompts/, CLAUDE.md, and all source code.
    Called automatically after a successful auto-commit, or manually via --clean.
    """
    removed: list[str] = []

    for f in _CLEAN_FILES:
        if f.exists():
            f.unlink()
            removed.append(str(f.relative_to(REPO_ROOT)))

    for d in _CLEAN_DIRS:
        if d.exists():
            shutil.rmtree(d)
            removed.append(str(d.relative_to(REPO_ROOT)) + "/")

    # Reset state to idle (keeps config intact)
    save_state({
        "current_stage": "idle",
        "branch": None,
        "started_at": None,
        "last_updated": None,
        "completed_stages": [],
    })

    if not silent:
        if removed:
            for item in removed:
                info(f"Removed: {item}")
        else:
            info("Nothing to clean.")
        success("Workspace cleaned. Ready for next implementation.")

# ─────────────────────────────────────────────────────────────
# Token-limit fallback helpers
# ─────────────────────────────────────────────────────────────

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
    """Invoke the planning (codex) agent as a fallback for an oversized task."""
    fallback_cfg = config.get("agents", {}).get("planning", {})
    cmd = fallback_cfg.get("command", "")
    if not cmd or not shutil.which(cmd):
        raise RuntimeError(
            "Codex fallback requested but 'planning' agent is not available. "
            "Install codex and configure it in .ai-pipeline/config.json."
        )
    info("Invoking codex fallback agent ...")
    return call_agent("planning", task_prompt, config)


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
    _log(f"AGENT CALL: {agent_name} via {command} (prompt_mode={prompt_mode})")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / f"{agent_name}_last_prompt.txt").write_text(prompt, encoding="utf-8")

    try:
        if prompt_mode == "arg":
            result = run_command(cmd + [prompt], capture=True, timeout=600)
        else:
            result = run_command(cmd, capture=True, input_text=prompt, timeout=600)
    except FileNotFoundError:
        raise RuntimeError(
            f"Agent command not found: '{command}'. "
            f"Edit .ai-pipeline/config.json to configure the '{agent_name}' agent."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agent '{agent_name}' timed out after 600 seconds.")

    output = result.stdout or ""
    stderr = (result.stderr or "").strip()
    (LOGS_DIR / f"{agent_name}_last_output.txt").write_text(output, encoding="utf-8")

    if result.returncode != 0:
        warn(f"Agent {agent_name} exited {result.returncode}")
        _log(f"AGENT STDERR: {stderr[:500]}")

        if is_token_limit_error(output + "\n" + stderr, result.returncode):
            context_hint = f"Prompt size: {len(prompt):,} chars"
            use_fallback = notify_fallback_and_get_decision(context_hint)
            if use_fallback:
                info("Rolling back any partial file writes ...")
                rolled = rollback_implementation_files()
                if rolled:
                    for f in rolled:
                        info(f"  Rolled back: {f}")
                return invoke_codex_fallback(prompt, config)
            raise RuntimeError(
                f"Agent {agent_name} hit a token limit — task skipped by user."
            )

    return output


# ─────────────────────────────────────────────────────────────
# Parallel task execution helpers
# ─────────────────────────────────────────────────────────────

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
    config: dict,
    task_data: dict,
    log_lines: list,
) -> bool:
    """Execute a single task and update shared state.  Thread-safe.  Returns True on success."""
    task_id     = task["id"]
    description = task["description"]
    agent_cfg   = config.get("agents", {}).get("implementation", {})
    agent_name  = agent_cfg.get("command", "claude")

    _emit(TaskStarted(
        task_id=str(task_id),
        description=description,
        agent=agent_name,
        started_at=_now_iso(),
    ))
    info(f"  ▸ Task {task_id}: {description}")

    t0 = datetime.now(timezone.utc)
    try:
        task_prompt = _build_task_prompt(task, prompt_file)
        _call_agent_with_fallback("implementation", task_prompt, config)

        duration_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        _emit(TaskCompleted(
            task_id=str(task_id),
            description=description,
            duration_ms=duration_ms,
            ended_at=_now_iso(),
        ))
        success(f"  ✓ Task {task_id} complete ({duration_ms // 1000}s)")

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
        error(f"  ✗ Task {task_id} failed: {e}")
        _log(f"Task {task_id} failed: {e}")
        log_lines.append(f"## Task {task_id}: {description}")
        log_lines.append(f"Status: FAILED")
        log_lines.append(f"Error: {e}")
        log_lines += ["", "---", ""]
        return False


def _execute_parallel_groups(
    task_data: dict,
    prompt_file: Path,
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
                pool.submit(_run_one_task, task, prompt_file, config, task_data, log_lines): task
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
            "completed" if _run_one_task(task, prompt_file, config, task_data, log_lines)
            else "failed"
        )


# ─────────────────────────────────────────────────────────────
# Stage 7: Implementation (Claude)
# ─────────────────────────────────────────────────────────────

def run_implementation(state: dict, config: dict) -> None:
    banner("Stage: Implementation (Claude)")
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
        info(f"Parallel groups detected ({len(parallel_groups)} groups) — running concurrently.")
        _execute_parallel_groups(task_data, prompt_file, config, log_lines)
    else:
        # No parallel groups — run sequentially via the shared helper.
        dep_statuses = {t["id"]: t["status"] for t in tasks}
        for task in pending:
            blocked = [d for d in task.get("depends_on", [])
                       if dep_statuses.get(d) != "completed"]
            if blocked:
                warn(f"    Task {task['id']} blocked by {blocked}. Skipping.")
                continue
            succeeded = _run_one_task(task, prompt_file, config, task_data, log_lines)
            dep_statuses[task["id"]] = "completed" if succeeded else "failed"

    IMPLEMENTATION_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    success(f"Log → {IMPLEMENTATION_LOG_FILE.relative_to(REPO_ROOT)}")
    set_stage("validation", state)


def _mark_task_complete_in_md(description: str) -> None:
    with _GRAPH_LOCK:
        if not TASKS_FILE.exists():
            return
        content = TASKS_FILE.read_text(encoding="utf-8")
        snippet = re.escape(description[:60])
        new = re.sub(r"- \[ \] " + snippet, "- [x] " + description[:60], content, count=1)
        TASKS_FILE.write_text(new, encoding="utf-8")

# ─────────────────────────────────────────────────────────────
# Stage 8: Validation
# ─────────────────────────────────────────────────────────────

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
            cmds.append(["python", "-m", "pytest", "--tb=short", "-q"])

    if "go.mod" in files_set:
        cmds.append(["go", "test", "./..."])
        cmds.append(["go", "vet", "./..."])

    if "Cargo.toml" in files_set:
        cmds.append(["cargo", "test"])

    return cmds


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

    val_log = LOGS_DIR / "validation.log"
    val_log.write_text("\n".join(log_lines), encoding="utf-8")
    success(f"Log → {val_log.relative_to(REPO_ROOT)}")

    if not all_passed:
        raise RuntimeError("Validation failed. See .ai-pipeline/logs/validation.log")

    commit_implementation(state, config)
    success("All validation checks passed.")
    set_stage("done", state)

    # Clean generated artifacts after a successful auto-commit
    if config["git"]["auto_commit"]:
        banner("Cleaning workspace for next run")
        clean_workspace()

# ─────────────────────────────────────────────────────────────
# Agent CLI installation
# ─────────────────────────────────────────────────────────────

# Maps CLI command name → npm package to install if missing
AGENT_NPM_PACKAGES: dict[str, str] = {
    "gemini": "@google/gemini-cli",
    "codex":  "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}

# Maps CLI command name → curl install script URL
AGENT_CURL_INSTALLS: dict[str, str] = {
    "chief": "https://raw.githubusercontent.com/minicodemonkey/chief/main/install.sh",
}


def _install_via_npm(command: str, pkg: str) -> bool:
    """Install an npm package globally. Returns True on success."""
    info(f"{command} — not found, installing {pkg} ...")
    result = run_command(["npm", "install", "-g", pkg], capture=True, timeout=120)
    if result.returncode == 0:
        success(f"{command} — installed")
        return True
    error(f"{command} — installation failed")
    warn(f"  Manual install: npm install -g {pkg}")
    if result.stderr:
        warn(f"  {result.stderr.strip()[:200]}")
    return False


def _install_via_curl(command: str, url: str) -> bool:
    """Install via a remote shell script. Returns True on success.

    Downloads the script first, then pipes it to bash — avoids ``shell=True``
    with a raw URL interpolated into a command string.
    """
    info(f"{command} — not found, installing via install script ...")
    if not shutil.which("curl"):
        error(f"{command} — curl not found, cannot run install script")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    if not shutil.which("bash"):
        error(f"{command} — bash not found, cannot run install script")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    try:
        fetch = run_command(["curl", "-fsSL", url], capture=True, timeout=30)
        if fetch.returncode != 0:
            error(f"{command} — download failed (exit {fetch.returncode})")
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
            success(f"{command} — installed")
            return True
        error(f"{command} — installation failed (exit {install.returncode})")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    except subprocess.TimeoutExpired:
        error(f"{command} — installation timed out")
        return False


def install_agents(config: dict) -> None:
    """Check each configured agent CLI and install any that are missing."""
    print()
    info("Checking agent CLIs ...")

    has_npm = bool(shutil.which("npm"))
    if not has_npm:
        warn("npm not found — npm-based agents cannot be auto-installed.")
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
            success(f"{command} ({stage}) — already installed")
        elif command in AGENT_CURL_INSTALLS:
            any_missing = True
            _install_via_curl(command, AGENT_CURL_INSTALLS[command])
        elif command in AGENT_NPM_PACKAGES:
            any_missing = True
            if has_npm:
                _install_via_npm(command, AGENT_NPM_PACKAGES[command])
            else:
                warn(f"{command} — skipped (npm not available)")
        else:
            warn(f"{command} ({stage}) — not found and no auto-install configured")
            warn(f"  Install it manually and ensure it is on your PATH")

    if not any_missing:
        success("All agents available.")


# ─────────────────────────────────────────────────────────────
# ain init — scaffold pipeline into current repo
# ─────────────────────────────────────────────────────────────

def run_init() -> None:
    from importlib.resources import files as res_files

    banner("A.I.N. Pipeline — Init")

    for d in [PIPELINE_DIR, SCAN_DIR, PROMPTS_DIR, LOGS_DIR, APPROVALS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        save_state({
            "current_stage": "idle", "branch": None,
            "started_at": None, "last_updated": None, "completed_stages": [],
        })
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

# ─────────────────────────────────────────────────────────────
# Status display
# ─────────────────────────────────────────────────────────────

def show_status(state: dict) -> None:
    banner("A.I.N. Pipeline — Status")
    current   = state.get("current_stage", "unknown")
    completed = state.get("completed_stages", [])

    print(f"  Stage:   {C.BOLD}{C.CYAN}{STAGE_LABELS.get(current, current)}{C.RESET}")
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
            icon = f"{C.GREEN}✓{C.RESET}"
        elif stage == current:
            icon = f"{C.YELLOW}▶{C.RESET}"
        else:
            icon = f"{C.DIM}○{C.RESET}"
        print(f"    {icon}  {STAGE_LABELS.get(stage, stage)}")

    if TASK_GRAPH_FILE.exists():
        try:
            data = json.loads(TASK_GRAPH_FILE.read_text(encoding="utf-8"))
            print(f"\n  Tasks: {C.GREEN}{data.get('completed', 0)}{C.RESET}/{data.get('total', 0)} completed")
        except Exception:
            pass
    print()

# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────

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
    state  = load_state()

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
    for i, stage in enumerate(runnable):
        _emit(StageQueued(stage_id=stage, stage_name=STAGE_LABELS.get(stage, stage), index=i))

    for stage in to_run:
        if stage in ("idle", "done"):
            continue
        runner = STAGE_RUNNERS.get(stage)
        if not runner:
            continue
        started_at = _now_iso()
        t0 = datetime.now(timezone.utc)
        _emit(StageStarted(stage_id=stage, started_at=started_at))
        try:
            runner(state, config)
            state = load_state()
            ended_at = _now_iso()
            duration_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            _emit(StageCompleted(stage_id=stage, ended_at=ended_at, duration_ms=duration_ms))
        except RuntimeError as e:
            ended_at = _now_iso()
            _emit(StageFailed(stage_id=stage, ended_at=ended_at, error=str(e)))
            _emit(RunCompleted(run_id=_RUN_ID, ended_at=ended_at, status=RunStatus.FAILED))
            fail_pipeline(state, str(e))
        except KeyboardInterrupt:
            warn("\nInterrupted by user.")
            _emit(RunCompleted(run_id=_RUN_ID, ended_at=_now_iso(), status=RunStatus.INTERRUPTED))
            sys.exit(0)

    state = load_state()
    if state["current_stage"] == "done":
        banner("Pipeline Complete")
        show_status(state)
        _emit(RunCompleted(run_id=_RUN_ID, ended_at=_now_iso(), status=RunStatus.DONE))

# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ain",
        description="A.I.N. Pipeline — multi-agent AI development orchestrator",
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

    if args.reset:
        save_state({"current_stage": "idle", "branch": None,
                    "started_at": None, "last_updated": None, "completed_stages": []})
        if PLANNING_APPROVED_FLAG.exists():
            PLANNING_APPROVED_FLAG.unlink()
        success("Pipeline reset to idle.")
        return

    if args.clean:
        banner("A.I.N. Pipeline — Clean")
        clean_workspace()
        return

    if args.approve:
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

    if args.status:
        show_status(load_state())
        return

    if args.command == "run":
        single = bool(getattr(args, "stage", None))
        run_pipeline(start_stage=getattr(args, "resume", None) or getattr(args, "stage", None),
                     single_stage=single)
        return

    # No subcommand and no flag — show help
    parser.print_help()


if __name__ == "__main__":
    main()
