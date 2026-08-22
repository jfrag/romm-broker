"""DuckStation ROM resolution, settings.ini patching, resume-state selection,
launch, and exit-on-save via graceful shutdown."""

import os
from pathlib import Path

import pytest

from webstation_broker.emulators import duckstation


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(duckstation, "ROM_ROOT", root)
    return root


@pytest.fixture
def duckstation_dirs(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    ini_path = data_dir / "settings.ini"
    sstate_dir = data_dir / "savestates"
    data_dir.mkdir()

    monkeypatch.setattr(duckstation, "DATA_DIR", data_dir)
    monkeypatch.setattr(duckstation, "INI_PATH", ini_path)
    monkeypatch.setattr(duckstation, "SSTATE_DIR", sstate_dir)
    monkeypatch.setattr(duckstation.Duckstation, "save_root", data_dir)
    return {"data_dir": data_dir, "ini_path": ini_path, "sstate_dir": sstate_dir}


def _touch(path: Path, mtime=None, content: bytes = b"state") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── resolve_rom_file / _pick_rom_file ───────────────────────────────────


def test_resolve_takes_a_direct_file_as_given(rom_root):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(rom) == rom


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root):
    missing = rom_root / "nope"

    assert duckstation.Duckstation().resolve_rom_file(missing) is None


def test_resolve_prefers_m3u_over_a_raw_bin_beside_it(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.bin").write_bytes(b"")
    m3u = folder / "MyGame.m3u"
    m3u.write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) == m3u


def test_resolve_prefers_disc_1_over_disc_2_at_the_same_extension_rank(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    disc1 = folder / "MyGame (Disc 1).cue"
    disc1.write_bytes(b"")
    (folder / "MyGame (Disc 2).cue").write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) == disc1


def test_resolve_ignores_dotfiles(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / ".hidden.chd").write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) is None


def test_resolve_ignores_extensions_it_does_not_recognize(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) is None


def test_resolve_refuses_a_disc_image_that_symlinks_outside_the_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.chd"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.chd").symlink_to(secret)

    assert duckstation.Duckstation().resolve_rom_file(folder) is None


def test_resolve_accepts_a_disc_image_that_symlinks_inside_the_rom_root(rom_root):
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real = shared / "actual.chd"
    real.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    link = folder / "MyGame.chd"
    link.symlink_to(real)

    assert duckstation.Duckstation().resolve_rom_file(folder) == real


def test_resolve_searches_one_level_of_subfolders(rom_root):
    folder = rom_root / "MyGame"
    sub = folder / "disc"
    sub.mkdir(parents=True)
    rom = sub / "MyGame.chd"
    rom.write_bytes(b"")

    assert duckstation.Duckstation().resolve_rom_file(folder) == rom


# ── settings.ini patching ───────────────────────────────────────────────


def test_patch_ini_seeds_a_missing_file_with_every_forced_key(duckstation_dirs):
    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "SetupWizardIncomplete = false" in text
    assert "ConfirmPowerOff = false" in text
    assert "SaveStateOnExit = true" in text
    assert "CreateSaveStateBackups = false" in text
    assert "[AutoUpdater]" in text
    assert "CheckAtStartup = false" in text


def test_patch_ini_overwrites_a_conflicting_value_but_keeps_the_rest(duckstation_dirs):
    duckstation.INI_PATH.write_text(
        "[Main]\n"
        "SaveStateOnExit = false\n"
        "SomeOtherSetting = 5\n"
    )

    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "SaveStateOnExit = true" in text
    assert "SomeOtherSetting = 5" in text


def test_patch_ini_adds_a_missing_key_into_an_existing_section(duckstation_dirs):
    duckstation.INI_PATH.write_text("[Main]\nSomeOtherSetting = 5\n")

    duckstation._patch_ini()

    lines = duckstation.INI_PATH.read_text().splitlines()
    assert "[Main]" in lines
    assert "ConfirmPowerOff = false" in lines


def test_patch_ini_adds_a_missing_section_entirely(duckstation_dirs):
    duckstation.INI_PATH.write_text("[Main]\nSetupWizardIncomplete = false\n")

    duckstation._patch_ini()

    text = duckstation.INI_PATH.read_text()
    assert "[AutoUpdater]" in text
    assert "CheckAtStartup = false" in text


def test_patch_ini_leaves_the_existing_file_untouched_when_the_tmp_write_fails(
    duckstation_dirs, monkeypatch
):
    original = "[Main]\nSetupWizardIncomplete = false\nSomeOtherSetting = 5\n"
    duckstation.INI_PATH.write_text(original)
    real_write_text = Path.write_text

    def guarded(self, *a, **kw):
        if self.suffix == ".tmp":
            raise OSError("disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", guarded)

    duckstation._patch_ini()  # must not raise

    assert duckstation.INI_PATH.read_text() == original


# ── resume state snapshot / diff ────────────────────────────────────────


def test_resume_snapshot_is_empty_without_a_savestates_dir(duckstation_dirs):
    assert duckstation._resume_snapshot() == {}


def test_newest_resume_state_picks_the_highest_mtime(duckstation_dirs):
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav", mtime=1000)
    newest = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav", mtime=3000)

    assert duckstation._newest_resume_state() == newest


def test_newest_resume_state_is_none_with_no_resume_files(duckstation_dirs):
    assert duckstation._newest_resume_state() is None


def test_changed_resume_state_finds_the_file_that_appeared(duckstation_dirs):
    before = duckstation._resume_snapshot()
    new = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    assert duckstation._changed_resume_state(before) == new


def test_changed_resume_state_finds_a_rewritten_file_by_size(duckstation_dirs):
    p = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    before = duckstation._resume_snapshot()
    p.write_bytes(b"a longer state than before")

    assert duckstation._changed_resume_state(before) == p


def test_changed_resume_state_is_none_when_nothing_moved(duckstation_dirs):
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    before = duckstation._resume_snapshot()

    assert duckstation._changed_resume_state(before) is None


# ── clear_working_slot ──────────────────────────────────────────────────


def test_clear_working_slot_is_a_noop_without_a_savestates_dir(duckstation_dirs):
    duckstation.Duckstation().clear_working_slot()  # must not raise

    assert not duckstation.SSTATE_DIR.exists()


def test_clear_working_slot_wipes_every_leftover_resume_state(duckstation_dirs):
    """A resume state left behind by an earlier game or session must not
    outrank (by mtime) whatever this session's own archive restore brings
    back, since selection has no serial filter at all."""
    stale_a = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    stale_b = _touch(duckstation.SSTATE_DIR / "SLUS-00002_resume.sav")

    duckstation.Duckstation().clear_working_slot()

    assert not stale_a.exists()
    assert not stale_b.exists()
    assert duckstation.SSTATE_DIR.is_dir()


def test_clear_working_slot_leaves_unrelated_files_alone(duckstation_dirs):
    unrelated = _touch(duckstation.SSTATE_DIR / "notes.txt")

    duckstation.Duckstation().clear_working_slot()

    assert unrelated.exists()


def test_clear_working_slot_tolerates_a_file_it_cannot_delete(
    duckstation_dirs, monkeypatch, caplog
):
    stuck = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    def boom(self):
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", boom)

    with caplog.at_level("WARNING"):
        duckstation.Duckstation().clear_working_slot()  # must not raise

    assert stuck.exists()
    assert "could not clear stale resume state" in caplog.text


# ── launch ───────────────────────────────────────────────────────────────


def test_launch_stops_then_patches_ini_then_spawns(duckstation_dirs, rom_root, monkeypatch):
    order = []
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: order.append("stop"))
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: order.append("patch_ini"))

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        order.append("spawn")

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=None)

    assert order == ["stop", "patch_ini", "spawn"]


def test_launch_with_no_resume_slot_omits_statefile(
    duckstation_dirs, rom_root, monkeypatch
):
    monkeypatch.delenv("DUCKSTATION_BIN", raising=False)
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=None)

    assert spawned["cmd"] == [
        "/opt/duckstation/AppRun",
        "-batch",
        "-fullscreen",
        "--",
        str(rom),
    ]


def test_launch_with_a_resume_slot_boots_the_newest_resume_state(
    duckstation_dirs, rom_root, monkeypatch
):
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    state = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    duckstation.Duckstation().launch(rom, resume_slot=1)

    assert "-statefile" in spawned["cmd"]
    assert str(state) in spawned["cmd"]
    assert spawned["cmd"][-2:] == ["--", str(rom)]


def test_launch_with_a_resume_slot_but_no_state_boots_fresh(
    duckstation_dirs, rom_root, monkeypatch, caplog
):
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    monkeypatch.setattr(duckstation, "_patch_ini", lambda: None)
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd

    monkeypatch.setattr(duckstation.Duckstation, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    with caplog.at_level("WARNING"):
        duckstation.Duckstation().launch(rom, resume_slot=1)

    assert "-statefile" not in spawned["cmd"]
    assert "resume requested but no resume state" in caplog.text


# ── save_and_exit ────────────────────────────────────────────────────────


def test_exit_without_a_slot_reports_nothing_but_still_stops(duckstation_dirs, monkeypatch):
    stopped = []
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: stopped.append(True))
    emu = duckstation.Duckstation()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert stopped == [True]


def test_exit_with_a_slot_reports_the_state_the_shutdown_wrote(duckstation_dirs, monkeypatch):
    written = _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")

    class FakeProc:
        returncode = 0  # a normal, non-SIGKILL exit

    def fake_stop(self):
        # Simulates the graceful shutdown writing a fresh resume state.
        written.write_bytes(b"a fresh resume state")

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(written)


def test_exit_with_a_slot_reports_no_save_when_no_new_state_appears(
    duckstation_dirs, monkeypatch, caplog
):
    class FakeProc:
        returncode = 0  # a normal, non-SIGKILL exit

    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "no resume state written during shutdown" in caplog.text


def test_exit_with_a_slot_discards_a_state_from_a_force_killed_process(
    duckstation_dirs, monkeypatch, caplog
):
    """SIGKILL escalation (term_timeout exceeded) can cut the resume state
    write off mid-flight. Such a file must not just go unreported: since the
    save archive dump sweeps up anything with a fresh mtime regardless of
    what this method reports, the incomplete file has to be removed outright
    or it ships to RomM anyway and gets booted next session."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = -9  # -signal.SIGKILL

    def fake_stop(self):
        # Simulates a partial write landing before the kill lands.
        _touch(written)

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not written.exists()
    assert "force-killed" in caplog.text


def test_exit_with_a_slot_discards_a_state_when_stop_never_confirms_the_exit(
    duckstation_dirs, monkeypatch
):
    """proc.returncode stays None if even the post-SIGKILL wait() times out;
    that is not proof of a clean shutdown either, so it must be treated the
    same as a confirmed kill rather than trusted by default."""
    written = duckstation.SSTATE_DIR / "SLUS-00001_resume.sav"

    class FakeProc:
        returncode = None

    def fake_stop(self):
        _touch(written)

    monkeypatch.setattr(duckstation.Duckstation, "stop", fake_stop)
    emu = duckstation.Duckstation()
    emu._proc = FakeProc()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not written.exists()


def test_exit_with_a_slot_but_not_alive_reports_nothing(duckstation_dirs, monkeypatch):
    """No process running means SIGTERM's shutdown write can never happen, so
    there is nothing to diff for."""
    _touch(duckstation.SSTATE_DIR / "SLUS-00001_resume.sav")
    monkeypatch.setattr(duckstation.Duckstation, "stop", lambda self: None)
    emu = duckstation.Duckstation()
    monkeypatch.setattr(emu, "alive", lambda: False)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


# ── class attributes (API surface parity with the other exit-only emulators) ──


def test_save_subtrees_cover_both_memcards_and_savestates(duckstation_dirs):
    """These are the two directories the generic save-archive dump walks;
    dropping either would silently stop shipping that data to RomM."""
    emu = duckstation.Duckstation()

    assert emu.save_subtrees == ("memcards", "savestates")
    assert emu.rom_extensions == (
        ".m3u", ".chd", ".cue", ".pbp", ".ccd", ".mds",
        ".iso", ".img", ".ecm", ".bin", ".exe", ".psexe",
    )
