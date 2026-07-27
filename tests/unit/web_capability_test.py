from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _FakeHeaders:
    def __init__(self, content_type: str, charset: str | None):
        self._content_type = content_type
        self._charset = charset

    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str | None:
        return self._charset


class _FakePage:
    def __init__(self, url: str, content_type: str, body: bytes, charset: str = "utf-8"):
        self._url = url
        self.headers = _FakeHeaders(content_type, charset)
        self._body = body

    def __enter__(self) -> _FakePage:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def _public_getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list[Any]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]


def _fetch_handler() -> Any:
    package = _web_package()
    tool = next(item for item in package.manifest.tools if item.name == "fetch_web_page")
    return package.load_handler(tool)


def _web_package() -> SkillPackage:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "research-web"
    )


def test_tavily_result_is_normalized_without_persisting_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _web_package()
    tool = next(item for item in package.manifest.tools if item.name == "web_search")
    handler = package.load_handler(tool)
    secret = "tvly-test-secret"
    monkeypatch.setenv("TAVILY_API_KEY", secret)
    monkeypatch.setitem(
        handler.__globals__,
        "urlopen",
        lambda request, timeout: _Response(
            {
                "results": [
                    {
                        "title": "Official docs",
                        "url": "https://example.org/docs",
                        "content": "Evidence text",
                        "score": 0.9,
                    }
                ],
                "request_id": "request-1",
            }
        ),
    )
    context = SimpleNamespace(staging_dir=tmp_path)

    result = handler({"query": "package API", "include_domains": ["example.org"]}, context)

    assert result["details"]["results"][0]["url"] == "https://example.org/docs"
    artifact = (tmp_path / "search-results.json").read_text(encoding="utf-8")
    assert secret not in artifact
    assert "example.org" in artifact


def test_fetch_blocks_private_network_targets(tmp_path: Path) -> None:
    handler = _fetch_handler()

    with pytest.raises(ValueError, match="non-public"):
        handler(
            {"url": "http://127.0.0.1/private"},
            SimpleNamespace(staging_dir=tmp_path),
        )


def test_html_fetch_prefers_tavily_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _fetch_handler()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-secret")
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setitem(
        handler.__globals__,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda request, timeout: _FakePage(
                "https://example.org/docs",
                "text/html",
                b"<html><body><p>shell only</p></body></html>",
            )
        ),
    )
    monkeypatch.setitem(
        handler.__globals__,
        "urlopen",
        lambda request, timeout: _Response(
            {"results": [{"url": "https://example.org/docs", "raw_content": "Rendered doc body"}]}
        ),
    )

    result = handler(
        {"url": "https://example.org/docs"}, SimpleNamespace(staging_dir=tmp_path)
    )

    assert result["details"]["extraction_backend"] == "tavily-extract"
    assert result["details"]["content"] == "Rendered doc body"


def test_html_fetch_falls_back_to_direct_extraction_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _fetch_handler()
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setitem(
        handler.__globals__,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda request, timeout: _FakePage(
                "https://example.org/docs",
                "text/html",
                b"<html><body><p>Direct body text</p></body></html>",
            )
        ),
    )

    result = handler(
        {"url": "https://example.org/docs"}, SimpleNamespace(staging_dir=tmp_path)
    )

    assert result["details"]["extraction_backend"] == "direct-html"
    assert "Direct body text" in result["details"]["content"]
