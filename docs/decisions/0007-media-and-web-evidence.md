# Decision 0007: media and web evidence are reusable capability contracts

Status: accepted

## Context

The legacy agent embedded image queues, PDF handling, provider-specific vision messages, and web
search in its central agent and tool modules. That enabled useful behavior but made every new
visual or retrieval feature enlarge the runtime core.

## Decision

- Capability results may declare `model_media` entries that also exist as staged artifacts.
- The executor validates paths, MIME types, count, and size, then returns standard MCP image
  content. Base64 is transport data, not scientific state.
- `inspect-media` owns arbitrary image normalization and PDF text-plus-page rendering.
- Figure-producing scientific skills use the same contract, so visual review is automatic.
- `research-web` owns Tavily search and bounded public-page retrieval. Search credentials come
  from environment variables loaded from an untracked, explicitly selected dotenv file.
- Web and document content is always untrusted evidence and never an instruction source.

## Consequences

The runtime remains provider-neutral and contains no PDF, image, Tavily, or single-cell plotting
logic. Skills remain independently testable, while the durable session records the exact media and
web artifacts used by the model. Text-only models can later gain a sidecar adapter at the runtime
boundary without changing any scientific skill.
