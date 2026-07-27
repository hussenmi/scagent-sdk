"""Provider-neutral model configuration with stable compatibility fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomli

from scagent_sdk.errors import ModelProfileError, ModelProfileNotFoundError
from scagent_sdk.models.thinking import ThinkingSettings

MODEL_PROFILE_SCHEMA_VERSION = 1
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _string_list(value: Any, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ModelProfileError(f"{name} must be a list of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class ModelProfile:
    """Everything the runtime needs to select and reach a model, excluding secrets."""

    name: str
    runtime: str
    transport: str
    model: str
    system_prompt: str
    base_url: str | None = None
    api_key_env: str | None = None
    allow_noauth: bool = False
    small_fast_model: str | None = None
    max_turns: int = 40
    max_budget_usd: float | None = None
    context_window_fallback: int | None = None
    max_output_tokens: int | None = None
    skills: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    health_paths: tuple[str, ...] = ("/health/liveliness", "/health")
    thinking: ThinkingSettings = field(default_factory=ThinkingSettings)
    schema_version: int = MODEL_PROFILE_SCHEMA_VERSION
    source_path: Path | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_PROFILE_SCHEMA_VERSION:
            raise ModelProfileError(
                f"unsupported model-profile schema version: {self.schema_version}"
            )
        if not _PROFILE_NAME.fullmatch(self.name):
            raise ModelProfileError(
                "profile name must contain only lowercase letters, digits, '.', '_' or '-'"
            )
        for name, value in (
            ("runtime", self.runtime),
            ("transport", self.transport),
            ("model", self.model),
            ("system_prompt", self.system_prompt),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ModelProfileError(f"{name} must be a non-empty string")
        if self.max_turns < 1:
            raise ModelProfileError("max_turns must be positive")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ModelProfileError("max_budget_usd must be positive when provided")
        if self.context_window_fallback is not None and self.context_window_fallback < 1:
            raise ModelProfileError("context_window_fallback must be positive when provided")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ModelProfileError("max_output_tokens must be positive when provided")
        if (
            self.context_window_fallback is not None
            and self.max_output_tokens is not None
            and self.max_output_tokens >= self.context_window_fallback
        ):
            raise ModelProfileError("max_output_tokens must be smaller than the context window")
        if self.base_url is not None:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ModelProfileError("base_url must be an absolute HTTP(S) URL")
        if self.transport == "litellm" and self.base_url is None:
            raise ModelProfileError("LiteLLM profiles require base_url")
        if not self.allow_noauth and not self.api_key_env:
            raise ModelProfileError("api_key_env is required unless allow_noauth=true")
        if self.api_key_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.api_key_env):
            raise ModelProfileError("api_key_env must be an uppercase environment-variable name")
        if not self.health_paths:
            raise ModelProfileError("health_paths must contain at least one path")
        if any(not path.startswith("/") for path in self.health_paths):
            raise ModelProfileError("every health path must start with '/'")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise ModelProfileError("environment values must be strings")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(include_source=False),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @property
    def prompt_path(self) -> Path:
        candidate = Path(self.system_prompt)
        if candidate.is_absolute():
            return candidate
        if self.source_path is None:
            raise ModelProfileError("relative system_prompt needs a profile source path")
        return (self.source_path.parent / candidate).resolve()

    def read_system_prompt(self) -> str:
        try:
            prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ModelProfileError(f"cannot read system prompt {self.prompt_path}: {exc}") from exc
        if not prompt:
            raise ModelProfileError(f"system prompt is empty: {self.prompt_path}")
        return prompt

    def resolve_api_key(self, environ: dict[str, str] | None = None) -> str:
        source = os.environ if environ is None else environ
        if self.api_key_env:
            value = source.get(self.api_key_env)
            if value:
                return value
        if self.allow_noauth:
            return "sk-local-noauth"
        raise ModelProfileError(
            f"required API key environment variable is unset: {self.api_key_env}"
        )

    def runtime_environment(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        result = dict(self.environment)
        result["ANTHROPIC_API_KEY"] = self.resolve_api_key(environ)
        if self.base_url:
            result["ANTHROPIC_BASE_URL"] = self.base_url.rstrip("/")
        if self.small_fast_model:
            result["ANTHROPIC_SMALL_FAST_MODEL"] = self.small_fast_model
        return result

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "runtime": self.runtime,
            "transport": self.transport,
            "model": self.model,
            "small_fast_model": self.small_fast_model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "allow_noauth": self.allow_noauth,
            "system_prompt": self.system_prompt,
            "max_turns": self.max_turns,
            "max_budget_usd": self.max_budget_usd,
            "skills": list(self.skills),
            "health_paths": list(self.health_paths),
            "thinking": self.thinking.to_dict(),
            "environment": dict(sorted(self.environment.items())),
        }
        # Preserve fingerprints of profiles created before context-limit discovery existed.
        # These optional values affect compatibility only when an operator actually configures
        # them; absent values must not invalidate otherwise resumable runtime history.
        if self.context_window_fallback is not None:
            result["context_window_fallback"] = self.context_window_fallback
        if self.max_output_tokens is not None:
            result["max_output_tokens"] = self.max_output_tokens
        if include_source:
            result["source_path"] = str(self.source_path) if self.source_path else None
        return result

    @classmethod
    def from_toml(cls, path: str | Path) -> ModelProfile:
        source = Path(path).expanduser().resolve()
        try:
            with source.open("rb") as handle:
                data = tomli.load(handle)
        except FileNotFoundError as exc:
            raise ModelProfileNotFoundError(f"model profile not found: {source}") from exc
        except tomli.TOMLDecodeError as exc:
            raise ModelProfileError(f"invalid TOML in {source}: {exc}") from exc

        try:
            profile = data["profile"]
            if not isinstance(profile, dict):
                raise TypeError("profile must be a table")
            environment = data.get("environment", {})
            if not isinstance(environment, dict):
                raise TypeError("environment must be a table")
            health = data.get("health", {})
            if not isinstance(health, dict):
                raise TypeError("health must be a table")
            thinking = data.get("thinking", {})
            if not isinstance(thinking, dict):
                raise TypeError("thinking must be a table")
            return cls(
                schema_version=data["schema_version"],
                name=profile["name"],
                runtime=profile["runtime"],
                transport=profile["transport"],
                model=profile["model"],
                small_fast_model=profile.get("small_fast_model"),
                base_url=profile.get("base_url"),
                api_key_env=profile.get("api_key_env"),
                allow_noauth=profile.get("allow_noauth", False),
                system_prompt=profile["system_prompt"],
                max_turns=profile.get("max_turns", 40),
                max_budget_usd=profile.get("max_budget_usd"),
                context_window_fallback=profile.get("context_window_fallback"),
                max_output_tokens=profile.get("max_output_tokens"),
                skills=_string_list(profile.get("skills", []), name="profile.skills"),
                health_paths=_string_list(
                    health.get("paths", ["/health/liveliness", "/health"]),
                    name="health.paths",
                ),
                thinking=ThinkingSettings.from_mapping(thinking),
                environment={str(key): str(value) for key, value in environment.items()},
                source_path=source,
            )
        except (KeyError, TypeError) as exc:
            raise ModelProfileError(f"invalid model profile {source}: {exc}") from exc


class ModelProfileRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def path_for(self, name: str) -> Path:
        if not _PROFILE_NAME.fullmatch(name):
            raise ModelProfileError(f"invalid profile name: {name}")
        return self.root / f"{name}.toml"

    def load(self, name: str) -> ModelProfile:
        return ModelProfile.from_toml(self.path_for(name))

    def list(self) -> list[ModelProfile]:
        if not self.root.is_dir():
            return []
        return [ModelProfile.from_toml(path) for path in sorted(self.root.glob("*.toml"))]
