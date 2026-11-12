"""Standalone Hermes MemoryProvider registration for eimemory."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from weakref import ref

_release_path_file = Path(__file__).resolve().parent / "release_path.py"
_release_path_spec = importlib.util.spec_from_file_location(
    "_eimemory_hermes_release_path",
    _release_path_file,
)
if _release_path_spec is None or _release_path_spec.loader is None:
    raise ImportError(f"missing Hermes release path helper: {_release_path_file}")
_release_path = importlib.util.module_from_spec(_release_path_spec)
_release_path_spec.loader.exec_module(_release_path)
_release_path.ensure_release_on_path(Path(__file__))

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # Allows package validation without Hermes dependency.
    class MemoryProvider:
        pass

from eimemory.adapters.hermes.provider_core import (
    HermesMemoryProviderCore,
    hermes_host_tool_context_matches,
)
from eimemory.adapters.hermes.profile_context import resolve_host_binding, verify_owner
from eimemory.adapters.hermes.provider_registry import (
    bind_hermes_provider,
    get_hermes_provider,
    moving_hermes_provider,
    unbind_hermes_provider,
)


class EIMemoryProvider(HermesMemoryProviderCore, MemoryProvider):
    """Hermes-native provider backed by the authenticated eimemory RPC."""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._retained_tool_owner = None
        existing=get_hermes_provider(session_id,hermes_home=kwargs.get('hermes_home'))
        if existing is not None and existing is not self:
            # v0.23 hosts retain a candidate in their tool table after an
            # initialize failure. Hooks still name the initialized registry
            # owner. Remember only an exact compatible collision, without
            # rebinding either instance or accepting the failed initialize.
            if self._host_binding is None and self._same_initialization(existing, session_id, kwargs):
                self._retained_tool_owner = (ref(existing), existing._session_id, existing._hermes_home)
            raise ValueError('hermes_provider_session_already_bound')
        super().initialize(session_id, **kwargs)
        bind_hermes_provider(session_id=self._session_id, provider=self)

    def _same_initialization(self, owner, session_id, context) -> bool:
        if not isinstance(owner, HermesMemoryProviderCore) or owner._host_binding is None:
            return False
        verify_owner(owner._host_binding)
        binding = resolve_host_binding(context)
        scope = dict(binding['scope']) if binding.get('scope') else self._scope_from_context(context)
        sources = list(binding['source_ids']) if binding.get('source_ids') else self._source_ids()
        expected_sources = context.get('expected_source_ids')
        if expected_sources is not None:
            from eimemory.models.source_partitions import normalize_source_ids
            expected_sources = list(normalize_source_ids(expected_sources))
        return (
            session_id == owner._session_id
            and {**binding, 'scope': scope} == owner._host_binding
            and sources == owner._source_ids()
            and {key: str(context.get(key) or '') for key in
                 ('agent_identity', 'agent_workspace', 'user_id')} == owner._identity_context
            and context.get('expected_scope') == owner._expected_scope
            and expected_sources == owner._expected_source_ids
            and (str(context.get('agent_context') or 'primary').strip().lower()
                 not in {'cron', 'flush', 'subagent'}) == owner._write_enabled
        )

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        retained = getattr(self, '_retained_tool_owner', None)
        if retained is not None and tool_name in {'eimemory_recall', 'eimemory_status'}:
            owner = retained[0]()
            if (owner is not None and owner._session_id == retained[1]
                    and get_hermes_provider(retained[1], hermes_home=retained[2]) is owner
                    and hermes_host_tool_context_matches(owner, tool_name, args)):
                # The normal owner guard, RPC auth and exact-argument capture
                # check still run on the original initialized provider.
                return owner.handle_tool_call(tool_name, args, **kwargs)
        return super().handle_tool_call(tool_name, args, **kwargs)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        old_session_id = self._session_id
        with moving_hermes_provider(old_session_id=old_session_id,
                new_session_id=new_session_id or old_session_id,provider=self):
            super().on_session_switch(
                new_session_id,
                parent_session_id=parent_session_id,
                reset=reset,
                rewound=rewound,
                **kwargs,
            )

    def shutdown(self) -> None:
        self._retained_tool_owner = None
        try:
            super().shutdown()
        finally:
            unbind_hermes_provider(self)


def register(ctx) -> None:
    ctx.register_memory_provider(EIMemoryProvider())
