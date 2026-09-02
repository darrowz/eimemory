from pathlib import Path


def test_rpc_logs_do_not_depend_on_an_adapter_home():
    source = Path("deploy/systemd/eimemory-rpc.service").read_text(encoding="utf-8")
    assert "StandardOutput=journal" in source and "StandardError=journal" in source
    assert ".openclaw" not in source


def test_core_closure_units_do_not_start_optional_gateway():
    for name in ("eimemory-release-closure.service", "eimemory-l5-effect-review.service"):
        source = Path("deploy/systemd", name).read_text(encoding="utf-8")
        assert "openclaw-gateway" not in source
        assert "After=eimemory-rpc.service" in source

    path_source = Path("deploy/systemd/eimemory-release-closure.path").read_text(
        encoding="utf-8"
    )
    assert "openclaw-gateway" not in path_source
    assert "After=eimemory-rpc.service" not in path_source


def test_core_reports_use_core_storage():
    dashboard = Path("deploy/systemd/eimemory-learn-dashboard.service").read_text(encoding="utf-8")
    review = Path("deploy/systemd/eimemory-l5-effect-review.sh").read_text(encoding="utf-8")
    assert "--output /var/lib/eimemory/reports/" in dashboard
    assert "$EIMEMORY_ROOT/reports/l5-48h-effect.json" in review
    assert ".openclaw/reports" not in dashboard + review


def test_discovery_does_not_invent_absent_adapter_units():
    source = Path("deploy/discover_python_runtime_units.sh").read_text(encoding="utf-8")
    baseline = source.split("BASE_UNITS=(", 1)[1].split(")", 1)[0]
    assert "openclaw" not in baseline
    assert "eimemory-rpc.service" in baseline
    assert "-name '*.service'" in source and "grep -Fq '/opt/eimemory/current'" in source
