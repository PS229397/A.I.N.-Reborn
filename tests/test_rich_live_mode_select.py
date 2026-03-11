from __future__ import annotations

import threading
import time

from ain.ui.renderers.rich_live import RichLiveRenderer, _KeyPress
from ain.ui.views.mode_select_view import ModeSelectView


def test_mode_select_view_returns_selected_mode_on_enter() -> None:
    view = ModeSelectView(
        [
            {"key": "default", "label": "Default", "summary": "A"},
            {"key": "codex_only", "label": "Codex Only", "summary": "B"},
        ],
        current_mode="default",
    )
    view.handle_key("down")
    result = view.handle_key("enter")
    assert result.is_select
    assert result.mode == "codex_only"


def test_renderer_request_mode_selection_fullscreen_flow() -> None:
    renderer = RichLiveRenderer(enable_keyboard=False)
    result_holder: dict[str, str] = {}

    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "mode",
            renderer.request_mode_selection(
                [
                    {"key": "default", "label": "Default", "summary": "A"},
                    {"key": "codex_only", "label": "Codex Only", "summary": "B"},
                ],
                "default",
            ),
        ),
        daemon=True,
    )
    worker.start()

    for _ in range(100):
        if renderer._state.mode_select_view is not None:  # noqa: SLF001 - intentional UI-path test
            break
        time.sleep(0.01)

    renderer._handle_key(_KeyPress("down"))  # noqa: SLF001 - intentional UI-path test
    renderer._handle_key(_KeyPress("\r"))  # noqa: SLF001 - intentional UI-path test

    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result_holder.get("mode") == "codex_only"
