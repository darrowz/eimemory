from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from eimemory.autonomous.exp_log import ExpLog
from eimemory.autonomous.runner import run_karpathy_iteration


def _write_profile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[autonomy]\nprofile = learning\n", encoding="utf-8")


def test_runner_generates_real_hypothesis_and_appends_exp_log(tmp_path: Path) -> None:
    profile_ini = tmp_path / "config" / "eimemory.ini"
    audit_path = tmp_path / "state" / "audit.jsonl"
    records_path = tmp_path / "records.jsonl"
    exp_log_path = tmp_path / "exp_log" / "experiments.jsonl"
    _write_profile(profile_ini)
    now = datetime.now(timezone.utc).isoformat()
    records_path.write_text(
        json.dumps(
            {
                "kind": "weakness",
                "record_id": "w1",
                "content": {"summary": "LongMemEval recall misses target turns"},
                "time": {"occurred_at": now},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_karpathy_iteration(
        profile_ini=profile_ini,
        audit_path=audit_path,
        records_path=records_path,
        exp_log_path=exp_log_path,
        experiment_id="kl-test-1",
        time_box_seconds=30.0,
    )

    assert result["experiment_id"] == "kl-test-1"
    assert result["outcome"] in {"kept", "discarded"}
    assert "cron_warmup" not in json.dumps(result)
    entries = ExpLog(exp_log_path).read_all()
    assert len(entries) == 1
    assert entries[0].experiment_id == "kl-test-1"
    assert "recall" in entries[0].hypothesis.lower()
