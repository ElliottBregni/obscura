"""Load memory channel definitions from config.toml and workspace specs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from obscura.memory_channels.models import ChannelTriggers, MemoryChannel

logger = logging.getLogger(__name__)


def load_channels_from_config() -> list[MemoryChannel]:
    """Read ``[memory.channels.*]`` from ``~/.obscura/config.toml``.

    Returns an empty list if no channels are configured (backward-compat).
    """
    try:
        from obscura.core.config_io import try_load_config

        home_cfg = try_load_config(Path.home() / ".obscura" / "config.toml")
        if not home_cfg:
            return []

        # Also check project-level config
        project_cfg = try_load_config(Path(".obscura") / "config.toml")

        channels_raw = (home_cfg or {}).get("memory", {}).get("channels", {})
        # Merge project-level channels (project wins)
        if project_cfg:
            project_channels = project_cfg.get("memory", {}).get("channels", {})
            channels_raw = {**channels_raw, **project_channels}

        return _parse_channels(channels_raw)
    except Exception:
        logger.debug("Could not load memory channels from config", exc_info=True)
        return []


def load_channels_from_spec(spec_channels: list[dict[str, Any]]) -> list[MemoryChannel]:
    """Parse channel definitions from a compiled agent/workspace spec."""
    channels: list[MemoryChannel] = []
    for raw in spec_channels:
        try:
            channels.append(_parse_single_channel(raw.get("name", "unnamed"), raw))
        except Exception:
            logger.debug("Skipping malformed channel spec: %s", raw, exc_info=True)
    return channels


def merge_channels(
    global_channels: list[MemoryChannel],
    agent_channels: list[MemoryChannel],
) -> list[MemoryChannel]:
    """Merge agent-level channels with global channels.  Agent wins on name collision."""
    by_name: dict[str, MemoryChannel] = {c.name: c for c in global_channels}
    for c in agent_channels:
        by_name[c.name] = c
    return list(by_name.values())


# ---------------------------------------------------------------------------
# Internal parsing
# ---------------------------------------------------------------------------


def _parse_channels(raw: dict[str, Any]) -> list[MemoryChannel]:
    """Parse a dict of ``{channel_name: channel_config}`` into MemoryChannel list."""
    channels: list[MemoryChannel] = []
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        try:
            channels.append(_parse_single_channel(name, cast(dict[str, Any], cfg)))
        except Exception:
            logger.debug("Skipping malformed channel %s", name, exc_info=True)
    return channels


def _parse_single_channel(name: str, cfg: dict[str, Any]) -> MemoryChannel:
    """Parse a single channel config dict into a MemoryChannel."""
    triggers_raw_any = cfg.get("triggers", {})
    triggers_raw: dict[str, Any] = (
        cast(dict[str, Any], triggers_raw_any)
        if isinstance(triggers_raw_any, dict)
        else {}
    )

    # Support flat keys (TOML-friendly) or nested triggers dict
    file_globs = cast(
        list[Any], cfg.get("file_globs") or triggers_raw.get("file_globs", [])
    )
    keywords = cast(list[Any], cfg.get("keywords") or triggers_raw.get("keywords", []))
    tool_names = cast(
        list[Any], cfg.get("tool_names") or triggers_raw.get("tool_names", [])
    )
    always = cfg.get("always", triggers_raw.get("always", False))

    triggers = ChannelTriggers(
        file_globs=tuple(str(g) for g in file_globs),
        keywords=tuple(str(k) for k in keywords),
        tool_names=tuple(str(t) for t in tool_names),
        always=bool(always),
    )

    return MemoryChannel(
        name=name,
        namespace=cfg.get("namespace", f"channel:{name}"),
        triggers=triggers,
        query_template=cfg.get("query_template", "{query}"),
        max_tokens=int(cfg.get("max_tokens", 500)),
        injection=cfg.get("injection", "turn"),
        priority=int(cfg.get("priority", 50)),
        enabled=cfg.get("enabled", True),
    )
