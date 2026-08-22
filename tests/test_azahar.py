"""Azahar (3DS) ROM resolution, qt-config.ini patching, launch, and save-dump
mtime restamping."""

import configparser
import os
import time
from pathlib import Path

import pytest

from webstation_broker.emulators import azahar


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(azahar, "ROM_ROOT", root)
    return root


def test_class_declares_no_save_state_or_disc_swap_support():
    assert azahar.Azahar.supports_states is False
    assert azahar.Azahar.supports_disc_swap is False


# ---- _xdg_dir ----


def test_xdg_dir_uses_the_absolute_env_var_when_set(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    assert azahar._xdg_dir("XDG_DATA_HOME", ".local/share") == "/custom/data/azahar-emu"


def test_xdg_dir_falls_back_to_home_relative_path_when_unset(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/testuser")
    assert (
        azahar._xdg_dir("XDG_DATA_HOME", ".local/share")
        == "/home/testuser/.local/share/azahar-emu"
    )


def test_xdg_dir_ignores_a_relative_env_var(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    monkeypatch.setenv("HOME", "/home/testuser")
    assert (
        azahar._xdg_dir("XDG_DATA_HOME", ".local/share")
        == "/home/testuser/.local/share/azahar-emu"
    )


# ---- resolve_rom_file / _pick_rom_file ----


def test_resolve_takes_a_direct_file_as_given(rom_root):
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(rom) == rom


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root):
    missing = rom_root / "nope"

    assert azahar.Azahar().resolve_rom_file(missing) is None


def test_resolve_returns_none_when_the_folder_has_no_candidates(rom_root):
    folder = rom_root / "Empty"
    folder.mkdir()

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_finds_a_rom_directly_inside_a_folder(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    rom = folder / "game.3ds"
    rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == rom


def test_resolve_finds_a_rom_one_level_deeper_in_a_wrapper_folder(rom_root):
    folder = rom_root / "MyGame"
    inner = folder / "disc"
    inner.mkdir(parents=True)
    rom = inner / "game.cxi"
    rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == rom


def test_resolve_ignores_hidden_files(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / ".game.3ds").write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_ignores_non_rom_extensions(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_prefers_the_earlier_extension_in_priority_order(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.cci").write_bytes(b"")
    threeds = folder / "game.3ds"
    threeds.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == threeds


def test_resolve_deprioritizes_update_and_dlc_files(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "update.3ds").write_bytes(b"")
    base_rom = folder / "base.3ds"
    base_rom.write_bytes(b"")

    assert azahar.Azahar().resolve_rom_file(folder) == base_rom


def test_resolve_refuses_a_rom_that_symlinks_outside_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.3ds"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.3ds").symlink_to(secret)

    assert azahar.Azahar().resolve_rom_file(folder) is None


def test_resolve_accepts_a_rom_that_symlinks_inside_rom_root(rom_root):
    shared = rom_root / "Shared"
    shared.mkdir()
    real = shared / "actual.3ds"
    real.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.3ds").symlink_to(real)

    # _pick_rom_file ranks and returns the resolved real path, not the
    # symlink that was found; both point at the same bootable file.
    assert azahar.Azahar().resolve_rom_file(folder) == real


def test_resolve_ignores_a_dangling_symlink(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "game.3ds").symlink_to(rom_root / "does-not-exist")

    assert azahar.Azahar().resolve_rom_file(folder) is None


# ---- _patch_config ----


@pytest.fixture
def config_path(monkeypatch, tmp_path):
    path = tmp_path / "azahar-config" / "qt-config.ini"
    monkeypatch.setattr(azahar, "CONFIG_PATH", path)
    return path


def _read_ini(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def test_patch_config_creates_missing_parent_directories(config_path):
    assert not config_path.parent.exists()

    azahar._patch_config()

    assert config_path.exists()


def test_patch_config_seeds_a_missing_file_with_every_forced_key(config_path):
    azahar._patch_config()

    parser = _read_ini(config_path)
    assert parser["UI"]["confirmClose"] == "false"
    assert parser["UI"]["check_for_update_on_start"] == "false"
    assert parser["UI"]["enable_discord_presence"] == "false"


def test_patch_config_overwrites_a_conflicting_value_but_keeps_the_rest(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "[UI]\nconfirmClose=true\ncustomSetting=keepme\n\n[Other]\nfoo=bar\n"
    )

    azahar._patch_config()

    parser = _read_ini(config_path)
    assert parser["UI"]["confirmClose"] == "false"
    assert parser["UI"]["customSetting"] == "keepme"
    assert parser["Other"]["foo"] == "bar"


def test_patch_config_preserves_key_case(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[UI]\nMixedCaseKey=Value\n")

    azahar._patch_config()

    assert "MixedCaseKey" in config_path.read_text()


def test_patch_config_reseeds_a_file_that_fails_to_decode(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"\x80\x81\x82 not valid utf-8")

    azahar._patch_config()

    parser = _read_ini(config_path)
    assert parser["UI"]["confirmClose"] == "false"


def test_patch_config_does_not_raise_when_the_directory_cannot_be_created(
    monkeypatch, config_path
):
    def fail_mkdir(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    azahar._patch_config()  # must not raise


# ---- launch ----


def test_launch_stops_first_patches_config_and_spawns(monkeypatch, rom_root, config_path):
    order = []
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: order.append("stop"))
    patched = []
    real_patch = azahar._patch_config

    def tracking_patch():
        patched.append(True)
        real_patch()

    monkeypatch.setattr(azahar, "_patch_config", tracking_patch)
    spawned = {}

    def fake_spawn(self, cmd, env):
        order.append("spawn")
        spawned["cmd"] = cmd
        spawned["env"] = env

    monkeypatch.setattr(azahar.Azahar, "_spawn", fake_spawn)
    monkeypatch.setenv("AZAHAR_BIN", "/opt/azahar/AppRun")
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()

    emu.launch(rom, resume_slot=None)

    assert order == ["stop", "spawn"]
    assert patched == [True]
    assert spawned["cmd"] == ["/opt/azahar/AppRun", "-w", str(rom)]


def test_launch_uses_the_default_binary_path_when_unset(monkeypatch, rom_root, config_path):
    monkeypatch.delenv("AZAHAR_BIN", raising=False)
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        azahar.Azahar, "_spawn", lambda self, cmd, env: spawned.update(cmd=cmd)
    )
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()

    emu.launch(rom, resume_slot=None)

    assert spawned["cmd"][0] == "/opt/azahar/AppRun"


def test_launch_logs_and_ignores_a_resume_slot(monkeypatch, rom_root, config_path, caplog):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    monkeypatch.setattr(azahar.Azahar, "_spawn", lambda self, cmd, env: None)
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()

    with caplog.at_level("INFO"):
        emu.launch(rom, resume_slot=4)

    assert "resume_slot 4 ignored" in caplog.text


def test_launch_records_the_session_start_time(monkeypatch, rom_root, config_path):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    monkeypatch.setattr(azahar.Azahar, "_spawn", lambda self, cmd, env: None)
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")
    emu = azahar.Azahar()
    before = time.time()

    emu.launch(rom, resume_slot=None)

    assert before <= emu._session_start <= time.time()


def test_launch_uses_windowed_not_fullscreen(monkeypatch, rom_root, config_path):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    monkeypatch.setattr(azahar, "_patch_config", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        azahar.Azahar, "_spawn", lambda self, cmd, env: spawned.update(cmd=cmd)
    )
    rom = rom_root / "game.3ds"
    rom.write_bytes(b"")

    azahar.Azahar().launch(rom, resume_slot=None)

    assert "-w" in spawned["cmd"]
    assert "-f" not in spawned["cmd"]
    assert "--fullscreen" not in spawned["cmd"]


# ---- prepare_restore ----


def test_prepare_restore_stops_the_emulator(monkeypatch):
    stopped = []
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: stopped.append(True))

    azahar.Azahar().prepare_restore()

    assert stopped == [True]


# ---- _modified_title_saves / save_and_exit ----


@pytest.fixture
def save_roots(monkeypatch, tmp_path):
    sdmc_title = tmp_path / "sdmc_title"
    sdmc_extdata = tmp_path / "sdmc_extdata"
    nand_extdata = tmp_path / "nand_extdata"
    nand_sysdata = tmp_path / "nand_sysdata"
    for d in (sdmc_title, sdmc_extdata, nand_extdata, nand_sysdata):
        d.mkdir()
    roots = (sdmc_title, sdmc_extdata, nand_extdata, nand_sysdata)
    monkeypatch.setattr(azahar, "_SAVE_GROUP_ROOTS", roots)
    return roots


def test_modified_title_saves_includes_a_title_touched_this_session(save_roots):
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == [title]


def test_modified_title_saves_excludes_a_title_not_touched_this_session(save_roots):
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = time.time() + 10_000

    assert emu._modified_title_saves() == []


def test_modified_title_saves_skips_a_non_hex_title_high_dir(save_roots):
    root = save_roots[0]
    title = root / "not-hex-8" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == []


def test_modified_title_saves_skips_a_non_hex_title_low_dir(save_roots):
    root = save_roots[0]
    title = root / "00010032" / "not-hex-8"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == []


def test_modified_title_saves_ignores_a_missing_root(save_roots):
    for root in save_roots:
        root.rmdir()
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert emu._modified_title_saves() == []


def test_modified_title_saves_covers_every_save_group_root(save_roots):
    titles = []
    for root in save_roots:
        title = root / "00010032" / "00040000"
        title.mkdir(parents=True)
        (title / "save.bin").write_bytes(b"data")
        titles.append(title)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    assert sorted(emu._modified_title_saves()) == sorted(titles)


def test_save_and_exit_stops_the_emulator(monkeypatch, save_roots):
    stopped = []
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: stopped.append(True))
    emu = azahar.Azahar()
    emu._session_start = 0.0

    emu.save_and_exit(slot=1)

    assert stopped == [True]


def test_save_and_exit_returns_no_state_shape(monkeypatch, save_roots):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    result = emu.save_and_exit(slot=1)

    assert result == {"state_saved": None, "state_slot": None, "state_file": None}


def test_save_and_exit_restamps_every_file_in_a_touched_title_dir(monkeypatch, save_roots):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    touched = title / "save.bin"
    untouched_sibling = title / "misc.bin"
    touched.write_bytes(b"data")
    untouched_sibling.write_bytes(b"data2")
    old = time.time() - 10_000
    os.utime(untouched_sibling, (old, old))
    emu = azahar.Azahar()
    emu._session_start = time.time() - 1  # before touched's just-written mtime

    emu.save_and_exit(slot=1)

    assert os.stat(untouched_sibling).st_mtime > old


def test_save_and_exit_leaves_untouched_title_dirs_alone(monkeypatch, save_roots):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    f = title / "save.bin"
    f.write_bytes(b"data")
    old = time.time() - 10_000
    os.utime(f, (old, old))
    emu = azahar.Azahar()
    emu._session_start = time.time()  # after f's mtime: not touched this session

    emu.save_and_exit(slot=1)

    assert os.stat(f).st_mtime == pytest.approx(old, abs=1)


def test_save_and_exit_logs_and_continues_when_a_restamp_fails(
    monkeypatch, save_roots, caplog
):
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")

    def fail_utime(path, times):
        raise OSError("boom")

    monkeypatch.setattr(azahar.os, "utime", fail_utime)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    with caplog.at_level("WARNING"):
        result = emu.save_and_exit(slot=1)

    assert "could not restamp" in caplog.text
    assert result == {"state_saved": None, "state_slot": None, "state_file": None}


def test_save_and_exit_logs_and_continues_when_the_walk_itself_raises(
    monkeypatch, save_roots, caplog
):
    """A title dir vanishing mid-walk must not crash the exit route: the
    caller already committed to stopping the emulator by this point."""
    monkeypatch.setattr(azahar.Azahar, "stop", lambda self: None)
    root = save_roots[0]
    title = root / "00010032" / "00040000"
    title.mkdir(parents=True)
    (title / "save.bin").write_bytes(b"data")
    monkeypatch.setattr(azahar.Azahar, "_modified_title_saves", lambda self: [title])

    def fail_rglob(self, pattern):
        raise OSError("directory vanished mid-walk")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    emu = azahar.Azahar()
    emu._session_start = 0.0

    with caplog.at_level("WARNING"):
        result = emu.save_and_exit(slot=1)

    assert "could not walk" in caplog.text
    assert result == {"state_saved": None, "state_slot": None, "state_file": None}
