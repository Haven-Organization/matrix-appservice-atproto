"""The PDS: local users' ATProto accounts and repositories.

Ties the protocol pieces in ``bridge.atproto`` to storage. Everything that
creates an account or changes a repository goes through here, which is
what keeps three things in step for every write: the record index in the
database, the signed commit over it, and the event the network sees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from bridge.atproto import cbor
from bridge.atproto.car import write_car
from bridge.atproto.cid import CID
from bridge.atproto.firehose import Firehose
from bridge.atproto.handle_probe import HandleRoutingProber
from bridge.atproto.handles import HandlePolicy
from bridge.atproto.keys import SigningKey
from bridge.atproto.mst import build_mst, search_path
from bridge.atproto.plc import build_operation, build_tombstone, did_for_genesis, sign_operation
from bridge.atproto.repo import sign_commit
from bridge.atproto.tid import TidClock
from bridge.handle_provisioning import claim_handle_for_user
from bridge.record_sources import RecordSources, RecordUnavailable, inline_source
from bridge.repository import Account, BridgeRepository, RecordRow

logger = logging.getLogger(__name__)

_NSID_RE = re.compile(r"^[a-zA-Z](?:[a-zA-Z0-9-]{0,62})?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,62})?){2,}$")
_RKEY_RE = re.compile(r"^[A-Za-z0-9._:~-]{1,512}$")

_PLC_ROTATION_KEY = "plc_rotation"

# At most one unprompted crawl request per this many seconds.
_CRAWL_REQUEST_INTERVAL = 1200.0  # as the reference PDS (20 minutes)


class PdsError(Exception):
    """A failure with an XRPC error name (``RepoNotFound``, ...), so the
    API layer can report it the way ATProto clients expect."""

    def __init__(self, error: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Write:
    """One change to a repository.

    ``record`` (a data-model value) is required for create/update.
    ``source`` is the recipe for rebuilding it later (see
    ``bridge.record_sources``). Left unset, the record itself is stored
    inline -- correct only for records with no Matrix content behind them
    (follows, likes); anything derived from a Matrix event must pass the
    Matrix-backed source instead, or its content ends up in the database.
    """

    action: str  # "create" | "update" | "delete"
    collection: str
    rkey: str
    record: Any = None
    source: dict[str, Any] | None = None
    matrix_event_id: str | None = None


class Pds:
    def __init__(
        self,
        *,
        repository: BridgeRepository,
        firehose: Firehose,
        sources: RecordSources,
        handle_policy: HandlePolicy,
        handle_prober: HandleRoutingProber | None,
        http_client: httpx.AsyncClient,
        server_name: str,
        public_base_url: str,
        plc_directory: str,
        relays: list[str],
    ) -> None:
        self._repository = repository
        self._firehose = firehose
        self._sources = sources
        self._handle_policy = handle_policy
        self._handle_prober = handle_prober
        self._http_client = http_client
        self._server_name = server_name
        self._public_base_url = public_base_url
        self._plc_directory = plc_directory.rstrip("/")
        self._relays = relays
        self._clock = TidClock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_crawl_request = 0.0
        self._crawl_task: asyncio.Task | None = None

    def _lock_for(self, did: str) -> asyncio.Lock:
        """Serializes writes to one repository: a commit is built from the
        current head, so two built concurrently would both claim to follow it."""
        return self._locks.setdefault(did, asyncio.Lock())

    def handle_of(self, account: Account) -> str:
        return self._handle_policy.handle_for_label(account.label)

    def next_record_key(self) -> str:
        """A fresh timestamp-ordered record key."""
        return self._clock.next()

    async def request_crawl(self) -> dict[str, str]:
        """Ask each configured relay to subscribe to this PDS's firehose.
        Needed once for a relay to learn the PDS exists; harmless to repeat.
        Returns each relay's answer, for the operator to read."""
        hostname = urlsplit(self._public_base_url).hostname
        results: dict[str, str] = {}
        for relay in self._relays:
            try:
                response = await self._http_client.post(
                    f"{relay}/xrpc/com.atproto.sync.requestCrawl", json={"hostname": hostname}
                )
                results[relay] = f"HTTP {response.status_code} {response.text[:200]}".strip()
            except httpx.HTTPError as exc:
                results[relay] = f"failed: {exc!r}"
        return results

    def nudge_relays(self) -> None:
        """Ask the relays to (re)connect -- called at startup and after each
        commit, as the reference PDS does. A relay that couldn't reach this
        PDS for a while (a reboot, say) marks it offline and stops trying
        until asked again, so without this, posts would sit unseen. Asked
        whoever else is subscribed: other consumers of the firehose say
        nothing about whether the relays are. Harmless to a relay already
        connected. Rate-limited, and run in the background so a slow relay
        never delays a write."""
        if not self._relays:
            return
        now = time.monotonic()
        if now - self._last_crawl_request < _CRAWL_REQUEST_INTERVAL:
            return
        self._last_crawl_request = now

        async def run() -> None:
            for relay, result in (await self.request_crawl()).items():
                logger.info("Requested crawl from %s: %s", relay, result)

        self._crawl_task = asyncio.create_task(run())

    async def accounts_on_other_endpoint(self) -> list[tuple[Account, str]]:
        """Accounts whose identity was registered pointing at a PDS URL
        other than the configured one, with that URL. Their DID documents
        still send the network to the old address."""
        mismatched = []
        for account in await self._repository.list_accounts():
            endpoint = json.loads(account.genesis_operation)["services"]["atproto_pds"]["endpoint"]
            if endpoint != self._public_base_url:
                mismatched.append((account, endpoint))
        return mismatched

    # -- accounts -----------------------------------------------------------

    async def _rotation_key(self) -> SigningKey:
        """The bridge-wide key that controls every local account's did:plc
        (it signs the operations that change a DID's handle, keys or PDS)."""
        private_hex = await self._repository.get_or_create_server_key(
            _PLC_ROTATION_KEY, SigningKey.generate().to_hex()
        )
        return SigningKey.from_hex(private_hex)

    async def create_account(self, matrix_user_id: str, *, register: bool = True) -> Account:
        """Give a local Matrix user an ATProto account, or return the one
        they have. Raises ``HandleRejected`` if they can't have a handle.

        ``register=True`` publishes the new DID to the PLC directory, which
        is public and permanent; ``False`` creates a fully working local
        account that the rest of the network can't resolve (tests, dry runs).

        Safe to call again after a failure at any point: the keys and DID
        are stored before anything is published, and publishing the same
        operation twice is harmless.
        """
        account = await self._repository.get_account_for_user(matrix_user_id)
        if account is None:
            claim = await claim_handle_for_user(
                self._repository,
                self._handle_policy,
                self._handle_prober,
                matrix_user_id=matrix_user_id,
                server_name=self._server_name,
            )
            rotation_key = await self._rotation_key()
            signing_key = SigningKey.generate()
            operation = sign_operation(
                build_operation(
                    rotation_keys=[rotation_key.did_key()],
                    signing_key=signing_key.did_key(),
                    handle=self._handle_policy.handle_for_label(claim.label),
                    pds_endpoint=self._public_base_url,
                ),
                rotation_key,
            )
            did = did_for_genesis(operation)
            root, _ = build_mst({})
            rev = self._clock.next()
            commit_cid, commit_block = sign_commit(signing_key, did=did, data=root, rev=rev)
            account = Account(
                did=did,
                matrix_user_id=matrix_user_id,
                label=claim.label,
                signing_key_hex=signing_key.to_hex(),
                genesis_operation=json.dumps(operation),
                plc_registered=False,
                active=True,
                rev=rev,
                commit_cid=str(commit_cid),
                commit_block=commit_block,
                data_cid=str(root),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            await self._repository.create_account(account)
            logger.info("Created ATProto account %s for %s", did, matrix_user_id)

        if register and not account.plc_registered:
            await self._register_did(account)
            account = replace(account, plc_registered=True)

        claim = await self._repository.get_handle_claim(account.label)
        if claim is not None and claim.did is None and (account.plc_registered or not register):
            await self._announce(account)
        return account

    async def _register_did(self, account: Account) -> None:
        try:
            response = await self._http_client.post(
                f"{self._plc_directory}/{account.did}", json=json.loads(account.genesis_operation)
            )
        except httpx.HTTPError as exc:
            raise PdsError("PlcRegistrationFailed", f"Could not reach the PLC directory: {exc!r}") from exc
        if response.status_code != 200:
            raise PdsError(
                "PlcRegistrationFailed",
                f"PLC directory refused {account.did}: HTTP {response.status_code} {response.text[:200]}",
            )
        await self._repository.mark_plc_registered(account.did)
        logger.info("Registered %s with %s", account.did, self._plc_directory)

    async def _announce(self, account: Account) -> None:
        """Make a new account resolvable and tell the network it exists.
        Setting the claim's DID is what turns on its
        ``/.well-known/atproto-did`` answer, and marks this step done."""
        await self._repository.set_handle_did(account.label, account.did)
        root, nodes = build_mst({})
        commit = CID.from_str(account.commit_cid)
        await self._firehose.emit_identity(account.did, self.handle_of(account))
        await self._firehose.emit_account(account.did, active=True)
        await self._firehose.emit_commit(
            did=account.did,
            commit=commit,
            rev=account.rev,
            since=None,
            prev_data=None,
            ops=[],
            blocks=[(commit, account.commit_block), *nodes.items()],
        )

    async def delete_account(self, did: str) -> None:
        """Permanently delete a local account: tell the network it is
        gone, retire its DID, and erase everything kept about it. There is
        no undo -- the signing key is destroyed with the rest."""
        async with self._lock_for(did):
            account = await self._repository.get_account(did)
            if account is None:
                return
            await self._firehose.emit_account(did, active=False, status="deleted")
            if account.plc_registered:
                # Retire the DID itself, so it stops resolving to this PDS
                # (and to a handle someone else may later be given).
                # Best-effort: the account is deleted here regardless.
                try:
                    rotation_key = await self._rotation_key()
                    tombstone = sign_operation(build_tombstone(json.loads(account.genesis_operation)), rotation_key)
                    response = await self._http_client.post(f"{self._plc_directory}/{did}", json=tombstone)
                    if response.status_code != 200:
                        logger.warning("PLC directory refused tombstone for %s: %s", did, response.text[:200])
                except httpx.HTTPError:
                    logger.warning("Could not retire %s at the PLC directory", did, exc_info=True)
            await self._repository.delete_account(did)
            logger.info("Deleted account %s (%s)", did, account.matrix_user_id)

    async def resolve_repo(self, identifier: str) -> Account:
        """The account for a DID or a handle, as XRPC ``repo``/``did``
        parameters allow either."""
        if identifier.startswith("did:"):
            account = await self._repository.get_account(identifier)
        else:
            label = self._handle_policy.label_for_host(identifier)
            account = await self._repository.get_account_for_label(label) if label else None
        if account is None:
            raise PdsError("RepoNotFound", f"Could not find repo: {identifier}")
        return account

    # -- writes -------------------------------------------------------------

    async def apply_writes(self, did: str, writes: list[Write]) -> CID:
        """Apply ``writes`` as one commit and publish it. Returns the new
        commit's CID."""
        async with self._lock_for(did):
            account = await self._repository.get_account(did)
            if account is None:
                raise PdsError("RepoNotFound", f"Could not find repo: {did}")
            return await self._apply_locked(account, writes)

    async def _apply_locked(self, account: Account, writes: list[Write]) -> CID:
        did = account.did
        old = {row.path: CID.from_str(row.cid) for row in await self._repository.list_records(did)}
        new = dict(old)
        ops: list[dict[str, Any]] = []
        record_blocks: dict[CID, bytes] = {}
        upserts: list[RecordRow] = []
        deletes: list[tuple[str, str]] = []

        for write in writes:
            if not _NSID_RE.match(write.collection) or not _RKEY_RE.match(write.rkey) or write.rkey in (".", ".."):
                raise PdsError("InvalidRequest", f"Invalid record path {write.collection}/{write.rkey}")
            path = f"{write.collection}/{write.rkey}"
            if write.action == "delete":
                if path not in new:
                    raise PdsError("InvalidRequest", f"Record does not exist: {path}")
                ops.append({"action": "delete", "path": path, "cid": None, "prev": new.pop(path)})
                deletes.append((write.collection, write.rkey))
                continue
            if write.action not in ("create", "update") or (write.action == "create") == (path in new):
                raise PdsError("InvalidRequest", f"Cannot {write.action} {path}")
            if not isinstance(write.record, dict) or write.record.get("$type") != write.collection:
                raise PdsError("InvalidRequest", f"Record for {path} must have $type {write.collection}")
            block = cbor.encode(write.record)
            cid = CID.for_cbor(block)
            op: dict[str, Any] = {"action": write.action, "path": path, "cid": cid}
            if write.action == "update":
                op["prev"] = new[path]
            ops.append(op)
            new[path] = cid
            record_blocks[cid] = block
            source = write.source if write.source is not None else inline_source(write.record)
            upserts.append(
                RecordRow(did, write.collection, write.rkey, str(cid), json.dumps(source), write.matrix_event_id)
            )

        old_root, old_nodes = build_mst(old)
        new_root, new_nodes = build_mst(new)
        rev = self._clock.next(after=account.rev)
        commit_cid, commit_block = sign_commit(
            SigningKey.from_hex(account.signing_key_hex), did=did, data=new_root, rev=rev
        )

        # What a consumer needs to check this commit against the previous
        # one WITHOUT holding the repository: the commit, the new records,
        # every tree node that changed, and the nodes on the way to each
        # touched key (for a delete those may be old, unchanged nodes).
        event_blocks: dict[CID, bytes] = {commit_cid: commit_block}
        event_blocks.update(record_blocks)
        event_blocks.update((cid, block) for cid, block in new_nodes.items() if cid not in old_nodes)
        for op in ops:
            for cid in search_path(new_root, op["path"].encode("utf-8"), new_nodes.__getitem__):
                event_blocks[cid] = new_nodes[cid]

        await self._repository.commit_repo(
            did,
            rev=rev,
            commit_cid=str(commit_cid),
            commit_block=commit_block,
            data_cid=str(new_root),
            upserts=upserts,
            deletes=deletes,
        )
        await self._firehose.emit_commit(
            did=did,
            commit=commit_cid,
            rev=rev,
            since=account.rev,
            prev_data=old_root,
            ops=ops,
            blocks=list(event_blocks.items()),
        )
        self.nudge_relays()
        return commit_cid

    # -- reads --------------------------------------------------------------

    async def _rebuild(self, row: RecordRow) -> tuple[bytes | None, Write | None]:
        """A record's current block, plus the write needed to bring the
        repository in line with its source if they've diverged: a delete
        if the source is gone (block is then ``None``), an update if it now
        produces different bytes."""
        try:
            rebuilt = await self._sources.rebuild(row)
        except RecordUnavailable as exc:
            logger.warning("%s", exc)
            raise PdsError("UpstreamFailure", "Record content is temporarily unavailable", status=502) from exc
        if rebuilt.block is None:
            return None, Write("delete", row.collection, row.rkey)
        if rebuilt.matches:
            return rebuilt.block, None
        return rebuilt.block, Write(
            "update",
            row.collection,
            row.rkey,
            cbor.decode(rebuilt.block),
            source=json.loads(row.source),
            matrix_event_id=row.matrix_event_id,
        )

    async def _snapshot_locked(self, did: str) -> tuple[Account, list[RecordRow], dict[CID, bytes]]:
        """The repository's head, index and every record block, rebuilt
        from their sources. If any record has diverged from its source
        (redacted on Matrix, or changed), the repository is corrected with
        a new commit first and the snapshot retaken, so what is returned
        always matches its own signed commit."""
        while True:
            account = await self._repository.get_account(did)
            if account is None:
                raise PdsError("RepoNotFound", f"Could not find repo: {did}")
            rows = await self._repository.list_records(did)
            blocks: dict[CID, bytes] = {}
            corrections: list[Write] = []
            for row in rows:
                block, correction = await self._rebuild(row)
                if correction is not None:
                    corrections.append(correction)
                elif block is not None:
                    blocks[CID.from_str(row.cid)] = block
            if not corrections:
                return account, rows, blocks
            logger.info("Correcting %d record(s) of %s that diverged from their source", len(corrections), did)
            await self._apply_locked(account, corrections)

    async def repo_car(self, did: str) -> bytes:
        """The whole repository as a CAR file (``com.atproto.sync.getRepo``)."""
        async with self._lock_for(did):
            account, rows, record_blocks = await self._snapshot_locked(did)
        _, nodes = build_mst({row.path: CID.from_str(row.cid) for row in rows})
        commit = CID.from_str(account.commit_cid)
        return write_car(commit, [(commit, account.commit_block), *nodes.items(), *record_blocks.items()])

    async def blocks_car(self, did: str, cids: list[CID]) -> bytes:
        """The requested blocks of the repository -- commit, tree nodes or
        records -- as a CAR (``com.atproto.sync.getBlocks``). Raises
        ``BlockNotFound`` if any isn't part of the current repository."""
        async with self._lock_for(did):
            account, rows, blocks = await self._snapshot_locked(did)
        _, nodes = build_mst({row.path: CID.from_str(row.cid) for row in rows})
        blocks.update(nodes)
        blocks[CID.from_str(account.commit_cid)] = account.commit_block
        missing = [cid for cid in cids if cid not in blocks]
        if missing:
            raise PdsError("BlockNotFound", f"Could not find cids: {', '.join(str(cid) for cid in missing)}")
        return write_car(None, [(cid, blocks[cid]) for cid in cids])

    async def _get_record_locked(self, did: str, collection: str, rkey: str) -> tuple[RecordRow, bytes] | None:
        row = await self._repository.get_record(did, collection, rkey)
        if row is None:
            return None
        block, correction = await self._rebuild(row)
        if correction is not None:
            account = await self._repository.get_account(did)
            if account is not None:
                await self._apply_locked(account, [correction])
            row = await self._repository.get_record(did, collection, rkey)
        if block is None or row is None:
            return None
        return row, block

    async def put_record(
        self,
        did: str,
        collection: str,
        rkey: str,
        record: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
        matrix_event_id: str | None = None,
    ) -> CID | None:
        """Create the record, or update it if it exists. Returns the new
        commit's CID, or ``None`` if the record already had exactly this
        content (no commit is made for a no-op)."""
        async with self._lock_for(did):
            account = await self._repository.get_account(did)
            if account is None:
                raise PdsError("RepoNotFound", f"Could not find repo: {did}")
            row = await self._repository.get_record(did, collection, rkey)
            if row is not None and row.cid == str(CID.for_cbor(cbor.encode(record))):
                return None
            action = "update" if row is not None else "create"
            write = Write(action, collection, rkey, record, source=source, matrix_event_id=matrix_event_id)
            return await self._apply_locked(account, [write])

    async def get_record(self, did: str, collection: str, rkey: str) -> tuple[RecordRow, bytes] | None:
        """A record's index row and block, or ``None`` if it doesn't exist
        (including: its source is gone, in which case it is deleted)."""
        async with self._lock_for(did):
            return await self._get_record_locked(did, collection, rkey)

    async def record_proof_car(self, did: str, collection: str, rkey: str) -> bytes:
        """A CAR proving a record is (or isn't) in the repository: the
        commit, the tree nodes on the way to its key, and the record if
        present (``com.atproto.sync.getRecord``)."""
        async with self._lock_for(did):
            found = await self._get_record_locked(did, collection, rkey)
            account = await self._repository.get_account(did)
            if account is None:
                raise PdsError("RepoNotFound", f"Could not find repo: {did}")
            rows = await self._repository.list_records(did)
        root, nodes = build_mst({row.path: CID.from_str(row.cid) for row in rows})
        commit = CID.from_str(account.commit_cid)
        blocks = [(commit, account.commit_block)]
        key = f"{collection}/{rkey}".encode("utf-8")
        blocks.extend((cid, nodes[cid]) for cid in search_path(root, key, nodes.__getitem__))
        if found is not None:
            blocks.append((CID.from_str(found[0].cid), found[1]))
        return write_car(commit, blocks)

    def did_document(self, account: Account) -> dict[str, Any]:
        """The account's DID document, as the PLC directory renders it from
        the operation this bridge signed."""
        key = SigningKey.from_hex(account.signing_key_hex)
        return {
            "@context": [
                "https://www.w3.org/ns/did/v1",
                "https://w3id.org/security/multikey/v1",
                "https://w3id.org/security/suites/secp256k1-2019/v1",
            ],
            "id": account.did,
            "alsoKnownAs": [f"at://{self.handle_of(account)}"],
            "verificationMethod": [
                {
                    "id": f"{account.did}#atproto",
                    "type": "Multikey",
                    "controller": account.did,
                    "publicKeyMultibase": key.multibase(),
                }
            ],
            "service": [
                {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": self._public_base_url}
            ],
        }
