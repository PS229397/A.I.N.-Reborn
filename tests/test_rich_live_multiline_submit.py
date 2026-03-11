from __future__ import annotations

import threading

from ain.runtime.emitter import Emitter
from ain.runtime.events import SubmitMultilineInputEvent
from ain.ui.renderers.rich_live import RichLiveRenderer, _KeyPress
from ain.ui.views.multiline_input_view import MultilineInputView


def test_multiline_submit_does_not_deadlock_renderer_lock() -> None:
    emitter = Emitter()
    renderer = RichLiveRenderer(enable_keyboard=False)
    renderer.attach_emitter(emitter)
    emitter.subscribe(renderer.handle)

    submitted = threading.Event()

    def _watch(event: object) -> None:
        if isinstance(event, SubmitMultilineInputEvent):
            submitted.set()

    emitter.subscribe(_watch)
    renderer._state.multiline_view = MultilineInputView(  # noqa: SLF001 - targeted event-path test
        id="planning.feature_description",
        title="Describe the feature or bug",
        prompt="Prompt",
        initial_text="ready",
    )

    worker = threading.Thread(
        target=lambda: renderer._handle_key(_KeyPress("enter", ctrl=True)),  # noqa: SLF001
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert submitted.wait(timeout=1.0)
