"""Read-only diagnostics for an Anthropic-compatible model gateway."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from scagent_sdk.models.profile import ModelProfile


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    url: str


class HTTPTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse: ...


class UrllibHTTPTransport:
    def request(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HTTPResponse(
                    status=int(response.status),
                    body=response.read(),
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            return HTTPResponse(status=exc.code, body=exc.read(), url=url)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


class GatewayDoctor:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        transport: HTTPTransport | None = None,
        timeout: float = 5.0,
        environ: dict[str, str] | None = None,
    ):
        self.profile = profile
        self.transport = transport or UrllibHTTPTransport()
        self.timeout = timeout
        self.environ = environ

    def _headers(self) -> dict[str, str]:
        key = self.profile.resolve_api_key(self.environ)
        return {
            "authorization": f"Bearer {key}",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def profile_check(self) -> CheckResult:
        prompt_path = self.profile.prompt_path
        try:
            prompt = self.profile.read_system_prompt()
            self.profile.resolve_api_key(self.environ)
        except Exception as exc:
            return CheckResult("profile", "fail", str(exc))
        return CheckResult(
            "profile",
            "pass",
            f"profile {self.profile.name} is valid",
            {
                "fingerprint": self.profile.fingerprint,
                "system_prompt": str(prompt_path),
                "system_prompt_chars": len(prompt),
                "transport": self.profile.transport,
                "model": self.profile.model,
            },
        )

    def health_check(self) -> CheckResult:
        if not self.profile.base_url:
            return CheckResult(
                "gateway_health",
                "skip",
                "profile has no custom gateway URL",
            )
        failures: list[dict[str, Any]] = []
        for path in self.profile.health_paths:
            url = self.profile.base_url.rstrip("/") + path
            try:
                response = self.transport.request(
                    url=url,
                    method="GET",
                    headers=self._headers(),
                    body=None,
                    timeout=self.timeout,
                )
            except OSError as exc:
                failures.append({"url": url, "error": str(exc)})
                continue
            if 200 <= response.status < 300:
                return CheckResult(
                    "gateway_health",
                    "pass",
                    f"gateway health endpoint returned HTTP {response.status}",
                    {"url": response.url},
                )
            failures.append({"url": url, "status": response.status})
        return CheckResult(
            "gateway_health",
            "fail",
            "no configured gateway health endpoint succeeded",
            {"attempts": failures},
        )

    def messages_check(self) -> CheckResult:
        if not self.profile.base_url:
            return CheckResult(
                "anthropic_messages",
                "skip",
                "profile has no custom gateway URL",
            )
        url = self.profile.base_url.rstrip("/") + "/v1/messages"
        body = json.dumps(
            {
                "model": self.profile.model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            }
        ).encode("utf-8")
        try:
            response = self.transport.request(
                url=url,
                method="POST",
                headers=self._headers(),
                body=body,
                timeout=max(self.timeout, 30.0),
            )
        except OSError as exc:
            return CheckResult("anthropic_messages", "fail", str(exc), {"url": url})
        if not 200 <= response.status < 300:
            return CheckResult(
                "anthropic_messages",
                "fail",
                f"messages probe returned HTTP {response.status}",
                {"url": url, "body": response.body.decode("utf-8", errors="replace")[-1000:]},
            )
        try:
            payload = json.loads(response.body)
            content = payload.get("content")
            if not isinstance(content, list) or not content:
                raise ValueError("response has no Anthropic content blocks")
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            return CheckResult(
                "anthropic_messages",
                "fail",
                f"gateway response is not valid Anthropic Messages JSON: {exc}",
                {"url": url},
            )
        return CheckResult(
            "anthropic_messages",
            "pass",
            "gateway accepted an Anthropic Messages request",
            {"url": url, "content_blocks": len(content)},
        )

    def run(self, *, probe_messages: bool = False) -> list[CheckResult]:
        checks = [self.profile_check(), self.health_check()]
        if probe_messages:
            checks.append(self.messages_check())
        return checks
