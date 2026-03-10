"""Compatibility logging hooks used by the legacy pipeline."""

from __future__ import annotations

from typing import Any

from ain.models.state import StageTiming


def log_stage_timing(stage_id: str, timing: StageTiming) -> None:
    """No-op timing logger for checkouts without the structured log service."""


def log_error_record(
    code: str,
    message: str,
    *,
    stage: str | None = None,
    details: dict[str, Any] | None = None,
    recoverable: bool = False,
) -> None:
    """No-op error logger for checkouts without the structured log service."""

