"""Compatibility alias: this module moved to eimemory.governance.safety.prompt_safety_openclaw."""

import sys as _sys

import eimemory.governance.safety.prompt_safety_openclaw as _target

if __name__ != "__main__":
    _sys.modules[__name__] = _target

if __name__ == "__main__":
    raise SystemExit(_target.main())
