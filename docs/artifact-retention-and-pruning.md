# Artifact retention and recoverable pruning

Status: implemented for file-backed artifacts; Zarr directory stores remain explicitly unsupported
Date: 2026-07-30

## Purpose

The lineage forest answers which matrix version produced another. It does **not** answer whether an
alternative is still valuable, whether a secondary artifact inside a live execution is redundant,
or whether deleting a path would actually free bytes. Retention adds those missing policy decisions
without weakening immutable artifacts or making age/reachability a deletion heuristic.

The rules are deliberately conservative:

1. Unclassified means protected.
2. Only an explicitly `rejected` branch can lose its matrix payload.
3. A retained, pinned, active, or still-unclassified leaf protects its complete ancestry.
4. A file inside an otherwise protected execution is eligible only when its exact recorded path is
   explicitly marked `superseded`.
5. `required` and `published` files survive branch rejection.
6. `result.json`, reports, figures, tables, and other lightweight provenance survive branch
   rejection by default. Rejected-branch selection targets recorded matrix containers.
7. Unreliable topology, changed or missing files, symlinks, unknown schemas, stale plans, or
   incomplete prior operations fail closed.

## Durable state

Session-state schema v3 adds executor-owned `state.retention`:

```json
{
  "schema_version": 1,
  "branches": {
    "<execution-id>": {
      "disposition": "retained | pinned | rejected",
      "reason": "...",
      "recorded_at": "..."
    }
  },
  "artifact_roles": {
    "<execution-id>": {
      "<recorded-relative-path>": {
        "role": "required | published | superseded",
        "reason": "...",
        "recorded_at": "..."
      }
    }
  },
  "operations": {
    "<operation-id>": {
      "status": "staging | trashed | restoring | restored | purging | purged",
      "entries": []
    }
  }
}
```

v1 and v2 sessions migrate to an empty registry. Nothing is inferred from age, topology, filenames,
or prior model prose.

`pinned` is stronger than `retained`: it cannot become `rejected` in one transition. The user must
first record an explicit unpin (`pinned → retained`) and then a separate rejection. Likewise,
`published` must first become `required` before it can become `superseded`.

## Planning and concurrency

`session prune` is read-only unless `--apply` is present. A plan contains:

- the state revision;
- exact execution and artifact paths;
- file size, modification time, device, inode, link count, and SHA-256 content identity;
- the grace period;
- byte accounting using `st_nlink`;
- a SHA-256 `plan_id` over that complete snapshot.

Apply requires the exact `plan_id`. The plan is recomputed inside the session lock against freshly
reloaded state and current file metadata before the authorization event is appended. Any state or
file change makes the token stale and no move occurs.

Device and inode provide the cheap same-filesystem race check. The content digest is the portable
fallback: a complete session restored from backup legitimately receives new inodes, and recovery
accepts it only when the artifact bytes still match the authorized plan.

Once an operation is staged, a lineage head covered by it is unavailable for checkout, capability
input, evidence attachment, or commit. Commit performs this check against state reloaded under the
session lock, so a process that began compute before pruning cannot attach a result to an artifact
that has since entered trash.

## Event-first trash, restore, and purge

Pruning never unlinks canonical artifacts directly.

1. `retention.prune_staged` records the exact paths and changes operation state to `staging`.
2. Each file is atomically renamed on the same filesystem to
   `.trash/retention/<operation-id>/<original-session-relative-path>`.
3. `retention.prune_trashed` records completion and sets `purge_after`.

A crash between any two renames leaves the operation in `staging`. `session retention-recover`
continues the already-authorized moves idempotently; it never invents a new plan.

Trash consumes its original bytes until purge. `session storage` therefore reports canonical
artifacts, grace-period trash, and their inode-deduplicated physical union separately; moving a
file to trash can never appear as reclaimed storage.

The physical view also reports paths under `artifacts/capabilities/` whose execution directory is
not indexed by `state.artifacts`. Such untracked data may be crash debris or an external copy. It is
measured and named separately, and is never eligible for policy pruning; recovery or explicit
adoption must establish ownership first.

Restore mirrors that protocol through `retention.restore_staged` and `retention.restored`. Paths are
session-relative, so restoring still works after the complete session directory is moved.

Purge is separate and irreversible:

- the operation must still be `trashed`;
- the grace timestamp must have elapsed;
- `--confirm` must exactly repeat the operation UUID;
- `retention.purge_staged` is durable before unlinking;
- `retention.purged` leaves a permanent tombstone.

The output view is disposable. Refreshing it after trash or restore drops or recreates generated
symlinks according to which canonical files currently exist.

## Commands

```bash
# Classify branches and exact artifacts; these never move files.
scagent-sdk session branch-disposition SESSION EXECUTION rejected \
  --reason "resolution 1.5 was selected instead"
scagent-sdk session artifact-role SESSION EXECUTION secondary.h5ad superseded \
  --reason "duplicate intermediate emitted by the same execution"

# Read-only plan, then apply that exact snapshot to trash.
scagent-sdk session prune SESSION --grace-hours 168
scagent-sdk session prune SESSION --grace-hours 168 --apply 'prune:v1:sha256:...'

# Recover an interrupted authorized operation.
scagent-sdk session retention-recover SESSION

# Restore during the grace window.
scagent-sdk session restore SESSION OPERATION --confirm OPERATION

# Permanently unlink only after the grace window.
scagent-sdk session purge SESSION OPERATION --confirm OPERATION
```

## Deliberate boundary

This implementation supports file-backed H5AD/HDF5/loom artifacts. A Zarr artifact is a directory
tree whose chunk links, metadata files, and partial updates require a format-aware safe walker.
Directory artifacts therefore block a plan with an actionable error. Zarr deduplication and purge
belong to the separate storage-format prototype; they are not smuggled into H5AD retention.

Column overlays are also unchanged. Pruning bounds abandoned/superseded storage; overlays or
chunk-sharing reduce the cost of versions that remain live.
