"""Fragment vector top-K regression: production builder plus relational oracle.

No PostgreSQL server or provider. SQLite scalar scores model the DISTINCT ON,
ORDER BY and LIMIT stages; this does not validate pgvector or planner behavior.
"""
import re
import sqlite3

import pytest

from eimemory.retrieval.contracts import CandidateRequest, ExactScope
from eimemory.retrieval.postgres_vector import PostgresCandidateRepository, PostgresVectorConfig


def build_arm(arm, limit):
    repository = PostgresCandidateRepository(PostgresVectorConfig(vector_dimension=3, evidence_fragments=True))
    request = CandidateRequest(query='synthetic', scope=ExactScope('tenant','agent','workspace','user'),
        source_ids=('source',), kinds=('memory',), limit=limit)
    where, params = repository._fragment_scope_filters(request, watermark='committed')
    return repository._fragment_arm_sql(arm=arm, fields='p.storage_key', where=where, params=params,
        literal='[1,0,0]', fts_query_text="'synthetic'", limit=limit)


def relational_winners(sql, params, rows):
    """Read the generated stage limits; execute equivalent scalar SQL in SQLite."""
    middle = sql.split(') ann ORDER BY ', 1)[1].split(') dedup ORDER BY ', 1)[0]
    assert middle.startswith('storage_key, vector_score DESC, fragment_id')
    middle_limit = ' LIMIT :k' if 'LIMIT %(result_limit)s' in middle else ''
    k = params['result_limit']
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE fragments(storage_key TEXT, fragment_id TEXT, vector_score REAL)')
        conn.executemany('INSERT INTO fragments VALUES(?,?,?)', rows)
        result = conn.execute('''WITH ann AS (
            SELECT * FROM fragments ORDER BY vector_score DESC LIMIT (:k * 8)
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY storage_key ORDER BY vector_score DESC, fragment_id
            ) AS rn FROM ann
        ), dedup AS (
            SELECT storage_key, fragment_id, vector_score FROM ranked WHERE rn=1
            ORDER BY storage_key, vector_score DESC, fragment_id''' + middle_limit + '''
        ) SELECT storage_key, fragment_id FROM dedup ORDER BY vector_score DESC, storage_key LIMIT :k''',
        {'k':k}).fetchall()
    return result


@pytest.mark.parametrize(('limit', 'rows', 'expected'), [
    (1, [('z-best','z',.99),('a-worse','a',.2)], [('z-best','z')]),
    (2, [('a-last','a',.1),('b-third','b',.7),('z-best','z2',.8),('z-best','z1',.99),
         ('y-second','y',.9)], [('z-best','z1'),('y-second','y')]),
    (2, [('z','b',.9),('z','a',.9),('a','x',.9),('b-low','b',.1)], [('a','x'),('z','a')]),
])
def test_vector_arm_keeps_global_leaders_from_ann_pool(limit, rows, expected):
    sql, params = build_arm('vector', limit)
    assert relational_winners(sql, params, rows) == expected


def test_vector_arm_retains_exact_filters_and_bounded_stages():
    sql, params = build_arm('vector', 5)
    assert sql.count('LIMIT ') == 2  # ANN 8K, then globally score-ordered K.
    assert 'LIMIT (%(result_limit)s * 8)' in sql
    assert sql.endswith('ORDER BY vector_score DESC, storage_key LIMIT %(result_limit)s')
    for name in ('tenant_id','agent_id','workspace_id','user_id','index_watermark','source_ids','kinds'):
        assert f'%({name})s' in sql
    assert set(re.findall(r'%\(([a-z_]+)\)s',sql)) <= params.keys()
    assert "p.status='active'" in sql
    assert params['source_ids'] == ['source']
    assert params['kinds'] == ['memory']


def test_keyword_arm_keeps_dedupe_then_score_limit():
    sql, params = build_arm('keyword', 5)
    assert 'DISTINCT ON(p.storage_key)' in sql
    assert 'ORDER BY p.storage_key,fragment_fts_score DESC,f.fragment_id) best' in sql
    assert sql.endswith('ORDER BY fragment_fts_score DESC,storage_key LIMIT %(result_limit)s')
    assert sql.count('LIMIT ') == 1
    assert params['result_limit'] == 5
