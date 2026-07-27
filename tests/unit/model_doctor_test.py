from __future__ import annotations

import json
from pathlib import Path

from scagent_sdk.doctor.model import GatewayDoctor, HTTPResponse
from scagent_sdk.models.profile import ModelProfile


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse | OSError]):
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, OSError):
            raise response
        return response


def _profile(tmp_path: Path) -> ModelProfile:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("System prompt", encoding="utf-8")
    return ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://127.0.0.1:4000",
        allow_noauth=True,
        health_paths=("/health/liveliness", "/health"),
    )


def test_doctor_falls_through_health_paths_and_probes_messages(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            HTTPResponse(404, b"", "http://127.0.0.1:4000/health/liveliness"),
            HTTPResponse(200, b"ok", "http://127.0.0.1:4000/health"),
            HTTPResponse(
                200,
                json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode(),
                "http://127.0.0.1:4000/v1/messages",
            ),
        ]
    )
    results = GatewayDoctor(_profile(tmp_path), transport=transport).run(probe_messages=True)

    assert [result.status for result in results] == ["pass", "pass", "pass"]
    assert transport.requests[-1]["method"] == "POST"


def test_doctor_reports_unreachable_gateway(tmp_path: Path) -> None:
    transport = FakeTransport([OSError("refused"), OSError("refused")])
    result = GatewayDoctor(_profile(tmp_path), transport=transport).health_check()

    assert result.status == "fail"
    assert len(result.details["attempts"]) == 2


def test_doctor_rejects_non_anthropic_message_shape(tmp_path: Path) -> None:
    transport = FakeTransport([HTTPResponse(200, b'{"choices": []}', "gateway")])
    result = GatewayDoctor(_profile(tmp_path), transport=transport).messages_check()

    assert result.status == "fail"
    assert "Anthropic Messages" in result.summary
