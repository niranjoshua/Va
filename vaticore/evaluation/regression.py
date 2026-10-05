"""Compare result records number by number, for golden tests and study reruns.

Used by tests/test_golden.py (code paths on fixed synthetic data) and
examples/verify_results.py (full studies rerun on their data), so both apply
the same rule: every number within a tight relative tolerance, every flag,
label and count exactly, and nothing missing or new.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from typing import Any


def differences(
    expected: Any,
    actual: Any,
    *,
    rtol: float = 1e-6,
    atol: float = 1e-6,
    ignore: Collection[str] = (),
    path: str = "",
) -> list[str]:
    """Every place `actual` departs from `expected`, as readable paths."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out = [f"{path}/{k}: missing" for k in expected if k not in actual and k not in ignore]
        out += [f"{path}/{k}: new" for k in actual if k not in expected and k not in ignore]
        for key in sorted(expected.keys() & actual.keys()):
            if key in ignore:
                continue
            out += differences(
                expected[key], actual[key], rtol=rtol, atol=atol, ignore=ignore,
                path=f"{path}/{key}",
            )  # fmt: skip
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} became {len(actual)}"]
        out = []
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            out += differences(e, a, rtol=rtol, atol=atol, ignore=ignore, path=f"{path}[{i}]")
        return out
    numbers = (int, float)
    if (
        isinstance(expected, numbers)
        and isinstance(actual, numbers)
        and not isinstance(expected, bool)
        and not isinstance(actual, bool)
        and (isinstance(expected, float) or isinstance(actual, float))
    ):
        e, a = float(expected), float(actual)
        if math.isnan(e) and math.isnan(a):
            return []
        if math.isclose(e, a, rel_tol=rtol, abs_tol=atol):
            return []
        return [f"{path}: {e!r} became {a!r}"]
    return [] if expected == actual else [f"{path}: {expected!r} became {actual!r}"]
