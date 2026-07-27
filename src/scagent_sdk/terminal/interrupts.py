"""Keyboard interrupts that stop a running turn instead of killing the session."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, Literal

DISABLE_ESC_ENV = "SCAGENT_SDK_NO_ESC_INTERRUPT"
_ESC = 0x1B
_TRUE = {"1", "true", "yes", "on"}


class EscInterruptListener:
    """Watch stdin for a bare Esc while a turn is running.

    Nothing else reads stdin during a turn, so the terminal is put in cbreak mode and a daemon
    thread waits for Esc. cbreak leaves ISIG enabled, so Ctrl+C keeps producing SIGINT as usual.
    Escape *sequences* (arrow and function keys send Esc '[' / Esc 'O' …) arrive as a burst and
    are drained and ignored; only a standalone Esc counts.

    A no-op without an interactive TTY or termios (headless runs, tests, Windows), and when
    ``SCAGENT_SDK_NO_ESC_INTERRUPT`` is set.
    """

    def __init__(self, on_interrupt: Callable[[], None], *, enabled: bool = True):
        self._on_interrupt = on_interrupt
        self._enabled = enabled
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fd: int | None = None
        self._original_attributes: Any | None = None

    def _supported(self) -> bool:
        if not self._enabled or os.environ.get(DISABLE_ESC_ENV, "").strip().lower() in _TRUE:
            return False
        try:
            return bool(sys.stdin) and sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def __enter__(self) -> EscInterruptListener:
        if not self._supported():
            return self
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._original_attributes = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except (ImportError, OSError, ValueError):
            self._fd = None
            self._original_attributes = None
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="scagent-esc", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> Literal[False]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._fd is not None and self._original_attributes is not None:
            with suppress(ImportError, OSError, ValueError):
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original_attributes)
        self._fd = None
        self._original_attributes = None
        return False  # never suppress an exception raised inside the turn

    def _watch(self) -> None:
        import select

        fd = self._fd
        assert fd is not None
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(fd, 1)
            except (OSError, ValueError):
                return
            if not data or data[0] != _ESC:
                continue
            try:
                more, _, _ = select.select([fd], [], [], 0.05)
                if more:
                    os.read(fd, 32)  # an escape sequence, not a bare Esc: drain and ignore
                    continue
            except (OSError, ValueError):
                return
            self._stop.set()
            self._on_interrupt()
            return


@contextmanager
def keyboard_interrupts(on_interrupt: Callable[[], None]) -> Iterator[None]:
    """Route Esc and Ctrl+C to ``on_interrupt`` for the duration of one turn.

    SIGINT is handled through the event loop rather than the default handler: a raw
    ``KeyboardInterrupt`` unwinds out of ``asyncio.run`` and takes the whole REPL with it,
    which is exactly the crash this replaces. On exit the loop handler is removed, restoring
    Python's default SIGINT behavior for the prompt.
    """

    loop = asyncio.get_running_loop()
    signal_installed = False
    with suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGINT, on_interrupt)
        signal_installed = True
    def from_listener_thread() -> None:
        loop.call_soon_threadsafe(on_interrupt)

    listener = EscInterruptListener(from_listener_thread)
    try:
        with listener:
            yield
    finally:
        if signal_installed:
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signal.SIGINT)


class TurnInterruptController:
    """Two-stage stop for one running turn.

    The first press asks the runtime and any running capability to stop cleanly, which keeps
    the model conversation resumable and lets the transcript flush. If that has not landed
    within the grace period — or the user presses again — the turn task is cancelled outright.
    Either way the REPL survives and the durable session keeps everything already committed.
    """

    def __init__(
        self,
        task: asyncio.Task[Any],
        *,
        interrupter: Any | None = None,
        notify: Callable[[str], None] = lambda _message: None,
        grace_seconds: float = 20.0,
    ):
        self.task = task
        self.interrupter = interrupter
        self.notify = notify
        self.grace_seconds = grace_seconds
        self.requests = 0
        self.forced = False
        self._pending: asyncio.Task[None] | None = None

    @property
    def stopped(self) -> bool:
        """Whether this controller is the reason the turn is ending."""

        return self.requests > 0

    def request(self) -> None:
        """Handle one Esc or Ctrl+C. Must run on the event loop thread."""

        self.requests += 1
        if self.task.done():
            return
        if self.requests == 1:
            self.notify("Stopping this turn — press Esc or Ctrl+C again to stop immediately.")
            self._pending = asyncio.get_running_loop().create_task(self._stop_cleanly())
        else:
            self._force("Stopping immediately.")

    async def _stop_cleanly(self) -> None:
        stopped = ()
        if self.interrupter is not None:
            with suppress(Exception):
                stopped = await self.interrupter.request()
        if stopped:
            self.notify("Stopped " + ", ".join(stopped) + ".")
        await asyncio.sleep(self.grace_seconds)
        if not self.task.done():
            self._force("Still running after the grace period — stopping immediately.")

    def _force(self, message: str) -> None:
        if self.forced or self.task.done():
            return
        self.forced = True
        self.notify(message)
        self.task.cancel()

    def close(self) -> None:
        """Drop the pending clean-stop timer once the turn is over."""

        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None
