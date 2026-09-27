from pathlib import Path
import os
import pytest
from deploy.offline_state_review import open_snapshot, write_new_report
from deploy.rebuild_projection_snapshot import rebuild

@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixture")
@pytest.mark.parametrize("operation", ["read", "report", "rebuild"])
def test_intermediate_symlink_rejected_before_io(tmp_path, operation):
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    linked = alias / "nested"
    with pytest.raises(ValueError, match="ancestor"):
        if operation == "read":
            with open_snapshot(linked / "missing.sqlite"):
                pass
        elif operation == "report":
            write_new_report(linked / "report.json", {})
        else:
            rebuild(tmp_path / "backup.sqlite", linked / "output")
    assert list((outside / "nested").iterdir()) == []
