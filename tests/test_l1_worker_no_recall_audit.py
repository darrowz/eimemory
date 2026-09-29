"""The L1 queue must not trigger the retired label-signing audit."""
import sys
from types import SimpleNamespace

from eimemory.cli.l1_worker import drain_l1


def test_l1_drain_does_not_run_delegated_recall_review(tmp_path, monkeypatch):
    def unexpected_review(_runtime):
        raise AssertionError('retired audit invoked')

    monkeypatch.setitem(sys.modules, 'eimemory.evaluation.delegated_recall_review',
        SimpleNamespace(collect_and_review_configured=unexpected_review))
    report = drain_l1(root=str(tmp_path), limit=1)
    assert report['ok'] is True
    assert 'delegated_review' not in report
