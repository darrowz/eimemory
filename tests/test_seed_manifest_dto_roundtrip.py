"""Isolated local DTO regressions; no package import, loader, registry or store.

Only allowlisted AST declarations are compiled. Enum values are inert fixture
labels, not assertions about production enum membership or contract meaning.
Run directly with the standard library, optionally supplying a baseline source.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Any
import unittest


DECLARATIONS = frozenset({
    "CapabilitySeedManifestError", "SeedCapability", "CapabilitySeedManifest",
    "canonical_manifest_digest", "validate_seed_manifest", "_coerce_manifest",
    "_validate_seed_capability", "_validate_seed_contract", "_object_schema",
    "_invariant_names", "_tags", "_expect_mapping", "_expect_exact_keys",
    "_copy_plain_json", "_freeze_json", "_thaw_json", "_opaque_text",
    "_capability_id", "_text", "_timestamp_text", "_digest_text", "_allowed_text",
    "apply_seed_manifest", "_seed_provenance", "SeedManifestApplyResult",
})
CONSTANTS = frozenset({"SEED_MANIFEST_SCHEMA_VERSION", "_OPAQUE_TEXT", "_SEED_PROVENANCE_SOURCE"})
SOURCE = Path(__file__).resolve().parents[1] / "eimemory/capabilities/seed_manifest.py"


def isolated_namespace(source: Path) -> dict[str, Any]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in DECLARATIONS:
            selected.append(node)
            found.add(node.name)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and node.targets[0].id in CONSTANTS):
            selected.append(node)
            found.add(node.targets[0].id)
    if found != DECLARATIONS | CONSTANTS:
        raise AssertionError("Isolated source declaration set changed")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    namespace = {
        "__name__": __name__, "Mapping": Mapping, "deepcopy": deepcopy,
        "dataclass": dataclass, "sha256": sha256, "json": json, "re": re,
        "MappingProxyType": MappingProxyType, "Any": Any,
        "RISK_TIERS": frozenset({"low"}), "SIDE_EFFECT_CLASSES": frozenset({"none"}),
    }
    # The extracted apply function receives only inert, local stubs in its test.
    # No package imports, loader, real registry or store are available.
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


def fixture(ns: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "capability_seed_manifest.v1", "manifest_id": "fixture",
        "version": "v1", "created_at": "2026-10-06T00:00:00Z", "manifest_digest": "",
        "capabilities": [{
            "capability_id": "fixture.echo", "display_name": "Echo",
            "description": "Fixture description", "owner": "Fixture owner",
            "risk_tier": "low", "tags": ["fixture"],
            "revision": {"revision_id": "fixture.echo:v1", "compatibility": "incompatible",
                "contract": {"input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "success_invariants": ["success"], "failure_invariants": ["failure"],
                    "evidence_requirements": {"minimum_refs": 1}, "dependencies": [],
                    "composition": [], "risk_tier": "low", "side_effect_class": "none"}},
        }],
    }
    value["manifest_digest"] = ns["canonical_manifest_digest"](value)
    return value


class SeedManifestDTORoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ns = isolated_namespace(SOURCE)

    def assert_round_trip(self, raw: dict[str, Any]) -> None:
        before = deepcopy(raw)
        digest = raw["manifest_digest"]
        dto = self.ns["validate_seed_manifest"](raw)
        self.assertEqual(dto.to_dict(), raw)
        self.assertEqual(dto.manifest_digest, digest)
        self.assertEqual(self.ns["canonical_manifest_digest"](dto.to_dict()), digest)
        self.assertEqual(self.ns["validate_seed_manifest"](dto.to_dict()).to_dict(), raw)
        self.assertEqual(self.ns["_coerce_manifest"](dto).to_dict(), raw)
        self.assertEqual(self.ns["_coerce_manifest"](raw).to_dict(), raw)
        self.assertEqual(raw, before)

    def test_canonical_manifest_round_trip(self) -> None:
        self.assert_round_trip(fixture(self.ns))

    def test_each_accepted_text_field_preserves_whitespace_and_digest(self) -> None:
        for field in ("display_name", "description", "owner", "created_at"):
            for prefix, suffix in ((" ", ""), ("", " "), ("\t", "\n")):
                with self.subTest(field=field, prefix=prefix, suffix=suffix):
                    raw = fixture(self.ns)
                    target = raw if field == "created_at" else raw["capabilities"][0]
                    target[field] = prefix + target[field] + suffix
                    raw["manifest_digest"] = self.ns["canonical_manifest_digest"](raw)
                    self.assert_round_trip(raw)

    def test_apply_argument_projection_preserves_previous_trimming(self) -> None:
        captured = {}

        class InertRegistry:
            def register_seed_manifest(self, **kwargs):
                captured.update(kwargs)
                return (), ()

        ns = dict(self.ns)
        # Function globals refer to the isolated namespace, so patch only its
        # external names temporarily. Constructors merely return their kwargs.
        self.ns.update({"CapabilityDefinition": lambda **kw: kw,
                        "CapabilityRevision": lambda **kw: kw,
                        "exact_runtime_scope": lambda value: value,
                        "CapabilityRegistryError": ValueError})
        try:
            raw = fixture(self.ns)
            raw["created_at"] = " \t2026-10-06T00:00:00Z\n"
            for field in ("display_name", "description", "owner"):
                raw["capabilities"][0][field] = " \t" + raw["capabilities"][0][field] + "\n"
            raw["manifest_digest"] = self.ns["canonical_manifest_digest"](raw)
            dto = self.ns["validate_seed_manifest"](raw)
            for input_value in (raw, dto):
                with self.subTest(kind=type(input_value).__name__):
                    captured.clear()
                    self.ns["apply_seed_manifest"](InertRegistry(), runtime_scope="inert-scope", manifest=input_value)
                    definition = captured["definitions"][0]
                    revision = captured["revisions"][0]
                    for field in ("display_name", "description", "owner"):
                        self.assertEqual(definition[field], raw["capabilities"][0][field].strip())
                    self.assertEqual(definition["created_at"], raw["created_at"].strip())
                    self.assertEqual(revision["created_at"], raw["created_at"].strip())
                    self.assertEqual(captured["manifest_digest"], raw["manifest_digest"])
                    self.assertEqual(captured["runtime_scope"], "inert-scope")
        finally:
            for key in set(self.ns) - set(ns):
                del self.ns[key]

    def test_text_rejection_rules_unchanged(self) -> None:
        for value in ("", " \t\n", "x" * 4097, None, 1, True):
            with self.subTest(value_type=type(value).__name__, length=len(value) if isinstance(value, str) else None):
                with self.assertRaises(self.ns["CapabilitySeedManifestError"]):
                    self.ns["_text"](value, field_name="fixture")
        self.assertEqual(self.ns["_text"]("x" * 4096, field_name="fixture"), "x" * 4096)

    def test_mismatched_digest_still_rejected_for_mapping_and_dto(self) -> None:
        raw = fixture(self.ns)
        dto = self.ns["validate_seed_manifest"](raw)
        raw["manifest_digest"] = "0" * 64
        for value in (raw, replace(dto, manifest_digest="0" * 64)):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(self.ns["CapabilitySeedManifestError"], "digest does not match"):
                    self.ns["_coerce_manifest"](value)

    def test_unknown_fields_still_rejected(self) -> None:
        raw = fixture(self.ns)
        raw["unexpected"] = "value"
        raw["manifest_digest"] = self.ns["canonical_manifest_digest"](raw)
        with self.assertRaisesRegex(self.ns["CapabilitySeedManifestError"], "invalid declarative fields"):
            self.ns["validate_seed_manifest"](raw)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--source":
        SOURCE = Path(sys.argv[2])
        del sys.argv[1:3]
    unittest.main()
