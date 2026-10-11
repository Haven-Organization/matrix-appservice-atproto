"""FastAPI application factory.

Wires together config, the durable ``BridgeRepository`` (sqlite-backed),
the shared ``httpx.AsyncClient``, the handle policy/routing prober, and the
Synapse client, and the PDS (``bridge.pds.Pds``) with its firehose -- then
mounts the handle resolution routes (``bridge.atproto.routes``), the PDS's
XRPC API (``bridge.atproto.xrpc``) and the AppService transaction receiver
(``bridge.appservice_routes``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from bridge.atproto.handle_probe import HandleRoutingProber, ProbeResult
from bridge.atproto.handles import HandlePolicy, infrastructure_labels
from bridge.admin_routes import router as admin_router
from bridge.appservice_routes import router as appservice_router
from bridge.atproto.firehose import Firehose
from bridge.atproto.routes import router as atproto_router
from bridge.atproto.xrpc import pds_error_handler, router as xrpc_router
from bridge.config import BridgeConfig
from bridge.commands import bot_mxid
from bridge.handle_provisioning import audit_handle_claims
from bridge.bridge_info import refresh_all_bridge_info
from bridge.chat import chat_loop, ensure_declaration
from bridge.jetstream import JetstreamConsumer, catch_up_loop
from bridge.matrix_records import register_matrix_sources
from bridge.notification_poller import notification_loop
from bridge.pds import Pds, PdsError
from bridge.record_sources import RecordSources
from bridge.repository import BridgeRepository
from bridge.sqlite_repository import SqliteBridgeRepository
from bridge.synapse_client import SynapseClient, SynapseError

logger = logging.getLogger(__name__)

# The self-check below goes out over the public path and back in to this
# same process, so it can only succeed once uvicorn is actually accepting
# connections -- which is after lifespan startup returns.
_WILDCARD_SELF_CHECK_DELAY = 3.0


# systemd ordering only guarantees Synapse's process has started, not that
# it is accepting connections yet (a cold start can take a while), so wait
# for it rather than crash on the first request.
_SYNAPSE_READY_TIMEOUT = 60.0
_SYNAPSE_READY_POLL_INTERVAL = 2.0


async def _wait_for_synapse_ready(synapse: SynapseClient, base_url: str) -> None:
    deadline = time.monotonic() + _SYNAPSE_READY_TIMEOUT
    last_exc: Exception | None = None
    logged_wait = False
    while time.monotonic() < deadline:
        try:
            await synapse.get_versions()
            return
        except (httpx.HTTPError, SynapseError) as exc:
            last_exc = exc
            if not logged_wait:
                logger.info("Waiting for Synapse to become reachable at %s...", base_url)
                logged_wait = True
            await asyncio.sleep(_SYNAPSE_READY_POLL_INTERVAL)
    raise RuntimeError(f"Synapse never became reachable at {base_url} after {_SYNAPSE_READY_TIMEOUT:.0f}s") from last_exc


async def _ensure_bot_user(synapse: SynapseClient, config: BridgeConfig) -> None:
    """Register the bot's Matrix account (a no-op once it exists) and apply
    its configured profile -- only where that differs, since every profile
    write makes Synapse rewrite the bot's membership in each of its rooms."""
    try:
        await synapse.register_appservice_user(config.appservice.bot_localpart)
    except SynapseError as exc:
        if exc.errcode != "M_USER_IN_USE":
            # Most likely the registration file isn't loaded by Synapse yet.
            logger.error(
                "Could not register the bot user @%s: %s. Is the AppService registration listed in "
                "homeserver.yaml's app_service_config_files, and has Synapse been restarted since?",
                config.appservice.bot_localpart,
                exc,
            )
            return
    mxid = bot_mxid(config)
    try:
        profile = await synapse.get_profile(mxid)
    except SynapseError:
        profile = {}
    try:
        name, avatar = config.appservice.bot_display_name, config.appservice.bot_avatar_mxc
        if name and profile.get("displayname") != name:
            await synapse.set_display_name(mxid, name)
        if avatar and profile.get("avatar_url") != avatar:
            await synapse.set_avatar_url(mxid, avatar)
    except SynapseError:
        logger.warning("Could not update the bot's profile", exc_info=True)


def build_handle_policy(config: BridgeConfig) -> HandlePolicy:
    return HandlePolicy(
        config.bridge.domain,
        reserved=config.handles.reserved,
        allowed=config.handles.allowed,
        infrastructure=infrastructure_labels(config.bridge.domain, config.bridge.public_base_url),
    )


async def _announce_to_relays(pds: Pds, repository: BridgeRepository) -> None:
    """Once the server is accepting connections, tell the relays this PDS
    is (back) up so they resubscribe -- pointless before any account exists."""
    await asyncio.sleep(_WILDCARD_SELF_CHECK_DELAY)
    if any(account.plc_registered for account in await repository.list_accounts()):
        pds.nudge_relays()


async def _ensure_chat_declarations(app: FastAPI) -> None:
    """Every account needs its chat declaration published to receive
    chats; accounts made before chat existed (or before the setting was
    changed) get theirs here."""
    if app.state.config.atproto.chat_poll_seconds <= 0:
        return
    for account in await app.state.repository.list_accounts():
        try:
            await ensure_declaration(app, account)
        except Exception:
            logger.warning("Could not publish chat declaration for %s", account.did, exc_info=True)


async def _wildcard_self_check(prober: HandleRoutingProber, domain: str) -> None:
    """Tell the operator at startup whether ``*.{domain}`` is set up, rather
    than leaving them to find out when the first user is refused a handle."""
    await asyncio.sleep(_WILDCARD_SELF_CHECK_DELAY)
    label = f"probe-{secrets.token_hex(6)}"
    result = await prober.probe(label)
    if result is ProbeResult.ROUTED:
        logger.info("Wildcard handle routing for *.%s reaches this bridge", domain)
    else:
        logger.warning(
            "Wildcard handle routing check failed (%s.%s: %s). No handle can be granted until a "
            "*.%s DNS record and TLS certificate route to this bridge with the Host header intact "
            "-- see deploy/nginx.conf.example.",
            label,
            domain,
            result.value,
            domain,
        )


def create_app(
    config: BridgeConfig, *, repository: BridgeRepository | None = None, synapse: SynapseClient | None = None
) -> FastAPI:
    """Build the FastAPI app.

    ``repository`` defaults to ``SqliteBridgeRepository`` persisted at
    ``{storage.data_dir}/bridge.db``. Pass an alternative implementation
    (e.g. ``SqliteBridgeRepository(":memory:")``) for tests, and likewise a
    ``synapse`` client pointed at a stand-in homeserver.
    """
    owns_repository = repository is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config
        app.state.repository = repository or SqliteBridgeRepository(Path(config.storage.data_dir) / "bridge.db")

        http_client = httpx.AsyncClient(timeout=15.0)
        app.state.http_client = http_client

        app.state.handle_policy = build_handle_policy(config)
        app.state.handle_prober = (
            HandleRoutingProber(http_client, config.bridge.domain) if config.handles.verify_routing else None
        )

        app.state.synapse = synapse or SynapseClient(
            config.synapse.base_url, as_token=config.appservice.as_token, admin_token=config.synapse.admin_token
        )
        await _wait_for_synapse_ready(app.state.synapse, config.synapse.base_url)
        await _ensure_bot_user(app.state.synapse, config)

        app.state.firehose = Firehose(app.state.repository)
        app.state.record_sources = RecordSources()
        register_matrix_sources(app.state.record_sources, app.state.synapse, bot_mxid=bot_mxid(config))
        app.state.pds = Pds(
            repository=app.state.repository,
            firehose=app.state.firehose,
            sources=app.state.record_sources,
            handle_policy=app.state.handle_policy,
            handle_prober=app.state.handle_prober,
            http_client=http_client,
            server_name=config.synapse.server_name,
            public_base_url=config.bridge.public_base_url,
            plc_directory=config.atproto.plc_directory,
            relays=config.atproto.relays,
        )
        for account, endpoint in await app.state.pds.accounts_on_other_endpoint():
            logger.warning(
                "%s (%s) was registered with PDS endpoint %s, not the configured bridge.public_base_url %s. "
                "The network will keep looking for it at the old address until its identity is updated.",
                account.did,
                account.matrix_user_id,
                endpoint,
                config.bridge.public_base_url,
            )

        for claim, rejection in await audit_handle_claims(app.state.repository, app.state.handle_policy):
            logger.warning(
                "Handle %s held by %s is no longer allowed by the current handle policy: %s",
                app.state.handle_policy.handle_for_label(claim.label),
                claim.matrix_user_id,
                rejection.message,
            )

        self_check_task = (
            asyncio.create_task(_wildcard_self_check(app.state.handle_prober, config.bridge.domain))
            if app.state.handle_prober is not None
            else None
        )
        relay_task = asyncio.create_task(_announce_to_relays(app.state.pds, app.state.repository))
        app.state.background_tasks = set()
        app.state.jetstream = JetstreamConsumer(app, config.atproto.jetstream_url)
        jetstream_task = asyncio.create_task(app.state.jetstream.run())
        notifications_task = asyncio.create_task(notification_loop(app))
        catch_up_task = asyncio.create_task(catch_up_loop(app))
        bridge_info_task = asyncio.create_task(refresh_all_bridge_info(app))
        chat_task = asyncio.create_task(chat_loop(app))
        declarations_task = asyncio.create_task(_ensure_chat_declarations(app))
        tasks = (
            self_check_task, relay_task, jetstream_task, notifications_task, catch_up_task, bridge_info_task,
            chat_task, declarations_task,
        )
        try:
            yield
        finally:
            for task in (*tasks, *app.state.background_tasks):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            await http_client.aclose()
            await app.state.synapse.aclose()
            if owns_repository:
                await app.state.repository.close()

    app = FastAPI(
        title="matrix-appservice-atproto",
        description="Native Matrix <-> ATProto bridge",
        lifespan=lifespan,
    )
    app.include_router(atproto_router)
    app.include_router(xrpc_router)
    app.include_router(admin_router)
    app.include_router(appservice_router)
    app.add_exception_handler(PdsError, pds_error_handler)
    return app
