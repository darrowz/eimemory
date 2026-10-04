import ast
from pathlib import Path
import pytest


@pytest.mark.parametrize("name", ["capture_prior_health_snapshot.py", "verify_release_health.py"])
def test_loopback_health_opener_explicitly_disables_environment_proxies(name):
    path = Path(__file__).resolve().parents[1] / "deploy" / name
    tree = ast.parse(path.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "build_opener"]
    assert len(calls) == 1
    assert any(isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
               and arg.func.id == "ProxyHandler" and len(arg.args) == 1
               and isinstance(arg.args[0], ast.Dict) and not arg.args[0].keys
               for arg in calls[0].args)
    assert any(isinstance(arg, ast.Name) and arg.id == "_NoRedirect" for arg in calls[0].args)
