# Web evidence contract

- Backend: Tavily Search and Extract APIs using `TAVILY_API_KEY`. The key is read from the process environment and sent in the `Authorization: Bearer` header — never in a request body, tool input, artifact, or event.
- Search results: title, canonical URL, bounded content excerpt, relevance score, and publication date when supplied by the backend.
- Search size: one to ten results; raw page bodies and generated answers are disabled.
- Fetch: HTTP(S) only, private/reserved/link-local/loopback targets blocked, redirects revalidated, text bodies capped at 2 MiB and PDF downloads at 32 MiB.
- HTML extraction backend, recorded per fetch in `extraction_backend`:
  - `tavily-extract` — preferred when `TAVILY_API_KEY` is set. Tavily fetches and renders the page server-side, recovering text from JavaScript-rendered documentation the local byte fetch cannot see.
  - `direct-html` — hand-rolled tag-stripping fallback used when no key is set or Tavily extraction returns nothing.
  - `direct-text` — non-HTML text bodies (JSON, XML, plain text) are returned verbatim, never routed through extraction.
- HTML/text is normalized and capped at 40,000 characters. PDFs are saved for subsequent `inspect_pdf` review.
- Every result is external, potentially stale, and untrusted. URLs establish provenance but do not establish scientific correctness.
