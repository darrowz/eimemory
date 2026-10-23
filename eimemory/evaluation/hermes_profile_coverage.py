"""Exact logical-profile coverage, independent of the physical gateway count.

The registered topology grants identity; observed records never add a profile
or authorize a scope. Counts below are diagnostics, not quality certificates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

SCOPE_FIELDS = ("tenant_id", "agent_id", "workspace_id", "user_id")
CAPTURE_FIELDS = (*SCOPE_FIELDS, "channel", "source_id")


def load_topology():
    from eimemory.contracts.hermes_topology import load_topology as load
    return load()


def validated_topology(value):
    from eimemory.contracts.hermes_topology import validate_snapshot
    return validate_snapshot(value)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def capture_key(entry):
    if (not isinstance(entry, dict) or set(entry) != set(CAPTURE_FIELDS)
            or any(not isinstance(entry[k], str) or not entry[k]
                   or entry[k] != entry[k].strip() or "*" in entry[k]
                   for k in CAPTURE_FIELDS)):
        raise ValueError("hermes_capture_scope_invalid")
    return tuple(entry[k] for k in CAPTURE_FIELDS)


def profile_bindings(topology):
    """One entry per declared exact scope, never a guessed default user."""
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    topology = validated_topology(topology)
    bindings = []
    for profile in topology["profiles"]:
        if profile["identity_status"] != "bound":
            continue
        for capture in profile["capture_scopes"]:
            bindings.append({"profile_name": profile["profile_name"],
                "scope": resolve_channel_scope(capture["channel"], capture["scope"]),
                "channel": capture["channel"],
                "source_ids": sorted(capture["source_ids"])})
    return bindings


def required_capture_scopes(topology):
    entries = {}
    for binding in profile_bindings(topology):
        for source in binding["source_ids"]:
            entry = {**binding["scope"], "channel": binding["channel"], "source_id": source}
            entries[capture_key(entry)] = entry
    return [entries[key] for key in sorted(entries)]


def capture_coverage(allowed, topology):
    required = required_capture_scopes(topology)
    try:
        if not isinstance(allowed, list) or not 1 <= len(allowed) <= 100:
            raise ValueError("hermes_capture_policy_invalid")
        actual = [capture_key(entry) for entry in allowed]
        if len(set(actual)) != len(actual):
            raise ValueError("hermes_capture_policy_duplicate")
        missing = set(map(capture_key, required)) - set(actual)
        valid = bool(required) and not missing
    except ValueError:
        valid, missing = False, set(map(capture_key, required))
    return {"ok": valid, "required_scope_count": len(required),
        "missing_scope_count": len(missing), "required_scope_digest": _digest(required),
        "topology_digest": topology["topology_digest"]}


def request_sampling_binding(*, channel, scope, source_ids, claim=None):
    """Re-derive sampling class server-side; caller flags grant no authority.

    An explicitly registered local owner remains maintenance traffic even when
    its caller omits the profile envelope or requests natural classification.
    The envelope can only agree with an independently matched exact scope.
    """
    if str(channel).strip().lower() != 'hermes':
        if claim is not None:
            raise ValueError('hermes_profile_binding_channel_mismatch')
        return None
    topology = load_topology()
    if topology is None:
        if claim is not None:
            raise ValueError('hermes_profile_binding_not_registered')
        return None
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    if (not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS)
            or any(not isinstance(scope[key], str) or not scope[key] or '*' in scope[key]
                   for key in SCOPE_FIELDS)):
        raise ValueError('hermes_profile_request_scope_invalid')
    exact = resolve_channel_scope('hermes', scope)
    if (not isinstance(source_ids, list) or not source_ids
            or any(not isinstance(source, str) or not source or '*' in source for source in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise ValueError('hermes_profile_request_sources_invalid')
    sources = sorted(source_ids)
    matches = []
    for profile in topology['profiles']:
        operator = profile.get('operator_scope')
        if operator is not None and resolve_channel_scope('hermes', operator) == exact and sources == ['hermes']:
            matches.append((profile, 'operator'))
        if profile['identity_status'] == 'bound':
            for capture in profile['capture_scopes']:
                if (resolve_channel_scope('hermes', capture['scope']) == exact
                        and sorted(capture['source_ids']) == sources):
                    matches.append((profile, 'business'))
    if len(matches) != 1:
        raise ValueError('hermes_profile_request_scope_unbound_or_ambiguous')
    profile, kind = matches[0]
    expected = {'profile_name': profile['profile_name'], 'hermes_home': profile['hermes_home'],
        'topology_digest': topology['topology_digest'], 'binding_kind': kind,
        'natural_sample_eligible': kind == 'business'}
    if claim is not None and (not isinstance(claim, dict) or claim != expected
            or type(claim.get('natural_sample_eligible')) is not bool):
        raise ValueError('hermes_profile_request_binding_mismatch')
    return expected


def roster_summary(topology):
    topology = validated_topology(topology)
    return {"schema": "hermes_logical_profile_inventory.v1",
        "topology_digest": topology["topology_digest"],
        "physical_gateway_count": 1, "runtime_units": list(topology["runtime_units"]),
        "logical_profile_count": len(topology["profiles"]),
        "profiles": [{"profile_name": row["profile_name"], "agent_id": row["agent_id"],
            "identity_status": row["identity_status"],
            "authorized_scope_count": len(row["capture_scopes"])} for row in topology["profiles"]]}


def policy_covers_topology(policy, topology):
    """Cover all bound business scopes; operator-only profiles grant no credit."""
    topology = validated_topology(topology)
    required = {capture_key(row) for row in required_capture_scopes(topology)}
    actual = set()
    for row in policy["agents"]:
        for source in row["source_ids"]:
            actual.add(capture_key({**row["scope"], "channel": "hermes", "source_id": source}))
    if not required or actual != required:
        raise ValueError("hermes_business_scope_inventory_mismatch")
    return True


def natural_profile_scopes(runtime, topology, *, tenant_id, window_days=14):
    """G2c: return only authorized scopes that already have natural traffic.

    Declared profiles with zero natural decisions stay visible in sampling
    diagnostics and roster inventory, but are not forced through the recall
    gate until they produce traffic.
    """
    from eimemory.adapters.runtime.channel import base_scope_from_channel
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    bindings = [row for row in profile_bindings(topology) if row["scope"]["tenant_id"] == tenant_id]
    def read(sqlite):
        results = []
        for binding in bindings:
            scope = binding["scope"]
            count = sqlite.execute(
                "SELECT COUNT(*) FROM proactive_decisions WHERE tenant_id=? AND agent_id=? "
                "AND workspace_id=? AND user_id=? AND channel=? AND created_at>=? "
                "AND acceptance_generated=0 AND session_id NOT LIKE 'hermes-release-%' "
                "AND session_id NOT LIKE 'cron\\_%' ESCAPE '\\' "
                "AND json_valid(source_ids_json) AND json_array_length(source_ids_json)=? "
                "AND NOT EXISTS (SELECT 1 FROM json_each(source_ids_json) WHERE value NOT IN ("
                + ",".join("?" for _ in binding["source_ids"]) + "))",
                (*[scope[k] for k in SCOPE_FIELDS], binding["channel"], since,
                 len(binding["source_ids"]), *binding["source_ids"])).fetchone()[0]
            base = base_scope_from_channel(binding["channel"], scope)
            results.append({**base, "profile_name": binding["profile_name"],
                "channel": binding["channel"], "source_ids": binding["source_ids"],
                "scope_digest": _digest(scope), "discovered_decisions": int(count),
                "source": "trusted_hermes_topology", "topology_digest": topology["topology_digest"]})
        # Force list = traffic only (G2c). Zero-traffic profiles remain in diagnostics.
        return [row for row in results if int(row.get("discovered_decisions") or 0) > 0]
    return runtime.store.run_locked(read)


def profile_sampling_diagnostics(runtime, topology, *, tenant_id, window_days=14):
    """Content-free stored evidence counts; never label or terminal authority."""
    topology = validated_topology(topology)
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    bindings = [row for row in profile_bindings(topology) if row['scope']['tenant_id'] == tenant_id]
    capture_codes = {'captured', 'disabled', 'scope_not_enabled', 'scope_policy_invalid',
        'input_bounds_rejected', 'decision_identity_mismatch', 'host_input_identity_mismatch',
        'capture_unavailable'}
    record_sources = {'explicit_capture_count': 'eimemory.production_recall.explicit_capture',
        'pending_case_count': 'eimemory.production_recall.pending_case',
        'accepted_case_count': 'eimemory.production_recall.accepted_case',
        'semantic_observation_count': 'eimemory.semantic_relevance_monitor'}
    def read(sqlite):
        tables = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        results = []
        for binding in bindings:
            scope, sources = binding['scope'], binding['source_ids']
            args = tuple(scope[k] for k in SCOPE_FIELDS)
            where = ('tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? '
                "AND channel=? AND created_at>=? AND acceptance_generated=0 "
                "AND session_id NOT LIKE 'hermes-release-%' "
                "AND session_id NOT LIKE 'cron\\_%' ESCAPE '\\' "
                'AND json_valid(source_ids_json) AND json_array_length(source_ids_json)=? '
                'AND NOT EXISTS (SELECT 1 FROM json_each(source_ids_json) WHERE value NOT IN ('
                + ','.join('?' for _ in sources) + '))')
            params = (*args, binding['channel'], since, len(sources), *sources)
            counts = sqlite.execute('SELECT COUNT(*),COALESCE(SUM(release_bound=1),0),'
                'COALESCE(SUM(terminal=1),0),COALESCE(SUM(terminal=1 AND outcome_verified=1),0) '
                'FROM proactive_decisions WHERE ' + where, params).fetchone()
            row = {'profile_name': binding['profile_name'], 'agent_id': scope['agent_id'],
                'scope_digest': _digest(scope), 'source_ids': sources, 'status': 'reported',
                **dict(zip(('natural_decision_count', 'stored_release_bound_count',
                            'stored_terminal_count', 'stored_verified_terminal_count'), map(int, counts)))}
            selected = 'SELECT decision_id FROM proactive_decisions WHERE ' + where
            reasons = {}
            if 'proactive_query_capture_status' in tables:
                for payload, count in sqlite.execute(
                        'SELECT json_extract(payload,\'$.status\'),COUNT(*) '
                        'FROM proactive_query_capture_status WHERE json_valid(payload) '
                        'AND decision_id IN (' + selected + ') GROUP BY json_extract(payload,\'$.status\')',
                        params).fetchall():
                    code = payload if payload in capture_codes else 'unknown_capture_status'
                    reasons[code] = reasons.get(code, 0) + int(count)
            row['capture_status_counts'] = reasons
            row['private_input_count'] = int(sqlite.execute(
                'SELECT COUNT(*) FROM proactive_query_input_vault WHERE decision_id IN (' + selected + ')',
                params).fetchone()[0]) if 'proactive_query_input_vault' in tables else 0
            for label, source in record_sources.items():
                packet_sources = sources
                if label == 'explicit_capture_count':
                    array = "json_extract(payload_json,'$.content.request.source_ids')"
                elif label == 'semantic_observation_count':
                    array = "json_extract(payload_json,'$.content.source_digests')"
                    packet_sources = [_digest(value) for value in sources]
                else:
                    array = None
                source_predicate = ('source_id IN (' + ','.join('?' for _ in packet_sources) + ')'
                    if array is None else 'json_valid(payload_json) AND json_array_length(' + array + ')=? '
                    'AND NOT EXISTS (SELECT 1 FROM json_each(' + array + ') WHERE value NOT IN ('
                    + ','.join('?' for _ in packet_sources) + '))')
                source_params = packet_sources if array is None else [len(packet_sources), *packet_sources]
                row[label] = int(sqlite.execute('SELECT COUNT(*) FROM records WHERE '
                    'tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? '
                    "AND source=? AND kind='evaluation_packet' AND status='active' AND "
                    + source_predicate, (*args, source, *source_params)).fetchone()[0])
            results.append(row)
        return results
    try:
        rows = runtime.store.run_locked(read)
        status = 'reported'
    except Exception:
        rows, status = [], 'unavailable'
    return {'schema': 'hermes_profile_sampling_diagnostics.v1', 'status': status,
        'inventory': roster_summary(topology), 'window_days': window_days,
        'formal_acceptance': False, 'l5_credit': False,
        'counts_semantics': 'stored_metadata_counts_not_verified_quality', 'scopes': rows}
