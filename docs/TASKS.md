# Tasks

## Config / Schema

- [ ] add `agent_teams` block (`enabled`, `max_teammates`, `lead_model`, `teammate_model`, `require_verification`) to `ain/data/config.json` default schema
- [ ] add `fallback` block (`enabled`, `trigger_on`, `notification_timeout_secs`, `fallback_agent`, `fallback_prompt_mode`, `protected_paths`, `stages_with_fallback`, `codex_timeout_secs`, `on_codex_limit`) to `ain/data/config.json` default schema
- [ ] add `prd_import` block (`min_prd_chars`, `allowed_extensions`) to `ain/data/config.json` default schema
- [ ] add `fallback` sub-object (`triggered`, `trigger_reason`, `trigger_agent`, `trigger_stage`, `trigger_timestamp`, `user_response`, `auto_switched_at`, `rollback_commit`, `rollback_files`, `fallback_agent`, `fallback_stage`, `fallback_completed`) to `.ai-pipeline/state.json` schema in `ain/pipeline.py`
- [ ] add `prd_import` sub-object (`enabled`, `source`, `imported_at`, `files_written`, `skipped_stages`) to `.ai-pipeline/state.json` schema in `ain/pipeline.py`

## Prompts

- [ ] update `.ai-pipeline/prompts/task_creation_prompt.md` to require `parallel_groups` in `docs/TASK_GRAPH.json` output
- [ ] update `ain/data/prompts/task_creation_prompt.md` (canonical copy) to match, requiring `parallel_groups`
- [ ] update `.ai-pipeline/prompts/implementation_prompt.md` with teammate constraints and `.ai-pipeline/state/<group_id>.flag` completion protocol
- [ ] add `.ai-pipeline/prompts/verification_prompt.md` defining audit behavior and `docs/VERIFICATION_REPORT.md` output contract
- [ ] add `ain/data/prompts/verification_prompt.md` (canonical copy)
- [ ] add `.ai-pipeline/prompts/prd_extraction_prompt.md` for single-file PRD split into `docs/DESIGN.md` and `docs/FEATURE_SPEC.md`
- [ ] add `ain/data/prompts/prd_extraction_prompt.md` (canonical copy)
- [ ] add `.ai-pipeline/prompts/fallback_task_creation_prompt.md` for Codex fallback task-creation stage
- [ ] add `ain/data/prompts/fallback_task_creation_prompt.md` (canonical copy)
- [ ] add `.ai-pipeline/prompts/fallback_implementation_prompt.md` for Codex fallback implementation stage
- [ ] add `ain/data/prompts/fallback_implementation_prompt.md` (canonical copy)
- [ ] ensure `CLAUDE.md` exists; add creation step to `ain init` in `ain/pipeline.py`

## Backend — Helper Functions

- [ ] implement `is_token_limit_error(err_or_output) -> bool` in `ain/pipeline.py` matching canonical usage-limit string and excluding context-overflow class
- [ ] implement `capture_rollback_point(state) -> str` in `ain/pipeline.py` to capture and persist current commit SHA to `state.fallback.rollback_commit`
- [ ] implement `notify_fallback_and_get_decision(context, timeout_secs) -> Literal["wait","switch","abort","auto_switch"]` in `ain/pipeline.py` with countdown and terminal notification
- [ ] implement `rollback_implementation_files(state) -> list[str]` in `ain/pipeline.py` to compute diff since rollback commit, filter protected paths, reset remaining files, persist list to state
- [ ] implement `invoke_codex_fallback(stage, prompt_path, timeout_secs) -> bool` in `ain/pipeline.py` running `codex exec --full-auto --cwd <repo> "<prompt>"`, streaming output, enforcing timeout and completion flag
- [ ] implement `handle_prd_import(import_path, state) -> None` in `ain/pipeline.py` resolving file/directory mode, writing normalized docs, persisting import metadata, setting stage to `task_creation`
- [ ] implement `validate_prd_import(state) -> None` in `ain/pipeline.py` enforcing `docs/PRD.md` >= 500 chars and non-empty `docs/DESIGN.md` + `docs/FEATURE_SPEC.md`
- [ ] implement `run_verification_stage(state) -> bool` in `ain/pipeline.py` executing verification prompt and requiring `.ai-pipeline/approvals/verification.flag`
- [ ] implement `run_parallel_groups(task_graph, config, state) -> RunResult` in `ain/pipeline.py` with dependency-aware scheduler, Windows-native-first worker launch, flag polling, and timeout/error handling

## Backend — Stage Machine

- [ ] extend `implementation` stage in `ain/pipeline.py` to call `run_parallel_groups()` when `parallel_groups` present in `docs/TASK_GRAPH.json`
- [ ] add `verification` stage to stage machine in `ain/pipeline.py` between `implementation` and `validation`, gated by `require_verification` config key
- [ ] wrap Claude/Chief stage invocations in `ain/pipeline.py` with fallback controller that calls `is_token_limit_error()`, `capture_rollback_point()`, `notify_fallback_and_get_decision()`, `rollback_implementation_files()`, and `invoke_codex_fallback()`
- [ ] update stage machine in `ain/pipeline.py` to skip `user_context`, `planning_questions`, `planning_generation` when `prd_import.enabled` is true in state

## CLI

- [ ] add `--prd-import <path>` argument to `main()` in `ain/pipeline.py` supporting file and directory inputs
- [ ] support `--prd-import` combined with `--approve`, `--resume task_creation`, and `--dry-run` flags in `ain/pipeline.py`
- [ ] update `ain --status` output in `ain/pipeline.py` to display `mode`, `fallback_active`, `fallback_agent`, `fallback_stage`, and `prd_import.source` fields

## Artifact Files

- [ ] define `docs/IMPLEMENTATION_LOG.md` format (task-level DONE/FAILED lines) and write initial template in `ain/pipeline.py` at implementation stage start
- [ ] define `docs/VERIFICATION_REPORT.md` format (PASS/FAIL per task + overall verdict) and write it in `run_verification_stage()` in `ain/pipeline.py`
- [ ] create `.ai-pipeline/approvals/` directory and write `verification.flag` in `run_verification_stage()` on VERIFIED result in `ain/pipeline.py`
- [ ] create `.ai-pipeline/state/` directory and write `fallback_complete.flag` in `invoke_codex_fallback()` on Codex success in `ain/pipeline.py`
- [ ] create `.ai-pipeline/state/prd_extraction.flag` in `handle_prd_import()` after single-file extraction completes in `ain/pipeline.py`
- [ ] update `docs/TASK_GRAPH.json` schema to include `parallel_groups[]` array (via `task_creation_prompt.md` and validation in `ain/pipeline.py`)

## Tests — Unit

- [ ] add unit test `test_is_token_limit_error_matches_canonical_string()` in `tests/test_pipeline.py` verifying true for usage-limit message and false for context-overflow message
- [ ] add unit test `test_rollback_filter_excludes_protected_paths()` in `tests/test_pipeline.py` verifying `rollback_implementation_files()` skips `docs/`, `.ai-pipeline/`, `.git/`, `CLAUDE.md`, `.claude/`
- [ ] add unit test `test_validate_prd_import_enforces_min_chars()` in `tests/test_pipeline.py` verifying halt when `docs/PRD.md` < 500 chars or dependent docs empty
- [ ] add unit test `test_status_rendering_includes_new_fields()` in `tests/test_pipeline.py` verifying `ain --status` output contains `mode`, `fallback_active`, `fallback_agent`, `fallback_stage`, `prd_import.source`

## Tests — Integration

- [ ] add integration test `test_parallel_group_scheduling_honors_depends_on()` in `tests/test_integration.py` verifying groups execute in dependency order
- [ ] add integration test `test_verification_gate_blocks_validation_on_failure()` in `tests/test_integration.py` verifying missing/failed verification flag halts before validation
- [ ] add integration test `test_prd_import_file_mode_creates_three_docs()` in `tests/test_integration.py` verifying `--prd-import` file path creates `docs/PRD.md`, `docs/DESIGN.md`, `docs/FEATURE_SPEC.md` and skips planning stages
- [ ] add integration test `test_prd_import_directory_mode_resolves_filenames()` in `tests/test_integration.py` verifying directory import resolves PRD/design/spec files by filename tokens
- [ ] add integration test `test_resume_after_wait_returns_to_correct_stage()` in `tests/test_integration.py` verifying `ain run --resume <stage>` re-enters at saved stage after `wait` decision

## Tests — End-to-End

- [ ] add end-to-end test `test_normal_path_with_agent_teams_and_verification()` in `tests/test_e2e.py` covering full run with `agent_teams.enabled=true` and successful `docs/VERIFICATION_REPORT.md`
- [ ] add end-to-end test `test_simulated_token_limit_in_implementation()` in `tests/test_e2e.py` verifying trigger detected, notification shown, auto-switch after timeout, rollback executed, Codex completion flag written, verification + validation pass
- [ ] add end-to-end test `test_rollback_failure_halts_safely()` in `tests/test_e2e.py` verifying pipeline halts without launching Codex when rollback errors
- [ ] add end-to-end test `test_on_codex_limit_pause_preserves_resumability()` in `tests/test_e2e.py` verifying `on_codex_limit=pause` keeps state intact and allows `--resume`
