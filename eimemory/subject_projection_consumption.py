"""Shared read fences for explicitly authorized subject projections."""
from contextlib import contextmanager, ExitStack
import sqlite3

from eimemory.subject_projection import (
    SOURCE, is_subject_projection, projection_authority_directory,
    subject_projection_validation_error,
)


@contextmanager
def projection_read_fence(records, *, reference_ids=()):
    """Keep the operator authority locked through the caller's physical read."""
    if not reference_ids and not any(is_subject_projection(record) for record in records):
        yield lambda _sqlite, _record: True
        return
    from eimemory.identity_authority import snapshot
    with ExitStack() as stack:
        try:
            authority, _ = stack.enter_context(snapshot(projection_authority_directory(), blocking=False))
        except (OSError, ValueError, ImportError):
            authority = None
        yield lambda sqlite, record: not subject_projection_validation_error(
            sqlite, record, authority=authority)


def delivered_projection_reference_ids(explanation):
    """Inspect body-bearing output lanes, never unselected candidate diagnostics."""
    if not isinstance(explanation, dict):
        return set()
    rows = list(explanation.get('cascade_evidence') or [])
    rows.extend(item.get('record') for item in explanation.get('raw_evidence') or [] if isinstance(item, dict))
    view = explanation.get('recall_view')
    if isinstance(view, dict):
        rows.extend(view.get('items') or [])
    found = set()
    for node in rows:
        if isinstance(node, dict):
            record_id = node.get('record_id')
            if isinstance(record_id, str) and (record_id.startswith('mem_sproj_') or node.get('source') == SOURCE):
                found.add(record_id)
    return found


def projection_is_current(store, record):
    """Ordinary records need no new authority; marked projections fail closed."""
    if not is_subject_projection(record):
        return True
    try:
        # Cache/attestation paths must not wait indefinitely on an operator's
        # exclusive authorization lock. The shared fence fails closed instead.
        with projection_read_fence([record]) as valid:
            return store.read_consistent(lambda sqlite: valid(sqlite, record))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ImportError):
        return False
