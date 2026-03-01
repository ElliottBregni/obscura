"""Cached model registry with TTL and async safety.

This module provides a caching layer for model discovery, reducing the number
of API calls to provider endpoints while ensuring users get fresh model data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from obscura.core.types import Backend
from obscura.providers.registry import ModelInfo

logger = logging.getLogger(__name__)


class ModelCache:
    """Thread-safe cache for provider model listings.
    
    Features:
    - TTL-based expiration (default 1 hour)
    - Async lock for thread safety
    - Per-provider caching
    - Manual invalidation
    - Stale cache fallback on errors
    """
    
    def __init__(self, ttl_seconds: int = 3600):
        """Initialize cache.
        
        Args:
            ttl_seconds: Time-to-live in seconds (default 1 hour).
        """
        self._cache: dict[Backend, tuple[list[ModelInfo], datetime]] = {}
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = asyncio.Lock()
    
    async def get_models(
        self,
        backend: Backend,
        backend_instance: Any,
    ) -> list[ModelInfo]:
        """Get models for a provider, using cache if fresh."""
        async with self._lock:
            # Check cache
            if backend in self._cache:
                models, cached_at = self._cache[backend]
                age = datetime.now() - cached_at
                
                if age < self._ttl:
                    logger.debug(
                        f"Model cache hit for {backend.value} "
                        f"(age: {age.total_seconds():.0f}s)"
                    )
                    return models
            
            # Cache miss or expired - fetch fresh
            logger.info(f"Fetching models for {backend.value}")
            
            try:
                models = await backend_instance.list_models()
                self._cache[backend] = (models, datetime.now())
                logger.info(f"Cached {len(models)} models for {backend.value}")
                return models
            except Exception as e:
                logger.error(f"Failed to fetch models for {backend.value}: {e}")
                
                # Return stale cache if available
                if backend in self._cache:
                    logger.warning(f"Returning stale cache for {backend.value}")
                    models, _ = self._cache[backend]
                    return models
                raise
    
    def invalidate(self, backend: Backend | None = None) -> None:
        """Clear cache for a provider or all providers."""
        if backend:
            self._cache.pop(backend, None)
            logger.info(f"Invalidated cache for {backend.value}")
        else:
            self._cache.clear()
            logger.info("Invalidated all model caches")
    
    def get_cache_age(self, backend: Backend) -> timedelta | None:
        """Get age of cached data for a provider."""
        if backend in self._cache:
            _, cached_at = self._cache[backend]
            return datetime.now() - cached_at
        return None


# Global singleton
_global_cache = ModelCache()


async def list_provider_models(
    backend: Backend,
    backend_instance: Any,
) -> list[ModelInfo]:
    """Public API to list models with caching.
    
    Main entry point for model discovery throughout Obscura.
    """
    return await _global_cache.get_models(backend, backend_instance)


def invalidate_cache(backend: Backend | None = None) -> None:
    """Invalidate model cache."""
    _global_cache.invalidate(backend)


def get_cache_age(backend: Backend) -> timedelta | None:
    """Get age of cached model data for a provider."""
    return _global_cache.get_cache_age(backend)
