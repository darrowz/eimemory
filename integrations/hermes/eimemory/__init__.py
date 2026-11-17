"""Standalone Hermes MemoryProvider registration for eimemory."""

from __future__ import annotations

import importlib.util
import json
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
    _source_ids_from_env,
    hermes_host_tool_context_matches,
)
from eimemory.adapters.hermes.profile_context import resolve_host_binding, verify_owner
from eimemory.adapters.hermes.provider_registry import (
    HermesProviderSessionAlreadyBound,
    bind_hermes_provider,
    get_hermes_provider,
    moving_hermes_provider,
    unbind_hermes_provider,
)


class EIMemoryProvider(HermesMemoryProviderCore, MemoryProvider):
    """Hermes-native provider backed by the authenticated eimemory RPC."""

    def initialize(self, session_id: str, **kwargs) -> None:
        if getattr(self, '_closed', False) or getattr(self, '_closing', False):
            raise ValueError('hermes_provider_closed')
        self._retained_tool_owner = None
        session_id = str(session_id or '').strip() or 'hermes-session'
        if self._host_binding is not None:
            # A MemoryManager may initialize its provider more than once. Do
            # not reset verified turns, pending feedback or queues on reentry,
            # and never mutate the identity of an already initialized worker.
            if not self._same_initialization(self, self._session_id, kwargs):
                raise ValueError('hermes_session_owner_change_requires_new_provider')
            if getattr(self, '_initialized', False):
                if session_id != self._session_id:
                    self.on_session_switch(session_id)
                else:
                    bind_hermes_provider(session_id=session_id, provider=self)
                # A prior unavailable configuration may have recovered; this
                # does not require resetting the bound session's state.
                if not self._active:
                    self._active = self._client_injected or self.is_available()
                    if self._active and self._write_enabled:
                        self._start_write_worker()
                return
        existing=get_hermes_provider(session_id,hermes_home=kwargs.get('hermes_home'))
        if existing is not None and existing is not self:
            # v0.23 hosts retain a candidate in their tool table after an
            # initialize failure. Hooks still name the initialized registry
            # owner. Remember only an exact compatible collision, without
            # rebinding either instance or accepting the failed initialize.
            try:
                compatible = self._host_binding is None and self._same_initialization(existing, session_id, kwargs)
            except Exception:
                compatible = False
            if compatible:
                self._retained_tool_owner = (ref(existing), existing._session_id, existing._hermes_home)
            raise HermesProviderSessionAlreadyBound()
        try:
            super().initialize(session_id, **kwargs)
            bind_hermes_provider(session_id=self._session_id, provider=self)
        except Exception:
            self._active = False
            raise
        self._initialized = True

    def _same_initialization(self, owner, session_id, context) -> bool:
        if not isinstance(owner, HermesMemoryProviderCore) or owner._host_binding is None:
            return False
        verify_owner(owner._host_binding)
        binding = resolve_host_binding(context)
        scope = dict(binding['scope']) if binding.get('scope') else self._scope_from_context(context)
        sources = list(binding['source_ids']) if binding.get('source_ids') else _source_ids_from_env('default')
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

    def can_reuse_for_session(self, session_id: str, **context) -> bool:
        """Read-only opt-in for an owning host's exclusive manager handoff.

        This grants no registry lookup, rebinding or lifecycle authority. The
        host must already own this provider through its canonical manager.
        """
        if (not getattr(self, '_initialized', False) or not self._active
                or getattr(self, '_closed', False) or getattr(self, '_closing', False)):
            return False
        try:
            return self._same_initialization(self, str(session_id or '').strip(), context) is True
        except Exception:
            return False

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if getattr(self, '_closed', False) or getattr(self, '_closing', False):
            return json.dumps({'ok': False, 'error': 'hermes_provider_closed'})
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
        if getattr(self, '_closed', False) or getattr(self, '_closing', False):
            raise ValueError('hermes_provider_closed')
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
        if getattr(self, '_closed', False):
            return
        self._closing = True
        self._active = False
        self._retained_tool_owner = None
        try:
            super().shutdown()
        finally:
            self._closed = True
            self._active = False
            unbind_hermes_provider(self)

    def _safe_call_with(self, client, method, params, *, metadata_deadline=None):
        # Draining during shutdown still uses the normal owner checks. Once
        # teardown has returned, late hooks and stale tool tables cannot emit
        # RPCs from the released provider, even if they retain its object.
        if getattr(self, '_closed', False):
            return {'ok': False, 'bypassed': True, 'error': 'hermes_provider_closed', 'result': None}
        return super()._safe_call_with(client, method, params, metadata_deadline=metadata_deadline)


def register(ctx) -> None:
    ctx.register_memory_provider(EIMemoryProvider())
