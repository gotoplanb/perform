"""The access-log filter.

Small, but worth testing precisely: a filter that hides a real failure is worse
than a noisy log, and the two boundaries here (status code, path matching) are
exactly where that would happen.
"""

from __future__ import annotations

import logging

import pytest

from observability.logs import QUIET_PATHS, _QuietPolls, quiet_access_logs


def access_record(path: str, status: int = 200) -> logging.LogRecord:
    """A record shaped the way uvicorn emits them."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", status),
        exc_info=None,
    )


@pytest.fixture
def quiet() -> _QuietPolls:
    return _QuietPolls()


@pytest.mark.parametrize("path", QUIET_PATHS)
def test_successful_polls_are_dropped(quiet, path):
    assert quiet.filter(access_record(path)) is False


def test_everything_else_is_kept(quiet):
    for path in ("/speak", "/status", "/buses/voice/items", "/", "/surface"):
        assert quiet.filter(access_record(path)) is True


@pytest.mark.parametrize("status", [400, 404, 422, 500, 503])
def test_a_failing_poll_still_prints(quiet, status):
    """The whole point of the status endpoints is telling you something broke.
    Hiding them when they break would invert that."""
    for path in QUIET_PATHS:
        assert quiet.filter(access_record(path, status)) is True


@pytest.mark.parametrize("status", [200, 204, 301, 307])
def test_redirects_and_no_content_are_still_quiet(quiet, status):
    assert quiet.filter(access_record("/ui", status)) is False


def test_a_query_string_does_not_defeat_the_match(quiet):
    assert quiet.filter(access_record("/ui?bus=voice")) is False


def test_a_trailing_slash_does_not_defeat_the_match(quiet):
    assert quiet.filter(access_record("/ui/")) is False


def test_a_longer_path_that_merely_starts_the_same_is_kept(quiet):
    """`/uicontrols` is not `/ui`. A prefix match would swallow it."""
    for path in ("/uicontrols", "/health-detail", "/ui/nested"):
        assert quiet.filter(access_record(path)) is True


def test_an_unrecognized_record_shape_is_kept(quiet):
    """uvicorn's args tuple is a private detail. If a future version changes it
    this filter stops matching and everything prints — the safe direction."""
    for args in [None, (), ("just one",), ("a", "b", "c"), "not a tuple"]:
        record = access_record("/ui")
        record.args = args
        assert quiet.filter(record) is True


def test_non_string_path_or_non_int_status_is_kept(quiet):
    record = access_record("/ui")
    record.args = ("127.0.0.1", "GET", 12345, "1.1", 200)
    assert quiet.filter(record) is True

    record2 = access_record("/ui")
    record2.args = ("127.0.0.1", "GET", "/ui", "1.1", "200")
    assert quiet.filter(record2) is True


def test_install_is_idempotent():
    """The lifespan may run more than once in a process under --reload."""
    import observability.logs as logs_module

    logger = logging.getLogger("uvicorn.access")
    before = len(logger.filters)
    logs_module._access_filtered = False
    try:
        quiet_access_logs()
        quiet_access_logs()
        quiet_access_logs()
        assert len(logger.filters) == before + 1
    finally:
        logger.filters = logger.filters[:before]
        logs_module._access_filtered = False
