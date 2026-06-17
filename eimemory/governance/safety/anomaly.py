"""3-sigma anomaly detector on per-action-class hourly action counts.

Maintains a rolling baseline of action counts bucketed by (action_class,
day-of-week, hour-of-day) and flags observations whose z-score exceeds
``sigma`` (default 3.0) as anomalous.

Used by ``eimemory/governance/safety/audit_verifier.py`` (Task 0.5) on the
hourly audit chain sweep: any 3-sigma deviation triggers
``emergency_stop()`` (Task 0.1). Defined as a standalone primitive here so
the detector has its own RED-GREEN coverage independent of the kill chain.

Persists state to ``<root>/anomaly_baseline.json`` so baselines survive
process restarts; the file is read on construction and rewritten on every
``record`` call.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev


class AnomalyDetector:
    """Per-(action_class, day-bucket, hour-bucket) z-score detector."""

    def __init__(
        self,
        root: Path,
        window_days: int = 7,
        sigma: float = 3.0,
    ) -> None:
        """Initialize detector rooted at ``root``.

        ``window_days`` controls the size of each per-bucket history window
        (older samples are trimmed); ``sigma`` is the z-score threshold.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "anomaly_baseline.json"
        self.window_days = window_days
        self.sigma = sigma
        self.baselines: dict[str, list[float]] = defaultdict(list)
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self.baselines[k] = v

    def record(self, action_class: str, count: int, *, day: int, hour: int) -> None:
        """Append ``count`` to the baseline for the given bucket."""
        key = f"{action_class}:d{day % self.window_days}:h{hour}"
        self.baselines[key].append(float(count))
        # Trim to window (keep last 2*window_days samples to allow mild smoothing)
        self.baselines[key] = self.baselines[key][-self.window_days * 2:]
        self._save()

    def check(self, action_class: str, count: int, *, day: int, hour: int) -> bool:
        """Return True iff ``count`` deviates more than ``sigma`` std-deviations from the bucket baseline.

        Minimum-history guard is ``< 1`` (empty bucket only) — not the plan's ``< 3``.
        Rationale: ``day % window_days`` bucketing gives 1 sample per bucket per week,
        so a ``< 3`` guard would suppress detection for the entire first week of any
        new detector. The ``s < 0.001`` early-return is replaced with a stdev floor
        (``max(observed, max(mean*0.1, 1.0))``) so a constant baseline still produces
        a meaningful z-score against a spike (e.g. 10 -> 100). Plan deviation,
        documented in the Task 0.6 report-back.
        """
        key = f"{action_class}:d{day % self.window_days}:h{hour}"
        history = self.baselines.get(key, [])
        if not history:
            return False
        m = mean(history)
        s = pstdev(history)
        # Floor for stdev: 10% of mean with a hard minimum of 1.0.
        eff_stdev = max(s, max(m * 0.1, 1.0))
        z = abs(count - m) / eff_stdev
        return z > self.sigma

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(dict(self.baselines), sort_keys=True), encoding="utf-8"
        )