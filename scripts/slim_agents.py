#!/usr/bin/env python3
"""Slim agents.yaml: strip redundant fields, split into enabled + available catalog."""

from __future__ import annotations

import argparse
import copy
import sys
from collections import Counter
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Custom YAML dumper: flow-style for short lists, block scalar for prompts
# ---------------------------------------------------------------------------

class CompactDumper(yaml.SafeDumper):
    pass


def _represent_short_list(dumper: CompactDumper, data: list) -> yaml.Node:
    """Use flow style for short lists of short strings."""
    if (
        len(data) <= 6
        and all(isinstance(item, str) and len(item) < 40 for item in data)
    ):
        return dumper.represent_sequence(
            "tag:yaml.org,2002:seq", data, flow_style=True
        )
    return dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=False
    )


class _LiteralStr(str):
    """Marker for block-scalar strings."""


def _represent_literal(dumper: CompactDumper, data: _LiteralStr) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")


CompactDumper.add_representer(list, _represent_short_list)
CompactDumper.add_representer(_LiteralStr, _represent_literal)


# ---------------------------------------------------------------------------
# Model defaults — fields stripped when they match these values
# ---------------------------------------------------------------------------

MODEL_DEFAULTS: dict[str, object] = {
    "type": "loop",
    "provider": "copilot",
    "can_delegate": False,
    "max_delegation_depth": 3,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_empty(val: object) -> bool:
    """Return True for [], {}, None."""
    if val is None:
        return True
    if isinstance(val, (list, dict)) and len(val) == 0:
        return True
    return False


def _clean_prompt(text: str) -> str:
    """Strip trailing whitespace/newlines from system_prompt values."""
    return text.rstrip()


def _compute_defaults(agents: list[dict]) -> dict:
    """Compute the most common values for defaultable fields."""
    defaults: dict = {}

    # mcp_servers
    mcp_counter: Counter[str] = Counter()
    for a in agents:
        v = a.get("mcp_servers")
        if isinstance(v, str):
            mcp_counter[v] += 1
    if mcp_counter:
        top_mcp, top_count = mcp_counter.most_common(1)[0]
        # Only default if >50% of agents that set it use this value
        mcp_set_count = sum(1 for a in agents if isinstance(a.get("mcp_servers"), str))
        if top_count > mcp_set_count * 0.5:
            defaults["mcp_servers"] = top_mcp

    # skills.lazy_load
    ll_counter: Counter[bool] = Counter()
    for a in agents:
        s = a.get("skills")
        if isinstance(s, dict) and "lazy_load" in s:
            ll_counter[s["lazy_load"]] += 1
    if ll_counter:
        top_ll, _ = ll_counter.most_common(1)[0]
        defaults.setdefault("skills", {})["lazy_load"] = top_ll

    # capabilities.grant
    grant_counter: Counter[tuple[str, ...]] = Counter()
    for a in agents:
        g = a.get("capabilities", {}).get("grant", [])
        grant_counter[tuple(sorted(g))] += 1
    if grant_counter:
        top_grant, top_grant_count = grant_counter.most_common(1)[0]
        # Only default if it appears in >10% of agents
        if top_grant_count >= len(agents) * 0.05:
            defaults.setdefault("capabilities", {})["grant"] = list(top_grant)

    return defaults


def _strip_agent(agent: dict, defaults: dict) -> dict:
    """Strip redundant fields from a single agent dict."""
    a = copy.deepcopy(agent)

    # Remove 'enabled' — semantics come from which file it's in
    a.pop("enabled", None)

    # Remove model defaults
    for key, default_val in MODEL_DEFAULTS.items():
        if a.get(key) == default_val:
            del a[key]

    # Clean system_prompt
    if "system_prompt" in a:
        cleaned = _clean_prompt(a["system_prompt"])
        if "\n" in cleaned:
            a["system_prompt"] = _LiteralStr(cleaned)
        else:
            a["system_prompt"] = cleaned

    # Strip empty arrays and dicts from nested structures
    for section in ("capabilities", "plugins", "permissions", "skills"):
        container = a.get(section)
        if not isinstance(container, dict):
            continue
        for k in list(container.keys()):
            if _is_empty(container[k]):
                del container[k]
        if not container:
            del a[section]

    # Strip fields matching computed defaults
    # mcp_servers
    if "mcp_servers" in defaults and a.get("mcp_servers") == defaults["mcp_servers"]:
        del a["mcp_servers"]

    # skills.lazy_load
    if "skills" in defaults and isinstance(a.get("skills"), dict):
        default_ll = defaults["skills"].get("lazy_load")
        if a["skills"].get("lazy_load") == default_ll:
            del a["skills"]["lazy_load"]
            if not a["skills"]:
                del a["skills"]

    # capabilities.grant
    if "capabilities" in defaults and isinstance(a.get("capabilities"), dict):
        default_grant = defaults["capabilities"].get("grant")
        if default_grant is not None:
            agent_grant = a["capabilities"].get("grant", [])
            if sorted(agent_grant) == sorted(default_grant):
                del a["capabilities"]["grant"]
                if not a["capabilities"]:
                    del a["capabilities"]

    return a


def _dump_yaml(data: dict) -> str:
    """Dump dict to YAML string with compact style."""
    return yaml.dump(
        data,
        Dumper=CompactDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Slim agents.yaml: strip redundant fields, split enabled/available."
    )
    parser.add_argument("path", type=Path, help="Path to agents.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats but don't write files",
    )
    args = parser.parse_args()

    input_path: Path = args.path.expanduser().resolve()
    if not input_path.exists():
        print(f"Error: {input_path} does not exist", file=sys.stderr)
        sys.exit(1)

    with open(input_path) as f:
        raw = yaml.safe_load(f)

    agents: list[dict] = raw.get("agents", [])
    if not agents:
        print("No agents found.", file=sys.stderr)
        sys.exit(1)

    enabled = [a for a in agents if a.get("enabled", True)]
    disabled = [a for a in agents if not a.get("enabled", True)]

    # Compute defaults from ALL agents
    defaults = _compute_defaults(agents)

    # Strip each agent
    slim_enabled = [_strip_agent(a, defaults) for a in enabled]
    slim_disabled = [_strip_agent(a, defaults) for a in disabled]

    # Compute original vs slim sizes
    orig_yaml = yaml.dump(raw, Dumper=yaml.SafeDumper, default_flow_style=False)
    orig_lines = len(orig_yaml.splitlines())

    enabled_doc = {"defaults": defaults, "agents": slim_enabled}
    disabled_doc = {"defaults": defaults, "agents": slim_disabled}

    enabled_yaml = _dump_yaml(enabled_doc)
    disabled_yaml = _dump_yaml(disabled_doc)

    enabled_lines = len(enabled_yaml.splitlines())
    disabled_lines = len(disabled_yaml.splitlines())

    # Stats
    print(f"Input:    {input_path}")
    print(f"Agents:   {len(agents)} total, {len(enabled)} enabled, {len(disabled)} disabled")
    print(f"Original: {orig_lines} lines")
    print(f"Enabled:  {enabled_lines} lines  ({len(slim_enabled)} agents)")
    print(f"Disabled: {disabled_lines} lines  ({len(slim_disabled)} agents)")
    print(f"Savings:  {orig_lines - enabled_lines - disabled_lines:+d} lines "
          f"({100 * (1 - (enabled_lines + disabled_lines) / orig_lines):.0f}% smaller)")
    print(f"Defaults: {defaults}")

    if args.dry_run:
        print("\n[dry-run] No files written.")
        return

    # Write output files
    out_dir = input_path.parent
    enabled_path = out_dir / "agents.yaml"
    disabled_path = out_dir / "agents-available.yaml"

    with open(enabled_path, "w") as f:
        f.write(enabled_yaml)
    print(f"\nWrote: {enabled_path}")

    with open(disabled_path, "w") as f:
        f.write(disabled_yaml)
    print(f"Wrote: {disabled_path}")


if __name__ == "__main__":
    main()
