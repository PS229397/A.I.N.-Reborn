"""Compatibility wrapper that exposes the current Rich Live renderer."""

from __future__ import annotations

from typing import Any

from ain.ui.renderers.rich_live import RichLiveRenderer


class RichRenderer(RichLiveRenderer):
    """Backwards-compatible name used by older pipeline code."""

    def __init__(self, version: str = "0.1.8", **kwargs: Any) -> None:
        super().__init__(version=version, **kwargs)

    def subscribe(self, emitter: Any) -> None:
        self.attach_emitter(emitter)
        emitter.subscribe(self.handle)


__all__ = ["RichRenderer"]
