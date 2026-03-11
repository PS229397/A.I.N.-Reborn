"""Fullscreen Rich view for selecting pipeline mode inside the TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Shared neon palette for UI consistency.
_C_PRIMARY_TEXT = "#2EDCD1"
_C_SECONDARY_TEXT = "#23A19F"
_C_NEON_PINK = "bold #ff2d6f"
_C_BORDER = "#ff2d6f"


@dataclass
class ModeSelectResult:
    """Outcome of a key press in mode selection."""

    action: str  # "none" | "select" | "cancel"
    mode: str | None = None

    @property
    def is_select(self) -> bool:
        return self.action == "select"

    @property
    def is_cancel(self) -> bool:
        return self.action == "cancel"


class ModeSelectView:
    """Simple mode selector rendered fullscreen inside the Rich TUI."""

    def __init__(self, modes: Sequence[dict[str, str]], *, current_mode: str) -> None:
        if not modes:
            raise ValueError("modes cannot be empty")
        self._modes = [dict(mode) for mode in modes]
        self._current_mode = current_mode
        self._selected_idx = 0
        for idx, mode in enumerate(self._modes):
            if mode.get("key") == current_mode:
                self._selected_idx = idx
                break

    @property
    def current_mode(self) -> str:
        return self._current_mode

    def handle_key(self, key: str) -> ModeSelectResult:
        norm = self._normalize_key(key)
        if norm == "up":
            self._selected_idx = (self._selected_idx - 1) % len(self._modes)
            return ModeSelectResult("none")
        if norm == "down":
            self._selected_idx = (self._selected_idx + 1) % len(self._modes)
            return ModeSelectResult("none")
        if norm == "enter":
            return ModeSelectResult("select", mode=self._modes[self._selected_idx]["key"])
        if norm in {"q", "quit", "esc", "escape", "\x1b"}:
            return ModeSelectResult("cancel", mode=self._current_mode)
        return ModeSelectResult("none")

    def render(self) -> Panel:
        body = Text()
        body.append("Select Pipeline Mode\n\n", style=_C_NEON_PINK)
        for idx, mode in enumerate(self._modes):
            key = mode.get("key", "")
            label = mode.get("label", key)
            summary = mode.get("summary", "")
            is_selected = idx == self._selected_idx
            marker = ">" if is_selected else " "
            marker_style = _C_NEON_PINK if is_selected else _C_SECONDARY_TEXT
            text_style = _C_NEON_PINK if is_selected else _C_PRIMARY_TEXT
            current_marker = " (current)" if key == self._current_mode else ""
            body.append(f"{marker} ", style=marker_style)
            body.append(f"{label} [{key}]{current_marker}\n", style=text_style)
            if summary:
                body.append(f"  {summary}\n", style=_C_SECONDARY_TEXT)
        body.append("\n  ↑/↓ navigate  ENTER select", style=_C_SECONDARY_TEXT)

        footer_table = Table.grid(padding=(0, 1))
        footer_table.add_column(style=_C_NEON_PINK, no_wrap=True)
        footer_table.add_column(style=_C_SECONDARY_TEXT)

        return Panel(
            Group(body, footer_table),
            title="[bold #ff2d6f]Mode Select[/bold #ff2d6f]",
            border_style=_C_BORDER,
            padding=(0, 2),
        )

    @staticmethod
    def _normalize_key(key: str) -> str:
        if key in ("\r", "\n"):
            return "enter"
        return key.lower()
