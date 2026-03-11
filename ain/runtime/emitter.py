"""Event emitter for the pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List

from ain.models.state import HealthSummary, MultilineInputMode, StageTiming
from ain.runtime.events import (
    AnyEvent,
    CancelMultilineInputEvent,
    HealthCheckResult,
    OpenMultilineInputEvent,
    PlannedFileChangeCompleted,
    PlannedFileChangeStarted,
    StageCompleted,
    StageFailed,
    StageQueued,
    StageStarted,
    StageTimingUpdated,
    SubmitMultilineInputEvent,
)


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Emitter:
    """Simple synchronous event bus with helpers for structured events.

    Subscribers receive every event object emitted.  The pipeline core
    keeps one global ``Emitter`` instance when running in TUI/server mode;
    in plain CLI mode ``_EMITTER`` is ``None`` and events are silently
    dropped (no overhead).
    """

    def __init__(self) -> None:
        self._handlers: List[Callable[[AnyEvent], None]] = []

    def subscribe(self, handler: Callable[[AnyEvent], None]) -> None:
        """Register a callable that will be called with each event."""
        self._handlers.append(handler)

    def unsubscribe(self, handler: Callable[[AnyEvent], None]) -> None:
        """Remove a previously registered handler (no-op if not found)."""
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def emit(self, event: AnyEvent) -> None:
        """Dispatch *event* to all registered handlers."""

        handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # Handlers must not crash the pipeline
                pass

    # ------------------------------------------------------------------
    # Stage lifecycle helpers
    # ------------------------------------------------------------------

    def stage_queued(self, stage_id: str, stage_name: str, index: int = 0) -> None:
        self.emit(StageQueued(stage_id=stage_id, stage_name=stage_name, index=index))

    def stage_started(
        self,
        stage_id: str,
        *,
        stage_name: str | None = None,
        index: int | None = None,
        started_at: str | None = None,
    ) -> None:
        self.emit(
            StageStarted(
                stage_id=stage_id,
                stage_name=stage_name,
                index=index,
                started_at=started_at or _now_iso(),
            )
        )

    def stage_completed(
        self,
        stage_id: str,
        *,
        stage_name: str | None = None,
        duration_ms: int = 0,
        status: str = "success",
        ended_at: str | None = None,
    ) -> None:
        self.emit(
            StageCompleted(
                stage_id=stage_id,
                stage_name=stage_name,
                duration_ms=duration_ms,
                status=status,
                ended_at=ended_at or _now_iso(),
            )
        )

    def stage_failed(
        self,
        stage_id: str,
        *,
        stage_name: str | None = None,
        error: str = "",
        error_code: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        self.emit(
            StageFailed(
                stage_id=stage_id,
                stage_name=stage_name,
                error=error,
                error_code=error_code,
                ended_at=ended_at or _now_iso(),
            )
        )

    # ------------------------------------------------------------------
    # Planned file change helpers
    # ------------------------------------------------------------------

    def planned_file_change_started(
        self, path: str, operation: str, *, started_at: str | None = None
    ) -> None:
        self.emit(
            PlannedFileChangeStarted(
                path=path,
                operation=operation,
                started_at=started_at or _now_iso(),
            )
        )

    def planned_file_change_completed(
        self,
        path: str,
        operation: str,
        *,
        status: str = "success",
        error: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        self.emit(
            PlannedFileChangeCompleted(
                path=path,
                operation=operation,
                status=status,
                error=error,
                ended_at=ended_at or _now_iso(),
            )
        )

    # ------------------------------------------------------------------
    # Multiline input helpers
    # ------------------------------------------------------------------

    def open_multiline_input(
        self,
        *,
        id: str,
        mode: MultilineInputMode,
        title: str,
        prompt: str,
        initial_text: str | None = None,
        source_stage: str = "",
    ) -> None:
        """Emit an event to open a multiline input overlay in the UI."""

        self.emit(
            OpenMultilineInputEvent(
                id=id,
                mode=mode,
                title=title,
                prompt=prompt,
                initial_text=initial_text,
                source_stage=source_stage,
            )
        )

    def submit_multiline_input(self, *, id: str, mode: MultilineInputMode, value: str) -> None:
        """Emit an event indicating a multiline input submission."""

        self.emit(SubmitMultilineInputEvent(id=id, mode=mode, value=value))

    def cancel_multiline_input(self, *, id: str, mode: MultilineInputMode) -> None:
        """Emit an event indicating the active multiline input session was cancelled."""

        self.emit(CancelMultilineInputEvent(id=id, mode=mode))

    # ------------------------------------------------------------------
    # Health and timing helpers
    # ------------------------------------------------------------------

    def health_check_result(
        self, summary: HealthSummary, *, checked_at: str | None = None
    ) -> None:
        self.emit(HealthCheckResult(summary=summary, checked_at=checked_at or _now_iso()))

    def stage_timing_updated(self, stage_id: str, timing: StageTiming) -> None:
        self.emit(StageTimingUpdated(stage_id=stage_id, timing=timing))
