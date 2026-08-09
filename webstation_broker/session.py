"""Single-session state and room fanout.

The container hosts exactly one play session at a time, so session and room
state are module globals.
"""

import asyncio
import logging
import re
import secrets
import time
from typing import Optional

from starlette.websockets import WebSocket, WebSocketState

from . import selkies

log = logging.getLogger(__name__)

# The active play session, or None. Shape:
# {
#   "id", "active", "created_at",
#   "user": {...}, "emulator": "pcsx2", "rom": {...}, "rom_file": str,
#   "save": {...} | None, "callback": {...} | None, "multiplayer": bool,
#   "controller_token",
#   "viewers": [{"token","slot","mk_control","username","permission"}...],
#   "controller_slot", "mk_owner_token", "designated_speaker",
#   "save_baseline": float, "emulator_obj": Emulator,
# }
SESSION: Optional[dict] = None

# What is left of the session that just exited: {"id", "rom", "emulator_obj"}.
# RomM files the exit state in its library after the teardown has answered, so
# the emulator that captured it has to outlive the session for the read routes
# to find the file. Dropped at the next activate.
LAST_EXIT: Optional[dict] = None

ROOM: dict = {"controller": None, "viewers": {}}


def _session_id(raw) -> str:
    """A caller-supplied id, reduced to what is safe in the export filename.

    The exit archive is named after the session, and the export routes reject
    anything with path structure in it, so an id carrying a slash or a dot
    would produce an archive the parent could never fetch back.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:64]
    return cleaned or secrets.token_hex(8)


def new_session(payload: dict, emulator_obj, rom_file: str) -> dict:
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


def find_viewer(token: str) -> Optional[dict]:
    if SESSION is None:
        return None
    return next((v for v in SESSION.get("viewers", []) if v["token"] == token), None)


def add_viewer(permission: str, user: Optional[dict] = None) -> dict:
    """Mint a viewer token. A re-join by the same user (matched by id, else
    username) replaces the old entry and invalidates its token."""
    import random

    user = user or {}
    username = user.get("display_name") or user.get("username")

    def _same_user(v: dict) -> bool:
        if user.get("id") is not None and v.get("user_id") is not None:
            return v["user_id"] == user["id"]
        return bool(username) and v.get("username") == username

    SESSION["viewers"] = [v for v in SESSION.get("viewers", []) if not _same_user(v)]

    viewer = {
        "token": secrets.token_urlsafe(16),
        "user_id": user.get("id"),
        "slot": None,
        "mk_control": False,
        "username": username or f"User-{random.randint(100, 999)}",
        "permission": permission,
    }
    SESSION.setdefault("viewers", []).append(viewer)
    return viewer


async def broadcast_to_room(payload: dict) -> None:
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
    await broadcast_to_room({"type": "session_ended"})
    ROOM["controller"] = None
    ROOM["viewers"] = {}
