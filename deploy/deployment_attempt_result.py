"""Independent deployment-attempt results, not an L5 qualification receipt."""
from __future__ import annotations


def finalize_attempt(*, technical_ok: bool, smoke_exit_code: int | None,
                     source_exit_code: int | None) -> dict[str, bool]:
    """Keep technical success without promoting failed or missing business evidence."""
    business_ok = type(smoke_exit_code) is int and smoke_exit_code == 0
    source_ok = type(source_exit_code) is int and source_exit_code == 0
    return {
        'technical_ok': technical_ok is True,
        'business_ok': business_ok,
        'source_ok': source_ok,
        'ok': technical_ok is True and business_ok and source_ok,
    }
