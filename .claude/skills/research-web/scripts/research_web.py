"""Bounded Tavily search and public-page retrieval with durable provenance."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
TAVILY_EXTRACT_ENDPOINT = "https://api.tavily.com/extract"
MAX_TEXT_FETCH_BYTES = 2 * 1024 * 1024
MAX_PDF_FETCH_BYTES = 32 * 1024 * 1024
USER_AGENT = "scagent-sdk/0.1 (+scientific research assistant)"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _tavily_api_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


def _tavily_post(endpoint: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """POST to a Tavily endpoint with the key in the Authorization header, never the body."""
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read(MAX_TEXT_FETCH_BYTES).decode("utf-8"))


def _tavily_extract(url: str, api_key: str) -> str | None:
    """Return Tavily server-side extracted page text, or None if extraction is unavailable.

    Tavily fetches and renders the page on its own infrastructure, so this recovers text from
    JavaScript-rendered documentation that the local byte fetch cannot see. Any failure returns
    None so the caller falls back to the direct-fetch extractor.
    """
    payload = {"urls": [url], "extract_depth": "basic", "format": "text"}
    try:
        raw = _tavily_post(TAVILY_EXTRACT_ENDPOINT, payload, api_key)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    results = raw.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    content = results[0].get("raw_content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _domains(value: Any, *, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{name} must be a list of at most 20 domains")
    output: list[str] = []
    for item in value:
        domain = str(item).strip().lower()
        if "://" in domain:
            domain = urlparse(domain).hostname or ""
        domain = domain.removeprefix("www.").strip(".")
        if not domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
            raise ValueError(f"invalid domain in {name}: {item!r}")
        output.append(domain)
    return output


def web_search(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not 2 <= len(query) <= 400:
        raise ValueError("query must contain 2 to 400 characters")
    api_key = _tavily_api_key()
    if not api_key:
        raise RuntimeError(
            "TAVILY_API_KEY is not configured; add it to the project .env or export it"
        )
    max_results = int(arguments.get("max_results") or 5)
    if not 1 <= max_results <= 10:
        raise ValueError("max_results must be between 1 and 10")
    search_depth = str(arguments.get("search_depth") or "basic")
    topic = str(arguments.get("topic") or "general")
    if search_depth not in {"basic", "advanced"}:
        raise ValueError("search_depth must be basic or advanced")
    if topic not in {"general", "news"}:
        raise ValueError("topic must be general or news")
    include_domains = _domains(arguments.get("include_domains"), name="include_domains")
    exclude_domains = _domains(arguments.get("exclude_domains"), name="exclude_domains")
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "topic": topic,
        "include_domains": include_domains,
        "exclude_domains": exclude_domains,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    try:
        raw = _tavily_post(TAVILY_SEARCH_ENDPOINT, payload, api_key)
    except HTTPError as exc:
        raise RuntimeError(f"Tavily search failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Tavily search failed: {type(exc).__name__}: {exc}") from exc
    results: list[dict[str, Any]] = []
    for item in raw.get("results", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": str(item.get("content") or "")[:2000],
                "score": item.get("score"),
                "published_date": item.get("published_date"),
            }
        )
    evidence = {
        "backend": "tavily",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "search_depth": search_depth,
        "topic": topic,
        "include_domains": include_domains,
        "exclude_domains": exclude_domains,
        "results": results,
        "response_time": raw.get("response_time"),
        "request_id": raw.get("request_id"),
    }
    _write_json(context.staging_dir / "search-results.json", evidence)
    return {
        "schema_version": 1,
        "summary": f"Tavily returned {len(results)} source-linked results for: {query}",
        "details": evidence,
        "artifacts": [
            {
                "name": "web-search-results",
                "relative_path": "search-results.json",
                "media_type": "application/json",
            }
        ],
    }


def _validate_public_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port)}
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve URL hostname: {parsed.hostname}") from exc
    if not addresses:
        raise ValueError(f"URL hostname has no addresses: {parsed.hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"URL resolves to a non-public address: {parsed.hostname}")
    return value


class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        target = _validate_public_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, target)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored += 1
        elif not self.ignored and tag in {"p", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in {"p", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def fetch_web_page(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    url = _validate_public_url(str(arguments.get("url") or "").strip())
    max_chars = int(arguments.get("max_chars") or 30000)
    if not 1000 <= max_chars <= 40000:
        raise ValueError("max_chars must be between 1000 and 40000")
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    try:
        with build_opener(_SafeRedirects()).open(request, timeout=20) as response:
            final_url = _validate_public_url(response.geturl())
            content_type = response.headers.get_content_type().lower()
            charset = response.headers.get_content_charset() or "utf-8"
            is_pdf = content_type == "application/pdf" or response.geturl().lower().endswith(".pdf")
            byte_limit = MAX_PDF_FETCH_BYTES if is_pdf else MAX_TEXT_FETCH_BYTES
            data = response.read(byte_limit + 1)
    except HTTPError as exc:
        raise RuntimeError(f"web fetch failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"web fetch failed: {type(exc).__name__}: {exc}") from exc
    byte_truncated = len(data) > byte_limit
    if is_pdf and byte_truncated:
        raise ValueError(f"PDF exceeds the {MAX_PDF_FETCH_BYTES}-byte download limit")
    data = data[:byte_limit]
    artifacts: list[dict[str, str]] = []
    if is_pdf:
        output = context.staging_dir / "downloaded.pdf"
        output.write_bytes(data)
        details: dict[str, Any] = {
            "source_url": url,
            "final_url": final_url,
            "content_type": content_type,
            "size_bytes": len(data),
            "byte_truncated": byte_truncated,
            "next_step": "Inspect the committed downloaded.pdf artifact with inspect_pdf.",
        }
        artifacts.append(
            {
                "name": "downloaded-pdf",
                "relative_path": output.name,
                "media_type": "application/pdf",
            }
        )
        summary = f"Downloaded a public PDF from {final_url}; inspect the saved PDF visually next."
    elif content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xhtml+xml",
        "application/xml",
    }:
        decoded = data.decode(charset, errors="replace")
        if "html" in content_type:
            api_key = _tavily_api_key()
            extracted = _tavily_extract(final_url, api_key) if api_key else None
            if extracted is not None:
                text = extracted
                extraction_backend = "tavily-extract"
            else:
                parser = _TextExtractor()
                parser.feed(decoded)
                text = parser.text()
                extraction_backend = "direct-html"
        else:
            text = decoded
            extraction_backend = "direct-text"
        char_truncated = len(text) > max_chars
        text = text[:max_chars]
        output = context.staging_dir / "page.txt"
        output.write_text(text + ("\n" if text else ""), encoding="utf-8")
        details = {
            "source_url": url,
            "final_url": final_url,
            "content_type": content_type,
            "extraction_backend": extraction_backend,
            "size_bytes": len(data),
            "byte_truncated": byte_truncated,
            "char_truncated": char_truncated,
            "content": text,
        }
        artifacts.append(
            {
                "name": "web-page-text",
                "relative_path": output.name,
                "media_type": "text/plain",
            }
        )
        summary = f"Fetched {len(text)} characters from {final_url} via {extraction_backend}."
    else:
        raise ValueError(f"unsupported fetched content type: {content_type}")
    _write_json(context.staging_dir / "fetch-metadata.json", details)
    artifacts.append(
        {
            "name": "web-fetch-metadata",
            "relative_path": "fetch-metadata.json",
            "media_type": "application/json",
        }
    )
    return {
        "schema_version": 1,
        "summary": summary,
        "details": details,
        "artifacts": artifacts,
    }
