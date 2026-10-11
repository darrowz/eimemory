"""Health remains observable while storage or optional retrieval is blocked."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from types import SimpleNamespace
import urllib.request

from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer, build_health_payload
from eimemory.api.runtime import Runtime


TOKEN = "Abcdefghijklmnopqrstuvwxyz012345_-"


def _get(server, path="/health"):
    request = urllib.request.Request(
        f"http://{server.address[0]}:{server.address[1]}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=1.5) as response:
        return json.load(response)


def test_sqlite_contention_returns_failed_readiness_and_recovers(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    runtime = Runtime.create(root=tmp_path)
    entered, release = Event(), Event()

    def block_sqlite():
        entered.set()
        assert release.wait(10)
        return 1

    runtime.store.run_locked(lambda sql: sql.conn.create_function("health_block", 0, block_sqlite))
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0, auth_token=TOKEN)
    server.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            blocker = pool.submit(
                runtime.store.run_locked,
                lambda sql: sql.conn.execute("SELECT health_block()").fetchone(),
            )
            try:
                assert entered.wait(1)
                results = list(pool.map(lambda _: _get(server), range(3)))
                for health in results:
                    assert health["ok"] is False
                    assert health["checks"]["process"] is True
                    assert health["checks"]["ready"] is False
                    assert health["store"]["migration_status"] == "timeout"
                    assert health["store"]["migration_complete"] is False
                assert _get(server, "/livez")["ok"] is True
            finally:
                release.set()
                blocker.result(timeout=2)
        health = _get(server)
        assert health["ok"] is True
        assert health["store"]["migration_status"] == "available"
    finally:
        release.set()
        server.stop()
        runtime.close()


def test_stalled_optional_candidate_health_is_single_flight_and_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    runtime = Runtime.create(root=tmp_path)
    release, entered = Event(), Event()
    calls = []

    def candidate_health():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return {"enabled": True, "configured": True, "available": True, "circuit": "closed"}

    runtime.memory.recall_engine.candidate_source = SimpleNamespace(health=candidate_health)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0, auth_token=TOKEN)
    server.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            try:
                results = list(pool.map(lambda _: _get(server), range(4)))
                assert entered.is_set()
                assert len(calls) == 1
                for health in results:
                    assert health["ok"] is True  # Local storage remains available.
                    assert health["retrieval"]["health_status"] == "timeout"
                    assert health["retrieval"]["candidate_source"]["available"] is False
                    assert health["retrieval"]["candidate_source"]["last_error"] == "candidate_health_timeout"
                assert _get(server, "/livez")["ok"] is True
                assert len(calls) == 1
            finally:
                release.set()
        health = _get(server)
        assert health["retrieval"]["health_status"] == "available"
        assert health["retrieval"]["candidate_source"]["available"] is True
    finally:
        release.set()
        server.stop()
        runtime.close()


def test_health_cache_does_not_cross_runtime_instances_at_same_database(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    first = Runtime.create(root=tmp_path)
    second = Runtime.create(root=tmp_path)
    monkeypatch.setattr(first.store.sqlite, "pending_storage_migrations", lambda: [])
    monkeypatch.setattr(second.store.sqlite, "pending_storage_migrations", lambda: ["storage.test_pending"])
    try:
        assert build_health_payload(first, listen_host="127.0.0.1", listen_port=1)["store"]["migration_complete"] is True
        health = build_health_payload(second, listen_host="127.0.0.1", listen_port=1)
        assert health["store"]["migration_complete"] is False
        assert health["store"]["pending_migrations"] == ["storage.test_pending"]
    finally:
        first.close()
        second.close()


def test_failed_storage_probe_never_reports_ready(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    runtime = Runtime.create(root=tmp_path)

    def fail():
        raise RuntimeError("private database diagnostic must not be exposed")

    monkeypatch.setattr(runtime.store.sqlite, "pending_storage_migrations", fail)
    try:
        health = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=1)
        assert health["ok"] is False
        assert health["checks"]["ready"] is False
        assert health["store"]["migration_status"] == "unavailable"
        assert health["store"]["pending_migrations"] == ["storage.migration_status_unavailable"]
        assert "private database diagnostic" not in json.dumps(health)
    finally:
        runtime.close()


def test_expired_healthy_snapshot_is_not_used_during_blocked_refresh(tmp_path, monkeypatch):
    from eimemory.adapters.eibrain import health_probe

    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    runtime = Runtime.create(root=tmp_path)
    release = Event()
    try:
        assert build_health_payload(runtime, listen_host="127.0.0.1", listen_port=1)["ok"] is True
        monkeypatch.setattr(health_probe, "_FRAGMENT_TTL_SECONDS", 0)

        def blocked():
            assert release.wait(10)
            return []

        monkeypatch.setattr(runtime.store.sqlite, "pending_storage_migrations", blocked)
        health = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=1)
        assert health["ok"] is False
        assert health["checks"]["ready"] is False
        assert health["store"]["migration_status"] == "timeout"
    finally:
        release.set()
        runtime.close()


def test_liveness_identity_cannot_replace_deployment_readiness(tmp_path, monkeypatch):
    from deploy.verify_release_health import verify_health_payload
    from eimemory.version import __version__
    from pathlib import Path

    commit = "a" * 40
    monkeypatch.setenv("EIMEMORY_COMMIT", commit)
    monkeypatch.setenv("EIMEMORY_RUNTIME_COMMIT", commit)
    runtime = Runtime.create(root=tmp_path)
    try:
        healthy = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=1)
        release = Path(healthy["import_root"]).parent
        assert verify_health_payload(healthy, commit=commit, version=__version__, release_dir=release)["ok"] is True
        liveness = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=1, ready=False)
        assert liveness["ok"] is True
        assert liveness["store"]["migration_status"] == "not_checked"
        report = verify_health_payload(liveness, commit=commit, version=__version__, release_dir=release)
        assert report["ok"] is False
        assert "readiness" in report["failed_checks"]
    finally:
        runtime.close()
