"""Tests only (no test_ prefix: never collected): a context in which record.is_record counts the current ERA's flagged
sessions whatever their sha, since the counted line's ancestry check (record.descends) needs the deployed checkout's
git history, which a synthetic session dir does not have."""
import contextlib

import record

TEST_BOUNDARY = "test-boundary"


@contextlib.contextmanager
def any_sha_of_record():
    saved_lines, saved_desc = dict(record.RECORD_LINES), record.descends
    record.RECORD_LINES[record.ERA] = TEST_BOUNDARY
    record.descends = lambda sha, boundary: boundary == TEST_BOUNDARY
    try:
        yield
    finally:
        record.RECORD_LINES.clear()
        record.RECORD_LINES.update(saved_lines)
        record.descends = saved_desc


def of_record(fn):
    """A test run inside any_sha_of_record (script and pytest alike)."""
    import functools

    @functools.wraps(fn)
    def wrapped(*a, **k):
        with any_sha_of_record():
            return fn(*a, **k)
    return wrapped
