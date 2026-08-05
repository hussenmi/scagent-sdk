"""On-request package installs into a per-user, per-runtime overlay.

This entrypoint runs *inside* the analysis runtime (routed through the ``overlay-admin``
capability), so ``sys.executable`` is that runtime's interpreter and ``pip`` resolves against the
real base. The broker exports the overlay directory to write into as ``SCAGENT_OVERLAY_DIR`` and
layers it on ``PYTHONPATH``; this code never searches the filesystem to discover it.

The safety model is a dependency dry-run (``pip install --dry-run --report``): only a package whose
resolved closure adds *new* distributions — none that would change a version the base already has —
may enter the overlay. Such a package cannot shadow the base regardless of path order. A conflicting
package is refused. Installs are gated a second time by explicit user approval, verified against the
session event log rather than trusted from the model.

Pure helpers here are unit-tested directly; only the thin ``_pip_*`` wrappers shell out. The module
is standard-library only so it runs in any runtime and is testable in the control plane.

See ``docs/decisions/0010-on-request-package-installs.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "installed.json"


# --------------------------------------------------------------------------- pure helpers


def canonical(name: str) -> str:
    """PEP 503 normalized distribution name for comparison."""

    return re.sub(r"[-_.]+", "-", str(name)).strip().lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def question_identity(package: str, runtime: str) -> str:
    """Stable id for the question "may I add this package to this runtime's overlay?"."""

    payload = json.dumps(
        {"package": canonical(package), "runtime": str(runtime)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "install-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def latest_user_turn_sequence(user_turns: Any) -> int:
    """Event sequence of the newest user turn, or 0 when none are recorded.

    Stored on a proposal as its consultation anchor so approval must come from a turn *after* the
    proposal, never from the request that triggered it.
    """

    best = 0
    for turn in user_turns or ():
        if not isinstance(turn, dict):
            continue
        sequence = turn.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > best:
            best = sequence
    return best


def classify_report(report: Any, inventory: dict[str, str]) -> dict[str, Any]:
    """Classify a pip ``--report`` resolution against the installed inventory.

    ``inventory`` maps canonical name to installed version. Every entry pip intends to install is
    either a brand-new distribution (additive) or a version change to something already installed
    (a conflict that would alter the base). Same-version entries are neither.
    """

    installs = report.get("install", []) if isinstance(report, dict) else []
    additive: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    for entry in installs:
        metadata = entry.get("metadata", {}) if isinstance(entry, dict) else {}
        name = metadata.get("name")
        version = metadata.get("version")
        if not name or version is None:
            continue
        key = canonical(name)
        if key in inventory:
            if str(inventory[key]) != str(version):
                conflicts.append(
                    {
                        "name": str(name),
                        "base_version": str(inventory[key]),
                        "proposed_version": str(version),
                    }
                )
            continue
        additive.append({"name": str(name), "version": str(version)})
    if conflicts:
        status = "conflict"
    elif additive:
        status = "additive"
    else:
        status = "already_satisfied"
    return {"status": status, "additive": additive, "conflicts": conflicts}


def parse_uv_plan(output: str) -> dict[str, Any]:
    """Classify a ``uv pip install --dry-run`` plan (printed to stderr).

    uv is environment-aware against the conda base, so its plan already excludes satisfied
    dependencies. A ``+ name==ver`` line with no matching ``- name==ver`` is a purely additive
    package; a ``- name==ver`` (a base distribution uv would uninstall) paired with a ``+`` is a
    version change to the base — a conflict. This maps onto the same buckets as
    :func:`classify_report` without a separate inventory call.
    """

    additions: dict[str, tuple[str, str]] = {}
    removals: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("+ ") or stripped.startswith("- "):
            spec = stripped[2:].strip().split()[0]  # drop any " (from ...)" provenance suffix
            if "==" not in spec:
                continue
            name, version = spec.split("==", 1)
            target = additions if stripped.startswith("+ ") else removals
            target[canonical(name)] = (name, version)
    additive: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    for key, (name, version) in additions.items():
        if key in removals:
            conflicts.append(
                {
                    "name": name,
                    "base_version": removals[key][1],
                    "proposed_version": version,
                }
            )
        else:
            additive.append({"name": name, "version": version})
    if conflicts:
        status = "conflict"
    elif additive:
        status = "additive"
    else:
        status = "already_satisfied"
    return {"status": status, "additive": additive, "conflicts": conflicts}


def validate_consultation(
    question_asked: Any, *, anchor_sequence: int, user_turns: Any
) -> dict[str, Any]:
    """Resolve the user's approving turn: the first real turn after the proposal.

    The model supplies only the question it asked; the runtime resolves the turn id, sequence, and
    verbatim answer from the event log. A turn that predates the proposal cannot approve it, which
    is what stops the original request from being replayed as authorization.
    """

    if not isinstance(question_asked, str) or not question_asked.strip():
        return {
            "ok": False,
            "violation": (
                "install requires question_asked and a later user turn approving it; "
                "ask the user and wait for their reply before installing"
            ),
            "consultation": None,
        }
    candidates: list[dict[str, Any]] = []
    for turn in user_turns or ():
        if not isinstance(turn, dict):
            continue
        sequence = turn.get("sequence")
        turn_id = turn.get("turn_id")
        answer = turn.get("user_prompt")
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > anchor_sequence
            and isinstance(turn_id, str)
            and turn_id.strip()
            and isinstance(answer, str)
            and answer.strip()
        ):
            candidates.append(turn)
    if not candidates:
        return {
            "ok": False,
            "violation": (
                "no user turn followed the proposal; ask the user to approve the install and "
                "wait for their reply before calling install_proposed_package"
            ),
            "consultation": None,
        }
    first = min(candidates, key=lambda turn: int(turn["sequence"]))
    return {
        "ok": True,
        "violation": None,
        "consultation": {
            "turn_id": str(first["turn_id"]),
            "sequence": int(first["sequence"]),
            "answer": str(first["user_prompt"]),
            "question_asked": question_asked.strip(),
            "turns_after_proposal": len(candidates),
        },
    }


def merge_installed(
    existing: Any, record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Insert or replace ``record`` in the installed list, keyed by canonical name, sorted."""

    key = canonical(record["name"])
    kept = [
        item
        for item in (existing or [])
        if isinstance(item, dict) and canonical(item.get("name", "")) != key
    ]
    kept.append(record)
    return sorted(kept, key=lambda item: canonical(item.get("name", "")))


def overlay_fact_patch(
    state_facts: Any,
    runtime: str,
    *,
    installed: list[dict[str, Any]] | None = None,
    proposal: tuple[str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build the ``environment_overlay`` merge patch.

    The installed list is written whole (RFC 7396 replaces arrays), keyed under the runtime so
    other runtimes' overlays are untouched. A proposal entry is added, or cleared with ``None``.
    """

    overlay: dict[str, Any] = {}
    if installed is not None:
        overlay["runtimes"] = {runtime: {"installed": installed}}
    if proposal is not None:
        question_id, value = proposal
        overlay["proposals"] = {question_id: value}
    return {"environment_overlay": overlay}


def existing_installed(state_facts: Any, runtime: str) -> list[dict[str, Any]]:
    overlay = state_facts.get("environment_overlay") if isinstance(state_facts, dict) else None
    runtimes = overlay.get("runtimes") if isinstance(overlay, dict) else None
    entry = runtimes.get(runtime) if isinstance(runtimes, dict) else None
    installed = entry.get("installed") if isinstance(entry, dict) else None
    return [item for item in installed] if isinstance(installed, list) else []


def existing_proposal(state_facts: Any, question_id: str) -> dict[str, Any] | None:
    overlay = state_facts.get("environment_overlay") if isinstance(state_facts, dict) else None
    proposals = overlay.get("proposals") if isinstance(overlay, dict) else None
    value = proposals.get(question_id) if isinstance(proposals, dict) else None
    return dict(value) if isinstance(value, dict) else None


# --------------------------------------------------------------------------- environment


def _overlay_dir() -> Path:
    raw = os.environ.get("SCAGENT_OVERLAY_DIR")
    if not raw:
        raise RuntimeError(
            "SCAGENT_OVERLAY_DIR is not set; this tool must run through the overlay-admin "
            "capability so the broker can supply the per-user overlay directory"
        )
    return Path(raw)


def _runtime_label(overlay_dir: Path) -> str:
    # Overlay path is <storage>/<runtime>/<base_key>; the runtime is the parent directory name.
    return overlay_dir.parent.name or "runtime"


# --------------------------------------------------------------------------- pip wrappers


def _pip(
    args: list[str], *, cwd: Path | None = None, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "pip", "--disable-pip-version-check", "--no-input", *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


def _pip_inventory() -> dict[str, str]:
    completed = _pip(["list", "--format=json"], timeout=300)
    if completed.returncode != 0:
        raise RuntimeError(f"pip list failed: {completed.stderr.strip()[-2000:]}")
    data = json.loads(completed.stdout or "[]")
    return {canonical(item["name"]): str(item["version"]) for item in data if item.get("name")}


def _pip_report(package: str, report_path: Path) -> tuple[str, dict[str, Any]]:
    """Resolve ``package`` without installing. Returns ``(status, report)``.

    ``status`` is ``"ok"`` when pip resolved a plan, or ``"unavailable"`` when no distribution
    could be found (the common signal for a conda-only or misspelled package).
    """

    # No --ignore-installed: pip must stay environment-aware so its "install" list is exactly the
    # set it would add or change relative to the base. --ignore-installed would re-resolve the
    # whole closure (every base dep) from PyPI, which is both wrong for the gate and pathologically
    # slow on a bleeding-edge Python where base deps lack wheels and would be prepared from sdists.
    completed = _pip(
        ["install", package, "--dry-run", "--report", str(report_path), "--quiet"],
        timeout=600,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if "No matching distribution" in stderr or "Could not find a version" in stderr:
            return "unavailable", {}
        raise RuntimeError(f"dependency resolution failed: {stderr[-2000:]}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return "ok", report


def _pip_install_additive(overlay_dir: Path, specs: list[str]) -> None:
    overlay_dir.mkdir(parents=True, exist_ok=True)
    completed = _pip(
        ["install", "--target", str(overlay_dir), "--no-deps", "--upgrade", *specs],
        timeout=900,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"overlay install failed: {completed.stderr.strip()[-2000:]}")


def _report_specs(additive: list[dict[str, str]]) -> list[str]:
    return [f"{item['name']}=={item['version']}" for item in additive]


# --------------------------------------------------------------------------- uv wrappers

# uv is pixi's own PyPI resolver, so it is the native tool for this env: faster than pip, fully
# aware of conda-installed distributions (it even tracks their conda-forge origin), and its plan
# maps directly onto the additive/conflict buckets. We prefer it when available and fall back to
# pip's --report otherwise, so a runtime without a reachable uv still works.


def _uv_binary() -> str | None:
    candidate = os.environ.get("SCAGENT_SDK_UV") or shutil.which("uv")
    if candidate and os.access(candidate, os.X_OK):
        return candidate
    return None


def _uv_resolve(package: str, uv_bin: str) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [uv_bin, "pip", "install", "--python", sys.executable, package, "--dry-run"],
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    output = (completed.stderr or "") + "\n" + (completed.stdout or "")
    if completed.returncode != 0:
        # Not found on PyPI, or no satisfiable version — either way it cannot be added additively.
        return {"status": "unavailable", "additive": [], "conflicts": [], "resolver": "uv"}
    classification = parse_uv_plan(output)
    classification["resolver"] = "uv"
    return classification


def _uv_install(overlay_dir: Path, specs: list[str], uv_bin: str) -> None:
    overlay_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            uv_bin,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(overlay_dir),
            "--no-deps",
            *specs,
        ],
        text=True,
        capture_output=True,
        timeout=900,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"overlay install failed (uv): {completed.stderr.strip()[-2000:]}")


# --------------------------------------------------------------------------- resolver dispatch


def resolve_package(package: str, *, staging_dir: Path) -> dict[str, Any]:
    """Classify a package against the runtime, preferring uv and falling back to pip.

    Returns ``{status, additive, conflicts, resolver}`` where ``status`` is one of ``additive``,
    ``conflict``, ``already_satisfied`` or ``unavailable``.
    """

    uv_bin = _uv_binary()
    if uv_bin is not None:
        return _uv_resolve(package, uv_bin)
    report_path = Path(staging_dir) / "resolution-report.json"
    status, report = _pip_report(package, report_path)
    if status == "unavailable":
        return {"status": "unavailable", "additive": [], "conflicts": [], "resolver": "pip"}
    classification = classify_report(report, _pip_inventory())
    classification["resolver"] = "pip"
    return classification


def materialize(overlay_dir: Path, additive: list[dict[str, str]]) -> str:
    """Install exactly the additive pins into the overlay. Returns which resolver was used."""

    specs = _report_specs(additive)
    uv_bin = _uv_binary()
    if uv_bin is not None:
        _uv_install(overlay_dir, specs, uv_bin)
        return "uv"
    _pip_install_additive(overlay_dir, specs)
    return "pip"


# --------------------------------------------------------------------------- manifest file


def _write_manifest(overlay_dir: Path, installed: list[dict[str, Any]]) -> None:
    overlay_dir.mkdir(parents=True, exist_ok=True)
    (overlay_dir / MANIFEST_NAME).write_text(
        json.dumps({"installed": installed}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_manifest(overlay_dir: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads((overlay_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    installed = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(installed, list):
        return []
    return [item for item in installed if isinstance(item, dict)]


def remaining_records(manifest: list[dict[str, Any]], package: str) -> list[dict[str, Any]]:
    """The manifest with ``package`` dropped (canonical match)."""

    key = canonical(package)
    return [item for item in manifest if canonical(item.get("name", "")) != key]


def specs_from_records(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flatten the pinned distributions a set of install records added, for a clean rebuild."""

    specs: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        dists = record.get("added_distributions")
        entries = dists if isinstance(dists, list) and dists else [record]
        for entry in entries:
            name = entry.get("name")
            version = entry.get("version")
            if not name or version is None:
                continue
            key = canonical(name)
            if key in seen:
                continue
            seen.add(key)
            specs.append({"name": str(name), "version": str(version)})
    return specs


def _wipe_overlay(overlay_dir: Path) -> None:
    """Empty the overlay directory (but keep the directory itself)."""

    if not overlay_dir.exists():
        return
    for child in overlay_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


# --------------------------------------------------------------------------- entrypoints


def propose_package_install(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    package = str(arguments["package"]).strip()
    reason = str(arguments["reason"]).strip()
    overlay_dir = _overlay_dir()
    runtime = _runtime_label(overlay_dir)
    question_id = question_identity(package, runtime)

    classification = resolve_package(package, staging_dir=Path(context.staging_dir))

    anchor = latest_user_turn_sequence(getattr(context, "user_turns", ()))
    proposal = {
        "package": package,
        "runtime": runtime,
        "reason": reason,
        "classification": classification,
        "consultation_anchor_sequence": anchor,
        "proposed_at": now_iso(),
    }
    facts_patch = overlay_fact_patch(
        getattr(context, "state_facts", {}),
        runtime,
        proposal=(question_id, proposal),
    )

    summaries = {
        "additive": (
            f"{package} is additive and safe to add to the overlay; ask the user to approve."
        ),
        "conflict": f"{package} would change a base package and cannot be added to the overlay.",
        "already_satisfied": f"{package} is already available; no install needed.",
        "unavailable": (
            f"{package} was not found on PyPI; it likely needs an operator to add it to the base."
        ),
    }
    return {
        "summary": summaries[classification["status"]],
        "details": {
            "install_request_id": question_id,
            "package": package,
            "runtime": runtime,
            "reason": reason,
            "status": classification["status"],
            "resolver": classification.get("resolver"),
            "would_add": classification["additive"],
            "conflicts": classification["conflicts"],
            "next_step": (
                "Ask the user to approve, then call install_proposed_package with this id."
                if classification["status"] == "additive"
                else "No install is possible; report the status to the user."
            ),
        },
        "facts_patch": facts_patch,
    }


def install_proposed_package(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = str(arguments["install_request_id"]).strip()
    question_asked = str(arguments["question_asked"])
    overlay_dir = _overlay_dir()
    runtime = _runtime_label(overlay_dir)

    proposal = existing_proposal(getattr(context, "state_facts", {}), request_id)
    if proposal is None:
        raise RuntimeError(
            f"no package proposal {request_id!r} is on record; call propose_package_install first"
        )
    package = str(proposal.get("package", "")).strip()
    anchor = int(proposal.get("consultation_anchor_sequence", 0) or 0)

    consultation = validate_consultation(
        question_asked, anchor_sequence=anchor, user_turns=getattr(context, "user_turns", ())
    )
    if not consultation["ok"]:
        raise RuntimeError(consultation["violation"])

    # Re-resolve at install time: the base or overlay may have changed since the proposal.
    classification = resolve_package(package, staging_dir=Path(context.staging_dir))
    if classification["status"] == "unavailable":
        raise RuntimeError(f"{package} is no longer resolvable on PyPI")
    if classification["status"] == "already_satisfied":
        return {
            "summary": f"{package} is already available; nothing to install.",
            "details": {"package": package, "status": "already_satisfied"},
        }
    if classification["status"] != "additive":
        raise RuntimeError(
            f"{package} is no longer additive ({classification['status']}); "
            f"conflicts: {classification['conflicts']}"
        )

    resolver = materialize(overlay_dir, classification["additive"])

    record = {
        "name": package,
        "version": _primary_version(package, classification["additive"]),
        "reason": str(proposal.get("reason", "")),
        "installed_at": now_iso(),
        "approved_by_turn": consultation["consultation"]["turn_id"],
        "added_distributions": classification["additive"],
        "resolver": resolver,
    }
    prior = existing_installed(getattr(context, "state_facts", {}), runtime)
    installed = merge_installed(prior, record)
    _write_manifest(overlay_dir, installed)

    facts_patch = overlay_fact_patch(
        getattr(context, "state_facts", {}),
        runtime,
        installed=installed,
        proposal=(request_id, None),  # clear the consumed proposal
    )
    return {
        "summary": (
            f"Added {package} to the overlay for {runtime}; it is now importable by analysis code."
        ),
        "details": {
            "package": package,
            "runtime": runtime,
            "added_distributions": classification["additive"],
            "resolver": resolver,
            "user_approval": consultation["consultation"],
            "overlay_directory": str(overlay_dir),
        },
        "facts_patch": facts_patch,
    }


def _primary_version(package: str, additive: list[dict[str, str]]) -> str:
    key = canonical(re.split(r"[<>=!~]", package, maxsplit=1)[0])
    for item in additive:
        if canonical(item["name"]) == key:
            return item["version"]
    return additive[0]["version"] if additive else ""


def list_installed_packages(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    overlay_dir = _overlay_dir()
    runtime = _runtime_label(overlay_dir)
    installed = _read_manifest(overlay_dir)
    if not installed:
        summary = f"No overlay packages are installed for {runtime}."
    else:
        names = ", ".join(f"{item.get('name')}=={item.get('version')}" for item in installed)
        summary = f"{len(installed)} overlay package(s) for {runtime}: {names}."
    return {
        "summary": summary,
        "details": {
            "runtime": runtime,
            "installed": installed,
            "overlay_directory": str(overlay_dir),
        },
    }


def remove_installed_package(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    package = str(arguments["package"]).strip()
    overlay_dir = _overlay_dir()
    runtime = _runtime_label(overlay_dir)
    key = canonical(package)

    existing = existing_installed(getattr(context, "state_facts", {}), runtime)
    manifest = _read_manifest(overlay_dir)
    known = {canonical(item.get("name", "")) for item in existing} | {
        canonical(item.get("name", "")) for item in manifest
    }
    if key not in known:
        return {
            "summary": f"{package} is not an overlay package for {runtime}; nothing to remove.",
            "details": {"runtime": runtime, "package": package, "removed": False},
        }

    # Rebuild the overlay from the survivors rather than deleting one package's files in place.
    # A ``--target`` layout has no uninstaller, and RECORD-based deletion cannot remove the
    # __pycache__ an import leaves behind, so a directory package would survive as an importable
    # namespace remnant. Wipe and reinstall the remaining pins is deterministic and, with uv, fast.
    remaining = remaining_records(manifest, package)
    _wipe_overlay(overlay_dir)
    specs = specs_from_records(remaining)
    if specs:
        materialize(overlay_dir, specs)
    _write_manifest(overlay_dir, remaining)
    fact_remaining = [item for item in existing if canonical(item.get("name", "")) != key]
    facts_patch = overlay_fact_patch(
        getattr(context, "state_facts", {}), runtime, installed=fact_remaining
    )
    return {
        "summary": f"Removed {package} from the overlay for {runtime}.",
        "details": {
            "runtime": runtime,
            "package": package,
            "removed": True,
            "remaining": remaining,
        },
        "facts_patch": facts_patch,
    }
