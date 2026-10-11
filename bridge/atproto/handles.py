"""ATProto handle policy: which ``<label>.<bridge.domain>`` a Matrix user gets.

An ATProto handle is a DNS hostname and nothing else -- there is no
``user@domain`` or ``domain/user`` form -- and it is verified by fetching
``https://<handle>/.well-known/atproto-did`` (or a ``_atproto.<handle>``
TXT record). So unlike the ActivityPub bridge, where every local user lives
under one hostname, each local user here needs a hostname of their own
directly under the site's domain, and usernames end up sharing a namespace
with the site's real subdomains.

This module is the synchronous, pure half of deciding who may hold which
name: syntax, and the reserved-name rules. The stateful half (is it already
claimed, does the hostname actually route to this bridge) lives in
``bridge.handle_provisioning``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from bridge.atproto.reserved_handles import RESERVED_HANDLE_LABELS

# RFC 1035 limits, which the ATProto handle spec inherits.
MAX_LABEL_LENGTH = 63
MAX_HANDLE_LENGTH = 253

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_NON_LABEL_CHARS_RE = re.compile(r"[^a-z0-9]+")


class RejectionKind(str, Enum):
    # Can never be a handle label (syntax, length, not a local user).
    INVALID = "invalid"
    # Syntactically fine, but held back by the built-in list, the
    # operator's own list, or because the bridge itself lives there.
    RESERVED = "reserved"
    # Another Matrix user already holds it.
    TAKEN = "taken"
    # The hostname doesn't reach this bridge: something else on the site
    # already answers for it, or the wildcard DNS/TLS isn't set up.
    NOT_ROUTED = "not_routed"


@dataclass(frozen=True)
class Rejection:
    kind: RejectionKind
    message: str


def label_for_localpart(localpart: str) -> str:
    """Default handle label for a Matrix localpart.

    Matrix localparts allow ``_ . = / +`` on top of what a DNS label does,
    so every run of characters a label can't hold becomes a single hyphen
    (``john_smith`` -> ``john-smith``). This is lossy on purpose -- distinct
    localparts can map to the same label, and whoever claims it first keeps
    it (see ``bridge.handle_provisioning``). A dot in particular must not
    survive: ``a.b.example.org`` is a second-level name that a
    ``*.example.org`` DNS record and certificate don't cover.

    May return a string that still isn't a valid label (empty, too long);
    ``HandlePolicy.check`` is what decides that.
    """
    return _NON_LABEL_CHARS_RE.sub("-", localpart.lower()).strip("-")


class HandlePolicy:
    """The reserved-name rules for one deployment.

    Three sources, checked in this order:

    1. ``infrastructure`` -- labels the bridge's own hostnames occupy
       (derived from ``bridge.public_base_url``), never overridable.
    2. ``reserved`` -- the operator's list (``handles.reserved``): exact
       labels or shell-style globs (``ns*``) for subdomains this site
       really uses or wants kept free.
    3. The built-in ``RESERVED_HANDLE_LABELS``, minus anything in
       ``allowed`` (``handles.allowed``).

    2 and 3 are matched against the label both as-is and with hyphens
    removed, so ``web-mail`` can't stand in for ``webmail``.
    """

    def __init__(
        self,
        domain: str,
        *,
        reserved: list[str] | None = None,
        allowed: list[str] | None = None,
        infrastructure: list[str] | None = None,
    ) -> None:
        self.domain = domain.lower().rstrip(".")
        self._reserved_patterns = tuple(p.lower() for p in (reserved or []))
        self._allowed = frozenset(a.lower() for a in (allowed or []))
        self._infrastructure = frozenset(i.lower() for i in (infrastructure or []))

    def handle_for_label(self, label: str) -> str:
        return f"{label}.{self.domain}"

    def label_for_host(self, host: str) -> str | None:
        """The label a request's ``Host`` header names, or ``None`` if the
        host isn't exactly one level under ``domain``."""
        # Drop any ":port"; a bracketed IPv6 literal is never a handle.
        host = host.lower().split(":", 1)[0].rstrip(".")
        suffix = f".{self.domain}"
        if not host.endswith(suffix):
            return None
        label = host[: -len(suffix)]
        if not label or "." in label:
            return None
        return label

    def check(self, label: str) -> Rejection | None:
        """Why ``label`` may not be claimed, or ``None`` if nothing here
        objects to it."""
        if not label:
            return Rejection(RejectionKind.INVALID, "The username has no letters or digits to build a handle from.")
        if len(label) > MAX_LABEL_LENGTH or len(self.handle_for_label(label)) > MAX_HANDLE_LENGTH:
            return Rejection(
                RejectionKind.INVALID,
                f"'{label}' is too long for a handle (at most {MAX_LABEL_LENGTH} characters).",
            )
        if not _LABEL_RE.match(label):
            return Rejection(
                RejectionKind.INVALID,
                f"'{label}' is not a valid handle: only a-z, 0-9 and hyphens, not starting or ending with a hyphen.",
            )
        # "xn--" is punycode (a handle that renders as arbitrary Unicode,
        # i.e. lookalikes of other handles); any other "??--" prefix is
        # reserved by IDNA for future encodings of the same kind.
        if label[2:4] == "--":
            return Rejection(RejectionKind.RESERVED, f"'{label}' uses a reserved internationalized-name prefix.")

        if label in self._infrastructure:
            return Rejection(RejectionKind.RESERVED, f"'{label}' is used by the bridge itself.")

        forms = {label, label.replace("-", "")}
        for form in forms:
            if any(fnmatch.fnmatchcase(form, pattern) for pattern in self._reserved_patterns):
                return Rejection(RejectionKind.RESERVED, f"'{label}' is reserved on this server.")
        if label not in self._allowed and not forms.isdisjoint(RESERVED_HANDLE_LABELS):
            return Rejection(RejectionKind.RESERVED, f"'{label}' is a reserved name.")
        return None


def infrastructure_labels(domain: str, *urls: str) -> list[str]:
    """Labels directly under ``domain`` that the given URLs' hostnames
    occupy -- e.g. ``https://pds.example.org`` occupies ``pds``, and so
    does ``https://a.pds.example.org``."""
    suffix = f".{domain.lower()}"
    labels = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host.endswith(suffix):
            labels.append(host[: -len(suffix)].rsplit(".", 1)[-1])
    return labels
