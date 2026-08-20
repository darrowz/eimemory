"""Dynamic L5 v3 capability contracts and bounded runtime façades.

Importing this package has no registration, storage, or runtime side effects.
The registry and Profile resolver become active only when a caller explicitly
uses the Runtime capability service with an exact runtime scope.
"""

from eimemory.capabilities.contracts import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilityContractError,
    contract_digest,
    normalize_capability_id,
)
from eimemory.capabilities.models import (
    ADAPTER_CAPABILITY_ADVERTISEMENT_SCHEMA_VERSION,
    AdapterCapabilityAdvertisement,
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityKnowledgeLink,
    CapabilityObservation,
    CapabilityProfile,
    CapabilityRelation,
    CapabilityRevision,
    CapabilityStateSnapshot,
    EvaluationRun,
    EvaluationSpec,
    L5AssessmentV3,
)

__all__ = [
    "ADAPTER_CAPABILITY_ADVERTISEMENT_SCHEMA_VERSION",
    "AdapterCapabilityAdvertisement",
    "CAPABILITY_SCHEMA_VERSION",
    "CapabilityBinding",
    "CapabilityContractError",
    "CapabilityDefinition",
    "CapabilityKnowledgeLink",
    "CapabilityObservation",
    "CapabilityProfile",
    "CapabilityRelation",
    "CapabilityRevision",
    "CapabilityStateSnapshot",
    "EvaluationRun",
    "EvaluationSpec",
    "L5AssessmentV3",
    "contract_digest",
    "normalize_capability_id",
]
