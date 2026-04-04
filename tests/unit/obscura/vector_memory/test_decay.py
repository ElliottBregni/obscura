"""Tests for obscura.vector_memory.decay — centralized decay math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obscura.vector_memory.decay import (
    DecayConfig,
    compute_decay,
    is_below_floor,
    load_decay_config,
)

# ---------------------------------------------------------------------------
# compute_decay
# ---------------------------------------------------------------------------


def test_episode_half_life_7_days() -> None:
    """An episode exactly 7 days old should have decay ≈ 0.5."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    created = now - timedelta(days=7)
    decay = compute_decay("episode", created, None, config, now=now)
    assert abs(decay - 0.5) < 0.01


def test_fact_decays_slower() -> None:
    """A fact at 7 days should decay much less than an episode."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    created = now - timedelta(days=7)
    episode_decay = compute_decay("episode", created, None, config, now=now)
    fact_decay = compute_decay("fact", created, None, config, now=now)
    assert fact_decay > episode_decay


def test_preference_immune() -> None:
    """Preferences should always return 1.0 regardless of age."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    created = now - timedelta(days=365)
    decay = compute_decay("preference", created, None, config, now=now)
    assert decay == 1.0


def test_accessed_at_resets_effective_age() -> None:
    """accessed_at should make the memory appear younger."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    created = now - timedelta(days=30)
    accessed = now - timedelta(days=1)  # accessed yesterday

    decay_without = compute_decay("general", created, None, config, now=now)
    decay_with = compute_decay("general", created, accessed, config, now=now)

    # Accessed version should have higher decay (less decayed)
    assert decay_with > decay_without


def test_zero_age_returns_one() -> None:
    """A brand-new memory should have decay = 1.0."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    decay = compute_decay("episode", now, None, config, now=now)
    assert decay == 1.0


def test_naive_datetime_handled() -> None:
    """Naive datetimes (no tzinfo) should be treated as UTC."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    created = datetime(2026, 3, 21)  # naive, 7 days ago
    decay = compute_decay("episode", created, None, config, now=now)
    assert abs(decay - 0.5) < 0.01


def test_unknown_type_uses_general() -> None:
    """Unknown memory types should fall back to 'general' profile."""
    config = DecayConfig()
    now = datetime(2026, 3, 28, tzinfo=UTC)
    created = now - timedelta(days=30)
    decay = compute_decay("unknown_type", created, None, config, now=now)
    general_decay = compute_decay("general", created, None, config, now=now)
    assert decay == general_decay


# ---------------------------------------------------------------------------
# is_below_floor
# ---------------------------------------------------------------------------


def test_below_floor_old_episode() -> None:
    """A very old episode should be below the floor."""
    config = DecayConfig()
    created = datetime(2020, 1, 1, tzinfo=UTC)
    assert is_below_floor("episode", created, None, config)


def test_not_below_floor_recent() -> None:
    """A recent memory should not be below the floor."""
    config = DecayConfig()
    created = datetime.now(UTC) - timedelta(hours=1)
    assert not is_below_floor("episode", created, None, config)


def test_immune_never_below_floor() -> None:
    """Preferences should never be below floor."""
    config = DecayConfig()
    created = datetime(2000, 1, 1, tzinfo=UTC)
    assert not is_below_floor("preference", created, None, config)


# ---------------------------------------------------------------------------
# load_decay_config
# ---------------------------------------------------------------------------


def test_load_defaults() -> None:
    """load_decay_config(None) should return defaults."""
    config = load_decay_config(None)
    assert config.profiles["episode"].half_life_days == 7.0
    assert config.profiles["preference"].immune is True
    assert config.maintenance_on_startup is True


def test_load_custom_profile() -> None:
    """Custom profiles should merge with defaults."""
    raw = {
        "profiles": {
            "episode": {"half_life_days": 14},
            "custom_type": {"half_life_days": 45, "immune": False},
        },
        "access_boost_days": 10.0,
    }
    config = load_decay_config(raw)
    assert config.profiles["episode"].half_life_days == 14.0
    assert config.profiles["custom_type"].half_life_days == 45.0
    assert config.profiles["fact"].half_life_days == 90.0  # untouched default
    assert config.access_boost_days == 10.0


def test_load_empty_raw() -> None:
    """Empty dict should return defaults."""
    config = load_decay_config({})
    assert config.profiles["episode"].half_life_days == 7.0
