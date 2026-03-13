"""CLI entry point compatible with Click's testing helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ain import pipeline


@click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def main(args: tuple[str, ...]) -> None:
    def _fallback_refresh(repo_root: Path) -> None:
        """Minimal rebinding for older pipeline builds that lack refresh_runtime_paths."""
        root = repo_root
        pipeline.REPO_ROOT = root
        pipeline.PIPELINE_DIR = root / ".ai-pipeline"
        pipeline.DOCS_DIR = root / "docs"
        pipeline.STATE_FILE = pipeline.PIPELINE_DIR / "state.json"
        pipeline.CONFIG_FILE = pipeline.PIPELINE_DIR / "config.json"
        pipeline.SCAN_DIR = pipeline.PIPELINE_DIR / "scan"
        pipeline.PROMPTS_DIR = pipeline.PIPELINE_DIR / "prompts"
        pipeline.LOGS_DIR = pipeline.PIPELINE_DIR / "logs"
        pipeline.APPROVALS_DIR = pipeline.PIPELINE_DIR / "approvals"
        pipeline.USER_CONTEXT_FILE = pipeline.DOCS_DIR / "user_context.md"
        pipeline.BRAINSTORM_CONTEXT_FILE = pipeline.DOCS_DIR / "brainstorm_context.md"
        pipeline.TASK_REVIEW_FEEDBACK_FILE = pipeline.DOCS_DIR / "task_review_feedback.md"
        pipeline.PIPELINE_LOG = pipeline.LOGS_DIR / "pipeline.log"
        pipeline.NOTIFICATIONS_LOG = pipeline.LOGS_DIR / "notifications.log"
        pipeline.GITIGNORE_FILE = root / ".gitignore"
        pipeline.REPO_TREE_FILE = pipeline.SCAN_DIR / "repo_tree.txt"
        pipeline.TRACKED_FILES_FILE = pipeline.SCAN_DIR / "tracked_files.txt"
        pipeline.REPO_SUMMARY_FILE = pipeline.SCAN_DIR / "repo_summary.md"
        pipeline.ARCHITECTURE_FILE = pipeline.DOCS_DIR / "architecture.md"
        pipeline.OPEN_QUESTIONS_FILE = pipeline.DOCS_DIR / "OPEN_QUESTIONS.md"
        pipeline.OPEN_ANSWERS_FILE = pipeline.DOCS_DIR / "OPEN_ANSWERS.md"
        pipeline.PRD_FILE = pipeline.DOCS_DIR / "PRD.md"
        pipeline.DESIGN_FILE = pipeline.DOCS_DIR / "DESIGN.md"
        pipeline.FEATURE_SPEC_FILE = pipeline.DOCS_DIR / "FEATURE_SPEC.md"
        pipeline.TASKS_FILE = pipeline.DOCS_DIR / "TASKS.md"
        pipeline.TASK_GRAPH_FILE = pipeline.DOCS_DIR / "TASK_GRAPH.json"
        pipeline.IMPLEMENTATION_LOG_FILE = pipeline.DOCS_DIR / "IMPLEMENTATION_LOG.md"
        pipeline.PLANNING_APPROVED_FLAG = pipeline.APPROVALS_DIR / "planning_approved.flag"

    original_argv = sys.argv[:]
    try:
        sys.argv = ["ain", *args]
        if hasattr(pipeline, "refresh_runtime_paths"):
            pipeline.refresh_runtime_paths(Path.cwd())
        else:
            _fallback_refresh(Path.cwd())
        pipeline.main()
    finally:
        sys.argv = original_argv


__all__ = ["main"]
