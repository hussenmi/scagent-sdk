# Literature evidence contract

## Backend

Europe PMC REST API (`https://www.ebi.ac.uk/europepmc/webservices/rest`). No API key or account is
required, so literature search stays reproducible in headless and scheduled runs.

Europe PMC is a superset of PubMed for this purpose: it indexes all of MEDLINE/PubMed (`SRC:MED`),
PubMed Central (`SRC:PMC`), and preprint servers including bioRxiv and medRxiv (`SRC:PPR`), and it
returns JSON with open-access flags and PMCIDs. That single API covers both citation-grade metadata
search and open-access full-text retrieval.

## Record schema

Each `search_literature` result is normalized to:

| Field | Meaning |
| --- | --- |
| `id`, `source` | Europe PMC record id and index (`MED`, `PMC`, `PPR`) |
| `pmid`, `pmcid`, `doi` | Stable external identifiers; empty string when absent |
| `title`, `authors`, `journal`, `year`, `publication_date` | Citation metadata (`authors` is the full author string, not just the first author) |
| `publication_types`, `is_review` | Europe PMC publication types and a derived review flag |
| `is_preprint` | True when `source` is `PPR` — not peer reviewed |
| `is_open_access`, `full_text_available` | Open-access flag; `full_text_available` also requires a PMCID |
| `cited_by_count` | Europe PMC citation count — attention, not correctness |
| `abstract` | Abstract text, capped at 2,000 characters |
| `url` | Canonical link: PubMed when a PMID exists, else DOI, else Europe PMC |

## Query filters

The user query is parenthesized and combined with `AND` clauses, so Europe PMC field syntax in the
query itself still works. `resolved_query` in the evidence artifact records exactly what was sent.

| Argument | Clause |
| --- | --- |
| `reviews_only` | `PUB_TYPE:"Review"` |
| `open_access_only` | `OPEN_ACCESS:Y` |
| `pubmed_only` | `SRC:MED` (supersedes `include_preprints`) |
| `include_preprints: false` | `NOT (SRC:"PPR")` |
| `recent_years: N` | `PUB_YEAR:[<current-N+1> TO <current>]` |
| `sort: date` | `P_PDATE_D desc`; default is Europe PMC relevance |

## Bounds and failure behavior

- Search returns 1–25 records; JSON responses capped at 4 MiB, full text at 8 MiB and 40,000
  characters.
- `fetch_article_fulltext` accepts a `PMC\d+` PMCID (preferred) or a numeric PMID, which it resolves
  through Europe PMC. Identifiers are pattern-validated before use in a request path.
- Full text is JATS XML reduced to body text; reference lists, tables, and figure blocks are dropped.
- **Transport and parse failures raise**, they do not return an empty list. An empty `results` array
  means the query genuinely matched nothing — a distinction the legacy implementation lost by
  swallowing every exception.
- A non-open-access article raises with explicit fallback guidance (`research-web` fetch, then
  `inspect_pdf`).

## Standing caveats

Every record is external and potentially stale or wrong. Identifiers establish provenance, not
scientific correctness. Preprints are unreviewed. Literature corroborates dataset evidence; it never
substitutes for it, and it never satisfies a scientific floor.
