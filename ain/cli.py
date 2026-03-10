"""CLI entry point compatible with Click's testing helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ain import pipeline


@click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def main(args: tuple[str, ...]) -> None:
    original_argv = sys.argv[:]
    try:
        sys.argv = ["ain", *args]
        if hasattr(pipeline, "refresh_runtime_paths"):
            pipeline.refresh_runtime_paths(Path.cwd())
        pipeline.main()
    finally:
        sys.argv = original_argv


__all__ = ["main"]
