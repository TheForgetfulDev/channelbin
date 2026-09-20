"""The oldest ChannelBin server this integration can run against, and the check for it.

Kept free of Home Assistant imports so the ChannelBin repo's test suite can load this file on
its own and hold MIN_CHANNELBIN_VERSION to the app version being released
(tests/test_ha_integration_packaging.py).
"""
from __future__ import annotations

import re
from typing import Any

# Raise this when the integration starts reading something from /api/ha/v1/status that an
# older server does not send. Never above the app version in the same release.
MIN_CHANNELBIN_VERSION = "0.12.0"

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


def parse_version(value: Any) -> tuple[int, int, int] | None:
    """(major, minor, patch) from a version string, ignoring any suffix; None if unreadable."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def server_too_old(payload: Any) -> tuple[bool, str | None]:
    """(too_old, reported_version) for a decoded /status payload.

    A server that reports no version, or one that cannot be parsed, counts as too old:
    servers before the check did not send the field, and guessing otherwise would let the
    integration run against an API it cannot vouch for.
    """
    reported = payload.get("app_version") if isinstance(payload, dict) else None
    if not isinstance(reported, str):
        reported = None
    parsed = parse_version(reported)
    if parsed is None:
        return True, reported
    return parsed < parse_version(MIN_CHANNELBIN_VERSION), reported
