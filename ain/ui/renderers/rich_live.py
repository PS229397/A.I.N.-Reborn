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

import json
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ain.runtime.emitter import Emitter
from ain.runtime.events import (
    AnyEvent,
    AgentOutput,
    ApprovalReceived,
    AwaitingApproval,
    CancelMultilineInputEvent,
    LogLevel,
    LogLine,
    OpenMultilineInputEvent,
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
    SubmitMultilineInputEvent,
)
from ain.ui.views.approval_view import ApprovalView
from ain.ui.views.mode_select_view import ModeSelectView
from ain.ui.views.multiline_input_view import MultilineInputView

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
_TASK_GRAPH_FILE = Path.cwd() / "docs" / "TASK_GRAPH.json"

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
    ("D", "data feed"),
    ("A", "agent view"),
    ("T", "tasks"),
    ("F", "freeze"),
]

# Approval action shown only when awaiting approval.
_KEYBAR_APPROVE = None

# Help text lines shown in the help overlay.
_HELP_LINES: list[tuple[str, str]] = [
    ("Q",     "jack out  (confirm if run active)"),
    ("R",     "reboot current run"),
    ("M",     "cycle pipeline mode"),
    ("← / →", "cycle panel focus"),
    ("D",     "toggle data-feed view"),
    ("A",     "toggle agent view (when not approving)"),
    ("T",     "toggle task list view"),
    ("?",     "toggle help.sys overlay"),
    ("F",     "freeze / unfreeze focused live pane"),
    ("↑ / ↓", "scroll focused pane"),
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
    agent: str = ""
    status: str = "pending"   # pending | running | done | failed
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class _KeyPress:
    key: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False


@dataclass
class _RendererState:
    run_id: str | None = None
    run_status: str = "idle"
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    mode: str = "rich"
    stages: list[_StageEntry] = field(default_factory=list)
    tasks: list[_TaskEntry] = field(default_factory=list)   # task graph + live updates
    logs: Deque[LogLine] = field(default_factory=lambda: deque(maxlen=_MAX_LOG_LINES))
    agent_output: Deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_LOG_LINES))
    agent_name: str = ""
    awaiting_approval: bool = False
    # Keyboard-driven UI state
    view_mode: str = "normal"   # normal | logs | help | agent | tasks
    focused_panel: str = "none"  # none | deck | data | stage | agent
    deck_scroll_offset: int = 0
    data_scroll_offset: int = 0
    data_autoscroll: bool = True
    stage_scroll_offset: int = 0
    stage_autoscroll: bool = True
    agent_scroll_offset: int = 0
    agent_autoscroll: bool = True
    compact: bool = True        # compact stage list density
    quit_confirm: bool = False  # True while awaiting quit confirmation
    # In-TUI input state (set when pipeline needs user input)
    input_prompt: str | None = None
    input_buffer: str = ""
    # Fullscreen multiline input overlay
    multiline_view: MultilineInputView | None = None
    # Fullscreen mode-selection overlay
    mode_select_view: ModeSelectView | None = None
    # Fullscreen task-approval overlay
    approval_view: ApprovalView | None = None


# -----------------------------------------------------------------------------
# Keyboard poller (background thread)
# -----------------------------------------------------------------------------

class _KeyboardPoller:
    """Reads keypresses in a daemon thread and calls *callback* with a key event."""

    def __init__(self, callback: Callable[[_KeyPress], None]) -> None:
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
                ctrl, alt, shift = self._modifiers_windows()
                if ch in (b"\x00", b"\xe0"):
                    # Extended key: read the scan code.
                    ch2 = msvcrt.getch()
                    if ch2 == b"H":
                        self._callback(_KeyPress("up", ctrl=ctrl, alt=alt, shift=shift))
                    elif ch2 == b"P":
                        self._callback(_KeyPress("down", ctrl=ctrl, alt=alt, shift=shift))
                    elif ch2 == b"K":
                        self._callback(_KeyPress("left", ctrl=ctrl, alt=alt, shift=shift))
                    elif ch2 == b"M":
                        self._callback(_KeyPress("right", ctrl=ctrl, alt=alt, shift=shift))
                else:
                    try:
                        decoded = ch.decode("utf-8", errors="ignore")
                        self._callback(_KeyPress(decoded, ctrl=ctrl, alt=alt, shift=shift))
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
                            self._callback(_KeyPress("up"))
                        elif next2 == "B":
                            self._callback(_KeyPress("down"))
                        elif next2 == "D":
                            self._callback(_KeyPress("left"))
                        elif next2 == "C":
                            self._callback(_KeyPress("right"))
                    else:
                        # Plain ESC (or unrecognised sequence).
                        self._callback(_KeyPress("\x1b"))
                else:
                    self._callback(_KeyPress(ch))
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass

    @staticmethod
    def _modifiers_windows() -> tuple[bool, bool, bool]:
        try:
            import ctypes
        except ImportError:
            return False, False, False

        user32 = ctypes.windll.user32
        pressed = lambda vk: bool(user32.GetKeyState(vk) & 0x8000)  # noqa: E731
        return pressed(0x11), pressed(0x12), pressed(0x10)  # Ctrl, Alt, Shift


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
        emitter: Emitter | None = None,
        on_quit: Callable[[], None] | None = None,
        on_quit_clean: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
        on_approve: Callable[[], None] | None = None,
    ) -> None:
        self._console = console or Console()
        self._refresh_per_second = refresh_per_second
        self._enable_keyboard = enable_keyboard
        self._version = version
        self._on_quit = on_quit
        self._on_quit_clean = on_quit_clean
        self._on_restart = on_restart
        self._on_approve = on_approve
        self._state = _RendererState()
        self._live: Live | None = None
        self._kbd: _KeyboardPoller | None = None
        self._ticker: threading.Thread | None = None
        # Re-entrant lock avoids deadlock when key handlers emit events that synchronously
        # call back into `handle()` on this renderer (for example approval denial feedback).
        self._lock = threading.RLock()
        self._running = False
        self._mode_details: dict[str, str] = {
            "key": "default",
            "label": "Default",
            "summary": "Gemini -> Codex -> Chief -> Claude",
        }
        self._cycle_mode_cb: Callable[[], dict[str, str]] | None = None
        # Input gate: set() when the user submits input inside the TUI
        self._input_ready = threading.Event()
        self._input_result: str = ""
        # Mode-select gate: set() when a mode is selected/cancelled in the TUI.
        self._mode_select_ready = threading.Event()
        self._mode_select_result: str = ""
        # Approval gate: set() when approval interaction completes in the TUI.
        self._approval_ready = threading.Event()
        self._approval_result: tuple[bool, str] = (False, "")
        self._emitter: Emitter | None = emitter

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

    def attach_emitter(self, emitter: Emitter | None) -> None:
        """Allow the renderer to emit events (submit/cancel) back to the pipeline."""
        self._emitter = emitter

    # -----------------------------------------------------------------------------
    # Public renderer interface
    # -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start the Rich Live display and keyboard poller."""
        self._state = _RendererState()
        self._input_ready.clear()
        self._input_result = ""
        self._running = True
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
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._ticker.start()

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

    def request_mode_selection(self, modes: list[dict[str, str]], current_mode: str) -> str:
        """Show fullscreen in-TUI mode selector and block until a choice is made."""

        if not modes:
            return current_mode
        self._mode_select_ready.clear()
        self._mode_select_result = current_mode
        with self._lock:
            self._state.mode_select_view = ModeSelectView(modes, current_mode=current_mode)
            if self._live is not None:
                self._live.update(self._render_root())
        self._mode_select_ready.wait()
        return self._mode_select_result or current_mode

    def request_task_approval(self, tasks: list[dict[str, str]]) -> tuple[bool, str]:
        """Show fullscreen task approval view and block until approved/denied."""

        self._approval_ready.clear()
        self._approval_result = (False, "")
        with self._lock:
            self._state.approval_view = ApprovalView(tasks, emitter=self._emitter)
            if self._live is not None:
                self._live.update(self._render_root())
        while not self._approval_ready.wait(0.1):
            pass
        return self._approval_result

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
        self._running = False
        if self._kbd is not None:
            self._kbd.stop()
            self._kbd = None
        with self._lock:
            if result is not None and self._state.run_status not in ("done", "failed", "interrupted"):
                self._state.run_status = result.value
            self._state.ended_at = time.monotonic()
            if self._state.mode_select_view is not None and not self._mode_select_ready.is_set():
                self._mode_select_result = self._state.mode_select_view.current_mode
                self._state.mode_select_view = None
                self._mode_select_ready.set()
            if self._state.approval_view is not None and not self._approval_ready.is_set():
                self._approval_result = (False, "")
                self._state.approval_view = None
                self._approval_ready.set()
            if self._live is not None:
                self._live.update(self._render_root())
                self._live.stop()
                self._live = None

    def _tick_loop(self) -> None:
        while self._running:
            time.sleep(1)
            with self._lock:
                if self._live is not None:
                    self._live.update(self._render_root())

    # -----------------------------------------------------------------------------
    # Keyboard state
    # -----------------------------------------------------------------------------

    def _handle_key(self, key: _KeyPress | str) -> None:
        if isinstance(key, str):
            key = _KeyPress(key)
        with self._lock:
            self._dispatch_key(key)
            if self._live is not None:
                self._live.update(self._render_root())

    def _render_root(self):
        """Render the live layout with a fixed 4-line top padding."""
        return Padding(self._build_layout(), (1, 1, 1, 1))

    def _dispatch_key(self, key: _KeyPress) -> None:
        state = self._state
        if state.mode_select_view is not None:
            result = state.mode_select_view.handle_key(key.key)
            if result.is_select:
                self._mode_select_result = result.mode or state.mode_select_view.current_mode
                state.mode_select_view = None
                self._mode_select_ready.set()
            elif result.is_cancel:
                self._mode_select_result = state.mode_select_view.current_mode
                state.mode_select_view = None
                self._mode_select_ready.set()
            return

        key_norm = key.key.lower() if len(key.key) == 1 else key.key
        ctrl = key.ctrl
        alt = key.alt
        shift = key.shift

        if state.multiline_view is not None:
            self._handle_multiline_key(state.multiline_view, key)
            return

        if state.approval_view is not None:
            approval_keys = {"up", "down", "left", "right", " ", "enter", "return", "\r", "\n"}
            if key.key in approval_keys or key_norm in approval_keys:
                result = state.approval_view.handle_key(key.key)
                if result.is_approved:
                    self._approval_result = (True, "")
                    state.approval_view = None
                    self._approval_ready.set()
                elif result.is_denied:
                    self._approval_result = (False, result.feedback)
                    state.approval_view = None
                    self._approval_ready.set()
                return

        # -"?-"? In-TUI input mode -"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?-"?
        if state.input_prompt is not None:
            if key.key in ("\r", "\n"):
                # Submit: capture result and signal the waiting thread.
                self._input_result = state.input_buffer
                state.input_prompt = None
                state.input_buffer = ""
                self._input_ready.set()
            elif key.key in ("\x7f", "\x08"):  # Backspace / DEL
                state.input_buffer = state.input_buffer[:-1]
            elif key.key == "\x1b":            # Escape -> clear buffer
                state.input_buffer = ""
            elif len(key.key) == 1 and key.key.isprintable():
                state.input_buffer += key.key
            return  # absorb all keys while input panel is open

        # Quit confirmation flow.
        if state.quit_confirm:
            if key.key in ("\r", "\n", "y", "Y"):
                state.quit_confirm = False
                self._trigger_quit()
                return
            elif key.key in ("c", "C"):
                state.quit_confirm = False
                self._trigger_quit(clean=True)
                return
            elif key.key == "\x1b" or key.key in ("n", "N"):
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

        elif key.key in ("left", "right"):
            if state.view_mode == "normal":
                current_idx = self._FOCUS_ORDER.index(state.focused_panel)
                step = -1 if key.key == "left" else 1
                state.focused_panel = self._FOCUS_ORDER[(current_idx + step) % len(self._FOCUS_ORDER)]

        elif key_norm == "d":
            state.view_mode = "normal" if state.view_mode == "logs" else "logs"

        elif key.key == "?":
            state.view_mode = "normal" if state.view_mode == "help" else "help"

        elif key_norm == "a":
            state.view_mode = "normal" if state.view_mode == "agent" else "agent"

        elif key_norm == "t":
            state.view_mode = "normal" if state.view_mode == "tasks" else "tasks"

        elif key_norm == "f":
            if state.focused_panel == "data":
                state.data_autoscroll = not state.data_autoscroll
                if state.data_autoscroll:
                    state.data_scroll_offset = 0
            elif state.focused_panel == "agent":
                state.agent_autoscroll = not state.agent_autoscroll
                if state.agent_autoscroll:
                    state.agent_scroll_offset = 0

        elif key.key == "up":
            self._scroll_panel(state.focused_panel, 1)

        elif key.key == "down":
            self._scroll_panel(state.focused_panel, -1)


    def _handle_multiline_key(self, view: MultilineInputView, key: _KeyPress) -> None:
        """Route keypresses to the multiline overlay and emit submit/cancel events."""
        state = self._state
        # Allow Ctrl+S as a portable submit fallback.
        key_for_view = "submit" if key.key == "\x13" else key.key
        result = view.handle_key(
            key_for_view,
            ctrl=key.ctrl,
            alt=key.alt,
            shift=key.shift,
        )
        if result.is_submit:
            self._emit_multiline_submit(view, result.value or "")
            state.multiline_view = None
        elif result.is_cancel:
            self._emit_multiline_cancel(view)
            state.multiline_view = None

    def _emit_multiline_submit(self, view: MultilineInputView, value: str) -> None:
        if self._emitter is not None:
            # Emit asynchronously to avoid re-entrant deadlocks:
            # key handling holds renderer lock while emitter dispatch is synchronous.
            def _emit() -> None:
                try:
                    self._emitter.submit_multiline_input(id=view.id, mode=view.mode, value=value)
                except Exception:
                    pass

            threading.Thread(target=_emit, daemon=True).start()

    def _emit_multiline_cancel(self, view: MultilineInputView) -> None:
        if self._emitter is not None:
            # Emit asynchronously to avoid re-entrant deadlocks:
            # key handling holds renderer lock while emitter dispatch is synchronous.
            def _emit() -> None:
                try:
                    self._emitter.cancel_multiline_input(id=view.id, mode=view.mode)
                except Exception:
                    pass

            threading.Thread(target=_emit, daemon=True).start()

    def _trigger_quit(self, clean: bool = False) -> None:
        if clean:
            if self._on_quit_clean is not None:
                self._on_quit_clean()
                return
            if self._on_quit is not None:
                self._on_quit()
            return
        if self._on_quit is not None:
            self._on_quit()

    def _scroll_panel(self, panel: str, delta: int) -> None:
        state = self._state
        if panel == "deck":
            state.deck_scroll_offset = max(0, state.deck_scroll_offset + delta)
            return
        if panel == "stage":
            if delta > 0:
                state.stage_autoscroll = False
                state.stage_scroll_offset += delta
            else:
                state.stage_scroll_offset = max(0, state.stage_scroll_offset + delta)
                if state.stage_scroll_offset == 0:
                    state.stage_autoscroll = True
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
            # Clear prior completion timestamp so elapsed time tracks the new run.
            state.ended_at = None
            state.awaiting_approval = False
            state.deck_scroll_offset = 0
            state.data_scroll_offset = 0
            state.data_autoscroll = True
            state.stage_scroll_offset = 0
            state.stage_autoscroll = True
            state.agent_scroll_offset = 0
            state.agent_autoscroll = True
            state.tasks = self._load_task_entries()
            state.agent_output.clear()
            state.agent_name = ""
            state.multiline_view = None
            state.mode_select_view = None
            state.approval_view = None

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

        elif isinstance(event, StageCompleted):
            entry = self._find_stage(event.stage_id)
            if entry is not None:
                entry.status = "done"
                entry.duration_ms = event.duration_ms
            if event.stage_id == "task_creation":
                state.tasks = self._load_task_entries()

        elif isinstance(event, StageFailed):
            entry = self._find_stage(event.stage_id)
            if entry is not None:
                entry.status = "failed"
                entry.error = event.error
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
            entry = self._find_task(event.task_id)
            if entry is None:
                entry = _TaskEntry(
                    task_id=event.task_id,
                    description=self._compact_task_text(event.description),
                    agent=event.agent,
                )
                state.tasks.append(entry)
            entry.agent = event.agent
            entry.status = "running"
            entry.error = None
            if state.stage_autoscroll:
                state.stage_scroll_offset = 0

        elif isinstance(event, TaskCompleted):
            entry = self._find_task(event.task_id)
            if entry is not None:
                entry.status = "done"
                entry.duration_ms = event.duration_ms
            if state.stage_autoscroll:
                state.stage_scroll_offset = 0

        elif isinstance(event, TaskFailed):
            entry = self._find_task(event.task_id)
            if entry is not None:
                entry.status = "failed"
                entry.error = event.error
            if state.stage_autoscroll:
                state.stage_scroll_offset = 0

        elif isinstance(event, RunCompleted):
            state.run_status = event.status.value
            state.awaiting_approval = False
            state.ended_at = time.monotonic()
            state.multiline_view = None
            state.mode_select_view = None
            state.approval_view = None

        elif isinstance(event, OpenMultilineInputEvent):
            state.multiline_view = MultilineInputView(
                id=event.id,
                title=event.title,
                prompt=event.prompt,
                initial_text=event.initial_text or "",
                mode=event.mode,
                source_stage=event.source_stage,
                body_height=self._multiline_body_height(),
            )
            state.input_prompt = None

        elif isinstance(event, SubmitMultilineInputEvent):
            if state.multiline_view and state.multiline_view.id == event.id:
                state.multiline_view = None
            if state.approval_view is not None:
                approval_result = state.approval_view.handle_event(event)
                if approval_result.is_denied:
                    self._approval_result = (False, approval_result.feedback)
                    state.approval_view = None
                    self._approval_ready.set()

        elif isinstance(event, CancelMultilineInputEvent):
            if state.multiline_view and state.multiline_view.id == event.id:
                state.multiline_view = None
            if state.approval_view is not None:
                state.approval_view.handle_event(event)

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

    def _load_task_entries(self) -> list[_TaskEntry]:
        if not _TASK_GRAPH_FILE.exists():
            return []
        try:
            payload = json.loads(_TASK_GRAPH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

        entries: list[_TaskEntry] = []
        for task in payload.get("tasks", []):
            status = str(task.get("status", "pending"))
            entries.append(
                _TaskEntry(
                    task_id=str(task.get("id", "")),
                    description=self._compact_task_text(str(task.get("description", "")).strip()),
                    status="done" if status == "completed" else status,
                    duration_ms=None,
                )
            )
        return entries

    @staticmethod
    def _compact_task_text(description: str, limit: int = 48) -> str:
        text = " ".join(description.split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    # -----------------------------------------------------------------------------
    # Layout builders
    # -----------------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        state = self._state
        if state.mode_select_view is not None:
            keybar = self._build_keybar()
            layout = Layout()
            layout.split_column(
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(state.mode_select_view.render(), name="mode_select"),
                Layout(keybar, name="keybar", size=4),
            )
            return layout

        if state.multiline_view is not None:
            state.multiline_view.set_body_height(self._multiline_body_height())
            multiline_size = max(5, self._console.size.height - 3 - 4 - 2)
            keybar = self._build_keybar()
            layout = Layout()
            layout.split_column(
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(state.multiline_view.render(), name="multiline", size=multiline_size),
                Layout(keybar, name="keybar", size=4),
            )
            return layout

        if state.approval_view is not None:
            approval_size = max(5, self._console.size.height - 3 - 4 - 2)
            keybar = self._build_keybar()
            layout = Layout()
            layout.split_column(
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(state.approval_view.render(), name="approval", size=approval_size),
                Layout(keybar, name="keybar", size=4),
            )
            return layout

        has_input = state.input_prompt is not None
        keybar = self._build_keybar()
        keybar_size = 4

        def _fullscreen_body_height() -> int:
            base = self._console.size.height - 3 - keybar_size - (5 if has_input else 0)
            return max(5, base - 2)  # trim two lines from the body to keep keybar fully visible

        if state.view_mode == "help":
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(self._build_help_panel(), name="help", size=_fullscreen_body_height()),
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=keybar_size))
            layout = Layout()
            layout.split_column(*cols)
            return layout

        if state.view_mode == "logs":
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(self._build_stream_panel(), name="stream", size=_fullscreen_body_height()),
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=keybar_size))
            layout = Layout()
            layout.split_column(*cols)
            return layout

        if state.view_mode == "agent":
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(self._build_agent_panel(), name="agent", size=_fullscreen_body_height()),
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=keybar_size))
            layout = Layout()
            layout.split_column(*cols)
            return layout

        if state.view_mode == "tasks":
            cols = [
                Layout(self._build_status_bar(), name="status", size=3),
                Layout(self._build_timing_panel(), name="tasks", size=_fullscreen_body_height()),
            ]
            if has_input:
                cols.append(Layout(self._build_input_panel(), name="input", size=5))
            cols.append(Layout(keybar, name="keybar", size=keybar_size))
            layout = Layout()
            layout.split_column(*cols)
            return layout
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
        timing_size = max(6, timing_size - 2)
        agent_size = max(5, (estimated_body_height // 2) - 3)
        agent_size += 6
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

    def _default_body_metrics(self) -> dict[str, int]:
        has_input = self._state.input_prompt is not None
        base_body_height = max(
            8,
            self._console.size.height - 3 - 4 - (5 if has_input else 0),
        )
        body_size = max(8, base_body_height - 2)
        estimated_body_height = base_body_height
        timing_size = max(6, (estimated_body_height * 4) // 7 - 1)
        timing_size = max(6, timing_size - 2)
        agent_size = max(5, (estimated_body_height // 2) - 3)
        agent_size += 6
        pipeline_size = max(5, body_size - timing_size)
        stream_size = max(5, body_size - agent_size)
        return {
            "pipeline_size": pipeline_size,
            "timing_size": timing_size,
            "stream_size": stream_size,
            "agent_size": agent_size,
        }

    def _build_status_bar(self) -> Panel:
        state = self._state
        elapsed = self._elapsed_str()
        run_color = _C_NEON_CYAN

        bar = Text()

        if state.quit_confirm:
            bar.append("  ⚠  JACK OUT? ", style=_C_NEON_CYAN)
            bar.append(" Y ", style="bold #2EDCD1")
            bar.append(" quit  ", style=_C_NEON_CYAN)
            bar.append(" C ", style="bold #2EDCD1")
            bar.append(" quit+clean  ", style=_C_NEON_CYAN)
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
        "pending": ("◦", _C_DIM_CYAN),
        "running": ("▷", "#2EDCD1"),
        "done":    ("▶", _C_NEON_PINK),
        "failed":  ("✖", _C_NEON_CYAN),
    }
    _FOCUS_ORDER: tuple[str, ...] = ("none", "deck", "data", "stage", "agent")

    def _build_pipeline_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(justify="right", no_wrap=True)
        table.add_column(justify="left", no_wrap=False)
        table.add_column(justify="right", no_wrap=True)
        rows: list[tuple[Text, Text, Text]] = []

        if not self._state.stages:
            rows.append(
                (
                    Text("◈", style=_C_DIM_CYAN),
                    Text("awaiting deck init...", style=_C_DIM_CYAN),
                    Text("", style=_C_DIM_CYAN),
                )
            )
        else:
            active_tasks = self._state.tasks
            for entry in sorted(self._state.stages, key=lambda e: e.index):
                symbol, color = _STAGE_STYLE.get(entry.status, ("?", "#2EDCD1"))
                name_text = Text(entry.stage_name, style=color)
                if not self._state.compact and entry.error:
                    name_text.append(f"\n  ERR: {entry.error}", style=_C_NEON_CYAN)
                time_text = Text(
                    f"{entry.duration_ms / 1000:.1f}s" if entry.duration_ms is not None else "",
                    style=_C_DIM_CYAN,
                )
                rows.append((Text(symbol, style=color), name_text, time_text))

                if entry.status == "running" and active_tasks and not self._state.compact:
                    for task in active_tasks:
                        t_sym, t_color = self._TASK_STYLE.get(task.status, ("▷", "#2EDCD1"))
                        desc = task.description
                        if len(desc) > 38:
                            desc = desc[:35] + "…"
                        task_text = Text()
                        task_text.append(desc, style=t_color)
                        task_text.append(f"  /{task.agent}/", style=_C_DIM_CYAN)
                        if task.error:
                            task_text.append(f"  {task.error}", style=_C_NEON_CYAN)
                        task_time = Text(
                            f"{task.duration_ms / 1000:.1f}s" if task.duration_ms is not None else "",
                            style=_C_DIM_CYAN,
                        )
                        rows.append((Text(f"  {t_sym}", style=t_color), task_text, task_time))

        max_rows = self._pipeline_panel_lines()
        for icon, content, time_text in self._window_slice(rows, self._state.deck_scroll_offset, max_rows):
            table.add_row(icon, content, time_text)

        title = self._panel_title("deck", "// DECK")
        return Panel(table, title=title, border_style=self._panel_border_style("deck"), padding=(0, 1))

    def _build_timing_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(justify="right", no_wrap=True)
        table.add_column(justify="left", no_wrap=False)

        rows: list[tuple[Text, Text]] = []
        if not self._state.tasks:
            rows.append(
                (
                    Text("◦", style=_C_DIM_CYAN),
                    Text("awaiting task graph...", style=_C_DIM_CYAN),
                )
            )
        else:
            for task in self._state.tasks:
                symbol, color = self._TASK_STYLE.get(task.status, ("◦", _C_DIM_CYAN))
                task_text = Text(task.description, style=color)
                if task.agent and task.status == "running":
                    task_text.append(f"  /{task.agent}/", style=_C_DIM_CYAN)
                if task.error:
                    task_text.append(f"  {task.error}", style=_C_NEON_CYAN)
                rows.append((Text(symbol, style=color), task_text))

        for icon, content in self._window_slice(rows, self._state.stage_scroll_offset, self._timing_panel_lines()):
            table.add_row(icon, content)

        return Panel(
            table,
            title=self._panel_title("stage", "// TASKLIST"),
            border_style=self._panel_border_style("stage"),
            padding=(0, 1),
        )

    def _build_stream_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True, style=_C_NEON_PINK)  # timestamp
        table.add_column(no_wrap=True)                       # level tag
        table.add_column(no_wrap=False)                      # message

        logs = list(self._state.logs)

        visible = self._window_slice(logs, self._state.data_scroll_offset, self._stream_panel_lines())

        for log in visible:
            ts_str = _short_ts(log.ts)
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

    def _pipeline_panel_lines(self) -> int:
        return max(3, self._default_body_metrics()["pipeline_size"] - 2)

    def _timing_panel_lines(self) -> int:
        return max(3, self._default_body_metrics()["timing_size"] - 2)

    def _stream_panel_lines(self) -> int:
        return max(3, self._default_body_metrics()["stream_size"] - 2)

    def _agent_panel_lines(self) -> int:
        return max(3, self._default_body_metrics()["agent_size"] - 2)

    def _build_agent_panel(self) -> Panel:
        title = "// AGENT.OUTPUT"
        if self._state.agent_name:
            title = f"{title} — {self._state.agent_name}"

        lines = list(self._state.agent_output)
        if not lines:
            body: Text | str = Text("No agent running.", style=f"dim {_C_PRIMARY_TEXT}")
        else:
            visible = self._window_slice(lines, self._state.agent_scroll_offset, self._agent_panel_lines())
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

        if state.awaiting_approval and _KEYBAR_APPROVE:
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
        bar.append(" ←/→ ", style="bold #ff2d6f")
        bar.append(" focus panel", style=_C_DIM_CYAN)
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

    def _multiline_body_height(self) -> int:
        """Estimate body rows available for the multiline overlay."""
        total = self._console.size.height
        reserved = 3 + 4 + 7  # status + keybar + header/prompt/footer padding (+1 safety)
        return max(5, total - reserved)


def _short_ts(ts: str) -> str:
    """Return a compact local-system time from an ISO timestamp string."""
    try:
        normalized = ts.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.astimezone().strftime("%H:%M:%S")
    except Exception:
        return ts[11:19] if len(ts) >= 19 else ts


