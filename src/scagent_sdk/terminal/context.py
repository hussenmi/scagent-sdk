"""Persistent bottom-right terminal display for live model context usage."""

from __future__ import annotations

from typing import Any


def format_token_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1000:
        return f"{value // 1000}K"
    return str(value)


def context_bar_text(usage: dict[str, Any], *, width: int = 10) -> str:
    used = usage.get("total_tokens")
    limit = usage.get("context_window_tokens")
    if (
        not isinstance(used, int)
        or isinstance(used, bool)
        or used < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
    ):
        return ""
    ratio = min(max(used / limit, 0.0), 1.0)
    filled = min(width, int(ratio * width))
    bar = "▓" * filled + "░" * (width - filled)
    return (
        f"{bar} {ratio:.0%} · "
        f"{format_token_count(used)}/{format_token_count(limit)}"
    )


def context_toolbar_text(usage: dict[str, Any], *, columns: int) -> str:
    """Return a full-width, right-aligned prompt toolbar for the available columns."""

    text = context_bar_text(usage)
    if not text or columns < 1:
        return ""
    bar = f" {text} "
    if len(bar) > columns:
        used = usage["total_tokens"]
        limit = usage["context_window_tokens"]
        ratio = min(max(used / limit, 0.0), 1.0)
        bar = (
            f" {ratio:.0%} "
            f"{format_token_count(used)}/{format_token_count(limit)} "
        )
    if len(bar) > columns:
        return ""
    return " " * (columns - len(bar)) + bar
