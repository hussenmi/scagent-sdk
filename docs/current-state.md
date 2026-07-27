# Current project state

Status date: 2026-07-27  
Authority: this is the concise source of truth for what exists now, what was actually verified,
and what should happen next. Historical detail remains in `docs/handoff.md`.

## Product direction

`scagent-sdk` is an independent, resumable single-cell RNA-seq agent built around four boundaries:

1. the model selects, interprets, and replans;
2. focused skills hold scientific guidance, deterministic scripts, references, schemas, and evals;
3. a durable scientific session owns facts, decisions, events, identities, lineage, and artifacts;
4. hooks enforce a small set of identity-aware scientific floors without encoding a workflow DAG.

The project intentionally does not inherit from or run legacy `scagent`. Scientific lessons are
reimplemented in focused packages and checked against legacy code/tests and BioNeMo contributed
skills only as read-only references.

## Artifact lineage: accepted specification, not implemented (2026-07-27)

`docs/artifact-lineage-and-head-spec.md` (v2.2) is review-cleared and **implementation-ready but
unimplemented**. Read it before touching the capability executor, session state, or any skill that
writes an H5AD.

The defect it addresses: session facts merge by identity in the executor and always converge, while
the H5AD chain merges only by whichever `path` the model passed. A column-adding capability can
therefore derive from a sibling branch carrying identical identities — which **no floor can
detect** — leaving facts and reports correct but the delivered H5AD missing an `obs` column. The
head convention already exists as `facts.analysis.dataset_revision.prepared_path`, written
independently by eight skill scripts.

The design: an executor-owned lineage **forest** in session state, nodes keyed by `execution_id`,
parent taken from the *resolved input* rather than the commit-time active head, identity demoted to
an indexed signature, branch commits state-isolated by a per-node facts snapshot, and fact scope
declared in a central versioned registry. Pruning and column-overlay storage are deliberate
follow-ons; the session measured in that document holds 228 MB of artifacts from a 12 MB input and
no retention policy exists anywhere in `src/scagent_sdk`.

Three standalone defects the review surfaced were **fixed separately** and do not wait on the
forest:

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
  symlinks under `code/`, `figures/`, `reports/`, `tables/`, and `data/`, plus readable
  `outputs.md` and machine-readable `outputs.json` indexes. Finalization receives stable final-data,
  report, and label aliases. The UUID capability tree remains authoritative; the view is rebuilt
  after every commit and resume, adds no duplicate H5AD storage, and never exposes staged or failed
  executions. Nested output groups stay grouped, byte-identical legacy code copies migrate safely
  to links, edited/user files are preserved, and a view I/O failure cannot invalidate a committed
  result.

**Output-view acceptance:** the 7.4 GiB session `run_20260726T181314Z_2adcda` now exposes 114
relative links (6 code, 36 figures, 29 reports plus a final-report alias, 25 tables plus a
final-label alias, and 15 datasets plus a final-data alias), with zero broken links and zero
physical payload copies in the review directories. The final deterministic baseline is **426 tests
passed**; full Ruff and strict mypy are clean; capability validation passes at 21/20/44
skills/executable/tools; and all six Iris environment profiles are healthy.

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
