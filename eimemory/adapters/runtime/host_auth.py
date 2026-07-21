from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Mapping, MutableMapping


MIN_PRODUCER_TOKEN_LENGTH = 32
MAX_CREDENTIAL_FILE_BYTES = 8 * 1024
ATTESTATION_TOKENS_FILE_ENV = "EIMEMORY_ATTESTATION_TOKENS_FILE"
ATTESTATION_HOST_PROFILE_ENV = "EIMEMORY_ATTESTATION_HOST_PROFILE"
OPERATOR_SEPARATED_PROFILE = "operator-separated-v1"
PRODUCER_TOKEN_FILE_ENVS = {
    "codex": "EIMEMORY_CODEX_ATTESTATION_TOKEN_FILE",
    "hermes": "EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE",
}
_LEGACY_TOKEN_ENVS = (
    "EIMEMORY_ATTESTATION_TOKEN",
    "EIMEMORY_ATTESTATION_TOKENS_JSON",
)


def is_strong_producer_token(value: object) -> bool:
    token = str(value or "").strip()
    return len(token) >= MIN_PRODUCER_TOKEN_LENGTH and len(set(token)) >= 12


def producer_token_from_private_file(
    channel: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    if str(source.get(ATTESTATION_HOST_PROFILE_ENV) or "").strip() != OPERATOR_SEPARATED_PROFILE:
        return ""
    env_name = PRODUCER_TOKEN_FILE_ENVS.get(str(channel or "").strip().lower(), "")
    path_value = str(source.get(env_name) or "").strip() if env_name else ""
    if not path_value:
        return ""
    payload = _read_private_file(Path(path_value), max_bytes=MAX_CREDENTIAL_FILE_BYTES)
    if not payload:
        return ""
    try:
        lines = [line.strip() for line in payload.decode("utf-8").splitlines() if line.strip()]
    except UnicodeError:
        return ""
    token = lines[0] if len(lines) == 1 else ""
    return token if is_strong_producer_token(token) else ""


def attestation_tokens_from_private_file(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    if str(source.get(ATTESTATION_HOST_PROFILE_ENV) or "").strip() != OPERATOR_SEPARATED_PROFILE:
        return {}
    path_value = str(source.get(ATTESTATION_TOKENS_FILE_ENV) or "").strip()
    if not path_value:
        return {}
    payload = _read_private_file(Path(path_value), max_bytes=MAX_CREDENTIAL_FILE_BYTES)
    if not payload:
        return {}
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict) or set(parsed) - {"codex", "hermes"}:
        return {}
    tokens: dict[str, str] = {}
    for producer, token_value in parsed.items():
        token = str(token_value or "").strip()
        if not is_strong_producer_token(token) or token in tokens:
            return {}
        tokens[token] = str(producer)
    return tokens


def scrub_producer_credential_environment(
    environ: MutableMapping[str, str] | None = None,
) -> None:
    target = os.environ if environ is None else environ
    for name in (
        *PRODUCER_TOKEN_FILE_ENVS.values(),
        ATTESTATION_TOKENS_FILE_ENV,
        ATTESTATION_HOST_PROFILE_ENV,
        *_LEGACY_TOKEN_ENVS,
    ):
        target.pop(name, None)


def _read_private_file(path: Path, *, max_bytes: int) -> bytes:
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
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > max_bytes
            or (os.name == "posix" and metadata.st_mode & 0o077)
        ):
            return b""
        payload = os.read(descriptor, max_bytes + 1)
        return payload if len(payload) <= max_bytes else b""
    finally:
        os.close(descriptor)
