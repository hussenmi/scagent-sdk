from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from rich.console import Console

from scagent_sdk.models.limits import ModelLimits
from scagent_sdk.runtime.protocol import RuntimeResponse
from scagent_sdk.session import AnalysisSession
from scagent_sdk.terminal.app import RichInteractiveAgent


def _agent(tmp_path: Path, console: Console) -> RichInteractiveAgent:
    session = AnalysisSession.create(tmp_path, title="Header test")
    return RichInteractiveAgent(
        service=None,  # type: ignore[arg-type]
        session=session,
        profile=None,  # type: ignore[arg-type]
        cwd=tmp_path,
        console=console,
    )


def test_turn_header_shows_output_folder_only_on_first_turn(tmp_path: Path) -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    agent = _agent(tmp_path, console)

    agent._render_turn_header()
    agent._render_turn_header()

    text = output.getvalue()
    folder = agent.session.store.session_dir.name
    assert folder.startswith("run_")
    # Output folder is named exactly once, on the first turn.
    assert text.count("📁 Output:") == 1
    assert folder in text
    # A separating rule is drawn on every turn.
    assert text.count("─") >= 2


def test_completed_turn_updates_the_persistent_context_bar(tmp_path: Path) -> None:
    usage = {"total_tokens": 24_000, "context_window_tokens": 262_144}

    class Service:
        async def run_turn(self, *_args, **_kwargs) -> RuntimeResponse:
            return RuntimeResponse(
                runtime_session_id="sdk-new",
                messages=(),
                final_text="done",
                stop_reason="end_turn",
                is_error=False,
                subtype="success",
                context_usage=usage,
            )

    console = Console(file=StringIO(), force_terminal=False, width=80)
    agent = _agent(tmp_path, console)
    agent.service = Service()  # type: ignore[assignment]

    asyncio.run(agent._turn("continue"))

    assert agent._last_context_usage == usage
    assert agent._context_toolbar(80).endswith(" 24K/262K ")


def test_resumed_session_restores_and_corrects_the_last_context_bar(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path, title="Resume context")
    session.store.record(
        "runtime.turn_completed",
        payload={
            "turn_id": "turn-1",
            "response": {
                "context_usage": {
                    "total_tokens": 100_000,
                    "context_window_tokens": 200_000,
                }
            },
        },
    )
    console = Console(file=StringIO(), force_terminal=False, width=80)
    agent = RichInteractiveAgent(
        service=None,  # type: ignore[arg-type]
        session=session,
        profile=None,  # type: ignore[arg-type]
        cwd=tmp_path,
        console=console,
        model_limits=ModelLimits(
            context_window_tokens=262_144,
            max_output_tokens=None,
            source="upstream:models",
        ),
    )

    assert agent._last_context_usage["context_window_tokens"] == 262_144
    assert agent._last_context_usage["sdk_raw_context_window_tokens"] == 200_000
    assert agent._context_toolbar(80).endswith(" 100K/262K ")
