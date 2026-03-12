from __future__ import annotations

from rich.console import Console

from ain.models.state import MultilineInputMode
from ain.ui.views.multiline_input_view import MultilineInputView


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
