"""Bounded, registry-backed evaluation primitives for dynamic capabilities.

Evaluation descriptors are data.  They can select only executors and graders
that trusted application code registered in-process; descriptors never contain
commands, scripts, or executable selector expressions.
"""

from eimemory.evaluation.capability_catalog import (
    ApplicationCatalogBootstrap,
    CatalogBootstrapInstaller,
    CapabilityEvaluationCatalog,
    CatalogCase,
    CatalogResolutionError,
    application_capability_catalog,
    application_capability_catalog_status,
    bootstrap_application_capability_catalog,
    default_capability_catalog,
    execution_evidence_digest,
    install_application_capability_catalog,
)
from eimemory.evaluation.capability_graders import (
    CapabilityGraderRegistry,
    grade_schema_rules,
)
from eimemory.evaluation.application_catalog_bootstrap import (
    APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP,
    bootstrap_installed_application_catalog,
    installed_application_catalog_installers,
)
# These runners predate the dynamic capability catalog.  They are exposed
# lazily below: importing Runtime first imports the bootstrap submodule, which
# necessarily initializes this package.  Eagerly importing runners here would
# pull the runtime adapters back in and create a Runtime import cycle.

__all__ = [
    "CapabilityEvaluationCatalog",
    "ApplicationCatalogBootstrap",
    "CatalogBootstrapInstaller",
    "APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP",
    "CapabilityGraderRegistry",
    "CatalogCase",
    "CatalogResolutionError",
    "application_capability_catalog",
    "application_capability_catalog_status",
    "bootstrap_application_capability_catalog",
    "bootstrap_installed_application_catalog",
    "default_capability_catalog",
    "execution_evidence_digest",
    "grade_schema_rules",
    "install_application_capability_catalog",
    "installed_application_catalog_installers",
    "run_actionable_memory_eval",
    "run_evaluation",
    "run_livingmem_eval",
    "run_locomo",
    "run_longmemeval",
    "run_memory_eval_ci",
    "run_production_recall_eval",
    "run_public_memory_benchmark",
    "run_real_task_replay",
]


_RUNNER_COMPATIBILITY_EXPORTS = {
    "run_actionable_memory_eval": ("actionable_memory", "run_actionable_memory_eval"),
    "run_evaluation": ("framework", "run_evaluation"),
    "run_livingmem_eval": ("livingmem", "run_livingmem_eval"),
    "run_locomo": ("locomo", "run_locomo"),
    "run_longmemeval": ("longmemeval", "run_longmemeval"),
    "run_memory_eval_ci": ("framework", "run_memory_eval_ci"),
    "run_production_recall_eval": ("production_recall", "run_production_recall_eval"),
    "run_public_memory_benchmark": ("public_benchmarks", "run_public_memory_benchmark"),
    "run_real_task_replay": ("task_replay", "run_real_task_replay"),
}


def __getattr__(name: str):
    """Lazily restore the stable evaluation-runner facade.

    This compatibility surface has no catalog side effects.  In particular it
    never creates a v3 catalog or turns an absent application bootstrap into a
    legacy/default evaluation path.
    """

    try:
        module_name, attribute_name = _RUNNER_COMPATIBILITY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
