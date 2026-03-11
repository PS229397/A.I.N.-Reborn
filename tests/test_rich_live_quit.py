from __future__ import annotations

from ain.ui.renderers.rich_live import RichLiveRenderer, _KeyPress


def test_quit_confirm_y_triggers_normal_quit_callback() -> None:
    calls: list[str] = []
    renderer = RichLiveRenderer(enable_keyboard=False, on_quit=lambda: calls.append("quit"))
    renderer._state.quit_confirm = True  # noqa: SLF001 - intentional internal state setup

    renderer._dispatch_key(_KeyPress("y"))  # noqa: SLF001 - intentional keypath test

    assert calls == ["quit"]
    assert renderer._state.quit_confirm is False  # noqa: SLF001


def test_quit_confirm_c_triggers_clean_quit_callback() -> None:
    calls: list[str] = []
    renderer = RichLiveRenderer(
        enable_keyboard=False,
        on_quit=lambda: calls.append("quit"),
        on_quit_clean=lambda: calls.append("clean"),
    )
    renderer._state.quit_confirm = True  # noqa: SLF001 - intentional internal state setup

    renderer._dispatch_key(_KeyPress("c"))  # noqa: SLF001 - intentional keypath test

    assert calls == ["clean"]
    assert renderer._state.quit_confirm is False  # noqa: SLF001
