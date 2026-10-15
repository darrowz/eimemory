from pathlib import Path

from eimemory.adapters.hermes.code_implementation import (
    FIXED_COMPLETION_TIMEOUT_SECONDS, CodeImplementationError, CodeImplementationSocketClient,
)
from eimemory.evaluation.hongtu_code_implementation import run_code_implementation_catalog_pass


def test_socket_client_honours_budget_plus_headroom():
    assert CodeImplementationSocketClient(timeout_seconds=125.0).timeout_seconds == 125.0
    assert CodeImplementationSocketClient(timeout_seconds=999).timeout_seconds == FIXED_COMPLETION_TIMEOUT_SECONDS + 10.0


class _Raising:
    def __init__(self, exc):
        self.exc = exc

    def propose_patch_v2(self, request):
        raise self.exc


def _run(tmp_path: Path, exc):
    (tmp_path / "fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
    return run_code_implementation_catalog_pass(
        provider=_Raising(exc), fixture_root=tmp_path, fixture_files=("fixture.py",),
        evaluator=lambda root, updates: True,
    )


def test_reason_code_surfaced(tmp_path):
    result = _run(tmp_path, CodeImplementationError("provider_transport_unavailable"))
    assert result["reason"] == "catalog_pass_failed:CodeImplementationError:provider_transport_unavailable"
    assert result["provider_invoked"] is True


def test_free_form_message_not_echoed(tmp_path):
    result = _run(tmp_path, CodeImplementationError("secret path /x y"))
    assert result["reason"] == "catalog_pass_failed:CodeImplementationError"
