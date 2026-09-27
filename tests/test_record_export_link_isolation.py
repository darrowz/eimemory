"""Exporter unit tests use the envelope's consumed interface, not scoring stubs."""
from types import SimpleNamespace
import os
import pytest
from eimemory.storage.record_export import export_record_markdown, _scope_partition, exported_records_dir


def record(*, user="one", record_id="memory-one", rejected=False):
    return SimpleNamespace(
        scope=SimpleNamespace(tenant_id="t", agent_id="a", workspace_id="w", user_id=user),
        record_id=record_id, kind="memory", status="rejected" if rejected else "active",
        meta={}, title="Memory", source="test", summary="content", detail="", content={}, tags=[],
        links=[], evidence=[], time=SimpleNamespace(created_at="2026-09-27T00:00:00Z"),
    )


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation not permitted on this runner")


@pytest.mark.parametrize("rejected", [False, True])
def test_leaf_alias_cannot_overwrite_or_delete_another_record(tmp_path, rejected):
    victim = record(record_id="victim")
    victim_path = export_record_markdown(tmp_path, victim)
    before = victim_path.read_bytes()
    attacker = record(record_id="alias", rejected=rejected)
    alias = victim_path.with_name("alias.md")
    symlink(alias, victim_path)
    with pytest.raises(ValueError):
        export_record_markdown(tmp_path, attacker)
    assert victim_path.read_bytes() == before and alias.is_symlink()


@pytest.mark.parametrize("rejected", [False, True])
def test_scope_directory_alias_is_rejected(tmp_path, rejected):
    victim = record(user="victim")
    victim_path = export_record_markdown(tmp_path, victim)
    before = victim_path.read_bytes()
    other = record(user="other", rejected=rejected)
    partition = exported_records_dir(tmp_path) / _scope_partition(other.scope)
    symlink(partition, victim_path.parent, directory=True)
    with pytest.raises(ValueError):
        export_record_markdown(tmp_path, other)
    assert victim_path.read_bytes() == before


@pytest.mark.parametrize("level", ["qmd", "records"])
def test_derived_parent_links_are_rejected(tmp_path, level):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "runtime"
    root.mkdir()
    if level == "qmd":
        symlink(root / "qmd", outside, directory=True)
    else:
        (root / "qmd").mkdir()
        symlink(root / "qmd" / "records", outside, directory=True)
    with pytest.raises(ValueError):
        export_record_markdown(root, record())
    assert not list(outside.iterdir())


def test_regular_record_update_and_reject_still_work(tmp_path):
    item = record()
    path = export_record_markdown(tmp_path, item)
    assert path.is_file()
    item.summary = "updated"
    assert export_record_markdown(tmp_path, item) == path
    assert "updated" in path.read_text()
    item.status = "rejected"
    assert export_record_markdown(tmp_path, item) is None
    assert not path.exists()


def test_different_scopes_remain_separate(tmp_path):
    one, two = export_record_markdown(tmp_path, record()), export_record_markdown(tmp_path, record(user="two"))
    assert one != two and one.is_file() and two.is_file()
