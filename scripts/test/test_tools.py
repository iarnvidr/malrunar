"""Runs the tests of tools/ (tools/test) with the rest of the scripts tests, so the existing
`python3 -m unittest discover -s scripts/test` step covers them (standard library only)."""
import sys
import unittest
from pathlib import Path

TOOLS_TESTS = Path(__file__).resolve().parents[2] / "tools" / "test"


def load_tests(loader, tests, pattern):
    sys.path.insert(0, str(TOOLS_TESTS))
    tests.addTests(loader.discover(str(TOOLS_TESTS), pattern="test_*.py", top_level_dir=str(TOOLS_TESTS)))
    return tests


if __name__ == "__main__":
    unittest.main()
