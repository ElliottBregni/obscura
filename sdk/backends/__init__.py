"""
sdk.backends — Backend implementations for Obscura.

This package contains all backend implementations:
- copilot: GitHub Copilot backend
- claude: Anthropic Claude backend
- mcp_backend: MCP-based tool backend
- localllm: Local LLM servers (LM Studio, Ollama, llama.cpp, vLLM)
- openai_compat: OpenAI SDK (OpenAI, OpenRouter, Together, Groq, Fireworks)
- moonshot: Moonshot/Kimi via OpenAI-compatible API
"""

from sdk.backends.mcp_backend import MCPBackend, MCPBackendMixin
from sdk.backends.localllm import LocalLLMBackend
from sdk.backends.openai_compat import OpenAIBackend
from sdk.backends.moonshot import MoonshotBackend
from sdk.backends.copilot import CopilotBackend
from sdk.backends.claude import ClaudeBackend

__all__ = [
    "MCPBackend",
    "MCPBackendMixin",
    "LocalLLMBackend",
    "OpenAIBackend",
    "MoonshotBackend",
    "CopilotBackend",
    "ClaudeBackend",
]
