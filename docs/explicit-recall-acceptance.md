# Explicit recall acceptance

Codex MCP `eimemory_recall` now sends the MCP process session and JSON-RPC request ID to `adapter.prefetch`. Set `acceptance_generated: true` for deliberate functional acceptance calls. The server stores an explicit intent before querying, then attests its actual rendered result using the existing evidence receipt keyring. It returns `capture.record_id` and `capture.result_digest`.

The capture contains the original request, explicit/acceptance classification, release identity and independently verified receipt reference, delivered record references with their physical scopes and sources, and a result digest. The reader verifies both signatures, intent identity, receipt authority, exact source references and current record payload digests. It applies the existing default retrieval alias/shared visibility rules; it grants no additional read access. No proactive decision is created.

Repeated completed requests in the same MCP session return the original result. Reusing an ID for a changed request fails. Recall exceptions produce a failed capture containing the exception class, without private exception text. If signing, receipt verification, or persistence fails after the intent is stored, that request remains incomplete and fails closed on retry. Audit that failure before taking further action; there is no automatic replay or recovery protocol.

The existing production-query collector exposes these records under `explicit`, separately from proactive pending IDs. Operators can also use:

```sh
eimemory eval production-query explicit-collect --scope-agent AGENT --scope-workspace WORKSPACE --scope-user USER
eimemory eval production-query explicit-accept CAPTURE_ID --label-json /private/labels.json --scope-agent AGENT --scope-workspace WORKSPACE --scope-user USER
eimemory eval production-query explicit-eval --labels-json /private/manifest.json --persist-report --output /private/acceptance.json --scope-agent AGENT --scope-workspace WORKSPACE --scope-user USER
```

Both input files use the existing secure dataset loader. A label packet has only `labeler: "operator"` and `labels`; each label provides `record_ref`, the record's real `scope`, `source_id`, and a grade from 1 to 3. The manifest has only `label_record_ids`, referencing accepted label records. An authorized durable answer may be labelled even when the original result missed it. Raw episodes, diagnostics and default-suppressed records cannot become gold. The evaluation scores the captured result, including empty results and misses, without replacing it with a later successful query.

Reports are `production_recall_explicit_acceptance.v1`, with `gate_status: "acceptance_only"` and `natural_benchmark_eligible: false`. They retain each request's generation flag and release identity. Unbound local captures are visible in `release_bound_sample_count`; they are not production release evidence. Duplicate captures cannot increase the sample count. These reports do not qualify natural proactive coverage, establish historical pre-release anchors, or delegate base data authority to a different exact release scope.
