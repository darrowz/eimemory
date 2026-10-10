"""Host callback observations. Only labels and keyed transient fingerprints."""
from collections import OrderedDict
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from uuid import uuid4

from eimemory.retrieval.effect_signals import validate_signal

_CORRECTION = re.compile(r"^(?:不对|不是这个|我说的是|你理解错了|你搞错了|不是这个意思|no[,，! ]|that's wrong|i meant\b)", re.I)


def tool_status(result):
    # Free-form prose is not proof that a tool or task succeeded.
    if isinstance(result, str):
        try:
            result = json.loads(result[:16000])
        except (ValueError, TypeError):
            return "unknown"
    if not isinstance(result, dict):
        return "unknown"
    if result.get("success") is False or result.get("ok") is False or result.get("is_error") is True or result.get("error"):
        return "failed"
    code = result.get("exit_code", result.get("returncode"))
    if isinstance(code, int) and not isinstance(code, bool):
        return "succeeded" if code == 0 else "failed"
    return "succeeded" if result.get("success") is True or result.get("ok") is True else "unknown"


class EffectObserver:
    def __init__(self, provider):
        self.provider = provider
        self.lock = threading.RLock()
        self.flush_lock = threading.Lock()
        self.secret = os.urandom(32)
        self.turns = OrderedDict()
        self.current = OrderedDict()
        self.previous = OrderedDict()
        self.completed_targets = OrderedDict()
        self.pending = OrderedDict()
        self.path = None
        self.sent = 0
        self.dropped = 0
        self.unbound_tools = 0
        self.unbound_turns = 0
        self.error = ""
        self.persist_error = ""
        self.ledger_error = ""
        self.worker = None

    def _fingerprint(self, query):
        normalized = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(query)[:8000]).casefold())
        return hmac.new(self.secret, normalized.encode(), sha256).hexdigest() if normalized else ""

    def _event(self, turn):
        # Never persist a possibly user-supplied host identifier verbatim.
        return "host-" + sha256(str(turn).encode()).hexdigest()[:32]

    def pre(self, session, turn, query):
        with self.lock:
            self.request_flush()
            if not turn:
                current = self.current.get(session)
                active = self.turns.get((session, current))
                turn = current if active and not active["done"] else "local-" + uuid4().hex
            key = (session, turn)
            if key in self.turns:
                return
            fingerprint = self._fingerprint(query)
            previous = self.previous.get(session)
            if previous and previous["turn"] != turn and not previous["observed"]:
                elapsed = time.monotonic() - previous["time"]
                if 0 <= elapsed <= 300:
                    self.emit(previous["namespace"], "next_user", self._event(turn), {
                        "correction": "suspected" if _CORRECTION.match(str(query).strip()[:256]) else "none",
                        "reask": "suspected" if fingerprint and fingerprint == previous["fingerprint"] else "none",
                    })
                previous["observed"] = True
            self.turns[key] = {"fingerprint": fingerprint, "time": time.monotonic(), "tools": {}, "done": False}
            self.current[session] = turn
            while len(self.current) > 128:
                self.current.popitem(last=False)
            while len(self.turns) > 128:
                self.turns.popitem(last=False)

    def tool(self, session, turn, call_id, result):
        with self.lock:
            state = self.turns.get((session, turn))
            if state is None and str(self.current.get(session, "")).startswith("local-"):
                state = self.turns.get((session, self.current[session]))
            if state and not state["done"]:
                call_id = call_id or "local-tool-" + uuid4().hex
                key = self._event(call_id)
                if len(state["tools"]) < 128 or key in state["tools"]:
                    value = tool_status(result)
                    state["tools"][key] = "failed" if state["tools"].get(key) == "failed" else value
                else:
                    state["tools"]["overflow"] = "unknown"
            else:
                self.unbound_tools += 1
                current = self.turns.get((session, self.current.get(session, "")))
                if current and not current["done"]:
                    current["tools"]["unbound"] = "unknown"

    def completed(self, session, turn, pending, task_success=None):
        with self.lock:
            turn = turn or self.current.get(session, "")
            state = self.turns.get((session, turn))
            if not pending or not pending.get("decision_id"):
                self.unbound_turns += 1
                return
            if not state:
                self.unbound_turns += 1
                turn = turn or "local-" + uuid4().hex
                state = {"fingerprint": self._fingerprint(pending.get("query", "")),
                         "time": None, "tools": {}, "done": False}
                self.turns[(session, turn)] = state
                while len(self.turns) > 128:
                    self.turns.popitem(last=False)
            if state["done"]:
                return
            state["done"] = True
            namespace = {"channel": "hermes", "scope": pending["scope"], "source_ids": pending["source_ids"],
                         "session_id": session, "turn_id": pending["decision_turn_id"], "decision_id": pending["decision_id"]}
            tools = list(state["tools"].values())
            chain = "failed" if "failed" in tools else "unknown" if "unknown" in tools else "succeeded" if tools else "not_run" if state["time"] is not None else "unknown"
            labels = {
                "tool_chain": chain,
                "task_success": "succeeded" if task_success is True else "failed" if task_success is False else "unknown",
            }
            if state["time"] is not None:
                labels["latency_ms"] = round(max(0, time.monotonic() - state["time"]) * 1000, 3)
            self.emit(namespace, "turn_completed", self._event(turn), labels)
            self.previous[session] = {"turn": turn, "time": time.monotonic(), "fingerprint": state["fingerprint"],
                                      "namespace": namespace, "observed": False}
            self.completed_targets[(session, turn)] = namespace
            while len(self.completed_targets) > 128:
                self.completed_targets.popitem(last=False)
            while len(self.previous) > 64:
                self.previous.popitem(last=False)

    def rating(self, session, turn, rating, event_id):
        with self.lock:
            namespace = self.completed_targets.get((session, turn))
            if not namespace or rating not in {"positive", "negative"} or not event_id:
                return False
            self.emit(namespace, "explicit_rating", self._event(event_id), {"rating": rating})
            return True

    def _load(self):
        if self.path is not None or not self.provider._hermes_home:
            return
        self.path = Path(self.provider._hermes_home) / "logs" / ("eimemory-effect-signals-" + sha256(self.provider._session_id.encode()).hexdigest()[:20] + ".json")
        try:
            if self.path.exists():
                if self.path.stat().st_size > 256000:
                    raise ValueError("oversized")
                payloads = json.loads(self.path.read_text())
                if not isinstance(payloads, list) or len(payloads) > 64:
                    raise ValueError("invalid")
                for p in payloads:
                    if set(p) != {"channel", "scope", "source_ids", "session_id", "turn_id", "decision_id", "phase", "event_id", "labels"}:
                        raise ValueError("invalid")
                    validate_signal(p["phase"], p["event_id"], p["labels"])
                    self.pending[self._key(p)] = p
        except (OSError, ValueError, TypeError, KeyError):
            self.ledger_error = "signal_retry_ledger_unreadable"

    @staticmethod
    def _key(params):
        return (params["decision_id"], params["phase"], params["event_id"])

    def _persist(self):
        if self.path is None or self.ledger_error:
            return
        temp = self.path.with_suffix("." + uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(list(self.pending.values()), stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            self.persist_error = ""
        except OSError:
            self.persist_error = "signal_retry_persist_failed"
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def emit(self, namespace, phase, event_id, labels):
        self._load()
        params = {**namespace, "phase": phase, "event_id": event_id, "labels": labels}
        key = self._key(params)
        if len(self.pending) >= 64 and key not in self.pending:
            self.dropped += 1
            self.error = "signal_retry_capacity_exceeded"
            return
        self.pending.setdefault(key, params)
        self._persist()
        self.request_flush()

    def _client(self):
        if self.provider._attestation_client is not None:
            return self.provider._attestation_client
        from eimemory.adapters.hermes.provider_core import hermes_attestation_client_from_env
        return hermes_attestation_client_from_env(hermes_home=self.provider._hermes_home, timeout_seconds=1.0)

    def request_flush(self):
        # No network I/O on the host callback path. One worker per provider.
        with self.lock:
            self._load()
            if self._client() is None:
                self.error = "signal_host_credentials_unavailable"
                return
            if not self.pending or (self.worker and self.worker.is_alive()):
                return
            self.worker = threading.Thread(target=self.flush, name="eimemory-effect-signals", daemon=True)
            self.worker.start()

    def flush(self):
        with self.flush_lock:
            with self.lock:
                self._load()
                pending = list(self.pending.items())[:4]
            client = self._client()
            if client is None:
                self.error = "signal_host_credentials_unavailable"
                return
            for key, params in pending:
                response = self.provider._safe_call_with(client, "adapter.proactive_signal", params)
                if response.get("ok") is True and isinstance(response.get("result"), dict) and response["result"].get("ok") is True:
                    with self.lock:
                        self.pending.pop(key, None)
                        self.sent += 1
                        self.error = ""
                        self._persist()
                else:
                    self.error = "signal_delivery_failed"
                    break

    def status(self):
        with self.lock:
            return {"schema": "recall.effect_observer.v1", "sent": self.sent, "pending": len(self.pending),
                    "durable_queue_configured": self.path is not None,
                    "dropped": self.dropped, "error": self.ledger_error or self.persist_error or self.error,
                    "unbound_tool_callbacks": self.unbound_tools, "unbound_completed_turns": self.unbound_turns,
                    "worker_running": bool(self.worker and self.worker.is_alive()),
                    "next_user_heuristics": True, "native_reaction_hook": False}
