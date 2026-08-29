"""Single-session state and room fanout.

The container hosts exactly one play session at a time, so session and room
state are module globals.
"""

import asyncio
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Optional

from starlette.websockets import WebSocket, WebSocketState

from . import selkies, settings

if TYPE_CHECKING:
    from .emulators.base import Emulator

log = logging.getLogger(__name__)

SESSION: Optional[dict[str, Any]] = None
"""The active play session, or None.

Shape:

```
{
  "id", "active", "created_at",
  "user": {...}, "emulator": "pcsx2", "rom": {...}, "rom_file": str,
  "save": {...} | None, "callback": {...} | None, "multiplayer": bool,
  "controller_token",
  "viewers": [{"token","slot","mk_control","username","permission"}...],
  "controller_slot", "mk_owner_token", "designated_speaker",
  "save_baseline": float, "emulator_obj": Emulator,
}
```
"""

LAST_EXIT: Optional[dict[str, Any]] = None
"""What is left of the session that just exited: `{"id", "rom", "emulator_obj"}`.

RomM files the exit state in its library after the teardown has answered, so
the emulator that captured it has to outlive the session for the read routes
to find the file. Dropped at the next activate.
"""

ROOM: dict[str, Any] = {"controller": None, "viewers": {}, "cooldowns": {}}
"""Live websocket connections for the room.

`controller` is the controller's connection info dict (or None while offline),
`viewers` maps each online viewer's token to its connection info dict, and
`cooldowns` tracks per-token rate limits.
"""


def _session_id(raw: object) -> str:
    """Reduce a caller-supplied id to what is safe in the export filename.

    The exit archive is named after the session, and the export routes reject
    anything with path structure in it, so an id carrying a slash or a dot
    would produce an archive the parent could never fetch back.

    Args:
        raw: The `session_id` from the activate payload, of any type or None.

    Returns:
        `raw` stringified and stripped to `[A-Za-z0-9_-]`, at most 64
        characters, or a fresh random hex id when nothing is left.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:64]
    return cleaned or secrets.token_hex(8)


def new_session(payload: dict[str, Any], emulator_obj: "Emulator", rom_file: str) -> dict[str, Any]:
    """Replace the module-level session with a fresh one built from the activate payload.

    The controller starts with gamepad 1 and mouse/keyboard control; the viewer
    list starts empty and the save baseline is the moment of creation.

    Args:
        payload: The activate request body; `emulator` is required, the rest
            (`session_id`, `user`, `rom`, `save`, `callback`, `multiplayer`) is
            optional.
        emulator_obj: The emulator instance driving this session.
        rom_file: The resolved path of the ROM file being played.

    Returns:
        The new session dict, which is also stored in `SESSION`.
    """
    global SESSION, LAST_EXIT
    # The previous session's state is about to be cleared off the working slot,
    # and serving it under this session's rom would file it against the wrong
    # game, so the record goes before the new one is built.
    LAST_EXIT = None
    SESSION = {
        "id": _session_id(payload.get("session_id")),
        "active": True,
        "created_at": time.time(),
        "user": payload.get("user") or {},
        "emulator": payload["emulator"],
        "rom": payload.get("rom") or {},
        "rom_file": rom_file,
        "save": payload.get("save"),
        "callback": payload.get("callback"),
        "multiplayer": bool(payload.get("multiplayer")),
        "controller_token": secrets.token_urlsafe(16),
        "viewers": [],
        # Reusable invite tokens by permission, minted on first request. One
        # link per role is what a host hands round; each arrival on it takes
        # its own seat.
        "invites": {},
        # The controller starts with gamepad 1.
        "controller_slot": 1,
        "mk_owner_token": None,
        "designated_speaker": None,
        "save_baseline": time.time(),
        "emulator_obj": emulator_obj,
    }
    return SESSION


def retire_session() -> None:
    """End the session, keeping what the state routes still have to answer with.

    Exit captures a state and then tears the session down, but RomM only asks
    for that state once the teardown has replied, so clearing outright is what
    made every post-exit read a 409. Only the emulator is kept, and only the
    read routes consult it: nothing here can be played, written to or resumed.
    """
    global SESSION, LAST_EXIT
    if SESSION is not None:
        LAST_EXIT = {
            "id": SESSION["id"],
            "rom": SESSION.get("rom") or {},
            "emulator_obj": SESSION["emulator_obj"],
        }
    SESSION = None


def find_viewer(token: str) -> Optional[dict[str, Any]]:
    """Look up a viewer entry in the active session by its token.

    Args:
        token: The viewer's streaming token.

    Returns:
        The viewer dict, or None when there is no session or no viewer holds
        that token.
    """
    if SESSION is None:
        return None
    return next((v for v in SESSION.get("viewers", []) if v["token"] == token), None)


def invite_token(permission: str) -> str:
    """Return the session's shareable invite token for a permission, minting it on first use.

    The token identifies the session and the permission, not a person: every
    arrival on it is seated separately by `add_viewer`, so the same link can go
    to any number of friends and stays valid until the session ends.

    Args:
        permission: The permission the link grants, `participant` or `readonly`.

    Returns:
        The invite token for that permission.
    """
    invites = SESSION.setdefault("invites", {})
    if permission not in invites:
        invites[permission] = secrets.token_urlsafe(16)
    return invites[permission]


def find_invite(token: str) -> Optional[str]:
    """Resolve an invite token to the permission it grants.

    Args:
        token: The `invite` query parameter from a landing URL.

    Returns:
        The permission, or None when no session is active or the token is unknown.
    """
    if SESSION is None:
        return None
    for permission, minted in SESSION.get("invites", {}).items():
        if secrets.compare_digest(minted, token):
            return permission
    return None


def add_viewer(permission: str, user: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Mint a viewer token and add the viewer to the active session.

    A re-join by the same user (matched by id, else username) replaces the old
    entry and invalidates its token.

    Args:
        permission: The viewer's permission level, e.g. `readonly`.
        user: The joining user's details (`id`, `display_name`, `username`), or
            None for an anonymous viewer who gets a generated username.

    Returns:
        The new viewer dict: `{"token", "user_id", "anonymous", "last_seen",
        "slot", "mk_control", "username", "permission"}`. None when the
        session is at `settings.MAX_ROOM_VIEWERS` seats and none of them can
        be reclaimed: every seat is either a named user or an anonymous seat
        that is still connected.
    """
    import random

    user = user or {}
    username = user.get("display_name") or user.get("username")
    anonymous = user.get("id") is None and not username

    def _same_user(v: dict[str, Any]) -> bool:
        """Whether viewer entry `v` belongs to the user now joining."""
        if user.get("id") is not None and v.get("user_id") is not None:
            return v["user_id"] == user["id"]
        return bool(username) and v.get("username") == username

    SESSION["viewers"] = [v for v in SESSION.get("viewers", []) if not _same_user(v)]

    if len(SESSION["viewers"]) >= settings.MAX_ROOM_VIEWERS:
        # A seat lives for the whole session by design (its invite link stays
        # valid after the tab closes), so an anonymous arrival never frees its
        # own slot on disconnect. Reclaim the anonymous seat that has gone
        # longest without a live socket (never-connected first) rather than
        # treat every seat ever minted as permanent. "anonymous" mirrors
        # _same_user above: a seat with neither an id nor a username to
        # identify it, not merely one with no id.
        online_tokens = set(ROOM.get("viewers", {}).keys())
        reclaimable = [
            v for v in SESSION["viewers"] if v.get("anonymous") and v["token"] not in online_tokens
        ]
        stale = min(
            reclaimable,
            key=lambda v: (v.get("last_seen") is not None, v.get("last_seen") or 0),
            default=None,
        )
        if stale is None:
            log.warning(
                "session: refusing new viewer, room is at its %d-seat cap",
                settings.MAX_ROOM_VIEWERS,
            )
            return None
        log.info("session: reclaiming a disconnected anonymous seat for a new arrival")
        SESSION["viewers"].remove(stale)
        # Mirrors the disconnect cleanup in room.py: a token that no longer
        # holds a seat must not stay wired up as the speaker or MK owner.
        if SESSION.get("designated_speaker") == stale["token"]:
            SESSION["designated_speaker"] = None
        if SESSION.get("mk_owner_token") == stale["token"]:
            SESSION["mk_owner_token"] = None

    viewer = {
        "token": secrets.token_urlsafe(16),
        "user_id": user.get("id"),
        "anonymous": anonymous,
        "last_seen": None,
        "slot": None,
        "mk_control": False,
        "username": username or f"User-{random.randint(100, 999)}",
        "permission": permission,
    }
    SESSION.setdefault("viewers", []).append(viewer)
    return viewer


async def broadcast_to_room(payload: dict[str, Any]) -> None:
    """Send a JSON message to every connected room member.

    Sends run concurrently; a failure on one socket is logged and does not
    stop delivery to the others.

    Args:
        payload: The JSON-serializable message, normally carrying a `type` key.
    """
    all_ws = []
    if ROOM.get("controller"):
        all_ws.append(ROOM["controller"]["websocket"])
    for conn in ROOM.get("viewers", {}).values():
        all_ws.append(conn["websocket"])
    tasks = [
        ws.send_json(payload)
        for ws in all_ws
        if ws.client_state == WebSocketState.CONNECTED
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            log.warning("room send failed: %s", result)


async def broadcast_binary_to_room(payload: bytes, sender_ws: WebSocket) -> None:
    """Relay a binary media frame to every connected room member except its sender.

    Args:
        payload: The raw frame in the room's binary wire format.
        sender_ws: The websocket the frame arrived on; it is skipped.
    """
    all_ws = []
    if ROOM.get("controller") and ROOM["controller"]["websocket"] != sender_ws:
        all_ws.append(ROOM["controller"]["websocket"])
    for conn in ROOM.get("viewers", {}).values():
        if conn["websocket"] != sender_ws:
            all_ws.append(conn["websocket"])
    tasks = [
        ws.send_bytes(payload)
        for ws in all_ws
        if ws.client_state == WebSocketState.CONNECTED
    ]
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.warning("room binary send failed: %s", result)


async def broadcast_state() -> None:
    """Broadcast a `state_update` describing every room member to the room.

    The controller is listed first, then each viewer with its online status,
    mouse/keyboard ownership and, while connected, its public id. Does nothing
    when there is no session.
    """
    if SESSION is None:
        return
    controller_name = (SESSION.get("user") or {}).get("display_name") or "Controller"
    controller_info = {
        "token": SESSION["controller_token"],
        "username": controller_name,
        "slot": SESSION.get("controller_slot"),
        "online": ROOM.get("controller") is not None,
        "has_mk": (SESSION.get("mk_owner_token") == SESSION["controller_token"])
        or (SESSION.get("mk_owner_token") is None),
        "permission": "controller",
        "publicId": ROOM["controller"]["public_id"] if ROOM.get("controller") else None,
    }
    online_tokens = set(ROOM.get("viewers", {}).keys())
    users = [controller_info]
    for v in SESSION.get("viewers", []):
        info = v.copy()
        info["has_mk"] = SESSION.get("mk_owner_token") == v["token"]
        info["online"] = v["token"] in online_tokens
        if info["online"]:
            conn = ROOM["viewers"].get(v["token"])
            if conn:
                info["publicId"] = conn.get("public_id")
        users.append(info)
    await broadcast_to_room(
        {
            "type": "state_update",
            "viewers": users,
            "designated_speaker": SESSION.get("designated_speaker"),
        }
    )


async def handle_assign_slot(viewer_token: str, slot: Optional[int]) -> None:
    """Assign a gamepad slot to a room member, or take theirs away.

    A slot can only be held by one member, so whoever held it before is
    unassigned first. The new token map is pushed to selkies and a
    `gamepad_change` notification is broadcast for every change made, followed
    by a state update. Unknown tokens are logged and ignored.

    Args:
        viewer_token: The token of the member to change; the controller's own
            token targets the controller.
        slot: The gamepad slot to assign, or None to unassign.
    """
    if SESSION is None:
        return
    target_user = None
    target_username = "Unknown"
    old_slot = None
    if viewer_token == SESSION["controller_token"]:
        target_user = SESSION
        target_username = "Controller"
        old_slot = SESSION.get("controller_slot")
    else:
        for v in SESSION.get("viewers", []):
            if v["token"] == viewer_token:
                target_user = v
                target_username = v.get("username", "Unnamed")
                old_slot = v.get("slot")
                break
    if not target_user:
        log.warning("assign_slot for unknown token")
        return

    notifications = []
    if slot is not None:
        cleared = False
        if (
            SESSION.get("controller_slot") == slot
            and SESSION["controller_token"] != viewer_token
        ):
            SESSION["controller_slot"] = None
            notifications.append(f"Controller was unassigned from Gamepad {slot}.")
            cleared = True
        if not cleared:
            for v in SESSION.get("viewers", []):
                if v.get("slot") == slot and v.get("token") != viewer_token:
                    v["slot"] = None
                    notifications.append(
                        f"{v.get('username', 'Unnamed')} was unassigned from Gamepad {slot}."
                    )
                    break

    if target_user is SESSION:
        SESSION["controller_slot"] = slot
    else:
        target_user["slot"] = slot

    if slot is not None and old_slot != slot:
        notifications.append(f"Gamepad {slot} was assigned to {target_username}.")
    elif slot is None and old_slot is not None:
        notifications.append(f"{target_username} was unassigned from Gamepad {old_slot}.")

    await selkies.push_tokens(SESSION)
    for msg in notifications:
        await broadcast_to_room(
            {"type": "gamepad_change", "message": msg, "timestamp": int(time.time() * 1000)}
        )
    await broadcast_state()


async def handle_assign_mk(target_token: Optional[str]) -> None:
    """Hand mouse and keyboard control to a room member.

    The controller's own token and None both mean control returns to the
    controller. A no-op when the target already holds it; otherwise the token
    map is pushed to selkies and an `mk_change` notification and a state update
    are broadcast.

    Args:
        target_token: The token of the viewer to receive control, or None (or
            the controller's token) to give it back to the controller.
    """
    if SESSION is None:
        return
    if target_token == SESSION["controller_token"]:
        target_token = None
    if SESSION.get("mk_owner_token") == target_token:
        return
    SESSION["mk_owner_token"] = target_token

    username = "Controller"
    if target_token:
        for v in SESSION.get("viewers", []):
            if v["token"] == target_token:
                username = v.get("username", "User")
                break

    await selkies.push_tokens(SESSION)
    await broadcast_to_room(
        {
            "type": "mk_change",
            "message": f"Mouse & Keyboard control assigned to {username}.",
            "timestamp": int(time.time() * 1000),
        }
    )
    await broadcast_state()


async def notify_session_ended() -> None:
    """Tell the room the session has ended and forget every connection."""
    await broadcast_to_room({"type": "session_ended"})
    ROOM["controller"] = None
    ROOM["viewers"] = {}
    ROOM["cooldowns"] = {}
