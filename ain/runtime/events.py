"""Typed event objects emitted by the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from ain.models.state import HealthSummary, MultilineInputMode, StageTiming


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LogSource(str, Enum):
    PIPELINE = "pipeline"
    AGENT = "agent"


class RunStatus(str, Enum):
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


@dataclass
class LogLine:
    ts: str
    level: LogLevel
    source: LogSource
    message: str
    stage_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


@dataclass
class RunStarted:
    run_id: str
    started_at: str
    mode: str = "plain"


@dataclass
class RunCompleted:
    run_id: str
    ended_at: str
    status: RunStatus = RunStatus.DONE


# ---------------------------------------------------------------------------
# Stage lifecycle
# ---------------------------------------------------------------------------


@dataclass
class StageQueued:
    stage_id: str
    stage_name: str
    index: int = 0


@dataclass
class StageStarted:
    stage_id: str
    started_at: str
    stage_name: Optional[str] = None
    index: Optional[int] = None


@dataclass
class StageCompleted:
    stage_id: str
    ended_at: str
    duration_ms: int = 0
    stage_name: Optional[str] = None
    status: str = "success"


@dataclass
class StageFailed:
    stage_id: str
    ended_at: str
    error: str = ""
    stage_name: Optional[str] = None
    error_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Planned file changes
# ---------------------------------------------------------------------------


@dataclass
class PlannedFileChangeStarted:
    """Emitted just before applying a planned file change."""

    path: str
    operation: str
    started_at: str


@dataclass
class PlannedFileChangeCompleted:
    """Emitted after a planned file change is processed."""

    path: str
    operation: str
    status: str
    ended_at: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


@dataclass
class TaskStarted:
    task_id: str
    description: str
    agent: str
    started_at: str


@dataclass
class TaskCompleted:
    task_id: str
    description: str
    ended_at: str
    duration_ms: int = 0


@dataclass
class TaskFailed:
    task_id: str
    description: str
    ended_at: str
    error: str = ""


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------


@dataclass
class AgentOutput:
    ts: str
    line: str
    agent: str = ""


# ---------------------------------------------------------------------------
# Approval gates
# ---------------------------------------------------------------------------


@dataclass
class AwaitingApproval:
    run_id: str
    stage_id: str


@dataclass
class ApprovalReceived:
    run_id: str
    actor: str
    at: str


@dataclass
class WaitingApprovalEvent:
    """Emitted when the pipeline enters the waiting-for-approval gate."""

    run_id: str


@dataclass
class ApprovedEvent:
    """Emitted when a run has been approved and can resume."""

    run_id: str


# ---------------------------------------------------------------------------
# Multiline input
# ---------------------------------------------------------------------------


@dataclass
class OpenMultilineInputEvent:
    """Emitted when the UI should open a multiline input overlay."""

    id: str
    mode: MultilineInputMode
    title: str
    prompt: str
    initial_text: Optional[str] = None
    source_stage: str = ""


@dataclass
class SubmitMultilineInputEvent:
    """Emitted when a multiline input value has been submitted."""

    id: str
    mode: MultilineInputMode
    value: str


@dataclass
class CancelMultilineInputEvent:
    """Emitted when the active multiline input view is cancelled."""

    id: str
    mode: MultilineInputMode


# ---------------------------------------------------------------------------
# Health and timings
# ---------------------------------------------------------------------------


@dataclass
class HealthCheckResult:
    """Emitted after a health check run with the aggregated summary."""

    summary: HealthSummary
    checked_at: str


@dataclass
class StageTimingUpdated:
    """Emitted when timing data for a stage is recorded or updated."""

    stage_id: str
    timing: StageTiming


# ---------------------------------------------------------------------------
# Union helper for type checkers
# ---------------------------------------------------------------------------


AnyEvent = Union[
    LogLine,
    RunStarted,
    RunCompleted,
    StageQueued,
    StageStarted,
    StageCompleted,
    StageFailed,
    TaskStarted,
    TaskCompleted,
    TaskFailed,
    AgentOutput,
    AwaitingApproval,
    ApprovalReceived,
    WaitingApprovalEvent,
    ApprovedEvent,
    OpenMultilineInputEvent,
    SubmitMultilineInputEvent,
    CancelMultilineInputEvent,
    PlannedFileChangeStarted,
    PlannedFileChangeCompleted,
    HealthCheckResult,
    StageTimingUpdated,
]


__all__ = [
    "LogLevel",
    "LogSource",
    "RunStatus",
    "LogLine",
    "RunStarted",
    "RunCompleted",
    "StageQueued",
    "StageStarted",
    "StageCompleted",
    "StageFailed",
    "PlannedFileChangeStarted",
    "PlannedFileChangeCompleted",
    "TaskStarted",
    "TaskCompleted",
    "TaskFailed",
    "AgentOutput",
    "AwaitingApproval",
    "ApprovalReceived",
    "WaitingApprovalEvent",
    "ApprovedEvent",
    "OpenMultilineInputEvent",
    "SubmitMultilineInputEvent",
    "CancelMultilineInputEvent",
    "HealthCheckResult",
    "StageTimingUpdated",
    "AnyEvent",
]
