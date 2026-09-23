"""Validation and arithmetic for context rollover targets.

Rollover targets have two representations at the command boundary:

* a positive absolute token count (including the historical ``k``/``m``
  suffixes); or
* an integer percentage of the observed model context window, written as
  ``1%`` through ``99%``.

The requested representation is retained in task/run state.  Callers resolve
it to a positive integer with :func:`effective_limit` only after telemetry has
reported a context window.
"""
from __future__ import annotations

import re
from numbers import Integral


_PERCENT = re.compile(r"([0-9]+)%\Z")
_ABSOLUTE = re.compile(r"([0-9]+)([kKmM]?)\Z")


def parse_limit(value: object) -> int | str:
    """Normalize a rollover target.

    Absolute targets are returned as positive ``int`` values.  Percentages
    are returned as canonical strings such as ``"80%"``.  ``bool`` and
    floating-point values are rejected even though Python treats booleans as
    integers; accepting either would make CLI/configuration mistakes silently
    alter the rollover policy.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("Rollover target must be a positive integer or integer percentage")
    if isinstance(value, Integral):
        result = int(value)
        if result <= 0:
            raise ValueError("Rollover target must be positive")
        return result
    if not isinstance(value, str):
        raise ValueError("Rollover target must be a positive integer or integer percentage")

    text = value.strip()
    match = _PERCENT.fullmatch(text)
    if match:
        percent = int(match.group(1))
        if not 1 <= percent <= 99:
            raise ValueError("Rollover percentage must be an integer from 1% through 99%")
        return f"{percent}%"

    match = _ABSOLUTE.fullmatch(text)
    if not match:
        raise ValueError("Use a positive integer token count (optionally k or m), or 1% through 99%")
    amount = int(match.group(1))
    multiplier = {"": 1, "k": 1000, "m": 1_000_000}[match.group(2).lower()]
    result = amount * multiplier
    if result <= 0:
        raise ValueError("Rollover target must be positive")
    return result


def _window_value(window: object) -> int:
    if isinstance(window, bool) or not isinstance(window, Integral) or int(window) <= 0:
        raise ValueError("Observed context window must be a positive integer")
    return int(window)


def effective_limit(spec: int | str, window: object = None) -> int:
    """Resolve ``spec`` to an effective positive token threshold.

    Percentage targets require a positive observed context window and use
    floor arithmetic.  Historical absolute targets continue to work without
    a window; when a window is available they retain the existing safety cap
    of 80% of that window.
    """
    normalized = parse_limit(spec)
    if isinstance(normalized, str):
        observed = _window_value(window)
        result = (observed * int(normalized[:-1])) // 100
        if result < 1:
            raise ValueError("Rollover percentage is below one token for the observed context window")
        return result

    # The legacy coordinator treated a missing/zero window as unavailable and
    # left an absolute target unchanged. Preserve that behavior for existing
    # absolute callers; percentage targets take the strict branch above.
    if window is None or window == 0:
        return normalized
    observed = _window_value(window)
    result = min(normalized, (observed * 80) // 100)
    if result < 1:
        raise ValueError("Observed context window is too small for a safe rollover threshold")
    return result


def format_limit(spec: int | str) -> str:
    """Format a normalized target for status and resume messages."""
    normalized = parse_limit(spec)
    return normalized if isinstance(normalized, str) else f"{normalized:,}"


__all__ = ["parse_limit", "effective_limit", "format_limit"]
