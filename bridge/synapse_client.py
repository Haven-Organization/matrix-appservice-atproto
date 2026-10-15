"""Async wrapper around the Synapse Client-Server API.

Every interaction this bridge has with Matrix goes through this client,
authenticated with the AppService's ``as_token`` and acting as the bot (or,
later, a ghost) via ``user_id=`` impersonation. It is a thin, typed
wrapper: it knows nothing about ATProto or Profile Rooms.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class SynapseError(Exception):
    """Raised when Synapse returns an error response."""

    def __init__(self, status_code: int, errcode: str | None, error: str | None) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.error = error
        super().__init__(f"Synapse error {status_code} {errcode}: {error}")


@dataclass(frozen=True)
class MediaDownload:
    content: bytes
    content_type: str
    # As the media repository reports it, including the uploader's filename.
    content_disposition: str | None = None


class SynapseClient:
    def __init__(
        self, base_url: str, *, as_token: str, admin_token: str | None = None, timeout: float = 15.0
    ) -> None:
        self._as_token = as_token
        self._admin_token = admin_token
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        # Fetched once and cached -- see _is_pre_v12_default.
        self._default_room_version: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        as_user_id: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._as_token}"} if authenticated else {}
        request_params = dict(params or {})
        if as_user_id:
            request_params["user_id"] = as_user_id
        response = await self._client.request(method, path, headers=headers, params=request_params, json=json_body)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise SynapseError(response.status_code, body.get("errcode"), body.get("error"))
        return response.json() if response.content else {}

    async def get_versions(self) -> dict[str, Any]:
        """Public and side-effect-free: used as a startup readiness probe."""
        return await self._request("GET", "/_matrix/client/versions", authenticated=False)

    async def _is_pre_v12_default(self) -> bool:
        """Whether rooms this homeserver creates by default predate room
        version 12, which changed how a room's creator relates to its power
        levels (see ``create_room``). Unknown counts as pre-v12, the older
        and far more common behaviour."""
        if self._default_room_version is None:
            try:
                caps = await self._request("GET", "/_matrix/client/v3/capabilities")
                self._default_room_version = caps.get("capabilities", {}).get("m.room_versions", {}).get("default") or "1"
            except SynapseError:
                self._default_room_version = "1"
        try:
            return int(self._default_room_version) < 12
        except ValueError:
            return True

    async def register_appservice_user(self, localpart: str) -> str:
        """Provision a user in the AppService's namespace. Returns the full MXID."""
        body = await self._request(
            "POST",
            "/_matrix/client/v3/register",
            json_body={"type": "m.login.application_service", "username": localpart},
        )
        return body["user_id"]

    async def set_display_name(self, user_id: str, display_name: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{user_id}/displayname",
            json_body={"displayname": display_name},
            as_user_id=user_id,
        )

    async def set_avatar_url(self, user_id: str, mxc_uri: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{user_id}/avatar_url",
            json_body={"avatar_url": mxc_uri},
            as_user_id=user_id,
        )

    async def get_profile(self, user_id: str) -> dict[str, Any]:
        """Returns ``{"displayname": ..., "avatar_url": "mxc://..."}`` (keys may be absent)."""
        return await self._request("GET", f"/_matrix/client/v3/profile/{user_id}")

    async def create_room(
        self,
        *,
        as_user_id: str,
        name: str | None = None,
        topic: str | None = None,
        invite: list[str] | None = None,
        avatar_mxc: str | None = None,
        room_type: str | None = None,
        power_level_users: dict[str, int] | None = None,
        power_level_events: dict[str, int] | None = None,
        is_direct: bool = False,
        preset: str = "private_chat",
        predecessor: dict[str, str] | None = None,
        join_rule: str | None = None,
        history_visibility: str | None = None,
    ) -> str:
        """Create an invite-only room as ``as_user_id``. Returns the room ID.

        ``preset`` is ``private_chat`` or ``trusted_private_chat`` (the
        latter gives everyone invited at creation the creator's own level).

        ``join_rule`` (e.g. ``"knock"``) and ``history_visibility`` (e.g.
        ``"world_readable"``), if given, override the preset's.

        ``power_level_users``/``power_level_events`` are applied on top of
        the defaults. The creator must not be given a level in a v12+ room
        (the server rejects it: creators are implicitly above every level
        there) but MUST be listed in an older one (Synapse rejects a
        ``users`` override that omits them), so that is handled here.
        """
        body: dict[str, Any] = {"preset": preset, "is_direct": is_direct}
        if name:
            body["name"] = name
        if topic:
            body["topic"] = topic
        if invite:
            body["invite"] = invite
        initial_state: list[dict[str, Any]] = []
        if avatar_mxc:
            initial_state.append({"type": "m.room.avatar", "state_key": "", "content": {"url": avatar_mxc}})
        if join_rule:
            initial_state.append({"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": join_rule}})
        if history_visibility:
            initial_state.append(
                {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": history_visibility}}
            )
        if initial_state:
            body["initial_state"] = initial_state
        # `predecessor` ({"room_id", "event_id"}) marks the new room as the
        # continuation of an old one, as a room upgrade does.
        creation_content: dict[str, Any] = {}
        if room_type:
            creation_content["type"] = room_type
        if predecessor:
            creation_content["predecessor"] = predecessor
        if creation_content:
            body["creation_content"] = creation_content
        override: dict[str, Any] = {}
        if power_level_users:
            users = dict(power_level_users)
            if await self._is_pre_v12_default():
                users.setdefault(as_user_id, 100)
            override["users"] = users
        if power_level_events:
            override["events"] = dict(power_level_events)
        if override:
            body["power_level_content_override"] = override
        result = await self._request("POST", "/_matrix/client/v3/createRoom", json_body=body, as_user_id=as_user_id)
        return result["room_id"]

    async def invite_user(self, room_id: str, user_id: str, *, as_user_id: str, is_direct: bool = False) -> None:
        """``is_direct`` marks the invite as one to a direct message, which
        is what makes the invitee's client file the room as a DM."""
        body: dict[str, Any] = {"user_id": user_id}
        if is_direct:
            body["is_direct"] = True
        await self._request("POST", f"/_matrix/client/v3/rooms/{room_id}/invite", json_body=body, as_user_id=as_user_id)

    async def join_room(self, room_id: str, *, as_user_id: str) -> None:
        await self._request("POST", f"/_matrix/client/v3/join/{room_id}", as_user_id=as_user_id)

    async def get_joined_members(self, room_id: str, *, as_user_id: str) -> list[str]:
        result = await self._request("GET", f"/_matrix/client/v3/rooms/{room_id}/joined_members", as_user_id=as_user_id)
        return list(result.get("joined", {}).keys())

    async def send_message_event(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        as_user_id: str,
        ts: int | None = None,
        event_type: str = "m.room.message",
    ) -> str:
        """Send a message event. ``ts`` (Unix milliseconds) uses the
        AppService "timestamp massaging" parameter to show the event at
        that time -- a mirrored post's own posting time -- rather than
        whenever the bridge happened to process it."""
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/send/{quote(event_type, safe='')}/{uuid.uuid4().hex}",
            json_body=content,
            params={"ts": ts} if ts is not None else None,
            as_user_id=as_user_id,
        )
        return result["event_id"]

    async def send_reaction(self, room_id: str, event_id: str, key: str, *, as_user_id: str) -> str:
        """React to ``event_id`` with the emoji ``key``. Returns the
        reaction's own event ID (what a redaction of it targets)."""
        content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}}
        return await self.send_message_event(room_id, content, as_user_id=as_user_id, event_type="m.reaction")

    async def set_profile_field(self, user_id: str, key: str, value: Any) -> None:
        """Set an arbitrary profile field via MSC4133 (Extensible
        Profiles). Raises ``SynapseError`` on a homeserver without it."""
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{user_id}/{quote(key, safe='')}",
            json_body={key: value},
            as_user_id=user_id,
        )

    async def delete_profile_field(self, user_id: str, key: str) -> None:
        """Remove an MSC4133 profile field."""
        await self._request(
            "DELETE", f"/_matrix/client/v3/profile/{user_id}/{quote(key, safe='')}", as_user_id=user_id
        )

    async def redact_event(self, room_id: str, event_id: str, *, as_user_id: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/redact/{event_id}/{uuid.uuid4().hex}",
            json_body={},
            as_user_id=as_user_id,
        )

    async def kick_user(self, room_id: str, user_id: str, *, as_user_id: str, reason: str | None = None) -> None:
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        await self._request("POST", f"/_matrix/client/v3/rooms/{room_id}/kick", json_body=body, as_user_id=as_user_id)

    async def send_state_event(
        self, room_id: str, event_type: str, content: dict[str, Any], *, as_user_id: str, state_key: str = ""
    ) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{quote(state_key, safe='')}",
            json_body=content,
            as_user_id=as_user_id,
        )

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = "", *, as_user_id: str
    ) -> dict[str, Any] | None:
        """The content of one current state event, or ``None`` if the room
        has no such state. Any other failure raises."""
        try:
            return await self._request(
                "GET",
                f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{quote(state_key, safe='')}",
                as_user_id=as_user_id,
            )
        except SynapseError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_event(self, room_id: str, event_id: str, *, as_user_id: str) -> dict[str, Any] | None:
        """A single event, or ``None`` if it doesn't exist (or isn't
        visible). Any other failure raises -- callers rebuilding records
        from events rely on "missing" and "couldn't ask" staying distinct."""
        try:
            return await self._request(
                "GET", f"/_matrix/client/v3/rooms/{room_id}/event/{event_id}", as_user_id=as_user_id
            )
        except SynapseError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def upload_media(self, data: bytes, content_type: str, filename: str, *, as_user_id: str) -> str:
        """Upload to the homeserver's media repository. Returns the ``mxc://`` URI."""
        response = await self._client.post(
            "/_matrix/media/v3/upload",
            headers={"Authorization": f"Bearer {self._as_token}", "Content-Type": content_type},
            params={"filename": filename, "user_id": as_user_id},
            content=data,
        )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise SynapseError(response.status_code, body.get("errcode"), body.get("error"))
        return response.json()["content_uri"]

    async def download_media(self, mxc: str) -> MediaDownload | None:
        """A media item via the authenticated media API, or ``None`` if it
        doesn't exist. Any other failure raises."""
        server_name, _, media_id = mxc.removeprefix("mxc://").partition("/")
        if not mxc.startswith("mxc://") or not server_name or not media_id or "/" in media_id:
            return None
        response = await self._client.get(
            f"/_matrix/client/v1/media/download/{quote(server_name, safe=':')}/{quote(media_id, safe='')}",
            headers={"Authorization": f"Bearer {self._as_token}"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise SynapseError(response.status_code, body.get("errcode"), body.get("error"))
        return MediaDownload(
            content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            content_disposition=response.headers.get("content-disposition"),
        )

    async def get_last_event_id(self, room_id: str, *, as_user_id: str) -> str | None:
        """The most recent event in the room, for a replacement room's
        ``predecessor`` link."""
        try:
            result = await self._request(
                "GET", f"/_matrix/client/v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 1}, as_user_id=as_user_id
            )
        except SynapseError:
            return None
        chunk = result.get("chunk") or []
        return chunk[0].get("event_id") if chunk else None

    async def verify_openid_token(self, access_token: str) -> str | None:
        """The ``@user:server`` a Matrix OpenID token (what a widget gets
        from its client) belongs to, or ``None`` if it isn't valid. Asks
        this homeserver's own ``openid/userinfo`` endpoint, which needs no
        authentication of our own; only tokens for this homeserver's users
        are ever checked, so no federation lookup is needed."""
        try:
            response = await self._client.get(
                "/_matrix/federation/v1/openid/userinfo", params={"access_token": access_token}
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            sub = response.json().get("sub")
        except ValueError:
            return None
        return sub if isinstance(sub, str) else None

    async def is_server_admin(self, user_id: str) -> bool:
        """Whether ``user_id`` is a Synapse server admin. Always False
        without an admin token to ask with."""
        if not self._admin_token:
            return False
        response = await self._client.get(
            f"/_synapse/admin/v2/users/{quote(user_id, safe='@:')}",
            headers={"Authorization": f"Bearer {self._admin_token}"},
        )
        return response.status_code == 200 and bool(response.json().get("admin"))
