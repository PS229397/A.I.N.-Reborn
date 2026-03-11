from __future__ import annotations

import threading
import time

from ain.models.state import MultilineInputMode
from ain.runtime.emitter import Emitter
from ain.ui.renderers.rich_live import RichLiveRenderer, _KeyPress


def test_renderer_request_task_approval_approve_flow() -> None:
    renderer = RichLiveRenderer(enable_keyboard=False)
    result_holder: dict[str, tuple[bool, str]] = {}

    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            renderer.request_task_approval([{"id": "1", "description": "Task one"}]),
        ),
        daemon=True,
    )
    worker.start()

    for _ in range(100):
        if renderer._state.approval_view is not None:  # noqa: SLF001 - intentional UI-path test
            break
        time.sleep(0.01)

    renderer._handle_key(_KeyPress("\r"))  # noqa: SLF001 - intentional UI-path test

    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result_holder.get("result") == (True, "")


def test_renderer_request_task_approval_deny_flow_with_multiline_feedback() -> None:
    emitter = Emitter()
    renderer = RichLiveRenderer(enable_keyboard=False, emitter=emitter)
    emitter.subscribe(renderer.handle)
    result_holder: dict[str, tuple[bool, str]] = {}

    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            renderer.request_task_approval([{"id": "1", "description": "Task one"}]),
        ),
        daemon=True,
    )
    worker.start()

    for _ in range(100):
        if renderer._state.approval_view is not None:  # noqa: SLF001 - intentional UI-path test
            break
        time.sleep(0.01)

    renderer._handle_key(_KeyPress(" "))  # noqa: SLF001 - intentional UI-path test
    renderer._handle_key(_KeyPress("\r"))  # noqa: SLF001 - intentional UI-path test

    multiline_id = ""
    for _ in range(100):
        multiline_view = renderer._state.multiline_view  # noqa: SLF001 - intentional UI-path test
        if multiline_view is not None:
            multiline_id = multiline_view.id
            break
        time.sleep(0.01)

    assert multiline_id
    emitter.submit_multiline_input(
        id=multiline_id,
        mode=MultilineInputMode.TASK_DENIAL_FEEDBACK,
        value="Need to split this task.",
    )

    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result_holder.get("result") == (False, "[1] Need to split this task.")
