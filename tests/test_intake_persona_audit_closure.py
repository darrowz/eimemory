"""Real persistence and interruption contracts from the 2026-10-13 audit."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import _cmd_intake
from eimemory.persona.cli import handle_persona_command
from eimemory.persona.correction import correction_from_user_text
from eimemory.persona.evolver import evolve_persona
from eimemory.persona.feedback_safety import PersonaCorrectionRejected
from eimemory.persona.store import PersonaStore


SCOPE = {"agent_id": "audit", "workspace_id": "local", "user_id": "owner"}


def add_source(runtime, index=0):
    path = runtime.store.root / f"source-{index}.txt"
    path.write_text(f"Source {index}: Independent evidence and durable feedback establish repeatable knowledge acceptance.")
    runtime.sources.add_source({"source_id": f"local-{index}", "source_kind": "manual",
                                "uri": str(path), "title": f"Source {index}"})


def test_intake_cli_preview_does_not_consume_later_capture(tmp_path, capsys):
    with Runtime.create(root=tmp_path) as runtime:
        add_source(runtime)
        before = runtime.sources.path.read_bytes()
        assert _cmd_intake(SimpleNamespace(intake_command="report", limit=None, source_kind=""), runtime, SCOPE) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["candidate_count"] == 1 and report["written_count"] == 0
        assert runtime.sources.path.read_bytes() == before
    with Runtime.create(root=tmp_path) as runtime:
        report = runtime.run_knowledge_intake(persist=True, scope=SCOPE)
        assert report["scanned_count"] == report["written_count"] == 1
        assert runtime.sources.list_sources()[0].last_scanned_at
        assert runtime.run_knowledge_intake(persist=True, scope=SCOPE)["skipped_not_due_count"] == 1


def test_failed_intake_keeps_source_due_and_retry_deduplicates_committed_prefix(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        add_source(runtime, 0)
        add_source(runtime, 1)
        append = runtime.store.append
        calls = []

        def interrupt(record, **kwargs):
            calls.append(record.record_id)
            if len(calls) == 2:
                raise OSError("synthetic write interruption")
            return append(record, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "append", interrupt)
            with pytest.raises(OSError):
                runtime.run_knowledge_intake(persist=True, scope=SCOPE)
        assert all(not source.last_scanned_at for source in runtime.sources.list_sources())
    with Runtime.create(root=tmp_path) as runtime:
        report = runtime.run_knowledge_intake(persist=True, scope=SCOPE)
        assert report["written_count"] == report["skipped_existing_count"] == 1
        assert runtime.store.count_records(kinds=["knowledge_candidate"], scope=SCOPE) == 2


def record_correction(store, key="first"):
    correction = correction_from_user_text("too verbose")
    store.record_correction(correction, scope=SCOPE, idempotency_key=key)
    return correction


def evolve(store, corrections):
    return evolve_persona(store.load_state(), corrections, store=store, scope=SCOPE, dry_run=False)


def evolution_records(runtime):
    return [record for record in runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100)
            if record.source == "persona.evolution"]


def test_persona_cli_does_not_reconsume_after_restart_and_accepts_distinct_identical_event(tmp_path, capsys):
    values = []
    for index in range(4):
        with Runtime.create(root=tmp_path) as runtime:
            store = PersonaStore(runtime.store)
            if index == 0:
                record_correction(store)
            if index == 3:
                record_correction(store, "second")
            assert handle_persona_command(SimpleNamespace(persona_command="evolve", dry_run=False), runtime, SCOPE) == 0
            capsys.readouterr()
            values.append(store.load_state().traits.verbosity)
            assert len(evolution_records(runtime)) == (2 if index == 3 else 1)
    assert values == [0.1, 0.1, 0.1, 0.0]


def test_persona_dry_run_does_not_consume_correction(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        store = PersonaStore(runtime.store)
        correction = record_correction(store)
        result = evolve_persona(store.load_state(), [correction], store=store, scope=SCOPE, dry_run=True)
        assert result.state.traits.verbosity == 0.1
        assert store.load_state().traits.verbosity == 0.25
        assert store.load_state().applied_correction_ids == []
        assert not store.evolution_pending_path.exists()
        assert not evolution_records(runtime)
        assert evolve(store, [correction]).state.traits.verbosity == 0.1


@pytest.mark.parametrize("stage", ["before_state", "after_state", "after_receipt"])
def test_persona_evolution_recovers_each_commit_boundary_once(tmp_path, monkeypatch, stage):
    with Runtime.create(root=tmp_path) as runtime:
        store = PersonaStore(runtime.store)
        correction = record_correction(store)
        with monkeypatch.context() as patch:
            if stage == "before_state":
                patch.setattr(store, "_save_state", lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted")))
            else:
                real = store.record_evolution

                def interrupt(*args, **kwargs):
                    if stage == "after_receipt":
                        real(*args, **kwargs)
                    raise OSError("interrupted")

                patch.setattr(store, "record_evolution", interrupt)
            with pytest.raises(OSError):
                evolve(store, [correction])
        assert store.evolution_pending_path.exists()
    with Runtime.create(root=tmp_path) as runtime:
        store = PersonaStore(runtime.store)
        evolve(store, store.list_corrections(scope=SCOPE))
        assert store.load_state().traits.verbosity == 0.1
        assert store.load_state().applied_correction_ids == [correction.record_id]
        assert len(evolution_records(runtime)) == 1
        snapshots = [record for record in runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100)
                     if record.source == "persona.state_snapshot"]
        assert len(snapshots) == 1
        assert not store.evolution_pending_path.exists()


def test_concurrent_persona_evolutions_reread_authority_inside_lock(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        correction = record_correction(PersonaStore(runtime.store))
    barrier = Barrier(2)

    def run():
        with Runtime.create(root=tmp_path) as runtime:
            store = PersonaStore(runtime.store)
            stale = store.load_state()
            barrier.wait(timeout=10)
            return evolve_persona(stale, [correction], store=store, scope=SCOPE, dry_run=False).applied_categories

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        second = pool.submit(run)
        categories = [first.result(timeout=20), second.result(timeout=20)]
    assert sorted(categories) == [[], ["verbosity"]]
    with Runtime.create(root=tmp_path) as runtime:
        assert PersonaStore(runtime.store).load_state().traits.verbosity == 0.1
        assert len(evolution_records(runtime)) == 1


def test_persona_persistent_evolution_uses_stored_content_and_exact_scope(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        store = PersonaStore(runtime.store)
        correction = record_correction(store)
        for forged in (replace(correction, trait_delta={"verbosity": -1.0}), replace(correction, record_id="")):
            with pytest.raises(PersonaCorrectionRejected):
                evolve(store, [forged])
        with pytest.raises(PersonaCorrectionRejected):
            evolve_persona(store.load_state(), [correction], store=store, scope={**SCOPE, "user_id": "other"}, dry_run=False)
        assert store.load_state().traits.verbosity == 0.25
        assert not evolution_records(runtime)


def test_file_only_persona_cannot_discard_pending_native_receipts(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        store = PersonaStore(runtime.store)
        correction = record_correction(store)
        with monkeypatch.context() as patch:
            patch.setattr(store, "record_evolution", lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted")))
            with pytest.raises(OSError):
                evolve(store, [correction])
        before = store.evolution_pending_path.read_bytes()
        file_view = PersonaStore(tmp_path)
        with pytest.raises(PersonaCorrectionRejected, match="record_store_unavailable"):
            file_view.save_state(file_view.load_state())
        assert store.evolution_pending_path.read_bytes() == before
        assert not evolution_records(runtime)
        evolve(store, [correction])
        assert store.load_state().traits.verbosity == 0.1
        assert len(evolution_records(runtime)) == 1


def test_consumed_window_does_not_hide_an_older_unconsumed_event(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        store = PersonaStore(runtime.store)
        older = record_correction(store, "older")
        recent = [record_correction(store, f"latest-{index}") for index in range(4)]
        evolve(store, recent)
        remaining = store.list_corrections(scope=SCOPE, limit=1, max_scan=3,
                                           exclude_record_ids=store.load_state().applied_correction_ids)
        assert [item.record_id for item in remaining] == [older.record_id]
        evolve(store, remaining)
        assert store.list_corrections(scope=SCOPE, limit=1, max_scan=3,
                                      exclude_record_ids=store.load_state().applied_correction_ids) == []
