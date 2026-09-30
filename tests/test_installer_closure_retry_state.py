"""The installer may only say 'pending retry' when a retry is really queued."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

SCRIPT = Path("deploy/install_immutable_release.sh")
COMMIT = "b" * 40


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    return name + "() {" + text.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def _retry_state(tmp_path: Path, payload: object | None, *, raw: str | None = None) -> str:
    pending = tmp_path / "release-closure-pending.json"
    if raw is not None:
        pending.write_text(raw, encoding="utf-8")
    elif payload is not None:
        pending.write_text(json.dumps(payload), encoding="utf-8")
    script = (
        "set -euo pipefail\n"
        + _function("_release_closure_retry_state")
        + "_release_closure_retry_state\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHON_BIN": sys.executable,
            "EIMEMORY_ROOT": str(tmp_path / "unused-root"),
            "EIMEMORY_RELEASE_CLOSURE_PENDING_PATH": str(pending),
            "COMMIT": COMMIT,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _checkpoint(commit: str = COMMIT) -> dict:
    return {
        "schema_version": "release_closure_pending.v1",
        "status": "waiting_for_channel_acceptance",
        "current_commit": commit,
    }


def test_retry_state_is_queued_only_for_a_checkpoint_of_this_commit(tmp_path) -> None:
    assert _retry_state(tmp_path, _checkpoint()) == "queued"


def test_missing_checkpoint_is_not_queued(tmp_path) -> None:
    # honrui 1.14.14: closure_rehearsal block, no checkpoint was ever written.
    assert _retry_state(tmp_path, None) == "not_queued"


def test_other_commit_or_status_is_not_queued(tmp_path) -> None:
    assert _retry_state(tmp_path, _checkpoint("c" * 40)) == "not_queued"
    assert _retry_state(tmp_path, {**_checkpoint(), "status": "done"}) == "not_queued"


def test_unreadable_checkpoint_is_reported_invalid(tmp_path) -> None:
    assert _retry_state(tmp_path, None, raw="{not json") == "invalid"


def test_pending_retry_warning_is_guarded_by_queued_checkpoint() -> None:
    validation = _function("_run_post_deploy_validation")
    closure = validation.split("_run_post_switch_closure", 1)[1]
    guard = closure.index('if [ "$closure_retry" = "queued" ]; then')
    warning = closure.index("warning: post-deploy business closure is pending retry")
    honest = closure.index("no automatic retry is queued")
    assert 'closure_retry="$(_release_closure_retry_state)"' in closure
    assert 'echo "release_closure_retry=$closure_retry"' in closure
    assert guard < warning < honest
    # Degraded status and exit semantics are unchanged.
    assert "degraded=1" in closure
    assert 'echo "post_deploy_validation=degraded"' in validation


def test_closure_block_fields_are_captured_from_the_summary() -> None:
    closure = _function("_run_post_switch_closure")
    assert 'CLOSURE_BLOCKED_STAGE="${closure_block%% *}"' in closure
    assert 'CLOSURE_BLOCKED_REASON="${closure_block#* }"' in closure
