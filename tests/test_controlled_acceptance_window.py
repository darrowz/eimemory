"""A current signed acceptance cannot be superseded inside its own closure."""
from __future__ import annotations

from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from eimemory.evaluation.controlled_acceptance_window import (
    controlled_acceptance_window, ControlledAcceptanceWindowError,
)


def runtime(path):
    return SimpleNamespace(store=SimpleNamespace(sqlite=SimpleNamespace(path=path)))


def writer(path, output, entered, finished, timeout=5):
    entered.set()
    try:
        with controlled_acceptance_window(runtime(Path(path)), timeout_seconds=timeout):
            Path(output).write_text('later-attempt')
    except ControlledAcceptanceWindowError as exc:
        Path(output).write_text(str(exc))
    finally:
        finished.set()


def test_complete_window_blocks_other_process_after_nested_writer_returns(tmp_path):
    database = tmp_path / 'authority.sqlite'
    database.touch()
    output = tmp_path / 'later-attempt'
    ctx = multiprocessing.get_context('spawn')
    entered, finished = ctx.Event(), ctx.Event()
    process = ctx.Process(target=writer, args=(str(database), str(output), entered, finished))
    try:
        with controlled_acceptance_window(runtime(database)):
            # CPA execution inside the closure is reentrant. Ending that
            # writer must not release the outer readback/observation window.
            with controlled_acceptance_window(runtime(database), timeout_seconds=0):
                pass
            process.start()
            assert entered.wait(10)
            assert not finished.wait(.15)
            assert not output.exists()
        assert finished.wait(10)
        process.join(10)
        assert process.exitcode == 0 and output.read_text() == 'later-attempt'
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_another_process_gets_explicit_busy_without_publishing_an_attempt(tmp_path):
    database = tmp_path / 'authority.sqlite'
    database.touch()
    output = tmp_path / 'outcome'
    ctx = multiprocessing.get_context('spawn')
    entered, finished = ctx.Event(), ctx.Event()
    process = ctx.Process(target=writer, args=(str(database), str(output), entered, finished, .05))
    try:
        with controlled_acceptance_window(runtime(database)):
            process.start()
            assert finished.wait(10)
            process.join(10)
            assert output.read_text() == 'controlled_acceptance_window_busy'
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_window_exception_releases_os_and_local_locks(tmp_path):
    database = tmp_path / 'authority.sqlite'
    database.touch()
    with pytest.raises(RuntimeError, match='native_failed'):
        with controlled_acceptance_window(runtime(database)):
            with controlled_acceptance_window(runtime(database)):
                raise RuntimeError('native_failed')
    with controlled_acceptance_window(runtime(database), timeout_seconds=0):
        pass
    ctx = multiprocessing.get_context('spawn')
    entered, finished = ctx.Event(), ctx.Event()
    process = ctx.Process(target=writer,
        args=(str(database), str(tmp_path / 'outcome'), entered, finished, .2))
    try:
        process.start()
        assert finished.wait(10)
        process.join(10)
        assert process.exitcode == 0
        assert (tmp_path / 'outcome').read_text() == 'later-attempt'
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_thread_reentrancy_does_not_bypass_another_thread(tmp_path):
    database = tmp_path / 'authority.sqlite'
    database.touch()
    errors = []
    def contender():
        try:
            with controlled_acceptance_window(runtime(database), timeout_seconds=.02):
                pytest.fail('another thread entered the held window')
        except ControlledAcceptanceWindowError as exc:
            errors.append(str(exc))
    with controlled_acceptance_window(runtime(database)):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    assert errors == ['controlled_acceptance_window_busy']


@pytest.mark.parametrize('bad', [None, object(), '', ':memory:'])
def test_window_has_no_storage_less_bypass(bad):
    with pytest.raises(ControlledAcceptanceWindowError, match='authority_unavailable'):
        with controlled_acceptance_window(runtime(bad)):
            pytest.fail('invalid authority entered')


def test_missing_database_is_not_created_to_make_the_gate_pass(tmp_path):
    database = tmp_path / 'missing.sqlite'
    with pytest.raises(ControlledAcceptanceWindowError, match='authority_unavailable'):
        with controlled_acceptance_window(runtime(database)):
            pass
    assert list(tmp_path.iterdir()) == []


def test_database_replacement_invalidates_window_and_still_releases_lock(tmp_path):
    database = tmp_path / 'authority.sqlite'
    database.touch()
    with pytest.raises(ControlledAcceptanceWindowError, match='authority_replaced'):
        with controlled_acceptance_window(runtime(database)):
            database.rename(tmp_path / 'original.sqlite')
            database.touch()
    with controlled_acceptance_window(runtime(database), timeout_seconds=0):
        pass


def test_lock_timeout_does_not_turn_into_a_signed_cpa_attempt(monkeypatch, tmp_path):
    from eimemory.evaluation import controlled_acceptance_window as window
    from eimemory.evaluation import controlled_production_acceptance as cpa
    database = tmp_path / 'authority.sqlite'
    database.touch()
    owner = dict(tenant_id='default', agent_id='hongrui', workspace_id='work', user_id='operator')
    monkeypatch.setattr(cpa, 'load_controlled_policy', lambda *_: {})
    monkeypatch.setattr(cpa, '_run_controlled_production_acceptance_locked',
        lambda *_a, **_kw: pytest.fail('a busy window must not execute or sign'))
    @contextmanager
    def busy(*_a, **_kw):
        raise TimeoutError('state_lock_timeout')
        yield
    monkeypatch.setattr(window, 'interprocess_lock', busy)
    result = cpa.run_controlled_production_acceptance(runtime(database), scope=owner)
    assert result['ok'] is result['accepted'] is False
    assert result['status'] == 'blocked'
    assert result['blocked_reason'] == 'controlled_acceptance_window_busy'


@pytest.mark.parametrize('bad', [-1, True, 601, float('nan'), float('inf')])
def test_window_wait_budget_is_finite_and_bounded(tmp_path, bad):
    database = tmp_path / 'authority.sqlite'
    database.touch()
    with pytest.raises(ValueError, match='timeout_invalid'):
        with controlled_acceptance_window(runtime(database), timeout_seconds=bad):
            pass
