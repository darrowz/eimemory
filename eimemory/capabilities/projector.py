"""Reproducible, profile-specific projection of capability evidence.

The projector consumes immutable v3 observations and a frozen Profile
resolution.  It does not manufacture capabilities, infer provider identity
from a host/version, or let accumulated knowledge raise a maturity level by
itself.  Knowledge applicability is an explicit input surface for the bridge
in WP8; observations and deterministic profile thresholds remain the only
path to a persisted state.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.applicability import evaluate_applicability
from eimemory.capabilities.contracts import (
    CapabilityContractError,
    normalize_capability_id,
    normalize_json_payload,
    normalize_opaque_id,
)
from eimemory.capabilities.models import CapabilityStateSnapshot
from eimemory.capabilities.observations import CapabilityObservations
from eimemory.capabilities.profiles import CapabilityProfiles
from eimemory.capabilities.registry import MutationReceipt, exact_runtime_scope
from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


PROJECTOR_SCHEMA = "capability.state_projection.v1"
DEFAULT_ALGORITHM_REVISION = "capability-projector.v2"
_MATURITY_RANK = {
    "unknown": 0,
    "observed": 1,
    "evaluated": 2,
    "reliable": 3,
    "regressed": -1,
    "quarantined": -2,
    "retired": -3,
}


class CapabilityProjectionError(ValueError):
    """A projection input is incomplete, ambiguous, or internally invalid."""


@dataclass(frozen=True, slots=True)
class CapabilityProjectionResult:
    profile_id: str
    profile_digest: str
    capability_scope: str
    input_watermark: str
    projection_digest: str
    persisted: bool
    snapshots: tuple[dict[str, Any], ...]
    blocked: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTOR_SCHEMA,
            "ok": not self.blocked,
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "capability_scope": self.capability_scope,
            "input_watermark": self.input_watermark,
            "projection_digest": self.projection_digest,
            "persisted": self.persisted,
            "snapshots": [dict(item) for item in self.snapshots],
            "blocked": [dict(item) for item in self.blocked],
        }


class CapabilityStateProjector:
    """Project only selected capability revisions/bindings from explicit data."""

    def __init__(self, store: RuntimeStore) -> None:
        self._store = store
        self._profiles = CapabilityProfiles(store)
        self._observations = CapabilityObservations(store)

    def project(
        self,
        profile_key: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        at_time: str = "",
        max_candidates: int = 100,
        observation_limit: int = 500,
        algorithm_revision: str = DEFAULT_ALGORITHM_REVISION,
        persist: bool = True,
        affected_capability_ids: Sequence[object] = (),
        max_relations: int = 499,
    ) -> CapabilityProjectionResult:
        """Compute a deterministic snapshot set from frozen current inputs.

        ``affected_capability_ids`` is an optional incremental surface.  The
        projector expands it through both dependency and composition edges, so
        a late component failure invalidates every affected composite without a
        whole-profile scan.  Omitting it preserves the original full-profile
        behaviour and compatibility of this method.

        Every bounded query is fail-closed.  A page at its declared cap is
        never treated as complete evidence or a complete relation graph.
        """

        scope = exact_runtime_scope(runtime_scope)
        logical_scope = _capability_scope(capability_scope)
        if not isinstance(observation_limit, int) or isinstance(observation_limit, bool):
            raise CapabilityProjectionError("observation_limit must be an integer")
        if not 1 <= observation_limit <= 500:
            raise CapabilityProjectionError("observation_limit must be from 1 to 500")
        if not isinstance(max_candidates, int) or isinstance(max_candidates, bool):
            raise CapabilityProjectionError("max_candidates must be an integer")
        if not 1 <= max_candidates <= 499:
            raise CapabilityProjectionError("max_candidates must be from 1 to 499")
        if not isinstance(algorithm_revision, str) or not algorithm_revision.strip():
            raise CapabilityProjectionError("algorithm_revision is required")
        if not isinstance(max_relations, int) or isinstance(max_relations, bool):
            raise CapabilityProjectionError("max_relations must be an integer")
        if not 1 <= max_relations <= 499:
            raise CapabilityProjectionError("max_relations must be from 1 to 499")
        affected = _normalize_affected_capability_ids(affected_capability_ids)
        # Freshness and temporal applicability are stateful at a point in
        # time.  Make that point explicit in the projection inputs even for a
        # live caller, rather than silently treating the newest evidence as
        # perpetually current.  Historical callers retain exact replay by
        # passing ``at_time`` themselves.
        projection_at_time = at_time or now_iso()

        resolution = self._profiles.resolve(
            profile_key,
            runtime_scope=scope,
            capability_scope=logical_scope,
            at_time=projection_at_time,
            max_candidates=max_candidates,
        )
        relation_result = self._read_relation_context(
            scope=scope,
            capability_scope=logical_scope,
            at_time=projection_at_time,
            max_relations=max_relations,
        )
        relations = relation_result["relations"]
        candidate_ids = _resolution_capability_ids(resolution)
        unknown_affected = sorted(set(affected).difference(candidate_ids))
        target_ids = (
            _expand_affected_capabilities(affected, relations, candidate_ids)
            if affected
            else set(candidate_ids)
        )
        if affected:
            target_ids = _expand_projection_targets(
                resolution,
                target_ids=target_ids,
                relations=relations,
                selected_capabilities=candidate_ids,
            )
        binding_context = self._read_binding_context(
            scope=scope,
            capability_scope=logical_scope,
            capability_ids=target_ids,
            at_time=projection_at_time,
        )
        pairs = _resolution_binding_pairs(resolution, target_ids=target_ids)
        observation_result = self._read_projection_inputs(
            scope=scope,
            capability_scope=logical_scope,
            pairs=pairs,
            observation_limit=observation_limit,
            at_time=projection_at_time,
            binding_contexts=binding_context["bindings"],
        )
        observations = observation_result["observations"]
        watermark_observations = observation_result["watermark_observations"]
        knowledge_links = observation_result["knowledge_links"]
        observation_index = _index_observations(observations)
        knowledge_index = _index_knowledge_links(knowledge_links)
        global_watermark = _projection_watermark(
            resolution,
            watermark_observations,
            knowledge_links=knowledge_links,
            relations=relations,
            runtime_scope=scope,
            capability_scope=logical_scope,
            at_time=projection_at_time,
            binding_contexts=binding_context["bindings"],
            binding_context_truncated_capability_ids=set(binding_context["truncated_capability_ids"]),
        )

        candidates, blocked = _project_candidates(
            resolution,
            observation_index=observation_index,
            knowledge_index=knowledge_index,
            observation_truncated=bool(observation_result["observation_truncated"]),
            observation_truncated_pairs=set(observation_result["observation_truncated_pairs"]),
            portable_observation_index=observation_result["portable_observation_index"],
            portable_observation_truncated_pairs=set(observation_result["portable_observation_truncated_pairs"]),
            knowledge_truncated=bool(observation_result["knowledge_truncated"]),
            knowledge_truncated_pairs=set(observation_result["knowledge_truncated_pairs"]),
            relation_truncated=bool(relation_result["truncated"]),
            global_watermark=global_watermark,
            algorithm_revision=algorithm_revision.strip(),
            at_time=projection_at_time,
            target_ids=target_ids,
            binding_contexts=binding_context["bindings"],
            binding_context_truncated=set(binding_context["truncated_capability_ids"]),
        )
        _apply_relationship_gates(candidates, relations=relations)
        blocked.extend(_profile_requirement_gaps(candidates))
        for capability_id in unknown_affected:
            blocked.append(
                {
                    "capability_id": capability_id,
                    "reason": "affected_capability_not_selected_by_profile",
                }
            )
        snapshots = [
            candidate
            for candidate in candidates
            if candidate.get("emit") is True and candidate.get("snapshot") is not None
        ]

        snapshot_receipts: dict[str, MutationReceipt] = {}
        if persist and snapshots:
            def mutation(repository):
                receipts: dict[str, MutationReceipt] = {}
                for candidate in snapshots:
                    snapshot = candidate["snapshot"]
                    stored = repository.register_snapshot(
                        snapshot,
                        scope=scope,
                        provider_binding_id=candidate.get("provider_binding_id") or None,
                        request_key=f"capability-snapshot:{snapshot.snapshot_id}",
                    )
                    receipts[snapshot.snapshot_id] = MutationReceipt.from_stored(stored)
                return receipts

            snapshot_receipts = self._store.mutate_capabilities_atomically(mutation)

        public_snapshots: list[dict[str, Any]] = []
        for candidate in snapshots:
            snapshot = candidate["snapshot"]
            receipt = snapshot_receipts.get(snapshot.snapshot_id)
            public_snapshots.append(
                {
                    "capability_id": snapshot.capability_id,
                    "capability_revision_id": snapshot.capability_revision_id,
                    "provider_binding_id": candidate.get("provider_binding_id") or "_revision",
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_digest": snapshot.snapshot_digest,
                    "maturity": snapshot.maturity,
                    "confidence": snapshot.confidence,
                    "input_watermark": snapshot.input_watermark,
                    "computed_at": snapshot.computed_at,
                    "reason_codes": list(snapshot.reason_codes),
                    "evidence_refs": list(snapshot.evidence_refs),
                    "input_digests": dict(snapshot.input_digests),
                    "idempotent": receipt.idempotent if receipt is not None else False,
                }
            )
        public_snapshots.sort(
            key=lambda item: (
                str(item["capability_id"]),
                str(item["capability_revision_id"]),
                str(item["provider_binding_id"]),
            )
        )
        public_blocked = tuple(sorted(blocked, key=_blocked_sort_key))
        projection_digest = _digest(
            {
                "schema": PROJECTOR_SCHEMA,
                "resolution_digest": resolution["resolution_digest"],
                "input_watermark": global_watermark,
                "algorithm_revision": algorithm_revision.strip(),
                "incremental_targets": sorted(target_ids) if affected else [],
                "snapshots": public_snapshots,
                "blocked": public_blocked,
            }
        )
        return CapabilityProjectionResult(
            profile_id=str(resolution["profile"]["profile_id"]),
            profile_digest=str(resolution["profile"]["profile_digest"]),
            capability_scope=logical_scope,
            input_watermark=global_watermark,
            projection_digest=projection_digest,
            persisted=bool(persist and snapshots),
            snapshots=tuple(public_snapshots),
            blocked=public_blocked,
        )

    def project_affected(
        self,
        profile_key: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        affected_capability_ids: Sequence[object],
        at_time: str = "",
        max_candidates: int = 100,
        observation_limit: int = 500,
        algorithm_revision: str = DEFAULT_ALGORITHM_REVISION,
        persist: bool = True,
        max_relations: int = 499,
    ) -> CapabilityProjectionResult:
        """Incrementally project changed capabilities and dependency composites.

        This named convenience method keeps callers from using an unscoped
        ad-hoc filter.  It is equivalent to ``project(...,
        affected_capability_ids=...)`` and retains the same exact-scope and
        fail-closed semantics.
        """

        return self.project(
            profile_key,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            at_time=at_time,
            max_candidates=max_candidates,
            observation_limit=observation_limit,
            algorithm_revision=algorithm_revision,
            persist=persist,
            affected_capability_ids=affected_capability_ids,
            max_relations=max_relations,
        )

    def _read_relation_context(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        at_time: str,
        max_relations: int,
    ) -> dict[str, Any]:
        def reader(repository):
            rows = repository.list_effective_entities(
                entity_type="relation",
                scope=scope,
                capability_scope=capability_scope,
                status="active",
                at_time=at_time,
                limit=max_relations + 1,
            )
            return rows

        rows = self._store.read_capabilities(reader)
        truncated = len(rows) > max_relations
        if truncated:
            rows = rows[:max_relations]
        relations = [_relation_view(item) for item in rows]
        return {
            "relations": relations,
            "truncated": truncated,
            "watermark": _digest(
                {
                    "truncated": truncated,
                    "relations": [
                        {
                            "relation_id": item.get("relation_id"),
                            "relation_digest": item.get("relation_digest"),
                            "state_digest": item.get("state_digest"),
                        }
                        for item in relations
                    ],
                }
            ),
        }

    def _read_binding_context(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        capability_ids: set[str],
        at_time: str,
    ) -> dict[str, Any]:
        """Read current binding descriptors for explicit portability checks.

        The lookup is per selected semantic capability so an unrelated adapter
        with many bindings cannot silently truncate another capability's
        portability decision.  A capped capability is retained as direct-only
        evidence; cross-binding inheritance is simply refused for it.
        """

        bindings: dict[str, dict[str, Any]] = {}
        truncated_capability_ids: set[str] = set()
        for capability_id in sorted(capability_ids):
            rows = self._store.read_capabilities(
                lambda repository, capability_id=capability_id: repository.list_effective_entities(
                    entity_type="binding",
                    scope=scope,
                    capability_scope=capability_scope,
                    status=None,
                    at_time=at_time,
                    capability_id=capability_id,
                    limit=500,
                )
            )
            if len(rows) >= 500:
                truncated_capability_ids.add(capability_id)
            for entity in rows:
                descriptor = dict(entity.payload) if isinstance(entity.payload, Mapping) else {}
                binding_id = str(descriptor.get("binding_id") or entity.entity_id or "")
                if not binding_id:
                    continue
                bindings[binding_id] = {
                    "binding_id": binding_id,
                    "status": str(entity.status or ""),
                    "state_digest": str(entity.state_digest or ""),
                    "effective_at": str(entity.effective_at or ""),
                    "descriptor": descriptor,
                }
        return {
            "bindings": bindings,
            "truncated_capability_ids": truncated_capability_ids,
        }

    def _read_projection_inputs(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        pairs: set[tuple[str, str, str]],
        observation_limit: int,
        at_time: str,
        binding_contexts: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Read only exact profile-selected evidence.

        Both whole-profile and incremental projection use the same exact
        capability/revision/binding reads.  The incremental caller simply
        supplies fewer pairs.  This deliberately replaces the historical
        scope-wide aggregate page: unrelated high-volume capabilities cannot
        hide a late failure, poison a profile-specific watermark, or consume
        another capability's evidence budget.
        """

        observations: list[Mapping[str, Any]] = []
        knowledge_links: list[Mapping[str, Any]] = []
        observation_truncated_pairs: set[tuple[str, str, str]] = set()
        portable_observation_truncated_pairs: set[tuple[str, str]] = set()
        knowledge_truncated_pairs: set[tuple[str, str]] = set()
        seen_observation_ids: set[str] = set()
        seen_link_ids: set[str] = set()
        queried_knowledge: set[tuple[str, str]] = set()
        queried_revisions: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for capability_id, revision_id, binding_id in sorted(pairs):
            rows = self._observations.list(
                runtime_scope=scope,
                capability_scope=capability_scope,
                capability_id=capability_id,
                capability_revision_id=revision_id,
                provider_binding_id=binding_id,
                until=at_time,
                limit=observation_limit,
            )
            if len(rows) >= observation_limit:
                observation_truncated_pairs.add((capability_id, revision_id, binding_id))
            for row in rows:
                observation_id = str(row.get("observation_id") or "")
                if observation_id and observation_id not in seen_observation_ids:
                    observations.append(row)
                    seen_observation_ids.add(observation_id)
            revision_key = (capability_id, revision_id)
            if revision_key not in queried_revisions:
                revision_rows = self._observations.list(
                    runtime_scope=scope,
                    capability_scope=capability_scope,
                    capability_id=capability_id,
                    capability_revision_id=revision_id,
                    until=at_time,
                    limit=observation_limit,
                )
                queried_revisions[revision_key] = revision_rows
                if len(revision_rows) >= observation_limit:
                    portable_observation_truncated_pairs.add(revision_key)
            knowledge_key = (capability_id, revision_id)
            if knowledge_key in queried_knowledge:
                continue
            queried_knowledge.add(knowledge_key)
            links = self._store.read_capabilities(
                lambda repository, capability_id=capability_id, revision_id=revision_id: repository.list_knowledge_links(
                    scope=scope,
                    capability_scope=capability_scope,
                    capability_id=capability_id,
                    capability_revision_id=revision_id,
                    limit=observation_limit,
                )
            )
            if len(links) >= observation_limit:
                knowledge_truncated_pairs.add(knowledge_key)
            for link in _knowledge_links_as_of(links, at_time=at_time):
                link_id = str(link.get("link_id") or "")
                if link_id and link_id not in seen_link_ids:
                    knowledge_links.append(link)
                    seen_link_ids.add(link_id)
        portable = _portable_observation_index(
            [row for rows in queried_revisions.values() for row in rows],
            target_pairs=pairs,
            binding_contexts=binding_contexts,
            excluded_revisions=portable_observation_truncated_pairs,
        )
        return {
            "observations": observations,
            # A selected binding can lawfully consume portable evidence from a
            # different compatible binding of the *same* capability revision.
            # Those source rows may not be direct observations for a selected
            # target pair, but they are still material projection inputs and
            # must therefore move the public input watermark.
            "watermark_observations": [
                row
                for revision_rows in queried_revisions.values()
                for row in revision_rows
            ],
            "knowledge_links": knowledge_links,
            "observation_truncated": False,
            "observation_truncated_pairs": observation_truncated_pairs,
            "portable_observation_index": portable,
            "portable_observation_truncated_pairs": portable_observation_truncated_pairs,
            "knowledge_truncated": False,
            "knowledge_truncated_pairs": knowledge_truncated_pairs,
        }


def _project_candidates(
    resolution: Mapping[str, Any],
    *,
    observation_index: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
    knowledge_index: Mapping[tuple[str, str], list[Mapping[str, Any]]],
    observation_truncated: bool,
    observation_truncated_pairs: set[tuple[str, str, str]],
    portable_observation_index: Mapping[tuple[str, str, str], list[Mapping[str, Any]]],
    portable_observation_truncated_pairs: set[tuple[str, str]],
    knowledge_truncated: bool,
    knowledge_truncated_pairs: set[tuple[str, str]],
    relation_truncated: bool,
    global_watermark: str,
    algorithm_revision: str,
    at_time: str,
    target_ids: set[str],
    binding_contexts: Mapping[str, Mapping[str, Any]],
    binding_context_truncated: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    profile = resolution.get("profile") if isinstance(resolution.get("profile"), Mapping) else {}
    profile_id = str(profile.get("profile_id") or "")
    profile_digest = str(profile.get("profile_digest") or "")
    capability_scope = str(resolution.get("capability_scope") or "")
    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for requirement_entry in resolution.get("requirements") or ():
        if not isinstance(requirement_entry, Mapping):
            raise CapabilityProjectionError("profile resolution contains an invalid requirement entry")
        capability_id = str(requirement_entry.get("capability_id") or "")
        requirement = requirement_entry.get("requirement")
        if not capability_id or not isinstance(requirement, Mapping):
            raise CapabilityProjectionError("profile resolution requirement is missing identity or policy")
        if capability_id not in target_ids:
            continue
        definition = requirement_entry.get("definition") if isinstance(requirement_entry.get("definition"), Mapping) else {}
        descriptor = definition.get("descriptor") if isinstance(definition.get("descriptor"), Mapping) else {}
        risk_tier = str(descriptor.get("risk_tier") or "")
        allowed_risks = set(str(item) for item in requirement.get("allowed_risk_tiers") or ())
        if allowed_risks and risk_tier not in allowed_risks:
            blocked.append(
                {
                    "capability_id": capability_id,
                    "reason": "risk_tier_not_allowed_by_profile",
                    "risk_tier": risk_tier,
                }
            )
            continue
        revisions = requirement_entry.get("revisions") or ()
        bindings = requirement_entry.get("bindings") or ()
        bindings_by_revision: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            binding_descriptor = binding.get("descriptor") if isinstance(binding.get("descriptor"), Mapping) else {}
            revision_id = str(binding_descriptor.get("capability_revision_id") or "")
            if revision_id:
                bindings_by_revision[revision_id].append(binding)
        if not revisions:
            blocked.append({"capability_id": capability_id, "reason": "no_active_revision"})
            continue
        for revision in revisions:
            if not isinstance(revision, Mapping):
                continue
            revision_id = str(revision.get("entity_id") or "")
            revision_descriptor = revision.get("descriptor") if isinstance(revision.get("descriptor"), Mapping) else {}
            if not revision_id:
                blocked.append({"capability_id": capability_id, "reason": "revision_identity_missing"})
                continue
            selected_bindings = bindings_by_revision.get(revision_id, [])
            if not selected_bindings:
                inactive_reason = _inactive_binding_reason(
                    binding_contexts,
                    capability_id=capability_id,
                    capability_revision_id=revision_id,
                )
                blocked.append(
                    {
                        "capability_id": capability_id,
                        "capability_revision_id": revision_id,
                        "reason": inactive_reason,
                    }
                )
                continue
            for binding in selected_bindings:
                binding_id = str(binding.get("entity_id") or "")
                if not binding_id:
                    blocked.append(
                        {
                            "capability_id": capability_id,
                            "capability_revision_id": revision_id,
                            "reason": "binding_identity_missing",
                        }
                    )
                    continue
                rows = list(observation_index.get((capability_id, revision_id, binding_id), ()))
                portable_rows = list(
                    portable_observation_index.get((capability_id, revision_id, binding_id), ())
                )
                candidate = _candidate_from_observations(
                    capability_id=capability_id,
                    capability_revision_id=revision_id,
                    provider_binding_id=binding_id,
                    profile_id=profile_id,
                    profile_digest=profile_digest,
                    capability_scope=capability_scope,
                    requirement=requirement,
                    revision_descriptor=revision_descriptor,
                    binding_descriptor=(
                        binding.get("descriptor") if isinstance(binding.get("descriptor"), Mapping) else {}
                    ),
                    observations=_merge_observation_rows(rows, portable_rows),
                    knowledge_links=list(knowledge_index.get((capability_id, revision_id), ())),
                    binding_status=str(binding.get("status") or ""),
                    observation_truncated=(
                        observation_truncated
                        or (capability_id, revision_id, binding_id) in observation_truncated_pairs
                    ),
                    portable_observation_truncated=(capability_id, revision_id) in portable_observation_truncated_pairs,
                    knowledge_truncated=(
                        knowledge_truncated or (capability_id, revision_id) in knowledge_truncated_pairs
                    ),
                    relation_truncated=relation_truncated,
                    binding_context_truncated=capability_id in binding_context_truncated,
                    portable_observation_count=len(portable_rows),
                    global_watermark=global_watermark,
                    algorithm_revision=algorithm_revision,
                    at_time=at_time,
                )
                if candidate.get("snapshot") is None:
                    blocked.append(
                        {
                            "capability_id": capability_id,
                            "capability_revision_id": revision_id,
                            "provider_binding_id": binding_id,
                            "reason": candidate["reason"],
                        }
                    )
                candidates.append(candidate)
    return candidates, blocked


def _candidate_from_observations(
    *,
    capability_id: str,
    capability_revision_id: str,
    provider_binding_id: str,
    profile_id: str,
    profile_digest: str,
    capability_scope: str,
    requirement: Mapping[str, Any],
    revision_descriptor: Mapping[str, Any],
    binding_descriptor: Mapping[str, Any],
    observations: list[Mapping[str, Any]],
    knowledge_links: list[Mapping[str, Any]],
    binding_status: str,
    observation_truncated: bool,
    portable_observation_truncated: bool,
    knowledge_truncated: bool,
    relation_truncated: bool,
    binding_context_truncated: bool,
    portable_observation_count: int,
    global_watermark: str,
    algorithm_revision: str,
    at_time: str,
) -> dict[str, Any]:
    if observation_truncated:
        return {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "reason": "observation_window_truncated",
            "snapshot": None,
        }
    if portable_observation_truncated or binding_context_truncated:
        # Direct evidence remains usable.  Only portability is refused when a
        # source-binding page cannot prove its full compatibility set.
        portable_observation_count = 0
    portability_context_incomplete = portable_observation_truncated or binding_context_truncated
    if knowledge_truncated:
        return {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "reason": "knowledge_link_window_truncated",
            "snapshot": None,
        }
    if relation_truncated:
        return {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "reason": "relation_window_truncated",
            "snapshot": None,
        }
    if not observations:
        return {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "reason": "no_observation_evidence",
            "snapshot": None,
        }
    ordered = sorted(observations, key=_observation_key)
    payloads = [row["payload"] for row in ordered if isinstance(row.get("payload"), Mapping)]
    if not payloads:
        return {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "reason": "observation_payload_missing",
            "snapshot": None,
        }
    decisive = [payload for payload in payloads if str(payload.get("verdict") or "") in {"pass", "fail"}]
    passes = [payload for payload in decisive if str(payload.get("verdict")) == "pass"]
    failures = [payload for payload in decisive if str(payload.get("verdict")) == "fail"]
    latest = payloads[-1]
    latest_verdict = str(latest.get("verdict") or "")
    latest_failure = next(
        (
            row
            for row in reversed(ordered)
            if str((row.get("payload") or {}).get("verdict") or "") == "fail"
        ),
        None,
    )
    latest_success = next(
        (
            row
            for row in reversed(ordered)
            if str((row.get("payload") or {}).get("verdict") or "") == "pass"
        ),
        None,
    )
    source_evidence_refs = sorted(
        {
            str(ref)
            for payload in payloads
            for ref in payload.get("evidence_refs", ())
            if str(ref or "")
        }
    )
    if not source_evidence_refs:
        return {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "reason": "observation_evidence_refs_missing",
            "snapshot": None,
        }
    evidence_refs = sorted(
        {
            *source_evidence_refs,
            *(str(row.get("observation_id") or "") for row in ordered),
        }
        - {""}
    )
    pass_rate = len(passes) / len(decisive) if decisive else 0.0
    consecutive_passes = _consecutive_passes(payloads)
    min_evidence = max(
        int(requirement.get("min_evidence_count") or 0),
        int(requirement.get("min_sample_count") or 0),
    )
    min_pass_rate = float(requirement.get("min_pass_rate") or 0.0)
    min_consecutive = int(requirement.get("min_consecutive_passes") or 0)
    reasons: list[str] = []
    if portability_context_incomplete:
        reasons.append("portable_evidence_inheritance_refused_incomplete_binding_context")
    elif portable_observation_count:
        reasons.append("portable_evidence_inherited_from_compatible_binding")
    maturity = "observed"
    verdicts = [str(payload.get("verdict") or "") for payload in payloads]
    if "invalid" in verdicts:
        maturity = "quarantined"
        reasons.append("invalid_observation_quarantined")
    elif not decisive:
        reasons.append("no_decisive_verdict")
    elif latest_verdict == "fail":
        maturity = "regressed"
        reasons.append("latest_observation_failed")
    elif latest_verdict in {"stale", "invalid"}:
        maturity = "observed"
        reasons.append("latest_observation_not_current")
    else:
        maturity = "evaluated"
        threshold_reasons: list[str] = []
        if len(payloads) < min_evidence:
            threshold_reasons.append("insufficient_evidence_count")
        if pass_rate < min_pass_rate:
            threshold_reasons.append("pass_rate_below_profile_threshold")
        if consecutive_passes < min_consecutive:
            threshold_reasons.append("consecutive_passes_below_profile_threshold")
        reasons.extend(threshold_reasons)
        if not threshold_reasons:
            maturity = "reliable"
    if failures and latest_verdict == "pass":
        # A failure received after a previous projection may have an earlier
        # event timestamp.  The snapshot input includes every immutable row,
        # so it is invalidated and recomputed even when the newest event is a
        # pass.  Do not silently retain a cached reliable state.
        reasons.append("failure_present_in_recomputed_evidence_window")
    applicability = evaluate_applicability(
        capability_scope=capability_scope,
        binding_descriptor=binding_descriptor,
        binding_status=binding_status,
        observations=ordered,
        knowledge_links=knowledge_links,
        requirement=requirement,
        at_time=at_time,
    )
    applicability_ceiling = str(applicability.get("maturity_ceiling") or "reliable")
    if applicability_ceiling == "quarantined":
        maturity = "quarantined"
    elif maturity not in {"regressed", "quarantined"}:
        maturity = _cap_maturity(maturity, applicability_ceiling)
    reasons.extend(str(item) for item in applicability.get("reason_codes", ()) if str(item or ""))
    evidence_refs = sorted(
        {
            *evidence_refs,
            *(str(item) for item in applicability.get("evidence_refs", ()) if str(item or "")),
        }
    )
    confidence = _confidence(
        observation_count=len(payloads),
        decisive_count=len(decisive),
        pass_rate=pass_rate,
        maturity=maturity,
    )
    input = {
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "capability_id": capability_id,
        "capability_revision_id": capability_revision_id,
        "provider_binding_id": provider_binding_id,
        "requirement": dict(requirement),
        "revision_digest": str(revision_descriptor.get("contract_digest") or ""),
        "binding_digest": str(binding_descriptor.get("binding_digest") or ""),
        "observations": [
            {
                "observation_id": row.get("observation_id"),
                "observation_digest": row.get("observation_digest"),
                "observed_at": row.get("observed_at"),
                "portability": (
                    dict(row.get("projection_portability"))
                    if isinstance(row.get("projection_portability"), Mapping)
                    else {}
                ),
            }
            for row in ordered
        ],
        "knowledge_links": [
            {
                "link_id": row.get("link_id"),
                "link_digest": row.get("link_digest"),
            }
            for row in sorted(knowledge_links, key=lambda row: str(row.get("link_id") or ""))
        ],
        "applicability": {
            "input_digest": applicability.get("input_digest"),
            "status": applicability.get("status"),
        },
        "global_input_watermark": global_watermark,
        "algorithm_revision": algorithm_revision,
    }
    input_digest = _digest(input)
    snapshot_id = f"capability-snapshot-{input_digest[:40]}"
    input_watermark = _digest(
        {
            "global": global_watermark,
            "projection_input": input_digest,
            "revision": capability_revision_id,
            "binding": provider_binding_id,
        }
    )
    computed_at = str(applicability.get("reference_time") or ordered[-1].get("observed_at") or now_iso())
    retry_metrics = _controlled_retry_metrics(payloads)
    reliability_metrics: dict[str, Any] = {
        "pass_at_1": round(pass_rate, 6),
        "consecutive_passes": consecutive_passes,
        "required_pass_rate": min_pass_rate,
        "required_consecutive_passes": min_consecutive,
    }
    if portable_observation_count:
        reliability_metrics["portable_observation_count"] = portable_observation_count
    reliability_metrics.update(retry_metrics)
    snapshot = CapabilityStateSnapshot(
        snapshot_id=snapshot_id,
        capability_id=capability_id,
        capability_revision_id=capability_revision_id,
        profile_id=profile_id,
        maturity=maturity,
        confidence=confidence,
        evidence_refs=evidence_refs,
        sample_sufficiency={
            "observation_count": len(payloads),
            "decisive_count": len(decisive),
            "required_count": min_evidence,
            "sufficient": len(payloads) >= min_evidence,
        },
        reliability_metrics=reliability_metrics,
        latest_success_ref=_observation_ref(latest_success),
        latest_failure_ref=_observation_ref(latest_failure),
        regression_streak=_regression_streak(payloads),
        dependency_state={"status": "unchecked"},
        knowledge_applicability=dict(applicability["knowledge"]),
        provider_applicability=dict(applicability["binding"]),
        environment_applicability=dict(applicability["environment"]),
        input_watermark=input_watermark,
        algorithm_revision=algorithm_revision,
        computed_at=computed_at,
        scope=capability_scope,
        reason_codes=tuple(sorted(set(reasons))) or ("evidence_observed",),
        input_digests={
            "projection_input": input_digest,
            "profile_resolution": profile_digest,
            "applicability": str(applicability.get("input_digest") or ""),
        },
    )
    return {
        "capability_id": capability_id,
        "capability_revision_id": capability_revision_id,
        "provider_binding_id": provider_binding_id,
        "requirement": dict(requirement),
        "revision_dependencies": tuple(
            str(item) for item in (revision_descriptor.get("contract") or {}).get("dependencies", ())
        ),
        "revision_supersedes": str(revision_descriptor.get("supersedes_revision_id") or ""),
        "revision_compatibility": str(revision_descriptor.get("compatibility") or ""),
        "emit": True,
        "snapshot": snapshot,
        "reason": "",
    }


def _apply_relationship_gates(
    candidates: list[dict[str, Any]],
    *,
    relations: Sequence[Mapping[str, Any]],
) -> None:
    """Apply declarative dependency/composition state without promotion.

    The relation direction is explicit: ``source_capability_id`` depends on or
    composes ``target_capability_id``.  A relationship is never evidence by
    itself; it can only cap or quarantine an evidence-backed source snapshot.
    The bounded fixed-point iteration exists so a late failure propagates from
    a component to every dependent composite in one incremental projection.
    """

    active = [candidate for candidate in candidates if candidate.get("snapshot") is not None]
    if not active:
        return
    edges = _relation_edges(candidates, relations)
    edges_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        edges_by_source[str(edge["source_capability_id"])].append(edge)

    _apply_supersession_gates(active)
    _apply_capability_supersession_gates(active, relations=relations)
    for _ in range(len(active) + 1):
        changed = False
        best_state = _best_capability_state(active)
        for candidate in sorted(active, key=_candidate_sort_key):
            snapshot = candidate.get("snapshot")
            if snapshot is None:
                continue
            if str(snapshot.dependency_state.get("status") or "") == "superseded":
                continue
            source_edges = edges_by_source.get(snapshot.capability_id, [])
            if not source_edges:
                if str(snapshot.dependency_state.get("status") or "") == "unchecked":
                    candidate["snapshot"] = _with_projection_state(
                        snapshot,
                        dependency_state={"status": "not_required", "relations": []},
                        reason_codes=snapshot.reason_codes,
                        evidence_refs=snapshot.evidence_refs,
                    )
                    changed = True
                continue
            evaluations = [_evaluate_relation_edge(edge, best_state) for edge in source_edges]
            blocked = [item for item in evaluations if item["satisfied"] is not True]
            dependency_state = {
                "status": "satisfied" if not blocked else "blocked",
                "relations": evaluations,
                "missing_or_insufficient": sorted(
                    {
                        str(item["target_capability_id"])
                        for item in blocked
                        if str(item.get("target_capability_id") or "")
                    }
                ),
            }
            relation_refs = {
                str(item["relation_id"])
                for item in evaluations
                if str(item.get("relation_id") or "")
            }
            relation_refs.update(
                str(ref)
                for item in evaluations
                for ref in item.get("evidence_refs", ())
                if str(ref or "")
            )
            reasons = set(snapshot.reason_codes)
            reasons.update(
                str(reason)
                for item in blocked
                for reason in item.get("reason_codes", ())
                if str(reason or "")
            )
            maturity = snapshot.maturity
            confidence = snapshot.confidence
            if blocked:
                if any(str(item.get("on_failure") or "") == "quarantined" for item in blocked):
                    maturity = "quarantined"
                    reasons.add("relation_quarantined")
                elif maturity not in {"regressed", "quarantined"}:
                    maturity = "observed"
                confidence = min(confidence, 0.25)
                reasons.add("dependency_or_composite_not_ready")
            updated_reasons = tuple(sorted(reasons))
            updated_refs = tuple(sorted({*snapshot.evidence_refs, *relation_refs}))
            if not _projection_state_matches(
                snapshot,
                maturity=maturity,
                confidence=confidence,
                dependency_state=dependency_state,
                reason_codes=updated_reasons,
                evidence_refs=updated_refs,
            ):
                candidate["snapshot"] = _with_projection_state(
                    snapshot,
                    maturity=maturity,
                    confidence=confidence,
                    dependency_state=dependency_state,
                    reason_codes=updated_reasons,
                    evidence_refs=updated_refs,
                )
                changed = True
        if not changed:
            return

    # A cycle should already have been rejected by the registry.  If a
    # corrupted imported graph slipped through, retain evidence but fail closed
    # instead of letting an arbitrary iteration order decide readiness.
    for candidate in active:
        snapshot = candidate.get("snapshot")
        if snapshot is None:
            continue
        state = dict(snapshot.dependency_state)
        state["status"] = "blocked"
        state["cycle_or_unstable_projection"] = True
        candidate["snapshot"] = _with_projection_state(
            snapshot,
            maturity="quarantined",
            confidence=min(snapshot.confidence, 0.1),
            dependency_state=state,
            reason_codes=tuple(sorted({*snapshot.reason_codes, "relationship_projection_unstable"})),
            evidence_refs=snapshot.evidence_refs,
        )


def _profile_requirement_gaps(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return explicit L5-blocking gaps for evidence-backed weak snapshots.

    Persisting an observed/regressed/quarantined snapshot is useful evidence,
    but it must not make a profile projection ``ok`` merely because a snapshot
    object exists.  The L5 v3 assessor consumes ``blocked`` as its hard gate,
    so profile minima are checked after dependency, conflict, and supersession
    effects have reached a fixed point.
    """

    gaps: list[dict[str, Any]] = []
    accepted_minima = {"observed", "evaluated", "reliable"}
    for candidate in candidates:
        snapshot = candidate.get("snapshot")
        if snapshot is None:
            continue
        requirement = candidate.get("requirement") if isinstance(candidate.get("requirement"), Mapping) else {}
        minimum = str(requirement.get("minimum_maturity") or "")
        identity = {
            "capability_id": snapshot.capability_id,
            "capability_revision_id": snapshot.capability_revision_id,
            "provider_binding_id": str(candidate.get("provider_binding_id") or ""),
        }
        if minimum not in accepted_minima:
            gaps.append(
                {
                    **identity,
                    "reason": "profile_minimum_maturity_invalid_for_readiness",
                    "required_maturity": minimum,
                    "actual_maturity": snapshot.maturity,
                }
            )
            continue
        if _MATURITY_RANK.get(snapshot.maturity, -99) < _MATURITY_RANK[minimum]:
            gaps.append(
                {
                    **identity,
                    "reason": "projected_maturity_below_profile_requirement",
                    "required_maturity": minimum,
                    "actual_maturity": snapshot.maturity,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_refs": list(snapshot.evidence_refs),
                }
            )
    return gaps


def _relation_edges(
    candidates: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Combine revision-local dependencies with active registered relations."""

    edges: list[dict[str, Any]] = []
    for candidate in candidates:
        snapshot = candidate.get("snapshot")
        requirement = candidate.get("requirement") if isinstance(candidate.get("requirement"), Mapping) else {}
        if snapshot is None or requirement.get("require_dependencies") is not True:
            continue
        for target in sorted(set(str(item) for item in candidate.get("revision_dependencies") or () if str(item))):
            edges.append(
                {
                    "relation_id": f"revision-contract:{snapshot.capability_revision_id}:{target}",
                    "relation_type": "depends_on",
                    "source_capability_id": snapshot.capability_id,
                    "target_capability_id": target,
                    "relation_policy": {"minimum_maturity": "evaluated", "on_dependency_failure": "blocked"},
                    "evidence_refs": [],
                }
            )
    for relation in relations:
        relation_type = str(relation.get("relation_type") or "")
        if relation_type not in {"depends_on", "composes", "conflicts_with"}:
            continue
        source = str(relation.get("source_capability_id") or "")
        target = str(relation.get("target_capability_id") or "")
        if not source or not target:
            continue
        edges.append(
            {
                "relation_id": str(relation.get("relation_id") or ""),
                "relation_type": relation_type,
                "source_capability_id": source,
                "target_capability_id": target,
                "relation_policy": (
                    dict(relation.get("relation_policy"))
                    if isinstance(relation.get("relation_policy"), Mapping)
                    else {}
                ),
                "evidence_refs": _relation_evidence_refs(relation),
            }
        )
    edges.sort(key=lambda item: (str(item["source_capability_id"]), str(item["relation_id"])))
    return edges


def _evaluate_relation_edge(edge: Mapping[str, Any], best_state: Mapping[str, str]) -> dict[str, Any]:
    policy = edge.get("relation_policy") if isinstance(edge.get("relation_policy"), Mapping) else {}
    required_maturity = str(policy.get("minimum_maturity") or policy.get("required_maturity") or "evaluated")
    if required_maturity not in {"observed", "evaluated", "reliable"}:
        return {
            **dict(edge),
            "required_maturity": "evaluated",
            "target_maturity": "unknown",
            "satisfied": False,
            "on_failure": "quarantined",
            "reason_codes": ["relation_policy_minimum_maturity_invalid"],
        }
    target = str(edge.get("target_capability_id") or "")
    target_maturity = str(best_state.get(target) or "unknown")
    relation_type = str(edge.get("relation_type") or "")
    if relation_type == "conflicts_with":
        threshold = _MATURITY_RANK[required_maturity]
        conflicted = _MATURITY_RANK.get(target_maturity, -99) >= threshold
        on_failure = str(policy.get("on_conflict") or "blocked")
        if on_failure not in {"blocked", "quarantined"}:
            return {
                **dict(edge),
                "required_maturity": required_maturity,
                "target_maturity": target_maturity,
                "satisfied": False,
                "on_failure": "quarantined",
                "reason_codes": ["relation_policy_on_conflict_invalid"],
            }
        return {
            **dict(edge),
            "required_maturity": required_maturity,
            "target_maturity": target_maturity,
            "satisfied": not conflicted,
            "on_failure": on_failure,
            "reason_codes": ["capability_conflict_active"] if conflicted else [],
        }
    satisfied = _MATURITY_RANK.get(target_maturity, -99) >= _MATURITY_RANK[required_maturity]
    on_failure = str(policy.get("on_dependency_failure") or policy.get("on_failure") or "blocked")
    if on_failure not in {"blocked", "quarantined"}:
        return {
            **dict(edge),
            "required_maturity": required_maturity,
            "target_maturity": target_maturity,
            "satisfied": False,
            "on_failure": "quarantined",
            "reason_codes": ["relation_policy_on_failure_invalid"],
        }
    return {
        **dict(edge),
        "required_maturity": required_maturity,
        "target_maturity": target_maturity,
        "satisfied": satisfied,
        "on_failure": on_failure,
        "reason_codes": [] if satisfied else ["declared_relation_target_not_ready"],
    }


def _apply_supersession_gates(candidates: Sequence[dict[str, Any]]) -> None:
    superseded: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for candidate in candidates:
        snapshot = candidate.get("snapshot")
        previous = str(candidate.get("revision_supersedes") or "")
        if snapshot is not None and previous:
            superseded[(snapshot.capability_id, previous)].append(
                (snapshot.capability_revision_id, str(candidate.get("revision_compatibility") or ""))
            )
    for candidate in candidates:
        snapshot = candidate.get("snapshot")
        if snapshot is None:
            continue
        successors = superseded.get((snapshot.capability_id, snapshot.capability_revision_id), [])
        if not successors:
            continue
        state = {
            "status": "superseded",
            "successor_revisions": [item[0] for item in sorted(successors)],
            "compatibility": [item[1] for item in sorted(successors)],
        }
        candidate["snapshot"] = _with_projection_state(
            snapshot,
            maturity="retired",
            confidence=min(snapshot.confidence, 0.1),
            dependency_state=state,
            reason_codes=tuple(sorted({*snapshot.reason_codes, "revision_superseded_by_active_revision"})),
            evidence_refs=snapshot.evidence_refs,
        )


def _apply_capability_supersession_gates(
    candidates: Sequence[dict[str, Any]],
    *,
    relations: Sequence[Mapping[str, Any]],
) -> None:
    """Retire an explicitly superseded semantic capability without inheritance.

    ``CapabilityRelation(source=A, target=B, relation_type='supersedes')``
    means A replaces B.  The relation carries no evidence transfer: B is
    retired even if A is not itself selected by this profile, preventing a
    historical B snapshot from continuing to satisfy L5 while its successor
    has not yet been independently evidenced.
    """

    successors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in relations:
        if str(relation.get("relation_type") or "") != "supersedes":
            continue
        source = str(relation.get("source_capability_id") or "")
        target = str(relation.get("target_capability_id") or "")
        relation_id = str(relation.get("relation_id") or "")
        if source and target:
            successors[target].append(
                {
                    "capability_id": source,
                    "relation_id": relation_id,
                    "evidence_refs": _relation_evidence_refs(relation),
                }
            )
    for candidate in candidates:
        snapshot = candidate.get("snapshot")
        if snapshot is None:
            continue
        replacement = successors.get(snapshot.capability_id, [])
        if not replacement:
            continue
        successors_state = sorted(replacement, key=lambda item: (item["capability_id"], item["relation_id"]))
        relation_refs = {
            str(ref)
            for item in successors_state
            for ref in (item.get("relation_id"), *(item.get("evidence_refs") or ()))
            if str(ref or "")
        }
        candidate["snapshot"] = _with_projection_state(
            snapshot,
            maturity="retired",
            confidence=min(snapshot.confidence, 0.1),
            dependency_state={
                "status": "superseded",
                "successor_capabilities": [item["capability_id"] for item in successors_state],
                "superseding_relation_ids": [item["relation_id"] for item in successors_state],
            },
            reason_codes=tuple(sorted({*snapshot.reason_codes, "capability_superseded_by_active_relation"})),
            evidence_refs=tuple(sorted({*snapshot.evidence_refs, *relation_refs})),
        )


def _best_capability_state(candidates: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    best: dict[str, str] = {}
    for candidate in candidates:
        snapshot = candidate.get("snapshot")
        if snapshot is None:
            continue
        current = best.get(snapshot.capability_id, "unknown")
        if _MATURITY_RANK.get(snapshot.maturity, -99) > _MATURITY_RANK.get(current, -99):
            best[snapshot.capability_id] = snapshot.maturity
    return best


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[str, str, str]:
    snapshot = candidate.get("snapshot")
    if snapshot is None:
        return "", "", ""
    return snapshot.capability_id, snapshot.capability_revision_id, str(candidate.get("provider_binding_id") or "")


def _relation_evidence_refs(relation: Mapping[str, Any]) -> tuple[str, ...]:
    payload = relation.get("payload") if isinstance(relation.get("payload"), Mapping) else relation
    refs = payload.get("evidence_refs") if isinstance(payload, Mapping) else ()
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        return ()
    return tuple(sorted({str(item) for item in refs if str(item or "")}))


def _with_projection_state(
    snapshot: CapabilityStateSnapshot,
    *,
    dependency_state: Mapping[str, Any],
    reason_codes: Sequence[object],
    evidence_refs: Sequence[object],
    maturity: str | None = None,
    confidence: float | None = None,
) -> CapabilityStateSnapshot:
    # Snapshots deliberately freeze nested JSON with MappingProxyType.  A
    # relationship update can feed that immutable state back through this
    # function, so make a canonical mutable JSON copy before deriving a new
    # digest.  A shallow dict() leaves nested mapping proxies in place and
    # makes projection crash only when a dependency/composition state changes.
    normalized_dependency_state = _normalized_projection_dependency_state(dependency_state)
    material = {
        "base_snapshot": snapshot.snapshot_digest,
        "maturity": maturity or snapshot.maturity,
        "confidence": snapshot.confidence if confidence is None else confidence,
        "dependency_state": normalized_dependency_state,
        "reason_codes": list(reason_codes),
        "evidence_refs": list(evidence_refs),
    }
    snapshot_id = f"capability-snapshot-{_digest(material)[:40]}"
    input_digests = {
        **dict(snapshot.input_digests),
        "relationship_state": _digest(normalized_dependency_state),
    }
    return CapabilityStateSnapshot(
        snapshot_id=snapshot_id,
        capability_id=snapshot.capability_id,
        capability_revision_id=snapshot.capability_revision_id,
        profile_id=snapshot.profile_id,
        maturity=maturity or snapshot.maturity,
        confidence=snapshot.confidence if confidence is None else confidence,
        evidence_refs=evidence_refs,
        sample_sufficiency=snapshot.sample_sufficiency,
        reliability_metrics=snapshot.reliability_metrics,
        latest_success_ref=snapshot.latest_success_ref,
        latest_failure_ref=snapshot.latest_failure_ref,
        regression_streak=snapshot.regression_streak,
        dependency_state=normalized_dependency_state,
        knowledge_applicability=snapshot.knowledge_applicability,
        provider_applicability=snapshot.provider_applicability,
        environment_applicability=snapshot.environment_applicability,
        input_watermark=snapshot.input_watermark,
        algorithm_revision=snapshot.algorithm_revision,
        computed_at=snapshot.computed_at,
        scope=snapshot.scope,
        reason_codes=reason_codes,
        input_digests=input_digests,
    )


def _projection_state_matches(
    snapshot: CapabilityStateSnapshot,
    *,
    maturity: str,
    confidence: float,
    dependency_state: Mapping[str, Any],
    reason_codes: Sequence[object],
    evidence_refs: Sequence[object],
) -> bool:
    return (
        snapshot.maturity == maturity
        and snapshot.confidence == confidence
        and _normalized_projection_dependency_state(snapshot.dependency_state)
        == _normalized_projection_dependency_state(dependency_state)
        and tuple(snapshot.reason_codes) == tuple(reason_codes)
        and tuple(snapshot.evidence_refs) == tuple(evidence_refs)
    )


def _normalized_projection_dependency_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the one JSON representation used for both compare and digest.

    CapabilityStateSnapshot freezes nested structures into mapping proxies and
    tuples.  Relationship evaluation naturally produces dicts and lists.  A
    shallow comparison sees those as different forever, falsely declaring a
    stable DAG an unstable cycle.  Canonicalizing both sides preserves the
    immutable-model boundary without weakening malformed-data handling.
    """

    try:
        return normalize_json_payload(
            value,
            field="projection.dependency_state",
            reject_executable=True,
        )
    except CapabilityContractError as exc:
        raise CapabilityProjectionError("projection dependency state is invalid") from exc


def _index_observations(rows: list[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    index: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        capability_id = str(payload.get("capability_id") or "")
        revision_id = str(payload.get("capability_revision_id") or "")
        binding_id = str(payload.get("provider_binding_id") or "")
        if capability_id and revision_id and binding_id:
            index[(capability_id, revision_id, binding_id)].append(row)
    return index


def _index_knowledge_links(rows: list[Mapping[str, Any]]) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    index: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        capability_id = str(payload.get("capability_id") or "")
        revision_id = str(payload.get("capability_revision_id") or "")
        if capability_id and revision_id:
            index[(capability_id, revision_id)].append(row)
    return index


def _knowledge_links_as_of(
    rows: Sequence[Mapping[str, Any]],
    *,
    at_time: str,
) -> list[Mapping[str, Any]]:
    """Keep only link facts that existed at a historical projection point.

    Knowledge-link storage has no temporal filter today, unlike observation
    storage.  Filtering at this narrow boundary prevents a later refutation or
    support record from being read into a historical replay.  Contract-backed
    links always carry ``created_at``; a malformed imported row is retained so
    the downstream applicability/contract path can fail closed rather than
    making it disappear from the input surface.
    """

    result: list[Mapping[str, Any]] = []
    reference = _parse_projection_time(at_time)
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        created_at = str(payload.get("created_at") or "")
        if not created_at:
            result.append(row)
            continue
        try:
            if _parse_projection_time(created_at) <= reference:
                result.append(row)
        except ValueError:
            # Preserve malformed immutable input for a deterministic blocked
            # projection instead of pretending it never existed.
            result.append(row)
    return result


def _parse_projection_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("projection timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _capability_scope(value: object) -> str:
    try:
        return normalize_opaque_id(value, field="capability_scope")
    except CapabilityContractError as exc:
        raise CapabilityProjectionError(str(exc)) from exc


def _normalize_affected_capability_ids(value: Sequence[object]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityProjectionError("affected_capability_ids must be a sequence")
    normalized: set[str] = set()
    for raw in value:
        try:
            normalized.add(normalize_capability_id(raw, field="affected capability_id"))
        except CapabilityContractError as exc:
            raise CapabilityProjectionError(str(exc)) from exc
    return tuple(sorted(normalized))


def _resolution_capability_ids(resolution: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for entry in resolution.get("requirements") or ():
        if not isinstance(entry, Mapping):
            continue
        capability_id = str(entry.get("capability_id") or "")
        if capability_id:
            result.add(capability_id)
    return result


def _resolution_binding_pairs(
    resolution: Mapping[str, Any],
    *,
    target_ids: set[str],
) -> set[tuple[str, str, str]]:
    pairs: set[tuple[str, str, str]] = set()
    for entry in resolution.get("requirements") or ():
        if not isinstance(entry, Mapping):
            continue
        capability_id = str(entry.get("capability_id") or "")
        if capability_id not in target_ids:
            continue
        bindings = entry.get("bindings") or ()
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            descriptor = binding.get("descriptor") if isinstance(binding.get("descriptor"), Mapping) else {}
            revision_id = str(descriptor.get("capability_revision_id") or "")
            binding_id = str(binding.get("entity_id") or "")
            if revision_id and binding_id:
                pairs.add((capability_id, revision_id, binding_id))
    return pairs


def _expand_affected_capabilities(
    affected: Sequence[str],
    relations: Sequence[Mapping[str, Any]],
    selected_capabilities: set[str],
) -> set[str]:
    """Return both changed components and all selected relationship neighbors."""

    result = {item for item in affected if item in selected_capabilities}
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    for relation in relations:
        relation_type = str(relation.get("relation_type") or "")
        if relation_type not in {"depends_on", "composes", "conflicts_with", "supersedes"}:
            continue
        source = str(relation.get("source_capability_id") or "")
        target = str(relation.get("target_capability_id") or "")
        if source and target:
            forward[source].add(target)
            reverse[target].add(source)
    # Revision-local dependencies are not available at this layer.  The
    # profile expansion still includes their source; missing targets then fail
    # closed in relationship gating.  Registered relation traversal is bounded
    # by the selected profile candidate set.
    queue = list(sorted(result))
    while queue:
        current = queue.pop(0)
        for neighbor in sorted(forward.get(current, set()) | reverse.get(current, set())):
            if neighbor in selected_capabilities and neighbor not in result:
                result.add(neighbor)
                queue.append(neighbor)
    return result


def _expand_projection_targets(
    resolution: Mapping[str, Any],
    *,
    target_ids: set[str],
    relations: Sequence[Mapping[str, Any]],
    selected_capabilities: set[str],
) -> set[str]:
    """Close an incremental set over declared revision dependencies as well."""

    result = set(target_ids)
    while True:
        before = set(result)
        for entry in resolution.get("requirements") or ():
            if not isinstance(entry, Mapping):
                continue
            capability_id = str(entry.get("capability_id") or "")
            if capability_id not in result:
                continue
            requirement = entry.get("requirement") if isinstance(entry.get("requirement"), Mapping) else {}
            if requirement.get("require_dependencies") is not True:
                continue
            for revision in entry.get("revisions") or ():
                descriptor = revision.get("descriptor") if isinstance(revision, Mapping) and isinstance(revision.get("descriptor"), Mapping) else {}
                contract = descriptor.get("contract") if isinstance(descriptor.get("contract"), Mapping) else {}
                for dependency in contract.get("dependencies") or ():
                    dependency_id = str(dependency or "")
                    if dependency_id in selected_capabilities:
                        result.add(dependency_id)
        result = _expand_affected_capabilities(tuple(sorted(result)), relations, selected_capabilities)
        if result == before:
            return result


def _relation_view(entity: Any) -> dict[str, Any]:
    payload = entity.payload if isinstance(getattr(entity, "payload", None), Mapping) else {}
    return {
        "relation_id": str(payload.get("relation_id") or getattr(entity, "entity_id", "") or ""),
        "relation_digest": str(payload.get("relation_digest") or getattr(entity, "entity_digest", "") or ""),
        "relation_type": str(payload.get("relation_type") or ""),
        "source_capability_id": str(payload.get("source_capability_id") or ""),
        "target_capability_id": str(payload.get("target_capability_id") or ""),
        "relation_policy": dict(payload.get("relation_policy") or {}) if isinstance(payload.get("relation_policy"), Mapping) else {},
        "evidence_refs": list(payload.get("evidence_refs") or ()) if isinstance(payload.get("evidence_refs"), Sequence) and not isinstance(payload.get("evidence_refs"), (str, bytes)) else [],
        "state_digest": str(getattr(entity, "state_digest", "") or ""),
        "effective_at": str(getattr(entity, "effective_at", "") or ""),
    }


def _inactive_binding_reason(
    binding_contexts: Mapping[str, Mapping[str, Any]],
    *,
    capability_id: str,
    capability_revision_id: str,
) -> str:
    statuses: set[str] = set()
    for context in binding_contexts.values():
        descriptor = context.get("descriptor") if isinstance(context.get("descriptor"), Mapping) else {}
        if (
            str(descriptor.get("capability_id") or "") == capability_id
            and str(descriptor.get("capability_revision_id") or "") == capability_revision_id
        ):
            statuses.add(str(context.get("status") or ""))
    if "quarantined" in statuses:
        return "provider_binding_quarantined"
    if "stale" in statuses:
        return "provider_binding_stale"
    if statuses.intersection({"disabled", "deprecated", "retired"}):
        return "provider_binding_not_active"
    return "no_active_provider_binding"


def _portable_observation_index(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_pairs: set[tuple[str, str, str]],
    binding_contexts: Mapping[str, Mapping[str, Any]],
    excluded_revisions: set[tuple[str, str]] | None = None,
) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    """Select only explicitly portable evidence for a replacement binding.

    This does not conflate deployment-dependent evidence with
    environment-dependent evidence.  A portable observation may still carry a
    commit/receipt/session authority; release validation remains an independent
    deployment-assurance concern.  The projector only verifies the facts it
    owns: same semantic revision, provider kind, implementation digest, and no
    explicit environment dependency or malformed authority declaration.
    """

    excluded = excluded_revisions or set()
    result: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    target_by_revision: dict[tuple[str, str], list[str]] = defaultdict(list)
    for capability_id, revision_id, binding_id in target_pairs:
        target_by_revision[(capability_id, revision_id)].append(binding_id)
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        capability_id = str(payload.get("capability_id") or "")
        revision_id = str(payload.get("capability_revision_id") or "")
        source_binding_id = str(payload.get("provider_binding_id") or "")
        revision_key = (capability_id, revision_id)
        if revision_key in excluded or not source_binding_id:
            continue
        source = binding_contexts.get(source_binding_id)
        if not isinstance(source, Mapping):
            continue
        for target_binding_id in sorted(target_by_revision.get(revision_key, ())):
            if target_binding_id == source_binding_id:
                continue
            target = binding_contexts.get(target_binding_id)
            if not isinstance(target, Mapping):
                continue
            if not _observation_is_portable_to_binding(payload, source=source, target=target):
                continue
            tagged = dict(row)
            tagged["projection_portability"] = {
                "source_binding_id": source_binding_id,
                "target_binding_id": target_binding_id,
                "provider_kind": str((target.get("descriptor") or {}).get("provider_kind") or ""),
                "implementation_digest": str((target.get("descriptor") or {}).get("implementation_digest") or ""),
                "source_binding_digest": str((source.get("descriptor") or {}).get("binding_digest") or ""),
                "source_lifecycle_state_digest": str(source.get("state_digest") or ""),
                "target_lifecycle_state_digest": str(target.get("state_digest") or ""),
            }
            result[(capability_id, revision_id, target_binding_id)].append(tagged)
    for key in result:
        result[key].sort(key=_observation_key)
    return result


def _observation_is_portable_to_binding(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
) -> bool:
    source_descriptor = source.get("descriptor") if isinstance(source.get("descriptor"), Mapping) else {}
    target_descriptor = target.get("descriptor") if isinstance(target.get("descriptor"), Mapping) else {}
    capability_id = str(payload.get("capability_id") or "")
    revision_id = str(payload.get("capability_revision_id") or "")
    source_binding_id = str(payload.get("provider_binding_id") or "")
    if not capability_id or not revision_id or not source_binding_id:
        return False
    # The row is immutable but projection remains defensive against imported
    # or corrupted storage: portability is never an escape hatch across a
    # semantic capability, a revision, or a claimed binding identity.
    if (
        str(source.get("binding_id") or "") != source_binding_id
        or str(source_descriptor.get("binding_id") or "") != source_binding_id
        or str(source_descriptor.get("capability_id") or "") != capability_id
        or str(source_descriptor.get("capability_revision_id") or "") != revision_id
        or str(target_descriptor.get("capability_id") or "") != capability_id
        or str(target_descriptor.get("capability_revision_id") or "") != revision_id
        or str(target_descriptor.get("binding_id") or "") != str(target.get("binding_id") or "")
    ):
        return False
    if str(source_descriptor.get("provider_kind") or "") != str(target_descriptor.get("provider_kind") or ""):
        return False
    if str(source_descriptor.get("implementation_digest") or "") != str(target_descriptor.get("implementation_digest") or ""):
        return False
    if not str(source_descriptor.get("implementation_digest") or ""):
        return False
    if _declares_environment_dependency(payload):
        return False
    if _declares_environment_dependency(source_descriptor) or _declares_environment_dependency(target_descriptor):
        return False
    if _declares_nonportable(payload):
        return False
    authority = payload.get("deployment_authority")
    deployment_dependent = _declares_deployment_dependency(payload)
    if deployment_dependent and not authority:
        return False
    if authority and not _portable_authority_is_well_formed(
        authority,
        provider_kind=str(target_descriptor.get("provider_kind") or ""),
        implementation_digest=str(target_descriptor.get("implementation_digest") or ""),
        deployment_dependent=deployment_dependent,
    ):
        return False
    return True


def _declares_environment_dependency(value: Mapping[str, Any]) -> bool:
    candidates: list[Mapping[str, Any]] = [value]
    for key in ("provenance", "applicability", "deployment_authority"):
        nested = value.get(key) if isinstance(value, Mapping) else None
        if isinstance(nested, Mapping):
            candidates.append(nested)
    return any(item.get("environment_dependent") is True for item in candidates)


def _declares_nonportable(value: Mapping[str, Any]) -> bool:
    provenance = value.get("provenance") if isinstance(value.get("provenance"), Mapping) else {}
    portability = provenance.get("portability") if isinstance(provenance.get("portability"), Mapping) else {}
    return value.get("portable") is False or provenance.get("portable") is False or portability.get("portable") is False


def _declares_deployment_dependency(value: Mapping[str, Any]) -> bool:
    provenance = value.get("provenance") if isinstance(value.get("provenance"), Mapping) else {}
    authority = value.get("deployment_authority") if isinstance(value.get("deployment_authority"), Mapping) else {}
    return (
        value.get("deployment_dependent") is True
        or provenance.get("deployment_dependent") is True
        or authority.get("deployment_dependent") is True
    )


def _portable_authority_is_well_formed(
    authority: object,
    *,
    provider_kind: str,
    implementation_digest: str,
    deployment_dependent: bool,
) -> bool:
    if not isinstance(authority, Mapping):
        return False
    if deployment_dependent:
        # A deployment-bound record must name all three authorities; this is a
        # local structural check only.  Whether that authority is current is
        # intentionally decided by the independent deployment axis.
        aliases = (
            ("commit", "release_commit", "commit_sha"),
            ("receipt", "release_receipt", "receipt_id"),
            ("session", "release_session", "session_id"),
        )
        if any(not any(str(authority.get(key) or "") for key in group) for group in aliases):
            return False
    authority_provider = str(authority.get("provider_kind") or "")
    if authority_provider and authority_provider != provider_kind:
        return False
    authority_implementation = str(authority.get("implementation_digest") or "")
    if authority_implementation and authority_implementation != implementation_digest:
        return False
    return True


def _merge_observation_rows(
    direct_rows: Sequence[Mapping[str, Any]],
    portable_rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in (*direct_rows, *portable_rows):
        observation_id = str(row.get("observation_id") or "")
        if observation_id:
            result.setdefault(observation_id, row)
    return sorted(result.values(), key=_observation_key)


def _cap_maturity(maturity: str, ceiling: str) -> str:
    if maturity not in _MATURITY_RANK or ceiling not in _MATURITY_RANK:
        return "observed"
    # Negative terminal states are never upgraded by a qualifier.
    if _MATURITY_RANK[maturity] < 0:
        return maturity
    return maturity if _MATURITY_RANK[maturity] <= _MATURITY_RANK[ceiling] else ceiling


def _controlled_retry_metrics(payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Expose retry reliability only when observations declare retry context."""

    retry_rows: list[Mapping[str, Any]] = []
    for payload in payloads:
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
        attempt = metrics.get("attempt", provenance.get("attempt", metrics.get("retry_index", provenance.get("retry_index"))))
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            continue
        retry_rows.append(payload)
    if not retry_rows:
        return {}
    decisive = [item for item in retry_rows if str(item.get("verdict") or "") in {"pass", "fail"}]
    if not decisive:
        return {"controlled_retry_sample_count": len(retry_rows)}
    passes = sum(1 for item in decisive if str(item.get("verdict") or "") == "pass")
    return {
        "controlled_retry_sample_count": len(retry_rows),
        "controlled_retry_pass_rate": round(passes / len(decisive), 6),
    }


def _consecutive_passes(payloads: list[Mapping[str, Any]]) -> int:
    count = 0
    for payload in reversed(payloads):
        if str(payload.get("verdict") or "") != "pass":
            break
        count += 1
    return count


def _regression_streak(payloads: list[Mapping[str, Any]]) -> int:
    count = 0
    for payload in reversed(payloads):
        if str(payload.get("verdict") or "") != "fail":
            break
        count += 1
    return count


def _confidence(*, observation_count: int, decisive_count: int, pass_rate: float, maturity: str) -> float:
    if maturity == "regressed":
        return round(min(1.0, 0.5 + 0.05 * observation_count), 6)
    if not decisive_count:
        return round(min(0.25, 0.05 * observation_count), 6)
    coverage = min(1.0, decisive_count / max(3, observation_count))
    return round(max(0.0, min(1.0, 0.2 + 0.5 * pass_rate + 0.3 * coverage)), 6)


def _observation_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("observed_at") or ""), str(row.get("observation_id") or "")


def _observation_ref(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    value = row.get("observation_id") or row.get("id") or ""
    return str(value)


def _projection_watermark(
    resolution: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    knowledge_links: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    runtime_scope: ScopeRef,
    capability_scope: str,
    at_time: str,
    binding_contexts: Mapping[str, Mapping[str, Any]],
    binding_context_truncated_capability_ids: set[str],
) -> str:
    """Hash every material exact-scope projection input, not only outcomes."""

    return _digest(
        {
            "resolution_digest": resolution.get("resolution_digest"),
            "registry_watermark": resolution.get("registry_watermark"),
            "lifecycle_watermark": resolution.get("lifecycle_watermark"),
            "runtime_scope": {
                "tenant_id": runtime_scope.tenant_id,
                "agent_id": runtime_scope.agent_id,
                "workspace_id": runtime_scope.workspace_id,
                "user_id": runtime_scope.user_id,
            },
            "capability_scope": capability_scope,
            "at_time": at_time,
            "binding_context": [
                {
                    "binding_id": binding_id,
                    "binding_digest": str(
                        (
                            context.get("descriptor", {}).get("binding_digest")
                            if isinstance(context.get("descriptor"), Mapping)
                            else ""
                        )
                        or ""
                    ),
                    "status": str(context.get("status") or ""),
                    "state_digest": str(context.get("state_digest") or ""),
                }
                for binding_id, context in sorted(binding_contexts.items())
            ],
            "binding_context_truncated_capability_ids": sorted(binding_context_truncated_capability_ids),
            "observations": [
                {
                    "id": row.get("observation_id"),
                    "digest": row.get("observation_digest"),
                    "at": row.get("observed_at"),
                }
                for row in sorted(observations, key=_observation_key)
            ],
            "knowledge_links": [
                {
                    "id": row.get("link_id"),
                    "digest": row.get("link_digest"),
                    "knowledge_record_digest": row.get("knowledge_record_digest"),
                }
                for row in sorted(knowledge_links, key=lambda row: str(row.get("link_id") or ""))
            ],
            "relations": [
                {
                    "id": row.get("relation_id"),
                    "digest": row.get("relation_digest"),
                    "state_digest": row.get("state_digest"),
                    "effective_at": row.get("effective_at"),
                }
                for row in sorted(relations, key=lambda row: str(row.get("relation_id") or ""))
            ],
        }
    )


def _digest(value: Any) -> str:
    """Hash a stable, JSON-shaped view of immutable capability descriptors.

    Capability models recursively freeze mappings as ``MappingProxyType`` to
    prevent a caller from mutating an entity after its digest is computed.
    Relationship projection carries those frozen mappings into dependency
    state, so hashing the raw value makes a valid graph crash at runtime.  The
    digest boundary intentionally thaws only JSON containers; unsupported
    values still fail through ``json.dumps`` rather than being stringified.
    """

    encoded = json.dumps(
        _digest_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _digest_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _digest_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digest_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_digest_json_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    return value


def _blocked_sort_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("capability_id") or ""),
        str(value.get("capability_revision_id") or ""),
        str(value.get("provider_binding_id") or ""),
    )


__all__ = [
    "CapabilityProjectionError",
    "CapabilityProjectionResult",
    "CapabilityStateProjector",
    "DEFAULT_ALGORITHM_REVISION",
    "PROJECTOR_SCHEMA",
]
