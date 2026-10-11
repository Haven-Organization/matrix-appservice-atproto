"""Who counts as a bridge admin.

Admins may do the things that reach beyond their own identity: choose how
much a backfill fetches from someone else's server, and replace a room
that represents someone else. Listed in ``bridge.admins``; a Synapse
server admin counts too when ``synapse.admin_token`` lets the bridge ask.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def is_admin(app: FastAPI, matrix_user_id: str) -> bool:
    if matrix_user_id in app.state.config.bridge.admins:
        return True
    try:
        return await app.state.synapse.is_server_admin(matrix_user_id)
    except (httpx.HTTPError, ValueError):
        logger.warning("Could not check admin status for %s", matrix_user_id, exc_info=True)
        return False
