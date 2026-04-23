from __future__ import annotations

import argparse
import secrets
from pathlib import Path


def rotate_token(unit_path: Path, *, token: str | None = None, show_token: bool = False) -> dict:
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
    report = {"ok": True, "unit_path": str(unit_path)}
    if show_token:
        report["token"] = token
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate the read-only eimemory console token in a systemd user unit.")
    parser.add_argument(
        "--unit",
        default=str(Path.home() / ".config/systemd/user/eimemory-console.service"),
        help="Path to eimemory-console.service",
    )
    parser.add_argument("--token", default="", help="Optional explicit token; omitted generates a random token")
    parser.add_argument("--show-token", action="store_true", help="Print the new token URL to stdout")
    args = parser.parse_args(argv)
    report = rotate_token(Path(args.unit), token=args.token or None, show_token=bool(args.show_token))
    print("token_rotated=true")
    print(f"unit_path={report['unit_path']}")
    if args.show_token:
        print(f"new_url=http://<host>:8765/{report['token']}")
    print("run: systemctl --user daemon-reload && systemctl --user restart eimemory-console.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
