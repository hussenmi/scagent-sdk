# Artifact lineage and the identity head

Status: proposal for review; not implemented
Date: 2026-07-27
Scope: move H5AD continuity from model discretion into the capability executor, without
restoring an in-memory AnnData, without introducing a DAG, and without changing floor semantics.

## Contents

1. [Problem](#problem)
2. [Observed evidence](#observed-evidence)
3. [Why point fixes do not generalize](#why-point-fixes-do-not-generalize)
4. [Proposed change](#proposed-change)
5. [Migration](#migration)
6. [Invariants and tests](#invariants-and-tests)
7. [Open questions for review](#open-questions-for-review)
8. [Non-goals](#non-goals)
9. [Review checklist](#review-checklist)

## Problem

A session carries scientific state in two substrates with different merge semantics:

| substrate | merges by | owned by | drift possible |
| --- | --- | --- | --- |
| `state.facts` | identity key, via `apply_merge_patch` at commit | executor (`store.py:53`, `executor.py:478`) | no |
| the H5AD chain | whichever `path` argument the model passed | the model | yes |

Facts converge correctly regardless of the order tools run in. The matrix converges only if the
model threads output paths forward correctly. Every capability that adds columns to the matrix
inherits this exposure automatically; nothing in the executor, the floors, or the capability
contract constrains which file a tool is handed.

The failure mode is narrow but permanent: facts, review, and report stay correct, while the
delivered H5AD silently lacks the `obs`/`obsm` produced on a sibling branch.

## Observed evidence

Source: `sessions/run_20260727T052254Z_58b435` (CRC, 960 cells, 14 clusters).

**The reported incident chained correctly, by model choice, not by construction.**

```
clustered.h5ad          a5236aba
  └─ scimilarity-annotated.h5ad   48ebcd21   (read a5236aba)
       └─ celltypist-annotated.h5ad  294debc7 (read 48ebcd21)
            └─ final-annotated.h5ad    2cdb90eb (read 294debc7)
```

Had CellTypist been handed `clustered.h5ad` — equally consistent with both skills' contracts, since
both need only raw counts plus gene symbols — the two branches would have carried identical
`cell_set_id` and `clustering_id`, so no floor would have objected, and `final-annotated.h5ad`
would have shipped without `scimilarity_prediction`.

**The science was never at risk, because evidence lives in facts.**

```
annotation.evidence.scimilarity.cell_set_id  == annotation.evidence.celltypist.cell_set_id
annotation.evidence.scimilarity.clustering_id == annotation.evidence.celltypist.clustering_id
annotation.review.evidence_ids = {markers, scimilarity, celltypist}
```

`finalize` adjudicated from the evidence CSVs those facts reference, not from the `obs` columns of
its input file. The delivered artifact — not the conclusion — is what a mis-chain damages.

**A head pointer already exists, and is already stale.**

`facts.analysis.dataset_revision.prepared_path` points at `.../a5236aba/clustered.h5ad`. That is
the correct *chosen clustering*, but it was not advanced by either annotation write. It lags the
true head by two commits.

**The convention is duplicated per skill and has drifted.** Twelve skill scripts write an H5AD;
`prepared_path` is set by eight of them, re-asserted by one read-only skill, and omitted by four:

| skill script | writes H5AD | sets `prepared_path` |
| --- | --- | --- |
| `single-cell-counts/scripts/counts.py:225` | yes | yes |
| `single-cell-qc/scripts/qc.py:516,626` | yes | yes |
| `doublet-evidence/scripts/doublets.py:491,738` | yes | yes |
| `expression-preprocessing/scripts/preprocess.py:88,186` | yes | yes |
| `dimensionality-reduction/scripts/reduce.py:168,268,351` | yes | yes |
| `single-cell-clustering/scripts/clustering.py:106` | yes | yes |
| `cluster-qc/scripts/evaluate.py:1257` | yes | yes |
| `scvi-integration/scripts/integrate.py:101` | yes | yes |
| `batch-investigation/scripts/investigate.py:397` | **no** | yes (re-asserts input) |
| `inspect-dataset/scripts/convert.py` | yes | **no** |
| `scimilarity-annotation/scripts/run_scimilarity.py` | yes | **no** |
| `celltypist-annotation/scripts/run_celltypist.py` | yes | **no** |
| `finalize-analysis/scripts/finalize.py` | yes | **no** |

The four omissions are exactly the tools implicated in the reported incident. This is not four
bugs; it is one missing owner.

**Footprint consequence.** 58 capability executions produced 15 H5ADs and 228 MB of artifacts from
a 12 MB input (≈19×). Three sibling `clustered.h5ad` files exist from a resolution sweep; two are
unreachable from any live result and will never be read again (~28 MB). No retention or pruning
policy exists anywhere in `src/scagent_sdk`.

## Why point fixes do not generalize

- **Rematerialize the output from facts.** Works only for column-shaped results (annotations,
  scores, per-cell calls). It cannot express `X`-shaped results: normalization, HVG subsetting,
  cell/gene filtering, CellBender, scVI latents. Structurally a fix for one skill family.
- **Set `prepared_path` in the four missing skills.** Adds a ninth and tenth copy of a convention
  that has already drifted four times, and does nothing for the eleventh capability added later.
- **Add a floor.** Floors are state predicates over identities. Both branches in the failure mode
  carry *identical* identities, so no predicate over `facts.analysis.*` can distinguish them.

The property that must hold is "the matrix a tool receives is the current one for its identity."
That is a statement about the executor's dispatch, so it belongs in the executor.

## Proposed change

### D1 — An executor-owned lineage index in session state

Add a top-level `lineage` field to `SessionState` (`contracts/state.py:72`), sibling to
`artifacts`, and bump `SESSION_SCHEMA_VERSION`.

```jsonc
"lineage": {
  "active": "<identity_key>",
  "nodes": {
    "<identity_key>": {
      "identities":    { "cell_set": "cells:sha256:…", "clustering": "clustering:sha256:…", … },
      "head_execution_id": "294debc7-…",
      "head_path":         "artifacts/capabilities/294debc7-…/celltypist-annotated.h5ad",
      "parent_key":        "<identity_key or null>",
      "history":           ["48ebcd21-…", "294debc7-…"]
    }
  }
}
```

Top-level, **not** under `facts`, is deliberate: skills return `facts_patch` and must never be able
to write the index (AGENTS.md — "never let a skill edit session state directly"). Storing it beside
`artifacts` keeps the executor the sole writer, matching how `artifacts` is already maintained at
`executor.py:481-486`.

`identity_key` is a canonical, sorted, hashed encoding of every `facts.analysis.*.id` present after
the patch is applied.

### D2 — Advance vs. fork, computed at commit

In `CapabilityExecutor.commit` (`executor.py:451`), which already holds both `data["facts_patch"]`
and `data["files"]`:

1. If the result declares no `.h5ad` artifact → no lineage change. Read-only evidence tools
   (`marker-annotation`, `batch-investigation`, most of `visualize-single-cell`) never move a head.
2. Otherwise compute `post = apply_merge_patch(state.facts, facts_patch)` and derive
   `key = identity_key(post.analysis)`.
   - `key == state.lineage.active` → **advance**: same node, new `head_execution_id`/`head_path`,
     append to `history`.
   - otherwise → **fork**: create the node with `parent_key = active`, then set `active = key`.

Both inputs are already present at commit; **no skill changes are required** for D1–D2. The
executor derives everything from data the skills already return.

### D3 — Path resolution defaults to the head

In `CapabilityExecutor.execute`, alongside `_resolve_session_paths` (`executor.py:171`, helper at
`:56`):

- Tool declares an H5AD input and `path` is omitted → resolve to `lineage.nodes[active].head_path`.
- `path` supplied → resolve as today, then classify against the index as `head`, `ancestor`, or
  `sibling`, and surface the classification in the envelope.
- `sibling` is rejected unless the call carries the explicit branch intent from D4.

Requires the manifest to mark which argument is the primary matrix input; `capability.yaml` already
declares tool arguments (`capabilities/manifest.py`), so this is a declared field, not inference
from argument names.

### D4 — Explicit branching vocabulary

A resolution sweep is a legitimate deliberate fork. Today the model expresses it by passing a
non-head path, which is indistinguishable from the failure mode. Give it a name — a `branch_from`
argument or a `branch: true` flag — so intent is stated rather than inferred. Without D4, D3
converts a real workflow into a hard error.

### D5 — Envelope and base-prompt alignment

The envelope (`executor.py:438`) gains `resolved_input` (which path was used and why) and
`head_action` (`advanced` | `forked` | `unchanged`). Silent resolution must be visible, or a
`FileNotFound` is traded for an invisible wrong-input.

`configs/models/prompts/base.md:104-105` currently instructs the model to reuse `files[].path`
verbatim — guidance added for the CRC path-resolution defect. D3 supersedes it; that line is
rewritten, not appended to.

### D6 — Pruning (follow-on, unlocked by D1)

With a head index, reachability is computable: an artifact directory not on the ancestry path of
any live head and not referenced by a fact is prunable. This is the first point at which "safe to
delete" is answerable at all.

### D7 — Column overlays (follow-on, optimization of D2)

Once "advance at unchanged identity" is a named executor operation, it may write an `obs`/`obsm`
delta plus a parent pointer instead of a full matrix copy, materialized on read. This is where the
19× footprint goes away. It is an optimization *under* the index and must not be attempted first —
a resolver with no notion of what it resolves toward is the same unowned convention in a new place.

## Migration

- **Schema.** Bump `SESSION_SCHEMA_VERSION`; `SessionState.from_dict` (`contracts/state.py:112`)
  defaults `lineage` to empty.
- **Existing sessions.** The index is fully reconstructible: `capability.result_committed` events
  already carry `facts_patch` and `files`, so replaying the log rebuilds heads. This is consistent
  with the existing rule that events are the source of truth and `state.json` is a materialized
  view (`store.py:270`). Reconstruction should be exercised as a test, not only as a migration.
- **`prepared_path`.** Keep the key, change its writer: the executor derives it from the active
  head, and the nine skill call sites listed above are stripped. Anything reading it keeps working;
  the duplicated convention disappears.
- **`recover_pending`.** `executor.py:496` commits orphaned staging directories at startup. Head
  advancement must be idempotent and ordered, since recovery may commit several executions in one
  pass.

## Invariants and tests

Deterministic contract tests (no compute required — all inputs are JSON):

1. A column-only tool at unchanged identity **advances** the head.
2. A tool minting a new `clustering_id` **forks**, and the parent node retains its own head.
3. A read-only evidence tool leaves the index untouched.
4. Index rebuilt by event replay equals the live index (fixture: the CRC session's `events.jsonl`).
5. Omitted `path` resolves to the active head; envelope reports it.
6. A sibling `path` without branch intent is rejected; with branch intent it forks.
7. A staged-but-uncommitted execution does not move the head — the two-phase guarantee at
   `executor.py:461-463` is preserved.
8. An interrupted or failed execution (`executor.py:206-240`) leaves the index unchanged.
9. `recover_pending` over several orphans produces the same index as sequential commits.

Regression fixture worth adding explicitly: the SCimilarity/CellTypist pair dispatched *both*
against `clustered.h5ad`, asserting the delivered artifact still carries both prediction columns.

## Open questions for review

1. **Which identity nodes compose the key?** `facts.analysis` currently holds `dataset_revision`,
   `cell_set`, `count_representation`, `representation`, `clustering`, `expression`, `hvg`, `umap`.
   The floor evaluator keys on four (`cell_set`, `count_representation`, `representation`,
   `clustering` — `floors/evaluator.py:91-135, 292-302`). Using only the floor four would make two
   different HVG selections collide on one head; using all of them makes almost everything fork.
   **Recommendation: use every `analysis.*.id` present, and advance only on a byte-identical
   tuple.** Over-forking is recoverable (an extra node, nothing lost); over-merging silently
   overwrites a head. This is the highest-risk decision in the spec and the one most worth
   challenging.
2. **Default, or validate-and-warn?** D3 as written resolves silently. A stricter variant requires
   an explicit path and only *rejects* non-head values, keeping the model's intent legible at the
   cost of verbosity.
3. **Should `finalize` be permitted to run off a non-head at all?** Arguably finalization should
   fail closed on anything but the active head, independent of D3.
4. **Two notions of staleness.** Floors detect staleness by comparing evidence identities; the
   index adds "this file is not the head." Should a floor consult the index, or must they stay
   independent so floors remain pure predicates over facts?
5. **Multi-node sessions.** With `active` as a single pointer, how does the model work on two
   branches alternately — implicit switch on branch reference, or an explicit checkout?

## Non-goals

- Restoring a persistent in-memory AnnData. The subprocess/runtime boundary
  (`execution/broker.py:120`) makes that impossible and undesirable.
- Enforcing tool order. The index constrains *which file* a tool reads, never *which tool* runs
  next. Floors remain state predicates, not pipelines.
- Changing floor semantics, the 48 KiB inline limit, artifact immutability, non-overwrite, or
  environment provenance.
- Implementing D6 or D7 in the same change.

## Review checklist

- [ ] Is the identity key in open question 1 correct, and is "over-forking is recoverable" sound?
- [ ] Does executor-owned `lineage` outside `facts` fully preserve the skills-never-write-state
      boundary, given skills receive `state_facts` read-only at `executor.py:169`?
- [ ] Is advance/fork derivable from `facts_patch` alone at commit time for every current skill, or
      does any skill mutate the matrix without patching an identity?
- [ ] Does event replay reconstruct the index for the existing CRC session without ambiguity?
- [ ] Do D3 and D4 together leave the resolution sweep expressible without extra ceremony?
- [ ] Is there a failure mode where silent head resolution produces a *scientifically* wrong result
      rather than a merely surprising one?
- [ ] Does anything here conflict with the P0 items in `docs/current-state.md`?
