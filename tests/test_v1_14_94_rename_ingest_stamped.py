from types import SimpleNamespace

import pytest

from eimemory import identity_ops as ops


def _rec(agent):
    return SimpleNamespace(scope=SimpleNamespace(agent_id=agent), meta={"identity_stamped_on_ingest": True},
                           provenance={}, source="eimemory.test")


def test_ingest_stamp_under_legacy_alias_is_eligible(monkeypatch):
    import eimemory.identity as identity

    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("old-agent",))
    monkeypatch.setattr(ops, "needs_hongtu_identity_repair", lambda record: True)
    ops._require_eligible(_rec("old-agent"), None)  # no skip
    with pytest.raises(ops._RepairSkipped) as exc:
        ops._require_eligible(_rec("someone"), None)
    assert exc.value.reason == "ingest_stamped"


def test_no_rename_keeps_ingest_stamp_skip(monkeypatch):
    import eimemory.identity as identity

    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ())
    monkeypatch.setattr(ops, "needs_hongtu_identity_repair", lambda record: True)
    with pytest.raises(ops._RepairSkipped):
        ops._require_eligible(_rec("old-agent"), None)
