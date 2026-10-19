"""Append-only bounded chronology, using the existing RuntimeStore connection.

The control plane injects its existing HMAC signer/verifier. This data module
has no governance, Runtime, provider or effect dependencies.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from eimemory.contracts.code_incident_coverage import (
    CHECKPOINT_SCHEMA, OBSERVATION_SCHEMA, DOMAIN, SCOPE_FIELDS, FACT_FIELDS,
    MAX_CHECKPOINT_AGE_SECONDS, MAX_JOURNAL_PAGE, MAX_EPOCH_CHECKPOINTS, NATIVE_SOURCES, canonical, digest, exact_scope,
    incident_key, instant, unknown, validate_diagnostic,
)
from eimemory.storage.migrations.code_incident_coverage import (
    JOURNAL, CHECKPOINTS, OBSERVATIONS, schema_ready, trigger_definitions,
)


def _now():
    return datetime.now(timezone.utc)


class CodeIncidentCoverageStore:
    def __init__(self, store):
        self.conn = store.sqlite.conn
        self.lock = store._lock

    def _transaction(self, write, callback):
        with self.lock:
            owns = not self.conn.in_transaction
            if write and not owns:
                raise ValueError('incident_coverage_requires_own_write_transaction')
            if owns:
                self.conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                result = callback()
                if owns:
                    self.conn.commit() if write else self.conn.rollback()
                return result
            except BaseException:
                if owns:
                    self.conn.rollback()
                raise

    def _head(self):
        high = int(self.conn.execute(f'SELECT coalesce(max(sequence),0) FROM {JOURNAL}').fetchone()[0])
        row = self.conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (JOURNAL,)).fetchone()
        if row is not None and int(row[0]) != high:
            raise ValueError('incident_coverage_journal_prefix_changed')
        return high

    def _page(self, scope, after, through):
        rows = self.conn.execute(f'''SELECT * FROM {JOURNAL} WHERE sequence>? AND sequence<=?
            AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?
            UNION ALL SELECT * FROM {JOURNAL} WHERE sequence>? AND sequence<=? AND table_name='coverage_control'
            ORDER BY sequence LIMIT ?''',
            (after, through, *(scope[key] for key in SCOPE_FIELDS), after, through, MAX_JOURNAL_PAGE + 1)).fetchall()
        if len(rows) > MAX_JOURNAL_PAGE:
            raise ValueError('incident_coverage_incremental_page_incomplete')
        return [dict(row) for row in rows]

    def _row(self, table, scope, verify, record_id=None):
        condition = ' AND record_id=?' if record_id is not None else ''
        args = [scope[key] for key in SCOPE_FIELDS] + ([record_id] if record_id is not None else [])
        row = self.conn.execute(f'SELECT * FROM {table} WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?{condition} ORDER BY sequence DESC LIMIT 1', args).fetchone()
        if row is None:
            return None
        body = json.loads(row['payload_json'])
        domain = CHECKPOINT_SCHEMA if table == CHECKPOINTS else OBSERVATION_SCHEMA
        validate_diagnostic(body, schema=domain, scope=scope)
        if digest(body) != row['payload_digest'] or not verify(body, domain):
            raise ValueError('incident_coverage_signature_or_row_invalid')
        return body

    def _checkpoint_by_digest(self, value, scope, verify):
        row = self.conn.execute(f'SELECT payload_json,payload_digest FROM {CHECKPOINTS} WHERE payload_digest=?', (value,)).fetchone()
        if row is None:
            raise ValueError('incident_coverage_checkpoint_prefix_missing')
        body = json.loads(row['payload_json'])
        validate_diagnostic(body, schema=CHECKPOINT_SCHEMA, scope=scope)
        if digest(body) != value or not verify(body, CHECKPOINT_SCHEMA):
            raise ValueError('incident_coverage_checkpoint_prefix_unverified')
        return body

    def _verify_increment(self, checkpoint, scope):
        low, high = checkpoint['prior_high_water'], checkpoint['high_water']
        if checkpoint.get('incremental_truncated') is True:
            if checkpoint['continuity'] is not False:
                raise ValueError('incident_coverage_truncated_continuity_invalid')
            return
        if high - low > MAX_JOURNAL_PAGE or low > high:
            raise ValueError('incident_coverage_incremental_bound_invalid')
        count = self.conn.execute(f'SELECT count(*) FROM {JOURNAL} WHERE sequence>? AND sequence<=?', (low, high)).fetchone()[0]
        page = self._page(scope, low, high)
        if count != high - low or len(page) != checkpoint['incremental_count'] or digest(page) != checkpoint['incremental_digest']:
            raise ValueError('incident_coverage_incremental_prefix_changed')

    def _verify_epoch(self, checkpoint, scope, verify):
        """Bounded physical prefix replay only for an actual incident/readback.

        Daily checkpoints verify just their last increment. An incident proof
        additionally revalidates every signed interval in its bounded epoch.
        No record bodies, event text or pre-epoch journal are scanned.
        """
        current = checkpoint
        seen = set()
        for remaining in range(checkpoint['epoch_checkpoint_count'], 0, -1):
            token = digest(current)
            if token in seen:
                raise ValueError('incident_coverage_checkpoint_cycle')
            seen.add(token)
            if current['epoch_checkpoint_count'] != remaining:
                raise ValueError('incident_coverage_checkpoint_chain_length_mismatch')
            self._verify_increment(current, scope)
            if current['epoch_start_sequence'] == current['high_water'] and not current['continuity']:
                if remaining != 1:
                    raise ValueError('incident_coverage_checkpoint_chain_length_mismatch')
                return
            prior = self._checkpoint_by_digest(current['prior_checkpoint_digest'], scope, verify)
            if (prior['epoch_id'] != checkpoint['epoch_id'] or prior['producer_digest'] != checkpoint['producer_digest']
                    or prior['high_water'] != current['prior_high_water']):
                raise ValueError('incident_coverage_checkpoint_chain_mismatch')
            current = prior
        raise ValueError('incident_coverage_checkpoint_chain_bound_exceeded')

    def _insert(self, table, scope, body):
        self.conn.execute(f'''INSERT INTO {table}(tenant_id,agent_id,workspace_id,user_id,record_id,payload_json,payload_digest,created_at)
            VALUES(?,?,?,?,?,?,?,?)''', (*[scope[key] for key in SCOPE_FIELDS], body.get('record_id', ''), canonical(body), digest(body), body['observed_at']))

    def checkpoint(self, scope, *, producer_digest, seal, verify):
        owner = exact_scope(scope)
        def perform():
            if not schema_ready(self.conn):
                return unknown('capture_schema_or_index_not_ready')
            previous = self._row(CHECKPOINTS, owner, verify)
            now = _now()
            high = self._head()
            start = previous['high_water'] if previous else high
            if start > high:
                raise ValueError('incident_coverage_high_water_regressed')
            if previous:
                self._verify_increment(previous, owner)
                if previous['prior_checkpoint_digest']:
                    self._checkpoint_by_digest(previous['prior_checkpoint_digest'], owner, verify)
            truncated = high - start > MAX_JOURNAL_PAGE
            if not truncated:
                count = self.conn.execute(f'SELECT count(*) FROM {JOURNAL} WHERE sequence>? AND sequence<=?', (start, high)).fetchone()[0]
                if count != high - start:
                    raise ValueError('incident_coverage_incremental_sequence_gap')
            page = [] if truncated else self._page(owner, start, high)
            gap = any(row['table_name'] == 'coverage_control' for row in page)
            capacity = bool(previous and high - previous['epoch_start_sequence'] > MAX_JOURNAL_PAGE)
            checkpoint_capacity = bool(previous and previous['epoch_checkpoint_count'] >= MAX_EPOCH_CHECKPOINTS)
            continuous = bool(previous and not gap and previous['producer_digest'] == producer_digest
                and not capacity and not checkpoint_capacity and not truncated
                and 0 <= (now - instant(previous['observed_at'])).total_seconds() <= MAX_CHECKPOINT_AGE_SECONDS)
            reason = ('continued' if continuous else 'incremental_span_exceeded' if truncated else 'capture_gap' if gap else 'bounded_checkpoint_capacity_reached' if checkpoint_capacity else 'bounded_epoch_capacity_reached' if capacity else 'producer_changed' if previous and previous['producer_digest'] != producer_digest
                      else 'checkpoint_expired' if previous else 'first_actual_checkpoint')
            body = dict(schema=CHECKPOINT_SCHEMA, scope=owner, claim_domain=DOMAIN, diagnostic_only=True,
                qualifies_for_product_completion=False, all_backend_knowledge_covered=False,
                external_prior_knowledge=None, external_prior_user_reports=None,
                checkpoint_id='coverage-'+uuid4().hex, epoch_id=previous['epoch_id'] if continuous else uuid4().hex,
                epoch_started_at=previous['epoch_started_at'] if continuous else now.isoformat(),
                epoch_start_sequence=previous['epoch_start_sequence'] if continuous else high,
                observed_at=now.isoformat(), producer_digest=producer_digest,
                capture_contract_digest=digest(trigger_definitions()),
                prior_checkpoint_digest=digest(previous) if previous else '', prior_high_water=start,
                high_water=high, incremental_count=len(page), incremental_digest=digest(page),
                incremental_truncated=truncated,
                epoch_event_count=previous.get('epoch_event_count', 0) + len(page) if continuous else 0,
                epoch_checkpoint_count=previous['epoch_checkpoint_count'] + 1 if continuous else 1,
                continuity=continuous, reason=reason,
                backend_coverage={'sqlite.records': True, 'sqlite.events': True, 'postgres': None, 'external_inputs': None},
                historical_before_epoch_covered=False, unclassified_text_semantics_covered=False)
            signed = seal(body, CHECKPOINT_SCHEMA)
            if not verify(signed, CHECKPOINT_SCHEMA):
                raise ValueError('incident_coverage_checkpoint_seal_invalid')
            self._insert(CHECKPOINTS, owner, signed)
            return signed
        return self._transaction(True, perform)

    def _observation(self, owner, record_id, producer_digest, verify, expected_payload_digest=None):
        if not schema_ready(self.conn):
            return unknown('capture_schema_or_index_not_ready')
        checkpoint = self._row(CHECKPOINTS, owner, verify)
        if checkpoint is None:
            return unknown('actual_checkpoint_missing')
        self._verify_epoch(checkpoint, owner, verify)
        now = _now()
        if checkpoint['producer_digest'] != producer_digest:
            return unknown('producer_changed')
        if not 0 <= (now - instant(checkpoint['observed_at'])).total_seconds() <= MAX_CHECKPOINT_AGE_SECONDS:
            return unknown('checkpoint_expired')
        high = self._head()
        if high < checkpoint['high_water']:
            return unknown('high_water_regressed')
        if high - checkpoint['epoch_start_sequence'] > MAX_JOURNAL_PAGE:
            return unknown('bounded_epoch_capacity_exceeded')
        count = self.conn.execute(f'SELECT count(*) FROM {JOURNAL} WHERE sequence>? AND sequence<=?', (checkpoint['high_water'], high)).fetchone()[0]
        if count != high - checkpoint['high_water']:
            return unknown('incremental_sequence_gap')
        rows = self.conn.execute('SELECT * FROM records INDEXED BY idx_records_record_id WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND record_id=? LIMIT 2',
            (*[owner[key] for key in SCOPE_FIELDS], record_id)).fetchall()
        if len(rows) != 1 or rows[0]['payload_pointer_json']:
            return unknown('incident_physical_record_missing_or_opaque')
        physical = rows[0]
        if expected_payload_digest is not None and physical['payload_digest'] != expected_payload_digest:
            return unknown('native_incident_changed_after_source_check')
        if physical['kind'] != 'incident' or physical['source'] not in NATIVE_SOURCES:
            return unknown('native_incident_source_required')
        envelope = json.loads(physical['payload_json'])
        if digest(envelope) != physical['payload_digest'] or envelope.get('scope') != owner:
            return unknown('incident_physical_digest_or_scope_mismatch')
        content = envelope.get('content') or {}
        report = content.get('detector_report') or {}
        if (report.get('detector') != physical['source'] + '.v1'
                or (envelope.get('provenance') or {}).get('origin') != 'system_detector'):
            return unknown('native_detector_identity_required')
        key = incident_key(content.get('incident_class'), report.get('release_commit') or report.get('expected_commit'), content.get('diagnostic_codes'))
        if key is None:
            return unknown('typed_incident_identity_unavailable')
        # Lookup the actual INSERT version, never the caller's event timestamp.
        inserted = self.conn.execute(f'''SELECT * FROM {JOURNAL} WHERE table_name='records' AND operation='INSERT'
            AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND record_id=? AND payload_digest=?
            ORDER BY sequence LIMIT 2''', (*[owner[field] for field in SCOPE_FIELDS], record_id, physical['payload_digest'])).fetchall()
        if len(inserted) != 1 or inserted[0]['sequence'] <= checkpoint['epoch_start_sequence']:
            return unknown('incident_precedes_actual_coverage_epoch')
        detection = dict(inserted[0])
        detected_at = instant(str(report.get('detected_at') or ''))
        # Actual sequence establishes receive order. SQLite's VFS wall clock
        # may be coarser than the native detector's Python clock on Windows;
        # never turn that precision difference into a claimed ordering. Source
        # time only invalidates pre-anchor or future declarations.
        if not instant(checkpoint['epoch_started_at']) <= detected_at <= now:
            return unknown('source_time_outside_actual_coverage')
        page = self._page(owner, checkpoint['epoch_start_sequence'], high)
        reasons = []
        known = human = False
        unclassified = False
        for row in page:
            if row['table_name'] == 'coverage_control':
                reasons.append('capture_gap')
                continue
            if row['record_id'] == record_id:
                if row['operation'] != 'INSERT' or row['payload_digest'] != physical['payload_digest']:
                    reasons.append('incident_version_changed')
                continue
            try:
                source_time = instant(row['source_time'])
            except (TypeError, ValueError):
                reasons.append('unverifiable_source_time')
                continue
            if row['sequence'] > detection['sequence']:
                if source_time <= detected_at:
                    reasons.append('late_prior_input')
                continue
            if source_time < instant(checkpoint['epoch_started_at']):
                reasons.append('pre_anchor_source_received')
            try:
                prior_key = incident_key(row['incident_class'], row['release_commit'], json.loads(row['codes_json']))
            except (TypeError, ValueError):
                prior_key = None
            if prior_key is None or row['opaque_payload']:
                unclassified = True
            if prior_key == key:
                known = True
                human = human or row['human_input'] == 1 or row['origin'] == 'user_reported'
        original = envelope.get('provenance') or {}
        # Global negative declarations from legacy/native sources are never
        # promoted by this diagnostic. Preserve positive adverse declarations.
        global_facts = {field: True if original.get(field) is True else None for field in FACT_FIELDS}
        known = known or global_facts['known_before_detection'] is True
        human = human or global_facts['prior_user_reported'] is True
        if unclassified:
            reasons.append('unclassified_input_present')
        blocked = bool(reasons)
        body = dict(schema=OBSERVATION_SCHEMA, scope=owner, diagnostic_only=True, claim_domain=DOMAIN,
            qualifies_for_product_completion=False, all_backend_knowledge_covered=False,
            external_prior_knowledge=None, external_prior_user_reports=None,
            record_id=record_id, payload_digest=physical['payload_digest'], incident_key=key,
            observed_at=now.isoformat(), detected_at=report.get('detected_at'), detection_received_at=detection['observed_at'],
            journal_clock_precision_seconds=0.001,
            detection_sequence=detection['sequence'], epoch_id=checkpoint['epoch_id'], checkpoint_digest=digest(checkpoint),
            epoch_started_at=checkpoint['epoch_started_at'], epoch_start_sequence=checkpoint['epoch_start_sequence'],
            high_water=high, page_count=len(page), page_digest=digest(page), producer_digest=producer_digest,
            global_facts=global_facts, covered_known_before_detection=True if known else None if blocked else False,
            covered_prior_user_reported=True if human else None if blocked else False,
            status='unproven' if blocked else 'bounded_chronology_verified', reasons=sorted(set(reasons)),
            backend_coverage=checkpoint['backend_coverage'])
        return validate_diagnostic(body, schema=OBSERVATION_SCHEMA, scope=owner)

    def observe(self, scope, record_id, *, producer_digest, seal, verify, expected_payload_digest=None):
        owner = exact_scope(scope)
        def perform():
            body = self._observation(owner, record_id, producer_digest, verify, expected_payload_digest)
            if 'scope' not in body:
                return body
            signed = seal(body, OBSERVATION_SCHEMA)
            if not verify(signed, OBSERVATION_SCHEMA):
                raise ValueError('incident_coverage_observation_seal_invalid')
            self._insert(OBSERVATIONS, owner, signed)
            return signed
        return self._transaction(True, perform)

    def _historical_prefix(self, saved, owner, producer_digest, verify):
        """Verify the signed original range before considering any later ingress."""
        coordinates = ('epoch_start_sequence', 'high_water', 'detection_sequence', 'page_count')
        if (any(type(saved.get(key)) is not int or saved[key] < 0 for key in coordinates)
                or not saved['epoch_start_sequence'] < saved['detection_sequence'] <= saved['high_water']
                or saved['high_water'] - saved['epoch_start_sequence'] > MAX_JOURNAL_PAGE
                or saved['page_count'] > MAX_JOURNAL_PAGE):
            raise ValueError('incident_coverage_observation_prefix_coordinates_invalid')
        original = self._checkpoint_by_digest(saved['checkpoint_digest'], owner, verify)
        latest = self._row(CHECKPOINTS, owner, verify)
        if (latest is None or saved['producer_digest'] != producer_digest
                or any(row['producer_digest'] != producer_digest or row['epoch_id'] != saved['epoch_id']
                       or row['epoch_start_sequence'] != saved['epoch_start_sequence']
                       or row['epoch_started_at'] != saved['epoch_started_at']
                       or row['capture_contract_digest'] != digest(trigger_definitions())
                       for row in (original, latest))
                or original['high_water'] > saved['high_water']):
            raise ValueError('incident_coverage_observation_epoch_changed')
        low, high = saved['epoch_start_sequence'], saved['high_water']
        count = self.conn.execute(f'SELECT count(*) FROM {JOURNAL} WHERE sequence>? AND sequence<=?',
                                  (low, high)).fetchone()[0]
        page = self._page(owner, low, high)
        if count != high - low or len(page) != saved['page_count'] or digest(page) != saved['page_digest']:
            raise ValueError('incident_coverage_observation_prefix_changed')

    def _safe_later_ingress(self, saved, current, owner):
        low, high = saved['high_water'], current['high_water']
        if high < low or high - low > MAX_JOURNAL_PAGE:
            return False
        count = self.conn.execute(f'SELECT count(*) FROM {JOURNAL} WHERE sequence>? AND sequence<=?',
                                  (low, high)).fetchone()[0]
        page = self._page(owner, low, high)
        if count != high - low:
            return False
        detected_at = instant(saved['detected_at'])
        for row in page:
            if (row['table_name'] == 'coverage_control' or row['record_id'] == saved['record_id']
                    or row['human_input'] == 1 or row['origin'] == 'user_reported'
                    or instant(row['source_time']) <= detected_at):
                return False
            try:
                key = incident_key(row['incident_class'], row['release_commit'], json.loads(row['codes_json']))
            except (TypeError, ValueError):
                key = None
            if key == saved['incident_key']:
                return False
        return True

    def read_observation(self, scope, record_id, *, producer_digest, verify):
        owner = exact_scope(scope)
        def perform():
            if not schema_ready(self.conn):
                return unknown('capture_schema_or_index_not_ready')
            saved = self._row(OBSERVATIONS, owner, verify, record_id)
            if saved is None:
                return unknown('persisted_observation_missing')
            if saved['producer_digest'] != producer_digest:
                return unknown('producer_changed')
            self._historical_prefix(saved, owner, producer_digest, verify)
            current = self._observation(owner, record_id, producer_digest, verify, saved['payload_digest'])
            if 'scope' not in current:
                return current
            # A complete physical prefix and safe later ingress may preserve an
            # historical reading. Never silently substitute the new high water
            # or sign new bytes as though they belonged to the original proof.
            if not self._safe_later_ingress(saved, current, owner):
                return {**unknown('unsafe_ingress_after_observation'), 'persisted_observation_digest': digest(saved)}
            progress = {'key_id', 'signature', 'observed_at', 'high_water', 'page_count', 'page_digest', 'checkpoint_digest'}
            comparable = {key: value for key, value in saved.items() if key not in progress}
            if comparable != {key: value for key, value in current.items() if key not in progress}:
                return {**unknown('physical_chronology_changed_since_observation'), 'persisted_observation_digest': digest(saved)}
            return saved
        return self._transaction(False, perform)
