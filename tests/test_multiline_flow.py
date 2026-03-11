from __future__ import annotations

from pathlib import Path

from ain import pipeline
from ain.models.state import MultilineInputMode
from ain.runtime.emitter import Emitter
from ain.runtime.events import OpenMultilineInputEvent
from ain.services import state_service
from ain.ui.views.multiline_input_view import MultilineInputView

FEATURE_TEXT = "Line one\nLine two"
DENIAL_TEXT = "Needs clearer acceptance criteria\nand additional edge cases."


class StubRenderer:
    """Minimal renderer stub that tracks resume/suspend calls."""

    def __init__(self) -> None:
        self.resumed = 0
        self.suspended = 0

    def resume(self) -> None:
        self.resumed += 1

    def suspend(self) -> None:
        self.suspended += 1


class MultilineTestDriver:
    """Replies to OpenMultilineInput events using the real view key handling."""

    def __init__(self, emitter: Emitter) -> None:
        self.emitter = emitter
        self.open_events: list[OpenMultilineInputEvent] = []
        emitter.subscribe(self.handle)

    def handle(self, event: object) -> None:
        if not isinstance(event, OpenMultilineInputEvent):
            return

        self.open_events.append(event)
        text = FEATURE_TEXT if event.mode == MultilineInputMode.FEATURE_DESCRIPTION else DENIAL_TEXT
        submit_via_shift_alt = event.mode == MultilineInputMode.FEATURE_DESCRIPTION

        view = MultilineInputView(
            id=event.id,
            title=event.title,
            prompt=event.prompt,
            initial_text=event.initial_text or "",
            mode=event.mode,
            source_stage=event.source_stage,
        )
        self._type_text(view, text)
        result = (
            view.handle_key("enter", alt=True, shift=True)
            if submit_via_shift_alt
            else view.handle_key("enter", ctrl=True)
        )
        assert result.is_submit
        self.emitter.submit_multiline_input(id=event.id, mode=event.mode, value=result.value or "")

    @staticmethod
    def _type_text(view: MultilineInputView, text: str) -> None:
        lines = text.split("\n")
        for idx, line in enumerate(lines):
            for ch in line:
                view.handle_key(ch)
            if idx < len(lines) - 1:
                view.handle_key("enter")


def _configure_pipeline_paths(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    pipeline_dir = repo_root / ".ai-pipeline"
    docs_dir = repo_root / "docs"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    replacements = {
        "REPO_ROOT": repo_root,
        "PIPELINE_DIR": pipeline_dir,
        "STATE_FILE": pipeline_dir / "state.json",
        "CONFIG_FILE": pipeline_dir / "config.json",
        "SCAN_DIR": pipeline_dir / "scan",
        "PROMPTS_DIR": pipeline_dir / "prompts",
        "LOGS_DIR": pipeline_dir / "logs",
        "APPROVALS_DIR": pipeline_dir / "approvals",
        "USER_CONTEXT_FILE": pipeline_dir / "user_context.md",
        "BRAINSTORM_CONTEXT_FILE": pipeline_dir / "brainstorm_context.md",
        "TASK_REVIEW_FEEDBACK_FILE": pipeline_dir / "task_review_feedback.md",
        "DOCS_DIR": docs_dir,
        "ARCHITECTURE_FILE": docs_dir / "architecture.md",
        "OPEN_QUESTIONS_FILE": docs_dir / "OPEN_QUESTIONS.md",
        "OPEN_ANSWERS_FILE": docs_dir / "OPEN_ANSWERS.md",
        "PRD_FILE": docs_dir / "PRD.md",
        "DESIGN_FILE": docs_dir / "DESIGN.md",
        "FEATURE_SPEC_FILE": docs_dir / "FEATURE_SPEC.md",
        "TASKS_FILE": docs_dir / "TASKS.md",
        "TASK_GRAPH_FILE": docs_dir / "TASK_GRAPH.json",
        "IMPLEMENTATION_LOG_FILE": docs_dir / "IMPLEMENTATION_LOG.md",
        "PIPELINE_LOG": pipeline_dir / "pipeline.log",
        "NOTIFICATIONS_LOG": pipeline_dir / "notifications.log",
        "PLANNING_APPROVED_FLAG": pipeline_dir / "approvals" / "planning_approved.flag",
        "REPO_TREE_FILE": pipeline_dir / "scan" / "repo_tree.txt",
        "TRACKED_FILES_FILE": pipeline_dir / "scan" / "tracked_files.txt",
        "REPO_SUMMARY_FILE": pipeline_dir / "scan" / "repo_summary.md",
    }

    for name, value in replacements.items():
        monkeypatch.setattr(pipeline, name, value)

    monkeypatch.setattr(pipeline, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "warn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "ensure_config", lambda: None)
    monkeypatch.setattr(pipeline, "load_config", lambda: {})
    monkeypatch.setattr(pipeline, "save_config", lambda *_args, **_kwargs: None)


def test_multiline_planning_and_approval_flow(tmp_path, monkeypatch):
    _configure_pipeline_paths(tmp_path, monkeypatch)

    emitter = Emitter()
    driver = MultilineTestDriver(emitter)
    renderer = StubRenderer()
    monkeypatch.setattr(pipeline, "_EMITTER", emitter)
    monkeypatch.setattr(pipeline, "_RENDERER", renderer)

    start_calls: list[str] = []
    complete_calls: list[tuple[str, MultilineInputMode, str]] = []

    real_start = state_service.start_multiline_input
    real_complete = state_service.complete_multiline_input

    def start_spy(state, ctx, *, state_path):
        start_calls.append(ctx.id)
        return real_start(state, ctx, state_path=state_path)

    def complete_spy(state, value, mode, context_id, *, state_path):
        complete_calls.append((context_id, mode, value))
        return real_complete(state, value, mode, context_id, state_path=state_path)

    monkeypatch.setattr(state_service, "start_multiline_input", start_spy)
    monkeypatch.setattr(state_service, "complete_multiline_input", complete_spy)

    state = {
        "current_stage": "planning_generation",
        "completed_stages": [],
        "started_at": None,
        "last_updated": None,
    }

    description = pipeline._ensure_feature_description(state, source_stage="planning_generation")

    assert description == FEATURE_TEXT
    service_state = state_service.load_state(state_path=pipeline.STATE_FILE)
    assert service_state.feature_description == FEATURE_TEXT
    assert service_state.multiline_input is None
    assert pipeline.USER_CONTEXT_FILE.read_text(encoding="utf-8") == FEATURE_TEXT
    assert pipeline._FEATURE_DESCRIPTION_CONTEXT_ID in start_calls
    assert "approval.task_denial.42" not in start_calls  # ensure denial handled separately later

    feedback = pipeline._collect_task_denial_feedback("42", "Add audit logging", source_stage="waiting_approval")

    assert feedback == DENIAL_TEXT
    assert "approval.task_denial.42" in start_calls
    assert len(driver.open_events) == 2
    service_state = state_service.load_state(state_path=pipeline.STATE_FILE)
    feedback_key = "approval.task_denial.42"
    assert service_state.task_denial_feedback_by_task_id[feedback_key] == DENIAL_TEXT
    assert service_state.multiline_input is None
    assert renderer.resumed == 1
    assert renderer.suspended == 1

    assert any(mode == MultilineInputMode.FEATURE_DESCRIPTION for _, mode, _ in complete_calls)
    assert any(mode == MultilineInputMode.TASK_DENIAL_FEEDBACK for _, mode, _ in complete_calls)


def test_user_context_stage_uses_multiline_tui_flow(tmp_path, monkeypatch):
    _configure_pipeline_paths(tmp_path, monkeypatch)

    emitter = Emitter()
    driver = MultilineTestDriver(emitter)
    monkeypatch.setattr(pipeline, "_EMITTER", emitter)
    monkeypatch.setattr(pipeline, "_RENDERER", StubRenderer())
    # If this gets called, the stage regressed to the legacy inline collector path.
    monkeypatch.setattr(
        pipeline,
        "_collect_multiline_input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("Legacy inline collector should not be used")),
    )

    transitions: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "set_stage",
        lambda stage, state=None: transitions.append(stage) or (state or {}),
    )

    state = {
        "current_stage": "user_context",
        "completed_stages": [],
        "started_at": None,
        "last_updated": None,
    }

    pipeline.run_user_context(state, {})

    assert pipeline.USER_CONTEXT_FILE.read_text(encoding="utf-8") == FEATURE_TEXT
    assert transitions == ["planning_questions"]
    assert len(driver.open_events) == 1
    assert driver.open_events[0].mode == MultilineInputMode.FEATURE_DESCRIPTION
    assert driver.open_events[0].source_stage == "user_context"
