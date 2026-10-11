"""Repository commits: the signed root of one account's data.

A commit names the account, the MST root covering all of its records, and
a revision that only ever increases, and is signed with the account's
signing key (the one its DID document publishes). Anyone holding the
commit, the tree nodes and the records can check the whole repository
against that one signature.
"""

from __future__ import annotations

from typing import Any

from bridge.atproto import cbor
from bridge.atproto.cid import CID
from bridge.atproto.keys import SigningKey, verify_signature
from bridge.atproto.mst import build_mst

REPO_VERSION = 3


def sign_commit(key: SigningKey, *, did: str, data: CID, rev: str) -> tuple[CID, bytes]:
    """Create and sign a commit; returns its CID and block."""
    # "prev" is a leftover of repo format v2. v3 requires the field to be
    # present and (for anything written fresh) null.
    commit: dict[str, Any] = {"did": did, "version": REPO_VERSION, "data": data, "rev": rev, "prev": None}
    commit["sig"] = key.sign(cbor.encode(commit))
    block = cbor.encode(commit)
    return CID.for_cbor(block), block


def verify_commit(block: bytes, did_key: str) -> bool:
    commit = cbor.decode(block)
    signature = commit.pop("sig", None)
    return isinstance(signature, bytes) and verify_signature(did_key, cbor.encode(commit), signature)


def commit_records(
    key: SigningKey, *, did: str, records: dict[str, CID], rev: str
) -> tuple[CID, dict[CID, bytes]]:
    """Build the tree over ``records`` (path -> record CID) and sign a
    commit for it. Returns the commit CID and every block created -- tree
    nodes plus the commit, but not the records themselves."""
    root, blocks = build_mst(records)
    commit_cid, commit_block = sign_commit(key, did=did, data=root, rev=rev)
    blocks[commit_cid] = commit_block
    return commit_cid, blocks
