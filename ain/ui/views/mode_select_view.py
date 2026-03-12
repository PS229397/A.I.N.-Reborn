"""Fullscreen Rich view for selecting pipeline mode inside the TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Shared neon palette for UI consistency.
_C_PRIMARY_TEXT = "#2EDCD1"
_C_SECONDARY_TEXT = "#23A19F"
_C_NEON_PINK = "bold #ff2d6f"
_C_BORDER = "#ff2d6f"


@dataclass
class ModeSelectResult:
    """Outcome of a key press in mode selection."""

    action: str  # "none" | "select" | "cancel"
    mode: str | None = None

    @property
    def is_select(self) -> bool:
        return self.action == "select"

    @property
    def is_cancel(self) -> bool:
        return self.action == "cancel"


class ModeSelectView:
    """Mode selector with tier selection per workflow."""

    def __init__(self, modes: Sequence[dict[str, Any]], *, current_mode: str) -> None:
        if not modes:
            raise ValueError("modes cannot be empty")
        self._bases = self._group_by_base(list(modes))
        self._current_mode = current_mode
        self._selected_base = 0
        self._tier_select_active = False
        # Map base -> tier idx
        self._tier_idx: Dict[str, int] = {}
        for b_idx, base in enumerate(self._bases):
            # pick tier that matches current_mode, else first
            tier_idx = 0
            for idx, tier in enumerate(base["tiers"]):
                if tier["key"] == current_mode:
                    tier_idx = idx
                    self._selected_base = b_idx
                    break
            self._tier_idx[base["base_key"]] = tier_idx

    @property
    def current_mode(self) -> str:
        base = self._bases[self._selected_base]
        tier_idx = self._tier_idx.get(base["base_key"], 0)
        return base["tiers"][tier_idx]["key"]

    def handle_key(self, key: str) -> ModeSelectResult:
        norm = self._normalize_key(key)
        base = self._bases[self._selected_base]

        if norm == "up":
            if self._tier_select_active:
                self._tier_select_active = False
            self._selected_base = (self._selected_base - 1) % len(self._bases)
            return ModeSelectResult("none")
        if norm == "down":
            if self._tier_select_active:
                self._tier_select_active = False
            self._selected_base = (self._selected_base + 1) % len(self._bases)
            return ModeSelectResult("none")
        if norm in {"left", "right"}:
            if len(base["tiers"]) > 1:
                if not self._tier_select_active:
                    self._tier_select_active = True
                delta = -1 if norm == "left" else 1
                cur = self._tier_idx.get(base["base_key"], 0)
                self._tier_idx[base["base_key"]] = (cur + delta) % len(base["tiers"])
            return ModeSelectResult("none")
        if norm == "enter":
            if not self._tier_select_active and len(base["tiers"]) > 1:
                self._tier_select_active = True
                return ModeSelectResult("none")
            return ModeSelectResult("select", mode=self.current_mode)
        if norm in {"q", "quit", "esc", "escape", "\x1b"}:
            if self._tier_select_active:
                self._tier_select_active = False
                return ModeSelectResult("none")
            return ModeSelectResult("cancel", mode=self.current_mode)
        return ModeSelectResult("none")

    def render(self) -> Panel:
        body = Text()
        body.append("Select Pipeline Workflow\n\n", style=_C_NEON_PINK)
        selected_models: list[str] = []
        selected_summary: str | None = None
        for idx, base in enumerate(self._bases):
            is_selected = idx == self._selected_base
            marker = ">" if is_selected else " "
            marker_style = _C_NEON_PINK if is_selected else _C_SECONDARY_TEXT
            text_style = _C_NEON_PINK if is_selected else _C_PRIMARY_TEXT
            body.append(f"{marker} ", style=marker_style)
            body.append(f"{base['label']}\n", style=text_style)
            if is_selected:
                tier_idx = self._tier_idx.get(base["base_key"], 0)
                model_line = base["tiers"][tier_idx].get("model_line")
                if model_line:
                    selected_models.append(model_line)
                if base.get("summary"):
                    selected_summary = base["summary"]
                if self._tier_select_active and len(base["tiers"]) > 1:
                    body.append("  ", style=_C_SECONDARY_TEXT)
                    for t_idx, tier in enumerate(base["tiers"]):
                        selected = t_idx == tier_idx
                        open_b = "[" if selected else " "
                        close_b = "]" if selected else " "
                        tier_style = _C_NEON_PINK if selected else _C_SECONDARY_TEXT
                        body.append(f"{open_b}{tier['tier']}{close_b} ", style=tier_style)
                    body.append("\n", style=_C_SECONDARY_TEXT)
        if selected_models or selected_summary:
            body.append("\n", style=_C_SECONDARY_TEXT)
            for line in selected_models:
                body.append(f"  {line}\n", style=_C_SECONDARY_TEXT)
            if selected_summary:
                body.append(f"  {selected_summary}\n", style=_C_SECONDARY_TEXT)

        body.append("\n  ↑/↓ workflow  ENTER open/confirm  ←/→ tier  ESC back", style=_C_SECONDARY_TEXT)

        footer_table = Table.grid(padding=(0, 1))
        footer_table.add_column(style=_C_NEON_PINK, no_wrap=True)
        footer_table.add_column(style=_C_SECONDARY_TEXT)

        return Panel(
            Group(body, footer_table),
            title="[bold #ff2d6f]Mode Select[/bold #ff2d6f]",
            border_style=_C_BORDER,
            padding=(0, 2),
        )

    @staticmethod
    def _normalize_key(key: str) -> str:
        if key in ("\r", "\n"):
            return "enter"
        return key.lower()

    @staticmethod
    def _group_by_base(modes: List[dict[str, Any]]) -> List[dict[str, Any]]:
        bases: Dict[str, dict[str, Any]] = {}

        def split_base_tier(key: str) -> tuple[str, str | None]:
            parts = key.rsplit("_", 1)
            if len(parts) == 2 and parts[1] in {"lite", "balanced", "max"}:
                return parts[0], parts[1]
            return key, None

        for mode in modes:
            mkey = mode.get("key", "")
            base_key, tier = split_base_tier(mkey)
            label = mode.get("label", base_key)
            base_label = label.split("(")[0].strip() if "(" in label else label
            entry = bases.setdefault(
                base_key,
                {
                    "base_key": base_key,
                    "label": base_label,
                    "summary": mode.get("summary", ""),
                    "tiers": [],
                },
            )
            entry["tiers"].append(
                {
                    "key": mkey,
                    "tier": tier or "only",
                    "model_line": (mode.get("model_lines") or [""])[0],
                }
            )

        # Sort tiers in logical order
        def tier_order(t: dict[str, Any]) -> int:
            order = {"lite": 0, "balanced": 1, "max": 2, "only": 3}
            return order.get(t["tier"], 99)

        for base in bases.values():
            base["tiers"] = sorted(base["tiers"], key=tier_order)

        return list(bases.values())
