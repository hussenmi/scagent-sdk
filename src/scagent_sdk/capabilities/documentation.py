"""Plain-language documentation a capability ships beside the files it produced.

A committed artifact folder is a pile of PNGs and CSVs whose meaning lives entirely in the
conversation that produced them. That conversation is gone by the time anyone -- a collaborator, a
reviewer, the user six weeks later -- opens the session directory, so every capability that writes
a folder a human has to interpret can declare a ``documentation`` block in its result envelope and
the runtime renders it to ``README.md`` next to those files.

Division of labour follows the rest of the SDK: the *skill* supplies the content, because what was
computed and what it showed is scientific knowledge that belongs with the science; the runtime only
renders and commits it, so the shape is identical everywhere and no skill hand-rolls markdown.

Two rules make these READMEs worth reading rather than boilerplate:

* **Findings are dataset-specific.** ``findings`` states what these results actually showed -- real
  cluster ids, real counts, real verdicts -- not what the method computes in general. A skill that
  can only restate its own method should leave findings empty rather than pad it.
* **No jargon without its meaning.** The audience is a scientist who did not run the analysis and
  does not know this pipeline's vocabulary. Name the metric, then say what it means in words.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from scagent_sdk.contracts._json import require_mapping
from scagent_sdk.errors import CapabilityExecutionError

README_FILENAME = "README.md"
_GENERATED_NOTE = (
    "_Written automatically when this step ran, from the same measurements the analysis used._"
)


def _text(value: Any) -> str:
    # A missing field must read as absent, not as the string "None", or every required-field check
    # below passes on nothing.
    return "" if value is None else str(value).strip()


def _lines(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise CapabilityExecutionError(f"documentation {field_name} must be a list of strings")
    return tuple(_text(item) for item in value if _text(item))


@dataclass(frozen=True)
class DocumentedFile:
    """One file in the documented folder, described for someone who has not seen it."""

    filename: str
    purpose: str
    how_to_read: str = ""
    columns: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any) -> DocumentedFile:
        data = require_mapping(value, name="documented file")
        filename = _text(data.get("filename"))
        purpose = _text(data.get("purpose"))
        if not filename:
            raise CapabilityExecutionError("documented file requires a filename")
        if not purpose:
            raise CapabilityExecutionError(f"documented file {filename!r} requires a purpose")
        columns = require_mapping(data.get("columns", {}), name="documented file columns")
        return cls(
            filename=filename,
            purpose=purpose,
            how_to_read=_text(data.get("how_to_read", "")),
            columns={str(key): _text(item) for key, item in columns.items()},
        )


@dataclass(frozen=True)
class ArtifactDocumentation:
    """A capability's own account of what it did, how, and what it found.

    ``directory`` places the README beside the files it describes, which matters for skills that
    group output per subject (one folder per clustering, per sample); the default writes it at the
    root of the execution's artifact folder.
    """

    name: str
    title: str
    directory: str = ""
    overview: str = ""
    what_was_done: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    files: tuple[DocumentedFile, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    caveats: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> ArtifactDocumentation:
        data = require_mapping(value, name="documentation")
        name = _text(data.get("name"))
        title = _text(data.get("title"))
        if not name:
            raise CapabilityExecutionError("documentation requires a name")
        if not title:
            raise CapabilityExecutionError("documentation requires a title")
        directory = _text(data.get("directory", "")).strip("/")
        if directory:
            path = PurePosixPath(directory)
            if path.is_absolute() or ".." in path.parts:
                raise CapabilityExecutionError(
                    f"documentation directory must stay inside the artifact folder: {directory}"
                )
        files = data.get("files", [])
        if not isinstance(files, (list, tuple)):
            raise CapabilityExecutionError("documentation files must be a list")
        parameters = require_mapping(data.get("parameters", {}), name="documentation parameters")
        return cls(
            name=name,
            title=title,
            directory=directory,
            overview=_text(data.get("overview", "")),
            what_was_done=_lines(data.get("what_was_done"), field_name="what_was_done"),
            findings=_lines(data.get("findings"), field_name="findings"),
            files=tuple(DocumentedFile.from_dict(item) for item in files),
            parameters=dict(parameters),
            caveats=_lines(data.get("caveats"), field_name="caveats"),
        )

    @property
    def relative_path(self) -> str:
        return f"{self.directory}/{README_FILENAME}" if self.directory else README_FILENAME


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "not set"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, Mapping):
        return ", ".join(f"{key}: {_format_value(item)}" for key, item in value.items()) or "none"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_format_value(item) for item in value) or "none"
    return str(value)


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_readme(doc: ArtifactDocumentation) -> str:
    """Render the documentation as markdown a non-specialist can read top to bottom."""

    lines: list[str] = [f"# {doc.title}", "", _GENERATED_NOTE, ""]
    if doc.overview:
        lines.extend([doc.overview, ""])
    if doc.what_was_done:
        lines.extend(["## What was done", ""])
        lines.extend(f"{index}. {item}" for index, item in enumerate(doc.what_was_done, start=1))
        lines.append("")
    if doc.findings:
        lines.extend(["## What this found", ""])
        lines.extend(f"- {item}" for item in doc.findings)
        lines.append("")
    if doc.parameters:
        lines.extend(["## Settings used", "", "| Setting | Value |", "| --- | --- |"])
        lines.extend(
            f"| {_cell(str(key))} | {_cell(_format_value(value))} |"
            for key, value in doc.parameters.items()
        )
        lines.append("")
    if doc.files:
        lines.extend(["## Files here", ""])
        for entry in doc.files:
            lines.extend([f"### `{entry.filename}`", "", entry.purpose, ""])
            if entry.how_to_read:
                lines.extend([f"**How to read it.** {entry.how_to_read}", ""])
            if entry.columns:
                lines.extend(["| Column | What it means |", "| --- | --- |"])
                lines.extend(
                    f"| `{_cell(str(column))}` | {_cell(meaning)} |"
                    for column, meaning in entry.columns.items()
                )
                lines.append("")
    if doc.caveats:
        lines.extend(["## What this does not tell you", ""])
        lines.extend(f"- {item}" for item in doc.caveats)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
