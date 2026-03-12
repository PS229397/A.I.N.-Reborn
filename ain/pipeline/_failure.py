"""Agent failure classification utilities."""

from __future__ import annotations

_TOKEN_LIMIT_PHRASES = [
    "context window",
    "token limit",
    "maximum context",
    "too long",
    "prompt is too",
    "input too long",
    "context length",
    "max_tokens",
    "context_length_exceeded",
    "rate limit",
    "overloaded",
    "reduce the length",
]


def is_token_limit_error(output: str, returncode: int) -> bool:
    """Return True if the agent output/exit looks like a context or token-limit error."""
    if returncode == 0:
        return False
    combined = output.lower()
    return any(phrase in combined for phrase in _TOKEN_LIMIT_PHRASES)


def classify_agent_failure(message: str) -> str:
    text = (message or "").lower()
    if any(phrase in text for phrase in _TOKEN_LIMIT_PHRASES):
        return "token_exhaustion"
    if "timed out" in text or "no response" in text:
        return "no_response"
    return "unknown"
