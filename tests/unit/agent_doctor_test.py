from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from scagent_sdk.doctor.agent import AVAILABLE_PROBES, AgentCompatibilityDoctor
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.protocol import RuntimeMessage, RuntimeResponse


class HookMatcher:
    def __init__(self, *, matcher, hooks):
        self.matcher = matcher
        self.hooks = hooks


def tool(name, description, input_schema):
    del description, input_schema

    def decorate(handler):
        return SimpleNamespace(name=name, handler=handler)

    return decorate


def create_sdk_mcp_server(name, *, tools):
    return {"name": name, "tools": tools}


def _profile(tmp_path: Path) -> ModelProfile:
    prompt = tmp_path / "system.md"
    prompt.write_text("system", encoding="utf-8")
    return ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )


def test_all_agent_compatibility_evaluators_pass_with_conforming_runtime(
    tmp_path: Path,
) -> None:
    sdk = SimpleNamespace(
        tool=tool,
        create_sdk_mcp_server=create_sdk_mcp_server,
        HookMatcher=HookMatcher,
    )

    async def executor(prompt, extensions):
        if not extensions.mcp_servers:
            token = re.search(r"TEXT-[a-f0-9]+", prompt).group(0)
            return _response(token, ())
        server = next(iter(extensions.mcp_servers.values()))
        selected_tool = server["tools"][0]
        full_name = f"mcp__doctor__{selected_tool.name}"
        for matcher in extensions.hooks.get("PreToolUse", []):
            for hook in matcher.hooks:
                await hook({}, "use-1", {})
        result = await selected_tool.handler({})
        if result.get("is_error"):
            result = await selected_tool.handler({})
        for matcher in extensions.hooks.get("PostToolUse", []):
            for hook in matcher.hooks:
                await hook({}, "use-1", {})
        result_text = result["content"][0]["text"]
        marker = re.search(r"(?:TOOL|REASON|RETRY|LONG)-[a-f0-9]+", result_text)
        final_text = marker.group(0) if marker else result_text
        messages = (
            RuntimeMessage("thinking", "reason"),
            RuntimeMessage("tool_use", {"id": "use-1", "name": full_name, "input": {}}),
        )
        return _response(final_text, messages)

    doctor = AgentCompatibilityDoctor(
        _profile(tmp_path),
        sessions_root=tmp_path / "sessions",
        cwd=tmp_path,
        executor=executor,
        sdk_module=sdk,
        long_result_chars=1_024,
    )

    results = asyncio.run(doctor.run())

    assert [result.name for result in results] == list(AVAILABLE_PROBES)
    assert {result.status for result in results} == {"pass"}


def _response(text: str, messages: tuple[RuntimeMessage, ...]) -> RuntimeResponse:
    return RuntimeResponse(
        runtime_session_id="sdk-session",
        messages=messages,
        final_text=text,
        stop_reason="end_turn",
        is_error=False,
        subtype="success",
    )
