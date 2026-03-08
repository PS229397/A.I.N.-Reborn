# A.I.N. Pipeline

A local multi-agent orchestrator for structured, reproducible AI-assisted development.

Coordinates Gemini, Codex, ChiefLoop, and Claude through deterministic stages — producing planning artifacts before touching any code, and requiring human approval before implementation begins.

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│  ▸ A.I.N. v0.1.8  ║  SYS: RUNNING  ║  UPTIME: 0s  ║  NODE: 527af                           │
├───────────────────────────────────┬──────────────────────────────────────────────────────────┤
│  // DECK                          │  // DATA FEED                                            │
│  ▶ scanning                       │  12:41:03 [INF] Repo scan started                        │
│  ▶ architecture                   │  12:41:08 [INF] architecture.md generated                │
│    ▷ codex /planner/ 0.7s         │  12:41:11 [INF] OPEN_QUESTIONS.md updated               │
│  ◈ planning_generation            │  12:41:15 [INF] waiting for approval                     │
│  ◆ task_creation                  │                                                          │
├───────────────────────────────────┴──────────────────────────────────────────────────────────┤
│  Q jack out  R reboot  L data feed  C sys.config  S density  ? help.sys  F freeze  ↑/↓ scroll │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Installation

```bash
pip install ain-pipeline
```

This installs the `ain` CLI globally. You can then initialize the pipeline in any repository.

---

## How it works

```bash
ain run
```

The pipeline executes these stages in sequence:

```
Repo Scan
  └─ Gemini → docs/architecture.md
       └─ [you describe the feature in an editor popup]
            └─ Codex (interactive popup) → brainstorm + OPEN_QUESTIONS.md
                 └─ Codex (interactive popup) → PRD.md, DESIGN.md, FEATURE_SPEC.md
                      └─ Claude → TASKS.md, TASK_GRAPH.json
                           └─ [you approve]
                                └─ Claude → implements tasks
                                     └─ Validation → tests/lint
                                          └─ Done
```

Human involvement occurs at three points:
1. **Feature context** — describe what you want to build or fix
2. **Brainstorm** — interactive back-and-forth with Codex in a popup terminal
3. **Approval** — review the plan before implementation starts

---

## Requirements

- Python 3.10+
- Git
- AI agent CLIs available on your PATH:
  - [`gemini`](https://github.com/google-gemini/gemini-cli) — architecture stage
  - [`codex`](https://github.com/openai/codex) — planning + brainstorm stage
  - [`claude`](https://claude.ai/claude-code) — task creation + implementation stage

---

## Quickstart

```bash
# 1. Install
pip install ain-pipeline

# 2. Enter your repo
cd your-project

# 3. Run (creates .ai-pipeline/config.json automatically if missing)
ain run

# 4. Describe your feature in the editor that opens (Notepad on Windows)
#    Save and press Enter in the terminal

# 5. Brainstorm with Codex in the popup terminal
#    When satisfied, press Enter in the terminal to continue

# 6. Wait for Codex to generate the plan documents in another popup
#    Press Enter when done

# 7. Review the plan, then approve:
ain approve

# 8. Pipeline runs implementation and validation automatically
```

---

## CLI Reference

### Subcommands

| Command | Description |
|---|---|
| `ain run` | Run pipeline from current stage |
| `ain run --resume <stage>` | Resume from a specific stage |
| `ain approve [--run-id <id>]` | Approve a pending stage |
| `ain status` | Show current pipeline status |
| `ain reset [--hard] [--yes]` | Reset pipeline state |
| `ain logs [--follow] [--tail N] [--level L] [--source S] [--json]` | View/stream merged logs |
| `ain config <list|get|set|reset>` | Manage layered config |
| `ain version [--short]` | Show version |

### `ain run` options

| Option | Description |
|---|---|
| `--plain` | Disable TUI and print plain line output |
| `--tui rich|textual` | Select renderer (non-TTY falls back to plain) |
| `--no-color` | Disable ANSI colours via `NO_COLOR=1` |
| `--resume <stage>` | Resume from a specific stage |

### TUI behavior (Rich Live)

- Default renderer in TTY mode is Rich Live (`--tui rich`).
- Panels support keyboard controls:
  - `Q` quit (with confirmation while active)
  - `R` reboot current run
  - `L` toggle log/data-feed focus
  - `C` toggle config view
  - `S` toggle compact deck density
  - `?` toggle help overlay
  - `F` freeze/unfreeze log autoscroll
  - `↑` / `↓` scroll the feed
  - `A` approve when awaiting approval
- Theme: neon cyan + red-pink accents (panel borders/titles and status accents).

### Legacy aliases (deprecated, still accepted)

| Old flag | Replacement |
|---|---|
| `ain --status` | `ain status` |
| `ain --approve` | `ain approve` |
| `ain --reset` | `ain reset` |

### Stages

```
idle
scanning
architecture
user_context
planning_questions
planning_generation
task_creation
waiting_approval
implementation
validation
done
```

Examples:

```bash
ain run --resume architecture
ain run --resume waiting_approval
```

---

## Configuration

`ain run` creates `.ai-pipeline/config.json` if needed. Edit it to configure agent commands, git behaviour, and validation.

### Agent commands

```json
{
  "agents": {
    "architecture": {
      "command": "gemini",
      "args": [],
      "model": null
    },
    "planning": {
      "command": "codex",
      "args": [],
      "model": null,
      "prompt_mode": "arg"
    },
    "task_creation": {
      "command": "claude",
      "args": ["--print"],
      "model": null
    },
    "implementation": {
      "command": "claude",
      "args": ["--allowedTools", "Edit,Write,Bash,Read,Glob,Grep"],
      "model": null
    }
  }
}
```

**`prompt_mode`** controls how the prompt is delivered to the agent:
- `"stdin"` (default) — prompt is piped via stdin (works for Gemini, Claude)
- `"arg"` — prompt is passed as a positional argument (required for Codex)

Set `"model"` to override an agent's default model.

### Planning stage — popup terminals

The planning stages open interactive popup terminal windows rather than running non-interactively. This lets you have a real conversation with Codex during brainstorm.

**Stage: `user_context`** — Opens your system editor (Notepad on Windows) with a template. Fill in the feature or bug description, save, and press Enter in the main terminal.

**Stage: `planning_questions`** — Opens a `cmd` window running Codex with the feature context pre-loaded. Codex asks clarifying questions; you answer. When done, Codex writes `docs/OPEN_QUESTIONS.md`. Press Enter in the main terminal to continue.

**Stage: `planning_generation`** — Opens another `cmd` window with Codex tasked to write `PRD.md`, `DESIGN.md`, and `FEATURE_SPEC.md`. Press Enter when the files are written.

If Codex doesn't write the files automatically, you can create them manually and then press Enter — the pipeline validates the headings and continues.

### Git settings

```json
{
  "git": {
    "auto_branch": true,
    "auto_commit": false,
    "branch_prefix": "ai/feature"
  }
}
```

`auto_branch` creates an `ai/feature-<timestamp>` branch before implementation begins.
`auto_commit` is disabled by default — enable to commit automatically after validation passes.

### Custom validation commands

The pipeline auto-detects validation commands from your project type (Laravel, Node, Python, Go, Rust). Override with explicit commands:

```json
{
  "validation": {
    "auto_detect": false,
    "commands": [
      ["npm", "run", "lint"],
      ["npm", "test"],
      ["npm", "run", "build"]
    ]
  }
}
```

---

## File structure

After first run and a full pipeline pass, your repo will contain:

```
your-repo/
├── docs/
│   ├── architecture.md                Generated by Gemini
│   ├── OPEN_QUESTIONS.md              Generated by Codex (brainstorm output)
│   ├── PRD.md                         Generated by Codex
│   ├── DESIGN.md                      Generated by Codex
│   ├── FEATURE_SPEC.md                Generated by Codex
│   ├── TASKS.md                       Generated by Claude
│   ├── TASK_GRAPH.json                Generated by Claude
│   └── IMPLEMENTATION_LOG.md          Written by Claude
└── .ai-pipeline/
    ├── state.json                     Pipeline state
    ├── config.json                    Agent configuration
    ├── user_context.md                Your feature description
    ├── brainstorm_context.md          Context sent to Codex for brainstorm
    ├── scan/
    │   ├── repo_tree.txt
    │   ├── tracked_files.txt
    │   └── repo_summary.md
    ├── prompts/
    │   ├── architecture_prompt.md
    │   ├── planning_questions_prompt.md
    │   ├── planning_generation_prompt.md
    │   ├── task_creation_prompt.md
    │   └── implementation_prompt.md
    ├── approvals/
    │   └── planning_approved.flag     Created by ain approve
    └── logs/
        ├── pipeline.log
        ├── validation.log
        └── <agent>_last_prompt.txt    Debug: last prompt sent to each agent
```

The prompt files are yours to edit — they control exactly what each agent is asked to do.

---

## Design principles

**Agents communicate only through files.**
Gemini never talks to Codex. Codex never talks to Claude. Each agent reads documents and writes documents. This keeps the pipeline deterministic and auditable.

**Planning is locked before implementation starts.**
The approval gate prevents Claude from implementing a plan that hasn't been reviewed. The flag file at `.ai-pipeline/approvals/planning_approved.flag` controls this.

**Every decision is traceable.**
The scan artifacts, planning documents, task graph, implementation log, and validation log are all written to disk and can be committed to version control.

**The pipeline is resumable.**
Any stage can be re-run with `ain run --resume <stage>`. If an agent produces bad output, fix the artifact manually and resume from the next stage.

---

## Drop-in usage (no install)

If you don't want a global install, run from a clone of this repo:

```bash
python -m ain run
python -m ain status
python -m ain logs --tail 100
```

---

## Warp Terminal

Workflow shortcuts are included in `.warp/workflows/`. Copy them to make them available globally in Warp:

```bash
cp .warp/workflows/*.yaml ~/.warp/workflows/
```

Available shortcuts: `Pipeline — Run`, `Pipeline — Scan`, `Pipeline — Plan`, `Pipeline — Implement`, `Pipeline — Approve`, `Pipeline — Status`, `Pipeline — Reset`.

---

## Publishing (maintainer notes)

```bash
pip install build twine
python -m build
python -m twine upload dist/*
# Username: __token__
# Password: <your PyPI API token>
```

---

## Troubleshooting

**Pipeline is stuck in `failed` state**
```bash
ain status                         # see the failure reason
ain run --resume <stage>           # resume after fixing the issue
ain reset                          # or reset entirely
```

**Agent command not found**
Install the missing CLI (`gemini`, `codex`, or `claude`), then verify it is on your PATH. You can also edit `.ai-pipeline/config.json` and set the correct `command`.

**On Windows: agent not found even though it's installed**
npm global installs on Windows create `.cmd` wrappers (e.g. `gemini.CMD`). The pipeline resolves these automatically using `shutil.which`. If it still fails, ensure the npm global bin directory is on your PATH:
```powershell
npm config get prefix   # note the path
# Add <prefix> to your PATH in System Settings → Environment Variables
```

**Architecture validation failed**
The architecture document is missing required headings. Open `docs/architecture.md`, add the missing sections, then:
```bash
ain run --resume user_context
```

**Codex popup closes immediately or produces no files**
Codex may need to be configured with an API key. Run `codex` manually in a terminal to complete its setup. You can also create the planning documents manually and press Enter to continue.

**TASK_GRAPH.json is invalid or empty**
The agent may have wrapped JSON in a code fence. The pipeline strips these automatically as of v0.1.7. If you're on an older version, fix manually:
```bash
pip install --upgrade ain-pipeline
ain run --resume task_creation
```

**Validation fails after implementation**
Check `.ai-pipeline/logs/validation.log`. Fix the issues in the codebase, then:
```bash
ain run --resume validation
```

---

## Migration Guide

### Upgrading from pre-v1 (flag-based interface)

The original interface used global flags (`ain --reset`, `ain --approve`, `ain --status`).
These are still accepted but will print a `DeprecationWarning` and **will be removed in v2.0.0**.

| Old command | New command |
|---|---|
| `ain --reset` | `ain reset` |
| `ain --approve` | `ain approve` |
| `ain --status` | `ain status` |
| `ain --resume <stage>` | `ain run --resume <stage>` |

Update your scripts before upgrading to v2.0.0 to avoid breakage.
