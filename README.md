# A.I.N. Pipeline

A local multi-agent orchestrator for structured, reproducible AI-assisted development.

Coordinates Gemini, Codex, ChiefLoop, and Claude through deterministic stages — producing planning artifacts before touching any code, and requiring human approval before implementation begins.

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
       └─ Codex → OPEN_QUESTIONS.md
            └─ [you answer in OPEN_ANSWERS.md]
                 └─ Codex → PRD.md, DESIGN.md, FEATURE_SPEC.md
                      └─ ChiefLoop (or Claude) → TASKS.md, TASK_GRAPH.json
                           └─ [you approve]
                                └─ Claude → implements tasks
                                     └─ Validation → tests/lint
                                          └─ Done
```

Human involvement occurs at two points only:
1. Answering planning questions
2. Approving the task graph before implementation

---

## Requirements

- Python 3.10+
- Git
- AI agent CLIs configured in `.ai-pipeline/config.json`:
  - [`gemini`](https://github.com/google-gemini/gemini-cli) — architecture stage
  - [`codex`](https://github.com/openai/codex) — planning stage
  - [`chiefloop`](https://github.com/PS229397/A.I.N.-Reborn) or `claude --print` — task creation stage
  - [`claude`](https://claude.ai/claude-code) — implementation stage

---

## Quickstart

```bash
# 1. Install
pip install ain-pipeline

# 2. Initialize the pipeline in your repo
cd your-project
ain init

# 3. Configure your agents
#    Edit .ai-pipeline/config.json

# 4. Run
ain run

# 5. The pipeline pauses after planning questions — answer them:
#    Edit docs/OPEN_ANSWERS.md, then continue:
ain run

# 6. Review the plan, then approve:
ain --approve

# 7. Pipeline runs implementation and validation automatically
```

---

## CLI Reference

### Subcommands

| Command | Description |
|---|---|
| `ain init` | Scaffold `.ai-pipeline/` into the current repo |
| `ain run` | Run pipeline from current stage |
| `ain run --resume <stage>` | Resume from a specific stage |
| `ain run --stage <stage>` | Run one stage only |

### Flags

| Flag | Description |
|---|---|
| `ain --status` | Show current stage and task progress |
| `ain --approve` | Approve planning artifacts, advance to implementation |
| `ain --reset` | Reset pipeline to idle (clears all state) |

### Stages

```
idle
scanning
architecture
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
ain run --stage task_creation
```

---

## Configuration

`ain init` writes `.ai-pipeline/config.json` to your repo. Edit it to configure agent commands, git behaviour, and validation.

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
      "model": null
    },
    "task_creation": {
      "command": "chiefloop",
      "args": [],
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

Set `"model"` to override an agent's default model. Each agent is invoked with the prompt piped via stdin and reads output from stdout.

### ChiefLoop (task creation agent)

ChiefLoop is the task orchestration engine responsible for converting planning documents into a structured, dependency-ordered task graph (`TASKS.md` + `TASK_GRAPH.json`).

The pipeline defaults `task_creation` to `claude --print` so it works out of the box without ChiefLoop installed. To use ChiefLoop when available:

```json
{
  "agents": {
    "task_creation": {
      "command": "chiefloop",
      "args": [],
      "model": null
    }
  }
}
```

Any agent used in this slot must:
1. Accept a prompt on stdin
2. Return output containing `<!-- FILE: TASKS.md -->` and `<!-- FILE: TASK_GRAPH.json -->` markers
3. Exit 0 on success

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

After `ain init`, your repo will contain:

```
your-repo/
├── docs/
│   ├── architecture.md                Generated by Gemini
│   ├── OPEN_QUESTIONS.md              Generated by Codex
│   ├── OPEN_ANSWERS.md                Written by you
│   ├── PRD.md                         Generated by Codex
│   ├── DESIGN.md                      Generated by Codex
│   ├── FEATURE_SPEC.md                Generated by Codex
│   ├── TASKS.md                       Generated by ChiefLoop
│   ├── TASK_GRAPH.json                Generated by ChiefLoop
│   └── IMPLEMENTATION_LOG.md          Written by Claude
└── .ai-pipeline/
    ├── state.json                     Pipeline state
    ├── config.json                    Agent configuration
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
    │   └── planning_approved.flag     Created by ain --approve
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

If you don't want a global install, clone this repo and copy `pipeline.py` into your project. It works identically to the `ain` CLI as long as the `ain/` package directory is alongside it:

```bash
# Clone or download pipeline.py + ain/ into your project
python pipeline.py init
python pipeline.py run
python pipeline.py --status
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
twine upload dist/*
```

Future releases:
```bash
# bump version in pyproject.toml, then:
python -m build && twine upload dist/*
```

---

## Troubleshooting

**Pipeline is stuck in `failed` state**
```bash
ain --status                       # see the failure reason
ain run --resume <stage>           # resume after fixing the issue
ain --reset                        # or reset entirely
```

**Agent command not found**
Edit `.ai-pipeline/config.json` and set the correct `command` for the failing agent. Verify the CLI is on your PATH.

**Architecture validation failed**
The architecture document is missing required headings. Open `docs/architecture.md`, add the missing sections, then:
```bash
ain run --resume planning_questions
```

**Planning documents are malformed**
The planning agent didn't use the required `<!-- FILE: name.md -->` markers. Check `.ai-pipeline/logs/planning_last_output.txt` for the raw output, fix the docs manually, then:
```bash
ain run --resume task_creation
```

**Validation fails after implementation**
Check `.ai-pipeline/logs/validation.log`. Fix the issues in the codebase, then:
```bash
ain run --resume validation
```
