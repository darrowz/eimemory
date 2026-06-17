"""L3 action queue: pending_human by default, human approval flips to approved.

L3 actions in eimemory (spend, auth, deploy, devices, prompt mutation, deletion)
require explicit human approval. The L3Queue is the single entry point: any caller
that wants to perform an L3 action must request and wait for a human approver.

Backed by a JSONL file at ``<root>/l3_queue.jsonl`` so the queue survives process
restarts and can be inspected by a separate human-facing tool.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from eimemory.governance.safety.l3_queue import L3Queue


def test_l3_request_starts_pending() -> None:
    """A new L3 request must default to status=pending_human."""
    with tempfile.TemporaryDirectory() as tmp:
        q = L3Queue(root=Path(tmp))
        rid = q.request(
            action_class="send_external_message",
            payload={"to": "x", "msg": "y"},
            requester="loop.py",
        )
        req = q.get(rid)
        assert req["status"] == "pending_human"
        assert req["action_class"] == "send_external_message"
        assert req["requester"] == "loop.py"
        assert req["payload"] == {"to": "x", "msg": "y"}
        assert req["approver"] is None
        assert "created_at" in req


def test_l3_approval_flips_status() -> None:
    """An approve() call flips the request from pending_human to approved."""
    with tempfile.TemporaryDirectory() as tmp:
        q = L3Queue(root=Path(tmp))
        rid = q.request(
            action_class="send_external_message",
            payload={},
            requester="loop.py",
        )
        q.approve(rid, approver="hongtu")
        req = q.get(rid)
        assert req["status"] == "approved"
        assert req["approver"] == "hongtu"
        assert "approved_at" in req


def test_l3_list_pending_only_returns_pending() -> None:
    """list_pending() returns only requests still in pending_human state."""
    with tempfile.TemporaryDirectory() as tmp:
        q = L3Queue(root=Path(tmp))
        rid1 = q.request(action_class="send_external_message", payload={}, requester="loop.py")
        rid2 = q.request(action_class="deploy_production", payload={}, requester="loop.py")
        q.approve(rid1, approver="hongtu")
        pending = q.list_pending()
        ids = [r["id"] for r in pending]
        assert rid1 not in ids
        assert rid2 in ids
        assert len(pending) == 1
