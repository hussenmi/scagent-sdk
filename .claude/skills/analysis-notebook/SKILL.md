---
name: analysis-notebook
description: Build a readable step-by-step Jupyter notebook walkthrough of the analysis so far, with each step's parameters, decision rationale, and figures inline. Use whenever a user asks what was done, wants a walkthrough, a summary of the steps, something to share with a collaborator, or an updated notebook after further work.
---

# Analysis notebook

`build_analysis_notebook` renders everything the session has committed into one readable document:
ordered steps grouped under phase headings, each step's arguments with inputs resolved to the step
that produced them, full decision rationales, and the figures that step produced.

This is a reporting surface over provenance, not a scientific step. It asserts nothing, changes no
state, and has no floors.

## Offer it, do not assume it

Ask before building one. A notebook is a deliverable a user may or may not want yet, and it is not
tied to any particular stage:

- It is valid mid-analysis. The document says plainly that the analysis is still in progress.
- It is valid repeatedly. The notebook is rebuilt from committed provenance each time, so building
  it again after more work covers the newly added steps.
- Nothing is overwritten. Each build commits its own immutable artifact, and
  `reports/analysis-notebook.ipynb` points at the newest one.

Do not silently generate a notebook as a side effect of finishing something. Do not tell a user
their notebook is stale and unfixable; rebuild it.

## What it is not

The code cells record capability calls faithfully, but they are **not runnable scanpy**.
Capabilities need the runtime's floors, staging, and artifact commits. Never tell a user they can
execute the notebook as a standalone pipeline. A `call` stub raises an explanatory error so pressing
shift-enter explains itself instead of failing obscurely.

For the exact machine-replayable call list, `finalize-analysis` writes `code/analysis-recipe.py`.
For narrative findings and caveats, it writes `reports/final-analysis-report.md`. Those three
surfaces are not interchangeable: point a user who asks *what was done* at this notebook.

## Figures

Each step's overview figures embed inline so the notebook survives being shared on its own.
Per-item diagnostic series (one panel per cluster) are linked rather than embedded, because
embedding them would dominate the file. Linked paths resolve when the notebook is opened from the
session's `reports/` directory. Raise `max_embedded_megabytes` when a user wants more inline.
