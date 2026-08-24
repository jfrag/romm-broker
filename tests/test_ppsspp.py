"""PPSSPP ROM resolution, state naming, and window selection.

Covers picking a bootable image out of a folder, the working-slot state
naming contract, and finding the game window among PPSSPP's windows.
"""

import os
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import ppsspp


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PPSSPP ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(ppsspp, "ROM_ROOT", root)
    return root


@pytest.fixture
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PPSSPP state directory at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "PPSSPP_STATE"
    d.mkdir()
    monkeypatch.setattr(ppsspp, "STATE_DIR", d)
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


def test_rom_pick_prefers_the_compressed_image_beside_the_raw_one(rom_root: Path) -> None:
    """A .chd beside an .iso of the same game is the one picked."""
    game = rom_root / "game"
    _touch(game / "Game.iso")
    _touch(game / "Game.chd")

    assert ppsspp._pick_rom_file(game.glob("*"), game).name == "Game.chd"


def test_rom_pick_ignores_unbootable_and_hidden_files(rom_root: Path) -> None:
    """Files with the wrong extension or a leading dot are never picked."""
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.iso")

    assert ppsspp._pick_rom_file(game.glob("*"), game) is None


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """An image symlinked from outside the ROM root is never picked."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert ppsspp._pick_rom_file(game.glob("*"), game) is None


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.iso")

    assert ppsspp.Ppsspp().resolve_rom_file(rom) == rom


def test_resolve_searches_one_level_into_a_folder(rom_root: Path) -> None:
    """A folder is searched one level down for a bootable image."""
    _touch(rom_root / "game" / "inner" / "Game.iso")

    resolved = ppsspp.Ppsspp().resolve_rom_file(rom_root / "game")

    assert resolved.name == "Game.iso"


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that does not exist resolves to None."""
    assert ppsspp.Ppsspp().resolve_rom_file(rom_root / "gone") is None


def test_resolve_refuses_a_direct_path_that_is_a_symlink_out_of_the_library(
    rom_root: Path, tmp_path: Path
) -> None:
    """A direct path that is a symlink escaping the ROM library resolves to None."""
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
def test_restamp_keeps_the_game_and_rewrites_the_slot(filename: str, expected: str) -> None:
    """Restamping keeps the game id and rewrites only the slot number."""
    assert ppsspp._restamp_slot(filename, 7) == expected


@pytest.mark.parametrize(
    "filename",
    ["ULUS10041.ppst", "ULUS10041_1_1.jpg", "", "a/b_1.ppst"],
)
def test_restamp_refuses_anything_that_is_not_a_state_name(filename: str) -> None:
    """Restamping returns None for a name that is not a PPSSPP state name."""
    assert ppsspp._restamp_slot(filename, 1) is None


def test_working_slot_reads_the_newest_state_in_it(state_dir: Path) -> None:
    """The working slot resolves to the newest state in that slot, ignoring other slots."""
    _touch(state_dir / "OLD01_1_1.ppst", mtime=1000)
    newest = _touch(state_dir / "NEW01_1_1.ppst", mtime=3000)
    _touch(state_dir / "OTHER_1_2.ppst", mtime=9000)

    assert ppsspp._state_for_slot(1) == newest


def test_working_slot_is_empty_when_it_holds_nothing(state_dir: Path) -> None:
    """The working slot resolves to None when only other slots hold states."""
    _touch(state_dir / "OTHER_1_2.ppst")

    assert ppsspp._state_for_slot(1) is None


def test_state_target_names_a_push_for_the_working_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed state is targeted at the working slot under its own game id."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    target = ppsspp.Ppsspp().state_target("ULUS10041_1_5.ppst")

    assert target == state_dir / "ULUS10041_1_1.ppst"


def test_state_target_matches_the_state_already_in_the_slot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push for the game already in the slot targets that state, and another game is refused."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    existing = _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp.Ppsspp().state_target("ULUS10041_1_9.ppst") == existing
    # A different game cannot land on top of the state the slot is holding.
    assert ppsspp.Ppsspp().state_target("ULUS20041_1_9.ppst") is None


@pytest.mark.parametrize("filename", ["../escape_1.ppst", "", ".", "..", "notastate.bin"])
def test_state_target_refuses_a_name_ppsspp_would_never_write(state_dir: Path, filename: str) -> None:
    """A push whose name PPSSPP would never write is refused."""
    assert ppsspp.Ppsspp().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot removes its state and thumbnail and keeps the other slots."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    stale = _touch(state_dir / "ULUS10041_1_1.ppst")
    stale_shot = _touch(state_dir / "ULUS10041_1_1.jpg")
    other = _touch(state_dir / "ULUS10041_1_2.ppst")

    ppsspp.Ppsspp().clear_working_slot()

    assert not stale.exists()
    assert not stale_shot.exists()
    assert other.exists()


def test_state_screenshot_path_matches_the_working_state(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screenshot path is the .jpg beside the working slot's state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    _touch(state_dir / "ULUS10041_1_1.ppst")
    shot = _touch(state_dir / "ULUS10041_1_1.jpg")

    assert ppsspp.Ppsspp().state_screenshot_path() == shot


def test_state_screenshot_path_is_none_without_a_thumbnail(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screenshot path is None when the working state has no .jpg beside it."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)
    _touch(state_dir / "ULUS10041_1_1.ppst")

    assert ppsspp.Ppsspp().state_screenshot_path() is None


def test_the_game_window_is_the_one_titled_with_a_running_game() -> None:
    """The game window is the PPSSPP window whose title names a running game."""
    emu = ppsspp.Ppsspp()
    titles = {
        "111": "PPSSPP 1.20.4",
        "222": "Controller Settings",
        "333": "PPSSPP 1.20.4 - ULUS10041 : Some Game",
    }

    def fake_xdotool(*args: str) -> str:
        if args[0] == "search":
            return "111\n222\n333\n"
        if args[0] == "getwindowname":
            return titles[args[1]]
        return ""

    emu._xdotool = fake_xdotool

    assert emu._game_window() == "333"


def test_no_game_window_when_only_the_menu_is_up() -> None:
    """No game window is found while only the PPSSPP menu window is open."""
    emu = ppsspp.Ppsspp()
    emu._xdotool = lambda *args: "111\n" if args[0] == "search" else "PPSSPP 1.20.4"

    assert emu._game_window() is None


def test_load_state_refuses_an_empty_slot(state_dir: Path) -> None:
    """Loading an empty slot returns False without sending a hotkey."""
    emu = ppsspp.Ppsspp()
    emu._send_key = lambda key: pytest.fail("hotkey sent at an empty slot")

    assert emu.load_state(1) is False


def test_exit_reports_the_working_slot_without_a_running_emulator(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit with no emulator running reports the working slot and no saved state."""
    monkeypatch.setattr(ppsspp, "STATE_SLOT", 1)

    report = ppsspp.Ppsspp().save_and_exit(4)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
