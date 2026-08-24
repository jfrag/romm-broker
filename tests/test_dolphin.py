"""Dolphin ROM resolution, state naming, and window selection.

Covers picking a bootable image out of a folder, the working-slot state
naming contract, the undo buffer, and finding the render window.
"""

import os
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import dolphin


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Dolphin ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(dolphin, "ROM_ROOT", root)
    return root


@pytest.fixture
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Dolphin state directory at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "StateSaves"
    d.mkdir()
    monkeypatch.setattr(dolphin, "STATE_DIR", d)
    return d


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    """Write a placeholder file, creating parents, optionally with a fixed mtime.

    Args:
        path: The file to create.
        mtime: Modification time to stamp on it, if any.

    Returns:
        The path that was written.
    """
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
def test_disc_number_reads_only_a_disc_marker(name: str, expected: int) -> None:
    """The disc number comes from an explicit disc marker, not any digit in the name."""
    assert dolphin._disc_number(Path(name)) == expected


def test_rom_pick_prefers_the_compressed_image_beside_the_raw_one(rom_root: Path) -> None:
    """A .rvz beside an .iso of the same game is the one picked."""
    game = rom_root / "game"
    _touch(game / "Game.iso")
    _touch(game / "Game.rvz")

    assert dolphin._pick_rom_file(game.glob("*"), game).name == "Game.rvz"


def test_rom_pick_prefers_the_first_disc(rom_root: Path) -> None:
    """A folder holding several discs resolves to disc one."""
    game = rom_root / "game"
    _touch(game / "Game (Disc 2).iso")
    _touch(game / "Game (Disc 1).iso")

    assert dolphin._pick_rom_file(game.glob("*"), game).name == "Game (Disc 1).iso"


def test_rom_pick_ignores_unbootable_and_hidden_files(rom_root: Path) -> None:
    """Files with the wrong extension or a leading dot are never picked."""
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.rvz")

    assert dolphin._pick_rom_file(game.glob("*"), game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """An image symlinked from outside the ROM root is never picked."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert dolphin._pick_rom_file(game.glob("*"), game) is None


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.rvz")

    assert dolphin.Dolphin().resolve_rom_file(rom) == rom


def test_resolve_searches_one_level_into_a_folder(rom_root: Path) -> None:
    """A folder is searched one level down for a bootable image."""
    _touch(rom_root / "game" / "inner" / "Game.rvz")

    resolved = dolphin.Dolphin().resolve_rom_file(rom_root / "game")

    assert resolved.name == "Game.rvz"


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that does not exist resolves to None."""
    assert dolphin.Dolphin().resolve_rom_file(rom_root / "gone") is None


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root: Path, tmp_path: Path
) -> None:
    """A direct path that is a symlink escaping the ROM library resolves to None."""
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
def test_restamp_keeps_the_game_and_rewrites_the_slot(filename: str, expected: str) -> None:
    """Restamping keeps the game id and rewrites only the slot number."""
    assert dolphin._restamp_slot(filename, 7) == expected


@pytest.mark.parametrize(
    "filename",
    ["GXCE01.sav", "GXCE01.s1", "GXCE01.s001", "lastState.sav", "", "a/b.s01"],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename: str) -> None:
    """Restamping returns None for a name that is not a Dolphin state name."""
    assert dolphin._restamp_slot(filename, 1) is None


def test_working_slot_reads_the_newest_state_in_it(state_dir: Path) -> None:
    """The working slot resolves to the newest state in that slot, ignoring other slots."""
    _touch(state_dir / "OLD01.s01", mtime=1000)
    newest = _touch(state_dir / "NEW01.s01", mtime=3000)
    _touch(state_dir / "OTHER.s02", mtime=9000)

    assert dolphin._state_for_slot(1) == newest


def test_working_slot_is_empty_when_it_holds_nothing(state_dir: Path) -> None:
    """The working slot resolves to None when only other slots hold states."""
    _touch(state_dir / "OTHER.s02")

    assert dolphin._state_for_slot(1) is None


def test_state_target_names_a_push_for_the_working_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed state is targeted at the working slot under its own game id."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)

    target = dolphin.Dolphin().state_target("GXCE01.s05")

    assert target == state_dir / "GXCE01.s01"


def test_state_target_matches_the_state_already_in_the_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push for the game already in the slot targets that state, and another game is refused."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    existing = _touch(state_dir / "GXCE01.s01")

    assert dolphin.Dolphin().state_target("GXCE01.s09") == existing
    # A different game cannot land on top of the state the slot is holding.
    assert dolphin.Dolphin().state_target("RMCE01.s09") is None


@pytest.mark.parametrize("filename", ["../escape.s01", "", ".", "..", "notastate.bin"])
def test_state_target_refuses_a_name_dolphin_would_never_write(state_dir: Path, filename: str) -> None:
    """A push whose name Dolphin would never write is refused."""
    assert dolphin.Dolphin().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot removes its state and keeps the other slots."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    stale = _touch(state_dir / "GXCE01.s01")
    other = _touch(state_dir / "GXCE01.s02")

    dolphin.Dolphin().clear_working_slot()

    assert not stale.exists()
    assert other.exists()


def test_the_undo_buffer_is_dropped_before_the_dump(state_dir: Path) -> None:
    """Dropping the undo buffer removes lastState.sav from the state directory."""
    undo = _touch(state_dir / "lastState.sav")

    dolphin.Dolphin()._drop_undo_buffer()

    assert not undo.exists()


def test_the_render_window_is_the_one_titled_with_the_running_game() -> None:
    """The render window is the Dolphin window whose title names the running game."""
    emu = dolphin.Dolphin()
    titles = {
        "111": "Dolphin 2606-280",
        "222": "Controller Settings",
        "333": "Dolphin 2606-280 | JIT64 SC | OpenGL | HLE | Custom Robo (GXCE01)",
    }

    def fake_xdotool(*args: str) -> str:
        if args[0] == "search":
            return "111\n222\n333\n"
        if args[0] == "getwindowname":
            return titles[args[1]]
        return ""

    emu._xdotool = fake_xdotool

    assert emu._render_window() == "333"


def test_no_render_window_when_only_the_main_window_is_up() -> None:
    """No render window is found while only Dolphin's main window is open."""
    emu = dolphin.Dolphin()
    emu._xdotool = lambda *args: "111\n" if args[0] == "search" else "Dolphin 2606-280"

    assert emu._render_window() is None


def test_load_state_refuses_an_empty_slot(state_dir: Path) -> None:
    """Loading an empty slot returns False without sending a hotkey."""
    emu = dolphin.Dolphin()
    emu._send_key = lambda key: pytest.fail("hotkey sent at an empty slot")

    assert emu.load_state(1) is False


def test_memory_card_is_gamecube_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GC has a physical card, Wii saves live in NAND and have none."""
    monkeypatch.setattr(dolphin, "USER_DIR", tmp_path)
    emu = dolphin.Dolphin()
    assert emu.memory_card_path(platform="ngc") == tmp_path / "GC"
    assert emu.memory_card_path(platform="wii") is None
    assert emu.memory_card_path() is None


def test_exit_reports_the_working_slot_without_a_running_emulator(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with no emulator running reports the working slot and drops the undo buffer."""
    monkeypatch.setattr(dolphin, "STATE_SLOT", 1)
    undo = _touch(state_dir / "lastState.sav")

    report = dolphin.Dolphin().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not undo.exists()
