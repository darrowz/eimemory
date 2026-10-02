# Focused regression evidence

- [Latest published-tree checkpoint](checkpoint-ced3d648.json): commit ced3d648c1b272f51c2a17da68ad417b85db2b76, 156 Python AST methods across 18 frozen runners. Implementation validation executed the tests; independent review corroborated logs, source/runner hashes, exact HEAD and clean tree without a second execution.
- [Independent 123-method execution](checkpoint-54b092a6.json): exact commit 54b092a62fed652833163752ad404a532da0c0ce.
- [Independent 85 Python methods and 364 JavaScript byte cases](checkpoint-6167237d.json): exact commit 6167237d7c776ee52d5c4aa99d0bf443d98da062, with the two languages counted separately.
- [Earlier published 94-method checkpoint](checkpoint-6832404e.json) and [batch021 before/after review](review-021.json) remain available.

Individual normalized before/after evidence is in [../patch-evidence](../patch-evidence/), with older batches in their numbered directories. [Audit ledger](../../audit-ledger.json) maps every published repair to its exact code commit and evidence.

The older 35 runtime methods were not rerun on the latest tree. The 191-method Python inventory spans separate checkpoints and is not a combined191 pass. Repeated runs add no cases. These source/hash-gated synthetic checks do not establish real database, gateway, thread-resource, model/network, state/identity/evidence/admission or production behavior. No full-suite or CI acceptance is claimed.

Every normalized log has its recalculated public hash, with original sealed fingerprints recorded separately. Source and test assertions, counts and exits are unchanged by evidence normalization.
