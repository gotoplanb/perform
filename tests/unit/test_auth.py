"""The optional bearer gate.

SPEC §7: "Loopback-only with no auth is fine to start. Anything exposed over a
tunnel needs a shared secret first." So the default really is no auth, and the
knob exists so turning it on is an edit rather than a refactor.

An untested auth path is the wrong thing to ship even when it is off by default,
because the moment it matters is the moment someone sets PERFORM_TOKEN and
assumes it works.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from config.settings import Settings
from deps import require_token


def settings(token: str | None) -> Settings:
    return Settings(PERFORM_TOKEN=token)  # type: ignore[call-arg]


def bearer(value: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


async def test_no_token_configured_means_no_auth():
    """The loopback default. Nothing is required and nothing is checked."""
    await require_token(None, settings(None))
    await require_token(bearer("anything at all"), settings(None))


async def test_an_empty_token_is_treated_as_unset():
    """`PERFORM_TOKEN=` in a .env is the template form, not a demand for a
    literal empty credential."""
    await require_token(None, settings(""))


async def test_the_right_token_passes():
    await require_token(bearer("s3cret-value"), settings("s3cret-value"))


async def test_a_wrong_token_is_403():
    with pytest.raises(HTTPException) as excinfo:
        await require_token(bearer("wrong"), settings("s3cret-value"))
    assert excinfo.value.status_code == 403


async def test_a_missing_token_is_403_when_one_is_configured():
    """The case that matters: auth on, caller sent nothing."""
    with pytest.raises(HTTPException) as excinfo:
        await require_token(None, settings("s3cret-value"))
    assert excinfo.value.status_code == 403
    assert "bearer" in excinfo.value.detail.lower()


async def test_a_token_that_is_a_prefix_of_the_real_one_fails():
    """compare_digest is length-safe, but assert the behavior rather than
    trusting the call site to have used it."""
    with pytest.raises(HTTPException):
        await require_token(bearer("s3cret"), settings("s3cret-value"))


@pytest.mark.parametrize("wrong", ["", " s3cret-value", "s3cret-value ", "S3CRET-VALUE"])
async def test_near_misses_all_fail(wrong):
    with pytest.raises(HTTPException):
        await require_token(bearer(wrong), settings("s3cret-value"))
