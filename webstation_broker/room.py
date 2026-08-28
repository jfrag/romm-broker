"""Room websocket: presence, chat, webcam/mic binary fanout, resolution negotiation, and input assignment.

Binary wire format:

```
[0..7] 8-byte ASCII publicId of sender
[8]    0x01 video frame / 0x02 audio frame / 0x03 video config / 0x04 pcm
[9..]  payload
```

The publicId indirection keeps real tokens out of the media byte stream.
"""

import json
import logging
import secrets
import time

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import session
from .api import _ct_eq

log = logging.getLogger(__name__)
router = APIRouter()

# Comfortably above any legitimate text frame (chat caps at 500 chars,
# username at 25); rejects a flood of oversized frames before json.loads
# ever runs on them.
MAX_TEXT_FRAME_BYTES = 8 * 1024

CHAT_COOLDOWN_SECONDS = 1.0


@router.websocket("/ws/room")
async def room_websocket(websocket: WebSocket) -> None:
    """Join the play session's room and relay its presence, chat, control and media traffic.

    The `token` query parameter must be the session's controller token or a
    minted viewer token, otherwise the socket is closed with 1008. Text frames
    are JSON actions: the controller may assign gamepad slots, mouse/keyboard
    control and the designated speaker and request resolutions; viewers may
    rename themselves (rate limited to one change per two seconds); anyone may
    chat, report their resolution and send video/audio control messages. Binary
    frames are media in the module's wire format and are fanned out to the
    other members, except from read-only viewers, frames over 1 MiB, and audio
    from anyone but the designated speaker while one is set.

    On disconnect the member is removed from the room; a departing viewer also
    loses its slot, mouse/keyboard ownership and speaker role, and the token
    map is pushed to selkies when it held either. The seat itself stays in
    the session, so its token (and the invite link carrying it) keeps working
    until the session ends; a new connection on a token that is already
    connected replaces the old socket.

    Args:
        websocket: The incoming room connection.
    """
    token = websocket.query_params.get("token")
    sess = session.SESSION
    if sess is None or not sess.get("active") or not token:
        await websocket.close(code=1008)
        return

    is_controller = _ct_eq(token, sess["controller_token"])
    viewer_ref = session.find_viewer(token)
    is_viewer = viewer_ref is not None
    if not is_controller and not is_viewer:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    username = "Controller"
    if is_controller:
        username = (sess.get("user") or {}).get("display_name") or "Controller"
    elif is_viewer:
        username = viewer_ref.get("username", f"User-{token[:6]}")

    connection_info = {
        "websocket": websocket,
        "username": username,
        "token": token,
        "public_id": secrets.token_hex(4),
        "has_joined": False,
    }
    if is_controller:
        session.ROOM["controller"] = connection_info
    else:
        # A seat lives for the whole session, so the same token can come back
        # after a reload or from another device. The earlier socket, if any,
        # is a leftover of that and is closed rather than left to share the seat.
        previous = session.ROOM["viewers"].get(token)
        session.ROOM["viewers"][token] = connection_info
        if previous is not None:
            try:
                await previous["websocket"].close(code=1000)
            except Exception:
                log.debug("stale room socket for %s was already gone", username)
    await session.broadcast_to_room(
        {"type": "user_joined", "username": username, "timestamp": int(time.time() * 1000)}
    )
    connection_info["has_joined"] = True
    await session.broadcast_state()

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message:
                if len(message["text"]) > MAX_TEXT_FRAME_BYTES:
                    # 8 KiB is already comfortably above any legitimate text
                    # frame, so one oversized frame is abuse, not a benign
                    # edge case -- close instead of dropping and letting an
                    # attacker hold the connection open for a repeat flood.
                    log.warning("room websocket: oversized text frame from %s, closing", username)
                    await websocket.close(code=1009)
                    return
                data = json.loads(message["text"])
                action = data.get("action")
                data["sender_token"] = token

                if action == "assign_slot" and is_controller:
                    await session.handle_assign_slot(data.get("viewer_token"), data.get("slot"))

                elif action == "assign_mk" and is_controller:
                    await session.handle_assign_mk(data.get("token"))

                elif action == "set_designated_speaker" and is_controller:
                    sess["designated_speaker"] = data.get("token")
                    await session.broadcast_state()

                elif action == "set_username" and is_viewer:
                    # Keyed by token in session.ROOM["cooldowns"], not on
                    # connection_info: a fresh WebSocket reconnect gets a new
                    # connection_info every time, which would otherwise reset
                    # the cooldown for free.
                    cooldowns = session.ROOM["cooldowns"].setdefault(token, {})
                    now = time.time()
                    if now - cooldowns.get("username", 0) < 2.0:
                        continue
                    new_username = data.get("username", "").strip()
                    if new_username and 1 <= len(new_username) <= 25:
                        old_username = viewer_ref.get("username")
                        if old_username == new_username:
                            continue
                        viewer_ref["username"] = new_username
                        cooldowns["username"] = now
                        connection_info["username"] = new_username
                        username = new_username
                        await session.broadcast_to_room(
                            {
                                "type": "username_changed",
                                "old_username": old_username,
                                "new_username": new_username,
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                        await session.broadcast_state()

                elif action == "send_chat_message":
                    # Same reconnect-proof keying as set_username above.
                    cooldowns = session.ROOM["cooldowns"].setdefault(token, {})
                    now = time.time()
                    if now - cooldowns.get("chat", 0) < CHAT_COOLDOWN_SECONDS:
                        continue
                    text = data.get("message", "").strip()
                    if text and 1 <= len(text) <= 500:
                        cooldowns["chat"] = now
                        await session.broadcast_to_room(
                            {
                                "type": "chat_message",
                                "sender": username,
                                "message": text,
                                "timestamp": int(time.time() * 1000),
                                "messageId": f"{int(time.time() * 1000)}-{secrets.token_hex(4)}",
                                "replyTo": data.get("replyTo"),
                            }
                        )

                elif action in ("video_state", "audio_state", "force_cursor_render"):
                    # Self-reported (webcam/mic) or self-triggered (gaming-mode
                    # cursor baking by a non-controller viewer, so this can't be
                    # gated to the controller); sender_token above already stops
                    # a client from spoofing another user's identity in it. The
                    # only thing left to enforce is that state is actually a
                    # boolean flag, not an arbitrary value forwarded verbatim
                    # into the Selkies input channel.
                    if data.get("state") in (0, 1):
                        await session.broadcast_to_room({"type": "control", "payload": data})

                elif action == "request_resolutions" and is_controller:
                    await session.broadcast_to_room({"type": "request_resolutions"})

                elif action == "client_resolution":
                    width, height = data.get("width"), data.get("height")
                    if width and height:
                        await session.broadcast_to_room(
                            {
                                "type": "resolution_update",
                                "token": token,
                                "width": width,
                                "height": height,
                            }
                        )

            elif "bytes" in message:
                binary_data = message["bytes"]
                if is_viewer and viewer_ref.get("permission") == "readonly":
                    continue
                if len(binary_data) > 1024 * 1024:
                    continue
                designated = sess.get("designated_speaker")
                is_audio = len(binary_data) > 8 and binary_data[8] == 0x02
                if designated and is_audio and token != designated:
                    continue
                await session.broadcast_binary_to_room(binary_data, websocket)

    except (WebSocketDisconnect, RuntimeError):
        log.info("room websocket disconnected for %s", username)
    except Exception:
        log.exception("unhandled room websocket error for %s", username)
    finally:
        current_username = connection_info.get("username")
        # A socket that was replaced by a newer one on the same token is not
        # the member leaving, so it neither cleans up nor announces a departure.
        was_live = (
            session.ROOM.get("controller") is connection_info
            if is_controller
            else session.ROOM["viewers"].get(token) is connection_info
        )
        if is_controller:
            if was_live:
                session.ROOM["controller"] = None
        elif was_live:
            # Only the live connection for the seat cleans up; a socket that
            # was replaced by a newer one on the same token must not release
            # what the newer one now holds. The seat itself stays: its invite
            # link is valid for the life of the session, and it only gives up
            # the input it was holding, since a pad nobody is driving should
            # go back to the tray.
            session.ROOM["viewers"].pop(token, None)
            sess = session.SESSION
            if sess is not None:
                if sess.get("designated_speaker") == token:
                    sess["designated_speaker"] = None
                disconnected = session.find_viewer(token)
                input_released = False
                if disconnected:
                    if disconnected.get("slot"):
                        await session.broadcast_to_room(
                            {
                                "type": "gamepad_change",
                                "message": f"{disconnected.get('username', 'A user')} disconnected "
                                f"and was unassigned from Gamepad {disconnected['slot']}.",
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                        disconnected["slot"] = None
                        input_released = True
                    if sess.get("mk_owner_token") == token:
                        sess["mk_owner_token"] = None
                        disconnected["mk_control"] = False
                        input_released = True
                        await session.broadcast_to_room(
                            {
                                "type": "mk_change",
                                "message": f"{disconnected.get('username', 'User')} disconnected. "
                                "MK control reverted to Controller.",
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                if input_released:
                    from . import selkies

                    await selkies.push_tokens(sess)

        if was_live and connection_info.get("has_joined"):
            await session.broadcast_to_room(
                {
                    "type": "user_left",
                    "username": current_username,
                    "timestamp": int(time.time() * 1000),
                }
            )
        await session.broadcast_state()
