"""Structured biomedical literature search and open-access full-text retrieval.

Backend: Europe PMC REST API. Europe PMC indexes the whole of MEDLINE/PubMed plus
PubMed Central and preprint servers (bioRxiv/medRxiv), returns clean JSON, and exposes
open-access flags and PMCIDs, so a single coherent API covers both citation-grade metadata
search and full-text retrieval without fragile XML scraping.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
SEARCH_ENDPOINT = f"{EUROPEPMC_BASE}/search"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_FULLTEXT_BYTES = 8 * 1024 * 1024
USER_AGENT = "scagent-sdk/0.1 (+scientific research assistant)"
_PMID_RE = re.compile(r"^\d{1,9}$")
_PMCID_RE = re.compile(r"^PMC\d{1,9}$")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _get(url: str, params: dict[str, Any], *, byte_limit: int) -> bytes:
    target = f"{url}?{urlencode(params)}" if params else url
    request = Request(
        target,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    with urlopen(request, timeout=25) as response:
        return response.read(byte_limit + 1)[:byte_limit]


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    raw = _get(url, params, byte_limit=MAX_JSON_BYTES)
    return json.loads(raw.decode("utf-8"))


def _clean_markup(value: Any) -> str:
    """Europe PMC escapes inline JATS markup, so titles arrive as '&lt;i&gt;THBS1&lt;/i&gt;'.

    Unescape first, then strip the revealed tags, so gene names survive intact.
    """
    text = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()


def _journal(result: dict[str, Any]) -> str:
    info = result.get("journalInfo")
    if isinstance(info, dict):
        journal = info.get("journal")
        if isinstance(journal, dict):
            title = _clean_markup(journal.get("title"))
            if title:
                return title
    return _clean_markup(result.get("journalTitle"))


def _publication_types(result: dict[str, Any]) -> list[str]:
    container = result.get("pubTypeList")
    if not isinstance(container, dict):
        return []
    types = container.get("pubType")
    if isinstance(types, str):
        return [types]
    if isinstance(types, list):
        return [str(item) for item in types if item]
    return []


def _best_url(pmid: str, pmcid: str, doi: str, source: str, record_id: str) -> str:
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if doi:
        return f"https://doi.org/{doi}"
    if pmcid:
        return f"https://europepmc.org/article/PMC/{pmcid}"
    return f"https://europepmc.org/article/{source or 'MED'}/{record_id}"


def _normalize(result: dict[str, Any]) -> dict[str, Any]:
    pmid = str(result.get("pmid") or "")
    pmcid = str(result.get("pmcid") or "")
    doi = str(result.get("doi") or "")
    source = str(result.get("source") or "")
    record_id = str(result.get("id") or "")
    is_open_access = str(result.get("isOpenAccess") or "").upper() == "Y"
    # inEPMC is the authoritative signal that full text is retrievable here. isOpenAccess alone
    # over-promises: OA records regularly have no Europe PMC full text and 404 on fullTextXML.
    in_epmc = str(result.get("inEPMC") or "").upper() == "Y"
    pub_types = _publication_types(result)
    return {
        "id": record_id,
        "source": source,
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": _clean_markup(result.get("title")),
        "authors": _clean_markup(result.get("authorString")),
        "journal": _journal(result),
        "year": str(result.get("pubYear") or ""),
        "publication_date": str(result.get("firstPublicationDate") or ""),
        "publication_types": pub_types,
        "is_review": any("review" in item.lower() for item in pub_types),
        "is_preprint": source == "PPR",
        "is_open_access": is_open_access,
        "full_text_available": bool(in_epmc and pmcid),
        "has_pdf": str(result.get("hasPDF") or "").upper() == "Y",
        "cited_by_count": result.get("citedByCount"),
        "abstract": _clean_markup(result.get("abstractText"))[:2000],
        "url": _best_url(pmid, pmcid, doi, source, record_id),
    }


def search_literature(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not 2 <= len(query) <= 400:
        raise ValueError("query must contain 2 to 400 characters")
    max_results = int(arguments.get("max_results") or 8)
    if not 1 <= max_results <= 25:
        raise ValueError("max_results must be between 1 and 25")
    sort = str(arguments.get("sort") or "relevance")
    if sort not in {"relevance", "date"}:
        raise ValueError("sort must be relevance or date")

    filters: list[str] = []
    if arguments.get("reviews_only"):
        filters.append('PUB_TYPE:"Review"')
    if arguments.get("open_access_only"):
        filters.append("OPEN_ACCESS:Y")
    if arguments.get("pubmed_only"):
        filters.append("SRC:MED")
    elif not arguments.get("include_preprints", True):
        filters.append('NOT (SRC:"PPR")')
    recent_years = arguments.get("recent_years")
    if recent_years is not None:
        recent_years = int(recent_years)
        if not 1 <= recent_years <= 50:
            raise ValueError("recent_years must be between 1 and 50")
        current_year = datetime.now(timezone.utc).year
        filters.append(f"PUB_YEAR:[{current_year - recent_years + 1} TO {current_year}]")

    full_query = f"({query})" + "".join(f" AND {clause}" for clause in filters)
    params: dict[str, Any] = {
        "query": full_query,
        "format": "json",
        "resultType": "core",
        "pageSize": max_results,
    }
    if sort == "date":
        params["sort"] = "P_PDATE_D desc"
    try:
        raw = _get_json(SEARCH_ENDPOINT, params)
    except HTTPError as exc:
        raise RuntimeError(f"Europe PMC search failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Europe PMC search failed: {type(exc).__name__}: {exc}") from exc

    hits = raw.get("resultList", {}).get("result", [])
    results = [_normalize(item) for item in hits[:max_results] if isinstance(item, dict)]
    total = raw.get("hitCount")
    evidence = {
        "backend": "europepmc",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "resolved_query": full_query,
        "sort": sort,
        "total_hits": total,
        "returned": len(results),
        "results": results,
    }
    _write_json(context.staging_dir / "literature-results.json", evidence)
    open_access = sum(1 for item in results if item["full_text_available"])
    if total is not None:
        summary = (
            f"Europe PMC returned {len(results)} of {total} matching records for: {query} "
            f"({open_access} with open-access full text)."
        )
    else:
        summary = f"Europe PMC returned {len(results)} records for: {query}."
    return {
        "schema_version": 1,
        "summary": summary,
        "details": evidence,
        "artifacts": [
            {
                "name": "literature-search-results",
                "relative_path": "literature-results.json",
                "media_type": "application/json",
            }
        ],
    }


def _resolve_pmcid_from_pmid(pmid: str) -> tuple[str, bool]:
    params = {
        "query": f"EXT_ID:{pmid} AND SRC:MED",
        "format": "json",
        "resultType": "core",
        "pageSize": 1,
    }
    try:
        raw = _get_json(SEARCH_ENDPOINT, params)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not resolve PMID {pmid}: {type(exc).__name__}") from exc
    hits = raw.get("resultList", {}).get("result", [])
    if not hits or not isinstance(hits[0], dict):
        raise RuntimeError(f"no Europe PMC record found for PMID {pmid}")
    record = hits[0]
    pmcid = str(record.get("pmcid") or "")
    in_epmc = str(record.get("inEPMC") or "").upper() == "Y"
    return pmcid, in_epmc


def _extract_body_text(xml: str) -> str:
    body = re.search(r"<body\b[^>]*>(.*?)</body>", xml, re.DOTALL | re.IGNORECASE)
    fragment = body.group(1) if body else xml
    fragment = re.sub(r"<(ref-list|table-wrap|fig)\b.*?</\1>", " ", fragment, flags=re.DOTALL)
    fragment = re.sub(r"</(p|sec|title|abstract|li|tr)>", "\n", fragment, flags=re.IGNORECASE)
    # Strip real JATS tags, then unescape, then strip the inline markup that unescaping reveals.
    # Doing it in the other order leaves literal '<i>' around gene symbols in the body text.
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    text = re.sub(r"<[^>]+>", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_article_fulltext(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    pmcid = str(arguments.get("pmcid") or "").strip().upper()
    pmid = str(arguments.get("pmid") or "").strip()
    max_chars = int(arguments.get("max_chars") or 30000)
    if not 1000 <= max_chars <= 40000:
        raise ValueError("max_chars must be between 1000 and 40000")
    if pmcid and not _PMCID_RE.fullmatch(pmcid):
        raise ValueError("pmcid must look like PMC followed by digits, e.g. PMC7654321")
    if pmid and not _PMID_RE.fullmatch(pmid):
        raise ValueError("pmid must be a numeric PubMed identifier")
    if not pmcid and not pmid:
        raise ValueError("provide a pmcid (preferred) or a pmid")

    resolved_from_pmid = False
    if not pmcid:
        pmcid, in_epmc = _resolve_pmcid_from_pmid(pmid)
        resolved_from_pmid = True
        if not pmcid or not in_epmc:
            raise RuntimeError(
                f"PMID {pmid} has no open-access full text in Europe PMC; "
                "fetch the article URL with research-web or inspect its PDF instead"
            )

    url = f"{EUROPEPMC_BASE}/{pmcid}/fullTextXML"
    try:
        raw = _get(url, {}, byte_limit=MAX_FULLTEXT_BYTES)
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"{pmcid} has no open-access full text in Europe PMC; "
                "fetch the article URL with research-web or inspect its PDF instead"
            ) from exc
        raise RuntimeError(f"full-text fetch failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"full-text fetch failed: {type(exc).__name__}: {exc}") from exc

    text = _extract_body_text(raw.decode("utf-8", errors="replace"))
    if not text:
        raise RuntimeError(f"{pmcid} full text could not be parsed into readable body text")
    char_truncated = len(text) > max_chars
    text = text[:max_chars]
    output = context.staging_dir / "fulltext.txt"
    output.write_text(text + "\n", encoding="utf-8")
    details = {
        "backend": "europepmc",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "pmcid": pmcid,
        "pmid": pmid,
        "resolved_from_pmid": resolved_from_pmid,
        "source_url": url,
        "article_url": f"https://europepmc.org/article/PMC/{pmcid}",
        "char_truncated": char_truncated,
        "content": text,
    }
    _write_json(context.staging_dir / "fulltext-metadata.json", details)
    return {
        "schema_version": 1,
        "summary": f"Fetched {len(text)} characters of open-access full text for {pmcid}.",
        "details": details,
        "artifacts": [
            {
                "name": "article-full-text",
                "relative_path": "fulltext.txt",
                "media_type": "text/plain",
            },
            {
                "name": "article-full-text-metadata",
                "relative_path": "fulltext-metadata.json",
                "media_type": "application/json",
            },
        ],
    }
