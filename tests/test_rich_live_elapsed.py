from __future__ import annotations

from ain.runtime.events import RunCompleted, RunStarted, RunStatus
from ain.ui.renderers.rich_live import RichLiveRenderer


def test_new_run_clears_previous_end_time() -> None:
    renderer = RichLiveRenderer(enable_keyboard=False)

    renderer._apply_event(  # noqa: SLF001 - intentional test of renderer internals
        RunStarted(run_id="run-1", started_at="2026-03-11T08:00:00Z", mode="rich")
    )
    renderer._apply_event(  # noqa: SLF001 - intentional test of renderer internals
        RunCompleted(run_id="run-1", ended_at="2026-03-11T08:10:00Z", status=RunStatus.DONE)
    )

    assert renderer._state.ended_at is not None  # noqa: SLF001

    renderer._apply_event(  # noqa: SLF001 - intentional test of renderer internals
        RunStarted(run_id="run-2", started_at="2026-03-11T08:11:00Z", mode="rich")
    )

    assert renderer._state.ended_at is None  # noqa: SLF001
    assert not renderer._elapsed_str().startswith("-")  # noqa: SLF001
