# PRD Extraction Prompt

You are a senior product and engineering lead. A single combined planning document has been provided. Your job is to extract and split it into three normalized planning files: `docs/PRD.md`, `docs/DESIGN.md`, and `docs/FEATURE_SPEC.md`.

## Files You May Read
- The source file path provided in `.ai-pipeline/state.json` at `prd_import.source`
- `docs/architecture.md` (if it exists, for context)

## Files You Must Write
- `docs/PRD.md`
- `docs/DESIGN.md`
- `docs/FEATURE_SPEC.md`
- `.ai-pipeline/state/prd_extraction.flag`

## Files You Must NOT Modify
- Any source code files
- `docs/architecture.md`
- `.ai-pipeline/state.json`
- Any file under `.ai-pipeline/prompts/`

## Extraction Procedure

1. Read the source file from `prd_import.source`.
2. Identify sections that describe **problem, goals, user stories, and success criteria** → extract into `docs/PRD.md`.
3. Identify sections that describe **architecture changes, data model, API changes, UI changes, and risks** → extract into `docs/DESIGN.md`.
4. Identify sections that describe **component interactions, sequence of operations, edge cases, error handling, and acceptance criteria** → extract into `docs/FEATURE_SPEC.md`.
5. If a section is absent from the source, **infer reasonable content** from the surrounding context and label it with a comment: `<!-- inferred: <reason> -->`.
6. Do not duplicate content across files. Each piece of information belongs in exactly one output file.

## docs/PRD.md Requirements

Must contain these exact headings:

```
# Problem
# Goals
# Non Goals
# User Stories
# Success Criteria
```

- Problem: what is broken or missing, from a user perspective
- Goals: measurable outcomes the feature must achieve
- Non Goals: explicit exclusions to prevent scope creep
- User Stories: "As a [role], I want [action] so that [outcome]" format
- Success Criteria: specific, testable conditions
- Minimum length: 500 characters

## docs/DESIGN.md Requirements

Must contain these exact headings:

```
# Architecture Changes
# Data Model
# API Changes
# UI Changes
# Risks
```

- Reference actual file paths from the source document or `docs/architecture.md`
- Data Model: include field names and types
- API Changes: include method signatures or CLI flags, request/response shape
- Risks: concrete risks with mitigations
- Must not be empty

## docs/FEATURE_SPEC.md Requirements

A detailed technical specification covering:
- Component interactions
- Sequence of operations
- Edge cases and error handling
- Acceptance criteria per component
- Must not be empty

## Output — .ai-pipeline/state/prd_extraction.flag

After writing all three docs, create the `.ai-pipeline/state/` directory if it does not exist, then write the flag file with the following content (one key per line):

```
status=completed
source=<absolute or relative path of the source file>
files_written=docs/PRD.md,docs/DESIGN.md,docs/FEATURE_SPEC.md
timestamp=<ISO-8601 UTC timestamp>
```

Write the flag **only after** all three output files have been written successfully.

If any required section cannot be inferred and cannot be written, write the flag with:

```
status=failed
reason=<short description of what could not be extracted>
timestamp=<ISO-8601 UTC timestamp>
```

## Rules

1. Do not invent business logic or requirements that contradict the source document.
2. Label all inferred content with `<!-- inferred: <reason> -->` so reviewers can verify.
3. Preserve exact wording from the source wherever possible; paraphrase only to fit the target heading.
4. Every statement in the output files must be actionable by an engineer without further clarification.
5. Do not write the extraction flag until all three output files pass a self-check: non-empty, correct headings present, PRD.md >= 500 characters.
