from __future__ import annotations

import asyncio
import signal
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from scagent_sdk.errors import RuntimeExecutionError
from scagent_sdk.runtime.interrupts import TurnInterrupter
from scagent_sdk.runtime.protocol import RuntimeMessage, RuntimeResponse
from scagent_sdk.session import AnalysisSession
from scagent_sdk.terminal.app import RichInteractiveAgent
from scagent_sdk.terminal.interrupts import (
    EscInterruptListener,
    TurnInterruptController,
    keyboard_interrupts,
)


class FakeBroker:
    def __init__(self, running: tuple[str, ...] = ()):
        self.running = list(running)
        self.cancelled: list[str] = []

    def cancel_all(self) -> tuple[str, ...]:
        self.cancelled.extend(self.running)
        stopped, self.running = tuple(self.running), []
        return stopped


class FakeBackend:
    runtime_name = "claude-agent-sdk"

    def __init__(self, *, in_flight: bool = True):
        self.in_flight = in_flight
        self.interrupts = 0

    async def interrupt(self) -> bool:
        self.interrupts += 1
        return self.in_flight


def test_interrupter_stops_compute_and_the_model_runtime_together() -> None:
    backend = FakeBackend()
    broker = FakeBroker(("exec-1", "exec-2"))

    stopped = asyncio.run(TurnInterrupter(backend, broker=broker).request())

    assert broker.cancelled == ["exec-1", "exec-2"]
    assert backend.interrupts == 1
    assert stopped == ("2 running capabilities", "model runtime")


def test_interrupter_tolerates_a_backend_that_cannot_be_interrupted() -> None:
    class PlainBackend:
        runtime_name = "other"

    assert asyncio.run(TurnInterrupter(PlainBackend()).request()) == ()


def test_first_request_stops_cleanly_and_second_cancels_the_turn() -> None:
    notices: list[str] = []

    async def scenario() -> tuple[bool, int]:
        turn = asyncio.ensure_future(asyncio.sleep(30))
        backend = FakeBackend()
        controller = TurnInterruptController(
            turn,
            interrupter=TurnInterrupter(backend),
            notify=notices.append,
            grace_seconds=30.0,
        )
        controller.request()
        await asyncio.sleep(0)  # let the clean stop run
        await asyncio.sleep(0)
        first_still_running = not turn.done()
        controller.request()
        with pytest.raises(asyncio.CancelledError):
            await turn
        controller.close()
        return first_still_running, backend.interrupts

    still_running, interrupts = asyncio.run(scenario())

    assert still_running, "a clean stop must not cancel the turn outright"
    assert interrupts == 1
    assert "press Esc or Ctrl+C again" in notices[0]
    assert notices[-1] == "Stopping immediately."


def test_clean_stop_escalates_when_the_grace_period_expires() -> None:
    async def scenario() -> bool:
        turn = asyncio.ensure_future(asyncio.sleep(30))
        controller = TurnInterruptController(turn, notify=lambda _m: None, grace_seconds=0.01)
        controller.request()
        with pytest.raises(asyncio.CancelledError):
            await turn
        controller.close()
        return controller.forced

    assert asyncio.run(scenario()) is True


def test_sigint_is_routed_to_the_turn_and_the_default_handler_is_restored() -> None:
    requests: list[int] = []

    async def scenario() -> None:
        with keyboard_interrupts(lambda: requests.append(1)):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert requests == [1], "SIGINT during a turn must not raise KeyboardInterrupt"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_esc_listener_is_inert_without_an_interactive_terminal() -> None:
    fired: list[int] = []

    with EscInterruptListener(lambda: fired.append(1)) as listener:
        assert listener._thread is None

    assert fired == []


def _agent(tmp_path: Path, console: Console, service: object) -> RichInteractiveAgent:
    return RichInteractiveAgent(
        service=service,  # type: ignore[arg-type]
        session=AnalysisSession.create(tmp_path, title="Interrupt"),
        profile=None,  # type: ignore[arg-type]
        cwd=tmp_path,
        console=console,
    )


def test_ctrl_c_stops_a_hanging_turn_and_returns_to_the_prompt(tmp_path: Path) -> None:
    """Ctrl+C during a turn that never ends must stop the turn, not the REPL."""

    class HangingService:
        stopped = False

        async def run_turn(self, *_args, **_kwargs) -> RuntimeResponse:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                type(self).stopped = True
                raise
            raise AssertionError("the turn should have been stopped")

    output = StringIO()
    agent = _agent(tmp_path, Console(file=output, force_terminal=False, width=90), HangingService())

    async def scenario() -> None:
        turn = asyncio.ensure_future(agent._turn("analyze this"))
        await asyncio.sleep(0.05)
        signal.raise_signal(signal.SIGINT)  # ask the turn to stop cleanly
        await asyncio.sleep(0.05)
        signal.raise_signal(signal.SIGINT)  # nothing to stop cleanly here: force it
        await asyncio.wait_for(turn, timeout=5)

    asyncio.run(scenario())

    text = output.getvalue()
    assert HangingService.stopped, "the turn task must actually be cancelled"
    assert "Turn stopped" in text
    assert "preserved" in text
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_turn_failure_still_reports_a_resumable_session(tmp_path: Path) -> None:
    class FailingService:
        async def run_turn(self, *_args, **_kwargs) -> RuntimeResponse:
            raise RuntimeExecutionError("gateway down")

    output = StringIO()
    agent = _agent(tmp_path, Console(file=output, force_terminal=False, width=90), FailingService())

    asyncio.run(agent._turn("analyze this"))

    assert "Turn failed" in output.getvalue()


def test_interrupted_response_is_reported_as_a_stop_not_a_failure(tmp_path: Path) -> None:
    class InterruptedService:
        async def run_turn(self, *_args, **_kwargs) -> RuntimeResponse:
            return RuntimeResponse(
                runtime_session_id="sdk-live",
                messages=(RuntimeMessage("text", "partial"),),
                final_text="partial",
                stop_reason="interrupted",
                is_error=False,
                subtype="interrupted",
                interrupted=True,
            )

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=90)
    agent = _agent(tmp_path, console, InterruptedService())

    asyncio.run(agent._turn("analyze this"))

    text = output.getvalue()
    assert "Turn stopped" in text
    assert "failed" not in text


def test_prompt_level_ctrl_c_needs_confirmation_but_ctrl_d_exits(tmp_path: Path) -> None:
    output = StringIO()
    agent = _agent(tmp_path, Console(file=output, force_terminal=False, width=90), object())
    answers: list[object] = [KeyboardInterrupt(), "  ", KeyboardInterrupt(), KeyboardInterrupt()]

    async def fake_input(_prompt: str, **_kwargs) -> str:
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    import scagent_sdk.terminal.app as app_module

    original = app_module.read_user_input
    app_module.read_user_input = fake_input  # type: ignore[assignment]
    try:
        assert asyncio.run(agent.run()) == 0
    finally:
        app_module.read_user_input = original  # type: ignore[assignment]

    text = output.getvalue()
    # First press warns; a real prompt in between disarms it; the next pair exits.
    assert text.count("Press Ctrl+C again to exit") == 2
    assert "Session ended" in text
