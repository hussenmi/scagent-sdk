---
name: batch-investigation
description: Investigate sample-associated structure with matched-population, within-sample DEGs, then record the user's explicit keep/integrate/separate decision. Use before integration and whenever samples, donors, libraries, lanes, or batches may organize the uncorrected representation.
---

# Batch Investigation

Keep this conversational. It is an investigation followed by a user choice, not a workflow DAG.

## When multiple samples are present

Ask the user how to proceed:

1. investigate whether integration is useful;
2. integrate;
3. continue with one combined uncorrected analysis;
4. analyze samples separately; or
5. provide more information about the experiment.

If they choose investigation, build the uncorrected representation and clustering, show a readable
sample-colored embedding, and call `investigate_batch`. Investigation is not authorization to
integrate. After presenting the evidence, put the same practical choices back to the user.

## Scientific method

`investigate_batch` uses a gene-first matched-population method:

1. Find sample-enriched cluster regions using enrichment over each sample's dataset-wide frequency,
   not an arbitrary raw-purity cutoff.
2. For each region, calculate the cluster versus every other cell in the **same sample**. These
   within-sample DEGs are the primary evidence because sample is held constant and the genes
   describe population identity.
3. Nominate similar regions from different samples by mean-expression correlation, then confirm
   the match using overlap between their within-sample identity DEG signatures.
4. For a small number of confirmed pairs, calculate direct pair DEGs to describe how the two
   samples' versions differ.
5. Record design confounding when condition metadata is available. Recurrence, composition,
   Cramér's V, and neighborhood mixing are supporting context only.

The capability writes five durable evidence tables:

- `batch_diagnostic_sample_enriched_regions.csv`
- `batch_diagnostic_within_sample_degs.csv`
- `batch_diagnostic_population_pairs.csv`
- `batch_diagnostic_direct_pair_degs.csv`
- `batch_diagnostic_design_check.csv`

It also writes `README.md`, `batch-investigation.md`, `batch-evidence.json`, a composition figure,
and SDK-native detailed tables. Read the README and name the actual matched regions and shared genes
when explaining the result.

Similar within-sample DEG signatures establish that sample-separated regions look like the same
biological population. That is strong evidence of sample-associated organization in the
uncorrected representation. It does not by itself establish that the cause is technical or that
correction is mandatory: comparable replicates, unrelated donors, biological conditions, tissues,
and protocols have different implications.

## Decision and state

`decide_batch_handling` records `keep_uncorrected`, `integrate`, `separate`,
`request_guidance`, or `not_applicable` against the current evidence identities.

After an investigation, every substantive decision requires a real user answer after the evidence.
Pass only the question you asked; the decision tool resolves the user's answering turn and records
its id, sequence, and exact text automatically. Do not search session files for turn ids. This
prevents the agent from converting its own interpretation into authorization without prescribing
tool order or dialogue rendering.

Once the user has answered, that answer holds. Re-running the investigation after integrating or
re-clustering re-measures the same structure; it does not re-ask the question, and the recorded
answer carries forward. Never ask the user to repeat a batch decision they have already given, and
never treat a later unrelated instruction as the answer. A different batch variable or a different
set of samples *is* a new question, and must actually be put to them.

`request_guidance` is pending and does not satisfy finalization. Integration also requires an
explicit `integration_basis`: use `documented_technical_batch` only when the evidence contains
real documentation, otherwise use `user_authorized_integration` when the user chooses integration.
Evidence stays bound to current cells, counts, representation, and clustering, so changed science
re-measures; the user's recorded choice is what persists across that.

## How to explain the result

Lead with the matched populations and the DEGs, in plain language:

- name the two clusters and samples;
- say that each cluster was compared with all other cells in its own sample;
- name several shared identity genes and give the shared top-25 count;
- explain that this supports the same population being organized by sample; and
- state that the genes alone cannot say whether the cause is technical or biological.

Do not lead with Cramér's V, neighbor mixing, or a list of sample-dominated clusters. Those are
supporting context only. Never call donors technical replicates, call a result a classic/clear
technical batch effect, or promise that integration will remove technical variation unless the
experimental information explicitly establishes that. Healthy tissue does not make different
donors technical replicates. If sample is confounded with donor or condition, say so simply.

Read [references/decision-guide.md](references/decision-guide.md) for interpretation cautions.
