"""Finite ingress-kind tests without importing or executing repository services.

Only AST-selected scalar/data helpers run, with inert registry/decision records.
The safety evaluator, security classifiers, models, Runtime, adapters and storage
are never loaded or run. A separate AST assertion checks the actual call wiring.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
import unittest
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


class DecisionRecorder:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class SourceRecorder:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class RegistryRecorder:
    def __init__(self, entries):
        self.entries = list(entries)
        self.calls = 0

    def list_sources(self):
        self.calls += 1
        return list(self.entries)


def load_helpers(relative_path, function_names, constant_names=(), extra=None):
    """Compile only explicitly named pure nodes; never module imports/classes."""
    path = ROOT / relative_path
    tree = ast.parse(path.read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            nodes.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constant_names for target in node.targets
        ):
            nodes.append(node)
    assert found == set(function_names)
    namespace = dict(
        Any=Any, Mapping=Mapping, re=re, json=json, math=math,
        sha256=sha256, urlsplit=urlsplit, urlunsplit=urlunsplit,
        SourceTrustDecision=DecisionRecorder,
    )
    namespace.update(extra or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), namespace)
    return namespace


INGEST = load_helpers(
    "eimemory/knowledge/ingest.py",
    {"_normalize_source_kind", "_canonical_source_kind_payload"},
    {"SUPPORTED_SOURCE_KINDS"},
)
TRUST = load_helpers(
    "eimemory/knowledge/source_trust.py",
    {"source_trust_for_kind", "trust_tier_for_score", "normalize_source_uri",
     "resolve_source_trust", "_source_kind", "_source_uri", "_source_id",
     "_nested", "_first_text", "_claimed_trust", "_finite_float", "_clamp"},
    {"TRUST_AUTHORITY", "UNREGISTERED_TRUST_CAP", "CAPABILITY_TRUST_THRESHOLD",
     "DEFAULT_SOURCE_TRUST", "REGISTERED_SOURCE_TRUST_CAP", "_POLICY", "POLICY_DIGEST"},
)
SAFETY = load_helpers(
    "eimemory/knowledge/safety.py",
    {"_source_kind", "_nested", "_first_text", "_requires_strict_provenance", "_status", "_screening_text"},
    {"SOURCE_TRUST", "EXTERNAL_MEMORY_TYPES"},
    {"DEFAULT_SOURCE_TRUST": TRUST["DEFAULT_SOURCE_TRUST"]},
)


class KnowledgeKindIngressBounds(unittest.TestCase):
    def canonical(self, payload):
        normalized = INGEST["_normalize_source_kind"](payload.get("source_kind", ""))
        return INGEST["_canonical_source_kind_payload"](payload, source_kind=normalized)

    def assert_aligned(self, view, expected):
        self.assertEqual(view["source_kind"], expected)
        self.assertEqual(view["meta"]["source_kind"], expected)
        self.assertEqual(TRUST["_source_kind"](view), expected)
        self.assertEqual(SAFETY["_source_kind"](view), expected)
        self.assertTrue(SAFETY["_requires_strict_provenance"](view, source_kind=expected))

    def test_original_shadow_counterexample_is_preserved(self):
        raw = {"source_kind": "docs", "meta": {"source_kind": "custom"}}
        self.assertEqual(TRUST["_source_kind"](raw), "docs")
        self.assertEqual(SAFETY["_source_kind"](raw), "custom")
        self.assertFalse(SAFETY["_requires_strict_provenance"](raw, source_kind="custom"))
        with self.assertRaisesRegex(ValueError, "conflicting source_kind in meta"):
            self.canonical(raw)

    def test_each_nonempty_nested_conflict_is_rejected_without_mutation(self):
        for carrier in ("meta", "content", "provenance"):
            for conflicting in ("paper", "custom", "unknown", "unsupported", "not-a-kind", True, 1, {"kind": "docs"}):
                with self.subTest(carrier=carrier, conflicting=conflicting):
                    raw = {"source_kind": "docs", carrier: {"source_kind": conflicting, "keep": ["original"]}}
                    original = deepcopy(raw)
                    with self.assertRaisesRegex(ValueError, f"conflicting source_kind in {carrier}"):
                        self.canonical(raw)
                    self.assertEqual(raw, original)

    def test_top_level_whitelist_is_not_salvaged_by_nested_kind(self):
        for top in (None, "", " \t ", "unknown", "custom", False, 0):
            with self.subTest(top=top):
                raw = {"source_kind": top, "meta": {"source_kind": "docs"}}
                self.assertEqual(INGEST["_normalize_source_kind"](top), "")
                with self.assertRaises(ValueError):
                    self.canonical(raw)
        with self.assertRaises(ValueError):
            self.canonical({"meta": {"source_kind": "docs"}})

    def test_all_supported_top_level_kinds_align(self):
        for kind in sorted(INGEST["SUPPORTED_SOURCE_KINDS"]):
            with self.subTest(kind=kind):
                self.assert_aligned(self.canonical({"source_kind": kind}), kind)

    def test_equivalent_aliases_and_case_align(self):
        for top, nested, expected in (("Documentation", " DOCS ", "docs"),
                                      ("docs", "documentation", "docs"),
                                      (" OFFICIAL-DOCS ", "official_docs", "official_docs"),
                                      ("api-docs", "API_DOCS", "api_docs")):
            with self.subTest(top=top, nested=nested):
                raw = {"source_kind": top, **{k: {"source_kind": nested} for k in ("meta", "content", "provenance")}}
                self.assert_aligned(self.canonical(raw), expected)

    def test_missing_or_empty_nested_declarations_keep_no_declaration_semantics(self):
        for carrier in ("meta", "content", "provenance"):
            for value in (None, "", " \n\t ", False, 0, [], {}):
                with self.subTest(carrier=carrier, value=value):
                    raw = {"source_kind": "docs", carrier: {"source_kind": value}}
                    original = deepcopy(raw)
                    self.assert_aligned(self.canonical(raw), "docs")
                    self.assertEqual(raw, original)
        self.assert_aligned(self.canonical({"source_kind": "docs", "meta": {}}), "docs")

    def test_nonmapping_carriers_keep_other_fields_untouched(self):
        for meta in (None, [], "legacy metadata"):
            with self.subTest(meta=meta):
                raw = {"source_kind": "docs", "meta": meta, "content": ["literal"], "provenance": "literal"}
                original = deepcopy(raw)
                view = self.canonical(raw)
                self.assert_aligned(view, "docs")
                self.assertEqual(raw, original)
                self.assertEqual(view["content"], raw["content"])
                self.assertEqual(view["provenance"], raw["provenance"])

    def test_mapping_carrier_is_copied_and_checked(self):
        raw = {"source_kind": "docs", "meta": MappingProxyType({"source_kind": "documentation", "keep": 3})}
        view = self.canonical(raw)
        self.assert_aligned(view, "docs")
        self.assertEqual(view["meta"]["keep"], 3)
        self.assertEqual(raw["meta"]["source_kind"], "documentation")
        with self.assertRaises(ValueError):
            self.canonical({"source_kind": "docs", "meta": MappingProxyType({"source_kind": "paper"})})

    def test_status_text_trust_claims_and_compatibility_fields_are_preserved(self):
        raw = {"source_kind": "docs", "source_id": "registered", "source_trust": 1,
               "status": "quarantined", "title": "A title", "text": "Original\ntext",
               "summary": "A summary", "detail": "A detail", "uri": "https://example.invalid/doc",
               "meta": {"source_kind": "documentation", "fetch_source": "connector-name", "confidence": 0.99, "source_uri": "legacy-uri", "status": "active"},
               "content": {"source_kind": "DOCS", "body": "Original body", "text": "Nested text"},
               "provenance": {"source_kind": "docs", "source_id": "preserved"}}
        original = deepcopy(raw)
        view = self.canonical(raw)
        self.assertEqual(raw, original)
        self.assertIsNot(view, raw)
        self.assertIsNot(view["meta"], raw["meta"])
        self.assert_aligned(view, "docs")
        self.assertEqual(SAFETY["_screening_text"](view), SAFETY["_screening_text"](raw))
        self.assertEqual(SAFETY["_status"](view), "quarantined")
        self.assertEqual(TRUST["_source_uri"](view), TRUST["_source_uri"](raw))
        self.assertEqual(TRUST["_claimed_trust"](view), TRUST["_claimed_trust"](raw))
        for key in ("status", "source_id", "source_trust", "uri", "content", "provenance", "text"):
            self.assertEqual(view[key], raw[key])
        for key in ("fetch_source", "confidence", "source_uri", "status"):
            self.assertEqual(view["meta"][key], raw["meta"][key])

    def test_empty_registry_does_not_gain_trust(self):
        raw = {"source_kind": "docs", "uri": "https://example.invalid/doc", "meta": {"source_kind": "documentation"}, "source_trust": 1}
        registry = RegistryRecorder([])
        decision = TRUST["resolve_source_trust"](self.canonical(raw), registry=registry, connector_id="server.test")
        self.assertEqual(decision.score, 0.5)
        self.assertIn("missing_source_id", decision.reasons)
        self.assertEqual(registry.calls, 1)

    def test_registry_binding_failures_remain_capped(self):
        entry = SourceRecorder(source_id="registered", source_kind="docs", uri="https://example.invalid/doc", enabled=True,
                               metadata={"connector_id": "server.test", "trust": 1})
        base = {"source_id": "registered", "source_kind": "docs", "uri": "https://example.invalid/doc"}
        cases = (({}, "other", "connector_mismatch"),
                 ({"uri": "https://example.invalid/other"}, "server.test", "registry_uri_mismatch"),
                 ({"source_id": "missing"}, "server.test", "registry_source_not_found"),
                 ({"source_kind": "paper"}, "server.test", "source_kind_mismatch"))
        for changes, connector, reason in cases:
            with self.subTest(reason=reason):
                decision = TRUST["resolve_source_trust"](self.canonical({**base, **changes}), registry=RegistryRecorder([entry]), connector_id=connector)
                self.assertEqual(decision.score, 0.5)
                self.assertIn(reason, decision.reasons)
        entry.enabled = False
        decision = TRUST["resolve_source_trust"](self.canonical(base), registry=RegistryRecorder([entry]), connector_id="server.test")
        self.assertEqual(decision.score, 0.5)
        self.assertIn("registry_source_disabled", decision.reasons)

    def test_valid_registered_identity_and_kind_cap_are_preserved(self):
        for kind, expected in (("docs", 1.0), ("webpage", 0.65), ("github_repo", 0.85)):
            with self.subTest(kind=kind):
                entry = SourceRecorder(source_id="registered", source_kind=kind, uri="https://example.invalid/doc", enabled=True,
                                       metadata={"connector_id": "server.test", "trust": 1})
                raw = {"source_id": "registered", "source_kind": kind, "uri": "https://example.invalid/doc", "meta": {"source_kind": kind}}
                decision = TRUST["resolve_source_trust"](self.canonical(raw), registry=RegistryRecorder([entry]), connector_id="server.test")
                self.assertEqual(decision.score, expected)
                self.assertEqual(decision.reasons, ("registry_verified",))
                self.assertEqual(decision.connector_id, "server.test")

    def test_actual_ingress_wires_same_canonical_base_without_executing_it(self):
        tree = ast.parse((ROOT / "eimemory/knowledge/ingest.py").read_text())
        ingress = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "ingest_knowledge_source")
        calls = {node.func.id: node for node in ast.walk(ingress)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id in {"resolve_source_trust", "evaluate_knowledge_safety"}}
        self.assertEqual(set(calls), {"resolve_source_trust", "evaluate_knowledge_safety"})
        for name, call in calls.items():
            with self.subTest(call=name):
                self.assertIsInstance(call.args[0], ast.Dict)
                first = call.args[0]
                self.assertIsNone(first.keys[0])
                self.assertIsInstance(first.values[0], ast.Name)
                self.assertEqual(first.values[0].id, "canonical_payload")
        normalize_calls = [node for node in ast.walk(ingress) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Name) and node.func.id == "_normalize_source_kind"]
        self.assertEqual(len(normalize_calls), 1)
        resolver = {kw.arg: ast.unparse(kw.value) for kw in calls["resolve_source_trust"].keywords}
        self.assertEqual(resolver["registry"], "getattr(runtime, 'sources', None)")
        self.assertEqual(resolver["connector_id"], "normalized_connector_id")
        safety = {kw.arg: ast.unparse(kw.value) for kw in calls["evaluate_knowledge_safety"].keywords}
        self.assertEqual(safety["trust_decision"], "trust_decision")
        self.assertEqual(safety["task"], "'ingest'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
