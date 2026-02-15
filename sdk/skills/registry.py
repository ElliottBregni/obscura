"""
sdk.skills.registry -- Skill registry for discovery and management.

The SkillRegistry manages all available skills and provides:
- Skill registration and lookup
- Capability discovery and search
- Skill lifecycle management (init/shutdown)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from sdk.skills.base import (
    Skill,
    SkillError,
    SkillHealth,
    SkillNotFoundError,
)

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Registry for managing skills.
    
    The registry maintains a mapping of skill names to skill instances
    and provides discovery capabilities across all registered skills.
    
    Example:
        registry = SkillRegistry()
        
        # Register skills
        registry.register(WebSearchSkill())
        registry.register(FileSystemSkill())
        
        # Initialize all skills
        await registry.initialize_all({"web_search": {"api_key": "xxx"}})
        
        # Execute a capability
        result = await registry.execute("web_search.search", {"query": "python"})
        
        # Discover capabilities
        caps = registry.discover("search")  # Find all search-related capabilities
    """
    
    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._capabilities: Dict[str, str] = {}  # capability_path -> skill_name
        self._initialized: Dict[str, bool] = {}
        self._configs: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
    
    def register(self, skill: Skill) -> None:
        """Register a skill with the registry.
        
        Args:
            skill: Skill instance to register
            
        Raises:
            SkillError: If skill with same name already registered
        """
        if skill.name in self._skills:
            raise SkillError(f"Skill '{skill.name}' is already registered")
        
        self._skills[skill.name] = skill
        self._initialized[skill.name] = False
        
        # Index capabilities
        for cap in skill.capabilities:
            cap_path = f"{skill.name}.{cap.name}"
            self._capabilities[cap_path] = skill.name
        
        logger.info(f"Registered skill: {skill.name} v{skill.version}")
    
    def unregister(self, skill_name: str) -> None:
        """Unregister a skill.
        
        Args:
            skill_name: Name of skill to unregister
            
        Raises:
            SkillNotFoundError: If skill not found
        """
        if skill_name not in self._skills:
            raise SkillNotFoundError(f"Skill '{skill_name}' not found")
        
        # Remove capability indices
        skill = self._skills[skill_name]
        for cap in skill.capabilities:
            cap_path = f"{skill_name}.{cap.name}"
            self._capabilities.pop(cap_path, None)
        
        del self._skills[skill_name]
        del self._initialized[skill_name]
        if skill_name in self._configs:
            del self._configs[skill_name]
        
        logger.info(f"Unregistered skill: {skill_name}")
    
    async def initialize_skill(self, skill_name: str, config: Dict[str, Any]) -> None:
        """Initialize a specific skill.
        
        Args:
            skill_name: Name of skill to initialize
            config: Configuration for the skill
            
        Raises:
            SkillNotFoundError: If skill not found
        """
        async with self._lock:
            if skill_name not in self._skills:
                raise SkillNotFoundError(f"Skill '{skill_name}' not found")
            
            skill = self._skills[skill_name]
            
            if self._initialized.get(skill_name, False):
                logger.debug(f"Skill '{skill_name}' already initialized")
                return
            
            try:
                await skill.initialize(config)
                self._initialized[skill_name] = True
                self._configs[skill_name] = config
                logger.info(f"Initialized skill: {skill_name}")
            except Exception as e:
                logger.error(f"Failed to initialize skill '{skill_name}': {e}")
                raise
    
    async def initialize_all(self, configs: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """Initialize all registered skills.
        
        Args:
            configs: Optional mapping of skill_name -> config
        """
        configs = configs or {}
        
        for skill_name in self._skills:
            config = configs.get(skill_name, {})
            try:
                await self.initialize_skill(skill_name, config)
            except Exception as e:
                logger.error(f"Failed to initialize skill '{skill_name}': {e}")
                # Continue with other skills
    
    async def shutdown_skill(self, skill_name: str) -> None:
        """Shutdown a specific skill.
        
        Args:
            skill_name: Name of skill to shutdown
        """
        async with self._lock:
            if skill_name not in self._skills:
                raise SkillNotFoundError(f"Skill '{skill_name}' not found")
            
            skill = self._skills[skill_name]
            
            if not self._initialized.get(skill_name, False):
                return
            
            try:
                await skill.shutdown()
                self._initialized[skill_name] = False
                logger.info(f"Shutdown skill: {skill_name}")
            except Exception as e:
                logger.error(f"Error shutting down skill '{skill_name}': {e}")
                raise
    
    async def shutdown_all(self) -> None:
        """Shutdown all initialized skills."""
        for skill_name in list(self._skills.keys()):
            if self._initialized.get(skill_name, False):
                try:
                    await self.shutdown_skill(skill_name)
                except Exception as e:
                    logger.error(f"Error shutting down skill '{skill_name}': {e}")
    
    def get_skill(self, skill_name: str) -> Optional[Skill]:
        """Get a skill by name.
        
        Args:
            skill_name: Name of the skill
            
        Returns:
            Skill instance or None if not found
        """
        return self._skills.get(skill_name)
    
    def get_skill_health(self, skill_name: str) -> Optional[SkillHealth]:
        """Get health status of a skill.
        
        Args:
            skill_name: Name of the skill
            
        Returns:
            SkillHealth or None if skill not found
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return None
        
        if not self._initialized.get(skill_name, False):
            return SkillHealth(
                healthy=False,
                message="Skill not initialized",
            )
        
        # Return a synchronous health check (async would need to be awaited)
        # For API purposes, return last known health or pending
        return SkillHealth(
            healthy=True,
            message="Skill initialized",
        )
    
    async def check_skill_health(self, skill_name: str) -> SkillHealth:
        """Perform health check on a skill.
        
        Args:
            skill_name: Name of the skill
            
        Returns:
            SkillHealth status
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return SkillHealth(
                healthy=False,
                message=f"Skill '{skill_name}' not found",
            )
        
        if not self._initialized.get(skill_name, False):
            return SkillHealth(
                healthy=False,
                message="Skill not initialized",
            )
        
        try:
            return await skill.health_check()
        except Exception as e:
            return SkillHealth(
                healthy=False,
                message=f"Health check failed: {e}",
            )
    
    def list_skills(self) -> List[Skill]:
        """List all registered skills."""
        return list(self._skills.values())
    
    def list_capabilities(self, skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all capabilities, optionally filtered by skill.
        
        Args:
            skill_name: Optional skill name to filter by
            
        Returns:
            List of capability dictionaries with skill info
        """
        results = []
        
        skills_to_check = [self._skills[skill_name]] if skill_name else self._skills.values()
        
        for skill in skills_to_check:
            for cap in skill.capabilities:
                results.append({
                    "skill": skill.name,
                    "skill_version": skill.version,
                    "skill_description": skill.description,
                    **cap.to_dict(),
                })
        
        return results
    
    def discover(self, query: str) -> List[Dict[str, Any]]:
        """Search capabilities by query string.
        
        Searches across skill names, descriptions, capability names,
        and capability descriptions.
        
        Args:
            query: Search query string
            
        Returns:
            List of matching capabilities
        """
        query = query.lower()
        results = []
        
        for skill in self._skills.values():
            # Check skill-level match
            skill_match = (
                query in skill.name.lower() or
                query in skill.description.lower() or
                any(query in tag.lower() for tag in skill.metadata.tags)
            )
            
            for cap in skill.capabilities:
                cap_match = (
                    query in cap.name.lower() or
                    query in cap.description.lower()
                )
                
                if skill_match or cap_match:
                    results.append({
                        "skill": skill.name,
                        "skill_version": skill.version,
                        **cap.to_dict(),
                    })
        
        # Sort by relevance (exact matches first)
        def relevance(item):
            cap_name = item.get("name", "").lower()
            skill_name = item.get("skill", "").lower()
            if query == cap_name or query == skill_name:
                return 0
            if query in cap_name or query in skill_name:
                return 1
            return 2
        
        results.sort(key=relevance)
        return results
    
    async def execute(
        self, 
        capability_path: str, 
        params: Dict[str, Any]
    ) -> Any:
        """Execute a capability by its full path (skill.capability).
        
        Args:
            capability_path: Full path like "web_search.search"
            params: Parameters for the capability
            
        Returns:
            Result of capability execution
            
        Raises:
            SkillNotFoundError: If skill not found
            CapabilityNotFoundError: If capability not found
            SkillError: If execution fails
        """
        # Parse capability path
        if "." not in capability_path:
            raise ValueError(f"Invalid capability path: {capability_path}. Expected format: skill.capability")
        
        skill_name, cap_name = capability_path.rsplit(".", 1)
        
        skill = self._skills.get(skill_name)
        if not skill:
            raise SkillNotFoundError(f"Skill '{skill_name}' not found")
        
        if not self._initialized.get(skill_name, False):
            raise SkillError(f"Skill '{skill_name}' is not initialized")
        
        # Validate parameters
        errors = skill.validate_params(cap_name, params)
        if errors:
            raise SkillError(f"Parameter validation failed: {'; '.join(errors)}")
        
        # Execute
        try:
            return await skill.execute(cap_name, params)
        except Exception as e:
            logger.error(f"Error executing capability '{capability_path}': {e}")
            raise SkillError(f"Execution failed: {e}") from e
    
    def is_initialized(self, skill_name: str) -> bool:
        """Check if a skill is initialized."""
        return self._initialized.get(skill_name, False)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get registry statistics."""
        return {
            "total_skills": len(self._skills),
            "initialized_skills": sum(1 for v in self._initialized.values() if v),
            "total_capabilities": len(self._capabilities),
            "skills": [
                {
                    "name": name,
                    "initialized": self._initialized.get(name, False),
                    "capabilities": len(skill.capabilities),
                }
                for name, skill in self._skills.items()
            ],
        }


# Global registry instance (singleton pattern)
_global_registry: Optional[SkillRegistry] = None


def get_global_registry() -> SkillRegistry:
    """Get the global skill registry instance."""
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
    return _global_registry


def reset_global_registry() -> None:
    """Reset the global registry (useful for testing)."""
    global _global_registry
    _global_registry = None
