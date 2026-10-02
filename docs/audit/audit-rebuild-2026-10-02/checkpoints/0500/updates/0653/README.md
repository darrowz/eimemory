# Adapters and contracts source delta · 2026-10-02 06:53 UTC

This sealed update adds 632 paired baseline source functions in 39 files: adapters 535 and contracts 97. Result: 2,762 / 7,140 (38.68%).

Base: the [2,130-function update](../0627/README.md), full manifest SHA 63a7e1b65ed5cae235f2606305ee7bf85539bf9f7e86d4603ac3b3852969cc52. Concatenate only the 16 segments in [delta-shards/index.json](delta-shards/index.json), verify the 943,992-byte delta and its SHA-256, then parse it. Reassembly: load the base manifest; apply root_updates; find each function_updates entry by file path and graph_node_id and apply its set fields; append new_files in listed order. Serialize UTF-8 JSON with ensure_ascii=false, comma/colon separators and a final newline. The result must hash to e000db29197227edb6d7f8161719f3ede9f920a904c841d6baa3dd024f8d2c19.

The nine existing-function updates change only finding associations. Source/range/node/pass fields and all 48 inherited evidence hashes remain unchanged. Five new normalized evidence files share the 0500 package root. [Evidence index](evidence-index.json)

All 16 segments and five new evidence files are durable and verified. The [publication receipt](publication-receipt.json) records final remote object and exact chain-reconstruction checks. Source review and its semantic limits remain separate from runtime-test outcomes.
