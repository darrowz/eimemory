"""Immutable enqueue-time evidence. No database writes or provider imports."""
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CompletedTurnSnapshot:
    session_id: str
    hermes_home: str
    _payload: str

    @classmethod
    def capture(cls, messages, *, session_id, hermes_home):
        if not isinstance(messages, list) or not messages:
            return None
        tail = messages[-128:]
        if any(not isinstance(m, dict) for m in tail):
            return None
        start = next((i for i in range(len(tail) - 1, -1, -1)
                      if tail[i].get('role') == 'user'), None)
        if start is None:
            return None
        turn = tail[start:]
        keys = ('id', '_row_id', '_db_persisted', 'role', 'content', 'tool_name',
                'tool_call_id', 'tool_calls', 'session_id', 'scope', 'source_id', 'source_event_id')
        try:
            payload = json.dumps([{k: m[k] for k in keys if k in m} for m in turn], ensure_ascii=False)
            if len(payload) > 1_000_000:
                return None
            return cls(str(session_id), str(Path(hermes_home).resolve()), payload)
        except (TypeError, ValueError, RecursionError):
            return None

    def messages(self):
        return json.loads(self._payload)
