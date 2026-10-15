from types import SimpleNamespace
from unittest import mock

from eimemory.governance.release import deployment_receipt as dr

P = "eimemory.governance.release.evidence_contract.verified_deployment_receipt_identity"


def _rec():
    return SimpleNamespace(record_id="rec_new", content={}, scope=SimpleNamespace())


def test_response_uses_persisted_release_session():
    with mock.patch(P, return_value=SimpleNamespace(session_id="rec_release")):
        assert dr._receipt_session_id(_rec()) == "rec_release"


def test_response_falls_back_to_record_id():
    with mock.patch(P, return_value=None):
        assert dr._receipt_session_id(_rec()) == "rec_new"
