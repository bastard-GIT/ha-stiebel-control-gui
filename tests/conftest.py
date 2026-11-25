"""Pytest setup: always use the checked-in test table.

gui/elster.py reads ELSTER_REFERENCE_JSON once at import time (DEFAULT_JSON),
so the variable must be set here, before any test imports a gui module.
Overwrites any value from the outside, so the private reference file in
../elster_reference is never used by the test run.
"""
import os
from pathlib import Path

os.environ["ELSTER_REFERENCE_JSON"] = str(
    Path(__file__).resolve().parent / "data" / "ElsterTable.test.json"
)


_exit_status = 0


def pytest_sessionfinish(session, exitstatus):
    global _exit_status
    _exit_status = int(exitstatus)


def pytest_unconfigure(config):
    """In CI, leave without the Qt teardown.

    On the Linux runner PySide6 crashes while destroying Qt objects at
    interpreter exit ("shared QObject was deleted directly", exit code 139),
    after all tests have passed. os._exit keeps pytest's own exit status.
    """
    if os.environ.get("CI"):
        import sys

        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_exit_status)
