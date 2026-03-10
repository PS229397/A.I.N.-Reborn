"""Compatibility helpers for environment health reporting."""

from __future__ import annotations

from pathlib import Path

from ain.models.state import HealthSummary


def get_effective_config(project_root: Path | None = None) -> dict:
    """Return a minimal effective configuration payload."""

    _ = project_root
    return {}


def get_health_summary(project_root: Path | None = None) -> HealthSummary:
    """Return a minimal healthy summary for legacy pipeline runs."""

    root = Path(project_root) if project_root is not None else Path.cwd()
    return HealthSummary(
        external_binaries={},
        config_files={},
        state_files={
            "project_root": {
                "name": str(root),
                "status": "ok",
                "message": "health checks not configured in this checkout",
                "details": {},
            }
        },
        overall_status="healthy",
    )
