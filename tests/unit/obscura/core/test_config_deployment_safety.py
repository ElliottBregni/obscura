"""Tests for ObscuraConfig.validate_deployment_safety.

This is a SOC2 CC6.1 control: the server refuses to start unauthenticated
on a non-loopback bind unless the operator has explicitly opted in.
"""

from __future__ import annotations

import pytest

from obscura.core.config import ObscuraConfig


def test_loopback_without_auth_is_allowed() -> None:
    """Local development on 127.0.0.1 with auth off is the intended path."""
    cfg = ObscuraConfig(host="127.0.0.1", auth_enabled=False)
    cfg.validate_deployment_safety()  # must not raise


def test_ipv6_loopback_without_auth_is_allowed() -> None:
    cfg = ObscuraConfig(host="::1", auth_enabled=False)
    cfg.validate_deployment_safety()


def test_localhost_string_is_treated_as_loopback() -> None:
    cfg = ObscuraConfig(host="localhost", auth_enabled=False)
    cfg.validate_deployment_safety()


def test_bind_all_without_auth_is_rejected() -> None:
    cfg = ObscuraConfig(host="0.0.0.0", auth_enabled=False)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        cfg.validate_deployment_safety()


def test_bind_specific_non_loopback_without_auth_is_rejected() -> None:
    cfg = ObscuraConfig(host="10.0.0.5", auth_enabled=False)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        cfg.validate_deployment_safety()


def test_bind_all_with_auth_is_allowed() -> None:
    cfg = ObscuraConfig(host="0.0.0.0", auth_enabled=True)
    cfg.validate_deployment_safety()


def test_explicit_opt_in_overrides_the_safety_check() -> None:
    cfg = ObscuraConfig(
        host="0.0.0.0",
        auth_enabled=False,
        allow_unauthenticated=True,
    )
    cfg.validate_deployment_safety()


def test_error_message_explains_remediation() -> None:
    cfg = ObscuraConfig(host="0.0.0.0", auth_enabled=False)
    with pytest.raises(RuntimeError) as excinfo:
        cfg.validate_deployment_safety()
    msg = str(excinfo.value)
    # The error must tell an operator all three ways out, not just one —
    # auditors will ask and customers will read it under pressure.
    assert "OBSCURA_AUTH_ENABLED=true" in msg
    assert "127.0.0.1" in msg
    assert "OBSCURA_ALLOW_UNAUTHENTICATED" in msg
