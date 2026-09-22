"""PERF §4.2/§4.3: lexical prune/tie-group stay rejected; default path locked by P0."""
from __future__ import annotations

import os
from pathlib import Path

from eimemory.storage.runtime_store import RuntimeStore


def test_lexical_df_prune_default_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EIMEMORY_LEXICAL_DF_PRUNE", raising=False)
    monkeypatch.delenv("EIMEMORY_LEXICAL_TIEGROUP_OPT", raising=False)
    runtime = RuntimeStore(tmp_path)
    assert runtime.sqlite._lexical_prune_enabled() is False
    assert runtime.sqlite._lexical_tiegroup_opt_enabled() is False
    tokens = ["alpha", "deployment", "marker"]
    assert runtime.sqlite._rejected_lexical_df_prune_tokens(tokens) == tokens
    runtime.close()


def test_lexical_df_prune_opt_in_still_noop_without_quality_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_LEXICAL_DF_PRUNE", "1")
    monkeypatch.delenv("EIMEMORY_LEXICAL_PRUNE_QUALITY_GATE", raising=False)
    runtime = RuntimeStore(tmp_path)
    assert runtime.sqlite._lexical_prune_enabled() is True
    tokens = ["alpha", "deployment"]
    # Opt-in alone must not change tokens without quality-gate hook.
    assert runtime.sqlite._rejected_lexical_df_prune_tokens(tokens) == tokens
    runtime.close()


def test_default_path_never_reads_prune_flag_into_fts_sql(tmp_path: Path, monkeypatch) -> None:
    """Closed: rejected after counterexample; default FTS SQL stays composite ORDER BY."""
    monkeypatch.setenv("EIMEMORY_LEXICAL_DF_PRUNE", "1")
    monkeypatch.setenv("EIMEMORY_LEXICAL_TIEGROUP_OPT", "1")
    runtime = RuntimeStore(tmp_path)
    # Source inspection: _collect_fts_candidates body must still use composite ORDER BY.
    import inspect
    src = inspect.getsource(runtime.sqlite._collect_fts_candidates)
    assert "ORDER BY bm25_score ASC, i.quality_score DESC, i.updated_at DESC" in src
    assert "EIMEMORY_LEXICAL_DF_PRUNE" not in src
    runtime.close()
