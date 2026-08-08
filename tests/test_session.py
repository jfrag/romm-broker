"""Session bookkeeping, viewer roster, and the selkies token map."""

import pytest

from webstation_broker import selkies, session

from .conftest import FakeEmulator


def _activate(session_id="sess-1", user=None):
    return session.new_session(
        {"session_id": session_id, "emulator": "fake", "user": user or {}, "rom": {"name": "Game"}},
        FakeEmulator(),
        "/romm/roms/ps2/Game.iso",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sess-1", "sess-1"),
        ("../../etc/passwd", "etcpasswd"),
        ("a b.c/d", "abcd"),
        ("Session_42", "Session_42"),
    ],
)
def test_the_session_id_keeps_nothing_that_could_shape_a_path(raw, expected):
    assert session._session_id(raw) == expected


def test_an_unusable_session_id_falls_back_to_a_generated_one():
    for raw in (None, "", "///", "..."):
        generated = session._session_id(raw)
        assert generated and generated.isalnum()


def test_the_session_id_is_bounded():
    assert len(session._session_id("x" * 500)) == 64


def test_activating_drops_the_record_of_the_session_that_just_exited():
    _activate()
    session.retire_session()
    assert session.LAST_EXIT is not None

    _activate("sess-2")

    assert session.LAST_EXIT is None


def test_retiring_keeps_the_emulator_the_state_routes_still_have_to_read():
    sess = _activate()
    emulator = sess["emulator_obj"]

    session.retire_session()

    assert session.SESSION is None
    assert session.LAST_EXIT["emulator_obj"] is emulator
    assert session.LAST_EXIT["id"] == "sess-1"


def test_a_viewer_gets_a_token_that_resolves_back_to_them():
    _activate()

    viewer = session.add_viewer("readonly", {"id": 7, "username": "ana"})

    assert session.find_viewer(viewer["token"]) == viewer
    assert viewer["permission"] == "readonly"
    assert viewer["username"] == "ana"


def test_rejoining_replaces_the_entry_and_kills_the_old_token():
    _activate()
    first = session.add_viewer("participant", {"id": 7, "username": "ana"})

    second = session.add_viewer("participant", {"id": 7, "username": "ana"})

    assert len(session.SESSION["viewers"]) == 1
    assert second["token"] != first["token"]
    assert session.find_viewer(first["token"]) is None


def test_a_rejoin_without_an_id_is_matched_on_the_name():
    _activate()
    session.add_viewer("participant", {"username": "ana"})

    session.add_viewer("participant", {"username": "ana"})

    assert len(session.SESSION["viewers"]) == 1


def test_two_different_users_both_keep_a_seat():
    _activate()
    session.add_viewer("participant", {"id": 7, "username": "ana"})
    session.add_viewer("participant", {"id": 8, "username": "bo"})

    assert len(session.SESSION["viewers"]) == 2


def test_an_anonymous_viewer_still_gets_a_name():
    _activate()

    viewer = session.add_viewer("participant", None)

    assert viewer["username"].startswith("User-")


def test_the_controller_holds_mouse_and_keyboard_until_it_is_handed_over():
    sess = _activate()
    viewer = session.add_viewer("participant", {"id": 7, "username": "ana"})

    tokens = selkies.build_token_map(sess)
    assert tokens[sess["controller_token"]]["mk_control"] is True
    assert tokens[viewer["token"]]["mk_control"] is False

    sess["mk_owner_token"] = viewer["token"]
    tokens = selkies.build_token_map(sess)
    assert tokens[sess["controller_token"]]["mk_control"] is False
    assert tokens[viewer["token"]]["mk_control"] is True


def test_the_token_map_carries_the_gamepad_slot_selkies_routes_on():
    sess = _activate()
    viewer = session.add_viewer("participant", {"id": 7, "username": "ana"})
    viewer["slot"] = 2

    tokens = selkies.build_token_map(sess)

    assert tokens[sess["controller_token"]]["slot"] == 1
    assert tokens[viewer["token"]] == {"role": "viewer", "slot": 2, "mk_control": False}
