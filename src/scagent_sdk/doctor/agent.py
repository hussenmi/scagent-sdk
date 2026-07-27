"""Live Claude Agent SDK compatibility probes for local model gateways."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from scagent_sdk.doctor.model import CheckResult
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.claude import ClaudeAgentSDKBackend, ClaudeRuntimeExtensions
from scagent_sdk.runtime.protocol import RuntimeResponse
from scagent_sdk.runtime.service import AgentRuntimeService
from scagent_sdk.session import AnalysisSession

ProbeExecutor = Callable[[str, ClaudeRuntimeExtensions], Awaitable[RuntimeResponse]]
AVAILABLE_PROBES = ("text", "tool_hooks", "reasoning_tool", "retry", "long_result")


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


class AgentCompatibilityDoctor:
    """Exercise model behavior the raw gateway health check cannot prove."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        sessions_root: str | Path,
        cwd: str | Path,
        long_result_chars: int = 49_152,
        executor: ProbeExecutor | None = None,
        sdk_module: Any | None = None,
    ):
        if long_result_chars < 1_024:
            raise ValueError("long_result_chars must be at least 1024")
        self.profile = profile
        self.sessions_root = Path(sessions_root).expanduser().resolve()
        self.cwd = Path(cwd).expanduser().resolve()
        self.long_result_chars = long_result_chars
        self._executor = executor
        self._sdk_module = sdk_module

    def _sdk(self) -> Any:
        if self._sdk_module is not None:
            return self._sdk_module
        return importlib.import_module("claude_agent_sdk")

    async def _execute(self, prompt: str, extensions: ClaudeRuntimeExtensions) -> RuntimeResponse:
        if self._executor is not None:
            return await self._executor(prompt, extensions)
        session = AnalysisSession.create(
            self.sessions_root,
            title=f"compatibility-probe-{uuid4().hex[:8]}",
        )
        service = AgentRuntimeService(
            ClaudeAgentSDKBackend(sdk_module=self._sdk_module, extensions=extensions)
        )
        return await service.run_turn(
            session,
            user_prompt=prompt,
            profile=self.profile,
            cwd=self.cwd,
        )

    async def text_check(self) -> CheckResult:
        nonce = f"TEXT-{uuid4().hex}"
        response = await self._execute(
            f"Reply with exactly this token and no other text: {nonce}",
            ClaudeRuntimeExtensions(),
        )
        if nonce not in response.final_text:
            return CheckResult(
                "text",
                "fail",
                "model response did not preserve the requested token",
                {"expected": nonce, "response": response.final_text[-500:]},
            )
        return CheckResult("text", "pass", "basic SDK text turn succeeded")

    async def tool_hooks_check(self) -> CheckResult:
        sdk = self._sdk()
        nonce = f"TOOL-{uuid4().hex}"
        counts = {"tool": 0, "pre": 0, "post": 0}

        async def doctor_echo_handler(_args: dict[str, Any]) -> dict[str, Any]:
            counts["tool"] += 1
            return _text_result(nonce)

        doctor_echo = sdk.tool("doctor_echo", "Return the compatibility token", {})(
            doctor_echo_handler
        )

        async def pre_hook(
            _input_data: dict[str, Any], _tool_use_id: str | None, _context: dict[str, Any]
        ) -> dict[str, Any]:
            counts["pre"] += 1
            return {}

        async def post_hook(
            _input_data: dict[str, Any], _tool_use_id: str | None, _context: dict[str, Any]
        ) -> dict[str, Any]:
            counts["post"] += 1
            return {}

        tool_name = "mcp__doctor__doctor_echo"
        extensions = ClaudeRuntimeExtensions(
            mcp_servers={"doctor": sdk.create_sdk_mcp_server("doctor", tools=[doctor_echo])},
            allowed_tools=(tool_name,),
            hooks={
                "PreToolUse": [sdk.HookMatcher(matcher=tool_name, hooks=[pre_hook])],
                "PostToolUse": [sdk.HookMatcher(matcher=tool_name, hooks=[post_hook])],
            },
            include_hook_events=True,
        )
        response = await self._execute(
            "You must call doctor_echo exactly once, then reply with only its token.",
            extensions,
        )
        used_tools = [
            message.content.get("name")
            for message in response.messages
            if message.kind == "tool_use" and isinstance(message.content, dict)
        ]
        passed = (
            counts == {"tool": 1, "pre": 1, "post": 1}
            and tool_name in used_tools
            and nonce in response.final_text
        )
        return CheckResult(
            "tool_hooks",
            "pass" if passed else "fail",
            "in-process tool and lifecycle hooks succeeded"
            if passed
            else "tool or hook lifecycle did not complete as expected",
            {
                "counts": counts,
                "used_tools": used_tools,
                "token_returned": nonce in response.final_text,
            },
        )

    async def reasoning_tool_check(self) -> CheckResult:
        sdk = self._sdk()
        nonce = f"REASON-{uuid4().hex}"

        async def doctor_reason_handler(_args: dict[str, Any]) -> dict[str, Any]:
            return _text_result(f"Evidence token: {nonce}")

        doctor_reason = sdk.tool("doctor_reason", "Return evidence needed to answer", {})(
            doctor_reason_handler
        )

        tool_name = "mcp__doctor__doctor_reason"
        response = await self._execute(
            "Use doctor_reason before answering. Return the evidence token.",
            ClaudeRuntimeExtensions(
                mcp_servers={"doctor": sdk.create_sdk_mcp_server("doctor", tools=[doctor_reason])},
                allowed_tools=(tool_name,),
            ),
        )
        kinds = [message.kind for message in response.messages]
        tool_worked = "tool_use" in kinds and nonce in response.final_text
        if not tool_worked:
            return CheckResult(
                "reasoning_tool",
                "fail",
                "model did not combine a required tool call with its answer",
                {"message_kinds": kinds},
            )
        if "thinking" not in kinds:
            return CheckResult(
                "reasoning_tool",
                "warn",
                "tool reasoning succeeded, but no separate thinking block was exposed",
                {"message_kinds": kinds, "note": "gateway may merge reasoning into text"},
            )
        return CheckResult(
            "reasoning_tool",
            "pass",
            "model emitted reasoning and used the required tool",
            {"message_kinds": kinds},
        )

    async def retry_check(self) -> CheckResult:
        sdk = self._sdk()
        nonce = f"RETRY-{uuid4().hex}"
        calls = 0

        async def doctor_flaky_handler(_args: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _text_result("transient compatibility error; retry", is_error=True)
            return _text_result(nonce)

        doctor_flaky = sdk.tool("doctor_flaky", "Fails once, then returns a token", {})(
            doctor_flaky_handler
        )

        tool_name = "mcp__doctor__doctor_flaky"
        response = await self._execute(
            "Call doctor_flaky. If it reports a transient error, retry it once. "
            "Then return only the success token.",
            ClaudeRuntimeExtensions(
                mcp_servers={"doctor": sdk.create_sdk_mcp_server("doctor", tools=[doctor_flaky])},
                allowed_tools=(tool_name,),
            ),
        )
        passed = calls >= 2 and nonce in response.final_text
        return CheckResult(
            "retry",
            "pass" if passed else "fail",
            "model recovered from a transient tool error"
            if passed
            else "model did not retry and recover from the transient tool error",
            {"tool_calls": calls, "token_returned": nonce in response.final_text},
        )

    async def long_result_check(self) -> CheckResult:
        sdk = self._sdk()
        nonce = f"LONG-{uuid4().hex}"
        payload = "x" * self.long_result_chars + f"\nFINAL_MARKER={nonce}"

        async def doctor_long_result_handler(_args: dict[str, Any]) -> dict[str, Any]:
            return _text_result(payload)

        doctor_long_result = sdk.tool(
            "doctor_long_result", "Return a long payload ending in a marker", {}
        )(doctor_long_result_handler)

        tool_name = "mcp__doctor__doctor_long_result"
        response = await self._execute(
            "Call doctor_long_result. Find FINAL_MARKER at the end and return only its value.",
            ClaudeRuntimeExtensions(
                mcp_servers={
                    "doctor": sdk.create_sdk_mcp_server("doctor", tools=[doctor_long_result])
                },
                allowed_tools=(tool_name,),
            ),
        )
        passed = nonce in response.final_text
        return CheckResult(
            "long_result",
            "pass" if passed else "fail",
            "long tool result reached the model without losing its tail marker"
            if passed
            else "model could not recover the tail marker from a long tool result",
            {"payload_chars": len(payload), "token_returned": passed},
        )

    async def run(self, checks: Sequence[str] = AVAILABLE_PROBES) -> list[CheckResult]:
        unknown = sorted(set(checks) - set(AVAILABLE_PROBES))
        if unknown:
            raise ValueError(f"unknown compatibility checks: {', '.join(unknown)}")
        results: list[CheckResult] = []
        for name in checks:
            try:
                check = getattr(self, f"{name}_check")
                results.append(await check())
            except Exception as exc:
                results.append(
                    CheckResult(
                        name,
                        "fail",
                        f"compatibility probe raised {type(exc).__name__}: {exc}",
                    )
                )
        return results
