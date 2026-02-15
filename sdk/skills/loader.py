"""
sdk.skills.loader -- Dynamic skill loading from modules and packages.

Supports loading skills from:
- Built-in skills (sdk.skills.builtin)
- Python modules by path
- Future: Skill packages from marketplace
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
from pathlib import Path
from typing import List

from sdk.skills.base import Skill

logger = logging.getLogger(__name__)


class SkillLoader:
    """Loader for dynamically loading skills from various sources.
    
    Example:
        loader = SkillLoader()
        
        # Load built-in skills
        skills = loader.load_builtin_skills()
        
        # Load from a module path
        skills = loader.load_from_module("my_package.skills")
        
        # Load from a file path
        skills = loader.load_from_file("/path/to/custom_skill.py")
    """
    
    BUILTIN_MODULE = "sdk.skills.builtin"
    
    def __init__(self):
        self._loaded_modules: set[str] = set()
    
    def load_builtin_skills(self) -> List[Skill]:
        """Load all built-in skills from sdk.skills.builtin.
        
        Returns:
            List of loaded skill instances
        """
        return self.load_from_module(self.BUILTIN_MODULE)
    
    def load_from_module(self, module_path: str) -> List[Skill]:
        """Load all Skill subclasses from a module.
        
        Args:
            module_path: Python module path (e.g., "my_package.skills")
            
        Returns:
            List of loaded skill instances
        """
        skills: List[Skill] = []

        try:
            # Import the module
            module = importlib.import_module(module_path)

            # Find all Skill subclasses in the module
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, Skill) and
                    obj is not Skill and
                    obj.__module__ == module.__name__ and
                    getattr(obj, "name", None)  # Has a name attribute
                ):
                    try:
                        skill = obj()
                        skills.append(skill)
                        logger.info(f"Loaded skill '{skill.name}' from {module_path}")
                    except Exception as e:
                        logger.error(f"Failed to instantiate skill '{name}' from {module_path}: {e}")

            # Check for submodules (packages)
            if hasattr(module, "__path__"):
                for _importer, submod_name, ispkg in pkgutil.iter_modules(module.__path__):
                    if not ispkg and not submod_name.startswith("_"):
                        full_path = f"{module_path}.{submod_name}"
                        try:
                            sub_skills = self.load_from_module(full_path)
                            skills.extend(sub_skills)
                        except Exception as e:
                            logger.error(f"Failed to load submodule {full_path}: {e}")

        except ImportError as e:
            logger.error(f"Failed to import module {module_path}: {e}")
        except Exception as e:
            logger.error(f"Error loading skills from {module_path}: {e}")

        return skills
    
    def load_from_file(self, file_path: str | Path) -> List[Skill]:
        """Load skills from a Python file.
        
        Args:
            file_path: Path to Python file containing skill definitions
            
        Returns:
            List of loaded skill instances
        """
        file_path = Path(file_path)
        
        if not file_path.exists():
            raise FileNotFoundError(f"Skill file not found: {file_path}")
        
        if not file_path.suffix == ".py":
            raise ValueError(f"Skill file must be a Python file: {file_path}")
        
        skills = []
        module_name = f"_skill_loader_{file_path.stem}_{id(file_path)}"
        
        try:
            # Load the module from file
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load spec from {file_path}")
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            # Find Skill subclasses
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, Skill) and
                    obj is not Skill and
                    getattr(obj, "name", None)
                ):
                    try:
                        skill = obj()
                        skills.append(skill)
                        logger.info(f"Loaded skill '{skill.name}' from {file_path}")
                    except Exception as e:
                        logger.error(f"Failed to instantiate skill '{name}' from {file_path}: {e}")
            
        except Exception as e:
            logger.error(f"Error loading skills from {file_path}: {e}")
            raise
        finally:
            # Clean up sys.modules
            if module_name in sys.modules:
                del sys.modules[module_name]
        
        return skills
    
    def load_from_directory(self, directory: str | Path) -> List[Skill]:
        """Load all skills from a directory.
        
        Args:
            directory: Path to directory containing skill files
            
        Returns:
            List of loaded skill instances
        """
        directory = Path(directory)
        
        if not directory.exists():
            raise FileNotFoundError(f"Skill directory not found: {directory}")
        
        if not directory.is_dir():
            raise ValueError(f"Path is not a directory: {directory}")
        
        skills = []
        
        for file_path in directory.glob("*.py"):
            if file_path.name.startswith("_"):
                continue
            
            try:
                file_skills = self.load_from_file(file_path)
                skills.extend(file_skills)
            except Exception as e:
                logger.error(f"Failed to load skills from {file_path}: {e}")
        
        return skills


def load_builtin_skills() -> List[Skill]:
    """Convenience function to load all built-in skills.
    
    Returns:
        List of built-in skill instances
    """
    loader = SkillLoader()
    return loader.load_builtin_skills()
