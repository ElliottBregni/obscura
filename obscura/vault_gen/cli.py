from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import click
import structlog

from obscura.vault_gen.generator import RepoConfig, RepoType, generate_repo
from obscura.vault_gen.registry import Registry

log = structlog.get_logger()


def _configure_logging(verbose: bool) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.WARNING
        ),
    )


@click.group()
@click.version_option(package_name="vault-gen")
@click.option(
    "--verbose", "-v", is_flag=True, default=False, help="Enable debug logging."
)
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """vault-gen: scaffold Obsidian-compatible git-backed repos for Obscura."""
    ctx.ensure_object(dict)
    _configure_logging(verbose)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.option(
    "--type",
    "repo_type",
    type=click.Choice(["config", "vault"]),
    required=True,
    help="Repo type to scaffold.",
)
@click.option(
    "--path",
    "destination",
    type=click.Path(),
    default=None,
    help="Parent directory for the new repo. Defaults to cwd.",
)
def init(name: str, repo_type: str, destination: str | None) -> None:
    """Scaffold a new repo at <destination>/<name>."""
    dest = Path(destination).resolve() if destination else Path.cwd()
    config = RepoConfig(name=name, repo_type=RepoType(repo_type), destination=dest)
    try:
        repo_path = generate_repo(config)
    except FileExistsError as exc:
        log.debug("suppressed exception in init", exc_info=True)
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        log.debug("suppressed exception in init", exc_info=True)
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    registry = Registry.load()
    registry.add(name=name, path=repo_path, repo_type=config.repo_type.value)
    registry.save()

    click.echo(f"✓ Created {repo_type} repo '{name}' at {repo_path}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command("list")
def list_repos() -> None:
    """List all repos known to vault-gen."""
    registry = Registry.load()
    if not registry.entries:
        click.echo("No repos registered. Run `vault-gen init` to create one.")
        return

    for entry in registry.entries.values():
        link_suffix = f"  → obscura: {entry.obscura_path}" if entry.obscura_path else ""
        type_tag = f"[{entry.repo_type}]" if entry.repo_type else "[unknown]"
        click.echo(f"  {entry.name}  {type_tag}  {entry.path}{link_suffix}")


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option(
    "--obscura-path",
    required=True,
    type=click.Path(),
    help="Path to the Obscura instance to plug this repo into.",
)
def link(repo_path: str, obscura_path: str) -> None:
    """Register a repo as pluggable into an Obscura instance."""
    from obscura.vault_gen.access.repo import RepoAccess

    rp = Path(repo_path).resolve()
    op = Path(obscura_path).resolve()

    # Confirm it's a git repo and resolve its root.
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=rp,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(f"Error: {rp} is not inside a git repository.", err=True)
        sys.exit(1)
    git_root = Path(result.stdout.strip())

    registry = Registry.load()
    entry = registry.find_by_path(git_root)
    if entry is None:
        entry = registry.add(name=git_root.name, path=git_root, repo_type=None)

    entry.obscura_path = str(op)
    registry.save()

    access = RepoAccess(git_root)
    try:
        access.link_obsura(op)
        click.echo(f"✓ Linked {git_root}  →  Obscura at {op}")
    except Exception as exc:
        log.debug("suppressed exception in link", exc_info=True)
        click.echo(f"Warning: could not update Obscura config: {exc}", err=True)
        click.echo("Registry entry saved. Configure Obscura manually if needed.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def status(name: str) -> None:
    """Show git status, sync status, and Obscura link for a repo."""
    registry = Registry.load()
    entry = registry.entries.get(name)
    if entry is None:
        click.echo(
            f"Unknown repo '{name}'. Run `vault-gen list` to see registered repos."
        )
        sys.exit(1)

    path = Path(entry.path)
    if not path.exists():
        click.echo(f"Error: repo path does not exist: {path}", err=True)
        sys.exit(1)

    click.echo(f"Name:    {entry.name}")
    click.echo(f"Type:    {entry.repo_type or 'unknown'}")
    click.echo(f"Path:    {path}")
    click.echo(f"Linked:  {entry.obscura_path or '(not linked to Obscura)'}")
    click.echo("")

    # Git status
    gs = subprocess.run(
        ["git", "status", "--short"], cwd=path, capture_output=True, text=True
    )
    if gs.returncode == 0:
        dirty_lines = gs.stdout.strip()
        if dirty_lines:
            click.echo("Git status (dirty):")
            for line in dirty_lines.splitlines():
                click.echo(f"  {line}")
        else:
            click.echo("Git status: clean")
    else:
        click.echo(f"Git status: error — {gs.stderr.strip()}")

    # Remote
    remote = subprocess.run(
        ["git", "remote", "-v"], cwd=path, capture_output=True, text=True
    )
    click.echo("")
    if remote.returncode == 0 and remote.stdout.strip():
        click.echo("Remotes:")
        seen: set[str] = set()
        for line in remote.stdout.strip().splitlines():
            parts = line.split()
            remote_name = parts[0] if parts else ""
            remote_url = parts[1] if len(parts) > 1 else ""
            if remote_name not in seen:
                click.echo(f"  {remote_name}  {remote_url}")
                seen.add(remote_name)
    else:
        click.echo("Remote: (none configured)")


# ---------------------------------------------------------------------------
# sync command group
# ---------------------------------------------------------------------------


def _resolve_repo_path(repo_name: str) -> Path:
    """Look up a repo by name from the registry and return its path."""
    registry = Registry.load()
    entry = registry.entries.get(repo_name)
    if entry is None:
        click.echo(
            f"Unknown repo '{repo_name}'. Run `vault-gen list` to see registered repos.",
            err=True,
        )
        sys.exit(1)
    path = Path(entry.path)
    if not path.exists():
        click.echo(f"Error: repo path does not exist: {path}", err=True)
        sys.exit(1)
    return path


@main.group()
def sync() -> None:
    """Sync config repos to external backends (Unleash, etc.)."""


@sync.command("push")
@click.argument("repo_name")
@click.option(
    "--adapter",
    "adapter_name",
    default=None,
    help="Adapter to use (default: all enabled).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would change without applying.",
)
def sync_push(repo_name: str, adapter_name: str | None, dry_run: bool) -> None:
    """Push repo state to external backend(s)."""
    from obscura.vault_gen.access.repo import RepoAccess
    from obscura.vault_gen.sync.config import SyncConfig, SyncState
    from obscura.vault_gen.sync.registry import AdapterRegistry

    repo_path = _resolve_repo_path(repo_name)
    access = RepoAccess(repo_path, privilege="admin")
    sync_cfg = SyncConfig.load(repo_path)
    registry = AdapterRegistry()

    targets = (
        [sync_cfg.get_adapter_config(adapter_name)]
        if adapter_name
        else sync_cfg.enabled_adapters()
    )
    if not targets or targets == [None]:
        click.echo("No adapters enabled in sync.toml. Edit sync.toml to configure.")
        sys.exit(1)

    state = SyncState.load(repo_path)

    for adapter_cfg in targets:
        if adapter_cfg is None:
            click.echo(f"Adapter '{adapter_name}' not found in sync.toml.", err=True)
            continue

        try:
            adapter = registry.get_adapter(adapter_cfg.name)
        except KeyError as exc:
            log.debug("suppressed exception in sync_push", exc_info=True)
            click.echo(f"Error: {exc}", err=True)
            continue

        if dry_run:
            changes = asyncio.run(adapter.diff(access, adapter_cfg.config))
            if not changes:
                click.echo(f"[{adapter_cfg.name}] No changes.")
            else:
                click.echo(
                    f"[{adapter_cfg.name}] Would apply {len(changes)} change(s):"
                )
                for c in changes:
                    click.echo(f"  {c.action:8s}  {c.path}  {c.detail}")
        else:
            result = asyncio.run(adapter.push(access, adapter_cfg.config))
            if result.success:
                state.record_push(adapter_cfg.name, len(result.changes))
                if result.changes:
                    click.echo(
                        f"[{adapter_cfg.name}] Pushed {len(result.changes)} change(s):"
                    )
                    for c in result.changes:
                        click.echo(f"  {c.action:8s}  {c.path}")
                else:
                    click.echo(f"[{adapter_cfg.name}] Already in sync.")
            else:
                click.echo(
                    f"[{adapter_cfg.name}] Push failed: {result.error}", err=True
                )

    if not dry_run:
        state.save(repo_path)


@sync.command("pull")
@click.argument("repo_name")
@click.option(
    "--adapter",
    "adapter_name",
    default=None,
    help="Adapter to use (default: all enabled).",
)
def sync_pull(repo_name: str, adapter_name: str | None) -> None:
    """Pull backend state into the repo."""
    from obscura.vault_gen.access.repo import RepoAccess
    from obscura.vault_gen.sync.config import SyncConfig, SyncState
    from obscura.vault_gen.sync.registry import AdapterRegistry

    repo_path = _resolve_repo_path(repo_name)
    access = RepoAccess(repo_path, privilege="admin")
    sync_cfg = SyncConfig.load(repo_path)
    registry = AdapterRegistry()

    targets = (
        [sync_cfg.get_adapter_config(adapter_name)]
        if adapter_name
        else sync_cfg.enabled_adapters()
    )
    state = SyncState.load(repo_path)

    for adapter_cfg in targets:
        if adapter_cfg is None:
            click.echo(f"Adapter '{adapter_name}' not found in sync.toml.", err=True)
            continue
        try:
            adapter = registry.get_adapter(adapter_cfg.name)
        except KeyError as exc:
            log.debug("suppressed exception in sync_pull", exc_info=True)
            click.echo(f"Error: {exc}", err=True)
            continue

        result = asyncio.run(adapter.pull(access, adapter_cfg.config))
        if result.success:
            state.record_pull(adapter_cfg.name, len(result.changes))
            if result.changes:
                click.echo(
                    f"[{adapter_cfg.name}] Pulled {len(result.changes)} change(s):"
                )
                for c in result.changes:
                    click.echo(f"  {c.action:8s}  {c.path}")
            else:
                click.echo(f"[{adapter_cfg.name}] Already in sync.")
        else:
            click.echo(f"[{adapter_cfg.name}] Pull failed: {result.error}", err=True)

    state.save(repo_path)


@sync.command("diff")
@click.argument("repo_name")
@click.option(
    "--adapter", "adapter_name", default=None, help="Adapter to diff against."
)
def sync_diff(repo_name: str, adapter_name: str | None) -> None:
    """Show what would change on push without applying anything."""
    from obscura.vault_gen.access.repo import RepoAccess
    from obscura.vault_gen.sync.config import SyncConfig
    from obscura.vault_gen.sync.registry import AdapterRegistry

    repo_path = _resolve_repo_path(repo_name)
    access = RepoAccess(repo_path)
    sync_cfg = SyncConfig.load(repo_path)
    registry = AdapterRegistry()

    targets = (
        [sync_cfg.get_adapter_config(adapter_name)]
        if adapter_name
        else sync_cfg.enabled_adapters()
    )

    for adapter_cfg in targets:
        if adapter_cfg is None:
            click.echo(f"Adapter '{adapter_name}' not found in sync.toml.", err=True)
            continue
        try:
            adapter = registry.get_adapter(adapter_cfg.name)
        except KeyError as exc:
            log.debug("suppressed exception in sync_diff", exc_info=True)
            click.echo(f"Error: {exc}", err=True)
            continue

        changes = asyncio.run(adapter.diff(access, adapter_cfg.config))
        if not changes:
            click.echo(f"[{adapter_cfg.name}] In sync — no changes.")
        else:
            click.echo(f"[{adapter_cfg.name}] {len(changes)} change(s) pending:")
            for c in changes:
                click.echo(f"  {c.action:8s}  {c.path}  {c.detail}")


@sync.command("status")
@click.argument("repo_name")
def sync_status(repo_name: str) -> None:
    """Show all registered adapters and their last sync times."""
    from obscura.vault_gen.sync.config import SyncConfig, SyncState
    from obscura.vault_gen.sync.registry import AdapterRegistry

    repo_path = _resolve_repo_path(repo_name)
    sync_cfg = SyncConfig.load(repo_path)
    sync_state = SyncState.load(repo_path)
    registry = AdapterRegistry()

    click.echo(f"Repo: {repo_name}  ({repo_path})")
    click.echo(f"Available adapters: {', '.join(registry.list_adapters())}")
    click.echo("")

    if not sync_cfg.adapters:
        click.echo("No adapters configured in sync.toml.")
        return

    for adapter_cfg in sync_cfg.adapters:
        status_tag = "enabled" if adapter_cfg.enabled else "disabled"
        adapter_state = sync_state.adapters.get(adapter_cfg.name)
        last_push = adapter_state.last_push if adapter_state else None
        last_pull = adapter_state.last_pull if adapter_state else None
        click.echo(f"  {adapter_cfg.name}  [{status_tag}]")
        click.echo(f"    last push: {last_push or 'never'}")
        click.echo(f"    last pull: {last_pull or 'never'}")
