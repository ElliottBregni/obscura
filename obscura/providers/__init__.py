"""obscura.providers — Backend implementations for Obscura.

This package contains all LLM provider implementations:
- copilot: GitHub Copilot backend
- claude: Anthropic Claude backend
- mcp_backend: MCP-based tool backend
- localllm: Local LLM servers (LM Studio, Ollama, llama.cpp, vLLM)
- openai: OpenAI SDK (OpenAI, OpenRouter, Together, Groq, Fireworks)
- moonshot: Moonshot/Kimi via OpenAI-compatible API
"""

from obscura.providers.claude import ClaudeBackend
from obscura.providers.codex import CodexBackend
from obscura.providers.copilot import CopilotBackend
from obscura.providers.localllm import LocalLLMBackend
from obscura.providers.mcp_backend import MCPBackend, MCPBackendMixin
from obscura.providers.moonshot import MoonshotBackend
from obscura.providers.openai import OpenAIBackend

__all__ = [
    "ClaudeBackend",
    "CodexBackend",
    "CopilotBackend",
    "LocalLLMBackend",
    "MCPBackend",
    "MCPBackendMixin",
    "MoonshotBackend",
    "OpenAIBackend",
]
