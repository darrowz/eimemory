from __future__ import annotations

import argparse
import secrets
from pathlib import Path


def rotate_token(unit_path: Path, *, token: str | None = None) -> dict:
    if not unit_path.exists():
        raise FileNotFoundError(str(unit_path))
    token = token or secrets.token_urlsafe(24)
    lines = unit_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.startswith("Environment=EIMEMORY_CONSOLE_TOKEN="):
            output.append(f"Environment=EIMEMORY_CONSOLE_TOKEN={token}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        raise ValueError("EIMEMORY_CONSOLE_TOKEN entry not found")
    unit_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return {"ok": True, "unit_path": str(unit_path), "token": token}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate the read-only eimemory console token in a systemd user unit.")
    parser.add_argument(
        "--unit",
        default=str(Path.home() / ".config/systemd/user/eimemory-console.service"),
        help="Path to eimemory-console.service",
    )
    parser.add_argument("--token", default="", help="Optional explicit token; omitted generates a random token")
    args = parser.parse_args(argv)
    report = rotate_token(Path(args.unit), token=args.token or None)
    print(f"new_url=http://<host>:8765/{report['token']}")
    print("run: systemctl --user daemon-reload && systemctl --user restart eimemory-console.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
