from deploy.deployment_attempt_result import finalize_attempt


def test_smoke_failure_cannot_report_overall_success():
    result = finalize_attempt(technical_ok=True, smoke_exit_code=1, source_exit_code=0)
    assert result['technical_ok'] is True
    assert result['business_ok'] is False
    assert result['ok'] is False


def test_unrun_smoke_is_not_success():
    assert finalize_attempt(technical_ok=True, smoke_exit_code=None, source_exit_code=0)['ok'] is False


def test_source_failure_blocks_business_success():
    assert finalize_attempt(technical_ok=True, smoke_exit_code=0, source_exit_code=1)['ok'] is False


def test_all_boundaries_must_pass():
    assert finalize_attempt(technical_ok=True, smoke_exit_code=0, source_exit_code=0)['ok'] is True
    assert finalize_attempt(technical_ok=False, smoke_exit_code=0, source_exit_code=0)['ok'] is False
