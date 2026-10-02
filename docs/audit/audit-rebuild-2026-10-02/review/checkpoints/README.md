# Focused regression evidence

The focused checks passed within their recorded synthetic test boundaries. This package keeps independent test execution distinct from read-only corroboration of a later published-head run.

## Checkpoints

- At commit **6167237d7c776ee52d5c4aa99d0bf443d98da062**, independent review execution passed **85 Python AST methods** and **364 parameterized JavaScript byte cases**. The tree stayed clean and approved source/runner hashes were stable. [Complete evidence](checkpoint-6167237d.json)
- At published commit **6832404ef2f31d7440b77dea6362d052007f48a8**, implementation validation passed **94 Python AST methods**. Independent review then checked the manifest/log hashes, approved source/runner bytes, actual commit and clean tree without rerunning those tests. [Complete evidence](checkpoint-6832404e.json)
- Batch **021** received independent before/after review: three failing methods with eight KeyError records before the patch, then nine new methods plus 85 prior AST methods passing on the prepared tree. Only the optional stored-score hint completeness guard changed. [Approval and embedded logs](review-021.json)

## Scope and counting

Python test methods and JavaScript parameterized byte cases are different units and are not added together. Repeated validation runs do not increase either count. The 35 earlier runtime methods were excluded; the cumulative 129-method Python inventory spans separate checkpoints and is not a 129-test combined run.

These are source/hash-gated fake-only checks. No actual database, gateway, thread resource, SDK/model/network, environment/credential accessor, identity/admission or archive validator was exercised. No full-suite, integration, deployment or production validation is claimed. The held circuit-ownership candidate remains unresolved.

## Evidence normalization

Only repository-relative paths and neutral validation labels appear here. Every embedded log stores its original SHA-256 separately from the recalculated normalized-text SHA-256. Original manifest/verdict hashes are preserved as provenance. The sealed original artifacts were not edited. Normalization does not change test assertions, counts, exits or results.
