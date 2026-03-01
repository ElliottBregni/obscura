"""
obscura.core.context_lazy — Lazy-loading extensions for ContextLoader.

Adds agent-specific skill loading with on-demand body loading to reduce
initial context window bloat.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from obscura.core.frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)


class SkillMetadata:
    """Lightweight skill metadata for lazy loading."""
    
    def __init__(
        self,
        name: str,
        description: str,
        path: Path,
        user_invocable: bool = True,
        allowed_tools: list[str] | None = None,
    ):
        self.name = name
        self.description = description
        self.path = path
        self.user_invocable = user_invocable
        self.allowed_tools = allowed_tools or []
    
    def to_stub(self) -> str:
        """Generate minimal skill stub for system prompt."""
        return f"""---
name: {self.name}
description: {self.description}
---"""


class LazySkillLoader:
    """Lazy loader for agent skills - loads metadata first, body on-demand."""
    
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self._metadata_cache: dict[str, SkillMetadata] = {}
        self._body_cache: dict[str, str] = {}
    
    def discover_skills(self, filter_names: list[str] | None = None) -> list[SkillMetadata]:
        """Discover all skills and load their metadata.
        
        Args:
            filter_names: If provided, only load skills with these names
        
        Returns:
            List of skill metadata objects
        """
        if not self.skills_dir.is_dir():
            return []
        
        skills: list[SkillMetadata] = []
        
        for skill_file in sorted(self.skills_dir.rglob("*.md")):
            if not skill_file.is_file():
                continue
            
            try:
                # Read file and parse frontmatter
                raw = skill_file.read_text(encoding="utf-8").strip()
                if not raw:
                    continue
                
                result = parse_frontmatter(raw, source_path=skill_file)
                meta = result.metadata
                
                name = str(meta.get("name", skill_file.stem))
                
                # Apply name filter if provided
                if filter_names and name not in filter_names:
                    continue
                
                skill_meta = SkillMetadata(
                    name=name,
                    description=str(meta.get("description", "")),
                    path=skill_file,
                    user_invocable=bool(meta.get("user-invocable", meta.get("user_invocable", True))),
                    allowed_tools=meta.get("allowed-tools", meta.get("allowed_tools", [])),
                )
                
                # Cache metadata
                self._metadata_cache[name] = skill_meta
                skills.append(skill_meta)
                
                logger.debug(f"Discovered skill: {name} at {skill_file}")
                
            except Exception as e:
                logger.warning(f"Failed to load skill metadata from {skill_file}: {e}")
                continue
        
        return skills
    
    def load_skill_body(self, skill_name: str) -> str | None:
        """Load full skill body on-demand.
        
        Args:
            skill_name: Name of skill to load
        
        Returns:
            Full skill content (frontmatter + body), or None if not found
        """
        # Check cache first
        if skill_name in self._body_cache:
            logger.debug(f"Skill '{skill_name}' loaded from cache")
            return self._body_cache[skill_name]
        
        # Get metadata
        if skill_name not in self._metadata_cache:
            logger.warning(f"Skill '{skill_name}' not found in metadata cache")
            return None
        
        skill_meta = self._metadata_cache[skill_name]
        
        try:
            # Load full file
            full_content = skill_meta.path.read_text(encoding="utf-8").strip()
            
            # Cache it
            self._body_cache[skill_name] = full_content
            
            logger.info(f"Loaded skill body for: {skill_name}")
            return full_content
            
        except Exception as e:
            logger.error(f"Failed to load skill body for '{skill_name}': {e}")
            return None
    
    def get_skill_stubs(self, skill_names: list[str] | None = None) -> str:
        """Get minimal skill stubs for system prompt.
        
        Args:
            skill_names: If provided, only include these skills
        
        Returns:
            Formatted string of skill stubs
        """
        skills_to_include = self._metadata_cache.values()
        
        if skill_names:
            skills_to_include = [
                s for s in skills_to_include 
                if s.name in skill_names
            ]
        
        if not skills_to_include:
            return ""
        
        stubs = [skill.to_stub() for skill in skills_to_include]
        return "\n\n".join(stubs)
    
    def clear_cache(self):
        """Clear all caches."""
        self._metadata_cache.clear()
        self._body_cache.clear()
        logger.debug("Skill caches cleared")
