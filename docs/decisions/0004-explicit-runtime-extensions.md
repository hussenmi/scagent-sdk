# ADR 0004: Runtime capabilities are explicit injected extensions

Status: accepted

## Context

Claude Agent SDK options can expose built-in tools, MCP servers, hooks, project settings, and
skills. Enabling these implicitly would make behavior depend on the user's Claude installation
and would recreate a monolithic tool layer. The future capability registry needs one narrow seam
through which discovered skill packages can contribute executable tools and deterministic floors.

## Decision

`ClaudeAgentSDKBackend` is deny-by-default. It starts with no tools, allowed tools, MCP servers,
or hooks and loads no user/project/local Claude settings. A `ClaudeRuntimeExtensions` value may
explicitly inject in-process MCP servers, exact allow/disallow lists, hook matchers, skills, and
hook-event capture.

The adapter owns transport mechanics only. It does not discover skills, implement scientific
tools, or encode workflow order. Phase 2's registry will validate skill packages and assemble the
extension value. State-based floors will be supplied as hooks over scientific predicates.

## Consequences

- capabilities in one run are inspectable and testable;
- ambient Claude configuration cannot silently grant tools;
- scientific packages remain outside the runtime adapter;
- the compatibility doctor can exercise the same extension seam as production skills;
- registry construction is required before scientific tools become available.
