# Tasks

## Runtime & Events
- [ ] implement EventEmitter class in ain/runtime/emitter.py
- [ ] define pipeline event constants in ain/runtime/events.py

## Pipeline Core
- [ ] implement scanning stage logic in ain/pipeline.py
- [ ] implement architecture stage (Gemini agent invocation) in ain/pipeline.py
- [ ] implement planning_questions stage (Codex agent invocation) in ain/pipeline.py
- [ ] implement planning_generation stage (Codex agent invocation) in ain/pipeline.py
- [ ] implement task_creation stage (Claude agent invocation) in ain/pipeline.py
- [ ] implement waiting_approval gate in ain/pipeline.py
- [ ] implement implementation stage (Claude agent invocation) in ain/pipeline.py
- [ ] implement validation stage with auto-detected test runner in ain/pipeline.py
- [ ] implement state persistence to .ai-pipeline/state.json in ain/pipeline.py

## CLI
- [ ] implement `ain run` command in ain/cli.py
- [ ] implement `ain scan` command in ain/cli.py
- [ ] implement `ain plan` command in ain/cli.py
- [ ] implement `ain implement` command in ain/cli.py
- [ ] implement `ain approve` command in ain/cli.py
- [ ] implement `ain status` command in ain/cli.py
- [ ] implement `ain reset` command in ain/cli.py

## TUI
- [ ] implement TUI pipeline stage progress display in ain/tui.py
- [ ] implement TUI live status panel with agent output in ain/tui.py

## Tests
- [ ] add unit test for pipeline state transitions in tests/test_pipeline_state.py
- [ ] add unit test for agent fallback behavior in tests/test_agent_fallback.py
- [ ] add unit test for pipeline pause and resume in tests/test_pipeline_resume.py
- [ ] add integration test for full pipeline orchestration in tests/test_terminal_orchestration.py
