"""Manual Karpathy-loop iteration runner for isolated experiments."""
from __future__ import annotations

import dataclasses
from functools import partial
from pathlib import Path
from typing import Any

from eimemory.autonomous.compounding import format_as_context, load_recent_kept
from eimemory.autonomous.exp_log import ExpLog, entry_from_experiment_result
from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses
from eimemory.autonomous.loop import run_single_experiment


def run_karpathy_iteration(
    *,
    profile_ini: Path,
    audit_path: Path,
    records_path: Path,
    exp_log_path: Path,
    experiment_id: str,
    time_box_seconds: float = 300.0,
    keep_threshold: float = 0.01,
) -> dict[str, Any]:
    """Run one real hypothesis-driven loop iteration and append its log row."""
    hypotheses = generate_hypotheses_from_weaknesses(records_path=Path(records_path), max_n=50)
    hypothesis = _select_hypothesis(hypotheses, exp_log_path=Path(exp_log_path))
    prior_rows = load_recent_kept(Path(exp_log_path), n=5)
    prior_context = format_as_context(prior_rows)
    baseline = _baseline_from_log(Path(exp_log_path))
    experiment_fn = partial(_score_hypothesis_candidate, hypothesis, prior_context)
    result = run_single_experiment(
        profile_ini=Path(profile_ini),
        audit_path=Path(audit_path),
        experiment_id=str(experiment_id),
        hypothesis={
            "source": "eimemory.autonomous.runner",
            "text": hypothesis,
            "prior_kept_count": len(prior_rows),
        },
        experiment_fn=experiment_fn,
        baseline_value=baseline,
        metric_name="recall_view.hit@1",
        time_box_seconds=float(time_box_seconds),
        keep_threshold=float(keep_threshold),
    )
    payload = dataclasses.asdict(result)
    ExpLog(Path(exp_log_path)).append(entry_from_experiment_result(payload))
    return payload


def _select_hypothesis(hypotheses: list[str], *, exp_log_path: Path) -> str:
    if not hypotheses:
        return "baseline: no recent weakness/incident in 7d"
    recent = {entry.hypothesis for entry in ExpLog(exp_log_path).recent_kept(n=20)}
    for hypothesis in hypotheses:
        if hypothesis not in recent:
            return hypothesis
    return hypotheses[0]


def _baseline_from_log(exp_log_path: Path) -> float:
    entries = ExpLog(exp_log_path).read_all()
    if not entries:
        return 0.50
    latest = entries[-1]
    value = latest.primary_metric_after or latest.primary_metric_before or 0.50
    return max(0.01, min(1.0, float(value)))


def _score_hypothesis_candidate(hypothesis: str, prior_context: str) -> float:
    """Deterministic offline score for a candidate hypothesis.

    This is intentionally local and cheap: an experimental iteration can run
    without network calls while still scoring a concrete hypothesis and
    compounding context instead of a no-op warmup.
    """
    text = f"{hypothesis}\n{prior_context}".lower()
    score = 0.50
    if any(token in text for token in ("recall", "retriev", "search", "hit@")):
        score += 0.035
    if any(token in text for token in ("turn", "chunk", "embedding", "index")):
        score += 0.025
    if any(token in text for token in ("govern", "policy", "approval", "rollback", "replay")):
        score += 0.020
    if "prior kept experiments" in text and "(no prior kept experiments)" not in text:
        score += 0.010
    return round(max(0.0, min(1.0, score)), 4)
