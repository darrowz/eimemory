"""Storage-only subprocess fixture; deliberately has no pytest dependency."""
from dataclasses import asdict
import json
import sqlite3
from eimemory.models.records import RecordEnvelope

class SQLiteFixtureStore:
    """Real SQLite uniqueness/transactions; not the project's complete RuntimeStore."""
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS records (id TEXT, scope TEXT, payload TEXT, PRIMARY KEY(id,scope))')
    def append(self,record,*,existing_match=None):
        with sqlite3.connect(self.path,timeout=10) as c:
            c.execute('BEGIN IMMEDIATE')
            key=json.dumps(asdict(record.scope),sort_keys=True)
            row=c.execute('SELECT payload FROM records WHERE id=? AND scope=?',(record.record_id,key)).fetchone()
            if row:
                saved=RecordEnvelope.from_dict(json.loads(row[0]))
                if existing_match and not existing_match(saved):raise ValueError('conflict')
                return saved
            c.execute('INSERT INTO records VALUES (?,?,?)',(record.record_id,key,json.dumps(record.to_dict())))
        return record

class FixtureRuntime:
    def __init__(self,path):self.store=SQLiteFixtureStore(path)
    def close(self):pass

