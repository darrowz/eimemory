# API, intake and knowledge source delta · 2026-10-02 06:27 UTC

This sealed update adds 901 independently paired baseline source functions in 46 files: API 298, intake 323, and knowledge 280. Result: 2,130 / 7,140 (29.83%). The [compact summary](../../../../checkpoint-0627.md) is already published.

Base: the [1,229-function update](../0611/README.md), full manifest SHA 076f70c33194a8315b23dc8209bdb3db3899d75823e7b857e58035881a752153. Concatenate only the 27 segments in [delta-shards/index.json](delta-shards/index.json), verify the 1,586,687-byte delta hash, then follow its version 2 reassembly instructions. The resulting full canonical JSON must hash to 63a7e1b65ed5cae235f2606305ee7bf85539bf9f7e86d4603ac3b3852969cc52.

The 106 existing function updates retain original first-pass observation IDs and reconcile central finding bindings. They modify only pass1_observation_ids and findings fields; source bytes, ranges, graph IDs and first/second-pass states remain unchanged. Six new normalized evidence files share the 0500 package root; the 42 inherited evidence hashes are unchanged. [Evidence index](evidence-index.json)

Publication is in progress until publication-receipt.json exists here. The summary and this index alone do not establish that all segments and evidence are durable. Static source counts and their stated semantic limits remain separate from runtime-test results.
