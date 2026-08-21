"""Xenia ROM resolution and the launch command line.

Covers picking a disc, executable, or XBLA container out of a folder, and
the flags and storage root the launch hands to xenia.
"""

from pathlib import Path

import pytest

from webstation_broker.emulators import xenia


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Xenia ROM root at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The ROM root directory.
    """
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(xenia, "ROM_ROOT", root)
    return root


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Xenia data directory, save root, and log path under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The data directory xenia is given as its storage root; it is not created.
    """
    d = tmp_path / "xenia"
    monkeypatch.setattr(xenia, "DATA_DIR", d)
    monkeypatch.setattr(xenia.Xenia, "save_root", d)
    monkeypatch.setattr(xenia, "XENIA_LOG_PATH", tmp_path / "xenia.log")
    monkeypatch.setattr(xenia.Xenia, "log_path", tmp_path / "xenia.log")
    return d


def _touch(path: Path) -> Path:
    """Write a placeholder ROM file, creating parents.

    Args:
        path: The file to create.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"rom")
    return path


# ── ROM resolution ───────────────────────────────────────────────────────────


def test_resolve_takes_a_file_as_given(rom_root: Path) -> None:
    """A path that is already a file resolves to itself."""
    rom = _touch(rom_root / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(rom) == rom


def test_resolve_boots_an_extracted_dump_from_its_default_xex(rom_root: Path) -> None:
    """A folder holding default.xex resolves to that executable over a disc image."""
    game = rom_root / "game"
    _touch(game / "Game.iso")
    xex = _touch(game / "default.xex")

    assert xenia.Xenia().resolve_rom_file(game) == xex


def test_resolve_prefers_an_iso_over_a_stray_xex(rom_root: Path) -> None:
    """A disc image wins over an .xex that is not default.xex."""
    game = rom_root / "game"
    _touch(game / "update.xex")
    _touch(game / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(game).name == "Game.iso"


def test_resolve_picks_disc_one_of_a_multi_disc_folder(rom_root: Path) -> None:
    """A folder holding several discs resolves to disc one."""
    game = rom_root / "game"
    _touch(game / "Game (Disc 2).iso")
    _touch(game / "Game (Disc 1).iso")

    assert xenia.Xenia().resolve_rom_file(game).name == "Game (Disc 1).iso"


def test_resolve_searches_one_level_into_a_folder(rom_root: Path) -> None:
    """A folder is searched one level down for a bootable image."""
    _touch(rom_root / "game" / "inner" / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(rom_root / "game").name == "Game.iso"


def test_resolve_ignores_unbootable_and_hidden_files(rom_root: Path) -> None:
    """Files with the wrong extension or a leading dot are never picked."""
    game = rom_root / "game"
    _touch(game / "readme.txt")
    _touch(game / ".Game.iso")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_refuses_a_link_out_of_the_library(rom_root: Path, tmp_path: Path) -> None:
    """An image symlinked from outside the ROM root is never picked."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    game = rom_root / "game"
    game.mkdir()
    (game / "linked.iso").symlink_to(outside)

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_gives_up_on_a_path_that_is_not_there(rom_root: Path) -> None:
    """A path that does not exist resolves to None."""
    assert xenia.Xenia().resolve_rom_file(rom_root / "gone") is None


def _container(path: Path, magic: bytes = b"LIVE") -> Path:
    """Write a placeholder STFS package whose header starts with `magic`.

    Args:
        path: The file to create.
        magic: The four-byte package signature to write first.

    Returns:
        The path that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b"\x00" * 60)
    return path


def test_resolve_finds_the_xbla_package_under_a_full_content_tree(rom_root: Path) -> None:
    """An XBLA package under a full Content tree resolves to the package file."""
    # The layout as the console writes it and as RomM holds it, with the
    # game's own folder on top.
    game = rom_root / "DOOM"
    pkg = _container(
        game / "Content" / "0000000000000000" / "58410824" / "000D0000"
        / "5BE22631DA178A036A01DC57A30D1326FF562F1F58"
    )

    assert xenia.Xenia().resolve_rom_file(game) == pkg


def test_resolve_finds_the_package_when_the_title_id_is_the_root(rom_root: Path) -> None:
    """A folder named by the title id, holding the content type directly, resolves to the package."""
    game = rom_root / "58410960"
    pkg = _container(game / "000D0000" / "F3B26E77DCA7E3BE683193FC5F6AB46F70FE6A5E58")

    assert xenia.Xenia().resolve_rom_file(game) == pkg


def test_resolve_finds_a_games_on_demand_install(rom_root: Path) -> None:
    """A Games on Demand install resolves to its PIRS package, not the payload fragments."""
    game = rom_root / "Halo 3"
    pkg = _container(game / "Content" / "0000000000000000" / "4D5307E6" / "00007000" / "ABCDEF", b"PIRS")
    # The payload fragments live in a sibling directory named after the
    # package; they are not the thing to boot.
    _touch(pkg.parent / "ABCDEF.data" / "Data0000")

    assert xenia.Xenia().resolve_rom_file(game) == pkg


def test_resolve_leaves_dlc_and_title_updates_alone(rom_root: Path) -> None:
    """A tree holding only DLC and title update packages resolves to nothing."""
    game = rom_root / "game"
    title = game / "Content" / "0000000000000000" / "58410824"
    _container(title / "00000002" / "DLCPACK")
    _container(title / "000B0000" / "TU_1")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_refuses_a_file_in_the_right_place_with_the_wrong_magic(rom_root: Path) -> None:
    """A file in the package position without a package signature is not bootable."""
    game = rom_root / "game"
    _container(game / "58410824" / "000D0000" / "README", b"hello")

    assert xenia.Xenia().resolve_rom_file(game) is None


def test_resolve_prefers_an_executable_or_disc_over_a_container(rom_root: Path) -> None:
    """A disc image in the folder wins over a package deeper in the tree."""
    game = rom_root / "game"
    _container(game / "58410824" / "000D0000" / "PKG")
    iso = _touch(game / "Game.iso")

    assert xenia.Xenia().resolve_rom_file(game) == iso


# ── Launch ───────────────────────────────────────────────────────────────────


def _spawned(monkeypatch: pytest.MonkeyPatch, rom: Path, resume_slot: int | None = None) -> list[str]:
    """Launch with the process spawn stubbed out and hand back the command line.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        rom: The ROM path handed to launch().
        resume_slot: The slot to ask launch() to resume, if any.

    Returns:
        The argv the stubbed spawn received.
    """
    calls = []
    monkeypatch.setattr(xenia.Xenia, "_spawn", lambda self, cmd, env: calls.append(cmd))
    xenia.Xenia().launch(rom, resume_slot)
    assert len(calls) == 1
    return calls[0]


def test_launch_runs_headless_fullscreen_against_the_broker_storage_root(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """The launch runs xenia headless and fullscreen with the broker's storage root and no Discord."""
    rom = _touch(rom_root / "Game.iso")

    cmd = _spawned(monkeypatch, rom)

    assert cmd[0] == xenia.XENIA_BIN
    assert "--fullscreen" in cmd
    # Guest dialogs (storage select, sign-in) are auto-answered; nobody in
    # the stream could dismiss them otherwise.
    assert "--headless" in cmd
    assert f"--storage_root={data_dir}" in cmd
    assert "--discord=false" in cmd
    assert cmd[-1] == str(rom)


def test_launch_creates_the_storage_root(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """The launch creates the storage root directory before spawning."""
    rom = _touch(rom_root / "Game.iso")
    assert not data_dir.exists()

    _spawned(monkeypatch, rom)

    assert data_dir.is_dir()


def test_launch_ignores_a_resume_slot_because_there_are_no_states(
    monkeypatch: pytest.MonkeyPatch, rom_root: Path, data_dir: Path
) -> None:
    """A resume slot changes nothing on the command line, since Xenia has no states."""
    rom = _touch(rom_root / "Game.iso")

    cmd = _spawned(monkeypatch, rom, resume_slot=3)

    assert "3" not in cmd
    assert not xenia.Xenia.supports_states


def test_the_save_archive_is_the_content_tree(data_dir: Path) -> None:
    """The save archive is the content tree under the storage root."""
    # Saves are keyed by the profile XUID, and the profile lives under
    # content/ too, so the two have to travel together.
    emu = xenia.Xenia()

    assert emu.save_root == data_dir
    assert emu.save_subtrees == ("content",)
