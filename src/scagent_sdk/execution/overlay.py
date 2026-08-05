"""Per-user, per-runtime package overlays layered on immutable base runtimes.

On-request installs must never mutate a shared locked runtime: in deployment the runtime
prefix is read-only, and its ``pixi.lock`` hash is fingerprinted into every result, so a
``pip install`` into it would silently invalidate provenance for every downstream step.
Instead each install lands in a per-``(user, runtime, base)`` directory that the broker
prepends to a worker's ``PYTHONPATH``. An added package becomes importable by the runtime
that needs it without the base ever changing, and the directory persists across sessions so
a user installs once. Only additive (non-overlapping) packages are ever placed here, so the
overlay can never shadow a base distribution regardless of path order.

The directory is *declared* to the install tool through the :data:`OVERLAY_DIR_ENV`
environment variable the broker sets, never discovered by the tool searching the filesystem.

See ``docs/decisions/0010-on-request-package-installs.md``.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

# The broker exports the resolved overlay directory to every runtime-routed worker under this
# name. The install tool writes there; other capabilities merely inherit it on their path.
OVERLAY_DIR_ENV = "SCAGENT_OVERLAY_DIR"
# Explicit override for the storage root, mirroring SCAGENT_SDK_COMPUTE_STORAGE for compute envs.
STORAGE_ENV = "SCAGENT_SDK_OVERLAY_STORAGE"
# Written inside an overlay directory by the install tool; read best-effort by the broker so a
# result's provenance can record exactly which added packages were on its path.
MANIFEST_NAME = "installed.json"


def overlay_storage_base() -> Path:
    """Root under which per-runtime overlays live.

    Honours an explicit override, then the per-user Iris software area this project already uses
    for compute storage, then a home-directory cache so the mechanism also works off-cluster and
    in tests.
    """

    override = os.environ.get(STORAGE_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or _current_user()
    iris_user_area = Path(f"/usersoftware/peerd/{user}")
    if iris_user_area.is_dir():
        return iris_user_area / "scagent-overlay"
    return Path.home() / ".cache" / "scagent-sdk" / "overlay"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser only fails without a resolvable account
        return "unknown"


def base_key(provenance_files: tuple[Path, ...] | list[Any]) -> str:
    """Short stable key identifying the base a runtime resolves against.

    Derived from the content of the runtime's provenance files (``pixi.toml`` + ``pixi.lock``),
    not the interpreter's mtime, so re-stating an unchanged interpreter keeps the same overlay,
    while a genuine lock change starts a fresh overlay generation rather than mixing packages
    resolved against different bases.
    """

    digest = hashlib.sha256()
    used_any = False
    for raw in provenance_files:
        try:
            digest.update(Path(raw).read_bytes())
            used_any = True
        except OSError:
            continue
    if not used_any:
        return "base"
    return digest.hexdigest()[:16]


def overlay_directory(runtime_name: str, key: str, *, storage_base: Path | None = None) -> Path:
    """Absolute overlay directory for one ``(runtime, base)`` under a user's storage root."""

    root = storage_base if storage_base is not None else overlay_storage_base()
    return root / runtime_name / key


def overlay_directory_for_runtime(runtime: Any, *, storage_base: Path | None = None) -> Path:
    """Resolve the overlay directory for an :class:`ExecutionRuntime`-shaped object."""

    provenance = getattr(runtime, "provenance_files", ()) or ()
    return overlay_directory(
        str(runtime.name), base_key(provenance), storage_base=storage_base
    )


def installed_packages(overlay_dir: Path) -> list[dict[str, Any]]:
    """Read the overlay's install manifest, tolerating a missing or malformed file."""

    try:
        raw = json.loads((overlay_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    packages = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(packages, list):
        return []
    return [item for item in packages if isinstance(item, dict)]


def prepend_pythonpath(environment: dict[str, str], overlay_dir: Path) -> None:
    """Prepend ``overlay_dir`` to ``PYTHONPATH`` in place, preserving any existing entries.

    ``ExecutionRuntime.build_environment`` deliberately clears ``PYTHONPATH`` for isolation, so
    this normally sets a single entry; the join is defensive for a capability that set one.
    """

    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(overlay_dir)
        if not existing
        else f"{overlay_dir}{os.pathsep}{existing}"
    )


def apply_to_worker_environment(environment: dict[str, str], runtime: Any) -> dict[str, Any]:
    """Declare the overlay to a runtime-routed worker and report what was applied.

    Always exports :data:`OVERLAY_DIR_ENV` so the install tool knows where to write. Only puts
    the directory on ``PYTHONPATH`` when it already holds packages, so an empty overlay never
    perturbs a clean base. Returns a provenance record of the overlay state that was active.
    """

    overlay_dir = overlay_directory_for_runtime(runtime)
    environment[OVERLAY_DIR_ENV] = str(overlay_dir)
    packages = installed_packages(overlay_dir)
    if overlay_dir.is_dir() and packages:
        prepend_pythonpath(environment, overlay_dir)
    return {
        "directory": str(overlay_dir),
        "base_key": base_key(getattr(runtime, "provenance_files", ()) or ()),
        "packages": packages,
        "active": bool(packages),
    }
