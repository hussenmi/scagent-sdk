"""Human-browsable projection of committed scientific-session artifacts.

Capability artifacts remain authoritative under ``artifacts/capabilities/<execution-id>``.
This module builds a disposable review surface from those records using relative symlinks, so
large scientific files are never copied merely to make a session easier to navigate.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

OUTPUT_VIEW_SCHEMA_VERSION = 1
OUTPUT_DIRECTORIES = ("code", "figures", "reports", "tables", "data")
_GENERATOR = "scagent-sdk"
_INDEX_JSON = "outputs.json"
_INDEX_MARKDOWN = "outputs.md"
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
_TABLE_SUFFIXES = frozenset({".csv", ".tsv", ".parquet", ".feather", ".xlsx", ".xls"})
_DATA_SUFFIXES = frozenset(
    {".h5ad", ".h5", ".hdf5", ".loom", ".zarr", ".npz", ".npy", ".mtx"}
)
_REPORT_SUFFIXES = frozenset({".md", ".pdf", ".html", ".htm"})
_IMAGE_MEDIA_PREFIX = "image/"
_TABLE_MEDIA_TYPES = frozenset(
    {
        "text/csv",
        "text/tab-separated-values",
        "application/parquet",
        "application/vnd.apache.parquet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
_DATA_MEDIA_TYPES = frozenset(
    {
        "application/x-hdf5",
        "application/x-h5ad",
        "application/x-anndata",
        "application/x-zarr",
        "application/x-numpy",
    }
)
_REPORT_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "text/markdown",
        "text/html",
    }
)


def _slugify(text: str, *, maxlen: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:maxlen].rstrip("-")


def _natural_key(text: str) -> tuple[tuple[int, str | int], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", text.lower())
        if part
    )


def _code_label(summary: str, fallback: str) -> str:
    text = summary.split(":", 1)[1] if ":" in summary else summary
    return _slugify(text, maxlen=48) or _slugify(fallback, maxlen=48) or "analysis"


def _classify(relative_path: str, media_type: str) -> str | None:
    suffix = Path(relative_path).suffix.lower()
    media_type = media_type.lower().split(";", 1)[0].strip()
    name = Path(relative_path).name.lower()
    if media_type == "text/x-python" or suffix == ".py":
        return "code"
    if media_type.startswith(_IMAGE_MEDIA_PREFIX) or suffix in _IMAGE_SUFFIXES:
        return "figures"
    if media_type in _TABLE_MEDIA_TYPES or suffix in _TABLE_SUFFIXES:
        return "tables"
    if media_type in _DATA_MEDIA_TYPES or suffix in _DATA_SUFFIXES:
        return "data"
    if media_type in _REPORT_MEDIA_TYPES or suffix in _REPORT_SUFFIXES:
        return "reports"
    if media_type == "text/plain" or suffix == ".txt":
        if "stdout" not in name and "stderr" not in name:
            return "reports"
    return None


def _is_final_data(name: str, relative_path: str, tool_name: str) -> bool:
    value = " ".join((name, relative_path, tool_name)).lower().replace("_", "-")
    return "final-annotated" in value or (
        "finalize-analysis" in value and Path(relative_path).suffix.lower() == ".h5ad"
    )


def _projected_name(
    *,
    category: str,
    execution_id: str,
    tool_name: str,
    summary: str,
    artifact_name: str,
    relative_path: str,
) -> str:
    suffix = Path(relative_path).suffix.lower()
    short_id = _slugify(execution_id, maxlen=8) or "unknown"
    if category == "code":
        return f"{_code_label(summary, artifact_name)}-{short_id}{suffix or '.py'}"
    artifact = _slugify(artifact_name) or _slugify(Path(relative_path).stem) or "output"
    tool = _slugify(tool_name, maxlen=48) or "capability"
    return f"{artifact}--{tool}--{short_id}{suffix}"


def _safe_source(session_dir: Path, artifact_path: str, relative_path: str) -> Path | None:
    root = session_dir.resolve()
    source = (session_dir / artifact_path / relative_path).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return None
    return source if source.is_file() else None


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_previous_paths(index_path: Path) -> set[str]:
    if not index_path.is_file():
        return set()
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if (
        not isinstance(value, dict)
        or value.get("generator") != _GENERATOR
        or value.get("schema_version") != OUTPUT_VIEW_SCHEMA_VERSION
    ):
        return set()
    paths: set[str] = set()
    categories = value.get("categories", {})
    if isinstance(categories, dict):
        for entries in categories.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("view_path"), str):
                    paths.add(entry["view_path"])
    aliases = value.get("aliases", [])
    if isinstance(aliases, list):
        for entry in aliases:
            if isinstance(entry, dict) and isinstance(entry.get("view_path"), str):
                paths.add(entry["view_path"])
    return paths


def _same_link(target: Path, source: Path) -> bool:
    if not target.is_symlink():
        return False
    try:
        return target.resolve() == source.resolve()
    except OSError:
        return False


def _available_target(
    requested: Path,
    *,
    source: Path,
    session_dir: Path,
    previous_paths: set[str],
) -> Path:
    candidate = requested
    attempt = 1
    while candidate.exists() or candidate.is_symlink():
        relative = str(candidate.relative_to(session_dir))
        if _same_link(candidate, source) or relative in previous_paths:
            return candidate
        attempt += 1
        suffix = requested.suffix
        candidate = requested.with_name(f"{requested.stem}--view-{attempt}{suffix}")
    return candidate


def _ensure_relative_link(
    target: Path,
    source: Path,
    *,
    session_dir: Path,
    previous_paths: set[str],
) -> Path:
    target = _available_target(
        target,
        source=source,
        session_dir=session_dir,
        previous_paths=previous_paths,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if _same_link(target, source):
        return target
    if target.is_symlink():
        target.unlink()
    relative_source = os.path.relpath(source, start=target.parent)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.symlink_to(relative_source)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _remove_identical_legacy_code_copy(target: Path, source: Path) -> None:
    """Remove only a byte-identical code mirror created by the earlier copy-based view."""

    if target.is_symlink() or not target.is_file():
        return
    try:
        same_size = target.stat().st_size == source.stat().st_size
        if same_size and target.read_bytes() == source.read_bytes():
            target.unlink()
    except OSError:
        return


def _projected_directory(
    session_dir: Path,
    *,
    category: str,
    relative_path: str,
    final_data: bool,
) -> Path:
    directory = session_dir / category
    if category == "data" and not final_data:
        directory /= "intermediates"
    if category == "code":
        return directory
    for part in Path(relative_path).parent.parts:
        slug = _slugify(part)
        if slug:
            directory /= slug
    return directory


def _cleanup_old_links(
    session_dir: Path,
    previous_paths: set[str],
    desired_paths: set[str],
) -> None:
    root = session_dir.resolve()
    for relative in sorted(previous_paths - desired_paths):
        target = session_dir / relative
        try:
            target.parent.resolve().relative_to(root)
        except ValueError:
            continue
        if target.is_symlink():
            target.unlink()


def _markdown_link(path: str) -> str:
    return quote(path, safe="/-._~")


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _render_markdown(index: Mapping[str, Any]) -> str:
    lines = [
        "<!-- generated by scagent-sdk; rebuildable from state.json -->",
        "# Session outputs",
        "",
        "This is the human-browsable view of committed results. The authoritative,",
        "provenance-bearing copies remain under `artifacts/capabilities/`.",
        "",
    ]
    aliases = index["aliases"]
    if aliases:
        lines.extend(["## Primary results", ""])
        for entry in aliases:
            path = entry["view_path"]
            lines.append(f"- [{Path(path).name}]({_markdown_link(path)})")
        lines.append("")
    labels = {
        "code": "Code",
        "figures": "Figures",
        "reports": "Reports",
        "tables": "Tables",
        "data": "Data",
    }
    for category in OUTPUT_DIRECTORIES:
        entries = index["categories"][category]
        lines.extend([f"## {labels[category]}", ""])
        if not entries:
            lines.extend(["_No committed outputs yet._", ""])
            continue
        lines.extend(["| Output | Produced by | Summary |", "|---|---|---|"])
        for entry in entries:
            view_path = entry["view_path"]
            link = f"[{Path(view_path).name}]({_markdown_link(view_path)})"
            lines.append(
                f"| {link} | `{_markdown_cell(entry['tool_name'])}` | "
                f"{_markdown_cell(entry['summary'])} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def refresh_output_view(
    session_dir: Path,
    artifacts: Mapping[str, Any],
    *,
    commit_sequences: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Rebuild the derived output view from committed artifact records.

    Regular files and unrecognized symlinks in the review directories are preserved. Generated
    links are relative and point directly at canonical artifacts, so moving the complete session
    keeps them valid and incurs no duplicate storage.
    """

    session_dir = session_dir.resolve()
    for child in OUTPUT_DIRECTORIES:
        (session_dir / child).mkdir(parents=True, exist_ok=True)
    (session_dir / "data" / "intermediates").mkdir(parents=True, exist_ok=True)

    previous_paths = _load_previous_paths(session_dir / _INDEX_JSON)
    sequences = commit_sequences or {}
    entries: list[dict[str, Any]] = []
    alias_candidates: dict[str, tuple[int, dict[str, Any], Path]] = {}

    ordered_artifacts = sorted(
        artifacts.items(),
        key=lambda item: (sequences.get(item[0], 0), item[0]),
    )
    for execution_id, raw_record in ordered_artifacts:
        if not isinstance(raw_record, Mapping):
            continue
        artifact_path = raw_record.get("path")
        files = raw_record.get("files")
        if not isinstance(artifact_path, str) or not isinstance(files, list):
            continue
        tool_name = str(raw_record.get("tool_name", "capability"))
        summary = str(raw_record.get("summary", ""))
        sequence = sequences.get(execution_id, 0)
        for raw_file in files:
            if not isinstance(raw_file, Mapping):
                continue
            relative_path = raw_file.get("relative_path")
            if not isinstance(relative_path, str):
                continue
            media_type = str(raw_file.get("media_type", "application/octet-stream"))
            category = _classify(relative_path, media_type)
            if category is None:
                continue
            source = _safe_source(session_dir, artifact_path, relative_path)
            if source is None:
                continue
            artifact_name = str(raw_file.get("name", Path(relative_path).stem))
            filename = _projected_name(
                category=category,
                execution_id=execution_id,
                tool_name=tool_name,
                summary=summary,
                artifact_name=artifact_name,
                relative_path=relative_path,
            )
            final_data = _is_final_data(artifact_name, relative_path, tool_name)
            directory = _projected_directory(
                session_dir,
                category=category,
                relative_path=relative_path,
                final_data=final_data,
            )
            if category == "code":
                _remove_identical_legacy_code_copy(directory / filename, source)
            target = _ensure_relative_link(
                directory / filename,
                source,
                session_dir=session_dir,
                previous_paths=previous_paths,
            )
            view_path = str(target.relative_to(session_dir))
            canonical_path = str(source.relative_to(session_dir))
            entry = {
                "execution_id": execution_id,
                "artifact_name": artifact_name,
                "media_type": media_type,
                "tool_name": tool_name,
                "summary": summary,
                "view_path": view_path,
                "canonical_path": canonical_path,
                "commit_sequence": sequence,
            }
            entries.append({"category": category, **entry})

            normalized = f"{artifact_name} {relative_path}".lower().replace("_", "-")
            alias_path: str | None = None
            if category == "data" and final_data:
                alias_path = f"data/final-annotated{Path(relative_path).suffix.lower()}"
            elif category == "reports" and "analysis-report" in normalized:
                alias_path = f"reports/final-analysis-report{Path(relative_path).suffix.lower()}"
            elif category == "tables" and "final-labels" in normalized:
                alias_path = f"tables/final-labels{Path(relative_path).suffix.lower()}"
            elif category == "code" and "analysis-recipe" in normalized:
                alias_path = f"code/analysis-recipe{Path(relative_path).suffix.lower()}"
            if alias_path is not None:
                current = alias_candidates.get(alias_path)
                if current is None or (sequence, execution_id) > (
                    current[0],
                    current[1]["execution_id"],
                ):
                    alias_candidates[alias_path] = (sequence, entry, source)

    categories: dict[str, list[dict[str, Any]]] = {
        category: [] for category in OUTPUT_DIRECTORIES
    }
    for item in entries:
        category = item.pop("category")
        categories[category].append(item)
    for category_entries in categories.values():
        category_entries.sort(
            key=lambda entry: (
                entry["commit_sequence"],
                entry["tool_name"],
                _natural_key(entry["view_path"]),
            )
        )

    aliases: list[dict[str, Any]] = []
    for alias_path, (_, source_entry, source) in sorted(alias_candidates.items()):
        requested = session_dir / alias_path
        if (requested.exists() or requested.is_symlink()) and not (
            _same_link(requested, source) or alias_path in previous_paths
        ):
            continue
        target = _ensure_relative_link(
            requested,
            source,
            session_dir=session_dir,
            previous_paths=previous_paths,
        )
        aliases.append(
            {
                "view_path": str(target.relative_to(session_dir)),
                "canonical_path": source_entry["canonical_path"],
                "execution_id": source_entry["execution_id"],
            }
        )

    index: dict[str, Any] = {
        "schema_version": OUTPUT_VIEW_SCHEMA_VERSION,
        "generator": _GENERATOR,
        "authoritative_root": "artifacts/capabilities",
        "categories": categories,
        "aliases": aliases,
    }
    desired_paths = {
        entry["view_path"] for category_entries in categories.values() for entry in category_entries
    }
    desired_paths.update(entry["view_path"] for entry in aliases)
    _cleanup_old_links(session_dir, previous_paths, desired_paths)
    _atomic_write_text(
        session_dir / _INDEX_JSON,
        json.dumps(index, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _atomic_write_text(session_dir / _INDEX_MARKDOWN, _render_markdown(index))
    return index
