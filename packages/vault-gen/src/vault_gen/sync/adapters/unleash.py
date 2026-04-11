"""Unleash feature-flag sync adapter.

Bridges a vault-gen config repo's ``flags/`` directory to an Unleash instance
via the Unleash admin API.

Flag format in repo (one TOML file per flag in ``flags_dir``)::

    # flags/dark-mode.toml
    name = "dark-mode"
    description = "Enable dark mode for users"
    type = "release"
    enabled = true

    [[strategies]]
    name = "default"

Auth: set ``VAULT_GEN_UNLEASH_TOKEN`` in the environment.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import structlog
from pydantic import BaseModel

from vault_gen.access.repo import RepoAccess
from vault_gen.sync.base import Change, SyncAdapter, SyncResult

log = structlog.get_logger()

_TOKEN_ENV = "VAULT_GEN_UNLEASH_TOKEN"


# ---------------------------------------------------------------------------
# Config / data models
# ---------------------------------------------------------------------------


class UnleashAdapterConfig(BaseModel, extra="forbid"):
    base_url: str
    project: str = "default"
    environment: str = "development"
    flags_dir: str = "flags/"


class FlagSpec(BaseModel, extra="ignore"):
    """Representation of a feature flag — both in repo TOML and Unleash API JSON."""

    name: str
    description: str = ""
    type: str = "release"
    enabled: bool = True
    strategies: list[dict[str, object]] = []


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class UnleashAdapter(SyncAdapter):
    """Sync adapter for Unleash feature flags.

    Reads flags from ``flags_dir`` in the repo (TOML files, one per flag) and
    syncs them to/from an Unleash admin API.
    """

    ADAPTER_NAME = "unleash"

    @property
    def name(self) -> str:
        return self.ADAPTER_NAME

    async def push(self, repo: RepoAccess, config: dict[str, object]) -> SyncResult:
        """Push repo flag state to Unleash. Creates/updates/archives as needed."""
        cfg = UnleashAdapterConfig.model_validate(config)
        token = _require_token()

        repo_flags = _read_flags(repo, cfg.flags_dir)

        async with _client(cfg.base_url, token) as client:
            try:
                remote_flags = await _fetch_flags(client, cfg)
            except httpx.HTTPStatusError as exc:
                return SyncResult(
                    success=False,
                    adapter=self.name,
                    error=f"Failed to fetch Unleash flags: HTTP {exc.response.status_code}",
                )

            applied: list[Change] = []
            flags_dir = cfg.flags_dir.rstrip("/")

            for flag_name, spec in repo_flags.items():
                file_path = f"{flags_dir}/{flag_name}.toml"
                if flag_name not in remote_flags:
                    try:
                        await _create_flag(client, cfg, spec)
                        applied.append(
                            Change(path=file_path, action="add", detail=f"created '{flag_name}'")
                        )
                    except httpx.HTTPStatusError as exc:
                        return SyncResult(
                            success=False,
                            adapter=self.name,
                            changes=tuple(applied),
                            error=f"Failed to create '{flag_name}': HTTP {exc.response.status_code}: {exc.response.text}",
                        )
                elif _flag_differs(spec, remote_flags[flag_name]):
                    try:
                        await _update_flag(client, cfg, spec)
                        applied.append(
                            Change(path=file_path, action="update", detail=f"updated '{flag_name}'")
                        )
                    except httpx.HTTPStatusError as exc:
                        return SyncResult(
                            success=False,
                            adapter=self.name,
                            changes=tuple(applied),
                            error=f"Failed to update '{flag_name}': HTTP {exc.response.status_code}: {exc.response.text}",
                        )

            # Archive flags present in Unleash but not in repo.
            for flag_name in remote_flags:
                if flag_name not in repo_flags:
                    file_path = f"{flags_dir}/{flag_name}.toml"
                    try:
                        await _archive_flag(client, cfg, flag_name)
                        applied.append(
                            Change(
                                path=file_path,
                                action="remove",
                                detail=f"archived '{flag_name}' (not in repo)",
                            )
                        )
                    except httpx.HTTPStatusError as exc:
                        return SyncResult(
                            success=False,
                            adapter=self.name,
                            changes=tuple(applied),
                            error=f"Failed to archive '{flag_name}': HTTP {exc.response.status_code}: {exc.response.text}",
                        )

        return SyncResult(success=True, adapter=self.name, changes=tuple(applied))

    async def pull(self, repo: RepoAccess, config: dict[str, object]) -> SyncResult:
        """Pull Unleash flags into the repo as TOML files."""
        cfg = UnleashAdapterConfig.model_validate(config)
        token = _require_token()
        flags_dir = cfg.flags_dir.rstrip("/")

        async with _client(cfg.base_url, token) as client:
            try:
                remote_flags = await _fetch_flags(client, cfg)
            except httpx.HTTPStatusError as exc:
                return SyncResult(
                    success=False,
                    adapter=self.name,
                    error=f"Failed to fetch Unleash flags: HTTP {exc.response.status_code}",
                )

        applied: list[Change] = []
        for flag in remote_flags.values():
            file_path = f"{flags_dir}/{flag.name}.toml"
            new_content = _flag_to_toml(flag)

            try:
                existing = repo.read(file_path)
                if existing == new_content:
                    continue
                action = "update"
            except FileNotFoundError:
                action = "add"

            repo.write(
                file_path,
                new_content,
                commit_msg=f"sync(unleash): {action} flag '{flag.name}'",
            )
            applied.append(Change(path=file_path, action=action, detail=f"from Unleash"))

        return SyncResult(success=True, adapter=self.name, changes=tuple(applied))

    async def diff(self, repo: RepoAccess, config: dict[str, object]) -> list[Change]:
        """Return what would change on push without applying anything."""
        cfg = UnleashAdapterConfig.model_validate(config)
        token = _require_token()

        repo_flags = _read_flags(repo, cfg.flags_dir)

        async with _client(cfg.base_url, token) as client:
            remote_flags = await _fetch_flags(client, cfg)

        flags_dir = cfg.flags_dir.rstrip("/")
        changes: list[Change] = []

        for flag_name, spec in repo_flags.items():
            file_path = f"{flags_dir}/{flag_name}.toml"
            if flag_name not in remote_flags:
                changes.append(
                    Change(path=file_path, action="add", detail=f"'{flag_name}' not in Unleash")
                )
            elif _flag_differs(spec, remote_flags[flag_name]):
                changes.append(
                    Change(
                        path=file_path,
                        action="update",
                        detail=f"'{flag_name}' differs from Unleash state",
                    )
                )

        for flag_name in remote_flags:
            if flag_name not in repo_flags:
                changes.append(
                    Change(
                        path=f"{flags_dir}/{flag_name}.toml",
                        action="remove",
                        detail=f"'{flag_name}' in Unleash but not in repo — will archive",
                    )
                )

        return changes


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_token() -> str:
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(
            f"Unleash API token not found. "
            f"Set the {_TOKEN_ENV} environment variable before syncing."
        )
    return token


@asynccontextmanager
async def _client(base_url: str, token: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield a configured httpx.AsyncClient for the Unleash admin API."""
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        yield client


def _read_flags(repo: RepoAccess, flags_dir: str) -> dict[str, FlagSpec]:
    """Parse all TOML flag files from flags_dir in the repo."""
    pattern = flags_dir.rstrip("/") + "/*.toml"
    flags: dict[str, FlagSpec] = {}
    for file_path in repo.list_files(pattern):
        try:
            data = tomllib.loads(repo.read(file_path))
            spec = FlagSpec.model_validate(data)
            flags[spec.name] = spec
        except Exception as exc:
            log.warning("skipping unparseable flag file", path=file_path, error=str(exc))
    return flags


async def _fetch_flags(
    client: httpx.AsyncClient, cfg: UnleashAdapterConfig
) -> dict[str, FlagSpec]:
    """Fetch all feature flags from Unleash for the configured project."""
    resp = await client.get(f"/api/admin/projects/{cfg.project}/features")
    resp.raise_for_status()
    data = resp.json()
    flags: dict[str, FlagSpec] = {}
    for feature in data.get("features", []):
        spec = FlagSpec.model_validate(feature)
        flags[spec.name] = spec
    return flags


async def _create_flag(
    client: httpx.AsyncClient, cfg: UnleashAdapterConfig, spec: FlagSpec
) -> None:
    """Create a new feature flag in Unleash."""
    payload: dict[str, object] = {
        "name": spec.name,
        "description": spec.description,
        "type": spec.type,
    }
    resp = await client.post(
        f"/api/admin/projects/{cfg.project}/features",
        json=payload,
    )
    resp.raise_for_status()

    # Apply enable/disable state for the environment.
    toggle = "on" if spec.enabled else "off"
    env_resp = await client.post(
        f"/api/admin/projects/{cfg.project}/features/{spec.name}"
        f"/environments/{cfg.environment}/{toggle}"
    )
    env_resp.raise_for_status()

    # Add strategies if specified.
    for strategy in spec.strategies:
        strat_resp = await client.post(
            f"/api/admin/projects/{cfg.project}/features/{spec.name}"
            f"/environments/{cfg.environment}/strategies",
            json=strategy,
        )
        strat_resp.raise_for_status()


async def _update_flag(
    client: httpx.AsyncClient, cfg: UnleashAdapterConfig, spec: FlagSpec
) -> None:
    """Update an existing feature flag in Unleash."""
    payload: dict[str, object] = {
        "description": spec.description,
        "type": spec.type,
    }
    resp = await client.put(
        f"/api/admin/projects/{cfg.project}/features/{spec.name}",
        json=payload,
    )
    resp.raise_for_status()

    # Sync enable/disable state.
    toggle = "on" if spec.enabled else "off"
    env_resp = await client.post(
        f"/api/admin/projects/{cfg.project}/features/{spec.name}"
        f"/environments/{cfg.environment}/{toggle}"
    )
    env_resp.raise_for_status()


async def _archive_flag(
    client: httpx.AsyncClient, cfg: UnleashAdapterConfig, flag_name: str
) -> None:
    """Archive (soft-delete) a feature flag in Unleash."""
    resp = await client.delete(
        f"/api/admin/projects/{cfg.project}/features/{flag_name}"
    )
    resp.raise_for_status()


def _flag_to_toml(flag: FlagSpec) -> str:
    """Serialise a FlagSpec to the canonical TOML format."""
    # Escape double-quotes in string values.
    def q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        f'name = "{q(flag.name)}"',
        f'description = "{q(flag.description)}"',
        f'type = "{q(flag.type)}"',
        f"enabled = {str(flag.enabled).lower()}",
    ]

    for strategy in flag.strategies:
        lines.append("")
        lines.append("[[strategies]]")
        for k, v in strategy.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{q(v)}"')
            elif isinstance(v, bool):
                lines.append(f"{k} = {str(v).lower()}")
            elif isinstance(v, int | float):
                lines.append(f"{k} = {v}")
            # Skip nested dicts — Unleash strategy parameters can be complex;
            # they're preserved on pull but not round-tripped here.

    return "\n".join(lines) + "\n"


def _flag_differs(repo_flag: FlagSpec, remote_flag: FlagSpec) -> bool:
    """Return True if the repo flag differs from the remote flag in any tracked field."""
    return (
        repo_flag.description != remote_flag.description
        or repo_flag.type != remote_flag.type
        or repo_flag.enabled != remote_flag.enabled
    )
