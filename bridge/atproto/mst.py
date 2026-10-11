"""The Merkle Search Tree: the index of a repository's records.

A repository is a map from record path (``app.bsky.feed.post/3k...``) to
the CID of that record. The MST is how that map is stored as content-
addressed blocks so that one hash -- the root's CID, which the commit
signs -- covers every record.

Its shape is a pure function of the keys, not of insertion order: each
key's layer is the number of leading zero bits of its SHA-256, divided by
two (so each layer up holds about a quarter as many keys). A node holds
the keys of its own layer in sorted order, with a link to a lower subtree
in every gap between them. Any two implementations given the same records
therefore produce the same root CID, which is what lets the network verify
this bridge's repositories -- and what the live interop check verifies in
the other direction.

``build_mst`` rebuilds the whole tree from the full map. That is O(records)
per commit rather than the O(log n) an incremental update would be, which
is fine at the sizes a personal repository reaches and leaves no mutable
tree state to get out of sync.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Iterator

from bridge.atproto import cbor
from bridge.atproto.cid import CID


def key_layer(key: bytes) -> int:
    zeros = 0
    for byte in hashlib.sha256(key).digest():
        if byte == 0:
            zeros += 8
            continue
        zeros += 8 - byte.bit_length()
        break
    return zeros // 2


def _common_prefix_length(a: bytes, b: bytes) -> int:
    length = 0
    for x, y in zip(a, b):
        if x != y:
            break
        length += 1
    return length


def _build(items: list[tuple[bytes, int, CID]], layer: int, blocks: dict[CID, bytes]) -> CID | None:
    """Store the node covering ``items`` (sorted, all at ``layer`` or
    below) and return its CID; ``None`` for an empty range."""
    if not items:
        return None
    left: CID | None = None
    entries: list[dict] = []
    below: list[tuple[bytes, int, CID]] = []
    previous_key = b""
    for item in items:
        key, item_layer, value = item
        if item_layer != layer:
            below.append(item)
            continue
        subtree = _build(below, layer - 1, blocks)
        below = []
        if entries:
            entries[-1]["t"] = subtree
        else:
            left = subtree
        # Keys are stored with the prefix they share with the previous key
        # in the same node stripped off.
        shared = _common_prefix_length(previous_key, key)
        entries.append({"p": shared, "k": key[shared:], "v": value, "t": None})
        previous_key = key
    subtree = _build(below, layer - 1, blocks)
    if entries:
        entries[-1]["t"] = subtree
    else:
        # No key lives at this layer: a pass-through node holding only the
        # link down, so the tree's height still matches its highest key.
        left = subtree
    block = cbor.encode({"l": left, "e": entries})
    cid = CID.for_cbor(block)
    blocks[cid] = block
    return cid


def build_mst(records: dict[str, CID]) -> tuple[CID, dict[CID, bytes]]:
    """Build the tree for ``records`` (path -> record CID). Returns the root
    CID and every node block, keyed by CID."""
    blocks: dict[CID, bytes] = {}
    items = sorted((path.encode("utf-8"), cid) for path, cid in records.items())
    layered = [(key, key_layer(key), cid) for key, cid in items]
    if not layered:
        block = cbor.encode({"l": None, "e": []})
        root = CID.for_cbor(block)
        blocks[root] = block
        return root, blocks
    root = _build(layered, max(layer for _, layer, _ in layered), blocks)
    assert root is not None
    return root, blocks


def walk_mst(root: CID, get_block: Callable[[CID], bytes]) -> Iterator[tuple[str, CID]]:
    """Yield every ``(path, record CID)`` under ``root`` in key order."""
    node = cbor.decode(get_block(root))
    if node.get("l") is not None:
        yield from walk_mst(node["l"], get_block)
    key = b""
    for entry in node["e"]:
        key = key[: entry["p"]] + entry["k"]
        yield key.decode("utf-8"), entry["v"]
        if entry.get("t") is not None:
            yield from walk_mst(entry["t"], get_block)


def search_path(root: CID, key: bytes, get_block: Callable[[CID], bytes]) -> list[CID]:
    """The nodes visited looking ``key`` up from ``root``: down to the node
    holding it, or, if it's absent, all the way to the empty gap where it
    would go. Together these blocks prove whether the key is present, and
    are what someone needs in order to insert or remove it."""
    path: list[CID] = []
    cid: CID | None = root
    while cid is not None:
        path.append(cid)
        node = cbor.decode(get_block(cid))
        below = node.get("l")
        entry_key = b""
        for entry in node["e"]:
            entry_key = entry_key[: entry["p"]] + entry["k"]
            if entry_key == key:
                return path
            if entry_key > key:
                break
            below = entry.get("t")
        cid = below
    return path
