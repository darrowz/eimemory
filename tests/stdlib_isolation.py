"""Isolate fault-injected stdlib objects from pytest's process-wide objects."""
from pathlib import Path
from types import SimpleNamespace


def isolate_stdlib(module, monkeypatch):
    class PrivatePath(type(Path())):
        pass
    if hasattr(module, "os"):
        os_obj = module.os
        proxy = SimpleNamespace(**vars(os_obj))
        if hasattr(os_obj, "path"):
            proxy.path = SimpleNamespace(**vars(os_obj.path))
        monkeypatch.setattr(module, "os", proxy)
    for name in ("sys", "pathlib"):
        shared = getattr(module, name, None)
        if shared is not None:
            proxy = SimpleNamespace(**vars(shared))
            if name == "pathlib" and shared.Path is Path:
                proxy.Path = PrivatePath
            monkeypatch.setattr(module, name, proxy)
    if getattr(module, "Path", None) is Path:
        monkeypatch.setattr(module, "Path", PrivatePath)
