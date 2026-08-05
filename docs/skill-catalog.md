# Skill catalog and taxonomy

Status date: 2026-08-03  
Discovered inventory (from `scagent-sdk capability validate`): **24 skills, 23 executable skills,
54 tools**. Some rows below predate this count and are not yet individually listed
(`analysis-versions`); the inventory line reflects validation, not table completeness.

## How the catalog is organized

The filesystem remains a flat discovery root at `.claude/skills/<skill-id>/`. Categories below are
conceptual: they help the model find the right capability without adding nested discovery logic or
an umbrella pipeline.

- **Orchestration** contains concise reasoning and branch-selection guidance, never the scientific
  implementation of every step.
- **Workflow science** owns evidence/decision transitions such as preparation, doublet review,
  cluster QC, batch diagnosis, annotation adjudication, and publication.
- **Library/method adapters** wrap a focused external scientific method with strict inputs,
  provenance, artifacts, state effects, and environment routing.
- **Open/reference models** provide non-final annotation evidence that must be reconciled with
  current DEGs and biological context.
- **Utilities** provide bounded inspection, media, web, and auditable workspace capabilities.

Skills can carry secondary tags. For example, CellBender is both a library adapter and an optional
preprocessing branch; marker annotation is both workflow evidence and a method implementation.
RAPIDS-singlecell is mainly an execution environment shared by focused skills, not one giant
`sc-rapids` workflow skill.

## Current packages

| Category | Skill | Tools | Environment | Enforced floors | Current maturity and known limits |
|---|---|---|---|---|---|
| Orchestration | `orchestrate-single-cell` | prompt-only | n/a | n/a | Active concise workflow/resume guidance; must stay non-DAG and defer method detail to focused skills |
| Utility | `analysis-workspace` | `list_workspace`, `read_text_file`, `run_shell_command`, `run_analysis_code` | current; `gpu-singlecell` for code | none | Iris is trusted in-process execution with complete code/command/stdout/stderr provenance, ordinary imports and shell syntax, and only a narrow catastrophic-command refusal. It is explicitly not a security sandbox; OpenShell is the future Spark/Linux isolation boundary |
| Utility | `inspect-dataset` | `inspect_dataset`, `describe_dataset` | current; `gpu-singlecell` for describe | none | Live fingerprint/container identity plus a judgment-free H5AD content report (shape, X/layer/raw value facts, obs/var column facts, embedding/uns keys, gene-identifier signals) with backed/sparse/GPU-safe value sampling; verified live on a backed 43k-cell file |
| Utility | `inspect-media` | `inspect_image`, `inspect_pdf` | current | none | Live image pixels and rendered PDF-page evidence; bounded previews/text |
| Utility | `manage-environment` (0.1.0) | `propose_package_install`, `install_proposed_package`, `list_installed_packages`, `remove_installed_package` | `overlay-admin` (no-GPU view of rapids-main) | install requires an approving user turn after the proposal | On-request package installs into a persistent per-user, per-runtime overlay layered on the immutable locked base — never the shared prefix. `propose` runs a **uv-first (pip-fallback)** dry-run and admits only *additive* packages (a resolved closure that changes no base version); conflict/conda-only/unavailable are refused. `install` re-checks additivity and verifies approval via `CapabilityContext.user_turns` before writing exactly the resolved pins with `--target --no-deps`; `remove` rebuilds the overlay from the remaining pins (a `--target` layout has no uninstaller). The broker exports `SCAGENT_OVERLAY_DIR` and prepends a non-empty overlay to `PYTHONPATH`, and records the overlay set in every runtime result's provenance. **P0 landed and live-validated on real rapids-main** (`feat/on-request-package-installs`): uv classifies gseapy additive in ~1s, flags version-pin conflicts, installs+imports+removes cleanly; pip fallback passes the same flow. Constrained-retry + `doctor` drift/graduation (P1) and isolated sidecar runtimes for incompatible packages (P2) remain per ADR 0010 |
| Utility | `research-web` | `web_search`, `fetch_web_page` | current | none | Live Tavily path for documentation and public pages; requires `TAVILY_API_KEY`. HTML is extracted via Tavily `/extract` with a local fallback (`extraction_backend` records which) |
| Utility | `research-literature` | `search_literature`, `fetch_article_fulltext` | current | none | Europe PMC (MEDLINE/PubMed + PMC + preprints); keyless. Structured PMID/PMCID/DOI records with review/preprint/open-access flags and open-access full text. Claim/source schema and context relevance matching remain future work |
| Workflow science | `single-cell-counts` (0.2.0) | `materialize_count_matrix` | `gpu-singlecell` | none | Standalone H5AD/10x raw-count resolution and materialization; live CRC execution from a blank session |
| Workflow science | `single-cell-qc` (0.5.0) | `calculate_single_cell_qc`, `review_single_cell_qc`, `filter_single_cells`, `filter_single_cell_genes` | `gpu-singlecell` + current review | finalization requires `current_cell_qc_review` | Auto count-source resolution, MT/ribo metrics, six standard QC figures, evidence-bound visual review, and separately confirmed mutation. Gene filtering (0.5.0) matches legacy science: a dataset-size-aware low-detection threshold (2% of cells capped at 100 absolute, `min_cells` for an exact override, `min_cell_fraction=0` to disable) plus default ribosomal removal (RPS/RPL and mito-ribosomal MRPS/MRPL, `remove_ribosomal=false` to keep), mitochondrial genes kept as a cell-level concern; `pct_counts_mt`/`pct_counts_ribo` are computed before removal and preserved in obs so the ribo figures across QC, embeddings, and cluster-QC keep their data source. The review gate matches a figure by file name, path suffix, or full path rather than demanding exact transcription, and records the canonical paths. Heavy-tailed count metrics are plotted as log10 *values* on a linear axis rather than raw values on a log-scaled one, with the raw magnitudes mirrored on the opposite spine; scatter panels mask their columns jointly so both axes describe the same cells |
| Workflow science | `expression-preprocessing` | `normalize_single_cell_expression`, `select_highly_variable_genes` | `gpu-singlecell` | none | Normalization and HVG selection are independent, artifact-in/artifact-out operations |
| Workflow science | `dimensionality-reduction` | `compute_single_cell_pca`, `build_single_cell_neighbors`, `compute_single_cell_umap` | `gpu-singlecell` | none | PCA, graph construction, and UMAP are separate; each validates only the representation it consumes |
| Workflow science | `single-cell-clustering` | `cluster_single_cells`, `rank_single_cell_groups` | `gpu-singlecell` | none | Leiden and group-wise gene ranking are independent; UMAP and cluster QC are not prerequisites |
| Workflow science / method | `doublet-evidence` | `evaluate_doublet_evidence`, `review_doublet_evidence` | `gpu-singlecell` | review requires `current_doublet_evidence` | Deterministic tests and live PBMC Scrublet run; Scrublet-as-probability eval added, broader model conservatism evals still to run |
| Workflow science | `cluster-qc` (0.7.1) | `evaluate_cluster_qc`, `review_cluster_qc` | `gpu-singlecell` + current review | finalization requires current resolved review | Three-axis evidence, per-cluster metric boxplots, cluster/QC UMAP, every eligible covariance heatmap, explicit visual adjudication, and optional bounded cleanup. Figures identify themselves but do not interpret themselves: the boxplot names its metric-flagged clusters beneath the axes, and each covariance heatmap is titled with its clustering, cluster, gene and cell counts, and `mean_abs_corr` — never the structure verdict, since the heatmap is the evidence that verdict is read from and printing it lets a review agree with the label without inspecting the pattern. Reading guidance lives in the folder README rather than on every image. Declares the runtime `documentation` block, so the pass also commits a jargon-free `README.md` beside its evidence stating what the three checks are, what they found on this dataset, and how to read every file |
| Workflow science | `batch-investigation` (0.9.0) | `investigate_batch`, `decide_batch_handling` | `gpu-singlecell` | decision requires `current_batch_evidence`; every substantive post-investigation choice captures the user's real answer turn automatically; `request_guidance` is pending | Writes five gene-first evidence tables: sample-enriched regions, within-sample identity DEGs (primary), profile-nominated/DEG-confirmed cross-sample pairs, direct pair DEGs, and design checks, with README plus SDK JSON/report/native tables. Matched clusters, comparisons, and shared genes are surfaced directly in the result. Recurrence/composition/mixing are advisory only. The user chooses integrate/keep/separate after seeing the evidence. The consultation anchors on the *question* (batch variable plus its sample labels), so a recorded answer survives re-investigation after integration or re-clustering, and the first turn after the question is the answer rather than the newest turn in the session. Evidence schema 2; decision policy 4. |
| Workflow science | `marker-annotation` (0.5.1) | `evaluate_marker_evidence`, `review_annotation_evidence` | `gpu-singlecell` + current review | finalization requires current reviewed evidence | Group-wise DEG/marker evidence plus DEG-primary cross-method review; two references when available or a specific waiver |
| Workflow science | `finalize-analysis` (0.4.0) | `finalize_analysis` | `gpu-singlecell` | `dataset_identity`, `current_cell_qc_review`, `current_cluster_qc`, `batch_decision`, `current_annotation_evidence` | Complete label validation plus state-derived comprehensive report, automatic caveats, final figures, and a readable ordered capability-call recipe (commented, importable Python). Deliberately does not emit the notebook: that is the separate requestable `analysis-notebook` capability |
| Introspection | `query-analysis-state` (0.1.0) | `summarize_analysis_state`, `find_analysis_steps`, `list_recorded_decisions` | `current` | none | Read-only, lineage-aware structured queries over the durable record so the model answers "what was done / which parameters / what was decided" from committed provenance rather than conversation memory. Reads `state.json` facts/artifacts/decisions, orders steps by the `events.jsonl` commit log, and labels every step and version against the active line of descent (a raw grep of `state.json` cannot, so it can surface a superseded sibling-branch value as current). Pure stdlib, control-plane, unit-tested without a live session. Reports what is recorded only: it evaluates no floors and decides nothing |
| Reporting | `analysis-notebook` (0.1.0) | `build_analysis_notebook` | `current` | none | Renders committed provenance as a readable nbformat-4.5 walkthrough: phase headings in true chronological order (marked `(continued)` when an analysis iterates), inputs resolved to the step that produced them instead of UUID paths, untruncated decision rationales, overview figures embedded and per-item diagnostic series linked, final labels read from the committed CSV with the `csv` module so no compute runtime is needed. Requestable at any point rather than bound to finalization, states plainly when an analysis is still in progress, and rebuilt from provenance so a later build covers newly added steps. Code cells are an honest record, not runnable scanpy |
| Library/method adapter | `cellbender-background-removal` | `validate_cellbender_input`, `remove_ambient_background` | `cellbender` | identity; removal also requires current suitability | Strict raw-droplet refusal/routing/lineage/failure contracts and partial live GPU run; no completed corrected-matrix biological claim |
| Library/method adapter | `scvi-integration` (0.5.0) | `train_scvi_latent` | `scvi` | none | Training produces model + `X_scVI` only and streams epoch/loss plus save-phase progress; the epoch cap comes from scVI's inverse cell-count heuristic unless set explicitly, early stopping is on by default on one device, and the result carries epochs trained, ELBO history, a convergence figure, and an overfitting flag; neighbors, UMAP, clustering, comparison, and adoption are separate |
| Open/reference model | `celltypist-annotation` | `run_celltypist_annotation`, `summarize_celltypist_by_cluster` | `celltypist` | none | Per-cell inference is standalone; optional grouping consensus is separate. Declares a readiness probe: the cached classifier filenames reach the model's instructions at session start, so model choice needs no filesystem search |
| Open/reference model | `scimilarity-annotation` (0.5.0) | `run_scimilarity_annotation`, `summarize_scimilarity_by_cluster`, `query_reference_cells` | `scimilarity` | none | Standalone raw-count/gene/model inference with embedding, labels, and novelty distance; live CRC run from blank state succeeded with 15,040 overlapping genes and zero floor denials. `organism` is required with no default and is verified against the organism-specific parts of both model vocabularies, refusing a contradiction before any model loads (overridable, recorded); overlap alone cannot see species. Annotation exposes `knn_k`/`weighting`, surfaces SCimilarity's own per-cell vote margins (`vs_second`, `vs_all`), and supports constrained annotation over a validated `target_celltypes` safelist run from the same embeddings as the unconstrained call. `query_reference_cells` searches the reference cell atlas in two modes — `centroid` over any obs grouping or explicit cell set, and `cells` for per-cell search capped at 10 by default — returning reference cell-type/tissue/disease/study composition, distance distribution, k-relative coherence, study/sample enrichment, and a reference-background enrichment comparison, with `exclude_studies` for self-matching references, one index load per call, refusal instead of truncation, and a budget-fitted inline view that states what it trimmed. Declares a readiness probe reporting each organism's model path, completeness, reference vocabulary size, and cell-search index size |
| Visualization | `visualize-single-cell` (0.4.0) | `plot_qc_distributions`, `plot_qc_embedding`, `plot_embedding`, `plot_group_composition`, `plot_label_agreement`, `plot_marker_expression` | `gpu-singlecell` | none | Figure-per-question tools return pixels for inspection; each call returns a figure *set*. QC covers knee, log-scaled size histograms, MT/ribo, jittered violins, joint scatters, doublets, and per-group boxplots; categorical embeddings get an automatic per-category highlight grid; cardinality-aware layout and tables behind plots. Embedding figures declare a `title` naming their basis and colouring, so the review surface distinguishes the many embeddings one session plots while the artifact `name` downstream selection matches stays fixed |

## Standard executable skill anatomy

An executable package normally contains:

```text
.claude/skills/<skill-id>/
├── SKILL.md                 # when/why/how and interpretation boundaries
├── capability.yaml         # strict tool schema, environment, activity label, floors, optional readiness probe
├── scripts/                # deterministic implementation beside the science (incl. readiness.py when assets are required)
├── references/             # detailed method, state, and interpretation contracts
├── evals/evals.json        # model-choice and scientific-behavior cases
└── agents/openai.yaml      # optional cross-agent metadata
```

Tests live under `tests/` and must distinguish deterministic contracts from model-behavior evals.
Large results become registered artifacts; scripts write only to the supplied staging directory;
the executor validates and atomically commits state/artifact changes after tool completion.

Two result-envelope fields exist so a committed folder is legible without the conversation that
produced it, and both are the skill's to fill:

- Each artifact may carry a **`title`** describing what that particular file shows. The `name` is
  the stable identifier floors, reports, and figure requirements match on and must not absorb the
  subject; the title carries it, and the human review surface names the projected file after the
  title. A skill whose tool emits the same kind of file for different subjects — an embedding per
  colouring, a table per clustering — needs titles or its outputs are indistinguishable on disk.
- A **`documentation`** block (see `src/scagent_sdk/capabilities/documentation.py`) declares what
  was done, what it found on this dataset, what each file is, and what the results do not show.
  The runtime renders it to `README.md` inside the folder it describes and registers it as an
  ordinary artifact, so it is committed, hashed, retained, and projected on the same terms as the
  figures it explains. Findings must be dataset-specific and jargon-free: the reader is a scientist
  who did not run the analysis and does not know this pipeline's vocabulary.

## How the model should select skills

1. Read current session facts and artifact identities.
2. State the next scientific uncertainty, not the next pipeline stage.
3. Choose the smallest focused skill that can resolve it.
4. Check method assumptions and environment health before consequential compute.
5. Inspect result facts, warnings, tables, and figures after commit.
6. Replan or request guidance when evidence conflicts or design metadata is insufficient.

Optional methods are not mandatory steps. CellBender requires suitable raw droplets; integration
requires a justified current batch decision; predicted doublets are reviewed before removal; and
reference-model labels remain hypotheses until DEG-first adjudication.

## Adding or strengthening a skill

Follow `docs/capability-authoring.md` and the migration sequence in `docs/handoff.md`. Start from a
scientific responsibility in `docs/scientific-parity.md`, inspect legacy/BioNeMo sources only for
requirements, define state/lineage effects before coding, implement the focused package, add
contract/floor/recovery tests and model evals, run the exact brokered runtime, inspect artifacts,
then update this catalog and `docs/current-state.md` with the achieved validation level.
