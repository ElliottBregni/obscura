"""Tests for sdk.telemetry.context."""
from typing import TypedDict
from unittest.mock import MagicMock
from sdk.telemetry.context import enrich_span_with_user, get_user_id, get_user_email
from sdk.auth.models import AuthenticatedUser


class _UserKw(TypedDict, total=False):
    user_id: str
    email: str
    roles: tuple[str, ...]
    org_id: str | None
    token_type: str
    raw_token: str


def _make_user(**overrides: object) -> AuthenticatedUser:
    defaults: _UserKw = {
        "user_id": "u1",
        "email": "a@b.com",
        "roles": ("admin",),
        "org_id": "org1",
        "token_type": "user",
        "raw_token": "tok",
    }
    for k, v in overrides.items():
        defaults[k] = v
    return AuthenticatedUser(**defaults)


class TestEnrichSpanWithUser:
    def test_with_user(self):
        span = MagicMock()
        user = _make_user()
        enrich_span_with_user(span, user)
        span.set_attribute.assert_any_call("user.id", "u1")
        span.set_attribute.assert_any_call("user.email", "a@b.com")
        span.set_attribute.assert_any_call("user.org_id", "org1")
        span.set_attribute.assert_any_call("user.token_type", "user")
        span.set_attribute.assert_any_call("user.roles", "admin")

    def test_with_none(self):
        span = MagicMock()
        enrich_span_with_user(span, None)
        span.set_attribute.assert_any_call("user.id", "system")
        span.set_attribute.assert_any_call("user.email", "system")
        span.set_attribute.assert_any_call("user.auth_type", "none")

    def test_with_no_roles(self):
        span = MagicMock()
        user = _make_user(roles=())
        enrich_span_with_user(span, user)
        # Should NOT set user.roles when empty
        role_calls = [c for c in span.set_attribute.call_args_list if c[0][0] == "user.roles"]
        assert len(role_calls) == 0

    def test_with_multiple_roles(self):
        span = MagicMock()
        user = _make_user(roles=("admin", "agent:copilot"))
        enrich_span_with_user(span, user)
        span.set_attribute.assert_any_call("user.roles", "admin,agent:copilot")

    def test_with_none_org_id(self):
        span = MagicMock()
        user = _make_user(org_id=None)
        enrich_span_with_user(span, user)
        span.set_attribute.assert_any_call("user.org_id", "")


class TestGetUserId:
    def test_with_user(self):
        assert get_user_id(_make_user()) == "u1"

    def test_with_none(self):
        assert get_user_id(None) == "system"

    def test_with_plain_object(self):
        obj = MagicMock(spec=[])
        assert get_user_id(obj) == "system"


class TestGetUserEmail:
    def test_with_user(self):
        assert get_user_email(_make_user()) == "a@b.com"

    def test_with_none(self):
        assert get_user_email(None) == "system"
