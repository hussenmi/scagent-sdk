from __future__ import annotations

from pathlib import Path

from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.gateway import GatewaySupervisor


def test_gateway_does_not_manage_an_already_ready_service(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system", encoding="utf-8")
    profile = ModelProfile(
        name="ready",
        runtime="claude-agent-sdk",
        transport="anthropic",
        model="model",
        system_prompt=str(prompt),
        allow_noauth=True,
    )
    supervisor = GatewaySupervisor(
        profile,
        config_path=None,
        log_path=tmp_path / "gateway.log",
    )

    assert supervisor.ensure() is False
    assert supervisor.managed is False
