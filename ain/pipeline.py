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
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Literal

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
STATE_DIR         = PIPELINE_DIR / "state"
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
VERIFICATION_REPORT_FILE = DOCS_DIR / "VERIFICATION_REPORT.md"

PLANNING_APPROVED_FLAG = APPROVALS_DIR / "planning_approved.flag"
VERIFICATION_FLAG      = APPROVALS_DIR / "verification.flag"
CLAUDE_MD_FILE         = REPO_ROOT / "CLAUDE.md"

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
    "verification",
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
    "verification":        "Verification",
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
            "description": "Chief autonomous PRD agent — auto-installed by ain init",
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


def banner(text: str) -> None:
    w = 62
    print(f"\n{C.BOLD}{C.CYAN}{'─' * w}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'─' * w}{C.RESET}\n")

def info(text: str)    -> None: print(f"{C.BLUE}  ▸{C.RESET} {text}")
def success(text: str) -> None: print(f"{C.GREEN}  ✓{C.RESET} {text}")
def warn(text: str)    -> None: print(f"{C.YELLOW}  ⚠{C.RESET} {text}")
def error(text: str)   -> None: print(f"{C.RED}  ✗{C.RESET} {text}", file=sys.stderr)
def step(n: int, total: int, text: str) -> None:
    print(f"{C.BOLD}{C.WHITE}  [{n}/{total}]{C.RESET} {text}")

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

def _log(message: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(PIPELINE_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")

# ─────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────

_DEFAULT_FALLBACK: dict[str, Any] = {
    "triggered": False,
    "trigger_reason": None,
    "trigger_agent": None,
    "trigger_stage": None,
    "trigger_timestamp": None,
    "user_response": None,
    "auto_switched_at": None,
    "rollback_commit": None,
    "rollback_files": [],
    "fallback_agent": None,
    "fallback_stage": None,
    "fallback_completed": False,
}

_DEFAULT_PRD_IMPORT: dict[str, Any] = {
    "enabled": False,
    "source": None,
    "imported_at": None,
    "files_written": [],
    "skipped_stages": [],
}


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("fallback", dict(_DEFAULT_FALLBACK))
        state.setdefault("prd_import", dict(_DEFAULT_PRD_IMPORT))
        return state
    return {
        "current_stage": "idle", "branch": None,
        "started_at": None, "last_updated": None, "completed_stages": [],
        "fallback": dict(_DEFAULT_FALLBACK),
        "prd_import": dict(_DEFAULT_PRD_IMPORT),
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
    cmd: list[str],
    cwd: Path | None = None,
    capture: bool = False,
    input_text: str | None = None,
    timeout: int = 300,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    _log(f"RUN: {' '.join(str(c) for c in cmd)}")
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
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0 and capture:
        _log(f"STDERR: {(result.stderr or '').strip()[:500]}")
    return result


def run_command_output(cmd: list[str], cwd: Path | None = None) -> str:
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
            # Ensure parallel_groups exists (inject empty list if absent)
            if "parallel_groups" not in data:
                data["parallel_groups"] = []
            graph_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        return False
    except (json.JSONDecodeError, KeyError):
        return False

# ─────────────────────────────────────────────────────────────
# Fallback helpers
# ─────────────────────────────────────────────────────────────

# Canonical token-limit phrase emitted by Claude CLI.
_TOKEN_LIMIT_PATTERN = re.compile(
    r"claude usage limit reached\.",
    re.IGNORECASE,
)

# Context-overflow phrases that must NOT trigger the fallback controller.
_CONTEXT_OVERFLOW_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"prompt is too long",
        r"exceeds (the )?(maximum )?context",
        r"context (window|length) (exceeded|too long)",
        r"too many tokens",
    )
]


def is_token_limit_error(err_or_output: str) -> bool:
    """Return True when *err_or_output* contains the canonical Claude usage-limit
    message but does NOT contain a context-overflow message.

    The usage-limit signature is:
        "Claude usage limit reached. Your limit will reset at <time> (<tz>)."

    Context-overflow errors (e.g. "prompt is too long", "exceeds context window")
    are explicitly excluded so that the fallback controller is not triggered for
    model-context failures, which require a different resolution path.
    """
    if not isinstance(err_or_output, str):
        return False
    if not _TOKEN_LIMIT_PATTERN.search(err_or_output):
        return False
    for pattern in _CONTEXT_OVERFLOW_PATTERNS:
        if pattern.search(err_or_output):
            return False
    return True


def capture_rollback_point(state: dict[str, Any]) -> str:
    """Capture the current HEAD commit SHA and persist it to state.fallback.rollback_commit.

    Returns the captured commit SHA string.
    Raises subprocess.CalledProcessError if the git command fails.
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    sha = result.stdout.strip()
    state["fallback"]["rollback_commit"] = sha
    save_state(state)
    return sha


def notify_fallback_and_get_decision(
    context: dict[str, Any],
    timeout_secs: int,
) -> Literal["wait", "switch", "abort", "auto_switch"]:
    """Display a terminal fallback notification and wait for a user decision.

    Prints stage, agent, tasks progress, rollback SHA, available options, and
    a live countdown.  Blocks until the user types a valid option or the
    timeout expires.

    Parameters
    ----------
    context:
        Dict with keys ``stage``, ``agent``, ``tasks_progress``,
        ``rollback_sha``.  Missing keys render as ``"unknown"``.
    timeout_secs:
        Seconds to wait before auto-switching.

    Returns
    -------
    One of ``"wait"``, ``"switch"``, ``"abort"``, or ``"auto_switch"``
    (returned when the countdown expires without user input).
    """
    stage          = context.get("stage", "unknown")
    agent          = context.get("agent", "unknown")
    tasks_progress = context.get("tasks_progress", "unknown")
    rollback_sha   = context.get("rollback_sha", "unknown")

    w = 62
    print(f"\n{C.BOLD}{C.RED}{'─' * w}{C.RESET}")
    print(f"{C.BOLD}{C.RED}  FALLBACK TRIGGERED — Token limit reached{C.RESET}")
    print(f"{C.BOLD}{C.RED}{'─' * w}{C.RESET}")
    print(f"  {C.BOLD}Stage:{C.RESET}           {stage}")
    print(f"  {C.BOLD}Agent:{C.RESET}           {agent}")
    print(f"  {C.BOLD}Tasks progress:{C.RESET}  {tasks_progress}")
    print(f"  {C.BOLD}Rollback SHA:{C.RESET}    {rollback_sha}")
    print(f"{C.BOLD}{C.RED}{'─' * w}{C.RESET}")
    print(f"\n  {C.BOLD}Options:{C.RESET}")
    print(f"    {C.GREEN}wait{C.RESET}    — Wait for the token limit to reset, then resume")
    print(f"    {C.YELLOW}switch{C.RESET}  — Roll back and switch to Codex fallback agent")
    print(f"    {C.RED}abort{C.RESET}   — Roll back and stop the pipeline")
    print()

    VALID: frozenset[str] = frozenset({"wait", "switch", "abort"})
    response_q: queue.Queue[str] = queue.Queue()

    def _read_input() -> None:
        while response_q.empty():
            try:
                line = input().strip().lower()
            except EOFError:
                break
            if line in VALID:
                response_q.put(line)
                break
            if response_q.empty():
                print(f"  {C.YELLOW}Please type one of: wait, switch, abort{C.RESET}")

    reader = threading.Thread(target=_read_input, daemon=True)
    reader.start()

    for remaining in range(timeout_secs, 0, -1):
        print(
            f"\r  {C.CYAN}Auto-switching in {remaining:3d}s …  "
            f"[wait / switch / abort]{C.RESET}",
            end="",
            flush=True,
        )
        try:
            decision = response_q.get(timeout=1)
            print()  # newline past countdown
            _log(f"Fallback decision: {decision} (user input)")
            return decision  # type: ignore[return-value]
        except queue.Empty:
            pass

    print(
        f"\r  {C.YELLOW}Timeout — auto-switching to fallback agent.{C.RESET}"
        f"{'  ' * 20}"
    )
    _log("Fallback decision: auto_switch (timeout)")
    return "auto_switch"


def rollback_implementation_files(state: dict[str, Any]) -> list[str]:
    """Compute files changed since rollback_commit, filter protected paths,
    reset them via git checkout, and persist the list to state.fallback.rollback_files.

    Returns the list of files actually rolled back.
    """
    rollback_commit = state.get("fallback", {}).get("rollback_commit")
    if not rollback_commit:
        warn("No rollback_commit in state; skipping file rollback.")
        return []

    protected_prefixes = [
        "docs/", ".ai-pipeline/", ".git/", ".claude/",
    ]
    protected_exact = {"CLAUDE.md"}

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", rollback_commit, "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        changed = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git diff failed: {e.stderr.strip()}")

    to_reset = []
    for f in changed:
        if f in protected_exact:
            continue
        if any(f.startswith(p) for p in protected_prefixes):
            continue
        to_reset.append(f)

    if to_reset:
        try:
            subprocess.run(
                ["git", "checkout", rollback_commit, "--"] + to_reset,
                cwd=REPO_ROOT,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git checkout rollback failed: {e}")

    state["fallback"]["rollback_files"] = to_reset
    save_state(state)
    _log(f"Rolled back {len(to_reset)} files to {rollback_commit}")
    return to_reset


def invoke_codex_fallback(
    stage: str,
    prompt_path: Path,
    timeout_secs: int,
) -> bool:
    """Run Codex in full-auto mode with the given prompt file.

    Streams output to stdout, enforces a timeout, and writes
    .ai-pipeline/state/fallback_complete.flag on success.

    Returns True on success, False on failure or timeout.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    completion_flag = STATE_DIR / "fallback_complete.flag"

    if not prompt_path.exists():
        error(f"Codex fallback prompt not found: {prompt_path}")
        return False

    prompt_text = prompt_path.read_text(encoding="utf-8")
    codex_cmd   = shutil.which("codex") or "codex"

    cmd = [codex_cmd, "--full-auto", prompt_text]
    _log(f"CODEX FALLBACK: {stage} | cmd={cmd[0]}")
    info(f"Launching Codex fallback for stage '{stage}' (timeout={timeout_secs}s) ...")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        import select as _select
        end_time = __import__("time").time() + timeout_secs
        while True:
            remaining = end_time - __import__("time").time()
            if remaining <= 0:
                proc.kill()
                warn("Codex fallback timed out.")
                _log("CODEX FALLBACK: timed out")
                return False
            # Poll for output (Windows-compatible: just readline with a small timeout)
            line = proc.stdout.readline()
            if line:
                print(f"  {line}", end="", flush=True)
            elif proc.poll() is not None:
                break
        rc = proc.wait()
        if rc != 0:
            warn(f"Codex fallback exited with code {rc}")
            _log(f"CODEX FALLBACK: exit {rc}")
            return False

        completion_flag.write_text(
            f"stage={stage}\ntimestamp={datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        _log(f"CODEX FALLBACK: success, flag written")
        return True
    except FileNotFoundError:
        error(f"Codex not found: '{codex_cmd}'. Install it and ensure it is on PATH.")
        return False
    except Exception as e:
        error(f"Codex fallback error: {e}")
        _log(f"CODEX FALLBACK: error {e}")
        return False


def validate_prd_import(state: dict[str, Any]) -> None:
    """Enforce that PRD.md >= 500 chars and DESIGN.md + FEATURE_SPEC.md are non-empty.

    Raises RuntimeError with a descriptive message on failure.
    """
    config = load_config()
    min_chars = config.get("prd_import", {}).get("min_prd_chars", 500)

    if not PRD_FILE.exists():
        raise RuntimeError(f"docs/PRD.md not found after PRD import.")
    prd_content = PRD_FILE.read_text(encoding="utf-8")
    if len(prd_content) < min_chars:
        raise RuntimeError(
            f"docs/PRD.md is too short ({len(prd_content)} chars < {min_chars} required)."
        )
    if not DESIGN_FILE.exists() or not DESIGN_FILE.read_text(encoding="utf-8").strip():
        raise RuntimeError("docs/DESIGN.md is missing or empty after PRD import.")
    if not FEATURE_SPEC_FILE.exists() or not FEATURE_SPEC_FILE.read_text(encoding="utf-8").strip():
        raise RuntimeError("docs/FEATURE_SPEC.md is missing or empty after PRD import.")


def handle_prd_import(import_path: Path, state: dict[str, Any]) -> None:
    """Import a PRD from a file or directory, write normalized docs, and advance to task_creation.

    File mode:   reads the single file; if it looks like a combined PRD,
                 runs prd_extraction_prompt to split it into three docs.
    Directory mode: resolves PRD/design/spec files by filename tokens.

    Persists import metadata to state.prd_import and writes
    .ai-pipeline/state/prd_extraction.flag for single-file extraction.
    """
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    allowed_exts = {e.lower() for e in config.get("prd_import", {}).get("allowed_extensions", [".md", ".txt"])}
    files_written: list[str] = []
    is_single_file = False

    if import_path.is_file():
        is_single_file = True
        content = import_path.read_text(encoding="utf-8", errors="replace")

        # Check if this is a combined file that needs splitting.
        # Heuristic: contains all three section markers → use extraction prompt.
        design_marker   = re.search(r"#\s*(architecture|design)", content, re.IGNORECASE)
        feature_marker  = re.search(r"#\s*(feature\s*spec|implementation\s*spec)", content, re.IGNORECASE)

        if design_marker and feature_marker:
            # Use the prd_extraction_prompt to split
            extraction_prompt_file = PROMPTS_DIR / "prd_extraction_prompt.md"
            if extraction_prompt_file.exists():
                extraction_prompt = extraction_prompt_file.read_text(encoding="utf-8")
                combined_prompt = (
                    f"{extraction_prompt}\n\n---\n## Source Document\n\n{content}"
                )
                info("Splitting combined PRD document using extraction prompt ...")
                output = call_agent("implementation", combined_prompt, config)
                _parse_and_write_planning_docs(output)
            else:
                # Fallback: write everything to PRD.md, stubs for the rest
                PRD_FILE.write_text(content, encoding="utf-8")
                if not DESIGN_FILE.exists():
                    DESIGN_FILE.write_text(
                        "# Architecture Changes\n\n# Data Model\n\n# API Changes\n\n# UI Changes\n\n# Risks\n",
                        encoding="utf-8",
                    )
                if not FEATURE_SPEC_FILE.exists():
                    FEATURE_SPEC_FILE.write_text("# Feature Specification\n\n", encoding="utf-8")

            # Write extraction flag
            (STATE_DIR / "prd_extraction.flag").write_text(
                f"source={import_path}\ntimestamp={datetime.now(timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )
        else:
            # Single PRD file with no embedded design/spec — use as-is
            PRD_FILE.write_text(content, encoding="utf-8")
            if not DESIGN_FILE.exists():
                DESIGN_FILE.write_text(
                    "# Architecture Changes\n\n# Data Model\n\n# API Changes\n\n# UI Changes\n\n# Risks\n",
                    encoding="utf-8",
                )
            if not FEATURE_SPEC_FILE.exists():
                FEATURE_SPEC_FILE.write_text("# Feature Specification\n\n", encoding="utf-8")

        for f in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE]:
            if f.exists():
                files_written.append(str(f.relative_to(REPO_ROOT)))
                success(f"Written → {f.relative_to(REPO_ROOT)}")

    elif import_path.is_dir():
        _PRD_TOKENS     = {"prd", "product", "requirements"}
        _DESIGN_TOKENS  = {"design", "architecture", "arch"}
        _SPEC_TOKENS    = {"spec", "feature", "implementation"}

        def _pick_file(tokens: set[str]) -> Path | None:
            for candidate in sorted(import_path.iterdir()):
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in allowed_exts:
                    continue
                name_lower = candidate.stem.lower()
                if any(t in name_lower for t in tokens):
                    return candidate
            return None

        prd_src    = _pick_file(_PRD_TOKENS)
        design_src = _pick_file(_DESIGN_TOKENS)
        spec_src   = _pick_file(_SPEC_TOKENS)

        for src, dst in [(prd_src, PRD_FILE), (design_src, DESIGN_FILE), (spec_src, FEATURE_SPEC_FILE)]:
            if src:
                dst.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                files_written.append(str(dst.relative_to(REPO_ROOT)))
                success(f"Imported {src.name} → {dst.relative_to(REPO_ROOT)}")
            else:
                if not dst.exists():
                    dst.write_text(f"# {dst.stem}\n\n", encoding="utf-8")
                    files_written.append(str(dst.relative_to(REPO_ROOT)))
    else:
        raise RuntimeError(f"--prd-import path not found: {import_path}")

    # Persist import metadata
    state["prd_import"]["enabled"]       = True
    state["prd_import"]["source"]        = str(import_path)
    state["prd_import"]["imported_at"]   = datetime.now(timezone.utc).isoformat()
    state["prd_import"]["files_written"] = files_written
    state["prd_import"]["skipped_stages"] = ["user_context", "planning_questions", "planning_generation"]
    save_state(state)

    set_stage("task_creation", state)
    success("PRD import complete. Advancing to task_creation.")


def run_verification_stage(state: dict[str, Any], config: dict[str, Any]) -> bool:
    """Execute the verification audit via the claude agent and check for the verification flag.

    Returns True if verification passed (flag written), False otherwise.
    """
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)

    verification_prompt_file = PROMPTS_DIR / "verification_prompt.md"
    if not verification_prompt_file.exists():
        warn("verification_prompt.md not found. Skipping verification.")
        return True

    banner("Stage: Verification (Audit)")

    ctx_files = [f for f in [TASKS_FILE, TASK_GRAPH_FILE, IMPLEMENTATION_LOG_FILE] if f.exists()]
    prompt = build_prompt(verification_prompt_file, *ctx_files)

    info("Running verification audit ...")
    try:
        call_agent("implementation", prompt, config)
    except Exception as e:
        error(f"Verification agent failed: {e}")
        _log(f"VERIFICATION FAILED: {e}")
        return False

    if VERIFICATION_FLAG.exists():
        success("Verification passed. Flag written.")
        _log("VERIFICATION: PASSED")
        return True
    else:
        error("Verification failed. Flag not written.")
        _log("VERIFICATION: FAILED — flag not found")
        return False


# ─────────────────────────────────────────────────────────────
# RunResult type
# ─────────────────────────────────────────────────────────────

class RunResult:
    def __init__(self, success: bool, failed_groups: list[str], completed_groups: list[str]) -> None:
        self.success          = success
        self.failed_groups    = failed_groups
        self.completed_groups = completed_groups


def _topological_sort_groups(groups: list[dict]) -> list[dict]:
    """Return parallel_groups sorted in dependency order (topological sort)."""
    id_to_group  = {g["group_id"]: g for g in groups}
    visited: set[str] = set()
    order: list[dict] = []

    def _visit(gid: str) -> None:
        if gid in visited:
            return
        visited.add(gid)
        for dep in id_to_group.get(gid, {}).get("depends_on", []):
            if dep in id_to_group:
                _visit(dep)
        order.append(id_to_group[gid])

    for g in groups:
        _visit(g["group_id"])

    return order


def run_parallel_groups(
    task_graph: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
) -> RunResult:
    """Execute task groups from a parallel_groups-aware task graph.

    - Groups are executed in dependency order (topological sort).
    - Within a group marked can_run_parallel=True with multiple tasks,
      each task is launched as a separate subprocess worker.
    - Completion is signalled by .ai-pipeline/state/<group_id>.flag files.
    - Falls back to sequential execution when worker launch fails.
    """
    import time as _time

    groups   = task_graph.get("parallel_groups", [])
    tasks    = {str(t["id"]): t for t in task_graph.get("tasks", [])}
    ordered  = _topological_sort_groups(groups)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = PROMPTS_DIR / "implementation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

    dep_statuses: dict[str, str] = {tid: t.get("status", "pending") for tid, t in tasks.items()}
    completed_groups: list[str] = []
    failed_groups:    list[str] = []

    log_lines = [
        "# Implementation Log",
        f"\nStarted: {datetime.now(timezone.utc).isoformat()}",
        f"Branch: {state.get('branch', 'unknown')}",
        "",
    ]

    for group in ordered:
        group_id        = group["group_id"]
        can_parallel    = group.get("can_run_parallel", False)
        group_task_ids  = [str(t) for t in group.get("tasks", [])]
        pending_in_group = [
            tid for tid in group_task_ids
            if dep_statuses.get(tid) != "completed"
        ]

        if not pending_in_group:
            completed_groups.append(group_id)
            continue

        group_flag = STATE_DIR / f"{group_id}.flag"
        group_flag.unlink(missing_ok=True)

        if can_parallel and len(pending_in_group) > 1:
            # Launch each task as a separate worker subprocess
            procs: list[tuple[str, subprocess.Popen]] = []
            for tid in pending_in_group:
                task = tasks.get(tid)
                if not task:
                    continue

                blocked = [d for d in task.get("depends_on", []) if dep_statuses.get(str(d)) != "completed"]
                if blocked:
                    warn(f"  Task {tid} blocked by {blocked}. Skipping in parallel group.")
                    continue

                base_prompt = prompt_file.read_text(encoding="utf-8")
                context     = read_context_files(ARCHITECTURE_FILE, DESIGN_FILE, TASKS_FILE)
                task_prompt = (
                    f"{base_prompt}\n\n---\n## Current Task\n\n"
                    f"**Task {tid}:** {task['description']}\n\n"
                    f"**Group ID (write flag when done):** {group_id}\n\n"
                    f"**Dependencies:** {task.get('depends_on') or 'none'}\n\n"
                    f"---\n## Reference Documents\n\n{context}"
                )

                agent_cfg = config["agents"].get("implementation", {})
                command   = agent_cfg.get("command", "claude")
                extra_args = agent_cfg.get("args", [])
                model      = agent_cfg.get("model")
                resolved   = shutil.which(command) or command
                cmd        = [resolved] + extra_args
                if model:
                    cmd += ["--model", model]
                cmd += ["--print", task_prompt]

                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                try:
                    if platform.system() == "Windows":
                        proc = subprocess.Popen(
                            cmd,
                            cwd=str(REPO_ROOT),
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
                        )
                    else:
                        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
                    procs.append((tid, proc))
                    info(f"  Launched worker for Task {tid} ({task['description'][:50]})")
                except Exception as e:
                    warn(f"  Could not launch worker for Task {tid}: {e}. Running sequentially.")
                    try:
                        call_agent("implementation", task_prompt, config)
                        dep_statuses[tid] = "completed"
                        for t in task_graph["tasks"]:
                            if str(t["id"]) == tid:
                                t["status"]       = "completed"
                                t["completed_at"] = datetime.now(timezone.utc).isoformat()
                        _mark_task_complete_in_md(task["description"])
                        log_lines += [f"## Task {tid}: {task['description']}", "Status: completed",
                                      f"Completed: {datetime.now(timezone.utc).isoformat()}", "", "---", ""]
                    except Exception as e2:
                        error(f"  Task {tid} failed: {e2}")
                        log_lines += [f"## Task {tid}: {task['description']}",
                                      f"Status: FAILED", f"Error: {e2}", "", "---", ""]
                        failed_groups.append(group_id)

            # Poll for worker completion
            timeout = 3600
            poll_interval = 5
            elapsed = 0
            while procs and elapsed < timeout:
                still_running = []
                for tid, proc in procs:
                    rc = proc.poll()
                    if rc is not None:
                        if rc == 0:
                            dep_statuses[tid] = "completed"
                            for t in task_graph["tasks"]:
                                if str(t["id"]) == tid:
                                    t["status"]       = "completed"
                                    t["completed_at"] = datetime.now(timezone.utc).isoformat()
                            _mark_task_complete_in_md(tasks[tid]["description"])
                            success(f"  Worker Task {tid} completed.")
                            log_lines += [f"## Task {tid}: {tasks[tid]['description']}", "Status: completed",
                                          f"Completed: {datetime.now(timezone.utc).isoformat()}", "", "---", ""]
                        else:
                            error(f"  Worker Task {tid} failed (exit {rc}).")
                            log_lines += [f"## Task {tid}: {tasks[tid]['description']}",
                                          f"Status: FAILED", f"Error: exit {rc}", "", "---", ""]
                            failed_groups.append(group_id)
                    else:
                        still_running.append((tid, proc))
                procs = still_running
                if procs:
                    _time.sleep(poll_interval)
                    elapsed += poll_interval

            if procs:
                for tid, proc in procs:
                    proc.kill()
                    warn(f"  Worker Task {tid} timed out and was killed.")
                    failed_groups.append(group_id)
        else:
            # Sequential execution within group
            for tid in pending_in_group:
                task = tasks.get(tid)
                if not task:
                    continue

                blocked = [d for d in task.get("depends_on", []) if dep_statuses.get(str(d)) != "completed"]
                if blocked:
                    warn(f"  Task {tid} blocked by {blocked}. Skipping.")
                    continue

                print(f"{C.BOLD}  Task {tid}:{C.RESET} {task['description']}")
                base_prompt = prompt_file.read_text(encoding="utf-8")
                context     = read_context_files(ARCHITECTURE_FILE, DESIGN_FILE, TASKS_FILE)
                task_prompt = (
                    f"{base_prompt}\n\n---\n## Current Task\n\n"
                    f"**Task {tid}:** {task['description']}\n\n"
                    f"**Dependencies:** {task.get('depends_on') or 'none'}\n\n"
                    f"---\n## Reference Documents\n\n{context}"
                )
                try:
                    call_agent("implementation", task_prompt, config)
                    success(f"    Task {tid} complete.")
                    dep_statuses[tid] = "completed"
                    for t in task_graph["tasks"]:
                        if str(t["id"]) == tid:
                            t["status"]       = "completed"
                            t["completed_at"] = datetime.now(timezone.utc).isoformat()
                    _mark_task_complete_in_md(task["description"])
                    log_lines += [f"## Task {tid}: {task['description']}", "Status: completed",
                                  f"Completed: {datetime.now(timezone.utc).isoformat()}", "", "---", ""]
                except Exception as e:
                    error(f"    Task {tid} failed: {e}")
                    log_lines += [f"## Task {tid}: {task['description']}",
                                  f"Status: FAILED", f"Error: {e}", "", "---", ""]
                    failed_groups.append(group_id)
                    _log(f"Task {tid} failed: {e}")

        # Write group completion flag
        if group_id not in failed_groups:
            group_flag.write_text(
                f"group_id={group_id}\ntimestamp={datetime.now(timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )
            completed_groups.append(group_id)

    # Update task graph completed count and write log
    task_graph["completed"] = sum(1 for t in task_graph["tasks"] if t.get("status") == "completed")
    TASK_GRAPH_FILE.write_text(json.dumps(task_graph, indent=2), encoding="utf-8")
    IMPLEMENTATION_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    success(f"Log → {IMPLEMENTATION_LOG_FILE.relative_to(REPO_ROOT)}")

    return RunResult(
        success=len(failed_groups) == 0,
        failed_groups=failed_groups,
        completed_groups=completed_groups,
    )


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
        # Use list form + CREATE_NEW_CONSOLE to avoid shell=True injection
        subprocess.Popen(
            ["cmd", "/k", command],
            creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
        )
    elif platform.system() == "Darwin":
        # Escape command for AppleScript string context
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Terminal" to do script "{escaped}"'
        subprocess.Popen(["osascript", "-e", script])
    else:
        for term in ["gnome-terminal --", "xterm -e", "konsole -e"]:
            parts = term.split()
            exe = parts[0]
            if shutil.which(exe):
                # Use list form to avoid shell=True injection
                subprocess.Popen(parts + ["bash", "-c", f"{command}; exec bash"])
                break


def _wait_for_user(prompt: str) -> None:
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

    info("Opening Codex brainstorm session in a new terminal window ...")
    info(f"Context: {brainstorm_context.relative_to(REPO_ROOT)}")
    _open_popup_terminal("A.I.N. Brainstorm", f'{codex_cmd} "{brainstorm_prompt}"')

    print()
    print(f"{C.BOLD}{C.YELLOW}  BRAINSTORM IN PROGRESS{C.RESET}")
    print(f"  Codex is running in the popup window.")
    print(f"  Have your back-and-forth conversation there.")
    print(f"  When done, Codex should have written:")
    print(f"    {C.CYAN}{OPEN_QUESTIONS_FILE.relative_to(REPO_ROOT)}{C.RESET}")
    print(f"  If it did not, create that file manually with your Q&A summary.")
    print()
    _wait_for_user("Press Enter when your brainstorm session is complete")

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


def _safe_doc_path(filename: str) -> "Path | None":
    """Resolve filename relative to DOCS_DIR; return None if it escapes the directory."""
    try:
        target = (DOCS_DIR / filename.strip()).resolve()
        target.relative_to(DOCS_DIR.resolve())  # raises ValueError if outside
        return target
    except (ValueError, OSError):
        warn(f"Skipping unsafe path from agent output: {filename!r}")
        return None


def _parse_and_write_planning_docs(output: str) -> None:
    pattern = re.compile(
        r"<!--\s*FILE:\s*(?:docs/)?(\S+?)\s*-->(.*?)<!--\s*END:\s*(?:docs/)?\S+?\s*-->",
        re.DOTALL,
    )
    matches = list(pattern.finditer(output))
    if matches:
        for m in matches:
            target = _safe_doc_path(m.group(1))
            if target is None:
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
# Stage 5: Task Creation (chief)
# ─────────────────────────────────────────────────────────────

def run_task_creation(state: dict, config: dict) -> None:
    banner("Stage: Task Creation (Chief)")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = PROMPTS_DIR / "task_creation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

    ctx_files = [f for f in [PRD_FILE, DESIGN_FILE, FEATURE_SPEC_FILE, ARCHITECTURE_FILE] if f.exists()]
    agent_cmd = config.get("agents", {}).get("task_creation", {}).get("command", "")

    if agent_cmd == "chief":
        step(1, 3, "Building chief PRD ...")
        prompt = build_prompt(prompt_file, *ctx_files)
        _write_chief_prd(prompt)

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
            target = _safe_doc_path(m.group(1))
            if target is None:
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(tasks),
        "completed": sum(1 for t in tasks if t["status"] == "completed"),
    }
    TASK_GRAPH_FILE.write_text(json.dumps(graph, indent=2), encoding="utf-8")


def _write_chief_prd(prompt: str) -> None:
    """Write .chief/prds/main/prd.json and prd.md from the task-creation prompt."""
    CHIEF_PRDS_DIR.mkdir(parents=True, exist_ok=True)

    # prd.md: give chief's agent the full context and direct file-write instructions
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
                    "dependency-ordered markdown checkbox task list derived "
                    "from the planning documents in prd.md."
                ),
                "acceptanceCriteria": [
                    "File docs/TASKS.md exists",
                    "File contains at least one checkbox task in '- [ ] ...' format",
                    "Tasks are grouped by layer (Database, Backend, Frontend, etc.)",
                    "Each task names a specific file, class, method, or table",
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
                    "files_affected, and completed_at for every task in TASKS.md."
                ),
                "acceptanceCriteria": [
                    "File docs/TASK_GRAPH.json exists",
                    "File contains valid JSON with a 'tasks' array",
                    "Each task object has id, description, depends_on, status, files_affected, completed_at",
                    "Top-level keys include tasks, generated_at, total, completed",
                    "depends_on arrays reflect task dependencies from TASKS.md",
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


def _run_chief_tui(config: dict) -> None:
    """Run chief with a real TTY — no stdout capture so the TUI renders correctly."""
    agent_cfg  = config.get("agents", {}).get("task_creation", {})
    command    = agent_cfg.get("command", "chief")
    extra_args = agent_cfg.get("args", [])

    resolved = shutil.which(command)
    if not resolved:
        raise RuntimeError(
            f"Agent command not found: '{command}'. "
            "Edit .ai-pipeline/config.json to configure the 'task_creation' agent."
        )

    cmd = [resolved] + extra_args + ["--no-retry", "main"]
    _log(f"RUN (TUI): {' '.join(str(c) for c in cmd)}")
    info("Launching chief — work through the stories, then exit chief when done.")

    result = subprocess.run(cmd, cwd=str(REPO_ROOT), env={**os.environ})
    if result.returncode != 0:
        warn(f"chief exited with code {result.returncode}")


# ─────────────────────────────────────────────────────────────
# Stage 6: Approval Gate
# ─────────────────────────────────────────────────────────────

def run_waiting_approval(state: dict, config: dict) -> None:
    banner("Stage: Waiting for Approval")

    if PLANNING_APPROVED_FLAG.exists():
        success("Planning approved. Advancing to implementation.")
        set_stage("implementation", state)
        return

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
# Stage 7: Implementation (Claude)
# ─────────────────────────────────────────────────────────────

def _call_agent_with_fallback(
    agent_name: str,
    prompt: str,
    config: dict[str, Any],
    state: dict[str, Any],
    stage: str,
) -> str:
    """Call an agent and activate the fallback controller on token-limit errors.

    Returns the agent output string.
    Raises RuntimeError if the agent fails with a non-token-limit error or
    if the fallback flow results in abort.
    """
    fallback_cfg = config.get("fallback", {})
    fallback_enabled   = fallback_cfg.get("enabled", False)
    stages_with_fb     = fallback_cfg.get("stages_with_fallback", ["task_creation", "implementation"])
    notification_secs  = fallback_cfg.get("notification_timeout_secs", 180)
    codex_timeout_secs = fallback_cfg.get("codex_timeout_secs", 3600)
    on_codex_limit     = fallback_cfg.get("on_codex_limit", "pause")

    try:
        output = call_agent(agent_name, prompt, config)
    except RuntimeError as e:
        err_str = str(e)
        if (
            fallback_enabled
            and stage in stages_with_fb
            and is_token_limit_error(err_str)
        ):
            warn(f"Token limit detected in stage '{stage}'. Activating fallback controller.")
            sha = capture_rollback_point(state)
            decision = notify_fallback_and_get_decision(
                context={
                    "stage":           stage,
                    "agent":           agent_name,
                    "tasks_progress":  "see docs/TASK_GRAPH.json",
                    "rollback_sha":    sha,
                },
                timeout_secs=notification_secs,
            )
            state["fallback"]["triggered"]         = True
            state["fallback"]["trigger_reason"]    = "token_limit"
            state["fallback"]["trigger_agent"]     = agent_name
            state["fallback"]["trigger_stage"]     = stage
            state["fallback"]["trigger_timestamp"] = datetime.now(timezone.utc).isoformat()
            state["fallback"]["user_response"]     = decision

            if decision in ("switch", "auto_switch"):
                state["fallback"]["auto_switched_at"] = datetime.now(timezone.utc).isoformat()
                rollback_files = rollback_implementation_files(state)
                info(f"Rolled back {len(rollback_files)} files.")

                fb_agent = fallback_cfg.get("fallback_agent", "codex")
                state["fallback"]["fallback_agent"] = fb_agent
                state["fallback"]["fallback_stage"] = stage
                save_state(state)

                fb_prompt_name = (
                    "fallback_implementation_prompt.md"
                    if stage == "implementation"
                    else "fallback_task_creation_prompt.md"
                )
                fb_prompt_path = PROMPTS_DIR / fb_prompt_name
                ok = invoke_codex_fallback(stage, fb_prompt_path, codex_timeout_secs)
                if ok:
                    state["fallback"]["fallback_completed"] = True
                    save_state(state)
                    return ""
                else:
                    if on_codex_limit == "pause":
                        warn("Codex also hit a limit. Pausing pipeline — re-run to resume.")
                        sys.exit(0)
                    raise RuntimeError(f"Codex fallback failed for stage '{stage}'.")

            elif decision == "abort":
                rollback_implementation_files(state)
                save_state(state)
                raise RuntimeError(f"Pipeline aborted by user at stage '{stage}'.")

            else:  # wait
                warn("Waiting for token limit reset. Re-run when ready.")
                sys.exit(0)
        raise
    return output


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
        set_stage("verification", state)
        return

    info(f"Tasks: {len(tasks)} total | {len(pending)} pending")
    print()

    # Write initial implementation log template
    if not IMPLEMENTATION_LOG_FILE.exists():
        IMPLEMENTATION_LOG_FILE.write_text(
            "# Implementation Log\n\n"
            f"Started: {datetime.now(timezone.utc).isoformat()}\n"
            f"Branch: {state.get('branch', 'unknown')}\n\n"
            "<!-- Tasks will be appended below as they complete -->\n",
            encoding="utf-8",
        )

    # Use parallel_groups scheduler if present and non-empty
    parallel_groups = task_data.get("parallel_groups", [])
    if parallel_groups:
        result = run_parallel_groups(task_data, config, state)
        if not result.success:
            warn(f"Some groups failed: {result.failed_groups}")
        set_stage("verification", state)
        return

    # Fallback: sequential execution (no parallel_groups)
    prompt_file = PROMPTS_DIR / "implementation_prompt.md"
    if not prompt_file.exists():
        raise RuntimeError(f"Missing prompt: {prompt_file}")

    log_lines = [
        "# Implementation Log",
        f"\nStarted: {datetime.now(timezone.utc).isoformat()}",
        f"Branch: {state.get('branch', 'unknown')}",
        "",
    ]

    dep_statuses = {t["id"]: t["status"] for t in tasks}

    for task in pending:
        task_id     = task["id"]
        description = task["description"]
        deps        = task.get("depends_on", [])

        print(f"{C.BOLD}  Task {task_id}:{C.RESET} {description}")

        blocked = [d for d in deps if dep_statuses.get(d) != "completed"]
        if blocked:
            warn(f"    Blocked by tasks: {blocked}. Skipping.")
            continue

        base_prompt = prompt_file.read_text(encoding="utf-8")
        context     = read_context_files(ARCHITECTURE_FILE, DESIGN_FILE, TASKS_FILE)
        task_prompt = (
            f"{base_prompt}\n\n---\n## Current Task\n\n"
            f"**Task {task_id}:** {description}\n\n"
            f"**Dependencies:** {deps or 'none'}\n\n"
            f"---\n## Reference Documents\n\n{context}"
        )

        try:
            _call_agent_with_fallback("implementation", task_prompt, config, state, "implementation")
            success(f"    Task {task_id} complete.")

            for t in tasks:
                if t["id"] == task_id:
                    t["status"]       = "completed"
                    t["completed_at"] = datetime.now(timezone.utc).isoformat()
            dep_statuses[task_id] = "completed"
            task_data["completed"] = sum(1 for t in tasks if t.get("status") == "completed")
            TASK_GRAPH_FILE.write_text(json.dumps(task_data, indent=2), encoding="utf-8")

            _mark_task_complete_in_md(description)

            log_lines += [f"## Task {task_id}: {description}", "Status: completed",
                          f"Completed: {datetime.now(timezone.utc).isoformat()}", "", "---", ""]
        except Exception as e:
            error(f"    Task {task_id} failed: {e}")
            log_lines += [f"## Task {task_id}: {description}",
                          f"Status: FAILED", f"Error: {e}", "", "---", ""]
            _log(f"Task {task_id} failed: {e}")

    IMPLEMENTATION_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    success(f"Log → {IMPLEMENTATION_LOG_FILE.relative_to(REPO_ROOT)}")
    set_stage("verification", state)


def _mark_task_complete_in_md(description: str) -> None:
    if not TASKS_FILE.exists():
        return
    content = TASKS_FILE.read_text(encoding="utf-8")
    snippet = re.escape(description[:60])
    new = re.sub(r"- \[ \] " + snippet, "- [x] " + description[:60], content, count=1)
    TASKS_FILE.write_text(new, encoding="utf-8")

# ─────────────────────────────────────────────────────────────
# Stage 8: Verification (Audit)
# ─────────────────────────────────────────────────────────────

def run_verification(state: dict, config: dict) -> None:
    """Stage runner for verification. Gated by agent_teams.require_verification config key."""
    agent_teams_cfg = config.get("agent_teams", {})
    require_verification = agent_teams_cfg.get("require_verification", False)

    if not require_verification:
        info("Verification skipped (require_verification=false).")
        set_stage("validation", state)
        return

    passed = run_verification_stage(state, config)

    if not passed:
        # Write a FAILED report if the agent didn't write one
        if not VERIFICATION_REPORT_FILE.exists():
            ts = datetime.now(timezone.utc).isoformat()
            VERIFICATION_REPORT_FILE.write_text(
                f"# Verification Report\n\nGenerated: {ts}\n\n"
                "## Summary\n\nOverall verdict: **FAILED**\n\n"
                "Verification agent did not produce a report.\n",
                encoding="utf-8",
            )
        raise RuntimeError(
            "Verification failed. Review docs/VERIFICATION_REPORT.md before continuing."
        )

    set_stage("validation", state)


# ─────────────────────────────────────────────────────────────
# Stage 9: Validation
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
    """Install via a remote shell script. Returns True on success."""
    info(f"{command} — not found, installing via install script ...")
    if not shutil.which("curl"):
        error(f"{command} — curl not found, cannot run install script")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    # Fetch script first, then pipe into bash — avoids shell=True with string interpolation
    fetch = run_command(["curl", "-fsSL", url], capture=True, timeout=60)
    if fetch.returncode != 0:
        error(f"{command} — failed to download install script")
        warn(f"  Manual install: curl -fsSL {url} | bash")
        return False
    result = run_command(["bash"], capture=True, input_text=fetch.stdout, timeout=120)
    if result.returncode == 0:
        success(f"{command} — installed")
        return True
    error(f"{command} — installation failed")
    warn(f"  Manual install: curl -fsSL {url} | bash")
    if result.stderr:
        warn(f"  {result.stderr.strip()[:200]}")
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

CHIEF_DIR        = REPO_ROOT / ".chief"
CHIEF_CONFIG     = CHIEF_DIR / "config.yaml"
CHIEF_PRDS_DIR   = CHIEF_DIR / "prds" / "main"
CHIEF_PRD_FILE   = CHIEF_PRDS_DIR / "prd.json"
CHIEF_PRD_MD     = CHIEF_PRDS_DIR / "prd.md"

_CHIEF_CONFIG_TEMPLATE = dedent("""\
    worktree:
        setup: ""
    onComplete:
        push: false
        createPR: false
    agent:
        provider: "claude"
        cliPath: ""
""")


def _scaffold_chief_config() -> None:
    """Create .chief/ directory and config.yaml if chief is the task_creation agent."""
    cfg = load_config()
    task_cmd = cfg.get("agents", {}).get("task_creation", {}).get("command", "")
    if task_cmd != "chief":
        return

    CHIEF_PRDS_DIR.mkdir(parents=True, exist_ok=True)

    if not CHIEF_CONFIG.exists():
        # Derive agent provider from the implementation agent command
        impl_cmd = cfg.get("agents", {}).get("implementation", {}).get("command", "claude")
        provider = impl_cmd if impl_cmd in ("claude", "codex", "opencode") else "claude"
        content  = _CHIEF_CONFIG_TEMPLATE.replace('provider: "claude"', f'provider: "{provider}"')
        CHIEF_CONFIG.write_text(content, encoding="utf-8")
        success(f"Created {CHIEF_CONFIG.relative_to(REPO_ROOT)}")
    else:
        info(f"Skipped {CHIEF_CONFIG.relative_to(REPO_ROOT)} (already exists)")


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
        "verification_prompt.md",
        "prd_extraction_prompt.md",
        "fallback_task_creation_prompt.md",
        "fallback_implementation_prompt.md",
    ]
    for name in prompt_names:
        target = PROMPTS_DIR / name
        if not target.exists():
            content = res_files("ain").joinpath(f"data/prompts/{name}").read_text(encoding="utf-8")
            target.write_text(content, encoding="utf-8")
            success(f"Created {target.relative_to(REPO_ROOT)}")
        else:
            info(f"Skipped {target.relative_to(REPO_ROOT)} (already exists)")

    if not CLAUDE_MD_FILE.exists():
        content = res_files("ain").joinpath("data/CLAUDE.md").read_text(encoding="utf-8")
        CLAUDE_MD_FILE.write_text(content, encoding="utf-8")
        success(f"Created {CLAUDE_MD_FILE.relative_to(REPO_ROOT)}")
    else:
        info(f"Skipped {CLAUDE_MD_FILE.relative_to(REPO_ROOT)} (already exists)")

    _scaffold_chief_config()

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

    fallback_data   = state.get("fallback", {})
    prd_import_data = state.get("prd_import", {})
    fallback_active = fallback_data.get("triggered", False)
    mode            = "fallback" if fallback_active else "normal"

    print(f"  Stage:          {C.BOLD}{C.CYAN}{STAGE_LABELS.get(current, current)}{C.RESET}")
    print(f"  Mode:           {C.YELLOW if mode == 'fallback' else C.DIM}{mode}{C.RESET}")
    print(f"  Fallback active:{C.YELLOW if fallback_active else C.DIM} {fallback_active}{C.RESET}")
    if fallback_active:
        print(f"  Fallback agent: {C.DIM}{fallback_data.get('fallback_agent')}{C.RESET}")
        print(f"  Fallback stage: {C.DIM}{fallback_data.get('fallback_stage')}{C.RESET}")
    if prd_import_data.get("source"):
        print(f"  PRD import src: {C.DIM}{prd_import_data['source']}{C.RESET}")
    if state.get("branch"):
        print(f"  Branch:         {C.DIM}{state['branch']}{C.RESET}")
    if state.get("started_at"):
        print(f"  Started:        {C.DIM}{state['started_at']}{C.RESET}")
    if state.get("failure_reason"):
        print(f"  Reason:         {C.RED}{state['failure_reason']}{C.RESET}")
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
    "verification":        run_verification,
    "validation":          run_validation,
}


def run_pipeline(start_stage: str | None = None, single_stage: bool = False) -> None:
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

    current = state.get("current_stage", "idle")
    if current == "idle":
        state   = set_stage("scanning", state)
        current = "scanning"

    try:
        idx = STAGES.index(current)
    except ValueError:
        error(f"Unknown stage in state: {current}")
        sys.exit(1)

    to_run = [current] if single_stage else STAGES[idx:]

    prd_import_skipped = set(state.get("prd_import", {}).get("skipped_stages", []))

    for stage in to_run:
        if stage in ("idle", "done"):
            continue
        if stage in prd_import_skipped:
            info(f"Skipping stage '{stage}' (prd_import active).")
            continue
        runner = STAGE_RUNNERS.get(stage)
        if not runner:
            continue
        try:
            runner(state, config)
            state = load_state()
            # Refresh skipped stages after each step (state may have been updated)
            prd_import_skipped = set(state.get("prd_import", {}).get("skipped_stages", []))
        except RuntimeError as e:
            fail_pipeline(state, str(e))
        except KeyboardInterrupt:
            warn("\nInterrupted by user.")
            sys.exit(0)

    state = load_state()
    if state["current_stage"] == "done":
        banner("Pipeline Complete")
        show_status(state)

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
        """),
    )

    subparsers = parser.add_subparsers(dest="command")

    # ain init
    subparsers.add_parser("init", help="Scaffold .ai-pipeline/ into the current repo")

    # ain run
    run_parser = subparsers.add_parser("run", help="Run pipeline from current stage")
    run_parser.add_argument("--resume",     metavar="STAGE", help="Resume from a specific stage")
    run_parser.add_argument("--stage",      metavar="STAGE", help="Run only this stage")
    run_parser.add_argument("--prd-import", metavar="PATH",  help="Import PRD from file or directory and skip planning stages")
    run_parser.add_argument("--dry-run",    action="store_true", help="Validate inputs without executing stages")

    # Global flags (no subcommand)
    parser.add_argument("--status",     action="store_true", help="Show pipeline status")
    parser.add_argument("--approve",    action="store_true", help="Approve planning artifacts")
    parser.add_argument("--reset",      action="store_true", help="Reset pipeline to idle")
    parser.add_argument("--prd-import", metavar="PATH",      help="Import PRD from file or directory (shorthand, implies run)")

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

    if args.approve:
        APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
        PLANNING_APPROVED_FLAG.write_text(
            f"Approved: {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8"
        )
        success("Planning approved.")
        state = load_state()
        if state["current_stage"] == "waiting_approval":
            set_stage("implementation", state)
            success("Advanced to implementation. Run: ain run")
        return

    if args.status:
        show_status(load_state())
        return

    # Handle --prd-import (global shorthand or run subcommand)
    prd_import_path_str = (
        getattr(args, "prd_import", None)
        or (getattr(args, "command", None) == "run" and getattr(args, "prd_import", None))
    )
    if not prd_import_path_str and hasattr(args, "prd_import"):
        prd_import_path_str = args.prd_import

    if prd_import_path_str:
        ensure_config()
        config = load_config()
        state  = load_state()
        import_path = Path(prd_import_path_str).expanduser().resolve()

        if getattr(args, "dry_run", False):
            info(f"Dry run: would import PRD from {import_path}")
            return

        handle_prd_import(import_path, state)
        state = load_state()
        validate_prd_import(state)

        # If --approve was combined, auto-approve planning
        if getattr(args, "approve", False):
            APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
            PLANNING_APPROVED_FLAG.write_text(
                f"Approved: {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8"
            )
            success("Planning auto-approved (--approve flag).")
            set_stage("implementation", state)
            state = load_state()

        resume_stage = getattr(args, "resume", None) if args.command == "run" else None
        run_pipeline(start_stage=resume_stage or state.get("current_stage"))
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
