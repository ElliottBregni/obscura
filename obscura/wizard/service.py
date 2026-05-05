"""Pure-Python wizard service — the single source of truth.

The TUI, the FastAPI router, and the MCP tools all consume this class.
It owns:

* discovery of available prompts / capabilities / plugins / agents / MCP
  servers from the local filesystem,
* parsing of the wizard-managed sections of ``config.toml``
  (``[profiles.*]``, ``[active]``, ``[workspaces.*]``),
* atomic writes that preserve any unrelated keys (``[plugins]``,
  ``[defaults.capabilities]``, ``[mcp]`` …) untouched.

Nothing in this module imports FastAPI, prompt-toolkit, or anything else
that could anchor the wizard to a single frontend. Keep it that way.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from obscura.core.config_io import dump_toml, load_config
from obscura.wizard.schema import (
    ActiveState,
    Profile,
    WizardSnapshot,
    WorkspaceBinding,
)

logger = logging.getLogger(__name__)


# Well-known capability strings. Sourced from the codebase + the existing
# default grant set in ``~/.obscura/config.toml``. Unknown grants in the
# user's config are also surfaced so the wizard does not lose them.
_KNOWN_CAPABILITIES: tuple[str, ...] = (
    "shell.exec",
    "file.read",
    "file.write",
    "file.delete",
    "git.ops",
    "web.browse",
    "search.web",
    "security.scan",
    "memory.read",
    "memory.write",
    "vector.read",
    "vector.write",
    "mcp.invoke",
    "browser.control",
)

# Backends that the agent loop knows how to instantiate.
_KNOWN_BACKENDS: tuple[str, ...] = (
    "copilot",
    "claude",
    "openai",
    "codex",
    "localllm",
    "moonshot",
)

# Tool-loading modes (config.toml top-level ``mode`` key).
# See obscura/core/workspace.py for the canonical comment block:
#   code  — loads all registered tools (unrestricted)
#   ask   — disables tools (conversational only)
#   plan  — read-only tools (research + planning)
#   diff  — read + git inspection tools
_KNOWN_MODES: tuple[str, ...] = ("code", "ask", "plan", "diff")


def default_service() -> WizardService:
    """Return a :class:`WizardService` rooted at ``~/.obscura/``."""
    return WizardService()


class WizardService:
    """Read/write wizard-managed config without disturbing other sections."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = (config_dir or Path.home() / ".obscura").expanduser()
        self.config_path = self.config_dir / "config.toml"

    # ------------------------------------------------------------------
    # Snapshot — single read used by every frontend on entry
    # ------------------------------------------------------------------

    def snapshot(self) -> WizardSnapshot:
        return WizardSnapshot(
            profiles=self.list_profiles(),
            active=self.get_active(),
            workspaces=self.list_workspaces(),
            available_prompts=self.list_available_prompts(),
            available_capabilities=self.list_available_capabilities(),
            available_plugins=self.list_available_plugins(),
            available_backends=list(_KNOWN_BACKENDS),
            available_mcp_servers=self.list_available_mcp_servers(),
            available_agents=self.list_available_agents(),
            available_skills=self.list_available_skills(),
            available_modes=list(_KNOWN_MODES),
            available_commands=self.list_available_commands(),
            hooks_summary=self.hooks_summary(),
            default_vault_path=str(self.default_vault_path()),
            soul_path=str(self.soul_path()),
        )

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[Profile]:
        data = self._load()
        raw = _as_dict(data.get("profiles"))
        out: list[Profile] = []
        for name, body in raw.items():
            body_dict = _as_dict(body)
            out.append(_profile_from_dict(name, body_dict))
        out.sort(key=lambda p: p.name)
        return out

    def get_profile(self, name: str) -> Profile | None:
        for p in self.list_profiles():
            if p.name == name:
                return p
        return None

    def upsert_profile(self, profile: Profile) -> Profile:
        data = self._load()
        profiles = _as_dict(data.setdefault("profiles", {}))
        profiles[profile.name] = _profile_to_dict(profile)
        self._save(data)
        return profile

    def delete_profile(self, name: str) -> bool:
        data = self._load()
        profiles = _as_dict(data.get("profiles"))
        if name not in profiles:
            return False
        del profiles[name]
        # If we just removed the active profile, fall back to "default".
        active = _as_dict(data.get("active"))
        if active.get("profile") == name:
            active["profile"] = "default"
            data["active"] = active
        self._save(data)
        return True

    # ------------------------------------------------------------------
    # Active profile
    # ------------------------------------------------------------------

    def get_active(self) -> ActiveState:
        data = self._load()
        active = _as_dict(data.get("active"))
        name = str(active.get("profile") or "default")
        return ActiveState(profile=name)

    def set_active(self, profile_name: str) -> ActiveState:
        data = self._load()
        data["active"] = {"profile": profile_name}
        self._save(data)
        return ActiveState(profile=profile_name)

    # ------------------------------------------------------------------
    # Workspace bindings
    # ------------------------------------------------------------------

    def list_workspaces(self) -> list[WorkspaceBinding]:
        data = self._load()
        raw = _as_dict(data.get("workspaces"))
        out: list[WorkspaceBinding] = []
        for path, body in raw.items():
            body_dict = _as_dict(body)
            profile = str(body_dict.get("profile") or "")
            if profile:
                out.append(WorkspaceBinding(path=path, profile=profile))
        out.sort(key=lambda w: w.path)
        return out

    def set_workspace(self, path: str, profile: str) -> WorkspaceBinding:
        data = self._load()
        workspaces = _as_dict(data.setdefault("workspaces", {}))
        workspaces[path] = {"profile": profile}
        self._save(data)
        return WorkspaceBinding(path=path, profile=profile)

    def unset_workspace(self, path: str) -> bool:
        data = self._load()
        workspaces = _as_dict(data.get("workspaces"))
        if path not in workspaces:
            return False
        del workspaces[path]
        self._save(data)
        return True

    # ------------------------------------------------------------------
    # Resolution — runtime callers ask "what profile is active right now?"
    # ------------------------------------------------------------------

    def resolve_active_profile(self, cwd: Path | None = None) -> Profile | None:
        """Return the profile that should govern this run, or ``None``.

        Precedence (first match wins):

        1. ``OBSCURA_PROFILE`` env var
        2. Closest ancestor workspace binding for ``cwd``
        3. ``[active].profile`` in ``config.toml``

        If the resolved name has no matching ``[profiles.<name>]`` section,
        ``None`` is returned — callers fall back to their existing defaults
        rather than failing.
        """
        name = self._resolve_active_name(cwd)
        if not name:
            return None
        return self.get_profile(name)

    def _resolve_active_name(self, cwd: Path | None) -> str | None:
        env_name = os.environ.get("OBSCURA_PROFILE")
        if env_name:
            return env_name
        cwd = (cwd or Path.cwd()).resolve()
        bindings = self.list_workspaces()
        # Match the longest binding path that is an ancestor of cwd —
        # nested workspaces should pick the most-specific binding.
        best: tuple[int, str] | None = None
        for binding in bindings:
            try:
                bound = Path(binding.path).expanduser().resolve()
            except OSError:
                logger.debug("workspace path resolve failed", exc_info=True)
                continue
            try:
                cwd.relative_to(bound)
            except ValueError:
                logger.debug("workspace not an ancestor of cwd", exc_info=True)
                continue
            depth = len(bound.parts)
            if best is None or depth > best[0]:
                best = (depth, binding.profile)
        if best is not None:
            return best[1]
        return self.get_active().profile

    def load_profile_prompt_text(self, profile: Profile) -> list[str]:
        """Return the text content of every prompt named in ``profile.prompts``.

        Prompts are resolved against the same roots as
        :meth:`list_available_prompts` (packaged + user overlay). Missing
        files are skipped with a debug log so a stale profile does not
        break boot.
        """
        out: list[str] = []
        roots = self._prompt_roots()
        for name in profile.prompts:
            text = _read_first(name, roots)
            if text:
                out.append(text)
            else:
                logger.debug(
                    "profile %s references missing prompt %s", profile.name, name
                )
        return out

    def apply_profile_to_environment(self, profile: Profile) -> None:
        """Export profile-derived environment variables in-process.

        Currently exports:

        * ``OBSCURA_MODE`` — when ``profile.mode`` is set; consumed by
          downstream code that branches on tool-loading mode.
        * ``OBSCURA_VAULT_DIR`` — when ``profile.vault_path`` is set;
          ``kairos.vault_sync._resolve_default_vault_dir`` reads this so
          callers using ``VaultSync()`` (no args) honour the override.

        Existing values are *not* overwritten — shell env wins, exactly
        like :mod:`obscura.cli._env_loader` semantics.
        """
        if profile.mode:
            os.environ.setdefault("OBSCURA_MODE", profile.mode)
        if profile.vault_path:
            os.environ.setdefault("OBSCURA_VAULT_DIR", profile.vault_path)

    def env_file_for(self, profile_name: str) -> Path:
        """Path to a per-profile ``.env`` file (may not exist)."""
        return self.config_dir / f".env.{profile_name}"

    def read_env_file(self, profile_name: str) -> str:
        path = self.env_file_for(profile_name)
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.debug("could not read %s", path, exc_info=True)
            return ""

    def write_env_file(self, profile_name: str, content: str) -> Path:
        """Atomic write of ``~/.obscura/.env.<profile>``. Creates parent dir."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.env_file_for(profile_name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path

    # ------------------------------------------------------------------
    # Discovery — derived from filesystem state, not config
    # ------------------------------------------------------------------

    def list_available_prompts(self) -> list[str]:
        names: set[str] = set()
        for root in self._prompt_roots():
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.is_file() and entry.suffix.lower() in {".md", ".txt"}:
                    names.add(entry.stem)
        return sorted(names)

    def list_available_capabilities(self) -> list[str]:
        # Static list + anything the user already has granted.
        granted: set[str] = set(_KNOWN_CAPABILITIES)
        data = self._load()
        defaults = _as_dict(data.get("defaults"))
        caps = _as_dict(defaults.get("capabilities"))
        for key in ("grant", "deny"):
            for c in caps.get(key, []) or []:
                if isinstance(c, str):
                    granted.add(c)
        return sorted(granted)

    def list_available_plugins(self) -> list[str]:
        # Walk ~/.obscura/plugins/builtins/*.toml plus any registry.json entries.
        names: set[str] = set()
        plugins_root = self.config_dir / "plugins"
        builtins = plugins_root / "builtins"
        if builtins.is_dir():
            for entry in builtins.iterdir():
                if entry.is_dir() or entry.suffix.lower() == ".toml":
                    names.add(entry.stem)
        registry = plugins_root / "registry.json"
        if registry.is_file():
            try:
                payload = json.loads(registry.read_text())
            except (OSError, json.JSONDecodeError):
                logger.debug("could not parse %s", registry, exc_info=True)
            else:
                if isinstance(payload, dict):
                    for k in payload:
                        if isinstance(k, str):
                            names.add(k)
        return sorted(names)

    def list_available_mcp_servers(self) -> list[str]:
        core = self.config_dir / "mcp" / "core.json"
        if not core.is_file():
            return []
        try:
            payload = json.loads(core.read_text())
        except (OSError, json.JSONDecodeError):
            logger.debug("could not parse %s", core, exc_info=True)
            return []
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
        if not isinstance(servers, dict):
            return []
        return sorted(k for k in servers if isinstance(k, str))

    def list_available_agents(self) -> list[str]:
        names: set[str] = set()
        agents_dir = self.config_dir / "agents"
        if agents_dir.is_dir():
            for entry in agents_dir.iterdir():
                if entry.is_file() and entry.suffix.lower() in {".md", ".yaml", ".yml"}:
                    names.add(entry.stem.removesuffix(".agent"))
        return sorted(names)

    def list_available_skills(self) -> list[str]:
        """Return skill names discovered under ``~/.obscura/skills/`` (recursive)."""
        names: set[str] = set()
        skills_dir = self.config_dir / "skills"
        if skills_dir.is_dir():
            for entry in skills_dir.rglob("*.md"):
                if entry.is_file():
                    names.add(entry.stem)
        return sorted(names)

    def list_available_commands(self) -> list[str]:
        """Return ``@command`` macro names from ``~/.obscura/commands/``."""
        names: set[str] = set()
        cmd_dir = self.config_dir / "commands"
        if cmd_dir.is_dir():
            for entry in cmd_dir.rglob("*.md"):
                if entry.is_file():
                    names.add(entry.stem)
        return sorted(names)

    def hooks_summary(self) -> dict[str, int]:
        """Map of hook event name -> number of registered handlers.

        Reads ``~/.obscura/hooks/hooks.json`` if present. Schema isn't
        enforced — anything that isn't a list-valued top-level key is
        ignored. Returns ``{}`` on any failure.
        """
        hooks_file = self.config_dir / "hooks" / "hooks.json"
        if not hooks_file.is_file():
            return {}
        try:
            payload = json.loads(hooks_file.read_text())
        except (OSError, json.JSONDecodeError):
            logger.debug("could not parse %s", hooks_file, exc_info=True)
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in payload.items():
            if isinstance(k, str) and isinstance(v, list):
                out[k] = len(v)
        return out

    # ------------------------------------------------------------------
    # Vault + soul (extra config artifacts the wizard surfaces)
    # ------------------------------------------------------------------

    def default_vault_path(self) -> Path:
        """The default vault location (``~/.obscura/vault``)."""
        return self.config_dir / "vault"

    def soul_path(self) -> Path:
        """Location of the user's SOUL.md (whether or not it exists yet)."""
        return self.config_dir / "SOUL.md"

    def read_soul(self) -> str:
        """Return the contents of SOUL.md, or empty string if missing."""
        path = self.soul_path()
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.debug("could not read %s", path, exc_info=True)
            return ""

    def write_soul(self, content: str) -> Path:
        """Atomic write of ``~/.obscura/SOUL.md``."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.soul_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return path

    # ------------------------------------------------------------------
    # Internal — atomic read/write
    # ------------------------------------------------------------------

    def _prompt_roots(self) -> list[Path]:
        # 1) packaged prompts (obscura/prompts/) — discovered via importlib
        # 2) user-overlaid prompts at ~/.obscura/prompts/
        roots: list[Path] = []
        try:
            import obscura.prompts as _packaged  # noqa: PLC0415  # optional dep; tolerate missing

            packaged_init = _packaged.__file__
            if packaged_init:
                roots.append(Path(packaged_init).parent)
        except Exception:
            logger.debug("failed to locate packaged prompts dir", exc_info=True)
        roots.append(self.config_dir / "prompts")
        return roots

    def _load(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return {}
        try:
            return load_config(self.config_path, warn_yaml=False)
        except (FileNotFoundError, ValueError):
            logger.warning("could not load %s; treating as empty", self.config_path)
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        dump_toml(data, tmp)
        os.replace(tmp, self.config_path)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401  # accepts arbitrary YAML/JSON blob
    return value if isinstance(value, dict) else {}


def _read_first(stem: str, roots: list[Path]) -> str | None:
    """Return the contents of the first ``<stem>.{md,txt}`` found in ``roots``."""
    for root in roots:
        for suffix in (".md", ".txt"):
            candidate = root / f"{stem}{suffix}"
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8")
                except OSError:
                    logger.debug("could not read %s", candidate, exc_info=True)
                    return None
    return None


def _profile_from_dict(name: str, body: dict[str, Any]) -> Profile:
    def _strs(key: str) -> list[str]:
        raw = body.get(key) or []
        return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []

    def _opt_str(key: str) -> str | None:
        v = body.get(key)
        return v if isinstance(v, str) and v else None

    return Profile(
        name=name,
        prompts=_strs("prompts"),
        backend=_opt_str("backend"),
        model=_opt_str("model"),
        mode=_opt_str("mode"),
        capabilities=_strs("capabilities"),
        plugins=_strs("plugins"),
        mcp_servers=_strs("mcp_servers"),
        agents=_strs("agents"),
        skills=_strs("skills"),
        vault_path=_opt_str("vault_path"),
    )


def _profile_to_dict(profile: Profile) -> dict[str, Any]:
    out: dict[str, Any] = {
        "prompts": list(profile.prompts),
        "capabilities": list(profile.capabilities),
        "plugins": list(profile.plugins),
        "mcp_servers": list(profile.mcp_servers),
        "agents": list(profile.agents),
        "skills": list(profile.skills),
    }
    if profile.backend:
        out["backend"] = profile.backend
    if profile.model:
        out["model"] = profile.model
    if profile.mode:
        out["mode"] = profile.mode
    if profile.vault_path:
        out["vault_path"] = profile.vault_path
    return out
