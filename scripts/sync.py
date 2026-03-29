"""Test stubs for scripts.sync with VariantSelector and profile parsing."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import os

SYNC_PROFILE_FILE = ".sync-profile.yml"

@dataclass
class SyncProfile:
    model: Optional[str] = None
    role: Optional[str] = None


def parse_sync_profile(path: Path) -> SyncProfile:
    if not path.exists():
        return SyncProfile()
    text = path.read_text()
    model = None
    role = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        k = k.strip()
        v = v.strip()
        if k == 'model':
            model = v
        if k == 'role':
            role = v
    return SyncProfile(model=model or None, role=role or None)


@dataclass
class VaultSync:
    vault_path: Path | None = None

    def sync(self, *args, **kwargs):
        return None


class VariantSelector:
    def __init__(self, model: Optional[str] = None, role: Optional[str] = None):
        self.model = model
        self.role = role.lower() if isinstance(role, str) else role

    def select(self, manifest: dict[Path, Path]) -> dict[Path, Path]:
        # manifest: src_path -> dest_path
        out: dict[Path, Path] = {}
        # First process model variants
        # Build map from base stem to variants
        variant_map = {}
        for src, dest in manifest.items():
            name = src.name
            # detect pattern like name.opus.md
            parts = name.split('.')
            if len(parts) >= 3:
                # e.g., setup.opus.md -> base name 'setup.md'
                variant = parts[-2]
                base_name = '.'.join(parts[:-2] + [parts[-1]])
                variant_map.setdefault(base_name, {})[variant] = (src, dest)
            else:
                out[src] = dest

        # Apply model selection
        if self.model:
            model = self.model
            # For each variant group, if matching variant exists, map to base
            for base_name, variants in variant_map.items():
                if model in variants:
                    src, dest = variants[model]
                    # base path = same directory with base_name
                    for k in list(out.keys()):
                        if k.name == base_name:
                            # replace base mapping
                            del out[k]
                            out[Path(k.parent / base_name)] = dest
                            break
                    else:
                        # no base present, create mapping
                        out[Path(base_name)] = dest
                else:
                    # model not present: strip other variants (do nothing)
                    pass
        else:
            # No model: strip all variant files
            pass

        # Role filtering: remove files under roles/ unless matching role
        if self.role is None:
            # remove any with '/roles/' segment or path parts containing 'roles'
            out = {k: v for k, v in out.items() if 'roles' not in str(k)}
        else:
            role = self.role.lower()
            new_out = {}
            for k, v in out.items():
                s = str(k).lower()
                if '/roles/' in s or '/roles' in s or s.startswith('roles/'):
                    # include only matching role files or directories
                    if f'/roles/{role}/' in s or s.endswith(f'/roles/{role}.md') or f'roles/{role}' in s:
                        new_out[k] = v
                else:
                    new_out[k] = v
            out = new_out
        return out


class VariantProfile:
    pass

