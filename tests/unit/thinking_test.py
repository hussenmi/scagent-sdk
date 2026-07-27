from __future__ import annotations

import pytest

from scagent_sdk.errors import ModelProfileError
from scagent_sdk.models.thinking import (
    ThinkingSettings,
    apply_env,
    apply_overrides,
)


def test_enabled_maps_to_budget_and_disabled_is_explicit() -> None:
    assert ThinkingSettings().to_claude_options() == {
        "thinking": {"type": "enabled", "budget_tokens": 8000}
    }
    assert ThinkingSettings(mode="disabled").to_claude_options() == {
        "thinking": {"type": "disabled"}
    }


def test_native_injects_nothing() -> None:
    assert ThinkingSettings(mode="native").to_claude_options() == {}


def test_adaptive_carries_display_and_effort_is_independent() -> None:
    settings = ThinkingSettings(mode="adaptive", display="summarized", effort="high")
    assert settings.to_claude_options() == {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "effort": "high",
    }


def test_disabled_drops_display() -> None:
    # display only makes sense while reasoning is emitted.
    assert ThinkingSettings(mode="disabled", display="summarized").to_claude_options() == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "sideways"},
        {"budget_tokens": 512},
        {"budget_tokens": True},
        {"effort": "extreme"},
        {"display": "verbose"},
    ],
)
def test_invalid_settings_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ModelProfileError):
        ThinkingSettings(**kwargs)


def test_from_mapping_fills_defaults_and_coerces_booleans() -> None:
    settings = ThinkingSettings.from_mapping({"mode": "on", "show": False})
    assert settings.mode == "enabled"
    assert settings.show is False
    assert settings.budget_tokens == 8000  # default preserved


def test_apply_env_overrides_and_precedence() -> None:
    base = ThinkingSettings(mode="native", show=True, save=True)
    env = {
        "SCAGENT_SDK_THINKING": "on",
        "SCAGENT_SDK_THINKING_BUDGET": "12000",
        "SCAGENT_SDK_THINKING_EFFORT": "high",
        "SCAGENT_SDK_SHOW_THINKING": "0",
    }
    resolved = apply_env(base, env)
    assert resolved.mode == "enabled"
    assert resolved.budget_tokens == 12000
    assert resolved.effort == "high"
    assert resolved.show is False
    assert resolved.save is True  # untouched by env


def test_apply_env_ignores_blank_values() -> None:
    base = ThinkingSettings(mode="enabled")
    assert apply_env(base, {"SCAGENT_SDK_THINKING": "  "}) is base


def test_apply_env_rejects_non_integer_budget() -> None:
    with pytest.raises(ModelProfileError):
        apply_env(ThinkingSettings(), {"SCAGENT_SDK_THINKING_BUDGET": "lots"})


def test_apply_overrides_cli_wins_and_none_is_noop() -> None:
    base = ThinkingSettings(mode="enabled", show=True)
    assert apply_overrides(base) is base
    resolved = apply_overrides(base, mode="off", show=False)
    assert resolved.mode == "disabled"
    assert resolved.show is False
