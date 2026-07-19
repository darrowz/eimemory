from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping


RECEIPT_KEY_ENV = "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY"
RECEIPT_KEY_FILE_ENV = "EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE"
MIN_KEY_LENGTH = 32


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
        int(metadata.st_ctime_ns),
        int(metadata.st_size),
    )
    return _receipt_key_from_file(str(path), identity)


@lru_cache(maxsize=16)
def _receipt_key_from_file(path_value: str, expected_identity: tuple[int, int, int, int, int]) -> str:
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
            int(metadata.st_ctime_ns),
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
        "source": "openclaw.after_tool_call",
        "tool_call_id": str(receipt.get("tool_call_id") or "").strip(),
        "tool_name": str(receipt.get("tool_name") or "").strip(),
    }


def _canonical_bytes(receipt: Mapping[str, Any]) -> bytes:
    return json.dumps(
        canonical_tool_receipt(receipt),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sign_tool_receipt(receipt: Mapping[str, Any], *, key: str = "") -> dict[str, Any]:
    secret = str(key or _receipt_key()).strip()
    if len(secret) < MIN_KEY_LENGTH or len(set(secret)) < 12:
        raise ValueError("tool receipt attestation key is unavailable")
    canonical = canonical_tool_receipt(receipt)
    signature = hmac.new(secret.encode("utf-8"), _canonical_bytes(canonical), sha256).hexdigest()
    return {**canonical, "signature": signature}


def verify_tool_receipt(
    receipt: Mapping[str, Any],
    *,
    session_id: str,
    run_id: str,
    key: str = "",
) -> bool:
    secret = str(key or _receipt_key()).strip()
    signature = str(receipt.get("signature") or "").strip().lower()
    canonical = canonical_tool_receipt(receipt)
    if not (
        len(secret) >= MIN_KEY_LENGTH
        and len(set(secret)) >= 12
        and receipt.get("receipt_version") == 1
        and receipt.get("attestation") == "hmac-sha256"
        and receipt.get("source") == "openclaw.after_tool_call"
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
    expected = hmac.new(secret.encode("utf-8"), _canonical_bytes(canonical), sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


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
