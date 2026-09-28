"""Public API; importing a leaf module must not initialize the full Runtime."""
from __future__ import annotations

from pathlib import Path as _Path
import re as _re
import sys as _sys

# Before any local import. The loader may have cached this __init__ BEFORE
# executing it: only pre-start -B or the OS read-only mount covers that write.
_release_root = _Path(__file__).resolve().parent.parent
if _release_root.parent.name == 'releases' and _re.fullmatch(r'[0-9a-fA-F]{40}', _release_root.name):
    _sys.dont_write_bytecode = True
    from eimemory.core.release_source_guard import install_release_write_guard as _install_guard
    _install_guard(_release_root)

from eimemory.version import __version__

__all__ = ['Runtime', '__version__']


def __getattr__(name: str):
    if name == 'Runtime':
        from eimemory.api.runtime import Runtime
        globals()['Runtime'] = Runtime
        return Runtime
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__():
    return sorted(set(globals()) | set(__all__))
