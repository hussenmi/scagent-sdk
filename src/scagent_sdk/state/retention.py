"""What a session's artifacts cost, and what deleting some of them would actually reclaim.

Storage reporting has to distinguish four quantities, because on a filesystem where versions may
share bytes they diverge sharply:

- **apparent** -- the sum of file sizes, which is what ``du`` reports and what a naive report would
  promise;
- **unique** -- distinct ``(device, inode)`` pairs, so a file counted once no matter how many
  versions link to it;
- **shared** -- unique bytes that a version being considered for deletion holds in common with a
  version being kept;
- **reclaimable** -- unique bytes whose last link would go away, which is the only number a prune
  may promise.

Under hard-linked snapshots those numbers can differ by everything: a branch that only added an
embedding shares essentially all of its bytes with the line it forked from, so its apparent size is
its full store while its reclaimable size is near zero. Measured on a four-version fixture, pruning
such a branch reported 6.5 MB apparent and freed 0.0 MB. Today's artifacts are independent H5AD
files where apparent and reclaimable nearly coincide, which is exactly why the distinction has to be
built in now rather than discovered when the storage format changes underneath the report.

This module only measures and proposes. Nothing here deletes anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scagent_sdk.state.lineage import active_head, ancestry, reachable_from_heads


@dataclass(frozen=True)
class ByteAccounting:
    """The four quantities, plus the file count they were computed over."""

    apparent_bytes: int = 0
    unique_bytes: int = 0
    shared_bytes: int = 0
    reclaimable_bytes: int = 0
    files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "apparent_bytes": self.apparent_bytes,
            "unique_bytes": self.unique_bytes,
            "shared_bytes": self.shared_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "files": self.files,
        }


@dataclass(frozen=True)
class ArtifactUsage:
    """Storage attributable to one execution's artifact directory."""

    execution_id: str
    path: str
    exists: bool
    on_active_line: bool
    reachable: bool
    accounting: ByteAccounting
    tool_name: str = ""
    head_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "path": self.path,
            "exists": self.exists,
            "on_active_line": self.on_active_line,
            "reachable": self.reachable,
            "tool_name": self.tool_name,
            "head_path": self.head_path,
            **self.accounting.to_dict(),
        }


@dataclass(frozen=True)
class PruneProposal:
    """A dry-run prune: what would be removed, and what it would honestly free."""

    session_id: str
    candidates: tuple[ArtifactUsage, ...] = ()
    retained: tuple[str, ...] = ()
    total: ByteAccounting = ByteAccounting()
    candidate_total: ByteAccounting = ByteAccounting()
    warnings: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_total": self.total.to_dict(),
            "candidate_total": self.candidate_total.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "retained_count": len(self.retained),
            "warnings": list(self.warnings),
            "notes": self.notes,
        }


def _files(root: Path) -> list[tuple[Path, tuple[int, int], int]]:
    """Every regular file under ``root`` with its ``(device, inode)`` and size."""

    out: list[tuple[Path, tuple[int, int], int]] = []
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
        except OSError:
            continue
        out.append((path, (stat.st_dev, stat.st_ino), stat.st_size))
    return out


def account(
    targets: Iterable[Path], *, retained: Iterable[Path] = ()
) -> ByteAccounting:
    """Measure ``targets``, treating inodes also present under ``retained`` as unreclaimable.

    Deliberately measures by ``(device, inode)`` rather than by summing file sizes: a hard-linked
    file appears once physically however many directories reference it, and summing apparent sizes
    would overstate both the cost and the saving.
    """

    kept_inodes = {key for path in retained for _, key, _ in _files(Path(path))}
    apparent = 0
    seen: set[tuple[int, int]] = set()
    unique = shared = reclaimable = 0
    count = 0
    for path in targets:
        for _, key, size in _files(Path(path)):
            apparent += size
            count += 1
            if key in seen:
                continue
            seen.add(key)
            unique += size
            if key in kept_inodes:
                shared += size
            else:
                reclaimable += size
    return ByteAccounting(
        apparent_bytes=apparent,
        unique_bytes=unique,
        shared_bytes=shared,
        reclaimable_bytes=reclaimable,
        files=count,
    )


def propose_prune(
    session_dir: Path,
    *,
    session_id: str,
    lineage: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> PruneProposal:
    """Describe what an unreachable-artifact prune would remove and reclaim.

    Candidates are matrix versions the active head does not descend from. That is a *reachability*
    answer, not a liveness one: a version off the active line may still be an alternative the user
    is comparing, so this proposes and never deletes. Deciding which branches are genuinely spent
    needs an explicit disposition vocabulary (retained / pinned / rejected) that does not exist yet,
    which is recorded here as a warning rather than assumed away.
    """

    nodes = lineage.get("nodes")
    nodes = nodes if isinstance(nodes, Mapping) else {}
    head = active_head(lineage)
    live = reachable_from_heads(lineage, [head] if head else [])
    root = Path(session_dir)

    # More than one parentless version means the recorded topology does not explain how the work
    # connects: either inputs were adopted from outside the analysis, or a migration could not
    # recover parentage because the arguments were never recorded. Reachability is then close to
    # meaningless -- a real session shows up as a dozen isolated roots, all but one of which look
    # prunable while actually being the analysis. Refuse to imply they are spent.
    roots = sorted(
        execution_id
        for execution_id, node in nodes.items()
        if isinstance(node, Mapping) and not isinstance(node.get("parent_execution_id"), str)
    )
    topology_reliable = len(roots) <= 1

    warnings: list[str] = []
    if not nodes:
        warnings.append(
            "no lineage versions recorded, so nothing can be judged unreachable; "
            "open the session once to reconstruct the forest before pruning"
        )
    if head is None and nodes:
        warnings.append(
            "lineage has versions but no active head; every version reads as unreachable"
        )
    if not topology_reliable:
        warnings.append(
            f"{len(roots)} of {len(nodes)} versions have no recorded parent, so this session's "
            "topology does not explain how its work connects and reachability cannot identify "
            "abandoned work; treat every candidate below as unverified and prune nothing here"
        )

    candidates: list[ArtifactUsage] = []
    retained_paths: list[Path] = []
    retained_ids: list[str] = []
    for execution_id in sorted(nodes):
        node = nodes[execution_id]
        if not isinstance(node, Mapping):
            continue
        directory = root / "artifacts" / "capabilities" / execution_id
        if execution_id in live:
            retained_paths.append(directory)
            retained_ids.append(execution_id)

    for execution_id in sorted(nodes):
        node = nodes[execution_id]
        if not isinstance(node, Mapping) or execution_id in live:
            continue
        directory = root / "artifacts" / "capabilities" / execution_id
        created = node.get("created_by")
        tool = created.get("tool_name") if isinstance(created, Mapping) else None
        candidates.append(
            ArtifactUsage(
                execution_id=execution_id,
                path=str(directory.relative_to(root)),
                exists=directory.is_dir(),
                on_active_line=False,
                reachable=False,
                accounting=account([directory], retained=retained_paths),
                tool_name=str(tool) if isinstance(tool, str) else "",
                head_path=(
                    str(node["head_path"]) if isinstance(node.get("head_path"), str) else None
                ),
            )
        )

    if candidates and topology_reliable:
        warnings.append(
            "these versions are merely unreachable from the active head, which is not the same as "
            "spent; an explicit branch disposition (retained / pinned / rejected) is required "
            "before any of them may be deleted"
        )

    every_artifact = [
        root / "artifacts" / "capabilities" / execution_id
        for execution_id in sorted(artifacts)
        if isinstance(artifacts.get(execution_id), Mapping)
    ]
    candidate_total = account(
        [root / "artifacts" / "capabilities" / item.execution_id for item in candidates],
        retained=retained_paths,
    )
    if candidate_total.apparent_bytes and not candidate_total.reclaimable_bytes:
        warnings.append(
            "candidates hold no independent bytes: everything they contain is shared with a "
            "retained version, so deleting them would reclaim nothing"
        )

    return PruneProposal(
        session_id=session_id,
        candidates=tuple(candidates),
        retained=tuple(retained_ids),
        total=account(every_artifact),
        candidate_total=candidate_total,
        warnings=tuple(warnings),
        notes={
            "active_head": head,
            "versions": len(nodes),
            "versions_on_active_line": len(live),
            "active_line_depth": len(ancestry(lineage, head)) if head else 0,
            "committed_executions": len(artifacts),
            "parentless_versions": len(roots),
            # A prune must consult this before acting on ``candidates`` at all.
            "topology_reliable": topology_reliable,
        },
    )
