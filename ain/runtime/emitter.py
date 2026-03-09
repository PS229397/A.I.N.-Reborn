"""Event emitter for the pipeline."""

from __future__ import annotations

from typing import Any, Callable, List


class Emitter:
    """Simple synchronous event bus.

    Subscribers receive every event object emitted.  The pipeline core
    keeps one global ``Emitter`` instance when running in TUI/server mode;
    in plain CLI mode ``_EMITTER`` is ``None`` and events are silently
    dropped (no overhead).
    """

    def __init__(self) -> None:
        self._handlers: List[Callable[[Any], None]] = []

    def subscribe(self, handler: Callable[[Any], None]) -> None:
        """Register a callable that will be called with each event."""
        self._handlers.append(handler)

    def unsubscribe(self, handler: Callable[[Any], None]) -> None:
        """Remove a previously registered handler (no-op if not found)."""
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def emit(self, event: Any) -> None:
        """Dispatch *event* to all registered handlers."""
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                pass  # handlers must not crash the pipeline
