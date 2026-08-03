"""Room websocket: presence, chat, webcam/mic binary fanout, resolution
negotiation, and input assignment.

Binary wire format:
  [0..7] 8-byte ASCII publicId of sender
  [8]    0x01 video frame / 0x02 audio frame / 0x03 video config / 0x04 pcm
  [9..]  payload
The publicId indirection keeps real tokens out of the media byte stream.
"""

import json
import logging
import secrets
import time

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import session

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/room")
async def room_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token")
    sess = session.SESSION
    if sess is None or not sess.get("active") or not token:
        await websocket.close(code=1008)
        return

    is_controller = token == sess["controller_token"]
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
        session.ROOM["viewers"][token] = connection_info
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
                    now = time.time()
                    if now - connection_info.get("last_username_change", 0) < 2.0:
                        continue
                    new_username = data.get("username", "").strip()
                    if new_username and 1 <= len(new_username) <= 25:
                        old_username = viewer_ref.get("username")
                        if old_username == new_username:
                            continue
                        viewer_ref["username"] = new_username
                        connection_info["last_username_change"] = now
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
                    text = data.get("message", "").strip()
                    if text and 1 <= len(text) <= 500:
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
        if is_controller:
            if session.ROOM.get("controller") is connection_info:
                session.ROOM["controller"] = None
        else:
            session.ROOM["viewers"].pop(token, None)
            sess = session.SESSION
            if sess is not None:
                if sess.get("designated_speaker") == token:
                    sess["designated_speaker"] = None
                disconnected = session.find_viewer(token)
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
                    if sess.get("mk_owner_token") == token:
                        sess["mk_owner_token"] = None
                        await session.broadcast_to_room(
                            {
                                "type": "mk_change",
                                "message": f"{disconnected.get('username', 'User')} disconnected. "
                                "MK control reverted to Controller.",
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                sess["viewers"] = [v for v in sess.get("viewers", []) if v["token"] != token]
                from . import selkies

                await selkies.push_tokens(sess)

        if connection_info.get("has_joined"):
            await session.broadcast_to_room(
                {
                    "type": "user_left",
                    "username": current_username,
                    "timestamp": int(time.time() * 1000),
                }
            )
        await session.broadcast_state()
