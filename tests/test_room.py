"""Room websocket: oversized-frame rejection, chat rate limiting, and
state-flag validation on the video/audio/cursor control messages."""

from contextlib import contextmanager

from webstation_broker import room

from .conftest import PREFIX

API = f"{PREFIX}/api"


def _activate(client, broker_dirs, **overrides):
    rom = broker_dirs["roms"] / "Game.iso"
    rom.write_bytes(b"iso")
    body = {
        "session_id": "sess-1",
        "emulator": "fake",
        "user": {"id": 1, "username": "ana", "display_name": "Ana"},
        "rom": {"id": 5, "name": "Game", "platform": "ps2", "path": str(rom)},
    }
    body.update(overrides)
    response = client.post(f"{API}/session/activate", json=body)
    return response.json()["url"].split("token=")[1]


@contextmanager
def _connect(client, token):
    """Open the room socket and drain the join broadcasts every connect sends."""
    with client.websocket_connect(f"{PREFIX}/ws/room?token={token}") as conn:
        conn.receive_json()  # user_joined
        conn.receive_json()  # state_update
        yield conn


def test_an_oversized_text_frame_is_dropped_without_breaking_the_connection(
    client, broker_dirs, fake_emulator
):
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_text("x" * (room.MAX_TEXT_FRAME_BYTES + 1))
        conn.send_json({"action": "send_chat_message", "message": "still alive"})

        message = conn.receive_json()

        assert message["type"] == "chat_message"
        assert message["message"] == "still alive"


def test_a_chat_message_inside_the_cooldown_window_is_dropped(
    client, broker_dirs, fake_emulator, monkeypatch
):
    token = _activate(client, broker_dirs)
    clock = {"now": 1000.0}
    monkeypatch.setattr(room.time, "time", lambda: clock["now"])
    with _connect(client, token) as conn:
        conn.send_json({"action": "send_chat_message", "message": "first"})
        first = conn.receive_json()
        assert first["message"] == "first"

        clock["now"] += 0.1
        conn.send_json({"action": "send_chat_message", "message": "too soon"})
        # The room reads the clock when it actually processes a message, not
        # when the test sends one, so a harmless message the server does
        # reply to forces it to have handled "too soon" before the clock
        # moves again -- otherwise both sends race the same clock value.
        conn.send_json({"action": "video_state", "state": 1})
        sync = conn.receive_json()
        assert sync["payload"]["state"] == 1

        clock["now"] += room.CHAT_COOLDOWN_SECONDS
        conn.send_json({"action": "send_chat_message", "message": "after cooldown"})
        second = conn.receive_json()

        assert second["message"] == "after cooldown"


def test_a_video_state_of_zero_or_one_is_forwarded(client, broker_dirs, fake_emulator):
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "video_state", "state": 1})
        message = conn.receive_json()

        assert message["type"] == "control"
        assert message["payload"]["state"] == 1


def test_a_json_boolean_state_is_forwarded(client, broker_dirs, fake_emulator):
    """JS booleans serialize as JSON true/false, which Python decodes as
    True/False -- these have to keep working since the room.js webcam/mic
    toggles send exactly this shape."""
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "audio_state", "state": True})
        message = conn.receive_json()

        assert message["payload"]["state"] is True


def test_an_out_of_range_state_value_is_dropped(client, broker_dirs, fake_emulator):
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "video_state", "state": 2})
        # Nothing broadcasts for the bad value; a well-formed follow-up message
        # proves the connection is still alive and the bad one wasn't queued.
        conn.send_json({"action": "video_state", "state": 0})
        message = conn.receive_json()

        assert message["payload"]["state"] == 0
