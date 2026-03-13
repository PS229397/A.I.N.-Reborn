"""Rich-based fullscreen multiline input view with local buffer management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import re

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ain.models.state import MultilineInputMode, MultilineInputState

# Reuse the same palette as the primary Rich renderer for visual consistency.
_C_PRIMARY_TEXT = "#2EDCD1"
_C_SECONDARY_TEXT = "#23A19F"
_C_NEON_PINK = "bold #ff2d6f"
_C_BORDER = "#ff2d6f"
_C_ERROR = "bold red"
_FEATURE_DESCRIPTION_MAX_BODY_ROWS = 10


@dataclass
class MultilineInputResult:
    """Result of handling a keypress inside the multiline input view."""

    action: str  # "none" | "submit" | "cancel"
    value: str | None = None

    @property
    def is_submit(self) -> bool:
        return self.action == "submit"

    @property
    def is_cancel(self) -> bool:
        return self.action == "cancel"


class MultilineInputView:
    """Editable Rich view used for feature descriptions and denial feedback."""

    def __init__(
        self,
        *,
        id: str = "",
        title: str,
        prompt: str,
        mode: MultilineInputMode = MultilineInputMode.FEATURE_DESCRIPTION,
        initial_text: str | None = None,
        source_stage: str = "",
        body_height: int = 10,
    ) -> None:
        self.id = id
        self.title = title
        self.prompt = prompt
        self.mode = mode
        self.source_stage = source_stage
        self._body_height = self._clamp_body_height(body_height)
        self._lines = self._coerce_lines(initial_text or "")
        self._cursor_row = self._first_editable_row()
        self._cursor_col = self._initial_cursor_col(self._cursor_row)
        self._scroll_top = 0
        self.validation_error: str | None = None
        self._ensure_cursor_visible()

    @classmethod
    def from_state(cls, state: MultilineInputState, *, body_height: int = 10) -> "MultilineInputView":
        """Instantiate a view from a persisted MultilineInputState."""

        text = state.value or state.initial_text or ""
        return cls(
            id=state.id,
            title=state.title,
            prompt=state.prompt,
            initial_text=text,
            mode=state.mode,
            source_stage=state.source_stage,
            body_height=body_height,
        )

    # ------------------------------------------------------------------ public API

    @property
    def value(self) -> str:
        """Current buffer contents."""

        return "\n".join(self._lines)

    def set_body_height(self, rows: int) -> None:
        """Update the max body rows and keep the cursor in view."""

        if rows > 0:
            self._body_height = self._clamp_body_height(rows)
            self._ensure_cursor_visible()

    def handle_key(
        self,
        key: str,
        *,
        ctrl: bool = False,
        alt: bool = False,
        shift: bool = False,
    ) -> MultilineInputResult:
        """Process a single key press and update the buffer/cursor."""

        norm = self._normalize_key(key)

        if self._is_cancel(norm):
            return MultilineInputResult(action="cancel")

        if self._is_submit(norm, ctrl=ctrl, alt=alt, shift=shift):
            if not self.value.strip():
                self.validation_error = "Input cannot be empty"
                return MultilineInputResult(action="none")
            self.validation_error = None
            return MultilineInputResult(action="submit", value=self.value)

        self.validation_error = None

        if self._is_newline(norm):
            self._enter_or_next()
        elif norm in ("backspace", "\x7f", "\x08"):
            self._backspace()
        elif norm in ("delete", "del"):
            self._delete()
        elif norm == "left":
            self._move_left()
        elif norm == "right":
            self._move_right()
        elif norm == "up":
            self._move_up()
        elif norm == "down":
            self._move_down()
        elif norm == "home":
            self._cursor_col = max(self._prefix_len(self._cursor_row), 0)
        elif norm == "end":
            self._cursor_col = len(self._lines[self._cursor_row])
        elif norm == "tab":
            self._insert_text("    ")
        elif len(norm) == 1 and norm.isprintable():
            self._insert_text(norm)

        self._ensure_cursor_visible()
        return MultilineInputResult(action="none")

    def render(self) -> Panel:
        """Return a Rich renderable representing the fullscreen view."""

        header = self._render_header()
        prompt = Text(self.prompt, style=_C_SECONDARY_TEXT)
        spacer = Text("")
        body_panel = self._render_body()
        footer = self._render_footer()

        content = Group(header, prompt, spacer, body_panel, footer)
        return Panel(
            content,
            title=self._panel_title(),
            border_style=_C_BORDER,
            # Keep the outer frame tighter so the global keybar stays visible.
            padding=(0, 2),
        )

    # ------------------------------------------------------------------ buffer ops

    @staticmethod
    def _coerce_lines(text: str) -> list[str]:
        lines = text.splitlines()
        if text.endswith("\n"):
            lines.append("")
        if not lines:
            lines = [""]
        return lines

    def _first_editable_row(self) -> int:
        for idx, line in enumerate(self._lines):
            if self._is_editable_row(idx):
                return idx
        return 0

    def _initial_cursor_col(self, row: int) -> int:
        if not self._lines:
            return 0
        return max(self._prefix_len(row), 0)

    @staticmethod
    def _normalize_key(key: str) -> str:
        if key in ("\r", "\n"):
            return "enter"
        # Only lowercase multi-char key names (e.g. "Enter", "Backspace");
        # preserve case for single printable characters so capitals work.
        if len(key) > 1:
            return key.lower()
        return key

    @staticmethod
    def _is_cancel(key: str) -> bool:
        return key in ("esc", "escape", "\x1b")

    @staticmethod
    def _is_newline(key: str) -> bool:
        return key in ("enter", "return", "\n", "\r")

    @staticmethod
    def _is_submit(key: str, *, ctrl: bool, alt: bool, shift: bool) -> bool:
        combo_labels = {
            "ctrl+enter",
            "ctrl+return",
            "shift+alt+enter",
            "alt+shift+enter",
            "submit",
        }
        if ctrl and key in ("enter", "return"):
            return True
        if shift and alt and key in ("enter", "return"):
            return True
        return key in combo_labels

    def _prefix_len(self, row_idx: int) -> int:
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            line = self._lines[row_idx]
            if line.lstrip().startswith("A:"):
                # Lock the label "A:" itself, allow editing immediately after the space.
                return line.index("A:") + 3 if "A:" in line else 0
            m = re.match(r"\s*\d+\.\s", line)
            if m:
                # questions are read-only
                return len(line)
        return 0

    def _is_editable_row(self, row_idx: int) -> bool:
        pref = self._prefix_len(row_idx)
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            line = self._lines[row_idx]
            if line.lstrip().startswith("A:"):
                return True
            # question rows are not editable
            return False
        return True

    def _insert_text(self, text: str) -> None:
        line = self._lines[self._cursor_row]
        prefix = self._prefix_len(self._cursor_row)
        if self.mode == MultilineInputMode.PLANNING_ANSWERS and not line.lstrip().startswith("A:"):
            return  # non-editable row
        if self._cursor_col < prefix:
            self._cursor_col = prefix
        before = line[: self._cursor_col]
        after = line[self._cursor_col :]
        self._lines[self._cursor_row] = before + text + after
        self._cursor_col += len(text)

    def _enter_or_next(self) -> None:
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            # Jump to the next editable answer row.
            nxt = self._cursor_row + 1
            while nxt < len(self._lines) and not self._lines[nxt].lstrip().startswith("A:"):
                nxt += 1
            if nxt < len(self._lines):
                self._cursor_row = nxt
                self._cursor_col = max(self._prefix_len(nxt), 0)
                self._ensure_cursor_visible()
            return
        self._insert_newline()

    def _insert_newline(self) -> None:
        line = self._lines[self._cursor_row]
        before = line[: self._cursor_col]
        after = line[self._cursor_col :]
        self._lines[self._cursor_row] = before
        self._lines.insert(self._cursor_row + 1, after)
        self._cursor_row += 1
        self._cursor_col = 0

    def _backspace(self) -> None:
        prefix = self._prefix_len(self._cursor_row)
        if self._cursor_col > prefix:
            line = self._lines[self._cursor_row]
            self._lines[self._cursor_row] = line[: self._cursor_col - 1] + line[self._cursor_col :]
            self._cursor_col -= 1
            return
        if self._cursor_row == 0:
            return
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            return
        prev_line = self._lines[self._cursor_row - 1]
        current_line = self._lines[self._cursor_row]
        self._cursor_col = len(prev_line)
        self._lines[self._cursor_row - 1] = prev_line + current_line
        del self._lines[self._cursor_row]
        self._cursor_row -= 1

    def _delete(self) -> None:
        prefix = self._prefix_len(self._cursor_row)
        line = self._lines[self._cursor_row]
        if self._cursor_col < len(line):
            if self._cursor_col < prefix:
                return
            self._lines[self._cursor_row] = line[: self._cursor_col] + line[self._cursor_col + 1 :]
            return
        if self._cursor_row >= len(self._lines) - 1:
            return
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            return
        self._lines[self._cursor_row] = line + self._lines[self._cursor_row + 1]
        del self._lines[self._cursor_row + 1]

    def _move_left(self) -> None:
        min_col = self._prefix_len(self._cursor_row)
        if self._cursor_col > min_col:
            self._cursor_col -= 1
            return
        if self._cursor_row > 0:
            prev = self._cursor_row - 1
            while prev >= 0 and not self._is_editable_row(prev):
                prev -= 1
            if prev >= 0:
                self._cursor_row = prev
                self._cursor_col = max(self._prefix_len(prev), len(self._lines[prev]))

    def _move_right(self) -> None:
        line = self._lines[self._cursor_row]
        if self._cursor_col < len(line):
            self._cursor_col += 1
            return
        if self._cursor_row < len(self._lines) - 1:
            nxt = self._cursor_row + 1
            while nxt < len(self._lines) and not self._is_editable_row(nxt):
                nxt += 1
            if nxt < len(self._lines):
                self._cursor_row = nxt
                self._cursor_col = max(self._prefix_len(nxt), 0)

    def _move_up(self) -> None:
        if self._cursor_row == 0:
            return
        prev = self._cursor_row - 1
        while prev >= 0 and not self._is_editable_row(prev):
            prev -= 1
        if prev >= 0:
            self._cursor_row = prev
            self._cursor_col = max(self._prefix_len(prev), min(self._cursor_col, len(self._lines[prev])))

    def _move_down(self) -> None:
        if self._cursor_row >= len(self._lines) - 1:
            return
        nxt = self._cursor_row + 1
        while nxt < len(self._lines) and not self._is_editable_row(nxt):
            nxt += 1
        if nxt < len(self._lines):
            self._cursor_row = nxt
            self._cursor_col = max(self._prefix_len(nxt), min(self._cursor_col, len(self._lines[nxt])))

    def _ensure_cursor_visible(self) -> None:
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            # Keep the question line visible by biasing one row above the cursor.
            target_top = max(0, self._cursor_row - 1)
            if target_top < self._scroll_top:
                self._scroll_top = target_top
            elif self._cursor_row >= self._scroll_top + self._body_height:
                self._scroll_top = self._cursor_row - self._body_height + 1
            return

        if self._cursor_row < self._scroll_top:
            self._scroll_top = self._cursor_row
        elif self._cursor_row >= self._scroll_top + self._body_height:
            self._scroll_top = self._cursor_row - self._body_height + 1

    def _clamp_body_height(self, rows: int) -> int:
        height = max(3, rows)
        if self.mode in (MultilineInputMode.FEATURE_DESCRIPTION, MultilineInputMode.PLANNING_ANSWERS):
            return min(height, _FEATURE_DESCRIPTION_MAX_BODY_ROWS)
        return height

    # ------------------------------------------------------------------ rendering

    def _panel_title(self) -> str:
        mode_label = (
            "Feature / Bug"
            if self.mode == MultilineInputMode.FEATURE_DESCRIPTION
            else "Brainstorm Answers"
            if self.mode == MultilineInputMode.PLANNING_ANSWERS
            else "Task denial"
        )
        return f"[bold #ff2d6f]{mode_label}[/bold #ff2d6f]"

    def _render_header(self) -> Text:
        text = Text()
        text.append(self.title, style=_C_NEON_PINK)
        if self.source_stage:
            text.append(f"  // {self.source_stage}", style=_C_SECONDARY_TEXT)
        return text

    def _render_body(self) -> Panel:
        lines = list(self._visible_lines())
        return Panel(
            Align.left(Group(*lines)),
            border_style=_C_BORDER,
            padding=(0, 1),
        )

    def _visible_lines(self) -> Iterable[Text]:
        start = self._scroll_top
        viewport_end = start + self._body_height
        content_end = min(len(self._lines), viewport_end)
        for row_idx in range(start, content_end):
            yield self._render_line(row_idx)
        for _ in range(content_end, viewport_end):
            filler = Text()
            filler.append("     ", style=_C_SECONDARY_TEXT)
            filler.append("", style=_C_PRIMARY_TEXT)
            yield filler

    def _render_line(self, row_idx: int) -> Text:
        line = self._lines[row_idx]
        text = Text()
        cursor_here = row_idx == self._cursor_row
        if self.mode == MultilineInputMode.PLANNING_ANSWERS:
            pref_len = self._prefix_len(row_idx)
            prefix = line[:pref_len]
            rest = line[pref_len:]
            text.append(prefix, style=_C_SECONDARY_TEXT)
            if cursor_here:
                col = min(max(self._cursor_col - pref_len, 0), len(rest))
                text.append(rest[:col], style=_C_PRIMARY_TEXT)
                text.append("█", style=_C_NEON_PINK)
                text.append(rest[col:], style=_C_PRIMARY_TEXT)
            else:
                text.append(rest, style=_C_PRIMARY_TEXT)
        else:
            if cursor_here:
                col = min(self._cursor_col, len(line))
                text.append(line[:col], style=_C_PRIMARY_TEXT)
                text.append("█", style=_C_NEON_PINK)
                text.append(line[col:], style=_C_PRIMARY_TEXT)
            else:
                text.append(line, style=_C_PRIMARY_TEXT)
        return text

    def _render_footer(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(style=_C_NEON_PINK, no_wrap=True)
        table.add_column(style=_C_SECONDARY_TEXT)
        table.add_row("Ctrl+Enter", "Submit")
        if self.validation_error:
            table.add_row("", f"[{_C_ERROR}]{self.validation_error}[/{_C_ERROR}]")
        return Panel(
            table,
            border_style=_C_BORDER,
            padding=(0, 1),
        )
