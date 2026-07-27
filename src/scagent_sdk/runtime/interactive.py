"""Interactive, resumable shell over the durable runtime service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.interrupts import TurnInterrupter
from scagent_sdk.runtime.protocol import RuntimeResponse
from scagent_sdk.runtime.service import AgentRuntimeService
from scagent_sdk.session import AnalysisSession
from scagent_sdk.terminal.interrupts import TurnInterruptController, keyboard_interrupts

ReadLine = Callable[[str], Awaitable[str]]
WriteLine = Callable[[str], None]


async def terminal_readline(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


@dataclass
class InteractiveAgent:
    service: AgentRuntimeService
    session: AnalysisSession
    profile: ModelProfile
    cwd: Path
    readline: ReadLine = terminal_readline
    write: WriteLine = print
    interrupter: TurnInterrupter | None = None

    def _show_state(self) -> None:
        self.write(json.dumps(self.session.summary(), indent=2, sort_keys=True))

    def _show_response(self, response: RuntimeResponse) -> None:
        self.write(response.final_text.strip() or "[model returned no text]")

    async def run(self) -> int:
        self.write(f"Scientific session: {self.session.session_id}\nCommands: /state, /help, /exit")
        while True:
            try:
                user_prompt = (await self.readline("you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                self.write("")
                return 0
            if not user_prompt:
                continue
            if user_prompt in {"/exit", "/quit"}:
                return 0
            if user_prompt == "/state":
                self._show_state()
                continue
            if user_prompt == "/help":
                self.write(
                    "/state shows durable state; /exit ends this terminal session. "
                    "Esc or Ctrl+C stops a running turn without ending the session."
                )
                continue
            response = await self._turn(user_prompt)
            if response is not None:
                self._show_response(response)

    async def _turn(self, user_prompt: str) -> RuntimeResponse | None:
        """Run one turn that a keypress can stop, leaving the session intact."""

        task = asyncio.get_running_loop().create_task(
            self.service.run_turn(
                self.session,
                user_prompt=user_prompt,
                profile=self.profile,
                cwd=self.cwd,
            )
        )
        backend = getattr(self.service, "backend", None)
        interrupter = self.interrupter or (
            TurnInterrupter(backend) if backend is not None else None
        )
        controller = TurnInterruptController(task, interrupter=interrupter, notify=self.write)
        try:
            with keyboard_interrupts(controller.request):
                return await task
        except asyncio.CancelledError:
            if not controller.stopped:
                raise
        except KeyboardInterrupt:
            task.cancel()
        finally:
            controller.close()
        self.write("Turn stopped; the scientific session is preserved.")
        return None
