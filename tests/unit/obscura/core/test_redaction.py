"""Tests for obscura.core.redaction.

These pin the pattern library's contract so future regressions are
obvious. When adding a new pattern, add a positive case + a realistic
near-miss that must NOT be redacted.
"""

from __future__ import annotations

import pytest

from obscura.core.redaction import (
    REDACTED,
    RedactionLevel,
    hash_identifier,
    hit_counts,
    redact_mapping,
    redact_text,
    reset_hit_counts,
)


@pytest.fixture(autouse=True)
def _clear_hit_counts() -> None:
    reset_hit_counts()


# ---------------------------------------------------------------------------
# Pattern library — each entry is (label, input, expected-substring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "dirty", "needle"),
    [
        (
            "github-classic-pat",
            "token=ghp_aZ3k9mXYZ01234567890abcdefghijklmnopqrstuv0W",
            "ghp_",
        ),
        (
            "github-fine-grained",
            "github_pat_11ABCDEFG0abcdefghijklmnopq_stuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ01234567890123456789",
            "github_pat_",
        ),
        (
            "anthropic-key",
            "key=sk-ant-api03-" + "A" * 60,
            "sk-ant-",
        ),
        (
            "openai-key",
            "OPENAI_API_KEY=sk-proj-" + "B" * 40,
            "sk-proj-",
        ),
        (
            "aws-access-key",
            "AKIAIOSFODNN7EXAMPLE in the log",
            "AKIA",
        ),
        (
            "google-api-key",
            "key=AIza" + "C" * 35,
            "AIza",
        ),
        (
            "slack-bot-token",
            "xoxb-1234567890-ABCDEFGHIJ",
            "xoxb-",
        ),
        (
            "jwt",
            "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature-opaque-blob",
            "ey",
        ),
        (
            "bearer",
            "Authorization: Bearer abcdef1234567890abcdef",
            "abcdef1234567890abcdef",
        ),
    ],
)
def test_standard_patterns_redact(label: str, dirty: str, needle: str) -> None:
    cleaned = redact_text(dirty)
    assert REDACTED in cleaned, f"{label!r}: {cleaned!r}"
    assert needle not in cleaned, (
        f"{label!r}: expected {needle!r} scrubbed but still in {cleaned!r}"
    )


def test_bearer_preserves_scheme_prefix() -> None:
    out = redact_text("Authorization: Bearer abcdef1234567890abcdef")
    assert out == f"Authorization: Bearer {REDACTED}"


def test_pem_private_key_block_redacted() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAabcdefghijklmn\nanothersecretline\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_text(f"key:\n{pem}\nend")
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert REDACTED in out


def test_off_level_leaves_text_alone() -> None:
    dirty = "ghp_" + "x" * 40
    assert redact_text(dirty, RedactionLevel.OFF) == dirty


def test_strict_level_also_redacts_email() -> None:
    text = "user dev@example.com did a thing"
    assert "dev@example.com" in redact_text(text, RedactionLevel.STANDARD)
    assert REDACTED in redact_text(text, RedactionLevel.STRICT)


def test_non_token_prose_untouched() -> None:
    # Must not redact ordinary words, short hex, version numbers, etc.
    text = "Release 0.2.0 built on abc123 with green tests."
    assert redact_text(text) == text


def test_hit_counts_increment_per_pattern() -> None:
    redact_text("ghp_" + "x" * 40 + " and AKIAIOSFODNN7EXAMPLE")
    counts = hit_counts()
    assert counts.get("github-token", 0) >= 1
    assert counts.get("aws-access-key", 0) >= 1


# ---------------------------------------------------------------------------
# Mapping redaction
# ---------------------------------------------------------------------------


def test_sensitive_keys_wiped_wholesale() -> None:
    out = redact_mapping(
        {
            "user": "elliott",
            "api_key": "anything-at-all",
            "Authorization": "Bearer opaque",
            "cookie": "session=abc",
        }
    )
    assert out["user"] == "elliott"
    assert out["api_key"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["cookie"] == REDACTED


def test_values_elsewhere_scrubbed_by_pattern() -> None:
    out = redact_mapping(
        {
            "details": "saw token ghp_" + "z" * 40 + " in request",
        }
    )
    assert REDACTED in out["details"]


def test_nested_mapping_recursively_redacted() -> None:
    out = redact_mapping(
        {
            "outer": {
                "inner": {
                    "password": "hunter2",
                    "note": "see AKIAIOSFODNN7EXAMPLE",
                }
            }
        }
    )
    inner = out["outer"]["inner"]
    assert inner["password"] == REDACTED
    assert REDACTED in inner["note"]


def test_list_values_redacted_element_wise() -> None:
    out = redact_mapping(
        {
            "tokens": ["not-a-secret", "ghp_" + "a" * 40],
        }
    )
    assert "not-a-secret" in out["tokens"]
    assert any(REDACTED in item for item in out["tokens"] if isinstance(item, str))


# ---------------------------------------------------------------------------
# Identifier hashing
# ---------------------------------------------------------------------------


def test_hash_is_stable_and_non_reversible() -> None:
    a = hash_identifier("dev@example.com")
    b = hash_identifier("dev@example.com")
    assert a == b
    assert a.startswith("u_")
    assert len(a) == 18  # "u_" + 16 hex chars
    assert "example" not in a


def test_hash_differs_between_identifiers() -> None:
    assert hash_identifier("dev@example.com") != hash_identifier("ops@example.com")


def test_hash_empty_is_empty() -> None:
    assert hash_identifier("") == ""
