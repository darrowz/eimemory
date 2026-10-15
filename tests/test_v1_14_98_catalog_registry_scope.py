import inspect

from eimemory.evaluation import capability_catalog as cc


def test_catalog_writes_use_registry_partition():
    src = inspect.getsource(cc.CapabilityCatalog.persist_spec_and_run) if hasattr(cc, "CapabilityCatalog") else inspect.getsource(cc)
    assert "_registry_scope(service, scope, capability_scope)" in src
    assert src.count("runtime_scope=registry_scope") == 2
    assert "scope=registry_scope, capability_scope=capability_scope" in src
