"""obscura.a2a.definition — Declarative agent definition for A2A.

This module is the single source of truth for what skills and capabilities
the Obscura A2A agent advertises at the well-known discovery endpoint.
``AgentDefinition`` converts directly to an ``AgentCard`` via
``AgentCardGenerator``, so the TOML config file, the Python constant, and
the HTTP response all stay in sync automatically.

Usage::

    from obscura.integrations.a2a.definition import DEFAULT_AGENT_DEFINITION

    card = DEFAULT_AGENT_DEFINITION.to_agent_card(base_url="https://my-agent.example.com")
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.types import AgentSkill

if TYPE_CHECKING:
    from obscura.integrations.a2a.types import AgentCard


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SkillDefinition:
    """Declarative description of a single agent skill.

    Parameters
    ----------
    id:
        Machine-readable skill identifier (e.g. ``"code-execution"``).
    name:
        Human-readable skill name.
    description:
        Short description of what the skill does.
    tags:
        Categorical tags for discovery and routing.
    examples:
        Example prompts that exercise this skill.
    """

    id: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_agent_skill(self) -> AgentSkill:
        """Convert to the wire-format ``AgentSkill`` model."""
        return AgentSkill(
            id=self.id,
            name=self.name,
            description=self.description,
            tags=self.tags,
            examples=self.examples,
        )


@dataclass
class AgentDefinition:
    """Declarative agent definition — source of truth for the well-known card.

    Parameters
    ----------
    name:
        Agent display name.
    description:
        Human-readable description of the agent.
    version:
        Semver version string (e.g. ``"1.0"``).
    url:
        Base URL where the A2A server is hosted. Can be set later via
        ``to_agent_card(base_url=...)``.
    skills:
        List of skills the agent advertises.
    streaming:
        Whether the agent supports streaming responses.
    push_notifications:
        Whether the agent supports push notifications.
    auth_required:
        Whether bearer auth is required to call the agent.
    provider_name:
        Name of the provider organisation.
    provider_url:
        URL of the provider organisation.
    """

    name: str
    description: str
    version: str = "1.0"
    url: str = ""
    skills: list[SkillDefinition] = field(default_factory=list)
    streaming: bool = True
    push_notifications: bool = False
    auth_required: bool = True
    provider_name: str = "Obscura"
    provider_url: str = "https://obscura.dev"

    def to_agent_card(self, base_url: str | None = None) -> AgentCard:
        """Convert this definition to a wire-format ``AgentCard``.

        Parameters
        ----------
        base_url:
            Override the ``url`` field at card-build time. Useful when the
            definition is loaded from a static file and the runtime URL is
            known only at startup.
        """
        effective_url = base_url if base_url is not None else self.url
        agent_skills = [s.to_agent_skill() for s in self.skills]

        builder = (
            AgentCardGenerator(
                name=self.name,
                url=effective_url,
                description=self.description,
                version=self.version,
            )
            .with_skills(agent_skills)
            .with_capabilities(
                streaming=self.streaming,
                push_notifications=self.push_notifications,
                extended_card=False,
            )
        )

        if self.auth_required:
            builder = builder.with_bearer_auth()

        builder = builder.with_provider(self.provider_name, self.provider_url)

        return builder.build()


# ---------------------------------------------------------------------------
# Default definition
# ---------------------------------------------------------------------------


DEFAULT_AGENT_DEFINITION = AgentDefinition(
    name="Obscura Agent",
    description=(
        "A general-purpose AI agent powered by Obscura with access to 100+ tools "
        "for code execution, file management, web search, data analysis, and more."
    ),
    version="1.0",
    push_notifications=True,
    skills=[
        SkillDefinition(
            id="code-execution",
            name="Code Execution",
            description="Execute Python, shell, and other code snippets safely",
            tags=["code", "execution", "python"],
        ),
        SkillDefinition(
            id="file-management",
            name="File Management",
            description="Read, write, search, and manage files and directories",
            tags=["files", "io", "filesystem"],
        ),
        SkillDefinition(
            id="web-search",
            name="Web Search",
            description="Search the web and fetch URLs for up-to-date information",
            tags=["search", "web", "research"],
        ),
        SkillDefinition(
            id="data-analysis",
            name="Data Analysis",
            description="Query and analyze structured data with SQL and Python",
            tags=["data", "sql", "analytics"],
        ),
        SkillDefinition(
            id="tool-use",
            name="Tool Use",
            description="Invoke any of 100+ registered Obscura tools to complete tasks",
            tags=["tools", "automation", "integration"],
        ),
    ],
)


# ---------------------------------------------------------------------------
# TOML loader
# ---------------------------------------------------------------------------


def load_definition_from_toml(path: str | Path) -> AgentDefinition:
    """Load an ``AgentDefinition`` from a TOML configuration file.

    The expected TOML structure is::

        [agent]
        name = "My Agent"
        description = "..."
        version = "1.0"
        url = "https://example.com"
        streaming = true
        push_notifications = false
        auth_required = true
        provider_name = "My Org"
        provider_url = "https://example.com"

        [[agent.skills]]
        id = "search"
        name = "Search"
        description = "Search the web"
        tags = ["search"]
        examples = ["Search for X"]

    Parameters
    ----------
    path:
        Filesystem path to the TOML file.

    Returns
    -------
    AgentDefinition
        Fully populated definition loaded from the file.

    Raises
    ------
    ImportError
        If no TOML parsing library is available (requires Python 3.11+ stdlib
        ``tomllib`` or the ``tomli`` back-port package).
    FileNotFoundError
        If the file does not exist.
    KeyError
        If the required ``[agent]`` table is missing.
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError as exc:
            msg = (
                "No TOML library found. On Python < 3.11 install tomli: "
                "pip install tomli"
            )
            raise ImportError(msg) from exc

    toml_path = Path(path)
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)

    agent_data: dict = data["agent"]

    raw_skills: list[dict] = agent_data.get("skills", [])
    skills = [
        SkillDefinition(
            id=s["id"],
            name=s["name"],
            description=s.get("description", ""),
            tags=s.get("tags", []),
            examples=s.get("examples", []),
        )
        for s in raw_skills
    ]

    return AgentDefinition(
        name=agent_data["name"],
        description=agent_data.get("description", ""),
        version=agent_data.get("version", "1.0"),
        url=agent_data.get("url", ""),
        skills=skills,
        streaming=agent_data.get("streaming", True),
        push_notifications=agent_data.get("push_notifications", False),
        auth_required=agent_data.get("auth_required", True),
        provider_name=agent_data.get("provider_name", "Obscura"),
        provider_url=agent_data.get("provider_url", "https://obscura.dev"),
    )


# ---------------------------------------------------------------------------
# OpenClaw-compatible definition factory
# ---------------------------------------------------------------------------


def openclaw_compatible_definition(
    base_url: str = "http://localhost:8080",
    *,
    gateway_url: str = "http://localhost:18789",
) -> AgentDefinition:
    """Build an ``AgentDefinition`` tuned for OpenClaw gateway discovery.

    Returns an ``AgentDefinition`` that:

    - Sets ``url`` to ``base_url`` (the Obscura standalone A2A server)
    - Extends the default skills with an ``"openclaw"`` tag so OpenClaw's
      skill router can match them
    - Adds a ``"kimi-k2"`` tag to the tool-use skill, advertising that the
      backend can use the Kimi K2.5 model via the OpenClaw gateway
    - Sets ``provider_name`` to ``"Obscura / OpenClaw"``

    Parameters
    ----------
    base_url:
        Public URL of the Obscura standalone A2A server.
    gateway_url:
        OpenClaw gateway URL (informational; embedded in the description).
    """
    # Add "openclaw" tag to all skills
    skills_with_oc = [
        replace(s, tags=[*s.tags, "openclaw"]) for s in DEFAULT_AGENT_DEFINITION.skills
    ]
    # Add "kimi-k2" tag specifically to the tool-use skill
    skills_with_oc = [
        replace(s, tags=[*s.tags, "kimi-k2"]) if s.id == "tool-use" else s
        for s in skills_with_oc
    ]
    return AgentDefinition(
        name="Obscura Agent (OpenClaw)",
        description=(
            f"Obscura agent integrated with OpenClaw gateway at {gateway_url}. "
            "Supports Kimi K2.5, Claude, and Copilot backends via OpenClaw model routing."
        ),
        version=DEFAULT_AGENT_DEFINITION.version,
        url=base_url,
        skills=skills_with_oc,
        streaming=DEFAULT_AGENT_DEFINITION.streaming,
        push_notifications=DEFAULT_AGENT_DEFINITION.push_notifications,
        auth_required=DEFAULT_AGENT_DEFINITION.auth_required,
        provider_name="Obscura / OpenClaw",
        provider_url=DEFAULT_AGENT_DEFINITION.provider_url,
    )


__all__ = [
    "AgentDefinition",
    "DEFAULT_AGENT_DEFINITION",
    "SkillDefinition",
    "load_definition_from_toml",
    "openclaw_compatible_definition",
]
