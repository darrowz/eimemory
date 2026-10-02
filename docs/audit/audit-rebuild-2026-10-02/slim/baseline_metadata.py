"""Reproduce tracked-file logical sizes without importing project code."""
from pathlib import Path
import argparse
import json
import stat
import subprocess


def summarize(rows):
    return {
        "files": len(rows),
        "bytes": sum(row["bytes"] for row in rows),
        "python_files": sum(row["suffix"] == ".py" for row in rows),
        "python_bytes": sum(row["bytes"] for row in rows if row["suffix"] == ".py"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output == source or source in output.parents:
        raise SystemExit("Output must be outside the source checkout")
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != "4763001d1c4f3f4af6e6dda17e008e1b4c9b5609":
        raise SystemExit("This baseline tool is pinned to source commit 4763001d")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True):
        raise SystemExit("Source checkout must be clean for the pinned baseline")
    raw = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"])
    rows = []
    for path in sorted(value.decode() for value in raw.split(b"\0") if value):
        info = (source / path).lstat()
        parts = path.split("/")
        top = parts[0] if len(parts) > 1 else "(root)"
        group = (parts[1] if len(parts) > 2 else "(package root)") if top == "eimemory" else None
        rows.append({
            "path": path, "bytes": info.st_size,
            "type": "symlink" if stat.S_ISLNK(info.st_mode) else "file",
            "suffix": Path(path).suffix, "top_level": top, "package_group": group,
        })
    result = {
        "source_commit": commit,
        "source_version": "1.14.31",
        "method": "git ls-files tracked paths; lstat logical byte size; no source import, install, project execution or package build",
        "all_tracked": summarize(rows),
        "top_level": {key: summarize([row for row in rows if row["top_level"] == key]) for key in sorted({row["top_level"] for row in rows})},
        "package_groups": {key: summarize([row for row in rows if row["package_group"] == key]) for key in sorted({row["package_group"] for row in rows if row["package_group"] is not None})},
        "largest_package_files": sorted([row for row in rows if row["top_level"] == "eimemory"], key=lambda row: (-row["bytes"], row["path"]))[:25],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "baseline-summary.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "baseline-files.json").write_text(json.dumps(rows, indent=2) + "\n")
    (output / "baseline-files.tsv").write_text("path\tbytes\ttype\ttop_level\tpackage_group\n" + "".join(f"{r['path']}\t{r['bytes']}\t{r['type']}\t{r['top_level']}\t{r['package_group'] or ''}\n" for r in rows))
    print(json.dumps(result["all_tracked"], indent=2))


if __name__ == "__main__":
    main()
