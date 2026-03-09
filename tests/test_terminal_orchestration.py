from __future__ import annotations

from ain import pipeline


def test_open_warp_tab_uses_existing_warp_window_when_running(monkeypatch):
    popen_calls: list[list[str]] = []

    def fake_popen(args, **_kwargs):
        popen_calls.append(args if isinstance(args, list) else [args])
        return None

    monkeypatch.setattr(pipeline.shutil, "which", lambda exe: "warp" if exe == "warp" else None)
    monkeypatch.setattr(pipeline, "is_warp_running", lambda: True)
    monkeypatch.setattr(pipeline.subprocess, "Popen", fake_popen)

    result = pipeline.open_warp_tab("A.I.N.", "echo hello")

    assert result["success"] is True
    assert result["mode"] == "warp"
    assert "existing Warp window" in result["details"]
    assert popen_calls == [["warp", "new-tab", "--title", "A.I.N.", "--command", "echo hello"]]


def test_open_warp_tab_launches_warp_when_not_running(monkeypatch):
    popen_calls: list[list[str]] = []

    def fake_popen(args, **_kwargs):
        popen_calls.append(args if isinstance(args, list) else [args])
        return None

    monkeypatch.setattr(pipeline.shutil, "which", lambda exe: "warp" if exe == "warp" else None)
    monkeypatch.setattr(pipeline, "is_warp_running", lambda: False)
    monkeypatch.setattr(pipeline.subprocess, "Popen", fake_popen)

    result = pipeline.open_warp_tab("A.I.N.", "echo hello")

    assert result["success"] is True
    assert result["mode"] == "warp"
    assert "new Warp launch" in result["details"]
    assert popen_calls == [
        ["warp"],
        ["warp", "new-tab", "--title", "A.I.N.", "--command", "echo hello"],
    ]


def test_open_preferred_terminal_tab_falls_back_when_warp_fails(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "open_warp_tab",
        lambda _title, _command: {
            "success": False,
            "mode": "warp",
            "details": "Warp CLI not found on PATH.",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "open_fallback_terminal",
        lambda _title, _command: {
            "success": True,
            "mode": "fallback_terminal",
            "details": "Opened fallback terminal window with title 'A.I.N.'.",
        },
    )

    result = pipeline.open_preferred_terminal_tab("A.I.N.", "echo hello")

    assert result["success"] is True
    assert result["mode"] == "fallback_terminal"
    assert "Warp tab launch failed: Warp CLI not found on PATH." in result["details"]
    assert "Opened fallback terminal window with title 'A.I.N.'." in result["details"]
