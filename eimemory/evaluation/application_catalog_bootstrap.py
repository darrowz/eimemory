"""Deployment-time discovery for trusted dynamic evaluation catalog plugins.

This is intentionally a Python package entry-point boundary, not a runtime
payload parser.  A deployed application or plugin publishes a callable under
``eimemory.capability_catalog.bootstrap.v1``; that callable receives the
narrow :class:`ApplicationCatalogBootstrap` writer during process startup.
No CLI argument, adapter advertisement, database row, or JSON value is read
as a catalog registration.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points
from threading import RLock
from typing import Any

from eimemory.evaluation.capability_catalog import (
    CatalogBootstrapInstaller,
    CatalogResolutionError,
    CapabilityEvaluationCatalog,
    application_capability_catalog,
    bootstrap_application_capability_catalog,
)


APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP = "eimemory.capability_catalog.bootstrap.v1"
APPLICATION_CATALOG_BOOTSTRAP_SOURCE = "eimemory.installed-catalog-bootstrap"
_BOOTSTRAP_LOCK = RLock()


def _matching_entry_points(raw: Any) -> tuple[Any, ...]:
    """Support the small entry-point API differences across supported Python builds."""

    select = getattr(raw, "select", None)
    if callable(select):
        return tuple(select(group=APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP))
    if isinstance(raw, dict):
        values = raw.get(APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP) or ()
        return tuple(values) if isinstance(values, Iterable) and not isinstance(values, (str, bytes)) else ()
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        return tuple(
            item
            for item in raw
            if str(getattr(item, "group", "") or "") == APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP
        )
    return ()


def installed_application_catalog_installers() -> tuple[CatalogBootstrapInstaller, ...]:
    """Load only callable installers published by installed Python packages.

    Calling this is a deployment action.  It does not inspect user payloads
    and a malformed installed package raises a bounded bootstrap error rather
    than falling back to an empty catalog.
    """

    installers: list[CatalogBootstrapInstaller] = []
    for entry_point in _matching_entry_points(entry_points()):
        name = str(getattr(entry_point, "name", "") or "unknown")
        try:
            installer = entry_point.load()
        except Exception as exc:
            raise CatalogResolutionError(
                f"catalog_bootstrap_entrypoint_load_failed:{name}:{type(exc).__name__}"
            ) from exc
        if not callable(installer):
            raise CatalogResolutionError(f"catalog_bootstrap_entrypoint_not_callable:{name}")
        installers.append(installer)
    return tuple(installers)


def bootstrap_installed_application_catalog() -> CapabilityEvaluationCatalog | None:
    """Bootstrap the configured catalog from installed application/plugin code.

    Return ``None`` when no trusted deployment plugin is installed; callers
    keep running but dynamic L5 resolution then raises the stable
    ``catalog_not_configured`` reason.  A present but invalid plugin is a
    configuration error and is never converted into an empty catalog.
    """

    with _BOOTSTRAP_LOCK:
        try:
            return application_capability_catalog()
        except CatalogResolutionError as exc:
            if str(exc) != "catalog_not_configured":
                raise
        installers = installed_application_catalog_installers()
        if not installers:
            return None
        return bootstrap_application_capability_catalog(
            source_id=APPLICATION_CATALOG_BOOTSTRAP_SOURCE,
            installers=installers,
        )


__all__ = [
    "APPLICATION_CATALOG_BOOTSTRAP_ENTRYPOINT_GROUP",
    "APPLICATION_CATALOG_BOOTSTRAP_SOURCE",
    "bootstrap_installed_application_catalog",
    "installed_application_catalog_installers",
]
