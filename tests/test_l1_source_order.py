"""Native source-order and stale-queue regressions; no external model calls."""
from contextlib import closing
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.contracts.l1_source_order import (
    L1SourceOrderBlocked, l1_source_event_binding, l1_source_material_digest,
)
from eimemory.knowledge.l1_pipeline import (
    L1_EXTRACT_VERSION, extract_l1_from_l0_record, persist_l1_atoms,
)
from eimemory.knowledge.sediment import L1Atom
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.jsonl import canonical_payload_json, payload_digest


SCOPE = ScopeRef(tenant_id="synthetic", agent_id="test", workspace_id="isolated", user_id="owner")
OLD = "Orchid hourly supervision setting is enabled."
NEW = "Orchid hourly supervision setting is cancelled."
EARLY = "2026-10-01T01:00:00Z"
LATE = "2026-10-02T01:00:00Z"


@pytest.fixture(autouse=True)
def prohibit_external_provider(monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient
    monkeypatch.setattr(CommandLLMClient, "complete", lambda *a, **k: pytest.fail("External model prohibited"))


def capture(store, text, at, *, source_id="hermes", scope=SCOPE):
    blob = "User: " + text + "\nAssistant: Acknowledged."
    parent = RecordEnvelope.create(kind="memory", title="Synthetic captured turn", summary=blob,
        content={"text": blob, "memory_type": "conversation"}, scope=scope,
        source=source_id + ".turn", source_id=source_id,
        meta={"memory_type": "conversation", "memory_layer": "l0", "capture_origin": "turn_sync",
              "session_id": "synthetic-session", "turn_id": "synthetic-turn"})
    parent.time.occurred_at = at
    return store.append(parent)


def rewrite(store, parent, *, text=None, **fields):
    def mutation(sqlite):
        current = sqlite.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id)
        if text is not None:
            current.summary = "User: " + text + "\nAssistant: Acknowledged."
            current.content = {**current.content, "text": current.summary}
        for key, value in fields.items():
            setattr(current, key, value)
        sqlite.upsert(current, commit=False)
        return current, [current], []
    return store.mutate_records_atomically(mutation)


class NativeFakeLLM:
    def __init__(self, text, *, action="update", mutate=None, empty=False):
        self.text, self.action, self.mutate, self.empty = text, action, mutate, empty
        self.extract_prompts, self.conflict_prompts = [], []

    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        if prompt.startswith("{"):
            body = json.loads(prompt)
            self.conflict_prompts.append(body)
            result = [{"record_id": "new-0", "action": self.action,
                "target_ids": body["new_memories"][0]["related_ids"] if self.action == "update" else [],
                "merged_content": self.text, "merged_type": body["new_memories"][0]["type"],
                # A model-supplied chronology must be ignored.
                "occurred_at": "2100-01-01T00:00:00Z"}]
        else:
            self.extract_prompts.append(prompt)
            result = [] if self.empty else [{"content": self.text, "type": "fact", "priority": 90}]
        if self.mutate is not None:
            self.mutate()
            self.mutate = None
        return SimpleNamespace(text=json.dumps(result))


def extract(api, parent, text, **kwargs):
    client = kwargs.pop("llm", NativeFakeLLM(text))
    return extract_l1_from_l0_record(api, parent, llm=client, use_llm=True,
                                   fallback_heuristic=False, **kwargs)


def current(store, record_id):
    return store.get_by_exact_ref(record_id, scope=SCOPE, source_id="hermes")


def test_later_source_update_retires_prior_and_uses_actual_event_time(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        early, late = capture(store, OLD, EARLY), capture(store, NEW, LATE)
        old_id = extract(api, early, OLD)[0]["record_id"]
        llm = NativeFakeLLM(NEW)
        new_id = extract(api, late, NEW, llm=llm)[0]["record_id"]
        assert current(store, old_id).status == "superseded"
        assert current(store, new_id).status == "active"
        assert current(store, new_id).time.occurred_at == LATE
        assert business_metadata(current(store, new_id).meta)["source_event_binding"] == l1_source_event_binding(late)
        prompt = llm.conflict_prompts[0]
        assert prompt["candidate_pool"][0]["source_observation"] == {"record_id": early.record_id, "occurred_at": EARLY}
        assert prompt["new_memories"][0]["source_observation"] == {"record_id": late.record_id, "occurred_at": LATE}


def test_out_of_order_old_source_is_terminal_no_change_and_keeps_raw_history(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        early, late = capture(store, OLD, EARLY), capture(store, NEW, LATE)
        new_id = extract(api, late, NEW)[0]["record_id"]
        assert extract(api, early, OLD) == []
        assert current(store, new_id).status == "active"
        meta = business_metadata(current(store, early.record_id).meta)
        assert meta["l1_extraction_status"] == "no_change"
        assert meta["l1_extraction_diagnostics"]["adjudication"]["source_order_stale_count"] == 1
        assert meta["l1_extraction_diagnostics"]["adjudication"]["skipped_targets"]["refs"][0]["record_id"] == new_id
        assert current(store, early.record_id).content == early.content
        assert current(store, late.record_id).content == late.content
        assert extract(api, early, OLD, llm=SimpleNamespace(complete=lambda **k: pytest.fail("Completed retry"))) == []


def test_same_semantic_key_cannot_bypass_source_order_on_store_decision(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        early, late = capture(store, OLD, EARLY), capture(store, NEW, LATE)
        def persist(parent, text):
            atom = L1Atom(text=text, title=text, memory_type="fact", semantic_key="same-state-key",
                          category="fact", source_message_ids=(parent.record_id,))
            return persist_l1_atoms(api, atoms=[atom], episode_id=parent.record_id, scope=SCOPE,
                channel_id="hermes", llm=NativeFakeLLM(text, action="store"), strict_conflict=True)
        new_id = persist(late, NEW)[0]["record_id"]
        assert persist(early, OLD) == []
        assert current(store, new_id).status == "active"


@pytest.mark.parametrize("at", [LATE, "", "not-a-timestamp", "2026-10-02T01:00:00", "0001-01-01T00:00:00+02:00"])
def test_equal_or_unknown_source_clock_cannot_replace_other_observation(tmp_path, at):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        late = capture(store, NEW, LATE)
        new_id = extract(api, late, NEW)[0]["record_id"]
        incoming = capture(store, OLD, at)
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_order_unknown"):
            extract(api, incoming, OLD)
        assert current(store, new_id).status == "active"
        meta = business_metadata(current(store, incoming.record_id).meta)
        assert not meta.get("l1_extracted_at")
        assert meta["l1_extraction_diagnostics"]["completion_status"] == "blocked"


def test_distinct_episodic_events_accumulate_despite_equal_semantic_keys(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        rows = []
        for text, at in [(NEW, LATE), (OLD, EARLY)]:
            parent = capture(store, text, at)
            atom = L1Atom(text=text, title="Repeated event", memory_type="episodic",
                          semantic_key="same-event-title", category="episodic",
                          source_message_ids=(parent.record_id,))
            rows.extend(persist_l1_atoms(api, atoms=[atom], episode_id=parent.record_id, scope=SCOPE,
                channel_id="hermes", llm=NativeFakeLLM(text, action="store"), strict_conflict=True))
        assert len(rows) == 2
        assert all(current(store, row["record_id"]).status == "active" for row in rows)


def test_source_order_rejection_rolls_back_all_targets_and_incoming_record(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        targets = [api.ingest(text=text, title=text, memory_type="fact", scope=asdict(SCOPE),
            source="hermes.manual", source_id="hermes", force_capture=True) for text in [OLD, NEW]]
        for target, at in zip(targets, [EARLY, "2026-10-03T01:00:00Z"]):
            target.time.occurred_at = at
            store.mutate_records_atomically(lambda sqlite, row=target: (
                sqlite.upsert(row, commit=False), [row], []))
        parent = capture(store, "Orchid hourly supervision final setting is paused.", LATE)
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_order_stale"):
            api.ingest(record_id="mem_synthetic_atomic_incoming", text="Orchid setting is paused.",
                title="Orchid final setting", memory_type="fact", scope=asdict(SCOPE), source="hermes.l1",
                source_id="hermes", force_capture=True, meta={"memory_layer":"l1", "capture_origin":"l1_extract"},
                links=[LinkRef(relation="derived_from", target_kind="memory", target_id=parent.record_id)],
                supersede_record_ids=[row.record_id for row in targets], source_event_binding=l1_source_event_binding(parent))
        assert current(store, "mem_synthetic_atomic_incoming") is None
        assert all(current(store, row.record_id).status == "active" for row in targets)


@pytest.mark.parametrize("field", ["body", "session_id", "status", "mutation_state"])
def test_source_change_during_extraction_is_blocked_before_any_child_write(tmp_path, field):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, OLD, EARLY)
        def change():
            if field == "body":
                rewrite(store, parent, text=NEW)
            elif field == "status":
                rewrite(store, parent, status="removed")
            else:
                rewrite(store, parent, meta={**parent.meta, field: "changed" if field == "session_id" else "removed"})
        with pytest.raises((L1SourceOrderBlocked, ValueError)):
            extract(api, parent, OLD, llm=NativeFakeLLM(OLD, mutate=change))
        rows = store.list_records(kinds=["memory"], scope=SCOPE, limit=100)
        assert not any(business_metadata(row.meta).get("memory_layer") == "l1" for row in rows)
        assert not business_metadata(current(store, parent.record_id).meta).get("l1_extracted_at")


@pytest.mark.parametrize("marker", ["status", "mutation_state"])
def test_empty_output_does_not_complete_an_inactivated_parent(tmp_path, marker):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, OLD, EARLY)
        change = lambda: rewrite(store, parent, **(
            {"status":"removed"} if marker == "status" else {"meta":{**parent.meta, "mutation_state":"removed"}}))
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_parent_unavailable"):
            extract(api, parent, OLD, llm=NativeFakeLLM(OLD, mutate=change, empty=True))
        assert not business_metadata(current(store, parent.record_id).meta).get("l1_extracted_at")


def test_stale_queued_role_material_never_overrides_authoritative_parent(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        parent = capture(store, "Falcon account belongs to Alice.", EARLY)
        updated = rewrite(store, parent, text="Falcon account belongs to Bob.")
        llm = NativeFakeLLM("Falcon account belongs to Bob.")
        written = extract(api, parent, "Falcon account belongs to Bob.", llm=llm,
            user_text="Falcon account belongs to Alice.", assistant_text="Wrong stale assistant.", turn_text=parent.summary)
        assert "Alice" not in llm.extract_prompts[0]
        assert "Wrong stale assistant" not in llm.extract_prompts[0]
        assert "Bob" in llm.extract_prompts[0]
        atom = current(store, written[0]["record_id"])
        assert atom.summary == "Falcon account belongs to Bob."
        assert business_metadata(atom.meta)["source_event_binding"] == l1_source_event_binding(updated)


def test_old_v2_no_memory_is_only_reprocessed_on_explicit_retry_and_v3_is_idempotent(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, NEW, LATE)
        parent = rewrite(store, parent, meta={**parent.meta, "l1_extract_version":"l1_extract.v2",
            "l1_extracted_at":"2026-10-03T00:00:00Z", "l1_extraction_status":"no_memory", "l1_atom_count":0})
        forbidden = SimpleNamespace(complete=lambda **k: pytest.fail("Default legacy reprocess"))
        assert extract(api, parent, NEW, llm=forbidden) == []
        assert L1_EXTRACT_VERSION == "l1_extract.v3"
        rows = extract(api, parent, NEW, retry_legacy=True)
        assert len(rows) == 1
        updated = current(store, parent.record_id)
        assert business_metadata(updated.meta)["l1_extract_version"] == L1_EXTRACT_VERSION
        assert business_metadata(updated.meta)["l1_extraction_diagnostics"]["model_called"] is True
        assert extract(api, updated, NEW, retry_legacy=True, llm=forbidden) == []


def test_material_binding_ignores_completion_touch_but_covers_identity_metadata(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store, NEW, LATE)
        digest = l1_source_material_digest(parent)
        parent.meta = {**parent.meta, "l1_extracted_at":"later", "l1_atom_count":1, "l1_extraction_diagnostics":{}}
        parent.touch()
        assert l1_source_material_digest(parent) == digest
        parent.meta = {**parent.meta, "session_id":"different"}
        assert l1_source_material_digest(parent) != digest


@pytest.mark.parametrize("channel", ["hermes", "openclaw", "codex"])
def test_native_source_scope_is_preserved_across_client_identity_stamping(tmp_path, channel):
    scope = ScopeRef(tenant_id="synthetic", agent_id="xiaomage",
                     workspace_id="isolated::channel::" + channel, user_id="owner")
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        parent = capture(store, NEW, LATE, source_id=channel, scope=scope)
        written = extract(api, parent, NEW)
        atom = store.get_by_exact_ref(written[0]["record_id"], scope=scope, source_id=channel)
        assert atom is not None and atom.scope == scope and atom.source_id == channel
        assert business_metadata(atom.meta)["source_event_binding"] == l1_source_event_binding(parent)


@pytest.mark.parametrize("boundary", ["source", "scope"])
def test_other_partition_cannot_replay_a_source_binding(tmp_path, boundary):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, NEW, LATE)
        source_id = "other" if boundary == "source" else "hermes"
        scope = SCOPE if boundary == "source" else ScopeRef(**{**asdict(SCOPE), "user_id":"other"})
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_parent_unavailable"):
            api.ingest(text=NEW, title=NEW, memory_type="fact", scope={**asdict(scope), "preserve_scope":True},
                source="hermes.l1", source_id=source_id, force_capture=True,
                links=[LinkRef(relation="derived_from", target_kind="memory", target_id=parent.record_id)],
                meta={"memory_layer":"l1", "capture_origin":"l1_extract"},
                source_event_binding=l1_source_event_binding(parent))
        assert store.list_records(kinds=["memory"], scope=scope, limit=100) == ([parent] if scope == SCOPE else [])


def test_idempotent_bound_ingest_still_rechecks_source_inside_write_owner(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, NEW, LATE)
        kwargs = dict(record_id="mem_synthetic_bound_once", text=NEW, title=NEW, memory_type="fact",
            scope={**asdict(SCOPE), "preserve_scope":True}, source="hermes.l1", source_id="hermes",
            force_capture=True, links=[LinkRef(relation="derived_from", target_kind="memory", target_id=parent.record_id)],
            meta={"memory_layer":"l1", "capture_origin":"l1_extract"},
            source_event_binding=l1_source_event_binding(parent))
        original = api.ingest(**kwargs)
        calls = []
        append = store.append_and_supersede
        def changed_before_owner(*args, **kw):
            calls.append(True)
            rewrite(store, parent, text=OLD)
            return append(*args, **kw)
        monkeypatch.setattr(store, "append_and_supersede", changed_before_owner)
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_parent_changed"):
            api.ingest(**kwargs)
        assert len(calls) == 1
        assert current(store, original.record_id).to_dict() == original.to_dict()


def test_unknown_legacy_partial_child_is_not_rebound_or_completed(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, NEW, LATE)
        child_id = extract(api, parent, NEW)[0]["record_id"]
        # A legacy first child commit can exist without a source binding or
        # parent completion, carrying extraction-clock time instead of source
        # time. That old provenance is not reconstructed from a new model run.
        def legacy_partial(sqlite):
            child = sqlite.get_by_exact_ref(child_id, scope=SCOPE, source_id="hermes")
            child.meta = {key:value for key, value in child.meta.items() if key != "source_event_binding"}
            nested = child.meta.get("business_meta")
            if isinstance(nested, dict):
                child.meta["business_meta"] = {key:value for key, value in nested.items() if key != "source_event_binding"}
            child.time.occurred_at = "2026-10-03T01:00:00Z"
            parent_row = sqlite.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="hermes")
            parent_row.meta = dict(parent.meta)
            sqlite.upsert(child, commit=False)
            sqlite.upsert(parent_row, commit=False)
            return child, [child, parent_row], []
        legacy = store.mutate_records_atomically(legacy_partial).to_dict()
        with pytest.raises(L1SourceOrderBlocked, match="l1_legacy_source_binding_unknown"):
            extract(api, parent, NEW)
        assert current(store, child_id).to_dict() == legacy
        meta = business_metadata(current(store, parent.record_id).meta)
        assert not meta.get("l1_extracted_at")
        assert meta["l1_extraction_diagnostics"]["completion_status"] == "blocked"
        assert meta["l1_extraction_diagnostics"]["failure_reason"] == "l1_legacy_source_binding_unknown"


def remove_native_observed_time(store, record, mode, *, archived):
    payload = record.to_dict()
    if mode == "missing":
        payload["time"].pop("occurred_at")
    elif mode == "null":
        payload["time"]["occurred_at"] = None
    else:
        payload["time"]["occurred_at"] = True
    raw = canonical_payload_json(payload)
    with store._lock:
        pointer = store.sqlite.payload_segments.append(raw.encode()) if archived else None
        # An archived header deliberately retains the old known clock; only
        # the fully hydrated, digest-verified source payload is authoritative.
        header = canonical_payload_json(record.to_dict()) if archived else raw
        store.sqlite.execute("UPDATE records SET payload_json=?,payload_digest=?,payload_pointer_json=? WHERE storage_key=?",
            (header, payload_digest(payload), json.dumps(pointer) if pointer else "", store.sqlite._storage_key(record)))
        store.sqlite.commit()


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("mode", ["missing", "null", "nonstring"])
def test_missing_raw_source_clock_blocks_and_failed_diagnostic_preserves_unknown(tmp_path, mode, archived):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, NEW, LATE)
        remove_native_observed_time(store, parent, mode, archived=archived)
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_order_unknown"):
            extract(api, parent, NEW)
        strict = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="hermes",
                                        preserve_unknown_occurred_at=True)
        assert strict.time.occurred_at == ""
        # Failure metadata must not permanently save a hydration clock as the
        # event time. Upsert canonicalizes the unknown representation to ''.
        assert current(store, parent.record_id).time.occurred_at == ""
        assert not business_metadata(strict.meta).get("l1_extracted_at")
        assert business_metadata(strict.meta)["l1_extraction_diagnostics"]["source_binding"]["source_material_digest"] == l1_source_material_digest(strict)


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("kind", ["derived", "manual"])
def test_future_source_cannot_supersede_unknown_native_prior_clock(tmp_path, kind, archived):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        if kind == "derived":
            parent = capture(store, OLD, EARLY)
            child_id = extract(api, parent, OLD)[0]["record_id"]
            child = current(store, child_id)
            # Older releases had no source_event_binding. Removing the raw
            # source clock must remain unknown even for those real lineages.
            child.meta = {key:value for key, value in child.meta.items() if key != "source_event_binding"}
            if isinstance(child.meta.get("business_meta"), dict):
                child.meta["business_meta"] = {key:value for key, value in child.meta["business_meta"].items()
                                             if key != "source_event_binding"}
            rewrite(store, child, meta=child.meta)
            remove_native_observed_time(store, current(store, parent.record_id), "missing", archived=archived)
        else:
            child = api.ingest(text=OLD, title=OLD, memory_type="fact", scope=asdict(SCOPE),
                source="hermes.manual", source_id="hermes", force_capture=True)
            child_id = child.record_id
            remove_native_observed_time(store, child, "null", archived=archived)
        incoming = capture(store, NEW, "2099-10-02T01:00:00Z")
        with pytest.raises(L1SourceOrderBlocked, match="l1_source_order_unknown"):
            extract(api, incoming, NEW)
        assert current(store, child_id).status == "active"
        assert not business_metadata(current(store, incoming.record_id).meta).get("l1_extracted_at")


def test_existing_atom_final_decision_conflict_is_not_silently_reused(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, OLD, EARLY)
        original = extract(api, parent, OLD)[0]["record_id"]
        # Reopen a known partial write, then introduce another current-state
        # target so retry adjudication is actually invoked.
        rewrite(store, parent, meta=dict(parent.meta))
        api.ingest(text="Orchid hourly supervision details are documented.",
            title="Orchid current detail", memory_type="fact", scope=asdict(SCOPE),
            source="hermes.manual", source_id="hermes", force_capture=True)
        class ConflictingRetry(NativeFakeLLM):
            def complete(self, **kwargs):
                result = super().complete(**kwargs)
                if kwargs["user_prompt"].startswith("{"):
                    body = json.loads(result.text)
                    body[0]["merged_content"] = "A changed final judgment that was never stored."
                    result.text = json.dumps(body)
                return result
        before = current(store, original).to_dict()
        with pytest.raises(L1SourceOrderBlocked, match="l1_existing_atom_conflict"):
            extract(api, parent, OLD, llm=ConflictingRetry(OLD))
        assert current(store, original).to_dict() == before
        meta = business_metadata(current(store, parent.record_id).meta)
        assert not meta.get("l1_extracted_at")
        assert meta["l1_extraction_diagnostics"]["failure_reason"] == "l1_existing_atom_conflict"


@pytest.mark.parametrize("change", ["mutation_state", "superseded_by", "event_time", "binding_removed"])
def test_existing_reconcile_rechecks_atom_state_inside_second_transaction(tmp_path, monkeypatch, change):
    import eimemory.knowledge.l1_pipeline as pipeline
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store, NEW, LATE)
        child_id = extract(api, parent, NEW)[0]["record_id"]
        rewrite(store, parent, meta=dict(parent.meta))
        target = api.ingest(text=OLD, title=OLD, memory_type="fact", scope=asdict(SCOPE),
            source="hermes.manual", source_id="hermes", force_capture=True)
        target.time.occurred_at = EARLY
        store.mutate_records_atomically(lambda sqlite: (sqlite.upsert(target, commit=False), [target], []))
        target_before = current(store, target.record_id).to_dict()
        reconcile = pipeline._reconcile_existing_l1_update
        crossed_first_transaction = []
        def change_between_transactions(*args, **kwargs):
            crossed_first_transaction.append(True)
            row = current(store, child_id)
            if change == "event_time":
                row.time.occurred_at = EARLY
            elif change == "binding_removed":
                row.meta = {key:value for key, value in row.meta.items() if key != "source_event_binding"}
                if isinstance(row.meta.get("business_meta"), dict):
                    row.meta["business_meta"] = {key:value for key, value in row.meta["business_meta"].items()
                                                 if key != "source_event_binding"}
            else:
                row.meta = {**row.meta, change:"removed" if change == "mutation_state" else "mem_other_successor"}
            store.mutate_records_atomically(lambda sqlite: (sqlite.upsert(row, commit=False), [row], []))
            return reconcile(*args, **kwargs)
        monkeypatch.setattr(pipeline, "_reconcile_existing_l1_update", change_between_transactions)
        with pytest.raises(L1SourceOrderBlocked):
            extract(api, parent, NEW)
        assert crossed_first_transaction == [True]
        assert current(store, target.record_id).to_dict() == target_before
        assert not business_metadata(current(store, parent.record_id).meta).get("l1_extracted_at")
