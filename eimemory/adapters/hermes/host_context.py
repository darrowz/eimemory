from __future__ import annotations

from threading import RLock
import os

from eimemory.adapters.runtime.host_auth import (
    producer_token_from_private_file,
)
from eimemory.adapters.hermes.profile_context import current_home,profile_value


_LOCK = RLock()
_PRODUCER_TOKEN = ""
_PRODUCER_TOKENS = {}


def hermes_producer_token() -> str:
    """Read the active owner's private credential without changing gateway env."""

    global _PRODUCER_TOKEN
    with _LOCK:
        names=('EIMEMORY_ATTESTATION_HOST_PROFILE','EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE')
        settings={}
        for name in names:
            value=profile_value(name)
            settings[name]=value if value is not None else os.environ.get(name,'')
        home=current_home()
        key=(home,*(settings[name] for name in names))
        # Revalidate permission changes and rotations even when discovery has
        # already seen this path. A cached token cannot outlive its authority.
        token=producer_token_from_private_file('hermes',environ=settings)
        if token:_PRODUCER_TOKENS[key]=token
        else:_PRODUCER_TOKENS.pop(key,None)
        if not home and token:_PRODUCER_TOKEN=token
        return token
