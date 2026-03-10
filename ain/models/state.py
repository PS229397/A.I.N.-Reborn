"""Minimal state models required by the current checkout."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass
class StageTiming:
    stage_name: str
    started_at: str
    ended_at: str
    duration_ms: int
    status: str


@dataclass
class HealthSummary:
    external_binaries: dict[str, Any] = field(default_factory=dict)
    config_files: dict[str, Any] = field(default_factory=dict)
    state_files: dict[str, Any] = field(default_factory=dict)
    overall_status: str = "healthy"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlannedFileChange:
    path: str
    content: str
    operation: Literal["create", "overwrite", "skip_if_exists"] = "create"
    allow_overwrite: bool = False
    ensure_parent_dir: bool = True


@dataclass
class PipelineState:
    version: int
    current_stage: str
    status: str
    last_error: dict[str, Any] | None
    artifacts: dict[str, Any]
    created_at: str
    updated_at: str
    planned_file_changes: list[PlannedFileChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
