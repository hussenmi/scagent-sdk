# Current project state

Status date: 2026-08-04
Authority: this is the concise source of truth for what exists now, what was actually verified,
and what should happen next. Historical detail remains in `docs/handoff.md`.

## On-request package installs land in a per-user overlay, never the locked base (2026-08-04)

Users occasionally need a package the base environment does not ship. Legacy `scagent` handled this
with an `install_package` tool that `pip install`ed into `sys.executable` — the shared agent
interpreter — which in its own deployed module (a read-only `chmod 555` pixi env) would have failed
or silently mutated every user's environment. That approach is doubly wrong here: locks are
authoritative and their hashes are fingerprinted into every result, and deployment is again a shared
read-only base.

P0 of ADR 0010 is implemented **and live-validated** on branch `feat/on-request-package-installs`
(design discussion and full rationale in `docs/decisions/0010-on-request-package-installs.md`). A new
`manage-environment` skill adds a package into a **persistent per-user, per-`(runtime, base)`
overlay** under `/usersoftware/peerd/$USER`, layered on the immutable runtime via `PYTHONPATH` — the
shared prefix is never written. The tools (`propose_package_install` → `install_proposed_package`,
plus list/remove) run on a no-GPU `overlay-admin` view of `rapids-main`, so `sys.executable` is the
runtime interpreter and the resolver runs against the real base. The gate is **uv-first with a pip
fallback** (`resolve_package`/`materialize`): uv is pixi's own PyPI resolver, so it reads the conda
env correctly and is ~5–15× faster; both paths admit only **additive** packages (a resolved closure
that changes no base version), so nothing on the path can shadow the RAPIDS stack, and
conflict/conda-only/unavailable are refused with a reason. The install is gated a second time by
explicit user approval, verified against the event log via the same `user_turns` consultation
pattern the batch decision uses (approval must be the first user turn after the proposal). The broker
exports `SCAGENT_OVERLAY_DIR`, injects a non-empty overlay onto `PYTHONPATH` (never during the health
probe), and records the active overlay set in every runtime result's provenance; a new
`environment_overlay` session fact (in `FACT_ROOT_SCOPES`) records what was added and why. Removal
rebuilds the overlay from the remaining pins (a `--target` layout has no uninstaller and RECORD
deletion leaves an importable `__pycache__` namespace remnant). A package installed once persists
across exit, resume, and new sessions, while every result stays reproducible and explainable.

**Live-validated** on the real rapids conda env (Python 3.14.6): uv classifies `gseapy` as additive
in ~1s excluding base numpy/pandas/scipy, flags an older `requests`/`click` pin as a conflict,
reports a nonexistent package as unavailable, installs into the overlay and imports via `PYTHONPATH`,
and removes cleanly while a second overlay package survives — the pip fallback path passes the same
end-to-end flow. Two bugs were caught by that testing and fixed: a stray `--ignore-installed` that
made the pip dry-run re-resolve the whole base closure (slow + wrong), and RECORD-based removal
leaving a namespace remnant (now rebuild-based). Deterministic checks green: 27 new unit tests, full
suite green, ruff + mypy clean, `capability validate` passes (25 skills, 58 tools).

Governance is **decided: open** — any PyPI package, gated only by the additive check and explicit
user approval; no allowlist. The intended posture is that the base environment carries the common
single-cell stack so on-request installs stay the exception, with the overlay as the escape hatch.

Acting on that posture, the `rapids` base was audited and grown (2026-08-04): **`mygene==3.2.2`**
(activates `inspect-dataset`'s previously-inert online gene-id conversion), **`harmonypy==0.0.10`**
(Harmony integration via `scanpy.external.pp.harmony_integrate`), and **`gseapy==1.3.1`** (GSEA)
added to `pixi.toml`/`pixi.lock`; the re-lock added only those three + their small deps with zero
churn to the rest of the env, and the rapids `check` task passes (scanpy 1.12.1, scvi 1.4.3, torch
2.12.1+cu130, all new packages import). `scrublet` was intentionally not added (scanpy has it
built in). Note: `harmonypy` was first pinned to its latest `2.0.0`, which resolves additive but is
a C++ rewrite that fails to build on py3.14 — the pure-Python `0.0.10` scanpy uses installs cleanly;
**additive resolution ≠ a successful build.** Still open technically: P1 constrained-retry +
`doctor` drift/graduation, P2 isolated sidecar runtimes for incompatible packages.

## The batch decision survives the integration it authorizes (2026-08-03)

An integrated run stalled at `finalize_analysis`: the `batch_decision` floor reported "No explicit
batch-handling decision is recorded against current batch evidence for the active cells, counts,
representation, and clustering," and the model — following the floor's own remediation string —
rationalized its way into re-running `investigate_batch` on the *corrected* data at the very last
step. That is the implicit-DAG smell: a settled question re-asked at finalize, where re-measuring
residual batch structure on the integrated representation makes no scientific sense.

Root cause: `batch_decision` and `integration_authorized` both bound the *decision* to all four
analysis identities (cells, counts, representation, clustering) via `_batch_identities_current` —
the same binding used for batch *evidence*. But integration deliberately produces a new
representation and a new clustering, so honoring an `integrate` decision instantly invalidated the
decision that authorized it. The failure only surfaced at finalize, not when it was caused.

Fix (`floors/evaluator.py`): the decision now uses `_batch_decision_current`, bound only to
`cell_set` + `count_representation` — the substrate the user actually chose how to handle.
Representation/clustering changes (integration, reclustering) no longer stale the decision;
removing cells (cluster cleanup) still does, correctly. Batch *evidence* keeps its full four-way
binding and re-measures on any change. This makes the floor match the contract already written in
`batch-investigation/SKILL.md` ("the user's recorded choice is what persists across that"); no
skill or prose change was needed. Two floor tests were rewritten to assert the decision and the
integration authorization survive the integration/reclustering they authorize while still staling
on cell removal. Full suite green; ruff + mypy clean on the touched files.

## Gene filtering carries per-cell evidence forward; doublets ordered before clustering (2026-08-04)

A live run (`run_20260804T153724Z_bde07f`, Reyfman, 43,632 cells) exposed a defect the 0.5.0 gene
filter created: after `filter_single_cell_genes` removed ~18k genes, the `current_cell_qc_review`
floor went stale and the model — told by our own SKILL.md **not** to recompute QC after ribosomal
removal (it would read a false ~0% ribosomal) — had no legal way to refresh it and just "moved on."
The floor and the guidance pointed in opposite directions. Root cause: gene filtering nulled
`cell_qc` and minted a new `count_representation_id`, but the `current_cell_qc_review` floor keys
freshness on that id (`evaluator.py:103,108`). Cell QC is a decision about **cells**; gene filtering
removes genes, not cells, so invalidating it was wrong — and diverged from legacy and standard
scanpy, which never re-review cell QC after gene filtering.

Fix: `filter_single_cell_genes` now **carries the cell-QC review and complete doublet evidence
forward**, re-stamped to the new count representation and marked `carried_through: ["gene_filter"]`,
instead of nulling them. The floor stays strict (a genuine re-materialization still would not
carry): the carry only fires when the evidence is about the same `cell_set_id` and was current for
the input artifact. Everything gene-dependent — representation, clustering, cluster QC, annotation,
batch — is still correctly invalidated. The same reasoning covers doublets (Scrublet scores are
per-cell), which the `current_doublet_evidence` floor also keys on count representation
(`evaluator.py:81`).

The same run showed doublets running **late** — `evaluate_doublet_evidence` at step 39, after a full
clustering + cluster-QC pass, forcing a re-cluster. Legacy runs Scrublet early (Step 2 of
`run_qc_pipeline`, before clustering), because cluster QC consumes the `predicted_doublet` calls
(`cluster-qc/scripts/evaluate.py:477`). Guidance now mirrors legacy: `base.md`, the orchestration
SKILL/reference, and `doublet-evidence/SKILL.md` place doublet evidence after cell QC and **before
the first clustering**, and state that gene filtering carries QC/doublets forward while only *cell*
removal invalidates them.

Verified: 12 new deterministic carry-forward tests (re-stamp at both evidence and review levels,
same-cell guard, stale-for-input guard, no-mutation, doublets) on top of the existing gene-filter
suite, plus two live checks in the real `rapids` runtime — the gene filter itself, and a
carry-forward integration check confirming `filter_single_cell_genes` reads `state_facts` and emits
a re-stamped `cell_qc` + `doublets` facts_patch that the floor accepts. Full suite, Ruff, strict
mypy on the script, and `capability validate` pass. Not done: no full model-behavior session has yet
confirmed the model now runs doublets before clustering and does not re-review QC after gene
filtering — the guidance change is the lever, not proof.

## Gene filtering matches legacy science: dataset-size threshold plus ribosomal removal (2026-08-03)

`filter_single_cell_genes` removed only genes detected in fewer than a flat `min_cells=3` and did
nothing about ribosomal genes — well below what legacy `scagent` actually does. Legacy's gene
filter is not in its low-level `filter_genes` (default `min_cells=None`); it lives in
`normalize_and_hvg` (`agent/tools.py:9965`) as a guaranteed pre-HVG filter with two parts: a
**dataset-size-aware** low-detection threshold — keep genes detected in at least 2% of cells,
`ceil`-rounded and **capped at 100 absolute** so a large dataset does not delete rare-cell-type
markers — and **ribosomal-gene removal by default** (RPS/RPL and the mitochondrial-ribosomal
MRPS/MRPL) so they cannot dominate variance and drive the embedding. Mitochondrial genes are kept;
MT content stays a cell-level concern.

`single-cell-qc` 0.5.0 brings `filter_single_cell_genes` to that behavior, keeping it an explicit,
`confirm_filtering`-gated mutation rather than legacy's automatic bundling into normalization (QC
calculation stays flag-only, nothing removed):

- The low-detection threshold is now `min(ceil(min_cell_fraction × n_cells), max_cells)` with
  defaults `0.02` and `100` — the flat 3 is gone. `min_cells` is an exact absolute override;
  `min_cell_fraction=0` disables the filter for strict source replication. `low_detection_threshold`
  is a pure helper, unit-tested across the fraction, cap, override, and disabled branches.
- `remove_ribosomal` (default true) drops RPS/RPL/MRPS/MRPL, matched case-insensitively on the
  resolved gene symbols so Ensembl-indexed matrices still match. `remove_mitochondrial` (default
  false) exists for completeness; the default keeps MT genes.
- **The ribosomal figures do not break.** `pct_counts_mt`/`pct_counts_ribo` are computed on the full
  matrix before any removal and preserved in `obs` (a var-subset keeps all obs), so the QC,
  embedding, and cluster-QC ribosomal panels — which already prefer the recorded obs column over
  recomputing from genes, since the 2026-07-28 figure pass — keep reading the correct value. The
  one hazard is re-running `calculate_single_cell_qc` on the ribosomal-filtered artifact, which
  would recompute `pct_counts_ribo` to ~0; the SKILL.md now says not to, and names the preserved obs
  column as authoritative.

Verified: 22 new deterministic tests (`tests/unit/single_cell_qc_gene_filter_test.py`) covering the
threshold scaling/cap/override/disable branches and the ribo/mito symbol classification, plus a live
end-to-end run in the real `rapids` runtime on a synthetic 200-cell matrix — threshold resolved to 4
(2% of 200), 2 low-detection and 4 ribosomal genes removed including MRPS/MRPL, MT and normal genes
kept, and the preserved median `pct_counts_ribo` reading 20.7% *after* the ribosomal genes were
gone. Full suite, Ruff, and `capability validate` pass. Not done: no full model-behavior session has
exercised whether the model runs this at the right point (post-QC, pre-representation), and HVG-time
ribosomal *exclusion* (legacy's alternative when `remove_ribosomal=false`) is not ported — this
removes from the matrix rather than excluding at HVG.

## The durable record is queryable, so answers stop coming from memory (2026-08-03)

When a user asked what a step used or whether something was done, the model had no first-class way to
consult the authoritative record mid-conversation. The full durable view is injected only at the
first turn or a non-exact resume; on ordinary turns the model leaned on conversation memory plus
tool results it had already seen. Its only reach into the authoritative files was to shell into them
ad hoc through `analysis-workspace` — an unstructured escape hatch that is also lineage-blind, so a
raw `grep` of `state.json` can return a parameter from a superseded sibling branch and present it as
current, which is exactly the divergence the executor-owned lineage forest exists to prevent.

`query-analysis-state` 0.1.0 makes the record a structured, always-available query surface instead
of a thing to grep. Three read-only control-plane tools, no floors, no mutation:

- `summarize_analysis_state` — dataset identity and working shape, the active version and how many
  alternatives exist, and every recorded fact area flattened to its scalar leaves (bounded, with an
  explicit truncation marker so a cut-short view is never mistaken for the whole state).
- `find_analysis_steps` — committed steps by tool name or free text (matched against arguments too),
  newest first by the `events.jsonl` commit order, each carrying its exact recorded `arguments` and
  annotated with lineage placement: a transforming step reports `on_active_line` precisely, a
  read-only step reports `null` rather than a fabricated currency claim.
- `list_recorded_decisions` — each recorded decision with its rationale and, when it cites a turn,
  the authorizing user prompt resolved from `context.user_turns`.

Lineage awareness is the reason this beats grep, and it is the design's whole point: the tools
resolve the active line of descent (`active_execution_id` → parent chain, cycle-guarded) and label
every step and version against it. The logic is pure stdlib over plain dicts — it reads the same
`state.json`/`events.jsonl`/lineage sources the runtime does — so it runs in the control plane with
no compute runtime and is unit-tested without a live session. `base.md` now lists the skill and
makes "read the durable record with `query-analysis-state`, do not reconstruct from memory" an
explicit norm in the durable-state grounding section.

Verified: 10 new deterministic tests in `tests/unit/query_analysis_state_test.py` covering bounded
flattening with the truncation marker, cycle-guarded ancestry, event-ordered history, the
active-line/sibling-branch/read-only labelling that distinguishes it from grep, tool/query/limit
filtering including matches inside recorded arguments, the summary shape, decision rationale and
authorizing-turn resolution, and the three entrypoints end to end against a fabricated session on
disk. Ruff, strict mypy on the script, and `scagent-sdk capability validate` (now 24 skills, 23
executable, 54 tools) pass. Not done: no model-behavior session has yet exercised whether the model
reaches for these tools instead of memory, and the tools deliberately do not re-derive floor status
(the runtime already states outstanding floors) — adding that would duplicate the fragile
floor-schema logic these skills are meant to avoid.

## Gates stopped being able to destroy finished work (2026-07-31)

Session `run_20260731T160516Z_074aa5` (Reyfman, 43,559 cells, three user turns: *analyze this*, *yes
integrate*, *can you finalize*) ran for two and a half hours across 44 committed capabilities and 25
failures, and never finalized. Reading the event log, almost none of the failures were scientific.
Four mechanisms accounted for them, and each is now fixed at the mechanism rather than by asking the
model to be more careful.

**A presentational limit could discard completed science.** `model_media` over 2 MiB raised
`CapabilityExecutionError` *after* the capability had computed and written everything. That killed
`plot_qc_embedding` three times in a row and `evaluate_cluster_qc` three times — and the model could
not fix it, because the figure's size is not something a tool argument controls. With cluster QC
unobtainable, `review_cluster_qc` was unreachable and finalization stayed blocked on the
`current_cluster_qc` floor forever. **This single limit is why the session could not finish.**
Transport ceilings now degrade: the executor downscales an oversized image (Pillow, longest edge
2000 → 1400 → 1000, committed artifact untouched at full resolution) and, if it still will not fit or
the total budget is spent, leaves it unattached and names it in the figure directive with an
instruction to open it via `inspect-media` and a warning not to re-run. Verified on a regenerated
figure of the same class: 4.19 MB / 3134 px → 1.15 MB / 1400 px, panels and tick labels legible.

**A recorded human answer could be invalidated by recomputation.** The user said "yes integrate" at
turn 46. The model then trained scVI, re-clustered, and re-ran `investigate_batch` because the batch
floor legitimately requires evidence for the current clustering — which moved the consultation anchor
past turn 46. `decide_batch_handling` then failed four times with *no user answer after the batch
evidence*, and no action available to the model could satisfy it; only a future user turn could.
Worse, when one eventually arrived, `max(candidates)` harvested "can you finalize" as the recorded
authorization to integrate. The anchor now belongs to the *question* (`consultation_question_id` over
the batch variable and its sample labels, deliberately excluding representation, clustering, and
counts), a recorded answer carries forward while the question is unchanged, and the *first* turn
after the question is taken as the answer with `turns_after_anchor` recording that an exchange
followed. A different batch variable or sample set is still a new question that must be asked.

**Bookkeeping gates failed on transcription.** `review_single_cell_qc` demanded that six to seven
90-character artifact paths be echoed back exactly; it failed twice in a row, twice, and the review
that finally landed was written by copying paths out of the error text. Both QC review gates now
match on file name, path suffix, or full path, ignore extra names, and record the canonical paths in
the fact rather than the shorthand. The requirement is still that the figures were looked at.

**Floors were discovered only on denial.** `finalize_analysis` reported four unmet predicates at
once, one of them (`current_cell_qc_review`) stale since a filtering step near the beginning. Floors
are state predicates and there is more than one legitimate order in which to satisfy them, so the
unmet set is now stated in the system prompt at bind time — explicitly as predicates to satisfy by
any route, not a checklist — and each committed step reports, via PostToolUse `additionalContext`,
only what it *newly* made outstanding or satisfied. Silence when nothing changed is deliberate.

Also: the untracked-matrix error now names `adopt_untracked=true` instead of saying "adopt the matrix
through a transforming tool", and `methods_reviewed` documents that `markers` is required instead of
only rejecting its absence.

Verified: 4 new media/transport tests including the oversized-figure regression, 5 new batch
consultation tests including the exact deadlock and the harvested-wrong-turn case, 3 new
floor-visibility tests, 1 new assembly test proving the prompt block and the per-step delta; the
synthetic batch acceptance gained `B_answer_survives_reinvestigation` and
`B_a_different_question_still_needs_asking` and passes in the real compute environment, as does the
cluster-cleanup synthetic. Full suite, Ruff, strict mypy, and capability validation pass. `mypy`'s
`python_version` was corrected from 3.10 to 3.12 (the control plane's actual version) because the
installed numpy stubs use PEP 695 syntax, which made `mypy src/scagent_sdk` fail on any cold cache
for reasons unrelated to this package.

Not fixed: the model still wrote custom `run_analysis_code` that guessed column names in its own DEG
tables (`KeyError: 'group1'`, `'p_val_adj'`) — a discoverability problem for tool-produced table
schemas, and the largest remaining source of failures in that log. The ancestor-path guard still
fails rather than resolving (correctly, for transforming tools), and no model-behavior eval yet
covers any of this.

## scVI trains to a cell-count epoch cap and reports its own convergence (2026-07-31)

`train_scvi_latent` had a fixed `max_epochs` default of 200 and, although it passed
`early_stopping=True`, it reported nothing about how training ended: no epochs actually trained, no
loss curve, and a history CSV that saved only the first of scVI's history frames. A 200-epoch cap is
wrong in both directions — too few for a small dataset, far more than a large one needs — and without
the ELBO curve neither the model nor the user could tell a converged run from a truncated one.

`scvi-integration` 0.5.0 makes the schedule cell-aware and the outcome inspectable:

- `max_epochs` has no default. Unset, the cap comes from scVI's own `get_max_epochs_heuristic`,
  which scales epochs *inversely* with cell count (`min(400, round(20000 / n_cells × 400))`), with
  the same formula reimplemented locally as a fallback if that helper moves. `epoch_cap_source`
  records which applied, so `400` is never mistaken for a default.
- `early_stopping` is an explicit input, defaulting to true. Training runs on `devices=1` because
  scvi-tools cannot early-stop under multi-GPU DDP; a multi-device run would silently train the full
  cap. That is the legacy multi-GPU caveat removed at the source rather than explained afterward.
- The result reports `epochs_trained`, `early_stopped`, `hit_cap`, final and best train/validation
  ELBO, `best_val_epoch`, a descriptive tail improvement, and an `overfitting_warning` when the
  validation ELBO rose more than 2% above its best value. A train/validation convergence figure is
  saved and attached as `model_media`, so the curve is reviewed from pixels, not from recall. The
  history CSV now merges every frame scVI logged, one row per epoch.
- One sentence (`epoch_narrative`) states the cell count, the resolved cap, that the heuristic chose
  it, and how training ended, in the result summary and the artifact README. Running the full cap
  *with* early stopping enabled is reported as a possible undertrained model; with early stopping
  off it is reported as expected.
- `epochs_trained` joins `max_epochs`, `early_stopping`, and the seed in the representation identity,
  since two runs that stopped at different epochs are different latent spaces.

Verified live in the real `rapids` runtime on 2,000 cells × 11,501 genes with a two-level
`sample_name` covariate: the heuristic set a 400-epoch cap, early stopping fired at 196 epochs (best
validation epoch 150), and the run produced a 197-row history CSV plus a convergence figure showing
both curves flattening with no overfitting flag. 13 new deterministic tests in
`tests/unit/scvi_training_schedule_test.py` cover the cap formula, the heuristic/fallback/explicit
resolution, early-stopped vs cap-bound runs, the overfitting threshold from both sides, empty
history, and the narrative wording; they import the skill script without scvi, torch, or pandas.
Full suite, Ruff, and capability validation pass.

Not established: whether these latent spaces are scientifically better than the fixed-200 ones on
any dataset, and no model-behavior eval yet checks that the epoch count is explained to the user in
these terms. Integration evaluation (`evaluate-integration`, like-for-like latent comparison and
biological conservation) remains unimplemented, so adoption evidence is still the weak link.

## Shell bundles are named for what the command did (2026-07-31)

The `shell/` projection named each bundle by re-slugifying the command text, which produced names
that reproduced the shell instead of describing the work:
`001-cd-scripts-grep-n-context-artifact-relat-fcd39b46` leads with a `cd` that accomplished nothing
and truncates the one word that mattered. A reader had to reconstruct the command to learn what the
run was for, which is precisely what the name should have saved them.

The label now describes the function performed. `output_view.py` lexes the command with `shlex`
(`punctuation_chars=True`, so one pass both tokenizes and cuts the command into its steps), drops
shell preamble (`cd`, `source`, `set`, …) and redirection plumbing, and describes the first working
step as a verb plus what it acted on, appending the final step when it differs. Absolute paths still
reduce to the basename the command acted on, option values are not mistaken for operands
(`head -n 5 notes.txt`), a pattern from `-e`/`-f` is recognized as the pattern, and stream-editor
program text is never used as a name. A command outside the verb table keeps its own name as the
verb rather than being given an invented description; unbalanced quotes fall back to the old
command-text slug. Names are cut at a word boundary within a 56-character budget, so a truncated
name still ends on a whole word.

The two bundles above became `001-search-evaluate-py-for-context-artifact-relative-path-fcd39b46`
and `002-copy-evaluate-py-then-edit-b1c71d3d`; other examples are `read-end-of-events-jsonl`
(`tail -5 …/events.jsonl`), `search-events-jsonl-for-finalize-then-count-lines` (a `grep | wc`
pipeline), `find-files-named-h5ad`, `run-pytest-output-view-test-py`, and `run-inline-python` for a
`python3 -c` heredoc whose source cannot be named.

Verified by refreshing the real session `run_20260731T033831Z_d1649c`: both bundles were renamed,
stale bundle directories pruned, every projected link still resolves, and the `outputs.md` shell
table and `outputs.json` `bundle_path` entries agree with the new names. Covered by 21 parametrized
labeler cases plus the existing bundle-shape tests in `tests/unit/output_view_test.py`; full suite,
Ruff, strict mypy, and capability validation pass.

## Cluster-QC figures identify themselves without interpreting themselves (2026-07-31)

The covariance heatmaps had accumulated too much text, and the worst of it was the verdict: the
title ended `→ covariance: moderate`. That is the conclusion the figure is *evidence for*, so
printing it on the image lets a review agree with the label without ever reading the pattern —
which is exactly what the visual-review floor exists to prevent. Removed, along with the two-line
"how to read a correlation block" caption, the colorbar label, and the gene-count x-label.

The heatmaps now match the legacy figures: a two-line title carrying only what a reader opening the
file cold cannot reconstruct — `{cluster_key} / cluster {id}` then `{n} genes x {m} cells |
mean_abs_corr=0.121` — gene names when there are 60 or fewer, hidden spines, an unlabelled
colorbar, and a square sized from the gene count. The degenerate case is still named honestly
(`mean_abs_corr=undefined (genes do not vary)`) because that is a fact about the data rather than an
interpretation of it. The reading guidance moved into the folder README's `how_to_read` entry, where
it is stated once instead of burned into every image, and `cluster-qc/SKILL.md` now tells the model
to state the structure it sees rather than repeat `mean_abs_corr`.

The metric boxplot caption lost its redundant clause: `Metric-flagged clusters (red boxes and red
labels above): 1` is now `Metric-flagged clusters: 1`, and the no-flag case reads
`Metric-flagged clusters: none`.

Verified by rendering the heatmap at 150 genes with three injected co-expression modules, at 24
genes to confirm gene labels still appear, and the boxplot in both the flagged and unflagged cases,
then inspecting each. Full suite, Ruff, mypy, and capability validation pass.

## Heavy-tailed QC metrics are plotted as log values, not on log-scaled axes (2026-07-31)

Every figure that previously drew raw counts on a `set_xscale("log")`/`set_yscale("log")` axis now
plots the log10 of the metric on a linear axis. The distinction is not cosmetic: on a log-scaled
axis the numbers printed are not the numbers drawn, so a distribution's spread and symmetry cannot
be read off it, histogram bins have to be manufactured uniform-in-log to avoid a distorted low end,
and equal distances carry no fixed meaning. With the logarithm plotted, bins are uniform by
construction, a box's height is a spread in fold-change, equal distances are equal fold-changes, and
the axis reads in the same space library-size and gene-count cutoffs are chosen in.

Converted across all three skills that draw these metrics, so one convention holds everywhere:
`single-cell-qc` 0.3.0 (knee, library-size and gene histograms, violins, joint scatters),
`visualize-single-cell` 0.4.0 (the same set plus the per-group QC boxplots), and `cluster-qc` 0.7.0
(per-cluster metric boxplots). No `set_xscale("log")`/`set_yscale("log")` remains under
`.claude/skills`.

Two things keep the change from costing the reader anything. Each log10 axis carries a **raw
magnitude mirror** on the opposite spine — marks at 1/2/5 per decade, widening to a finer mantissa
set when the metric spans well under a decade, so a narrow range still gets a usable reference
rather than one mark. And zero-count cells, which have no logarithm, are dropped *and counted* in
the panel title rather than folded in by a log1p shift that would present them as ordinary small
values. `log1p_*` violin panels moved to log10 as well, so every logged panel reads in one unit: a
decade is a decade, where natural-log 8.5 has to be converted before it means anything.

The helper trio (`log10_positive`, `raw_reference_ticks`, `label_log10_axis`) is duplicated in each
skill rather than shared, because a skill's own `scripts/` directory is the only thing on its import
path and `scagent_sdk` is not importable from the compute environment. The copies cross-reference
each other and must not drift.

One latent defect surfaced and was fixed: `single-cell-qc` built its scatter panels from columns
filtered for finiteness *independently*, so a dataset with any zero-count cell produced `total` and
`pct_counts_mt` arrays of different lengths that no longer described the same cells. A new `paired`
helper masks the columns together. This was present before the change and only went unnoticed
because the tested datasets had no such cell.

Verified by rendering every affected figure through the real compute runtime on a synthetic dataset
carrying two populations of different depth plus 30 zero-count cells, and inspecting each: the
library-size histogram is now a symmetric bell in log space, the knee reads in decades with raw
counts mirrored, the per-cluster boxplot separates the two populations with raw counts mirrored on
the right, and the narrow-range genes panel still gets three raw marks. Full suite, Ruff, mypy, and
capability validation pass. Not verified: no model-behavior session has read the new figures.

## A committed session is legible without the conversation that produced it (2026-07-30)

Four presentation defects were fixed together because they share one cause: the review surface was
naming and describing artifacts from what the runtime could infer, rather than from what the
producing skill knew.

**Projections are named for content, not for the tool.** `_projected_name` now renders
`<artifact>--<execution-short-id>`; the tool name is gone from the filename (it is still in state
and `outputs.md`). `ProducedArtifact` gained an optional `title`, and the projection prefers it over
the artifact `name`. That separation is the point: `name` is the stable identifier floors, report
selection, and figure requirements match on and must not absorb a subject, while `title` says which
embedding, clustering, or sample this particular file shows. So
`embedding--plot-embedding--a8c2f0d0.png` is now `umap-by-leiden-int-sample--a8c2f0d0.png` with the
artifact still named `embedding`. `visualize-single-cell` 0.4.0 titles its embedding figures and
grids from the basis plus the coloured columns, truncating long panel sets with a count.
`finalize-analysis` figure selection was changed to match artifact names only — it had been matching
projected paths, which coupled report contents to a filename convention.

**Skills can ship a plain-language README.** A new `documentation` field on the result envelope
(`src/scagent_sdk/capabilities/documentation.py`) declares title, overview, what was done, what was
found on this dataset, a per-file guide with column glossaries, settings, and caveats. The executor
renders it to `README.md` inside the folder it describes and registers it as an ordinary artifact,
so it is committed, hashed, retained, and projected like the figures it explains. Structure is the
runtime's; content is the skill's, and findings must be dataset-specific. Rolled out to `cluster-qc`
0.7.0 only for now — the mechanism is general, the remaining skills are not yet converted, and
`batch-investigation` still hand-writes its own README rather than declaring one.

**Cluster-QC figures identify themselves.** The metric boxplot now names its metric-flagged clusters
in bold text beneath the axes and reddens their tick labels, instead of relying on box colour alone.
Each covariance heatmap is captioned with its clustering, cluster, gene and cell counts, mean |r|,
the share of strongly correlated pairs, the resulting verdict, gene names when few enough to read, a
labelled colorbar, and a sentence naming the pattern to look for.

Two real defects surfaced while verifying this, both fixed:

- The covariance gene filter kept genes on `variance > 0`. A gene with variance 1e-30 passes that
  and then makes `np.corrcoef` divide by ~0, so its correlations return NaN; zeroing those NaNs
  drove mean |r| to exactly 0 and the cluster was called `unstructured` on undefined evidence. The
  filter now uses the legacy floor (`isfinite` and `> 1e-12`), and the heatmap detects a still-
  degenerate matrix and says the correlation is undefined rather than claiming independence.
- `scripts/validate_cluster_cleanup_synthetic.py` built its junk cluster with `rng.poisson(20)` and
  no `size=`, so one scalar draw broadcast across all 150 cells and every gene was constant within
  the cluster. The acceptance passed its `junk_cluster_confirmed` check while testing nothing about
  covariance. With `size=n_junk` the junk cluster now carries genuinely independent noise
  (mean |r| = 0.064, 28 genes) and is still correctly confirmed and removed.

Verified with the full test suite, repository-wide Ruff and mypy, capability validation, the
synthetic convergent-junk acceptance through the real compute runtime (all 13 checks pass), visual
inspection of the regenerated boxplot and both covariance heatmaps, a README rendered from that
run's real evidence JSON, and an end-to-end executor run confirming the README is committed as an
artifact and projected into `reports/`. Not verified: no model-behavior session has exercised the
new figures or README, and `evaluate_cluster_qc` through the brokered live path is currently blocked
by a **separate pre-existing defect** — when the input already carries a `rank_genes_groups` for the
same cluster key, `evaluate.py:476` reuses it and `sc.get.rank_genes_groups_df` raises
`AttributeError: 'dict' object has no attribute 'dtype'`. That reuse branch trusts a stored shape
scanpy's reader cannot parse. It is untouched by this work and is the next thing to fix here.

## Batch investigation is gene-first without becoming a workflow DAG (2026-07-30)

`batch-investigation` 0.8.0 keeps the matched-population, gene-first science and concrete
deliverables while making the interaction guidance-led. When multiple samples are found, the model
offers investigate / integrate / keep combined uncorrected / analyze separately / describe the
experiment. Investigation is an uncorrected first pass followed by the diagnostic; afterward the
user chooses the action. Floors protect only the outcome: the choice is evidence-bound, stale after
identity changes, and every substantive post-investigation decision cites a real user turn after the
evidence. The decision tool finds and records the newest post-evidence user answer itself, so the
model never searches event logs for an opaque turn id or paraphrases the answer.

The capability writes the five-file evidence packet:

- `batch_diagnostic_sample_enriched_regions.csv`
- `batch_diagnostic_within_sample_degs.csv`
- `batch_diagnostic_population_pairs.csv`
- `batch_diagnostic_direct_pair_degs.csv`
- `batch_diagnostic_design_check.csv`

It also writes a plain-language `README.md`, the SDK report/JSON, detailed native tables, and the
composition figure. The scientific hierarchy is gene-first: within-sample one-vs-rest DEGs are
primary; mean-expression nomination plus identity-gene overlap confirms a small number of
cross-sample pairs; direct pair DEGs, recurrence, composition, mixing, and design checks are
supporting context. Similar identity signatures establish a shared population split by sample, not
technical causality. Experimental design and the user determine whether correction is appropriate.
The structured result and report now name each matched cluster/sample pair, the two within-sample
comparisons, shared top-25 genes, and direct differences. Runtime guidance explicitly forbids
calling donors technical replicates or claiming a proven technical batch without documentation.
Saved decision rationales are generated neutrally from the exact recorded user choice.

Pair work is compact: default three confirmed pairs, at most ten nominations attempted, with a 0.4
profile-correlation nomination floor. The prior union-find population expansion and up-to-60 direct
comparison default are no longer on the main path. The two-axis interpretation remains advisory
metadata; it neither authorizes nor blocks a choice. Recurrence is explicitly secondary.

Verified with 66 focused unit/contract tests, the full 715-test suite, repository-wide Ruff and
mypy, capability validation, the synthetic compute-runtime acceptance, and a real direct run on
the 43,559-cell Reyfman artifact. The real run
found 30 enriched regions and three confirmed matches and wrote the compact packet: 30 region
rows, 200 within-sample DEG rows, three pair rows, 150 direct-DEG rows, one explicit unknown-design
row, plus README/JSON/report. The matched pairs were coherent donor-specific macrophage regions
sharing 15–20 of their top-25 identity genes. No full model-behavior session has yet exercised the
new guidance and post-evidence conversation.

`scvi-integration` publishes an append-only progress stream. The terminal shows live
epoch/maximum, training ELBO when available, and the later latent-computation and save phases on the
same elapsed status line. A two-epoch run through the real isolated scVI environment delivered every
progress event from loading through model save.

## The batch decision now requires the user when the evidence cannot settle it (2026-07-30)

> Historical 0.6.0 account. The 0.7.0 section above supersedes its
> recommendation-dependent consultation and recurrence-priority conclusions.

Session `run_20260729T205323Z_2a1c40` (Reyfman lung, 8 donors, `iris-qwen36`) recorded
`keep_uncorrected` on evidence that said the opposite of settled:
`gene_evidence=recurring_sample_associated`, `design_interpretation=unknown`,
`recommendation=cannot_determine_technical_vs_biological`, Cramér's V 0.906, same-donor neighbor
fraction 0.887 against 0.130 expected from composition. The donor highlight grid is textbook
unintegrated structure — AT2 (24,301 cells) smeared across ~8 disconnected islands, alveolar
macrophages across 4. **The engine was right and the decision layer was not.**

Nothing was broken; nothing required asking. `orchestrate-single-cell/SKILL.md` said "record an
explicit decision", `request_guidance` was one of five peer enum values, and `validate_decision`
allowed every non-`integrate` decision unconditionally. The gating was asymmetric in exactly the
wrong direction: `integrate` needed a basis plus an override warning, while `keep_uncorrected` — the
option that abandons the question — needed nothing. Legacy `scagent` closed this with a mandatory
runtime selector (`agent/agent.py:1497`) fired twice, the second time re-opening the same
`multi_sample_strategy` decision after `diagnose_batch_effect`; its own code comment records what
happened when that checkpoint failed to fire: "the model hand-rolled the decision."

Verified first: legacy HEAD's engine reaches the **identical** verdict on this dataset
(`run_2026_07_19_212128/reports/conversation.md`), so this is not a science regression. The
"orig always said clear batch effect" memory is the *pre-two-axis* generation, whose
`batch_diagnosis.json` carried a single `verdict: batch_effect_supported`.

The obligation is expressed as a state predicate rather than a widget, so it holds however the
question reaches the user:

- **`CapabilityContext.user_turns`** — a read-only index of `{turn_id, sequence, user_prompt}` built
  by `SessionStore.user_turns()`, which streams `events.jsonl` and parses only turn-start lines
  (the scientific payloads dominate its bytes; a full parse per execution is not affordable). The
  runtime states who spoke and when; **no consultation policy lives in the runtime package.**
- **`validate_consultation`** (`batch-investigation` 0.6.0) — when the recommendation is
  `cannot_determine_technical_vs_biological` or `integration_optional_for_confirmed_replicates`,
  `keep_uncorrected`/`integrate`/`separate` require `user_consultation` (`turn_id`,
  `question_asked`, `user_answer`). It is checked, not trusted: the turn must exist in the event log
  **and postdate the evidence**, so the opening "analyze this dataset" prompt cannot be cited as
  authorization — and neither can the turn the evidence itself was produced in, which is precisely
  what the bad session would have tried.
- **`request_guidance` is pending, not terminal** — the `batch_decision` floor now fails on it with
  a distinct reason. Previously it satisfied the floor, so "I need to ask the user" was the cheapest
  route to finalization. The loop closes itself: ask → user answers → record with attestation →
  floor passes; no answer means finalization stays blocked and the floor re-prompts.
- **Design collection moved before the investigation.** `design_interpretation=unknown` in that
  session existed only because nobody told the agent what the 8 donors were, and an `unknown` design
  caps the verdict at "cannot determine" regardless of gene evidence. Asking for `condition_keys` and
  a real `technical_batch_basis` is now part of the method in `orchestrate-single-cell` and
  `batch-investigation`. This one is guidance, deliberately: pre-investigation there is no evidence
  to write a predicate against, and hard-blocking every multi-sample run on a user turn would break
  non-interactive and eval runs.

Evidence schema 1 → 2 (carries the anchor) and decision policy 1 → 2, both fail-closed: existing
sessions must re-run `investigate_batch` before a decision binds. The anchor is deliberately
**excluded from every `evidence_id`** — it records when evidence was made, not what it found, so
identical science in a different turn keeps its identity and does not stale a prior decision.

Verified live, two ways. **In the `rapids` runtime against the real entrypoints:** evidence carries
schema 2 and the anchor (0 with no turns, 17 with), the `not_applicable` `evidence_id` is unchanged
by turn history, an unconsulted `keep_uncorrected` is refused, citing the anchor turn itself is
refused, `request_guidance` records and reports finalization as blocked, and a consultation on a
later turn is accepted and persisted in full. **Brokered end to end** via
`scripts/validate_p0_live.py` on `combined_truly_raw_annotated.h5ad` (36,419 cells, 7 samples):
`anchor_sequence=22` proves the turn index survives the broker → worker JSON boundary and is read
*inside* the isolated compute environment; `request_guidance` records and leaves
`batch_decision` failing with the pending reason; the consulted decision is accepted; the floor
passes after the real answer and still stales after a forged reclustering. Honest gap in that run:
this dataset's recommendation was `do_not_integrate_based_on_current_evidence`, so the brokered path
did **not** exercise the refusal branch — that is covered by the in-runtime check and the
deterministic tests, not brokered.

Deterministic coverage: 20 new tests across `batch_capability_test.py`, `floor_evaluator_test.py`,
`session_test.py`, `capability_executor_test.py`, plus two new contract tests, both negative-checked
by breaking them deliberately:
`tests/contracts/capability_context_boundary_test.py` pins every `CapabilityContext` field to both
halves of the broker/worker boundary (add a field to one side and compute capabilities silently get
the dataclass default), and `tests/contracts/evidence_schema_agreement_test.py` pins the
evidence-schema and policy constants that are necessarily duplicated between skill scripts and
`floors/evaluator.py` (disagreement makes a floor reject every piece of evidence its skill can
produce, with a staleness message no rerun can fix).

Fixed in passing, pre-existing and unrelated: `scripts/live_fixture_pipeline.py` hardcoded
`cluster_key: "leiden"`, which the non-overwrite guard correctly refuses on already-annotated
validation inputs — the P0 live baseline could not run at all. Now `leiden_res_0_8`, with
`validate_p0_live.py` passing that key to `investigate_batch` explicitly so it cannot silently
investigate the input's legacy clustering instead of the one the fixture just built.

**Known limit, not closed:** a faithful `turn_id` paired with a misreported `user_answer` passes.
Recording the user's own words is the mitigation. A runtime selector would remove the gap entirely
by capturing the choice directly, and is the natural next step — it would write the same
`user_consultation`, so nothing downstream changes. It was deliberately not built first: without the
attestation the model would skip a selector exactly as it skipped `request_guidance`, and a selector
means new interactive-loop rendering, a `user.decision` event kind, and resume semantics for a
session interrupted mid-question. Build it if measurement shows the local Qwen profile stalls often.

**Still open from the same review (P1, unfixed):** the `gene_evidence` recurrence axis is
**unstable in both directions**, because its magnitude tracks how many region pairs the clustering
happened to surface rather than anything about the biology. Reyfman (30 regions, 91 matches, 3
populations) produced **19,883** recurring `(gene, donor)` entries over 7,221 unique genes of ~20k
from 159,097 direct rows — saturated. The 7-sample validation dataset (9 regions, 6 matches) produced
**0**, landing on `localized`. Two porting gaps: (1) legacy caps the recurrence accumulator at
`top_n=25` genes per side per pair, while `summarize_recurrence` consumes every significant row
uncapped (`investigate.py:786-790` records that the detection-fraction gate was dropped
deliberately); (2) legacy keys recurrence on the distinct **cluster** and unions a second
within-cluster cross-batch **region scan** added expressly to stop the verdict flapping between
models, while the SDK keys on the union-find **component** with only the pair geometry — the exact
dependence legacy's region scan exists to remove. So the Reyfman verdict was right by accident, and a
sparser dataset can miss a real recurring program entirely. Fixing this changes how often
`cannot_determine` — and therefore the new consultation gate — fires at all.

## `materialize_count_matrix` cost halved (2026-07-29)

The step felt slow because it was, and almost none of it was science. Profiled on
`combined_truly_raw_annotated.h5ad` (36,419 cells, 100M stored values): read 10.8 s, the
count-source validation scan 2.1 s, matrix copies 1.0 s, full-matrix sha256 0.6 s — and
`write_h5ad` **82.7 s, 85% of the total**. Two compounding causes, both fixed:

- **Duplicated payload.** The artifact carried the counts in `X` *and* `layers["counts"]`, plus the
  original source layer and `.raw` — four copies of the same values, making the count-ready
  artifact (0.96 GB) *larger than its 0.73 GB input*. `_redundant_payload` now drops only what the
  tool itself duplicated: `.raw`, and the source layer when a layer was selected. Unrelated layers
  (spliced/unspliced, velocity) survive, and the input is immutable and recorded in
  `dataset_revision.source_path`. `doublet-evidence` (`doublets.py:656`) and `cluster-qc`
  (`evaluate.py:1459`) already null `.raw` this way. `details.dropped_payload` reports it.
- **h5py's default gzip level 4.** A level sweep on the identical trimmed payload: lzf 34.6 s /
  1.09 GB, gzip-1 41.8 s / 0.52 GB, gzip-2 37.7 s / 0.51 GB, gzip-4 55.4 s / 0.50 GB. Level 2 costs
  ~2% size for ~32% less time and matches lzf's speed at half its size. lzf was rejected despite
  being fastest: it is an h5py filter, not part of every HDF5 distribution, so R readers may not
  open the artifact. The third redundant matrix copy was also removed (three copies → two).

Measured A/B, old and new alternating in one process over three reps to control filesystem drift:
**100.2 s → 52.9 s (1.89×) and 0.96 GB → 0.51 GB (1.87×)**, with the new path varying by under a
second across reps. Verified against the real entrypoint: `X` and `layers["counts"]` both equal the
selected source and are integer-valued, `.raw` and the source layer are gone, all 24 obs and 14 var
columns survive, and `uns` identities match the emitted facts.

Not addressed: **16 other `write_h5ad` sites still use default gzip** (`expression-preprocessing`,
`dimensionality-reduction`, `single-cell-clustering`, `cluster-qc`, `doublet-evidence`,
`scvi-integration`, the annotation skills). The same level-2 change applies to each and is the
obvious next efficiency pass. `finalize-analysis` should deliberately keep the default for the
published artifact, where interop matters more than speed.

## Clustering resolutions are now an iterative ladder, not a parallel sweep (2026-07-29)

The comprehensive-analysis pass had turned the 2.0/1.5/1.0 resolutions into a **beauty contest**:
three clusterings of the same cells on the same embedding, written to distinct `obs` keys, judged
side by side, one selected. Legacy `scagent` never did that. There the resolutions are three
**phases with different jobs**, and each runs on different data than the last:

- 2.0 is the first clustering of a run and exists to expose small low-quality populations while
  they are still separable (`agent/prompts.py:220`);
- confirmed junk is removed, and the cleaned cells are re-embedded — including a **fresh HVG
  selection**, because subsetting cells changes the variance landscape (`agent/prompts.py:203`);
- 1.5 is the working granularity on that new embedding, repeating until nothing is flagged
  (`agent/prompts.py:221`);
- 1.0 is the annotation granularity that `prepare_annotation` binds to (`agent/prompts.py:222`).

No number of distinct `obs` keys on one unchanged object can reproduce that, because the cleanup
and re-embedding between rounds are the point. The corrective change is **guidance only**, by
explicit decision: the ladder is described as a loop in `orchestrate-single-cell/SKILL.md`,
`orchestrate-single-cell/references/workflow-decisions.md`, and `cluster-qc/SKILL.md`, with 1.0 as
the default annotation resolution overridable for a stated scientific reason. Observed model
behavior already follows 2.0 → 1.5 → 1.0, so legacy's harness-level refusal of off-ladder and
upward resolutions (`agent/tools.py:5114-5175`) was deliberately **not** ported; if drift appears,
that enforcement plus durable ladder history in facts is the next step.

The mutation machinery required no change and already forces the loop: applying a removal through
`evaluate_cluster_qc(auto_remove_convergent=true)` mints fresh dataset/cell-set/count identities and
nulls `representation`, `clustering`, `cell_qc`, and `doublets`
(`cluster-qc/scripts/evaluate.py:1368-1417`), so the next round cannot begin on a stale embedding.
That consequence is now stated in the guidance instead of being left for the model to discover from
a floor failure.

Validation level: deterministic tests and capability validation only. This is a model-instruction
change, so it is **not** evidence that the model executes the loop correctly; that needs a live
end-to-end run.

## Figure inspection: adopted the legacy immediate-interpretation model (2026-07-28)

Figures were being generated, passed over, and attested to much later when a review floor blocked
the turn — a review written from recall rather than from pixels. Two causes, both fixed by adopting
what legacy `scagent` did:

- **Nothing obliged the model to read an attached figure.** `model_media` already arrives as inline
  base64 in the same tool result (`executor.py:_model_content`), so the pixels were never the
  problem; the model was simply free to chain into the next tool without comment. Legacy appended
  the figures as their own **user turn** carrying an interpretive prompt (`agent/agent.py:4869`,
  `_build_image_message`), which had to be answered before anything else. `CapabilityExecutor` now
  appends an equivalent directive as the **last content block** of any result carrying media: name
  what each figure shows, what it implies, what needs acting on, before the next tool call, and
  report legibility failures instead of falling back to the table. It is deliberately generic —
  what a figure *means* stays in the skill; the runtime package holds no biology. This covers every
  skill, including `plot_embedding`, markers, and composition, which previously had no figure
  enforcement of any kind.
- **`MODEL_MEDIA_LIMIT = 8` made the cluster-QC floor unsatisfiable.** `required_visual_artifacts`
  lists every per-cluster correlation heatmap (default `max_heatmaps: null`; 20–28 typical), but
  only 3 overview figures + 5 heatmaps were attached, so ~15–23 required figures were never shown
  and had to be reopened one at a time. Legacy had **no cap at all**: it auto-encoded every
  `artifacts_created` entry whose path ended in `.png` and injected all of them. The cap dates to
  the initial commit with a rationale covering only the byte budgets, not the count. Raised to 64;
  `cluster-qc` now attaches its whole heatmap set.

Measured, not assumed: the legacy 28-heatmap set is 2.8 MiB raw / ~3.8 MiB base64 — already inside
our existing 8 MiB `MODEL_MEDIA_TOTAL_BYTES`. Our own heatmaps are 22–33 KiB at 691x608 (a third of
legacy's 1197px), so a 60-cluster set is ~2 MiB / ~34k image tokens. **Worst-case transport is
unchanged** by the count increase: the payload ceiling was and remains `min(count x 2 MiB, 8 MiB)`
= 8 MiB, so the byte budget was always the binding constraint. Beyond 64 figures `cluster-qc`
degrades visibly — it names the un-attached heatmaps in the summary and `details.figures_not_attached`
and tells the model to open them with `inspect-media`, rather than failing an expensive pass.

Review schemas were deliberately **left alone**. Legacy carried no per-figure attestation and
worked; the fix targeted immediacy, not stricter ceremony. `visual_findings` remains a flat list,
so a terse attestation is still structurally possible — revisit only if immediacy proves
insufficient in practice.

## Figure comprehensiveness pass (implemented 2026-07-28)

Comparison against legacy run `/data1/peerd/ibrahih3/cs_agent/run_2026_07_22_115023` found the SDK
figure set at or below legacy coverage in three places, against a product goal of exceeding it.
Fixed across `visualize-single-cell` (0.3.0), `single-cell-qc`, and `cluster-qc`:

- **Log scaling.** Library size and detected genes are now drawn on log axes with **log-spaced bin
  edges**. Linear bins under `set_xscale("log")` — what both the legacy run and the SDK did —
  render the leftmost bar hundreds of times wider than the rightmost and compress the low tail into
  two or three blocks. Log tick labels use a 1/2/5-per-decade locator with compact text (`2k`,
  `50k`); the default mantissa labels collided into an unreadable smear in the legacy figures.
  Per-group and per-cluster boxplots are log-scaled on the same two metrics.
- **Violins.** Replaced the bare `violinplot` with the legacy shape: violin body plus the per-cell
  points jittered over it (legacy used `sc.pl.violin(..., jitter=0.2, multi_panel=True)`), over
  `log1p_total_counts`, `log1p_n_genes_by_counts`, `pct_counts_mt`, `pct_counts_ribo`, with white
  IQR/median markers added. Jitter is capped at 20,000 deterministically sampled cells.
- **Coverage.** `plot_qc_distributions` now returns a *set* — knee, log-scaled size histograms,
  MT and ribosomal percent, jittered violins, joint scatters (genes-vs-counts log-log,
  MT-vs-counts, ribo-vs-MT), doublet violin+histogram when the artifact carries scores, and
  four-metric per-group boxplots. `single-cell-qc` gained the doublet figure and a ribosomal panel.
- **Highlight grids.** `plot_embedding`, `plot_qc_embedding`, and `cluster-qc` now emit a
  per-category grid alongside every categorical overlay — one panel per category, that category
  colored and the rest grey, count in the title, shared axis limits, colors matched to the overlay.
  This is the legacy `_generate_cluster_highlight_grid`, which the SDK lacked. Bounded to 2–150
  categories and 4 companion grids per call; skips are reported in `highlight_grids_skipped`.
- **Metric source.** MT/ribo percent now prefers a recorded `pct_counts_mt` / `pct_counts_ribo`
  over recomputing from the matrix, reporting which was used. Found live: recomputing on an
  annotated artifact whose RPS/RPL genes had been filtered out reported **0% ribosomal** where the
  recorded column held the correct 21%.
- **Display order.** Categories are selected by size but drawn in natural order (2 before 10), so a
  cluster can be found by name and two figures of one clustering do not shuffle when counts move.

Validation level: deterministic tests for every new pure helper, plus **live rendering** of all
figures against `run_2026_07_22_115023/machete_data_annotated.h5ad` (21,506 cells, 20 Leiden
clusters, 15 cell types) with each image visually inspected. Not yet exercised through a full
`scagent start` session, and not tested on a >150-category clustering or a >1M-cell dataset.

## Product direction

`scagent-sdk` is an independent, resumable single-cell RNA-seq agent built around four boundaries:

1. the model selects, interprets, and replans;
2. focused skills hold scientific guidance, deterministic scripts, references, schemas, and evals;
3. a durable scientific session owns facts, decisions, events, identities, lineage, and artifacts;
4. hooks enforce a small set of identity-aware scientific floors without encoding a workflow DAG.

The project intentionally does not inherit from or run legacy `scagent`. Scientific lessons are
reimplemented in focused packages and checked against legacy code/tests and BioNeMo contributed
skills only as read-only references.

## Artifact lineage: implemented (2026-07-30)

`docs/artifact-lineage-and-head-spec.md` (v2.2) is implemented across five staged commits. Read it
before touching the capability executor, session state, or any skill that writes an H5AD;
`docs/lineage-implementation-review.md` records what landed per stage, how each part was verified,
where the implementation departed from the spec, and what is still open.

| stage | commit | what landed |
|---|---|---|
| 1 | `64d8e33` | state contract: split schema versions, `SessionState.lineage`, identity allowlist, fact-scope registry, pure topology helpers |
| 2 | `df47cd4` | node creation at commit, parentage from the resolved input, fact routing by scope, optimistic head check, recovery quarantine |
| 3 | `e4457d4` | declared `primary_matrix_input`/`primary_matrix_output`, head-resolved omitted paths, refusal of superseded inputs |
| 4 | `95f9134` | `branch_from`, `analysis-versions` (list/switch), atomic checkout of head plus node facts |
| 5 | (this pass) | v1→v2 reducer, migration on open, reference-session reconstruction |

**The defect is closed.** Continuing from a superseded artifact is refused, and omitting the dataset
path — now the normal way to call a tool — makes the divergence unrepresentable. Verified live
against `sessions/crc_truth_labeled.h5ad` through the real broker at stages 3 and 4.

**Correctness hardening after implementation review.** The follow-up reviews found eight gaps and
they are now closed: untracked transforms require explicit `adopt_untracked`; the manifest loader
adds that executor-owned control to the model-visible schemas of all 20 transforming tools;
node-scoped facts from an untracked read-only input fail before staging instead of attaching to the
active dataset; the head compare, lineage patch, artifact move, and event append share the
session-store lock; `prepared_path` is derived from the node head and skills cannot write it;
`branch_from` accepts a version ID/prefix or artifact and injects its canonical absolute path; legacy
reconstruction uses a versioned tool/artifact-role registry, including CellBender `.h5` and
multi-H5AD executions; and moved sessions recover historical absolute inputs by their complete
executor-owned artifact tail. A degraded reconstruction preserves the legacy fact checkpoint rather
than shrinking it. Identity signatures derive from the parent-resolved node view, never from active
global facts.

**The bug was real and had already happened.** Reconstructing the nine sessions on disk found it in
`run_20260728T153108Z_de92e7`: `finalize_analysis` read `clustered.h5ad` rather than the annotated
output, so its `final-annotated.h5ad` carries neither annotator's per-cell columns. Nothing in the
old state recorded that; the forest makes it visible.

**Migration status.** All 11 sessions currently on disk reconstruct without failure. Rebuilding
each under a synthetic restored location produced the same parent map and resolved head facts as its
original location. Three predate argument recording, so no input path was ever stored and every
version reconstructs as a root — reported as a migration warning rather than implied; those degraded
migrations retain the legacy fact checkpoint. The full suite is green at 628 tests.

**Still deferred (D11):** pruning and column overlays. `reachable_from_heads` is the prerequisite and
exists; retention policy needs a branch-status vocabulary (`retained`/`pinned`/`rejected`) and is a
storage concern, not a correctness one. Sessions on disk total 26 GB with no retention policy.

The defect it addressed: session facts merge by identity in the executor and always converge, while
the H5AD chain merged only by whichever `path` the model passed. A column-adding capability could
therefore derive from a sibling artifact carrying identical identities — which **no floor can
detect** — leaving facts and reports correct but the delivered H5AD missing an `obs` column.

Working rules the implementation establishes:

- Omitting the dataset path is normal. The executor injects the current artifact and reports it as
  `resolved_input`; `path` is no longer in `required` for any tool that consumes the analysis matrix.
- A tool that transforms the dataset refuses a superseded artifact and names the current one.
  An untracked matrix can replace a non-empty analysis only with `adopt_untracked: true`.
  Read-only inspection of an unrelated file remains allowed, but node-scoped evidence from it is
  refused because there is no lineage node to which it can truthfully attach.
- `branch_from` forks an alternative without moving the active version; `analysis-versions` lists
  versions and switches between them. A branch's evidence is not session evidence until it is active.
- Fact roots are registered as node- or session-scoped and **fail closed**; identity axes likewise.
  Adding either is a deliberate, reviewed act.

Three standalone defects the review surfaced were fixed separately and did not wait on the forest:

- `convert_gene_ids` did not re-mint `count_representation`/`dataset_revision` after relabelling
  the var axis, even though `_count_matrix_identity` hashes `var_names`. Fixed, with the explicit
  invalidation of `annotation`/`finalization`/`reference_runs` that `current_annotation_evidence`
  cannot detect on its own (it keys only on `clustering_id`).
- `recover_pending` ordered crash recovery by UUID directory name. Now ordered by staging event
  sequence.
- `investigate_batch` re-asserted `prepared_path` from a read-only tool. Removed.

## Comprehensive-analysis parity pass (implemented 2026-07-27)

The CRC comparison between legacy `run_2026_07_20_234322` and SDK session
`run_20260727T033221Z_59a5ac` exposed a distinction between composability and completeness. The SDK
had the core computation, but publication floors accepted unreviewed cell-QC flags, cluster-QC
warnings, one reference method, a short model-authored report, and an incomplete figure set.

The corrective pass preserves focused, directly callable capabilities and adds a comprehensive
end-to-end **skill default**, not a hardcoded DAG:

- QC count selection defaults to `auto` (`layers["counts"]` when present, otherwise validated
  `X`), eliminating the repeated missing-`counts` failure.
- Cell QC now measures mitochondrial and ribosomal signal, emits the standard figure suite, returns
  every figure for inspection, and has a separate evidence-bound `review_single_cell_qc`
  decision. Final publication requires a current resolved review.
- PCA emits variance and cumulative-variance figures. UMAP now honors the canonical `X_umap`
  contract end to end, and `plot_qc_embedding` paints all available QC/doublet signals.
- The orchestration skill defaults comprehensive runs to user-overridable Leiden comparisons at
  2.0, 1.5, and 1.0. Every resolution receives cluster metric boxplots, a cluster/QC UMAP, all
  eligible covariance heatmaps, three-axis evidence, and `review_cluster_qc`.
  **Superseded 2026-07-29:** the parallel comparison framing was wrong; those resolutions are now
  an iterative ladder. See the 2026-07-29 section at the top.
- Annotation review is explicitly DEG-primary. It expects CellTypist plus SCimilarity when both are
  suitable, requires a specific waiver when only one reference can run, records agreement
  findings, and blocks finalization while any cluster remains unresolved. Cytopus 1.3.4 is locked
  into the RAPIDS environment and can augment seven broad human marker programs; its availability
  and exact program coverage are recorded rather than assumed.
- Committed capability arguments are now provenance. Finalization reconstructs a comprehensive
  report from durable state and capability history, surfaces known caveats, creates final
  UMAP/composition figures, and emits an ordered `analysis-recipe.py`.
- The human `reports/` view contains readable Markdown/PDF/HTML only. JSON/YAML remains in
  canonical capability artifacts and `outputs.json`, removing metadata clutter without deleting
  provenance.

Current inventory after this pass: **21 skills, 20 executable skills, and 48 tools**.

Verification after the pass: **433 deterministic tests passed**; Ruff and strict mypy are clean;
capability validation passes at 21/20/48; all brokered environments are healthy. Live CRC smoke
tests confirmed raw-`X` QC (13 mitochondrial and 97 ribosomal genes, six QC figures), canonical
`X_umap`, Cytopus loading, all 12 cluster covariance heatmaps plus metric/UMAP figures, and a
104-line comprehensive report with an ordered capability recipe.

## What is implemented

### Platform and interaction

- `source setup_gpu.sh` activates the locked uv Python 3.12 control plane, ensures locked Pixi
  compute runtimes, starts/checks the configured LiteLLM gateway, and supports `scagent start`.
- The Rich terminal supports Markdown/LaTeX rendering, dimmed model reasoning when exposed,
  capability progress indicators, local commands, and exact or reconstructed session resume.
  Esc or Ctrl+C stops a running turn (cleanly first, forcibly on a second press) and returns to
  the prompt with the session intact; see "Interruptible turns" below.
  Each analysis turn draws a blue separator rule below the user's input; the first turn of a
  session (new or resumed) names the output folder (`📁 Output: <session-id>`) dimmed beneath it.
- The prompt word-wraps its own input (`terminal/input.py`). prompt_toolkit soft-wraps at the
  character that overflows the terminal width, which cuts words in half, and maps Ctrl+U — what
  terminals send for Cmd+Backspace — to `unix-line-discard`, which kills the whole *logical* line,
  i.e. the entire message however many display rows it occupies. The prompt now computes its own
  word-boundary breaks, renders those rows with prompt_toolkit's character wrapping disabled, and
  scopes Ctrl+U to the display row the cursor is on (killed text still goes to the clipboard, so
  Ctrl+Y restores it). Wrapping stays a display concern: the buffer holds one unbroken line and
  submitted text gains no newline. Verified in a pty at width 40 — word-boundary breaks, cursor
  mapping across the boundary, reflow while deleting through it, row-scoped Ctrl+U, and a real
  two-logical-line submission (`first\nsecond`).
- Newlines are entered with **Ctrl+J** or **Esc+Enter**. Shift+Enter is not bindable: terminals
  send the same byte (CR, `0x0D`) for it as for Enter unless configured otherwise, so nothing
  distinguishes it — Claude Code has the same constraint and its `/terminal-setup` remaps the
  chord to Escape+Enter at the terminal. Ctrl+J (`0x0A`) is a distinct byte everywhere and is what
  several terminals already send for Ctrl+Enter, so it is the zero-configuration single chord.
  `enable_modified_enter()` additionally repoints the three xterm "modified other keys" Enter
  sequences (`CSI 27;{2,5,6};13~`, Shift/Ctrl/Ctrl+Shift+Enter), which prompt_toolkit ships
  deliberately collapsed onto plain Enter, at the newline binding — a terminal configured to send
  them would otherwise submit the message. Both paths verified in a pty (`'first line\nsecond
  line'`).
- The terminal is never silent while a turn is in flight. A spinner covers every wait and names
  what is being waited on — `Thinking` (cold start, model generation between streamed blocks, and
  after a tool failure the model must react to), `Working` (a tool this terminal does not
  instrument, such as the built-in `Skill`), the capability's own `activity_label` while it runs,
  and `Analyzing results` after a capability returns — each with a live elapsed timer
  (`Materializing raw counts... (1m 20s)`) so a minutes-long compute is distinguishable from a
  hang. Only turn-ending events (finished, failed, interrupted) clear it. Previously the spinner
  stopped at the first streamed block and never resumed, so generation gaps and capability
  execution showed nothing at all.
- Completed turns show a persistent bottom-right context bar such as
  `▓▓▓░░░░░░░ 38% · 100K/262K`. prompt_toolkit owns it as a right-aligned bottom toolbar, so its
  prompt redraw cannot erase an out-of-band ANSI cursor write. The toolbar explicitly cancels
  prompt_toolkit's default full-width reverse-video style, so alignment spaces retain the normal
  terminal background. The numerator is live SDK `totalTokens`; the denominator is the discovered
  model/deployment limit. On Iris the SDK reports 200,000 for `rawMaxTokens` despite upstream
  vLLM's 262,144 advertisement, so 200K is retained as diagnostic metadata rather than overriding
  discovery. Native compaction or automatic conversation rollover updates the bar to the new
  epoch's lower usage immediately.
- Session directories now use a time-ordered, collision-safe id — `run_<UTC-timestamp>_<hex>`
  (e.g. `run_20260723T182931Z_532155`) — instead of a bare UUID. The id is still the durable
  identity and resume key (folder name == id); a safe-token validator on create/open/fork also
  closes a directory-traversal gap. Existing UUID-named sessions still resolve.
- Model reasoning ("thinking") is configurable, mirroring the legacy `.env` controls but split by
  concern. Generation (`thinking` mode enabled/disabled/adaptive/native, `budget_tokens`, `effort`)
  maps to Claude Agent SDK options; presentation (`show`, `save`) governs dimmed terminal display
  and a persisted `<session>/logs/reasoning.log`. Defaults live in the model-profile `[thinking]`
  table; `SCAGENT_SDK_THINKING*` env vars override the profile and CLI flags override the env.
  Local Qwen/local-default profiles use `mode = "native"` (inject no Claude-native thinking params),
  while the built-in default is enabled/8000 for Claude-native use. For local Qwen, reasoning is
  turned on at the **gateway**: `configs/litellm/iris-qwen36.yaml` sets
  `extra_body.chat_template_kwargs.enable_thinking: true`, and LiteLLM surfaces the resulting
  reasoning as an Anthropic `thinking` block over `/v1/messages` — verified live end to end (vLLM →
  gateway → thinking block → dimmed in terminal). A running old-config gateway must be restarted to
  pick this up. The terminal renders Markdown headings left-aligned and colored (Rich centers and
  panels H1 by default), and reasoning that arrives both streamed and in the final result text is
  de-duplicated so it shows once. The system prompt (`base.md`, ~70 lines) was enriched for model
  self-awareness — identity, the capability landscape grouped by purpose, the typical evidence flow
  (framed as an arc, not a required DAG), explicit limits ("what you cannot do"), the floor
  rationale, and reasoning guidance that steers toward substantive scientific interpretation rather
  than procedural narration. Verified live: the model now cites floors and the evidence flow and
  reasons about the specific dataset, versus the previous "the user wants X, I'll call tool Y".
- Sessions persist versioned metadata/state, append-only fsynced events, atomic checkpoints,
  runtime bindings, mirrored SDK turns, artifacts, failures, and replayable pending commits.
- `scagent start --resume [session-id]` now prompts for Automatic, Exact, or Reconstructed
  continuation. Automatic uses compatible SDK history while it fits and performs a single safe
  conversation-epoch rollover when preflight usage or a provider context error says it does not.
  Exact never silently reconstructs; Reconstructed intentionally starts from durable state.
  Resume defaults to the session's recorded profile when it remains installed, preserving
  compatibility across changes to the project's default profile; an explicit `--profile` wins.
- Context limits are resolved per deployment rather than hard-coded. Live on Iris, the
  `scagent-primary` LiteLLM alias resolves through `/model/info` to upstream
  `Qwen3.6-27B`, whose vLLM `/v1/models` response advertises **262,144 tokens**. Hosted models can
  use direct provider metadata or the installed model registry; the Claude SDK's `rawMaxTokens`
  remains the runtime fallback.
- Rollover preserves the scientific session verbatim: full facts, decisions, artifacts, identity
  and lineage state, append-only events, and old mirrored transcripts remain on disk. A bounded
  reconstructed handoff carries authoritative state, artifact provenance/paths, state/event file
  pointers, hashes for any oversized sections, and the last four completed/interrupted turns.
  `runtime.context_rolled_over` records the old runtime ID, state revision/event sequence, limit
  source, usage/reserve details, and reconstruction policy before the new runtime is bound.
  Automatic rollover is visible in the terminal as a persistent dimmed notice with the measured
  usage/limit when available, followed by a live `Reconstructing context` spinner. The notice
  explicitly confirms that scientific state and artifacts remain preserved; it does not compete
  with or overwrite the prompt-owned context toolbar.

Verification after the visible-rollover addition: **438 deterministic tests passed**; focused
Ruff checks and strict mypy are clean.
- Claude Agent SDK is the current agent loop; model transport is profile-driven and replaceable.
  The verified Iris profile uses LiteLLM against the local Qwen3.6-27B vLLM service.
- Image pixels, rendered PDF pages plus bounded text, Tavily search, and bounded public-page fetches
  are available as skills. Web search requires `TAVILY_API_KEY`.
- Capability discovery is package-based and deny-by-default. Current inventory: **21 skills, 20
  executable skills, and 48 tools**.
- `inspect-dataset` now also exposes `describe_dataset`: a deterministic, judgment-free H5AD content
  report (shape; `X`/layer/`raw` value facts; per-column `obs`/`var` summaries; embedding/`uns` keys;
  gene-identifier signals) that opens the file backed and samples matrix values safely from a bounded
  row block. It replaces ad-hoc model-written inspection code and handles backed `_CSRDataset`,
  sparse, and GPU matrices without crashing. Gene-identifier facts now scan **every** gene (not a
  200-gene head), and a symbol-aware `gene_symbols` block resolves MT/ribosomal counts from the gene
  **symbol** source (a `feature_name`/`gene_symbol`-style var column when `var_names` are Ensembl IDs)
  and reports Ensembl→symbol mapping coverage. Verified live on `CRC_KG136P_counts.h5ad`: 13 MT / 97
  ribosomal genes via `var['feature_name']`, 16,903/17,583 symbols mapped — matching legacy depth
  (previously it reported "no MT genes in first 200 sampled").
- `inspect-dataset` now also owns gene-identifier conversion. A shared `scripts/genes.py`
  (content-validated symbol/Ensembl column detection, format inference, genome-prefix/version
  stripping, species-case and reference-vocabulary case alignment) backs a new `convert_gene_ids`
  tool that maps `var_names` to symbols **offline** via the file's own symbol column, preserves
  originals in `var['ensembl_id']`, makes duplicates unique without dropping genes, and saves a
  provenance-bearing `gene-symbols.h5ad`. `mygene` is an opt-in, fail-soft online fallback.
  Verified live on `CRC_KG136P_counts.h5ad`: Ensembl→symbol via `feature_name`, 16,903/17,583
  mapped, originals preserved. The SCimilarity and CellTypist skills no longer blind-uppercase
  `var_names` (which silently broke mouse Title-case data and Ensembl-indexed data); they now
  realign gene names to the **reference model's own vocabulary** case-insensitively, auto-selecting
  the best name source (var_names or a symbol column). This fixes the class of failure seen in
  legacy `run_2026_07_22_010140` (mouse UPPER vs model Title case). Inventory is now **23 tools**.
- Capability failures now surface a concise one-line reason to the user (`<tool> failed: <reason>`)
  while the model still receives the full traceback; the `analysis-workspace` code sandbox blocks
  only object-graph-escape dunders so routine introspection runs; and saved custom Python is
  mirrored into the session `code/` directory alongside its provenance-bearing artifact copy.
- Committed artifacts now also project into a human-browsable session view: descriptive relative
  symlinks under `code/`, `shell/`, `figures/`, `reports/`, `tables/`, and `data/`, plus readable
  `outputs.md` and machine-readable `outputs.json` indexes. Finalization receives stable final-data,
  report, and label aliases. The UUID capability tree remains authoritative; the view is rebuilt
  after every commit and resume, adds no duplicate H5AD storage, and never exposes staged or failed
  executions. Nested output groups stay grouped, byte-identical legacy code copies migrate safely
  to links, edited/user files are preserved, and a view I/O failure cannot invalidate a committed
  result.
- `run_shell_command` executions now project too, closing a gap where every shell command a session
  ran was committed under `artifacts/capabilities/<uuid>/` but had no browsable surface at all: the
  `shell/` directory existed only because session creation made it. Because a command is
  uninterpretable without its output, shell runs project as one **bundle directory per execution**
  rather than as loose files — `shell/002-read-end-of-events-jsonl-f513a638/{command.sh, stdout.txt,
  stderr.txt, run.json}`. The name carries an ordinal so a name-sorted listing preserves run order,
  and a label derived from the command text in the committed `shell-run.json` (the only place that
  text is recorded). Files a shell record produces that are not part of the run itself still
  classify into their own category.
  `outputs.md` gains a **Shell commands** section listing command, duration, and per-member links;
  `outputs.json` shell entries carry `bundle_path`, `command`, `cwd`, `exit_code`, and
  `duration_seconds`. The index schema is version 2; existing sessions reuse their current links on
  the first refresh rather than duplicating them. Note that a nonzero exit raises inside the
  entrypoint, so no artifact is committed — projected shell runs are always successful ones.
- `analysis-recipe.py` was 6 lines holding one 39.6 KB `repr()` of the call list — a machine replay
  record that had been doing duty as the human deliverable. It is now readable Python (953 lines
  for the reference session, one commented call per entry, still importable and exactly
  round-tripping) rendered with `pprint` rather than `json.dumps`, because JSON emits
  `true`/`false`/`null` which Python cannot parse, and non-finite floats become explicit
  `float("inf")` calls for the same reason. It stays beside finalization in
  `finalize-analysis/scripts/recipe.py`.
- The reading surface is a **new requestable capability**, `analysis-notebook`
  (`build_analysis_notebook`, `environment: current`, no floors, no required arguments, 22 skills /
  49 tools). It was first built inside `finalize_analysis` and that was wrong: it inherited
  finalization's floors, so a walkthrough was impossible until the analysis was already
  finalizable, it could only ever be produced once, and it implicitly treated annotation as the end
  of the analysis. It is now requestable at any point, states plainly when an analysis is still in
  progress, advertises only the deliverables that actually exist, and is **rebuilt from committed
  provenance on every request** rather than appended to — so a later build covers the steps added
  since, and cannot drift, duplicate, or go stale. Nothing is overwritten: each build commits its
  own immutable artifact and the `reports/analysis-notebook.ipynb` alias resolves to the newest.
  Shape and cluster count come from `facts.analysis.dataset_revision` / `facts.analysis.clustering`
  and labels from the committed `final-labels.csv` parsed with the `csv` module, so the whole
  capability runs in the control plane with no compute runtime. The notebook carries the readability
  work:
  **inputs resolve to their producer** (`path='counts-ready.h5ad'  # output of step 2 ·
  materialize_count_matrix`) instead of showing session-absolute UUID paths, decisions are promoted
  to headings with **untruncated** rationales, steps stay in true chronological order with phase
  headings marked `(continued)` when an analysis iterates, and each step's overview figures are
  embedded while nested per-item diagnostic series are linked. Code cells are an honest record of
  capability calls, not runnable scanpy; a `call` stub raises an explanatory `NotImplementedError`
  so shift-enter explains itself instead of raising `NameError`. `.ipynb` classifies into
  `reports/` with an `analysis-notebook.ipynb` alias. The renderer is stdlib-only
  (`finalize-analysis/scripts/notebook.py`) so it is unit-testable from the control plane.
- Pass 2 for the notebook is **not** done and is a deliberate follow-up: making code cells
  genuinely runnable needs a per-skill reproduction-snippet contract so each capability declares
  its own standard-library equivalent. Synthesizing that centrally would assert an equivalence the
  generator cannot verify. Judgment steps (`review_cluster_qc`, `decide_batch_handling`,
  `review_annotation_evidence`) have no code equivalent and stay documented decisions.
- `run_shell_command`'s result summary now identifies the command instead of reporting only a
  duration: `Shell command completed in 2.77s: tail -5 …/current-state.md`, collapsed to one line
  and bounded at 120 characters so an inline `python3 -c` block stays readable in a transcript and
  in `state.json` on resume. Failures name the command too, on a line above the captured output so
  the terminal's concise one-line extraction still lands on the actual error. The projector's
  summary fallback drops the leading boilerplate clause at `:`, mirroring `_code_label`, so a
  degraded label reads `ls-la-skills` rather than `shell-command-completed-in-2-79s-ls`.

**Output-view acceptance:** the 7.4 GiB session `run_20260726T181314Z_2adcda` now exposes 114
relative links (6 code, 36 figures, 29 reports plus a final-report alias, 25 tables plus a
final-label alias, and 15 datasets plus a final-data alias), with zero broken links and zero
physical payload copies in the review directories. The final deterministic baseline is **426 tests
passed**; full Ruff and strict mypy are clean; capability validation passes at 21/20/44
skills/executable/tools; and all six Iris environment profiles are healthy.

**Shell-projection acceptance:** replayed against a copy of the real session
`run_20260729T205323Z_2a1c40`, whose 8 committed `run_shell_command` executions previously had no
browsable surface. All 8 now project as named bundles (`001-…-b3516d16` through `008-…-7a3d322e`,
under the command-text labels that predate the function-based naming above) carrying 32 links; every
link resolves to a file, repeated refreshes reproduce a byte-identical index, and no `--view-N`
collision names appear. `tests/unit/output_view_test.py` covers this at 28 tests (bundle shape and
member order, ordinal-follows-commit-order with reverse-sorting ids, absolute-path and
trailing-separator label compression, URLs and relative paths left intact, unreadable-report and
non-finite-number fallbacks, mixed shell/figure records, markdown escaping, stale-bundle pruning
that spares category roots, and refresh idempotency), with the summary format and its bounded
multi-line and failure cases covered in `tests/unit/sandbox_and_error_test.py`. The summary change
was confirmed by executing the real entrypoint on Iris, not only through fixtures.

**Notebook acceptance:** driven through the real registered capability handler against the real
session `run_20260729T205323Z_2a1c40` — 113 cells, 8.25 MB, 44 committed steps, 26 embedded overview
figures with the 70 per-cluster cluster-QC panels linked. The same session with its `finalization`
fact removed produces the in-progress variant, which drops the Final labels section and stops
advertising `data/final-annotated.h5ad`. Validated with `nbformat.validate` under
`warnings.simplefilter("error")`, which is what surfaced the missing cell `id` fields that nbformat
4.5 requires and will later hard-fail on; ids are assigned positionally so regenerating a session's
notebook diffs cleanly. `tests/unit/analysis_notebook_test.py` covers this at 30 tests, including a
real defect the tests caught (`pprint` renders `inf`/`nan` as bare names, which made the recipe
un-importable) and a path-escape guard on the state-supplied labels path. Adding the skill required
updating three inventory assertions (registry ids, CLI counts) and naming it in
`configs/models/prompts/base.md`, which the capability-self-knowledge contract test enforces.

Full Ruff, strict mypy, and capability validation (22 skills / 49 tools) are clean, as are all tests
across the touched areas.

### Environments

The control plane is uv-managed; scientific dependencies are executed through the broker in
locked Pixi runtimes. Six logical profiles are configured and healthy on Iris:

| Logical profile | Main use |
|---|---|
| `gpu-singlecell` | RAPIDS-singlecell/Scanpy preparation, QC, doublets, batch, markers, finalization, and restricted analysis code |
| `scvi` | scVI integration |
| `celltypist` | CellTypist reference evidence |
| `scimilarity` | SCimilarity embedding/reference evidence |
| `cellbender` | GPU CellBender ambient-background removal |
| `diffxpy` | Isolated diffxpy/TensorFlow runtime; no focused user-facing DEG skill yet |

See `docs/environments.md` for physical runtime mappings and maintenance rules.

### Scientific capabilities

The current catalog supports dataset identity; separately callable counts, QC/filtering,
normalization, HVG, PCA, graph, UMAP, clustering, and ranking operations; Scrublet evidence and
review; cluster QC; batch investigation; optional CellBender; standalone scVI latent training;
marker DEGs; standalone per-cell CellTypist and SCimilarity with separate cluster summaries; gated
final publication; and external evidence. See `docs/skill-catalog.md` for every skill, tool,
environment, floor, and maturity level.

This is a credible end-to-end vertical slice, not complete scientific parity.

## Artifact-centric decoupling pass (completed 2026-07-24)

The failed CRC SCimilarity session exposed implementation drift toward the DAG that this SDK was
created to avoid. The corrective pass changed both contracts and behavior:

- removed the monolithic `prepare_single_cell` capability;
- added focused count, QC/filter, expression, dimensionality, and clustering packages with eleven
  independent tools;
- made QC assessment flag-only and split cell/gene filtering into explicit mutation calls;
- split PCA, neighbor graph, UMAP, Leiden, and gene ranking so no operation silently performs the
  others;
- made SCimilarity and CellTypist per-cell inference standalone, with optional cluster aggregation
  exposed as a separate tool;
- removed the cluster-QC floor from marker computation and changed cluster QC to accept any
  compatible artifact, with report-only behavior the default;
- made Scrublet evaluation and gene-first batch investigation artifact-driven; their separate
  review/filter/integration decisions remain identity-bound;
- made CellBender input validation standalone and responsible for establishing its own full-file
  dataset identity, while the GPU removal step still requires the matching suitability attestation;
- changed scVI to save only its model and `X_scVI`; graph/UMAP/clustering and scientific adoption
  are separate;
- rewrote the base/orchestration guidance so method prerequisites come from the focused tool,
  not a universal workflow arc; and
- replaced the porous Iris AST/command allowlists with trusted, audited in-process Python and
  Bash. OpenShell remains the intended future isolation boundary on Spark/Linux.

The floor policy is now: ordinary measurement, transformation, model inference, and comparison
validate intrinsic inputs locally; floors are reserved for consequential decisions or mutations
such as CellBender execution, accepting review actions, and final publication.

Artifact handoff is session-aware. Capability envelopes expose absolute paths for immediate reuse
and retain session-relative paths for portable provenance. The executor resolves an existing
relative `path`, `*_path`, `*_dir`, or `cwd` against the scientific session before dispatch, so an
artifact produced by one skill can be consumed directly by another regardless of the CLI working
directory. AnnData remains artifact-backed: each capability process loads, computes, writes its
result, and exits rather than relying on a process-global object.

**Live acceptance:** `scripts/validate_decoupling_live.py` started from blank scientific sessions
and ran on `CRC_KG136P_counts.h5ad`. Count materialization selected raw `X` for 960 cells × 17,583
genes. SCimilarity then ran directly on the original artifact with no inspection/preprocessing/
clustering state: it used `var['feature_name']`, aligned 15,040 input genes to the 28,231-gene
human model, produced a 960 × 128 embedding, 30 reference labels, and per-cell nearest-reference
distances, with **zero floor denials**. The annotated artifact is in session
`run_20260724T042307Z_54c6b7`. This is live-compute evidence, not biological validation of the
predicted labels.

The separated operations were also composed through the real broker on the same CRC input in
session `run_20260724T044725Z_cc80e6`: count materialization → confirmed cell filter → confirmed
gene filter → normalization → HVG mask → PCA → neighbors → UMAP → Leiden, followed by report-only
cluster QC. The chain retained 422 cells × 13,752 genes, produced 9 clusters, preserved fresh
cell/count/representation/clustering identities, and completed three-axis QC without mutation.
This validates composability while keeping every operation separately callable.

The session-relative handoff regression then converted gene identifiers and passed the returned
portable artifact path directly into SCimilarity with `counts_layer="X"` in session
`run_20260724T152312Z_2775ab`. The executor resolved the file inside that session and SCimilarity
completed for 960 cells with 15,040 overlapping genes.

Deterministic baseline after the readiness-inventory, transport, visualization, and
skill-channel changes: **326 tests passed**; Ruff clean; strict mypy clean; capability validation passed with
21/20/43 skills/executable/tools; and all six Iris environment profiles were healthy. (The
preceding decoupling pass baselined at 285 tests.) The SCimilarity and composition runs establish live compute only; model
behavior evals and biological generality remain separate validation levels.

## Skill instructions now reach the model (completed 2026-07-24)

Probing the live CLI with the production options showed the runtime offered **43 tools, all MCP,
and no `Skill` tool**. `ClaudeAgentOptions.tools` is the built-in allowlist and the assembler
passed `tools=()` — correct deny-by-default for Bash/Read/Write, but it also excluded `Skill`.
The `skills=[…]` list became `Skill(name)` allowlist patterns that the CLI discarded because no
Skill tool existed. Every `SKILL.md` — the scientific contract beside each capability — had
therefore **never been seen by the model**, which was running on tool schemas plus `base.md`
alone. This reframes earlier behavior complaints: the model had far less context than the
architecture assumed.

Two candidate routes were tested live:

- `setting_sources: ["project"]` makes skills discoverable, and a probe confirmed it also loads
  this repository's `CLAUDE.md` and `AGENTS.md` — the model quoted "Do not commit or push unless
  the user asks" and the `pytest`/`ruff`/`mypy` check list back. Coding-agent instructions have no
  place in a scientific session, so this route was rejected.
- A **local plugin root** (`--plugin-dir`) publishes skills alone. `capabilities/skill_plugin.py`
  materializes `<session>/runtime/skills-plugin/` with a `.claude-plugin/plugin.json` and a
  `skills` symlink to the configured skills root; it is idempotent and repoints when
  `--skills-root` changes. The assembler now grants exactly one built-in tool (`Skill`) and passes
  that plugin. `setting_sources` stays `[]`; a probe with cwd outside the repository confirmed no
  development rules enter context.

The wiring alone changed little: with the mechanism live, the model loaded a skill when asked
explicitly, and on a first-consequential-use framing it loaded `celltypist-annotation` **and
followed the skill's own pointer to `references/model-selection.md`** — but on a casually phrased
model-choice question it loaded nothing and answered from the system prompt. Loading was still a
model decision, which is precisely the dependency that produced the session failures above.

**Instructions are therefore always on.** `capabilities/instructions.py` renders every discovered
skill's `SKILL.md` — frontmatter stripped, stable order, each section stating the skill's absolute
directory so its relative `references/` links resolve — into the same system-prompt suffix as the
readiness block, recorded as a `capability.instructions_injected` event with per-skill
fingerprints. A 128 KiB soft budget names any omitted skill rather than trimming guidance
invisibly. Progressive disclosure is the wrong trade here: one domain, 21 skills, and a
262,144-token window, so deferring the read buys ~5% of context and costs the failure it was meant
to prevent. The `Skill` tool stays wired for `references/` and re-reading.

Measured: the composed system prompt is 50,073 characters (~12.5k tokens, **4.8%** of the window);
39,253 bytes of instructions from 21 skills; assembly 1.45 s.

**Live acceptance:** the earlier casual model-choice question now returns **zero tool calls**, with
reasoning drawn from the injected inventory and instructions — it selects
`Human_Colorectal_Cancer.pkl` over the immune defaults and adds the skill's own caveat, "don't
treat any reference prediction as final — reconcile with DEG/marker evidence at finalization".
Explicit `Skill` loading still works for the deeper references.

### Open defect: coding-session memory leaks into scientific sessions

While probing, the science agent was found to have `~/.claude/projects/<slug>/memory/MEMORY.md` —
this repository's **coding-assistant** memory — in its context. Verified with a phrase that exists
nowhere else: cwd `/tmp` → `ABSENT`, cwd = repository root → `FOUND`. It is cwd-scoped and
**predates this work** (reproduced with `tools=[]`, `setting_sources=[]`), so every session started
the documented way (`cd /home/ibrahih3/projects/scagent-sdk && scagent start`) has carried it.
The obvious fix — pointing `CLAUDE_CONFIG_DIR` at a scagent-owned directory — must set it for the
parent process too, or the SDK's transcript mirror drops frames whose path is no longer under the
parent's projects directory, which would break exact resume. This remains open. The new
scientific-state context rollover does not change cwd-scoped SDK memory loading and should not be
mistaken for a fix to this separate isolation defect.

## Figure capability and media transport (completed 2026-07-24)

Session `run_20260724T184855Z_e7113a` asked for "some good plots" of the CellTypist/SCimilarity
results. There was no visualization skill, so the model hand-wrote matplotlib in
`analysis-workspace` and failed five times on trivial API drift (`pd.c Crosstab`, `_CSRDataset`
has no `.copy()`, `Index.startswith`, `ndarray.values`, a scanpy QC column name). It eventually
produced four figures, attached them through `inspect-media`, and then the **turn itself died**:

```
Fatal error in message reader: Failed to decode JSON: JSON message exceeded maximum
buffer size of 1048576 bytes
```

Two independent defects, both fixed:

1. **Transport mismatch.** The capability contract allowed 8 MiB × 8 images per result while the
   Claude Agent SDK's stdout reader defaults to a 1 MiB frame; the four previews (231/253/190 KB
   plus one pending, ~1.0 MB base64) crossed it and killed the whole turn instead of one tool
   call. `max_buffer_size` is now set explicitly to 64 MiB, so the binding limit is the
   scientific one: per-image 2 MiB, per-result total 8 MiB (newly enforced in the executor).
   `inspect-media` previews now target the model's own ~1568 px resampling edge and choose PNG or
   JPEG by measured size, because dense scientific figures re-encode *larger* as PNG.
2. **Missing capability.** `visualize-single-cell` (v0.1.0, five tools, `gpu-singlecell`, no
   floors) closes the `visualize-single-cell` P2 gap in `docs/scientific-parity.md`:
   `plot_qc_distributions`, `plot_embedding`, `plot_group_composition`, `plot_label_agreement`,
   and `plot_marker_expression`. Each reads one H5AD, writes the figure **and the table behind
   it**, and returns the figure as model media. Layout is cardinality-aware: panel width is
   budgeted for the legend it will draw, composition switches to a heatmap past twelve classes,
   and categories beyond the cap collapse into a reported `other`. `base.md` and
   `analysis-workspace` now direct figure work here and reserve custom code for real gaps.

**Live acceptance** (`scripts/validate_visualization_live.py`, session
`run_20260724T202255Z_7daf56`): all five tools ran through the real broker on the CRC artifacts
from the failed session (960 cells carrying both annotations plus `X_scimilarity`). Two genuine
bugs were caught only by that live run — matplotlib 3.10 removed `boxplot(labels=)`, and a single
absent gene aborted an eight-gene dot plot; the second is now a reported fact
(`genes_absent`) rather than a failure, since one missing marker must not cost the figure. The
label-agreement heatmap is the substantive win: exact string agreement between CellTypist and
SCimilarity is **0.0%**, yet the figure shows Goblet cells→goblet cell, Enteric glial cells→glial
cell, CD8+ T cells→T cell, Mature Enterocytes→enterocyte. Figures were inspected, not just
produced.

## Capability self-knowledge: local prerequisite inventory (completed 2026-07-24)

Session `run_20260724T182015Z_034c6e` asked a question the agent should have answered instantly —
"are you able to run CellTypist or SCimilarity on this? don't run yet" — and instead spent about
two minutes on `find` sweeps over `/`, `/usersoftware`, and an environment prefix, three of which
timed out at the 20-second shell limit, before the user interrupted the turn. Tool schemas and
skill descriptions were already in context; what was missing was any statement of which
**host assets** exist. Reference-model availability decides feasibility, so the model treated it as
something to investigate.

Prerequisites are now given to the model rather than discovered, in two layers so the coverage is
general rather than two hand-written special cases:

- **Every skill, via its environment.** `probe_environments` reports each logical environment a
  skill routes to — configured, interpreter present, GPU visible, free memory against the declared
  minimum — together with the skills that use it. This is structural, not the full import probe
  behind `doctor environment`, so all six environments cost one `nvidia-smi` call.
- **Skills with host assets, via a declared probe.** `capability.yaml` accepts an optional
  `readiness` block naming a fast probe entrypoint and the capability environment whose variables
  it should see. The probe is skill code that lives beside the skill, runs in the control plane
  using only the standard library, and never imports a scientific package or touches a GPU. Three
  skills declare one today: `celltypist-annotation`, `scimilarity-annotation`, and `research-web`
  (which correctly reports `TAVILY_API_KEY` missing as PARTIAL rather than letting the model
  discover it by watching `web_search` fail).
- `capabilities/readiness.py` runs every declared probe at assembly time in a bounded thread pool
  (10-second default per probe), coerces results to a `ready`/`partial`/`unavailable`/`unknown`
  verdict with a summary and details, and renders them as a "Local prerequisites on this host"
  section. A probe that raises, hangs, returns garbage, or fails to load degrades to `unknown` and
  never blocks session assembly.
- `CapabilityAssembler` appends that section to the model's system prompt through the new
  `ClaudeRuntimeExtensions.system_prompt_suffix`, and records a `capability.readiness_probed`
  event. `scagent-sdk capability validate` reports the same inventory for humans.
- `celltypist-annotation` reports its resolved model cache directory and the cached classifier
  filenames; `scimilarity-annotation` reports each organism's configured model path, completeness,
  and reference vocabulary size.
- `base.md` gained a "Knowing what you can run" section: capabilities are given, not discovered;
  answer feasibility questions from the inventory plus known dataset facts; never search the
  filesystem or import a package to learn what is installed; a contradicting tool result wins.
  `analysis-workspace` now forbids using the shell for capability discovery and treats a timed-out
  search as a signal to stop, not to retry differently.

**Verified on this host:** full assembly including every probe takes 1.45 s, and the composed
system prompt grows 6,148 → 10,196 characters. It states all six compute environments with their
GPU state and users, 54 cached CellTypist classifiers (including `Human_Colorectal_Cancer.pkl`,
the defensible model for the CRC sample in the failed session), usable human (28,231-gene) and
mouse (21,646-gene) SCimilarity models, and the missing Tavily key. That is prompt-content
verification: the model now has the facts in context. Whether its behavior changes on the same
question is a separate model-behavior validation level.

Two contract tests guard against re-drift: every executable skill id must be named in `base.md`,
and the asset-dependent annotation skills must declare a readiness probe.

## SCimilarity organism verification and reference atlas query (completed 2026-07-26)

`scimilarity-annotation` 0.4.0 closes the two remaining gaps against legacy: a species guard that
actually works, and the reference cell query legacy advertised but never ran. Shared model, gene,
and organism resolution moved into `scripts/model_assets.py`; the new query tool is
`scripts/query_reference.py`.

**Organism is declared and then verified.** `organism` previously defaulted to `human`, which was
a real hazard: gene-overlap counting cannot detect a species mismatch, because ~15,600 symbols are
shared between the human and mouse vocabularies once case is folded, so mouse data clears a
5,000-gene threshold against the human reference and returns confident wrong labels. Casing is no
signal either — some pipelines uppercase mouse symbols, which is exactly the bug the case-folded
alignment was added to fix.

The check scores only the *organism-specific* part of each configured vocabulary (12,622
human-only and 6,037 mouse-only case-folded symbols on this host). Verdicts are `consistent`,
`contradicted`, or `unverified`; a contradiction is refused before the encoder, reference labels,
or any index is touched, and `allow_species_mismatch=true` converts the refusal into a recorded
caveat. `organism` is now required with no default in both model tools. Ensembl-only inputs
correctly return `unverified` rather than a guess, leaving the overlap threshold to catch them.

**`query_reference_cells`.** Builds a centroid from selected cells and searches the reference
cell-search index, returning per query the reference `celltype_name` / `tissue` / `tissue_general`
/ `disease` / `study` composition with fractions, the neighbor-distance distribution, and
SCimilarity's coherence QC. Selection is `group_key` (+ optional `group_values`, defaulting to
every group) or explicit `cell_ids` — never both. Design properties:

- **Cheap checks before the expensive load.** Assets, counts, genes, organism, and selection are
  all validated before `CellQuery` opens a 46.9 GiB index; `gene_order.tsv` is read directly so
  validation never constructs the encoder.
- **One index load serves every group**, and `measure_coherence=false` skips the ten extra atlas
  searches coherence needs.
- **Refusal over silent truncation.** More groups than `max_queries` is an error naming the fix;
  groups under `min_query_cells` are reported as skipped; the neighbor artifact is capped at
  200,000 rows with the cap flagged; inline output is bounded to 25 queries × top 5 values with
  the full report always written as an artifact.
- **Identity and cost are recorded.** A model fingerprint that separates embedding files from the
  cell-search index, a content-addressed `query_id`, and measured load/search seconds.

Query evidence lands in `facts.reference_runs.scimilarity_query.<execution_id>`, deliberately
**not** in `annotation.evidence`, so the `current_annotation_evidence` floor keeps its current
meaning (marker evidence plus a CellTypist or SCimilarity kNN run). Promoting atlas queries to a
floor satisfier is a one-line change if that is ever wanted.

Two legacy defects were confirmed while porting, and neither behavior was carried over: legacy
`query_cells` centroid mode could never run (`adata_sci` was assigned inside an always-raising
branch, so it was unbound at use), and its summaries read a `cell_type` column the releases do not
have — the metadata carries `celltype_name` — so `top_celltypes` was always empty.

## SCimilarity capability coverage from the upstream notebooks (completed 2026-07-26)

`scimilarity-annotation` 0.5.0. The SCimilarity tutorials in
`/data1/peerd/ibrahih3/scimilarity/docs/notebooks` were read as the capability inventory; the
annotation and query families they demonstrate are now all reachable, with cost bounded by default.

**Annotation.** `knn_k` (default 50) and `weighting` are exposed instead of hardcoded. The per-cell
vote margins SCimilarity already computes while predicting — `vs2nd`/`vsAll`, or their
inverse-distance-weighted twins — are now written as `obs` columns and summarized in the report;
they were previously discarded, which is the same class of loss as the dropped novelty distances.
**Constrained annotation** (`target_celltypes`) is implemented: it appears in every upstream
annotation notebook and was a dead parameter in legacy scagent. Labels are validated against the
model's 698-label set with near-miss suggestions first, because `safelist_celltypes` works by
marking every *other* label deleted, so one typo would silently leave the reference empty.
Constrained and unconstrained predictions come from a single embedding pass and the report gives
their agreement, the reassigned-cell count, and any safelisted type that matched nothing.

**Query.** `query_mode="cells"` adds the per-cell search the tutorials use most (one cell of
interest against the atlas), alongside the existing centroid mode. Cost control is the point:
`max_query_cells` defaults to **10** and a larger selection is refused with its size rather than
subsampled, `k` stays at 100 against SCimilarity's own default of 10,000, and `measure_coherence`
can be switched off. Three notebook analysis steps that were manual are now part of the contract:
`exclude_studies` (the tutorials filter the query's own study out of every result, otherwise
self-matches dominate), study/sample enrichment via `compile_sample_metadata`, and a
reference-background comparison that turns hit composition into an enrichment ratio against the
reference's own composition for the same cell type.

**Live compute, human atlas.** Constrained annotation on the 960-cell CRC benchmark: a deliberate
typo (`'goblet cel'`) was refused with the three real candidates named; the valid 11-type
colorectal safelist narrowed 30 unconstrained labels to 10, reassigned 201 cells, agreed with 79%
of unconstrained calls, and reported `tuft cell of colon` as safelisted-but-absent (SCimilarity
used `brush cell`, matching the atlas query). Vote margins: median `vs_all` 0.70 with 19% of cells
below 0.5 — evidence that was previously thrown away. Per-cell query of two CRC cells with
`exclude_studies`: the two goblet cells matched goblet terms at 100% (median distance 0.0054-0.0075)
while the tumor cell was ambiguous at distance 0.0586; exclusion removed 69 and 84 of 100 neighbors,
and background comparison showed `large intestine` 8.1× enriched and `small intestine` 0.06×
depleted for `intestine goblet cell`, with `adenocarcinoma` present in the query but absent from
the reference for that type (no ratio, correctly).

**Two defects this live run caught and fixed.** The new per-query evidence was written to the
report artifact but never reached the model, because the inline projection carried a fixed key
whitelist; and sample enrichment was computed from the pre-exclusion neighbor set, so it reported
samples from the very study that had just been excluded. The inline view is now budget-fitted: it
trims in a stated order (narrower value lists → sample enrichment → background → fewer queries)
until it fits 32 KiB and reports what it gave up, because the executor drops `details` wholesale
past its 48 KiB limit — an over-budget result would have cost the model everything rather than the
least useful part.

**Measured cost, k=100, 42.9M-cell human atlas.** Index load 82-88 s at ~84 GiB peak resident; then
55 s per centroid query with coherence, 6.2 s without, ~17 s per cell in per-cell mode, and 4-6 s
for background comparison. All reported with every run.

**Not implemented from the notebooks:** `Interpreter` gene attributions (advanced tutorial's
signature derivation), `get_precomputed_embeddings` pairwise distance-to-query distributions across
reference samples, cell-ontology descendant expansion (needs a network fetch), and resolving a hit's
`study` id to a citation via the local `datasets.csv`/cellxgene census. Each is a separate
capability rather than a parameter of these tools.

**Verification levels.** Deterministic: **34 tests now cover this skill** across
`tests/unit/scimilarity_query_test.py` (26) and `tests/unit/reference_capability_test.py` (8) —
organism declaration/evidence/refusal/override and symbol-column reading, cellsearch asset
validation, fingerprint separation, selection planning, ranking, coherence planning, safelist
validation with near-miss hints, vote-margin surfacing and its degradation paths, per-cell caps,
exclusion records, background enrichment ratios, neighbor and inline-budget bounding, and the
schema contracts for all of it. The whole suite passes; the repository total moved during this
period with concurrent unrelated work on the terminal, model-profile, and runtime modules, so read
the total in the verification record as a measurement rather than this pass's delta. Live compute, human atlas, 42,948,868 reference cells: the contradicted-organism refusal
fired before any load; the documented override then ran a full mouse-model annotation on human
data, which is direct evidence overlap alone would not have caught it. A real query of seven CRC
ground-truth populations took 88 s to load the index and 384 s to search (~55 s/query; 6.2 s/query
with coherence off, identical composition), peaking near 84 GiB resident. Biologically, all seven
mapped to the correct intestinal lineage — Goblet → goblet cell 91% across three goblet terms,
Tuft → brush cell + tuft cell of colon 100%, Enterocytes → colonocyte + enterocyte 100%,
Secretory Precursor → goblet cell 98%, Absorptive Precursor → enterocyte 70% + crypt stem cell
23% — with colon / large intestine / small intestine as the top tissues throughout, and Primary
Tumor carrying the largest median neighbor distance (0.0388 versus 0.0014–0.0208 elsewhere), the
expected novelty signal. Enteroendocrine (4 cells) and ISC (8) were reported skipped.
`scripts/validate_scimilarity_query_live.py` reproduces all of this. Model behavior: three new
evals cover asking rather than guessing an unknown organism, not reflexively overriding a species
contradiction, and using the query tool with correct coherence interpretation — written, not yet
run. No biological-generality claim: one human colorectal sample, one atlas release.

## Verification record

The latest deterministic baseline reported:

- **437 tests passed** (386 at the context-lifecycle baseline plus SCimilarity organism,
  constrained-annotation, and atlas-query coverage); context-lifecycle coverage includes
  local/upstream and hosted-provider
  model-limit discovery, SDK context preflight, automatic/provider-error rollover,
  exact/reconstructed policy, bounded scientific handoff, failed-turn goal preservation, and the
  interactive resume chooser, live SDK context normalization, prompt-owned/right-aligned toolbar
  formatting, rollover refresh behavior, resumed-session restoration, and terminal turn
  integration;
- Ruff clean for `src`, `tests`, `scripts`, and skill scripts;
- strict mypy clean for `src/scagent_sdk`;
- capability validation passed with 21/20/48 skills/executable/tools;
- all six environment profiles healthy through `scagent-sdk doctor environment`.

The live Iris resolver returned
`context_window_tokens=262144`, `source=upstream:models`,
`advertised_model=Qwen3.6-27B`, endpoint `http://iscp001:8000/v1/models`.

Representative live evidence:

- PBMC 10k end to end: inspection, preparation, 17-cluster QC, batch decision,
  marker/CellTypist/SCimilarity evidence, final publication, terminal resume, and an independent
  two-epoch scVI GPU smoke run.
- Doublet live run on 11,043 PBMC cells: 565 Scrublet predicted calls (5.12%). Clusters 4, 11, 16,
  and 17 were enriched and retained for review; no cells were removed.
- Multi-sample batch run: 26 samples, Cramér's V 0.539, mean same-sample-neighbor fraction 0.595
  versus 0.205 expected from composition. Because design/confounding could not be resolved from
  those metrics, the recorded decision was conservatively `request_guidance`, not integration.
- CellBender: the paired 8,201-barcode filtered matrix was correctly refused; a genuine
  396,297-barcode raw matrix trained normally on GPU through epoch 46 before a user-requested
  interruption. A separate controlled timeout committed failure/provenance evidence without
  replacing the active dataset. No completed corrected matrix or biological-improvement claim is
  recorded.

Validation vocabulary used in this repository:

| Level | What it establishes |
|---|---|
| Deterministic contract | Schema, routing, state, artifacts, identity, and failure behavior work for tested cases |
| Live compute | The real dependency and hardware executed on a representative input |
| Model behavior | The selected model chose and interpreted tools appropriately in evaluated conversations |
| Biological generality | The method remains valid across relevant tissues, organisms, study designs, and edge cases |

Never collapse these levels into one “validated” claim.

## P0 corrective pass (completed 2026-07-22)

All five P0 items are corrected with direct deterministic tests; the deterministic-science items
also passed one bounded brokered live run. Validation levels are kept distinct and are not
collapsed into a single "validated" claim.

1. **Resolve raw-count sources for H5AD safely — done.** `prepare-single-cell` now inspects every
   candidate source (`X`, each layer, aligned `.raw`) for finite nonnegative integer counts and
   selects one via `counts_source` (`auto`/`X`/`raw`/`layer`). `auto` prefers a count-like `X`,
   otherwise the sole integer count source; it refuses double normalization when no count source
   exists and refuses ambiguity when several conflict. The validated counts are materialized before
   QC, a `count-source-selection.json` report is emitted, and the selected source is recorded in
   provenance, the count-representation fact, and the count identity. Deterministic tests cover
   inspection and every selection branch. *Files:* `.claude/skills/prepare-single-cell/scripts/prepare.py`,
   `capability.yaml`, `tests/unit/prepare_capability_test.py`.
2. **Bind batch decisions to current identities — done.** `investigate_batch` records the
   cell-set, count-representation, and clustering identities on the `batch` fact; the
   `batch_decision` and `integration_authorized` floors pass only when all three match the active
   analysis (with `not_applicable` exempt), and preparation clears the `batch` fact on re-prep.
   Because scVI issues a new clustering identity, integration now requires a batch decision bound to
   the integrated clustering. Allow/stale/re-fire tests added. *Files:* `src/scagent_sdk/floors/evaluator.py`,
   `.claude/skills/batch-investigation/scripts/investigate.py`,
   `.claude/skills/prepare-single-cell/scripts/prepare.py`, `tests/unit/floor_evaluator_test.py`,
   `tests/unit/batch_capability_test.py`.
3. **Fix high-cardinality batch figures — done.** A pure `_figure_layout` selects an
   external-legend stacked bar at low batch cardinality and a colorbar proportion heatmap (with a
   suppressed, labeled batch-UMAP legend) above a bounded limit; figure dimensions are clamped.
   Layout-decision tests added; live bar-path render confirmed on 7 samples, heatmap render not yet
   exercised on a >12-batch integer-count dataset (none available). *Files:*
   `.claude/skills/batch-investigation/scripts/investigate.py`, `tests/unit/batch_capability_test.py`.
4. **Tighten conservative annotation behavior — done.** The marker dictionary gained a
   plasmacytoid-dendritic-cell program (`LILRA4`/`IL3RA`/`CLEC4C`/`IRF7`/`TCF4`), `GZMB` is shared
   with the cytotoxic program so it cannot carry a call alone, and the conventional-DC program is
   renamed. Marker/finalize references, SKILL guidance, and new marker/finalize evals plus a
   strengthened doublet eval require DEG-primary reasoning, pDC-versus-plasma discrimination,
   confidence that tracks conflict, and Scrublet-as-probability. A deterministic scoring test shows
   a pDC signature outranks plasma and vice versa, and isolated `GZMB` supports neither. The model
   evals remain a separate behavior level, still to be run. *Files:*
   `.claude/skills/marker-annotation/scripts/markers.py` and `references/`/`SKILL.md`/`evals/`,
   `.claude/skills/finalize-analysis/references/`/`SKILL.md`/`evals/`,
   `.claude/skills/doublet-evidence/evals/evals.json`, `tests/unit/marker_capability_test.py`.
5. **Finish targeted finalization tests — done.** `finalize.py` is split into pure validators, an
   `_execute_finalization` I/O step, and envelope assembly. Direct tests cover complete coverage,
   missing/extra clusters, final-vs-DEG mismatch with and without override, unknown-cluster
   overrides, empty values, confidence enum, staleness, non-overwrite, and the emitted state and
   artifacts. *Files:* `.claude/skills/finalize-analysis/scripts/finalize.py`,
   `tests/unit/finalize_capability_test.py`.

### Live evidence for this pass

A bounded brokered run (`scripts/validate_p0_live.py`, no CellBender/scVI training) established:

- **Raw-count refusal:** `pbmc3k_processed.h5ad` (scaled `X`, log-normalized `.raw`, no integer
  counts) was refused with the double-normalization message rather than prepared.
- **Raw-count layer selection:** `combined_truly_raw_annotated.h5ad` (log-normalized `X`, integer
  `raw_counts` layer, 7-sample `sample`) auto-selected `layer:raw_counts` and prepared 36,247 cells
  × 14,836 genes into 21 Leiden clusters.
- **Batch identity binding:** the recorded batch decision matched the current cell-set,
  count-representation, and clustering identities; the `batch_decision` floor passed, and a forced
  new clustering identity made it go stale.

Remaining validation levels for this pass: the P0 #4 model-behavior evals and the heatmap-path live
render still need to run; neither is implied by the deterministic tests or the single live run.

## Restoration 1 — three-axis cluster QC (completed 2026-07-23)

The first of three deterministic-engine restorations (`docs/scientific-restoration-specs.md`).
`cluster-qc` was rebuilt (0.3.0 → 0.4.0) from a single-axis attestation into three independent
axes plus a bounded, identity-safe cleanup:

- **metric QC** — robust per-cluster severity (`clean`/`ambiguous`/`obvious`); no single signal
  decides removal;
- **DEG identity** — `identity_supported`/`junk_markers`/`inconclusive` from a versioned
  nuisance/broad/discriminating gene classification;
- **covariance/coherence** — within-cluster gene-gene correlation structure
  (`unstructured`…`strong`) with a saved heatmap per eligible cluster;
- **technical Moran's I** for mitochondrial fraction and library size (localization only, never
  cell-type evidence);
- **synthesis** removes only `confirmed_junk` clusters (metric-adverse *and* junk markers *and*
  unstructured/weak covariance) strictly below `auto_remove_max_fraction`; a missing/inconclusive
  axis never counts as agreement, and conflicts are kept for review. `auto_remove_convergent=false`
  is inspect/report-only. Removal mirrors the doublet lineage template: fresh
  dataset/cell-set/count identities from preserved raw counts, and downstream invalidation.

The `current_cluster_qc` floor now requires the restored evidence (`evidence_schema_version`,
`evidence_id`, and matching cell-set/representation/clustering identities), so pre-restoration
attestations fail closed and force a rerun before marker/reference/finalization capabilities.

*Validation levels (kept distinct):*

- **Deterministic — green.** `python -m pytest` 203 passed (up from 124: 28 new pure-classifier,
  manifest, cleanup-boundary, and floor tests), ruff clean, strict mypy clean, capability validation
  pass (15/14/22).
- **Live compute — green (bounded).** `scripts/validate_cluster_qc_live.py` drove the real broker
  inspect → prepare → cluster-QC on `combined_truly_raw_annotated.h5ad` (36,247 cells, 21 clusters,
  GPU). All three axes and Moran's I computed on real data (global Moran mt 0.40 / lib 0.72), 20
  correlation heatmaps rendered, evidence CSVs/JSON committed, and the schema-2 attestation bound to
  current identities. Conservative and correct: 0 auto-removals; a high-mitochondrial but
  identity-supported, structured cluster was kept (the rescue case), not removed.
- **Model behavior — not yet run.** Separate level.
- **Biological generality — open.** Structure thresholds (0.08/0.12/0.18) and metric z-gates
  (2.0/3.0) are recorded compatibility defaults; a multi-dataset sweep is required before calling
  them mature, and is best done after Restoration 2 (gene-first batch investigation clarifies when
  weak coherence is batch mixture rather than junk). The large per-cluster review set on this
  un-integrated 7-sample input is expected (batch-smeared coherence) and is flag-only, not removal.

### Post-review conformance fixes (2026-07-23)

Four spec-conformance gaps and one scientific defect from code review were closed:

1. **Complete identity binding** — `current_cluster_qc` now also requires `count_representation_id`;
   `evaluate_cluster_qc` additionally requires `cluster_key` to equal the recorded clustering key
   and the input to be the registered `dataset_revision.prepared_path`.
2. **Complete cleanup invalidation** — the cleanup `facts_patch` now explicitly nulls `doublets`
   (previously left stale under merge-patch) and the downstream `doublet_handling`/`batch_handling`/
   `integration`/`final_labels` decisions, alongside the existing cell-QC/batch/annotation/
   finalization/representation/clustering invalidation.
3. **Exercised cleanup branch** — extracted pure `_cleanup_identities`/`_cleanup_facts_patch`
   helpers (unit-tested for fresh identities and full downstream invalidation without AnnData), plus
   a **synthetic convergent-junk acceptance** (`scripts/validate_cluster_cleanup_synthetic.py` via
   `launch_cluster_cleanup_synthetic.py`) that ran the real `evaluate.run` under the compute runtime:
   the injected junk cluster (obvious/junk_markers/unstructured) was removed, raw integer counts were
   restored, embeddings/clustering dropped, fresh identities issued, and downstream facts invalidated
   — 13/13 checks passed.
4. **Strengthened evidence id** — `evidence_id` now hashes all effective parameters/thresholds and
   the cell/count/representation/clustering identities, not just decisions, so differently
   configured evaluations cannot collide.

Scientific defect: local Moran's I is now variance-normalized (÷ second moment), making per-cell
values scale-invariant, with explicit skip reasons recorded for a missing graph or covariate.
Deterministic baseline after the fixes: **221 tests passed**, ruff/mypy/capability clean.

## Restoration 2 — gene-first batch evidence (completed 2026-07-23)

`batch-investigation` (0.3.0 → 0.4.0) was rebuilt from a single tool that demanded a decision in the
same call into an evidence-before-decision, gene-first pair:

- **`investigate_batch`** produces evidence and records no decision. Gene-first stages: sample-enriched
  regions (enrichment over each batch's dataset-wide frequency, not raw purity); within-sample
  identity DEGs (a region vs the rest of its own batch, holding batch constant); cross-sample
  population matching by shared identity genes; direct matched-region comparison; recurring
  sample-associated programs across ≥2 populations; and a design/confounding cross-tab of supplied
  `condition_keys`. Composition, Cramér's V, neighborhood mixing, and per-batch QC are retained only
  as advisory context. The verdict is two axes — `gene_evidence` × `design_interpretation` — with a
  non-binding recommendation; only a recurring program plus a documented technical batch yields
  `integration_supported`.
- **`decide_batch_handling`** consumes the current `evidence_id`. Non-integration decisions are
  always allowed; `integrate` requires an explicit `integration_basis` and an `override_warning` when
  the recommendation does not support it — integration never proceeds silently against the evidence.
- diffxpy lives in an isolated runtime, so `prefer_diffxpy` degrades visibly to the in-environment
  Wilcoxon test rather than running in the wrong environment.

The `batch` fact is now `{evidence, decision}`. New `current_batch_evidence` floor; `batch_decision`
and `integration_authorized` rebound to that shape, require `decision.evidence_id` to match the
current evidence, and bind all four identities (cell-set, count, representation, clustering). The
identity-free not-applicable shortcut is removed.

### Restoration 2 closure patch (2026-07-23)

Review found six closure-level defects; five are fixed and one is documented rather than hidden:

1. **diffxpy misrepresentation removed** — `prefer_diffxpy` was a no-op that only recorded that
   Wilcoxon ran. It is deleted; scanpy Wilcoxon is the declared primary method (`DE_ENGINE`). A real
   diffxpy cross-check belongs in a separate tool.
2. **False population matches fixed** — matching now uses a versioned nuisance/broad/discriminating
   classifier (`GENE_CLASS_VERSION`) and **discriminating genes only**, with both a shared-gene
   minimum and a Jaccard minimum plus explicit rejection reasons. The live false match of clusters
   19/9 on `DERL3`/`H13`/`HSP90B1` (broad ER/stress) is now rejected and is covered by a test.
3. **Confounding priority corrected** — `classify_design` puts perfect biological confounding
   **ahead of** documented-technical status, so a confounded design is never silently reclassified.
4. **Evidence artifacts thickened** — `sample-enriched-regions.csv` added; within-sample DEGs carry
   gene class, effect, score, adjusted p-value and target/reference fractions; direct comparisons
   carry q-values, scores, both detection fractions, and the compared cluster/batch identities;
   confounding rows carry missingness, level counts, and the batch→condition level mapping.
5. **Provenance and authorization hardened** — the registered `prepared_path` and the recorded
   clustering key are required; a `schema_version` is recorded and required by the floor;
   `evidence_id` hashes effective parameters, technical basis, match evidence and all four
   identities; a `documented_technical_batch` basis is accepted only when the *evidence* recorded
   `technical_batch_documented=true` with a non-empty basis; accepted decisions persist
   `validated` and `decision_policy_version`; and `integration_authorized` now requires that
   validation at the current policy version, a matching evidence id, all four current identities, a
   valid basis, and an explicit `override_warning` whenever the recommendation does not support
   integration. `max_candidate_pairs` bounds the direct-DE cost.
6. **Acceptance coverage extended** — `scripts/validate_batch_synthetic.py` exercises the branches
   real data did not reach: a recurring program (→ `cannot_determine_technical_vs_biological`, never
   auto-integrate), a confounded design with documented-technical claimed (confounding still wins,
   and integration is refused without an override), and pair-order invariance. 12/12 checks pass. It
   also caught a real crash: the direct-comparison path read `pct_nz_reference`, which scanpy omits
   when the reference is a named group; detection fractions are now computed directly.

**Known open defect (documented, not fixed):** recurrence is computed from cell-level Wilcoxon
tests and is retained as a **legacy-compatible advisory signal**. On the synthetic case it reports
low-expression/compositional false positives alongside the injected program. A naive
detection-fraction gate removed the noise but also removed true positives, so it was reverted rather
than shipped. The report, reference, and artifacts state explicitly that recurrence is not
biological replication and must be weighed with detection fractions, effect size, and gene class.
Sample-aware pseudobulk is the appropriate fix and is deliberately out of scope here.

*Validation:* Deterministic — **260 tests passed**, ruff/mypy/capability clean. Live —
`scripts/validate_batch_live.py` drove the real broker inspect → prepare → investigate_batch →
decide on the 7-sample dataset: 9 enriched regions, 8 matches, 0 recurring → `gene_evidence=localized`,
`design=unknown` → `do_not_integrate_based_on_current_evidence`; `keep_uncorrected` recorded with a
matching evidence id. Model-behavior evals remain a tracked follow-up.

## GPU device pinning (completed 2026-07-23)

The broker gated on free GPU memory at probe time but never pinned a device, so frameworks
defaulting to device 0 could land on a busy GPU (on this node, the one hosting the model gateway) —
the behavior legacy `batch/scvi.py` avoided with an NVML most-free selection. `EnvironmentBroker`
now turns its own probe measurement into an explicit pin: `select_gpu_devices` picks the
`gpu_count` devices with the most free memory (deterministic on ties, empty when unmeasured) and
`execute` sets `CUDA_VISIBLE_DEVICES` for the subprocess, recording `pinned_cuda_devices` in the
environment provenance. This is centralized for every GPU capability rather than per script. Unit
tests cover busy-device-0 avoidance, tie determinism, and the no-measurement fallback; a real
brokered GPU call recorded `pinned_cuda_devices: [0]` on an idle node.

## Restoration 3 — external evidence: web and literature (completed 2026-07-23)

External evidence was one skill (`research-web`) doing general web search. Legacy scagent had a
richer story: native `web_search` (Tavily + Google fallback), native `search_papers` (NCBI
E-utilities), `fetch_url`, Europe PMC search/full text via MCP, and a context-aware relevance
scorer (`analysis/literature.py`). Two changes closed the retrieval half of that gap.

**`research-web` (unchanged tool surface, improved backend).**

- Tavily auth moved from the deprecated `api_key`-in-body form to `Authorization: Bearer`, so the
  key never appears in a serializable payload.
- HTML extraction now prefers Tavily `/extract` (server-side render, recovers JavaScript-rendered
  documentation) and falls back to the local tag-stripping parser when no key is set or extraction
  returns nothing. PDFs still download locally for `inspect_pdf`; JSON/XML/text is still returned
  verbatim. Each fetch records `extraction_backend` (`tavily-extract` | `direct-html` |
  `direct-text`).
- The MCP server was deliberately **not** adopted: MCP results bypass the capability executor,
  durable artifacts, lineage, and the SSRF/byte bounds, and interactively-authenticated MCP can be
  absent in headless runs.

**`research-literature` (new skill, 0.1.0).** Europe PMC REST backend, keyless, two tools:

- **`search_literature`** — MEDLINE/PubMed + PMC + preprints in one JSON API. Normalized records
  carry PMID, PMCID, DOI, title, full author string, journal, year, publication types, and derived
  `is_review` / `is_preprint` / `is_open_access` / `full_text_available` / `has_pdf` /
  `cited_by_count`. Filters (`reviews_only`, `open_access_only`, `pubmed_only`,
  `include_preprints`, `recent_years`, `sort`) compose into a `resolved_query` recorded in the
  evidence artifact.
- **`fetch_article_fulltext`** — open-access JATS full text by PMCID (preferred) or PMID, reduced
  to body text with reference lists, tables, and figure blocks dropped. Non-open-access articles
  fail with explicit fallback guidance to `research-web` / `inspect_pdf`.

Improvements over the legacy implementation: a JSON API replaces regex-over-PubMed-XML; preprints
and open-access full text are covered natively rather than only through MCP; all authors rather than
the first; and **transport/parse failures raise** instead of legacy's `except Exception: return []`,
so "search failed" is distinguishable from "nothing matched".

Three defects were found by live testing and one by a deterministic test, all fixed: Europe PMC
escapes inline JATS markup (`&lt;i&gt;THBS1&lt;/i&gt;`), which mangled gene symbols in titles,
abstracts, and body text; `journalTitle` is absent under `resultType=core` (the field is
`journalInfo.journal.title`); `isOpenAccess` over-promises full text, so availability now keys on
`inEPMC`; and the full-text endpoint takes `{base}/{PMCID}/fullTextXML` with no `PMC/` path segment
— the original had one, and the graceful 404 handler was masking it as "not open access".

Validation: 15 new deterministic tests (`tests/unit/literature_capability_test.py`) plus 2 added for
web extraction; full suite green; ruff, mypy, and `capability validate` (15 executable skills, 26
tools) pass. Live Europe PMC search and full-text retrieval verified, and live Tavily search and
`tavily-extract` fetch verified.

Still open, and unchanged in priority: the biological claim/source schema and the lineage/context
relevance matching from legacy `analysis/literature.py`. No floor consumes literature evidence, by
design — citations corroborate dataset evidence and never satisfy a scientific floor.

## Interruptible turns (completed 2026-07-23)

Stopping a running turn used to end the process. `KeyboardInterrupt` unwound out of
`asyncio.run`, so the REPL died with a traceback; the `except KeyboardInterrupt` inside the turn
was unreachable. Worse, `EnvironmentBroker.execute` called blocking `subprocess.run` directly
inside the async executor, so during a long compute the event loop was frozen and *no* interrupt
of any kind could be serviced until the compute finished. Legacy scagent handled this only because
it was synchronous (an Esc listener calling `_thread.interrupt_main()`).

Esc and Ctrl+C now mean "stop this turn, keep the session":

- `terminal/interrupts.py` owns the keyboard. `EscInterruptListener` ports the legacy bare-Esc
  watcher (cbreak + a daemon thread, escape *sequences* drained and ignored, inert without a TTY,
  disabled by `SCAGENT_SDK_NO_ESC_INTERRUPT`); SIGINT is handled through
  `loop.add_signal_handler` for the duration of a turn and the default handler is restored after,
  so no `KeyboardInterrupt` is ever raised through the loop.
- `TurnInterruptController` is a two-stage stop. The first press asks for a clean stop
  (`TurnInterrupter` → the Claude SDK's own `interrupt()` control request **and**
  `EnvironmentBroker.cancel_all()`); a second press, or a 20 s grace expiry, cancels the turn task.
- Compute is now cancellable. Workers run through `Popen(start_new_session=True)` in a registry,
  so `cancel()` signals the entire process group (SIGTERM then SIGKILL) rather than leaving CUDA
  children alive, and the executor runs the broker via `asyncio.to_thread` behind a shield — the
  event loop stays responsive while science runs, and a cancellation deliberately stops and reaps
  the worker instead of orphaning it.
- An interrupt is recorded as its own outcome, not a failure: `runtime.turn_interrupted` (with
  `forced`) and `capability.execution_interrupted`. A stopped turn still binds the runtime session
  it reached, so the next turn resumes the model conversation **exactly** rather than silently
  downgrading to a reconstruction. Nothing partial is committed — a staged capability without
  `result.json` is never recovered.
- At the prompt (no turn running) a single Ctrl+C now warns instead of exiting; a second exits.
  `agent chat` gets the same treatment, and `scagent start` keeps a last-resort `KeyboardInterrupt`
  net so even a raced signal exits like `/exit`.

Verification: 277 deterministic tests pass (12 new, covering the controller's two stages, SIGINT
routing and handler restoration, the interrupted-turn event/binding contract, a real killed worker
process, and event-loop liveness during compute); ruff and strict mypy clean; capability validation
and all six environment profiles healthy. Live on Iris with the Qwen3.6 gateway: pressing Esc, and
separately Ctrl+C, mid-turn printed "Model runtime stopped cleanly", returned to the prompt,
recorded `runtime.turn_interrupted` (`forced: false`), flushed the SDK transcript, and the next
turn on that session resumed in `exact` mode. Real Esc/arrow-key handling was exercised over a pty
(bare Esc stops; arrow/function keys and typing are ignored). Not yet exercised live: an interrupt
landing in the middle of a long GPU capability — that path is covered deterministically only.

## Scientific work after the P0 corrections

Suggested order, still subject to the user's scientific priorities:

1. flag-first cell QC and preview/authorize filtering separated from preparation;
2. sample-aware differential expression and pseudobulk with explicit designs/contrasts;
3. integration alternatives and like-for-like biological-conservation evaluation;
4. pathways/enrichment with reproducible gene universes and database versions;
5. dedicated evidence-consuming visualization/reporting;
6. broader tissue/species identity, annotation models/resources, and repeated scientific evals.

Other open platform work includes Spark profiles, portable host discovery, scheduler/resource
queues, formal state migrations, and a repeated cross-model evaluation harness.

## Durable reference boundaries

| Purpose | Path |
|---|---|
| Active project | `/home/ibrahih3/projects/scagent-sdk` |
| Legacy scientific/UX reference | `/data1/peerd/ibrahih3/cs_agent/scagent` |
| BioNeMo skill packaging/science reference | `/home/ibrahih3/projects/bionemo-lab/contrib-skills` |
| Earlier SDK prototype reference | `/home/ibrahih3/projects/sdk-floor-proto` |

The latter three may be inspected to extract requirements. They must not become imports,
subprocess dependencies, inherited classes, or hidden runtime assumptions.

## User and developer entry points

```bash
cd /home/ibrahih3/projects/scagent-sdk
source setup_gpu.sh
scagent start

# Resume the latest or a named scientific session
scagent start --resume
scagent start --resume <session-id>

# Baseline
python -m pytest
ruff check src tests scripts .claude/skills/*/scripts
mypy src/scagent_sdk
scagent-sdk capability validate
scagent-sdk doctor environment
```

Before beginning work, compare the code and latest artifacts against this file. If they differ,
update this snapshot with evidence rather than silently trusting either source.
