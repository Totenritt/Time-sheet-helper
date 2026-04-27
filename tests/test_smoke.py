"""Smoke test that the package imports."""

import tsh


def test_package_imports() -> None:
    assert tsh.__version__ == "0.1.0"
