from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage


def _literature_package() -> SkillPackage:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "research-literature"
    )


def _handler(tool_name: str) -> Any:
    package = _literature_package()
    tool = next(item for item in package.manifest.tools if item.name == tool_name)
    return package.load_handler(tool)


class _BytesResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self) -> _BytesResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


def _json_response(payload: dict[str, Any]) -> _BytesResponse:
    return _BytesResponse(json.dumps(payload).encode("utf-8"))


def _search_payload(results: list[dict[str, Any]], hit_count: int | None = None) -> dict[str, Any]:
    return {
        "hitCount": len(results) if hit_count is None else hit_count,
        "resultList": {"result": results},
    }


_RECORD = {
    "id": "42172319",
    "source": "MED",
    "pmid": "42172319",
    "pmcid": "PMC13196778",
    "doi": "10.1126/sciadv.example",
    # Europe PMC escapes inline JATS markup in titles and abstracts.
    "title": "Multiregional profiling reveals &lt;i&gt;THBS1&lt;/i&gt; monocyte axis.",
    "abstractText": "Colorectal cancer &lt;i&gt;SPP1&lt;/i&gt; macrophages drive suppression.",
    "authorString": "Doe J, Roe A.",
    "journalInfo": {"journal": {"title": "Science advances"}},
    "pubYear": "2026",
    "firstPublicationDate": "2026-03-11",
    "pubTypeList": {"pubType": ["Journal Article", "Review"]},
    "isOpenAccess": "Y",
    "inEPMC": "Y",
    "hasPDF": "Y",
    "citedByCount": 4,
}


def _capture_search(
    monkeypatch: pytest.MonkeyPatch, handler: Any, payload: dict[str, Any]
) -> list[str]:
    """Patch urlopen to record requested URLs and return a fixed search payload."""
    seen: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> _BytesResponse:
        seen.append(request.full_url)
        return _json_response(payload)

    monkeypatch.setitem(handler.__globals__, "urlopen", fake_urlopen)
    return seen


def test_search_normalizes_records_and_strips_escaped_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler("search_literature")
    _capture_search(monkeypatch, handler, _search_payload([_RECORD]))

    result = handler({"query": "THBS1 monocyte"}, SimpleNamespace(staging_dir=tmp_path))

    record = result["details"]["results"][0]
    # Gene symbols must survive the escaped <i> markup intact.
    assert record["title"] == "Multiregional profiling reveals THBS1 monocyte axis."
    assert "SPP1 macrophages" in record["abstract"]
    assert "&lt;" not in record["title"] and "<i>" not in record["title"]
    assert record["journal"] == "Science advances"
    assert record["pmid"] == "42172319"
    assert record["pmcid"] == "PMC13196778"
    assert record["url"] == "https://pubmed.ncbi.nlm.nih.gov/42172319/"
    assert record["is_review"] is True
    assert record["is_preprint"] is False
    assert record["full_text_available"] is True
    assert (tmp_path / "literature-results.json").exists()


def test_full_text_availability_uses_in_epmc_not_open_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open-access record without Europe PMC full text must not promise full text."""
    handler = _handler("search_literature")
    record = {**_RECORD, "isOpenAccess": "Y", "inEPMC": "N"}
    _capture_search(monkeypatch, handler, _search_payload([record]))

    result = handler({"query": "anything"}, SimpleNamespace(staging_dir=tmp_path))

    entry = result["details"]["results"][0]
    assert entry["is_open_access"] is True
    assert entry["full_text_available"] is False


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"reviews_only": True}, 'PUB_TYPE:"Review"'),
        ({"open_access_only": True}, "OPEN_ACCESS:Y"),
        ({"pubmed_only": True}, "SRC:MED"),
        ({"include_preprints": False}, 'NOT (SRC:"PPR")'),
    ],
)
def test_query_filters_compose_into_resolved_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    expected: str,
) -> None:
    handler = _handler("search_literature")
    _capture_search(monkeypatch, handler, _search_payload([]))

    result = handler(
        {"query": "T cell exhaustion", **arguments}, SimpleNamespace(staging_dir=tmp_path)
    )

    resolved = result["details"]["resolved_query"]
    assert resolved.startswith("(T cell exhaustion)")
    assert expected in resolved


def test_pubmed_only_supersedes_preprint_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler("search_literature")
    _capture_search(monkeypatch, handler, _search_payload([]))

    result = handler(
        {"query": "markers", "pubmed_only": True, "include_preprints": False},
        SimpleNamespace(staging_dir=tmp_path),
    )

    resolved = result["details"]["resolved_query"]
    assert "SRC:MED" in resolved
    assert "PPR" not in resolved


def test_no_matches_returns_empty_results_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler("search_literature")
    _capture_search(monkeypatch, handler, _search_payload([], hit_count=0))

    result = handler({"query": "zzzz nonexistent"}, SimpleNamespace(staging_dir=tmp_path))

    assert result["details"]["results"] == []
    assert result["details"]["total_hits"] == 0


def test_transport_failure_raises_instead_of_returning_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy swallowed every exception into []; a failed search must be distinguishable."""
    handler = _handler("search_literature")

    def boom(request: Any, timeout: int) -> _BytesResponse:
        raise HTTPError(request.full_url, 500, "Server Error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setitem(handler.__globals__, "urlopen", boom)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        handler({"query": "markers"}, SimpleNamespace(staging_dir=tmp_path))


def test_fulltext_extracts_body_and_drops_reference_and_table_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler("fetch_article_fulltext")
    xml = (
        b"<article><front><title>ignored front matter</title></front>"
        b"<body><sec><title>Introduction</title>"
        b"<p>Metastasis drives mortality in &lt;i&gt;CRC&lt;/i&gt;.</p>"
        b"<table-wrap><p>TABLE NOISE</p></table-wrap>"
        b"<fig><p>FIGURE NOISE</p></fig></sec></body>"
        b"<back><ref-list><ref><p>REFERENCE NOISE</p></ref></ref-list></back></article>"
    )
    seen: list[str] = []

    def fake_urlopen(request: Any, timeout: int) -> _BytesResponse:
        seen.append(request.full_url)
        return _BytesResponse(xml)

    monkeypatch.setitem(handler.__globals__, "urlopen", fake_urlopen)

    result = handler({"pmcid": "PMC13196778"}, SimpleNamespace(staging_dir=tmp_path))

    content = result["details"]["content"]
    assert "Metastasis drives mortality in CRC." in content
    assert "TABLE NOISE" not in content
    assert "FIGURE NOISE" not in content
    assert "REFERENCE NOISE" not in content
    assert "ignored front matter" not in content
    # The endpoint takes the PMCID directly, with no source path segment.
    assert seen[0].endswith("/PMC13196778/fullTextXML")
    assert (tmp_path / "fulltext.txt").exists()


def test_fulltext_missing_open_access_gives_fallback_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler("fetch_article_fulltext")

    def not_found(request: Any, timeout: int) -> _BytesResponse:
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setitem(handler.__globals__, "urlopen", not_found)

    with pytest.raises(RuntimeError, match="research-web"):
        handler({"pmcid": "PMC999"}, SimpleNamespace(staging_dir=tmp_path))


@pytest.mark.parametrize(
    "arguments",
    [
        {"pmcid": "PMC1/../../etc/passwd"},
        {"pmcid": "not-a-pmcid"},
        {"pmid": "abc"},
        {},
    ],
)
def test_fulltext_rejects_malformed_identifiers(tmp_path: Path, arguments: dict[str, Any]) -> None:
    handler = _handler("fetch_article_fulltext")

    with pytest.raises(ValueError):
        handler(arguments, SimpleNamespace(staging_dir=tmp_path))
