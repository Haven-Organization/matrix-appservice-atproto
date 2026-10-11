"""Claiming an ATProto handle for a local Matrix user.

The one entry point every path that gives a Matrix user an ATProto identity
must go through, so the reserved-name rules can't be bypassed by whichever
feature happens to create the account. Order of checks, cheapest first:

1. The MXID is a local user (``synapse.server_name``).
2. ``HandlePolicy.check`` -- syntax, and the reserved lists.
3. Nobody else holds the label.
4. ``HandleRoutingProber`` -- the hostname really reaches this bridge, i.e.
   no other service on the site already answers for that subdomain.
5. The atomic claim itself (which re-decides 3 under a race).
"""

from __future__ import annotations

import logging

from bridge.atproto.handle_probe import HandleRoutingProber, ProbeResult
from bridge.atproto.handles import HandlePolicy, Rejection, RejectionKind, label_for_localpart
from bridge.repository import BridgeRepository, HandleClaim

logger = logging.getLogger(__name__)


class HandleRejected(Exception):
    """The user can't be given a handle; ``rejection.message`` is safe to
    show them."""

    def __init__(self, rejection: Rejection) -> None:
        super().__init__(rejection.message)
        self.rejection = rejection


async def claim_handle_for_user(
    repository: BridgeRepository,
    policy: HandlePolicy,
    prober: HandleRoutingProber | None,
    *,
    matrix_user_id: str,
    server_name: str,
) -> HandleClaim:
    """Return ``matrix_user_id``'s handle claim, creating it if needed.

    Idempotent: a user who already holds a label gets that claim back
    unchanged, even if the label has since become reserved (see
    ``audit_handle_claims`` for how that gets surfaced instead).

    ``prober`` is ``None`` when ``handles.verify_routing`` is off.
    """
    localpart, _, server = matrix_user_id.removeprefix("@").partition(":")
    if not matrix_user_id.startswith("@") or server != server_name:
        raise HandleRejected(
            Rejection(RejectionKind.INVALID, f"{matrix_user_id} is not a user on {server_name}.")
        )

    existing = await repository.get_handle_claim_for_user(matrix_user_id)
    if existing is not None:
        return existing

    label = label_for_localpart(localpart)
    rejection = policy.check(label)
    if rejection is not None:
        raise HandleRejected(rejection)

    taken = Rejection(RejectionKind.TAKEN, f"'{label}' is already another user's handle.")
    if await repository.get_handle_claim(label) is not None:
        raise HandleRejected(taken)

    if prober is not None:
        result = await prober.probe(label)
        if result is ProbeResult.FOREIGN:
            raise HandleRejected(
                Rejection(
                    RejectionKind.NOT_ROUTED,
                    f"'{policy.handle_for_label(label)}' is already in use by another service on this server.",
                )
            )
        if result is ProbeResult.UNREACHABLE:
            # An operator problem (wildcard DNS/TLS), not the user's --
            # but still fail closed: a handle that can't be fetched fails
            # ATProto verification and shows up as "handle.invalid".
            logger.warning(
                "Refusing handle %s: the hostname is unreachable. Check the wildcard DNS record "
                "and certificate for *.%s.",
                policy.handle_for_label(label),
                policy.domain,
            )
            raise HandleRejected(
                Rejection(
                    RejectionKind.NOT_ROUTED,
                    f"'{policy.handle_for_label(label)}' can't be reached right now. Ask the server admin.",
                )
            )

    claim = await repository.claim_handle(label, matrix_user_id)
    if claim is None:
        raise HandleRejected(taken)
    logger.info("%s claimed handle %s", matrix_user_id, policy.handle_for_label(label))
    return claim


async def audit_handle_claims(repository: BridgeRepository, policy: HandlePolicy) -> list[tuple[HandleClaim, Rejection]]:
    """Existing claims the current policy would no longer grant -- e.g. the
    operator added a subdomain to ``handles.reserved`` after a user already
    held it. Nothing is revoked here; taking a live identity's handle away
    is the operator's call, this only finds them."""
    conflicts = []
    for claim in await repository.list_handle_claims():
        rejection = policy.check(claim.label)
        if rejection is not None:
            conflicts.append((claim, rejection))
    return conflicts
