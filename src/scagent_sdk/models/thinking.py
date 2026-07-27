"""Model reasoning ("thinking") configuration and layered resolution.

Reasoning splits into two independent concerns, mirroring how a model actually
behaves:

* **Generation** — whether, and how hard, the model reasons before answering.
  Maps to the Claude Agent SDK's ``thinking`` and ``effort`` options.
* **Presentation** — whether the terminal shows that reasoning (dimmed) and
  whether it is persisted to the session's reasoning log.

Defaults live in the model-profile TOML (``[thinking]``); environment variables
(``SCAGENT_SDK_THINKING*``) override the profile, and CLI flags override the
environment. Keeping behavioural config in the profile — not scattered across a
secrets ``.env`` — gives a single declarative source of truth that a future
front-end can read and edit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from scagent_sdk.errors import ModelProfileError

# ``native`` means "inject nothing" — defer entirely to the backend/gateway
# default. It preserves the exact behaviour of a runtime that predates thinking
# control, and is the right choice for local models whose reasoning is steered
# server-side (e.g. Qwen leaking ``<think>`` blocks into content).
MODES = ("enabled", "disabled", "adaptive", "native")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DISPLAYS = ("summarized", "omitted")

DEFAULT_BUDGET_TOKENS = 8000
# The Claude API rejects a thinking budget below 1024 tokens.
MIN_BUDGET_TOKENS = 1024

_TRUE = {"1", "true", "yes", "on", "enabled", "enable"}
_FALSE = {"0", "false", "no", "off", "disabled", "disable"}


def _coerce_bool(value: str, *, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ModelProfileError(f"{name} must be a boolean-like value, got {value!r}")


def _coerce_mode(value: str, *, name: str) -> str:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return "enabled"
    if lowered in _FALSE:
        return "disabled"
    if lowered in MODES:
        return lowered
    raise ModelProfileError(f"{name} must be one of {MODES} (or a boolean), got {value!r}")


@dataclass(frozen=True)
class ThinkingSettings:
    """Resolved reasoning configuration for one session."""

    mode: str = "enabled"
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    effort: str | None = None
    display: str | None = None
    show: bool = True
    save: bool = True

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ModelProfileError(f"thinking.mode must be one of {MODES}, got {self.mode!r}")
        if not isinstance(self.budget_tokens, int) or isinstance(self.budget_tokens, bool):
            raise ModelProfileError("thinking.budget_tokens must be an integer")
        if self.budget_tokens < MIN_BUDGET_TOKENS:
            raise ModelProfileError(
                f"thinking.budget_tokens must be >= {MIN_BUDGET_TOKENS}, got {self.budget_tokens}"
            )
        if self.effort is not None and self.effort not in EFFORTS:
            raise ModelProfileError(
                f"thinking.effort must be one of {EFFORTS}, got {self.effort!r}"
            )
        if self.display is not None and self.display not in DISPLAYS:
            raise ModelProfileError(
                f"thinking.display must be one of {DISPLAYS}, got {self.display!r}"
            )

    @property
    def generation_enabled(self) -> bool:
        """True when the model is asked to reason (as opposed to explicit off)."""

        return self.mode in {"enabled", "adaptive"}

    def to_claude_options(self) -> dict[str, Any]:
        """Return the ``ClaudeAgentOptions`` fragment for this configuration.

        ``native`` injects nothing so the backend keeps its own default. Every
        other mode is explicit in both directions — matching the design rule
        that a floor-bearing runtime never relies on an implicit server default.
        """

        if self.mode == "native":
            return {}
        thinking: dict[str, Any]
        if self.mode == "enabled":
            thinking = {"type": "enabled", "budget_tokens": self.budget_tokens}
        elif self.mode == "adaptive":
            thinking = {"type": "adaptive"}
        else:  # disabled
            thinking = {"type": "disabled"}
        if self.display is not None and self.mode != "disabled":
            thinking["display"] = self.display
        options: dict[str, Any] = {"thinking": thinking}
        if self.effort is not None:
            options["effort"] = self.effort
        return options

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "budget_tokens": self.budget_tokens,
            "effort": self.effort,
            "display": self.display,
            "show": self.show,
            "save": self.save,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ThinkingSettings:
        """Build settings from a profile ``[thinking]`` table (defaults fill gaps)."""

        if not isinstance(data, Mapping):
            raise ModelProfileError("thinking must be a table")
        defaults = cls()
        raw_mode = data.get("mode", defaults.mode)
        mode = _coerce_mode(str(raw_mode), name="thinking.mode")
        budget = data.get("budget_tokens", defaults.budget_tokens)
        effort = data.get("effort", defaults.effort)
        display = data.get("display", defaults.display)
        show = data.get("show", defaults.show)
        save = data.get("save", defaults.save)
        return cls(
            mode=mode,
            budget_tokens=int(budget),
            effort=None if effort is None else str(effort),
            display=None if display is None else str(display),
            show=bool(show),
            save=bool(save),
        )


def apply_env(base: ThinkingSettings, environ: Mapping[str, str]) -> ThinkingSettings:
    """Override ``base`` with any ``SCAGENT_SDK_THINKING*`` variables that are set."""

    changes: dict[str, Any] = {}
    if (value := environ.get("SCAGENT_SDK_THINKING")) is not None and value.strip():
        changes["mode"] = _coerce_mode(value, name="SCAGENT_SDK_THINKING")
    if (value := environ.get("SCAGENT_SDK_THINKING_BUDGET")) is not None and value.strip():
        try:
            changes["budget_tokens"] = int(value)
        except ValueError as exc:
            raise ModelProfileError(
                f"SCAGENT_SDK_THINKING_BUDGET must be an integer, got {value!r}"
            ) from exc
    if (value := environ.get("SCAGENT_SDK_THINKING_EFFORT")) is not None and value.strip():
        changes["effort"] = value.strip().lower()
    if (value := environ.get("SCAGENT_SDK_THINKING_DISPLAY")) is not None and value.strip():
        changes["display"] = value.strip().lower()
    if (value := environ.get("SCAGENT_SDK_SHOW_THINKING")) is not None and value.strip():
        changes["show"] = _coerce_bool(value, name="SCAGENT_SDK_SHOW_THINKING")
    if (value := environ.get("SCAGENT_SDK_SAVE_THINKING")) is not None and value.strip():
        changes["save"] = _coerce_bool(value, name="SCAGENT_SDK_SAVE_THINKING")
    return replace(base, **changes) if changes else base


def apply_overrides(
    base: ThinkingSettings,
    *,
    mode: str | None = None,
    budget_tokens: int | None = None,
    effort: str | None = None,
    show: bool | None = None,
    save: bool | None = None,
) -> ThinkingSettings:
    """Override ``base`` with explicit values (e.g. CLI flags). ``None`` means unset."""

    changes: dict[str, Any] = {}
    if mode is not None:
        changes["mode"] = _coerce_mode(mode, name="--thinking")
    if budget_tokens is not None:
        changes["budget_tokens"] = budget_tokens
    if effort is not None:
        changes["effort"] = effort
    if show is not None:
        changes["show"] = show
    if save is not None:
        changes["save"] = save
    return replace(base, **changes) if changes else base
