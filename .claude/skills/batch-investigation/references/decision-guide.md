# Batch investigation decision guide

## What the gene test establishes

For a sample-enriched cluster C1 in sample A, compare C1 with all other cells in sample A. Do the
same for C5 in sample B. If the two positive DEG signatures share a coherent set of identity genes,
C1 and C5 are credible versions of the same population in different samples.

If those matched populations occupy separate sample-specific regions of the uncorrected embedding,
the representation has strong sample-associated structure. This is the central scientific result.
Direct C1-versus-C5 DEGs describe the shift between samples but are secondary because that contrast
mixes sample, technical, and biological differences.

Do not call DEG similarity alone proof of a technical batch effect. Comparable technical or
biological replicates may reasonably be integrated; unrelated patients, conditions, tissues, or
sample-private states may contain biology that integration would erase.

## How to present the evidence

Show a readable sample-colored embedding and name one or a few representative matched pairs:

- both cluster and sample labels;
- enrichment of each region over its sample baseline;
- shared within-sample identity genes;
- direct pair differences, when useful;
- available experimental-design context; and
- what remains uncertain.

Cell-level q-values rank separation; cells are not independent biological replicates. Weigh effect
sizes, detection fractions, coherent gene identity, repeated examples, and experimental design.
Recurrence, composition, Cramér's V, and mixing metrics are supporting context, not gates.

## Put the choice to the user

After investigation, ask whether to:

- integrate;
- keep the samples combined without integration;
- analyze them separately; or
- provide more information about the experiment.

The tool finds the newest real user turn after the evidence and records its exact answer, turn id,
and sequence. Pass the question that was asked; do not search session files for an id.
`request_guidance` records that the question is still open; it is not a completed decision.

## Integration

Integration requires an explicit basis:

- `documented_technical_batch` when the evidence was created with a real documented technical
  basis; or
- `user_authorized_integration` when the user chooses integration for the experimental question.

The evidence packet informs the decision but does not authorize correction. After integration,
evaluate the corrected representation again for batch mixing and biological conservation.

## Durable files

The five `batch_diagnostic_*.csv` files contain the scientific evidence. `README.md` documents their
roles, `batch-evidence.json` persists the structured evidence and provenance, and
`batch-decision.md` records the user's chosen action. These files—not conversational prose—are
the durable analysis state.
