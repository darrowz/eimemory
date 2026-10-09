Verbatim `agent/memory_manager.py` from https://github.com/NousResearch/hermes-agent (MIT),
used to test `deploy/ensure_hermes_sync_snapshot.py` against real host code:

- `memory_manager_2ef41d2b.py.txt` — 2ef41d2b (v0.21.5+2084, installed on honrui; legacy `messages` seam)
- `memory_manager_v0.21.6.py.txt` — tag v0.21.6 (818c13be; `redacted_messages` seam, upstream #115104)
- `memory_manager_main_e0550c97.py.txt` — main at e0550c97 (2026-10-09; `redacted_messages` seam)

Stored as `.txt` so pytest/importers never execute them.
