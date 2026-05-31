from __future__ import annotations

__all__ = [
    "build_governance_snapshot",
    "render_evolution_console",
    "run_rule_evolution_loop",
    "write_evolution_console",
]


def __getattr__(name: str):
    if name in {"render_evolution_console", "write_evolution_console"}:
        from eimemory.governance.console import render_evolution_console, write_evolution_console

        return {
            "render_evolution_console": render_evolution_console,
            "write_evolution_console": write_evolution_console,
        }[name]
    if name == "run_rule_evolution_loop":
        from eimemory.governance.rule_evolution import run_rule_evolution_loop

        return run_rule_evolution_loop
    if name == "build_governance_snapshot":
        from eimemory.governance.snapshot import build_governance_snapshot

        return build_governance_snapshot
    raise AttributeError(name)
