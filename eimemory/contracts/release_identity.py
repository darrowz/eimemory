"""ARCH-01: release identity value object shared by storage and governance."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """Deployment authority plus a descriptive release label.

    ``version`` is retained for diagnostics only; authority is commit/receipt/session.
    """

    commit: str
    version: str
    receipt_id: str
    session_id: str

    @property
    def complete(self) -> bool:
        return bool(len(self.commit) == 40 and self.receipt_id and self.session_id)


def release_identity_payload(release: ReleaseIdentity) -> dict[str, str]:
    return {
        "release_commit": release.commit,
        "release_version": release.version,
        "deployment_receipt_id": release.receipt_id,
        "release_session_id": release.session_id,
    }


def release_authority_key(release: ReleaseIdentity) -> tuple[str, str, str]:
    return (
        str(release.commit or "").strip().lower(),
        str(release.receipt_id or "").strip(),
        str(release.session_id or "").strip(),
    )


def same_release_authority(
    left: ReleaseIdentity | None,
    right: ReleaseIdentity | None,
) -> bool:
    return bool(
        isinstance(left, ReleaseIdentity)
        and isinstance(right, ReleaseIdentity)
        and left.complete
        and right.complete
        and release_authority_key(left) == release_authority_key(right)
    )
