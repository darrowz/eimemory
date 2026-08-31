#!/usr/bin/env python3
"""Bounded policy surface for immutable runtime identity installation.

The immutable installer is deliberately large and remains trusted deployment
code. This small, typed module exposes only the drop-in authority name and
the units whose effective environment must be verified.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable


_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
_MANAGED_DROPIN_NAME = "zzzz-eimemory-python-runtime.conf"
_BASELINE_REQUIRED_UNITS = (
    "openclaw-loop-watch.service",
    "openclaw-gateway.service",
)
_OPENCLAW_UNITS = frozenset({
    "openclaw-gateway.service", "openclaw-loop-watch.service", "openclaw-loop-compact.service",
})


def managed_dropin_name() -> str:
    """Return the final-authority filename used for Python release identity."""

    return _MANAGED_DROPIN_NAME


def verification_units(
    discovered_units: Iterable[str], *, include_hermes: bool, include_openclaw: bool = True
) -> tuple[str, ...]:
    """Return every discovered runtime unit plus required baseline units.

    Input order is retained, duplicate names are removed, and baseline units
    are appended only when discovery did not already include them.
    """

    selected: list[str] = []
    seen: set[str] = set()

    for raw_unit in discovered_units:
        unit = str(raw_unit or "").strip()
        if not unit:
            continue
        if _UNIT_RE.fullmatch(unit) is None:
            raise ValueError("invalid systemd service unit")
        if not include_openclaw and unit in _OPENCLAW_UNITS:
            continue
        if unit not in seen:
            selected.append(unit)
            seen.add(unit)

    required_units = list(_BASELINE_REQUIRED_UNITS) if include_openclaw else []
    if include_hermes:
        required_units.append("hermes-gateway.service")

    for unit in required_units:
        if unit not in seen:
            selected.append(unit)
            seen.add(unit)

    return tuple(selected)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("dropin-name")
    verification = subparsers.add_parser("verification-units")
    verification.add_argument("--include-hermes", action="store_true")
    verification.add_argument("--exclude-openclaw", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "dropin-name":
        print(managed_dropin_name())
        return 0
    discovered = [line.strip() for line in sys.stdin if line.strip()]
    for unit in verification_units(discovered, include_hermes=bool(args.include_hermes),
                                   include_openclaw=not args.exclude_openclaw):
        print(unit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
