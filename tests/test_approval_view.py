from __future__ import annotations

from dataclasses import dataclass

from ain.models.state import MultilineInputMode
from ain.runtime.events import SubmitMultilineInputEvent
from ain.ui.views.approval_view import ApprovalView


@dataclass
class _OpenCall:
    id: str
    title: str
    prompt: str
    initial_text: str | None
    source_stage: str


class _StubEmitter:
    def __init__(self) -> None:
        self.calls: list[_OpenCall] = []

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
        assert mode is MultilineInputMode.TASK_DENIAL_FEEDBACK
        self.calls.append(
            _OpenCall(
                id=id,
                title=title,
                prompt=prompt,
                initial_text=initial_text,
                source_stage=source_stage,
            )
        )


def test_approval_view_collects_feedback_for_all_denied_tasks() -> None:
    emitter = _StubEmitter()
    view = ApprovalView(
        [
            {"id": "1", "description": "Task one"},
            {"id": "2", "description": "Task two"},
        ],
        emitter=emitter,
    )

    view.handle_key(" ")  # deny task 1
    view.handle_key("down")
    view.handle_key(" ")  # deny task 2
    view.handle_key("up")
    start = view.handle_key("enter")

    assert start.is_waiting_feedback
    assert len(emitter.calls) == 1
    first_context = emitter.calls[0].id

    first_submit = view.handle_event(
        SubmitMultilineInputEvent(
            id=first_context,
            mode=MultilineInputMode.TASK_DENIAL_FEEDBACK,
            value="First reason",
        )
    )
    assert first_submit.is_waiting_feedback
    assert len(emitter.calls) == 2
    second_context = emitter.calls[1].id

    final_submit = view.handle_event(
        SubmitMultilineInputEvent(
            id=second_context,
            mode=MultilineInputMode.TASK_DENIAL_FEEDBACK,
            value="Second reason",
        )
    )
    assert final_submit.is_denied
    assert "[1] First reason" in final_submit.feedback
    assert "[2] Second reason" in final_submit.feedback
