from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from scagent_sdk.errors import ScagentSDKError
from scagent_sdk.runtime.resume import ResumeMode, ResumePlan, ResumePreference
from scagent_sdk.terminal.resume import choose_resume_preference


def _plan(mode: ResumeMode) -> ResumePlan:
    return ResumePlan(
        mode=mode,
        scientific_session_id="science",
        runtime_session_id="sdk-old" if mode is ResumeMode.EXACT else None,
        reason="test",
        context="checkpoint",
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", ResumePreference.AUTO),
        ("2", ResumePreference.EXACT),
        ("3", ResumePreference.RECONSTRUCTED),
    ],
)
def test_resume_menu_maps_user_choice(monkeypatch, answer: str, expected: ResumePreference) -> None:
    monkeypatch.setattr(
        "scagent_sdk.terminal.resume.Prompt.ask",
        lambda *args, **kwargs: answer,
    )
    console = Console(file=StringIO(), force_terminal=False)

    assert choose_resume_preference(console, _plan(ResumeMode.EXACT)) is expected


def test_resume_menu_does_not_offer_exact_when_runtime_is_incompatible(monkeypatch) -> None:
    captured = {}

    def ask(*args, **kwargs):
        captured["choices"] = kwargs["choices"]
        return "3"

    monkeypatch.setattr("scagent_sdk.terminal.resume.Prompt.ask", ask)
    console = Console(file=StringIO(), force_terminal=False)

    selected = choose_resume_preference(console, _plan(ResumeMode.RECONSTRUCTED))

    assert selected is ResumePreference.RECONSTRUCTED
    assert "2" not in captured["choices"]


def test_resume_menu_can_cancel(monkeypatch) -> None:
    monkeypatch.setattr(
        "scagent_sdk.terminal.resume.Prompt.ask",
        lambda *args, **kwargs: "4",
    )
    console = Console(file=StringIO(), force_terminal=False)

    with pytest.raises(ScagentSDKError, match="cancelled"):
        choose_resume_preference(console, _plan(ResumeMode.EXACT))
