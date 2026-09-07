"""Guard the bounded TEI candidate fan-out contract without launching Docker."""
import ast
from pathlib import Path


def test_reranker_permits_cover_client_batch_but_inference_stays_serial():
    source = Path(__file__).parents[1] / "deploy" / "provision_reranker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    command = next(node for node in ast.walk(tree) if isinstance(node, ast.List)
                   and any(isinstance(item, ast.Constant) and item.value == "--max-concurrent-requests"
                           for item in node.elts))
    values = [item.value if isinstance(item, ast.Constant) else None for item in command.elts]
    option = lambda name: values[values.index(name) + 1]
    assert int(option("--max-concurrent-requests")) >= int(option("--max-client-batch-size"))
    assert option("--max-batch-requests") == "1"
    assert option("--cpus") == "1"
    assert option("--memory") == "2g"
    assert option("--log-driver") == "none"
