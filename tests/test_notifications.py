from __future__ import annotations

import json

from ain import pipeline


def _configure_notification_paths(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / ".ai-pipeline"
    state_file = pipeline_dir / "state.json"
    logs_dir = pipeline_dir / "logs"
    notifications_log = logs_dir / "notifications.log"

    monkeypatch.setattr(pipeline, "PIPELINE_DIR", pipeline_dir)
    monkeypatch.setattr(pipeline, "STATE_FILE", state_file)
    monkeypatch.setattr(pipeline, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(pipeline, "NOTIFICATIONS_LOG", notifications_log)
    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "error", lambda *_args, **_kwargs: None)

    return state_file, notifications_log


def test_notify_creates_notification_tab_and_returns_payload(monkeypatch, tmp_path):
    state_file, notifications_log = _configure_notification_paths(monkeypatch, tmp_path)
    open_calls: list[tuple[str, str]] = []

    def fake_open_preferred_terminal_tab(title, command):
        open_calls.append((title, command))
        return {
            "success": True,
            "mode": "warp",
            "details": "Opened tab in existing Warp window.",
        }

    monkeypatch.setattr(
        pipeline,
        "open_preferred_terminal_tab",
        fake_open_preferred_terminal_tab,
    )

    result = pipeline.notify("INFO", "Stage started", "ain continue")

    assert result["success"] is True
    assert result["level"] == "info"
    assert result["summary"] == "Stage started"
    assert result["hint"] == "ain continue"
    assert result["channel_launch"]["success"] is True
    assert result["channel_launch"]["mode"] == "warp"
    assert open_calls[0][0] == pipeline.NOTIFICATIONS_TAB_TITLE
    assert str(notifications_log) in open_calls[0][1]

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    channel = persisted["notification_channel"]
    assert channel["active"] is True
    assert channel["title"] == pipeline.NOTIFICATIONS_TAB_TITLE
    assert channel["mode"] == "warp"
    assert channel["log_path"] == str(notifications_log)
    assert channel["last_level"] == "info"
    assert channel["created_at"]
    assert channel["last_notified_at"]

    contents = notifications_log.read_text(encoding="utf-8")
    assert "INFO: Stage started" in contents
    assert "HINT: ain continue" in contents


def test_notify_reuses_existing_reachable_notification_tab(monkeypatch, tmp_path):
    state_file, notifications_log = _configure_notification_paths(monkeypatch, tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "current_stage": "implementation",
                "completed_stages": [],
                "notification_channel": {
                    "active": True,
                    "title": pipeline.NOTIFICATIONS_TAB_TITLE,
                    "mode": "fallback_terminal",
                    "details": "Existing fallback terminal channel.",
                    "log_path": str(notifications_log),
                    "created_at": "2026-03-01T10:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    open_calls = 0

    def fake_open_preferred_terminal_tab(_title, _command):
        nonlocal open_calls
        open_calls += 1
        return {"success": True, "mode": "warp", "details": "Unexpected launch."}

    monkeypatch.setattr(
        pipeline,
        "open_preferred_terminal_tab",
        fake_open_preferred_terminal_tab,
    )

    result = pipeline.notify("warning", "Waiting for approval")

    assert open_calls == 0
    assert result["channel_launch"]["success"] is True
    assert result["channel_launch"]["mode"] == "fallback_terminal"
    assert "Reusing existing notification channel." in result["channel_launch"]["details"]

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    channel = persisted["notification_channel"]
    assert channel["active"] is True
    assert channel["mode"] == "fallback_terminal"
    assert channel["last_level"] == "warning"
    assert channel["last_notified_at"]


def test_notify_recreates_notification_tab_when_previous_channel_unreachable(monkeypatch, tmp_path):
    state_file, notifications_log = _configure_notification_paths(monkeypatch, tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "current_stage": "implementation",
                "completed_stages": [],
                "notification_channel": {
                    "active": False,
                    "title": pipeline.NOTIFICATIONS_TAB_TITLE,
                    "mode": "fallback_terminal",
                    "details": "Closed terminal tab.",
                    "log_path": str(notifications_log),
                    "created_at": "2026-03-01T10:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    open_calls: list[tuple[str, str]] = []

    def fake_open_preferred_terminal_tab(title, command):
        open_calls.append((title, command))
        return {
            "success": True,
            "mode": "fallback_terminal",
            "details": "Opened fallback terminal window.",
        }

    monkeypatch.setattr(
        pipeline,
        "open_preferred_terminal_tab",
        fake_open_preferred_terminal_tab,
    )

    result = pipeline.notify("error", "Unrecoverable failure", "See logs")

    assert len(open_calls) == 1
    assert result["level"] == "error"
    assert result["channel_launch"]["success"] is True
    assert result["channel_launch"]["mode"] == "fallback_terminal"
    assert result["channel_launch"]["details"].startswith("Recreated notification channel.")

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    channel = persisted["notification_channel"]
    assert channel["active"] is True
    assert channel["mode"] == "fallback_terminal"
    assert channel["last_level"] == "error"
    assert channel["last_notified_at"]
