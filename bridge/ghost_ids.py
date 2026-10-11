"""Ghost user IDs: the Matrix users standing for remote ATProto accounts."""

from __future__ import annotations

import re

from fastapi import FastAPI


def ghost_user_id(app: FastAPI, did: str) -> str:
    """Keyed on the DID, not the handle: a handle can change or be taken
    over by someone else, and a Matrix user ID can never be renamed."""
    config = app.state.config
    method, _, identifier = did.removeprefix("did:").partition(":")
    name = identifier if method == "plc" else f"{method}_{identifier}"
    localpart = config.appservice.user_prefix + re.sub(r"[^a-z0-9._=-]", "_", name.lower())
    return f"@{localpart}:{config.synapse.server_name}"


def did_for_ghost(app: FastAPI, user_id: str) -> str | None:
    """The DID a ghost user ID stands for -- the reverse of
    ``ghost_user_id``. ``None`` if it isn't one of the bridge's ghosts."""
    config = app.state.config
    prefix = f"@{config.appservice.user_prefix}"
    localpart, _, server = user_id.partition(":")
    if not localpart.startswith(prefix) or server != config.synapse.server_name:
        return None
    name = localpart[len(prefix) :]
    if name.startswith("web_"):
        return f"did:web:{name[4:]}"
    return f"did:plc:{name}" if re.fullmatch(r"[a-z2-7]{24}", name) else None
