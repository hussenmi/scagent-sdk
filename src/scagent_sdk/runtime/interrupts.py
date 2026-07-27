"""Stop the work of one turn without ending the scientific session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TurnInterrupter:
    """Ask everything running in a turn to stop cleanly.

    A turn holds two independent kinds of in-flight work: the model runtime's own loop, and
    deterministic capability workers executing in scientific environments. Stopping only the
    first would leave a GPU job running with nothing to report to; stopping only the second
    would leave the model waiting on a tool that will never answer. Both are addressed here so
    a single keypress means one thing: stop this turn, keep the session.
    """

    backend: Any
    broker: Any | None = None

    async def request(self) -> tuple[str, ...]:
        """Signal in-flight work. Returns human-readable descriptions of what was stopped."""

        stopped: list[str] = []
        if self.broker is not None:
            cancelled = self.broker.cancel_all()
            if cancelled:
                count = len(cancelled)
                noun = "capability" if count == 1 else "capabilities"
                stopped.append(f"{count} running {noun}")
        interrupt = getattr(self.backend, "interrupt", None)
        if interrupt is not None and await interrupt():
            stopped.append("model runtime")
        return tuple(stopped)
