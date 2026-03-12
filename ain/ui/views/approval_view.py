"""Rich view for task approval/denial that triggers multiline feedback on denial."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ain.models.state import MultilineInputMode
from ain.runtime.emitter import Emitter
from ain.runtime.events import CancelMultilineInputEvent, SubmitMultilineInputEvent

# Shared neon palette for UI consistency.
_C_PRIMARY_TEXT = "#2EDCD1"
_C_SECONDARY_TEXT = "#23A19F"
_C_NEON_CYAN = f"bold {_C_PRIMARY_TEXT}"
_C_NEON_PINK = "bold #ff2d6f"
_C_BORDER = "#ff2d6f"

_TASK_DENIAL_CONTEXT_PREFIX = "approval.task_denial"
_TASK_DENIAL_TITLE = "Explain why you are denying this task"
_TASK_DENIAL_PROMPT_TEMPLATE = (
    "Explain why you are denying this task so planning can be rerun with your feedback.\n\n"
    "Task {task_id}: {description}"
)


@dataclass
class ApprovalResult:
    """Outcome of a user action inside the approval view."""

    action: str  # "none" | "approved" | "awaiting_feedback" | "denied"
    denied_task_ids: List[str] = field(default_factory=list)
    feedback: str = ""

    @property
    def is_approved(self) -> bool:
        return self.action == "approved"

    @property
    def is_denied(self) -> bool:
        return self.action == "denied"

    @property
    def is_waiting_feedback(self) -> bool:
        return self.action == "awaiting_feedback"


class ApprovalView:
    """Task approval list with denial feedback routed to multiline input."""

    def __init__(
        self,
        tasks: Sequence[Dict[str, Any]],
        *,
        emitter: Emitter | None = None,
        existing_feedback: Dict[str, str] | None = None,
        source_stage: str = "waiting_approval",
    ) -> None:
        self._tasks: List[Dict[str, str]] = [
            {
                "id": str(task.get("id", idx + 1)),
                "description": str(task.get("description", "")).strip(),
            }
            for idx, task in enumerate(tasks)
        ]
        self._decisions: List[bool] = [True] * len(self._tasks)
        self._selected_idx = 0
        self._emitter = emitter
        self._feedback_by_context = self._normalize_feedback(existing_feedback or {})
        self._source_stage = source_stage
        self._pending_context_id: str | None = None
        self._pending_task_id: str | None = None
        self._active_denials: List[str] = []
        self._pending_denials: List[str] = []
        self._awaiting_feedback = False

    # ------------------------------------------------------------------ public API

    def handle_key(self, key: str) -> ApprovalResult:
        """Process a key press; Enter opens feedback when any task is denied."""

        norm = self._normalize_key(key)

        if self._awaiting_feedback:
            return ApprovalResult("awaiting_feedback", denied_task_ids=list(self._pending_denials))

        if norm == "up":
            self._selected_idx = max(0, self._selected_idx - 1)
        elif norm == "down":
            self._selected_idx = min(len(self._tasks) - 1, self._selected_idx + 1)
        elif norm in ("left", "right", " "):
            self._toggle_selected()
        elif norm == "enter":
            denied_ids = self._denied_task_ids()
            if not self._tasks or not denied_ids:
                return ApprovalResult("approved")
            denial_queue = self._ordered_denials(denied_ids)
            opened = self._start_feedback_collection(denial_queue)
            if opened:
                return ApprovalResult(
                    "awaiting_feedback",
                    denied_task_ids=list(denial_queue),
                )
            return ApprovalResult(
                "denied",
                denied_task_ids=list(denial_queue),
                feedback=self._combined_feedback(denial_queue),
            )

        return ApprovalResult("none")

    def handle_event(self, event: Any) -> ApprovalResult:
        """Handle multiline submit/cancel events emitted by the renderer."""

        if self._pending_context_id is None:
            return ApprovalResult("none")

        if isinstance(event, SubmitMultilineInputEvent):
            if self._matches_pending(event.id, event.mode):
                feedback = (event.value or "").strip()
                if self._pending_task_id:
                    self._set_feedback(self._pending_task_id, feedback)
                    if self._pending_denials and self._pending_denials[0] == self._pending_task_id:
                        self._pending_denials.pop(0)
                    else:
                        self._pending_denials = [
                            task_id for task_id in self._pending_denials if task_id != self._pending_task_id
                        ]
                self._awaiting_feedback = False
                if self._open_next_feedback_overlay():
                    return ApprovalResult(
                        "awaiting_feedback",
                        denied_task_ids=list(self._active_denials),
                    )
                denied_task_ids = list(self._active_denials)
                combined_feedback = self._combined_feedback(denied_task_ids)
                self._clear_pending()
                return ApprovalResult(
                    "denied",
                    denied_task_ids=denied_task_ids,
                    feedback=combined_feedback,
                )

        elif isinstance(event, CancelMultilineInputEvent):
            if self._matches_pending(event.id, event.mode):
                self._awaiting_feedback = False
                result = ApprovalResult("none", denied_task_ids=list(self._active_denials))
                self._clear_pending()
                return result

        return ApprovalResult("none")

    def render(self) -> Panel:
        """Render the task list with approval/denial markers."""

        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True, style=_C_SECONDARY_TEXT)
        table.add_column(no_wrap=False, style=_C_PRIMARY_TEXT)
        table.add_column(no_wrap=True, justify="right", style=_C_SECONDARY_TEXT)

        if not self._tasks:
            table.add_row("  ", Text("No tasks to review.", style=_C_SECONDARY_TEXT), "")
        else:
            for idx, task in enumerate(self._tasks):
                cursor = ">" if idx == self._selected_idx else " "
                status = "ACCEPT" if self._decisions[idx] else "DENY"
                if not self._decisions[idx] and self._has_feedback(task["id"]):
                    status = "DENY*"
                status_style = _C_NEON_CYAN if self._decisions[idx] else _C_NEON_PINK
                desc = self._truncate(task["description"], limit=96)
                table.add_row(
                    Text(f" {cursor} ", style=_C_NEON_PINK if idx == self._selected_idx else _C_SECONDARY_TEXT),
                    Text(f"{task['id']}. {desc}", style=_C_PRIMARY_TEXT),
                    Text(status, style=status_style),
                )

        footer = Text()
        # Start one line below the table and indent to align with the task numbers.
        footer.append("\n    ", style=_C_SECONDARY_TEXT)
        footer.append("UP/DOWN select  ", style=_C_SECONDARY_TEXT)
        footer.append("LEFT/RIGHT toggle  ", style=_C_SECONDARY_TEXT)
        footer.append("Enter submit", style=_C_NEON_PINK)
        if self._awaiting_feedback:
            footer.append("\nAwaiting denial feedback", style=_C_NEON_PINK)
            if self._pending_task_id:
                footer.append(f" for task {self._pending_task_id}", style=_C_NEON_PINK)

        body = Group(table, footer)
        return Panel(
            body,
            title="[bold #ff2d6f]// TASK REVIEW[/bold #ff2d6f]",
            border_style=_C_BORDER,
            padding=(1, 1),
        )

    # ------------------------------------------------------------------ internals

    def _denied_task_ids(self) -> List[str]:
        return [task["id"] for task, ok in zip(self._tasks, self._decisions) if not ok]

    def _toggle_selected(self) -> None:
        if not self._tasks:
            return
        self._decisions[self._selected_idx] = not self._decisions[self._selected_idx]

    def _start_feedback_collection(self, denied_ids: List[str]) -> bool:
        if not denied_ids:
            return False
        self._active_denials = list(denied_ids)
        self._pending_denials = list(denied_ids)
        return self._open_next_feedback_overlay()

    def _open_next_feedback_overlay(self) -> bool:
        if not self._pending_denials:
            return False
        target_id = self._pending_denials[0]
        target_idx = self._find_task_index(target_id)
        if target_idx is None:
            self._pending_denials.pop(0)
            return self._open_next_feedback_overlay()
        target_task = self._tasks[target_idx]
        context_id = self._context_id_for(target_id)
        initial = self._feedback_for(target_id)
        description = target_task.get("description", "") if self._tasks else ""
        prompt = _TASK_DENIAL_PROMPT_TEMPLATE.format(
            task_id=target_id,
            description=self._truncate(description, limit=280),
        )

        if self._emitter is not None:
            self._emitter.open_multiline_input(
                id=context_id,
                mode=MultilineInputMode.TASK_DENIAL_FEEDBACK,
                title=_TASK_DENIAL_TITLE,
                prompt=prompt,
                initial_text=initial or None,
                source_stage=self._source_stage,
            )
            self._pending_context_id = context_id
            self._pending_task_id = target_id
            self._awaiting_feedback = True
            return True

        # No emitter available; cannot open multiline overlay.
        self._clear_pending()
        return False

    def _ordered_denials(self, denied_ids: List[str]) -> List[str]:
        if not denied_ids:
            return []
        selected_task_id = self._tasks[self._selected_idx]["id"] if self._tasks else ""
        if selected_task_id in denied_ids:
            return [selected_task_id] + [task_id for task_id in denied_ids if task_id != selected_task_id]
        return list(denied_ids)

    @staticmethod
    def _normalize_key(key: str) -> str:
        if key in ("\r", "\n"):
            return "enter"
        lowered = key.lower()
        if lowered == " ":
            return " "
        return lowered

    def _matches_pending(self, context_id: str, mode: MultilineInputMode | Any) -> bool:
        try:
            expected_mode = MultilineInputMode(mode)
        except Exception:
            return False
        return context_id == self._pending_context_id and expected_mode == MultilineInputMode.TASK_DENIAL_FEEDBACK

    def _find_task_index(self, task_id: str) -> int | None:
        for idx, task in enumerate(self._tasks):
            if task.get("id") == task_id:
                return idx
        return None

    def _context_id_for(self, task_id: str) -> str:
        return f"{_TASK_DENIAL_CONTEXT_PREFIX}.{task_id}"

    def _normalize_feedback(self, feedback_map: Dict[str, str]) -> Dict[str, str]:
        normalized: Dict[str, str] = {}
        for key, value in feedback_map.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            context_key = key if key.startswith(f"{_TASK_DENIAL_CONTEXT_PREFIX}.") else self._context_id_for(key)
            normalized[context_key] = value
        return normalized

    def _feedback_for(self, task_id: str) -> str:
        context_key = self._context_id_for(task_id)
        return self._feedback_by_context.get(context_key, "")

    def _set_feedback(self, task_id: str, feedback: str) -> None:
        context_key = self._context_id_for(task_id)
        self._feedback_by_context[context_key] = feedback

    def _has_feedback(self, task_id: str) -> bool:
        return bool(self._feedback_for(task_id).strip())

    def _clear_pending(self) -> None:
        self._pending_context_id = None
        self._pending_task_id = None
        self._active_denials = []
        self._pending_denials = []

    def _combined_feedback(self, task_ids: Sequence[str]) -> str:
        entries: List[str] = []
        for task_id in task_ids:
            feedback = self._feedback_for(task_id).strip()
            if feedback:
                entries.append(f"[{task_id}] {feedback}")
        return "\n".join(entries).strip()

    @staticmethod
    def _truncate(text: str, *, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."
