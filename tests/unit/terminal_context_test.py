from __future__ import annotations

from scagent_sdk.terminal.context import (
    context_bar_text,
    context_toolbar_text,
    format_token_count,
)


def test_context_bar_formats_actual_raw_window_usage() -> None:
    text = context_bar_text(
        {
            "total_tokens": 100_000,
            "context_window_tokens": 262_144,
        }
    )

    assert text == "▓▓▓░░░░░░░ 38% · 100K/262K"


def test_context_bar_reflects_lower_usage_after_rollover() -> None:
    before = context_bar_text(
        {"total_tokens": 230_145, "context_window_tokens": 262_144}
    )
    after = context_bar_text(
        {"total_tokens": 24_000, "context_window_tokens": 262_144}
    )

    assert before.startswith("▓▓▓▓▓▓▓▓░░ 88%")
    assert after.startswith("░░░░░░░░░░ 9%")


def test_context_bar_uses_compact_count_units() -> None:
    assert format_token_count(999) == "999"
    assert format_token_count(262_144) == "262K"
    assert format_token_count(1_000_000) == "1M"
    assert format_token_count(1_500_000) == "1.5M"


def test_context_toolbar_is_full_width_and_right_aligned() -> None:
    toolbar = context_toolbar_text(
        {"total_tokens": 100_000, "context_window_tokens": 262_144},
        columns=80,
    )

    assert len(toolbar) == 80
    assert toolbar.endswith(" 100K/262K ")
    assert toolbar.lstrip().startswith("▓▓▓░░░░░░░ 38%")


def test_context_toolbar_degrades_for_narrow_terminals() -> None:
    usage = {"total_tokens": 100_000, "context_window_tokens": 262_144}

    compact = context_toolbar_text(usage, columns=20)
    hidden = context_toolbar_text(usage, columns=8)

    assert len(compact) == 20
    assert compact.endswith(" 38% 100K/262K ")
    assert hidden == ""
