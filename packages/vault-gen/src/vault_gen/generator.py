from __future__ import annotations

import enum
import shutil
import subprocess
from pathlib import Path
from string import Template

import structlog
from pydantic import BaseModel

log = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ACCESS_DIR = Path(__file__).parent / "access"


class RepoType(str, enum.Enum):
    CONFIG = "config"
    VAULT = "vault"


class RepoConfig(BaseModel, extra="forbid"):
    name: str
    repo_type: RepoType
    destination: Path


def generate_repo(config: RepoConfig) -> Path:
    """Scaffold a new repo at config.destination / config.name.

    Creates the directory, copies templates, embeds the _access module, then
    runs `git init` and creates an initial commit.

    Raises FileExistsError if the destination already exists.
    On any other failure, the partially-created directory is removed before
    re-raising so we don't leave garbage on disk.
    """
    dest = config.destination / config.name
    if dest.exists():
        raise FileExistsError(f"Destination already exists: {dest}")

    log.info(
        "scaffolding repo",
        name=config.name,
        type=config.repo_type.value,
        path=str(dest),
    )

    dest.mkdir(parents=True)
    try:
        variables = {"name": config.name, "type": config.repo_type.value}
        _copy_shared(dest, variables)
        if config.repo_type == RepoType.CONFIG:
            _scaffold_config(dest, variables)
        else:
            _scaffold_vault(dest, variables)
        _embed_access_layer(dest)
        _write_meta(dest, config)
        _git_init(dest, config)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    log.info("repo created", path=str(dest))
    return dest


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render(template_path: Path, variables: dict[str, str]) -> str:
    """Render a template file using ${var} substitution (safe — leaves unknown vars)."""
    return Template(template_path.read_text()).safe_substitute(variables)


def _copy_shared(dest: Path, variables: dict[str, str]) -> None:
    shared = _TEMPLATES_DIR / "shared"
    (dest / ".gitignore").write_text((shared / "gitignore").read_text())
    (dest / "CLAUDE.md").write_text(_render(shared / "CLAUDE.md", variables))


def _scaffold_type(dest: Path, type_dir: Path, variables: dict[str, str]) -> None:
    """Copy a type-specific template directory into dest.

    Files under `obsidian/` are mapped to `.obsidian/` in the destination so
    the vault-gen repo never contains a live `.obsidian/` directory.
    """
    for item in sorted(type_dir.rglob("*")):
        if item.is_dir():
            continue
        rel = item.relative_to(type_dir)
        parts = list(rel.parts)
        if parts[0] == "obsidian":
            parts[0] = ".obsidian"
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix in {".md", ".toml", ".json", ".env"}:
            target.write_text(_render(item, variables))
        else:
            shutil.copy2(item, target)


def _scaffold_config(dest: Path, variables: dict[str, str]) -> None:
    _scaffold_type(dest, _TEMPLATES_DIR / "config", variables)


def _scaffold_vault(dest: Path, variables: dict[str, str]) -> None:
    _scaffold_type(dest, _TEMPLATES_DIR / "vault", variables)


def _write_meta(dest: Path, config: RepoConfig) -> None:
    """Write .vault-gen/meta.toml so RepoAccess can detect the repo type.

    This file is what enables the permission model: config repos gate writes
    behind privilege="admin" by reading this file at init time.
    """
    meta_dir = dest / ".vault-gen"
    meta_dir.mkdir(exist_ok=True)
    (meta_dir / "meta.toml").write_text(
        f"# vault-gen repo metadata — do not edit by hand\n"
        f"\n"
        f"[vault]\n"
        f'name = "{config.name}"\n'
        f'type = "{config.repo_type.value}"\n'
        f'generated_by = "vault-gen"\n'
        f'schema_version = "1"\n'
    )


def _embed_access_layer(dest: Path) -> None:
    """Copy src/vault_gen/access/ into the repo as _access/.

    The access module is stdlib-only so it works without vault-gen installed.
    Relative imports in __init__.py resolve correctly in the _access context.
    """
    shutil.copytree(_ACCESS_DIR, dest / "_access")


def _git_init(dest: Path, config: RepoConfig) -> None:
    def run(*args: str) -> str:
        result = subprocess.run(list(args), cwd=dest, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"`{' '.join(args)}` failed:\n{result.stderr.strip()}"
            )
        return result.stdout

    run("git", "init")

    # Set a local identity if no global git user is configured, so the initial
    # commit doesn't fail in CI or fresh environments.
    has_email = subprocess.run(
        ["git", "config", "--global", "user.email"],
        capture_output=True,
    ).returncode == 0
    if not has_email:
        run("git", "config", "user.email", "vault-gen@local")
        run("git", "config", "user.name", "vault-gen")

    run("git", "add", ".")
    run(
        "git",
        "commit",
        "-m",
        f"chore: initialize {config.repo_type.value} vault '{config.name}'\n\nScaffolded by vault-gen.",
    )
    log.info("initial commit created", path=str(dest))
