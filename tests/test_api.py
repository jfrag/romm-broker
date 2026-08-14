"""The HTTP surface RomM drives the container through."""

import io
import signal
import zipfile

import pytest
from fastapi.testclient import TestClient

from webstation_broker import session, settings
from webstation_broker.app import create_app
from webstation_broker.emulators import base

from .conftest import PREFIX

API = f"{PREFIX}/api"


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _rom(broker_dirs, name="Game.iso"):
    rom = broker_dirs["roms"] / name
    rom.write_bytes(b"iso")
    return rom


def _activate(client, broker_dirs, **overrides):
    body = {
        "session_id": "sess-1",
        "emulator": "fake",
        "user": {"id": 1, "username": "ana", "display_name": "Ana"},
        "rom": {"id": 5, "name": "Game", "platform": "ps2", "path": str(_rom(broker_dirs))},
    }
    body.update(overrides)
    return client.post(f"{API}/session/activate", json=body)


def test_health_answers_without_a_secret(client):
    assert client.get(f"{API}/health").json() == {"status": "ok"}


def test_status_is_inactive_before_anything_runs(client):
    assert client.get(f"{API}/session/status").json() == {"active": False}


def test_a_wrong_secret_is_refused(secret_client, broker_dirs):
    response = _activate(secret_client, broker_dirs)

    assert response.status_code == 403


def test_the_right_secret_gets_through(secret_client, broker_dirs, fake_emulator):
    secret_client.headers["X-Broker-Secret"] = "s3cret"

    assert _activate(secret_client, broker_dirs).status_code == 200


def test_activate_launches_the_rom_and_hands_back_a_landing_url(
    client, broker_dirs, fake_emulator
):
    response = _activate(client, broker_dirs)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "launching"
    assert body["rom_file"] == str(broker_dirs["roms"] / "Game.iso")
    assert body["url"].startswith(f"{PREFIX}/?token=")
    emulator = fake_emulator[0]
    assert emulator.launched == (broker_dirs["roms"] / "Game.iso", None)
    # The slot is emptied before the restore so the incoming archive, not the
    # last session, decides what the emulator resumes from.
    assert emulator.cleared is True


def test_activate_refuses_a_second_session_over_a_running_one(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)

    assert _activate(client, broker_dirs).status_code == 409


def test_activate_refuses_an_emulator_that_is_not_installed(client, broker_dirs):
    assert _activate(client, broker_dirs, emulator="gameboy").status_code == 422


def test_activate_refuses_a_rom_outside_the_library(client, broker_dirs, fake_emulator, tmp_path):
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"iso")

    response = _activate(
        client, broker_dirs, rom={"path": str(outside), "platform": "ps2"}
    )

    assert response.status_code == 400


def test_activate_reports_a_rom_that_is_not_there(client, broker_dirs, fake_emulator):
    response = _activate(
        client,
        broker_dirs,
        rom={"path": str(broker_dirs["roms"] / "gone.iso"), "platform": "ps2"},
    )

    assert response.status_code == 404


def test_activate_reports_a_folder_holding_nothing_bootable(
    client, broker_dirs, fake_emulator
):
    folder = broker_dirs["roms"] / "game"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"nope")

    response = _activate(client, broker_dirs, rom={"path": str(folder), "platform": "ps2"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "no bootable file found"


def test_activate_restores_the_save_archive_it_is_pointed_at(
    client, broker_dirs, fake_emulator, tmp_path
):
    archive = tmp_path / "incoming.zip"
    archive.write_bytes(_zip({"saves/card.bin": b"restored"}))

    response = _activate(client, broker_dirs, save={"archive": str(archive)})

    assert response.json()["save_restore"]["written"] == 1
    root = fake_emulator[0].save_root
    assert (root / "saves" / "card.bin").read_bytes() == b"restored"


def test_activate_refuses_a_save_archive_that_is_not_there(
    client, broker_dirs, fake_emulator, tmp_path
):
    response = _activate(client, broker_dirs, save={"archive": str(tmp_path / "gone.zip")})

    assert response.status_code == 404


def test_activate_rejects_an_archive_it_cannot_unpack(
    client, broker_dirs, fake_emulator, tmp_path
):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"not a zip")

    assert _activate(client, broker_dirs, save={"archive": str(archive)}).status_code == 422


def test_status_reports_what_romm_reads_off_the_running_session(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)

    body = client.get(f"{API}/session/status").json()

    assert body["active"] is True
    assert body["session_id"] == "sess-1"
    assert body["emulator"] == "fake"
    assert body["emulator_alive"] is True
    assert body["boot_failed"] is False
    assert body["supports_states"] is True
    assert body["state_slot"] == 3


def test_joining_hands_the_viewer_their_own_landing_url(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)

    body = client.post(
        f"{API}/session/join",
        json={"user": {"id": 7, "username": "bo"}, "permission": "readonly"},
    ).json()

    assert body["username"] == "bo"
    assert body["url"] != f"{PREFIX}/?token={session.SESSION['controller_token']}"


def test_joining_nothing_is_a_conflict(client):
    response = client.post(f"{API}/session/join", json={"permission": "participant"})

    assert response.status_code == 409


def test_an_unknown_permission_is_refused(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)

    response = client.post(f"{API}/session/join", json={"permission": "admin"})

    assert response.status_code == 422


def test_a_state_save_resolves_to_the_emulator_working_slot(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)

    body = client.post(f"{API}/session/save-state", json={"slot": 7}).json()

    assert body == {"status": "saved", "slot": 3, "saved": True}
    # The requested slot is passed through, but the emulator is the authority
    # on where it actually lands.
    assert fake_emulator[0].saved_slots == [7]


def test_a_state_load_answers_with_the_same_working_slot(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)

    body = client.post(f"{API}/session/load-state", json={"slot": 0}).json()

    assert body == {"status": "loaded", "slot": 3, "loaded": True}


def test_a_state_route_with_no_session_is_a_conflict(client):
    assert client.post(f"{API}/session/save-state", json={"slot": 1}).status_code == 409


def test_a_state_route_refuses_a_slot_outside_the_accepted_range(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)

    assert client.post(f"{API}/session/save-state", json={"slot": 99}).status_code == 422


def test_a_state_route_refuses_an_emulator_that_died(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)
    fake_emulator[0].running = False

    assert client.post(f"{API}/session/save-state", json={"slot": 1}).status_code == 409


def test_the_state_file_is_served_with_the_name_and_slot_romm_files_it_under(
    client, broker_dirs, fake_emulator, tmp_path
):
    _activate(client, broker_dirs)
    state = tmp_path / "GAME.03.p2s"
    state.write_bytes(b"state bytes")
    fake_emulator[0].state_file = state

    response = client.get(f"{API}/session/state-file")

    assert response.content == b"state bytes"
    assert response.headers["X-State-Filename"] == "GAME.03.p2s"
    assert response.headers["X-State-Slot"] == "3"


def test_an_empty_working_slot_serves_no_state_file(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)

    assert client.get(f"{API}/session/state-file").status_code == 404


def test_a_pushed_state_lands_under_the_name_the_emulator_chose(
    client, broker_dirs, fake_emulator, tmp_path
):
    _activate(client, broker_dirs)
    target = tmp_path / "GAME.03.p2s"
    fake_emulator[0].state_file = target

    response = client.put(
        f"{API}/session/state-file", params={"filename": "GAME.01.p2s"}, content=b"pushed"
    )

    assert response.json() == {"status": "ok", "filename": "GAME.03.p2s", "slot": 3}
    assert target.read_bytes() == b"pushed"


def test_a_pushed_state_the_emulator_would_never_write_is_refused(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)
    fake_emulator[0].state_file = None

    response = client.put(
        f"{API}/session/state-file", params={"filename": "junk.bin"}, content=b"pushed"
    )

    assert response.status_code == 400


def test_an_empty_state_push_leaves_nothing_behind(
    client, broker_dirs, fake_emulator, tmp_path
):
    _activate(client, broker_dirs)
    target = tmp_path / "GAME.03.p2s"
    fake_emulator[0].state_file = target

    response = client.put(
        f"{API}/session/state-file", params={"filename": "GAME.01.p2s"}, content=b""
    )

    assert response.status_code == 400
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_the_memory_card_routes_refuse_an_emulator_without_one(client, fake_emulator):
    response = client.get(f"{API}/session/memory-card", params={"emulator": "fake"})

    assert response.status_code == 400


def test_the_memory_card_routes_refuse_an_emulator_that_is_not_installed(client):
    response = client.get(f"{API}/session/memory-card", params={"emulator": "gameboy"})

    assert response.status_code == 422


def test_an_empty_card_push_is_refused(client, fake_emulator):
    response = client.put(
        f"{API}/session/memory-card", params={"emulator": "fake"}, content=b""
    )

    assert response.status_code == 400


def test_an_import_is_stored_under_the_path_activate_takes(client, broker_dirs):
    body = client.put(f"{API}/session/imports/sess-1.zip", content=_zip({"a": b"x"})).json()

    assert body["path"] == str(broker_dirs["imports"] / "sess-1.zip")
    assert body["status"] == "stored"


def test_an_import_that_is_not_a_zip_is_refused(client):
    response = client.put(f"{API}/session/imports/sess-1.zip", content=b"not a zip")

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["sess-1.tar", ".hidden.zip"])
def test_an_import_that_is_not_a_plain_zip_name_is_refused(client, name):
    response = client.put(f"{API}/session/imports/{name}", content=_zip({"a": b"x"}))

    assert response.status_code == 400


@pytest.mark.parametrize("name", ["../escape.zip", "sub/dir.zip", "/abs.zip", ""])
def test_an_archive_name_carrying_path_structure_is_refused(name):
    from fastapi import HTTPException

    from webstation_broker.api import _archive_name

    with pytest.raises(HTTPException) as raised:
        _archive_name(name)
    assert raised.value.status_code == 400


def test_exports_list_newest_first(client, broker_dirs):
    import os

    old = broker_dirs["exports"] / "old.zip"
    new = broker_dirs["exports"] / "new.zip"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1000, 1000))
    os.utime(new, (3000, 3000))

    names = [e["name"] for e in client.get(f"{API}/session/exports").json()["exports"]]

    assert names == ["new.zip", "old.zip"]


def test_an_export_downloads_and_then_deletes(client, broker_dirs):
    (broker_dirs["exports"] / "sess-1.zip").write_bytes(b"archive")

    assert client.get(f"{API}/session/exports/sess-1.zip").content == b"archive"
    assert client.delete(f"{API}/session/exports/sess-1.zip").json()["status"] == "deleted"
    assert not (broker_dirs["exports"] / "sess-1.zip").exists()


def test_an_export_that_was_never_written_is_a_404(client):
    assert client.get(f"{API}/session/exports/gone.zip").status_code == 404


def test_context_resolves_the_controller_token(client, broker_dirs, fake_emulator):
    token = _activate(client, broker_dirs).json()["url"].split("token=")[1]

    body = client.get(f"{API}/session/context", params={"token": token}).json()

    assert body["userRole"] == "controller"
    assert body["username"] == "Ana"
    assert body["gameName"] == "Game"
    assert body["iframeSrc"] == f"stream/?token={token}"


def test_context_resolves_a_viewer_token_to_their_permission(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)
    url = client.post(
        f"{API}/session/join",
        json={"user": {"id": 7, "username": "bo"}, "permission": "readonly"},
    ).json()["url"]

    body = client.get(
        f"{API}/session/context", params={"token": url.split("token=")[1]}
    ).json()

    assert body["userRole"] == "viewer"
    assert body["userPermission"] == "readonly"


@pytest.mark.parametrize("params", [{}, {"token": "made-up"}])
def test_context_refuses_a_token_it_cannot_place(client, broker_dirs, fake_emulator, params):
    _activate(client, broker_dirs)

    assert client.get(f"{API}/session/context", params=params).status_code == 401


def test_exit_dumps_the_save_delta_and_retires_the_session(
    client, broker_dirs, fake_emulator, monkeypatch
):
    monkeypatch.setattr(settings, "DEV_MODE", True)
    _activate(client, broker_dirs)
    (fake_emulator[0].save_root / "saves" / "card.bin").write_bytes(b"played")

    body = client.post(f"{API}/session/exit").json()

    assert body["status"] == "exited"
    assert [f["path"] for f in body["save_dump"]["files"]] == ["saves/card.bin"]
    assert body["upload"]["mode"] == "report-only"
    assert fake_emulator[0].running is False
    assert session.SESSION is None


def test_exit_leaves_the_state_readable_for_the_pull_that_follows(
    client, broker_dirs, fake_emulator, tmp_path
):
    _activate(client, broker_dirs)
    state = tmp_path / "GAME.03.p2s"
    state.write_bytes(b"final state")
    fake_emulator[0].state_file = state

    client.post(f"{API}/session/exit")

    # RomM comes back for the exit state after the teardown has answered, so
    # the retired emulator has to keep serving it.
    assert client.get(f"{API}/session/state-file").content == b"final state"


def test_the_controller_token_is_enough_to_exit(secret_client, broker_dirs, fake_emulator):
    secret_client.headers["X-Broker-Secret"] = "s3cret"
    token = _activate(secret_client, broker_dirs).json()["url"].split("token=")[1]
    del secret_client.headers["X-Broker-Secret"]

    response = secret_client.post(f"{API}/session/exit", params={"token": token})

    assert response.status_code == 200


def test_a_stranger_cannot_exit_someone_else_session(
    secret_client, broker_dirs, fake_emulator
):
    secret_client.headers["X-Broker-Secret"] = "s3cret"
    _activate(secret_client, broker_dirs)
    del secret_client.headers["X-Broker-Secret"]

    response = secret_client.post(f"{API}/session/exit", params={"token": "made-up"})

    assert response.status_code == 403
    assert session.SESSION is not None


def test_exit_with_save_off_writes_no_state_but_still_dumps_the_saves(
    client, broker_dirs, fake_emulator, monkeypatch
):
    """A player leaving without saving gets no state written, and the game's
    own save data still travels: that progress is theirs either way."""
    monkeypatch.setattr(settings, "DEV_MODE", True)
    _activate(client, broker_dirs)
    (fake_emulator[0].save_root / "saves" / "card.bin").write_bytes(b"played")

    body = client.post(f"{API}/session/exit", params={"save": "false"}).json()

    assert fake_emulator[0].exit_slots == [None]
    assert body["state_saved"] is False
    assert body["state_slot"] is None
    assert [f["path"] for f in body["save_dump"]["files"]] == ["saves/card.bin"]


def test_exit_carries_slot_zero_rather_than_falling_back_to_the_default(
    client, broker_dirs, fake_emulator
):
    """Slot 0 is a real slot here, so a request asking for it has to arrive as
    0 and not be mistaken for "nothing was asked for"."""
    _activate(client, broker_dirs)

    client.post(f"{API}/session/exit", params={"slot": 0})

    assert fake_emulator[0].exit_slots == [0]


@pytest.mark.parametrize("prefix", [PREFIX, ""])
def test_starting_the_app_reaps_an_emulator_an_earlier_broker_left(
    prefix, pid_record, sleeper, monkeypatch
):
    """Exit answers 409 with no session, so a broker that comes back to an
    orphan can only kill it at startup. Both app shapes are checked because
    Starlette hands the lifespan to the served app, never to a mounted one."""
    monkeypatch.setattr(settings, "PREFIX", prefix)
    proc = sleeper()
    base._record_pid("fake", proc.pid, ["/usr/bin/sleep", "60"])

    with TestClient(create_app()):
        pass

    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert not pid_record.exists()


def test_exiting_nothing_is_a_conflict(client):
    assert client.post(f"{API}/session/exit").status_code == 409


def test_activate_records_a_multiplayer_session(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs, multiplayer=True)

    assert session.SESSION["multiplayer"] is True


def test_activate_defaults_to_a_solo_session(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)

    assert session.SESSION["multiplayer"] is False


def test_context_tells_the_room_which_kind_of_session_it_is(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs, multiplayer=True)
    token = session.SESSION["controller_token"]

    body = client.get(f"{API}/session/context", params={"token": token}).json()

    assert body["multiplayer"] is True


def test_context_reports_a_solo_session_as_such(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]

    body = client.get(f"{API}/session/context", params={"token": token}).json()

    assert body["multiplayer"] is False


def test_the_controller_can_mint_an_invite_link(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]

    body = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "participant"},
    ).json()

    assert body["url"] != f"{PREFIX}/?token={token}"
    assert len(session.SESSION["viewers"]) == 1


def test_an_invite_works_on_a_solo_session(client, broker_dirs, fake_emulator):
    """The switch governs discovery and comms, never the link."""
    _activate(client, broker_dirs, multiplayer=False)
    token = session.SESSION["controller_token"]

    response = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "participant"},
    )

    assert response.status_code == 200


def test_a_viewer_cannot_mint_invites(client, broker_dirs, fake_emulator):
    _activate(client, broker_dirs)
    viewer = client.post(
        f"{API}/session/join", json={"permission": "participant"}
    ).json()
    viewer_token = session.SESSION["viewers"][0]["token"]
    assert viewer["username"]

    response = client.post(
        f"{API}/session/invite",
        params={"token": viewer_token},
        json={"permission": "participant"},
    )

    assert response.status_code == 403


def test_inviting_into_nothing_is_a_conflict(client):
    response = client.post(
        f"{API}/session/invite",
        params={"token": "whatever"},
        json={"permission": "participant"},
    )

    assert response.status_code == 409


def test_an_invite_with_an_unknown_permission_is_refused(
    client, broker_dirs, fake_emulator
):
    _activate(client, broker_dirs)
    token = session.SESSION["controller_token"]

    response = client.post(
        f"{API}/session/invite",
        params={"token": token},
        json={"permission": "admin"},
    )

    assert response.status_code == 422
