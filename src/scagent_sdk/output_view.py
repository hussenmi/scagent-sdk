"""Human-browsable projection of committed scientific-session artifacts.

Capability artifacts remain authoritative under ``artifacts/capabilities/<execution-id>``.
This module builds a disposable review surface from those records using relative symlinks, so
large scientific files are never copied merely to make a session easier to navigate.

Most categories project one file to one link, named ``<artifact-name>--<execution-short-id>`` so
the descriptive part of the name is what a reader sees first. Naming a projection usefully is
therefore the producing skill's job: when a tool can emit the same kind of artifact for different
subjects, its artifact names must carry the subject (the embedding and the column it is coloured
by, the cluster key and resolution) or the projection cannot describe the file either.

Shell runs instead project as a bundle directory per execution --
``shell/003-read-end-of-events-jsonl-f513a638/{command,stdout,stderr,run}`` -- because a command is
only interpretable together with the output it produced. The bundle is named for the function the
command performed rather than for its text, so a listing reads as what the session did.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

OUTPUT_VIEW_SCHEMA_VERSION = 2
OUTPUT_DIRECTORIES = ("code", "shell", "figures", "reports", "tables", "data")
_GENERATOR = "scagent-sdk"
_INDEX_JSON = "outputs.json"
_INDEX_MARKDOWN = "outputs.md"
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
_TABLE_SUFFIXES = frozenset({".csv", ".tsv", ".parquet", ".feather", ".xlsx", ".xls"})
_DATA_SUFFIXES = frozenset(
    {".h5ad", ".h5", ".hdf5", ".loom", ".zarr", ".npz", ".npy", ".mtx"}
)
# A notebook is a document to read, so it belongs with the reports rather than with the code the
# analysis ran; it is classified before code so its JSON body is never mistaken for a script.
_REPORT_SUFFIXES = frozenset({".md", ".pdf", ".html", ".htm", ".ipynb"})
_NOTEBOOK_MEDIA_TYPES = frozenset({"application/x-ipynb+json", "application/ipynb+json"})
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
_SHELL_MEDIA_TYPES = frozenset({"text/x-shellscript", "application/x-sh"})
_SHELL_SUFFIXES = frozenset({".sh", ".bash"})
_JSON_MEDIA_TYPES = frozenset({"application/json", "text/json"})
# A shell run report is a handful of scalars; anything larger is not the report this expects.
_SHELL_REPORT_MAX_BYTES = 256 * 1024
# A label describes a function ("search-evaluate-py-for-artifact-relative-path"), which needs more
# room than an abbreviated command did.
_SHELL_LABEL_MAXLEN = 56
_SHELL_OBJECT_MAXLEN = 32
_SHELL_COMMAND_CELL_MAXLEN = 120
# Reading order within a bundle: what ran, what it printed, then how it ran.
_SHELL_MEMBER_ORDER = ("command", "stdout", "stderr", "run")
_PROTECTED_DIRECTORIES = frozenset({"data/intermediates"})
_ABSOLUTE_PATH = re.compile(r"(?<![:/\w.])/(?:[\w.@%+=~-]+/)+([\w.@%+=~-]+)?")
# ``shlex`` with ``punctuation_chars`` emits these as tokens of their own, which is what lets one
# pass both tokenize a command and cut it into the steps it performs.
_SHELL_SEPARATORS = frozenset({"&&", "||", "|", "|&", ";", "&", "(", ")", "{", "}"})
_SHELL_REDIRECTIONS = frozenset({">", ">>", ">|", ">&", "&>", "&>>", "<", "<<", "<<<", "<&"})
# Commands that only position a shell. Naming a run after one of these describes nothing, so a
# leading ``cd``/``source`` clause is skipped in favour of the step that did the work.
_SHELL_PREAMBLE_HEADS = frozenset(
    {"cd", "pushd", "popd", "set", "source", ".", "export", "unset", "true", ":", "clear"}
)
# What each command does, in the reader's terms rather than the shell's. A verb ending in a
# preposition expects an object; when the command has none, the preposition is dropped.
_SHELL_VERBS = {
    "awk": "extract-from",
    "basename": "name-of",
    "bat": "read",
    "cat": "read",
    "chmod": "set-permissions-on",
    "chown": "set-owner-of",
    "cmp": "compare",
    "cp": "copy",
    "cut": "extract-columns-from",
    "df": "check-free-space",
    "diff": "compare",
    "du": "measure-size-of",
    "echo": "print",
    "file": "identify",
    "find": "find-files-in",
    "gunzip": "decompress",
    "gzip": "compress",
    "head": "read-start-of",
    "jq": "query-json-in",
    "kill": "stop-process",
    "less": "read",
    "ln": "link",
    "ls": "list",
    "md5sum": "checksum",
    "mkdir": "create-directory",
    "more": "read",
    "mv": "move",
    "mypy": "type-check",
    "nvidia-smi": "check-gpus",
    "printf": "print",
    "ps": "list-processes",
    "pytest": "run-tests",
    "rm": "delete",
    "rmdir": "remove-directory",
    "ruff": "lint",
    # Without an in-place flag a stream editor prints selected or transformed lines.
    "sed": "extract-from",
    "sha1sum": "checksum",
    "sha256sum": "checksum",
    "shasum": "checksum",
    "sort": "sort",
    "stat": "inspect",
    "tail": "read-end-of",
    "touch": "create",
    "tr": "translate-characters-in",
    "unzip": "decompress",
    "uniq": "deduplicate",
    "wc": "count-lines-in",
    "wget": "download",
    "which": "locate-command",
    "zcat": "decompress",
}
_SHELL_SEARCH_HEADS = frozenset({"ack", "ag", "egrep", "fgrep", "grep", "rg", "ugrep"})
_SHELL_STREAM_EDIT_HEADS = frozenset({"awk", "perl", "sed"})
_SHELL_PYTHON_HEADS = frozenset({"ipython", "python", "python2", "python3"})
_SHELL_IN_PLACE_FLAGS = frozenset({"-i", "-pi", "-ip", "--in-place"})
_SHELL_PREPOSITIONS = frozenset({"of", "in", "on", "from", "for", "to", "named"})
# Options whose value is the interesting part of the command (the pattern searched for, the name
# looked for) rather than something the command acted on. Deliberately short: a flag wrongly listed
# here swallows a real operand, so only flags whose value is never a subject belong.
_SHELL_VALUE_FLAGS = frozenset(
    {
        "--exclude",
        "--exclude-dir",
        "--expression",
        "--file",
        "--include",
        "--max-count",
        "--regexp",
        "-e",
        "-f",
        "-iname",
        "-m",
        "-name",
        "-path",
    }
)
_SHELL_PATTERN_FLAGS = ("-e", "--regexp", "--expression", "-f", "--file")
_SHELL_RUFF_VERBS = {"check": "lint", "format": "format-code"}


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


def _finite_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _finite_float(value: Any) -> float | None:
    """Keep the index strictly JSON-serializable; ``json.loads`` accepts NaN and Infinity."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _is_shell_script(relative_path: str, media_type: str) -> bool:
    media_type = media_type.lower().split(";", 1)[0].strip()
    return (
        media_type in _SHELL_MEDIA_TYPES or Path(relative_path).suffix.lower() in _SHELL_SUFFIXES
    )


def _compress_command_paths(command: str) -> str:
    """Reduce absolute paths to their basename so a command reads as what it acted on.

    ``tail -5 /home/user/sessions/run_.../events.jsonl`` becomes ``tail -5 events.jsonl``, which
    is what makes a generated name recognizable at a glance. Only the path substring is replaced,
    so surrounding syntax such as ``open('...')`` survives. Relative paths and URLs are left
    alone: the lookbehind refuses a match that follows a word character, ``.``, ``:`` or ``/``.
    """

    def replace(match: re.Match[str]) -> str:
        # A trailing separator must not swallow the basename: ``/a/b/c/`` still names ``c``.
        return match.group(1) or match.group(0).rstrip("/").rsplit("/", 1)[-1]

    return _ABSOLUTE_PATH.sub(replace, command)


def _trim_slug(text: str, maxlen: int) -> str:
    """Slugify, and when the budget is exceeded cut at a word boundary.

    A hard character cut leaves a reader guessing ("...-artifact-relat"), which defeats the point
    of a descriptive name; dropping the last whole word does not.
    """

    slug = _slugify(text, maxlen=max(len(text), 1))
    if len(slug) <= maxlen:
        return slug
    cut = slug[:maxlen]
    boundary = cut.rfind("-")
    return (cut[:boundary] if boundary > 0 else cut).rstrip("-")


def _object_slug(operand: str) -> str:
    """Name the thing a command acted on, not the path used to reach it."""

    return _trim_slug(operand.rstrip("/").rsplit("/", 1)[-1] or operand, _SHELL_OBJECT_MAXLEN)


def _tokenize_command(command: str) -> list[str] | None:
    """Split a command into words and shell operators, or return None if it does not lex.

    Unbalanced quotes are common in a generated one-liner and are not worth a partial parse: the
    caller falls back to naming the run after the raw text instead.
    """

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _command_steps(tokens: list[str]) -> list[list[str]]:
    """Cut a token list at every separator, so each step is one thing the command did."""

    steps: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                steps.append(current)
            current = []
            continue
        current.append(token)
    if current:
        steps.append(current)
    return steps


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Drop redirection operators and their targets; they are plumbing, not the function."""

    kept: list[str] = []
    skip_target = False
    for token in tokens:
        if skip_target:
            skip_target = False
            continue
        if token in _SHELL_REDIRECTIONS:
            # ``2>&1`` lexes as ``2``, ``>&``, ``1``: the descriptor is not an operand either.
            if kept and kept[-1].isdigit():
                kept.pop()
            skip_target = True
            continue
        kept.append(token)
    return kept


def _split_operands(tokens: list[str]) -> tuple[str, list[str], list[str], dict[str, str]]:
    """Return the command name, its options, its operands, and the values of its value-taking flags.

    Leading ``VAR=value`` assignments are stripped so the head is the program. A bare number after
    an option is that option's value (``head -n 5 notes.txt``), not something the command acted on.
    """

    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
        index += 1
    if index >= len(tokens):
        return "", [], [], {}
    head = Path(tokens[index]).name.lower()
    options: list[str] = []
    operands: list[str] = []
    values: dict[str, str] = {}
    pending: str | None = None
    for token in tokens[index + 1 :]:
        if token.startswith("-") and token != "-":
            options.append(token)
            pending = token
            continue
        if pending is not None and (token.isdigit() or pending in _SHELL_VALUE_FLAGS):
            values.setdefault(pending, token)
            pending = None
            continue
        pending = None
        operands.append(token)
    return head, options, operands, values


def _first_value(values: dict[str, str], flags: tuple[str, ...]) -> str:
    return next((values[flag] for flag in flags if flag in values), "")


def _describe_step(tokens: list[str]) -> tuple[str, list[str]] | None:
    """Describe one step as a verb plus the words that complete it, or None for shell preamble.

    The verb says what the step accomplished and the tail says on what, so a bundle name reads as
    a sentence ("search-evaluate-py-for-artifact-relative-path") rather than as re-slugified shell.
    A command outside the table keeps its own name as the verb, which stays honest about an
    unfamiliar tool instead of inventing a description for it.
    """

    head, options, operands, values = _split_operands(_strip_redirections(tokens))
    if not head or head in _SHELL_PREAMBLE_HEADS:
        return None
    objects = [slug for slug in (_object_slug(operand) for operand in operands) if slug]

    if head in _SHELL_SEARCH_HEADS:
        # The pattern comes from ``-e``/``-f`` when given and is otherwise the first operand, which
        # also decides whether that operand is one of the files searched.
        flagged = _first_value(values, _SHELL_PATTERN_FLAGS)
        pattern = _trim_slug(flagged or (operands[0] if operands else ""), _SHELL_OBJECT_MAXLEN)
        targets = objects if flagged else objects[1:]
        if not pattern:
            return "search", targets[:1]
        return "search", ([targets[0], "for", pattern] if targets else ["for", pattern])
    if head in _SHELL_STREAM_EDIT_HEADS:
        # Likewise the program text is an operand unless it came from a flag, and it never makes a
        # readable name either way.
        in_place = any(
            option in _SHELL_IN_PLACE_FLAGS or (head == "sed" and option.startswith("-i"))
            for option in options
        )
        verb = "edit" if in_place else _SHELL_VERBS.get(head, f"run-{_slugify(head)}")
        files = objects if _first_value(values, _SHELL_PATTERN_FLAGS) else objects[1:]
        return verb, files[:1]
    if head in _SHELL_PYTHON_HEADS:
        if "-c" in options:
            return "run-inline-python", []
        if "-m" in options:
            # ``python -m pytest tests/x.py`` ran pytest, so the module is the verb and the file
            # it was pointed at is the object.
            module = _slugify(values.get("-m", ""), maxlen=_SHELL_OBJECT_MAXLEN)
            return (f"run-{module}" if module else "run-python-module"), objects[:1]
        return ("run", objects[:1]) if objects else ("start-python", [])
    if head == "find":
        named = _first_value(values, ("-name", "-iname", "-path"))
        if named:
            return "find-files-named", [_trim_slug(named, _SHELL_OBJECT_MAXLEN)]
        return "find-files-in", objects[:1]
    if head == "git":
        return (f"git-{_slugify(operands[0])}" if operands else "git"), objects[1:2]
    if head == "ruff":
        verb = _SHELL_RUFF_VERBS.get(operands[0] if operands else "", "lint")
        return verb, objects[1:2] if operands and operands[0] in _SHELL_RUFF_VERBS else objects[:1]
    if head == "tar":
        short = [option for option in options if not option.startswith("--")]
        extracting = "--extract" in options or any("x" in option for option in short)
        return ("extract-archive" if extracting else "create-archive"), objects[:1]
    if head == "curl":
        # A URL is the subject of a fetch, so it is kept whole rather than reduced to a basename.
        return "fetch", [_trim_slug(operands[0], _SHELL_OBJECT_MAXLEN)] if operands else []
    known = _SHELL_VERBS.get(head)
    if known:
        return known, objects[:1]
    # An unfamiliar tool keeps its own name, and a first operand that is not a path is almost
    # always its subcommand -- ``scagent-sdk capability validate`` needs both words to mean
    # anything, while ``gnuplot plot.gp`` is complete after one.
    verb = _slugify(head, maxlen=_SHELL_OBJECT_MAXLEN) or "run"
    subcommand = bool(operands) and not set("./").intersection(operands[0])
    return verb, objects[:2] if subcommand else objects[:1]


def _verb_without_object(verb: str) -> str:
    """``count-lines-in`` with nothing after it reads as a fragment; ``count-lines`` does not."""

    parts = verb.split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 and parts[-1] in _SHELL_PREPOSITIONS else verb


def _describe_command(command: str) -> str:
    """Name a command by what it did: the first real step, then a differing final step.

    A run is usually one function reached through some plumbing -- ``cd`` into a directory, then
    grep a file, then pipe the result through ``wc``. Naming the first working step captures the
    subject and appending the last step captures the outcome, which together describe the run
    without replaying every clause of it.
    """

    tokens = _tokenize_command(command)
    if tokens is None:
        return ""
    steps = [
        description
        for description in (_describe_step(step) for step in _command_steps(tokens))
        if description is not None
    ]
    if not steps:
        return ""
    verb, tail = steps[0]
    parts = [verb, *tail] if tail else [_verb_without_object(verb)]
    if len(steps) > 1:
        last_verb, last_tail = steps[-1]
        subject_changed = last_tail[:1] != tail[:1]
        if last_verb != verb or subject_changed:
            follow = (
                [last_verb, *last_tail[:1]]
                if last_tail and subject_changed
                else [_verb_without_object(last_verb)]
            )
            parts += ["then", *follow]
    return _trim_slug("-".join(part for part in parts if part), _SHELL_LABEL_MAXLEN)


def _shell_label(command: str, fallback: str) -> str:
    """Name a shell run after the function it performed, falling back to the text that ran.

    Only the command is described as a function; the summary is a sentence a skill wrote, and
    reading it as shell would name a run after its grammar rather than its work. The summary does
    drop a leading clause at ``:``, because it carries boilerplate that would otherwise consume the
    whole label budget ("Shell command completed in 2.83s: ls").
    """

    collapsed = " ".join(command.split())
    if collapsed:
        label = _describe_command(collapsed) or _trim_slug(
            _compress_command_paths(collapsed), _SHELL_LABEL_MAXLEN
        )
        if label:
            return label
    text = fallback.split(":", 1)[1] if ":" in fallback else fallback
    collapsed = " ".join(text.split())
    return (
        _trim_slug(_compress_command_paths(collapsed), _SHELL_LABEL_MAXLEN)
        or _trim_slug(collapsed, _SHELL_LABEL_MAXLEN)
        or "command"
    )


def _shell_member(artifact_name: str, relative_path: str, media_type: str) -> str | None:
    """Name one file inside a shell bundle, or return None to classify it normally.

    The bundle directory already says the record is a shell run, so members drop the redundant
    ``shell`` prefix and become ``command.sh``, ``stdout.txt``, ``stderr.txt``, ``run.json``.
    Files that are not part of the run itself -- a figure a command happened to write -- fall
    through so they still appear in their own category.
    """

    normalized = media_type.lower().split(";", 1)[0].strip()
    suffix = Path(relative_path).suffix.lower()
    is_member = (
        _is_shell_script(relative_path, media_type)
        or normalized == "text/plain"
        or suffix == ".txt"
        or normalized in _JSON_MEDIA_TYPES
        or suffix == ".json"
    )
    if not is_member:
        return None
    stem = _slugify(artifact_name) or _slugify(Path(relative_path).stem)
    for prefix in ("shell-", "shell"):
        if stem.startswith(prefix) and len(stem) > len(prefix):
            stem = stem[len(prefix) :].strip("-")
            break
    return f"{stem or 'output'}{suffix}"


def _shell_member_rank(view_path: str) -> int:
    stem = Path(view_path).stem.lower()
    try:
        return _SHELL_MEMBER_ORDER.index(stem)
    except ValueError:
        return len(_SHELL_MEMBER_ORDER)


def _read_shell_report(
    session_dir: Path, artifact_path: str, files: list[Any]
) -> dict[str, Any] | None:
    """Load the run report a shell execution commits beside its script.

    The command text lives only in this file, never in the artifact record, so the projection
    reads it to build a descriptive name and a browsable command table.
    """

    for raw_file in files:
        if not isinstance(raw_file, Mapping):
            continue
        relative_path = raw_file.get("relative_path")
        if not isinstance(relative_path, str):
            continue
        media_type = str(raw_file.get("media_type", "")).lower().split(";", 1)[0].strip()
        if media_type not in _JSON_MEDIA_TYPES and Path(relative_path).suffix.lower() != ".json":
            continue
        source = _safe_source(session_dir, artifact_path, relative_path)
        if source is None:
            continue
        try:
            if source.stat().st_size > _SHELL_REPORT_MAX_BYTES:
                continue
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("command"), str):
            return value
    return None


def _classify(relative_path: str, media_type: str) -> str | None:
    suffix = Path(relative_path).suffix.lower()
    media_type = media_type.lower().split(";", 1)[0].strip()
    name = Path(relative_path).name.lower()
    if media_type in _NOTEBOOK_MEDIA_TYPES or suffix == ".ipynb":
        return "reports"
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
    summary: str,
    artifact_name: str,
    artifact_title: str = "",
    relative_path: str,
    disambiguate: bool = False,
) -> str:
    suffix = Path(relative_path).suffix.lower()
    short_id = _slugify(execution_id, maxlen=8) or "unknown"
    if category == "code":
        # Code is named for what it does rather than for the artifact, which reads better in a
        # session listing. That label comes from the record's summary, so it is identical for
        # every code file in one execution; add the artifact name when there is more than one,
        # or they collide and fall through to the --view-N guard meant for cross-run reuse.
        label = _code_label(summary, artifact_name)
        artifact = _slugify(artifact_name)
        if disambiguate and artifact and artifact != label:
            return f"{label}-{artifact}-{short_id}{suffix or '.py'}"
        return f"{label}-{short_id}{suffix or '.py'}"
    # The title says what this file shows and the name only says what kind of file it is, so the
    # title wins when a skill supplied one. The short id makes the projection unique across passes
    # and traceable to its execution. The producing tool is deliberately absent -- it is recorded in
    # state and in outputs.md, and repeating it here only crowds out the descriptive part.
    artifact = (
        _slugify(artifact_title)
        or _slugify(artifact_name)
        or _slugify(Path(relative_path).stem)
        or "output"
    )
    return f"{artifact}--{short_id}{suffix}"


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
            _prune_empty_group(session_dir, target.parent)


def _prune_empty_group(session_dir: Path, directory: Path) -> None:
    """Drop grouping directories a removed link leaves behind, never a category root.

    Shell bundles and nested figure groups are generated names; once empty they are noise. The
    category roots and ``data/intermediates`` are part of the promised layout and always stay.
    """

    while directory != session_dir:
        try:
            relative = directory.relative_to(session_dir)
        except ValueError:
            return
        if len(relative.parts) < 2 or str(relative) in _PROTECTED_DIRECTORIES:
            return
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _markdown_link(path: str) -> str:
    return quote(path, safe="/-._~")


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _command_cell(command: str) -> str:
    # Session paths are long enough to push the distinctive part of a command past the truncation
    # limit, so the table shows the same basename form the bundle name uses.
    collapsed = _compress_command_paths(" ".join(command.split()))
    if len(collapsed) > _SHELL_COMMAND_CELL_MAXLEN:
        collapsed = collapsed[: _SHELL_COMMAND_CELL_MAXLEN - 1].rstrip() + "…"
    # Backticks would end the code span; the exact text stays one click away in command.sh.
    return _markdown_cell(collapsed).replace("`", "'") or "(empty)"


def _render_shell_section(entries: list[Mapping[str, Any]]) -> list[str]:
    """Render one row per shell run rather than per file, since a run is the unit of interest."""

    bundles: dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        bundle = str(entry.get("bundle_path") or Path(str(entry["view_path"])).parent)
        bundles.setdefault(bundle, []).append(entry)
    lines = [
        "_Commands are abbreviated to basenames; `command.sh` holds the exact text that ran._",
        "",
        "| Run | Command | Duration | Files |",
        "|---|---|---|---|",
    ]
    for bundle, members in bundles.items():
        first = members[0]
        duration = first.get("duration_seconds")
        elapsed = f"{float(duration):.2f}s" if isinstance(duration, (int, float)) else "—"
        files = " · ".join(
            f"[{Path(str(member['view_path'])).name}]({_markdown_link(str(member['view_path']))})"
            for member in members
        )
        lines.append(
            f"| [{Path(bundle).name}]({_markdown_link(bundle)}) | "
            f"`{_command_cell(str(first.get('command', '')))}` | {elapsed} | {files} |"
        )
    lines.append("")
    return lines


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
        "shell": "Shell commands",
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
        if category == "shell":
            lines.extend(_render_shell_section(entries))
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
    shell_ordinal = 0
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
        code_files = sum(
            1
            for raw_file in files
            if isinstance(raw_file, Mapping)
            and raw_file.get("include_in_output_view") is not False
            and isinstance(raw_file.get("relative_path"), str)
            and _classify(
                str(raw_file["relative_path"]),
                str(raw_file.get("media_type", "application/octet-stream")),
            )
            == "code"
        )
        is_shell_run = any(
            isinstance(raw_file, Mapping)
            and raw_file.get("include_in_output_view") is not False
            and isinstance(raw_file.get("relative_path"), str)
            and _is_shell_script(
                str(raw_file["relative_path"]),
                str(raw_file.get("media_type", "application/octet-stream")),
            )
            for raw_file in files
        )
        shell_report: dict[str, Any] | None = None
        shell_bundle: str | None = None
        if is_shell_run:
            shell_ordinal += 1
            shell_report = _read_shell_report(session_dir, artifact_path, files)
            command = str(shell_report.get("command", "")) if shell_report else ""
            label = _shell_label(command, summary or tool_name)
            short_id = _slugify(execution_id, maxlen=8) or "unknown"
            # An ordinal prefix keeps a shell listing in the order the commands actually ran,
            # which a name-sorted directory otherwise destroys.
            shell_bundle = f"shell/{shell_ordinal:03d}-{label}-{short_id}"
        for raw_file in files:
            if not isinstance(raw_file, Mapping):
                continue
            if raw_file.get("include_in_output_view") is False:
                continue
            relative_path = raw_file.get("relative_path")
            if not isinstance(relative_path, str):
                continue
            media_type = str(raw_file.get("media_type", "application/octet-stream"))
            artifact_name = str(raw_file.get("name", Path(relative_path).stem))
            artifact_title = str(raw_file.get("title", ""))
            shell_member = (
                _shell_member(artifact_name, relative_path, media_type)
                if shell_bundle is not None
                else None
            )
            category = "shell" if shell_member is not None else _classify(relative_path, media_type)
            if category is None:
                continue
            source = _safe_source(session_dir, artifact_path, relative_path)
            if source is None:
                continue
            if shell_member is not None and shell_bundle is not None:
                filename = shell_member
                directory = session_dir / shell_bundle
                final_data = False
            else:
                filename = _projected_name(
                    category=category,
                    execution_id=execution_id,
                    summary=summary,
                    artifact_name=artifact_name,
                    artifact_title=artifact_title,
                    relative_path=relative_path,
                    disambiguate=code_files > 1,
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
            if artifact_title:
                entry["artifact_title"] = artifact_title
            if category == "shell" and shell_bundle is not None:
                report = shell_report or {}
                entry["bundle_path"] = shell_bundle
                entry["command"] = str(report.get("command", ""))
                entry["cwd"] = str(report.get("cwd", ""))
                entry["exit_code"] = _finite_int(report.get("exit_code"))
                entry["duration_seconds"] = _finite_float(report.get("duration_seconds"))
            entries.append({"category": category, **entry})

            normalized = f"{artifact_name} {relative_path}".lower().replace("_", "-")
            alias_path: str | None = None
            if category == "data" and final_data:
                alias_path = f"data/final-annotated{Path(relative_path).suffix.lower()}"
            elif category == "reports" and "analysis-notebook" in normalized:
                alias_path = f"reports/analysis-notebook{Path(relative_path).suffix.lower()}"
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
    for category, category_entries in categories.items():
        if category == "shell":
            category_entries.sort(
                key=lambda entry: (
                    entry["commit_sequence"],
                    _natural_key(str(entry.get("bundle_path", ""))),
                    _shell_member_rank(entry["view_path"]),
                    entry["view_path"],
                )
            )
            continue
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
