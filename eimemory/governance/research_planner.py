"""Compatibility alias: this module moved to eimemory.governance.learning.research_planner."""

import sys as _sys

if __name__ != "__main__":
    import eimemory.governance.learning.research_planner as _target

    _sys.modules[__name__] = _target
else:
    # `python -m eimemory.governance.research_planner` support: do not pre-import the relocated module;
    # run it directly as __main__ so its real CLI entry point executes
    # cleanly (no sys.modules warnings, no loader mismatch).
    import runpy as _runpy

    _runpy.run_module("eimemory.governance.learning.research_planner", run_name="__main__", alter_sys=True)
