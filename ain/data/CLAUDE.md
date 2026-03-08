# CLAUDE.md — A.I.N. Pipeline Instructions

This repository is managed by the **A.I.N. Pipeline** (`ain`), a multi-agent AI development orchestrator.

## Pipeline Overview

The pipeline coordinates multiple AI agents through deterministic stages:

1. **Scanning** — repository metadata is collected
2. **Architecture** — Gemini generates `docs/architecture.md`
3. **User Context** — feature requirements are captured
4. **Planning** — Codex produces `docs/PRD.md`, `docs/DESIGN.md`, `docs/FEATURE_SPEC.md`
5. **Task Creation** — Claude generates `docs/TASKS.md` and `docs/TASK_GRAPH.json`
6. **Implementation** — Claude executes tasks from the task graph
7. **Verification** — audit of implemented tasks against the task graph
8. **Validation** — automated tests and linting

## Key Files

- `.ai-pipeline/config.json` — pipeline configuration (agents, models, features)
- `.ai-pipeline/state.json` — current pipeline state (do not edit manually)
- `docs/TASKS.md` — task checklist (mark `[x]` when complete)
- `docs/TASK_GRAPH.json` — dependency graph with `parallel_groups`
- `docs/IMPLEMENTATION_LOG.md` — per-task completion log
- `docs/VERIFICATION_REPORT.md` — audit results (PASS/FAIL per task)

## Implementation Rules

When executing tasks from `docs/TASK_GRAPH.json`:

1. Implement only the assigned task — nothing more
2. Follow existing code conventions (naming, structure, formatting)
3. Mark completed tasks `[x]` in `docs/TASKS.md`
4. Append an entry to `docs/IMPLEMENTATION_LOG.md` after each task
5. Do not modify: `docs/architecture.md`, `docs/PRD.md`, `docs/DESIGN.md`, `docs/FEATURE_SPEC.md`, `.ai-pipeline/state.json`, `.ai-pipeline/prompts/*`

## Protected Paths (never modify during rollback)

- `CLAUDE.md`
- `.claude/`
- `docs/`
- `.ai-pipeline/`
- `.git/`

## Agent Team Protocol

When running as a **teammate worker** with an assigned `group_id`:

- Scope is the assigned group only
- Write the flag file `.ai-pipeline/state/<group_id>.flag` on completion
- Do not write to `.ai-pipeline/state.json` or `.ai-pipeline/prompts/`
