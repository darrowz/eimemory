"""Hermes importing eimemory from an immutable release never writes bytecode."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def test_release_import_through_plugin_helper_writes_no_pycache(tmp_path) -> None:
    release = tmp_path / ("a" * 40)
    plugin = release / "integrations" / "hermes" / "eimemory"
    plugin.mkdir(parents=True)
    shutil.copy(REPO / "integrations/hermes/eimemory/release_path.py", plugin / "release_path.py")
    core = release / "eimemory" / "adapters" / "hermes"
    core.mkdir(parents=True)
    for pkg in (release / "eimemory", release / "eimemory" / "adapters", core):
        (pkg / "__init__.py").write_text("")
    (core / "provider_core.py").write_text("VALUE = 1\n")
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('rp', {str(plugin / 'release_path.py')!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "sys.dont_write_bytecode = True; spec.loader.exec_module(m); sys.dont_write_bytecode = False\n"
        f"m.ensure_release_on_path(__import__('pathlib').Path({str(plugin / '__init__.py')!r}))\n"
        "import eimemory.adapters.hermes.provider_core as p\n"
        "assert p.VALUE == 1 and sys.dont_write_bytecode is True\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH"}}
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, check=True, timeout=60)
    assert not list(release.rglob("__pycache__"))


def test_plugin_entries_disable_bytecode_before_loading_release() -> None:
    text = (REPO / "integrations/hermes/eimemory/__init__.py").read_text(encoding="utf-8")
    assert text.index("sys.dont_write_bytecode = True") < text.index("spec_from_file_location")
    # The hook is part of the code.implementation implementation digest; editing
    # it rebinds the provider and invalidates its catalog activation (1.14.51).
    # It gets the guard through ensure_release_on_path() instead.
    hook = (REPO / "integrations/hermes/eimemory_hook/__init__.py").read_text(encoding="utf-8")
    assert "dont_write_bytecode" not in hook
    assert hook.index("_ensure_release_on_path()") < hook.index("from eimemory.")
