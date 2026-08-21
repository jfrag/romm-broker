"""shadPS4 ROM resolution, binary version selection, launch, and IPC-driven stop."""

import subprocess
from pathlib import Path

import pytest

from webstation_broker.emulators import base, shadps4


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(shadps4, "ROM_ROOT", root)
    return root


def test_resolve_refuses_an_eboot_that_symlinks_out_of_the_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(secret)

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_takes_a_direct_file_as_given(rom_root):
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(rom) == rom


def test_resolve_finds_eboot_inside_a_game_folder(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    eboot = folder / "eboot.bin"
    eboot.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(folder) == eboot


def test_resolve_falls_back_to_the_bare_folder_when_there_is_no_eboot(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()

    assert shadps4.Shadps4().resolve_rom_file(folder) == folder


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root):
    missing = rom_root / "nope"

    assert shadps4.Shadps4().resolve_rom_file(missing) is None
