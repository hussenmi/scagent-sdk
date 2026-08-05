from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scagent_sdk.errors import ContextRolloverRequired, RuntimeExecutionError
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.models.thinking import ThinkingSettings
from scagent_sdk.runtime.claude import ClaudeAgentSDKBackend, ClaudeRuntimeExtensions
from scagent_sdk.runtime.observer import NullRuntimeObserver
from scagent_sdk.runtime.protocol import RuntimeRequest


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: object
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list


@dataclass
class ResultMessage:
    session_id: str = "sdk-session"
    result: str = "final"
    stop_reason: str = "end_turn"
    is_error: bool = False
    subtype: str = "success"
    usage: dict | None = None
    model_usage: dict | None = None
    total_cost_usd: float | None = None


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeClient:
    last_options = None
    last_prompt = None
    context_calls = 0

    def __init__(self, *, options):
        type(self).last_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def query(self, prompt):
        type(self).last_prompt = prompt

    async def get_context_usage(self):
        type(self).context_calls += 1
        return {
            "totalTokens": 100_000,
            "maxTokens": 230_000,
            "rawMaxTokens": 200_000,
            "percentage": 38.1,
            "model": "Qwen3.6-27B",
            "isAutoCompactEnabled": True,
        }

    async def receive_response(self):
        yield AssistantMessage(
            [
                ThinkingBlock("reason"),
                TextBlock("answer"),
                ToolUseBlock("tool-1", "inspect", {"path": "a.h5ad"}),
            ]
        )
        yield ResultMessage(usage={"input_tokens": 4})


def _sdk():
    return SimpleNamespace(
        ClaudeAgentOptions=ClaudeAgentOptions,
        ClaudeSDKClient=FakeClient,
        TextBlock=TextBlock,
        ThinkingBlock=ThinkingBlock,
        ToolUseBlock=ToolUseBlock,
        ToolResultBlock=ToolResultBlock,
        AssistantMessage=AssistantMessage,
        ResultMessage=ResultMessage,
    )


def test_claude_backend_builds_isolated_resumable_sdk_options(tmp_path: Path) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        small_fast_model="fast",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
        resume_session_id="sdk-old",
        fork_session=True,
        context_window_tokens=262_144,
        context_limit_source="upstream:models",
    )

    response = asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    options = FakeClient.last_options
    assert options.system_prompt == "System"
    assert options.resume == "sdk-old"
    assert options.fork_session is True
    assert options.tools == []
    assert options.strict_mcp_config is True
    assert options.skills == []
    assert options.env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "183500"
    assert response.runtime_session_id == "sdk-session"
    assert [message.kind for message in response.messages] == ["thinking", "text", "tool_use"]
    assert response.context_usage == {
        "total_tokens": 100_000,
        "context_window_tokens": 262_144,
        "sdk_raw_context_window_tokens": 200_000,
        "effective_limit_tokens": 230_000,
        "percentage": pytest.approx(38.14697),
        "model": "Qwen3.6-27B",
        "auto_compact_enabled": True,
        "auto_compact_threshold_tokens": None,
        "source": "claude-agent-sdk:get_context_usage",
        "context_window_source": "upstream:models",
    }
    assert response.to_dict()["context_usage"]["total_tokens"] == 100_000


def test_claude_backend_applies_only_explicit_extensions(tmp_path: Path) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
        skills=("profile-skill",),
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )
    hooks = {"PreToolUse": [object()]}
    backend = ClaudeAgentSDKBackend(
        sdk_module=_sdk(),
        extensions=ClaudeRuntimeExtensions(
            mcp_servers={"science": {"type": "sdk"}},
            hooks=hooks,
            tools=("Skill",),
            allowed_tools=("mcp__science__inspect",),
            disallowed_tools=("Bash",),
            skills=("extension-skill",),
            include_hook_events=True,
            system_prompt_suffix="## Local prerequisites on this host\n\n- ready",
        ),
    )

    asyncio.run(backend.execute(request))

    options = FakeClient.last_options
    assert options.system_prompt == "System\n\n## Local prerequisites on this host\n\n- ready"
    assert options.mcp_servers == {"science": {"type": "sdk"}}
    assert options.hooks == hooks
    assert options.tools == ["Skill"]
    assert options.allowed_tools == ["mcp__science__inspect"]
    assert options.disallowed_tools == ["Bash"]
    assert options.skills == ["extension-skill"]
    assert options.include_hook_events is True


def test_sdk_stderr_filters_only_known_connector_notice(capsys) -> None:
    ClaudeAgentSDKBackend._sdk_stderr(
        "claude.ai connectors are disabled because an API key takes precedence"
    )
    ClaudeAgentSDKBackend._sdk_stderr("actionable runtime diagnostic")

    assert capsys.readouterr().err == "actionable runtime diagnostic\n"


def _thinking_request(tmp_path: Path, thinking: ThinkingSettings) -> RuntimeRequest:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
        thinking=thinking,
    )
    return RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )


def test_claude_backend_injects_profile_thinking_options(tmp_path: Path) -> None:
    request = _thinking_request(
        tmp_path, ThinkingSettings(mode="enabled", budget_tokens=12000, effort="high")
    )

    asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    options = FakeClient.last_options
    assert options.thinking == {"type": "enabled", "budget_tokens": 12000}
    assert options.effort == "high"


def test_claude_backend_native_thinking_injects_nothing(tmp_path: Path) -> None:
    request = _thinking_request(tmp_path, ThinkingSettings(mode="native"))

    asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    options = FakeClient.last_options
    # "native" defers entirely to the backend: no thinking/effort keys are set.
    assert getattr(options, "thinking", None) is None
    assert getattr(options, "effort", None) is None


@dataclass
class SystemMessage:
    subtype: str
    data: dict


class SlowClient:
    """A client whose turn never ends on its own, and that answers interrupt()."""

    instances: list = []

    def __init__(self, *, options):
        self.options = options
        self.interrupted = asyncio.Event()
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def query(self, prompt):
        return None

    async def interrupt(self):
        self.interrupted.set()

    async def receive_response(self):
        yield SystemMessage("init", {"session_id": "sdk-live"})
        yield AssistantMessage([TextBlock("partial answer")])
        await self.interrupted.wait()
        # A stopped turn ends without a ResultMessage.


def _slow_sdk():
    sdk = _sdk()
    sdk.ClaudeSDKClient = SlowClient
    sdk.SystemMessage = SystemMessage
    return sdk


def _plain_request(tmp_path: Path) -> RuntimeRequest:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    return RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )


def test_interrupt_ends_the_turn_without_a_result_message(tmp_path: Path) -> None:
    SlowClient.instances.clear()
    backend = ClaudeAgentSDKBackend(sdk_module=_slow_sdk())

    async def scenario():
        turn = asyncio.ensure_future(backend.execute(_plain_request(tmp_path)))
        while not SlowClient.instances:
            await asyncio.sleep(0)
        assert await backend.interrupt() is True
        return await turn

    response = asyncio.run(scenario())

    assert response.interrupted is True
    assert response.is_error is False
    assert response.subtype == "interrupted"
    # The session ID seen on the wire is kept, so the next turn can resume this conversation.
    assert response.runtime_session_id == "sdk-live"
    assert response.final_text == "partial answer"


def test_interrupt_without_a_running_turn_reports_nothing_to_stop(tmp_path: Path) -> None:
    backend = ClaudeAgentSDKBackend(sdk_module=_slow_sdk())

    assert asyncio.run(backend.interrupt()) is False


def test_cancelled_turn_keeps_the_runtime_session_it_reached(tmp_path: Path) -> None:
    SlowClient.instances.clear()
    backend = ClaudeAgentSDKBackend(sdk_module=_slow_sdk())

    async def scenario():
        turn = asyncio.ensure_future(backend.execute(_plain_request(tmp_path)))
        while not SlowClient.instances:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(scenario())

    assert backend.last_runtime_session_id == "sdk-live"


def test_runtime_frame_limit_exceeds_the_capability_media_budget(tmp_path: Path) -> None:
    """Figure pixels must not overflow the transport that carries them.

    The SDK's default 1 MiB stdout frame is smaller than a few normal scientific figures, and
    exceeding it fails the entire turn rather than one tool call.
    """

    from scagent_sdk.capabilities.results import MODEL_MEDIA_TOTAL_BYTES
    from scagent_sdk.runtime.claude import MAX_RUNTIME_MESSAGE_BYTES

    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )

    asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    assert FakeClient.last_options.max_buffer_size == MAX_RUNTIME_MESSAGE_BYTES
    # Base64 inflates by 4/3 and the CLI replays results in its own frames.
    assert MAX_RUNTIME_MESSAGE_BYTES > 2 * MODEL_MEDIA_TOTAL_BYTES


def test_skill_plugins_are_passed_through_without_enabling_project_settings(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )
    backend = ClaudeAgentSDKBackend(
        sdk_module=_sdk(),
        extensions=ClaudeRuntimeExtensions(
            tools=("Skill",),
            skills=("celltypist-annotation",),
            plugins=({"type": "local", "path": str(tmp_path / "plugin")},),
        ),
    )

    asyncio.run(backend.execute(request))

    options = FakeClient.last_options
    assert options.tools == ["Skill"]
    assert options.plugins == [{"type": "local", "path": str(tmp_path / "plugin")}]
    # Project settings stay off: they would import CLAUDE.md/AGENTS.md into a science session.
    assert options.setting_sources == []


def test_exact_resume_preflight_uses_most_conservative_live_limit_before_query(
    tmp_path: Path,
) -> None:
    class ContextClient(FakeClient):
        queried = False

        async def get_context_usage(self):
            return {
                "totalTokens": 203_613,
                "maxTokens": 200_000,
                "rawMaxTokens": 262_144,
            }

        async def query(self, prompt):
            type(self).queried = True

    sdk = _sdk()
    sdk.ClaudeSDKClient = ContextClient
    request = _plain_request(tmp_path)
    request = RuntimeRequest(
        **{
            **request.__dict__,
            "resume_session_id": "sdk-old",
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_000,
            "context_limit_source": "upstream:models",
        }
    )

    with pytest.raises(ContextRolloverRequired) as captured:
        asyncio.run(ClaudeAgentSDKBackend(sdk_module=sdk).execute(request))

    assert captured.value.context_window_tokens == 200_000
    assert captured.value.total_tokens == 203_613
    assert captured.value.source == "claude-agent-sdk:maxTokens"
    assert ContextClient.queried is False


def test_embedded_reasoning_is_separated_from_visible_final_text(tmp_path: Path) -> None:
    class ReasoningClient(FakeClient):
        async def receive_response(self):
            text = "<think>private reasoning</think>\n\nVisible answer"
            yield AssistantMessage([TextBlock(text)])
            yield ResultMessage(result=text)

    sdk = _sdk()
    sdk.ClaudeSDKClient = ReasoningClient

    response = asyncio.run(ClaudeAgentSDKBackend(sdk_module=sdk).execute(_plain_request(tmp_path)))

    assert response.final_text == "Visible answer"
    assert [(item.kind, item.content) for item in response.messages] == [
        ("thinking", "private reasoning"),
        ("text", "Visible answer"),
    ]
    assert "<think>" not in response.to_dict()["final_text"]


def test_textual_tool_call_is_retried_natively_instead_of_becoming_final_text(
    tmp_path: Path,
) -> None:
    class RecoveryClient(FakeClient):
        prompts: list[str] = []
        query_count = 0

        async def query(self, prompt):
            type(self).prompts.append(prompt)
            type(self).query_count += 1

        async def receive_response(self):
            if type(self).query_count == 1:
                raw = (
                    "<think>need the table</think>"
                    "<tool_call><function=mcp__workspace__read_text_file>"
                    "<parameter=path>/tmp/table.csv</parameter></function></tool_call>"
                )
                yield AssistantMessage([TextBlock(raw)])
                yield ResultMessage(result=raw)
                return
            yield AssistantMessage([TextBlock("Review continued after the native call.")])
            yield ResultMessage(result="Review continued after the native call.")

    sdk = _sdk()
    sdk.ClaudeSDKClient = RecoveryClient

    response = asyncio.run(ClaudeAgentSDKBackend(sdk_module=sdk).execute(_plain_request(tmp_path)))

    assert response.final_text == "Review continued after the native call."
    assert len(RecoveryClient.prompts) == 2
    assert "native tool interface" in RecoveryClient.prompts[1]
    assert all("<tool_call>" not in str(item.content) for item in response.messages)
    assert any(item.kind == "textual_tool_call" for item in response.messages)


def test_repeated_textual_tool_call_fails_closed(tmp_path: Path) -> None:
    class BrokenClient(FakeClient):
        query_count = 0

        async def query(self, prompt):
            type(self).query_count += 1

        async def receive_response(self):
            raw = "<tool_call>{\"name\":\"inspect\",\"arguments\":{}}</tool_call>"
            yield AssistantMessage([TextBlock(raw)])
            yield ResultMessage(result=raw)

    sdk = _sdk()
    sdk.ClaudeSDKClient = BrokenClient

    with pytest.raises(RuntimeExecutionError, match="repeatedly emitted"):
        asyncio.run(ClaudeAgentSDKBackend(sdk_module=sdk).execute(_plain_request(tmp_path)))

    assert BrokenClient.query_count == 2


def test_recoverable_context_error_is_not_rendered_as_a_final_response(
    tmp_path: Path,
) -> None:
    class ContextErrorClient(FakeClient):
        async def receive_response(self):
            yield ResultMessage(
                result="API Error: input length exceeds maximum context length",
                is_error=True,
            )

    class Observer(NullRuntimeObserver):
        def __init__(self):
            self.finished = []

        def on_runtime_finished(self, response):
            self.finished.append(response)

    sdk = _sdk()
    sdk.ClaudeSDKClient = ContextErrorClient
    observer = Observer()

    response = asyncio.run(
        ClaudeAgentSDKBackend(sdk_module=sdk, observer=observer).execute(
            _plain_request(tmp_path)
        )
    )

    assert response.is_error is True
    assert observer.finished == []
