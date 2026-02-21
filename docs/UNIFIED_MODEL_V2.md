# Unified Model V2 (Native + Unified)

## Goal

Provide one stable Obscura contract that supports:

- `unified` mode for portability
- `native` mode for provider-accurate semantics and metadata

The objective is full SDK integration for each backend without losing normalized cross-provider workflows.

## V2 Core Concepts

Implemented in `sdk/internal/types.py`:

- `ExecutionMode` (`unified`, `native`)
- `ProviderNativeRequest` (provider-specific payload envelope)
- `UnifiedRequest` (future request envelope carrying mode + native payload)
- `StreamChunk.native_event` (raw provider stream passthrough)
- `BackendCapabilities.supports_native_mode`
- `BackendCapabilities.native_features` (versionable per-provider feature list)

Implemented in API schema/routing:

- `POST /api/v1/send` and `POST /api/v1/stream` now accept:
  - `mode` (`unified` / `native`)
  - `api_mode` (backend-specific API selector)
  - `native` (provider-native payload envelope)

## Research Constraints (Provider Reality)

- OpenAI recommends migrating agent workflows to Responses API and has set Assistants sunset for August 26, 2026.
- OpenAI shell/apply_patch/coding-agent loops are Responses-first.
- OpenAI remote MCP behavior has explicit list/call output items and approval controls.
- Claude Agent SDK permission semantics are first-class (`default`, `acceptEdits`, `bypassPermissions`, `plan`) with hooks and `canUseTool`.
- GitHub Copilot CLI and coding agent features are still in public preview and changing quickly.
- MCP transport/security now prioritize stdio + Streamable HTTP, including `Origin` validation guidance.

References:

- [OpenAI: Migrate to Responses](https://platform.openai.com/docs/guides/migrate-to-responses)
- [OpenAI: Deprecations](https://platform.openai.com/docs/deprecations/)
- [OpenAI: Connectors and MCP](https://platform.openai.com/docs/guides/tools-connectors-mcp)
- [OpenAI: Shell tool](https://platform.openai.com/docs/guides/tools-shell)
- [OpenAI: Local shell note](https://platform.openai.com/docs/guides/tools-local-shell)
- [Claude Agent SDK permissions](https://platform.claude.com/docs/en/agent-sdk/permissions)
- [GitHub: Copilot SDK](https://docs.github.com/en/copilot/how-tos/copilot-sdk)
- [GitHub: Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli)
- [GitHub: Hooks configuration](https://docs.github.com/en/copilot/reference/hooks-configuration)
- [MCP transports (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

## Backend Target Matrix

- OpenAI:
  - Add native Responses lane in addition to chat completions.
  - Preserve item-level stream outputs and MCP tool lifecycle.
- Claude:
  - Preserve permission mode and hook order semantics in native lane.
  - Preserve native session operations (`resume`, `fork`).
- Copilot:
  - Preserve event stream types and hook semantics.
  - Support native session/event metadata in stream passthrough.
- LocalLLM:
  - Keep OpenAI-compatible fallback lane.
  - Surface server capability detection (tool support, model quirks).

## Suggested Implementation Phases

1. Contract phase:
   - Introduce mode-aware request/response routing in SDK + API schema.
   - Keep unified mode backward compatible.
2. OpenAI phase:
   - Add native Responses adapter + conformance tests.
3. Claude phase:
   - Add native permission/hook/session conformance tests.
4. Copilot phase:
   - Add native event/hook/session conformance tests.
5. Policy phase:
   - Unify approval/audit policy engine independent of provider transport.
6. Stabilization:
   - Publish capability matrix endpoint and pin compatibility tests in CI.
