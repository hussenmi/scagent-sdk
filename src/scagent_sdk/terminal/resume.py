"""Interactive resume policy selection for an existing scientific session."""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt

from scagent_sdk.errors import ScagentSDKError
from scagent_sdk.runtime.resume import ResumeMode, ResumePlan, ResumePreference


def choose_resume_preference(console: Console, plan: ResumePlan) -> ResumePreference:
    exact_available = plan.mode is ResumeMode.EXACT
    console.print("\n[bold]How should this scientific session resume?[/bold]")
    console.print(
        "  [cyan]1[/cyan]  Automatic [dim](recommended) — use exact history when it fits; "
        "roll over to durable scientific state when needed[/dim]"
    )
    if exact_available:
        console.print(
            "  [cyan]2[/cyan]  Exact — require the recorded model conversation; "
            "never fall back silently"
        )
    else:
        console.print(
            "  [dim]2  Exact — unavailable for the selected runtime/model profile[/dim]"
        )
    console.print(
        "  [cyan]3[/cyan]  Reconstructed — start a fresh model conversation from facts, "
        "decisions, artifacts, events, and recent handoff"
    )
    console.print("  [cyan]4[/cyan]  Cancel")
    choices = ["1", "2", "3", "4"] if exact_available else ["1", "3", "4"]
    selected = Prompt.ask("Resume mode", choices=choices, default="1", console=console)
    if selected == "4":
        raise ScagentSDKError("resume cancelled")
    return {
        "1": ResumePreference.AUTO,
        "2": ResumePreference.EXACT,
        "3": ResumePreference.RECONSTRUCTED,
    }[selected]
