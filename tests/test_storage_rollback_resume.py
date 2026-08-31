from __future__ import annotations

import pytest

from deploy.storage_release_transaction import (
    StorageReleaseTransactionError,
    begin_storage_release_transaction,
    classify_storage_release_reconcile,
    guard_allows_start,
    update_storage_release_transaction,
)


POST_RESTORE = ("rollback_storage_restored", "rollback_metadata_ready", "rollback_validating", "rollback_validated")


def _transaction(tmp_path, phase):
    marker = tmp_path / "transaction.json"
    begin_storage_release_transaction(marker, prior_commit="1" * 40, candidate_commit="2" * 40,
                                      current_link=tmp_path / "current", attempt_id="resume-test",
                                      snapshot_dir=tmp_path / "snapshot", active_writer_units=["eimemory-rpc.service"])
    transaction = update_storage_release_transaction(marker, expected_attempt_id="resume-test", phase=phase,
                                                     snapshot_manifest_sha256="a" * 64, storage_destructive=True)
    return marker, transaction


@pytest.mark.parametrize("phase", POST_RESTORE)
def test_durable_restoration_resumes_validation_without_restoring_again(tmp_path, phase):
    _, transaction = _transaction(tmp_path, phase)
    assert classify_storage_release_reconcile(transaction, current_commit="1" * 40, migrations_complete=False) == (
        "finalize_rollback" if phase == "rollback_validated" else "resume_rollback_validation"
    )


@pytest.mark.parametrize("phase", POST_RESTORE)
@pytest.mark.parametrize("current", ["2" * 40, "3" * 40])
def test_post_restore_phase_with_wrong_current_fails_closed(tmp_path, phase, current):
    _, transaction = _transaction(tmp_path, phase)
    with pytest.raises(StorageReleaseTransactionError, match="inconsistent"):
        classify_storage_release_reconcile(transaction, current_commit=current, migrations_complete=True)


@pytest.mark.parametrize("phase", POST_RESTORE)
@pytest.mark.parametrize("target", ["rollback_started", "rollback_link_restored", "candidate_validating", "writers_captured"])
def test_post_restore_phase_cannot_reenter_destructive_restore(tmp_path, phase, target):
    marker, _ = _transaction(tmp_path, phase)
    before = marker.read_bytes()
    with pytest.raises(StorageReleaseTransactionError, match="regress"):
        update_storage_release_transaction(marker, expected_attempt_id="resume-test", phase=target)
    assert marker.read_bytes() == before


@pytest.mark.parametrize("phase,target", [("rollback_validating", "rollback_metadata_ready"),
                                          ("rollback_metadata_ready", "rollback_storage_restored"),
                                          ("rollback_validated", "rollback_validating")])
def test_post_restore_phase_is_monotonic(tmp_path, phase, target):
    marker, _ = _transaction(tmp_path, phase)
    with pytest.raises(StorageReleaseTransactionError, match="regress"):
        update_storage_release_transaction(marker, expected_attempt_id="resume-test", phase=target)


def test_post_restore_updates_are_idempotent_and_allow_validation_progress(tmp_path):
    marker, _ = _transaction(tmp_path, "rollback_storage_restored")
    for phase in POST_RESTORE:
        for _ in range(2):
            result = update_storage_release_transaction(marker, expected_attempt_id="resume-test", phase=phase)
            assert result["phase"] == phase


@pytest.mark.parametrize("phase", ["rollback_started", "rollback_link_restored"])
def test_pre_restore_phase_still_requires_full_restore(tmp_path, phase):
    _, transaction = _transaction(tmp_path, phase)
    assert classify_storage_release_reconcile(transaction, current_commit="1" * 40, migrations_complete=False) == "restore_prior"


@pytest.mark.parametrize("phase", POST_RESTORE)
def test_durable_restore_phase_does_not_authorize_start_without_live_locks(tmp_path, phase, monkeypatch):
    marker, _ = _transaction(tmp_path, phase)
    monkeypatch.setattr("deploy.storage_release_transaction._exclusive_lock_is_held", lambda path: False)
    assert guard_allows_start(marker) is False
