"""
sdk.skills.base -- Base skill protocol and classes for the Skills Framework.

Skills are pluggable capabilities that agents can use to interact with
external systems, perform actions, or access data.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Protocol, TypeVar, Union


class CapabilityType(Enum):
    """Types of skill capabilities."""
    QUERY = "query"        # Read-only operations
    ACTION = "action"      # Write/modify operations
    STREAM = "stream"      # Streaming operations
    HYBRID = "hybrid"      # Mixed operations


@dataclass
class CapabilityParameter:
    """Definition of a capability parameter."""
    name: str
    type: str  # JSON schema type: string, number, boolean, array, object
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[List[str]] = None  # For enumerated values


@dataclass
class CapabilityReturn:
    """Definition of a capability return value."""
    type: str  # JSON schema type
    description: str
    schema: Optional[Dict[str, Any]] = None  # JSON schema for complex types


@dataclass
class SkillCapability:
    """A capability exposed by a skill.
    
    Capabilities are discrete actions that can be invoked on a skill.
    They define their parameters and return types for discovery and validation.
    """
    name: str
    description: str
    parameters: List[CapabilityParameter]
    returns: CapabilityReturn
    capability_type: CapabilityType = CapabilityType.ACTION
    examples: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert capability to dictionary for serialization."""
        return {
            "name": self.name,
            "description": self.description,
            "type": self.capability_type.value,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": p.enum,
                }
                for p in self.parameters
            ],
            "returns": {
                "type": self.returns.type,
                "description": self.returns.description,
                "schema": self.returns.schema,
            },
            "examples": self.examples,
        }


@dataclass
class SkillMetadata:
    """Metadata about a skill."""
    author: str = "unknown"
    license: str = "MIT"
    homepage: Optional[str] = None
    repository: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    category: str = "general"  # web, filesystem, communication, etc.
    icon: Optional[str] = None  # Icon name or URL


@dataclass
class SkillHealth:
    """Health status of a skill."""
    healthy: bool
    message: str
    last_check: Optional[str] = None
    latency_ms: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "message": self.message,
            "last_check": self.last_check,
            "latency_ms": self.latency_ms,
            "details": self.details,
        }


class Skill(ABC):
    """Base class for all skills.
    
    Skills are pluggable components that provide specific capabilities.
    They follow a lifecycle: initialize -> execute -> shutdown.
    
    Example:
        class MySkill(Skill):
            name = "my_skill"
            version = "1.0.0"
            description = "Does something useful"
            
            capabilities = [
                SkillCapability(
                    name="do_something",
                    description="Does something",
                    parameters=[CapabilityParameter("input", "string", "Input data")],
                    returns=CapabilityReturn("string", "Output data"),
                )
            ]
            
            async def initialize(self, config: dict) -> None:
                # Setup resources
                pass
            
            async def execute(self, capability: str, params: dict) -> Any:
                if capability == "do_something":
                    return await self._do_something(params["input"])
                raise ValueError(f"Unknown capability: {capability}")
    """
    
    # Skill identity - must be overridden by subclasses
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    
    # Capabilities - must be defined by subclasses
    capabilities: List[SkillCapability] = []
    
    # Optional metadata
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    
    def __init__(self):
        self._initialized = False
        self._config: Dict[str, Any] = {}
    
    @abstractmethod
    async def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize the skill with configuration.
        
        Called once before the skill is used. Use this to set up
        connections, load resources, or validate configuration.
        
        Args:
            config: Configuration dictionary specific to this skill
            
        Raises:
            SkillInitializationError: If initialization fails
        """
        pass
    
    @abstractmethod
    async def execute(self, capability: str, params: Dict[str, Any]) -> Any:
        """Execute a capability.
        
        Args:
            capability: Name of the capability to execute
            params: Parameters for the capability
            
        Returns:
            Result of the capability execution
            
        Raises:
            SkillExecutionError: If execution fails
            ValueError: If capability doesn't exist
        """
        pass
    
    async def execute_stream(
        self, 
        capability: str, 
        params: Dict[str, Any]
    ) -> AsyncIterator[Any]:
        """Execute a capability with streaming results.
        
        Default implementation falls back to execute() and yields single result.
        Override for true streaming capabilities.
        
        Args:
            capability: Name of the capability to execute
            params: Parameters for the capability
            
        Yields:
            Stream chunks from the capability execution
        """
        result = await self.execute(capability, params)
        yield result
    
    @abstractmethod
    async def health_check(self) -> SkillHealth:
        """Check if the skill is healthy.
        
        Returns:
            SkillHealth with status and details
        """
        pass
    
    @abstractmethod
    async def shutdown(self) -> None:
        """Cleanup resources and shutdown the skill.
        
        Called when the skill is no longer needed. Release all resources
        and close connections.
        """
        pass
    
    def get_capability(self, name: str) -> Optional[SkillCapability]:
        """Get a capability by name."""
        for cap in self.capabilities:
            if cap.name == name:
                return cap
        return None
    
    def list_capabilities(self) -> List[SkillCapability]:
        """List all capabilities of this skill."""
        return list(self.capabilities)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert skill info to dictionary."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "capabilities": [cap.to_dict() for cap in self.capabilities],
            "metadata": {
                "author": self.metadata.author,
                "license": self.metadata.license,
                "homepage": self.metadata.homepage,
                "repository": self.metadata.repository,
                "tags": self.metadata.tags,
                "category": self.metadata.category,
                "icon": self.metadata.icon,
            },
        }
    
    def validate_params(self, capability_name: str, params: Dict[str, Any]) -> List[str]:
        """Validate parameters for a capability.
        
        Returns:
            List of validation errors (empty if valid)
        """
        cap = self.get_capability(capability_name)
        if not cap:
            return [f"Unknown capability: {capability_name}"]
        
        errors = []
        provided_params = set(params.keys())
        required_params = {p.name for p in cap.parameters if p.required}
        
        # Check required parameters
        for param_name in required_params:
            if param_name not in provided_params:
                errors.append(f"Missing required parameter: {param_name}")
        
        # Check for unknown parameters
        known_params = {p.name for p in cap.parameters}
        for param_name in provided_params:
            if param_name not in known_params:
                errors.append(f"Unknown parameter: {param_name}")
        
        # Type validation (basic)
        for param in cap.parameters:
            if param.name in params:
                value = params[param.name]
                expected_type = param.type
                
                if expected_type == "string" and not isinstance(value, str):
                    errors.append(f"Parameter '{param.name}' must be a string")
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    errors.append(f"Parameter '{param.name}' must be a number")
                elif expected_type == "boolean" and not isinstance(value, bool):
                    errors.append(f"Parameter '{param.name}' must be a boolean")
                elif expected_type == "array" and not isinstance(value, list):
                    errors.append(f"Parameter '{param.name}' must be an array")
                elif expected_type == "object" and not isinstance(value, dict):
                    errors.append(f"Parameter '{param.name}' must be an object")
        
        return errors


class SkillError(Exception):
    """Base exception for skill-related errors."""
    pass


class SkillInitializationError(SkillError):
    """Error during skill initialization."""
    pass


class SkillExecutionError(SkillError):
    """Error during skill execution."""
    pass


class SkillNotFoundError(SkillError):
    """Skill not found in registry."""
    pass


class CapabilityNotFoundError(SkillError):
    """Capability not found on skill."""
    pass
