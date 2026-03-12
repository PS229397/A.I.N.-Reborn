"""Repository scanning and analysis utilities."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _build_tree(
    root: Path,
    ignore: set[str],
    prefix: str = "",
    depth: int = 0,
    max_depth: int = 6,
) -> list[str]:
    if depth > max_depth:
        return ["..."]
    lines = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return []
    visible = [e for e in entries if e.name not in ignore]
    for i, entry in enumerate(visible):
        connector = "└ " if i == len(visible) - 1 else "├ "
        lines.append(f"{prefix}{connector}{entry.name}")
        if entry.is_dir():
            ext = "    " if i == len(visible) - 1 else "│   "
            lines.extend(_build_tree(entry, ignore, prefix + ext, depth + 1, max_depth))
    return lines


def detect_stack(tracked_files: list[str]) -> dict[str, Any]:
    files_set = set(tracked_files)
    stack: dict[str, Any] = {
        "languages": [],
        "frameworks": [],
        "package_managers": [],
        "devops": [],
    }
    ext_counts: dict[str, int] = {}
    for f in tracked_files:
        ext = Path(f).suffix.lower()
        if ext:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    lang_map = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".php": "PHP",
        ".rb": "Ruby",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".cs": "C#",
    }
    seen: set[str] = set()
    for ext, lang in lang_map.items():
        if ext in ext_counts and lang not in seen:
            stack["languages"].append(lang)
            seen.add(lang)

    pm_map = {
        "package.json": "npm/yarn/bun",
        "composer.json": "Composer",
        "requirements.txt": "pip",
        "pyproject.toml": "poetry/uv",
        "Gemfile": "Bundler",
        "go.mod": "Go modules",
        "Cargo.toml": "Cargo",
    }
    for fname, pm in pm_map.items():
        if fname in files_set:
            stack["package_managers"].append(pm)

    if "artisan" in files_set or any("app/Http" in f for f in tracked_files):
        stack["frameworks"].append("Laravel")
    if any("next.config" in f for f in tracked_files):
        stack["frameworks"].append("Next.js")
    if any("nuxt.config" in f for f in tracked_files):
        stack["frameworks"].append("Nuxt.js")
    if any("manage.py" in f for f in tracked_files):
        stack["frameworks"].append("Django")
    if "Dockerfile" in files_set:
        stack["devops"].append("Docker")
    if any("docker-compose" in f for f in tracked_files):
        stack["devops"].append("Docker Compose")
    if any(".github/workflows" in f for f in tracked_files):
        stack["devops"].append("GitHub Actions")

    migrations = [f for f in tracked_files if "migration" in f.lower()]
    if migrations:
        stack["migrations"] = migrations[:10]

    return stack


def _extract_key_file_content(config: dict, root: Path) -> dict[str, str]:
    result = {}
    for fname in config["scan"]["key_files"]:
        path = root / fname
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if len(content) > 3000:
                content = content[:3000] + "\n... [truncated]"
            result[fname] = content
    return result


def generate_repo_summary(
    tree: str,
    tracked_files: list[str],
    config: dict,
    root: Path,
) -> str:
    stack = detect_stack(tracked_files)
    key_files = _extract_key_file_content(config, root)

    lines = [
        "# Repository Summary",
        f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "\n## Technology Stack",
    ]
    for cat, items in stack.items():
        if items and cat != "migrations":
            lines.append(f"\n**{cat.title()}:** {', '.join(str(i) for i in items)}")
    if "migrations" in stack:
        lines.append(f"\n**Migrations:** {len(stack['migrations'])} files detected")

    lines.append(f"\n## File Count\n\nTracked files: {len(tracked_files)}")
    lines.append("\n## Key Configuration Files\n")
    for fname, content in key_files.items():
        lines.append(f"### {fname}\n```\n{content}\n```\n")
    if not key_files:
        lines.append("No standard configuration files detected.")

    lines.append("\n## Entry Points\n")
    entries = [
        f
        for f in tracked_files
        if any(
            n in Path(f).name.lower()
            for n in ["main.", "index.", "app.", "server.", "manage.py", "artisan"]
        )
        and not any(s in f for s in ["node_modules", "vendor", "test", "spec"])
    ]
    for ep in entries[:15]:
        lines.append(f"- `{ep}`")

    lines.append("\n## Routes / Controllers\n")
    routes = [
        f
        for f in tracked_files
        if any(kw in f.lower() for kw in ["route", "controller", "handler", "endpoint"])
        and "node_modules" not in f
        and "vendor" not in f
    ]
    for rf in routes[:20]:
        lines.append(f"- `{rf}`")

    lines.append(f"\n## Repository Tree\n\n```\n{tree}\n```")
    return "\n".join(lines)
