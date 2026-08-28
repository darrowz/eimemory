from __future__ import annotations

import pytest

import eimemory.api.runtime as runtime_module
import eimemory.evaluation.application_catalog_bootstrap as installed_bootstrap
import eimemory.evaluation.capability_catalog as catalog_module
import eimemory.governance.l5_reader as l5_reader_module
import eimemory.governance.release_lineage as release_lineage_module
import eimemory.governance.replay_dataset as replay_dataset_module
from eimemory.api.runtime import Runtime
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogCase,
    CatalogResolutionError,
    application_capability_catalog,
    application_capability_catalog_status,
    bootstrap_application_capability_catalog,
    default_capability_catalog,
    install_application_capability_catalog,
    resolve_application_capability_catalog,
)
from eimemory.evaluation.capability_graders import CapabilityGraderError


def _clear_application_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_module, "_APPLICATION_CATALOG", None)
    monkeypatch.setattr(catalog_module, "_APPLICATION_CATALOG_SOURCE", "")


def _install_dynamic_probe(bootstrap) -> None:
    bootstrap.register_executor(
        executor_id="eimemory.eval.bootstrap-probe",
        revision="v1",
        handler=lambda _input, _fixture, _runtime: {"decision": "traceable"},
    )
    bootstrap.register_case(
        CatalogCase(
            case_id="bootstrap_dynamic_contract",
            capability_id="bootstrap.dynamic",
            executor_id="eimemory.eval.bootstrap-probe",
            input_data={"request": "exercise bootstrap"},
            fixture={"fixture_id": "bootstrap-v1"},
            expected_invariants=[{"field": "decision", "op": "eq", "value": "traceable"}],
        )
    )


def test_application_catalog_is_explicitly_unconfigured_until_bootstrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)

    assert application_capability_catalog_status() == {
        "configured": False,
        "reason": "catalog_not_configured",
    }
    with pytest.raises(CatalogResolutionError, match="^catalog_not_configured$"):
        application_capability_catalog()
    with pytest.raises(CatalogResolutionError, match="^catalog_not_configured$"):
        default_capability_catalog()
    with pytest.raises(CatalogResolutionError, match="^catalog_not_configured$"):
        resolve_application_capability_catalog()


def test_bootstrap_registers_typed_code_and_seals_the_application_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)

    catalog = bootstrap_application_capability_catalog(
        source_id="eimemory.test.bootstrap",
        installers=(_install_dynamic_probe,),
    )

    assert resolve_application_capability_catalog() is catalog
    assert application_capability_catalog() is catalog
    assert application_capability_catalog_status() == {
        "configured": True,
        "source_id": "eimemory.test.bootstrap",
        "sealed": True,
        "case_count": 1,
        "executor_count": 1,
    }
    with pytest.raises(CatalogResolutionError, match="^capability_catalog_sealed$"):
        catalog.register_executor(
            executor_id="eimemory.eval.after-bootstrap",
            revision="v1",
            handler=lambda _input, _fixture, _runtime: {},
        )
    with pytest.raises(CatalogResolutionError, match="^capability_catalog_sealed$"):
        catalog.register_case(
            CatalogCase(
                case_id="after_bootstrap_contract",
                capability_id="bootstrap.dynamic",
                executor_id="eimemory.eval.bootstrap-probe",
                input_data={},
                fixture={},
                expected_invariants=[{"field": "decision", "op": "nonempty"}],
            )
        )
    with pytest.raises(CapabilityGraderError, match="^grader_registry_sealed$"):
        catalog.graders.register(
            grader_id="eimemory.grader.after-bootstrap",
            grader_type="code",
            revision="v1",
            handler=lambda _output, _rules, _evidence_ref, _context: {},
        )


def test_bootstrap_rejects_payload_shaped_installers_and_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)

    with pytest.raises(CatalogResolutionError, match="^catalog_bootstrap_installer_must_be_callable$"):
        bootstrap_application_capability_catalog(
            source_id="eimemory.test.bootstrap",
            installers=({"executor_id": "payload"},),
        )

    def register_payload_case(bootstrap) -> None:
        bootstrap.register_case(
            {
                "case_id": "payload_case",
                "capability": "payload.dynamic",
                "executor_id": "eimemory.eval.payload",
            }
        )

    with pytest.raises(CatalogResolutionError, match="^catalog_bootstrap_case_must_be_CatalogCase$"):
        bootstrap_application_capability_catalog(
            source_id="eimemory.test.bootstrap",
            installers=(register_payload_case,),
        )
    with pytest.raises(
        CatalogResolutionError,
        match="^catalog_bootstrap_requires_in_process_CapabilityEvaluationCatalog$",
    ):
        install_application_capability_catalog({"cases": []}, source_id="eimemory.test.bootstrap")
    with pytest.raises(CatalogResolutionError, match="^catalog_case_must_be_CatalogCase$"):
        CapabilityEvaluationCatalog().register_case(
            {
                "case_id": "payload_case",
                "capability": "payload.dynamic",
                "executor_id": "eimemory.eval.payload",
            }
        )


def test_application_catalog_cannot_be_replaced_after_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)
    first = bootstrap_application_capability_catalog(
        source_id="eimemory.test.bootstrap",
        installers=(_install_dynamic_probe,),
    )
    second = CapabilityEvaluationCatalog()
    second.register_executor(
        executor_id="eimemory.eval.second-probe",
        revision="v1",
        handler=lambda _input, _fixture, _runtime: {"decision": "traceable"},
    )
    second.register_case(
        CatalogCase(
            case_id="second_dynamic_contract",
            capability_id="second.dynamic",
            executor_id="eimemory.eval.second-probe",
            input_data={},
            fixture={},
            expected_invariants=[{"field": "decision", "op": "eq", "value": "traceable"}],
        )
    )

    assert install_application_capability_catalog(first, source_id="eimemory.test.bootstrap") is first
    with pytest.raises(CatalogResolutionError, match="^catalog_already_configured$"):
        install_application_capability_catalog(second, source_id="eimemory.test.second-bootstrap")
    assert second.sealed is False


class _InstalledEntryPoint:
    group = installed_bootstrap.APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP
    name = "dynamic-test-plugin"

    def load(self):
        return _install_dynamic_probe


def test_installed_plugin_bootstrap_is_usable_by_standard_runtime_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)
    monkeypatch.setattr(installed_bootstrap, "entry_points", lambda: (_InstalledEntryPoint(),))

    catalog = installed_bootstrap.bootstrap_installed_application_catalog()

    assert catalog is not None
    assert application_capability_catalog() is catalog
    assert installed_bootstrap.bootstrap_installed_application_catalog() is catalog


def test_runtime_bootstraps_installed_catalog_and_forwards_it_to_standard_paths(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)
    monkeypatch.setattr(installed_bootstrap, "entry_points", lambda: (_InstalledEntryPoint(),))
    replay_kwargs: dict = {}
    readiness_kwargs: dict = {}
    learning_kwargs: dict = {}

    monkeypatch.setattr(
        replay_dataset_module,
        "build_replay_dataset",
        lambda _runtime, **kwargs: replay_kwargs.update(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        l5_reader_module,
        "build_l5_effective_report",
        lambda _runtime, **kwargs: readiness_kwargs.update(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        "eimemory.governance.autonomous_learning.run_autonomous_learning_cycle",
        lambda _runtime, **kwargs: learning_kwargs.update(kwargs) or {"ok": True},
    )

    runtime = Runtime.create(root=tmp_path)
    try:
        assert runtime.capability_catalog is not None
        assert runtime.catalog_bootstrap_error == ""
        runtime.build_replay_dataset(scope={"agent_id": "runtime-bootstrap"})
        runtime.build_l5_readiness_report(scope={"agent_id": "runtime-bootstrap"})
        explicit_catalog = CapabilityEvaluationCatalog()
        runtime.run_autonomous_learning_cycle(
            scope={"agent_id": "runtime-bootstrap"},
            catalog=explicit_catalog,
        )
    finally:
        runtime.close()

    assert replay_kwargs["catalog"] is runtime.capability_catalog
    assert readiness_kwargs["catalog"] is runtime.capability_catalog
    assert learning_kwargs["catalog"] is explicit_catalog


def test_runtime_keeps_running_when_catalog_bootstrap_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "bootstrap_installed_application_catalog",
        lambda: (_ for _ in ()).throw(CatalogResolutionError("catalog_bootstrap_invalid")),
    )

    runtime = Runtime.create(root=tmp_path)
    try:
        assert runtime.capability_catalog is None
        assert runtime.catalog_bootstrap_error == "catalog_bootstrap_invalid"
        assert runtime.store.count_records(kinds=["memory"], scope={}) == 0
    finally:
        runtime.close()


def test_runtime_release_lineage_facades_keep_legacy_catalog_explicit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    dynamic_catalog = CapabilityEvaluationCatalog()
    runtime.capability_catalog = dynamic_catalog
    recorded: dict = {}
    resolved: dict = {}
    monkeypatch.setattr(
        release_lineage_module,
        "record_release_lineage",
        lambda _runtime, **kwargs: recorded.update(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        release_lineage_module,
        "current_release_lineage",
        lambda _runtime, **kwargs: resolved.update(kwargs) or {"ok": True},
    )

    try:
        runtime.record_release_lineage(
            repo_root=str(tmp_path),
            current_release=object(),
            legacy_compatibility=True,
        )
        runtime.current_release_lineage(
            repo_root=str(tmp_path),
            current_release=object(),
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert recorded["catalog"] is None
    assert resolved["catalog"] is None


def test_absent_installed_plugin_keeps_catalog_explicitly_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_application_catalog(monkeypatch)
    monkeypatch.setattr(installed_bootstrap, "entry_points", lambda: ())

    assert installed_bootstrap.bootstrap_installed_application_catalog() is None
    with pytest.raises(CatalogResolutionError, match="^catalog_not_configured$"):
        resolve_application_capability_catalog()
