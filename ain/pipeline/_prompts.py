"""Prompt building and document validation utilities."""

from __future__ import annotations

import re
from pathlib import Path

from ain.pipeline._text import _strip_fences


def read_context_files(*files: Path) -> str:
    parts = []
    for f in files:
        if f.exists():
            content = f.read_text(encoding="utf-8")
            parts.append(f"<!-- FILE: {f.name} -->\n{content}\n<!-- END: {f.name} -->")
        else:
            parts.append(f"<!-- FILE: {f.name}  NOT FOUND -->")
    return "\n\n".join(parts)


def build_prompt(prompt_file: Path, *context_files: Path) -> str:
    if not prompt_file.exists():
        raise RuntimeError(f"Prompt file not found: {prompt_file}")
    prompt = prompt_file.read_text(encoding="utf-8")
    if context_files:
        ctx = read_context_files(*context_files)
        prompt = f"{prompt}\n\n---\n## Context\n\n{ctx}"
    return prompt


def validate_headings(file: Path, required: list[str]) -> list[str]:
    if not file.exists():
        return required[:]
    content = file.read_text(encoding="utf-8")
    return [h for h in required if not re.search(r"^" + re.escape(h) + r"(\s|$)", content, re.MULTILINE)]


def validate_tasks_file(tasks_file: Path) -> bool:
    if not tasks_file.exists():
        return False
    return bool(re.search(r"- \[[ x]\]", tasks_file.read_text(encoding="utf-8")))


def validate_task_graph(graph_file: Path) -> bool:
    if not graph_file.exists():
        return False
    try:
        import json
        content = _strip_fences(graph_file.read_text(encoding="utf-8"))
        data = json.loads(content)
        if isinstance(data.get("tasks"), list) and len(data["tasks"]) > 0:
            graph_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        return False
    except (json.JSONDecodeError, KeyError):
        return False
