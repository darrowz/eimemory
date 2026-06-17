"""Hourly audit chain verifier. On break, calls kill switch.

The verifier walks the on-disk audit log and re-derives every row's
sha256 hash.  If the chain is intact, it is a no-op.  If the chain is
broken (a row's ``prev_hash`` no longer matches the previous row's
``row_hash``, or its own ``row_hash`` no longer matches the body), the
verifier appends a forensic record to the same log and calls
:func:`emergency_stop` so the offending eimemory process is killed
before further autonomous action can land.

The append-on-break step is best-effort: an audit append failure
(e.g. disk full) must not block the kill switch, so the append is
wrapped in a try/except.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from eimemory.governance.safety.audit import AuditLog, ChainBroken
from eimemory.governance.safety.kill_switch import emergency_stop, _audit_path


class AuditVerifier:
    """Read-only wrapper that runs :meth:`AuditLog.verify` and reacts on break."""

    def __init__(self, *, log_path: Path) -> None:
        self.log_path = Path(log_path)

    def verify_once(self) -> None:
        """Verify the chain once.  On break, log + emergency_stop."""
        log = AuditLog(self.log_path)
        try:
            log.verify()
        except ChainBroken as exc:
            self._record_break(exc)
            # Kill switch first; never let an audit append failure block the kill.
            emergency_stop()

    def _record_break(self, exc: ChainBroken) -> None:
        """Best-effort: append a forensic record of the break to the same log."""
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "audit_chain_broken",
            "row_index": exc.row_index,
            "reason": exc.reason,
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            # Audit append failure is non-fatal; the kill switch still fires.
            pass


def main() -> None:
    """CLI entry point: run verify_once on the resolved audit log."""
    env_path = os.environ.get("EIMEMORY_AUDIT_PATH")
    log_path = Path(env_path) if env_path else _audit_path()
    AuditVerifier(log_path=log_path).verify_once()


if __name__ == "__main__":
    sys.exit(main())
