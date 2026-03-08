# Tasks

## Phase 1: CLI Foundation

- [ ] register `ain` console_scripts entry in `pyproject.toml`
- [ ] create `ain/cli.py` Click root group with stub commands (`run`, `approve`, `reset`, `logs`, `config`, `status`, `version`)
- [ ] update `ain/__main__.py` to delegate to Click entry point
- [ ] verify `ain --help` and `ain <cmd> --help` work correctly

## Phase 2: Command Implementations

- [ ] implement `ain/commands/run.py` with `--plain`, `--tui`, `--no-color`, `--resume` flags and TTY detection
- [ ] implement `ain/commands/approve.py` with `--run-id` option and non-zero exit when no approvable run exists
- [ ] implement `ain/commands/reset.py` with soft/hard reset logic and `--hard`/`--yes` flags
- [ ] implement `ain/commands/logs.py` with `--follow`, `--tail`, `--level`, `--source`, `--json` flags
- [ ] implement `ain/commands/config.py` with `list`, `get`, `set`, `reset` subcommands
- [ ] implement `ain/commands/status.py` to display persisted pipeline state summary
- [ ] implement `ain/commands/version.py` with `--short` flag and commit hash display
- [ ] implement `ain/services/config_service.py` with layered config resolution (`defaults < ~/.ainrc < .ai-pipeline/config.json`)
- [ ] implement `ain/services/log_service.py` merging `pipeline.log`, `validation.log`, and agent logs by timestamp

## Phase 3: TUI + Event Bus

- [ ] define typed event dataclasses in `ain/runtime/events.py` (`RunStarted`, `StageQueued`, `StageStarted`, `StageCompleted`, `StageFailed`, `AwaitingApproval`, `ApprovalReceived`, `LogLine`, `RunCompleted`)
- [ ] implement emitter interface in `ain/runtime/emitter.py`
- [ ] instrument `ain/pipeline.py` to emit stage/log/approval events via emitter
- [ ] implement Rich Live renderer in `ain/ui/renderers/rich_live.py` with status bar, pipeline panel, stream panel, and keybar
- [ ] add keybindings (`q`, `r`, `l`, `c`, `s`, `?`, `f`, arrows, `a`) to Rich Live renderer
- [ ] implement plain renderer in `ain/ui/renderers/plain.py` for non-TTY and `--plain` mode
- [ ] implement optional Textual renderer adapter in `ain/ui/renderers/textual_app.py` gated behind `--tui textual`
- [ ] add first-run onboarding hint with `first_run` flag persistence in project config

## Phase 4: Migration + Docs + Hardening

- [ ] add legacy flag alias mapping in CLI root (`ain --reset`, `ain --approve`, `ain --status`)
- [ ] emit deprecation warnings with replacement command and `v2.0.0` removal target for all legacy aliases
- [ ] add SIGINT handling with active-run confirmation
- [ ] add unit tests for CLI parser and option validation in `tests/`
- [ ] add unit tests for config precedence and persistence in `tests/`
- [ ] add unit tests for log normalization, filtering, and merge behavior in `tests/`
- [ ] add unit tests for deprecation alias mapping in `tests/`
- [ ] add integration tests for `ain run` TTY/non-TTY mode selection in `tests/`
- [ ] add integration tests for `ain reset` soft vs hard filesystem effects in `tests/`
- [ ] add integration tests for `ain approve` state transition in `tests/`
- [ ] add snapshot/golden tests for `--help` outputs of all commands in `tests/`
- [ ] add snapshot/golden tests for plain renderer output format in `tests/`
- [ ] rewrite README command section to verb-first grammar with migration guide
- [ ] document alias removal target (`v2.0.0`) in README migration section
- [ ] write operator quick reference for `ain logs`, `ain config`, and `ain reset` behavior in docs
