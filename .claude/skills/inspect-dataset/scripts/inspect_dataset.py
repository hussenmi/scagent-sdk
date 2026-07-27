"""Read-only, standard-library dataset identity inspection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SAMPLE_BYTES = 1024 * 1024


def _format_evidence(path: Path, head: bytes) -> dict[str, Any]:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    signature = "unknown"
    if head.startswith(b"\x89HDF\r\n\x1a\n"):
        signature = "hdf5"
    elif head.startswith(b"%%MatrixMarket"):
        signature = "matrix-market"
    elif head.startswith(b"\x1f\x8b"):
        signature = "gzip"
    elif head.startswith(b"PK\x03\x04"):
        signature = "zip"
    extension = suffixes[-1].lstrip(".") if suffixes else "none"
    expected = {
        "h5ad": "hdf5",
        "h5": "hdf5",
        "hdf5": "hdf5",
        "mtx": "matrix-market",
        "gz": "gzip",
        "zip": "zip",
    }.get(extension)
    return {
        "extension": extension,
        "suffixes": suffixes,
        "byte_signature": signature,
        "extension_signature_consistent": expected is None or expected == signature,
    }


def _fingerprint(path: Path, *, mode: str, size: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"scagent-dataset-v1\0{size}\0".encode())
    with path.open("rb") as handle:
        if mode == "full":
            for chunk in iter(lambda: handle.read(SAMPLE_BYTES), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(SAMPLE_BYTES))
            if size > SAMPLE_BYTES:
                handle.seek(max(0, size - SAMPLE_BYTES))
                digest.update(handle.read(SAMPLE_BYTES))
    return f"sha256:{digest.hexdigest()}"


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be a non-empty string")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"dataset file not found: {path}")
    mode = arguments.get("hash_mode", "sampled")
    if mode not in {"sampled", "full"}:
        raise ValueError("hash_mode must be sampled or full")
    stat = path.stat()
    with path.open("rb") as handle:
        head = handle.read(64)
    identity = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "fingerprint": _fingerprint(path, mode=mode, size=stat.st_size),
        "fingerprint_mode": mode,
        "format": _format_evidence(path, head),
    }
    artifact_path = context.staging_dir / "inspection.json"
    artifact_path.write_text(
        json.dumps(identity, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "summary": (
            f"Inspected {path.name}: {stat.st_size} bytes, "
            f"signature={identity['format']['byte_signature']}, hash_mode={mode}."
        ),
        "details": identity,
        "facts_patch": {"dataset": identity},
        "artifacts": [
            {
                "name": "dataset-inspection",
                "relative_path": "inspection.json",
                "media_type": "application/json",
            }
        ],
    }
