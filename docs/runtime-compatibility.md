# Runtime compatibility and deployment

The runtime has three independent layers:

1. the Claude Agent SDK provides the agent loop, session protocol, MCP dispatch, and hooks;
2. LiteLLM exposes an Anthropic Messages API and translates requests;
3. a local vLLM or NIM server performs inference through its OpenAI-compatible API.

Scientific sessions do not depend on any of these processes. A compatible runtime binding
permits exact conversational resume; otherwise the next model reconstructs context from durable
facts, artifacts, decisions, and events.

## Iris Qwen deployment

The checked-in Iris profile targets the existing `Qwen3.6-27B` vLLM service at
`http://iscp001:8000/v1` through an unprivileged loopback LiteLLM process:

```bash
source .venv/bin/activate
litellm --config configs/litellm/iris-qwen36.yaml --host 127.0.0.1 --port 4000
```

In another terminal, verify the gateway and then the complete SDK behavior:

```bash
source .venv/bin/activate
scagent-sdk doctor model --profile iris-qwen36 --probe-messages
scagent-sdk doctor agent --profile iris-qwen36
```

The full agent diagnostic creates disposable scientific sessions under the selected sessions
root. It checks:

- exact text round-tripping;
- in-process MCP tool calls and pre/post hooks;
- reasoning plus a required tool call (a warning is allowed when a gateway merges reasoning);
- recovery from a transient tool error by retrying;
- preservation of the tail marker in a 49,152-character tool result.

No local-model deployment is promoted to supported merely because `/health` responds. The model
must pass these behavioral probes. Change `--checks` to run a subset and
`--long-result-chars` to test a deployment-specific context boundary.

### Verified result on Iris

| Behavior | Qwen3.6-27B result |
|---|---|
| Anthropic Messages translation | pass |
| Basic SDK text | pass |
| In-process MCP tool | pass |
| PreToolUse and PostToolUse hooks | pass |
| Required tool plus answer | pass; reasoning is merged into text |
| Retry after transient tool error | pass; exactly two calls |
| 49,203-character total tool payload | pass |
| 51,251-character total tool payload | fails direct delivery; SDK emits a 2 KiB preview and file pointer |
| Exact resume across separate CLI processes | pass; same SDK session ID and conversation-only token recovered |

The production capability contract therefore limits inline tool content to 48 KiB. Larger
scientific outputs must be written atomically under the scientific session's artifact directory
and returned as a compact summary plus a registered artifact reference. We will not enable ambient
file or shell tools merely to recover SDK-spilled output from `~/.claude`; the capability layer
must own the durable artifact instead.

The upstream vLLM service advertises a **262,144-token** `max_model_len`. The SDK's lower
`maxTokens` value is its effective/autocompact threshold, not the raw model limit;
`rawMaxTokens` agrees with vLLM. The runtime therefore does not hard-code 200,000 or 262,144.
It resolves each profile in this order:

1. context/output metadata published for the LiteLLM alias by `/model/info`;
2. the concrete upstream model reached through `litellm_params.api_base`, using its
   OpenAI-compatible `/models` response (`max_model_len` for vLLM);
3. a direct provider `/v1/models` or `/models` advertisement;
4. installed model-specific provider metadata for hosted APIs;
5. an optional profile fallback, only when discovery publishes no limit.

For an exact Claude Agent SDK resume, `get_context_usage()` supplies the current `totalTokens` and
`rawMaxTokens`. Before sending the turn, scagent reserves the configured maximum output (32,000
tokens when the runtime does not advertise one) plus a safety margin of 3% of the window, with a
4,096-token floor. Reaching that reserve triggers a model-conversation rollover before the
provider rejects the request. A provider-side context error is also recognized and retried once
through the same safe rollover path.

The same SDK control request drives the terminal's persistent bottom-right context indicator:
`▓▓▓░░░░░░░ 38% · 100K/262K`. The numerator is live SDK `totalTokens`; the denominator is the
model/deployment limit resolved above. The Iris SDK currently returns 200,000 for both
`rawMaxTokens` and its effective limit even though upstream vLLM advertises 262,144, so the SDK
value is retained as diagnostic metadata but does not override live deployment discovery.
Usage is captured before the SDK client closes and persisted with the normalized turn response.
The bar itself is rendered by prompt_toolkit's right-aligned bottom toolbar, not an out-of-band
`/dev/tty` cursor write that the next prompt redraw can erase. Its style explicitly cancels
prompt_toolkit's default full-width reverse video, leaving normal terminal background under the
alignment padding. After native compaction or a scientific-session conversation rollover, the
next response immediately replaces it with the new lower context usage.

vLLM returns HTTP 405 for the optional OpenAI `/responses/input_tokens` endpoint; LiteLLM falls
back to its local tokenizer and still serves the SDK's Anthropic count-tokens request successfully.

## Interactive resume

Start with an existing scientific session:

```bash
scagent start --resume <session-id> --profile iris-qwen36
```

The terminal asks for **Automatic**, **Exact**, or **Reconstructed** continuation. Automatic uses
the same Claude SDK conversation when the runtime/profile fingerprint is compatible and its
context fits; otherwise it records `runtime.context_rolled_over` and starts a fresh runtime
conversation from the same durable scientific session. Exact refuses reconstruction. Reconstructed
skips runtime history intentionally. When `--profile` is omitted, the recorded active profile is
selected automatically if it is still installed; an explicit `--profile` always wins.

Rollover is a conversation-epoch boundary, not destructive transcript trimming. The old mirrored
SDK transcript remains under the session, and `state.json`, `events.jsonl`, artifacts, decisions,
identity/lineage records, and capability provenance are unchanged. The reconstructed handoff
contains:

- authoritative facts and decisions;
- a compact artifact/path/provenance index rather than media payloads;
- state/event/artifact file locations plus revision and event sequence;
- the last four completed/interrupted turn handoffs, bounded to avoid recreating the overflow.

If the structured state itself is pathological in size, the prompt substitutes deterministic
section hashes and top-level inventories and explicitly requires reading the authoritative
`state.json`; it never truncates that file. Exact turns after the first process handoff receive
only the checkpoint revision/path reference rather than another full state dump.

`/state` shows the authoritative state and `/exit` ends only the terminal interaction, not the
scientific session.
