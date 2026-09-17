"""Production-blocking regression tests for eimemory 1.13.14.

Covers:
1. Deploy health probe loopback allow on safe_urlopen
2. memory_quality_report NameError (missing business_metadata import)
3. Diagnostic recall missing evolution-artifact (replay_result) records
"""

from __future__ import annotations

import inspect
from io import BytesIO
from pathlib import Path
import pytest

from eimemory.api.evolution import EvolutionAPI
from eimemory.api.runtime import Runtime
from eimemory.governance.deployment_receipt import _fetch_health
from eimemory.intake.safe_transport import UnsafeURL, safe_urlopen
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


# ---------------------------------------------------------------------------
# Bug 1 — deploy health probe / allow_loopback
# ---------------------------------------------------------------------------


def test_safe_urlopen_rejects_loopback_by_default() -> None:
    with pytest.raises(UnsafeURL, match="private|unsafe"):
        safe_urlopen("http://127.0.0.1:8091/health", timeout=1, max_redirects=0)


def test_safe_urlopen_allow_loopback_passes_address_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """With allow_loopback=True, 127.0.0.1 is not rejected at address policy.

    Connect/read are mocked so we only assert the opt-in policy path.
    """

    class _FakeSocket:
        def __init__(self) -> None:
            self.sent = b""
            self.closed = False

        def sendall(self, payload: bytes) -> None:
            self.sent += payload

        def makefile(self, *_args, **_kwargs):
            return BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 15\r\n\r\n{\"status\":\"ok\"}"
            )

        def getpeername(self):
            return ("127.0.0.1", 8091)

        def close(self) -> None:
            self.closed = True

    sock = _FakeSocket()
    connected: list[tuple[str, int]] = []

    def fake_create_connection(address, *_args, **_kwargs):
        connected.append(address)
        return sock

    monkeypatch.setattr("socket.create_connection", fake_create_connection)

    with safe_urlopen(
        "http://127.0.0.1:8091/health",
        timeout=1,
        max_redirects=0,
        allow_loopback=True,
    ) as response:
        assert response.read() == b'{"status":"ok"}'
        assert response.peer_ip == "127.0.0.1"

    assert connected == [("127.0.0.1", 8091)]


def test_safe_urlopen_allow_loopback_still_rejects_private_non_loopback() -> None:
    with pytest.raises(UnsafeURL, match="private|unsafe"):
        safe_urlopen("http://10.0.0.5/health", timeout=1, max_redirects=0, allow_loopback=True)


def test_fetch_health_passes_allow_loopback() -> None:
    source = inspect.getsource(_fetch_health)
    assert "allow_loopback=True" in source
    assert "max_redirects=0" in source


def test_fetch_health_uses_allow_loopback_for_default_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Resp:
        headers = {"Content-Length": "15"}

        def geturl(self) -> str:
            return "http://127.0.0.1:8091/health"

        def read(self, _n: int = -1) -> bytes:
            return b'{"status":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_safe_urlopen(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(
        "eimemory.governance.deployment_receipt.safe_urlopen",
        fake_safe_urlopen,
    )
    payload = _fetch_health("http://127.0.0.1:8091/health")
    assert payload == {"status": "ok"}
    assert captured.get("allow_loopback") is True
    assert captured.get("max_redirects") == 0


# ---------------------------------------------------------------------------
# Bug 2 — memory_quality_report NameError: business_metadata
# ---------------------------------------------------------------------------


def test_memory_quality_report_uses_business_metadata_without_nameerror(tmp_path: Path) -> None:
    store = RuntimeStore(root=tmp_path / "store")
    try:
        quality = {
            "importance": 0.9,
            "confidence": 0.9,
            "freshness": 1.0,
            "reuse_potential": 0.8,
            "salience_score": 0.85,
            "quality_tier": "confirmed",
            "capture_decision": "accept",
        }
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title="quality probe",
                summary="business_metadata quality should be readable",
                scope=ScopeRef(agent_id="main", workspace_id="quality"),
                source="test",
                content={"text": "business_metadata quality should be readable", "memory_type": "durable_fact"},
                meta={"memory_type": "durable_fact", "quality": quality},
            )
        )
        api = EvolutionAPI(store)
        report = api.memory_quality_report(scope={"agent_id": "main", "workspace_id": "quality"})
        assert report["memory_count"] == 1
        assert report["quality_distribution"]["confirmed"] == 1
        assert report["average_salience"] > 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Bug 3 — diagnostic recall misses evolution artifact (replay_result) records
# ---------------------------------------------------------------------------


def test_diagnostic_recall_returns_evolution_artifact_replay_result(tmp_path: Path) -> None:
    """replay_result is lane=operational / visibility=report_only.

    Diagnostic recall sets include_report_records + include_evidence_only; the
    lane allowlist must include operational or these 演化工件 records are dropped.
    """
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
    marker = "OpenClaw autonomy artifact marker for evolution regression"
    try:
        replay = runtime.store.append(
            RecordEnvelope.create(
                kind="replay_result",
                title="OpenClaw diagnostic replay artifact",
                summary=f"{marker} pass-rate analysis.",
                scope=scope,
                source="eimemory.skill_validation",
                content={"report": {"pass_rate": 0.8}, "text": marker},
                meta={"report_type": "skill_candidate_validation"},
            )
        )
        learning = runtime.store.append(
            RecordEnvelope.create(
                kind="learning_eval",
                title="OpenClaw diagnostic learning eval",
                summary=f"{marker} learning eval analysis.",
                scope=scope,
                source="eimemory.learning_eval",
                content={"pass": True, "text": marker},
                meta={"ok": True},
            )
        )

        default_bundle = runtime.memory.recall(
            query=marker,
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "chat.reply"},
            limit=20,
        )
        diagnostic_bundle = runtime.memory.recall(
            query=f"debug {marker}",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "ops.diagnostic", "intent": "diagnostic"},
            limit=20,
        )

        default_ids = {item.record_id for item in default_bundle.items}
        diagnostic_ids = {item.record_id for item in diagnostic_bundle.items}
        assert replay.record_id not in default_ids
        assert learning.record_id not in default_ids
        assert replay.record_id in diagnostic_ids, (
            "diagnostic recall must return evolution-artifact replay_result records"
        )
        assert learning.record_id in diagnostic_ids
    finally:
        runtime.close()


def test_allowed_recall_lanes_include_operational_when_report_records_enabled(tmp_path: Path) -> None:
    store = RuntimeStore(root=tmp_path / "store")
    try:
        sqlite = store.sqlite
        lanes = sqlite._allowed_recall_lanes(
            kinds=["memory", "replay_result", "learning_eval"],
            recall_filters={"include_evidence_only": True, "include_report_records": True},
        )
        assert "operational" in lanes
        evidence_only = sqlite._allowed_recall_lanes(
            kinds=["memory"],
            recall_filters={"include_evidence_only": True},
        )
        assert "operational" not in evidence_only
    finally:
        store.close()
