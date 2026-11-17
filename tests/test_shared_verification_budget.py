"""One request allowance survives nested scopes and copied worker contexts."""
from contextvars import copy_context
from threading import Barrier, Thread
from time import perf_counter

import pytest

from eimemory.retrieval.verification_budget import (
    _BUDGET, bounded_verification_call, final_authority_deadline, verification_budget_scope,
)
from eimemory.retrieval.caller_assistance import _HOST_DEADLINE, host_delivery_deadline


def test_nested_engine_scope_keeps_outer_call_count_and_deadline():
    hard = perf_counter() + 5
    with verification_budget_scope(), host_delivery_deadline(hard):
        original = _BUDGET.get()
        with verification_budget_scope(), bounded_verification_call(1):
            assert _BUDGET.get() is original and _HOST_DEADLINE.get() == hard
        assert _BUDGET.get() is original and original.calls == 1
        assert final_authority_deadline(0) > perf_counter()
        with pytest.raises(ValueError, match="invalid_or_repeated"):
            with bounded_verification_call(1):
                pytest.fail("second call started")
    assert _BUDGET.get() is None


def test_two_workers_in_one_request_cannot_both_start_a_verifier():
    barrier = Barrier(2)
    results = []
    hard = perf_counter() + 5
    with verification_budget_scope(), host_delivery_deadline(hard):
        def run():
            barrier.wait(timeout=2)
            try:
                with verification_budget_scope(), bounded_verification_call(1):
                    assert _HOST_DEADLINE.get() == hard
                    results.append("started")
            except ValueError:
                results.append("denied")
        contexts = [copy_context(), copy_context()]
        workers = [Thread(target=lambda ctx=ctx: ctx.run(run)) for ctx in contexts]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()
        assert sorted(results) == ["denied", "started"]
        assert _BUDGET.get().calls == 1 and final_authority_deadline(0) > perf_counter()


def test_failed_call_cannot_grant_authority_time_or_restore_a_fresh_slot():
    with verification_budget_scope():
        with pytest.raises(TimeoutError):
            with bounded_verification_call(1):
                raise TimeoutError
        assert final_authority_deadline(0) == 0 and _BUDGET.get().calls == 1
        with verification_budget_scope(), pytest.raises(ValueError):
            with bounded_verification_call(1):
                pass
