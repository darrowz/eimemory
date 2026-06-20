import json
import io
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
import sys
import pytest

from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.adapters.openclaw.tools import OpenClawMemoryTools
from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.compatibility.migration_helpers import export_records, import_records
from eimemory.config.loader import load_settings
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.governance.supervisor import build_supervisor_contract
from eimemory.scheduler.jobs import run_nightly_jobs


def test_settings_loader_prefers_env_and_file(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps({"root": str(tmp_path / "from-file"), "default_agent_id": "file-agent"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIMEMORY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "from-env"))

    settings = load_settings()

    assert settings.root == tmp_path / "from-env"
    assert settings.default_agent_id == "file-agent"


def test_settings_loader_reads_settings_from_config_dir(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "root": str(tmp_path / "from-config-dir"),
                "default_agent_id": "dir-agent",
                "default_workspace_id": "repo-x",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("EIMEMORY_ROOT", raising=False)
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(config_dir))

    settings = load_settings()

    assert settings.root == tmp_path / "from-config-dir"
    assert settings.default_agent_id == "dir-agent"
    assert settings.default_workspace_id == "repo-x"


def test_settings_loader_defaults_rpc_port_for_eibrain_rpc(monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("EIMEMORY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EIMEMORY_ROOT", raising=False)

    settings = load_settings()

    assert settings.rpc_host == "127.0.0.1"
    assert settings.rpc_port == 8091


def test_settings_loader_reads_loopback_health_proxy_settings(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "root": str(tmp_path / "runtime"),
                "rpc_host": "100.105.189.120",
                "rpc_port": 8091,
                "rpc_loopback_health_host": "127.0.0.1",
                "rpc_loopback_health_port": 8091,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("EIMEMORY_ROOT", raising=False)

    settings = load_settings()

    assert settings.rpc_loopback_health_host == "127.0.0.1"
    assert settings.rpc_loopback_health_port == 8091



def test_settings_loader_requires_present_config_path(tmp_path, monkeypatch) -> None:
    missing_path = tmp_path / "missing.json"
    monkeypatch.setenv("EIMEMORY_CONFIG_PATH", str(missing_path))
    monkeypatch.delenv("EIMEMORY_CONFIG_DIR", raising=False)

    try:
        load_settings()
    except FileNotFoundError as exc:
        assert str(missing_path) in str(exc)
    else:
        raise AssertionError("expected missing config path to fail fast")


def test_cli_reports_invalid_config_when_config_dir_missing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(tmp_path / "missing-config-dir"))
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)

    assert cli_main(["serve-eibrain-rpc"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_config"
    assert payload["exception"] == "FileNotFoundError"

def test_cli_init_ingest_recall_and_export_import(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    assert cli_main(["init"]) == 0
    assert cli_main(["ingest", "Remember concise replies", "--title", "Concise"]) == 0
    assert cli_main(["recall", "concise replies"]) == 0
    export_path = tmp_path / "export.jsonl"
    assert cli_main(["export", str(export_path)]) == 0
    assert export_path.exists()

    import_root = tmp_path / "imported"
    monkeypatch.setenv("EIMEMORY_ROOT", str(import_root))
    assert cli_main(["init"]) == 0
    assert cli_main(["import", str(export_path)]) == 0
    assert cli_main(["recall", "concise replies"]) == 0

    output = capsys.readouterr().out
    assert "Concise" in output


def test_cli_supports_paper_ingest_extract_compile_and_research_recall(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    assert cli_main([
        "paper",
        "ingest",
        "--arxiv-id",
        "2501.12345",
        "--title",
        "Compact Retrieval",
        "--abstract",
        "Compact retrieval improves embodied response quality.",
    ]) == 0
    source = json.loads(capsys.readouterr().out)
    paper_source_id = source["record_id"]
    assert cli_main([
        "paper",
        "extract",
        "--paper-source-id",
        paper_source_id,
        "--title",
        "Compact Retrieval",
        "--abstract",
        "Compact retrieval improves embodied response quality.",
        "--body",
        "Method: compact retrieval.",
    ]) == 0
    assert cli_main(["paper", "compile", "--paper-source-id", paper_source_id]) == 0
    assert cli_main(["recall", "compact retrieval", "--view", "page_centered"]) == 0

    output = capsys.readouterr().out
    assert "knowledge_page" in output
    assert "page_centered" in output


def test_cli_can_serve_eibrain_rpc(tmp_path, monkeypatch, capsys) -> None:
    from eimemory.cli.main import main as cli_main

    started: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, runtime, *, host: str, port: int) -> None:
            started["host"] = host
            started["port"] = port
            started["root"] = str(runtime.store.root)
            self.address = (host, port)

        def serve_forever(self) -> None:
            started["served"] = True

    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(tmp_path / "config"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"rpc_host": "127.0.0.1", "rpc_port": 8091}),
        encoding="utf-8",
    )
    monkeypatch.setattr("eimemory.cli.main.EIBrainRPCServer", _FakeServer)

    assert cli_main(["serve-eibrain-rpc"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert started == {
        "host": "127.0.0.1",
        "port": 8091,
        "root": str(tmp_path / "runtime"),
        "served": True,
    }
    assert output == {"ok": True, "host": "127.0.0.1", "port": 8091}


def test_cli_doctor_reports_ops_diagnostics(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("EIMEMORY_COMMIT", "abc123doctor")

    assert cli_main(["doctor"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["service"] == "eimemory-rpc"
    assert payload["version"]
    assert payload["commit"] == "abc123doctor"
    assert payload["paths"]["current"]
    assert payload["paths"]["release"]
    assert payload["listen_host"] == "127.0.0.1"
    assert payload["listen_port"] == 8091
    assert payload["store"]["ready"] is True
    assert payload["checks"]["ready"] is True
    assert payload["supervisor"]["status"] in {"healthy", "degraded", "stuck", "unknown"}
    assert "learn-watch" in payload["supervisor"]["runs"]
    for key in ("last_success_at", "last_error_at", "duration_ms", "memory_peak", "produced_count", "promoted_count", "rolled_back_count"):
        assert key in payload["supervisor"]["runs"]["learn-watch"]


def test_http_rpc_server_serves_recall_and_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Prefer concise replies",
        memory_type="preference",
        title="Concise",
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
    )
    runtime.evolution.store_rule(
        title="Task context first",
        summary="Prefer task context",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        status="active",
    )
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        recall = server.request(
            {
                "method": "memory.recall",
                "params": {
                    "query": "concise replies",
                    "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                    "task_context": {"task_type": "brain.respond"},
                },
            }
        )
        policy = server.request(
            {
                "method": "evolution.get_active_policy",
                "params": {
                    "task_type": "brain.respond",
                    "scope": {"agent_id": "eibrain", "workspace_id": "robot"},
                },
            }
        )
    finally:
        server.stop()

    assert recall["ok"] is True
    assert recall["result"]["items"]
    assert policy["result"]["retrieval_policy"]["route_hint"] == "task_context_first"


def test_http_rpc_server_health_reports_release_and_store_readiness(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_COMMIT", "abc123health")
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        with urllib.request.urlopen(f"http://{server.address[0]}:{server.address[1]}/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["version"]
    assert payload["commit"] == "abc123health"
    assert payload["paths"]["current"]
    assert payload["paths"]["release"]
    assert payload["listen_host"] == server.address[0]
    assert payload["listen_port"] == server.address[1]
    assert payload["store"]["ready"] is True
    assert payload["store"]["root"] == str(tmp_path)
    assert payload["checks"]["store"] is True
    assert payload["checks"]["ready"] is True


def test_health_payload_infers_commit_from_release_working_directory(tmp_path, monkeypatch) -> None:
    from eimemory.adapters.eibrain.rpc_server import build_health_payload

    commit = "abc123def4567890"
    release_dir = tmp_path / "opt" / "eimemory" / "releases" / commit
    release_dir.mkdir(parents=True)
    monkeypatch.delenv("EIMEMORY_COMMIT", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("SOURCE_VERSION", raising=False)
    monkeypatch.chdir(release_dir)
    runtime = Runtime.create(root=tmp_path / "runtime")

    payload = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=8091)

    assert payload["commit"] == commit
    assert payload["paths"]["release"] == str(release_dir)


def test_http_rpc_server_can_expose_loopback_health_proxy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(
        runtime,
        host="127.0.0.1",
        port=0,
        loopback_health_host="127.0.0.1",
        loopback_health_port=0,
    )
    server.start()
    try:
        host, port = server.loopback_health_address
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["listen_host"] == server.address[0]
    assert payload["listen_port"] == server.address[1]
    assert payload["loopback_health"]["host"] == "127.0.0.1"
    assert payload["loopback_health"]["port"] == port


def test_http_rpc_server_get_root_returns_daily_brief_digest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.store.append(
        RecordEnvelope.create(
            kind="news",
            title="News item: eimemory launches RSS intake",
            summary="RSS news intake is available.",
            scope=ScopeRef.from_dict({"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}),
            content={"item_url": "https://example.test/news/rss"},
            tags=["news"],
            source="eimemory.news.collect",
        )
    )
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        with urllib.request.urlopen(f"http://{server.address[0]}:{server.address[1]}/", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["service"] == "eimemory-rpc"
    assert payload["news_digest"]["count"] == 1
    assert payload["news_digest"]["items"][0]["url"] == "https://example.test/news/rss"


def test_scheduler_and_openclaw_tools_surface_runtime_state(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Remember prompt build context",
        memory_type="fact",
        title="Prompt context",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    runtime.evolution.store_rule(
        title="Prompt build rule",
        summary="Prefer task context",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        status="active",
    )
    tools = OpenClawMemoryTools(runtime)

    search = tools.memory_search(
        query="prompt build context",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        limit=5,
    )
    explain = tools.memory_explain(
        query="prompt build context",
        task_context={"task_type": "chat.reply"},
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    nightly = run_nightly_jobs(runtime, scope={"agent_id": "main", "workspace_id": "repo-x"})

    assert search["ok"] is True
    assert search["items"]
    assert explain["ok"] is True
    assert nightly["active_rule_count"] == 1
    assert nightly["supervisor_summary"]["command"] == "nightly"
    supervisor = build_supervisor_contract(runtime, scope={"agent_id": "main", "workspace_id": "repo-x"})
    assert supervisor["runs"]["nightly"]["last_success_at"]


def test_export_and_import_helpers_roundtrip(tmp_path) -> None:
    source = Runtime.create(root=tmp_path / "source")
    source.memory.ingest(
        text="Portable memory export",
        memory_type="fact",
        title="Exported memory",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    export_path = tmp_path / "portable.jsonl"
    count = export_records(source, export_path)

    target = Runtime.create(root=tmp_path / "target")
    imported = import_records(target, export_path)
    bundle = target.memory.recall(
        query="portable memory export",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert count == 1
    assert imported == 1
    assert bundle.items[0].title == "Exported memory"


@pytest.mark.slow
def test_export_records_includes_more_than_ten_thousand_rows(tmp_path) -> None:
    source = Runtime.create(root=tmp_path / "source")
    for idx in range(10005):
        source.memory.ingest(
            text=f"Exportable memory {idx}",
            memory_type="fact",
            title=f"Memory {idx}",
            scope={"agent_id": "main", "workspace_id": "repo-x"},
        )

    export_path = tmp_path / "large-export.jsonl"
    count = export_records(source, export_path)

    lines = export_path.read_text(encoding="utf-8").splitlines()
    assert count == 10005
    assert len(lines) == 10005


def test_export_records_writes_all_runtime_records_without_large_fixture(tmp_path) -> None:
    source = Runtime.create(root=tmp_path / "source_small")
    for idx in range(3):
        source.memory.ingest(
            text=f"Small export memory {idx}",
            memory_type="fact",
            title=f"Memory {idx}",
            scope={"agent_id": "main", "workspace_id": "repo-x"},
        )

    export_path = tmp_path / "small-export.jsonl"
    count = export_records(source, export_path)

    lines = export_path.read_text(encoding="utf-8").splitlines()
    assert count == 3
    assert len(lines) == 3


def test_readme_and_example_files_exist() -> None:
    assert Path("README.md").exists()
    assert Path("docs/architecture.md").exists()
    assert Path("examples/standalone/basic_usage.py").exists()


def test_qmd_compat_collection_and_search(tmp_path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = workspace / "memory"
    memory_dir.mkdir()
    note = memory_dir / "fact.md"
    note.write_text("# Fact\n\nServer smoke record\n", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    assert cli_main(["qmd", "collection", "add", str(memory_dir), "--name", "memory-dir-main", "--mask", "**/*.md"]) == 0
    assert cli_main(["qmd", "update"]) == 0
    assert cli_main(["qmd", "search", "Server smoke", "--json", "-n", "5", "-c", "memory-dir-main"]) == 0

    payload = capsys.readouterr().out
    assert "memory-dir-main" in payload
    assert "Server smoke record" in payload


def test_qmd_aliases_and_collection_listing(tmp_path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_text("Remember qmd alias search\n", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    assert cli_main(["qmd", "collection", "add", str(workspace), "--name", "memory-root-main", "--mask", "MEMORY.md"]) == 0
    assert cli_main(["qmd", "update"]) == 0
    assert cli_main(["qmd", "collection", "list", "--json"]) == 0
    assert cli_main(["qmd", "query", "qmd alias", "--json", "-n", "3"]) == 0
    assert cli_main(["qmd", "vsearch", "qmd alias", "--json", "-n", "3"]) == 0

    output = capsys.readouterr().out
    assert "memory-root-main" in output
    assert "Remember qmd alias search" in output


def test_qmd_status_reports_documents_and_vectors(tmp_path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "MEMORY.md"
    note.write_text("Remember status command coverage\n", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    assert cli_main(["qmd", "collection", "add", str(workspace), "--name", "memory-root-main", "--mask", "MEMORY.md"]) == 0
    assert cli_main(["qmd", "update"]) == 0
    assert cli_main(["qmd", "status"]) == 0

    output = capsys.readouterr().out
    assert "Collections: 1" in output
    assert "Documents: 1" in output
    assert "Vectors: 0" in output


def test_runtime_materializes_qmd_markdown_exports(tmp_path, monkeypatch, capsys) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    record = runtime.memory.ingest(
        text="Remember markdown exports for qmd search",
        memory_type="fact",
        title="Markdown export memory",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    export_path = tmp_path / "runtime" / "qmd" / "records" / f"{record.record_id}.md"
    assert export_path.exists()
    exported = export_path.read_text(encoding="utf-8")
    assert "Markdown export memory" in exported
    assert "Remember markdown exports for qmd search" in exported

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    assert cli_main(["qmd", "collection", "add", str(export_path.parent), "--name", "memory-md", "--mask", "*.md"]) == 0
    assert cli_main(["qmd", "update"]) == 0
    assert cli_main(["qmd", "search", "markdown exports", "--json", "-n", "5", "-c", "memory-md"]) == 0

    output = capsys.readouterr().out
    assert "Markdown export memory" in output


def test_qmd_markdown_export_keeps_active_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    record = runtime.memory.ingest(
        text="Remember active memory exports to qmd markdown",
        memory_type="fact",
        title="Active qmd export",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    export_path = tmp_path / "runtime" / "qmd" / "records" / f"{record.record_id}.md"

    assert export_path.exists()
    assert "Active qmd export" in export_path.read_text(encoding="utf-8")


def test_qmd_markdown_export_skips_rejected_memory_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    rejected = RecordEnvelope.create(
        kind="memory",
        title="Rejected qmd memory",
        summary="This should not be exported to qmd.",
        scope=ScopeRef(agent_id="main", workspace_id="repo-x"),
        status="rejected",
        meta={"quality": {"capture_decision": "reject", "salience_score": 0.0}},
    )

    runtime.store.append(rejected)
    export_path = tmp_path / "runtime" / "qmd" / "records" / f"{rejected.record_id}.md"

    assert not export_path.exists()


def test_qmd_markdown_export_deletes_previously_exported_rejected_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    record = runtime.memory.ingest(
        text="Remember temporary qmd export until quality review rejects it",
        memory_type="fact",
        title="Temporary qmd export",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    export_path = tmp_path / "runtime" / "qmd" / "records" / f"{record.record_id}.md"
    assert export_path.exists()

    record.status = "rejected"
    record.meta["quality"]["capture_decision"] = "reject"
    runtime.store.append(record)

    assert not export_path.exists()


def test_qmd_markdown_export_skips_internal_control_plane_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.evolution.log_reflection(
        tag="reply-style",
        miss="Forgot concise style",
        fix="Reply with one sentence",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    runtime.evolution.store_rule(
        title="Task context first",
        summary="Prefer task context",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        status="active",
    )
    export_dir = tmp_path / "runtime" / "qmd" / "records"

    exported_files = sorted(path.name for path in export_dir.glob("*.md")) if export_dir.exists() else []
    assert exported_files == []


def test_qmd_update_skips_non_utf8_files_and_keeps_indexing(tmp_path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    notes = workspace / "notes"
    notes.mkdir()
    (notes / "good.md").write_text("# Good\n\nIndex me\n", encoding="utf-8")
    (notes / "bad.md").write_bytes(b"\xff\xfe\xfd")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    assert cli_main(["qmd", "collection", "add", str(notes), "--name", "notes", "--mask", "*.md"]) == 0
    assert cli_main(["qmd", "update"]) == 0
    assert cli_main(["qmd", "search", "Index me", "--json", "-n", "5"]) == 0

    output = capsys.readouterr().out
    assert '"skipped": 1' in output
    assert "Index me" in output


def test_cli_reflect_log_read_stats_and_check(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.evolution.observe(
        signal_type="incident",
        payload={
            "incident_type": "reply_too_long",
            "title": "Long reply incident",
            "summary": "Need shorter answers",
        },
        scope={"agent_id": "main", "workspace_id": ""},
    )

    assert cli_main(["reflect", "check"]) == 0
    assert cli_main(["reflect", "log", "reply-style", "Forgot concise style", "Reply with one sentence"]) == 0
    assert cli_main(["reflect", "read", "3"]) == 0
    assert cli_main(["reflect", "stats"]) == 0

    output = capsys.readouterr().out
    assert "ALERT" in output
    assert "reply-style" in output
    assert "reflection_count" in output


def test_http_rpc_server_returns_400_on_invalid_json(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        request = urllib.request.Request(
            f"http://{server.address[0]}:{server.address[1]}/",
            data=b"{bad json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode("utf-8"))
        else:
            raise AssertionError("expected malformed JSON request to fail")
    finally:
        server.stop()

    assert body["ok"] is False
    assert body["error"] == "invalid_request"


def test_cli_reflect_read_rejects_invalid_count(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    exit_code = cli_main(["reflect", "read", "many"])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "invalid count" in output


def test_cli_backup_create_and_verify_reports_corruption(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    backup_base = tmp_path / "cli-backup"

    assert cli_main(["backup", "create", str(backup_base)]) == 0
    capsys.readouterr()
    (tmp_path / "cli-backup.jsonl").write_text("broken\n", encoding="utf-8")

    exit_code = cli_main(["backup", "verify", str(backup_base)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["errors"]


def test_cli_openclaw_hook_bridge_reads_stdin_and_returns_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    stdin = io.StringIO(
        json.dumps(
            {
                "session_id": "sess-1",
                "agent_id": "main",
                "workspace_id": "repo-x",
                "message": {"role": "user", "content": "Remember bridge-driven memory capture."},
            }
        )
    )
    previous_stdin = sys.stdin
    sys.stdin = stdin
    try:
        assert cli_main(["openclaw-hook", "message_received"]) == 0
    finally:
        sys.stdin = previous_stdin

    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"]["kind"] == "memory"

    stdin = io.StringIO(
        json.dumps(
            {
                "session_id": "sess-1",
                "agent_id": "main",
                "workspace_id": "repo-x",
                "task_context": {"task_type": "chat.reply", "goal": "answer"},
                "query": "bridge-driven memory capture",
            }
        )
    )
    previous_stdin = sys.stdin
    sys.stdin = stdin
    try:
        assert cli_main(["openclaw-hook", "before_prompt_build"]) == 0
    finally:
        sys.stdin = previous_stdin

    bundle = json.loads(capsys.readouterr().out)
    assert bundle["memory_bundle"]["items"]

    stdin = io.StringIO(
        json.dumps(
            {
                "session_id": "sess-1",
                "agent_id": "main",
                "workspace_id": "repo-x",
                "user_messages": [{"content": "请帮我巡检 OpenClaw 队列"}],
                "assistant_messages": [{"content": "Summary: 队列已恢复。"}],
                "task_context": {
                    "event_type": "operational_check",
                    "interpreted_intent": "巡检 OpenClaw 队列",
                    "verification": "队列恢复",
                },
                "outcome": {"success": True, "verified": True},
            }
        )
    )
    previous_stdin = sys.stdin
    sys.stdin = stdin
    try:
        assert cli_main(["openclaw-hook", "task_end"]) == 0
    finally:
        sys.stdin = previous_stdin

    terminal = json.loads(capsys.readouterr().out)
    assert terminal["event"]["user_phrase"] == "请帮我巡检 OpenClaw 队列"
    assert terminal["event"]["event_type"] == "operational_check"
    assert terminal["outcome"]["outcome"] == "good"


def test_openclaw_js_bridge_before_prompt_build_defaults_recall_context(tmp_path) -> None:
    hook_script = tmp_path / "capture-openclaw-context.js"
    capture_path = tmp_path / "captured-payload.json"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
fs.writeFileSync(process.env.CAPTURE_PATH, JSON.stringify(payload));
process.stdout.write(JSON.stringify({
  usage_telemetry: {},
  memory_bundle: {
    items: [{ title: 'Bridge smoke', summary: 'context forwarded' }],
    rules: [],
    reflections: [],
    confidence: 0.5,
    next_action_hint: '',
    explanation: {}
  },
}));
""".strip(),
        encoding="utf-8",
    )
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({
  query: 'platform recall path',
  task_context: { task_type: 'chat.reply' },
  session_id: 'sess-platform',
  user_id: 'darrow',
  tenant_id: 'tenant-a',
})
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    env["CAPTURE_PATH"] = str(capture_path)
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(capture_path.read_text(encoding="utf-8"))
    assert payload["task_context"]["recall_mode"] == "fast"
    assert payload["task_context"]["recall_budget_ms"] == 800
    assert payload["task_context"]["candidate_limit"] == 24
    assert "Relevant eimemory context" in json.loads(result.stdout or "{}")["prependContext"]


def test_cli_openclaw_hook_reports_rejected_message_without_persisting(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    stdin = io.StringIO(
        json.dumps(
            {
                "session_id": "sess-1",
                "agent_id": "main",
                "workspace_id": "repo-x",
                "message": {"role": "user", "content": "测试eimemory"},
            }
        )
    )
    previous_stdin = sys.stdin
    sys.stdin = stdin
    try:
        assert cli_main(["openclaw-hook", "message_received"]) == 0
    finally:
        sys.stdin = previous_stdin

    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] is None
    assert payload["rejected"]["status"] == "rejected"
    assert payload["rejected"]["meta"]["quality"]["capture_decision"] == "reject"

    runtime = Runtime.create(root=tmp_path / "runtime")
    records = runtime.store.list_records(limit=20)
    assert records == []


def test_openclaw_bridge_assets_exist() -> None:
    assert Path("integrations/openclaw/eimemory-bridge/index.js").exists()
    manifest = json.loads(Path("integrations/openclaw/eimemory-bridge/openclaw.plugin.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "eimemory-bridge"
    assert manifest["activation"] == {"onStartup": True, "onCapabilities": ["hook"]}
    assert manifest["hooks"] == ["message_received", "before_prompt_build", "agent_end", "session_end"]
    assert manifest["contracts"]["tools"] == ["eimemory_bridge_status", "memory_e2e_check"]
    assert manifest["configSchema"]["type"] == "object"
    assert Path("integrations/openclaw/eimemory-bridge/package.json").exists()


def test_openclaw_js_bridge_registers_modern_typed_hooks_without_prompt_injection_by_default() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const names = [];
plugin.register({ hooks: { on(name, handler) { names.push(name); } } });
process.stdout.write(JSON.stringify(names));
""".strip()
    result = subprocess.run(["node", "-e", script], cwd=Path.cwd(), capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == ["message_received", "agent_end", "session_end"]


def test_openclaw_js_bridge_registers_before_prompt_build_only_when_enabled() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const names = [];
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, hooks: { on(name, handler) { names.push(name); } } });
process.stdout.write(JSON.stringify(names));
""".strip()
    result = subprocess.run(["node", "-e", script], cwd=Path.cwd(), capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == ["message_received", "before_prompt_build", "agent_end", "session_end"]


def test_openclaw_js_bridge_reads_openclaw_prompt_injection_policy(tmp_path) -> None:
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps({"plugins": {"entries": {"eimemory-bridge": {"hooks": {"allowPromptInjection": True}}}}}),
        encoding="utf-8",
    )
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const names = [];
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ hooks: { on(name, handler) { names.push(name); } } });
process.stdout.write(JSON.stringify(names));
""".strip()
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    result = subprocess.run(["node", "-e", script], cwd=Path.cwd(), env=env, capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == ["message_received", "before_prompt_build", "agent_end", "session_end"]


def test_openclaw_js_bridge_registers_status_tool() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const names = [];
plugin.register({
  registerTool(factory, opts) {
    const tool = factory();
    names.push(opts.name);
    names.push(tool.name);
    names.push(Array.isArray(tool.parameters.required) ? 'required-array' : 'missing-required');
  },
  on() {}
});
process.stdout.write(JSON.stringify(names));
""".strip()
    result = subprocess.run(["node", "-e", script], cwd=Path.cwd(), capture_output=True, text=True, check=True)

    assert json.loads(result.stdout) == ["eimemory_bridge_status", "eimemory_bridge_status", "required-array"]


def test_openclaw_js_bridge_status_tool_returns_json() -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
let statusTool;
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({
  config: { allowPromptInjection: true },
  registerTool(factory) {
    statusTool = factory();
  },
  on() {}
});
statusTool.execute().then((result) => { process.stdout.write(JSON.stringify(result)); });
""".strip()
    result = subprocess.run(["node", "-e", script], cwd=Path.cwd(), capture_output=True, text=True, check=True)

    tool_result = json.loads(result.stdout)
    payload = json.loads(tool_result["content"][0]["text"])
    assert tool_result["details"] == payload
    assert payload["ok"] is True
    assert payload["promptInjectionEnvEnabled"] is True
    assert payload["allowPromptInjection"] is True
    assert payload["promptInjectionEnabled"] is True


def test_openclaw_js_bridge_degrades_gracefully_on_hook_failure(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: 'hello' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = "does-not-exist openclaw-hook"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout or "{}") == {}


def test_openclaw_js_bridge_degrades_gracefully_on_malformed_hook_output(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.message_received({ content: 'Remember bridge capture should not throw.', captureMemory: true })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "bad-json-hook.js"
    hook_script.write_text(
        "process.stdout.write('not json');",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout or "{}") == {}


def test_openclaw_js_bridge_preserves_camel_case_explicit_capture_flag(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.message_received({ content: 'ok', captureMemory: true })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "capture-message.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({ stored: { capture_memory: payload.capture_memory, text: payload.message.content } }));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert payload["stored"]["capture_memory"] is True
    assert payload["stored"]["text"] == "ok"


def test_openclaw_js_bridge_supports_quoted_hook_command(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: 'quoted bridge memory' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "echo hook.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const hook = process.argv[2] || '';
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
if (hook === 'before_prompt_build') {
  process.stdout.write(JSON.stringify({ memory_bundle: { items: [{ title: 'Quoted command', summary: payload.query || '' }] } }));
} else {
  process.stdout.write(JSON.stringify({ stored: null }));
}
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "Quoted command" in payload["prependContext"]


def test_openclaw_js_bridge_injects_live_eibrain_context_from_feishu_bridge(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: '现在看到了什么', senderId: 'ou_user' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    bridge_script = tmp_path / "bridge.js"
    bridge_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({
  matched: true,
  reply: '已完成：视觉状态：live；识别到：person、keyboard',
  prepend_context: `实时 eibrain 视觉上下文：${payload.query}`,
}));
""".strip(),
        encoding="utf-8",
    )
    hook_script = tmp_path / "empty-hook.js"
    hook_script.write_text("process.stdout.write(JSON.stringify({ memory_bundle: { items: [] } }));", encoding="utf-8")
    env = os.environ.copy()
    env["EIMEMORY_BRIDGE_COMMAND"] = f'node "{bridge_script}"'
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "Live eibrain context" in payload["prependContext"]
    assert "实时 eibrain 视觉上下文：现在看到了什么" in payload["prependContext"]


def test_openclaw_js_bridge_filters_ei_bridge_audit_from_memory_context(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: 'what do you see', senderId: 'ou_user' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    bridge_script = tmp_path / "bridge.js"
    bridge_script.write_text(
        "process.stdout.write(JSON.stringify({ matched: true, prepend_context: 'live scene: person' }));",
        encoding="utf-8",
    )
    hook_script = tmp_path / "memory-hook.js"
    hook_script.write_text(
        """
process.stdout.write(JSON.stringify({
  memory_bundle: {
    items: [
      { title: 'ei-bridge OpenClaw command audit', source: 'ei_bridge.openclaw_feishu', summary: 'noisy audit' },
      { title: 'Useful memory', source: 'openclaw.message_received', summary: 'operator prefers concise replies' }
    ]
  }
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_BRIDGE_COMMAND"] = f'node "{bridge_script}"'
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "live scene: person" in payload["prependContext"]
    assert "Useful memory" in payload["prependContext"]
    assert "noisy audit" not in payload["prependContext"]


def test_openclaw_js_bridge_injects_policy_suggestions_before_memory_items(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: '给我唱首歌', senderId: 'ou_user' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    bridge_script = tmp_path / "bridge.js"
    bridge_script.write_text("process.stdout.write(JSON.stringify({ matched: false }));", encoding="utf-8")
    hook_script = tmp_path / "policy-memory-hook.js"
    hook_script.write_text(
        """
process.stdout.write(JSON.stringify({
  memory_bundle: {
    items: [
      { title: 'Generic song chat', source: 'openclaw.message_received', summary: 'This looks like a lyric-writing memory.' }
    ],
    explanation: {
      policy_suggestions: [
        {
          source: 'intent_pattern',
          event_type: 'media_playback',
          success_criteria: '用户能听到或打开播放',
          execution_policy: ['先判断播放出口和物理条件', '再确认歌曲和播放方式']
        }
      ]
    }
  }
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_BRIDGE_COMMAND"] = f'node "{bridge_script}"'
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    context = json.loads(result.stdout or "{}")["prependContext"]
    assert context.index("policy_suggestions") < context.index("Generic song chat")
    assert "event_type: media_playback" in context
    assert "success_criteria: 用户能听到或打开播放" in context
    assert "execution_policy: 先判断播放出口和物理条件; 再确认歌曲和播放方式" in context


def test_openclaw_js_bridge_enforces_injection_plan_lanes(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: 'health status', senderId: 'ou_user' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    bridge_script = tmp_path / "bridge.js"
    bridge_script.write_text("process.stdout.write(JSON.stringify({ matched: false }));", encoding="utf-8")
    hook_script = tmp_path / "injection-plan-hook.js"
    hook_script.write_text(
        """
process.stdout.write(JSON.stringify({
  injection_plan: {
    mode: 'strict',
    items: [
      { record_id: 'full-1', action: 'full_text' },
      { record_id: 'summary-1', action: 'summary_only' },
      { record_id: 'policy-1', action: 'policy_only' },
      { record_id: 'withheld-1', action: 'withheld', reason: 'blocked_recall_lane' }
    ]
  },
  memory_bundle: {
    items: [
      { record_id: 'full-1', title: 'Full preference', summary: 'short summary', content: { text: 'FULL TEXT DETAIL' } },
      { record_id: 'summary-1', title: 'Summary fact', summary: 'SUMMARY ONLY', content: { text: 'SHOULD NOT USE FULL TEXT' } },
      { record_id: 'policy-1', kind: 'rule', title: 'Policy rule', summary: 'POLICY SUMMARY', content: { text: 'SHOULD NOT USE RULE FULL TEXT' } },
      { record_id: 'withheld-1', title: 'Old incident', summary: 'WITHHELD SUMMARY', content: { text: 'WITHHELD FULL TEXT' } }
    ],
    explanation: {}
  }
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_BRIDGE_COMMAND"] = f'node "{bridge_script}"'
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    context = json.loads(result.stdout or "{}")["prependContext"]
    assert "Full preference: FULL TEXT DETAIL" in context
    assert "Summary fact: SUMMARY ONLY" in context
    assert "SHOULD NOT USE FULL TEXT" not in context
    assert "policy_only:" in context
    assert "Policy rule: POLICY SUMMARY" in context
    assert "SHOULD NOT USE RULE FULL TEXT" not in context
    assert "Old incident" not in context
    assert "WITHHELD" not in context


def test_openclaw_js_bridge_normalizes_agent_end_message_content(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.agent_end({
  agentId: 'main',
  workspaceId: 'repo-x',
  success: true,
  messages: [
    { role: 'assistant', content: [{ type: 'text', text: 'First part' }, { type: 'text', text: 'Second part' }] }
  ]
})
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "echo-agent-end.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({ stored: { summary: payload.assistant_messages?.[0]?.content || '' } }));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert payload["stored"]["summary"] == "First part\nSecond part"


def test_openclaw_js_bridge_agent_end_forwards_event_policy_context(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.agent_end({
  agentId: 'main',
  workspaceId: 'repo-x',
  query: '请帮我巡检 OpenClaw 队列',
  taskContext: {
    event_type: 'operational_check',
    interpreted_intent: '巡检 OpenClaw 队列并处理卡住任务',
    verification: '队列恢复'
  },
  tools: ['openclaw_status', 'systemctl'],
  actionPath: ['检查队列', '查看日志', '复查状态'],
  success: true,
  verified: true,
  messages: [
    { role: 'user', content: '请帮我巡检 OpenClaw 队列' },
    { role: 'assistant', content: 'Summary: OpenClaw 队列已恢复。' }
  ]
})
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "capture-agent-end-policy-context.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({
  event: {
    user_messages: payload.user_messages || [],
    query: payload.query || '',
    task_context: payload.task_context || {},
    tools: payload.tools || [],
    action_path: payload.action_path || [],
    outcome: payload.outcome || {}
  }
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    event = payload["event"]
    assert event["user_messages"] == [{"content": "请帮我巡检 OpenClaw 队列"}]
    assert event["query"] == "请帮我巡检 OpenClaw 队列"
    assert event["task_context"]["event_type"] == "operational_check"
    assert event["tools"] == ["openclaw_status", "systemctl"]
    assert event["action_path"] == ["检查队列", "查看日志", "复查状态"]
    assert event["outcome"]["success"] is True
    assert event["outcome"]["verified"] is True
    assert event["outcome"]["verification"] == "队列恢复"


def test_openclaw_js_bridge_sends_clean_user_query_from_feishu_prompt(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({
  agentId: 'main',
  prompt: `System: [2026-04-21 05:05:10 UTC] Feishu[default] DM | user [msg:abc]

Conversation info (untrusted metadata):
\\`\\`\\`json
{"chat_id":"user:abc","message_id":"abc"}
\\`\\`\\`

Sender (untrusted metadata):
\\`\\`\\`json
{"id":"abc"}
\\`\\`\\`

暂时没有新的计划，我在调试你的长期记忆系统`
})
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "capture-query.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({
  memory_bundle: {
    items: [{ title: 'Captured query', summary: payload.query }],
  },
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "暂时没有新的计划，我在调试你的长期记忆系统" in payload["prependContext"]
    assert "Conversation info" not in payload["prependContext"]


def test_openclaw_js_bridge_preserves_raw_query_and_scope(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({
  tenantId: 'tenant-a',
  userId: 'user-a',
  agentId: 'main',
  workspaceId: 'repo-x',
  prompt: `System: wrapper

Conversation info:
\\`\\`\\`json
{"message_id":"abc"}
\\`\\`\\`

debug deployment memory`
})
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "capture-raw-query.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({
  memory_bundle: {
    items: [{
      title: payload.tenant_id + '/' + payload.user_id + '/' + payload.agent_id + '/' + payload.workspace_id,
      summary: payload.query + '|' + (payload.raw_query || ''),
    }],
  },
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "tenant-a/user-a/hongtu/embodied" in payload["prependContext"]
    assert "debug deployment memory|System: wrapper" in payload["prependContext"]
    assert "Conversation info" in payload["prependContext"]


def test_openclaw_js_bridge_derives_feishu_session_and_user_from_prompt_metadata(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({
  agentId: 'main',
  prompt: `System: [2026-04-21 05:31:46 UTC] Feishu[default] DM | ou_sender [msg:om_123]

Conversation info (untrusted metadata):
\\`\\`\\`json
{"chat_id":"user:ou_sender","message_id":"om_123","sender_id":"ou_sender"}
\\`\\`\\`

Sender (untrusted metadata):
\\`\\`\\`json
{"id":"ou_sender","name":"ou_sender"}
\\`\\`\\`

从现在开始你有自己专属的记忆系统了eimemory`
})
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "capture-feishu-scope.js"
    hook_script.write_text(
        """
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
process.stdout.write(JSON.stringify({
  memory_bundle: {
    items: [{
      title: payload.session_id,
      summary: payload.user_id + '|' + payload.query,
    }],
  },
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "feishu:user:ou_sender" in payload["prependContext"]
    assert "ou_sender|从现在开始你有自己专属的记忆系统了eimemory" in payload["prependContext"]


def test_openclaw_js_bridge_strips_thinking_from_injected_context(tmp_path) -> None:
    script = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
process.env.EIMEMORY_ENABLE_PROMPT_INJECTION = 'true';
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
handlers.before_prompt_build({ prompt: 'memory' })
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((error) => { console.error(error && error.stack ? error.stack : String(error)); process.exit(1); });
""".strip()
    hook_script = tmp_path / "thinking-context.js"
    hook_script.write_text(
        """
process.stdout.write(JSON.stringify({
  memory_bundle: {
    items: [{
      title: 'Agent outcome',
      summary: '{"type":"thinking","thinking":"internal trace","thinkingSignature":"abc"}\\nVisible answer',
    }],
  },
}));
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_HOOK_COMMAND"] = f'node "{hook_script}"'
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout or "{}")
    assert "Visible answer" in payload["prependContext"]
    assert "thinkingSignature" not in payload["prependContext"]
    assert "internal trace" not in payload["prependContext"]
