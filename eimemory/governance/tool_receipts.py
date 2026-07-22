from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping
from datetime import datetime, timezone


RECEIPT_KEY_ENV = "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY"
RECEIPT_KEY_FILE_ENV = "EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE"
RECEIPT_KEYRING_FILE_ENV = "EIMEMORY_EVIDENCE_RECEIPT_KEYRING_FILE"
RECEIPT_MAX_AGE_ENV = "EIMEMORY_EVIDENCE_RECEIPT_MAX_AGE_SECONDS"
MIN_KEY_LENGTH = 32
MAX_KEYRING_BYTES = 16_384
MAX_PREVIOUS_KEYS = 4
SUPPORTED_TOOL_RECEIPT_SOURCES = frozenset(
    {
        "openclaw.after_tool_call",
        "codex.post_tool_use",
        "hermes.post_tool_call",
    }
)
V2_RECEIPT_VERSION = 2
V2_ATTESTATION = "hmac-sha256-v2"
V2_MAX_AGE_SECONDS = 15 * 60
MAX_ELIGIBLE_RECEIPTS_PER_RUN = 32
STRUCTURED_TEST_POLICY_ID = "test_command.exit_zero.positive_count.v1"
TRUSTED_TEST_POLICY_IDS = frozenset({STRUCTURED_TEST_POLICY_ID})
ATTESTATION_PRODUCERS = {
    "codex": ("codex", "codex.post_tool_use"),
    "hermes": ("hermes", "hermes.post_tool_call"),
}


@dataclass(frozen=True, slots=True)
class ReceiptKeySet:
    active_id: str
    active_key: str
    verification_keys: dict[str, str]


def _key_id(secret: str) -> str:
    return "key_" + sha256(secret.encode("utf-8")).hexdigest()[:16]


def _strong_key(value: object) -> str:
    key = str(value or "").strip()
    return key if len(key) >= MIN_KEY_LENGTH and len(set(key)) >= 12 else ""


def _safe_key_id(value: object) -> str:
    key_id = str(value or "").strip()
    return (
        key_id
        if key_id.lower() != "active" and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id)
        else ""
    )


def _secure_file_mode(mode: int, *, platform_name: str | None = None) -> bool:
    platform = os.name if platform_name is None else str(platform_name)
    return stat.S_ISREG(mode) and (platform != "posix" or mode & 0o077 == 0)


def _read_secure_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        if path.is_symlink():
            return b""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return b""
    try:
        metadata = os.fstat(descriptor)
        if (
            not _secure_file_mode(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > max_bytes
        ):
            return b""
        payload = os.read(descriptor, max_bytes + 1)
        return payload if len(payload) <= max_bytes else b""
    finally:
        os.close(descriptor)


def _receipt_key_set() -> ReceiptKeySet | None:
    configured = _strong_key(os.environ.get(RECEIPT_KEY_ENV))
    if configured:
        active_id = _key_id(configured)
        return ReceiptKeySet(active_id, configured, {active_id: configured})
    keyring_path = str(os.environ.get(RECEIPT_KEYRING_FILE_ENV) or "").strip()
    if keyring_path:
        return _load_receipt_keyring(Path(keyring_path))
    configured = _receipt_key()
    if configured:
        active_id = _key_id(configured)
        return ReceiptKeySet(active_id, configured, {active_id: configured})
    return None


def _load_receipt_keyring(path: Path) -> ReceiptKeySet | None:
    payload = _read_secure_file(path, max_bytes=MAX_KEYRING_BYTES)
    if not payload:
        return None
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"active", "previous"}:
        return None
    active = parsed.get("active")
    previous = parsed.get("previous")
    if not isinstance(active, dict) or set(active) != {"key_id", "key"}:
        return None
    if not isinstance(previous, list) or len(previous) > MAX_PREVIOUS_KEYS:
        return None
    entries = [active, *previous]
    keys: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"key_id", "key"}:
            return None
        entry_id = _safe_key_id(entry.get("key_id"))
        entry_key = _strong_key(entry.get("key"))
        if (
            not entry_id
            or not entry_key
            or entry_id != _key_id(entry_key)
            or entry_id in keys
        ):
            return None
        keys[entry_id] = entry_key
    active_id = _safe_key_id(active.get("key_id"))
    return ReceiptKeySet(active_id, keys[active_id], keys)


def _receipt_key() -> str:
    configured = str(os.environ.get(RECEIPT_KEY_ENV) or "").strip()
    if len(configured) >= MIN_KEY_LENGTH and len(set(configured)) >= 12:
        return configured
    configured_path = str(os.environ.get(RECEIPT_KEY_FILE_ENV) or "").strip()
    path = Path(configured_path) if configured_path else Path(
        os.environ.get("EIMEMORY_CONFIG_DIR") or "/etc/eimemory"
    ) / "evidence-receipt.env"
    try:
        metadata = path.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return ""
    identity = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mtime_ns),
        int(metadata.st_size),
    )
    # Key rotation must be observed even on filesystems with coarse timestamp
    # resolution; the secure descriptor checks below remain the trust boundary.
    _receipt_key_from_file.cache_clear()
    return _receipt_key_from_file(str(path), identity)


@lru_cache(maxsize=16)
def _receipt_key_from_file(path_value: str, expected_identity: tuple[int, int, int, int]) -> str:
    path = Path(path_value)
    try:
        if path.is_symlink():
            return ""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return ""
    try:
        metadata = os.fstat(descriptor)
        observed_identity = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_mtime_ns),
            int(metadata.st_size),
        )
        if (
            observed_identity != expected_identity
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            return ""
        if os.name == "posix" and metadata.st_mode & 0o077:
            return ""
        payload = os.read(descriptor, 4097)
        if len(payload) > 4096:
            return ""
    finally:
        os.close(descriptor)
    try:
        lines = [
            line.strip()
            for line in payload.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except UnicodeError:
        return ""
    prefix = f"{RECEIPT_KEY_ENV}="
    if len(lines) != 1 or not lines[0].startswith(prefix):
        return ""
    key = lines[0][len(prefix) :].strip()
    return key if len(key) >= MIN_KEY_LENGTH and len(set(key)) >= 12 else ""


def canonical_tool_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if receipt.get("receipt_version") == V2_RECEIPT_VERSION:
        return _canonical_v2_tool_receipt(receipt)
    try:
        duration_ms = max(0, int(receipt.get("duration_ms") or 0))
    except (TypeError, ValueError):
        duration_ms = 0
    return {
        "attestation": "hmac-sha256",
        "duration_ms": duration_ms,
        "passed": receipt.get("passed") is True,
        "receipt_version": 1,
        "result_digest": str(receipt.get("result_digest") or "").strip().lower(),
        "run_id": str(receipt.get("run_id") or "").strip(),
        "session_id": str(receipt.get("session_id") or "").strip(),
        "source": str(receipt.get("source") or "openclaw.after_tool_call").strip(),
        "tool_call_id": str(receipt.get("tool_call_id") or "").strip(),
        "tool_name": str(receipt.get("tool_name") or "").strip(),
    }


def _canonical_v2_tool_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 allowlist is also the persistence/redaction boundary."""
    try:
        duration_ms = max(0, int(receipt.get("duration_ms") or 0))
    except (TypeError, ValueError):
        duration_ms = 0
    return {
        "attestation": V2_ATTESTATION,
        "attestation_id": str(receipt.get("attestation_id") or "").strip(),
        "channel": str(receipt.get("channel") or "").strip(),
        "deployment_receipt_id": str(receipt.get("deployment_receipt_id") or "").strip(),
        "duration_ms": duration_ms,
        "expires_at": str(receipt.get("expires_at") or "").strip(),
        "issued_at": str(receipt.get("issued_at") or "").strip(),
        "invocation_digest": str(receipt.get("invocation_digest") or "").strip().lower(),
        "key_id": str(receipt.get("key_id") or "").strip(),
        "passed": receipt.get("passed") is True,
        "receipt_id": str(receipt.get("receipt_id") or "").strip(),
        "receipt_version": V2_RECEIPT_VERSION,
        "release_commit": str(receipt.get("release_commit") or "").strip(),
        "release_session_id": str(receipt.get("release_session_id") or "").strip(),
        "release_version": str(receipt.get("release_version") or "").strip(),
        "result_digest": str(receipt.get("result_digest") or "").strip().lower(),
        "retrieval_policy_digest": str(receipt.get("retrieval_policy_digest") or "").strip().lower(),
        "run_id": str(receipt.get("run_id") or "").strip(),
        "session_id": str(receipt.get("session_id") or "").strip(),
        "source": str(receipt.get("source") or "").strip(),
        "tool_call_id": str(receipt.get("tool_call_id") or "").strip(),
        "tool_name": str(receipt.get("tool_name") or "").strip(),
        "verification_policy_id": str(receipt.get("verification_policy_id") or "").strip(),
    }


def _canonical_bytes(receipt: Mapping[str, Any]) -> bytes:
    return json.dumps(
        canonical_tool_receipt(receipt),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def tool_receipt_commitment(value: Any, *, domain: str) -> str:
    """Return a secret-keyed, domain-separated commitment to complete raw data."""

    key_set = _receipt_key_set()
    if key_set is None or not key_set.active_key:
        raise ValueError("tool receipt attestation key is unavailable")
    domain_id = str(domain or "").strip().lower()
    if domain_id not in {"invocation", "result"}:
        raise ValueError("unsupported tool receipt commitment domain")
    digest = hmac.new(
        key_set.active_key.encode("utf-8"),
        f"eimemory.tool-receipt.commitment.v1\0{domain_id}\0".encode("ascii"),
        sha256,
    )
    for chunk in _canonical_raw_chunks(value, active=set()):
        digest.update(chunk)
    return digest.hexdigest()


def _canonical_raw_chunks(value: Any, *, active: set[int]) -> Iterable[bytes]:
    if value is None:
        yield b"n;"
        return
    if isinstance(value, bool):
        yield b"b1;" if value else b"b0;"
        return
    if isinstance(value, int):
        yield from _framed_chunks(b"i", str(value).encode("ascii"))
        return
    if isinstance(value, float):
        yield from _framed_chunks(b"f", repr(value).encode("ascii"))
        return
    if isinstance(value, str):
        yield from _framed_chunks(b"s", value.encode("utf-8", errors="surrogatepass"))
        return
    if isinstance(value, bytes):
        yield from _framed_chunks(b"y", value)
        return

    identity = id(value)
    if identity in active:
        raise ValueError("cyclic tool receipt input is unsupported")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            yield f"d{len(value)}:".encode("ascii")
            ordered = sorted(value.items(), key=lambda item: (type(item[0]).__qualname__, repr(item[0])))
            for key, nested in ordered:
                yield from _canonical_raw_chunks(key, active=active)
                yield from _canonical_raw_chunks(nested, active=active)
            yield b";"
            return
        if isinstance(value, (list, tuple)):
            marker = b"l" if isinstance(value, list) else b"t"
            yield marker + str(len(value)).encode("ascii") + b":"
            for nested in value:
                yield from _canonical_raw_chunks(nested, active=active)
            yield b";"
            return
        if isinstance(value, (set, frozenset)):
            marker = b"e" if isinstance(value, set) else b"r"
            ordered = sorted(value, key=lambda item: (type(item).__qualname__, repr(item)))
            yield marker + str(len(ordered)).encode("ascii") + b":"
            for nested in ordered:
                yield from _canonical_raw_chunks(nested, active=active)
            yield b";"
            return
        fallback = f"{type(value).__module__}.{type(value).__qualname__}\0{value}".encode(
            "utf-8", errors="replace"
        )
        yield from _framed_chunks(b"o", fallback)
    finally:
        active.remove(identity)


def _framed_chunks(marker: bytes, payload: bytes) -> Iterable[bytes]:
    yield marker + str(len(payload)).encode("ascii") + b":"
    yield payload
    yield b";"


def sign_tool_receipt(
    receipt: Mapping[str, Any],
    *,
    key: str = "",
    key_id: str = "",
) -> dict[str, Any]:
    is_v2 = receipt.get("receipt_version") == V2_RECEIPT_VERSION
    if key:
        secret = _strong_key(key)
        signing_key_id = _key_id(secret) if secret else ""
        if key_id and str(key_id).strip() != signing_key_id:
            raise ValueError("tool receipt key_id must match the key fingerprint")
    elif is_v2:
        key_set = _receipt_key_set()
        secret = key_set.active_key if key_set is not None else ""
        signing_key_id = key_set.active_id if key_set is not None else ""
    else:
        secret = _strong_key(_receipt_key())
        signing_key_id = ""
    if not secret or (is_v2 and not signing_key_id):
        raise ValueError("tool receipt attestation key is unavailable")
    canonical = canonical_tool_receipt(
        {**dict(receipt), **({"key_id": signing_key_id} if is_v2 else {})}
    )
    if canonical["source"] not in SUPPORTED_TOOL_RECEIPT_SOURCES:
        raise ValueError("unsupported tool receipt source")
    signature = hmac.new(secret.encode("utf-8"), _canonical_bytes(canonical), sha256).hexdigest()
    return {**canonical, "signature": signature}


def verify_tool_receipt(
    receipt: Mapping[str, Any],
    *,
    session_id: str,
    run_id: str,
    key: str = "",
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> bool:
    signature = str(receipt.get("signature") or "").strip().lower()
    canonical = canonical_tool_receipt(receipt)
    if canonical.get("receipt_version") == V2_RECEIPT_VERSION:
        if key:
            secret = _strong_key(key)
        else:
            key_set = _receipt_key_set()
            secret = (
                key_set.verification_keys.get(str(canonical.get("key_id") or ""), "")
                if key_set is not None
                else ""
            )
        return _verify_v2_tool_receipt(
            receipt,
            canonical,
            secret,
            session_id=session_id,
            run_id=run_id,
            now=now,
            max_age_seconds=max_age_seconds,
        )
    if not (
        receipt.get("receipt_version") == 1
        and receipt.get("attestation") == "hmac-sha256"
        and receipt.get("source") in SUPPORTED_TOOL_RECEIPT_SOURCES
        and canonical["passed"] is True
        and canonical["session_id"] == str(session_id or "").strip()
        and canonical["run_id"] == str(run_id or "").strip()
        and canonical["session_id"]
        and canonical["run_id"]
        and canonical["tool_name"]
        and canonical["tool_call_id"]
        and re.fullmatch(r"[0-9a-f]{64}", canonical["result_digest"])
        and re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        return False
    if key:
        verification_keys = [_strong_key(key)]
    else:
        key_set = _receipt_key_set()
        verification_keys = (
            list(dict.fromkeys(key_set.verification_keys.values()))
            if key_set is not None
            else []
        )
    return any(
        secret
        and hmac.compare_digest(
            signature,
            hmac.new(secret.encode("utf-8"), _canonical_bytes(canonical), sha256).hexdigest(),
        )
        for secret in verification_keys
    )


def _verify_v2_tool_receipt(
    receipt: Mapping[str, Any],
    canonical: Mapping[str, Any],
    secret: str,
    *,
    session_id: str,
    run_id: str,
    now: datetime | None,
    max_age_seconds: int | None,
) -> bool:
    signature = str(receipt.get("signature") or "").strip().lower()
    if not (
        len(secret) >= MIN_KEY_LENGTH
        and len(set(secret)) >= 12
        and receipt.get("attestation") == V2_ATTESTATION
        and canonical["source"] in {value[1] for value in ATTESTATION_PRODUCERS.values()}
        and canonical["channel"] in {value[0] for value in ATTESTATION_PRODUCERS.values()}
        and canonical["passed"] is True
        and canonical["session_id"] == str(session_id or "").strip()
        and canonical["run_id"] == str(run_id or "").strip()
        and all(canonical[name] for name in ("receipt_id", "attestation_id", "key_id", "issued_at", "expires_at", "tool_name", "tool_call_id", "verification_policy_id"))
        and re.fullmatch(r"[0-9a-f]{64}", canonical["invocation_digest"])
        and re.fullmatch(r"[0-9a-f]{64}", canonical["result_digest"])
        and re.fullmatch(r"[0-9a-f]{64}", canonical["retrieval_policy_digest"])
        and re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        return False
    try:
        issued_at = datetime.fromisoformat(canonical["issued_at"].replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(canonical["expires_at"].replace("Z", "+00:00"))
        verifier_now = now or datetime.now(timezone.utc)
        if issued_at.tzinfo is None or expires_at.tzinfo is None or verifier_now.tzinfo is None:
            return False
        issued_at = issued_at.astimezone(timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        verifier_now = verifier_now.astimezone(timezone.utc)
        maximum = _bounded_max_age(max_age_seconds)
        if (
            issued_at > verifier_now
            or verifier_now >= expires_at
            or expires_at <= issued_at
            or (expires_at - issued_at).total_seconds() > maximum
            or (verifier_now - issued_at).total_seconds() > maximum
        ):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    expected = hmac.new(secret.encode("utf-8"), _canonical_bytes(canonical), sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _bounded_max_age(value: int | None) -> int:
    raw: object = value if value is not None else os.environ.get(RECEIPT_MAX_AGE_ENV, V2_MAX_AGE_SECONDS)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = V2_MAX_AGE_SECONDS
    return max(60, min(60 * 60, parsed))


def verified_tool_receipts(
    value: Any,
    *,
    session_id: str,
    run_id: str,
    limit: int = 32,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    verified: list[dict[str, Any]] = []
    for item in value[: max(1, int(limit))]:
        if not isinstance(item, Mapping) or not verify_tool_receipt(
            item,
            session_id=session_id,
            run_id=run_id,
        ):
            continue
        verified.append({**canonical_tool_receipt(item), "signature": str(item["signature"]).lower()})
    return verified
