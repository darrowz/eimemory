import sys
import pytest
from eimemory.core.python_invocation import suppress_python_bytecode


def test_known_interpreter_is_protected_even_when_custom_named(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/runtime/interpreter-custom")
    command = [sys.executable, "-I", "-c", "import module"]
    assert suppress_python_bytecode(command) == [sys.executable, "-B", *command[1:]]
    assert command[1] == "-I"


@pytest.mark.parametrize("command", [[], ["bash", "python -I tool.py"], ["custom-cli", "python"], ["env", "python", "-I"]])
def test_noninterpreter_commands_are_not_rewritten(command):
    assert suppress_python_bytecode(command) == command
