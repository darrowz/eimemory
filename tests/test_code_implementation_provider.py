from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from types import SimpleNamespace
from hashlib import sha256
from pathlib import Path

import pytest

import eimemory.adapters.hermes.code_implementation as provider_module
import eimemory.capabilities.code_implementation_bootstrap as bootstrap_module
from eimemory.adapters.hermes.code_implementation import (
    BINDING_ID,
    CAPABILITY_ID,
    IMPLEMENTATION_DIGEST,
    OPERATION,
    PROVIDER_INSTANCE_ID,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    REVISION_ID,
    CodeImplementationError,
    CodeImplementationSocketClient,
    CodeImplementationSocketServer,
    build_request,
    implementation_digest,
    resolve_code_implementation_provider,
    validate_request,
    validate_response,
)
from eimemory.governance.code_evolution_test_plans import protected_test_plan_digest


def _request() -> dict:
    return build_request(
        transaction_id="tx-provider-1",
        request_id="req-provider-1",
        nonce="nonce-provider-1",
        incident={
            "incident_id": "incident-provider-1",
            "incident_digest": "a" * 64,
            "incident_class": "l5.product_completion_semantic_misreport",
            "title": "Bounded reporting repair",
            "summary": "The full-product envelope must remain incomplete.",
            "diagnostic_codes": ["completion_missing"],
            "acceptance_requirements": ["top_level_ok_false"],
        },
        base={"commit": "b" * 40, "tree_digest": "c" * 64},
        allowed_files=[
            {
                "path": "eimemory/governance/l5_reader.py",
                "sha256": sha256(b"VALUE = 1\n").hexdigest(),
                "content": "VALUE = 1\n",
            }
        ],
        bounds={
            "maximum_files": 1,
            "maximum_bytes_per_file": 49_152,
            "maximum_total_bytes": 49_152,
            "maximum_changed_lines": 80,
        },
        test_plan_id="l5.product-completion-reporting.v1",
        test_plan_digest=protected_test_plan_digest("l5.product-completion-reporting.v1"),
    )


def test_provider_resolution_requires_two_pass_catalog_activation_provenance() -> None:
    class Resolution:
        ok = True
        reason = ""
        bindings = (
            {
                "descriptor": {
                    "binding_id": BINDING_ID,
                    "capability_revision_id": REVISION_ID,
                    "provider_kind": "hermes",
                    "provider_instance_id": PROVIDER_INSTANCE_ID,
                    "implementation_digest": IMPLEMENTATION_DIGEST,
                    "operations": [OPERATION],
                }
            },
        )

        def to_dict(self):
            return {"ok": True}

    class Capabilities:
        def resolve(self, *_args, **_kwargs):
            return Resolution()

        def list_adapter_advertisements(self, **_kwargs):
            return [
                {
                    "entity_id": "advertisement.code.v2",
                    "entity_digest": "a" * 64,
                    "descriptor": {
                        "binding_id": BINDING_ID,
                        "capability_revision_id": REVISION_ID,
                        "provider_kind": "hermes",
                        "provider_instance_id": PROVIDER_INSTANCE_ID,
                        "operations": [OPERATION],
                        "side_effect_class": "network",
                        "environment_fingerprint": {
                            "implementation_digest": IMPLEMENTATION_DIGEST,
                        },
                    },
                    "freshness": {"is_fresh": True},
                }
            ]

        def list_lifecycle_events(self, **_kwargs):
            return []

    report = resolve_code_implementation_provider(
        SimpleNamespace(capabilities=Capabilities()),
        runtime_scope={
            "tenant_id": "tenant",
            "agent_id": "agent",
            "workspace_id": "workspace",
            "user_id": "user",
        },
        capability_scope="global",
        checked_at="2026-08-23T00:00:00Z",
    )

    assert report["ok"] is False
    assert report["provider_ready"] is False
    assert report["reason"] == "catalog_activation_unavailable"


def test_provider_resolution_selects_latest_strict_match_during_ttl_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Resolution:
        ok = True
        reason = ""
        bindings = (
            {
                "descriptor": {
                    "binding_id": BINDING_ID,
                    "capability_revision_id": REVISION_ID,
                    "provider_kind": "hermes",
                    "provider_instance_id": PROVIDER_INSTANCE_ID,
                    "implementation_digest": IMPLEMENTATION_DIGEST,
                    "operations": [OPERATION],
                }
            },
        )

        def to_dict(self):
            return {"ok": True}

    def advertisement(*, identity: str, digest: str, advertised_at: str) -> dict:
        return {
            "entity_id": identity,
            "entity_digest": digest,
            "descriptor": {
                "binding_id": BINDING_ID,
                "capability_revision_id": REVISION_ID,
                "provider_kind": "hermes",
                "provider_instance_id": PROVIDER_INSTANCE_ID,
                "operations": [OPERATION],
                "side_effect_class": "network",
                "advertised_at": advertised_at,
                "expires_at": "2026-08-23T01:00:00Z",
                "environment_fingerprint": {
                    "implementation_digest": IMPLEMENTATION_DIGEST,
                },
            },
            "freshness": {
                "is_fresh": True,
                "advertised_at": advertised_at,
                "expires_at": "2026-08-23T01:00:00Z",
            },
        }

    older = advertisement(
        identity="advertisement.code.v2:older",
        digest="a" * 64,
        advertised_at="2026-08-23T00:00:00Z",
    )
    newer = advertisement(
        identity="advertisement.code.v2:newer",
        digest="b" * 64,
        advertised_at="2026-08-23T00:20:00Z",
    )

    class Capabilities:
        def resolve(self, *_args, **_kwargs):
            return Resolution()

        def list_adapter_advertisements(self, **_kwargs):
            return [older, newer]

    monkeypatch.setattr(
        provider_module,
        "_catalog_activation_snapshot",
        lambda *_args, **_kwargs: {
            "catalog_case_id": "hongtu_code_implementation_v2",
            "catalog_snapshot_digest": "c" * 64,
            "activation_state_digest": "d" * 64,
        },
    )

    report = resolve_code_implementation_provider(
        SimpleNamespace(capabilities=Capabilities()),
        runtime_scope={
            "tenant_id": "tenant",
            "agent_id": "agent",
            "workspace_id": "workspace",
            "user_id": "user",
        },
        capability_scope="global",
        checked_at="2026-08-23T00:30:00Z",
    )

    assert report["ok"] is True
    assert report["advertisement_id"] == newer["entity_id"]
    assert report["advertisement_digest"] == newer["entity_digest"]


def test_v2_request_is_strict_and_contains_no_execution_authority() -> None:
    request = _request()

    assert request["schema"] == REQUEST_SCHEMA
    assert request["operation"] == OPERATION
    assert request["capability_id"] == CAPABILITY_ID
    assert request["revision_id"] == REVISION_ID
    assert request["binding_id"] == BINDING_ID
    assert request["provider_instance_id"] == PROVIDER_INSTANCE_ID
    assert validate_request(request) == request
    encoded = json.dumps(request, sort_keys=True)
    assert all(token not in encoded.lower() for token in ("shell", "argv", "command", "environment", "secret"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "argv": ["git", "push"]},
        lambda value: {**value, "allowed_files": [{**value["allowed_files"][0], "path": "../outside.py"}]},
        lambda value: {**value, "incident": {**value["incident"], "command": "git push"}},
        lambda value: {**value, "base": {**value["base"], "commit": "not-a-commit"}},
    ],
)
def test_v2_request_rejects_prompt_or_path_authority(mutation) -> None:
    with pytest.raises(CodeImplementationError):
        validate_request(mutation(_request()))


def test_v2_request_rejects_files_outside_the_protected_plan() -> None:
    with pytest.raises(CodeImplementationError, match="allowed_files_not_protected"):
        build_request(
            transaction_id="tx-provider-1",
            request_id="req-provider-1",
            nonce="nonce-provider-1",
            incident=_request()["incident"],
            base={"commit": "b" * 40, "tree_digest": "c" * 64},
            allowed_files=[
                {
                    "path": "eimemory/config/loader.py",
                    "sha256": sha256(b"VALUE = 1\n").hexdigest(),
                    "content": "VALUE = 1\n",
                }
            ],
            bounds=_request()["bounds"],
            test_plan_id=_request()["test_plan_id"],
            test_plan_digest=_request()["test_plan_digest"],
        )


def test_v2_response_rejects_extra_keys_and_untrusted_commands() -> None:
    response = {
        "schema": RESPONSE_SCHEMA,
        "request_id": "req-provider-1",
        "request_digest": "f" * 64,
        "file_updates": [
            {
                "path": "eimemory/governance/l5_reader.py",
                "prior_sha256": "d" * 64,
                "content": "VALUE = 2\n",
            }
        ],
        "rationale": "bounded",
        "assumptions": [],
    }
    assert validate_response(response)["file_updates"]
    with pytest.raises(CodeImplementationError):
        validate_response({**response, "commands": [["pytest"]]})
    with pytest.raises(CodeImplementationError):
        validate_response({**response, "file_updates": [{**response["file_updates"][0], "path": "deploy/x.py"}]})
    with pytest.raises(CodeImplementationError, match="response_execution_authority"):
        validate_response({**response, "rationale": "run git push origin master"})
    with pytest.raises(CodeImplementationError, match="response_secret_material"):
        validate_response({**response, "assumptions": ["OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789"]})


@pytest.mark.parametrize(
    "addition",
    [
        'os.system("git push origin master")',
        'LEAK = "-----BEGIN PRIVATE KEY-----"',
        'URL = "https://operator:password@example.invalid/api"',
        'OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789',
        'TOKEN = "YWFhYmJiY2NjZGRkZWVlZmZmZ2dnaGhoaWlpanNvbWVzZWNyZXQ="',
    ],
)
def test_v2_response_rejects_execution_or_secret_material_in_added_lines(addition: str) -> None:
    request = _request()
    response = {
        "schema": RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "file_updates": [
            {
                "path": request["allowed_files"][0]["path"],
                "prior_sha256": request["allowed_files"][0]["sha256"],
                "content": f"VALUE = 1\n{addition}\n",
            }
        ],
        "rationale": "bounded",
        "assumptions": [],
    }

    with pytest.raises(CodeImplementationError, match="file_update_(execution_authority|secret_material)"):
        validate_response(response, request=request)


def test_implementation_digest_is_stable_and_changes_when_provider_source_changes(tmp_path: Path) -> None:
    files = {
        "provider.py": b"provider\n",
        "plugin.yaml": b"plugin\n",
        "schema.json": b'{"schema": "v2"}\n',
    }
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    first = implementation_digest(tmp_path, relative_paths=tuple(files))
    second = implementation_digest(tmp_path, relative_paths=tuple(files))
    assert first == second
    (tmp_path / "provider.py").write_bytes(b"changed\n")
    assert implementation_digest(tmp_path, relative_paths=tuple(files)) != first
    assert IMPLEMENTATION_DIGEST


def test_implementation_digest_ignores_release_only_plugin_version(
    tmp_path: Path,
) -> None:
    for relative in provider_module.DEFAULT_IMPLEMENTATION_PATHS:
        source = Path(relative)
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    manifest = tmp_path / "integrations/hermes/eimemory_hook/plugin.yaml"
    original = manifest.read_text(encoding="utf-8")
    assert "version: 1.11.4" in original
    expected_digest = implementation_digest(tmp_path)

    manifest.write_text(
        original.replace("version: 1.11.4", "version: 9.99.0", 1),
        encoding="utf-8",
    )
    assert implementation_digest(tmp_path) == expected_digest

    manifest.write_text(
        original.replace(
            "description: \"Register official Hermes host callbacks for the official eimemory provider.\"",
            "description: \"Changed provider behavior metadata.\"",
            1,
        ),
        encoding="utf-8",
    )
    assert implementation_digest(tmp_path) != expected_digest


@pytest.mark.parametrize(
    "replacement",
    (
        "version: 1.11.4 # behavior-affecting annotation",
        "version: !!str 1.11.4",
        "version : 1.11.4",
    ),
)
def test_implementation_digest_rejects_noncanonical_release_version_lines(
    tmp_path: Path,
    replacement: str,
) -> None:
    for relative in provider_module.DEFAULT_IMPLEMENTATION_PATHS:
        source = Path(relative)
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    manifest = tmp_path / "integrations/hermes/eimemory_hook/plugin.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "version: 1.11.4",
            replacement,
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        CodeImplementationError,
        match="implementation_release_version_invalid",
    ):
        implementation_digest(tmp_path)


@pytest.mark.parametrize(
    "duplicate",
    (
        "version: 9.99.0",
        "version: !!str 9.99.0",
        "version : 9.99.0",
        '"version": 9.99.0',
        "'version': 9.99.0",
        "!!str version: 9.99.0",
        "? version\n: 9.99.0",
        '"ver\\u0073ion": 9.99.0',
        "&dup version: 9.99.0",
    ),
)
def test_implementation_digest_rejects_duplicate_top_level_release_versions(
    tmp_path: Path,
    duplicate: str,
) -> None:
    for relative in provider_module.DEFAULT_IMPLEMENTATION_PATHS:
        source = Path(relative)
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    manifest = tmp_path / "integrations/hermes/eimemory_hook/plugin.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "version: 1.11.4",
            f"version: 1.11.4\n{duplicate}",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        CodeImplementationError,
        match="implementation_release_version_invalid",
    ):
        implementation_digest(tmp_path)


def test_implementation_digest_preserves_nested_version_metadata(
    tmp_path: Path,
) -> None:
    for relative in provider_module.DEFAULT_IMPLEMENTATION_PATHS:
        source = Path(relative)
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    manifest = tmp_path / "integrations/hermes/eimemory_hook/plugin.yaml"
    expected_digest = implementation_digest(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "runtime_metadata:\n  version: nested-behavior-v1\n",
        encoding="utf-8",
    )

    assert implementation_digest(tmp_path) != expected_digest


def test_implementation_digest_rejects_column_zero_version_scalar_decoy(
    tmp_path: Path,
) -> None:
    for relative in provider_module.DEFAULT_IMPLEMENTATION_PATHS:
        source = Path(relative)
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    manifest = tmp_path / "integrations/hermes/eimemory_hook/plugin.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'description: "Register official Hermes host callbacks for the official eimemory provider."',
            'description: "behavior-a\nversion: 1.2.3\nend"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        CodeImplementationError,
        match="implementation_release_version_invalid",
    ):
        implementation_digest(tmp_path)


def test_implementation_digest_requires_every_bound_source_file(tmp_path: Path) -> None:
    (tmp_path / "provider.py").write_bytes(b"provider\n")

    with pytest.raises(CodeImplementationError, match="implementation_source_missing"):
        implementation_digest(tmp_path, relative_paths=("provider.py", "missing.yaml"))


def test_default_repo_root_finds_release_above_nested_wheel_site_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    for relative in provider_module.DEFAULT_IMPLEMENTATION_PATHS:
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "integrations/hermes/eimemory_hook/plugin.yaml":
            path.write_bytes(Path(relative).read_bytes())
        else:
            path.write_text(f"bound source: {relative}\n", encoding="utf-8")
    installed_module = (
        release
        / ".venv/lib/python3.14/site-packages/eimemory/adapters/hermes/code_implementation.py"
    )
    installed_module.parent.mkdir(parents=True, exist_ok=True)
    installed_module.write_text("installed wheel module\n", encoding="utf-8")
    monkeypatch.setattr(provider_module, "__file__", str(installed_module))

    assert provider_module._default_repo_root() == release
    assert implementation_digest() == implementation_digest(release)


def test_fixed_socket_client_uses_length_prefixed_json_and_peer_credentials(tmp_path: Path) -> None:
    if not hasattr(socket, "AF_UNIX") or not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Unix peer credentials are unavailable")
    # Linux sun_path is normally 108 bytes.  The full-suite isolation root is
    # intentionally much longer than that, so keep this transport test on a
    # private short directory instead of testing pytest's path layout.
    with tempfile.TemporaryDirectory(prefix="eimemory-provider-", dir="/tmp") as root_name:
        socket_path = Path(root_name) / "provider.sock"
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.bind(str(socket_path))
        except PermissionError:
            probe.close()
            pytest.skip("this sandbox does not permit Unix-domain socket bind")
        probe.close()
        socket_path.unlink(missing_ok=True)
        ready = threading.Event()

        def server() -> None:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
            listener.listen(1)
            ready.set()
            try:
                connection, _ = listener.accept()
                with connection:
                    frame = connection.recv(4096)
                    size = int.from_bytes(frame[:4], "big")
                    payload = json.loads(frame[4 : 4 + size])
                    response = {
                        "ok": True,
                        "operation": payload["operation"],
                        "nonce": payload["nonce"],
                        "provider_instance_id": PROVIDER_INSTANCE_ID,
                        "implementation_digest": IMPLEMENTATION_DIGEST,
                    }
                    encoded = json.dumps(response, sort_keys=True).encode()
                    connection.sendall(len(encoded).to_bytes(4, "big") + encoded)
            finally:
                listener.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        ready.wait(timeout=2)
        client = CodeImplementationSocketClient(socket_path=socket_path, timeout_seconds=2)
        result = client.health(nonce="socket-health-1")
        thread.join(timeout=2)
        assert result == {
            "ok": True,
            "operation": "health",
            "nonce": "socket-health-1",
            "provider_instance_id": PROVIDER_INSTANCE_ID,
            "implementation_digest": IMPLEMENTATION_DIGEST,
        }


def test_socket_transport_rejects_a_group_accessible_parent(tmp_path: Path) -> None:
    socket_root = tmp_path / "provider"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "provider.sock"
    socket_root.chmod(0o750)

    with pytest.raises(CodeImplementationError, match="socket_parent_permissions_invalid"):
        provider_module._validate_socket_path(socket_path)


def test_socket_transport_rejects_a_parent_owned_by_another_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_root = tmp_path / "provider"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "provider.sock"
    actual_uid = os.geteuid()
    monkeypatch.setattr(provider_module.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(CodeImplementationError, match="socket_parent_owner_invalid"):
        provider_module._validate_socket_path(socket_path)


def test_gateway_provider_replaces_a_stale_owned_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="eimemory-provider-stale-",
        dir="/tmp",
    ) as root_name:
        socket_path = Path(root_name) / "provider.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
        finally:
            stale.close()
        monkeypatch.setenv("EIMEMORY_HERMES_GATEWAY_PROCESS", "1")
        server = CodeImplementationSocketServer(
            SimpleNamespace(llm=SimpleNamespace(complete_structured=lambda **_kwargs: None)),
            socket_path=socket_path,
        )
        try:
            assert server.start() is True
            assert CodeImplementationSocketClient(
                socket_path=socket_path,
                timeout_seconds=2,
            ).health(nonce="stale-socket-recovered")["ok"] is True
        finally:
            server.stop()


def test_gateway_provider_never_unlinks_a_live_owned_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="eimemory-provider-live-",
        dir="/tmp",
    ) as root_name:
        socket_path = Path(root_name) / "provider.sock"
        monkeypatch.setenv("EIMEMORY_HERMES_GATEWAY_PROCESS", "1")
        context = SimpleNamespace(
            llm=SimpleNamespace(complete_structured=lambda **_kwargs: None)
        )
        first = CodeImplementationSocketServer(context, socket_path=socket_path)
        second = CodeImplementationSocketServer(context, socket_path=socket_path)
        try:
            assert first.start() is True
            assert second.start() is False
            second.stop()
            assert socket_path.is_socket()
            assert CodeImplementationSocketClient(
                socket_path=socket_path,
                timeout_seconds=2,
            ).health(nonce="live-socket-preserved")["ok"] is True
        finally:
            first.stop()


def test_gateway_provider_uses_bounded_host_structured_completion_contract() -> None:
    captured = {}
    request = _request()
    response = {
        "schema": RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "file_updates": [
            {
                "path": request["allowed_files"][0]["path"],
                "prior_sha256": request["allowed_files"][0]["sha256"],
                "content": "VALUE = 2\n",
            }
        ],
        "rationale": "bounded fixture repair",
        "assumptions": [],
    }

    class Llm:
        def complete_structured(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                parsed=response,
                provider="test-provider",
                model="test-model",
                agent_id="default",
                audit={"task": provider_module.FIXED_COMPLETION_TASK},
            )

    result = CodeImplementationSocketServer(SimpleNamespace(llm=Llm()))._complete(request)

    assert captured["task"] == provider_module.FIXED_COMPLETION_TASK
    assert captured["instructions"] == provider_module.FIXED_COMPLETION_INSTRUCTIONS
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == provider_module.FIXED_COMPLETION_MAX_TOKENS
    assert captured["timeout"] == provider_module.FIXED_COMPLETION_TIMEOUT_SECONDS
    assert captured["json_schema"]["additionalProperties"] is False
    assert captured["input"] == [{"type": "text", "text": provider_module.canonical_json(request)}]
    assert result["response"] == response
    assert result["attestation"]["route"] == {
        "provider": "test-provider",
        "model": "test-model",
        "agent_id": "default",
        "task": provider_module.FIXED_COMPLETION_TASK,
    }


def test_gateway_provider_applies_a_bounded_sliding_window_rate_limit() -> None:
    now = [0.0]
    server = CodeImplementationSocketServer(
        SimpleNamespace(),
        clock=lambda: now[0],
    )

    assert all(server._admit_request() for _ in range(provider_module.PROVIDER_RATE_LIMIT))
    assert server._admit_request() is False

    now[0] = provider_module.PROVIDER_RATE_WINDOW_SECONDS + 0.001
    assert server._admit_request() is True


def test_gateway_health_probe_does_not_consume_or_require_proposal_rate_budget() -> None:
    server = CodeImplementationSocketServer(SimpleNamespace())
    server._admit_request = lambda: False  # type: ignore[method-assign]
    client, accepted = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        nonce = "deployment-health-probe"
        request = provider_module.canonical_json(
            {"operation": "health", "nonce": nonce}
        ).encode("utf-8")
        client.sendall(len(request).to_bytes(4, "big") + request)

        server._serve_connection_serial(accepted)

        size = int.from_bytes(client.recv(4), "big")
        response = json.loads(client.recv(size).decode("utf-8"))
    finally:
        client.close()
        accepted.close()

    assert response == {
        "ok": True,
        "operation": "health",
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "implementation_digest": IMPLEMENTATION_DIGEST,
        "nonce": nonce,
    }


def test_bootstrap_advertisement_requires_a_live_provider_health_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableClient:
        def health(self, *, nonce: str):
            raise CodeImplementationError("provider_transport_unavailable")

    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", UnavailableClient)
    monkeypatch.setattr(bootstrap_module.time, "sleep", lambda _seconds: None)
    report = bootstrap_module.advertise_code_implementation_v2(
        SimpleNamespace(),
        runtime_scope={
            "tenant_id": "tenant",
            "agent_id": "agent",
            "workspace_id": "workspace",
            "user_id": "user",
        },
        advertised_at="2026-08-23T00:00:00Z",
        expires_at="2026-08-23T01:00:00Z",
    )

    assert report == {
        "ok": False,
        "status": "blocked",
        "reason": "provider_health_unavailable",
        "qualifying": False,
    }


def test_bootstrap_advertisement_rejects_a_non_hour_ttl() -> None:
    report = bootstrap_module.advertise_code_implementation_v2(
        SimpleNamespace(),
        runtime_scope={
            "tenant_id": "tenant",
            "agent_id": "agent",
            "workspace_id": "workspace",
            "user_id": "user",
        },
        advertised_at="2026-08-23T00:00:00Z",
        expires_at="2026-08-23T02:00:00Z",
    )

    assert report["reason"] == "advertisement_ttl_invalid"


def test_bootstrap_advertisement_does_not_retry_an_attestation_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class MismatchedClient:
        def health(self, *, nonce: str):
            nonlocal calls
            calls += 1
            raise CodeImplementationError("provider_health_attestation_mismatch")

    monkeypatch.setattr(bootstrap_module, "CodeImplementationSocketClient", MismatchedClient)
    monkeypatch.setattr(
        bootstrap_module.time,
        "sleep",
        lambda _seconds: pytest.fail("non-transient health failure was retried"),
    )

    report = bootstrap_module.advertise_code_implementation_v2(
        SimpleNamespace(),
        runtime_scope={
            "tenant_id": "tenant",
            "agent_id": "agent",
            "workspace_id": "workspace",
            "user_id": "user",
        },
        advertised_at="2026-08-23T00:00:00Z",
        expires_at="2026-08-23T01:00:00Z",
    )

    assert report["reason"] == "provider_health_unavailable"
    assert calls == 1
