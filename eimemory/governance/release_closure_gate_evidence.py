"""Compatibility alias: this module moved to eimemory.governance.release.release_closure_gate_evidence."""

import sys as _sys

if __name__ != "__main__":
    import eimemory.governance.release.release_closure_gate_evidence as _target

    _sys.modules[__name__] = _target
else:
    # `python -m eimemory.governance.release_closure_gate_evidence` support: do not pre-import the relocated module;
    # run it directly as __main__ so its real CLI entry point executes
    # cleanly (no sys.modules warnings, no loader mismatch).
    import runpy as _runpy

    _runpy.run_module("eimemory.governance.release.release_closure_gate_evidence", run_name="__main__", alter_sys=True)
