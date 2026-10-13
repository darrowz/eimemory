"""Behavioral coverage for public leaf views without an automatic workflow.

These compatibility/read-only helpers do not grant recall or release authority.
Keep their bounded output and exact identity contracts available to callers.
"""
from dataclasses import replace
import os

import pytest

from eimemory.core.release_source_guard import immutable_release_root
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.caller_assistance import record_contains_display_name, visible_evidence_text
from eimemory.storage.knowledge_review_inventory import get_input_row, inventory_page
from eimemory.storage.runtime_store import RuntimeStore


def test_immutable_release_locator_requires_release_parent_and_full_commit(tmp_path):
    release = tmp_path / "releases" / ("a" * 40)
    package = release / "eimemory" / "__init__.py"
    assert immutable_release_root(str(package)) == release
    assert immutable_release_root(str(tmp_path / "checkout" / ("a" * 40) / "eimemory" / "__init__.py")) is None
    assert immutable_release_root(str(tmp_path / "releases" / ("a" * 8) / "eimemory" / "__init__.py")) is None
    assert immutable_release_root(str(tmp_path / "releases" / ("z" * 40) / "eimemory" / "__init__.py")) is None
    if os.name != "nt":  # Windows developer-mode link rights are host-specific.
        release.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(release, target_is_directory=True)
        assert immutable_release_root(str(current / "eimemory" / "__init__.py")) == release


def test_display_name_view_requires_a_complete_configured_cjk_token(monkeypatch):
    import eimemory.identity as identity

    monkeypatch.setattr(identity, "operator_display_name", lambda: "星舟")
    assert record_contains_display_name("用户（星舟）选择先给结论。") is True
    assert record_contains_display_name("星舟舰队使用另一名称。") is False
    assert record_contains_display_name("记录没有配置的称呼。") is False
    monkeypatch.setattr(identity, "operator_display_name", lambda: "")
    assert record_contains_display_name("空配置不能匹配任何用户。") is False


def test_legacy_visible_evidence_view_is_a_bounded_contiguous_parent_span():
    text = ("Synthetic background. " * 80
            + "Certificate rotation interval is sixty days. "
            + "Synthetic appendix. " * 40)
    visible = visible_evidence_text("certificate rotation interval", text, limit=192)
    assert 0 < len(visible) <= 192
    assert visible in text
    assert "Certificate rotation interval is sixty days." in visible
    assert visible_evidence_text("anything", "") == ""
    with pytest.raises(ValueError, match="invalid_verifier_projection"):
        visible_evidence_text("certificate", text, limit=769)


def test_inventory_single_row_view_is_exact_scope_metadata_only_and_survives_reopen(tmp_path):
    scope = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="space", user_id="owner")
    other = replace(scope, user_id="other")
    store = RuntimeStore(tmp_path / "inventory")
    try:
        record = RecordEnvelope.create(kind="claim_card", title="Synthetic private source",
            summary="Synthetic private body", content={"body": "body must never enter inventory views"},
            source="synthetic.source", source_id="paper", scope=scope)
        store.append(record)
        row = inventory_page(store, scope)["rows"][0]
        key = row["storage_key"]
        assert get_input_row(store, scope, key) == row
        assert get_input_row(store, other, key) is None
        assert get_input_row(store, scope, "missing-storage-key") is None
        assert not {"summary", "title", "content", "body", "meta", "payload_json"}.intersection(row)
    finally:
        store.close()
    reopened = RuntimeStore(tmp_path / "inventory")
    try:
        assert get_input_row(reopened, scope, key) == row
        assert get_input_row(reopened, other, key) is None
    finally:
        reopened.close()
