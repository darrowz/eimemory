"""Pure synthetic applicability contracts; no storage or Runtime initialization."""
from collections import UserList
from dataclasses import replace
import hashlib
import json
import sys
import unittest

from eimemory.capabilities.applicability import evaluate_applicability
from eimemory.capabilities.contracts import CapabilityContractError, contract_digest, validate_applicability_allowlists
from eimemory.capabilities.models import CapabilityBinding, CapabilityKnowledgeLink

T = "2026-01-01T00:00:00.000000Z"
ENVIRONMENT = {"synthetic": True}
ENV_DIGEST = hashlib.sha256(json.dumps(ENVIRONMENT, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
OBSERVATIONS = [{"observation_id": "obs-a", "observed_at": T, "payload": {"environment_fingerprint": ENVIRONMENT}}]


def binding(constraints=None):
    return CapabilityBinding(
        "bind-a", "audit.test", "rev-a", "synthetic", "provider-a", "1" * 64,
        ("read",), {"max": 1}, ENVIRONMENT, T,
        applicability={"scope": "global", **(constraints or {})},
        advertisement_evidence_refs=("audit-advertisement",),
    )


def knowledge(constraints=None, relation_type="supports"):
    return CapabilityKnowledgeLink(
        link_id="link-a", capability_id="audit.test", capability_revision_id="rev-a",
        knowledge_record_id="knowledge-a", relation_type=relation_type,
        source_status="active", applicability="applicable", source_trust="high",
        review_state="approved", temporal_validity={},
        environment_constraints={"scope": "global", **(constraints or {})},
        contradiction_state="none", applicability_score=1.0,
        applicability_evidence_refs=("audit-applicability",), evidence_refs=("audit-evidence",),
        created_at=T,
    )


def evaluate(*, binding_payload=None, knowledge_payload=None, scope="global", binding_status="active"):
    return evaluate_applicability(
        capability_scope=scope,
        binding_descriptor=binding_payload if binding_payload is not None else binding().to_dict(),
        binding_status=binding_status, observations=OBSERVATIONS,
        knowledge_links=[] if knowledge_payload is None else [{"payload": knowledge_payload}],
        requirement={}, at_time=T,
    )


def historical_payload(kind, constraints):
    """Self-consistent JSON DTO simulating the old permissive typed boundary."""
    obj = binding() if kind == "binding" else knowledge()
    payload = obj.to_dict()
    constraint_field = "applicability" if kind == "binding" else "environment_constraints"
    digest_field = "binding_digest" if kind == "binding" else "link_digest"
    payload[constraint_field] = {"scope": "global", **constraints}
    payload[digest_field] = contract_digest({key: value for key, value in payload.items() if key != digest_field})
    return payload


class ApplicabilityAllowlistTypeTests(unittest.TestCase):
    FIELDS = ("allowed_scopes", "allowed_environment_digests")

    def test_typed_null_scalar_mapping_and_bad_items_are_rejected(self):
        values = (None, "global", "", {}, {"global": True}, True, 1, [None], [1], [False], [{}], [[]], [""], [" "])
        for field in self.FIELDS:
            for value in values:
                for factory in (binding, knowledge):
                    with self.subTest(field=field, value=value, entity=factory.__name__):
                        with self.assertRaises(CapabilityContractError):
                            factory({field: value})

    def test_typed_items_reuse_existing_scope_and_sha_contracts(self):
        valid_scopes = ("global", "TEAM_scope:west-1.v2", "123", " padded-scope ")
        for factory in (binding, knowledge):
            factory({"allowed_scopes": list(valid_scopes)})
            factory({"allowed_environment_digests": [ENV_DIGEST, "A" * 64, " " + ENV_DIGEST + " "]})
            for field, value in (("allowed_scopes", ["has/slash"]), ("allowed_environment_digests", ["bad-digest"]), ("allowed_environment_digests", ["g" * 64])):
                with self.subTest(entity=factory.__name__, field=field, value=value):
                    with self.assertRaises(CapabilityContractError):
                        factory({field: value})

    def test_arrays_are_bounded_at_256_and_not_rewritten(self):
        for field, item in (("allowed_scopes", "global"), ("allowed_environment_digests", ENV_DIGEST)):
            for factory in (binding, knowledge):
                with self.subTest(field=field, entity=factory.__name__):
                    obj = factory({field: [item] * 256})
                    name = "applicability" if factory is binding else "environment_constraints"
                    self.assertEqual(obj.to_dict()[name][field], [item] * 256)
                    with self.assertRaises(CapabilityContractError):
                        factory({field: [item] * 257})
                    with self.assertRaises(CapabilityContractError):
                        factory({field: {item}})
                    with self.assertRaises(CapabilityContractError):
                        factory({field: (x for x in [item])})

    def test_raw_python_sequences_are_rejected_before_normalization(self):
        for field, item in (("allowed_scopes", "TEAM_scope:west-1.v2"), ("allowed_environment_digests", ENV_DIGEST)):
            for factory in (binding, knowledge):
                for value in (UserList([item]), range(0)):
                    for key in (field, " " + field + " "):
                        with self.subTest(field=key, entity=factory.__name__, container=type(value).__name__):
                            with self.assertRaises(CapabilityContractError):
                                factory({key: value})

    def test_raw_container_check_preserves_other_json_normalization(self):
        for field, item in (("allowed_scopes", "TEAM_scope:west-1.v2"), ("allowed_environment_digests", ENV_DIGEST)):
            for factory in (binding, knowledge):
                for value in ([item], (item,), [], ()):
                    with self.subTest(field=field, entity=factory.__name__, container=type(value).__name__, empty=not value):
                        canonical = factory({field: value})
                        padded_key = factory({" " + field + " ": value})
                        self.assertEqual(canonical.to_dict(), padded_key.to_dict())
                # The raw restriction is local to these two top-level fields.
                extension = {"custom_extension": UserList([item]), "nested": {field: range(0)}}
                obj = factory(extension)
                name = "applicability" if factory is binding else "environment_constraints"
                self.assertEqual(obj.to_dict()[name]["custom_extension"], [item])
                self.assertEqual(obj.to_dict()[name]["nested"], {field: []})
                with self.assertRaises(CapabilityContractError):
                    factory({field: [item], " " + field + " ": [item]})

    def test_list_and_frozen_tuple_have_identical_bytes_and_digest(self):
        for field, values in (("allowed_scopes", ["TEAM_scope:west-1.v2", "global", "global"]), ("allowed_environment_digests", ["A" * 64, ENV_DIGEST, ENV_DIGEST])):
            for factory in (binding, knowledge):
                with self.subTest(field=field, entity=factory.__name__):
                    from_list = factory({field: values})
                    from_tuple = factory({field: tuple(values)})
                    self.assertEqual(from_list.to_dict(), from_tuple.to_dict())
                    name = "applicability" if factory is binding else "environment_constraints"
                    self.assertIsInstance(getattr(from_list, name)[field], tuple)
                    self.assertEqual(from_list.to_dict()[name][field], values)

    def test_validation_and_model_freezing_do_not_mutate_original_array(self):
        values = ["TEAM_scope:west-1.v2", "global", "global"]
        constraints = {"allowed_scopes": values}
        before = json.dumps(constraints, sort_keys=True)
        validate_applicability_allowlists(constraints, field="test")
        obj = binding(constraints)
        self.assertEqual(json.dumps(constraints, sort_keys=True), before)
        values.append("later")
        self.assertEqual(obj.to_dict()["applicability"]["allowed_scopes"], ["TEAM_scope:west-1.v2", "global", "global"])

    def test_missing_fields_add_no_constraint_for_both_models(self):
        for factory in (binding, knowledge):
            obj = factory()
            name = "binding_payload" if factory is binding else "knowledge_payload"
            result = evaluate(**{name: obj.to_dict()})
            self.assertEqual(result["status"], "applicable")
            self.assertFalse(result["blocking"])

    def test_explicit_empty_allowlists_block_both_entities(self):
        for field in self.FIELDS:
            for factory in (binding, knowledge):
                for sequence in ([], ()):
                    with self.subTest(field=field, entity=factory.__name__, sequence=type(sequence).__name__):
                        obj = factory({field: sequence})
                        key = "binding_payload" if factory is binding else "knowledge_payload"
                        result = evaluate(**{key: obj.to_dict()})
                        self.assertEqual(result["status"], "blocked")
                        self.assertTrue(result["blocking"])
                        self.assertEqual(result["maturity_ceiling"], "observed")

    def test_matching_scope_and_environment_arrays_are_applicable(self):
        for factory in (binding, knowledge):
            obj = factory({"allowed_scopes": ["global"], "allowed_environment_digests": [ENV_DIGEST]})
            key = "binding_payload" if factory is binding else "knowledge_payload"
            self.assertEqual(evaluate(**{key: obj.to_dict()})["status"], "applicable")

    def test_global_in_scope_array_is_literal_not_wildcard(self):
        b = evaluate(binding_payload=binding({"allowed_scopes": ["global"]}).to_dict(), scope="tenant:other")
        k = evaluate(knowledge_payload=knowledge({"allowed_scopes": ["global"]}).to_dict(), scope="tenant:other")
        self.assertEqual(b["status"], "blocked")
        self.assertEqual(k["knowledge"]["applicable_link_count"], 0)
        self.assertEqual(k["knowledge"]["status"], "qualified")  # Existing ordinary-context mismatch policy.

    def test_ordinary_nonmatching_knowledge_context_policy_is_unchanged(self):
        for constraints in ({"allowed_scopes": ["other"]}, {"allowed_environment_digests": ["0" * 64]}):
            self.assertEqual(evaluate(binding_payload=binding(constraints).to_dict())["status"], "blocked")
            result = evaluate(knowledge_payload=knowledge(constraints).to_dict())
            self.assertEqual(result["status"], "qualified")
            self.assertFalse(result["blocking"])
            self.assertEqual(result["maturity_ceiling"], "evaluated")
            self.assertEqual(result["knowledge"]["applicable_link_count"], 0)

    def test_historical_json_dto_invalid_types_hard_block_instead_of_qualify(self):
        for field in self.FIELDS:
            for value in (None, "global", {}, True, 1, [None], [1], [{}], [" "]):
                for kind in ("binding", "knowledge"):
                    with self.subTest(field=field, value=value, kind=kind):
                        payload = historical_payload(kind, {field: value})
                        key = "binding_payload" if kind == "binding" else "knowledge_payload"
                        result = evaluate(**{key: payload})
                        self.assertEqual(result["status"], "blocked")
                        self.assertTrue(result["blocking"])
                        self.assertIn("applicability_allowlist_invalid", result["reason_codes"])

    def test_empty_or_invalid_second_field_cannot_hide_behind_first_mismatch(self):
        for constraints in (
            {"scope": "other", "allowed_scopes": []},
            {"allowed_scopes": ["other"], "allowed_environment_digests": []},
            {"allowed_scopes": ["other"], "allowed_environment_digests": None},
        ):
            result = evaluate(knowledge_payload=historical_payload("knowledge", constraints))
            self.assertEqual(result["status"], "blocked")
            self.assertNotEqual(result["knowledge"]["status"], "qualified")

    def test_empty_and_invalid_constraints_block_limiting_knowledge_too(self):
        for field in self.FIELDS:
            for value in ([], None, "global"):
                payload = historical_payload("knowledge", {field: value})
                payload["relation_type"] = "limits_applicability"
                result = evaluate(knowledge_payload=payload)
                self.assertEqual(result["status"], "blocked")

    def test_quarantine_is_never_weakened_by_invalid_restriction(self):
        result = evaluate(binding_payload=historical_payload("binding", {"allowed_scopes": None}), binding_status="quarantined")
        self.assertEqual(result["status"], "quarantined")
        self.assertTrue(result["blocking"])

    def test_other_declared_fields_and_extension_metadata_are_unchanged(self):
        constraints = {"custom_extension": {"allowed_scopes": "opaque metadata"}, "scope": "global"}
        b = binding(constraints)
        self.assertEqual(b.to_dict()["applicability"], constraints)
        self.assertEqual(evaluate(binding_payload=b.to_dict())["status"], "applicable")

    def test_no_runtime_or_provider_modules_initialized(self):
        self.assertNotIn("eimemory.api.runtime", sys.modules)
        self.assertFalse([name for name in sys.modules if name.startswith(("eimemory.providers", "eimemory.adapters", "eimemory.embeddings"))])


if __name__ == "__main__":
    unittest.main()
