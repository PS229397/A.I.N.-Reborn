# Task Creation Prompt

Convert planning documents into a structured, dependency-ordered task graph.

## Planning Documents (context)
- `docs/PRD.md`
- `docs/DESIGN.md`
- `docs/FEATURE_SPEC.md`
- `docs/architecture.md`

## Output

Produce two files:

**docs/TASKS.md** — markdown checkbox task list

```markdown
# Tasks

## Database
- [ ] create migration for <table_name> table
- [ ] add <Model> model with fields: <field list>

## Backend
- [ ] implement <MethodName>() in <ServiceClass>
- [ ] add <HTTP_METHOD> /api/<path> endpoint in <Controller>
- [ ] add unit test for <method>()

## Frontend
- [ ] add <ComponentName> component
- [ ] wire <ComponentName> to <endpoint>

## Integration
- [ ] end-to-end test for <user_story>
```

**docs/TASK_GRAPH.json** — dependency graph

```json
{
  "tasks": [
    {
      "id": 1,
      "description": "exact text matching TASKS.md checkbox",
      "depends_on": [],
      "status": "pending",
      "files_affected": ["path/to/expected/file.ext"],
      "completed_at": null
    }
  ],
  "parallel_groups": [
    {
      "group_id": "group-1",
      "can_run_parallel": true,
      "tasks": [1],
      "depends_on": []
    },
    {
      "group_id": "group-2",
      "can_run_parallel": true,
      "tasks": [2, 3],
      "depends_on": ["group-1"]
    }
  ],
  "generated_at": "<ISO timestamp>",
  "total": <number>,
  "completed": 0
}
```

## Rules

- Each task is a single, atomic action (one migration, one model, one method, one test)
- Task descriptions must be concrete — name the exact file, class, method, or table
- Order tasks so dependencies come first (migrations before models, models before services, services before endpoints, endpoints before tests)
- `depends_on` must list the IDs of tasks that must complete first
- `files_affected` must list the actual file paths that will be created or modified
- Do not create tasks that are not supported by the planning documents
- Do not create vague tasks like "update the frontend" — be specific
- `parallel_groups` is required — every task must belong to exactly one group
- Set `can_run_parallel: true` when tasks in the group touch disjoint files; `false` when they share files or must run sequentially
- `parallel_groups[].depends_on` lists `group_id` values that must complete before this group starts
- Groups with no overlapping file ownership and no data dependencies may share a group or appear as sibling groups with the same `depends_on`

Write both files directly to disk using your file-editing tools.
