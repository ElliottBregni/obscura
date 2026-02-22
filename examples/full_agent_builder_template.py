"""Full-featured Obscura agent builder interface template."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, override

try:
    from obscura import ObscuraClient
    from obscura.agent.agent import BaseAgent
    from obscura.agent.agents import (
        AgentRuntime,
        MCPConfig,
        RuntimeLifecycleEvent,
        RuntimeLifecycleHook,
    )
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import AgentContext, AgentEventKind, HookPoint
    from obscura.integrations.a2a.client import A2AClient
    from obscura.integrations.a2a.tool_adapter import register_remote_agent_as_tool
    from obscura.integrations.mcp.config_loader import (
        build_runtime_server_configs,
        discover_mcp_servers,
    )
    from obscura.tools.system import get_system_tool_specs
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from obscura import ObscuraClient
    from obscura.agent.agent import BaseAgent
    from obscura.agent.agents import (
        AgentRuntime,
        MCPConfig,
        RuntimeLifecycleEvent,
        RuntimeLifecycleHook,
    )
    from obscura.auth.models import AuthenticatedUser
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import AgentContext, AgentEventKind, HookPoint
    from obscura.integrations.a2a.client import A2AClient
    from obscura.integrations.a2a.tool_adapter import register_remote_agent_as_tool
    from obscura.integrations.mcp.config_loader import (
        build_runtime_server_configs,
        discover_mcp_servers,
    )
    from obscura.tools.system import get_system_tool_specs


BackendName = Literal["copilot", "claude", "openai", "moonshot", "localllm"]
RunMode = Literal["run", "stream", "loop", "stream_loop", "aper"]


def _empty_str_map() -> dict[str, str]:
    return {}


def _default_lifecycle_logger(event: RuntimeLifecycleEvent) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    name = event.agent_name or "-"
    model = event.model or "-"
    print(
        f"[{timestamp}] [{event.kind}] agent={name} model={model} {event.message}",
        flush=True,
    )


@dataclass(frozen=True)
class SkillSpec:
    name: str
    content: str
    source: str = "inline"


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    transport: Literal["stdio", "sse"]
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    env: dict[str, str] = field(default_factory=_empty_str_map)

    def to_runtime_config(self) -> dict[str, Any]:
        if self.transport == "stdio":
            return {
                "transport": "stdio",
                "command": self.command,
                "args": list(self.args),
                "env": dict(self.env),
            }
        return {"transport": "sse", "url": self.url, "env": dict(self.env)}


@dataclass(frozen=True)
class A2ARemoteToolsSpec:
    enabled: bool
    urls: tuple[str, ...]
    auth_token: str | None = None

    def to_runtime_config(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"enabled": self.enabled, "urls": list(self.urls)}
        if self.auth_token is not None:
            payload["auth_token"] = self.auth_token
        return payload


@dataclass(frozen=True)
class APERProfile:
    """Custom APER behavior templates."""

    analyze_template: str = "Analyze the user goal and extract constraints."
    plan_template: str = "Create a step-by-step plan to solve the goal."
    execute_template: str = (
        "Goal:\n{goal}\n\nAnalysis:\n{analysis}\n\nPlan:\n{plan}\n\n"
        "Execute using tools where useful and return concise output."
    )
    respond_template: str = "Return a final concise answer based on execution output."
    max_turns: int = 8


def make_user_for_backend(backend: BackendName) -> AuthenticatedUser:
    role = f"agent:{backend}"
    return AuthenticatedUser(
        user_id=f"builder-{backend}-user",
        email=f"{backend}@obscura.local",
        roles=("operator", role),
        org_id="org-builder",
        token_type="user",
        raw_token="builder-token",
    )


class BuilderAPERAgent(BaseAgent):
    """Custom APER agent that runs inside the builder path."""

    def __init__(self, client: ObscuraClient, profile: APERProfile, *, name: str) -> None:
        super().__init__(client, name=name)
        self._profile = profile

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        ctx.analysis = {
            "instruction": self._profile.analyze_template,
            "goal": str(ctx.input_data),
        }

    @override
    async def plan(self, ctx: AgentContext) -> None:
        ctx.plan = {
            "instruction": self._profile.plan_template,
            "steps": [
                "Review goal and context",
                "Use available tools where helpful",
                "Synthesize concise result",
            ],
        }

    @override
    async def execute(self, ctx: AgentContext) -> None:
        execute_prompt = self._profile.execute_template.format(
            goal=str(ctx.input_data),
            analysis=json.dumps(ctx.analysis, indent=2, default=str),
            plan=json.dumps(ctx.plan, indent=2, default=str),
        )
        result = await self._client.run_loop_to_completion(
            execute_prompt,
            max_turns=self._profile.max_turns,
        )
        ctx.results.append(result)

    @override
    async def respond(self, ctx: AgentContext) -> None:
        execute_result = str(ctx.results[-1]) if ctx.results else ""
        ctx.response = (
            f"{self._profile.respond_template}\n\n"
            f"Execution Output:\n{execute_result}"
        )


class AgentBuilder:
    """Interface-style builder for a complete Obscura agent configuration."""

    def __init__(self) -> None:
        self.name: str = "builder-agent"
        self.backend: BackendName = "copilot"
        self.system_prompt: str = "You are a production-grade Obscura agent."
        self.memory_namespace: str = "builder:default"
        self.max_iterations: int = 10
        self.timeout_seconds: float = 180.0
        self.tags: list[str] = []
        self.parent_agent_id: str | None = None
        self.enable_system_tools: bool = True
        self.skills: list[SkillSpec] = []
        self.mcp_servers: list[MCPServerSpec] = []
        self.mcp_auto_discover: bool = False
        self.mcp_config_path: str = "config/mcp-config.json"
        self.mcp_server_names: list[str] = []
        self.mcp_primary_server_name: str = "github"
        self.mcp_resolve_env: bool = True
        self.a2a_remote_tools: A2ARemoteToolsSpec | None = None
        self.aper_profile: APERProfile = APERProfile()
        self.lifecycle_logs_enabled: bool = True

    def with_identity(
        self,
        *,
        name: str | None = None,
        backend: BackendName | None = None,
        memory_namespace: str | None = None,
    ) -> AgentBuilder:
        if name is not None:
            self.name = name
        if backend is not None:
            self.backend = backend
        if memory_namespace is not None:
            self.memory_namespace = memory_namespace
        return self

    def with_runtime_options(
        self,
        *,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        enable_system_tools: bool | None = None,
        parent_agent_id: str | None = None,
        tags: list[str] | None = None,
    ) -> AgentBuilder:
        if max_iterations is not None:
            self.max_iterations = max_iterations
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        if enable_system_tools is not None:
            self.enable_system_tools = enable_system_tools
        self.parent_agent_id = parent_agent_id
        if tags is not None:
            self.tags = list(tags)
        return self

    def with_system_prompt(self, prompt: str) -> AgentBuilder:
        self.system_prompt = prompt
        return self

    def with_aper_profile(self, profile: APERProfile) -> AgentBuilder:
        self.aper_profile = profile
        return self

    def with_lifecycle_logs(self, enabled: bool) -> AgentBuilder:
        self.lifecycle_logs_enabled = enabled
        return self

    def _aper_phase_log(self, label: str) -> None:
        if not self.lifecycle_logs_enabled:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] [aper.{label}] agent={self.name}", flush=True)

    def with_skill_text(self, name: str, content: str) -> AgentBuilder:
        self.skills.append(SkillSpec(name=name, content=content))
        return self

    def with_skill_file(self, path: str) -> AgentBuilder:
        skill_path = Path(path)
        self.skills.append(
            SkillSpec(
                name=skill_path.stem,
                content=skill_path.read_text(encoding="utf-8"),
                source=str(skill_path),
            )
        )
        return self

    def with_skills_from_dir(self, path: str, pattern: str = "*.md") -> AgentBuilder:
        for file_path in sorted(Path(path).glob(pattern)):
            if file_path.is_file():
                self.with_skill_file(str(file_path))
        return self

    def with_mcp_stdio_server(
        self,
        *,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentBuilder:
        self.mcp_servers.append(
            MCPServerSpec(
                name=name,
                transport="stdio",
                command=command,
                args=tuple(args or []),
                env=dict(env or {}),
            )
        )
        return self

    def with_mcp_sse_server(
        self,
        *,
        name: str,
        url: str,
        env: dict[str, str] | None = None,
    ) -> AgentBuilder:
        self.mcp_servers.append(
            MCPServerSpec(
                name=name,
                transport="sse",
                url=url,
                env=dict(env or {}),
            )
        )
        return self

    def with_mcp_discovery(
        self,
        *,
        config_path: str = "config/mcp-config.json",
        server_names: list[str] | None = None,
        primary_server_name: str = "github",
        resolve_env: bool = True,
    ) -> AgentBuilder:
        self.mcp_auto_discover = True
        self.mcp_config_path = config_path
        self.mcp_server_names = list(server_names or [])
        self.mcp_primary_server_name = primary_server_name
        self.mcp_resolve_env = resolve_env
        return self

    def with_a2a_remote_tools(
        self,
        *,
        urls: list[str],
        auth_token: str | None = None,
    ) -> AgentBuilder:
        self.a2a_remote_tools = A2ARemoteToolsSpec(
            enabled=True,
            urls=tuple(urls),
            auth_token=auth_token,
        )
        return self

    def _composed_system_prompt(self) -> str:
        if not self.skills:
            return self.system_prompt
        parts: list[str] = [self.system_prompt, "", "## Loaded Skills"]
        for skill in self.skills:
            parts.append(f"### {skill.name} (source: {skill.source})")
            parts.append(skill.content.strip())
            parts.append("")
        return "\n".join(parts).strip()

    def _resolved_mcp_servers_for_client(self) -> list[dict[str, Any]]:
        explicit_servers = [entry.to_runtime_config() for entry in self.mcp_servers]
        if explicit_servers:
            return explicit_servers
        if not self.mcp_auto_discover:
            return []
        discovered = discover_mcp_servers(
            self.mcp_config_path,
            resolve_env=self.mcp_resolve_env,
        )
        selected: list[str] | None = None
        if self.mcp_server_names:
            known = {entry.name for entry in discovered}
            selected = [name for name in self.mcp_server_names if name in known]
            if not selected:
                requested = ", ".join(self.mcp_server_names)
                print(
                    "Warning: requested MCP server names were not discovered: "
                    f"{requested}. Continuing without MCP servers."
                )
                return []
        return build_runtime_server_configs(
            discovered,
            selected_names=selected,
            primary_server_name=self.mcp_primary_server_name,
        )

    def _build_mcp_config(self) -> MCPConfig:
        explicit_servers = [entry.to_runtime_config() for entry in self.mcp_servers]
        enabled = self.mcp_auto_discover or bool(explicit_servers)
        return MCPConfig(
            enabled=enabled,
            servers=explicit_servers,
            config_path=self.mcp_config_path,
            server_names=list(self.mcp_server_names),
            primary_server_name=self.mcp_primary_server_name,
            auto_discover=self.mcp_auto_discover,
            resolve_env=self.mcp_resolve_env,
        )

    def build_spawn_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "name": self.name,
            "model": self.backend,
            "system_prompt": self._composed_system_prompt(),
            "memory_namespace": self.memory_namespace,
            "max_iterations": self.max_iterations,
            "timeout_seconds": self.timeout_seconds,
            "enable_system_tools": self.enable_system_tools,
            "tags": list(self.tags),
            "mcp": self._build_mcp_config(),
        }
        if self.parent_agent_id is not None:
            kwargs["parent_agent_id"] = self.parent_agent_id
        if self.a2a_remote_tools is not None:
            kwargs["a2a_remote_tools"] = self.a2a_remote_tools.to_runtime_config()
        return kwargs

    async def _attach_a2a_remote_tools(self, client: ObscuraClient) -> list[A2AClient]:
        attached: list[A2AClient] = []
        remote = self.a2a_remote_tools
        if remote is None or not remote.enabled:
            return attached
        for url in remote.urls:
            a2a_client = A2AClient(url, auth_token=remote.auth_token)
            await a2a_client.connect()
            await a2a_client.discover()
            local_registry = ToolRegistry()
            spec = register_remote_agent_as_tool(local_registry, a2a_client)
            client.register_tool(spec)
            attached.append(a2a_client)
        return attached

    async def _run_custom_aper(self, prompt: str) -> str:
        mcp_servers = self._resolved_mcp_servers_for_client()
        user = make_user_for_backend(self.backend)
        if self.lifecycle_logs_enabled:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{timestamp}] [aper.starting] agent={self.name} model={self.backend} "
                "Starting APER session.",
                flush=True,
            )
        async with ObscuraClient(
            self.backend,
            system_prompt=self._composed_system_prompt(),
            mcp_servers=(mcp_servers if mcp_servers else None),
            user=user,
        ) as client:
            if self.lifecycle_logs_enabled:
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{timestamp}] [aper.client_ready] agent={self.name} "
                    "Backend client started.",
                    flush=True,
                )
            if self.enable_system_tools:
                for spec in get_system_tool_specs():
                    client.register_tool(spec)
            a2a_clients = await self._attach_a2a_remote_tools(client)
            try:
                agent = BuilderAPERAgent(
                    client,
                    profile=self.aper_profile,
                    name=self.name,
                )
                agent.on(HookPoint.PRE_ANALYZE, lambda _: self._aper_phase_log("pre_analyze"))
                agent.on(HookPoint.POST_ANALYZE, lambda _: self._aper_phase_log("post_analyze"))
                agent.on(HookPoint.PRE_PLAN, lambda _: self._aper_phase_log("pre_plan"))
                agent.on(HookPoint.POST_PLAN, lambda _: self._aper_phase_log("post_plan"))
                agent.on(HookPoint.PRE_EXECUTE, lambda _: self._aper_phase_log("pre_execute"))
                agent.on(HookPoint.POST_EXECUTE, lambda _: self._aper_phase_log("post_execute"))
                agent.on(HookPoint.PRE_RESPOND, lambda _: self._aper_phase_log("pre_respond"))
                agent.on(HookPoint.POST_RESPOND, lambda _: self._aper_phase_log("post_respond"))
                result = await agent.run(prompt)
                if self.lifecycle_logs_enabled:
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"[{timestamp}] [aper.completed] agent={self.name} APER run completed.",
                        flush=True,
                    )
                return str(result)
            finally:
                for a2a_client in a2a_clients:
                    await a2a_client.disconnect()

    async def run(
        self,
        prompt: str,
        *,
        mode: RunMode = "loop",
        max_turns: int | None = None,
    ) -> str:
        if mode == "aper":
            return await self._run_custom_aper(prompt)

        lifecycle_hook: RuntimeLifecycleHook | None = (
            _default_lifecycle_logger if self.lifecycle_logs_enabled else None
        )
        runtime_user = make_user_for_backend(self.backend)
        try:
            runtime = AgentRuntime(
                user=runtime_user,
                lifecycle_hook=lifecycle_hook,
            )
        except TypeError:
            runtime = AgentRuntime(user=runtime_user)
            if lifecycle_hook is not None and hasattr(runtime, "set_lifecycle_hook"):
                setter = getattr(runtime, "set_lifecycle_hook")
                setter(lifecycle_hook)
        try:
            await runtime.start()
            agent = runtime.spawn(**self.build_spawn_kwargs())
            agent.heartbeat_enabled = False
            await agent.start()

            if mode == "run":
                return str(await agent.run(prompt))
            if mode == "stream":
                chunks: list[str] = []
                async for chunk in agent.stream(prompt):
                    chunks.append(chunk)
                return "".join(chunks)
            if mode == "loop":
                return str(await agent.run_loop(prompt, max_turns=max_turns))

            text_parts: list[str] = []
            async for event in agent.stream_loop(prompt, max_turns=max_turns):
                if event.kind == AgentEventKind.TEXT_DELTA:
                    text_parts.append(event.text)
            return "".join(text_parts)
        finally:
            await runtime.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Full Obscura Agent Builder template")
    parser.add_argument(
        "--backend",
        choices=("copilot", "claude", "openai", "moonshot", "localllm"),
        default="copilot",
    )
    parser.add_argument("--name", default="builder-agent")
    parser.add_argument(
        "--prompt",
        "-p",
        default="Summarize your loaded capabilities in 5 bullets.",
    )
    parser.add_argument(
        "--mode",
        choices=("run", "stream", "loop", "stream_loop", "aper"),
        default="loop",
    )
    parser.add_argument("--skill-file", action="append", default=[], help="Path to skill markdown/text file.")
    parser.add_argument("--skills-dir", default="", help="Directory containing skill files (*.md).")
    parser.add_argument("--mcp-stdio", action="append", default=[], help="MCP stdio server as name:command:arg1,arg2")
    parser.add_argument("--mcp-sse", action="append", default=[], help="MCP sse server as name:url")
    parser.add_argument("--mcp-discover", action="store_true", help="Enable MCP config auto-discovery.")
    parser.add_argument("--mcp-config", default="config/mcp-config.json")
    parser.add_argument("--mcp-server-names", default="", help="Comma list for discovered MCP server names.")
    parser.add_argument("--a2a-urls", default="", help="Comma-separated A2A remote tool URLs.")
    parser.add_argument("--a2a-auth-token", default="")
    parser.add_argument("--enable-system-tools", action="store_true")
    parser.add_argument("--disable-system-tools", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--tags", default="")
    parser.add_argument("--aper-max-turns", type=int, default=8)
    parser.add_argument("--aper-execute-template", default="")
    parser.add_argument(
        "--disable-lifecycle-logs",
        action="store_true",
        help="Disable runtime/APER lifecycle progress logs.",
    )
    return parser


def _parse_stdio(raw: str) -> tuple[str, str, list[str]]:
    parts = raw.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"Invalid --mcp-stdio format: {raw}")
    name = parts[0].strip()
    command = parts[1].strip()
    args: list[str] = []
    if len(parts) == 3 and parts[2]:
        args = [part.strip() for part in parts[2].split(",") if part.strip()]
    return name, command, args


def _parse_sse(raw: str) -> tuple[str, str]:
    parts = raw.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid --mcp-sse format: {raw}")
    return parts[0].strip(), parts[1].strip()


def main() -> None:
    args = build_parser().parse_args()
    backend = args.backend
    assert backend in ("copilot", "claude", "openai", "moonshot", "localllm")
    backend_name: BackendName = backend

    enable_system_tools = True
    if args.disable_system_tools:
        enable_system_tools = False
    elif args.enable_system_tools:
        enable_system_tools = True

    builder = AgentBuilder().with_identity(
        name=str(args.name),
        backend=backend_name,
        memory_namespace=f"builder:{backend_name}:{args.name}",
    ).with_runtime_options(
        max_iterations=int(args.max_iterations),
        timeout_seconds=float(args.timeout_seconds),
        enable_system_tools=enable_system_tools,
        tags=[tag.strip() for tag in str(args.tags).split(",") if tag.strip()],
    ).with_lifecycle_logs(not bool(args.disable_lifecycle_logs))

    aper_execute_template = str(args.aper_execute_template).strip()
    if aper_execute_template:
        builder.with_aper_profile(
            APERProfile(
                execute_template=aper_execute_template,
                max_turns=int(args.aper_max_turns),
            )
        )
    else:
        builder.with_aper_profile(APERProfile(max_turns=int(args.aper_max_turns)))

    for path in args.skill_file:
        builder.with_skill_file(str(path))
    if str(args.skills_dir).strip():
        builder.with_skills_from_dir(str(args.skills_dir))

    for raw in args.mcp_stdio:
        name, command, mcp_args = _parse_stdio(str(raw))
        builder.with_mcp_stdio_server(name=name, command=command, args=mcp_args)

    for raw in args.mcp_sse:
        name, url = _parse_sse(str(raw))
        builder.with_mcp_sse_server(name=name, url=url)

    if args.mcp_discover:
        names = [entry.strip() for entry in str(args.mcp_server_names).split(",") if entry.strip()]
        builder.with_mcp_discovery(
            config_path=str(args.mcp_config),
            server_names=names,
        )

    if str(args.a2a_urls).strip():
        urls = [entry.strip() for entry in str(args.a2a_urls).split(",") if entry.strip()]
        token = str(args.a2a_auth_token).strip()
        builder.with_a2a_remote_tools(urls=urls, auth_token=(token or None))

    mode = str(args.mode)
    assert mode in ("run", "stream", "loop", "stream_loop", "aper")
    result = asyncio.run(builder.run(str(args.prompt), mode=mode))
    print(result)


if __name__ == "__main__":
    main()
