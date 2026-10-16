import importlib.util, os, re
from pathlib import Path

def _m():
    spec = importlib.util.spec_from_file_location("rot", Path(__file__).parents[1] / "deploy" / "rotate_releases.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _mk(root, names):
    root.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        d = root / n; d.mkdir(); os.utime(d, (1000 + i, 1000 + i))

C = [f"{i:040x}" for i in range(8)]  # C[7] newest

def test_keeps_newest_plus_current_and_rollback(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_KEEP_RELEASES", raising=False)
    m = _m(); r = tmp_path / "releases"; _mk(r, C)
    (r / ".eimemory-stage-x").mkdir(); (r / "notes").mkdir(); (r / f"{'f'*40}").symlink_to(r / C[0])
    rc = m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "none"),
                 "--current-commit", C[0], "--previous-commit", C[1]])
    assert rc == 0
    left = {p.name for p in r.iterdir()}
    assert {C[0], C[1], ".eimemory-stage-x", "notes", "f" * 40} <= left
    # default keep 5 newest non-protected (C[3..7]) plus protected C[0], C[1]
    assert {c for c in C if c in left} == {C[0], C[1], *C[3:]}

def test_keep_n_removes_oldest_only(tmp_path, monkeypatch):
    m = _m(); r = tmp_path / "releases"; _mk(r, C)
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASES", "2")
    m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "s"), "--current-commit", C[0], "--previous-commit", C[3]])
    assert {p.name for p in r.iterdir()} == {C[0], C[3], C[7], C[6]}

def test_snapshots_protect_current_prefix_and_zero_disables(tmp_path, monkeypatch):
    m = _m(); s = tmp_path / "snap"; _mk(s, [f"{c}-a{i}" for i, c in enumerate(C)])
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASE_SNAPSHOTS", "1")
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASES", "0")
    m.main(["--releases-root", str(tmp_path / "r"), "--snapshot-root", str(s), "--current-commit", C[2]])
    assert {p.name.split("-")[0] for p in s.iterdir()} == {C[2], C[7]}

def test_dry_run_and_invalid_current(tmp_path, monkeypatch):
    m = _m(); r = tmp_path / "releases"; _mk(r, C)
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASES", "1")
    m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "s"), "--current-commit", C[0], "--dry-run"])
    assert len(list(r.iterdir())) == 8
    assert m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "s"), "--current-commit", "bad"]) == 2
