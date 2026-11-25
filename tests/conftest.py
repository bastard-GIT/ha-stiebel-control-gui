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
