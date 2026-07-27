"""Multiline terminal input with a dependency-light fallback.

The prompt holds one editable buffer, not one buffer per screen row: what looks like several
lines at the right edge of the terminal is a single logical line soft-wrapped for display.
prompt_toolkit wraps at whichever character overflows the width, which cuts words in half, and
it maps Ctrl+U — what terminals send for Cmd+Backspace — to a kill that removes the whole
logical line, meaning the entire message.

Both behaviors are wrong for a chat-style prompt, so this module owns wrapping instead:
:func:`wrap_offsets` places the breaks on word boundaries, the buffer control renders those rows
directly with prompt_toolkit's own character wrapping turned off, and Ctrl+U deletes back only to
the start of the display row the cursor is on. The wrapping stays a display concern — the buffer
still contains one unbroken line, and the submitted text never gains a newline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: Prefix drawn in front of every display row after the first one.
_CONTINUATION = ""

_PROMPT_SESSION: Any | None = None
_BOTTOM_TOOLBAR_PROVIDER: Callable[[int], str] | None = None
_BOTTOM_TOOLBAR_STYLE = {
    # prompt_toolkit's default bottom-toolbar style is `reverse`, which paints every alignment
    # space as a solid gray band. Explicitly cancel it so only the right-aligned glyphs are visible.
    "bottom-toolbar": "noreverse fg:#888888",
    "context-toolbar": "noreverse fg:#888888",
}


def wrap_offsets(text: str, first_width: int, rest_width: int) -> tuple[int, ...]:
    """Return the offset at which each display row of one logical line starts.

    Rows break after a space so a word is never split across two rows. A single word longer than
    a row is still split, because it cannot be placed any other way. Every character keeps a
    place in exactly one row — nothing is dropped at a break — so each offset in the text maps to
    a reachable cursor position.
    """

    widths = (max(1, first_width), max(1, rest_width))
    starts = [0]
    position = 0
    row = 0
    while True:
        width = widths[0] if row == 0 else widths[1]
        if len(text) - position <= width:
            return tuple(starts)
        limit = position + width
        space = text.rfind(" ", position, limit)
        # Breaking after the last space keeps the word whole and leaves that space on the row it
        # ends; with no space to break on, the word is longer than the row and has to be split.
        position = space + 1 if space != -1 else limit
        starts.append(position)
        row += 1


def row_widths(total_width: int, prompt_width: int, *, first_line: bool) -> tuple[int, int]:
    """Return the text width of a logical line's first display row and of its later rows.

    Only the first row of the first logical line carries the prompt; every other row carries the
    continuation prefix, so it has that much less room for text.
    """

    prefix = prompt_width if first_line else len(_CONTINUATION)
    return max(1, total_width - prefix), max(1, total_width - len(_CONTINUATION))


def visual_row_start(text: str, column: int, first_width: int, rest_width: int) -> int:
    """Return the offset where the display row holding ``column`` begins."""

    start = 0
    for offset in wrap_offsets(text, first_width, rest_width):
        if offset > column:
            break
        start = offset
    return start


def _slice_fragments(fragments: Any, start: int, end: int) -> Any:
    """Return the ``(style, text)`` fragments covering ``text[start:end]``."""

    sliced = []
    position = 0
    for fragment in fragments:
        style, text = fragment[0], fragment[1]
        following = position + len(text)
        if following > start and position < end:
            piece = text[max(0, start - position) : end - position]
            sliced.append((style, piece, *fragment[2:]))
        position = following
        if position >= end:
            break
    return sliced


def _wrapped_content(content: Any, width: int, prompt_width: int) -> Any:
    """Rebuild ``content`` so each of its lines is one word-wrapped display row."""

    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.controls import UIContent

    rows: list[tuple[int, int, int]] = []  # (logical line, start offset, end offset)
    rows_by_line: list[list[int]] = []
    fragments_by_line: list[Any] = []

    for lineno in range(content.line_count):
        fragments = content.get_line(lineno)
        fragments_by_line.append(fragments)
        text = "".join(fragment[1] for fragment in fragments)
        # `BufferControl` appends one space to every line as the cursor position available after
        # the last character. It is not part of the text and must not influence the breaks.
        body = text[:-1] if text.endswith(" ") else text

        first, rest = row_widths(width, prompt_width, first_line=lineno == 0)
        starts = wrap_offsets(body, first, rest)
        spans = [
            (start, starts[index + 1] if index + 1 < len(starts) else len(body))
            for index, start in enumerate(starts)
        ]
        # A full last row leaves the trailing cursor position nowhere to sit, so give it a row.
        last_start, last_end = spans[-1]
        if last_end - last_start >= (first if len(spans) == 1 else rest):
            spans.append((len(body), len(body)))
        spans[-1] = (spans[-1][0], len(text))

        rows_by_line.append([len(rows) + offset for offset in range(len(spans))])
        rows.extend((lineno, start, end) for start, end in spans)

    def translate(point: Any) -> Any:
        if point is None or not 0 <= point.y < len(rows_by_line):
            return point
        chosen = rows_by_line[point.y][0]
        for index in rows_by_line[point.y]:
            if rows[index][1] <= point.x:
                chosen = index
        return Point(x=point.x - rows[chosen][1], y=chosen)

    def get_line(index: int) -> Any:
        lineno, start, end = rows[index]
        return _slice_fragments(fragments_by_line[lineno], start, end)

    return UIContent(
        get_line=get_line,
        line_count=len(rows),
        cursor_position=translate(content.cursor_position),
        menu_position=translate(content.menu_position),
        show_cursor=content.show_cursor,
    )


def _prompt_width(session: Any) -> int:
    from prompt_toolkit.formatted_text import fragment_list_width, to_formatted_text

    return int(fragment_list_width(to_formatted_text(session.message)))


def _bottom_toolbar() -> Any:
    """Render the active toolbar inside prompt_toolkit so its redraw cannot erase it."""

    provider = _BOTTOM_TOOLBAR_PROVIDER
    if provider is None:
        return []
    try:
        from prompt_toolkit.application.current import get_app

        text = provider(get_app().output.get_size().columns)
    except (AttributeError, ImportError, RuntimeError):
        return []
    return [("class:context-toolbar", text)] if text else []


def _install_word_wrap(session: Any) -> bool:
    """Render the input word-wrapped. Return whether the session was rewired."""

    from prompt_toolkit.layout.controls import BufferControl

    controls: list[Any] = [
        control
        for control in session.layout.find_all_controls()
        if isinstance(control, BufferControl) and control.buffer is session.default_buffer
    ]
    if not controls:
        return False
    control = controls[0]
    render = control.create_content

    def create_content(width: int, height: Any, preview_search: bool = False) -> Any:
        return _wrapped_content(
            render(width, height, preview_search), width, _prompt_width(session)
        )

    control.create_content = create_content
    session.wrap_lines = False
    return True


def enable_modified_enter() -> None:
    """Make a disambiguated Shift/Ctrl+Enter insert a newline instead of submitting.

    prompt_toolkit knows the xterm "modified other keys" forms of these chords but deliberately
    collapses them onto plain Enter, so a terminal that does send them submits the message — the
    opposite of what the user pressed them for. Mapping them to Escape+Enter routes them to the
    newline binding. Most terminals send nothing distinct for Shift+Enter and are unaffected;
    those users need a terminal-side mapping to Escape+Enter, or Ctrl+J.
    """

    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    for sequence in ("\x1b[27;2;13~", "\x1b[27;5;13~", "\x1b[27;6;13~"):
        # Replacing values of existing keys only — the parser's prefix cache stays valid.
        ANSI_SEQUENCES[sequence] = (Keys.Escape, Keys.ControlM)


def key_bindings(prompt_width: Callable[[], int]) -> Any:
    """Build the prompt's key bindings, given a way to measure the current prompt."""

    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    # Esc+Enter is the sequence terminals can always produce; Ctrl+J is a single chord that
    # arrives as a distinct byte everywhere, and is what several terminals send for Ctrl+Enter.
    @bindings.add("escape", "enter")
    @bindings.add("c-j")
    def newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-c")
    def interrupt(event: Any) -> None:
        if event.current_buffer.text:
            event.current_buffer.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt)

    @bindings.add("c-u")
    def kill_display_row(event: Any) -> None:
        """Delete back to the start of the display row, not of the whole message.

        Terminals send Ctrl+U for Cmd+Backspace, and the row the user sees is the unit they mean.
        The removed text goes to the clipboard, so Ctrl+Y still brings it back.
        """

        buffer = event.current_buffer
        document = buffer.document
        first, rest = row_widths(
            event.app.output.get_size().columns,
            prompt_width(),
            first_line=document.cursor_position_row == 0,
        )
        column = document.cursor_position_col
        count = column - visual_row_start(document.current_line, column, first, rest)
        if count <= 0:
            # Already at the start of a row; match prompt_toolkit and step back one character.
            if document.cursor_position > 0:
                buffer.delete_before_cursor(count=1)
            return
        event.app.clipboard.set_text(buffer.delete_before_cursor(count=count))

    return bindings


def _session() -> Any | None:
    global _PROMPT_SESSION
    if _PROMPT_SESSION is not None:
        return _PROMPT_SESSION
    session: Any = None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.styles import Style

        enable_modified_enter()
        bindings = key_bindings(lambda: _prompt_width(session))
    except ImportError:
        return None

    session = PromptSession(
        multiline=True,
        key_bindings=bindings,
        prompt_continuation=_CONTINUATION,
        bottom_toolbar=_bottom_toolbar,
        style=Style.from_dict(_BOTTOM_TOOLBAR_STYLE),
    )
    # Word wrapping reaches into how prompt_toolkit builds its content. If a future version stops
    # fitting, the prompt keeps working with the library's own character wrapping rather than
    # failing on every keystroke.
    try:
        _install_word_wrap(session)
    except (AttributeError, ImportError, TypeError):
        session.wrap_lines = True
    _PROMPT_SESSION = session
    return _PROMPT_SESSION


async def read_user_input(
    prompt: str = "> ",
    *,
    bottom_toolbar: Callable[[int], str] | None = None,
) -> str:
    import asyncio

    global _BOTTOM_TOOLBAR_PROVIDER
    _BOTTOM_TOOLBAR_PROVIDER = bottom_toolbar
    session = _session()
    try:
        if session is None:
            return await asyncio.to_thread(input, prompt)
        return await asyncio.to_thread(session.prompt, prompt)
    finally:
        _BOTTOM_TOOLBAR_PROVIDER = None
