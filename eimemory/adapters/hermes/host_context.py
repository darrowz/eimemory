from __future__ import annotations

from threading import RLock

from eimemory.adapters.runtime.host_auth import (
    producer_token_from_private_file,
    scrub_producer_credential_environment,
)


_LOCK = RLock()
_PRODUCER_TOKEN = ""


def hermes_producer_token() -> str:
    """Load the host credential once and survive official plugin rescans."""

    global _PRODUCER_TOKEN
    with _LOCK:
        if not _PRODUCER_TOKEN:
            _PRODUCER_TOKEN = producer_token_from_private_file("hermes")
        scrub_producer_credential_environment()
        return _PRODUCER_TOKEN
