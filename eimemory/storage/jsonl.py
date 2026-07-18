from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from typing import Iterable, Iterator, Protocol
from uuid import uuid4

from eimemory.models.records import RecordEnvelope
from eimemory.storage.atomic_file import atomic_write_json, interprocess_lock


DEFAULT_SEGMENT_MAX_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MANIFEST_SEGMENTS = 100_000
DEFAULT_CLEANUP_GRACE_SECONDS = 60 * 60
GENERATION_TRANSACTION_NAME = "transaction.json"


class _DigestAccumulator(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    *,
    max_row_bytes: int = DEFAULT_SEGMENT_MAX_BYTES,
) -> Iterator[JsonlScanEntry]:
    """Stream JSONL rows once and fail at the first malformed or forged row."""

    row_limit = max(256, int(max_row_bytes))
    if isinstance(paths, (str, Path)):
        candidates = [Path(paths)]
        require_snapshot = False
    else:
        candidates = [Path(path) for path in paths]
        require_snapshot = True
    for path in candidates:
        if not path.exists():
            if require_snapshot:
                raise FileNotFoundError(f"JSONL segment disappeared: {path}")
            continue
        with path.open("rb") as handle:
            line_number = 0
            while True:
                offset = handle.tell()
                raw_line = handle.readline(row_limit + 1)
                if not raw_line:
                    break
                line_number += 1
                if len(raw_line) > row_limit:
                    raise JsonlScanError(
                        path=path,
                        line=line_number,
                        offset=offset,
                        error=f"row exceeds size limit of {row_limit} bytes",
                    )
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


def iter_jsonl_payloads(
    path: str | Path,
    *,
    max_row_bytes: int | None = None,
) -> Iterator[dict]:
    """Stream valid payloads from every segment, skipping isolated bad rows.

    Recovery uses :func:`scan_jsonl_strict` and fails closed. Analytical and
    autonomous consumers use this iterator so one historical corrupt row does
    not stop a long-running loop, while forged digest rows are never accepted.
    """

    log = JsonlLog(Path(path), create_parent=False)
    row_limit = (
        log.max_segment_bytes
        if max_row_bytes is None
        else max(256, int(max_row_bytes))
    )
    for segment in log.segment_paths():
        with segment.open("rb") as handle:
            while True:
                raw_line = handle.readline(row_limit + 1)
                if not raw_line:
                    break
                if len(raw_line) > row_limit:
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = handle.readline(row_limit + 1)
                    continue
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
        create_parent: bool = True,
        cleanup_grace_seconds: int | None = None,
    ) -> None:
        self.path = Path(path)
        if create_parent:
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
        configured_grace = (
            os.environ.get("EIMEMORY_JSONL_CLEANUP_GRACE_SECONDS")
            if cleanup_grace_seconds is None
            else cleanup_grace_seconds
        )
        try:
            grace = (
                int(configured_grace)
                if configured_grace is not None
                else DEFAULT_CLEANUP_GRACE_SECONDS
            )
        except (TypeError, ValueError):
            grace = DEFAULT_CLEANUP_GRACE_SECONDS
        self.cleanup_grace_seconds = max(0, grace)
        self.manifest_path = self.path.with_name(
            f"{self.path.stem}.segments.json"
        )
        self.manifest_backup_path = self.path.with_name(
            f"{self.path.stem}.segments.backup.json"
        )

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
        if len(encoded) > self.max_segment_bytes:
            raise ValueError("single JSONL row exceeds segment limit")
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with interprocess_lock(lock_path):
            self._rotate_if_needed(len(encoded))
            with self.path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return digest

    def segment_paths(self) -> list[Path]:
        manifest = self._read_manifest()
        if manifest is not None:
            paths = [
                self._manifest_segment_path(relative, must_exist=True)
                for relative in manifest["segments"]
            ]
            pending = manifest.get("pending_segment")
            if pending:
                pending_path = self._manifest_segment_path(
                    pending,
                    must_exist=False,
                )
                if pending_path.exists():
                    self._validate_regular_segment(pending_path)
                    paths.append(pending_path)
            if self.path.exists():
                self._validate_regular_segment(self.path)
                paths.append(self.path)
            return paths
        archived = sorted(
            self.path.parent.glob(f"{self.path.stem}.[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]{self.path.suffix}")
        )
        return [*archived, *([self.path] if self.path.exists() else [])]

    def scan_strict(self) -> Iterator[JsonlScanEntry]:
        return scan_jsonl_strict(
            self.segment_paths(),
            max_row_bytes=self.max_segment_bytes,
        )

    def resegment_oversized(self, *, force_cleanup: bool = False) -> dict:
        """Stream oversized historical segments into bounded, atomic generations.

        SQLite remains canonical while this derived log is rebuilt.  The manifest
        becomes visible only after every replacement segment has been flushed and
        its byte stream has been verified.  Superseded files stay hidden behind
        the manifest until cleanup succeeds, so a crash cannot expose duplicates.
        """

        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with interprocess_lock(lock_path):
            manifest = self._recover_pending_manifest(self._read_manifest())
            if manifest is not None and not self.manifest_backup_path.exists():
                self._write_manifest(manifest)
            cleanup = self._retry_manifest_cleanup(
                manifest,
                force=force_cleanup,
            )
            manifest = cleanup["manifest"]
            orphan_cleanup = self._cleanup_orphan_generations(manifest)

            # An active legacy file is first made an archived segment.  This keeps
            # the active filename out of the atomic manifest switch and prevents
            # duplicate visibility if cleanup is interrupted.
            if self.path.exists() and self.path.stat().st_size > self.max_segment_bytes:
                self._rotate_if_needed(1)
                manifest = self._read_manifest()

            visible = self.segment_paths()
            archived = [path for path in visible if path != self.path]
            oversized = [
                path
                for path in archived
                if path.stat().st_size > self.max_segment_bytes
            ]
            if not oversized:
                sizes = [path.stat().st_size for path in visible]
                return {
                    "ok": not cleanup["errors"] and not orphan_cleanup["errors"],
                    "changed": False,
                    "oversized_segment_count": 0,
                    "source_bytes": 0,
                    "replacement_bytes": 0,
                    "source_sha256": "",
                    "replacement_sha256": "",
                    "segment_count": len(visible),
                    "largest_segment_bytes": max(sizes, default=0),
                    "cleaned_source_count": cleanup["cleaned"],
                    "cleanup_deferred_count": cleanup["deferred"],
                    "cleanup_errors": cleanup["errors"],
                    "orphan_generation_count": orphan_cleanup["directories"],
                    "orphan_file_count": orphan_cleanup["files"],
                    "orphan_cleanup_errors": orphan_cleanup["errors"],
                }

            generation = self.path.parent / (
                f".{self.path.stem}.segments-{uuid4().hex}"
            )
            generation.mkdir(mode=0o700)
            self._write_generation_transaction(generation, state="staging")
            generated: list[Path] = []
            source_digest = sha256()
            replacement_digest = sha256()
            source_bytes = 0
            replacement_bytes = 0
            try:
                replacements: dict[Path, list[Path]] = {}
                for source in oversized:
                    chunks, metrics = self._split_segment_streaming(
                        source,
                        generation,
                        start_index=len(generated) + 1,
                        source_digest=source_digest,
                        replacement_digest=replacement_digest,
                    )
                    generated.extend(chunks)
                    replacements[source] = chunks
                    source_bytes += metrics["source_bytes"]
                    replacement_bytes += metrics["replacement_bytes"]

                if source_bytes != replacement_bytes:
                    raise OSError("JSONL resegmentation byte count mismatch")
                if source_digest.digest() != replacement_digest.digest():
                    raise OSError("JSONL resegmentation digest mismatch")

                ordered: list[Path] = []
                for source in archived:
                    ordered.extend(replacements.get(source, [source]))
                relative_segments = [
                    self._relative_manifest_name(path) for path in ordered
                ]
                superseded = [
                    self._relative_manifest_name(path) for path in oversized
                ]
                previous_cleanup = list(
                    (manifest or {}).get("cleanup_pending", [])
                )
                _fsync_directory(generation)
                _fsync_directory(self.path.parent)
                next_manifest = {
                    "version": 1,
                    "segments": relative_segments,
                    "pending_segment": None,
                    "cleanup_pending": list(
                        dict.fromkeys([*previous_cleanup, *superseded])
                    ),
                    "cleanup_not_before": (
                        0
                        if force_cleanup
                        else max(
                            float(
                                (manifest or {}).get("cleanup_not_before", 0)
                                or 0
                            ),
                            time.time() + self.cleanup_grace_seconds,
                        )
                    ),
                }
                self._write_generation_transaction(generation, state="prepared")
                self._write_manifest(next_manifest)
                self._write_generation_transaction(generation, state="activated")
            except BaseException:
                try:
                    activated = self._read_manifest()
                    active_names = set((activated or {}).get("segments", []))
                except (OSError, ValueError):
                    # The manifest is ambiguous, so preserving generated data is
                    # safer than deleting files it may already reference.
                    active_names = {
                        self._relative_manifest_name(path) for path in generated
                    }
                generation_is_active = any(
                    self._relative_manifest_name(path) in active_names
                    for path in generated
                )
                for generated_path in generated:
                    if self._relative_manifest_name(generated_path) not in active_names:
                        generated_path.unlink(missing_ok=True)
                if not generation_is_active:
                    (generation / GENERATION_TRANSACTION_NAME).unlink(
                        missing_ok=True
                    )
                try:
                    generation.rmdir()
                except OSError:
                    pass
                raise

            cleanup = self._retry_manifest_cleanup(
                self._read_manifest(),
                force=force_cleanup,
            )
            final_paths = self.segment_paths()
            sizes = [path.stat().st_size for path in final_paths]
            return {
                "ok": not cleanup["errors"] and not orphan_cleanup["errors"],
                "changed": True,
                "oversized_segment_count": len(oversized),
                "source_bytes": source_bytes,
                "replacement_bytes": replacement_bytes,
                "source_sha256": source_digest.hexdigest(),
                "replacement_sha256": replacement_digest.hexdigest(),
                "segment_count": len(final_paths),
                "largest_segment_bytes": max(sizes, default=0),
                "cleaned_source_count": cleanup["cleaned"],
                "cleanup_deferred_count": cleanup["deferred"],
                "cleanup_errors": cleanup["errors"],
                "orphan_generation_count": orphan_cleanup["directories"],
                "orphan_file_count": orphan_cleanup["files"],
                "orphan_cleanup_errors": orphan_cleanup["errors"],
            }

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        manifest = self._recover_pending_manifest(self._read_manifest())
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_segment_bytes:
            return
        if manifest is not None:
            target = self.path.with_name(
                f"{self.path.stem}.segment-{uuid4().hex}{self.path.suffix}"
            )
            relative = self._relative_manifest_name(target)
            pending = {
                **manifest,
                "pending_segment": relative,
            }
            self._write_manifest(pending)
            os.replace(self.path, target)
            self._write_manifest(
                {
                    **pending,
                    "segments": [*pending["segments"], relative],
                    "pending_segment": None,
                },
            )
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

    def _read_manifest(self) -> dict | None:
        primary_error: ValueError | None = None
        if self.manifest_path.exists() or self.manifest_path.is_symlink():
            try:
                return self._load_manifest(self.manifest_path)
            except ValueError as exc:
                primary_error = exc
        if self.manifest_backup_path.exists() or self.manifest_backup_path.is_symlink():
            try:
                recovered = self._load_manifest(self.manifest_backup_path)
            except ValueError:
                if primary_error is not None:
                    raise primary_error
                raise
            atomic_write_json(self.manifest_path, recovered)
            return recovered
        if primary_error is not None:
            raise primary_error
        self._assert_no_activated_segments_without_manifest()
        return None

    def _load_manifest(self, path: Path) -> dict:
        if path.is_symlink():
            raise ValueError("JSONL segment manifest must not be a symlink")
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("JSONL segment manifest exceeds size limit")
        try:
            with path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSONL segment manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ValueError("unsupported JSONL segment manifest")
        segments = manifest.get("segments")
        if not isinstance(segments, list) or len(segments) > MAX_MANIFEST_SEGMENTS:
            raise ValueError("invalid JSONL segment list")
        pending = manifest.get("pending_segment")
        cleanup = manifest.get("cleanup_pending", [])
        cleanup_not_before = manifest.get("cleanup_not_before", 0)
        if pending is not None and not isinstance(pending, str):
            raise ValueError("invalid pending JSONL segment")
        if not isinstance(cleanup, list) or len(cleanup) > MAX_MANIFEST_SEGMENTS:
            raise ValueError("invalid JSONL cleanup list")
        if isinstance(cleanup_not_before, bool) or not isinstance(
            cleanup_not_before, (int, float)
        ):
            raise ValueError("invalid JSONL cleanup deadline")
        if cleanup_not_before < 0:
            raise ValueError("invalid JSONL cleanup deadline")
        names = [*segments, *cleanup, *([pending] if pending else [])]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("invalid JSONL segment name")
        if len(set(segments)) != len(segments):
            raise ValueError("duplicate JSONL segment in manifest")
        if len(set(cleanup)) != len(cleanup):
            raise ValueError("duplicate JSONL cleanup path in manifest")
        for relative in segments:
            if not self._is_owned_segment_name(relative):
                raise ValueError("manifest segment is not owned by this log")
        for relative in cleanup:
            if not self._is_owned_segment_name(relative):
                raise ValueError("cleanup path is not owned by this log")
        if set(segments).intersection(cleanup):
            raise ValueError("cleanup path is still active")
        if pending and not self._is_pending_rotation_name(pending):
            raise ValueError("pending segment is not owned by this log")
        for relative in names:
            self._manifest_segment_path(relative, must_exist=False)
        return {
            "version": 1,
            "segments": list(segments),
            "pending_segment": pending,
            "cleanup_pending": list(cleanup),
            "cleanup_not_before": float(cleanup_not_before),
        }

    def _write_manifest(self, manifest: dict) -> None:
        atomic_write_json(self.manifest_backup_path, manifest)
        atomic_write_json(self.manifest_path, manifest)

    def _is_owned_segment_name(self, relative: str) -> bool:
        if "\\" in relative:
            return False
        stem = re.escape(self.path.stem)
        suffix = re.escape(self.path.suffix)
        legacy = rf"{stem}\.[0-9]{{8}}{suffix}"
        rotated = rf"{stem}\.segment-[0-9a-f]{{32}}{suffix}"
        generated = (
            rf"\.{stem}\.segments-[0-9a-f]{{32}}/"
            rf"{stem}\.[0-9]{{8}}{suffix}"
        )
        return bool(re.fullmatch(rf"(?:{legacy}|{rotated}|{generated})", relative))

    def _is_pending_rotation_name(self, relative: str) -> bool:
        if "\\" in relative or "/" in relative:
            return False
        return bool(
            re.fullmatch(
                rf"{re.escape(self.path.stem)}\.segment-[0-9a-f]{{32}}"
                rf"{re.escape(self.path.suffix)}",
                relative,
            )
        )

    def _assert_no_activated_segments_without_manifest(self) -> None:
        rotated_pattern = f"{self.path.stem}.segment-*{self.path.suffix}"
        for candidate in self.path.parent.glob(rotated_pattern):
            if self._is_pending_rotation_name(candidate.name):
                raise ValueError("manifest missing for activated generation")
        generation_pattern = f".{self.path.stem}.segments-*"
        for generation in self.path.parent.glob(generation_pattern):
            if generation.is_symlink() or not generation.is_dir():
                raise ValueError("manifest missing for activated generation")
            try:
                transaction = self._read_generation_transaction(generation)
            except ValueError as exc:
                raise ValueError("manifest missing for activated generation") from exc
            if transaction["state"] == "activated":
                raise ValueError("manifest missing for activated generation")

    def _manifest_segment_path(
        self,
        relative: str,
        *,
        must_exist: bool,
    ) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("JSONL manifest path must stay inside log root")
        candidate = self.path.parent / relative_path
        root = self.path.parent.resolve()
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("JSONL manifest path escapes log root") from exc
        if must_exist:
            self._validate_regular_segment(candidate)
        elif candidate.exists() and candidate.is_symlink():
            raise ValueError("JSONL segment must not be a symlink")
        return candidate

    @staticmethod
    def _validate_regular_segment(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"JSONL segment is not a regular file: {path}")

    def _relative_manifest_name(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.path.parent.resolve())
        except ValueError as exc:
            raise ValueError("JSONL segment path escapes log root") from exc
        return relative.as_posix()

    def _recover_pending_manifest(self, manifest: dict | None) -> dict | None:
        if manifest is None or not manifest.get("pending_segment"):
            return manifest
        relative = manifest["pending_segment"]
        pending = self._manifest_segment_path(relative, must_exist=False)
        if pending.exists():
            self._validate_regular_segment(pending)
            segments = list(manifest["segments"])
            if relative not in segments:
                segments.append(relative)
        elif self.path.exists():
            segments = list(manifest["segments"])
        else:
            raise OSError("pending JSONL rotation lost both source and target")
        recovered = {
            **manifest,
            "segments": segments,
            "pending_segment": None,
        }
        self._write_manifest(recovered)
        return recovered

    def _retry_manifest_cleanup(
        self,
        manifest: dict | None,
        *,
        force: bool = False,
    ) -> dict:
        if manifest is None:
            return {
                "manifest": None,
                "cleaned": 0,
                "deferred": 0,
                "errors": [],
            }
        pending = list(manifest.get("cleanup_pending", []))
        if not pending:
            return {
                "manifest": manifest,
                "cleaned": 0,
                "deferred": 0,
                "errors": [],
            }
        not_before = float(manifest.get("cleanup_not_before", 0) or 0)
        if not force and time.time() < not_before:
            return {
                "manifest": manifest,
                "cleaned": 0,
                "deferred": len(pending),
                "errors": [],
            }
        active_names = set(manifest["segments"])
        errors: list[dict] = []
        cleaned = 0
        remaining: list[str] = []
        for relative in pending:
            if not self._is_owned_segment_name(relative):
                raise ValueError("cleanup path is not owned by this log")
            if relative in active_names:
                errors.append(
                    {"path": relative, "error": "cleanup path is still active"}
                )
                remaining.append(relative)
                continue
            source = self._manifest_segment_path(relative, must_exist=False)
            try:
                source.unlink(missing_ok=True)
                cleaned += 1
            except OSError as exc:
                errors.append({"path": relative, "error": str(exc)})
                remaining.append(relative)
        updated = {
            **manifest,
            "cleanup_pending": remaining,
            "cleanup_not_before": not_before if remaining else 0,
        }
        if updated != manifest:
            self._write_manifest(updated)
        return {
            "manifest": updated,
            "cleaned": cleaned,
            "deferred": 0,
            "errors": errors,
        }

    def _write_generation_transaction(self, generation: Path, *, state: str) -> None:
        if state not in {"staging", "prepared", "activated"}:
            raise ValueError("invalid JSONL generation transaction state")
        atomic_write_json(
            generation / GENERATION_TRANSACTION_NAME,
            {
                "version": 1,
                "state": state,
                "log_name": self.path.name,
                "generation": generation.name,
            },
        )

    def _read_generation_transaction(self, generation: Path) -> dict:
        transaction_path = generation / GENERATION_TRANSACTION_NAME
        if not transaction_path.exists() or transaction_path.is_symlink():
            raise ValueError("missing JSONL generation transaction")
        if transaction_path.stat().st_size > 16 * 1024:
            raise ValueError("JSONL generation transaction exceeds size limit")
        try:
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSONL generation transaction") from exc
        if (
            not isinstance(transaction, dict)
            or transaction.get("version") != 1
            or transaction.get("state") not in {"staging", "prepared", "activated"}
            or transaction.get("log_name") != self.path.name
            or transaction.get("generation") != generation.name
        ):
            raise ValueError("invalid JSONL generation transaction")
        return transaction

    def _cleanup_orphan_generations(self, manifest: dict | None) -> dict:
        referenced = {
            Path(relative).parts[0]
            for relative in (manifest or {}).get("segments", [])
            if len(Path(relative).parts) > 1
        }
        directories = 0
        files = 0
        errors: list[dict] = []
        pattern = f".{self.path.stem}.segments-*"
        expected_prefix = f"{self.path.stem}."
        expected_suffix = self.path.suffix
        for generation in self.path.parent.glob(pattern):
            if generation.name in referenced:
                transaction_path = generation / GENERATION_TRANSACTION_NAME
                if transaction_path.exists():
                    try:
                        transaction = self._read_generation_transaction(generation)
                        if transaction["state"] != "activated":
                            self._write_generation_transaction(
                                generation,
                                state="activated",
                            )
                    except ValueError as exc:
                        errors.append(
                            {"path": str(generation), "error": str(exc)}
                        )
                continue
            if generation.is_symlink() or not generation.is_dir():
                errors.append(
                    {
                        "path": str(generation),
                        "error": "orphan generation is not a regular directory",
                    }
                )
                continue
            safe_children: list[Path] = []
            try:
                transaction = self._read_generation_transaction(generation)
                if transaction["state"] == "activated":
                    raise ValueError(
                        "activated generation is not referenced by manifest"
                    )
                for index, child in enumerate(generation.iterdir(), start=1):
                    if index > MAX_MANIFEST_SEGMENTS:
                        raise ValueError("orphan generation entry limit exceeded")
                    name = child.name
                    if name == GENERATION_TRANSACTION_NAME:
                        continue
                    middle = (
                        name[len(expected_prefix) : -len(expected_suffix)]
                        if name.startswith(expected_prefix)
                        and name.endswith(expected_suffix)
                        and expected_suffix
                        else ""
                    )
                    if (
                        child.is_symlink()
                        or not child.is_file()
                        or len(middle) != 8
                        or not middle.isdigit()
                    ):
                        raise ValueError(
                            "orphan generation contains an unexpected entry"
                        )
                    safe_children.append(child)
                for child in safe_children:
                    child.unlink()
                    files += 1
                (generation / GENERATION_TRANSACTION_NAME).unlink()
                generation.rmdir()
                directories += 1
            except (OSError, ValueError) as exc:
                errors.append({"path": str(generation), "error": str(exc)})
        return {"directories": directories, "files": files, "errors": errors}

    def _split_segment_streaming(
        self,
        source: Path,
        generation: Path,
        *,
        start_index: int,
        source_digest: _DigestAccumulator,
        replacement_digest: _DigestAccumulator,
    ) -> tuple[list[Path], dict]:
        chunks: list[Path] = []
        source_bytes = 0
        replacement_bytes = 0
        handle = None
        current_size = 0
        try:
            with source.open("rb") as reader:
                while True:
                    raw_line = reader.readline(self.max_segment_bytes + 1)
                    if not raw_line:
                        break
                    if len(raw_line) > self.max_segment_bytes:
                        raise ValueError(
                            f"single JSONL row exceeds segment limit: {source}"
                        )
                    if handle is None or (
                        current_size and current_size + len(raw_line) > self.max_segment_bytes
                    ):
                        if handle is not None:
                            handle.flush()
                            os.fsync(handle.fileno())
                            handle.close()
                        target = generation / (
                            f"{self.path.stem}.{start_index + len(chunks):08d}{self.path.suffix}"
                        )
                        handle = target.open("xb")
                        chunks.append(target)
                        current_size = 0
                    handle.write(raw_line)
                    current_size += len(raw_line)
                    source_bytes += len(raw_line)
                    source_digest.update(raw_line)
            if handle is not None:
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                handle = None
            if not chunks and source.stat().st_size:
                raise OSError("JSONL resegmentation produced no output")
            for chunk in chunks:
                chunk_size = chunk.stat().st_size
                if chunk_size <= 0 or chunk_size > self.max_segment_bytes:
                    raise OSError("generated JSONL segment violates size bound")
                with chunk.open("rb") as verifier:
                    while True:
                        block = verifier.read(1024 * 1024)
                        if not block:
                            break
                        replacement_bytes += len(block)
                        replacement_digest.update(block)
        finally:
            if handle is not None:
                handle.close()
        return chunks, {
            "source_bytes": source_bytes,
            "replacement_bytes": replacement_bytes,
        }
