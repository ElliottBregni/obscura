# Backend Parity Matrix

Feature support across LLM providers.

| Feature | Copilot | Claude | OpenAI | LocalLLM | Moonshot |
|---|---|---|---|---|---|
| `send()` | Y | Y | Y | Y | Y |
| `stream()` | Y | Y | Y | Y | Y |
| Tool use | Y | Y | Y | Partial | N |
| System prompt | Y | Y | Y | Y | Y |
| Multi-turn | Y | Y | Y | Y | Y |
| Session mgmt | Y | Y | Y | N | N |
| Session fork | Y | N | N | N | N |
| Native SDK | Y | Y | Y | N | N |
| MCP backend | N/A | N/A | N/A | N/A | N/A |
| Agent loop | Y | Y | Y | Y | Y |
| Thinking/CoT | N | Y | Y | N | N |

**Legend**: Y = fully supported, Partial = basic support, N = not supported, N/A = not applicable

See `obscura/parity/` for automated conformance testing.
