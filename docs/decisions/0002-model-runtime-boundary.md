# ADR 0002: Model transport is replaceable and profile-driven

Status: accepted for the initial runtime

## Context

The first agent runtime uses the Claude Agent SDK, while most target models are local models
served through vLLM or NIM. The SDK speaks Anthropic Messages and local servers usually speak
an OpenAI-compatible protocol. LiteLLM can bridge them, but local-model quirks must not leak
into scientific skills or session state.

## Decision

Represent model access with versioned TOML profiles. A profile selects a runtime, transport,
model alias, small/fast alias, endpoint, key environment-variable name, prompt, turn limits,
and non-secret runtime environment.

Hash the full non-secret profile into a compatibility fingerprint. Native exact resume is
allowed only when runtime, profile name, and fingerprint are compatible. A profile change
falls back to reconstructed scientific-state resume.

Keep LiteLLM outside the agent process. The runtime health-checks it and sends the endpoint to
the Claude SDK, but does not silently launch or mutate a shared gateway.

The runtime is expressed through a provider-neutral protocol so a future direct LiteLLM or
other model loop can reuse sessions, skills, state, and floors.

## Consequences

- Scientific code does not know whether the model is Claude, vLLM, NIM, or LiteLLM-backed.
- Model-template and reasoning compatibility changes invalidate exact resume safely.
- Secrets remain in environment variables and never enter the profile fingerprint.
- Every supported local model needs explicit compatibility tests.

