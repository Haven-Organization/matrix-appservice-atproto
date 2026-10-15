"""A Matrix room widget for the bridge: buttons for what ``]follow``,
``]block``, ``]replace room`` and the rest do as commands, as a small web
page any widget-capable client can show in a room. Modelled on
matrix-appservice-activitypub's.

Nothing the page's URL says is trusted (a client fills in
``$matrix_user_id``/``$matrix_room_id``, but anyone can write a URL). The
page instead asks its client for a Matrix OpenID token, which the
homeserver itself vouches for (``SynapseClient.verify_openid_token``), and
gets a short-lived session for that user. Every ``/widget/api/*`` action
needs that session (``Authorization: Bearer <session>``), and works only in
a room the user is joined to.

Each action runs the same ``bridge.commands`` handler the command does --
same checks, and the same notice in the room as its answer. The page only
shows state and triggers those; it holds no bridge logic of its own.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from bridge.admins import is_admin
from bridge.commands import (
    _block_rkey,
    _handle_backfill,
    _handle_banner,
    _handle_block,
    _handle_chat,
    _handle_create_profile,
    _handle_follow,
    _handle_import,
    _handle_import_follows,
    _handle_unfollow,
    bot_mxid,
    notice,
    parse_follows_list,
    run_follows_import,
)
from bridge.follows import run_in_background
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

router = APIRouter()

# -- sessions -----------------------------------------------------------------
#
# In memory only: after a restart an open widget just signs in again on its
# next action, which the page does without the user noticing, and a leaked
# session is useless once the process it was made by is gone.
_SESSION_TTL_SECONDS = 3600
_sessions: dict[str, tuple[str, float]] = {}  # session -> (matrix_user_id, expires_at)


def _new_session(matrix_user_id: str) -> str:
    now = time.monotonic()
    for session_id, (_user, expires_at) in list(_sessions.items()):
        if expires_at < now:
            del _sessions[session_id]
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = (matrix_user_id, now + _SESSION_TTL_SECONDS)
    return session_id


def _session_user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing session -- reload the widget.")
    session_id = auth[len("bearer "):].strip()
    entry = _sessions.get(session_id)
    if entry is None or time.monotonic() > entry[1]:
        _sessions.pop(session_id, None)
        raise HTTPException(401, "Session expired -- reload the widget.")
    return entry[0]


async def _room_member(request: Request, room_id: str) -> str:
    """The session's user, provided they are joined to ``room_id``."""
    user = _session_user(request)
    try:
        member = await request.app.state.synapse.get_room_state(
            room_id, "m.room.member", user, as_user_id=bot_mxid(request.app.state.config)
        )
    except SynapseError:
        member = None
    if (member or {}).get("membership") != "join":
        raise HTTPException(403, "You need to be in this room to use the widget here.")
    return user


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("room_id"), str):
        raise HTTPException(400, "room_id is required")
    return body


def _handle_argument(body: dict[str, Any]) -> str:
    handle = body.get("handle")
    return handle.strip() if isinstance(handle, str) else ""


# -- sign-in --------------------------------------------------------------------


@router.post("/widget/api/session")
async def widget_session(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    access_token = body.get("access_token") if isinstance(body, dict) else None
    server_name = body.get("matrix_server_name") if isinstance(body, dict) else None
    if not isinstance(access_token, str) or not access_token or not isinstance(server_name, str):
        raise HTTPException(400, "access_token and matrix_server_name are required")
    config = request.app.state.config
    if server_name != config.synapse.server_name:
        raise HTTPException(403, f"This widget only works for accounts on {config.synapse.server_name}.")
    user = await request.app.state.synapse.verify_openid_token(access_token)
    if user is None or user.split(":", 1)[-1] != config.synapse.server_name:
        raise HTTPException(403, "Could not verify who you are with the homeserver.")
    return JSONResponse({"session_id": _new_session(user), "matrix_user_id": user})


# -- what the page shows ----------------------------------------------------------


async def _display_name(request: Request, user_id: str) -> str | None:
    try:
        return (await request.app.state.synapse.get_profile(user_id)).get("displayname")
    except SynapseError:
        return None


@router.get("/widget/api/context")
async def widget_context(request: Request, room_id: str) -> JSONResponse:
    user = await _room_member(request, room_id)
    app = request.app
    config = app.state.config
    repository = app.state.repository
    own = await repository.get_account_for_user(user)

    room: dict[str, Any] = {"kind": "unmanaged"}
    profile_account = await repository.get_account_for_profile_room(room_id)
    remote = await repository.get_remote_account_for_room(room_id)
    chat = await repository.get_chat_room_for_room(room_id)
    if profile_account is not None:
        handle = app.state.pds.handle_of(profile_account)
        try:
            name = ((await app.state.synapse.get_room_state(room_id, "m.room.name", as_user_id=bot_mxid(config))) or {}).get("name")
        except SynapseError:
            name = None
        room = {
            "kind": "profile",
            "is_owner": profile_account.matrix_user_id == user,
            "handle": handle,
            "did": profile_account.did,
            "display_name": name,
            "public_url": f"https://bsky.app/profile/{handle}",
        }
    elif remote is not None:
        room = {
            "kind": "remote",
            "handle": remote.handle,
            "did": remote.did,
            "display_name": await _display_name(request, remote.ghost_user_id),
            "public_url": f"https://bsky.app/profile/{remote.handle}",
            "is_following": own is not None and await repository.get_follow_rkey(own.did, remote.did) is not None,
            "is_blocked": own is not None and await _block_rkey(request, own, remote.did) is not None,
        }
    elif chat is not None:
        other = await repository.get_remote_account(chat.remote_did)
        room = {
            "kind": "chat",
            "is_owner": own is not None and chat.local_did == own.did,
            "handle": other.handle if other else None,
            "did": chat.remote_did,
        }
    elif (owner := await repository.get_notifications_room_owner(room_id)) is not None:
        room = {"kind": "notifications", "is_owner": owner == user}
    elif (owner := await repository.get_space_owner(room_id)) is not None:
        room = {"kind": "space", "is_owner": owner == user}

    return JSONResponse(
        {
            "matrix_user_id": user,
            "is_admin": await is_admin(app, user),
            "has_profile": own is not None,
            "has_profile_room": own is not None and bool(own.profile_room_id),
            "own_handle": app.state.pds.handle_of(own) if own is not None else None,
            "domain": config.bridge.domain,
            "backfill_default_count": config.atproto.follow_backfill_count,
            "command_prefix": config.appservice.command_prefix,
            "room": room,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/following")
async def widget_following(request: Request) -> JSONResponse:
    user = _session_user(request)
    repository = request.app.state.repository
    own = await repository.get_account_for_user(user)
    follows = await repository.list_follows(own.did) if own is not None else []
    entries = [{"did": remote.did, "handle": remote.handle, "room_id": remote.room_id} for remote in follows]
    return JSONResponse({"following": entries}, headers={"Cache-Control": "no-store"})


# -- actions ----------------------------------------------------------------------
#
# Each answers {"ok": true} whatever the handler decided: the page reloads
# its context afterwards, and the handler's notice in the room says what
# happened, exactly as if the command had been typed.


async def _dispatch(request: Request, room_id: str, handler, /, **kwargs: Any) -> JSONResponse:
    user = await _room_member(request, room_id)
    try:
        await handler(request, sender=user, room_id=room_id, **kwargs)
    except SynapseError as exc:
        logger.warning("Widget action in %s failed: %s", room_id, exc, exc_info=True)
        raise HTTPException(502, "The homeserver refused that -- try again.") from exc
    return JSONResponse({"ok": True})


@router.post("/widget/api/follow")
async def widget_follow(request: Request) -> JSONResponse:
    body = await _json(request)
    return await _dispatch(request, body["room_id"], _handle_follow, argument=_handle_argument(body))


@router.post("/widget/api/unfollow")
async def widget_unfollow(request: Request) -> JSONResponse:
    body = await _json(request)
    return await _dispatch(request, body["room_id"], _handle_unfollow, argument=_handle_argument(body))


@router.post("/widget/api/block")
async def widget_block(request: Request) -> JSONResponse:
    body = await _json(request)
    return await _dispatch(request, body["room_id"], _handle_block, argument=_handle_argument(body), block=True)


@router.post("/widget/api/unblock")
async def widget_unblock(request: Request) -> JSONResponse:
    body = await _json(request)
    return await _dispatch(request, body["room_id"], _handle_block, argument=_handle_argument(body), block=False)


@router.post("/widget/api/chat")
async def widget_chat(request: Request) -> JSONResponse:
    body = await _json(request)
    return await _dispatch(request, body["room_id"], _handle_chat, argument=_handle_argument(body))


@router.post("/widget/api/import")
async def widget_import(request: Request) -> JSONResponse:
    body = await _json(request)
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(400, "url is required")
    return await _dispatch(request, body["room_id"], _handle_import, argument=url.strip())


@router.post("/widget/api/import_follows_from")
async def widget_import_follows_from(request: Request) -> JSONResponse:
    body = await _json(request)
    if not _handle_argument(body):
        raise HTTPException(400, "handle is required")
    return await _dispatch(request, body["room_id"], _handle_import_follows, argument=_handle_argument(body), content={})


@router.post("/widget/api/import_follows")
async def widget_import_follows(request: Request, room_id: str) -> JSONResponse:
    """Like replying ``import follows`` to an uploaded file, but the page
    hands over the file itself."""
    user = await _room_member(request, room_id)
    own = await request.app.state.repository.get_account_for_user(user)
    if own is None:
        raise HTTPException(400, "You need a Bluesky identity before importing follows.")
    references = parse_follows_list((await request.body()).decode("utf-8", errors="replace"))
    if not references:
        raise HTTPException(
            400, "That file doesn't list any Bluesky accounts -- expected one handle, DID or bsky.app profile link per line."
        )
    await notice(
        request, room_id,
        f"Importing {len(references)} follows -- this can take a while (each new account gets its own room). "
        "I'll post a summary here when it's done.",
    )
    run_in_background(request.app, run_follows_import(request, account=own, room_id=room_id, references=references))
    return JSONResponse({"ok": True, "count": len(references)})


@router.post("/widget/api/backfill")
async def widget_backfill(request: Request) -> JSONResponse:
    body = await _json(request)
    count = body.get("count")
    argument = str(int(count)) if isinstance(count, (int, float)) and count else ""
    return await _dispatch(request, body["room_id"], _handle_backfill, argument=argument, content={})


@router.post("/widget/api/create_profile")
async def widget_create_profile(request: Request) -> JSONResponse:
    body = await _json(request)
    return await _dispatch(request, body["room_id"], _handle_create_profile)


async def _danger(request: Request, action: str) -> JSONResponse:
    """The Danger Zone commands. The page has already asked "are you
    sure?", so these run straight away, as in matrix-appservice-activitypub
    -- except deleting a profile, which can never be undone (its DID is
    retired for good): that posts the usual warning in the room, to be
    confirmed there by replying "confirm"."""
    # Imported here: bridge.danger builds on bridge.commands, as this does.
    from bridge import danger

    body = await _json(request)
    if action == "delete_profile":
        return await _dispatch(request, body["room_id"], danger.handle_delete_profile)
    executor = danger._EXECUTORS[action]
    return await _dispatch(request, body["room_id"], executor, args={})


@router.post("/widget/api/link_profile")
async def widget_link_profile(request: Request) -> JSONResponse:
    return await _danger(request, "link_profile")


@router.post("/widget/api/unlink_profile")
async def widget_unlink_profile(request: Request) -> JSONResponse:
    return await _danger(request, "unlink_profile")


@router.post("/widget/api/replace_room")
async def widget_replace_room(request: Request) -> JSONResponse:
    return await _danger(request, "replace_room")


@router.post("/widget/api/delete_profile")
async def widget_delete_profile(request: Request) -> JSONResponse:
    return await _danger(request, "delete_profile")


@router.post("/widget/api/upload_banner")
async def widget_upload_banner(request: Request, room_id: str) -> JSONResponse:
    """``banner``, given the image itself: uploaded as the bot (the
    appservice can't upload as a real user), which doesn't matter to the
    banner -- only that the media exists."""
    user = await _room_member(request, room_id)
    data = await request.body()
    if not data:
        raise HTTPException(400, "No image received")
    content_type = request.headers.get("content-type") or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(400, "That isn't an image")
    try:
        mxc = await request.app.state.synapse.upload_media(
            data, content_type, "banner", as_user_id=bot_mxid(request.app.state.config)
        )
        await _handle_banner(request, sender=user, room_id=room_id, argument=mxc, content={})
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver refused that image.") from exc
    return JSONResponse({"ok": True})


# -- the page -----------------------------------------------------------------------


@router.get("/widget/icon")
async def widget_icon(request: Request) -> Response:
    """The bot's avatar, as the page's icon. Fetched here because the
    homeserver URL the bridge uses is usually not one a browser can reach."""
    mxc = request.app.state.config.appservice.bot_avatar_mxc
    if not mxc:
        raise HTTPException(404, "No bot avatar configured")
    try:
        download = await request.app.state.synapse.download_media(mxc)
    except SynapseError as exc:
        raise HTTPException(502, "Could not fetch the bot's avatar") from exc
    if download is None:
        raise HTTPException(404, "No bot avatar")
    return Response(content=download.content, media_type=download.content_type, headers={"Cache-Control": "max-age=86400"})


@router.get("/widget")
async def widget_page() -> HTMLResponse:
    # no-store: otherwise a client's webview may keep showing an old
    # version of the page after the bridge is updated.
    return HTMLResponse(_WIDGET_HTML, headers={"Cache-Control": "no-store, must-revalidate"})


_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bluesky Bridge</title>
<link rel="icon" href="/widget/icon">
<style>
  :root {
    --bg: #f5f7fa; --card: #ffffff; --border: #e1e6ec; --text: #0b0f14; --muted: #5f6b7a;
    --accent: #1083fe; --accent-text: #ffffff; --danger: #d92d20; --danger-bg: #fef3f2;
    --ok-bg: #ecfdf3; --ok-text: #067647;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #12161c; --card: #1a2029; --border: #2a3340; --text: #e6ebf1; --muted: #93a0b0;
      --accent: #4ea1ff; --accent-text: #0b0f14; --danger: #f97066; --danger-bg: #2b1618;
      --ok-bg: #0f2a1d; --ok-text: #47cd89;
    }
  }
  :root[data-theme="dark"] {
    --bg: #12161c; --card: #1a2029; --border: #2a3340; --text: #e6ebf1; --muted: #93a0b0;
    --accent: #4ea1ff; --accent-text: #0b0f14; --danger: #f97066; --danger-bg: #2b1618;
    --ok-bg: #0f2a1d; --ok-text: #47cd89;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; min-height: 100vh;
  }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 12px; }
  h1 { font-size: 15px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 10px; }
  .sub { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; overflow-wrap: anywhere; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--accent); color: var(--accent-text); font-weight: 600; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .row:last-child { margin-bottom: 0; }
  button {
    font: inherit; font-weight: 600; border: 1px solid var(--border); background: var(--card);
    color: var(--text); border-radius: 8px; padding: 8px 14px; cursor: pointer;
  }
  button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  button.danger { color: var(--danger); border-color: var(--danger); }
  button:disabled { opacity: .5; cursor: default; }
  input[type=text], input[type=number] {
    font: inherit; border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: 8px; padding: 8px 10px; flex: 1; min-width: 120px;
  }
  input[type=file] { max-width: 100%; }
  .toast { position: fixed; left: 16px; right: 16px; bottom: 16px; padding: 10px 14px; border-radius: 10px; font-size: 13px; font-weight: 600; display: none; z-index: 10; }
  .toast.ok { background: var(--ok-bg); color: var(--ok-text); display: block; }
  .toast.err { background: var(--danger-bg); color: var(--danger); display: block; }
  .kv { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; padding: 4px 0; }
  .kv span:first-child { color: var(--muted); }
  .kv span:last-child { overflow-wrap: anywhere; text-align: right; }
  .list { display: flex; flex-direction: column; gap: 6px; }
  .list-item { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; }
  .list-item span { overflow-wrap: anywhere; }
  .muted { color: var(--muted); }
  .spinner { text-align: center; color: var(--muted); padding: 40px 0; }
  a { color: var(--accent); }
</style>
</head>
<body>
  <div id="app"><div class="spinner">Connecting&hellip;</div></div>
  <div id="toast" class="toast"></div>
<script>
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const widgetId = params.get("widgetId") || "";
  const roomId = params.get("matrix_room_id") || "";
  const theme = params.get("theme") || "";
  if (theme.indexOf("dark") !== -1) document.documentElement.setAttribute("data-theme", "dark");
  else if (theme.indexOf("light") !== -1) document.documentElement.setAttribute("data-theme", "light");

  // -- Matrix Widget API: just enough to get an OpenID token. No room
  // capabilities are asked for; every action goes through the bridge's
  // own API under the verified identity.
  let reqCounter = 0;
  const pending = {};
  let openIdResolve = null;

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.widgetId && widgetId && msg.widgetId !== widgetId) return;
    if (msg.api === "fromWidget" && msg.requestId && pending[msg.requestId] && "response" in msg) {
      const resolve = pending[msg.requestId];
      delete pending[msg.requestId];
      resolve(msg.response);
      return;
    }
    if (msg.api === "toWidget" && msg.action === "capabilities") {
      window.parent.postMessage(Object.assign({}, msg, { response: { capabilities: [] } }), "*");
      return;
    }
    if (msg.api === "toWidget" && msg.action === "openid_credentials") {
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
      if (openIdResolve) { const resolve = openIdResolve; openIdResolve = null; resolve(msg.data); }
      return;
    }
    if (msg.api === "toWidget" && msg.requestId) {
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
    }
  });

  function sendFromWidget(action, data) {
    return new Promise((resolve) => {
      const requestId = "req-" + (reqCounter++) + "-" + Date.now();
      pending[requestId] = resolve;
      window.parent.postMessage({ api: "fromWidget", widgetId, requestId, action, data: data || {} }, "*");
    });
  }

  async function getOpenIdToken() {
    const first = await sendFromWidget("get_openid", {});
    if (first && first.state === "allowed") return first;
    if (first && first.state === "blocked") throw new Error("Your client refused to tell the widget who you are.");
    // "request": the client is asking you; the token arrives later.
    return new Promise((resolve, reject) => {
      openIdResolve = resolve;
      setTimeout(() => {
        if (openIdResolve) { openIdResolve = null; reject(new Error("Timed out waiting for approval.")); }
      }, 60000);
    });
  }

  sendFromWidget("content_loaded", {});

  // -- the bridge's API ------------------------------------------------------

  let sessionId = null;

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign(opts.raw ? {} : { "Content-Type": "application/json" }, opts.headers || {});
    if (sessionId) headers["Authorization"] = "Bearer " + sessionId;
    const body = opts.raw ? opts.raw : (opts.body ? JSON.stringify(opts.body) : undefined);
    const res = await fetch(path, { method: opts.method || "GET", headers, body });
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    return data;
  }

  function toast(message, ok) {
    const node = document.getElementById("toast");
    node.textContent = message;
    node.className = "toast " + (ok ? "ok" : "err");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { node.className = "toast"; }, 4000);
  }

  async function doAction(path, body, button, done) {
    if (button) button.disabled = true;
    try {
      await api(path, { method: "POST", body: Object.assign({ room_id: roomId }, body || {}) });
      toast(done || "Done -- see the room for my answer.", true);
      await loadContext();
    } catch (e) {
      toast(e.message, false);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function upload(path, file, button, done) {
    button.disabled = true;
    try {
      const result = await api(path + "?room_id=" + encodeURIComponent(roomId), {
        method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, raw: file,
      });
      toast(done(result), true);
      await loadContext();
    } catch (e) {
      toast(e.message, false);
    } finally {
      button.disabled = false;
    }
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "class") node.className = attrs[k];
      else if (k.indexOf("on") === 0) node.addEventListener(k.slice(2), attrs[k]);
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }
  function button(label, kind, handler) { return el("button", { class: kind || "", onclick: handler, text: label }); }
  function card(title, children) { return el("div", { class: "card" }, [el("h2", { text: title })].concat(children)); }
  function sub(text) { return el("div", { class: "sub", text: text }); }
  function kv(key, value) { return el("div", { class: "kv" }, [el("span", { text: key }), el("span", { text: value })]); }

  let ctx = null;

  function kindLabel(kind) {
    return {
      profile: "Profile Room", remote: "Bluesky account", chat: "Chat", notifications: "Notifications",
      space: "Space", unmanaged: "Other room",
    }[kind] || kind;
  }

  function render() {
    const app = document.getElementById("app");
    app.innerHTML = "";
    if (!ctx) return;
    app.appendChild(el("div", { class: "card" }, [
      el("h1", {}, [el("span", { text: "\u{1F98B} Bluesky Bridge" }), el("span", { class: "badge", text: kindLabel(ctx.room.kind) })]),
      sub(ctx.matrix_user_id + (ctx.own_handle ? "  ·  @" + ctx.own_handle : "") + (ctx.is_admin ? "  ·  admin" : "")),
    ]));

    if (!ctx.has_profile) {
      app.appendChild(card("Get started", [
        sub("You don't have a Bluesky identity yet. This creates one (yourname." + ctx.domain + ") and a room to post from."),
        el("div", { class: "row" }, [button("Create my Bluesky identity", "primary", (e) => doAction("/widget/api/create_profile", {}, e.target))]),
      ]));
    }

    const kind = ctx.room.kind;
    if (kind === "profile") renderProfile(app);
    else if (kind === "remote") renderRemote(app);
    else if (kind === "chat") renderChat(app);
    else if (kind === "notifications" || kind === "space") renderOwnedRoom(app);
    else renderUnmanaged(app);

    if (ctx.has_profile) {
      renderManageAccount(app);
      renderFollowing(app);
      renderImportPost(app);
      renderImportFollows(app);
    }
    app.appendChild(el("div", { class: "card" }, [
      sub("Each button does the same as its " + ctx.command_prefix + " command, and answers in the room the same way."),
    ]));
  }

  function renderProfile(app) {
    const r = ctx.room;
    const children = [kv("Handle", "@" + r.handle), kv("Display name", r.display_name || "—"), kv("DID", r.did)];
    children.push(sub(""));
    children[children.length - 1].appendChild(el("a", { href: r.public_url, target: "_blank", rel: "noopener", text: "View on Bluesky →" }));
    if (r.is_owner) {
      children.push(el("div", { class: "row" }, [
        button("Replace this room", "", (e) => {
          if (confirm("Create a fresh Profile Room and retire this one?")) doAction("/widget/api/replace_room", {}, e.target);
        }),
        button("Unlink profile", "danger", (e) => {
          if (confirm("Detach this room from your Bluesky identity? Nothing you post here will be published until you link a room again.")) doAction("/widget/api/unlink_profile", {}, e.target);
        }),
        button("Delete profile…", "danger", (e) => doAction("/widget/api/delete_profile", {}, e.target,
          "I've posted what deleting your identity does in the room -- reply \"confirm\" to it there to go ahead.")),
      ]));
    }
    app.appendChild(card(r.is_owner ? "Your profile" : "Profile", children));
    if (r.is_owner) renderBanner(app);
  }

  function renderBanner(app) {
    const fileInput = el("input", { type: "file", accept: "image/*" });
    app.appendChild(card("Profile banner", [
      el("div", { class: "row" }, [fileInput]),
      el("div", { class: "row" }, [button("Upload banner", "primary", (e) => {
        const file = fileInput.files[0];
        if (!file) { toast("Choose an image first.", false); return; }
        upload("/widget/api/upload_banner", file, e.target, () => "Banner updated.");
      })]),
    ]));
  }

  function renderRemote(app) {
    const r = ctx.room;
    const children = [kv("Name", r.display_name || "—"), kv("Handle", "@" + r.handle), kv("DID", r.did), sub("")];
    children[3].appendChild(el("a", { href: r.public_url, target: "_blank", rel: "noopener", text: "View on Bluesky →" }));
    if (ctx.has_profile) {
      children.push(el("div", { class: "row" }, [
        r.is_following
          ? button("Unfollow", "danger", (e) => doAction("/widget/api/unfollow", {}, e.target))
          : button("Follow", "primary", (e) => doAction("/widget/api/follow", {}, e.target)),
        button("Chat", "", (e) => doAction("/widget/api/chat", {}, e.target)),
        r.is_blocked
          ? button("Unblock", "", (e) => doAction("/widget/api/unblock", {}, e.target))
          : button("Block", "danger", (e) => {
              if (confirm("Block @" + r.handle + "? This also unfollows them.")) doAction("/widget/api/block", {}, e.target);
            }),
      ]));
      const backfillRow = el("div", { class: "row" }, [
        button("Backfill latest " + ctx.backfill_default_count + " posts", "", (e) => doAction("/widget/api/backfill", {}, e.target)),
      ]);
      if (ctx.is_admin) {
        const countInput = el("input", { type: "number", min: "1", placeholder: "custom count" });
        backfillRow.appendChild(countInput);
        backfillRow.appendChild(button("Backfill custom amount", "", (e) =>
          doAction("/widget/api/backfill", { count: parseInt(countInput.value || "0", 10) }, e.target)));
        backfillRow.appendChild(button("Replace this room", "", (e) => {
          if (confirm("Create a fresh room for @" + r.handle + " and retire this one?")) doAction("/widget/api/replace_room", {}, e.target);
        }));
      }
      children.push(backfillRow);
    }
    app.appendChild(card("Bluesky account", children));
  }

  function renderChat(app) {
    const r = ctx.room;
    const children = [kv("With", r.handle ? "@" + r.handle : r.did)];
    if (r.is_owner) {
      children.push(el("div", { class: "row" }, [
        button("Replace this room", "", (e) => {
          if (confirm("Create a fresh room for this chat and retire this one?")) doAction("/widget/api/replace_room", {}, e.target);
        }),
        button("Block", "danger", (e) => {
          if (confirm("Block this account?")) doAction("/widget/api/block", { handle: r.did }, e.target);
        }),
      ]));
    }
    app.appendChild(card("Chat", children));
  }

  function renderOwnedRoom(app) {
    const r = ctx.room;
    const children = [sub(r.kind === "notifications"
      ? "Where I tell you about follows, likes, reposts, replies, mentions and quotes on Bluesky."
      : "Holds every room the bridge manages for you.")];
    if (r.kind === "notifications" && r.is_owner) {
      children.push(el("div", { class: "row" }, [button("Replace this room", "", (e) => {
        if (confirm("Create a fresh notifications room and retire this one?")) doAction("/widget/api/replace_room", {}, e.target);
      })]));
    }
    app.appendChild(card(kindLabel(r.kind), children));
  }

  function renderUnmanaged(app) {
    const children = [sub("This isn't a room the bridge manages.")];
    if (!ctx.has_profile_room) {
      children.push(sub("You can make it your Profile Room: what you post here is then published on Bluesky, and its name, topic and avatar become your profile's."));
      children.push(el("div", { class: "row" }, [button("Make this my Profile Room", "", (e) => {
        const extra = ctx.has_profile ? "" : " A Bluesky identity is also created and registered for you, publicly and permanently.";
        if (confirm("Make this room your permanent Bluesky profile? Everything you post here from then on is published publicly." + extra)) doAction("/widget/api/link_profile", {}, e.target);
      })]));
    }
    app.appendChild(card("This room", children));
  }

  function renderManageAccount(app) {
    const input = el("input", { type: "text", placeholder: "handle.bsky.social" });
    const target = () => input.value.trim();
    app.appendChild(card("Follow, chat with or block an account", [
      el("div", { class: "row" }, [input]),
      el("div", { class: "row" }, [
        button("Follow", "primary", (e) => { if (target()) doAction("/widget/api/follow", { handle: target() }, e.target); }),
        button("Chat", "", (e) => { if (target()) doAction("/widget/api/chat", { handle: target() }, e.target); }),
        button("Block", "danger", (e) => {
          if (target() && confirm("Block " + target() + "? This also unfollows them.")) doAction("/widget/api/block", { handle: target() }, e.target);
        }),
        button("Unblock", "", (e) => { if (target()) doAction("/widget/api/unblock", { handle: target() }, e.target); }),
      ]),
    ]));
  }

  function renderFollowing(app) {
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(card("Following", [list]));
    api("/widget/api/following").then((data) => {
      list.innerHTML = "";
      if (!data.following.length) { list.appendChild(el("div", { class: "muted", text: "You don't follow anyone yet." })); return; }
      data.following.forEach((f) => {
        list.appendChild(el("div", { class: "list-item" }, [
          el("span", { text: "@" + f.handle }),
          button("Unfollow", "", (e) => doAction("/widget/api/unfollow", { handle: f.did }, e.target)),
        ]));
      });
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  function renderImportPost(app) {
    const input = el("input", { type: "text", placeholder: "https://bsky.app/profile/…/post/…" });
    app.appendChild(card("Import a post", [
      sub("Bring one post into Matrix without following its author."),
      el("div", { class: "row" }, [input, button("Import", "primary", (e) => {
        if (input.value.trim()) doAction("/widget/api/import", { url: input.value.trim() }, e.target).then(() => { input.value = ""; });
      })]),
    ]));
  }

  function renderImportFollows(app) {
    const handleInput = el("input", { type: "text", placeholder: "your-old-account.bsky.social" });
    const fileInput = el("input", { type: "file", accept: ".csv,.txt,text/plain,text/csv" });
    app.appendChild(card("Import follows", [
      sub("Follow everyone another Bluesky account follows -- your old account, say."),
      el("div", { class: "row" }, [handleInput, button("Import their follows", "primary", (e) => {
        if (handleInput.value.trim()) doAction("/widget/api/import_follows_from", { handle: handleInput.value.trim() }, e.target,
          "Importing -- I'll post a summary in the room when it's done.");
      })]),
      sub("Or a file listing handles, DIDs or bsky.app profile links, one per line."),
      el("div", { class: "row" }, [fileInput]),
      el("div", { class: "row" }, [button("Import file", "", (e) => {
        const file = fileInput.files[0];
        if (!file) { toast("Choose a file first.", false); return; }
        upload("/widget/api/import_follows", file, e.target,
          (result) => "Importing " + result.count + " follows -- I'll post a summary in the room when it's done.");
      })]),
    ]));
  }

  async function loadContext() {
    ctx = await api("/widget/api/context?room_id=" + encodeURIComponent(roomId));
    render();
  }

  async function start() {
    try {
      if (!roomId) throw new Error("Open this from a Matrix room's widget panel.");
      const creds = await getOpenIdToken();
      const session = await api("/widget/api/session", {
        method: "POST", body: { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name },
      });
      sessionId = session.session_id;
      await loadContext();
    } catch (e) {
      const app = document.getElementById("app");
      app.innerHTML = "";
      app.appendChild(card("Couldn't connect", [sub(e.message)]));
    }
  }

  start();
})();
</script>
</body>
</html>
"""
