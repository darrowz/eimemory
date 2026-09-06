from pathlib import Path


def test_fused_closure_script_keeps_memory_plane_before_l5_gates() -> None:
    text = Path("deploy/run_memory_l5_fused_closure.sh").read_text(encoding="utf-8")
    assert "stage=memory_plane_eval" in text
    assert "eval production-query collect" in text
    assert "eval production-query status" in text
    assert "learn release-closure" in text
    assert text.index("stage=memory_plane_eval") < text.index("eval production-query collect")
    assert text.index("eval production-query status") < text.index("learn release-closure")
    assert "install_immutable_release.sh" not in text
    assert "production_query_not_ready" in text
