from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from scagent_sdk.execution import overlay


def _runtime(tmp_path: Path, name: str = "rapids-main") -> SimpleNamespace:
    lock = tmp_path / "pixi.lock"
    lock.write_text("locked-contents-v1", encoding="utf-8")
    return SimpleNamespace(name=name, provenance_files=(lock,))


def test_storage_base_prefers_explicit_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(overlay.STORAGE_ENV, str(tmp_path / "chosen"))
    assert overlay.overlay_storage_base() == tmp_path / "chosen"


def test_base_key_is_stable_and_content_derived(tmp_path: Path) -> None:
    a = tmp_path / "pixi.lock"
    a.write_text("one", encoding="utf-8")
    first = overlay.base_key((a,))
    assert first == overlay.base_key((a,))
    a.write_text("two", encoding="utf-8")
    assert overlay.base_key((a,)) != first
    assert overlay.base_key(()) == "base"


def test_overlay_directory_is_runtime_and_base_scoped(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    directory = overlay.overlay_directory_for_runtime(runtime, storage_base=tmp_path / "root")
    # <root>/<runtime>/<base_key>
    assert directory.parent.name == "rapids-main"
    assert directory.parent.parent == tmp_path / "root"


def test_apply_exports_dir_but_no_pythonpath_when_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(overlay.STORAGE_ENV, str(tmp_path / "store"))
    runtime = _runtime(tmp_path)
    env: dict[str, str] = {}
    record = overlay.apply_to_worker_environment(env, runtime)
    assert env[overlay.OVERLAY_DIR_ENV].endswith(overlay.base_key(runtime.provenance_files))
    assert "PYTHONPATH" not in env
    assert record["active"] is False
    assert record["packages"] == []


def test_apply_injects_pythonpath_when_overlay_has_packages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(overlay.STORAGE_ENV, str(tmp_path / "store"))
    runtime = _runtime(tmp_path)
    overlay_dir = overlay.overlay_directory_for_runtime(runtime)
    overlay_dir.mkdir(parents=True)
    (overlay_dir / overlay.MANIFEST_NAME).write_text(
        json.dumps({"installed": [{"name": "gseapy", "version": "1.1.3"}]}), encoding="utf-8"
    )
    env: dict[str, str] = {}
    record = overlay.apply_to_worker_environment(env, runtime)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(overlay_dir)
    assert record["active"] is True
    assert record["packages"][0]["name"] == "gseapy"


def test_prepend_pythonpath_preserves_existing(tmp_path: Path) -> None:
    env = {"PYTHONPATH": "/pre/existing"}
    overlay.prepend_pythonpath(env, tmp_path / "ov")
    assert env["PYTHONPATH"] == f"{tmp_path / 'ov'}{os.pathsep}/pre/existing"


def test_installed_packages_tolerates_missing_and_malformed(tmp_path: Path) -> None:
    assert overlay.installed_packages(tmp_path) == []
    (tmp_path / overlay.MANIFEST_NAME).write_text("not json", encoding="utf-8")
    assert overlay.installed_packages(tmp_path) == []
