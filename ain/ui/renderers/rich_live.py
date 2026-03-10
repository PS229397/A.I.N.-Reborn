"""Rich Live TUI renderer for A.I.N. Pipeline.

Layout (top -> bottom):
    +---------------------------------------------+
    |  Status bar: version - run status - elapsed |
    +------------------+--------------------------+
    |  Pipeline panel  |  Stream panel            |
    |  (stage list)    |  (log feed)              |
    +------------------+--------------------------+
    |  Keybar: active keybindings                 |
    +---------------------------------------------+
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ain.models.state import StageTiming
from ain.runtime.events import (
    AnyEvent,
    AgentOutput,
    ApprovalReceived,
    AwaitingApproval,
    LogLevel,
    LogLine,
    RunCompleted,
    RunStarted,
    RunStatus,
    StageFailed,
    StageCompleted,
    StageQueued,
    StageStarted,
    TaskCompleted,
    TaskFailed,
    TaskStarted,
)

# Maximum log lines retained in the stream panel buffer.
_MAX_LOG_LINES = 200

# -- Cyberpunk colour palette: teal / red-pink ------------------
# Primary text: #2EDCD1   Secondary text: #23A19F
_C_PRIMARY_TEXT = "#2EDCD1"
_C_SECONDARY_TEXT = "#23A19F"
_C_NEON_CYAN  = f"bold {_C_PRIMARY_TEXT}"
_C_NEON_PINK  = "bold #ff2d6f"          # red-leaning neon pink accent
_C_NEON_GREEN = "bold green"
_C_NEON_AMBER = "bold yellow"
_C_NEON_RED   = "bold red"
_C_DIM_CYAN   = _C_SECONDARY_TEXT

# Single border colour used on every panel for visual consistency.
_C_BORDER     = "#ff2d6f"

# Stage status -> (symbol, Rich color)
_STAGE_STYLE: dict[str, tuple[str, str]] = {
    "queued":  ("◈", _C_DIM_CYAN),
    "running": ("▶", _C_NEON_PINK),
    "done":    ("◆", _C_NEON_CYAN),
    "failed":  ("✖", _C_NEON_CYAN),
}

# Log level -> Rich color
_LEVEL_COLOR: dict[LogLevel, str] = {
    LogLevel.DEBUG: _C_NEON_CYAN,
    LogLevel.INFO:  _C_NEON_PINK,
    LogLevel.WARN:  _C_NEON_CYAN,
    LogLevel.ERROR: _C_NEON_CYAN,
}

# Run status -> Rich color
_RUN_STATUS_COLOR: dict[str, str] = {
    "idle":             _C_NEON_CYAN,
    "running":          _C_NEON_PINK,
    "waiting_approval": _C_NEON_CYAN,
    "done":             _C_NEON_CYAN,
    "failed":           _C_NEON_CYAN,
    "interrupted":      _C_NEON_CYAN,
}

# Bottom keybar entries: (key label, action description)
_KEYBAR_ENTRIES: list[tuple[str, str]] = [
    ("Q", "jack out"),
    ("R", "reboot"),
    ("M", "cycle mode"),
    ("L", "data feed"),
    ("C", "sys.config"),
    ("S", "density"),
    ("F", "freeze"),
]

# Approval action shown only when awaiting approval.
_KEYBAR_APPROVE = ("A", "AUTHORIZE")

# Help text lines shown in the help overlay.
_HELP_LINES: list[tuple[str, str]] = [
    ("Q",     "jack out  (confirm if run active)"),
    ("R",     "reboot current run"),
    ("M",     "cycle pipeline mode"),
    ("← / →", "cycle pane focus"),
    ("L",     "toggle data-feed view"),
    ("C",     "toggle sys.config view"),
    ("S",     "toggle compact deck density"),
    ("?",     "toggle help.sys overlay"),
    ("F",     "freeze / unfreeze focused live pane"),
    ("↑ / ↓", "scroll focused pane"),
    ("A",     "AUTHORIZE  (awaiting approval only)"),
    ("Esc",   "abort jack-out"),
]


@dataclass
class _StageEntry:
    stage_id: str
    stage_name: str
    index: int
    status: str = "queued"  # queued | running | done | failed
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class _TaskEntry:
    task_id: str
    description: str
    agent: str
    status: str = "running"   # running | done | failed
    duration_ms: int | None = None
    error: str | None = None


class StageTimingLiveTable:
    """Maintains per-stage timing data and renders a Rich table."""

    _STATUS_STYLE: dict[str, tuple[str, str]] = {
        "success": ("●", _C_NEON_CYAN),
        "failed":  ("✖", _C_NEON_RED),
        "skipped": ("➜", _C_DIM_CYAN),
    }

    def __init__(self, title: str = "[bold #ff2d6f]// STAGE.TIMINGS[/bold #ff2d6f]") -> None:
        self._title = title
        self._rows: dict[str, tuple[str, StageTiming]] = {}

    def upsert(self, stage_id: str, timing: StageTiming, stage_name: str | None = None) -> None:
        """Insert or update timing data for *stage_id*."""
        current_name = self._rows.get(stage_id, (stage_id, timing))[0]
        name = stage_name or current_name or stage_id
        self._rows[stage_id] = (name, timing)

    def get(self, stage_id: str) -> tuple[str, StageTiming] | None:
        """Return the current (name, timing) tuple for *stage_id*, if any."""
        return self._rows.get(stage_id)

    def render(
        self,
        *,
        border_style: str = _C_BORDER,
        title: str | None = None,
        scroll_offset: int = 0,
        max_rows: int | None = None,
    ) -> Panel:
        """Return a Panel containing the live timing table."""
        table = Table.grid(padding=(0, 1))
        table.add_column("Stage", style=_C_NEON_PINK, no_wrap=True)
        table.add_column("Window", style=_C_DIM_CYAN, no_wrap=False)
        table.add_column("Duration", style=_C_NEON_CYAN, justify="right", no_wrap=True)
        table.add_column("Status", style=_C_NEON_CYAN, no_wrap=True)

        if not self._rows:
            table.add_row(
                Text("–", style=_C_DIM_CYAN),
                Text("no timings recorded", style=_C_DIM_CYAN),
                Text("–", style=_C_DIM_CYAN),
                Text("–", style=_C_DIM_CYAN),
            )
        else:
            sorted_rows = sorted(self._rows.items(), key=lambda item: item[1][1].started_at)
            if max_rows is not None:
                offset = max(0, min(scroll_offset, max(0, len(sorted_rows) - 1)))
                if offset == 0:
                    sorted_rows = sorted_rows[-max_rows:]
                else:
                    end = max(0, len(sorted_rows) - offset)
                    start = max(0, end - max_rows)
                    sorted_rows = sorted_rows[start:end]
            for stage_id, (name, timing) in sorted_rows:
                symbol, color = self._STATUS_STYLE.get(timing.status, ("●", _C_NEON_CYAN))
                duration = f"{timing.duration_ms / 1000:.1f}s" if timing.duration_ms is not None else "–"
                window = self._format_range(timing.started_at, timing.ended_at)
                table.add_row(
                    Text(f"{symbol} {name}", style=color),
                    Text(window, style=_C_DIM_CYAN),
                    Text(duration, style=_C_NEON_CYAN),
                    Text(timing.status or "unknown", style=color),
                )

        return Panel(table, title=title or self._title, border_style=border_style, padding=(0, 1))

    @staticmethod
    def _format_range(start: str, end: str) -> str:
        if start and end:
            return f"{_short_ts(start)} -> {_short_ts(end)}"
        if start and not end:
            return f"{_short_ts(start)} -> …"
        if end and not start:
            return f"… -> {_short_ts(end)}"
        return "–"


@dataclass
class _RendererState:
    run_id: str | None = None
    run_status: str = "idle"
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    mode: str = "rich"
    stages: list[_StageEntry] = field(default_factory=list)
    tasks: list[_TaskEntry] = field(default_factory=list)   # live task rows
    logs: Deque[LogLine] = field(default_factory=lambda: deque(maxlen=_MAX_LOG_LINES))
    agent_output: Deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_LOG_LINES))
    agent_name: str = ""
    awaiting_approval: bool = False
    # Keyboard-driven UI state
    view_mode: str = "normal"   # normal | logs | config | help
    focused_panel: str = "none"  # none | deck | data | stage | agent
    deck_scroll_offset: int = 0
    data_scroll_offset: int = 0
    data_autoscroll: bool = True
    stage_scroll_offset: int = 0
    agent_scroll_offset: int = 0
    agent_autoscroll: bool = True
    compact: bool = True        # compact stage list density
    quit_confirm: bool = False  # True while awaiting quit confirmation
    # In-TUI input state (set when pipeline needs user input)
    input_prompt: str | None = None
    input_buffer: str = ""


# -----------------------------------------------------------------------------
# Keyboard poller (background thread)
# -----------------------------------------------------------------------------

class _KeyboardPoller:
    """Reads keypresses in a daemon thread and calls *callback* with a key name."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _run(self) -> None:
        if sys.platform == "win32":
            self._run_windows()
        else:
            self._run_unix()

    def _run_windows(self) -> None:
        try:
            import msvcrt
        except ImportError:
            return
        while not self._stopped.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):
                    # Extended key: read the scan code.
                    ch2 = msvcrt.getch()
                    if ch2 == b"H":
                        self._callback("up")
                    elif ch2 == b"P":
                        self._callback("down")
                    elif ch2 == b"K":
                        self._callback("left")
                    elif ch2 == b"M":
                        self._callback("right")
                else:
                    try:
                        self._callback(ch.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
            self._stopped.wait(0.05)

    def _run_unix(self) -> None:
        try:
            import tty
            import termios
        except ImportError:
            return
        fd = sys.stdin.fileno()
        try:
            old_settings = termios.tcgetattr(fd)
        except termios.error:
            return
        try:
            tty.setraw(fd)
            while not self._stopped.is_set():
                ch = sys.stdin.read(1)
                if not ch:
                    break
                if ch == "\x1b":
                    # Possible ANSI escape sequence for arrow keys.
                    next1 = sys.stdin.read(1)
                    if next1 == "[":
                        next2 = sys.stdin.read(1)
                        if next2 == "A":
                            self._callback("up")
                        elif next2 == "B":
                            self._callback("down")
                        elif next2 == "D":
                            self._callback("left")
                        elif next2 == "C":
                            self._callback("right")
                    else:
                        # Plain ESC (or unrecognised sequence).
                        self._callback("\x1b")
                else:
                    self._callback(ch)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass


# -----------------------------------------------------------------------------
# Renderer
# -----------------------------------------------------------------------------

class RichLiveRenderer:
    """Rich Live TUI renderer.

    Usage::

        renderer = RichLiveRenderer()
        emitter.subscribe(renderer.handle)
        renderer.start()
        ...
        renderer.stop(result)

    Optional callbacks let callers act on key-triggered actions::

        renderer = RichLiveRenderer(
            on_quit=lambda: pipeline.cancel(),
            on_restart=lambda: pipeline.restart(),
            on_approve=lambda: pipeline.approve(),
        )
    """

    def __init__(
        self,
        console: Console | None = None,
        refresh_per_second: int = 4,
        enable_keyboard: bool = True,
        version: str = "0.1.8",
        on_quit: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
        on_approve: Callable[[], None] | None = None,
    ) -> None:
        self._console = console or Console()
        self._refresh_per_second = refresh_per_second
        self._enable_keyboard = enable_keyboard
        self._version = version
        self._on_quit = on_quit
        self._on_restart = on_restart
        self._on_approve = on_approve
        self._state = _RendererState()
        self._timing_table = StageTimingLiveTable()
        self._live: Live | None = None
        self._kbd: _KeyboardPoller | None = None
        self._lock = threading.Lock()
        self._mode_details: dict[str, str] = {
            "key": "default",
            "label": "Default",
            "summary": "Gemini -> Codex -> Chief -> Claude",
        }
        self._cycle_mode_cb: Callable[[], dict[str, str]] | None = None
        # Input gate: set() when the user submits input inside the TUI
        self._input_ready = threading.Event()
        self._input_result: str = ""

    def configure_mode_controls(
        self,
        mode_details: dict[str, str],
        cycle_callback: Callable[[], dict[str, str]] | None,
    ) -> None:
        """Configure mode metadata and callback used by the `M` hotkey."""
        with self._lock:
            self._mode_details = dict(mode_details)
            self._cycle_mode_cb = cycle_callback
            if self._live is not None:
                self._live.update(self._render_root())

    # -----------------------------------------------------------------------------
    # Public renderer interface
    # -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start the Rich Live display and keyboard poller."""
        self._state = _RendererState()
        self._timing_table = StageTimingLiveTable()
        self._input_ready.clear()
        self._input_result = ""
        self._live = Live(
            self._render_root(),
            console=self._console,
            refresh_per_second=self._refresh_per_second,
            screen=True,
        )
        self._live.start()
        if self._enable_keyboard:
            self._kbd = _KeyboardPoller(self._handle_key)
            self._kbd.start()

    def request_input(self, prompt: str) -> str:
        """Show an input bar in the TUI and block until the user presses Enter.

        This replaces the old suspend/input()/resume flow so that all user
        interaction stays inside the Live display.  The keyboard poller feeds
        keystrokes into the input buffer; Enter submits and unblocks this call.
        """
        self._input_ready.clear()
        self._input_result = ""
        with self._lock:
            self._state.input_prompt = prompt
            self._state.input_buffer = ""
            if self._live is not None:
                self._live.update(self._render_root())
        self._input_ready.wait()
        return self._input_result

    def suspend(self) -> None:
        """Temporarily stop Live rendering and keyboard capture."""
        if self._kbd is not None:
            self._kbd.stop()
            self._kbd = None
        if self._live is not None:
            self._live.stop()
            self._live = None

    def resume(self) -> None:
        """Restart Live rendering and keyboard capture after suspend()."""
        if self._live is None:
            self._live = Live(
                self._render_root(),
                console=self._console,
                refresh_per_second=self._refresh_per_second,
                screen=True,
            )
            self._live.start()
        if self._enable_keyboard and self._kbd is None:
            self._kbd = _KeyboardPoller(self._handle_key)
            self._kbd.start()

    def handle(self, event: AnyEvent) -> None:
        """Process a typed pipeline event and refresh the display."""
        with self._lock:
            self._apply_event(event)
            if self._live is not None:
                self._live.update(self._render_root())

    def stop(self, result: RunStatus | None = None) -> None:
        """Stop the Rich Live display and keyboard poller."""
        if self._kbd is not None:
            self._kbd.stop()
            self._kbd = None
        with self._lock:
            if result is not None and self._state.run_status not in ("done", "failed", "interrupted"):
                self._state.run_status = result.value
            self._state.ended_at = time.monotonic()
            if self._live is not None:
                self._live.update(self._render_root())
                self._live.stop()
                self._live = None

    # -----------------------------------------------------------------------------
    # Keyboard state
    # -----------------------------------------------------------------------------

    def _handle_key(self, key: str) -> None:
        with self._lock:
            self._dispatch_key(key)
            if self._live is not None:
                self._live.update(self._render_root())

    def _render_root(self):
        """Render the live layout with a fixed 4-line top padding."""
        return Padding(self._build_layout(), (1, 1, 1, 1))

    def _dispatch_key(self, key: str) -> None:
        state = self._state
        key_norm = key.lower() if len(key) == 1 else key

        # -"?-"? In-TUI input mode -"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?
        if state.input_prompt is not None:
            if key in ("\r", "\n"):
                # Submit: capture result and signal the waiting thread.
                self._input_result = state.input_buffer
                state.input_prompt = None
                state.input_buffer = ""
                self._input_ready.set()
            elif key in ("\x7f", "\x08"):  # Backspace / DEL
                state.input_buffer = state.input_buffer[:-1]
            elif key == "\x1b":            # Escape -> clear buffer
                state.input_buffer = ""
            elif len(key) == 1 and key.isprintable():
                state.input_buffer += key
            return  # absorb all keys while input panel is open

        # Quit confirmation flow.
        if state.quit_confirm:
            if key in ("\r", "\n", "y", "Y"):
                state.quit_confirm = False
                self._trigger_quit()
                return
            elif key == "\x1b" or key in ("n", "N"):
                state.quit_confirm = False
                return
            # Any other key while confirming: cancel confirmation.
            state.quit_confirm = False
            return

        if key_norm == "q":
            active = state.run_status in ("running", "waiting_approval")
            if active:
                state.quit_confirm = True
            else:
                self._trigger_quit()

        elif key_norm == "r":
            if self._on_restart is not None:
                self._on_restart()

        elif key_norm == "m":
            if self._cycle_mode_cb is not None:
                try:
                    details = self._cycle_mode_cb()
                except Exception:
                    details = None
                if isinstance(details, dict):
                    self._mode_details = dict(details)

        elif key in ("left", "right"):
            if state.view_mode == "normal":
                current_idx = self._FOCUS_ORDER.index(state.focused_panel)
                step = -1 if key == "left" else 1
                state.focused_panel = self._FOCUS_ORDER[(current_idx + step) % len(self._FOCUS_ORDER)]

        elif key_norm == "l":
            state.view_mode = "normal" if state.view_mode == "logs" else "logs"

        elif key_norm == "c":
            state.view_mode = "normal" if state.view_mode == "config" else "config"

        elif key_norm == "s":
            state.compact = not state.compact

        elif key == "?":
            state.view_mode = "normal" if state.view_mode == "help" else "help"

        elif key_norm == "f":
            if state.focused_panel == "data":
                state.data_autoscroll = not state.data_autoscroll
                if state.data_autoscroll:
                    state.data_scroll_offset = 0
            elif state.focused_panel == "agent":
                state.agent_autoscroll = not state.agent_autoscroll
                if state.agent_autoscroll:
                    state.agent_scroll_offset = 0

        elif key == "up":
            self._scroll_panel(state.focused_panel, 1)

        elif key == "down":
            self._scroll_panel(state.focused_panel, -1)

        elif key_norm == "a" and state.awaiting_approval:
            if self._on_approve is not None:
                self._on_approve()

    def _trigger_quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()

    def _scroll_panel(self, panel: str, delta: int) -> None:
        state = self._state
        if panel == "deck":
            state.deck_scroll_offset = max(0, state.deck_scroll_offset + delta)
            return
        if panel == "stage":
            state.stage_scroll_offset = max(0, state.stage_scroll_offset + delta)
            return
        if panel == "data":
            if delta > 0:
                state.data_autoscroll = False
                state.data_scroll_offset += delta
            else:
                state.data_scroll_offset = max(0, state.data_scroll_offset + delta)
                if state.data_scroll_offset == 0:
                    state.data_autoscroll = True
            return
        if panel == "agent":
            if delta > 0:
                state.agent_autoscroll = False
                state.agent_scroll_offset += delta
            else:
                state.agent_scroll_offset = max(0, state.agent_scroll_offset + delta)
                if state.agent_scroll_offset == 0:
                    state.agent_autoscroll = True

    def _is_focused(self, panel: str) -> bool:
        return self._state.view_mode == "normal" and self._state.focused_panel == panel

    def _panel_border_style(self, panel: str) -> str:
        return _C_NEON_CYAN if self._is_focused(panel) else _C_BORDER

    def _panel_title(self, panel: str, title: str) -> str:
        marker = "▶ " if self._is_focused(panel) else ""
        return f"[bold #ff2d6f]{marker}{title}[/bold #ff2d6f]"

    @staticmethod
    def _window_slice(items: list, scroll_offset: int, max_rows: int) -> list:
        if not items:
            return items
        offset = max(0, min(scroll_offset, max(0, len(items) - 1)))
        if offset == 0:
            return items[-max_rows:]
        end = max(0, len(items) - offset)
        start = max(0, end - max_rows)
        return items[start:end]

    # -----------------------------------------------------------------------------
    # Event to state
    # -----------------------------------------------------------------------------

    def _apply_event(self, event: AnyEvent) -> None:
        state = self._state

        if isinstance(event, RunStarted):
            state.run_id = event.run_id
            state.run_status = "running"
            state.mode = event.mode
            state.started_at = time.monotonic()
            state.awaiting_approval = False
            state.deck_scroll_offset = 0
            state.data_scroll_offset = 0
            state.data_autoscroll = True
            state.stage_scroll_offset = 0
            state.agent_scroll_offset = 0
            state.agent_autoscroll = True
            state.agent_output.clear()
            state.agent_name = ""
            self._timing_table = StageTimingLiveTable()

        elif isinstance(event, StageQueued):
            state.stages.append(
                _StageEntry(
                    stage_id=event.stage_id,
                    stage_name=event.stage_name,
                    index=event.index,
                )
            )

        elif isinstance(event, StageStarted):
            entry = self._find_stage(event.stage_id)
            if entry is not None:
                entry.status = "running"
            # Match legacy behaviour: clear agent output at stage boundaries.
            state.agent_output.clear()
            state.agent_name = ""
            stage_name = event.stage_name or (entry.stage_name if entry is not None else event.stage_id)
            timing = StageTiming(
                stage_name=stage_name,
                started_at=event.started_at,
                ended_at="",
                duration_ms=None,  # updated on completion
                status="running",
            )
            self._timing_table.upsert(event.stage_id, timing, stage_name=stage_name)

        elif isinstance(event, StageCompleted):
            entry = self._find_stage(event.stage_id)
            if entry is not None:
                entry.status = "done"
                entry.duration_ms = event.duration_ms
            existing = self._timing_table.get(event.stage_id)
            started_at = existing[1].started_at if existing else ""
            stage_name = event.stage_name or (existing[0] if existing else (entry.stage_name if entry else event.stage_id))
            timing = StageTiming(
                stage_name=stage_name,
                started_at=started_at,
                ended_at=event.ended_at,
                duration_ms=event.duration_ms,
                status=event.status or "success",
            )
            self._timing_table.upsert(event.stage_id, timing, stage_name=stage_name)

        elif isinstance(event, StageFailed):
            entry = self._find_stage(event.stage_id)
            if entry is not None:
                entry.status = "failed"
                entry.error = event.error
            existing = self._timing_table.get(event.stage_id)
            started_at = existing[1].started_at if existing else ""
            stage_name = event.stage_name or (existing[0] if existing else (entry.stage_name if entry else event.stage_id))
            timing = StageTiming(
                stage_name=stage_name,
                started_at=started_at,
                ended_at=getattr(event, "ended_at", "") if hasattr(event, "ended_at") else "",
                duration_ms=existing[1].duration_ms if existing else None,
                status="failed",
            )
            self._timing_table.upsert(event.stage_id, timing, stage_name=stage_name)
            state.run_status = "failed"

        elif isinstance(event, AwaitingApproval):
            state.run_status = "waiting_approval"
            state.awaiting_approval = True

        elif isinstance(event, ApprovalReceived):
            state.run_status = "running"
            state.awaiting_approval = False

        elif isinstance(event, LogLine):
            state.logs.append(event)
            if state.data_autoscroll:
                state.data_scroll_offset = 0

        elif isinstance(event, AgentOutput):
            if event.agent and event.agent != state.agent_name:
                state.agent_name = event.agent
            state.agent_output.append(event.line)
            if state.agent_autoscroll:
                state.agent_scroll_offset = 0

        elif isinstance(event, TaskStarted):
            state.tasks.append(
                _TaskEntry(
                    task_id=event.task_id,
                    description=event.description,
                    agent=event.agent,
                )
            )

        elif isinstance(event, TaskCompleted):
            entry = self._find_task(event.task_id)
            if entry is not None:
                entry.status = "done"
                entry.duration_ms = event.duration_ms

        elif isinstance(event, TaskFailed):
            entry = self._find_task(event.task_id)
            if entry is not None:
                entry.status = "failed"
                entry.error = event.error

        elif isinstance(event, RunCompleted):
            state.run_status = event.status.value
            state.awaiting_approval = False
            state.ended_at = time.monotonic()

    def _find_stage(self, stage_id: str) -> _StageEntry | None:
        for entry in self._state.stages:
            if entry.stage_id == stage_id:
                return entry
        return None

    def _find_task(self, task_id: str) -> _TaskEntry | None:
        for entry in self._state.tasks:
            if entry.task_id == task_id:
                return entry
        return None

    # -----------------------------------------------------------------------------
    # Layout builders
    # -----------------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        state = self._state
        has_input = state.input_prompt is not None
        keybar = self._build_keybar()

        if state.view_mode == "help":
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(self._build_help_panel(), name="help", ratio=1),
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=4))
            layout = Layout()
            layout.split_column(*cols)
            return layout

        if state.view_mode == "logs":
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(self._build_stream_panel(), name="stream", ratio=1),
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=4))
            layout = Layout()
            layout.split_column(*cols)
            return layout

        if state.view_mode == "config":
            config_layout = Layout(name="config", ratio=1)
            config_layout.split_column(
                Layout(self._build_config_panel(), name="config.info", ratio=1),
                Layout(self._build_timing_panel(), name="config.timings", ratio=1),
            )
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                config_layout,
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=4))
            layout = Layout()
            layout.split_column(*cols)
            return layout

        # Default: DECK on the left, DATA FEED + AGENT OUTPUT on the right.
        base_body_height = max(
            8,
            self._console.size.height - 3 - 4 - (5 if has_input else 0),
        )
        body_size = max(8, base_body_height - 2)
        cols = [
            Layout(self._build_status_bar(), name="status", size=3),
            Layout(name="body", size=body_size),
        ]
        if has_input:
            cols.append(Layout(self._build_input_panel(), name="input", size=5))
        cols.append(Layout(keybar, name="keybar", size=4))

        layout = Layout()
        layout.split_column(*cols)
        # Keep STAGE/AGENT sizes; shrink only DECK/DATA by 4 lines.
        estimated_body_height = base_body_height
        timing_size = max(6, (estimated_body_height * 4) // 7 - 1)
        agent_size = max(5, (estimated_body_height // 2) - 3)
        pipeline_size = max(5, body_size - timing_size)
        stream_size = max(5, body_size - agent_size)
        deck = Layout(name="deck", ratio=1)
        deck.split_column(
            Layout(self._build_pipeline_panel(), name="pipeline", size=pipeline_size),
            Layout(self._build_timing_panel(), name="timing", size=timing_size),
        )
        right = Layout(name="right", ratio=2)
        right.split_column(
            Layout(self._build_stream_panel(), name="stream", size=stream_size),
            Layout(self._build_agent_panel(), name="agent", size=agent_size),
        )
        layout["body"].split_row(
            deck,
            Layout(Text(" "), name="deck_right_gap", size=1),
            right,
        )
        return layout

    def _build_status_bar(self) -> Panel:
        state = self._state
        elapsed = self._elapsed_str()
        run_color = _C_NEON_CYAN

        bar = Text()

        if state.quit_confirm:
            bar.append("  ⚠  JACK OUT? ", style=_C_NEON_CYAN)
            bar.append(" Y ", style="bold #2EDCD1")
            bar.append(" confirm  ", style=_C_NEON_CYAN)
            bar.append(" ESC ", style="bold #2EDCD1")
            bar.append(" abort", style=_C_NEON_CYAN)
            return Panel(bar, border_style=_C_BORDER, padding=(0, 1))

        bar.append("  ▸ A.I.N.", style=_C_NEON_PINK)
        bar.append(f" v{self._version}", style=_C_DIM_CYAN)
        bar.append("  ║  ", style=_C_NEON_PINK)
        bar.append("SYS:", style=_C_NEON_PINK)
        bar.append(f" {state.run_status.upper().replace('_', '.')}", style=run_color)
        bar.append("  ║  ", style=_C_NEON_PINK)
        bar.append("UPTIME:", style=_C_NEON_PINK)
        bar.append(f" {elapsed}", style=_C_NEON_CYAN)
        bar.append("  ║  ", style=_C_NEON_PINK)
        bar.append("MODE:", style=_C_NEON_PINK)
        bar.append(f" {self._mode_details.get('key', 'default')}", style=_C_NEON_CYAN)

        if state.run_id:
            bar.append("  ║  ", style=_C_NEON_PINK)
            bar.append("NODE:", style=_C_NEON_PINK)
            bar.append(f" {state.run_id[:8]}", style=_C_NEON_CYAN)

        if not state.data_autoscroll:
            bar.append("  ║  ", style=_C_NEON_PINK)
            bar.append("⏸ DATA FROZEN", style=_C_NEON_PINK)
        if not state.agent_autoscroll:
            bar.append("  ║  ", style=_C_NEON_PINK)
            bar.append("⏸ AGENT FROZEN", style=_C_NEON_PINK)

        return Panel(bar, border_style=_C_BORDER, padding=(0, 1))

    # Task status -> (symbol, Rich color)
    _TASK_STYLE: dict[str, tuple[str, str]] = {
        "running": ("▷", "#2EDCD1"),
        "done":    ("◆", _C_NEON_CYAN),
        "failed":  ("✖", _C_NEON_CYAN),
    }
    _FOCUS_ORDER: tuple[str, ...] = ("none", "deck", "data", "stage", "agent")

    def _build_pipeline_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(justify="right", no_wrap=True)
        table.add_column(justify="left", no_wrap=False)
        rows: list[tuple[Text, Text]] = []

        if not self._state.stages:
            rows.append(
                (
                    Text("◈", style=_C_DIM_CYAN),
                    Text("awaiting deck init...", style=_C_DIM_CYAN),
                )
            )
        else:
            active_tasks = self._state.tasks
            for entry in sorted(self._state.stages, key=lambda e: e.index):
                symbol, color = _STAGE_STYLE.get(entry.status, ("?", "#2EDCD1"))
                name_text = Text(entry.stage_name, style=color)
                if not self._state.compact and entry.duration_ms is not None:
                    name_text.append(
                        f"  {entry.duration_ms / 1000:.1f}s",
                        style=_C_DIM_CYAN,
                    )
                if not self._state.compact and entry.error:
                    name_text.append(f"\n  ERR: {entry.error}", style=_C_NEON_CYAN)
                rows.append((Text(symbol, style=color), name_text))

                if entry.status == "running" and active_tasks and not self._state.compact:
                    for task in active_tasks:
                        t_sym, t_color = self._TASK_STYLE.get(task.status, ("▷", "#2EDCD1"))
                        desc = task.description
                        if len(desc) > 38:
                            desc = desc[:35] + "…"
                        task_text = Text()
                        task_text.append(desc, style=t_color)
                        task_text.append(f"  /{task.agent}/", style=_C_DIM_CYAN)
                        if task.duration_ms is not None:
                            task_text.append(f"  {task.duration_ms / 1000:.1f}s", style=_C_DIM_CYAN)
                        if task.error:
                            task_text.append(f"  {task.error}", style=_C_NEON_CYAN)
                        rows.append((Text(f"  {t_sym}", style=t_color), task_text))

        for icon, content in self._window_slice(rows, self._state.deck_scroll_offset, self._panel_lines()):
            table.add_row(icon, content)

        title = self._panel_title("deck", "// DECK")
        return Panel(table, title=title, border_style=self._panel_border_style("deck"), padding=(0, 1))

    def _build_timing_panel(self) -> Panel:
        return self._timing_table.render(
            border_style=self._panel_border_style("stage"),
            title=self._panel_title("stage", "// STAGE.TIMINGS"),
            scroll_offset=self._state.stage_scroll_offset,
            max_rows=self._panel_lines(),
        )

    def _build_stream_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True, style=_C_NEON_PINK)  # timestamp
        table.add_column(no_wrap=True)                       # level tag
        table.add_column(no_wrap=False)                      # message

        logs = list(self._state.logs)

        visible = self._window_slice(logs, self._state.data_scroll_offset, self._panel_lines())

        for log in visible:
            ts_str = log.ts[11:19] if len(log.ts) >= 19 else log.ts
            level_color = _LEVEL_COLOR.get(log.level, "#2EDCD1")
            label = Text()
            label.append(f"[{log.level.value.upper()[:3]}]", style=level_color)
            table.add_row(
                Text(ts_str, style=_C_NEON_PINK),
                label,
                Text(log.message, style="#2EDCD1"),
            )

        if not logs:
            table.add_row(
                "",
                Text(""),
                Text("// awaiting data transmission...", style=_C_NEON_CYAN),
            )

        title = self._panel_title("data", "// DATA FEED")
        if not self._state.data_autoscroll:
            offset = self._state.data_scroll_offset
            title += f" [bold #ff2d6f]⏸ +{offset}[/bold #ff2d6f]"
        return Panel(table, title=title, border_style=self._panel_border_style("data"), padding=(0, 1))

    def _panel_lines(self) -> int:
        """Approximate content lines for half-height right-column panels."""
        h = self._console.size.height
        return max(5, (h - 14) // 2 - 2)

    def _build_agent_panel(self) -> Panel:
        title = "// AGENT.OUTPUT"
        if self._state.agent_name:
            title = f"{title} — {self._state.agent_name}"

        lines = list(self._state.agent_output)
        if not lines:
            body: Text | str = Text("No agent running.", style=f"dim {_C_PRIMARY_TEXT}")
        else:
            visible = self._window_slice(lines, self._state.agent_scroll_offset, self._panel_lines())
            body = Text()
            for idx, line in enumerate(visible):
                if idx:
                    body.append("\n")
                body.append(line, style=_C_PRIMARY_TEXT)

        return Panel(
            body,
            title=self._panel_title("agent", title),
            border_style=self._panel_border_style("agent"),
            padding=(0, 1),
        )

    def _build_config_panel(self) -> Panel:
        text = Text()
        text.append("// SYS.CONFIG\n\n", style=_C_NEON_PINK)
        text.append("Press ", style=_C_DIM_CYAN)
        text.append(" C ", style="bold #2EDCD1")
        text.append(" to return to main deck.", style=_C_DIM_CYAN)
        return Panel(
            text,
            title="[bold #ff2d6f]// SYS.CONFIG[/bold #ff2d6f]",
            border_style=_C_BORDER,
            padding=(0, 1),
        )

    def _build_help_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(justify="right", no_wrap=True, style="bold #ff2d6f")
        table.add_column(justify="left", no_wrap=False, style="#2EDCD1")

        for key, desc in _HELP_LINES:
            table.add_row(f" {key} ", desc)

        return Panel(
            table,
            title="[bold #ff2d6f]// HELP.SYS[/bold #ff2d6f]",
            border_style=_C_BORDER,
            padding=(0, 1),
        )

    def _build_keybar(self) -> Panel:
        state = self._state
        entries = list(_KEYBAR_ENTRIES)

        entries = [
            (k, "unfreeze" if k == "F" and self._focused_panel_frozen() else d)
            for k, d in entries
        ]

        if state.awaiting_approval:
            entries.append(_KEYBAR_APPROVE)

        bar = Text()
        for i, (key, desc) in enumerate(entries):
            if i > 0:
                bar.append(" ", style="")
            bar.append(f" {key} ", style="bold #ff2d6f")
            bar.append(f" {desc}", style=_C_DIM_CYAN)

        bar.append("\n")
        bar.append(" FOCUS ", style="bold #ff2d6f")
        bar.append(state.focused_panel.upper(), style=f"bold {_C_PRIMARY_TEXT}")
        bar.append("  |  ", style="#ff2d6f")
        bar.append(self._focus_hint(), style=_C_SECONDARY_TEXT)
        bar.append("  ", style="")
        bar.append(" ? ", style="bold #ff2d6f")
        bar.append(" help.sys", style=_C_DIM_CYAN)
        bar.append("  ", style="")
        bar.append(" ←/→ ", style="bold #ff2d6f")
        bar.append(" focus pane", style=_C_DIM_CYAN)
        bar.append("  ", style="")
        bar.append(" ↑/↓ ", style="bold #ff2d6f")
        bar.append(" scroll", style=_C_DIM_CYAN)
        bar.append("\n")
        bar.append(" MODE ", style="bold #ff2d6f")
        bar.append(self._mode_details.get("key", "default"), style=f"bold {_C_PRIMARY_TEXT}")
        bar.append("  |  ", style="#ff2d6f")
        bar.append(self._mode_details.get("label", ""), style=_C_SECONDARY_TEXT)
        bar.append("  |  ", style="#ff2d6f")
        bar.append("FLOW ", style="bold #ff2d6f")
        bar.append(self._mode_details.get("summary", ""), style=_C_SECONDARY_TEXT)
        bar.append("\n")
        bar.append("─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────", style="#ff2d6f")

        return Panel(bar, border_style=_C_BORDER, padding=(0, 1))

    def _focused_panel_frozen(self) -> bool:
        state = self._state
        if state.focused_panel == "data":
            return not state.data_autoscroll
        if state.focused_panel == "agent":
            return not state.agent_autoscroll
        return False

    def _focus_hint(self) -> str:
        state = self._state
        if state.focused_panel == "deck":
            return f"rows +{state.deck_scroll_offset}"
        if state.focused_panel == "stage":
            return f"rows +{state.stage_scroll_offset}"
        if state.focused_panel == "data":
            return "live" if state.data_autoscroll else f"rows +{state.data_scroll_offset}"
        if state.focused_panel == "agent":
            return "live" if state.agent_autoscroll else f"rows +{state.agent_scroll_offset}"
        return ""

    def _build_input_panel(self) -> Panel:
        """In-TUI input bar - replaces keybar when pipeline needs user input."""
        state = self._state
        text = Text()
        text.append("▸ ", style=_C_NEON_CYAN)
        text.append(state.input_prompt or "", style=_C_NEON_CYAN)
        text.append("\n\n  ❯ ", style=_C_NEON_CYAN)
        text.append(state.input_buffer, style="bold #2EDCD1")
        text.append("█", style=_C_NEON_CYAN)
        return Panel(
            text,
            title="[bold #ff2d6f]// INPUT.AWAITING[/bold #ff2d6f]",
            border_style=_C_BORDER,
            padding=(0, 1),
        )

    # -----------------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------------

    def _elapsed_str(self) -> str:
        end = self._state.ended_at or time.monotonic()
        elapsed = int(end - self._state.started_at)
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"


def _short_ts(ts: str) -> str:
    """Return a compact time component from an ISO timestamp string."""
    return ts[11:19] if len(ts) >= 19 else ts


