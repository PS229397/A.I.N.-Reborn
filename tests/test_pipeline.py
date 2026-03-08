"""Unit tests for ain/pipeline.py helper functions."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Make ain.pipeline importable without the package being installed
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import ain.pipeline as pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# T-040 — test_is_token_limit_error_matches_canonical_string
# ---------------------------------------------------------------------------

class TestIsTokenLimitError:
    def test_matches_canonical_usage_limit(self):
        msg = "Claude usage limit reached. Your limit will reset at 5:00 PM (PST)."
        assert pipeline.is_token_limit_error(msg) is True

    def test_case_insensitive(self):
        msg = "CLAUDE USAGE LIMIT REACHED."
        assert pipeline.is_token_limit_error(msg) is True

    def test_false_for_context_overflow_prompt_too_long(self):
        msg = "Claude usage limit reached. Prompt is too long for this model."
        assert pipeline.is_token_limit_error(msg) is False

    def test_false_for_context_overflow_exceeds_context(self):
        msg = "Claude usage limit reached. Exceeds the maximum context window."
        assert pipeline.is_token_limit_error(msg) is False

    def test_false_for_context_overflow_too_many_tokens(self):
        msg = "Claude usage limit reached. Too many tokens in request."
        assert pipeline.is_token_limit_error(msg) is False

    def test_false_for_unrelated_string(self):
        assert pipeline.is_token_limit_error("Some random error occurred.") is False

    def test_false_for_non_string(self):
        assert pipeline.is_token_limit_error(None) is False  # type: ignore[arg-type]
        assert pipeline.is_token_limit_error(42) is False    # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T-041 — test_rollback_filter_excludes_protected_paths
# ---------------------------------------------------------------------------

class TestRollbackImplementationFiles:
    def _make_state(self, sha: str = "abc123") -> dict:
        state = pipeline.load_state.__wrapped__() if hasattr(pipeline.load_state, "__wrapped__") else {}
        return {
            "current_stage": "implementation",
            "branch": None,
            "started_at": None,
            "last_updated": None,
            "completed_stages": [],
            "fallback": {**pipeline._DEFAULT_FALLBACK, "rollback_commit": sha},
            "prd_import": dict(pipeline._DEFAULT_PRD_IMPORT),
        }

    def test_protected_paths_excluded(self, tmp_path, monkeypatch):
        """rollback_implementation_files must skip docs/, .ai-pipeline/, .git/, CLAUDE.md, .claude/."""
        sha = "deadbeef"
        state = self._make_state(sha)

        changed_files = [
            "docs/PRD.md",
            ".ai-pipeline/config.json",
            ".git/COMMIT_EDITMSG",
            "CLAUDE.md",
            ".claude/settings.json",
            "ain/pipeline.py",       # should be reset
            "README.md",             # should be reset
        ]

        monkeypatch.setattr(pipeline, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(pipeline, "STATE_FILE", tmp_path / ".ai-pipeline" / "state.json")
        monkeypatch.setattr(pipeline, "PIPELINE_DIR", tmp_path / ".ai-pipeline")
        monkeypatch.setattr(pipeline, "LOGS_DIR", tmp_path / ".ai-pipeline" / "logs")

        def fake_save_state(s):
            pass

        monkeypatch.setattr(pipeline, "save_state", fake_save_state)

        diff_result = MagicMock()
        diff_result.stdout = "\n".join(changed_files) + "\n"

        checkout_calls: list = []

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            if "checkout" in cmd:
                checkout_calls.append(cmd)
                mock = MagicMock()
                mock.returncode = 0
                return mock
            return MagicMock(returncode=0)

        import subprocess as _subprocess
        monkeypatch.setattr(_subprocess, "run", fake_run)

        rolled = pipeline.rollback_implementation_files(state)

        assert "docs/PRD.md" not in rolled
        assert ".ai-pipeline/config.json" not in rolled
        assert ".git/COMMIT_EDITMSG" not in rolled
        assert "CLAUDE.md" not in rolled
        assert ".claude/settings.json" not in rolled
        assert "ain/pipeline.py" in rolled
        assert "README.md" in rolled

    def test_no_rollback_commit_returns_empty(self, monkeypatch):
        state = {
            "fallback": {**pipeline._DEFAULT_FALLBACK, "rollback_commit": None},
            "prd_import": dict(pipeline._DEFAULT_PRD_IMPORT),
        }
        monkeypatch.setattr(pipeline, "save_state", lambda s: None)
        result = pipeline.rollback_implementation_files(state)
        assert result == []


# ---------------------------------------------------------------------------
# T-042 — test_validate_prd_import_enforces_min_chars
# ---------------------------------------------------------------------------

class TestValidatePrdImport:
    def _make_state(self) -> dict:
        return {
            "current_stage": "task_creation",
            "prd_import": {"enabled": True, "source": "/tmp/prd.md",
                           "imported_at": None, "files_written": [], "skipped_stages": []},
            "fallback": dict(pipeline._DEFAULT_FALLBACK),
        }

    def test_passes_with_sufficient_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "PRD_FILE", tmp_path / "PRD.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE", tmp_path / "DESIGN.md")
        monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", tmp_path / "FEATURE_SPEC.md")

        (tmp_path / "PRD.md").write_text("x" * 600, encoding="utf-8")
        (tmp_path / "DESIGN.md").write_text("# Design\n\ncontent", encoding="utf-8")
        (tmp_path / "FEATURE_SPEC.md").write_text("# Spec\n\ncontent", encoding="utf-8")

        monkeypatch.setattr(pipeline, "load_config", lambda: {"prd_import": {"min_prd_chars": 500}})

        pipeline.validate_prd_import(self._make_state())  # should not raise

    def test_fails_when_prd_too_short(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "PRD_FILE", tmp_path / "PRD.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE", tmp_path / "DESIGN.md")
        monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", tmp_path / "FEATURE_SPEC.md")

        (tmp_path / "PRD.md").write_text("short", encoding="utf-8")
        (tmp_path / "DESIGN.md").write_text("# Design\n\ncontent", encoding="utf-8")
        (tmp_path / "FEATURE_SPEC.md").write_text("# Spec\n\ncontent", encoding="utf-8")

        monkeypatch.setattr(pipeline, "load_config", lambda: {"prd_import": {"min_prd_chars": 500}})

        with pytest.raises(RuntimeError, match="too short"):
            pipeline.validate_prd_import(self._make_state())

    def test_fails_when_prd_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "PRD_FILE", tmp_path / "PRD.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE", tmp_path / "DESIGN.md")
        monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", tmp_path / "FEATURE_SPEC.md")

        monkeypatch.setattr(pipeline, "load_config", lambda: {"prd_import": {"min_prd_chars": 500}})

        with pytest.raises(RuntimeError, match="not found"):
            pipeline.validate_prd_import(self._make_state())

    def test_fails_when_design_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "PRD_FILE", tmp_path / "PRD.md")
        monkeypatch.setattr(pipeline, "DESIGN_FILE", tmp_path / "DESIGN.md")
        monkeypatch.setattr(pipeline, "FEATURE_SPEC_FILE", tmp_path / "FEATURE_SPEC.md")

        (tmp_path / "PRD.md").write_text("x" * 600, encoding="utf-8")
        (tmp_path / "DESIGN.md").write_text("   ", encoding="utf-8")
        (tmp_path / "FEATURE_SPEC.md").write_text("content", encoding="utf-8")

        monkeypatch.setattr(pipeline, "load_config", lambda: {"prd_import": {"min_prd_chars": 500}})

        with pytest.raises(RuntimeError, match="DESIGN.md"):
            pipeline.validate_prd_import(self._make_state())


# ---------------------------------------------------------------------------
# T-043 — test_status_rendering_includes_new_fields
# ---------------------------------------------------------------------------

class TestStatusRendering:
    def _make_state(self, *, fallback_triggered=False, fallback_agent=None,
                    fallback_stage=None, prd_source=None) -> dict:
        return {
            "current_stage": "implementation",
            "branch": "ai/feature-test",
            "started_at": "2026-01-01T00:00:00+00:00",
            "last_updated": None,
            "completed_stages": ["scanning"],
            "failure_reason": None,
            "fallback": {
                **pipeline._DEFAULT_FALLBACK,
                "triggered":      fallback_triggered,
                "fallback_agent": fallback_agent,
                "fallback_stage": fallback_stage,
            },
            "prd_import": {
                **pipeline._DEFAULT_PRD_IMPORT,
                "source": prd_source,
            },
        }

    def _capture_status(self, state: dict) -> str:
        import io
        from unittest.mock import patch as _patch
        buf = io.StringIO()
        with _patch("sys.stdout", buf):
            pipeline.show_status(state)
        return buf.getvalue()

    def test_mode_field_present(self):
        output = self._capture_status(self._make_state())
        assert "mode" in output.lower() or "Mode" in output

    def test_fallback_active_field_present(self):
        output = self._capture_status(self._make_state(fallback_triggered=True,
                                                        fallback_agent="codex",
                                                        fallback_stage="implementation"))
        assert "fallback" in output.lower()
        assert "codex" in output
        assert "implementation" in output

    def test_prd_import_source_shown(self):
        output = self._capture_status(self._make_state(prd_source="/tmp/my_prd.md"))
        assert "/tmp/my_prd.md" in output

    def test_normal_mode_when_no_fallback(self):
        output = self._capture_status(self._make_state(fallback_triggered=False))
        assert "normal" in output.lower()
