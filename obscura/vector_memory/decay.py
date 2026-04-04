"""Centralized decay math and configuration for vector memory.

Provides per-type decay profiles, access-based freshness, and a single
``compute_decay()`` function used by both backends and the reranker.

Usage::

    from obscura.vector_memory.decay import compute_decay, load_decay_config

    config = load_decay_config()
    multiplier = compute_decay("episode", entry.created_at, entry.accessed_at, config)
    final_score = similarity * multiplier
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecayProfile:
    """Decay configuration for a single memory type.

    Parameters
    ----------
    half_life_days:
        Number of days for the score to halve.  Ignored when *immune* is True.
    min_score_floor:
        Decay multiplier below which a memory is eligible for GC / consolidation.
    immune:
        If True the memory never decays (e.g. preferences).

    """

    half_life_days: float = 30.0
    min_score_floor: float = 0.01
    immune: bool = False


DEFAULT_PROFILES: dict[str, DecayProfile] = {
    "episode": DecayProfile(half_life_days=7.0, min_score_floor=0.01),
    "summary": DecayProfile(half_life_days=60.0, min_score_floor=0.01),
    "fact": DecayProfile(half_life_days=90.0, min_score_floor=0.005),
    "general": DecayProfile(half_life_days=30.0, min_score_floor=0.01),
    "preference": DecayProfile(immune=True),
}


@dataclass(frozen=True)
class DecayConfig:
    """Top-level decay configuration, loadable from ``[vector_memory.decay]``."""

    profiles: dict[str, DecayProfile] = field(
        default_factory=lambda: dict(DEFAULT_PROFILES),
    )
    access_boost_days: float = 7.0
    consolidation_age_days: float = 14.0
    consolidation_batch_size: int = 20
    maintenance_on_startup: bool = True


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------


def compute_decay(
    memory_type: str,
    created_at: datetime,
    accessed_at: datetime | None,
    config: DecayConfig,
    *,
    now: datetime | None = None,
) -> float:
    """Return a decay multiplier in ``[0, 1]``.

    The effective age is measured from the most recent of *created_at* and
    *accessed_at*.  If the memory was accessed within *access_boost_days*,
    the effective age is reduced accordingly.

    Returns ``1.0`` for immune types (e.g. ``"preference"``).
    """
    profile = config.profiles.get(
        memory_type,
        config.profiles.get("general", DecayProfile()),
    )

    if profile.immune:
        return 1.0

    if now is None:
        now = datetime.now(UTC)

    # Normalize to UTC-aware
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if accessed_at is not None and accessed_at.tzinfo is None:
        accessed_at = accessed_at.replace(tzinfo=UTC)

    # Effective timestamp is the most recent of created / accessed
    effective_ts = created_at
    if accessed_at is not None and accessed_at > created_at:
        effective_ts = accessed_at

    age_days = max((now - effective_ts).total_seconds() / 86400.0, 0.0)

    # Access boost: if accessed within boost window, reduce effective age
    if accessed_at is not None and config.access_boost_days > 0:
        access_age_days = max((now - accessed_at).total_seconds() / 86400.0, 0.0)
        if access_age_days < config.access_boost_days:
            # Scale the boost: full boost at access_age=0, no boost at access_age=boost_window
            boost_factor = 1.0 - (access_age_days / config.access_boost_days)
            age_days = age_days * (1.0 - boost_factor * 0.5)  # up to 50% age reduction

    if profile.half_life_days <= 0:
        return 1.0

    return math.pow(0.5, age_days / profile.half_life_days)


def is_below_floor(
    memory_type: str,
    created_at: datetime,
    accessed_at: datetime | None,
    config: DecayConfig,
) -> bool:
    """True when a memory has decayed below its floor — eligible for GC."""
    profile = config.profiles.get(
        memory_type,
        config.profiles.get("general", DecayProfile()),
    )
    if profile.immune:
        return False
    decay = compute_decay(memory_type, created_at, accessed_at, config)
    return decay < profile.min_score_floor


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_decay_config(raw: dict[str, Any] | None = None) -> DecayConfig:
    """Build a :class:`DecayConfig` from a raw config dict.

    *raw* should be the ``[vector_memory.decay]`` section of config.toml.
    Falls back to ``DEFAULT_PROFILES`` for any missing types.  If *raw* is
    ``None``, returns the default config.
    """
    if raw is None:
        return DecayConfig()

    profiles = dict(DEFAULT_PROFILES)

    raw_profiles = raw.get("profiles", {})
    for type_name, profile_dict in raw_profiles.items():
        if not isinstance(profile_dict, dict):
            continue
        base = profiles.get(type_name, DecayProfile())
        profiles[type_name] = DecayProfile(
            half_life_days=profile_dict.get("half_life_days", base.half_life_days),
            min_score_floor=profile_dict.get("min_score_floor", base.min_score_floor),
            immune=profile_dict.get("immune", base.immune),
        )

    return DecayConfig(
        profiles=profiles,
        access_boost_days=raw.get("access_boost_days", 7.0),
        consolidation_age_days=raw.get("consolidation_age_days", 14.0),
        consolidation_batch_size=raw.get("consolidation_batch_size", 20),
        maintenance_on_startup=raw.get("maintenance_on_startup", True),
    )


def load_decay_config_from_disk() -> DecayConfig:
    """Load decay config from ``~/.obscura/config.toml`` and project config.

    Reads the ``[vector_memory.decay]`` section.  Returns defaults if the
    section doesn't exist or config can't be loaded.
    """
    try:
        from obscura.core.config_io import try_load_config

        home_cfg = try_load_config(
            Path.home() / ".obscura" / "config.toml",
        )
        raw_section = (home_cfg or {}).get("vector_memory", {}).get("decay")
        return load_decay_config(raw_section)
    except Exception:
        logger.debug(
            "Could not load decay config from disk, using defaults",
            exc_info=True,
        )
        return DecayConfig()
