"""Text processing utilities used across pipeline stages."""

from __future__ import annotations

import re

# Strips ANSI/VT escape sequences from agent output so they don't corrupt
# Rich's Live display when embedded in Text objects.
_ANSI_ESC = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07)")


def _strip_ansi(s: str) -> str:
    return _ANSI_ESC.sub("", s)


def _strip_fences(content: str) -> str:
    """Strip markdown code fences from file content written by agents."""
    lines = content.strip().splitlines()
    if lines and lines[0].startswith(chr(96) * 3):
        lines = lines[1:]
    if lines and lines[-1].strip() == chr(96) * 3:
        lines = lines[:-1]
    return chr(10).join(lines).strip()


def _truncate_for_prompt(text: str, max_len: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= max_len else f"{text[: max_len - 3]}..."
