# Subprocessors

A **subprocessor** is any third-party service that can, by virtue of being integrated into Obscura, see data you route through it. This document enumerates every subprocessor Obscura can use, what each one sees, and how you control whether it's in your data path.

Last reviewed: 2026-04-22.

This list reflects the software's *capabilities*. What any given Obscura deployment actually sends depends on which integrations you enable. Nothing here is sent by default except (a) the LLM backend you select and (b) any explicitly enabled telemetry endpoint.

---

## How the data flows

Obscura is an agent runtime. Three categories of data move:

1. **Prompts and responses** — what the user types, what the LLM replies.
2. **Tool calls** — arguments an agent sends to a tool, and the results that come back.
3. **Memory** — durable state Obscura keeps (key-value and vector).

Different subprocessors see different subsets of these. The table below is explicit about which.

---

## LLM backends

Exactly one is active per session; you pick via `OBSCURA_DEFAULT_BACKEND` or `-b` on the CLI. Whichever backend is active sees **all prompts and tool-call arguments + results** for that session.

| Backend | Vendor | Sees | Residency | Enable/disable |
|---|---|---|---|---|
| `copilot` | GitHub (Microsoft) | Prompts + responses + tool args/results | US (primarily) | Default. `-b <other>` to switch. |
| `claude` | Anthropic | Same | US | `-b claude` |
| `openai` | OpenAI | Same | US | `-b openai` |
| `moonshot` | Moonshot AI (Kimi) | Same | CN | `-b moonshot` |
| `localllm` | None — runs locally | Same (stays on device) | Local | `-b localllm` |

Each of these is invoked under an API key *you* supply (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.). The provider's terms of service apply on top of Obscura's.

## Vector memory

Optional. Enabled via `OBSCURA_VECTOR_BACKEND`.

| Backend | Vendor | Sees | Residency | Enable/disable |
|---|---|---|---|---|
| `sqlite` | None — local file | Embeddings + metadata stay on device | Local | Default. |
| `qdrant` (local) | None — local file | Same | Local | `OBSCURA_VECTOR_BACKEND=qdrant`, `OBSCURA_QDRANT_MODE=local` |
| `qdrant` (cloud) | Qdrant | Embeddings + metadata of memories you store | Depends on your Qdrant Cloud region | `OBSCURA_QDRANT_MODE=cloud` + `OBSCURA_QDRANT_URL` + `QDRANT_API_KEY` |

## Embeddings

When vector memory is on, embeddings are computed to index new memories.

| Model | Vendor | Sees | Residency | Enable/disable |
|---|---|---|---|---|
| `sentence-transformers` local (default) | None — runs locally | Nothing leaves the device | Local | Default. |
| Fallback hash-based | None | Nothing leaves the device | Local | Automatic if sentence-transformers not installed (degraded search quality). |

If you later configure a hosted embedding provider, add it here.

## Telemetry (OpenTelemetry)

Off by default in local-CLI usage. The Helm chart sets `telemetry.enabled: true` by default — customers operating the chart must configure the destination.

| Destination | Vendor | Sees | Residency | Enable/disable |
|---|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Whoever you point it at | Trace spans, metrics, structured logs — including redacted tool arguments and outputs (redaction controlled by the logging config) | Wherever your collector is | `OTEL_ENABLED=false` to disable |

## Authentication (optional)

| Service | Vendor | Sees | Residency | Enable/disable |
|---|---|---|---|---|
| Supabase (when configured) | Supabase | User profile (email, OAuth provider), session tokens; OAuth provider tokens are forwarded back through Obscura but not stored server-side | Depends on your Supabase project region | `SUPABASE_URL` unset → Obscura falls back to local API-key auth |
| GitHub OAuth (via Supabase or `gh auth`) | GitHub | OAuth handshake only; scopes requested: `read:user user:email` | US | `GITHUB_TOKEN` direct or disable Supabase |
| Google OAuth (via Supabase) | Google | OAuth handshake only | Global | Disable via Supabase |

## Integration plugins (all opt-in)

None of these are in the data path unless you install and enable the plugin. Tools gated by the plugin will not execute without credentials you provide.

| Plugin | Vendor | Sees (when enabled) | Enable |
|---|---|---|---|
| `alphavantage`, `coingecko`, `polygon`, `sec-edgar`, `data-gov` | Respective data vendors | Query parameters you send | `/plugin enable <id>` + provider API key |
| `browserless`, `playwright`, `lightpanda` | Respective vendors | URLs you browse + extracted content | Same |
| `gws` (Google Workspace) | Google | Per-call scope (mail/drive/calendar) you authorize | OAuth + `/plugin enable gws` |
| `m365`, `msgraph` (Microsoft 365) | Microsoft | Same | OAuth + enable |
| `notion` | Notion | Pages you read/write | API key + enable |
| `huggingface` | Hugging Face | Model metadata + artifact downloads | Enable (often no key) |
| `datadog`, `prometheus`, `grafana` | Respective vendors | Metrics you export/query | API keys + enable |
| `kubernetes-api`, `docker-engine` | Your cluster | Resources you query — no data to third parties | Local/in-cluster |
| `censys`, `shodan`, `securitytrails` | Respective vendors | Queries you run | API keys + enable |
| `x-twitter` | X | Read/write to your X account | OAuth + enable |
| `matrix`, `nats` | Your server | Messages you send/receive | Local/custom |

## MCP servers (user-installed)

Model Context Protocol servers are installed with `/mcp install <name>` from the user's `.obscura/mcp/mcp.json` or the global config. **Each MCP server you install is an additional subprocessor you are choosing to add.** Obscura neither enumerates nor audits them on your behalf.

## A2A peer agents

If `OBSCURA_A2A_ENABLED=true` and you connect to a remote agent, that remote agent receives the prompts/tool calls you send it. A2A connections are explicit (`/a2a send <agent> <msg>`).

## Messaging channels (opt-in daemons)

| Channel | Vendor | Sees |
|---|---|---|
| iMessage | Apple (local delivery agent) | Messages routed in/out |
| Slack | Slack | Messages to/from the channels you connect |
| Signal | Signal | Messages routed in/out |
| WhatsApp | Meta | Messages routed in/out |
| Webhook | Whoever you send to | Webhook payloads |
| Push | FCM / APNs | Push notification contents |

All off by default; each requires explicit credentials and agent configuration.

## Source control and CI

These see code and commit metadata only. They don't see end-user prompts or memory unless you explicitly store them in the repo.

| Vendor | Sees | Residency |
|---|---|---|
| GitHub | Source, issues, PRs, CI logs, release artifacts | US |
| GitHub Actions | Build logs, test output, any secrets you pass to the workflow | US |
| GitHub Container Registry | Published images + signatures + SBOMs | US |

---

## What we do on subprocessor changes

- We review this list quarterly and on any dependency that introduces a new subprocessor.
- New subprocessors added to the shipped software are flagged in release notes.
- For the hosted Obscura service, we'll publish a changelog for subprocessor changes with a commitment to notify customers at least 30 days before a new subprocessor enters the data path (except when the change is required to fix a security issue, in which case we notify as soon as practical).

## How to opt out of a subprocessor

Every subprocessor in the shipped software is opt-in per the "Enable/disable" column above. If you're using hosted Obscura and want a subprocessor removed from your deployment, contact security@obscura.dev — we'll either configure that instance to exclude it or tell you honestly that it's structural.

## Questions

security@obscura.dev.
