"""Dolphin ROM resolution, state naming, and window selection."""

import os
from pathlib import Path

import pytest

from webstation_broker.emulators import dolphin


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(dolphin, "ROM_ROOT", root)
    return root


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    d = tmp_path / "StateSaves"
    d.mkdir()
    monkeypatch.setattr(dolphin, "STATE_DIR", d)
    return d


def _touch(path: Path, mtime=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Game.rvz", 1),
        ("Game (Disc 2).rvz", 2),
        ("Game.disc3.iso", 3),
        ("Game_cd-4.iso", 4),
        # A digit elsewhere in the name is not a disc number.
        ("Sonic Adventure 2.gcm", 1),
        ("Game (Disc 0).iso", 1),
    ],
)
def test_disc_number_reads_only_a_disc_marker(name, expected):
    assert dolphin._disc_number(Path(name)) == expected


def test_rom_pick_prefers_the_compressed_image_beside_the_raw_one(rom_root):
    game = rom_root / "game"
    _touch(game / "Game.iso")
    _touch(game / "Game.rvz")

    assert dolphin._pick_rom_file(game.glob("*"), game).name == "Game.rvz"


def test_rom_pick_prefers_the_first_disc(rom_root):
    game = rom_root / "game"
    _touch(game / "Game (Disc 2).iso")
    _touch(game / "Game (Disc 1).iso")

    assert dolphin._pick_rom_file(game.glob("*"), game).name == "Game (Disc 1).iso"


def test_rom_pick_ignores_unbootable_and_hidden_files(rom_root):
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.rvz")

    assert dolphin._pick_rom_file(game.glob("*"), game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root, tmp_path):
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert dolphin._pick_rom_file(game.glob("*"), game) is None


def test_resolve_takes_a_file_as_given(rom_root):
    rom = _touch(rom_root / "Game.rvz")

    assert dolphin.Dolphin().resolve_rom_file(rom) == rom


def test_resolve_searches_one_level_into_a_folder(rom_root):
    _touch(rom_root / "game" / "inner" / "Game.rvz")

    resolved = dolphin.Dolphin().resolve_rom_file(rom_root / "game")

    assert resolved.name == "Game.rvz"


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root):
    assert dolphin.Dolphin().resolve_rom_file(rom_root / "gone") is None


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root, tmp_path
):
    outside = tmp_path / "elsewhere.rvz"
    outside.write_bytes(b"rvz")
    linked = rom_root / "Game.rvz"
    linked.symlink_to(outside)

    assert dolphin.Dolphin().resolve_rom_file(linked) is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("GXCE01.s01", "GXCE01.s07"),
        ("GXCE01.s09", "GXCE01.s07"),
        ("Game Name (GXCE01).s02", "Game Name (GXCE01).s07"),
    ],
)
def test_restamp_keeps_the_game_and_rewrites_the_slot(filename, expected):
    assert dolphin._restamp_slot(filename, 7) == expected


@pytest.mark.parametrize(
    "filename",
    ["GXCE01.sav", "GXCE01.s1", "GXCE01.s001", "lastState.sav", "", "a/b.s01"],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename):
    assert dolphin._restamp_slot(filename, 1) is None


def test_working_slot_reads_the_newest_state_in_it(state_dir):
    _touch(state_dir / "OLD01.s01", mtime=1000)
    newest = _touch(state_dir / "NEW01.s01", mtime=3000)
    _touch(state_dir / "OTHER.s02", mtime=9000)

    assert dolphin._state_for_slot(1) == newest


def test_working_slot_is_empty_when_it_holds_nothing(state_dir):
    _touch(state_dir / "OTHER.s02")

    assert dolphin._state_for_slot(1) is None


def test_state_target_names_a_push_for_the_working_slot(state_dir, monkeypatch):
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)

    target = dolphin.Dolphin().state_target("GXCE01.s05")

    assert target == state_dir / "GXCE01.s01"


def test_state_target_matches_the_state_already_in_the_slot(state_dir, monkeypatch):
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    existing = _touch(state_dir / "GXCE01.s01")

    assert dolphin.Dolphin().state_target("GXCE01.s09") == existing
    # A different game cannot land on top of the state the slot is holding.
    assert dolphin.Dolphin().state_target("RMCE01.s09") is None


@pytest.mark.parametrize("filename", ["../escape.s01", "", ".", "..", "notastate.bin"])
def test_state_target_refuses_a_name_dolphin_would_never_write(state_dir, filename):
    assert dolphin.Dolphin().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(state_dir, monkeypatch):
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    stale = _touch(state_dir / "GXCE01.s01")
    other = _touch(state_dir / "GXCE01.s02")

    dolphin.Dolphin().clear_working_slot()

    assert not stale.exists()
    assert other.exists()


def test_the_undo_buffer_is_dropped_before_the_dump(state_dir):
    undo = _touch(state_dir / "lastState.sav")

    dolphin.Dolphin()._drop_undo_buffer()

    assert not undo.exists()


def test_the_render_window_is_the_one_titled_with_the_running_game():
    emu = dolphin.Dolphin()
    titles = {
        "111": "Dolphin 2606-280",
        "222": "Controller Settings",
        "333": "Dolphin 2606-280 | JIT64 SC | OpenGL | HLE | Custom Robo (GXCE01)",
    }

    def fake_xdotool(*args):
        if args[0] == "search":
            return "111\n222\n333\n"
        if args[0] == "getwindowname":
            return titles[args[1]]
        return ""

    emu._xdotool = fake_xdotool

    assert emu._render_window() == "333"


def test_no_render_window_when_only_the_main_window_is_up():
    emu = dolphin.Dolphin()
    emu._xdotool = lambda *args: "111\n" if args[0] == "search" else "Dolphin 2606-280"

    assert emu._render_window() is None


def test_load_state_refuses_an_empty_slot(state_dir):
    emu = dolphin.Dolphin()
    emu._send_key = lambda key: pytest.fail("hotkey sent at an empty slot")

    assert emu.load_state(1) is False


def test_memory_card_is_gamecube_only(tmp_path, monkeypatch):
    """GC has a physical card, Wii saves live in NAND and have none."""
    monkeypatch.setattr(dolphin, "USER_DIR", tmp_path)
    emu = dolphin.Dolphin()
    assert emu.memory_card_path(platform="ngc") == tmp_path / "GC"
    assert emu.memory_card_path(platform="wii") is None
    assert emu.memory_card_path() is None


def test_exit_reports_the_working_slot_without_a_running_emulator(state_dir, monkeypatch):
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    undo = _touch(state_dir / "lastState.sav")

    report = dolphin.Dolphin().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not undo.exists()
