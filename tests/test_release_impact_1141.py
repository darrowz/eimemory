from pathlib import Path
import pytest
from eimemory.governance.release.release_impact import _domains_for_change

@pytest.mark.parametrize('path,domains', [
 ('eimemory/compatibility/migration_helpers.py', {'storage.integrity'}),
 ('eimemory/intake/connectors.py', {'memory.governance'}),
 ('eimemory/judgment.py', {'memory.governance'}),
 ('eimemory/llm/gateway_pool.py', {'memory.recall'}),
 ('eimemory/raw/retrieval.py', {'memory.recall'}),
 ('scripts/openclaw_loop.py', {'channel.delivery','deployment.runtime'}),
])
def test_runtime_paths_have_explicit_domains(path,domains):
    assert _domains_for_change(Path(__file__).parents[1],path=path,ancestor='HEAD',current='HEAD') == domains
