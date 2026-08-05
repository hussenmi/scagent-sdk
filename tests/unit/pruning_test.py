from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import scagent_sdk.state.pruning as pruning
from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.cli import main
from scagent_sdk.contracts.state import SESSION_STATE_SCHEMA_VERSION
from scagent_sdk.errors import CapabilityExecutionError, RetentionError
from scagent_sdk.session import AnalysisSession
from scagent_sdk.state.pruning import (
    RetentionManager,
    build_prune_plan,
    execution_unavailable_reason,
)
from scagent_sdk.state.store import SessionStore


def _file(
    directory: Path,
    relative_path: str,
    size: int,
    *,
    name: str | None = None,
    media_type: str = "application/x-hdf5",
) -> dict[str, Any]:
    path = directory / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return {
        "name": name or Path(relative_path).stem,
        "relative_path": relative_path,
        "media_type": media_type,
        "size_bytes": size,
    }


def _node(
    execution_id: str,
    parent: str | None,
    *,
    head: str = "matrix.h5ad",
) -> dict[str, Any]:
    return {
        "parent_execution_id": parent,
        "head_path": f"artifacts/capabilities/{execution_id}/{head}",
        "fact_patches": [],
    }


def _session(
    tmp_path: Path,
    *,
    rejected_files: tuple[tuple[str, int], ...] = (("matrix.h5ad", 4096),),
) -> AnalysisSession:
    session = AnalysisSession.create(tmp_path / "sessions", title="Retention", session_id="s")
    artifacts: dict[str, Any] = {}
    for execution_id, files in {
        "root": (("matrix.h5ad", 100),),
        "keep": (("matrix.h5ad", 200),),
        "reject": rejected_files + (("report.json", 30),),
    }.items():
        directory = session.directory / "artifacts" / "capabilities" / execution_id
        directory.mkdir(parents=True)
        records = [
            _file(
                directory,
                relative,
                size,
                media_type=(
                    "application/json" if relative.endswith(".json") else "application/x-hdf5"
                ),
            )
            for relative, size in files
        ]
        (directory / "result.json").write_text(
            json.dumps({"execution_id": execution_id}), encoding="utf-8"
        )
        artifacts[execution_id] = {
            "kind": "capability-result",
            "path": f"artifacts/capabilities/{execution_id}",
            "tool_name": "fixture",
            "files": records,
        }
    session.store.record(
        "fixture.committed",
        state_patch={
            "artifacts": artifacts,
            "lineage": {
                "active_execution_id": "keep",
                "nodes": {
                    "root": _node("root", None),
                    "keep": _node("keep", "root"),
                    "reject": _node("reject", "root"),
                },
            },
        },
    )
    return session


def _reject(manager: RetentionManager) -> None:
    manager.set_branch_disposition("reject", "rejected", reason="alternative was not selected")


def test_unknown_branches_are_protected_until_explicitly_rejected(tmp_path: Path) -> None:
    session = _session(tmp_path)
    plan = RetentionManager(session.store).plan(grace_seconds=3600)

    assert not plan.applicable
    assert plan.targets == ()
    assert "reject" in plan.protected_execution_ids


def test_rejected_branch_selects_only_matrix_payload_and_keeps_provenance(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)

    plan = manager.plan(grace_seconds=3600)

    assert plan.applicable
    assert [(item.execution_id, item.relative_path) for item in plan.targets] == [
        ("reject", "matrix.h5ad")
    ]
    assert plan.accounting.reclaimable_bytes == 4096
    assert (session.directory / "artifacts/capabilities/reject/report.json").is_file()
    assert (session.directory / "artifacts/capabilities/reject/result.json").is_file()


def test_active_or_still_needed_ancestry_cannot_be_rejected(tmp_path: Path) -> None:
    manager = RetentionManager(_session(tmp_path).store)

    with pytest.raises(RetentionError, match="protected"):
        manager.set_branch_disposition("keep", "rejected", reason="wrong")
    with pytest.raises(RetentionError, match="protected"):
        manager.set_branch_disposition("root", "rejected", reason="still needed")


def test_pinned_branch_requires_an_explicit_unpin_transition(tmp_path: Path) -> None:
    manager = RetentionManager(_session(tmp_path).store)
    manager.set_branch_disposition("reject", "pinned", reason="publication input")

    with pytest.raises(RetentionError, match="pinned"):
        manager.set_branch_disposition("reject", "rejected", reason="changed mind")

    manager.set_branch_disposition("reject", "retained", reason="unpin after archive")
    manager.set_branch_disposition("reject", "rejected", reason="alternative was rejected")
    assert manager.store.state.retention["branches"]["reject"]["disposition"] == "rejected"


def test_superseded_secondary_artifact_is_eligible_but_live_head_is_not(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    keep_dir = session.directory / "artifacts/capabilities/keep"
    secondary = _file(keep_dir, "preview.h5ad", 300, name="preview")
    session.store.record(
        "fixture.secondary",
        state_patch={
            "artifacts": {
                "keep": {
                    **session.store.state.artifacts["keep"],
                    "files": [*session.store.state.artifacts["keep"]["files"], secondary],
                }
            }
        },
    )
    manager = RetentionManager(session.store)

    with pytest.raises(RetentionError, match="matrix head"):
        manager.set_artifact_role(
            "keep", "matrix.h5ad", "superseded", reason="must never remove head"
        )

    manager.set_artifact_role(
        "keep", "preview.h5ad", "superseded", reason="duplicate preview matrix"
    )
    plan = manager.plan(grace_seconds=3600)
    assert [(item.execution_id, item.relative_path) for item in plan.targets] == [
        ("keep", "preview.h5ad")
    ]


def test_required_or_published_artifact_survives_branch_rejection(tmp_path: Path) -> None:
    session = _session(tmp_path, rejected_files=(("matrix.h5ad", 400), ("secondary.h5ad", 500)))
    manager = RetentionManager(session.store)
    manager.set_artifact_role(
        "reject", "secondary.h5ad", "published", reason="referenced by manuscript"
    )
    _reject(manager)

    plan = manager.plan(grace_seconds=3600)
    assert [item.relative_path for item in plan.targets] == ["matrix.h5ad"]

    with pytest.raises(RetentionError, match="published"):
        manager.set_artifact_role("reject", "secondary.h5ad", "superseded", reason="one-step unpin")


def test_plan_token_changes_with_state_or_file_metadata(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    first = manager.plan(grace_seconds=3600)

    session.store.record("fixture.changed", state_patch={"decisions": {"x": True}})
    second = manager.plan(grace_seconds=3600)
    assert second.plan_id != first.plan_id

    matrix = session.directory / "artifacts/capabilities/reject/matrix.h5ad"
    matrix.write_bytes(b"y" * 4096)
    third = manager.plan(grace_seconds=3600)
    assert third.plan_id != second.plan_id


def test_stale_plan_is_refused_before_any_authorization_event_or_move(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=3600)
    session.store.record("fixture.changed", state_patch={"decisions": {"x": True}})
    before = len(session.store.events())

    with pytest.raises(RetentionError, match="stale"):
        manager.apply(str(plan.plan_id), grace_seconds=3600)

    assert len(session.store.events()) == before
    assert (session.directory / "artifacts/capabilities/reject/matrix.h5ad").is_file()


def test_apply_moves_to_trash_records_both_events_and_preserves_result(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=3600)

    result = manager.apply(str(plan.plan_id), grace_seconds=3600)
    operation = result["operation"]
    operation_id = operation["operation_id"]

    assert operation["status"] == "trashed"
    assert not (session.directory / "artifacts/capabilities/reject/matrix.h5ad").exists()
    assert (
        session.directory
        / ".trash"
        / "retention"
        / operation_id
        / "artifacts/capabilities/reject/matrix.h5ad"
    ).is_file()
    assert (session.directory / "artifacts/capabilities/reject/result.json").is_file()
    assert [event.kind for event in session.store.events()][-2:] == [
        "retention.prune_staged",
        "retention.prune_trashed",
    ]


def test_trashed_lineage_head_is_unavailable_until_restored(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=3600)
    operation = manager.apply(str(plan.plan_id), grace_seconds=3600)["operation"]

    reason = execution_unavailable_reason(session.store.state.retention, "reject")
    assert reason is not None and "trashed" in reason

    manager.restore(operation["operation_id"], confirmation=operation["operation_id"])
    assert execution_unavailable_reason(session.store.state.retention, "reject") is None
    assert (session.directory / "artifacts/capabilities/reject/matrix.h5ad").is_file()


def test_restore_is_portable_after_the_complete_session_moves(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=3600)
    operation_id = manager.apply(str(plan.plan_id), grace_seconds=3600)["operation"]["operation_id"]

    moved_root = tmp_path / "moved"
    moved_root.mkdir()
    shutil.copytree(session.directory, moved_root / session.session_id)
    moved = RetentionManager(SessionStore.open(moved_root, session.session_id))
    moved.restore(operation_id, confirmation=operation_id)

    assert (moved_root / session.session_id / "artifacts/capabilities/reject/matrix.h5ad").is_file()


def test_purge_requires_confirmation_and_elapsed_grace(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=3600)
    operation = manager.apply(str(plan.plan_id), grace_seconds=3600)["operation"]
    operation_id = operation["operation_id"]

    with pytest.raises(RetentionError, match="confirmation"):
        manager.purge(operation_id, confirmation="wrong")
    with pytest.raises(RetentionError, match="grace period"):
        manager.purge(operation_id, confirmation=operation_id)

    after = datetime.fromisoformat(operation["purge_after"]) + timedelta(seconds=1)
    result = manager.purge(operation_id, confirmation=operation_id, now=after)
    assert result["operation"]["status"] == "purged"
    assert not (
        session.directory
        / ".trash"
        / "retention"
        / operation_id
        / "artifacts/capabilities/reject/matrix.h5ad"
    ).exists()
    assert (session.directory / "artifacts/capabilities/reject/result.json").is_file()


def test_partial_trash_failure_is_recovered_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, rejected_files=(("matrix.h5ad", 400), ("secondary.h5ad", 500)))
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)
    real_move = pruning._move_path
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash")
        real_move(source, destination)

    monkeypatch.setattr(pruning, "_move_path", fail_second)
    with pytest.raises(RetentionError, match="recoverable"):
        manager.apply(str(plan.plan_id), grace_seconds=0)
    operation_id, operation = next(iter(session.store.state.retention["operations"].items()))
    assert operation["status"] == "staging"

    monkeypatch.setattr(pruning, "_move_path", real_move)
    recovered = manager.recover()
    assert recovered[0]["operation"]["status"] == "trashed"
    assert manager.recover() == []
    assert operation_id in session.store.state.retention["operations"]


def test_incomplete_operation_blocks_new_plans_until_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)

    def fail_move(source: Path, destination: Path) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(pruning, "_move_path", fail_move)
    with pytest.raises(RetentionError, match="recoverable"):
        manager.apply(str(plan.plan_id), grace_seconds=0)

    blocked = manager.plan(grace_seconds=0)
    assert not blocked.applicable
    assert any("must be recovered" in item for item in blocked.blocking_errors)


def test_recovery_refuses_a_path_replaced_after_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)
    real_move = pruning._move_path

    def fail_move(source: Path, destination: Path) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(pruning, "_move_path", fail_move)
    with pytest.raises(RetentionError, match="recoverable"):
        manager.apply(str(plan.plan_id), grace_seconds=0)

    matrix = session.directory / "artifacts/capabilities/reject/matrix.h5ad"
    matrix.unlink()
    matrix.write_bytes(b"z" * 4096)
    monkeypatch.setattr(pruning, "_move_path", real_move)
    with pytest.raises(RetentionError, match="changed before"):
        manager.recover()
    assert manager.store.state.retention["operations"]
    assert next(iter(manager.store.state.retention["operations"].values()))["status"] == "staging"


def test_partial_restore_failure_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, rejected_files=(("matrix.h5ad", 400), ("secondary.h5ad", 500)))
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)
    operation_id = manager.apply(str(plan.plan_id), grace_seconds=0)["operation"]["operation_id"]
    real_move = pruning._move_path
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated restore crash")
        real_move(source, destination)

    monkeypatch.setattr(pruning, "_move_path", fail_second)
    with pytest.raises(RetentionError, match="recoverable"):
        manager.restore(operation_id, confirmation=operation_id)
    assert manager.store.state.retention["operations"][operation_id]["status"] == "restoring"

    monkeypatch.setattr(pruning, "_move_path", real_move)
    recovered = manager.recover()
    assert recovered[0]["operation"]["status"] == "restored"
    assert (session.directory / "artifacts/capabilities/reject/matrix.h5ad").is_file()
    assert (session.directory / "artifacts/capabilities/reject/secondary.h5ad").is_file()


def test_partial_purge_failure_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, rejected_files=(("matrix.h5ad", 400), ("secondary.h5ad", 500)))
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)
    operation_id = manager.apply(str(plan.plan_id), grace_seconds=0)["operation"]["operation_id"]
    real_unlink = pruning._unlink_path
    calls = 0

    def fail_second(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated purge crash")
        real_unlink(path)

    monkeypatch.setattr(pruning, "_unlink_path", fail_second)
    with pytest.raises(RetentionError, match="recoverable"):
        manager.purge(
            operation_id,
            confirmation=operation_id,
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    assert manager.store.state.retention["operations"][operation_id]["status"] == "purging"

    monkeypatch.setattr(pruning, "_unlink_path", real_unlink)
    recovered = manager.recover()
    assert recovered[0]["operation"]["status"] == "purged"


def test_branch_intent_does_not_bypass_unavailable_input_commit_check(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)
    manager.apply(str(plan.plan_id), grace_seconds=0)
    executor = CapabilityExecutor(session)

    with pytest.raises(CapabilityExecutionError, match="became unavailable"):
        executor._reject_stale_base(
            "child",
            {
                "lineage": {
                    "matrix_output": "child.h5ad",
                    "branch_intent": True,
                    "resolved_input_execution_id": "reject",
                }
            },
            session.store.state,
        )


def test_symlink_artifact_is_refused_without_following_it(tmp_path: Path) -> None:
    session = _session(tmp_path)
    matrix = session.directory / "artifacts/capabilities/reject/matrix.h5ad"
    target = session.directory / "safe-but-different-file.h5ad"
    target.write_bytes(b"q" * 4096)
    matrix.unlink()
    matrix.symlink_to(target)
    manager = RetentionManager(session.store)
    _reject(manager)

    plan = manager.plan(grace_seconds=0)
    assert not plan.applicable
    assert any("symlink" in item for item in plan.blocking_errors)
    assert target.read_bytes() == b"q" * 4096


def test_symlinked_artifact_directory_is_refused(tmp_path: Path) -> None:
    session = _session(tmp_path)
    directory = session.directory / "artifacts/capabilities/reject"
    actual = session.directory / "actual-reject-directory"
    directory.rename(actual)
    directory.symlink_to(actual, target_is_directory=True)
    manager = RetentionManager(session.store)
    _reject(manager)

    plan = manager.plan(grace_seconds=0)
    assert not plan.applicable
    assert any("symlinked directory" in item for item in plan.blocking_errors)
    assert (actual / "matrix.h5ad").is_file()


@pytest.mark.parametrize(
    "retention",
    [
        {
            "schema_version": 1,
            "branches": {
                "reject": {
                    "disposition": "maybe",
                    "reason": "corrupt",
                    "recorded_at": "now",
                }
            },
            "artifact_roles": {},
            "operations": {},
        },
        {
            "schema_version": 1,
            "branches": {},
            "artifact_roles": {
                "reject": {
                    "matrix.h5ad": {
                        "role": "probably-published",
                        "reason": "corrupt",
                        "recorded_at": "now",
                    }
                }
            },
            "operations": {},
        },
        {
            "schema_version": 1,
            "branches": {},
            "artifact_roles": {},
            "operations": {
                "op": {
                    "operation_id": "op",
                    "status": "mystery",
                    "entries": [],
                }
            },
        },
    ],
)
def test_malformed_retention_registry_fails_closed(
    tmp_path: Path, retention: dict[str, Any]
) -> None:
    session = _session(tmp_path)
    state = json.loads(json.dumps(session.store.state.to_dict()))
    state["retention"] = retention

    with pytest.raises(RetentionError, match="state.retention"):
        build_prune_plan(
            session.directory,
            session_id=session.session_id,
            state=state,
            grace_seconds=0,
        )


def test_plan_refuses_a_file_that_changes_while_being_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    real_hash = pruning._sha256_file

    def mutate_after_hash(path: Path) -> str:
        digest = real_hash(path)
        path.write_bytes(b"z" * path.stat().st_size)
        return digest

    monkeypatch.setattr(pruning, "_sha256_file", mutate_after_hash)
    plan = manager.plan(grace_seconds=0)
    assert not plan.applicable
    assert any("changed while" in item for item in plan.blocking_errors)


def test_hard_link_outside_plan_is_shared_not_reclaimable(tmp_path: Path) -> None:
    session = _session(tmp_path)
    matrix = session.directory / "artifacts/capabilities/reject/matrix.h5ad"
    outside = tmp_path / "outside-link.h5ad"
    os.link(matrix, outside)
    manager = RetentionManager(session.store)
    _reject(manager)

    plan = manager.plan(grace_seconds=3600)
    assert plan.accounting.shared_bytes == 4096
    assert plan.accounting.reclaimable_bytes == 0


def test_unreliable_topology_blocks_even_explicit_classification(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    session.store.record(
        "fixture.corrupt-topology",
        state_patch={
            "lineage": {
                "nodes": {
                    "reject": {
                        **session.store.state.lineage["nodes"]["reject"],
                        "parent_execution_id": "missing",
                    }
                }
            }
        },
    )

    plan = manager.plan(grace_seconds=3600)
    assert not plan.applicable
    assert any("unreliable" in item for item in plan.blocking_errors)


def test_changed_or_missing_committed_file_fails_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    matrix = session.directory / "artifacts/capabilities/reject/matrix.h5ad"
    matrix.write_bytes(b"short")

    plan = manager.plan(grace_seconds=3600)
    assert not plan.applicable
    assert any("changed size" in item for item in plan.blocking_errors)


def test_v2_checkpoint_migrates_to_empty_fail_closed_retention(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state_path = session.store.state_path
    value = json.loads(state_path.read_text(encoding="utf-8"))
    value["schema_version"] = 2
    value.pop("retention")
    state_path.write_text(json.dumps(value), encoding="utf-8")

    reopened = SessionStore.open(session.directory.parent, session.session_id)

    assert reopened.state.schema_version == SESSION_STATE_SCHEMA_VERSION == 3
    assert reopened.state.retention == {
        "schema_version": 1,
        "branches": {},
        "artifact_roles": {},
        "operations": {},
    }


def test_purge_tombstone_survives_reopen_and_cannot_be_restored(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manager = RetentionManager(session.store)
    _reject(manager)
    plan = manager.plan(grace_seconds=0)
    operation = manager.apply(str(plan.plan_id), grace_seconds=0)["operation"]
    operation_id = operation["operation_id"]
    manager.purge(
        operation_id,
        confirmation=operation_id,
        now=datetime.now(timezone.utc) + timedelta(seconds=1),
    )

    reopened = RetentionManager(SessionStore.open(session.directory.parent, session.session_id))
    assert reopened.store.state.retention["operations"][operation_id]["status"] == "purged"
    with pytest.raises(RetentionError, match="cannot be restored"):
        reopened.restore(operation_id, confirmation=operation_id)


def test_cli_plan_apply_and_restore_require_exact_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _session(tmp_path)
    root = session.directory.parent
    assert (
        main(
            [
                "session",
                "branch-disposition",
                "s",
                "reject",
                "rejected",
                "--reason",
                "not selected",
                "--sessions-root",
                str(root),
            ]
        )
        == 0
    )
    capsys.readouterr()

    state_mtime = session.store.state_path.stat().st_mtime_ns
    assert (
        main(
            [
                "session",
                "prune",
                "s",
                "--grace-hours",
                "1",
                "--sessions-root",
                str(root),
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["applicable"] is True
    assert session.store.state_path.stat().st_mtime_ns == state_mtime

    assert (
        main(
            [
                "session",
                "prune",
                "s",
                "--grace-hours",
                "1",
                "--apply",
                plan["plan_id"],
                "--sessions-root",
                str(root),
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    operation_id = applied["operation"]["operation_id"]
    untracked = session.directory / "artifacts/capabilities/untracked-crash"
    untracked.mkdir()
    (untracked / "orphan.bin").write_bytes(b"o" * 123)

    assert main(["session", "storage", "s", "--sessions-root", str(root)]) == 0
    trashed_storage = json.loads(capsys.readouterr().out)
    assert trashed_storage["trash_total"]["unique_bytes"] == 4096
    assert trashed_storage["untracked_artifacts_total"]["unique_bytes"] == 123
    assert trashed_storage["untracked_artifacts"] == [
        {
            "path": "artifacts/capabilities/untracked-crash",
            "apparent_bytes": 123,
            "unique_bytes": 123,
            "shared_bytes": 0,
            "reclaimable_bytes": 123,
            "files": 1,
        }
    ]
    assert (
        trashed_storage["managed_physical_total"]["unique_bytes"]
        == trashed_storage["session_total"]["unique_bytes"]
        + trashed_storage["trash_total"]["unique_bytes"]
        + trashed_storage["untracked_artifacts_total"]["unique_bytes"]
    )

    assert (
        main(
            [
                "session",
                "restore",
                "s",
                operation_id,
                "--confirm",
                operation_id,
                "--sessions-root",
                str(root),
            ]
        )
        == 0
    )
    restored = json.loads(capsys.readouterr().out)
    assert restored["operation"]["status"] == "restored"
    assert main(["session", "storage", "s", "--sessions-root", str(root)]) == 0
    restored_storage = json.loads(capsys.readouterr().out)
    assert restored_storage["trash_total"]["unique_bytes"] == 0
