"""Persona-only regressions: synthetic Store roots; never execute runtime hooks."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.persona.cli import handle_persona_command
from eimemory.persona.correction import correction_from_user_text
from eimemory.persona.feedback_safety import PersonaCorrectionRejected, RAW_TEXT_WITHHELD
from eimemory.persona.store import PersonaStore, _stable_hash
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = {"tenant_id": "test-tenant", "agent_id": "test-agent", "workspace_id": "test-workspace", "user_id": "test-user"}


@pytest.fixture
def stores(tmp_path):
    raw = RuntimeStore(tmp_path / "synthetic-store")
    try:
        yield raw, PersonaStore(raw)
    finally:
        raw.close()


def record_id(key, scope=SCOPE):
    return "personacorr_" + _stable_hash(scope, key)[:24]


def legacy_record(text="too verbose", *, key="legacy-event", status="active", source="persona.correction", scope=SCOPE):
    correction = correction_from_user_text(text)
    record = RecordEnvelope.create(
        kind="feedback", title="Legacy persona correction", summary=correction.rule_candidate,
        detail=text, scope=ScopeRef.from_dict(scope), source=source, status=status,
        content={**correction.to_dict(), "idempotency_key": key}, meta={"idempotency_key": key},
    )
    record.record_id = record_id(key, scope)
    return record


def assert_rejected(exc, code):
    assert type(exc.value) is PersonaCorrectionRejected
    assert str(exc.value) == code
    assert exc.value.to_dict() == {"ok": False, "persisted": False, "record_id": None, "error": code}


@pytest.mark.parametrize("secret", [
    "API key=SYNTHETIC_PRIVATE_123", "password: SYNTHETIC_PRIVATE_123",
    "token is SYNTHETIC_PRIVATE_123", "密钥为SYNTHETIC_PRIVATE_123", "密码：SYNTHETIC_PRIVATE_123",
    "Bearer synthetic-token-12345", "sk-synthetic123456789012345", "ghp_synthetic1234567890123",
    "github_pat_synthetic1234567890", "AKIAABCDEFGHIJKLMNOP", "eyJsynthetic.abcdefghijk.lmnopqrs",
    "-----BEGIN PRIVATE KEY----- synthetic",
])
def test_secret_feedback_rejected_without_any_new_store_write(stores, secret):
    raw, store = stores
    before = {str(p.relative_to(raw.root)): p.read_bytes() for p in raw.root.rglob('*') if p.is_file()}
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text("too verbose; " + secret), scope=SCOPE, idempotency_key="event")
    assert_rejected(exc, "sensitive_feedback")
    after = {str(p.relative_to(raw.root)): p.read_bytes() for p in raw.root.rglob('*') if p.is_file()}
    assert after == before
    assert secret not in json.dumps(exc.value.to_dict())


@pytest.mark.parametrize("field", ["rule_candidate", "idempotency_key"])
def test_sensitive_rule_or_key_is_also_rejected(stores, field):
    raw, store = stores
    correction = correction_from_user_text("too verbose")
    key = "event"
    if field == 'rule_candidate': correction.rule_candidate = 'password=SYNTHETIC'
    else: key = 'session|password=SYNTHETIC'
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction, scope=SCOPE, idempotency_key=key)
    assert_rejected(exc, "sensitive_feedback")
    assert raw.count_records(kinds=["feedback"], scope=SCOPE) == 0


def test_store_only_path_cannot_claim_a_correction_was_persisted(tmp_path):
    store = PersonaStore(str(tmp_path / 'uncreated'))
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text("too verbose"), scope=SCOPE)
    assert_rejected(exc, "record_store_unavailable")
    assert not store.root.exists()


def test_accepted_record_and_key_are_minimized_with_compatible_return(stores):
    raw, store = stores
    text = 'too verbose; synthetic ordinary personal detail 7329'
    key = 'session|verbosity|' + text
    correction = correction_from_user_text(text)
    stored = store.record_correction(correction, scope=SCOPE, idempotency_key=key)
    assert isinstance(stored, RecordEnvelope)
    assert stored.record_id == record_id(key)
    assert stored.meta['persisted'] is True
    assert stored.meta['source_validation'] == 'format_only'
    persisted = raw.get_by_id(stored.record_id, scope=SCOPE, exact_scope=True)
    for value in (stored.to_dict(), persisted.to_dict()):
        encoded = json.dumps(value, ensure_ascii=False)
        assert text not in encoded and key not in encoded
        assert value['detail'] == RAW_TEXT_WITHHELD
        assert value['content']['raw_text'] == RAW_TEXT_WITHHELD
    assert len(persisted.content['idempotency_digest']) == 64
    assert 'idempotency_key' not in persisted.content
    assert 'idempotency_key' not in persisted.meta
    assert text not in (raw.root/'records.jsonl').read_text()


def test_safe_retries_use_old_real_identity_without_new_write(stores):
    raw, store = stores
    correction = correction_from_user_text('too verbose')
    first = store.record_correction(correction, scope=SCOPE, idempotency_key='same')
    before = (raw.root/'records.jsonl').read_bytes()
    again = PersonaStore(raw).record_correction(correction_from_user_text('too verbose'), scope=SCOPE, idempotency_key='same')
    assert again.record_id == first.record_id
    assert (raw.root/'records.jsonl').read_bytes() == before
    assert raw.count_records(kinds=['feedback'], scope=SCOPE) == 1


def test_retry_survives_store_close_reopen(tmp_path):
    root = tmp_path/'store'
    raw = RuntimeStore(root)
    first = PersonaStore(raw).record_correction(correction_from_user_text('too verbose'), scope=SCOPE, idempotency_key='same')
    raw.close()
    raw = RuntimeStore(root)
    try:
        again = PersonaStore(raw).record_correction(correction_from_user_text('too verbose'), scope=SCOPE, idempotency_key='same')
        assert again.record_id == first.record_id
        assert raw.count_records(kinds=['feedback'], scope=SCOPE) == 1
    finally: raw.close()


def test_same_identity_different_input_is_safe_conflict(stores):
    raw, store = stores
    first = store.record_correction(correction_from_user_text('too verbose'), scope=SCOPE, idempotency_key='same')
    before = raw.get_by_id(first.record_id, scope=SCOPE, exact_scope=True).to_dict()
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text('incorrect'), scope=SCOPE, idempotency_key='same')
    assert_rejected(exc, 'idempotency_conflict')
    assert raw.get_by_id(first.record_id, scope=SCOPE, exact_scope=True).to_dict() == before


def test_legacy_exact_retry_never_returns_or_rewrites_extra_plaintext(stores, monkeypatch):
    raw, store = stores
    old = legacy_record()
    marker = 'SYNTHETIC_OLD_PRIVATE_MARKER'
    old.detail = marker
    old.summary = marker
    old.meta['legacy_secret'] = marker
    old.content['legacy_extension'] = marker
    old.evidence = [marker]
    raw.append(old)
    before = deepcopy(raw.get_by_id(old.record_id, scope=SCOPE, exact_scope=True).to_dict())
    log = (raw.root/'records.jsonl').read_bytes()
    real = raw.get_by_id
    calls = []
    def exact_lookup(*args, **kwargs):
        calls.append((args,kwargs))
        return real(*args,**kwargs)
    monkeypatch.setattr(raw, 'get_by_id', exact_lookup)
    def no_scan(**kwargs): raise AssertionError('legacy retry must not scan records')
    monkeypatch.setattr(raw, 'list_records', no_scan)
    result = store.record_correction(correction_from_user_text('too verbose'), scope=SCOPE, idempotency_key='legacy-event')
    assert result.record_id == old.record_id and result.meta['persisted'] is True
    assert marker not in json.dumps(result.to_dict())
    assert result.detail == RAW_TEXT_WITHHELD
    assert calls == [((old.record_id,), {'scope':ScopeRef.from_dict(SCOPE), 'exact_scope':True})]
    assert real(old.record_id,scope=SCOPE,exact_scope=True).to_dict() == before
    assert (raw.root/'records.jsonl').read_bytes() == log


def test_unsafe_legacy_exact_retry_is_safe_error_without_rewrite(stores):
    raw, store = stores
    old = legacy_record('too verbose; API key=SYNTHETIC_OLD_SECRET')
    raw.append(old)
    before = old.to_dict()
    log = (raw.root/'records.jsonl').read_bytes()
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text('too verbose'), scope=SCOPE, idempotency_key='legacy-event')
    assert_rejected(exc,'legacy_sensitive_payload_withheld')
    assert raw.get_by_id(old.record_id,scope=SCOPE,exact_scope=True).to_dict() == before
    assert (raw.root/'records.jsonl').read_bytes() == log


@pytest.mark.parametrize('field,value', [('source','tool_output'),('event_type','arbitrary.event'),('source',''),('event_type','')])
def test_invalid_inner_source_or_type_is_rejected_not_authentication(stores,field,value):
    raw, store = stores
    correction=correction_from_user_text('too verbose');setattr(correction,field,value)
    with pytest.raises(PersonaCorrectionRejected) as exc:store.record_correction(correction,scope=SCOPE)
    assert_rejected(exc,'invalid_source_type')
    assert raw.count_records(kinds=['feedback'],scope=SCOPE)==0


@pytest.mark.parametrize('mutate', [
    lambda c:setattr(c,'severity',float('nan')),lambda c:setattr(c,'severity',float('inf')),
    lambda c:setattr(c,'severity',True),lambda c:setattr(c,'category','arbitrary'),
    lambda c:setattr(c,'created_at','private unparseable timestamp'),
    lambda c:setattr(c,'raw_text','x'*16385),lambda c:setattr(c,'trait_delta',{'precision':float('nan')}),
])
def test_invalid_payload_rejected_with_safe_structured_error(stores,mutate):
    raw,store=stores;c=correction_from_user_text('too verbose');mutate(c)
    with pytest.raises(PersonaCorrectionRejected) as exc:store.record_correction(c,scope=SCOPE)
    assert_rejected(exc,'invalid_feedback')
    assert raw.count_records(kinds=['feedback'],scope=SCOPE)==0


def test_status_filtered_before_limit_and_invalid_content_skipped(stores,monkeypatch):
    raw,store=stores
    eligible=legacy_record(key='eligible');eligible.time.updated_at='2020-01-01T00:00:00Z';raw.append(eligible)
    for i,status in enumerate(['rejected','inactive','superseded','expired','removed']):
        item=legacy_record(key='status-'+str(i),status=status);item.time.updated_at='2030-01-01T00:00:00Z';raw.append(item)
    for i,(field,value) in enumerate([('source','tool_output'),('event_type','invalid')]):
        item=legacy_record(key='invalid-'+str(i));item.content[field]=value;raw.append(item)
    calls=[];real=raw.read_consistent
    def read_snapshot(callback):
        def inspect_reader(reader):
            query=reader.list_records
            def inspect_query(**kwargs): calls.append(kwargs);return query(**kwargs)
            with monkeypatch.context() as local:
                local.setattr(reader,'list_records',inspect_query)
                return callback(reader)
        return real(inspect_reader)
    monkeypatch.setattr(raw,'read_consistent',read_snapshot)
    corrections=store.list_corrections(scope=SCOPE,limit=1)
    assert len(corrections)==1 and corrections[0].raw_text==RAW_TEXT_WITHHELD
    assert calls and all(c['status']=='active' for c in calls)


def test_active_unrelated_feedback_does_not_starve_a_later_page(stores):
    raw,store=stores
    eligible=legacy_record(key='eligible');eligible.time.updated_at='2020-01-01T00:00:00Z';raw.append(eligible)
    for i in range(105):
        other=legacy_record(key='other-'+str(i),source='other.feedback');other.time.updated_at='2030-01-01T00:00:00Z';raw.append(other)
    result=store.list_corrections(scope=SCOPE,limit=1,max_scan=200)
    assert len(result)==1 and result[0].category=='verbosity'
    with pytest.raises(PersonaCorrectionRejected) as exc:store.list_corrections(scope=SCOPE,limit=1,max_scan=100)
    assert_rejected(exc,'correction_scan_incomplete')


def test_scan_is_bounded_and_does_not_claim_full_history(stores):
    raw,store=stores
    assert store.list_corrections(scope=SCOPE,limit=0)==[]
    with pytest.raises(PersonaCorrectionRejected) as exc:store.list_corrections(scope=SCOPE,limit=100,max_scan=2)
    assert_rejected(exc,'invalid_scan_limit')


def test_exact_scope_correction_list_does_not_accept_shared_user_records(stores):
    raw,store=stores
    shared={**SCOPE,'user_id':''}
    raw.append(legacy_record(scope=shared,key='shared'))
    assert store.list_corrections(scope=SCOPE)==[]


def test_cli_rejects_sensitive_feedback_without_printing_original(stores,capsys):
    raw,_=stores
    args=SimpleNamespace(persona_command='correct',text='too verbose; API key=SYNTHETIC_CLI_SECRET')
    code=handle_persona_command(args,SimpleNamespace(store=raw),SCOPE)
    captured=capsys.readouterr();out=captured.out
    assert captured.err==''
    assert code==2 and 'SYNTHETIC_CLI_SECRET' not in out
    assert json.loads(out)=={'ok':False,'persisted':False,'record_id':None,'error':'sensitive_feedback'}
    assert raw.count_records(kinds=['feedback'],scope=SCOPE)==0


def test_cli_success_keeps_category_but_outputs_safe_stored_content(stores,capsys):
    raw,_=stores
    args=SimpleNamespace(persona_command='correct',text='too verbose; ordinary synthetic detail')
    assert handle_persona_command(args,SimpleNamespace(store=raw),SCOPE)==0
    captured=capsys.readouterr();out=captured.out;payload=json.loads(out)
    assert captured.err==''
    assert payload['category']=='verbosity' and payload['persisted'] is True and payload['record_id']
    assert payload['raw_text']==RAW_TEXT_WITHHELD
    assert 'ordinary synthetic detail' not in out


def test_rejection_does_not_allocate_an_envelope_or_record_id(stores, monkeypatch):
    _,store=stores
    def forbid_create(**kwargs): raise AssertionError('Rejected input allocated a record')
    monkeypatch.setattr(RecordEnvelope,'create',forbid_create)
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text('too verbose; password=SYNTHETIC'),scope=SCOPE)
    assert_rejected(exc,'sensitive_feedback')


def test_append_race_with_legacy_sensitive_record_cannot_return_or_overwrite_it(stores, monkeypatch):
    raw,store=stores
    old=legacy_record('too verbose; API key=SYNTHETIC_RACE_SECRET')
    raw.append(old);log=(raw.root/'records.jsonl').read_bytes()
    real=raw.get_by_id
    # Simulate a stale lookup; native transactional append must still inspect the existing row.
    monkeypatch.setattr(raw,'get_by_id',lambda *args,**kwargs:None)
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text('too verbose'),scope=SCOPE,idempotency_key='legacy-event')
    assert_rejected(exc,'legacy_sensitive_payload_withheld')
    assert real(old.record_id,scope=SCOPE,exact_scope=True).to_dict()==old.to_dict()
    assert (raw.root/'records.jsonl').read_bytes()==log


def test_append_race_with_matching_record_returns_real_existing_identity(stores,monkeypatch):
    raw,store=stores
    old=legacy_record();raw.append(old);log=(raw.root/'records.jsonl').read_bytes()
    monkeypatch.setattr(raw,'get_by_id',lambda *args,**kwargs:None)
    result=store.record_correction(correction_from_user_text('too verbose'),scope=SCOPE,idempotency_key='legacy-event')
    assert result.record_id==old.record_id and result.meta['persisted'] is True
    assert (raw.root/'records.jsonl').read_bytes()==log


def test_cli_scan_limit_error_is_safe_and_does_not_evolve(stores,capsys,monkeypatch):
    raw,_=stores
    def incomplete(*args,**kwargs):raise PersonaCorrectionRejected('correction_scan_incomplete')
    monkeypatch.setattr(PersonaStore,'list_corrections',incomplete)
    code=handle_persona_command(SimpleNamespace(persona_command='evolve',dry_run=False),SimpleNamespace(store=raw),SCOPE)
    assert code==2
    assert json.loads(capsys.readouterr().out)=={'ok':False,'persisted':False,'record_id':None,'error':'correction_scan_incomplete'}
    assert not (raw.root/'state/persona_state.json').exists()


def corrupt_inline_payload(raw, record):
    def change(sqlite):
        sqlite.execute('UPDATE records SET payload_json = ? WHERE record_id = ?', ('[]', record.record_id))
        sqlite.commit()
    raw.run_locked(change)


def test_hydration_short_page_is_incomplete_not_false_empty(stores):
    raw,store=stores
    eligible=legacy_record(key='older-eligible');eligible.time.updated_at='2000-01-01T00:00:00Z';raw.append(eligible)
    for i in range(100):
        item=legacy_record(key='unrelated-'+str(i),source='other.feedback');item.time.updated_at='2030-01-01T00:00:00Z';raw.append(item)
    corrupt_inline_payload(raw,item)
    assert raw.count_records(kinds=['feedback'],scope=SCOPE,status='active')==101
    assert len(raw.list_records(kinds=['feedback'],scope=SCOPE,status='active',limit=100))==99
    assert raw.list_records(kinds=['feedback'],scope=SCOPE,status='active',limit=100,offset=100)[0].record_id==eligible.record_id
    with pytest.raises(PersonaCorrectionRejected) as exc:store.list_corrections(scope=SCOPE,limit=1,max_scan=200)
    assert_rejected(exc,'correction_scan_incomplete')


def test_legal_empty_snapshot_is_empty(stores):
    _,store=stores
    assert store.list_corrections(scope=SCOPE,limit=1,max_scan=1)==[]


def test_legal_ninety_nine_row_tail_is_complete(stores):
    raw,store=stores
    for i in range(99):raw.append(legacy_record(key='tail-'+str(i)))
    assert len(store.list_corrections(scope=SCOPE,limit=100,max_scan=100))==99


def test_all_corrupt_physical_rows_are_not_a_legal_empty(stores):
    raw,store=stores
    item=legacy_record();raw.append(item);corrupt_inline_payload(raw,item)
    with pytest.raises(PersonaCorrectionRejected) as exc:store.list_corrections(scope=SCOPE,limit=1,max_scan=1)
    assert_rejected(exc,'correction_scan_incomplete')


def test_complete_first_page_can_return_requested_top_limit(stores):
    raw,store=stores
    for i in range(101):raw.append(legacy_record(key='window-'+str(i)))
    assert len(store.list_corrections(scope=SCOPE,limit=1,max_scan=100))==1


@pytest.mark.parametrize('change',['insert','delete'])
def test_count_and_pages_share_one_snapshot_during_real_writer_change(stores,monkeypatch,change):
    raw,store=stores
    original=legacy_record(key='snapshot-original');original.time.updated_at='2000-01-01T00:00:00Z';raw.append(original)
    other=RuntimeStore(raw.root)
    read_consistent=raw.read_consistent;observations=[]
    def run_snapshot(callback):
        def on_reader(reader):
            count=reader.count_records
            def count_then_change(**kwargs):
                total=count(**kwargs)
                observations.append({'in_transaction':reader.in_transaction,'physical_count':total})
                if change=='insert':
                    extra=legacy_record('incorrect',key='snapshot-new');extra.time.updated_at='2099-01-01T00:00:00Z';other.append(extra)
                else:
                    def remove(sqlite):
                        sqlite.execute('DELETE FROM records WHERE record_id = ?',(original.record_id,));sqlite.commit()
                    other.run_locked(remove)
                return total
            with monkeypatch.context() as local:
                local.setattr(reader,'count_records',count_then_change)
                return callback(reader)
        return read_consistent(on_reader)
    monkeypatch.setattr(raw,'read_consistent',run_snapshot)
    try:
        result=store.list_corrections(scope=SCOPE,limit=10,max_scan=100)
        assert observations==[{'in_transaction':True,'physical_count':1}]
        assert len(result)==1 and result[0].category=='verbosity'
        current_count=raw.count_records(kinds=['feedback'],scope=SCOPE,status='active')
        assert current_count==(2 if change=='insert' else 0)
    finally:other.close()


def test_snapshot_reader_preserves_callers_open_transaction(stores):
    raw,store=stores;raw.append(legacy_record())
    with raw.locked() as sqlite:
        sqlite.execute('BEGIN')
        try:
            assert len(store.list_corrections(scope=SCOPE,limit=1))==1
            assert sqlite.in_transaction is True
        finally:sqlite.rollback()


def test_missing_snapshot_capability_fails_closed_without_independent_count(tmp_path):
    class OldStore:
        root=str(tmp_path)
        def append(self,*args,**kwargs):raise AssertionError('no write')
        def count_records(self,**kwargs):raise AssertionError('independent count is not a snapshot')
        def list_records(self,**kwargs):raise AssertionError('unsnapshotted list must not run')
    store=PersonaStore(OldStore())
    with pytest.raises(PersonaCorrectionRejected) as exc:store.list_corrections(scope=SCOPE)
    assert_rejected(exc,'correction_scan_incomplete')
    assert store.list_corrections(scope=SCOPE,limit=0)==[]


@pytest.mark.parametrize('methods',[{}, {'count_records':lambda **kwargs:0}, {'list_records':lambda **kwargs:[]}])
def test_missing_reader_count_or_list_contract_fails_closed(tmp_path,methods):
    class IncompleteStore:
        root=str(tmp_path)
        def append(self,*args,**kwargs):raise AssertionError('no write')
        def read_consistent(self,callback):return callback(SimpleNamespace(**methods))
    with pytest.raises(PersonaCorrectionRejected) as exc:PersonaStore(IncompleteStore()).list_corrections(scope=SCOPE)
    assert_rejected(exc,'correction_scan_incomplete')


def test_corrupt_keyed_retry_becomes_structured_null_id_rejection(stores):
    raw,store=stores
    item=legacy_record();raw.append(item);corrupt_inline_payload(raw,item)
    # Warm native reader initialization before the byte-preservation observation.
    with pytest.raises(RuntimeError):raw.get_by_id(item.record_id,scope=SCOPE,exact_scope=True)
    before={str(p.relative_to(raw.root)):p.read_bytes() for p in raw.root.rglob('*') if p.is_file()}
    with pytest.raises(PersonaCorrectionRejected) as exc:
        store.record_correction(correction_from_user_text('too verbose'),scope=SCOPE,idempotency_key='legacy-event')
    assert_rejected(exc,'invalid_existing_correction')
    after={str(p.relative_to(raw.root)):p.read_bytes() for p in raw.root.rglob('*') if p.is_file()}
    assert before==after


@pytest.mark.parametrize('failure',[
    RuntimeError('exact_scope_record_unavailable_or_mismatched extra'),
    RuntimeError('prefix exact_scope_record_unavailable_or_mismatched'),
    RuntimeError('exact_scope_record_unavailable_or_mismatched','extra'),
    RuntimeError('database is locked'),
    type('CustomRuntimeError',(RuntimeError,),{})('exact_scope_record_unavailable_or_mismatched'),
])
def test_other_runtime_errors_are_not_swallowed_or_called_rejections(stores,monkeypatch,failure):
    raw,store=stores
    def fail(*args,**kwargs):raise failure
    monkeypatch.setattr(raw,'get_by_id',fail)
    with pytest.raises(RuntimeError) as exc:
        store.record_correction(correction_from_user_text('too verbose'),scope=SCOPE,idempotency_key='event')
    assert exc.value is failure and not isinstance(exc.value,PersonaCorrectionRejected)
    assert raw.count_records(kinds=['feedback'],scope=SCOPE)==0


def test_uncertain_write_runtime_error_is_not_mislabeled_as_safe_rejection(stores,monkeypatch):
    raw,store=stores
    failure=RuntimeError('exact_scope_record_unavailable_or_mismatched')
    def fail_write(*args,**kwargs):raise failure
    monkeypatch.setattr(raw,'append',fail_write)
    with pytest.raises(RuntimeError) as exc:
        store.record_correction(correction_from_user_text('too verbose'),scope=SCOPE,idempotency_key='event')
    assert exc.value is failure and not isinstance(exc.value,PersonaCorrectionRejected)
