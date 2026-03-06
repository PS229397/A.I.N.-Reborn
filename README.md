# AI Development Pipeline

A local multi-agent orchestrator for structured, reproducible AI-assisted development.

Coordinates Gemini, Codex, and Claude through deterministic stages — producing planning artifacts before touching any code, and requiring human approval before implementation begins.

---

## How it works

```
python pipeline.py
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
- Git (for branch creation and file tracking)
- AI agent CLIs configured in `.ai-pipeline/config.json`:
  - [`gemini`](https://github.com/google-gemini/gemini-cli) — architecture stage
  - [`codex`](https://github.com/openai/codex) — planning stage
  - [`claude`](https://claude.ai/claude-code) — task creation + implementation

Install Python dependencies (optional — only adds colored terminal output):

```bash
pip install -r requirements.txt
```

---

## Quickstart

```bash
# 1. Run the full pipeline from the beginning
python pipeline.py

# 2. After the planning questions stage pauses, answer the questions:
#    Edit docs/OPEN_ANSWERS.md, then continue:
python pipeline.py

# 3. Review the planning artifacts, then approve:
python pipeline.py --approve

# 4. Pipeline continues through implementation and validation automatically
```

---

## CLI Reference

| Command | Description |
|---|---|
| `python pipeline.py` | Run pipeline from current stage |
| `python pipeline.py --status` | Show current stage and task progress |
| `python pipeline.py --approve` | Approve planning artifacts, advance to implementation |
| `python pipeline.py --resume <stage>` | Resume from a specific stage |
| `python pipeline.py --stage <stage>` | Run one stage only |
| `python pipeline.py --reset` | Reset pipeline to idle (clears all state) |
| `python pipeline.py --init-config` | Write default `.ai-pipeline/config.json` |

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

Resume example:

```bash
python pipeline.py --resume architecture
python pipeline.py --resume implementation
```

---

## Configuration

Edit `.ai-pipeline/config.json` to configure agent commands, git behavior, and validation.

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

Set `"model"` to a specific model string if you want to override the agent's default.

### ChiefLoop (task creation agent)

ChiefLoop is the task orchestration engine for the `task_creation` stage. It converts the planning documents (`PRD.md`, `DESIGN.md`, `FEATURE_SPEC.md`) into an ordered, dependency-aware task graph (`TASKS.md` + `TASK_GRAPH.json`).

The pipeline defaults `task_creation` to `claude --print` so it works out of the box without ChiefLoop installed. If you have ChiefLoop available, point the stage at it in config:

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

The pipeline pipes the prompt to the command via stdin and reads the output. ChiefLoop (or any substitute) must:

1. Accept a prompt on stdin
2. Return output containing `<!-- FILE: TASKS.md -->` and `<!-- FILE: TASK_GRAPH.json -->` markers
3. Exit 0 on success

Any agent that produces those two artifacts in the expected format will work.

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

`auto_branch` creates a `ai/feature-<timestamp>` branch before implementation begins.
`auto_commit` is disabled by default — enable to auto-commit after validation passes.

### Custom validation commands

By default the pipeline auto-detects validation commands from your project type (Laravel, Node, Python, Go, Rust). Override with explicit commands:

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

```
repo/
├── pipeline.py                        Orchestrator
├── requirements.txt
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
    │   └── planning_approved.flag     Created by --approve
    └── logs/
        ├── pipeline.log
        ├── validation.log
        └── <agent>_last_prompt.txt    Debug: last prompt sent to each agent
```

---

## Design principles

**Agents communicate only through files.**
Gemini never talks to Codex. Codex never talks to Claude. Each agent reads documents and writes documents. This keeps the pipeline deterministic and auditable.

**Planning is locked before implementation starts.**
The approval gate prevents Claude from implementing a plan that hasn't been reviewed. The flag file at `.ai-pipeline/approvals/planning_approved.flag` controls this.

**Every decision is traceable.**
The scan artifacts, all planning documents, the task graph, the implementation log, and the validation log are all written to disk and can be reviewed or committed to version control.

**The pipeline is resumable.**
Any stage can be re-run with `--resume`. If an agent produces bad output, fix the artifact manually and resume from the next stage.

---

## Warp Terminal

Workflow shortcuts are included in `.warp/workflows/`. To make them available globally, copy them to your Warp workflows directory:

```bash
cp .warp/workflows/*.yaml ~/.warp/workflows/
```

Available shortcuts: `Pipeline — Run`, `Pipeline — Scan`, `Pipeline — Plan`, `Pipeline — Implement`, `Pipeline — Approve`, `Pipeline — Status`, `Pipeline — Reset`.

---

## Troubleshooting

**Pipeline is stuck in `failed` state**
```bash
python pipeline.py --status           # see the failure reason
python pipeline.py --resume <stage>   # resume after fixing the issue
python pipeline.py --reset            # or reset entirely
```

**Agent command not found**
Edit `.ai-pipeline/config.json` and set the correct `command` for the failing agent. Verify the CLI is on your PATH.

**Architecture validation failed**
The architecture document is missing required headings. Open `docs/architecture.md`, add the missing sections, then re-run:
```bash
python pipeline.py --resume planning_questions
```

**Planning documents are malformed**
The planning agent didn't use the required `<!-- FILE: name.md -->` markers. Check `.ai-pipeline/logs/planning_last_output.txt` for the raw output, fix `docs/PRD.md` and `docs/DESIGN.md` manually, then:
```bash
python pipeline.py --resume task_creation
```

**Validation fails after implementation**
Check `.ai-pipeline/logs/validation.log`. Fix the issues in the codebase, then:
```bash
python pipeline.py --resume validation
```
