"""PPSSPP ROM resolution, state naming, and window selection."""

import os
from pathlib import Path

import pytest

from webstation_broker.emulators import ppsspp


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(ppsspp, "ROM_ROOT", root)
    return root


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    d = tmp_path / "PPSSPP_STATE"
    d.mkdir()
    monkeypatch.setattr(ppsspp, "STATE_DIR", d)
    return d


def _touch(path: Path, mtime=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_rom_pick_prefers_the_compressed_image_beside_the_raw_one(rom_root):
    game = rom_root / "game"
    _touch(game / "Game.iso")
    _touch(game / "Game.chd")

    assert ppsspp._pick_rom_file(game.glob("*"), game).name == "Game.chd"


def test_rom_pick_ignores_unbootable_and_hidden_files(rom_root):
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.iso")

    assert ppsspp._pick_rom_file(game.glob("*"), game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root, tmp_path):
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert ppsspp._pick_rom_file(game.glob("*"), game) is None


def test_resolve_takes_a_file_as_given(rom_root):
    rom = _touch(rom_root / "Game.iso")

    assert ppsspp.Ppsspp().resolve_rom_file(rom) == rom


def test_resolve_searches_one_level_into_a_folder(rom_root):
    _touch(rom_root / "game" / "inner" / "Game.iso")

    resolved = ppsspp.Ppsspp().resolve_rom_file(rom_root / "game")

    assert resolved.name == "Game.iso"


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root):
    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "gone") is None


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root, tmp_path
):
    outside = tmp_path / "elsewhere.iso"
    outside.write_bytes(b"iso")
    linked = rom_root / "Game.iso"
    linked.symlink_to(outside)

    assert ppsspp.Ppsspp().resolve_rom_file(linked) is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ULUS10041_1_1.ppst", "ULUS10041_1_7.ppst"),
        ("ULUS10041_1_9.ppst", "ULUS10041_1_7.ppst"),
    ],
)
def test_restamp_keeps_the_game_and_rewrites_the_slot(filename, expected):
    assert ppsspp._restamp_slot(filename, 7) == expected


@pytest.mark.parametrize(
    "filename",
    ["ULUS10041.ppst", "ULUS10041_1_1.jpg", "", "a/b_1.ppst"],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename):
    assert ppsspp._restamp_slot(filename, 1) is None


def test_working_slot_reads_the_newest_state_in_it(state_dir):
    _touch(state_dir / "OLD01_1_1.ppst", mtime=1000)
    newest = _touch(state_dir / "NEW01_1_1.ppst", mtime=3000)
    _touch(state_dir / "OTHER_1_2.ppst", mtime=9000)

    assert ppsspp._state_for_slot(1) == newest


def test_working_slot_is_empty_when_it_holds_nothing(state_dir):
    _touch(state_dir / "OTHER_1_2.ppst")

    assert ppsspp._state_for_slot(1) is None


def test_state_target_names_a_push_for_the_working_slot(state_dir, monkeypatch):
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    target = ppsspp.Ppsspp().state_target("ULUS10041_1_5.ppst")

    assert target == state_dir / "ULUS10041_1_1.ppst"


def test_state_target_matches_the_state_already_in_the_slot(state_dir, monkeypatch):
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    existing = _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp.Ppsspp().state_target("ULUS10041_1_9.ppst") == existing
    # A different game cannot land on top of the state the slot is holding.
    assert ppsspp.Ppsspp().state_target("ULUS20041_1_9.ppst") is None


@pytest.mark.parametrize("filename", ["../escape_1.ppst", "", ".", "..", "notastate.bin"])
def test_state_target_refuses_a_name_ppsspp_would_never_write(state_dir, filename):
    assert ppsspp.Ppsspp().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(state_dir, monkeypatch):
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    stale = _touch(state_dir / "ULUS10041_1_1.ppst")
    stale_shot = _touch(state_dir / "ULUS10041_1_1.jpg")
    other = _touch(state_dir / "ULUS10041_1_2.ppst")

    ppsspp.Ppsspp().clear_working_slot()

    assert not stale.exists()
    assert not stale_shot.exists()
    assert other.exists()


def test_state_screenshot_path_matches_the_working_state(state_dir, monkeypatch):
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    shot = _touch(state_dir / "ULUS10041_1_1.jpg")

    assert ppsspp.Ppsspp().state_screenshot_path() == shot


def test_state_screenshot_path_is_none_without_a_thumbnail(state_dir, monkeypatch):
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp.Ppsspp().state_screenshot_path() is None


def test_the_game_window_is_the_one_titled_with_a_running_game():
    emu = ppsspp.Ppsspp()
    titles = {
        "111": "PPSSPP 1.20.4",
        "222": "Controller Settings",
        "333": "PPSSPP 1.20.4 - ULUS10041 : Some Game",
    }

    def fake_xdotool(*args):
        if args[0] == "search":
            return "111\n222\n333\n"
        if args[0] == "getwindowname":
            return titles[args[1]]
        return ""

    emu._xdotool = fake_xdotool

    assert emu._game_window() == "333"


def test_no_game_window_when_only_the_menu_is_up():
    emu = ppsspp.Ppsspp()
    emu._xdotool = lambda *args: "111\n" if args[0] == "search" else "PPSSPP 1.20.4"

    assert emu._game_window() is None


def test_load_state_refuses_an_empty_slot(state_dir):
    emu = ppsspp.Ppsspp()
    emu._send_key = lambda key: pytest.fail("hotkey sent at an empty slot")

    assert emu.load_state(1) is False


def test_exit_reports_the_working_slot_without_a_running_emulator(state_dir, monkeypatch):
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    report = ppsspp.Ppsspp().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
