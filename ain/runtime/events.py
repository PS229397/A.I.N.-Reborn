"""Typed event objects emitted by the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LogLevel(str, Enum):
    INFO  = "info"
    WARN  = "warn"
    ERROR = "error"


class LogSource(str, Enum):
    PIPELINE = "pipeline"
    AGENT    = "agent"


class RunStatus(str, Enum):
    DONE        = "done"
    FAILED      = "failed"
    INTERRUPTED = "interrupted"


# ── Log ──────────────────────────────────────────────────────

@dataclass
class LogLine:
    ts:       str
    level:    LogLevel
    source:   LogSource
    message:  str
    stage_id: Optional[str] = None


# ── Run lifecycle ─────────────────────────────────────────────

@dataclass
class RunStarted:
    run_id:     str
    started_at: str
    mode:       str = "plain"


@dataclass
class RunCompleted:
    run_id:   str
    ended_at: str
    status:   RunStatus = RunStatus.DONE


# ── Stage lifecycle ───────────────────────────────────────────

@dataclass
class StageQueued:
    stage_id:   str
    stage_name: str
    index:      int = 0


@dataclass
class StageStarted:
    stage_id:   str
    started_at: str


@dataclass
class StageCompleted:
    stage_id:    str
    ended_at:    str
    duration_ms: int = 0


@dataclass
class StageFailed:
    stage_id: str
    ended_at: str
    error:    str = ""


# ── Task lifecycle ────────────────────────────────────────────

@dataclass
class TaskStarted:
    task_id:     str
    description: str
    agent:       str
    started_at:  str


@dataclass
class TaskCompleted:
    task_id:     str
    description: str
    ended_at:    str
    duration_ms: int = 0


@dataclass
class TaskFailed:
    task_id:     str
    description: str
    ended_at:    str
    error:       str = ""


# ── Agent output ──────────────────────────────────────────────

@dataclass
class AgentOutput:
    ts:    str
    line:  str
    agent: str = ""


# ── Approval ──────────────────────────────────────────────────

@dataclass
class AwaitingApproval:
    run_id:   str
    stage_id: str


@dataclass
class ApprovalReceived:
    run_id: str
    actor:  str
    at:     str


@dataclass
class WaitingApprovalEvent:
    """Emitted when the pipeline enters the waiting-for-approval gate."""

    run_id: str


@dataclass
class ApprovedEvent:
    """Emitted when a run has been approved and can resume."""

    run_id: str
