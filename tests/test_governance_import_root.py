"""A release gate must not import a checkout through the controller environment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest




def test_governance_launcher_has_explicit_release_impact():
    from eimemory.governance.release_impact import _domains_for_change
    domains = _domains_for_change(Path(__file__).parents[1],
        path='deploy/run_with_governance_env.py', ancestor='HEAD', current='HEAD')
    assert domains == {'deployment.runtime', 'memory.governance', 'code.evolution'}
