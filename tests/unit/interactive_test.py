from __future__ import annotations

import asyncio
from pathlib import Path

from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.interactive import InteractiveAgent
from scagent_sdk.runtime.protocol import RuntimeResponse
from scagent_sdk.session import AnalysisSession


class FakeService:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_turn(self, _session, *, user_prompt, profile, cwd):
        self.prompts.append(user_prompt)
        return RuntimeResponse(
            runtime_session_id="sdk-session",
            messages=(),
            final_text=f"answer:{user_prompt}",
            stop_reason="end_turn",
            is_error=False,
            subtype="success",
        )


def test_interactive_agent_handles_local_commands_and_multiple_turns(tmp_path: Path) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("system", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    session = AnalysisSession.create(tmp_path / "sessions", title="interactive")
    entries = iter(["/help", "first", "/state", "second", "/exit"])
    output: list[str] = []

    async def readline(_prompt: str) -> str:
        return next(entries)

    service = FakeService()
    agent = InteractiveAgent(
        service=service,  # type: ignore[arg-type]
        session=session,
        profile=profile,
        cwd=tmp_path,
        readline=readline,
        write=output.append,
    )

    assert asyncio.run(agent.run()) == 0
    assert service.prompts == ["first", "second"]
    assert "answer:first" in output
    assert "answer:second" in output
    assert any('"session_id"' in item for item in output)
