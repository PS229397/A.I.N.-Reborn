from __future__ import annotations

from ain import pipeline


class _StubApprovalRenderer:
    def __init__(self, approved: bool, feedback: str) -> None:
        self._approved = approved
        self._feedback = feedback
        self.calls: list[list[dict[str, str]]] = []

    def request_task_approval(self, tasks: list[dict[str, str]]) -> tuple[bool, str]:
        self.calls.append(tasks)
        return self._approved, self._feedback


class _StubInputRenderer:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses

    def request_input(self, _prompt: str) -> str:
        if not self._responses:
            return ""
        return self._responses.pop(0)


def test_review_tasks_with_popup_uses_task_approval_overlay_api(monkeypatch) -> None:
    renderer = _StubApprovalRenderer(
        approved=False,
        feedback="Need to split task 1 into smaller chunks.",
    )
    monkeypatch.setattr(pipeline, "_RENDERER", renderer)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)

    approved, feedback = pipeline._review_tasks_with_popup([{"id": 1, "description": "Task one"}])

    assert approved is False
    assert feedback == "Need to split task 1 into smaller chunks."
    assert renderer.calls == [[{"id": "1", "description": "Task one"}]]


def test_review_tasks_with_popup_falls_back_to_input_prompt_for_approve(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "_RENDERER", _StubInputRenderer(["approve"]))
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)

    approved, feedback = pipeline._review_tasks_with_popup([{"id": "1", "description": "Task one"}])

    assert approved is True
    assert feedback == ""


def test_review_tasks_with_popup_falls_back_to_input_prompt_for_deny(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "_RENDERER", _StubInputRenderer(["deny"]))
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "_collect_task_denial_feedback",
        lambda *_args, **_kwargs: "Need to split task 1 into smaller chunks.",
    )

    approved, feedback = pipeline._review_tasks_with_popup([{"id": "1", "description": "Task one"}])

    assert approved is False
    assert feedback == "Need to split task 1 into smaller chunks."
