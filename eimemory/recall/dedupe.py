"""Full memory-content identity; callers must also partition by scope/source ID."""
from hashlib import sha256
import json

from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


def memory_content_key(item: RecordEnvelope) -> str:
    # Projection truncation and token sets cannot establish fact equivalence.
    content = dict(item.content)
    if isinstance(content.get('text'), str):
        content['text'] = ' '.join(content['text'].split())
    metadata = {key: value for key, value in business_metadata(item.meta).items()
                if key not in {'quality', 'scoring'}}
    memory_type = str(metadata.get('memory_type') or content.get('memory_type') or '').lower()
    text = json.dumps([
        item.kind, item.source, item.status, ' '.join(item.title.split()), ' '.join(item.summary.split()),
        item.detail, content, metadata, item.provenance, item.tags,
        [(link.relation, link.target_kind, link.target_id) for link in item.links],
        item.evidence, item.aliases, item.aliases_version,
        item.time.occurred_at if memory_type in {'event', 'commitment'} else '',
    ], ensure_ascii=False, sort_keys=True)
    return sha256(text.encode('utf-8')).hexdigest()[:24]
