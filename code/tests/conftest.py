"""Fail the whole run up front when a required parser dependency is missing from this interpreter.

The Phase 5/6 "intermittent pypdf ModuleNotFoundError" was interpreter selection: a bare `pytest` (or a command
proxy rewriting `python -m pytest` to it) can resolve to a second Python on PATH that has no pypdf, and the PDF
tests then fail one by one far from the cause. main.py already refuses to write output in that case
(integrity.check, FAILED_PARSE).
"""
import importlib.util
import sys

import pytest

REQUIRED = ("pypdf",)


def pytest_configure(config):
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        raise pytest.UsageError(f"{sys.executable} lacks {', '.join(missing)}; run the tests with the interpreter "
                                f"that has the project dependencies (python -m pytest code/tests)")
