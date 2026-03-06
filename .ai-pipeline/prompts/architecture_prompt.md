# Architecture Analysis Prompt

You are a senior software architect. Analyze the provided repository metadata and produce a precise architecture document.

## Files You May Read
- `.ai-pipeline/scan/repo_tree.txt`
- `.ai-pipeline/scan/repo_summary.md`
- `.ai-pipeline/scan/tracked_files.txt`

## Files You Must Write
- `docs/architecture.md`

## Files You Must NOT Modify
- Any source code files
- `.ai-pipeline/state.json`

## Output Format

Produce `docs/architecture.md` containing ALL of the following headings in this exact format:

```
# System Overview
# Tech Stack
# Repo Structure
# Core Domains
# Runtime Architecture
# Data Flow
# Entry Points
# State Management
# Testing Strategy
# Risks and Unknowns
```

Under each heading:
- Reference concrete file paths, module names, and component names from the scan data
- Do not invent components that are not evidenced in the scan
- If information is absent, say so explicitly under that heading
- Be architectural, not generic

Write the document to `docs/architecture.md`. Output only the document content — no preamble.
