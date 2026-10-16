"""Synthetic test directories are retained for inspection, never auto-deleted.

The test runner supplies an independent TMPDIR. No production tree is a harness
or cleanup root; cleanup, if later authorized, belongs to the caller.
"""
from contextlib import contextmanager
import tempfile


@contextmanager
def retained_directory():
    yield tempfile.mkdtemp(prefix="serviced-fixture-")
