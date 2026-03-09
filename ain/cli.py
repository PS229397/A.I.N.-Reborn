"""Click-based CLI entry point for A.I.N. Pipeline."""

from __future__ import annotations

import click

from ain import pipeline
from ain.commands import approve, config, logs, reset, run, status, version


def _warn_deprecated(flag: str, replacement: str) -> None:
    message = (
        f"DeprecationWarning: {flag} is deprecated and will be removed in v2.0.0. "
        f"Use `{replacement}` instead."
    )
    click.echo(message)


@click.group()
@click.option("--reset", "legacy_reset", is_flag=True, hidden=True, help="Deprecated alias for `ain reset`.")
@click.option("--clean", "legacy_clean", is_flag=True, hidden=True, help="Deprecated alias for `ain clean`.")
@click.option("--approve", "legacy_approve", is_flag=True, hidden=True, help="Deprecated alias for `ain approve`.")
@click.option("--status", "legacy_status", is_flag=True, hidden=True, help="Deprecated alias for `ain status`.")
@click.pass_context
def main(
    ctx: click.Context,
    legacy_reset: bool,
    legacy_clean: bool,
    legacy_approve: bool,
    legacy_status: bool,
) -> None:
    """A.I.N. Pipeline command-line interface."""
    if legacy_reset:
        _warn_deprecated("--reset", "ain reset")
        reset.execute(hard=False, yes=False)
        ctx.exit(0)
    if legacy_clean:
        _warn_deprecated("--clean", "ain clean")
        pipeline.clean_workspace()
        ctx.exit(0)
    if legacy_approve:
        _warn_deprecated("--approve", "ain approve")
        approve.execute(run_id=None)
        ctx.exit(0)
    if legacy_status:
        _warn_deprecated("--status", "ain status")
        status.execute()
        ctx.exit(0)


@main.command(name="run")
@click.option("--plain", is_flag=True, help="Disable TUI; print plain output.")
@click.option("--tui", metavar="RENDERER", help="Select renderer: rich or textual.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colors for all output.")
@click.option("--resume", metavar="STAGE", help="Resume from a specific stage.")
@click.option(
    "--health-check-only",
    is_flag=True,
    help="Run environment and state health checks then exit.",
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="Disable pipeline caches for this run when supported by config.",
)
def run_command(
    plain: bool,
    tui: str | None,
    no_color: bool,
    resume: str | None,
    health_check_only: bool,
    no_cache: bool,
) -> None:
    """Run the pipeline with optional renderer and health-check controls."""
    run.execute(
        plain=plain,
        tui=tui,
        no_color=no_color,
        resume=resume,
        health_check_only=health_check_only,
        no_cache=no_cache,
    )


@main.command(name="approve")
@click.option("--run-id", help="Require a specific run id when approving.", default=None)
def approve_command(run_id: str | None) -> None:
    """Approve a pipeline waiting for approval."""
    approve.execute(run_id=run_id)


@main.command(name="reset")
@click.option("--hard", is_flag=True, help="Remove state.json and logs directory.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts for hard reset.")
def reset_command(hard: bool, yes: bool) -> None:
    """Reset pipeline state (soft by default)."""
    reset.execute(hard=hard, yes=yes)


@main.command(name="clean")
def clean_command() -> None:
    """Remove generated pipeline artifacts and reset to idle."""
    pipeline.clean_workspace()


@main.command(name="logs")
@click.option("--follow", is_flag=True, help="Stream logs until interrupted.")
@click.option("--tail", type=int, default=50, show_default=True, help="Show last N lines.")
@click.option("--level", default="info", show_default=True, help="Minimum log level to display.")
@click.option("--source", default=None, help="Filter to a specific log source.")
@click.option("--json", "as_json", is_flag=True, help="Emit logs as JSON lines.")
def logs_command(follow: bool, tail: int, level: str, source: str | None, as_json: bool) -> None:
    """View or stream pipeline logs."""
    logs.execute(follow=follow, tail=tail, level=level, source=source, as_json=as_json)


@main.group(name="config")
def config_command() -> None:
    """Manage project configuration values."""
    # Group handler


@config_command.command(name="list")
def config_list() -> None:
    """List all managed config keys with their origin."""
    config.execute_list()


@config_command.command(name="get")
@click.argument("key")
def config_get(key: str) -> None:
    """Get the effective value of KEY."""
    config.execute_get(key)


@config_command.command(name="set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set KEY to VALUE in the project config."""
    config.execute_set(key, value)


@config_command.command(name="reset")
@click.argument("key", required=False)
def config_reset(key: str | None) -> None:
    """Reset KEY (or all keys) to defaults."""
    config.execute_reset(key)


@main.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Emit PipelineState and HealthSummary as JSON.")
def status_command(as_json: bool) -> None:
    """Display current pipeline status."""
    status.execute(as_json=as_json)


@main.command(name="version")
@click.option("--short", is_flag=True, help="Print version only.")
def version_command(short: bool) -> None:
    """Show the ain CLI version."""
    version.execute(short)


if __name__ == "__main__":
    main()
