"""Room websocket: oversized-frame rejection, chat rate limiting, and video/audio/cursor state validation."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from webstation_broker import room, session, settings

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


def _invite(client: TestClient, controller_token: str, permission: str = "participant") -> str:
    """Take a seat through the controller's invite link and return the seat token."""
    response = client.post(
        f"{API}/session/invite?token={controller_token}", json={"permission": permission}
    )
    invite = response.json()["url"].split("invite=")[1]
    return client.get(f"{API}/session/context?invite={invite}").json()["userToken"]


def _wait_for_state(conn: WebSocketTestSession, predicate) -> dict:  # noqa: ANN001
    """Read room broadcasts until a state_update satisfies `predicate`, and return it."""
    while True:
        message = conn.receive_json()
        if message["type"] == "state_update" and predicate(message):
            return message


def test_an_invite_link_stays_valid_after_its_viewer_disconnects(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """An invite link stays valid after its viewer disconnects.

    The seat is minted once and lives for the whole session: a reload, a closed tab or the room's
    own reconnect must land back in the same seat rather than on a "session does not exist" page.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, viewer):
        pass

    assert session.find_viewer(viewer) is not None
    assert client.get(f"{API}/session/context?token={viewer}").status_code == 200
    with _connect(client, viewer) as conn:
        conn.send_json({"action": "video_state", "state": 1})
        assert conn.receive_json()["payload"]["state"] == 1


def test_a_disconnecting_viewer_releases_its_gamepad_but_keeps_its_seat(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A disconnecting viewer releases its gamepad but keeps its seat.

    A pad nobody is driving goes back to the tray so the host can hand it on, while the seat (and
    the link to it) survives for when that person comes back.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host, _connect(client, viewer) as guest:
        host.send_json({"action": "assign_slot", "viewer_token": viewer, "slot": 2})
        _wait_for_state(
            guest,
            lambda m: any(u["token"] == viewer and u["slot"] == 2 for u in m["viewers"]),
        )

    seat = session.find_viewer(viewer)
    assert seat is not None
    assert seat["slot"] is None


def test_a_seat_reclaimed_mid_handshake_gets_the_new_socket_closed(
    client: TestClient,
    broker_dirs: dict[str, Path],
    fake_emulator: list[FakeEmulator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seat reclaimed by a concurrent join while accept() is still pending closes the stale socket.

    accept() is an await point: a join at the room cap can reclaim this very seat while the
    handshake is in flight, so the seat is re-checked after accept() rather than trusting the lookup
    made before it.
    """
    monkeypatch.setattr(settings, "MAX_ROOM_VIEWERS", 1)
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    original_accept = room.WebSocket.accept

    async def accept_then_reclaim(self: object, *args: object, **kwargs: object) -> None:
        await original_accept(self, *args, **kwargs)
        # A second arrival at the cap goes through the real reclaim path in
        # add_viewer, evicting `viewer`'s seat: it is still the only anonymous,
        # not-yet-online candidate at this point in the handshake.
        session.add_viewer("participant", None)

    monkeypatch.setattr(room.WebSocket, "accept", accept_then_reclaim)

    with client.websocket_connect(f"{PREFIX}/ws/room?token={viewer}") as conn:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            conn.receive_json()
        assert excinfo.value.code == 1008


def test_a_new_connection_on_the_same_token_replaces_the_old_one(
    client: TestClient, broker_dirs: dict[str, Path], fake_emulator: list[FakeEmulator]
) -> None:
    """A new connection on the same token replaces the old one.

    The stale socket is closed and, since it was not the member leaving, it must not announce a
    departure or release what the live socket now holds.
    """
    controller = _activate(client, broker_dirs)
    viewer = _invite(client, controller)

    with _connect(client, controller) as host:
        with _connect(client, viewer) as first:
            with _connect(client, viewer) as second:
                with pytest.raises(WebSocketDisconnect):
                    first.receive_json()
                second.send_json({"action": "video_state", "state": 1})
                assert second.receive_json()["payload"]["state"] == 1

        # The host saw a join per socket but no departure from the replaced one.
        seen = []
        while not seen or seen[-1] != "control":
            seen.append(host.receive_json()["type"])
        assert seen.count("user_left") == 0
