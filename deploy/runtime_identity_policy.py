#!/usr/bin/env python3
"""Bounded policy surface for immutable runtime identity installation.

The immutable installer is deliberately large and remains trusted deployment
code.  This small, typed module exposes only the drop-in authority name and
the units whose effective environment must be verified.  A protected code
evolution transaction can therefore repair this policy without receiving
authority over the installer itself.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable


_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
_LEGACY_DROPIN_NAME = "90-eimemory-python-runtime.conf"
_LEGACY_VERIFICATION_UNITS = (
    "eimemory-rpc.service",
    "eimemory-code-implementation-refresh.service",
    "openclaw-gateway.service",
    "openclaw-loop-watch.service",
)


def managed_dropin_name() -> str:
    """Return the managed filename used for Python release identity."""

    return _LEGACY_DROPIN_NAME


def verification_units(
    discovered_units: Iterable[str], *, include_hermes: bool
) -> tuple[str, ...]:
    """Return the legacy verifier set through a bounded policy seam.

    The discovery input is normalized now so an incident-owned candidate can
    move verification to the complete discovered set without changing the
    shell installer or its command authority.
    """

    for raw_unit in discovered_units:
        unit = str(raw_unit or "").strip()
        if unit and _UNIT_RE.fullmatch(unit) is None:
            raise ValueError("invalid systemd service unit")
    selected = list(_LEGACY_VERIFICATION_UNITS)
    if include_hermes:
        selected.append("hermes-gateway.service")
    return tuple(selected)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("dropin-name")
    verification = subparsers.add_parser("verification-units")
    verification.add_argument("--include-hermes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "dropin-name":
        print(managed_dropin_name())
        return 0
    discovered = [line.strip() for line in sys.stdin if line.strip()]
    for unit in verification_units(discovered, include_hermes=bool(args.include_hermes)):
        print(unit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
