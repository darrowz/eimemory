"""Pin bytecode suppression in controlled Python argv, including -I/-E."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

_PYTHON_NAME = re.compile(r"python(?:[23](?:\.\d+)?)?(?:\.exe)?", re.IGNORECASE)


def suppress_python_bytecode(command: list[str]) -> list[str]:
    """Do not rewrite shell commands, arbitrary console scripts or script args.

    Only the actual interpreter's basename is recognized; isolation flags
    ignore environment variables but do not override this explicit -B flag.
    Explicit py_compile/compileall and a child that resets Python flags are
    outside this guard. Enforce filesystem immutability separately.
    """
    argv = [str(part) for part in command]
    if not argv:
        return argv
    name = PurePosixPath(argv[0].replace("\\", "/")).name
    if _PYTHON_NAME.fullmatch(name):
        return [argv[0], "-B", *argv[1:]]
    return argv
