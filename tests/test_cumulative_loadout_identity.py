from eimemory.recall.loadout import assemble_loadout, render_loadout


def row(source='a', user='owner', memory_type='instruction'):
    return dict(record_id='mem_fixture', source_id=source, scope=dict(tenant_id='synthetic',agent_id='synthetic',workspace_id='synthetic',user_id=user),memory_type=memory_type,summary='Synthetic fixture memory.',title='fixture')


def test_loadout_preserves_same_id_other_source_and_scope():
    rows=[row(),row('b'),row('c',memory_type='fact'),row('a','other',memory_type='fact')]
    payload=assemble_loadout(rows,limit=5)
    assert len(payload['persona'])+len(payload['items'])==4
    assert render_loadout(payload,max_chars=4096).count('Synthetic fixture memory.')==4


def test_complete_duplicate_is_emitted_once():
    a=row()
    payload=assemble_loadout([a,dict(a),dict(a,memory_type='fact')],limit=5)
    assert render_loadout(payload,max_chars=4096).count('Synthetic fixture memory.')==1


def test_incomplete_identity_does_not_prove_equivalence():
    a=row();a.pop('scope')
    payload=assemble_loadout([a,dict(a,memory_type='fact')],limit=5)
    assert render_loadout(payload,max_chars=4096).count('Synthetic fixture memory.')==2
