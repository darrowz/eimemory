from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake.papers.normalize import normalize_paper_input
from eimemory.intake.papers.sources import paper_source_from_payload


def test_normalize_arxiv_input_to_paper_source_payload() -> None:
    payload = normalize_paper_input({"arxiv_id": "2501.12345"})

    assert payload["source_kind"] == "arxiv"
    assert payload["arxiv_id"] == "2501.12345"


def test_normalize_pdf_input_to_paper_source_payload(tmp_path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    payload = normalize_paper_input({"pdf_file": str(pdf_path)})

    assert payload["source_kind"] == "pdf"
    assert payload["pdf_path"] == str(pdf_path)


def test_normalize_pdf_input_keeps_source_hash_stable_across_local_paths(tmp_path) -> None:
    pdf_path_one = tmp_path / "first" / "paper.pdf"
    pdf_path_one.parent.mkdir(parents=True)
    pdf_path_one.write_bytes(b"%PDF-1.4 fake")

    pdf_path_two = tmp_path / "second" / "paper.pdf"
    pdf_path_two.parent.mkdir(parents=True)
    pdf_path_two.write_bytes(b"%PDF-1.4 fake")

    payload_one = normalize_paper_input({"pdf_file": str(pdf_path_one)})
    payload_two = normalize_paper_input({"pdf_file": str(pdf_path_two)})
    source_one = paper_source_from_payload(payload_one)
    source_two = paper_source_from_payload(payload_two)

    assert payload_one["source_hash"] == payload_two["source_hash"]
    assert source_one.paper_source_id == source_two.paper_source_id


def test_normalize_pdf_input_distinguishes_different_pdf_content(tmp_path) -> None:
    pdf_path_one = tmp_path / "first" / "paper.pdf"
    pdf_path_one.parent.mkdir(parents=True)
    pdf_path_one.write_bytes(b"%PDF-1.4 first")

    pdf_path_two = tmp_path / "second" / "paper.pdf"
    pdf_path_two.parent.mkdir(parents=True)
    pdf_path_two.write_bytes(b"%PDF-1.4 second")

    payload_one = normalize_paper_input({"pdf_file": str(pdf_path_one)})
    payload_two = normalize_paper_input({"pdf_file": str(pdf_path_two)})

    assert payload_one["source_hash"] != payload_two["source_hash"]
    assert paper_source_from_payload(payload_one).paper_source_id != paper_source_from_payload(payload_two).paper_source_id


def test_normalize_pdf_hashes_content_without_reading_whole_file(tmp_path, monkeypatch) -> None:
    pdf_path = tmp_path / "large.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024))

    def reject_read_bytes(_path):
        raise AssertionError("paper hashing must stream file content")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    payload = normalize_paper_input({"pdf_file": str(pdf_path)})

    assert len(payload["source_hash"]) == 64


def test_runtime_can_persist_paper_source(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    record = runtime.ingest_paper_source({"arxiv_id": "2501.12345"}, scope={"agent_id": "main"})

    assert record.kind == "paper_source"
    assert record.content["source_kind"] == "arxiv"
    assert record.content["arxiv_id"] == "2501.12345"


def test_normalized_metadata_survives_paper_source_round_trip(tmp_path) -> None:
    payload = normalize_paper_input({"arxiv_id": "2501.12345"})
    paper_source = paper_source_from_payload(payload)
    runtime = Runtime.create(root=tmp_path)
    record = runtime.ingest_paper_source({"arxiv_id": "2501.12345"}, scope={"agent_id": "main"})

    assert paper_source.metadata["enrichment_state"]["title"] == "pending"
    assert record.content["metadata"]["enrichment_state"]["title"] == "pending"
    assert record.content["metadata"]["authors"] == []


def test_reingesting_same_paper_reuses_record_identity(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}

    first = runtime.ingest_paper_source({"arxiv_id": "2501.12345"}, scope=scope)
    second = runtime.ingest_paper_source({"arxiv_id": "2501.12345"}, scope=scope)

    records = runtime.store.list_records(kinds=["paper_source"], scope=scope)

    assert first.record_id == second.record_id
    assert len(records) == 1
    assert records[0].record_id == first.record_id


def test_paper_source_payload_is_effectively_immutable() -> None:
    payload = normalize_paper_input(
        {
            "arxiv_id": "2501.12345",
            "metadata": {"client": {"editable": True}},
            "provenance": {"ingest": {"source": "manual"}},
        }
    )
    payload["metadata"]["enrichment_state"]["title"] = "complete"
    payload["provenance"]["input_keys"].append("mutated")

    paper_source = paper_source_from_payload(payload)

    with pytest.raises(TypeError):
        paper_source.metadata["extra"] = "value"
    with pytest.raises(TypeError):
        paper_source.metadata["enrichment_state"]["title"] = "mutated"
    with pytest.raises(TypeError):
        paper_source.provenance["input_keys"] += ("another",)

    round_trip = paper_source.to_payload()
    round_trip["metadata"]["enrichment_state"]["title"] = "changed"
    round_trip["provenance"]["input_keys"].append("changed")

    assert paper_source.metadata["enrichment_state"]["title"] == "complete"
    assert "changed" not in paper_source.provenance["input_keys"]


def test_normalized_metadata_uses_canonical_values(tmp_path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    payload = normalize_paper_input(
        {
            "paper_url": "https://example.com/papers/canonical",
            "pdf_file": str(pdf_path),
            "authors": ["Ada Lovelace"],
        }
    )

    assert payload["metadata"]["canonical_url"] == payload["canonical_url"]
    assert payload["metadata"]["pdf_blob_ref"] == payload["pdf_blob_ref"]
    assert payload["metadata"]["authors"] == payload["authors"]


@pytest.mark.parametrize(
    ("first_input", "second_input", "field", "expected"),
    [
        (
            {"doi": "10.48550/ARXIV.2501.12345"},
            {"doi": " https://doi.org/10.48550/arxiv.2501.12345 "},
            "doi",
            "10.48550/arxiv.2501.12345",
        ),
        (
            {"arxiv_id": "arXiv:2501.12345v2"},
            {"arxiv_id": " https://arxiv.org/abs/2501.12345v2 "},
            "arxiv_id",
            "2501.12345v2",
        ),
        (
            {"url": "HTTPS://Example.COM:443/papers?id=123#section"},
            {"paper_url": "https://example.com/papers?id=123"},
            "canonical_url",
            "https://example.com/papers?id=123",
        ),
    ],
)
def test_equivalent_identifier_inputs_share_canonical_identity(
    tmp_path, first_input, second_input, field, expected
) -> None:
    first_payload = normalize_paper_input(first_input)
    second_payload = normalize_paper_input(second_input)

    assert first_payload[field] == expected
    assert second_payload[field] == expected
    assert first_payload["source_hash"] == second_payload["source_hash"]
    assert paper_source_from_payload(first_payload).paper_source_id == paper_source_from_payload(second_payload).paper_source_id

    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    first_record = runtime.ingest_paper_source(first_input, scope=scope)
    second_record = runtime.ingest_paper_source(second_input, scope=scope)

    assert first_record.record_id == second_record.record_id
    assert len(runtime.store.list_records(kinds=["paper_source"], scope=scope)) == 1


def test_normalization_preserves_upstream_metadata_and_provenance() -> None:
    payload = normalize_paper_input(
        {
            "doi": "HTTPS://doi.org/10.48550/ARXIV.2501.12345",
            "metadata": {
                "client": {"editable": True},
                "enrichment_state": {"title": "complete"},
                "canonical_url": "https://override.invalid/ignored",
            },
            "provenance": {
                "ingest": {"source": "manual"},
                "input_keys": ["upstream"],
                "tags": ["trusted"],
            },
        }
    )

    assert payload["doi"] == "10.48550/arxiv.2501.12345"
    assert payload["metadata"]["doi"] == payload["doi"]
    assert payload["metadata"]["client"] == {"editable": True}
    assert payload["metadata"]["enrichment_state"]["title"] == "complete"
    assert payload["metadata"]["canonical_url"] == "https://override.invalid/ignored"
    assert payload["provenance"]["ingest"] == {"source": "manual"}
    assert payload["provenance"]["tags"] == ["trusted"]
    assert payload["provenance"]["input_kind"] == "paper_intake"
    assert "upstream" in payload["provenance"]["input_keys"]
    assert "doi" in payload["provenance"]["input_keys"]


def test_runtime_ingest_paper_source_sanitizes_metadata_for_json_persistence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    record = runtime.ingest_paper_source(
        {
            "arxiv_id": "2501.12345",
            "metadata": {
                "bad_set": {"zeta", "alpha"},
                "local_path": Path("papers/source.pdf"),
                "captured_at": datetime(2026, 4, 20, 12, 30, tzinfo=timezone.utc),
            },
            "provenance": {
                "seen": {"manual", "operator"},
                "path": Path("inputs/paper.pdf"),
            },
        },
        scope={"agent_id": "main"},
    )

    reloaded = runtime.store.get_by_id(record.record_id)

    assert reloaded is not None
    assert reloaded.content["metadata"]["bad_set"] == ["alpha", "zeta"]
    assert reloaded.content["metadata"]["local_path"] == str(Path("papers/source.pdf"))
    assert reloaded.content["metadata"]["captured_at"] == "2026-04-20T12:30:00+00:00"
    assert reloaded.content["provenance"]["seen"] == ["manual", "operator"]
