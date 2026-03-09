"""Rich-based full-screen TUI renderer for the A.I.N. Pipeline."""

from __future__ import annotations

import collections
import sys
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from ain.runtime.events import (
    RunStarted,
    RunCompleted,
    StageQueued,
    StageStarted,
    StageCompleted,
    StageFailed,
    LogLine,
    LogLevel,
    TaskStarted,
    TaskCompleted,
    TaskFailed,
    AwaitingApproval,
    AgentOutput,
    RunStatus,
)

# ── Colour constants ──────────────────────────────────────────────────────────

C_PRIMARY  = "#00e5ff"       # text, stage names, feed messages
C_ACCENT   = "#ff2d78"       # titles, keys, separators, active highlights
C_ACTIVE   = "bold #00e5ff"
C_DONE     = "bright_green"
C_FAILED   = "bright_red"
C_TS       = "dim cyan"      # timestamps, dim labels
C_INF      = "cyan"
C_WRN      = "yellow"
C_ERR      = "bright_red"
C_BORDER   = "dim #ff2d78"

MAX_FEED_LINES = 500


# ── Stage state ───────────────────────────────────────────────────────────────

class _StageState:
    __slots__ = ("stage_id", "name", "status")

    def __init__(self, stage_id: str, name: str) -> None:
        self.stage_id = stage_id
        self.name     = name
        self.status   = "queued"   # queued | running | done | failed


# ── Renderer ──────────────────────────────────────────────────────────────────

class RichRenderer:
    """Full-screen Rich TUI.

    - Clears the terminal on start (screen=True).
    - Keyboard thread handles ↑/↓ scroll, printable input, Enter, Q/Ctrl-C quit.
    - request_input() blocks in-place; the TUI keeps refreshing while the user
      types — no suspend/resume needed.
    """

    def __init__(self, version: str = "0.1.8") -> None:
        self._version = version

        # Ensure UTF-8 on Windows before creating Console
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

        # legacy_windows=False → VT/ANSI output; avoids cp1252 encode errors
        self._console = Console(legacy_windows=False)

        # ── Run state ──────────────────────────────────────────────────────
        self._run_id     : str   = "--------"
        self._status     : str   = "IDLE"
        self._start_mono : float = time.monotonic()

        # ── Stage deck ─────────────────────────────────────────────────────
        self._stages   : List[_StageState]      = []
        self._stage_map: Dict[str, _StageState] = {}

        # ── Data feed ──────────────────────────────────────────────────────
        self._feed        : Deque[Text] = collections.deque(maxlen=MAX_FEED_LINES)
        self._feed_scroll : int         = 0   # lines hidden from bottom (0 = live)

        # ── Agent output ────────────────────────────────────────────────────
        self._agent_output: Deque[Text] = collections.deque(maxlen=MAX_FEED_LINES)
        self._agent_scroll: int         = 0
        self._agent_name  : str         = ""

        # ── Scroll focus & freeze ───────────────────────────────────────────
        self._scroll_target: str  = "feed"   # "feed" | "agent"
        self._frozen       : bool = False    # when True, live scroll is paused

        # ── Inline input ───────────────────────────────────────────────────
        self._input_pending: bool            = False
        self._input_prompt : str             = ""
        self._input_buffer : str             = ""
        self._input_event  : threading.Event = threading.Event()

        # ── Internals ──────────────────────────────────────────────────────
        self._lock              : threading.Lock            = threading.Lock()
        self._live              : Optional[Live]            = None
        self._kb_thread         : Optional[threading.Thread] = None
        self._running           : bool                      = False
        self._kb_paused         : bool                      = False  # True while TUI is suspended
        self._last_agent_refresh: float                     = 0.0  # throttle agent output redraws
        self._mode_details      : Dict[str, str]            = {
            "key": "default",
            "label": "Default",
            "summary": "Gemini -> Codex -> Chief -> Claude",
        }
        self._cycle_mode_cb     : Any                       = None

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, emitter: Any) -> None:
        emitter.subscribe(self._handle_event)

    def start(self) -> None:
        """Start the full-screen Live display and keyboard thread."""
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

        self._running = True
        self._live = Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=8,
            screen=True,   # ← takes over the full terminal, clears on start/stop
        )
        self._live.start()

        self._kb_thread = threading.Thread(target=self._kb_loop, daemon=True)
        self._kb_thread.start()

        # Tick thread: refreshes the display every second so UPTIME updates
        # even when no events or input arrive.
        threading.Thread(target=self._tick_loop, daemon=True).start()

    def stop(self) -> None:
        """End the Live display and restore the terminal."""
        self._running = False
        # Unblock any waiting request_input
        self._input_event.set()
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def suspend(self) -> None:
        """Stop the Live display so a subprocess can own the terminal."""
        self._kb_paused = True   # stop stealing keystrokes from the subprocess
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def resume(self) -> None:
        """Create a fresh Live display after a subprocess has finished.

        Rich's Live cannot be restarted after stop() — a new instance is required.
        """
        self._live = Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=8,
            screen=True,
        )
        try:
            self._live.start()
        except Exception:
            self._live = None
            return
        self._kb_paused = False  # let the keyboard thread take input again
        self._refresh()

    def request_input(self, prompt: str) -> str:
        """Show *prompt* in the INPUT panel and wait for the user to press Enter.

        The TUI keeps refreshing while the user types.
        """
        with self._lock:
            self._input_pending = True
            self._input_prompt  = prompt
            self._input_buffer  = ""
        self._input_event.clear()
        self._refresh()

        self._input_event.wait()   # released by _handle_char on Enter / stop()

        with self._lock:
            result              = self._input_buffer
            self._input_pending = False
            self._input_prompt  = ""
            self._input_buffer  = ""
        self._refresh()
        return result

    def configure_mode_controls(self, mode_details: Dict[str, str], cycle_callback: Any) -> None:
        with self._lock:
            self._mode_details = dict(mode_details)
            self._cycle_mode_cb = cycle_callback
        self._refresh()

    # ── Tick thread (keeps UPTIME live) ──────────────────────────────────────

    def _tick_loop(self) -> None:
        while self._running:
            time.sleep(1)
            if self._running:
                self._refresh()

    # ── Keyboard thread ───────────────────────────────────────────────────────

    def _kb_loop(self) -> None:
        try:
            import msvcrt
            self._kb_loop_windows(msvcrt)
        except ImportError:
            self._kb_loop_unix()

    def _kb_loop_windows(self, msvcrt: Any) -> None:
        while self._running:
            if self._kb_paused:
                time.sleep(0.05)
                continue
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):   # extended key prefix
                    ch2 = msvcrt.getch()
                    if ch2 == b"H":            # ↑
                        self.scroll_up()
                    elif ch2 == b"P":          # ↓
                        self.scroll_down()
                else:
                    self._handle_char(ch)
            else:
                time.sleep(0.02)

    def _kb_loop_unix(self) -> None:
        import select
        try:
            import tty, termios
            fd  = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
        except Exception:
            # If we can't set raw mode just skip kb
            return
        try:
            while self._running:
                if self._kb_paused:
                    time.sleep(0.05)
                    continue
                if select.select([sys.stdin], [], [], 0.02)[0]:
                    ch = sys.stdin.buffer.read(1)
                    if ch == b"\x1b":
                        seq = sys.stdin.buffer.read(2)
                        if seq == b"[A":
                            self.scroll_up()
                        elif seq == b"[B":
                            self.scroll_down()
                    else:
                        self._handle_char(ch)
        finally:
            import termios as _t
            _t.tcsetattr(fd, _t.TCSADRAIN, old)

    def _handle_char(self, ch: bytes) -> None:
        # Ctrl-C
        if ch == b"\x03":
            self._running = False
            self.stop()
            sys.exit(0)

        in_input = self._input_pending

        # Enter
        if ch in (b"\r", b"\n"):
            if in_input:
                self._input_event.set()
            return

        # Backspace
        if ch in (b"\x08", b"\x7f"):
            if in_input:
                with self._lock:
                    self._input_buffer = self._input_buffer[:-1]
                self._refresh()
            return

        # Printable character
        try:
            char = ch.decode("utf-8", errors="replace")
        except Exception:
            return

        if in_input:
            if char.isprintable():
                with self._lock:
                    self._input_buffer += char
                self._refresh()
        else:
            k = char.lower()
            if k == "q":
                self._running = False
                self.stop()
                sys.exit(0)
            elif k == "\t":          # Tab — switch scroll focus
                with self._lock:
                    self._scroll_target = "agent" if self._scroll_target == "feed" else "feed"
                self._refresh()
            elif k == "f":           # F — freeze / unfreeze live scroll
                with self._lock:
                    self._frozen = not self._frozen
                self._refresh()
            elif k == "r":           # R — jump back to live (bottom)
                with self._lock:
                    self._feed_scroll  = 0
                    self._agent_scroll = 0
                    self._frozen       = False
                self._refresh()
            elif k == "c":           # C — clear agent output panel
                with self._lock:
                    self._agent_output.clear()
                    self._agent_scroll = 0
                self._refresh()
            elif k == "m":
                callback = self._cycle_mode_cb
                if callback is None:
                    return
                try:
                    details = callback()
                except Exception:
                    return
                if isinstance(details, dict):
                    with self._lock:
                        self._mode_details = dict(details)
                    self._refresh()

    # ── Event handler ─────────────────────────────────────────────────────────

    def _handle_event(self, event: Any) -> None:
        with self._lock:
            if isinstance(event, RunStarted):
                self._run_id     = event.run_id
                self._start_mono = time.monotonic()
                self._status     = "RUNNING"
                self._stages.clear()
                self._stage_map.clear()
                self._feed.clear()

            elif isinstance(event, RunCompleted):
                self._status = {
                    RunStatus.DONE:        "DONE",
                    RunStatus.FAILED:      "FAILED",
                    RunStatus.INTERRUPTED: "INTERRUPTED",
                }.get(event.status, str(event.status).upper())

            elif isinstance(event, StageQueued):
                ss = _StageState(event.stage_id, event.stage_name)
                self._stages.append(ss)
                self._stage_map[event.stage_id] = ss

            elif isinstance(event, StageStarted):
                ss = self._stage_map.get(event.stage_id)
                if ss:
                    ss.status = "running"
                # Clear agent panel between stages
                self._agent_output.clear()
                self._agent_scroll = 0
                self._agent_name   = ""

            elif isinstance(event, StageCompleted):
                ss = self._stage_map.get(event.stage_id)
                if ss:
                    ss.status = "done"

            elif isinstance(event, StageFailed):
                ss = self._stage_map.get(event.stage_id)
                if ss:
                    ss.status = "failed"

            elif isinstance(event, LogLine):
                self._feed.append(self._fmt_log(event))

            elif isinstance(event, TaskStarted):
                t = Text()
                t.append("  TASK ▸ ", style=f"bold {C_ACCENT}")
                t.append(event.description, style=C_PRIMARY)
                t.append(f"  [{event.agent}]", style=C_TS)
                self._feed.append(t)

            elif isinstance(event, TaskCompleted):
                t = Text()
                t.append("  TASK ✓ ", style=f"bold {C_DONE}")
                t.append(event.description, style=C_PRIMARY)
                t.append(f"  {event.duration_ms}ms", style=C_TS)
                self._feed.append(t)

            elif isinstance(event, TaskFailed):
                t = Text()
                t.append("  TASK ✗ ", style=f"bold {C_FAILED}")
                t.append(event.description, style=C_PRIMARY)
                if event.error:
                    t.append(f"  {event.error}", style=C_ERR)
                self._feed.append(t)

            elif isinstance(event, AgentOutput):
                if event.agent and event.agent != self._agent_name:
                    self._agent_name = event.agent
                t = Text()
                t.append(event.line, style=C_PRIMARY)
                self._agent_output.append(t)
                if not self._frozen:
                    self._agent_scroll = 0   # stay live
                # Throttle: redraw at most every 100 ms to avoid overwhelming Rich
                now = time.monotonic()
                if now - self._last_agent_refresh >= 0.1:
                    self._last_agent_refresh = now
                    self._refresh()
                return  # skip the final self._refresh() below

            elif isinstance(event, AwaitingApproval):
                t = Text()
                t.append("  ⚑ Awaiting approval — run: ", style=f"bold {C_ACCENT}")
                t.append("ain approve", style=f"bold {C_PRIMARY}")
                self._feed.append(t)

        self._refresh()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._build_layout())
            except Exception:
                pass

    def _fmt_log(self, event: LogLine) -> Text:
        ts = event.ts[11:19] if len(event.ts) >= 19 else event.ts
        level_style = {LogLevel.WARN: C_WRN, LogLevel.ERROR: C_ERR}.get(event.level, C_ACCENT)
        level_tag   = {LogLevel.INFO: "INF", LogLevel.WARN: "WRN", LogLevel.ERROR: "ERR"}.get(event.level, "INF")
        # Stage banner lines (━━━ … ━━━) get accent colour; normal text is cyan
        msg_style = C_ACCENT if event.message.startswith("━━━") else C_PRIMARY
        t = Text()
        t.append(ts,               style=f"dim {C_PRIMARY}")
        t.append(" ")
        t.append(f"[{level_tag}]", style=level_style) # red-pink for INF
        t.append(" — ",            style=C_BORDER)
        t.append(event.message,    style=msg_style)
        return t

    def _uptime(self) -> str:
        s = int(time.monotonic() - self._start_mono)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        return f"{m}m{s:02d}s" if m < 60 else f"{m // 60}h{m % 60:02d}m{s:02d}s"

    def _node(self) -> str:
        return (self._run_id[:8] if len(self._run_id) >= 8 else self._run_id).ljust(8, "-")

    # ── Layout builders ───────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        root = Layout(name="root")
        root.split_column(
            Layout(name="header",      size=3),
            Layout(name="body"),
            Layout(name="input_panel", size=5),
            Layout(name="footer",      size=4),
        )
        root["body"].split_row(
            Layout(name="deck",  ratio=1),
            Layout(name="right", ratio=3),
        )
        root["right"].split_column(
            Layout(name="feed",         ratio=1),
            Layout(name="agent_output", ratio=1),
        )
        root["header"].update(self._render_header())
        root["deck"].update(self._render_deck())
        root["feed"].update(self._render_feed())
        root["agent_output"].update(self._render_agent_output())
        root["input_panel"].update(self._render_input())
        root["footer"].update(self._render_footer())
        return root

    def _render_header(self) -> Panel:
        sep = Text(" ║ ", style=C_ACCENT)
        t = Text()
        t.append("• ",                       style=C_ACCENT)
        t.append("A.I.N.",                   style=f"bold {C_ACCENT}")
        t.append(f" v{self._version}",       style=f"bold {C_PRIMARY}")
        t.append_text(sep)
        t.append("SYS: ",                    style=f"bold {C_ACCENT}")
        t.append(self._status,               style=f"bold {C_PRIMARY}")
        t.append_text(sep)
        t.append("UPTIME: ",                 style=f"bold {C_ACCENT}")
        t.append(self._uptime(),             style=C_PRIMARY)
        t.append_text(sep)
        t.append("NODE: ",                   style=f"bold {C_ACCENT}")
        t.append(self._node(),               style=C_PRIMARY)
        return Panel(t, border_style=C_ACCENT, padding=(0, 1))

    def _render_deck(self) -> Panel:
        body = Text()
        for i, ss in enumerate(self._stages):
            if i:
                body.append("\n")
            if ss.status == "running":
                body.append("► ", style=C_ACTIVE)
                body.append(ss.name, style=C_ACTIVE)
            elif ss.status == "done":
                body.append("✓ ", style=C_ACCENT)
                body.append(ss.name, style=C_ACCENT)
            elif ss.status == "failed":
                body.append("✗ ", style=C_FAILED)
                body.append(ss.name, style=f"dim {C_FAILED}")
            else:
                body.append("◈ ", style=f"dim {C_PRIMARY}")
                body.append(ss.name, style=f"dim {C_PRIMARY}")
        if not self._stages:
            body.append("No stages queued.", style=f"dim {C_PRIMARY}")
        return Panel(
            body,
            title=Text("// DECK", style=C_ACCENT),
            border_style=C_ACCENT,
            padding=(1, 1),
        )

    def _panel_lines(self) -> int:
        """Approximate number of content lines available in each half-height panel."""
        h = self._console.size.height
        # header(3) + input(5) + footer(3) + borders ≈ 13 overhead
        # body splits right pane into feed + agent_output (ratio 1:1)
        return max(5, (h - 13) // 2 - 2)

    def _render_feed(self) -> Panel:
        lines = list(self._feed)
        if not lines:
            body: Any = Text("Awaiting data feed…", style=f"dim {C_PRIMARY}")
        else:
            scroll = max(0, min(self._feed_scroll, len(lines) - 1))
            if scroll:
                # scrolled up — show older window ending before the scroll offset
                end = len(lines) - scroll
                visible = lines[max(0, end - self._panel_lines()):end]
            else:
                # live mode — always show the most recent lines
                visible = lines[-self._panel_lines():]
            body = Text()
            for i, line in enumerate(visible):
                if i:
                    body.append("\n")
                body.append_text(line)
        return Panel(
            body,
            title=Text("// DATA FEED", style=C_ACCENT),
            border_style=C_ACCENT,
            padding=(0, 1),
        )

    def _render_agent_output(self) -> Panel:
        title_label = f"// AGENT.OUTPUT — {self._agent_name}" if self._agent_name else "// AGENT.OUTPUT"
        lines = list(self._agent_output)
        if not lines:
            body: Any = Text("No agent running.", style=f"dim {C_PRIMARY}")
        else:
            scroll = max(0, min(self._agent_scroll, len(lines) - 1))
            if scroll:
                end = len(lines) - scroll
                visible = lines[max(0, end - self._panel_lines()):end]
            else:
                visible = lines[-self._panel_lines():]
            body = Text()
            for i, line in enumerate(visible):
                if i:
                    body.append("\n")
                body.append_text(line)
        return Panel(
            body,
            title=Text(title_label, style=C_ACCENT),
            border_style=C_ACCENT,
            padding=(0, 1),
        )

    def _render_input(self) -> Panel:
        # Read snapshot without lock (display-only; slight race is harmless)
        pending = self._input_pending
        prompt  = self._input_prompt
        buf     = self._input_buffer

        if pending:
            body = Text()
            body.append(f"▸ {prompt}\n\n", style=f"bold {C_PRIMARY}")
            body.append(f"> {buf}",         style=f"bold {C_PRIMARY}")
            body.append("█",               style=f"bold {C_ACCENT}")
            return Panel(
                body,
                title=Text("// INPUT.AWAITING", style=f"bold {C_ACCENT}"),
                border_style=f"bold {C_ACCENT}",
                padding=(1, 2),
            )
        return Panel(
            Text("Idle. No input requested.", style=f"dim {C_PRIMARY}"),
            title=Text("// INPUT.IDLE", style=C_ACCENT),
            border_style=C_ACCENT,
            padding=(1, 2),
        )

    def _render_footer(self) -> Panel:
        t = Text()
        focus_label = "agent" if self._scroll_target == "agent" else "feed"
        freeze_label = "unfreeze" if self._frozen else "freeze"
        shortcuts = [
            ("Q",     "quit"),
            ("Tab",   f"focus:{focus_label}"),
            ("↑/↓",   "scroll"),
            ("F",     freeze_label),
            ("R",     "live"),
            ("C",     "clear agent"),
            ("M",     "cycle mode"),
        ]
        for i, (key, label) in enumerate(shortcuts):
            if i:
                t.append("   ")
            t.append(key,          style=f"bold {C_ACCENT}")
            t.append(f"  {label}", style=C_PRIMARY)
        t.append("\n")
        t.append("MODE ", style=f"bold {C_ACCENT}")
        t.append(self._mode_details.get("key", "default"), style=f"bold {C_PRIMARY}")
        t.append("  |  ", style=C_ACCENT)
        t.append(self._mode_details.get("summary", ""), style=C_PRIMARY)
        return Panel(t, border_style=C_ACCENT, padding=(0, 1))

    # ── Scroll helpers ────────────────────────────────────────────────────────

    def scroll_up(self, lines: int = 3) -> None:
        with self._lock:
            if self._scroll_target == "agent":
                self._agent_scroll = min(self._agent_scroll + lines, MAX_FEED_LINES - 1)
            else:
                self._feed_scroll = min(self._feed_scroll + lines, MAX_FEED_LINES - 1)
        self._refresh()

    def scroll_down(self, lines: int = 3) -> None:
        with self._lock:
            if self._scroll_target == "agent":
                self._agent_scroll = max(self._agent_scroll - lines, 0)
            else:
                self._feed_scroll = max(self._feed_scroll - lines, 0)
        self._refresh()
