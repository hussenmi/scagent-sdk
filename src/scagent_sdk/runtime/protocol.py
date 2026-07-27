"""Provider-neutral runtime messages and execution protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from scagent_sdk.models.profile import ModelProfile


@dataclass(frozen=True)
class RuntimeMessage:
    kind: str
    content: Any

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "content": self.content}


@dataclass(frozen=True)
class RuntimeRequest:
    prompt: str
    profile: ModelProfile
    scientific_session_id: str
    scientific_session_dir: Path
    cwd: Path
    resume_session_id: str | None = None
    fork_session: bool = False
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    context_limit_source: str | None = None


@dataclass(frozen=True)
class RuntimeResponse:
    runtime_session_id: str
    messages: tuple[RuntimeMessage, ...]
    final_text: str
    stop_reason: str | None
    is_error: bool
    subtype: str
    usage: dict[str, Any] = field(default_factory=dict)
    model_usage: dict[str, Any] = field(default_factory=dict)
    context_usage: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: float | None = None
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_session_id": self.runtime_session_id,
            "messages": [message.to_dict() for message in self.messages],
            "final_text": self.final_text,
            "stop_reason": self.stop_reason,
            "is_error": self.is_error,
            "subtype": self.subtype,
            "usage": self.usage,
            "model_usage": self.model_usage,
            "context_usage": self.context_usage,
            "total_cost_usd": self.total_cost_usd,
            "interrupted": self.interrupted,
        }


class RuntimeBackend(Protocol):
    runtime_name: str

    async def execute(self, request: RuntimeRequest) -> RuntimeResponse: ...


class InterruptibleBackend(Protocol):
    """A backend that can stop an in-flight turn without losing its runtime session.

    Optional: :class:`AgentRuntimeService` and the terminal probe for these members and fall
    back to plain cancellation for backends (and test doubles) that do not implement them.
    """

    last_runtime_session_id: str | None

    async def interrupt(self) -> bool: ...
