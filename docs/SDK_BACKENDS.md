# SDK Backends

Obscura supports four LLM backends. Each wraps a different SDK in **full proxy mode** — the SDK owns the entire HTTP lifecycle while Obscura adds agent orchestration, tool dispatch, hooks, memory, and telemetry on top.

## Quick Reference

| | Copilot | Claude | OpenAI | LocalLLM |
|---|---------|--------|--------|----------|
| **SDK** | `github-copilot-sdk` | `claude-agent-sdk` | `openai` | `openai` |
| **Transport** | Event-push | Async-iterator | Chat Completions API | Chat Completions API |
| **Auth** | `GH_TOKEN` | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` | None |
| **Role** | `agent:copilot` | `agent:claude` | `agent:openai` | `agent:localllm` |
| **Default model** | Resolved via `copilot_models` | `claude-sonnet-4-5-20250929` | `gpt-4o` | Server default |
| **Tool calling** | Native SDK session config | Via in-process MCP server | OpenAI function calling | OpenAI function calling |
| **Sessions** | SDK-managed | Implicit (session_id from ResultMessage) | In-memory conversation history | In-memory conversation history |
| **Hooks** | SDK lifecycle events | SDK lifecycle events | `USER_PROMPT_SUBMITTED`, `STOP` | `USER_PROMPT_SUBMITTED`, `STOP` |

---

## Copilot (`github-copilot-sdk`)

**Proxy path:** `ObscuraClient` → `CopilotClient` (SDK) → GitHub API

**Auth:** GitHub token via `GH_TOKEN` env var or `gh auth token`.

**How it works:** The Copilot SDK uses an event-push model. Responses arrive as events (`TEXT_DELTA`, `REFERENCES`, etc.) which Obscura bridges into async iterators via `EventToIteratorBridge`.

**Best for:**
- GitHub CI/CD pipelines with native Copilot access
- Model aliases via `copilot_models.py` (e.g. `copilot_automation_safe`)
- GitHub SSO-integrated environments

**Limitations:**
- Requires GitHub authentication
- Model selection limited to what GitHub exposes
- Cannot point at arbitrary providers

**Testing:** Needs a valid GitHub token or a BYOK mock endpoint.

**Production:** GitHub SSO integration, model aliases via `copilot_models.py`, `automation_safe` flag for guardrails.

```python
async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as client:
    response = await client.send("explain this code")
```

---

## Claude (`claude-agent-sdk`)

**Proxy path:** `ObscuraClient` → `ClaudeSDKClient` (SDK) → Anthropic API

**Auth:** `ANTHROPIC_API_KEY` env var.

**How it works:** The Claude SDK uses an async-iterator model. Obscura wraps it in `ClaudeIteratorAdapter` to normalize into `StreamChunk` / `Message` types. Supports extended thinking and vision.

**Best for:**
- Complex reasoning and extended thinking tasks
- Vision-capable workflows
- Session branching via `fork_session()`

**Limitations:**
- Anthropic API costs
- Anthropic-specific features (extended thinking) are not portable

**Testing:** Needs an Anthropic API key or a mock SDK.

**Production:** Direct Anthropic integration, `fork_session()` for conversation branching.

```python
async with ObscuraClient("claude", model="claude-sonnet-4-5-20250929") as client:
    async for chunk in client.stream("count to 5"):
        print(chunk.text, end="", flush=True)
```

---

## OpenAI (`openai` SDK)

**Proxy path:** `ObscuraClient` → `openai.AsyncOpenAI` → OpenAI API (or compatible provider)

**Auth:** `OPENAI_API_KEY` env var. Optional `OPENAI_BASE_URL` to point at another provider.

**How it works:** Uses the standard `POST /v1/chat/completions` endpoint via the `openai` Python SDK. Tool schemas are injected as OpenAI function calling format. Supports streaming with tool call deltas.

**Best for:**
- Multi-provider production (OpenAI, OpenRouter, Together, Groq, Fireworks)
- Cost flexibility — swap providers by changing `base_url`
- OpenAI-native models (GPT-4o, o1, etc.)

**Limitations:**
- API key required
- Provider-specific model IDs
- No extended thinking or vision (provider-dependent)

**Testing:** Easy to mock with `httpx` or `respx` — standard REST API.

**Production:** Switch providers by changing `OPENAI_BASE_URL`, no code changes needed.

**Supported providers:**

| Provider | `OPENAI_BASE_URL` |
|----------|-------------------|
| OpenAI (default) | `https://api.openai.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Together | `https://api.together.xyz/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Fireworks | `https://api.fireworks.ai/inference/v1` |

```python
async with ObscuraClient("openai", model="gpt-4o") as client:
    response = await client.send("summarize this")
```

---

## LocalLLM (`openai` SDK → localhost)

**Proxy path:** `ObscuraClient` → `openai.AsyncOpenAI` → `localhost:1234/v1` → local model

**Auth:** None required (`api_key="not-needed"`).

**How it works:** Uses the same `openai` SDK as the OpenAI backend, but pointed at a local server. Automatically discovers the default model if none specified. Tool calling works if the server supports it.

**Best for:**
- Development and testing without API keys
- Offline / air-gapped environments
- Privacy-sensitive workloads (data never leaves the machine)
- Rapid iteration without API costs

**Limitations:**
- Model quality varies by server and model
- Tool calling depends on server support
- No cloud-scale throughput

**Testing:** No API key needed. Spin up LM Studio, Ollama, llama.cpp, vLLM, or any OpenAI-compatible server.

**Production:** Air-gapped deployments, data sovereignty requirements.

**Supported servers:**

| Server | Default URL |
|--------|-------------|
| LM Studio | `http://localhost:1234/v1` |
| Ollama | `http://localhost:11434/v1` |
| llama.cpp | `http://localhost:8080/v1` |
| vLLM | `http://localhost:8000/v1` |
| LocalAI | `http://localhost:8080/v1` |

```python
async with ObscuraClient("localllm") as client:
    response = await client.send("hello from localhost")

# Or with a specific server
async with ObscuraClient("localllm", auth=AuthConfig(localllm_base_url="http://localhost:11434/v1")) as client:
    response = await client.send("hello from ollama")
```

**Escape hatches:**

```python
backend = client.backend_impl  # LocalLLMBackend
models = await backend.list_models()       # List available models
health = await backend.health_check()      # Check server reachability
```

---

## RBAC Reference

| Role | Access |
|------|--------|
| `admin` | Full access to all endpoints |
| `agent:copilot` | Copilot backend, read/write agent operations |
| `agent:claude` | Claude backend, read/write agent operations |
| `agent:openai` | OpenAI backend, read/write agent operations |
| `agent:localllm` | LocalLLM backend, read/write agent operations |
| `agent:read` | Read-only access to all agent endpoints |
| `sync:write` | Write access to sync operations |
| `sessions:manage` | Manage persistent sessions |

Write endpoints (spawn, run, delete, etc.) require one of the `agent:*` write roles. Read endpoints (list, get, search, export) also accept `agent:read`.

---

## Environment Variables

| Variable | Backend | Required | Default |
|----------|---------|----------|---------|
| `GH_TOKEN` | Copilot | Yes | — |
| `ANTHROPIC_API_KEY` | Claude | Yes | — |
| `OPENAI_API_KEY` | OpenAI | Yes | — |
| `OPENAI_BASE_URL` | OpenAI | No | `https://api.openai.com/v1` |
| `LOCALLLM_BASE_URL` | LocalLLM | No | `http://localhost:1234/v1` |
| `LM_STUDIO_URL` | LocalLLM | No | (fallback for `LOCALLLM_BASE_URL`) |
| `OLLAMA_URL` | LocalLLM | No | (fallback for `LOCALLLM_BASE_URL`) |

---

## Integration Examples

### SDK (Python)

```python
from sdk import ObscuraClient

# Copilot
async with ObscuraClient("copilot", model_alias="copilot_automation_safe") as c:
    msg = await c.send("explain this code")

# Claude
async with ObscuraClient("claude") as c:
    async for chunk in c.stream("count to 10"):
        print(chunk.text, end="")

# OpenAI (or any compatible provider)
async with ObscuraClient("openai", model="gpt-4o") as c:
    msg = await c.send("summarize this")

# LocalLLM
async with ObscuraClient("localllm") as c:
    msg = await c.send("hello")
```

### HTTP API

```bash
# Send (any backend)
curl -X POST http://localhost:8000/api/v1/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"backend": "openai", "model": "gpt-4o", "prompt": "hello"}'

# Stream (SSE)
curl -X POST http://localhost:8000/api/v1/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"backend": "localllm", "prompt": "hello"}'

# Spawn agent
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "model": "localllm"}'
```

### Agent Loop (with tools)

```python
from sdk import ObscuraClient, tool

@tool(name="get_weather", description="Get current weather")
async def get_weather(city: str) -> str:
    return f"72°F in {city}"

async with ObscuraClient("openai", model="gpt-4o") as c:
    c.register_tool(get_weather)
    async for event in c.run_loop("What's the weather in SF?"):
        if event.text:
            print(event.text, end="")
```
