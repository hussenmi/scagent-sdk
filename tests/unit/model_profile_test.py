from __future__ import annotations

from pathlib import Path

import pytest

from scagent_sdk.errors import ModelProfileError
from scagent_sdk.models.profile import ModelProfile, ModelProfileRegistry


def _write_profile(root: Path, *, name: str = "local-test", model: str = "primary") -> Path:
    prompts = root / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "base.md").write_text("A short test prompt.\n", encoding="utf-8")
    path = root / f"{name}.toml"
    path.write_text(
        f"""schema_version = 1
[profile]
name = "{name}"
runtime = "claude-agent-sdk"
transport = "litellm"
model = "{model}"
small_fast_model = "fast"
base_url = "http://127.0.0.1:4000"
api_key_env = "TEST_GATEWAY_KEY"
allow_noauth = true
system_prompt = "prompts/base.md"
max_turns = 12
skills = []
[health]
paths = ["/health"]
[environment]
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
""",
        encoding="utf-8",
    )
    return path


def test_profile_loads_prompt_and_has_stable_secret_free_fingerprint(tmp_path: Path) -> None:
    path = _write_profile(tmp_path)
    profile = ModelProfile.from_toml(path)

    assert profile.read_system_prompt() == "A short test prompt."
    assert profile.resolve_api_key({}) == "sk-local-noauth"
    assert profile.runtime_environment({})["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert profile.fingerprint.startswith("sha256:")
    assert profile.fingerprint == ModelProfile.from_toml(path).fingerprint


def test_profile_fingerprint_changes_when_runtime_compatibility_changes(tmp_path: Path) -> None:
    first = ModelProfile.from_toml(_write_profile(tmp_path / "a", model="model-a"))
    second = ModelProfile.from_toml(_write_profile(tmp_path / "b", model="model-b"))

    assert first.fingerprint != second.fingerprint


def test_unset_context_fields_do_not_invalidate_existing_profile_fingerprint(
    tmp_path: Path,
) -> None:
    profile = ModelProfile.from_toml(_write_profile(tmp_path))

    assert "context_window_fallback" not in profile.to_dict()
    assert "max_output_tokens" not in profile.to_dict()


def test_registry_lists_and_loads_profiles(tmp_path: Path) -> None:
    _write_profile(tmp_path, name="b")
    _write_profile(tmp_path, name="a")
    registry = ModelProfileRegistry(tmp_path)

    assert [profile.name for profile in registry.list()] == ["a", "b"]
    assert registry.load("a").model == "primary"


def test_profile_rejects_invalid_gateway_url(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    with pytest.raises(ModelProfileError, match="absolute HTTP"):
        ModelProfile(
            name="bad",
            runtime="claude-agent-sdk",
            transport="litellm",
            model="m",
            system_prompt=str(prompt),
            base_url="localhost:4000",
            allow_noauth=True,
        )


def test_profile_validates_optional_context_fallback_and_output_reserve(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    with pytest.raises(ModelProfileError, match="smaller than"):
        ModelProfile(
            name="bad",
            runtime="claude-agent-sdk",
            transport="litellm",
            model="m",
            system_prompt=str(prompt),
            base_url="http://localhost:4000",
            allow_noauth=True,
            context_window_fallback=10_000,
            max_output_tokens=10_000,
        )


def test_profile_defaults_to_enabled_thinking_when_table_absent(tmp_path: Path) -> None:
    profile = ModelProfile.from_toml(_write_profile(tmp_path))
    assert profile.thinking.mode == "enabled"
    assert profile.thinking.to_claude_options() == {
        "thinking": {"type": "enabled", "budget_tokens": 8000}
    }


def test_profile_parses_thinking_table(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "base.md").write_text("p", encoding="utf-8")
    path = tmp_path / "native.toml"
    path.write_text(
        """schema_version = 1
[profile]
name = "native"
runtime = "claude-agent-sdk"
transport = "litellm"
model = "primary"
base_url = "http://127.0.0.1:4000"
allow_noauth = true
system_prompt = "prompts/base.md"
[thinking]
mode = "native"
show = true
save = false
""",
        encoding="utf-8",
    )
    profile = ModelProfile.from_toml(path)
    assert profile.thinking.mode == "native"
    assert profile.thinking.save is False
    assert profile.thinking.to_claude_options() == {}
    # thinking config participates in the secret-free fingerprint.
    assert "thinking" in profile.to_dict()


def test_profile_rejects_invalid_thinking_table(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "base.md").write_text("p", encoding="utf-8")
    path = tmp_path / "bad.toml"
    path.write_text(
        """schema_version = 1
[profile]
name = "bad"
runtime = "claude-agent-sdk"
transport = "litellm"
model = "primary"
base_url = "http://127.0.0.1:4000"
allow_noauth = true
system_prompt = "prompts/base.md"
[thinking]
budget_tokens = 100
""",
        encoding="utf-8",
    )
    with pytest.raises(ModelProfileError):
        ModelProfile.from_toml(path)
