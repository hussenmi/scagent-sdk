# Artifact lineage implementation — review brief

Status: implementation record for review; the design it implements is
`docs/artifact-lineage-and-head-spec.md` (v2.2)
Date: 2026-07-30
Scope: what landed for each stage of the spec, how each part was verified, where the implementation
departed from the specification, and what remains open.

Branch `main` at `f741291`.

Baseline at HEAD: **610 passed, 0 failed**; `ruff` clean; `mypy` clean (51 files);
`scagent-sdk capability validate` pass (23 skills, 51 tools).
One pre-existing failure (`media_capability_test`) exists only in the shared working tree, from
another worker's uncommitted files; it fails identically at HEAD without our changes.

## Commits

| commit | scope |
|---|---|
| `f927e81` | docs: record the spec + the three defects it surfaced in `current-state.md` |
| `02d3935` | fix: `convert_gene_ids` re-mints count identities + explicit invalidation |
| `38df39e` | fix: `recover_pending` ordered by staging event sequence |
| `d4cf013` | fix: `investigate_batch` stops asserting a head from a read-only tool |
| `565fe25` | fix: `output_view` gives each code artifact in a record a distinct name |
| `64d8e33` | **Stage 1** state contract |
| `df47cd4` | **Stage 2** forest recorded at commit + fact routing |
| `e4457d4` | **Stage 3** head-resolved paths + refusal of superseded inputs |
| `95f9134` | **Stage 4** branching and version switching |
| `f741291` | **Stage 5** v1→v2 reducer and migration |

## Stage 1 — state contract (`64d8e33`)

- Split `SESSION_SCHEMA_VERSION` into `SESSION_METADATA_SCHEMA_VERSION` (1) and
  `SESSION_STATE_SCHEMA_VERSION` (2), per your D10 note. `SUPPORTED_STATE_SCHEMA_VERSIONS = {1, 2}`
  so a v1 state still opens, keeps its version, and defaults to an empty forest.
- `SessionState.lineage` added top-level, deliberately outside `facts`.
- New `src/scagent_sdk/state/lineage.py`, side-effect-free:
  `identity_signature` (8 axes: `cell_set`, `clustering`, `count_representation`,
  `dataset_revision`, `expression`, `hvg`, `representation`, `umap`; **raises** on an unregistered
  axis), `FACT_ROOT_SCOPES` (12 roots), `LineageNode`, `place_node`, `checkout`, `classify_input`,
  `ancestry`, `node_for_path`, `reachable_from_heads`.
- Validated against all 8 real sessions on disk: every one computes a signature, **no session
  contains an unregistered fact root**.
- 25 tests.

## Stage 2 — forest at commit + fact routing (`df47cd4`)

- Dispatch records the matrix input, the node it resolves to, and the head it started from.
  Parentage comes from the **resolved input**, never the commit-time head.
- Facts split by the scope registry. Node-scoped roots attach to the node they describe — including
  from read-only tools, which create no node but produce most of a session's evidence. They reach
  global facts only when the head is that node **or descends from it**.
- `_reject_stale_base` (D6) refuses a matrix commit whose head moved, **before** `os.replace`.
- `recover_pending` quarantines an unorderable staged result (D9) rather than adopting it.
- Fact-root validation moved to staging time, so a contract breach is an ordinary tool error rather
  than a PostToolUse failure after compute was reported successful.
- 23 tests.

### Two deviations from the spec, both deliberate

1. **Nodes store fact patches, not snapshots.** A snapshot per node would place one full copy of the
   session's facts on every node — 272 KB × 15 nodes on the reference session. `resolve_node_facts`
   folds patches along the ancestry and reproduces the same value.
2. **Fail-closed validation happens at staging, not commit** (as above).

### Three corrections the tests forced

- RFC 7396 treats `null` as *delete*, so emitting `parent_execution_id: None` erased the key.
  Optional fields are now omitted and read with `.get()`.
- Forking off an ancestor moves the head to a line that does not inherit the previous head's facts,
  so that view must be **replaced**, not merged. Added `merge_diff`, which Stage 4's checkout reuses.
- Ancestor evidence must stay globally visible; suppressing it made global facts disagree with
  `resolve_node_facts(active)`, the invariant a checkout depends on.

## Stage 3 — head resolution and refusal (`e4457d4`)

This is the stage that closes the original defect.

- Tools declare `primary_matrix_input` / `primary_matrix_output`. **Declared, not inferred**, for two
  measured reasons: CellBender emits three `.h5` matrices of which only the filtered one continues
  the analysis, and cluster QC writes a matrix only when it removes clusters. A tool producing an
  undeclared AnnData **fails closed**.
- `path` removed from `input_schema.required` across the 20 manifests that had it. The manifest
  rejects declaring a matrix input that is still required.
- Omitted input → the executor injects the active artifact and reports `resolved_input`
  (`path`, `relation`, `source`) in the envelope.
- Supplied input validated: a tracked `ancestor` **or** `sibling` is refused for any tool that
  transforms the dataset, naming the current artifact. Read-only tools exempt — inspecting an earlier
  or unrelated file creates no node.
- `base.md` loses "use `files[].path` verbatim"; gains the tracking contract. Resolution-ladder
  reference updated.

**Live verification** (`sessions/crc_truth_labeled.h5ad`, real broker + compute runtimes, no model):
materialize → QC → normalize chained through injected heads with no path named; handing the
superseded counts artifact to `select_highly_variable_genes` was refused; `describe_dataset` on that
same artifact was allowed.

**Interim heuristics were measured, not assumed** before being replaced by declarations:
89/89 recorded matrix arguments are named `path`; 0/61 matrix-producing executions declared more
than one `.h5ad`.

## Stage 4 — branching and switching (`95f9134`)

Stage 3 removed the only way to express a deliberate comparison, so this restores it.

- `branch_from` forks an alternative from a recorded version; parent is the named artifact, the
  active version does not move. It is an **executor control argument**: declared in the schema of all
  20 transforming tools so the model may pass it, stripped before dispatch (a test asserts the skill
  never sees it). Refused for a read-only tool, alongside an explicit path, or naming an artifact
  this analysis did not produce.
- New skill `analysis-versions`: `list_analysis_versions`, `switch_analysis_version`.
  **Skills still never write state** — the manifest declares `lineage_operation: checkout`, the skill
  resolves/validates its target and reports what the switch means, the executor performs the
  mutation. Switching moves head + node-scoped facts in **one recorded event**. A rationale is
  required.
- `CapabilityContext` gains read-only `state_lineage` so a tool can describe versions without
  discovering them from the filesystem.
- Guidance: `base.md` + resolution-ladder reference cover branching, switching before annotation or
  finalization, and that a branch's evidence is not session evidence until active.

**Live verification**: a real 2.0/1.5/1.0 sweep of one neighbor graph produced three siblings with
the head unchanged; switching to the 1.0 branch made its clustering current; the next omitted-path
step derived from it.

- 17 tests.

## Stage 5 — reducer and migration (`f741291`)

- `rebuild_forest` replays committed capability events. Read-only executions are replayed too, not
  only matrix-producing ones — walking parent chains alone rebuilds nodes describing almost nothing.
- `SessionStore.open` migrates a v1 checkpoint. Historical events carry no lineage patch, so a schema
  default cannot substitute for the reducer. The forest is derived, so it is materialized into the
  checkpoint without recording an event.
- Reconstruction is **more forgiving than a live commit**: an unregistered fact root in
  already-committed history is kept session-wide and named in warnings rather than raising.
- Sessions that recorded no arguments cannot yield parentage; reported explicitly so a flat set of
  roots does not read as evidence that the work was unrelated.
- 15 tests, including two against the reference session (skipped if absent, since it is host state).

### Verified against all 9 sessions on disk

All open, upgrade to state v2, and reconstruct. The reference session
(`run_20260727T052254Z_58b435`) rebuilds its exact 13-deep chain
(`final-annotated → celltypist-annotated → scimilarity-annotated → clustered → umap → neighbors →
pca → hvg-selected → log-normalized → doublet-annotated → qc-assessed → counts-ready →
gene-symbols`) with precisely 2 nodes off the line — the abandoned clusterings — both children of the
UMAP. Its 7 node-scoped fact roots round-trip **identically**, reached by a different route than the
one that originally accumulated them.

Three sessions predate argument recording → every version reconstructs as a root, with a warning.

### The bug had already happened in production

`run_20260728T153108Z_de92e7`: `finalize_analysis` read `clustered.h5ad` (parent `cc68c7f1`) rather
than the annotated output, so its `final-annotated.h5ad` carries **neither annotator's per-cell
columns**. `run_scimilarity_annotation` → `run_celltypist_annotation` had chained correctly; finalize
did not. Nothing in the old state recorded this.

### A deadlock the migration tests caught

Recording the warning event inside `open()`'s exclusive lock hangs forever: `record()` takes the same
lock on its own descriptor and `flock` does not re-enter. The reducer now returns warnings for the
caller to record after the lock is released. This would have frozen every session open.

## Acceptance cases from the spec

| # | case | where |
|---|---|---|
| 1 | three runs from one parent are siblings, not a chain | contract + migration tests |
| 2 | sequential omitted-path annotations compose into one head | `lineage_commit_test` |
| 3 | explicitly stale annotation input rejected | `lineage_commit_test` + live |
| 4 | explicit same-identity branching creates two nodes | `lineage_branch_test` + live |
| 5 | reference session reconstructs exactly | `lineage_migration_test` + all 9 sessions |
| 6 | recovery cannot reorder or silently rebase | `lineage_commit_test` |
| 7 | unknown identity axis fails loudly | `lineage_contract_test` |
| 8 | read-only tools cannot move or rewrite the head | `lineage_commit_test` |
| 9 | branch commit leaves head **and** active facts unchanged | `lineage_branch_test` |
| 10 | checkout is atomic across head + facts + `prepared_path` | `lineage_branch_test` |
| 11 | omitted input reaches the executor and resolves | `lineage_commit_test` + live |
| 12 | untracked path mid-session rejected without adopt intent | see open question 1 |
| 13 | unregistered fact root fails closed | contract + commit tests |
| 14 | finalize blocked after gene conversion | `gene_conversion_identity_test` (7 tests) |

## Open questions we would like judged

1. **D3's untracked-path rule was narrowed.** The spec rejects an untracked path when the forest is
   non-empty, absent adopt intent. Applied literally that blocks legitimate work — asking about an
   unrelated file mid-session hands `describe_dataset` an untracked path. We restricted the policy to
   tools declaring a `primary_matrix_output`, so read-only inspection is unrestricted, and did **not**
   implement `adopt_untracked`. A transforming tool given an untracked path currently becomes a new
   root rather than being refused. Is that acceptable, or does adopt intent still need to exist?
2. **`prepared_path` was not removed.** The spec says the executor should derive it and the nine
   skill call sites should be stripped. We left both in place: `investigate_batch`'s read-only
   assertion is gone (`d4cf013`), but the eight writers still set it and the executor does not own it.
   It is now redundant rather than authoritative. Worth a follow-up, or leave it?
3. **Historical matrix outputs are identified by `.h5ad` suffix in the reducer**, since historical
   events predate the declarations. Safe on measured data (0/61 wrote two), and a multi-matrix event
   is warned about. Sound?
4. **`branch_from` is a per-tool schema property**, duplicated across 20 manifests, because the model
   runtime enforces `additionalProperties: false` and the executor cannot inject an undeclared
   argument. Is a control argument declared per tool the right shape, or should it be a manifest-level
   affordance the runtime adds?
5. **D11 remains deferred**: pruning and column overlays. `reachable_from_heads` exists as the
   prerequisite; retention still needs the `retained`/`pinned`/`rejected` branch-status vocabulary.
   `sessions/` on this host is 26 GB with no retention policy, and the 19× multiplier is unchanged.

## Notes on process

- Every commit was staged as HEAD-plus-our-changes for files entangled with another worker's
  uncommitted work (`single-cell-counts` and `visualize-single-cell` manifests, `base.md`,
  `workflow-decisions.md`, and the two inventory tests). Each commit was verified to build and test
  in a clean checkout of its own tree, not just in the shared working tree.
- Inventory-count tests (`cli_test`, `capability_registry_test`) are asserted at HEAD+ours only, so
  our commits are self-consistent; the other worker's skill bumps them again separately.
