from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any, Mapping
import zlib

from eimemory.storage.atomic_file import atomic_write_json, interprocess_lock, read_json_strict


DEFAULT_MAX_SEGMENT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAGIC = b"EIPS"
_VERSION = 1
_HEADER = struct.Struct(">4sB3xQQ32s")
_SEGMENT_NAME = re.compile(r"payload-(\d{8})\.seg")
_DIGEST_NAME = re.compile(r"[0-9a-f]{64}\.json")


class PayloadSegmentError(RuntimeError):
    pass


class PayloadSegmentStore:
    """Private append-only zlib frames with persistent content addressing."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self.root = Path(root)
        self.max_segment_bytes = max(
            _HEADER.size + 64,
            min(DEFAULT_MAX_SEGMENT_BYTES, int(max_segment_bytes)),
        )
        self.max_payload_bytes = max(256, min(DEFAULT_MAX_PAYLOAD_BYTES, int(max_payload_bytes)))
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate_ancestor_chain(self.root)
        self._make_private(self.root, directory=True)
        self._validate_directory(self.root)
        self.index_root = self.root / "index"
        self.index_root.mkdir(parents=True, exist_ok=True)
        self._make_private(self.index_root, directory=True)
        self._validate_directory(self.index_root)
        self._lock_path = self.root / ".append.lock"
        self._stats_path = self.root / "stats.json"
        self._digest_index: dict[str, dict[str, Any]] = {}
        with interprocess_lock(self._lock_path):
            self._recover_latest_segment()
            self._initialize_stats()

    def append(self, payload: bytes) -> dict[str, Any]:
        raw = bytes(payload)
        if len(raw) > self.max_payload_bytes:
            raise PayloadSegmentError("payload exceeds hard limit")
        digest = sha256(raw).hexdigest()
        cached = self._digest_index.get(digest) or self._indexed_pointer(digest)
        if cached is not None:
            self._digest_index[digest] = dict(cached)
            return dict(cached)
        compressed = zlib.compress(raw, level=6)
        frame_size = _HEADER.size + len(compressed)
        if frame_size > self.max_segment_bytes:
            raise PayloadSegmentError("compressed payload exceeds segment hard limit")
        with interprocess_lock(self._lock_path):
            cached = self._indexed_pointer(digest)
            if cached is not None:
                self._digest_index[digest] = dict(cached)
                return dict(cached)
            segment = self._writable_segment(frame_size)
            descriptor = self._open_secure(segment, os.O_RDWR | os.O_CREAT | os.O_APPEND)
            offset = int(os.fstat(descriptor).st_size)
            try:
                header = _HEADER.pack(
                    _MAGIC,
                    _VERSION,
                    len(raw),
                    len(compressed),
                    bytes.fromhex(digest),
                )
                self._write_all(descriptor, header + compressed)
                os.fsync(descriptor)
                if int(os.fstat(descriptor).st_size) != offset + frame_size:
                    raise PayloadSegmentError("payload segment size changed during append")
            except Exception:
                try:
                    os.ftruncate(descriptor, offset)
                    os.fsync(descriptor)
                except OSError:
                    pass
                raise
            finally:
                os.close(descriptor)
            pointer = self._pointer(
                segment=segment.name,
                offset=offset,
                compressed_size=len(compressed),
                raw_size=len(raw),
                digest=digest,
            )
            self._write_pointer_index(pointer)
            self._record_append_stats(frame_size=frame_size, new_segment=(offset == 0))
        self._digest_index[digest] = dict(pointer)
        return pointer

    def read(self, pointer: Mapping[str, Any]) -> bytes:
        normalized = self._validate_pointer(pointer)
        segment = self.root / normalized["segment"]
        if not segment.exists():
            raise PayloadSegmentError("payload segment is missing")
        descriptor = self._open_secure(segment, os.O_RDONLY)
        try:
            os.lseek(descriptor, normalized["offset"], os.SEEK_SET)
            frame = self._read_exact(
                descriptor,
                _HEADER.size + normalized["compressed_size"],
            )
            if int(os.fstat(descriptor).st_size) > self.max_segment_bytes:
                raise PayloadSegmentError("payload segment exceeds hard limit")
        finally:
            os.close(descriptor)
        if len(frame) != _HEADER.size + normalized["compressed_size"]:
            raise PayloadSegmentError("payload segment frame is truncated")
        magic, version, raw_size, compressed_size, digest_bytes = _HEADER.unpack(
            frame[: _HEADER.size]
        )
        if (
            magic != _MAGIC
            or version != _VERSION
            or int(raw_size) != normalized["raw_size"]
            or int(compressed_size) != normalized["compressed_size"]
            or digest_bytes.hex() != normalized["digest"]
        ):
            raise PayloadSegmentError("payload segment header is corrupt")
        return self._decompress_verified(
            frame[_HEADER.size :],
            raw_size=normalized["raw_size"],
            digest=normalized["digest"],
        )

    def archive_stats(self) -> dict[str, int]:
        segment_count = 0
        archive_bytes = 0
        for path in self.root.glob("payload-*.seg"):
            if not _SEGMENT_NAME.fullmatch(path.name):
                continue
            self._validate_regular_file(path)
            segment_count += 1
            archive_bytes += int(path.stat(follow_symlinks=False).st_size)
        return {"segment_count": segment_count, "archive_bytes": archive_bytes}

    def quick_stats(self) -> dict[str, Any]:
        """Return O(1) persisted counters suitable for a request health path."""

        try:
            state = read_json_strict(self._stats_path, dict)
        except (OSError, ValueError) as exc:
            raise PayloadSegmentError("payload segment stats are unavailable") from exc
        if str(state.get("schema") or "") != "payload_segment_stats.v1":
            raise PayloadSegmentError("payload segment stats schema is invalid")
        return {
            "segment_count": max(0, int(state.get("segment_count") or 0)),
            "archive_bytes": max(0, int(state.get("archive_bytes") or 0)),
            "indexed_count": max(0, int(state.get("indexed_count") or 0)),
            "stats_exact": bool(state.get("stats_exact")),
        }

    def _initialize_stats(self) -> None:
        if self._stats_path.exists():
            self.quick_stats()
            return
        physical = self.archive_stats()
        has_existing_segments = int(physical["segment_count"]) > 0
        atomic_write_json(
            self._stats_path,
            {
                "schema": "payload_segment_stats.v1",
                **physical,
                "indexed_count": 0,
                "stats_exact": not has_existing_segments,
            },
        )

    def _record_append_stats(self, *, frame_size: int, new_segment: bool) -> None:
        state = read_json_strict(self._stats_path, dict)
        if str(state.get("schema") or "") != "payload_segment_stats.v1":
            raise PayloadSegmentError("payload segment stats schema is invalid")
        state["segment_count"] = max(0, int(state.get("segment_count") or 0)) + int(
            bool(new_segment)
        )
        state["archive_bytes"] = max(0, int(state.get("archive_bytes") or 0)) + max(
            0, int(frame_size)
        )
        state["indexed_count"] = max(0, int(state.get("indexed_count") or 0)) + 1
        atomic_write_json(self._stats_path, state)

    def orphan_report(self, referenced_digests: set[str]) -> dict[str, Any]:
        referenced = {str(item).lower() for item in referenced_digests if str(item)}
        orphan_digests: list[str] = []
        orphan_compressed_bytes = 0
        indexed_count = 0
        for path in self.index_root.glob("*/*.json"):
            if not _DIGEST_NAME.fullmatch(path.name):
                continue
            pointer = read_json_strict(path, dict)
            normalized = self._validate_pointer(pointer)
            indexed_count += 1
            if normalized["digest"] not in referenced:
                orphan_digests.append(normalized["digest"])
                orphan_compressed_bytes += normalized["compressed_size"] + _HEADER.size
        return {
            "schema": "payload_orphan_report.v1",
            "indexed_count": indexed_count,
            "referenced_count": len(referenced),
            "orphan_count": len(orphan_digests),
            "orphan_compressed_bytes": orphan_compressed_bytes,
            "orphan_digest_sample": sorted(orphan_digests)[:20],
            "action": "offline_segment_compaction_required" if orphan_digests else "none",
        }

    def _recover_latest_segment(self) -> None:
        candidates: list[tuple[int, Path]] = []
        for path in self.root.glob("payload-*.seg"):
            match = _SEGMENT_NAME.fullmatch(path.name)
            if match:
                self._validate_regular_file(path)
                candidates.append((int(match.group(1)), path))
        if not candidates:
            return
        _number, latest = max(candidates)
        descriptor = self._open_secure(latest, os.O_RDWR)
        try:
            size = int(os.fstat(descriptor).st_size)
            offset = 0
            while offset < size:
                os.lseek(descriptor, offset, os.SEEK_SET)
                header = self._read_exact(descriptor, _HEADER.size)
                if len(header) < _HEADER.size:
                    self._truncate_tail(descriptor, offset)
                    break
                magic, version, raw_size, compressed_size, digest_bytes = _HEADER.unpack(header)
                if magic != _MAGIC or version != _VERSION:
                    raise PayloadSegmentError("payload segment recovery found corrupt header")
                frame_end = offset + _HEADER.size + int(compressed_size)
                if int(raw_size) > self.max_payload_bytes or frame_end > self.max_segment_bytes:
                    raise PayloadSegmentError("payload segment recovery bounds failed")
                compressed = self._read_exact(descriptor, int(compressed_size))
                if len(compressed) < int(compressed_size):
                    self._truncate_tail(descriptor, offset)
                    break
                digest = digest_bytes.hex()
                self._decompress_verified(compressed, raw_size=int(raw_size), digest=digest)
                pointer = self._pointer(
                    segment=latest.name,
                    offset=offset,
                    compressed_size=int(compressed_size),
                    raw_size=int(raw_size),
                    digest=digest,
                )
                if self._indexed_pointer(digest) is None:
                    self._write_pointer_index(pointer)
                offset = frame_end
        finally:
            os.close(descriptor)

    @staticmethod
    def _truncate_tail(descriptor: int, offset: int) -> None:
        os.ftruncate(descriptor, offset)
        os.fsync(descriptor)

    def _writable_segment(self, frame_size: int) -> Path:
        candidates: list[tuple[int, Path]] = []
        for path in self.root.glob("payload-*.seg"):
            match = _SEGMENT_NAME.fullmatch(path.name)
            if match:
                self._validate_regular_file(path)
                candidates.append((int(match.group(1)), path))
        if candidates:
            index, latest = max(candidates)
            if int(latest.stat(follow_symlinks=False).st_size) + frame_size <= self.max_segment_bytes:
                return latest
            index += 1
        else:
            index = 1
        return self.root / f"payload-{index:08d}.seg"

    def _pointer(
        self,
        *,
        segment: str,
        offset: int,
        compressed_size: int,
        raw_size: int,
        digest: str,
    ) -> dict[str, Any]:
        return {
            "schema": "payload_segment_pointer.v1",
            "codec": "zlib",
            "segment": segment,
            "offset": int(offset),
            "header_size": _HEADER.size,
            "compressed_size": int(compressed_size),
            "raw_size": int(raw_size),
            "digest": digest,
        }

    def _pointer_index_path(self, digest: str) -> Path:
        shard = self.index_root / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        self._make_private(shard, directory=True)
        self._validate_directory(shard)
        return shard / f"{digest}.json"

    def _indexed_pointer(self, digest: str) -> dict[str, Any] | None:
        path = self._pointer_index_path(digest)
        if not path.exists():
            return None
        if path.is_symlink() or self._is_reparse(path):
            raise PayloadSegmentError("payload pointer index must not be a symlink or reparse point")
        pointer = read_json_strict(path, dict)
        normalized = self._validate_pointer(pointer)
        if normalized["digest"] != digest:
            raise PayloadSegmentError("payload pointer index digest mismatch")
        return dict(pointer)

    def _write_pointer_index(self, pointer: Mapping[str, Any]) -> None:
        digest = str(pointer["digest"])
        path = self._pointer_index_path(digest)
        if path.exists():
            existing = self._indexed_pointer(digest)
            if existing != dict(pointer):
                raise PayloadSegmentError("payload digest has conflicting segment pointers")
            return
        atomic_write_json(path, dict(pointer))

    def _open_secure(self, path: Path, flags: int) -> int:
        if path.parent != self.root or not _SEGMENT_NAME.fullmatch(path.name):
            raise PayloadSegmentError("invalid payload segment path")
        if path.is_symlink() or self._is_reparse(path):
            raise PayloadSegmentError("payload segment must not be a symlink or reparse point")
        # Windows defaults low-level file descriptors to text mode.  Segment
        # frames are binary and a compressed byte equal to ``\n`` must not be
        # expanded to ``\r\n`` (nor treated as a text EOF on read).
        open_flags = (
            flags
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, open_flags, 0o600)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise PayloadSegmentError("payload segment secure open failed") from exc
        try:
            opened = os.fstat(descriptor)
            self._validate_open_metadata(opened)
            current = path.stat(follow_symlinks=False)
            if (int(opened.st_dev), int(opened.st_ino)) != (int(current.st_dev), int(current.st_ino)):
                raise PayloadSegmentError("payload segment identity changed during open")
            if Path(os.path.realpath(path)).parent != Path(os.path.realpath(self.root)):
                raise PayloadSegmentError("payload segment final path escaped its private root")
            self._make_private(path, directory=False)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _validate_directory(self, path: Path) -> None:
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or self._is_reparse(path) or not stat.S_ISDIR(metadata.st_mode):
            raise PayloadSegmentError("payload segment root must be a private regular directory")
        if os.name == "posix" and (
            int(metadata.st_uid) not in {0, os.getuid()} or metadata.st_mode & 0o077
        ):
            raise PayloadSegmentError("payload segment root owner or mode is unsafe")

    def _validate_ancestor_chain(self, path: Path) -> None:
        current = path
        while True:
            if current.is_symlink() or self._is_reparse(current):
                raise PayloadSegmentError("payload segment ancestor must not be a symlink or reparse point")
            if current.parent == current:
                break
            current = current.parent

    def _validate_regular_file(self, path: Path) -> None:
        if path.is_symlink() or self._is_reparse(path):
            raise PayloadSegmentError("payload segment must not be a symlink or reparse point")
        self._validate_open_metadata(path.stat(follow_symlinks=False))

    @staticmethod
    def _validate_open_metadata(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1)) != 1:
            raise PayloadSegmentError("payload segment must be one regular file")
        if os.name == "posix" and (
            int(metadata.st_uid) not in {0, os.getuid()} or metadata.st_mode & 0o077
        ):
            raise PayloadSegmentError("payload segment owner or mode is unsafe")

    @staticmethod
    def _make_private(path: Path, *, directory: bool) -> None:
        try:
            os.chmod(path, 0o700 if directory else 0o600, follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))

    def _validate_pointer(self, pointer: Mapping[str, Any]) -> dict[str, Any]:
        try:
            segment = str(pointer["segment"])
            offset = int(pointer["offset"])
            compressed_size = int(pointer["compressed_size"])
            raw_size = int(pointer["raw_size"])
            digest = str(pointer["digest"]).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadSegmentError("invalid payload segment pointer") from exc
        if (
            str(pointer.get("schema") or "") != "payload_segment_pointer.v1"
            or str(pointer.get("codec") or "") != "zlib"
            or not _SEGMENT_NAME.fullmatch(segment)
            or offset < 0
            or compressed_size < 0
            or compressed_size + _HEADER.size > self.max_segment_bytes
            or offset + compressed_size + _HEADER.size > self.max_segment_bytes
            or raw_size < 0
            or raw_size > self.max_payload_bytes
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise PayloadSegmentError("invalid payload segment pointer bounds")
        return {
            "segment": segment,
            "offset": offset,
            "compressed_size": compressed_size,
            "raw_size": raw_size,
            "digest": digest,
        }

    def _decompress_verified(self, compressed: bytes, *, raw_size: int, digest: str) -> bytes:
        decompressor = zlib.decompressobj()
        try:
            raw = decompressor.decompress(compressed, raw_size + 1)
        except zlib.error as exc:
            raise PayloadSegmentError("payload segment decompress failed") from exc
        if (
            not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
            or len(raw) != raw_size
            or len(raw) > self.max_payload_bytes
        ):
            raise PayloadSegmentError("payload segment decompression bounds failed")
        if sha256(raw).hexdigest() != digest:
            raise PayloadSegmentError("payload segment digest mismatch")
        return raw

    @staticmethod
    def _read_exact(descriptor: int, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = max(0, int(length))
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise PayloadSegmentError("short payload segment write")
            written += count
