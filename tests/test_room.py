"""Room websocket: oversized-frame rejection, chat rate limiting, and video/audio/cursor state validation."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from webstation_broker import room

from .conftest import PREFIX, FakeEmulator

API = f"{PREFIX}/api"


def _activate(client: TestClient, broker_dirs: dict[str, Path], **overrides: object) -> str:
    """Activate a session for the fake emulator and return the controller token."""
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
def _connect(client: TestClient, token: str) -> Iterator[WebSocketTestSession]:
    """Open the room socket and drain the join broadcasts every connect sends."""
    with client.websocket_connect(f"{PREFIX}/ws/room?token={token}") as conn:
        conn.receive_json()  # user_joined
        conn.receive_json()  # state_update
        yield conn


def test_an_oversized_text_frame_closes_the_connection(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An oversized text frame closes the connection.

    8 KiB is already comfortably above any legitimate text frame, so the server treats one oversized
    frame as abuse and closes rather than dropping it and leaving the connection open to a repeat
    flood.
    """
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_text("x" * (room.MAX_TEXT_FRAME_BYTES + 1))

        with pytest.raises(WebSocketDisconnect):
            conn.receive_json()


def test_a_chat_message_inside_the_cooldown_window_is_dropped(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat message inside the cooldown window is dropped."""
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


def test_the_chat_cooldown_survives_a_reconnect(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat cooldown survives a reconnect.

    The cooldown is keyed by token in session.ROOM["cooldowns"], not on the per-connection object,
    since otherwise disconnecting and reconnecting with the same token would hand out a fresh,
    unthrottled connection on every reconnect.
    """
    token = _activate(client, broker_dirs)
    clock = {"now": 1000.0}
    monkeypatch.setattr(room.time, "time", lambda: clock["now"])

    with _connect(client, token) as conn:
        conn.send_json({"action": "send_chat_message", "message": "first"})
        first = conn.receive_json()
        assert first["message"] == "first"

    clock["now"] += 0.1
    with _connect(client, token) as conn:
        conn.send_json({"action": "send_chat_message", "message": "too soon"})
        conn.send_json({"action": "video_state", "state": 1})
        sync = conn.receive_json()
        assert sync["payload"]["state"] == 1


def test_a_video_state_of_zero_or_one_is_forwarded(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A video state of zero or one is forwarded."""
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "video_state", "state": 1})
        message = conn.receive_json()

        assert message["type"] == "control"
        assert message["payload"]["state"] == 1


def test_a_json_boolean_state_is_forwarded(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A JSON boolean state is forwarded.

    JS booleans serialize as JSON true/false, which Python decodes as True/False -- these have to keep
    working since the room.js webcam/mic toggles send exactly this shape.
    """
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "audio_state", "state": True})
        message = conn.receive_json()

        assert message["payload"]["state"] is True


def test_an_out_of_range_state_value_is_dropped(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An out-of-range state value is dropped."""
    token = _activate(client, broker_dirs)
    with _connect(client, token) as conn:
        conn.send_json({"action": "video_state", "state": 2})
        # Nothing broadcasts for the bad value; a well-formed follow-up message
        # proves the connection is still alive and the bad one wasn't queued.
        conn.send_json({"action": "video_state", "state": 0})
        message = conn.receive_json()

        assert message["payload"]["state"] == 0
