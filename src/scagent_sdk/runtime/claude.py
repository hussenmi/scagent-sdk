"""Claude Agent SDK implementation of the provider-neutral runtime protocol."""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from scagent_sdk.errors import ContextRolloverRequired, RuntimeExecutionError
from scagent_sdk.runtime.claude_store import ScientificSessionTranscriptStore
from scagent_sdk.runtime.model_text import split_model_text
from scagent_sdk.runtime.observer import NullRuntimeObserver, RuntimeObserver
from scagent_sdk.runtime.protocol import (
    RuntimeMessage,
    RuntimeRequest,
    RuntimeResponse,
)

# Headroom over the capability layer's per-result media budget (see
# scagent_sdk.capabilities.results.MODEL_MEDIA_TOTAL_BYTES), which is the limit that should
# actually bind. Base64 inflates payloads by 4/3, and the CLI replays tool results in its own
# frames, so this leaves room for several image-bearing results in one turn.
MAX_RUNTIME_MESSAGE_BYTES = 64 * 1024 * 1024
DEFAULT_OUTPUT_RESERVE_TOKENS = 32_000
MIN_CONTEXT_SAFETY_MARGIN_TOKENS = 4_096
AUTO_COMPACT_CONTEXT_FRACTION = 0.70
MAX_TEXTUAL_TOOL_CALL_RECOVERY_ATTEMPTS = 1
_TEXTUAL_TOOL_CALL_RECOVERY_PROMPT = (
    "Your previous response printed a tool call as text, so no tool was executed. Continue the "
    "same task from the current scientific state. If a tool is needed, invoke it through the "
    "native tool interface; never print <tool_call>, <tools>, or function protocol markup. Do "
    "not repeat work that durable state already records as complete."
)
_CONTEXT_ERROR_MARKERS = (
    "contextwindowexceeded",
    "context window exceeded",
    "maximum context length",
    "maximum context window",
    "prompt is too long",
    "too many input tokens",
    "input length",
)


@dataclass(frozen=True)
class ClaudeRuntimeExtensions:
    """Explicit SDK capabilities assembled outside the model backend.

    The default is intentionally empty. Capability discovery can later build one
    of these from installed skill packages without teaching this adapter any
    single-cell science or granting Claude Code's built-in tools implicitly.
    """

    mcp_servers: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] | None = None
    # Local plugin roots the CLI loads skills from. Skills reach the model this way rather than
    # through project setting sources, which would also pull this repository's CLAUDE.md/AGENTS.md
    # — coding-agent instructions — into a scientific session's context.
    plugins: tuple[dict[str, str], ...] = ()
    include_hook_events: bool = False
    # Host facts the profile prompt cannot know statically, appended to it verbatim so the
    # model knows what it can run without probing the filesystem for evidence.
    system_prompt_suffix: str = ""


class ClaudeAgentSDKBackend:
    runtime_name = "claude-agent-sdk"

    def __init__(
        self,
        *,
        sdk_module: Any | None = None,
        extensions: ClaudeRuntimeExtensions | None = None,
        observer: RuntimeObserver | None = None,
    ):
        self._sdk_module = sdk_module
        self.extensions = extensions or ClaudeRuntimeExtensions()
        self.observer = observer or NullRuntimeObserver()
        self._client: Any | None = None
        self._interrupt_requested = False
        # Last runtime session ID seen on the wire. A turn stopped before its ResultMessage
        # still has a resumable model conversation; keeping the ID is what preserves exact
        # resume instead of silently downgrading the next turn to a reconstructed one.
        self.last_runtime_session_id: str | None = None

    def _sdk(self) -> Any:
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            return importlib.import_module("claude_agent_sdk")
        except ImportError as exc:
            raise RuntimeExecutionError(
                "Claude runtime is not installed; install the project with .[runtime]"
            ) from exc

    @staticmethod
    def _sdk_stderr(line: str) -> None:
        if "connectors are disabled because" in line:
            return
        sys.stderr.write(line if line.endswith("\n") else line + "\n")

    def _options(self, request: RuntimeRequest, sdk: Any) -> Any:
        profile = request.profile
        extensions = self.extensions
        system_prompt = profile.read_system_prompt()
        if extensions.system_prompt_suffix.strip():
            system_prompt = f"{system_prompt}\n\n{extensions.system_prompt_suffix.strip()}"
        runtime_env = profile.runtime_environment()
        if request.context_window_tokens:
            # Claude Code's own autocompaction is the only guard between tool iterations inside
            # one SDK query. Trigger it conservatively because a local fallback tokenizer may
            # undercount the provider's actual Qwen prompt.
            runtime_env.setdefault(
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                str(int(request.context_window_tokens * AUTO_COMPACT_CONTEXT_FRACTION)),
            )
        values: dict[str, Any] = {
            "model": profile.model,
            "system_prompt": system_prompt,
            "max_turns": profile.max_turns,
            "max_budget_usd": profile.max_budget_usd,
            "cwd": request.cwd,
            "tools": list(extensions.tools),
            "allowed_tools": list(extensions.allowed_tools),
            "disallowed_tools": list(extensions.disallowed_tools),
            "mcp_servers": dict(extensions.mcp_servers),
            "strict_mcp_config": True,
            "permission_mode": "dontAsk",
            "setting_sources": [],
            "skills": (
                list(extensions.skills) if extensions.skills is not None else list(profile.skills)
            ),
            "hooks": dict(extensions.hooks) or None,
            "plugins": [dict(plugin) for plugin in extensions.plugins],
            "include_hook_events": extensions.include_hook_events,
            "env": runtime_env,
            "resume": request.resume_session_id,
            "fork_session": request.fork_session,
            "session_store": ScientificSessionTranscriptStore(request.scientific_session_dir),
            "session_store_flush": "batched",
            "stderr": self._sdk_stderr,
            # Scientific turns carry figure pixels back to the model. The SDK's default 1 MiB
            # stdout frame limit is a transport detail that a few normal-sized plots exceed, and
            # overflowing it kills the whole turn rather than one tool call. Size it above the
            # capability layer's own media budget so the enforced limit is the scientific one.
            "max_buffer_size": MAX_RUNTIME_MESSAGE_BYTES,
        }
        # Reasoning generation is profile-controlled; "native" injects nothing.
        values.update(profile.thinking.to_claude_options())
        return sdk.ClaudeAgentOptions(**values)

    @staticmethod
    def _assistant_blocks(message: Any, sdk: Any) -> list[RuntimeMessage]:
        output: list[RuntimeMessage] = []
        for block in message.content:
            if isinstance(block, sdk.TextBlock):
                visible, reasoning, textual_tools = split_model_text(block.text)
                output.extend(RuntimeMessage("thinking", item) for item in reasoning)
                if visible:
                    output.append(RuntimeMessage("text", visible))
                if textual_tools:
                    output.append(
                        RuntimeMessage(
                            "textual_tool_call",
                            {
                                "count": len(textual_tools),
                                "status": "not_executed",
                            },
                        )
                    )
            elif isinstance(block, sdk.ThinkingBlock):
                output.append(RuntimeMessage("thinking", block.thinking))
            elif isinstance(block, sdk.ToolUseBlock):
                output.append(
                    RuntimeMessage(
                        "tool_use",
                        {"id": block.id, "name": block.name, "input": block.input},
                    )
                )
            elif isinstance(block, sdk.ToolResultBlock):
                output.append(
                    RuntimeMessage(
                        "tool_result",
                        {
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        },
                    )
                )
        return output

    async def interrupt(self) -> bool:
        """Ask the running turn to stop cleanly. Returns whether a turn was in flight.

        This is the graceful stop: the CLI ends the turn itself, so the transcript is
        flushed and the runtime session stays resumable. Cancelling the execute() task is
        the forcible fallback when this does not land.
        """

        self._interrupt_requested = True
        client = self._client
        if client is None:
            return False
        with suppress(Exception):
            await client.interrupt()
        return True

    def _note_session_id(self, message: Any) -> None:
        session_id = getattr(message, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            data = getattr(message, "data", None)
            session_id = data.get("session_id") if isinstance(data, dict) else None
        if isinstance(session_id, str) and session_id:
            self.last_runtime_session_id = session_id

    @staticmethod
    def _is_context_error_result(response: RuntimeResponse) -> bool:
        if not response.is_error:
            return False
        text = f"{response.subtype}\n{response.final_text}".casefold().replace("_", " ")
        return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)

    def _interrupted_response(
        self, messages: list[RuntimeMessage], result: Any | None
    ) -> RuntimeResponse:
        text_blocks = [
            str(message.content)
            for message in messages
            if message.kind == "text" and str(message.content).strip()
        ]
        session_id = getattr(result, "session_id", None) or self.last_runtime_session_id or ""
        return RuntimeResponse(
            runtime_session_id=session_id,
            messages=tuple(messages),
            final_text=getattr(result, "result", None) or "\n".join(text_blocks),
            stop_reason=getattr(result, "stop_reason", None) or "interrupted",
            is_error=False,
            subtype="interrupted",
            usage=dict(getattr(result, "usage", None) or {}),
            model_usage=dict(getattr(result, "model_usage", None) or {}),
            total_cost_usd=getattr(result, "total_cost_usd", None),
            interrupted=True,
        )

    @staticmethod
    async def _preflight_context(client: Any, request: RuntimeRequest) -> None:
        """Refuse an exact query before the provider rejects its overfull transcript.

        Use the most conservative live boundary. The deployment may advertise a larger physical
        window than Claude Code's effective/autocompact ceiling, and crossing that lower ceiling
        is unsafe when the gateway's count-tokens endpoint falls back to an approximate local
        tokenizer. A failed usage probe is non-fatal because the service also recognizes an
        actual provider context error and can roll over safely.
        """

        if not request.resume_session_id or not hasattr(client, "get_context_usage"):
            return
        try:
            usage = await client.get_context_usage()
        except Exception:
            return
        if not isinstance(usage, dict):
            return
        total = usage.get("totalTokens")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return
        limit_candidates: list[tuple[int, str]] = []
        for value, source in (
            (request.context_window_tokens, request.context_limit_source or "model-discovery"),
            (usage.get("rawMaxTokens"), "claude-agent-sdk:rawMaxTokens"),
            (usage.get("maxTokens"), "claude-agent-sdk:maxTokens"),
            (usage.get("autoCompactThreshold"), "claude-agent-sdk:autoCompactThreshold"),
        ):
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limit_candidates.append((value, source))
        if not limit_candidates:
            return
        limit, source = min(limit_candidates, key=lambda item: item[0])
        output_reserve = request.max_output_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS
        safety_margin = max(MIN_CONTEXT_SAFETY_MARGIN_TOKENS, int(limit * 0.03))
        if total + output_reserve + safety_margin < limit:
            return
        raise ContextRolloverRequired(
            "the exact model conversation needs a context rollover before another turn "
            f"({total:,} used + {output_reserve:,} output reserve + "
            f"{safety_margin:,} safety margin >= {limit:,} token window)",
            total_tokens=total,
            context_window_tokens=limit,
            output_reserve_tokens=output_reserve,
            safety_margin_tokens=safety_margin,
            source=source,
        )

    @staticmethod
    async def _current_context_usage(
        client: Any,
        request: RuntimeRequest,
    ) -> dict[str, Any]:
        if not hasattr(client, "get_context_usage"):
            return {}
        try:
            usage = await client.get_context_usage()
        except Exception:
            return {}
        if not isinstance(usage, dict):
            return {}
        total = usage.get("totalTokens")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return {}
        sdk_raw_limit = usage.get("rawMaxTokens")
        effective_limit = usage.get("maxTokens")
        if (
            not isinstance(sdk_raw_limit, int)
            or isinstance(sdk_raw_limit, bool)
            or sdk_raw_limit < 1
        ):
            sdk_raw_limit = None
        # Live model/deployment discovery is authoritative. Claude SDK can report its own
        # autocompact ceiling (200K on the 262K Iris deployment) as rawMaxTokens, so using it as
        # the denominator makes a healthy context appear over 100%.
        context_limit = request.context_window_tokens or sdk_raw_limit
        if (
            not isinstance(effective_limit, int)
            or isinstance(effective_limit, bool)
            or effective_limit < 1
        ):
            effective_limit = None
        percentage = (100.0 * total / context_limit) if context_limit else None
        return {
            "total_tokens": total,
            "context_window_tokens": context_limit,
            "sdk_raw_context_window_tokens": sdk_raw_limit,
            "effective_limit_tokens": effective_limit,
            "percentage": percentage,
            "model": usage.get("model"),
            "auto_compact_enabled": usage.get("isAutoCompactEnabled"),
            "auto_compact_threshold_tokens": usage.get("autoCompactThreshold"),
            "source": "claude-agent-sdk:get_context_usage",
            "context_window_source": (
                request.context_limit_source
                if request.context_window_tokens
                else "claude-agent-sdk:rawMaxTokens"
            ),
        }

    async def execute(self, request: RuntimeRequest) -> RuntimeResponse:
        sdk = self._sdk()
        options = self._options(request, sdk)
        messages: list[RuntimeMessage] = []
        result: Any | None = None
        final_attempt_start = 0
        context_usage: dict[str, Any] = {}
        self._interrupt_requested = False
        self.last_runtime_session_id = request.resume_session_id
        self.observer.on_runtime_started(request)
        try:
            async with sdk.ClaudeSDKClient(options=options) as client:
                self._client = client
                await self._preflight_context(client, request)
                query_prompt = request.prompt
                for recovery_attempt in range(MAX_TEXTUAL_TOOL_CALL_RECOVERY_ATTEMPTS + 1):
                    attempt_start = len(messages)
                    attempt_result: Any | None = None
                    await client.query(query_prompt)
                    async for message in client.receive_response():
                        self._note_session_id(message)
                        if isinstance(message, sdk.AssistantMessage):
                            blocks = self._assistant_blocks(message, sdk)
                            messages.extend(blocks)
                            for block in blocks:
                                self.observer.on_message(block)
                        elif isinstance(message, sdk.ResultMessage):
                            attempt_result = message
                    if attempt_result is None:
                        if self._interrupt_requested:
                            break
                        raise RuntimeExecutionError(
                            "Claude Agent SDK ended without a ResultMessage"
                        )
                    result = attempt_result
                    raw_result = str(getattr(result, "result", None) or "")
                    _, _, result_tool_calls = split_model_text(raw_result)
                    emitted_tool_call = any(
                        item.kind == "textual_tool_call" for item in messages[attempt_start:]
                    )
                    if not emitted_tool_call and not result_tool_calls:
                        final_attempt_start = attempt_start
                        break
                    if bool(getattr(result, "is_error", False)):
                        final_attempt_start = attempt_start
                        break
                    if recovery_attempt >= MAX_TEXTUAL_TOOL_CALL_RECOVERY_ATTEMPTS:
                        raise RuntimeExecutionError(
                            "the model repeatedly emitted a textual tool call that was not "
                            "executed; refusing to report the incomplete turn as successful"
                        )
                    query_prompt = _TEXTUAL_TOOL_CALL_RECOVERY_PROMPT
                context_usage = await self._current_context_usage(client, request)
        except asyncio.CancelledError:
            self.observer.on_runtime_interrupted(forced=True)
            raise
        except RuntimeExecutionError:
            raise
        except Exception as exc:
            if self._interrupt_requested:
                # The runtime tore itself down while stopping. That is the interrupt landing,
                # not a scientific failure.
                self.observer.on_runtime_interrupted(forced=False)
                return self._interrupted_response(messages, result)
            self.observer.on_runtime_failed(str(exc))
            raise RuntimeExecutionError(f"Claude Agent SDK execution failed: {exc}") from exc
        finally:
            self._client = None

        if self._interrupt_requested:
            self.observer.on_runtime_interrupted(forced=False)
            return self._interrupted_response(messages, result)
        if result is None:
            raise RuntimeExecutionError("Claude Agent SDK ended without a ResultMessage")
        if not isinstance(result.session_id, str) or not result.session_id:
            raise RuntimeExecutionError("Claude Agent SDK returned no resumable session ID")
        text_blocks = [
            str(message.content)
            for message in messages[final_attempt_start:]
            if message.kind == "text" and str(message.content).strip()
        ]
        clean_result, _, result_tool_calls = split_model_text(str(result.result or ""))
        if result_tool_calls:
            raise RuntimeExecutionError(
                "the model ended with an unexecuted textual tool call"
            )
        final_text = clean_result or "\n".join(text_blocks)
        response = RuntimeResponse(
            runtime_session_id=result.session_id,
            messages=tuple(messages),
            final_text=final_text,
            stop_reason=result.stop_reason,
            is_error=bool(result.is_error),
            subtype=result.subtype,
            usage=dict(result.usage or {}),
            model_usage=dict(result.model_usage or {}),
            context_usage=context_usage,
            total_cost_usd=result.total_cost_usd,
        )
        # The service can recover a context rejection by rebuilding from durable state. Do not
        # print the provider's transient 400 as a final answer before that recovery decision.
        if not self._is_context_error_result(response):
            self.observer.on_runtime_finished(response)
        return response
