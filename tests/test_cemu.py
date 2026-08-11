"""Cemu ROM resolution, settings.xml patching, pad profile seeding, and the
exit-time save refresh."""

import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from webstation_broker.emulators import cemu


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(cemu, "ROM_ROOT", root)
    return root


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    d = tmp_path / "config" / "Cemu"
    monkeypatch.setattr(cemu, "SETTINGS_PATH", d / "settings.xml")
    monkeypatch.setattr(cemu, "PROFILE_PATH", d / "controllerProfiles" / "controller0.xml")
    return d


@pytest.fixture
def save_dir(monkeypatch, tmp_path):
    mlc = tmp_path / "mlc01"
    save = mlc / "usr" / "save"
    save.mkdir(parents=True)
    monkeypatch.setattr(cemu, "MLC_DIR", mlc)
    monkeypatch.setattr(cemu, "SAVE_DIR", save)
    return save


def _touch(path: Path, mtime=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_rom_pick_prefers_the_archive_over_the_raw_image(rom_root):
    game = rom_root / "Game"
    _touch(game / "Game.wud")
    best = _touch(game / "Game.wua")

    assert cemu.Cemu().resolve_rom_file(game) == best


def test_rom_pick_finds_the_rpx_inside_an_extracted_dump(rom_root):
    game = rom_root / "Game"
    rpx = _touch(game / "code" / "Game.rpx")
    _touch(game / "content" / "data.bin")
    _touch(game / "meta" / "meta.xml")

    assert cemu.Cemu().resolve_rom_file(game) == rpx


def test_rom_pick_reaches_a_dump_wrapped_in_a_library_folder(rom_root):
    game = rom_root / "Game"
    rpx = _touch(game / "Game [TITLEID]" / "code" / "Game.rpx")

    assert cemu.Cemu().resolve_rom_file(game) == rpx


def test_rom_pick_skips_the_update_beside_the_base_game(rom_root):
    game = rom_root / "Game"
    _touch(game / "Game (Update)" / "code" / "Game.rpx")
    base = _touch(game / "Game.wux")

    assert cemu.Cemu().resolve_rom_file(game) == base


def test_rom_pick_refuses_a_link_out_of_the_library(rom_root, tmp_path):
    outside = _touch(tmp_path / "outside" / "Game.wua")
    game = rom_root / "Game"
    game.mkdir()
    (game / "Game.wua").symlink_to(outside)

    assert cemu.Cemu().resolve_rom_file(game) is None


def test_resolve_takes_a_file_as_given(rom_root):
    rom = _touch(rom_root / "Game.wux")
    assert cemu.Cemu().resolve_rom_file(rom) == rom


def test_settings_are_seeded_when_the_file_is_missing(config_dir):
    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    assert root.tag == "content"
    assert root.find("check_update").text == "false"
    assert root.find("use_discord_presence").text == "false"


def test_settings_patch_keeps_what_the_user_tuned(config_dir):
    config_dir.mkdir(parents=True)
    cemu.SETTINGS_PATH.write_text(
        "<content><check_update>true</check_update>"
        "<vsync>1</vsync><graphic_api>1</graphic_api></content>"
    )

    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    assert root.find("check_update").text == "false"
    assert root.find("vsync").text == "1"
    assert root.find("graphic_api").text == "1"


def test_a_broken_settings_file_is_reseeded_not_fatal(config_dir):
    config_dir.mkdir(parents=True)
    cemu.SETTINGS_PATH.write_text("<content><unclosed>")

    cemu._patch_settings()

    root = ET.parse(cemu.SETTINGS_PATH).getroot()
    assert root.find("check_update").text == "false"


def test_crc16_matches_the_arc_check_value():
    assert cemu._crc16(b"123456789") == 0xBB3D


def test_the_classic_pad_guid_is_the_known_xpad_entry():
    assert cemu._sdl_guid(0) == "030000005e0400008e02000010010000"


def test_pad_profile_carries_both_guid_variants(config_dir):
    cemu._seed_pad_profile()

    root = ET.parse(cemu.PROFILE_PATH).getroot()
    assert root.find("type").text == "Wii U GamePad"
    uuids = [c.find("uuid").text for c in root.findall("controller")]
    assert len(uuids) == 2 and len(set(uuids)) == 2
    for controller in root.findall("controller"):
        entries = controller.find("mappings").findall("entry")
        assert len(entries) == len(cemu._VPAD_SDL_MAPPINGS)


def test_pad_profile_is_not_overwritten_once_present(config_dir):
    cemu.PROFILE_PATH.parent.mkdir(parents=True)
    cemu.PROFILE_PATH.write_text("player tuned")

    cemu._seed_pad_profile()

    assert cemu.PROFILE_PATH.read_text() == "player tuned"


def test_pad_uuids_can_be_pinned_by_env(monkeypatch):
    monkeypatch.setenv("CEMU_PAD_UUIDS", "0_aaaa, 1_bbbb")
    assert cemu._pad_uuids() == ["0_aaaa", "1_bbbb"]


def test_exit_refreshes_only_the_title_saves_this_session_wrote(save_dir):
    emu = cemu.Cemu()
    emu._session_start = time.time() - 100

    stale = _touch(
        save_dir / "00050000" / "aaaaaaaa" / "user" / "80000001" / "old.dat",
        mtime=emu._session_start - 500,
    )
    partner = _touch(
        save_dir / "00050000" / "bbbbbbbb" / "user" / "80000001" / "old.dat",
        mtime=emu._session_start - 500,
    )
    _touch(save_dir / "00050000" / "bbbbbbbb" / "user" / "80000001" / "new.dat")
    system = _touch(
        save_dir / "system" / "act" / "80000001" / "account.dat",
        mtime=emu._session_start + 10,
    )
    system_mtime = system.stat().st_mtime

    emu.save_and_exit(10)

    # The touched title ships whole; the untouched title and the system
    # tree keep their stamps.
    assert partner.stat().st_mtime >= emu._session_start
    assert stale.stat().st_mtime < emu._session_start
    assert system.stat().st_mtime == system_mtime


def test_exit_reports_no_state(save_dir):
    report = cemu.Cemu().save_and_exit(10)
    assert report == {"state_saved": None, "state_slot": None, "state_file": None}
