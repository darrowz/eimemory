#!/usr/bin/env python3
"""Classify exact release commits for the immutable installer's closure gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", "--repo-root", dest="repository", required=True, type=Path)
    parser.add_argument("--prior-commit", required=True)
    parser.add_argument("--current-commit", required=True)
    args = parser.parse_args(argv)

    try:
        # The installer executes this file with isolated system Python. Loading
        # by file path avoids importing eimemory.__init__ and its runtime
        # dependencies before the release is installed.
        module_path = (
            Path(__file__).resolve().parents[1]
            / "eimemory"
            / "governance"
            / "release_impact.py"
        )
        spec = importlib.util.spec_from_file_location("_eimemory_release_impact", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("release impact module could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        release_impact = getattr(module, "release_impact")
        impact = release_impact(
            args.repository,
            ancestor=args.prior_commit,
            current=args.current_commit,
        )
        encoded = json.dumps(impact, ensure_ascii=True, sort_keys=True)
        print(encoded)
    except Exception as exc:
        parser.exit(2, f"release impact failed: {exc}\n")
    return 0 if impact["requires_closure"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
