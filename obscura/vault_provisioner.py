"""obscura.vault_provisioner — Thin adapter over vault-gen.

Obscura calls ``provision_vault`` to scaffold a new git-backed repo.
Nothing else in Obscura should import vault_gen directly; all coupling
is contained here.

When vault-gen gets a published release, swap the file:// dep in
pyproject.toml for a versioned reference and nothing here changes.
"""

from __future__ import annotations

from pathlib import Path

from vault_gen.generator import RepoConfig, RepoType, generate_repo
from vault_gen.registry import Registry


class VaultProvisionError(RuntimeError):
    """Raised when vault scaffolding fails."""


def provision_vault(
    name: str,
    *,
    repo_type: str,
    destination: Path,
    obscura_path: Path | None = None,
) -> Path:
    """Scaffold and register a new vault-gen repo for Obscura.

    Parameters
    ----------
    name:
        Repo directory name (e.g. ``"my-vault"``).
    repo_type:
        ``"vault"`` or ``"config"``.
    destination:
        Parent directory under which ``name/`` will be created.
    obscura_path:
        Optional path to the Obscura workspace to record in the registry.

    Returns
    -------
    Path
        Absolute path to the newly created repo.

    Raises
    ------
    VaultProvisionError
        Wraps any exception from vault-gen with an Obscura-friendly message.
    """
    try:
        rtype = RepoType(repo_type)
    except ValueError:
        raise VaultProvisionError(
            f"Unknown repo_type {repo_type!r}. Expected 'vault' or 'config'."
        )

    try:
        config = RepoConfig(name=name, repo_type=rtype, destination=destination)
        repo_path = generate_repo(config)
    except FileExistsError as exc:
        raise VaultProvisionError(str(exc)) from exc
    except Exception as exc:
        raise VaultProvisionError(
            f"vault-gen failed to scaffold '{name}': {exc}"
        ) from exc

    registry = Registry.load()
    entry = registry.add(name=name, path=repo_path, repo_type=repo_type)
    if obscura_path is not None:
        entry.obscura_path = str(obscura_path)
    registry.save()

    return repo_path
