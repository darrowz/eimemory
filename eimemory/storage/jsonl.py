from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Iterable, Iterator

from eimemory.models.records import RecordEnvelope
from eimemory.storage.atomic_file import interprocess_lock


DEFAULT_SEGMENT_MAX_BYTES = 64 * 1024 * 1024


def canonical_payload_json(payload: dict) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_digest(payload: dict) -> str:
    return sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JsonlScanEntry:
    payload: dict
    path: Path
    line: int
    offset: int
    operation_id: str
    payload_digest: str


class JsonlScanError(ValueError):
    def __init__(self, *, path: Path, line: int, offset: int, error: str) -> None:
        self.report = {
            "path": str(path),
            "line": int(line),
            "offset": int(offset),
            "error": str(error),
        }
        super().__init__(
            f"invalid JSONL at {path}:{line} offset {offset}: {error}"
        )


def scan_jsonl_strict(
    paths: str | Path | Iterable[str | Path],
) -> Iterator[JsonlScanEntry]:
    """Stream JSONL rows once and fail at the first malformed or forged row."""

    if isinstance(paths, (str, Path)):
        candidates = [Path(paths)]
    else:
        candidates = [Path(path) for path in paths]
    for path in candidates:
        if not path.exists():
            continue
        with path.open("rb") as handle:
            line_number = 0
            while True:
                offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                line_number += 1
                if not raw_line.strip():
                    continue
                try:
                    text = raw_line.decode("utf-8")
                    raw_payload = json.loads(text)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise JsonlScanError(
                        path=path,
                        line=line_number,
                        offset=offset,
                        error=str(exc),
                    ) from exc
                if not isinstance(raw_payload, dict):
                    raise JsonlScanError(
                        path=path,
                        line=line_number,
                        offset=offset,
                        error="row must be a JSON object",
                    )
                operation_id = str(raw_payload.pop("_operation_id", "") or "")
                claimed_digest = str(raw_payload.pop("_payload_digest", "") or "")
                actual_digest = payload_digest(raw_payload)
                if claimed_digest and claimed_digest != actual_digest:
                    raise JsonlScanError(
                        path=path,
                        line=line_number,
                        offset=offset,
                        error="payload digest mismatch",
                    )
                yield JsonlScanEntry(
                    payload=raw_payload,
                    path=path,
                    line=line_number,
                    offset=offset,
                    operation_id=operation_id,
                    payload_digest=claimed_digest or actual_digest,
                )


def iter_jsonl_payloads(path: str | Path) -> Iterator[dict]:
    """Stream valid payloads from every segment, skipping isolated bad rows.

    Recovery uses :func:`scan_jsonl_strict` and fails closed. Analytical and
    autonomous consumers use this iterator so one historical corrupt row does
    not stop a long-running loop, while forged digest rows are never accepted.
    """

    log = JsonlLog(Path(path))
    for segment in log.segment_paths():
        with segment.open("rb") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                try:
                    raw_payload = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(raw_payload, dict):
                    continue
                clean_payload = dict(raw_payload)
                clean_payload.pop("_operation_id", None)
                claimed_digest = str(clean_payload.pop("_payload_digest", "") or "")
                if claimed_digest and claimed_digest != payload_digest(clean_payload):
                    continue
                yield clean_payload


class JsonlLog:
    def __init__(
        self,
        path: Path,
        *,
        max_segment_bytes: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        configured = (
            os.environ.get("EIMEMORY_JSONL_SEGMENT_MAX_BYTES")
            if max_segment_bytes is None
            else max_segment_bytes
        )
        try:
            resolved = int(configured) if configured is not None else DEFAULT_SEGMENT_MAX_BYTES
        except (TypeError, ValueError):
            resolved = DEFAULT_SEGMENT_MAX_BYTES
        self.max_segment_bytes = max(256, resolved)

    def append(self, record: RecordEnvelope) -> None:
        self.append_payload(record.to_dict())

    def append_payload(
        self,
        payload: dict,
        *,
        operation_id: str = "",
        expected_digest: str = "",
    ) -> str:
        clean_payload = dict(payload)
        clean_payload.pop("_operation_id", None)
        clean_payload.pop("_payload_digest", None)
        digest = payload_digest(clean_payload)
        if expected_digest and expected_digest != digest:
            raise ValueError("outbox payload digest mismatch")
        envelope = {
            **({"_operation_id": str(operation_id)} if operation_id else {}),
            **({"_payload_digest": digest} if operation_id else {}),
            **clean_payload,
        }
        encoded = (
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with interprocess_lock(lock_path):
            self._rotate_if_needed(len(encoded))
            with self.path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return digest

    def segment_paths(self) -> list[Path]:
        archived = sorted(
            self.path.parent.glob(f"{self.path.stem}.[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]{self.path.suffix}")
        )
        return [*archived, *([self.path] if self.path.exists() else [])]

    def scan_strict(self) -> Iterator[JsonlScanEntry]:
        return scan_jsonl_strict(self.segment_paths())

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_segment_bytes:
            return
        sequence = 1
        existing = self.segment_paths()
        if existing:
            archived = [path for path in existing if path != self.path]
            if archived:
                try:
                    sequence = int(archived[-1].stem.rsplit(".", 1)[-1]) + 1
                except ValueError:
                    sequence = len(archived) + 1
        target = self.path.with_name(
            f"{self.path.stem}.{sequence:08d}{self.path.suffix}"
        )
        os.replace(self.path, target)
