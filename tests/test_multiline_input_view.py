from __future__ import annotations

from rich.console import Console

from ain.models.state import MultilineInputMode
from ain.ui.views.multiline_input_view import MultilineInputView


def test_feature_description_body_is_capped_at_ten_and_scrolls() -> None:
    content = "\n".join(f"line {i}" for i in range(1, 15))
    view = MultilineInputView(
        title="Describe the feature or bug",
        prompt="Prompt",
        mode=MultilineInputMode.FEATURE_DESCRIPTION,
        initial_text=content,
        body_height=20,
    )

    visible = list(view._visible_lines())  # noqa: SLF001 - targeted view-level behavior check
    assert len(visible) == 10
    assert "line 5" in visible[0].plain
    assert "line 14" in visible[-1].plain

    for _ in range(13):
        view.handle_key("up")

    visible = list(view._visible_lines())  # noqa: SLF001 - targeted view-level behavior check
    assert len(visible) == 10
    assert "line 1" in visible[0].plain
    assert "line 10" in visible[-1].plain


def test_feature_description_viewport_renders_full_ten_rows_when_buffer_is_short() -> None:
    view = MultilineInputView(
        title="Describe the feature or bug",
        prompt="Prompt",
        mode=MultilineInputMode.FEATURE_DESCRIPTION,
        initial_text="",
        body_height=10,
    )

    visible = list(view._visible_lines())  # noqa: SLF001 - targeted view-level behavior check
    assert len(visible) == 10
    assert "1" in visible[0].plain
    assert visible[-1].plain.strip() == ""


def test_feature_description_footer_shows_only_ctrl_enter_submit_keybind() -> None:
    view = MultilineInputView(
        title="Describe the feature or bug",
        prompt="Prompt",
        mode=MultilineInputMode.FEATURE_DESCRIPTION,
    )

    footer = view._render_footer()  # noqa: SLF001 - targeted view-level behavior check
    console = Console(record=True, width=120)
    console.print(footer)
    rendered = console.export_text()

    assert "Insert newline" not in rendered
    assert "Shift+Alt+Enter" not in rendered
    assert "Ctrl+Enter" in rendered
    assert "Esc" not in rendered
