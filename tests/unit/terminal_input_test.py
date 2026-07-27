"""Display wrapping and row-scoped deletion for the interactive prompt."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.terminal.input import (
    _BOTTOM_TOOLBAR_STYLE,
    _bottom_toolbar,
    row_widths,
    visual_row_start,
    wrap_offsets,
)


def rows(text: str, first_width: int, rest_width: int | None = None) -> list[str]:
    """Return the text as the prompt would display it, one string per row."""

    rest = first_width if rest_width is None else rest_width
    starts = wrap_offsets(text, first_width, rest)
    bounds = list(starts) + [len(text)]
    return [text[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def test_short_line_is_a_single_row() -> None:
    assert rows("run quality control", 40) == ["run quality control"]


def test_words_are_never_split_across_rows() -> None:
    text = "please run quality control on the pancreas dataset and then cluster it"
    for row in rows(text, 38):
        assert len(row) <= 38
    assert rows(text, 38) == [
        "please run quality control on the ",
        "pancreas dataset and then cluster it",
    ]


def test_no_character_is_lost_at_a_break() -> None:
    text = "annotate the T cell compartment using curated markers and report confidence"
    for width in range(4, 40):
        assert "".join(rows(text, width)) == text


def test_a_word_longer_than_the_row_is_split() -> None:
    # Nothing else can be done with it, but only that word is broken.
    assert rows("see ENSMUSG00000026193ENSMUSG00000026194 now", 12) == [
        "see ",
        "ENSMUSG00000",
        "026193ENSMUS",
        "G00000026194",
        " now",
    ]


def test_a_space_stays_on_the_row_it_ends_while_it_fits() -> None:
    assert rows("alpha beta  gamma", 12) == ["alpha beta  ", "gamma"]
    # One column less and the second space no longer fits, so it opens the next row.
    assert rows("alpha beta  gamma", 11) == ["alpha beta ", " gamma"]


def test_first_row_is_shortened_by_the_prompt() -> None:
    # "> " leaves two columns less on the first row, so the break moves left.
    assert rows("cluster the pancreas cells", 18, 20) == ["cluster the ", "pancreas cells"]


def test_wrapping_terminates_on_pathological_widths() -> None:
    assert rows("abc", 0) == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("column", "expected"),
    [(0, 0), (5, 0), (33, 0), (34, 34), (50, 34), (69, 34)],
)
def test_visual_row_start_locates_the_row_holding_the_cursor(column: int, expected: int) -> None:
    text = "please run quality control on the pancreas dataset and then cluster it"
    assert visual_row_start(text, column, 38, 38) == expected


def test_cmd_backspace_would_delete_only_the_current_row() -> None:
    """The count Ctrl+U deletes is the distance back to the row start, not to the line start."""

    text = "please run quality control on the pancreas dataset and then cluster it"
    column = len(text)
    deleted = column - visual_row_start(text, column, 38, 38)
    assert text[:-deleted] == "please run quality control on the "
    assert deleted < column  # the rest of the message survives


def test_row_widths_charge_the_prompt_to_the_first_line_only() -> None:
    assert row_widths(80, 2, first_line=True) == (78, 80)
    assert row_widths(80, 2, first_line=False) == (80, 80)
    assert row_widths(1, 2, first_line=True) == (1, 1)


def test_prompt_toolkit_owns_the_right_aligned_context_toolbar(monkeypatch) -> None:
    import scagent_sdk.terminal.input as input_module

    app = SimpleNamespace(output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=80)))
    monkeypatch.setattr("prompt_toolkit.application.current.get_app", lambda: app)
    monkeypatch.setattr(
        input_module,
        "_BOTTOM_TOOLBAR_PROVIDER",
        lambda columns: " " * (columns - len("100K/262K")) + "100K/262K",
    )

    fragments = _bottom_toolbar()

    assert fragments[0][0] == "class:context-toolbar"
    assert len(fragments[0][1]) == 80
    assert fragments[0][1].endswith("100K/262K")


def test_context_toolbar_cancels_prompt_toolkits_full_width_reverse_style() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.styles import Style, merge_styles
    from prompt_toolkit.styles.defaults import default_ui_style

    combined = merge_styles(
        [default_ui_style(), Style.from_dict(_BOTTOM_TOOLBAR_STYLE)]
    )
    attrs = combined.get_attrs_for_style_str(
        "class:bottom-toolbar class:context-toolbar"
    )

    assert attrs.reverse is False
    assert attrs.bgcolor == ""


class FakeApp:
    """Enough of an `Application` for a key binding handler to run against."""

    def __init__(self, columns: int) -> None:
        self.output = SimpleNamespace(get_size=lambda: SimpleNamespace(columns=columns))
        self.clipboard = SimpleNamespace(set_text=lambda text: None)

    def invalidate(self) -> None:
        """`Binding.call` redraws after a handler returns."""


def press(keys: str, text: str, cursor: int | None = None, columns: int = 40) -> Any:
    """Send one key chord to the prompt's bindings and return the resulting buffer."""

    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.keys import Keys

    from scagent_sdk.terminal.input import key_bindings

    lookup = {"c-j": Keys.ControlJ, "c-u": Keys.ControlU, "enter": Keys.ControlM}
    bindings = key_bindings(lambda: 2)
    buffer = Buffer(multiline=True)
    buffer.text = text
    buffer.cursor_position = len(text) if cursor is None else cursor
    event = SimpleNamespace(current_buffer=buffer, app=FakeApp(columns))

    found = bindings.get_bindings_for_keys((lookup[keys],))
    assert found, f"no binding registered for {keys}"
    found[-1].call(event)
    return buffer


def test_ctrl_j_adds_a_newline_instead_of_submitting() -> None:
    assert press("c-j", "first line").text == "first line\n"


def test_escape_enter_adds_a_newline() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.keys import Keys

    from scagent_sdk.terminal.input import key_bindings

    bindings = key_bindings(lambda: 2)
    assert bindings.get_bindings_for_keys((Keys.Escape, Keys.ControlM))


def test_ctrl_u_deletes_the_display_row_not_the_message() -> None:
    text = "please run quality control on the pancreas dataset and then cluster it"
    # Width 40 less the two-column prompt breaks after "the ".
    assert press("c-u", text, columns=40).text == "please run quality control on the "


def test_ctrl_u_deletes_back_to_the_row_start_only() -> None:
    assert press("c-u", "abc", cursor=2, columns=40).text == "c"


def test_ctrl_u_at_the_start_of_a_line_joins_it_to_the_previous_one() -> None:
    # Nothing to delete on this row, so it steps back over the newline as prompt_toolkit does.
    assert press("c-u", "ab\ncd", cursor=3, columns=40).text == "abcd"


def test_ctrl_u_on_an_empty_prompt_does_nothing() -> None:
    assert press("c-u", "", columns=40).text == ""


def test_shift_enter_sequences_reach_the_newline_binding() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    from scagent_sdk.terminal.input import enable_modified_enter

    # prompt_toolkit ships these collapsed onto plain Enter, which would submit the message.
    enable_modified_enter()
    assert ANSI_SEQUENCES["\x1b[27;2;13~"] == (Keys.Escape, Keys.ControlM)
    assert ANSI_SEQUENCES["\x1b[27;5;13~"] == (Keys.Escape, Keys.ControlM)
